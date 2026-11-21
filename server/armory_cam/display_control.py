"""Unified discrete IP & voice controller for The Armory gym displays and Denon AVR.

Controls:
  - Armory Primary Display (Hisense 43E6QF Fire TV, ADB + discrete KEYCODE_WAKEUP/SLEEP + HDMI intents)
  - SmartSpin Display (TCL Roku TV, discrete ECP PowerOn/PowerOff + auto Denon Monitor 1/Dual coordination)
  - Armory Wallboard (32" Fire TV Edition, delegated to FireTvAdbController)
  - Denon AVR-3313CI (discrete PWON / PWSTANDBY / Monitor Out 1 vs Auto Dual)
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import re
import socket
import threading
import time
from typing import Any, Callable
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


DEFAULT_PRIMARY_HOST = "192.168.1.230"
DEFAULT_PRIMARY_MAC = "bc:09:b9:e7:35:26"
DEFAULT_HDMI_OPTIPLEX_INPUT = "com.mediatek.tis/.HdmiInputService/HW2"
DEFAULT_HDMI_DENON_INPUT = "com.mediatek.tis/.HdmiInputService/HW3"


@dataclass
class PrimaryDisplayConfig:
    """Configuration for Armory Primary Display (Hisense 43E6QF Fire TV)."""

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
    """Configuration for SmartSpin Display (TCL Roku TV)."""

    enabled: bool = True
    host: str = ""
    mac: str = ""
    ecp_port: int = 8060
    hdmi_input: str = "InputHDMI1"
    auto_denon_dual: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "host": self.host,
            "mac": self.mac,
            "ecpPort": int(self.ecp_port),
            "hdmiInput": self.hdmi_input,
            "autoDenonDual": bool(self.auto_denon_dual),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SpinDisplayConfig":
        if not isinstance(raw, dict):
            return cls()
        try:
            ecp_port = max(1, min(65535, int(raw.get("ecpPort", 8060))))
        except (TypeError, ValueError):
            ecp_port = 8060
        return cls(
            enabled=bool(raw.get("enabled", True)),
            host=str(raw.get("host", "") or "").strip(),
            mac=str(raw.get("mac", "") or "").strip(),
            ecp_port=ecp_port,
            hdmi_input=str(raw.get("hdmiInput", "InputHDMI1") or "InputHDMI1").strip(),
            auto_denon_dual=bool(raw.get("autoDenonDual", True)),
        )


@dataclass
class GymDisplaysConfig:
    """Persistent configuration for Armory Primary & SmartSpin gym displays."""

    primary: PrimaryDisplayConfig = field(default_factory=PrimaryDisplayConfig)
    spin: SpinDisplayConfig = field(default_factory=SpinDisplayConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict(),
            "spin": self.spin.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "GymDisplaysConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            primary=PrimaryDisplayConfig.from_dict(raw.get("primary")),
            spin=SpinDisplayConfig.from_dict(raw.get("spin")),
        )


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


def _strip_wake_prefix(text: str, wake_word: str = "armory") -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    wake_raw = (wake_word or "armory").strip().lower() or "armory"
    if wake_raw == "armory":
        alts = ["armory fit", "armoryfit", "armourys", "armorys", "armoury", "armory", "amory"]
    else:
        alts = [wake_raw]
    pat = r"^(?:" + "|".join(re.escape(a) for a in alts) + r")(?:\s+|$)"
    return re.sub(pat, "", s).strip()


def parse_display_voice_command(transcript: str, wake_word: str = "armory") -> dict[str, str] | None:
    """Parse natural-language display/TV/AVR/HDMI voice commands without matching gym lift logs."""
    cmd = _strip_wake_prefix(transcript, wake_word=wake_word)
    if not cmd:
        return None

    # Ignore lifting/cardio cues outright if there are no display/TV/AVR/HDMI words
    if not re.search(
        r"\b(display|tv|tvs|screen|screens|hisense|primary|spin|smartspin|smart\s+spin|tcl|roku|bike|wallboard|wall\s+board|toshiba|denon|avr|stereo|receiver|speakers|hdmi|optiplex|pc|computer|desktop|fire\s*tv|cube|gym|everything)\b",
        cmd,
    ):
        return None

    # 1. Explicit HDMI / input switching on Primary Display (Hisense)
    if re.search(r"\b(hdmi\s*1|hdmi\s*one|optiplex|pc|computer|desktop)\b", cmd) and re.search(
        r"\b(switch|set|change|put|show|go|to|input|hdmi|primary|hisense|tv|display)\b", cmd
    ):
        return {"target": "primary", "action": "hdmi1", "speech": "Primary display on PC"}

    if re.search(r"\b(hdmi\s*2|hdmi\s*two|fire\s*tv|cube|denon)\b", cmd) and re.search(
        r"\b(switch|set|change|put|show|go|to|input|hdmi)\b", cmd
    ):
        return {"target": "primary", "action": "hdmi2", "speech": "Primary display on Fire TV"}

    # 2. Denon Multi-Ch Stereo command
    if re.search(r"\b(denon|avr|receiver|stereo)\b", cmd) and re.search(r"\b(multi\s*channel|multi\s*ch|all\s*speakers)\b", cmd):
        return {"target": "avr", "action": "mch_stereo", "speech": "Denon multi channel stereo"}

    # 3. Determine power action (on vs off)
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

    # 4. Determine display target
    if re.search(r"\b(all\s+(?:tvs|displays|screens)|armory\s+tvs|gym|everything)\b", cmd):
        return {
            "target": "gym",
            "action": action,
            "speech": "Gym displays on" if action == "on" else "Gym displays off",
        }
    if re.search(r"\b(wall\s*board|32\s*(?:inch)?\s*tv|toshiba)\b", cmd):
        return {
            "target": "wallboard",
            "action": action,
            "speech": "Wallboard on" if action == "on" else "Wallboard off",
        }
    if re.search(r"\b(smart\s*spin|spin(?:\s+bike)?(?:\s+(?:tv|display|screen))?|tcl|roku|bike\s+(?:tv|display|screen))\b", cmd):
        return {
            "target": "spin",
            "action": action,
            "speech": "SmartSpin display on" if action == "on" else "SmartSpin display off",
        }
    if re.search(r"\b(denon|avr|stereo|receiver|speakers)\b", cmd):
        return {
            "target": "avr",
            "action": action,
            "speech": "Denon on" if action == "on" else "Denon off",
        }
    if re.search(r"\b(primary(?:\s+(?:display|tv|screen))?|hisense|main\s+(?:tv|display|screen)|(?:the\s+)?tv)\b", cmd):
        return {
            "target": "primary",
            "action": action,
            "speech": "Primary display on" if action == "on" else "Primary display off",
        }
    return None


class GymDisplayController:
    """Discrete power/input controller across Armory Primary, SmartSpin Roku, Wallboard & Denon AVR."""

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
        self.last_spin_info: dict[str, Any] = {}

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

        # Primary: Denon AVR-3313CI web setup page preserves every unrelated field
        try:
            code, raw = self._do_get(f"{base}/SETUP/VIDEO/d_video.asp", 3.5)
            if code < 400:
                fields = _parse_denon_video_form(raw.decode("utf-8", errors="ignore"))
                if "radioMonitorOut" in fields:
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

        # Fallback: direct Telnet/HTTP command alias (used on AVR-X/S models or unit tests)
        cmd = "VSMONIAUTO" if m == "AUTO" else f"VSMONI{m}"
        res = self.denon.send_command(cmd)
        return {**res, "monitorOut": m, "via": "direct"}

    def probe_primary(self) -> dict[str, Any]:
        cfg = self.get_config().primary
        ser = self._adb_serial(cfg.host, cfg.adb_port)
        if not ser:
            return {"ok": False, "state": "unconfigured", "message": "Primary Display IP not set."}
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

    def _control_primary(self, action: str) -> dict[str, Any]:
        cfg = self.get_config().primary
        if not cfg.enabled:
            return {"ok": False, "target": "primary", "action": action, "error": "Primary display disabled"}
        ser = self._adb_serial(cfg.host, cfg.adb_port)
        if not ser or self.adb is None:
            return {"ok": False, "target": "primary", "action": action, "error": "Primary display IP or ADB missing"}

        if action == "on":
            st, detail = self.adb._connect_and_status(ser)
            if st != "device" and cfg.mac and self._wol_sender is not None:
                self._wol_sender(cfg.mac)
                self._sleep(0.8)
                st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "primary", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "primary",
                "action": "on",
                "serial": ser,
                "message": "Primary Display woken (discrete KEYCODE_WAKEUP)" if rc == 0 else (err or out).strip(),
            }

        if action == "off":
            st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "primary", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            rc, out, err = self.adb._run(["-s", ser, "shell", "input", "keyevent", "223"], timeout=4.0)
            return {
                "ok": rc == 0,
                "target": "primary",
                "action": "off",
                "serial": ser,
                "message": "Primary Display put to sleep (discrete KEYCODE_SLEEP)" if rc == 0 else (err or out).strip(),
            }

        if action in ("hdmi1", "pc", "hdmi2", "firetv"):
            input_id = cfg.hdmi_optiplex_input if action in ("hdmi1", "pc") else cfg.hdmi_denon_input
            if not input_id:
                return {"ok": False, "target": "primary", "action": action, "error": "HDMI input ID not configured"}
            st, detail = self.adb._connect_and_status(ser)
            if st != "device" and cfg.mac and self._wol_sender is not None:
                self._wol_sender(cfg.mac)
                self._sleep(0.8)
                st, detail = self.adb._connect_and_status(ser)
            if st != "device":
                return {"ok": False, "target": "primary", "action": action, "state": st, "error": f"ADB {st}: {detail}"}
            self.adb._run(["-s", ser, "shell", "input", "keyevent", "224"], timeout=4.0)
            uri = f"content://android.media.tv/passthrough/{urllib.parse.quote(input_id, safe='')}"
            rc, out, err = self.adb._run(
                ["-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri],
                timeout=5.0,
            )
            return {
                "ok": rc == 0,
                "target": "primary",
                "action": action,
                "inputId": input_id,
                "message": f"Switched Primary Display to {input_id}" if rc == 0 else (err or out).strip(),
            }

        return {"ok": False, "target": "primary", "action": action, "error": f"Unsupported action: {action}"}

    def get_roku_info(self, host: str, port: int = 8060) -> dict[str, Any]:
        clean = str(host or "").strip()
        if not clean:
            return {"ok": False, "unconfigured": True, "error": "SmartSpin Roku IP not set"}
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

    def discover_roku_tvs(self, subnet_prefix: str = "192.168.1") -> list[dict[str, Any]]:
        """Fast LAN scan of `:8060/query/device-info` to discover Roku TVs (e.g. SmartSpin TCL)."""
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

    def _control_spin(self, action: str) -> dict[str, Any]:
        cfg = self.get_config().spin
        if not cfg.enabled:
            return {"ok": False, "target": "spin", "action": action, "error": "SmartSpin display disabled"}
        host = str(cfg.host or "").strip()
        if not host:
            return {
                "ok": False,
                "target": "spin",
                "action": action,
                "error": "SmartSpin Roku IP not configured yet. Click Find Roku on :8766/.",
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
                denon_res = self.set_denon_monitor_out("AUTO") if (cfg.auto_denon_dual and self.denon is not None) else None
                return {
                    "ok": True,
                    "target": "spin",
                    "action": "on",
                    "denonMonitor": denon_res,
                    "message": f"SmartSpin Roku ON ({cfg.hdmi_input or 'PowerOn'})",
                }
            except Exception as exc:
                return {"ok": False, "target": "spin", "action": "on", "error": str(exc)}

        if action == "off":
            try:
                code, _ = self._do_post(f"{base}/keypress/PowerOff", b"", {}, 2.0)
                if code >= 400:
                    return {"ok": False, "target": "spin", "action": "off", "error": f"HTTP {code}"}
                denon_res = self.set_denon_monitor_out("1") if (cfg.auto_denon_dual and self.denon is not None) else None
                return {
                    "ok": True,
                    "target": "spin",
                    "action": "off",
                    "denonMonitor": denon_res,
                    "message": "SmartSpin Roku OFF",
                }
            except Exception as exc:
                return {"ok": False, "target": "spin", "action": "off", "error": str(exc)}

        return {"ok": False, "target": "spin", "action": action, "error": f"Unsupported action: {action}"}

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
        return {"ok": False, "target": "avr", "action": action, "error": f"Unsupported AVR action: {action}"}

    def execute(self, target: str, action: str) -> dict[str, Any]:
        """Run a discrete action against `primary`, `spin`, `wallboard`, `avr`, or `gym`."""
        tgt = str(target or "").strip().lower()
        act = str(action or "").strip().lower()

        if tgt == "primary":
            return self._control_primary(act)
        if tgt == "spin":
            return self._control_spin(act)
        if tgt == "avr":
            return self._control_avr(act)
        if tgt == "wallboard":
            if act == "on":
                if self._wake_wallboard_fn is None:
                    return {"ok": False, "target": "wallboard", "action": "on", "error": "Wallboard wake unavailable"}
                res = self._wake_wallboard_fn(True)
                return {"ok": bool(res.get("ok", True)), "target": "wallboard", "action": "on", "adb": res, "message": res.get("message", "Wallboard wake sent")}
            if act == "off":
                if self._sleep_wallboard_fn is None:
                    return {"ok": False, "target": "wallboard", "action": "off", "error": "Wallboard sleep unavailable"}
                res = self._sleep_wallboard_fn()
                return {"ok": bool(res.get("ok", True)), "target": "wallboard", "action": "off", "adb": res, "message": res.get("message", "Wallboard sleep sent")}
            return {"ok": False, "target": "wallboard", "action": act, "error": f"Unsupported wallboard action: {act}"}
        if tgt == "gym":
            results: dict[str, Any] = {}
            if act == "on":
                results["primary"] = self._control_primary("on")
                results["avr"] = self._control_avr("on")
                ok = bool(results["primary"].get("ok") or results["avr"].get("ok"))
                return {"ok": ok, "target": "gym", "action": "on", "results": results, "message": "Gym Primary & Denon ON"}
            if act == "off":
                results["primary"] = self._control_primary("off")
                results["avr"] = self._control_avr("off")
                if self._sleep_wallboard_fn is not None:
                    results["wallboard"] = self._sleep_wallboard_fn()
                if self.get_config().spin.host:
                    results["spin"] = self._control_spin("off")
                ok = any(bool(r.get("ok")) for r in results.values() if isinstance(r, dict))
                return {"ok": ok, "target": "gym", "action": "off", "results": results, "message": "Gym displays & AVR OFF"}
            return {"ok": False, "target": "gym", "action": act, "error": f"Unsupported gym action: {act}"}

        return {"ok": False, "target": tgt, "action": act, "error": f"Unknown target: {tgt}"}

    def status_dict(self, *, live_probe: bool = False) -> dict[str, Any]:
        cfg = self.get_config()
        spin_info = self.get_roku_info(cfg.spin.host, cfg.spin.ecp_port) if (live_probe and cfg.spin.host) else (
            {"ok": False, "unconfigured": True, "error": "SmartSpin IP not set"} if not cfg.spin.host else self.last_spin_info
        )
        return {
            "config": cfg.to_dict(),
            "primary": {
                "host": cfg.primary.host,
                "mac": cfg.primary.mac,
                "probe": self.last_primary_probe,
            },
            "spin": spin_info,
        }
