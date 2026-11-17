# Strength & Conditioning Hub ("The Armory" + Armory Fit App)

## 1. Umbrella Ecosystem & Deployment (APK Only)
- **Workspace Scope:** Umbrella S&C hub (`N:\Documents\Antigravity\Strength and Conditioning`) hosting **Armory Fit**, a native Android Capacitor app combining **Cardio Cockpit** (AirBike, Spin, Treadmill + Polar H10) with **Built-in Strength Lift Tracker** & **Local Rolling Queue**.
- **Primary & Only Client:** Native Android `.apk` (`com.froriep.armoryfit`) with native `WorkoutForegroundService`, `WorkoutChime`, `RestOverlay`, and `forceClose`. **No PWA deployment or PWA dev-testing** — always deliver `https://github.com/davefroriep/armory/releases/latest/download/armory-fit.apk`.
- **Full Documentation & Agent Skill:** See `docs/ARMORY_FIT_TECHNICAL_REFERENCE.md` for every screen/module/protocol, and global skill `C:\Users\davef\.gemini\config\skills\armory-fit\SKILL.md` (+ `armory_cli.py`) for workout lookups, skipped sets, and program migrations.

## 2. Data, Intervals.icu Sync & BLE Architecture
- **Phone Local Source of Truth:** `IndexedDB` `armory-fit` v2 (`workouts_meta`, `workout_samples`, `lift_sessions` incl. `skipped`, `lift_routines`, `lift_ex`, `lift_videos`) + `localStorage` (`armory.settings.v1` rolling `QueueState`/`extras`/`skippedSlots`/`carryOver`, `armory.lifts.settings.v1`). **Never** edit `seed.ts` routines/notes for existing users without a `localStorage`-guarded migration in `LiftStore.load()` (`src/lifts/store.ts`).
- **Intervals.icu (`i758105`) Post-Workout Bridge:** Zero planned-event staging (`createEvent`/`moveEvent` removed in `v0.33`). Finished workouts auto-upload `.tcx` or `WeightTraining` logs (including `Skipped:` items in `v0.35+` via `intervalsDescription()`) plus read-only `fetchActivities` for external MyWhoosh reconciliation.
- **Primary 5 Hz Direct BLE (`NUS` `6E400001-...`):** Unquantized `200 ms` JSON across **AirBike (`AssaultBike` `Rev v1.5`, `ratio=11.1100`)**, **Spin (`SS2K_Tuner`)**, and **Treadmill (`Horizon T101 Pod`)** alongside Polar H10 (`0x180D`) & passive FTMS (`0x1826`) for MyWhoosh. Effort scale = 8 words (**RPE 3–10**); `i758105` FTP `182 W`, Z2 `128–130 W`, 4×4 `218–225 W`. (Keys stay in local storage/`intervals_creds.json`, never in git.)

## 3. Canonical Directory Layout
- `src/`: Core UI (`src/ui/`, `src/ui/lifts/`), domain logic (`src/domain/`, `src/lifts/`), BLE (`src/ble/`), IndexedDB/settings (`src/store/`), Intervals client/calendar (`src/intervals/`), native bridge (`src/native/`), audio/workout engine (`src/audio/`, `src/workout/`).
- `android/`: Capacitor Android project + Kotlin/Java `WorkoutServicePlugin`, `WorkoutForegroundService`, `WorkoutNotif`, `WorkoutChime`, `RestOverlay`.
- `docs/`: `ARMORY_FIT_TECHNICAL_REFERENCE.md`, `ARMORY_MASTER_PRIMER.md`, `LIFT_TRACKER.md`.
- `scripts/` & `tests/`: `android.ps1`, `bump-rev.mjs`, `make_setup_qr.py`, 8 `vitest` test suites (`tests/*.test.ts`).

## 4. Verification, Build & APK Release Workflow
- **Verify:** `npx tsc --noEmit && npm test` -> `npm run build && npm test` (bumps `rev.json`, `src/rev.ts`, `public/sw.js`).
- **Commit & Publish APK:** `git add -A && git commit ... && git push origin main && .\scripts\android.ps1 -NoBump -Publish` (add `-Install` for USB adb). One-time QR setup: `python scripts\make_setup_qr.py`.
