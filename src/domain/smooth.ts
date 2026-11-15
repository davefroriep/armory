/** Trailing time-window average (used for the 3 s display smoothing of cadence and power). */
export class WindowAvg {
  private pts: { t: number; v: number }[] = [];
  constructor(private readonly windowMs: number) {}

  push(t: number, v: number): void {
    this.pts.push({ t, v });
    this.trim(t);
  }

  /** Mean of the samples in the window, or null when there are none (sensor silent). */
  value(now: number, windowMs: number = this.windowMs): number | null {
    this.trim(now);
    const cut = now - Math.min(windowMs, this.windowMs);
    let s = 0;
    let n = 0;
    for (const p of this.pts) if (p.t >= cut) { s += p.v; n++; }
    return n ? s / n : null;
  }

  clear(): void {
    this.pts = [];
  }

  private trim(now: number): void {
    const cut = now - this.windowMs;
    while (this.pts.length && this.pts[0].t < cut) this.pts.shift();
  }
}
