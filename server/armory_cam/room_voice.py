"""OptiPlex Room Voice Engine — PowerConf S330 far-field mic capture, Silero VAD, faster-whisper ASR, and TTS readback.

Designed to live inside `ArmoryCamDVR` (`armory_cam_server.py`) and relay spoken commands to
Armory Fit (`POST /api/voice` / `GET /api/voice/events`) while automatically opening the
PowerConf S330 microphone ONLY during active Strength workouts (or explicit dashboard test /
"always" mode) via WASAPI Shared Mode when local so Windows 11 Voice Typing (`Win + H`)
stays unblocked.
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
import ctypes
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
import wave

import numpy as np
from scipy.signal import resample_poly
import sounddevice as sd

SAMPLE_RATE_16K = 16_000
VAD_FRAME_SAMPLES = 512  # 32 ms @ 16 kHz (native Silero VAD window)
VAD_CONTEXT_SAMPLES = 64
PRE_ROLL_FRAMES = 14     # ~448 ms pre-roll so "Armory" onset is never clipped
ARM_WINDOW_MS = 7_000
MAX_RECENT_TRANSCRIPTS = 12
HOST_API_PREFERENCE = (
    "Windows WASAPI",
    "MME",
    "Windows DirectSound",
    "Windows WDM-KS",
)
DEFAULT_TTS_VOICE = "en-US-AndrewMultilingualNeural"
TTS_VOICES = (
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "sapi",
)
_LEGACY_TTS_VOICE_MAP = {
    "en-US-AndrewNeural": "en-US-AndrewMultilingualNeural",
    "en-US-AvaNeural": "en-US-AvaMultilingualNeural",
}
PREWARM_TTS_PHRASES = (
    "Armory room mic ready",
    "Armory mic on",
    "225 for 8, got it",
    "Checked, got it",
    "Didn't catch that",
    "Rest skipped",
    "Set added",
    "Undid last",
    "Next exercise",
    "Previous exercise",
    "Recording Rack",
    "Recording Smith",
    "Clipping last set",
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
})


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _no_window_kwargs() -> dict[str, Any]:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def is_hallucination(text: str) -> bool:
    """Reject known Whisper silence/noise hallucinations and punctuation-only strings."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return True
    if cleaned in HALLUCINATION_EXACT:
        return True
    words = cleaned.split()
    if len(words) >= 3 and len(set(words)) == 1 and words[0] in {"you", "the", "a", "uh", "um", "bye"}:
        return True
    return False


def normalize_wake_text(text: str) -> str:
    """Lightweight normalization aligned with `src/lifts/voiceParser.ts` wake matching."""
    s = text.lower()
    s = re.sub(r"[—–\-/,]+", " ", s)
    s = re.sub(r"['’]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _wake_regex(wake_word: str) -> re.Pattern[str]:
    wake_raw = (wake_word or "armory").strip().lower() or "armory"
    if wake_raw == "armory":
        alts = [
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
        ]
    else:
        alts = [wake_raw]
    joined = "|".join(re.escape(a) for a in alts)
    return re.compile(rf"\b(?:hey\s+|ok\s+|okay\s+)?(?:{joined})\b", re.IGNORECASE)


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
    """Build concise Whisper `initial_prompt` + `hotwords` seeded with gym commands and current exercises."""
    wake = (wake_word or "armory").strip() or "armory"
    core = [
        wake,
        "225 for 8",
        "135 for 10",
        "check",
        "done",
        "for 10",
        "by 8",
        "rpe 8",
        "rpe 8.5",
        "next exercise",
        "previous exercise",
        "add set",
        "skip rest",
        "add 30 seconds",
        "record rack",
        "record smith",
        "clip that",
        "replay slow",
        "buddy mode",
        "undo",
        "open the gym",
        "close the gym",
        "SmartSpin TV",
        "wallboard",
        "all TVs",
        "play music",
        "pause music",
        "next song",
        "skip song",
        "Matt just hit a PR",
        "Matt just got here",
        "Matt is here",
        "Matt is in the gym",
        "Ryder is here",
        "Ryder debut",
        "Gabe is here",
        "Josh is here",
        "Jared and Mark",
        "stop show",
        "stop",
        "enough",
        "end",
        "cancel",
    ]
    seen = {x.lower() for x in core}
    extras: list[str] = []
    for raw in bias_phrases:
        clean = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        extras.append(clean)
        if len(extras) >= 24:
            break
    all_terms = core + extras
    prompt = ", ".join(all_terms) + "."
    hotwords = ", ".join([wake, "check", "rpe", "smith", "rack", "replay", "buddy", *extras[:12]])
    return prompt, hotwords


@dataclass
class RoomVoiceConfig:
    enabled: bool = True
    device_match: str = "PowerConf S330"
    speaker_match: str = "PowerConf S330"
    listen_mode: str = "workout"  # "workout" | "always"
    model_size: str = "base.en"   # empirically benchmarked <= 1.2s on i7-4790
    wake_word: str = "armory"
    speaker_readback: bool = True
    tts_voice: str = DEFAULT_TTS_VOICE
    vad_threshold: float = 0.45
    min_speech_ms: int = 280
    end_silence_ms: int = 620
    max_utterance_sec: float = 7.5
    rms_gate: float = 0.004

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "deviceMatch": self.device_match,
            "speakerMatch": self.speaker_match,
            "listenMode": self.listen_mode,
            "modelSize": self.model_size,
            "wakeWord": self.wake_word,
            "speakerReadback": bool(self.speaker_readback),
            "ttsVoice": self.tts_voice,
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
        model_raw = str(raw.get("modelSize", "base.en")).strip()
        model_size = model_raw if model_raw in ("tiny.en", "base.en", "small.en") else "base.en"
        wake = str(raw.get("wakeWord") or "armory").strip().lower() or "armory"
        tts_raw = str(raw.get("ttsVoice") or DEFAULT_TTS_VOICE).strip()
        tts_raw = _LEGACY_TTS_VOICE_MAP.get(tts_raw, tts_raw)
        tts_voice = tts_raw if tts_raw in TTS_VOICES else DEFAULT_TTS_VOICE
        return cls(
            enabled=bool(raw.get("enabled", True)),
            device_match=str(raw.get("deviceMatch", "PowerConf S330") or "").strip(),
            speaker_match=str(raw.get("speakerMatch", "PowerConf S330") or "").strip(),
            listen_mode=listen_mode,
            model_size=model_size,
            wake_word=wake,
            speaker_readback=bool(raw.get("speakerReadback", True)),
            tts_voice=tts_voice,
            vad_threshold=_clamp(float(raw.get("vadThreshold", 0.45) or 0.45), 0.15, 0.90),
            min_speech_ms=max(160, min(1200, int(raw.get("minSpeechMs", 280) or 280))),
            end_silence_ms=max(250, min(2000, int(raw.get("endSilenceMs", 620) or 620))),
            max_utterance_sec=_clamp(float(raw.get("maxUtteranceSec", 7.5) or 7.5), 2.5, 15.0),
            rms_gate=_clamp(float(raw.get("rmsGate", 0.004) or 0.004), 0.0005, 0.05),
        )


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
        vad_threshold: float = 0.45,
        min_speech_ms: int = 280,
        end_silence_ms: int = 620,
        max_utterance_sec: float = 7.5,
        rms_gate: float = 0.004,
    ) -> None:
        self.vad = vad or SileroStreamingVad()
        self.vad_threshold = vad_threshold
        self.min_speech_frames = max(2, int(round(min_speech_ms / 32.0)))
        self.end_silence_frames = max(3, int(round(end_silence_ms / 32.0)))
        self.max_frames = max(20, int(round((max_utterance_sec * 1000.0) / 32.0)))
        self.rms_gate = rms_gate

        self._buf16 = np.zeros(0, dtype=np.float32)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=PRE_ROLL_FRAMES)
        self._in_speech = False
        self._onset_streak = 0
        self._silence_streak = 0
        self._speech_frames = 0
        self._collected: list[np.ndarray] = []
        self.latest_rms = 0.0
        self.latest_vad = 0.0

    def configure(self, cfg: RoomVoiceConfig) -> None:
        self.vad_threshold = cfg.vad_threshold
        self.min_speech_frames = max(2, int(round(cfg.min_speech_ms / 32.0)))
        self.end_silence_frames = max(3, int(round(cfg.end_silence_ms / 32.0)))
        self.max_frames = max(20, int(round((cfg.max_utterance_sec * 1000.0) / 32.0)))
        self.rms_gate = cfg.rms_gate

    def reset(self) -> None:
        self._buf16 = np.zeros(0, dtype=np.float32)
        self._pre_roll.clear()
        self._in_speech = False
        self._onset_streak = 0
        self._silence_streak = 0
        self._speech_frames = 0
        self._collected.clear()
        self.vad.reset()

    def push_samples(self, samples_16k: np.ndarray, muted: bool = False) -> list[np.ndarray]:
        """Ingest 16 kHz mono float32 samples; returns zero or more completed utterance arrays."""
        if muted:
            self.reset()
            self.latest_rms = 0.0
            self.latest_vad = 0.0
            return []

        if samples_16k.size == 0:
            return []
        self._buf16 = np.concatenate([self._buf16, samples_16k.astype(np.float32, copy=False)])
        completed: list[np.ndarray] = []

        while self._buf16.size >= VAD_FRAME_SAMPLES:
            frame = self._buf16[:VAD_FRAME_SAMPLES]
            self._buf16 = self._buf16[VAD_FRAME_SAMPLES:]

            rms = float(np.sqrt(np.mean(frame * frame)))
            self.latest_rms = rms
            if rms < self.rms_gate and not self._in_speech:
                prob = 0.0
                self.vad.reset()
            else:
                prob = self.vad.score_frame(frame, rms_gate=self.rms_gate)
            self.latest_vad = prob

            is_speech = (prob >= self.vad_threshold) and (rms >= self.rms_gate * 0.5)

            if not self._in_speech:
                self._pre_roll.append(frame.copy())
                if is_speech:
                    self._onset_streak += 1
                    if self._onset_streak >= 2:
                        self._in_speech = True
                        self._collected = list(self._pre_roll)
                        self._speech_frames = self._onset_streak
                        self._silence_streak = 0
                        self._onset_streak = 0
                else:
                    self._onset_streak = 0
            else:
                self._collected.append(frame.copy())
                if is_speech:
                    self._speech_frames += 1
                    self._silence_streak = 0
                else:
                    self._silence_streak += 1

                if self._silence_streak >= self.end_silence_frames or len(self._collected) >= self.max_frames:
                    if self._speech_frames >= self.min_speech_frames:
                        completed.append(np.concatenate(self._collected).astype(np.float32))
                    self._collected.clear()
                    self._pre_roll.clear()
                    self._in_speech = False
                    self._onset_streak = 0
                    self._silence_streak = 0
                    self._speech_frames = 0
                    self.vad.reset()

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


class _SapiSynthesizer:
    """Persistent hidden PowerShell `System.Speech` WAV synthesizer (offline fallback at natural rate)."""

    def __init__(self, max_cache: int = 48) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._cache: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
        self._max_cache = max_cache

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        ps_script = (
            "Add-Type -AssemblyName System.Speech\n"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$s.Rate = 0\n"
            "while (($line = [Console]::In.ReadLine()) -ne $null) {\n"
            "  if ($line -eq '') { continue }\n"
            "  $ms = New-Object System.IO.MemoryStream\n"
            "  $s.SetOutputToWaveStream($ms)\n"
            "  $s.Speak($line)\n"
            "  $s.SetOutputToNull()\n"
            "  [Console]::Out.WriteLine([Convert]::ToBase64String($ms.ToArray()))\n"
            "  [Console]::Out.Flush()\n"
            "  $ms.Dispose()\n"
            "}\n"
        )
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            **_no_window_kwargs(),
        )
        return self._proc

    def synthesize(self, text: str) -> tuple[np.ndarray, int] | None:
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return None
        key = clean.lower()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            try:
                proc = self._ensure_proc()
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(clean + "\n")
                proc.stdin.flush()
                b64 = proc.stdout.readline().strip()
                if not b64:
                    raise RuntimeError("Empty TTS response")
                wav_bytes = base64.b64decode(b64)
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    sr = wf.getframerate()
                    ch = wf.getnchannels()
                    pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
                    if ch > 1:
                        pcm = pcm.reshape(-1, ch).mean(axis=1)
                self._cache[key] = (pcm, sr)
                while len(self._cache) > self._max_cache:
                    self._cache.popitem(last=False)
                return pcm, sr
            except Exception:
                if self._proc is not None:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                    self._proc = None
                return None


class _NeuralTtsSynthesizer:
    """Microsoft Neural `edge-tts` synthesizer with persistent WAV cache + SAPI5 fallback."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        sapi_fallback: _SapiSynthesizer | None = None,
        max_cache: int = 64,
    ) -> None:
        self.cache_dir = cache_dir
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._sapi = sapi_fallback or _SapiSynthesizer()
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
        self._max_cache = max_cache

    def close(self) -> None:
        self._sapi.close()

    @staticmethod
    def _cache_key(text: str, voice: str) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip().lower()
        return hashlib.sha1(f"{voice}|{clean}".encode("utf-8")).hexdigest()

    def get_cached(self, text: str, voice: str) -> tuple[np.ndarray, int] | None:
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean or voice == "sapi":
            return None
        key = self._cache_key(clean, voice)
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

    def _put_cached(self, text: str, voice: str, pcm: np.ndarray, sr: int) -> None:
        key = self._cache_key(text, voice)
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

    @staticmethod
    def _synth_edge_pcm(text: str, voice: str, attempts: int = 2) -> tuple[np.ndarray, int]:
        import av
        import edge_tts

        async def _fetch() -> bytes:
            comm = edge_tts.Communicate(text, voice=voice, rate="+0%")
            chunks: list[bytes] = []
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        mp3_bytes = b""
        last_exc: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                mp3_bytes = asyncio.run(_fetch())
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

    def synthesize(self, text: str, voice: str = DEFAULT_TTS_VOICE) -> tuple[np.ndarray, int] | None:
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return None
        if voice == "sapi":
            return self._sapi.synthesize(clean)
        cached = self.get_cached(clean, voice)
        if cached is not None:
            return cached
        try:
            pcm, sr = self._synth_edge_pcm(clean, voice)
            self._put_cached(clean, voice, pcm, sr)
            return pcm, sr
        except Exception:
            return self._sapi.synthesize(clean)


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
    ) -> None:
        self.cfg = config or RoomVoiceConfig()
        self.root_dir = root_dir
        self.on_fallback_transcript = on_fallback_transcript
        self._asr_transcribe_fn = asr_transcribe_fn
        self._speak_fn = speak_fn
        self._allow_hardware_audio = allow_hardware_audio
        self._house_audio_router = house_audio_router
        self._local_command_handler = local_command_handler
        self._on_pcm_chunk = on_pcm_chunk

        self.log = logging.getLogger("armory-voice")
        self.lock = threading.RLock()
        self._audio_lock = threading.Lock()
        self._cond = threading.Condition(self.lock)

        self._started = False
        self._stop_event = threading.Event()
        self._worker_threads: list[threading.Thread] = []
        self._utt_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=4)
        self._speaker_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=16)

        self._model: Any = None
        self._model_size_loaded: str = ""
        self._model_loading = False
        self._model_error: str | None = None

        self._stream: sd.InputStream | None = None
        self._stream_sr: int = 48_000
        self._stream_dev_name: str = ""
        self._stream_host_api: str = ""
        self._detector = UtteranceDetector(
            vad_threshold=self.cfg.vad_threshold,
            min_speech_ms=self.cfg.min_speech_ms,
            end_silence_ms=self.cfg.end_silence_ms,
            max_utterance_sec=self.cfg.max_utterance_sec,
            rms_gate=self.cfg.rms_gate,
        )
        self._sapi = _SapiSynthesizer()
        cache_dir = (root_dir / "tts_cache") if root_dir is not None else None
        self._tts = _NeuralTtsSynthesizer(cache_dir=cache_dir, sapi_fallback=self._sapi)
        self._winmm_player = _WinMmSpeakerPlayer() if sys.platform == "win32" else None
        self.ab = VoiceAbTracker(root_dir=root_dir)

        self.listening = False
        self.last_error: str | None = None
        self.sse_clients = 0
        self.context_active = False
        self.wallboard_active = False
        self.context_readback = True
        self.context_wake_word = self.cfg.wake_word
        self.bias_phrases: list[str] = []
        self.test_listen_until_mono = 0.0
        self.last_client_seen_mono = 0.0
        self.armed_until_ms = 0
        self._mute_until_mono = 0.0

        self.seq = 0
        self.event_seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=40)
        self.recent_transcripts: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_TRANSCRIPTS)

    # ---------- lifecycle ----------

    def start(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
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

    def stop_s330_playback(self) -> None:
        """Cancel any active PowerConf S330 speaker playback cleanly."""
        if self._winmm_player is not None:
            self._winmm_player.stop()

    def update_config(self, raw: dict[str, Any]) -> RoomVoiceConfig:
        with self.lock:
            merged = {**self.cfg.to_dict(), **(raw or {})}
            next_cfg = RoomVoiceConfig.from_dict(merged)
            dev_changed = next_cfg.device_match != self.cfg.device_match
            model_changed = next_cfg.model_size != self.cfg.model_size
            self.cfg = next_cfg
            self.context_wake_word = next_cfg.wake_word
            self._detector.configure(next_cfg)
            if model_changed:
                self._model = None
                self._model_size_loaded = ""
            if self._started and next_cfg.enabled:
                self._ensure_model_loading_locked()
            self._bump_status_locked()
        if dev_changed or not next_cfg.enabled:
            self._close_stream()
        return next_cfg

    def _ensure_model_loading_locked(self) -> None:
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

    def _load_model_worker(self, model_size: str) -> None:
        try:
            from faster_whisper import WhisperModel

            t0 = time.perf_counter()
            model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)
            dt = time.perf_counter() - t0
            with self.lock:
                if self.cfg.model_size == model_size:
                    self._model = model
                    self._model_size_loaded = model_size
                    self._model_loading = False
                    self._model_error = None
                    self._bump_status_locked()
            self.log.info("Loaded faster-whisper %s (cpu/int8) in %.2fs", model_size, dt)
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
                self.context_wake_word = str(wake_word).strip().lower()
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
                "wakeWord": self._effective_wake_word_locked(),
                "device": dev_label,
                "hostApi": self._stream_host_api or (in_dev["hostApi"] if in_dev else ""),
                "speaker": out_dev["name"] if out_dev else self.cfg.speaker_match,
                "speakerReadback": bool(self.cfg.speaker_readback),
                "ttsVoice": self.cfg.tts_voice,
                "sseClients": self.sse_clients,
                "contextActive": self.context_active,
                "wallboardActive": self.wallboard_active,
                "testRemainingSec": test_rem,
                "rms": round(float(self._detector.latest_rms), 5),
                "vad": round(float(self._detector.latest_vad), 3),
                "error": self._model_error or self.last_error,
                "config": self.cfg.to_dict(),
                "recent": list(self.recent_transcripts),
                "abSummary": self.ab.summary_dict(),
            }
            if devs_snap is not None:
                out["devices"] = devs_snap
            return out

    # ---------- SSE broadcast & ack ----------

    def _emit_transcript(self, transcript: str, latency_ms: int, force_emit: bool = False) -> dict[str, Any]:
        clean = re.sub(r"\s+", " ", transcript or "").strip()
        now_ms = int(time.time() * 1000)
        with self.lock:
            wake = self._effective_wake_word_locked()
            was_armed = now_ms < self.armed_until_ms
            has_wake = matches_wake_word(clean, wake)
            emitted = bool(clean) and (force_emit or has_wake or was_armed)
            local_handler = self._local_command_handler if emitted else None

        local_res: dict[str, Any] | None = None
        if local_handler is not None:
            try:
                local_res = local_handler(clean)
            except Exception as exc:
                self.log.debug("Local voice command handler error: %s", exc)
                local_res = None

        if isinstance(local_res, dict) and local_res.get("handled"):
            speech_txt = str(local_res.get("speech") or "").strip()
            ack_txt = str(local_res.get("ackText") or speech_txt or "Done").strip()
            with self.lock:
                self.armed_until_ms = 0
                self.event_seq += 1
                ev_id = self.event_seq
                entry = {
                    "id": ev_id,
                    "tsMs": now_ms,
                    "transcript": clean,
                    "wakeMatched": has_wake,
                    "emitted": True,
                    "localHandled": True,
                    "latencyMs": int(latency_ms),
                    "ackText": ack_txt,
                }
                self.recent_transcripts.appendleft(entry)
                do_readback = bool(self.cfg.speaker_readback and self.context_readback)
            if do_readback and speech_txt:
                self.speak_confirmation(speech_txt)
            return entry

        with self.lock:
            if emitted:
                if has_wake and not has_command_tail(clean, wake):
                    self.armed_until_ms = now_ms + ARM_WINDOW_MS
                elif has_wake or was_armed:
                    self.armed_until_ms = 0

            self.event_seq += 1
            ev_id = self.event_seq
            entry = {
                "id": ev_id,
                "tsMs": now_ms,
                "transcript": clean,
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
                    "transcript": clean,
                    "candidates": [clean],
                    "wakeWord": wake,
                }
                self._events.append(evt)
                self.seq += 1
                self._cond.notify_all()
                sse_count = self.sse_clients
            else:
                sse_count = self.sse_clients

        if emitted and sse_count == 0 and self.on_fallback_transcript is not None:
            try:
                self.on_fallback_transcript(clean)
            except Exception:
                pass
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

        spoke = False
        clean_speech = str(speech or "").strip()
        if clean_speech and do_readback:
            spoke = self.speak_confirmation(clean_speech)
        elif armed is True and not clean_speech and do_readback:
            self.play_ready_chime()
        return {"ok": True, "spoke": spoke}

    def subscribe_sse(self, after_id: int | None = None):
        """Generator yielding SSE `data:` frames (`status` + `transcript`) and keepalive pings."""
        with self.lock:
            self.sse_clients += 1
            self.last_client_seen_mono = time.monotonic()
            last_seq = self.seq
            last_ev_id = int(after_id or 0)
            self._bump_status_locked()
        try:
            init_payload = json.dumps({"type": "status", **self.status_dict()}, separators=(",", ":"))
            yield f"data: {init_payload}\n\n".encode("utf-8")
            prev_listening = self.listening
            prev_ready = self.is_ready()

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
                        if cur_listening != prev_listening or cur_ready != prev_ready:
                            prev_listening = cur_listening
                            prev_ready = cur_ready
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
        """Synthesize speech into `(mono_float32_pcm, sample_rate)` using configured neural/SAPI voice."""
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
    ) -> bool:
        if mono_pcm.size == 0:
            return False
        duck_ctx_factory = None
        if allow_router and self._house_audio_router is not None:
            try:
                route_res = self._house_audio_router(mono_pcm, src_sr, kind=kind)
            except Exception as exc:
                self.log.debug("House audio router error: %s", exc)
                route_res = None
            if isinstance(route_res, dict):
                if route_res.get("handled"):
                    dur = float(route_res.get("durationSec") or (float(mono_pcm.size) / float(max(1, src_sr))))
                    self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + dur + 0.70)
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
                elif kind == "tts":
                    text, voice = data
                    rendered = self._tts.synthesize(text, voice=voice)
                    if rendered:
                        pcm, sr = rendered
                        self._play_pcm_blocking(pcm, sr, lead_ms=420, tail_ms=300, target_peak=0.84, kind="tts")
            except Exception as exc:
                self.log.debug("Speaker worker error: %s", exc)

    def _prewarm_tts_worker(self) -> None:
        voice = self.cfg.tts_voice
        if voice == "sapi":
            return
        for phrase in PREWARM_TTS_PHRASES:
            if self._stop_event.is_set():
                return
            if self._tts.get_cached(phrase, voice) is not None:
                continue
            try:
                self._tts.synthesize(phrase, voice=voice)
            except Exception:
                pass
            if self._stop_event.wait(0.15):
                return

    def play_ready_chime(self) -> bool:
        """Play a brief 120 ms two-tone ready chime on House Audio / PowerConf S330 speaker."""
        if not self._allow_hardware_audio and self._house_audio_router is None:
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
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + 0.85)
                self._speaker_queue.put_nowait(("pcm", (tone, sr, 220, 150, 0.28)))
                return True
            except queue.Full:
                return False
        return self._play_pcm_blocking(tone, sr, lead_ms=220, tail_ms=150, target_peak=0.28, kind="chime")

    def speak_confirmation(self, text: str) -> bool:
        """Synthesize natural neural confirmation speech and queue padded playback to House Audio / S330."""
        clean = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not clean:
            return False
        if self._speak_fn is not None:
            return bool(self._speak_fn(clean))
        if not self._allow_hardware_audio and self._house_audio_router is None:
            return False
        if self._resolve_speaker() is None and self._house_audio_router is None:
            return False
        voice = self.cfg.tts_voice
        if self._started:
            try:
                self._mute_until_mono = max(self._mute_until_mono, time.monotonic() + 2.4)
                self._speaker_queue.put_nowait(("tts", (clean, voice)))
                return True
            except queue.Full:
                return False
        rendered = self._tts.synthesize(clean, voice=voice)
        if not rendered:
            return False
        pcm, sr = rendered
        return self._play_pcm_blocking(pcm, sr, lead_ms=420, tail_ms=300, target_peak=0.84, kind="tts")

    # ---------- capture & ASR loops ----------

    def _open_stream(self) -> bool:
        with self._audio_lock:
            if self._stream is not None:
                return True
            with _PORTAUDIO_LOCK:
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
                utterances = self._detector.push_samples(pcm16, muted=muted)
                for utt in utterances:
                    try:
                        self._utt_queue.put_nowait(utt)
                    except queue.Full:
                        try:
                            _ = self._utt_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._utt_queue.put_nowait(utt)
                        except queue.Full:
                            pass

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
                    self._detector.reset()
                    with self.lock:
                        self.listening = True
                        self.last_error = None
                        self._bump_status_locked()
                    self.log.info("Room mic open: %s [%s @ %d Hz, %d ch]", dev["name"], dev["hostApi"], sr, ch)
                    return True
                except Exception as exc:
                    last_err = str(exc)

            with self.lock:
                self.listening = False
                self.last_error = last_err or f"Could not open {dev['name']}"
                self._bump_status_locked()
            return False

    def _close_stream(self) -> None:
        with self._audio_lock:
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
        with self.lock:
            if self.listening:
                self.listening = False
                self._bump_status_locked()

    def _capture_supervisor_loop(self) -> None:
        last_wasapi_probe = time.monotonic()
        while not self._stop_event.is_set():
            want = self.should_capture_now()
            if want:
                if self._stream is None:
                    if not self._open_stream():
                        self._stop_event.wait(2.5)
                        continue
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
                    # If on WDM-KS fallback (RDP) and currently idle, periodically check if WASAPI returned
                    if (
                        self._stream_host_api == "Windows WDM-KS"
                        and not self._detector._in_speech
                        and (time.monotonic() - last_wasapi_probe) >= 30.0
                    ):
                        last_wasapi_probe = time.monotonic()
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
            return str(fn(audio16, prompt, hotwords) or "").strip()
        if model is None:
            return ""
        segs, _info = model.transcribe(
            audio16,
            language="en",
            beam_size=2,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            no_speech_threshold=0.65,
            log_prob_threshold=-1.0,
            initial_prompt=prompt,
            hotwords=hotwords,
        )
        return " ".join(s.text.strip() for s in segs).strip()

    def _asr_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                audio16 = self._utt_queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                break
            t0 = time.perf_counter()
            try:
                text = self._transcribe_utterance(audio16)
            except Exception as exc:
                self.log.warning("ASR transcription error: %s", exc)
                continue
            dt_ms = int(round((time.perf_counter() - t0) * 1000.0))
            if not text or is_hallucination(text):
                continue
            self._emit_transcript(text, latency_ms=dt_ms, force_emit=False)
