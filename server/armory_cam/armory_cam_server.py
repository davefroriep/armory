"""Armory Cam — OptiPlex LAN DVR, Motion-Burst Detector & Clip Server.

Listens on port 8766 (default) and serves the exact REST + HTTP 206 byte-range
contract consumed by Armory Fit (`src/lifts/gymCam.ts`), plus a local web
dashboard at `/` for configuring Wyze/Thingino or NVR RTSP camera URLs.

Configuration & media live in `ARMORY_CAM_ROOT` (default `S:\\ArmoryCam`),
outside the git workspace so RTSP credentials are never committed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import cv2
from flask import Flask, Response, jsonify, request
import numpy as np
from werkzeug.serving import make_server

DEFAULT_PORT = 8766
DEFAULT_RETENTION_DAYS = 14
DEFAULT_PRE_ROLL_SEC = 120
DEFAULT_MOTION_THRESHOLD = 2.2
RING_KEEP_MIN_SEC = 300
SEG_NAME_RE = re.compile(r"^seg_(\d{8})_(\d{6})\.ts$")


def default_root_dir() -> Path:
    env = os.environ.get("ARMORY_CAM_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(r"S:\ArmoryCam")


def resolve_tool_bin(tool_name: str, env_key: str) -> str:
    """Resolve `ffmpeg` / `ffprobe` binary from env, local portable Tools, or PATH."""
    env_val = os.environ.get(env_key, "").strip()
    if env_val and Path(env_val).exists():
        return env_val
    exe = f"{tool_name}.exe" if os.name == "nt" else tool_name
    candidates = [
        Path(r"C:\Users\davef\Tools\ffmpeg\bin") / exe,
        Path(os.path.expanduser("~")) / "Tools" / "ffmpeg" / "bin" / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(tool_name)
    return found or exe


def parse_segment_start_ms(file_name: str) -> int | None:
    """Parse local wall-clock epoch ms from FFmpeg `-strftime 1` name `seg_YYYYMMDD_HHMMSS.ts`."""
    m = SEG_NAME_RE.match(file_name)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def safe_slug(raw: str, fallback: str = "clip") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip()).strip("._-")
    return (cleaned[:48] or fallback)


def compute_motion_energy_from_frames(
    frames: list[np.ndarray],
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> float:
    """Compute lightweight motion score (0..100) across sampled BGR frames inside normalized ROI."""
    if len(frames) < 2:
        return 0.0
    x0, y0, x1, y1 = roi
    x0 = max(0.0, min(0.95, float(x0)))
    y0 = max(0.0, min(0.95, float(y0)))
    x1 = max(x0 + 0.05, min(1.0, float(x1)))
    y1 = max(y0 + 0.05, min(1.0, float(y1)))

    prep: list[np.ndarray] = []
    for frm in frames:
        small = cv2.resize(frm, (160, 90), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        cx0, cx1 = int(round(x0 * w)), int(round(x1 * w))
        cy0, cy1 = int(round(y0 * h)), int(round(y1 * h))
        crop = small[max(0, cy0) : max(cy0 + 2, cy1), max(0, cx0) : max(cx0 + 2, cx1)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        prep.append(cv2.GaussianBlur(gray, (5, 5), 0))

    best = 0.0
    for i in range(len(prep) - 1):
        diff = cv2.absdiff(prep[i], prep[i + 1])
        mean_diff = float(np.mean(diff))
        active_pct = float(np.mean(diff > 14)) * 100.0
        score = round(0.6 * mean_diff + 0.25 * active_pct, 3)
        if score > best:
            best = score
    return min(100.0, best)


def analyze_ts_segment(
    seg_path: Path,
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    fallback_dur_ms: int = 2000,
) -> tuple[int, float]:
    """Read sampled frames from an MPEG-TS segment to get `(duration_ms, motion_energy)`."""
    cap = cv2.VideoCapture(str(seg_path))
    if not cap.isOpened():
        return fallback_dur_ms, 0.0
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not np.isfinite(fps) or fps <= 1.0 or fps > 120.0:
            fps = 20.0

        frames: list[np.ndarray] = []
        if total >= 4:
            idxs = sorted({0, total // 3, (2 * total) // 3, total - 1})
            for idx in idxs:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frm = cap.read()
                if ok and frm is not None:
                    frames.append(frm)
        else:
            while len(frames) < 4:
                ok, frm = cap.read()
                if not ok or frm is None:
                    break
                frames.append(frm)

        dur_ms = int(round((total / fps) * 1000)) if total >= 2 else fallback_dur_ms
        if dur_ms < 400 or dur_ms > 20_000:
            dur_ms = fallback_dur_ms
        score = compute_motion_energy_from_frames(frames, roi=roi)
        return dur_ms, score
    finally:
        cap.release()


def encode_thumb_data_url(frame: np.ndarray, max_w: int = 200, quality: int = 65) -> str:
    if frame is None or frame.size == 0:
        return ""
    h, w = frame.shape[:2]
    tw = min(max_w, max(1, w))
    th = max(1, int(round(h * (tw / max(1, w)))))
    resized = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def extract_video_thumb(video_path: Path) -> str:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return ""
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(total // 2, 30))
        ok, frm = cap.read()
        if not ok or frm is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frm = cap.read()
        return encode_thumb_data_url(frm) if ok and frm is not None else ""
    finally:
        cap.release()


def cluster_motion_bursts(
    samples: list[dict[str, Any]],
    threshold: float = DEFAULT_MOTION_THRESHOLD,
    min_dur_ms: int = 4000,
    merge_gap_ms: int = 4000,
    pad_before_ms: int = 3000,
    pad_after_ms: int = 2000,
) -> list[dict[str, Any]]:
    """Group active motion samples into padded bursts and resolve Rack-vs-Smith collisions."""
    by_cam: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        if float(s.get("energy", 0.0)) >= threshold:
            by_cam.setdefault(str(s.get("camera", "Rack Cam")), []).append(s)

    cam_bursts: list[dict[str, Any]] = []
    for cam, items in by_cam.items():
        ordered = sorted(items, key=lambda x: int(x["startMs"]))
        groups: list[list[dict[str, Any]]] = []
        for it in ordered:
            if not groups:
                groups.append([it])
                continue
            prev_end = int(groups[-1][-1]["endMs"])
            if int(it["startMs"]) - prev_end <= merge_gap_ms:
                groups[-1].append(it)
            else:
                groups.append([it])

        for grp in groups:
            raw_start = int(grp[0]["startMs"])
            raw_end = int(grp[-1]["endMs"])
            if raw_end - raw_start < min_dur_ms:
                continue
            energies = [float(x["energy"]) for x in grp]
            mean_e = sum(energies) / max(1, len(energies))
            peak_e = max(energies)
            b_start = max(0, raw_start - pad_before_ms)
            b_end = raw_end + pad_after_ms
            cam_bursts.append(
                {
                    "id": f"{safe_slug(cam)}_{b_start}",
                    "camera": cam,
                    "startMs": b_start,
                    "endMs": b_end,
                    "rawStartMs": raw_start,
                    "rawEndMs": raw_end,
                    "durationSec": max(1, int(round((b_end - b_start) / 1000.0))),
                    "peakEnergy": round(peak_e, 2),
                    "meanEnergy": round(mean_e, 2),
                    "note": f"Auto-picked {cam} (motion {peak_e:.1f})" if len(by_cam) > 1 else f"Motion peak {peak_e:.1f}",
                }
            )

    # Deduplicate overlapping bursts across multiple cameras by keeping the dominant active camera.
    cam_bursts.sort(key=lambda b: (int(b["startMs"]), -float(b["meanEnergy"])))
    merged: list[dict[str, Any]] = []
    for b in cam_bursts:
        if not merged:
            merged.append(b)
            continue
        last = merged[-1]
        overlap = min(int(last["endMs"]), int(b["endMs"])) - max(int(last["startMs"]), int(b["startMs"]))
        if overlap > 0:
            if float(b["meanEnergy"]) > float(last["meanEnergy"]):
                merged[-1] = b
        else:
            merged.append(b)
    return merged


def select_camera_for_window(
    samples: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    preferred_camera: str | None = None,
    fallback_camera: str = "Rack Cam",
) -> str:
    """Pick explicit `preferred_camera` or the camera with strongest motion inside `[start_ms, end_ms]`."""
    if preferred_camera and preferred_camera.strip():
        return preferred_camera.strip()
    totals: dict[str, list[float]] = {}
    for s in samples:
        s0 = int(s["startMs"])
        s1 = int(s["endMs"])
        if s1 >= start_ms and s0 <= end_ms:
            totals.setdefault(str(s["camera"]), []).append(float(s.get("energy", 0.0)))
    if not totals:
        return fallback_camera
    best_cam = max(totals.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), max(kv[1])))[0]
    return best_cam or fallback_camera


def select_pr_window(
    bursts: list[dict[str, Any]],
    set_at_ms: int,
    prev_set_at_ms: int | None = None,
) -> tuple[int, int, str | None]:
    """Align a PR completion tap with the preceding active lift burst."""
    floor_ms = prev_set_at_ms if prev_set_at_ms and prev_set_at_ms < set_at_ms else (set_at_ms - 180_000)
    ceiling_ms = set_at_ms + 5000
    candidates = [
        b
        for b in bursts
        if int(b["endMs"]) >= floor_ms and int(b["startMs"]) <= ceiling_ms
    ]
    if candidates:
        before_tap = [b for b in candidates if int(b["endMs"]) <= set_at_ms + 3500]
        picked = (before_tap[-1] if before_tap else candidates[-1])
        return int(picked["startMs"]), int(picked["endMs"]), str(picked["camera"])
    fallback_start = max(floor_ms, set_at_ms - 35_000)
    return int(fallback_start), int(set_at_ms + 2000), None


@dataclass
class SegmentInfo:
    path: Path
    start_ms: int
    end_ms: int
    energy: float
    camera: str


@dataclass
class CameraConfig:
    name: str
    slug: str
    rtsp_url: str = ""
    enabled: bool = True
    motion_threshold: float = DEFAULT_MOTION_THRESHOLD
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "rtspUrl": self.rtsp_url,
            "enabled": self.enabled,
            "motionThreshold": self.motion_threshold,
            "roi": list(self.roi),
        }


class ArmoryCamService:
    """Core DVR manager: config persistence, FFmpeg RTSP ring capture, motion indexing & clip cutting."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root = (root_dir or default_root_dir()).resolve()
        self.ring_root = self.root / "ring"
        self.tapes_root = self.root / "tapes"
        self.clips_root = self.root / "clips"
        self.logs_root = self.root / "logs"
        self.config_path = self.root / "config.json"
        self.clips_index_path = self.clips_root / "index.jsonl"
        for d in (self.root, self.ring_root, self.tapes_root, self.clips_root, self.logs_root):
            d.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        self.ffprobe = resolve_tool_bin("ffprobe", "ARMORY_CAM_FFPROBE")
        self.log = logging.getLogger("armory-cam")

        self.lock = threading.RLock()
        self.port = DEFAULT_PORT
        self.retention_days = DEFAULT_RETENTION_DAYS
        self.pre_roll_sec = DEFAULT_PRE_ROLL_SEC
        self.cameras: list[CameraConfig] = []

        self.active_session_id: str | None = None
        self.active_session_name: str = ""
        self.recording: bool = False
        self.paused: bool = False

        # In-memory recent segment index keyed by slug, plus global motion timeline
        self.ring_segments: dict[str, list[SegmentInfo]] = {}
        self.seen_files: set[str] = set()
        self.latest_energy: dict[str, float] = {}
        self.cam_connected: dict[str, bool] = {}

        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._worker_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._started = False

        self.load_or_init_config()
        self._rehydrate_ring_index()

    def load_or_init_config(self) -> None:
        default_cams = [
            CameraConfig(name="Rack Cam", slug="rack_cam"),
            CameraConfig(name="Smith Cam", slug="smith_cam"),
        ]
        if not self.config_path.exists():
            self.cameras = default_cams
            self.save_config()
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.port = int(raw.get("port", DEFAULT_PORT))
            self.retention_days = max(1, int(raw.get("retentionDays", DEFAULT_RETENTION_DAYS)))
            self.pre_roll_sec = max(30, int(raw.get("preRollSec", DEFAULT_PRE_ROLL_SEC)))
            loaded: list[CameraConfig] = []
            for item in raw.get("cameras", []):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                slug = safe_slug(str(item.get("slug") or name).lower(), "cam")
                roi_raw = item.get("roi") or [0.0, 0.0, 1.0, 1.0]
                roi = (
                    float(roi_raw[0]),
                    float(roi_raw[1]),
                    float(roi_raw[2]),
                    float(roi_raw[3]),
                )
                loaded.append(
                    CameraConfig(
                        name=name,
                        slug=slug,
                        rtsp_url=str(item.get("rtspUrl", "")).strip(),
                        enabled=bool(item.get("enabled", True)),
                        motion_threshold=float(item.get("motionThreshold", DEFAULT_MOTION_THRESHOLD)),
                        roi=roi,
                    )
                )
            self.cameras = loaded or default_cams
        except Exception as exc:
            self.log.warning("Failed parsing config.json (%s); using defaults", exc)
            self.cameras = default_cams

    def save_config(self) -> None:
        payload = {
            "port": self.port,
            "retentionDays": self.retention_days,
            "preRollSec": self.pre_roll_sec,
            "cameras": [c.to_dict() for c in self.cameras],
        }
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def update_config(self, raw: dict[str, Any]) -> None:
        with self.lock:
            if "retentionDays" in raw:
                self.retention_days = max(1, min(90, int(raw["retentionDays"])))
            if "preRollSec" in raw:
                self.pre_roll_sec = max(30, min(600, int(raw["preRollSec"])))
            if isinstance(raw.get("cameras"), list):
                next_cams: list[CameraConfig] = []
                for idx, item in enumerate(raw["cameras"]):
                    name = str(item.get("name", f"Camera {idx + 1}")).strip() or f"Camera {idx + 1}"
                    slug = safe_slug(str(item.get("slug") or name).lower(), f"cam_{idx + 1}")
                    roi_raw = item.get("roi") or [0.0, 0.0, 1.0, 1.0]
                    roi = (
                        float(roi_raw[0]),
                        float(roi_raw[1]),
                        float(roi_raw[2]),
                        float(roi_raw[3]),
                    )
                    next_cams.append(
                        CameraConfig(
                            name=name,
                            slug=slug,
                            rtsp_url=str(item.get("rtspUrl", "")).strip(),
                            enabled=bool(item.get("enabled", True)),
                            motion_threshold=float(item.get("motionThreshold", DEFAULT_MOTION_THRESHOLD)),
                            roi=roi,
                        )
                    )
                if next_cams:
                    self.cameras = next_cams
            self.save_config()
            self._restart_capture_workers()

    def _rehydrate_ring_index(self) -> None:
        for cam in self.cameras:
            cdir = self.ring_root / cam.slug
            cdir.mkdir(parents=True, exist_ok=True)
            segs: list[SegmentInfo] = []
            for p in sorted(cdir.glob("seg_*.ts")):
                start_ms = parse_segment_start_ms(p.name)
                if start_ms is None:
                    continue
                self.seen_files.add(str(p))
                segs.append(SegmentInfo(path=p, start_ms=start_ms, end_ms=start_ms + 2000, energy=0.0, camera=cam.name))
            self.ring_segments[cam.slug] = segs

    def start_background(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._restart_capture_workers()
            poll_t = threading.Thread(target=self._segment_poll_loop, name="armory-cam-poller", daemon=True)
            poll_t.start()
            self._worker_threads.append(poll_t)

    def stop_background(self) -> None:
        self._stop_event.set()
        with self.lock:
            for slug in list(self._procs.keys()):
                self._stop_proc(slug)
            self._started = False

    def _stop_proc(self, slug: str) -> None:
        proc = self._procs.pop(slug, None)
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _restart_capture_workers(self) -> None:
        for slug in list(self._procs.keys()):
            self._stop_proc(slug)
        for cam in self.cameras:
            (self.ring_root / cam.slug).mkdir(parents=True, exist_ok=True)
            self.ring_segments.setdefault(cam.slug, [])
            self.cam_connected[cam.slug] = False
            if cam.enabled and cam.rtsp_url:
                t = threading.Thread(
                    target=self._rtsp_capture_loop,
                    args=(cam,),
                    name=f"rtsp-{cam.slug}",
                    daemon=True,
                )
                t.start()
                self._worker_threads.append(t)

    def _rtsp_capture_loop(self, cam: CameraConfig) -> None:
        out_dir = self.ring_root / cam.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / "seg_%Y%m%d_%H%M%S.ts")
        backoff = 2.0
        while not self._stop_event.is_set():
            with self.lock:
                cur = next((c for c in self.cameras if c.slug == cam.slug), None)
                if not cur or not cur.enabled or cur.rtsp_url != cam.rtsp_url or not self._started:
                    return
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-fflags",
                "+genpts",
                "-i",
                cam.rtsp_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                pattern,
            ]
            t_start = time.time()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                with self.lock:
                    self._procs[cam.slug] = proc
                self.cam_connected[cam.slug] = True
                _, stderr_data = proc.communicate()
                self.cam_connected[cam.slug] = False
                if self._stop_event.is_set():
                    return
                if stderr_data:
                    msg = stderr_data.decode("utf-8", errors="ignore").strip().splitlines()[-1:]
                    if msg:
                        self.log.warning("[%s] FFmpeg exited (%s): %s", cam.name, proc.returncode, msg[0])
            except Exception as exc:
                self.cam_connected[cam.slug] = False
                self.log.warning("[%s] RTSP worker error: %s", cam.name, exc)

            uptime = time.time() - t_start
            backoff = 2.0 if uptime > 20.0 else min(15.0, backoff * 1.5)
            self._stop_event.wait(backoff)

    def _segment_poll_loop(self) -> None:
        last_prune = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            with self.lock:
                cams = list(self.cameras)
            for cam in cams:
                self._ingest_closed_segments(cam, now)
            if now - last_prune > 60.0:
                last_prune = now
                self._prune_storage(now)
            self._stop_event.wait(0.8)

    def _ingest_closed_segments(self, cam: CameraConfig, now_s: float) -> None:
        cdir = self.ring_root / cam.slug
        if not cdir.exists():
            return
        files = sorted(cdir.glob("seg_*.ts"))
        for p in files:
            key = str(p)
            if key in self.seen_files:
                continue
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            # Leave the active segment untouched until FFmpeg finishes writing it.
            if now_s - st.st_mtime < 0.7 or st.st_size < 1024:
                continue
            start_ms = parse_segment_start_ms(p.name)
            if start_ms is None:
                start_ms = int(st.st_mtime * 1000) - 2000
            dur_ms, energy = analyze_ts_segment(p, roi=cam.roi)
            info = SegmentInfo(
                path=p,
                start_ms=start_ms,
                end_ms=start_ms + dur_ms,
                energy=energy,
                camera=cam.name,
            )
            with self.lock:
                self.seen_files.add(key)
                self.latest_energy[cam.slug] = energy
                bucket = self.ring_segments.setdefault(cam.slug, [])
                bucket.append(info)
                if len(bucket) > 600:
                    del bucket[:-600]
                if self.recording and not self.paused and self.active_session_id:
                    self._copy_segment_to_session(self.active_session_id, cam, info)

    def _copy_segment_to_session(self, session_id: str, cam: CameraConfig, info: SegmentInfo) -> None:
        sdir = self.tapes_root / safe_slug(session_id, "session") / cam.slug
        sdir.mkdir(parents=True, exist_ok=True)
        dst = sdir / info.path.name
        if not dst.exists() and info.path.exists():
            try:
                os.link(info.path, dst)
            except OSError:
                try:
                    shutil.copy2(info.path, dst)
                except OSError:
                    return
        motion_file = self.tapes_root / safe_slug(session_id, "session") / "motion.jsonl"
        sample = {
            "camera": cam.name,
            "slug": cam.slug,
            "file": info.path.name,
            "startMs": info.start_ms,
            "endMs": info.end_ms,
            "energy": info.energy,
        }
        with motion_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample) + "\n")

    def _prune_storage(self, now_s: float) -> None:
        ring_cutoff = now_s - max(RING_KEEP_MIN_SEC, self.pre_roll_sec * 2)
        with self.lock:
            cams = list(self.cameras)
        for cam in cams:
            cdir = self.ring_root / cam.slug
            if not cdir.exists():
                continue
            for p in cdir.glob("seg_*.ts"):
                try:
                    if p.stat().st_mtime < ring_cutoff:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        with self.lock:
            cutoff_ms = int(ring_cutoff * 1000)
            for slug, segs in self.ring_segments.items():
                self.ring_segments[slug] = [s for s in segs if s.end_ms >= cutoff_ms]

        tape_cutoff = now_s - (self.retention_days * 86_400)
        for sdir in self.tapes_root.iterdir():
            if not sdir.is_dir():
                continue
            if self.active_session_id and sdir.name == safe_slug(self.active_session_id, "session"):
                continue
            try:
                if sdir.stat().st_mtime < tape_cutoff:
                    shutil.rmtree(sdir, ignore_errors=True)
            except OSError:
                pass

    def start_session(self, session_id: str, name: str = "") -> None:
        clean_id = safe_slug(session_id, f"sess_{int(time.time())}")
        with self.lock:
            self.active_session_id = clean_id
            self.active_session_name = name
            self.recording = True
            self.paused = False
            sdir = self.tapes_root / clean_id
            sdir.mkdir(parents=True, exist_ok=True)
            meta_file = sdir / "session.json"
            meta_file.write_text(
                json.dumps({"sessionId": session_id, "name": name, "startedMs": int(time.time() * 1000)}),
                encoding="utf-8",
            )
            pre_cutoff = int(time.time() * 1000) - (self.pre_roll_sec * 1000)
            for cam in self.cameras:
                for info in self.ring_segments.get(cam.slug, []):
                    if info.end_ms >= pre_cutoff:
                        self._copy_segment_to_session(clean_id, cam, info)

    def pause_session(self, session_id: str | None = None) -> None:
        with self.lock:
            if session_id and self.active_session_id and safe_slug(session_id) != self.active_session_id:
                return
            self.paused = True

    def resume_session(self, session_id: str | None = None) -> None:
        with self.lock:
            if session_id and not self.active_session_id:
                self.active_session_id = safe_slug(session_id)
            self.recording = True
            self.paused = False

    def stop_session(self, session_id: str | None = None, discard: bool = False) -> None:
        with self.lock:
            target = safe_slug(session_id) if session_id else self.active_session_id
            self.recording = False
            self.paused = False
            if target and target == self.active_session_id:
                self.active_session_id = None
            if discard and target:
                shutil.rmtree(self.tapes_root / target, ignore_errors=True)

    def session_samples(self, session_id: str | None = None) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        if session_id:
            clean_id = safe_slug(session_id)
            motion_file = self.tapes_root / clean_id / "motion.jsonl"
            if motion_file.exists():
                for line in motion_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        k = (str(obj.get("camera", "")), str(obj.get("file", "")))
                        if k not in seen_keys:
                            seen_keys.add(k)
                            samples.append(obj)
                    except json.JSONDecodeError:
                        continue
        with self.lock:
            for cam in self.cameras:
                for info in self.ring_segments.get(cam.slug, []):
                    k = (info.camera, info.path.name)
                    if k not in seen_keys:
                        seen_keys.add(k)
                        samples.append(
                            {
                                "camera": info.camera,
                                "slug": cam.slug,
                                "file": info.path.name,
                                "startMs": info.start_ms,
                                "endMs": info.end_ms,
                                "energy": info.energy,
                            }
                        )
        samples.sort(key=lambda x: int(x["startMs"]))
        return samples

    def find_segments(self, camera_name: str, session_id: str, start_ms: int, end_ms: int) -> list[SegmentInfo]:
        cam = self._find_camera(camera_name)
        slug = cam.slug if cam else safe_slug(camera_name.lower(), "rack_cam")
        canon_name = cam.name if cam else camera_name
        clean_id = safe_slug(session_id, "session") if session_id else ""

        by_name: dict[str, SegmentInfo] = {}
        motion_map: dict[str, tuple[int, int, float]] = {}
        if clean_id:
            motion_file = self.tapes_root / clean_id / "motion.jsonl"
            if motion_file.exists():
                for line in motion_file.read_text(encoding="utf-8").splitlines():
                    try:
                        obj = json.loads(line)
                        if obj.get("slug") == slug or obj.get("camera") == canon_name:
                            motion_map[str(obj["file"])] = (
                                int(obj["startMs"]),
                                int(obj["endMs"]),
                                float(obj.get("energy", 0.0)),
                            )
                    except Exception:
                        continue

            tape_cam_dir = self.tapes_root / clean_id / slug
            if tape_cam_dir.exists():
                for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                    m = motion_map.get(p.name)
                    s_ms = m[0] if m else (parse_segment_start_ms(p.name) or 0)
                    e_ms = m[1] if m else (s_ms + 2000)
                    eng = m[2] if m else 0.0
                    by_name[p.name] = SegmentInfo(path=p, start_ms=s_ms, end_ms=e_ms, energy=eng, camera=canon_name)

        with self.lock:
            for info in self.ring_segments.get(slug, []):
                if info.path.exists() and info.path.name not in by_name:
                    by_name[info.path.name] = info

        ordered = sorted(by_name.values(), key=lambda s: s.start_ms)
        return [s for s in ordered if s.end_ms >= start_ms - 2500 and s.start_ms <= end_ms + 2500]

    def _find_camera(self, name_or_slug: str | None) -> CameraConfig | None:
        if not name_or_slug:
            return self.cameras[0] if self.cameras else None
        q = name_or_slug.strip().lower()
        for c in self.cameras:
            if c.name.lower() == q or c.slug.lower() == q:
                return c
        return self.cameras[0] if self.cameras else None

    def bursts_for_session(self, session_id: str, around_ms: int | None = None) -> list[dict[str, Any]]:
        samples = self.session_samples(session_id)
        min_thresh = min((c.motion_threshold for c in self.cameras), default=DEFAULT_MOTION_THRESHOLD)
        bursts = cluster_motion_bursts(samples, threshold=min_thresh)
        if around_ms is not None:
            bursts.sort(key=lambda b: abs(int(b["endMs"]) - around_ms))
            bursts = bursts[:12]
            bursts.sort(key=lambda b: int(b["startMs"]))
        else:
            bursts = bursts[-12:]

        for b in bursts:
            mid_ms = (int(b["startMs"]) + int(b["endMs"])) // 2
            b["thumb"] = self._thumb_near_ms(str(b["camera"]), session_id, mid_ms)
        return bursts

    def _thumb_near_ms(self, camera_name: str, session_id: str, target_ms: int) -> str:
        segs = self.find_segments(camera_name, session_id, target_ms - 3000, target_ms + 3000)
        if not segs:
            return ""
        best = min(segs, key=lambda s: abs(((s.start_ms + s.end_ms) // 2) - target_ms))
        return extract_video_thumb(best.path)

    def get_snapshot_jpeg(self, camera_name: str | None = None) -> bytes | None:
        cam = self._find_camera(camera_name)
        if not cam:
            return None
        with self.lock:
            segs = list(self.ring_segments.get(cam.slug, []))
        for seg in reversed(segs[-4:]):
            if seg.path.exists():
                cap = cv2.VideoCapture(str(seg.path))
                if cap.isOpened():
                    ok, frm = cap.read()
                    cap.release()
                    if ok and frm is not None:
                        ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                        if ok_jpg:
                            return buf.tobytes()

        # Live RTSP 1-frame fallback if configured but no closed segment yet.
        if cam.enabled and cam.rtsp_url:
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                cam.rtsp_url,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=5)
                if res.returncode == 0 and res.stdout.startswith(b"\xff\xd8"):
                    return res.stdout
            except Exception:
                pass
        return None

    def cut_clip(
        self,
        session_id: str,
        ex_name: str,
        start_ms: int,
        end_ms: int,
        camera_override: str | None = None,
    ) -> dict[str, Any] | None:
        if end_ms <= start_ms:
            end_ms = start_ms + 5000

        samples = self.session_samples(session_id)
        fallback_cam = self.cameras[0].name if self.cameras else "Rack Cam"
        chosen_cam = select_camera_for_window(
            samples, start_ms, end_ms, preferred_camera=camera_override, fallback_camera=fallback_cam
        )
        segs = self.find_segments(chosen_cam, session_id, start_ms, end_ms)
        if not segs and not camera_override:
            for alt in self.cameras:
                if alt.name != chosen_cam:
                    alt_segs = self.find_segments(alt.name, session_id, start_ms, end_ms)
                    if alt_segs:
                        chosen_cam = alt.name
                        segs = alt_segs
                        break
        if not segs:
            return None

        first_start_ms = segs[0].start_ms
        ss_sec = max(0.0, (start_ms - first_start_ms) / 1000.0)
        dur_sec = max(0.5, (end_ms - start_ms) / 1000.0)

        day_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%H%M%S")
        day_dir = self.clips_root / day_str
        day_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{safe_slug(ex_name or 'lift')}_{time_str}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = day_dir / out_name
        concat_path = day_dir / f".concat_{uuid.uuid4().hex[:8]}.txt"

        try:
            lines = []
            for s in segs:
                posix_p = s.path.resolve().as_posix().replace("'", r"'\''")
                lines.append(f"file '{posix_p}'")
            concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            cmd = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-ss",
                f"{ss_sec:.3f}",
                "-t",
                f"{dur_sec:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(out_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
                self.log.warning("FFmpeg clip failed: %s", res.stderr.strip())
                out_path.unlink(missing_ok=True)
                return None

            bytes_len = out_path.stat().st_size
            thumb = extract_video_thumb(out_path)
            actual_dur = self._probe_duration_sec(out_path) or int(round(dur_sec))
            rel_url = f"/clips/{day_str}/{out_name}"
            meta = {
                "ok": True,
                "lanUrl": rel_url,
                "fileName": out_name,
                "camera": chosen_cam,
                "thumb": thumb,
                "durationSec": max(1, actual_dur),
                "startMs": int(start_ms),
                "endMs": int(end_ms),
                "bytes": bytes_len,
                "exName": ex_name,
                "createdAtMs": int(time.time() * 1000),
            }
            with self.clips_index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in meta.items() if k != "thumb"}) + "\n")
            return meta
        finally:
            concat_path.unlink(missing_ok=True)

    def _probe_duration_sec(self, path: Path) -> int:
        try:
            res = subprocess.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if res.returncode == 0 and res.stdout.strip():
                return max(1, int(round(float(res.stdout.strip()))))
        except Exception:
            pass
        return 0

    def recent_clips(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.clips_index_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(self.clips_index_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def disk_stats(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        return {
            "root": str(self.root),
            "freeGb": round(usage.free / (1024**3), 1),
            "totalGb": round(usage.total / (1024**3), 1),
        }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Cam · OptiPlex DVR</title>
<style>
  :root{--bg:#0b0f14;--card:#131a22;--line:#233040;--fg:#edf2f7;--mut:#8b9bb0;--blue:#1e9bff;--green:#22c55e;--amber:#f59e0b;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:20px}
  h1{margin:0 0 6px;font-size:22px;display:flex;align-items:center;gap:10px}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;background:#1d2836;color:var(--mut);font-weight:700}
  .pill.ok{background:rgba(34,197,94,.16);color:#86efac}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
  .card h2{margin:0 0 10px;font-size:15px;display:flex;justify-content:space-between;align-items:center}
  label{display:block;font-size:12px;color:var(--mut);margin:8px 0 4px}
  input[type=text],input[type=number]{width:100%;padding:9px 10px;border-radius:9px;border:1px solid var(--line);background:#0d1218;color:var(--fg);font:inherit}
  .row{display:flex;gap:8px;align-items:center;margin-top:10px}
  .btn{background:var(--blue);color:#00131f;border:0;border-radius:9px;padding:9px 14px;font-weight:800;cursor:pointer}
  .btn.sec{background:#1d2836;color:var(--fg);border:1px solid var(--line)}
  img.snap{width:100%;aspect-ratio:16/9;object-fit:cover;background:#080b0f;border-radius:10px;border:1px solid var(--line);margin-top:10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 6px;border-top:1px solid var(--line)}
  a{color:#7cc4ff;text-decoration:none}
  code{background:#0d1218;padding:2px 6px;border-radius:6px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>ARMORY CAM <span id="svcBadge" class="pill">Checking…</span></h1>
  <div id="subInfo" style="color:var(--mut)">Loading OptiPlex status…</div>

  <div id="camGrid" class="grid"></div>

  <div class="card" style="margin-top:14px">
    <h2>Retention &amp; Save</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div><label>Full Tape Retention (days)</label><input id="retDays" type="number" min="1" max="90"></div>
      <div><label>Pre-Roll Buffer (seconds)</label><input id="preRoll" type="number" min="30" max="600"></div>
    </div>
    <div class="row">
      <button class="btn" id="saveBtn" type="button">Save Camera Config &amp; Connect</button>
      <span id="saveMsg" style="color:var(--mut);font-size:12px"></span>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Recent Saved Clips</h2>
    <div id="clipList" style="color:var(--mut)">No clips saved yet.</div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let cfg = null;

async function refresh() {
  const [ping, conf, clips] = await Promise.all([
    fetch('/api/ping').then(r => r.json()),
    fetch('/api/config').then(r => r.json()),
    fetch('/api/clips').then(r => r.json())
  ]);
  cfg = conf;
  const badge = $('svcBadge');
  badge.textContent = ping.recording ? (ping.paused ? 'PAUSED' : 'RECORDING') : 'STANDBY RING';
  badge.className = 'pill ' + (ping.recording ? 'ok' : '');
  $('subInfo').innerHTML = `Storage: <code>${conf.disk.root}</code> · <b>${conf.disk.freeGb} GB</b> free / ${conf.disk.totalGb} GB total · Thingino default: <code>rtsp://thingino:thingino@&lt;CAM_IP&gt;/ch0</code>`;
  $('retDays').value = conf.retentionDays;
  $('preRoll').value = conf.preRollSec;

  $('camGrid').innerHTML = conf.cameras.map((c, idx) => `
    <div class="card">
      <h2>
        <span>${c.name}</span>
        <span class="pill ${c.connected ? 'ok' : ''}">${c.connected ? 'STREAMING' : (c.rtspUrl ? 'RECONNECTING' : 'NO RTSP URL')}</span>
      </h2>
      <div style="font-size:12px;color:var(--mut)">Latest motion energy: <b>${(c.latestEnergy || 0).toFixed(2)}</b> (threshold ${c.motionThreshold})</div>
      <label>RTSP URL</label>
      <input type="text" data-rtsp="${idx}" value="${c.rtspUrl || ''}" placeholder="rtsp://thingino:thingino@192.168.1.xxx/ch0">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <div><label>Motion Threshold</label><input type="number" step="0.2" data-thresh="${idx}" value="${c.motionThreshold}"></div>
        <div><label>Enabled</label><select data-en="${idx}" style="width:100%;padding:9px;border-radius:9px;border:1px solid var(--line);background:#0d1218;color:var(--fg)"><option value="1"${c.enabled?' selected':''}>Yes</option><option value="0"${!c.enabled?' selected':''}>No</option></select></div>
      </div>
      <img class="snap" src="/api/snapshot?camera=${encodeURIComponent(c.name)}&t=${Date.now()}" onerror="this.style.opacity=0.25" alt="${c.name}">
    </div>
  `).join('');

  if (clips.clips && clips.clips.length) {
    $('clipList').innerHTML = `<table><thead><tr><th>Clip</th><th>Camera</th><th>Duration</th><th>Size</th></tr></thead><tbody>` +
      clips.clips.map(x => `<tr><td><a href="${x.lanUrl}" target="_blank">${x.fileName}</a></td><td>${x.camera}</td><td>${x.durationSec}s</td><td>${(x.bytes/1048576).toFixed(1)} MB</td></tr>`).join('') +
      `</tbody></table>`;
  }
}

$('saveBtn').onclick = async () => {
  if (!cfg) return;
  const cameras = cfg.cameras.map((c, idx) => ({
    ...c,
    rtspUrl: document.querySelector(`[data-rtsp="${idx}"]`).value.trim(),
    motionThreshold: Number(document.querySelector(`[data-thresh="${idx}"]`).value) || 2.2,
    enabled: document.querySelector(`[data-en="${idx}"]`).value === '1',
  }));
  const body = {
    retentionDays: Number($('retDays').value) || 14,
    preRollSec: Number($('preRoll').value) || 120,
    cameras,
  };
  const res = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
  $('saveMsg').textContent = res.ok ? 'Saved & reconnecting cameras…' : 'Save failed';
  setTimeout(refresh, 1200);
};

refresh();
setInterval(() => {
  if (!document.activeElement || !['INPUT','SELECT'].includes(document.activeElement.tagName)) refresh();
}, 10000);
</script>
</body>
</html>
"""


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None | str:
    """Parse HTTP `Range: bytes=start-end`; returns `(start, end)`, `None` (no range), or `'invalid'`."""
    if not range_header:
        return None
    val = range_header.strip()
    if not val.startswith("bytes="):
        return "invalid"
    spec = val[len("bytes=") :].split(",")[0].strip()
    if "-" not in spec:
        return "invalid"
    left, right = spec.split("-", 1)
    try:
        if left == "":
            suffix = int(right)
            if suffix <= 0:
                return "invalid"
            start = max(0, file_size - suffix)
            end = file_size - 1
        elif right == "":
            start = int(left)
            end = file_size - 1
        else:
            start = int(left)
            end = min(int(right), file_size - 1)
    except ValueError:
        return "invalid"
    if start < 0 or end < start or start >= file_size:
        return "invalid"
    return (start, end)


def create_app(service: ArmoryCamService) -> Flask:
    app = Flask("armory-cam")

    @app.after_request
    def add_cors(resp: Response) -> Response:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
        return resp

    @app.route("/<path:dummy>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def handle_options(dummy: str = "") -> Response:
        return Response(status=204)

    @app.get("/")
    def index() -> Response:
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/api/ping")
    def api_ping() -> Response:
        with service.lock:
            cams = [c.name for c in service.cameras if c.enabled]
            rec = service.recording
            paused = service.paused
        return jsonify(
            {
                "service": "armory-cam",
                "serverEpochMs": int(time.time() * 1000),
                "recording": rec,
                "paused": paused,
                "cameras": cams,
            }
        )

    @app.get("/api/config")
    def api_get_config() -> Response:
        with service.lock:
            cams = [
                {
                    **c.to_dict(),
                    "connected": bool(service.cam_connected.get(c.slug, False)),
                    "latestEnergy": float(service.latest_energy.get(c.slug, 0.0)),
                }
                for c in service.cameras
            ]
            payload = {
                "port": service.port,
                "retentionDays": service.retention_days,
                "preRollSec": service.pre_roll_sec,
                "cameras": cams,
                "disk": service.disk_stats(),
            }
        return jsonify(payload)

    @app.post("/api/config")
    def api_post_config() -> Response:
        body = request.get_json(silent=True) or {}
        service.update_config(body)
        return jsonify({"ok": True})

    @app.get("/api/clips")
    def api_clips() -> Response:
        return jsonify({"clips": service.recent_clips(30)})

    @app.get("/api/snapshot")
    def api_snapshot() -> Response:
        cam = request.args.get("camera")
        jpg = service.get_snapshot_jpeg(cam)
        if not jpg:
            return Response(status=404)
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/session/start")
    def api_session_start() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or f"sess_{int(time.time())}")
        name = str(body.get("name") or "")
        service.start_session(sid, name)
        return jsonify({"ok": True})

    @app.post("/api/session/pause")
    def api_session_pause() -> Response:
        body = request.get_json(silent=True) or {}
        service.pause_session(body.get("sessionId"))
        return jsonify({"ok": True})

    @app.post("/api/session/resume")
    def api_session_resume() -> Response:
        body = request.get_json(silent=True) or {}
        service.resume_session(body.get("sessionId"))
        return jsonify({"ok": True})

    @app.post("/api/session/stop")
    def api_session_stop() -> Response:
        body = request.get_json(silent=True) or {}
        service.stop_session(body.get("sessionId"), discard=bool(body.get("discard", False)))
        return jsonify({"ok": True})

    @app.get("/api/bursts")
    def api_bursts() -> Response:
        sid = request.args.get("sessionId", "")
        around_raw = request.args.get("aroundMs")
        around_ms = int(float(around_raw)) if around_raw is not None else None
        bursts = service.bursts_for_session(sid, around_ms=around_ms)
        return jsonify({"bursts": bursts})

    @app.post("/api/clip")
    def api_clip() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or "")
        ex_name = str(body.get("exName") or body.get("exId") or "lift")
        start_ms = int(body.get("startMs") or (time.time() * 1000 - 20_000))
        end_ms = int(body.get("endMs") or time.time() * 1000)
        cam_override = body.get("camera")
        clip = service.cut_clip(
            session_id=sid,
            ex_name=ex_name,
            start_ms=start_ms,
            end_ms=end_ms,
            camera_override=str(cam_override) if cam_override else None,
        )
        if not clip:
            return jsonify({"ok": False, "error": "No matching video segments found"}), 404
        return jsonify(clip)

    @app.post("/api/clip/auto-pr")
    def api_clip_auto_pr() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or "")
        ex_name = str(body.get("exName") or body.get("exId") or "PR")
        set_at_ms = int(body.get("setAtMs") or time.time() * 1000)
        prev_raw = body.get("prevSetAtMs")
        prev_ms = int(prev_raw) if prev_raw is not None else None

        samples = service.session_samples(sid)
        min_thresh = min((c.motion_threshold for c in service.cameras), default=DEFAULT_MOTION_THRESHOLD)
        bursts = cluster_motion_bursts(samples, threshold=min_thresh)
        start_ms, end_ms, cam = select_pr_window(bursts, set_at_ms=set_at_ms, prev_set_at_ms=prev_ms)
        clip = service.cut_clip(
            session_id=sid,
            ex_name=f"{ex_name}_PR",
            start_ms=start_ms,
            end_ms=end_ms,
            camera_override=cam,
        )
        if not clip:
            return jsonify({"ok": False, "error": "No PR video segments found"}), 404
        return jsonify(clip)

    @app.get("/clips/<path:relpath>")
    def serve_clip(relpath: str) -> Response:
        base = service.clips_root.resolve()
        target = (base / relpath).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return Response(status=403)
        if not target.is_file():
            return Response(status=404)

        size = target.stat().st_size
        parsed = parse_http_range(request.headers.get("Range"), size)
        if parsed == "invalid":
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

        start, end = (0, size - 1) if parsed is None else parsed
        length = end - start + 1

        def generate():
            remaining = length
            with target.open("rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(65_536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        status = 200 if parsed is None else 206
        resp = Response(generate(), status=status, mimetype="video/mp4", direct_passthrough=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        if parsed is not None:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return resp

    return app


def run_server(root_dir: Path | None = None, port_override: int | None = None) -> None:
    svc = ArmoryCamService(root_dir=root_dir)
    log_file = svc.logs_root / "server.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    port = port_override or svc.port
    app = create_app(svc)
    svc.start_background()
    server = make_server("0.0.0.0", port, app, threaded=True)
    logging.info("Armory Cam listening on http://0.0.0.0:%d (root=%s, ffmpeg=%s)", port, svc.root, svc.ffmpeg)
    try:
        server.serve_forever()
    finally:
        svc.stop_background()


if __name__ == "__main__":
    p_arg = int(os.environ["ARMORY_CAM_PORT"]) if os.environ.get("ARMORY_CAM_PORT") else None
    run_server(port_override=p_arg)
