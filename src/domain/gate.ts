import type { Gate } from './programs';

export type RecoveryMode = 'gated' | 'timed';

export interface GateInput {
  gate: Gate;
  /** Nominal (timer) length of the recovery block, seconds. */
  nominal: number;
  /** Seconds spent in the block so far. */
  into: number;
  /** Fresh heart rate (bpm) or null when there is no live HR. */
  hr: number | null;
  /** Recovered once HR is at or below this (bpm). */
  hrLimit: number;
  mode: RecoveryMode;
}

/**
 * Should a gated recovery block end now?
 *
 *  - never before the floor (the protocol's own rest, `gate.minSec`);
 *  - always by the ceiling (`gate.maxSec`) so a bad HR strap or a slow day cannot stall the session;
 *  - in between, as soon as HR is back at the recovery target;
 *  - with no live HR, or in "timed" mode, it behaves exactly like the plain timer.
 */
export function gateOpen(i: GateInput): boolean {
  if (i.into >= i.gate.maxSec) return true;
  if (i.into < i.gate.minSec) return false;
  if (i.mode === 'timed' || i.hr == null) return i.into >= i.nominal;
  return i.hr <= i.hrLimit;
}

/** Recovery target in bpm from the % of HRmax setting. */
export const recoveryLimitBpm = (hrMax: number, pct: number): number => Math.round((hrMax * pct) / 100);
