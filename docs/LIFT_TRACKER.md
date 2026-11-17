# Lift Tracker (Strength Bay)

The Strength tile is a full lift tracker: log sets, auto-fill from last time, swap and add exercises, configurable per-exercise/global rest timers, history, records, muscle volume, share card, optional DCIM lift videos, and automatic Intervals.icu logging.

> Complete technical & programmer architecture: [ARMORY_FIT_TECHNICAL_REFERENCE.md](./ARMORY_FIT_TECHNICAL_REFERENCE.md)  
> Global Antigravity Skill: `C:\Users\davef\.gemini\config\skills\armory-fit\SKILL.md`

Route: `#/strength/...` (code: `src/lifts/` domain + storage, `src/ui/lifts/` screens).

## What is deliberately NOT in it
Auto-progression, bloat/social feeds, how-to tutorials, plate calculator. Your 4 Armory Phase 1 lifting days (`Lower 1`, `Upper 1`, `Lower 2`, `Upper 2`) seed cleanly on first install (`src/lifts/seed.ts`) and remain fully editable repeating templates.

## Screens
| Tab / route | What it does |
|---|---|
| Train `#/strength` | `STRENGTH · PROGRAM` card (today's local queue / carry-over extras), **Custom Workout** freestyle card, collapsible **DAY TEMPLATES**, HR-only session link |
| Session `#/strength/session` | The workout (`0:00` preview mode + `▶ Start` until Set 1 / video / manual start). Zero-nag Back (sticky top `Resume ›` bar), `PREVIOUS` column, forward-fill, steppers dock, `⋯` exercise Settings (rest duration, `Show timer`, `Show auto-timer`, `Auto`), spanning superset brackets (`1A/1B`), video camera/DCIM linker |
| Summary `#/strength/summary/<id>` | Totals, new PRs, **SKIPPED** card (`v0.35+` skipped exercises & partial sets), HR Z1–Z5 bars, live Intervals.icu sync, 8-word RPE (`3–10`), `1080×1350` PNG share card, `Save as a day` |
| History `#/strength/history` | Month calendar + feed; workout detail page (`#/strength/workout/<id>`) with completed sets, `SKIPPED` card, HR/Intervals link, edit sets, repeat, delete, share |
| Progress `#/strength/progress` | Weekly hard sets per muscle (`1.0` primary / `0.5` secondary), workouts/week chart, best lifts (est. 1RM), recent records, lift videos reel |
| Exercises `#/strength/exercises` | 120+ master list (+ custom). Detail page (`#/strength/exercise/<id>`): `1RM–5RM` (up to `12RM`), est. 1RM chart, videos, history, sticky note, per-exercise rest |
| Settings `#/strength/settings` | Units (`lb`/`kg`), default rest, `Show timer`, `Show auto-timer`, master `Auto-timer`, rest chime + `Test`, Android floating overlay walkthrough, camera delay, Intervals status, JSON export/import |
| `#/strength/hr` | Standalone heart-rate-only recorder + Keytel kcal |

## Logging, Skipped Tracking & Intervals.icu (`v0.35+`)
* **Auto-fill & Forward-fill:** Every unlogged set is pre-filled from the matching previous set (`warm-ups <-> warm-ups`, `working <-> working`). Typing or logging flows forward into untouched suggestions only.
* **No-Trap Navigation & Explicit Start:** Tapping `< Back` saves live state and leaves the top `Resume ›` banner pinned across every screen. Unchecking Set 1 restarts the clock if `manualStart` is false; tapping `▶ Start` or linking a video commits `manualStart`.
* **Rest Controls Behind `⋯` → Settings:** `showTimer` (`▶ m:ss`) and `showAutoTimer` (`Auto`) default `off` (`v0.30+`) for a clean card layout and can be toggled live per card/globally from any exercise's `⋯ -> Settings`.
* **Skipped Exercises & Partial Sets (`v0.35+`):** Finishing a workout calls `pruneUnlogged(s)`, which records any unlogged working-set shortfalls into `LiftSession.skipped` (`exId`, `plannedSets`, `doneSets`) before removing empty rows. Skipped items display on Home Day Zoom, Summary, Workout History, and upload inside the Intervals.icu description (`Skipped: <Exercise> (0/3 sets)` or `(2/3 sets — 1 skipped)`).
* **Intervals.icu Automatic Sync:** Local rolling scheduling runs offline with zero planned-event staging (`v0.33+`). On Finish:
  1. Polar H10 connected (`>=30` beats) -> uploads `.tcx` HR trace (+ Keytel kcal); no strap -> `POST /athlete/{id}/activities/manual`.
  2. Patches `WeightTraining`, condensed lift log + `Skipped:` summary, `icu_rpe`, and `kg_lifted`.
