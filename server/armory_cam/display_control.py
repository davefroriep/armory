"""Unified discrete IP & voice controller for The Armory gym displays, Denon AVR, and Cube YouTube Music.

Controls:
  - TV / Main TV (Hisense 43E6QF Fire TV, ADB + discrete KEYCODE_WAKEUP/SLEEP + HDMI intents)
  - Wallboard (32" Fire TV Edition, delegated to FireTvAdbController)
  - SmartSpin TV (TCL Roku TV, discrete ECP PowerOn/PowerOff)
  - Denon AVR-3313CI (discrete PWON / PWSTANDBY / Multi-Ch Stereo)
  - Fire TV Cube (YouTube Music launch on Open Gym + media pause on Close Gym)
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import html as html_lib
import json
from pathlib import Path
import re
import shlex
import socket
import threading
import time
from typing import Any, Callable, Sequence
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


DEFAULT_PRIMARY_HOST = "192.168.1.230"
DEFAULT_PRIMARY_MAC = "bc:09:b9:e7:35:26"
DEFAULT_HDMI_OPTIPLEX_INPUT = "KEYCODE_TV_INPUT_HDMI_1"
DEFAULT_HDMI_DENON_INPUT = "KEYCODE_TV_INPUT_HDMI_2"
DEFAULT_CUBE_HOST = "192.168.1.151"
DEFAULT_OPEN_GYM_MUSIC_QUERY = "workout music mix"


@dataclass
class PrimaryDisplayConfig:
    """Configuration for Main TV / Armory Primary Display (Hisense 43E6QF Fire TV)."""

    enabled: bool = True
    host: str = DEFAULT_PRIMARY_HOST
    mac: str = DEFAULT_PRIMARY_MAC
    adb_port: int = 5555
    hdmi_optiplex_input: str = DEFAULT_HDMI_OPTIPLEX_INPUT
    hdmi_denon_input: str = DEFAULT_HDMI_DENON_INPUT

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "host": self.host,
            "mac": self.mac,
            "adbPort": int(self.adb_port),
            "hdmiOptiplexInput": self.hdmi_optiplex_input,
            "hdmiDenonInput": self.hdmi_denon_input,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PrimaryDisplayConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            adb_port = max(1, min(65535, int(raw.get("adbPort", 5555))))
        except (TypeError, ValueError):
            adb_port = 5555
        return cls(
            enabled=bool(raw.get("enabled", True)),
            host=str(raw.get("host", DEFAULT_PRIMARY_HOST) or "").strip(),
            mac=str(raw.get("mac", DEFAULT_PRIMARY_MAC) or "").strip(),
            adb_port=adb_port,
            hdmi_optiplex_input=str(raw.get("hdmiOptiplexInput", DEFAULT_HDMI_OPTIPLEX_INPUT) or "").strip(),
            hdmi_denon_input=str(raw.get("hdmiDenonInput", DEFAULT_HDMI_DENON_INPUT) or "").strip(),
        )


@dataclass
class SpinDisplayConfig:
    """Configuration for SmartSpin TV (TCL Roku TV)."""

    enabled: bool = True
    host: str = ""
    mac: str = ""
    ecp_port: int = 8060
    hdmi_input: str = "InputHDMI1"
    auto_denon_dual: bool = True
    primary_denon_monitor: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "host": self.host,
            "mac": self.mac,
            "ecpPort": int(self.ecp_port),
            "hdmiInput": self.hdmi_input,
            "autoDenonDual": bool(self.auto_denon_dual),
            "primaryDenonMonitor": self.primary_denon_monitor,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SpinDisplayConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            ecp_port = max(1, min(65535, int(raw.get("ecpPort", 8060))))
        except (TypeError, ValueError):
            ecp_port = 8060
        mon = str(raw.get("primaryDenonMonitor", "1") or "1").strip()
        return cls(
            enabled=bool(raw.get("enabled", True)),
            host=str(raw.get("host", "") or "").strip(),
            mac=str(raw.get("mac", "") or "").strip(),
            ecp_port=ecp_port,
            hdmi_input=str(raw.get("hdmiInput", "InputHDMI1") or "InputHDMI1").strip(),
            auto_denon_dual=bool(raw.get("autoDenonDual", True)),
            primary_denon_monitor=mon if mon in ("1", "2") else "1",
        )


@dataclass
class GymDisplaysConfig:
    """Persistent configuration for Main TV, SmartSpin TV, and Open/Close Gym macro settings."""

    primary: PrimaryDisplayConfig = field(default_factory=PrimaryDisplayConfig)
    spin: SpinDisplayConfig = field(default_factory=SpinDisplayConfig)
    cube_host: str = DEFAULT_CUBE_HOST
    cube_adb_port: int = 5555
    open_gym_play_music: bool = True
    open_gym_music_url: str = ""
    open_gym_music_query: str = DEFAULT_OPEN_GYM_MUSIC_QUERY

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict(),
            "spin": self.spin.to_dict(),
            "cubeHost": self.cube_host,
            "cubeAdbPort": int(self.cube_adb_port),
            "openGymPlayMusic": bool(self.open_gym_play_music),
            "openGymMusicUrl": self.open_gym_music_url,
            "openGymMusicQuery": self.open_gym_music_query,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "GymDisplaysConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            cube_adb_port = max(1, min(65535, int(raw.get("cubeAdbPort", 5555))))
        except (TypeError, ValueError):
            cube_adb_port = 5555
        query_raw = raw.get("openGymMusicQuery", DEFAULT_OPEN_GYM_MUSIC_QUERY)
        query_clean = str(DEFAULT_OPEN_GYM_MUSIC_QUERY if query_raw is None else query_raw).strip()
        return cls(
            primary=PrimaryDisplayConfig.from_dict(raw.get("primary")),
            spin=SpinDisplayConfig.from_dict(raw.get("spin")),
            cube_host=str(raw.get("cubeHost", DEFAULT_CUBE_HOST) or "").strip(),
            cube_adb_port=cube_adb_port,
            open_gym_play_music=bool(raw.get("openGymPlayMusic", True)),
            open_gym_music_url=str(raw.get("openGymMusicUrl", "") or "").strip(),
            open_gym_music_query=query_clean or DEFAULT_OPEN_GYM_MUSIC_QUERY,
        )


def normalize_youtube_tv_url(url: str) -> str:
    """Convert `music.youtube.com` links to `www.youtube.com` for Fire TV YouTube app intents."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    return re.sub(r"^https?://music\.youtube\.com\b", "https://www.youtube.com", raw, flags=re.I)


def resolve_youtube_music_url(
    query: str,
    http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
) -> str:
    """Look up top YouTube video ID for `query` and return an auto-playing Radio Mix URL."""
    q = str(query or "").strip() or DEFAULT_OPEN_GYM_MUSIC_QUERY
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q)}"
    try:
        if http_get is not None:
            code, raw = http_get(search_url, 4.0)
        else:
            req = urllib.request.Request(
                search_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                code, raw = int(resp.status), resp.read()
        if code >= 400:
            return ""
        html = raw.decode("utf-8", errors="ignore")
        m = re.search(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', html)
        if not m:
            return ""
        vid = m.group(1)
        return f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    except Exception:
        return ""


_HDMI_INPUT_KEYCODES: dict[str, str] = {
    "keycode_tv_input_hdmi_1": "243",
    "hdmi1": "243",
    "hdmi 1": "243",
    "hdmi_1": "243",
    "/hw2": "243",
    "keycode_tv_input_hdmi_2": "244",
    "hdmi2": "244",
    "hdmi 2": "244",
    "hdmi_2": "244",
    "/hw3": "244",
    "keycode_tv_input_hdmi_3": "245",
    "hdmi3": "245",
    "hdmi 3": "245",
    "hdmi_3": "245",
    "/hw4": "245",
    "keycode_tv_input_hdmi_4": "246",
    "hdmi4": "246",
    "hdmi 4": "246",
    "hdmi_4": "246",
    "/hw5": "246",
}


def resolve_hdmi_keyevent(input_spec: str, fallback_keycode: str) -> str:
    """Resolve an HDMI keycode, shorthand, or legacy MediaTek TIF ID to an ADB keyevent code."""
    s = str(input_spec or "").strip().lower()
    if not s:
        return fallback_keycode
    if s.isdigit():
        return s
    for key, code in _HDMI_INPUT_KEYCODES.items():
        if key.startswith("/") and s.endswith(key):
            return code
        if s == key:
            return code
    return fallback_keycode


def _default_http_get(url: str, timeout: float = 1.5) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


def _default_http_post(
    url: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


def _parse_denon_video_form(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    form_m = re.search(r'<FORM\b[^>]*name=["\']video["\'][^>]*>(.*?)</FORM>', html, re.I | re.S)
    body = form_m.group(1) if form_m else html
    for m in re.finditer(r"<INPUT\b([^>]*)>", body, re.I):
        attrs = m.group(1)
        tp = re.search(r'type=["\']?([^"\'\s>]+)', attrs, re.I)
        nm = re.search(r'name=["\']?([^"\'\s>]+)', attrs, re.I)
        vl = re.search(r'value=["\']([^"\']*)["\']', attrs, re.I) or re.search(r"value=([^\s>]+)", attrs, re.I)
        if not nm:
            continue
        t = (tp.group(1) if tp else "").lower()
        name = nm.group(1)
        val = vl.group(1) if vl else ""
        if t in ("hidden", "text"):
            fields[name] = val
        elif t == "radio" and re.search(r"\bchecked\b", attrs, re.I):
            fields[name] = val
    for m in re.finditer(r'<SELECT\b[^>]*name=["\']?([^"\'\s>]+)[^>]*>(.*?)</SELECT>', body, re.I | re.S):
        name = m.group(1)
        sel = (
            re.search(r'<OPTION\b[^>]*selected[^>]*value=["\']([^"\']*)["\']', m.group(2), re.I)
            or re.search(r'<OPTION\b[^>]*value=["\']([^"\']*)["\'][^>]*selected', m.group(2), re.I)
        )
        if sel:
            fields[name] = sel.group(1)
    return fields


_IP_CTRL_UNSAFE_FIELD_RE = re.compile(
    r"(?:dhcp|ipaddr|ip_addr|subnet|netmask|gateway|dns|proxy|ssid|pass|wep|wpa|macaddr)",
    re.I,
)
_IP_CTRL_KEYWORD_RE = re.compile(
    r"(?:ip\s*control|network\s*(?:control|standby)|net\s*control|ipcontrol|netcontrol|standby)",
    re.I,
)


def _strip_html_label(raw: str, max_len: int = 120) -> str:
    txt = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", txt).strip()[:max_len]


def find_denon_ip_control_form(html: str) -> dict[str, Any] | None:
    """Detect a safe standalone Denon IP Control / Network Standby form without DHCP/IP fields."""
    if not html or "<form" not in html.lower():
        return None

    for fm in re.finditer(r"<FORM\b([^>]*)>(.*?)</FORM>", html, re.I | re.S):
        form_attrs, raw_body = fm.group(1), fm.group(2)
        act_m = re.search(r'\baction=["\']([^"\']*)["\']|\baction=([^\s>]+)', form_attrs, re.I)
        action_str = (act_m.group(1) if (act_m and act_m.group(1) is not None) else (act_m.group(2) if act_m else "")) or ""
        clean_body = re.sub(r"<!--.*?-->", " ", raw_body, flags=re.S)

        fields: dict[str, str] = {}
        all_names: list[str] = []
        text_inputs = 0
        radios: dict[str, list[tuple[str, str, bool]]] = {}
        selects: dict[str, list[tuple[str, str, bool]]] = {}

        for im in re.finditer(r"<INPUT\b([^>]*)>", clean_body, re.I):
            attrs = im.group(1)
            tp_m = re.search(r'\btype=["\']?([^"\'\s>]+)', attrs, re.I)
            nm_m = re.search(r'\bname=["\']?([^"\'\s>]+)', attrs, re.I)
            vl_m = re.search(r'\bvalue=["\']([^"\']*)["\']', attrs, re.I) or re.search(r"\bvalue=([^\s>]+)", attrs, re.I)
            if not nm_m:
                continue
            t = (tp_m.group(1) if tp_m else "text").lower()
            if t in ("submit", "button", "reset", "image"):
                continue
            name = nm_m.group(1)
            val = html_lib.unescape(vl_m.group(1)) if vl_m else ""
            is_checked = bool(re.search(r"\bchecked\b", attrs, re.I))
            all_names.append(name)
            if t == "text":
                text_inputs += 1
            if t in ("hidden", "text"):
                fields[name] = val
            elif t == "checkbox":
                if is_checked:
                    fields[name] = val or "on"
            elif t == "radio":
                if is_checked:
                    fields[name] = val
                tail = clean_body[im.end():]
                stop_m = re.search(r"<(?:input|select)\b|</(?:tr|p|li|form)\b", tail, re.I)
                snippet = tail[: stop_m.start()] if stop_m else tail[:240]
                radios.setdefault(name, []).append((val, _strip_html_label(snippet), is_checked))

        for sm in re.finditer(r"<SELECT\b([^>]*)>(.*?)</SELECT>", clean_body, re.I | re.S):
            s_attrs, s_body = sm.group(1), sm.group(2)
            nm_m = re.search(r'\bname=["\']?([^"\'\s>]+)', s_attrs, re.I)
            if not nm_m:
                continue
            name = nm_m.group(1)
            all_names.append(name)
            opts: list[tuple[str, str, bool]] = []
            for om in re.finditer(r"<OPTION\b([^>]*)>(.*?)(?=<OPTION\b|$)", s_body, re.I | re.S):
                o_attrs, o_raw_txt = om.group(1), om.group(2)
                lbl = _strip_html_label(o_raw_txt)
                vl_m = re.search(r'\bvalue=["\']([^"\']*)["\']', o_attrs, re.I) or re.search(r"\bvalue=([^\s>]+)", o_attrs, re.I)
                val = html_lib.unescape(vl_m.group(1)) if vl_m else lbl
                is_sel = bool(re.search(r"\bselected\b", o_attrs, re.I))
                if is_sel:
                    fields[name] = val
                opts.append((val, lbl, is_sel))
            if opts:
                if name not in fields:
                    fields[name] = opts[0][0]
                selects[name] = opts

        # Safety Guard 1: never submit a form with IP/DHCP/Wi-Fi settings or IPv4 octets
        if text_inputs >= 4 or any(_IP_CTRL_UNSAFE_FIELD_RE.search(nm) for nm in all_names):
            continue

        # Safety Guard 2: require IP/Network Control context and distinct Always On / Standby Off pair
        form_text = _strip_html_label(clean_body, max_len=4000)
        form_has_kw = bool(_IP_CTRL_KEYWORD_RE.search(form_text))
        candidates = sorted(
            [*radios.items(), *selects.items()],
            key=lambda kv: (0 if _IP_CTRL_KEYWORD_RE.search(kv[0]) else 1),
        )
        for ctrl_name, options in candidates:
            if not (form_has_kw or _IP_CTRL_KEYWORD_RE.search(ctrl_name)):
                continue
            always_on_val: str | None = None
            for val, lbl, _cur in options:
                if (
                    re.search(r"\balways\s*_?\s*on\b", lbl, re.I)
                    or re.search(r"\balways\s*_?\s*on\b", val, re.I)
                    or val.strip().upper() in ("ALWAYS", "ALWAYS_ON", "ALWAYSON")
                ):
                    always_on_val = val
                    break
            if always_on_val is None and any(
                re.search(r"off\s*in\s*standby|\bstandby\b", lbl, re.I) or re.search(r"standby", val, re.I)
                for val, lbl, _cur in options
            ):
                for val, lbl, _cur in options:
                    if re.match(r"^on$", lbl.strip(), re.I) or val.strip().upper() == "ON":
                        always_on_val = val
                        break
            if always_on_val is None:
                continue

            standby_off_val: str | None = None
            for val, lbl, _cur in options:
                if val == always_on_val:
                    continue
                if re.search(r"standby|\boff\b", lbl, re.I) or re.search(r"standby|\boff\b", val, re.I):
                    standby_off_val = val
                    break
            if standby_off_val is None:
                continue

            current_val = fields.get(ctrl_name, "")
            if not current_val:
                for val, _lbl, is_cur in options:
                    if is_cur:
                        current_val = val
                        break
            return {
                "action": action_str,
                "fieldName": ctrl_name,
                "alwaysOnValue": always_on_val,
                "standbyOffValue": standby_off_val,
                "currentValue": current_val,
                "alreadyAlwaysOn": current_val == always_on_val,
                "fields": fields,
            }
    return None


def parse_roku_device_info(xml_text: str) -> dict[str, Any]:
    """Parse Roku ECP `/query/device-info` XML into structured metadata."""
    text = (xml_text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty Roku XML"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"ok": False, "error": f"Roku XML parse error: {exc}"}

    def _txt(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    return {
        "ok": True,
        "name": _txt("user-device-name") or _txt("friendly-device-name"),
        "location": _txt("user-device-location"),
        "model": _txt("friendly-model-name") or _txt("model-name"),
        "modelNumber": _txt("model-number"),
        "powerMode": _txt("power-mode"),
        "mac": _txt("wifi-mac") or _txt("ethernet-mac"),
    }


def parse_roku_active_app(xml_text: str) -> dict[str, Any]:
    """Parse Roku ECP `/query/active-app` XML into structured app ID and name."""
    text = (xml_text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty Roku active-app XML"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"ok": False, "error": f"Roku XML parse error: {exc}"}
    app_el = root.find("app")
    if app_el is None:
        return {"ok": True, "appId": "", "appType": "", "name": ""}
    return {
        "ok": True,
        "appId": str(app_el.attrib.get("id") or "").strip(),
        "appType": str(app_el.attrib.get("type") or "").strip(),
        "name": (app_el.text or "").strip(),
    }


def _strip_wake_prefix(text: str, wake_word: str = "armory") -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    wake_raw = (wake_word or "armory").strip().lower() or "armory"
    if wake_raw == "armory":
        alts = ["armory fit", "armoryfit", "armourys", "armorys", "armoury", "armory", "amory"]
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
    else:
        alts = [wake_raw]
    pat = r"^(?:(?:hey|ok|okay)\s+)?(?:" + "|".join(re.escape(a) for a in alts) + r")(?:\s+|$)"
    return re.sub(pat, "", s).strip()


_DISPLAY_SPEECH_LABELS = {
    "tv": "TV",
    "wallboard": "Wallboard",
    "spin": "SmartSpin TV",
    "cube": "Fire TV Cube",
    "avr": "Denon",
}


def _format_multi_speech(targets: Sequence[str], action: str) -> str:
    verb = "on" if action == "on" else "off"
    names = [_DISPLAY_SPEECH_LABELS.get(t, t) for t in targets]
    if not names:
        return f"Display {verb}"
    if len(names) == 1:
        return f"{names[0]} {verb}"
    if len(names) == 2:
        return f"{names[0]} and {names[1]} {verb}"
    return f"{', '.join(names[:-1])} and {names[-1]} {verb}"


def parse_display_voice_command(transcript: str, wake_word: str = "armory") -> dict[str, Any] | None:
    """Parse natural-language TV, Wallboard, SmartSpin, Denon, Open/Close Gym commands."""
    cmd = _strip_wake_prefix(transcript, wake_word=wake_word)
    if not cmd:
        return None

    # 1. Open / Close Gym macros
    if re.search(r"\bopen\s+(?:the\s+)?gym\b", cmd):
        return {
            "target": "open_gym",
            "targets": ["open_gym"],
            "action": "run",
            "speech": "",
            "silent": True,
        }
    if re.search(
        r"\b(?:close[ds]?|clothes?|cloze|flows|first|shut\s*(?:down|off)|power\s*(?:down|off)|turn\s+off)\s+(?:(?:the|this)\s+)?gym\b"
        r"|\bgym\s+off$"
        r"|^(?:please\s+)?(?:shut\s*down|shutdown|power\s+down|close\s+up|close\s+(?:it\s+)?down)(?:\s+(?:now|please))?$",
        cmd,
    ):
        return {
            "target": "close_gym",
            "targets": ["close_gym"],
            "action": "run",
            "speech": "",
            "silent": True,
        }

    # 1b. Music playback commands on Fire TV Cube / Denon AVR (skip if user said clip/video/replay)
    if not re.search(r"\b(?:clip|video|replay)\b", cmd):
        if re.search(
            r"\b(?:play\s+(?:some\s+)?(?:music|songs?|tracks?|tunes|radio|playlist|mix)|(?:start|turn\s+on|put\s+on)\s+(?:some\s+|the\s+)?music)\b",
            cmd,
        ):
            return {"target": "music", "targets": ["music"], "action": "play", "speech": "Playing music"}
        q_m = re.search(
            r"\bplay\s+(?:some\s+)?(.+?)\s+(?:music|songs?|tracks?|tunes|radio|playlist|mix|on\s+youtube|on\s+cube)\b",
            cmd,
        )
        if q_m:
            query_txt = re.sub(r"\b(?:on\s+(?:the\s+)?(?:cube|tv|fire\s*tv|youtube|speakers))\b", "", q_m.group(1)).strip()
            if query_txt:
                return {
                    "target": "music",
                    "targets": ["music"],
                    "action": f"play:{query_txt}",
                    "speech": f"Playing {query_txt}",
                }
        if re.search(r"\b(?:pause|stop)\s+(?:the\s+)?(?:music|song|track|tunes)\b", cmd):
            return {"target": "music", "targets": ["music"], "action": "pause", "speech": "Music paused"}
        if re.search(r"\b(?:resume|unpause|continue)\s+(?:the\s+)?(?:music|song|track|tunes)\b", cmd):
            return {"target": "music", "targets": ["music"], "action": "resume", "speech": "Resuming music"}
        if re.search(r"\b(?:next|skip)\s+(?:this\s+|the\s+)?(?:song|track|music)\b", cmd):
            return {"target": "music", "targets": ["music"], "action": "next", "speech": "Next track"}
        if re.search(r"\b(?:previous|last|back)\s+(?:the\s+)?(?:song|track|music)\b", cmd):
            return {"target": "music", "targets": ["music"], "action": "prev", "speech": "Previous track"}

    # Ignore lifting/cardio cues outright if there are no display/TV/AVR/HDMI words
    if not re.search(
        r"\b(display|tv|tvs|screen|screens|hisense|primary|spin|smartspin|smart\s+spin|tcl|roku|bike|wallboard|wall\s+board|toshiba|denon|avr|stereo|receiver|speakers|hdmi|optiplex|pc|computer|desktop|fire\s*tv|cube|everything)\b",
        cmd,
    ):
        return None

    # 2. Explicit HDMI / input switching on Main TV (Hisense)
    if re.search(r"\b(hdmi\s*1|hdmi\s*one|optiplex|pc|computer|desktop)\b", cmd) and re.search(
        r"\b(switch\s+(?!on\b|off\b)|set|change|put|show|go|to|input|hdmi|main|primary|hisense|tv|display)\b", cmd
    ):
        return {"target": "tv", "targets": ["tv"], "action": "hdmi1", "speech": "TV on PC"}

    if re.search(r"\b(hdmi\s*2|hdmi\s*two|fire\s*tv|cube|denon)\b", cmd) and re.search(
        r"\b(switch\s+(?!on\b|off\b)|set|change|put|show|go|to|input|hdmi)\b", cmd
    ):
        return {"target": "tv", "targets": ["tv"], "action": "hdmi2", "speech": "TV on Fire TV"}

    # 3. Denon Multi-Ch Stereo command
    if re.search(r"\b(denon|avr|receiver|stereo)\b", cmd) and re.search(
        r"\b(multi\s*channel|multi\s*ch|all\s*speakers)\b", cmd
    ):
        return {"target": "avr", "targets": ["avr"], "action": "mch_stereo", "speech": "Denon multi channel stereo"}

    # 3b. Fire TV Cube Home screen
    if re.search(r"\b(?:fire\s*tv(?:\s+cube)?|cube)\b", cmd) and re.search(r"\bhome\b", cmd):
        return {"target": "cube", "targets": ["cube"], "action": "home", "speech": "Fire TV Cube home"}

    # 4. Determine power action (on vs off)
    action: str | None = None
    if re.search(r"\b(turn\s+on|power\s+on|switch\s+on|wake(?:\s+up)?|boot(?:\s+up)?|start)\b", cmd) or re.search(
        r"\bon$", cmd
    ):
        action = "on"
    elif re.search(r"\b(turn\s+off|power\s+off|shut\s*(?:off|down)|switch\s+off|sleep|kill)\b", cmd) or re.search(
        r"\boff$", cmd
    ):
        action = "off"

    if action is None:
        return None

    # 5. All TVs / Everything
    if re.search(r"\b(all\s+(?:(?:three|3)\s+)?(?:tvs|displays|screens)|armory\s+tvs|everything)\b", cmd):
        return {
            "target": "all_tvs",
            "targets": ["tv", "wallboard", "spin"],
            "action": action,
            "speech": "All TVs on" if action == "on" else "All TVs off",
        }

    # 6. Compound or single targets matched specific-first, blanking matched spans so "SmartSpin TV" != "TV"
    working = cmd
    found: list[tuple[int, str]] = []
    target_patterns = [
        (
            "spin",
            r"\b(?:smart\s*spin(?:\s+(?:tv|display|screen))?|spin(?:\s+bike)?(?:\s+(?:tv|display|screen))?|tcl(?:\s+tv)?|roku(?:\s+tv)?|bike\s+(?:tv|display|screen))\b",
        ),
        (
            "wallboard",
            r"\b(?:wall\s*board(?:\s+(?:tv|display|screen))?|32\s*(?:inch)?\s*tv|toshiba(?:\s+tv)?)\b",
        ),
        (
            "cube",
            r"\b(?:fire\s*tv(?:\s+cube)?|cube)\b",
        ),
        (
            "avr",
            r"\b(?:denon|avr|stereo|receiver|speakers)\b",
        ),
        (
            "tv",
            r"\b(?:(?:main|primary)\s+(?:tv|display|screen)|primary|hisense(?:\s+tv)?|tv)\b",
        ),
    ]
    for tgt_id, pat in target_patterns:
        for m in re.finditer(pat, working):
            found.append((m.start(), tgt_id))
        working = re.sub(pat, lambda m: " " * len(m.group(0)), working)

    if not found:
        return None

    found.sort(key=lambda pair: pair[0])
    ordered: list[str] = []
    for _pos, tgt_id in found:
        if tgt_id not in ordered:
            ordered.append(tgt_id)

    return {
        "target": ordered[0] if len(ordered) == 1 else "multi",
        "targets": ordered,
        "action": action,
        "speech": _format_multi_speech(ordered, action),
    }


class GymDisplayController:
    """Discrete power/input controller across TV, SmartSpin TV, Wallboard, Denon AVR & Open/Close Gym."""

    def __init__(
        self,
        *,
        config: GymDisplaysConfig | None = None,
        adb_controller: Any = None,
        denon_client: Any = None,
        wake_wallboard_fn: Callable[[bool], dict[str, Any]] | None = None,
        sleep_wallboard_fn: Callable[[], dict[str, Any]] | None = None,
        wol_sender: Callable[[str], list[str]] | None = None,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        http_post: Callable[[str, bytes, dict[str, str] | None, float], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        allow_network: bool = True,
        state_dir: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.cfg = config or GymDisplaysConfig()
        self.adb = adb_controller
        self.denon = denon_client
        self._wake_wallboard_fn = wake_wallboard_fn
        self._sleep_wallboard_fn = sleep_wallboard_fn
        self._wol_sender = wol_sender
        self._http_get = http_get
        self._http_post = http_post
        self._sleep = sleep_fn or time.sleep
        self._allow_network = bool(allow_network)
        self.last_primary_probe: dict[str, Any] = {}
        self.last_cube_probe: dict[str, Any] = {}
        self.last_spin_info: dict[str, Any] = {}
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._ip_ctrl_state_path = (self._state_dir / "denon_ip_control_state.json") if self._state_dir else None
        self._ip_ctrl_dump_path = (self._state_dir / "denon_network_dump.json") if self._state_dir else None
        self._denon_ip_ctrl_state: dict[str, Any] = self._load_denon_ip_control_state()

    def _load_denon_ip_control_state(self) -> dict[str, Any]:
        if self._ip_ctrl_state_path is not None and self._ip_ctrl_state_path.is_file():
            try:
                raw = json.loads(self._ip_ctrl_state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("state"):
                    return raw
            except Exception:
                pass
        return {"state": "pending"}

    def get_denon_ip_control_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._denon_ip_ctrl_state)

    def _save_denon_ip_control_state(self, data: dict[str, Any]) -> None:
        snap = dict(data)
        with self._lock:
            self._denon_ip_ctrl_state = snap
            path = self._ip_ctrl_state_path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _save_denon_network_dump(self, captured_pages: dict[str, str]) -> None:
        if self._ip_ctrl_dump_path is None or not captured_pages:
            return
        payload = {
            "savedAtMs": int(time.time() * 1000),
            "pages": captured_pages,
        }
        self._ip_ctrl_dump_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ip_ctrl_dump_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._ip_ctrl_dump_path)

    def is_denon_tcp_reachable(self, timeout: float = 0.8) -> bool:
        """Fast TCP check to see whether the Denon AVR web interface is currently awake."""
        if self.denon is None:
            return False
        dcfg = self.denon._get_cfg()
        if not getattr(dcfg, "denon_enabled", False):
            return False
        raw_host = str(getattr(dcfg, "denon_host", "") or "").strip()
        if not raw_host:
            return False
        if self._http_get is not None:
            return True
        if not self._allow_network:
            return False
        port = int(getattr(dcfg, "denon_port", 80) or 80)
        host = raw_host
        if ":" in raw_host and not raw_host.startswith("["):
            h_part, _, p_part = raw_host.rpartition(":")
            if p_part.isdigit():
                host = h_part
                port = int(p_part)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def ensure_denon_ip_control_always_on(self, *, force: bool = False) -> dict[str, Any]:
        """Crawl Denon `/SETUP/NETWORK/`, verify or set IP Control = Always On, else safely dump HTML."""
        if not force:
            cur = self.get_denon_ip_control_state()
            if cur.get("state") == "verified_always_on":
                return {**cur, "ok": True}

        if self.denon is None:
            return {"ok": False, "state": "pending", "error": "Denon client unavailable"}
        dcfg = self.denon._get_cfg()
        base = self.denon._base_url(dcfg)
        if not getattr(dcfg, "denon_enabled", False) or not base:
            return {"ok": False, "state": "pending", "error": "Denon disabled or unconfigured"}

        base_netloc = urllib.parse.urlparse(base).netloc.lower()
        seed_paths = (
            "/SETUP/NETWORK/IPCONTROL/d_ipcontrol.asp",
            "/SETUP/NETWORK/d_ipcontrol.asp",
            "/SETUP/NETWORK/NETCONTROL/d_netcontrol.asp",
            "/SETUP/NETWORK/d_netcontrol.asp",
            "/SETUP/NETWORK/d_network.asp",
            "/SETUP/NETWORK/f_network.asp",
            "/SETUP/NETWORK/l_network.asp",
            "/SETUP/l_menu.asp",
            "/SETUP/l_setup.asp",
            "/SETUP/f_setup.asp",
        )
        queue: list[str] = [urllib.parse.urljoin(base + "/", p) for p in seed_paths]
        seen: set[str] = set()
        captured_pages: dict[str, str] = {}
        reachable_any = False

        while queue and len(seen) < 20:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                code, raw = self._do_get(url, 3.0)
            except Exception:
                continue
            if code < 500:
                reachable_any = True
            if not (200 <= code < 300):
                continue
            html_txt = raw.decode("utf-8", errors="ignore")
            path_key = urllib.parse.urlparse(url).path or url
            captured_pages[path_key] = html_txt[:32000]

            for link_m in re.finditer(
                r'(?:href|src)\s*=\s*["\']([^"\']+)["\']|["\']([^"\']*\.(?:asp|html?)(?:\?[^"\']*)?)["\']',
                html_txt,
                re.I,
            ):
                cand = (link_m.group(1) or link_m.group(2) or "").strip()
                if not cand or cand.startswith(("#", "javascript:", "mailto:")):
                    continue
                joined = urllib.parse.urljoin(url, cand).split("#", 1)[0]
                parsed_j = urllib.parse.urlparse(joined)
                if parsed_j.netloc.lower() != base_netloc or joined in seen:
                    continue
                upper_p = parsed_j.path.upper()
                fname_low = parsed_j.path.rsplit("/", 1)[-1].lower()
                if fname_low.startswith("s_"):
                    continue
                if (
                    any(tok in upper_p for tok in ("NETWORK", "IPCONTROL", "NETCONTROL", "STANDBY"))
                    or upper_p.startswith(("/SETUP/L_", "/SETUP/F_"))
                ):
                    queue.append(joined)

        if not reachable_any:
            return {
                "ok": False,
                "state": "pending",
                "reachable": False,
                "error": "Denon unreachable (in standby)",
            }

        self._save_denon_network_dump(captured_pages)

        for path_key, page_html in captured_pages.items():
            page_url = urllib.parse.urljoin(base + "/", path_key)
            match = find_denon_ip_control_form(page_html)
            if not match:
                continue

            if match["alreadyAlwaysOn"]:
                res = {
                    "ok": True,
                    "state": "verified_always_on",
                    "changed": False,
                    "page": page_url,
                    "field": match["fieldName"],
                    "value": match["alwaysOnValue"],
                    "updatedAtMs": int(time.time() * 1000),
                }
                self._save_denon_ip_control_state(res)
                return res

            action_rel = str(match.get("action") or "").strip()
            if action_rel:
                post_url = urllib.parse.urljoin(page_url, action_rel)
            else:
                parsed_p = urllib.parse.urlparse(page_url)
                parts = parsed_p.path.rsplit("/", 1)
                fname = parts[-1]
                if not fname.lower().startswith("d_"):
                    res = {
                        "ok": False,
                        "state": "needs_review",
                        "changed": False,
                        "page": page_url,
                        "dumpPath": str(self._ip_ctrl_dump_path or ""),
                        "error": f"No safe POST target for {path_key}",
                        "updatedAtMs": int(time.time() * 1000),
                    }
                    self._save_denon_ip_control_state(res)
                    return res
                new_fname = ("s_" if fname.startswith("d_") else "S_") + fname[2:]
                new_path = f"{parts[0]}/{new_fname}" if len(parts) == 2 else new_fname
                post_url = urllib.parse.urlunparse(parsed_p._replace(path=new_path))

            post_fields = dict(match["fields"])
            post_fields[match["fieldName"]] = match["alwaysOnValue"]
            encoded = urllib.parse.urlencode(post_fields).encode("ascii")
            try:
                pcode, _ = self._do_post(
                    post_url,
                    encoded,
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    3.5,
                )
            except Exception as exc:
                res = {
                    "ok": False,
                    "state": "needs_review",
                    "changed": False,
                    "page": page_url,
                    "postUrl": post_url,
                    "dumpPath": str(self._ip_ctrl_dump_path or ""),
                    "error": f"Denon POST failed: {exc}",
                    "updatedAtMs": int(time.time() * 1000),
                }
                self._save_denon_ip_control_state(res)
                return res

            if pcode >= 400:
                res = {
                    "ok": False,
                    "state": "needs_review",
                    "changed": False,
                    "page": page_url,
                    "postUrl": post_url,
                    "dumpPath": str(self._ip_ctrl_dump_path or ""),
                    "error": f"Denon POST returned HTTP {pcode}",
                    "updatedAtMs": int(time.time() * 1000),
                }
                self._save_denon_ip_control_state(res)
                return res

            self._sleep(1.0)
            try:
                vcode, vraw = self._do_get(page_url, 3.0)
            except Exception as exc:
                res = {
                    "ok": False,
                    "state": "needs_review",
                    "changed": False,
                    "page": page_url,
                    "postUrl": post_url,
                    "dumpPath": str(self._ip_ctrl_dump_path or ""),
                    "error": f"Denon verify GET failed: {exc}",
                    "updatedAtMs": int(time.time() * 1000),
                }
                self._save_denon_ip_control_state(res)
                return res

            v_match = find_denon_ip_control_form(vraw.decode("utf-8", errors="ignore")) if vcode < 400 else None
            if v_match and v_match["alreadyAlwaysOn"]:
                res = {
                    "ok": True,
                    "state": "verified_always_on",
                    "changed": True,
                    "page": page_url,
                    "postUrl": post_url,
                    "field": v_match["fieldName"],
                    "value": v_match["alwaysOnValue"],
                    "updatedAtMs": int(time.time() * 1000),
                }
                self._save_denon_ip_control_state(res)
                return res

            res = {
                "ok": False,
                "state": "needs_review",
                "changed": False,
                "page": page_url,
                "postUrl": post_url,
                "dumpPath": str(self._ip_ctrl_dump_path or ""),
                "error": "POST succeeded but re-read did not confirm Always On",
                "updatedAtMs": int(time.time() * 1000),
            }
            self._save_denon_ip_control_state(res)
            return res

        res = {
            "ok": False,
            "state": "needs_review",
            "changed": False,
            "capturedPages": sorted(captured_pages.keys()),
            "dumpPath": str(self._ip_ctrl_dump_path or ""),
            "message": "Denon web pages captured; no safe standalone IP Control form matched (zero settings modified)",
            "updatedAtMs": int(time.time() * 1000),
        }
        self._save_denon_ip_control_state(res)
        return res

    def get_config(self) -> GymDisplaysConfig:
        with self._lock:
            return self.cfg

    def update_config(self, raw: dict[str, Any]) -> GymDisplaysConfig:
        with self._lock:
            cur = self.cfg.to_dict()
            if isinstance(raw, dict):
                if isinstance(raw.get("primary"), dict):
                    cur["primary"] = {**cur["primary"], **raw["primary"]}
                if isinstance(raw.get("spin"), dict):
                    cur["spin"] = {**cur["spin"], **raw["spin"]}
                for k in ("cubeHost", "cubeAdbPort", "openGymPlayMusic", "openGymMusicUrl", "openGymMusicQuery"):
                    if k in raw:
                        cur[k] = raw[k]
            self.cfg = GymDisplaysConfig.from_dict(cur)
            return self.cfg

    def _do_get(self, url: str, timeout: float = 1.5) -> tuple[int, bytes]:
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
        timeout: float = 2.0,
    ) -> tuple[int, bytes]:
        if self._http_post is not None:
            return self._http_post(url, body, headers, timeout)
        if not self._allow_network:
            raise RuntimeError("Network I/O disabled in offline mode")
        return _default_http_post(url, body, headers, timeout)

    @staticmethod
    def _adb_serial(host: str, port: int) -> str:
        clean = str(host or "").strip()
        if not clean:
            return ""
        return clean if ":" in clean else f"{clean}:{int(port)}"

    def set_denon_monitor_out(self, mode: str) -> dict[str, Any]:
        """Switch Denon AVR-3313CI HDMI Monitor Out (`AUTO`, `1`, or `2`) safely via read-before-write."""
        m = str(mode or "").strip().upper()
        if m not in ("AUTO", "1", "2"):
            return {"ok": False, "error": f"Invalid monitor mode: {mode}"}
        if self.denon is None:
            return {"ok": False, "error": "Denon client unavailable"}
        dcfg = self.denon._get_cfg()
        base = self.denon._base_url(dcfg)
        if not dcfg.denon_enabled or not base:
            return {"ok": False, "error": "Denon disabled or unconfigured"}

        try:
            code, raw = self._do_get(f"{base}/SETUP/VIDEO/d_video.asp", 3.5)
            if code < 400:
                fields = _parse_denon_video_form(raw.decode("utf-8", errors="ignore"))
                if "radioMonitorOut" in fields:
                    if str(fields.get("radioMonitorOut") or "").strip().upper() == m:
                        return {"ok": True, "monitorOut": m, "via": "noop_already_set"}
                    fields["radioMonitorOut"] = m
                    encoded = urllib.parse.urlencode(fields).encode("ascii")
                    pcode, _ = self._do_post(
                        f"{base}/SETUP/VIDEO/s_video.asp",
                        encoded,
                        {"Content-Type": "application/x-www-form-urlencoded"},
                        3.5,
                    )
                    if pcode < 400:
                        return {"ok": True, "monitorOut": m, "via": "s_video.asp"}
        except Exception:
            pass

        cmd = "VSMONIAUTO" if m == "AUTO" else f"VSMONI{m}"
        res = self.denon.send_command(cmd)
        return {**res, "monitorOut": m, "via": "direct"}

    def sync_denon_monitor_for_spin(self, *, spin_expected_on: bool | None = None) -> dict[str, Any]:
        """Keep Denon on single sink when TCL SmartSpin is OFF so Hisense never blacks out."""
        cfg = self.get_config().spin
        if not cfg.auto_denon_dual or self.denon is None:
            return {"ok": True, "skipped": True}
        if spin_expected_on is not None:
            spin_on = bool(spin_expected_on)
        elif cfg.host:
            info = self.get_roku_info(cfg.host, cfg.ecp_port)
            spin_on = bool(info.get("ok") and info.get("powerMode") == "PowerOn")
        else:
            spin_on = False
        target = "AUTO" if spin_on else (cfg.primary_denon_monitor if cfg.primary_denon_monitor in ("1", "2") else "1")
        return self.set_denon_monitor_out(target)

    def probe_primary(self) -> dict[str, Any]:
        cfg = self.get_config().primary
        ser = self._adb_serial(cfg.host, cfg.adb_port)
        if not ser:
            return {"ok": False, "state": "unconfigured", "message": "Main TV IP not set."}
        if self.adb is None:
            return {"ok": False, "state": "error", "serial": ser, "message": "ADB controller unavailable."}
        st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            res = {"ok": False, "state": st, "serial": ser, "message": f"ADB {st}: {detail}"}
            self.last_primary_probe = res
            return res

        def _sh(*args: str) -> str:
            rc, out, _ = self.adb._run(["-s", ser, "shell", *args], timeout=4.5)
            return out.strip() if rc == 0 else ""

        model = (_sh("getprop", "ro.product.model").splitlines() or [""])[0].strip()
        pwr = _sh("dumpsys", "power")
        wake_m = re.search(r"mWakefulness=(\w+)", pwr)
        disp_m = re.search(r"Display Power:\s*state=(\w+)", pwr)
        tvin = _sh("dumpsys", "tv_input")
        active_inp = re.search(r"^\s*inputId:\s*(\S+)", tvin, re.M)
        hdmi_ids = sorted(set(re.findall(r"com\.mediatek\.tis/\.HdmiInputService/HW\d+", tvin)))
        res = {
            "ok": True,
            "state": "device",
            "serial": ser,
            "model": model,
            "wakefulness": wake_m.group(1) if wake_m else "",
            "displayPower": disp_m.group(1) if disp_m else "",
            "activeInput": active_inp.group(1) if active_inp else "",
            "hdmiInputs": hdmi_ids,
            "message": f"{model or ser} · {disp_m.group(1) if disp_m else '?'} · {active_inp.group(1) if active_inp else '?'}",
        }
        self.last_primary_probe = res
        return res

    def probe_cube(self) -> dict[str, Any]:
        """Probe Fire TV Cube ADB wakefulness & display power state without waking the device."""
        cfg = self.get_config()
        ser = self._adb_serial(cfg.cube_host, cfg.cube_adb_port)
        if not ser:
            res = {"ok": False, "state": "unconfigured", "message": "Fire TV Cube IP not set."}
            self.last_cube_probe = res
            return res
        if self.adb is None:
            res = {"ok": False, "state": "error", "serial": ser, "message": "ADB controller unavailable."}
            self.last_cube_probe = res
            return res
        st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            res = {"ok": False, "state": st, "serial": ser, "message": f"ADB {st}: {detail}"}
            self.last_cube_probe = res
            return res

        def _sh(*args: str) -> str:
            rc, out, _ = self.adb._run(["-s", ser, "shell", *args], timeout=4.5)
            return out.strip() if rc == 0 else ""

        model = (_sh("getprop", "ro.product.model").splitlines() or [""])[0].strip()
        pwr = _sh("dumpsys", "power")
        wake_m = re.search(r"mWakefulness=(\w+)", pwr)
        disp_m = re.search(r"Display Power:\s*state=(\w+)", pwr)
        wakefulness = wake_m.group(1) if wake_m else ""
        display_power = disp_m.group(1) if disp_m else ""
        res = {
            "ok": True,
            "state": "device",
            "serial": ser,
            "model": model,
            "wakefulness": wakefulness,
            "displayPower": display_power,
            "message": f"{model or ser} · {wakefulness or '?'} · Display {display_power or '?'}",
        }
        self.last_cube_probe = res
        return res

    def _control_primary(self, action: str) -> dict[str, Any]:
        cfg = self.get_config().primary
        if not cfg.enabled:
            return {"ok": False, "target": "tv", "action": action, "error": "Main TV disabled"}
        ser = self._adb_serial(cfg.host, cfg.adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "target": "tv", "action": action, "error": "Main TV IP or ADB missing"}

        if action == "on":
            st, detail = self.adb._connect_and_status(ser)
            if st != "device" and cfg.mac and self._wol_sender is not None:
                self._wol_sender(cfg.mac)
                self._sleep(0.8)
                st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "tv", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "tv",
                "action": "on",
                "serial": ser,
                "message": "TV woken (discrete KEYCODE_WAKEUP)" if rc == 0 else (err or out).strip(),
            }

        if action == "off":
            st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "tv", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "223"], timeout=4.0)
            if rc == 0:
                self._sleep(0.25)
                if hasattr(self.adb, "disconnect_serial"):
                    self.adb.disconnect_serial(ser)
            return {
                "ok": rc == 0,
                "target": "tv",
                "action": "off",
                "serial": ser,
                "message": "TV put to sleep (discrete KEYCODE_SLEEP)" if rc == 0 else (err or out).strip(),
            }

        if action in ("hdmi1", "pc", "hdmi2", "firetv"):
            is_hdmi1 = action in ("hdmi1", "pc")
            input_id = cfg.hdmi_optiplex_input if is_hdmi1 else cfg.hdmi_denon_input
            if not input_id:
                return {"ok": False, "target": "tv", "action": action, "error": "HDMI input ID not configured"}
            keycode = resolve_hdmi_keyevent(input_id, "243" if is_hdmi1 else "244")
            st, detail = self.adb._connect_and_status(ser)
            if st != "device" and cfg.mac and self._wol_sender is not None:
                self._wol_sender(cfg.mac)
                self._sleep(0.8)
                st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "tv", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            self._sleep(0.25)
            rc, out, err = self.adb._run(
                ["-s", ser, "shell", "input", "keyevent", keycode],
                timeout=4.0,
            )
            denon_res = self.sync_denon_monitor_for_spin() if (rc == 0 and not is_hdmi1) else None
            return {
                "ok": rc == 0,
                "target": "tv",
                "action": action,
                "keycode": keycode,
                "inputId": input_id,
                **({"denonMonitor": denon_res} if denon_res is not None else {}),
                "message": (
                    f"Switched TV to {'HDMI 1 (PC)' if is_hdmi1 else 'HDMI 2 (Fire TV)'} (keyevent {keycode})"
                    if rc == 0
                    else (err or out).strip()
                ),
            }

        return {"ok": False, "target": "tv", "action": action, "error": f"Unsupported action: {action}"}

    def ensure_primary_hdmi_ready(self, action: str = "hdmi2") -> dict[str, Any]:
        """Ensure Hisense primary TV is ON and on the requested HDMI port without Denon monitor flips."""
        cfg = self.get_config().primary
        if not cfg.enabled:
            return {"ok": False, "target": "tv", "action": action, "error": "Main TV disabled"}
        ser = self._adb_serial(cfg.host, cfg.adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "target": "tv", "action": action, "error": "Main TV IP or ADB missing"}

        is_hdmi1 = str(action or "hdmi2").strip().lower() in ("hdmi1", "pc")
        input_id = cfg.hdmi_optiplex_input if is_hdmi1 else cfg.hdmi_denon_input
        if not input_id:
            return {"ok": False, "target": "tv", "action": action, "error": "HDMI input ID not configured"}
        keycode = resolve_hdmi_keyevent(input_id, "243" if is_hdmi1 else "244")

        st, detail = self.adb._connect_and_status(ser)
        if st != "device" and cfg.mac and self._wol_sender is not None:
            self._wol_sender(cfg.mac)
            self._sleep(0.8)
            st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            return {"ok": False, "target": "tv", "action": action, "state": st, "error": f"ADB {st}: {detail}"}

        try:
            def _sh(*args: str) -> str:
                rc, out, _ = self.adb._run(["-s", ser, "shell", *args], timeout=4.5)
                return out.strip() if rc == 0 else ""

            pwr = _sh("dumpsys", "power")
            disp_m = re.search(r"Display Power:\s*state=(\w+)", pwr)
            display_power = disp_m.group(1) if disp_m else ""

            tvin = _sh("dumpsys", "tv_input")
            active_m = re.search(r"^\s*inputId:\s*(\S+)", tvin, re.M)
            active_input = active_m.group(1) if active_m else ""
            raw_hdmi_ids = set(re.findall(r"com\.mediatek\.tis/\.HdmiInputService/HW\d+", tvin))

            def _hw_num(item: str) -> int:
                m_hw = re.search(r"(\d+)$", item)
                return int(m_hw.group(1)) if m_hw else 0

            hdmi_ids = sorted(raw_hdmi_ids, key=_hw_num)
            port_idx = {"243": 0, "244": 1, "245": 2, "246": 3}.get(keycode, 1)
            target_hw = hdmi_ids[port_idx] if port_idx < len(hdmi_ids) else ""

            if display_power == "ON" and target_hw and active_input == target_hw:
                return {
                    "ok": True,
                    "target": "tv",
                    "action": action,
                    "alreadyReady": True,
                    "switched": False,
                    "displayPower": display_power,
                    "activeInput": active_input,
                    "targetInput": target_hw,
                    "message": f"Main TV already ON on {target_hw}",
                }

            switched = False
            if display_power != "ON":
                self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
                self._sleep(0.35)
                switched = True
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", keycode], timeout=4.0)
            if rc == 0 and (not target_hw or active_input != target_hw):
                switched = True
            return {
                "ok": rc == 0,
                "target": "tv",
                "action": action,
                "alreadyReady": False,
                "switched": switched,
                "keycode": keycode,
                "displayPower": display_power,
                "activeInput": active_input,
                "targetInput": target_hw,
                "message": (
                    f"Pre-staged Main TV ({'HDMI 1' if is_hdmi1 else 'HDMI 2'})"
                    if rc == 0
                    else (err or out).strip()
                ),
            }
        finally:
            if hasattr(self.adb, "disconnect_serial"):
                try:
                    self.adb.disconnect_serial(ser)
                except Exception:
                    pass

    def _launch_cube_youtube_music(self, query_override: str | None = None) -> dict[str, Any]:
        cfg = self.get_config()
        ser = self._adb_serial(cfg.cube_host, cfg.cube_adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "error": "Fire TV Cube IP or ADB missing"}
        st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            return {"ok": False, "state": st, "error": f"Cube ADB {st}: {detail}"}
        self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
        q_over = str(query_override or "").strip()
        if not q_over and cfg.open_gym_music_url:
            yt_url = normalize_youtube_tv_url(cfg.open_gym_music_url)
            shell_cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(yt_url)} com.amazon.firetv.youtube"
            rc, out, err = self.adb._run(["-s", ser, "shell", shell_cmd], timeout=6.0)
            return {
                "ok": rc == 0,
                "mode": "url",
                "url": yt_url,
                "message": "Launched YouTube playlist on Cube" if rc == 0 else (err or out).strip(),
            }
        query = q_over or cfg.open_gym_music_query or DEFAULT_OPEN_GYM_MUSIC_QUERY
        yt_url = (
            resolve_youtube_music_url(query, http_get=self._http_get)
            if (self._allow_network or self._http_get is not None)
            else ""
        )
        if yt_url:
            shell_cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(yt_url)} com.amazon.firetv.youtube"
            rc, out, err = self.adb._run(["-s", ser, "shell", shell_cmd], timeout=6.0)
            return {
                "ok": rc == 0,
                "mode": "mix_url",
                "query": query,
                "url": yt_url,
                "message": f"Playing YouTube mix ({query}) on Cube" if rc == 0 else (err or out).strip(),
            }
        shell_cmd = (
            f"am start -a android.media.action.MEDIA_PLAY_FROM_SEARCH -d 'youtube://search' "
            f"-e query {shlex.quote(query)} com.amazon.firetv.youtube"
        )
        rc, out, err = self.adb._run(["-s", ser, "shell", shell_cmd], timeout=6.0)
        return {
            "ok": rc == 0,
            "mode": "search",
            "query": query,
            "message": f"Launched YouTube search ({query}) on Cube" if rc == 0 else (err or out).strip(),
        }

    def _cube_media_keyevent(self, keycode: str, label: str) -> dict[str, Any]:
        cfg = self.get_config()
        ser = self._adb_serial(cfg.cube_host, cfg.cube_adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "skipped": True, "error": "Fire TV Cube IP or ADB missing"}
        st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            return {"ok": False, "state": st, "error": f"Cube ADB {st}: {detail}"}
        rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", keycode], timeout=4.0)
        return {
            "ok": rc == 0,
            "keycode": keycode,
            "message": label if rc == 0 else (err or out).strip(),
        }

    def _pause_cube_media(self) -> dict[str, Any]:
        return self._cube_media_keyevent("127", "Paused Fire TV Cube media")

    def _control_cube(self, action: str) -> dict[str, Any]:
        cfg = self.get_config()
        ser = self._adb_serial(cfg.cube_host, cfg.cube_adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "target": "cube", "action": action, "error": "Fire TV Cube IP or ADB missing"}
        st, detail = self.adb._connect_and_status(ser)
        if st != "device":
            return {"ok": False, "target": "cube", "action": action, "state": st, "error": f"Cube ADB {st}: {detail}"}

        if action == "on":
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "cube",
                "action": "on",
                "serial": ser,
                "message": "Fire TV Cube woken (discrete KEYCODE_WAKEUP)" if rc == 0 else (err or out).strip(),
            }

        if action == "off":
            self.adb._run(["-s", ser, "shell", "input", "keyevent", "127"], timeout=4.0)
            self._sleep(0.15)
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "223"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "cube",
                "action": "off",
                "serial": ser,
                "message": "Fire TV Cube put to sleep (discrete KEYCODE_SLEEP)" if rc == 0 else (err or out).strip(),
            }

        if action == "home":
            self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            self._sleep(0.2)
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "3"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "cube",
                "action": "home",
                "serial": ser,
                "message": "Fire TV Cube Home (KEYCODE_HOME)" if rc == 0 else (err or out).strip(),
            }

        return {"ok": False, "target": "cube", "action": action, "error": f"Unsupported Cube action: {action}"}

    def _control_music(self, action: str) -> dict[str, Any]:
        raw_act = str(action or "").strip()
        low = raw_act.lower()
        if low == "play" or low.startswith("play:"):
            q_over = raw_act.split(":", 1)[1].strip() if ":" in raw_act else ""
            if self.denon is not None:
                self._control_avr("on")
                self.denon.send_command("MSMCH STEREO")
                self.sync_denon_monitor_for_spin()
            res = self._launch_cube_youtube_music(query_override=q_over or None)
            return {**res, "target": "music", "action": raw_act}
        key_map = {
            "pause": ("127", "Music paused"),
            "resume": ("126", "Resuming music"),
            "next": ("87", "Next track"),
            "prev": ("88", "Previous track"),
        }
        if low in key_map:
            kc, msg = key_map[low]
            res = self._cube_media_keyevent(kc, msg)
            return {**res, "target": "music", "action": low}
        return {"ok": False, "target": "music", "action": raw_act, "error": f"Unsupported music action: {raw_act}"}

    def get_roku_info(self, host: str, port: int = 8060) -> dict[str, Any]:
        clean = str(host or "").strip()
        if not clean:
            return {"ok": False, "unconfigured": True, "error": "SmartSpin TV IP not set"}
        try:
            code, raw = self._do_get(f"http://{clean}:{int(port)}/query/device-info", 1.2)
            if code >= 400:
                res = {"ok": False, "host": clean, "error": f"HTTP {code}"}
            else:
                res = {**parse_roku_device_info(raw.decode("utf-8", errors="ignore")), "host": clean, "port": int(port)}
        except Exception as exc:
            res = {"ok": False, "host": clean, "error": str(exc)}
        self.last_spin_info = res
        return res

    def get_roku_active_app(self, host: str, port: int = 8060) -> dict[str, Any]:
        """Query Roku ECP `/query/active-app` to inspect the active HDMI/app ID."""
        clean = str(host or "").strip()
        if not clean:
            return {"ok": False, "unconfigured": True, "error": "SmartSpin TV IP not set"}
        try:
            code, raw = self._do_get(f"http://{clean}:{int(port)}/query/active-app", 1.2)
            if code >= 400:
                return {"ok": False, "host": clean, "error": f"HTTP {code}"}
            return {**parse_roku_active_app(raw.decode("utf-8", errors="ignore")), "host": clean, "port": int(port)}
        except Exception as exc:
            return {"ok": False, "host": clean, "error": str(exc)}

    def discover_roku_tvs(self, subnet_prefix: str = "192.168.1") -> list[dict[str, Any]]:
        """Fast LAN scan of `:8060/query/device-info` to discover Roku TVs (e.g. SmartSpin TV)."""
        if not self._allow_network and self._http_get is None:
            return []

        def _probe(ip: str) -> dict[str, Any] | None:
            if self._http_get is None:
                sock = socket.socket()
                sock.settimeout(0.25)
                try:
                    sock.connect((ip, 8060))
                except Exception:
                    return None
                finally:
                    sock.close()
            info = self.get_roku_info(ip, 8060)
            return info if info.get("ok") else None

        ips = [f"{subnet_prefix}.{n}" for n in range(1, 255)]
        found: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            for item in ex.map(_probe, ips):
                if item:
                    found.append(item)
        return found

    def _control_spin(self, action: str, *, sync_denon: bool = True) -> dict[str, Any]:
        cfg = self.get_config().spin
        if not cfg.enabled:
            return {"ok": False, "target": "spin", "action": action, "error": "SmartSpin TV disabled"}
        host = str(cfg.host or "").strip()
        if not host:
            return {
                "ok": False,
                "target": "spin",
                "action": action,
                "error": "SmartSpin TV IP not configured yet. Click Find Roku on :8766/.",
            }
        base = f"http://{host}:{int(cfg.ecp_port)}"
        if action == "on":
            if cfg.mac and self._wol_sender is not None:
                self._wol_sender(cfg.mac)
            try:
                code, _ = self._do_post(f"{base}/keypress/PowerOn", b"", {}, 2.0)
                if code >= 400:
                    return {"ok": False, "target": "spin", "action": "on", "error": f"HTTP {code}"}
                if cfg.hdmi_input:
                    self._sleep(0.5)
                    self._do_post(f"{base}/keypress/{urllib.parse.quote(cfg.hdmi_input, safe='')}", b"", {}, 2.0)
                denon_res = (
                    self.sync_denon_monitor_for_spin(spin_expected_on=True)
                    if sync_denon
                    else {"ok": True, "skipped": True, "reason": "Skipped Denon monitor sync"}
                )
                return {
                    "ok": True,
                    "target": "spin",
                    "action": "on",
                    "denonMonitor": denon_res,
                    "message": f"SmartSpin TV ON ({cfg.hdmi_input or 'PowerOn'})",
                }
            except Exception as exc:
                return {"ok": False, "target": "spin", "action": "on", "error": str(exc)}

        if action == "off":
            try:
                code, _ = self._do_post(f"{base}/keypress/PowerOff", b"", {}, 2.0)
                if code >= 400:
                    return {"ok": False, "target": "spin", "action": "off", "error": f"HTTP {code}"}
                denon_res = (
                    self.sync_denon_monitor_for_spin(spin_expected_on=False)
                    if sync_denon
                    else {"ok": True, "skipped": True, "reason": "Skipped during gym shutdown"}
                )
                return {
                    "ok": True,
                    "target": "spin",
                    "action": "off",
                    "denonMonitor": denon_res,
                    "message": "SmartSpin TV OFF",
                }
            except Exception as exc:
                return {"ok": False, "target": "spin", "action": "off", "error": str(exc)}

        return {"ok": False, "target": "spin", "action": action, "error": f"Unsupported action: {action}"}

    def ensure_spin_hdmi_ready(self) -> dict[str, Any]:
        """Ensure TCL SmartSpin Roku TV is powered ON and on its Denon HDMI input without flipping Denon."""
        cfg = self.get_config().spin
        if not cfg.enabled:
            return {"ok": False, "target": "spin", "error": "SmartSpin TV disabled"}
        host = str(cfg.host or "").strip()
        if not host:
            return {"ok": True, "target": "spin", "skipped": True, "alreadyReady": True, "switched": False}

        m_hdmi = re.search(r"hdmi\s*([1-4])", cfg.hdmi_input or "", re.I)
        expected_id = f"tvinput.hdmi{m_hdmi.group(1)}" if m_hdmi else "tvinput.hdmi1"

        info = self.get_roku_info(host, cfg.ecp_port)
        app = self.get_roku_active_app(host, cfg.ecp_port)
        power_mode = str(info.get("powerMode") or "")
        active_id = str(app.get("appId") or "")
        if info.get("ok") and power_mode == "PowerOn" and app.get("ok") and active_id == expected_id:
            return {
                "ok": True,
                "target": "spin",
                "alreadyReady": True,
                "switched": False,
                "powerMode": power_mode,
                "activeAppId": active_id,
                "message": f"SmartSpin TV already ON on {active_id}",
            }

        base = f"http://{host}:{int(cfg.ecp_port)}"
        if cfg.mac and self._wol_sender is not None and not info.get("ok"):
            self._wol_sender(cfg.mac)
            self._sleep(0.5)
        try:
            switched = False
            if power_mode != "PowerOn":
                code, _ = self._do_post(f"{base}/keypress/PowerOn", b"", {}, 2.0)
                if code >= 400:
                    return {"ok": False, "target": "spin", "error": f"HTTP {code}"}
                switched = True
            if cfg.hdmi_input and active_id != expected_id:
                self._sleep(0.5 if switched else 0.1)
                self._do_post(f"{base}/keypress/{urllib.parse.quote(cfg.hdmi_input, safe='')}", b"", {}, 2.0)
                switched = True
            return {
                "ok": True,
                "target": "spin",
                "alreadyReady": False,
                "switched": switched,
                "expectedAppId": expected_id,
                "previousAppId": active_id,
                "message": f"Pre-staged SmartSpin TV ({cfg.hdmi_input or 'PowerOn'})",
            }
        except Exception as exc:
            return {"ok": False, "target": "spin", "error": str(exc)}

    def _control_avr(self, action: str) -> dict[str, Any]:
        if self.denon is None:
            return {"ok": False, "target": "avr", "action": action, "error": "Denon client unavailable"}
        if action == "on":
            r1 = self.denon.send_command("PWON")
            if r1.get("ok"):
                self._sleep(0.25)
                self.denon.send_command("SIMPLAY")
            return {**r1, "target": "avr", "action": "on", "message": "Denon AVR powered on (Media Player)"}
        if action == "off":
            r1 = self.denon.send_command("PWSTANDBY")
            return {**r1, "target": "avr", "action": "off", "message": "Denon AVR standby"}
        if action == "mch_stereo":
            r1 = self.denon.send_command("MSMCH STEREO")
            return {**r1, "target": "avr", "action": "mch_stereo", "message": "Denon Multi Ch Stereo"}
        if action == "monitor1":
            return {**self.set_denon_monitor_out("1"), "target": "avr", "action": "monitor1"}
        if action == "monitor_auto":
            return {**self.set_denon_monitor_out("AUTO"), "target": "avr", "action": "monitor_auto"}
        if action == "ip_control_on":
            return {**self.ensure_denon_ip_control_always_on(force=True), "target": "avr", "action": "ip_control_on"}
        return {"ok": False, "target": "avr", "action": action, "error": f"Unsupported AVR action: {action}"}

    def execute_many(self, targets: Sequence[str], action: str, *, skip_unconfigured_spin: bool = False) -> dict[str, Any]:
        """Run a discrete action across multiple targets in order and aggregate results."""
        act = str(action or "").strip().lower()
        ordered: list[str] = []
        for raw_t in targets:
            t = str(raw_t or "").strip().lower()
            if t == "primary":
                t = "tv"
            elif t in ("firetv", "fire_tv", "fire_tv_cube"):
                t = "cube"
            if t and t not in ordered:
                ordered.append(t)
        if not ordered:
            return {"ok": False, "action": act, "error": "No targets specified"}

        results: dict[str, Any] = {}
        ok_any = False
        for t in ordered:
            if t == "spin" and skip_unconfigured_spin and not self.get_config().spin.host:
                results["spin"] = {"ok": True, "skipped": True, "reason": "SmartSpin TV IP not set"}
                continue
            r = self.execute(t, act)
            results[t] = r
            if r.get("ok"):
                ok_any = True

        label = _format_multi_speech(ordered, act) if act in ("on", "off") else f"{', '.join(ordered)} {act}"
        return {"ok": ok_any, "target": "multi", "targets": ordered, "action": act, "results": results, "message": label}

    def execute(self, target: str, action: str) -> dict[str, Any]:
        """Run a discrete action against `tv`/`primary`, `spin`, `wallboard`, `avr`, `all_tvs`, `open_gym`, `close_gym`, or `gym`."""
        tgt = str(target or "").strip().lower()
        act = str(action or "").strip().lower()

        if tgt in ("primary", "tv"):
            return self._control_primary(act)
        if tgt in ("cube", "firetv", "fire_tv", "fire_tv_cube"):
            return self._control_cube(act)
        if tgt == "spin":
            return self._control_spin(act)
        if tgt == "avr":
            return self._control_avr(act)
        if tgt == "music":
            return self._control_music(str(action or "").strip())
        if tgt == "wallboard":
            if act == "on":
                if self._wake_wallboard_fn is None:
                    return {"ok": False, "target": "wallboard", "action": "on", "error": "Wallboard wake unavailable"}
                res = self._wake_wallboard_fn(True)
                ok = bool(res.get("ok", True))
                out = {"ok": ok, "target": "wallboard", "action": "on", "adb": res, "message": res.get("message", "Wallboard wake sent")}
                if not ok:
                    out["error"] = res.get("message") or res.get("error") or "Wallboard wake failed"
                return out
            if act == "off":
                if self._sleep_wallboard_fn is None:
                    return {"ok": False, "target": "wallboard", "action": "off", "error": "Wallboard sleep unavailable"}
                res = self._sleep_wallboard_fn()
                ok = bool(res.get("ok", True))
                out = {"ok": ok, "target": "wallboard", "action": "off", "adb": res, "message": res.get("message", "Wallboard sleep sent")}
                if not ok:
                    out["error"] = res.get("message") or res.get("error") or "Wallboard sleep failed"
                return out
            return {"ok": False, "target": "wallboard", "action": act, "error": f"Unsupported wallboard action: {act}"}
        if tgt == "all_tvs":
            return self.execute_many(["tv", "wallboard", "spin"], act, skip_unconfigured_spin=True)
        if tgt == "open_gym":
            cfg = self.get_config()
            results: dict[str, Any] = {}

            def _open_avr_chain() -> dict[str, Any]:
                out: dict[str, Any] = {"avr": self._control_avr("on")}
                if self.denon is not None:
                    out["avr_mode"] = self.denon.send_command("MSMCH STEREO")
                    if cfg.spin.enabled and cfg.spin.host:
                        out["avr_monitor"] = self.set_denon_monitor_out("AUTO")
                    else:
                        out["avr_monitor"] = self.sync_denon_monitor_for_spin()
                return out

            open_tasks: dict[str, Callable[[], dict[str, Any]]] = {
                "tv": lambda: {"tv": self.ensure_primary_hdmi_ready("hdmi2")},
                "avr_chain": _open_avr_chain,
            }
            if cfg.spin.enabled and cfg.spin.host:
                open_tasks["spin"] = lambda: {"spin": self.ensure_spin_hdmi_ready()}
            if self._wake_wallboard_fn is not None:
                wake_fn = self._wake_wallboard_fn
                open_tasks["wallboard"] = lambda: {"wallboard": wake_fn(True)}
            if cfg.open_gym_play_music:
                open_tasks["music"] = lambda: {"music": self._launch_cube_youtube_music()}
            elif cfg.cube_host:
                open_tasks["cube"] = lambda: {"cube": self._control_cube("on")}

            pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(open_tasks))
            try:
                futs = {key: pool.submit(fn) for key, fn in open_tasks.items()}
                for key, fut in futs.items():
                    try:
                        results.update(fut.result(timeout=10.0))
                    except Exception as exc:
                        err_key = "avr" if key == "avr_chain" else key
                        results[err_key] = {"ok": False, "target": err_key, "error": str(exc)}
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            ok = any(bool(r.get("ok")) for r in results.values() if isinstance(r, dict))
            return {"ok": ok, "target": "open_gym", "action": "run", "results": results, "message": "Gym opened (All 3 TVs, Denon, YouTube Music)"}
        if tgt == "close_gym":
            results = {}
            close_tasks: dict[str, Callable[[], dict[str, Any]]] = {
                "cube": lambda: self._control_cube("off"),
                "tv": lambda: self._control_primary("off"),
                "avr": lambda: self._control_avr("off"),
            }
            if self._sleep_wallboard_fn is not None:
                sleep_fn = self._sleep_wallboard_fn
                close_tasks["wallboard"] = sleep_fn
            if self.get_config().spin.host:
                close_tasks["spin"] = lambda: self._control_spin("off", sync_denon=False)

            pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(close_tasks))
            try:
                futs = {key: pool.submit(fn) for key, fn in close_tasks.items()}
                for key, fut in futs.items():
                    try:
                        results[key] = fut.result(timeout=10.0)
                    except Exception as exc:
                        results[key] = {"ok": False, "target": key, "error": str(exc)}
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            results["media_pause"] = results["cube"]
            ok = any(bool(r.get("ok")) for r in results.values() if isinstance(r, dict) and not r.get("skipped"))
            return {"ok": ok, "target": "close_gym", "action": "run", "results": results, "message": "Gym closed"}
        if tgt == "gym":
            if act == "on":
                return self.execute("open_gym", "run")
            if act == "off":
                return self.execute("close_gym", "run")
            return {"ok": False, "target": "gym", "action": act, "error": f"Unsupported gym action: {act}"}

        return {"ok": False, "target": tgt, "action": act, "error": f"Unknown target: {tgt}"}

    def status_dict(self, *, live_probe: bool = False) -> dict[str, Any]:
        cfg = self.get_config()
        spin_info = self.get_roku_info(cfg.spin.host, cfg.spin.ecp_port) if (live_probe and cfg.spin.host) else (
            {"ok": False, "unconfigured": True, "error": "SmartSpin TV IP not set"} if not cfg.spin.host else self.last_spin_info
        )
        return {
            "config": cfg.to_dict(),
            "primary": {
                "host": cfg.primary.host,
                "mac": cfg.primary.mac,
                "probe": self.last_primary_probe,
            },
            "cube": {
                "host": cfg.cube_host,
                "adbPort": int(cfg.cube_adb_port),
                "probe": self.last_cube_probe,
            },
            "spin": spin_info,
            "denonIpControl": self.get_denon_ip_control_state(),
        }
