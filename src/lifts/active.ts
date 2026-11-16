import { audio, buzz } from '../audio/audio';
import { Emitter } from '../emitter';
import { hrFresh, live } from '../ble/telemetry';
import { isNative, workoutService } from '../native/service';
import { getMeta, getSamples, putMeta } from '../store/db';
import { getSettings } from '../store/settings';
import { discard as discardWorkout, syncWorkout } from '../workout/sync';
import { Recorder } from '../workout/recorder';
import type { WorkoutMeta } from '../types';
import { hasLogged, midSuperset, pruneUnlogged } from './logic';
import { liftSettings } from './settings';
import { lifts } from './store';
import type { LiftSession, SessionEx } from './types';
import { uid } from './types';

export interface RestState {
  endMs: number;
  totalSec: number;
  label: string;
  fired: boolean;
}

const mmss = (sec: number): string => {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/**
 * Owns the one in-progress workout: heart-rate recording, the elapsed clock, the rest timer, wake lock and
 * the lock-screen notification. It lives outside any screen so you can wander to history or an exercise's
 * records mid-workout and the session keeps running.
 */
class ActiveWorkout {
  rec: Recorder | null = null;
  rest: RestState | null = null;
  /** ~4 Hz while a workout is running (clock, rest countdown). */
  readonly tick = new Emitter();
  /** Rest timer started / adjusted / cleared. */
  readonly restChanged = new Emitter();

  private timer: ReturnType<typeof setInterval> | null = null;
  private offRec: (() => void) | null = null;
  private lock: WakeLockSentinel | null = null;
  private lastSvc = 0;
  private booting: Promise<void> | null = null;

  get session(): LiftSession | undefined {
    return lifts.active;
  }

  elapsedSec(): number {
    const s = this.session;
    return s ? Math.max(0, (Date.now() - s.startMs) / 1000) : 0;
  }

  /** Call once at app start / when entering the lift tracker: resumes a workout the OS killed. */
  init(): Promise<void> {
    this.booting ??= (async () => {
      await lifts.ready;
      const s = lifts.active;
      if (s && !this.timer) await this.attach(s, true);
    })();
    return this.booting;
  }

  async start(o: { name: string; routineId: string | null; eventId: number | null; exs: SessionEx[] }): Promise<LiftSession> {
    await this.init();
    const existing = this.session;
    if (existing) return existing;
    const s: LiftSession = {
      id: uid(), name: o.name, routineId: o.routineId, eventId: o.eventId, startMs: Date.now(), endMs: null,
      status: 'active', exs: o.exs, rpe: null, bwKg: getSettings().weightKg,
    };
    lifts.saveSession(s, true);
    await this.attach(s, false);
    return s;
  }

  private async attach(s: LiftSession, resume: boolean): Promise<void> {
    const hrMax = getSettings().hrMax;
    let rec: Recorder | null = null;
    if (resume) {
      const meta = await getMeta(s.id);
      if (meta) rec = new Recorder(s.id, 'strength', 'strength', s.name, s.eventId, hrMax, { meta, samples: await getSamples(s.id) });
    }
    if (!rec) {
      rec = new Recorder(s.id, 'strength', 'strength', s.name, s.eventId, hrMax);
      rec.meta.startEpochMs = s.startMs;
      void putMeta(rec.meta);
    }
    this.rec = rec;
    // Beat-driven so heart rate keeps logging while Android throttles this page's timers in the background.
    this.offRec = rec.attach(() => ({ elapsed: this.elapsedSec(), targetRpm: 0 }));
    this.timer = setInterval(() => this.onTick(), 250);
    if (!isNative()) this.requestLock();
    void workoutService.start('Armory Fit · Strength', 'Lifting…');
    document.addEventListener('visibilitychange', this.onVis);
    this.tick.emit();
  }

  private onVis = (): void => {
    if (!document.hidden && this.session && !isNative()) this.requestLock();
  };

  private requestLock(): void {
    void navigator.wakeLock?.request('screen').then((l) => { this.lock = l; }).catch(() => {});
  }

  private onTick(): void {
    const s = this.session;
    if (!s) { this.teardown(); return; }
    const el = this.elapsedSec();
    this.rec?.tick(el, 0);
    const r = this.rest;
    if (r && !r.fired && Date.now() >= r.endMs) {
      r.fired = true;
      if (liftSettings().restSound) { audio.unlock(); audio.chime(); }
      buzz([220, 90, 220]);
    }
    if (Date.now() - this.lastSvc > 5000) {
      this.lastSvc = Date.now();
      const hr = hrFresh() ? live.hr : 0;
      const txt = r && !r.fired ? `Rest ${mmss((r.endMs - Date.now()) / 1000)} · ${mmss(el)}` : `${mmss(el)} · ${hr || '--'} BPM`;
      void workoutService.update(`Armory Fit · ${s.name}`, txt);
    }
    this.tick.emit();
  }

  // ---------- rest timer ----------
  /** Rest length for an exercise: this-session override, then the exercise's own, then the default. */
  restSecFor(se: SessionEx): number {
    return se.rest ?? lifts.row(se.exId)?.restSec ?? liftSettings().restSec;
  }

  startRest(sec: number, label: string): void {
    this.rest = { endMs: Date.now() + sec * 1000, totalSec: sec, label, fired: false };
    this.restChanged.emit();
  }

  adjustRest(deltaSec: number): void {
    const r = this.rest;
    if (!r) return;
    r.endMs += deltaSec * 1000;
    r.totalSec = Math.max(5, r.totalSec + deltaSec);
    r.fired = Date.now() >= r.endMs;
    this.restChanged.emit();
  }

  clearRest(): void {
    this.rest = null;
    this.restChanged.emit();
  }

  /** A set was just logged: start the rest timer unless this is mid-superset or auto-rest is off. */
  afterSetLogged(s: LiftSession, i: number): void {
    audio.unlock();
    if (midSuperset(s.exs, i)) {
      if (this.rest) this.clearRest();
      return;
    }
    if (!liftSettings().restAuto) {
      if (this.rest?.fired) this.clearRest();
      return;
    }
    const e = s.exs[i];
    this.startRest(this.restSecFor(e), lifts.exOrStub(e.exId).name);
  }

  // ---------- end ----------
  /** Finish: drop unlogged sets, freeze the HR recording, save, and kick off the Intervals.icu upload. */
  async finish(): Promise<{ session: LiftSession; meta: WorkoutMeta; sync: Promise<WorkoutMeta> } | null> {
    const s = this.session;
    if (!s) return null;
    if (!s.exs.some(hasLogged)) {
      await this.discard();
      return null;
    }
    pruneUnlogged(s);
    s.endMs = Date.now();
    s.status = 'done';
    const rec = this.rec;
    const el = (s.endMs - s.startMs) / 1000;
    rec?.tick(el, 0, s.endMs);
    const meta = rec ? rec.finalize() : ((await getMeta(s.id)) as WorkoutMeta);
    meta.name = s.name;
    meta.status = 'saved';
    meta.upload = 'pending';
    meta.rpe = null;
    meta.patched = false;
    await putMeta(meta);
    lifts.saveSession(s, true);
    this.teardown();
    const sync = syncWorkout(meta);
    return { session: s, meta, sync };
  }

  async discard(): Promise<void> {
    const s = this.session;
    if (!s) return;
    const id = s.id;
    this.teardown();
    await lifts.deleteSession(id);
    await discardWorkout(id);
  }

  private teardown(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.offRec?.();
    this.offRec = null;
    this.rec = null;
    this.rest = null;
    document.removeEventListener('visibilitychange', this.onVis);
    void this.lock?.release().catch(() => {});
    this.lock = null;
    void workoutService.stop();
    this.restChanged.emit();
    this.tick.emit();
  }
}

export const active = new ActiveWorkout();
