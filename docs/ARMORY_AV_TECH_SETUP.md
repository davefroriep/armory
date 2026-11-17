# The Armory — A/V, Compute, Network & Display Setup

Living hardware/A/V topology reference for Dave's home gym (**The Armory**) so future Armory Fit telemetry, treadmill/spin pod, and gym wallboard features integrate cleanly with existing screens and audio.

---

## 1. Core App & Network Constraints

- **Primary Training Controller:** **Armory Fit** runs strictly as a local native Android APK (`com.froriep.armoryfit`) on Dave's **Google Pixel**.
- **Gym Wi-Fi:** Strong local Wi-Fi coverage throughout the gym.
- **Workstation / Remote Access:**
  - **Dell OptiPlex** in the gym (used headless primarily via **Microsoft RDP** and **Android Antigravity Remote**, and occasionally directly while standing on the treadmill).
  - Connected to **HDMI Input 2** on the **Hisense TV**.
  - Controlled locally via a **wireless mouse & keyboard** on a flip-away counter mounted on the treadmill rails (flips out of the way when running).

---

## 2. Current A/V Routing & Audio Rig

```text
[ Fire TV Cube ] ──HDMI──▶ [ Denon AVR-3313CI (?) ] ──HDMI Out 1──▶ [ Hisense TV (HDMI 1) ]
                                    │                                       ▲
                                    ├─▶ 4× Corner Tower Speakers             │ HDMI 2
                                    ├─▶ 1× Center Channel (over treadmill)  │
                                    └─▶ HDMI Out 2: [ Optional 32" TV / Pending check ]

[ OptiPlex ] ───────────────────────────────────────────────────────────────┘
```

- **Streaming & Audio Source:** **Amazon Fire TV Cube** feeds the Denon AVR — acts as both the **treadmill video display** (through Hisense HDMI 1) and the **whole-gym music / audio-video streaming head**.
- **Receiver & Speaker Array:** **Denon AVR-3313CI** *(model number to be confirmed)* driving **4 corner tower speakers** + **1 center-channel speaker** mounted over/in front of the treadmill (typically running in a **5-channel / Multi-Ch Stereo** mode — *exact surround mode to confirm*).
- **Cardio & Lift BLE Sensors (Active Today / In-Flight):**
  - **Polar H10** chest strap (`0x180D` / `0x2A37`, supports 2 simultaneous BLE connections when dual-BLE is enabled in Polar Beat).
  - **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS).
  - **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM — pending calibration).
  - **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).

---

## 3. Secondary Wall Display / Live Wallboard Candidates *(Holding Pattern)*

Goal for a future secondary display (no code changes yet): glanceable real-time **Polar H10 HR / readiness**, **active rest timer**, and **daily workout / sets-remaining / next-machine progression** without touching the Pixel.

- **Candidate 1 — Existing 19" Monitor:** Need to check physical video inputs before deciding whether a **Fire TV Stick** (or adapter) can plug in directly.
- **Candidate 2 — Possible Spare 32" TV:** Availability/placement TBD (potentially tied to Denon **HDMI Out 2** or standalone with a Fire TV Stick).
- **Candidate 3 — ESP32 Rack/Wall Display:** Purpose-built BLE or Wi-Fi receiver mounted near the rack/wall (no spare PCs currently on hand).
- **Important Constraint:** Music/video on the **Fire TV Cube -> Denon** should stay uninterrupted while lifting or running; any wallboard should avoid hijacking the main music source unless intended.

---

## 4. Open Questions / To Verify Next

- [ ] Confirm exact **Denon AVR** model (`AVR-3313CI`?) and **5-channel stereo** setting, plus whether **HDMI Monitor Out 2** is free or already wired/mirrored vs. independent Zone 2.
- [ ] Check video inputs on the **19" monitor** (HDMI vs. VGA/DVI/DisplayPort).
- [ ] Confirm whether the **spare 32" TV** is available and where it could mount cleanly relative to the rack vs. cardio bay.
- [ ] Confirm **OptiPlex** network link (Ethernet vs. Wi-Fi) and sleep/always-on policy, plus where the **Pixel** typically sits during lifting sets (pocket vs. rack shelf/stand).
