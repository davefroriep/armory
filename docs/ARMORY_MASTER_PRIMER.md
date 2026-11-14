# THE ARMORY — Master Cardio Cockpit Engineering Primer & Migration Blueprint

**Created:** 2026-11-14  
**Workspace:** `N:\Documents\Antigravity\Armory`  
**Target Athlete:** Dave Froriep (`Intervals.icu` ID `i758105` — Cycling FTP `182 W`, Zone 2 `128–130 W`, Norwegian 4×4 `218–225 W`)  
**Scope:** Unified native Android telemetry app (**Capacitor `.apk`** + **GitHub Pages** web mirror) replacing 3 fragmented browser PWAs across **Spin (Bowflex C7 / SS2K)**, **AirBike (Assault Elite)**, and **Treadmill (Horizon T101)**, paired simultaneously with **Polar H10** and syncing directly to **Intervals.icu**.

---

## 1. Brand & App Naming Options

Keep the codebase folder/repo slug clean (`Armory` / `davefroriep/armory`) so the Android app display name (`appName` in `capacitor.config.ts`) and header title can change any time without breaking paths.

| Candidate Display Name | Pixel Home-Screen Label | Vibe / Fit |
| :--- | :--- | :--- |
| **`ARMORY`** *(Recommended)* | `Armory` | Punchiest, zero truncation on Android launcher, zero wordiness. Fits cleanly alongside the machine pill (`ARMORY • SPIN`). |
| **`ARMORY ENGINE`** | `Armory Engine` | Speaks directly to aerobic/VO2 metabolic conditioning ("building the engine"). |
| **`ARMORY OPS`** | `Armory Ops` | Crisp tactical instrument feel for structured Z2 / Z5 intervals. |
| **`ARMORY DECK`** | `Armory Deck` | Natural replacement for "Cockpit" without sounding generic. |
| **`ARMORY PULSE`** | `Armory Pulse` | Clean tie-in to Polar H10 heart-rate zones + cadence pulses. |

---

## 2. Why Capacitor + Foreground Service Permanently Eliminates BLE Drops

### What Broke the Browser PWAs (`SS2K Head/cockpit_pwa/index.html`)
1. **Tab-Lifecycle Throttling:** Chrome Android binds Web Bluetooth (`navigator.bluetooth`) strictly to the active visible browser tab. Locking the phone screen, opening Messages, or switching apps lets Android Doze freeze JS timers and tear down GATT notifications.
2. **Brittle Workarounds Needed in Chrome:** `SS2K` previously needed a `silent.wav` audio loop, MediaSession spoofing, `wakeLock`, repeated `watchAdvertisements()`, and a full SPIFFS `/wo.bin` binary Flight Recorder + telemetry HR passback (`HR:<bpm>`) just to survive a 45-minute Zone 2 ride.
3. **Audio Focus Conflict:** Playing `silent.wav` collides with Spotify, podcasts, or YouTube audio focus; turning it off (`Silence app audio`) re-enables Android BLE disconnects.

### The Permanent Root-Cause Architecture
Build **one** responsive web UI (HTML/CSS/TypeScript or modular ES6), bundled once and shipped in two ways:
1. **Primary Gym Daily Driver — Capacitor Android `.apk`:**
   - **Native OS Bluetooth (`@capacitor-community/bluetooth-le`):** Talks directly to Android's native `BluetoothGatt` stack — *not* Chrome's tab-bound Web Bluetooth API.
   - **Remembered Device IDs (`deviceId`):** Stores the bonded/last MAC address per slot (`spin`, `airbike`, `treadmill`, `hrm`) in local preferences so `BleClient.connect(deviceId, ...)` reconnects silently with zero browser picker dialog.
   - **Custom Lightweight `WorkoutForegroundService.kt` (~60 lines in `android/app/src/main/java/...`):**
     - No paid third-party background-runner plugins required.
     - Starts when `START` is tapped (and stops on `FINISH` / `DISCARD`).
     - Acquires a `PARTIAL_WAKE_LOCK` and displays a persistent live Android status-bar / lockscreen notification (`WORKOUT ACTIVE • 18:42 • 129 W • 134 BPM`).
     - Because Android treats an active `foregroundServiceType="connectedDevice"` app identically to Wahoo/Strava/Garmin, you can lock your phone, text, check email, or play Spotify/YouTube with **zero BLE disconnects** and **zero `silent.wav` hacks**.
2. **Bonus Desktop/Backup Mirror — GitHub Pages (`https://davefroriep.github.io/armory/`):**
   - A thin **`BleTransport` interface** swaps implementations at runtime via `Capacitor.isNativePlatform()`:
     - Android APK -> `CapacitorBleTransport` (`@capacitor-community/bluetooth-le`)
     - Desktop Chrome -> `WebBluetoothTransport` (`navigator.bluetooth`)

### Required Android 12+ (`API 31+`) Manifest Permissions (`AndroidManifest.xml`)
- `android.permission.BLUETOOTH_SCAN` (`android:usesPermissionFlags="neverForLocation"`)
- `android.permission.BLUETOOTH_CONNECT`
- `android.permission.FOREGROUND_SERVICE`
- `android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE` (`service android:foregroundServiceType="connectedDevice"`)
- `android.permission.POST_NOTIFICATIONS` (requested cleanly at runtime on Android 13+)
- `android.permission.WAKE_LOCK`
- `android.permission.INTERNET` (only for Intervals.icu TCX upload on finish/retry)

---

## 3. Recording Ownership & External Zwift / FTMS Workflow

### Phone-Side 1 Hz `IndexedDB` Recording Replaces ESP32 `/wo.bin`
Once BLE runs inside a native Android Foreground Service, storing binary logs on the ESP32 SPIFFS is redundant and fragments Polar H10 data:
- **Phone Owns the Complete Workout Log:** Every `1,000 ms`, Armory writes one unified sample (`{ t, watts, cad, speedMph, inclinePct, elevGainM, distM, kcal, hr, targetW }`) straight into **`IndexedDB`**:
  - `workouts_meta` store: `{ id, equipment, mode, title, startEpoch, lastTickEpoch, status: 'active'|'paused'|'completed'|'pending_upload', summary }`
  - `workout_samples` store: sequential 1 Hz samples keyed by `[workoutId, t]`
- **Crash / Reboot Safety:** If the phone battery dies or restarts mid-Zone 2, every second written up to the last tick is already safely in `IndexedDB`. On next launch, Armory detects the incomplete `active`/`paused` workout and offers **`Resume` | `Finish & Sync` | `Discard`**.
- **ESP32s Become Simple Sensor/Actuator Nodes:** ESP32s keep simple cumulative hardware counters (`pulses`, `distance_m`) in RAM during a ride so even a transient 1-second RF packet drop never loses total distance, and host only lightweight **`/dev` calibration & OTA** web tools over Wi-Fi.

### What About Zwift / External FTMS Apps?
- **Clean Rule:** When riding Zwift, Rouvy, or MyWhoosh, **leave Armory closed** — Zwift connects straight to the machine's ESP32 over standard BLE FTMS (`0x1826`):
  - **AirBike (`AssaultBike`):** *Already* broadcasts standard BLE FTMS (`0x1826`) + CSC (`0x1816`).
  - **Treadmill (`Horizon T101 Pod`):** *Already* broadcasts standard BLE FTMS (`0x1826`) + RSC (`0x1814`).
  - **Spin (`SS2K_Tuner`):** Currently broadcasts Nordic UART (`NUS`) only; Phase 3 adds standard BLE FTMS (`0x2AD2` Indoor Bike Data + `0x2AD9` Control Point for ERG/sim grade) in `SS2K Head`.

---

## 4. Athlete Profile, Master Selector & Workout Modes (`i758105`)

### Top Equipment & HR Bar
- **Master Equipment Toggle:** **`SPIN`** (Bowflex C7) | **`AIRBIKE`** (Assault Elite) | **`TREADMILL`** (Horizon T101)
- **Always-On Heart Rate Slot:** **`POLAR H10`** (`0x180D`) pairs independently and feeds live BPM + `% HRmax` zones (`HRmax` configurable, default `180 BPM`) across all 3 machines.

### Workout Programs per Equipment Type

| Mode | Spin (`Bowflex C7`) | AirBike (`Assault Elite`) | Treadmill (`Horizon T101`) |
| :--- | :--- | :--- | :--- |
| **Zone 2 Base** | Auto-ERG **`129 W`** (`128–130 W` target band); default 5m Warmup + 45m Z2 + 5m Cooldown | Pace Cue **`44 RPM`** (`~127–134 W` Assault table); target RPM bar + Music BPM (`2×RPM`) | Heart-Rate & Pace Guided **`Zone 2 (60–70% HRmax)`** walk/jog + incline tracking |
| **Zone 5 Norwegian 4×4** | Auto-ERG Warmup (`129 W`) → **4 × (`4:00 @ 220 W` [218–225 W] / `3:00 @ 105 W`)** → Cooldown (`105 W`) | **4 × (`4:00 @ 54–55 RPM` [`218–228 W`] / `3:00 @ ~40 RPM` [`~99 W`])** with phase audio/vibrate alerts | **4 × (`4:00 Work @ 85–95% HRmax` / `3:00 Active Recovery`)** with Work/Rest split tracking & incline elevation |
| **Quick / Custom / HIIT** | **Quick ERG** (`±5W / ±10W`) or **1–100 Manual Gears** (`±1 / ±5 LVL`), + handlebar button sync | **Quick Start**, **Tabata 20/10**, **Interval 10/20**, **Target Calories**, **Target Time** | **Quick Run / Walk**, Custom Timed Intervals, live `[Tare 0%]` incline button |
| **Intervals.icu Activity Type** | `Sport="Biking"` (`VirtualRide`) | `Sport="Biking"` (`VirtualRide`) | `Sport="Running"` (`VirtualRun`) with `RunCadence`, `Speed`, `AltitudeMeters` |

---

## 5. Byte-Exact BLE Protocol Contracts (Verified from Hardware Firmware)

> **Android WebView / Capacitor Endianness Note:** Every multi-byte BLE GATT integer below is **Little-Endian (`true` in `DataView.getUint16(off, true)`)** unless specified. Notice also that `@capacitor-community/bluetooth-le` passes `DataView` objects into notification callbacks identically to Web Bluetooth.

### 5.1 Heart Rate Strap — Polar H10 (Standard SIG)
- **Service:** `0000180d-0000-1000-8000-00805f9b34fb` (`heart_rate`)
- **Notify Characteristic:** `00002a37-0000-1000-8000-00805f9b34fb` (`heart_rate_measurement`)
- **Payload Decode:**
  - `flags = dv.getUint8(0)`
  - `hr = (flags & 0x01) ? dv.getUint16(1, true) : dv.getUint8(1)`
- **Optional Battery Read:** Service `0000180f-...`, Char `00002a19-...` -> `dv.getUint8(0)` (`0–100%`).

---

### 5.2 Equipment 1: Spin — Bowflex C7 / `SS2K_Tuner`
Source verified: `N:\Documents\Antigravity\SS2K Head\ss2k_install_mode\src\main.cpp` & `c7_power_model.h`
- **Scan Filter:** Name prefix `SS2K` (`"SS2K_Tuner"`) or Service UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- **Service (Nordic UART / NUS):** `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- **TX Notify (ESP32 → Phone, 5 Hz):** `6e400003-b5a3-f393-e0a9-e50e24dcca9e` (MTU 517, chunked, `\n`-delimited JSON)
- **RX Write (Phone → ESP32):** `6e400002-b5a3-f393-e0a9-e50e24dcca9e` (ASCII line + `\n`)

**5 Hz TX Telemetry JSON Schema (`main.cpp:839-845`):**
- `cad` (`float`): Pedal cadence in RPM (calibrated `flywheelRatio = 4.50`)
- `ew` (`int`): Estimated C7 console Watts (`C7PowerModel::calculateWatts(lvl, cad)`)
- `lvl` (`float`): Current estimated C7 resistance level (`3.0–100.0`)
- `tlvl` (`int`): Integer stepper `targetLevel` (`1–100`)
- `mode` (`0|1`): Active hardware control mode (`0 = Gears`, `1 = ERG`)
- `tgt_w` (`int`): Firmware ERG target Watts (`0–800`; updates if user presses physical handlebar `UP`/`DOWN` GPIO 32/26 buttons!)
- `pot` (`int`), `v` (`float`), `pos` (`long`), `sg` (`int`), `stall` (`0|1`), `run` (`0|1`): Stepper & ADC diagnostics
- `pls` (`ulong`), `ratio` (`float`): Hall pulse count & pulley ratio (`4.50`)
- `wo`, `ws`, `wp`, `rev`, `wk`, `mw`, `auto`: Legacy SPIFFS workout fields

**RX Command Protocol (`executeCommand`, `main.cpp:458-545`):**
- `SET_MODE:0` (Gears) / `SET_MODE:1` (ERG)
- `SET_WATTS:<0..800>`
- `SET_LEVEL:<1..100>`
- `SET_RATIO:<0.1..20.0>`
- `RESET_PULSES`
- `WO_START:<unixEpochSec>`, `WO_PAUSE`, `WO_RESUME`, `WO_DISCARD`
- Calibration/Recovery commands (reserved for Spin `/dev` view): `JOG:<steps>`, `ZERO`, `UNWEDGE_FWD`, `UNWEDGE_REV`, `SET_MA:<100..1200>`

> **CRITICAL FIRMWARE GOTCHA (`SS2K main.cpp:745`):**  
> Currently, `bool isErgActive = (controlMode == 1 && woState == WO_ACTIVE);`! That means the SS2K stepper closed-loop ERG controller and ±5 W handlebar buttons ONLY engage when `woState == WO_ACTIVE`.  
> - **Immediate Armory Driver Rule:** When connecting in ERG mode (`Z2`, `4x4`, `Quick ERG`), Armory MUST send `WO_START:<epoch>\n` right before `SET_MODE:1\n` + `SET_WATTS:<w>\n`, and send `WO_DISCARD\n` on workout completion/disconnect so SPIFFS `/wo.bin` stays empty while Phone `IndexedDB` owns the real recording.  
> - **Phase 3 Firmware Cleanup:** Decouple `isErgActive` from `woState` in `SS2K` firmware (`isErgActive = (controlMode == 1)`), remove SPIFFS workout bloat, and add native BLE FTMS (`0x1826` `0x2AD2` + `0x2AD9` Control Point) for direct Zwift compatibility.

---

### 5.3 Equipment 2: AirBike — Assault Elite / `AssaultBike`
Source verified: `N:\Documents\Antigravity\Smart Airbike\PlatformIO\include\airbike_math.h`
- **Scan Filter:** Name `"AssaultBike"` or Service UUID `00001826-...`
- **FTMS Service (`0x1826`):** `00001826-0000-1000-8000-00805f9b34fb`
  - **Indoor Bike Data (`0x2AD2`, Notify @ 2 Hz / 500 ms):** 8 bytes fixed payload:
    - Bytes `0..1` (`u16LE`): Flags = `0x0044` (Bit 0 = `0` => Speed present; Bit 2 = `1` => Cadence present; Bit 6 = `1` => Instantaneous Power present)
    - Bytes `2..3` (`u16LE`): `speedRaw` -> `speedKmh = speedRaw / 100.0` (`speedMph = speedKmh * 0.621371`)
    - Bytes `4..5` (`u16LE`): `cadRaw` -> `cadenceRpm = cadRaw / 2.0`
    - Bytes `6..7` (`s16LE`): `powerRaw` -> `watts = powerRaw`
- **CSC Service (`0x1816`, Secondary / Backup):** `00001816-0000-1000-8000-00805f9b34fb`
  - **CSC Measurement (`0x2A5B`, Notify @ 500 ms):** 11 bytes (`flags = 0x03`, `u32LE` fan wheel revs @ byte 1, `u16LE` wheel time 1/1024s @ byte 5, `u32` truncated `u16LE` crank revs @ byte 7, `u16LE` crank time @ byte 9).
- **Kinematics & Official Lookup Curve (Firmware Compute, NVS-Backed):**
  - Default `fanToCrankRatio = 2.875` (46T:16T single stage; calibrated via `http://assaultbike.local/dev` 100-rev tally), `speedMphFactor = 0.3833` (`23.0 MPH @ 60 RPM`), `cal/min = watts * 0.04035`.
  - **5-RPM Reference Curve (`0..100 RPM`):**  
    `15=13W, 20=21W, 25=33W, 30=50W, 35=71W, 40=99W, 45=134W, 50=176W, 55=228W, 60=288W, 65=359W, 70=442W, 75=536W, 80=644W, 85=763W, 90=896W, 95=1043W, 100=1205W`
  - **Key Training Markers:** Zone 2 (`128–130 W`) ≈ **`44 RPM`** · Cycling FTP (`182 W`) ≈ **`50.6 RPM`** · Norwegian 4×4 (`218–225 W`) ≈ **`54–55 RPM`**.

---

### 5.4 Equipment 3: Treadmill — Horizon T101 / `Horizon T101 Pod`
Source verified: `N:\Documents\Antigravity\Smart Treadmill\PlatformIO\src\main.cpp`
- **Scan Filter:** Name `"Horizon T101 Pod"` (prefix `"Horizon"`) or Service UUID `00001826-...` / `00001814-...`
- **FTMS Service (`0x1826`):** `00001826-0000-1000-8000-00805f9b34fb`
  - **Treadmill Data (`0x2ACD`, Notify @ 1 Hz):** 8 bytes fixed payload (`main.cpp:828-845`):
    - Bytes `0..1` (`u16LE`): Flags = `0x0008` (Instantaneous Speed present + Inclination & Ramp Angle Setting present)
    - Bytes `2..3` (`u16LE`): `ftmsSpeed` -> `speedKmh = ftmsSpeed / 100.0`, `speedMph = speedKmh * 0.621371`, `speedMs = speedKmh / 3.6`
    - Bytes `4..5` (`s16LE`): `ftmsIncline` -> `rawInclinePct = ftmsIncline / 10.0` (`0.0–15.0 %`, snapped to `0.5%` steps)
    - Bytes `6..7` (`s16LE`): `ftmsRamp` -> `rampDeg = ftmsRamp / 10.0`
- **RSC Service (`0x1814`):** `00001814-0000-1000-8000-00805f9b34fb`
  - **RSC Measurement (`0x2A53`, Notify @ 1 Hz):** 4 bytes (`main.cpp:848-856`):
    - Byte `0`: Flags = `0x00`
    - Bytes `1..2` (`u16LE`): `rscSpeed` (`m/s * 256`)
    - Byte `3` (`u8`): `cadenceSpm` (runner cadence in steps/min from MPU-6050 shockwave detector)
- **Treadmill Software Helpers inside Armory:**
  - **Client-Side `[TARE 0%]`:** Since the ESP32 `0x2ACD` currently has no BLE write tare characteristic (HTTP `/api/tare` only), tapping `[TARE 0%]` in Armory immediately snapshots `tareOffsetPct = rawInclinePct` (`effectiveInclinePct = Math.max(0, rawInclinePct - tareOffsetPct)`), working over pure BLE with zero Wi-Fi needed.
  - **Distance & Elevation Integration (1 Hz):** `distM += speedMs * dt`, `elevGainM += speedMs * Math.max(0, effectiveInclinePct / 100.0) * dt`.

---

## 6. Intervals.icu TCX Upload & Security Hygiene

- **Endpoint:** `POST https://intervals.icu/api/v1/athlete/{athleteId}/activities?name={title}&description={desc}`
- **Auth Header:** `Authorization: Basic ` + `btoa("API_KEY:" + apiKey)`
- **Payload Format:** `.tcx` (`multipart/form-data`, field `file`):
  - **Spin / AirBike (`<Activity Sport="Biking">`):** Trackpoints include `<Time>`, `<Cadence>`, `<HeartRateBpm><Value>`, `<DistanceMeters>` (AirBike), and `<Extensions><ns3:TPX><ns3:Watts>`.
  - **Treadmill (`<Activity Sport="Running">`):** Trackpoints include `<Time>`, `<DistanceMeters>`, `<AltitudeMeters>` (cumulative `elevGainM` from 0 m baseline), `<HeartRateBpm><Value>`, and `<Extensions><ns3:TPX><ns3:Speed>` (`m/s`) + `<ns3:RunCadence>` (half or full SPM consistent with Garmin TCX convention — test once in Intervals.icu!).
- **Retry Queue:** Failed uploads stay marked `pending_upload` in `IndexedDB` with a 1-tap `Sync Pending Workouts` button in Armory History/Settings (essential if gym Wi-Fi drops briefly).
- **STRICT SECRETS RULE:** Never hardcode an Intervals.icu API key or home Wi-Fi password in `.html`/`.ts`/`.js` committed to GitHub. Store `intervalsApiKey` exclusively in encrypted/local `Capacitor Preferences` (or `localStorage`) entered in Settings on the device. *(Action item: rotate or scrub the hardcoded fallback key in legacy `SS2K Head/cockpit_pwa/index.html`.)*

---

## 7. Automated Build & Deployment Pipeline (GitHub Actions)

You do **not** need to fight local Android Studio SDK updates every time you edit a screen:
1. Push `N:\Documents\Antigravity\Armory` to **`davefroriep/armory`** (private or public — private is recommended for personal APK builds; note GitHub Pages free tier on personal accounts may require a public repo or keep `gh-pages` public with zero secrets!).
2. **`.github/workflows/build.yml`** runs on every push to `main`:
   - `npm ci && npm run build && npx cap sync android`
   - Runs `./gradlew assembleDebug` (using Java 17 + Android SDK preinstalled on `ubuntu-latest`)
   - Uploads `Armory-latest.apk` as a downloadable **GitHub Actions Artifact / Release** (install straight onto your Pixel in ~10 seconds)
   - Simultaneously publishes `dist/` to **GitHub Pages** (`https://davefroriep.github.io/armory/`).

---

## 8. Migration Roadmap (From 3 Discrete Projects → Unified Ecosystem)

| Phase | Target Repo | Objective | Outcome |
| :--- | :--- | :--- | :--- |
| **Phase 1: Foundation** *(Current)* | `Smart Airbike` + `Armory` | Flash waiting AirBike C6 firmware (`/`, `/dev`, official wattage curve), run 100-rev pedal tally to lock `fan_to_crank_ratio`. Build `Armory` MVP (`Vite` + `Capacitor` + `BluetoothLE` + `WorkoutForegroundService` + `IndexedDB` + Polar H10 + AirBike & Treadmill drivers). | Pixel runs `Armory.apk`; screen-off Z2 & 4×4 workouts never drop BLE; AirBike & Treadmill sync cleanly to Intervals.icu. |
| **Phase 2: Spin Integration** | `Armory` + `SS2K Head` | Add `SpinAdapter` (`SS2K_Tuner` NUS driver) into Armory (`Z2 129W`, `4×4 220W/105W`, `Quick ERG`, `1–100 Gears`, handlebar button sync). Initially send `WO_START` internally to engage `SS2K` ERG loop cleanly. | Single Pixel app now replaces *both* `SS2K` cockpit and `Treadmill` cockpit PWAs and adds `AirBike`. |
| **Phase 3: Firmware Slim-Down & FTMS** | `SS2K Head` (+ minor `Treadmill`) | In `SS2K`: decouple `isErgActive` from `woState`, strip SPIFFS `/wo.bin` flight-recorder complexity if desired, and add standard **BLE FTMS (`0x2AD2` + `0x2AD9` Control Point)** so **Zwift** talks directly to C7. In `Treadmill`: optional BLE `Tare` write command. Keep ESP32 `/dev` web tools (`assaultbike.local/dev`, `treadmill.local`, `SS2K` tuner) solely for bench calibration & OTA. | Hardware nodes become lean, crash-proof telemetry bridges; Zwift connects directly over FTMS with zero phone middleman; Armory handles all Z2/Z5/custom training. |
| **Phase 4: Polish** | `Armory` | Dedicated per-machine Calibration/Config drawer (or deep-link hints to `/dev`), workout history viewer with `.tcx` download + Intervals retry queue, voice/chime TTS prompts (`"Interval 2: Push to 220 Watts"` over Bluetooth headphones without killing Spotify!). | Complete production gym console on your Pixel. |

---

## 9. Copy-Paste Kickoff Prompt for Your New `Armory` App Thread

> *"I am starting the unified **Armory** fitness app workspace in `N:\Documents\Antigravity\Armory`. Read `GEMINI.md` and `docs/ARMORY_MASTER_PRIMER.md` first so you have the exact Capacitor + Android Foreground Service architecture, IndexedDB 1 Hz recorder design, and byte-exact BLE GATT contracts for Polar H10, Spin (SS2K_Tuner), AirBike (AssaultBike), and Treadmill (Horizon T101 Pod). Let's scaffold the `Vite + TypeScript + Capacitor` app, implement the custom `WorkoutForegroundService.kt`, the `BleTransport` abstraction, the `IndexedDB` session recorder + crash recovery, the machine adapters, and the GitHub Actions `.apk` build workflow."*
