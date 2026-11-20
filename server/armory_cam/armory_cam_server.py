"""Armory Cam — OptiPlex LAN DVR, Live MJPEG Streamer, Motion-Burst Detector & Clip Server.

Listens on port 8766 (default) and serves the exact REST + HTTP 206 byte-range
contract consumed by Armory Fit (`src/lifts/gymCam.ts`), plus a real-time
web dashboard at `/` with interactive zoom/crop + rotate controls.

Configuration & media live in `ARMORY_CAM_ROOT` (default `S:\\ArmoryCam`),
outside the git workspace so RTSP credentials are never committed.
"""

from __future__ import annotations

import base64
import bisect
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
import glob
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
import urllib.error
import urllib.parse
import urllib.request
import uuid

import cv2
from flask import Flask, Response, jsonify, request
import numpy as np
from werkzeug.serving import make_server

DEFAULT_PORT = 8766
DEFAULT_RETENTION_DAYS = 14
DEFAULT_PRE_ROLL_SEC = 120
DEFAULT_MOTION_THRESHOLD = 2.2
DEFAULT_ADB_PORT = 5555
DEFAULT_IDLE_SLEEP_SEC = 600
DEFAULT_SCRUB_THUMB_W = 280
DEFAULT_SCRUB_THUMB_Q = 58
MIN_CROP_SPAN = 0.15
MIN_EXCL_SPAN = 0.04
MAX_EXCLUSIONS = 6
Quad = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
RING_KEEP_MIN_SEC = 300
PREVIEW_KEEP_SEC = 24 * 3600
PREVIEW_MAX_FILES = 120
SEG_NAME_RE = re.compile(r"^seg_(\d{8})_(\d{6})\.ts$")

FALLBACK_WALLBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Fit · Gym Wallboard</title>
<style>html,body{height:100%;margin:0;background:#070b12;color:#f2f6fc;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;text-align:center}
.box{padding:32px;border:1px solid #1e2b42;border-radius:18px;background:#0f1724;max-width:680px}.mut{color:#8fa2bf;margin-top:8px;font-size:18px}
h1{margin:0;font-size:40px;letter-spacing:.08em}#st{font-size:28px;margin-top:16px;color:#38bdf8;font-weight:700}</style></head>
<body><div class="box"><h1>ARMORY FIT · WALLBOARD</h1><div id="st">Standby · Waiting for workout on Pixel</div>
<div class="mut" id="sub">Connect Armory Fit on Wi-Fi to start streaming live sets, rest timer &amp; HR.</div></div>
<script>
const paint = (d) => {
  const st = document.getElementById('st'), sub = document.getElementById('sub');
  if (!d) return;
  if (d.mode === 'lift' && d.lift) {
    st.textContent = d.lift.title + ' · ' + d.lift.setsDone + '/' + d.lift.setsTotal + ' sets';
    sub.textContent = d.hr && d.hr.fresh ? ('HR ' + d.hr.bpm + ' BPM') : 'Strength active';
  } else if (d.mode === 'cardio' && d.cardio) {
    st.textContent = d.cardio.title + ' · ' + d.cardio.phase;
    sub.textContent = Math.round(d.cardio.watts || 0) + ' W · ' + (d.cardio.countdownText || '');
  } else {
    st.textContent = (d.standby && d.standby.timeLabel) ? ('Standby · ' + d.standby.timeLabel) : 'Standby';
    sub.textContent = 'Start a workout on your Pixel to launch live telemetry.';
  }
};
fetch('/wallboard/state?t=' + Date.now()).then(r => r.json()).then(paint).catch(() => {});
try { const es = new EventSource('/wallboard/events'); es.onmessage = (e) => { try { paint(JSON.parse(e.data)); } catch {} }; } catch {}
</script></body></html>"""


DEFAULT_DRIVE_BACKUP_BASE = Path(r"J:\My Drive\Backups\ArmoryFit Backup")
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
CALIBRATION_MILESTONES = (5, 15, 50)
DEFAULT_NTFY_TOPIC = "FroriepAntigravity"


def send_ntfy_alert(topic: str, title: str, message: str, tags: str = "movie_camera,bell") -> bool:
    """Send a push notification via ntfy.sh (best-effort, non-blocking failure)."""
    clean = topic.strip()
    if not clean:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(clean)}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": tags, "Priority": "default"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def default_root_dir() -> Path:
    env = os.environ.get("ARMORY_CAM_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(r"S:\ArmoryCam")


def default_drive_backup_dir() -> str:
    env = os.environ.get("ARMORY_CAM_DRIVE_DIR", "").strip()
    if env:
        return env
    if DEFAULT_DRIVE_BACKUP_BASE.exists():
        return str(DEFAULT_DRIVE_BACKUP_BASE / "Clips")
    return ""


def read_google_drive_file_id(path: Path) -> str | None:
    """Read Windows Google Drive for Desktop `user.drive.id` NTFS stream, returning Drive file ID or None."""
    if os.name != "nt":
        return None
    try:
        with open(f"{path}:user.drive.id", "r", encoding="utf-8") as fh:
            fid = fh.read().strip()
        if fid and DRIVE_ID_RE.match(fid) and not fid.lower().startswith(("local", "tmp")):
            return fid
    except OSError:
        pass
    return None


def google_drive_view_url(file_id: str | None) -> str | None:
    if not file_id:
        return None
    return f"https://drive.google.com/file/d/{file_id}/view"


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


def resolve_adb_bin() -> str | None:
    """Resolve `adb` binary from env, PATH, WinGet PlatformTools, or Android SDK."""
    env_val = os.environ.get("ARMORY_ADB", "").strip()
    if env_val and Path(env_val).exists():
        return env_val
    found = shutil.which("adb")
    if found:
        return found
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local_app:
        candidates.append(Path(local_app) / "Microsoft" / "WinGet" / "Links" / "adb.exe")
        winget_glob = glob.glob(
            str(Path(local_app) / "Microsoft" / "WinGet" / "Packages" / "Google.PlatformTools*" / "platform-tools" / "adb.exe")
        )
        candidates.extend(Path(p) for p in winget_glob)
        candidates.append(Path(local_app) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    candidates.append(Path(r"C:\Users\davef\Tools\platform-tools\adb.exe"))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def detect_lan_ip() -> str:
    """Best-effort LAN IPv4 lookup for building the static OptiPlex wallboard URL."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.1.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "192.168.1.152"


def extract_wallboard_html_from_ts(ts_path: Path) -> str | None:
    """Extract literal `WALLBOARD_HTML` string from `src/wallboard/page.ts` if present."""
    if not ts_path.is_file():
        return None
    try:
        raw = ts_path.read_text(encoding="utf-8")
        marker = "export const WALLBOARD_HTML = `"
        start = raw.find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = raw.rfind("`;")
        if end <= start:
            return None
        html = raw[start:end]
        return html if "<!doctype html" in html[:200].lower() else None
    except Exception:
        return None


def _subprocess_no_window_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


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
    return cleaned[:48] or fallback


def normalize_crop(raw: Any) -> tuple[float, float, float, float]:
    """Clamp normalized `[x0, y0, x1, y1]` crop box to valid bounds with >= MIN_CROP_SPAN."""
    try:
        vals = [float(v) for v in raw]
        if len(vals) != 4 or any(not np.isfinite(v) for v in vals):
            return (0.0, 0.0, 1.0, 1.0)
        x0, y0, x1, y1 = vals
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0 = max(0.0, min(1.0 - MIN_CROP_SPAN, x0))
        y0 = max(0.0, min(1.0 - MIN_CROP_SPAN, y0))
        x1 = max(x0 + MIN_CROP_SPAN, min(1.0, x1))
        y1 = max(y0 + MIN_CROP_SPAN, min(1.0, y1))
        return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
    except Exception:
        return (0.0, 0.0, 1.0, 1.0)


def _tri_area(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return 0.5 * abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def normalize_excl_box(raw: Any) -> tuple[float, float, float, float] | None:
    """Clamp normalized `[x0, y0, x1, y1]` exclusion box with >= MIN_EXCL_SPAN, or None if invalid."""
    try:
        vals = [float(v) for v in raw]
        if len(vals) != 4 or any(not np.isfinite(v) for v in vals):
            return None
        x0, y0, x1, y1 = vals
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0 = max(0.0, min(1.0 - MIN_EXCL_SPAN, x0))
        y0 = max(0.0, min(1.0 - MIN_EXCL_SPAN, y0))
        x1 = max(x0 + MIN_EXCL_SPAN, min(1.0, x1))
        y1 = max(y0 + MIN_EXCL_SPAN, min(1.0, y1))
        return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
    except Exception:
        return None


def normalize_excl_quad(raw: Any) -> Quad | None:
    """Normalize a 4-point quad `[[x,y], ...]` or legacy `[x0,y0,x1,y1]` rect to clamped 4-point Quad."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        box = normalize_excl_box(raw)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))

    pts: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        px_raw, py_raw = item
        if (
            not isinstance(px_raw, (int, float))
            or isinstance(px_raw, bool)
            or not isinstance(py_raw, (int, float))
            or isinstance(py_raw, bool)
        ):
            return None
        px, py = float(px_raw), float(py_raw)
        if not np.isfinite(px) or not np.isfinite(py):
            return None
        pts.append((round(max(0.0, min(1.0, px)), 4), round(max(0.0, min(1.0, py)), 4)))

    p0, p1, p2, p3 = pts[0], pts[1], pts[2], pts[3]
    area = max(
        _tri_area(p0, p1, p2) + _tri_area(p0, p2, p3),
        _tri_area(p0, p1, p3) + _tri_area(p1, p2, p3),
    )
    if area < 1e-4:
        return None
    return (p0, p1, p2, p3)


def normalize_exclusions(raw: Any) -> list[Quad]:
    """Normalize up to MAX_EXCLUSIONS 4-point quad (or legacy rect) motion exclusion masks."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[Quad] = []
    for item in raw:
        quad = normalize_excl_quad(item)
        if quad is not None:
            out.append(quad)
            if len(out) >= MAX_EXCLUSIONS:
                break
    return out


def normalize_rotate(raw: Any) -> int:
    try:
        v = int(raw) % 360
        return v if v in (0, 90, 180, 270) else 0
    except Exception:
        return 0


def normalize_tilt(raw: Any) -> float:
    try:
        v = float(raw)
    except Exception:
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = max(-45.0, min(45.0, round(v, 1)))
    return 0.0 if abs(v) < 0.05 else v


def tilted_frame_size(w: int, h: int, tilt: float) -> tuple[int, int]:
    t = normalize_tilt(tilt)
    if t == 0.0:
        return w, h
    rad = math.radians(t)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    nw = max(2, int((w * ca + h * sa) // 2) * 2)
    nh = max(2, int((w * sa + h * ca) // 2) * 2)
    return nw, nh


def build_ffmpeg_vf(
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    rotate: int = 0,
    scale_w: int | None = None,
    fps: int | None = None,
    tilt: float = 0.0,
) -> str | None:
    """Build ordered FFmpeg `-vf` chain (rotate -> tilt -> even-pixel crop -> optional fps/scale)."""
    parts: list[str] = []
    rot = normalize_rotate(rotate)
    if rot == 90:
        parts.append("transpose=1")
    elif rot == 180:
        parts.append("hflip,vflip")
    elif rot == 270:
        parts.append("transpose=2")

    tlt = normalize_tilt(tilt)
    if tlt != 0.0:
        rad = math.radians(tlt)
        parts.append(
            f"rotate={rad:.6f}:"
            f"ow=max(2\\,trunc(rotw({rad:.6f})/2)*2):"
            f"oh=max(2\\,trunc(roth({rad:.6f})/2)*2):"
            "fillcolor=black"
        )

    x0, y0, x1, y1 = normalize_crop(crop)
    if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
        w = max(MIN_CROP_SPAN, x1 - x0)
        h = max(MIN_CROP_SPAN, y1 - y0)
        parts.append(
            f"crop=max(2\\,trunc(iw*{w:.6f}/2)*2):max(2\\,trunc(ih*{h:.6f}/2)*2):"
            f"trunc(iw*{x0:.6f}/2)*2:trunc(ih*{y0:.6f}/2)*2"
        )

    if fps and int(fps) > 0:
        parts.append(f"fps={int(fps)}")
    if scale_w and int(scale_w) > 0:
        parts.append(f"scale={int(scale_w)}:-2")
    return ",".join(parts) if parts else None


def apply_frame_transform(
    frame: np.ndarray,
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    rotate: int = 0,
    apply_crop: bool = True,
    tilt: float = 0.0,
) -> np.ndarray:
    """Apply OpenCV rotation + fine tilt + optional normalized crop matching `build_ffmpeg_vf`."""
    if frame is None or frame.size == 0:
        return frame
    out = frame
    rot = normalize_rotate(rotate)
    if rot == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif rot == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)

    tlt = normalize_tilt(tilt)
    if tlt != 0.0:
        h0, w0 = out.shape[:2]
        nw, nh = tilted_frame_size(w0, h0, tlt)
        mat = cv2.getRotationMatrix2D((w0 / 2.0, h0 / 2.0), -tlt, 1.0)
        mat[0, 2] += (nw - w0) / 2.0
        mat[1, 2] += (nh - h0) / 2.0
        out = cv2.warpAffine(out, mat, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))

    if apply_crop:
        x0, y0, x1, y1 = normalize_crop(crop)
        if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
            h, w = out.shape[:2]
            span_w = max(MIN_CROP_SPAN, x1 - x0)
            span_h = max(MIN_CROP_SPAN, y1 - y0)
            cw = max(2, min(w, int((w * span_w) // 2) * 2))
            ch = max(2, min(h, int((h * span_h) // 2) * 2))
            cx0 = max(0, min(w - cw, int((w * x0) // 2) * 2))
            cy0 = max(0, min(h - ch, int((h * y0) // 2) * 2))
            out = out[cy0 : cy0 + ch, cx0 : cx0 + cw]
    return out


def compute_motion_energy_from_frames(
    frames: list[np.ndarray],
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    exclusions: Sequence[Any] = (),
) -> float:
    """Compute lightweight motion score (0..100) across sampled BGR frames inside normalized ROI minus exclusion quads."""
    if len(frames) < 2:
        return 0.0
    x0, y0, x1, y1 = normalize_crop(roi)
    excl_list = normalize_exclusions(list(exclusions)) if exclusions else []

    prep: list[np.ndarray] = []
    include_mask: np.ndarray | None = None
    for frm in frames:
        small = cv2.resize(frm, (160, 90), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        cx0, cx1 = int(round(x0 * w)), int(round(x1 * w))
        cy0, cy1 = int(round(y0 * h)), int(round(y1 * h))
        rx0, ry0 = max(0, cx0), max(0, cy0)
        rx1, ry1 = min(w, max(rx0 + 2, cx1)), min(h, max(ry0 + 2, cy1))
        crop = small[ry0:ry1, rx0:rx1]
        if include_mask is None and excl_list:
            mask_u8 = np.ones((crop.shape[0], crop.shape[1]), dtype=np.uint8)
            shift = 4
            scale = 1 << shift
            for quad in excl_list:
                pts = np.array(
                    [
                        [int(round((px * w - rx0) * scale)), int(round((py * h - ry0) * scale))]
                        for px, py in quad
                    ],
                    dtype=np.int32,
                )
                cv2.fillPoly(mask_u8, [pts], 0, shift=shift)
            include_mask = mask_u8.astype(bool)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        prep.append(cv2.GaussianBlur(gray, (5, 5), 0))

    if include_mask is not None and not np.any(include_mask):
        return 0.0

    best = 0.0
    for i in range(len(prep) - 1):
        diff = cv2.absdiff(prep[i], prep[i + 1])
        vals = diff[include_mask] if include_mask is not None else diff
        mean_diff = float(np.mean(vals))
        active_pct = float(np.mean(vals > 14)) * 100.0
        score = round(0.6 * mean_diff + 0.25 * active_pct, 3)
        if score > best:
            best = score
    return min(100.0, best)


def extract_ts_sampled_frames(seg_path: Path, fallback_dur_ms: int = 2000) -> tuple[int, list[np.ndarray]]:
    """Read up to 4 sampled raw BGR frames and duration (ms) from an MPEG-TS segment in a single decode pass."""
    cap = cv2.VideoCapture(str(seg_path))
    if not cap.isOpened():
        return fallback_dur_ms, []
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
        if len(frames) < 2:
            frames.clear()
            cap.release()
            cap = cv2.VideoCapture(str(seg_path))
            if cap.isOpened():
                if total >= 4:
                    target_set = {0, total // 3, (2 * total) // 3, total - 1}
                    for i in range(total):
                        if not cap.grab():
                            break
                        if i in target_set:
                            ok, frm = cap.retrieve()
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
        return dur_ms, frames
    finally:
        cap.release()


def analyze_ts_segment(
    seg_path: Path,
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    exclusions: Sequence[Any] = (),
    rotate: int = 0,
    fallback_dur_ms: int = 2000,
    tilt: float = 0.0,
) -> tuple[int, float]:
    """Read sampled frames from an MPEG-TS segment to get `(duration_ms, motion_energy)`."""
    dur_ms, raw_frames = extract_ts_sampled_frames(seg_path, fallback_dur_ms=fallback_dur_ms)
    if not raw_frames:
        return dur_ms, 0.0
    rot = normalize_rotate(rotate)
    tlt = normalize_tilt(tilt)
    frames = [apply_frame_transform(frm, rotate=rot, tilt=tlt, apply_crop=False) for frm in raw_frames]
    score = compute_motion_energy_from_frames(frames, roi=roi, exclusions=exclusions)
    return dur_ms, score


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
    camera: str | None = None,
    resolve_overlap: bool = True,
    min_raw_dur_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Group active motion samples into padded bursts, optionally isolated to one camera and min duration."""
    cam_q = camera.strip().lower() if camera and camera.strip() else None
    eff_merge_ms = max(merge_gap_ms, 6000) if min_dur_ms >= 30_000 else merge_gap_ms
    eff_min_raw = (
        min_raw_dur_ms
        if min_raw_dur_ms is not None
        else (max(4000, min_dur_ms - 10_000) if min_dur_ms >= 30_000 else min_dur_ms)
    )

    by_cam: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        if cam_q is not None:
            s_cam = str(s.get("camera", "")).strip().lower()
            s_slug = str(s.get("slug", "")).strip().lower()
            if s_cam != cam_q and s_slug != cam_q:
                continue
        if float(s.get("energy", 0.0)) >= threshold:
            by_cam.setdefault(str(s.get("camera", camera or "Rack Cam")), []).append(s)

    cam_bursts: list[dict[str, Any]] = []
    for cam, items in by_cam.items():
        ordered = sorted(items, key=lambda x: int(x["startMs"]))
        groups: list[list[dict[str, Any]]] = []
        for it in ordered:
            if not groups:
                groups.append([it])
                continue
            prev_end = int(groups[-1][-1]["endMs"])
            if int(it["startMs"]) - prev_end <= eff_merge_ms:
                groups[-1].append(it)
            else:
                groups.append([it])

        for grp in groups:
            raw_start = int(grp[0]["startMs"])
            raw_end = int(grp[-1]["endMs"])
            if raw_end - raw_start < eff_min_raw:
                continue
            b_start = max(0, raw_start - pad_before_ms)
            b_end = raw_end + pad_after_ms
            if b_end - b_start < min_dur_ms:
                continue
            energies = [float(x["energy"]) for x in grp]
            mean_e = sum(energies) / max(1, len(energies))
            peak_e = max(energies)
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
                    "note": f"Motion peak {peak_e:.1f}" if cam_q or len(by_cam) <= 1 else f"Auto-picked {cam} (motion {peak_e:.1f})",
                }
            )

    if not resolve_overlap or cam_q is not None:
        return sorted(cam_bursts, key=lambda b: int(b["startMs"]))

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
    primary_cameras: set[str] | None = None,
) -> str:
    """Pick explicit `preferred_camera` or the primary camera with strongest motion inside `[start_ms, end_ms]`."""
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
    if primary_cameras:
        prim_totals = {k: v for k, v in totals.items() if k in primary_cameras}
        if prim_totals:
            totals = prim_totals
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
        picked = before_tap[-1] if before_tap else candidates[-1]
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
    primary: bool = True
    motion_threshold: float = DEFAULT_MOTION_THRESHOLD
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    exclusions: list[Quad] = field(default_factory=list)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    rotate: int = 0
    tilt: float = 0.0
    source_slug: str = ""

    def effective_roi(self) -> tuple[float, float, float, float]:
        if self.roi != (0.0, 0.0, 1.0, 1.0):
            return self.roi
        return self.crop

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "rtspUrl": self.rtsp_url,
            "enabled": self.enabled,
            "primary": self.primary,
            "motionThreshold": self.motion_threshold,
            "roi": list(self.roi),
            "exclusions": [[[pt[0], pt[1]] for pt in q] for q in self.exclusions],
            "crop": list(self.crop),
            "rotate": self.rotate,
            "tilt": self.tilt,
            "sourceSlug": self.source_slug,
        }


@dataclass
class _LiveChannel:
    slug: str
    cond: threading.Condition = field(default_factory=threading.Condition)
    seq: int = 0
    latest_jpeg: bytes = b""
    latest_ts: float = 0.0
    stop_flag: bool = False


class LiveStreamHub:
    """Thread-safe per-camera frame broadcaster fed directly by the single RTSP capture worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, _LiveChannel] = {}

    def stop_all(self) -> None:
        with self._lock:
            slugs = list(self._channels.keys())
        for slug in slugs:
            self.stop_camera(slug)

    def stop_camera(self, slug: str) -> None:
        with self._lock:
            ch = self._channels.pop(slug, None)
        if not ch:
            return
        with ch.cond:
            ch.stop_flag = True
            ch.cond.notify_all()

    def get_channel(self, slug: str) -> _LiveChannel:
        with self._lock:
            ch = self._channels.get(slug)
            if not ch:
                ch = _LiveChannel(slug=slug)
                self._channels[slug] = ch
            return ch

    def publish_frame(self, slug: str, jpg: bytes) -> None:
        ch = self.get_channel(slug)
        with ch.cond:
            ch.latest_jpeg = jpg
            ch.latest_ts = time.time()
            ch.seq += 1
            ch.cond.notify_all()

    def recent_frame(self, slug: str, max_age_sec: float = 2.0) -> bytes | None:
        with self._lock:
            ch = self._channels.get(slug)
        if not ch:
            return None
        with ch.cond:
            if ch.latest_jpeg and (time.time() - ch.latest_ts) <= max_age_sec:
                return ch.latest_jpeg
        return None


@dataclass
class WallboardConfig:
    enabled: bool = True
    fire_tv_host: str = ""
    adb_port: int = DEFAULT_ADB_PORT
    display_client: str = "silk"
    custom_component: str = ""
    wallboard_url: str = ""
    wake_on_start: bool = True
    sleep_on_stop: bool = True
    idle_sleep_sec: int = DEFAULT_IDLE_SLEEP_SEC
    suppress_screensaver: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "WallboardConfig":
        if not isinstance(raw, dict):
            return cls()
        client = str(raw.get("displayClient", "silk")).strip().lower()
        if client not in ("silk", "fully", "custom"):
            client = "silk"
        try:
            adb_port = max(1, min(65535, int(raw.get("adbPort", DEFAULT_ADB_PORT))))
        except (TypeError, ValueError):
            adb_port = DEFAULT_ADB_PORT
        try:
            idle_sec = max(60, min(7200, int(raw.get("idleSleepSec", DEFAULT_IDLE_SLEEP_SEC))))
        except (TypeError, ValueError):
            idle_sec = DEFAULT_IDLE_SLEEP_SEC
        return cls(
            enabled=bool(raw.get("enabled", True)),
            fire_tv_host=str(raw.get("fireTvHost", "")).strip(),
            adb_port=adb_port,
            display_client=client,
            custom_component=str(raw.get("customComponent", "")).strip(),
            wallboard_url=str(raw.get("wallboardUrl", "")).strip(),
            wake_on_start=bool(raw.get("wakeOnStart", True)),
            sleep_on_stop=bool(raw.get("sleepOnStop", True)),
            idle_sleep_sec=idle_sec,
            suppress_screensaver=bool(raw.get("suppressScreensaver", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fireTvHost": self.fire_tv_host,
            "adbPort": self.adb_port,
            "displayClient": self.display_client,
            "customComponent": self.custom_component,
            "wallboardUrl": self.wallboard_url,
            "wakeOnStart": self.wake_on_start,
            "sleepOnStop": self.sleep_on_stop,
            "idleSleepSec": self.idle_sleep_sec,
            "suppressScreensaver": self.suppress_screensaver,
        }


class FireTvAdbController:
    """Manages ADB-over-Wi-Fi wake, Silk/kiosk launch, screensaver suppression, and auto-sleep."""

    def __init__(
        self,
        adb_bin: str | None = None,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        settle_sec: float = 0.35,
    ) -> None:
        self.adb_bin = adb_bin if adb_bin is not None else resolve_adb_bin()
        self._runner = runner or self._default_runner
        self.settle_sec = max(0.0, float(settle_sec))
        self.lock = threading.RLock()
        self.adb_state = "unconfigured"
        self.last_model = ""
        self.last_action = ""
        self.last_error = ""
        self.last_wake_ms = 0
        self.tv_awake_requested = False
        self.pending_sleep_at_ms: int | None = None
        self.pending_sleep_reason = ""

    @staticmethod
    def _default_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                **_subprocess_no_window_kwargs(),
            )
            return res.returncode, res.stdout or "", res.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"ADB command timed out after {timeout:.0f}s"
        except Exception as exc:
            return 1, "", str(exc)

    def _run(self, args: list[str], timeout: float = 6.0) -> tuple[int, str, str]:
        if not self.adb_bin:
            return 127, "", "adb.exe not found on OptiPlex"
        return self._runner([self.adb_bin, *args], timeout)

    @staticmethod
    def serial(cfg: WallboardConfig) -> str:
        host = cfg.fire_tv_host.strip()
        if not host:
            return ""
        if ":" in host:
            return host
        return f"{host}:{cfg.adb_port}"

    def _connect_and_status(self, serial: str) -> tuple[str, str]:
        rc_c, out_c, err_c = self._run(["connect", serial], timeout=6.0)
        rc_d, out_d, err_d = self._run(["devices"], timeout=5.0)
        combined_c = f"{out_c} {err_c}".strip()
        for line in out_d.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == serial:
                st = parts[1].lower()
                if st in ("device", "unauthorized"):
                    return st, combined_c
                return "offline", f"{line.strip()} ({combined_c})".strip()
        if "failed to authenticate" in combined_c.lower() or "unauthorized" in combined_c.lower():
            return "unauthorized", combined_c
        detail = combined_c or err_d.strip() or out_d.strip() or f"rc={rc_c}/{rc_d}"
        return "offline", detail

    def connect_and_probe(self, cfg: WallboardConfig) -> dict[str, Any]:
        with self.lock:
            ser = self.serial(cfg)
            if not ser:
                self.adb_state = "unconfigured"
                self.last_error = ""
                return {
                    "ok": False,
                    "configured": False,
                    "state": "unconfigured",
                    "serial": "",
                    "model": "",
                    "message": 'Enter the 32" Fire TV IP address first.',
                }
            st, detail = self._connect_and_status(ser)
            if st == "device":
                rc_m, out_m, _ = self._run(["-s", ser, "shell", "getprop", "ro.product.model"], timeout=4.0)
                model = out_m.strip().splitlines()[0] if rc_m == 0 and out_m.strip() else ""
                self.adb_state = "ready"
                if model:
                    self.last_model = model
                self.last_error = ""
                self.last_action = f"probe ({ser})"
                return {
                    "ok": True,
                    "configured": True,
                    "state": "ready",
                    "serial": ser,
                    "model": self.last_model,
                    "message": f"Connected ({self.last_model or ser})",
                }
            if st == "unauthorized":
                self.adb_state = "unauthorized"
                msg = "Click 'Always allow from this computer' on the 32\" Fire TV screen, then retry."
                self.last_error = msg
                return {
                    "ok": False,
                    "configured": True,
                    "state": "unauthorized",
                    "serial": ser,
                    "model": "",
                    "message": msg,
                }
            self.adb_state = "offline"
            msg = f"Fire TV unreachable at {ser}: {detail}"
            self.last_error = msg
            return {
                "ok": False,
                "configured": True,
                "state": "offline",
                "serial": ser,
                "model": "",
                "message": msg,
            }

    @staticmethod
    def _launch_cmd(cfg: WallboardConfig, wallboard_url: str, ser: str) -> list[str]:
        if cfg.display_client == "fully":
            return [
                "-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                "-d", wallboard_url, "-n", "de.ozerov.fully/.MainActivity",
            ]
        if cfg.display_client == "custom":
            if cfg.custom_component.strip():
                return [
                    "-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", wallboard_url, "-n", cfg.custom_component.strip(),
                ]
            return ["-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", wallboard_url]
        return [
            "-s", ser, "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
            "-d", wallboard_url, "-n", "com.amazon.cloud9/.BrowserActivity",
        ]

    def wake_and_launch(self, cfg: WallboardConfig, wallboard_url: str, force: bool = False) -> dict[str, Any]:
        with self.lock:
            self.pending_sleep_at_ms = None
            self.pending_sleep_reason = ""
            ser = self.serial(cfg)
            if not cfg.enabled or not ser:
                self.adb_state = "disabled" if not cfg.enabled else "unconfigured"
                self.last_error = ""
                return {
                    "ok": False,
                    "configured": bool(ser),
                    "state": self.adb_state,
                    "serial": ser,
                    "model": self.last_model,
                    "message": "Fire TV IP not configured yet" if not ser else "Wallboard ADB disabled",
                }
            now_ms = int(time.time() * 1000)
            if self.tv_awake_requested and not force and (now_ms - self.last_wake_ms) < 20_000:
                return {
                    "ok": True,
                    "skipped": True,
                    "configured": True,
                    "state": self.adb_state,
                    "serial": ser,
                    "model": self.last_model,
                    "message": "Fire TV wake already active",
                }
            st, detail = self._connect_and_status(ser)
            if st == "unauthorized":
                self.adb_state = "unauthorized"
                msg = "Click 'Always allow from this computer' on the 32\" Fire TV screen, then retry."
                self.last_error = msg
                return {"ok": False, "configured": True, "state": "unauthorized", "serial": ser, "message": msg}
            if st != "device":
                self.adb_state = "offline"
                msg = f"Fire TV unreachable at {ser}: {detail}"
                self.last_error = msg
                return {"ok": False, "configured": True, "state": "offline", "serial": ser, "message": msg}

            rc_w, out_w, err_w = self._run(["-s", ser, "shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=5.0)
            if rc_w != 0:
                self.adb_state = "offline"
                msg = f"Wake keyevent failed: {(err_w or out_w).strip()}"
                self.last_error = msg
                return {"ok": False, "configured": True, "state": "offline", "serial": ser, "message": msg}

            if self.settle_sec > 0:
                time.sleep(self.settle_sec)

            if cfg.suppress_screensaver:
                self._run(["-s", ser, "shell", "settings", "put", "secure", "screensaver_enabled", "0"], timeout=4.0)
                self._run(["-s", ser, "shell", "settings", "put", "secure", "sleep_timeout", "0"], timeout=4.0)

            rc_l, out_l, err_l = self._run(self._launch_cmd(cfg, wallboard_url, ser), timeout=8.0)
            if cfg.display_client == "silk" and (rc_l != 0 or "Error" in f"{out_l}\n{err_l}"):
                rc_l, out_l, err_l = self._run(
                    ["-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", wallboard_url, "-p", "com.amazon.cloud9"],
                    timeout=8.0,
                )

            if rc_l != 0:
                self.adb_state = "ready"
                msg = f"Awoke TV but browser launch failed: {(err_l or out_l).strip()}"
                self.last_error = msg
                return {"ok": False, "configured": True, "state": "ready", "serial": ser, "message": msg}

            self.tv_awake_requested = True
            self.last_wake_ms = int(time.time() * 1000)
            self.adb_state = "awake"
            self.last_action = f"wake -> {cfg.display_client}"
            self.last_error = ""
            return {
                "ok": True,
                "configured": True,
                "state": "awake",
                "serial": ser,
                "model": self.last_model,
                "message": f"Awoke {self.last_model or ser} & opened {cfg.display_client}",
            }

    def schedule_sleep(self, cfg: WallboardConfig, delay_sec: float = 0.0, reason: str = "") -> dict[str, Any]:
        if delay_sec <= 0:
            return self.sleep_now(cfg, reason=reason)
        with self.lock:
            self.pending_sleep_at_ms = int(time.time() * 1000) + int(delay_sec * 1000)
            self.pending_sleep_reason = reason or "workout_end"
            return {
                "ok": True,
                "scheduledDelaySec": delay_sec,
                "reason": self.pending_sleep_reason,
                "message": f"Sleep scheduled in {int(delay_sec)}s",
            }

    def sleep_now(self, cfg: WallboardConfig, reason: str = "") -> dict[str, Any]:
        with self.lock:
            self.pending_sleep_at_ms = None
            self.pending_sleep_reason = ""
            self.tv_awake_requested = False
            ser = self.serial(cfg)
            if not ser:
                self.adb_state = "unconfigured"
                self.last_action = f"sleep skipped ({reason or 'unconfigured'})"
                return {
                    "ok": False,
                    "configured": False,
                    "state": "unconfigured",
                    "serial": "",
                    "message": "Fire TV IP not configured yet",
                }
            st, detail = self._connect_and_status(ser)
            if st != "device":
                self.adb_state = st
                msg = f"Sleep failed ({st}): {detail}"
                self.last_error = msg
                return {"ok": False, "configured": True, "state": st, "serial": ser, "message": msg}
            if cfg.suppress_screensaver:
                self._run(["-s", ser, "shell", "settings", "put", "secure", "screensaver_enabled", "1"], timeout=4.0)
                self._run(["-s", ser, "shell", "settings", "put", "secure", "sleep_timeout", "1200000"], timeout=4.0)
            self._run(["-s", ser, "shell", "input", "keyevent", "KEYCODE_SLEEP"], timeout=5.0)
            self.adb_state = "asleep"
            self.last_action = f"sleep ({reason or 'manual'})"
            self.last_error = ""
            return {
                "ok": True,
                "configured": True,
                "state": "asleep",
                "serial": ser,
                "model": self.last_model,
                "message": f"Slept {self.last_model or ser}",
            }

    def check_timers(
        self,
        cfg: WallboardConfig,
        last_active_push_ms: int,
        workout_active: bool,
        now_ms: int | None = None,
    ) -> dict[str, Any] | None:
        cur_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        reason_to_sleep: str | None = None
        with self.lock:
            if self.pending_sleep_at_ms is not None and cur_ms >= self.pending_sleep_at_ms:
                if cfg.sleep_on_stop:
                    reason_to_sleep = self.pending_sleep_reason or "workout_end"
                else:
                    self.pending_sleep_at_ms = None
                    self.pending_sleep_reason = ""
            elif (
                self.tv_awake_requested
                and cfg.idle_sleep_sec > 0
                and last_active_push_ms > 0
                and (cur_ms - last_active_push_ms) >= cfg.idle_sleep_sec * 1000
            ):
                reason_to_sleep = "idle_timeout"
        if reason_to_sleep:
            return self.sleep_now(cfg, reason=reason_to_sleep)
        return None

    def status_dict(self, cfg: WallboardConfig) -> dict[str, Any]:
        with self.lock:
            now_ms = int(time.time() * 1000)
            pending_sec = (
                max(0, int((self.pending_sleep_at_ms - now_ms) / 1000))
                if self.pending_sleep_at_ms is not None
                else 0
            )
            return {
                "adbBinFound": bool(self.adb_bin),
                "adbBin": self.adb_bin or "",
                "adbState": self.adb_state,
                "fireTvHost": cfg.fire_tv_host,
                "adbPort": cfg.adb_port,
                "fireTvModel": self.last_model,
                "tvAwake": self.tv_awake_requested,
                "lastAction": self.last_action,
                "lastError": self.last_error,
                "pendingSleepSec": pending_sec,
            }


class WallboardRelay:
    """Thread-safe OptiPlex mirror of Pixel's live Wallboard state with SSE fan-out."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.cache_html_path = root_dir / "wallboard_cache.html"
        self.repo_page_ts_path = Path(__file__).resolve().parents[2] / "src" / "wallboard" / "page.ts"
        self.cond = threading.Condition()
        self.seq = 0
        self.latest_json = ""
        self.mode = "standby"
        self.workout_active = False
        self.last_push_ms = 0
        self.last_active_push_ms = 0
        self.source_url = ""
        self.sse_clients = 0

    def get_html(self) -> str:
        repo_html: str | None = None
        repo_mtime = 0.0
        if self.repo_page_ts_path.is_file():
            repo_html = extract_wallboard_html_from_ts(self.repo_page_ts_path)
            if repo_html:
                repo_mtime = self.repo_page_ts_path.stat().st_mtime

        cache_html: str | None = None
        cache_mtime = 0.0
        if self.cache_html_path.is_file():
            try:
                txt = self.cache_html_path.read_text(encoding="utf-8")
                if "<!doctype html" in txt[:200].lower():
                    cache_html = txt
                    cache_mtime = self.cache_html_path.stat().st_mtime
            except Exception:
                pass

        if repo_html and cache_html:
            return repo_html if repo_mtime >= cache_mtime else cache_html
        if repo_html:
            return repo_html
        if cache_html:
            return cache_html
        return FALLBACK_WALLBOARD_HTML

    def save_uploaded_html(self, html: str) -> bool:
        if not isinstance(html, str) or "<!doctype html" not in html[:200].lower():
            return False
        tmp = self.cache_html_path.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(self.cache_html_path)
        return True

    @staticmethod
    def standby_state_dict(now_ms: int | None = None) -> dict[str, Any]:
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        dt = datetime.fromtimestamp(ts / 1000.0)
        hour12 = dt.strftime("%I").lstrip("0") or "12"
        return {
            "ts": ts,
            "mode": "standby",
            "hr": {
                "bpm": 0,
                "fresh": False,
                "zone": 0,
                "pctMax": 0,
                "readyLimitBpm": 130,
                "drop60": 0,
                "readiness": "idle",
            },
            "voice": {
                "enabled": False,
                "listening": False,
                "armed": False,
                "wakeWord": "armory",
                "banner": None,
            },
            "standby": {
                "dateLabel": dt.strftime("%a, %b ") + str(dt.day),
                "timeLabel": f"{hour12}:{dt.strftime('%M %p')}",
                "weekLabel": "OptiPlex Wallboard Relay · Standby",
                "todayItems": [],
            },
        }

    def current_state_json(self, stale_ms: int = 900_000) -> str:
        with self.cond:
            now_ms = int(time.time() * 1000)
            if self.latest_json and self.last_push_ms > 0 and (now_ms - self.last_push_ms) <= stale_ms:
                return self.latest_json
        return json.dumps(self.standby_state_dict(), separators=(",", ":"))

    def update_state(
        self,
        state_obj: dict[str, Any] | None,
        workout_active: bool,
        source_url: str = "",
    ) -> None:
        now_ms = int(time.time() * 1000)
        payload = state_obj if isinstance(state_obj, dict) else self.standby_state_dict(now_ms)
        compact = json.dumps(payload, separators=(",", ":")).replace("\r", " ").replace("\n", " ")
        with self.cond:
            self.latest_json = compact
            self.workout_active = bool(workout_active)
            self.mode = str(payload.get("mode") or ("lift" if workout_active else "standby"))
            self.last_push_ms = now_ms
            if workout_active:
                self.last_active_push_ms = now_ms
            if source_url and source_url.strip():
                self.source_url = source_url.strip().rstrip("/")
            self.seq += 1
            self.cond.notify_all()

    def mark_active_wake(self, state_obj: dict[str, Any] | None = None, source_url: str = "") -> None:
        now_ms = int(time.time() * 1000)
        if isinstance(state_obj, dict):
            self.update_state(state_obj, workout_active=True, source_url=source_url)
            return
        with self.cond:
            self.workout_active = True
            self.last_push_ms = now_ms
            self.last_active_push_ms = now_ms
            if source_url and source_url.strip():
                self.source_url = source_url.strip().rstrip("/")

    def status_dict(self) -> dict[str, Any]:
        with self.cond:
            return {
                "clients": self.sse_clients,
                "mode": self.mode,
                "workoutActive": self.workout_active,
                "lastPushMs": self.last_push_ms,
                "lastActivePushMs": self.last_active_push_ms,
            }


class ArmoryCamService:
    """Core DVR manager: config persistence, FFmpeg RTSP ring capture, motion indexing & clip cutting."""

    def __init__(
        self,
        root_dir: Path | None = None,
        adb_controller: FireTvAdbController | None = None,
        drive_backup_dir: str | Path | None = None,
        ntfy_topic: str | None = None,
    ) -> None:
        self.root = (root_dir or default_root_dir()).resolve()
        self.ring_root = self.root / "ring"
        self.tapes_root = self.root / "tapes"
        self.clips_root = self.root / "clips"
        self.previews_root = self.root / "previews"
        self.thumbs_root = self.root / "thumbs"
        self.logs_root = self.root / "logs"
        self.config_path = self.root / "config.json"
        self.clips_index_path = self.clips_root / "index.jsonl"
        self.cal_state_path = self.root / "calibration_state.json"
        for d in (
            self.root,
            self.ring_root,
            self.tapes_root,
            self.clips_root,
            self.previews_root,
            self.thumbs_root,
            self.logs_root,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        self.ffprobe = resolve_tool_bin("ffprobe", "ARMORY_CAM_FFPROBE")
        self.log = logging.getLogger("armory-cam")
        self.live_hub = LiveStreamHub()
        self.wallboard_cfg = WallboardConfig()
        self.wallboard = WallboardRelay(self.root)
        self.adb = adb_controller or FireTvAdbController()

        self.lock = threading.RLock()
        self.port = DEFAULT_PORT
        self.retention_days = DEFAULT_RETENTION_DAYS
        self.pre_roll_sec = DEFAULT_PRE_ROLL_SEC
        if drive_backup_dir is not None:
            self.drive_backup_dir = str(drive_backup_dir).strip()
        elif root_dir is None:
            self.drive_backup_dir = default_drive_backup_dir()
        else:
            self.drive_backup_dir = ""
        if ntfy_topic is not None:
            self.ntfy_topic = ntfy_topic.strip()
        elif root_dir is None:
            self.ntfy_topic = DEFAULT_NTFY_TOPIC
        else:
            self.ntfy_topic = ""
        self.cameras: list[CameraConfig] = []

        self.active_session_id: str | None = None
        self.active_session_name: str = ""
        self.recording: bool = False
        self.paused: bool = False

        self.ring_segments: dict[str, list[SegmentInfo]] = {}
        self.seen_files: set[str] = set()
        self.latest_energy: dict[str, float] = {}
        self.cam_connected: dict[str, bool] = {}
        self._thumb_lock = threading.Lock()
        self._thumb_mem: OrderedDict[str, bytes] = OrderedDict()
        self._preview_locks_guard = threading.Lock()
        self._preview_locks: dict[str, threading.Lock] = {}

        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._proc_urls: dict[str, str] = {}
        self._worker_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._started = False

        self.load_or_init_config()
        self._rehydrate_ring_index()

    @staticmethod
    def _build_camera_configs(
        raw_items: list[dict[str, Any]],
        prev_by_slug: dict[str, CameraConfig] | None = None,
    ) -> list[CameraConfig]:
        used_slugs: set[str] = set()
        cams: list[CameraConfig] = []
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip() or f"Camera {idx + 1}"
            base_slug = safe_slug(str(item.get("slug") or name).lower(), f"cam_{idx + 1}")
            slug = base_slug
            suffix = 2
            while slug in used_slugs:
                slug = f"{base_slug[:40]}_{suffix}"
                suffix += 1
            used_slugs.add(slug)

            old = prev_by_slug.get(slug) if prev_by_slug else None
            raw_src = str(
                item["sourceSlug"] if "sourceSlug" in item else (old.source_slug if old else "")
            ).strip()
            src_slug = safe_slug(raw_src.lower(), "") if raw_src else ""
            rtsp_url = str(item["rtspUrl"]).strip() if "rtspUrl" in item else (old.rtsp_url if old else "")
            enabled = bool(item["enabled"]) if "enabled" in item else (old.enabled if old else True)
            primary = bool(item["primary"]) if "primary" in item else (old.primary if old else True)
            thresh = (
                float(item["motionThreshold"])
                if "motionThreshold" in item
                else (old.motion_threshold if old else DEFAULT_MOTION_THRESHOLD)
            )
            roi = normalize_crop(item["roi"]) if "roi" in item else (old.roi if old else (0.0, 0.0, 1.0, 1.0))
            exclusions = (
                normalize_exclusions(item["exclusions"])
                if "exclusions" in item
                else (list(old.exclusions) if old else [])
            )
            crop = normalize_crop(item["crop"]) if "crop" in item else (old.crop if old else (0.0, 0.0, 1.0, 1.0))
            rotate = normalize_rotate(item["rotate"]) if "rotate" in item else (old.rotate if old else 0)
            tilt = normalize_tilt(item["tilt"]) if "tilt" in item else (old.tilt if old else 0.0)
            cams.append(
                CameraConfig(
                    name=name,
                    slug=slug,
                    rtsp_url=rtsp_url,
                    enabled=enabled,
                    primary=primary,
                    motion_threshold=thresh,
                    roi=roi,
                    exclusions=exclusions,
                    crop=crop,
                    rotate=rotate,
                    tilt=tilt,
                    source_slug=src_slug,
                )
            )

        by_slug = {c.slug: c for c in cams}
        for c in cams:
            if not c.source_slug or c.source_slug == c.slug or c.source_slug not in by_slug:
                c.source_slug = ""
                continue
            cur_slug = c.source_slug
            seen = {c.slug}
            for _ in range(3):
                parent = by_slug.get(cur_slug)
                if not parent or not parent.source_slug or parent.source_slug in seen or parent.source_slug not in by_slug:
                    break
                seen.add(cur_slug)
                cur_slug = parent.source_slug
            if cur_slug == c.slug or by_slug[cur_slug].source_slug:
                c.source_slug = ""
            else:
                c.source_slug = cur_slug
                c.rtsp_url = ""
        return cams

    def _source_slug_for(self, cam: CameraConfig) -> str:
        if not cam.source_slug or cam.source_slug == cam.slug:
            return cam.slug
        by_slug = {c.slug: c for c in self.cameras}
        cur = cam.source_slug
        seen = {cam.slug}
        for _ in range(3):
            parent = by_slug.get(cur)
            if not parent:
                return cam.slug
            if not parent.source_slug or parent.source_slug in seen:
                return parent.slug
            seen.add(cur)
            cur = parent.source_slug
        return cam.slug

    def _is_virtual(self, cam: CameraConfig) -> bool:
        return self._source_slug_for(cam) != cam.slug

    def _source_camera(self, cam: CameraConfig) -> CameraConfig:
        src_slug = self._source_slug_for(cam)
        for c in self.cameras:
            if c.slug == src_slug:
                return c
        return cam

    def _views_for_source(self, source_slug: str, enabled_only: bool = True) -> list[CameraConfig]:
        out: list[CameraConfig] = []
        for c in self.cameras:
            if self._source_slug_for(c) == source_slug and (not enabled_only or c.enabled):
                out.append(c)
        return out

    def _physical_cameras(self) -> list[CameraConfig]:
        return [c for c in self.cameras if not self._is_virtual(c)]

    def load_or_init_config(self) -> None:
        default_cams = [
            CameraConfig(name="Rack Cam", slug="rack_cam"),
            CameraConfig(name="Smith Cam", slug="smith_cam"),
        ]
        if not self.config_path.exists():
            self.cameras = default_cams
            self.wallboard_cfg = WallboardConfig()
            self.save_config()
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.port = int(raw.get("port", DEFAULT_PORT))
            self.retention_days = max(1, int(raw.get("retentionDays", DEFAULT_RETENTION_DAYS)))
            self.pre_roll_sec = max(30, int(raw.get("preRollSec", DEFAULT_PRE_ROLL_SEC)))
            if "driveBackupDir" in raw:
                self.drive_backup_dir = str(raw.get("driveBackupDir") or "").strip()
            self.wallboard_cfg = WallboardConfig.from_dict(raw.get("wallboard"))
            raw_cams = [item for item in raw.get("cameras", []) if isinstance(item, dict) and str(item.get("name", "")).strip()]
            loaded = self._build_camera_configs(raw_cams) if raw_cams else []
            self.cameras = loaded or default_cams
        except Exception as exc:
            self.log.warning("Failed parsing config.json (%s); using defaults", exc)
            self.cameras = default_cams

    def save_config(self) -> None:
        payload = {
            "port": self.port,
            "retentionDays": self.retention_days,
            "preRollSec": self.pre_roll_sec,
            "driveBackupDir": self.drive_backup_dir,
            "cameras": [c.to_dict() for c in self.cameras],
            "wallboard": self.wallboard_cfg.to_dict(),
        }
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def effective_wallboard_url(self) -> str:
        with self.lock:
            explicit = self.wallboard_cfg.wallboard_url.strip()
            port = self.port
        if explicit:
            return explicit
        return f"http://{detect_lan_ip()}:{port}/wallboard"

    def trigger_adb_wake_async(self, force: bool = False) -> None:
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
            url = self.effective_wallboard_url()

        def _worker() -> None:
            try:
                res = self.adb.wake_and_launch(cfg, url, force=force)
                if not res.get("skipped"):
                    self.log.info("Wallboard ADB wake: %s", res.get("message", res))
            except Exception as exc:
                self.adb.last_error = str(exc)

        threading.Thread(target=_worker, name="wallboard-adb-wake", daemon=True).start()

    def trigger_adb_sleep_async(self, reason: str = "", delay_sec: float = 0.0) -> None:
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())

        def _worker() -> None:
            try:
                res = self.adb.schedule_sleep(cfg, delay_sec=delay_sec, reason=reason)
                self.log.info("Wallboard ADB sleep: %s", res.get("message", res))
            except Exception as exc:
                self.adb.last_error = str(exc)

        threading.Thread(target=_worker, name="wallboard-adb-sleep", daemon=True).start()

    def update_config(self, raw: dict[str, Any]) -> None:
        with self.lock:
            prev_by_slug = {c.slug: c for c in self.cameras}
            if "retentionDays" in raw:
                self.retention_days = max(1, min(90, int(raw["retentionDays"])))
            if "preRollSec" in raw:
                self.pre_roll_sec = max(30, min(600, int(raw["preRollSec"])))
            drive_changed = False
            if "driveBackupDir" in raw:
                next_dir = str(raw.get("driveBackupDir") or "").strip()
                drive_changed = next_dir != self.drive_backup_dir and bool(next_dir)
                self.drive_backup_dir = next_dir
            if isinstance(raw.get("wallboard"), dict):
                merged = {**self.wallboard_cfg.to_dict(), **raw["wallboard"]}
                self.wallboard_cfg = WallboardConfig.from_dict(merged)
            if isinstance(raw.get("cameras"), list):
                next_cams = self._build_camera_configs(raw["cameras"], prev_by_slug=prev_by_slug)
                if next_cams:
                    self.cameras = next_cams
            for cam in self.cameras:
                media_slug = self._source_slug_for(cam)
                if media_slug != cam.slug and not self.ring_segments.get(cam.slug):
                    src_segs = self.ring_segments.get(media_slug, [])
                    if src_segs:
                        self.ring_segments[cam.slug] = [
                            SegmentInfo(
                                path=s.path,
                                start_ms=s.start_ms,
                                end_ms=s.end_ms,
                                energy=0.0,
                                camera=cam.name,
                            )
                            for s in src_segs
                        ]
            self.save_config()
            for slug, old in prev_by_slug.items():
                cur = next((c for c in self.cameras if c.slug == slug), None)
                has_enabled_views = bool(cur and not self._is_virtual(cur) and self._views_for_source(slug, enabled_only=True))
                if not cur or self._is_virtual(cur) or not has_enabled_views or cur.rtsp_url != old.rtsp_url:
                    self.live_hub.stop_camera(slug)
            if self._started:
                self._restart_capture_workers()
                if drive_changed:
                    threading.Thread(target=self.backfill_drive_clips, name="drive-clip-backfill", daemon=True).start()

    def _rehydrate_ring_index(self) -> None:
        phys_segs: dict[str, list[SegmentInfo]] = {}
        for phys in self._physical_cameras():
            cdir = self.ring_root / phys.slug
            cdir.mkdir(parents=True, exist_ok=True)
            segs: list[SegmentInfo] = []
            for p in sorted(cdir.glob("seg_*.ts")):
                start_ms = parse_segment_start_ms(p.name)
                if start_ms is None:
                    continue
                self.seen_files.add(str(p))
                segs.append(SegmentInfo(path=p, start_ms=start_ms, end_ms=start_ms + 2000, energy=0.0, camera=phys.name))
            phys_segs[phys.slug] = segs
        for cam in self.cameras:
            src_slug = self._source_slug_for(cam)
            base_segs = phys_segs.get(src_slug, [])
            self.ring_segments[cam.slug] = [
                SegmentInfo(path=s.path, start_ms=s.start_ms, end_ms=s.end_ms, energy=0.0, camera=cam.name)
                for s in base_segs
            ]

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
            backfill_t = threading.Thread(target=self.backfill_drive_clips, name="drive-clip-backfill", daemon=True)
            backfill_t.start()
            self._worker_threads.append(backfill_t)

    def stop_background(self) -> None:
        self._stop_event.set()
        self.live_hub.stop_all()
        with self.lock:
            for slug in list(self._procs.keys()):
                self._stop_proc(slug)
            self._started = False

    def _stop_proc(self, slug: str) -> None:
        self._proc_urls.pop(slug, None)
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
        phys_by_slug = {c.slug: c for c in self._physical_cameras()}
        for slug in list(self._procs.keys()):
            cur = phys_by_slug.get(slug)
            has_views = bool(cur and self._views_for_source(slug, enabled_only=True))
            if not cur or not has_views or not cur.rtsp_url or self._proc_urls.get(slug) != cur.rtsp_url:
                self._stop_proc(slug)
        for cam in self.cameras:
            self.ring_segments.setdefault(cam.slug, [])
        for cam in self._physical_cameras():
            (self.ring_root / cam.slug).mkdir(parents=True, exist_ok=True)
            has_views = bool(self._views_for_source(cam.slug, enabled_only=True))
            if has_views and cam.rtsp_url:
                existing = self._procs.get(cam.slug)
                if existing and existing.poll() is None and self._proc_urls.get(cam.slug) == cam.rtsp_url:
                    continue
                self.cam_connected[cam.slug] = False
                self._proc_urls[cam.slug] = cam.rtsp_url
                t = threading.Thread(
                    target=self._rtsp_capture_loop,
                    args=(cam,),
                    name=f"rtsp-{cam.slug}",
                    daemon=True,
                )
                t.start()
                self._worker_threads.append(t)
            else:
                self.cam_connected[cam.slug] = False

    def _rtsp_capture_loop(self, cam: CameraConfig) -> None:
        out_dir = self.ring_root / cam.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / "seg_%Y%m%d_%H%M%S.ts")
        backoff = 2.0
        while not self._stop_event.is_set():
            with self.lock:
                cur = next((c for c in self.cameras if c.slug == cam.slug), None)
                has_views = bool(cur and not self._is_virtual(cur) and self._views_for_source(cam.slug, enabled_only=True))
                if not cur or not has_views or cur.rtsp_url != cam.rtsp_url or not self._started:
                    return
            # Single RTSP connection outputting both 2s MPEG-TS ring segments AND 10fps MJPEG preview frames.
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "10000000",
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
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "fps=10,scale=960:-2",
                "-q:v",
                "7",
                "-f",
                "mpjpeg",
                "pipe:1",
            ]
            t_start = time.time()
            proc = None
            err_lines: list[str] = []
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                with self.lock:
                    self._procs[cam.slug] = proc

                def drain_stderr(pipe=proc.stderr, sink=err_lines):
                    if not pipe:
                        return
                    try:
                        for line in pipe:
                            txt = line.decode("utf-8", errors="ignore").strip()
                            if txt:
                                sink.append(txt)
                                if len(sink) > 6:
                                    del sink[:-6]
                    except Exception:
                        pass

                err_t = threading.Thread(target=drain_stderr, daemon=True)
                err_t.start()

                buf = bytearray()
                got_frame = False
                assert proc.stdout is not None
                while not self._stop_event.is_set():
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        s = buf.find(b"\xff\xd8")
                        if s < 0:
                            if len(buf) > 65_536:
                                del buf[:-2]
                            break
                        e = buf.find(b"\xff\xd9", s + 2)
                        if e < 0:
                            if s > 0:
                                del buf[:s]
                            break
                        jpg = bytes(buf[s : e + 2])
                        del buf[: e + 2]
                        if not got_frame:
                            got_frame = True
                            self.cam_connected[cam.slug] = True
                        self.live_hub.publish_frame(cam.slug, jpg)

                proc.wait(timeout=3)
                err_t.join(timeout=1)
                self.cam_connected[cam.slug] = False
                if self._stop_event.is_set():
                    return
                if err_lines:
                    self.log.warning("[%s] FFmpeg exited (%s): %s", cam.name, proc.returncode, err_lines[-1])
            except Exception as exc:
                self.cam_connected[cam.slug] = False
                self.log.warning("[%s] RTSP worker error: %s", cam.name, exc)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            uptime = time.time() - t_start
            backoff = 2.0 if uptime > 20.0 else min(15.0, backoff * 1.5)
            self._stop_event.wait(backoff)

    def _segment_poll_loop(self) -> None:
        last_prune = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            with self.lock:
                phys_cams = self._physical_cameras()
                wb_cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
            for cam in phys_cams:
                self._ingest_closed_segments(cam, now)
            with self.wallboard.cond:
                last_active_ms = self.wallboard.last_active_push_ms
                workout_active = self.wallboard.workout_active
            self.adb.check_timers(wb_cfg, last_active_ms, workout_active)
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
            if now_s - st.st_mtime < 0.7 or st.st_size < 1024:
                continue
            start_ms = parse_segment_start_ms(p.name)
            if start_ms is None:
                start_ms = int(st.st_mtime * 1000) - 2000
            with self.lock:
                views = self._views_for_source(cam.slug, enabled_only=True)
            if not views:
                with self.lock:
                    self.seen_files.add(key)
                continue

            dur_ms, raw_frames = extract_ts_sampled_frames(p)
            rot_cache: dict[tuple[int, float], list[np.ndarray]] = {}
            first_raw = raw_frames[0] if raw_frames else None
            view_results: list[tuple[CameraConfig, SegmentInfo]] = []
            for v in views:
                tkey = (normalize_rotate(v.rotate), normalize_tilt(v.tilt))
                if tkey not in rot_cache:
                    rot_cache[tkey] = (
                        [
                            apply_frame_transform(frm, rotate=tkey[0], tilt=tkey[1], apply_crop=False)
                            for frm in raw_frames
                        ]
                        if tkey != (0, 0.0)
                        else raw_frames
                    )
                energy = (
                    compute_motion_energy_from_frames(
                        rot_cache[tkey], roi=v.effective_roi(), exclusions=v.exclusions
                    )
                    if rot_cache[tkey]
                    else 0.0
                )
                try:
                    self._get_or_create_seg_thumb(
                        v,
                        p,
                        max_w=DEFAULT_SCRUB_THUMB_W,
                        predecoded_frame=first_raw,
                    )
                except Exception:
                    pass
                info = SegmentInfo(
                    path=p,
                    start_ms=start_ms,
                    end_ms=start_ms + dur_ms,
                    energy=energy,
                    camera=v.name,
                )
                view_results.append((v, info))

            with self.lock:
                self.seen_files.add(key)
                for v, info in view_results:
                    self.latest_energy[v.slug] = info.energy
                    bucket = self.ring_segments.setdefault(v.slug, [])
                    bucket.append(info)
                    if len(bucket) > 600:
                        del bucket[:-600]
                    if self.recording and not self.paused and self.active_session_id:
                        self._copy_segment_to_session(self.active_session_id, v, info)

    def _copy_segment_to_session(self, session_id: str, cam: CameraConfig, info: SegmentInfo) -> None:
        media_slug = self._source_slug_for(cam)
        sdir = self.tapes_root / safe_slug(session_id, "session") / media_slug
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
            phys_cams = self._physical_cameras()
        for cam in phys_cams:
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

        if self.thumbs_root.exists():
            for jpg_p in self.thumbs_root.rglob("*.jpg"):
                try:
                    if jpg_p.stat().st_mtime < tape_cutoff:
                        jpg_p.unlink(missing_ok=True)
                except OSError:
                    pass
            for dir_p in sorted(self.thumbs_root.rglob("*"), reverse=True):
                if dir_p.is_dir():
                    try:
                        if not any(dir_p.iterdir()):
                            dir_p.rmdir()
                    except OSError:
                        pass

        if self.previews_root.exists():
            for tmp_p in self.previews_root.glob(".*.mp4"):
                try:
                    if tmp_p.stat().st_mtime < now_s - 300:
                        tmp_p.unlink(missing_ok=True)
                except OSError:
                    pass
            prev_cutoff = now_s - PREVIEW_KEEP_SEC
            kept_previews: list[tuple[float, Path]] = []
            for p in self.previews_root.glob("*.mp4"):
                if p.name.startswith("."):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime < prev_cutoff:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    kept_previews.append((mtime, p))
            if len(kept_previews) > PREVIEW_MAX_FILES:
                kept_previews.sort(key=lambda item: item[0])
                for _, stale_p in kept_previews[: len(kept_previews) - PREVIEW_MAX_FILES]:
                    try:
                        stale_p.unlink(missing_ok=True)
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
            (sdir / "session.json").write_text(
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
        media_slug = self._source_slug_for(cam) if cam else slug
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

            tape_dirs = [self.tapes_root / clean_id / media_slug]
            if slug != media_slug:
                tape_dirs.append(self.tapes_root / clean_id / slug)
            for tape_cam_dir in tape_dirs:
                if tape_cam_dir.exists():
                    for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                        if p.name in by_name:
                            continue
                        m = motion_map.get(p.name)
                        s_ms = m[0] if m else (parse_segment_start_ms(p.name) or 0)
                        e_ms = m[1] if m else (s_ms + 2000)
                        eng = m[2] if m else 0.0
                        by_name[p.name] = SegmentInfo(path=p, start_ms=s_ms, end_ms=e_ms, energy=eng, camera=canon_name)

        with self.lock:
            for info in self.ring_segments.get(slug, []):
                if info.path.exists() and info.path.name not in by_name:
                    by_name[info.path.name] = info
            if media_slug != slug:
                for info in self.ring_segments.get(media_slug, []):
                    if info.path.exists() and info.path.name not in by_name:
                        by_name[info.path.name] = SegmentInfo(
                            path=info.path,
                            start_ms=info.start_ms,
                            end_ms=info.end_ms,
                            energy=0.0,
                            camera=canon_name,
                        )

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

    def _find_camera_exact(self, name_or_slug: str | None) -> CameraConfig | None:
        if not name_or_slug:
            return None
        q = name_or_slug.strip().lower()
        if not q:
            return None
        with self.lock:
            cams = list(self.cameras)
        for c in cams:
            if c.name.lower() == q or c.slug.lower() == q:
                return c
        return None

    def _list_camera_segments(self, camera_name: str, session_id: str) -> list[SegmentInfo]:
        cam = self._find_camera(camera_name)
        slug = cam.slug if cam else safe_slug(camera_name.lower(), "rack_cam")
        media_slug = self._source_slug_for(cam) if cam else slug
        canon_name = cam.name if cam else camera_name
        clean_id = safe_slug(session_id, "session") if session_id else ""
        by_name: dict[str, SegmentInfo] = {}

        if clean_id:
            tape_dirs = [self.tapes_root / clean_id / media_slug]
            if slug != media_slug:
                tape_dirs.append(self.tapes_root / clean_id / slug)
            for tape_cam_dir in tape_dirs:
                if tape_cam_dir.exists():
                    for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                        if p.name in by_name:
                            continue
                        s_ms = parse_segment_start_ms(p.name)
                        if s_ms is None:
                            try:
                                s_ms = int(p.stat().st_mtime * 1000) - 2000
                            except OSError:
                                continue
                        by_name[p.name] = SegmentInfo(
                            path=p,
                            start_ms=s_ms,
                            end_ms=s_ms + 2000,
                            energy=0.0,
                            camera=canon_name,
                        )

        with self.lock:
            for info in self.ring_segments.get(slug, []):
                if info.path.exists() and info.path.name not in by_name:
                    by_name[info.path.name] = info
            if media_slug != slug:
                for info in self.ring_segments.get(media_slug, []):
                    if info.path.exists() and info.path.name not in by_name:
                        by_name[info.path.name] = SegmentInfo(
                            path=info.path,
                            start_ms=info.start_ms,
                            end_ms=info.end_ms,
                            energy=0.0,
                            camera=canon_name,
                        )

        return sorted(by_name.values(), key=lambda s: s.start_ms)

    def tape_timeline(self, camera_name: str, session_id: str, buckets: int = 80) -> dict[str, Any]:
        cam = self._find_camera(camera_name)
        canon = cam.name if cam else (camera_name or "Rack Cam")
        segs = self._list_camera_segments(canon, session_id)
        if not segs:
            return {
                "ok": True,
                "camera": canon,
                "minMs": 0,
                "maxMs": 0,
                "segmentCount": 0,
                "activity": [],
                "bursts30": [],
            }

        min_ms = int(segs[0].start_ms)
        max_ms = max(min_ms + 2000, max(int(s.end_ms) for s in segs))
        span = max(1, max_ms - min_ms)
        n_buckets = max(10, min(200, int(buckets)))
        bucket_max = [0.0] * n_buckets

        cam_samples: list[dict[str, Any]] = []
        cam_l = canon.lower()
        slug_l = cam.slug.lower() if cam else ""
        for s in self.session_samples(session_id):
            sc = str(s.get("camera", "")).strip().lower()
            ss = str(s.get("slug", "")).strip().lower()
            if sc != cam_l and (not slug_l or ss != slug_l):
                continue
            cam_samples.append(s)
            mid = (int(s["startMs"]) + int(s["endMs"])) // 2
            bi = min(n_buckets - 1, max(0, int(((mid - min_ms) / span) * n_buckets)))
            e = float(s.get("energy", 0.0))
            if e > bucket_max[bi]:
                bucket_max[bi] = e

        activity = [
            {"tMs": int(min_ms + ((i + 0.5) / n_buckets) * span), "e": round(bucket_max[i], 2)}
            for i in range(n_buckets)
        ]
        thresh = cam.motion_threshold if cam else DEFAULT_MOTION_THRESHOLD
        bursts30 = [
            {"startMs": b["startMs"], "endMs": b["endMs"], "durationSec": b["durationSec"]}
            for b in cluster_motion_bursts(
                cam_samples,
                threshold=thresh,
                min_dur_ms=30_000,
                camera=canon,
                resolve_overlap=False,
            )
        ]
        return {
            "ok": True,
            "camera": canon,
            "minMs": min_ms,
            "maxMs": max_ms,
            "segmentCount": len(segs),
            "activity": activity,
            "bursts30": bursts30,
        }

    @staticmethod
    def _crop_cache_key(cam: CameraConfig) -> str:
        x0, y0, x1, y1 = cam.crop
        base = (
            f"r{cam.rotate}_{int(round(x0 * 1000))}_{int(round(y0 * 1000))}_"
            f"{int(round(x1 * 1000))}_{int(round(y1 * 1000))}"
        )
        tlt = normalize_tilt(cam.tilt)
        return f"{base}_t{int(round(tlt * 10))}" if tlt != 0.0 else base

    def _render_segment_thumb_jpeg(
        self,
        seg_path: Path,
        cam: CameraConfig | None,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
        quality: int = DEFAULT_SCRUB_THUMB_Q,
        predecoded_frame: np.ndarray | None = None,
    ) -> bytes | None:
        frm = predecoded_frame
        if frm is None:
            cap = cv2.VideoCapture(str(seg_path))
            if not cap.isOpened():
                return None
            try:
                # Read sequentially from the opening IDR keyframe (never call POS_MSEC/POS_FRAMES on MPEG-TS).
                for _ in range(5):
                    ok0, f0 = cap.read()
                    if ok0 and f0 is not None:
                        frm = f0
                        break
            finally:
                cap.release()
        if frm is None:
            return None
        if cam:
            frm = apply_frame_transform(frm, cam.crop, cam.rotate, apply_crop=True, tilt=cam.tilt)
        h, w = frm.shape[:2]
        tw = max(180, min(720, int(max_w)))
        if w > tw:
            th = max(1, int(round(h * (tw / max(1, w)))))
            frm = cv2.resize(frm, (tw, th), interpolation=cv2.INTER_AREA)
        ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        return buf.tobytes() if ok_jpg else None

    def _put_mem_thumb(self, mem_key: str, jpg: bytes) -> None:
        with self._thumb_lock:
            self._thumb_mem[mem_key] = jpg
            self._thumb_mem.move_to_end(mem_key)
            while len(self._thumb_mem) > 1200:
                self._thumb_mem.popitem(last=False)

    def _get_or_create_seg_thumb(
        self,
        cam: CameraConfig | None,
        seg_path: Path,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
        quality: int = DEFAULT_SCRUB_THUMB_Q,
        predecoded_frame: np.ndarray | None = None,
    ) -> bytes | None:
        tw = max(180, min(720, int(max_w)))
        if cam is None or tw != DEFAULT_SCRUB_THUMB_W:
            return self._render_segment_thumb_jpeg(
                seg_path, cam, max_w=tw, quality=quality, predecoded_frame=predecoded_frame
            )
        ck = self._crop_cache_key(cam)
        mem_key = f"{cam.slug}/{ck}/{seg_path.stem}"
        with self._thumb_lock:
            hit = self._thumb_mem.get(mem_key)
            if hit is not None:
                self._thumb_mem.move_to_end(mem_key)
                return hit
        cache_dir = self.thumbs_root / cam.slug / ck
        cache_path = cache_dir / f"{seg_path.stem}.jpg"
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 100:
                jpg = cache_path.read_bytes()
                self._put_mem_thumb(mem_key, jpg)
                return jpg
        except OSError:
            pass
        jpg = self._render_segment_thumb_jpeg(
            seg_path,
            cam,
            max_w=DEFAULT_SCRUB_THUMB_W,
            quality=quality,
            predecoded_frame=predecoded_frame,
        )
        if not jpg:
            return None
        self._put_mem_thumb(mem_key, jpg)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_dir / f"{seg_path.stem}.{threading.get_ident()}.tmp"
            tmp_path.write_bytes(jpg)
            tmp_path.replace(cache_path)
        except OSError:
            pass
        return jpg

    def get_tape_frame_jpeg(
        self,
        camera_name: str,
        session_id: str,
        ts_ms: int,
        max_w: int = 480,
    ) -> bytes | None:
        cam = self._find_camera(camera_name)
        segs = self._list_camera_segments(camera_name, session_id)
        if not segs:
            return None

        starts = [s.start_ms for s in segs]
        idx = bisect.bisect_right(starts, ts_ms) - 1
        cands: list[SegmentInfo] = []
        if idx >= 0:
            cands.append(segs[idx])
        if idx + 1 < len(segs):
            cands.append(segs[idx + 1])
        if not cands:
            cands.append(segs[0])
        best = min(cands, key=lambda s: abs(((s.start_ms + s.end_ms) // 2) - ts_ms))

        if cam and int(max_w) <= DEFAULT_SCRUB_THUMB_W:
            cached = self._get_or_create_seg_thumb(cam, best.path, max_w=DEFAULT_SCRUB_THUMB_W)
            if cached:
                return cached

        cap = cv2.VideoCapture(str(best.path))
        if not cap.isOpened():
            return None
        try:
            # Read sequentially from the opening IDR keyframe (never call POS_MSEC/POS_FRAMES on MPEG-TS).
            first_frm = None
            for _ in range(5):
                ok0, f0 = cap.read()
                if ok0 and f0 is not None:
                    first_frm = f0
                    break
            if first_frm is None:
                return None

            frm = first_frm
            offset_ms = ts_ms - best.start_ms
            if 600 <= offset_ms <= 1900:
                grabs = min(20, max(1, int(offset_ms // 80)))
                ok_g = True
                for _ in range(grabs):
                    if not cap.grab():
                        ok_g = False
                        break
                if ok_g:
                    ok_r, f_later = cap.retrieve()
                    if ok_r and f_later is not None:
                        frm = f_later

            if cam:
                frm = apply_frame_transform(frm, cam.crop, cam.rotate, apply_crop=True, tilt=cam.tilt)
            h, w = frm.shape[:2]
            tw = max(180, min(720, int(max_w)))
            if w > tw:
                th = max(1, int(round(h * (tw / max(1, w)))))
                frm = cv2.resize(frm, (tw, th), interpolation=cv2.INTER_AREA)
            ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
            return buf.tobytes() if ok_jpg else None
        finally:
            cap.release()

    def tape_frame_pack(
        self,
        camera_name: str,
        session_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        count: int = 80,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
    ) -> dict[str, Any]:
        cam = self._find_camera(camera_name)
        canon = cam.name if cam else (camera_name or "Rack Cam")
        segs = self._list_camera_segments(canon, session_id)
        if not segs:
            return {
                "ok": True,
                "camera": canon,
                "minMs": 0,
                "maxMs": 0,
                "winStartMs": 0,
                "winEndMs": 0,
                "frames": [],
            }

        min_ms = int(segs[0].start_ms)
        max_ms = max(min_ms + 2000, max(int(s.end_ms) for s in segs))
        filtered = segs
        if start_ms is not None or end_ms is not None:
            w0 = int(start_ms) if start_ms is not None else min_ms
            w1 = int(end_ms) if end_ms is not None else max_ms
            sub = [s for s in segs if s.end_ms >= w0 - 2500 and s.start_ms <= w1 + 2500]
            if sub:
                filtered = sub

        n = max(1, min(180, int(count)))
        if len(filtered) <= n:
            picked = filtered
        elif n == 1:
            picked = [filtered[len(filtered) // 2]]
        else:
            seen_idx: set[int] = set()
            picked = []
            last = len(filtered) - 1
            for i in range(n):
                idx = int(round((i * last) / (n - 1)))
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    picked.append(filtered[idx])

        tw = max(180, min(720, int(max_w)))
        if len(picked) > 4:
            def _load_one(s: SegmentInfo) -> bytes | None:
                return self._get_or_create_seg_thumb(cam, s.path, max_w=tw)
            with ThreadPoolExecutor(max_workers=4) as pool:
                jpgs = list(pool.map(_load_one, picked))
        else:
            jpgs = [self._get_or_create_seg_thumb(cam, seg.path, max_w=tw) for seg in picked]

        frames: list[dict[str, Any]] = []
        for seg, jpg in zip(picked, jpgs):
            if jpg:
                frames.append(
                    {
                        "tsMs": (int(seg.start_ms) + int(seg.end_ms)) // 2,
                        "jpg": base64.b64encode(jpg).decode("ascii"),
                    }
                )
        return {
            "ok": True,
            "camera": canon,
            "minMs": min_ms,
            "maxMs": max_ms,
            "winStartMs": int(picked[0].start_ms),
            "winEndMs": int(picked[-1].end_ms),
            "frames": frames,
        }

    def bursts_for_session(
        self,
        session_id: str,
        around_ms: int | None = None,
        camera: str | None = None,
        min_dur_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        samples = self.session_samples(session_id)
        cam_cfg = self._find_camera(camera) if camera and camera.strip() else None
        if cam_cfg is None:
            with self.lock:
                primary_cams = {c.name for c in self.cameras if c.enabled and c.primary}
            if primary_cams:
                prim_samples = [s for s in samples if s.get("camera") in primary_cams]
                if prim_samples:
                    samples = prim_samples
        thresh = (
            cam_cfg.motion_threshold
            if cam_cfg
            else min((c.motion_threshold for c in self.cameras), default=DEFAULT_MOTION_THRESHOLD)
        )
        min_dur_ms = int(round(max(1.0, min_dur_sec) * 1000)) if min_dur_sec is not None else 4000
        bursts = cluster_motion_bursts(
            samples,
            threshold=thresh,
            min_dur_ms=min_dur_ms,
            camera=(cam_cfg.name if cam_cfg else None),
            resolve_overlap=(cam_cfg is None),
        )
        limit = 20
        if around_ms is not None:
            bursts.sort(key=lambda b: abs(int(b["endMs"]) - around_ms))
            bursts = bursts[:limit]
            bursts.sort(key=lambda b: int(b["startMs"]))
        else:
            bursts = bursts[-limit:]

        for b in bursts:
            mid_ms = (int(b["startMs"]) + int(b["endMs"])) // 2
            b["thumb"] = self._thumb_near_ms(str(b["camera"]), session_id, mid_ms, max_w=DEFAULT_SCRUB_THUMB_W)
        return bursts

    def _thumb_near_ms(
        self,
        camera_name: str,
        session_id: str,
        target_ms: int,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
    ) -> str:
        jpg = self.get_tape_frame_jpeg(camera_name, session_id, target_ms, max_w=max_w)
        if not jpg:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")

    def get_snapshot_jpeg(
        self,
        camera_name: str | None = None,
        raw: bool = False,
        hires: bool = False,
        rotate_override: int | None = None,
        tilt_override: float | None = None,
    ) -> bytes | None:
        cam = self._find_camera(camera_name)
        if not cam:
            return None
        eff_rot = normalize_rotate(rotate_override) if rotate_override is not None else cam.rotate
        eff_tilt = normalize_tilt(tilt_override) if tilt_override is not None else cam.tilt
        media_slug = self._source_slug_for(cam)
        src_cam = self._source_camera(cam)

        if hires:
            with self.lock:
                segs = list(self.ring_segments.get(cam.slug) or self.ring_segments.get(media_slug, []))
            for seg in reversed(segs[-4:]):
                if seg.path.exists():
                    cap = cv2.VideoCapture(str(seg.path))
                    if cap.isOpened():
                        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                        if total > 1:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
                        ok, frm = cap.read()
                        if (not ok or frm is None) and total > 1:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            ok, frm = cap.read()
                        cap.release()
                        if ok and frm is not None:
                            frm = apply_frame_transform(
                                frm, cam.crop, eff_rot, apply_crop=not raw, tilt=eff_tilt
                            )
                            ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                            if ok_jpg:
                                return buf.tobytes()

        live_jpg = self.live_hub.recent_frame(media_slug, max_age_sec=2.0)
        if live_jpg:
            if not raw and (eff_rot != 0 or eff_tilt != 0.0 or cam.crop != (0.0, 0.0, 1.0, 1.0)):
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    img = apply_frame_transform(img, cam.crop, eff_rot, apply_crop=True, tilt=eff_tilt)
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        return buf.tobytes()
            elif eff_rot != 0 or eff_tilt != 0.0:
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    img = apply_frame_transform(img, cam.crop, eff_rot, apply_crop=False, tilt=eff_tilt)
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        return buf.tobytes()
            return live_jpg

        with self.lock:
            segs = list(self.ring_segments.get(cam.slug) or self.ring_segments.get(media_slug, []))
        for seg in reversed(segs[-4:]):
            if seg.path.exists():
                cap = cv2.VideoCapture(str(seg.path))
                if cap.isOpened():
                    ok, frm = cap.read()
                    cap.release()
                    if ok and frm is not None:
                        frm = apply_frame_transform(
                            frm, cam.crop, eff_rot, apply_crop=not raw, tilt=eff_tilt
                        )
                        ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                        if ok_jpg:
                            return buf.tobytes()

        if cam.enabled and src_cam.rtsp_url:
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                src_cam.rtsp_url,
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
                    img = cv2.imdecode(np.frombuffer(res.stdout, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        img = apply_frame_transform(
                            img, cam.crop, eff_rot, apply_crop=not raw, tilt=eff_tilt
                        )
                        ok_jpg, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                        if ok_jpg:
                            return buf.tobytes()
                    return res.stdout
            except Exception:
                pass
        return None

    def _encode_segments_mp4(
        self,
        segs: list[SegmentInfo],
        cam_cfg: CameraConfig | None,
        start_ms: int,
        end_ms: int,
        out_path: Path,
    ) -> bool:
        if not segs:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        vf = build_ffmpeg_vf(cam_cfg.crop, cam_cfg.rotate, tilt=cam_cfg.tilt) if cam_cfg else None
        first_start_ms = segs[0].start_ms
        ss_sec = max(0.0, (start_ms - first_start_ms) / 1000.0)
        dur_sec = max(0.5, (end_ms - start_ms) / 1000.0)
        concat_path = out_path.parent / f".concat_{uuid.uuid4().hex[:8]}.txt"
        tmp_out = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex[:6]}.mp4")
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
                *(["-vf", vf] if vf else []),
                "-c:v",
                "libx264",
                "-preset",
                "superfast",
                "-g",
                "15",
                "-keyint_min",
                "15",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(tmp_out),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
                self.log.warning("FFmpeg clip failed: %s", res.stderr.strip())
                tmp_out.unlink(missing_ok=True)
                return False
            tmp_out.replace(out_path)
            return True
        finally:
            concat_path.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)

    def tape_angles_for_window(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        norm_end = max(int(start_ms) + 1000, int(end_ms))
        with self.lock:
            cams = [c for c in self.cameras if c.enabled]
        avail: list[str] = []
        for cam in cams:
            segs = self.find_segments(cam.name, session_id, int(start_ms), norm_end)
            if any(s.end_ms > int(start_ms) and s.start_ms < norm_end for s in segs):
                avail.append(cam.name)
        return avail

    def cut_preview(
        self,
        session_id: str,
        start_ms: int,
        end_ms: int,
        camera_name: str,
    ) -> dict[str, Any] | None:
        cam_cfg = self._find_camera_exact(camera_name)
        if not cam_cfg:
            return None
        s_ms = int(start_ms)
        e_ms = min(s_ms + 180_000, max(s_ms + 1000, int(end_ms)))
        segs = self.find_segments(cam_cfg.name, session_id, s_ms, e_ms)
        if not any(s.end_ms > s_ms and s.start_ms < e_ms for s in segs):
            return None

        ck = self._crop_cache_key(cam_cfg)
        sid_slug = safe_slug(session_id or "ring", "ring")[:32]
        fname = f"{sid_slug}_{cam_cfg.slug}_{s_ms}_{e_ms}_{ck}.mp4"
        out_path = self.previews_root / fname

        with self._preview_locks_guard:
            p_lock = self._preview_locks.get(fname)
            if p_lock is None:
                p_lock = threading.Lock()
                self._preview_locks[fname] = p_lock

        with p_lock:
            if out_path.is_file() and out_path.stat().st_size > 1000:
                try:
                    os.utime(out_path, None)
                except OSError:
                    pass
                cached = True
            else:
                ok = self._encode_segments_mp4(segs, cam_cfg, s_ms, e_ms, out_path)
                if not ok:
                    return None
                cached = False

        actual_dur = self._probe_duration_sec(out_path) or max(1, int(round((e_ms - s_ms) / 1000.0)))
        return {
            "ok": True,
            "ephemeral": True,
            "cached": cached,
            "lanUrl": f"/previews/{fname}",
            "fileName": fname,
            "camera": cam_cfg.name,
            "durationSec": max(1, actual_dur),
            "startMs": s_ms,
            "endMs": e_ms,
            "bytes": out_path.stat().st_size,
        }

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
        primary_cams = {c.name for c in self.cameras if c.enabled and c.primary}
        fallback_cam = next((c.name for c in self.cameras if c.enabled and c.primary), self.cameras[0].name if self.cameras else "Rack Cam")
        chosen_cam = select_camera_for_window(
            samples,
            start_ms,
            end_ms,
            preferred_camera=camera_override,
            fallback_camera=fallback_cam,
            primary_cameras=primary_cams or None,
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

        cam_cfg = self._find_camera(chosen_cam)
        dur_sec = max(0.5, (end_ms - start_ms) / 1000.0)

        day_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%H%M%S")
        day_dir = self.clips_root / day_str
        day_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{safe_slug(ex_name or 'lift')}_{time_str}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = day_dir / out_name

        if not self._encode_segments_mp4(segs, cam_cfg, int(start_ms), int(end_ms), out_path):
            return None

        bytes_len = out_path.stat().st_size
        thumb = extract_video_thumb(out_path)
        actual_dur = self._probe_duration_sec(out_path) or int(round(dur_sec))
        drive_info = self.mirror_clip_to_drive(out_path, day_str)
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
            **({"driveFileId": drive_info["driveFileId"]} if drive_info.get("driveFileId") else {}),
            **({"driveUrl": drive_info["driveUrl"]} if drive_info.get("driveUrl") else {}),
        }
        with self.clips_index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in meta.items() if k != "thumb"}) + "\n")
        self.check_calibration_milestones()
        return meta

    def mirror_clip_to_drive(self, src_path: Path, day_str: str) -> dict[str, str]:
        """Copy an encoded MP4 into Google Drive backup folder and read Drive file ID if synced."""
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        if not backup_dir or not src_path.is_file():
            return {}
        try:
            dest_root = Path(backup_dir)
            anchor = Path(dest_root.anchor) if dest_root.anchor else dest_root
            if dest_root.anchor and not anchor.exists():
                return {}
            dest_dir = dest_root / (day_str or datetime.now().strftime("%Y-%m-%d"))
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src_path.name
            src_size = src_path.stat().st_size
            if not dest.exists() or dest.stat().st_size != src_size:
                shutil.copy2(src_path, dest)
            fid = read_google_drive_file_id(dest)
            url = google_drive_view_url(fid)
            out: dict[str, str] = {"drivePath": str(dest)}
            if fid and url:
                out["driveFileId"] = fid
                out["driveUrl"] = url
            return out
        except Exception as exc:
            self.log.warning("Google Drive clip mirror failed for %s: %s", src_path.name, exc)
            return {}

    def backfill_drive_clips(self) -> int:
        """Mirror any existing OptiPlex clips missing from Google Drive backup folder."""
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        if not backup_dir or not self.clips_root.exists():
            return 0
        copied = 0
        for p in sorted(self.clips_root.glob("*/*.mp4")):
            if self._stop_event.is_set():
                break
            dest = Path(backup_dir) / p.parent.name / p.name
            try:
                need = not dest.exists() or dest.stat().st_size != p.stat().st_size
            except OSError:
                need = True
            if need and self.mirror_clip_to_drive(p, p.parent.name):
                copied += 1
        return copied

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
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        out: list[dict[str, Any]] = []
        for line in reversed(self.clips_index_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if backup_dir and not item.get("driveUrl"):
                fname = str(item.get("fileName") or "").strip()
                parts = [p for p in str(item.get("lanUrl") or "").split("/") if p]
                day_str = parts[1] if len(parts) >= 3 and parts[0] == "clips" else ""
                if fname and day_str:
                    fid = read_google_drive_file_id(Path(backup_dir) / day_str / fname)
                    url = google_drive_view_url(fid)
                    if fid and url:
                        item["driveFileId"] = fid
                        item["driveUrl"] = url
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def delete_clip(self, lan_url: str = "", file_name: str = "") -> dict[str, Any]:
        """Delete a saved MP4 from local `clips_root`, Google Drive mirror, and `index.jsonl` (preserves raw tapes)."""
        base_resolved = self.clips_root.resolve()
        rel_path = ""
        raw_url = str(lan_url or "").strip()
        if "/clips/" in raw_url:
            rel_path = raw_url.split("/clips/", 1)[1].split("?", 1)[0].strip("/")
        parts = [p for p in rel_path.split("/") if p] if rel_path else []
        target_name = Path(parts[-1]).name if parts else Path(str(file_name or "").strip()).name
        candidates: list[Path] = []
        if len(parts) >= 2 and target_name:
            candidates.append(self.clips_root / parts[-2] / target_name)
        elif target_name:
            candidates.extend(self.clips_root.glob(f"*/{target_name}"))

        local_deleted = False
        drive_deleted = False
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
            for cand in candidates:
                try:
                    resolved = cand.resolve()
                    rel = resolved.relative_to(base_resolved)
                except Exception:
                    continue
                if resolved.is_file():
                    try:
                        resolved.unlink()
                        local_deleted = True
                    except OSError:
                        pass
                if backup_dir and len(rel.parts) >= 2:
                    d_file = Path(backup_dir) / rel.parts[-2] / rel.parts[-1]
                    if d_file.is_file():
                        try:
                            d_file.unlink()
                            drive_deleted = True
                        except OSError:
                            pass
            if backup_dir and target_name and not drive_deleted and len(parts) >= 2:
                d_file = Path(backup_dir) / parts[-2] / target_name
                if d_file.is_file():
                    try:
                        d_file.unlink()
                        drive_deleted = True
                    except OSError:
                        pass
            if self.clips_index_path.exists() and (target_name or raw_url):
                kept: list[str] = []
                for line in self.clips_index_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        kept.append(line)
                        continue
                    fname = str(obj.get("fileName") or "")
                    lurl = str(obj.get("lanUrl") or "")
                    if (target_name and fname == target_name) or (
                        target_name and lurl.endswith(f"/{target_name}")
                    ):
                        continue
                    kept.append(line)
                tmp = self.clips_index_path.with_suffix(".jsonl.tmp")
                tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                tmp.replace(self.clips_index_path)

        return {
            "ok": True,
            "fileName": target_name,
            "localDeleted": local_deleted,
            "driveDeleted": drive_deleted,
        }

    def calibration_status(self) -> dict[str, Any]:
        """Return unique clean clip count and notified pose-calibration milestones."""
        with self.lock:
            clip_count = (
                len({p.name for p in self.clips_root.glob("*/*.mp4") if p.is_file()})
                if self.clips_root.exists()
                else 0
            )
            notified: list[int] = []
            if self.cal_state_path.is_file():
                try:
                    raw = json.loads(self.cal_state_path.read_text(encoding="utf-8"))
                    notified = [int(x) for x in raw.get("notified", []) if isinstance(x, (int, float))]
                except Exception:
                    notified = []
        next_m = next((m for m in CALIBRATION_MILESTONES if clip_count < m), None)
        return {
            "clipCount": clip_count,
            "milestones": list(CALIBRATION_MILESTONES),
            "notified": sorted(set(notified)),
            "nextMilestone": next_m,
        }

    def check_calibration_milestones(self) -> list[int]:
        """Persist newly reached calibration clip milestones (5 / 15 / 50) and fire ntfy alert once per tier."""
        with self.lock:
            st = self.calibration_status()
            count = int(st["clipCount"])
            notified = list(st["notified"])
            crossed = [m for m in CALIBRATION_MILESTONES if count >= m and m not in notified]
            if not crossed:
                return []
            merged = sorted(set(notified + crossed))
            tmp = self.cal_state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"notified": merged, "clipCount": count, "updatedAtMs": int(time.time() * 1000)}),
                encoding="utf-8",
            )
            tmp.replace(self.cal_state_path)
            topic = self.ntfy_topic

        if topic:
            top = max(crossed)
            if top == 5:
                title = "Armory Fit: 5 Clean Clips Ready"
                msg = f"You now have {count} clean Gym Cam clips saved. Tell Antigravity 'calibrate pose trimming' anytime for the early unrack-to-rack pass."
            elif top == 15:
                title = "Armory Fit: 15 Clips Collected"
                msg = f"{count} clean clips across multiple lifts are ready for multi-exercise pose-trim tuning in Antigravity."
            else:
                title = "Armory Fit: 50 Clips Collected"
                msg = f"{count} clean clips saved! Ready for full unrack-to-rack calibration lock-in in Antigravity."
            threading.Thread(
                target=send_ntfy_alert,
                args=(topic, title, msg),
                name="ntfy-cal-milestone",
                daemon=True,
            ).start()
        return crossed

    def disk_stats(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        return {
            "root": str(self.root),
            "freeGb": round(usage.free / (1024**3), 1),
            "totalGb": round(usage.total / (1024**3), 1),
        }

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            cams = [
                {
                    "name": c.name,
                    "slug": c.slug,
                    "sourceSlug": c.source_slug,
                    "primary": c.primary,
                    "enabled": c.enabled,
                    "connected": bool(self.cam_connected.get(self._source_slug_for(c), False)),
                    "latestEnergy": float(self.latest_energy.get(c.slug, 0.0)),
                    "motionThreshold": c.motion_threshold,
                    "ringSegments": len(
                        self.ring_segments.get(c.slug) or self.ring_segments.get(self._source_slug_for(c), [])
                    ),
                }
                for c in self.cameras
            ]
            wb_cfg = self.wallboard_cfg
            wb_status = {
                **self.adb.status_dict(wb_cfg),
                **self.wallboard.status_dict(),
                "wallboardUrl": self.effective_wallboard_url(),
                "wakeOnStart": wb_cfg.wake_on_start,
                "sleepOnStop": wb_cfg.sleep_on_stop,
                "idleSleepSec": wb_cfg.idle_sleep_sec,
                "displayClient": wb_cfg.display_client,
                "enabled": wb_cfg.enabled,
            }
            return {
                "service": "armory-cam",
                "serverEpochMs": int(time.time() * 1000),
                "recording": self.recording,
                "paused": self.paused,
                "disk": self.disk_stats(),
                "calibration": self.calibration_status(),
                "cameras": cams,
                "wallboard": wb_status,
            }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Cam · OptiPlex DVR</title>
<style>
  :root{--bg:#0b0f14;--card:#131a22;--line:#233040;--fg:#edf2f7;--mut:#8b9bb0;--blue:#1e9bff;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  [hidden]{display:none!important}
  .wrap{max-width:1120px;margin:0 auto;padding:18px}
  h1{margin:0 0 6px;font-size:22px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .pill{font-size:11px;padding:3px 10px;border-radius:999px;background:#1d2836;color:var(--mut);font-weight:800;letter-spacing:.04em}
  .pill.ok{background:rgba(34,197,94,.18);color:#86efac}
  .pill.live{background:rgba(30,155,255,.18);color:#7cc4ff}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-top:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
  .card h2{margin:0 0 10px;font-size:16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
  .cam-head-l{display:flex;align-items:center;gap:8px;min-width:0;flex:1;flex-wrap:wrap}
  .cam-head-r{display:flex;align-items:center;gap:6px;flex-shrink:0}
  .cam-name-inp{width:170px;max-width:100%;padding:4px 8px!important;font-weight:800!important;font-size:14px!important;border-radius:8px!important}
  .stage{position:relative;width:100%;aspect-ratio:16/9;background:#06090d;border-radius:10px;border:1px solid var(--line);overflow:hidden;user-select:none;touch-action:none}
  .stage.rot90,.stage.rot270{width:auto;height:480px;max-width:100%;aspect-ratio:9/16;margin:0 auto}
  .stage img.stream,.stage canvas.stream{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none}
  .level-grid{position:absolute;inset:0;pointer-events:none;z-index:4;opacity:0;transition:opacity .12s ease;background-image:linear-gradient(to right,rgba(124,196,255,.32) 1px,transparent 1px),linear-gradient(to bottom,rgba(124,196,255,.32) 1px,transparent 1px);background-size:12.5% 12.5%;}
  .stage.leveling .level-grid{opacity:1}
  .tilt-num{width:58px!important;padding:4px 6px!important;font-size:12px!important;text-align:right;font-variant-numeric:tabular-nums}
  .crop-layer{position:absolute;inset:0;pointer-events:auto}
  .scrim{position:absolute;background:rgba(4,7,11,.62);pointer-events:none}
  .crop-box{position:absolute;border:2px solid var(--blue);box-shadow:0 0 0 1px rgba(0,0,0,.6);cursor:move;touch-action:none}
  .crop-box::after{content:'';position:absolute;inset:0;border:1px dashed rgba(255,255,255,.28);pointer-events:none}
  .box-tag{position:absolute;left:4px;top:4px;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:900;letter-spacing:.06em;pointer-events:none;background:rgba(6,9,13,.82);color:var(--blue)}
  .crop-box.passive{border-style:dashed;opacity:.52;pointer-events:none;cursor:default}
  .crop-box.passive .handle{display:none}
  .excl-wrap{position:absolute;inset:0;pointer-events:none;z-index:3}
  .excl-wrap.passive{opacity:.6}
  .excl-wrap.passive .quad-poly{stroke-dasharray:6 4;pointer-events:none;cursor:default}
  .excl-wrap.passive .quad-dot{display:none}
  .excl-svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none}
  .quad-poly{fill:rgba(239,68,68,.24);stroke:#ef4444;stroke-width:2;pointer-events:all;cursor:move;transition:fill .12s,stroke .12s}
  .quad-poly.active{fill:rgba(239,68,68,.30);stroke:#fca5a5;stroke-width:2.4}
  .quad-poly.crossed{stroke:var(--amber);fill:rgba(245,158,11,.22)}
  .quad-tag{position:absolute;transform:translate(-50%,-50%);padding:1px 6px;border-radius:4px;font-size:10px;font-weight:900;letter-spacing:.06em;pointer-events:none;background:rgba(6,9,13,.84);color:#fecaca;border:1px solid rgba(239,68,68,.45);white-space:nowrap}
  .quad-dot{position:absolute;width:28px;height:28px;transform:translate(-50%,-50%);cursor:grab;pointer-events:auto;touch-action:none;z-index:6;display:flex;align-items:center;justify-content:center;background:transparent}
  .mm-canvas .quad-dot{width:34px;height:34px}
  .quad-ring{position:relative;width:16px;height:16px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1.5px rgba(239,68,68,.95),0 1px 6px rgba(0,0,0,.85);background:rgba(239,68,68,.10);pointer-events:none;transition:transform .08s ease,border-color .1s ease,box-shadow .1s ease}
  .mm-canvas .quad-ring{width:20px;height:20px}
  .quad-ring::before{content:'';position:absolute;left:50%;top:1px;bottom:1px;width:1px;transform:translateX(-50%);background:rgba(255,255,255,.55)}
  .quad-ring::after{content:'';position:absolute;top:50%;left:1px;right:1px;height:1px;transform:translateY(-50%);background:rgba(255,255,255,.55)}
  .quad-dot:hover .quad-ring,.quad-dot.dragging .quad-ring{border-color:#fde047;box-shadow:0 0 0 2px rgba(245,158,11,.95),0 0 12px rgba(250,204,21,.65);transform:scale(1.14)}
  .quad-dot.dragging{cursor:grabbing;z-index:8}
  .quad-num{position:absolute;left:100%;top:-2px;margin-left:2px;font-size:9px;font-weight:900;color:#fff;background:rgba(6,9,13,.88);padding:0 3px;border-radius:3px;border:1px solid rgba(255,255,255,.22);pointer-events:none;line-height:1.2}
  .handle{position:absolute;width:22px;height:22px;border-radius:50%;background:var(--blue);border:2px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.7);touch-action:none}
  .handle.nw{left:-11px;top:-11px;cursor:nwse-resize}
  .handle.ne{right:-11px;top:-11px;cursor:nesw-resize}
  .handle.sw{left:-11px;bottom:-11px;cursor:nesw-resize}
  .handle.se{right:-11px;bottom:-11px;cursor:nwse-resize}
  .crop-info{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin-top:8px;font-variant-numeric:tabular-nums}
  .ctrl-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
  .ctrl-row input[type=range]{flex:1;min-width:120px;accent-color:var(--blue)}
  .mbar-wrap{position:relative;height:10px;border-radius:999px;background:#0b1017;border:1px solid var(--line);overflow:hidden;margin-top:6px}
  .mbar-fill{position:absolute;left:0;top:0;bottom:0;background:var(--blue);width:0%;transition:width .25s}
  .mbar-fill.hot{background:var(--green)}
  .mbar-th{position:absolute;top:0;bottom:0;width:2px;background:var(--amber)}
  label{display:block;font-size:12px;color:var(--mut);margin:8px 0 4px}
  input[type=text],input[type=number],select{width:100%;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:#0d1218;color:var(--fg);font:inherit}
  .btn{background:var(--blue);color:#00131f;border:0;border-radius:9px;padding:7px 12px;font-weight:800;font-size:12px;cursor:pointer;transition:transform .08s ease,background-color .15s ease,border-color .15s ease,box-shadow .15s ease,filter .15s ease,color .15s ease;user-select:none}
  .btn:hover:not(:disabled){filter:brightness(1.12);box-shadow:0 2px 10px rgba(0,0,0,.35)}
  .btn:active:not(:disabled){transform:translateY(2px) scale(.96);filter:brightness(.92);box-shadow:inset 0 2px 5px rgba(0,0,0,.45)}
  .btn:disabled{opacity:.42;cursor:not-allowed}
  .btn.sm{padding:5px 10px}
  .btn.sec{background:#1d2836;color:var(--fg);border:1px solid var(--line)}
  .btn.sec.on{background:rgba(30,155,255,.2);border-color:var(--blue);color:#7cc4ff}
  .btn.ok{background:rgba(34,197,94,.22);border:1px solid rgba(34,197,94,.5);color:#86efac}
  .btn.red{background:rgba(239,68,68,.18);border:1px solid rgba(239,68,68,.55);color:#fecaca}
  .btn.red:hover:not(:disabled){background:rgba(239,68,68,.28)}
  .btn.saving{opacity:.82;cursor:wait}
  .btn.saved{background:#22c55e!important;border-color:#4ade80!important;color:#031a0c!important;box-shadow:0 0 0 2px rgba(34,197,94,.4),0 2px 12px rgba(34,197,94,.35)}
  .btn.save-err{background:#ef4444!important;border-color:#f87171!important;color:#fff!important}
  .btn.dirty{box-shadow:0 0 0 2px rgba(245,158,11,.55)}
  .mask-modal{position:fixed;inset:0;z-index:9999;background:rgba(4,7,11,.96);display:flex;flex-direction:column;padding:10px 14px;gap:8px;user-select:none}
  .mm-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;justify-content:space-between}
  .mm-title{font-weight:900;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .mm-tabs{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .mm-tab{display:inline-flex;align-items:center;gap:6px;background:#1d2836;border:1px solid var(--line);border-radius:9px;padding:3px 6px 3px 10px;font-size:12px;font-weight:800;color:var(--fg);cursor:pointer}
  .mm-tab.on{background:rgba(239,68,68,.22);border-color:var(--red);color:#fecaca}
  .mm-tab-del{border:0;background:rgba(239,68,68,.3);color:#fecaca;border-radius:5px;width:18px;height:18px;font:900 12px/1 system-ui,sans-serif;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0}
  .mm-tab-del:hover{background:var(--red);color:#fff}
  .mm-zoombar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 10px}
  .mm-zoombar input[type=range]{width:140px;accent-color:var(--blue)}
  .mm-viewport{flex:1;min-height:0;position:relative;overflow:hidden;background:#05080d;border:1px solid var(--line);border-radius:12px;touch-action:none;cursor:grab}
  .mm-viewport.panning{cursor:grabbing}
  .mm-canvas{position:absolute;transform-origin:0 0;touch-action:none}
  .mm-img{width:100%;height:100%;display:block;pointer-events:none}
  .mm-crop-ref{position:absolute;border:1.5px dashed rgba(30,155,255,.72);background:rgba(30,155,255,.04);pointer-events:none}
  .mm-mbar{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
  .mm-mbar-track{position:relative;width:72px;height:8px;border-radius:999px;background:#0b1017;border:1px solid var(--line);overflow:hidden}
  .mm-mbar-fill{position:absolute;left:0;top:0;bottom:0;width:0%;background:var(--blue);transition:width .2s}
  .mm-mbar-fill.hot{background:var(--green)}
  #toast{position:fixed;left:50%;bottom:20px;transform:translate(-50%,16px);opacity:0;pointer-events:none;z-index:10050;padding:10px 18px;border-radius:10px;font-weight:800;font-size:13px;background:#1d2836;color:var(--fg);border:1px solid var(--line);box-shadow:0 8px 24px rgba(0,0,0,.55);transition:opacity .18s ease,transform .18s ease}
  #toast.show{opacity:1;transform:translate(-50%,0)}
  #toast.ok{background:#15803d;border-color:#4ade80;color:#f0fdf4}
  #toast.err{background:#b91c1c;border-color:#f87171;color:#fef2f2}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 6px;border-top:1px solid var(--line)}
  a{color:#7cc4ff;text-decoration:none}
  code{background:#0d1218;padding:2px 6px;border-radius:6px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>
    <span>ARMORY CAM</span>
    <span id="svcBadge" class="pill">Checking…</span>
    <button class="btn" id="saveAllTop" type="button" style="margin-left:auto">Save Camera Settings</button>
  </h1>
  <div id="subInfo" style="color:var(--mut)">Loading OptiPlex status…</div>

  <div id="camGrid" class="grid"></div>

  <div class="card" style="margin-top:14px">
    <h2>Retention, Buffer &amp; Google Drive Sync</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div><label>Full Tape Retention (days)</label><input id="retDays" type="number" min="1" max="90"></div>
      <div><label>Pre-Roll Buffer (seconds)</label><input id="preRoll" type="number" min="30" max="600"></div>
    </div>
    <label>Google Drive Clip Backup Folder (auto-copied on save)</label>
    <input id="driveDir" type="text" placeholder="J:\\My Drive\\Backups\\ArmoryFit Backup\\Clips">
    <div class="ctrl-row" style="margin-top:10px">
      <button class="btn" id="saveBtn" type="button">Save Camera Config &amp; Apply</button>
      <span id="saveMsg" style="color:var(--mut);font-size:12px"></span>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>
      <span>32&quot; Fire TV Wallboard &amp; ADB Auto-Wake</span>
      <span style="display:flex;align-items:center;gap:8px">
        <a href="/wallboard" target="_blank" style="font-size:12px;font-weight:700">Open Wallboard ↗</a>
        <span id="wbBadge" class="pill">UNCONFIGURED</span>
      </span>
    </h2>
    <div id="wbMsg" style="color:var(--mut);font-size:12px;margin-bottom:8px">Permanent URL: <code>/wallboard</code></div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">
      <div><label>Fire TV IP / Hostname</label><input id="wbHost" type="text" placeholder="192.168.1.xxx"></div>
      <div><label>ADB Port</label><input id="wbPort" type="number" min="1" max="65535" value="5555"></div>
      <div>
        <label>Display Client</label>
        <select id="wbClient">
          <option value="silk">Amazon Silk</option>
          <option value="fully">Fully Kiosk Browser</option>
          <option value="custom">Custom App / URL Intent</option>
        </select>
      </div>
      <div><label>Idle Auto-Sleep (minutes)</label><input id="wbIdleMin" type="number" min="1" max="120" value="10"></div>
      <div>
        <label>Wake on Workout Start</label>
        <select id="wbWake"><option value="1">Yes (Auto-Wake)</option><option value="0">No</option></select>
      </div>
      <div>
        <label>Sleep When Workout Ends</label>
        <select id="wbSleep"><option value="1">Yes (30s delay)</option><option value="0">No</option></select>
      </div>
    </div>
    <div class="ctrl-row" style="margin-top:10px">
      <button class="btn" id="wbSaveBtn" type="button">Save Wallboard</button>
      <button class="btn sec" id="wbPairBtn" type="button">Connect / Pair ADB</button>
      <button class="btn ok" id="wbWakeBtn" type="button">Test Wake &amp; Launch</button>
      <button class="btn sec" id="wbSleepBtn" type="button">Sleep TV Now</button>
      <span id="wbOut" style="color:var(--mut);font-size:12px"></span>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>
      <span>Recent Saved Clips</span>
      <span id="calBadge" class="pill">0 / 5 clips for pose calibration</span>
    </h2>
    <div id="clipList" style="color:var(--mut)">No clips saved yet.</div>
  </div>
</div>

<div id="maskModal" class="mask-modal" hidden>
  <div class="mm-top">
    <div class="mm-title">
      <span id="mmTitle">Mirror Mask Editor</span>
      <div class="mm-mbar">
        <div class="mm-mbar-track"><div id="mmMotionBar" class="mm-mbar-fill"></div></div>
        <span id="mmMotionTxt">Motion: 0.0</span>
      </div>
    </div>
    <div id="mmMaskTabs" class="mm-tabs"></div>
    <div class="ctrl-row" style="margin-top:0">
      <button class="btn red sm" id="mmAddBtn" type="button">+ Add Mask</button>
      <button class="btn sec sm" id="mmClearBtn" type="button">Clear All</button>
      <button class="btn sec sm" id="mmHiresBtn" type="button">1080p Still</button>
      <button class="btn sec sm" id="mmRefreshBtn" type="button" hidden>↻ Grab Frame</button>
      <button class="btn ok sm" id="mmSaveBtn" type="button">Save Masks</button>
      <button class="btn sec sm" id="mmCloseBtn" type="button">Done</button>
    </div>
  </div>
  <div class="mm-zoombar">
    <span style="font-size:12px;color:var(--mut)">Zoom:</span>
    <button class="btn sec sm" type="button" data-mmz="1">1×</button>
    <button class="btn sec sm" type="button" data-mmz="2">2×</button>
    <button class="btn sec sm" type="button" data-mmz="3">3×</button>
    <button class="btn sec sm" type="button" data-mmz="5">5×</button>
    <button class="btn sec sm" type="button" data-mmz="8">8×</button>
    <button class="btn sec sm" type="button" id="mmFitMaskBtn">Fit Mask</button>
    <button class="btn sec sm" type="button" id="mmFitCropBtn">Fit Crop</button>
    <input type="range" id="mmZoomRange" min="1" max="8" step="0.1" value="1">
    <span id="mmZoomVal" style="font-size:12px;font-weight:800;min-width:38px;font-variant-numeric:tabular-nums">1.0×</span>
    <span id="mmHint" style="margin-left:auto;font-size:12px;color:var(--mut)">Drag 4 dots to match angled mirror corners · Wheel/pinch to zoom · Drag outside mask to pan</span>
  </div>
  <div id="mmViewport" class="mm-viewport">
    <div id="mmCanvas" class="mm-canvas">
      <canvas id="mmLiveCanvas" class="mm-img"></canvas>
      <img id="mmStream" class="mm-img" alt="Zoomed mirror mask view" hidden>
      <div id="mmCropRef" class="mm-crop-ref"></div>
      <svg id="mmSvg" class="excl-svg" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>
      <div id="mmDots"></div>
    </div>
  </div>
</div>

<div id="rawFeeds" hidden></div>
<div id="toast" role="status" aria-live="polite"></div>

<script>
const $ = (id) => document.getElementById(id);
let cfg = null;
const previewCropped = {};
const aspectLock = {};
const editTarget = {};
let isDirty = false;
let toastTimer = null;
const btnTimers = new WeakMap();

const mmState = {
  open: false,
  camIdx: 0,
  activeMask: 0,
  zoom: 1,
  panX: 0,
  panY: 0,
  hires: false,
  pendingFit: false,
  dragDot: null,
};

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const MIN_SPAN = 0.15;
const MAX_EXCLUSIONS = 6;
const MM_MIN_ZOOM = 1;
const MM_MAX_ZOOM = 8;
const MM_PAN_MARGIN = 56;
const rawFeedUrls = {};

function makeUniqueSlug(rawName) {
  const base = (String(rawName || 'view').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^[._-]+|[._-]+$/g, '')).slice(0, 40) || 'view';
  const taken = new Set((cfg?.cameras || []).map(c => c.slug));
  if (!taken.has(base)) return base;
  let k = 2;
  while (taken.has(`${base}_${k}`)) k += 1;
  return `${base}_${k}`;
}

function sourceSlugOf(cam) {
  return (cam && cam.sourceSlug) ? cam.sourceSlug : (cam ? cam.slug : '');
}

function syncRawFeeds() {
  const box = $('rawFeeds');
  if (!box || !cfg) return;
  const activePhys = new Map();
  for (const c of cfg.cameras) {
    if (!c.sourceSlug && c.rtspUrl) {
      const hasEnabledView = cfg.cameras.some(v => sourceSlugOf(v) === c.slug && v.enabled);
      if (hasEnabledView) activePhys.set(c.slug, c);
    }
  }
  Array.from(box.children).forEach(el => {
    const slug = el.dataset.rawSlug;
    const phys = activePhys.get(slug);
    if (!phys || rawFeedUrls[slug] !== phys.rtspUrl) {
      el.removeAttribute('src');
      el.remove();
      delete rawFeedUrls[slug];
    }
  });
  for (const [slug, phys] of activePhys.entries()) {
    if ($(`rawFeed-${slug}`)) continue;
    const img = document.createElement('img');
    img.id = `rawFeed-${slug}`;
    img.dataset.rawSlug = slug;
    rawFeedUrls[slug] = phys.rtspUrl;
    const setSrc = () => {
      img.src = `/api/stream?camera=${encodeURIComponent(phys.name)}&raw=1&t=${Date.now()}`;
    };
    img.onload = () => {
      img._sized = true;
      if (cfg && cfg.cameras) {
        cfg.cameras.forEach((cam, idx) => {
          if (sourceSlugOf(cam) === slug) syncCropOverlay(idx);
        });
      }
    };
    img.onerror = () => setTimeout(() => {
      if (img.isConnected && rawFeedUrls[slug]) setSrc();
    }, 2500);
    setSrc();
    box.appendChild(img);
  }
}

function normTilt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  const c = clamp(Math.round(n * 10) / 10, -45, 45);
  return Math.abs(c) < 0.05 ? 0 : c;
}

function tiltedSize(w, h, tilt) {
  const t = normTilt(tilt);
  if (t === 0) return [w, h];
  const rad = t * Math.PI / 180;
  const ca = Math.abs(Math.cos(rad));
  const sa = Math.abs(Math.sin(rad));
  const nw = Math.max(2, Math.floor((w * ca + h * sa) / 2) * 2);
  const nh = Math.max(2, Math.floor((w * sa + h * ca) / 2) * 2);
  return [nw, nh];
}

function viewFullDims(cam) {
  const rot = cam ? (cam.rotate || 0) : 0;
  const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
  const sw = (rawImg && rawImg.naturalWidth > 0) ? rawImg.naturalWidth : 1920;
  const sh = (rawImg && rawImg.naturalHeight > 0) ? rawImg.naturalHeight : 1080;
  const rw = (rot === 90 || rot === 270) ? sh : sw;
  const rh = (rot === 90 || rot === 270) ? sw : sh;
  return tiltedSize(rw, rh, cam ? (cam.tilt || 0) : 0);
}

function drawFeedToCanvas(canvas, rawImg, rot, tilt, crop, applyCrop) {
  if (!canvas || !rawImg || !rawImg.complete || rawImg.naturalWidth <= 0 || rawImg.naturalHeight <= 0) return;
  const sw = rawImg.naturalWidth;
  const sh = rawImg.naturalHeight;
  const r = ((rot % 360) + 360) % 360;
  const t = normTilt(tilt);
  const rw = (r === 90 || r === 270) ? sh : sw;
  const rh = (r === 90 || r === 270) ? sw : sh;
  const [fw, fh] = tiltedSize(rw, rh, t);
  let sx = 0, sy = 0, cw = fw, ch = fh;
  if (applyCrop && Array.isArray(crop) && crop.length === 4) {
    sx = clamp(crop[0], 0, 0.95) * fw;
    sy = clamp(crop[1], 0, 0.95) * fh;
    cw = Math.max(2, (clamp(crop[2], 0.05, 1) - clamp(crop[0], 0, 0.95)) * fw);
    ch = Math.max(2, (clamp(crop[3], 0.05, 1) - clamp(crop[1], 0, 0.95)) * fh);
  }
  const tw = Math.max(2, Math.round(cw));
  const th = Math.max(2, Math.round(ch));
  if (canvas.width !== tw) canvas.width = tw;
  if (canvas.height !== th) canvas.height = th;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.save();
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, tw, th);
  ctx.translate(-sx, -sy);
  if (t !== 0) {
    ctx.translate(fw / 2, fh / 2);
    ctx.rotate(t * Math.PI / 180);
    ctx.translate(-rw / 2, -rh / 2);
  }
  if (r === 90) {
    ctx.translate(rw, 0);
    ctx.rotate(Math.PI / 2);
  } else if (r === 180) {
    ctx.translate(rw, rh);
    ctx.rotate(Math.PI);
  } else if (r === 270) {
    ctx.translate(0, rh);
    ctx.rotate(-Math.PI / 2);
  }
  ctx.drawImage(rawImg, 0, 0, sw, sh);
  ctx.restore();
}

function drawAllCanvases() {
  if (!cfg || !cfg.cameras) return;
  cfg.cameras.forEach((c, idx) => {
    const rawImg = $(`rawFeed-${sourceSlugOf(c)}`);
    if (rawImg && !rawImg._sized && rawImg.naturalWidth > 0) {
      rawImg._sized = true;
      syncCropOverlay(idx);
    }
    const canvas = $(`stream-${idx}`);
    if (canvas && rawImg) {
      drawFeedToCanvas(canvas, rawImg, c.rotate || 0, c.tilt || 0, c.crop, !!previewCropped[idx]);
    }
  });
  if (mmState.open && !mmState.hires) {
    const cam = cfg.cameras[mmState.camIdx];
    const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
    const mmCanvas = $('mmLiveCanvas');
    if (cam && rawImg && mmCanvas) {
      if (mmState.pendingFit && rawImg.naturalWidth > 0) {
        mmState.pendingFit = false;
        if ((cam.exclusions || []).length) fitMaskInModal();
        else fitCropInModal();
      }
      drawFeedToCanvas(mmCanvas, rawImg, cam.rotate || 0, cam.tilt || 0, [0, 0, 1, 1], false);
    }
  }
}

function normQuad(item) {
  if (!Array.isArray(item) || item.length !== 4) return null;
  if (item.every(v => typeof v === 'number' && Number.isFinite(v))) {
    const x0 = clamp(Math.min(item[0], item[2]), 0, 1);
    const y0 = clamp(Math.min(item[1], item[3]), 0, 1);
    const x1 = clamp(Math.max(item[0], item[2]), 0, 1);
    const y1 = clamp(Math.max(item[1], item[3]), 0, 1);
    return [[+x0.toFixed(4), +y0.toFixed(4)], [+x1.toFixed(4), +y0.toFixed(4)], [+x1.toFixed(4), +y1.toFixed(4)], [+x0.toFixed(4), +y1.toFixed(4)]];
  }
  if (item.every(pt => Array.isArray(pt) && pt.length === 2 && Number.isFinite(Number(pt[0])) && Number.isFinite(Number(pt[1])))) {
    return item.map(pt => [+clamp(Number(pt[0]), 0, 1).toFixed(4), +clamp(Number(pt[1]), 0, 1).toFixed(4)]);
  }
  return null;
}

function defaultQuadFor(cam, k, viewNorm = null) {
  const b = viewNorm || (cam && Array.isArray(cam.crop) ? cam.crop : [0, 0, 1, 1]);
  const bw = Math.max(0.12, b[2] - b[0]);
  const bh = Math.max(0.12, b[3] - b[1]);
  const qw = Math.min(0.32, bw * 0.34);
  const qh = Math.min(0.42, bh * 0.44);
  const sx = (k % 3) * (bw * 0.08);
  const sy = Math.floor(k / 3) * (bh * 0.09);
  const x0 = clamp(b[0] + bw * 0.52 - sx, 0.01, 0.99 - qw);
  const y0 = clamp(b[1] + bh * 0.16 + sy, 0.01, 0.99 - qh);
  const x1 = +(x0 + qw).toFixed(4);
  const y1 = +(y0 + qh).toFixed(4);
  return [[+x0.toFixed(4), +y0.toFixed(4)], [x1, +y0.toFixed(4)], [x1, y1], [+x0.toFixed(4), y1]];
}

function quadBounds(q) {
  const xs = q.map(p => p[0]);
  const ys = q.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function quadCentroid(q) {
  return [
    (q[0][0] + q[1][0] + q[2][0] + q[3][0]) / 4,
    (q[0][1] + q[1][1] + q[2][1] + q[3][1]) / 4,
  ];
}

function segsCross(a, b, c, d) {
  const ccw = (p1, p2, p3) => (p3[1] - p1[1]) * (p2[0] - p1[0]) - (p2[1] - p1[1]) * (p3[0] - p1[0]);
  const d1 = ccw(c, d, a), d2 = ccw(c, d, b), d3 = ccw(a, b, c), d4 = ccw(a, b, d);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function quadHasCrossing(q) {
  if (!q || q.length !== 4) return false;
  return segsCross(q[0], q[1], q[2], q[3]) || segsCross(q[1], q[2], q[3], q[0]);
}

function showToast(msg, kind = 'ok') {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'show ' + kind;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 2400);
}

function allSaveButtons() {
  return [$('saveAllTop'), $('saveBtn'), $('wbSaveBtn'), $('mmSaveBtn'), ...document.querySelectorAll('[data-save]')].filter(Boolean);
}

function markDirty(flag = true) {
  isDirty = flag;
  allSaveButtons().forEach(b => b.classList.toggle('dirty', flag && !b.classList.contains('saved')));
}

function setBtnFeedback(btn, state, label, restoreMs = 0) {
  if (!btn) return;
  if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent;
  const oldT = btnTimers.get(btn);
  if (oldT) { clearTimeout(oldT); btnTimers.delete(btn); }
  btn.classList.remove('saving', 'saved', 'save-err');
  if (state) btn.classList.add(state);
  btn.textContent = label || btn.dataset.origLabel;
  btn.disabled = (state === 'saving');
  if (restoreMs > 0) {
    const tid = setTimeout(() => {
      btn.classList.remove('saving', 'saved', 'save-err');
      btn.textContent = btn.dataset.origLabel;
      btn.disabled = false;
      btn.classList.toggle('dirty', isDirty);
      btnTimers.delete(btn);
    }, restoreMs);
    btnTimers.set(btn, tid);
  }
}

function renderQuadLayer(svgEl, dotsEl, excls, opts = {}) {
  if (!svgEl || !dotsEl) return;
  const activeIdx = opts.activeIdx ?? -1;
  const dragDot = opts.dragDot || null;
  const showNums = !!opts.showNums;

  svgEl.innerHTML = excls.map((q, ei) => {
    const pts = q.map(p => `${(p[0] * 1000).toFixed(1)},${(p[1] * 1000).toFixed(1)}`).join(' ');
    const cls = ['quad-poly', activeIdx === ei ? 'active' : '', quadHasCrossing(q) ? 'crossed' : ''].filter(Boolean).join(' ');
    return `<polygon class="${cls}" fill-rule="evenodd" vector-effect="non-scaling-stroke" data-excl-idx="${ei}" points="${pts}"></polygon>`;
  }).join('');

  dotsEl.innerHTML = excls.map((q, ei) => {
    const c = quadCentroid(q);
    const tag = `<div class="quad-tag" style="left:${(c[0] * 100).toFixed(3)}%;top:${(c[1] * 100).toFixed(3)}%">MASK ${ei + 1}</div>`;
    const dots = q.map((p, ci) => {
      const isDrag = dragDot && dragDot.ei === ei && dragDot.ci === ci;
      return `<div class="quad-dot${isDrag ? ' dragging' : ''}" data-excl-idx="${ei}" data-corner-idx="${ci}" style="left:${(p[0] * 100).toFixed(3)}%;top:${(p[1] * 100).toFixed(3)}%"><div class="quad-ring"></div>${showNums ? `<span class="quad-num">${ci + 1}</span>` : ''}</div>`;
    }).join('');
    return tag + dots;
  }).join('');
}

function cropZoomFactor(c) {
  const w = Math.max(MIN_SPAN, c[2] - c[0]);
  const h = Math.max(MIN_SPAN, c[3] - c[1]);
  return Math.max(1 / w, 1 / h);
}

function setZoomAroundCenter(idx, zoom) {
  const c = cfg.cameras[idx].crop;
  const cx = (c[0] + c[2]) / 2;
  const cy = (c[1] + c[3]) / 2;
  const span = clamp(1 / zoom, MIN_SPAN, 1);
  const half = span / 2;
  let x0 = clamp(cx - half, 0, 1 - span);
  let y0 = clamp(cy - half, 0, 1 - span);
  cfg.cameras[idx].crop = [
    +x0.toFixed(4), +y0.toFixed(4), +(x0 + span).toFixed(4), +(y0 + span).toFixed(4)
  ];
  markDirty();
  syncCropOverlay(idx);
}

function syncCropOverlay(idx) {
  const cam = cfg.cameras[idx];
  const c = cam.crop;
  const rawExcls = Array.isArray(cam.exclusions) ? cam.exclusions : (cam.exclusions = []);
  const excls = rawExcls.map(normQuad).filter(Boolean);
  cam.exclusions = excls;

  let mode = editTarget[idx] || 'crop';
  if (mode === 'excl' && !excls.length) {
    mode = 'crop';
    editTarget[idx] = 'crop';
  }
  const rot = cam.rotate || 0;
  const tilt = normTilt(cam.tilt || 0);
  const stage = $(`stage-${idx}`);
  const layer = $(`layer-${idx}`);
  if (!stage || !layer) return;
  stage.classList.toggle('rot90', rot === 90);
  stage.classList.toggle('rot270', rot === 270);

  const [stageFw, stageFh] = viewFullDims(cam);
  const showEditor = !previewCropped[idx];
  stage.style.aspectRatio = showEditor ? `${stageFw} / ${stageFh}` : '';
  layer.hidden = !showEditor;

  const isFullCrop = c[0] <= 0.001 && c[1] <= 0.001 && c[2] >= 0.999 && c[3] >= 0.999;

  const boxEl = $(`box-${idx}`);
  boxEl.style.cssText = `left:${c[0]*100}%;top:${c[1]*100}%;width:${(c[2]-c[0])*100}%;height:${(c[3]-c[1])*100}%;`;
  boxEl.classList.toggle('passive', mode === 'excl');

  const exclWrap = $(`exclWrap-${idx}`);
  const exclSvg = $(`exclSvg-${idx}`);
  const exclDots = $(`exclDots-${idx}`);
  if (exclWrap && exclSvg && exclDots) {
    exclWrap.classList.toggle('passive', mode !== 'excl');
    renderQuadLayer(exclSvg, exclDots, excls, {
      activeIdx: mode === 'excl' ? (mmState.open && mmState.camIdx === idx ? mmState.activeMask : 0) : -1,
      showNums: false,
    });
  }

  const lPct = c[0] * 100, tPct = c[1] * 100, wPct = (c[2] - c[0]) * 100, hPct = (c[3] - c[1]) * 100;
  $(`scT-${idx}`).style.cssText = `left:0;top:0;right:0;height:${tPct}%;`;
  $(`scB-${idx}`).style.cssText = `left:0;top:${tPct + hPct}%;right:0;bottom:0;`;
  $(`scL-${idx}`).style.cssText = `left:0;top:${tPct}%;width:${lPct}%;height:${hPct}%;`;
  $(`scR-${idx}`).style.cssText = `left:${lPct + wPct}%;top:${tPct}%;right:0;height:${hPct}%;`;

  const z = Math.min(3.5, Math.max(1.0, cropZoomFactor(c)));
  const slider = $(`zoom-${idx}`);
  if (slider) slider.value = z.toFixed(2);
  const [srcW, srcH] = viewFullDims(cam);
  const outW = Math.round((c[2] - c[0]) * srcW);
  const outH = Math.round((c[3] - c[1]) * srcH);
  const maskLabel = excls.length ? `${excls.length} mirror mask${excls.length === 1 ? '' : 's'} (Red)` : 'No mirror masks';
  const tiltTag = tilt !== 0 ? `${tilt > 0 ? '+' : ''}${tilt.toFixed(1)}°` : '';
  const rotLabel = tilt === 0 ? `Rotate ${rot}°` : (rot === 0 ? `Angle ${tiltTag}` : `Rotate ${rot}° (${tiltTag})`);
  $(`cropInfo-${idx}`).textContent = isFullCrop
    ? `Full frame · ${srcW}×${srcH} · ${rotLabel} · ${maskLabel}`
    : `Zoom ${z.toFixed(2)}× · ~${outW}×${outH} · ${rotLabel} · ${maskLabel}`;
  const tiltSlider = $(`tilt-${idx}`);
  if (tiltSlider) tiltSlider.value = String(tilt);
  const tiltNum = $(`tiltNum-${idx}`);
  if (tiltNum && document.activeElement !== tiltNum) tiltNum.value = tilt.toFixed(1);
  const prevBtn = $(`prevBtn-${idx}`);
  if (prevBtn) prevBtn.classList.toggle('on', !!previewCropped[idx]);
  const rotBtn = $(`rotBtn-${idx}`);
  if (rotBtn) rotBtn.textContent = `Rotate ${rot}°`;
  const mCrop = $(`modeCrop-${idx}`);
  const mExcl = $(`modeExcl-${idx}`);
  const zExcl = $(`zoomExcl-${idx}`);
  const addExcl = $(`addExcl-${idx}`);
  if (mCrop) mCrop.classList.toggle('on', mode === 'crop');
  if (mExcl) {
    mExcl.classList.toggle('on', mode === 'excl');
    mExcl.disabled = !excls.length;
    mExcl.textContent = excls.length ? `Edit Masks (${excls.length})` : 'Edit Masks';
  }
  if (zExcl) zExcl.disabled = !excls.length;
  if (addExcl) addExcl.disabled = excls.length >= MAX_EXCLUSIONS;
}

function wireCropDrag(idx) {
  const layer = $(`layer-${idx}`);
  if (!layer) return;
  let drag = null;

  layer.addEventListener('pointerdown', (e) => {
    const mode = editTarget[idx] || 'crop';
    const dotEl = e.target.closest('.quad-dot');
    const polyEl = e.target.closest('.quad-poly');
    const handleEl = e.target.closest('[data-handle]');
    const cropEl = e.target.closest('.crop-box:not(.passive)');
    if (mode === 'excl' && (dotEl || polyEl)) {
      e.preventDefault();
      const rect = layer.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      layer.setPointerCapture(e.pointerId);
      const ei = Number((dotEl || polyEl).dataset.exclIdx);
      const q = cfg.cameras[idx].exclusions[ei];
      if (!q) return;
      drag = {
        pid: e.pointerId,
        dragKey: dotEl ? 'quad-dot' : 'quad-move',
        exclIdx: ei,
        cornerIdx: dotEl ? Number(dotEl.dataset.cornerIdx) : -1,
        sx: e.clientX,
        sy: e.clientY,
        rect,
        origQuad: q.map(p => [...p]),
      };
      return;
    }
    if (!cropEl) return;
    e.preventDefault();
    const rect = layer.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    layer.setPointerCapture(e.pointerId);
    drag = {
      pid: e.pointerId,
      dragKey: 'crop',
      minSpan: MIN_SPAN,
      mode: handleEl ? handleEl.dataset.handle : 'move',
      sx: e.clientX,
      sy: e.clientY,
      rect,
      orig: [...(cfg.cameras[idx].crop || [0, 0, 1, 1])],
    };
  });

  layer.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.pid) return;
    if (drag.dragKey === 'quad-dot') {
      const nx = +clamp((e.clientX - drag.rect.left) / drag.rect.width, 0, 1).toFixed(4);
      const ny = +clamp((e.clientY - drag.rect.top) / drag.rect.height, 0, 1).toFixed(4);
      cfg.cameras[idx].exclusions[drag.exclIdx][drag.cornerIdx] = [nx, ny];
      markDirty();
      syncCropOverlay(idx);
      return;
    }
    if (drag.dragKey === 'quad-move') {
      const dxRaw = (e.clientX - drag.sx) / drag.rect.width;
      const dyRaw = (e.clientY - drag.sy) / drag.rect.height;
      const [bx0, by0, bx1, by1] = quadBounds(drag.origQuad);
      const dx = clamp(dxRaw, -bx0, 1 - bx1);
      const dy = clamp(dyRaw, -by0, 1 - by1);
      cfg.cameras[idx].exclusions[drag.exclIdx] = drag.origQuad.map(p => [+(p[0] + dx).toFixed(4), +(p[1] + dy).toFixed(4)]);
      markDirty();
      syncCropOverlay(idx);
      return;
    }

    const dx = (e.clientX - drag.sx) / drag.rect.width;
    const dy = (e.clientY - drag.sy) / drag.rect.height;
    let [x0, y0, x1, y1] = drag.orig;
    const ow = x1 - x0, oh = y1 - y0;
    const minS = drag.minSpan || MIN_SPAN;

    if (drag.mode === 'move') {
      x0 = clamp(x0 + dx, 0, 1 - ow);
      y0 = clamp(y0 + dy, 0, 1 - oh);
      x1 = x0 + ow;
      y1 = y0 + oh;
    } else {
      if (drag.mode.includes('w')) x0 = clamp(x0 + dx, 0, x1 - minS);
      if (drag.mode.includes('e')) x1 = clamp(x1 + dx, x0 + minS, 1);
      if (drag.mode.includes('n')) y0 = clamp(y0 + dy, 0, y1 - minS);
      if (drag.mode.includes('s')) y1 = clamp(y1 + dy, y0 + minS, 1);

      const lock = aspectLock[idx];
      if (lock) {
        const [srcW, srcH] = viewFullDims(cfg.cameras[idx]);
        const targetNormRatio = lock * (srcH / srcW);
        let nw = x1 - x0;
        let nh = nw / targetNormRatio;
        if (nh < minS) { nh = minS; nw = nh * targetNormRatio; }
        if (drag.mode.includes('n')) {
          y0 = y1 - nh;
          if (y0 < 0) { y0 = 0; nh = y1; nw = nh * targetNormRatio; }
        } else {
          y1 = y0 + nh;
          if (y1 > 1) { y1 = 1; nh = 1 - y0; nw = nh * targetNormRatio; }
        }
        if (drag.mode.includes('w')) x0 = clamp(x1 - nw, 0, x1 - minS);
        else x1 = clamp(x0 + nw, x0 + minS, 1);
      }
    }
    cfg.cameras[idx].crop = [+x0.toFixed(4), +y0.toFixed(4), +x1.toFixed(4), +y1.toFixed(4)];
    markDirty();
    syncCropOverlay(idx);
  });

  const endDrag = (e) => { if (drag && e.pointerId === drag.pid) drag = null; };
  layer.addEventListener('pointerup', endDrag);
  layer.addEventListener('pointercancel', endDrag);
}

function modalBaseMetrics() {
  const vp = $('mmViewport');
  const img = $('mmStream');
  const cam = cfg?.cameras?.[mmState.camIdx];
  const rot = cam ? (cam.rotate || 0) : 0;
  const tilt = cam ? normTilt(cam.tilt || 0) : 0;
  const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
  const vw = Math.max(120, vp ? vp.clientWidth : 800);
  const vh = Math.max(120, vp ? vp.clientHeight : 500);
  const rawW = (rawImg && rawImg.naturalWidth > 0) ? rawImg.naturalWidth : 1920;
  const rawH = (rawImg && rawImg.naturalHeight > 0) ? rawImg.naturalHeight : 1080;
  const rw = (rot === 90 || rot === 270) ? rawH : rawW;
  const rh = (rot === 90 || rot === 270) ? rawW : rawH;
  const [fw, fh] = tiltedSize(rw, rh, tilt);
  let ratio = fw / fh;
  if (mmState.hires && img && img.naturalWidth > 0 && img.naturalHeight > 0) {
    ratio = img.naturalWidth / img.naturalHeight;
  }
  const maxW = Math.max(100, vw - 24);
  const maxH = Math.max(100, vh - 24);
  let baseW, baseH;
  if (maxW / maxH > ratio) {
    baseH = maxH;
    baseW = maxH * ratio;
  } else {
    baseW = maxW;
    baseH = maxW / ratio;
  }
  return { vw, vh, baseW, baseH };
}

function clampModalPan() {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const dispW = baseW * mmState.zoom;
  const dispH = baseH * mmState.zoom;
  if (dispW <= vw) mmState.panX = (vw - dispW) / 2;
  else mmState.panX = clamp(mmState.panX, vw - dispW - MM_PAN_MARGIN, MM_PAN_MARGIN);
  if (dispH <= vh) mmState.panY = (vh - dispH) / 2;
  else mmState.panY = clamp(mmState.panY, vh - dispH - MM_PAN_MARGIN, MM_PAN_MARGIN);
}

function setModalZoom(nextZoom, anchorClientX = null, anchorClientY = null) {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const oldDispW = baseW * mmState.zoom;
  const oldDispH = baseH * mmState.zoom;
  const vp = $('mmViewport');
  let ax = vw / 2, ay = vh / 2;
  if (vp && anchorClientX !== null && anchorClientY !== null) {
    const r = vp.getBoundingClientRect();
    ax = anchorClientX - r.left;
    ay = anchorClientY - r.top;
  }
  const ux = (ax - mmState.panX) / Math.max(1, oldDispW);
  const uy = (ay - mmState.panY) / Math.max(1, oldDispH);
  mmState.zoom = clamp(nextZoom, MM_MIN_ZOOM, MM_MAX_ZOOM);
  const newDispW = baseW * mmState.zoom;
  const newDispH = baseH * mmState.zoom;
  mmState.panX = ax - ux * newDispW;
  mmState.panY = ay - uy * newDispH;
  clampModalPan();
  renderMaskModal();
}

function centerModalOnNormBox(x0, y0, x1, y1, fillFraction = 0.62, minZ = 1.8, maxZ = 6.5) {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const bw = Math.max(0.05, x1 - x0);
  const bh = Math.max(0.05, y1 - y0);
  const zx = (vw * fillFraction) / (baseW * bw);
  const zy = (vh * fillFraction) / (baseH * bh);
  mmState.zoom = clamp(Math.min(zx, zy), minZ, maxZ);
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  mmState.panX = vw / 2 - cx * baseW * mmState.zoom;
  mmState.panY = vh / 2 - cy * baseH * mmState.zoom;
  clampModalPan();
  renderMaskModal();
}

function fitMaskInModal() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const excls = cam?.exclusions || [];
  if (!excls.length) {
    fitCropInModal();
    return;
  }
  const ei = clamp(mmState.activeMask, 0, excls.length - 1);
  mmState.activeMask = ei;
  const [x0, y0, x1, y1] = quadBounds(excls[ei]);
  centerModalOnNormBox(x0, y0, x1, y1, 0.58, 2.0, 6.5);
}

function fitCropInModal() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const c = cam?.crop || [0, 0, 1, 1];
  centerModalOnNormBox(c[0], c[1], c[2], c[3], 0.86, 1.0, 6.0);
}

function modalVisibleNormBox() {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const dispW = Math.max(1, baseW * mmState.zoom);
  const dispH = Math.max(1, baseH * mmState.zoom);
  const x0 = clamp((0 - mmState.panX) / dispW, 0, 0.9);
  const y0 = clamp((0 - mmState.panY) / dispH, 0, 0.9);
  const x1 = clamp((vw - mmState.panX) / dispW, x0 + 0.1, 1);
  const y1 = clamp((vh - mmState.panY) / dispH, y0 + 0.1, 1);
  return [x0, y0, x1, y1];
}

function updateModalMediaSrc() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const img = $('mmStream');
  const liveCvs = $('mmLiveCanvas');
  if (!cam || !img || !liveCvs) return;
  if (mmState.hires) {
    liveCvs.hidden = true;
    img.hidden = false;
    img.src = `/api/snapshot?camera=${encodeURIComponent(cam.name)}&raw=1&hires=1&rotate=${cam.rotate || 0}&tilt=${normTilt(cam.tilt || 0)}&t=${Date.now()}`;
  } else {
    img.hidden = true;
    img.removeAttribute('src');
    liveCvs.hidden = false;
    drawAllCanvases();
  }
  const hBtn = $('mmHiresBtn');
  const rBtn = $('mmRefreshBtn');
  if (hBtn) {
    hBtn.classList.toggle('on', mmState.hires);
    hBtn.textContent = mmState.hires ? '▶ Live Stream' : '1080p Still';
  }
  if (rBtn) rBtn.hidden = !mmState.hires;
}

function renderMaskModal() {
  if (!mmState.open || !cfg) return;
  const cam = cfg.cameras[mmState.camIdx];
  if (!cam) return;
  const excls = cam.exclusions || [];
  if (mmState.activeMask >= excls.length) mmState.activeMask = Math.max(0, excls.length - 1);

  $('mmTitle').textContent = `Mirror Mask Editor · ${cam.name}`;
  const tabsEl = $('mmMaskTabs');
  if (tabsEl) {
    tabsEl.innerHTML = excls.map((_, ei) => `
      <div class="mm-tab${ei === mmState.activeMask ? ' on' : ''}" data-mm-tab="${ei}">
        <span>Mask ${ei + 1}</span>
        <button class="mm-tab-del" type="button" data-mm-del="${ei}" title="Remove Mask ${ei + 1}">×</button>
      </div>
    `).join('');
  }
  const addBtn = $('mmAddBtn');
  if (addBtn) addBtn.disabled = excls.length >= MAX_EXCLUSIONS;
  const fitBtn = $('mmFitMaskBtn');
  if (fitBtn) fitBtn.disabled = !excls.length;

  clampModalPan();
  const { baseW, baseH } = modalBaseMetrics();
  const dispW = baseW * mmState.zoom;
  const dispH = baseH * mmState.zoom;
  const canvas = $('mmCanvas');
  if (canvas) {
    canvas.style.width = dispW.toFixed(1) + 'px';
    canvas.style.height = dispH.toFixed(1) + 'px';
    canvas.style.left = mmState.panX.toFixed(1) + 'px';
    canvas.style.top = mmState.panY.toFixed(1) + 'px';
  }

  const c = cam.crop || [0, 0, 1, 1];
  const cropRef = $('mmCropRef');
  if (cropRef) {
    cropRef.style.cssText = `left:${c[0]*100}%;top:${c[1]*100}%;width:${(c[2]-c[0])*100}%;height:${(c[3]-c[1])*100}%;`;
  }

  renderQuadLayer($('mmSvg'), $('mmDots'), excls, {
    activeIdx: mmState.activeMask,
    dragDot: mmState.dragDot,
    showNums: true,
  });

  const zr = $('mmZoomRange');
  const zv = $('mmZoomVal');
  if (zr) zr.value = mmState.zoom.toFixed(1);
  if (zv) zv.textContent = mmState.zoom.toFixed(1) + '×';

  const activeQ = excls[mmState.activeMask];
  const hint = $('mmHint');
  if (hint) {
    if (activeQ && quadHasCrossing(activeQ)) {
      hint.innerHTML = `<span style="color:var(--amber);font-weight:800">Corners crossed on Mask ${mmState.activeMask + 1} — drag dots to uncross</span>`;
    } else if (mmState.dragDot && excls[mmState.dragDot.ei]) {
      const pt = excls[mmState.dragDot.ei][mmState.dragDot.ci];
      const [srcW, srcH] = viewFullDims(cam);
      hint.textContent = `Mask ${mmState.dragDot.ei + 1} · Corner ${mmState.dragDot.ci + 1} · px (${Math.round(pt[0] * srcW)}, ${Math.round(pt[1] * srcH)})`;
    } else {
      hint.textContent = 'Drag 4 dots onto mirror corners · Drag mask to slide · Wheel/pinch or drag background to pan';
    }
  }
}

function openMaskModal(idx, focusMaskIdx = 0) {
  if (!cfg || !cfg.cameras[idx]) return;
  if (previewCropped[idx]) {
    previewCropped[idx] = false;
  }
  editTarget[idx] = 'excl';
  syncCropOverlay(idx);

  mmState.open = true;
  mmState.camIdx = idx;
  mmState.activeMask = focusMaskIdx;
  mmState.hires = false;
  mmState.dragDot = null;
  mmState.pendingFit = true;

  $('maskModal').hidden = false;
  document.body.style.overflow = 'hidden';
  updateModalMediaSrc();
  if ((cfg.cameras[idx].exclusions || []).length) fitMaskInModal();
  else fitCropInModal();
  drawAllCanvases();
}

function closeMaskModal() {
  if (!mmState.open) return;
  mmState.open = false;
  mmState.dragDot = null;
  $('maskModal').hidden = true;
  document.body.style.overflow = '';
  const img = $('mmStream');
  if (img) img.removeAttribute('src');
  syncCropOverlay(mmState.camIdx);
}

function wireMaskModal() {
  const vp = $('mmViewport');
  const img = $('mmStream');
  if (!vp || !img) return;

  img.addEventListener('load', () => {
    if (!mmState.open) return;
    if (mmState.pendingFit) {
      mmState.pendingFit = false;
      if ((cfg.cameras[mmState.camIdx]?.exclusions || []).length) fitMaskInModal();
      else fitCropInModal();
    } else if (mmState.hires) {
      renderMaskModal();
    }
  });

  const activePointers = new Map();
  let drag = null;
  let pinch = null;

  vp.addEventListener('pointerdown', (e) => {
    if (!mmState.open) return;
    activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (activePointers.size === 2) {
      drag = null;
      mmState.dragDot = null;
      const pts = Array.from(activePointers.values());
      const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      const midX = (pts[0].x + pts[1].x) / 2;
      const midY = (pts[0].y + pts[1].y) / 2;
      const vpRect = vp.getBoundingClientRect();
      const { baseW, baseH } = modalBaseMetrics();
      const ax = midX - vpRect.left;
      const ay = midY - vpRect.top;
      pinch = {
        startDist: Math.max(10, dist),
        startZoom: mmState.zoom,
        ux: (ax - mmState.panX) / Math.max(1, baseW * mmState.zoom),
        uy: (ay - mmState.panY) / Math.max(1, baseH * mmState.zoom),
      };
      return;
    }

    e.preventDefault();
    vp.setPointerCapture(e.pointerId);
    const camIdx = mmState.camIdx;
    const dotEl = e.target.closest('.quad-dot');
    const polyEl = e.target.closest('.quad-poly');
    const canvas = $('mmCanvas');
    const rect = canvas.getBoundingClientRect();

    if (dotEl && rect.width && rect.height) {
      const ei = Number(dotEl.dataset.exclIdx);
      const ci = Number(dotEl.dataset.cornerIdx);
      mmState.activeMask = ei;
      mmState.dragDot = { ei, ci };
      drag = { pid: e.pointerId, type: 'dot', ei, ci };
      renderMaskModal();
      return;
    }

    if (polyEl && rect.width && rect.height) {
      const ei = Number(polyEl.dataset.exclIdx);
      const q = cfg.cameras[camIdx].exclusions[ei];
      if (!q) return;
      mmState.activeMask = ei;
      drag = {
        pid: e.pointerId,
        type: 'move',
        ei,
        sx: e.clientX,
        sy: e.clientY,
        w: rect.width,
        h: rect.height,
        origQuad: q.map(p => [...p]),
      };
      renderMaskModal();
      return;
    }

    vp.classList.add('panning');
    drag = {
      pid: e.pointerId,
      type: 'pan',
      sx: e.clientX,
      sy: e.clientY,
      origPanX: mmState.panX,
      origPanY: mmState.panY,
    };
  });

  vp.addEventListener('pointermove', (e) => {
    if (!mmState.open) return;
    if (activePointers.has(e.pointerId)) activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pinch && activePointers.size >= 2) {
      const pts = Array.from(activePointers.values());
      const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      const midX = (pts[0].x + pts[1].x) / 2;
      const midY = (pts[0].y + pts[1].y) / 2;
      const vpRect = vp.getBoundingClientRect();
      const { baseW, baseH } = modalBaseMetrics();
      mmState.zoom = clamp(pinch.startZoom * (dist / pinch.startDist), MM_MIN_ZOOM, MM_MAX_ZOOM);
      mmState.panX = (midX - vpRect.left) - pinch.ux * baseW * mmState.zoom;
      mmState.panY = (midY - vpRect.top) - pinch.uy * baseH * mmState.zoom;
      clampModalPan();
      renderMaskModal();
      return;
    }

    if (!drag || e.pointerId !== drag.pid) return;
    const camIdx = mmState.camIdx;
    if (drag.type === 'dot') {
      const rect = $('mmCanvas').getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const nx = +clamp((e.clientX - rect.left) / rect.width, 0, 1).toFixed(4);
      const ny = +clamp((e.clientY - rect.top) / rect.height, 0, 1).toFixed(4);
      cfg.cameras[camIdx].exclusions[drag.ei][drag.ci] = [nx, ny];
      markDirty();
      renderMaskModal();
      syncCropOverlay(camIdx);
      return;
    }
    if (drag.type === 'move') {
      const dxRaw = (e.clientX - drag.sx) / drag.w;
      const dyRaw = (e.clientY - drag.sy) / drag.h;
      const [bx0, by0, bx1, by1] = quadBounds(drag.origQuad);
      const dx = clamp(dxRaw, -bx0, 1 - bx1);
      const dy = clamp(dyRaw, -by0, 1 - by1);
      cfg.cameras[camIdx].exclusions[drag.ei] = drag.origQuad.map(p => [+(p[0] + dx).toFixed(4), +(p[1] + dy).toFixed(4)]);
      markDirty();
      renderMaskModal();
      syncCropOverlay(camIdx);
      return;
    }
    if (drag.type === 'pan') {
      mmState.panX = drag.origPanX + (e.clientX - drag.sx);
      mmState.panY = drag.origPanY + (e.clientY - drag.sy);
      clampModalPan();
      renderMaskModal();
    }
  });

  const endPtr = (e) => {
    activePointers.delete(e.pointerId);
    if (activePointers.size < 2) pinch = null;
    if (drag && e.pointerId === drag.pid) {
      drag = null;
      mmState.dragDot = null;
      vp.classList.remove('panning');
      if (mmState.open) renderMaskModal();
    }
  };
  vp.addEventListener('pointerup', endPtr);
  vp.addEventListener('pointercancel', endPtr);

  vp.addEventListener('wheel', (e) => {
    if (!mmState.open) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    setModalZoom(mmState.zoom * factor, e.clientX, e.clientY);
  }, { passive: false });

  $('mmMaskTabs').addEventListener('click', (e) => {
    const delBtn = e.target.closest('[data-mm-del]');
    const camIdx = mmState.camIdx;
    const list = cfg.cameras[camIdx].exclusions || [];
    if (delBtn) {
      const ei = Number(delBtn.dataset.mmDel);
      if (ei >= 0 && ei < list.length) list.splice(ei, 1);
      if (mmState.activeMask >= list.length) mmState.activeMask = Math.max(0, list.length - 1);
      if (!list.length) editTarget[camIdx] = 'crop';
      markDirty();
      syncCropOverlay(camIdx);
      renderMaskModal();
      return;
    }
    const tab = e.target.closest('[data-mm-tab]');
    if (tab) {
      mmState.activeMask = Number(tab.dataset.mmTab);
      fitMaskInModal();
    }
  });

  $('mmAddBtn').onclick = () => {
    const cam = cfg.cameras[mmState.camIdx];
    const list = (cam.exclusions ||= []);
    if (list.length >= MAX_EXCLUSIONS) return;
    list.push(defaultQuadFor(cam, list.length, modalVisibleNormBox()));
    mmState.activeMask = list.length - 1;
    editTarget[mmState.camIdx] = 'excl';
    markDirty();
    syncCropOverlay(mmState.camIdx);
    fitMaskInModal();
  };

  $('mmClearBtn').onclick = () => {
    cfg.cameras[mmState.camIdx].exclusions = [];
    mmState.activeMask = 0;
    editTarget[mmState.camIdx] = 'crop';
    markDirty();
    syncCropOverlay(mmState.camIdx);
    renderMaskModal();
  };

  $('mmHiresBtn').onclick = () => {
    mmState.hires = !mmState.hires;
    updateModalMediaSrc();
  };

  $('mmRefreshBtn').onclick = () => {
    if (mmState.hires) updateModalMediaSrc();
  };

  $('mmSaveBtn').onclick = () => saveConfig($('mmSaveBtn'));
  $('mmCloseBtn').onclick = closeMaskModal;
  $('mmFitMaskBtn').onclick = fitMaskInModal;
  $('mmFitCropBtn').onclick = fitCropInModal;
  $('mmZoomRange').oninput = (e) => setModalZoom(Number(e.target.value));

  document.querySelectorAll('[data-mmz]').forEach(btn => {
    btn.onclick = () => setModalZoom(Number(btn.dataset.mmz));
  });

  window.addEventListener('resize', () => { if (mmState.open) renderMaskModal(); });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mmState.open) {
      closeMaskModal();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveConfig(mmState.open ? $('mmSaveBtn') : $('saveAllTop'));
    }
  });
}

function renderCameras() {
  const physList = cfg.cameras.filter(x => !x.sourceSlug);
  const physBySlug = new Map(physList.map(x => [x.slug, x]));

  $('camGrid').innerHTML = cfg.cameras.map((c, idx) => {
    const isVirt = !!c.sourceSlug;
    const parent = isVirt ? physBySlug.get(c.sourceSlug) : null;
    const headLeft = isVirt
      ? `<input type="text" class="cam-name-inp" id="name-${idx}" value="${esc(c.name)}" aria-label="View name">` +
        `<span class="pill">via ${esc(parent ? parent.name : c.sourceSlug)}</span>`
      : `<span>${esc(c.name)}</span>`;
    const headRight = isVirt
      ? `<span class="pill live" id="camState-${idx}">CONNECTING</span>` +
        `<button class="btn red sm" type="button" data-delview="${idx}" title="Remove view">Remove</button>`
      : `<button class="btn sec sm" type="button" data-addview="${idx}" title="Add another crop/mask view from this camera">+ Add View</button>` +
        `<span class="pill live" id="camState-${idx}">CONNECTING</span>`;
    const srcSelect = isVirt ? `
      <label>Source Camera</label>
      <select id="src-${idx}">
        ${physList.map(p => `<option value="${esc(p.slug)}"${p.slug === c.sourceSlug ? ' selected' : ''}>${esc(p.name)} (Shared Feed · No Extra RTSP)</option>`).join('')}
        <option value="">Own RTSP Camera (Physical)</option>
      </select>` : '';

    return `
    <div class="card">
      <h2>
        <div class="cam-head-l">${headLeft}</div>
        <div class="cam-head-r">${headRight}</div>
      </h2>
      <div class="stage" id="stage-${idx}">
        <canvas class="stream" id="stream-${idx}"></canvas>
        <div class="level-grid"></div>
        <div class="crop-layer" id="layer-${idx}">
          <div class="scrim" id="scT-${idx}"></div>
          <div class="scrim" id="scB-${idx}"></div>
          <div class="scrim" id="scL-${idx}"></div>
          <div class="scrim" id="scR-${idx}"></div>
          <div class="crop-box" id="box-${idx}">
            <span class="box-tag">CROP</span>
            <div class="handle nw" data-handle="nw"></div>
            <div class="handle ne" data-handle="ne"></div>
            <div class="handle sw" data-handle="sw"></div>
            <div class="handle se" data-handle="se"></div>
          </div>
          <div class="excl-wrap" id="exclWrap-${idx}">
            <svg class="excl-svg" id="exclSvg-${idx}" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>
            <div id="exclDots-${idx}"></div>
          </div>
        </div>
      </div>
      <div class="crop-info">
        <span id="cropInfo-${idx}">Full frame</span>
        <span id="energyTxt-${idx}">Motion: 0.0</span>
      </div>
      <div class="mbar-wrap">
        <div class="mbar-fill" id="mbar-${idx}"></div>
        <div class="mbar-th" id="mth-${idx}" style="left:${clamp((c.motionThreshold / 20) * 100, 2, 98)}%"></div>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Edit:</span>
        <button class="btn sec sm on" type="button" data-emode="${idx}:crop" id="modeCrop-${idx}">Frame Crop (Blue)</button>
        <button class="btn sec sm" type="button" data-emode="${idx}:excl" id="modeExcl-${idx}" disabled>Edit Masks</button>
        <button class="btn sec sm" type="button" data-zoomexcl="${idx}" id="zoomExcl-${idx}" disabled>⤢ Zoom Masks</button>
        <button class="btn red sm" type="button" data-addexcl="${idx}" id="addExcl-${idx}">+ Mask Mirror (Red)</button>
        <button class="btn sec sm" type="button" data-exclclear="${idx}">Clear Masks</button>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Zoom</span>
        <input type="range" id="zoom-${idx}" min="1" max="3" step="0.05" value="1">
        <button class="btn sec sm" type="button" data-reset="${idx}">1×</button>
        <button class="btn sec sm" type="button" data-rot="${idx}" id="rotBtn-${idx}">Rotate 0°</button>
        <button class="btn sec sm" type="button" data-prev="${idx}" id="prevBtn-${idx}">Preview Crop</button>
      </div>
      <div class="ctrl-row" id="tiltRow-${idx}">
        <span style="font-size:12px;color:var(--mut)">Angle</span>
        <button class="btn sec sm" type="button" data-tiltstep="${idx}:-0.5" title="Rotate counter-clockwise 0.5°">−0.5°</button>
        <input type="range" id="tilt-${idx}" min="-45" max="45" step="0.5" value="${normTilt(c.tilt || 0)}">
        <button class="btn sec sm" type="button" data-tiltstep="${idx}:0.5" title="Rotate clockwise 0.5°">+0.5°</button>
        <input type="number" class="tilt-num" id="tiltNum-${idx}" min="-45" max="45" step="0.1" value="${normTilt(c.tilt || 0).toFixed(1)}" aria-label="Custom rotate angle in degrees">
        <button class="btn sec sm" type="button" data-tiltreset="${idx}" title="Reset angle to 0°">0°</button>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Box Ratio:</span>
        <button class="btn sec sm on" type="button" data-asp="${idx}:free">Free</button>
        <button class="btn sec sm" type="button" data-asp="${idx}:1.7778">16:9</button>
        <button class="btn sec sm" type="button" data-asp="${idx}:0.5625">9:16</button>
        <button class="btn ok sm" type="button" data-save="${idx}" style="margin-left:auto">Save</button>
      </div>
      ${srcSelect}
      <div id="rtspRow-${idx}"${isVirt ? ' hidden' : ''}>
        <label>RTSP URL</label>
        <input type="text" id="rtsp-${idx}" value="${esc(c.rtspUrl || '')}" placeholder="rtsp://thingino:thingino@192.168.1.xxx:554/ch0">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        <div><label>Motion Threshold</label><input type="number" step="0.2" id="thresh-${idx}" value="${c.motionThreshold}"></div>
        <div>
          <label>Role</label>
          <select id="prim-${idx}">
            <option value="1"${c.primary !== false ? ' selected' : ''}>Primary (Auto + Pick)</option>
            <option value="0"${c.primary === false ? ' selected' : ''}>Secondary / Pinned</option>
          </select>
        </div>
        <div><label>Enabled</label><select id="en-${idx}"><option value="1"${c.enabled?' selected':''}>Yes</option><option value="0"${!c.enabled?' selected':''}>No</option></select></div>
      </div>
    </div>`;
  }).join('');

  cfg.cameras.forEach((c, idx) => {
    syncCropOverlay(idx);
    wireCropDrag(idx);
    $(`zoom-${idx}`).oninput = (e) => setZoomAroundCenter(idx, Number(e.target.value));
    const stageEl = $(`stage-${idx}`);
    const tiltSlider = $(`tilt-${idx}`);
    const tiltNum = $(`tiltNum-${idx}`);
    const showLevel = (on) => { if (stageEl) stageEl.classList.toggle('leveling', on); };
    if (tiltSlider) {
      tiltSlider.oninput = (e) => setViewTilt(idx, e.target.value, false);
      tiltSlider.addEventListener('pointerdown', () => showLevel(true));
      tiltSlider.addEventListener('focus', () => showLevel(true));
      tiltSlider.addEventListener('pointerup', () => showLevel(false));
      tiltSlider.addEventListener('pointercancel', () => showLevel(false));
      tiltSlider.addEventListener('blur', () => showLevel(false));
    }
    if (tiltNum) {
      tiltNum.oninput = (e) => {
        const raw = String(e.target.value || '').trim();
        if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return;
        setViewTilt(idx, raw, false);
      };
      tiltNum.onchange = () => {
        setViewTilt(idx, tiltNum.value, false);
        tiltNum.value = normTilt(cfg.cameras[idx].tilt || 0).toFixed(1);
      };
      tiltNum.addEventListener('focus', () => showLevel(true));
      tiltNum.addEventListener('blur', () => {
        showLevel(false);
        tiltNum.value = normTilt(cfg.cameras[idx].tilt || 0).toFixed(1);
      });
    }
    $(`thresh-${idx}`).oninput = (e) => {
      const th = Number(e.target.value) || 2.2;
      cfg.cameras[idx].motionThreshold = th;
      const tick = $(`mth-${idx}`);
      if (tick) tick.style.left = clamp((th / 20) * 100, 2, 98) + '%';
      markDirty();
    };
    const nameInp = $(`name-${idx}`);
    if (nameInp) nameInp.oninput = () => markDirty();
    const srcSel = $(`src-${idx}`);
    if (srcSel) {
      srcSel.onchange = () => {
        const nextSrc = srcSel.value;
        cfg.cameras[idx].sourceSlug = nextSrc;
        const rRow = $(`rtspRow-${idx}`);
        if (rRow) rRow.hidden = !!nextSrc;
        markDirty();
      };
    }
    $(`rtsp-${idx}`).oninput = () => markDirty();
    $(`prim-${idx}`).onchange = () => markDirty();
    $(`en-${idx}`).onchange = () => markDirty();
  });
  syncRawFeeds();
  drawAllCanvases();
}

const levelTimers = new Map();
function setViewTilt(idx, val, flashGrid = false) {
  if (!cfg || !cfg.cameras || !cfg.cameras[idx]) return;
  cfg.cameras[idx].tilt = normTilt(val);
  markDirty();
  syncCropOverlay(idx);
  drawAllCanvases();
  if (flashGrid) {
    const st = $(`stage-${idx}`);
    if (st) {
      st.classList.add('leveling');
      const old = levelTimers.get(idx);
      if (old) clearTimeout(old);
      levelTimers.set(idx, setTimeout(() => { st.classList.remove('leveling'); }, 900));
    }
  }
}

$('camGrid').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.addview !== undefined) {
    const idx = Number(b.dataset.addview);
    const parent = cfg.cameras[idx];
    if (!parent) return;
    const countOnParent = cfg.cameras.filter(x => sourceSlugOf(x) === parent.slug).length;
    const defName = `${parent.name.replace(/\\s+cam$/i, '')} View ${countOnParent + 1}`;
    const rawName = window.prompt(`New view name (from ${parent.name}):`, defName);
    if (!rawName || !rawName.trim()) return;
    const name = rawName.trim();
    const slug = makeUniqueSlug(name);
    cfg.cameras.push({
      name,
      slug,
      sourceSlug: parent.slug,
      rtspUrl: '',
      enabled: true,
      primary: false,
      motionThreshold: 2.2,
      roi: [0, 0, 1, 1],
      exclusions: [],
      crop: [0, 0, 1, 1],
      rotate: parent.rotate || 0,
      tilt: 0,
    });
    renderCameras();
    await saveConfig(null);
    showToast(`✓ Added view "${name}"`, 'ok');
    return;
  }
  if (b.dataset.delview !== undefined) {
    const idx = Number(b.dataset.delview);
    const c = cfg.cameras[idx];
    if (!c || !c.sourceSlug) return;
    if (!window.confirm(`Remove view "${c.name}"? Raw camera tape is unaffected.`)) return;
    if (mmState.open) closeMaskModal();
    cfg.cameras.splice(idx, 1);
    renderCameras();
    await saveConfig(null);
    showToast(`Removed "${c.name}"`, 'ok');
    return;
  }
  if (b.dataset.reset !== undefined) {
    const idx = Number(b.dataset.reset);
    cfg.cameras[idx].crop = [0, 0, 1, 1];
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    return;
  }
  if (b.dataset.rot !== undefined) {
    const idx = Number(b.dataset.rot);
    cfg.cameras[idx].rotate = ((cfg.cameras[idx].rotate || 0) + 90) % 360;
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    if (previewCropped[idx]) saveConfig();
    return;
  }
  if (b.dataset.tiltstep !== undefined) {
    const [iStr, dStr] = b.dataset.tiltstep.split(':');
    const idx = Number(iStr);
    const delta = Number(dStr) || 0;
    setViewTilt(idx, (cfg.cameras[idx].tilt || 0) + delta, true);
    return;
  }
  if (b.dataset.tiltreset !== undefined) {
    const idx = Number(b.dataset.tiltreset);
    setViewTilt(idx, 0, true);
    return;
  }
  if (b.dataset.prev !== undefined) {
    const idx = Number(b.dataset.prev);
    previewCropped[idx] = !previewCropped[idx];
    syncCropOverlay(idx);
    drawAllCanvases();
    if (previewCropped[idx]) saveConfig();
    return;
  }
  if (b.dataset.asp !== undefined) {
    const [iStr, ratioStr] = b.dataset.asp.split(':');
    const idx = Number(iStr);
    aspectLock[idx] = ratioStr === 'free' ? null : Number(ratioStr);
    b.parentElement.querySelectorAll('[data-asp]').forEach(x => x.classList.toggle('on', x === b));
    if (aspectLock[idx]) setZoomAroundCenter(idx, cropZoomFactor(cfg.cameras[idx].crop));
    return;
  }
  if (b.dataset.addexcl !== undefined) {
    const idx = Number(b.dataset.addexcl);
    const list = (cfg.cameras[idx].exclusions ||= []);
    let focusIdx = Math.max(0, list.length - 1);
    if (list.length < MAX_EXCLUSIONS) {
      list.push(defaultQuadFor(cfg.cameras[idx], list.length));
      focusIdx = list.length - 1;
      markDirty();
    }
    openMaskModal(idx, focusIdx);
    return;
  }
  if (b.dataset.zoomexcl !== undefined) {
    const idx = Number(b.dataset.zoomexcl);
    if (!(cfg.cameras[idx].exclusions || []).length) return;
    openMaskModal(idx, 0);
    return;
  }
  if (b.dataset.exclclear !== undefined) {
    const idx = Number(b.dataset.exclclear);
    cfg.cameras[idx].exclusions = [];
    editTarget[idx] = 'crop';
    markDirty();
    syncCropOverlay(idx);
    return;
  }
  if (b.dataset.emode !== undefined) {
    const [iStr, mode] = b.dataset.emode.split(':');
    const idx = Number(iStr);
    if (mode === 'excl') {
      if (!(cfg.cameras[idx].exclusions || []).length) return;
      openMaskModal(idx, 0);
      return;
    }
    editTarget[idx] = mode;
    syncCropOverlay(idx);
    return;
  }
  if (b.dataset.save !== undefined) {
    saveConfig(b);
  }
});

async function saveConfig(triggerBtn = null) {
  if (!cfg) return;
  if (triggerBtn) setBtnFeedback(triggerBtn, 'saving', 'Saving…');

  const cameras = cfg.cameras.map((c, idx) => {
    const nameInp = $(`name-${idx}`);
    const srcSel = $(`src-${idx}`);
    const sourceSlug = srcSel ? srcSel.value : (c.sourceSlug || '');
    return {
      ...c,
      name: nameInp ? (nameInp.value.trim() || c.name) : c.name,
      sourceSlug,
      rtspUrl: sourceSlug ? '' : ($(`rtsp-${idx}`) ? $(`rtsp-${idx}`).value.trim() : c.rtspUrl),
      primary: $(`prim-${idx}`) ? ($(`prim-${idx}`).value === '1') : (c.primary !== false),
      motionThreshold: $(`thresh-${idx}`) ? (Number($(`thresh-${idx}`).value) || 2.2) : c.motionThreshold,
      tilt: normTilt(c.tilt || 0),
      enabled: $(`en-${idx}`) ? ($(`en-${idx}`).value === '1') : !!c.enabled,
    };
  });
  const wallboard = {
    ...(cfg.wallboard || {}),
    fireTvHost: $('wbHost').value.trim(),
    adbPort: Number($('wbPort').value) || 5555,
    displayClient: $('wbClient').value || 'silk',
    idleSleepSec: clamp(Math.round((Number($('wbIdleMin').value) || 10) * 60), 60, 7200),
    wakeOnStart: $('wbWake').value === '1',
    sleepOnStop: $('wbSleep').value === '1',
  };
  const body = {
    retentionDays: Number($('retDays').value) || 14,
    preRollSec: Number($('preRoll').value) || 120,
    driveBackupDir: $('driveDir').value.trim(),
    cameras,
    wallboard,
  };
  try {
    const resp = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const res = await resp.json();
    if (!res || !res.ok) throw new Error('Save rejected');
    const stamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    $('saveMsg').textContent = `Saved & applied live! (${stamp})`;
    $('wbOut').textContent = `Saved! (${stamp})`;
    cfg.cameras = cameras;
    cfg.wallboard = wallboard;
    syncRawFeeds();
    markDirty(false);
    if (triggerBtn) {
      setBtnFeedback(triggerBtn, 'saved', '✓ Saved!', 2200);
      showToast(triggerBtn === $('mmSaveBtn') ? '✓ Masks saved & applied live' : '✓ Saved & applied live', 'ok');
    }
  } catch (err) {
    $('saveMsg').textContent = 'Save failed';
    $('wbOut').textContent = 'Save failed';
    if (triggerBtn) {
      setBtnFeedback(triggerBtn, 'save-err', '✗ Save Failed', 3000);
      showToast('✗ Save failed — check OptiPlex server', 'err');
    }
  }
}

async function pollStatus() {
  try {
    const st = await fetch('/api/status').then(r => r.json());
    const badge = $('svcBadge');
    badge.textContent = st.recording ? (st.paused ? 'PAUSED' : 'RECORDING') : 'STANDBY RING';
    badge.className = 'pill ' + (st.recording ? 'ok' : 'live');
    $('subInfo').innerHTML = `Storage: <code>${st.disk.root}</code> · <b>${st.disk.freeGb} GB</b> free / ${st.disk.totalGb} GB · Drag crop corners or <b>4-dot Mirror Masks</b>, or click <b>+ Add View</b> to crop extra stations from a camera.`;
    const bySlug = new Map((st.cameras || []).map(sc => [sc.slug, sc]));
    (cfg?.cameras || []).forEach((cam, idx) => {
      const c = bySlug.get(cam.slug) || st.cameras[idx];
      if (!c) return;
      const sEl = $(`camState-${idx}`);
      if (sEl) {
        sEl.textContent = c.connected ? 'LIVE 25 FPS' : 'RECONNECTING';
        sEl.className = 'pill ' + (c.connected ? 'ok' : '');
      }
      const eEl = $(`energyTxt-${idx}`);
      const mEl = $(`mbar-${idx}`);
      if (eEl) eEl.textContent = `Motion: ${c.latestEnergy.toFixed(2)} / ${c.motionThreshold}`;
      if (mEl) {
        mEl.style.width = clamp((c.latestEnergy / 20) * 100, 0, 100) + '%';
        mEl.classList.toggle('hot', c.latestEnergy >= c.motionThreshold);
      }
      if (mmState.open && mmState.camIdx === idx) {
        const mmTxt = $('mmMotionTxt');
        const mmBar = $('mmMotionBar');
        if (mmTxt) mmTxt.textContent = `Motion: ${c.latestEnergy.toFixed(2)} / ${c.motionThreshold}`;
        if (mmBar) {
          mmBar.style.width = clamp((c.latestEnergy / 20) * 100, 0, 100) + '%';
          mmBar.classList.toggle('hot', c.latestEnergy >= c.motionThreshold);
        }
      }
    });
    if (st.wallboard) {
      const wb = st.wallboard;
      const wbBadge = $('wbBadge');
      const stTxt = (wb.adbState || 'unconfigured').toUpperCase();
      wbBadge.textContent = wb.workoutActive ? `LIVE · ${(wb.mode || 'WORKOUT').toUpperCase()}` : stTxt;
      wbBadge.className = 'pill ' + ((wb.adbState === 'awake' || wb.adbState === 'ready' || wb.workoutActive) ? 'ok' : 'live');
      const parts = [
        `URL: <code>${wb.wallboardUrl}</code>`,
        `SSE Displays: <b>${wb.clients}</b>`,
        `Mode: <b>${wb.mode || 'standby'}</b>`,
        wb.adbBinFound ? (wb.fireTvModel ? `TV: <b>${wb.fireTvModel}</b>` : (wb.fireTvHost ? `TV: <code>${wb.fireTvHost}</code>` : 'TV IP not set yet')) : 'adb.exe missing',
      ];
      if (wb.pendingSleepSec > 0) parts.push(`Sleep in ${wb.pendingSleepSec}s`);
      if (wb.lastError) parts.push(`<span style="color:var(--amber)">${wb.lastError}</span>`);
      else if (wb.lastAction) parts.push(`Last: ${wb.lastAction}`);
      $('wbMsg').innerHTML = parts.join(' · ');
    }
    if (st.calibration) {
      const cal = st.calibration;
      const calEl = $('calBadge');
      if (calEl) {
        calEl.textContent = cal.nextMilestone
          ? `${cal.clipCount} / ${cal.nextMilestone} clips for pose calibration`
          : `${cal.clipCount} clips · 50+ full calibration ready`;
        calEl.className = 'pill ' + (cal.clipCount >= 5 ? 'ok' : '');
      }
    }
  } catch {}
}

async function pollClips() {
  try {
    const clips = await fetch('/api/clips').then(r => r.json());
    if (clips.clips && clips.clips.length) {
      $('clipList').innerHTML = `<table><thead><tr><th>Clip</th><th>Camera</th><th>Duration</th><th>Size</th><th>Drive</th></tr></thead><tbody>` +
        clips.clips.map(x => `<tr><td><a href="${x.lanUrl}" target="_blank">${x.fileName}</a></td><td>${x.camera}</td><td>${x.durationSec}s</td><td>${(x.bytes/1048576).toFixed(1)} MB</td><td>${x.driveUrl ? `<a href="${x.driveUrl}" target="_blank">Drive ↗</a>` : 'Syncing…'}</td></tr>`).join('') +
        `</tbody></table>`;
    } else {
      $('clipList').textContent = 'No clips saved yet.';
    }
  } catch {}
}

async function init() {
  cfg = await fetch('/api/config').then(r => r.json());
  (cfg.cameras || []).forEach(c => {
    c.exclusions = (Array.isArray(c.exclusions) ? c.exclusions : []).map(normQuad).filter(Boolean);
  });
  $('retDays').value = cfg.retentionDays;
  $('preRoll').value = cfg.preRollSec;
  $('driveDir').value = cfg.driveBackupDir || '';
  const wb = cfg.wallboard || {};
  $('wbHost').value = wb.fireTvHost || '';
  $('wbPort').value = wb.adbPort || 5555;
  $('wbClient').value = wb.displayClient || 'silk';
  $('wbIdleMin').value = Math.max(1, Math.round((wb.idleSleepSec || 600) / 60));
  $('wbWake').value = wb.wakeOnStart === false ? '0' : '1';
  $('wbSleep').value = wb.sleepOnStop === false ? '0' : '1';
  renderCameras();
  wireMaskModal();
  setInterval(drawAllCanvases, 100);
  await Promise.all([pollStatus(), pollClips()]);
  setInterval(pollStatus, 1000);
  setInterval(pollClips, 6000);
}

['retDays', 'preRoll', 'driveDir', 'wbHost', 'wbPort', 'wbIdleMin'].forEach(id => {
  $(id).addEventListener('input', () => markDirty());
});
['wbClient', 'wbWake', 'wbSleep'].forEach(id => {
  $(id).addEventListener('change', () => markDirty());
});

$('saveBtn').onclick = () => saveConfig($('saveBtn'));
$('saveAllTop').onclick = () => saveConfig($('saveAllTop'));
$('wbSaveBtn').onclick = () => saveConfig($('wbSaveBtn'));
$('wbPairBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Connecting ADB…';
  const r = await fetch('/api/wallboard/adb/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
  $('wbOut').textContent = r.message || r.state || 'Done';
};
$('wbWakeBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Waking TV & launching…';
  const r = await fetch('/api/wallboard/wake', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ force: true, sync: true }) }).then(x => x.json());
  $('wbOut').textContent = (r.adb && r.adb.message) || 'Sent wake';
};
$('wbSleepBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Sleeping TV…';
  const r = await fetch('/api/wallboard/sleep', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ reason: 'manual', delaySec: 0, sync: true }) }).then(x => x.json());
  $('wbOut').textContent = (r.adb && r.adb.message) || 'Sent sleep';
};
init();
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
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp

    @app.route("/<path:dummy>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def handle_options(dummy: str = "") -> Response:
        return Response(status=204)

    @app.get("/favicon.ico")
    def favicon() -> Response:
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
            ret_days = service.retention_days
        return jsonify(
            {
                "service": "armory-cam",
                "serverEpochMs": int(time.time() * 1000),
                "recording": rec,
                "paused": paused,
                "retentionDays": ret_days,
                "cameras": cams,
            }
        )

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(service.status_snapshot())

    @app.get("/api/config")
    def api_get_config() -> Response:
        with service.lock:
            cams = [
                {
                    **c.to_dict(),
                    "connected": bool(service.cam_connected.get(service._source_slug_for(c), False)),
                    "latestEnergy": float(service.latest_energy.get(c.slug, 0.0)),
                }
                for c in service.cameras
            ]
            payload = {
                "port": service.port,
                "retentionDays": service.retention_days,
                "preRollSec": service.pre_roll_sec,
                "driveBackupDir": service.drive_backup_dir,
                "cameras": cams,
                "wallboard": service.wallboard_cfg.to_dict(),
                "disk": service.disk_stats(),
            }
        return jsonify(payload)

    @app.post("/api/config")
    def api_post_config() -> Response:
        body = request.get_json(silent=True) or {}
        service.update_config(body)
        return jsonify({"ok": True})

    @app.get("/wallboard")
    @app.get("/wallboard/")
    def wallboard_page() -> Response:
        resp = Response(service.wallboard.get_html(), mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/wallboard/state")
    def wallboard_state() -> Response:
        resp = Response(service.wallboard.current_state_json(), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/wallboard/events")
    def wallboard_events() -> Response:
        def generate():
            with service.wallboard.cond:
                service.wallboard.sse_clients += 1
                last_seq = service.wallboard.seq
            try:
                initial = service.wallboard.current_state_json()
                yield f"data: {initial}\n\n".encode("utf-8")
                while not service._stop_event.is_set():
                    payload = None
                    with service.wallboard.cond:
                        if service.wallboard.seq == last_seq:
                            service.wallboard.cond.wait(timeout=15.0)
                        if service.wallboard.seq != last_seq:
                            last_seq = service.wallboard.seq
                            payload = service.wallboard.latest_json or service.wallboard.current_state_json()
                    if payload:
                        yield f"data: {payload}\n\n".encode("utf-8")
                    else:
                        yield b": ping\n\n"
            finally:
                with service.wallboard.cond:
                    service.wallboard.sse_clients = max(0, service.wallboard.sse_clients - 1)

        resp = Response(generate(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.post("/wallboard/voice")
    def wallboard_voice() -> Response:
        body = request.get_json(silent=True) or {}
        transcript = str(body.get("transcript") or "").strip()
        if not transcript:
            return jsonify({"ok": False, "error": "Empty transcript"}), 400
        with service.wallboard.cond:
            target_base = service.wallboard.source_url
        if not target_base:
            return jsonify({"ok": False, "error": "No active Pixel wallboard source URL"}), 503
        req = urllib.request.Request(
            f"{target_base}/api/voice",
            data=json.dumps({"transcript": transcript}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw = resp.read()
                return Response(raw or b'{"ok":true}', status=resp.status, mimetype="application/json")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/api/wallboard/html")
    def api_wallboard_html() -> Response:
        body = request.get_json(silent=True) or {}
        html = body.get("html")
        if not isinstance(html, str) or not service.wallboard.save_uploaded_html(html):
            return jsonify({"ok": False, "error": "Invalid HTML"}), 400
        return jsonify({"ok": True})

    @app.post("/api/wallboard/state")
    def api_wallboard_state() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        workout_active = bool(body.get("workoutActive", False))
        source_url = str(body.get("sourceUrl") or "")
        service.wallboard.update_state(state_obj, workout_active=workout_active, source_url=source_url)
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        if workout_active:
            with service.adb.lock:
                service.adb.pending_sleep_at_ms = None
                service.adb.pending_sleep_reason = ""
                should_wake = (
                    cfg.enabled
                    and cfg.wake_on_start
                    and bool(service.adb.serial(cfg))
                    and not service.adb.tv_awake_requested
                )
            if should_wake:
                service.trigger_adb_wake_async(force=False)
        return jsonify({"ok": True})

    @app.post("/api/wallboard/wake")
    def api_wallboard_wake() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        source_url = str(body.get("sourceUrl") or "")
        force = bool(body.get("force", False))
        sync_flag = bool(body.get("sync", False))
        service.wallboard.mark_active_wake(state_obj=state_obj, source_url=source_url)
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
            url = service.effective_wallboard_url()
        if not (cfg.enabled and cfg.wake_on_start) and not force:
            adb_res: dict[str, Any] = {"ok": False, "skipped": True, "message": "Wake on workout start disabled"}
        elif sync_flag:
            adb_res = service.adb.wake_and_launch(cfg, url, force=force)
        else:
            service.trigger_adb_wake_async(force=force)
            adb_res = {"queued": True, "state": service.adb.adb_state, "message": "Wake queued on OptiPlex"}
        return jsonify(
            {
                "ok": True,
                "wallboardUrl": url,
                "fireTvHost": cfg.fire_tv_host,
                "adb": adb_res,
            }
        )

    @app.post("/api/wallboard/sleep")
    def api_wallboard_sleep() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        reason = str(body.get("reason") or "workout_finished")
        delay_sec = max(0.0, float(body.get("delaySec", 0) or 0))
        sync_flag = bool(body.get("sync", False))
        service.wallboard.update_state(state_obj, workout_active=False)
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        if reason != "manual" and not cfg.sleep_on_stop:
            return jsonify(
                {
                    "ok": True,
                    "scheduledDelaySec": 0,
                    "adb": {"ok": False, "skipped": True, "message": "Auto-sleep disabled"},
                }
            )
        if delay_sec == 0 and sync_flag:
            adb_res = service.adb.sleep_now(cfg, reason=reason)
        elif delay_sec > 0:
            adb_res = service.adb.schedule_sleep(cfg, delay_sec=delay_sec, reason=reason)
        else:
            service.trigger_adb_sleep_async(reason=reason, delay_sec=0.0)
            adb_res = {"queued": True, "state": service.adb.adb_state, "message": "Sleep queued on OptiPlex"}
        return jsonify({"ok": True, "scheduledDelaySec": delay_sec, "adb": adb_res})

    @app.post("/api/wallboard/adb/test")
    def api_wallboard_adb_test() -> Response:
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        res = service.adb.connect_and_probe(cfg)
        return jsonify(res)

    @app.get("/api/clips")
    def api_clips() -> Response:
        try:
            limit = max(1, min(200, int(request.args.get("limit", 30))))
        except (TypeError, ValueError):
            limit = 30
        return jsonify({"clips": service.recent_clips(limit)})

    @app.post("/api/clip/delete")
    def api_clip_delete() -> Response:
        body = request.get_json(silent=True) or {}
        res = service.delete_clip(
            lan_url=str(body.get("lanUrl") or ""),
            file_name=str(body.get("fileName") or ""),
        )
        return jsonify(res)

    @app.get("/api/snapshot")
    def api_snapshot() -> Response:
        cam = request.args.get("camera")
        raw = request.args.get("raw") in ("1", "true", "yes")
        hires = request.args.get("hires") in ("1", "true", "yes")
        rot_arg = request.args.get("rotate")
        tilt_arg = request.args.get("tilt")
        rot_ov: int | None = None
        if rot_arg is not None:
            rot_ov = normalize_rotate(rot_arg)
        tilt_ov: float | None = None
        if tilt_arg is not None:
            tilt_ov = normalize_tilt(tilt_arg)
        jpg = service.get_snapshot_jpeg(
            cam,
            raw=raw,
            hires=hires,
            rotate_override=rot_ov,
            tilt_override=tilt_ov,
        )
        if not jpg:
            return Response(status=404)
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/stream")
    def api_stream() -> Response:
        cam_name = request.args.get("camera")
        cropped = request.args.get("cropped") in ("1", "true", "yes")
        raw = request.args.get("raw") in ("1", "true", "yes")
        with service.lock:
            cam = service._find_camera(cam_name)
            if not cam or not cam.enabled:
                return Response(status=404)
            media_slug = service._source_slug_for(cam)
            src_cam = service._source_camera(cam)
            if not src_cam.rtsp_url:
                return Response(status=404)
        slug = cam.slug
        ch = service.live_hub.get_channel(media_slug)

        def generate():
            last_seq = 0
            last_frame_at = time.time()
            while True:
                with ch.cond:
                    if ch.seq == last_seq:
                        ch.cond.wait(timeout=1.5)
                    if ch.stop_flag:
                        break
                    if ch.seq == last_seq:
                        if time.time() - last_frame_at > 12.0:
                            break
                        continue
                    last_seq = ch.seq
                    raw_jpg = ch.latest_jpeg
                last_frame_at = time.time()

                payload = raw_jpg
                if not raw:
                    with service.lock:
                        cur = service._find_camera(slug) or cam
                        rot = cur.rotate
                        tilt = cur.tilt
                        crop = cur.crop
                    if rot != 0 or tilt != 0.0 or (cropped and crop != (0.0, 0.0, 1.0, 1.0)):
                        img = cv2.imdecode(np.frombuffer(raw_jpg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            img = apply_frame_transform(img, crop, rot, apply_crop=cropped, tilt=tilt)
                            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
                            if ok:
                                payload = buf.tobytes()

                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(payload)).encode("ascii")
                    + b"\r\n\r\n"
                    + payload
                    + b"\r\n"
                )

        resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
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
        cam = request.args.get("camera")
        min_dur_raw = request.args.get("minDurSec")
        min_dur_sec = float(min_dur_raw) if min_dur_raw is not None else None
        bursts = service.bursts_for_session(sid, around_ms=around_ms, camera=cam, min_dur_sec=min_dur_sec)
        return jsonify({"bursts": bursts})

    @app.get("/api/tape/timeline")
    def api_tape_timeline() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        buckets_raw = request.args.get("buckets")
        buckets = int(buckets_raw) if buckets_raw else 80
        return jsonify(service.tape_timeline(cam, sid, buckets=buckets))

    @app.get("/api/tape/pack")
    def api_tape_pack() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        s_raw = request.args.get("startMs")
        e_raw = request.args.get("endMs")
        c_raw = request.args.get("count")
        w_raw = request.args.get("w")
        start_ms = int(float(s_raw)) if s_raw is not None and s_raw != "" else None
        end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else None
        count = int(c_raw) if c_raw else 80
        max_w = int(w_raw) if w_raw else DEFAULT_SCRUB_THUMB_W
        resp = jsonify(
            service.tape_frame_pack(
                cam,
                sid,
                start_ms=start_ms,
                end_ms=end_ms,
                count=count,
                max_w=max_w,
            )
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/tape/frame")
    def api_tape_frame() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        ts_raw = request.args.get("tsMs")
        ts_ms = int(float(ts_raw)) if ts_raw is not None else int(time.time() * 1000)
        w_raw = request.args.get("w")
        max_w = int(w_raw) if w_raw else 480
        jpg = service.get_tape_frame_jpeg(cam, sid, ts_ms, max_w=max_w)
        if not jpg:
            return Response(status=404)
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    @app.get("/api/tape/angles")
    def api_tape_angles() -> Response:
        sid = request.args.get("sessionId", "")
        s_raw = request.args.get("startMs")
        e_raw = request.args.get("endMs")
        try:
            if s_raw is None or s_raw == "":
                raise ValueError("missing startMs")
            start_ms = int(float(s_raw))
            end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else start_ms + 5000
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid startMs/endMs"}), 400
        cams = service.tape_angles_for_window(sid, start_ms, end_ms)
        with service.lock:
            ret_days = service.retention_days
        resp = jsonify({"ok": True, "retentionDays": ret_days, "cameras": cams})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/tape/preview")
    def api_tape_preview() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or "")
        cam = str(body.get("camera") or "").strip()
        s_raw = body.get("startMs")
        e_raw = body.get("endMs")
        try:
            if not cam or s_raw is None or s_raw == "":
                raise ValueError("missing camera or startMs")
            start_ms = int(float(s_raw))
            end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else start_ms + 5000
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid camera or startMs/endMs"}), 400
        prev = service.cut_preview(sid, start_ms, end_ms, cam)
        if not prev:
            return jsonify({"ok": False, "error": "No alternate camera tape for this timestamp"}), 404
        return jsonify(prev)

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
        cam_raw = body.get("camera")
        cam_override = str(cam_raw).strip() if cam_raw else None

        samples = service.session_samples(sid)
        primary_cams = {c.name for c in service.cameras if c.enabled and c.primary}
        if not cam_override and primary_cams:
            prim_samples = [s for s in samples if s.get("camera") in primary_cams]
            if prim_samples:
                samples = prim_samples
        min_thresh = min((c.motion_threshold for c in service.cameras), default=DEFAULT_MOTION_THRESHOLD)
        bursts = cluster_motion_bursts(
            samples,
            threshold=min_thresh,
            camera=cam_override,
            resolve_overlap=(cam_override is None),
        )
        start_ms, end_ms, cam = select_pr_window(bursts, set_at_ms=set_at_ms, prev_set_at_ms=prev_ms)
        clip = service.cut_clip(
            session_id=sid,
            ex_name=f"{ex_name}_PR",
            start_ms=start_ms,
            end_ms=end_ms,
            camera_override=(cam_override or cam),
        )
        if not clip:
            return jsonify({"ok": False, "error": "No PR video segments found"}), 404
        return jsonify(clip)

    def _serve_mp4_from_root(root: Path, relpath: str) -> Response:
        base = root.resolve()
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

    @app.get("/clips/<path:relpath>")
    def serve_clip(relpath: str) -> Response:
        return _serve_mp4_from_root(service.clips_root, relpath)

    @app.get("/previews/<path:relpath>")
    def serve_preview(relpath: str) -> Response:
        return _serve_mp4_from_root(service.previews_root, relpath)

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
