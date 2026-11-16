import { Emitter } from '../emitter';

/** Live values from whichever source is active (real BLE or the simulator). */
export interface Live {
  hr: number;
  hrAt: number;
  w: number;
  cad: number;
  /** Cumulative counters straight from the bike (session deltas are computed by the recorder). */
  dist: number;
  kcal: number;
  spd: number;
  bikeAt: number;
}

export const live: Live = { hr: 0, hrAt: 0, w: 0, cad: 0, dist: 0, kcal: 0, spd: 0, bikeAt: 0 };

export const HR_STALE_MS = 5000;
export const BIKE_STALE_MS = 2000;

export const hrFresh = (now = Date.now()) => now - live.hrAt < HR_STALE_MS && live.hr > 0;
export const bikeFresh = (now = Date.now()) => now - live.bikeAt < BIKE_STALE_MS;

/** Fires for every 5 Hz bike frame. */
export const bikeFrame = new Emitter<Live>();
/** Fires when any connection state changes (HR / bike / simulator). */
export const linkStatus = new Emitter();

/** Timestamped H10 beats, so a stalled UI timer can be back-filled from what the sensor really sent. */
export interface HrPoint {
  at: number;
  hr: number;
}
const HR_LOG_MAX = 14400; // 4 h at 1 Hz
const hrLog: HrPoint[] = [];
/** A recorded second takes the nearest beat within this window; otherwise it is a gap (0), never a guess. */
export const HR_MATCH_MS = 3000;

/** Fires for every heart-rate notification (BLE callbacks keep running while the screen is off). */
export const hrBeat = new Emitter();

export function pushHr(hr: number, at = Date.now()): void {
  live.hr = hr;
  live.hrAt = at;
  hrLog.push({ at, hr });
  if (hrLog.length > HR_LOG_MAX) hrLog.splice(0, HR_LOG_MAX / 4);
  hrBeat.emit();
}

/** Heart rate (bpm) from the beat nearest to `atMs`, or 0 when no beat is within `HR_MATCH_MS`. */
export function hrAtTime(atMs: number): number {
  let lo = 0;
  let hi = hrLog.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (hrLog[mid].at < atMs) lo = mid + 1;
    else hi = mid;
  }
  let best: HrPoint | null = null;
  for (const i of [lo - 1, lo]) {
    const p = hrLog[i];
    if (p && (!best || Math.abs(p.at - atMs) < Math.abs(best.at - atMs))) best = p;
  }
  return best && Math.abs(best.at - atMs) <= HR_MATCH_MS && best.hr > 0 ? best.hr : 0;
}

/** Test hook. */
export function clearHrLog(): void {
  hrLog.length = 0;
}

export function pushBike(f: { w: number; cad: number; dist: number; kcal: number; spd: number }): void {
  live.w = f.w;
  live.cad = f.cad;
  live.dist = f.dist;
  live.kcal = f.kcal;
  live.spd = f.spd;
  live.bikeAt = Date.now();
  bikeFrame.emit(live);
}
