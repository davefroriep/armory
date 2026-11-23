import { type RecoveryMode } from './gate';
import type { Program, Segment } from './programs';
import { locate, type Locate } from './timeline';

export type EngineState = 'ready' | 'running' | 'paused' | 'finished';

export interface GateContext {
  hr: number | null;
  hrLimit: number;
  mode: RecoveryMode;
  /** Seconds of "get ready" after the gate opens before the next block starts. */
  lead: number;
  /** Optional minimum sustained ms below `hrLimit` before opening an HR-gated recovery block. */
  debounceMs?: number;
}

/**
 * Wall-clock workout timer. All methods take an explicit `now` (ms) so it is deterministic in tests
 * and stays correct when the browser throttles timers in the background.
 *
 * Gated recovery blocks have no fixed length in `'gated'` mode: the block stays open (up to its
 * ceiling) until the HUD reports that HR has recovered, which "releases" it. In `'timed'` mode,
 * blocks stay at their exact protocol duration without stretching or extra lead-time overhang.
 */
export class WorkoutEngine {
  state: EngineState = 'ready';
  private recoveryMode: RecoveryMode = 'gated';
  private accMs = 0;
  private resumedAt = 0;
  /** segment index -> real length (s) once a gated / skipped block has ended. */
  private released = new Map<number, number>();
  private gateLowSince: number | null = null;
  private gateLowIdx = -1;

  constructor(public program: Program) {}

  setProgram(p: Program): void {
    this.program = p;
  }

  setRecoveryMode(mode: RecoveryMode): void {
    this.recoveryMode = mode;
  }

  start(now: number): void {
    this.accMs = 0;
    this.resumedAt = now;
    this.released.clear();
    this.gateLowSince = null;
    this.gateLowIdx = -1;
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

  /** Serializes released/skipped segment lengths for crash-resume persistence. */
  exportReleased(): Record<number, number> {
    const out: Record<number, number> = {};
    for (const [k, v] of this.released.entries()) out[k] = v;
    return out;
  }

  /** Hydrates the engine in paused state from previously recorded elapsed seconds (crash resume). */
  hydratePaused(elapsedSec: number, released?: Record<number, number>): void {
    this.accMs = Math.max(0, elapsedSec * 1000);
    this.resumedAt = 0;
    this.released.clear();
    this.gateLowSince = null;
    this.gateLowIdx = -1;
    if (released) {
      for (const [k, v] of Object.entries(released)) {
        const idx = Number(k);
        if (Number.isInteger(idx) && idx >= 0 && typeof v === 'number' && Number.isFinite(v) && v >= 0) {
          this.released.set(idx, v);
        }
      }
    }
    this.state = 'paused';
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
      else if (g.gate && this.recoveryMode === 'gated' && elapsed >= start) {
        sec = Math.max(g.seconds, g.gate.maxSec + 2); // +2 s slack: the ceiling check runs before the block can roll over
      }
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
    if (this.recoveryMode === 'timed') return false;
    const loc = this.locate(now);
    return !loc.complete && !!loc.seg?.gate && !this.released.has(loc.index);
  }

  /** Nominal seconds left in the workout (open gate counts only its remaining nominal time). */
  remainingNominal(now: number): number {
    const el = this.elapsed(now);
    const loc = locate(this.segments(el), el);
    if (loc.complete) return 0;
    const base = this.program.segments;
    let rest = loc.seg?.gate && this.recoveryMode === 'gated' && !this.released.has(loc.index)
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
    const nominal = this.program.segments[loc.index].seconds;
    const lead = Math.max(0, ctx.lead);

    if (ctx.mode === 'timed' || ctx.hr == null) {
      if (loc.index === this.gateLowIdx) {
        this.gateLowSince = null;
        this.gateLowIdx = -1;
      }
      if (loc.into < nominal) return null;
      this.released.set(loc.index, loc.into <= nominal + 0.5 ? nominal : loc.into);
      return loc.index;
    }

    // Track HR-below-target stability even before `gate.minSec` so hitting HR early finishes right on `minSec`.
    if (ctx.hr <= ctx.hrLimit) {
      if (loc.index !== this.gateLowIdx) {
        this.gateLowIdx = loc.index;
        this.gateLowSince = now;
      } else {
        this.gateLowSince ??= now;
      }
    } else if (loc.index === this.gateLowIdx) {
      this.gateLowSince = null;
    }

    const debounceMs = ctx.debounceMs ?? 0;
    const recovered = ctx.hr <= ctx.hrLimit
      && (debounceMs <= 0 || (this.gateLowIdx === loc.index && this.gateLowSince != null && now - this.gateLowSince >= debounceMs));

    if (loc.into >= gate.maxSec) {
      this.gateLowSince = null;
      this.gateLowIdx = -1;
      this.released.set(loc.index, loc.into + lead);
      return loc.index;
    }

    if (loc.into < gate.minSec) {
      if (recovered && loc.into >= Math.max(0, gate.minSec - lead)) {
        this.gateLowSince = null;
        this.gateLowIdx = -1;
        this.released.set(loc.index, gate.minSec);
        return loc.index;
      }
      return null;
    }

    if (!recovered) return null;
    this.gateLowSince = null;
    this.gateLowIdx = -1;
    this.released.set(loc.index, loc.into + lead);
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
