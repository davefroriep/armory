"""Armory Walk-In & PR Drop-In Show Director.

Coordinates custom arena-announcer intros, tightly clipped PR music drops,
stadium crowd cheers, thunder FX, anti-roller-coaster Denon AVR + Fire TV Cube
volume choreography, 3-screen synchronized entrance videos, and optional
local HTTP smart-home hooks (lighting flashes & fog machine triggers).
"""

from __future__ import annotations

from collections import deque
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Sequence
import urllib.parse
import urllib.request
import wave

import numpy as np
from scipy.signal import butter, fftconvolve, resample_poly, sosfilt, sosfiltfilt

try:
    from .display_control import _strip_wake_prefix, normalize_youtube_tv_url
    from .house_audio import db_to_denon_mv
except ImportError:
    from display_control import _strip_wake_prefix, normalize_youtube_tv_url
    from house_audio import db_to_denon_mv


SHOW_SR = 44_100
STAGE_VIDEO_SR = 48_000
MAX_CUBE_WAV_BYTES = 11 * 1024 * 1024  # Safe margin under Cube's 12 MB HTTP cap
MAX_SHOW_DENON_VOL = 75                # -5.0 dB hard safety ceiling
MIN_SHOW_DENON_VOL = 30                # -50.0 dB floor
MAX_SONG_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 250 * 1024 * 1024
VALID_SHOW_EXTS = (".mp3", ".wav", ".m4a", ".ogg")
VALID_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm")
VALID_SHOW_KINDS = ("pr_drop", "walkin", "debut_video")
VALID_CUBE_MODES = ("duck", "takeover", "overlay")
VALID_DEMO_STYLES = ("anthem", "boombap", "thunder_only", "hype")
RYDER_ENTRANCE_URL = "https://www.youtube.com/watch?v=05vwKMySzKw"
DEFAULT_ANNOUNCER_VOICE = "kokoro:onyx_fenrir_epic"
DEFAULT_ELEVENLABS_MODEL = "eleven_v3"
DEFAULT_RYDER_VOICE_STYLE = (
    "Electric HBO championship boxing ring announcer in a packed arena. "
    "Booming deep baritone, authentic crowd-hyping energy, crisp dramatic pacing, "
    "exploding with excitement on the athlete's name."
)
DEFAULT_KOKORO_MODELS_DIR = Path(r"S:\ArmoryCam\models\kokoro")
DEFAULT_STUDIO_FX_DIR = Path(r"S:\ArmoryCam\shows\fx")
BANNED_ROBOTIC_VOICES = frozenset({"en-US-ChristopherNeural", "sapi"})
ELEVEN_V3_AUDIO_CUES = frozenset({
    "shouting",
    "excited",
    "dramatic",
    "whispers",
    "whispering",
    "laughs",
    "laughing",
    "sighs",
    "cheering",
    "slows down",
    "rushed",
    "drawn out",
})

EPIC_VOICE_PRESETS: dict[str, dict[str, Any]] = {
    "elevenlabs:JBFqnCBsd6RMkjVDRZzb": {
        "label": "George Studio (ElevenLabs · Warm Deep British)",
        "engine": "elevenlabs",
        "voiceId": "JBFqnCBsd6RMkjVDRZzb",
    },
    "elevenlabs:nPczCjzI2devNBz1zQrb": {
        "label": "Brian Studio (ElevenLabs · Deep Broadcast Announcer)",
        "engine": "elevenlabs",
        "voiceId": "nPczCjzI2devNBz1zQrb",
    },
    "elevenlabs:pqHfZKP75CvOlQylNhV4": {
        "label": "Bill Studio (ElevenLabs · Authoritative Baritone)",
        "engine": "elevenlabs",
        "voiceId": "pqHfZKP75CvOlQylNhV4",
    },
    "elevenlabs:N2lVS1w4EtoT3dr4eOWO": {
        "label": "Callum Studio (ElevenLabs · Gritty Intense)",
        "engine": "elevenlabs",
        "voiceId": "N2lVS1w4EtoT3dr4eOWO",
    },
    "gemini:Fenrir": {
        "label": "Fenrir Expressive (Gemini 2.5 Flash · Dynamic Deep)",
        "engine": "gemini",
        "voiceName": "Fenrir",
    },
    "gemini:Charon": {
        "label": "Charon Deep (Gemini 2.5 Flash · Resonant Informative)",
        "engine": "gemini",
        "voiceName": "Charon",
    },
    "gemini:Alnilam": {
        "label": "Alnilam Strong (Gemini 2.5 Flash · Firm Announcer)",
        "engine": "gemini",
        "voiceName": "Alnilam",
    },
    "gemini:Orus": {
        "label": "Orus Firm (Gemini 2.5 Flash · Crisp Baritone)",
        "engine": "gemini",
        "voiceName": "Orus",
    },
    "openai:onyx": {
        "label": "Onyx Deep (OpenAI gpt-4o-mini-tts)",
        "engine": "openai",
        "voiceName": "onyx",
    },
    "openai:ash": {
        "label": "Ash Bold (OpenAI gpt-4o-mini-tts · Prompt-Directed)",
        "engine": "openai",
        "voiceName": "ash",
    },
    "openai:ballad": {
        "label": "Ballad Dramatic (OpenAI gpt-4o-mini-tts)",
        "engine": "openai",
        "voiceName": "ballad",
    },
    "kokoro:onyx_fenrir_epic": {
        "label": "Onyx + Fenrir (Kokoro Local Offline)",
        "engine": "kokoro",
        "weights": {"am_onyx": 0.55, "am_fenrir": 0.45},
        "speed": 0.95,
        "finalSpeed": 0.88,
    },
    "kokoro:am_onyx": {
        "label": "Onyx Deep (Kokoro Local Offline)",
        "engine": "kokoro",
        "weights": {"am_onyx": 1.0},
        "speed": 0.98,
        "finalSpeed": 0.92,
    },
    "kokoro:onyx_george_hbo": {
        "label": "Onyx + George (Kokoro Local Offline)",
        "engine": "kokoro",
        "weights": {"am_onyx": 0.55, "bm_george": 0.45},
        "speed": 0.95,
        "finalSpeed": 0.88,
    },
    "kokoro:am_fenrir": {
        "label": "Fenrir Baritone (Kokoro Local Offline)",
        "engine": "kokoro",
        "weights": {"am_fenrir": 1.0},
        "speed": 0.98,
        "finalSpeed": 0.92,
    },
    "kokoro:am_adam": {
        "label": "Adam Baritone (Kokoro Local Offline)",
        "engine": "kokoro",
        "weights": {"am_adam": 1.0},
        "speed": 0.98,
        "finalSpeed": 0.92,
    },
    "edge:en-US-BrianMultilingualNeural": {
        "label": "Brian HD Multilingual (Cloud Free Edge)",
        "engine": "edge",
        "voice": "en-US-BrianMultilingualNeural",
        "rate": "-2%",
    },
    "edge:en-US-AndrewMultilingualNeural": {
        "label": "Andrew HD Multilingual (Cloud Free Edge)",
        "engine": "edge",
        "voice": "en-US-AndrewMultilingualNeural",
        "rate": "-2%",
    },
}


def resolve_voice_id(voice: str | None) -> str:
    """Resolve built-in or dynamic cloud voice ID while rejecting legacy robotic voices."""
    raw = str(voice or "").strip()
    if not raw or raw in BANNED_ROBOTIC_VOICES:
        return DEFAULT_ANNOUNCER_VOICE
    if raw in EPIC_VOICE_PRESETS:
        return raw
    if raw.startswith("elevenlabs:") and re.fullmatch(r"elevenlabs:[A-Za-z0-9_-]{6,64}", raw):
        return raw
    if raw.startswith("gemini:") and re.fullmatch(r"gemini:[A-Za-z0-9_-]{2,40}", raw):
        return raw
    if raw.startswith("openai:") and re.fullmatch(r"openai:[A-Za-z0-9_-]{2,40}", raw):
        return raw
    if raw.startswith("edge:") and raw[5:].strip() not in BANNED_ROBOTIC_VOICES:
        return raw
    if raw in ("en-US-BrianMultilingualNeural", "en-US-AndrewMultilingualNeural"):
        return f"edge:{raw}"
    return DEFAULT_ANNOUNCER_VOICE


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


def validate_video_filename(name: str) -> str | None:
    """Validate uploaded or cached entrance video filename safely."""
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    p = Path(raw)
    if p.name != raw or p.suffix.lower() not in VALID_VIDEO_EXTS:
        return None
    if not all(c.isalnum() or c in "._- " for c in raw):
        return None
    return raw


def sanitize_video_upload_filename(raw_name: str, show_id: str) -> str:
    """Validate and namespace an uploaded entrance video into `shows/sources/` (rejects traversal)."""
    raw = str(raw_name or "").strip()
    if not raw or ".." in raw or "/" in raw or "\\" in raw:
        raise ValueError("Invalid video filename (path separators and '..' are not allowed)")
    base = Path(raw).name.strip()
    ext = Path(base).suffix.lower()
    if ext not in VALID_VIDEO_EXTS:
        raise ValueError(f"Unsupported video format ({ext or 'none'}); use .mp4, .mkv, .mov, or .webm")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(base).stem).strip("_-") or "video"
    sid = _safe_show_id(show_id) or "show"
    return f"{sid}_video_{stem[:40]}{ext}"


def _norm_trigger_text(text: str) -> str:
    s = str(text or "").lower()
    s = s.replace("’", "'")
    s = re.sub(r"\bp\.?\s*r\.?(?:'?e?d|\s+d)\b", " prd ", s)
    s = re.sub(r"\bp\.?\s*r\.?\b", " pr ", s)
    s = re.sub(r"\bmatt's\b", "matt is", s)
    s = re.sub(r"\bryder's\b", "ryder is", s)
    s = re.sub(r"\bgabe's\b", "gabe is", s)
    s = re.sub(r"\bjosh's\b", "josh is", s)
    s = re.sub(r"\bjenna's\b", "jenna is", s)
    s = re.sub(r"\bavenleigh's\b", "avenleigh is", s)
    s = re.sub(r"\bjared's\b", "jared is", s)
    s = re.sub(r"\bmark's\b", "mark is", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_optional_wake(text: str, wake_word: str = "armory") -> str:
    norm = _norm_trigger_text(text)
    norm = re.sub(r"^(?:hey|ok|okay)\s+", "", norm).strip()
    return _strip_wake_prefix(norm, wake_word=wake_word)


VALID_FX_ZONES = ("all", "rack", "smith", "alternate")
VALID_THEATER_SCENES = (
    "",
    "walkin",
    "pr",
    "jenna",
    "avenleigh",
    "vip_gold",
    "vip_prism",
    "thunder",
    "fireworks",
    "bench_hype",
)


def _clean_host(host: str) -> str:
    h = str(host or "").strip()
    for pfx in ("http://", "https://"):
        if h.lower().startswith(pfx):
            h = h[len(pfx):]
    return h.rstrip("/")


@dataclass
class SmartFxConfig:
    """Optional local HTTP hooks for dual-zone Shelly lighting (Rack + Smith), Smart Fogger v3.1, and fog triggers."""

    lights_host: str = ""
    lights_off_url: str = ""
    lights_on_url: str = ""
    lights_rack_host: str = ""
    lights_rack_off_url: str = ""
    lights_rack_on_url: str = ""
    lights_smith_host: str = ""
    lights_smith_off_url: str = ""
    lights_smith_on_url: str = ""
    fog_host: str = ""
    fog_trigger_url: str = ""
    fog_burst_sec: float = 4.0
    theater_host: str = ""
    lights_lead_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lightsHost": self.lights_host,
            "lightsOffUrl": self.lights_off_url,
            "lightsOnUrl": self.lights_on_url,
            "lightsRackHost": self.lights_rack_host,
            "lightsRackOffUrl": self.lights_rack_off_url,
            "lightsRackOnUrl": self.lights_rack_on_url,
            "lightsSmithHost": self.lights_smith_host,
            "lightsSmithOffUrl": self.lights_smith_off_url,
            "lightsSmithOnUrl": self.lights_smith_on_url,
            "fogHost": self.fog_host,
            "fogTriggerUrl": self.fog_trigger_url,
            "fogBurstSec": round(float(self.fog_burst_sec), 1),
            "theaterHost": self.theater_host,
            "lightsLeadSec": round(float(self.lights_lead_sec), 2),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SmartFxConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            burst = _clamp(float(raw.get("fogBurstSec", 4.0) or 4.0), 1.0, 60.0)
        except (TypeError, ValueError):
            burst = 4.0
        try:
            lead_raw = raw.get("lightsLeadSec", 0.0)
            lead_sec = _clamp(float(0.0 if lead_raw is None or lead_raw == "" else lead_raw), 0.0, 5.0)
        except (TypeError, ValueError):
            lead_sec = 0.0
        return cls(
            lights_host=str(raw.get("lightsHost", "") or "").strip(),
            lights_off_url=str(raw.get("lightsOffUrl", "") or "").strip(),
            lights_on_url=str(raw.get("lightsOnUrl", "") or "").strip(),
            lights_rack_host=str(raw.get("lightsRackHost", "") or "").strip(),
            lights_rack_off_url=str(raw.get("lightsRackOffUrl", "") or "").strip(),
            lights_rack_on_url=str(raw.get("lightsRackOnUrl", "") or "").strip(),
            lights_smith_host=str(raw.get("lightsSmithHost", "") or "").strip(),
            lights_smith_off_url=str(raw.get("lightsSmithOffUrl", "") or "").strip(),
            lights_smith_on_url=str(raw.get("lightsSmithOnUrl", "") or "").strip(),
            fog_host=str(raw.get("fogHost", "") or "").strip(),
            fog_trigger_url=str(raw.get("fogTriggerUrl", "") or "").strip(),
            fog_burst_sec=burst,
            theater_host=str(raw.get("theaterHost", "") or "").strip(),
            lights_lead_sec=lead_sec,
        )

    @staticmethod
    def _single_light_url(host: str, explicit_on: str, explicit_off: str, on: bool) -> str:
        explicit = explicit_on if on else explicit_off
        if explicit:
            return explicit.strip()
        h = host.strip()
        if not h:
            return ""
        flag = "true" if on else "false"
        return f"http://{h}/rpc/Switch.Set?id=0&on={flag}"

    def resolve_lights_urls(self, on: bool, zone: str = "all") -> list[str]:
        """Resolve HTTP light control URLs for `rack`, `smith`, or `all` (with legacy `lightsHost` fallback)."""
        z = (zone or "all").strip().lower()
        rack_u = self._single_light_url(self.lights_rack_host, self.lights_rack_on_url, self.lights_rack_off_url, on)
        smith_u = self._single_light_url(self.lights_smith_host, self.lights_smith_on_url, self.lights_smith_off_url, on)
        legacy_u = self._single_light_url(self.lights_host, self.lights_on_url, self.lights_off_url, on)
        if z == "rack":
            return [rack_u] if rack_u else ([legacy_u] if (legacy_u and not smith_u) else [])
        if z == "smith":
            return [smith_u] if smith_u else ([legacy_u] if (legacy_u and not rack_u) else [])
        urls: list[str] = []
        for u in (rack_u, smith_u):
            if u and u not in urls:
                urls.append(u)
        if urls:
            return urls
        return [legacy_u] if legacy_u else []

    def resolve_lights_url(self, on: bool) -> str:
        urls = self.resolve_lights_urls(on, "all")
        return urls[0] if urls else ""

    def resolve_fog_url(self, burst_sec: float | None = None) -> str:
        val = float(burst_sec) if (burst_sec is not None and float(burst_sec) > 0.0) else float(self.fog_burst_sec)
        sec_str = f"{_clamp(val, 1.0, 60.0):.1f}".rstrip("0").rstrip(".")
        if self.fog_trigger_url:
            return self.fog_trigger_url.replace("{sec}", sec_str)
        host = _clean_host(self.fog_host)
        if host:
            return f"http://{host}/rpc/Switch.Set?id=0&on=true&toggle_after={sec_str}"
        t_host = _clean_host(self.theater_host)
        if t_host:
            return f"http://{t_host}/api/trigger?sec={sec_str}"
        return ""

    def resolve_theater_scene_url(
        self,
        scene: str,
        *,
        duration_ms: int = 0,
        fog_ms: int | None = None,
    ) -> str:
        host = _clean_host(self.theater_host)
        sc = str(scene or "").strip().lower()
        if not host or not sc or sc not in VALID_THEATER_SCENES:
            return ""
        params = [f"mode={urllib.parse.quote(sc)}"]
        if int(duration_ms or 0) > 0:
            dur = min(300_000, max(1_000, int(duration_ms)))
            params.append(f"ms={dur}")
        if fog_ms is not None:
            fms = max(0, min(60_000, int(fog_ms)))
            params.append(f"fog_ms={fms}")
        return f"http://{host}/api/scene?" + "&".join(params)

    def resolve_theater_stop_url(self) -> str:
        host = _clean_host(self.theater_host)
        if not host:
            return ""
        return f"http://{host}/api/stop"


VALID_FX_CUE_TYPES = ("flash", "lights_off", "lights_on", "fog")
_CUBE_MEDIA_IGNORE_PKGS = frozenset({
    "com.froriep.armorywallboard",
    "com.froriep.armoryaudio",
    "com.android.bluetooth",
    "com.amazon.vizzini",
})


def _normalize_speech_cues(raw_list: Any) -> list[dict[str, Any]]:
    """Normalize ordered timed speech cues (`target`: 'intro'|'video', `atSec`, `text`, optional `voice`/`postDenonVol`)."""
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        tgt_raw = str(item.get("target") or "video").strip().lower()
        target = "intro" if tgt_raw == "intro" else "video"
        try:
            at_raw = item.get("atSec", 0.0)
            at_sec = round(_clamp(float(0.0 if at_raw is None or at_raw == "" else at_raw), 0.0, 300.0), 2)
        except (TypeError, ValueError):
            at_sec = 0.0
        text = str(item.get("text") or "").strip()
        voice_raw = str(item.get("voice") or "").strip()
        voice = resolve_voice_id(voice_raw) if voice_raw else ""
        try:
            pv_raw = item.get("postDenonVol", 0)
            pv = int(round(float(0 if pv_raw is None or pv_raw == "" else pv_raw)))
        except (TypeError, ValueError):
            pv = 0
        if pv != 0:
            pv = max(MIN_SHOW_DENON_VOL, min(MAX_SHOW_DENON_VOL, pv))
        try:
            dg_raw = item.get("duckGain", 0.22)
            dg = round(_clamp(float(0.22 if dg_raw is None or dg_raw == "" else dg_raw), 0.05, 0.85), 2)
        except (TypeError, ValueError):
            dg = 0.22
        raw_id = re.sub(r"[^a-zA-Z0-9_-]+", "", str(item.get("id") or ""))
        cid = raw_id or f"sc_{idx + 1}"
        if cid in seen_ids:
            cid = f"{cid}_{idx + 1}"
        seen_ids.add(cid)
        out.append({
            "id": cid,
            "target": target,
            "atSec": at_sec,
            "text": text,
            "voice": voice,
            "postDenonVol": pv,
            "duckGain": dg,
        })
    return out


def _normalize_fx_cues(raw_list: Any) -> list[dict[str, Any]]:
    """Normalize ordered timed lighting/fog FX cues (`flash`, `lights_off`, `lights_on`, `fog`)."""
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        tgt_raw = str(item.get("target") or "video").strip().lower()
        target = "intro" if tgt_raw == "intro" else "video"
        fx_type = str(item.get("type") or "flash").strip().lower()
        if fx_type not in VALID_FX_CUE_TYPES:
            fx_type = "flash"
        try:
            at_raw = item.get("atSec", 0.0)
            at_sec = round(_clamp(float(0.0 if at_raw is None or at_raw == "" else at_raw), 0.0, 300.0), 2)
        except (TypeError, ValueError):
            at_sec = 0.0
        try:
            cnt_raw = item.get("count", 1)
            cnt = max(1, min(6, int(round(float(1 if cnt_raw is None or cnt_raw == "" else cnt_raw)))))
        except (TypeError, ValueError):
            cnt = 1
        default_dur = 4.0 if fx_type == "fog" else 0.2
        try:
            dur_raw = item.get("durationSec", default_dur)
            dur = round(_clamp(float(default_dur if dur_raw is None or dur_raw == "" else dur_raw), 0.1, 60.0), 2)
        except (TypeError, ValueError):
            dur = default_dur
        raw_id = re.sub(r"[^a-zA-Z0-9_-]+", "", str(item.get("id") or ""))
        cid = raw_id or f"fx_{idx + 1}"
        if cid in seen_ids:
            cid = f"{cid}_{idx + 1}"
        seen_ids.add(cid)
        zone_raw = str(item.get("zone") or "all").strip().lower()
        zone = zone_raw if zone_raw in VALID_FX_ZONES else "all"
        try:
            off_raw = item.get("offSec", 0.22)
            off_sec = round(_clamp(float(0.22 if off_raw is None or off_raw == "" else off_raw), 0.1, 5.0), 2)
        except (TypeError, ValueError):
            off_sec = 0.22
        out.append({
            "id": cid,
            "target": target,
            "atSec": at_sec,
            "type": fx_type,
            "count": cnt,
            "durationSec": dur,
            "offSec": off_sec,
            "endOn": bool(item.get("endOn", True)),
            "lightFlash": bool(item.get("lightFlash", True)),
            "screenFlash": bool(item.get("screenFlash", True)),
            "zone": zone,
        })
    return out


def parse_cube_media_snapshot(raw_output: str) -> dict[str, Any]:
    """Parse combined `dumpsys media_session` + window focus output from Fire TV Cube."""
    raw = str(raw_output or "")
    if "---ARMORY_WIN---" in raw:
        media_part, win_part = raw.split("---ARMORY_WIN---", 1)
    else:
        media_part, win_part = raw, raw

    sessions: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in media_part.splitlines():
        if re.match(r"^\s{4}\S+\s+[A-Za-z0-9_.]+/\S+", line):
            cur = {"package": "", "active": False, "state": None}
            sessions.append(cur)
            continue
        m_pkg = re.search(r"\bpackage=([A-Za-z0-9_.]+)", line)
        if m_pkg:
            pkg = m_pkg.group(1).strip()
            if cur is None or cur["package"]:
                cur = {"package": pkg, "active": False, "state": None}
                sessions.append(cur)
            else:
                cur["package"] = pkg
            continue
        if cur is None:
            continue
        m_act = re.search(r"\bactive=(true|false)\b", line, re.I)
        if m_act:
            cur["active"] = m_act.group(1).lower() == "true"
        m_st = re.search(r"PlaybackState\s*\{[^}]*\bstate=(\d+)", line)
        if m_st:
            cur["state"] = int(m_st.group(1))

    non_ignored = [s for s in sessions if s.get("package") and s["package"] not in _CUBE_MEDIA_IGNORE_PKGS]
    was_playing = False
    media_pkg = ""
    media_state = 0
    for s in non_ignored:
        st_val = s.get("state")
        if s.get("active") and st_val in (3, 6):
            was_playing = True
            media_pkg = str(s["package"])
            media_state = int(st_val)
            break
    if not was_playing and non_ignored:
        first_s = non_ignored[0]
        media_pkg = str(first_s.get("package") or "")
        media_state = 2 if first_s.get("state") == 2 else 0

    focused_component = ""
    focused_package = ""
    focus_lines = [ln for ln in win_part.splitlines() if "mCurrentFocus" in ln or "mFocusedApp" in ln]
    if not focus_lines:
        focus_lines = win_part.splitlines()
    for ln in focus_lines:
        for m_cmp in re.finditer(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", ln):
            comp = m_cmp.group(1)
            pkg = comp.split("/", 1)[0]
            if pkg in _CUBE_MEDIA_IGNORE_PKGS:
                continue
            focused_component = comp
            focused_package = pkg
            break
        if focused_component:
            break

    return {
        "wasPlaying": was_playing,
        "mediaPackage": media_pkg,
        "mediaState": media_state,
        "focusedComponent": focused_component,
        "focusedPackage": focused_package,
    }


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
    announcer_pitch: str = ""
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
    video_source_file: str = ""
    three_screen_video: bool = False
    video_hold_sec: float = 45.0
    is_dev_drop: bool = False
    fog_burst_sec: float = 0.0
    lightning_flash_sec: float = 0.0
    lights_pulse_duration_sec: float = 0.0
    lights_pulse_interval_sec: float = 2.0
    lights_pulse_on_sec: float = 0.25
    overlay_at_sec: float = 0.0
    overlay_text: str = ""
    post_overlay_denon_vol: int = 0
    voice_reverb_wet: float = 0.06
    voice_boost_db: float = 2.0
    voice_style: str = ""
    custom_intro_voice_file: str = ""
    custom_overlay_voice_file: str = ""
    intro_start_sec: float = 2.3
    exclude_main_tv: bool = False
    theater_scene: str = ""
    dj_seamless_intro: bool = False
    dj_drop_sec: float = 0.0
    speech_cues: list[dict[str, Any]] = field(default_factory=list)
    fx_cues: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.speech_cues and self.overlay_text and float(self.overlay_at_sec or 0.0) > 0.0:
            self.speech_cues = [
                {
                    "id": "sc_1",
                    "target": "video",
                    "atSec": round(float(self.overlay_at_sec), 2),
                    "text": str(self.overlay_text).strip(),
                    "voice": "",
                    "postDenonVol": int(self.post_overlay_denon_vol or 0),
                    "duckGain": 0.22,
                }
            ]

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
            "announcerVoice": resolve_voice_id(self.announcer_voice),
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
            "videoSourceFile": self.video_source_file,
            "threeScreenVideo": bool(self.three_screen_video),
            "videoHoldSec": round(float(self.video_hold_sec), 1),
            "isDevDrop": bool(self.is_dev_drop),
            "fogBurstSec": round(float(self.fog_burst_sec), 1),
            "lightningFlashSec": round(float(self.lightning_flash_sec), 2),
            "lightsPulseDurationSec": round(float(self.lights_pulse_duration_sec), 1),
            "lightsPulseIntervalSec": round(float(self.lights_pulse_interval_sec), 2),
            "lightsPulseOnSec": round(float(self.lights_pulse_on_sec), 2),
            "overlayAtSec": round(float(self.overlay_at_sec), 1),
            "overlayText": self.overlay_text,
            "postOverlayDenonVol": int(self.post_overlay_denon_vol),
            "voiceReverbWet": round(float(self.voice_reverb_wet), 2),
            "voiceBoostDb": round(float(self.voice_boost_db), 1),
            "voiceStyle": self.voice_style,
            "customIntroVoiceFile": self.custom_intro_voice_file,
            "customOverlayVoiceFile": self.custom_overlay_voice_file,
            "introStartSec": round(float(self.intro_start_sec), 2),
            "excludeMainTv": bool(self.exclude_main_tv),
            "theaterScene": self.theater_scene,
            "djSeamlessIntro": bool(self.dj_seamless_intro),
            "djDropSec": round(float(self.dj_drop_sec), 2),
            "speechCues": list(self.speech_cues),
            "fxCues": list(self.fx_cues),
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
        vid_valid = validate_video_filename(str(raw.get("videoSourceFile", "") or "")) or ""
        ci_valid = validate_song_filename(str(raw.get("customIntroVoiceFile", "") or "")) or ""
        co_valid = validate_song_filename(str(raw.get("customOverlayVoiceFile", "") or "")) or ""
        ov_at = _clamp(_f("overlayAtSec", 0.0), 0.0, 300.0)
        ov_txt = str(raw.get("overlayText", "") or "").strip()
        post_vol = _i("postOverlayDenonVol", 0)
        if post_vol != 0:
            post_vol = max(MIN_SHOW_DENON_VOL, min(MAX_SHOW_DENON_VOL, post_vol))

        if "speechCues" in raw:
            speech_cues = _normalize_speech_cues(raw.get("speechCues"))
            first_vid = next(
                (c for c in speech_cues if c.get("target") == "video" and c.get("text")),
                None,
            )
            if first_vid is not None:
                ov_txt = str(first_vid["text"])
                ov_at = float(first_vid["atSec"])
                post_vol = int(first_vid.get("postDenonVol") or 0)
            else:
                ov_txt = ""
                ov_at = 0.0
                post_vol = 0
        else:
            speech_cues = []
            if ov_txt and ov_at > 0.0:
                speech_cues = [
                    {
                        "id": "sc_1",
                        "target": "video",
                        "atSec": round(ov_at, 2),
                        "text": ov_txt,
                        "voice": "",
                        "postDenonVol": post_vol,
                        "duckGain": 0.22,
                    }
                ]

        fx_cues = _normalize_fx_cues(raw.get("fxCues") or [])
        t_scene = str(raw.get("theaterScene", "") or "").strip().lower()
        if t_scene not in VALID_THEATER_SCENES:
            t_scene = ""

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
            announcer_voice=resolve_voice_id(raw.get("announcerVoice")),
            announcer_rate=str(raw.get("announcerRate", "-6%") or "-6%").strip(),
            announcer_pitch="",  # Never inject warbly SSML pitch shifts
            demo_style=demo_style,
            demo_shout=str(raw.get("demoShout", "") or "").strip(),
            source_file=src_valid,
            song_start_sec=max(0.0, _f("songStartSec", 0.0)),
            song_duration_sec=_clamp(_f("songDurationSec", 15.0), 0.0, 65.0),
            fade_out_sec=_clamp(_f("fadeOutSec", 2.5), 0.3, 10.0),
            trim_leading_silence=bool(raw.get("trimLeadingSilence", True)),
            add_cheer=bool(raw.get("addCheer", True)),
            cheer_delay_sec=max(0.0, _f("cheerDelaySec", 9.5)),
            add_thunder=bool(raw.get("addThunder", False)),
            lights_out=bool(raw.get("lightsOut", False)),
            lights_flash_count=max(0, min(6, _i("lightsFlashCount", 0))),
            trigger_fog=bool(raw.get("triggerFog", False)),
            youtube_url=str(raw.get("youtubeUrl", "") or "").strip(),
            video_source_file=vid_valid,
            three_screen_video=bool(raw.get("threeScreenVideo", False)),
            video_hold_sec=_clamp(_f("videoHoldSec", 45.0), 5.0, 300.0),
            is_dev_drop=bool(raw.get("isDevDrop", sid == "ryder_walkin")),
            fog_burst_sec=_clamp(_f("fogBurstSec", 0.0), 0.0, 60.0),
            lightning_flash_sec=_clamp(_f("lightningFlashSec", 0.0), 0.0, 15.0),
            lights_pulse_duration_sec=_clamp(_f("lightsPulseDurationSec", 0.0), 0.0, 120.0),
            lights_pulse_interval_sec=_clamp(_f("lightsPulseIntervalSec", 2.0), 0.5, 15.0),
            lights_pulse_on_sec=_clamp(_f("lightsPulseOnSec", 0.25), 0.1, 2.0),
            overlay_at_sec=ov_at,
            overlay_text=ov_txt,
            post_overlay_denon_vol=post_vol,
            voice_reverb_wet=_clamp(_f("voiceReverbWet", 0.06), 0.0, 0.30),
            voice_boost_db=_clamp(_f("voiceBoostDb", 2.0), -4.0, 6.0),
            voice_style=str(raw.get("voiceStyle", "") or "").strip(),
            custom_intro_voice_file=ci_valid,
            custom_overlay_voice_file=co_valid,
            intro_start_sec=_clamp(_f("introStartSec", 2.3), 0.0, 15.0),
            exclude_main_tv=bool(raw.get("excludeMainTv", False)),
            theater_scene=t_scene,
            dj_seamless_intro=bool(raw.get("djSeamlessIntro", False)),
            dj_drop_sec=_clamp(_f("djDropSec", 0.0), 0.0, 20.0),
            speech_cues=speech_cues,
            fx_cues=fx_cues,
        )


def default_shows() -> list[ShowSpec]:
    """Return built-in shows requested by Dave (PR drops, walk-ins, debut templates)."""
    return [
        ShowSpec(
            id="pr",
            name="PR",
            kind="pr_drop",
            enabled=True,
            triggers=[
                "i prd",
                "just prd",
                "hit a pr",
                "new pr",
                "i just hit a pr",
                "personal record",
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
            theater_scene="fireworks",
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
                "[excited] Straight out of Brick, New Jersey! "
                "The Plow Bolt King of Toms River! [shouting] MATTHEW COOKSON!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-4%",
            announcer_pitch="",
            demo_style="boombap",
            song_start_sec=0.0,
            song_duration_sec=21.47,
            fade_out_sec=2.2,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            voice_reverb_wet=0.11,
            voice_boost_db=2.0,
            theater_scene="walkin",
        ),
        ShowSpec(
            id="ryder_walkin",
            name="DEV DROP · Ryder Curry Debut (Thunder + 30s Fog + 3 Screens + 1:20 Overlay)",
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
                "Ladies and gentlemen! ... "
                "Out of Toms River, New Jersey! ... "
                "Making his Armory debut... Ryder, Curry!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-6%",
            announcer_pitch="",
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
            video_hold_sec=218.0,
            is_dev_drop=True,
            fog_burst_sec=30.0,
            lightning_flash_sec=0.75,
            lights_pulse_duration_sec=20.0,
            lights_pulse_interval_sec=2.0,
            lights_pulse_on_sec=0.25,
            overlay_at_sec=80.0,
            overlay_text="Ryder Curry... welcome to The Armory. Remember... strength is never a weakness.",
            post_overlay_denon_vol=55,
            voice_reverb_wet=0.06,
            voice_boost_db=2.0,
            voice_style=DEFAULT_RYDER_VOICE_STYLE,
            theater_scene="thunder",
        ),
        ShowSpec(
            id="jenna_walkin",
            name="Jenna Walk-In · Rockstar Headliner (Missy Elliott — Lose Control)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "jenna is here",
                "jenna just got here",
                "jenna walk in",
                "jenna entrance",
                "rockstar walk in",
            ],
            denon_vol=70,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text=(
                "[excited] Clear the floor! Headlining tonight at The Armory! [shouting] JENNA!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-4%",
            announcer_pitch="",
            demo_style="hype",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=2.0,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            add_thunder=False,
            lights_out=True,
            lights_flash_count=1,
            trigger_fog=True,
            fog_burst_sec=3.5,
            lightning_flash_sec=0.0,
            intro_start_sec=0.15,
            youtube_url="https://www.youtube.com/watch?v=aOt4xRE05wQ",
            video_source_file="jenna_walkin_video.mp4",
            three_screen_video=True,
            video_hold_sec=28.72,
            voice_reverb_wet=0.10,
            voice_boost_db=2.0,
            voice_style=(
                "Charismatic MTV VMA and Super Bowl Halftime live concert host. "
                "Electric, stylish, rockstar energy, exploding with excitement on JENNA."
            ),
            theater_scene="jenna",
            dj_seamless_intro=True,
            dj_drop_sec=5.67,
        ),
        ShowSpec(
            id="avenleigh_walkin",
            name="Avenleigh Walk-In · Pink VIP Stage (J.Lo ft. Pitbull — On The Floor)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "avenleigh is here",
                "avenleigh just got here",
                "avenleigh walk in",
                "avenleigh entrance",
                "avenly is here",
                "avenly just got here",
                "avenly walk in",
                "avenlee is here",
                "avenlee walk in",
                "tavenly is here",
                "tavenly just got here",
                "tavenly walk in",
                "avonie is here",
                "avonie just got here",
                "avonie walk in",
                "avenie is here",
                "havinly is here",
                "evanly is here",
                "heavenly is here",
                "aveline is here",
                "avaly is here",
                "avalee is here",
                "avenny is here",
            ],
            denon_vol=68,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text=(
                "[excited] Hands up! Live on the VIP stage, the star of the show! [shouting] Avinlee!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-4%",
            announcer_pitch="",
            demo_style="hype",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=1.8,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            add_thunder=False,
            lights_out=True,
            lights_flash_count=1,
            trigger_fog=True,
            fog_burst_sec=3.5,
            lightning_flash_sec=0.0,
            intro_start_sec=0.15,
            youtube_url="https://www.youtube.com/shorts/ET3HB1xzx14",
            video_source_file="avenleigh_walkin_video.mp4",
            three_screen_video=True,
            video_hold_sec=37.07,
            voice_reverb_wet=0.09,
            voice_boost_db=2.0,
            voice_style=(
                "Warm, joyful, sparkling pop-concert arena MC welcoming a young superstar "
                "onto a bright pink VIP dance stage. Upbeat, enthusiastic, fun."
            ),
            theater_scene="avenleigh",
            dj_seamless_intro=True,
            dj_drop_sec=7.34,
        ),
        ShowSpec(
            id="jared_mark_walkin",
            name="Jared & Mark Walk-In · Modern Prism VIP (Whitney Houston)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "jared and mark are here",
                "jared and mark just got here",
                "jared and mark walk in",
                "mark and jared are here",
                "mark and jared just got here",
                "mark and jared walk in",
                "jared and mark prism",
            ],
            denon_vol=70,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text=(
                "[excited] Lights down, volume up! Your guests of honor at The Armory! [shouting] JARED and MARK!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-4%",
            announcer_pitch="",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=2.2,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            add_thunder=False,
            lights_out=True,
            lights_flash_count=1,
            trigger_fog=True,
            fog_burst_sec=3.5,
            lightning_flash_sec=0.0,
            intro_start_sec=0.15,
            youtube_url="https://www.youtube.com/watch?v=id2-K3daNRQ",
            video_source_file="jared_mark_prism_video.mp4",
            three_screen_video=True,
            video_hold_sec=42.89,
            voice_reverb_wet=0.10,
            voice_boost_db=2.0,
            voice_style=(
                "Sleek, classy, celebratory prime-time awards-show host. "
                "Warm, charismatic, high-energy welcome without sports-announcer cliches."
            ),
            theater_scene="vip_prism",
            dj_seamless_intro=True,
            dj_drop_sec=6.80,
        ),
        ShowSpec(
            id="jared_mark_vip",
            name="Jared & Mark Walk-In · Velvet Rope VIP (Whitney Houston)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "jared and mark vip",
                "jared and mark gold",
                "mark and jared vip",
                "velvet rope walk in",
            ],
            denon_vol=70,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text=(
                "[excited] Roll out the red carpet! Welcome to The Armory, [shouting] JARED and MARK!!"
            ),
            announcer_voice=DEFAULT_ANNOUNCER_VOICE,
            announcer_rate="-4%",
            announcer_pitch="",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=2.2,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            add_thunder=False,
            lights_out=True,
            lights_flash_count=1,
            trigger_fog=True,
            fog_burst_sec=3.5,
            lightning_flash_sec=0.0,
            intro_start_sec=0.15,
            youtube_url="https://www.youtube.com/watch?v=id2-K3daNRQ",
            video_source_file="jared_mark_vip_video.mp4",
            three_screen_video=True,
            video_hold_sec=42.89,
            voice_reverb_wet=0.10,
            voice_boost_db=2.0,
            voice_style=(
                "Glamorous red-carpet gala and VIP lounge host. "
                "Smooth, upbeat, genuinely warm and celebratory."
            ),
            theater_scene="vip_gold",
            dj_seamless_intro=True,
            dj_drop_sec=6.80,
        ),
        ShowSpec(
            id="welcome_armory",
            name="Welcome to the Armory (Storm Intro + Simply the Best)",
            kind="debut_video",
            enabled=True,
            triggers=[
                "we have guests",
                "we got guests",
                "guests are here",
                "welcome to the armory",
                "welcome our guests",
                "show them the armory",
            ],
            denon_vol=70,
            cube_mode="takeover",
            overlay_gain=1.0,
            announcer_text="[dramatic] Welcome... to The Armory!",
            announcer_voice="elevenlabs:nPczCjzI2devNBz1zQrb",
            announcer_rate="-8%",
            announcer_pitch="",
            demo_style="anthem",
            song_start_sec=0.0,
            song_duration_sec=0.0,
            fade_out_sec=2.4,
            trim_leading_silence=True,
            add_cheer=True,
            cheer_delay_sec=0.0,
            add_thunder=True,
            lights_out=True,
            lights_flash_count=0,
            trigger_fog=True,
            fog_burst_sec=5.5,
            lightning_flash_sec=0.0,
            intro_start_sec=0.15,
            video_source_file="welcome_armory_video.mp4",
            three_screen_video=True,
            video_hold_sec=26.5,
            voice_reverb_wet=0.11,
            voice_boost_db=0.0,
            voice_style=(
                "Deep, commanding, cinematic Atlas-style baritone welcoming VIP guests "
                "into a storm-lit private strength arena. Confident, booming, resonant authority."
            ),
            theater_scene="thunder",
            dj_seamless_intro=True,
            dj_drop_sec=8.00,
            fx_cues=[
                {"id": "wa_bolt1", "target": "intro", "atSec": 0.75, "type": "flash", "count": 1, "durationSec": 0.22, "offSec": 0.24, "endOn": False, "lightFlash": True, "screenFlash": False, "zone": "all"},
                {"id": "wa_bolt2", "target": "intro", "atSec": 2.10, "type": "flash", "count": 1, "durationSec": 0.26, "offSec": 0.24, "endOn": False, "lightFlash": True, "screenFlash": False, "zone": "all"},
                {"id": "wa_bolt3", "target": "intro", "atSec": 3.80, "type": "flash", "count": 1, "durationSec": 0.22, "offSec": 0.24, "endOn": False, "lightFlash": True, "screenFlash": False, "zone": "all"},
                {"id": "wa_pre1", "target": "intro", "atSec": 6.70, "type": "flash", "count": 1, "durationSec": 0.20, "offSec": 0.22, "endOn": False, "lightFlash": True, "screenFlash": False, "zone": "all"},
                {"id": "wa_pre2", "target": "intro", "atSec": 7.35, "type": "flash", "count": 1, "durationSec": 0.20, "offSec": 0.22, "endOn": False, "lightFlash": True, "screenFlash": False, "zone": "all"},
                {"id": "wa_drop_on", "target": "video", "atSec": 0.00, "type": "flash", "count": 1, "durationSec": 0.24, "offSec": 0.22, "endOn": True, "lightFlash": False, "screenFlash": True, "zone": "all"},
            ],
        ),
        ShowSpec(
            id="gabe_walkin",
            name="Gabe Walk-In (Template)",
            kind="walkin",
            enabled=False,
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
            enabled=False,
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


def load_studio_fx_stereo(
    fx_dir: Path | None,
    filename: str,
    dur_sec: float,
    *,
    allow_default_fallback: bool = True,
    swell_sec: float = 0.25,
    fade_out_sec: float = 1.2,
    sr: int = SHOW_SR,
) -> np.ndarray | None:
    """Load real studio CC0 WAV from `shows/fx/`, trimming/crossfade-looping to exact duration."""
    candidates: list[Path] = []
    if fx_dir is not None:
        candidates.append(Path(fx_dir) / filename)
    if allow_default_fallback and (fx_dir is None or Path(fx_dir) != DEFAULT_STUDIO_FX_DIR):
        candidates.append(DEFAULT_STUDIO_FX_DIR / filename)
    src_path = next((p for p in candidates if p.is_file() and p.stat().st_size > 200), None)
    if src_path is None:
        return None
    pcm = read_wav_stereo_float(src_path, target_sr=sr)
    if pcm.shape[0] < 16:
        return None

    target_n = max(16, int(round(max(0.4, dur_sec) * sr)))
    if pcm.shape[0] < target_n:
        # Smooth 200ms crossfade loop if clip is shorter than requested bed
        xf_n = min(pcm.shape[0] // 4, int(round(0.20 * sr)))
        out = np.zeros((target_n, 2), dtype=np.float32)
        pos = 0
        while pos < target_n:
            rem = target_n - pos
            take = min(pcm.shape[0], rem)
            seg = pcm[:take].copy()
            if pos > 0 and xf_n > 8 and take > xf_n:
                seg[:xf_n] *= np.linspace(0.0, 1.0, xf_n, dtype=np.float32)[:, None]
                out[pos : pos + xf_n] *= np.linspace(1.0, 0.0, xf_n, dtype=np.float32)[:, None]
            out[pos : pos + take] += seg
            step = max(16, take - xf_n)
            pos += step
        pcm = out
    else:
        pcm = pcm[:target_n].copy()

    atk = min(pcm.shape[0] // 3, max(0, int(round(swell_sec * sr))))
    if atk > 12:
        in_env = np.sin(0.5 * np.pi * np.linspace(0.0, 1.0, atk, dtype=np.float32))
        pcm[:atk] *= in_env[:, None]
    return apply_fade_out_stereo(pcm, sr=sr, fade_out_sec=min(fade_out_sec, max(0.2, float(pcm.shape[0]) / float(sr) * 0.5)))


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


def synth_thunder_rumble_stereo(
    dur_sec: float = 4.2,
    sr: int = SHOW_SR,
    *,
    crack_at_sec: float = 0.75,
) -> np.ndarray:
    """Deep stadium sub-bass rolling thunder + timed lightning crack transient."""
    n = max(1, int(round(max(1.0, dur_sec) * sr)))
    rng = np.random.default_rng(7731)
    nl = rng.standard_normal(n).astype(np.float32)
    nr = rng.standard_normal(n).astype(np.float32)
    t = np.linspace(0.0, float(n) / sr, n, endpoint=False, dtype=np.float32)

    sos_sub = butter(3, [32.0, 145.0], btype="bandpass", fs=sr, output="sos")
    sos_crack = butter(2, [420.0, 2600.0], btype="bandpass", fs=sr, output="sos")

    c0 = max(0.05, float(crack_at_sec))
    env_rumble = (
        np.exp(-((t - c0) / 0.75) ** 2)
        + 0.72 * np.exp(-((t - (c0 + 1.4)) / 1.05) ** 2)
        + 0.35 * np.exp(-t / max(0.8, dur_sec * 0.45))
    )
    crack_env = np.exp(-np.maximum(0.0, t - c0) / 0.14) * (t >= c0)

    l = (sosfilt(sos_sub, nl) * env_rumble * 1.35 + sosfilt(sos_crack, nl) * crack_env * 0.42).astype(np.float32)
    r = (sosfilt(sos_sub, nr) * env_rumble * 1.35 + sosfilt(sos_crack, nr) * crack_env * 0.42).astype(np.float32)
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


# ---------------- Studio Kokoro Neural TTS & Mastering ----------------

_KOKORO_LOCK = threading.Lock()
_KOKORO_CACHE: dict[tuple[str, str], Any] = {}


def _resolve_kokoro_paths(models_dir: Path | None, *, allow_fallback: bool) -> tuple[Path, Path] | None:
    candidates: list[Path] = []
    if models_dir is not None:
        candidates.append(Path(models_dir))
    if allow_fallback and DEFAULT_KOKORO_MODELS_DIR not in candidates:
        candidates.append(DEFAULT_KOKORO_MODELS_DIR)
    for d in candidates:
        onnx_p = d / "kokoro-v1.0.onnx"
        voices_p = d / "voices-v1.0.bin"
        if onnx_p.is_file() and voices_p.is_file():
            return onnx_p, voices_p
    return None


def _get_kokoro(models_dir: Path | None, *, allow_fallback: bool = True) -> Any | None:
    paths = _resolve_kokoro_paths(models_dir, allow_fallback=allow_fallback)
    if paths is None:
        return None
    try:
        try:
            from .room_voice import get_shared_kokoro
        except ImportError:
            from room_voice import get_shared_kokoro
        kokoro, _dev = get_shared_kokoro(paths[0].parent)
        return kokoro
    except Exception:
        pass
    key = (str(paths[0].resolve()), str(paths[1].resolve()))
    with _KOKORO_LOCK:
        inst = _KOKORO_CACHE.get(key)
        if inst is not None:
            return inst
        from kokoro_onnx import Kokoro
        inst = Kokoro(str(paths[0]), str(paths[1]))
        _KOKORO_CACHE[key] = inst
        return inst


def kokoro_models_available(models_dir: Path | None = None) -> bool:
    return _resolve_kokoro_paths(models_dir, allow_fallback=True) is not None


def _split_announcer_beats(text: str) -> list[str]:
    """Split script on '...' or '|' so ring-announcer pauses land cleanly."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    parts = [p.strip() for p in re.split(r"\s*(?:\.{3,}|…|\|)\s*", normalized) if p.strip()]
    return parts or [normalized]


_INLINE_MARKUP_RE = re.compile(
    r"<\s*break\b[^>]*\btime\s*=\s*['\"]?\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s)\s*['\"]?[^>]*/?\s*>"
    r"|\[([^\[\]]+)\]",
    re.I,
)


def parse_speech_markup(
    text: str,
    *,
    default_speed: float = 1.0,
    keep_eleven_v3_tags: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Parse inline pause, tempo/speed, volume, style, and expressive acting tags into ordered tokens."""
    raw = str(text or "").strip()
    if not raw:
        return [], ""

    base_spd = _clamp(float(default_speed or 1.0), 0.65, 1.50)
    speed_stack: list[float] = [base_spd]
    vol_stack: list[float] = [0.0]
    style_parts: list[str] = []
    tokens: list[dict[str, Any]] = []

    def _emit_speech(chunk: str, spd: float, gdb: float) -> None:
        clean = re.sub(r"\s+", " ", chunk).strip()
        if not clean:
            return
        # Keep existing `...` / `…` / `|` beat splitting inside literal speech runs
        parts = [p.strip() for p in re.split(r"\s*(?:\.{3,}|…|\|)\s*", clean) if p.strip()]
        if not parts:
            return
        for idx, sub in enumerate(parts):
            tokens.append({"kind": "speech", "text": sub, "speed": round(spd, 4), "gainDb": round(gdb, 2)})
            if idx < len(parts) - 1:
                tokens.append({"kind": "pause", "sec": 0.48 if (idx + 1 == len(parts) - 1 and len(parts) > 1) else 0.38, "implicit": True})

    def _emit_pause(sec: float) -> None:
        sec_c = round(_clamp(float(sec), 0.02, 10.0), 3)
        if tokens and tokens[-1]["kind"] == "pause":
            # Explicit pause replaces adjacent implicit pause; coalesce explicit consecutive pauses cleanly
            if tokens[-1].get("implicit"):
                tokens[-1] = {"kind": "pause", "sec": sec_c}
            else:
                tokens[-1]["sec"] = round(_clamp(float(tokens[-1]["sec"]) + sec_c, 0.02, 10.0), 3)
        else:
            tokens.append({"kind": "pause", "sec": sec_c})

    def _parse_speed_val(val: str) -> float | None:
        s = val.strip().lower()
        if s in ("normal", "reset", "default", "1x"):
            return base_spd
        m_rel = re.fullmatch(r"([+-]\d+(?:\.\d+)?)\s*%", s)
        if m_rel:
            return _clamp(base_spd * (1.0 + float(m_rel.group(1)) / 100.0), 0.65, 1.50)
        m_pct = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", s)
        if m_pct:
            return _clamp(base_spd * (float(m_pct.group(1)) / 100.0), 0.65, 1.50)
        m_num = re.fullmatch(r"(\d+(?:\.\d+)?)\s*x?", s)
        if m_num:
            return _clamp(float(m_num.group(1)), 0.65, 1.50)
        return None

    def _parse_vol_val(val: str) -> float | None:
        s = val.strip().lower()
        if s in ("normal", "reset", "default"):
            return 0.0
        m_db = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*(?:db)?", s)
        if m_db:
            return _clamp(float(m_db.group(1)), -12.0, 8.0)
        return None

    buf: list[str] = []
    pos = 0
    for m in _INLINE_MARKUP_RE.finditer(raw):
        if m.start() > pos:
            buf.append(raw[pos : m.start()])
        pos = m.end()

        if m.group(1) is not None:
            _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
            buf.clear()
            num = float(m.group(1))
            unit = m.group(2).lower()
            _emit_pause(num / 1000.0 if unit == "ms" else num)
            continue

        tag = (m.group(3) or "").strip()
        low = re.sub(r"\s+", " ", tag).lower()

        # Closing tags [/speed], [/tempo], [/rate], [/vol], [/loud]
        if low in ("/speed", "/tempo", "/rate"):
            _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
            buf.clear()
            if len(speed_stack) > 1:
                speed_stack.pop()
            continue
        if low in ("/vol", "/volume", "/loud"):
            _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
            buf.clear()
            if len(vol_stack) > 1:
                vol_stack.pop()
            continue

        # Pause [pause:100ms], [pause:0.5s], [p:250ms]
        mp = re.fullmatch(r"(?:pause|p)\s*[:=\s]\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s)?", low)
        if mp:
            _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
            buf.clear()
            num = float(mp.group(1))
            unit = (mp.group(2) or ("ms" if num >= 20.0 else "s")).lower()
            _emit_pause(num / 1000.0 if unit == "ms" else num)
            continue

        # Speed / tempo / rate [speed:+10%], [tempo:1.15], [speed:normal]
        ms = re.fullmatch(r"(?:speed|tempo|rate)\s*[:=\s]\s*(.+)", low)
        if ms:
            parsed_spd = _parse_speed_val(ms.group(1))
            if parsed_spd is not None:
                _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
                buf.clear()
                if ms.group(1).strip() in ("normal", "reset", "default"):
                    speed_stack = [base_spd]
                else:
                    speed_stack.append(parsed_spd)
                continue

        # Volume [vol:+3dB], [loud]
        if low == "loud":
            _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
            buf.clear()
            vol_stack.append(3.0)
            continue
        mv = re.fullmatch(r"(?:vol|volume)\s*[:=\s]\s*(.+)", low)
        if mv:
            parsed_db = _parse_vol_val(mv.group(1))
            if parsed_db is not None:
                _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])
                buf.clear()
                if mv.group(1).strip() in ("normal", "reset", "default"):
                    vol_stack = [0.0]
                else:
                    vol_stack.append(parsed_db)
                continue

        # Style instruction [style:...]
        mst = re.match(r"style\s*[:=]\s*(.+)$", tag, re.I | re.S)
        if mst:
            st_val = re.sub(r"\s+", " ", mst.group(1)).strip()
            if st_val:
                style_parts.append(st_val)
            continue

        # Expressive ElevenLabs v3 acting cues [shouting], [excited], [whispers], ...
        if low in ELEVEN_V3_AUDIO_CUES:
            if keep_eleven_v3_tags:
                buf.append(f"[{low}] ")
            continue

        # Unknown bracketed token -> preserve literal text without crashing
        buf.append(f"[{tag}]")

    if pos < len(raw):
        buf.append(raw[pos:])
    _emit_speech("".join(buf), speed_stack[-1], vol_stack[-1])

    # Coalesce adjacent speech runs that ended up with identical speed/gainDb (e.g. from stripped cue or style)
    merged: list[dict[str, Any]] = []
    for tok in tokens:
        if tok["kind"] == "pause":
            cleaned: dict[str, Any] = {"kind": "pause", "sec": tok["sec"]}
            if tok.get("implicit"):
                cleaned["implicit"] = True
            if merged and merged[-1]["kind"] == "pause":
                if not tok.get("implicit"):
                    merged[-1]["sec"] = round(_clamp(float(cleaned["sec"]), 0.02, 10.0), 3)
                    merged[-1].pop("implicit", None)
            elif merged:  # Drop leading pause before any words
                merged.append(cleaned)
        else:
            if (
                merged
                and merged[-1]["kind"] == "speech"
                and abs(float(merged[-1]["speed"]) - float(tok["speed"])) < 1e-3
                and abs(float(merged[-1]["gainDb"]) - float(tok["gainDb"])) < 1e-2
            ):
                merged[-1]["text"] = f"{merged[-1]['text']} {tok['text']}".strip()
            else:
                merged.append(tok)

    while merged and merged[-1]["kind"] == "pause":
        merged.pop()
    return merged, "; ".join(style_parts)


def time_stretch_mono_pcm(mono: np.ndarray, sr: int, speed_factor: float, ffmpeg_bin: str) -> np.ndarray:
    """Pitch-preserving tempo change via FFmpeg `atempo` (no chipmunk pitch shift)."""
    arr = np.asarray(mono, dtype=np.float32).reshape(-1)
    spd = _clamp(float(speed_factor), 0.65, 1.50)
    if arr.size < 64 or abs(spd - 1.0) < 0.015 or not ffmpeg_bin:
        return arr
    pcm16_in = np.round(np.clip(arr, -0.99, 0.99) * 32767.0).astype("<i2").tobytes()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(int(sr)),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-af",
        f"atempo={spd:.4f}",
        "-f",
        "s16le",
        "-ar",
        str(int(sr)),
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=pcm16_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=25,
            **_no_window_kwargs(),
        )
        if proc.returncode == 0 and len(proc.stdout) >= 4:
            usable = (len(proc.stdout) // 2) * 2
            return (np.frombuffer(proc.stdout[:usable], dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32)
    except Exception:
        pass
    return arr


def master_announcer_stereo(
    mono_pcm: np.ndarray,
    src_sr: int,
    *,
    target_sr: int = SHOW_SR,
    reverb_wet: float = 0.06,
    boost_db: float = 2.0,
) -> np.ndarray:
    """Broadcast/arena mastering: zero-phase warm chest EQ, smooth room hall, -0.9 dBFS true-peak cap."""
    x = np.asarray(mono_pcm, dtype=np.float32).reshape(-1)
    if x.size < 16:
        return np.zeros((max(16, int(0.2 * target_sr)), 2), dtype=np.float32)

    if int(src_sr) != int(target_sr):
        g = math.gcd(int(src_sr), int(target_sr))
        x = resample_poly(x, int(target_sr) // g, int(src_sr) // g).astype(np.float32)

    # 1. Clean 55 Hz sub-rumble filter (preserves deep baritone fundamentals)
    sos_hpf = butter(2, 55.0, btype="highpass", fs=target_sr, output="sos")
    x = sosfilt(sos_hpf, x).astype(np.float32)

    # 2. Zero-phase warm baritone EQ (85-200 Hz chest + 2.6-4.2 kHz clarity) — ZERO comb/phase smear!
    sos_warm = butter(2, [85.0, 200.0], btype="bandpass", fs=target_sr, output="sos")
    sos_pres = butter(2, [2600.0, 4200.0], btype="bandpass", fs=target_sr, output="sos")
    if x.size > 36:
        warm = sosfiltfilt(sos_warm, x).astype(np.float32)
        pres = sosfiltfilt(sos_pres, x).astype(np.float32)
    else:
        warm = sosfilt(sos_warm, x).astype(np.float32)
        pres = sosfilt(sos_pres, x).astype(np.float32)
    x = (x + 0.24 * warm + 0.14 * pres).astype(np.float32)

    # 3. Gentle broadcast compression & level target BEFORE stereo hall IR
    env = np.sqrt(np.maximum(1e-8, sosfilt(butter(1, 14.0, btype="lowpass", fs=target_sr, output="sos"), x * x)))
    comp_gain = np.where(env > 0.18, 0.18 + (env - 0.18) * 0.42, env) / env
    x = (x * comp_gain).astype(np.float32)

    rms = float(np.sqrt(np.mean(x * x)))
    if rms > 1e-6:
        x *= 0.25 / rms

    # 4. Warm diffuse arena room IR (45ms pre-delay, 2.2 kHz damped tail, zero hollow metallic ring)
    wet = _clamp(float(reverb_wet), 0.0, 0.30)
    if wet > 0.005:
        ir_len = int(round(0.78 * target_sr))
        pre_n = int(round(0.045 * target_sr))
        rng = np.random.default_rng(4401)
        t_ir = np.linspace(0.0, 0.78, ir_len, endpoint=False, dtype=np.float32)
        decay = np.exp(-t_ir / 0.22)
        sos_ir = butter(2, 2200.0, btype="lowpass", fs=target_sr, output="sos")
        ir_l = sosfilt(sos_ir, rng.standard_normal(ir_len).astype(np.float32)) * decay
        ir_r = sosfilt(sos_ir, rng.standard_normal(ir_len).astype(np.float32)) * decay
        ir_l[:pre_n] = 0.0
        ir_r[:pre_n] = 0.0
        # Gentle stereo early reflections ahead of dense diffuse tail
        er_l = int(round(0.022 * target_sr))
        er_r = int(round(0.031 * target_sr))
        if er_l < ir_len:
            ir_l[er_l] += 0.18
        if er_r < ir_len:
            ir_r[er_r] += 0.16
        norm_ir = max(float(np.sqrt(np.sum(ir_l * ir_l))), float(np.sqrt(np.sum(ir_r * ir_r))), 1e-6)
        ir_l /= norm_ir
        ir_r /= norm_ir
        tail_n = int(round(0.38 * target_sr))
        xp = np.pad(x, (0, tail_n))
        wet_l = fftconvolve(xp, ir_l, mode="full")[: xp.size].astype(np.float32)
        wet_r = fftconvolve(xp, ir_r, mode="full")[: xp.size].astype(np.float32)
        stereo = np.stack([(1.0 - wet) * xp + wet * wet_l, (1.0 - wet) * xp + wet * wet_r], axis=1)
    else:
        stereo = np.stack([x, x], axis=1)

    # 5. Loudness trim + smooth soft-knee true-peak limiter (0.90 ceiling = -0.9 dBFS, ZERO hard clipping)
    lin_boost = 10.0 ** (_clamp(float(boost_db), -4.0, 6.0) / 20.0)
    stereo = stereo * float(lin_boost)
    peak = float(np.max(np.abs(stereo)))
    if peak > 0.90:
        stereo = np.where(np.abs(stereo) > 0.72, np.sign(stereo) * (0.72 + 0.18 * np.tanh((np.abs(stereo) - 0.72) / 0.25)), stereo)
        pk2 = float(np.max(np.abs(stereo)))
        if pk2 > 0.90:
            stereo *= 0.90 / pk2

    return trim_edge_silence_stereo(stereo.astype(np.float32), sr=target_sr, thresh=0.003, pad_lead_ms=10.0, pad_tail_ms=90.0)


def duck_bed_under_speech(
    bed: np.ndarray,
    speech: np.ndarray,
    speech_offset_samples: int,
    *,
    sr: int = SHOW_SR,
    duck_gain: float = 0.16,
    base_gain: float = 0.85,
) -> np.ndarray:
    """Broadcast/DJ sidechain ducker: 70ms lookahead attack, 650ms hold, 650ms smooth release."""
    out = bed.copy()
    n_out = out.shape[0]
    if n_out == 0 or speech.shape[0] == 0:
        return (out * float(base_gain)).astype(np.float32)

    sp_abs_full = np.zeros(n_out, dtype=np.float32)
    s0 = max(0, int(speech_offset_samples))
    avail = min(speech.shape[0], n_out - s0)
    if avail > 0:
        sp_abs_full[s0 : s0 + avail] = np.max(np.abs(speech[:avail]), axis=1)

    # Downsample to 100 Hz (10 ms blocks) for hold-and-release broadcast dynamics
    hop = max(1, int(round(0.010 * sr)))
    n_blk = max(1, int(math.ceil(float(n_out) / float(hop))))
    pad_len = n_blk * hop - n_out
    env_padded = np.pad(sp_abs_full, (0, pad_len)) if pad_len > 0 else sp_abs_full[: n_blk * hop]
    blk_peak = env_padded.reshape(n_blk, hop).max(axis=1)

    active = np.clip((blk_peak - 0.015) / 0.045, 0.0, 1.0)
    held = active.copy()

    # 70 ms lookahead pre-dip so initial speech consonants stay clear
    ahead_blks = max(1, int(round(0.070 / 0.010)))
    for sh in range(1, min(n_blk, ahead_blks + 1)):
        held[:-sh] = np.maximum(held[:-sh], active[sh:])

    # 650 ms hold dilation across commas, breaths, and dramatic gaps
    hold_blks = max(1, int(round(0.650 / 0.010)))
    hold_rem = 0
    hold_lvl = 0.0
    for idx in range(n_blk):
        cur_v = float(held[idx])
        if cur_v >= 0.25:
            hold_lvl = max(cur_v, hold_lvl * 0.96 if hold_rem > 0 else cur_v)
            hold_rem = hold_blks
            held[idx] = max(cur_v, hold_lvl)
        elif hold_rem > 0:
            held[idx] = max(cur_v, hold_lvl)
            hold_rem -= 1

    # Asymmetric smoothing: ~70ms attack, ~650ms silky release
    gate_blk = np.zeros(n_blk, dtype=np.float32)
    alpha_atk = 1.0 - math.exp(-0.010 / 0.024)
    alpha_rel = 1.0 - math.exp(-0.010 / 0.220)
    g_cur = 0.0
    for idx in range(n_blk):
        tgt = float(held[idx])
        a = alpha_atk if tgt >= g_cur else alpha_rel
        g_cur = g_cur + a * (tgt - g_cur)
        gate_blk[idx] = g_cur

    t_blk = (np.arange(n_blk, dtype=np.float32) + 0.5) * float(hop)
    t_samp = np.arange(n_out, dtype=np.float32)
    gate = np.interp(t_samp, t_blk, gate_blk).astype(np.float32)
    gain_curve = (float(base_gain) - gate * (float(base_gain) - float(duck_gain))).astype(np.float32)
    return (out * gain_curve[:, None]).astype(np.float32)


def _preset_for_voice(voice_id: str) -> dict[str, Any]:
    if voice_id in EPIC_VOICE_PRESETS:
        return dict(EPIC_VOICE_PRESETS[voice_id])
    if voice_id.startswith("elevenlabs:"):
        return {"engine": "elevenlabs", "voiceId": voice_id.split(":", 1)[1], "label": voice_id}
    if voice_id.startswith("gemini:"):
        return {"engine": "gemini", "voiceName": voice_id.split(":", 1)[1], "label": voice_id}
    if voice_id.startswith("openai:"):
        return {"engine": "openai", "voiceName": voice_id.split(":", 1)[1], "label": voice_id}
    if voice_id.startswith("edge:"):
        return {"engine": "edge", "voice": voice_id.split(":", 1)[1], "label": voice_id}
    return dict(EPIC_VOICE_PRESETS[DEFAULT_ANNOUNCER_VOICE])


def _decode_mp3_bytes_mono(mp3_bytes: bytes, *, target_sr: int = 24_000) -> np.ndarray:
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=int(target_sr))
    dec: list[np.ndarray] = []
    with av.open(io.BytesIO(mp3_bytes), format="mp3") as container:
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                dec.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None):
            dec.append(rf.to_ndarray().reshape(-1))
    if not dec:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(dec).astype(np.float32)


def _decode_wav_bytes_mono(wav_bytes: bytes, *, target_sr: int = 24_000) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw != 2 or not frames:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        n = (arr.size // ch) * ch
        arr = np.mean(arr[:n].reshape(-1, ch), axis=1).astype(np.float32)
    if int(sr) != int(target_sr) and arr.size > 16:
        g = math.gcd(int(sr), int(target_sr))
        arr = resample_poly(arr, int(target_sr) // g, int(sr) // g).astype(np.float32)
    return arr.astype(np.float32)


def _trim_mono_edges(seg: np.ndarray, sr: int = 24_000) -> np.ndarray:
    if seg.size == 0:
        return seg
    nz = np.flatnonzero(np.abs(seg) >= 0.003)
    if nz.size:
        pad_lead = int(0.025 * sr)
        pad_tail = int(0.085 * sr)
        return seg[max(0, int(nz[0]) - pad_lead) : min(seg.size, int(nz[-1]) + pad_tail)].astype(np.float32)
    return seg.astype(np.float32)


def _format_tts_error(engine: str, exc: Exception) -> str:
    """Format cloud TTS exceptions into concise, actionable messages (esp. Gemini 429 daily quota)."""
    raw = str(exc or "").strip()
    if engine == "gemini" and ("429" in raw or "RESOURCE_EXHAUSTED" in raw.upper()):
        wait_str = ""
        m_sec = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)(?:\.\d+)?s", raw)
        if m_sec:
            secs = max(1, int(m_sec.group(1)))
            h, rem = divmod(secs, 3600)
            m = rem // 60
            wait_str = f"~{h}h {m}m" if h else (f"~{m}m" if m else f"~{secs}s")
        else:
            m_txt = re.search(r"retry in\s+([0-9hms\.]+)", raw, re.I)
            if m_txt:
                wait_str = f"~{m_txt.group(1).strip('.')}"
        wait_part = f", {wait_str}" if wait_str else ""
        return (
            f"Gemini Free Tier daily quota reached (10 requests/day; resets at midnight PT{wait_part}). "
            "Switch to ElevenLabs (10k chars/mo) or Kokoro Local meanwhile, or enable pay-as-you-go billing in Google AI Studio. "
            "Cached lines reuse 0 quota."
        )
    return re.sub(r"\s+", " ", raw)[:260] or f"{engine} TTS synthesis failed"


def _coalesce_gemini_tokens(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge implicit `...` pauses between equal-style speech runs to cut Gemini TTS API requests per show."""
    out: list[dict[str, Any]] = []
    for tok in tokens:
        if (
            tok.get("kind") == "speech"
            and len(out) >= 2
            and out[-1].get("kind") == "pause"
            and out[-1].get("implicit")
            and out[-2].get("kind") == "speech"
            and abs(float(out[-2].get("speed", 1.0)) - float(tok.get("speed", 1.0))) < 1e-4
            and abs(float(out[-2].get("gainDb", 0.0)) - float(tok.get("gainDb", 0.0))) < 0.05
        ):
            out.pop()
            out[-1]["text"] = f"{out[-1]['text']} ... {tok['text']}"
        else:
            out.append(dict(tok))
    return out


def synthesize_announcer_wav(
    text: str,
    *,
    voice: str = DEFAULT_ANNOUNCER_VOICE,
    rate: str = "-4%",
    pitch: str = "",
    out_wav: Path,
    ffmpeg_bin: str = "",
    allow_network: bool = True,
    models_dir: Path | None = None,
    tts_cache_dir: Path | None = None,
    reverb_wet: float = 0.06,
    boost_db: float = 2.0,
    voice_style: str = "",
    tts_secrets: dict[str, Any] | None = None,
    http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
    gemini_synth_fn: Callable[[str, str, str], bytes] | None = None,
) -> Path:
    """Synthesize a high-end studio announcer stem (ElevenLabs / Gemini 2.5 TTS / OpenAI / Kokoro / Edge HD)."""
    del pitch  # Forbidden SSML pitch-shifting warble
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    voice_id = resolve_voice_id(voice)
    preset = _preset_for_voice(voice_id)
    engine = str(preset.get("engine") or "kokoro")
    sec_cfg = dict(tts_secrets or {})
    el_model = str(sec_cfg.get("elevenlabsModel") or DEFAULT_ELEVENLABS_MODEL).strip() or DEFAULT_ELEVENLABS_MODEL

    def _seg_cache_path(key_parts: tuple[str, ...]) -> Path | None:
        if tts_cache_dir is None:
            return None
        tts_cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1("|".join(key_parts).encode("utf-8")).hexdigest()[:24]
        return tts_cache_dir / f"show_seg_{digest}.wav"

    def _load_cached_mono(p: Path | None, target_sr: int) -> np.ndarray | None:
        if p is None or not p.is_file() or p.stat().st_size < 100:
            return None
        try:
            with wave.open(str(p), "rb") as wf:
                sr = wf.getframerate()
                ch = wf.getnchannels()
                raw_pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
            if ch > 1:
                n = (raw_pcm.size // ch) * ch
                raw_pcm = np.mean(raw_pcm[:n].reshape(-1, ch), axis=1).astype(np.float32)
            if int(sr) != int(target_sr) and raw_pcm.size > 16:
                g = math.gcd(int(sr), int(target_sr))
                raw_pcm = resample_poly(raw_pcm, int(target_sr) // g, int(sr) // g).astype(np.float32)
            return raw_pcm.astype(np.float32) if raw_pcm.size else None
        except Exception:
            return None

    def _save_cached_mono(p: Path | None, mono: np.ndarray, sr: int) -> None:
        if p is None or mono.size == 0:
            return
        try:
            pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
            tmp_c = p.with_suffix(".wav.tmp")
            with wave.open(str(tmp_c), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(sr))
                wf.writeframes(pcm16.tobytes())
            tmp_c.replace(p)
        except Exception:
            pass

    tokens, inline_style = parse_speech_markup(
        text,
        default_speed=1.0,
        keep_eleven_v3_tags=(engine == "elevenlabs" and el_model == "eleven_v3"),
    )
    speech_indices = [i for i, t in enumerate(tokens) if t.get("kind") == "speech"]
    if not speech_indices:
        pcm = np.zeros((int(0.5 * SHOW_SR), 2), dtype=np.float32)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    effective_style = "; ".join(p for p in (str(voice_style or "").strip(), inline_style) if p)
    last_speech_idx = speech_indices[-1]
    has_multiple_speech = len(speech_indices) > 1
    k_sr = 24_000

    def _do_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 30.0) -> bytes:
        raw_body = json.dumps(payload).encode("utf-8")
        if http_post is not None:
            code, resp_bytes = http_post(url, raw_body, headers, timeout)
            if code >= 400:
                msg = resp_bytes.decode("utf-8", errors="ignore")[:240]
                raise RuntimeError(f"TTS HTTP {code}: {msg}")
            return resp_bytes
        if not allow_network:
            raise RuntimeError("Network disabled for cloud TTS")
        req = urllib.request.Request(url, data=raw_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as he:
            detail = ""
            try:
                detail = he.read().decode("utf-8", errors="ignore")[:240]
            except Exception:
                pass
            raise RuntimeError(f"TTS HTTP {he.code}: {detail or he.reason}") from he

    # Engine 1: Local Kokoro-82M FP32
    if engine == "kokoro":
        kokoro = _get_kokoro(models_dir, allow_fallback=allow_network)
        if not allow_network and kokoro is None:
            pcm = synth_demo_music_bed("anthem", 1.2, sr=SHOW_SR) * 0.55
            pcm = apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=0.25)
            tmp = out_wav.with_suffix(".wav.tmp")
            tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
            tmp.replace(out_wav)
            return out_wav
        if kokoro is not None:
            weights: dict[str, float] = dict(preset.get("weights") or {"am_onyx": 0.55, "am_fenrir": 0.45})
            style_vec = None
            for v_name, w in weights.items():
                vec = kokoro.get_voice_style(v_name) * float(w)
                style_vec = vec if style_vec is None else (style_vec + vec)
            base_speed = float(preset.get("speed", 0.95))
            final_speed = float(preset.get("finalSpeed", 0.88))
            parts: list[np.ndarray] = []
            for idx, tok in enumerate(tokens):
                if tok["kind"] == "pause":
                    parts.append(np.zeros(int(round(float(tok["sec"]) * k_sr)), dtype=np.float32))
                    continue
                is_last = idx == last_speech_idx and has_multiple_speech
                tok_spd = float(tok.get("speed", 1.0))
                spd = _clamp((final_speed if (is_last and abs(tok_spd - 1.0) < 1e-3) else base_speed) * tok_spd, 0.65, 1.50)
                samples, k_sr = kokoro.create(tok["text"], voice=style_vec, speed=spd, lang="en-us")
                arr = _trim_mono_edges(np.asarray(samples, dtype=np.float32).reshape(-1), k_sr)
                gdb = float(tok.get("gainDb", 0.0))
                if abs(gdb) > 0.05:
                    arr = (arr * (10.0 ** (gdb / 20.0))).astype(np.float32)
                parts.append(arr)
            mono = np.concatenate(parts) if parts else np.zeros(int(0.5 * k_sr), dtype=np.float32)
            stereo = master_announcer_stereo(mono, k_sr, target_sr=SHOW_SR, reverb_wet=reverb_wet, boost_db=boost_db)
            tmp = out_wav.with_suffix(".wav.tmp")
            tmp.write_bytes(encode_stereo_pcm16_wav(stereo, sr=SHOW_SR))
            tmp.replace(out_wav)
            return out_wav

    # Engine 2: ElevenLabs Studio Cloud
    if engine == "elevenlabs":
        el_key = str(sec_cfg.get("elevenlabsKey") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not el_key:
            raise RuntimeError("ElevenLabs API key missing — add your key under Studio Voice API Keys in Show Lab.")
        el_vid = str(preset.get("voiceId") or "JBFqnCBsd6RMkjVDRZzb").strip()
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(el_vid, safe='')}?output_format=mp3_44100_128"
        hdrs = {"xi-api-key": el_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
        mono_parts: list[np.ndarray] = []
        for tok in tokens:
            if tok["kind"] == "pause":
                mono_parts.append(np.zeros(int(round(float(tok["sec"]) * k_sr)), dtype=np.float32))
                continue
            c_path = _seg_cache_path(("elevenlabs", el_vid, el_model, tok["text"]))
            seg = _load_cached_mono(c_path, k_sr)
            if seg is None:
                vset: dict[str, Any] = {"stability": 0.45, "similarity_boost": 0.78}
                if el_model != "eleven_v3":
                    vset["style"] = 0.45
                mp3_bytes = _do_post_json(
                    url,
                    hdrs,
                    {"text": tok["text"], "model_id": el_model, "voice_settings": vset},
                )
                seg = _decode_mp3_bytes_mono(mp3_bytes, target_sr=k_sr)
                seg = _trim_mono_edges(seg, k_sr)
                _save_cached_mono(c_path, seg, k_sr)
            seg = time_stretch_mono_pcm(seg, k_sr, float(tok.get("speed", 1.0)), ffmpeg_bin)
            gdb = float(tok.get("gainDb", 0.0))
            if abs(gdb) > 0.05:
                seg = (seg * (10.0 ** (gdb / 20.0))).astype(np.float32)
            mono_parts.append(seg)
        mono_all = np.concatenate(mono_parts) if mono_parts else np.zeros(int(0.5 * k_sr), dtype=np.float32)
        stereo = master_announcer_stereo(mono_all, k_sr, target_sr=SHOW_SR, reverb_wet=reverb_wet, boost_db=boost_db)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(stereo, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    # Engine 3: Google Gemini 2.5 Flash TTS
    if engine == "gemini":
        gem_key = str(sec_cfg.get("geminiKey") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        if not gem_key and gemini_synth_fn is None:
            raise RuntimeError("Gemini API key missing — add your free Google AI Studio key in Show Lab.")
        v_name = str(preset.get("voiceName") or "Fenrir").strip()
        gem_client = None
        types_mod = None
        gem_tokens = _coalesce_gemini_tokens(tokens)

        mono_parts = []
        for tok in gem_tokens:
            if tok["kind"] == "pause":
                mono_parts.append(np.zeros(int(round(float(tok["sec"]) * k_sr)), dtype=np.float32))
                continue
            c_path = _seg_cache_path(("gemini", v_name, effective_style, tok["text"]))
            seg = _load_cached_mono(c_path, k_sr)
            if seg is None:
                prompt_txt = (
                    f"Audio direction ({effective_style}). Speak only the exact script below naturally with that energy:\n\n{tok['text']}"
                    if effective_style
                    else tok["text"]
                )
                if gemini_synth_fn is not None:
                    raw_pcm = gemini_synth_fn(prompt_txt, v_name, effective_style)
                else:
                    if gem_client is None:
                        if not allow_network:
                            raise RuntimeError("Network disabled for Gemini TTS")
                        from google import genai
                        from google.genai import types as g_types

                        gem_client = genai.Client(api_key=gem_key)
                        types_mod = g_types
                    assert gem_client is not None and types_mod is not None
                    try:
                        resp = gem_client.models.generate_content(
                            model="gemini-2.5-flash-preview-tts",
                            contents=prompt_txt,
                            config=types_mod.GenerateContentConfig(
                                response_modalities=["AUDIO"],
                                speech_config=types_mod.SpeechConfig(
                                    voice_config=types_mod.VoiceConfig(
                                        prebuilt_voice_config=types_mod.PrebuiltVoiceConfig(voice_name=v_name)
                                    )
                                ),
                            ),
                        )
                    except Exception as exc:
                        raise RuntimeError(_format_tts_error("gemini", exc)) from exc
                    try:
                        raw_pcm = resp.candidates[0].content.parts[0].inline_data.data
                    except (IndexError, AttributeError) as exc:
                        raise RuntimeError("Gemini TTS returned empty audio response") from exc
                if isinstance(raw_pcm, str):
                    import base64

                    raw_pcm = base64.b64decode(raw_pcm)
                usable = (len(raw_pcm) // 2) * 2
                seg = (np.frombuffer(raw_pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0) if usable else np.zeros(0, dtype=np.float32)
                seg = _trim_mono_edges(seg, k_sr)
                _save_cached_mono(c_path, seg, k_sr)
            seg = time_stretch_mono_pcm(seg, k_sr, float(tok.get("speed", 1.0)), ffmpeg_bin)
            gdb = float(tok.get("gainDb", 0.0))
            if abs(gdb) > 0.05:
                seg = (seg * (10.0 ** (gdb / 20.0))).astype(np.float32)
            mono_parts.append(seg)
        mono_all = np.concatenate(mono_parts) if mono_parts else np.zeros(int(0.5 * k_sr), dtype=np.float32)
        stereo = master_announcer_stereo(mono_all, k_sr, target_sr=SHOW_SR, reverb_wet=reverb_wet, boost_db=boost_db)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(stereo, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    # Engine 4: OpenAI gpt-4o-mini-tts
    if engine == "openai":
        oai_key = str(sec_cfg.get("openaiKey") or os.environ.get("OPENAI_API_KEY") or "").strip()
        if not oai_key:
            raise RuntimeError("OpenAI API key missing — add your OpenAI key in Show Lab.")
        oai_voice = str(preset.get("voiceName") or "onyx").strip()
        url = "https://api.openai.com/v1/audio/speech"
        hdrs = {"Authorization": f"Bearer {oai_key}", "Content-Type": "application/json"}
        inst = effective_style or DEFAULT_RYDER_VOICE_STYLE
        mono_parts = []
        for tok in tokens:
            if tok["kind"] == "pause":
                mono_parts.append(np.zeros(int(round(float(tok["sec"]) * k_sr)), dtype=np.float32))
                continue
            c_path = _seg_cache_path(("openai", oai_voice, inst, tok["text"]))
            seg = _load_cached_mono(c_path, k_sr)
            if seg is None:
                wav_bytes = _do_post_json(
                    url,
                    hdrs,
                    {
                        "model": "gpt-4o-mini-tts",
                        "voice": oai_voice,
                        "input": tok["text"],
                        "instructions": inst,
                        "response_format": "wav",
                    },
                )
                seg = _decode_wav_bytes_mono(wav_bytes, target_sr=k_sr)
                seg = _trim_mono_edges(seg, k_sr)
                _save_cached_mono(c_path, seg, k_sr)
            seg = time_stretch_mono_pcm(seg, k_sr, float(tok.get("speed", 1.0)), ffmpeg_bin)
            gdb = float(tok.get("gainDb", 0.0))
            if abs(gdb) > 0.05:
                seg = (seg * (10.0 ** (gdb / 20.0))).astype(np.float32)
            mono_parts.append(seg)
        mono_all = np.concatenate(mono_parts) if mono_parts else np.zeros(int(0.5 * k_sr), dtype=np.float32)
        stereo = master_announcer_stereo(mono_all, k_sr, target_sr=SHOW_SR, reverb_wet=reverb_wet, boost_db=boost_db)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(stereo, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    # Engine 5: Clean HD Edge Multilingual voice (ZERO SSML pitch warble, ZERO aecho)
    edge_voice = str(preset.get("voice") or "en-US-BrianMultilingualNeural")
    edge_rate = str(preset.get("rate") or rate or "-2%")
    if not edge_rate.startswith(("+", "-")):
        edge_rate = "-2%"

    if not allow_network:
        pcm = synth_demo_music_bed("anthem", 1.2, sr=SHOW_SR) * 0.55
        pcm = apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=0.25)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    import asyncio
    import edge_tts

    async def _fetch_phrase(phr: str, spd_rate: str) -> bytes:
        comm = edge_tts.Communicate(phr, voice=edge_voice, rate=spd_rate)
        chunks: list[bytes] = []
        async for ch in comm.stream():
            if ch.get("type") == "audio":
                chunks.append(ch["data"])
        return b"".join(chunks)

    base_edge_pct = 0
    try:
        base_edge_pct = int(round(float(edge_rate.rstrip("%"))))
    except Exception:
        base_edge_pct = -2

    mono_parts = []
    for idx, tok in enumerate(tokens):
        if tok["kind"] == "pause":
            mono_parts.append(np.zeros(int(round(float(tok["sec"]) * k_sr)), dtype=np.float32))
            continue
        is_last = idx == last_speech_idx and has_multiple_speech
        tok_spd = float(tok.get("speed", 1.0))
        if abs(tok_spd - 1.0) >= 0.015:
            pct = max(-35, min(50, int(round((tok_spd - 1.0) * 100.0))))
            r_str = f"{pct:+d}%"
        else:
            r_str = "-8%" if is_last else f"{base_edge_pct:+d}%"
        mp3_bytes = b""
        for attempt in range(3):
            try:
                mp3_bytes = asyncio.run(_fetch_phrase(tok["text"], r_str))
                if mp3_bytes:
                    break
            except Exception:
                time.sleep(0.25 * (attempt + 1))
        if not mp3_bytes:
            continue
        seg = _decode_mp3_bytes_mono(mp3_bytes, target_sr=k_sr)
        seg = _trim_mono_edges(seg, k_sr)
        gdb = float(tok.get("gainDb", 0.0))
        if abs(gdb) > 0.05:
            seg = (seg * (10.0 ** (gdb / 20.0))).astype(np.float32)
        if seg.size:
            mono_parts.append(seg)

    if not mono_parts:
        pcm = synth_demo_music_bed("anthem", 1.2, sr=SHOW_SR) * 0.55
        pcm = apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=0.25)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(encode_stereo_pcm16_wav(pcm, sr=SHOW_SR))
        tmp.replace(out_wav)
        return out_wav

    mono_all = np.concatenate(mono_parts)
    stereo = master_announcer_stereo(mono_all, k_sr, target_sr=SHOW_SR, reverb_wet=reverb_wet, boost_db=boost_db)
    tmp = out_wav.with_suffix(".wav.tmp")
    tmp.write_bytes(encode_stereo_pcm16_wav(stereo, sr=SHOW_SR))
    tmp.replace(out_wav)
    return out_wav


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

    pcm = np.zeros((0, 2), dtype=np.float32)
    if src_path.suffix.lower() == ".wav":
        pcm = read_wav_stereo_float(src_path, target_sr=SHOW_SR)
        if pcm.shape[0] > 0:
            s0 = int(round(start * SHOW_SR))
            pcm = pcm[s0:]
            if trim_leading_silence:
                pcm = trim_edge_silence_stereo(pcm, sr=SHOW_SR, thresh=0.015, pad_lead_ms=8.0, pad_tail_ms=0.0)
            max_n = int(round(dur * SHOW_SR))
            if pcm.shape[0] > max_n:
                pcm = pcm[:max_n]

    if pcm.shape[0] == 0 and ffmpeg_bin:
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
    elif pcm.shape[0] == 0 and src_path.suffix.lower() != ".wav":
        raise RuntimeError("FFmpeg is required to decode compressed audio files (.mp3/.m4a/.ogg)")

    if pcm.shape[0] == 0:
        raise RuntimeError("Trimmed audio segment is empty (check songStartSec)")
    peak = float(np.max(np.abs(pcm)))
    if peak > 1e-6:
        pcm = (pcm * (0.88 / peak)).astype(np.float32)
    if float(fade_out_sec) <= 0.01:
        return pcm.astype(np.float32)
    return apply_fade_out_stereo(pcm, sr=SHOW_SR, fade_out_sec=fade_out_sec)


def resample_stereo_pcm(pcm: np.ndarray, src_sr: int, target_sr: int) -> np.ndarray:
    """High-accuracy rational resampling for stereo float32 PCM."""
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    if arr.shape[0] == 0 or int(src_sr) == int(target_sr):
        return arr.astype(np.float32)
    g = math.gcd(int(src_sr), int(target_sr))
    up = int(target_sr) // g
    down = int(src_sr) // g
    l = resample_poly(arr[:, 0], up, down).astype(np.float32)
    r = resample_poly(arr[:, 1], up, down).astype(np.float32)
    return np.stack([l, r], axis=1).astype(np.float32)


def apply_timed_duck_stereo(
    pcm: np.ndarray,
    *,
    sr: int,
    duck_start_sec: float,
    duck_hold_sec: float,
    duck_gain: float = 0.22,
    attack_sec: float = 0.25,
    release_sec: float = 0.45,
) -> np.ndarray:
    """Apply smooth cosine gain ducking over a specific `[duck_start_sec, duck_start_sec + duck_hold_sec]` window."""
    out = pcm.copy()
    n = out.shape[0]
    if n == 0 or duck_hold_sec <= 0.0:
        return out
    env = np.ones(n, dtype=np.float32)
    s0 = max(0, min(n, int(round(duck_start_sec * sr))))
    s1 = max(s0, min(n, int(round((duck_start_sec + duck_hold_sec) * sr))))
    atk_n = max(8, int(round(attack_sec * sr)))
    rel_n = max(8, int(round(release_sec * sr)))

    atk_start = max(0, s0 - atk_n // 2)
    atk_end = min(n, atk_start + atk_n)
    if atk_end > atk_start:
        t_a = np.linspace(0.0, 1.0, atk_end - atk_start, dtype=np.float32)
        env[atk_start:atk_end] = 1.0 - (1.0 - float(duck_gain)) * 0.5 * (1.0 - np.cos(np.pi * t_a))

    hold_start = min(n, atk_end)
    hold_end = min(n, s1)
    if hold_end > hold_start:
        env[hold_start:hold_end] = float(duck_gain)

    rel_start = hold_end
    rel_end = min(n, rel_start + rel_n)
    if rel_end > rel_start:
        t_r = np.linspace(0.0, 1.0, rel_end - rel_start, dtype=np.float32)
        env[rel_start:rel_end] = float(duck_gain) + (1.0 - float(duck_gain)) * 0.5 * (1.0 - np.cos(np.pi * t_r))

    out *= env[:, None]
    return out


def _resolve_ffprobe_bin(ffmpeg_bin: str) -> str:
    if ffmpeg_bin:
        p = Path(ffmpeg_bin)
        cand = p.with_name("ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe")
        if cand.is_file():
            return str(cand)
    return "ffprobe"


def _probe_media_duration_sec(media_path: Path, ffmpeg_bin: str) -> float:
    """Return exact container/video duration in seconds via ffprobe (falling back to ffmpeg stderr)."""
    ffprobe = _resolve_ffprobe_bin(ffmpeg_bin)
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **_no_window_kwargs(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            val = float(proc.stdout.strip())
            if val > 0.0:
                return val
    except Exception:
        pass
    if ffmpeg_bin:
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-i", str(media_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                **_no_window_kwargs(),
            )
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
    return 0.0


def _intro_tint_for_scene(scene: str) -> tuple[int, int, int]:
    """Return radial intro glow RGB tint tuned to each Smart Fogger theater scene."""
    table = {
        "jenna": (185, 65, 255),
        "avenleigh": (255, 60, 175),
        "vip_gold": (255, 195, 75),
        "vip_prism": (190, 230, 255),
    }
    return table.get(str(scene or "").strip().lower(), (255, 255, 255))


def _generate_intro_pulse_video(
    out_mp4: Path,
    *,
    duration_sec: float,
    lightning_sec: float = 0.75,
    flash_count: int = 2,
    tint_rgb: tuple[int, int, int] = (255, 255, 255),
    ffmpeg_bin: str,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> float:
    """Render dramatic monochrome white-to-black arena pulse & strobe intro video matched to intro duration."""
    if not ffmpeg_bin:
        raise RuntimeError("FFmpeg is required to generate intro pulse video")
    dur = max(1.5, float(duration_sec))
    n_frames = max(15, int(round(dur * fps)))
    actual_dur = float(n_frames) / float(fps)

    gw, gh = 320, 180
    yy, xx = np.mgrid[0:gh, 0:gw].astype(np.float32)
    nx = (xx - (gw - 1) * 0.5) / ((gw - 1) * 0.5)
    ny = (yy - (gh - 1) * 0.5) / ((gh - 1) * 0.5)
    radial = np.clip(1.0 - 0.36 * (nx * nx + ny * ny), 0.55, 1.0).astype(np.float32)
    flat = np.ones((gh, gw), dtype=np.float32)

    strobe_guard = 1.05 if flash_count >= 2 else (0.55 if flash_count == 1 else 0.25)
    t_pulse0 = (lightning_sec + 0.85) if lightning_sec > 0.0 else 0.90
    period = 1.50

    tr = int(max(0, min(255, tint_rgb[0])))
    tg = int(max(0, min(255, tint_rgb[1])))
    tb = int(max(0, min(255, tint_rgb[2])))
    use_rgb = (tr, tg, tb) != (255, 255, 255)
    tint_vec = np.array([tr, tg, tb], dtype=np.float32)

    frames = bytearray()
    for i in range(n_frames):
        t = float(i) / float(fps)
        rem = actual_dur - t
        full_white = 0.0
        radial_white = 0.0

        # 1. Opening lightning crack during thunder rumble
        if lightning_sec > 0.0:
            dt = t - lightning_sec
            if 0.0 <= dt < 0.08:
                full_white = 1.0
            elif 0.08 <= dt < 0.16:
                full_white = 0.10
            elif 0.16 <= dt < 0.24:
                full_white = 0.86
            elif 0.24 <= dt < 0.56:
                full_white = 0.86 * math.exp(-(dt - 0.24) * 9.0)

        # 2. Escalating rhythmic glow pulses under announcer build
        if t >= t_pulse0 and rem > strobe_guard:
            p_pos = t - t_pulse0
            idx = int(p_pos // period)
            phase = p_pos - idx * period
            cycle_end = t_pulse0 + (idx + 1) * period
            if cycle_end <= actual_dur - strobe_guard + 0.35:
                peak = min(0.92, 0.52 + 0.12 * idx)
                if phase < 0.12:
                    radial_white = max(radial_white, peak * ((phase / 0.12) ** 0.7))
                elif phase < 0.96:
                    radial_white = max(radial_white, peak * math.exp(-(phase - 0.12) * 3.8))

        # 3. Final full-screen white->black strobes leading straight into entrance video
        if flash_count >= 2:
            if (actual_dur - 0.92) <= t < (actual_dur - 0.72):
                full_white = 1.0
                radial_white = 0.0
            elif (actual_dur - 0.48) <= t < (actual_dur - 0.22):
                full_white = 1.0
                radial_white = 0.0
            elif rem <= 0.22:
                full_white = 0.0
                radial_white = 0.0
        elif flash_count == 1:
            if (actual_dur - 0.48) <= t < (actual_dur - 0.22):
                full_white = 1.0
                radial_white = 0.0
            elif rem <= 0.22:
                full_white = 0.0
                radial_white = 0.0

        if full_white >= radial_white:
            lum = flat * min(1.0, max(0.0, full_white))
            if use_rgb:
                u8 = np.clip(np.round(lum[:, :, None] * 255.0), 0, 255).astype(np.uint8)
                u8 = np.repeat(u8, 3, axis=2)
            else:
                u8 = np.clip(np.round(lum * 255.0), 0, 255).astype(np.uint8)
        else:
            lum = radial * min(1.0, max(0.0, radial_white))
            if use_rgb:
                u8 = np.clip(np.round(lum[:, :, None] * tint_vec[None, None, :]), 0, 255).astype(np.uint8)
            else:
                u8 = np.clip(np.round(lum * 255.0), 0, 255).astype(np.uint8)
        frames.extend(u8.tobytes())

    tmp_mp4 = out_mp4.with_name(f"{out_mp4.stem}.tmp{out_mp4.suffix}")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24" if use_rgb else "gray",
        "-s",
        f"{gw}x{gh}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-vf",
        f"scale={width}:{height}:flags=bicubic,setsar=1,format=yuv420p",
        "-c:v",
        "libx264",
        "-profile:v",
        "main",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-g",
        str(fps * 2),
        "-bf",
        "0",
        "-video_track_timescale",
        "90000",
        "-crf",
        "20",
        "-preset",
        "veryfast",
        "-an",
        str(tmp_mp4),
    ]
    proc = subprocess.run(
        cmd,
        input=bytes(frames),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=90,
        **_no_window_kwargs(),
    )
    if proc.returncode != 0 or not tmp_mp4.is_file():
        err = (proc.stderr or b"").decode("utf-8", errors="ignore")[-280:]
        try:
            tmp_mp4.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"FFmpeg intro pulse video failed: {err}")
    tmp_mp4.replace(out_mp4)
    return round(actual_dur, 3)


# ---------------- ShowDirector Orchestrator ----------------


class ShowDirector:
    """Manages show specs, custom song uploads, composite WAV/MP4 rendering, voice triggers & live playback."""

    def __init__(
        self,
        *,
        root_dir: Path,
        house_audio: Any = None,
        displays: Any = None,
        adb_controller: Any = None,
        wallboard_cfg_fn: Callable[[], Any] | None = None,
        stage_base_url_fn: Callable[[], str] | None = None,
        ffmpeg_bin: str = "",
        allow_network: bool = True,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
        gemini_synth_fn: Callable[[str, str, str], bytes] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._multimedia_build_lock = threading.Lock()
        self.root_dir = Path(root_dir)
        self.shows_dir = self.root_dir / "shows"
        self.sources_dir = self.shows_dir / "sources"
        self.rendered_dir = self.shows_dir / "rendered"
        self.fx_dir = self.shows_dir / "fx"
        self.models_dir = self.root_dir / "models" / "kokoro"
        self.config_path = self.root_dir / "shows.json"
        self.tts_secrets_path = self.root_dir / "tts_secrets.json"
        for d in (self.shows_dir, self.sources_dir, self.rendered_dir, self.fx_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.house_audio = house_audio
        self.displays = displays
        self.adb = adb_controller
        self._wallboard_cfg_fn = wallboard_cfg_fn
        self._stage_base_url_fn = stage_base_url_fn
        self.ffmpeg_bin = str(ffmpeg_bin or "").strip()
        self.allow_network = bool(allow_network)
        self._http_get = http_get
        self._http_post = http_post
        self._gemini_synth_fn = gemini_synth_fn
        self._el_voices_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._has_custom_sleep = sleep_fn is not None
        self._sleep = sleep_fn or time.sleep
        self.log = logging.getLogger("armory-shows")

        self.fx_hooks = SmartFxConfig()
        self.shows: dict[str, ShowSpec] = {}
        self._show_order: list[str] = []

        self._event_lock = threading.Lock()
        self._event_subs: list[tuple[str, queue.Queue[dict[str, Any]]]] = []
        self._stage_roles: dict[str, int] = {}
        self._stage_state: dict[str, Any] = {"type": "stage_stop", "state": "idle", "runId": 0}
        self._stage_ack_cond = threading.Condition()
        self._stage_acks: dict[tuple[int, str], str] = {}
        self.recent_cues: deque[dict[str, Any]] = deque(maxlen=30)

        self._run_lock = threading.Lock()
        self.log = logging.getLogger("armory-shows")
        self._http_fail_until: dict[str, float] = {}
        self._mdns_ipv4_cache: dict[str, tuple[str, float]] = {}
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

    # ---------- Cue & Synchronized Stage Event Bus (SSE) ----------

    def subscribe_events(self, role: str = "dashboard") -> queue.Queue[dict[str, Any]]:
        clean_role = re.sub(r"[^a-z0-9_-]+", "", str(role or "dashboard").strip().lower()) or "dashboard"
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self._event_lock:
            self._event_subs.append((clean_role, q))
            self._stage_roles[clean_role] = self._stage_roles.get(clean_role, 0) + 1
        return q

    def unsubscribe_events(self, q: queue.Queue[dict[str, Any]], role: str = "dashboard") -> None:
        clean_role = re.sub(r"[^a-z0-9_-]+", "", str(role or "dashboard").strip().lower()) or "dashboard"
        with self._event_lock:
            self._event_subs = [(r, sub_q) for (r, sub_q) in self._event_subs if sub_q is not q]
            cnt = self._stage_roles.get(clean_role, 0)
            if cnt <= 1:
                self._stage_roles.pop(clean_role, None)
            else:
                self._stage_roles[clean_role] = cnt - 1

    def connected_stage_roles(self) -> dict[str, int]:
        with self._event_lock:
            return dict(self._stage_roles)

    def has_stage_client(self, role: str) -> bool:
        clean_role = str(role or "").strip().lower()
        with self._event_lock:
            return self._stage_roles.get(clean_role, 0) > 0

    def current_stage_state(self) -> dict[str, Any]:
        with self._event_lock:
            return dict(self._stage_state)

    def _fanout_event_locked(self, evt: dict[str, Any], subs: list[tuple[str, queue.Queue[dict[str, Any]]]]) -> None:
        for _r, q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass

    def _broadcast_stage_event(self, evt_type: str, **payload: Any) -> None:
        evt: dict[str, Any] = {
            "type": evt_type,
            "tsMs": int(time.time() * 1000),
            **payload,
        }
        with self._event_lock:
            if evt_type == "stage_stop":
                self._stage_state = {"type": "stage_stop", "state": "idle", "runId": int(payload.get("runId") or 0)}
            else:
                self._stage_state = dict(evt)
            subs = list(self._event_subs)
        self._fanout_event_locked(evt, subs)

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._active_state.get("active"))

    def record_stage_ack(self, *, role: str, run_id: int, state: str) -> dict[str, Any]:
        clean_role = re.sub(r"[^a-z0-9_-]+", "", str(role or "").strip().lower())
        clean_state = re.sub(r"[^a-z0-9_-]+", "", str(state or "").strip().lower())
        if not clean_role:
            return {"ok": False, "error": "Missing role"}
        with self._stage_ack_cond:
            self._stage_acks[(int(run_id), clean_role)] = clean_state
            self._stage_ack_cond.notify_all()
        return {"ok": True, "role": clean_role, "runId": int(run_id), "state": clean_state}

    def _wait_for_stage_acks(
        self,
        run_id: int,
        roles: Sequence[str],
        *,
        target_states: tuple[str, ...] = ("ready",),
        cancel_ev: threading.Event | None = None,
        timeout_sec: float = 5.5,
    ) -> dict[str, bool]:
        wanted = [str(r or "").strip().lower() for r in roles if str(r or "").strip()]
        if not wanted:
            return {}
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            with self._stage_ack_cond:
                status = {r: (self._stage_acks.get((int(run_id), r)) in target_states) for r in wanted}
                if all(status.values()):
                    return status
                if cancel_ev and cancel_ev.is_set():
                    return status
                rem = deadline - time.monotonic()
                if rem <= 0.0:
                    return status
                self._stage_ack_cond.wait(timeout=min(0.15, rem))

    def _emit_cue(self, tag: str, label: str, *, show_id: str = "", duration_ms: int = 1200, **extra: Any) -> None:
        evt: dict[str, Any] = {
            "type": "cue",
            "tag": tag,
            "label": label,
            "showId": show_id,
            "durationMs": int(duration_ms),
            "tsMs": int(time.time() * 1000),
            **extra,
        }
        with self._event_lock:
            self.recent_cues.append(evt)
            subs = list(self._event_subs)
        self._fanout_event_locked(evt, subs)

    def load_tts_secrets(self) -> dict[str, str]:
        """Read outside-git studio TTS API keys & model settings from `tts_secrets.json` (plus env fallback)."""
        data: dict[str, str] = {
            "elevenlabsKey": "",
            "geminiKey": "",
            "openaiKey": "",
            "elevenlabsModel": DEFAULT_ELEVENLABS_MODEL,
        }
        if self.tts_secrets_path.is_file():
            try:
                raw = json.loads(self.tts_secrets_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k in ("elevenlabsKey", "geminiKey", "openaiKey", "elevenlabsModel"):
                        v = str(raw.get(k) or "").strip()
                        if v:
                            data[k] = v
            except Exception as exc:
                self.log.warning("Failed reading tts_secrets.json: %s", exc)
        if not data["elevenlabsKey"]:
            data["elevenlabsKey"] = str(os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not data["geminiKey"]:
            data["geminiKey"] = str(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        if not data["openaiKey"]:
            data["openaiKey"] = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        return data

    def update_tts_secrets(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Persist studio API keys securely to `tts_secrets.json` outside git without exposing raw keys."""
        if not isinstance(raw, dict):
            raise ValueError("Invalid TTS provider config")
        with self._lock:
            cur: dict[str, str] = {}
            if self.tts_secrets_path.is_file():
                try:
                    loaded = json.loads(self.tts_secrets_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        cur = {str(k): str(v or "") for k, v in loaded.items()}
                except Exception:
                    cur = {}
            for k in ("elevenlabsKey", "geminiKey", "openaiKey"):
                if raw.get(f"clear_{k}") or raw.get(f"clear{k[0].upper()}{k[1:]}"):
                    cur[k] = ""
                elif k in raw:
                    val = str(raw.get(k) or "").strip()
                    if val and not val.startswith("••••"):
                        cur[k] = val
            if "elevenlabsModel" in raw:
                m = str(raw.get("elevenlabsModel") or "").strip()
                if m in ("eleven_v3", "eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5"):
                    cur["elevenlabsModel"] = m
            tmp = self.tts_secrets_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
            tmp.replace(self.tts_secrets_path)
            self._el_voices_cache = None
        return self.tts_secrets_status()

    @staticmethod
    def _mask_hint(secret: str) -> str:
        s = str(secret or "").strip()
        if not s:
            return ""
        return f"••••{s[-4:]}" if len(s) >= 4 else "••••"

    def tts_secrets_status(self) -> dict[str, Any]:
        sec = self.load_tts_secrets()
        return {
            "hasElevenlabsKey": bool(sec.get("elevenlabsKey")),
            "elevenlabsHint": self._mask_hint(sec.get("elevenlabsKey", "")),
            "elevenlabsModel": sec.get("elevenlabsModel") or DEFAULT_ELEVENLABS_MODEL,
            "hasGeminiKey": bool(sec.get("geminiKey")),
            "geminiHint": self._mask_hint(sec.get("geminiKey", "")),
            "hasOpenaiKey": bool(sec.get("openaiKey")),
            "openaiHint": self._mask_hint(sec.get("openaiKey", "")),
        }

    def fetch_elevenlabs_voices(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Query account's ElevenLabs voices (cached 5 min) so custom/cloned library voices appear in Show Lab."""
        sec = self.load_tts_secrets()
        key = str(sec.get("elevenlabsKey") or "").strip()
        if not key or (not self.allow_network and self._http_get is None):
            return []
        now = time.monotonic()
        if not force and self._el_voices_cache is not None and (now - self._el_voices_cache[0]) < 300.0:
            return list(self._el_voices_cache[1])
        url = "https://api.elevenlabs.io/v1/voices"
        try:
            if self._http_get is not None:
                code, raw = self._http_get(url, 6.0)
            else:
                req = urllib.request.Request(url, headers={"xi-api-key": key}, method="GET")
                with urllib.request.urlopen(req, timeout=6.0) as resp:
                    code, raw = int(resp.status), resp.read()
            if code >= 400:
                return []
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            out: list[dict[str, Any]] = []
            for v in payload.get("voices") or []:
                vid = str(v.get("voice_id") or "").strip()
                name = str(v.get("name") or vid).strip()
                cat = str(v.get("category") or "").strip()
                if vid:
                    out.append({
                        "id": f"elevenlabs:{vid}",
                        "label": f"{name} (ElevenLabs · {cat or 'Account'})",
                        "engine": "elevenlabs",
                        "requiresKey": True,
                        "available": True,
                    })
            self._el_voices_cache = (now, out)
            return out
        except Exception:
            return []

    def list_voices(self) -> dict[str, Any]:
        sec_status = self.tts_secrets_status()
        kokoro_ok = kokoro_models_available(self.models_dir)
        avail_by_engine = {
            "elevenlabs": bool(sec_status["hasElevenlabsKey"]),
            "gemini": bool(sec_status["hasGeminiKey"]),
            "openai": bool(sec_status["hasOpenaiKey"]),
            "kokoro": kokoro_ok,
            "edge": True,
        }
        voices: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        # Append account-specific / cloned ElevenLabs voices first when key is active
        if avail_by_engine["elevenlabs"]:
            for item in self.fetch_elevenlabs_voices():
                if item["id"] not in seen_ids:
                    voices.append(item)
                    seen_ids.add(item["id"])
        for vid, meta in EPIC_VOICE_PRESETS.items():
            if vid in seen_ids:
                continue
            eng = str(meta["engine"])
            voices.append({
                "id": vid,
                "label": meta["label"],
                "engine": eng,
                "requiresKey": eng in ("elevenlabs", "gemini", "openai"),
                "available": avail_by_engine.get(eng, True),
            })
            seen_ids.add(vid)
        return {
            "ok": True,
            "defaultVoice": DEFAULT_ANNOUNCER_VOICE,
            "kokoroReady": kokoro_ok,
            "ttsProviders": sec_status,
            "voices": voices,
        }

    # ---------- Persistence & Configuration ----------

    def _load_or_init(self) -> None:
        defaults = default_shows()
        default_map = {s.id: s for s in defaults}
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
                            if "matt_pr" in raw_shows and "pr" not in raw_shows and isinstance(raw_shows["matt_pr"], dict):
                                legacy_pr = dict(raw_shows.pop("matt_pr"))
                                legacy_pr["id"] = "pr"
                                legacy_pr["name"] = "PR"
                                cleaned_trigs = [
                                    t for t in (legacy_pr.get("triggers") or [])
                                    if "matt" not in str(t).lower()
                                ]
                                for base_t in default_map["pr"].triggers:
                                    if base_t not in cleaned_trigs:
                                        cleaned_trigs.append(base_t)
                                legacy_pr["triggers"] = cleaned_trigs
                                src_f = str(legacy_pr.get("sourceFile") or "").strip()
                                if src_f.startswith("matt_pr_"):
                                    new_src_f = "pr_" + src_f[len("matt_pr_"):]
                                    old_src_p = self.sources_dir / src_f
                                    new_src_p = self.sources_dir / new_src_f
                                    try:
                                        if old_src_p.is_file() and not new_src_p.exists():
                                            old_src_p.rename(new_src_p)
                                    except Exception:
                                        pass
                                    if new_src_p.is_file() or not old_src_p.exists():
                                        legacy_pr["sourceFile"] = new_src_f
                                old_prev = self.rendered_dir / "matt_pr_preview.wav"
                                new_prev = self.rendered_dir / "pr_preview.wav"
                                try:
                                    if old_prev.is_file() and not new_prev.exists():
                                        old_prev.rename(new_prev)
                                except Exception:
                                    pass
                                raw_shows = {"pr": legacy_pr, **raw_shows}
                                dirty = True
                            if any(sid not in raw_shows for sid in default_map):
                                dirty = True
                            for sid, sraw in raw_shows.items():
                                if isinstance(sraw, dict):
                                    s_merged = {"id": sid, **sraw}
                                    # Targeted migration ONLY on ryder_walkin Dev Drop
                                    if sid == "ryder_walkin" and (
                                        str(sraw.get("announcerVoice") or "") in BANNED_ROBOTIC_VOICES
                                        or not str(sraw.get("overlayText") or "").strip()
                                    ):
                                        d_ryder = default_map["ryder_walkin"].to_dict()
                                        for k in (
                                            "name",
                                            "announcerVoice",
                                            "announcerText",
                                            "isDevDrop",
                                            "fogBurstSec",
                                            "lightningFlashSec",
                                            "lightsPulseDurationSec",
                                            "lightsPulseIntervalSec",
                                            "lightsPulseOnSec",
                                            "overlayAtSec",
                                            "overlayText",
                                            "postOverlayDenonVol",
                                            "videoHoldSec",
                                            "voiceReverbWet",
                                            "voiceBoostDb",
                                        ):
                                            s_merged[k] = d_ryder[k]
                                        dirty = True
                                    # Migrate legacy Beantown placeholder on jared_mark_walkin cleanly
                                    if sid == "jared_mark_walkin" and "beantown" in json.dumps(sraw).lower():
                                        s_merged = default_map["jared_mark_walkin"].to_dict()
                                        dirty = True
                                    if (
                                        sid in default_map
                                        and not str(s_merged.get("theaterScene") or "").strip()
                                        and default_map[sid].theater_scene
                                    ):
                                        s_merged["theaterScene"] = default_map[sid].theater_scene
                                        dirty = True
                                    spec = ShowSpec.from_dict(s_merged)
                                    self.shows[spec.id] = spec
                                    if spec.id not in self._show_order:
                                        self._show_order.append(spec.id)
                except Exception as exc:
                    self.log.warning("Failed parsing shows.json (%s); using defaults", exc)
                    dirty = True
            if dirty:
                self._save_locked()
            ry = self.shows.get("ryder_walkin")
            if ry and not ry.voice_style:
                ry = ShowSpec.from_dict({**ry.to_dict(), "voiceStyle": DEFAULT_RYDER_VOICE_STYLE})
                self.shows["ryder_walkin"] = ry
                self._save_locked()
            auto_videos = (
                ("ryder_walkin", "ryder_walkin_video.mp4"),
                ("jenna_walkin", "jenna_walkin_video.mp4"),
                ("avenleigh_walkin", "avenleigh_walkin_video.mp4"),
                ("jared_mark_walkin", "jared_mark_prism_video.mp4"),
                ("jared_mark_vip", "jared_mark_vip_video.mp4"),
            )
            av_dirty = False
            for av_sid, av_file in auto_videos:
                cur_sp = self.shows.get(av_sid)
                av_path = self.sources_dir / av_file
                if cur_sp and not cur_sp.video_source_file and av_path.is_file() and av_path.stat().st_size > 1000:
                    self.shows[av_sid] = ShowSpec.from_dict({**cur_sp.to_dict(), "videoSourceFile": av_file})
                    av_dirty = True
            if av_dirty:
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

    def overlay_wav_path(self, show_id: str) -> Path:
        sid = _safe_show_id(show_id)
        return self.rendered_dir / f"{sid}_overlay.wav"

    def show_mp4_path(self, show_id: str) -> Path:
        sid = _safe_show_id(show_id)
        return self.rendered_dir / f"{sid}_show.mp4"

    def show_meta_path(self, show_id: str) -> Path:
        sid = _safe_show_id(show_id)
        return self.rendered_dir / f"{sid}_show.meta.json"

    def _read_show_meta(self, show_id: str) -> dict[str, Any]:
        mp = self.show_meta_path(show_id)
        if not mp.is_file():
            return {}
        try:
            raw = json.loads(mp.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _resolve_stage_base_url(self) -> str:
        if self._stage_base_url_fn is not None:
            try:
                raw = str(self._stage_base_url_fn() or "").strip().rstrip("/")
                if raw:
                    if raw.endswith("/wallboard"):
                        raw = raw[:-10]
                    return raw
            except Exception:
                pass
        return "http://127.0.0.1:8766"

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
                o_wav = self.overlay_wav_path(sid)
                o_ready = o_wav.is_file() and o_wav.stat().st_size > 100
                o_dur = wav_duration_sec(o_wav) if o_ready else 0.0
                s_mp4 = self.show_mp4_path(sid)
                s_ready = s_mp4.is_file() and s_mp4.stat().st_size > 1000
                meta = self._read_show_meta(sid) if s_ready else {}
                items.append({
                    **spec.to_dict(),
                    "previewReady": ready,
                    "previewDurationSec": dur,
                    "previewUrl": f"/api/shows/{sid}/preview.wav",
                    "overlayReady": o_ready,
                    "overlayDurationSec": o_dur,
                    "overlayUrl": f"/api/shows/{sid}/overlay.wav" if spec.overlay_text else "",
                    "sourceUploaded": bool(spec.source_file and (self.sources_dir / spec.source_file).is_file()),
                    "showVideoReady": s_ready,
                    "showVideoUrl": f"/api/shows/{sid}/show.mp4" if s_ready else "",
                    "showVideoDurationSec": float(meta.get("totalDurationSec") or 0.0),
                    "introDurationSec": float(meta.get("introDurationSec") or dur),
                    "hasSourceVideo": bool(meta.get("hasSourceVideo", False)),
                    "speechCueTimings": list(meta.get("speechCueTimings") or []),
                    "videoUploaded": bool(
                        spec.video_source_file and (self.sources_dir / spec.video_source_file).is_file()
                    ),
                })
            with self._event_lock:
                cues = list(self.recent_cues)[-12:]
                stage_roles = dict(self._stage_roles)
                stage_state = dict(self._stage_state)
            v_info = self.list_voices()
            return {
                "shows": items,
                "fxHooks": self.fx_hooks.to_dict(),
                "active": dict(self._active_state),
                "lastResult": dict(self._last_result),
                "kokoroReady": v_info["kokoroReady"],
                "voices": v_info["voices"],
                "ttsProviders": v_info["ttsProviders"],
                "recentCues": cues,
                "stageClients": stage_roles,
                "stageState": stage_state,
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
            if patch and patch.get("clearVideoSource"):
                old_vid = cur.video_source_file
                merged["videoSourceFile"] = ""
                if old_vid:
                    other_vrefs = any(
                        s.id != sid and s.video_source_file == old_vid for s in self.shows.values()
                    )
                    if not other_vrefs:
                        try:
                            (self.sources_dir / old_vid).unlink(missing_ok=True)
                        except Exception:
                            pass
            if patch and patch.get("clearCustomIntroVoice"):
                old_ci = cur.custom_intro_voice_file
                merged["customIntroVoiceFile"] = ""
                if old_ci:
                    try:
                        (self.sources_dir / old_ci).unlink(missing_ok=True)
                    except Exception:
                        pass
            if patch and patch.get("clearCustomOverlayVoice"):
                old_co = cur.custom_overlay_voice_file
                merged["customOverlayVoiceFile"] = ""
                if old_co:
                    try:
                        (self.sources_dir / old_co).unlink(missing_ok=True)
                    except Exception:
                        pass
            if patch and isinstance(patch.get("triggersText"), str):
                merged["triggers"] = [
                    line.strip()
                    for line in re.split(r"[\r\n,]+", patch["triggersText"])
                    if line.strip()
                ]
            if patch and "speechCues" not in patch and any(
                k in patch for k in ("overlayText", "overlayAtSec", "postOverlayDenonVol")
            ):
                merged.pop("speechCues", None)
            updated = ShowSpec.from_dict(merged)
            self.shows[sid] = updated
            self._save_locked()

        build_res = self.build_show_preview(sid, force=True) if rebuild else {"ok": True}
        if rebuild and updated.three_screen_video and self.ffmpeg_bin:
            try:
                build_res["multimedia"] = self.build_show_multimedia(sid, force=True)
            except Exception as exc:
                build_res["multimedia"] = {"ok": False, "error": str(exc)}
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

    def save_uploaded_video(self, show_id: str, raw_filename: str, file_bytes: bytes) -> dict[str, Any]:
        sid = _safe_show_id(show_id)
        if not self.get_show(sid):
            raise KeyError(f"Unknown show: {show_id}")
        if not file_bytes or len(file_bytes) > MAX_VIDEO_UPLOAD_BYTES:
            raise ValueError("Video upload must be non-empty and <= 250 MB")
        safe_name = sanitize_video_upload_filename(raw_filename, sid)
        dest = self.sources_dir / safe_name
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(file_bytes)
        tmp.replace(dest)
        return self.update_show(sid, {"videoSourceFile": safe_name}, rebuild=True)

    def save_uploaded_voice_clip(
        self,
        show_id: str,
        slot: str,
        raw_filename: str,
        file_bytes: bytes,
    ) -> dict[str, Any]:
        """Save user-supplied studio/recorded intro or 1:20 overlay clip cleanly overriding TTS."""
        sid = _safe_show_id(show_id)
        if not self.get_show(sid):
            raise KeyError(f"Unknown show: {show_id}")
        clean_slot = str(slot or "intro").strip().lower()
        if clean_slot not in ("intro", "overlay"):
            raise ValueError("Voice slot must be 'intro' or 'overlay'")
        if not file_bytes or len(file_bytes) > MAX_SONG_UPLOAD_BYTES:
            raise ValueError("Voice upload must be non-empty and <= 30 MB")
        safe_name = sanitize_upload_filename(raw_filename, f"{sid}_{clean_slot}voice")
        dest = self.sources_dir / safe_name
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(file_bytes)
        tmp.replace(dest)
        field = "customIntroVoiceFile" if clean_slot == "intro" else "customOverlayVoiceFile"
        return self.update_show(sid, {field: safe_name}, rebuild=True)

    def fetch_youtube_video_source(self, show_id: str) -> dict[str, Any]:
        """Download configured YouTube entrance video locally via yt-dlp (no anti-bot bypass)."""
        sid = _safe_show_id(show_id)
        spec = self.get_show(sid)
        if not spec:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        if not spec.youtube_url:
            return {"ok": False, "error": "No youtubeUrl configured on show"}
        if not self.allow_network:
            return {"ok": False, "error": "Network disabled"}
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            return {"ok": False, "error": "yt-dlp not installed on server (install with pip install --user yt-dlp)"}

        target_name = f"{sid}_video.mp4"
        out_tpl = str(self.sources_dir / f"{sid}_video.%(ext)s")
        ydl_opts: dict[str, Any] = {
            "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]",
            "merge_output_format": "mp4",
            "outtmpl": out_tpl,
            "overwrites": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        if self.ffmpeg_bin:
            ydl_opts["ffmpeg_location"] = str(Path(self.ffmpeg_bin).parent)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(spec.youtube_url, download=True)
        except Exception as exc:
            return {"ok": False, "error": f"YouTube download blocked or failed: {exc}"}

        dest = self.sources_dir / target_name
        if not dest.is_file() or dest.stat().st_size < 1000:
            return {"ok": False, "error": "Downloaded MP4 file missing or empty"}
        res = self.update_show(sid, {"videoSourceFile": target_name}, rebuild=True)
        return {**res, "videoSourceFile": target_name}

    def _mix_dj_seamless_audio(
        self,
        spec: ShowSpec,
        ent_pcm: np.ndarray,
        *,
        sr: int = STAGE_VIDEO_SR,
    ) -> tuple[np.ndarray, float]:
        """Mix live studio announcer + smooth stadium roar cleanly over a DJ buildup -> beat-drop slam."""
        if ent_pcm.shape[0] == 0:
            return ent_pcm.copy(), 0.0
        total_n = ent_pcm.shape[0]
        total_sec = float(total_n) / float(sr)
        max_drop = max(0.8, total_sec - 0.35)
        drop_s = min(_clamp(float(spec.dj_drop_sec or 3.8), 0.8, 20.0), max_drop)
        drop_n = max(int(round(0.4 * sr)), min(total_n - 1, int(round(drop_s * sr))))

        ann_44k = self._get_announcer_stem(spec, suffix="announcer")
        ann_pcm = resample_stereo_pcm(ann_44k, SHOW_SR, sr) if (ann_44k is not None and ann_44k.shape[0] > 0) else np.zeros((0, 2), dtype=np.float32)
        if ann_pcm.shape[0] > 0:
            ann_pcm = trim_edge_silence_stereo(ann_pcm, sr=sr, thresh=0.003, pad_lead_ms=10.0, pad_tail_ms=85.0)
        ann_off = 0
        if ann_pcm.shape[0] > 0:
            lead_before_drop = 0.42 if spec.add_thunder else 0.06
            target_end = max(int(round(0.25 * sr)), drop_n - int(round(lead_before_drop * sr)))
            ann_off = max(int(round(0.08 * sr)), target_end - ann_pcm.shape[0])

        # DJ fader curve: for storm/thunder intros keep full storm loudness until Atlas speaks,
        # ducking -14 dB (0.20) only beneath speech, then opening smoothly into the pre-drop snare pickup
        fader = np.ones(total_n, dtype=np.float32)
        slam_n = min(drop_n // 2, max(16, int(round((0.46 if spec.add_thunder else 0.14) * sr))))
        hold_end = max(0, drop_n - slam_n)
        if spec.add_thunder and ann_pcm.shape[0] > 0:
            duck_gain = 0.20
            ramp_n = min(int(round(0.22 * sr)), max(16, ann_off))
            duck_start = max(0, ann_off - ramp_n)
            if ann_off > duck_start:
                u_dn = np.linspace(0.0, 1.0, ann_off - duck_start, dtype=np.float32)
                fader[duck_start:ann_off] = 1.0 - (1.0 - duck_gain) * 0.5 * (1.0 - np.cos(math.pi * u_dn))
            if hold_end > ann_off:
                fader[ann_off:hold_end] = duck_gain
            if drop_n > hold_end:
                u = np.linspace(0.0, 1.0, drop_n - hold_end, dtype=np.float32)
                fader[hold_end:drop_n] = duck_gain + (1.0 - duck_gain) * 0.5 * (1.0 - np.cos(math.pi * u))
        else:
            fader[:hold_end] = 0.28
            click_n = min(hold_end, int(round(0.020 * sr)))
            if click_n > 2:
                fader[:click_n] = np.linspace(0.0, 0.28, click_n, dtype=np.float32)
            if drop_n > hold_end:
                u = np.linspace(0.0, 1.0, drop_n - hold_end, dtype=np.float32)
                fader[hold_end:drop_n] = 0.28 + 0.72 * 0.5 * (1.0 - np.cos(math.pi * u))
        mixed = (ent_pcm * fader[:, None]).astype(np.float32)

        # Steady stadium bed under speech (zero choppy word-pumping) -> celebratory roar on drop -> silky fade
        if spec.add_cheer:
            c_len = min(total_sec, drop_s + 2.5)
            cheer = load_studio_fx_stereo(
                self.fx_dir,
                "stadium_crowd.wav",
                c_len,
                allow_default_fallback=self.allow_network,
                swell_sec=0.25,
                fade_out_sec=1.9,
                sr=sr,
            )
            if cheer is None:
                cheer = synth_crowd_cheer_stereo(c_len, sr=sr, swell_sec=0.25, fade_out_sec=1.9)
            c_n = min(cheer.shape[0], total_n)
            if c_n > 16:
                c_env = np.zeros(c_n, dtype=np.float32)
                fi_n = min(c_n, int(round(0.18 * sr)))
                sw_s = max(fi_n, min(c_n, drop_n - int(round(0.16 * sr))))
                sw_e = max(sw_s + 1, min(c_n, drop_n + int(round(0.12 * sr))))
                if spec.add_thunder:
                    # Keep stadium silent during dark storm/voice; swell cleanly right into the beat drop
                    if sw_e > sw_s:
                        u_sw = np.linspace(0.0, 1.0, sw_e - sw_s, dtype=np.float32)
                        c_env[sw_s:sw_e] = 0.42 * 0.5 * (1.0 - np.cos(math.pi * u_sw))
                else:
                    if fi_n > 1:
                        c_env[:fi_n] = np.linspace(0.0, 0.15, fi_n, dtype=np.float32)
                    if sw_s > fi_n:
                        c_env[fi_n:sw_s] = 0.15
                    if sw_e > sw_s:
                        u_sw = np.linspace(0.0, 1.0, sw_e - sw_s, dtype=np.float32)
                        c_env[sw_s:sw_e] = 0.15 + 0.27 * 0.5 * (1.0 - np.cos(math.pi * u_sw))
                if c_n > sw_e:
                    u_dc = np.linspace(0.0, 1.0, c_n - sw_e, dtype=np.float32)
                    c_env[sw_e:] = 0.42 * (np.cos(0.5 * math.pi * u_dc) ** 2)
                mix_into(mixed, cheer[:c_n] * c_env[:, None], 0, gain=1.0)

        if ann_pcm.shape[0] > 0:
            ann_gain = 1.0
            song_tail = ent_pcm[drop_n:]
            if song_tail.shape[0] > sr // 2:
                song_rms = float(np.sqrt(np.mean(song_tail ** 2)))
                ann_rms = float(np.sqrt(np.mean(ann_pcm ** 2)))
                if song_rms > 0.01 and ann_rms > song_rms * 1.15:
                    ann_gain = max(0.55, min(1.0, (song_rms * 1.15) / ann_rms))
            mix_into(mixed, ann_pcm, ann_off, gain=ann_gain)

        # Transparent true-peak soft-knee limiter (<= -1.1 dBFS ceiling so AAC never clips or pulls down song loudness)
        pk = float(np.max(np.abs(mixed))) if mixed.size else 0.0
        if pk > 0.78:
            knee = 0.76
            ceil = 0.88
            head = ceil - knee
            mag = np.abs(mixed)
            over = np.maximum(0.0, mag - knee)
            mixed = np.where(mag > knee, np.sign(mixed) * (knee + head * np.tanh(over / head)), mixed).astype(np.float32)
        return mixed, round(drop_s, 3)

    def _build_norm_source_mezzanine(self, src_vid: Path, stamp: str) -> tuple[Path, bool]:
        """Cache a clean 1080p H.264 Main L4.0 30fps (-bf 0) mezzanine once per source video."""
        vid_key = hashlib.sha1(
            f"v1|{src_vid.name}|{src_vid.stat().st_size}|{int(src_vid.stat().st_mtime_ns)}".encode("utf-8")
        ).hexdigest()[:12]
        norm_src_mp4 = self.rendered_dir / f".norm_{vid_key}.mp4"
        if norm_src_mp4.is_file() and norm_src_mp4.stat().st_size > 1000:
            return norm_src_mp4, True
        tmp_norm = self.rendered_dir / f".norm_{vid_key}_{stamp}.tmp.mp4"
        try:
            n_proc = subprocess.run(
                [
                    self.ffmpeg_bin,
                    "-y",
                    "-i",
                    str(src_vid),
                    "-an",
                    "-vf",
                    (
                        "scale=1920:1080:force_original_aspect_ratio=decrease,"
                        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
                    ),
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "main",
                    "-level:v",
                    "4.0",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    "30",
                    "-g",
                    "60",
                    "-bf",
                    "0",
                    "-video_track_timescale",
                    "90000",
                    "-crf",
                    "21",
                    "-preset",
                    "veryfast",
                    str(tmp_norm),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=300,
                **_no_window_kwargs(),
            )
            if n_proc.returncode == 0 and tmp_norm.is_file() and tmp_norm.stat().st_size > 1000:
                tmp_norm.replace(norm_src_mp4)
                return norm_src_mp4, True
        finally:
            try:
                tmp_norm.unlink(missing_ok=True)
            except Exception:
                pass
        return norm_src_mp4, False

    def _extract_source_video_audio(self, src_vid: Path, tmp_wav: Path, *, target_sr: int, max_sec: float | None = None) -> tuple[np.ndarray, float]:
        """Decode source MP4 soundtrack cleanly to stereo float32 PCM + probed duration."""
        cmd = [self.ffmpeg_bin, "-y", "-i", str(src_vid)]
        if max_sec is not None and max_sec > 0.2:
            cmd += ["-t", f"{float(max_sec):.2f}"]
        cmd += ["-vn", "-ar", str(int(target_sr)), "-ac", "2", "-c:a", "pcm_s16le", str(tmp_wav)]
        ext_proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60, **_no_window_kwargs())
        src_vid_dur = _probe_media_duration_sec(src_vid, self.ffmpeg_bin)
        if ext_proc.returncode == 0 and tmp_wav.is_file() and tmp_wav.stat().st_size > 100:
            ent_pcm = read_wav_stereo_float(tmp_wav, target_sr=target_sr)
        else:
            fallback_dur = min(max_sec, src_vid_dur) if max_sec else src_vid_dur
            ent_pcm = np.zeros((max(int(target_sr), int(round(max(1.0, fallback_dur) * target_sr))), 2), dtype=np.float32)

        dur = src_vid_dur if src_vid_dur > 0.2 else (float(ent_pcm.shape[0]) / float(target_sr))
        if max_sec is not None and max_sec > 0.2:
            dur = min(dur, float(max_sec))
        target_samples = max(1, int(round(dur * target_sr)))
        if ent_pcm.shape[0] < target_samples:
            ent_pcm = np.concatenate([ent_pcm, np.zeros((target_samples - ent_pcm.shape[0], 2), dtype=np.float32)], axis=0)
        else:
            ent_pcm = ent_pcm[:target_samples]

        cur_rms = float(np.sqrt(np.mean(ent_pcm ** 2))) if ent_pcm.size else 0.0
        cur_peak = float(np.max(np.abs(ent_pcm))) if ent_pcm.size else 0.0
        if cur_rms > 1e-5 and cur_peak > 1e-5:
            scale = min(0.22 / cur_rms, 0.88 / cur_peak)
            ent_pcm = np.tanh(ent_pcm * scale * 1.05).astype(np.float32) * 0.92
        return ent_pcm.astype(np.float32), dur

    def build_show_multimedia(self, show_id: str, *, force: bool = False) -> dict[str, Any]:
        """Assemble self-contained 1080p H.264+AAC MP4 combining intro pulses/voice (or seamless DJ buildup) + entrance video."""
        sid = _safe_show_id(show_id)
        spec = self.get_show(sid)
        if spec is None:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        if not self.ffmpeg_bin:
            return {"ok": False, "error": "FFmpeg binary not configured"}

        with self._multimedia_build_lock:
            p_res = self.build_show_preview(sid, force=force)
            if not p_res.get("ok"):
                return {"ok": False, "error": p_res.get("error") or "Preview build failed"}
            if spec.overlay_text:
                o_res = self.build_show_overlay(sid, force=force)
                if not o_res.get("ok"):
                    return {"ok": False, "error": o_res.get("error") or "Overlay build failed"}

            p_wav = self.preview_wav_path(sid)
            o_wav = self.overlay_wav_path(sid)
            src_vid = (self.sources_dir / spec.video_source_file) if spec.video_source_file else None
            has_src_vid = bool(src_vid and src_vid.is_file() and src_vid.stat().st_size > 1000)

            sig_parts = [
                "v3",
                f"p={p_wav.stat().st_size}:{int(p_wav.stat().st_mtime_ns)}" if p_wav.is_file() else "p=0",
                f"o={o_wav.stat().st_size}:{int(o_wav.stat().st_mtime_ns)}" if o_wav.is_file() else "o=0",
                f"vid={src_vid.name}:{src_vid.stat().st_size}:{int(src_vid.stat().st_mtime_ns)}" if has_src_vid else "vid=none",
                f"l={spec.lightning_flash_sec:.2f}",
                f"is={spec.intro_start_sec:.2f}",
                f"fc={spec.lights_flash_count}",
                f"ts={spec.theater_scene}",
                f"th={int(bool(spec.add_thunder))}",
                f"dj={int(bool(spec.dj_seamless_intro))}:{spec.dj_drop_sec:.2f}",
                f"ov={spec.overlay_at_sec:.2f}:{spec.overlay_text}",
                f"sc={json.dumps(spec.speech_cues, sort_keys=True)}",
            ]
            sig = hashlib.sha1("|".join(sig_parts).encode("utf-8")).hexdigest()[:16]

            out_mp4 = self.show_mp4_path(sid)
            meta_path = self.show_meta_path(sid)
            existing_meta = self._read_show_meta(sid)
            if (
                not force
                and out_mp4.is_file()
                and out_mp4.stat().st_size > 1000
                and existing_meta.get("sig") == sig
            ):
                return {"ok": True, "cached": True, **existing_meta}

            stamp = f"{threading.get_ident()}_{int(time.time() * 1000)}"
            intro_mp4 = self.rendered_dir / f".intro_{sid}_{stamp}.mp4"
            src_audio_wav = self.rendered_dir / f".srcaud_{sid}_{stamp}.wav"
            master_wav = self.rendered_dir / f".master_{sid}_{stamp}.wav"
            concat_txt = self.rendered_dir / f".concat_{sid}_{stamp}.txt"
            tmp_mp4 = out_mp4.with_name(f"{out_mp4.stem}.tmp{out_mp4.suffix}")

            try:
                use_seamless = bool(spec.dj_seamless_intro and has_src_vid and src_vid is not None)
                src_dur = 0.0
                overlay_dur = 0.0
                speech_cue_timings: list[dict[str, Any]] = []

                if use_seamless and src_vid is not None:
                    ent_pcm, src_dur = self._extract_source_video_audio(src_vid, src_audio_wav, target_sr=STAGE_VIDEO_SR)
                    ent_pcm, drop_s = self._mix_dj_seamless_audio(spec, ent_pcm, sr=STAGE_VIDEO_SR)
                    intro_video_dur = drop_s
                    cue_show_offset = 0.0
                    intro_pcm_48k = np.zeros((0, 2), dtype=np.float32)
                else:
                    intro_pcm_44k = read_wav_stereo_float(p_wav, target_sr=SHOW_SR)
                    intro_pcm_48k = resample_stereo_pcm(intro_pcm_44k, SHOW_SR, STAGE_VIDEO_SR)
                    raw_intro_dur = float(intro_pcm_48k.shape[0]) / float(STAGE_VIDEO_SR)

                    intro_video_dur = _generate_intro_pulse_video(
                        intro_mp4,
                        duration_sec=max(1.5, raw_intro_dur),
                        lightning_sec=spec.lightning_flash_sec,
                        flash_count=spec.lights_flash_count,
                        tint_rgb=_intro_tint_for_scene(spec.theater_scene),
                        ffmpeg_bin=self.ffmpeg_bin,
                    )
                    intro_target_samples = max(1, int(round(intro_video_dur * STAGE_VIDEO_SR)))
                    if intro_pcm_48k.shape[0] < intro_target_samples:
                        pad_n = intro_target_samples - intro_pcm_48k.shape[0]
                        intro_pcm_48k = np.concatenate(
                            [intro_pcm_48k, np.zeros((pad_n, 2), dtype=np.float32)], axis=0
                        )
                    else:
                        intro_pcm_48k = intro_pcm_48k[:intro_target_samples]
                    cue_show_offset = intro_video_dur
                    ent_pcm = np.zeros((0, 2), dtype=np.float32)

                if has_src_vid and src_vid is not None:
                    if not use_seamless:
                        ent_pcm, src_dur = self._extract_source_video_audio(src_vid, src_audio_wav, target_sr=STAGE_VIDEO_SR)

                    # Bake all timed video speech cues (duck soundtrack + mix voice cleanly at exact timestamps)
                    vid_cues = sorted(
                        (c for c in spec.speech_cues if c.get("target") == "video" and c.get("text")),
                        key=lambda c: float(c.get("atSec") or 0.0),
                    )
                    mixed_any_vid_cue = False
                    for v_idx, cue in enumerate(vid_cues):
                        if v_idx == 0 and spec.custom_overlay_voice_file:
                            v_pcm_44k = self._get_announcer_stem(
                                spec,
                                suffix="overlay",
                                text_override=cue["text"],
                                reverb_override=min(spec.voice_reverb_wet, 0.03),
                                voice_override=str(cue.get("voice") or ""),
                            )
                        else:
                            v_pcm_44k = self._get_announcer_stem(
                                spec,
                                suffix=f"vcue_{v_idx}",
                                text_override=cue["text"],
                                reverb_override=min(spec.voice_reverb_wet, 0.03),
                                voice_override=str(cue.get("voice") or ""),
                            )
                        if v_pcm_44k is None or v_pcm_44k.shape[0] == 0:
                            continue
                        ov_48k = resample_stereo_pcm(v_pcm_44k, SHOW_SR, STAGE_VIDEO_SR)
                        cue_dur = round(float(ov_48k.shape[0]) / float(STAGE_VIDEO_SR), 2)
                        if v_idx == 0:
                            overlay_dur = cue_dur
                        cue_at = float(cue.get("atSec") or 0.0)
                        if src_dur > (cue_at + 0.25):
                            ent_pcm = apply_timed_duck_stereo(
                                ent_pcm,
                                sr=STAGE_VIDEO_SR,
                                duck_start_sec=cue_at,
                                duck_hold_sec=max(0.4, cue_dur),
                                duck_gain=float(cue.get("duckGain") or 0.22),
                                attack_sec=0.25,
                                release_sec=0.45,
                            )
                            ov_off = int(round(cue_at * STAGE_VIDEO_SR))
                            mix_into(ent_pcm, ov_48k, ov_off, gain=1.0)
                            mixed_any_vid_cue = True
                            speech_cue_timings.append({
                                "id": str(cue.get("id") or f"sc_{v_idx + 1}"),
                                "target": "video",
                                "atSec": round(cue_at, 2),
                                "showAtSec": round(cue_show_offset + cue_at, 2),
                                "durSec": cue_dur,
                                "text": str(cue["text"]),
                                "postDenonVol": int(cue.get("postDenonVol") or 0),
                            })
                    if mixed_any_vid_cue:
                        pk_ent = float(np.max(np.abs(ent_pcm)))
                        if pk_ent > 0.94:
                            ent_pcm = ent_pcm * (0.92 / pk_ent)

                    master_pcm = ent_pcm if use_seamless else np.concatenate([intro_pcm_48k, ent_pcm], axis=0)
                    master_wav.write_bytes(encode_stereo_pcm16_wav(master_pcm, sr=STAGE_VIDEO_SR))

                    norm_src_mp4, norm_ready = self._build_norm_source_mezzanine(src_vid, stamp)

                    mux_ok = False
                    if norm_ready:
                        if use_seamless:
                            fast_cmd = [
                                self.ffmpeg_bin,
                                "-y",
                                "-i",
                                str(norm_src_mp4),
                                "-i",
                                str(master_wav),
                                "-map",
                                "0:v",
                                "-map",
                                "1:a",
                                "-c:v",
                                "copy",
                                "-c:a",
                                "aac",
                                "-b:a",
                                "192k",
                                "-ar",
                                str(STAGE_VIDEO_SR),
                                "-ac",
                                "2",
                                "-movflags",
                                "+faststart",
                                "-shortest",
                                str(tmp_mp4),
                            ]
                        else:
                            p_intro_s = intro_mp4.resolve().as_posix().replace("'", r"\'")
                            p_norm_s = norm_src_mp4.resolve().as_posix().replace("'", r"\'")
                            concat_txt.write_text(f"file '{p_intro_s}'\nfile '{p_norm_s}'\n", encoding="utf-8")
                            fast_cmd = [
                                self.ffmpeg_bin,
                                "-y",
                                "-f",
                                "concat",
                                "-safe",
                                "0",
                                "-i",
                                str(concat_txt),
                                "-i",
                                str(master_wav),
                                "-map",
                                "0:v",
                                "-map",
                                "1:a",
                                "-c:v",
                                "copy",
                                "-c:a",
                                "aac",
                                "-b:a",
                                "192k",
                                "-ar",
                                str(STAGE_VIDEO_SR),
                                "-ac",
                                "2",
                                "-movflags",
                                "+faststart",
                                "-shortest",
                                str(tmp_mp4),
                            ]
                        mux_proc = subprocess.run(
                            fast_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=120,
                            **_no_window_kwargs(),
                        )
                        mux_ok = mux_proc.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 1000

                    if not mux_ok:
                        if use_seamless:
                            fallback_cmd = [
                                self.ffmpeg_bin,
                                "-y",
                                "-i",
                                str(src_vid),
                                "-i",
                                str(master_wav),
                                "-map",
                                "0:v",
                                "-map",
                                "1:a",
                                "-vf",
                                (
                                    "scale=1920:1080:force_original_aspect_ratio=decrease,"
                                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
                                ),
                                "-c:v",
                                "libx264",
                                "-profile:v",
                                "main",
                                "-level:v",
                                "4.0",
                                "-pix_fmt",
                                "yuv420p",
                                "-r",
                                "30",
                                "-g",
                                "60",
                                "-bf",
                                "0",
                                "-video_track_timescale",
                                "90000",
                                "-crf",
                                "21",
                                "-preset",
                                "veryfast",
                                "-c:a",
                                "aac",
                                "-b:a",
                                "192k",
                                "-ar",
                                str(STAGE_VIDEO_SR),
                                "-ac",
                                "2",
                                "-movflags",
                                "+faststart",
                                "-shortest",
                                str(tmp_mp4),
                            ]
                        else:
                            fallback_cmd = [
                                self.ffmpeg_bin,
                                "-y",
                                "-i",
                                str(intro_mp4),
                                "-i",
                                str(src_vid),
                                "-i",
                                str(master_wav),
                                "-filter_complex",
                                (
                                    "[0:v]setsar=1,setpts=PTS-STARTPTS[v0];"
                                    "[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[v1];"
                                    "[v0][v1]concat=n=2:v=1:a=0[v]"
                                ),
                                "-map",
                                "[v]",
                                "-map",
                                "2:a",
                                "-c:v",
                                "libx264",
                                "-profile:v",
                                "main",
                                "-level:v",
                                "4.0",
                                "-pix_fmt",
                                "yuv420p",
                                "-r",
                                "30",
                                "-g",
                                "60",
                                "-bf",
                                "0",
                                "-video_track_timescale",
                                "90000",
                                "-crf",
                                "21",
                                "-preset",
                                "veryfast",
                                "-c:a",
                                "aac",
                                "-b:a",
                                "192k",
                                "-ar",
                                str(STAGE_VIDEO_SR),
                                "-ac",
                                "2",
                                "-movflags",
                                "+faststart",
                                "-shortest",
                                str(tmp_mp4),
                            ]
                        mux_proc = subprocess.run(
                            fallback_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=300,
                            **_no_window_kwargs(),
                        )
                        if mux_proc.returncode != 0 or not tmp_mp4.is_file() or tmp_mp4.stat().st_size < 1000:
                            err = (mux_proc.stderr or b"").decode("utf-8", errors="ignore")[-320:]
                            raise RuntimeError(f"FFmpeg show MP4 build failed: {err}")
                else:
                    master_wav.write_bytes(encode_stereo_pcm16_wav(intro_pcm_48k, sr=STAGE_VIDEO_SR))
                    cmd = [
                        self.ffmpeg_bin,
                        "-y",
                        "-i",
                        str(intro_mp4),
                        "-i",
                        str(master_wav),
                        "-map",
                        "0:v",
                        "-map",
                        "1:a",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ar",
                        str(STAGE_VIDEO_SR),
                        "-ac",
                        "2",
                        "-movflags",
                        "+faststart",
                        "-shortest",
                        str(tmp_mp4),
                    ]
                    mux_proc = subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=300,
                        **_no_window_kwargs(),
                    )
                    if mux_proc.returncode != 0 or not tmp_mp4.is_file() or tmp_mp4.stat().st_size < 1000:
                        err = (mux_proc.stderr or b"").decode("utf-8", errors="ignore")[-320:]
                        raise RuntimeError(f"FFmpeg show MP4 build failed: {err}")

                tmp_mp4.replace(out_mp4)
                probed_total = _probe_media_duration_sec(out_mp4, self.ffmpeg_bin)
                fallback_total = src_dur if use_seamless else (intro_video_dur + src_dur)
                total_dur = round(probed_total if probed_total > 0.5 else fallback_total, 2)
                meta = {
                    "ok": True,
                    "showId": sid,
                    "sig": sig,
                    "path": str(out_mp4),
                    "introDurationSec": round(intro_video_dur, 2),
                    "sourceDurationSec": round(src_dur, 2),
                    "totalDurationSec": total_dur,
                    "overlayDurationSec": overlay_dur,
                    "hasSourceVideo": has_src_vid,
                    "speechCueTimings": speech_cue_timings,
                    "builtAt": _utc_iso_now(),
                }
                tmp_meta = meta_path.with_name(f"{meta_path.stem}.tmp{meta_path.suffix}")
                tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                tmp_meta.replace(meta_path)
                return meta
            finally:
                for p in (intro_mp4, src_audio_wav, master_wav, concat_txt, tmp_mp4):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass

    def update_fx_hooks(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = {**self.fx_hooks.to_dict(), **(raw or {})}
            self.fx_hooks = SmartFxConfig.from_dict(merged)
            self._save_locked()
            return {"ok": True, "fxHooks": self.fx_hooks.to_dict()}

    # ---------- Composite Preview Audio Builder ----------

    def _get_announcer_stem(
        self,
        spec: ShowSpec,
        *,
        suffix: str = "announcer",
        text_override: str | None = None,
        reverb_override: float | None = None,
        voice_override: str = "",
    ) -> np.ndarray | None:
        custom_name = ""
        if not voice_override:
            if suffix == "overlay":
                custom_name = spec.custom_overlay_voice_file
            elif suffix == "announcer":
                custom_name = spec.custom_intro_voice_file
        if custom_name:
            c_path = self.sources_dir / custom_name
            if c_path.is_file() and c_path.stat().st_size > 100:
                pcm_custom = decode_and_trim_song_file(
                    c_path,
                    start_sec=0.0,
                    duration_sec=60.0,
                    fade_out_sec=0.06,
                    trim_leading_silence=True,
                    ffmpeg_bin=self.ffmpeg_bin,
                )
                if abs(spec.voice_boost_db) > 0.05:
                    pcm_custom = (pcm_custom * (10.0 ** (_clamp(spec.voice_boost_db, -4.0, 6.0) / 20.0))).astype(np.float32)
                pk = float(np.max(np.abs(pcm_custom)))
                if pk > 0.90:
                    pcm_custom = pcm_custom * (0.90 / pk)
                return pcm_custom if pcm_custom.shape[0] > 0 else None

        txt = str(text_override if text_override is not None else spec.announcer_text).strip()
        if not txt:
            return None
        voice_id = resolve_voice_id(voice_override or spec.announcer_voice)
        reverb = spec.voice_reverb_wet if reverb_override is None else float(reverb_override)
        sec = self.load_tts_secrets()
        el_model = str(sec.get("elevenlabsModel") or DEFAULT_ELEVENLABS_MODEL)
        key_src = (
            f"v3|{txt}|{voice_id}|{spec.announcer_rate}|{reverb:.2f}|{spec.voice_boost_db:.1f}|"
            f"{spec.voice_style}|{el_model}|{int(self.allow_network)}|{int(kokoro_models_available(self.models_dir))}"
        )
        digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]
        stem_path = self.rendered_dir / f"{spec.id}_{suffix}_{digest}.wav"
        if not stem_path.is_file() or stem_path.stat().st_size < 100:
            synthesize_announcer_wav(
                txt,
                voice=voice_id,
                rate=spec.announcer_rate,
                out_wav=stem_path,
                ffmpeg_bin=self.ffmpeg_bin,
                allow_network=self.allow_network,
                models_dir=self.models_dir,
                tts_cache_dir=self.root_dir / "tts_cache",
                reverb_wet=reverb,
                boost_db=spec.voice_boost_db,
                voice_style=spec.voice_style,
                tts_secrets=sec,
                http_post=self._http_post,
                gemini_synth_fn=self._gemini_synth_fn,
            )
        pcm = read_wav_stereo_float(stem_path, target_sr=SHOW_SR)
        return pcm if pcm.shape[0] > 0 else None

    def build_show_overlay(self, show_id: str, *, force: bool = False) -> dict[str, Any]:
        """Render standalone mid-video voice overlay WAV (`shows/rendered/<id>_overlay.wav`)."""
        spec = self.get_show(show_id)
        if spec is None:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        if not spec.overlay_text:
            return {"ok": True, "skipped": True}
        out_wav = self.overlay_wav_path(spec.id)
        if not force and out_wav.is_file() and out_wav.stat().st_size > 200:
            return {"ok": True, "cached": True, "path": str(out_wav), "durationSec": wav_duration_sec(out_wav)}
        # Crisp warm broadcast reverb (0.03) so mid-song message cuts cleanly over ducked YouTube
        pcm = self._get_announcer_stem(spec, suffix="overlay", text_override=spec.overlay_text, reverb_override=0.03)
        if pcm is None or pcm.shape[0] == 0:
            pcm = np.zeros((int(0.5 * SHOW_SR), 2), dtype=np.float32)
        wav_bytes = encode_stereo_pcm16_wav(pcm, sr=SHOW_SR)
        tmp = out_wav.with_suffix(".wav.tmp")
        tmp.write_bytes(wav_bytes)
        tmp.replace(out_wav)
        return {"ok": True, "showId": spec.id, "path": str(out_wav), "durationSec": wav_duration_sec(out_wav)}

    def _mix_debut_intro_stereo(
        self,
        ann_pcm: np.ndarray,
        *,
        lightning_flash_sec: float = 0.75,
        add_cheer: bool = True,
        add_thunder: bool = True,
        intro_start_sec: float = 2.3,
        extra_intro_cues: Sequence[tuple[float, np.ndarray]] = (),
    ) -> np.ndarray:
        """Mix main + extra intro speech over optional thunder rumble/crack and stadium crowd cheer."""
        pre_thunder_n = int(round(max(0.0, float(intro_start_sec)) * SHOW_SR))
        post_tail_n = int(round(1.6 * SHOW_SR))
        max_speech_end = pre_thunder_n + ann_pcm.shape[0]
        extra_offsets: list[tuple[int, np.ndarray]] = []
        for at_s, cue_pcm in extra_intro_cues:
            if cue_pcm is not None and cue_pcm.shape[0] > 0:
                off = max(0, int(round(float(at_s) * SHOW_SR)))
                extra_offsets.append((off, cue_pcm))
                max_speech_end = max(max_speech_end, off + cue_pcm.shape[0])
        total_n = max(int(round(3.0 * SHOW_SR)), max_speech_end + post_tail_n)
        total_sec = float(total_n) / float(SHOW_SR)
        speech_bus = np.zeros((total_n, 2), dtype=np.float32)
        mix_into(speech_bus, ann_pcm, pre_thunder_n, gain=1.0)
        for off, cue_pcm in extra_offsets:
            mix_into(speech_bus, cue_pcm, off, gain=1.0)

        if add_thunder:
            crack_sec = lightning_flash_sec or 0.75
            thunder_raw = load_studio_fx_stereo(
                self.fx_dir,
                "thunder_rumble.wav",
                total_sec,
                allow_default_fallback=self.allow_network,
                swell_sec=0.15,
                fade_out_sec=1.1,
                sr=SHOW_SR,
            )
            if thunder_raw is None:
                thunder_raw = synth_thunder_rumble_stereo(
                    total_sec,
                    sr=SHOW_SR,
                    crack_at_sec=crack_sec,
                )
            crack_pcm = load_studio_fx_stereo(
                self.fx_dir,
                "thunder_crack.wav",
                min(4.0, total_sec),
                allow_default_fallback=self.allow_network,
                swell_sec=0.02,
                fade_out_sec=1.2,
                sr=SHOW_SR,
            )
            if crack_pcm is not None:
                crack_off = int(round(crack_sec * SHOW_SR))
                mix_into(thunder_raw, crack_pcm, crack_off, gain=0.82)
            bed = duck_bed_under_speech(
                thunder_raw,
                speech_bus,
                0,
                sr=SHOW_SR,
                duck_gain=0.14,
                base_gain=0.88,
            )
        else:
            bed = np.zeros((total_n, 2), dtype=np.float32)
        if add_cheer:
            cheer_full = np.zeros((total_n, 2), dtype=np.float32)
            c_start = min(
                max(0, pre_thunder_n + int(ann_pcm.shape[0] * 0.52)),
                max(0, total_n - int(1.5 * SHOW_SR)),
            )
            c_len = max(1.5, float(total_n - c_start) / float(SHOW_SR))
            cheer = load_studio_fx_stereo(
                self.fx_dir,
                "stadium_crowd.wav",
                c_len,
                allow_default_fallback=self.allow_network,
                swell_sec=0.8,
                fade_out_sec=1.3,
                sr=SHOW_SR,
            )
            if cheer is None:
                cheer = synth_crowd_cheer_stereo(c_len, sr=SHOW_SR, swell_sec=0.8, fade_out_sec=1.3)
            mix_into(cheer_full, cheer, c_start, gain=0.82)
            bed += duck_bed_under_speech(
                cheer_full,
                speech_bus,
                0,
                sr=SHOW_SR,
                duck_gain=0.16,
                base_gain=0.92,
            )
        mix = bed
        mix_into(mix, speech_bus, 0, gain=1.0)
        mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=min(1.25, float(post_tail_n) / SHOW_SR))
        # Soft true-peak protection after mixing thunder+crowd+speech
        pk_mix = float(np.max(np.abs(mix)))
        if pk_mix > 0.94:
            mix = mix * (0.92 / pk_mix)
        return mix

    def audition_voice(
        self,
        *,
        text: str,
        voice: str = DEFAULT_ANNOUNCER_VOICE,
        voice_style: str = "",
        reverb_wet: float = 0.06,
        boost_db: float = 2.0,
        with_fx: bool = False,
        slot: str = "intro",
        lightning_flash_sec: float = 0.75,
        add_cheer: bool = True,
    ) -> tuple[bytes, float]:
        """Synthesize a stateless audition WAV in memory/temp without touching shows.json or rebuilding MP4s."""
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Script text is empty")
        slot_norm = (slot or "intro").strip().lower()
        reverb = _clamp(float(reverb_wet), 0.0, 0.25)
        use_fx = bool(with_fx)
        if slot_norm == "overlay":
            reverb = min(reverb, 0.03)
            use_fx = False
        boost = _clamp(float(boost_db), -4.0, 6.0)
        resolved_voice = resolve_voice_id(voice)
        secrets = self.load_tts_secrets()

        with tempfile.TemporaryDirectory(prefix="armory_aud_") as tmp_dir:
            tmp_wav = Path(tmp_dir) / "audition.wav"
            synthesize_announcer_wav(
                clean_text,
                voice=resolved_voice,
                out_wav=tmp_wav,
                ffmpeg_bin=self.ffmpeg_bin,
                allow_network=self.allow_network,
                models_dir=self.models_dir,
                tts_cache_dir=self.root_dir / "tts_cache",
                reverb_wet=reverb,
                boost_db=boost,
                voice_style=str(voice_style or "").strip(),
                tts_secrets=secrets,
                http_post=self._http_post,
                gemini_synth_fn=self._gemini_synth_fn,
            )
            if use_fx:
                ann_pcm = read_wav_stereo_float(tmp_wav, target_sr=SHOW_SR)
                if ann_pcm.shape[0] == 0:
                    ann_pcm = np.zeros((int(0.5 * SHOW_SR), 2), dtype=np.float32)
                mix = self._mix_debut_intro_stereo(
                    ann_pcm,
                    lightning_flash_sec=lightning_flash_sec,
                    add_cheer=add_cheer,
                )
                wav_bytes = encode_stereo_pcm16_wav(mix, sr=SHOW_SR)
                dur_sec = round(float(mix.shape[0]) / float(SHOW_SR), 2)
            else:
                wav_bytes = tmp_wav.read_bytes()
                dur_sec = wav_duration_sec(tmp_wav)
        return wav_bytes, dur_sec

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
                fade_out_sec=0.0,
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
                cheer = load_studio_fx_stereo(
                    self.fx_dir,
                    "stadium_crowd.wav",
                    c_len,
                    allow_default_fallback=self.allow_network,
                    swell_sec=min(1.4, c_len * 0.35),
                    fade_out_sec=min(spec.fade_out_sec + 0.3, c_len * 0.6),
                    sr=SHOW_SR,
                )
                if cheer is None:
                    cheer = synth_crowd_cheer_stereo(
                        c_len,
                        sr=SHOW_SR,
                        swell_sec=min(1.4, c_len * 0.35),
                        fade_out_sec=min(spec.fade_out_sec + 0.3, c_len * 0.6),
                    )
                mix_into(mix, cheer, c_start, gain=0.68)
            mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=min(spec.fade_out_sec, 3.0))

        elif spec.kind == "debut_video":
            src_vid_p = (self.sources_dir / spec.video_source_file) if spec.video_source_file else None
            if (
                spec.dj_seamless_intro
                and src_vid_p is not None
                and src_vid_p.is_file()
                and src_vid_p.stat().st_size > 1000
                and self.ffmpeg_bin
            ):
                tmp_pv_wav = self.rendered_dir / f".pv_src_{spec.id}_{threading.get_ident()}_{int(time.time() * 1000)}.wav"
                try:
                    ent_44k, _ = self._extract_source_video_audio(
                        src_vid_p, tmp_pv_wav, target_sr=SHOW_SR, max_sec=22.0
                    )
                    mix, _ = self._mix_dj_seamless_audio(spec, ent_44k, sr=SHOW_SR)
                    mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=min(2.2, spec.fade_out_sec or 2.2))
                finally:
                    try:
                        tmp_pv_wav.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                ann_pcm = self._get_announcer_stem(spec, suffix="announcer")
                if ann_pcm is None:
                    ann_pcm = np.zeros((int(2.0 * SHOW_SR), 2), dtype=np.float32)
                extra_intro: list[tuple[float, np.ndarray]] = []
                for idx_ic, ic in enumerate(spec.speech_cues):
                    if ic.get("target") == "intro" and ic.get("text"):
                        ic_pcm = self._get_announcer_stem(
                            spec,
                            suffix=f"icue_{idx_ic}",
                            text_override=str(ic["text"]),
                            voice_override=str(ic.get("voice") or ""),
                        )
                        if ic_pcm is not None and ic_pcm.shape[0] > 0:
                            extra_intro.append((float(ic.get("atSec") or 0.0), ic_pcm))
                mix = self._mix_debut_intro_stereo(
                    ann_pcm,
                    lightning_flash_sec=spec.lightning_flash_sec or 0.75,
                    add_cheer=spec.add_cheer,
                    add_thunder=spec.add_thunder,
                    intro_start_sec=spec.intro_start_sec,
                    extra_intro_cues=extra_intro,
                )

        else:  # "walkin": announcer intro -> tight beat-1 slam into walk-in song
            ann_pcm = self._get_announcer_stem(spec, suffix="announcer")
            ann_n = ann_pcm.shape[0] if ann_pcm is not None else 0
            overlap_n = min(ann_n // 4, int(round(0.18 * SHOW_SR))) if (ann_pcm is not None and music_pcm is not None) else 0
            song_off = max(0, ann_n - overlap_n)
            song_n = music_pcm.shape[0] if music_pcm is not None else 0
            total_n = max(SHOW_SR, song_off + song_n)
            mix = np.zeros((total_n, 2), dtype=np.float32)
            if spec.add_cheer and ann_n > 0:
                c_dur = min(float(total_n) / float(SHOW_SR), float(ann_n) / float(SHOW_SR) + 2.4)
                cheer_bed = load_studio_fx_stereo(
                    self.fx_dir,
                    "stadium_crowd.wav",
                    c_dur,
                    allow_default_fallback=self.allow_network,
                    swell_sec=0.25,
                    fade_out_sec=1.8,
                    sr=SHOW_SR,
                )
                if cheer_bed is None:
                    cheer_bed = synth_crowd_cheer_stereo(
                        c_dur,
                        sr=SHOW_SR,
                        swell_sec=0.25,
                        fade_out_sec=1.8,
                    )
                c_n = min(cheer_bed.shape[0], total_n)
                if c_n > 16:
                    c_env = np.zeros(c_n, dtype=np.float32)
                    sw_s = max(0, min(c_n, song_off - int(round(0.12 * SHOW_SR))))
                    sw_e = max(sw_s + 1, min(c_n, song_off + int(round(0.10 * SHOW_SR))))
                    if sw_s > 0:
                        c_env[:sw_s] = 0.14
                    if sw_e > sw_s:
                        u_sw = np.linspace(0.0, 1.0, sw_e - sw_s, dtype=np.float32)
                        c_env[sw_s:sw_e] = 0.14 + 0.24 * 0.5 * (1.0 - np.cos(math.pi * u_sw))
                    if c_n > sw_e:
                        u_dc = np.linspace(0.0, 1.0, c_n - sw_e, dtype=np.float32)
                        c_env[sw_e:] = 0.38 * (np.cos(0.5 * math.pi * u_dc) ** 2)
                    mix_into(mix, cheer_bed[:c_n] * c_env[:, None], 0, gain=1.0)
            if ann_pcm is not None:
                mix_into(mix, ann_pcm, 0, gain=1.0)
            if music_pcm is not None:
                # Crisp 10ms anti-click onset ONLY so beat 1 hits with full punch right out of announcer's last word
                s_entry = music_pcm.copy()
                r_n = min(s_entry.shape[0], max(8, int(round(0.010 * SHOW_SR))))
                if r_n > 2:
                    s_entry[:r_n] *= np.linspace(0.0, 1.0, r_n, dtype=np.float32)[:, None]
                mix_into(mix, s_entry, song_off, gain=0.96)
            mix = apply_fade_out_stereo(mix, sr=SHOW_SR, fade_out_sec=spec.fade_out_sec)

        wav_bytes = encode_stereo_pcm16_wav(mix, sr=SHOW_SR)
        tmp_wav = out_wav.with_suffix(".wav.tmp")
        tmp_wav.write_bytes(wav_bytes)
        tmp_wav.replace(out_wav)
        if spec.overlay_text:
            self.build_show_overlay(spec.id, force=force)
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

    def match_voice_command(
        self,
        transcript: str,
        wake_word: str = "armory",
        *,
        workout_active: bool = False,
    ) -> dict[str, Any] | None:
        """Match spoken show triggers (`I just hit a PR`, `stop show`, bare `stop`/`enough`/`end`/`cancel`, etc.)."""
        cmd = _strip_optional_wake(transcript, wake_word=wake_word)
        if not cmd:
            return None

        if re.search(
            r"\b(?:stop|kill|cancel|abort|end|enough)\s+(?:the\s+)?(?:show|walk\s*in|drop\s*in|entrance|sim|simulation|intro)\b",
            cmd,
        ):
            return {"action": "stop"}

        if re.match(r"^(?:stop|enough|end|abort|kill)(?:\s+(?:it|that|now|please|everything|all))?$", cmd):
            return {"action": "stop"}

        if re.match(r"^cancel(?:\s+(?:it|that|now|please))?$", cmd):
            with self._lock:
                show_active = bool(self._active_state.get("active"))
            if show_active or not workout_active:
                return {"action": "stop"}

        m_lt = re.match(
            r"^(?:turn\s+|switch\s+|shut\s+|power\s+)?(on|off)\s+(?:the\s+)?(?:(rack|smith|all|house|gym)\s+)?lights?$|"
            r"^(?:(?:turn|switch|shut|power)\s+)?(?:the\s+)?(?:(rack|smith|all|house|gym)\s+)?lights?\s+(on|off)$|"
            r"^(?:turn\s+|switch\s+|shut\s+)?(?:the\s+)?house\s+lights?\s+(rack|smith)\s+(on|off)$",
            cmd,
        )
        if m_lt:
            state_tok = (m_lt.group(1) or m_lt.group(4) or m_lt.group(6) or "").lower()
            zone_tok = (m_lt.group(2) or m_lt.group(3) or m_lt.group(5) or "all").lower()
            zone = zone_tok if zone_tok in ("rack", "smith") else "all"
            return {"action": "lights", "zone": zone, "on": state_tok == "on"}

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
            if (
                re.search(
                    r"\b(?:prd|(?:hit|got|set|broke|smashed|crushed)\s+(?:a\s+|my\s+|his\s+|her\s+|their\s+)?pr|new\s+pr|personal\s+record)\b",
                    cmd,
                )
                or re.match(r"^(?:play\s+)?(?:the\s+)?pr(?:\s+(?:drop|song|music|track|show))?$", cmd)
            ):
                pr_spec = next((s for s in specs if s.kind == "pr_drop"), None)
                if pr_spec is not None:
                    return {"action": "fire", "showId": pr_spec.id, "showName": pr_spec.name}
            return None
        spec = best[1]
        return {"action": "fire", "showId": spec.id, "showName": spec.name}

    # ---------- HTTP & Smart FX Helpers ----------

    def _resolve_local_url_ipv4(self, url: str) -> tuple[str, dict[str, str]]:
        """Pre-resolve `*.local` mDNS hostnames to IPv4 with cache so Windows IPv6 mDNS never stalls."""
        headers = {"Connection": "close"}
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").strip().lower()
            if host.endswith(".local"):
                orig_netloc = parts.netloc
                cached = self._mdns_ipv4_cache.get(host)
                now_m = time.monotonic()
                ip_addr = cached[0] if (cached and (now_m - cached[1]) < 300.0) else ""
                if not ip_addr:
                    try:
                        infos = socket.getaddrinfo(host, parts.port or 80, socket.AF_INET, socket.SOCK_STREAM)
                        if infos:
                            ip_addr = str(infos[0][4][0])
                            self._mdns_ipv4_cache[host] = (ip_addr, now_m)
                    except Exception:
                        ip_addr = cached[0] if cached else ""
                if ip_addr:
                    new_netloc = f"{ip_addr}:{parts.port}" if parts.port else ip_addr
                    resolved = urllib.parse.urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
                    headers["Host"] = orig_netloc
                    return resolved, headers
        except Exception:
            pass
        return url, headers

    def _fire_http_hook(self, url: str) -> dict[str, Any]:
        clean = str(url or "").strip()
        if not clean:
            return {"ok": False, "skipped": True}
        host_key = urllib.parse.urlsplit(clean).netloc.lower()
        now_m = time.monotonic()
        if host_key and self._http_get is None:
            fail_until = self._http_fail_until.get(host_key, 0.0)
            if now_m < fail_until:
                return {"ok": False, "skipped": True, "circuitOpen": True, "url": clean}
        try:
            if self._http_get is not None:
                code, _ = self._http_get(clean, 1.5)
                return {"ok": code < 400, "url": clean, "status": code}
            if not self.allow_network:
                return {"ok": False, "skipped": True, "error": "Network disabled"}
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    target_url, hdrs = self._resolve_local_url_ipv4(clean)
                    req = urllib.request.Request(target_url, method="GET", headers=hdrs)
                    with urllib.request.urlopen(req, timeout=2.5) as resp:
                        try:
                            resp.read()
                        except Exception:
                            pass
                        if host_key:
                            self._http_fail_until.pop(host_key, None)
                        return {"ok": int(resp.status) < 400, "url": clean, "status": int(resp.status)}
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.15 * (attempt + 1))
            raise last_exc if last_exc is not None else RuntimeError("HTTP hook failed")
        except Exception as exc:
            if host_key and self._http_get is None:
                self._http_fail_until[host_key] = time.monotonic() + 10.0
            return {"ok": False, "url": clean, "error": str(exc)}

    def _fire_http_hooks_parallel(self, urls: Sequence[str]) -> list[dict[str, Any]]:
        clean_urls = [str(u or "").strip() for u in urls if str(u or "").strip()]
        if not clean_urls:
            return []
        if len(clean_urls) == 1:
            return [self._fire_http_hook(clean_urls[0])]
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(clean_urls))
        try:
            futs = [ex.submit(self._fire_http_hook, u) for u in clean_urls]
            out: list[dict[str, Any]] = []
            for u, fut in zip(clean_urls, futs):
                try:
                    out.append(fut.result(timeout=2.0))
                except Exception as exc:
                    out.append({"ok": False, "url": u, "error": str(exc)})
            return out
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _fire_lights_state(
        self,
        on: bool,
        *,
        zone: str = "all",
        tag: str = "",
        label: str = "",
        show_id: str = "",
        duration_ms: int = 1200,
    ) -> dict[str, Any]:
        with self._lock:
            fx = self.fx_hooks
        if tag:
            self._emit_cue(tag, label or tag, show_id=show_id, duration_ms=duration_ms, lightsOn=on, zone=zone)
        urls = fx.resolve_lights_urls(on, zone=zone)
        if not urls:
            return {"ok": False, "skipped": True, "zone": zone}
        res_list = self._fire_http_hooks_parallel(urls)
        ok = all(bool(r.get("ok")) for r in res_list)
        return {"ok": ok, "zone": zone, "on": bool(on), "results": res_list}

    def _flash_lights(
        self,
        count: int,
        cancel_ev: threading.Event | None = None,
        *,
        show_id: str = "",
        end_on: bool = True,
        on_sec: float = 0.18,
        off_sec: float = 0.16,
        tag_prefix: str = "FLASH",
        zone: str = "all",
    ) -> None:
        with self._lock:
            fx = self.fx_hooks
        if count <= 0:
            return
        z_norm = (zone or "all").strip().lower()
        rack_on = fx.resolve_lights_urls(True, "rack")
        rack_off = fx.resolve_lights_urls(False, "rack")
        smith_on = fx.resolve_lights_urls(True, "smith")
        smith_off = fx.resolve_lights_urls(False, "smith")
        has_both = bool(rack_on and smith_on and rack_on != smith_on)
        all_on = fx.resolve_lights_urls(True, "all")
        all_off = fx.resolve_lights_urls(False, "all")
        on_urls = all_on if (z_norm not in ("rack", "smith")) else fx.resolve_lights_urls(True, z_norm)
        off_urls = all_off if (z_norm not in ("rack", "smith")) else fx.resolve_lights_urls(False, z_norm)

        deadline = time.monotonic()
        for idx in range(1, count + 1):
            if cancel_ev and cancel_ev.is_set():
                return
            lbl = f"({tag_prefix} {idx}/{count})" if count > 1 else f"({tag_prefix})"
            self._emit_cue(
                "flash",
                lbl,
                show_id=show_id,
                duration_ms=int(max(120, on_sec * 1000)),
                step=idx,
                total=count,
                zone=z_norm,
            )
            if z_norm == "alternate" and has_both:
                step_urls = (rack_on + smith_off) if (idx % 2 == 1) else (smith_on + rack_off)
                self._fire_http_hooks_parallel(step_urls)
                deadline = time.monotonic() + on_sec if self._has_custom_sleep else (deadline + on_sec)
                wait_on = on_sec if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
                if self._wait_cancelable(cancel_ev, wait_on):
                    return
                if off_sec > 0.0 and not (end_on and idx == count):
                    self._fire_http_hooks_parallel(all_off)
                    deadline = time.monotonic() + off_sec if self._has_custom_sleep else (deadline + off_sec)
                    wait_off = off_sec if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
                    if self._wait_cancelable(cancel_ev, wait_off):
                        return
                elif not end_on and all_off:
                    self._fire_http_hooks_parallel(all_off)
            else:
                if on_urls:
                    self._fire_http_hooks_parallel(on_urls)
                deadline = time.monotonic() + on_sec if self._has_custom_sleep else (deadline + on_sec)
                wait_on = on_sec if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
                if self._wait_cancelable(cancel_ev, wait_on):
                    return
                # Skip final OFF if we're finishing ON for the last flash
                if off_sec > 0.0 and not (end_on and idx == count):
                    if off_urls:
                        self._fire_http_hooks_parallel(off_urls)
                    deadline = time.monotonic() + off_sec if self._has_custom_sleep else (deadline + off_sec)
                    wait_off = off_sec if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
                    if self._wait_cancelable(cancel_ev, wait_off):
                        return
                elif not end_on and off_urls:
                    self._fire_http_hooks_parallel(off_urls)
        if end_on:
            fin_urls = all_on if z_norm == "alternate" else on_urls
            if fin_urls:
                self._fire_http_hooks_parallel(fin_urls)

    def _pulse_lights_loop(
        self,
        spec: ShowSpec,
        cancel_ev: threading.Event,
    ) -> bool:
        """Strobe lights `on_sec` every `interval_sec` for `duration_sec`. Returns True if canceled."""
        dur = float(spec.lights_pulse_duration_sec)
        interval = max(0.5, float(spec.lights_pulse_interval_sec or 2.0))
        on_s = max(0.1, min(interval - 0.1, float(spec.lights_pulse_on_sec or 0.25)))
        off_s = max(0.05, interval - on_s)
        n_pulses = max(1, int(round(dur / interval)))
        with self._lock:
            fx = self.fx_hooks
        on_urls = fx.resolve_lights_urls(True, "all")
        off_urls = fx.resolve_lights_urls(False, "all")
        deadline = time.monotonic()
        for i in range(1, n_pulses + 1):
            if cancel_ev.is_set():
                return True
            self._emit_cue(
                "pulse",
                f"(FLASH {on_s:.2f}s · Pulse {i}/{n_pulses})",
                show_id=spec.id,
                duration_ms=int(on_s * 1000),
                pulseIndex=i,
                pulseTotal=n_pulses,
            )
            if on_urls:
                self._fire_http_hooks_parallel(on_urls)
            deadline = time.monotonic() + on_s if self._has_custom_sleep else (deadline + on_s)
            wait_on = on_s if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
            if self._wait_cancelable(cancel_ev, wait_on):
                return True
            if off_urls:
                self._fire_http_hooks_parallel(off_urls)
            deadline = time.monotonic() + off_s if self._has_custom_sleep else (deadline + off_s)
            wait_off = off_s if self._has_custom_sleep else max(0.0, deadline - time.monotonic())
            if self._wait_cancelable(cancel_ev, wait_off):
                return True
        return False

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

    def _launch_three_screen_youtube(self, youtube_url: str, *, exclude_main_tv: bool = False) -> dict[str, Any]:
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
            if exclude_main_tv:
                results["tv"] = {"ok": True, "skipped": True, "excluded": True}
            else:
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

    def _tcp_port_open(self, host: str, port: int, timeout: float = 0.65) -> bool:
        """Fast non-blocking TCP reachability probe so offline TVs fail fast instead of stalling ADB."""
        if not self.allow_network or not host:
            return True
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(max(0.2, float(timeout)))
        try:
            s.connect((host, int(port)))
            return True
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _restore_wallboard_tv(self, *, force_stop_youtube: bool = False) -> dict[str, Any]:
        """Return the dedicated Wallboard Fire TV cleanly back to the Armory Wallboard URL."""
        wb_cfg = self._wallboard_cfg_fn() if self._wallboard_cfg_fn is not None else None
        wb_host = str(getattr(wb_cfg, "fire_tv_host", "") or "").strip() if wb_cfg else ""
        wb_port = int(getattr(wb_cfg, "adb_port", 5555) or 5555) if wb_cfg else 5555
        if not wb_host or self.adb is None:
            return {"ok": False, "skipped": True}
        if not self._tcp_port_open(wb_host, wb_port, timeout=0.6):
            return {"ok": False, "state": "offline", "error": f"{wb_host}:{wb_port} unreachable"}
        wb_ser = f"{wb_host}:{wb_port}" if ":" not in wb_host else wb_host
        wb_url = f"{self._resolve_stage_base_url()}/wallboard"
        st, detail = self.adb._connect_and_status(wb_ser)
        if st != "device":
            if hasattr(self.adb, "disconnect_serial"):
                try:
                    self.adb.disconnect_serial(wb_ser)
                except Exception:
                    pass
            return {"ok": False, "state": st, "error": detail}
        try:
            if force_stop_youtube:
                self.adb._run(
                    ["-s", wb_ser, "shell", "am", "force-stop", "com.amazon.firetv.youtube"],
                    timeout=4.0,
                )
            self.adb._run(["-s", wb_ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            if wb_cfg is not None and hasattr(self.adb, "_launch_cmd"):
                cmd_args = self.adb._launch_cmd(wb_cfg, wb_url, wb_ser)
            else:
                cmd = (
                    f"am start -W -a android.intent.action.VIEW -d {shlex.quote(wb_url)} "
                    "-n com.froriep.armorywallboard/.WallboardActivity"
                )
                cmd_args = ["-s", wb_ser, "shell", cmd]
            rc, out, err = self.adb._run(cmd_args, timeout=6.0)
            return {"ok": rc == 0, "url": wb_url, "out": (out or err).strip()}
        finally:
            if hasattr(self.adb, "disconnect_serial"):
                try:
                    self.adb.disconnect_serial(wb_ser)
                except Exception:
                    pass

    # ---------- Anti-Roller-Coaster Live Execution ----------

    def _restore_denon_down(self, from_db: float, to_db: float, *, fast: bool = False) -> bool:
        if self.house_audio is None:
            return False
        steps = 3 if fast else 6
        step_ms = 50 if fast else 135
        try:
            if abs(from_db - to_db) > 0.4:
                ramp_retry = getattr(self.house_audio.denon, "_ramp_volume_retry", None)
                if callable(ramp_retry):
                    ok = bool(ramp_retry(from_db, to_db, steps, step_ms, attempts=3))
                else:
                    ok = bool(self.house_audio.denon._ramp_volume(from_db, to_db, steps=steps, step_ms=step_ms))
            else:
                ok = False
                for attempt in range(3):
                    res = self.house_audio.denon.send_command(db_to_denon_mv(to_db))
                    if res.get("ok"):
                        ok = True
                        break
                    if attempt < 2:
                        time.sleep(0.3)
            if ok:
                clear_fn = getattr(self.house_audio.denon, "clear_volume_guard", None)
                if callable(clear_fn):
                    clear_fn()
            else:
                self.log.warning("Show Denon volume restore failed (%s -> %s dB)", from_db, to_db)
            return ok
        except Exception as exc:
            self.log.warning("Show Denon volume restore error (%s -> %s dB): %s", from_db, to_db, exc)
            return False

    def _trigger_pre_show_fx(
        self,
        spec: ShowSpec,
        fx: SmartFxConfig,
        *,
        scene_dur_ms: int,
        out_res: dict[str, Any],
    ) -> tuple[bool, bool]:
        """Fire lights-off, Smart Fogger v3.1 theater scene, and non-duplicated fog burst."""
        lights_turned_off = False
        theater_started = False
        if spec.lights_out:
            out_res["lightsOff"] = self._fire_lights_state(
                False, tag="lights_off", label="(LIGHTS OFF)", show_id=spec.id, duration_ms=1500
            )
            lights_turned_off = True

        burst_val = spec.fog_burst_sec if spec.fog_burst_sec > 0.0 else fx.fog_burst_sec
        fog_ms = int(round(burst_val * 1000)) if spec.trigger_fog else 0
        has_theater = bool(spec.theater_scene and fx.theater_host.strip())
        separate_fog_relay = bool(
            fx.fog_trigger_url
            or (fx.fog_host and fx.fog_host.strip().lower() != fx.theater_host.strip().lower())
        )

        if has_theater:
            scene_fog_ms = fog_ms if (spec.trigger_fog and not separate_fog_relay) else 0
            theater_url = fx.resolve_theater_scene_url(
                spec.theater_scene,
                duration_ms=scene_dur_ms,
                fog_ms=scene_fog_ms,
            )
            if theater_url:
                self._emit_cue(
                    "theater_fx",
                    f"(THEATER FX · {spec.theater_scene.upper()})",
                    show_id=spec.id,
                    duration_ms=scene_dur_ms,
                    scene=spec.theater_scene,
                    fogMs=scene_fog_ms,
                )
                out_res["theaterFx"] = self._fire_http_hook(theater_url)
                theater_started = True
                if scene_fog_ms > 0:
                    burst_str = f"{float(burst_val):.1f}".rstrip("0").rstrip(".")
                    self._emit_cue(
                        "fog",
                        f"(TRIGGER FOG · {burst_str}s)",
                        show_id=spec.id,
                        duration_ms=scene_fog_ms,
                        fogSec=float(burst_val),
                    )
                    out_res["fog"] = out_res["theaterFx"]

        if spec.trigger_fog and (not has_theater or separate_fog_relay):
            burst_str = f"{float(burst_val):.1f}".rstrip("0").rstrip(".")
            self._emit_cue(
                "fog",
                f"(TRIGGER FOG · {burst_str}s)",
                show_id=spec.id,
                duration_ms=int(burst_val * 1000),
                fogSec=float(burst_val),
            )
            fog_url = fx.resolve_fog_url(burst_sec=burst_val)
            if fog_url:
                out_res["fog"] = self._fire_http_hook(fog_url)

        return lights_turned_off, theater_started

    def _cleanup_post_show_fx(
        self,
        spec: ShowSpec,
        fx: SmartFxConfig,
        *,
        theater_started: bool,
        lights_turned_off: bool,
        lights_restored: bool,
    ) -> None:
        """Stop Smart Fogger scene and restore house lights if a show exits early or kept them off."""
        if theater_started:
            stop_u = fx.resolve_theater_stop_url()
            if stop_u:
                self._fire_http_hook(stop_u)
        if lights_turned_off and not lights_restored:
            self._fire_lights_state(
                True,
                tag="lights_on",
                label="(SAFETY HOUSE LIGHTS ON)",
                show_id=spec.id,
                duration_ms=1500,
            )

    def _run_multimedia_show_sequence(
        self,
        spec: ShowSpec,
        run_id: int,
        cancel_ev: threading.Event,
        mp4_path: Path,
        meta: dict[str, Any],
        fx: SmartFxConfig,
        *,
        exclude_main_tv: bool = False,
    ) -> dict[str, Any]:
        """Run unified local 1080p MP4 show across Cube + Hisense + SmartSpin + Wallboard with T=0 pre-staging."""
        prev_db: float | None = None
        has_baseline = False
        target_db = _clamp(float(spec.denon_vol) - 80.0, -50.0, -5.0)
        current_db = target_db
        denon_raised = False
        denon_restored = False
        theater_started = False
        lights_turned_off = False
        lights_restored = False
        cube_ser = ""
        cube_stage_launched = False
        prev_media: dict[str, Any] = {}
        out_res: dict[str, Any] = {"ok": True, "showId": spec.id, "multimedia": True}

        intro_dur = float(meta.get("introDurationSec") or 0.0)
        if intro_dur <= 0.1:
            p_wav = self.preview_wav_path(spec.id)
            intro_dur = wav_duration_sec(p_wav) if p_wav.is_file() else 9.0
        total_dur = float(meta.get("totalDurationSec") or 0.0)
        if total_dur <= intro_dur:
            total_dur = intro_dur + float(spec.video_hold_sec or 45.0)
        overlay_dur = float(meta.get("overlayDurationSec") or 0.0)
        if overlay_dur <= 0.1 and spec.overlay_text:
            o_wav = self.overlay_wav_path(spec.id)
            overlay_dur = wav_duration_sec(o_wav) if o_wav.is_file() else 4.0

        flash_events: list[dict[str, Any]] = []
        for fc in spec.fx_cues:
            if fc.get("type") == "flash" and fc.get("screenFlash", True):
                base_t = float(fc.get("atSec") or 0.0)
                show_t = base_t if fc.get("target") == "intro" else (intro_dur + base_t)
                flash_events.append({
                    "atSec": round(show_t, 2),
                    "durSec": float(fc.get("durationSec") or 0.2),
                    "offSec": float(fc.get("offSec") or 0.22),
                    "count": int(fc.get("count") or 1),
                })

        try:
            # 1. Snapshot Denon AVR baseline BEFORE raising volume or changing inputs
            if self.house_audio is not None:
                st = self.house_audio.denon.get_status()
                if st.get("ok") and st.get("volumeDb") is not None:
                    prev_db = float(st["volumeDb"])
                    has_baseline = True
            current_db = prev_db if prev_db is not None else target_db

            with self._lock:
                self._active_state["phase"] = "prestage_displays"
            self._emit_cue(
                "prestage",
                "(PRE-STAGING SCREENS + DENON + FOG)" if exclude_main_tv else "(PRE-STAGING ALL 3 TVS + DENON + FOG)",
                show_id=spec.id,
                duration_ms=3000,
            )

            # 2. Broadcast stage_prepare immediately so already-open Wallboard blanks to #000 & buffers MP4
            mm_scene_dur_ms = int(max(15_000, min(300_000, round((total_dur + 4.0) * 1000))))
            base_url = self._resolve_stage_base_url()
            try:
                ver_tag = int(mp4_path.stat().st_mtime)
            except Exception:
                ver_tag = int(time.time())
            video_url = f"/api/shows/{spec.id}/show.mp4?v={ver_tag}"
            self._broadcast_stage_event(
                "stage_prepare",
                runId=run_id,
                showId=spec.id,
                videoUrl=video_url,
                introDurationSec=round(intro_dur, 2),
                totalDurationSec=round(total_dur, 2),
                flashEvents=flash_events,
            )

            # 4. Pre-stage hardware targets IN PARALLEL (Denon + Hisense [unless excluded] + SmartSpin + Cube + Wallboard)
            wb_cfg = self._wallboard_cfg_fn() if self._wallboard_cfg_fn is not None else None
            wb_host = str(getattr(wb_cfg, "fire_tv_host", "") or "").strip() if wb_cfg else ""
            wb_port = int(getattr(wb_cfg, "adb_port", 5555) or 5555) if wb_cfg else 5555
            wb_ser = f"{wb_host}:{wb_port}" if (wb_host and ":" not in wb_host) else wb_host

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
            cube_ser = f"{cube_host}:{cube_port}" if (cube_host and ":" not in cube_host) else cube_host

            def _prep_denon() -> dict[str, Any]:
                res_d: dict[str, Any] = {}
                if self.house_audio is not None:
                    d_st = self.house_audio.denon.get_status()
                    cur_inp = str(d_st.get("input") or "").strip().upper()
                    if d_st.get("ok") and d_st.get("power") != "ON":
                        self.house_audio.denon.send_command("PWON")
                        self._wait_cancelable(cancel_ev, 0.35)
                        self.house_audio.denon.send_command("SIMPLAY")
                    elif not d_st.get("ok") or cur_inp != "MPLAY":
                        self.house_audio.denon.send_command("SIMPLAY")
                if self.displays is not None:
                    res_d["avrMode"] = self.displays._control_avr("mch_stereo")
                    res_d["avrMonitor"] = self.displays.set_denon_monitor_out("AUTO")
                return res_d

            def _prep_hisense() -> dict[str, Any]:
                if exclude_main_tv:
                    return {"ok": True, "skipped": True, "excluded": True, "switched": False}
                if self.displays is None:
                    return {"ok": False, "skipped": True}
                if hasattr(self.displays, "ensure_primary_hdmi_ready"):
                    return self.displays.ensure_primary_hdmi_ready("hdmi2")
                return self.displays._control_primary("hdmi2")

            def _prep_spin() -> dict[str, Any]:
                if self.displays is None:
                    return {"ok": False, "skipped": True}
                if hasattr(self.displays, "ensure_spin_hdmi_ready"):
                    return self.displays.ensure_spin_hdmi_ready()
                if self.displays.get_config().spin.host:
                    return self.displays._control_spin("on")
                return {"ok": True, "skipped": True, "switched": False}

            def _prep_cube() -> dict[str, Any]:
                if not cube_ser or self.adb is None:
                    return {"ok": False, "skipped": True}
                st, detail = self.adb._connect_and_status(cube_ser)
                if st != "device":
                    return {"ok": False, "state": st, "error": detail}
                try:
                    snap_info: dict[str, Any] = {
                        "wasPlaying": False,
                        "mediaPackage": "",
                        "mediaState": 0,
                        "focusedComponent": "",
                        "focusedPackage": "",
                    }
                    try:
                        rc_snap, snap_out, _ = self.adb._run(
                            [
                                "-s",
                                cube_ser,
                                "shell",
                                "dumpsys media_session; echo ---ARMORY_WIN---; dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'",
                            ],
                            timeout=4.5,
                        )
                        if rc_snap == 0 and snap_out:
                            snap_info = parse_cube_media_snapshot(snap_out)
                    except Exception:
                        pass
                    self.adb._run(["-s", cube_ser, "shell", "input", "keyevent", "224"], timeout=4.0)
                    self.adb._run(["-s", cube_ser, "shell", "input", "keyevent", "127"], timeout=4.0)
                    stage_url = f"{base_url}/stage?role=cube&audio=1"
                    cmd = (
                        f"am start -a android.intent.action.VIEW -d {shlex.quote(stage_url)} "
                        "-n com.froriep.armorywallboard/.WallboardActivity"
                    )
                    rc, out, err = self.adb._run(["-s", cube_ser, "shell", cmd], timeout=6.0)
                    return {"ok": rc == 0, "url": stage_url, "out": (out or err).strip(), "prevMedia": snap_info}
                finally:
                    if hasattr(self.adb, "disconnect_serial"):
                        try:
                            self.adb.disconnect_serial(cube_ser)
                        except Exception:
                            pass

            def _prep_wallboard() -> dict[str, Any]:
                if not wb_ser or self.adb is None:
                    return {"ok": False, "skipped": True}
                if not self._tcp_port_open(wb_host, wb_port, timeout=0.65):
                    wk = self.displays.execute("wallboard", "on") if self.displays is not None else None
                    woke = False
                    if self.allow_network:
                        deadline_wb = time.monotonic() + 2.0
                        while time.monotonic() < deadline_wb and not cancel_ev.is_set():
                            if self._tcp_port_open(wb_host, wb_port, timeout=0.45):
                                woke = True
                                break
                            time.sleep(0.25)
                    if not woke:
                        return {"ok": False, "state": "offline", "wake": wk, "error": f"{wb_ser} unreachable"}
                st, detail = self.adb._connect_and_status(wb_ser)
                if st != "device" and self.displays is not None:
                    wk = self.displays.execute("wallboard", "on")
                    st, detail = self.adb._connect_and_status(wb_ser)
                    if st != "device":
                        return {"ok": False, "state": st, "wake": wk, "error": detail}
                if st != "device":
                    return {"ok": False, "state": st, "error": detail}
                try:
                    self.adb._run(["-s", wb_ser, "shell", "input", "keyevent", "224"], timeout=4.0)
                    self.adb._run(["-s", wb_ser, "shell", "media", "volume", "--stream", "3", "--set", "0"], timeout=4.0)
                    rc_act, out_act, _ = self.adb._run(
                        ["-s", wb_ser, "shell", "dumpsys", "activity", "activities"], timeout=4.5
                    )
                    on_top = (
                        rc_act == 0
                        and "com.froriep.armorywallboard" in (out_act or "")
                        and self.has_stage_client("wallboard")
                    )
                    if on_top:
                        return {"ok": True, "alreadyActive": True}
                    wb_url = f"{base_url}/wallboard"
                    cmd = (
                        f"am start -a android.intent.action.VIEW -d {shlex.quote(wb_url)} "
                        "-n com.froriep.armorywallboard/.WallboardActivity"
                    )
                    rc, out, err = self.adb._run(["-s", wb_ser, "shell", cmd], timeout=6.0)
                    return {"ok": rc == 0, "url": wb_url, "launched": True, "out": (out or err).strip()}
                finally:
                    if hasattr(self.adb, "disconnect_serial"):
                        try:
                            self.adb.disconnect_serial(wb_ser)
                        except Exception:
                            pass

            prestage: dict[str, Any] = {}
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=5)
            try:
                futs = {
                    "denon": ex.submit(_prep_denon),
                    "tv": ex.submit(_prep_hisense),
                    "spin": ex.submit(_prep_spin),
                    "cube": ex.submit(_prep_cube),
                    "wallboard": ex.submit(_prep_wallboard),
                }
                for key, fut in futs.items():
                    try:
                        prestage[key] = fut.result(timeout=12.0)
                    except Exception as exc:
                        prestage[key] = {"ok": False, "error": str(exc)}
            finally:
                ex.shutdown(wait=False, cancel_futures=True)

            switch_done_mono = time.monotonic()
            out_res["prestage"] = prestage
            cube_stage_launched = bool(prestage.get("cube", {}).get("ok"))
            prev_media = dict(prestage.get("cube", {}).get("prevMedia") or {})

            if cancel_ev.is_set():
                return {"ok": False, "canceled": True}

            # 5. Wait for Cube & Wallboard stages to finish buffering FIRST (while room is still lit & quiet!)
            roles_to_wait: list[str] = []
            if cube_stage_launched:
                roles_to_wait.append("cube")
            if prestage.get("wallboard", {}).get("ok") or self.has_stage_client("wallboard"):
                roles_to_wait.append("wallboard")
            ack_timeout = 0.05 if self._has_custom_sleep else 8.0
            out_res["stageReady"] = self._wait_for_stage_acks(
                run_id, roles_to_wait, target_states=("ready",), cancel_ev=cancel_ev, timeout_sec=ack_timeout
            )
            if cancel_ev.is_set():
                return {"ok": False, "canceled": True}

            any_hdmi_switched = bool(
                prestage.get("tv", {}).get("switched") or prestage.get("spin", {}).get("switched")
            )
            if any_hdmi_switched and not self._has_custom_sleep:
                rem_settle = max(0.0, 2.8 - (time.monotonic() - switch_done_mono))
                if rem_settle > 0.0 and self._wait_cancelable(cancel_ev, rem_settle):
                    return {"ok": False, "canceled": True}

            # 5.5 Fire Lights Out + Theater FX + Fog Machine right before playback (~1.6s fog head-start, never 10-15s dead silence)
            lights_turned_off, theater_started = self._trigger_pre_show_fx(
                spec, fx, scene_dur_ms=mm_scene_dur_ms, out_res=out_res
            )
            if (lights_turned_off or theater_started) and not self._has_custom_sleep:
                if self._wait_cancelable(cancel_ev, 1.6):
                    return {"ok": False, "canceled": True}

            # 6. Ramp Denon to MV70, then fire clock-synced `stage_play` across stage screens!
            if self.house_audio is not None and has_baseline and prev_db is not None:
                save_guard = getattr(self.house_audio.denon, "save_volume_guard", None)
                if callable(save_guard):
                    save_guard(prev_db)
                self.house_audio.denon._ramp_volume(prev_db, target_db, steps=4, step_ms=35)
                current_db = target_db
                denon_raised = True

            lead_sec = 0.0 if self._has_custom_sleep else 0.45
            start_at_ms = int(time.time() * 1000) + int(round(lead_sec * 1000))
            with self._lock:
                self._active_state["phase"] = "playing"
            self._broadcast_stage_event(
                "stage_play",
                runId=run_id,
                showId=spec.id,
                videoUrl=video_url,
                startAtMs=start_at_ms,
                introDurationSec=round(intro_dur, 2),
                totalDurationSec=round(total_dur, 2),
                flashEvents=flash_events,
            )
            self._emit_cue(
                "intro",
                f"(ALL 3 SCREENS BLACK → PULSES · DENON {spec.denon_vol})",
                show_id=spec.id,
                duration_ms=int(intro_dur * 1000),
                denonVol=spec.denon_vol,
                caption=spec.announcer_text,
            )

            if lead_sec > 0.0 and self._wait_cancelable(cancel_ev, lead_sec):
                return {"ok": False, "canceled": True}

            # Lock server light/FX clock to actual Cube video playback start (`playing` ack) so lights never run 2-3s early!
            if cube_stage_launched and not self._has_custom_sleep:
                self._wait_for_stage_acks(
                    run_id,
                    ["cube"],
                    target_states=("playing", "ended"),
                    cancel_ev=cancel_ev,
                    timeout_sec=2.5,
                )
                if cancel_ev.is_set():
                    return {"ok": False, "canceled": True}

            play_t0 = time.monotonic()
            sim_clock = 0.0

            def _wait_until_show_sec(target_sec: float) -> bool:
                nonlocal sim_clock
                tgt = max(0.0, float(target_sec))
                if self._has_custom_sleep:
                    delta = max(0.0, tgt - sim_clock)
                    sim_clock = max(sim_clock, tgt)
                    return self._wait_cancelable(cancel_ev, delta)
                wait_s = max(0.0, (play_t0 + tgt) - time.monotonic())
                return self._wait_cancelable(cancel_ev, wait_s)

            # 7. Unified monotonic sorted timeline (lights, strobes, timed speech overlays, custom FX cues)
            lead_l = max(0.0, float(fx.lights_lead_sec or 0.0))
            has_explicit_lights_on = any(
                fc.get("type") == "lights_on" and str(fc.get("zone") or "all").lower() == "all"
                for fc in spec.fx_cues
            )
            timeline: list[tuple[float, int, str, dict[str, Any]]] = []
            if spec.lightning_flash_sec > 0.0 and spec.lightning_flash_sec < (intro_dur - 0.8):
                timeline.append((float(spec.lightning_flash_sec), 10, "lightning", {"on_sec": 0.16, "off_sec": 0.05}))
            if spec.lights_flash_count > 0:
                strobe_at = max(0.5, intro_dur - 0.92)
                timeline.append((strobe_at, 15, "intro_strobe", {"count": spec.lights_flash_count}))

            timeline.append((intro_dur, 20, "video_screens", {}))

            if spec.lights_pulse_duration_sec > 0.0:
                p_dur = float(spec.lights_pulse_duration_sec)
                p_interval = max(0.5, float(spec.lights_pulse_interval_sec or 2.0))
                p_on = max(0.1, min(p_interval - 0.1, float(spec.lights_pulse_on_sec or 0.25)))
                n_pulses = max(1, int(round(p_dur / p_interval)))
                for idx_p in range(1, n_pulses + 1):
                    t_p = intro_dur + (idx_p - 1) * p_interval
                    timeline.append((t_p, 25, "pulse_on", {"index": idx_p, "total": n_pulses, "on_sec": p_on}))
                    timeline.append((t_p + p_on, 26, "pulse_off", {}))
                timeline.append((max(0.0, intro_dur + n_pulses * p_interval - lead_l), 30, "lights_full_on", {}))
            elif spec.lights_out and not has_explicit_lights_on:
                timeline.append((max(0.0, intro_dur - lead_l), 30, "lights_full_on", {}))

            raw_vid_timings = meta.get("speechCueTimings")
            if isinstance(raw_vid_timings, list) and raw_vid_timings:
                active_vid_timings = [dict(x) for x in raw_vid_timings if isinstance(x, dict)]
            else:
                active_vid_timings = []
                for sc in spec.speech_cues:
                    if sc.get("target") == "video" and sc.get("text"):
                        active_vid_timings.append({
                            "id": sc.get("id") or "",
                            "target": "video",
                            "atSec": float(sc.get("atSec") or 0.0),
                            "showAtSec": round(intro_dur + float(sc.get("atSec") or 0.0), 2),
                            "durSec": float(overlay_dur or 3.5),
                            "text": str(sc.get("text") or ""),
                            "postDenonVol": int(sc.get("postDenonVol") or 0),
                        })

            any_post_denon = False
            for sc in active_vid_timings:
                show_at = float(sc.get("showAtSec") or 0.0)
                dur_s = max(0.5, float(sc.get("durSec") or 3.5))
                if show_at < total_dur:
                    timeline.append((show_at, 40, "speech_cue", sc))
                    p_vol = int(sc.get("postDenonVol") or 0)
                    if p_vol > 0:
                        any_post_denon = True
                        timeline.append((show_at + dur_s, 45, "denon_drop", {"vol": p_vol}))

            for fc in spec.fx_cues:
                base_t = float(fc.get("atSec") or 0.0)
                show_t = base_t if fc.get("target") == "intro" else (intro_dur + base_t)
                if show_t < total_dur:
                    eff_t = max(0.0, show_t - lead_l) if fc.get("type") in ("lights_on", "lights_off") else show_t
                    timeline.append((eff_t, 35, "fx_cue", dict(fc)))

            timeline.sort(key=lambda item: (item[0], item[1]))

            for evt_sec, _prio, kind, payload in timeline:
                if _wait_until_show_sec(evt_sec):
                    return {"ok": False, "canceled": True}
                if kind == "lightning":
                    self._flash_lights(
                        1,
                        cancel_ev=cancel_ev,
                        show_id=spec.id,
                        end_on=False,
                        on_sec=float(payload.get("on_sec") or 0.16),
                        off_sec=float(payload.get("off_sec") or 0.05),
                        tag_prefix="FLASH · LIGHTNING",
                    )
                    sim_clock += float(payload.get("on_sec") or 0.16) + float(payload.get("off_sec") or 0.05)
                elif kind == "intro_strobe":
                    cnt = int(payload.get("count") or 1)
                    self._flash_lights(
                        cnt,
                        cancel_ev=cancel_ev,
                        show_id=spec.id,
                        end_on=True,
                        tag_prefix="FLASH",
                    )
                    sim_clock += cnt * 0.18 + max(0, cnt - 1) * 0.16
                elif kind == "video_screens":
                    with self._lock:
                        self._active_state["phase"] = "strobe_video" if spec.lights_pulse_duration_sec > 0.0 else "video_screens"
                    self._emit_cue(
                        "screens",
                        f"(ALL 3 SCREENS LIVE · DENON {spec.denon_vol})",
                        show_id=spec.id,
                        duration_ms=4000,
                        denonVol=spec.denon_vol,
                        youtubeUrl=spec.youtube_url,
                    )
                elif kind == "pulse_on":
                    idx_p = int(payload.get("index") or 1)
                    tot_p = int(payload.get("total") or 1)
                    on_s = float(payload.get("on_sec") or 0.25)
                    self._emit_cue(
                        "pulse",
                        f"(FLASH {on_s:.2f}s · Pulse {idx_p}/{tot_p})",
                        show_id=spec.id,
                        duration_ms=int(on_s * 1000),
                        pulseIndex=idx_p,
                        pulseTotal=tot_p,
                    )
                    self._fire_http_hooks_parallel(fx.resolve_lights_urls(True, "all"))
                elif kind == "pulse_off":
                    self._fire_http_hooks_parallel(fx.resolve_lights_urls(False, "all"))
                elif kind == "lights_full_on":
                    self._fire_lights_state(
                        True,
                        tag="lights_on",
                        label="(FULL HOUSE LIGHTS ON)",
                        show_id=spec.id,
                        duration_ms=2500,
                    )
                    lights_restored = True
                elif kind == "speech_cue":
                    with self._lock:
                        self._active_state["phase"] = "overlay_duck"
                    c_txt = str(payload.get("text") or "")
                    c_dur = float(payload.get("durSec") or 3.5)
                    self._emit_cue(
                        "overlay",
                        f"(DUCK + OVERLAY · \"{c_txt}\")",
                        show_id=spec.id,
                        duration_ms=int((c_dur + 1.0) * 1000),
                        caption=c_txt,
                    )
                elif kind == "denon_drop":
                    p_vol = int(payload.get("vol") or 55)
                    post_db = _clamp(float(p_vol) - 80.0, -50.0, -5.0)
                    self._restore_denon_down(current_db, post_db, fast=False)
                    current_db = post_db
                    self._emit_cue(
                        "denon_drop",
                        f"(RESTORE VIDEO · DENON {spec.denon_vol} → {p_vol} · FULL LIGHTS ON)",
                        show_id=spec.id,
                        duration_ms=3000,
                        denonVol=p_vol,
                    )
                elif kind == "fx_cue":
                    fx_type = str(payload.get("type") or "flash")
                    dur_fx = float(payload.get("durationSec") or 0.2)
                    cnt_fx = int(payload.get("count") or 1)
                    zone_fx = str(payload.get("zone") or "all")
                    if fx_type == "flash":
                        end_on_fx = bool(payload.get("endOn", True))
                        off_fx = max(0.1, min(5.0, float(payload.get("offSec") or 0.22)))
                        if payload.get("lightFlash", True):
                            self._flash_lights(
                                cnt_fx,
                                cancel_ev=cancel_ev,
                                show_id=spec.id,
                                end_on=end_on_fx,
                                on_sec=max(0.1, min(2.0, dur_fx)),
                                off_sec=off_fx,
                                tag_prefix="FLASH · CUSTOM",
                                zone=zone_fx,
                            )
                            if zone_fx == "all":
                                if end_on_fx:
                                    lights_restored = True
                                else:
                                    lights_turned_off = True
                                    lights_restored = False
                            sim_clock += cnt_fx * max(0.1, min(2.0, dur_fx)) + (cnt_fx if not end_on_fx else max(0, cnt_fx - 1)) * off_fx
                        else:
                            self._emit_cue(
                                "flash",
                                "(TV FLASH · SYNCOPATED)",
                                show_id=spec.id,
                                duration_ms=int(max(120, dur_fx * 1000)),
                                zone=zone_fx,
                            )
                    elif fx_type == "lights_off":
                        self._fire_lights_state(
                            False, zone=zone_fx, tag="lights_off", label="(LIGHTS OFF · FX)", show_id=spec.id, duration_ms=1200
                        )
                        if zone_fx == "all":
                            lights_turned_off = True
                            lights_restored = False
                    elif fx_type == "lights_on":
                        self._fire_lights_state(
                            True, zone=zone_fx, tag="lights_on", label="(LIGHTS ON · FX)", show_id=spec.id, duration_ms=1200
                        )
                        if zone_fx == "all":
                            lights_restored = True
                    elif fx_type == "fog":
                        b_str = f"{dur_fx:.1f}".rstrip("0").rstrip(".")
                        self._emit_cue(
                            "fog",
                            f"(TRIGGER FOG · {b_str}s)",
                            show_id=spec.id,
                            duration_ms=int(dur_fx * 1000),
                            fogSec=dur_fx,
                        )
                        fog_u = fx.resolve_fog_url(burst_sec=dur_fx)
                        if fog_u:
                            self._fire_http_hook(fog_u)

            # 8. Hold until video ends (either Cube ACK `ended` or full MP4 duration)
            with self._lock:
                self._active_state["phase"] = "video_finish"
            if self._has_custom_sleep:
                rem = max(0.0, total_dur - sim_clock)
                if rem > 0.0 and self._wait_cancelable(cancel_ev, rem):
                    return {"ok": False, "canceled": True}
            else:
                end_deadline = play_t0 + total_dur + 0.6
                while not cancel_ev.is_set():
                    with self._stage_ack_cond:
                        if self._stage_acks.get((run_id, "cube")) == "ended":
                            break
                        rem_wait = end_deadline - time.monotonic()
                        if rem_wait <= 0.0:
                            break
                        self._stage_ack_cond.wait(timeout=min(0.20, rem_wait))
                if cancel_ev.is_set():
                    return {"ok": False, "canceled": True}

            if not any_post_denon and spec.post_overlay_denon_vol <= 0 and denon_raised and has_baseline and prev_db is not None:
                self._restore_denon_down(current_db, prev_db, fast=False)
            denon_restored = True
            self._emit_cue("ended", "(SHOW COMPLETE)", show_id=spec.id, duration_ms=1200)
            return out_res

        except Exception as exc:
            out_res = {"ok": False, "showId": spec.id, "error": str(exc)}
            return out_res
        finally:
            with self._lock:
                is_current = self._active_run_id == run_id
            if is_current:
                self._broadcast_stage_event("stage_stop", runId=run_id, showId=spec.id)
                self._cleanup_post_show_fx(
                    spec,
                    fx,
                    theater_started=theater_started,
                    lights_turned_off=lights_turned_off,
                    lights_restored=lights_restored,
                )
                if denon_raised and not denon_restored and has_baseline and prev_db is not None and self.house_audio is not None:
                    self._restore_denon_down(current_db, prev_db, fast=True)
                if self.displays is not None:
                    try:
                        self.displays.sync_denon_monitor_for_spin()
                    except Exception:
                        pass
                try:
                    self._restore_wallboard_tv(force_stop_youtube=False)
                except Exception:
                    pass
                if cube_stage_launched and cube_ser and self.adb is not None:
                    try:
                        st, _ = self.adb._connect_and_status(cube_ser)
                        if st == "device":
                            self.adb._run(["-s", cube_ser, "shell", "input", "keyevent", "4"], timeout=4.0)
                            self.adb._run(["-s", cube_ser, "shell", "am", "force-stop", "com.froriep.armorywallboard"], timeout=4.0)
                            target_comp = str(prev_media.get("focusedComponent") or "").strip()
                            target_pkg = str(prev_media.get("focusedPackage") or prev_media.get("mediaPackage") or "").strip()
                            if target_comp or target_pkg:
                                _, win_out, _ = self.adb._run(
                                    ["-s", cube_ser, "shell", "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'"],
                                    timeout=4.0,
                                )
                                cur_win = parse_cube_media_snapshot("---ARMORY_WIN---\n" + (win_out or ""))
                                cur_pkg = str(cur_win.get("focusedPackage") or "")
                                if target_pkg and cur_pkg != target_pkg:
                                    if target_comp:
                                        self.adb._run(
                                            ["-s", cube_ser, "shell", f"am start -n {shlex.quote(target_comp)}"],
                                            timeout=5.0,
                                        )
                                    elif target_pkg:
                                        self.adb._run(
                                            [
                                                "-s",
                                                cube_ser,
                                                "shell",
                                                f"monkey -p {shlex.quote(target_pkg)} -c android.intent.category.LAUNCHER 1",
                                            ],
                                            timeout=5.0,
                                        )
                            if prev_media.get("wasPlaying"):
                                if not self._has_custom_sleep:
                                    self._sleep(0.7)
                                _, ms_out, _ = self.adb._run(
                                    ["-s", cube_ser, "shell", "dumpsys", "media_session"], timeout=4.5
                                )
                                cur_ms = parse_cube_media_snapshot(ms_out or "")
                                if not cur_ms.get("wasPlaying"):
                                    self.adb._run(["-s", cube_ser, "shell", "input", "keyevent", "126"], timeout=4.0)
                                    if not self._has_custom_sleep:
                                        self._sleep(0.75)
                                    _, ms_out2, _ = self.adb._run(
                                        ["-s", cube_ser, "shell", "dumpsys", "media_session"], timeout=4.5
                                    )
                                    cur_ms2 = parse_cube_media_snapshot(ms_out2 or "")
                                    if not cur_ms2.get("wasPlaying") and cur_ms2.get("mediaState") == 2:
                                        rc_disp, _, _ = self.adb._run(
                                            ["-s", cube_ser, "shell", "cmd media_session dispatch play"], timeout=4.0
                                        )
                                        if rc_disp != 0:
                                            self.adb._run(["-s", cube_ser, "shell", "input", "keyevent", "85"], timeout=4.0)
                    except Exception:
                        pass
                    finally:
                        if hasattr(self.adb, "disconnect_serial"):
                            try:
                                self.adb.disconnect_serial(cube_ser)
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

    def _run_show_sequence(
        self,
        spec: ShowSpec,
        run_id: int,
        cancel_ev: threading.Event,
        *,
        exclude_main_tv: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            fx = SmartFxConfig.from_dict(self.fx_hooks.to_dict())

        eff_exclude_main = spec.exclude_main_tv if exclude_main_tv is None else bool(exclude_main_tv)

        if spec.three_screen_video:
            mp4_path = self.show_mp4_path(spec.id)
            meta = self._read_show_meta(spec.id)
            if mp4_path.is_file() and mp4_path.stat().st_size > 1000 and meta.get("hasSourceVideo"):
                return self._run_multimedia_show_sequence(
                    spec,
                    run_id,
                    cancel_ev,
                    mp4_path,
                    meta,
                    fx,
                    exclude_main_tv=eff_exclude_main,
                )

        prev_db: float | None = None
        has_baseline = False
        target_db = _clamp(float(spec.denon_vol) - 80.0, -50.0, -5.0)
        current_db = target_db
        denon_raised = False
        denon_restored = False
        theater_started = False
        lights_turned_off = False
        lights_restored = False
        cube_started = False
        wallboard_youtube_launched = False
        out_res: dict[str, Any] = {"ok": True, "showId": spec.id}

        try:
            # 1. Guarantee tight preview WAV (and optional overlay WAV) exists
            build_info = self.build_show_preview(spec.id, force=False)
            p_wav = self.preview_wav_path(spec.id)
            if not p_wav.is_file():
                raise RuntimeError(build_info.get("error") or "Preview WAV missing")
            raw_wav = p_wav.read_bytes()
            preview_dur = wav_duration_sec(p_wav)

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
                        has_baseline = True
                    if was_standby:
                        self.house_audio.denon.send_command("PWON")
                        if self._wait_cancelable(cancel_ev, 0.35):
                            return {"ok": False, "canceled": True}
                        self.house_audio.denon.send_command("SIMPLAY")
                        self.house_audio.denon.send_command("MSMCH STEREO")
                        if prev_db is not None:
                            prev_db = min(prev_db, target_db)
            current_db = prev_db if prev_db is not None else target_db

            # 3. Pre-show smart FX hooks (lights out + Theater FX + fog burst + live SSE cue simulation)
            seq_scene_dur_ms = int(
                max(
                    15_000,
                    min(
                        300_000,
                        round((preview_dur + (float(spec.video_hold_sec or 45.0) if spec.three_screen_video else 2.0)) * 1000),
                    ),
                )
            )
            lights_turned_off, theater_started = self._trigger_pre_show_fx(
                spec, fx, scene_dur_ms=seq_scene_dur_ms, out_res=out_res
            )

            # 4. PHASE 1 (ANTI-ROLLER-COASTER ENTRY): Cube ducks FIRST during 220ms silent lead-in
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

            if self._wait_cancelable(cancel_ev, 0.08):
                return {"ok": False, "canceled": True}

            # 5. Ramp Denon smoothly up to show target level (e.g., MV70 = -10.0 dB) right during lead-in
            if self.house_audio is not None and has_baseline and prev_db is not None:
                save_guard = getattr(self.house_audio.denon, "save_volume_guard", None)
                if callable(save_guard):
                    save_guard(prev_db)
                self.house_audio.denon._ramp_volume(prev_db, target_db, steps=4, step_ms=35)
                current_db = target_db
                denon_raised = True
            self._emit_cue(
                "intro",
                f"(DENON VOL {spec.denon_vol} · ANNOUNCER + FX)",
                show_id=spec.id,
                duration_ms=int(preview_dur * 1000),
                denonVol=spec.denon_vol,
                caption=spec.announcer_text,
            )

            # 6. Wait through intro audio, firing optional lightning crack flash during opening thunder
            intro_elapsed = 0.22
            if spec.lightning_flash_sec > 0.0 and spec.lightning_flash_sec < (preview_dur - 0.5):
                pre_flash_wait = max(0.0, spec.lightning_flash_sec - intro_elapsed)
                if self._wait_cancelable(cancel_ev, pre_flash_wait):
                    return {"ok": False, "canceled": True}
                self._flash_lights(
                    1,
                    cancel_ev=cancel_ev,
                    show_id=spec.id,
                    end_on=False,
                    on_sec=0.16,
                    off_sec=0.05,
                    tag_prefix="FLASH · LIGHTNING",
                )
                intro_elapsed += pre_flash_wait + 0.21

            rem_intro = max(0.15, preview_dur - intro_elapsed - 0.05)
            with self._lock:
                self._active_state["phase"] = "playing"
            if not (spec.three_screen_video and spec.youtube_url) and spec.lights_flash_count > 0:
                both_z = bool(
                    fx.resolve_lights_urls(True, "rack")
                    and fx.resolve_lights_urls(True, "smith")
                    and fx.resolve_lights_urls(True, "rack") != fx.resolve_lights_urls(True, "smith")
                )
                z_pr = "alternate" if both_z else "all"
                if self._has_custom_sleep:
                    self._flash_lights(
                        spec.lights_flash_count,
                        cancel_ev=cancel_ev,
                        show_id=spec.id,
                        end_on=True,
                        zone=z_pr,
                        tag_prefix="FLASH",
                    )
                else:
                    threading.Thread(
                        target=self._flash_lights,
                        args=(spec.lights_flash_count, cancel_ev),
                        kwargs={"show_id": spec.id, "end_on": True, "zone": z_pr, "tag_prefix": "FLASH"},
                        name=f"ArmoryPrFlash-{spec.id}",
                        daemon=True,
                    ).start()
            audio_fx_cues: list[tuple[float, dict[str, Any]]] = []
            if spec.fx_cues and not (spec.three_screen_video and spec.youtube_url):
                lead_l = max(0.0, float(fx.lights_lead_sec or 0.0))
                for fc in spec.fx_cues:
                    base_t = float(fc.get("atSec") or 0.0)
                    if base_t < preview_dur:
                        eff_t = max(intro_elapsed, base_t - lead_l) if fc.get("type") in ("lights_on", "lights_off") else max(intro_elapsed, base_t)
                        if eff_t < preview_dur:
                            audio_fx_cues.append((eff_t, dict(fc)))
                audio_fx_cues.sort(key=lambda item: item[0])

            if audio_fx_cues:
                cur_t = intro_elapsed
                end_t = max(intro_elapsed + 0.15, preview_dur - 0.05)
                for cue_t, fc in audio_fx_cues:
                    if cue_t > end_t:
                        break
                    wait_d = max(0.0, cue_t - cur_t)
                    if wait_d > 0.0 and self._wait_cancelable(cancel_ev, wait_d):
                        return {"ok": False, "canceled": True}
                    cur_t = max(cur_t, cue_t)
                    fx_type = str(fc.get("type") or "flash")
                    dur_fx = float(fc.get("durationSec") or 0.2)
                    cnt_fx = int(fc.get("count") or 1)
                    zone_fx = str(fc.get("zone") or "all")
                    if fx_type == "flash":
                        end_on_fx = bool(fc.get("endOn", True))
                        off_fx = max(0.1, min(5.0, float(fc.get("offSec") or 0.22)))
                        if fc.get("lightFlash", True):
                            self._flash_lights(
                                cnt_fx,
                                cancel_ev=cancel_ev,
                                show_id=spec.id,
                                end_on=end_on_fx,
                                on_sec=max(0.1, min(2.0, dur_fx)),
                                off_sec=off_fx,
                                tag_prefix="FLASH · CUSTOM",
                                zone=zone_fx,
                            )
                            if zone_fx == "all":
                                if end_on_fx:
                                    lights_restored = True
                                else:
                                    lights_turned_off = True
                                    lights_restored = False
                            cur_t += cnt_fx * max(0.1, min(2.0, dur_fx)) + (cnt_fx if not end_on_fx else max(0, cnt_fx - 1)) * off_fx
                    elif fx_type == "lights_off":
                        self._fire_lights_state(
                            False, zone=zone_fx, tag="lights_off", label="(LIGHTS OFF · FX)", show_id=spec.id, duration_ms=1200
                        )
                        if zone_fx == "all":
                            lights_turned_off = True
                            lights_restored = False
                    elif fx_type == "lights_on":
                        self._fire_lights_state(
                            True, zone=zone_fx, tag="lights_on", label="(LIGHTS ON · FX)", show_id=spec.id, duration_ms=1200
                        )
                        if zone_fx == "all":
                            lights_restored = True
                    elif fx_type == "fog":
                        b_str = f"{dur_fx:.1f}".rstrip("0").rstrip(".")
                        self._emit_cue(
                            "fog",
                            f"(TRIGGER FOG · {b_str}s)",
                            show_id=spec.id,
                            duration_ms=int(dur_fx * 1000),
                            fogSec=dur_fx,
                        )
                        fog_u = fx.resolve_fog_url(burst_sec=dur_fx)
                        if fog_u:
                            self._fire_http_hook(fog_u)
                tail_wait = max(0.0, end_t - cur_t)
                if tail_wait > 0.0 and self._wait_cancelable(cancel_ev, tail_wait):
                    return {"ok": False, "canceled": True}
            elif self._wait_cancelable(cancel_ev, rem_intro):
                return {"ok": False, "canceled": True}

            # 7A. Ryder / Debut 3-screen video + light strobe + 1:20 duck/overlay/MV55 choreography
            if spec.three_screen_video and spec.youtube_url:
                with self._lock:
                    self._active_state["phase"] = "video_screens"
                if spec.lights_flash_count > 0:
                    self._flash_lights(
                        spec.lights_flash_count,
                        cancel_ev=cancel_ev,
                        show_id=spec.id,
                        end_on=True,
                        tag_prefix="FLASH",
                    )
                out_res["threeScreen"] = self._launch_three_screen_youtube(
                    spec.youtube_url, exclude_main_tv=eff_exclude_main
                )
                wallboard_youtube_launched = True
                self._emit_cue(
                    "screens",
                    f"(ALL 3 SCREENS LIVE · DENON {spec.denon_vol})",
                    show_id=spec.id,
                    duration_ms=4000,
                    denonVol=spec.denon_vol,
                    youtubeUrl=spec.youtube_url,
                )

                video_elapsed = 0.0
                if spec.lights_pulse_duration_sec > 0.0:
                    with self._lock:
                        self._active_state["phase"] = "strobe_video"
                    if self._pulse_lights_loop(spec, cancel_ev):
                        return {"ok": False, "canceled": True}
                    video_elapsed += float(spec.lights_pulse_duration_sec)
                    self._fire_lights_state(
                        True,
                        tag="lights_on",
                        label="(FULL HOUSE LIGHTS ON)",
                        show_id=spec.id,
                        duration_ms=2500,
                    )
                    lights_restored = True
                elif spec.lights_out:
                    self._fire_lights_state(
                        True,
                        tag="lights_on",
                        label="(FULL HOUSE LIGHTS ON)",
                        show_id=spec.id,
                        duration_ms=2500,
                    )
                    lights_restored = True

                # Optional mid-video YouTube duck + personal overlay + Denon drop (e.g., 1:20 -> MV55)
                if spec.overlay_text and spec.overlay_at_sec > 0.0:
                    pre_ov_wait = max(0.0, float(spec.overlay_at_sec) - video_elapsed)
                    if pre_ov_wait > 0.0:
                        with self._lock:
                            self._active_state["phase"] = "video_main"
                        if self._wait_cancelable(cancel_ev, pre_ov_wait):
                            return {"ok": False, "canceled": True}
                        video_elapsed += pre_ov_wait

                    ov_info = self.build_show_overlay(spec.id, force=False)
                    ov_wav = self.overlay_wav_path(spec.id)
                    ov_dur = wav_duration_sec(ov_wav) if ov_wav.is_file() else 0.0
                    if ov_info.get("ok") and ov_wav.is_file() and ov_dur > 0.2:
                        with self._lock:
                            self._active_state["phase"] = "overlay_duck"
                        self._emit_cue(
                            "overlay",
                            f"(DUCK YOUTUBE + OVERLAY · \"{spec.overlay_text}\")",
                            show_id=spec.id,
                            duration_ms=int((ov_dur + 1.0) * 1000),
                            caption=spec.overlay_text,
                        )
                        ov_padded, ov_pad_dur = pad_wav_for_cube_duck(
                            ov_wav.read_bytes(), lead_silence_ms=220, tail_silence_ms=1400
                        )
                        if self.house_audio is not None and self.house_audio.get_config().cube_host:
                            cfg_ha = self.house_audio.get_config()
                            self.house_audio.cube.play_wav_bytes(
                                cfg_ha.cube_host,
                                cfg_ha.cube_port,
                                ov_padded,
                                mode="duck",
                                usage=cfg_ha.cube_usage,
                                gain=1.0,
                                max_sec=max(5, min(60, int(math.ceil(ov_pad_dur + 3.0)))),
                                sync=False,
                            )
                        if self._wait_cancelable(cancel_ev, max(0.2, 0.22 + ov_dur - 0.05)):
                            return {"ok": False, "canceled": True}
                        video_elapsed += max(0.2, 0.22 + ov_dur)

                        # Step Denon down to post_overlay_denon_vol (55) during 1.4s silent tail BEFORE un-duck!
                        if spec.post_overlay_denon_vol > 0:
                            post_db = _clamp(float(spec.post_overlay_denon_vol) - 80.0, -50.0, -5.0)
                            self._restore_denon_down(current_db, post_db, fast=False)
                            current_db = post_db
                            self._emit_cue(
                                "denon_drop",
                                f"(RESTORE YOUTUBE · DENON {spec.denon_vol} → {spec.post_overlay_denon_vol} · FULL LIGHTS ON)",
                                show_id=spec.id,
                                duration_ms=3000,
                                denonVol=spec.post_overlay_denon_vol,
                            )
                        if self._wait_cancelable(cancel_ev, 0.45):
                            return {"ok": False, "canceled": True}
                        video_elapsed += 0.45

                rem_video = max(0.0, float(spec.video_hold_sec) - video_elapsed)
                if rem_video > 0.0:
                    with self._lock:
                        self._active_state["phase"] = "video_finish"
                    if self._wait_cancelable(cancel_ev, rem_video):
                        return {"ok": False, "canceled": True}

                if spec.post_overlay_denon_vol <= 0 and denon_raised and has_baseline and prev_db is not None:
                    self._restore_denon_down(current_db, prev_db, fast=False)
                denon_restored = True
                if self.displays is not None:
                    try:
                        self.displays.sync_denon_monitor_for_spin()
                    except Exception:
                        pass
            else:
                # 7B. PHASE 2 (ANTI-ROLLER-COASTER EXIT):
                # Audio already faded smoothly to silence and entered 1400ms silent tail.
                # Ramp Denon BACK DOWN to prev_db WHILE Cube still holds MAY_DUCK!
                with self._lock:
                    self._active_state["phase"] = "restoring_denon"
                if denon_raised and has_baseline and prev_db is not None:
                    self._restore_denon_down(current_db, prev_db, fast=False)
                denon_restored = True
                self._wait_cancelable(cancel_ev, 0.35)

            self._emit_cue("ended", "(SHOW COMPLETE)", show_id=spec.id, duration_ms=1200)
            return out_res

        except Exception as exc:
            out_res = {"ok": False, "showId": spec.id, "error": str(exc)}
            return out_res
        finally:
            with self._lock:
                is_current = self._active_run_id == run_id
            if is_current:
                self._broadcast_stage_event("stage_stop", runId=run_id, showId=spec.id)
                self._cleanup_post_show_fx(
                    spec,
                    fx,
                    theater_started=theater_started,
                    lights_turned_off=lights_turned_off,
                    lights_restored=lights_restored,
                )
                # Emergency safety guarantee: ALWAYS ramp Denon back down FIRST before stopping Cube!
                if denon_raised and not denon_restored and has_baseline and prev_db is not None and self.house_audio is not None:
                    self._restore_denon_down(current_db, prev_db, fast=True)
                if wallboard_youtube_launched:
                    try:
                        self._restore_wallboard_tv(force_stop_youtube=True)
                    except Exception:
                        pass
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

    def fire_show(
        self,
        show_id: str,
        *,
        async_run: bool = True,
        exclude_main_tv: bool | None = None,
    ) -> dict[str, Any]:
        spec = self.get_show(show_id)
        if spec is None:
            return {"ok": False, "error": f"Unknown show: {show_id}"}
        if not spec.enabled:
            return {"ok": False, "error": f"Show '{spec.name}' is disabled"}

        self.stop_active_show(broadcast_stop=False)
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
            return self._run_show_sequence(spec, run_id, cancel_ev, exclude_main_tv=exclude_main_tv)

        t = threading.Thread(
            target=self._run_show_sequence,
            args=(spec, run_id, cancel_ev),
            kwargs={"exclude_main_tv": exclude_main_tv},
            name=f"ArmoryShow-{spec.id}",
            daemon=True,
        )
        with self._lock:
            self._active_thread = t
        t.start()
        return {"ok": True, "started": True, "showId": spec.id, "showName": spec.name}

    def stop_active_show(
        self,
        *,
        wait_timeout: float = 2.0,
        broadcast_stop: bool = True,
        restore_wallboard: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            ev = self._active_cancel
            t = self._active_thread
            was_active = bool(self._active_state.get("active"))
            run_id = int(self._active_run_id)
        if ev is not None:
            ev.set()
        with self._stage_ack_cond:
            self._stage_ack_cond.notify_all()
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
        if broadcast_stop:
            self._broadcast_stage_event("stage_stop", runId=run_id, showId="")
        with self._lock:
            stop_theater_url = self.fx_hooks.resolve_theater_stop_url() if was_active else ""
        if stop_theater_url:
            try:
                self._fire_http_hook(stop_theater_url)
            except Exception:
                pass
        if restore_wallboard and (not was_active or (t is not None and t.is_alive())):
            try:
                self._restore_wallboard_tv(force_stop_youtube=False)
            except Exception:
                pass
        return {"ok": True, "stopped": was_active}
