"""Overnight Post-Workout Voice Dissection & Safe Revertible Optimizer for The Armory.

Runs at 02:00 AM daily via Windows Scheduled Task (`ArmoryVoiceNightlyAudit`):
  1. Smart-skips rest / non-strength days (zero CPU/GPU work, zero morning buzz).
  2. Dissects the full strength workout's room mic (`aud_*.m4a`) + paired Denon Line-In
     reference (`ref_*.m4a`, passed through `LineInAecProcessor`) using heavyweight offline
     GPU Whisper (`medium.en`, `beam_size=5`) to find every spoken `"Hey Armory..."` command.
  3. Matches 1-to-1 against live telemetry (`S:\\ArmoryCam\\logs\\voice_events.jsonl`) to
     score Tier-1 instant ducks, Tier-2 AEC-VAD rescues, misses, false ducks, and false emits.
  4. Sweeps real-world streaming parameters (`wakeThreshold`, `armory_v0.1` vs
     `hey_armory_v0.1`, `aecResOverSub`, `aecResGMin`, `aecVadThreshold`, `rescueThreshold`)
     against that workout's real audio.
  5. With `--auto-apply`, backs up the current voice config to
     `S:\\ArmoryCam\\audits\\rollback_<ts>.json` (1-command `--rollback`) and applies ONLY
     bounded numeric config improvements that strictly increase hit/Tier-1 performance with
     zero added false ducks or false emits. Never modifies Python source code.
  6. Writes `S:\\ArmoryCam\\audits\\YYYY-MM-DD_voice_audit.{md,json}` and queues
     `pending_morning_notify.json` for 07:30 AM `ntfy` delivery (`--deliver-pending`).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import wave

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from aec_linein import LineInAecProcessor, SAMPLE_RATE_16K  # noqa: E402
from room_voice import (  # noqa: E402
    OWW_FRAME_SAMPLES,
    OpenWakeWordSpotter,
    RoomVoiceConfig,
    SileroStreamingVad,
    UtteranceDetector,
    VAD_FRAME_SAMPLES,
    _load_whisper_model,
    _register_cuda_dll_dirs,
    is_hallucination,
    matches_wake_word,
    prepare_asr_audio,
    resample_to_mono_16k,
    resolve_oww_model_name,
    strip_wake_word,
)

DEFAULT_ROOT = Path(r"S:\ArmoryCam")
DEFAULT_BACKUP = Path(r"J:\My Drive\Backups\ArmoryFit Backup\armory-fit-latest.json")
DEFAULT_SERVER_URL = "http://127.0.0.1:8766"
DEFAULT_NTFY_SCRIPT = Path(r"C:\Users\davef\.gemini\config\skills\ntfy-alerts\scripts\send_ntfy.py")

_SEG_RE = re.compile(r"^(aud|ref)_(\d+)_(\d+)\.m4a$")

# Bounded safe numeric ranges for --auto-apply (never touches source code or wake word silently)
SAFE_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "wakeThreshold": (0.30, 0.50),
    "rescueThreshold": (0.06, 0.22),
    "aecResOverSub": (0.80, 1.35),
    "aecResGMin": (0.12, 0.30),
    "aecVadThreshold": (0.34, 0.52),
}

_AUDIT_INITIAL_PROMPT = (
    "Armory gym workout log: 185 for 10, 225 for 8, RPE 8, RPE 8.5, "
    "check, done, skip, next, back, undo, start timer, rest, open the gym, "
    "close the gym, pause, resume, lights on, lights off."
)

# Phonetic near-misses for "Armory" that offline Whisper occasionally emits under heavy rock music
_WAKE_FUZZY_RE = re.compile(
    r"\b(?:hey\s+)?(?:armory|armery|armori|armoury|armour\s*y|armoree|arm\s+ready)\b",
    re.IGNORECASE,
)
_WAKE_SOFT_HEY_RE = re.compile(
    r"\bhey\s+(?:harmony|emery|amory|henry|marry|memory|armour)\b",
    re.IGNORECASE,
)
_GYM_CMD_HINT_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|record|recording|clip|log|logged|check|done|set|reps?|rpe|"
    r"open|close|shut|gym|lights?|skip|next|previous|back|undo|rest|timer|pause|resume|"
    r"stop|start|weight|pounds?|lbs?|for|plus|minus|pr|bench|squat|deadlift|press)\b",
    re.IGNORECASE,
)


def _dbfs(rms: float) -> float:
    return 20.0 * math.log10(max(1e-9, float(rms)))


def _fmt_clock(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%H:%M:%S")


def _default_target_date(now: datetime | None = None) -> str:
    dt = now or datetime.now()
    if dt.hour < 6:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _day_bounds_ms(date_str: str) -> tuple[int, int]:
    d0 = datetime.strptime(date_str, "%Y-%m-%d")
    d1 = d0 + timedelta(days=1)
    return int(d0.timestamp() * 1000), int(d1.timestamp() * 1000)


@dataclass
class WorkoutWindow:
    start_ms: int
    end_ms: int
    sources: list[str] = field(default_factory=list)
    label: str = ""

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000.0)


@dataclass
class SpokenCommand:
    offset_sec: float
    ts_ms: int
    clock: str
    transcript: str
    command_tail: str
    mic_db: float
    bg_db: float
    ref_db: float
    snr_db: float
    has_ref: bool
    status: str = "MISSED"          # TIER-1 DUCKED | TIER-2 RESCUED | MISSED
    matched_tier: str = ""
    matched_transcript: str = ""
    matched_delay_ms: int | None = None
    oww_peak: float = 0.0


@dataclass
class CandidateResult:
    label: str
    wake_word: str
    wake_model: str
    wake_threshold: float
    rescue_threshold: float
    aec_res_over_sub: float
    aec_res_g_min: float
    aec_vad_threshold: float
    total_commands: int
    landed: int
    tier1_hits: int
    tier2_hits: int
    missed: int
    false_ducks: int
    false_emits: int
    score: float
    is_baseline: bool = False
    auto_apply_eligible: bool = True

    @property
    def hit_rate(self) -> float:
        return (self.landed / self.total_commands) if self.total_commands > 0 else 1.0

    @property
    def tier1_rate(self) -> float:
        return (self.tier1_hits / self.total_commands) if self.total_commands > 0 else 1.0

    def config_delta(self, base: dict[str, Any]) -> dict[str, Any]:
        """Return bounded numeric config changes vs `base`."""
        cand_map = {
            "wakeThreshold": round(float(self.wake_threshold), 2),
            "rescueThreshold": round(float(self.rescue_threshold), 2),
            "aecResOverSub": round(float(self.aec_res_over_sub), 2),
            "aecResGMin": round(float(self.aec_res_g_min), 2),
            "aecVadThreshold": round(float(self.aec_vad_threshold), 2),
        }
        delta: dict[str, Any] = {}
        for k, v in cand_map.items():
            lo, hi = SAFE_PARAM_BOUNDS[k]
            v_clamped = round(max(lo, min(hi, float(v))), 2)
            cur = base.get(k)
            try:
                cur_f = round(float(cur), 2) if cur is not None else None
            except (TypeError, ValueError):
                cur_f = None
            if cur_f is None or abs(v_clamped - cur_f) >= 0.005:
                delta[k] = v_clamped
        return delta


def compute_composite_score(
    *,
    total_commands: int,
    landed: int,
    tier1_hits: int,
    false_ducks: int,
    false_emits: int,
) -> float:
    """Weighted voice quality score prioritizing 100% catch rate and zero false triggers."""
    hit_rate = (landed / total_commands) if total_commands > 0 else 1.0
    tier1_rate = (tier1_hits / total_commands) if total_commands > 0 else 1.0
    return round(
        100.0 * hit_rate
        + 30.0 * tier1_rate
        - 50.0 * float(false_ducks)
        - 100.0 * float(false_emits),
        2,
    )


# ---------------------------------------------------------------------------
# 1. Workout window detection & voice_events loading
# ---------------------------------------------------------------------------

def load_voice_events(root_dir: Path, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Read structured voice telemetry rows within `[start_ms, end_ms]`."""
    rows: list[dict[str, Any]] = []
    logs_dir = Path(root_dir) / "logs"
    for name in ("voice_events.jsonl.1", "voice_events.jsonl"):
        p = logs_dir / name
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = int(obj.get("tsMs") or 0)
                if start_ms <= ts <= end_ms:
                    rows.append(obj)
        except OSError:
            continue
    rows.sort(key=lambda r: int(r.get("tsMs") or 0))
    return rows


def _extract_session_set_timestamps(sess: dict[str, Any], day_start_ms: int, day_end_ms: int) -> list[int]:
    stamps: list[int] = []
    for ex in sess.get("exs") or []:
        if not isinstance(ex, dict):
            continue
        for st in ex.get("sets") or []:
            if not isinstance(st, dict):
                continue
            for key in ("doneAt", "completedAt", "tsMs", "ts", "loggedAt"):
                val = st.get(key)
                if isinstance(val, (int, float)) and day_start_ms <= int(val) <= day_end_ms:
                    stamps.append(int(val))
    return stamps


def detect_workout_windows(
    target_date: str,
    root_dir: Path = DEFAULT_ROOT,
    backup_path: Path | None = DEFAULT_BACKUP,
    *,
    merge_gap_ms: int = 10 * 60_000,
) -> list[WorkoutWindow]:
    """Identify strength-workout time windows for `target_date` from logs, backup, and clips.

    Returns an empty list on rest days so the nightly auditor cleanly exits with zero work.
    """
    day_start_ms, day_end_ms = _day_bounds_ms(target_date)
    raw_spans: list[tuple[int, int, str, str]] = []  # (start_ms, end_ms, source, label)

    # Source 1: voice_events.jsonl context spans & event clusters
    events = load_voice_events(root_dir, day_start_ms, day_end_ms)
    ctx_start: int | None = None
    for ev in events:
        kind = str(ev.get("kind") or "")
        ts = int(ev.get("tsMs") or 0)
        if kind == "context":
            if ev.get("active") and ctx_start is None:
                ctx_start = ts
            elif not ev.get("active") and ctx_start is not None:
                if ts > ctx_start:
                    raw_spans.append((ctx_start - 60_000, ts + 60_000, "voice_context", "Voice Active"))
                ctx_start = None
        elif kind in ("wake_tier1", "utterance") and (kind == "wake_tier1" or ev.get("emitted")):
            raw_spans.append((ts - 180_000, ts + 180_000, "voice_events", "Voice Activity"))
    if ctx_start is not None:
        raw_spans.append((ctx_start - 60_000, min(day_end_ms, ctx_start + 90 * 60_000), "voice_context", "Voice Active"))

    # Source 2: Armory Fit backup sessions
    if backup_path is not None and Path(backup_path).is_file():
        try:
            bak = json.loads(Path(backup_path).read_text(encoding="utf-8"))
            for sess in bak.get("sessions") or []:
                if not isinstance(sess, dict):
                    continue
                s_ms = int(sess.get("startMs") or 0)
                if not (day_start_ms <= s_ms <= day_end_ms):
                    continue
                e_ms = int(sess.get("endMs") or 0)
                status = str(sess.get("status") or "").lower()
                name = str(sess.get("name") or "Strength").strip() or "Strength"
                set_stamps = _extract_session_set_timestamps(sess, day_start_ms, day_end_ms)

                if e_ms > s_ms and (e_ms - s_ms) <= 4 * 3600_000:
                    raw_spans.append((s_ms - 120_000, e_ms + 120_000, "armory_backup", name))
                elif set_stamps:
                    w_s = min(s_ms, min(set_stamps)) - 120_000
                    w_e = max(s_ms + 300_000, max(set_stamps) + 180_000)
                    raw_spans.append((w_s, w_e, "armory_backup", name))
                elif status == "done":
                    # Completed session with missing/huge endMs -> cap cleanly at 90 min
                    raw_spans.append((s_ms - 120_000, s_ms + 90 * 60_000, "armory_backup", name))
        except Exception as exc:
            logging.getLogger("armory-voice-audit").warning("Could not parse backup %s: %s", backup_path, exc)

    # Source 3: Saved lift clips on target_date
    clips_idx = Path(root_dir) / "clips" / "index.jsonl"
    if clips_idx.is_file():
        try:
            for line in clips_idx.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cs = int(c.get("startMs") or 0)
                ce = int(c.get("endMs") or 0)
                if cs and day_start_ms <= cs <= day_end_ms:
                    ce_eff = ce if ce > cs else (cs + 60_000)
                    ex_name = str(c.get("exName") or "Lift Clip")
                    raw_spans.append((cs - 180_000, ce_eff + 180_000, "clips", ex_name))
        except OSError:
            pass

    if not raw_spans:
        return []

    # Clamp to day bounds and merge adjacent/overlapping spans
    clamped: list[tuple[int, int, str, str]] = []
    for s_ms, e_ms, src, lbl in raw_spans:
        cs = max(day_start_ms, int(s_ms))
        ce = min(day_end_ms, int(e_ms))
        if ce > cs:
            clamped.append((cs, ce, src, lbl))
    clamped.sort(key=lambda x: x[0])

    merged: list[WorkoutWindow] = []
    for s_ms, e_ms, src, lbl in clamped:
        if merged and s_ms <= merged[-1].end_ms + merge_gap_ms:
            win = merged[-1]
            win.end_ms = max(win.end_ms, e_ms)
            if src not in win.sources:
                win.sources.append(src)
            if lbl and lbl not in win.label:
                win.label = f"{win.label} / {lbl}" if win.label else lbl
        else:
            merged.append(WorkoutWindow(start_ms=s_ms, end_ms=e_ms, sources=[src], label=lbl))
    return merged


# ---------------------------------------------------------------------------
# 2. Fast Audio Timeline Loading (PyAV + WAV pair mode)
# ---------------------------------------------------------------------------

def _decode_m4a_to_16k(path: Path) -> np.ndarray:
    """Decode an `.m4a` tape segment cleanly to 16 kHz mono float32 via PyAV."""
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE_16K)
    parts: list[np.ndarray] = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                parts.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None):
            parts.append(rf.to_ndarray().reshape(-1))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


def _read_wav_to_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        sr = int(wf.getframerate())
        ch = max(1, int(wf.getnchannels()))
        sw = int(wf.getsampwidth())
        raw = wf.readframes(wf.getnframes())
    if sw == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width {sw} in {path}")
    if ch > 1:
        arr = arr[: (arr.size // ch) * ch].reshape(-1, ch).mean(axis=1)
    return resample_to_mono_16k(arr, sr)


def load_pair_dir_16k(pair_dir: Path) -> tuple[np.ndarray, np.ndarray, int, float, str]:
    """Load paired mic + Line-In WAVs from a calibration/stress-test folder."""
    pdir = Path(pair_dir)
    mic_p = pdir / "s330_48k.wav"
    ref_p = pdir / "linein_48k.wav"
    if not mic_p.is_file():
        cands = [p for p in sorted(pdir.glob("*.wav")) if any(k in p.name.lower() for k in ("s330", "mic", "room"))]
        if not cands:
            raise FileNotFoundError(f"No mic WAV found in {pdir}")
        mic_p = cands[0]
    if not ref_p.is_file():
        cands = [p for p in sorted(pdir.glob("*.wav")) if any(k in p.name.lower() for k in ("linein", "ref", "denon"))]
        ref_p = cands[0] if cands else None

    mic_16k = _read_wav_to_16k(mic_p)
    if ref_p is not None and ref_p.is_file():
        ref_16k = _read_wav_to_16k(ref_p)
        n = min(mic_16k.size, ref_16k.size)
        mic_16k = mic_16k[:n]
        ref_16k = ref_16k[:n]
    else:
        ref_16k = np.zeros_like(mic_16k)

    # Measure active ref coverage (512-sample frames above -72 dBFS)
    n_blk = ref_16k.size // 512
    if n_blk > 0:
        rms_blk = np.sqrt(np.mean(ref_16k[: n_blk * 512].reshape(n_blk, 512) ** 2, axis=1))
        ref_cov = float(np.mean(rms_blk >= 0.00025))
    else:
        ref_cov = 0.0
    start_ms = int(mic_p.stat().st_mtime * 1000) - int(round(mic_16k.size * 1000.0 / SAMPLE_RATE_16K))
    return mic_16k, ref_16k, start_ms, ref_cov, pdir.name


def load_window_audio_16k(
    root_dir: Path,
    start_ms: int,
    end_ms: int,
) -> tuple[np.ndarray, np.ndarray, int, int, float]:
    """Load wall-clock aligned `aud_*.m4a` and paired `ref_*.m4a` for `[start_ms, end_ms]`."""
    tapes_root = Path(root_dir) / "audio_tapes"
    total_samples = max(0, int(round((end_ms - start_ms) * SAMPLE_RATE_16K / 1000.0)))
    if total_samples == 0 or not tapes_root.is_dir():
        z = np.zeros(0, dtype=np.float32)
        return z, z, 0, 0, 0.0

    mic = np.zeros(total_samples, dtype=np.float32)
    ref = np.zeros(total_samples, dtype=np.float32)
    d0 = datetime.fromtimestamp(max(0, start_ms - 60_000) / 1000.0).date()
    d1 = datetime.fromtimestamp(max(0, end_ms + 60_000) / 1000.0).date()

    aud_files: list[tuple[int, int, Path]] = []
    ref_files: list[tuple[int, int, Path]] = []
    cur = d0
    while cur <= d1:
        dd = tapes_root / cur.strftime("%Y-%m-%d")
        if dd.is_dir():
            for p in dd.iterdir():
                m = _SEG_RE.match(p.name)
                if not m:
                    continue
                s_ms = int(m.group(2))
                e_ms = int(m.group(3))
                if e_ms > start_ms and s_ms < end_ms and p.stat().st_size > 128:
                    if m.group(1) == "aud":
                        aud_files.append((s_ms, e_ms, p))
                    else:
                        ref_files.append((s_ms, e_ms, p))
        cur += timedelta(days=1)

    aud_files.sort(key=lambda x: x[0])
    ref_files.sort(key=lambda x: x[0])

    def _place(files: list[tuple[int, int, Path]], target: np.ndarray) -> int:
        count = 0
        for s_ms, _e_ms, path in files:
            try:
                pcm = _decode_m4a_to_16k(path)
            except Exception:
                continue
            if pcm.size == 0:
                continue
            pos = int(round((s_ms - start_ms) * SAMPLE_RATE_16K / 1000.0))
            src_a = max(0, -pos)
            dst_a = max(0, pos)
            n_copy = min(pcm.size - src_a, target.size - dst_a)
            if n_copy > 0:
                target[dst_a : dst_a + n_copy] = pcm[src_a : src_a + n_copy]
                count += 1
        return count

    n_aud = _place(aud_files, mic)
    n_ref = _place(ref_files, ref)

    n_blk = ref.size // 512
    if n_blk > 0 and n_ref > 0:
        rms_blk = np.sqrt(np.mean(ref[: n_blk * 512].reshape(n_blk, 512) ** 2, axis=1))
        ref_cov = float(np.mean(rms_blk >= 0.00025))
    else:
        ref_cov = 0.0
    return mic, ref, n_aud, n_ref, ref_cov


# ---------------------------------------------------------------------------
# 3. Streaming AEC helper & Offline Heavyweight Ground-Truth Dissection
# ---------------------------------------------------------------------------

def run_aec_offline(
    mic_16k: np.ndarray,
    ref_16k: np.ndarray,
    *,
    res_over_sub: float = 1.00,
    res_g_min: float = 0.20,
    frame_samples: int = 512,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stream `mic_16k` + `ref_16k` through `LineInAecProcessor` frame-by-frame.

    Returns `(lin_16k, clean_16k, aec_active_mask)` matching the exact live pipeline.
    """
    n = int(min(mic_16k.size, ref_16k.size))
    if n == 0:
        z = np.zeros(0, dtype=np.float32)
        return z, z, np.zeros(0, dtype=bool)
    aec = LineInAecProcessor(enabled=True, res_over_sub=res_over_sub, res_g_min=res_g_min)
    lin_out = np.empty(n, dtype=np.float32)
    cln_out = np.empty(n, dtype=np.float32)
    n_frames = (n + frame_samples - 1) // frame_samples
    active_mask = np.zeros(n_frames, dtype=bool)

    for fi, pos in enumerate(range(0, n, frame_samples)):
        end = min(n, pos + frame_samples)
        r_blk = ref_16k[pos:end]
        m_blk = mic_16k[pos:end]
        aec.push_ref_samples(r_blk)
        lin_blk, cln_blk, is_act = aec.process_mic_frame(m_blk)
        k = min(end - pos, lin_blk.size)
        if k > 0:
            lin_out[pos : pos + k] = lin_blk[:k]
            cln_out[pos : pos + k] = cln_blk[:k]
        if k < (end - pos):
            lin_out[pos + k : end] = m_blk[k:]
            cln_out[pos + k : end] = m_blk[k:]
        active_mask[fi] = bool(is_act)
    return lin_out, cln_out, active_mask


def extract_wake_phrase_hits(text: str) -> tuple[bool, str]:
    """Return `(is_command, command_tail)` when text contains an Armory wake command."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean or is_hallucination(clean):
        return False, ""

    # Exact wake match via production helper
    for wake in ("hey armory", "armory"):
        if matches_wake_word(clean, wake):
            tail = strip_wake_word(clean, wake).strip(" ,.-!?")
            if tail and not is_hallucination(tail):
                return True, tail
            # Lone "Hey Armory" is also valid if explicit "hey armory"
            if wake == "hey armory" and re.search(r"\bhey\s+armory\b", clean, re.IGNORECASE):
                return True, ""

    # Close phonetic match ("Hey Armery, record this" / "Armory...")
    m = _WAKE_FUZZY_RE.search(clean)
    if m:
        tail = clean[m.end() :].strip(" ,.-!?")
        if tail and _GYM_CMD_HINT_RE.search(tail) and not is_hallucination(tail):
            return True, tail

    # Soft phonetic match ("Hey Harmony, 225 for 8") ONLY with explicit gym command hint
    m2 = _WAKE_SOFT_HEY_RE.search(clean)
    if m2:
        tail = clean[m2.end() :].strip(" ,.-!?")
        if tail and _GYM_CMD_HINT_RE.search(tail) and len(tail.split()) <= 10:
            return True, tail

    return False, ""


def _split_segment_wake_clauses(seg: Any) -> list[tuple[float, str]]:
    """Split a Whisper segment containing 1+ wake words into `(rel_start_sec, clause_text)` pairs."""
    seg_txt = re.sub(r"\s+", " ", str(getattr(seg, "text", "") or "")).strip()
    if not seg_txt:
        return []
    words = list(getattr(seg, "words", None) or [])
    seg_start = float(getattr(seg, "start", 0.0) or 0.0)
    if not words:
        return [(seg_start, seg_txt)]

    wake_starts: list[int] = []
    n_w = len(words)
    i = 0
    while i < n_w:
        w = re.sub(r"[^a-z]", "", str(getattr(words[i], "word", "") or "").lower())
        nxt = re.sub(r"[^a-z]", "", str(getattr(words[i + 1], "word", "") or "").lower()) if i + 1 < n_w else ""
        if w == "hey" and nxt in ("armory", "armery", "armori", "armoury", "armoree", "harmony", "emery", "amory", "henry", "armour"):
            wake_starts.append(i)
            i += 2
            continue
        if w in ("armory", "armery", "armori", "armoury", "armoree"):
            wake_starts.append(i)
            i += 1
            continue
        i += 1

    if len(wake_starts) <= 1:
        rel_s = float(getattr(words[wake_starts[0]], "start", seg_start) or seg_start) if wake_starts else seg_start
        txt = "".join(str(getattr(wobj, "word", "") or "") for wobj in words[wake_starts[0]:]).strip() if wake_starts else seg_txt
        return [(rel_s, re.sub(r"\s+", " ", txt).strip() or seg_txt)]

    clauses: list[tuple[float, str]] = []
    for k, wi in enumerate(wake_starts):
        w_end = wake_starts[k + 1] if k + 1 < len(wake_starts) else n_w
        rel_s = float(getattr(words[wi], "start", seg_start) or seg_start)
        clause_txt = re.sub(r"\s+", " ", "".join(str(getattr(wobj, "word", "") or "") for wobj in words[wi:w_end])).strip()
        if clause_txt:
            clauses.append((rel_s, clause_txt))
    return clauses


def dissect_ground_truth_commands(
    mic_16k: np.ndarray,
    ref_16k: np.ndarray,
    start_ms: int,
    *,
    whisper_model: Any,
    lin_16k: np.ndarray | None = None,
    clean_16k: np.ndarray | None = None,
    active_mask: np.ndarray | None = None,
    window_sec: float = 18.0,
    overlap_sec: float = 3.0,
    beam_size: int = 5,
) -> list[SpokenCommand]:
    """Find every spoken wake command using dual-pass offline GPU Whisper (never drops speech to music VAD).

    Pass 1: 12-second overlapping windows with `vad_filter=False` over `lin_16k` (pristine
            Stage-1 linear PBFDAF when Line-In ref exists, else raw mic).
    Pass 2: Targeted Stage-2 RES Silero-VAD bursts (when `clean_16k` exists) transcribed
            directly on the matching `lin_16k` slice so isolated speech over Vol 69 rock
            lyrics is captured cleanly even inside dense choruses.
    """
    src_audio = lin_16k if (lin_16k is not None and lin_16k.size == mic_16k.size) else mic_16k
    n = int(src_audio.size)
    if n < SAMPLE_RATE_16K:
        return []

    candidates: list[tuple[float, SpokenCommand]] = []  # (edge_dist_sec, cmd)

    def _scan_slice(slice_start_samp: int, slice_end_samp: int, edge_bonus: float = 0.0) -> None:
        chunk = src_audio[slice_start_samp:slice_end_samp]
        if chunk.size < SAMPLE_RATE_16K // 2:
            return
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        if rms < 0.0008:
            return
        win_start_sec = slice_start_samp / float(SAMPLE_RATE_16K)
        win_dur_sec = chunk.size / float(SAMPLE_RATE_16K)
        prep = prepare_asr_audio(chunk)
        segs, _info = whisper_model.transcribe(
            prep,
            language="en",
            beam_size=beam_size,
            best_of=max(1, min(5, beam_size)),
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.75,
            log_prob_threshold=-1.2,
            vad_filter=False,
            word_timestamps=True,
            initial_prompt=_AUDIT_INITIAL_PROMPT,
        )
        for seg in segs:
            if float(getattr(seg, "no_speech_prob", 0.0) or 0.0) > 0.62:
                continue
            if float(getattr(seg, "compression_ratio", 0.0) or 0.0) > 2.4:
                continue
            for seg_rel_start, clause_txt in _split_segment_wake_clauses(seg):
                is_cmd, tail = extract_wake_phrase_hits(clause_txt)
                if not is_cmd:
                    continue

                abs_sec = win_start_sec + seg_rel_start
                edge_dist = min(seg_rel_start, max(0.0, win_dur_sec - seg_rel_start)) + edge_bonus

                # Measure local mic, background, and Line-In reference dBFS around the spoken command
                c_i0 = max(0, int(round(abs_sec * SAMPLE_RATE_16K)))
                c_i1 = min(n, c_i0 + int(2.2 * SAMPLE_RATE_16K))
                b_i0 = max(0, c_i0 - int(1.5 * SAMPLE_RATE_16K))
                b_i1 = max(b_i0 + 1, c_i0)
                mic_slice = mic_16k[c_i0:c_i1]
                bg_slice = mic_16k[b_i0:b_i1]
                ref_slice = ref_16k[c_i0:c_i1] if ref_16k.size >= c_i1 else np.zeros(1, dtype=np.float32)

                mic_rms = float(np.sqrt(np.mean(mic_slice * mic_slice))) if mic_slice.size else 1e-9
                bg_rms = float(np.sqrt(np.mean(bg_slice * bg_slice))) if bg_slice.size else 1e-9
                ref_rms = float(np.sqrt(np.mean(ref_slice * ref_slice))) if ref_slice.size else 1e-9
                mic_db = round(_dbfs(mic_rms), 1)
                bg_db = round(_dbfs(bg_rms), 1)
                ref_db = round(_dbfs(ref_rms), 1) if ref_rms > 1e-7 else -99.0
                snr_db = round(mic_db - bg_db, 1)

                cmd_ts_ms = start_ms + int(round(abs_sec * 1000.0))
                cmd = SpokenCommand(
                    offset_sec=round(abs_sec, 2),
                    ts_ms=cmd_ts_ms,
                    clock=_fmt_clock(cmd_ts_ms),
                    transcript=clause_txt,
                    command_tail=tail,
                    mic_db=mic_db,
                    bg_db=bg_db,
                    ref_db=ref_db,
                    snr_db=snr_db,
                    has_ref=bool(ref_rms >= 0.00025),
                )
                candidates.append((edge_dist, cmd))

    # Pass 1: Continuous overlapping 18s windows with vad_filter=False
    win_n = int(round(window_sec * SAMPLE_RATE_16K))
    hop_n = max(SAMPLE_RATE_16K, int(round((window_sec - overlap_sec) * SAMPLE_RATE_16K)))
    total_wins = max(1, (max(0, n - 1) // hop_n) + 1)
    log_d = logging.getLogger("armory-voice-audit")
    pos = 0
    win_i = 0
    while pos < n:
        end = min(n, pos + win_n)
        _scan_slice(pos, end, edge_bonus=0.0)
        win_i += 1
        if total_wins >= 30 and (win_i % 25 == 0 or end >= n):
            log_d.info("Dissecting audio: %d/%d windows (%.0f%%) — %d candidate hit(s)", win_i, total_wins, (win_i * 100.0) / total_wins, len(candidates))
        if end >= n:
            break
        pos += hop_n

    # Pass 2: Targeted RES-VAD isolated speech bursts (when paired Line-In AEC is active)
    if clean_16k is not None and active_mask is not None and clean_16k.size == mic_16k.size and np.any(active_mask):
        vad_probs = _score_silero_vad_curve(clean_16k, active_mask)
        bursts = _simulate_tier2_vad_utterances(
            src_audio, clean_16k, active_mask, vad_probs=vad_probs, vad_threshold=0.35
        )
        for t0, t1, _clip in bursts:
            s_i0 = max(0, int(round((t0 - 0.35) * SAMPLE_RATE_16K)))
            s_i1 = min(n, int(round((t1 + 0.35) * SAMPLE_RATE_16K)))
            _scan_slice(s_i0, s_i1, edge_bonus=2.0)

    # Deduplicate overlapping detections within +/- 1.7s, preferring centered + fuller transcripts
    candidates.sort(key=lambda item: item[1].offset_sec)
    deduped: list[tuple[float, SpokenCommand]] = []
    for edge_d, cmd in candidates:
        if deduped and abs(cmd.offset_sec - deduped[-1][1].offset_sec) <= 1.7:
            prev_edge, prev_cmd = deduped[-1]
            # Prefer detection with a non-empty tail and better interior margin
            cur_rank = (1 if cmd.command_tail else 0, len(cmd.command_tail), edge_d)
            prev_rank = (1 if prev_cmd.command_tail else 0, len(prev_cmd.command_tail), prev_edge)
            if cur_rank > prev_rank:
                deduped[-1] = (edge_d, cmd)
        else:
            deduped.append((edge_d, cmd))

    return [cmd for _ed, cmd in deduped]


# ---------------------------------------------------------------------------
# 4. Matching against Live voice_events.jsonl & Real-World Parameter Sweep
# ---------------------------------------------------------------------------

def match_live_events(
    commands: list[SpokenCommand],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match ground-truth spoken commands 1-to-1 against `voice_events.jsonl`."""
    ducks = [ev for ev in events if ev.get("kind") == "wake_tier1"]
    emits = [ev for ev in events if ev.get("kind") == "utterance" and ev.get("emitted")]

    used_ducks: set[int] = set()
    used_emits: set[int] = set()
    tier1_hits = 0
    tier2_hits = 0
    missed = 0

    for cmd in commands:
        # Match emitted utterance in [-2.0s, +7.5s]
        best_ei: int | None = None
        best_dt = 10_000_000
        for ei, ev in enumerate(emits):
            if ei in used_emits:
                continue
            dt = int(ev.get("tsMs") or 0) - cmd.ts_ms
            if -2_000 <= dt <= 7_500 and abs(dt) < best_dt:
                best_dt = abs(dt)
                best_ei = ei

        # Match Tier-1 OWW duck in [-2.0s, +3.5s]
        best_di: int | None = None
        best_ddt = 10_000_000
        for di, ev in enumerate(ducks):
            if di in used_ducks:
                continue
            dt = int(ev.get("tsMs") or 0) - cmd.ts_ms
            if -2_000 <= dt <= 3_500 and abs(dt) < best_ddt:
                best_ddt = abs(dt)
                best_di = di

        if best_di is not None:
            used_ducks.add(best_di)
            cmd.oww_peak = round(float(ducks[best_di].get("score") or 0.0), 3)

        if best_ei is not None:
            used_emits.add(best_ei)
            ev = emits[best_ei]
            tier = str(ev.get("tier") or "")
            cmd.matched_tier = tier
            cmd.matched_transcript = str(ev.get("transcript") or "")
            cmd.matched_delay_ms = int(ev.get("tsMs") or 0) - cmd.ts_ms
            if tier == "tier1_oww" or best_di is not None:
                cmd.status = "TIER-1 DUCKED"
                tier1_hits += 1
            else:
                cmd.status = "TIER-2 RESCUED"
                tier2_hits += 1
        else:
            cmd.status = "MISSED"
            missed += 1

    false_ducks = 0
    for di, ev in enumerate(ducks):
        if di in used_ducks:
            continue
        ts = int(ev.get("tsMs") or 0)
        if not any(abs(ts - c.ts_ms) <= 4_000 for c in commands):
            false_ducks += 1

    false_emits = 0
    for ei, ev in enumerate(emits):
        if ei in used_emits:
            continue
        ts = int(ev.get("tsMs") or 0)
        if not any(-3_000 <= (ts - c.ts_ms) <= 7_500 for c in commands):
            false_emits += 1

    total = len(commands)
    landed = tier1_hits + tier2_hits
    return {
        "source": "voice_events.jsonl",
        "totalCommands": total,
        "landed": landed,
        "tier1Hits": tier1_hits,
        "tier2Hits": tier2_hits,
        "missed": missed,
        "falseDucks": false_ducks,
        "falseEmits": false_emits,
        "hitRatePct": round((landed / total) * 100.0, 1) if total else 100.0,
        "tier1RatePct": round((tier1_hits / total) * 100.0, 1) if total else 100.0,
        "score": compute_composite_score(
            total_commands=total,
            landed=landed,
            tier1_hits=tier1_hits,
            false_ducks=false_ducks,
            false_emits=false_emits,
        ),
    }


def _scan_oww_score_curve(
    audio_16k: np.ndarray,
    models_dir: Path,
    model_name: str,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Run `OpenWakeWordSpotter` raw inference once per 80 ms frame across `audio_16k`.

    Returns `(frame_times_sec, frame_scores)` so arbitrary thresholds can be swept in <5 ms.
    """
    spotter = OpenWakeWordSpotter(models_dir=models_dir, model_name=model_name, threshold=1.1)
    if not spotter.load():
        return None
    n_frames = int(audio_16k.size) // OWW_FRAME_SAMPLES
    if n_frames <= 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    times = np.empty(n_frames, dtype=np.float32)
    scores = np.empty(n_frames, dtype=np.float32)
    mdl = spotter._model
    key = spotter._model_key
    for i in range(n_frames):
        a = i * OWW_FRAME_SAMPLES
        chunk = audio_16k[a : a + OWW_FRAME_SAMPLES]
        pcm_i16 = np.round(np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2")
        try:
            preds = mdl.predict(pcm_i16)
            sc = float(preds.get(key, 0.0)) if isinstance(preds, dict) else 0.0
        except Exception:
            sc = 0.0
        times[i] = (a + OWW_FRAME_SAMPLES) / float(SAMPLE_RATE_16K)
        scores[i] = sc
    return times, scores


def _simulate_oww_triggers(
    times: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    cooldown_sec: float = 1.4,
) -> list[tuple[float, float]]:
    """Simulate exact cooldown-gated openWakeWord triggers on a precomputed score curve."""
    hits: list[tuple[float, float]] = []
    last_t = -999.0
    for t, sc in zip(times.tolist(), scores.tolist()):
        if sc >= threshold and (t - last_t) >= cooldown_sec:
            hits.append((float(t), float(sc)))
            last_t = float(t)
    return hits


def _simulate_tier2_vad_utterances(
    lin_16k: np.ndarray,
    vad_clean_16k: np.ndarray,
    active_mask: np.ndarray,
    *,
    vad_probs: np.ndarray,
    vad_threshold: float,
) -> list[tuple[float, float, np.ndarray]]:
    """Replay the live `UtteranceDetector` onset/hang state machine over precomputed Silero VAD probs."""
    n = int(lin_16k.size)
    n_frames = min(int(vad_probs.size), n // VAD_FRAME_SAMPLES, int(active_mask.size))
    if n_frames <= 0:
        return []

    min_speech_frames = 5      # 160 ms
    end_silence_frames = 18    # 576 ms
    max_frames = 140           # 4.48 s
    preroll_frames = 18        # 576 ms
    rms_gate = 0.0025

    in_speech = False
    onset_streak = 0
    silence_streak = 0
    speech_frames = 0
    start_fi = 0
    out: list[tuple[float, float, np.ndarray]] = []

    for fi in range(n_frames):
        if not active_mask[fi]:
            in_speech = False
            onset_streak = 0
            silence_streak = 0
            speech_frames = 0
            continue
        a = fi * VAD_FRAME_SAMPLES
        blk = lin_16k[a : a + VAD_FRAME_SAMPLES]
        rms = float(np.sqrt(np.mean(blk * blk)))
        is_sp = (rms >= rms_gate) and (float(vad_probs[fi]) >= vad_threshold)

        if not in_speech:
            if is_sp:
                onset_streak += 1
                if onset_streak >= 2:
                    in_speech = True
                    start_fi = max(0, fi - preroll_frames)
                    speech_frames = onset_streak
                    silence_streak = 0
                    onset_streak = 0
            else:
                onset_streak = 0
        else:
            if is_sp:
                speech_frames += 1
                silence_streak = 0
            else:
                silence_streak += 1
            cur_len = fi - start_fi + 1
            if silence_streak >= end_silence_frames or cur_len >= max_frames:
                if speech_frames >= min_speech_frames:
                    s_idx = start_fi * VAD_FRAME_SAMPLES
                    e_idx = min(n, (fi + 1) * VAD_FRAME_SAMPLES)
                    t0 = s_idx / float(SAMPLE_RATE_16K)
                    t1 = e_idx / float(SAMPLE_RATE_16K)
                    out.append((t0, t1, lin_16k[s_idx:e_idx]))
                in_speech = False
                onset_streak = 0
                silence_streak = 0
                speech_frames = 0

    return out


def _score_silero_vad_curve(vad_clean_16k: np.ndarray, active_mask: np.ndarray) -> np.ndarray:
    """Compute per-512-sample Silero VAD probabilities over AEC-active frames."""
    n_frames = min(int(vad_clean_16k.size) // VAD_FRAME_SAMPLES, int(active_mask.size))
    probs = np.zeros(n_frames, dtype=np.float32)
    vad = SileroStreamingVad()
    idle = 0
    for fi in range(n_frames):
        if not active_mask[fi]:
            if idle == 0:
                vad.reset()
            idle += 1
            continue
        idle = 0
        a = fi * VAD_FRAME_SAMPLES
        blk = np.clip(vad_clean_16k[a : a + VAD_FRAME_SAMPLES] * 3.0, -0.98, 0.98).astype(np.float32)
        probs[fi] = float(vad.score_frame(blk, rms_gate=0.0025))
    return probs


def run_streaming_sweep(
    mic_16k: np.ndarray,
    ref_16k: np.ndarray,
    commands: list[SpokenCommand],
    *,
    models_dir: Path,
    base_cfg: dict[str, Any],
    ref_coverage: float,
    fast_whisper: Any,
    quick: bool = False,
) -> tuple[CandidateResult, list[CandidateResult], dict[str, tuple[np.ndarray, np.ndarray]]]:
    """Sweep wake models, thresholds, and (when ref exists) AEC RES + VAD parameters on real audio."""
    base_wake_word = str(base_cfg.get("wakeWord") or "hey armory").strip().lower()
    base_wake_model = resolve_oww_model_name(base_wake_word)
    base_wake_thr = round(float(base_cfg.get("wakeThreshold", 0.38) or 0.38), 2)
    base_rescue_thr = round(float(base_cfg.get("rescueThreshold", 0.10) or 0.10), 2)
    base_over_sub = round(float(base_cfg.get("aecResOverSub", 1.00) or 1.00), 2)
    base_g_min = round(float(base_cfg.get("aecResGMin", 0.20) or 0.20), 2)
    base_aec_vad = round(float(base_cfg.get("aecVadThreshold", 0.42) or 0.42), 2)

    has_active_ref = ref_coverage >= 0.02 and ref_16k.size == mic_16k.size
    if has_active_ref:
        lin_base, cln_base, act_base = run_aec_offline(
            mic_16k, ref_16k, res_over_sub=base_over_sub, res_g_min=base_g_min
        )
    else:
        lin_base = mic_16k
        cln_base = mic_16k
        act_base = np.zeros(int(mic_16k.size) // VAD_FRAME_SAMPLES, dtype=bool)

    # Record per-command peak OWW score on baseline model for diagnostic tables
    oww_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    wake_models_to_test: list[tuple[str, str]] = [("hey armory", "hey_armory_v0.1")]
    if (models_dir / "openwakeword" / "hey_jarvis_v0.1.onnx").is_file() or (models_dir / "hey_jarvis_v0.1.onnx").is_file():
        wake_models_to_test.append(("hey jarvis", "hey_jarvis_v0.1"))
    if base_wake_model not in [m for _w, m in wake_models_to_test]:
        wake_models_to_test.insert(0, (base_wake_word, base_wake_model))

    for _ww, mname in wake_models_to_test:
        curve = _scan_oww_score_curve(lin_base, models_dir, mname)
        if curve is not None:
            oww_curves[mname] = curve

    # Annotate commands with baseline OWW peak score if not already populated from live events
    if base_wake_model in oww_curves:
        b_times, b_scores = oww_curves[base_wake_model]
        for cmd in commands:
            if cmd.oww_peak == 0.0 and b_times.size > 0:
                mask = (b_times >= cmd.offset_sec - 1.5) & (b_times <= cmd.offset_sec + 2.5)
                if np.any(mask):
                    cmd.oww_peak = round(float(np.max(b_scores[mask])), 3)

    # Cache Whisper verification results by quantized (t0, t1) slice so we never re-run ASR on identical audio
    asr_cache: dict[tuple[int, int, str], tuple[bool, str]] = {}

    def _verify_rescue_clip(t0: float, t1: float, clip_16k: np.ndarray, wake_word: str) -> tuple[bool, str]:
        key = (int(round(t0 * 4.0)), int(round(t1 * 4.0)), wake_word)
        if key in asr_cache:
            return asr_cache[key]
        if fast_whisper is None or clip_16k.size < int(0.35 * SAMPLE_RATE_16K):
            asr_cache[key] = (False, "")
            return False, ""
        prep = prepare_asr_audio(clip_16k)
        segs, _ = fast_whisper.transcribe(
            prep,
            language="en",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=_AUDIT_INITIAL_PROMPT,
        )
        txt = re.sub(r"\s+", " ", " ".join(str(s.text or "") for s in segs)).strip()
        has_w = matches_wake_word(txt, wake_word)
        tail = strip_wake_word(txt, wake_word) if has_w else ""
        tail_words = re.findall(r"[a-z0-9]+", tail.lower())
        ok = bool(has_w and tail and tail_words and len(tail_words) <= 9 and not is_hallucination(tail))
        asr_cache[key] = (ok, txt)
        return ok, txt

    def _eval_oww_triggers(triggers: list[tuple[float, float]]) -> tuple[set[int], int]:
        """Match OWW triggers against `commands`; returns `(hit_cmd_indices, false_ducks)`."""
        hit_cmds: set[int] = set()
        used_trigs: set[int] = set()
        for ci, cmd in enumerate(commands):
            best_ti: int | None = None
            best_dt = 999.0
            for ti, (t_trig, _sc) in enumerate(triggers):
                if ti in used_trigs:
                    continue
                dt = t_trig - cmd.offset_sec
                if -0.8 <= dt <= 2.2 and abs(dt) < best_dt:
                    best_dt = abs(dt)
                    best_ti = ti
            if best_ti is not None:
                hit_cmds.add(ci)
                used_trigs.add(best_ti)
        false_ducks = 0
        for ti, (t_trig, _sc) in enumerate(triggers):
            if ti in used_trigs:
                continue
            if not any(abs(t_trig - c.offset_sec) <= 3.5 for c in commands):
                false_ducks += 1
        return hit_cmds, false_ducks

    # Precompute Tier-2 AEC-VAD bursts per (res_over_sub, res_g_min, aec_vad_threshold) when paired ref exists
    aec_pairs: list[tuple[float, float]] = (
        [(base_over_sub, base_g_min), (0.85, 0.20), (1.15, 0.20)]
        if quick
        else [
            (0.85, 0.14),
            (0.85, 0.20),
            (1.00, 0.14),
            (1.00, 0.20),
            (1.00, 0.26),
            (1.15, 0.20),
            (1.25, 0.20),
        ]
    )
    if (base_over_sub, base_g_min) not in aec_pairs:
        aec_pairs.insert(0, (base_over_sub, base_g_min))

    vad_thrs = sorted({base_aec_vad, 0.36, 0.42, 0.48})
    wake_thrs = sorted({base_wake_thr, 0.30, 0.34, 0.38, 0.42, 0.46})
    rescue_thrs = sorted({base_rescue_thr, 0.06, 0.10, 0.14})

    # Map (over_sub, g_min, vad_thr) -> list of verified (t0, t1, clip) utterances
    tier2_vad_cache: dict[tuple[float, float, float], list[tuple[float, float, np.ndarray]]] = {}
    if has_active_ref:
        for ov, gm in aec_pairs:
            if abs(ov - base_over_sub) < 1e-6 and abs(gm - base_g_min) < 1e-6:
                lin_c, cln_c, act_c = lin_base, cln_base, act_base
            else:
                lin_c, cln_c, act_c = run_aec_offline(mic_16k, ref_16k, res_over_sub=ov, res_g_min=gm)
            vad_probs = _score_silero_vad_curve(cln_c, act_c)
            for vt in vad_thrs:
                tier2_vad_cache[(ov, gm, vt)] = _simulate_tier2_vad_utterances(
                    lin_c, cln_c, act_c, vad_probs=vad_probs, vad_threshold=vt
                )

    def _eval_tier2_vad(
        tier1_hit_cmds: set[int],
        ov: float,
        gm: float,
        vt: float,
        wake_word: str,
    ) -> tuple[int, int]:
        utts = tier2_vad_cache.get((ov, gm, vt), [])
        t2_cmds: set[int] = set()
        false_emits = 0
        for t0, t1, clip in utts:
            near_cis = [
                ci for ci, cmd in enumerate(commands)
                if (t0 - 1.2) <= cmd.offset_sec <= (t1 + 1.2)
            ]
            if near_cis:
                uncaught = [ci for ci in near_cis if ci not in tier1_hit_cmds and ci not in t2_cmds]
                if not uncaught:
                    continue
                ok, _txt = _verify_rescue_clip(t0, t1, clip, wake_word)
                if ok:
                    for ci in uncaught:
                        t2_cmds.add(ci)
            else:
                if (t1 - t0) >= 0.6:
                    ok, _txt = _verify_rescue_clip(t0, t1, clip, wake_word)
                    if ok:
                        false_emits += 1
        return len(t2_cmds), false_emits

    def _eval_tier2_silent_rescue(
        times: np.ndarray,
        scores: np.ndarray,
        wake_thr: float,
        rescue_thr: float,
        tier1_hit_cmds: set[int],
        wake_word: str,
    ) -> tuple[int, int]:
        """Evaluate grey-zone Silent Rescue Gate (`rescue_thr <= score < wake_thr`) when AEC is inactive."""
        if rescue_thr <= 0.0 or times.size == 0:
            return 0, 0
        t2_cmds: set[int] = set()
        false_emits = 0
        last_r = -999.0
        for t, sc in zip(times.tolist(), scores.tolist()):
            if rescue_thr <= sc < wake_thr and (t - last_r) >= 2.4:
                last_r = float(t)
                i0 = max(0, int(round((t - 1.0) * SAMPLE_RATE_16K)))
                i1 = min(int(lin_base.size), int(round((t + 1.9) * SAMPLE_RATE_16K)))
                clip = lin_base[i0:i1]
                near_ci: int | None = None
                for ci, cmd in enumerate(commands):
                    if abs(t - cmd.offset_sec) <= 2.5:
                        near_ci = ci
                        break
                if near_ci is not None:
                    if near_ci in tier1_hit_cmds or near_ci in t2_cmds:
                        continue
                    ok, _ = _verify_rescue_clip(i0 / SAMPLE_RATE_16K, i1 / SAMPLE_RATE_16K, clip, wake_word)
                    if ok:
                        t2_cmds.add(near_ci)
                else:
                    ok, _ = _verify_rescue_clip(i0 / SAMPLE_RATE_16K, i1 / SAMPLE_RATE_16K, clip, wake_word)
                    if ok:
                        false_emits += 1
        return len(t2_cmds), false_emits

    results: list[CandidateResult] = []
    total = len(commands)

    for ww, mname in wake_models_to_test:
        if mname not in oww_curves:
            continue
        times, scores = oww_curves[mname]
        same_wake = (mname == base_wake_model)

        for wt in wake_thrs:
            triggers = _simulate_oww_triggers(times, scores, wt)
            t1_cmds, f_ducks = _eval_oww_triggers(triggers)

            if has_active_ref:
                # Sweep AEC RES + VAD around this wake threshold (full grid for primary wake model, baseline AEC for alt model)
                combos = [(ov, gm, vt) for ov, gm in aec_pairs for vt in vad_thrs] if same_wake else [(base_over_sub, base_g_min, base_aec_vad)]
                for ov, gm, vt in combos:
                    t2_hits, f_emits = _eval_tier2_vad(t1_cmds, ov, gm, vt, ww)
                    landed = len(t1_cmds) + t2_hits
                    is_base = (
                        same_wake
                        and abs(wt - base_wake_thr) < 1e-6
                        and abs(ov - base_over_sub) < 1e-6
                        and abs(gm - base_g_min) < 1e-6
                        and abs(vt - base_aec_vad) < 1e-6
                    )
                    results.append(
                        CandidateResult(
                            label=f"{mname} thr={wt:.2f} res={ov:.2f}/{gm:.2f} vad={vt:.2f}",
                            wake_word=ww,
                            wake_model=mname,
                            wake_threshold=wt,
                            rescue_threshold=base_rescue_thr,
                            aec_res_over_sub=ov,
                            aec_res_g_min=gm,
                            aec_vad_threshold=vt,
                            total_commands=total,
                            landed=landed,
                            tier1_hits=len(t1_cmds),
                            tier2_hits=t2_hits,
                            missed=max(0, total - landed),
                            false_ducks=f_ducks,
                            false_emits=f_emits,
                            score=compute_composite_score(
                                total_commands=total,
                                landed=landed,
                                tier1_hits=len(t1_cmds),
                                false_ducks=f_ducks,
                                false_emits=f_emits,
                            ),
                            is_baseline=is_base,
                            auto_apply_eligible=same_wake,
                        )
                    )
            else:
                r_list = rescue_thrs if same_wake else [base_rescue_thr]
                for rt in r_list:
                    t2_hits, f_emits = _eval_tier2_silent_rescue(times, scores, wt, rt, t1_cmds, ww)
                    landed = len(t1_cmds) + t2_hits
                    is_base = same_wake and abs(wt - base_wake_thr) < 1e-6 and abs(rt - base_rescue_thr) < 1e-6
                    results.append(
                        CandidateResult(
                            label=f"{mname} thr={wt:.2f} rescue={rt:.2f}",
                            wake_word=ww,
                            wake_model=mname,
                            wake_threshold=wt,
                            rescue_threshold=rt,
                            aec_res_over_sub=base_over_sub,
                            aec_res_g_min=base_g_min,
                            aec_vad_threshold=base_aec_vad,
                            total_commands=total,
                            landed=landed,
                            tier1_hits=len(t1_cmds),
                            tier2_hits=t2_hits,
                            missed=max(0, total - landed),
                            false_ducks=f_ducks,
                            false_emits=f_emits,
                            score=compute_composite_score(
                                total_commands=total,
                                landed=landed,
                                tier1_hits=len(t1_cmds),
                                false_ducks=f_ducks,
                                false_emits=f_emits,
                            ),
                            is_baseline=is_base,
                            auto_apply_eligible=same_wake,
                        )
                    )

    if not results:
        fallback = CandidateResult(
            label=f"{base_wake_model} thr={base_wake_thr:.2f}",
            wake_word=base_wake_word,
            wake_model=base_wake_model,
            wake_threshold=base_wake_thr,
            rescue_threshold=base_rescue_thr,
            aec_res_over_sub=base_over_sub,
            aec_res_g_min=base_g_min,
            aec_vad_threshold=base_aec_vad,
            total_commands=total,
            landed=0,
            tier1_hits=0,
            tier2_hits=0,
            missed=total,
            false_ducks=0,
            false_emits=0,
            score=compute_composite_score(total_commands=total, landed=0, tier1_hits=0, false_ducks=0, false_emits=0),
            is_baseline=True,
            auto_apply_eligible=True,
        )
        return fallback, [fallback], oww_curves

    baseline = next((r for r in results if r.is_baseline), results[0])
    # Sort leaderboard: highest score, then fewer false triggers, then closer to baseline
    results.sort(
        key=lambda r: (
            r.score,
            r.landed,
            r.tier1_hits,
            -r.false_emits,
            -r.false_ducks,
            1 if r.is_baseline else 0,
        ),
        reverse=True,
    )
    return baseline, results, oww_curves


def select_safe_auto_apply(
    baseline: CandidateResult,
    leaderboard: list[CandidateResult],
    base_cfg: dict[str, Any],
    *,
    min_commands: int = 3,
    min_score_gain: float = 2.0,
) -> tuple[CandidateResult | None, dict[str, Any], str]:
    """Select the safest dominating numeric candidate, or explain why no change should be applied."""
    if baseline.total_commands < min_commands:
        return None, {}, f"Skipped auto-apply: only {baseline.total_commands} spoken commands (< {min_commands} min sample)."

    for cand in leaderboard:
        if not cand.auto_apply_eligible or cand.is_baseline:
            continue
        if cand.false_emits > 0:
            continue
        if cand.false_ducks > baseline.false_ducks:
            continue
        if cand.hit_rate < baseline.hit_rate:
            continue
        if cand.score < baseline.score + min_score_gain:
            continue
        delta = cand.config_delta(base_cfg)
        if not delta:
            continue
        return (
            cand,
            delta,
            (
                f"Strictly improved score {baseline.score:.1f} -> {cand.score:.1f} "
                f"(landed {baseline.landed}/{baseline.total_commands} -> {cand.landed}/{cand.total_commands}, "
                f"Tier-1 {baseline.tier1_hits} -> {cand.tier1_hits}, false ducks {cand.false_ducks}, false emits 0)."
            ),
        )

    return None, {}, "Current live voice settings are already optimal for this workout (no strictly dominating change)."


# ---------------------------------------------------------------------------
# 5. Live Config I/O, Safe Revertible Apply & 1-Command Rollback
# ---------------------------------------------------------------------------

def fetch_live_voice_config(server_url: str, root_dir: Path) -> tuple[dict[str, Any], str]:
    """Read current voice config from live ArmoryCamDVR HTTP API, falling back to `config.json`."""
    url = f"{server_url.rstrip('/')}/api/voice/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cfg = data.get("config")
        if isinstance(cfg, dict) and cfg:
            return dict(cfg), "http"
    except Exception:
        pass

    cfg_path = Path(root_dir) / "config.json"
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw.get("voice"), dict):
                return RoomVoiceConfig.from_dict(raw["voice"]).to_dict(), "config.json"
        except Exception:
            pass
    return RoomVoiceConfig().to_dict(), "default"


def push_voice_config(server_url: str, root_dir: Path, patch: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Apply a voice config patch via live HTTP API (fallback: `config.json`)."""
    url = f"{server_url.rstrip('/')}/api/voice/config"
    body = json.dumps(patch).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cfg = data.get("config") if isinstance(data, dict) else {}
        return True, "http", dict(cfg or {})
    except Exception as exc:
        # Fallback: persist directly into config.json if server is offline
        cfg_path = Path(root_dir) / "config.json"
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                voice_cfg = dict(raw.get("voice") or {})
                voice_cfg.update(patch)
                validated = RoomVoiceConfig.from_dict(voice_cfg).to_dict()
                raw["voice"] = validated
                tmp = cfg_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                tmp.replace(cfg_path)
                return True, f"config.json (server unreachable: {exc})", validated
            except Exception as exc2:
                return False, f"Failed HTTP ({exc}) and config.json ({exc2})", {}
        return False, f"HTTP failed ({exc}) and config.json missing", {}


def apply_safe_tuning(
    root_dir: Path,
    server_url: str,
    *,
    target_date: str,
    previous_cfg: dict[str, Any],
    delta: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Back up current voice config to `audits/rollback_<ts>.json`, then apply bounded numeric delta."""
    audits_dir = Path(root_dir) / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    rb_path = audits_dir / f"rollback_{ts_tag}.json"
    rb_payload = {
        "savedAt": datetime.now().isoformat(timespec="seconds"),
        "date": target_date,
        "reason": reason,
        "previousVoiceConfig": {k: previous_cfg.get(k) for k in SAFE_PARAM_BOUNDS if k in previous_cfg},
        "fullPreviousVoiceConfig": previous_cfg,
        "appliedDelta": delta,
    }
    rb_path.write_text(json.dumps(rb_payload, indent=2), encoding="utf-8")

    # Enforce strict bounds one more time before sending
    safe_delta: dict[str, Any] = {}
    for k, v in delta.items():
        if k not in SAFE_PARAM_BOUNDS:
            continue
        lo, hi = SAFE_PARAM_BOUNDS[k]
        safe_delta[k] = round(max(lo, min(hi, float(v))), 2)

    ok, via, after_cfg = push_voice_config(server_url, root_dir, safe_delta)
    return {
        "applied": bool(ok),
        "via": via,
        "rollbackFile": str(rb_path),
        "delta": safe_delta,
        "afterConfig": {k: after_cfg.get(k) for k in safe_delta} if after_cfg else {},
        "reason": reason,
    }


def rollback_voice_config(
    root_dir: Path = DEFAULT_ROOT,
    server_url: str = DEFAULT_SERVER_URL,
    rollback_file: str | Path | None = None,
) -> dict[str, Any]:
    """Restore previous voice config from latest (or explicit) `audits/rollback_*.json`."""
    audits_dir = Path(root_dir) / "audits"
    if rollback_file:
        rb_path = Path(rollback_file)
    else:
        cands = sorted(audits_dir.glob("rollback_*.json"), key=lambda p: p.name, reverse=True)
        if not cands:
            raise FileNotFoundError(f"No rollback_*.json files found in {audits_dir}")
        rb_path = cands[0]

    data = json.loads(rb_path.read_text(encoding="utf-8"))
    prev = data.get("previousVoiceConfig") or data.get("fullPreviousVoiceConfig")
    if not isinstance(prev, dict) or not prev:
        raise ValueError(f"Invalid rollback snapshot in {rb_path}")

    # Only restore the safe numeric keys (or keys touched in appliedDelta) so unrelated settings stay untouched
    restore_patch = {k: prev[k] for k in SAFE_PARAM_BOUNDS if k in prev and prev[k] is not None}
    ok, via, after_cfg = push_voice_config(server_url, root_dir, restore_patch)
    return {
        "ok": bool(ok),
        "via": via,
        "rollbackFile": str(rb_path),
        "restored": restore_patch,
        "afterConfig": {k: after_cfg.get(k) for k in restore_patch} if after_cfg else {},
    }


# ---------------------------------------------------------------------------
# 6. Reports & 7:30 AM Morning Notification Delivery
# ---------------------------------------------------------------------------

def render_markdown_report(summary: dict[str, Any]) -> str:
    """Format a clean, scannable Markdown after-action report."""
    date_str = summary["date"]
    sc = summary["scorecard"]
    base = summary["baselineSweep"]
    lb = summary["leaderboard"][:10]
    cmds = summary["commands"]
    tuning = summary.get("tuning") or {}

    lines: list[str] = [
        f"# Armory Voice Overnight Audit — {date_str}",
        "",
        f"- **Generated:** `{summary['generatedAt']}` (`{summary['elapsedSec']:.1f}s` on `{summary['whisperDevice']}`)",
        f"- **Audio Dissected:** `{summary['audioMinutes']:.1f} min` across `{len(summary['windows'])}` window(s) | Paired Line-In Ref: `{summary['refCoveragePct']:.1f}%`",
        f"- **Scorecard Source:** `{sc['source']}`",
        "",
        "## Live Performance Scorecard",
        "",
        "| Metric | Result |",
        "| :--- | :--- |",
        f"| **Spoken Commands Found** | `{sc['totalCommands']}` |",
        f"| **Total Landed** | **`{sc['landed']}/{sc['totalCommands']} ({sc['hitRatePct']:.1f}%)`** |",
        f"| **Tier-1 Instant Duck (OWW)** | `{sc['tier1Hits']}/{sc['totalCommands']} ({sc['tier1RatePct']:.1f}%)` |",
        f"| **Tier-2 Silent Rescue / AEC-VAD** | `{sc['tier2Hits']}` |",
        f"| **Missed Commands** | `{sc['missed']}` |",
        f"| **False Ducks / False Emits** | `{sc['falseDucks']}` / `{sc['falseEmits']}` |",
        f"| **Composite Score** | `{sc['score']:.1f}` |",
        "",
    ]

    if tuning.get("applied"):
        lines += [
            "## Auto-Applied Safe Tuning (Revertible)",
            "",
            f"- **Reason:** {tuning.get('reason', '')}",
            f"- **Applied Delta:** `{json.dumps(tuning.get('delta', {}))}` (via `{tuning.get('via', '')}`)",
            f"- **1-Command Rollback:** `C:\\Python314\\python.exe server\\armory_cam\\nightly_voice_audit.py --rollback \"{tuning.get('rollbackFile', '')}\"`",
            "",
        ]
    else:
        lines += [
            "## Tuning Decision",
            "",
            f"- **Status:** No config change applied — {tuning.get('reason', 'Auto-apply not enabled')}",
            "",
        ]

    # Missed commands callout
    missed_cmds = [c for c in cmds if c.get("status") == "MISSED"]
    if missed_cmds:
        lines += [
            "## Missed Commands Dissection",
            "",
            "| Clock | Offset | Mic dB | Bg dB | Ref dB | OWW Peak | Spoken Transcript |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
        ]
        for c in missed_cmds:
            lines.append(
                f"| `{c['clock']}` | `{c['offset_sec']:.1f}s` | `{c['mic_db']:.1f}` | `{c['bg_db']:.1f}` | "
                f"`{c['ref_db']:.1f}` | `{c['oww_peak']:.3f}` | {c['transcript']} |"
            )
        lines.append("")

    # Full command table
    lines += [
        "## All Dissected Commands",
        "",
        "| Clock | Status | Mic / Ref dB | OWW Peak | Spoken Transcript | Live Matched |",
        "| :--- | :--- | :--- | ---: | :--- | :--- |",
    ]
    if cmds:
        for c in cmds:
            m_txt = str(c.get("matched_transcript") or "—")
            if len(m_txt) > 96:
                m_txt = m_txt[:93] + "..."
            lines.append(
                f"| `{c['clock']}` | `{c['status']}` | `{c['mic_db']:.1f} / {c['ref_db']:.1f}` | "
                f"`{c['oww_peak']:.3f}` | {c['transcript']} | {m_txt} |"
            )
    else:
        lines.append("| — | — | — | — | *(No wake-word commands detected)* | — |")
    lines.append("")

    # Parameter sweep leaderboard
    lines += [
        "## Real-World Parameter Sweep Leaderboard (Top 10)",
        "",
        f"- **Baseline:** `{base['label']}` → Score `{base['score']:.1f}` (`{base['landed']}/{base['total_commands']}`, Tier-1 `{base['tier1_hits']}`, False Ducks `{base['false_ducks']}`, False Emits `{base['false_emits']}`)",
        "",
        "| Rank | Candidate | Landed | Tier-1 | Tier-2 | False Ducks | False Emits | Score |",
        "| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for idx, r in enumerate(lb, 1):
        tag = " **(live)**" if r.get("is_baseline") else (" *(alt wake)*" if not r.get("auto_apply_eligible") else "")
        lines.append(
            f"| {idx} | `{r['label']}`{tag} | `{r['landed']}/{r['total_commands']}` | `{r['tier1_hits']}` | "
            f"`{r['tier2_hits']}` | `{r['false_ducks']}` | `{r['false_emits']}` | **{r['score']:.1f}** |"
        )
    lines.append("")
    return "\n".join(lines)


def build_morning_ntfy_payload(summary: dict[str, Any], md_path: Path) -> dict[str, str]:
    """Compose a concise mobile push digest for 07:30 AM delivery."""
    date_str = summary["date"]
    sc = summary["scorecard"]
    tuning = summary.get("tuning") or {}
    missed_cmds = [c for c in summary.get("commands") or [] if c.get("status") == "MISSED"]

    title = f"Armory Voice Audit ({date_str}): {sc['landed']}/{sc['totalCommands']} ({sc['hitRatePct']:.0f}%)"
    parts = [
        f"Landed {sc['landed']}/{sc['totalCommands']} ({sc['hitRatePct']:.1f}%) — "
        f"Tier-1 duck: {sc['tier1Hits']}, Tier-2 rescue: {sc['tier2Hits']}, "
        f"Missed: {sc['missed']}, False ducks/emits: {sc['falseDucks']}/{sc['falseEmits']}.",
    ]
    if missed_cmds:
        m_txt = "; ".join(f"{c['clock']} '{c['transcript'][:42]}' ({c['mic_db']:.0f} dB)" for c in missed_cmds[:3])
        parts.append(f"Missed: {m_txt}")
    if tuning.get("applied"):
        parts.append(f"Auto-tuned: {json.dumps(tuning.get('delta', {}))} (rollback saved).")
    else:
        # Check if an alt wake model beat baseline meaningfully
        alt_best = next(
            (r for r in summary.get("leaderboard") or [] if not r.get("auto_apply_eligible") and r["score"] > summary["baselineSweep"]["score"] + 5.0),
            None,
        )
        if alt_best is not None:
            parts.append(f"Tip: {alt_best['wake_model']} scored {alt_best['score']:.1f} vs {summary['baselineSweep']['score']:.1f}.")
        else:
            parts.append("Settings unchanged (already optimal).")
    parts.append(f"Report: {md_path.name}")

    tags = "white_check_mark,microphone" if sc["missed"] == 0 and sc["falseEmits"] == 0 else "microphone,bar_chart"
    priority = "default"
    return {
        "date": date_str,
        "queuedAt": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "message": "\n".join(parts),
        "tags": tags,
        "priority": priority,
        "reportPath": str(md_path),
    }


def deliver_pending_notification(
    root_dir: Path = DEFAULT_ROOT,
    ntfy_script: Path = DEFAULT_NTFY_SCRIPT,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send queued `audits/pending_morning_notify.json` via `send_ntfy.py` and clear on success."""
    pending_path = Path(root_dir) / "audits" / "pending_morning_notify.json"
    if not pending_path.is_file():
        return {"sent": False, "reason": "No pending morning notification (rest day or already delivered)."}

    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    if dry_run:
        return {"sent": False, "dryRun": True, "payload": payload}

    if not Path(ntfy_script).is_file():
        raise FileNotFoundError(f"ntfy script not found: {ntfy_script}")

    cmd = [
        sys.executable,
        str(ntfy_script),
        "--title",
        str(payload.get("title") or "Armory Voice Audit"),
        "--message",
        str(payload.get("message") or ""),
        "--priority",
        str(payload.get("priority") or "default"),
        "--tags",
        str(payload.get("tags") or "microphone"),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    if res.returncode != 0:
        raise RuntimeError(f"send_ntfy.py failed (rc={res.returncode}): {res.stderr or res.stdout}")
    pending_path.unlink(missing_ok=True)
    return {"sent": True, "title": payload.get("title"), "stdout": res.stdout.strip()}


# ---------------------------------------------------------------------------
# 7. End-to-End Audit Orchestrator & CLI
# ---------------------------------------------------------------------------

def run_audit(
    *,
    target_date: str,
    root_dir: Path = DEFAULT_ROOT,
    backup_path: Path | None = DEFAULT_BACKUP,
    server_url: str = DEFAULT_SERVER_URL,
    whisper_model_name: str = "medium.en",
    pair_dir: Path | None = None,
    auto_apply: bool = False,
    queue_notify: bool = True,
    max_minutes: float = 0.0,
    sweep_quick: bool = False,
) -> dict[str, Any]:
    """Execute the full overnight voice dissection, sweep, reporting, and optional safe apply."""
    t0 = time.monotonic()
    log = logging.getLogger("armory-voice-audit")
    root_dir = Path(root_dir)
    audits_dir = root_dir / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    models_dir = root_dir / "models"

    base_cfg, cfg_src = fetch_live_voice_config(server_url, root_dir)
    base_over_sub = float(base_cfg.get("aecResOverSub", 1.00) or 1.00)
    base_g_min = float(base_cfg.get("aecResGMin", 0.20) or 0.20)

    # 1. Load audio (either direct --pair-dir or detected workout windows for target_date)
    windows_meta: list[dict[str, Any]] = []
    if pair_dir is not None:
        mic_16k, ref_16k, win_start_ms, ref_cov, label = load_pair_dir_16k(Path(pair_dir))
        if max_minutes > 0.0:
            cap = int(round(max_minutes * 60.0 * SAMPLE_RATE_16K))
            mic_16k = mic_16k[:cap]
            ref_16k = ref_16k[:cap]
        win_end_ms = win_start_ms + int(round(mic_16k.size * 1000.0 / SAMPLE_RATE_16K))
        windows_meta.append(
            {
                "startMs": win_start_ms,
                "endMs": win_end_ms,
                "startClock": _fmt_clock(win_start_ms),
                "endClock": _fmt_clock(win_end_ms),
                "durationSec": round(mic_16k.size / float(SAMPLE_RATE_16K), 1),
                "sources": ["pair_dir"],
                "label": label,
            }
        )
        all_mic = [(win_start_ms, mic_16k, ref_16k, ref_cov)]
        live_events: list[dict[str, Any]] = []
        report_slug = f"{target_date}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', label)}"
    else:
        wins = detect_workout_windows(target_date, root_dir=root_dir, backup_path=backup_path)
        if not wins:
            log.info("Rest day — no strength workout detected for %s; skipping.", target_date)
            return {
                "skipped": True,
                "date": target_date,
                "reason": "Rest day — no strength workout detected.",
            }
        all_mic = []
        live_events = []
        remaining_samples = int(round(max_minutes * 60.0 * SAMPLE_RATE_16K)) if max_minutes > 0.0 else 0
        for w in wins:
            mic_w, ref_w, n_aud, n_ref, ref_cov = load_window_audio_16k(root_dir, w.start_ms, w.end_ms)
            if n_aud == 0 or mic_w.size < SAMPLE_RATE_16K:
                continue
            if remaining_samples > 0:
                if mic_w.size > remaining_samples:
                    mic_w = mic_w[:remaining_samples]
                    ref_w = ref_w[:remaining_samples]
                remaining_samples = max(0, remaining_samples - mic_w.size)
            all_mic.append((w.start_ms, mic_w, ref_w, ref_cov))
            live_events.extend(load_voice_events(root_dir, w.start_ms - 5_000, w.end_ms + 5_000))
            windows_meta.append(
                {
                    "startMs": w.start_ms,
                    "endMs": w.end_ms,
                    "startClock": _fmt_clock(w.start_ms),
                    "endClock": _fmt_clock(w.end_ms),
                    "durationSec": round(mic_w.size / float(SAMPLE_RATE_16K), 1),
                    "sources": w.sources,
                    "label": w.label,
                    "audSegments": n_aud,
                    "refSegments": n_ref,
                    "refCoveragePct": round(ref_cov * 100.0, 1),
                }
            )
            if max_minutes > 0.0 and remaining_samples <= 0:
                break
        if not all_mic:
            log.info("Workout found on %s, but no audio_tapes segments exist; skipping.", target_date)
            return {
                "skipped": True,
                "date": target_date,
                "reason": "No audio tape segments available for detected workout window(s).",
            }
        report_slug = target_date

    # Concatenate windows cleanly with 3.0s silence separators for unified streaming sweep
    sep = np.zeros(3 * SAMPLE_RATE_16K, dtype=np.float32)
    mic_parts: list[np.ndarray] = []
    ref_parts: list[np.ndarray] = []
    window_offsets: list[tuple[int, int, int]] = []  # (sample_offset, win_start_ms, length)
    cursor = 0
    for idx, (w_start, m_w, r_w, _rcov) in enumerate(all_mic):
        if idx > 0:
            mic_parts.append(sep)
            ref_parts.append(sep)
            cursor += sep.size
        window_offsets.append((cursor, w_start, int(m_w.size)))
        mic_parts.append(m_w)
        ref_parts.append(r_w)
        cursor += int(m_w.size)

    mic_cat = np.concatenate(mic_parts).astype(np.float32, copy=False)
    ref_cat = np.concatenate(ref_parts).astype(np.float32, copy=False)
    n_blk = ref_cat.size // 512
    overall_ref_cov = (
        float(np.mean(np.sqrt(np.mean(ref_cat[: n_blk * 512].reshape(n_blk, 512) ** 2, axis=1)) >= 0.00025))
        if n_blk > 0
        else 0.0
    )

    # 2. Load offline heavyweight Whisper (`medium.en`, CUDA) + fast live-equivalent Whisper (`small.en`)
    _register_cuda_dll_dirs()
    log.info("Loading offline Whisper model %s...", whisper_model_name)
    heavy_whisper, heavy_dev = _load_whisper_model(whisper_model_name)
    if whisper_model_name == "small.en":
        fast_whisper = heavy_whisper
    else:
        log.info("Loading fast live-equivalent Whisper small.en for Tier-2 verification...")
        fast_whisper, _fast_dev = _load_whisper_model("small.en")

    # 3. Run baseline AEC once for ground-truth dissection (when paired ref exists)
    if overall_ref_cov >= 0.02:
        lin_cat, cln_cat, act_cat = run_aec_offline(
            mic_cat, ref_cat, res_over_sub=base_over_sub, res_g_min=base_g_min
        )
    else:
        lin_cat = mic_cat
        cln_cat = None
        act_cat = None

    # 4. Ground-truth spoken command dissection per window
    all_commands: list[SpokenCommand] = []
    for sample_off, w_start_ms, w_len in window_offsets:
        sub_mic = mic_cat[sample_off : sample_off + w_len]
        sub_ref = ref_cat[sample_off : sample_off + w_len]
        sub_lin = lin_cat[sample_off : sample_off + w_len]
        sub_cln = cln_cat[sample_off : sample_off + w_len] if cln_cat is not None else None
        if act_cat is not None:
            f0 = sample_off // VAD_FRAME_SAMPLES
            f1 = (sample_off + w_len) // VAD_FRAME_SAMPLES
            sub_act = act_cat[f0:f1]
        else:
            sub_act = None
        win_cmds = dissect_ground_truth_commands(
            sub_mic,
            sub_ref,
            w_start_ms,
            whisper_model=heavy_whisper,
            lin_16k=sub_lin,
            clean_16k=sub_cln,
            active_mask=sub_act,
            beam_size=5,
        )
        # Convert offset_sec to unified concatenated timeline seconds for streaming sweep
        off_sec_shift = sample_off / float(SAMPLE_RATE_16K)
        for c in win_cmds:
            c.offset_sec = round(c.offset_sec + off_sec_shift, 2)
        all_commands.extend(win_cmds)

    log.info("Dissected %d spoken wake-word command(s) across %.1f min of audio.", len(all_commands), mic_cat.size / (60.0 * SAMPLE_RATE_16K))

    # 5. Streaming parameter sweep
    baseline_cand, leaderboard, oww_curves = run_streaming_sweep(
        mic_cat,
        ref_cat,
        all_commands,
        models_dir=models_dir,
        base_cfg=base_cfg,
        ref_coverage=overall_ref_cov,
        fast_whisper=fast_whisper,
        quick=sweep_quick,
    )

    # 6. Scorecard: prefer real live voice_events.jsonl when available, else baseline streaming replay
    has_live_events = any(ev.get("kind") in ("wake_tier1", "utterance") for ev in live_events)
    if has_live_events:
        scorecard = match_live_events(all_commands, live_events)
    else:
        # Annotate commands with baseline replay outcomes reusing the already-computed OWW curve
        base_model = baseline_cand.wake_model
        base_curve = oww_curves.get(base_model)
        t1_trigs = _simulate_oww_triggers(base_curve[0], base_curve[1], baseline_cand.wake_threshold) if base_curve else []
        for cmd in all_commands:
            matched_t1 = next((sc for t_tr, sc in t1_trigs if -0.8 <= (t_tr - cmd.offset_sec) <= 2.2), None)
            if matched_t1 is not None:
                cmd.status = "TIER-1 DUCKED"
                cmd.matched_tier = "tier1_oww_replay"
                cmd.oww_peak = round(float(matched_t1), 3)
                cmd.matched_transcript = cmd.transcript
            elif baseline_cand.tier2_hits > 0:
                # Re-verify individual Tier-2 status cleanly
                i0 = max(0, int(round((cmd.offset_sec - 0.5) * SAMPLE_RATE_16K)))
                i1 = min(lin_cat.size, int(round((cmd.offset_sec + 3.2) * SAMPLE_RATE_16K)))
                prep = prepare_asr_audio(lin_cat[i0:i1])
                segs, _ = fast_whisper.transcribe(
                    prep,
                    language="en",
                    beam_size=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    max_new_tokens=32,
                    initial_prompt=_AUDIT_INITIAL_PROMPT,
                )
                txt = re.sub(r"\s+", " ", " ".join(str(s.text or "") for s in segs)).strip()
                if matches_wake_word(txt, baseline_cand.wake_word) and strip_wake_word(txt, baseline_cand.wake_word):
                    cmd.status = "TIER-2 RESCUED"
                    cmd.matched_tier = "tier2_replay"
                    cmd.matched_transcript = txt
                else:
                    cmd.status = "MISSED"
            else:
                cmd.status = "MISSED"

        scorecard = {
            "source": "streaming_replay (no voice_events.jsonl for date)",
            "totalCommands": baseline_cand.total_commands,
            "landed": baseline_cand.landed,
            "tier1Hits": baseline_cand.tier1_hits,
            "tier2Hits": baseline_cand.tier2_hits,
            "missed": baseline_cand.missed,
            "falseDucks": baseline_cand.false_ducks,
            "falseEmits": baseline_cand.false_emits,
            "hitRatePct": round(baseline_cand.hit_rate * 100.0, 1),
            "tier1RatePct": round(baseline_cand.tier1_rate * 100.0, 1),
            "score": baseline_cand.score,
        }

    # 7. Safe bounded auto-apply (only when requested & strictly dominating)
    winner, delta, reason = select_safe_auto_apply(baseline_cand, leaderboard, base_cfg)
    if auto_apply and winner is not None and delta:
        tuning_info = apply_safe_tuning(
            root_dir,
            server_url,
            target_date=target_date,
            previous_cfg=base_cfg,
            delta=delta,
            reason=reason,
        )
    else:
        tuning_info = {
            "applied": False,
            "reason": (reason if auto_apply else f"Dry run (--auto-apply off). {reason}"),
            "recommendedDelta": delta,
        }

    elapsed = round(time.monotonic() - t0, 1)
    summary: dict[str, Any] = {
        "skipped": False,
        "date": target_date,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "elapsedSec": elapsed,
        "whisperModel": whisper_model_name,
        "whisperDevice": heavy_dev,
        "configSource": cfg_src,
        "audioMinutes": round(mic_cat.size / (60.0 * SAMPLE_RATE_16K), 2),
        "refCoveragePct": round(overall_ref_cov * 100.0, 1),
        "windows": windows_meta,
        "scorecard": scorecard,
        "baselineSweep": asdict(baseline_cand),
        "leaderboard": [asdict(r) for r in leaderboard[:20]],
        "commands": [asdict(c) for c in all_commands],
        "tuning": tuning_info,
    }

    md_path = audits_dir / f"{report_slug}_voice_audit.md"
    json_path = audits_dir / f"{report_slug}_voice_audit.json"
    md_path.write_text(render_markdown_report(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["reportMd"] = str(md_path)
    summary["reportJson"] = str(json_path)

    if queue_notify:
        pending_p = audits_dir / "pending_morning_notify.json"
        ntfy_payload = build_morning_ntfy_payload(summary, md_path)
        pending_p.write_text(json.dumps(ntfy_payload, indent=2), encoding="utf-8")
        summary["pendingNotify"] = str(pending_p)

    log.info(
        "Audit complete in %.1fs -> %s (landed %d/%d, score=%.1f)",
        elapsed,
        md_path,
        scorecard["landed"],
        scorecard["totalCommands"],
        scorecard["score"],
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Armory Fit Overnight Post-Workout Voice Dissection & Safe Revertible Optimizer"
    )
    parser.add_argument("--date", type=str, default="", help="Target YYYY-MM-DD (default: yesterday if hour < 6 else today)")
    parser.add_argument("--root", type=str, default=str(DEFAULT_ROOT), help="ArmoryCam root directory")
    parser.add_argument("--backup", type=str, default=str(DEFAULT_BACKUP), help="Armory Fit latest backup JSON")
    parser.add_argument("--server-url", type=str, default=DEFAULT_SERVER_URL, help="ArmoryCamDVR HTTP URL")
    parser.add_argument("--model", type=str, default="medium.en", help="Offline Whisper model (default: medium.en)")
    parser.add_argument("--pair-dir", type=str, default="", help="Direct paired WAV dir (e.g. voice_cal_...) for stress testing")
    parser.add_argument("--auto-apply", action="store_true", help="Safely apply strictly-dominating bounded numeric config tweaks")
    parser.add_argument("--rollback", nargs="?", const="__LATEST__", default="", help="Revert voice config from latest or specified rollback JSON")
    parser.add_argument("--deliver-pending", action="store_true", help="Deliver queued 07:30 AM ntfy report if present")
    parser.add_argument("--no-queue-notify", action="store_true", help="Do not queue pending_morning_notify.json")
    parser.add_argument("--max-minutes", type=float, default=0.0, help="Optional cap on audio minutes (0 = unlimited)")
    parser.add_argument("--sweep-quick", action="store_true", help="Use reduced AEC sweep grid for fast verification")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    root_dir = Path(args.root)
    if args.deliver_pending:
        res = deliver_pending_notification(root_dir=root_dir)
        print(json.dumps(res, indent=2))
        return 0

    if args.rollback:
        rb_arg = None if args.rollback == "__LATEST__" else args.rollback
        res = rollback_voice_config(root_dir=root_dir, server_url=args.server_url, rollback_file=rb_arg)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    target_date = args.date.strip() or _default_target_date()
    summary = run_audit(
        target_date=target_date,
        root_dir=root_dir,
        backup_path=Path(args.backup) if args.backup else None,
        server_url=args.server_url,
        whisper_model_name=args.model,
        pair_dir=Path(args.pair_dir) if args.pair_dir else None,
        auto_apply=bool(args.auto_apply),
        queue_notify=not bool(args.no_queue_notify),
        max_minutes=float(args.max_minutes or 0.0),
        sweep_quick=bool(args.sweep_quick),
    )
    compact = {k: v for k, v in summary.items() if k not in ("leaderboard", "commands")}
    print(json.dumps(compact, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
