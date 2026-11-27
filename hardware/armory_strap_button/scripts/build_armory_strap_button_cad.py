"""
build_armory_strap_button_cad.py
Master Parametric CAD Generator for Armory Chest Strap Wi-Fi Button Pod (Rev v2.0 — Version C Single-Button).

Architecture (Compact Side-by-Side 502030 250mAh + Single Button + Zero-Electrode-Contact Strap Clip):
  - Compact enclosure without side wings: 47.00 mm (L) x 37.60 mm (W) x 10.50 mm (H)
    (-24.6 mm shorter than v1.1; -18.0 mm shorter than Polar H10!).
  - Method 2 100% solid 1.20 mm bottom floor (Z=0..1.20) + 2 compact internal bays:
      1. Bay 1 (-X): 502030 (250 mAh) LiPo pouch + PCM cavity (20.60 x 32.60 mm) behind 1.20 mm armor wall
      2. Bay 2 (+X): Lower Zone = Floated Seeed XIAO ESP32-C6 (belly-down, 3.30 mm piers, rotation=-90 -> USB-C on -Y)
                     Upper Zone = Single 6x6x4.3 mm tactile switch (SW1) centered at (34.31, +10.50)
  - Separate 1-Piece Zero-Electrode-Contact Strap Clip (armory_strap_button_strap_clip.step):
      * 1.15 mm top & bottom slide-in pockets + 2.00 mm inward-trapping wings grip ONLY outer strap edges
      * Leaves entire center 26.80 mm Polar electrode strip 100% open & flat
  - Exported Z=0 Support-Free Component STEPs in models/:
      1. armory_strap_button_base.step
      2. armory_strap_button_lid.step         (Single wide PETG flex-paddle, ARMORY v2.0)
      3. armory_strap_button_bezel.step       (Optional 1.60 mm anti-mash barbell guard frame)
      4. armory_strap_button_strap_clip.step  (Vertically printed C-rail strap clip)
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
    Plane,
    Polygon,
    Polyline,
    Rectangle,
    SlotOverall,
    Text,
    chamfer,
    export_step,
    extrude,
    fillet,
    make_face,
)

WORKSPACE = r"N:\Documents\Antigravity\Strength and Conditioning"
PROJECT_DIR = os.path.join(WORKSPACE, "hardware", "armory_strap_button")
MODELS_DIR = os.path.join(PROJECT_DIR, "models")
SCRATCH_DIR = (
    r"C:\Users\davef\.gemini\antigravity\brain\17b9157d-6878-4a18-8c64-aa205aebb08d\scratch"
)
SPEC_JSON = os.path.join(SCRATCH_DIR, "armory_strap_button_spec_v2.json")

# =============================================================================
# 1. VERIFIED MECHANICAL & COMPONENT CONSTANTS (mm)
# =============================================================================
ENC_L = 47.00                 # X in [0.00, 47.00]
ENC_W = 37.60                 # Y in [-18.80, +18.80]
Y_MIN = -0.5 * ENC_W          # -18.80 mm
Y_MAX = +0.5 * ENC_W          # +18.80 mm
CORNER_R = 3.00

WALL_X_L = 3.40               # Left snap wall (>= 1.25 mm backing behind groove)
WALL_X_R = 3.58               # Right snap wall (>= 1.45 mm backing behind groove)
WALL_POS_Y = 3.40             # Top registration tongue wall
WALL_USB = 1.60               # Flush XIAO USB-C wall on -Y (DRC 14)

CAV_X0 = WALL_X_L             # 3.40 mm
CAV_X1 = ENC_L - WALL_X_R     # 43.42 mm
CAV_Y0 = Y_MIN + WALL_USB     # -17.20 mm
CAV_Y1 = Y_MAX - WALL_POS_Y   # +15.40 mm (32.60 mm height for 30 mm 502030 LiPo)

# Bottom-Up Z-Stackup (DRC 13)
FLOOR_T = 1.20                # Solid Method 2 continuous floor (Z = 0.00..1.20)
H_BASE = 5.60                 # Split line (leaves 0.80 mm bridge above Z=1.20..4.80 USB stadium!)
TONGUE_H = 3.20               # DRC 30: tall structural shoulder tongue (Z = 5.60..8.80)
TONGUE_T = 1.45               # DRC 30: >= 1.40 mm tongue wall
RADIAL_CLR = 0.10             # Tight radial slip fit
BARB_PROUD = 0.55             # DRC 10: 0.45 mm net snap interference
LID_CEIL_Z = H_BASE + TONGUE_H + 0.15   # 8.95 mm interior Lid ceiling
ROOF_T = 1.55                 # Leaves 1.20 mm solid backing behind 0.35 mm deboss (DRC 8)
H_TOTAL = LID_CEIL_Z + ROOF_T # 10.50 mm total pod thickness

# Bay 1 (-X): 502030 (250 mAh) LiPo Pouch
DIV1_X0, DIV1_X1 = 24.00, 25.20  # 1.20 mm internal armor divider

# Bay 2 (+X): Lower = Floated Seeed XIAO ESP32-C6 | Upper = Single 6x6x4.3 mm Switch (SW1)
C6 = component_lib.load("xiao_esp32_c6")
XIAO_CLR = float(C6["pocket_clearance_xy_mm"])   # 0.22 mm
XIAO_L = float(C6["length"])                     # 20.955 mm (along Y after -90 rot)
XIAO_W = float(C6["width"])                      # 17.78 mm (along X after -90 rot)
XIAO_CX = DIV1_X1 + XIAO_CLR + 0.5 * XIAO_W      # 34.31 mm
XIAO_CY = CAV_Y0 + XIAO_CLR + 0.5 * XIAO_L       # -6.5025 mm
XIAO_PLACED = component_lib.place(
    C6, cx=XIAO_CX, cy=XIAO_CY, belly_down=True, rotation_deg=-90.0, well_floor_z=FLOOR_T
)
XIAO_BX0, XIAO_BY0, XIAO_BX1, XIAO_BY1 = XIAO_PLACED["bbox"]
XIAO_POCK_Y1 = XIAO_BY1 + XIAO_CLR               # +4.195 mm

# Upper Bay 2: Single 6x6x4.3 mm Switch (SW1) centered directly above XIAO
SW_CX = XIAO_CX               # 34.31 mm
SW_CY = +10.50                # 3.52 mm clean gap above XIAO +Y edge
SW_BODY_W = 6.00
SW_CRADLE_CLR = 0.35          # 6.35 x 6.35 mm snug cradle
SW_PLUNGER_Z = FLOOR_T + 4.30 # 5.50 mm stem tip

# Single Flex-Paddle & Anti-Mash Bezel Constants
HINGE_X0 = 22.00              # Left hinge root cleanly over divider zone
PADDLE_X1 = 41.50             # Paddle tip X (+7.2 mm past SW_CX)
PADDLE_Y0, PADDLE_Y1 = +5.50, +14.50
SLOT_GAP = 0.90
BEZEL_H = 1.60                # Optional raised barbell guard

# Zero-Electrode-Contact Top/Bottom Strap Clip (C-rail backplate)
CLIP_X0, CLIP_X1 = 2.50, 44.50       # 42.00 mm wide spine along strap
CLIP_OUTER_W = 36.80                 # Y in [-18.40, +18.40] (slight 0.40 mm setback from pod edge)
CLIP_SPINE_T = 0.80                  # Glue plate Z in [-0.80, 0.00]
CLIP_POCKET_H = 1.15                 # ~1 mm pocket height (Z in [-1.95, -0.80])
CLIP_SIDE_WALL = 3.00                # Y outer pocket wall -> clear Y slot = 30.80 mm (30 mm Polar band)
CLIP_WING_LEN = 2.00                 # 2.00 mm inward-pointing strap edge trapping wings
CLIP_WING_T = 1.20                   # Z in [-3.15, -1.95]


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
    with >= 1.15 mm solid Lid retaining lip below the barb groove and 0.45 mm net interference.
    """
    z_ctr = H_BASE + 2.05     # 7.65 mm -> bottom of 45-deg ramp at 6.85 -> groove bottom 6.75 -> 1.15 mm Lid lip
    crest_h = 0.50
    z_c0 = z_ctr - 0.5 * crest_h   # 7.40 mm
    z_c1 = z_ctr + 0.5 * crest_h   # 7.90 mm
    z_bot_ramp = z_c0 - BARB_PROUD # 6.85 mm
    z_top_ramp = min(H_BASE + TONGUE_H - 0.05, z_c1 + 1.30 * BARB_PROUD)  # 8.615 mm
    slot_w = 0.80
    tab_len = 10.00
    slot_z0 = H_BASE + 0.35        # 5.95 mm (safely outside H_BASE +/- 0.30 split band)
    slot_z1 = H_BASE + TONGUE_H + 0.50
    groove_rad = BARB_PROUD + 0.08
    groove_z_pad = 0.10

    snaps_spec = []
    walls = [
        {"side": -1, "wall_pos": CAV_X0 - TONGUE_T, "centers": [0.0]},
        {"side": +1, "wall_pos": CAV_X1 + TONGUE_T, "centers": [0.0]},
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


def build_base_lid_and_bezel():
    """Construct compact 47 x 37.6 x 10.5 mm Base, Single-Button Lid, and Anti-Mash Bezel."""
    # 1. Monolithic outer hull (0..H_TOTAL) + 0.80 mm DRC 29 anti-elephant-foot chamfers
    hull = _rounded_rect_prism(0.0, ENC_L, Y_MIN, Y_MAX, 0.0, H_TOTAL, CORNER_R)
    bot_edges = [e for e in hull.edges() if abs(e.bounding_box().min.Z) < 0.05 and abs(e.bounding_box().max.Z) < 0.05]
    top_edges = [e for e in hull.edges() if abs(e.bounding_box().min.Z - H_TOTAL) < 0.05 and abs(e.bounding_box().max.Z - H_TOTAL) < 0.05]
    hull = chamfer(bot_edges + top_edges, length=0.80)

    # 2. Hollow interior cavity above 1.20 mm solid continuous floor (Z = FLOOR_T..LID_CEIL_Z)
    hull = hull - PosBox(CAV_X0, CAV_X1, CAV_Y0, CAV_Y1, FLOOR_T, LID_CEIL_Z)

    # 3. Deboss ARMORY / v2.0 on flat lower roof (Y < +2, X in [3.5, 43.5]) -> 1.20 mm solid backing
    t_brand = Text("ARMORY", font_size=5.8, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER))
    t_rev = Text("v2.0", font_size=4.8, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER))
    deb1 = extrude(Plane.XY.offset(H_TOTAL - 0.35) * t_brand, amount=0.55).translate((23.5, -5.2, 0.0))
    deb2 = extrude(Plane.XY.offset(H_TOTAL - 0.35) * t_rev, amount=0.55).translate((23.5, -12.6, 0.0))

    # 4 shallow locating dimples (0.35 mm deep, leaves 1.20 mm backing) around Bezel footprint
    bx0_b, bx1_b = 19.40, 44.20
    by0_b, by1_b = +2.50, +17.10
    dimples = None
    for dx in (bx0_b + 1.15, bx1_b - 1.15):
        for dy in (by0_b + 1.10, by1_b - 1.10):
            d_cut = Cylinder(radius=0.86, height=0.55, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate(
                (dx, dy, H_TOTAL - 0.35)
            )
            dimples = d_cut if dimples is None else (dimples + d_cut)
    hull = hull - deb1 - deb2 - dimples

    # 4. Split monolithic solid at Z = H_BASE = 5.60
    cut_bot = PosBox(-10.0, ENC_L + 10.0, -30.0, 30.0, -5.0, H_BASE)
    cut_top = PosBox(-10.0, ENC_L + 10.0, -30.0, 30.0, H_BASE, H_TOTAL + 5.0)
    base_raw = hull - cut_top
    lid_raw = hull - cut_bot

    # 5. Stepped shoulder tongue on -X, +X, +Y ONLY (omitted along -Y flush USB wall per DRC 14)
    tongue_outer = _rounded_rect_prism(
        CAV_X0 - TONGUE_T, CAV_X1 + TONGUE_T,
        CAV_Y0, CAV_Y1 + TONGUE_T,
        H_BASE - 0.10, H_BASE + TONGUE_H, 1.40,
    )
    tongue_inner = PosBox(CAV_X0, CAV_X1, CAV_Y0 - 0.50, CAV_Y1, H_BASE - 0.20, H_BASE + TONGUE_H + 0.20)
    tongue = (tongue_outer - tongue_inner) - PosBox(-10.0, ENC_L + 10.0, Y_MIN - 5.0, CAV_Y0 + 0.10, H_BASE - 0.5, H_BASE + TONGUE_H + 0.5)

    clr_outer = _rounded_rect_prism(
        CAV_X0 - TONGUE_T - RADIAL_CLR, CAV_X1 + TONGUE_T + RADIAL_CLR,
        CAV_Y0 - 0.10, CAV_Y1 + TONGUE_T + RADIAL_CLR,
        H_BASE - 0.10, H_BASE + TONGUE_H + 0.15, 1.40,
    ) - PosBox(-10.0, ENC_L + 10.0, Y_MIN - 5.0, CAV_Y0, H_BASE - 0.5, H_BASE + TONGUE_H + 0.5)

    tongue, clr_outer, snaps_spec = _build_snap_tongue_with_custom_barb(tongue, clr_outer)
    base = base_raw + tongue
    lid_mated = lid_raw - clr_outer

    # 6. Bay 1 & Bay 2 Internal Partition (Z=5.15 clear of 5.60 split band)
    div_top_z = 5.15
    div1 = PosBox(DIV1_X0, DIV1_X1, CAV_Y0 - 0.1, CAV_Y1 + 0.1, FLOOR_T - 0.10, div_top_z)
    div1 = div1 - PosBox(DIV1_X0 - 0.2, DIV1_X1 + 0.2, +7.50, +13.00, 3.30, div_top_z + 0.5)
    base = base + div1

    # 7. Floated XIAO ESP32-C6 Pocket + Exact Piers + Anti-Pushout Backstops (DRC 24 & DRC 37)
    xiao_wall_top_z = 6.50
    xiao_pocket, _, xiao_spec = pcb_helpers.build_floated_module_pocket(
        XIAO_PLACED,
        well_floor_z=FLOOR_T,
        wall_top_z=xiao_wall_top_z,
        wall_t=1.00,
        open_usb_side="-Y",
        usb_opening_w=10.20,
    )
    clip_box = PosBox(DIV1_X0, CAV_X1, CAV_Y0 + 0.02, XIAO_POCK_Y1 + 1.20, FLOOR_T - 0.2, xiao_wall_top_z + 0.2)
    base = base + xiao_pocket.intersect(clip_box)

    # Anti-pushout corner backstops behind +Y XIAO edge (middle ANT1 stays 100% open RF air!)
    for bx_post0, bx_post1 in ((XIAO_BX0, XIAO_BX0 + 2.20), (XIAO_BX1 - 2.20, XIAO_BX1)):
        base = base + PosBox(bx_post0, bx_post1, XIAO_POCK_Y1, XIAO_POCK_Y1 + 1.20, FLOOR_T - 0.1, 6.50)

    # 8. Cut True Stadium USB-C Port flush through -Y wall (Z = 1.20..4.80 mm)
    usb_x = XIAO_PLACED["usb_xy"][0]
    usb_sk = Plane.XZ * SlotOverall(10.00, 3.60)
    usb_cut = extrude(usb_sk, amount=WALL_USB + 2.40).translate((usb_x, CAV_Y0 + 0.80, FLOOR_T + 1.80))
    base = base - usb_cut

    # 9. Single 6x6x4.3 mm Tactile Switch Cradle (SW1) centered at (34.31, +10.50) above XIAO
    cradle_w = SW_BODY_W + 2.0 * SW_CRADLE_CLR
    half_c = 0.5 * cradle_w
    rib_t = 0.80
    outer_c = PosBox(SW_CX - half_c - rib_t, SW_CX + half_c + rib_t, SW_CY - half_c - rib_t, SW_CY + half_c + rib_t, FLOOR_T - 0.10, FLOOR_T + 2.50)
    inner_c = PosBox(SW_CX - half_c, SW_CX + half_c, SW_CY - half_c, SW_CY + half_c, FLOOR_T + 0.01, FLOOR_T + 3.00)
    lead_clear = PosBox(SW_CX - half_c - rib_t - 0.30, SW_CX + half_c + rib_t + 0.30, SW_CY - 1.60, SW_CY + 1.60, FLOOR_T + 0.01, FLOOR_T + 3.00)
    base = base + (outer_c - inner_c - lead_clear)

    # 10. Single Wide PETG Cantilever Flex-Paddle in Lid + Plunger Boss over SW1
    u_top = PosBox(HINGE_X0, PADDLE_X1 + SLOT_GAP, PADDLE_Y1, PADDLE_Y1 + SLOT_GAP, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    u_bot = PosBox(HINGE_X0, PADDLE_X1 + SLOT_GAP, PADDLE_Y0 - SLOT_GAP, PADDLE_Y0, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    u_tip = PosBox(PADDLE_X1, PADDLE_X1 + SLOT_GAP, PADDLE_Y0 - SLOT_GAP, PADDLE_Y1 + SLOT_GAP, LID_CEIL_Z - 0.2, H_TOTAL + 0.3)
    hinge_relief = PosBox(HINGE_X0 - 0.20, HINGE_X0 + 3.40, PADDLE_Y0, PADDLE_Y1, LID_CEIL_Z - 0.10, LID_CEIL_Z + 0.55)
    dimple = Cone(0.50, 2.15, 0.85, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate((SW_CX, SW_CY, H_TOTAL - 0.83))
    lid_mated = lid_mated - u_top - u_bot - u_tip - hinge_relief - dimple

    plunger_h = LID_CEIL_Z - 5.65  # 3.30 mm boss -> 0.15 mm pre-travel above switch tip Z=5.50
    boss = Cylinder(radius=2.10, height=plunger_h + 0.20, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate((SW_CX, SW_CY, 5.65))
    lid_mated = lid_mated + boss
    lid_print = lid_mated.rotate(Axis.Y, 180.0).translate((ENC_L, 0.0, H_TOTAL))

    # 11. Optional Anti-Mash Barbell Guard Bezel (prints top-down at Z=0 -> zero overhangs!)
    frame = _rounded_rect_prism(bx0_b, bx1_b, by0_b, by1_b, H_TOTAL, H_TOTAL + BEZEL_H, 2.00)
    b_top_e = [e for e in frame.edges() if abs(e.bounding_box().min.Z - (H_TOTAL + BEZEL_H)) < 0.05 and abs(e.bounding_box().max.Z - (H_TOTAL + BEZEL_H)) < 0.05]
    frame = chamfer(b_top_e, length=0.50)
    win = _rounded_rect_prism(bx0_b + 2.00, bx1_b - 2.00, by0_b + 2.00, by1_b - 2.00, H_TOTAL - 0.2, H_TOTAL + BEZEL_H + 0.3, 1.30)
    bezel_mated = frame - win
    for dx in (bx0_b + 1.15, bx1_b - 1.15):
        for dy in (by0_b + 1.10, by1_b - 1.10):
            pin = Cylinder(radius=0.70, height=0.40, align=(Align.CENTER, Align.CENTER, Align.MIN)).translate((dx, dy, H_TOTAL - 0.30))
            bezel_mated = bezel_mated + pin
    bezel_print = bezel_mated.rotate(Axis.Y, 180.0).translate((bx0_b + bx1_b, 0.0, H_TOTAL + BEZEL_H))

    return base, lid_mated, lid_print, bezel_mated, bezel_print, snaps_spec, xiao_spec


def build_strap_clip():
    """
    Build 1-Piece Zero-Electrode-Contact Top/Bottom Polar Strap Clip:
      - 1.15 mm (~1 mm) top & bottom slide-in pockets + 2.00 mm inward trapping wings
      - Leaves center 26.80 mm (Y in [-13.40, +13.40]) 100% OPEN so PETG never bends/pinches electrodes!
      - Printed standing upright on its -X face (Z_print = 0..42.00 mm): ZERO overhangs, continuous
        unbroken perimeter loops wrapping around both 2 mm trapping wings!
    """
    half_w = 0.5 * CLIP_OUTER_W    # 18.40 mm
    y_slot = half_w - CLIP_SIDE_WALL  # 15.40 mm -> 30.80 mm strap pocket
    y_wing = y_slot - CLIP_WING_LEN   # 13.40 mm -> 26.80 mm open electrode gap!
    z0 = 0.0
    z1 = -CLIP_SPINE_T                # -0.80
    z2 = z1 - CLIP_POCKET_H           # -1.95 (1.15 mm strap edge pocket!)
    z3 = z2 - CLIP_WING_T             # -3.15 (1.20 mm wing thickness)

    # C-rail cross-section in (Y, Z_mated) extruded cleanly from X = CLIP_X0..CLIP_X1
    yz_pts = [
        (-half_w, z0),
        (+half_w, z0),
        (+half_w, z3 + 0.35),
        (+half_w - 0.35, z3),
        (+y_wing + 0.35, z3),
        (+y_wing, z3 + 0.35),
        (+y_wing, z2 + 0.35),
        (+y_wing + 0.35, z2),
        (+y_slot, z2),
        (+y_slot, z1),
        (-y_slot, z1),
        (-y_slot, z2),
        (-y_wing - 0.35, z2),
        (-y_wing, z2 + 0.35),
        (-y_wing, z3 + 0.35),
        (-y_wing - 0.35, z3),
        (-half_w + 0.35, z3),
        (-half_w, z3 + 0.35),
    ]
    sk_face = make_face(Plane.YZ * Polyline(*yz_pts, close=True))
    clip_mated = extrude(sk_face, amount=CLIP_X1 - CLIP_X0, dir=(1, 0, 0)).translate((CLIP_X0, 0.0, 0.0))

    # Rotate onto -X end at Z_print = 0 for 100% support-free vertical layer loops!
    clip_print = clip_mated.rotate(Axis.Y, -90.0).translate((0.0, 0.0, -CLIP_X0))
    return clip_mated, clip_print


def _wire_routes_3d():
    """Exact 4-wire 30 AWG silicone routes (zero plastic collision per Check 8d)."""
    z_pcb_top = FLOOR_T + float(C6["pier_height_mm"]) + float(C6["thickness"])  # 6.10 mm
    z_wire = z_pcb_top + 0.80  # 6.90 mm
    p_d1 = XIAO_PLACED["pins"]["D1"]
    p_gnd = XIAO_PLACED["pins"]["GND"]
    p_bat_pos = [XIAO_CX - 3.5, XIAO_CY + 3.0]
    p_bat_neg = [XIAO_CX - 3.5, XIAO_CY + 1.0]
    return [
        {
            "net": "BAT+",
            "color": "#dc2626",
            "points": [[21.0, +11.2, z_wire], [24.6, +11.2, z_wire], [27.0, +6.5, z_wire], [p_bat_pos[0], p_bat_pos[1], z_wire]],
        },
        {
            "net": "BAT-",
            "color": "#111827",
            "points": [[21.0, +9.2, z_wire], [24.6, +9.2, z_wire], [27.0, +4.5, z_wire], [p_bat_neg[0], p_bat_neg[1], z_wire]],
        },
        {
            "net": "BTN1_D1",
            "color": "#2563eb",
            "points": [[p_d1[0], p_d1[1], z_wire], [29.8, SW_CY, z_wire], [SW_CX - 3.4, SW_CY, 3.00]],
        },
        {
            "net": "GND_SW",
            "color": "#475569",
            "points": [[p_gnd[0], p_gnd[1], z_wire], [38.8, SW_CY, z_wire], [SW_CX + 3.4, SW_CY, 3.00]],
        },
    ]


def _write_spec_json(path, snaps_spec, xiao_spec):
    xiao_spec["name"] = "Seeed_XIAO_ESP32_C6"
    xiao_spec.pop("aligned_vias", None)

    components = [
        {"label": "502030 LiPo 250mAh",  "x": 3.80,                  "y": -15.60,              "w": 20.00, "h": 30.00},
        {"label": "Seeed XIAO ESP32-C6", "x": round(XIAO_BX0, 3),     "y": round(XIAO_BY0, 3), "w": round(XIAO_BX1 - XIAO_BX0, 3), "h": round(XIAO_BY1 - XIAO_BY0, 3)},
        {"label": "SW1 6x6mm Single",    "x": round(SW_CX - 3.0, 3),  "y": round(SW_CY - 3.0, 3), "w": 6.00, "h": 6.00},
    ]
    pins = [
        {"name": "D1",  "x": XIAO_PLACED["pins"]["D1"][0],  "y": XIAO_PLACED["pins"]["D1"][1],  "net": "BTN1_D1"},
        {"name": "GND", "x": XIAO_PLACED["pins"]["GND"][0], "y": XIAO_PLACED["pins"]["GND"][1], "net": "GND_SW"},
        {"name": "SW1", "x": round(SW_CX, 3),               "y": round(SW_CY, 3),               "net": "BTN1_D1"},
    ]
    spec = {
        "project": "Armory Chest Strap Wi-Fi Button Pod v2.0 (Version C — Single Button)",
        "title": "Armory Strap Button v2.0 (47.0 x 37.6 x 10.5 mm) — BRep Internals",
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
        "comb_zoom_box": [22.0, 46.5, -18.5, 18.5],
        "z_budget": {
            "required_stack_mm": 9.30,
            "max_excess_mm": 1.60,
        },
        "xy_compactness_exempt": True,
        "components": components,
        "pcb_supports": [xiao_spec],
        "lid_snaps": snaps_spec,
        "chip_pockets": [
            {"name": "502030_LiPo_Bay", "min": [4.5, -15.0, 2.0], "max": [23.0, 14.0, 5.2]},
            {"name": "XIAO_C6_Airgap",  "min": [27.0, -15.5, 5.2], "max": [41.5, 3.0, 8.6]},
        ],
        "wire_routes_3d": _wire_routes_3d(),
        "pins": pins,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(spec, fp, indent=2)


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    base, lid_mated, lid_print, bezel_mated, bezel_print, snaps_spec, xiao_spec = build_base_lid_and_bezel()
    clip_mated, clip_print = build_strap_clip()

    z0_edges = [e for e in base.edges() if abs(e.bounding_box().min.Z) < 0.08 and abs(e.bounding_box().max.Z) < 0.08]
    print(f"Base Z=0 edges count = {len(z0_edges)}")

    for lbl, a, b in [
        ("Base vs Lid", base, lid_mated),
        ("Lid vs Bezel", lid_mated, bezel_mated),
        ("Base vs Strap Clip", base, clip_mated),
    ]:
        inter = a.intersect(b)
        vol = sum(s.volume for s in inter.solids()) if inter and hasattr(inter, "solids") and len(inter.solids()) > 0 else 0.0
        print(f"Mated check [{lbl}]: {vol:.6f} mm^3")
        assert vol < 0.15, f"Mated collision in {lbl}: {vol:.4f} mm^3"

    p_base = os.path.join(MODELS_DIR, "armory_strap_button_base.step")
    p_lid = os.path.join(MODELS_DIR, "armory_strap_button_lid.step")
    p_bezel = os.path.join(MODELS_DIR, "armory_strap_button_bezel.step")
    p_clip = os.path.join(MODELS_DIR, "armory_strap_button_strap_clip.step")

    export_step(base, p_base)
    export_step(lid_print, p_lid)
    export_step(bezel_print, p_bezel)
    export_step(clip_print, p_clip)

    _write_spec_json(SPEC_JSON, snaps_spec, dict(xiao_spec))

    print("Exported 4 Z=0 support-free v2.0 STEP files to models/:")
    for p in (p_base, p_lid, p_bezel, p_clip):
        print(f"  - {os.path.basename(p)} ({os.path.getsize(p):,} bytes)")
    print(f"XIAO placed bbox={XIAO_PLACED['bbox']} usb_xy={XIAO_PLACED['usb_xy']} SW1=({SW_CX}, {SW_CY})")


if __name__ == "__main__":
    main()
