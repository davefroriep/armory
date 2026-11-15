import type { Segment } from './programs';
import type { Sample } from '../types';

export interface RepStat {
  kind: 'work' | 'rest';
  /** 1-based number within its kind. */
  n: number;
  sec: number;
  avgW: number;
  hrStart: number;
  hrEnd: number;
  hrPeak: number;
}

const mean = (xs: number[]): number => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

/**
 * Per-rep and per-recovery numbers from the real (post-gate) timeline and the 1 Hz samples:
 * average watts and HR at the end of each work rep, and how long each recovery really took and
 * what HR it ended on. These are the progress metrics: same power at a lower HR, or a faster
 * recovery, means the engine is getting bigger.
 */
export function repStats(segments: readonly Segment[], samples: readonly Sample[]): RepStat[] {
  const out: RepStat[] = [];
  const count = { work: 0, rest: 0 };
  let start = 0;
  for (const g of segments) {
    const end = start + g.seconds;
    if ((g.kind === 'work' || g.kind === 'rest') && !g.allOut) {
      const win = samples.filter((s) => s.t >= start && s.t < end);
      if (win.length) {
        const hr = win.filter((s) => s.hr > 0).map((s) => s.hr);
        const tailHr = win.slice(-4).filter((s) => s.hr > 0).map((s) => s.hr);
        const headHr = win.slice(0, 3).filter((s) => s.hr > 0).map((s) => s.hr);
        out.push({
          kind: g.kind, n: ++count[g.kind], sec: Math.round(g.seconds),
          avgW: mean(win.map((s) => s.w)),
          hrStart: Math.round(mean(headHr)), hrEnd: Math.round(mean(tailHr)), hrPeak: hr.length ? Math.max(...hr) : 0,
        });
      }
    }
    start = end;
  }
  return out;
}

const mmss = (s: number): string => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;

/** One-line progress note for the Intervals.icu description. */
export function repNote(stats: readonly RepStat[]): string {
  const work = stats.filter((r) => r.kind === 'work');
  const rest = stats.filter((r) => r.kind === 'rest');
  if (!work.length) return '';
  const parts = [`Reps ${work.map((r) => `${Math.round(r.avgW)}W${r.hrPeak ? `@${r.hrPeak}` : ''}`).join(' · ')}`];
  if (rest.length) parts.push(`Recoveries ${rest.map((r) => `${mmss(r.sec)}${r.hrEnd ? `→${r.hrEnd}` : ''}`).join(' · ')}`);
  return parts.join(' | ');
}
