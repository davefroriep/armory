# THE ARMORY — Strength & Conditioning Hub + Armory Fit Telemetry Primer

**Created:** 2026-11-14 (Updated for 5 Hz Direct BLE Architecture)  
**Umbrella Workspace:** `N:\Documents\Antigravity\Armory` *(Strength & Conditioning Ecosystem)*  
**Target Athlete:** Dave Froriep (`Intervals.icu` ID `i758105` — Cycling FTP `182 W`, Zone 2 `128–130 W`, Norwegian 4×4 `218–225 W`)  
**Scope:** Unified **Strength & Conditioning Hub** housing **Armory Fit** (Capacitor `.apk` + GitHub Pages mirror), **Boostcamp** (`boostcamp_client.py` strength programming), and **Intervals.icu** telemetry/analytics — connecting over **5 Hz Direct BLE (`NUS`)** to **Spin (Bowflex C7 / SS2K)**, **AirBike (Assault Elite)**, and **Treadmill (Horizon T101)** + **Polar H10**, while leaving **BLE FTMS (`0x1826`)** passive in the background for **MyWhoosh**.

---

## 1. Ecosystem Branding & Thread Topology

Because **"The Armory"** represents your entire home Strength & Conditioning gym (Belt Squat, 4-Post Rack, Cables, plus the cardio bay), keep the umbrella workspace focused on S&C and name the cardio cockpit app distinctly:

| Thread / Sub-Domain | Workspace / Target | Responsibility |
| :--- | :--- | :--- |
| **Thread 1: Strength Programming** | `Boostcamp` (moved/linked under S&C Hub) | 100% programmatic workout programming via `boostcamp_client.py` + `backups/` |
| **Thread 2: Telemetry & Wellness** | `Intervals.icu` (`i758105` + `health_records.sqlite`) | Daily wellness trends, TSB/CTL load, power zone tracking, `.fit`/`.tcx` sync |
| **Thread 3: `Armory Fit` App** | `N:\Documents\Antigravity\Armory` (`davefroriep/armory`) | Unified Capacitor `.apk` + GitHub Pages mirror replacing `SS2K/cockpit_pwa` & `Treadmill/pwa` |
| **Hardware Repo 1: AirBike** | `N:\Documents\Antigravity\Smart Airbike` | XIAO ESP32-C6 (`AssaultBike`) CAD + firmware + `/dev` bench calibration |
| **Hardware Repo 2: Spin** | `N:\Documents\Antigravity\SS2K Head` | ESP32-WROOM (`SS2K_Tuner`) NEMA17 + TMC2209 actuator + Power Pedal calibration |
| **Hardware Repo 3: Treadmill** | `N:\Documents\Antigravity\Smart Treadmill` | XIAO ESP32-C6 (`Horizon T101 Pod`) roller/MPU-6050 firmware + enclosure |

### App Name Candidates (`Armory Fit` / Alternatives)
- **`ARMORY FIT`** *(Recommended — clean separation from barbell lifting while staying clearly under The Armory banner)*
- **`ARMORY ENGINE`** *(Strong aerobic/VO2 metabolic identity)*
- **`ARMORY CARDIO`** / **`ARMORY DECK`** / **`ARMORY OPS`**

---

## 2. Why Primary 5 Hz Direct BLE (`NUS`) Beats FTMS for Standalone App Usage

Standard **BLE FTMS (`0x1826`)** is designed for third-party virtual worlds like **MyWhoosh** and imposes severe limits:
1. **Quantized Telemetry:** Forces integers for Watts (`1 W`), coarse `0.5 RPM` steps for Cadence, and `0.1%` incline steps, usually throttled to `1–2 Hz` (`500–1,000 ms` blips).
2. **No Raw Hardware Diagnostics:** Drops microsecond magnet pulse deltas (`dt` / `pulse_delta_us`) and raw Hall pulse counters (`pls`).
3. **No Bidirectional Calibration Channel:** FTMS Control Point (`0x2AD9`) has no native way to send `SET_RATIO`, `TARE_INCLINE`, `RESET_PULSES`, or `JOG`.

### The Unified Protocol Decision
- **Primary (`Armory Fit` <-> ESP32s): 5 Hz (`200 ms`) Direct BLE Nordic UART (`NUS` `6e400001-b5a3-f393-e0a9-e50e24dcca9e`)**
  - Every machine streams unquantized `200 ms` JSON frames on `TX (6e400003-...)` and accepts instant ASCII commands on `RX (6e400002-...)`.
  - Result: Zero 1-second blips, silky-smooth live Zone 2 / Sprint dials, exact pulse counts, and native BLE calibration from inside **Armory Fit**.
- **Secondary Passive (`MyWhoosh` <-> ESP32s): Standard BLE FTMS (`0x1826`)**
  - Runs passively alongside NUS on `AssaultBike` (`0x2AD2`), `Horizon T101 Pod` (`0x2ACD`), and (`Phase 3`) `SS2K_Tuner` (`0x2AD2` + `0x2AD9`).
  - When racing on **MyWhoosh**, leave **Armory Fit** closed and let MyWhoosh connect directly to FTMS.

---

## 3. Why Capacitor + Foreground Service Eliminates Android BLE Drops

### What Broke the Legacy Browser PWAs (`SS2K Head/cockpit_pwa/index.html`)
1. **Tab-Lifecycle Throttling:** Chrome Android ties `navigator.bluetooth` strictly to the visible foreground tab; locking the screen or opening Messages drops GATT notifications.
2. **Audio-Focus Collision:** `silent.wav` + `MediaSession` hacks collided with Spotify/podcasts/YouTube; silencing audio re-triggered Android Doze disconnects.

### Permanent Root-Cause Architecture
Build **one** responsive web UI (`Vite + TypeScript`), shipped in two ways:
1. **Primary Gym Daily Driver — Capacitor Android `.apk`:**
   - **Native OS Bluetooth (`@capacitor-community/bluetooth-le`):** Talks directly to Android's native `BluetoothGatt` stack, persists `deviceId` (MAC) per machine + Polar H10, and reconnects without browser chooser prompts.
   - **Custom Lightweight `WorkoutForegroundService.kt` (~60 lines):** Acquires `PARTIAL_WAKE_LOCK` + displays a live status-bar/lockscreen notification (`WORKOUT ACTIVE • 18:42 • 129 W • 134 BPM`) using `foregroundServiceType="connectedDevice"`. You can lock your phone, text, or stream Spotify/YouTube with **zero BLE disconnects**.
   - **Phone-Side 1 Hz `IndexedDB` Recording:** Every `1,000 ms`, writes `{ t, watts, cad, speedMph, inclinePct, elevGainM, distM, kcal, hr, targetW }` directly to IndexedDB (`workouts_meta` + `workout_samples`) with crash/reboot recovery (`Resume` | `Finish & Sync` | `Discard`). ESP32 SPIFFS `/wo.bin` logging is retired.
2. **Desktop/Backup Mirror — GitHub Pages (`https://davefroriep.github.io/armory/`):**
   - Swaps transport at runtime via `Capacitor.isNativePlatform()` (`CapacitorBleTransport` on Android vs. `WebBluetoothTransport` on Desktop Chrome).

### Required Android 12+ (`API 31+`) Manifest Permissions (`AndroidManifest.xml`)
- `BLUETOOTH_SCAN` (`android:usesPermissionFlags="neverForLocation"`), `BLUETOOTH_CONNECT`
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE` (`service android:foregroundServiceType="connectedDevice"`)
- `POST_NOTIFICATIONS`, `WAKE_LOCK`, `INTERNET` (Intervals.icu TCX upload only)

---

## 4. Athlete Profile, Master Selector & Workout Modes (`i758105`)

- **Top Bar:** Master Machine Toggle **`SPIN`** | **`AIRBIKE`** | **`TREADMILL`** + Always-On **`POLAR H10`** (`0x180D`, `0x2A37`, `HRmax` default `180 BPM`).

| Mode | Spin (`Bowflex C7`) | AirBike (`Assault Elite`) | Treadmill (`Horizon T101`) |
| :--- | :--- | :--- | :--- |
| **Zone 2 Base** | Auto-ERG **`129 W`** (`128–130 W`); default 5m Warmup + 45m Z2 + 5m Cooldown | Pace Cue **`44 RPM`** (`~127–134 W` Assault table); Music BPM (`2×RPM`) | HR & Pace Guided **`Zone 2 (60–70% HRmax)`** walk/jog + incline tracking |
| **Zone 5 Norwegian 4×4** | Auto-ERG Warmup (`129 W`) → **4 × (`4:00 @ 220 W` [218–225 W] / `3:00 @ 105 W`)** → Cooldown (`105 W`) | **4 × (`4:00 @ 54–55 RPM` [`218–228 W`] / `3:00 @ ~40 RPM` [`~99 W`])** with chimes/vibrate | **4 × (`4:00 Work @ 85–95% HRmax` / `3:00 Active Recovery`)** with elevation integration |
| **Quick / Custom / HIIT** | **Quick ERG** (`±5W / ±10W`) or **1–100 Gears** (`±1 / ±5 LVL`) + handlebar buttons | **Quick Start**, **Tabata 20/10**, **Interval 10/20**, **Target Cal**, **Target Time** | **Quick Run / Walk**, Custom Intervals, `[TARE 0%]` BLE command |
| **Intervals.icu Activity Type** | `Sport="Biking"` (`VirtualRide`) | `Sport="Biking"` (`VirtualRide`) | `Sport="Running"` (`VirtualRun`) with `RunCadence`, `Speed`, `AltitudeMeters` |

---

## 5. Byte-Exact BLE Protocol Contracts (Primary 5 Hz NUS + Passive FTMS)

> **Endianness & Framing:** All BLE multi-byte numbers below are **Little-Endian (`DataView.getUint16(off, true)`)**. Primary NUS streams (`6e400003-...`) terminate every JSON object with `\n` (`BLEDevice::setMTU(517)`), so clients buffer until `\n` before `JSON.parse()`.

### 5.1 Heart Rate Strap — Polar H10 (Standard SIG)
- **Service:** `0000180d-0000-1000-8000-00805f9b34fb` (`heart_rate`)
- **Notify Characteristic:** `00002a37-0000-1000-8000-00805f9b34fb` (`heart_rate_measurement`) -> `hr = (flags & 0x01) ? dv.getUint16(1, true) : dv.getUint8(1)`
- **Optional Battery Read:** `0000180f-...` / `00002a19-...` -> `dv.getUint8(0)` (`0–100%`)

---

### 5.2 Equipment 1: Spin — Bowflex C7 / `SS2K_Tuner`
Source verified: `N:\Documents\Antigravity\SS2K Head\ss2k_install_mode\src\main.cpp` & `c7_power_model.h`
- **Scan Filter:** Name prefix `SS2K` (`"SS2K_Tuner"`) or NUS UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- **Primary 5 Hz TX Notify (`6e400003-...`) JSON (`main.cpp:839-845`):**
  - `cad` (`float`), `ew` (`int` Watts), `lvl` (`float` 3–100), `tlvl` (`int`), `mode` (`0=Gears | 1=ERG`), `tgt_w` (`int` 0–800), `pot` (`int` ADC), `v` (`float`), `pos` (`long`), `sg` (`int`), `stall` (`0|1`), `run` (`0|1`), `pls` (`ulong`), `ratio` (`float`, `4.50`)
- **RX Write (`6e400002-...`) Commands:**
  - `SET_MODE:0` / `SET_MODE:1`, `SET_WATTS:<0..800>`, `SET_LEVEL:<1..100>`, `SET_RATIO:<0.1..20.0>`, `RESET_PULSES`, `WO_START:<unixEpochSec>`, `WO_PAUSE`, `WO_RESUME`, `WO_DISCARD`, `JOG:<steps>`, `ZERO`, `UNWEDGE_FWD`, `UNWEDGE_REV`, `SET_MA:<100..1200>`
- **Current Firmware Gotcha (`main.cpp:745`):** Until Stage 3 cleanup, `isErgActive` requires `woState == WO_ACTIVE`, so Armory Fit sends `WO_START:<epoch>\n` right before `SET_MODE:1\n` + `SET_WATTS:<w>\n`, and sends `WO_DISCARD\n` on finish.
- **Stage 3 External Compatibility:** Decouple `isErgActive = (controlMode == 1)`, remove SPIFFS `/wo.bin`, and add passive BLE FTMS (`0x1826` / `0x2AD2` + `0x2AD9`) for direct **MyWhoosh** races.

---

### 5.3 Equipment 2: AirBike — Assault Elite / `AssaultBike` (`Rev v1.3`)
Source verified: `N:\Documents\Antigravity\Smart Airbike\PlatformIO\include\airbike_math.h`
- **Scan Filter:** Name `"AssaultBike"` or NUS UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e` (Primary ADV carries `NUS` + `FTMS 0x1826`; Scan Response carries `AssaultBike` + `CSC 0x1816`)
- **Primary 5 Hz TX Notify (`6e400003-...`) JSON (`200 ms`):**
  `{"dev":"airbike","w":127.0,"cad":44.0,"fan":126.5,"rfan":126.8,"spd":16.87,"dist":452.1,"kcal":15.42,"cpm":5.12,"pls":1042,"dt":473120,"ratio":2.8750,"mag":1,"mphf":0.3833}\n`
  - `w`: Interpolated official Assault Elite Watts (`float`, `0.1 W` resolution)
  - `cad`: Pedal/Crank RPM (`fan / ratio`)
  - `fan` / `rfan`: EMA-smoothed and raw Fan RPM
  - `spd`: Console MPH (`cad * mphf`) · `dist`: Cumulative meters · `kcal`: Cumulative kcal · `cpm`: `watts * 0.04035`
  - `pls`: Cumulative ISR Hall pulses · `dt`: Last inter-pulse microsecond delta (`us`)
  - `ratio` / `mag` / `mphf`: Active NVS calibration parameters
- **RX Write (`6e400002-...`) Commands (persisted immediately to NVS Flash):**
  - `SET_RATIO:<float>` (`0.25..30.0`, default `2.875`)
  - `SET_MAGNETS:<int>` (`1..16`, default `1`)
  - `SET_MPH_FACTOR:<float>` (`0.05..2.0`, default `0.3833`)
  - `RESET` or `RESET_PULSES` (zeros distance, calories, and pulse counters)
- **Passive Secondary FTMS (`0x1826` -> `0x2AD2` @ `500 ms`) & CSC (`0x1816` -> `0x2A5B`):**
  - Retained silently for legacy FTMS apps (`Flags = 0x0044`: Speed `u16LE km/h*100`, Cadence `u16LE rpm*2`, Power `s16LE W`).
- **Official Assault Curve Anchors:** Zone 2 (`127–134 W`) ≈ **`44–45 RPM`** · FTP (`182 W`) ≈ **`50.6 RPM`** · Norwegian 4×4 (`218–228 W`) ≈ **`54–55 RPM`** · `60 RPM = 288 W` · `70 RPM = 442 W` · `100 RPM = 1205 W`.

---

### 5.4 Equipment 3: Treadmill — Horizon T101 / `Horizon T101 Pod`
Source verified: `N:\Documents\Antigravity\Smart Treadmill\PlatformIO\src\main.cpp`
- **Target Architecture Upgrade (5 Hz Primary NUS + Passive FTMS/RSC):**
  - Add NUS (`6e400001-...`) streaming 5 Hz unquantized JSON:
    `{"dev":"treadmill","spd_mph":3.42,"inc_pct":1.50,"inc_raw":1.52,"cad_spm":168,"dist_m":1240.5,"elev_m":18.6,"pls":8770,"dt":42100}\n`
  - Add RX write commands (`6e400002-...`): `TARE_INCLINE` (zeros MPU-6050 at flat `0%`), `SET_ROLLER_CM:<float>` (adjusts true belt speed factor), `RESET_PULSES`.
- **Currently Active Passive FTMS (`0x1826` -> `0x2ACD` @ `1 Hz`) & RSC (`0x1814` -> `0x2A53` @ `1 Hz`):**
  - `0x2ACD`: `Flags = 0x0008`, Speed (`u16LE km/h*100`), Incline (`s16LE %*10`), Ramp Angle (`s16LE deg*10`).
  - `0x2A53`: `Flags = 0x00`, Speed (`u16LE m/s*256`), Runner Cadence (`u8 SPM`).

---

## 6. Three-Machine Hardware & Calibration Roadmap

| Machine | Status | Immediate & Next Calibration Milestones |
| :--- | :--- | :--- |
| **1. Assault AirBike (`Smart Airbike`)** | **~90% Done** (`Rev v1.3` built/flashed; CAD clip `v16.3` printed) | 1. Snap mount onto fan shroud & spin fan to confirm `Total Pulses` increments on `/dev`.<br>2. Run **100-pedal-revolution tally** on `http://assaultbike.local/dev` (Start at stroke 0 -> Lock at stroke 100 *before* coasting) to lock `fanToCrankRatio` in NVS.<br>3. **5-Minute Screen Video Overlay**: Film physical LCD for 5 min while Python logs `/api/telemetry` at `4 Hz`; extract `1 fps` frames with `ffmpeg` and match timestamps to dial in dynamic fan inertia EMA smoothing (`alpha`) & exact watt parity. |
| **2. Bowflex C7 (`SS2K Head`)** | **~75% Done** (Stepper/knob collar installed; ERG works up to Level 36) | 1. **Stage 1 — Full-Range C7 Sweep (`Levels 3–100`):** Measure `SS2K Potentiometer ADC` vs. C7 head up through Levels `36–70` (`218–225 W`) to remove the physical Bowflex C7 head.<br>2. **Stage 2 — Tactical BLE Power-Pedal Protocol:** Install borrowed/rental BLE Power Pedals (`0x1818`, `9/16"` crank) and run automated `scripts/calibrate_true_watts.py` across 15 Potentiometer ADC steps (`0–100%`) at `70/85/95 RPM` to eliminate "photocopied C7 fuzz" forever (`trueWatts = f(pot_adc, rpm)`).<br>3. **Stage 3 — Slim-Down + FTMS:** Decouple `isErgActive`, retire SPIFFS `/wo.bin` + `cockpit_pwa`, and add passive FTMS (`0x2AD2` + `0x2AD9`) for MyWhoosh spin races. |
| **3. Horizon T101 (`Smart Treadmill`)** | **~45% Done** (Parts on workbench) | 1. **Workbench Solder & Dry Test:** Solder 7 shared points (`3V3`, `GND`, `D2` Hall `D0`, `D3` Tare button, `D4 SDA`, `D5 SCL` MPU-6050) + bench test OLED, magnet pulses, and tilt (`0–15%`).<br>2. **Physical Install + Firmware Upgrade:** Glue roller magnet (`1–3 mm` gap), mount Y-aligned MPU-6050 on frame, and add **5 Hz Direct BLE (`NUS`)** + `TARE_INCLINE`.<br>3. **True Belt Speed & Incline Verification:** Time 10 chalked belt revolutions at `2.0 / 4.0 / 6.0 MPH` to calibrate `ROLLER_DIAMETER_CM`, and check `0% / 5% / 10%` against a digital level. |

---

## 7. Intervals.icu TCX Upload & Security Hygiene

- **Endpoint:** `POST https://intervals.icu/api/v1/athlete/{athleteId}/activities?name={title}&description={desc}` (`Basic btoa("API_KEY:" + apiKey)`)
- **Format:** `.tcx` (`Sport="Biking"` for Spin/AirBike; `Sport="Running"` with `Speed` + `AltitudeMeters` + `RunCadence` for Treadmill) with `pending_upload` IndexedDB retry queue.
- **STRICT SECRETS RULE:** Never commit `intervalsApiKey` or Wi-Fi credentials into public `.html`/`.ts` files; keep secrets exclusively in local device `Preferences` / `localStorage` *(and scrub the legacy hardcoded key in `SS2K Head/cockpit_pwa/index.html` when retiring it).*

---

## 8. Copy-Paste Kickoff Prompts (Thread Structure)

### For the `Armory Fit` App Thread
> *"We are building the **Armory Fit** cardio cockpit app in `N:\Documents\Antigravity\Armory`. Read `GEMINI.md` and `docs/ARMORY_MASTER_PRIMER.md` first. Every machine (`Spin`, `AirBike`, `Treadmill`) uses **5 Hz Direct BLE (`NUS` `6e400001-...`)** as its primary telemetry and calibration transport, with FTMS kept strictly passive for MyWhoosh. Let's scaffold the `Vite + TypeScript + Capacitor` app with our custom `WorkoutForegroundService.kt`, `BleTransport` abstraction, 1 Hz `IndexedDB` session recorder with crash recovery, machine adapters (`SpinAdapter`, `AirBikeAdapter`, `TreadmillAdapter`, `PolarH10`), Intervals.icu `.tcx` uploader, and GitHub Actions `.apk` CI build."*
