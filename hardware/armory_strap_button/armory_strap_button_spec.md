# Armory Chest Strap Wi-Fi Button Pod — Master Engineering Spec (`Rev v1.1`)

## Executive Summary

The **Armory Chest Strap Wi-Fi Button Pod (`Rev v1.1`)** is a slim, low-profile wireless chest-strap remote designed for Armory Fit to trigger set completion (`check`), workout video tagging (`record`), voice ducking (`voice`), and `undo` directly over gym Wi-Fi (`POST http://192.168.1.152:8766/api/bt-button/trigger`) without BLE sleep timeouts or dropped first clicks. Built around a **Seeed Studio XIAO ESP32-C6**, a **`502030` 250 mAh 3.7 V LiPo pouch** (USB-C rechargeable via the XIAO's built-in 100 mA charger), and two **`6×6×4.3 mm` tactile switches** in a side-by-side layout on a **30 mm Polar chest strap**, the enclosure keeps overall thickness to **11.10 mm** (**12.90 mm** with the optional anti-mash barbell guard bezel) while protecting the battery behind an internal PETG armor divider. Flush **slotted slip-in C-bar strap wings** (`3.00 × 3.40 mm` bars with centered `2.60 -> 4.20 mm` flared entry slits) let the Polar elastic band weave in sideways cleanly without removing either bulky plastic strap clip. Both interchangeable **2-button** (raised horizontal bars + recessed thumb dimple) and **1-button** PETG cantilever flex-paddle snap lids click onto the same base shell with slotted positive-click snaps.

---

## 1. Mechanical Envelope & Z-Stackup (`Method 2` Solid Floor, `DRC 13 / 34`)

- **Main Pod Body:** `56.40 mm (L) × 37.60 mm (W) × 11.10 mm (H)` (`12.90 mm` with `1.80 mm` optional anti-mash bezel)
- **Overall Width w/ Flush 30 mm Polar Strap Wings:** `71.60 mm (L) × 37.60 mm (W)` (`0.00 mm` added Z thickness)
- **Polar Slotted C-Bar Wings:** `31.20 mm × 2.80 mm` strap slots (`Z = 0.00..3.40 mm`), framed by `3.00 × 3.40 mm` PETG outer bars with centered `2.60 mm` throat / `4.20 mm` 45°-flared sideways slip-in slits (clears Polar plastic end clips without unbuckling)
- **Bottom-Up Z-Stackup (mated coordinates):**
  - `Z = 0.00..1.20 mm` (`1.20 mm`): 100% solid continuous PETG bottom floor (`Method 2`), `0.80 mm × 45°` anti-elephant-foot outer chamfer (`DRC 29`)
  - `Z = 1.20..4.50 mm` (`3.30 mm`): Floated XIAO ESP32-C6 corner support piers (`component_lib` bare-PCB zones only; `100%` open airgap beneath `ANT1` ceramic chip & all header pins per `DRC 24/37`)
  - `Z = 1.20..4.80 mm` (`3.60 mm`): True `SlotOverall(10.00, 3.60)` stadium USB-C port flush in `-Y` wall (`WALL_USB = 1.60 mm`, flat butt seam per `DRC 14`)
  - `Z = 1.20..5.50 mm`: `6×6×4.3 mm` tactile switches seated in snug `6.70 × 6.70 mm` PETG retention cradles on solid floor
  - `Z = 5.80 mm`: Base/Lid parting plane (`1.00 mm` solid PETG bridge above USB-C stadium -> clean un-notched Lid skirt)
  - `Z = 5.80..9.30 mm` (`3.50 mm`): Stepped `1.45 mm` shoulder tongue on `-X/+X/+Y` with **2× `10 mm` slotted cantilever snap tabs** (`0.60 mm` barb, `0.10 mm` radial slip gap = **`0.50 mm` net interference**, `1.21 mm` solid Lid retaining lip per `DRC 10/30`)
  - `Z = 9.50..11.10 mm` (`1.60 mm`): Solid Lid roof (`ARMORY v1.1` `0.40 mm` deboss leaves `1.20 mm` solid backing per `DRC 8`)
  - `Z = 11.10..12.90 mm` (`1.80 mm`): Optional removable/CA-bond Anti-Mash Barbell Guard Bezel (`4× Ø1.44 mm` alignment pins mate into `0.40 mm` blind Lid dimples)

---

## 2. Bill of Materials (BOM)

| Ref | Component | Qty | Dimensions / Spec | Purpose / Sourcing |
|---|---|---|---|---|
| `U1` | **Seeed Studio XIAO ESP32-C6** | 1 | `20.96 × 17.78 × 4.8 mm` (USB-C, built-in 100 mA LiPo charger) | Wi-Fi MCU, USB-C rechargeable, LP-GPIO deep-sleep wake |
| `BT1` | **3.7 V `502030` LiPo Pouch w/ PCM** | 1 | `250 mAh`, `5.0 × 20.0 × 30.0 mm`, built-in protection circuit | Internal battery (~10–15 hr active workout runtime, months shelf) |
| `SW1` | **Tactile Push-Button Switch (Top)** | 1 | `6.0 × 6.0 × 4.3 mm` 4-pin TH/SMT momentary (recommend `250 gf`+) | Primary button (`Done / Check` single press; `Undo` long press) |
| `SW2` | **Tactile Push-Button Switch (Bot)** | 1 | `6.0 × 6.0 × 4.3 mm` 4-pin TH/SMT momentary (recommend `250 gf`+) | Secondary button (`Voice` single press; `Record` long/double press) |
| `W1–W5` | **30 AWG Silicone Hookup Wire** | 5 | Ultra-flexible `200 °C` silicone stranded (Red/Black/Blue/Green/Slate) | Direct-soldered flexible point-to-point harnesses (~265 mm total) |
| `P1` | `armory_strap_button_base.step` | 1 | `71.6 × 37.6 × 9.3 mm` PETG (~8.5 g) | Main strap base + slotted clip-clearing wings + LiPo bay + XIAO piers |
| `P2A` | `armory_strap_button_lid_2btn.step` | 1 | `56.4 × 37.6 × 5.3 mm` PETG (~5 g) | Primary 2-button tactile-coded flex-paddle lid (Bars + Dimple) |
| `P2B` | `armory_strap_button_lid_1btn.step` | 1 | `56.4 × 37.6 × 5.3 mm` PETG (~5 g) | Interchangeable 1-button wide flex-paddle lid |
| `P3` | `armory_strap_button_bezel.step` | 1 | `21.0 × 31.2 × 2.15 mm` PETG (~1.5 g) | Optional `1.80 mm` raised barbell/chest-pad anti-mash guard |

---

## 3. Consolidated Electrical Netlist & Wire Cut Lengths (`DRC 16`)

*(Direct-soldered belly-down `U1` pads to `SW1`/`SW2` and `BT1`; **longest single wire run = `42 mm`**, total pre-strip jacket strip allowance = **`4.0 mm` per end**, total wire stock **`265 mm`**, including `+5.0 mm` snip excess per run.)*

| Net Name | From (Pin / Pad) | To (Pin / Pad) | Gauge / Color | Geometric Path Length | Cut Length (`+5 mm` Trim & Strip) | MCU GPIO / Role |
|---|---|---|---|---|---|---|
| `BAT+` | `BT1 (+ Red PCM Lead)` | `U1 (BAT+ Underside Pad)` | 30 AWG Red | `28 mm` | **`38 mm`** (trim factory `BT1` lead) | Unregulated `3.0–4.2 V` LiPo + 100 mA USB-C charge |
| `BAT-` | `BT1 (- Black PCM Lead)` | `U1 (BAT- Underside Pad)` | 30 AWG Black | `28 mm` | **`38 mm`** (trim factory `BT1` lead) | LiPo ground return via PCM |
| `BTN1_D1` | `U1 (D1 / GPIO1)` | `SW1 (Left Leg Pair)` | 30 AWG Blue | `32 mm` | **`42 mm`** | LP-GPIO Wake (`LOW`) + `INPUT_PULLUP` -> Top Paddle (`SW1`) |
| `BTN2_D2` | `U1 (D2 / GPIO2)` | `SW2 (Left Leg Pair)` | 30 AWG Green | `26 mm` | **`36 mm`** | LP-GPIO Wake (`LOW`) + `INPUT_PULLUP` -> Bottom Paddle (`SW2`) |
| `GND_SW` | `U1 (GND)` | `SW2 (Right Leg) -> SW1 (Right Leg)` | 30 AWG Slate | `42 mm` (daisy-chain) | **`52 mm`** (strip middle `5 mm` at `SW2`) | Shared switch ground return |

*(Note: Verify exact `U1` polarity cleanly — on Seeed XIAO ESP32-C6 belly-down, `BAT+` / `BAT-` solder pads face **up** in Bay 2 for effortless soldering after hot-gluing the board onto its `3.30 mm` corner piers. Check factory `BT1` PCM lead polarity with a multimeter before soldering.)*

---

## 4. Flash Studio Slicer Profile (`Flashforge Adventurer 5M Pro` — PETG)

- **Filament:** PETG (already in shop) — do NOT enable extra global XY elephant-foot compensation (built-in `0.80 mm × 45°` perimeter chamfers handle first-layer squish while keeping `ARMORY v1.1` lettering crisp).
- **Layer Height:** `0.20 mm` (`0.20 mm` initial layer) — guarantees exact `3` full layers (`0.60 mm`) on the cantilever snap barb crests and crisp `0.40 mm` tactile grooves.
- **Line Width / Walls:** `0.40 mm` nozzle, **`3` wall loops**, `25%` Gyroid or Grid infill, **Supports: OFF (100% support-free at `Z=0`)**.
- **Orientation:** Import all 4 `.step` files directly onto the build plate as exported (`Z = 0.00 mm` flat — Base floor down, Lids roof down with `ARMORY v1.0` facing the bed, Bezel flat top down with tiny `0.35 mm` locating pins pointing `+Z`).

---

## 5. Gate 5 Visual Verification & Workbench Blueprint

All 4 printable `.step` files (`models/`) passed Gate 4 OpenCASCADE kernel verification (`0.000000 mm³` mated interference, `100%` support-free printability at `Z = 0.00 mm`, `80.0%` chiral split parity, `DRC 10` cantilever snaps, `DRC 24/37` XIAO airgap piers & keepouts) and Gate 5 BRep/PyVista visual inspection (PNG/SVG sheets stored in the session artifact folder so `models/` remains strictly `.step`-only):

- **3D Component & Build-Plate Verification:** [`armory_strap_button_verification.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_verification.png)
- **True BRep Internal Cross-Sections:** [`armory_strap_button_internal_mechanics.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_internal_mechanics.png)
- **30 mm Polar Strap Scale & Exploded Assembly:** [`armory_strap_button_context_exploded_3d.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_context_exploded_3d.png)
- **DRC 42 Workbench Wiring & Lid Blueprint:** [`armory_strap_button_wiring_blueprint.png`](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_wiring_blueprint.png) ([SVG](file:///C:/Users/davef/.gemini/antigravity/brain/17b9157d-6878-4a18-8c64-aa205aebb08d/armory_strap_button_wiring_blueprint.svg))
