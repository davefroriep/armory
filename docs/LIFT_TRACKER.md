# Lift Tracker (Strength tile)

The Strength tile is a full lift tracker: log sets, auto-fill from last time, swap and add exercises, rest timer, history, records, muscle volume, share card, optional lift videos, and automatic Intervals.icu logging.
Route: `#/strength/...` (code: `src/lifts/` domain + storage, `src/ui/lifts/` screens).

## What is deliberately NOT in it
Auto-progression, built-in programs, photos / social, how-to or demo content, plate calculator. The master exercise list is names + muscle mapping only. You build your own "days" (repeating workouts).

## Screens
| Tab / route | What it does |
|---|---|
| Train `#/strength` | Resume strip, today's Intervals.icu plan (Start pairs the workout with the planned event), **Custom Workout** freestyle card, **My Days** (your repeating workouts) |
| Session `#/strength/session` | The workout. Boostcamp-style cards, PREVIOUS column, forward-fill, steppers dock, rest timer, supersets |
| Summary `#/strength/summary/<id>` | Totals, new records, effort rating, upload status, share card, save as a day |
| History `#/strength/history` | Month calendar + list; detail page per workout (edit sets, repeat, delete, share) |
| Progress `#/strength/progress` | Weekly hard sets per muscle, workouts/week, your lifts (est. 1RM), recent records, lift videos |
| Exercises `#/strength/exercises` | Master list (+ custom). Detail page: **1RM-5RM** (more up to 12RM), est. 1RM chart, videos, history |
| Settings `#/strength/settings` | Units, default rest, record delay, backup export / import |
| `#/strength/hr` | The old heart-rate-only session (for lifting in another app) is kept here |

## Logging rules
* **Auto-fill:** every unlogged set is pre-filled from the matching set of the last time you did that exercise (warm-ups pair with warm-ups, working sets with working sets; extra sets reuse the last set). Suggestions render dim; typing or logging makes them real.
* **Forward-fill:** a weight / reps value typed or logged in set *n* flows into later sets that are still untouched suggestions. Never overwrites something you typed or a logged set. Warm-ups neither push nor receive.
* **Tap PREVIOUS** to copy last time's numbers into the set. **−/+ Set** under every card (with Undo).
* **Dock** above the keyboard: weight −5 −2.5 +2.5 +5 (kg: 2.5 / 1.25), reps −2 −1 +1 +2, and **LOG ✓** (jumps to the next set).
* Set types: Normal, Warm-up, Drop, To failure. Warm-ups never count anywhere. Optional per-set RPE (6-10 in halves) and note.
* Exercise types: weight × reps, bodyweight (+ optional load), time (seconds, e.g. triceps iso hold).
* **Swap:** "Just today" / "This day · *name*" (updates that repeating day) / "Everywhere" (every day that uses it). Swap suggestions list similar exercises first (shared primary muscles).
* Supersets: link an exercise with the next one; spanning left bracket wraps the full pair (`A` + `B`) with alternating colors across consecutive supersets (`1A/1B`, `2A/2B`, …).
* Rest timer: every card footer has an `Auto` toggle pill (OFF by default on `A` mid-superset, ON on `B` and standalone lifts), a 1-tap `▶` quick-start button, and a `⚙` Timer Settings cog (plus master toggle in Settings), with chime + haptic at zero and lock-screen notification updates.
* Sticky exercise notes ("seat 4, neutral handles") appear every time the exercise comes up.

## Records & PRs
* **Rep records:** heaviest load for *at least* n reps, n = 1..12. Table shows 1RM-5RM by default; ties keep the earliest date.
* **PR:** a set whose load beats every earlier set of ≥ that many reps. First time you ever log an exercise is not flagged. PRs are *derived* (recomputed from history), so edits and imports can never leave stale badges.
* **Est. 1RM:** Epley `w·(1 + reps/30)`, shown only for ≤12 reps. Bodyweight lifts use body mass (from Intervals.icu) + added load.

## Muscle tracking (recommended: yes, kept simple)
Hard sets per muscle per week is the one volume metric worth watching. A set counts **1.0 to each primary mover and 0.5 to each secondary mover** (fractional counting, as used in the volume literature and by Hevy/Strong-style trackers), warm-ups excluded. The Progress bars show a 10-20 sets/week band; it is a visual reference only, nothing is prescribed or progressed automatically. 17 muscles (Chest, Lats, Upper Back, Traps, Front/Side/Rear Delts, Biceps, Triceps, Forearms, Quads, Hamstrings, Glutes, Adductors, Calves, Abs, Lower Back). Every library exercise has a primary / secondary mapping; you can edit the mapping on any exercise (Exercise page → ⋯) and set it for custom ones.

## Intervals.icu (automatic)
Finish → the workout is saved locally first, then uploaded with no further taps:
1. Activity created. Heart-rate strap connected → TCX with the HR trace (+ Keytel kcal). No strap → manual activity (`POST /athlete/{id}/activities/manual`, fields verified against the Intervals.icu OpenAPI).
2. Patched with type `WeightTraining`, the **lift log as the description**, effort rating (`icu_rpe`), and `kg_lifted`.
3. Paired with today's planned event when you started from it (the rolling plan advances as before).

Both steps are idempotent and retried automatically (offline at the gym is fine; the Home banner shows pending uploads). Tapping an effort rating after the upload re-sends the description/RPE. Intervals.icu has no native sets/reps view, so the description is the lift log:
```
Armory Fit · Upper 1
52 min · 18 sets · 12,450 lb · 2 PRs

Barbell Bench Press: 3×5 @ 175 lb  ★ 5RM PR
Lat Pulldown: 3×10 @ 130 lb
avg HR 118 · max HR 151 · ~310 kcal · RPE 8/10
```

## Share card
1080×1350 PNG: name, date, sets / volume / time / PRs, one condensed line per exercise (`3×5 @ 175`, or `5 sets · top 195×1` when sets differ a lot), ★ on PRs, HR line if recorded. Native share sheet on Android (Capacitor Share + Filesystem), Web Share in the browser, download fallback.

## Record Lift (video)
Set options → **Record** (in-app camera, video only, start delay 0/3/5/10 s, 2 min max, review/retake) or **Attach** (pick from the phone). The clip is linked to that exact set/attempt and is playable from: the set, workout history, the exercise's **Rep Records** rows (a camera icon appears when the record set has a clip), the exercise's Videos grid, and Progress → Lift videos. Videos live in IndexedDB on the phone (persistent storage is requested); they are not uploaded anywhere and are not in the JSON backup.
Android needs the `CAMERA` permission (added to the manifest), so install a freshly built APK.

## Data & backup
IndexedDB `armory-fit` v2: `lift_sessions`, `lift_routines`, `lift_ex` (custom exercises + per-exercise note/rest/muscle overrides), `lift_videos` (meta + blobs). Settings → **Export backup** writes `{ app: "armory-lifts", v: 1, sessions, routines, exercises }` JSON; **Import** merges by id. Weights are stored in kg; the lb/kg switch is display only.

A session looks like:
```json
{ "id": "…", "name": "Upper 1", "routineId": null, "eventId": null, "startMs": 1763296200000, "endMs": 1763299320000,
  "status": "done", "rpe": 8, "bwKg": 88.5,
  "exs": [ { "uid": "…", "exId": "barbell-bench-press", "sets": [ { "id": "…", "w": 79.4, "r": 5, "done": true, "kind": "n", "rpe": 8 } ] } ] }
```
Library exercise ids are the slug of the name (`barbell-bench-press`, `belt-squat`, …), so history can be imported from other apps by mapping names to ids.
