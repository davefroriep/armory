"""Bluetooth chest-strap button (HID Volume Up/Down) global listener & state machine for OptiPlex.

Intercepts BLE HID consumer control / media buttons (`PICO V0.1` etc. sending Consumer Control
`0xE9`/`0xEA` and `hidserv`-synthesized `VK_VOLUME_UP`/`VK_VOLUME_DOWN`) globally on OptiPlex via:
  1. Windows Raw Input (`WM_INPUT` `RIM_TYPEHID`, Usage Page `0x000C` filtered to BLE HID / PICO),
     which captures the hardware HID report directly even across BLE sleep/wake transitions.
  2. Windows `WH_KEYBOARD_LL` hook (including `LLKHF_INJECTED` events from `hidserv`) so Windows
     system volume is swallowed cleanly and deduplicated against Raw Input.

All tap / double-tap / long-hold actions are configurable under either layout:
  - `"one_button"`: Both Vol+ and Vol- perform identical configurable `singleAction`,
    `doubleAction`, and `longAction` gestures (foolproof under-shirt feel).
  - `"two_button"`: Vol+ (`volUpSingle` / `volUpDouble`) and Vol- (`volDownSingle` /
    `volDownDouble`) are configured independently alongside `twoLongAction` (hold either).
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass
import logging
import queue
import sys
import threading
import time
from typing import Any, Callable

VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

HID_USAGE_PAGE_CONSUMER = 0x000C
HID_USAGE_CONSUMER_CONTROL = 0x0001
HID_USAGE_VOLUME_UP = 0x00E9
HID_USAGE_VOLUME_DOWN = 0x00EA
HIDP_INPUT = 0
HIDP_STATUS_SUCCESS = 0x00110000

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_INPUT = 0x00FF
WM_INPUT_DEVICE_CHANGE = 0x00FE
WM_QUIT = 0x0012
GIDC_REMOVAL = 2

RIM_TYPEHID = 2
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000

VALID_BT_LAYOUTS = ("one_button", "two_button")
VALID_BT_ACTIONS = ("check", "record", "voice", "undo", "none")

PICO_HID_HINTS = ("ffff114f976f", "020504", "00001812")


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        iv = int(round(float(v)))
    except Exception:
        return default
    return max(lo, min(hi, iv))


def _clean_action(v: Any, default: str) -> str:
    s = str(v or "").strip().lower()
    if s in VALID_BT_ACTIONS:
        return s
    return default


@dataclass
class BtButtonConfig:
    enabled: bool = True
    mode: str = "one_button"
    single_action: str = "check"
    double_action: str = "record"
    long_action: str = "voice"
    vol_up_single: str = "check"
    vol_up_double: str = "none"
    vol_down_single: str = "record"
    vol_down_double: str = "none"
    two_long_action: str = "voice"
    hold_ms: int = 450
    double_tap_ms: int = 380
    swallow_volume_keys: bool = True
    ble_keepalive: bool = False
    ble_mac: str = "FF:FF:11:4F:97:6F"

    def normalized(self) -> "BtButtonConfig":
        raw_mode = str(self.mode or "one_button").strip().lower()
        if raw_mode in ("split", "split_voice", "two_button"):
            m = "two_button"
        elif raw_mode in VALID_BT_LAYOUTS:
            m = raw_mode
        else:
            m = "one_button"
        return BtButtonConfig(
            enabled=bool(self.enabled),
            mode=m,
            single_action=_clean_action(self.single_action, "check"),
            double_action=_clean_action(self.double_action, "record"),
            long_action=_clean_action(self.long_action, "voice"),
            vol_up_single=_clean_action(self.vol_up_single, "check"),
            vol_up_double=_clean_action(self.vol_up_double, "none"),
            vol_down_single=_clean_action(self.vol_down_single, "record"),
            vol_down_double=_clean_action(self.vol_down_double, "none"),
            two_long_action=_clean_action(self.two_long_action, "voice"),
            hold_ms=_clamp_int(self.hold_ms, 200, 1500, 450),
            double_tap_ms=_clamp_int(self.double_tap_ms, 180, 700, 380),
            swallow_volume_keys=bool(self.swallow_volume_keys),
            ble_keepalive=bool(self.ble_keepalive),
            ble_mac=str(self.ble_mac or "FF:FF:11:4F:97:6F").strip().upper() or "FF:FF:11:4F:97:6F",
        )

    def to_dict(self) -> dict[str, Any]:
        c = self.normalized()
        return {
            "enabled": c.enabled,
            "mode": c.mode,
            "singleAction": c.single_action,
            "doubleAction": c.double_action,
            "longAction": c.long_action,
            "volUpSingle": c.vol_up_single,
            "volUpDouble": c.vol_up_double,
            "volDownSingle": c.vol_down_single,
            "volDownDouble": c.vol_down_double,
            "twoLongAction": c.two_long_action,
            "holdMs": c.hold_ms,
            "doubleTapMs": c.double_tap_ms,
            "swallowVolumeKeys": c.swallow_volume_keys,
            "bleKeepalive": c.ble_keepalive,
            "bleMac": c.ble_mac,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BtButtonConfig":
        if not isinstance(raw, dict):
            return cls().normalized()
        raw_mode = str(raw.get("mode", "one_button")).strip().lower()
        has_two_keys = any(
            k in raw for k in ("volUpSingle", "vol_up_single", "volDownSingle", "vol_down_single", "twoLongAction", "two_long_action")
        )
        has_one_keys = any(
            k in raw for k in ("singleAction", "single_action", "doubleAction", "double_action", "longAction", "long_action")
        )

        vol_up_single = raw.get("volUpSingle", raw.get("vol_up_single", "check"))
        vol_up_double = raw.get("volUpDouble", raw.get("vol_up_double", "none"))
        vol_down_single = raw.get("volDownSingle", raw.get("vol_down_single", "record"))
        vol_down_double = raw.get("volDownDouble", raw.get("vol_down_double", "none"))
        two_long_action = raw.get("twoLongAction", raw.get("two_long_action", "voice"))

        # Legacy preset migration
        if raw_mode == "split_voice" and not has_two_keys:
            vol_up_single = "check"
            vol_down_single = "voice"
            two_long_action = "record"

        single_action = raw.get("singleAction", raw.get("single_action", "check"))
        double_action = raw.get("doubleAction", raw.get("double_action", "record"))
        long_action = raw.get("longAction", raw.get("long_action", "voice"))
        if raw_mode == "one_button" and not has_one_keys:
            # First migration from old unconfigurable one_button -> use clean defaults unless caller sent per-gesture keys
            single_action = raw.get("singleAction", raw.get("single_action", "check"))
            double_action = raw.get("doubleAction", raw.get("double_action", "record"))
            long_action = raw.get("longAction", raw.get("long_action", "voice"))

        mode = "two_button" if raw_mode in ("split", "split_voice", "two_button") else "one_button"
        return cls(
            enabled=bool(raw.get("enabled", True)),
            mode=mode,
            single_action=str(single_action),
            double_action=str(double_action),
            long_action=str(long_action),
            vol_up_single=str(vol_up_single),
            vol_up_double=str(vol_up_double),
            vol_down_single=str(vol_down_single),
            vol_down_double=str(vol_down_double),
            two_long_action=str(two_long_action),
            hold_ms=_clamp_int(raw.get("holdMs", raw.get("hold_ms", 450)), 200, 1500, 450),
            double_tap_ms=_clamp_int(raw.get("doubleTapMs", raw.get("double_tap_ms", 380)), 180, 700, 380),
            swallow_volume_keys=bool(raw.get("swallowVolumeKeys", raw.get("swallow_volume_keys", True))),
            ble_keepalive=bool(raw.get("bleKeepalive", raw.get("ble_keepalive", False))),
            ble_mac=str(raw.get("bleMac", raw.get("ble_mac", "FF:FF:11:4F:97:6F")) or "FF:FF:11:4F:97:6F"),
        ).normalized()


def _default_scheduler(delay_sec: float, fn: Callable[[], None]) -> Callable[[], None]:
    t = threading.Timer(max(0.01, float(delay_sec)), fn)
    t.daemon = True
    t.start()
    return t.cancel


class BtButtonStateMachine:
    """Deterministic tap / double-tap / long-hold state machine for 2-key BLE HID buttons."""

    def __init__(
        self,
        config: BtButtonConfig | None = None,
        *,
        on_action: Callable[[str, dict[str, Any]], None],
        schedule_fn: Callable[[float, Callable[[], None]], Callable[[], None]] | None = None,
    ) -> None:
        self.cfg = (config or BtButtonConfig()).normalized()
        self._on_action = on_action
        self._schedule = schedule_fn or _default_scheduler
        self._lock = threading.RLock()

        self._pressed: dict[str, bool] = {"vol_up": False, "vol_down": False}
        self._down_at: dict[str, float] = {"vol_up": 0.0, "vol_down": 0.0}
        self._last_up_at: dict[str, float] = {"vol_up": -10.0, "vol_down": -10.0}
        self._hold_fired: dict[str, bool] = {"vol_up": False, "vol_down": False}
        self._hold_cancel: dict[str, Callable[[], None] | None] = {"vol_up": None, "vol_down": None}
        self._tap_pending_cancel: Callable[[], None] | None = None
        self._tap_pending_key: str = ""
        self._tap_pending_held_ms: int = 0
        self.press_count: int = 0
        self.last_event: dict[str, Any] | None = None

    def update_config(self, config: BtButtonConfig) -> None:
        with self._lock:
            self.cfg = config.normalized()

    def reset(self) -> None:
        with self._lock:
            for k in ("vol_up", "vol_down"):
                self._pressed[k] = False
                self._hold_fired[k] = False
                c = self._hold_cancel[k]
                self._hold_cancel[k] = None
                if callable(c):
                    try:
                        c()
                    except Exception:
                        pass
            if callable(self._tap_pending_cancel):
                try:
                    self._tap_pending_cancel()
                except Exception:
                    pass
            self._tap_pending_cancel = None
            self._tap_pending_key = ""
            self._tap_pending_held_ms = 0

    def _emit(self, action: str, *, key: str, gesture: str, held_ms: int = 0) -> None:
        if action not in ("check", "record", "voice", "undo"):
            return
        meta = {
            "key": key,
            "gesture": gesture,
            "action": action,
            "heldMs": int(max(0, held_ms)),
            "tsMs": int(time.time() * 1000),
            "mode": self.cfg.mode,
        }
        with self._lock:
            self.last_event = dict(meta)
        try:
            self._on_action(action, meta)
        except Exception:
            pass

    def _hold_action_for(self, _key: str) -> str:
        if self.cfg.mode == "two_button":
            return self.cfg.two_long_action
        return self.cfg.long_action

    def _single_action_for(self, key: str) -> str:
        if self.cfg.mode == "two_button":
            return self.cfg.vol_up_single if key == "vol_up" else self.cfg.vol_down_single
        return self.cfg.single_action

    def _double_action_for(self, key: str) -> str:
        if self.cfg.mode == "two_button":
            return self.cfg.vol_up_double if key == "vol_up" else self.cfg.vol_down_double
        return self.cfg.double_action

    def on_key_down(self, key: str, now_s: float | None = None) -> None:
        if key not in ("vol_up", "vol_down"):
            return
        t = time.monotonic() if now_s is None else float(now_s)
        with self._lock:
            if not self.cfg.enabled:
                return
            if self._pressed[key]:
                return  # Ignore OS key auto-repeat while held
            if (t - self._down_at[key]) < 0.035:
                return
            self._pressed[key] = True
            self._down_at[key] = t
            self._hold_fired[key] = False
            self.press_count += 1
            old_c = self._hold_cancel[key]
            if callable(old_c):
                try:
                    old_c()
                except Exception:
                    pass
                self._hold_cancel[key] = None
            hold_act = self._hold_action_for(key)
            if hold_act == "none":
                return
            hold_sec = float(self.cfg.hold_ms) / 1000.0

            def _on_hold_timeout() -> None:
                with self._lock:
                    if not self._pressed[key] or self._hold_fired[key]:
                        return
                    self._hold_fired[key] = True
                    self._hold_cancel[key] = None
                    if callable(self._tap_pending_cancel):
                        try:
                            self._tap_pending_cancel()
                        except Exception:
                            pass
                        self._tap_pending_cancel = None
                        self._tap_pending_key = ""
                    act = self._hold_action_for(key)
                    held = int(round(hold_sec * 1000.0))
                self._emit(act, key=key, gesture="hold", held_ms=held)

            self._hold_cancel[key] = self._schedule(hold_sec, _on_hold_timeout)

    def on_key_up(self, key: str, now_s: float | None = None) -> None:
        if key not in ("vol_up", "vol_down"):
            return
        t = time.monotonic() if now_s is None else float(now_s)
        emit_now: tuple[str, str, int] | None = None
        with self._lock:
            c = self._hold_cancel[key]
            self._hold_cancel[key] = None
            if callable(c):
                try:
                    c()
                except Exception:
                    pass
            was_pressed = self._pressed[key]
            was_hold_fired = self._hold_fired[key]
            held_ms = int(round(max(0.0, t - self._down_at[key]) * 1000.0)) if was_pressed else 0
            self._pressed[key] = False
            self._hold_fired[key] = False
            self._last_up_at[key] = t
            if not was_pressed or was_hold_fired or not self.cfg.enabled:
                return

            sgl_act = self._single_action_for(key)
            dbl_act = self._double_action_for(key)

            # In two-button mode, if a DIFFERENT key has a pending single-tap, flush that key first.
            if callable(self._tap_pending_cancel) and self.cfg.mode == "two_button" and self._tap_pending_key != key:
                prev_key = self._tap_pending_key
                prev_held = self._tap_pending_held_ms
                try:
                    self._tap_pending_cancel()
                except Exception:
                    pass
                self._tap_pending_cancel = None
                self._tap_pending_key = ""
                prev_act = self._single_action_for(prev_key)
                if prev_act != "none":
                    self._emit(prev_act, key=prev_key, gesture="tap", held_ms=prev_held)

            if dbl_act == "none":
                # Zero-delay fast single tap whenever double-tap is not mapped for this button
                if sgl_act != "none":
                    emit_now = (sgl_act, "tap", held_ms)
            else:
                if callable(self._tap_pending_cancel):
                    try:
                        self._tap_pending_cancel()
                    except Exception:
                        pass
                    self._tap_pending_cancel = None
                    self._tap_pending_key = ""
                    self._tap_pending_held_ms = 0
                    emit_now = (dbl_act, "double_tap", held_ms)
                else:
                    dt_sec = float(self.cfg.double_tap_ms) / 1000.0
                    self._tap_pending_key = key
                    self._tap_pending_held_ms = held_ms

                    def _on_single_tap() -> None:
                        with self._lock:
                            if self._tap_pending_cancel is None:
                                return
                            k_snap = self._tap_pending_key or key
                            h_snap = self._tap_pending_held_ms
                            self._tap_pending_cancel = None
                            self._tap_pending_key = ""
                            self._tap_pending_held_ms = 0
                            act = self._single_action_for(k_snap)
                        if act != "none":
                            self._emit(act, key=k_snap, gesture="tap", held_ms=h_snap)

                    self._tap_pending_cancel = self._schedule(dt_sec, _on_single_tap)

        if emit_now is not None:
            act, gest, h_ms = emit_now
            self._emit(act, key=key, gesture=gest, held_ms=h_ms)


if sys.platform == "win32":
    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wt.DWORD),
            ("scanCode", wt.DWORD),
            ("flags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", wt.USHORT),
            ("usUsage", wt.USHORT),
            ("dwFlags", wt.DWORD),
            ("hwndTarget", wt.HWND),
        ]

    class _RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", wt.DWORD),
            ("dwSize", wt.DWORD),
            ("hDevice", wt.HANDLE),
            ("wParam", wt.WPARAM),
        ]

    class _RAWHID_HEAD(ctypes.Structure):
        _fields_ = [
            ("dwSizeHid", wt.DWORD),
            ("dwCount", wt.DWORD),
        ]

    _LRESULT = ctypes.c_ssize_t
    _HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT),
            ("style", wt.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE),
            ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR),
            ("hIconSm", wt.HICON),
        ]


def is_pico_hid_device_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    return any(h in low for h in PICO_HID_HINTS)


class BtButtonListener:
    """Manages Windows Raw Input HID + low-level hook volume interception and action dispatch."""

    def __init__(
        self,
        *,
        config: BtButtonConfig | None = None,
        on_action: Callable[[str, dict[str, Any]], Any] | None = None,
        allow_hardware: bool = True,
    ) -> None:
        self.cfg = (config or BtButtonConfig()).normalized()
        self._on_action_cb = on_action
        self._allow_hardware = bool(allow_hardware and sys.platform == "win32")
        self.log = logging.getLogger("armory-bt-btn")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._queue: queue.Queue[tuple[str, bool, float, str] | None] = queue.Queue(maxsize=128)
        self._sm = BtButtonStateMachine(self.cfg, on_action=self._dispatch_action)
        self._hook_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._hook_tid: int = 0
        self._hook_handle: Any = None
        self._hook_proc: Any = None
        self._wnd_proc_ref: Any = None
        self._raw_hwnd: Any = None
        self._raw_devices: dict[int, dict[str, Any]] = {}
        self._raw_pressed_by_dev: dict[int, set[str]] = {}
        self._last_raw_event_at: dict[str, float] = {"vol_up": -10.0, "vol_down": -10.0}
        self._last_ll_seen_at: dict[str, float] = {"vol_up": -10.0, "vol_down": -10.0}
        self.hook_active: bool = False
        self.hook_error: str | None = None
        self.raw_input_active: bool = False
        self.raw_input_error: str | None = None
        self.last_source: str | None = None
        self.ble_keepalive_status: str = "off"
        self._started = False

    def _dispatch_action(self, action: str, meta: dict[str, Any]) -> None:
        self.log.info(
            "BT button %s (%s %s, %dms) -> %s",
            meta.get("key"),
            meta.get("gesture"),
            meta.get("mode"),
            int(meta.get("heldMs") or 0),
            action,
        )
        cb = self._on_action_cb
        if callable(cb):
            try:
                cb(action, meta)
            except Exception as exc:
                self.log.warning("BT button action %s failed: %s", action, exc)

    def simulate_action(self, action: str, *, key: str = "test") -> dict[str, Any]:
        clean = str(action or "").strip().lower()
        if clean not in ("check", "record", "voice", "undo"):
            raise ValueError(f"Unsupported button action '{action}'")
        meta = {
            "key": key,
            "gesture": "test",
            "action": clean,
            "heldMs": 0,
            "tsMs": int(time.time() * 1000),
            "mode": self.cfg.mode,
        }
        with self._lock:
            self._sm.last_event = dict(meta)
            self.last_source = "test"
        result: Any = None
        cb = self._on_action_cb
        if callable(cb):
            result = cb(clean, meta)
        return {"ok": True, "action": clean, "event": meta, "result": result}

    def update_config(self, patch: dict[str, Any] | None) -> BtButtonConfig:
        with self._lock:
            merged = {**self.cfg.to_dict(), **(patch if isinstance(patch, dict) else {})}
            self.cfg = BtButtonConfig.from_dict(merged)
            self._sm.update_config(self.cfg)
            if not self.cfg.enabled:
                self._sm.reset()
            if not self.cfg.ble_keepalive:
                self.ble_keepalive_status = "off"
            return self.cfg

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            last_ev = dict(self._sm.last_event) if isinstance(self._sm.last_event, dict) else None
            return {
                **self.cfg.to_dict(),
                "hardwareEnabled": self._allow_hardware,
                "hookActive": bool(self.hook_active),
                "hookError": self.hook_error,
                "rawInputActive": bool(self.raw_input_active),
                "rawInputError": self.raw_input_error,
                "lastSource": self.last_source,
                "bleKeepaliveStatus": self.ble_keepalive_status,
                "pressCount": int(self._sm.press_count),
                "lastEvent": last_ev,
            }

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="armory-bt-btn-worker", daemon=True)
        self._worker_thread.start()
        if self._allow_hardware:
            self._hook_thread = threading.Thread(target=self._hook_loop, name="armory-bt-btn-hook", daemon=True)
            self._hook_thread.start()
            self._keepalive_thread = threading.Thread(target=self._ble_keepalive_loop, name="armory-bt-ble-ka", daemon=True)
            self._keepalive_thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            tid = self._hook_tid
        self._sm.reset()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if tid and sys.platform == "win32":
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._hook_thread is not None:
            self._hook_thread.join(timeout=1.5)
            self._hook_thread = None
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=1.5)
            self._worker_thread = None
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=1.0)
            self._keepalive_thread = None

    def _enqueue_key(self, key: str, is_down: bool, now_s: float, source: str) -> None:
        with self._lock:
            self.last_source = source
        try:
            self._queue.put_nowait((key, is_down, now_s, source))
        except queue.Full:
            pass

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None or self._stop_event.is_set():
                break
            key, is_down, now_s, _source = item
            if is_down:
                self._sm.on_key_down(key, now_s)
            else:
                self._sm.on_key_up(key, now_s)

    def _ble_keepalive_loop(self) -> None:
        """Optional gentle GATT read ping while connected (note: coin-cell sleep firmware may still disconnect)."""
        while not self._stop_event.wait(20.0):
            cfg = self.cfg
            if not cfg.enabled or not cfg.ble_keepalive:
                with self._lock:
                    self.ble_keepalive_status = "off"
                continue
            try:
                import bleak  # type: ignore

                async def _ping() -> str:
                    async with bleak.BleakClient(cfg.ble_mac, timeout=4.0) as client:
                        if not client.is_connected:
                            return "asleep / disconnected"
                        try:
                            val = await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")
                            name = bytes(val).decode("utf-8", errors="ignore").strip() or "connected"
                            return f"alive ({name})"
                        except Exception as exc:
                            return f"connected ({type(exc).__name__})"

                res = asyncio.run(_ping())
            except Exception as exc:
                res = f"asleep ({type(exc).__name__})"
            with self._lock:
                self.ble_keepalive_status = res

    def _handle_raw_input(self, user32: Any, hid: Any, l_param: int) -> None:
        sz = wt.UINT(0)
        hdr_size = ctypes.sizeof(_RAWINPUTHEADER)
        user32.GetRawInputData(wt.HANDLE(l_param), RID_INPUT, None, ctypes.byref(sz), hdr_size)
        if sz.value < hdr_size:
            return
        buf = (ctypes.c_ubyte * int(sz.value))()
        got = user32.GetRawInputData(wt.HANDLE(l_param), RID_INPUT, buf, ctypes.byref(sz), hdr_size)
        if got == 0xFFFFFFFF or got < hdr_size:
            return
        hdr = ctypes.cast(buf, ctypes.POINTER(_RAWINPUTHEADER)).contents
        if int(hdr.dwType) != RIM_TYPEHID:
            return
        h_dev = int(hdr.hDevice or 0)
        if not h_dev:
            return
        dev_info = self._raw_devices.get(h_dev)
        if dev_info is None:
            dev_name = ""
            nchars = wt.UINT(0)
            user32.GetRawInputDeviceInfoW(hdr.hDevice, RIDI_DEVICENAME, None, ctypes.byref(nchars))
            if nchars.value > 0:
                nbuf = ctypes.create_unicode_buffer(int(nchars.value) + 2)
                if user32.GetRawInputDeviceInfoW(hdr.hDevice, RIDI_DEVICENAME, nbuf, ctypes.byref(nchars)) != 0xFFFFFFFF:
                    dev_name = nbuf.value
            is_pico = is_pico_hid_device_name(dev_name)
            pp_buf = None
            if is_pico:
                ppsz = wt.UINT(0)
                user32.GetRawInputDeviceInfoW(hdr.hDevice, RIDI_PREPARSEDDATA, None, ctypes.byref(ppsz))
                if ppsz.value > 0:
                    pp_buf = (ctypes.c_ubyte * int(ppsz.value))()
                    if user32.GetRawInputDeviceInfoW(hdr.hDevice, RIDI_PREPARSEDDATA, pp_buf, ctypes.byref(ppsz)) == 0xFFFFFFFF:
                        pp_buf = None
            dev_info = {"name": dev_name, "is_pico": is_pico, "preparsed": pp_buf}
            self._raw_devices[h_dev] = dev_info

        if not dev_info.get("is_pico"):
            return
        pp_buf = dev_info.get("preparsed")
        if pp_buf is None or int(got) < hdr_size + ctypes.sizeof(_RAWHID_HEAD):
            return

        raw_hid = ctypes.cast(ctypes.byref(buf, hdr_size), ctypes.POINTER(_RAWHID_HEAD)).contents
        rep_size = int(raw_hid.dwSizeHid)
        rep_cnt = int(raw_hid.dwCount)
        if rep_size <= 0 or rep_cnt <= 0:
            return
        rep_offset = hdr_size + ctypes.sizeof(_RAWHID_HEAD)
        now_s = time.monotonic()

        for i in range(rep_cnt):
            start = rep_offset + i * rep_size
            if start + rep_size > int(got):
                break
            usages = (wt.USHORT * 16)()
            usage_len = wt.ULONG(16)
            rep_ptr = ctypes.c_void_p(ctypes.addressof(buf) + start)
            status = hid.HidP_GetUsages(
                HIDP_INPUT,
                HID_USAGE_PAGE_CONSUMER,
                0,
                usages,
                ctypes.byref(usage_len),
                ctypes.c_void_p(ctypes.addressof(pp_buf)),
                rep_ptr,
                wt.ULONG(rep_size),
            )
            if int(status) != HIDP_STATUS_SUCCESS:
                continue
            active_now: set[str] = set()
            for u_idx in range(min(int(usage_len.value), 16)):
                u_val = int(usages[u_idx])
                if u_val == HID_USAGE_VOLUME_UP:
                    active_now.add("vol_up")
                elif u_val == HID_USAGE_VOLUME_DOWN:
                    active_now.add("vol_down")

            prev = self._raw_pressed_by_dev.get(h_dev, set())
            for k in sorted(active_now - prev):
                self._last_raw_event_at[k] = now_s
                if self.cfg.enabled and (now_s - self._last_ll_seen_at.get(k, -10.0)) > 0.12:
                    self._enqueue_key(k, True, now_s, "raw_hid")
            for k in sorted(prev - active_now):
                self._last_raw_event_at[k] = now_s
                if self.cfg.enabled and (now_s - self._last_ll_seen_at.get(k, -10.0)) > 0.12:
                    self._enqueue_key(k, False, now_s, "raw_hid")
            self._raw_pressed_by_dev[h_dev] = active_now

    def _hook_loop(self) -> None:
        if sys.platform != "win32":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hid = ctypes.WinDLL("hid", use_last_error=True)

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wt.HINSTANCE, wt.DWORD]
        user32.SetWindowsHookExW.restype = wt.HANDLE
        user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.restype = _LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]
        user32.UnhookWindowsHookEx.restype = wt.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user32.GetMessageW.restype = wt.BOOL
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = _LRESULT
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        user32.RegisterClassExW.restype = wt.ATOM
        user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
        ]
        user32.CreateWindowExW.restype = wt.HWND
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.DestroyWindow.restype = wt.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(_RAWINPUTDEVICE), wt.UINT, wt.UINT]
        user32.RegisterRawInputDevices.restype = wt.BOOL
        user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT), wt.UINT]
        user32.GetRawInputData.restype = wt.UINT
        user32.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT)]
        user32.GetRawInputDeviceInfoW.restype = wt.UINT
        hid.HidP_GetUsages.argtypes = [
            wt.DWORD, wt.USHORT, wt.USHORT, ctypes.POINTER(wt.USHORT),
            ctypes.POINTER(wt.ULONG), ctypes.c_void_p, ctypes.c_void_p, wt.ULONG,
        ]
        hid.HidP_GetUsages.restype = wt.LONG
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wt.DWORD
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wt.HINSTANCE

        self._hook_tid = int(kernel32.GetCurrentThreadId())

        # 1. Message-only window + Raw Input HID Consumer Control sink (filtered to PICO / BLE HID)
        @_WNDPROC
        def _wnd_proc(hwnd: wt.HWND, msg: int, w_param: int, l_param: int) -> int:
            if msg == WM_INPUT:
                try:
                    self._handle_raw_input(user32, hid, int(l_param))
                except Exception as exc:
                    self.log.debug("RawInput decode error: %s", exc)
                return 0
            if msg == WM_INPUT_DEVICE_CHANGE and int(w_param) == GIDC_REMOVAL:
                h_dev = int(l_param or 0)
                self._raw_devices.pop(h_dev, None)
                self._raw_pressed_by_dev.pop(h_dev, None)
                return 0
            return int(user32.DefWindowProcW(hwnd, msg, w_param, l_param))

        self._wnd_proc_ref = _wnd_proc
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"ArmoryBtRawMsgWnd_{self._hook_tid}"
        wc = _WNDCLASSEXW(
            cbSize=ctypes.sizeof(_WNDCLASSEXW),
            style=0,
            lpfnWndProc=_wnd_proc,
            cbClsExtra=0,
            cbWndExtra=0,
            hInstance=hinst,
            hIcon=None,
            hCursor=None,
            hbrBackground=None,
            lpszMenuName=None,
            lpszClassName=cls_name,
            hIconSm=None,
        )
        hwnd = None
        if user32.RegisterClassExW(ctypes.byref(wc)):
            hwnd_msg = ctypes.c_void_p(-3)
            hwnd = user32.CreateWindowExW(0, cls_name, "ArmoryBtRaw", 0, 0, 0, 0, 0, hwnd_msg, None, hinst, None)
        if hwnd:
            rid = _RAWINPUTDEVICE(
                usUsagePage=HID_USAGE_PAGE_CONSUMER,
                usUsage=HID_USAGE_CONSUMER_CONTROL,
                dwFlags=RIDEV_INPUTSINK | RIDEV_DEVNOTIFY,
                hwndTarget=hwnd,
            )
            if user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(_RAWINPUTDEVICE)):
                with self._lock:
                    self._raw_hwnd = hwnd
                    self.raw_input_active = True
                    self.raw_input_error = None
            else:
                err = ctypes.get_last_error()
                with self._lock:
                    self.raw_input_active = False
                    self.raw_input_error = f"RegisterRawInputDevices failed (win32 err={err})"
        else:
            err = ctypes.get_last_error()
            with self._lock:
                self.raw_input_active = False
                self.raw_input_error = f"CreateWindowExW failed (win32 err={err})"

        # 2. WH_KEYBOARD_LL hook — NOTE: must NOT skip LLKHF_INJECTED because Windows hidserv
        # translates BLE HID Consumer Control Vol+/Vol- into injected VK_VOLUME_UP/DOWN events!
        @_HOOKPROC
        def _ll_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and l_param:
                try:
                    kb = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    vk = int(kb.vkCode)
                    if vk in (VK_VOLUME_UP, VK_VOLUME_DOWN):
                        cfg = self.cfg
                        if cfg.enabled:
                            key = "vol_up" if vk == VK_VOLUME_UP else "vol_down"
                            is_down = int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN)
                            is_up = int(w_param) in (WM_KEYUP, WM_SYSKEYUP)
                            if is_down or is_up:
                                now_s = time.monotonic()
                                self._last_ll_seen_at[key] = now_s
                                if (now_s - self._last_raw_event_at.get(key, -10.0)) > 0.12:
                                    src = "ll_hook_inj" if (int(kb.flags) & 0x10) else "ll_hook"
                                    self._enqueue_key(key, is_down, now_s, src)
                                if cfg.swallow_volume_keys:
                                    return 1
                except Exception:
                    pass
            return int(user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param))

        self._hook_proc = _ll_proc
        hhook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not hhook:
            err = ctypes.get_last_error()
            with self._lock:
                self.hook_active = False
                self.hook_error = f"SetWindowsHookExW failed (win32 err={err})"
            self.log.warning("BT button hook unavailable: %s", self.hook_error)
        else:
            with self._lock:
                self._hook_handle = hhook
                self.hook_active = True
                self.hook_error = None
            self.log.info(
                "BT button capture active (hook=%s, rawInput=%s, mode=%s, swallow=%s)",
                self.hook_active,
                self.raw_input_active,
                self.cfg.mode,
                self.cfg.swallow_volume_keys,
            )

        msg = wt.MSG()
        try:
            if not hhook and not hwnd:
                return
            while not self._stop_event.is_set():
                rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if rc <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if hhook:
                try:
                    user32.UnhookWindowsHookEx(hhook)
                except Exception:
                    pass
            if hwnd:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            with self._lock:
                self._hook_handle = None
                self._raw_hwnd = None
                self.hook_active = False
                self.raw_input_active = False
                self._hook_tid = 0
