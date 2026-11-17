# Strength & Conditioning Hub ("The Armory" + Armory Fit App)

## 1. Umbrella Ecosystem & Deployment
- **Workspace Scope:** Umbrella S&C hub (`N:\Documents\Antigravity\Strength and Conditioning`) unifying **Boostcamp** (strength programming), **Intervals.icu** (`i758105` endurance/wellness telemetry), and **Armory Fit** (cardio telemetry + built-in Strength Lift Tracker).
- **Primary Client (Armory Fit):** Native Android `.apk` ONLY wrapped with **Capacitor** (`@capacitor-community/bluetooth-le` + native `WorkoutForegroundService` / `WorkoutChime` / `RestOverlay`). **No PWA deployment or PWA dev-testing** — always deliver the direct GitHub Release APK link (`https://github.com/davefroriep/armory/releases/latest/download/armory-fit.apk`).
- **Storage & Sync:** Phone-side 1 Hz `IndexedDB` (`workouts_meta`, `workout_samples`, plus v2 lift-tracker stores `lift_sessions`, `lift_routines`, `lift_ex`, `lift_videos`) with crash/resume recovery and automatic TCX/manual `WeightTraining` sync to Intervals.icu.

## 2. Primary 5 Hz Direct BLE (`NUS`) Protocol & Athlete Constants
- **Unified Direct BLE (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`):** Primary 5 Hz (`200 ms`) unquantized JSON `TX ...0003` + `RX ...0002` control/calibration across **all 3 machines** (`Spin`, `AirBike`, `Treadmill`); standard **BLE FTMS (`0x1826`)** runs passively only for external apps (e.g. **MyWhoosh**).
- **Spin (`Bowflex C7` / `SS2K_Tuner`):** 5 Hz NUS JSON + `SET_MODE` / `SET_WATTS` / `SET_LEVEL` / `SET_RATIO` (+ Stage 2 Power Pedal calibration & Stage 3 passive FTMS for MyWhoosh).
- **AirBike (`AssaultBike` `Rev v1.5`):** Primary 5 Hz NUS JSON (`w`, `cad`, `fan`, `pls`, `dt`, `SET_RATIO` [`11.1100`], `RESET`) + passive FTMS (`0x1826`) / CSC (`0x1816`).
- **Treadmill (`Horizon T101 Pod`):** Planned 5 Hz NUS stream (`spd_mph`, `inc_pct`, `cad_spm`, `TARE_INCLINE`) alongside existing passive FTMS (`0x2ACD`) / RSC (`0x2A53`).
- **Heart Rate (`Polar H10`) & Profile:** BLE HR `0x180D` (`0x2A37`); `i758105` FTP `182 W`, Z2 `128–130 W`, 4×4 `218–225 W` (API key stored strictly in local device settings, NEVER committed to git).
- **Z5 Suite & Tactical HUD:** `[ZONE 5 VO2]` 4-protocol drawer (`Norwegian 4×4`, `Classic VO2 5×3`, `Cardiac Power 6×2`, `Billat/Rønnestad 60/30`) + 5 Hz Sweet-Spot Horizon HUD, split/lookahead timers, and auto-muting WebAudio `2×RPM` Lock-In Transition Metronome + Overshoot Chirp.

## 3. Canonical Directory Structure
- `src/`: Modular TypeScript/JS UI (`src/ui/`, `src/ui/lifts/`), lift-tracker domain/store (`src/lifts/`), BLE transport (`capacitor` / `web`), `IndexedDB` recorder, cardio protocols, and Intervals uploader.
- `public/`: PWA manifest, icons (`192`, `512`), and service worker (`sw.js`).
- `android/`: Generated Capacitor Android project + custom Kotlin `WorkoutForegroundService`.
- `docs/`: `ARMORY_MASTER_PRIMER.md` and S&C / BLE protocol specs.
- `.github/workflows/`: CI checks/manual Pages workflow (disabled on push — APK releases only).
*(Root policy: Keep build scripts in `package.json` or `scripts/`; zero hardcoded API keys or Wi-Fi credentials in repo.)*

## 4. Build & Android APK Workflow (No PWA)
- Tests & checks: `npx tsc --noEmit && npm test` (plus local UI check if UI changed). Do NOT deploy, watch, verify, or link GitHub Pages PWA.
- Build & publish APK: `.\scripts\android.ps1 -Publish` (bumps `rev.json`, builds Vite bundle, syncs Capacitor, builds `app-debug.apk` with portable JDK 21 in `C:\Users\davef\Tools\jdk-21` + SDK in `%LOCALAPPDATA%\Android\Sdk`, uploads `armory-fit.apk` to GitHub Release `vX.Y`). Add `-Install` for local USB adb.
- One-time Intervals.icu hookup: `python scripts\make_setup_qr.py` -> scan QR (key never in repo; `local-secrets/` is git-ignored).
- Names: `Assault AirBike Elite` (Assault Fitness). Effort scale = 8 words -> RPE 3-10.
