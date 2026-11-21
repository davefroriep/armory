# Armory Fit — Engineering & Training Roadmap (`TODO`)

Tracked upcoming hardware, telemetry, and programming milestones for **Armory Fit** (`N:\Documents\Antigravity\Strength and Conditioning`).

1. **Full Home-Gym Equipment, Machine & Custom-Rigged Exercise Inventory Walkthrough with Dave**
   - Fill out [ARMORY_EQUIPMENT_SETUP.md](./ARMORY_EQUIPMENT_SETUP.md): audit and document every bar, rack attachment, cable tower rigging, standalone machine, bench/handle setup, and custom-rigged movement Dave uses in his garage/armory so `SKILL.md` and `src/lifts/library.ts` match 100% of his actual facility.
   - *(Confirmed `v0.37`: Dave does **not** own a Safety Squat Bar / SSB — scrubbed from Armory Fit skill docs.)*

2. **Smart Treadmill (`Horizon T101 Pod`)**
   - Build and calibrate the ESP32 speed/incline sensor pod (BLE NUS telemetry matching Armory Fit patterns) and wire the live Galloway run-walk HUD on `#/treadmill`.

3. **Spin Bike (`Bowflex C7 + SS2K`) Calibration**
   - Calibrate resistance steps against Dave's power pedals (`128–130 W @ 82–88 RPM` Zone 2) and wire the interactive `#/spin` ERG / Zone 2 HUD alongside MyWhoosh reconciliation.

4. **Armory Live Wall Display / Wallboard Stream & Voice Commands *(Wallboard & PowerConf S330 Room Mic Relay Shipped `v0.80` — TV Mount Pending See [ARMORY_AV_TECH_SETUP.md](./ARMORY_AV_TECH_SETUP.md))***
   - `v0.38`–`v0.61` shipped the Pixel Wallboard Server (`:8765`), **OptiPlex `:8766/wallboard` relay + Fire TV ADB auto-wake**, **superset-interleaved execution cursor**, and **Voice Command parser**.
   - `v0.80` shipped the live **OptiPlex PowerConf S330 Room Mic Relay** (`faster-whisper` `base.en` + Silero VAD + S330 speaker confirmation readback via `:8766/api/voice/*` SSE, Pixel phone mic OFF by default) + Windows `Win + H` Voice Typing readiness.
   - Pending physical check: mounting the 32" Fire TV & final ceiling placement of the Anker PowerConf S330 behind the walking desk (plus checking pickup sensitivity once mounted).
