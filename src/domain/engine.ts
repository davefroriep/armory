import type { Program } from './programs';
import { locate, type Locate } from './timeline';

export type EngineState = 'ready' | 'running' | 'paused' | 'finished';

/**
 * Wall-clock workout timer. All methods take an explicit `now` (ms) so it is deterministic in tests
 * and stays correct when the browser throttles timers in the background.
 */
export class WorkoutEngine {
  state: EngineState = 'ready';
  private accMs = 0;
  private resumedAt = 0;

  constructor(public program: Program) {}

  setProgram(p: Program): void {
    this.program = p;
  }

  start(now: number): void {
    this.accMs = 0;
    this.resumedAt = now;
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

  locate(now: number): Locate {
    return locate(this.program.segments, this.elapsed(now));
  }
}
