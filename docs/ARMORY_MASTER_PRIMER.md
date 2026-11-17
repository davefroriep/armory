# THE ARMORY — Strength & Conditioning Hub + Armory Fit Telemetry Primer

**Created:** 2026-11-14 *(Updated for `v0.35` APK-only Capacitor Architecture — see [ARMORY_FIT_TECHNICAL_REFERENCE.md](./ARMORY_FIT_TECHNICAL_REFERENCE.md) for the complete screen/module/API spec)*  
**Umbrella Workspace:** `N:\Documents\Antigravity\Strength and Conditioning` *(Strength & Conditioning Ecosystem)*  
**Target Athlete:** Dave Froriep (`Intervals.icu` ID `i758105` — Cycling FTP `182 W`, Zone 2 `128–130 W`, Norwegian 4×4 `218–225 W`)  
**Scope:** Unified **Strength & Conditioning Hub** housing **Armory Fit** (native Android `.apk` only — integrated Strength Lift Tracker, local 4-week rolling mesocycle queue, and Cardio Cockpit) plus automatic post-workout **Intervals.icu** sync — connecting over **5 Hz Direct BLE (`NUS`)** to **AirBike (Assault Elite)**, and targeting **Spin (Bowflex C7 / SS2K)** & **Treadmill (Horizon T101)** + **Polar H10**, while leaving **BLE FTMS (`0x1826`)** passive in the background for **MyWhoosh**.

---

## 1. Ecosystem Branding & Thread Topology

Because **"The Armory"** represents your entire home Strength & Conditioning gym (Belt Squat, 4-Post Rack, Cables, plus the cardio bay), **Armory Fit** serves as the single on-phone app while Intervals.icu acts purely as post-workout cloud analytics & cross-app activity sync:

| Thread / Sub-Domain | Workspace / Target | Responsibility |
| :--- | :--- | :--- |
| **`Armory Fit` Android App (Cardio + Strength)** | `N:\Documents\Antigravity\Strength and Conditioning` (`davefroriep/armory`) | Native Capacitor `.apk` **only** (GitHub Pages PWA disabled): Strength Lift Tracker, local rolling queue (`PHASE1_PLAN`), 5 Hz Cardio HUD, `WorkoutForegroundService`, offline queue |
| **Post-Workout Telemetry & CLI Analytics** | `Intervals.icu` (`i758105` + `armory_cli.py`) | Receives finished `.tcx` & `WeightTraining` logs (incl. `Skipped:` sets/exercises in `v0.35+`); read-only `fetchActivities` reconciles external MyWhoosh rides |
| **Hardware Repo 1: AirBike** | `N:\Documents\Antigravity\Smart Airbike` | XIAO ESP32-C6 (`AssaultBike` `Rev v1.5`) CAD + firmware + `/dev` bench calibration |
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
Build **one** Vite + TypeScript codebase packaged natively as a **Capacitor Android `.apk`** *(primary and only distribution channel — GitHub Pages deploy disabled; download latest via `https://github.com/davefroriep/armory/releases/latest/download/armory-fit.apk`)*:
1. **Native OS Bluetooth (`@capacitor-community/bluetooth-le`):** Talks directly to Android's native `BluetoothGatt` stack, persists `deviceId` (MAC) per machine + Polar H10, and reconnects without browser chooser prompts (`WebBluetoothTransport` remains inside `src/ble/transport.ts` solely for local Vite dev tests).
2. **`WorkoutForegroundService` (`android/.../WorkoutForegroundService.java` + `RestOverlay.java` + `WorkoutChime.java`):** Acquires `PARTIAL_WAKE_LOCK` + displays a live status-bar/lockscreen notification + minimized floating rest overlay using `foregroundServiceType="connectedDevice"`. You can lock your phone, text, or stream Spotify/YouTube with **zero BLE disconnects**.
3. **Phone-Side 1 Hz `IndexedDB` Recording (`armory-fit` v2):** Writes workouts (`workouts_meta` + `workout_samples`) and Strength logs (`lift_sessions`, `lift_routines`, `lift_ex`, `lift_videos`) locally with crash recovery. ESP32 SPIFFS `/wo.bin` logging is retired.

### Required Android 12+ (`API 31+`) Manifest Permissions (`AndroidManifest.xml`)
- `BLUETOOTH_SCAN` (`android:usesPermissionFlags="neverForLocation"`), `BLUETOOTH_CONNECT`
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE` (`service android:foregroundServiceType="connectedDevice"`)
- `POST_NOTIFICATIONS`, `WAKE_LOCK`, `INTERNET` (Intervals.icu TCX upload only)

---

## 4. Athlete Profile, Master Selector, Zone 5 Suite & Tactical HUD (`i758105`)

- **Top Bar:** Master Machine Toggle **`SPIN`** | **`AIRBIKE`** | **`TREADMILL`** + Always-On **`POLAR H10`** (`0x180D`, `0x2A37`, `HRmax` default `180 BPM`).

| Mode Button | Spin (`Bowflex C7`) | AirBike (`Assault Elite`) | Treadmill (`Horizon T101`) |
| :--- | :--- | :--- | :--- |
| **`[ZONE 2 BASE]`** | Auto-ERG **`129 W`** (`128–130 W`); default 5m Warmup + 45m Z2 + 5m Cooldown | Target **`44.5 RPM`** (`131 W` · `89 BPM` stroke); optional Lock-In Pacer | HR & Pace Guided **`Zone 2 (60–70% HRmax)`** incline walk/jog |
| **`[ZONE 5 VO2]`** *(Opens 4-Protocol Drawer)* | Auto-ERG across all 4 Z5 protocols below (`220–245 W` work / `105 W` recovery) | **4-Protocol Z5 Selector** (see **§4.1**) with Tactical Transition HUD + Lock-In Metronome | **Norwegian 4×4** / **5×3** / **6×2** (`85–95% HRmax`) with elevation integration |
| **`[QUICK / HIIT]`** | **Quick ERG** (`±5W / ±10W`) or **1–100 Gears** (`±1 / ±5 LVL`) + handlebar buttons | **Quick Start**, **Tabata 20/10**, **Sprint 10/20**, **Target Cal**, **Target Time** | **Quick Walk / Run**, Custom Intervals, `[TARE 0%]` BLE command |
| **Intervals.icu Naming & Sport** | `Bowflex C7 Z2` / `Bowflex C7 Z5 — <Protocol>` (`VirtualRide`) | `AirBike Z2` / `AirBike Z5 — <Protocol>` (`VirtualRide`) | `Treadmill Z2` / `Treadmill Z5 — <Protocol>` (`VirtualRun`) |

### 4.1 AirBike `[ZONE 5 VO2]` 4-Protocol Drawer (Exact `airbike_math.h` Targets)

Tapping **`[ZONE 5 VO2]`** on **AirBike** opens a 4-card protocol drawer displaying each workout's established sports-science/coaching name, structure, total time, Z5 work volume, and exact Assault Elite curve targets (`5:00` Warmup @ `44.0 RPM` / `127 W` included on all):

| # | Protocol Card Title | Coaching / Physiology Pedigree | Block Structure | Work Target (`RPM` · `Watts` · `L/R BPM`) | Recovery / Float Target | Total / Work Time |
| :-: | :--- | :--- | :--- | :--- | :--- | :---: |
| **1** | **Norwegian 4×4** | *Helgerud / NTNU CERG Protocol* — Classic stroke-volume ceiling benchmark | `5:00` WU → **4 × (`4:00` Work / `3:00` Easy)** *(4th rec = cooldown)* | **`54.5 RPM`** · **`223 W`** · **`109 BPM`** | **`40.0 RPM`** (`99 W` · `80 BPM` spin) | `33:00` (`16:00` Z5) |
| **2** | **Classic VO2 5×3** *(Recommended Start)* | *Fractionated Macro-Intervals* — Same Z5 stimulus before minute-4 posture breakdown | `5:00` WU → **5 × (`3:00` Work / `2:00` Cool-Down Spin)** | **`55.0 RPM`** · **`228 W`** · **`110 BPM`** | **`40.0 RPM`** (`99 W` · `80 BPM` spin) | `30:00` (`15:00` Z5) |
| **3** | **Cardiac Power 6×2** | *Joel Jamieson 1:1 Aerobic Power* — Crisp 2-min repeats; low mental dread & minimal overshoot penalty | `5:00` WU → **6 × (`2:00` Work / `2:00` Cool-Down Spin)** | **`56.0 RPM`** · **`240 W`** · **`112 BPM`** | **`40.0 RPM`** (`99 W` · `80 BPM` spin) | `29:00` (`12:00` Z5) |
| **4** | **Billat / Rønnestad 60/30** | *Intermittent Float VO2* — Adapted from Rønnestad `30/15` & Billat `60/30`; keeps $\text{VO}_2$ pinned via moving-fan floats | `5:00` WU → **2 Sets × [8 × (`60s` Work / `30s` Float)]** (`7` floats/set) + **`3:00` Full Set Rest** between sets + `2:00` CD | **`57.0 RPM`** · **`252 W`** · **`114 BPM`** | Float: **`40.0 RPM`** (`99 W` — *keep fan moving!*)<br>Set Rest: **`≤35 RPM` or Full Rest** | `33:00` (`16:00` Z5) |

*(Note: Spin `[ZONE 5 VO2]` mirrors the exact same 4 protocol cards using SS2K Auto-ERG at `220 W`, `225 W`, `235 W`, and `245 W` work / `105 W` recovery so either bike can execute the full Z5 menu.)*

### 4.1b Rev v0.5 program revision (supersedes the targets / order in the table above)

- **Order = preference, user-editable:** Norwegian 4×4 → Classic VO2 5×3 → 60/30 Float → Sprint 10/20 → Target Time → Target Calories. **Cardiac Power 6×2, Tabata 20/10 and Zone 2 Base ship hidden** (Z2 lives on spin / treadmill); tap **Reorder** on the AirBike screen to pin / move / hide, restore from the *Hidden* shelf, reset in Settings.
- **Conservative work targets** (arms + legs on a fan bike, %FTP is a ceiling): 4×4 `52.5 RPM` ≈ 111 % · 5×3 `53.5 RPM` ≈ 117 % · 60/30 `55 RPM` ≈ 125 % · Cardiac Power `54 RPM` ≈ 119 % of the 182 W baseline. Live HR ≥ 96 % HRmax on a work block raises an *EASE OFF* cue.
- **HR-gated recoveries** (Settings → *Recoveries*, default on): a rest never ends before the protocol's own rest (floor), then stays easy until HR ≤ 72 % HRmax (default 130 bpm @ 180), capped at 5:00 (ceiling); 5 s get-ready, then the next rep starts. No live HR, or *Fixed timers* mode = plain countdowns. *READY ▶* / *SKIP WARM-UP* ends a block manually. Cardiac Power recoveries are gated 2:00 → 5:00 (Jamieson).
- **Progress tracking:** the summary shows per-rep avg W / peak HR and each recovery's real length / end HR; the same line is appended to the Intervals.icu description (`Reps 205W@168 · … | Recoveries 3:05→127 · …`). Same power at lower HR, or faster recoveries, = bigger engine.
- **Live RPM chip:** the AirBike top-bar chip shows the bike's live pedal RPM once connected (green = frames arriving).
- **BLE self-healing:** 10 s connect timeout, 1.5 s retry, fire-and-forget disconnect, 6 s no-data watchdog, immediate retry on app resume / chip tap. The ESP32 only starts its radio ~15 s after power-up (Wi-Fi join + 5 s OTA rescue window), so a freshly rebooted bike appears a few seconds into that window.

### 4.2 Tactical Interval Cockpit HUD & "Lock-In" Transition Engine

Because AirBike wattage scales cubically ($P \propto \text{RPM}^3$), overshooting a launch by just `+10 RPM` spikes power by `+150–300 W` and blows anaerobic $W'$ in seconds. **Armory Fit** pairs a high-contrast cockpit HUD with a 5 Hz (`200 ms`) audio/visual transition pacer:

1. **Phase State Banner & Split Countdown HUD:**
   - Full-width color-coded phase header (`WARMUP` amber · `WORK` crimson-orange · `FLOAT / COOL` cyan · `SET REST` slate · `COOLDOWN` emerald).
   - Giant center-stage phase countdown (`MM:SS`, switching to **`SS.d` tenths** for the final `10.0s`), active pill (`SET 1/2 • REP 5/8`), and `"UP NEXT IN 0:10 → WORK 55.0 RPM (228 W)"` lookahead strip.
2. **5 Hz "Sweet-Spot" Horizon Gauge & Guardrails:**
   - Ultra-smooth `200 ms` needle + digital readout (`55.1 RPM` • `229 W`) centered on a glowing green target corridor (`±1.5 RPM` on AirBike / `±8 W` on Spin).
   - **Overshoot Red-Zone Flash:** Crossing `target + 2.5 RPM` flashes the gauge crimson with live watt-excess callout (`+140 W TOO HOT — EASE OFF`).
   - **Moving-Fan Float Floor (`AirBike`):** During `FLOAT` (`40 RPM`), dropping below `35 RPM` triggers an amber `"KEEP FAN SPINNING (40 RPM)"` cue so you never launch the next work rep against dead-fan inertia.
3. **Timeline Scrubber & Live `Z5 Bank` Telemetry:**
   - Bottom full-workout block timeline lighting up completed/current intervals + live **Polar H10** BPM, `%HRmax` badge, and cumulative **`Time ≥ 88% HRmax` (`Z5 Bank`)** timer so you see true central $\text{VO}_2\text{max}$ minutes stack up live.
4. **WebAudio "Lock-In & Auto-Mute" Transition Pacer (Zero Spotify Ducking):**
   - **`T-10s` Prep Pip:** Subtle double-pip + lookahead banner pulse (`"PREP SPIN-UP"`).
   - **`T-3s` Tempo Lead-In:** `3-2-1` countdown clicks pulsed **at the upcoming left/right stroke tempo** (`BPM = 2 × targetRPM`, e.g. `110 BPM` for `55 RPM`) + crisp phase-start chime at `T=0`.
   - **`T+0s..T+10s` Auto-Muting Metronome:** Keeps ticking the left/right arm-punch beat (`2 × targetRPM`) through the transition and **automatically fades out** as soon as live 5 Hz cadence stays within `±1.5 RPM` of target for `3.0s` (1-tap HUD pill `[PACER: AUTO | ON | MUTE]`).
   - **Instant Launch Overshoot Chirp (`<400 ms`):** High-pitch warning double-chirp + native phone haptic buzz if cadence exceeds `target + 3.0 RPM` during the first `15s` of any work interval.

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

### 5.3 Equipment 2: AirBike — Assault Elite / `AssaultBike` (`Rev v1.5`)
Source verified: `N:\Documents\Antigravity\Smart Airbike\PlatformIO\include\airbike_math.h`
- **Scan Filter:** Name `"AssaultBike"` or NUS UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e` (Primary ADV carries `NUS` + `FTMS 0x1826`; Scan Response carries `AssaultBike` + `CSC 0x1816`)
- **Primary 5 Hz TX Notify (`6e400003-...`) JSON (`200 ms`):**
  `{"dev":"airbike","w":127.0,"cad":44.0,"fan":488.8,"rfan":489.2,"spd":16.87,"dist":452.1,"kcal":15.42,"cpm":5.65,"pls":1042,"dt":122649,"ratio":11.1100,"mag":1,"mphf":0.3833}\n`
  - `w`: Interpolated official Assault Elite Watts (`float`, `0.1 W` resolution)
  - `cad`: Pedal/Crank RPM (`fan / ratio`)
  - `fan` / `rfan`: EMA-smoothed and raw Fan RPM (`alpha = 0.35f`)
  - `spd`: Console MPH (`cad * mphf`) · `dist`: Cumulative meters · `kcal`: Cumulative kcal · `cpm`: `watts * 0.04445`
  - `pls`: Cumulative ISR Hall pulses · `dt`: Last inter-pulse microsecond delta (`us`)
  - `ratio` / `mag` / `mphf`: Active NVS calibration parameters
- **RX Write (`6e400002-...`) Commands (persisted immediately to NVS Flash):**
  - `SET_RATIO:<float>` (`0.25..30.0`, default `11.1100`)
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
| **1. Assault AirBike (`Smart Airbike`)** | **100% Calibrated & Frozen (`Rev v1.5` @ `a783314`)** | 1. **DONE:** Factory magnet + flipped `3144E` (`D2`/GPIO 2) + 5-min + `4 fps` LCD calibration completed (`fanToCrankRatio = 11.1100`, `alpha = 0.35f`, `1.064 RPM` RMSE, `cpm = watts * 0.04445`, `20 ms` debounce).<br>2. Live on `AssaultBike` (`http://assaultbike.local`) ready for **Armory Fit** `5 Hz` `NUS` integration. *(Optional future mod: GPIO-gated sensor power + reed wake + BLE-only mode for 1S battery run).* |
| **2. Bowflex C7 (`SS2K Head`)** | **~75% Done** (Stepper/knob collar installed; ERG works up to Level 36) | 1. **Stage 1 — Full-Range C7 Sweep (`Levels 3–100`):** Measure `SS2K Potentiometer ADC` vs. C7 head up through Levels `36–70` (`218–225 W`) to remove the physical Bowflex C7 head.<br>2. **Stage 2 — Tactical BLE Power-Pedal Protocol:** Install borrowed/rental BLE Power Pedals (`0x1818`, `9/16"` crank) and run automated `scripts/calibrate_true_watts.py` across 15 Potentiometer ADC steps (`0–100%`) at `70/85/95 RPM` to eliminate "photocopied C7 fuzz" forever (`trueWatts = f(pot_adc, rpm)`).<br>3. **Stage 3 — Slim-Down + FTMS:** Decouple `isErgActive`, retire SPIFFS `/wo.bin` + `cockpit_pwa`, and add passive FTMS (`0x2AD2` + `0x2AD9`) for MyWhoosh spin races. |
| **3. Horizon T101 (`Smart Treadmill`)** | **~45% Done** (Parts on workbench) | 1. **Workbench Solder & Dry Test:** Solder 7 shared points (`3V3`, `GND`, `D2` Hall `D0`, `D3` Tare button, `D4 SDA`, `D5 SCL` MPU-6050) + bench test OLED, magnet pulses, and tilt (`0–15%`).<br>2. **Physical Install + Firmware Upgrade:** Glue roller magnet (`1–3 mm` gap), mount Y-aligned MPU-6050 on frame, and add **5 Hz Direct BLE (`NUS`)** + `TARE_INCLINE`.<br>3. **True Belt Speed & Incline Verification:** Time 10 chalked belt revolutions at `2.0 / 4.0 / 6.0 MPH` to calibrate `ROLLER_DIAMETER_CM`, and check `0% / 5% / 10%` against a digital level. |

---

## 7. Intervals.icu TCX Upload & Security Hygiene

- **Endpoint:** `POST https://intervals.icu/api/v1/athlete/{athleteId}/activities?name={title}&description={desc}` (`Basic btoa("API_KEY:" + apiKey)`)
- **Format & Calories Rule:** `.tcx` (`Sport="Biking"` for Spin/AirBike; `Sport="Running"` with `Speed` + `AltitudeMeters` + `RunCadence` for Treadmill) with `pending_upload` IndexedDB retry queue. Always write TCX `<Calories>` from mechanical work (`round(totalWorkKj)`, i.e. `1 kJ = 1 kcal`, matching Bowflex/Intervals convention) while keeping Assault console calories (`watts * 0.04445`) on the live AirBike workout screen.
- **STRICT SECRETS RULE:** Never commit `intervalsApiKey` or Wi-Fi credentials into public `.html`/`.ts` files; keep secrets exclusively in local device `Preferences` / `localStorage` *(and scrub the legacy hardcoded key in `SS2K Head/cockpit_pwa/index.html` when retiring it).*

---

## 8. Implementation Status
See [ARMORY_FIT_TECHNICAL_REFERENCE.md](./ARMORY_FIT_TECHNICAL_REFERENCE.md) for the live module map, screen-by-screen UI/button/gesture inventory, storage schemas, and APK release workflow (`v0.35+`).
