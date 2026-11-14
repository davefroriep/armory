# Armory Master Cardio & Telemetry App

## 1. Target Architecture & Deployment
- **Primary Client:** Native Android `.apk` wrapped with **Capacitor** (`@capacitor-community/bluetooth-le` + custom Kotlin `WorkoutForegroundService`) for lockscreen/background-safe BLE and persistent notification telemetry.
- **Web Mirror:** Identical PWA bundle deployed to GitHub Pages (`https://davefroriep.github.io/armory/`) using Web Bluetooth fallback on Desktop Chrome.
- **Storage & Sync:** Phone-side 1 Hz `IndexedDB` workout recording (`workouts_meta` + `workout_samples`) with crash/resume recovery and TCX export to Intervals.icu.

## 2. BLE Peripheral Targets & Athlete Constants
- **Spin (`Bowflex C7` / `SS2K_Tuner`):** BLE NUS `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` (`TX ...0003`, `RX ...0002`; future FTMS `0x1826` / `0x2AD2` + `0x2AD9`).
- **AirBike (`AssaultBike`):** BLE FTMS `0x1826` (`0x2AD2` Indoor Bike Data) + CSC `0x1816` (`0x2A5B` CSC Measurement).
- **Treadmill (`Horizon T101 Pod`):** BLE FTMS `0x1826` (`0x2ACD` Treadmill Data) + RSC `0x1814` (`0x2A53` RSC Measurement).
- **Heart Rate (`Polar H10`):** BLE HR `0x180D` (`0x2A37` HR Measurement) + Battery `0x180F` (`0x2A19`).
- **Athlete Profile (Intervals.icu `i758105`):** FTP `182 W`, Zone 2 Base `128–130 W`, Norwegian 4×4 VO2 Max `218–225 W` (API key stored strictly in local device settings, NEVER committed to git).

## 3. Canonical Directory Structure
- `src/`: Modular TypeScript/JS UI, BLE transport adapters (`capacitor` / `web`), `IndexedDB` recorder, workout protocols, and TCX/Intervals uploader.
- `public/`: PWA manifest, icons (`192`, `512`), and service worker (`sw.js`).
- `android/`: Generated Capacitor Android project + custom Kotlin `WorkoutForegroundService`.
- `docs/`: `ARMORY_MASTER_PRIMER.md` and protocol/architecture specs.
- `.github/workflows/`: Automated CI build for Android `.apk` releases + GitHub Pages deployment.
*(Root policy: Keep build scripts in `package.json` or `scripts/`; zero hardcoded API keys or Wi-Fi credentials in repo.)*
