import { wattsForRpm } from './airbikeCurve';
import type { Segment } from './programs';

/** Top of the profile chart, as % of FTP. Anything at or above it draws full height. */
export const PROFILE_MAX_PCT = 150;
/** Shortest bar (fraction of chart height) so full-rest blocks are still visible. */
export const PROFILE_MIN_H = 0.12;
/** % FTP used to place an all-out block (no fixed target) at the top of the chart. */
const ALL_OUT_PCT = 200;

export interface ProfileBar {
  /** Segment intensity, % of FTP (all-out = 200). 0 for full rest. */
  pct: number;
  /** Bar height, 0..1 of the chart. */
  h: number;
  color: string;
}

/** Intervals.icu / Wahoo-style power-zone colours by % FTP (Coggan zone edges). */
export function intensityColor(pct: number, allOut = false): string {
  if (allOut) return '#f43f5e';
  if (pct < 56) return '#6b7a90'; // Z1 active recovery
  if (pct < 76) return '#3b9cff'; // Z2 endurance
  if (pct < 91) return '#22c55e'; // Z3 tempo
  if (pct < 106) return '#facc15'; // Z4 threshold
  if (pct < 121) return '#fb923c'; // Z5 VO2
  return '#ef4444'; // Z6+ anaerobic
}

/** Height and colour for one block of a program, from its (already FTP-resolved) target cadence. */
export function profileBar(seg: Segment, ftp: number): ProfileBar {
  if (seg.allOut) return { pct: ALL_OUT_PCT, h: 1, color: intensityColor(ALL_OUT_PCT, true) };
  const pct = seg.rpm == null || ftp <= 0 ? 0 : (wattsForRpm(seg.rpm) / ftp) * 100;
  const h = Math.min(1, Math.max(PROFILE_MIN_H, pct / PROFILE_MAX_PCT));
  return { pct, h, color: intensityColor(pct) };
}

/** Where the 100 % FTP reference line sits, as a fraction of chart height. */
export const FTP_LINE_H = 100 / PROFILE_MAX_PCT;
