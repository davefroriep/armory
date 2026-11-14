import { wattsForRpm } from '../domain/airbikeCurve';
import { linkStatus, pushBike, pushHr } from './telemetry';

/**
 * Fake Polar H10 + AirBike so the whole UI (HUD, metronome, save, upload) can be exercised without
 * hardware: Settings -> Developer -> Simulator. Cadence chases the target with lag + noise, HR chases
 * watts with a slow lag.
 */
class Simulator {
  private timer: ReturnType<typeof setInterval> | null = null;
  private cad = 0;
  private hr = 78;
  private dist = 0;
  private kcal = 0;
  private target = 0;
  running = false;

  /** Target cadence to chase (0 = coast down). Set by the HUD every tick. */
  setTarget(rpm: number): void {
    this.target = rpm;
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
    linkStatus.emit();
  }

  private step(dt: number): void {
    const noise = (Math.random() - 0.5) * 1.6;
    const goal = this.target > 0 ? this.target + noise : 0;
    this.cad += (goal - this.cad) * Math.min(1, dt * 0.9);
    if (this.cad < 0.5) this.cad = 0;
    const w = wattsForRpm(this.cad);
    this.dist += this.cad * 0.3833 * 0.44704 * dt;
    this.kcal += (w * 0.04445 / 60) * dt;
    const hrGoal = 70 + Math.min(112, w * 0.42);
    this.hr += (hrGoal - this.hr) * dt * 0.05;
    pushBike({ w, cad: this.cad, dist: this.dist, kcal: this.kcal, spd: this.cad * 0.3833 });
    pushHr(Math.round(this.hr));
  }
}

export const sim = new Simulator();
