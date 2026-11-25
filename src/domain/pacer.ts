import { bpmForRpm, wattsForRpm } from './airbikeCurve';
import type { Program, Segment } from './programs';
import type { Locate } from './timeline';

export type MetroMode = 'transitions' | 'whole';

export interface PacerConfig {
  mode: MetroMode;
  /** Seconds before a transition that the metronome starts (at the upcoming tempo). */
  lead: number;
  /** Seconds after a transition that the metronome keeps going. */
  tail: number;
  /** In "transitions" mode, stop early once cadence has been locked on target. */
  autoMute: boolean;
}

export interface PacerDecision {
  play: boolean;
  rpm: number | null;
  bpm: number | null;
}

const OFF: PacerDecision = { play: false, rpm: null, bpm: null };

/** Seconds before a ramp-up transition where slow-down warnings pause and the dual-target bar appears. */
export const RAMP_TRANSITION_SEC = 5;

/**
 * True when transitioning from a lower-intensity block (warm-up / rest / float) into a
 * higher-intensity interval (work / steady). Always false when leaving an effort block
 * into rest, float, or cool-down so rest metronome and ramp UI never fire during work.
 */
export function isRampUpTransition(cur: Segment | null, next: Segment | null): boolean {
  if (!cur || !next) return false;
  const curEffort = cur.kind === 'work' || cur.kind === 'steady';
  const nextRecovery = next.kind === 'rest' || next.kind === 'float' || next.kind === 'cooldown';
  if (curEffort && nextRecovery) return false;
  if (cur.watts != null || next.watts != null) {
    if (next.watts == null) return false;
    if (cur.watts == null) return true;
    return next.watts > cur.watts;
  }
  if (next.rpm == null) return false;
  if (cur.rpm == null) return true;
  return next.rpm > cur.rpm;
}

/**
 * Metronome tempo = 2 x target pedal RPM (one beat per leg stroke). Target RPM comes from the
 * program (and is exactly what the Assault curve maps to the target watts), so tempo, cadence and
 * wattage are one consistent number.
 */
export function pacerDecision(
  program: Program, loc: Locate, cfg: PacerConfig, enabled: boolean, lockedIn: boolean,
): PacerDecision {
  if (!enabled) return OFF;

  if (program.segments.length === 0) {
    // Free ride / goal programs: no transitions, so only "whole workout" mode makes sense.
    if (cfg.mode !== 'whole' || !program.paceRpm) return OFF;
    return { play: true, rpm: program.paceRpm, bpm: bpmForRpm(program.paceRpm) };
  }
  if (loc.complete || !loc.seg) return OFF;

  const rampUp = isRampUpTransition(loc.seg, loc.next);
  const inLead = rampUp && loc.next !== null && loc.remaining <= cfg.lead;
  // Boundary 0 (start of the workout) is not a transition: nothing to prepare for.
  const inTail = loc.index >= 1 && loc.into <= cfg.tail;

  const rpm = tempoRpm(loc.seg, loc.next, inLead);
  if (rpm == null) return OFF;

  if (cfg.mode === 'whole') return { play: true, rpm, bpm: bpmForRpm(rpm) };

  if (!(inLead || inTail)) return OFF;
  if (cfg.autoMute && lockedIn && inTail && !inLead) return OFF;
  return { play: true, rpm, bpm: bpmForRpm(rpm) };
}

function tempoRpm(cur: Segment, next: Segment | null, inLead: boolean): number | null {
  if (inLead && next && next.rpm != null) return next.rpm;
  return cur.rpm;
}

// ---------------------------------------------------------------- live cadence cues

export type CueLevel = 'idle' | 'ok' | 'warn' | 'bad';
export interface Cue {
  level: CueLevel;
  text: string;
  /** Fire the launch-overshoot chirp (first 15 s of an effort block). */
  chirp: boolean;
}

export const CORRIDOR_RPM = 1.5;
export const OVERSHOOT_RPM = 2.5;
export const CHIRP_RPM = 3.0;
const FLOAT_FLOOR_RPM = 35;

export const CORRIDOR_WATTS = 10;
export const OVERSHOOT_WATTS = 25;
export const CHIRP_WATTS = 35;

/** Live wattage/cadence cue for Auto-ERG Spin Bike workouts (`SS2K_Tuner`). */
export function wattsCueFor(seg: Segment | null, w: number, cad: number, into: number): Cue {
  const idle: Cue = { level: 'idle', text: '', chirp: false };
  if (!seg || seg.watts == null) return idle;
  if (cad < 10) return { level: 'warn', text: 'START PEDALING', chirp: false };
  if (seg.kind === 'work' && cad < 60) {
    return { level: 'warn', text: `SPIN FASTER (${seg.rpm ?? 85} RPM)`, chirp: false };
  }
  const diff = w - seg.watts;
  const effort = seg.kind === 'work' || seg.kind === 'steady';
  if (diff > OVERSHOOT_WATTS) {
    return { level: 'bad', text: `+${Math.round(diff)} W TOO HOT — EASE OFF`, chirp: effort && diff > CHIRP_WATTS && into <= 15 };
  }
  if (diff < -15) return { level: 'warn', text: `${Math.round(seg.watts - w)} W UNDER — PUSH`, chirp: false };
  if (Math.abs(diff) <= CORRIDOR_WATTS) return { level: 'ok', text: 'ERG ON TARGET', chirp: false };
  return { level: 'ok', text: 'ON TARGET', chirp: false };
}

export function cueFor(seg: Segment | null, cad: number, into: number): Cue {
  const idle: Cue = { level: 'idle', text: '', chirp: false };
  if (!seg || seg.rpm == null) return idle;
  if (cad < 5) return { level: 'warn', text: seg.kind === 'float' ? 'KEEP FAN SPINNING' : 'START PEDALING', chirp: false };

  if (seg.allOut) {
    return cad >= seg.rpm ? { level: 'ok', text: 'ALL OUT', chirp: false } : { level: 'warn', text: `PUSH — ${seg.rpm}+ RPM`, chirp: false };
  }
  if (seg.kind === 'float' && cad < FLOAT_FLOOR_RPM) {
    return { level: 'warn', text: `KEEP FAN SPINNING (${seg.rpm} RPM)`, chirp: false };
  }
  const diff = cad - seg.rpm;
  const effort = seg.kind === 'work' || seg.kind === 'steady';
  if (diff > OVERSHOOT_RPM) {
    const excess = Math.round(wattsForRpm(cad) - wattsForRpm(seg.rpm));
    return { level: 'bad', text: `+${excess} W TOO HOT — EASE OFF`, chirp: effort && diff > CHIRP_RPM && into <= 15 };
  }
  if (diff < -CORRIDOR_RPM) return { level: 'warn', text: 'PICK IT UP', chirp: false };
  return { level: 'ok', text: 'ON TARGET', chirp: false };
}

/** Work-block HR ceiling (fraction of HRmax): past it the effort is too hot for a repeatable VO2 session. */
export const HR_CEILING = 0.96;

/** Extra cue on top of the cadence cue: HR too high on a work block, or recovery progress on a gated rest. */
export function hrCue(seg: Segment | null, hr: number, hrMax: number, gateLimit: number | null): Cue | null {
  if (!seg || hr <= 0) return null;
  if (seg.gate && gateLimit != null) {
    return hr <= gateLimit
      ? { level: 'ok', text: `HR ${hr} · RECOVERED`, chirp: false }
      : { level: 'warn', text: `HR ${hr} → ${gateLimit} · EASY SPIN`, chirp: false };
  }
  if (seg.kind === 'work' && !seg.allOut && hr >= HR_CEILING * hrMax) {
    return { level: 'bad', text: `HR ${Math.round((hr / hrMax) * 100)}% — EASE OFF`, chirp: false };
  }
  return null;
}

/**
 * Cadence cue during the 5-second red ramp-up countdown before a higher-intensity interval.
 * Strictly blocks all "TOO HOT / EASE OFF" slow-down warnings and chirps so ramping early feels clean.
 */
export function rampCueFor(cur: Segment | null, next: Segment | null, cad: number): Cue {
  if (!next || next.rpm == null) return { level: 'idle', text: '', chirp: false };
  const hi = Math.round(next.rpm);
  const lo = cur?.rpm ?? null;
  if (cad < 5) return { level: 'warn', text: `RAMP TO ${hi}${next.allOut ? '+' : ''} RPM`, chirp: false };
  if (next.allOut) {
    return cad >= next.rpm
      ? { level: 'ok', text: 'ON WORK TARGET', chirp: false }
      : { level: 'ok', text: `RAMP → ${hi}+ RPM`, chirp: false };
  }
  if (cad >= next.rpm - CORRIDOR_RPM) return { level: 'ok', text: 'ON WORK TARGET', chirp: false };
  if (lo != null && cad < lo - CORRIDOR_RPM) return { level: 'warn', text: `RAMP → ${hi} RPM`, chirp: false };
  if (lo != null && cad > lo + CORRIDOR_RPM) return { level: 'ok', text: `RAMPING → ${hi} RPM`, chirp: false };
  return { level: 'ok', text: `RAMP → ${hi} RPM`, chirp: false };
}

/**
 * Spin ERG wattage cue during the 5-second red ramp-up countdown before a higher-intensity interval.
 * Never complains about ramping early.
 */
export function rampWattsCueFor(cur: Segment | null, next: Segment | null, w: number, cad: number): Cue {
  if (!next || next.watts == null) return { level: 'idle', text: '', chirp: false };
  const hi = Math.round(next.watts);
  if (cad < 10) return { level: 'warn', text: `RAMP TO ${hi} W`, chirp: false };
  if (w >= next.watts - CORRIDOR_WATTS) return { level: 'ok', text: 'ON WORK TARGET', chirp: false };
  const lo = cur?.watts ?? null;
  if (lo != null && w > lo + CORRIDOR_WATTS) return { level: 'ok', text: `RAMPING → ${hi} W`, chirp: false };
  return { level: 'ok', text: `RAMP → ${hi} W`, chirp: false };
}

export interface GaugeModel {
  off: boolean;
  ramp: boolean;
  zoneLeftPct: number;
  zoneWidthPct: number;
  hiZoneLeftPct: number;
  hiZoneWidthPct: number;
  loMarkPct: number | null;
  loLabel: string;
  hiMarkPct: number | null;
  hiLabel: string;
  needlePct: number;
}

const clampPct = (v: number, lo: number, hi: number): number =>
  Math.min(100, Math.max(0, ((v - lo) / Math.max(0.001, hi - lo)) * 100));

/**
 * Computes percentage geometry for the center cadence/wattage bar.
 * In normal mode, centers the current segment target at 50%.
 * During the 5-second ramp-up transition (`inRampTransition`), shows the rest target on the low end
 * and the upcoming higher-intensity target on the high end with a shaded ramp bridge between them.
 */
export function computeGaugeModel(
  seg: Segment | null,
  next: Segment | null,
  cad: number,
  w: number,
  inRampTransition: boolean,
): GaugeModel {
  if (inRampTransition && seg && next && isRampUpTransition(seg, next)) {
    if (next.watts != null) {
      const highTgt = Math.round(next.watts);
      const hasLow = seg.watts != null;
      const lowTgt = hasLow ? Math.round(seg.watts!) : Math.max(50, highTgt - 100);
      const span = Math.max(30, highTgt - lowTgt);
      const pad = Math.max(20, Math.round(span * 0.28));
      const lo = lowTgt - pad;
      const hi = highTgt + pad;
      const zl = clampPct(lowTgt - CORRIDOR_WATTS, lo, hi);
      const zh = clampPct(highTgt + CORRIDOR_WATTS, lo, hi);
      const hzl = clampPct(highTgt - CORRIDOR_WATTS, lo, hi);
      const hzh = clampPct(highTgt + CORRIDOR_WATTS, lo, hi);
      return {
        off: false,
        ramp: true,
        zoneLeftPct: zl,
        zoneWidthPct: Math.max(0, zh - zl),
        hiZoneLeftPct: hzl,
        hiZoneWidthPct: Math.max(0, hzh - hzl),
        loMarkPct: clampPct(lowTgt, lo, hi),
        loLabel: hasLow ? `${lowTgt}W` : 'REST',
        hiMarkPct: clampPct(highTgt, lo, hi),
        hiLabel: `${highTgt}W`,
        needlePct: clampPct(w, lo, hi),
      };
    }
    if (next.rpm != null) {
      const highTgt = Math.round(next.rpm);
      const hasLow = seg.rpm != null;
      const lowTgt = hasLow ? Math.round(seg.rpm!) : Math.max(25, Math.min(35, highTgt - 15));
      const span = Math.max(8, highTgt - lowTgt);
      const pad = Math.max(5, Math.round(span * 0.28));
      const lo = lowTgt - pad;
      const hi = next.allOut ? highTgt + Math.max(12, pad) : highTgt + pad;
      const zl = clampPct(lowTgt - CORRIDOR_RPM, lo, hi);
      const zh = next.allOut ? 100 : clampPct(highTgt + CORRIDOR_RPM, lo, hi);
      const hzl = next.allOut ? clampPct(highTgt, lo, hi) : clampPct(highTgt - CORRIDOR_RPM, lo, hi);
      const hzh = next.allOut ? 100 : clampPct(highTgt + CORRIDOR_RPM, lo, hi);
      return {
        off: false,
        ramp: true,
        zoneLeftPct: zl,
        zoneWidthPct: Math.max(0, zh - zl),
        hiZoneLeftPct: hzl,
        hiZoneWidthPct: Math.max(0, hzh - hzl),
        loMarkPct: clampPct(lowTgt, lo, hi),
        loLabel: hasLow ? String(lowTgt) : 'REST',
        hiMarkPct: clampPct(highTgt, lo, hi),
        hiLabel: `${highTgt}${next.allOut ? '+' : ''}`,
        needlePct: clampPct(cad, lo, hi),
      };
    }
  }

  if (!seg || (seg.watts == null && seg.rpm == null)) {
    return {
      off: true, ramp: false, zoneLeftPct: 0, zoneWidthPct: 0, hiZoneLeftPct: 0, hiZoneWidthPct: 0,
      loMarkPct: null, loLabel: '', hiMarkPct: null, hiLabel: '', needlePct: 50,
    };
  }

  if (seg.watts != null) {
    const lo = seg.watts - 40;
    const hi = seg.watts + 40;
    const zl = clampPct(seg.watts - CORRIDOR_WATTS, lo, hi);
    const zh = clampPct(seg.watts + CORRIDOR_WATTS, lo, hi);
    return {
      off: false, ramp: false, zoneLeftPct: zl, zoneWidthPct: zh - zl, hiZoneLeftPct: 0, hiZoneWidthPct: 0,
      loMarkPct: null, loLabel: '', hiMarkPct: null, hiLabel: '', needlePct: clampPct(w, lo, hi),
    };
  }

  const rpm = seg.rpm!;
  const lo = seg.allOut ? rpm - 15 : rpm - 10;
  const hi = seg.allOut ? rpm + 25 : rpm + 10;
  const zl = seg.allOut ? clampPct(rpm, lo, hi) : clampPct(rpm - CORRIDOR_RPM, lo, hi);
  const zh = seg.allOut ? 100 : clampPct(rpm + CORRIDOR_RPM, lo, hi);
  return {
    off: false, ramp: false, zoneLeftPct: zl, zoneWidthPct: zh - zl, hiZoneLeftPct: 0, hiZoneWidthPct: 0,
    loMarkPct: null, loLabel: '', hiMarkPct: null, hiLabel: '', needlePct: clampPct(cad, lo, hi),
  };
}
