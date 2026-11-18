import { wattsForRpm } from '../domain/airbikeCurve';
import { linkStatus, pushBike, pushHr } from './telemetry';

/**
 * Fake Polar H10 + AirBike so the whole UI (HUD, metronome, save, upload) can be exercised without
 * hardware: Settings -> Developer -> Simulator. Cadence chases the target with lag + noise, HR chases
 * watts with a slow lag.
 */
class Simulator {
  private timer: ReturnType<typeof setInterval> | null = null;
  private mode: 'airbike' | 'spin' = 'airbike';
  private cad = 0;
  private watts = 0;
  private hr = 78;
  private dist = 0;
  private kcal = 0;
  private targetRpm = 0;
  private targetWatts = 0;
  running = false;

  setMode(mode: 'airbike' | 'spin'): void {
    this.mode = mode;
  }

  /** Target cadence (AirBike) or target cadence + watts (Spin) to chase (0 = coast down). */
  setTarget(rpm: number, spinWatts = 0): void {
    this.targetRpm = rpm;
    this.targetWatts = spinWatts;
  }

  start(): void {
    if (this.timer) return;
    this.running = true;
    this.timer = setInterval(() => this.step(0.2), 200);
    linkStatus.emit();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.running = false;
    this.cad = 0;
    this.watts = 0;
    linkStatus.emit();
  }

  private step(dt: number): void {
    const noise = (Math.random() - 0.5) * 1.6;
    const goalCad = this.targetRpm > 0 ? this.targetRpm + noise : (this.mode === 'spin' && this.targetWatts > 0 ? 85 + noise : 0);
    this.cad += (goalCad - this.cad) * Math.min(1, dt * 0.9);
    if (this.cad < 0.5) this.cad = 0;
    if (this.mode === 'spin') {
      const goalW = this.cad > 0 ? (this.targetWatts > 0 ? this.targetWatts + noise * 2.5 : 110) : 0;
      this.watts += (goalW - this.watts) * Math.min(1, dt * 1.3);
      if (this.watts < 1) this.watts = 0;
      const w = this.watts;
      const hrGoal = 70 + Math.min(112, w * 0.42);
      this.hr += (hrGoal - this.hr) * dt * 0.05;
      pushBike({
        dev: 'spin', w, cad: this.cad, dist: 0, kcal: 0, spd: 0,
        lvl: w > 0 ? Math.max(5, Math.min(80, Math.round(w / 6))) : 5,
        tgt_w: this.targetWatts, mode: this.targetWatts > 0 ? 1 : 0, pot: 1200, pos: 9000,
      });
    } else {
      const w = wattsForRpm(this.cad);
      this.dist += this.cad * 0.3833 * 0.44704 * dt;
      this.kcal += (w * 0.04445 / 60) * dt;
      const hrGoal = 70 + Math.min(112, w * 0.42);
      this.hr += (hrGoal - this.hr) * dt * 0.05;
      pushBike({ w, cad: this.cad, dist: this.dist, kcal: this.kcal, spd: this.cad * 0.3833 });
    }
    pushHr(Math.round(this.hr));
  }
}

export const sim = new Simulator();
