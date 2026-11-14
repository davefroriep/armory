# Strength & Conditioning Hub ("The Armory" + Armory Fit App)

## 1. Umbrella Ecosystem & Deployment
- **Workspace Scope:** Umbrella S&C hub (`N:\Documents\Antigravity\Strength and Conditioning`) unifying **Boostcamp** (strength programming), **Intervals.icu** (`i758105` endurance/wellness telemetry), and **Armory Fit** (cardio telemetry app).
- **Primary Client (Armory Fit):** Native Android `.apk` wrapped with **Capacitor** (`@capacitor-community/bluetooth-le` + custom Kotlin `WorkoutForegroundService`) for lockscreen/background-safe BLE and persistent notification telemetry, plus GitHub Pages web mirror (`https://davefroriep.github.io/armory/`).
- **Storage & Sync:** Phone-side 1 Hz `IndexedDB` workout recording (`workouts_meta` + `workout_samples`) with crash/resume recovery and TCX export to Intervals.icu.

## 2. Primary 5 Hz Direct BLE (`NUS`) Protocol & Athlete Constants
- **Unified Direct BLE (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`):** Primary 5 Hz (`200 ms`) unquantized JSON `TX ...0003` + `RX ...0002` control/calibration across **all 3 machines** (`Spin`, `AirBike`, `Treadmill`); standard **BLE FTMS (`0x1826`)** runs passively only for external apps (e.g. **MyWhoosh**).
- **Spin (`Bowflex C7` / `SS2K_Tuner`):** 5 Hz NUS JSON + `SET_MODE` / `SET_WATTS` / `SET_LEVEL` / `SET_RATIO` (+ Stage 2 Power Pedal calibration & Stage 3 passive FTMS for MyWhoosh).
- **AirBike (`AssaultBike` `Rev v1.5`):** Primary 5 Hz NUS JSON (`w`, `cad`, `fan`, `pls`, `dt`, `SET_RATIO` [`11.1100`], `RESET`) + passive FTMS (`0x1826`) / CSC (`0x1816`).
- **Treadmill (`Horizon T101 Pod`):** Planned 5 Hz NUS stream (`spd_mph`, `inc_pct`, `cad_spm`, `TARE_INCLINE`) alongside existing passive FTMS (`0x2ACD`) / RSC (`0x2A53`).
- **Heart Rate (`Polar H10`) & Profile:** BLE HR `0x180D` (`0x2A37`); `i758105` FTP `182 W`, Z2 `128–130 W`, 4×4 `218–225 W` (API key stored strictly in local device settings, NEVER committed to git).

## 3. Canonical Directory Structure
- `src/`: Modular TypeScript/JS UI, BLE transport adapters (`capacitor` / `web`), `IndexedDB` recorder, workout protocols, and TCX/Intervals uploader.
- `public/`: PWA manifest, icons (`192`, `512`), and service worker (`sw.js`).
- `android/`: Generated Capacitor Android project + custom Kotlin `WorkoutForegroundService`.
- `docs/`: `ARMORY_MASTER_PRIMER.md` and S&C / BLE protocol specs.
- `.github/workflows/`: Automated CI build for Android `.apk` releases + GitHub Pages deployment.
*(Root policy: Keep build scripts in `package.json` or `scripts/`; zero hardcoded API keys or Wi-Fi credentials in repo.)*
