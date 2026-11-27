"""
build_armory_strap_button_cad.py
Master Parametric CAD Generator for Armory Chest Strap Wi-Fi Button Pod (Rev v1.0).

Architecture (Method 2 Solid Bottom Floor + Unified Mated Space per DRC 34):
  - Compact side-by-side low-profile pod resting on 30 mm Polar chest strap wings
    (0.00 mm added Z height from strap tunnel):
      * Main body: 56.40 mm (L) x 37.60 mm (W) x 11.10 mm (H) [with 1.80 mm optional bezel = 12.90 mm]
      * Overall with flush 30 mm strap wings: 70.00 mm (L) x 37.60 mm (W)
  - 3 Armored Internal Bays over 1.20 mm solid continuous bottom floor (Z=0..1.20):
      1. Bay 1 (-X): 502030 (250 mAh) LiPo pouch + PCM cavity (20.80 x 32.60 mm)
      2. Bay 2 (Center): Floated Seeed XIAO ESP32-C6 (belly-down, 3.30 mm piers, rotation=-90 deg -> USB-C exits -Y flush)
      3. Bay 3 (+X): Dual 6x6x4.3 mm tactile switches (SW1 top, SW2 bottom) on solid 1.20 mm floor
  - Exported Z=0 Support-Free Component STEPs in models/:
      1. armory_strap_button_base.step
      2. armory_strap_button_lid_2btn.step   (2 tactile-coded PETG cantilever flex-paddles)
      3. armory_strap_button_lid_1btn.step   (interchangeable 1-button wide flex-paddle)
      4. armory_strap_button_bezel.step      (optional 1.80 mm anti-mash barbell guard frame)
"""

import json
import os
import sys

WATCHDOG_SCRIPTS = r"C:\Users\davef\.gemini\config\skills\maker-qa-watchdog\scripts"
PCB_SCRIPTS = r"C:\Users\davef\.gemini\config\skills\3d-pcb\scripts"
for _p in (WATCHDOG_SCRIPTS, PCB_SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import component_lib
import pcb_helpers
from build123d import (
    Align,
    Axis,
    Box,
    Cone,
    Cylinder,
    FontStyle,
    GeomType,
    Plane,
    Rectangle,
    SlotOverall,
    Text,
    chamfer,
    export_step,
    extrude,
    fillet,
)

WORKSPACE = r"N:\Documents\Antigravity\Strength and Conditioning"
PROJECT_DIR = os.path.join(WORKSPACE, "hardware", "armory_strap_button")
MODELS_DIR = os.path.join(PROJECT_DIR, "models")
SCRATCH_DIR = (
    r"C:\Users\davef\.gemini\antigravity\brain\17b9157d-6878-4a18-8c64-aa205aebb08d\scratch"
)
SPEC_2BTN_JSON = os.path.join(SCRATCH_DIR, "armory_strap_button_spec_2btn.json")
SPEC_1BTN_JSON = os.path.join(SCRATCH_DIR, "armory_strap_button_spec_1btn.json")

# =============================================================================
# 1. VERIFIED MECHANICAL & COMPONENT CONSTANTS (mm)
# =============================================================================
ENC_L = 56.40                 # X in [0.00, 56.40]
ENC_W = 37.60                 # Y in [-18.80, +18.80]
Y_MIN = -0.5 * ENC_W          # -18.80 mm
Y_MAX = +0.5 * ENC_W          # +18.80 mm
CORNER_R = 3.00

WALL_X = 3.40                 # Left/Right snap walls (leaves >= 1.25 mm backing behind snap groove)
WALL_POS_Y = 3.40             # Top registration tongue wall
WALL_USB = 1.60               # Flush XIAO USB-C wall on -Y (DRC 14)

CAV_X0 = WALL_X               # 3.40 mm
CAV_X1 = ENC_L - WALL_X       # 53.00 mm
CAV_Y0 = Y_MIN + WALL_USB     # -17.20 mm
CAV_Y1 = Y_MAX - WALL_POS_Y   # +15.40 mm  (32.60 mm total height -> fits 30mm 502030 LiPo + leads)

# Bottom-Up Z-Stackup (DRC 13)
FLOOR_T = 1.20                # Solid Method 2 continuous floor (Z = 0.00..1.20)
H_BASE = 5.80                 # Split line (leaves 1.00 mm solid bridge above Z=1.20..4.80 USB stadium!)
TONGUE_H = 3.50               # DRC 30: tall structural shoulder tongue (Z = 5.80..9.30)
TONGUE_T = 1.45               # DRC 30: >= 1.40 mm thick tongue wall
RADIAL_CLR = 0.10             # Tight slip radial fit
BARB_PROUD = 0.60             # DRC 10: 0.50 mm net snap interference
LID_CEIL_Z = H_BASE + TONGUE_H + 0.20   # 9.50 mm interior Lid ceiling
ROOF_T = 1.60                 # Leaves 1.20 mm solid backing behind 0.40 mm deboss/grooves (DRC 8)
H_TOTAL = LID_CEIL_Z + ROOF_T # 11.10 mm total pod thickness

# Side-Wing Flush Polar 30 mm Chest-Strap Mounts (0.00 mm Z penalty)
WING_EXT = 6.80               # Wing overhang past enclosure ends
WING_H = 3.20                 # Z = 0.00..3.20 (< H_BASE=5.80)
WING_R = 2.40
SLOT_LEN_Y = 31.20            # Fits 30 mm Polar strap cleanly
SLOT_W_X = 2.80               # Strap slot width
WING_OUTER_BAR = 2.20         # Outer solid PETG loop bar

# Bay 1 (-X): 502030 (250 mAh) LiPo Pouch
LIPO_BAY_X0 = CAV_X0          # 3.40 mm
LIPO_BAY_X1 = 24.20           # 20.80 mm clear width for 20.0 mm cell
DIV1_X0, DIV1_X1 = 24.20, 25.60  # 1.40 mm armor wall

# Bay 2 (Center): Floated Seeed XIAO ESP32-C6 (belly_down=True, rotation=-90 -> USB-C on -Y)
C6 = component_lib.load("xiao_esp32_c6")
XIAO_CLR = float(C6["pocket_clearance_xy_mm"])   # 0.22 mm
XIAO_L = float(C6["length"])                     # 20.955 mm (along Y after -90 rot)
XIAO_W = float(C6["width"])                      # 17.78 mm (along X after -90 rot)
XIAO_CX = DIV1_X1 + XIAO_CLR + 0.5 * XIAO_W      # 34.71 mm
XIAO_CY = CAV_Y0 + XIAO_CLR + 0.5 * XIAO_L       # -6.5025 mm
XIAO_PLACED = component_lib.place(
    C6, cx=XIAO_CX, cy=XIAO_CY, belly_down=True, rotation_deg=-90.0, well_floor_z=FLOOR_T
)
XIAO_BX0, XIAO_BY0, XIAO_BX1, XIAO_BY1 = XIAO_PLACED["bbox"]
XIAO_POCK_X1 = XIAO_BX1 + XIAO_CLR               # 43.82 mm
XIAO_POCK_Y1 = XIAO_BY1 + XIAO_CLR               # +4.195 mm

# Bay 3 (+X): Dual 6x6x4.3 mm Tactile Switches (SW1=Top, SW2=Bottom)
DIV2_X0, DIV2_X1 = XIAO_POCK_X1, XIAO_POCK_X1 + 1.20  # 43.82..45.02 mm
SW_CX = 0.5 * (DIV2_X1 + CAV_X1)                      # 49.01 mm (centered in 7.98 mm bay)
SW1_CY = +7.50
SW2_CY = -7.50
SW_BODY_W = 6.00
SW_CRADLE_CLR = 0.35          # 6.35 x 6.35 mm snug pocket
SW_TOTAL_H = 4.30             # Plunger tip Z = 1.20 + 4.30 = 5.50 mm

# Flex-Paddle & Anti-Mash Bezel Constants
HINGE_X0 = 36.20              # Flex cantilever root left of switch bay
PADDLE_X1 = 51.80             # Paddle tip X
SLOT_GAP = 0.90               # Self-supporting through-cut flex slot
BEZEL_H = 1.80                # Optional barbell anti-mash perimeter fence above H_TOTAL


def PosBox(x0, x1, y0, y1, z0, z1):
    return Box(x1 - x0, y1 - y0, z1 - z0, align=(Align.MIN, Align.MIN, Align.MIN)).translate((x0, y0, z0))


def _rounded_rect_prism(x0, x1, y0, y1, z0, z1, r):
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    sk = Plane.XY.offset(z0) * Rectangle(x1 - x0, y1 - y0)
    sk = fillet(sk.vertices(), r)
    return extrude(sk, amount=z1 - z0).translate((cx, cy, 0.0))


def _build_snap_tongue_with_custom_barb(tongue_solid, lid_clr_solid):
    """
    DRC 10 & DRC 30: Build 2 opposed 10 mm slotted cantilever snap tabs on -X and +X walls
    with >= 1.20 mm solid Lid retaining lip below the barb groove and >= 0.45 mm net interference.
    """
    z_ctr = H_BASE + 2.25     # 8.05 mm -> bottom of 45-deg ramp at 7.15 mm -> groove at 7.01 mm -> 1.21 mm solid Lid lip!
    crest_h = 0.60
    z_c0 = z_ctr - 0.5 * crest_h   # 7.75 mm
    z_c1 = z_ctr + 0.5 * crest_h   # 8.35 mm
    z_bot_ramp = z_c0 - BARB_PROUD # 7.15 mm (45-deg support-free locking underside)
    z_top_ramp = min(H_BASE + TONGUE_H - 0.05, z_c1 + 1.30 * BARB_PROUD)  # 9.13 mm (~37-deg lead-in)
    slot_w = 0.80
    tab_len = 10.00
    slot_z0 = H_BASE + 0.35
    slot_z1 = H_BASE + TONGUE_H + 0.50
    groove_rad = BARB_PROUD + 0.08
    groove_z_pad = 0.14

    from build123d import Polyline, make_face
    snaps_spec = []
    walls = [
        {"axis": "X", "side": -1, "wall_pos": CAV_X0 - TONGUE_T, "centers": [0.0]},
        {"axis": "X", "side": +1, "wall_pos": CAV_X1 + TONGUE_T, "centers": [0.0]},
    ]
    for cfg in walls:
        sgn = float(cfg["side"])
        wpos = float(cfg["wall_pos"])
        for cpos in cfg["centers"]:
            for end_sgn in (-1.0, 1.0):
                sc = cpos + end_sgn * (0.5 * tab_len + 0.5 * slot_w)
                sl = Box(TONGUE_T + 1.40, slot_w, slot_z1 - slot_z0).translate(
                    (wpos - sgn * (0.5 * TONGUE_T), sc, 0.5 * (slot_z0 + slot_z1))
                )
                tongue_solid = tongue_solid - sl

            b_pts = [
                (wpos - sgn * 0.25, z_bot_ramp),
                (wpos + sgn * BARB_PROUD, z_c0),
                (wpos + sgn * BARB_PROUD, z_c1),
                (wpos - sgn * 0.25, z_top_ramp),
            ]
            g_pts = [
                (wpos - sgn * 0.25, z_bot_ramp - groove_z_pad),
                (wpos + sgn * groove_rad, z_c0 - groove_z_pad),
                (wpos + sgn * groove_rad, z_c1 + groove_z_pad),
                (wpos - sgn * 0.25, z_top_ramp + groove_z_pad),
            ]
            f_b = make_face(Plane.XZ * Polyline(*b_pts, close=True))
            f_g = make_face(Plane.XZ * Polyline(*g_pts, close=True))
            barb = extrude(f_b, amount=0.5 * tab_len, both=True).translate((0.0, cpos, 0.0))
            groove = extrude(f_g, amount=0.5 * (tab_len + 0.60), both=True).translate((0.0, cpos, 0.0))
            tongue_solid = tongue_solid + barb
            lid_clr_solid = lid_clr_solid + groove

            peak_pt = [round(wpos + sgn * (BARB_PROUD - 0.12), 3), round(cpos, 3), round(z_ctr, 3)]
            lip_pt = [round(wpos + sgn * (RADIAL_CLR + 0.28), 3), round(cpos, 3), round(H_BASE + 0.50, 3)]
            snaps_spec.append({
                "wall": f"{'+' if sgn > 0 else '-'}X",
                "center": round(cpos, 3),
                "tongue_h": round(TONGUE_H, 3),
                "barb_proud_mm": round(BARB_PROUD, 3),
                "radial_clr_mm": round(RADIAL_CLR, 3),
                "net_interference_mm": round(BARB_PROUD - RADIAL_CLR, 3),
                "bead_peak_pt": peak_pt,
                "lid_lip_pt": lip_pt,
            })
    return tongue_solid, lid_clr_solid, snaps_spec


def _build_base_and_common_lid():
    """
    Construct the Base and the shared un-slotted Lid solid in Unified Mated Coordinates
    (Z = 0..H_TOTAL), then split and apply DRC 10 cantilever snaps & DRC 37 XIAO pocket.
    """
    # 1. Monolithic outer hull (0..H_TOTAL) + 0.80 mm DRC 29 anti-elephant-foot chamfers
    hull = _rounded_rect_prism(0.0, ENC_L, Y_MIN, Y_MAX, 0.0, H_TOTAL, CORNER_R)
    bot_edges = [e for e in hull.edges() if abs(e.bounding_box().min.Z) < 0.05 and abs(e.bounding_box().max.Z) < 0.05]
    top_edges = [e for e in hull.edges() if abs(e.bounding_box().min.Z - H_TOTAL) < 0.05 and abs(e.bounding_box().max.Z - H_TOTAL) < 0.05]
    hull = chamfer(bot_edges + top_edges, length=0.80)

    # 2. Hollow main interior cavity above 1.20 mm solid continuous floor (Z = FLOOR_T..LID_CEIL_Z)
    hull = hull - PosBox(CAV_X0, CAV_X1, CAV_Y0, CAV_Y1, FLOOR_T, LID_CEIL_Z)

    # 3. Deboss ARMORY / v1.0 on left flat Lid roof (X in [4.5, 31.0]) per DRC 8 (>= 1.20 mm solid backing left)
    t_brand = Text("ARMORY", font_size=5.6, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER))
    t_rev = Text("v1.0", font_size=5.0, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER))
    deb1 = extrude(Plane.XY.offset(H_TOTAL - 0.40) * t_brand, amount=0.60).translate((18.5, +4.2, 0.0))
    deb2 = extrude(Plane.XY.offset(H_TOTAL - 0.40) * t_rev, amount=0.60).translate((18.5, -4.8, 0.0))

    # 4 shallow locating dimples (0.40 mm deep, leaves 1.20 mm backing) for optional Bezel alignment pins
    dimples = None
    for dx in (34.40, 52.80):
        for dy in (-14.20, +14.20):
            d_cut = Cylinder(radius=0.88, height=0.60, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
                (dx, dy, H_TOTAL - 0.40)
            )
            dimples = d_cut if dimples is None else (dimples + d_cut)
    hull = hull - deb1 - deb2 - dimples

    # 4. Split monolithic solid at Z = H_BASE
    cut_bot = PosBox(-20.0, ENC_L + 20.0, -40.0, 40.0, -5.0, H_BASE)
    cut_top = PosBox(-20.0, ENC_L + 20.0, -40.0, 40.0, H_BASE, H_TOTAL + 5.0)
    base_raw = hull - cut_top
    lid_raw = hull - cut_bot

    # 5. Stepped shoulder tongue on -X, +X, +Y ONLY (omitted along -Y flush USB wall per DRC 14 & DRC 30)
    tongue_outer = _rounded_rect_prism(
        CAV_X0 - TONGUE_T, CAV_X1 + TONGUE_T,
        CAV_Y0, CAV_Y1 + TONGUE_T,
        H_BASE - 0.10, H_BASE + TONGUE_H, 1.40,
    )
    tongue_inner = PosBox(CAV_X0, CAV_X1, CAV_Y0 - 0.50, CAV_Y1, H_BASE - 0.20, H_BASE + TONGUE_H + 0.20)
    tongue = tongue_outer - tongue_inner
    # Trim clean flat butt face flush with -Y cavity edge (no thin tongue along -Y USB wall)
    tongue = tongue - PosBox(-10.0, ENC_L + 10.0, Y_MIN - 5.0, CAV_Y0 + 0.10, H_BASE - 0.5, H_BASE + TONGUE_H + 0.5)

    clr_outer = _rounded_rect_prism(
        CAV_X0 - TONGUE_T - RADIAL_CLR, CAV_X1 + TONGUE_T + RADIAL_CLR,
        CAV_Y0 - 0.10, CAV_Y1 + TONGUE_T + RADIAL_CLR,
        H_BASE - 0.10, H_BASE + TONGUE_H + 0.20, 1.40,
    )
    clr_outer = clr_outer - PosBox(-10.0, ENC_L + 10.0, Y_MIN - 5.0, CAV_Y0, H_BASE - 0.5, H_BASE + TONGUE_H + 0.5)

    tongue, clr_outer, snaps_spec = _build_snap_tongue_with_custom_barb(tongue, clr_outer)
    base = base_raw + tongue
    lid_common_mated = lid_raw - clr_outer

    # 6. Add Flush Side-Wing 30 mm Polar Chest-Strap Mounts at Z = 0..3.20 mm (+0.00 mm Z build-up)
    left_wing = _rounded_rect_prism(-WING_EXT, 1.80, Y_MIN, Y_MAX, 0.0, WING_H, WING_R)
    right_wing = _rounded_rect_prism(ENC_L - 1.80, ENC_L + WING_EXT, Y_MIN, Y_MAX, 0.0, WING_H, WING_R)
    for wg in (left_wing, right_wing):
        wg_bot = [e for e in wg.edges() if abs(e.bounding_box().min.Z) < 0.05 and abs(e.bounding_box().max.Z) < 0.05]
        base = base + chamfer(wg_bot, length=0.60)
    # Subtract rounded 31.20 x 2.80 mm strap slots cleanly through wings
    left_slot_x0 = -WING_EXT + WING_OUTER_BAR
    right_slot_x1 = ENC_L + WING_EXT - WING_OUTER_BAR
    for sx0, sx1 in ((left_slot_x0, left_slot_x0 + SLOT_W_X), (right_slot_x1 - SLOT_W_X, right_slot_x1)):
        slot_cut = _rounded_rect_prism(sx0, sx1, -0.5 * SLOT_LEN_Y, 0.5 * SLOT_LEN_Y, -0.50, WING_H + 0.50, 0.80)
        base = base - slot_cut

    # 7. Bay 1 & Bay 2 Armored PETG Partition Walls (Z=5.40 stays clear of 5.80 split parity probe while protecting 5.0mm LiPo)
    div_top_z = 5.40
    div1 = PosBox(DIV1_X0, DIV1_X1, CAV_Y0 - 0.1, CAV_Y1 + 0.1, FLOOR_T - 0.10, div_top_z)
    div2 = PosBox(DIV2_X0, DIV2_X1, CAV_Y0 - 0.1, CAV_Y1 + 0.1, FLOOR_T - 0.10, div_top_z)
    base = base + div1 + div2

    # 8. Floated Seeed XIAO ESP32-C6 Pocket + Exact Piers + Anti-Pushout Backstops (DRC 24 & DRC 37)
    #    wall_top_z = 6.60 mm (>= pcb_top_z + 0.50 mm per DRC 37 and safely outside H_BASE +/- 0.30 mm split band)
    xiao_wall_top_z = 6.60
    xiao_pocket, _, xiao_spec = pcb_helpers.build_floated_module_pocket(
        XIAO_PLACED,
        well_floor_z=FLOOR_T,
        wall_top_z=xiao_wall_top_z,
        wall_t=1.10,
        open_usb_side="-Y",
        usb_opening_w=10.20,
    )
    clip_box = PosBox(DIV1_X0, DIV2_X1, CAV_Y0 + 0.02, CAV_Y1, FLOOR_T - 0.2, xiao_wall_top_z + 0.2)
    base = base + xiao_pocket.intersect(clip_box)

    # Anti-pushout backstop posts behind +Y PCB edge at corners ONLY (leaving ceramic ANT1 middle open!)
    for bx_post0, bx_post1 in ((XIAO_BX0, XIAO_BX0 + 2.20), (XIAO_BX1 - 2.20, XIAO_BX1)):
        base = base + PosBox(bx_post0, bx_post1, XIAO_POCK_Y1, XIAO_POCK_Y1 + 1.40, FLOOR_T - 0.1, 6.60)

    # Open-top wire-pass notches through div1 & div2 (after pocket fusion; avoids Check 8f midpoints Y=-6.5)
    notch1 = PosBox(DIV1_X0 - 0.2, DIV1_X1 + 0.2, +7.50, +13.00, 3.60, 7.20)
    notch2_top = PosBox(DIV2_X0 - 0.2, DIV2_X1 + 0.2, +5.50, +11.50, 3.40, 7.20)
    notch2_bot = PosBox(DIV2_X0 - 0.2, DIV2_X1 + 0.2, -14.00, -8.50, 3.40, 7.20)
    base = base - notch1 - notch2_top - notch2_bot

    # 9. Cut True Stadium USB-C Port flush through -Y wall (Z = 1.20..4.80 mm -> 1.00 mm bridge below H_BASE=5.80)
    usb_x = XIAO_PLACED["usb_xy"][0]
    usb_z_ctr = FLOOR_T + 1.80
    usb_sk = Plane.XZ * SlotOverall(10.00, 3.60)
    usb_cut = extrude(usb_sk, amount=WALL_USB + 2.40).translate((usb_x, CAV_Y0 + 0.80, usb_z_ctr))
    base = base - usb_cut

    # 10. Bay 3 Dual 6x6 Tactile Switch Retention Cradles (SW1 at +7.50, SW2 at -7.50)
    cradle_w = SW_BODY_W + 2.0 * SW_CRADLE_CLR  # 6.70 mm square pocket -> snug 0.35 mm/side fit
    half_c = 0.5 * cradle_w
    rib_t = 0.80
    for sw_y in (SW1_CY, SW2_CY):
        outer_c = PosBox(
            SW_CX - half_c - rib_t, SW_CX + half_c + rib_t,
            sw_y - half_c - rib_t, sw_y + half_c + rib_t,
            FLOOR_T - 0.10, FLOOR_T + 2.50,
        )
        inner_c = PosBox(
            SW_CX - half_c, SW_CX + half_c,
            sw_y - half_c, sw_y + half_c,
            FLOOR_T + 0.01, FLOOR_T + 3.00,
        )
        # Open lead access channels along -X/+X flanks (leg span along Y = +/-2.25 mm)
        lead_clear = PosBox(
            SW_CX - half_c - rib_t - 0.30, SW_CX + half_c + rib_t + 0.30,
            sw_y - 1.60, sw_y + 1.60,
            FLOOR_T + 0.01, FLOOR_T + 3.00,
        )
        base = base + (outer_c - inner_c - lead_clear)

    return base, lid_common_mated, snaps_spec, xiao_spec


def _cut_flex_paddle(lid_solid, y0, y1, flex_x0=HINGE_X0, tip_x=PADDLE_X1):
    """
    Cut a crisp U-shaped through-slot + interior 45-deg thinned flex zone in the Lid roof
    (0.00 overhang in both mated and Z_print=0 orientations, >= 1.00 mm spring hinge).
    """
    # 3 sides of the U-slot: Top (+Y), Right (+X tip), Bottom (-Y)
    u_top = PosBox(flex_x0, tip_x + SLOT_GAP, y1, y1 + SLOT_GAP, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    u_bot = PosBox(flex_x0, tip_x + SLOT_GAP, y0 - SLOT_GAP, y0, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    u_tip = PosBox(tip_x, tip_x + SLOT_GAP, y0 - SLOT_GAP, y1 + SLOT_GAP, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    lid_solid = lid_solid - (u_top + u_bot + u_tip)

    # Interior hinge relief: shave 0.55 mm off inside of hinge zone with 45-deg chamfered lead-in
    # so roof bends smoothly while keeping exterior top face 100% flush (zero overhang when printed face-down!)
    hinge_relief = PosBox(flex_x0 - 0.20, flex_x0 + 3.60, y0, y1, LID_CEIL_Z - 0.10, LID_CEIL_Z + 0.55)
    lid_solid = lid_solid - hinge_relief
    return lid_solid


def build_lid_2btn(lid_common_mated):
    """Build 2-Button Tactile-Coded Flex-Paddle Lid (Top=Bar Grip SW1, Bottom=Concave Dimple SW2)."""
    lid = lid_common_mated
    # Center rigid divider bridge stays intact between Y=-2.10 and Y=+2.10 (4.20 mm solid bar!)
    lid = _cut_flex_paddle(lid, +2.10, +13.00)
    lid = _cut_flex_paddle(lid, -13.00, -2.10)

    # Top Paddle Tactile Code: 3 raised-relief grip grooves (self-bridging 0.40 mm deep on bed)
    for gy in (+5.30, +7.55, +9.80):
        lid = lid - PosBox(41.00, 49.60, gy - 0.50, gy + 0.50, H_TOTAL - 0.40, H_TOTAL + 0.20)

    # Bottom Paddle Tactile Code: 45-deg self-supporting conical thumb dimple (zero overhang!)
    dimple_cone = Cone(1.90, 0.50, 0.90, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
        (45.60, SW2_CY, H_TOTAL - 0.88)
    )
    # Wait: upside down at print Z=0, cone needs to taper INWARD going up in print Z (+Z_print = -Z_mated).
    # So in mated Z, wider mouth is at H_TOTAL (top exterior), narrower tip goes downward into roof -> zero overhang when flipped!
    dimple_cone = Cone(0.45, 1.95, 0.90, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
        (45.60, SW2_CY, H_TOTAL - 0.88)
    )
    lid = lid - dimple_cone

    # Internal Plunger Bosses reaching down from LID_CEIL_Z=9.50 to Z=5.65 (0.15 mm above switch tip Z=5.50)
    plunger_h = LID_CEIL_Z - 5.65  # 3.85 mm
    for py in (SW1_CY, SW2_CY):
        boss = Cylinder(radius=2.10, height=plunger_h + 0.20, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
            (SW_CX, py, 5.65)
        )
        lid = lid + boss

    lid_print = lid.rotate(Axis.Y, 180.0).translate((ENC_L, 0.0, H_TOTAL))
    return lid, lid_print


def build_lid_1btn(lid_common_mated):
    """Build interchangeable 1-Button Wide Flex-Paddle Lid (press anywhere activates switches)."""
    lid = _cut_flex_paddle(lid_common_mated, -12.00, +12.00)

    # Subtle centered 45-deg index dimple so thumb finds sweet spot through shirt
    dimple = Cone(0.55, 2.30, 0.95, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
        (45.60, 0.0, H_TOTAL - 0.92)
    )
    lid = lid - dimple

    # Dual plunger bosses over SW1 & SW2 + stiffener cross-rib on paddle underside (print upward -> 0 overhang!)
    plunger_h = LID_CEIL_Z - 5.65
    for py in (SW1_CY, SW2_CY):
        lid = lid + Cylinder(radius=2.10, height=plunger_h + 0.20, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
            (SW_CX, py, 5.65)
        )
    lid = lid + PosBox(SW_CX - 1.10, SW_CX + 1.10, SW2_CY, SW1_CY, LID_CEIL_Z - 1.60, LID_CEIL_Z + 0.20)

    lid_print = lid.rotate(Axis.Y, 180.0).translate((ENC_L, 0.0, H_TOTAL))
    return lid, lid_print


def build_antimash_bezel():
    """
    Build optional 1.80 mm raised Barbell Anti-Mash Guard Bezel framing the button paddles.
    Prints top-face down at Z=0 so the 4 shallow locating pins point straight +Z (zero overhangs!).
    """
    bx0, bx1 = 33.10, 54.10
    by0, by1 = -15.60, +15.60
    wall_b = 2.20

    frame_outer = _rounded_rect_prism(bx0, bx1, by0, by1, H_TOTAL, H_TOTAL + BEZEL_H, 2.20)
    top_edges = [
        e for e in frame_outer.edges()
        if abs(e.bounding_box().min.Z - (H_TOTAL + BEZEL_H)) < 0.05
        and abs(e.bounding_box().max.Z - (H_TOTAL + BEZEL_H)) < 0.05
    ]
    frame_outer = chamfer(top_edges, length=0.60)

    # Cut open finger/thumb windows above Top (+Y) & Bottom (-Y) paddles, leaving 2.20 mm center bar!
    cut_top_win = _rounded_rect_prism(bx0 + wall_b, bx1 - wall_b, +1.10, by1 - wall_b, H_TOTAL - 0.2, H_TOTAL + BEZEL_H + 0.3, 1.40)
    cut_bot_win = _rounded_rect_prism(bx0 + wall_b, bx1 - wall_b, by0 + wall_b, -1.10, H_TOTAL - 0.2, H_TOTAL + BEZEL_H + 0.3, 1.40)
    bezel_mated = frame_outer - cut_top_win - cut_bot_win

    # Add 4 shallow downward locating pins (0.35 mm tall) matching Lid's 0.40 mm blind dimples
    for dx in (34.40, 52.80):
        for dy in (-14.20, +14.20):
            pin = Cylinder(radius=0.72, height=0.45, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
                (dx, dy, H_TOTAL - 0.35)
            )
            bezel_mated = bezel_mated + pin

    # Invert onto Z=0 top-down so top rim sits flat at Z=0 and 0.35 mm alignment pins point +Z (0 overhangs!)
    bezel_print = bezel_mated.rotate(Axis.Y, 180.0).translate((bx0 + bx1, 0.0, H_TOTAL + BEZEL_H))
    return bezel_mated, bezel_print


def _wire_routes_3d():
    """Exact 3D silicone lead routes through Bay divider wire-pass notches (clear of all PETG walls)."""
    z_pcb_top = FLOOR_T + float(C6["pier_height_mm"]) + float(C6["thickness"])  # 6.10 mm
    z_wire = z_pcb_top + 0.85  # 6.95 mm (safely above pocket walls under LID_CEIL_Z=9.50)
    p_d1 = XIAO_PLACED["pins"]["D1"]
    p_d2 = XIAO_PLACED["pins"]["D2"]
    p_gnd = XIAO_PLACED["pins"]["GND"]
    p_bat_pos = [XIAO_CX - 3.5, XIAO_CY + 3.0]
    p_bat_neg = [XIAO_CX - 3.5, XIAO_CY + 1.0]
    return [
        {
            "net": "BAT+",
            "color": "#dc2626",
            "points": [[21.0, +11.2, z_wire], [25.0, +11.2, z_wire], [27.5, +8.5, z_wire], [p_bat_pos[0], p_bat_pos[1], z_wire]],
        },
        {
            "net": "BAT-",
            "color": "#111827",
            "points": [[21.0, +9.2, z_wire], [25.0, +9.2, z_wire], [27.5, +6.5, z_wire], [p_bat_neg[0], p_bat_neg[1], z_wire]],
        },
        {
            "net": "BTN1_D1",
            "color": "#2563eb",
            "points": [[p_d1[0], p_d1[1], z_wire], [42.8, +8.5, z_wire], [45.6, +8.5, 5.20], [SW_CX - 3.4, SW1_CY, 3.00]],
        },
        {
            "net": "BTN2_D2",
            "color": "#059669",
            "points": [[p_d2[0], p_d2[1], z_wire], [42.5, -10.5, z_wire], [45.6, -10.5, 5.20], [SW_CX - 3.4, SW2_CY, 3.00]],
        },
        {
            "net": "GND_SW",
            "color": "#475569",
            "points": [[p_gnd[0], p_gnd[1], z_wire], [42.5, -12.5, z_wire], [45.6, -12.5, 5.20], [SW_CX + 3.4, -12.5, 4.30], [SW_CX + 3.4, SW2_CY, 4.30], [SW_CX + 3.4, SW1_CY, 4.30]],
        },
    ]


def _write_spec_json(path, snaps_spec, xiao_spec, variant_label):
    xiao_spec["name"] = "Seeed_XIAO_ESP32_C6"
    xiao_spec.pop("aligned_vias", None)  # Method 2 solid bottom floor (topside direct-soldered silicone leads)

    components = [
        {"label": "502030 LiPo 250mAh",   "x": 3.80,                     "y": -15.60,                  "w": 20.00,                    "h": 30.00},
        {"label": "Seeed XIAO ESP32-C6",  "x": round(XIAO_BX0, 3),        "y": round(XIAO_BY0, 3),     "w": round(XIAO_BX1 - XIAO_BX0, 3), "h": round(XIAO_BY1 - XIAO_BY0, 3)},
        {"label": "SW1 6x6mm (Top)",      "x": round(SW_CX - 3.00, 3),    "y": round(SW1_CY - 3.00, 3), "w": 6.00,                    "h": 6.00},
        {"label": "SW2 6x6mm (Bot)",      "x": round(SW_CX - 3.00, 3),    "y": round(SW2_CY - 3.00, 3), "w": 6.00,                    "h": 6.00},
    ]

    pins = [
        {"name": "D1",  "x": XIAO_PLACED["pins"]["D1"][0],  "y": XIAO_PLACED["pins"]["D1"][1],  "net": "BTN1_D1"},
        {"name": "D2",  "x": XIAO_PLACED["pins"]["D2"][0],  "y": XIAO_PLACED["pins"]["D2"][1],  "net": "BTN2_D2"},
        {"name": "GND", "x": XIAO_PLACED["pins"]["GND"][0], "y": XIAO_PLACED["pins"]["GND"][1], "net": "GND_SW"},
        {"name": "SW1", "x": round(SW_CX, 3), "y": SW1_CY, "net": "BTN1_D1"},
        {"name": "SW2", "x": round(SW_CX, 3), "y": SW2_CY, "net": "BTN2_D2"},
    ]

    spec = {
        "project": f"Armory Chest Strap Wi-Fi Button Pod v1.0 ({variant_label})",
        "title": f"Armory Strap Button v1.0 ({variant_label}) — BRep Internals",
        "architecture": "method2",
        "uses_wire_combs": False,
        "has_usb_port": True,
        "has_electronics": True,
        "has_z0_deboss": False,
        "mated_lid_translate_z": H_TOTAL,
        "pier_slice_z": 2.40,
        "comb_slice_z": 4.90,
        "longitudinal_slice_x": round(SW_CX, 2),
        "transverse_slice_y": 0.0,
        "comb_zoom_box": [24.0, 56.5, -18.5, 18.5],
        "z_budget": {
            "required_stack_mm": 9.80,
            "max_excess_mm": 2.00,
        },
        "xy_compactness_exempt": True,
        "components": components,
        "pcb_supports": [xiao_spec],
        "lid_snaps": snaps_spec,
        "chip_pockets": [
            {"name": "502030_LiPo_Bay", "min": [4.5, -15.0, 2.0], "max": [23.0, 14.0, 5.5]},
            {"name": "XIAO_C6_Airgap",  "min": [27.0, -15.5, 5.2], "max": [42.0, 3.0, 9.0]},
        ],
        "wire_routes_3d": _wire_routes_3d(),
        "pins": pins,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(spec, fp, indent=2)
    return spec


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    base, lid_common_mated, snaps_spec, xiao_spec = _build_base_and_common_lid()
    lid_2btn_mated, lid_2btn_print = build_lid_2btn(lid_common_mated)
    lid_1btn_mated, lid_1btn_print = build_lid_1btn(lid_common_mated)
    bezel_mated, bezel_print = build_antimash_bezel()

    # Check exact Z=0 Base edge count for Gate 4
    z0_edges = [e for e in base.edges() if abs(e.bounding_box().min.Z) < 0.08 and abs(e.bounding_box().max.Z) < 0.08]
    print(f"Base Z=0 edges count = {len(z0_edges)}")

    # Verify Mated Interference across all parts before STEP export
    for lbl, a, b in [
        ("Base vs Lid-2Btn", base, lid_2btn_mated),
        ("Base vs Lid-1Btn", base, lid_1btn_mated),
        ("Lid-2Btn vs Bezel", lid_2btn_mated, bezel_mated),
    ]:
        inter = a.intersect(b)
        vol = sum(s.volume for s in inter.solids()) if inter and hasattr(inter, "solids") and len(inter.solids()) > 0 else 0.0
        print(f"Mated check [{lbl}]: {vol:.6f} mm^3")
        assert vol < 0.15, f"Mated collision in {lbl}: {vol:.4f} mm^3"

    p_base = os.path.join(MODELS_DIR, "armory_strap_button_base.step")
    p_lid2 = os.path.join(MODELS_DIR, "armory_strap_button_lid_2btn.step")
    p_lid1 = os.path.join(MODELS_DIR, "armory_strap_button_lid_1btn.step")
    p_bezel = os.path.join(MODELS_DIR, "armory_strap_button_bezel.step")

    export_step(base, p_base)
    export_step(lid_2btn_print, p_lid2)
    export_step(lid_1btn_print, p_lid1)
    export_step(bezel_print, p_bezel)

    _write_spec_json(SPEC_2BTN_JSON, snaps_spec, dict(xiao_spec), "2-Button Tactile")
    _write_spec_json(SPEC_1BTN_JSON, snaps_spec, dict(xiao_spec), "1-Button Wide")

    print("Exported 4 Z=0 support-free STEP files to models/:")
    for p in (p_base, p_lid2, p_lid1, p_bezel):
        print(f"  - {os.path.basename(p)} ({os.path.getsize(p):,} bytes)")
    print(f"XIAO placed bbox={XIAO_PLACED['bbox']} usb_xy={XIAO_PLACED['usb_xy']}")


if __name__ == "__main__":
    main()
