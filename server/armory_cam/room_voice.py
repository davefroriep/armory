"""OptiPlex Room Voice Engine — PowerConf S330 far-field mic capture, Silero VAD, faster-whisper ASR, and TTS readback.

Designed to live inside `ArmoryCamDVR` (`armory_cam_server.py`) and relay spoken commands to
Armory Fit (`POST /api/voice` / `GET /api/voice/events`) while automatically opening the
PowerConf S330 microphone ONLY during active Strength workouts (or explicit dashboard test /
"always" mode) via WASAPI Shared Mode when local so Windows 11 Voice Typing (`Win + H`)
stays unblocked.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict, deque
import concurrent.futures
import ctypes
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import site
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
import urllib.parse
import urllib.request
import wave

import numpy as np
from scipy.signal import lfilter, resample_poly
import sounddevice as sd

try:
    from .aec_linein import LineInAecProcessor
except ImportError:
    from aec_linein import LineInAecProcessor  # type: ignore[no-redef]

SAMPLE_RATE_16K = 16_000
VAD_FRAME_SAMPLES = 512  # 32 ms @ 16 kHz (native Silero VAD window)
VAD_CONTEXT_SAMPLES = 64
VAD_IDLE_RESET_FRAMES = 94  # ~3.0 s of idle non-speech -> reset Silero LSTM to prevent music drift
PRE_ROLL_FRAMES = 18     # ~576 ms pre-roll so far-field "Ar-" onset of "Armory" is never clipped
ARM_WINDOW_MS = 7_000
MAX_RECENT_TRANSCRIPTS = 12
HOST_API_PREFERENCE = (
    "Windows WASAPI",
    "MME",
    "Windows DirectSound",
    "Windows WDM-KS",
)
DEFAULT_TTS_VOICE = "kokoro:am_michael"
KOKORO_BLENDS: dict[str, dict[str, float]] = {
    "onyx_fenrir_epic": {"am_onyx": 0.55, "am_fenrir": 0.45},
    "onyx_george_hbo": {"am_onyx": 0.55, "bm_george": 0.45},
}
ATLAS_VOICE_CATALOG: tuple[dict[str, str], ...] = (
    # Local GPU Instant (~70-110 ms, $0, 100% offline)
    {
        "id": "kokoro:am_onyx",
        "slug": "kokoro_am_onyx",
        "name": "Onyx",
        "tier": "local",
        "engine": "Kokoro GPU",
        "latency": "~80 ms · Offline · Free",
        "vibe": "Deep commanding baritone — grounded, heavy Atlas presence.",
    },
    {
        "id": "kokoro:am_fenrir",
        "slug": "kokoro_am_fenrir",
        "name": "Fenrir",
        "tier": "local",
        "engine": "Kokoro GPU",
        "latency": "~80 ms · Offline · Free",
        "vibe": "Bold, resonant, slightly punchier power-announcer cadence.",
    },
    {
        "id": "kokoro:onyx_fenrir_epic",
        "slug": "kokoro_onyx_fenrir_epic",
        "name": "Onyx + Fenrir Blend",
        "tier": "local",
        "engine": "Kokoro GPU Blend",
        "latency": "~85 ms · Offline · Free",
        "vibe": "55/45 custom blend — deep Onyx rumble + Fenrir punch.",
    },
    {
        "id": "kokoro:onyx_george_hbo",
        "slug": "kokoro_onyx_george_hbo",
        "name": "Onyx + George Blend",
        "tier": "local",
        "engine": "Kokoro GPU Blend",
        "latency": "~85 ms · Offline · Free",
        "vibe": "55/45 deep baritone with subtle British Jarvis polish.",
    },
    {
        "id": "kokoro:bm_george",
        "slug": "kokoro_bm_george",
        "name": "George (UK)",
        "tier": "local",
        "engine": "Kokoro GPU",
        "latency": "~80 ms · Offline · Free",
        "vibe": "Crisp British AI commander (closest local Jarvis feel).",
    },
    {
        "id": "kokoro:am_adam",
        "slug": "kokoro_am_adam",
        "name": "Adam (Local)",
        "tier": "local",
        "engine": "Kokoro GPU",
        "latency": "~80 ms · Offline · Free",
        "vibe": "Clean, assertive American male voice.",
    },
    {
        "id": "kokoro:am_michael",
        "slug": "kokoro_am_michael",
        "name": "Michael (Current)",
        "tier": "local",
        "engine": "Kokoro GPU",
        "latency": "~80 ms · Offline · Free",
        "vibe": "Your current baseline voice for A/B comparison.",
    },
    # ElevenLabs Flash v2.5 Studio HD (~300-450 ms fresh, 0 ms cached locally)
    {
        "id": "elevenlabs:pNInz6obpgDQGcFmaJgB",
        "slug": "el_adam",
        "name": "Adam Studio",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Dominant, firm, ultra-clean studio strength coach.",
    },
    {
        "id": "elevenlabs:nPczCjzI2devNBz1zQrb",
        "slug": "el_brian",
        "name": "Brian Studio",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Deep, resonant broadcast voice — effortless authority.",
    },
    {
        "id": "elevenlabs:SOYHLrjzK2X1ezoPC6cr",
        "slug": "el_harry",
        "name": "Harry Studio",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Fierce, intense warrior energy — true Atlas vibe.",
    },
    {
        "id": "elevenlabs:pqHfZKP75CvOlQylNhV4",
        "slug": "el_bill",
        "name": "Bill Studio",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Mature, gravelly, wise authoritative baritone.",
    },
    {
        "id": "elevenlabs:IKne3meq5aSn9XLyUdCD",
        "slug": "el_charlie",
        "name": "Charlie Studio",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Deep, confident, natural conversational partner.",
    },
    {
        "id": "elevenlabs:JBFqnCBsd6RMkjVDRZzb",
        "slug": "el_george",
        "name": "George Studio (UK)",
        "tier": "elevenlabs",
        "engine": "ElevenLabs Flash v2.5",
        "latency": "~350 ms · 0 ms cached",
        "vibe": "Warm, rich Hollywood-grade British AI.",
    },
)
TTS_VOICES = (
    "kokoro:am_michael",
    "kokoro:am_onyx",
    "kokoro:am_fenrir",
    "kokoro:onyx_fenrir_epic",
    "kokoro:onyx_george_hbo",
    "kokoro:bm_george",
    "kokoro:am_adam",
    "kokoro:am_echo",
    "kokoro:am_eric",
    "elevenlabs:pNInz6obpgDQGcFmaJgB",
    "elevenlabs:nPczCjzI2devNBz1zQrb",
    "elevenlabs:SOYHLrjzK2X1ezoPC6cr",
    "elevenlabs:pqHfZKP75CvOlQylNhV4",
    "elevenlabs:IKne3meq5aSn9XLyUdCD",
    "elevenlabs:JBFqnCBsd6RMkjVDRZzb",
    "elevenlabs:onwK4e9ZLuTAKqWW03F9",
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
)
_ELEVENLABS_VOICE_RE = re.compile(r"^elevenlabs:[A-Za-z0-9_-]{6,64}$")
_ELEVENLABS_LIVE_MODEL = "eleven_flash_v2_5"
_ELEVENLABS_TIMEOUT_SEC = 5.0
_LEGACY_TTS_VOICE_MAP = {
    "en-US-AndrewNeural": "en-US-AndrewMultilingualNeural",
    "en-US-AvaNeural": "en-US-AvaMultilingualNeural",
    "sapi": DEFAULT_TTS_VOICE,
}
_KOKORO_NEGATIVE_TTL_SEC = 60.0
_EDGE_TTS_TIMEOUT_SEC = 3.5
PREWARM_TTS_PHRASES: tuple[str, ...] = (
    "App not connected",
    "Can't find session",
    "Armory room mic ready",
    "Armory mic on",
    "Didn't catch that",
    "Need weight and reps",
    "Start a workout first",
    "Open a workout first",
    "Open active workout",
    "Didn't find that exercise",
    "Could not log set",
    "No rest running",
    "Not running",
    "Not paused",
    "Nothing to undo",
    "Undid last set",
    "Undid last",
    "No empty sets",
    "All sets logged",
    "Workout complete",
    "Rest skipped",
    "Rest started",
    "Added 30 seconds",
    "Minus 30 seconds",
    "Set added",
    "Next exercise",
    "Previous exercise",
    "Recording",
    "Recording Rack",
    "Recording Smith",
    "Guest recording",
    "Clip saved",
    "Clipping last set",
    "No other views",
    "No video playing",
    "Buddy mode on",
    "Buddy mode off",
    "Workout paused",
    "Workout resumed",
)

HALLUCINATION_EXACT = frozenset({
    "",
    "you",
    "thank you",
    "thank you.",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "thank you very much",
    "thanks so much",
    "bye",
    "bye.",
    "bye-bye",
    "goodbye",
    "see you next time",
    "subscribe",
    "please subscribe",
    "the end",
    "silence",
    "music",
    "applause",
    "hmm",
    "uh",
    "um",
    "blank audio",
    "blankaudio",
    "youre welcome",
    "you're welcome",
})


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _no_window_kwargs() -> dict[str, Any]:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def is_hallucination(text: str) -> bool:
    """Reject known Whisper silence/noise hallucinations, repetition loops, and punctuation-only strings."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return True
    if cleaned in HALLUCINATION_EXACT:
        return True
    words = cleaned.split()
    if len(words) >= 3 and len(set(words)) == 1 and words[0] in {"you", "the", "a", "uh", "um", "bye"}:
        return True
    if len(words) >= 6:
        top_count = Counter(words).most_common(1)[0][1]
        if top_count * 5 >= len(words) * 3:
            return True
    return False


def is_prompt_echo(text: str, prompt: str) -> bool:
    """Detect when Whisper regurgitates a long run of the `initial_prompt` vocabulary list."""
    t_toks = re.findall(r"[a-z0-9]+", str(text or "").lower())
    if len(t_toks) < 8:
        return False
    p_toks = set(re.findall(r"[a-z0-9]+", str(prompt or "").lower()))
    if not p_toks:
        return False
    matched = sum(1 for tok in t_toks if tok in p_toks)
    return (matched / float(len(t_toks))) >= 0.70


def normalize_wake_text(text: str) -> str:
    """Lightweight normalization aligned with `src/lifts/voiceParser.ts` wake matching."""
    s = text.lower()
    s = re.sub(r"[—–\-/,]+", " ", s)
    s = re.sub(r"['’]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve_oww_model_name(wake_word: str) -> str:
    """Map configured wake phrase (e.g. 'armory', 'hey armory', 'hey jarvis', 'hey atlas') to its ONNX model stem."""
    w = re.sub(r"\s+", " ", (wake_word or "hey jarvis").strip().lower())
    if w in ("armory", "armoury"):
        return "armory_v0.1"
    if "armory" in w or "armoury" in w:
        return "hey_armory_v0.1"
    if "jarvis" in w:
        return "hey_jarvis_v0.1"
    if "atlas" in w:
        return "hey_atlas_v0.1"
    slug = re.sub(r"[^a-z0-9]+", "_", w).strip("_") or "hey_jarvis"
    return f"{slug}_v0.1"


def _wake_regex(wake_word: str) -> re.Pattern[str]:
    wake_raw = (wake_word or "armory").strip().lower() or "armory"
    if "armory" in wake_raw or "armoury" in wake_raw:
        alts = [
            "hey armory",
            "hey armoury",
            "ok armory",
            "okay armory",
            "armory",
            "armoury",
            "armorys",
            "armourys",
            "armory fit",
            "armoryfit",
            "amory",
            "emory",
            "army",
            "arm re",
            "arm ree",
            "armery",
            "armary",
            "ormory",
            "armorie",
            "armor",
            "armored",
        ]
    elif "jarvis" in wake_raw:
        alts = [
            "hey jarvis",
            "ok jarvis",
            "okay jarvis",
            "hey jarvas",
            "hey jarvus",
            "hey jarves",
            "jarvis",
            "jarvas",
            "jarvus",
            "jarves",
            "jarvish",
        ]
    elif "atlas" in wake_raw:
        alts = ["hey atlas", "ok atlas", "okay atlas", "atlas"]
    else:
        alts = [wake_raw]
    joined = "|".join(re.escape(a) for a in alts)
    return re.compile(rf"\b(?:hey\s+|ok\s+|okay\s+)?(?:{joined})\b", re.IGNORECASE)


def format_wake_display(wake_word: str) -> str:
    clean = re.sub(r"\s+", " ", (wake_word or "armory").strip())
    if not clean:
        return "Armory"
    return " ".join(w[0].upper() + w[1:] for w in clean.split(" "))


def strip_wake_word(text: str, wake_word: str = "armory") -> str:
    """Strip any leading wake word + punctuation, returning the clean command tail."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""
    pat = _wake_regex(wake_word)
    # Strip leading occurrences of the wake word (and trailing comma/colon/period right after wake)
    cur = raw
    for _ in range(2):
        m = pat.match(cur)
        if not m:
            break
        cur = cur[m.end():].lstrip(" ,.!?:;—–-")
    return cur.strip()


def matches_wake_word(text: str, wake_word: str = "armory") -> bool:
    """Return True if `text` contains the configured wake word (or common ASR variants)."""
    norm = normalize_wake_text(text)
    if not norm:
        return False
    return bool(_wake_regex(wake_word).search(norm))


def has_command_tail(text: str, wake_word: str = "armory") -> bool:
    """Return True if user spoke a command after the wake word in the same utterance."""
    norm = normalize_wake_text(text)
    if not norm:
        return False
    last_end = -1
    for m in _wake_regex(wake_word).finditer(norm):
        last_end = m.end()
    if last_end < 0:
        return False
    return bool(norm[last_end:].strip(" .,!?:;"))


def build_whisper_prompt(wake_word: str, bias_phrases: Sequence[str] = ()) -> tuple[str, str]:
    """Build concise Whisper `initial_prompt` + `hotwords` led by wake-word gym commands and current exercises."""
    wake = (wake_word or "armory").strip() or "armory"
    wake_cap = format_wake_display(wake)
    # Always lead `initial_prompt` with natural punctuated wake+command pairs so Whisper biases
    # strongly toward the wake word over music rather than substituting show names ("Ryder").
    lead_sentences = (
        f"{wake_cap}, record this. "
        f"{wake_cap}, done. "
        f"{wake_cap}, check. "
    )
    core = [
        "start hold",
        "rpe",
        "next exercise",
        "add set",
        "skip rest",
        "record rack",
        "clip that",
        "replay slow",
        "switch views",
        "buddy mode",
        "undo",
        "undo last set",
        "back",
        "go back",
        "open the gym",
        "close the gym",
        "shut down",
        "SmartSpin TV",
        "Fire TV Cube",
        "wallboard",
        "play music",
        "pause music",
        "next song",
        "I just hit a PR",
        "hit a PR",
        "new PR",
        "I PR'd",
        "we have guests",
        "welcome to the Armory",
        "Matt just got here",
        "Ryder is here",
        "Jenna is here",
        "Avenleigh is here",
        "Jared and Mark",
        "stop show",
        "rack lights",
        "smith lights",
    ]
    seen = {x.lower() for x in core}
    seen.add(wake.lower())
    extras: list[str] = []
    for raw in bias_phrases:
        clean = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        extras.append(clean)
        if len(extras) >= 12:
            break
    all_terms = core + extras
    prompt = lead_sentences + ", ".join(all_terms) + "."
    hotwords = ", ".join([
        wake_cap, "record this", "done", "check", "start hold", "skip rest", "next exercise",
        "add set", "undo", "back", "rack", "smith", "replay", "switch views", "play music",
        "lights", "PR", "open the gym", "close the gym", "shut down",
        *extras[:6],
    ])
    return prompt, hotwords


@dataclass
class RoomVoiceConfig:
    enabled: bool = True
    device_match: str = "PowerConf S330"
    speaker_match: str = "PowerConf S330"
    listen_mode: str = "workout"  # "workout" | "always"
    model_size: str = "small.en"  # empirically 18/18 (100%) over gym music on GTX 1650 CUDA
    beam_size: int = 1
    wake_word: str = "armory"
    wake_engine: str = "whisper"  # "whisper" | "openwakeword"
    wake_threshold: float = 0.38
    rescue_threshold: float = 0.10  # Two-Tier Silent Rescue Gate: 0.10..wake_threshold checks Whisper w/o ducking (0=off)
    aec_enabled: bool = True
    aec_device_match: str = "Line In"
    speaker_readback: bool = True
    room_command_readback: bool = True
    tts_voice: str = DEFAULT_TTS_VOICE
    set_commit_mode: str = "voice"  # "voice" | "chime"
    vad_threshold: float = 0.48
    min_speech_ms: int = 280
    end_silence_ms: int = 580
    max_utterance_sec: float = 4.8
    rms_gate: float = 0.0025

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "deviceMatch": self.device_match,
            "speakerMatch": self.speaker_match,
            "listenMode": self.listen_mode,
            "modelSize": self.model_size,
            "beamSize": int(self.beam_size),
            "wakeWord": self.wake_word,
            "wakeEngine": self.wake_engine,
            "wakeThreshold": round(float(self.wake_threshold), 2),
            "rescueThreshold": round(float(self.rescue_threshold), 2),
            "aecEnabled": bool(self.aec_enabled),
            "aecDeviceMatch": self.aec_device_match,
            "speakerReadback": bool(self.speaker_readback),
            "roomCommandReadback": bool(self.room_command_readback),
            "ttsVoice": self.tts_voice,
            "setCommitMode": self.set_commit_mode,
            "vadThreshold": round(float(self.vad_threshold), 2),
            "minSpeechMs": int(self.min_speech_ms),
            "endSilenceMs": int(self.end_silence_ms),
            "maxUtteranceSec": round(float(self.max_utterance_sec), 1),
            "rmsGate": round(float(self.rms_gate), 4),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> RoomVoiceConfig:
        if not isinstance(raw, dict):
            return cls()
        mode_raw = str(raw.get("listenMode", "workout")).strip().lower()
        listen_mode = "always" if mode_raw == "always" else "workout"
        model_raw = str(raw.get("modelSize", "small.en")).strip()
        model_size = model_raw if model_raw in ("tiny.en", "base.en", "small.en", "medium.en", "distil-large-v3") else "small.en"
        try:
            beam_size = max(1, min(5, int(raw.get("beamSize", 1) or 1)))
        except (TypeError, ValueError):
            beam_size = 1
        wake = str(raw.get("wakeWord") or "armory").strip().lower() or "armory"
        eng_raw = str(raw.get("wakeEngine", "whisper") or "whisper").strip().lower()
        wake_engine = "openwakeword" if eng_raw in ("openwakeword", "oww") else "whisper"
        tts_raw = str(raw.get("ttsVoice") or DEFAULT_TTS_VOICE).strip()
        tts_raw = _LEGACY_TTS_VOICE_MAP.get(tts_raw, tts_raw)
        if tts_raw in TTS_VOICES or _ELEVENLABS_VOICE_RE.match(tts_raw):
            tts_voice = tts_raw
        else:
            tts_voice = DEFAULT_TTS_VOICE
        commit_raw = str(raw.get("setCommitMode", "voice")).strip().lower()
        set_commit_mode = "chime" if commit_raw == "chime" else "voice"
        return cls(
            enabled=bool(raw.get("enabled", True)),
            device_match=str(raw.get("deviceMatch", "PowerConf S330") or "").strip(),
            speaker_match=str(raw.get("speakerMatch", "PowerConf S330") or "").strip(),
            listen_mode=listen_mode,
            model_size=model_size,
            beam_size=beam_size,
            wake_word=wake,
            wake_engine=wake_engine,
            wake_threshold=_clamp(float(raw.get("wakeThreshold", 0.38) if raw.get("wakeThreshold") is not None else 0.38), 0.15, 0.90),
            rescue_threshold=_clamp(float(raw.get("rescueThreshold", 0.10) if raw.get("rescueThreshold") is not None else 0.10), 0.0, 0.40),
            aec_enabled=bool(raw.get("aecEnabled", True)),
            aec_device_match=str(raw.get("aecDeviceMatch", "Line In") or "").strip(),
            speaker_readback=bool(raw.get("speakerReadback", True)),
            room_command_readback=bool(raw.get("roomCommandReadback", True)),
            tts_voice=tts_voice,
            set_commit_mode=set_commit_mode,
            vad_threshold=_clamp(float(raw.get("vadThreshold", 0.48) or 0.48), 0.15, 0.90),
            min_speech_ms=max(160, min(1200, int(raw.get("minSpeechMs", 280) or 280))),
            end_silence_ms=max(250, min(2000, int(raw.get("endSilenceMs", 580) or 580))),
            max_utterance_sec=_clamp(float(raw.get("maxUtteranceSec", 4.8) or 4.8), 2.5, 15.0),
            rms_gate=_clamp(float(raw.get("rmsGate", 0.0025) or 0.0025), 0.0005, 0.05),
        )


_SET_COMMIT_SPEECH_RE = re.compile(r"(?:,\s*(?:check|got it)|^set checked)$")


def is_set_commit_ack(text: str = "", kind: str | None = None, speech: str = "") -> bool:
    """Return True when an ack corresponds to a logged strength set (uses fast studio chime)."""
    clean_text = str(text or "").strip()
    if clean_text.startswith("✓"):
        return True
    clean_kind = str(kind or "").strip().lower()
    clean_speech = str(speech or "").strip().lower()
    return clean_kind == "ok" and bool(_SET_COMMIT_SPEECH_RE.search(clean_speech))


def _load_commit_chime_pcm(root_dir: Path | None = None) -> tuple[np.ndarray, int]:
    """Load the studio set-commit chime WAV (custom override or Windows 11 studio chime)."""
    candidates: list[Path] = []
    if root_dir is not None:
        candidates.append(Path(root_dir) / "sfx" / "commit_chime.wav")
    candidates.append(Path(r"C:\Windows\Media\Windows Default.wav"))

    for wav_p in candidates:
        if not wav_p.is_file():
            continue
        try:
            with wave.open(str(wav_p), "rb") as wf:
                sr = int(wf.getframerate() or 44_100)
                ch = max(1, int(wf.getnchannels() or 1))
                sw = int(wf.getsampwidth() or 2)
                raw = wf.readframes(wf.getnframes())
            if not raw:
                continue
            if sw == 1:
                arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            elif sw == 2:
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            elif sw == 4:
                arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                continue
            if ch > 1:
                trim_len = (arr.size // ch) * ch
                arr = arr[:trim_len].reshape(-1, ch).mean(axis=1)
            arr = arr.astype(np.float32, copy=False)
            max_samples = int(round(sr * 0.42))
            if arr.size > int(round(sr * 0.38)):
                nz = np.flatnonzero(np.abs(arr) > 0.004)
                tail_end = int(nz[-1]) + int(round(sr * 0.03)) if nz.size else int(round(sr * 0.28))
                keep = min(arr.size, max(min(tail_end, max_samples), int(round(sr * 0.25))))
                if keep < arr.size:
                    arr = arr[:keep].copy()
                    fade_n = min(int(round(sr * 0.010)), arr.size)
                    if fade_n > 1:
                        arr[-fade_n:] *= np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
            if arr.size > 0:
                return arr, sr
        except Exception:
            continue

    sr = 48_000
    n = int(round(sr * 0.22))
    t = np.linspace(0.0, 0.22, n, endpoint=False, dtype=np.float32)
    env = np.exp(-14.0 * t).astype(np.float32)
    fade_in = min(int(round(sr * 0.004)), n)
    if fade_in > 1:
        env[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    tone = (0.65 * np.sin(2.0 * np.pi * 880.0 * t) + 0.35 * np.sin(2.0 * np.pi * 1318.5 * t)) * env
    return tone.astype(np.float32), sr


_CUDA_DLL_HANDLES: list[Any] = []
_CUDA_DLL_REGISTERED: set[str] = set()


def _register_cuda_dll_dirs() -> list[str]:
    """Register user/system pip NVIDIA CUDA 12 bin directories for ctranslate2 on Windows."""
    added: list[str] = []
    roots: list[str] = []
    try:
        user_site = site.getusersitepackages()
        if user_site:
            roots.append(str(user_site))
    except Exception:
        pass
    try:
        roots.extend(str(p) for p in site.getsitepackages())
    except Exception:
        pass
    path_parts = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    for root in roots:
        nv = Path(root) / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in sorted(nv.glob("*/bin")):
            if not bin_dir.is_dir():
                continue
            s_dir = str(bin_dir)
            if s_dir not in _CUDA_DLL_REGISTERED:
                _CUDA_DLL_REGISTERED.add(s_dir)
                if hasattr(os, "add_dll_directory"):
                    try:
                        _CUDA_DLL_HANDLES.append(os.add_dll_directory(s_dir))
                    except OSError:
                        pass
            if s_dir not in path_parts:
                path_parts.insert(0, s_dir)
            added.append(s_dir)
    if path_parts:
        os.environ["PATH"] = os.pathsep.join(path_parts)
    return added


def _load_whisper_model(model_size: str) -> tuple[Any, str]:
    """Load `faster-whisper` on CUDA (verified via warmup) when available, else CPU `int8`."""
    _register_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    has_cuda = False
    try:
        import ctranslate2

        has_cuda = int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        has_cuda = False

    if has_cuda:
        warmup_pcm = np.zeros(SAMPLE_RATE_16K // 2, dtype=np.float32)
        for ct in ("int8_float16", "float16"):
            try:
                model = WhisperModel(model_size, device="cuda", compute_type=ct)
                segs, _ = model.transcribe(
                    warmup_pcm,
                    language="en",
                    beam_size=1,
                    best_of=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    max_new_tokens=4,
                )
                _ = list(segs)
                return model, f"cuda/{ct}"
            except Exception as exc:
                logging.getLogger("armory-voice").warning(
                    "CUDA faster-whisper %s (%s) unavailable, trying fallback: %s",
                    model_size,
                    ct,
                    exc,
                )

    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)
    return model, "cpu/int8"


_PORTAUDIO_LOCK = threading.RLock()
_AUDIO_DEVICES_CACHE: dict[str, list[dict[str, Any]]] | None = None
_AUDIO_DEVICES_TS: float = 0.0
AUDIO_DEVICES_CACHE_TTL_SEC = 15.0


def list_audio_devices(force_refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Return sanitized audio input and output devices grouped with host API names (cached & locked)."""
    global _AUDIO_DEVICES_CACHE, _AUDIO_DEVICES_TS
    with _PORTAUDIO_LOCK:
        now = time.monotonic()
        if (
            not force_refresh
            and _AUDIO_DEVICES_CACHE is not None
            and (now - _AUDIO_DEVICES_TS) < AUDIO_DEVICES_CACHE_TTL_SEC
        ):
            return {
                "inputs": [dict(d) for d in _AUDIO_DEVICES_CACHE["inputs"]],
                "outputs": [dict(d) for d in _AUDIO_DEVICES_CACHE["outputs"]],
            }
        inputs: list[dict[str, Any]] = []
        outputs: list[dict[str, Any]] = []
        try:
            devs = sd.query_devices()
            host_cache: dict[int, str] = {}
            for idx, d in enumerate(devs):
                ha_idx = int(d.get("hostapi", -1))
                if ha_idx not in host_cache:
                    try:
                        host_cache[ha_idx] = str(sd.query_hostapis(ha_idx).get("name", ""))
                    except Exception:
                        host_cache[ha_idx] = ""
                host_name = host_cache[ha_idx]
                item_base = {
                    "index": idx,
                    "name": str(d.get("name", "")),
                    "hostApi": host_name,
                    "sampleRate": int(round(float(d.get("default_samplerate") or 48000))),
                }
                if int(d.get("max_input_channels") or 0) > 0:
                    inputs.append({**item_base, "channels": int(d["max_input_channels"])})
                if int(d.get("max_output_channels") or 0) > 0:
                    outputs.append({**item_base, "channels": int(d["max_output_channels"])})
        except Exception:
            pass
        _AUDIO_DEVICES_CACHE = {"inputs": inputs, "outputs": outputs}
        _AUDIO_DEVICES_TS = time.monotonic()
        return {
            "inputs": [dict(d) for d in inputs],
            "outputs": [dict(d) for d in outputs],
        }


WAVE_MAPPER_ID = 0xFFFFFFFF
_WHDR_DONE = 0x00000001
_WAVE_FORMAT_PCM = 1
_CALLBACK_NULL = 0x00000000


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_uint16),
        ("wPid", ctypes.c_uint16),
        ("vDriverVersion", ctypes.c_uint32),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", ctypes.c_uint32),
        ("wChannels", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("dwSupport", ctypes.c_uint32),
    ]


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_uint16),
        ("nChannels", ctypes.c_uint16),
        ("nSamplesPerSec", ctypes.c_uint32),
        ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_uint16),
        ("wBitsPerSample", ctypes.c_uint16),
        ("cbSize", ctypes.c_uint16),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint32),
        ("dwBytesRecorded", ctypes.c_uint32),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", ctypes.c_uint32),
        ("dwLoops", ctypes.c_uint32),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


def _enum_winmm_output_devices() -> list[tuple[int, str]]:
    if sys.platform != "win32":
        return []
    try:
        winmm = ctypes.WinDLL("winmm")
        winmm.waveOutGetNumDevs.restype = ctypes.c_uint
        winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint]
        winmm.waveOutGetDevCapsW.restype = ctypes.c_uint
        count = int(winmm.waveOutGetNumDevs())
        out: list[tuple[int, str]] = []
        for idx in range(count):
            caps = _WAVEOUTCAPSW()
            if winmm.waveOutGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                out.append((idx, str(caps.szPname or "")))
        return out
    except Exception:
        return []


def _find_winmm_device_id(
    match_substr: str,
    wave_devices: Sequence[tuple[int, str]] | None = None,
) -> int | None:
    """Resolve Windows waveOut device ID safely handling 31-character szPname truncation."""
    devs = list(wave_devices) if wave_devices is not None else _enum_winmm_output_devices()
    q = re.sub(r"\s+", " ", (match_substr or "").strip().lower())
    if not q:
        return WAVE_MAPPER_ID if devs else None

    for dev_id, name in devs:
        if q in name.lower():
            return dev_id

    # Handle 31-char WAVEOUTCAPSW truncation (e.g. 'Speakers (2- Anker PowerConf S3')
    tokens = [t for t in re.split(r"[\s\-_]+", q) if t]
    for dev_id, name in devs:
        n_low = name.lower()
        truncated = len(name) >= 30
        if truncated and tokens:
            tail_words = [w for w in re.split(r"[\s\-_]+", n_low) if w]
            last_w = tail_words[-1] if tail_words else ""
            if all(
                (tok in n_low) or (len(last_w) >= 2 and tok.startswith(last_w))
                for tok in tokens
            ):
                return dev_id

    for kw in ("powerconf", "anker"):
        if kw in q or "s330" in q:
            for dev_id, name in devs:
                if kw in name.lower():
                    return dev_id
    return None


class _WinMmSpeakerPlayer:
    """Lock-free-of-PortAudio Windows `winmm.dll` waveOut speaker player with instant cancel."""

    def __init__(self) -> None:
        self._play_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cancel_ev = threading.Event()
        self._active_handle: ctypes.c_void_p | None = None
        self._winmm: Any = None

    def _get_winmm(self) -> Any:
        if self._winmm is not None:
            return self._winmm
        if sys.platform != "win32":
            return None
        w = ctypes.WinDLL("winmm")
        w.waveOutOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_uint32,
        ]
        w.waveOutOpen.restype = ctypes.c_uint
        w.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        w.waveOutPrepareHeader.restype = ctypes.c_uint
        w.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        w.waveOutWrite.restype = ctypes.c_uint
        w.waveOutReset.argtypes = [ctypes.c_void_p]
        w.waveOutReset.restype = ctypes.c_uint
        w.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        w.waveOutUnprepareHeader.restype = ctypes.c_uint
        w.waveOutClose.argtypes = [ctypes.c_void_p]
        w.waveOutClose.restype = ctypes.c_uint
        self._winmm = w
        return w

    def stop(self) -> None:
        self._cancel_ev.set()
        with self._state_lock:
            h = self._active_handle
        if h is not None and self._winmm is not None:
            try:
                self._winmm.waveOutReset(h)
            except Exception:
                pass

    def play(
        self,
        pcm_f32: np.ndarray,
        sample_rate: int,
        channels: int,
        match_substr: str,
    ) -> bool:
        winmm = self._get_winmm()
        if winmm is None:
            return False
        dev_id = _find_winmm_device_id(match_substr)
        if dev_id is None:
            return False

        arr = np.ascontiguousarray(pcm_f32, dtype=np.float32)
        if arr.size == 0:
            return False
        if arr.ndim == 1 and channels > 1:
            arr = np.repeat(arr[:, None], channels, axis=1)
        elif arr.ndim == 2 and arr.shape[1] != channels:
            mono = arr.mean(axis=1)
            arr = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono
        pcm16 = np.clip(np.round(arr * 32767.0), -32768, 32767).astype("<i2")
        raw_bytes = pcm16.tobytes()
        n_frames = arr.shape[0]
        dur_sec = float(n_frames) / float(max(1, sample_rate))

        # Cancel any prior playback cleanly before taking the serialized playback lock
        self.stop()
        with self._play_lock:
            self._cancel_ev.clear()
            wfx = _WAVEFORMATEX(
                wFormatTag=_WAVE_FORMAT_PCM,
                nChannels=max(1, int(channels)),
                nSamplesPerSec=max(8000, int(sample_rate)),
                nAvgBytesPerSec=max(8000, int(sample_rate)) * max(1, int(channels)) * 2,
                nBlockAlign=max(1, int(channels)) * 2,
                wBitsPerSample=16,
                cbSize=0,
            )
            h_wave = ctypes.c_void_p()
            rc_open = winmm.waveOutOpen(
                ctypes.byref(h_wave),
                ctypes.c_uint(int(dev_id)),
                ctypes.byref(wfx),
                0,
                0,
                _CALLBACK_NULL,
            )
            if rc_open != 0 or not h_wave.value:
                return False

            buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
            hdr = _WAVEHDR(
                lpData=ctypes.addressof(buf),
                dwBufferLength=len(raw_bytes),
                dwBytesRecorded=0,
                dwUser=0,
                dwFlags=0,
                dwLoops=0,
                lpNext=None,
                reserved=0,
            )
            prepared = False
            try:
                if winmm.waveOutPrepareHeader(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                    return False
                prepared = True
                with self._state_lock:
                    self._active_handle = h_wave
                if winmm.waveOutWrite(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 0:
                    return False
                deadline = time.monotonic() + dur_sec + 1.5
                while not (hdr.dwFlags & _WHDR_DONE):
                    if self._cancel_ev.is_set() or time.monotonic() >= deadline:
                        break
                    self._cancel_ev.wait(0.015)
                if not (hdr.dwFlags & _WHDR_DONE):
                    winmm.waveOutReset(h_wave)
                return True
            finally:
                with self._state_lock:
                    if self._active_handle == h_wave:
                        self._active_handle = None
                if prepared:
                    try:
                        winmm.waveOutUnprepareHeader(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr))
                    except Exception:
                        pass
                try:
                    winmm.waveOutClose(h_wave)
                except Exception:
                    pass


def resolve_audio_device(
    kind: str,
    match_substr: str,
    devices_snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Pick the best matching audio endpoint following `HOST_API_PREFERENCE`."""
    snap = devices_snapshot if devices_snapshot is not None else list_audio_devices()
    pool = snap["inputs"] if kind == "in" else snap["outputs"]
    if not pool:
        return None

    def _pick_for_query(needle: str) -> dict[str, Any] | None:
        q = needle.strip().lower()
        matches = [d for d in pool if q in d["name"].lower()] if q else list(pool)
        if not matches:
            return None
        for host in HOST_API_PREFERENCE:
            for d in matches:
                if d["hostApi"] == host:
                    return d
        return matches[0]

    raw_q = (match_substr or "").strip()
    if raw_q:
        hit = _pick_for_query(raw_q)
        if hit:
            return hit
        for fallback_q in ("powerconf", "anker"):
            if fallback_q not in raw_q.lower():
                hit = _pick_for_query(fallback_q)
                if hit:
                    return hit
        return None

    # Empty match_substr -> use default device if available, else preferred PowerConf/first
    try:
        def_pair = sd.default.device
        def_idx = int(def_pair[0] if kind == "in" else def_pair[1])
        for d in pool:
            if d["index"] == def_idx:
                return d
    except Exception:
        pass
    return _pick_for_query("powerconf") or _pick_for_query("")


def resample_to_mono_16k(pcm: np.ndarray, src_sr: int) -> np.ndarray:
    """Downmix multichannel float32 audio and resample cleanly to 16 kHz mono."""
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    if arr.size == 0 or src_sr == SAMPLE_RATE_16K:
        return arr
    g = math.gcd(SAMPLE_RATE_16K, int(src_sr))
    up = SAMPLE_RATE_16K // g
    down = int(src_sr) // g
    return resample_poly(arr, up, down).astype(np.float32)


def resample_from_to(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample 1D float32 audio between arbitrary rates."""
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0 or src_sr == dst_sr:
        return arr
    g = math.gcd(int(dst_sr), int(src_sr))
    return resample_poly(arr, int(dst_sr) // g, int(src_sr) // g).astype(np.float32)


def prepare_asr_audio(audio16: np.ndarray) -> np.ndarray:
    """High-pass (~85 Hz) and normalize quiet far-field speech before Whisper decoding."""
    arr = np.asarray(audio16, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    b = np.array([1.0, -1.0], dtype=np.float32)
    a = np.array([1.0, -0.967], dtype=np.float32)
    hp = lfilter(b, a, arr).astype(np.float32)
    peak = float(np.max(np.abs(hp)))
    if peak >= 0.008:
        gain = min(10.0, 0.75 / peak)
        hp = np.clip(hp * gain, -0.98, 0.98).astype(np.float32)
    return np.ascontiguousarray(hp, dtype=np.float32)


class SileroStreamingVad:
    """Stateful streaming wrapper over `faster-whisper`'s bundled `silero_vad_v6.onnx`."""

    def __init__(self, session: Any | None = None) -> None:
        self._session = session
        self._load_attempted = session is not None
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(VAD_CONTEXT_SAMPLES, dtype=np.float32)

    def _ensure_session(self) -> Any | None:
        if self._session is not None or self._load_attempted:
            return self._session
        self._load_attempted = True
        try:
            from faster_whisper import vad
            self._session = vad.get_vad_model().session
        except Exception:
            self._session = None
        return self._session

    def reset(self) -> None:
        self._h.fill(0.0)
        self._c.fill(0.0)
        self._ctx.fill(0.0)

    def score_frame(self, frame_512: np.ndarray, rms_gate: float = 0.004) -> float:
        """Score a 512-sample 16 kHz mono frame, returning speech probability in [0, 1]."""
        sess = self._ensure_session()
        if sess is None:
            rms = float(np.sqrt(np.mean(frame_512 * frame_512))) if frame_512.size else 0.0
            return 0.9 if rms >= max(rms_gate * 1.6, 0.01) else 0.0
        inp = np.concatenate([self._ctx, frame_512])[None, :].astype(np.float32)
        out, h_next, c_next = sess.run(None, {"input": inp, "h": self._h, "c": self._c})
        self._h = h_next
        self._c = c_next
        self._ctx = frame_512[-VAD_CONTEXT_SAMPLES:].copy()
        return float(np.asarray(out).reshape(-1)[0])


class UtteranceDetector:
    """Chunks 16 kHz mono audio into bounded utterances using pre-roll + VAD + silence hang."""

    def __init__(
        self,
        vad: SileroStreamingVad | None = None,
        vad_threshold: float = 0.48,
        min_speech_ms: int = 280,
        end_silence_ms: int = 580,
        max_utterance_sec: float = 4.8,
        rms_gate: float = 0.0025,
        idle_reset_frames: int = VAD_IDLE_RESET_FRAMES,
    ) -> None:
        self.vad = vad or SileroStreamingVad()
        self.vad_threshold = vad_threshold
        self.min_speech_frames = max(2, int(round(min_speech_ms / 32.0)))
        self.end_silence_frames = max(3, int(round(end_silence_ms / 32.0)))
        self.max_frames = max(20, int(round((max_utterance_sec * 1000.0) / 32.0)))
        self.rms_gate = rms_gate
        self.idle_reset_frames = max(0, int(idle_reset_frames))

        self._buf16 = np.zeros(0, dtype=np.float32)
        self._vad_buf16 = np.zeros(0, dtype=np.float32)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=PRE_ROLL_FRAMES)
        self._in_speech = False
        self._onset_streak = 0
        self._silence_streak = 0
        self._speech_frames = 0
        self._idle_frames = 0
        self._active_max_frames = 0
        self._active_end_silence_frames = 0
        self._collected: list[np.ndarray] = []
        self.latest_rms = 0.0
        self.latest_vad = 0.0

    def is_user_speaking(self) -> bool:
        """Return True if speech onset or an active utterance is currently in progress."""
        return bool(self._in_speech or self._onset_streak > 0)

    def configure(self, cfg: RoomVoiceConfig) -> None:
        self.vad_threshold = cfg.vad_threshold
        self.min_speech_frames = max(2, int(round(cfg.min_speech_ms / 32.0)))
        self.end_silence_frames = max(3, int(round(cfg.end_silence_ms / 32.0)))
        self.max_frames = max(20, int(round((cfg.max_utterance_sec * 1000.0) / 32.0)))
        self.rms_gate = cfg.rms_gate

    def reset(self) -> None:
        self._buf16 = np.zeros(0, dtype=np.float32)
        self._vad_buf16 = np.zeros(0, dtype=np.float32)
        self._pre_roll.clear()
        self._in_speech = False
        self._onset_streak = 0
        self._silence_streak = 0
        self._speech_frames = 0
        self._idle_frames = 0
        self._active_max_frames = 0
        self._active_end_silence_frames = 0
        self._collected.clear()
        self.vad.reset()

    def begin_armed_utterance(
        self,
        preroll_16k: np.ndarray,
        *,
        preroll_sec: float = 1.2,
        max_post_sec: float = 2.6,
        end_silence_ms: int = 390,
    ) -> None:
        """Start an active utterance seeded with unbroken pre-wake audio so short 1-word commands never clip."""
        self.reset()
        arr = np.asarray(preroll_16k, dtype=np.float32).reshape(-1)
        max_pre_samples = int(round(max(0.2, float(preroll_sec)) * SAMPLE_RATE_16K))
        if arr.size > max_pre_samples:
            arr = arr[-max_pre_samples:]
        n_frames = arr.size // VAD_FRAME_SAMPLES
        for i in range(n_frames):
            f512 = arr[i * VAD_FRAME_SAMPLES : (i + 1) * VAD_FRAME_SAMPLES]
            self._collected.append(f512.copy())
        self._in_speech = True
        self._speech_frames = max(self.min_speech_frames, 8)
        self._silence_streak = 0
        self._onset_streak = 0
        self._idle_frames = 0
        post_frames = max(25, int(round((float(max_post_sec) * 1000.0) / 32.0)))
        self._active_max_frames = len(self._collected) + post_frames
        self._active_end_silence_frames = min(self.end_silence_frames, max(4, int(round(float(end_silence_ms) / 32.0))))

    def push_samples(
        self,
        samples_16k: np.ndarray,
        muted: bool = False,
        vad_samples_16k: np.ndarray | None = None,
    ) -> list[np.ndarray]:
        """Ingest 16 kHz mono float32 samples; returns zero or more completed utterance arrays.

        When ``vad_samples_16k`` is supplied (e.g. RES-suppressed audio during loud gym music),
        Silero VAD gates speech onset/offset on ``vad_samples_16k`` while the returned utterance
        preserves the un-warbled ``samples_16k`` (Stage-1 linear PBFDAF) for Whisper ASR.
        """
        if muted:
            self.reset()
            self.latest_rms = 0.0
            self.latest_vad = 0.0
            return []

        if samples_16k.size == 0:
            return []
        raw_arr = samples_16k.astype(np.float32, copy=False)
        self._buf16 = np.concatenate([self._buf16, raw_arr])
        if vad_samples_16k is not None and vad_samples_16k.size == raw_arr.size:
            self._vad_buf16 = np.concatenate([self._vad_buf16, vad_samples_16k.astype(np.float32, copy=False)])
        else:
            self._vad_buf16 = np.concatenate([self._vad_buf16, raw_arr])
        if self._vad_buf16.size != self._buf16.size:
            self._vad_buf16 = self._buf16.copy()
        completed: list[np.ndarray] = []

        while self._buf16.size >= VAD_FRAME_SAMPLES:
            frame = self._buf16[:VAD_FRAME_SAMPLES]
            self._buf16 = self._buf16[VAD_FRAME_SAMPLES:]
            vad_frame = self._vad_buf16[:VAD_FRAME_SAMPLES]
            self._vad_buf16 = self._vad_buf16[VAD_FRAME_SAMPLES:]

            rms = float(np.sqrt(np.mean(vad_frame * vad_frame)))
            self.latest_rms = float(np.sqrt(np.mean(frame * frame)))
            if rms < self.rms_gate * 0.25 and not self._in_speech:
                prob = 0.0
            else:
                prob = self.vad.score_frame(vad_frame, rms_gate=self.rms_gate)
            self.latest_vad = prob

            is_speech = (prob >= self.vad_threshold) and (rms >= self.rms_gate * 0.5)

            if not self._in_speech:
                self._pre_roll.append(frame.copy())
                if is_speech:
                    self._idle_frames = 0
                    self._onset_streak += 1
                    if self._onset_streak >= 2:
                        self._in_speech = True
                        self._collected = list(self._pre_roll)
                        self._speech_frames = self._onset_streak
                        self._silence_streak = 0
                        self._onset_streak = 0
                else:
                    self._onset_streak = 0
                    if self.idle_reset_frames > 0:
                        self._idle_frames += 1
                        if self._idle_frames >= self.idle_reset_frames:
                            # Periodically reset Silero LSTM hidden state while idle so continuous
                            # background gym music cannot drift/saturate speech probability downward.
                            self.vad.reset()
                            self._idle_frames = 0
            else:
                self._idle_frames = 0
                self._collected.append(frame.copy())
                if is_speech:
                    self._speech_frames += 1
                    self._silence_streak = 0
                else:
                    self._silence_streak += 1

                eff_max_frames = self._active_max_frames if self._active_max_frames > 0 else self.max_frames
                eff_end_sil = self._active_end_silence_frames if self._active_end_silence_frames > 0 else self.end_silence_frames
                if self._silence_streak >= eff_end_sil or len(self._collected) >= eff_max_frames:
                    clean_silence_end = self._silence_streak >= eff_end_sil
                    if self._speech_frames >= self.min_speech_frames:
                        completed.append(np.concatenate(self._collected).astype(np.float32))
                    if clean_silence_end:
                        self._collected.clear()
                        self._pre_roll.clear()
                        self.vad.reset()
                    else:
                        tail = self._collected[-PRE_ROLL_FRAMES:]
                        self._collected.clear()
                        self._pre_roll.clear()
                        for f in tail:
                            self._pre_roll.append(f)
                    self._in_speech = False
                    self._onset_streak = 0
                    self._silence_streak = 0
                    self._speech_frames = 0
                    self._idle_frames = 0
                    self._active_max_frames = 0
                    self._active_end_silence_frames = 0

        return completed


def prepare_speaker_pcm(
    mono_pcm: np.ndarray,
    src_sr: int,
    dst_sr: int,
    dst_ch: int,
    lead_ms: int = 420,
    tail_ms: int = 300,
    target_peak: float = 0.84,
) -> np.ndarray:
    """Resample, normalize, edge-fade, and pad audio with USB-amp wakeup lead-in + trailing tail."""
    arr = resample_from_to(mono_pcm, src_sr, dst_sr).astype(np.float32)
    if arr.size == 0:
        return np.zeros((0, max(1, dst_ch)), dtype=np.float32) if dst_ch > 1 else np.zeros(0, dtype=np.float32)

    peak = float(np.max(np.abs(arr)))
    if peak > 0.05 and target_peak > 0.0:
        gain = min(4.0, float(target_peak) / peak)
        arr = arr * gain

    fade_n = min(arr.size // 4, max(0, int(round(dst_sr * 0.008))))
    if fade_n > 1:
        arr = arr.copy()
        ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        arr[:fade_n] *= ramp
        arr[-fade_n:] *= ramp[::-1]

    arr = np.clip(arr, -0.95, 0.95).astype(np.float32)
    lead_n = max(0, int(round(dst_sr * (lead_ms / 1000.0))))
    tail_n = max(0, int(round(dst_sr * (tail_ms / 1000.0))))
    parts: list[np.ndarray] = []
    if lead_n > 0:
        rng = np.random.default_rng(0)
        parts.append((rng.standard_normal(lead_n) * 1.2e-4).astype(np.float32))
    parts.append(arr)
    if tail_n > 0:
        parts.append(np.zeros(tail_n, dtype=np.float32))
    padded = np.concatenate(parts).astype(np.float32)
    if dst_ch > 1:
        return np.repeat(padded[:, None], dst_ch, axis=1)
    return padded


_SHARED_KOKORO_LOCK = threading.Lock()
_SHARED_KOKORO_CACHE: dict[str, tuple[Any, str]] = {}
_SHARED_KOKORO_FAIL_UNTIL: dict[str, tuple[float, str]] = {}
_EDGE_FALLBACK_VOICE = "en-US-AndrewMultilingualNeural"


def get_shared_kokoro(models_dir: Path | None) -> tuple[Any, str]:
    """Load or return the process-wide shared Kokoro ONNX session (GPU/CPU) with 60s negative cache."""
    if models_dir is None:
        raise FileNotFoundError("Kokoro models_dir not configured")
    kokoro_dir = (Path(models_dir) / "kokoro") if (Path(models_dir) / "kokoro").is_dir() else Path(models_dir)
    model_p = (kokoro_dir / "kokoro-v1.0.onnx").resolve()
    voices_p = (kokoro_dir / "voices-v1.0.bin").resolve()
    cache_key = f"{model_p}|{voices_p}"
    now = time.monotonic()
    with _SHARED_KOKORO_LOCK:
        cached = _SHARED_KOKORO_CACHE.get(cache_key)
        if cached is not None:
            return cached
        fail_entry = _SHARED_KOKORO_FAIL_UNTIL.get(cache_key)
        if fail_entry is not None and now < fail_entry[0]:
            raise RuntimeError(f"Kokoro temporarily disabled after failure: {fail_entry[1]}")
        try:
            if not model_p.is_file() or not voices_p.is_file():
                raise FileNotFoundError(f"Kokoro model files not found under {kokoro_dir}")
            _register_cuda_dll_dirs()
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            so = ort.SessionOptions()
            so.log_severity_level = 3
            sess = ort.InferenceSession(
                str(model_p),
                sess_options=so,
                providers=[
                    ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}),
                    "CPUExecutionProvider",
                ],
            )
            active = list(sess.get_providers())
            dev = "cuda" if "CUDAExecutionProvider" in active else "cpu"
            kokoro = Kokoro.from_session(sess, str(voices_p))
            kokoro.create("50 for 10, check", voice="am_michael", speed=1.10, lang="en-us")
            _SHARED_KOKORO_CACHE[cache_key] = (kokoro, dev)
            _SHARED_KOKORO_FAIL_UNTIL.pop(cache_key, None)
            return kokoro, dev
        except Exception as exc:
            _SHARED_KOKORO_FAIL_UNTIL[cache_key] = (time.monotonic() + _KOKORO_NEGATIVE_TTL_SEC, str(exc))
            raise


class _NeuralTtsSynthesizer:
    """Kokoro GPU + ElevenLabs Flash v2.5 + Microsoft Neural `edge-tts` synthesizer with persistent WAV cache."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        max_cache: int = 96,
        models_dir: Path | None = None,
        secrets_path: Path | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = Path(models_dir) if models_dir is not None else None
        self.secrets_path = Path(secrets_path) if secrets_path is not None else None
        self._lock = threading.Lock()
        self._kokoro_lock = threading.Lock()
        self._kokoro: Any = None
        self._kokoro_device: str = ""
        self._kokoro_error: str | None = None
        self._kokoro_fail_until: float = 0.0
        self._cache: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
        self._max_cache = max_cache

    @property
    def kokoro_device(self) -> str:
        return self._kokoro_device

    @staticmethod
    def is_kokoro_voice(voice: str) -> bool:
        return str(voice or "").strip().lower().startswith("kokoro:")

    @staticmethod
    def is_elevenlabs_voice(voice: str) -> bool:
        return bool(_ELEVENLABS_VOICE_RE.match(str(voice or "").strip()))

    @staticmethod
    def kokoro_speaker_name(voice: str) -> str:
        raw = str(voice or "").strip()
        if ":" in raw:
            raw = raw.split(":", 1)[1].strip()
        return raw or "am_michael"

    @staticmethod
    def elevenlabs_voice_id(voice: str) -> str:
        raw = str(voice or "").strip()
        if ":" in raw:
            raw = raw.split(":", 1)[1].strip()
        return raw

    def _load_elevenlabs_key(self) -> str:
        env_key = str(os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        candidates: list[Path] = []
        if self.secrets_path is not None:
            candidates.append(self.secrets_path)
        candidates.append(Path(r"S:\ArmoryCam\tts_secrets.json"))
        for p in candidates:
            if p.is_file():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    k = str((raw or {}).get("elevenlabsKey") or "").strip()
                    if k:
                        return k
                except Exception:
                    pass
        return env_key

    def close(self) -> None:
        with self._kokoro_lock:
            self._kokoro = None
            self._kokoro_device = ""

    @staticmethod
    def _cache_key(text: str, voice: str, speed: float = 1.0) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip().lower()
        sp = round(float(speed or 1.0), 2)
        if abs(sp - 1.0) >= 0.01:
            return hashlib.sha1(f"{voice}|@{sp:.2f}|{clean}".encode("utf-8")).hexdigest()
        return hashlib.sha1(f"{voice}|{clean}".encode("utf-8")).hexdigest()

    def get_cached(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int] | None:
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return None
        key = self._cache_key(clean, voice, speed=speed)
        with self._lock:
            mem = self._cache.get(key)
            if mem is not None:
                self._cache.move_to_end(key)
                return mem
        if self.cache_dir is not None:
            wav_p = self.cache_dir / f"{key}.wav"
            if wav_p.is_file():
                try:
                    with wave.open(str(wav_p), "rb") as wf:
                        sr = wf.getframerate()
                        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
                    with self._lock:
                        self._cache[key] = (pcm, sr)
                        while len(self._cache) > self._max_cache:
                            self._cache.popitem(last=False)
                    return pcm, sr
                except Exception:
                    pass
        return None

    def _put_cached(self, text: str, voice: str, pcm: np.ndarray, sr: int, speed: float = 1.0) -> None:
        key = self._cache_key(text, voice, speed=speed)
        with self._lock:
            self._cache[key] = (pcm, sr)
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
        if self.cache_dir is not None:
            try:
                wav_p = self.cache_dir / f"{key}.wav"
                tmp_p = self.cache_dir / f"{key}.wav.tmp"
                int16_pcm = np.clip(np.round(pcm * 32767.0), -32768, 32767).astype(np.int16)
                with wave.open(str(tmp_p), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(int(sr))
                    wf.writeframes(int16_pcm.tobytes())
                tmp_p.replace(wav_p)
            except Exception:
                pass

    def _ensure_kokoro(self) -> Any:
        with self._kokoro_lock:
            if self._kokoro is not None:
                return self._kokoro
            now = time.monotonic()
            if now < self._kokoro_fail_until:
                raise RuntimeError(self._kokoro_error or "Kokoro temporarily unavailable")
            try:
                kokoro, dev = get_shared_kokoro(self.models_dir)
                self._kokoro = kokoro
                self._kokoro_device = dev
                self._kokoro_error = None
                self._kokoro_fail_until = 0.0
                return kokoro
            except Exception as exc:
                self._kokoro_error = str(exc)
                self._kokoro_fail_until = time.monotonic() + _KOKORO_NEGATIVE_TTL_SEC
                raise

    def _synth_kokoro_pcm(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        kokoro = self._ensure_kokoro()
        speaker = self.kokoro_speaker_name(voice)
        eff_speed = max(0.7, min(1.5, float(speed or 1.0)))
        voice_arg: Any = speaker
        if speaker in KOKORO_BLENDS:
            style_vec = None
            for base_v, w in KOKORO_BLENDS[speaker].items():
                vec = kokoro.get_voice_style(base_v) * float(w)
                style_vec = vec if style_vec is None else (style_vec + vec)
            if style_vec is not None:
                voice_arg = style_vec
        pcm, sr = kokoro.create(text, voice=voice_arg, speed=eff_speed, lang="en-us")
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise RuntimeError("Empty kokoro-onnx audio")
        return arr, int(sr)

    def _synth_elevenlabs_pcm(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        import av

        el_key = self._load_elevenlabs_key()
        if not el_key:
            raise RuntimeError("ElevenLabs API key not configured")
        el_vid = self.elevenlabs_voice_id(voice)
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(el_vid, safe='')}"
            "?output_format=mp3_44100_128"
        )
        eff_speed = max(0.75, min(1.20, float(speed or 1.0)))
        payload = json.dumps({
            "text": text,
            "model_id": _ELEVENLABS_LIVE_MODEL,
            "voice_settings": {
                "stability": 0.55,
                "similarity_boost": 0.80,
                "speed": round(eff_speed, 2),
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "xi-api-key": el_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_ELEVENLABS_TIMEOUT_SEC) as resp:
            mp3_bytes = resp.read()
        if not mp3_bytes:
            raise RuntimeError("Empty ElevenLabs audio")
        resampler = av.AudioResampler(format="flt", layout="mono", rate=24_000)
        parts: list[np.ndarray] = []
        with av.open(io.BytesIO(mp3_bytes), format="mp3") as container:
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    parts.append(rf.to_ndarray().reshape(-1))
            for rf in resampler.resample(None):
                parts.append(rf.to_ndarray().reshape(-1))
        if not parts:
            raise RuntimeError("Decoded 0 ElevenLabs audio frames")
        return np.concatenate(parts).astype(np.float32), 24_000

    @staticmethod
    def _synth_edge_pcm(text: str, voice: str, attempts: int = 2, speed: float = 1.0) -> tuple[np.ndarray, int]:
        import av
        import edge_tts

        sp = float(speed or 1.0)
        rate_str = f"{int(round((sp - 1.0) * 100.0)):+d}%" if abs(sp - 1.0) >= 0.01 else "+0%"

        async def _fetch() -> bytes:
            comm = edge_tts.Communicate(text, voice=voice, rate=rate_str)
            chunks: list[bytes] = []
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        mp3_bytes = b""
        last_exc: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                mp3_bytes = asyncio.run(asyncio.wait_for(_fetch(), timeout=_EDGE_TTS_TIMEOUT_SEC))
                if not mp3_bytes:
                    raise RuntimeError("Empty edge-tts audio")
                break
            except Exception as exc:
                last_exc = exc
                mp3_bytes = b""
                if attempt < max(1, attempts):
                    time.sleep(0.15 * attempt)
        if not mp3_bytes:
            raise RuntimeError(f"Empty edge-tts audio: {last_exc}")

        resampler = av.AudioResampler(format="flt", layout="mono", rate=24_000)
        parts: list[np.ndarray] = []
        with av.open(io.BytesIO(mp3_bytes), format="mp3") as container:
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    parts.append(rf.to_ndarray().reshape(-1))
            for rf in resampler.resample(None):
                parts.append(rf.to_ndarray().reshape(-1))
        if not parts:
            raise RuntimeError("Decoded 0 audio frames")
        return np.concatenate(parts).astype(np.float32), 24_000

    def synthesize(
        self,
        text: str,
        voice: str = DEFAULT_TTS_VOICE,
        speed: float = 1.0,
    ) -> tuple[np.ndarray, int] | None:
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return None
        eff_voice = _LEGACY_TTS_VOICE_MAP.get(voice, voice)
        if (
            eff_voice not in TTS_VOICES
            and not self.is_kokoro_voice(eff_voice)
            and not self.is_elevenlabs_voice(eff_voice)
        ):
            eff_voice = DEFAULT_TTS_VOICE
        cached = self.get_cached(clean, eff_voice, speed=speed)
        if cached is not None:
            return cached
        if self.is_elevenlabs_voice(eff_voice):
            try:
                pcm, sr = self._synth_elevenlabs_pcm(clean, eff_voice, speed=speed)
                self._put_cached(clean, eff_voice, pcm, sr, speed=speed)
                return pcm, sr
            except Exception:
                try:
                    pcm, sr = self._synth_kokoro_pcm(clean, "kokoro:am_onyx", speed=speed)
                    self._put_cached(clean, "kokoro:am_onyx", pcm, sr, speed=speed)
                    return pcm, sr
                except Exception:
                    return None
        if self.is_kokoro_voice(eff_voice):
            try:
                pcm, sr = self._synth_kokoro_pcm(clean, eff_voice, speed=speed)
                self._put_cached(clean, eff_voice, pcm, sr, speed=speed)
                return pcm, sr
            except Exception:
                try:
                    pcm, sr = self._synth_edge_pcm(clean, _EDGE_FALLBACK_VOICE, speed=speed)
                    self._put_cached(clean, _EDGE_FALLBACK_VOICE, pcm, sr, speed=speed)
                    return pcm, sr
                except Exception:
                    return None
        try:
            pcm, sr = self._synth_edge_pcm(clean, eff_voice, speed=speed)
            self._put_cached(clean, eff_voice, pcm, sr, speed=speed)
            return pcm, sr
        except Exception:
            try:
                pcm, sr = self._synth_kokoro_pcm(clean, DEFAULT_TTS_VOICE, speed=speed)
                self._put_cached(clean, DEFAULT_TTS_VOICE, pcm, sr, speed=speed)
                return pcm, sr
            except Exception:
                return None


def _classify_ab_trial(trial: dict[str, Any]) -> str:
    r = trial.get("room")
    p = trial.get("phone")
    rm = bool(r and r.get("matched"))
    pm = bool(p and p.get("matched"))
    if rm and pm:
        return "both_match" if r.get("intentKey") == p.get("intentKey") else "disagree"
    if rm:
        return "room_only"
    if pm:
        return "phone_only"
    return "both_missed"


class VoiceAbTracker:
    """Pairs simultaneous Room Mic + Pixel Phone Mic samples and persists A/B metrics & shootout runs."""

    PAIR_WINDOW_MS = 4_500
    MAX_TRIALS = 300
    MAX_RUNS = 40

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir
        self.trials_path = (root_dir / "voice_ab.jsonl") if root_dir is not None else None
        self.runs_path = (root_dir / "voice_ab_runs.json") if root_dir is not None else None
        self.lock = threading.RLock()
        self.trials: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self._seq = 0
        self._load()

    def _load(self) -> None:
        if self.trials_path and self.trials_path.is_file():
            loaded: list[dict[str, Any]] = []
            try:
                for line in self.trials_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        loaded.append(json.loads(line))
                    except Exception:
                        pass
            except Exception:
                pass
            self.trials = loaded[-self.MAX_TRIALS :]
            self._seq = len(self.trials)
        if self.runs_path and self.runs_path.is_file():
            try:
                raw_runs = json.loads(self.runs_path.read_text(encoding="utf-8"))
                if isinstance(raw_runs, list):
                    self.runs = raw_runs[: self.MAX_RUNS]
            except Exception:
                pass

    def _save_trials_locked(self) -> None:
        if not self.trials_path:
            return
        self.trials_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.trials_path.with_suffix(".jsonl.tmp")
        body = "\n".join(json.dumps(t, separators=(",", ":")) for t in self.trials[-self.MAX_TRIALS :])
        tmp.write_text((body + "\n") if body else "", encoding="utf-8")
        tmp.replace(self.trials_path)

    def _save_runs_locked(self) -> None:
        if not self.runs_path:
            return
        self.runs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.runs[: self.MAX_RUNS], indent=2), encoding="utf-8")
        tmp.replace(self.runs_path)

    def record_sample(
        self,
        raw_sample: dict[str, Any],
        raw_room_recent: Sequence[dict[str, Any]] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        src = str(raw_sample.get("source") or "mic").strip().lower()
        slot = "room" if src == "room" else "phone"
        ts_ms = int(raw_sample.get("tsMs") or int(time.time() * 1000))
        station = str(raw_sample.get("station") or "").strip()
        side_obj = {
            "transcript": str(raw_sample.get("transcript") or "").strip(),
            "matched": bool(raw_sample.get("matched", False)),
            "intentKey": str(raw_sample.get("intentKey") or "none"),
            "intentLabel": str(raw_sample.get("intentLabel") or ""),
            "hasWake": bool(raw_sample.get("hasWake", False)),
            "latencyMs": raw_sample.get("latencyMs"),
            "executed": bool(raw_sample.get("executed", False)),
            "tsMs": ts_ms,
        }

        with self.lock:
            matched_trial: dict[str, Any] | None = None
            best_diff = self.PAIR_WINDOW_MS + 1
            for t in reversed(self.trials[-24:]):
                if t.get(slot) is not None:
                    continue
                other_slot = "phone" if slot == "room" else "room"
                other = t.get(other_slot)
                ref_ts = int((other or {}).get("tsMs") or t["tsMs"])
                diff = abs(ts_ms - ref_ts)
                if diff <= self.PAIR_WINDOW_MS and diff < best_diff:
                    best_diff = diff
                    matched_trial = t

            if matched_trial is not None:
                matched_trial[slot] = side_obj
                if not matched_trial.get("station") and station:
                    matched_trial["station"] = station
                matched_trial["outcome"] = _classify_ab_trial(matched_trial)
                trial = matched_trial
            else:
                self._seq += 1
                trial = {
                    "id": f"ab_{self._seq}_{ts_ms}",
                    "tsMs": ts_ms,
                    "station": station,
                    "room": side_obj if slot == "room" else None,
                    "phone": side_obj if slot == "phone" else None,
                    "outcome": "both_missed",
                    "resolvedWinner": None,
                }
                if slot == "phone":
                    used_room_ts = {
                        int((t.get("room") or {}).get("tsMs") or -1)
                        for t in self.trials[-24:]
                        if t.get("room")
                    }
                    for raw_r in raw_room_recent:
                        r_ts = int(raw_r.get("tsMs") or 0)
                        if not raw_r.get("emitted") and abs(ts_ms - r_ts) <= self.PAIR_WINDOW_MS and r_ts not in used_room_ts:
                            trial["room"] = {
                                "transcript": str(raw_r.get("transcript") or "").strip(),
                                "matched": False,
                                "intentKey": "none",
                                "intentLabel": "Missed wake",
                                "hasWake": bool(raw_r.get("wakeMatched", False)),
                                "latencyMs": raw_r.get("latencyMs"),
                                "executed": False,
                                "tsMs": r_ts,
                            }
                            break
                trial["outcome"] = _classify_ab_trial(trial)
                self.trials.append(trial)
                if len(self.trials) > self.MAX_TRIALS:
                    self.trials = self.trials[-self.MAX_TRIALS :]

            self._save_trials_locked()
            return dict(trial), self.summary_dict()

    def resolve_trial(self, trial_id: str, winner: str | None) -> dict[str, Any] | None:
        clean_w = winner if winner in ("room", "phone") else None
        with self.lock:
            for t in reversed(self.trials):
                if t.get("id") == trial_id:
                    t["resolvedWinner"] = clean_w
                    self._save_trials_locked()
                    return self.summary_dict()
            return None

    def save_run(self, raw_run: dict[str, Any]) -> dict[str, Any]:
        ts_ms = int(raw_run.get("tsMs") or int(time.time() * 1000))
        label = str(raw_run.get("label") or "Shootout Run").strip() or "Shootout Run"
        items = raw_run.get("items") if isinstance(raw_run.get("items"), list) else []
        count = max(1, int(raw_run.get("phrasesCount") or len(items) or 10))
        room_pct = round(float(raw_run.get("roomIntentPct") or 0.0), 1)
        phone_pct = round(float(raw_run.get("phoneIntentPct") or 0.0), 1)
        room_word = round(float(raw_run.get("roomWordPct") or 0.0), 1)
        phone_word = round(float(raw_run.get("phoneWordPct") or 0.0), 1)
        run = {
            "id": str(raw_run.get("id") or f"run_{ts_ms}"),
            "tsMs": ts_ms,
            "label": label,
            "phrasesCount": count,
            "roomIntentPct": room_pct,
            "phoneIntentPct": phone_pct,
            "deltaPts": round(room_pct - phone_pct, 1),
            "roomWordPct": room_word,
            "phoneWordPct": phone_word,
            "items": items[:25],
        }
        with self.lock:
            self.runs.insert(0, run)
            self.runs = self.runs[: self.MAX_RUNS]
            self._save_runs_locked()
            return run

    def reset(self, clear_runs: bool = False) -> dict[str, Any]:
        with self.lock:
            self.trials.clear()
            self._save_trials_locked()
            if clear_runs:
                self.runs.clear()
                self._save_runs_locked()
            return self.summary_dict()

    def summary_dict(self) -> dict[str, Any]:
        with self.lock:
            total = len(self.trials)
            both_match = 0
            room_only = 0
            phone_only = 0
            disagree = 0
            both_missed = 0
            disagree_room = 0
            disagree_phone = 0
            stations: dict[str, list[dict[str, Any]]] = {}

            for t in self.trials:
                out = t.get("outcome") or _classify_ab_trial(t)
                if out == "both_match":
                    both_match += 1
                elif out == "room_only":
                    room_only += 1
                elif out == "phone_only":
                    phone_only += 1
                elif out == "disagree":
                    disagree += 1
                    w = t.get("resolvedWinner")
                    if w == "room":
                        disagree_room += 1
                    elif w == "phone":
                        disagree_phone += 1
                else:
                    both_missed += 1

                st_name = str(t.get("station") or "General").strip() or "General"
                stations.setdefault(st_name, []).append(t)

            room_wins = both_match + room_only + disagree_room
            phone_wins = both_match + phone_only + disagree_phone
            room_pct = round((room_wins / total) * 100.0, 1) if total > 0 else 0.0
            phone_pct = round((phone_wins / total) * 100.0, 1) if total > 0 else 0.0
            delta_pts = round(room_pct - phone_pct, 1)
            rel_gain = round(((room_pct - phone_pct) / phone_pct) * 100.0, 1) if phone_pct > 0 else None
            both_parsed = both_match + disagree
            agree_pct = round((both_match / both_parsed) * 100.0, 1) if both_parsed > 0 else None

            by_station: list[dict[str, Any]] = []
            for st_name, st_trials in stations.items():
                n_st = len(st_trials)
                rw = sum(
                    1
                    for x in st_trials
                    if x.get("outcome") in ("both_match", "room_only")
                    or (x.get("outcome") == "disagree" and x.get("resolvedWinner") == "room")
                )
                pw = sum(
                    1
                    for x in st_trials
                    if x.get("outcome") in ("both_match", "phone_only")
                    or (x.get("outcome") == "disagree" and x.get("resolvedWinner") == "phone")
                )
                r_p = round((rw / n_st) * 100.0, 1) if n_st else 0.0
                p_p = round((pw / n_st) * 100.0, 1) if n_st else 0.0
                by_station.append({
                    "station": st_name,
                    "trials": n_st,
                    "roomCatchPct": r_p,
                    "phoneCatchPct": p_p,
                    "deltaPts": round(r_p - p_p, 1),
                })

            return {
                "totalTrials": total,
                "bothMatch": both_match,
                "roomOnly": room_only,
                "phoneOnly": phone_only,
                "disagree": disagree,
                "bothMissed": both_missed,
                "roomWins": room_wins,
                "phoneWins": phone_wins,
                "roomCatchPct": room_pct,
                "phoneCatchPct": phone_pct,
                "deltaPts": delta_pts,
                "relativeGainPct": rel_gain,
                "agreementPct": agree_pct,
                "byStation": by_station,
            }

    def snapshot_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "summary": self.summary_dict(),
                "recentTrials": list(reversed(self.trials[-25:])),
                "runs": list(self.runs),
            }


OWW_FRAME_SAMPLES = 1280  # 80 ms @ 16 kHz (native openWakeWord frame size)


class OpenWakeWordSpotter:
    """Streaming ONNX wake-word spotter (`hey_jarvis_v0.1.onnx`) running at <2% CPU with 0 GPU."""

    def __init__(
        self,
        models_dir: Path | None = None,
        *,
        model_name: str = "hey_jarvis_v0.1",
        threshold: float = 0.38,
        cooldown_sec: float = 1.4,
        oww_model: Any | None = None,
    ) -> None:
        self.models_dir = Path(models_dir) if models_dir is not None else None
        self.model_name = model_name
        self.threshold = float(threshold)
        self.cooldown_sec = float(cooldown_sec)
        self._model = oww_model
        self._model_key = model_name
        self._buf16 = np.zeros(0, dtype=np.float32)
        self._last_trigger_mono = 0.0
        self.latest_score = 0.0
        self.load_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        if self._model is not None:
            return True
        candidates: list[Path] = []
        if self.models_dir is not None:
            candidates.append(self.models_dir / "openwakeword")
            candidates.append(self.models_dir)
        candidates.append(Path(r"S:\ArmoryCam\models\openwakeword"))
        oww_dir = next((d for d in candidates if (d / f"{self.model_name}.onnx").is_file()), None)
        if oww_dir is None:
            self.load_error = f"openWakeWord ONNX model {self.model_name}.onnx not found"
            return False
        melspec_p = oww_dir / "melspectrogram.onnx"
        embed_p = oww_dir / "embedding_model.onnx"
        wake_p = oww_dir / f"{self.model_name}.onnx"
        if not (melspec_p.is_file() and embed_p.is_file() and wake_p.is_file()):
            self.load_error = f"openWakeWord ONNX assets incomplete in {oww_dir}"
            return False
        try:
            from openwakeword.model import Model as OwwModel

            mdl = OwwModel(
                wakeword_models=[str(wake_p)],
                inference_framework="onnx",
                melspec_model_path=str(melspec_p),
                embedding_model_path=str(embed_p),
            )
            keys = list(getattr(mdl, "models", {}).keys())
            self._model_key = keys[0] if keys else self.model_name
            self._model = mdl
            self.load_error = None
            return True
        except Exception as exc:
            self.load_error = str(exc)
            return False

    def reset(self) -> None:
        self._buf16 = np.zeros(0, dtype=np.float32)
        self.latest_score = 0.0
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass

    def push_samples(self, samples_16k: np.ndarray, now_mono: float | None = None) -> tuple[bool, float]:
        """Ingest 16 kHz float32 audio; returns `(triggered, peak_score)`."""
        if self._model is None or samples_16k.size == 0:
            return False, 0.0
        now_m = time.monotonic() if now_mono is None else float(now_mono)
        self._buf16 = np.concatenate([self._buf16, samples_16k.astype(np.float32, copy=False)])
        triggered = False
        peak = 0.0
        while self._buf16.size >= OWW_FRAME_SAMPLES:
            chunk = self._buf16[:OWW_FRAME_SAMPLES]
            self._buf16 = self._buf16[OWW_FRAME_SAMPLES:]
            pcm_i16 = np.round(np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2")
            try:
                preds = self._model.predict(pcm_i16)
            except Exception:
                continue
            score = float(preds.get(self._model_key, 0.0)) if isinstance(preds, dict) else 0.0
            self.latest_score = score
            if score > peak:
                peak = score
            if not triggered and score >= self.threshold and (now_m - self._last_trigger_mono) >= self.cooldown_sec:
                triggered = True
                self._last_trigger_mono = now_m
                try:
                    self._model.reset()
                except Exception:
                    pass
                self._buf16 = np.zeros(0, dtype=np.float32)
                break
        return triggered, peak


def looks_like_workout_app_command(text: str, wake_word: str = "hey armory") -> bool:
    """Return True only for workout-logging / phone-session commands that actually require the Armory Fit app."""
    tail = strip_wake_word(text, wake_word).strip().lower()
    if not tail:
        return False
    # Never alert "App not connected" on room/gym open/close/stop/chatter commands
    if re.search(r"\b(?:open|close|shut\s*down|power\s*(?:on|off|down)|guests?|welcome|lights?)\b", tail):
        return False
    # Numbers/weights/reps (e.g. "225 for 8", "for 10", "50s for 12", "rpe 8.5", "+5")
    if re.search(r"\b\d+(?:\.\d+)?\b", tail):
        return True
    # Explicit workout/set/camera session keywords
    return bool(
        re.search(
            r"\b(?:check|done|log|logged|set|reps?|weight|pounds?|lbs?|rpe|skip|rest|next|previous|back|"
            r"record|recording|clip|replay|slow|workout|exercise|warmup|warm\s*up|working\s*set|drop\s*set|amrap|"
            r"timer|pause\s+workout|resume\s+workout|finish|undo|delete\s+(?:last|set))\b",
            tail,
        )
    )


class RoomVoiceService:
    """Manages PowerConf S330 audio capture, Whisper ASR, SSE broadcast, and speaker feedback."""

    def __init__(
        self,
        config: RoomVoiceConfig | None = None,
        on_fallback_transcript: Callable[[str], None] | None = None,
        asr_transcribe_fn: Callable[[np.ndarray, str, str], str] | None = None,
        speak_fn: Callable[[str], bool] | None = None,
        allow_hardware_audio: bool = True,
        root_dir: Path | None = None,
        house_audio_router: Callable[..., dict[str, Any] | None] | None = None,
        local_command_handler: Callable[[str], dict[str, Any] | None] | None = None,
        on_pcm_chunk: Callable[[np.ndarray, int, float], None] | None = None,
        wake_duck_callbacks: dict[str, Callable[..., Any]] | None = None,
        wake_spotter: Any | None = None,
        show_active_fn: Callable[[], bool] | None = None,
        aec_processor: Any | None = None,
    ) -> None:
        self.cfg = config or RoomVoiceConfig()
        self.root_dir = root_dir
        self._root_dir = root_dir
        self.on_fallback_transcript = on_fallback_transcript
        self._asr_transcribe_fn = asr_transcribe_fn
        self._speak_fn = speak_fn
        self._chime_fn: Callable[[str], bool] | None = None
        self._commit_chime_cache: tuple[np.ndarray, int] | None = None
        self._last_commit_chime_mono: float = 0.0
        self._allow_hardware_audio = allow_hardware_audio
        self._house_audio_router = house_audio_router
        self._local_command_handler = local_command_handler
        self._on_pcm_chunk = on_pcm_chunk
        self._wake_duck_cbs: dict[str, Callable[..., Any]] = dict(wake_duck_callbacks or {})
        self._show_active_fn = show_active_fn

        self.log = logging.getLogger("armory-voice")
        self.lock = threading.RLock()
        self._audio_lock = threading.Lock()
        self._cond = threading.Condition(self.lock)

        self._started = False
        self._stop_event = threading.Event()
        self._worker_threads: list[threading.Thread] = []
        self._utt_queue: queue.Queue[Any] = queue.Queue(maxsize=6)
        self._speaker_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=16)

        self._model: Any = None
        self._model_size_loaded: str = ""
        self._model_device: str = ""
        self._model_loading = False
        self._model_error: str | None = None

        models_dir = (root_dir / "models") if root_dir is not None else None
        self._models_dir = models_dir
        self._custom_wake_spotter = wake_spotter is not None
        self._oww_spotter: Any = (
            wake_spotter
            if wake_spotter is not None
            else OpenWakeWordSpotter(
                models_dir=models_dir,
                model_name=resolve_oww_model_name(self.cfg.wake_word),
                threshold=self.cfg.wake_threshold,
            )
        )
        self._oww_loading = False
        self._oww_preroll: deque[np.ndarray] = deque(maxlen=48)
        self._oww_awaiting_cmd = False
        self._rescue_active = False
        self._rescue_chunks: list[np.ndarray] = []
        self._rescue_post_samples = 0
        self._rescue_peak_score = 0.0
        self._rescue_last_mono = 0.0
        self._wake_duck_held = False
        self._release_wake_duck_on_play_done = False
        self._wake_duck_release_timer: threading.Timer | None = None

        self._stream: sd.InputStream | None = None
        self._aec_stream: sd.InputStream | None = None
        self._aec: LineInAecProcessor = (
            aec_processor if aec_processor is not None else LineInAecProcessor(enabled=self.cfg.aec_enabled)
        )
        self._stream_sr: int = 48_000
        self._stream_dev_name: str = ""
        self._stream_host_api: str = ""
        self._wasapi_probe_misses: int = 0
        self._host_api_diag_logged: bool = False
        self._local_cmd_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._detector = UtteranceDetector(
            vad_threshold=self.cfg.vad_threshold,
            min_speech_ms=self.cfg.min_speech_ms,
            end_silence_ms=self.cfg.end_silence_ms,
            max_utterance_sec=self.cfg.max_utterance_sec,
            rms_gate=self.cfg.rms_gate,
        )
        self._aec_vad_detector = UtteranceDetector(
            vad_threshold=0.42,
            min_speech_ms=160,
            end_silence_ms=420,
            max_utterance_sec=4.5,
            rms_gate=0.0025,
        )
        self._last_emitted_norm: str = ""
        self._last_emitted_mono: float = 0.0
        cache_dir = (root_dir / "tts_cache") if root_dir is not None else None
        secrets_path = (root_dir / "tts_secrets.json") if root_dir is not None else None
        self._tts = _NeuralTtsSynthesizer(cache_dir=cache_dir, models_dir=models_dir, secrets_path=secrets_path)
        self._winmm_player = _WinMmSpeakerPlayer() if sys.platform == "win32" else None
        self.ab = VoiceAbTracker(root_dir=root_dir)

        self.listening = False
        self.last_error: str | None = None
        self.sse_clients = 0
        self.context_active = False
        self.wallboard_active = False
        self.context_readback = True
        self.context_wake_word = self.cfg.wake_word
        self._client_wake_word = "armory"
        self.bias_phrases: list[str] = []
        self.test_listen_until_mono = 0.0
        self.last_client_seen_mono = 0.0
        self.armed_until_ms = 0
        self._mute_until_mono = 0.0
        self._last_spoken_text: str = ""
        self._last_spoken_mono: float = 0.0

        self.seq = 0
        self.event_seq = int(time.time() * 1000)
        self._events: deque[dict[str, Any]] = deque(maxlen=40)
        self.recent_transcripts: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_TRANSCRIPTS)

    # ---------- lifecycle ----------

    def start(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._wasapi_probe_misses = 0
            self._host_api_diag_logged = False
            if self._local_cmd_pool is None:
                self._local_cmd_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="armory-voice-cmd",
                )
            if self.cfg.enabled:
                self._ensure_model_loading_locked()
            t_cap = threading.Thread(target=self._capture_supervisor_loop, name="armory-voice-cap", daemon=True)
            t_asr = threading.Thread(target=self._asr_worker_loop, name="armory-voice-asr", daemon=True)
            t_spk = threading.Thread(target=self._speaker_worker_loop, name="armory-voice-spk", daemon=True)
            t_cap.start()
            t_asr.start()
            t_spk.start()
            self._worker_threads = [t_cap, t_asr, t_spk]
            if self._allow_hardware_audio and self.cfg.speaker_readback:
                t_pre = threading.Thread(target=self._prewarm_tts_worker, name="armory-voice-ttswarm", daemon=True)
                t_pre.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_wake_duck_release_timer()
        self._release_wake_duck_sync()
        with self.lock:
            self._started = False
            self._cond.notify_all()
        try:
            self._speaker_queue.put_nowait(("stop", None))
        except queue.Full:
            pass
        self.stop_s330_playback()
        self._close_stream()
        self._tts.close()
        for t in self._worker_threads:
            t.join(timeout=1.5)
        self._worker_threads.clear()
        pool = self._local_cmd_pool
        self._local_cmd_pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def stop_s330_playback(self) -> None:
        """Cancel any active PowerConf S330 speaker playback cleanly."""
        if self._winmm_player is not None:
            self._winmm_player.stop()

    def _using_oww_spotter(self) -> bool:
        return bool(
            self.cfg.wake_engine == "openwakeword"
            and self._oww_spotter is not None
            and getattr(self._oww_spotter, "ready", False)
        )

    def _cancel_wake_duck_release_timer(self) -> None:
        with self.lock:
            t = self._wake_duck_release_timer
            self._wake_duck_release_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _schedule_wake_duck_release(self, delay_sec: float = 2.2) -> None:
        self._cancel_wake_duck_release_timer()
        with self.lock:
            if not self._wake_duck_held:
                return
            t = threading.Timer(max(0.2, float(delay_sec)), self._release_wake_duck_sync)
            t.daemon = True
            self._wake_duck_release_timer = t
            t.start()

    def _release_wake_duck_sync(self) -> None:
        self._cancel_wake_duck_release_timer()
        with self.lock:
            was_held = self._wake_duck_held
            self._wake_duck_held = False
            self._release_wake_duck_on_play_done = False
            end_fn = self._wake_duck_cbs.get("end")
        if was_held and callable(end_fn):
            try:
                end_fn()
            except Exception as exc:
                self.log.debug("Wake duck release error: %s", exc)

    def _release_wake_duck_async(self) -> None:
        with self.lock:
            if not self._wake_duck_held:
                return
        pool = self._local_cmd_pool
        if pool is not None and not self._stop_event.is_set():
            try:
                pool.submit(self._release_wake_duck_sync)
                return
            except Exception:
                pass
        threading.Thread(target=self._release_wake_duck_sync, name="armory-wake-unduck", daemon=True).start()

    def _extend_wake_duck(self, hold_sec: float = 6.0) -> None:
        with self.lock:
            if not self._wake_duck_held:
                return
            ext_fn = self._wake_duck_cbs.get("extend")
        if callable(ext_fn):
            try:
                ext_fn(hold_sec)
            except Exception:
                pass

    def _is_show_active(self) -> bool:
        fn = self._show_active_fn
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    def _on_oww_wake_detected(self, score: float) -> None:
        """Triggered off audio-callback thread the instant openWakeWord spots wake phrase."""
        # Never duck Denon, chime, or arm the phone pill while a live Show Director show is playing
        # (prevents shows saying "Welcome to The Armory" from self-triggering and ducking their own music!).
        if self._is_show_active():
            self.log.debug("Wake word suppressed during active show (score=%.3f)", score)
            return
        # Immediately flash the Pixel & Wallboard pill TEAL (armed) via SSE before Denon HTTP completes!
        now_ms = int(time.time() * 1000)
        with self.lock:
            if self.sse_clients > 0:
                wake = self._effective_wake_word_locked()
                client_wake = (self._client_wake_word or wake).strip().lower() or wake
                arm_text = format_wake_display(client_wake)
                self.event_seq += 1
                ev_id = self.event_seq
                evt = {
                    "type": "transcript",
                    "id": ev_id,
                    "tsMs": now_ms,
                    "transcript": arm_text,
                    "candidates": [arm_text],
                    "wakeWord": client_wake,
                }
                self._events.append(evt)
                self.seq += 1
                self._cond.notify_all()

        ducked = False
        duck_info: dict[str, Any] = {}
        begin_fn = self._wake_duck_cbs.get("begin")
        if callable(begin_fn):
            try:
                res = begin_fn()
                if isinstance(res, dict):
                    duck_info = res
                    ducked = bool(res.get("ducked"))
            except Exception as exc:
                self.log.debug("Wake duck begin error: %s", exc)
        with self.lock:
            self._wake_duck_held = ducked
            self._release_wake_duck_on_play_done = False
            self._bump_status_locked()
        if ducked:
            self.log.info(
                "Wake word detected (%s score=%.3f) -> Denon duck %.1f dB (%.1f -> %.1f dB)",
                self.cfg.wake_word,
                score,
                float(duck_info.get("dropDb") or 0.0),
                float(duck_info.get("fromDb") or 0.0),
                float(duck_info.get("toDb") or 0.0),
            )
        else:
            self.log.info(
                "Wake word detected (%s score=%.3f) -> Denon not ducked (%s); ready chime",
                self.cfg.wake_word,
                score,
                duck_info.get("reason", "off"),
            )
            if self.cfg.speaker_readback and self.context_readback:
                self.play_ready_chime()

    def update_config(self, raw: dict[str, Any]) -> RoomVoiceConfig:
        with self.lock:
            merged = {**self.cfg.to_dict(), **(raw or {})}
            next_cfg = RoomVoiceConfig.from_dict(merged)
            dev_changed = next_cfg.device_match != self.cfg.device_match
            aec_changed = (
                next_cfg.aec_enabled != self.cfg.aec_enabled
                or next_cfg.aec_device_match != self.cfg.aec_device_match
            )
            model_changed = next_cfg.model_size != self.cfg.model_size
            tts_changed = next_cfg.tts_voice != self.cfg.tts_voice
            wake_changed = (
                next_cfg.wake_word != self.cfg.wake_word
                or next_cfg.wake_engine != self.cfg.wake_engine
            )
            self.cfg = next_cfg
            self._aec.enabled = bool(next_cfg.aec_enabled)
            self.context_wake_word = next_cfg.wake_word
            self._detector.configure(next_cfg)
            if wake_changed and not self._custom_wake_spotter:
                self._oww_spotter = OpenWakeWordSpotter(
                    models_dir=self._models_dir,
                    model_name=resolve_oww_model_name(next_cfg.wake_word),
                    threshold=next_cfg.wake_threshold,
                )
                self._oww_loading = False
            elif self._oww_spotter is not None and hasattr(self._oww_spotter, "threshold"):
                self._oww_spotter.threshold = next_cfg.wake_threshold
            if dev_changed:
                self._wasapi_probe_misses = 0
                self._host_api_diag_logged = False
            if model_changed:
                self._model = None
                self._model_size_loaded = ""
                self._model_device = ""
            if self._started and next_cfg.enabled:
                self._ensure_model_loading_locked()
            if (
                tts_changed
                and self._started
                and self._allow_hardware_audio
                and next_cfg.speaker_readback
            ):
                threading.Thread(target=self._prewarm_tts_worker, name="armory-voice-ttswarm", daemon=True).start()
            self._bump_status_locked()
        if dev_changed or aec_changed or not next_cfg.enabled:
            self._close_stream()
        return next_cfg

    def _ensure_model_loading_locked(self) -> None:
        if (
            self.cfg.wake_engine == "openwakeword"
            and self._oww_spotter is not None
            and not getattr(self._oww_spotter, "ready", False)
            and not self._oww_loading
        ):
            self._oww_loading = True
            threading.Thread(target=self._load_oww_worker, name="armory-voice-load-oww", daemon=True).start()
        if self._asr_transcribe_fn is not None:
            return
        want_size = self.cfg.model_size
        if (self._model is not None and self._model_size_loaded == want_size) or self._model_loading:
            return
        self._model_loading = True
        self._model_error = None
        threading.Thread(
            target=self._load_model_worker,
            args=(want_size,),
            name=f"armory-voice-load-{want_size}",
            daemon=True,
        ).start()

    def _load_oww_worker(self) -> None:
        try:
            t0 = time.perf_counter()
            ok = bool(self._oww_spotter.load())
            dt = (time.perf_counter() - t0) * 1000.0
            with self.lock:
                self._oww_loading = False
                self._bump_status_locked()
            if ok:
                self.log.info(
                    "Loaded openWakeWord ONNX (%s, thr=%.2f) in %.0fms",
                    getattr(self._oww_spotter, "model_name", "hey_jarvis"),
                    float(getattr(self._oww_spotter, "threshold", 0.38)),
                    dt,
                )
            else:
                self.log.warning("openWakeWord unavailable (falling back to Whisper wake): %s", getattr(self._oww_spotter, "load_error", "unknown"))
        except Exception as exc:
            with self.lock:
                self._oww_loading = False
            self.log.warning("openWakeWord load error: %s", exc)

    def _load_model_worker(self, model_size: str) -> None:
        try:
            t0 = time.perf_counter()
            model, asr_dev = _load_whisper_model(model_size)
            dt = time.perf_counter() - t0
            with self.lock:
                if self.cfg.model_size == model_size:
                    self._model = model
                    self._model_size_loaded = model_size
                    self._model_device = asr_dev
                    self._model_loading = False
                    self._model_error = None
                    self._bump_status_locked()
            self.log.info("Loaded faster-whisper %s (%s) in %.2fs", model_size, asr_dev, dt)
        except Exception as exc:
            with self.lock:
                self._model_loading = False
                self._model_error = str(exc)
                self._bump_status_locked()
            self.log.warning("Failed loading faster-whisper %s: %s", model_size, exc)

    # ---------- context & control ----------

    def should_capture_now(self, now_mono: float | None = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        with self.lock:
            if not self.cfg.enabled:
                return False
            if self.cfg.listen_mode == "always":
                return True
            if now < self.test_listen_until_mono:
                return True
            if self.sse_clients > 0 or self.context_active or self.wallboard_active:
                return True
            if self.last_client_seen_mono > 0 and (now - self.last_client_seen_mono) < 12.0:
                return True
            return False

    def set_context(
        self,
        *,
        active: bool | None = None,
        wake_word: str | None = None,
        bias_phrases: Sequence[str] | None = None,
        readback: bool | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if active is not None:
                self.context_active = bool(active)
                if active:
                    self.last_client_seen_mono = time.monotonic()
            if wake_word is not None and str(wake_word).strip():
                clean_client_wake = str(wake_word).strip().lower()
                self._client_wake_word = clean_client_wake
                if self.cfg.wake_engine != "openwakeword":
                    self.context_wake_word = clean_client_wake
            if bias_phrases is not None:
                self.bias_phrases = [str(x).strip() for x in bias_phrases if str(x).strip()][:36]
            if readback is not None:
                self.context_readback = bool(readback)
            self._bump_status_locked()
            return self.status_dict()

    def set_wallboard_active(self, active: bool) -> None:
        with self.lock:
            if self.wallboard_active == bool(active):
                return
            self.wallboard_active = bool(active)
            if active:
                self.last_client_seen_mono = time.monotonic()
            self._bump_status_locked()

    def start_test_listen(self, seconds: float = 60.0) -> dict[str, Any]:
        with self.lock:
            sec = max(0.0, min(600.0, float(seconds)))
            self.test_listen_until_mono = time.monotonic() + sec if sec > 0 else 0.0
            if sec > 0 and self._started:
                self._ensure_model_loading_locked()
            self._bump_status_locked()
            return self.status_dict()

    def _bump_status_locked(self) -> None:
        self.seq += 1
        self._cond.notify_all()

    def _effective_wake_word_locked(self) -> str:
        if self.cfg.wake_engine == "openwakeword":
            return (self.cfg.wake_word or "hey jarvis").strip().lower() or "hey jarvis"
        return (self.context_wake_word or self.cfg.wake_word or "armory").strip().lower() or "armory"

    def is_ready(self) -> bool:
        with self.lock:
            return self._asr_transcribe_fn is not None or self._model is not None

    def status_dict(self, include_devices: bool = False) -> dict[str, Any]:
        devs_snap = list_audio_devices() if include_devices else None
        in_dev = resolve_audio_device("in", self.cfg.device_match, devs_snap) if devs_snap is not None else None
        out_dev = resolve_audio_device("out", self.cfg.speaker_match, devs_snap) if devs_snap is not None else None
        now_ms = int(time.time() * 1000)
        now_mono = time.monotonic()
        with self.lock:
            ready = self._asr_transcribe_fn is not None or self._model is not None
            dev_label = self._stream_dev_name or (in_dev["name"] if in_dev else self.cfg.device_match)
            test_rem = max(0, int(round(self.test_listen_until_mono - now_mono)))
            out: dict[str, Any] = {
                "enabled": bool(self.cfg.enabled),
                "ready": bool(ready),
                "loading": bool(self._model_loading),
                "listening": bool(self.listening),
                "armed": now_ms < self.armed_until_ms,
                "wantCapture": self.should_capture_now(now_mono),
                "listenMode": self.cfg.listen_mode,
                "modelSize": self.cfg.model_size,
                "asrDevice": self._model_device,
                "wakeWord": self._effective_wake_word_locked(),
                "wakeEngine": self.cfg.wake_engine,
                "owwReady": bool(self._oww_spotter is not None and getattr(self._oww_spotter, "ready", False)),
                "wakeScore": round(float(getattr(self._oww_spotter, "latest_score", 0.0) or 0.0), 3),
                "wakeDuckHeld": bool(self._wake_duck_held),
                "device": dev_label,
                "hostApi": self._stream_host_api or (in_dev["hostApi"] if in_dev else ""),
                "speaker": out_dev["name"] if out_dev else self.cfg.speaker_match,
                "speakerReadback": bool(self.cfg.speaker_readback),
                "roomCommandReadback": bool(self.cfg.room_command_readback),
                "ttsVoice": self.cfg.tts_voice,
                "ttsDevice": getattr(self._tts, "kokoro_device", ""),
                "setCommitMode": self.cfg.set_commit_mode,
                "sseClients": self.sse_clients,
                "contextActive": self.context_active,
                "wallboardActive": self.wallboard_active,
                "testRemainingSec": test_rem,
                "rms": round(float(self._detector.latest_rms), 5),
                "vad": round(float(self._detector.latest_vad), 3),
                "aec": self._aec.status_dict(),
                "error": self._model_error or self.last_error,
                "config": self.cfg.to_dict(),
                "recent": list(self.recent_transcripts),
                "abSummary": self.ab.summary_dict(),
            }
            if devs_snap is not None:
                out["devices"] = devs_snap
            return out

    # ---------- SSE broadcast & ack ----------

    def _emit_transcript(
        self,
        transcript: str,
        latency_ms: int,
        force_emit: bool = False,
        *,
        from_oww: bool = False,
        from_rescue: bool = False,
        rescue_score: float = 0.0,
    ) -> dict[str, Any]:
        clean = re.sub(r"\s+", " ", transcript or "").strip()
        now_ms = int(time.time() * 1000)
        with self.lock:
            wake = self._effective_wake_word_locked()
            client_wake = (self._client_wake_word or wake).strip().lower() or wake
            was_armed = now_ms < self.armed_until_ms
            has_wake = matches_wake_word(clean, wake)
            if from_rescue:
                # Two-Tier Silent Rescue Gate: STRICT confirmation required — must match wake word
                # AND contain a concise, non-hallucinated gym command tail. Otherwise drop 100% silently!
                cmd_tail = strip_wake_word(clean, wake) if has_wake else ""
                tail_words = re.findall(r"[a-z0-9]+", cmd_tail.lower())
                if not has_wake or not cmd_tail or not tail_words or len(tail_words) > 9 or is_hallucination(cmd_tail):
                    return {
                        "id": 0,
                        "tsMs": now_ms,
                        "transcript": clean,
                        "wakeMatched": False,
                        "emitted": False,
                        "rescueDropped": True,
                        "latencyMs": int(latency_ms),
                        "ackText": "",
                    }
                self.armed_until_ms = 0
                self._oww_awaiting_cmd = False
                display_clean = f"{format_wake_display(wake)}, {cmd_tail}"
                if client_wake != wake and not matches_wake_word(display_clean, client_wake):
                    sse_clean = f"{format_wake_display(client_wake)}, {cmd_tail}"
                    sse_wake = client_wake
                else:
                    sse_clean = display_clean
                    sse_wake = wake
                has_wake = True
                emitted = True
                self.log.info(
                    "Two-Tier Silent Rescue caught (score=%.3f, %dms): %s",
                    float(rescue_score),
                    int(latency_ms),
                    display_clean,
                )
            elif from_oww:
                cmd_tail = strip_wake_word(clean, wake)
                if not cmd_tail:
                    # User said lone "Hey Jarvis" and paused; keep armed + Denon ducked waiting for the command!
                    return {
                        "id": 0,
                        "tsMs": now_ms,
                        "transcript": clean,
                        "wakeMatched": True,
                        "emitted": False,
                        "awaitingCommand": True,
                        "latencyMs": int(latency_ms),
                        "ackText": "",
                    }
                self.armed_until_ms = 0
                self._oww_awaiting_cmd = False
                display_clean = f"{format_wake_display(wake)}, {cmd_tail}"
                if client_wake != wake and not matches_wake_word(display_clean, client_wake):
                    sse_clean = f"{format_wake_display(client_wake)}, {cmd_tail}"
                    sse_wake = client_wake
                else:
                    sse_clean = display_clean
                    sse_wake = wake
                has_wake = True
                emitted = True
            else:
                display_clean = clean
                sse_clean = clean
                sse_wake = wake
                emitted = bool(clean) and (force_emit or has_wake or was_armed)
            if emitted and not force_emit and (from_oww or from_rescue):
                norm_cmd = re.sub(r"[^a-z0-9]+", " ", strip_wake_word(display_clean, wake).lower()).strip()
                now_mono_dedup = time.monotonic()
                if (
                    norm_cmd
                    and norm_cmd == self._last_emitted_norm
                    and (now_mono_dedup - self._last_emitted_mono) < 3.0
                ):
                    return {
                        "id": 0,
                        "tsMs": now_ms,
                        "transcript": display_clean,
                        "wakeMatched": True,
                        "emitted": False,
                        "dedupDropped": True,
                        "latencyMs": int(latency_ms),
                        "ackText": "",
                    }
                if norm_cmd:
                    self._last_emitted_norm = norm_cmd
                    self._last_emitted_mono = now_mono_dedup
            local_handler = self._local_command_handler if emitted else None

        show_running = self._is_show_active()
        local_res: dict[str, Any] | None = None
        if local_handler is not None:
            try:
                if show_running:
                    # While a show is active, only allow an explicit stop command; ignore show self-speech!
                    cand = local_handler(display_clean) if re.search(r"\b(?:stop|kill|cancel|abort|end|enough)\b", display_clean.lower()) else None
                    if isinstance(cand, dict) and cand.get("handled") and cand.get("action") == "stop":
                        local_res = cand
                else:
                    local_res = local_handler(display_clean)
            except Exception as exc:
                self.log.debug("Local voice command handler error: %s", exc)
                local_res = None

        if show_running and not (isinstance(local_res, dict) and local_res.get("handled")):
            self._release_wake_duck_async()
            return {
                "id": 0,
                "tsMs": now_ms,
                "transcript": display_clean,
                "wakeMatched": False,
                "emitted": False,
                "suppressedDuringShow": True,
                "latencyMs": int(latency_ms),
                "ackText": "",
            }

        if isinstance(local_res, dict) and local_res.get("handled"):
            speech_txt = str(local_res.get("speech") or "").strip()
            ack_txt = str(local_res.get("ackText") or speech_txt or "Done").strip()
            with self.lock:
                self.armed_until_ms = 0
                self._oww_awaiting_cmd = False
                self.event_seq += 1
                ev_id = self.event_seq
                entry = {
                    "id": ev_id,
                    "tsMs": now_ms,
                    "transcript": display_clean,
                    "wakeMatched": has_wake,
                    "emitted": True,
                    "localHandled": True,
                    "latencyMs": int(latency_ms),
                    "ackText": ack_txt,
                }
                self.recent_transcripts.appendleft(entry)
                do_readback = bool(self.cfg.speaker_readback and self.context_readback and self.cfg.room_command_readback)
            if do_readback and speech_txt:
                with self.lock:
                    if self._wake_duck_held:
                        self._release_wake_duck_on_play_done = True
                self._extend_wake_duck(6.0)
                if not self.speak_confirmation(speech_txt):
                    self._release_wake_duck_async()
            else:
                self._release_wake_duck_async()
            return entry

        with self.lock:
            if emitted and not from_oww:
                if has_wake and not has_command_tail(clean, wake):
                    self.armed_until_ms = now_ms + ARM_WINDOW_MS
                elif has_wake or was_armed:
                    self.armed_until_ms = 0

            self.event_seq += 1
            ev_id = self.event_seq
            entry = {
                "id": ev_id,
                "tsMs": now_ms,
                "transcript": display_clean,
                "wakeMatched": has_wake,
                "emitted": emitted,
                "latencyMs": int(latency_ms),
                "ackText": "",
            }
            self.recent_transcripts.appendleft(entry)

            if emitted:
                evt = {
                    "type": "transcript",
                    "id": ev_id,
                    "tsMs": now_ms,
                    "transcript": sse_clean,
                    "candidates": [sse_clean],
                    "wakeWord": sse_wake,
                }
                self._events.append(evt)
                self.seq += 1
                self._cond.notify_all()
                sse_count = self.sse_clients
                ctx_active = bool(self.context_active or self.wallboard_active)
                do_alert = bool(self.cfg.speaker_readback and not force_emit)
            else:
                sse_count = self.sse_clients
                ctx_active = bool(self.context_active or self.wallboard_active)
                do_alert = False

        if emitted:
            is_app_cmd = looks_like_workout_app_command(display_clean, wake)
            if sse_count > 0:
                if not ctx_active and do_alert and is_app_cmd:
                    alert_txt = "Can't find session"
                    with self.lock:
                        entry["ackText"] = alert_txt
                        if self._wake_duck_held:
                            self._release_wake_duck_on_play_done = True
                    self._extend_wake_duck(5.0)
                    if not self.speak_confirmation(alert_txt):
                        self._schedule_wake_duck_release(0.5)
                    elif not self._started:
                        self._release_wake_duck_sync()
                else:
                    self._schedule_wake_duck_release(2.2 if ctx_active else 0.5)
            else:
                delivered = False
                if self.on_fallback_transcript is not None:
                    try:
                        delivered = bool(self.on_fallback_transcript(sse_clean))
                    except Exception:
                        delivered = False
                if not delivered and do_alert and is_app_cmd:
                    alert_txt = "App not connected"
                    with self.lock:
                        entry["ackText"] = alert_txt
                        if self._wake_duck_held:
                            self._release_wake_duck_on_play_done = True
                    self._extend_wake_duck(5.0)
                    if not self.speak_confirmation(alert_txt):
                        self._schedule_wake_duck_release(0.5)
                    elif not self._started:
                        self._release_wake_duck_sync()
                else:
                    self._schedule_wake_duck_release(0.5)
        return entry

    def inject_transcript(self, transcript: str) -> dict[str, Any]:
        """Inject a transcript directly into the SSE stream (for testing / dashboard probe)."""
        return self._emit_transcript(transcript, latency_ms=0, force_emit=True)

    def handle_ack(
        self,
        *,
        armed: bool | None = None,
        speech: str | None = None,
        text: str | None = None,
        kind: str | None = None,
        set_commit_mode: str | None = None,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        with self.lock:
            if armed is True:
                self.armed_until_ms = now_ms + ARM_WINDOW_MS
            elif armed is False:
                self.armed_until_ms = 0
            if text and self.recent_transcripts:
                for item in self.recent_transcripts:
                    if item.get("emitted") and not item.get("ackText"):
                        item["ackText"] = str(text)
                        break
            do_readback = bool(self.cfg.speaker_readback and self.context_readback)
            override_mode = str(set_commit_mode or "").strip().lower()
            commit_mode = override_mode if override_mode in ("chime", "voice") else self.cfg.set_commit_mode

        spoke = False
        chime_played = False
        clean_speech = str(speech or "").strip()
        will_play = False
        if do_readback:
            if is_set_commit_ack(text=text or "", kind=kind, speech=clean_speech):
                will_play = (commit_mode == "chime") or bool(clean_speech)
            elif clean_speech:
                will_play = True
        if will_play:
            self._cancel_wake_duck_release_timer()
            with self.lock:
                if self._wake_duck_held:
                    self._release_wake_duck_on_play_done = True
            self._extend_wake_duck(6.0)

        if do_readback:
            if is_set_commit_ack(text=text or "", kind=kind, speech=clean_speech):
                if commit_mode == "chime":
                    chime_played = self.play_commit_chime()
                    spoke = bool(chime_played)
                elif clean_speech:
                    spoke = self.speak_confirmation(clean_speech, speed=1.10)
            elif clean_speech:
                spoke = self.speak_confirmation(clean_speech)
            elif armed is True and not (self._using_oww_spotter() and self._wake_duck_held):
                self.play_ready_chime()

        if not spoke and armed is not True:
            self._release_wake_duck_async()
        elif spoke and not self._started:
            # Synchronous playback already completed when worker thread isn't running
            self._release_wake_duck_sync()
        return {"ok": True, "spoke": spoke, "chime": bool(chime_played)}

    def subscribe_sse(self, after_id: int | None = None, max_replay_age_ms: int = 12_000):
        """Generator yielding SSE `data:` frames (`status` + `transcript`) and keepalive pings.

        Never replays buffered transcripts older than `max_replay_age_ms` (default 12s) on reconnect
        so unlocking a sleeping phone minutes later never fires a stale 'record this' or set log.
        """
        with self.lock:
            self.sse_clients += 1
            self.last_client_seen_mono = time.monotonic()
            last_seq = self.seq
            req_after = int(after_id or 0)
            if req_after <= 0 or req_after > self.event_seq:
                last_ev_id = self.event_seq
            else:
                cutoff_ms = int(time.time() * 1000) - max(0, int(max_replay_age_ms))
                last_ev_id = req_after
                for ev in self._events:
                    ev_id = int(ev.get("id") or 0)
                    ev_ts = int(ev.get("tsMs") or 0)
                    if ev_id > last_ev_id and ev_ts > 0 and ev_ts < cutoff_ms:
                        last_ev_id = ev_id
            self._bump_status_locked()
        try:
            init_payload = json.dumps({"type": "status", **self.status_dict()}, separators=(",", ":"))
            yield f"data: {init_payload}\n\n".encode("utf-8")
            # Immediately flush any genuinely fresh (<12s) missed events from a brief Wi-Fi blip
            fresh_replay: list[str] = []
            with self.lock:
                cutoff_ms = int(time.time() * 1000) - max(0, int(max_replay_age_ms))
                for ev in list(self._events):
                    ev_id = int(ev.get("id") or 0)
                    ev_ts = int(ev.get("tsMs") or 0)
                    if ev_id > last_ev_id and (ev_ts == 0 or ev_ts >= cutoff_ms):
                        last_ev_id = ev_id
                        fresh_replay.append(json.dumps(ev, separators=(",", ":")))
            for frame in fresh_replay:
                yield f"data: {frame}\n\n".encode("utf-8")
            prev_listening = self.listening
            prev_ready = self.is_ready()
            prev_commit_mode = self.cfg.set_commit_mode

            while not self._stop_event.is_set():
                to_send: list[str] = []
                with self.lock:
                    if self.seq == last_seq:
                        self._cond.wait(timeout=15.0)
                    if self._stop_event.is_set():
                        break
                    if self.seq != last_seq:
                        last_seq = self.seq
                        for ev in list(self._events):
                            if int(ev["id"]) > last_ev_id:
                                last_ev_id = int(ev["id"])
                                to_send.append(json.dumps(ev, separators=(",", ":")))
                        cur_listening = self.listening
                        cur_ready = self._asr_transcribe_fn is not None or self._model is not None
                        cur_commit_mode = self.cfg.set_commit_mode
                        if cur_listening != prev_listening or cur_ready != prev_ready or cur_commit_mode != prev_commit_mode:
                            prev_listening = cur_listening
                            prev_ready = cur_ready
                            prev_commit_mode = cur_commit_mode
                            to_send.append(json.dumps({"type": "status", **self.status_dict()}, separators=(",", ":")))

                if to_send:
                    for frame in to_send:
                        yield f"data: {frame}\n\n".encode("utf-8")
                else:
                    yield b": ping\n\n"
        finally:
            with self.lock:
                self.sse_clients = max(0, self.sse_clients - 1)
                self.last_client_seen_mono = time.monotonic()
                self._bump_status_locked()

    # ---------- speaker output ----------

    def _resolve_speaker(self) -> dict[str, Any] | None:
        with self._audio_lock:
            out_dev = resolve_audio_device("out", self.cfg.speaker_match)
            if out_dev is None and self._stream is None:
                with _PORTAUDIO_LOCK:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
                    snap = list_audio_devices(force_refresh=True)
                out_dev = resolve_audio_device("out", self.cfg.speaker_match, snap)
            return out_dev

    def synthesize_tts_pcm(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int] | None:
        """Synthesize speech into `(mono_float32_pcm, sample_rate)` using configured neural voice."""
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return None
        return self._tts.synthesize(clean, voice=voice or self.cfg.tts_voice)

    def _play_pcm_blocking(
        self,
        mono_pcm: np.ndarray,
        src_sr: int,
        lead_ms: int = 420,
        tail_ms: int = 300,
        target_peak: float = 0.84,
        kind: str = "tts",
        allow_router: bool = True,
        mute_mic: bool = True,
    ) -> bool:
        if mono_pcm.size == 0:
            return False
        duck_ctx_factory = None
        if allow_router and self._house_audio_router is not None:
            est_dur = float(mono_pcm.size) / float(max(1, src_sr))
            if mute_mic:
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + est_dur + 0.70)
            try:
                route_res = self._house_audio_router(mono_pcm, src_sr, kind=kind)
            except Exception as exc:
                self.log.debug("House audio router error: %s", exc)
                route_res = None
            if isinstance(route_res, dict):
                if route_res.get("handled"):
                    # Blocking HTTP play finished; hold mic closed briefly for room reverb tail only.
                    if mute_mic:
                        self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + 0.55)
                    return True
                duck_ctx_factory = route_res.get("duckContext")
        if not self._allow_hardware_audio:
            return False
        out_dev = self._resolve_speaker()
        use_winmm = self._winmm_player is not None and _find_winmm_device_id(self.cfg.speaker_match) is not None
        if out_dev is None and not use_winmm:
            return False
        dst_sr = int(out_dev["sampleRate"] or 48_000) if out_dev is not None else 48_000
        dst_ch = max(1, int(out_dev["channels"] or 2)) if out_dev is not None else 2
        if use_winmm:
            dst_sr = 48_000 if dst_sr < 16_000 else dst_sr
            dst_ch = max(1, min(2, dst_ch))
        try:
            payload = prepare_speaker_pcm(
                mono_pcm,
                src_sr=src_sr,
                dst_sr=dst_sr,
                dst_ch=dst_ch,
                lead_ms=lead_ms,
                tail_ms=tail_ms,
                target_peak=target_peak,
            )
            if payload.size == 0:
                return False
            dur_sec = float(payload.shape[0]) / float(dst_sr)
            if mute_mic:
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + dur_sec + 0.35)

            def _do_hw_play() -> bool:
                if use_winmm and self._winmm_player is not None:
                    if self._winmm_player.play(payload, dst_sr, dst_ch, self.cfg.speaker_match):
                        return True
                if out_dev is None:
                    return False
                with _PORTAUDIO_LOCK:
                    sd.play(payload, samplerate=dst_sr, device=out_dev["index"], blocking=True)
                return True

            if callable(duck_ctx_factory):
                with duck_ctx_factory():
                    return _do_hw_play()
            return _do_hw_play()
        except Exception as exc:
            self.log.debug("Speaker playback failed on %s: %s", (out_dev or {}).get("name", self.cfg.speaker_match), exc)
            return False

    def _call_tts_synthesize(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int] | None:
        if abs(float(speed or 1.0) - 1.0) >= 0.01:
            try:
                return self._tts.synthesize(text, voice=voice, speed=float(speed))
            except TypeError:
                pass
        return self._tts.synthesize(text, voice=voice)

    def _speaker_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                kind, data = self._speaker_queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if self._stop_event.is_set() or kind == "stop":
                break
            try:
                if kind == "pcm":
                    pcm, sr, lead_ms, tail_ms, target_peak = data
                    self._play_pcm_blocking(
                        pcm, sr, lead_ms=lead_ms, tail_ms=tail_ms, target_peak=target_peak, kind="chime"
                    )
                elif kind == "ready_chime":
                    # Never interrupt or mute if user is already speaking their command tail ("Armory ... record this").
                    if self._detector.is_user_speaking():
                        continue
                    pcm, sr, lead_ms, tail_ms, target_peak = data
                    self._play_pcm_blocking(
                        pcm,
                        sr,
                        lead_ms=lead_ms,
                        tail_ms=tail_ms,
                        target_peak=target_peak,
                        kind="chime",
                        mute_mic=False,
                    )
                elif kind == "tts":
                    stop_now = False
                    while not self._stop_event.is_set():
                        try:
                            nxt_kind, nxt_data = self._speaker_queue.get_nowait()
                        except queue.Empty:
                            break
                        if nxt_kind == "stop":
                            stop_now = True
                            break
                        if nxt_kind == "tts":
                            data = nxt_data
                    if stop_now or self._stop_event.is_set():
                        break
                    if len(data) >= 3:
                        text, voice, speed = data[0], data[1], float(data[2])
                    else:
                        text, voice = data[0], data[1]
                        speed = 1.0
                    rendered = self._call_tts_synthesize(text, voice=voice, speed=speed)
                    if rendered:
                        pcm, sr = rendered
                        self._play_pcm_blocking(pcm, sr, lead_ms=160, tail_ms=120, target_peak=0.84, kind="tts")
                    else:
                        with self.lock:
                            cached_chime = self._commit_chime_cache
                        if cached_chime is None:
                            cached_chime = _load_commit_chime_pcm(self._root_dir)
                            with self.lock:
                                self._commit_chime_cache = cached_chime
                        cpcm, csr = cached_chime
                        self._play_pcm_blocking(cpcm, csr, lead_ms=140, tail_ms=80, target_peak=0.72, kind="chime")
            except Exception as exc:
                self.log.debug("Speaker worker error: %s", exc)
            finally:
                if kind in ("pcm", "tts") and self._speaker_queue.empty():
                    with self.lock:
                        should_rel = self._release_wake_duck_on_play_done
                    if should_rel:
                        self._release_wake_duck_sync()

    def _prewarm_tts_worker(self) -> None:
        voice = self.cfg.tts_voice
        if getattr(self._tts, "is_kokoro_voice", lambda _v: False)(voice):
            try:
                self._tts._ensure_kokoro()
                with self.lock:
                    self._bump_status_locked()
            except Exception as exc:
                self.log.warning("Kokoro GPU warmup failed (falling back if needed): %s", exc)
        for phrase in PREWARM_TTS_PHRASES:
            if self._stop_event.is_set():
                return
            if self._tts.get_cached(phrase, voice) is not None:
                continue
            try:
                self._tts.synthesize(phrase, voice=voice)
            except Exception:
                pass
            if self._stop_event.wait(0.10):
                return

    def play_ready_chime(self) -> bool:
        """Play a brief 120 ms two-tone ready chime without muting capture (skipped if user is already speaking)."""
        if not self._allow_hardware_audio and self._house_audio_router is None:
            return False
        if self._detector.is_user_speaking():
            return False
        sr = 48_000
        n1 = int(sr * 0.055)
        n2 = int(sr * 0.065)
        t1 = np.linspace(0.0, 0.055, n1, endpoint=False, dtype=np.float32)
        t2 = np.linspace(0.0, 0.065, n2, endpoint=False, dtype=np.float32)
        env1 = np.hanning(n1 * 2)[:n1] if n1 > 4 else np.ones(n1, dtype=np.float32)
        env2 = np.hanning(n2 * 2)[n2:] if n2 > 4 else np.ones(n2, dtype=np.float32)
        tone = np.concatenate([
            np.sin(2.0 * np.pi * 880.0 * t1) * env1,
            np.sin(2.0 * np.pi * 1174.66 * t2) * env2,
        ]).astype(np.float32)
        if self._started:
            try:
                # Pure 120 ms sine chime is non-speech for Silero VAD; never mute capture so a
                # user speaking "Armory ... (beep) record this" is never swallowed.
                self._speaker_queue.put_nowait(("ready_chime", (tone, sr, 100, 60, 0.28)))
                return True
            except queue.Full:
                return False
        return self._play_pcm_blocking(
            tone, sr, lead_ms=100, tail_ms=60, target_peak=0.28, kind="chime", mute_mic=False
        )

    def play_commit_chime(self) -> bool:
        """Play a crisp studio commit chime on PowerConf S330 / House Audio (~0.2-0.4s)."""
        now_mono = time.monotonic()
        with self.lock:
            if (now_mono - self._last_commit_chime_mono) < 1.2:
                return True
            self._last_commit_chime_mono = now_mono
        if self._chime_fn is not None:
            return bool(self._chime_fn("commit"))
        if not self._allow_hardware_audio and self._house_audio_router is None:
            return False
        if self._resolve_speaker() is None and self._house_audio_router is None:
            return False
        with self.lock:
            cached = self._commit_chime_cache
        if cached is None:
            cached = _load_commit_chime_pcm(self._root_dir)
            with self.lock:
                self._commit_chime_cache = cached
        pcm, sr = cached
        if self._started:
            try:
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + 0.65)
                self._speaker_queue.put_nowait(("pcm", (pcm, sr, 140, 80, 0.72)))
                return True
            except queue.Full:
                return False
        return self._play_pcm_blocking(pcm, sr, lead_ms=140, tail_ms=80, target_peak=0.72, kind="chime")

    def speak_confirmation(self, text: str, *, speed: float = 1.0) -> bool:
        """Synthesize natural neural confirmation speech and queue padded playback to House Audio / S330."""
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return False
        now_mono = time.monotonic()
        norm = clean.lower()
        with self.lock:
            if norm == self._last_spoken_text and (now_mono - self._last_spoken_mono) < 3.0:
                return True
            self._last_spoken_text = norm
            self._last_spoken_mono = now_mono
        if self._speak_fn is not None:
            return bool(self._speak_fn(clean))
        if not self._allow_hardware_audio and self._house_audio_router is None:
            return False
        if self._resolve_speaker() is None and self._house_audio_router is None:
            return False
        voice = self.cfg.tts_voice
        eff_speed = float(speed or 1.0)
        if self._started:
            try:
                est_dur = min(2.4, max(0.9, 0.70 + 0.055 * len(clean)))
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + est_dur)
                item: tuple[Any, ...] = (clean, voice, eff_speed) if abs(eff_speed - 1.0) >= 0.01 else (clean, voice)
                self._speaker_queue.put_nowait(("tts", item))
                return True
            except queue.Full:
                return False
        rendered = self._call_tts_synthesize(clean, voice=voice, speed=eff_speed)
        if not rendered:
            return False
        pcm, sr = rendered
        return self._play_pcm_blocking(pcm, sr, lead_ms=160, tail_ms=120, target_peak=0.84, kind="tts")

    # ---------- capture & ASR loops ----------

    def _enqueue_utterance(self, item: tuple[np.ndarray, bool]) -> None:
        try:
            self._utt_queue.put_nowait(item)
        except queue.Full:
            try:
                _ = self._utt_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._utt_queue.put_nowait(item)
            except queue.Full:
                pass

    def _ingest_pcm16_frame(
        self,
        pcm16: np.ndarray,
        *,
        muted: bool = False,
        now_ms: int | None = None,
        now_mono: float | None = None,
    ) -> None:
        """Route 16 kHz mono float32 capture frames through Line-In AEC + openWakeWord (Tier-1) + AEC-VAD (Tier-2)."""
        if pcm16.size == 0:
            return
        if self._aec is not None and self._aec.enabled:
            lin_16k, vad_clean_16k, aec_active = self._aec.process_mic_frame(pcm16)
        else:
            lin_16k, vad_clean_16k, aec_active = pcm16, pcm16, False
        vad_in = np.clip(vad_clean_16k * 3.0, -0.98, 0.98).astype(np.float32) if aec_active else None

        if not self._using_oww_spotter():
            utterances = self._detector.push_samples(lin_16k, muted=muted, vad_samples_16k=vad_in)
            for utt in utterances:
                self._enqueue_utterance((utt, False))
            return

        self._detector.latest_rms = float(np.sqrt(np.mean(lin_16k * lin_16k)))
        cur_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        cur_mono = time.monotonic() if now_mono is None else float(now_mono)

        if cur_ms < self.armed_until_ms:
            if muted:
                return
            utterances = self._detector.push_samples(lin_16k, muted=False, vad_samples_16k=vad_in)
            if utterances:
                self._enqueue_utterance((utterances[0], True))
                # Consume armed window on first follow-up utterance so background group chatter
                # over the remaining seconds never fires as 3-4 fake wake commands.
                self.armed_until_ms = 0
                self._oww_awaiting_cmd = False
            return

        # Idle in openWakeWord mode: NEVER mute openWakeWord during confirmation/error replies so
        # immediate user retries ("Hey Jarvis, done") are never locked out!
        self._detector.latest_vad = 0.0
        self._oww_preroll.append(lin_16k.copy())
        triggered, score = self._oww_spotter.push_samples(lin_16k, now_mono=cur_mono)
        if triggered:
            self._rescue_active = False
            self._rescue_chunks.clear()
            self._rescue_post_samples = 0
            self._aec_vad_detector.reset()
            self._mute_until_mono = 0.0
            self.armed_until_ms = cur_ms + ARM_WINDOW_MS
            self._oww_awaiting_cmd = True
            # Seed an immediately-active utterance with the FULL unbroken 1.2s pre-roll so one-breath
            # short commands ("Hey Jarvis, done") are NEVER sliced mid-word or dropped by minSpeechMs!
            pre_chunks = list(self._oww_preroll)
            self._oww_preroll.clear()
            pre_cat = np.concatenate(pre_chunks).astype(np.float32, copy=False) if pre_chunks else np.zeros(0, dtype=np.float32)
            self._detector.begin_armed_utterance(pre_cat, preroll_sec=1.2, max_post_sec=2.6)
            pool = self._local_cmd_pool
            if pool is not None and not self._stop_event.is_set():
                try:
                    pool.submit(self._on_oww_wake_detected, score)
                    return
                except Exception:
                    pass
            threading.Thread(target=self._on_oww_wake_detected, args=(score,), name="armory-oww-wake", daemon=True).start()
            return

        # Zero-Miss AEC-VAD Safety Net: while Line-In AEC is actively cancelling loud Denon music,
        # run Silero VAD on the -15 dB RES-suppressed stream and silently verify any speech burst
        # via GPU Whisper on the pristine linear-PBFDAF audio (zero false ducks, 100% Vol 69 landing).
        if aec_active:
            self._rescue_active = False
            self._rescue_chunks.clear()
            self._rescue_post_samples = 0
            if muted:
                self._aec_vad_detector.push_samples(lin_16k, muted=True)
            else:
                for utt in self._aec_vad_detector.push_samples(lin_16k, muted=False, vad_samples_16k=vad_in):
                    self._enqueue_utterance((utt, False, True, 0.99))
            return
        elif self._aec_vad_detector._in_speech or self._aec_vad_detector._collected:
            self._aec_vad_detector.reset()

        # Two-Tier Silent Rescue Gate (when Line-In AEC is idle/bypassed): grey-zone hits
        # (rescueThreshold <= score < wakeThreshold) capture quietly and verify via GPU Whisper.
        rescue_thr = float(getattr(self.cfg, "rescue_threshold", 0.0) or 0.0)
        if self._rescue_active:
            self._rescue_chunks.append(lin_16k.copy())
            self._rescue_post_samples += int(lin_16k.size)
            if score > self._rescue_peak_score:
                self._rescue_peak_score = score
            if self._rescue_post_samples >= int(1.9 * SAMPLE_RATE_16K):
                cat = np.concatenate(self._rescue_chunks).astype(np.float32, copy=False)
                pk_sc = self._rescue_peak_score
                self._rescue_active = False
                self._rescue_chunks.clear()
                self._rescue_post_samples = 0
                self._rescue_peak_score = 0.0
                self._enqueue_utterance((cat, False, True, pk_sc))
        elif (
            rescue_thr > 0.0
            and rescue_thr <= score < float(self.cfg.wake_threshold)
            and not muted
            and (cur_mono - self._rescue_last_mono) >= 2.4
        ):
            self._rescue_active = True
            self._rescue_chunks = list(self._oww_preroll)[-32:]  # ~1.0s unbroken pre-roll
            self._rescue_post_samples = 0
            self._rescue_peak_score = score
            self._rescue_last_mono = cur_mono

    def _open_stream(self, *, force_reinit: bool = False) -> bool:
        with self._audio_lock:
            if self._stream is not None:
                return True
            with _PORTAUDIO_LOCK:
                if force_reinit:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
                snap = list_audio_devices(force_refresh=force_reinit)
                dev = resolve_audio_device("in", self.cfg.device_match, snap)
                if dev is None and not force_reinit:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
                    snap = list_audio_devices(force_refresh=True)
                    dev = resolve_audio_device("in", self.cfg.device_match, snap)
            if dev is None:
                with self.lock:
                    self.last_error = f"Audio input '{self.cfg.device_match}' not connected"
                    self.listening = False
                    self._bump_status_locked()
                return False

            if dev["hostApi"] != "Windows WASAPI" and not self._host_api_diag_logged:
                self._host_api_diag_logged = True
                q = (self.cfg.device_match or "").lower()
                avail = [
                    f"{d.get('name')} [{d.get('hostApi')}]"
                    for d in snap.get("inputs", [])
                    if not q or q in str(d.get("name", "")).lower()
                ]
                self.log.info("WASAPI not chosen for '%s'; candidates=%s", self.cfg.device_match, avail)

            sr = int(dev["sampleRate"] or 48_000)
            max_ch = max(1, int(dev["channels"] or 1))
            ch_order = [min(2, max_ch), 1] if dev["hostApi"] == "Windows WDM-KS" and max_ch >= 2 else [1, min(2, max_ch)]
            seen_ch: list[int] = []
            for ch in ch_order:
                if ch not in seen_ch:
                    seen_ch.append(ch)

            def _audio_cb(indata: np.ndarray, _frames: int, _time_info: Any, _status: Any) -> None:
                if self._stop_event.is_set():
                    return
                mono = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata.reshape(-1)
                if self._on_pcm_chunk is not None:
                    try:
                        self._on_pcm_chunk(mono.copy(), sr, time.time())
                    except Exception:
                        pass
                pcm16 = resample_to_mono_16k(mono, sr)
                muted = time.monotonic() < self._mute_until_mono
                self._ingest_pcm16_frame(pcm16, muted=muted)

            last_err = ""
            for ch in seen_ch:
                try:
                    with _PORTAUDIO_LOCK:
                        stream = sd.InputStream(
                            device=dev["index"],
                            samplerate=sr,
                            channels=ch,
                            dtype="float32",
                            blocksize=int(round(sr * 0.032)),
                            callback=_audio_cb,
                        )
                        stream.start()
                    self._stream = stream
                    self._stream_sr = sr
                    self._stream_dev_name = dev["name"]
                    self._stream_host_api = dev["hostApi"]
                    if dev["hostApi"] == "Windows WASAPI":
                        self._wasapi_probe_misses = 0
                    self._detector.reset()
                    self._aec_vad_detector.reset()
                    self._open_aec_stream_locked(snap, main_dev_index=int(dev["index"]))
                    with self.lock:
                        self.listening = True
                        self.last_error = None
                        self._bump_status_locked()
                    self.log.info("Room mic open: %s [%s @ %d Hz, %d ch]", dev["name"], dev["hostApi"], sr, ch)
                    return True
                except Exception as exc:
                    last_err = str(exc)

            if not force_reinit:
                with _PORTAUDIO_LOCK:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
            with self.lock:
                self.listening = False
                self.last_error = last_err or f"Could not open {dev['name']}"
                self._bump_status_locked()
            return False

    def _open_aec_stream_locked(self, snap: dict[str, Any], *, main_dev_index: int) -> None:
        """Open optional Line-In reference stream for real-time Denon PRE OUT echo cancellation (fail-safe)."""
        self._close_aec_stream_locked()
        if not (self.cfg.aec_enabled and self._allow_hardware_audio and self.cfg.aec_device_match):
            return
        aec_dev = resolve_audio_device("in", self.cfg.aec_device_match, snap)
        if aec_dev is None or int(aec_dev["index"]) == int(main_dev_index):
            self._aec.stream_open = False
            self._aec.device_name = ""
            self._aec.host_api = ""
            self._aec.last_error = f"AEC ref '{self.cfg.aec_device_match}' not found"
            return
        aec_sr = int(aec_dev["sampleRate"] or 48_000)
        aec_max_ch = max(1, int(aec_dev["channels"] or 1))
        aec_ch_order = [min(2, aec_max_ch), 1] if aec_max_ch >= 2 else [1]

        def _aec_cb(indata: np.ndarray, _frames: int, _time_info: Any, _status: Any) -> None:
            if self._stop_event.is_set():
                return
            mono = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata.reshape(-1)
            ref16 = resample_to_mono_16k(mono, aec_sr)
            self._aec.push_ref_samples(ref16)

        last_err = ""
        for ch in aec_ch_order:
            try:
                self._aec.reset()
                with _PORTAUDIO_LOCK:
                    astream = sd.InputStream(
                        device=aec_dev["index"],
                        samplerate=aec_sr,
                        channels=ch,
                        dtype="float32",
                        blocksize=int(round(aec_sr * 0.032)),
                        callback=_aec_cb,
                    )
                    astream.start()
                self._aec_stream = astream
                self._aec.stream_open = True
                self._aec.device_name = str(aec_dev["name"])
                self._aec.host_api = str(aec_dev["hostApi"])
                self._aec.last_error = None
                self.log.info(
                    "Line-In AEC ref open: %s [%s @ %d Hz, %d ch]",
                    aec_dev["name"],
                    aec_dev["hostApi"],
                    aec_sr,
                    ch,
                )
                return
            except Exception as exc:
                last_err = str(exc)
        self._aec.stream_open = False
        self._aec.last_error = last_err or f"Could not open AEC ref {aec_dev['name']}"
        self.log.warning("Line-In AEC ref unavailable (continuing S330-only): %s", self._aec.last_error)

    def _close_aec_stream_locked(self) -> None:
        astream = self._aec_stream
        self._aec_stream = None
        if astream is not None:
            with _PORTAUDIO_LOCK:
                try:
                    astream.stop()
                except Exception:
                    pass
                try:
                    astream.close()
                except Exception:
                    pass
            self.log.info("Line-In AEC ref closed (%s)", self._aec.device_name or "idle")
        self._aec.stream_open = False
        self._aec.reset()

    def _close_stream(self) -> None:
        with self._audio_lock:
            self._close_aec_stream_locked()
            stream = self._stream
            self._stream = None
            if stream is not None:
                with _PORTAUDIO_LOCK:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                self.log.info("Room mic closed (%s)", self._stream_dev_name or "idle")
            self._stream_dev_name = ""
            self._stream_host_api = ""
        self._detector.reset()
        self._aec_vad_detector.reset()
        with self.lock:
            if self.listening:
                self.listening = False
                self._bump_status_locked()

    def _capture_supervisor_loop(self) -> None:
        last_wasapi_probe = time.monotonic()
        last_denon_refresh = 0.0
        probe_intervals = (30.0, 120.0, 600.0)
        pending_force_reinit = False
        while not self._stop_event.is_set():
            want = self.should_capture_now()
            now_m = time.monotonic()
            now_ms = int(time.time() * 1000)

            # Auto-release wake duck if armed window expired with no command spoken
            if (
                self._oww_awaiting_cmd
                and now_ms >= self.armed_until_ms
                and not self._detector.is_user_speaking()
            ):
                self._oww_awaiting_cmd = False
                self._detector.reset()
                if self._oww_spotter is not None:
                    self._oww_spotter.reset()
                self._release_wake_duck_async()
                with self.lock:
                    self._bump_status_locked()

            # Periodically refresh Denon status cache while listening & idle so wake-duck executes in 1 HTTP call
            if (
                want
                and self._using_oww_spotter()
                and not self._oww_awaiting_cmd
                and not self._wake_duck_held
                and (now_m - last_denon_refresh) >= 3.0
            ):
                last_denon_refresh = now_m
                ref_fn = self._wake_duck_cbs.get("refresh")
                if callable(ref_fn):
                    threading.Thread(target=ref_fn, name="armory-denon-cache", daemon=True).start()

            if want:
                if self._stream is None:
                    if not self._open_stream(force_reinit=pending_force_reinit):
                        pending_force_reinit = False
                        self._stop_event.wait(2.5)
                        continue
                    pending_force_reinit = False
                    last_wasapi_probe = time.monotonic()
                else:
                    # Check stream health
                    try:
                        active = bool(self._stream.active)
                    except Exception:
                        active = False
                    if not active:
                        self._close_stream()
                        self._stop_event.wait(1.0)
                        continue
                    # If on WDM-KS fallback (RDP) and idle, probe WASAPI with exponential backoff (stop after 3 misses)
                    if (
                        self._stream_host_api == "Windows WDM-KS"
                        and self._wasapi_probe_misses < len(probe_intervals)
                        and not self._detector._in_speech
                        and (time.time() * 1000.0) >= self.armed_until_ms
                        and time.monotonic() >= self._mute_until_mono
                    ):
                        interval = probe_intervals[self._wasapi_probe_misses]
                        if (time.monotonic() - last_wasapi_probe) >= interval:
                            self._wasapi_probe_misses += 1
                            last_wasapi_probe = time.monotonic()
                            pending_force_reinit = True
                            self._close_stream()
                            continue
            else:
                if self._stream is not None:
                    self._close_stream()

            self._stop_event.wait(0.35)

        self._close_stream()

    def _transcribe_utterance(self, audio16: np.ndarray) -> str:
        with self.lock:
            wake = self._effective_wake_word_locked()
            phrases = list(self.bias_phrases)
            fn = self._asr_transcribe_fn
            model = self._model
        prompt, hotwords = build_whisper_prompt(wake, phrases)
        if fn is not None:
            raw = str(fn(audio16, prompt, hotwords) or "").strip()
        elif model is None:
            return ""
        else:
            prepared = prepare_asr_audio(audio16)
            eff_beam = max(1, min(5, int(self.cfg.beam_size or 1)))
            segs, _info = model.transcribe(
                prepared,
                language="en",
                beam_size=eff_beam,
                best_of=eff_beam,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=False,
                no_speech_threshold=0.72,
                log_prob_threshold=-1.2,
                repetition_penalty=1.15,
                no_repeat_ngram_size=3,
                max_new_tokens=36,
                initial_prompt=prompt,
                hotwords=hotwords,
            )
            raw = " ".join(s.text.strip() for s in segs).strip()
        if raw and is_prompt_echo(raw, prompt):
            self.log.debug("Dropped Whisper prompt-echo hallucination: %s", raw)
            return ""
        return raw

    def _asr_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw_item = self._utt_queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                break
            from_rescue = False
            rescue_score = 0.0
            if isinstance(raw_item, tuple) and len(raw_item) == 4:
                audio16, from_oww, from_rescue, rescue_score = raw_item
            elif isinstance(raw_item, tuple) and len(raw_item) == 2:
                audio16, from_oww = raw_item
            else:
                audio16, from_oww = raw_item, False
            t0 = time.perf_counter()
            try:
                text = self._transcribe_utterance(audio16)
            except Exception as exc:
                self.log.warning("ASR transcription error: %s", exc)
                continue
            dt_ms = int(round((time.perf_counter() - t0) * 1000.0))
            if not text or is_hallucination(text):
                continue
            pool = self._local_cmd_pool
            if pool is not None:
                try:
                    pool.submit(
                        self._emit_transcript,
                        text,
                        dt_ms,
                        False,
                        from_oww=from_oww,
                        from_rescue=from_rescue,
                        rescue_score=rescue_score,
                    )
                    continue
                except Exception:
                    pass
            self._emit_transcript(
                text,
                latency_ms=dt_ms,
                force_emit=False,
                from_oww=from_oww,
                from_rescue=from_rescue,
                rescue_score=rescue_score,
            )
