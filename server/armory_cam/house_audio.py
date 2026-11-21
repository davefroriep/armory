"""House Audio routing & AVR control for The Armory (Denon AVR-3313CI + Fire TV Cube mixer APK).

Provides:
  - `HouseAudioConfig`: persistent settings for Denon loud-only volume ducking & Fire TV Cube mixing.
  - `DenonAvrClient`: read-only status XML parser + threshold-gated smooth `MV` ramp duck/restore.
  - `FireTvCubeAudioClient`: HTTP `:8767` playback/health client + ADB probe & foreground-service revival.
  - `AudioMatrixStore`: persistent app-by-mode test compatibility grid (`Spotify`, `YouTube`, `Netflix`, etc.).
  - `HouseAudioManager`: routes `RoomVoiceService` chimes/readbacks to Fire TV Cube or PowerConf S330.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterator
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET

import numpy as np


VALID_OUTPUT_ROUTES = ("s330", "cube", "auto")
VALID_MIX_MODES = ("overlay", "duck", "takeover")
VALID_CUBE_USAGES = ("media", "assist", "sonification", "alarm")
VALID_SFX_EXTS = (".wav", ".mp3", ".ogg", ".m4a")
MATRIX_APPS = (
    "Spotify",
    "YouTube",
    "Netflix",
    "Prime Video",
    "Amazon / Apple Music",
    "Live TV / Sports",
)
MATRIX_MODES = ("overlay", "duck", "takeover")
VALID_MATRIX_STATUSES = ("untested", "pass", "warn", "fail")


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class HouseAudioConfig:
    """Configuration for Denon AVR-3313CI loud-only ducking and Fire TV Cube house-speaker mixing."""

    denon_enabled: bool = True
    denon_host: str = "192.168.1.15"
    denon_port: int = 80
    denon_duck_enabled: bool = False  # Strictly opt-in; never ducks music unless loud & enabled
    denon_duck_threshold_db: float = -20.0
    denon_duck_drop_db: float = 5.0
    denon_duck_floor_db: float = -35.0
    denon_ramp_steps: int = 3
    denon_ramp_step_ms: int = 70

    cube_enabled: bool = False
    cube_host: str = ""
    cube_port: int = 8767
    cube_adb_port: int = 5555
    output_route: str = "s330"  # "s330" | "cube" | "auto"
    readback_mode: str = "overlay"
    chime_mode: str = "overlay"
    hype_mode: str = "takeover"
    cube_usage: str = "media"
    readback_gain: float = 0.85
    sfx_gain: float = 0.90

    def to_dict(self) -> dict[str, Any]:
        return {
            "denonEnabled": bool(self.denon_enabled),
            "denonHost": self.denon_host,
            "denonPort": int(self.denon_port),
            "denonDuckEnabled": bool(self.denon_duck_enabled),
            "denonDuckThresholdDb": round(float(self.denon_duck_threshold_db), 1),
            "denonDuckDropDb": round(float(self.denon_duck_drop_db), 1),
            "denonDuckFloorDb": round(float(self.denon_duck_floor_db), 1),
            "denonRampSteps": int(self.denon_ramp_steps),
            "denonRampStepMs": int(self.denon_ramp_step_ms),
            "cubeEnabled": bool(self.cube_enabled),
            "cubeHost": self.cube_host,
            "cubePort": int(self.cube_port),
            "cubeAdbPort": int(self.cube_adb_port),
            "outputRoute": self.output_route,
            "readbackMode": self.readback_mode,
            "chimeMode": self.chime_mode,
            "hypeMode": self.hype_mode,
            "cubeUsage": self.cube_usage,
            "readbackGain": round(float(self.readback_gain), 2),
            "sfxGain": round(float(self.sfx_gain), 2),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "HouseAudioConfig":
        if not isinstance(raw, dict):
            return cls()

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

        def _choice(key: str, allowed: tuple[str, ...], default: str) -> str:
            val = str(raw.get(key, default) or default).strip().lower()
            return val if val in allowed else default

        return cls(
            denon_enabled=bool(raw.get("denonEnabled", True)),
            denon_host=str(raw.get("denonHost", "192.168.1.15") or "").strip(),
            denon_port=max(1, min(65535, _i("denonPort", 80))),
            denon_duck_enabled=bool(raw.get("denonDuckEnabled", False)),
            denon_duck_threshold_db=_clamp(_f("denonDuckThresholdDb", -20.0), -65.0, 10.0),
            denon_duck_drop_db=_clamp(_f("denonDuckDropDb", 5.0), 0.5, 30.0),
            denon_duck_floor_db=_clamp(_f("denonDuckFloorDb", -35.0), -75.0, 0.0),
            denon_ramp_steps=max(1, min(10, _i("denonRampSteps", 3))),
            denon_ramp_step_ms=max(20, min(300, _i("denonRampStepMs", 70))),
            cube_enabled=bool(raw.get("cubeEnabled", False)),
            cube_host=str(raw.get("cubeHost", "") or "").strip(),
            cube_port=max(1, min(65535, _i("cubePort", 8767))),
            cube_adb_port=max(1, min(65535, _i("cubeAdbPort", 5555))),
            output_route=_choice("outputRoute", VALID_OUTPUT_ROUTES, "s330"),
            readback_mode=_choice("readbackMode", VALID_MIX_MODES, "overlay"),
            chime_mode=_choice("chimeMode", VALID_MIX_MODES, "overlay"),
            hype_mode=_choice("hypeMode", VALID_MIX_MODES, "takeover"),
            cube_usage=_choice("cubeUsage", VALID_CUBE_USAGES, "media"),
            readback_gain=_clamp(_f("readbackGain", 0.85), 0.05, 1.0),
            sfx_gain=_clamp(_f("sfxGain", 0.90), 0.05, 1.0),
        )


def db_to_denon_mv(db: float) -> str:
    """Convert dB (-80.0 .. +18.0) to Denon AVR `MV<XX>` / `MV<XX5>` command string."""
    abs_half = max(0, min(196, int(round((float(db) + 80.0) * 2.0))))
    whole = abs_half // 2
    if abs_half % 2 == 0:
        return f"MV{whole:02d}"
    return f"MV{whole:02d}5"


def parse_denon_status_xml(xml_text: str) -> dict[str, Any]:
    """Parse Denon AVR-3313CI XML status response into clean normalized fields."""
    text = (xml_text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty XML response"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"ok": False, "error": f"XML parse error: {exc}"}

    def _val(tag: str) -> str:
        el = root.find(f"./{tag}/value")
        if el is None or el.text is None:
            return ""
        return el.text.strip()

    power = _val("Power") or _val("ZonePower")
    inp = _val("InputFuncSelect")
    mv_raw = _val("MasterVolume")
    mute_raw = _val("Mute")
    surr = _val("SurrMode") or _val("selectSurround")

    vol_db: float | None = None
    vol_abs: float | None = None
    if mv_raw and mv_raw != "--":
        try:
            vol_db = round(float(mv_raw), 1)
            vol_abs = round(80.0 + vol_db, 1)
        except ValueError:
            vol_db = None
            vol_abs = None

    if not power and not inp:
        return {"ok": False, "error": "Unexpected Denon XML payload"}

    return {
        "ok": True,
        "power": power.upper(),
        "input": inp,
        "volumeDb": vol_db,
        "volumeAbs": vol_abs,
        "muted": mute_raw.lower() == "on",
        "surround": re.sub(r"\s+", " ", surr).strip(),
    }


def _default_http_get(url: str, timeout: float = 1.2) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


def _default_http_post(
    url: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


class DenonAvrClient:
    """Thread-safe Denon AVR-3313CI HTTP status reader and loud-only smooth volume ducker."""

    def __init__(
        self,
        get_cfg: Callable[[], HouseAudioConfig],
        *,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        allow_network: bool = True,
    ) -> None:
        self._get_cfg = get_cfg
        self._http_get = http_get
        self._sleep = sleep_fn or time.sleep
        self._allow_network = bool(allow_network)
        self._lock = threading.RLock()
        self._duck_depth = 0
        self._saved_db: float | None = None
        self._ducked_db: float | None = None
        self.last_status: dict[str, Any] = {"ok": False, "error": "Not polled yet"}
        self.last_duck_info: dict[str, Any] = {}

    def _base_url(self, cfg: HouseAudioConfig) -> str:
        host = (cfg.denon_host or "").strip()
        if not host:
            return ""
        if ":" in host and not host.startswith("["):
            return f"http://{host}"
        return f"http://{host}:{int(cfg.denon_port)}"

    def _do_get(self, url: str, timeout: float = 1.2) -> tuple[int, bytes]:
        if self._http_get is not None:
            return self._http_get(url, timeout)
        if not self._allow_network:
            raise RuntimeError("Network I/O disabled in offline mode")
        return _default_http_get(url, timeout)

    def get_status(self) -> dict[str, Any]:
        cfg = self._get_cfg()
        if not cfg.denon_enabled:
            res = {"ok": False, "disabled": True, "error": "Denon control disabled"}
            self.last_status = res
            return res
        base = self._base_url(cfg)
        if not base:
            res = {"ok": False, "error": "Denon IP not configured"}
            self.last_status = res
            return res
        try:
            code, raw = self._do_get(f"{base}/goform/formMainZone_MainZoneXmlStatus.xml", 1.2)
            if code >= 400:
                res = {"ok": False, "error": f"HTTP {code}"}
            else:
                res = parse_denon_status_xml(raw.decode("utf-8", errors="ignore"))
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        self.last_status = res
        return res

    def send_command(self, cmd: str) -> dict[str, Any]:
        cfg = self._get_cfg()
        base = self._base_url(cfg)
        if not cfg.denon_enabled or not base:
            return {"ok": False, "error": "Denon disabled or unconfigured"}
        clean = str(cmd or "").strip()
        if not clean:
            return {"ok": False, "error": "Empty command"}
        encoded = urllib.parse.quote(clean, safe="")
        try:
            code, _ = self._do_get(f"{base}/goform/formiPhoneAppDirect.xml?{encoded}", 1.2)
            return {"ok": code < 400, "command": clean, "status": code}
        except Exception as exc:
            return {"ok": False, "command": clean, "error": str(exc)}

    def _ramp_volume(self, start_db: float, end_db: float, steps: int, step_ms: int) -> bool:
        n_steps = max(1, int(steps))
        delay_sec = max(0.0, float(step_ms) / 1000.0)
        sent_cmds: list[str] = []
        last_cmd = ""
        for i in range(1, n_steps + 1):
            frac = float(i) / float(n_steps)
            step_db = start_db + (end_db - start_db) * frac
            cmd = db_to_denon_mv(step_db)
            if cmd != last_cmd:
                res = self.send_command(cmd)
                if not res.get("ok"):
                    return bool(sent_cmds)
                sent_cmds.append(cmd)
                last_cmd = cmd
            if i < n_steps and delay_sec > 0.0:
                self._sleep(delay_sec)
        return bool(sent_cmds)

    @contextmanager
    def duck_context(self, *, drop_db: float | None = None, force: bool = False) -> Iterator[dict[str, Any]]:
        """Threshold-gated smooth Denon volume dip during voice/alert playback, restoring on exit."""
        participating = False
        info: dict[str, Any] = {"ducked": False, "reason": "skipped"}
        with self._lock:
            cfg = self._get_cfg()
            if self._duck_depth > 0:
                self._duck_depth += 1
                participating = True
                info = {"ducked": True, "nested": True, "fromDb": self._saved_db, "toDb": self._ducked_db}
            elif cfg.denon_enabled and self._base_url(cfg) and (force or cfg.denon_duck_enabled):
                st = self.get_status()
                cur = st.get("volumeDb")
                if not st.get("ok"):
                    info = {"ducked": False, "reason": st.get("error", "unreachable")}
                elif st.get("power") != "ON":
                    info = {"ducked": False, "reason": "standby"}
                elif st.get("muted"):
                    info = {"ducked": False, "reason": "muted"}
                elif cur is None:
                    info = {"ducked": False, "reason": "volume_unknown"}
                elif not force and float(cur) < cfg.denon_duck_threshold_db:
                    info = {
                        "ducked": False,
                        "reason": "below_threshold",
                        "volumeDb": float(cur),
                        "thresholdDb": cfg.denon_duck_threshold_db,
                    }
                else:
                    current_db = float(cur)
                    drop = max(0.5, float(drop_db if drop_db is not None else cfg.denon_duck_drop_db))
                    target_db = round(max(cfg.denon_duck_floor_db, current_db - drop) * 2.0) / 2.0
                    if target_db >= current_db - 0.4:
                        info = {"ducked": False, "reason": "at_floor", "volumeDb": current_db}
                    elif self._ramp_volume(current_db, target_db, cfg.denon_ramp_steps, cfg.denon_ramp_step_ms):
                        self._saved_db = current_db
                        self._ducked_db = target_db
                        self._duck_depth = 1
                        participating = True
                        info = {"ducked": True, "fromDb": current_db, "toDb": target_db}
                        self.last_duck_info = {**info, "at": _utc_iso_now()}
                    else:
                        info = {"ducked": False, "reason": "command_failed"}
            elif not force and not cfg.denon_duck_enabled:
                info = {"ducked": False, "reason": "disabled"}

        try:
            yield info
        finally:
            if participating:
                with self._lock:
                    cfg_now = self._get_cfg()
                    self._duck_depth = max(0, self._duck_depth - 1)
                    if self._duck_depth == 0:
                        saved = self._saved_db
                        ducked = self._ducked_db
                        self._saved_db = None
                        self._ducked_db = None
                        if saved is not None and ducked is not None:
                            st_now = self.get_status()
                            now_db = st_now.get("volumeDb") if st_now.get("ok") else None
                            if st_now.get("power") == "ON" and now_db is not None:
                                if abs(float(now_db) - ducked) <= 1.0:
                                    self._ramp_volume(
                                        float(now_db),
                                        saved,
                                        cfg_now.denon_ramp_steps,
                                        cfg_now.denon_ramp_step_ms,
                                    )
                                    self.last_duck_info = {
                                        "ducked": True,
                                        "restoredToDb": saved,
                                        "at": _utc_iso_now(),
                                    }
                                else:
                                    self.last_duck_info = {
                                        "ducked": True,
                                        "restoreSkipped": "manual_volume_change",
                                        "nowDb": float(now_db),
                                        "expectedDuckedDb": ducked,
                                        "at": _utc_iso_now(),
                                    }


def encode_mono_pcm_to_wav(mono_pcm: np.ndarray, sr: int) -> bytes:
    """Convert mono float32 PCM (`-1..1`) into standard 16-bit PCM WAV bytes."""
    arr = np.asarray(mono_pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        arr = np.zeros(int(max(1, sr * 0.05)), dtype=np.float32)
    peak = float(np.max(np.abs(arr)))
    if peak > 0.98:
        arr = arr * (0.95 / peak)
    arr = np.clip(arr, -0.98, 0.98)
    pcm16 = np.round(arr * 32767.0).astype("<i2")
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(max(8000, int(sr)))
        wf.writeframes(pcm16.tobytes())
    return bio.getvalue()


def _tone_segment(freqs_hz: list[float], dur_sec: float, sr: int = 48_000, amp: float = 0.72) -> np.ndarray:
    n = max(1, int(round(dur_sec * sr)))
    t = np.linspace(0.0, dur_sec, n, endpoint=False, dtype=np.float32)
    sig = np.zeros(n, dtype=np.float32)
    for idx, f in enumerate(freqs_hz):
        weight = 1.0 / (1.0 + 0.35 * idx)
        sig += weight * (
            0.72 * np.sin(2.0 * np.pi * f * t)
            + 0.22 * np.sin(2.0 * np.pi * (f * 2.0) * t)
            + 0.08 * np.sin(2.0 * np.pi * (f * 3.0) * t)
        )
    atk = min(n // 4, max(4, int(round(0.012 * sr))))
    rel = min(n // 2, max(4, int(round(0.045 * sr))))
    env = np.ones(n, dtype=np.float32)
    env[:atk] = np.linspace(0.0, 1.0, atk, dtype=np.float32)
    env[-rel:] = np.linspace(1.0, 0.0, rel, dtype=np.float32)
    peak = float(np.max(np.abs(sig)))
    if peak > 1e-6:
        sig = sig * (amp / peak)
    return (sig * env).astype(np.float32)


def synth_ready_chime(sr: int = 48_000) -> bytes:
    """Two-tone crisp ready chime WAV."""
    a = _tone_segment([880.0], 0.065, sr=sr, amp=0.65)
    b = _tone_segment([1174.66], 0.095, sr=sr, amp=0.72)
    return encode_mono_pcm_to_wav(np.concatenate([a, b]), sr)


def synth_rest_done(sr: int = 48_000) -> bytes:
    """Three-note ascending rest-timer complete cue WAV."""
    gap = np.zeros(int(sr * 0.035), dtype=np.float32)
    p1 = _tone_segment([880.0], 0.11, sr=sr, amp=0.70)
    p2 = _tone_segment([1108.73], 0.11, sr=sr, amp=0.72)
    p3 = _tone_segment([1318.51], 0.22, sr=sr, amp=0.78)
    return encode_mono_pcm_to_wav(np.concatenate([p1, gap, p2, gap, p3]), sr)


def synth_pr_fanfare(sr: int = 48_000) -> bytes:
    """Offline fallback cue: dark 1.4s cinematic sub-bass impact drop (no MIDI fanfare triads)."""
    n = max(1, int(round(1.4 * sr)))
    t = np.linspace(0.0, 1.4, n, endpoint=False, dtype=np.float32)
    freq = 32.0 + 48.0 * np.exp(-t / 0.16)
    phase = 2.0 * np.pi * np.cumsum(freq) / float(sr)
    sub = np.sin(phase) * np.exp(-t / 0.52)
    transient = np.sin(2.0 * np.pi * 140.0 * t) * np.exp(-t / 0.035) * 0.45
    pcm = np.tanh(1.35 * (sub + transient)).astype(np.float32)
    rel = min(n // 2, int(0.18 * sr))
    pcm[-rel:] *= np.linspace(1.0, 0.0, rel, dtype=np.float32)
    return encode_mono_pcm_to_wav(pcm, sr)


def validate_sfx_filename(name: str) -> str | None:
    """Validate custom SFX basename safely (no path traversal)."""
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    p = Path(raw)
    if p.name != raw or p.suffix.lower() not in VALID_SFX_EXTS:
        return None
    return raw


class FireTvCubeAudioClient:
    """HTTP :8767 playback client & ADB audit/revive helper for the Fire TV Cube mixer APK."""

    def __init__(
        self,
        *,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        allow_network: bool = True,
    ) -> None:
        self._http_get = http_get
        self._http_post = http_post
        self._sleep = sleep_fn or time.sleep
        self._allow_network = bool(allow_network)
        self.last_health: dict[str, Any] = {"ok": False, "error": "Not polled yet"}
        self.last_adb_probe: dict[str, Any] = {}

    def _do_get(self, url: str, timeout: float = 0.9) -> tuple[int, bytes]:
        if self._http_get is not None:
            return self._http_get(url, timeout)
        if not self._allow_network:
            raise RuntimeError("Network I/O disabled in offline mode")
        return _default_http_get(url, timeout)

    def _do_post(
        self,
        url: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float = 3.0,
    ) -> tuple[int, bytes]:
        if self._http_post is not None:
            return self._http_post(url, body, headers, timeout)
        if not self._allow_network:
            raise RuntimeError("Network I/O disabled in offline mode")
        return _default_http_post(url, body, headers, timeout)

    @staticmethod
    def _base_url(host: str, port: int) -> str:
        clean = str(host or "").strip()
        if not clean:
            return ""
        if ":" in clean and not clean.startswith("["):
            clean = clean.split(":", 1)[0].strip()
        return f"http://{clean}:{int(port)}"

    @staticmethod
    def _adb_serial(host: str, adb_port: int) -> str:
        clean = str(host or "").strip()
        if not clean:
            return ""
        if ":" in clean:
            return clean
        return f"{clean}:{int(adb_port)}"

    def health(self, host: str, port: int = 8767) -> dict[str, Any]:
        base = self._base_url(host, port)
        if not base:
            res = {"ok": False, "unconfigured": True, "error": "Cube IP not configured"}
            self.last_health = res
            return res
        try:
            code, raw = self._do_get(f"{base}/health", 0.9)
            if code >= 400:
                res = {"ok": False, "error": f"HTTP {code}"}
            else:
                parsed = json.loads(raw.decode("utf-8", errors="ignore"))
                if isinstance(parsed, dict):
                    res = {"ok": bool(parsed.get("ok", True)), **parsed}
                else:
                    res = {"ok": False, "error": "Invalid JSON health payload"}
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        self.last_health = res
        return res

    def play_audio_bytes(
        self,
        host: str,
        port: int,
        audio_bytes: bytes,
        *,
        mode: str = "overlay",
        usage: str = "media",
        gain: float = 0.85,
        max_sec: int = 20,
        sync: bool = False,
        content_type: str = "audio/wav",
    ) -> dict[str, Any]:
        base = self._base_url(host, port)
        if not base:
            return {"ok": False, "error": "Cube IP not configured"}
        qs = urllib.parse.urlencode({
            "mode": mode if mode in VALID_MIX_MODES else "overlay",
            "usage": usage if usage in VALID_CUBE_USAGES else "media",
            "gain": f"{_clamp(float(gain), 0.05, 1.0):.2f}",
            "maxSec": str(max(1, min(60, int(max_sec)))),
            "sync": "1" if sync else "0",
        })
        timeout = max(3.5, float(max_sec) + 3.0) if sync else 3.0
        try:
            code, raw = self._do_post(
                f"{base}/play?{qs}",
                audio_bytes,
                {"Content-Type": content_type},
                timeout,
            )
            parsed = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            if code >= 400:
                return {"ok": False, "status": code, **parsed}
            return {"ok": bool(parsed.get("ok", True)), **parsed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_wav_bytes(
        self,
        host: str,
        port: int,
        wav_bytes: bytes,
        *,
        mode: str = "overlay",
        usage: str = "media",
        gain: float = 0.85,
        max_sec: int = 20,
        sync: bool = False,
    ) -> dict[str, Any]:
        return self.play_audio_bytes(
            host,
            port,
            wav_bytes,
            mode=mode,
            usage=usage,
            gain=gain,
            max_sec=max_sec,
            sync=sync,
            content_type="audio/wav",
        )

    def play_url(
        self,
        host: str,
        port: int,
        url: str,
        *,
        mode: str = "takeover",
        usage: str = "media",
        gain: float = 0.90,
        max_sec: int = 30,
        sync: bool = False,
    ) -> dict[str, Any]:
        base = self._base_url(host, port)
        if not base:
            return {"ok": False, "error": "Cube IP not configured"}
        qs = urllib.parse.urlencode({
            "mode": mode if mode in VALID_MIX_MODES else "takeover",
            "usage": usage if usage in VALID_CUBE_USAGES else "media",
            "gain": f"{_clamp(float(gain), 0.05, 1.0):.2f}",
            "maxSec": str(max(1, min(60, int(max_sec)))),
            "sync": "1" if sync else "0",
            "url": str(url or "").strip(),
        })
        timeout = max(4.0, float(max_sec) + 4.0) if sync else 3.5
        try:
            code, raw = self._do_post(f"{base}/play?{qs}", b"", {}, timeout)
            parsed = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            return {"ok": code < 400 and bool(parsed.get("ok", True)), **parsed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop(self, host: str, port: int = 8767) -> dict[str, Any]:
        base = self._base_url(host, port)
        if not base:
            return {"ok": False, "error": "Cube IP not configured"}
        try:
            code, raw = self._do_post(f"{base}/stop", b"", {}, 2.0)
            parsed = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            return {"ok": code < 400 and bool(parsed.get("ok", True)), **parsed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def adb_probe_and_audit(
        self,
        adb_controller: Any,
        host: str,
        adb_port: int = 5555,
        cube_port: int = 8767,
    ) -> dict[str, Any]:
        serial = self._adb_serial(host, adb_port)
        if not serial:
            return {"ok": False, "state": "unconfigured", "message": "Enter the Fire TV Cube IP address first."}
        if adb_controller is None:
            return {"ok": False, "state": "error", "serial": serial, "message": "ADB controller unavailable."}

        st, detail = adb_controller._connect_and_status(serial)
        if st != "device":
            msg = (
                f"Accept the 'Allow USB debugging' prompt on the Fire TV Cube ({serial})."
                if st == "unauthorized"
                else f"Cube ADB offline at {serial}: {detail or 'Enable ADB Debugging on the Cube'}"
            )
            res = {"ok": False, "state": st, "serial": serial, "message": msg, "health": self.health(host, cube_port)}
            self.last_adb_probe = res
            return res

        def _sh(*args: str, timeout: float = 4.0) -> str:
            rc, out, _ = adb_controller._run(["-s", serial, "shell", *args], timeout=timeout)
            return out.strip() if rc == 0 else ""

        model = (_sh("getprop", "ro.product.model").splitlines() or [""])[0].strip()
        android_ver = (_sh("getprop", "ro.build.version.release").splitlines() or [""])[0].strip()
        surr_out = _sh("settings", "get", "global", "encoded_surround_output")
        surr_mode = _sh("settings", "get", "global", "surround_sound_mode")
        apk_path = _sh("pm", "path", "com.froriep.armoryaudio")
        pkgs_raw = _sh("pm", "list", "packages", "-3", timeout=6.0)
        third_party = sorted(
            line[len("package:") :].strip()
            for line in pkgs_raw.splitlines()
            if line.strip().startswith("package:")
        )
        health_res = self.health(host, cube_port)
        res = {
            "ok": True,
            "state": "device",
            "serial": serial,
            "model": model,
            "androidVersion": android_ver,
            "surroundOutput": surr_out,
            "surroundMode": surr_mode,
            "apkInstalled": bool(apk_path),
            "apkPath": apk_path,
            "thirdPartyPackages": third_party,
            "health": health_res,
            "message": f"Connected to {model or serial} (Android {android_ver or '?'}) · APK {'installed' if apk_path else 'not installed yet'}",
        }
        self.last_adb_probe = res
        return res

    def adb_ensure_service(
        self,
        adb_controller: Any,
        host: str,
        adb_port: int = 5555,
        cube_port: int = 8767,
    ) -> dict[str, Any]:
        serial = self._adb_serial(host, adb_port)
        if not serial:
            return {"ok": False, "state": "unconfigured", "message": "Enter the Fire TV Cube IP address first."}
        if adb_controller is None:
            return {"ok": False, "state": "error", "serial": serial, "message": "ADB controller unavailable."}

        st, detail = adb_controller._connect_and_status(serial)
        if st != "device":
            return {"ok": False, "state": st, "serial": serial, "message": f"ADB not ready ({st}): {detail}"}

        adb_controller._run(
            ["-s", serial, "shell", "dumpsys", "deviceidle", "whitelist", "+com.froriep.armoryaudio"],
            timeout=5.0,
        )
        rc, out, err = adb_controller._run(
            ["-s", serial, "shell", "am", "start-foreground-service", "-n", "com.froriep.armoryaudio/.ArmoryAudioService"],
            timeout=5.0,
        )
        if rc != 0:
            adb_controller._run(
                ["-s", serial, "shell", "am", "startservice", "-n", "com.froriep.armoryaudio/.ArmoryAudioService"],
                timeout=5.0,
            )

        health_res: dict[str, Any] = {"ok": False}
        for _ in range(3):
            health_res = self.health(host, cube_port)
            if health_res.get("ok"):
                break
            self._sleep(0.4)

        return {
            "ok": bool(health_res.get("ok")),
            "state": "online" if health_res.get("ok") else "started",
            "serial": serial,
            "health": health_res,
            "message": (
                f"Cube service online on :{cube_port}"
                if health_res.get("ok")
                else f"Start sent ({(out or err).strip() or 'ok'}), waiting for :{cube_port}/health"
            ),
        }


class AudioMatrixStore:
    """Persistent Gym App × Mix Mode compatibility matrix (`audio_matrix.json`)."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.path = (root_dir / "audio_matrix.json") if root_dir is not None else None
        self._lock = threading.RLock()
        self._cells: dict[str, dict[str, dict[str, str]]] = {}
        self._init_defaults()
        self._load()

    def _init_defaults(self) -> None:
        self._cells = {
            app: {
                mode: {"status": "untested", "behavior": "", "notes": "", "updatedAt": ""}
                for mode in MATRIX_MODES
            }
            for app in MATRIX_APPS
        }

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            cells = raw.get("cells") if isinstance(raw, dict) else None
            if not isinstance(cells, dict):
                return
            for app, row in cells.items():
                if not isinstance(row, dict):
                    continue
                dst_row = self._cells.setdefault(str(app), {
                    m: {"status": "untested", "behavior": "", "notes": "", "updatedAt": ""}
                    for m in MATRIX_MODES
                })
                for mode, entry in row.items():
                    if mode in MATRIX_MODES and isinstance(entry, dict):
                        st = str(entry.get("status", "untested")).lower()
                        dst_row[mode] = {
                            "status": st if st in VALID_MATRIX_STATUSES else "untested",
                            "behavior": str(entry.get("behavior", "") or ""),
                            "notes": str(entry.get("notes", "") or ""),
                            "updatedAt": str(entry.get("updatedAt", "") or ""),
                        }
        except Exception:
            pass

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.snapshot_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def record(
        self,
        app: str,
        mode: str,
        *,
        status: str,
        behavior: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        app_clean = str(app or "").strip()
        mode_clean = str(mode or "").strip().lower()
        st_clean = str(status or "untested").strip().lower()
        if not app_clean or mode_clean not in MATRIX_MODES:
            raise ValueError("Invalid app or mode")
        if st_clean not in VALID_MATRIX_STATUSES:
            st_clean = "untested"
        with self._lock:
            row = self._cells.setdefault(
                app_clean,
                {m: {"status": "untested", "behavior": "", "notes": "", "updatedAt": ""} for m in MATRIX_MODES},
            )
            row[mode_clean] = {
                "status": st_clean,
                "behavior": str(behavior or "").strip(),
                "notes": str(notes or "").strip(),
                "updatedAt": _utc_iso_now() if st_clean != "untested" or behavior or notes else "",
            }
            self._save_locked()
            return self.snapshot_dict()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._init_defaults()
            self._save_locked()
            return self.snapshot_dict()

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            apps = list(dict.fromkeys([*MATRIX_APPS, *self._cells.keys()]))
            return {
                "apps": apps,
                "modes": list(MATRIX_MODES),
                "cells": {
                    app: {mode: dict(self._cells[app][mode]) for mode in MATRIX_MODES}
                    for app in apps
                },
            }


class HouseAudioManager:
    """Unified coordinator for Denon AVR-3313CI ducking, Fire TV Cube playback, and S330 fallback."""

    def __init__(
        self,
        *,
        root_dir: Path | None = None,
        config: HouseAudioConfig | None = None,
        adb_controller: Any = None,
        allow_network: bool = True,
        denon_http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        cube_http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        cube_http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.cfg = config or HouseAudioConfig()
        self.root_dir = root_dir
        self.sfx_dir = (root_dir / "sfx") if root_dir is not None else None
        if self.sfx_dir is not None:
            self.sfx_dir.mkdir(parents=True, exist_ok=True)
        self.adb = adb_controller
        self.allow_network = bool(allow_network)
        self.denon = DenonAvrClient(
            self.get_config,
            http_get=denon_http_get,
            sleep_fn=sleep_fn,
            allow_network=allow_network,
        )
        self.cube = FireTvCubeAudioClient(
            http_get=cube_http_get,
            http_post=cube_http_post,
            sleep_fn=sleep_fn,
            allow_network=allow_network,
        )
        self.matrix = AudioMatrixStore(root_dir=root_dir)

    def get_config(self) -> HouseAudioConfig:
        with self._lock:
            return self.cfg

    def update_config(self, raw: dict[str, Any]) -> HouseAudioConfig:
        with self._lock:
            merged = {**self.cfg.to_dict(), **(raw or {})}
            self.cfg = HouseAudioConfig.from_dict(merged)
            return self.cfg

    def list_sfx_files(self) -> list[dict[str, Any]]:
        if self.sfx_dir is None or not self.sfx_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for p in sorted(self.sfx_dir.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            valid = validate_sfx_filename(p.name)
            if not valid:
                continue
            items.append({"name": valid, "bytes": int(p.stat().st_size), "ext": p.suffix.lower()})
        return items

    def resolve_voice_target(self, kind: str = "tts") -> str:
        cfg = self.get_config()
        if cfg.output_route == "s330":
            return "s330"
        if not cfg.cube_enabled or not cfg.cube_host:
            return "s330"
        if cfg.output_route == "cube":
            return "cube"
        # "auto": play on Cube only when Denon is ON on Media Player AND Cube :8767/health is alive
        if cfg.denon_enabled and cfg.denon_host:
            den_st = self.denon.get_status()
            if not den_st.get("ok") or den_st.get("power") != "ON":
                return "s330"
            inp = str(den_st.get("input") or "").upper()
            if inp not in ("MPLAY", "MEDIA PLAYER"):
                return "s330"
        health = self.cube.health(cfg.cube_host, cfg.cube_port)
        return "cube" if health.get("ok") else "s330"

    def try_route_voice_pcm(self, mono_pcm: np.ndarray, sr: int, *, kind: str = "tts") -> dict[str, Any]:
        """Route RoomVoice chimes/TTS to Fire TV Cube when active, falling back cleanly to PowerConf S330."""
        cfg = self.get_config()
        target = self.resolve_voice_target(kind=kind)
        if target != "cube":
            return {"handled": False, "route": "s330", "duckContext": self.denon.duck_context}

        dur_sec = float(np.asarray(mono_pcm).size) / float(max(1, sr))
        wav_bytes = encode_mono_pcm_to_wav(mono_pcm, sr)
        if kind in ("chime", "rest_done"):
            mode = cfg.chime_mode
            gain = cfg.sfx_gain
        elif kind == "hype":
            mode = cfg.hype_mode
            gain = cfg.sfx_gain
        else:
            mode = cfg.readback_mode
            gain = cfg.readback_gain

        with self.denon.duck_context():
            res = self.cube.play_wav_bytes(
                cfg.cube_host,
                cfg.cube_port,
                wav_bytes,
                mode=mode,
                usage=cfg.cube_usage,
                gain=gain,
                max_sec=max(5, int(math.ceil(dur_sec + 3.0))),
                sync=True,
            )
        if res.get("ok"):
            return {"handled": True, "route": "cube", "mode": mode, "durationSec": dur_sec}
        return {
            "handled": False,
            "route": "s330",
            "fallback": True,
            "error": res.get("error", ""),
            "duckContext": self.denon.duck_context,
        }

    def test_play(
        self,
        *,
        kind: str = "chime",
        mode: str | None = None,
        usage: str | None = None,
        gain: float | None = None,
        target: str = "auto",
        text: str = "225 for 8, got it",
        sfx_file: str = "",
        with_denon_duck: bool = False,
        synthesize_tts_fn: Callable[[str], tuple[np.ndarray, int] | None] | None = None,
        play_s330_fn: Callable[[np.ndarray, int, str], bool] | None = None,
    ) -> dict[str, Any]:
        cfg = self.get_config()
        kind_clean = str(kind or "chime").strip().lower()
        mode_clean = str(mode or "").strip().lower()
        if mode_clean not in VALID_MIX_MODES:
            mode_clean = (
                cfg.hype_mode
                if kind_clean in ("pr_fanfare", "sfx")
                else (cfg.chime_mode if kind_clean in ("chime", "rest_done") else cfg.readback_mode)
            )
        usage_clean = str(usage or cfg.cube_usage).strip().lower()
        if usage_clean not in VALID_CUBE_USAGES:
            usage_clean = cfg.cube_usage
        gain_val = _clamp(
            float(gain if gain is not None else (cfg.readback_gain if kind_clean == "tts" else cfg.sfx_gain)),
            0.05,
            1.0,
        )

        # Build audio bytes (or WAV PCM for S330)
        payload_bytes: bytes = b""
        content_type = "audio/wav"
        pcm_pair: tuple[np.ndarray, int] | None = None

        if kind_clean == "chime":
            payload_bytes = synth_ready_chime()
        elif kind_clean == "rest_done":
            payload_bytes = synth_rest_done()
        elif kind_clean == "pr_fanfare":
            candidates: list[Path] = []
            if self.sfx_dir is not None:
                candidates.append(self.sfx_dir / "pr_bell_crowd.wav")
            if self.allow_network:
                candidates.append(Path(r"S:\ArmoryCam\sfx\pr_bell_crowd.wav"))
            bell_path = next((p for p in candidates if p.is_file() and p.stat().st_size > 1000), None)
            payload_bytes = bell_path.read_bytes() if bell_path is not None else synth_pr_fanfare()
        elif kind_clean == "tts":
            clean_txt = str(text or "225 for 8, got it").strip()
            if synthesize_tts_fn is not None:
                pcm_pair = synthesize_tts_fn(clean_txt)
            if pcm_pair is not None:
                payload_bytes = encode_mono_pcm_to_wav(pcm_pair[0], pcm_pair[1])
            else:
                payload_bytes = synth_ready_chime()
        elif kind_clean == "sfx":
            valid_name = validate_sfx_filename(sfx_file)
            if not valid_name or self.sfx_dir is None:
                return {"ok": False, "error": "Select a valid SFX file in S:\\ArmoryCam\\sfx\\"}
            fpath = self.sfx_dir / valid_name
            if not fpath.is_file():
                return {"ok": False, "error": f"Missing SFX file: {valid_name}"}
            payload_bytes = fpath.read_bytes()
            ext = fpath.suffix.lower()
            content_type = {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".m4a": "audio/mp4",
            }.get(ext, "application/octet-stream")
        else:
            return {"ok": False, "error": f"Unknown test sound kind: {kind_clean}"}

        tgt = str(target or "auto").strip().lower()
        if tgt not in ("auto", "cube", "s330"):
            tgt = "auto"
        chosen = self.resolve_voice_target(kind=kind_clean) if tgt == "auto" else tgt
        if chosen == "cube" and not cfg.cube_host:
            if tgt == "cube":
                return {"ok": False, "error": "Enter the Fire TV Cube IP first"}
            chosen = "s330"

        def _exec_play() -> dict[str, Any]:
            if chosen == "cube":
                res = self.cube.play_audio_bytes(
                    cfg.cube_host,
                    cfg.cube_port,
                    payload_bytes,
                    mode=mode_clean,
                    usage=usage_clean,
                    gain=gain_val,
                    max_sec=25,
                    sync=bool(with_denon_duck),
                    content_type=content_type,
                )
                return {**res, "route": "cube", "mode": mode_clean, "usage": usage_clean, "kind": kind_clean}

            # S330 path
            if pcm_pair is None and content_type == "audio/wav":
                try:
                    with wave.open(io.BytesIO(payload_bytes), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                        sr_w = wf.getframerate()
                        arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
                        if wf.getnchannels() > 1:
                            arr = arr.reshape(-1, wf.getnchannels()).mean(axis=1)
                        pcm_pair_local = (arr, sr_w)
                except Exception:
                    pcm_pair_local = None
            else:
                pcm_pair_local = pcm_pair

            if play_s330_fn is not None and pcm_pair_local is not None:
                ok = bool(play_s330_fn(pcm_pair_local[0], pcm_pair_local[1], kind_clean))
                return {"ok": ok, "route": "s330", "mode": "s330", "kind": kind_clean}
            return {"ok": False, "route": "s330", "error": "S330 playback unavailable for this file"}

        if with_denon_duck:
            with self.denon.duck_context(force=True) as duck_info:
                out = _exec_play()
            return {**out, "denonDuck": duck_info}
        return _exec_play()

    def status_dict(self, *, live_probe: bool = True) -> dict[str, Any]:
        cfg = self.get_config()
        denon_st = self.denon.get_status() if live_probe else self.denon.last_status
        cube_st = (
            self.cube.health(cfg.cube_host, cfg.cube_port)
            if (live_probe and cfg.cube_host)
            else (
                {"ok": False, "unconfigured": True, "error": "Cube IP not configured"}
                if not cfg.cube_host
                else self.cube.last_health
            )
        )
        return {
            "config": cfg.to_dict(),
            "denon": denon_st,
            "cube": cube_st,
            "activeRoute": self.resolve_voice_target(),
            "sfxFiles": self.list_sfx_files(),
            "matrix": self.matrix.snapshot_dict(),
            "lastDuck": self.denon.last_duck_info,
            "lastAdbProbe": self.cube.last_adb_probe,
        }
