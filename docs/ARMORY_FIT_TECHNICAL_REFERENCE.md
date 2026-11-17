# Armory Fit — Complete Technical & Programmer Reference (`v0.39+`)

Authoritative engineering specification for **Armory Fit** (`N:\Documents\Antigravity\Strength and Conditioning`, `com.froriep.armoryfit`), Dave Froriep's native Android Capacitor Strength & Conditioning application.

> Related docs:
> - Global Agent Skill: `C:\Users\davef\.gemini\config\skills\armory-fit\SKILL.md`
> - CLI Logbook Inspector: `C:\Users\davef\.gemini\config\skills\armory-fit\scripts\armory_cli.py`
> - Roadmap & Pending Hardware Upgrades: [TODO.md](./TODO.md)
> - A/V, Compute & Wall Display Setup: [ARMORY_AV_TECH_SETUP.md](./ARMORY_AV_TECH_SETUP.md)
> - Gym Equipment & Exercise Inventory: [ARMORY_EQUIPMENT_SETUP.md](./ARMORY_EQUIPMENT_SETUP.md)
> - Hardware & Ecosystem Primer: [ARMORY_MASTER_PRIMER.md](./ARMORY_MASTER_PRIMER.md)
> - Lift Tracker Notes: [LIFT_TRACKER.md](./LIFT_TRACKER.md)

---

## 1. Architecture & Data-Flow Topology

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        ANDROID PIXEL (com.froriep.armoryfit)               │
│  ┌──────────────────────┐    BLE GATT 5Hz NUS / HR     ┌────────────────┐ │
│  │ ESP32-C6 AssaultBike │─────────────────────────────▶│ @capacitor-    │ │
│  │ Polar H10 (0x180D)   │─────────────────────────────▶│ community/ble  │ │
│  └──────────────────────┘                              └───────┬────────┘ │
│                                                                ▼          │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │ WebView SPA (Vite + TypeScript)                                      │ │
│  │  • UI Screens (src/ui/*)   • LiftStore + ActiveLift (src/lifts/*)   │ │
│  │  • 1Hz Recorder (src/workout/recorder.ts)                            │ │
│  │  • Rolling Plan Queue + Projection (src/domain/plan.ts + calendar)   │ │
│  └────────────┬───────────────────────────────────────┬────────────────┘ │
│               │ Capacitor JS Bridge                   │ Local Storage    │
│               ▼                                       ▼                  │
│  ┌──────────────────────────────────┐   ┌──────────────────────────────┐ │
│  │ WorkoutForegroundService (Java)  │   │ IndexedDB `armory-fit` (v2)  │ │
│  │  • PARTIAL_WAKE_LOCK + Notif     │   │  workouts_meta               │ │
│  │  • WorkoutChime (MEDIA + duck)   │   │  workout_samples (1 Hz)      │ │
│  │  • RestOverlay (floating pill)   │   │  lift_sessions (w/ skipped)  │ │
│  │  • DCIM/Camera SAF video picker  │   │  lift_routines, lift_ex      │ │
│  │  • forceClose() hard exit        │   │  lift_videos (meta + blobs)  │ │
│  └──────────────────────────────────┘   │ + localStorage settings/queue│ │
│                                          └──────────────┬───────────────┘ │
└─────────────────────────────────────────────────────────┼──────────────────┘
                                                          │ REST API (Post-workout)
                                                          ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                INTERVALS.ICU (`https://intervals.icu/api/v1`)              │
│  • POST /athlete/{id}/activities (TCX: Cardio OR HR-backed Strength)       │
│  • POST /athlete/{id}/activities/manual (No-strap Strength)                │
│  • PUT  /activity/{id} (Type, lifts/reps + `Skipped:` desc, RPE, kg_lifted)│
│  • GET  /athlete/{id}/activities (Read-only MyWhoosh sync into QueueState) │
│  • ZERO pre-workout planned event staging (removed in v0.33)               │
└───────────────────────────────────────┬────────────────────────────────────┘
                                        │ Queried via `armory_cli.py`
                                        ▼
                       PC Antigravity Sessions (Offline from Phone)
```

### Key Architectural Principles
1. **Android APK Primary (`davefroriep/armory`):** Deployed exclusively as a native Android APK built with Vite -> Capacitor `cap sync android` -> Gradle debug APK -> GitHub Releases (`armory-fit.apk`). Legacy PWA artifacts (`pages.yml`, `manifest.json`, `sw.js`) are removed (`v0.37`).
2. **100% Local Schedule & Workout Truth:** Planned workouts, projections, skipped states, carried-over extras, day templates, and workout histories are evaluated synchronously at `0 ms` latency from phone `localStorage` + `IndexedDB`.
3. **Intervals.icu = Post-Workout Logbook & Cross-App Activity Bridge:** Finished Strength & Cardio workouts upload automatically once finished (with retry queue for offline gym sessions). Pre-workout `createEvent`/`moveEvent` staging was retired in `v0.33`. Intervals.icu also acts as the readable cloud store so PC Antigravity threads (`armory_cli.py`) can inspect completed sets and skipped exercises without USB ADB.

---

## 2. Complete Repository Directory & Module Inventory

### Root & Build Pipeline
| File | Purpose |
| :--- | :--- |
| [package.json](../package.json) | Scripts: `dev`, `build` (`bump-rev.mjs && tsc --noEmit && vite build`), `build:ci`, `typecheck`, `test` (`vitest run`). |
| [rev.json](../rev.json) | Canonical `{ "major": 0, "build": N }` version counter read by `bump-rev.mjs` and `android.ps1`. |
| [scripts/bump-rev.mjs](../scripts/bump-rev.mjs) | Increments `rev.json`, regenerates `src/rev.ts` (`REV`, `REV_STAMP`), updates `android/app/build.gradle` `versionCode` & `versionName`. |
| [scripts/android.ps1](../scripts/android.ps1) | Sets `JAVA_HOME` (`C:\Users\davef\Tools\jdk-21`), `ANDROID_HOME` (`%LOCALAPPDATA%\Android\Sdk`), runs web build + `npx cap sync android` + `.\gradlew.bat assembleDebug` (external build dir `%LOCALAPPDATA%/armory-fit-build`), copies APK, optional `-Install` (`adb install -r`) and `-Publish` (`gh.exe release` upload as `armory-fit.apk`). |
| [scripts/make_setup_qr.py](../scripts/make_setup_qr.py) | Generates one-time QR codes (`armory://setup?key=...`) without committing keys into git. |

### Top-Level App Shell (`src/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/main.ts](../src/main.ts) | Hash router (`routes` map: `''`, `program`, `airbike`, `spin`, `treadmill`, `strength`, `settings`, `wallboard`), initializes `voice.init()`, `initWallboardSync()`, `setLeaveGuard`, `allowNextRoute`, modal cleanup (`armory:dismiss`), `releaseBike()` on leaving `#/airbike`, native Android `appStateChange` (`kickSensors`, `calendar.checkDayRollover(true)`, `wallboard.refreshStatus()`) & hardware `backButton` stack. Launches background `retryPending()`. |
| [src/nav.ts](../src/nav.ts) | `parentHash(path)`: deterministic hierarchical Back tree (`#/strength/session` -> `#/strength`, `#/strength` -> `null` at `#/`). |
| [src/types.ts](../src/types.ts) | `Equipment` (`strength \| treadmill \| spin \| airbike`), `EQUIPMENT_LABEL`, `Sample` (`t, w, cad, hr, dist, kcal, tr`), `UploadState`, `WorkoutMeta`. |
| [src/emitter.ts](../src/emitter.ts) | Typed lightweight event pub/sub `Emitter<T>`. |
| [src/setup.ts](../src/setup.ts) | `initSetupLinks()`, `applySetup(key, aid)` handling `#setup=` fragments and `armory://setup?key=...` intents. |

### Persistence (`src/store/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/store/db.ts](../src/store/db.ts) | Opens IndexedDB `armory-fit` (version `2`). Object stores: `workouts_meta` (`id`), `workout_samples` (`[id, t]`), `lift_sessions` (`id`), `lift_routines` (`id`), `lift_ex` (`id`), `lift_videos` (`id`). Emits `metaSaved` on saved records. CRUD helpers incl. `putSamples()`. |
| [src/store/backup.ts](../src/store/backup.ts) & [src/ui/backupUi.ts](../src/ui/backupUi.ts) | Complete `armory-fit-backup` v2 engine (Strength + `skipped`, Cardio metas + 1 Hz samples, routines, `QueueState`, non-secret settings; omits `apiKey`). Auto-writes `armory-fit-latest.json` + daily `armory-fit-YYYY-MM-DD.json` to `Documents/ArmoryFit Backup/` for DriveSync -> Drive `Backups/ArmoryFit Backup` (`1Zdl0m9eAaZsPJRLI205Db4NT-Puui0iq`), plus 1-tap restore (`v2` & legacy `v1`). |
| [src/store/settings.ts](../src/store/settings.ts) | `Settings` interface & `DEFAULTS`, persisted in `localStorage` key `armory.settings.v1` (Node-safe `typeof localStorage !== 'undefined'` guard on `updateSettings`). Emits `settingsChanged`. |

### BLE, Sensors & Audio (`src/ble/`, `src/audio/`, `src/native/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/ble/transport.ts](../src/ble/transport.ts) | Unified transport selecting `CapacitorBleTransport` (`@capacitor-community/bluetooth-le`) natively vs Web Bluetooth in browsers. Persists MAC/device ids, handles `forgetAll()`. |
| [src/ble/link.ts](../src/ble/link.ts) | Reconnecting state machine `BleLink` (`idle \| scanning \| connecting \| connected \| lost \| unsupported`). Exponential reconnect backoff + freshness health checks. |
| [src/ble/devices.ts](../src/ble/devices.ts) | `hrLink` (`PolarLink`, service `0x180D`, char `0x2A37`, key `armory.ble.polar`) & `bikeLink` (`AirbikeLink`, device name `AssaultBike`, NUS `6e400001-...`, TX `6e400003-...`, RX `6e400002-...`, 6s silence timeout, `command()` helper). |
| [src/ble/parsers.ts](../src/ble/parsers.ts) | `parseHeartRate(DataView)`, `LineBuffer` newline frame assembler, `parseBikeLine(line)` returning `BikeFrame { w, cad, fan?, rfan?, dist, kcal, spd, pls?, dt?, ratio? }`. |
| [src/ble/telemetry.ts](../src/ble/telemetry.ts) | Live `live` snapshot (`hr`, `hrAt`, `w`, `cad`, `fan`, `rfan`, `dist`, `kcal`, `spd`, `pls`, `dt`, `ratio`, `bikeAt`), rolling ring buffers for `hrAtTime(ms)` lookup (`1 Hz` backfill fidelity), `recentHrPoints(windowMs)`, `currentStrapBeats()`, freshness helpers (`hrFresh`, `bikeFresh`), `hrBeat` & `bikeFrame` emitters. |
| [src/ble/sources.ts](../src/ble/sources.ts) | `ensureSensors({ bike })`, `kickSensors()`, `releaseBike()`, `chipView('hr' \| 'bike')`, `chipTap()`, `bikeStatusNote()`. Handles switching seamlessly to/from `sim` (`src/ble/sim.ts`). |
| [src/audio/audio.ts](../src/audio/audio.ts) | Routes `setMetronome()` / `setVolume()` natively via `setNativeMetronome` (`WorkoutMetronome.java` `SoundPool` on Android APK, avoiding WebView `AudioContext` suspension and `AudioFocus` ducking), with WebAudio oscillator fallback + overshoot chirp, PCM WAV generator (`synthesizeBellPcm`, `encodeWav`, `ALERT_SPECS`, `ALERT_SAMPLE_RATE`), haptic `buzz()`. |
| [src/native/service.ts](../src/native/service.ts) | Capacitor `registerPlugin('WorkoutService')` wrapper: `workoutService` (`start`, `update`, `stop`, `setRest`, `clearRest`), `canShowRestOverlay`, `requestRestOverlayPermission`, `openAppInfoSettings`, `onNativeRestSync`, `setKeepScreenOn`, `setNativeMetronome`, `startNativeWallboard` / `stopNativeWallboard` / `getNativeWallboardStatus` / `pushNativeWallboardState`, `setNativeVoiceListening` / `getNativeVoiceStatus` / `onNativeVoiceResult`, `launchNativeVideoCamera`, `pickNativeDcimVideo`, `openNativeVideoUri`, `playNativeChime`, `saveBackupFile`, `pickBackupFile`, `forceCloseApp`. |

### Cardio & Scheduling Domain (`src/domain/`, `src/intervals/`, `src/workout/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/domain/airbikeCurve.ts](../src/domain/airbikeCurve.ts) | Official 18-anchor Assault AirBike Elite curve (`RPM_WATTS` `[15, 13]`..`[100, 1205]`), piecewise linear + cubic extrapolation `wattsForRpm()`, `rpmForWatts()`, `bpmForRpm(rpm) = rpm * 2`. |
| [src/domain/programs.ts](../src/domain/programs.ts) | `BASE_FTP = 182`, `PHASE_COLOR`, `PROGRAMS` (`n4x4`, `v5x3`, `b6030`, `sprint1020`, `p6x2` hidden, `tabata` hidden, `z2` hidden, `free`), `resolveProgram(raw, ftp, offsetWatts)` scaling target RPMs via cube-root/curve at runtime, `targetWatts()`, `totalSeconds()`, `workSeconds()`, `freeProgram()`. |
| [src/domain/timeline.ts](../src/domain/timeline.ts) & [engine.ts](../src/domain/engine.ts) | `WorkoutEngine`: `ready \| running \| paused \| finished`, accurate wall-clock elapsed calculation, `hydratePaused(elapsedSec)` crash-resume hydration, segment index locator, manual `skip()`, live segment extension when HR gate holds recovery open. |
| [src/domain/gate.ts](../src/domain/gate.ts) | `gateOpen()` & `recoveryLimitBpm()`: enforces floor (`minSec`), ceiling (`maxSec`), and HR threshold (`hr <= recoveryHrPct * hrMax`) in `gated` mode. |
| [src/domain/pacer.ts](../src/domain/pacer.ts) | `pacerDecision()` (metronome `transitions` lead/tail/auto-mute vs `whole`), `cueFor()` (`CORRIDOR_RPM = 1.5`, `OVERSHOOT_RPM = 2.5`, `CHIRP_RPM = 3.0`), `hrCue()` (`HR_CEILING = 0.96`). |
| [src/domain/reps.ts](../src/domain/reps.ts), [profile.ts](../src/domain/profile.ts), [smooth.ts](../src/domain/smooth.ts), [rpe.ts](../src/domain/rpe.ts) | Per-rep avg W & HR drop tracker (`repStats`, `repNote`), SVG profile bar color/heights (`profileBar`), `WindowAvg` trailing time-window filter, 8-button `RPE_STEPS` (values `3..10`). |
| [src/domain/energy.ts](../src/domain/energy.ts) & [tcx.ts](../src/domain/tcx.ts) | `bodyProfile()`, `kcalFromSamples()` (Keytel et al. 2005 HR formula), `buildTcx()` (`Sport="Biking"` vs `"Other"` HR-only). |
| [src/domain/library.ts](../src/domain/library.ts) | `arrange`, `moveProgram`, `toggleHidden`, `restoreOrder` for user-reordered/hidden cardio programs. |
| [src/domain/plan.ts](../src/domain/plan.ts) | `PHASE1_PLAN` (24 steps, Weeks 1–4), `WEEK_THEMES`, `UPPER1_FINISH_ITEMS`, `ExtraWorkout`, `QueueState`, `reconcileQueue`, `todayItemsFromQueue`, `projectPlan`, `buildWeekGlimpse`, `fmtWeekSpan`, `nextEquipmentSlot`, `nextTrainingDate`. |
| [src/intervals/api.ts](../src/intervals/api.ts) | `call()`, `fetchAthleteProfile()`, `fetchLatestWellnessWeight()`, `syncAthleteFtp()` (also pulls smart-scale wellness weight + `weightDate`), `testConnection()`, `uploadActivity()`, `patchActivity()`, `createManualActivity()`, `fetchActivities()`, `fetchEvents()`, `deleteEvent()` (`intervals/parse.ts` removed `v0.37`). |
| [src/intervals/calendar.ts](../src/intervals/calendar.ts) | Singleton `calendar`: `msUntilNextLocalMidnight()`, `checkDayRollover()`, synchronous `syncLocalState()`, one-time migrations (`seedYesterdayUpper1Makeup`, `backfillLegacySkippedZ5`, `LEGACY_CLEANUP_KEY`), `markSlotDone`, `skipSlot`, `removeSlot`, `pushSlotToTomorrow`, `addSlotToTomorrow`, `pullTomorrowToToday`, `setStep`, `markSlotDoneEarly`, `makeupForSession`, `scheduleSkippedMakeup`. |
| [src/workout/recorder.ts](../src/workout/recorder.ts), [summary.ts](../src/workout/summary.ts), [sync.ts](../src/workout/sync.ts) | `Recorder` (`1 Hz` sensor-driven writing directly to IndexedDB), `summarize()`, `syncWorkout()` (cardio/HR path delegates to `syncLiftWorkout` if strength lift exists), `retryPending()`, `pendingCount()`. |

### Lift Tracker Domain (`src/lifts/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/lifts/types.ts](../src/lifts/types.ts) | `MUSCLES` (17 codes), `Equip`, `ExType` (`weight \| bodyweight \| time`), `SetKind` (`n \| w \| d \| f`), `Exercise`, `ExRow`, `LiftSet`, `SessionEx`, `SkippedEx`, `LiftSession`, `RoutineItem`, `Routine`, `VideoMeta`, `uid()`. |
| [src/lifts/library.ts](../src/lifts/library.ts) | 120+ built-in exercises (`LIBRARY`, `LIBRARY_BY_ID`, `slug()`). Canonical pipe-delimited format `Name|equip|type|primary|secondary|aliases`. |
| [src/lifts/muscles.ts](../src/lifts/muscles.ts) | `GROUPS`, `MUSCLE_LABEL`, `EQUIP_LABEL`, `PRIMARY_WEIGHT = 1.0`, `SECONDARY_WEIGHT = 0.5`, `WEEKLY_BAND = [10, 20]`. |
| [src/lifts/seed.ts](../src/lifts/seed.ts) | `PHASE1_ROUTINES` (`Lower 1`, `Upper 1`, `Lower 2`, `Upper 2`), `SEED_EX_ROWS` (`triceps-isometric-hold` `45s` note), `matchRoutineByTitle()`. |
| [src/lifts/settings.ts](../src/lifts/settings.ts) | `LiftSettings` in `localStorage` (`armory.lifts.settings.v1`): `unit`, `restSec` (`90`), `restAuto` (`false`), `restSound` (`true`), `showTimer` (`false`), `showAutoTimer` (`false`), `videoDelay` (`5`). `toDisplay`, `fromDisplay`, `fmtKg`, `stepsFor`. |
| [src/lifts/store.ts](../src/lifts/store.ts) | Singleton `lifts` (`LiftStore`): write-through RAM mirror over `lift_sessions`, `lift_routines`, `lift_ex`, `lift_videos`; `SEED_KEY` & `CLEAN_TIMERS_KEY` migrations; `pagehide`/`visibilitychange` flush; `setMasterRestAuto`, `replaceEverywhere`, `exportAll`, `importAll`. |
| [src/lifts/active.ts](../src/lifts/active.ts) | Singleton `active` (`ActiveLift`): owns live `LiftSession`, attaches 1 Hz HR-only `Recorder` (skips attaching while paused), `ensureStarted` (`started = true`, auto-resumes if paused), `pause()`, `resume()`, `elapsedSec()`, `resetIfNoneLogged`, `startRest`/`adjustRest`/`clearRest` synced bi-directionally with native `WorkoutForegroundService`, `afterSetLogged`, `finish()` (hydrates `bwKg ??= settings.weightKg`, calls `pruneUnlogged(s)`, saves session + meta, triggers `syncWorkout`), `discard()`. |
| [src/lifts/logic.ts](../src/lifts/logic.ts) | Auto-fill (`buildExercise`, `prevSetFor`), forward-fill (`editWeight`, `editReps`, `toggleDone`), `addSet`, `removeLastSet`, `deleteSet`, `swapExercise`, `supersetGroups`, `labels` (`1A/1B`), `effectiveRestAuto`, `collectUnfinishedItems`, `pruneUnlogged` (strips unlogged sets & `s.skipped`), `routineFromSession`, `hasSessionActivity`, `isSessionStarted`, `isSessionPaused`, `elapsedSessionSec`, `pauseSessionClock`, `resumeSessionClock`, `driveUrlFor`. |
| [src/lifts/calc.ts](../src/lifts/calc.ts) | `MAX_RM = 12`, `isWorking`, `totalLoadKg` (adds `bwKg` on `bodyweight`), `setVolumeKg`, Epley `e1rm`, `sessionStats`, `previousPerformance`, `repBests`, `computePRs`, `e1rmTrend`, `muscleSets`. |
| [src/lifts/summary.ts](../src/lifts/summary.ts) | `condenseSets`, `shortLine`, `setText`, `topSet`, `sessionPRs`, `intervalsDescription` (emits completed exercises + HR/kcal/RPE + notes without set-level skipped lines). |
| [src/lifts/intervals.ts](../src/lifts/intervals.ts) | `syncLiftWorkout` (serialized per session id via promise `chain`, uploads TCX if `hr.has` (`>=30` beats >= 30 bpm) else `createManualActivity`, then `patchActivity` with `WeightTraining`, `intervalsDescription`, `icu_rpe`, `kg_lifted`), `resyncSession`. |
| [src/lifts/mp4.ts](../src/lifts/mp4.ts) & [share.ts](../src/lifts/share.ts) | MP4 `mvhd` box creation timestamp parser + `PXL_YYYYMMDD_HHMMSS` Drive query builder; `1080×1350` PNG share-card renderer + Capacitor `Share`/`Filesystem` saver. |
| [src/lifts/cursor.ts](../src/lifts/cursor.ts), [voiceParser.ts](../src/lifts/voiceParser.ts) & [voice.ts](../src/lifts/voice.ts) | Superset-interleaved execution sequence (`1A Set 1 -> 1B Set 1 -> 1A Set 2 -> 1B Set 2`), `resolveTargetSet`, `advanceCursorAfterLog`, `stepExerciseCursor`, `jumpToExerciseCursor` (`"2B"` / `"two b"` / name match), spoken number normalization (`voiceParser.ts`), and foreground-scoped `voice` controller (`VoiceController`) with 6.5s wake window, undo stack, and hold-countdown trigger. |

### Gym Display Wallboard (`src/wallboard/`)
| File | Key Exports & Responsibilities |
| :--- | :--- |
| [src/wallboard/snapshot.ts](../src/wallboard/snapshot.ts) & [state.ts](../src/wallboard/state.ts) | Pure `buildWallboardSnapshot()` & singleton wrapper `buildWallboardState()` assembling `lift` (UP NOW + UP NEXT superset alternation, rest/hold bars, HR readiness), `cardio`, and `standby` payloads. |
| [src/wallboard/page.ts](../src/wallboard/page.ts) & [sync.ts](../src/wallboard/sync.ts) | Self-contained 10-foot dark `WALLBOARD_HTML` SPA (SSE `/events` + `/state` fallback + WakeLock + optional desktop LAN `/api/voice` PTT) and `wallboard` controller pushing live state through `WallboardServer.java`. |

### UI Layer (`src/ui/`, `src/ui/lifts/`, `android/`)
- **Core UI:** [home.ts](../src/ui/home.ts), [dayZoom.ts](../src/ui/dayZoom.ts), [chrome.ts](../src/ui/chrome.ts), [program.ts](../src/ui/program.ts), [equipment.ts](../src/ui/equipment.ts), [cardioWorkout.ts](../src/ui/cardioWorkout.ts), [hud.ts](../src/ui/hud.ts), [summaryView.ts](../src/ui/summaryView.ts), [bikeSheet.ts](../src/ui/bikeSheet.ts), [hrSheet.ts](../src/ui/hrSheet.ts), [hr.ts](../src/ui/hr.ts), [settings.ts](../src/ui/settings.ts), [wallboardPreview.ts](../src/ui/wallboardPreview.ts), [strength.ts](../src/ui/strength.ts), [dom.ts](../src/ui/dom.ts), [leaveGuard.ts](../src/ui/leaveGuard.ts), [style.css](../src/style.css).
- **Strength UI:** [index.ts](../src/ui/lifts/index.ts), [train.ts](../src/ui/lifts/train.ts), [session.ts](../src/ui/lifts/session.ts), [summary.ts](../src/ui/lifts/summary.ts), [history.ts](../src/ui/lifts/history.ts), [progress.ts](../src/ui/lifts/progress.ts), [exercises.ts](../src/ui/lifts/exercises.ts), [exercise.ts](../src/ui/lifts/exercise.ts), [routine.ts](../src/ui/lifts/routine.ts), [settings.ts](../src/ui/lifts/settings.ts), [picker.ts](../src/ui/lifts/picker.ts), [video.ts](../src/ui/lifts/video.ts), [cards.ts](../src/ui/lifts/cards.ts), [charts.ts](../src/ui/lifts/charts.ts), [kit.ts](../src/ui/lifts/kit.ts), [lifts.css](../src/ui/lifts/lifts.css).
- **Android Java:** [MainActivity.java](../android/app/src/main/java/com/froriep/armoryfit/MainActivity.java), [WorkoutServicePlugin.java](../android/app/src/main/java/com/froriep/armoryfit/WorkoutServicePlugin.java), [WorkoutForegroundService.java](../android/app/src/main/java/com/froriep/armoryfit/WorkoutForegroundService.java), [WorkoutNotif.java](../android/app/src/main/java/com/froriep/armoryfit/WorkoutNotif.java), [WorkoutChime.java](../android/app/src/main/java/com/froriep/armoryfit/WorkoutChime.java), [WorkoutMetronome.java](../android/app/src/main/java/com/froriep/armoryfit/WorkoutMetronome.java), [WallboardServer.java](../android/app/src/main/java/com/froriep/armoryfit/WallboardServer.java), [VoiceRecognitionManager.java](../android/app/src/main/java/com/froriep/armoryfit/VoiceRecognitionManager.java), [RestOverlay.java](../android/app/src/main/java/com/froriep/armoryfit/RestOverlay.java).

---

## 3. Storage Schemas, Keys & Mandatory Migration Pattern

### IndexedDB `armory-fit` (Version `2`)
| Store Name | Key Path | Record Shape & Retention Notes |
| :--- | :--- | :--- |
| `workouts_meta` | `id` | `WorkoutMeta`: `status` (`active\|finished\|saved\|discarded`), `upload` (`none\|pending\|done`), `patched`, `activityId`, `planSlot`, `rpe` (`3..10`), `notes` (`repNote` for AirBike). Written on start & every state transition. |
| `workout_samples` | `[id, t]` | `{ id: string, t: number, w, cad, hr, dist, kcal, tr }`: written 1x/second during live cardio or HR-paired Strength sessions. Enables crash recovery (`activeIncomplete` banner on `#/`). |
| `lift_sessions` | `id` | `LiftSession`: `{ id, name, routineId, eventId, planSlot, startMs, endMs, pausedAt?, pausedMs?, status: 'active'\|'done', started, manualStart, exs, skipped?: SkippedEx[], note, rpe, bwKg }`. Only one `status === 'active'` should exist at a time. |
| `lift_routines` | `id` | `Routine`: `{ id, name, items: [{ exId, sets, ss? }], createdMs, order }`. Seeded once from `PHASE1_ROUTINES`. |
| `lift_ex` | `id` | `ExRow`: overrides (`note`, `restSec`, `restAuto`, or custom `Exercise` fields + `archived`). |
| `lift_videos` | `id` | `VideoMeta` (`id`) + optional `BlobRow` (`id: "blob:<videoId>"`). Native APK stores `linkOnly: true`, `uri` (`content://` persisted), `fileName` (`PXL_...`), `bytes`, `fileMs`, `durationSec`, and `200px`-wide JPEG `thumb`. |

### `localStorage` Keys
| Key | Owner | Schema / Role |
| :--- | :--- | :--- |
| `armory.settings.v1` | `src/store/settings.ts` | Global `Settings` (`athleteId`, `apiKey`, `ftp`, `hrMax`, `sex`, `weightKg`, `weightDate`, `birthDate`, `recoveryMode`, `recoveryHrPct`, `programOrder`, `programHidden`, `metro*`, `chime`, `haptics`, `needleSmooth`, `simulator`, `restDay`, `wallboardEnabled`, `wallboardPort`, `voiceEnabled`, `voiceWakeWord`, `queue: QueueState`). |
| `armory.lifts.settings.v1` | `src/lifts/settings.ts` | `LiftSettings` (`unit`, `restSec`, `restAuto`, `restSound`, `showTimer`, `showAutoTimer`, `videoDelay`). |
| `armory.ble.polar` / `armory.ble.airbike` | `src/ble/devices.ts` | Saved native BLE device IDs (`deviceId`, `name`) for zero-chooser auto-reconnect. |
| One-time migration keys | `store.ts` / `calendar.ts` / `lifts/settings.ts` | `armory.lifts.seeded.v1`, `armory.lifts.clean_timers_rows.v1`, `armory.lifts.clean_timers_cfg.v1`, `armory.cleanup_staged_events.v1`, `armory.backfill_skipped_z5.v1`, `armory.extra_u1_finish_2026_11_16.v1`. |

### Critical Invariant: Seeding vs. Existing Device Upgrades
Because `PHASE1_ROUTINES` and `SEED_EX_ROWS` only run when `armory.lifts.seeded.v1` is absent (`LiftStore.load()`), **editing `seed.ts` alone does NOT update templates on Dave's existing phone**. Any developer or agent modifying an existing routine or exercise note **must** add an idempotent, `localStorage`-guarded migration in `LiftStore.load()` (`lift_routines`/`lift_ex`) or `Calendar` constructor (`QueueState`/`extras`). Guard every `localStorage` read/write with `typeof localStorage !== 'undefined'` for Node `vitest`.

---

## 4. Routing, Navigation Tree, Leave Guards & Native Exit

- **Hash Router (`src/main.ts`):** Splits `#/<path>?<query>` into `[section, ...args]` + `URLSearchParams`.
- **Parent Back Hierarchy (`src/nav.ts` `parentHash`):**
  - `#/` -> `null` (triggers `promptExit()` `"Exit Armory Fit? -> Yes"` -> `forceCloseApp()` which calls Android `WorkoutServicePlugin.forceClose()`: stops foreground service, `finishAffinity()`, `finishAndRemoveTask()`, then `Process.killProcess(myPid())` after `120 ms`).
  - `#/program`, `#/airbike`, `#/spin`, `#/treadmill`, `#/settings`, `#/strength`, `#/strength/history`, `#/strength/progress`, `#/strength/exercises` -> `#/`
  - `#/wallboard` -> `#/settings`
  - `#/airbike/p/<id>` -> `#/` (live HUD returns directly to Home); `#/airbike/w/<id>` -> `#/airbike`
  - `#/strength/summary/<id>`, `#/strength/workout/<id>` -> `#/strength/history`
  - `#/strength/session/<id>` -> `#/strength/workout/<id>` (historical session editor returns to its workout detail)
  - `#/strength/exercise/<id>` -> `#/strength/exercises`
  - `#/strength/session`, `#/strength/hr` -> `#/` (live workout/preview & Ruck/HR return directly to Home)
  - `#/strength/routine/<id>`, `#/strength/settings` -> `#/strength`
- **Hardware Back Interception Priority (`MainActivity`/`App.addListener('backButton')`):**
  1. Topmost visible `.lmodal` / `.modal` (dispatches `armory:dismiss` to close cleanly).
  2. `#dayZoom` `[data-back-today]` when Home is zoomed to a non-today date -> jumps back to Today.
  3. `parentHash(routeKey)` -> walks cleanly up the hierarchy.
  4. Root `#/` -> `promptExit()`.
- **Leave Guard:** Used ONLY on active Cardio (`#/airbike/p/<id>`) and `Ruck / Outdoor HR` (`#/strength/hr`) via `LeaveGuard` (returns directly to `#/`). Active lifting (`#/strength/session`) intentionally has **no** leave-confirmation trap — tapping `< Back` returns directly to Home (`#/`) while the sticky `Resume ›` banner stays pinned below every screen header.

---

## 5. Complete Screen-by-Screen, Button & Gesture Reference

### 5.1 `#/` — Home Shell (`src/ui/home.ts`, `src/ui/chrome.ts`, `src/ui/dayZoom.ts`)
- **Fixed `100dvh` App Shell (`.screen.home-app`):** Non-scrolling outer shell with four structural tiers:
  1. **Pinned Top Bar + Live Resume Strip + Locked Week Calendar (`#calDock`):**
     - `topbar`: `ARMORYFIT` wordmark, `[ ♥ HR ]` telemetry chip (opens `#hrBody` modal anywhere), `[ ⚙ ]` (`#/settings`). Note: tapping the `BIKE` chip (`[ RPM ]` / `[ CONNECT BIKE ]`) anywhere opens `openBikeSheet()` diagnostics/controls when connected or pairs when disconnected.
     - `#calDock`: Week span (`NOV 15 – 21`), conditional `Today ↩` pill when viewing another date, `‹` / `›` week shift buttons (`±7d`), 7 Sunday–Saturday columns (`wk-d`, `78px`) with equipment icons (`up to 2`) or `REST` label, green `✓` badge if done. **Horizontal touch swipe** across `#calDock` shifts weeks (`±7d`) with directional slide animation.
  2. **Locked `TODAY` Hero Dock (`.home-today-dock` -> `.prog-hero`):**
     - Clickable `[data-snap-today]` `TODAY · WK X · Step/24` pill header (snaps calendar week strip and `#dayZoom` back to Today), `Full Program ›` link (`#/program?w=X`), streamlined 1-line completed rows (`✓ Done` scrolls/focuses Today), 1-line pending/carried rows (`Title`, `Start` link deep-linking to `#/strength?pick=...&start=1` or `#/airbike/p/<id>?pick=...`, + `⋯` slot menu with `Mark done`, `Skip`, `Push to tomorrow`, `Add to tomorrow`, `Remove workout`), plus `Pull to Today` when Today is clear and the next step sits on a future date.
  3. **Scrollable Middle Body (`.home-scroll`):**
     - Conditional crash recovery card (`Unfinished workout` -> `Resume` [`#/airbike/p/<id>?resume=<id>`] / `Save & upload` / `Discard`) & offline sync card (`N workouts waiting to upload` -> `Retry now`).
     - **Always-Open Inline Day Zoom (`#dayZoom`, `flex-shrink: 0`):** Scrollable vertically without clipping even on 2-workout days; displays `selectedYmd` (defaults to Today; auto-advances at local midnight via `msUntilNextLocalMidnight()` and on foreground resume). Supports **horizontal touch swipe** (`±1d`) and `‹` / `›` header arrows, status pill (`● Today`, `✓ Today · Done`, `Tomorrow`, `✓ Done`, `Skipped`, `Past`, `Projected`), `Wk X · Day Y`, completed lift cards (exercise list, sets condensed, PR stars, `Full workout & edit ›`), completed cardio cards (`hms`, avg/max W, avg/max HR, kJ, RPE, rep notes, plus `Details & delete ›` link to `#/airbike/w/<id>`), skipped slots with amber `(skipped)` pill (removed slots omitted cleanly), carry-over `ExtraWorkout` cards, planned future strength slots resolved via `matchRoutineByTitle` with clickable `Edit template ›` and `Last:` sets, compact per-workout **`Do this workout now`** buttons inside each pending `.sh-slot` card (`launchDayZoomSlot`), plus a side-by-side `.dz-acts` row with conditional `Shift schedule to here` (only on future/past steps) and `View Full Program` (`#/program?w=X`).
  4. **Pinned Bottom Icon-Only Machine Dock (`.home-dock`):** Icon-only buttons (`aria-label`) for `Strength` (`#/strength`), `Treadmill` (`#/treadmill`), `Spin` (`#/spin`), `AirBike` (`#/airbike`), with glowing blue `.has-today` dot when scheduled today.

### 5.2 `#/program` — Full 4-Week Block (`src/ui/program.ts`)
- Progress card: `N / 24 sessions`, `%` progress bar, `Target Jan 8 10K`, clickable `Rest: Sun ▾` picker (`Sunday..Saturday` or `No fixed rest day`).
- `Wk 1`–`Wk 4` tabs (`●` on current week, `✓` when week resolved), projected date span next to week title (`Week 2 (Tomorrow – Fri, Nov 27)`), theme string (`WEEK_THEMES`).
- Step cards (`.p-step`): color-coded left border (`blue` current, `green` done/early, `slate` skipped/upcoming), slot rows showing `(skipped)` strikethrough amber or `✓` or duration.
- **Tap any step card -> `openStepSheet(stepIndex)`:** Shows completed session details, planned exercises (with `Last:` sets), or cardio description, plus contextual actions (`Start <Name>`, `Do <Name> Today`, `Mark <Name> Done Today`, `Shift schedule to here`, `Close`).

### 5.3 `#/airbike`, `#/airbike/p/<id>` & `#/airbike/w/<id>` — AirBike Selector, HUD & Ride Detail (`src/ui/equipment.ts`, `src/ui/hud.ts`, `src/ui/cardioWorkout.ts`, `src/ui/bikeSheet.ts`)
- **Selector (`#/airbike`):** Equipment `Today` program card, `Reorder` toggle (edit mode with `★` pin to top, `↑`, `↓`, `Hide` -> collapsible `HIDDEN` drawer with `Show`), visible program cards with target `RPM · Watts`, `Last: X W` chips from the latest completed session per program, total Z5 work duration, `HR-gated rest` chip, and `TODAY` highlight, `RECENT RIDES` list linking to `#/airbike/w/<id>`, plus bottom `Free Ride` banner (`#/airbike/p/free`).
- **Tactical HUD (`#/airbike/p/<id>`):**
  - Header with `< Back` (pauses instead of dropping if running), `[ ♥ BLE ]` bike chip (displays live RPM/connection state; tap opens `openBikeSheet()` diagnostics modal showing Power, Pedal RPM, Fan RPM, Pulses, `DT (µs)`, `RATIO`, odometer, `Zero Bike Distance & Calories` [`RESET_PULSES`], and `Disconnect Bike`), `[ ♥ HR ]` chip.
  - Supports `?resume=<id>` crash-recovery hydration via `WorkoutEngine.hydratePaused(elapsedSec)` straight into `#pauseModal` and engages native `setKeepScreenOn(true)` (`FLAG_KEEP_SCREEN_ON`) while running.
  - **Overview & Profile Bar (`#tl`):** Proportional segment bars (height/color = `% FTP`), dashed `100% FTP` horizontal line, live `#tlHead` playhead (or `.tl-fill` on goal/free rides).
  - **Phase Banner + Live Target Trim (`±5 W`):** Phase badge (`READY`, `WARM-UP`, `WORK`, `FLOAT`, `EASY SPIN`, `COOL-DOWN`, `COMPLETE`), `REP i/n`, target cadence & Watts; Zone 2/Zone 5 cards expose `−5 W` / centered reset label / `+5 W` live trim adjustments.
  - **Big Countdown & Next Cue (`#cd` / `#next`):** Shows time remaining (`mm:ss` or HR-gate status `+mm:ss / <=130 bpm`), plus `NEXT:` preview.
  - **Goal Stepper (`#goalCtl`, pre-start on `time`/`cal`):** `−` / `+` minutes or Assault kcal.
  - **5 Hz Sweet-Spot Horizon Gauge (`#gauge`):** Colored target corridor (`±1.5 RPM`), live smoothed needle (`0/1/2/3 s` selectable in Settings), dynamic status cue (`ON TARGET`, `PICK IT UP`, `+X W TOO HOT — EASE OFF`, HR-ceiling warning, or HR-recovery target prompt). Triggers native `WorkoutMetronome` (`2×RPM`) and launch-overshoot chirp (first 15 s of work block at `>+3.0 RPM`).
  - **3 Metric Cards + 5-Cell Strip:** Large `HEART RATE`, `POWER`, `CADENCE`, plus `ELAPSED`, `LEFT/AVG W`, `AVG W`, `KCAL` (Assault kcal), and `Z5 BANK` (cumulative seconds `>= 88% HRmax` on Z5 programs) or `DIST MI`.
  - **Controls:** `METRONOME` toggle, conditional `SKIP` button (advances to next segment), `START` / `PAUSE`.
  - **Modals & Summary:** Pause sheet (`Resume`, `Finish workout`, `Discard workout` -> returns to `#/`), Auto-completion sheet (`Finish & rate`, `Keep riding`), followed by `renderSummary()` (`summaryView.ts`): HR zone bars (`Z1–Z5`), Z5 per-rep wattage + 60s HR drop notes (`repNote`), mandatory 8-word RPE (`3..10`), `Save & upload to Intervals.icu`, `Discard workout` (returns to `#/`).
- **Saved Cardio / Outdoor HR Detail (`#/airbike/w/<id>` via `cardioWorkout.ts`):** Time/Power/Cadence/Work/HR/Distance stat grid, `SPLITS & RECOVERY` notes (`m.notes`), `HEART RATE ZONES` bars (`Z1–Z5`), Intervals.icu sync status + `Open ↗` / `Retry upload`, 8-word RPE editor (`3..10`) triggering re-sync, and `Delete workout` (`confirmSheet` + `discard`).

### 5.4 `#/spin` & `#/treadmill` (`src/ui/equipment.ts`)
- Displays equipment-scoped `TODAY` / `Next` program preview card (with `Preview` step modal button) and hardware calibration readiness note (`SS2K` power-pedal calibration / `Horizon T101 Pod`).

### 5.5 `#/strength` — Strength Train Hub (`src/ui/lifts/train.ts`)
- Topbar `< Back` (`#/`), `[ ♥ HR ]`, `[ ⚙ ]` (`#/strength/settings`). Auto-starts `?pick=<planSlot>&start=1` if launched from Home or auto-focuses `[data-plan-card]` when `?pick=` is passed.
- **Cards:** `STRENGTH · PROGRAM` (today's Strength & Carry-over rows with `Start` + `⋯`), prominent `Custom Workout` freestyle banner (`Open ›` or `Start ›`, with conflict dialog if another routine is active), collapsible `DAY TEMPLATES · N` drawer (`+ New day` -> `#/strength/routine/new`, routine cards with `⋯` [`Edit day`, `Duplicate`, `Move up`, `Move down`, `Delete day`] and `Start`), plus `Ruck / Outdoor HR` link (`#/strength/hr`).
- **4-Tab Bottom Navigation (`kit.ts`):** `Train` (`#/strength`), `History` (`#/strength/history`), `Progress` (`#/strength/progress`), `Exercises` (`#/strength/exercises`). Every Strength screen except active `#/strength/session` and `#/strength/summary/<id>` mounts the sticky top `Resume ›` bar whenever `isSessionStarted(lifts.active)`.

### 5.6 `#/strength/session` & `#/strength/session/<id>` — Active Lift & Edit Mode (`src/ui/lifts/session.ts`)
- **Zero-Trap Back, Deferred Clock & Pause/Resume (`v0.37+`):** Back `<` navigates cleanly to `#/strength` (or `#/strength/workout/<id>` in edit mode) without nagging modals. Active clock rests at `0:00` preview mode with a visible **`▶ Start`** pill button until Set 1 is checked (`✓`), a video is linked, or `▶ Start` is tapped manually. Once started, header displays **`⏸ Pause`** / **`▶ Resume`** (`active.pause()` / `active.resume()`): pausing stops active timer accumulation (`pausedAt`/`pausedMs`), halts idle HR sample recording during multi-hour breaks, and turns `mountTopResume` dot amber with `· paused`. Any subsequent check (`ensureStarted`) auto-resumes. Checking then unchecking Set 1 resets clock & pause tracking if `manualStart` is false (`resetSessionClock`).
- **Header & Keep-Awake (`v0.40+`):** Tap workout title to rename (`promptSheet`), `[ ▶ Start | ⏸ Pause | ▶ Resume ]`, `[ 🎤 MIC | ARMED | MIC … ]` live mic pill (toggles Strength-only voice logging, or restarts a paused/recovering mic in 1 tap), `[ ♥ HR ]`, `[ ⋯ ]` Workout Options (`Rename workout`, `Add/Edit workout note`, `TV Wallboard`, `Discard/Delete workout`). Active live sessions engage `setKeepScreenOn(true)` (`FLAG_KEEP_SCREEN_ON`) for the full duration of `#/strength/session` (re-applied on foreground resume; released on leave/finish). Active target row from the superset-interleaved cursor (`resolveTargetSet`) highlights `.cur-target`; `LOG ✓` on the keyboard dock advances focus across superset pairs automatically (`1A -> 1B -> 1A -> 1B`).
- **Superset Brackets (`supersetGroups`):** Adjacent exercises with `ss: true` wrap in alternating blue/emerald `.ss-group` containers tagged `SUPERSET`, labelled `1A`, `1B`, `2A`, `2B` (`labels()`).
- **Exercise Card (`.xcard`):**
  - Tap Exercise Name or `⋯` -> **Exercise Action Sheet:**
    - `Swap exercise` -> picks replacement from `picker.ts` (lists `SIMILAR` primary movers first) -> **3-Scope Prompt:** `Just today`, `This day · <Routine>` (patches `lift_routines`), or `Everywhere` (`replaceEverywhere`).
    - `Add/Edit note` -> sticky note saved per `exId` in `lift_ex`.
    - `Superset with next` / `Remove superset with next`.
    - `Settings` -> **Per-Exercise Timer & Card Settings Sheet:** `Show timer` (`showTimer`), `Show auto-timer` (`showAutoTimer`), `REST DURATION THIS EXERCISE` chips (`0:30..5:00`), `Auto-timer on check` (this card only), `Auto-timer by default` (master toggle), `Default duration`, `Rest-over chime`, `▶ Start <dur> now` (live mode).
    - `Move up`, `Move down`, `Records & history` (`#/strength/exercise/<id>`), `Remove exercise` (with toast `Undo`).
  - **Set Grid (`SET | PREVIOUS | LB/KG | REPS/SEC | [HOLD] | 🎥 | ✓`):**
    - On `ex.type === 'time'` exercises (`Triceps Isometric Hold`, etc.), unlogged rows expose an inline **`.shold` countdown button** (`▶ 45s` -> `0s`) that acquires `setKeepScreenOn(true)`, ticks down live, chimes + vibrates on zero, and auto-checks `✓` (`t-row` 7-column grid).
    - `snum` button (shows `W`, `D`, `F`, or working number + `★` PR badge, `@RPE`, and amber note dot) -> **Set Options Sheet:** Set type (`Normal`, `Warm-up`, `Drop set`, `To failure`), Effort RPE (`6..10` in 0.5s or `–`), Set Note, `Link or Record Video…` (`video.ts`: native `MediaStore.INTENT_ACTION_VIDEO_CAMERA` launch, SAF `ACTION_OPEN_DOCUMENT` in `DCIM/Camera`, poster frame + Drive search query builder), `Delete set`.
    - Tap `PREVIOUS` cell -> copies last-session set values into inputs (`wAuto/rAuto = false`).
    - Direct numeric inputs with **floating keyboard dock** above virtual keyboard (`−10 −5 +5 +10` lb or `−5 −2.5 +2.5 +5` kg for weight; `8 10 12 15 −1 +1` for reps; `−15 −5 +5 +15` for time exercises; `+ LOG ✓` jumps to next unlogged set).
  - **Card Footer:** `− SET +` buttons (`removeLastSet` with toast `Undo`, `addSet` cloning last working set), plus conditional `Auto` pill (`showAutoTimer`) and `▶ m:ss` quick rest trigger (`showTimer`).
  - **Floating Rest Bar (`#restbar`):** Appears above dock whenever a countdown is active (`−15`, `+15`, `Skip`), turning red (`GO +m:ss`) after zero while triggering native `WorkoutChime` + vibration + minimized `RestOverlay`.
- **Footer CTA:** `+ Add Exercise` / `+ Add Next Exercise` banner (`picker.ts` multi-select + `+ Custom` builder), `Finish workout` (`Done` in edit mode), and a live **`Discard workout`** danger button (`discardCurrentWorkout()` -> `#/`). Unlogged sets trigger an explicit **"There are unfinished sets"** sheet (`Do later today`, `Do tomorrow`, `Delete unfinished sets & finish`, `Keep lifting`) without marking individual sets as skipped (`s.skipped` omitted on lift sessions; only whole scheduled gym sessions or Z5 workouts carry `(skipped)` status), before calling `active.finish()` (`bwKg ??= settings.weightKg`, `pruneUnlogged(s)`, saves IndexedDB, triggers `syncWorkout` -> `syncLiftWorkout`, navigates to `#/strength/summary/<id>`).

### 5.7 Remaining Strength & Utility Screens
- **`#/strength/summary/<id>` (`summary.ts`):** Title, timestamp/duration, 4-stat grid (`SETS`, `VOLUME`, `EXERCISES`, `PRs`), `NEW RECORDS` card, Polar H10 `Z1–Z5` bar chart, live Intervals.icu sync status (`✓ Logged · Open` or error + `Retry now`), 8-word RPE grid (`3..10` -> calls `resyncSession()`), `Share lift card` (`1080×1350` PNG), `Save as a day` (if custom), `Done`.
- **`#/strength/history` & `#/strength/workout/<id>` (`history.ts`):** Navigable month grid with trained-day blue dots (`dayKey`), filterable workout feed. Workout detail view shows date, 4 stats, HR/kcal/RPE + Intervals link, note, per-exercise condensed header + set rows (warm-ups `W`, PR pills, clickable `▶` video buttons), `Do this workout again`, `Edit sets`, and `⋯` (`Edit sets`, `Delete workout`).
- **`#/strength/progress` (`progress.ts`):** Week-by-week navigation (`This week`, `Last week`, …), 3-stat summary, `HARD SETS BY MUSCLE` across all 17 muscles (`1.0` primary / `0.5` secondary, `10–20/wk` reference lines), 12-week workouts bar chart, `YOUR LIFTS` e1RM table, `RECENT RECORDS`, horizontal `LIFT VIDEOS` reel.
- **`#/strength/exercises` & `#/strength/exercise/<id>` (`exercises.ts`, `exercise.ts`):** Instant search across name + haystack aliases, filter chips (`All`, `Done before`, muscle groups), `+ Custom` exercise creator/editor. Detail view shows muscle tags, sticky clickable note, 4 stats (`EST 1RM` or `BEST HOLD`, `HEAVIEST`, `SESSIONS`, `LAST`), `1RM–5RM` expandable to `12RM` table (tap row to jump to session or play video), Epley e1RM SVG chart (`3M`, `6M`, `1Y`, `All`), video thumbnails, session history, per-exercise rest duration picker, and `⋯` menu (note, edit muscles/custom fields, archive/delete).
- **`#/strength/hr` (`strength.ts`):** **Ruck / Outdoor HR** Polar H10 logger with `PACK WEIGHT` selector (`No pack`, `20`, `30`, `35`, `45 lb` — default `35 lb`), `START RUCK · 35 LB` / `PAUSE`, `Z1–Z5` summary + Keytel kcal + TCX upload (`programId: 'ruck'` uploaded as Intervals.icu `type: 'Walk'`).
- **`#/strength/settings` (`ui/lifts/settings.ts`):** Units (`lb`/`kg`), Default rest (`0:30..5:00`), `Show timer button`, `Show auto-timer`, `Auto-timer` master switch (`setMasterRestAuto`), `Rest-over chime` + `Test`, Android `Floating timer overlay` unlock walkthrough (`App Info -> Allow restricted settings -> Overlay Toggle`), camera delay (`0/3/5/10 s`), linked video counter, Intervals sync status + `Retry now`, JSON `Export backup` (`{ app: "armory-lifts", v: 1, sessions, routines, exercises, videos }`) & `Import backup`.
- **`#/settings` (`ui/settings.ts`) & `#/wallboard` (`ui/wallboardPreview.ts`):** `AirBike FTP` (`80–450 W`), `Bodyweight`, `GYM DISPLAY & VOICE` (`Smart TV Wallboard` switch + Wi-Fi URL + `Open Preview & Setup ›` -> `#/wallboard` scaled 16:9 (1600×900) live TV preview + tap-to-fullscreen, `Hands-Free Voice Commands` foreground toggle + `Wake word` + live status + `Test phrase` runner), Intervals `Athlete ID` / `API key`, `Rest day`, Z5 recovery, Metronome, Transition chime, Vibration, Cadence bar smoothing, Max HR, Forget sensors, Simulator, plus `ArmoryFit Backup` (`Back up now` / `Restore backup`). Uses Armory dark `confirmSheet`/`toast`.
- **`#hrBody` Modal Sheet (`src/ui/hrSheet.ts`):** Opens from any screen's `[ ♥ HR ]` chip. Shows connection dot/device name, live zone pill, huge zone-colored BPM readout, 2-minute SVG area sparkline with recovery-threshold dashed line + 60s HR drop stats, `THIS WORKOUT` / `STRAP SESSION` `AVG HR` / `MAX HR` / `~KCAL`, `Z1–Z5` `TIME IN ZONE` bars, and contextual `Pair` / `Reconnect Polar H10` button.

---

## 6. Domain Algorithms & Calculation Rules

### Rolling Queue & Calendar Projection (`src/domain/plan.ts`)
- **`reconcileQueue()`:**
  1. Absorbs `doneEarly[slotKey]` into `completedSlots` when `stepIndex` reaches that step.
  2. Inspects non-Armory external Intervals.icu activities (e.g., MyWhoosh rides) since `min(stagedDate, today)` (`!external_id.startsWith('armory-') && !paired_event_id && !consumed.has(id)`): matches `carryOver` first, then unresolved current step slots (`activityMatchesSlot && !hasExplicitConflict`), then future steps if `matchesExplicitTag` succeeds (`routineTag` or `cardioTag`).
  3. Advances `stepIndex` whenever every slot of `plan.steps[stepIndex]` is either in `completedSlots`, `skippedSlots`, `removedSlots`, or `carryOver` (looping over consecutive steps whose slots were already finished/resolved early), setting `stagedDate = nextTrainingDate(tomorrow, restDay)`.
  4. Rolls uncompleted `extras` (`date < today`) forward onto `today`.
- **`projectPlan()`:** Renders past steps (`done` / `skipped` / `removed` using `stepDates` / `skippedSlots` / `removedSlots`), `current` step (`slotDates` override or `stagedDate`/`today`), early-resolved future steps (`early` / `skipped` / `removed`), and remaining `upcoming` steps spaced cleanly across consecutive dates skipping `restDay`.

### Strength Auto-Fill, Forward-Fill, PRs & Volume (`src/lifts/logic.ts`, `calc.ts`, `summary.ts`)
- **Canonical Storage:** All loads in `kg` (`KG_PER_LB = 0.45359237`); `toDisplay()` rounds to `0.1` so `lb` round-trips cleanly.
- **Auto-Fill & Forward-Fill:** `buildExercise()` pre-fills each set from matching set order (`warm-ups <-> warm-ups`, `working <-> working`) of `previousPerformance()`, marking `wAuto`/`rAuto = true` (extra sets repeat the final previous working set; `triceps-isometric-hold` defaults to `45s` `rAuto`). Editing weight/reps clears `Auto` on that cell and flows forward into subsequent unlogged `Auto` / empty cells only; clearing an input clears subsequent untouched `Auto` cells.
- **PRs & Rep Records (`calc.ts`):** Chronologically sorted sessions; warm-ups (`kind === 'w'`) and `time` exercises excluded from load PRs. A logged working set is a PR for `n = Math.min(MAX_RM, reps)` reps iff the athlete had at least one prior session for that exercise AND load strictly exceeds the prior heaviest weight lifted for `>= n` reps (ties keep the earliest session; unweighted bodyweight sets can still earn rep-count PRs via `0 kg` load vs higher rep counts).
- **Intervals Description (`intervalsDescription` in `src/lifts/summary.ts`):**
  ```text
  Armory Fit · Upper 1
  52 min · 8 sets · 10,095 lb · 1 PR

  Triceps Isometric Hold: 2×45s @ 50 lb
  Barbell Bench Press: 185×12 · 225×8 · 225×7 lb  ★ 8RM PR
  Smith Machine Row: 3×15 @ 100 lb

  avg HR 118 · max HR 151 · ~310 kcal · RPE 8/10
  ```

---

## 7. Native Android Foreground Service & Media Bridge

| Java Class | Responsibility |
| :--- | :--- |
| `MainActivity` | Registers `WorkoutServicePlugin`, requests Android 13+ `POST_NOTIFICATIONS`, tracks foreground/background visibility via `WorkoutServicePlugin.setAppVisible()`. |
| `WorkoutForegroundService` | Sticky `FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE` notification (`WorkoutNotif` using monochrome white vector `ic_stat_armory.xml`), acquires `PowerManager.PARTIAL_WAKE_LOCK`, runs live chronometer, tracks rest-timer end elapsed timestamp, fires `WorkoutChime.playCached()` + `vibrateRestDone()` + `emitRestSync("fired", ...)`, stops `WorkoutMetronome` in `onDestroy()`, manages minimized `RestOverlay`. |
| `WorkoutChime` | Caches PCM WAV in app cache, plays via `MediaPlayer` with `AudioAttributes.USAGE_MEDIA` and `AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK` so Bluetooth audio ducks during the 3-ding chime and resumes smoothly. |
| `WorkoutMetronome` | Drift-free native `SoundPool` (`USAGE_MEDIA` + `CONTENT_TYPE_SONIFICATION`) cadence clicker scheduled on `SystemClock.uptimeMillis()`, mixing accented/sub-beat cached click WAVs over Bluetooth without acquiring `AudioFocus`. |
| `WallboardServer` | Local Wi-Fi HTTP/1.1 + SSE server (`GET /`, `/state`, `/events`, LAN-restricted `POST /api/voice` injecting external speech transcripts) holding a `WifiLock` and auto-detecting `wlan`/`eth` IPv4 address. |
| `VoiceRecognitionManager` | Foreground-only Android `SpeechRecognizer` (`createOnDeviceSpeechRecognizer` on API 31+ with companion minimum-length/silence extras for `EXTRA_SEGMENTED_SESSION`, biasing strings, generation-guarded `RecognitionListener` instance reuse, 5s stall/timeout watchdog + automatic standard/non-segmented fallback), re-armed after `WorkoutChime` audio-focus release and torn down whenever `MainActivity` enters background. |
| `RestOverlay` | System overlay pill (`SYSTEM_ALERT_WINDOW`) displayed only while `appVisible == false` and `restActive == true`; supports tap-to-open-app, `−15`, `+15`, and `Skip` syncing back to `session.ts`. |
| `WorkoutServicePlugin` | Exposes `@PluginMethod`s (`start`, `update`, `stop`, `setRest`, `clearRest`, `canOverlay`, `requestOverlayPermission`, `openAppSettings`, `setKeepScreenOn` with foreground re-apply persistence, `setMetronome`, `startWallboard`, `stopWallboard`, `getWallboardStatus`, `pushWallboardState`, `setVoiceListening` incl. `forceRestart`, `getVoiceStatus`, `ensureVoicePermission`, `launchVideoCamera`, `pickDcimVideo`, `openVideoUri`, `playChime`, `writeBackupFile`, `pickBackupFile`, `forceClose`). |

---

## 8. Unit Test Suites & Release Verification

| Suite | Coverage |
| :--- | :--- |
| `tests/domain.test.ts` | `airbikeCurve`, `programs` scaling, `timeline`/`engine` (incl. `hydratePaused`), `gateOpen`, `pacerDecision`/`cueFor`/`hrCue`, `repStats`, `profileBar`, `arrange`/reordering. |
| `tests/lifts.test.ts` | 120+ library integrity, unit conversion, auto-fill/forward-fill, lift session pause/resume elapsed exclusion, superset brackets & auto-rest rules, `collectUnfinishedItems` + `pruneUnlogged` (zero set-level `skipped`), Epley e1RM, rep bests & PRs, muscle sets, `intervalsDescription`, `mp4` mvhd parser, `deriveDriveUrl`, `parentHash`. |
| `tests/voice-wallboard.test.ts` | `plannedSetSequence` superset interleaving (`1 -> 2A -> 2B -> 2A -> 2B`), `resolveTargetSet`, `advanceCursorAfterLog`, `jumpToExerciseCursor`, `applyAddSet`, `applyAddSetToLastEx`, `applyDeleteEmptySets`, `parseVoiceTranscript`, `buildWallboardSnapshot` (`lift`/`cardio`/`standby` + HR readiness), `#/wallboard` routing. |
| `tests/lifts-intervals.test.ts` | `createManualActivity` & `patchActivity` payloads/headers/error handling. |
| `tests/plan.test.ts` | `reconcileQueue`, `projectPlan`, `todayItemsFromQueue`, `buildWeekGlimpse`, `restDay` skipping, skipped slots & carry-overs, MyWhoosh conflict matching. |
| `tests/ui-domain.test.ts` | `WindowAvg`, `RPE_STEPS`, `buildTcx`, `msUntilNextLocalMidnight`, `hrDropStats`, `groupDayLogs`, `todayCompletedItems`, `renderDayZoomCard` (completed, schedule/Z5 skipped badges, extras, shift/today pills). |
| `tests/recorder-energy.test.ts` | `Recorder` 5 Hz averaging & backfill, Keytel energy math. |
| `tests/parsers.test.ts` | BLE `0x2A37` HR + NUS `LineBuffer` + `parseBikeLine` (including `fan`, `rfan`, `pls`, `dt`, `ratio`) + `summarize`. |
| `tests/modal-backdrop.test.ts` | Modal inert focus safety & backdrop dismiss behavior. |
| `tests/backup.test.ts` | ArmoryFit v2 & legacy v1 backup serialization & restore validation. |

Always run before committing & publishing:
```powershell
npx tsc --noEmit && npm test
npm run build && npm test
.\scripts\android.ps1 -NoBump -Publish
```
