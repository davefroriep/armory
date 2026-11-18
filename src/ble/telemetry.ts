import { Emitter } from '../emitter';

/** Live values from whichever source is active (real BLE or the simulator). */
export interface Live {
  dev: 'airbike' | 'spin';
  hr: number;
  hrAt: number;
  w: number;
  cad: number;
  /** Cumulative counters straight from the bike (session deltas are computed by the recorder). */
  dist: number;
  kcal: number;
  spd: number;
  fan: number;
  rfan: number;
  pls: number;
  dt: number;
  ratio: number;
  lvl: number;
  tlvl: number;
  tgtW: number;
  mode: number;
  pot: number;
  pos: number;
  bikeAt: number;
}

export const live: Live = {
  dev: 'airbike',
  hr: 0, hrAt: 0, w: 0, cad: 0, dist: 0, kcal: 0, spd: 0,
  fan: 0, rfan: 0, pls: 0, dt: 0, ratio: 11.11,
  lvl: 0, tlvl: 0, tgtW: 0, mode: 0, pot: 0, pos: 0, bikeAt: 0,
};

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

/** Recent valid H10 beats within `windowMs` ending at `now`. */
export function recentHrPoints(windowMs: number, now = Date.now()): HrPoint[] {
  const cutoff = now - windowMs;
  const out: HrPoint[] = [];
  for (let i = hrLog.length - 1; i >= 0; i--) {
    const p = hrLog[i];
    if (p.at < cutoff) break;
    if (p.hr > 0 && p.at <= now) out.push(p);
  }
  out.reverse();
  return out;
}

/** Contiguous current strap-session beats, breaking at any silence > `gapMs` (default 60s), resampled to <=1 per second. */
export function currentStrapBeats(gapMs = 60_000): HrPoint[] {
  if (!hrLog.length) return [];
  const raw: HrPoint[] = [];
  let prevAt = hrLog[hrLog.length - 1].at;
  for (let i = hrLog.length - 1; i >= 0; i--) {
    const p = hrLog[i];
    if (prevAt - p.at > gapMs) break;
    if (p.hr > 0) raw.push(p);
    prevAt = p.at;
  }
  raw.reverse();
  const out: HrPoint[] = [];
  let lastSec = -1;
  for (const p of raw) {
    const sec = Math.floor(p.at / 1000);
    if (sec !== lastSec) {
      out.push(p);
      lastSec = sec;
    } else {
      out[out.length - 1] = p;
    }
  }
  return out;
}

/** Test hook. */
export function clearHrLog(): void {
  hrLog.length = 0;
}

let spinDistM = 0;
let spinKcal = 0;
let spinLastAt = 0;

/** Reset virtual odometer accumulators (useful when starting a Spin ride or in tests). */
export function resetSpinOdometer(): void {
  spinDistM = 0;
  spinKcal = 0;
  spinLastAt = 0;
}

export function pushBike(f: {
  w: number; cad: number; dist: number; kcal: number; spd: number;
  fan?: number; rfan?: number; pls?: number; dt?: number; ratio?: number;
  dev?: 'airbike' | 'spin'; lvl?: number; tlvl?: number; tgt_w?: number; mode?: number; pot?: number; pos?: number;
}, now = Date.now()): void {
  const dev = f.dev ?? 'airbike';
  live.dev = dev;
  live.w = f.w;
  live.cad = f.cad;
  if (dev === 'spin' && f.dist <= 0 && f.kcal <= 0 && f.spd <= 0) {
    const dtSec = spinLastAt && now > spinLastAt ? Math.min(2, (now - spinLastAt) / 1000) : 0;
    spinLastAt = now;
    const pedaling = f.cad > 2 && f.w > 0;
    const vMps = pedaling ? Math.cbrt(f.w / 0.23) : 0;
    if (dtSec > 0 && pedaling) {
      spinDistM += vMps * dtSec;
      spinKcal += (f.w / 1000) * dtSec; // Spin Bike convention: 1 kJ = 1 kcal
    }
    live.spd = vMps * 3.6;
    live.dist = spinDistM;
    live.kcal = spinKcal;
  } else {
    live.dist = f.dist;
    live.kcal = f.kcal;
    live.spd = f.spd;
  }
  if (f.fan !== undefined) live.fan = f.fan;
  if (f.rfan !== undefined) live.rfan = f.rfan;
  if (f.pls !== undefined) live.pls = f.pls;
  if (f.dt !== undefined) live.dt = f.dt;
  if (f.ratio !== undefined) live.ratio = f.ratio;
  if (f.lvl !== undefined) live.lvl = f.lvl;
  if (f.tlvl !== undefined) live.tlvl = f.tlvl;
  if (f.tgt_w !== undefined) live.tgtW = f.tgt_w;
  if (f.mode !== undefined) live.mode = f.mode;
  if (f.pot !== undefined) live.pot = f.pot;
  if (f.pos !== undefined) live.pos = f.pos;
  live.bikeAt = now;
  bikeFrame.emit(live);
}
