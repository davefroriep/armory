/** Pure helpers for Gym Cam burst selection and tape scrubbing timelines. */

/** Returns the index of the most recent burst ending at/before `checkedMs + 5s`, falling back to closest overall. */
export function pickNearestBurstIndex(
  bursts: readonly { startMs: number; endMs: number }[],
  checkedMs: number,
): number {
  if (!bursts.length) return -1;
  let bestPriorIdx = -1;
  let bestPriorEnd = -Infinity;
  let fallbackIdx = 0;
  let fallbackDiff = Infinity;

  for (let i = 0; i < bursts.length; i++) {
    const end = bursts[i].endMs;
    if (end <= checkedMs + 5000 && end >= bestPriorEnd) {
      bestPriorEnd = end;
      bestPriorIdx = i;
    }
    const diff = Math.abs(end - checkedMs);
    if (diff < fallbackDiff) {
      fallbackDiff = diff;
      fallbackIdx = i;
    }
  }
  return bestPriorIdx >= 0 ? bestPriorIdx : fallbackIdx;
}

/** Computes visible `[winStart, winEnd]` ms window centered on `focusMs` for `5m` / `15m` / `All` zoom. */
export function computeScrubWindow(
  minMs: number,
  maxMs: number,
  focusMs: number,
  zoomMin: number | null,
): { winStart: number; winEnd: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 2000, maxMs);
  if (zoomMin == null || hi - lo <= zoomMin * 60_000) {
    return { winStart: lo, winEnd: hi };
  }
  const span = zoomMin * 60_000;
  const half = Math.round(span / 2);
  const winStart = Math.max(lo, Math.min(hi - span, focusMs - half));
  return { winStart, winEnd: Math.min(hi, winStart + span) };
}

/** Computes zoomed `[winStart, winEnd]` ms range expanding IN/OUT trim markers across the full scrub rail. */
export function computeClipZoomWindow(
  minMs: number,
  maxMs: number,
  inMs: number,
  outMs: number,
  mode: 'in_set' | 'both_set',
): { winStart: number; winEnd: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 2000, maxMs);
  const cIn = Math.max(lo, Math.min(hi, Math.min(inMs, outMs)));
  const cOut = Math.max(Math.min(hi, cIn + 2000), Math.min(hi, Math.max(inMs, outMs)));
  const span = Math.max(3_000, cOut - cIn);
  if (mode === 'in_set') {
    const winStart = Math.max(lo, cIn - 8_000);
    const winEnd = Math.min(hi, Math.max(cOut + 20_000, cIn + Math.max(75_000, Math.round(span * 1.6))));
    return { winStart, winEnd: Math.max(winStart + 4_000, winEnd) };
  }
  const pad = Math.min(15_000, Math.max(4_000, Math.round(span * 0.14)));
  const winStart = Math.max(lo, cIn - pad);
  const winEnd = Math.min(hi, cOut + pad);
  return { winStart, winEnd: Math.max(winStart + 4_000, winEnd) };
}

/** Updates clip IN/OUT marker from `cursorMs`, enforcing a valid `[3s .. 120s]` clip span inside `[minMs, maxMs]`. */
export function applyClipMarker(
  inMs: number,
  outMs: number,
  cursorMs: number,
  side: 'in' | 'out',
  minMs: number,
  maxMs: number,
): { inMs: number; outMs: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 3000, maxMs);
  const cur = Math.max(lo, Math.min(hi, Math.round(cursorMs)));
  if (side === 'in') {
    const nextIn = Math.min(cur, hi - 3000);
    let nextOut = outMs;
    if (nextOut < nextIn + 3000 || nextOut - nextIn > 120_000) {
      nextOut = Math.min(hi, Math.max(nextIn + 3000, nextIn + 45_000));
    }
    return { inMs: nextIn, outMs: nextOut };
  }
  const nextOut = Math.max(cur, lo + 3000);
  let nextIn = inMs;
  if (nextIn > nextOut - 3000 || nextOut - nextIn > 120_000) {
    nextIn = Math.max(lo, Math.min(nextOut - 3000, nextOut - 45_000));
  }
  return { inMs: nextIn, outMs: nextOut };
}

/** Short display label (`"Rack Cam"` -> `"Rack"`, `"Smith Cam"` -> `"Smith"`). */
export function shortCamLabel(name: string): string {
  return name.trim().replace(/\s+cam$/i, '') || name;
}

/** Formats relative offset between `targetMs` and set check time `checkedMs`. */
export function fmtRelativeToCheck(targetMs: number, checkedMs: number): string {
  const dSec = Math.round((checkedMs - targetMs) / 1000);
  if (Math.abs(dSec) <= 3) return 'right at ✓';
  const abs = Math.abs(dSec);
  const m = Math.floor(abs / 60);
  const s = abs % 60;
  const span = m > 0 ? (s ? `${m}m ${s}s` : `${m}m`) : `${abs}s`;
  return dSec > 0 ? `${span} before ✓` : `${span} after ✓`;
}

/** Binary-searches sorted tape frames by `tsMs` and returns index closest to `targetMs` (tie-breaks lower). */
export function findNearestFrameIndex(frames: readonly { tsMs: number }[], targetMs: number): number {
  const n = frames.length;
  if (n === 0) return -1;
  if (n === 1) return 0;
  if (targetMs <= frames[0].tsMs) return 0;
  if (targetMs >= frames[n - 1].tsMs) return n - 1;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (frames[mid].tsMs < targetMs) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) {
    const dPrev = Math.abs(targetMs - frames[lo - 1].tsMs);
    const dCur = Math.abs(frames[lo].tsMs - targetMs);
    if (dPrev <= dCur) return lo - 1;
  }
  return lo;
}
