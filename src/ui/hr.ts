export const hrZone = (hr: number, max: number): { z: number; pct: number } => {
  const pct = hr / max;
  return { pct, z: pct < 0.6 ? 1 : pct < 0.7 ? 2 : pct < 0.8 ? 3 : pct < 0.88 ? 4 : 5 };
};
export const ZONE_COLOR = ['', '#8a94a3', '#38bdf8', '#22c55e', '#f59e0b', '#ff4d2e'];
