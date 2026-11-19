# The Armory — A/V, Compute, Network & Display Setup

Living hardware/A/V topology reference for Dave's home gym (**The Armory**) so Armory Fit telemetry, camera DVR, and the dedicated 32" Wallboard integrate cleanly with existing screens and audio.

---

## 1. Core App & Network Constraints

- **Primary Training Controller:** **Armory Fit** runs strictly as a native Android APK (`com.froriep.armoryfit`) on Dave's **Google Pixel**.
- **Gym Wi-Fi & Static/Reserved Hosts:**
  - `192.168.1.152` — **Dell OptiPlex** (`i7-4790` / `GTX 1650`, `S:\` `500 GB`, Windows daily driver, **never sleeps**). Runs `ArmoryCamDVR` on port `8766` (scheduled logon task, zero UAC). Connected locally to Hisense HDMI 1 + desk keyboard/mouse.
  - `192.168.1.15` — **Denon AVR-3313CI** on wired Ethernet (Telnet `:23`, HTTP `:80`, UPnP `:8080`).
  - `192.168.1.13` — **Armory-Rack** Wyze Cam v3 (`Thingino` open firmware, `1920×1080 H.264 @ 25 fps + AAC`, `rtsp://.../ch0`).
  - `192.168.1.14` — **Armory-Smith** Wyze Cam v3 (`Thingino` open firmware, `1920×1080 H.264 @ 25 fps + AAC`, `rtsp://.../ch0`).
- **Phone Usage During Training:** Pixel stays close to Dave to log sets and push live state to OptiPlex `:8766`.

---

## 2. Live A/V & Display Topology

```text
[ OptiPlex 192.168.1.152 ] ───HDMI──────────────────────────────────────▶ [ Hisense 43" 43E6QF (HDMI 1) ]
  │ (ArmoryCamDVR :8766)                                                           ▲
  │  ├── Dual Wyze Cam v3 (.13 Rack / .14 Smith) ──RTSP──▶ 24/7 Ring + Clips       │ HDMI Out 1 (4K/1080p)
  │  └── /wallboard Relay + ADB over Wi-Fi ──▶ [ 32" Fire TV (Wi-Fi Silk) ]        │
                                                                                   │
[ Fire TV Cube ] ──HDMI──▶ [ Denon AVR-3313CI (192.168.1.15, Multi-Ch Stereo) ] ───┴──▶ HDMI Out 2: [ TCL Roku TV (1080p) ]
                                    │
                                    └──▶ 4× Corner Tower Speakers + 1× Center Channel (over treadmill)
```

- **Main Displays (Dual HDMI Out `Auto(Dual)` on Denon):**
  - **Monitor 1:** **Hisense 43" E6 Cinema Series 4K Fire TV (`43E6QF`, 2025)** (`HDMI 1 = OptiPlex`, `HDMI 2 = Denon Out 1`, `HDMI 3 = eARC`, plus Optical Out).
  - **Monitor 2:** **TCL Roku TV (`1080p`)** mirroring `Fire TV Cube -> Denon`.
- **Receiver Configuration Notes (`192.168.1.15`):**
  - Driving **Bi-Amp Fronts + Center + Surrounds** in **`MULTI CH STEREO`** (Audyssey MultEQ/DynEQ/DynVol intentionally left `OFF` — no calibration mic and current sound is dialed in).
  - Switching the Hisense to `HDMI 1` (OptiPlex) leaves `Fire TV Cube -> Denon -> Towers/TCL` playing uninterrupted.

---

## 3. Standalone 32" Fire TV Wallboard (OptiPlex Relay + ADB Auto-Wake)

Instead of pointing the 32" TV at the Pixel's changing DHCP IP (`:8765`), the 32" Fire TV points permanently at the always-on **OptiPlex relay (`http://192.168.1.152:8766/wallboard`)**:

1. **Workout Start on Pixel (`v0.61+`):** When Dave hits **Start Workout** (Lift, AirBike, or Spin), Armory Fit pushes live `WallboardState` (`HR`, `rest timer`, `1A <-> 1B` superset progression, watts/RPM) to OptiPlex `:8766/api/wallboard/wake`.
2. **OptiPlex ADB Auto-Wake:** OptiPlex spawns `adb.exe` silently (`CREATE_NO_WINDOW`) over Wi-Fi (`adb connect <firetv-ip>:5555`), sends `KEYCODE_WAKEUP`, temporarily suppresses the Fire TV screensaver (`screensaver_enabled = 0`), and opens **Amazon Silk** straight to `http://192.168.1.152:8766/wallboard` (live SSE updates, zero browser timeout when idle).
3. **Auto-Sleep When Done:** Finishing or pausing a workout schedules `KEYCODE_SLEEP` after `30 s` (and restores `screensaver_enabled = 1`), with a `10 min` idle watchdog if Wi-Fi drops.

### One-Time 32" Fire TV Setup Checklist (~2 Minutes When Hung)
1. Connect the 32" Fire TV to home Wi-Fi (reserve its DHCP IP in the router alongside `.13`, `.14`, `.15`, `.152`).
2. **Settings -> My Fire TV -> About -> click TV name 7 times** -> enable **Developer Options -> ADB Debugging**.
3. Open `http://192.168.1.152:8766/` -> enter the TV IP in **32" Fire TV Wallboard** -> click **Connect / Pair ADB** -> accept **"Always allow from this computer"** on the TV screen once.

---

## 4. Active BLE Cardio & Strength Sensors

- **Polar H10** chest strap (`0x180D` / `0x2A37`, Dual-BLE enabled).
- **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS, `ratio=11.1100`).
- **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM).
- **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).
