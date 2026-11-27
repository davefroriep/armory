# Armory Chest Strap Wi-Fi Button Pod — Master Engineering Spec (`Rev v2.2`)

## Executive Summary

The **Armory Chest Strap Wi-Fi Button Pod (`Rev v2.2` — Version C Eased-Wing Single-Button)** keeps the ultra-compact `47.00 × 37.60 × 10.50 mm` main pod housing a **`502030` 250 mAh 3.7 V LiPo pouch**, floated **Seeed Studio XIAO ESP32-C6**, and single `6×6×4.3 mm` tactile switch (`SW1` above the XIAO) alongside **monolithic eased-transition strap wings** (`62.20 mm` overall wing-to-wing). `Rev v2.2` upgrades the `SW1` cradle with solid top/bottom bars + `3.00 mm` left/right center locking bars (clearing all 4 corner switch legs cleanly) and shortens the Lid plunger stalk by `0.50 mm` (`Z = 6.15 mm`) so the paddle rests flat with zero pre-load.

---

## 1. Mechanical Envelope & Z-Stackup (`Method 2` Solid Floor, `DRC 13 / 34`)

- **Main Pod Body (`v2.1`):** `47.00 mm (L) × 37.60 mm (W) × 10.50 mm (H)` (`12.10 mm` with `1.60 mm` optional anti-mash guard bezel)
- **Overall Width w/ Eased 30 mm Strap Wings:** **`62.20 mm (L) × 37.60 mm (W)`** (`0.00 mm` added Z thickness; `2.8 mm` shorter than Polar H10)
- **Eased-Transition Monolithic Strap Wings (`Z = 0.00..2.60 mm`):**
  - Zero inner stub: **`4.80 mm` wide open slots** (`31.20 mm` tall) start flush against Base walls (`X = 0.00` / `X = 47.00`)
  - **Base Underside Transition Ramps:** **`1.80 × 2.00 mm`** bevels along `-X` and `+X` (`6.60 mm` lead-in so strap transitions gently under the floor)
  - **Outer Slotted C-Bars:** **`2.60 mm` tall × `2.80 mm` wide** with **`0.90 mm` top-edge bevels** (smooth flow OVER bars) and centered `2.60 -> 4.20 mm` flared slip-in slits (clears Polar plastic end clips without unbuckling)
- **Bottom-Up Z-Stackup (mated coordinates):**
  - `Z = 0.00..1.20 mm` (`1.20 mm`): 100% solid continuous PETG bottom floor (`Method 2`), `0.80 mm × 45°` anti-elephant-foot outer chamfer (`DRC 29`)
  - `Z = 1.20..4.50 mm` (`3.30 mm`): Floated XIAO ESP32-C6 corner support piers (`component_lib` bare-PCB zones only; open airgap beneath `ANT1` & header pins per `DRC 24/37`)
  - `Z = 1.20..4.80 mm` (`3.60 mm`): True `SlotOverall(10.00, 3.60)` stadium USB-C port flush in `-Y` wall (`WALL_USB = 1.60 mm`, flat butt seam per `DRC 14`)
  - `Z = 1.20..5.50 mm`: Single `6×6×4.3 mm` tactile switch (`SW1`) seated at `(34.31, +10.50)` in a `6.70 × 6.70 mm` cradle (solid top/bottom bars + `3.00 mm` `-X/+X` center bars clearing all 4 switch legs cleanly)
  - `Z = 5.60 mm`: Base/Lid parting plane (`0.80 mm` solid bridge above USB-C stadium -> clean un-notched Lid skirt)
  - `Z = 5.60..8.80 mm` (`3.20 mm`): Stepped `1.45 mm` shoulder tongue on `-X/+X/+Y` with **2× `10 mm` slotted cantilever snap tabs** (`0.55 mm` barb, `0.10 mm` radial gap = **`0.45 mm` net interference**, `1.15 mm` solid Lid retaining lip per `DRC 10/30`)
  - `Z = 6.15..8.95 mm`: Shortened `-0.50 mm` Lid plunger stalk above `SW1` (flush resting paddle, zero pre-load)
  - `Z = 8.95..10.50 mm` (`1.55 mm`): Solid Lid roof (`ARMORY v2.2` `0.35 mm` deboss leaves `1.20 mm` solid backing per `DRC 8`)
  - `Z = 10.50..12.10 mm` (`1.60 mm`): Optional removable/CA-bond Anti-Mash Barbell Guard Bezel (`4× Ø1.40 mm` alignment pins mate into `0.35 mm` blind Lid dimples)

---

## 2. Bill of Materials (BOM)

| Ref | Component | Qty | Dimensions / Spec | Purpose / Sourcing |
|---|---|---|---|---|
| `U1` | **Seeed Studio XIAO ESP32-C6** | 1 | `20.96 × 17.78 × 4.8 mm` (USB-C, built-in 100 mA LiPo charger) | Wi-Fi MCU, USB-C rechargeable, LP-GPIO deep-sleep wake |
| `BT1` | **3.7 V `502030` LiPo Pouch w/ PCM** | 1 | `250 mAh`, `5.0 × 20.0 × 30.0 mm`, built-in protection circuit | Internal battery (~10–15 hr active workout runtime, months shelf) |
| `SW1` | **Tactile Push-Button Switch** | 1 | `6.0 × 6.0 × 4.3 mm` 4-pin TH/SMT momentary (`250 gf`+ recommended) | Single wide paddle button (`1× Check`, `2× Record`, `Hold Voice`) |
| `W1–W4` | **30 AWG Silicone Hookup Wire** | 4 | Ultra-flexible `200 °C` silicone stranded (Red/Black/Blue/Slate) | Direct-soldered flexible point-to-point leads (~155 mm total) |
| `P1` | `armory_strap_button_base.step` | 1 | `62.2 × 37.6 × 8.8 mm` PETG (~7.5 g) | Monolithic Base + eased `4.8 mm` strap wings + `502030` + XIAO + `SW1` |
| `P2` | `armory_strap_button_lid.step` | 1 | `47.0 × 37.6 × 4.9 mm` PETG (~4.0 g) | Single-button wide cantilever flex-paddle lid (`ARMORY v2.1`) |
| `P3` | `armory_strap_button_bezel.step` | 1 | `24.8 × 14.6 × 1.90 mm` PETG (~1.0 g) | Optional `1.60 mm` raised barbell/chest-pad anti-mash guard |

---

## 3. Consolidated Electrical Netlist & Wire Cut Lengths (`DRC 16`)

*(Direct-soldered belly-down `U1` pads to `SW1` and `BT1`; **longest single wire run = `32 mm`**, jacket strip allowance = **`4.0 mm` per end**, total wire stock **`155 mm`**, including `+5.0 mm` snip excess per run.)*

| Net Name | From (Pin / Pad) | To (Pin / Pad) | Gauge / Color | Geometric Path Length | Cut Length (`+5 mm` Trim & Strip) | MCU GPIO / Role |
|---|---|---|---|---|---|---|
| `BAT+` | `BT1 (+ Red PCM Lead)` | `U1 (BAT+ Underside Pad)` | 30 AWG Red | `24 mm` | **`34 mm`** (trim factory `BT1` lead) | Unregulated `3.0–4.2 V` LiPo + 100 mA USB-C charge |
| `BAT-` | `BT1 (- Black PCM Lead)` | `U1 (BAT- Underside Pad)` | 30 AWG Black | `24 mm` | **`34 mm`** (trim factory `BT1` lead) | LiPo ground return via PCM |
| `BTN1_D1` | `U1 (D1 / GPIO1)` | `SW1 (Left Leg Pair)` | 30 AWG Blue | `32 mm` | **`42 mm`** | LP-GPIO Wake (`LOW`) + `INPUT_PULLUP` -> `SW1` (`ONE_BUTTON`) |
| `GND_SW` | `U1 (GND)` | `SW1 (Right Leg Pair)` | 30 AWG Slate | `28 mm` | **`38 mm`** | Switch ground return |

---

## 4. Flash Studio Slicer Profile (`Flashforge Adventurer 5M Pro` — PETG)

- **Filament:** PETG (already in shop) — do NOT enable extra global XY elephant-foot compensation (built-in `0.80 mm × 45°` perimeter chamfers keep `ARMORY v2.1` sharp).
- **Layer Height:** `0.20 mm` (`0.20 mm` initial layer) — **Supports: OFF (all 3 `.step` parts print flat at `Z = 0.00 mm`, `0` overhangs)**.
- **Line Width / Walls:** `0.40 mm` nozzle, **`3` wall loops**, `25%` Gyroid or Grid infill.

---

## 5. Gate 5 Visual Verification & Workbench Blueprint

All 3 printable `.step` files (`models/`) passed Gate 4 OpenCASCADE kernel verification (`0.000000 mm³` mated interference, `100%` support-free printability at `Z = 0.00 mm`, `DRC 10/13/14/22/24/29/34/37/39`) and Gate 5 visual inspection:

- **3D Component & Build-Plate Sheet:** [`armory_strap_button_verification.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_verification.png)
- **True BRep Internal Cross-Sections:** [`armory_strap_button_internal_mechanics.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_internal_mechanics.png)
- **30 mm Polar Strap Eased Flow & Exploded 3D:** [`armory_strap_button_context_exploded_3d.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_context_exploded_3d.png)
- **DRC 42 Workbench Wiring & Strap Transition Blueprint:** [`armory_strap_button_wiring_blueprint.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_wiring_blueprint.png) ([SVG](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_wiring_blueprint.svg))
