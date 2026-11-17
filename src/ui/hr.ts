export const hrZone = (hr: number, max: number): { z: number; pct: number } => {
  const pct = hr / max;
  return { pct, z: pct < 0.6 ? 1 : pct < 0.7 ? 2 : pct < 0.8 ? 3 : pct < 0.88 ? 4 : 5 };
};
export const ZONE_COLOR = ['', '#8a94a3', '#38bdf8', '#22c55e', '#f59e0b', '#ff4d2e'];
export const ZONE_NAME = ['', 'Recovery', 'Endurance', 'Tempo', 'Threshold', 'VO2 Max'];

export interface HrDropStats {
  peak: number;
  peakAt: number;
  low: number;
  /** Peak BPM over the window minus current HR (>= 0). */
  drop: number;
}

/** Peak, low, and 60s HR drop from recent HR points relative to `curHr`. */
export function hrDropStats(
  pts: { at: number; hr: number }[],
  curHr: number,
  now = Date.now(),
  windowMs = 60_000,
): HrDropStats | null {
  const cutoff = now - windowMs;
  let peak = 0;
  let peakAt = now;
  let low = Infinity;
  for (const p of pts) {
    if (p.at < cutoff || p.at > now || p.hr <= 0) continue;
    if (p.hr >= peak) { peak = p.hr; peakAt = p.at; }
    if (p.hr < low) low = p.hr;
  }
  if (curHr > 0) {
    if (curHr >= peak) { peak = curHr; peakAt = now; }
    if (curHr < low) low = curHr;
  }
  if (!peak || !Number.isFinite(low)) return null;
  const cur = curHr > 0 ? curHr : peak;
  return { peak, peakAt, low, drop: Math.max(0, peak - cur) };
}
