# The Armory — A/V, Compute, Network & Display Setup

Living hardware/A/V topology reference for Dave's home gym (**The Armory**) so Armory Fit telemetry, camera DVR, and the dedicated 32" Wallboard integrate cleanly with existing screens and audio.

---

## 1. Core App & Network Constraints

- **Primary Training Controller:** **Armory Fit** runs strictly as a native Android APK (`com.froriep.armoryfit`) on Dave's **Google Pixel**.
- **Gym Wi-Fi & Static/Reserved Hosts:**
  - `192.168.1.152` — **Dell OptiPlex** (`i7-4790` / `GTX 1650`, `S:\` `500 GB`, Windows daily driver, **never sleeps**). Runs `ArmoryCamDVR` on port `8766` (scheduled logon task, zero UAC). Connected locally to Hisense HDMI 1 + desk keyboard/mouse + **Anker PowerConf S330** USB speakerphone (`Room Voice Mic` + Windows dictation).
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
  │  ├── /wallboard Relay + ADB over Wi-Fi ──▶ [ 32" Fire TV (Wi-Fi Silk) ]        │
  │  └── [ Anker PowerConf S330 (USB) ] ──▶ Room Mic SSE (/api/voice/*) + Win+H    │
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

## 4. Room Voice Mic Relay & Windows Voice Typing (Anker PowerConf S330)

- **Hardware:** **Anker PowerConf S330** connected over USB to OptiPlex (`192.168.1.152`). Currently positioned on the treadmill walking desk (and planned for overhead ceiling placement behind the walking desk).
- **Armory Fit Room Voice Engine (`server/armory_cam/room_voice.py` on `:8766`):**
  - Captures audio via **WASAPI Shared Mode** (automatic `Windows WDM-KS` fallback during RDP sessions), runs Silero VAD (`silero_vad_v6.onnx`) + `faster-whisper` (`base.en` `int8` CPU, ~`1.1–1.4 s`), and broadcasts wake-word-gated commands (`"Armory …"`) over `GET /api/voice/events` SSE directly to the Pixel APK (`voiceRoomEnabled` default `true`, Pixel phone mic `voiceEnabled` default `false`).
  - Spoken confirmations (`"225 for 8, got it"`) route directly back out of the **PowerConf S330 speaker** (`POST /api/voice/ack`) while muting capture over the playback window so the speaker never self-triggers; falls back to Pixel TTS if off-LAN.
  - **Shared-Mode Coexistence (`listenMode = "workout"` default):** OptiPlex keeps the S330 microphone closed while idle and opens it ONLY when an active Strength workout (or dashboard 60 s mic test) is running, so **Windows 11 Voice Typing (`Win + H`)** works cleanly anytime at the walking desk.
- **Ceiling vs. Walking Desk Acoustics Notes:**
  - Moving the S330 from desk height to the ceiling behind the walking desk adds path distance (~2–4 ft → ~7–9 ft), puts the treadmill deck/motor fan closer to the bottom/rear boundary, and shifts speech slightly off-axis when facing the Hisense monitor. The S330's 4-mic omni array handles 10–15 ft rooms well; if ceiling height picks up extra motor hum or misses quiet commands, check the **Room Voice Mic** live meter at `http://192.168.1.152:8766/`, trim `vadThreshold` (`0.45 -> 0.35`), and for Windows dictation (`Win + H`) disable AGC/boost only if treadmill noise causes false starts.

---

## 5. Active BLE Cardio & Strength Sensors

- **Polar H10** chest strap (`0x180D` / `0x2A37`, Dual-BLE enabled).
- **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS, `ratio=11.1100`).
- **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM).
- **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).
