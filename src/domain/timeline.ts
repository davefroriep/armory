import type { Segment } from './programs';

export interface Locate {
  /** Index of the active segment, or -1 when the program has no segments. */
  index: number;
  seg: Segment | null;
  next: Segment | null;
  /** Seconds into the active segment. */
  into: number;
  /** Seconds left in the active segment. */
  remaining: number;
  /** Seconds left in the whole program (0 when complete). */
  totalRemaining: number;
  complete: boolean;
}

export function totalOf(segments: readonly Segment[]): number {
  return segments.reduce((a, g) => a + g.seconds, 0);
}

export function locate(segments: readonly Segment[], elapsed: number): Locate {
  if (segments.length === 0) {
    return { index: -1, seg: null, next: null, into: elapsed, remaining: 0, totalRemaining: 0, complete: false };
  }
  const total = totalOf(segments);
  if (elapsed >= total) {
    const last = segments[segments.length - 1];
    return { index: segments.length - 1, seg: last, next: null, into: last.seconds, remaining: 0, totalRemaining: 0, complete: true };
  }
  let start = 0;
  for (let i = 0; i < segments.length; i++) {
    const end = start + segments[i].seconds;
    if (elapsed < end) {
      return {
        index: i, seg: segments[i], next: segments[i + 1] ?? null,
        into: Math.max(0, elapsed - start), remaining: end - elapsed, totalRemaining: total - elapsed, complete: false,
      };
    }
    start = end;
  }
  throw new Error('unreachable');
}

/** Start offset (seconds) of every segment. */
export function segmentStarts(segments: readonly Segment[]): number[] {
  const out: number[] = [];
  let t = 0;
  for (const g of segments) { out.push(t); t += g.seconds; }
  return out;
}
