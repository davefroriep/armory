import { bikeFrame, bikeFresh, hrAtTime, hrBeat, live } from '../ble/telemetry';
import { putMeta, putSample } from '../store/db';
import type { Equipment, Sample, WorkoutMeta } from '../types';

/**
 * 1 Hz session recorder. Accumulates the 5 Hz bike frames and records the per-second mean (so sprint
 * peaks and kJ are faithful), persists each second to IndexedDB for crash recovery, and tracks the
 * stats shown on the summary screen.
 */
export class Recorder {
  readonly meta: WorkoutMeta;
  readonly samples: Sample[] = [];

  private nextT = 0;
  private wSum = 0;
  private cadSum = 0;
  private frames = 0;
  private base: { dist: number; kcal: number } | null = null;
  private kjAcc = 0;
  private wTotal = 0;
  private hrTotal = 0;
  private hrCount = 0;
  /** Seconds at or above 88% HRmax (the "Z5 bank"). */
  z5Seconds = 0;
  lastDist = 0;
  lastKcal = 0;

  constructor(
    id: string, equipment: Equipment, programId: string, name: string, eventId: number | null,
    private readonly hrMax: number,
    /** Continue a recording that was interrupted (app killed mid-workout) instead of starting a new one. */
    resumeFrom?: { meta: WorkoutMeta; samples: Sample[] },
    planSlot: string | null = null,
  ) {
    if (resumeFrom) {
      this.meta = { ...resumeFrom.meta };
      for (const s of resumeFrom.samples) {
        this.samples.push(s);
        this.wTotal += s.w;
        this.kjAcc += s.w / 1000;
        this.meta.maxW = Math.max(this.meta.maxW, s.w);
        if (s.hr > 0) {
          this.hrTotal += s.hr;
          this.hrCount++;
          this.meta.maxHr = Math.max(this.meta.maxHr, s.hr);
          if (s.hr >= 0.88 * hrMax) this.z5Seconds++;
        }
      }
      this.nextT = resumeFrom.samples.length;
      return;
    }
    this.meta = {
      id, equipment, programId, name, startEpochMs: Date.now(), status: 'active', seconds: 0, kj: 0,
      avgW: 0, maxW: 0, avgHr: 0, maxHr: 0, rpe: null, eventId,
      ...(planSlot ? { planSlot } : {}),
      upload: 'none', patched: false,
      activityId: null, lastError: null,
    };
    void putMeta(this.meta);
  }

  get kj(): number { return this.kjAcc; }
  get avgW(): number { return this.nextT ? this.wTotal / this.nextT : 0; }
  get avgHr(): number { return this.hrCount ? this.hrTotal / this.hrCount : 0; }

  /** Call for every bike frame while running. */
  onFrame(): void {
    if (!this.base) this.base = { dist: live.dist, kcal: live.kcal };
    this.wSum += live.w;
    this.cadSum += live.cad;
    this.frames++;
  }

  /**
   * Drive the recorder from the sensors instead of a UI timer. Android throttles WebView timers once the
   * app is in the background (switching to Boostcamp, screen off), but BLE callbacks keep arriving, so
   * every heart-rate beat / bike frame advances the recording. `drive` returns the current active
   * elapsed seconds, or null while not running. Returns an unsubscribe function.
   */
  attach(drive: () => { elapsed: number; targetRpm: number } | null): () => void {
    const run = (): void => {
      const d = drive();
      if (d) this.tick(d.elapsed, d.targetRpm);
    };
    const offs = [hrBeat.on(run), bikeFrame.on(run)];
    return () => offs.forEach((off) => off());
  }

  /**
   * Fills every second up to `elapsed`. Heart rate comes from the beat nearest to that second's wall
   * time, so a late call back-fills real data, and a stretch with no beats records 0 (a gap) rather than
   * smearing the current value backwards.
   */
  tick(elapsed: number, targetRpm: number, now = Date.now()): void {
    const bike = bikeFresh();
    while (this.nextT <= Math.floor(elapsed)) {
      const w = this.frames && bike ? this.wSum / this.frames : bike ? live.w : 0;
      const cad = this.frames && bike ? this.cadSum / this.frames : bike ? live.cad : 0;
      const hr = hrAtTime(now - (elapsed - this.nextT) * 1000);
      const dist = this.base ? Math.max(0, live.dist - this.base.dist) : 0;
      const kcal = this.base ? Math.max(0, live.kcal - this.base.kcal) : 0;
      this.lastDist = dist;
      this.lastKcal = kcal;
      const s: Sample = { t: this.nextT, w, cad, hr, dist, kcal, tr: targetRpm };
      this.samples.push(s);
      void putSample(this.meta.id, s);

      this.kjAcc += w / 1000;
      this.wTotal += w;
      this.meta.maxW = Math.max(this.meta.maxW, w);
      if (hr > 0) {
        this.hrTotal += hr;
        this.hrCount++;
        this.meta.maxHr = Math.max(this.meta.maxHr, hr);
        if (hr >= 0.88 * this.hrMax) this.z5Seconds++;
      }
      this.wSum = this.cadSum = 0;
      this.frames = 0;
      this.nextT++;
    }
  }

  /** Freeze final stats into the meta record. */
  finalize(): WorkoutMeta {
    this.meta.status = 'finished';
    this.meta.seconds = this.samples.length;
    this.meta.kj = this.kjAcc;
    this.meta.avgW = this.avgW;
    this.meta.avgHr = this.avgHr;
    void putMeta(this.meta);
    return this.meta;
  }
}
