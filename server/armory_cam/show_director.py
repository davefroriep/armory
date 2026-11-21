"""Armory Walk-In & PR Drop-In Show Director.

Coordinates custom arena-announcer intros, tightly clipped PR music drops,
stadium crowd cheers, thunder FX, anti-roller-coaster Denon AVR + Fire TV Cube
volume choreography, 3-screen synchronized entrance videos, and optional
local HTTP smart-home hooks (lighting flashes & fog machine triggers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import math
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Callable, Sequence
import urllib.parse
import urllib.request
import wave

import numpy as np
from scipy.signal import butter, sosfilt

try:
    from .display_control import _strip_wake_prefix, normalize_youtube_tv_url
    from .house_audio import db_to_denon_mv
except ImportError:
    from display_control import _strip_wake_prefix, normalize_youtube_tv_url
    from house_audio import db_to_denon_mv


SHOW_SR = 44_100
MAX_CUBE_WAV_BYTES = 11 * 1024 * 1024  # Safe margin under Cube's 12 MB HTTP cap
MAX_SHOW_DENON_VOL = 75                # -5.0 dB hard safety ceiling
MIN_SHOW_DENON_VOL = 30                # -50.0 dB floor
MAX_SONG_UPLOAD_BYTES = 30 * 1024 * 1024
VALID_SHOW_EXTS = (".mp3", ".wav", ".m4a", ".ogg")
VALID_SHOW_KINDS = ("pr_drop", "walkin", "debut_video")
VALID_CUBE_MODES = ("duck", "takeover", "overlay")
VALID_DEMO_STYLES = ("anthem", "boombap", "thunder_only", "hype")
RYDER_ENTRANCE_URL = "https://www.youtube.com/watch?v=05vwKMySzKw"
DEFAULT_ANNOUNCER_VOICE = "en-US-ChristopherNeural"


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(val)))


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _no_window_kwargs() -> dict[str, Any]:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _safe_show_id(raw: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
    return s


def validate_song_filename(name: str) -> str | None:
    """Validate uploaded custom song filename safely (reject traversal and non-audio)."""
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    p = Path(raw)
    if p.name != raw or p.suffix.lower() not in VALID_SHOW_EXTS:
        return None
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem).strip("._-") or "track"
    ext = p.suffix.lower()
    clean = f"{stem}{ext}"
    if clean != raw and not all(c.isalnum() or c in "._- " for c in raw):
        return None
    return raw


def sanitize_upload_filename(raw_name: str, show_id: str) -> str:
    """Validate and namespace an uploaded client filename into `shows/sources/` (rejects traversal)."""
    raw = str(raw_name or "").strip()
    if not raw or ".." in raw or "/" in raw or "\\" in raw:
        raise ValueError("Invalid filename (path separators and '..' are not allowed)")
    base = Path(raw).name.strip()
    ext = Path(base).suffix.lower()
    if ext not in VALID_SHOW_EXTS:
        raise ValueError(f"Unsupported audio format ({ext or 'none'}); use .mp3, .wav, .m4a, or .ogg")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(base).stem).strip("_-") or "custom"
    sid = _safe_show_id(show_id) or "show"
    return f"{sid}_{stem[:48]}{ext}"


def _norm_trigger_text(text: str) -> str:
    s = str(text or "").lower()
    s = s.replace("’", "'")
    s = re.sub(r"\bp\.?\s*r\.?\b", " pr ", s)
    s = re.sub(r"\bmatt's\b", "matt is", s)
    s = re.sub(r"\bryder's\b", "ryder is", s)
    s = re.sub(r"\bgabe's\b", "gabe is", s)
    s = re.sub(r"\bjosh's\b", "josh is", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_optional_wake(text: str, wake_word: str = "armory") -> str:
    norm = _norm_trigger_text(text)
    norm = re.sub(r"^(?:hey|ok|okay)\s+", "", norm).strip()
    return _strip_wake_prefix(norm, wake_word=wake_word)


@dataclass
class SmartFxConfig:
    """Optional local HTTP hooks for fast lighting switches (Shelly/Kasa) and fog triggers."""

    lights_host: str = ""
    lights_off_url: str = ""
    lights_on_url: str = ""
    fog_host: str = ""
    fog_trigger_url: str = ""
    fog_burst_sec: float = 4.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lightsHost": self.lights_host,
            "lightsOffUrl": self.lights_off_url,
            "lightsOnUrl": self.lights_on_url,
            "fogHost": self.fog_host,
            "fogTriggerUrl": self.fog_trigger_url,
            "fogBurstSec": round(float(self.fog_burst_sec), 1),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SmartFxConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            burst = _clamp(float(raw.get("fogBurstSec", 4.0) or 4.0), 1.0, 10.0)
        except (TypeError, ValueError):
            burst = 4.0
        return cls(
            lights_host=str(raw.get("lightsHost", "") or "").strip(),
            lights_off_url=str(raw.get("lightsOffUrl", "") or "").strip(),
            lights_on_url=str(raw.get("lightsOnUrl", "") or "").strip(),
            fog_host=str(raw.get("fogHost", "") or "").strip(),
            fog_trigger_url=str(raw.get("fogTriggerUrl", "") or "").strip(),
            fog_burst_sec=burst,
        )

    def resolve_lights_url(self, on: bool) -> str:
        explicit = self.lights_on_url if on else self.lights_off_url
        if explicit:
            return explicit
        host = self.lights_host.strip()
        if not host:
            return ""
        flag = "true" if on else "false"
        return f"http://{host}/rpc/Switch.Set?id=0&on={flag}"

    def resolve_fog_url(self) -> str:
        sec_str = f"{_clamp(self.fog_burst_sec, 1.0, 10.0):.1f}".rstrip("0").rstrip(".")
        if self.fog_trigger_url:
            return self.fog_trigger_url.replace("{sec}", sec_str)
        host = self.fog_host.strip()
        if not host:
            return ""
        return f"http://{host}/rpc/Switch.Set?id=0&on=true&toggle_after={sec_str}"


@dataclass
class ShowSpec:
    """Declarative specification for one Armory Walk-In or PR Drop-In show cue."""

    id: str
    name: str
    kind: str = "pr_drop"
    enabled: bool = True
    triggers: list[str] = field(default_factory=list)
    denon_vol: int = 70
    cube_mode: str = "duck"
    overlay_gain: float = 1.0
    announcer_text: str = ""
    announcer_voice: str = DEFAULT_ANNOUNCER_VOICE
    announcer_rate: str = "-6%"
    announcer_pitch: str = "-4Hz"
    demo_style: str = "anthem"
    demo_shout: str = ""
    source_file: str = ""
    song_start_sec: float = 0.0
    song_duration_sec: float = 15.0
    fade_out_sec: float = 2.5
    trim_leading_silence: bool = True
    add_cheer: bool = True
    cheer_delay_sec: float = 9.5
    add_thunder: bool = False
    lights_out: bool = False
    lights_flash_count: int = 0
    trigger_fog: bool = False
    youtube_url: str = ""
    three_screen_video: bool = False
    video_hold_sec: float = 45.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": bool(self.enabled),
            "triggers": list(self.triggers),
            "denonVol": int(self.denon_vol),
            "cubeMode": self.cube_mode,
            "overlayGain": round(float(self.overlay_gain), 2),
            "announcerText": self.announcer_text,
            "announcerVoice": self.announcer_voice,
            "announcerRate": self.announcer_rate,
            "announcerPitch": self.announcer_pitch,
            "demoStyle": self.demo_style,
            "demoShout": self.demo_shout,
            "sourceFile": self.source_file,
            "songStartSec": round(float(self.song_start_sec), 2),
            "songDurationSec": round(float(self.song_duration_sec), 2),
            "fadeOutSec": round(float(self.fade_out_sec), 2),
            "trimLeadingSilence": bool(self.trim_leading_silence),
            "addCheer": bool(self.add_cheer),
            "cheerDelaySec": round(float(self.cheer_delay_sec), 2),
            "addThunder": bool(self.add_thunder),
            "lightsOut": bool(self.lights_out),
            "lightsFlashCount": int(self.lights_flash_count),
            "triggerFog": bool(self.trigger_fog),
            "youtubeUrl": self.youtube_url,
            "threeScreenVideo": bool(self.three_screen_video),
            "videoHoldSec": round(float(self.video_hold_sec), 1),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ShowSpec":
        if not isinstance(raw, dict):
            raise ValueError("Invalid show spec")
        sid = _safe_show_id(raw.get("id") or "")
        if not sid:
            raise ValueError("Show spec missing id")

        def _f(key: str, default: float) -> float:
            try:
                v = raw.get(key, default)
                return float(default if v is None or v == "" else v)
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                v = raw.get(key, default)
                return int(round(float(default if v is None or v == "" else v)))
            except (TypeError, ValueError):
                return default

        kind = str(raw.get("kind", "pr_drop") or "pr_drop").strip().lower()
        if kind not in VALID_SHOW_KINDS:
            kind = "pr_drop"
        cube_mode = str(raw.get("cubeMode", "duck") or "duck").strip().lower()
        if cube_mode not in VALID_CUBE_MODES:
            cube_mode = "duck"
        demo_style = str(raw.get("demoStyle", "anthem") or "anthem").strip().lower()
        if demo_style not in VALID_DEMO_STYLES:
            demo_style = "anthem"
        raw_trigs = raw.get("triggers")
        triggers: list[str] = []
        if isinstance(raw_trigs, list):
            for t in raw_trigs:
                nt = _norm_trigger_text(str(t or ""))
                if nt and nt not in triggers:
                    triggers.append(nt)
        src_valid = validate_song_filename(str(raw.get("sourceFile", "") or "")) or ""
        return cls(
            id=sid,
            name=str(raw.get("name") or sid).strip(),
            kind=kind,
            enabled=bool(raw.get("enabled", True)),
            triggers=triggers,
            denon_vol=max(MIN_SHOW_DENON_VOL, min(MAX_SHOW_DENON_VOL, _i("denonVol", 70))),
            cube_mode=cube_mode,
            overlay_gain=_clamp(_f("overlayGain", 1.0), 0.1, 1.0),
            announcer_text=str(raw.get("announcerText", "") or "").strip(),
            announcer_voice=str(raw.get("announcerVoice", DEFAULT_ANNOUNCER_VOICE) or DEFAULT_ANNOUNCER_VOICE).strip(),
            announcer_rate=str(raw.get("announcerRate", "-6%") or "-6%").strip(),
            announcer_pitch=str(raw.get("announcerPitch", "-4Hz") or "-4Hz").strip(),
            demo_style=demo_style,
            demo_shout=str(raw.get("demoShout", "") or "").strip(),
            source_file=src_valid,
            song_start_sec=max(0.0, _f("songStartSec", 0.0)),
            song_duration_sec=_clamp(_f("songDurationSec", 15.0), 0.0, 50.0),
            fade_out_sec=_clamp(_f("fadeOutSec", 2.5), 0.3, 10.0),
            trim_leading_silence=bool(raw.get("trimLeadingSilence", True)),
            add_cheer=bool(raw.get("addCheer", True)),
            cheer_delay_sec=max(0.0, _f("cheerDelaySec", 9.5)),
            add_thunder=bool(raw.get("addThunder", False)),
            lights_out=bool(raw.get("lightsOut", False)),
            lights_flash_count=max(0, min(6, _i("lightsFlashCount", 0))),
            trigger_fog=bool(raw.get("triggerFog", False)),
            youtube_url=str(raw.get("youtubeUrl", "") or "").strip(),
            three_screen_video=bool(raw.get("threeScreenVideo", False)),
            video_hold_sec=_clamp(_f("videoHoldSec", 45.0), 5.0, 240.0),
        )


def default_shows() -> list[ShowSpec]:
    """Return built-in shows requested by Dave (PR drops, walk-ins, debut templates)."""
    return [
        ShowSpec(
            id="matt_pr",
            name="Matt PR Drop-In (Simply the Best)",
            kind="pr_drop",
            enabled=True,
            triggers=[
                "matt just hit a pr",
                "matt hit a pr",
                "matt pr",
                "matthew just hit a pr",
                "matthew hit a pr",
            ],
            denon_vol=70,
            cube_mode="duck",
            overlay_gain=1.0,
            announcer_voice="en-US-GuyNeural",
            announcer_rate="+6%",
            announcer_pitch="+2Hz",
            demo_style="anthem",
            demo_shout="Simply the best!!",
            song_start_sec=0.0,
            song_duration_sec=15.0,
            fade_out_sec=2.8,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=9.5,
        ),
        ShowSpec(
            id="matt_walkin",
            name="Matt Walk-In (Plow Bolt King + Juicy)",
            kind="walkin",
            enabled=True,
            triggers=[
                "matt just got here",
                "matt is in the gym",
                "matt is here",
                "matt walk in",
                "matthew is here",
                "matthew just got here",
            ],
            denon_vol=70,
            cube_mode="duck",
            overlay_gain=1.0,
            announcer_text=(
                "Ladies and gentlemen! Straight out of Brick, New Jersey... "
                "the Plow Bolt King of Toms River... Matthew... Cookson!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-8%",
            announcer_pitch="-4Hz",
            demo_style="boombap",
            song_start_sec=0.0,
            song_duration_sec=20.0,
            fade_out_sec=2.8,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
        ),
        ShowSpec(
            id="ryder_walkin",
            name="Ryder Curry Debut (Thunder + Fog + 3 Screens)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "ryder is here",
                "ryder just got here",
                "ryder walk in",
                "ryder debut",
                "rider is here",
                "rider just got here",
                "rider walk in",
                "rider debut",
            ],
            denon_vol=70,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text=(
                "Ladies and gentlemen! Out of Toms River, New Jersey... "
                "making his Armory debut... Ryder... Curry!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-8%",
            announcer_pitch="-5Hz",
            demo_style="thunder_only",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=1.5,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=3.0,
            add_thunder=True,
            lights_out=True,
            lights_flash_count=2,
            trigger_fog=True,
            youtube_url=RYDER_ENTRANCE_URL,
            three_screen_video=True,
            video_hold_sec=45.0,
        ),
        ShowSpec(
            id="jared_mark_walkin",
            name="Jared & Mark Walk-In (All the Way From Beantown)",
            kind="walkin",
            enabled=True,
            triggers=[
                "jared and mark are here",
                "jared and mark just got here",
                "jared and mark walk in",
                "mark and jared are here",
                "beantown walk in",
            ],
            denon_vol=70,
            cube_mode="duck",
            overlay_gain=1.0,
            announcer_text=(
                "Ladies and gentlemen! All the way from Beantown... "
                "stepping into The Armory... Jared... and Mark!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-7%",
            announcer_pitch="-4Hz",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=18.0,
            fade_out_sec=2.5,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
        ),
        ShowSpec(
            id="gabe_walkin",
            name="Gabe Walk-In (Template)",
            kind="walkin",
            enabled=True,
            triggers=[
                "gabe is here",
                "gabe just got here",
                "gabe walk in",
            ],
            denon_vol=70,
            cube_mode="duck",
            overlay_gain=1.0,
            announcer_text="Ladies and gentlemen! Stepping into The Armory... Gabe!!",
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-7%",
            announcer_pitch="-4Hz",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=15.0,
            fade_out_sec=2.5,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
        ),
        ShowSpec(
            id="josh_walkin",
            name="Josh Walk-In (Template)",
            kind="walkin",
            enabled=True,
            triggers=[
                "josh is here",
                "josh just got here",
                "josh walk in",
            ],
            denon_vol=70,
            cube_mode="duck",
            overlay_gain=1.0,
            announcer_text="Ladies and gentlemen! Stepping into The Armory... Josh!!",
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-7%",
            announcer_pitch="-4Hz",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=15.0,
            fade_out_sec=2.5,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
        ),
    ]


# ---------------- Stereo WAV & Procedural Arena Audio Helpers ----------------


def encode_stereo_pcm16_wav(pcm_lr: np.ndarray, sr: int = SHOW_SR) -> bytes:
    """Convert stereo (or mono) float32 array `[-1..1]` into 16-bit PCM WAV bytes."""
    arr = np.asarray(pcm_lr, dtype=np.float32)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    elif arr.shape[1] == 1:
        arr = np.repeat(arr, 2, axis=1)
    else:
        arr = arr[:, :2]
    if arr.shape[0] == 0:
        arr = np.zeros((max(1, int(sr * 0.05)), 2), dtype=np.float32)
    peak = float(np.max(np.abs(arr)))
    if peak > 0.96:
        arr = arr * (0.94 / peak)
    arr = np.clip(arr, -0.97, 0.97)
    pcm16 = np.round(arr * 32767.0).astype("<i2")
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm16.tobytes())
    return bio.getvalue()


def read_wav_stereo_float(wav_path: Path, target_sr: int = SHOW_SR) -> np.ndarray:
    """Read a PCM16 WAV file into `(N, 2)` float32 normalized in `[-1, 1]`."""
    with wave.open(str(wav_path), "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw != 2 or not frames:
        return np.zeros((0, 2), dtype=np.float32)
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch == 1:
        stereo = np.stack([arr, arr], axis=1)
    else:
        n = (arr.size // ch) * ch
        stereo = arr[:n].reshape(-1, ch)[:, :2]
    if sr != target_sr and stereo.shape[0] > 4:
        old_n = stereo.shape[0]
        new_n = max(1, int(round(old_n * float(target_sr) / float(sr))))
        xp = np.linspace(0.0, 1.0, old_n, endpoint=False)
        x = np.linspace(0.0, 1.0, new_n, endpoint=False)
        l = np.interp(x, xp, stereo[:, 0]).astype(np.float32)
        r = np.interp(x, xp, stereo[:, 1]).astype(np.float32)
        stereo = np.stack([l, r], axis=1)
    return stereo.astype(np.float32)


def wav_duration_sec(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            return round(float(wf.getnframes()) / float(max(1, wf.getframerate())), 2)
    except Exception:
        return 0.0


def trim_edge_silence_stereo(
    pcm: np.ndarray,
    sr: int = SHOW_SR,
    *,
    thresh: float = 0.014,
    pad_lead_ms: float = 12.0,
    pad_tail_ms: float = 80.0,
) -> np.ndarray:
    """Strip dead leading/trailing silence so cue onset is instantaneous."""
    if pcm.shape[0] == 0:
        return pcm
    env = np.max(np.abs(pcm), axis=1)
    hits = np.flatnonzero(env >= thresh)
    if hits.size == 0:
        return pcm
    lead = max(0, int(hits[0]) - int(round(pad_lead_ms * sr / 1000.0)))
    tail = min(pcm.shape[0], int(hits[-1]) + 1 + int(round(pad_tail_ms * sr / 1000.0)))
    return pcm[lead:tail].astype(np.float32)


def apply_fade_out_stereo(pcm: np.ndarray, sr: int = SHOW_SR, fade_out_sec: float = 2.5) -> np.ndarray:
    """Apply smooth cosine-squared fade-out to zero over the tail."""
    out = pcm.copy()
    n = out.shape[0]
    if n < 8:
        return out
    atk = min(n // 8, max(8, int(round(0.008 * sr))))
    in_env = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, atk, dtype=np.float32)))
    out[:atk] *= in_env[:, None]
    fade_n = min(n - atk, max(16, int(round(fade_out_sec * sr))))
    if fade_n > 1:
        t = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        f_env = (np.cos(0.5 * np.pi * t) ** 2).astype(np.float32)
        out[-fade_n:] *= f_env[:, None]
    return out


def mix_into(dst: np.ndarray, src: np.ndarray, offset_samples: int, gain: float = 1.0) -> None:
    """Add `src` stereo buffer into `dst` starting at `offset_samples`, clipping cleanly to bounds."""
    if src.shape[0] == 0 or dst.shape[0] == 0:
        return
    start = max(0, int(offset_samples))
    if start >= dst.shape[0]:
        return
    avail = min(src.shape[0], dst.shape[0] - start)
    dst[start : start + avail] += src[:avail] * float(gain)


def synth_crowd_cheer_stereo(
    dur_sec: float,
    sr: int = SHOW_SR,
    *,
    swell_sec: float = 1.1,
    fade_out_sec: float = 2.8,
) -> np.ndarray:
    """Procedural wide-stereo stadium crowd roar + rhythmic arena applause."""
    n = max(1, int(round(max(0.5, dur_sec) * sr)))
    rng = np.random.default_rng(2026)
    noise_l = rng.standard_normal(n).astype(np.float32)
    noise_r = rng.standard_normal(n).astype(np.float32)

    sos_low = butter(3, [210.0, 620.0], btype="bandpass", fs=sr, output="sos")
    sos_mid = butter(3, [680.0, 1650.0], btype="bandpass", fs=sr, output="sos")
    sos_clap = butter(2, [2100.0, 5200.0], btype="bandpass", fs=sr, output="sos")

    t = np.linspace(0.0, float(n) / sr, n, endpoint=False, dtype=np.float32)
    mod_l = 0.72 + 0.28 * np.sin(2.0 * np.pi * 1.45 * t + 0.3)
    mod_r = 0.72 + 0.28 * np.sin(2.0 * np.pi * 1.75 * t + 1.9)
    claps = np.maximum(0.0, np.sin(2.0 * np.pi * 6.4 * t) * 0.55 + 0.45)

    def _chan(raw: np.ndarray, mod: np.ndarray) -> np.ndarray:
        roar = 0.55 * sosfilt(sos_low, raw) + 0.55 * sosfilt(sos_mid, raw) + 0.28 * claps * sosfilt(sos_clap, raw)
        return (roar * mod).astype(np.float32)

    l = _chan(noise_l, mod_l)
    r = _chan(noise_r, mod_r)
    stereo = np.stack([0.82 * l + 0.18 * r, 0.82 * r + 0.18 * l], axis=1).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    if peak > 1e-6:
        stereo *= 0.80 / peak

    atk = min(n // 2, max(16, int(round(swell_sec * sr))))
    in_env = np.sin(0.5 * np.pi * np.linspace(0.0, 1.0, atk, dtype=np.float32)) ** 1.4
    stereo[:atk] *= in_env[:, None]
    return apply_fade_out_stereo(stereo, sr=sr, fade_out_sec=fade_out_sec)


def synth_thunder_rumble_stereo(dur_sec: float = 4.2, sr: int = SHOW_SR) -> np.ndarray:
    """Deep stadium sub-bass rolling thunder + initial lightning crack."""
    n = max(1, int(round(max(1.0, dur_sec) * sr)))
    rng = np.random.default_rng(7731)
    nl = rng.standard_normal(n).astype(np.float32)
    nr = rng.standard_normal(n).astype(np.float32)
    t = np.linspace(0.0, float(n) / sr, n, endpoint=False, dtype=np.float32)

    sos_sub = butter(3, [32.0, 145.0], btype="bandpass", fs=sr, output="sos")
    sos_crack = butter(2, [420.0, 2600.0], btype="bandpass", fs=sr, output="sos")

    env_rumble = (
        np.exp(-((t - 0.55) / 0.95) ** 2)
        + 0.72 * np.exp(-((t - 2.15) / 1.15) ** 2)
        + 0.40 * np.exp(-t / max(0.8, dur_sec * 0.45))
    )
    crack_env = np.exp(-np.maximum(0.0, t - 0.08) / 0.14) * (t >= 0.08)

    l = (sosfilt(sos_sub, nl) * env_rumble * 1.35 + sosfilt(sos_crack, nl) * crack_env * 0.35).astype(np.float32)
    r = (sosfilt(sos_sub, nr) * env_rumble * 1.35 + sosfilt(sos_crack, nr) * crack_env * 0.35).astype(np.float32)
    stereo = np.stack([l, r], axis=1)
    peak = float(np.max(np.abs(stereo)))
    if peak > 1e-6:
        stereo *= 0.88 / peak
    return apply_fade_out_stereo(stereo, sr=sr, fade_out_sec=min(1.1, dur_sec * 0.3))


def synth_demo_music_bed(style: str, dur_sec: float, sr: int = SHOW_SR) -> np.ndarray:
    """Instant-onset rhythmic demo music bed (`anthem` or `boombap`) starting loudly on sample 0."""
    n = max(1, int(round(max(1.0, dur_sec) * sr)))
    t = np.linspace(0.0, float(n) / sr, n, endpoint=False, dtype=np.float32)
    rng = np.random.default_rng(1994 if style == "boombap" else 1989)
    snare_noise = rng.standard_normal(n).astype(np.float32)
    sos_snare = butter(2, [850.0, 4800.0], btype="bandpass", fs=sr, output="sos")
    snare_hp = sosfilt(sos_snare, snare_noise).astype(np.float32)

    if style == "boombap":
        bpm = 96.0
        beat_sec = 60.0 / bpm
        chords = [
            [174.61, 207.65, 261.63, 311.13],  # Fm7
            [174.61, 207.65, 261.63, 311.13],
            [138.59, 174.61, 207.65, 261.63],  # Db maj7
            [130.81, 164.81, 196.00, 233.08],  # C7
        ]
        bass_freqs = [87.31, 87.31, 69.30, 65.41]
        kick_pattern = (0.0, 0.75, 1.5, 2.0, 2.5)  # beats per bar (4 beats)
        snare_pattern = (1.0, 3.0)
    else:
        bpm = 108.0
        beat_sec = 60.0 / bpm
        chords = [
            [174.61, 220.00, 261.63, 349.23],  # F major
            [146.83, 174.61, 220.00, 293.66],  # D minor
            [116.54, 146.83, 174.61, 233.08],  # Bb major
            [130.81, 164.81, 196.00, 261.63],  # C major
        ]
        bass_freqs = [87.31, 73.42, 58.27, 65.41]
        kick_pattern = (0.0, 1.0, 2.0, 2.5, 3.0)
        snare_pattern = (1.0, 3.0)

    bar_sec = 4.0 * beat_sec
    beats = t / beat_sec
    beat_in_bar = np.mod(beats, 4.0)
    bar_idx = np.mod((t // bar_sec).astype(np.int32), len(chords))

    # Instant kick right at t=0.00s
    kick = np.zeros(n, dtype=np.float32)
    for kp in kick_pattern:
        dt = np.mod(beat_in_bar - kp, 4.0) * beat_sec
        active = dt < 0.22
        env = np.where(active, np.exp(-dt / 0.055), 0.0)
        f_inst = 46.0 + 72.0 * np.exp(-dt / 0.025)
        kick += np.sin(2.0 * np.pi * f_inst * dt) * env

    # Crisp snare
    snare = np.zeros(n, dtype=np.float32)
    for sp in snare_pattern:
        dt = np.mod(beat_in_bar - sp, 4.0) * beat_sec
        active = dt < 0.18
        env = np.where(active, np.exp(-dt / 0.045), 0.0)
        snare += (0.55 * snare_hp + 0.45 * np.sin(2.0 * np.pi * 185.0 * dt)) * env

    # Closed hat on 8ths
    hat_dt = np.mod(beats * 2.0, 1.0) * (beat_sec * 0.5)
    hat = snare_hp * np.exp(-hat_dt / 0.018) * 0.16

    # Chords & driving bass
    harmony_l = np.zeros(n, dtype=np.float32)
    harmony_r = np.zeros(n, dtype=np.float32)
    bass = np.zeros(n, dtype=np.float32)
    chord_env = 0.45 + 0.55 * np.exp(-np.mod(beats, 1.0) * (1.8 if style == "boombap" else 0.9))
    for ci, freqs in enumerate(chords):
        mask = bar_idx == ci
        if not np.any(mask):
            continue
        tm = t[mask]
        b_f = bass_freqs[ci]
        bass[mask] = 0.65 * np.sin(2.0 * np.pi * b_f * tm) + 0.25 * np.sin(2.0 * np.pi * (2.0 * b_f) * tm)
        hl = np.zeros_like(tm)
        hr = np.zeros_like(tm)
        for j, f in enumerate(freqs):
            hl += (0.45 / (1 + 0.2 * j)) * np.sin(2.0 * np.pi * f * tm)
            hr += (0.45 / (1 + 0.2 * j)) * np.sin(2.0 * np.pi * (f * 1.003) * tm)
            if style != "boombap":
                hl += 0.14 * np.sin(2.0 * np.pi * (f * 2.0) * tm)
                hr += 0.14 * np.sin(2.0 * np.pi * (f * 2.005) * tm)
        harmony_l[mask] = np.tanh(1.8 * hl) * chord_env[mask]
        harmony_r[mask] = np.tanh(1.8 * hr) * chord_env[mask]

    drums = 0.62 * kick + 0.36 * snare + hat
    l = np.tanh(0.85 * (drums + 0.42 * bass + 0.46 * harmony_l)).astype(np.float32)
    r = np.tanh(0.85 * (drums + 0.42 * bass + 0.46 * harmony_r)).astype(np.float32)
    return np.stack([l, r], axis=1)


def pad_wav_for_cube_duck(
    wav_bytes: bytes,
    *,
    lead_silence_ms: int = 220,
    tail_silence_ms: int = 1400,
) -> tuple[bytes, float]:
    """Pad WAV with leading duck-settle silence & trailing Denon-ramp-down silence."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sw != 2 or not frames:
        return wav_bytes, 0.0

    arr = np.frombuffer(frames, dtype="<i2").reshape(-1, ch)
    est_bytes = len(frames) + int(((lead_silence_ms + tail_silence_ms) / 1000.0) * sr * ch * 2) + 44
    # If file would exceed Cube's 12 MB HTTP limit (11 MB margin), downsample cleanly to 24 kHz
    if est_bytes > MAX_CUBE_WAV_BYTES and sr > 24_000:
        step = 2 if sr // 2 >= 20_000 else max(2, int(round(sr / 24_000.0)))
        arr = arr[::step]
        sr = max(8_000, sr // step)

    lead_n = max(0, int(round((lead_silence_ms / 1000.0) * sr)))
    tail_n = max(0, int(round((tail_silence_ms / 1000.0) * sr)))
    padded = np.concatenate(
        [
            np.zeros((lead_n, ch), dtype="<i2"),
            arr,
            np.zeros((tail_n, ch), dtype="<i2"),
        ],
        axis=0,
    )
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(padded.tobytes())
    dur_sec = float(padded.shape[0]) / float(max(1, sr))
    return bio.getvalue(), dur_sec


# ---------------- Announcer TTS & Song Decoding Pipeline ----------------


def synthesize_announcer_wav(
    text: str,
    *,
    voice: str = DEFAULT_ANNOUNCER_VOICE,
    rate: str = "-6%",
    pitch: str = "-4Hz",
    out_wav: Path,
    ffmpeg_bin: str = "",
    allow_network: bool = True,
) -> Path:
    """Synthesize a stadium PA arena-announcer WAV using Edge Neural TTS + FFmpeg reverb/EQ."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if not clean or not allow_network:
        # Offline/test mode deterministic tonal intro
        t_dur = 0.8 if not clean else 1.2
        pcm = synth_demo_music_bed("anthem", t_dur, sr=SHOW_SR) * 0.55
        pcm = apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=0.25)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    import asyncio
    import edge_tts

    async def _fetch(use_pitch: bool) -> bytes:
        kwargs: dict[str, Any] = {"voice": voice, "rate": rate}
        if use_pitch and pitch:
            kwargs["pitch"] = pitch
        comm = edge_tts.Communicate(clean, **kwargs)
        chunks: list[bytes] = []
        async for ch in comm.stream():
            if ch.get("type") == "audio":
                chunks.append(ch["data"])
        return b"".join(chunks)

    mp3_bytes = b""
    for attempt in range(3):
        try:
            mp3_bytes = asyncio.run(_fetch(use_pitch=(attempt == 0)))
            if mp3_bytes:
                break
        except Exception:
            time.sleep(0.25 * (attempt + 1))

    if not mp3_bytes:
        pcm = synth_demo_music_bed("anthem", 1.2, sr=SHOW_SR) * 0.55
        pcm = apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=0.25)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    raw_mp3 = out_wav.with_suffix(".raw.mp3")
    tmp_wav = out_wav.with_suffix(".wav.tmp")
    raw_mp3.write_bytes(mp3_bytes)
    try:
        if ffmpeg_bin:
            pa_filter = (
                "silenceremove=start_periods=1:start_silence=0.02:start_threshold=-40dB,"
                "highpass=f=85,"
                "equalizer=f=240:width_type=o:width=1.2:g=3.5,"
                "equalizer=f=2800:width_type=o:width=1.2:g=4.0,"
                "acompressor=threshold=-18dB:ratio=4:attack=8:release=120:makeup=4dB,"
                "aecho=0.82:0.74:55|115:0.26|0.15,"
                "alimiter=limit=0.95"
            )
            proc = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(raw_mp3),
                    "-af",
                    pa_filter,
                    "-ar",
                    str(SHOW_SR),
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(tmp_wav),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                **_no_window_kwargs(),
            )
            if proc.returncode == 0 and tmp_wav.is_file() and tmp_wav.stat().st_size > 100:
                pcm = trim_edge_silence_stereo(read_wav_stereo_float(tmp_wav, target_sr=SHOW_SR), sr=SHOW_SR)
                tmp_wav.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
                tmp_wav.replace(out_wav)
                return out_wav

        # Fallback PyAV decode if ffmpeg binary is unavailable
        import av

        resampler = av.AudioResampler(format="flt", layout="stereo", rate=SHOW_SR)
        parts: list[np.ndarray] = []
        with av.open(io.BytesIO(mp3_bytes), format="mp3") as container:
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    parts.append(rf.to_ndarray().reshape(-1, 2))
            for rf in resampler.resample(None):
                parts.append(rf.to_ndarray().reshape(-1, 2))
        pcm = np.concatenate(parts, axis=0).astype(np.float32) if parts else np.zeros((SHOW_SR, 2), dtype=np.float32)
        pcm = trim_edge_silence_stereo(pcm, sr=SHOW_SR)
        tmp_wav.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp_wav.replace(out_wav)
        return out_wav
    finally:
        try:
            raw_mp3.unlink(missing_ok=True)
            if tmp_wav.exists():
                tmp_wav.unlink(missing_ok=True)
        except Exception:
            pass


def decode_and_trim_song_file(
    src_path: Path,
    *,
    start_sec: float = 0.0,
    duration_sec: float = 15.0,
    fade_out_sec: float = 2.5,
    trim_leading_silence: bool = True,
    ffmpeg_bin: str = "",
) -> np.ndarray:
    """Decode uploaded audio file at exact `start_sec`, optional leading-silence strip, exact duration & fade."""
    dur = max(1.0, float(duration_sec or 15.0))
    start = max(0.0, float(start_sec or 0.0))

    if src_path.suffix.lower() == ".wav" and not ffmpeg_bin:
        pcm = read_wav_stereo_float(src_path, target_sr=SHOW_SR)
        s0 = int(round(start * SHOW_SR))
        pcm = pcm[s0:]
        if trim_leading_silence:
            pcm = trim_edge_silence_stereo(pcm, sr=SHOW_SR, thresh=0.015, pad_lead_ms=8.0, pad_tail_ms=0.0)
        max_n = int(round(dur * SHOW_SR))
        if pcm.shape[0] > max_n:
            pcm = pcm[:max_n]
    elif ffmpeg_bin:
        tmp_out = src_path.parent / f".decode_{threading.get_ident()}_{int(time.time() * 1000)}.wav"
        try:
            cmd = [ffmpeg_bin, "-y"]
            if start > 0.001:
                cmd += ["-ss", f"{start:.3f}"]
            cmd += ["-i", str(src_path)]
            filters: list[str] = []
            if trim_leading_silence:
                filters.append("silenceremove=start_periods=1:start_silence=0.02:start_threshold=-38dB")
            filters.append("dynaudnorm=f=150:g=11:p=0.92")
            cmd += [
                "-t",
                f"{dur:.3f}",
                "-af",
                ",".join(filters),
                "-ar",
                str(SHOW_SR),
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(tmp_out),
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=45,
                **_no_window_kwargs(),
            )
            if proc.returncode != 0 or not tmp_out.is_file():
                err = proc.stderr.decode("utf-8", errors="ignore")[-240:]
                raise RuntimeError(f"FFmpeg decode failed: {err}")
            pcm = read_wav_stereo_float(tmp_out, target_sr=SHOW_SR)
            if trim_leading_silence:
                pcm = trim_edge_silence_stereo(pcm, sr=SHOW_SR, thresh=0.012, pad_lead_ms=6.0, pad_tail_ms=0.0)
            max_n = int(round(dur * SHOW_SR))
            if pcm.shape[0] > max_n:
                pcm = pcm[:max_n]
        finally:
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        raise RuntimeError("FFmpeg is required to decode compressed audio files (.mp3/.m4a/.ogg)")

    if pcm.shape[0] == 0:
        raise RuntimeError("Trimmed audio segment is empty (check songStartSec)")
    peak = float(np.max(np.abs(pcm)))
    if peak > 1e-6:
        pcm = (pcm * (0.88 / peak)).astype(np.float32)
    return apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=fade_out_sec)


# ---------------- ShowDirector Orchestrator ----------------


class ShowDirector:
    """Manages show specs, custom song uploads, composite WAV rendering, voice triggers & live playback."""

    def __init__(
        self,
        *,
        root_dir: Path,
        house_audio: Any = None,
        displays: Any = None,
        adb_controller: Any = None,
        wallboard_cfg_fn: Callable[[], Any] | None = None,
        ffmpeg_bin: str = "",
        allow_network: bool = True,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.root_dir = Path(root_dir)
        self.shows_dir = self.root_dir / "shows"
        self.sources_dir = self.shows_dir / "sources"
        self.rendered_dir = self.shows_dir / "rendered"
        self.config_path = self.root_dir / "shows.json"
        for d in (self.shows_dir, self.sources_dir, self.rendered_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.house_audio = house_audio
        self.displays = displays
        self.adb = adb_controller
        self._wallboard_cfg_fn = wallboard_cfg_fn
        self.ffmpeg_bin = str(ffmpeg_bin or "").strip()
        self.allow_network = bool(allow_network)
        self._http_get = http_get
        self._http_post = http_post
        self._has_custom_sleep = sleep_fn is not None
        self._sleep = sleep_fn or time.sleep
        self.log = logging.getLogger("armory-shows")

        self.fx_hooks = SmartFxConfig()
        self.shows: dict[str, ShowSpec] = {}
        self._show_order: list[str] = []

        self._run_lock = threading.Lock()
        self._active_run_id: int = 0
        self._active_cancel: threading.Event | None = None
        self._active_thread: threading.Thread | None = None
        self._active_state: dict[str, Any] = {
            "active": False,
            "showId": "",
            "showName": "",
            "phase": "idle",
            "startedAt": "",
            "lastError": "",
        }
        self._last_result: dict[str, Any] = {}

        self._load_or_init()

    # ---------- Persistence & Configuration ----------

    def _load_or_init(self) -> None:
        defaults = default_shows()
        with self._lock:
            self.shows = {s.id: s for s in defaults}
            self._show_order = [s.id for s in defaults]
            dirty = not self.config_path.exists()
            if self.config_path.exists():
                try:
                    raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        if isinstance(raw.get("fxHooks"), dict):
                            self.fx_hooks = SmartFxConfig.from_dict(raw["fxHooks"])
                        raw_shows = raw.get("shows")
                        if isinstance(raw_shows, dict):
                            for sid, sraw in raw_shows.items():
                                if isinstance(sraw, dict):
                                    s_merged = {"id": sid, **sraw}
                                    spec = ShowSpec.from_dict(s_merged)
                                    self.shows[spec.id] = spec
                                    if spec.id not in self._show_order:
                                        self._show_order.append(spec.id)
                except Exception as exc:
                    self.log.warning("Failed parsing shows.json (%s); using defaults", exc)
                    dirty = True
            if dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "fxHooks": self.fx_hooks.to_dict(),
            "shows": {sid: self.shows[sid].to_dict() for sid in self._show_order if sid in self.shows},
        }
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def get_show(self, show_id: str) -> ShowSpec | None:
        sid = _safe_show_id(show_id)
        with self._lock:
            return self.shows.get(sid)

    def preview_wav_path(self, show_id: str) -> Path:
        sid = _safe_show_id(show_id)
        return self.rendered_dir / f"{sid}_preview.wav"

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            items: list[dict[str, Any]] = []
            for sid in self._show_order:
                spec = self.shows.get(sid)
                if not spec:
                    continue
                p_wav = self.preview_wav_path(sid)
                ready = p_wav.is_file() and p_wav.stat().st_size > 100
                dur = wav_duration_sec(p_wav) if ready else 0.0
                items.append({
                    **spec.to_dict(),
                    "previewReady": ready,
                    "previewDurationSec": dur,
                    "previewUrl": f"/api/shows/{sid}/preview.wav",
                    "sourceUploaded": bool(spec.source_file and (self.sources_dir / spec.source_file).is_file()),
                })
            return {
                "shows": items,
                "fxHooks": self.fx_hooks.to_dict(),
                "active": dict(self._active_state),
                "lastResult": dict(self._last_result),
            }

    def update_show(self, show_id: str, patch: dict[str, Any], *, rebuild: bool = True) -> dict[str, Any]:
        sid = _safe_show_id(show_id)
        with self._lock:
            cur = self.shows.get(sid)
            if cur is None:
                raise KeyError(f"Unknown show: {show_id}")
            merged = {**cur.to_dict(), **(patch or {})}
            merged["id"] = sid
            if patch and patch.get("clearSource"):
                old_src = cur.source_file
                merged["sourceFile"] = ""
                if old_src:
                    other_refs = any(
                        s.id != sid and s.source_file == old_src for s in self.shows.values()
                    )
                    if not other_refs:
                        try:
                            (self.sources_dir / old_src).unlink(missing_ok=True)
                        except Exception:
                            pass
            if patch and isinstance(patch.get("triggersText"), str):
                merged["triggers"] = [
                    line.strip()
                    for line in re.split(r"[\r\n,]+", patch["triggersText"])
                    if line.strip()
                ]
            updated = ShowSpec.from_dict(merged)
            self.shows[sid] = updated
            self._save_locked()

        build_res = self.build_show_preview(sid, force=True) if rebuild else {"ok": True}
        return {"ok": True, "show": self.get_show(sid).to_dict(), "build": build_res, "status": self.status_dict()}

    def save_uploaded_song(
        self,
        show_id: str,
        raw_filename: str,
        file_bytes: bytes,
        *,
        song_start_sec: float | None = None,
        song_duration_sec: float | None = None,
        trim_leading_silence: bool | None = None,
    ) -> dict[str, Any]:
        sid = _safe_show_id(show_id)
        if not self.get_show(sid):
            raise KeyError(f"Unknown show: {show_id}")
        if not file_bytes or len(file_bytes) > MAX_SONG_UPLOAD_BYTES:
            raise ValueError("Audio upload must be non-empty and <= 30 MB")
        safe_name = sanitize_upload_filename(raw_filename, sid)
        dest = self.sources_dir / safe_name
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(file_bytes)
        tmp.replace(dest)

        patch: dict[str, Any] = {"sourceFile": safe_name}
        if song_start_sec is not None:
            patch["songStartSec"] = float(song_start_sec)
        if song_duration_sec is not None:
            patch["songDurationSec"] = float(song_duration_sec)
        if trim_leading_silence is not None:
            patch["trimLeadingSilence"] = bool(trim_leading_silence)
        return self.update_show(sid, patch, rebuild=True)

    def update_fx_hooks(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = {**self.fx_hooks.to_dict(), **(raw or {})}
            self.fx_hooks = SmartFxConfig.from_dict(merged)
            self._save_locked()
            return {"ok": True, "fxHooks": self.fx_hooks.to_dict()}

    # ---------- Composite Preview Audio Builder ----------

    def _get_announcer_stem(self, spec: ShowSpec, *, suffix: str = "announcer", text_override: str | None = None) -> np.ndarray | None:
        txt = str(text_override if text_override is not None else spec.announcer_text).strip()
        if not txt:
            return None
        key_src = f"{txt}|{spec.announcer_voice}|{spec.announcer_rate}|{spec.announcer_pitch}|{int(self.allow_network)}"
        digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]
        stem_path = self.rendered_dir / f"{spec.id}_{suffix}_{digest}.wav"
        if not stem_path.is_file() or stem_path.stat().st_size < 100:
            synthesize_announcer_wav(
                txt,
                voice=spec.announcer_voice,
                rate=spec.announcer_rate,
                pitch=spec.announcer_pitch,
                out_wav=stem_path,
                ffmpeg_bin=self.ffmpeg_bin,
                allow_network=self.allow_network,
            )
        pcm = read_wav_stereo_float(stem_path, target_sr=SHOW_SR)
        return pcm if pcm.shape[0] > 0 else None

    def build_show_preview(self, show_id: str, *, force: bool = False) -> dict[str, Any]:
        """Render the tight local preview WAV (`shows/rendered/<id>_preview.wav`) with zero lead-in waste."""
        spec = self.get_show(show_id)
        if spec is None:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        out_wav = self.preview_wav_path(spec.id)
        if not force and out_wav.is_file() and out_wav.stat().st_size > 200:
            return {"ok": True, "cached": True, "path": str(out_wav), "durationSec": wav_duration_sec(out_wav)}

        src_path = (self.sources_dir / spec.source_file) if spec.source_file else None
        has_custom_song = bool(src_path and src_path.is_file())
        song_dur = float(spec.song_duration_sec or (15.0 if spec.kind == "pr_drop" else 18.0))

        # 1. Load or synthesize music bed (only if duration > 0 or custom song present)
        music_pcm: np.ndarray | None = None
        used_custom = False
        if has_custom_song and song_dur > 0.2:
            music_pcm = decode_and_trim_song_file(
                src_path,  # type: ignore[arg-type]
                start_sec=spec.song_start_sec,
                duration_sec=song_dur,
                fade_out_sec=spec.fade_out_sec,
                trim_leading_silence=spec.trim_leading_silence,
                ffmpeg_bin=self.ffmpeg_bin,
            )
            used_custom = True
        elif spec.kind != "debut_video" and song_dur > 0.2:
            music_pcm = synth_demo_music_bed(spec.demo_style, song_dur, sr=SHOW_SR)
            if spec.demo_shout and spec.kind == "pr_drop":
                shout_pcm = self._get_announcer_stem(spec, suffix="shout", text_override=spec.demo_shout)
                if shout_pcm is not None and shout_pcm.shape[0] > 0:
                    # Duck demo bed slightly under the quick hype shout right at beat 1 (t=0.03s)
                    s0 = int(round(0.03 * SHOW_SR))
                    s1 = min(music_pcm.shape[0], s0 + shout_pcm.shape[0])
                    if s1 > s0:
                        music_pcm[s0:s1] *= 0.68
                        mix_into(music_pcm, shout_pcm, s0, gain=0.90)
            music_pcm = apply_fade_out_stereo(music_pcm, sr=SHOW_SR, fade_out_sec=spec.fade_out_sec)

        # 2. Assemble by show kind
        if spec.kind == "pr_drop":
            assert music_pcm is not None
            cheer_tail_extra = 1.6 if spec.add_cheer else 0.0
            total_n = music_pcm.shape[0] + int(round(cheer_tail_extra * SHOW_SR))
            mix = np.zeros((total_n, 2), dtype=np.float32)
            mix_into(mix, music_pcm, 0, gain=1.0)
            if spec.add_cheer:
                c_start = min(max(0, int(round(spec.cheer_delay_sec * SHOW_SR))), max(0, total_n - int(1.5 * SHOW_SR)))
                c_len = max(1.5, float(total_n - c_start) / float(SHOW_SR))
                cheer = synth_crowd_cheer_stereo(
                    c_len,
                    sr=SHOW_SR,
                    swell_sec=min(1.4, c_len * 0.35),
                    fade_out_sec=min(spec.fade_out_sec + 0.3, c_len * 0.6),
                )
                mix_into(mix, cheer, c_start, gain=0.68)
            mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=min(spec.fade_out_sec, 3.0))

        elif spec.kind == "debut_video":
            ann_pcm = self._get_announcer_stem(spec, suffix="announcer")
            if ann_pcm is None:
                ann_pcm = np.zeros((int(2.0 * SHOW_SR), 2), dtype=np.float32)
            pre_thunder_n = int(round(2.3 * SHOW_SR))
            post_tail_n = int(round(1.6 * SHOW_SR))
            total_n = pre_thunder_n + ann_pcm.shape[0] + post_tail_n
            mix = np.zeros((total_n, 2), dtype=np.float32)
            thunder = synth_thunder_rumble_stereo(float(total_n) / float(SHOW_SR), sr=SHOW_SR)
            mix_into(mix, thunder, 0, gain=0.82)
            # Duck thunder lightly during the HBO announcement so every word cuts through
            mix[pre_thunder_n : pre_thunder_n + ann_pcm.shape[0]] *= 0.42
            mix_into(mix, ann_pcm, pre_thunder_n, gain=1.0)
            if spec.add_cheer:
                c_start = pre_thunder_n + int(ann_pcm.shape[0] * 0.58)
                c_len = max(1.5, float(total_n - c_start) / float(SHOW_SR))
                cheer = synth_crowd_cheer_stereo(c_len, sr=SHOW_SR, swell_sec=0.8, fade_out_sec=1.3)
                mix_into(mix, cheer, c_start, gain=0.58)
            mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=min(1.3, float(post_tail_n) / SHOW_SR))

        else:  # "walkin": announcer intro -> tight crossfade into walk-in song
            ann_pcm = self._get_announcer_stem(spec, suffix="announcer")
            ann_n = ann_pcm.shape[0] if ann_pcm is not None else 0
            overlap_n = min(ann_n // 4, int(round(0.30 * SHOW_SR))) if (ann_pcm is not None and music_pcm is not None) else 0
            song_off = max(0, ann_n - overlap_n)
            song_n = music_pcm.shape[0] if music_pcm is not None else 0
            total_n = max(SHOW_SR, song_off + song_n)
            mix = np.zeros((total_n, 2), dtype=np.float32)
            if spec.add_cheer and ann_n > 0:
                cheer_bed = synth_crowd_cheer_stereo(
                    float(ann_n) / SHOW_SR + 1.5,
                    sr=SHOW_SR,
                    swell_sec=0.5,
                    fade_out_sec=1.4,
                )
                mix_into(mix, cheer_bed, 0, gain=0.22)
            if ann_pcm is not None:
                mix_into(mix, ann_pcm, 0, gain=1.0)
            if music_pcm is not None:
                # 180ms ramp-in on the song entry when overlapping announcer end
                s_entry = music_pcm.copy()
                if overlap_n > 16:
                    r_n = min(overlap_n, s_entry.shape[0])
                    s_entry[:r_n] *= np.linspace(0.25, 1.0, r_n, dtype=np.float32)[:, None]
                mix_into(mix, s_entry, song_off, gain=0.95)
            mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=spec.fade_out_sec)

        wav_bytes = encode_stereo_pcm16_wav(mix, sr=SHOW_SR)
        tmp_wav = out_wav.with_suffix(".wav.tmp")
        tmp_wav.write_bytes(wav_bytes)
        tmp_wav.replace(out_wav)
        dur_actual = round(float(mix.shape[0]) / float(SHOW_SR), 2)
        return {
            "ok": True,
            "showId": spec.id,
            "path": str(out_wav),
            "durationSec": dur_actual,
            "usedCustomSource": used_custom,
        }

    def rebuild_all_previews(self, *, force: bool = True) -> dict[str, Any]:
        results: dict[str, Any] = {}
        with self._lock:
            ids = list(self._show_order)
        for sid in ids:
            try:
                results[sid] = self.build_show_preview(sid, force=force)
            except Exception as exc:
                results[sid] = {"ok": False, "error": str(exc)}
        return results

    # ---------- Voice Matching ----------

    def match_voice_command(self, transcript: str, wake_word: str = "armory") -> dict[str, Any] | None:
        """Match spoken show triggers (`Matt just hit a PR`, `Matt just got here`, `stop show`, etc.)."""
        cmd = _strip_optional_wake(transcript, wake_word=wake_word)
        if not cmd:
            return None

        if re.search(r"\b(?:stop|kill|cancel|abort)\s+(?:the\s+)?(?:show|walk\s*in|drop\s*in|entrance)\b", cmd):
            return {"action": "stop"}

        best: tuple[int, ShowSpec] | None = None
        with self._lock:
            specs = [self.shows[sid] for sid in self._show_order if sid in self.shows and self.shows[sid].enabled]

        for spec in specs:
            for trig in spec.triggers:
                nt = _norm_trigger_text(trig)
                if not nt:
                    continue
                if re.search(rf"\b{re.escape(nt)}\b", cmd):
                    if best is None or len(nt) > best[0]:
                        best = (len(nt), spec)
        if best is None:
            return None
        spec = best[1]
        return {"action": "fire", "showId": spec.id, "showName": spec.name}

    # ---------- HTTP & Smart FX Helpers ----------

    def _fire_http_hook(self, url: str) -> dict[str, Any]:
        clean = str(url or "").strip()
        if not clean:
            return {"ok": False, "skipped": True}
        try:
            if self._http_get is not None:
                code, _ = self._http_get(clean, 1.5)
                return {"ok": code < 400, "url": clean, "status": code}
            if not self.allow_network:
                return {"ok": False, "skipped": True, "error": "Network disabled"}
            req = urllib.request.Request(clean, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return {"ok": int(resp.status) < 400, "url": clean, "status": int(resp.status)}
        except Exception as exc:
            return {"ok": False, "url": clean, "error": str(exc)}

    def _flash_lights(self, count: int, cancel_ev: threading.Event | None = None) -> None:
        with self._lock:
            fx = self.fx_hooks
        on_url = fx.resolve_lights_url(True)
        off_url = fx.resolve_lights_url(False)
        if not on_url or not off_url or count <= 0:
            return
        for _ in range(count):
            if cancel_ev and cancel_ev.is_set():
                return
            self._fire_http_hook(on_url)
            if self._wait_cancelable(cancel_ev, 0.18):
                return
            self._fire_http_hook(off_url)
            if self._wait_cancelable(cancel_ev, 0.16):
                return
        self._fire_http_hook(on_url)

    def _wait_cancelable(self, cancel_ev: threading.Event | None, seconds: float) -> bool:
        """Sleep up to `seconds`; returns True immediately if canceled."""
        rem = max(0.0, float(seconds))
        if rem <= 0.0:
            return bool(cancel_ev and cancel_ev.is_set())
        if cancel_ev is None or self._has_custom_sleep:
            if cancel_ev is not None and cancel_ev.is_set():
                return True
            self._sleep(rem)
            return bool(cancel_ev and cancel_ev.is_set())
        return cancel_ev.wait(timeout=rem)

    # ---------- 3-Screen Synchronized YouTube Launch ----------

    def _launch_three_screen_youtube(self, youtube_url: str) -> dict[str, Any]:
        url = normalize_youtube_tv_url(youtube_url)
        if not url:
            return {"ok": False, "error": "Empty YouTube URL"}
        results: dict[str, Any] = {}

        # 1. Put Denon Monitor Out on AUTO (Dual) so Hisense Main TV (HDMI 2) + TCL SmartSpin mirror Cube
        if self.displays is not None:
            try:
                results["avrMode"] = self.displays._control_avr("mch_stereo")
            except Exception as exc:
                results["avrMode"] = {"ok": False, "error": str(exc)}
            try:
                results["avrMonitor"] = self.displays.set_denon_monitor_out("AUTO")
            except Exception as exc:
                results["avrMonitor"] = {"ok": False, "error": str(exc)}
            try:
                results["tv"] = self.displays._control_primary("hdmi2")
            except Exception as exc:
                results["tv"] = {"ok": False, "error": str(exc)}
            try:
                if self.displays.get_config().spin.host:
                    results["spin"] = self.displays._control_spin("on")
            except Exception as exc:
                results["spin"] = {"ok": False, "error": str(exc)}

        # 2. Wake Wallboard Fire TV, silence Wallboard internal speakers (Denon carries sound!), launch YouTube
        if self.displays is not None and self.adb is not None:
            wb_cfg = self._wallboard_cfg_fn() if self._wallboard_cfg_fn is not None else None
            wb_host = str(getattr(wb_cfg, "fire_tv_host", "") or "").strip() if wb_cfg else ""
            wb_port = int(getattr(wb_cfg, "adb_port", 5555) or 5555) if wb_cfg else 5555
            if wb_host:
                wb_ser = f"{wb_host}:{wb_port}" if ":" not in wb_host else wb_host
                try:
                    results["wallboardWake"] = self.displays.execute("wallboard", "on")
                    st, _ = self.adb._connect_and_status(wb_ser)
                    if st == "device":
                        self.adb._run(["-s", wb_ser, "shell", "media", "volume", "--stream", "3", "--set", "0"], timeout=4.0)
                        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)} com.amazon.firetv.youtube"
                        rc, out, err = self.adb._run(["-s", wb_ser, "shell", cmd], timeout=6.0)
                        if rc != 0:
                            cmd_fb = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
                            rc, out, err = self.adb._run(["-s", wb_ser, "shell", cmd_fb], timeout=6.0)
                        results["wallboardVideo"] = {"ok": rc == 0, "url": url, "out": (out or err).strip()}
                    else:
                        results["wallboardVideo"] = {"ok": False, "state": st}
                except Exception as exc:
                    results["wallboardVideo"] = {"ok": False, "error": str(exc)}

        # 3. Launch YouTube on Fire TV Cube (audio flows to Denon @ MV70, video mirrors to Hisense + SmartSpin!)
        cube_host = ""
        cube_port = 5555
        if self.displays is not None:
            dcfg = self.displays.get_config()
            cube_host = dcfg.cube_host
            cube_port = dcfg.cube_adb_port
        elif self.house_audio is not None:
            hcfg = self.house_audio.get_config()
            cube_host = hcfg.cube_host
            cube_port = hcfg.cube_adb_port

        if cube_host and self.adb is not None:
            c_ser = f"{cube_host}:{cube_port}" if ":" not in cube_host else cube_host
            try:
                st, detail = self.adb._connect_and_status(c_ser)
                if st == "device":
                    self.adb._run(["-s", c_ser, "shell", "input", "keyevent", "224"], timeout=4.0)
                    cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)} com.amazon.firetv.youtube"
                    rc, out, err = self.adb._run(["-s", c_ser, "shell", cmd], timeout=6.0)
                    results["cubeVideo"] = {"ok": rc == 0, "url": url, "out": (out or err).strip()}
                else:
                    results["cubeVideo"] = {"ok": False, "state": st, "error": detail}
            except Exception as exc:
                results["cubeVideo"] = {"ok": False, "error": str(exc)}

        return {"ok": True, "url": url, "results": results}

    # ---------- Anti-Roller-Coaster Live Execution ----------

    def _restore_denon_down(self, from_db: float, to_db: float, *, fast: bool = False) -> None:
        if self.house_audio is None:
            return
        try:
            if abs(from_db - to_db) > 0.4:
                self.house_audio.denon._ramp_volume(
                    from_db,
                    to_db,
                    steps=(3 if fast else 6),
                    step_ms=(50 if fast else 135),
                )
            else:
                self.house_audio.denon.send_command(db_to_denon_mv(to_db))
        except Exception:
            pass

    def _run_show_sequence(self, spec: ShowSpec, run_id: int, cancel_ev: threading.Event) -> dict[str, Any]:
        with self._lock:
            fx = SmartFxConfig.from_dict(self.fx_hooks.to_dict())

        prev_db = -35.0
        target_db = _clamp(float(spec.denon_vol) - 80.0, -50.0, -5.0)
        denon_raised = False
        denon_restored = False
        cube_started = False
        was_standby = False
        out_res: dict[str, Any] = {"ok": True, "showId": spec.id}

        try:
            # 1. Guarantee tight preview WAV exists
            build_info = self.build_show_preview(spec.id, force=False)
            p_wav = self.preview_wav_path(spec.id)
            if not p_wav.is_file():
                raise RuntimeError(build_info.get("error") or "Preview WAV missing")
            raw_wav = p_wav.read_bytes()
            preview_dur = wav_duration_sec(p_wav)

            # Pad 220ms silent lead-in (Cube ducks BEFORE Denon ramps up)
            # & 1400ms silent tail (Denon ramps back down BEFORE Cube releases duck)
            lead_ms = 220
            tail_ms = 250 if spec.three_screen_video else 1400
            padded_wav, total_padded_sec = pad_wav_for_cube_duck(
                raw_wav, lead_silence_ms=lead_ms, tail_silence_ms=tail_ms
            )

            # 2. Snapshot Denon AVR baseline
            if self.house_audio is not None:
                st = self.house_audio.denon.get_status()
                if st.get("ok"):
                    was_standby = st.get("power") != "ON"
                    if st.get("volumeDb") is not None:
                        prev_db = float(st["volumeDb"])
                    if was_standby:
                        self.house_audio.denon.send_command("PWON")
                        if self._wait_cancelable(cancel_ev, 0.35):
                            return {"ok": False, "canceled": True}
                        self.house_audio.denon.send_command("SIMPLAY")
                        self.house_audio.denon.send_command("MSMCH STEREO")
                        prev_db = min(prev_db, target_db)

            # 3. Pre-show smart FX hooks (lights out + fog machine burst)
            if spec.lights_out:
                l_off = fx.resolve_lights_url(False)
                if l_off:
                    out_res["lightsOff"] = self._fire_http_hook(l_off)
            if spec.trigger_fog:
                fog_url = fx.resolve_fog_url()
                if fog_url:
                    out_res["fog"] = self._fire_http_hook(fog_url)

            # 4. PHASE 1 (ANTI-ROLLER-COASTER ENTRY):
            # Fire Cube first (sync=False -> requests MAY_DUCK & starts 220ms silent lead-in immediately!)
            if self.house_audio is not None:
                cfg_ha = self.house_audio.get_config()
                if cfg_ha.cube_host:
                    with self._lock:
                        self._active_state["phase"] = "duck_and_drop"
                    c_res = self.house_audio.cube.play_wav_bytes(
                        cfg_ha.cube_host,
                        cfg_ha.cube_port,
                        padded_wav,
                        mode=spec.cube_mode,
                        usage=cfg_ha.cube_usage,
                        gain=spec.overlay_gain,
                        max_sec=max(5, min(60, int(math.ceil(total_padded_sec + 3.0)))),
                        sync=False,
                    )
                    out_res["cubePlay"] = c_res
                    cube_started = bool(c_res.get("ok"))

            # Wait briefly so Cube completes AudioFocus duck before Denon moves upward
            if self._wait_cancelable(cancel_ev, 0.08):
                return {"ok": False, "canceled": True}

            # 5. Ramp Denon smoothly up to target show level (e.g., MV70 = -10.0 dB) right during lead-in
            if self.house_audio is not None:
                self.house_audio.denon._ramp_volume(prev_db, target_db, steps=4, step_ms=35)
                denon_raised = True

            # 6. Wait for preview music + cheer to play and complete its baked fade-out
            wait_sec = max(0.2, preview_dur - 0.05)
            with self._lock:
                self._active_state["phase"] = "playing"
            if self._wait_cancelable(cancel_ev, wait_sec):
                return {"ok": False, "canceled": True}

            # 7A. Ryder / Debut 3-screen video handoff at MV70
            if spec.three_screen_video and spec.youtube_url:
                with self._lock:
                    self._active_state["phase"] = "video_screens"
                if spec.lights_flash_count > 0:
                    self._flash_lights(spec.lights_flash_count, cancel_ev=cancel_ev)
                out_res["threeScreen"] = self._launch_three_screen_youtube(spec.youtube_url)
                if self._wait_cancelable(cancel_ev, spec.video_hold_sec):
                    return {"ok": False, "canceled": True}
                # Gently restore Denon down after video hold finishes
                self._restore_denon_down(target_db, prev_db, fast=False)
                denon_restored = True
                if self.displays is not None:
                    try:
                        self.displays.sync_denon_monitor_for_spin()
                    except Exception:
                        pass
            else:
                # 7B. PHASE 2 (ANTI-ROLLER-COASTER EXIT):
                # Audio already faded smoothly to silence and entered the 1400ms silent tail.
                # Cube STILL holds AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK right now!
                # Ramp Denon BACK DOWN to prev_db BEFORE Cube un-ducks background music!
                with self._lock:
                    self._active_state["phase"] = "restoring_denon"
                if denon_raised:
                    self._restore_denon_down(target_db, prev_db, fast=False)
                    denon_restored = True
                self._wait_cancelable(cancel_ev, 0.35)

            return out_res

        except Exception as exc:
            out_res = {"ok": False, "showId": spec.id, "error": str(exc)}
            return out_res
        finally:
            # Emergency safety guarantee: ALWAYS ramp Denon back down FIRST before stopping Cube!
            if denon_raised and not denon_restored and self.house_audio is not None:
                self._restore_denon_down(target_db, prev_db, fast=True)
            if cancel_ev.is_set() and cube_started and self.house_audio is not None:
                try:
                    cfg_ha = self.house_audio.get_config()
                    if cfg_ha.cube_host:
                        self.house_audio.cube.stop(cfg_ha.cube_host, cfg_ha.cube_port)
                except Exception:
                    pass
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_state = {
                        "active": False,
                        "showId": "",
                        "showName": "",
                        "phase": "idle",
                        "startedAt": "",
                        "lastError": "" if out_res.get("ok") else str(out_res.get("error") or ""),
                    }
                    self._last_result = {**out_res, "finishedAt": _utc_iso_now()}

    def fire_show(self, show_id: str, *, async_run: bool = True) -> dict[str, Any]:
        spec = self.get_show(show_id)
        if spec is None:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        if not spec.enabled:
            return {"ok": False, "error": f"Show '{spec.name}' is disabled"}

        self.stop_active_show()
        with self._lock:
            self._active_run_id += 1
            run_id = self._active_run_id
            cancel_ev = threading.Event()
            self._active_cancel = cancel_ev
            self._active_state = {
                "active": True,
                "showId": spec.id,
                "showName": spec.name,
                "phase": "starting",
                "startedAt": _utc_iso_now(),
                "lastError": "",
            }

        if not async_run:
            return self._run_show_sequence(spec, run_id, cancel_ev)

        t = threading.Thread(
            target=self._run_show_sequence,
            args=(spec, run_id, cancel_ev),
            name=f"ArmoryShow-{spec.id}",
            daemon=True,
        )
        with self._lock:
            self._active_thread = t
        t.start()
        return {"ok": True, "started": True, "showId": spec.id, "showName": spec.name}

    def stop_active_show(self, *, wait_timeout: float = 2.0) -> dict[str, Any]:
        with self._lock:
            ev = self._active_cancel
            t = self._active_thread
            was_active = bool(self._active_state.get("active"))
        if ev is not None:
            ev.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=wait_timeout)
        if self.house_audio is not None:
            try:
                cfg_ha = self.house_audio.get_config()
                if cfg_ha.cube_host and was_active:
                    # Denon is already restored in _run_show_sequence finally; ensure Cube is clean
                    self.house_audio.cube.stop(cfg_ha.cube_host, cfg_ha.cube_port)
            except Exception:
                pass
        return {"ok": True, "stopped": was_active}
