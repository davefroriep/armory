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

- **Display / TV:** **Hisense 43" E6 Cinema Series Hi-QLED 4K UHD Smart Fire TV (`43E6QF`, 2025 Model)** *(Amazon Order `#113-5238183-0153026`, delivered Feb 10, 2026)*.
  - **TV Ports (`43E6QF`):** **3× HDMI 2.0** (with **ARC/eARC on HDMI 3**), **1× Optical Digital Audio Out (TOSLINK)**, Dolby Vision / HDR10+ Adaptive.
- **Streaming & Audio Source:** **Amazon Fire TV Cube** feeds the Denon AVR — serves as both **treadmill video display** (through Hisense HDMI 2) and **whole-gym music / audio-video streaming head** (voice control + familiar UI for wife/kids; raw PC browser streaming is a non-starter for family usability). *(Note: having both an external Fire TV Cube and a Fire TV-built-in `43E6QF` can contribute to Alexa/CEC quirks.)*
- **Receiver & Speaker Array:** **Denon AVR-3313CI** (HDMI 1.4a: max 4K@24/30Hz passthrough, no HDMI 2.0 4K/60 HDR or HDCP 2.2; supports **ARC** on HDMI Monitor Out 1, 2× Optical TOSLINK inputs, and 3 HDMI outputs: 2× main-zone + 1× Zone 2) driving **4 corner tower speakers** + **1 center-channel speaker** mounted over/in front of the treadmill (typically **5-channel / Multi-Ch Stereo** mode — *exact surround mode to confirm*).
- **Known A/V Pain Points & Architecture Notes (Zero Extractor Wanted):**
  - **CRITICAL CONSTRAINT — Why Cube currently sits BEFORE the Denon (`Cube → Denon → TV HDMI 2`):** When Dave switches the **Hisense `43E6QF`** to **HDMI 1 (OptiPlex)** to type or check work on the treadmill rails, the Denon AVR stays locked on the **Fire TV Cube** input so **music keeps playing uninterrupted** over the 4 towers + center channel while the TV displays Windows. Back-feeding audio from the TV (`TV → Optical/ARC → Denon`) would **kill Fire TV Cube music** every time the Hisense switches from HDMI 2 to HDMI 1! *(Any new 4K or A/V routing solution must preserve Fire TV audio while viewing the OptiPlex.)*
  - **4K HDR + CEC power bottleneck:** Routing Fire TV Cube → Denon AVR-3313CI → Hisense `43E6QF` drops 4K/60 HDR / Dolby Vision and causes flaky HDMI-CEC TV turn-on. (Fire TV Cube built-in **IR blaster** for Hisense + Denon can bypass CEC power/volume flakiness.)
  - **Fire TV Cube streaming flakiness:** Voice track-skip errors ("nothing playing"), launching wrong song/Amazon Music, splash-screen hangs, and Music app crashes after ~3 songs.
  - **TV → Denon back-feed reference (`43E6QF` ports):** `43E6QF` has **3× HDMI 2.0 (`HDMI 3 = eARC/ARC`)** + **1× Optical Out**, and TV is always ON during music — so back-feed works cleanly *only* if Fire TV Cube and OptiPlex don't compete for the same single TV audio bus (e.g., if OptiPlex moves to a secondary display or audio is decoupled).
- **Cardio & Lift BLE Sensors (Active Today / In-Flight):**
  - **Polar H10** chest strap (`0x180D` / `0x2A37`, supports 2 simultaneous BLE connections when dual-BLE is enabled in Polar Beat).
  - **Assault AirBike Elite** (`AssaultBike` XIAO ESP32-C6, 5 Hz NUS + passive FTMS).
  - **Bowflex C7 + `SS2K_Tuner`** (ESP32-WROOM — pending calibration).
  - **Horizon T101 Treadmill + `Horizon T101 Pod`** (XIAO ESP32-C6 — planned).

---

## 3. Secondary Wall Display / Live Wallboard Candidates *(Holding Pattern)*

Goal for the strength-area display (`v0.38` framework now built directly into the Armory Fit APK): glanceable real-time **Polar H10 HR / readiness**, **active rest timer**, and **superset-interleaved current/next exercise** (`1A -> 1B -> 1A -> 1B`) served over local Wi-Fi at `http://<pixel-ip>:8765` (openable in any Smart TV / monitor browser) paired with **hands-free voice logging** (say *"Armory — check"*, *"Armory — 225 for 8"*, *"Armory — next exercise"*, *"Armory — 2B"*, *"Armory — undo"* strictly while Armory Fit is open on screen). Plus `POST /api/voice` is ready on LAN if an OptiPlex hardwired mic is ever needed over loud 5-ch gym music. *(Constraint: zero spare PCs currently on hand; OptiPlex never sleeps.)*

- **Candidate 1 — Existing 19" Monitor:** Need to check physical video inputs before deciding whether a **Fire TV Stick** (or adapter) can plug in directly.
- **Candidate 2 — Possible Spare 32" TV:** Availability/placement TBD (potentially tied to Denon **HDMI Out 2** or standalone with a Fire TV Stick).
- **Candidate 3 — ESP32 Rack/Wall Display (e.g., LED Matrix Gym Clock / Tactical Display):** Boots in 1s, direct BLE/Wi-Fi, doubles as always-on digital gym clock + giant rest/HR readiness readout without hanging another full TV.
- **Candidate 4 — OptiPlex Always-On Relay / Existing Hisense HDMI 1:** Because OptiPlex never sleeps and sits on strong gym Wi-Fi, it can optionally act as an always-on local relay or fullscreen browser host on Hisense HDMI 1 with zero extra hardware.
- **Important Constraint:** Music/video on the **Fire TV Cube -> Denon** should stay uninterrupted and wife/kid-friendly while lifting or running; voice capture must tolerate 4-tower + center speaker room audio (e.g., Pixel/rack mic wake word, OptiPlex conference mic + Whisper, or 1-tap BLE rack button PTT).

---

## 4. Open Questions / To Verify Next

- [x] *Hisense TV Model & Ports:* Confirmed **Hisense 43" E6 Cinema Series 4K Fire TV (`43E6QF`, 2025)** — **3× HDMI 2.0 (`HDMI 3 = eARC/ARC`)** + **1× Optical Digital Audio Out (TOSLINK)**.
- [x] *Music playback TV state:* Confirmed **Hisense `43E6QF` is always ON** when streaming music.
- [ ] Confirm exact Denon **5-channel stereo** mode and whether **HDMI Monitor Out 2** is free or wired.
- [ ] Check video inputs on the **19" monitor** (HDMI vs. VGA/DVI/DisplayPort) and approximate rack/bench viewing distance.
- [ ] Confirm whether the **spare 32" TV** is available and whether a **strength-area screen** feels worth hanging vs. a compact **ESP32 rack display / LED gym timer**.
