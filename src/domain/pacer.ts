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

  const inLead = loc.next !== null && loc.remaining <= cfg.lead;
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
