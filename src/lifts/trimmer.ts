export const FRAME_FPS = 30;
export const MIN_TRIM_SPAN_SEC = 0.25;

export type TrimFocus = 'playhead' | 'in' | 'out';

export interface TrimState {
  dur: number;
  start: number;
  end: number;
  playhead: number;
  focus: TrimFocus;
  savedStart: number;
  savedEnd: number;
}

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** Round to 1 ms so 1/30s (0.033s) and 5/30s (0.167s) frame steps stay exact. */
export const roundMs = (sec: number): number => Math.round(sec * 1000) / 1000;

/** Format seconds cleanly as `m:ss.cc` (hundredths of a second). */
export function fmtPreciseTime(sec: number): string {
  const s = Math.max(0, roundMs(sec));
  const m = Math.floor(s / 60);
  const rem = (s - m * 60).toFixed(2).padStart(5, '0');
  return `${m}:${rem}`;
}

/** Build initial trim bounds clamped safely against clip duration. */
export function createTrimState(durRaw: number, trimStartSec?: number, trimEndSec?: number): TrimState {
  const dur = Math.max(0, roundMs(durRaw));
  if (dur <= 0) {
    return { dur: 0, start: 0, end: 0, playhead: 0, focus: 'playhead', savedStart: 0, savedEnd: 0 };
  }
  const minSpan = Math.min(dur, MIN_TRIM_SPAN_SEC);
  const end = trimEndSec != null && trimEndSec > 0 ? clamp(roundMs(trimEndSec), minSpan, dur) : dur;
  const start = trimStartSec != null && trimStartSec > 0 ? clamp(roundMs(trimStartSec), 0, Math.max(0, end - minSpan)) : 0;
  return {
    dur,
    start,
    end,
    playhead: start,
    focus: 'playhead',
    savedStart: start,
    savedEnd: end,
  };
}

/** True when trim bounds exclude meaningful footage at start or end (>15 ms). */
export function isTrimmed(st: Pick<TrimState, 'dur' | 'start' | 'end'>): boolean {
  return st.dur > 0 && (st.start > 0.015 || st.end < st.dur - 0.015);
}

/** True when unsaved trim edits differ from persisted trimStartSec / trimEndSec. */
export function isTrimDirty(st: TrimState): boolean {
  return st.dur > 0 && (Math.abs(st.start - st.savedStart) > 0.005 || Math.abs(st.end - st.savedEnd) > 0.005);
}

/** Move Start rail ('in'), End rail ('out'), or scrub playhead on coarse bar drag. */
export function dragTrimHandle(st: TrimState, target: TrimFocus, timeSec: number): TrimState {
  if (st.dur <= 0) return { ...st, focus: target };
  const minSpan = Math.min(st.dur, MIN_TRIM_SPAN_SEC);
  const t = clamp(roundMs(timeSec), 0, st.dur);
  if (target === 'in') {
    const start = clamp(t, 0, Math.max(0, st.end - minSpan));
    return { ...st, start, playhead: start, focus: 'in' };
  }
  if (target === 'out') {
    const end = clamp(t, Math.min(st.dur, st.start + minSpan), st.dur);
    return { ...st, end, playhead: end, focus: 'out' };
  }
  return { ...st, playhead: t, focus: 'playhead' };
}

/** Nudge by deltaSec keeping the active bar ('in' or 'out') and preview frame locked together. */
export function jogTrimDelta(st: TrimState, deltaSec: number): TrimState {
  if (st.dur <= 0) return st;
  const minSpan = Math.min(st.dur, MIN_TRIM_SPAN_SEC);
  if (st.focus === 'in') {
    const start = clamp(roundMs(st.start + deltaSec), 0, Math.max(0, st.end - minSpan));
    return { ...st, start, playhead: start };
  }
  if (st.focus === 'out') {
    const end = clamp(roundMs(st.end + deltaSec), Math.min(st.dur, st.start + minSpan), st.dur);
    return { ...st, end, playhead: end };
  }
  return { ...st, playhead: clamp(roundMs(st.playhead + deltaSec), 0, st.dur) };
}

/** Step by ±1f or ±5f on whichever bar ('in', 'out', or 'playhead') is currently focused. */
export function stepTrimFrames(st: TrimState, frames: number, fps = FRAME_FPS): TrimState {
  return jogTrimDelta(st, frames / Math.max(1, fps));
}

/** Jump back to trim start if user just finished dialing End rail or is outside/at the end of the kept window. */
export function resolvePlayStart(st: TrimState): TrimState {
  if (st.dur <= 0) return { ...st, focus: 'playhead' };
  const trimmed = isTrimmed(st);
  const shouldJumpToStart = st.focus === 'out' || (trimmed && (st.playhead < st.start - 0.01 || st.playhead >= st.end - 0.04));
  return {
    ...st,
    focus: 'playhead',
    playhead: shouldJumpToStart ? st.start : st.playhead,
  };
}
