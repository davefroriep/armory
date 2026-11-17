# Armory Fit (`N:\Documents\Antigravity\Strength and Conditioning`)

- **APK Only:** Native Android `.apk` (`com.froriep.armoryfit`), never GitHub Pages PWA. Release URL: `https://github.com/davefroriep/armory/releases/latest/download/armory-fit.apk`.
- **Skill & Docs:** Activate `C:\Users\davef\.gemini\config\skills\armory-fit\SKILL.md` (`armory_cli.py`) for lookups/programming/migrations. Tech ref: `docs/ARMORY_FIT_TECHNICAL_REFERENCE.md` (update only when screens/schemas/behavior change). Roadmap: `docs/TODO.md`.
- **Local Truth & Migrations:** Phone `IndexedDB` (`armory-fit` v2) + `localStorage` (`armory.settings.v1`, `armory.lifts.settings.v1`). Editing `src/lifts/seed.ts` requires a `localStorage`-guarded migration in `src/lifts/store.ts` (`LiftStore.load()`).
- **Cloud & Backup:** Intervals.icu (`i758105`) is post-workout logbook + MyWhoosh `fetchActivities` only (no planned-event staging; secrets stay out of git). Full backups auto-write to phone `Documents/ArmoryFit Backup/` for DriveSync -> Drive `Backups/ArmoryFit Backup`.
- **Cardio & Effort:** 5 Hz BLE NUS (`6E400001-...`, passive FTMS); AirBike `ratio=11.1100`; FTP `182 W`, Z2 `128–130 W`, 4×4 `218–225 W`; RPE `3–10`.
- **Verify & Release:** `npx tsc --noEmit && npm test` -> `npm run build && npm test` -> `git commit` + `git push origin main` -> `.\scripts\android.ps1 -NoBump -Publish`.
