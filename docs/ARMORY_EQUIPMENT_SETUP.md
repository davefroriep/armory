# The Armory — Equipment, Machines & Exercise Inventory

> **Status:** Initial verified baseline synced with `SKILL.md` (`v0.37`) and `src/lifts/library.ts`.  
> **Pending Walkthrough (`docs/TODO.md` #1):** Full bay-by-bay audit with Dave covering every bar, attachment, pulley setup, standalone machine, and custom-rigged movement so zero equipment assumptions remain.

---

## 1. Confirmed Equipment & Active `exId` Baseline

### Power Rack, Barbells & Attachments
*(Confirmed: Dave owns **no Safety Squat Bar (SSB)** — removed from all skill/program docs in `v0.37`.)*
- **Rogue 4-Post Power Rack**
- **Ohio Bar**
- **Landmine Attachment**
- *Seeded / Active Exercise Slugs (`src/lifts/library.ts`):*
  - Squat / Hinge: `low-bar-squat`, `barbell-back-squat`, `paused-squat`, `front-squat`, `deadlift`, `romanian-deadlift`, `hip-thrust`, `hack-squat` *(verify exact rack/rigging setup)*
  - Press / Row / Landmine: `barbell-bench-press`, `paused-bench-press`, `incline-barbell-bench-press`, `close-grip-bench-press`, `overhead-press`, `barbell-row`, `pendlay-row`, `t-bar-row`, `landmine-press`, `landmine-overhead-press`

### Smith Machine & Functional Trainer / Cable Tower *(Belt Squat Capable)*
- *Seeded / Active Exercise Slugs (`src/lifts/library.ts`):*
  - **Smith Machine:** `smith-machine-squat`, `smith-machine-romanian-deadlift`, `smith-machine-bulgarian-split-squat`, `smith-machine-hip-thrust`, `smith-machine-bench-press`, `smith-machine-incline-press`, `smith-machine-overhead-press`, `smith-machine-row`, `standing-calf-raise`
  - **Cables / Tower / Belt Squat:** `belt-squat`, `cable-leg-press`, `lat-pulldown`, `neutral-grip-lat-pulldown`, `close-grip-lat-pulldown`, `seated-cable-row`, `single-arm-cable-row`, `cable-crunch`, `cable-triceps-pushdown`, `straight-bar-pushdown`, `overhead-cable-triceps-extension`, `face-pull`, `cable-lateral-raise`, `cable-rear-delt-fly`, `cable-curl`, `bayesian-cable-curl`, `cable-crossover`, `low-to-high-cable-fly`, `triceps-isometric-hold`

### Standalone / Rack-Integrated Machines & Bodyweight
- **GHD:** `glute-ham-raise`, `back-extension`
- **Lying Leg Curl:** `lying-leg-curl`
- **Leg Extension:** `leg-extension`
- **Chest Fly / Pec Deck Machine:** `chest-fly-machine`, `reverse-pec-deck`
- **Pull-Up / Dip Station + Dip Belt (`150+ lb`):** `pull-up`, `chin-up`, `neutral-grip-pull-up`, `dip`, `hanging-leg-raise`

### Free Weights, Dumbbells & Accessories
- **Dumbbells (`5–100 lb`):** `dumbbell-bench-press`, `incline-dumbbell-press`, `seated-dumbbell-shoulder-press`, `dumbbell-lateral-raise`, `rear-delt-fly`, `dumbbell-row`, `chest-supported-dumbbell-row`, `dumbbell-romanian-deadlift`, `bulgarian-split-squat`, `goblet-squat`, `dumbbell-curl`, `incline-dumbbell-curl`, `hammer-curl`, `farmers-carry`
- **Adjustable Benches**
- **Lifting Straps**
- **Weighted Pack / Ruck Vest (for `Ruck / Outdoor HR`):** supports `20`, `30`, `35` (Tough Ruck Boston target), `45 lb` packs.

### Cardio Bay
- **Assault AirBike Elite** + ESP32-C6 (`AssaultBike`, calibrated `ratio = 11.1100`, 5 Hz NUS + passive FTMS)
- **Bowflex C7 Spin Bike** + `SS2K_Tuner` ESP32-WROOM *(calibration pending — `docs/TODO.md` #3)*
- **Horizon T101 Treadmill** + `Horizon T101 Pod` *(telemetry/HUD build pending — `docs/TODO.md` #2)*
- **Polar H10** chest strap (`0x180D` / `0x2A37`)

---

## 2. Pending Detailed Inventory & Custom-Rigged Exercise Breakdown *(To Fill With Dave)*

- [ ] **Specialty Bars & Handles:** Exact curl bars, Swiss/multi-grip bars, cable attachments (D-handles, rope, MAG grips, lat bars, belt-squat harness/belt, ankle straps, landmine handles), plate loading/increment plates.
- [ ] **Exact Machine Models & Dual-Function Stations:** Which machines are separate vs. combined (e.g. leg curl/extension, pec deck/rear delt, Smith + functional trainer stack weights / pulley ratios `2:1` vs `1:1`).
- [ ] **Custom-Rigged Exercises Dave Uses:** Exact setups (`hack-squat` rigging, `cable-leg-press` rigging, `triceps-isometric-hold` rig, calf block setup, box height, etc.) and any missing library entries to add to `src/lifts/library.ts`.
