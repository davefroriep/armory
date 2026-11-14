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

export function pushHr(hr: number): void {
  live.hr = hr;
  live.hrAt = Date.now();
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
