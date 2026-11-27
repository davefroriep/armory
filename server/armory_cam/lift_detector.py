"""Pose-driven lifting set-block detector & unrack-to-rack boundary trimmer (V2).

Combines YOLO11n-Pose periodic rep-cycle detection, working-posture exit gating,
and lightweight pre-unrack <-> rack visual rest-frame symmetry to isolate crisp
`[unrack -> reps -> rack] +/- 1.0s` blocks from trimmed clips or full session tapes
without triggering on plate loading or post-set walking.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import cv2
import numpy as np

DEFAULT_POSE_MODEL_PATH = Path(r"S:\ArmoryCam\models\pose\yolo11n-pose.pt")

# COCO keypoint indices
KP_NOSE = 0
KP_L_SHOULDER = 5
KP_R_SHOULDER = 6
KP_L_ELBOW = 7
KP_R_ELBOW = 8
KP_L_WRIST = 9
KP_R_WRIST = 10
KP_L_HIP = 11
KP_R_HIP = 12
KP_L_KNEE = 13
KP_R_KNEE = 14
KP_L_ANKLE = 15
KP_R_ANKLE = 16


@dataclass
class PoseFrame:
    t: float
    present: bool
    cx: float = 0.0
    cy: float = 0.0
    bw: float = 0.0
    bh: float = 0.0
    torso_ang: float = 0.0
    sh_y: float = 0.0
    hip_y: float = 0.0
    wrist_y: float = 0.0
    wrist_to_sh_y: float = 1.0
    elbow_ang: float = 150.0
    knee_ang: float = 170.0
    ankle_x: float = 0.0
    ankle_y: float = 0.0
    torso_len: float = 0.2
    hip_ankle_dy: float = 0.25


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, Any] = {}


def get_pose_model(model_path: str | Path | None = None) -> Any:
    """Lazy-load and cache the YOLO11n-Pose model from `S:\\ArmoryCam\\models\\pose`."""
    from ultralytics import YOLO  # type: ignore

    target = Path(model_path) if model_path else DEFAULT_POSE_MODEL_PATH
    key = str(target.resolve()) if target.exists() else str(target)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = YOLO("yolo11n-pose.pt")
            ckpt = Path(getattr(tmp, "ckpt_path", "yolo11n-pose.pt"))
            if ckpt.is_file() and ckpt.resolve() != target.resolve():
                import shutil

                shutil.copy2(ckpt, target)
            _MODEL_CACHE[key] = tmp
            return tmp
        m = YOLO(str(target))
        _MODEL_CACHE[key] = m
        return m


def _kp_pt(kp: np.ndarray, idx: int, min_conf: float = 0.20) -> tuple[float, float] | None:
    if kp[idx][2] >= min_conf:
        return (float(kp[idx][0]), float(kp[idx][1]))
    return None


def _mid_pt(kp: np.ndarray, i1: int, i2: int, min_conf: float = 0.20) -> tuple[float, float] | None:
    p1 = _kp_pt(kp, i1, min_conf)
    p2 = _kp_pt(kp, i2, min_conf)
    if p1 is not None and p2 is not None:
        return ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
    return p1 or p2


def _joint_angle_deg(
    a: tuple[float, float] | None,
    b: tuple[float, float] | None,
    c: tuple[float, float] | None,
) -> float | None:
    if a is None or b is None or c is None:
        return None
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1 = math.hypot(v1[0], v1[1])
    n2 = math.hypot(v2[0], v2[1])
    if n1 < 1e-3 or n2 < 1e-3:
        return None
    cos_a = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos_a))


def _prep_gray_frames(frames_t: Sequence[tuple[float, np.ndarray]]) -> list[np.ndarray]:
    """Downscale BGR frames to blurred `(180, 160)` uint8 grayscale for fast rest-frame diff."""
    out: list[np.ndarray] = []
    for _, fr in frames_t:
        g = cv2.cvtColor(cv2.resize(fr, (160, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        out.append(cv2.GaussianBlur(g, (5, 5), 0))
    return out


def _pick_primary_person(
    xyxy: np.ndarray,
    confs: np.ndarray,
    frame_w: int,
    frame_h: int,
    prev_cx: float | None,
    prev_bh: float | None,
) -> int:
    """Select the foreground lifter while suppressing mirror ghosts and edge bystanders."""
    n = len(xyxy)
    if n == 0:
        return -1
    max_h = max(float(xyxy[i][3] - xyxy[i][1]) / max(1, frame_h) for i in range(n))
    has_centered = any(
        0.22 <= (((float(xyxy[i][0]) + float(xyxy[i][2])) * 0.5) / max(1, frame_w)) <= 0.80
        and float(confs[i]) >= 0.25
        for i in range(n)
    )
    best_i = -1
    best_score = -1e9
    for i in range(n):
        x0, y0, x1, y1 = [float(v) for v in xyxy[i]]
        conf = float(confs[i])
        bw = max(1.0, x1 - x0) / max(1, frame_w)
        bh = max(1.0, y1 - y0) / max(1, frame_h)
        cx = ((x0 + x1) * 0.5) / max(1, frame_w)
        if max_h >= 0.30 and bh < max_h * 0.55 and bh < 0.28:
            continue
        score = bh * (0.65 + 0.35 * conf) + 0.15 * bw
        if max_h >= 0.30 and cx < 0.30 and bh < max_h * 0.78:
            score -= 0.45
        if (cx < 0.14 or cx > 0.88) and (has_centered or (prev_cx is not None and 0.25 <= prev_cx <= 0.78)):
            score -= 0.55
        if prev_cx is not None:
            dist = abs(cx - prev_cx)
            if dist <= 0.18:
                score += 0.28 * (1.0 - dist / 0.18)
            elif dist >= 0.28 and prev_bh is not None and bh < prev_bh * 0.68:
                score -= 0.40
        if score > best_score:
            best_score = score
            best_i = i
    if best_i < 0:
        best_i = int(np.argmax([(xyxy[i][3] - xyxy[i][1]) * confs[i] for i in range(n)]))
    return best_i


def extract_pose_frames(
    frames_t: Sequence[tuple[float, np.ndarray]],
    *,
    imgsz: int = 480,
    batch_size: int = 16,
    model_path: str | Path | None = None,
) -> list[PoseFrame]:
    """Run batched YOLO11n-Pose on `(t_sec, bgr_frame)` pairs and compute normalized kinematics."""
    if not frames_t:
        return []
    model = get_pose_model(model_path)
    imgs = [f for _, f in frames_t]
    all_res: list[Any] = []
    for i in range(0, len(imgs), max(1, batch_size)):
        chunk = imgs[i : i + batch_size]
        res = model.predict(chunk, imgsz=imgsz, conf=0.20, verbose=False)
        all_res.extend(res)

    out: list[PoseFrame] = []
    prev_cx: float | None = None
    prev_bh: float | None = None
    miss_streak = 0

    for (t_s, frm), res in zip(frames_t, all_res):
        fh, fw = frm.shape[:2]
        boxes = res.boxes
        kps_obj = res.keypoints
        if boxes is None or len(boxes) == 0 or kps_obj is None:
            miss_streak += 1
            if miss_streak > 4:
                prev_cx = None
                prev_bh = None
            out.append(PoseFrame(t=float(t_s), present=False))
            continue

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        kp_data = kps_obj.data.cpu().numpy()
        idx = _pick_primary_person(xyxy, confs, fw, fh, prev_cx, prev_bh)
        if idx < 0:
            miss_streak += 1
            out.append(PoseFrame(t=float(t_s), present=False))
            continue

        miss_streak = 0
        x0, y0, x1, y1 = [float(v) for v in xyxy[idx]]
        kp = kp_data[idx]
        cx = ((x0 + x1) * 0.5) / max(1, fw)
        cy = ((y0 + y1) * 0.5) / max(1, fh)
        bw = max(1.0, x1 - x0) / max(1, fw)
        bh = max(1.0, y1 - y0) / max(1, fh)
        prev_cx = cx
        prev_bh = bh

        sh = _mid_pt(kp, KP_L_SHOULDER, KP_R_SHOULDER)
        hp = _mid_pt(kp, KP_L_HIP, KP_R_HIP)
        wr = _mid_pt(kp, KP_L_WRIST, KP_R_WRIST)
        ak = _mid_pt(kp, KP_L_ANKLE, KP_R_ANKLE)

        torso_ang = 0.0
        torso_len = max(0.12, bh * 0.35)
        if sh is not None and hp is not None:
            dx = (sh[0] - hp[0]) / max(1, fw)
            dy = (hp[1] - sh[1]) / max(1, fh)
            torso_ang = math.degrees(math.atan2(abs(dx), max(1e-3, abs(dy))))
            torso_len = max(0.08, math.hypot(dx, dy))

        sh_y = (sh[1] / max(1, fh)) if sh is not None else (y0 / max(1, fh) + 0.18 * bh)
        hip_y = (hp[1] / max(1, fh)) if hp is not None else cy
        wrist_y = (wr[1] / max(1, fh)) if wr is not None else sh_y
        wrist_to_sh_y = (wrist_y - sh_y) / max(0.08, torso_len)
        ankle_x = (ak[0] / max(1, fw)) if ak is not None else cx
        ankle_y = (ak[1] / max(1, fh)) if ak is not None else (y1 / max(1, fh))
        hip_ankle_dy = max(0.0, ankle_y - hip_y)

        e_angs = [
            ang
            for ang in (
                _joint_angle_deg(
                    _kp_pt(kp, KP_L_SHOULDER), _kp_pt(kp, KP_L_ELBOW), _kp_pt(kp, KP_L_WRIST)
                ),
                _joint_angle_deg(
                    _kp_pt(kp, KP_R_SHOULDER), _kp_pt(kp, KP_R_ELBOW), _kp_pt(kp, KP_R_WRIST)
                ),
            )
            if ang is not None
        ]
        k_angs = [
            ang
            for ang in (
                _joint_angle_deg(
                    _kp_pt(kp, KP_L_HIP), _kp_pt(kp, KP_L_KNEE), _kp_pt(kp, KP_L_ANKLE)
                ),
                _joint_angle_deg(
                    _kp_pt(kp, KP_R_HIP), _kp_pt(kp, KP_R_KNEE), _kp_pt(kp, KP_R_ANKLE)
                ),
            )
            if ang is not None
        ]
        elbow_ang = float(sum(e_angs) / len(e_angs)) if e_angs else 150.0
        knee_ang = float(sum(k_angs) / len(k_angs)) if k_angs else 170.0

        out.append(
            PoseFrame(
                t=float(t_s),
                present=True,
                cx=cx,
                cy=cy,
                bw=bw,
                bh=bh,
                torso_ang=torso_ang,
                sh_y=sh_y,
                hip_y=hip_y,
                wrist_y=wrist_y,
                wrist_to_sh_y=wrist_to_sh_y,
                elbow_ang=elbow_ang,
                knee_ang=knee_ang,
                ankle_x=ankle_x,
                ankle_y=ankle_y,
                torso_len=torso_len,
                hip_ankle_dy=hip_ankle_dy,
            )
        )

    # Neighborhood outlier filter: scrub transient mirror-ghost / edge-bystander spikes
    n = len(out)
    scrub_idx: list[int] = []
    for i in range(n):
        if not out[i].present:
            continue
        nbrs = [
            out[j].cx
            for j in range(max(0, i - 6), min(n, i + 7))
            if j != i and out[j].present and abs(out[j].t - out[i].t) <= 2.0
        ]
        if len(nbrs) >= 3:
            med_cx = float(np.median(nbrs))
            if abs(out[i].cx - med_cx) > 0.18:
                scrub_idx.append(i)
    for idx in scrub_idx:
        out[idx] = PoseFrame(t=out[idx].t, present=False)

    # Bridge brief <= 1.8s detection dropouts
    i = 0
    while i < n:
        if out[i].present:
            i += 1
            continue
        j = i
        while j < n and not out[j].present:
            j += 1
        if i > 0 and j < n and (out[j].t - out[i - 1].t) <= 1.8 and abs(out[j].cx - out[i - 1].cx) < 0.22:
            a, b = out[i - 1], out[j]
            span = max(1e-3, b.t - a.t)
            for k in range(i, j):
                alpha = (out[k].t - a.t) / span
                out[k] = PoseFrame(
                    t=out[k].t,
                    present=True,
                    cx=a.cx + alpha * (b.cx - a.cx),
                    cy=a.cy + alpha * (b.cy - a.cy),
                    bw=a.bw + alpha * (b.bw - a.bw),
                    bh=a.bh + alpha * (b.bh - a.bh),
                    torso_ang=a.torso_ang + alpha * (b.torso_ang - a.torso_ang),
                    sh_y=a.sh_y + alpha * (b.sh_y - a.sh_y),
                    hip_y=a.hip_y + alpha * (b.hip_y - a.hip_y),
                    wrist_y=a.wrist_y + alpha * (b.wrist_y - a.wrist_y),
                    wrist_to_sh_y=a.wrist_to_sh_y + alpha * (b.wrist_to_sh_y - a.wrist_to_sh_y),
                    elbow_ang=a.elbow_ang + alpha * (b.elbow_ang - a.elbow_ang),
                    knee_ang=a.knee_ang + alpha * (b.knee_ang - a.knee_ang),
                    ankle_x=a.ankle_x + alpha * (b.ankle_x - a.ankle_x),
                    ankle_y=a.ankle_y + alpha * (b.ankle_y - a.ankle_y),
                    torso_len=a.torso_len + alpha * (b.torso_len - a.torso_len),
                    hip_ankle_dy=a.hip_ankle_dy + alpha * (b.hip_ankle_dy - a.hip_ankle_dy),
                )
        i = j
    return out


def _window_range(vals: Sequence[float]) -> float:
    if len(vals) < 2:
        return 0.0
    return float(max(vals) - min(vals))


def _compute_kinematic_signals(frames: Sequence[PoseFrame]) -> list[dict[str, float]]:
    """Compute per-frame locomotion, rep excursion, supine bench, and iso-hold signals."""
    n = len(frames)
    sigs: list[dict[str, float]] = []
    for i, pf in enumerate(frames):
        if not pf.present:
            sigs.append({
                "t": pf.t, "present": 0.0, "locomotion": 0.0, "depth_vel": 0.0,
                "work_exc": 0.0, "lying": 0.0, "iso": 0.0, "score": 0.0,
                "cx": 0.0, "bh": 0.0, "ang": 0.0, "wrist_to_sh_y": 1.0,
            })
            continue

        win = [frames[j] for j in range(max(0, i - 8), min(n, i + 9)) if frames[j].present and abs(frames[j].t - pf.t) <= 1.65]
        swin = [frames[j] for j in range(max(0, i - 5), min(n, i + 6)) if frames[j].present and abs(frames[j].t - pf.t) <= 0.90]

        cx_span = _window_range([p.cx for p in swin])
        ak_span = _window_range([p.ankle_x for p in swin])
        dt_s = max(0.4, (swin[-1].t - swin[0].t)) if len(swin) >= 2 else 1.0
        locomotion = max(cx_span, ak_span * 0.85) / dt_s

        lwin = [frames[j] for j in range(max(0, i - 8), min(n, i + 9)) if frames[j].present and abs(frames[j].t - pf.t) <= 1.5]
        # Horizontal translation across frame measures true walking; avoid using `bh` delta since squats & hinges compress `bh` vertically.
        horiz_drift = (abs(lwin[-1].cx - lwin[0].cx) / max(0.5, lwin[-1].t - lwin[0].t)) if len(lwin) >= 2 else 0.0
        depth_drift = (abs(lwin[-1].bh - lwin[0].bh) / max(0.5, lwin[-1].t - lwin[0].t)) if len(lwin) >= 2 else 0.0
        walk_vel = max(locomotion, horiz_drift * 1.1)

        exc_ang_n = _window_range([p.torso_ang for p in win]) / 26.0
        exc_sh_n = _window_range([p.sh_y for p in win]) / 0.065
        exc_hip_n = _window_range([p.hip_y for p in win]) / 0.060
        exc_wr_n = _window_range([p.wrist_y for p in win]) / 0.080
        exc_elb_n = _window_range([p.elbow_ang for p in win]) / 44.0
        exc_knee_n = _window_range([p.knee_ang for p in win]) / 40.0
        exc_bw_n = _window_range([p.bw for p in win]) / 0.16

        walk_penalty = 1.0 if walk_vel < 0.075 else max(0.10, 1.0 - (walk_vel - 0.075) * 7.5)
        work_exc = walk_penalty * max(
            exc_ang_n, exc_sh_n, exc_hip_n, 0.92 * exc_wr_n, 0.90 * exc_elb_n, 0.82 * exc_knee_n, 0.85 * exc_bw_n
        )

        sh_hip_dy = abs(pf.sh_y - pf.hip_y)
        lying = 1.0 if (pf.bh <= 0.42 and sh_hip_dy <= 0.085 and pf.hip_ankle_dy <= 0.26) else 0.0

        hwin = [frames[j] for j in range(max(0, i - 10), min(n, i + 11)) if frames[j].present and abs(frames[j].t - pf.t) <= 2.5]
        steady_pos = (
            len(hwin) >= 4
            and _window_range([p.cx for p in hwin]) < 0.050
            and _window_range([p.wrist_y for p in hwin]) < 0.065
            and _window_range([p.bh for p in hwin]) < 0.055
            and pf.bh <= 0.74
            and -0.25 <= pf.wrist_to_sh_y <= 0.74
        )
        iso = 1.0 if steady_pos else 0.0

        score = max(work_exc, 1.40 * lying)
        sigs.append({
            "t": pf.t,
            "present": 1.0,
            "locomotion": walk_vel,
            "depth_vel": depth_drift,
            "work_exc": work_exc,
            "lying": lying,
            "iso": iso,
            "score": score,
            "cx": pf.cx,
            "bh": pf.bh,
            "ang": pf.torso_ang,
            "wrist_to_sh_y": pf.wrist_to_sh_y,
        })
    return sigs


def _extract_spans(flags: Sequence[bool], frames: Sequence[PoseFrame], min_dur: float) -> list[tuple[int, int]]:
    n = len(flags)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1]:
            j += 1
        if frames[j].t - frames[i].t >= min_dur:
            spans.append((i, j))
        i = j + 1
    return spans


_REP_CHANNELS: tuple[tuple[str, float], ...] = (
    ("sh_y", 0.048),
    ("hip_y", 0.042),
    ("torso_ang", 12.5),
    ("wrist_y", 0.056),
    ("elbow_ang", 30.0),
    ("knee_ang", 24.0),
    ("bw", 0.080),
)


def _binomial_smooth(vals: np.ndarray) -> np.ndarray:
    """Zero-lag 3-point binomial filter [0.25, 0.5, 0.25] preserving crisp extremum timing."""
    if len(vals) < 3:
        return vals.copy()
    out = vals.copy()
    out[1:-1] = 0.25 * vals[:-2] + 0.50 * vals[1:-1] + 0.25 * vals[2:]
    return out


def _zigzag_extrema(
    vals: np.ndarray,
    times: np.ndarray,
    min_prom: float,
    *,
    min_half_sec: float = 0.35,
    max_half_sec: float = 4.5,
) -> list[tuple[int, float, str]]:
    """Extract alternating local extrema (`max`/`min`) whose swing exceeds `min_prom`."""
    m = len(vals)
    if m < 4:
        return []

    extrema: list[tuple[int, float, str]] = []
    # 0 = seeking initial direction, +1 = looking for high, -1 = looking for low
    mode = 0
    hi_i = lo_i = 0
    hi_v = lo_v = float(vals[0])

    for i in range(1, m):
        v = float(vals[i])
        if mode == 0:
            if v > hi_v:
                hi_v, hi_i = v, i
            if v < lo_v:
                lo_v, lo_i = v, i
            if hi_v - float(vals[0]) >= min_prom * 0.85 and hi_i > 0:
                extrema.append((0 if float(vals[0]) <= lo_v + 1e-6 else lo_i, lo_v if hi_i > lo_i else float(vals[0]), "min"))
                mode = 1
            elif float(vals[0]) - lo_v >= min_prom * 0.85 and lo_i > 0:
                extrema.append((0 if float(vals[0]) >= hi_v - 1e-6 else hi_i, hi_v if lo_i > hi_i else float(vals[0]), "max"))
                mode = -1
        elif mode == 1:
            if v >= hi_v:
                hi_v, hi_i = v, i
            elif hi_v - v >= min_prom:
                if not extrema or (times[hi_i] - times[extrema[-1][0]]) >= min_half_sec:
                    extrema.append((hi_i, hi_v, "max"))
                lo_v, lo_i = v, i
                mode = -1
        else:
            if v <= lo_v:
                lo_v, lo_i = v, i
            elif v - lo_v >= min_prom:
                if not extrema or (times[lo_i] - times[extrema[-1][0]]) >= min_half_sec:
                    extrema.append((lo_i, lo_v, "min"))
                hi_v, hi_i = v, i
                mode = 1

    # Close trailing extremum if significant
    if mode == 1 and extrema and (hi_v - extrema[-1][1]) >= min_prom * 0.85 and (times[hi_i] - times[extrema[-1][0]]) >= min_half_sec:
        extrema.append((hi_i, hi_v, "max"))
    elif mode == -1 and extrema and (extrema[-1][1] - lo_v) >= min_prom * 0.85 and (times[lo_i] - times[extrema[-1][0]]) >= min_half_sec:
        extrema.append((lo_i, lo_v, "min"))

    # Keep extrema chains whose pause between adjacent extrema doesn't exceed ~10s,
    # allowing 1 shallow/missed rep or breathing pause without amputating the second half of a set.
    if len(extrema) < 3:
        return extrema
    runs: list[list[tuple[int, float, str]]] = [[extrema[0]]]
    for ex in extrema[1:]:
        dt = float(times[ex[0]] - times[runs[-1][-1][0]])
        if dt <= max_half_sec * 2.2:
            runs[-1].append(ex)
        else:
            runs.append([ex])
    best_run = max(runs, key=lambda r: (len(r), times[r[-1][0]] - times[r[0][0]]))
    return best_run


def _detect_rep_cycles(
    frames: Sequence[PoseFrame],
    s: int,
    e: int,
    *,
    posture: str = "dynamic",
) -> dict[str, Any] | None:
    """Confirm stationary periodic rep oscillations inside `[s..e]` and isolate exact first/last rep indices."""
    seg = frames[s : e + 1]
    pres_local = [idx for idx, p in enumerate(seg) if p.present]
    if len(pres_local) < 6:
        return None

    m = len(seg)
    times = np.array([p.t for p in seg], dtype=np.float64)
    # Build interpolated channels over present points
    p_times = np.array([seg[i].t for i in pres_local], dtype=np.float64)
    chan_arrays: dict[str, np.ndarray] = {}
    for attr, _ in _REP_CHANNELS:
        raw = np.array([getattr(seg[i], attr) for i in pres_local], dtype=np.float64)
        interp = np.interp(times, p_times, raw) if len(pres_local) < m else raw
        chan_arrays[attr] = _binomial_smooth(interp)

    best: dict[str, Any] | None = None
    best_rank = -1.0

    for attr, min_prom in _REP_CHANNELS:
        # Ignore bw/sh_y noise on supine bench except wrist_y & elbow_ang
        if posture == "bench" and attr not in ("wrist_y", "elbow_ang"):
            continue
        vals = chan_arrays[attr]
        ext = _zigzag_extrema(vals, times, min_prom)
        if len(ext) < 3:
            continue

        # Trim trailing/leading fake extrema caused by standing up, releasing bar, or walking away
        trimmed = list(ext)
        if len(trimmed) >= 4:
            core_lo = trimmed[0][0]
            core_hi = trimmed[-2][0]
            core_pts = [seg[k] for k in range(core_lo, core_hi + 1) if seg[k].present]
            if len(core_pts) >= 4:
                med_cx = float(np.median([p.cx for p in core_pts]))
                p90_bh = float(np.percentile([p.bh for p in core_pts], 90))
                p10_hip = float(np.percentile([p.hip_y for p in core_pts], 10))
                p90_hip = float(np.percentile([p.hip_y for p in core_pts], 90))
                p10_sh = float(np.percentile([p.sh_y for p in core_pts], 10))
                med_kn = float(np.median([p.knee_ang for p in core_pts]))
                med_w2s = float(np.median([p.wrist_to_sh_y for p in core_pts]))
                p90_w2s = float(np.percentile([p.wrist_to_sh_y for p in core_pts], 90))
                is_seated_core = (p90_hip - p10_hip) <= 0.06 and med_kn < 145.0
                fe = seg[trimmed[-1][0]]
                if fe.present and (
                    abs(fe.cx - med_cx) > 0.11
                    or fe.bh > p90_bh + 0.09
                    or (is_seated_core and (fe.hip_y < p10_hip - 0.050 or fe.sh_y < p10_sh - 0.060 or fe.bh > p90_bh + 0.07))
                    or (posture == "bench" and (abs(fe.sh_y - fe.hip_y) > 0.11 or fe.bh > 0.44))
                    or (attr == "bw" and fe.wrist_to_sh_y > p90_w2s + 0.35)
                    or (med_w2s <= 0.30 and not is_seated_core and fe.wrist_to_sh_y > max(0.38, p90_w2s + 0.28))
                    or (med_w2s >= 0.70 and fe.wrist_to_sh_y < med_w2s - 0.30 and fe.elbow_ang < 115.0)
                ):
                    trimmed = trimmed[:-1]
                if len(trimmed) >= 4:
                    fs = seg[trimmed[0][0]]
                    if fs.present and (abs(fs.cx - med_cx) > 0.12 or fs.bh > p90_bh + 0.12):
                        trimmed = trimmed[1:]

        if len(trimmed) < 3:
            continue

        half_reps = len(trimmed) - 1
        rep_count = half_reps * 0.5
        amps = [abs(trimmed[k][1] - trimmed[k - 1][1]) for k in range(1, len(trimmed))]
        med_amp = float(np.median(amps))
        if rep_count < 1.5:
            continue

        # Check stationarity specifically over the rep window [first_ext .. last_ext]
        i0 = trimmed[0][0]
        i1 = trimmed[-1][0]
        rep_pts = [seg[k] for k in range(i0, i1 + 1) if seg[k].present]
        if len(rep_pts) < 5:
            continue
        cxs = [p.cx for p in rep_pts]
        bhs = [p.bh for p in rep_pts]
        hips = [p.hip_y for p in rep_pts]
        cx_std = float(np.std(cxs))
        cx_span = float(np.percentile(cxs, 90) - np.percentile(cxs, 10))
        med_bh = float(np.median(bhs))
        hip_span = float(np.percentile(hips, 90) - np.percentile(hips, 10))

        # Kill walking-around / plate-loading translation, far background silhouettes, and left-border bystanders
        med_cx = float(np.median(cxs))
        med_bw = float(np.median([p.bw for p in rep_pts]))
        med_kn = float(np.median([p.knee_ang for p in rep_pts]))
        med_w2s = float(np.median([p.wrist_to_sh_y for p in rep_pts]))
        if cx_std > 0.065 or cx_span > 0.15:
            continue
        if posture != "bench" and med_bh < 0.33:
            continue
        if med_cx < 0.32 and med_bw < 0.45:
            continue
        # Reject single-stroke 1.5-rep blips (plate bending / reaching up / brief adjustments)
        if rep_count < 2.0:
            continue
        # Wrist-only jitter or foreground wrist-wrap fiddling needs corroboration
        if attr == "wrist_y":
            elb_rng = _window_range([p.elbow_ang for p in rep_pts])
            sh_rng = _window_range([p.sh_y for p in rep_pts])
            tang_rng = _window_range([p.torso_ang for p in rep_pts])
            if elb_rng < 26.0 and sh_rng < 0.042 and tang_rng < 14.0:
                continue
            if med_bh >= 0.64 and med_kn >= 158.0 and med_w2s >= 0.15 and hip_span < 0.042 and elb_rng < 55.0:
                continue
        # Kill foreground camera walks while preserving stationary seated machine / standing curl
        if med_bh > 0.78:
            if not (cx_std <= 0.055 and hip_span <= 0.055 and attr in ("elbow_ang", "wrist_y", "bw") and rep_count >= 2.5):
                continue

        # Score favors true multi-rep periodic cycles & high SNR over min_prom
        norm_amp = min(3.5, med_amp / min_prom)
        dur_r = float(times[i1] - times[i0])
        rank = rep_count * norm_amp + min(2.0, dur_r / 20.0)
        # Prefer primary body/limb channels slightly over noisy bbox width
        if attr == "bw":
            rank *= 0.85

        if rank > best_rank:
            best_rank = rank
            best = {
                "channel": attr,
                "repCount": round(rep_count, 1),
                "firstRepIdx": s + i0,
                "lastRepIdx": s + i1,
                "normAmp": round(norm_amp, 2),
                "rank": round(rank, 2),
                "extremaLocal": trimmed,
            }

    return best


def _compute_equipment_rest_curve(
    frames_gray: Sequence[np.ndarray],
    frames_pose: Sequence[PoseFrame],
    first_rep_idx: int,
    last_rep_idx: int,
    station_cx: float,
) -> dict[str, Any]:
    """Measure pre-unrack <-> rack visual similarity on barbell sleeves / machine arms outside torso."""
    n = len(frames_gray)
    if n < 6 or n != len(frames_pose) or first_rep_idx >= last_rep_idx:
        return {"hasSignal": False}

    rep_pose = [frames_pose[k] for k in range(first_rep_idx, last_rep_idx + 1) if frames_pose[k].present]
    if len(rep_pose) < 4:
        return {"hasSignal": False}

    h, w = frames_gray[0].shape[:2]
    # Sample interior rep frames to find active equipment pixels outside central torso box
    step = max(1, (last_rep_idx - first_rep_idx) // 18)
    rep_imgs = [np.asarray(frames_gray[k], dtype=np.float32) for k in range(first_rep_idx, last_rep_idx + 1, step)]
    if len(rep_imgs) < 3:
        return {"hasSignal": False}
    stack = np.stack(rep_imgs, axis=0)
    t_std = np.std(stack, axis=0)

    # Mask out lifter torso/legs cylinder so bar sleeves, plates, cable & machine arms dominate
    x0_t = max(0, min(w - 1, int(round((station_cx - 0.14) * w))))
    x1_t = max(0, min(w, int(round((station_cx + 0.14) * w))))
    min_sh = min(p.sh_y for p in rep_pose)
    max_hip = max(p.hip_y for p in rep_pose)
    y0_t = max(0, min(h - 1, int(round((min_sh - 0.10) * h))))
    y1_t = max(0, min(h, int(round((max_hip + 0.12) * h))))

    torso_excl = np.ones((h, w), dtype=bool)
    torso_excl[y0_t:y1_t, x0_t:x1_t] = False

    masked_std = np.where(torso_excl, t_std, 0.0)
    thresh = float(np.percentile(masked_std[torso_excl], 78)) if np.any(torso_excl) else 0.0
    eq_mask = torso_excl & (masked_std >= max(2.0, thresh))
    if int(eq_mask.sum()) < max(60, int(0.025 * h * w)):
        # Tight close-up fallback (e.g. Fly Machine Cam) -> side bands outside central spine
        x0_s = max(0, min(w - 1, int(round((station_cx - 0.09) * w))))
        x1_s = max(0, min(w, int(round((station_cx + 0.09) * w))))
        eq_mask = np.ones((h, w), dtype=bool)
        eq_mask[:, x0_s:x1_s] = False

    if int(eq_mask.sum()) < 40:
        return {"hasSignal": False}

    # Find low-motion pre-unrack reference frame (`>= 2.0s` before Rep 1) or post-rack rest frame
    pre_cands = [
        k for k in range(max(0, first_rep_idx - 25), max(0, first_rep_idx - 3))
        if frames_pose[first_rep_idx].t - frames_pose[k].t >= 2.0
    ]
    post_cands = [
        k for k in range(min(n - 1, last_rep_idx + 4), min(n, last_rep_idx + 28))
        if frames_pose[k].t - frames_pose[last_rep_idx].t >= 2.0
    ]

    def _pick_quiet_ref(indices: list[int]) -> np.ndarray | None:
        if len(indices) < 2:
            return None
        scored: list[tuple[float, int]] = []
        for idx in indices:
            if idx <= 0:
                continue
            g0 = np.asarray(frames_gray[idx], dtype=np.float32)
            g1 = np.asarray(frames_gray[idx - 1], dtype=np.float32)
            scored.append((float(np.mean(np.abs(g0[eq_mask] - g1[eq_mask]))), idx))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0])
        top = [np.asarray(frames_gray[idx], dtype=np.float32) for _, idx in scored[: min(3, len(scored))]]
        return np.median(np.stack(top, axis=0), axis=0)

    ref_img = _pick_quiet_ref(pre_cands)
    ref_source = "pre"
    if ref_img is None:
        ref_img = _pick_quiet_ref(post_cands)
        ref_source = "post"
    if ref_img is None:
        return {"hasSignal": False}

    ref_vec = ref_img[eq_mask]
    rest_dist = np.zeros(n, dtype=np.float64)
    eq_motion = np.zeros(n, dtype=np.float64)
    prev_vec: np.ndarray | None = None
    for k in range(n):
        vec = np.asarray(frames_gray[k], dtype=np.float32)[eq_mask]
        rest_dist[k] = float(np.mean(np.abs(vec - ref_vec)))
        if prev_vec is not None:
            eq_motion[k] = float(np.mean(np.abs(vec - prev_vec)))
        prev_vec = vec

    rep_slice = rest_dist[first_rep_idx : last_rep_idx + 1]
    rep_high = float(np.percentile(rep_slice, 75))
    ref_indices = pre_cands if ref_source == "pre" else post_cands
    rest_low = float(np.percentile([rest_dist[k] for k in ref_indices], 25))
    span = rep_high - rest_low
    if span < 1.8:
        return {"hasSignal": False}

    out_of_rest = np.clip((rest_dist - rest_low) / span, 0.0, 1.5)
    return {
        "hasSignal": True,
        "refSource": ref_source,
        "outOfRest": out_of_rest,
        "eqMotion": eq_motion,
        "span": round(span, 2),
    }


def detect_lift_blocks_from_pose(
    frames: Sequence[PoseFrame],
    *,
    clip_dur_sec: float | None = None,
    pre_pad_sec: float = 1.0,
    post_pad_sec: float = 1.0,
    min_block_sec: float = 4.0,
    multi_block: bool = False,
    frames_gray: Sequence[np.ndarray] | None = None,
) -> list[dict[str, Any]]:
    """Detect clean `[unrack -> reps -> rack] +/- 1.0s` block(s) from pose + optional grayscale frames."""
    if not frames:
        return []
    t_min = frames[0].t
    t_max = clip_dur_sec if clip_dur_sec is not None else frames[-1].t
    sigs = _compute_kinematic_signals(frames)
    n = len(sigs)

    # 1. Collect candidate intervals (supine bench, stationary oscillating work, isometric hold)
    bench_flags = [bool(s["present"] > 0.5 and s["lying"] > 0.5) for s in sigs]
    bench_spans = _extract_spans(bench_flags, frames, min_dur=3.0)
    merged_bench: list[tuple[int, int]] = []
    for s, e in bench_spans:
        if not merged_bench:
            merged_bench.append((s, e))
            continue
        ps, pe = merged_bench[-1]
        cx_a = float(np.median([frames[k].cx for k in range(ps, pe + 1) if frames[k].present]))
        cx_b = float(np.median([frames[k].cx for k in range(s, e + 1) if frames[k].present]))
        if frames[s].t - frames[pe].t <= 4.0 and abs(cx_a - cx_b) < 0.14:
            merged_bench[-1] = (ps, max(pe, e))
        else:
            merged_bench.append((s, e))

    valid_bench: list[tuple[int, int, dict[str, Any]]] = []
    for s, e in merged_bench:
        dur_b = frames[e].t - frames[s].t
        pres_k = [k for k in range(s, e + 1) if frames[k].present]
        if dur_b < 5.5 or not pres_k:
            continue
        lying_frac = sum(1 for k in pres_k if sigs[k]["lying"] > 0.5) / len(pres_k)
        med_sh_hip = float(np.median([abs(frames[k].sh_y - frames[k].hip_y) for k in pres_k]))
        med_bh = float(np.median([frames[k].bh for k in pres_k]))
        med_bw = float(np.median([frames[k].bw for k in pres_k]))
        if lying_frac < 0.60 or med_sh_hip > 0.08 or med_bh > 0.39 or med_bw < 0.28:
            continue
        cyc = _detect_rep_cycles(frames, s, e, posture="bench")
        if cyc is not None and cyc["repCount"] >= 2.0:
            valid_bench.append((s, e, cyc))

    # Strict isometric hold gate: active chest/elbow brace, steady center, never edge bystander
    iso_flags = [bool(s["present"] > 0.5 and s["iso"] > 0.5) for s in sigs]
    iso_raw = _extract_spans(iso_flags, frames, min_dur=14.0 if not multi_block else 18.0)
    iso_spans: list[tuple[int, int]] = []
    for s, e in iso_raw:
        dur_iso = frames[e].t - frames[s].t
        if dur_iso > (90.0 if not multi_block else 65.0):
            continue
        pres_k = [frames[k] for k in range(s, e + 1) if frames[k].present]
        if len(pres_k) < 8:
            continue
        med_cx = float(np.median([p.cx for p in pres_k]))
        cx_std = float(np.std([p.cx for p in pres_k]))
        med_bh = float(np.median([p.bh for p in pres_k]))
        wr_span = _window_range([p.wrist_y for p in pres_k])
        med_w2s = float(np.median([p.wrist_to_sh_y for p in pres_k]))
        med_elb = float(np.median([p.elbow_ang for p in pres_k]))
        if (
            0.32 <= med_cx <= 0.78
            and cx_std <= 0.025
            and med_bh <= 0.72
            and wr_span <= 0.072
            and -0.25 <= med_w2s <= 0.72
            and med_elb <= 145.0
        ):
            iso_spans.append((s, e))

    # Dynamic active stationary intervals (cut across walking breaks, merge brief/slow rep pauses at same station)
    core_flags = [
        bool(s["present"] > 0.5 and s["work_exc"] >= 0.85 and s["locomotion"] < 0.095)
        for s in sigs
    ]
    dyn_spans = _extract_spans(core_flags, frames, min_dur=2.5)
    merged_dyn: list[tuple[int, int]] = []
    for s, e in dyn_spans:
        if not merged_dyn:
            merged_dyn.append((s, e))
            continue
        ps, pe = merged_dyn[-1]
        gap = frames[s].t - frames[pe].t
        cx_a = float(np.median([frames[k].cx for k in range(ps, pe + 1) if frames[k].present]))
        cx_b = float(np.median([frames[k].cx for k in range(s, e + 1) if frames[k].present]))
        gap_pts = [frames[k].cx for k in range(pe, s + 1) if frames[k].present]
        gap_cx_span = (max(gap_pts) - min(gap_pts)) if len(gap_pts) >= 2 else 0.0
        max_gap_loc = max((sigs[k]["locomotion"] for k in range(pe, s + 1)), default=0.0)
        same_station = abs(cx_a - cx_b) < 0.11 and gap_cx_span < 0.14
        tight_pause = gap <= 12.0 and abs(cx_a - cx_b) <= 0.085 and gap_cx_span <= 0.11 and max_gap_loc <= 0.065
        if (gap <= (5.5 if multi_block else 4.8) and same_station) or tight_pause:
            merged_dyn[-1] = (ps, max(pe, e))
        else:
            merged_dyn.append((s, e))

    # Split any wide span where station center jumps mid-window, then verify periodic rep cycles!
    validated_dyn: list[tuple[int, int, dict[str, Any]]] = []
    for s, e in merged_dyn:
        cyc = _detect_rep_cycles(frames, s, e, posture="dynamic")
        if cyc is not None:
            validated_dyn.append((s, e, cyc))

    def _overlap_frac(a: tuple[int, int], b: tuple[int, int]) -> float:
        t0 = max(frames[a[0]].t, frames[b[0]].t)
        t1 = min(frames[a[1]].t, frames[b[1]].t)
        if t1 <= t0:
            return 0.0
        dur_a = max(0.5, frames[a[1]].t - frames[a[0]].t)
        return (t1 - t0) / dur_a

    # Entry: (span_s, span_e, posture, cyc_info)
    candidates: list[tuple[int, int, str, dict[str, Any] | None]] = []
    if multi_block:
        for s, e, cyc in valid_bench:
            candidates.append((s, e, "bench", cyc))
        for s, e, cyc in validated_dyn:
            if not any(_overlap_frac((s, e), (bs, be)) > 0.35 for bs, be, _ in valid_bench):
                candidates.append((s, e, "dynamic", cyc))
        for s, e in iso_spans:
            if not any(_overlap_frac((s, e), (cs, ce)) > 0.35 for cs, ce, _, _ in candidates):
                candidates.append((s, e, "isometric", None))
    elif valid_bench:
        s, e, cyc = max(valid_bench, key=lambda item: (item[2]["rank"], frames[item[1]].t - frames[item[0]].t))
        candidates.append((s, e, "bench", cyc))
    else:
        best_dyn_dur = max((frames[cyc["lastRepIdx"]].t - frames[cyc["firstRepIdx"]].t for _, _, cyc in validated_dyn), default=0.0)
        best_iso_dur = max((frames[e].t - frames[s].t for s, e in iso_spans), default=0.0)
        if iso_spans and (not validated_dyn or best_iso_dur >= max(16.0, best_dyn_dur * 1.4)):
            s, e = max(iso_spans, key=lambda sp: frames[sp[1]].t - frames[sp[0]].t)
            candidates.append((s, e, "isometric", None))
        elif validated_dyn:
            # Prefer highest rep-cycle rank on single clip
            s, e, cyc = max(validated_dyn, key=lambda item: (item[2]["rank"], frames[item[1]].t - frames[item[0]].t))
            candidates.append((s, e, "dynamic", cyc))

    if not candidates:
        return []

    blocks: list[dict[str, Any]] = []
    for sp_s, sp_e, posture, cyc in candidates:
        if cyc is not None:
            cs = int(cyc["firstRepIdx"])
            ce = int(cyc["lastRepIdx"])
            dom_ch = str(cyc["channel"])
            rep_count = float(cyc["repCount"])
        else:
            cs, ce = sp_s, sp_e
            dom_ch = "isometric"
            rep_count = 0.0

        core_frames = [frames[k] for k in range(cs, ce + 1) if frames[k].present]
        if not core_frames:
            continue

        # 2. Build Working Posture Envelope from interior rep frames
        station_cx = float(np.median([p.cx for p in core_frames]))
        station_bh_med = float(np.median([p.bh for p in core_frames]))
        station_bh_p90 = float(np.percentile([p.bh for p in core_frames], 90))
        hip_min = float(np.percentile([p.hip_y for p in core_frames], 10))
        hip_max = float(np.percentile([p.hip_y for p in core_frames], 90))
        hip_span = hip_max - hip_min
        sh_min = float(np.percentile([p.sh_y for p in core_frames], 10))
        med_kn = float(np.median([p.knee_ang for p in core_frames]))
        w2s_med = float(np.median([p.wrist_to_sh_y for p in core_frames]))
        w2s_max = float(np.percentile([p.wrist_to_sh_y for p in core_frames], 90))

        is_seated = (
            posture == "dynamic"
            and (
                (hip_span <= 0.055 and med_kn < 145.0)
                or (dom_ch in ("elbow_ang", "wrist_y", "bw") and hip_span <= 0.048)
            )
        )
        med_tang = float(np.median([p.torso_ang for p in core_frames]))
        is_bent_iso_hinge = (posture == "dynamic") and (med_tang >= 38.0) and (hip_span <= 0.055)
        bar_high = (posture == "dynamic") and (not is_seated) and (not is_bent_iso_hinge) and (w2s_med <= 0.30)

        def _is_posture_exit(k_idx: int) -> bool:
            fk = frames[k_idx]
            if not fk.present:
                return True
            if abs(fk.cx - station_cx) > 0.11:
                return True
            if posture == "bench":
                return abs(fk.sh_y - fk.hip_y) > 0.115 or fk.bh > 0.44
            if posture == "isometric":
                return fk.bh > station_bh_p90 + 0.07 or sigs[k_idx]["locomotion"] > 0.08
            if is_bent_iso_hinge:
                return fk.torso_ang < 28.0 or fk.bh > station_bh_p90 + 0.075
            if is_seated:
                if fk.hip_y < hip_min - 0.055 or fk.sh_y < sh_min - 0.065 or fk.bh > station_bh_p90 + 0.08:
                    return True
                if dom_ch == "bw" and fk.wrist_to_sh_y > w2s_max + 0.35:
                    return True
                return False
            if bar_high:
                return fk.wrist_to_sh_y > max(0.38, w2s_max + 0.25) or fk.bh > station_bh_p90 + 0.07
            # Standing barbell/Smith/DB (Deadlift, Smith RDL, DB Curl): catch releasing bar / walking forward
            if fk.bh > station_bh_p90 + 0.075:
                return True
            if w2s_med >= 0.70 and fk.wrist_to_sh_y < w2s_med - 0.32 and fk.elbow_ang < 115.0:
                return True
            return False

        # If lifter holds a bent-over rear-delt fly posture with active stationary work beyond dominant-channel stop,
        # extend `ce` forward to the end of that held bent posture (before sitting upright).
        if is_bent_iso_hinge:
            k_ext = ce + 1
            while k_ext < n and not _is_posture_exit(k_ext):
                if frames[k_ext].torso_ang >= 42.0 and sigs[k_ext]["locomotion"] < 0.09:
                    ce = k_ext
                    k_ext += 1
                else:
                    break

        # 3. Equipment / barbell rest-frame symmetry curve (optional grayscale)
        eq_info = (
            _compute_equipment_rest_curve(frames_gray, frames, cs, ce, station_cx)
            if frames_gray is not None
            else {"hasSignal": False}
        )
        has_eq = bool(eq_info.get("hasSignal"))
        eq_out = eq_info.get("outOfRest")

        # 4. Snap IN boundary (walk backward from first_rep_idx to unrack / grip)
        in_idx = cs
        if posture == "bench":
            k = cs - 1
            while k >= 0 and (frames[cs].t - frames[k].t) <= 3.5:
                fk = frames[k]
                if not fk.present or abs(fk.cx - station_cx) > 0.15:
                    break
                if abs(fk.sh_y - fk.hip_y) > 0.14 or fk.bh > 0.48:
                    break
                if has_eq and eq_out is not None and float(eq_out[k]) < 0.28 and (frames[cs].t - fk.t) >= 0.8:
                    in_idx = k
                    break
                in_idx = k
                k -= 1
        elif posture == "isometric":
            k = cs - 1
            while k >= 0 and (frames[cs].t - frames[k].t) <= 3.0:
                fk = frames[k]
                if fk.present and abs(fk.cx - station_cx) < 0.08 and sigs[k]["locomotion"] < 0.055 and fk.wrist_to_sh_y <= 0.72:
                    in_idx = k
                    k -= 1
                else:
                    break
        elif bar_high:
            # Squat / standing OHP unrack: walk back while hands/shoulders on bar, stop once equipment rests in hooks
            k = cs - 1
            while k >= 0 and (frames[cs].t - frames[k].t) <= 9.0:
                fk = frames[k]
                if not fk.present or abs(fk.cx - station_cx) > 0.13 or fk.bh > station_bh_p90 + 0.12:
                    break
                if fk.wrist_to_sh_y > 0.36:
                    break
                if has_eq and eq_out is not None and float(eq_out[k]) <= 0.25 and (frames[cs].t - fk.t) >= 1.5:
                    break
                in_idx = k
                k -= 1
        else:
            # Seated cable/machine, Deadlift, Smith RDL, DB Curl: tight <= 2.5s unrack/setup lookback
            k = cs - 1
            max_back = 2.5 if not is_seated else 2.0
            while k >= 0 and (frames[cs].t - frames[k].t) <= max_back:
                if _is_posture_exit(k):
                    break
                if has_eq and eq_out is not None and float(eq_out[k]) <= 0.25 and (frames[cs].t - frames[k].t) >= 0.8:
                    break
                if sigs[k]["locomotion"] >= 0.085:
                    break
                in_idx = k
                k -= 1

        # 5. Snap OUT boundary (walk forward from last_rep_idx to rack/release, HARD STOP before walk-away/stand-up)
        out_idx = ce
        posture_exit_t: float | None = None
        if posture == "bench":
            k = ce + 1
            while k < n and (frames[k].t - frames[ce].t) <= 3.0:
                if _is_posture_exit(k):
                    posture_exit_t = frames[k].t
                    break
                out_idx = k
                fk = frames[k]
                if has_eq and eq_out is not None and float(eq_out[k]) <= 0.30 and (fk.t - frames[ce].t) >= 0.4:
                    break
                if fk.wrist_to_sh_y <= -0.32 and (fk.t - frames[ce].t) >= 0.6:
                    break
                k += 1
        elif posture == "isometric":
            k = ce + 1
            while k < n and (frames[k].t - frames[ce].t) <= 2.5:
                if _is_posture_exit(k):
                    posture_exit_t = frames[k].t
                    break
                out_idx = k
                k += 1
        elif is_seated:
            # Seated lifts end cleanly right as last rep returns (before rising off seat or dropping handles)
            k = ce + 1
            while k < n and (frames[k].t - frames[ce].t) <= 1.2:
                if _is_posture_exit(k):
                    posture_exit_t = frames[k].t
                    break
                out_idx = k
                if has_eq and eq_out is not None and float(eq_out[k]) <= 0.30:
                    break
                k += 1
        else:
            # Squat / Deadlift / Smith RDL / DB Curl: track bar return into hooks or floor <= 3.5s
            k = ce + 1
            while k < n and (frames[k].t - frames[ce].t) <= 3.5:
                if _is_posture_exit(k):
                    posture_exit_t = frames[k].t
                    break
                if sigs[k]["locomotion"] >= 0.11 and not bar_high:
                    posture_exit_t = frames[k].t
                    break
                out_idx = k
                if has_eq and eq_out is not None and float(eq_out[k]) <= 0.32 and (frames[k].t - frames[ce].t) >= 0.4:
                    break
                k += 1

        # If loop terminated without hitting exit yet, peek up to 2.0s ahead to enforce hard exit clamp
        if posture_exit_t is None:
            k_peek = out_idx + 1
            while k_peek < n and (frames[k_peek].t - frames[out_idx].t) <= 2.0:
                if _is_posture_exit(k_peek):
                    posture_exit_t = frames[k_peek].t
                    break
                k_peek += 1

        t_unrack = frames[in_idx].t
        t_rack = frames[out_idx].t
        start_s = max(t_min, round(t_unrack - pre_pad_sec, 2))
        end_s = min(t_max, round(t_rack + post_pad_sec, 2))
        if posture_exit_t is not None:
            # Never show lifter already stood up / turned away / across the gym at OUT
            end_s = min(end_s, round(max(t_rack + 0.1, posture_exit_t - 0.15), 2))
        if end_s - start_s < min_block_sec:
            continue

        peak_score = max(sigs[k]["score"] for k in range(cs, ce + 1))
        blocks.append({
            "startSec": start_s,
            "endSec": end_s,
            "durationSec": round(end_s - start_s, 2),
            "unrackSec": round(t_unrack, 2),
            "rackSec": round(t_rack, 2),
            "coreStartSec": round(frames[cs].t, 2),
            "coreEndSec": round(frames[ce].t, 2),
            "posture": posture,
            "channel": dom_ch,
            "repCount": rep_count,
            "eqSignal": has_eq,
            "peakScore": round(peak_score, 2),
            "stationCx": round(station_cx, 3),
        })

    return blocks


def detect_lift_bounds_in_video(
    video_path: str | Path,
    *,
    sample_hz: float = 2.5,
    imgsz: int = 480,
    pre_pad_sec: float = 1.0,
    post_pad_sec: float = 1.0,
    model_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Detect `[unrack -> reps -> rack] +/- 1.0s` trim bounds inside an MP4 clip."""
    p = Path(video_path)
    if not p.is_file():
        return None
    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        return None
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        nfr = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = (nfr / fps) if (fps > 0 and nfr > 0) else 0.0
        step = max(1, int(round(fps / max(0.5, sample_hz))))
        frames_t: list[tuple[float, np.ndarray]] = []
        fi = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if fi % step == 0:
                ok2, fr = cap.retrieve()
                if ok2 and fr is not None:
                    frames_t.append((fi / fps, fr))
            fi += 1
        if dur <= 0.0 and fi > 0:
            dur = fi / fps
    finally:
        cap.release()

    if not frames_t:
        return None
    pose_frames = extract_pose_frames(frames_t, imgsz=imgsz, model_path=model_path)
    grays = _prep_gray_frames(frames_t)
    blocks = detect_lift_blocks_from_pose(
        pose_frames,
        clip_dur_sec=dur,
        pre_pad_sec=pre_pad_sec,
        post_pad_sec=post_pad_sec,
        multi_block=False,
        frames_gray=grays,
    )
    if not blocks:
        return None
    best = max(blocks, key=lambda b: b["durationSec"])
    best["clipDurSec"] = round(dur, 2)
    return best


def decode_tape_window_frames(
    segs: Sequence[Any],
    cam_cfg: Any,
    start_ms: int,
    end_ms: int,
    *,
    sample_hz: float = 2.0,
    use_defish: bool = False,
    transform_fn: Callable[..., np.ndarray] | None = None,
) -> list[tuple[int, np.ndarray]]:
    """Decode `(ts_ms, transformed_bgr)` frames across MPEG-TS segments at `sample_hz`."""
    want_segs = [s for s in segs if int(s.end_ms) >= start_ms and int(s.start_ms) <= end_ms]
    if not want_segs:
        return []
    step_ms = int(round(1000.0 / max(0.5, sample_hz)))
    frames_out: list[tuple[int, np.ndarray]] = []
    last_taken_ms = -10**15

    for seg in want_segs:
        cap = cv2.VideoCapture(str(seg.path))
        if not cap.isOpened():
            continue
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
            if fps <= 1.0 or fps > 120.0:
                fps = 25.0
            seg_dur_ms = max(500, int(seg.end_ms) - int(seg.start_ms))
            fi = 0
            while True:
                ok = cap.grab()
                if not ok:
                    break
                cur_ms = int(seg.start_ms) + int(round((fi / fps) * 1000.0))
                if cur_ms > end_ms or cur_ms > int(seg.end_ms) + 300:
                    break
                if cur_ms >= start_ms and (cur_ms - last_taken_ms) >= step_ms - 40:
                    ok2, fr = cap.retrieve()
                    if ok2 and fr is not None:
                        if transform_fn is not None and cam_cfg is not None:
                            fr = transform_fn(
                                fr,
                                cam_cfg.crop,
                                cam_cfg.rotate,
                                apply_crop=True,
                                tilt=cam_cfg.tilt,
                                defish=bool(use_defish and cam_cfg.defish),
                            )
                        frames_out.append((cur_ms, fr))
                        last_taken_ms = cur_ms
                fi += 1
                if fi > int(fps * (seg_dur_ms / 1000.0 + 2.0)):
                    break
        finally:
            cap.release()
    return frames_out


def _merge_motion_gap_coverage(
    service: Any,
    canon: str,
    session_id: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Merge workout tape segments/samples with same-day `motion_YYYY-MM-DD` fallback across tape restart gaps."""
    segs = list(service._list_camera_segments(canon, session_id))
    samples = list(service.session_samples(session_id))
    if not segs:
        return segs, samples

    min_ms = int(segs[0].start_ms)
    max_ms = max(int(s.end_ms) for s in segs)
    day_str = datetime.fromtimestamp(min_ms / 1000.0).strftime("%Y-%m-%d")
    motion_sid = f"motion_{day_str}"
    if motion_sid == session_id:
        return segs, samples

    m_segs = [
        s
        for s in service._list_camera_segments(canon, motion_sid)
        if int(s.end_ms) >= min_ms - 5000 and int(s.start_ms) <= max_ms + 5000
    ]
    if m_segs:
        existing_starts = [int(s.start_ms) for s in segs]
        for ms in m_segs:
            if not any(abs(int(ms.start_ms) - es) <= 1500 for es in existing_starts):
                segs.append(ms)
        segs.sort(key=lambda s: int(s.start_ms))

    m_samples = [
        s
        for s in service.session_samples(motion_sid)
        if int(s.get("endMs", 0)) >= min_ms - 5000 and int(s.get("startMs", 0)) <= max_ms + 5000
    ]
    if m_samples:
        cam_l = canon.strip().lower()
        existing_sample_ts = {
            (str(s.get("camera", "")).strip().lower(), int(s.get("startMs", 0)) // 1000)
            for s in samples
        }
        for s in m_samples:
            sc = str(s.get("camera", "")).strip().lower()
            if sc != cam_l:
                continue
            key = (sc, int(s.get("startMs", 0)) // 1000)
            if key not in existing_sample_ts:
                samples.append(s)
        samples.sort(key=lambda s: int(s.get("startMs", 0)))
    return segs, samples


def detect_set_blocks_on_tape(
    service: Any,
    session_id: str,
    camera_name: str,
    *,
    motion_threshold: float = 1.6,
    scan_hz: float = 2.0,
    imgsz: int = 320,
    pre_pad_sec: float = 1.0,
    post_pad_sec: float = 1.0,
    transform_fn: Callable[..., np.ndarray] | None = None,
    model_path: str | Path | None = None,
) -> dict[str, Any]:
    """Two-pass tape scanner: 2.0 Hz/320px periodic rep discovery -> 2.5 Hz/480px +/-1s boundary snap."""
    t0 = time.perf_counter()
    from armory_cam_server import apply_frame_transform, cluster_motion_bursts

    tfn = transform_fn or apply_frame_transform
    cam_cfg = service._find_camera(camera_name)
    canon = cam_cfg.name if cam_cfg else camera_name
    segs, samples = _merge_motion_gap_coverage(service, canon, session_id)
    if not segs or not samples:
        return {"ok": True, "camera": canon, "sessionId": session_id, "blocks": [], "elapsedSec": 0.0}

    candidates = cluster_motion_bursts(
        samples,
        threshold=motion_threshold,
        min_dur_ms=5000,
        merge_gap_ms=4500,
        pad_before_ms=3000,
        pad_after_ms=2500,
        camera=canon,
        resolve_overlap=False,
    )
    merged_cand: list[tuple[int, int]] = []
    for b in sorted(candidates, key=lambda x: int(x["startMs"])):
        s_ms, e_ms = int(b["startMs"]), int(b["endMs"])
        if not merged_cand or s_ms > merged_cand[-1][1] + 2500:
            merged_cand.append((s_ms, e_ms))
        else:
            merged_cand[-1] = (merged_cand[-1][0], max(merged_cand[-1][1], e_ms))

    # Pass 1: Coarse rep-cycle discovery across candidate motion windows (2.0 Hz / 320px)
    coarse_blocks: list[tuple[int, int, str, float]] = []
    total_pose_frames = 0
    for c_start_ms, c_end_ms in merged_cand:
        decoded = decode_tape_window_frames(
            segs,
            cam_cfg,
            c_start_ms,
            c_end_ms,
            sample_hz=scan_hz,
            use_defish=False,
            transform_fn=tfn,
        )
        if len(decoded) < 6:
            continue
        t_base_ms = decoded[0][0]
        frames_t = [((ms - t_base_ms) / 1000.0, fr) for ms, fr in decoded]
        total_pose_frames += len(frames_t)
        pose_frames = extract_pose_frames(frames_t, imgsz=imgsz, model_path=model_path)
        grays = _prep_gray_frames(frames_t)
        blocks = detect_lift_blocks_from_pose(
            pose_frames,
            clip_dur_sec=(decoded[-1][0] - t_base_ms) / 1000.0 + 0.5,
            pre_pad_sec=pre_pad_sec,
            post_pad_sec=post_pad_sec,
            min_block_sec=5.0,
            multi_block=True,
            frames_gray=grays,
        )
        for blk in blocks:
            b_start_ms = t_base_ms + int(round(blk["startSec"] * 1000.0))
            b_end_ms = t_base_ms + int(round(blk["endSec"] * 1000.0))
            coarse_blocks.append((b_start_ms, b_end_ms, str(blk["posture"]), float(blk["peakScore"])))

    # Merge coarse overlaps (including short breathing pauses <= 6s) before Pass 2 fine refinement
    coarse_blocks.sort(key=lambda x: x[0])
    merged_coarse: list[tuple[int, int, str, float]] = []
    for s_ms, e_ms, pos, sc in coarse_blocks:
        if not merged_coarse or s_ms > merged_coarse[-1][1] + 6000:
            merged_coarse.append((s_ms, e_ms, pos, sc))
        else:
            ps, pe, ppos, psc = merged_coarse[-1]
            merged_coarse[-1] = (ps, max(pe, e_ms), ppos, max(psc, sc))

    # Pass 2: 2.5 Hz / 480px fine confirmation + tight unrack-to-rack +/- 1.0s snapping
    all_blocks: list[dict[str, Any]] = []
    for c_s, c_e, _c_pos, _c_sc in merged_coarse:
        w0 = max(int(segs[0].start_ms), c_s - 10000)
        w1 = min(int(segs[-1].end_ms), c_e + 8000)
        decoded = decode_tape_window_frames(
            segs,
            cam_cfg,
            w0,
            w1,
            sample_hz=2.5,
            use_defish=False,
            transform_fn=tfn,
        )
        if len(decoded) < 8:
            continue
        t_base_ms = decoded[0][0]
        frames_t = [((ms - t_base_ms) / 1000.0, fr) for ms, fr in decoded]
        total_pose_frames += len(frames_t)
        pose_frames = extract_pose_frames(frames_t, imgsz=480, model_path=model_path)
        grays = _prep_gray_frames(frames_t)
        fine = detect_lift_blocks_from_pose(
            pose_frames,
            clip_dur_sec=(decoded[-1][0] - t_base_ms) / 1000.0 + 0.5,
            pre_pad_sec=pre_pad_sec,
            post_pad_sec=post_pad_sec,
            min_block_sec=7.0,
            multi_block=False,
            frames_gray=grays,
        )
        if not fine:
            continue
        best = fine[0]
        b_start_ms = t_base_ms + int(round(best["startSec"] * 1000.0))
        b_end_ms = t_base_ms + int(round(best["endSec"] * 1000.0))
        all_blocks.append({
            "camera": canon,
            "startMs": b_start_ms,
            "endMs": b_end_ms,
            "durationSec": max(1, int(round((b_end_ms - b_start_ms) / 1000.0))),
            "posture": best["posture"],
            "channel": best.get("channel", ""),
            "repCount": best.get("repCount", 0.0),
            "peakScore": best["peakScore"],
        })

    all_blocks.sort(key=lambda x: x["startMs"])
    dedup_blocks: list[dict[str, Any]] = []
    for blk in all_blocks:
        if not dedup_blocks or blk["startMs"] > dedup_blocks[-1]["endMs"] + 2000:
            dedup_blocks.append(blk)
        else:
            prev = dedup_blocks[-1]
            prev["endMs"] = max(prev["endMs"], blk["endMs"])
            prev["durationSec"] = max(1, int(round((prev["endMs"] - prev["startMs"]) / 1000.0)))
            prev["peakScore"] = max(prev["peakScore"], blk["peakScore"])

    return {
        "ok": True,
        "camera": canon,
        "sessionId": session_id,
        "candidateWindows": len(merged_cand),
        "poseFrames": total_pose_frames,
        "blocks": dedup_blocks,
        "elapsedSec": round(time.perf_counter() - t0, 2),
    }
