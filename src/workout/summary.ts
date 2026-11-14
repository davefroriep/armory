import type { Sample, WorkoutMeta } from '../types';

export type Stats = Pick<WorkoutMeta, 'seconds' | 'kj' | 'avgW' | 'maxW' | 'avgHr' | 'maxHr'>;

/** Recompute the summary stats from recorded 1 Hz samples (used for crash recovery). */
export function summarize(samples: Sample[]): Stats {
  const n = samples.length;
  const hr = samples.filter((s) => s.hr > 0);
  return {
    seconds: n,
    kj: samples.reduce((a, s) => a + s.w, 0) / 1000,
    avgW: n ? samples.reduce((a, s) => a + s.w, 0) / n : 0,
    maxW: samples.reduce((m, s) => Math.max(m, s.w), 0),
    avgHr: hr.length ? hr.reduce((a, s) => a + s.hr, 0) / hr.length : 0,
    maxHr: hr.reduce((m, s) => Math.max(m, s.hr), 0),
  };
}
