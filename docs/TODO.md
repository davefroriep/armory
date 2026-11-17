# Armory Fit — Engineering & Training Roadmap (`TODO`)

Tracked upcoming hardware, telemetry, and programming milestones for **Armory Fit** (`N:\Documents\Antigravity\Strength and Conditioning`).

1. **Full Home-Gym Equipment, Machine & Custom-Rigged Exercise Inventory Walkthrough with Dave**
   - Fill out [ARMORY_EQUIPMENT_SETUP.md](./ARMORY_EQUIPMENT_SETUP.md): audit and document every bar, rack attachment, cable tower rigging, standalone machine, bench/handle setup, and custom-rigged movement Dave uses in his garage/armory so `SKILL.md` and `src/lifts/library.ts` match 100% of his actual facility.
   - *(Confirmed `v0.37`: Dave does **not** own a Safety Squat Bar / SSB — scrubbed from Armory Fit skill docs.)*

2. **Smart Treadmill (`Horizon T101 Pod`)**
   - Build and calibrate the ESP32 speed/incline sensor pod (BLE NUS telemetry matching Armory Fit patterns) and wire the live Galloway run-walk HUD on `#/treadmill`.

3. **Spin Bike (`Bowflex C7 + SS2K`) Calibration**
   - Calibrate resistance steps against Dave's power pedals (`128–130 W @ 82–88 RPM` Zone 2) and wire the interactive `#/spin` ERG / Zone 2 HUD alongside MyWhoosh reconciliation.

4. **Armory Live Wall Display / Wallboard Stream *(Holding Pattern — See [ARMORY_AV_TECH_SETUP.md](./ARMORY_AV_TECH_SETUP.md))***
   - Once monitor/TV inputs & Denon HDMI Out 2 routing are confirmed, add a lightweight live state stream from the phone APK (1 Hz Polar H10 readiness/HR, rest timer, current/next exercise & sets left) for a secondary gym display without interrupting Fire TV Cube audio.
