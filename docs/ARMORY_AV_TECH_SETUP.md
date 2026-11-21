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
  - `192.168.1.204` — **Armory Wallboard** 32" Toshiba Fire TV Edition (`TF-32A710U21` / `AFTBAMR311`, ADB `:5555`, Wi-Fi MAC `08:97:98:1f:4c:2b`, Ethernet MAC `08:97:98:22:59:6b`).
- **Phone Usage During Training:** Pixel stays close to Dave to log sets and push live state to OptiPlex `:8766`.

---

## 2. Live A/V & Display Topology

```text
[ OptiPlex 192.168.1.152 ] ───HDMI──────────────────────────────────────▶ [ Hisense 43" 43E6QF (HDMI 1) ]
  │ (ArmoryCamDVR :8766)                                                           ▲
  │  ├── Dual Wyze Cam v3 (.13 Rack / .14 Smith) ──RTSP──▶ 24/7 Ring + Clips       │ HDMI Out 1 (4K/1080p)
  │  ├── /wallboard Relay + WoL/ADB ───────▶ [ 32" Fire TV .204 (Kiosk APK) ]      │
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

## 3. Standalone 32" Fire TV Wallboard (`TF-32A710U21` · `com.froriep.armorywallboard`)

Instead of pointing the 32" TV at the Pixel's changing DHCP IP (`:8765`), the 32" Toshiba Fire TV Edition (**`Armory Wallboard`**, model `TF-32A710U21` / `AFTBAMR311`, `192.168.1.204:5555`, Wi-Fi MAC `08:97:98:1f:4c:2b`, Ethernet MAC `08:97:98:22:59:6b`) runs the standalone fullscreen **`Armory Wallboard`** kiosk APK (`wallboard-tv/`, `com.froriep.armorywallboard/.WallboardActivity`) pointed permanently at the always-on **OptiPlex relay (`http://192.168.1.152:8766/wallboard`)**:

1. **Workout Start on Pixel (`v0.61+`):** When Dave hits **Start Workout** (Lift, AirBike, or Spin), Armory Fit pushes live `WallboardState` (`HR`, `rest timer`, `1A <-> 1B` superset progression, watts/RPM) to OptiPlex `:8766/api/wallboard/wake`.
2. **OptiPlex WoL + ADB Auto-Wake:** OptiPlex spawns `adb.exe` silently (`CREATE_NO_WINDOW`) over LAN (`adb connect 192.168.1.204:5555`). If deep sleep caused `adbd` to go offline, `FireTvAdbController` fires Wake-on-LAN magic packets to `08:97:98:1f:4c:2b` / `08:97:98:22:59:6b` and retries ADB connect; once connected, it sends `KEYCODE_WAKEUP`, locks `screensaver_enabled = 0`, `sleep_timeout = 0`, and `wifi_sleep_policy = 2`, and launches `com.froriep.armorywallboard/.WallboardActivity` (`singleTask` fullscreen WebView, `FLAG_KEEP_SCREEN_ON`, zero URL bar, zero cursor, 3 s auto-retry, live SSE updates).
3. **Auto-Sleep When Done:** Finishing or pausing a workout schedules `KEYCODE_SLEEP` after `30 s` (and restores `screensaver_enabled = 1`), with a `10 min` idle watchdog if Wi-Fi drops.
4. **Device Hardening:** Family-room bloatware uninstalled, `str.auto_wake_up_enabled = 1`, `str.auto_wake_up_always = 1`, and `wifi_sleep_policy = 2` applied on the TV.

### One-Time 32" Fire TV Re-Setup / Recovery Reference
1. Connect the 32" Fire TV to home Wi-Fi or Ethernet (reserve `192.168.1.204` in router DHCP alongside `.13`, `.14`, `.15`, `.152`).
2. **Settings -> My Fire TV -> About -> click TV name 7 times** -> enable **Developer Options -> ADB Debugging**.
3. Build & install the kiosk APK via `.\wallboard-tv\build_wallboard_apk.ps1 -Install -Serial 192.168.1.204:5555` (uses `wallboard-tv/` without touching `android/` or Pixel `rev.json`), or pair in `http://192.168.1.152:8766/` -> **32" Fire TV Wallboard**.

---

## 4. Room Voice Mic Relay & Windows Voice Typing (Anker PowerConf S330)

- **Hardware:** **Anker PowerConf S330** connected over USB to OptiPlex (`192.168.1.152`). Currently positioned on the treadmill walking desk (and planned for overhead ceiling placement behind the walking desk).
- **Armory Fit Room Voice Engine (`server/armory_cam/room_voice.py` on `:8766`):**
  - Captures audio via **WASAPI Shared Mode** (automatic `Windows WDM-KS` fallback during RDP sessions), runs Silero VAD (`silero_vad_v6.onnx`) + `faster-whisper` (`base.en` `int8` CPU, ~`1.1–1.4 s`), and broadcasts wake-word-gated commands (`"Armory …"`) over `GET /api/voice/events` SSE directly to the Pixel APK (`voiceRoomEnabled` default `true`, Pixel phone mic `voiceEnabled` default `false`).
  - Spoken confirmations (`"225 for 8, got it"`) use Microsoft Neural TTS (`edge-tts`, default `en-US-AndrewNeural`, cached as WAV under `S:\ArmoryCam\tts_cache\` with natural `Rate=0` offline SAPI fallback) and play sequentially on a dedicated speaker worker with **`420 ms` USB-amp wakeup lead-in + `300 ms` tail** so the PowerConf S330 never clips leading/trailing syllables while muting capture over the playback window.
  - **Dual-Mic A/B Benchmark & 10-Phrase Shootout (`v0.81+`):** Supports both passive workout A/B logging (`/api/voice/ab/*`, 4.5s cross-source dedup so sets never double-log) and a guided 10-phrase Desk-vs-Ceiling shootout comparing Room Mic vs Pixel Phone Mic catch %, word match %, and `+Δ pts` improvement.
  - **Shared-Mode Coexistence (`listenMode = "workout"` default):** OptiPlex keeps the S330 microphone closed while idle and opens it ONLY when an active Strength workout (or dashboard 60 s mic test) is running, so **Windows 11 Voice Typing (`Win + H`)** works cleanly anytime at the walking desk.
- **Ceiling vs. Walking Desk Acoustics Notes:**
  - Moving the S330 from desk height to the ceiling behind the walking desk adds path distance (~2–4 ft → ~7–9 ft), puts the treadmill deck/motor fan closer to the bottom/rear boundary, and shifts speech slightly off-axis when facing the Hisense monitor. The S330's 4-mic omni array handles 10–15 ft rooms well; if ceiling height picks up extra motor hum or misses quiet commands, check the **Room Voice Mic** live meter at `http://192.168.1.152:8766/`, trim `vadThreshold` (`0.45 -> 0.35`), and for Windows dictation (`Win + H`) disable AGC/boost only if treadmill noise causes false starts.

---

## 5. Active BLE Cardio & Strength Sensors

- **Polar H10** chest strap (`0x180D` / `0x2A37`, Dual-BLE enabled).
- **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS, `ratio=11.1100`).
- **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM).
- **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).
