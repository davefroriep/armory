# The Armory — A/V, Compute, Network & Display Setup

Living hardware/A/V topology reference for Dave's home gym (**The Armory**) so future Armory Fit telemetry, treadmill/spin pod, and gym wallboard features integrate cleanly with existing screens and audio.

---

## 1. Core App & Network Constraints

- **Primary Training Controller:** **Armory Fit** runs strictly as a local native Android APK (`com.froriep.armoryfit`) on Dave's **Google Pixel**.
- **Gym Wi-Fi:** Strong local Wi-Fi coverage throughout the gym.
- **Workstation / Remote Access:**
  - **Dell OptiPlex** in the gym (**never sleeps**; used headless primarily via **Microsoft RDP** and **Android Antigravity Remote**, and occasionally directly while standing on the treadmill).
  - Connected to **HDMI Input 1** on the **Hisense TV**.
  - Controlled locally via a **wireless mouse & keyboard** on a flip-away counter mounted on the treadmill rails (flips out of the way when running).
- **Phone Usage During Training:** Pixel stays close to Dave and is touched between sets to log Armory Fit reps/sets or send texts.

---

## 2. Current A/V Routing & Audio Rig

```text
[ OptiPlex (never sleeps) ] ───────────────────────────────────────────▶ [ Hisense TV (HDMI 1) ]
                                                                                  ▲
[ Fire TV Cube ] ──HDMI──▶ [ Denon AVR-3313CI (HDMI 1.4a) ] ──HDMI Out 1──────────┴─ (HDMI 2)
                                    │
                                    ├─▶ 4× Corner Tower Speakers + 1× Center Channel (over treadmill)
                                    └─▶ HDMI Out 2: [ Optional 32" TV / Pending check ]
```

- **Streaming & Audio Source:** **Amazon Fire TV Cube** feeds the Denon AVR — serves as both **treadmill video display** (through Hisense HDMI 2) and **whole-gym music / audio-video streaming head** (voice control + familiar UI for wife/kids; raw PC browser streaming is a non-starter for family usability).
- **Receiver & Speaker Array:** **Denon AVR-3313CI** (HDMI 1.4a — capped below modern 4K/60 HDR / HDCP 2.2; supports ARC on Monitor Out 1 and 2× Optical TOSLINK inputs) driving **4 corner tower speakers** + **1 center-channel speaker** mounted over/in front of the treadmill (typically **5-channel / Multi-Ch Stereo** mode — *exact surround mode to confirm*).
- **Known A/V Pain Points (Noted Context):**
  - Cube → AVR-3313CI → TV bottleneck drops modern 4K/60 HDR passthrough and introduces HDMI-CEC TV power-on flakiness.
  - Direct TV → AVR back-feed (`ARC` or `Optical`) avoids an audio extractor, though requires considering screen-off music listening vs. always-on TV audio pass-through.
  - Fire TV Cube voice/music app flakiness (track commands failing, Music app splash hangs/crashes).
- **Cardio & Lift BLE Sensors (Active Today / In-Flight):**
  - **Polar H10** chest strap (`0x180D` / `0x2A37`, supports 2 simultaneous BLE connections when dual-BLE is enabled in Polar Beat).
  - **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS).
  - **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM — pending calibration).
  - **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).

---

## 3. Secondary Wall Display / Live Wallboard Candidates *(Holding Pattern)*

Goal for a future secondary display (no code changes yet): glanceable real-time **Polar H10 HR / readiness**, **active rest timer**, and **current/next exercise** positioned for visibility from the **strength equipment** (not cardio, which already faces the Hisense). Because Dave already touches his Pixel between sets, a second giant TV may be overkill compared to an ambient rack/wall timer or lightweight dedicated screen.

- **Candidate 1 — Existing 19" Monitor:** Need to check physical video inputs before deciding whether a **Fire TV Stick** (or adapter) can plug in directly.
- **Candidate 2 — Possible Spare 32" TV:** Availability/placement TBD (potentially tied to Denon **HDMI Out 2** or standalone with a Fire TV Stick).
- **Candidate 3 — ESP32 Rack/Wall Display (e.g., LED Matrix Gym Clock / Tactical Display):** Boots in 1s, always-on clock + giant rest/HR readout without hanging another full TV.
- **Important Constraint:** Music/video on the **Fire TV Cube -> Denon** should stay uninterrupted and wife/kid-friendly while lifting or running.

---

## 4. Open Questions / To Verify Next

- [ ] Confirm total **HDMI input count** on the **Hisense TV** (and which port is labeled `ARC`) + whether music is often played on the Denon with the **Hisense TV powered OFF**.
- [ ] Confirm exact Denon **5-channel stereo** mode and whether **HDMI Monitor Out 2** is free or wired.
- [ ] Check video inputs on the **19" monitor** (HDMI vs. VGA/DVI/DisplayPort).
- [ ] Confirm whether the **spare 32" TV** is available and whether a **strength-area screen** feels worth hanging vs. a compact **ESP32 rack display / LED gym timer**.
