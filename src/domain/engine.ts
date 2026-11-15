import { gateOpen, type RecoveryMode } from './gate';
import type { Program, Segment } from './programs';
import { locate, type Locate } from './timeline';

export type EngineState = 'ready' | 'running' | 'paused' | 'finished';

export interface GateContext {
  hr: number | null;
  hrLimit: number;
  mode: RecoveryMode;
  /** Seconds of "get ready" after the gate opens before the next block starts. */
  lead: number;
}

/**
 * Wall-clock workout timer. All methods take an explicit `now` (ms) so it is deterministic in tests
 * and stays correct when the browser throttles timers in the background.
 *
 * Gated recovery blocks have no fixed length: the block stays open (up to its ceiling) until the HUD
 * reports that HR has recovered, which "releases" it. Released blocks keep their real, shorter or
 * longer, length so the rest of the timeline simply shifts.
 */
export class WorkoutEngine {
  state: EngineState = 'ready';
  private accMs = 0;
  private resumedAt = 0;
  /** segment index -> real length (s) once a gated / skipped block has ended. */
  private released = new Map<number, number>();

  constructor(public program: Program) {}

  setProgram(p: Program): void {
    this.program = p;
  }

  start(now: number): void {
    this.accMs = 0;
    this.resumedAt = now;
    this.released.clear();
    this.state = 'running';
  }

  pause(now: number): void {
    if (this.state !== 'running') return;
    this.accMs += now - this.resumedAt;
    this.state = 'paused';
  }

  resume(now: number): void {
    if (this.state !== 'paused') return;
    this.resumedAt = now;
    this.state = 'running';
  }

  finish(now: number): void {
    this.pause(now);
    this.state = 'finished';
  }

  /** Active (non-paused) elapsed seconds. */
  elapsed(now: number): number {
    const ms = this.state === 'running' ? this.accMs + (now - this.resumedAt) : this.accMs;
    return Math.max(0, ms / 1000);
  }

  /**
   * The timeline as it really is right now: released blocks at their real length, the gated block
   * currently being waited on open up to its ceiling, everything else at nominal length.
   */
  segments(elapsed: number): Segment[] {
    const out: Segment[] = [];
    let start = 0;
    this.program.segments.forEach((g, i) => {
      let sec = g.seconds;
      const rel = this.released.get(i);
      if (rel !== undefined) sec = rel;
      else if (g.gate && elapsed >= start) sec = Math.max(g.seconds, g.gate.maxSec + 2); // +2 s slack: the ceiling check runs before the block can roll over
      out.push(sec === g.seconds ? g : { ...g, seconds: sec });
      start += sec;
    });
    return out;
  }

  locate(now: number): Locate {
    const el = this.elapsed(now);
    return locate(this.segments(el), el);
  }

  /** True while the current block is a gated recovery that has not been released yet. */
  waitingOnGate(now: number): boolean {
    const loc = this.locate(now);
    return !loc.complete && !!loc.seg?.gate && !this.released.has(loc.index);
  }

  /** Nominal seconds left in the workout (open gate counts only its remaining nominal time). */
  remainingNominal(now: number): number {
    const el = this.elapsed(now);
    const loc = locate(this.segments(el), el);
    if (loc.complete) return 0;
    const base = this.program.segments;
    let rest = loc.seg?.gate && !this.released.has(loc.index)
      ? Math.max(0, base[loc.index].seconds - loc.into)
      : loc.remaining;
    for (let i = loc.index + 1; i < base.length; i++) rest += base[i].seconds;
    return rest;
  }

  /**
   * Called by the HUD every tick. Releases the open gated block when HR has recovered (or the floor /
   * ceiling rules say so). Returns the released segment index, or null.
   */
  checkGate(now: number, ctx: GateContext): number | null {
    if (this.state !== 'running') return null;
    const el = this.elapsed(now);
    const loc = locate(this.segments(el), el);
    const gate = loc.seg?.gate;
    if (loc.complete || !gate || this.released.has(loc.index)) return null;
    const open = gateOpen({
      gate, nominal: this.program.segments[loc.index].seconds, into: loc.into, hr: ctx.hr, hrLimit: ctx.hrLimit, mode: ctx.mode,
    });
    if (!open) return null;
    this.released.set(loc.index, loc.into + Math.max(0, ctx.lead));
    return loc.index;
  }

  /** Ends the current block right now (manual "skip rest" / "skip warm-up"). */
  skip(now: number): boolean {
    if (this.state !== 'running') return false;
    const el = this.elapsed(now);
    const loc = locate(this.segments(el), el);
    if (loc.complete || loc.index < 0) return false;
    this.released.set(loc.index, loc.into);
    return true;
  }
}
