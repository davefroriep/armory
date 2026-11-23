import { bikeFrame, bikeFresh, hrAtTime, hrBeat, live } from '../ble/telemetry';
import { putMeta, putSample, putSamples, replaceSamples } from '../store/db';
import type { Equipment, Sample, WorkoutMeta } from '../types';
import { Z5_HR_FRAC } from '../ui/hr';

/**
 * Compacts a 1 Hz sample stream down to `targetSec` seconds by dropping longest idle gaps
 * (`hr <= 30 && w === 0`, preferring later gaps on ties) before truncating trailing active samples,
 * then re-indexing `t = 0..n-1`.
 */
export function compactSamplesToDuration(samples: Sample[], targetSec: number): Sample[] {
  const keep = Math.max(0, Math.floor(targetSec));
  if (samples.length <= keep) return samples;
  if (keep === 0) return [];
  const runs: { start: number; len: number }[] = [];
  for (let i = 0; i < samples.length; ) {
    if (samples[i].hr <= 30 && samples[i].w === 0) {
      let j = i + 1;
      while (j < samples.length && samples[j].hr <= 30 && samples[j].w === 0) j++;
      runs.push({ start: i, len: j - i });
      i = j;
    } else {
      i++;
    }
  }
  runs.sort((a, b) => b.len - a.len || b.start - a.start);
  const drop = new Uint8Array(samples.length);
  let toDrop = samples.length - keep;
  for (const run of runs) {
    if (toDrop <= 0) break;
    const take = Math.min(toDrop, run.len);
    for (let i = run.start + run.len - take; i < run.start + run.len; i++) drop[i] = 1;
    toDrop -= take;
  }
  const kept = samples.filter((_, i) => !drop[i]);
  const trimmed = kept.length > keep ? kept.slice(0, keep) : kept;
  return trimmed.map((s, idx) => (s.t === idx ? s : { ...s, t: idx }));
}

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
  private accDist = 0;
  private accKcal = 0;
  private lastRawDist: number | null = null;
  private lastRawKcal: number | null = null;
  private kjAcc = 0;
  private wTotal = 0;
  private hrTotal = 0;
  private hrCount = 0;
  /** Seconds at or above 90% HRmax (the "Z5 bank", aligned with Intervals.icu Z5 / T@VO2max). */
  z5Seconds = 0;
  lastDist = 0;
  lastKcal = 0;

  constructor(
    id: string, equipment: Equipment, programId: string, name: string, eventId: number | null,
    private readonly hrMax: number,
    /** Continue a recording that was interrupted (app killed mid-workout) instead of starting a new one. */
    resumeFrom?: { meta: WorkoutMeta; samples: Sample[] },
    planSlot: string | null = null,
    private readonly persist = true,
  ) {
    if (resumeFrom) {
      this.meta = { ...resumeFrom.meta };
      this.samples.push(...resumeFrom.samples);
      this.recomputeFromSamples();
      return;
    }
    this.meta = {
      id, equipment, programId, name, startEpochMs: Date.now(), status: 'active', seconds: 0, kj: 0,
      avgW: 0, maxW: 0, avgHr: 0, maxHr: 0, rpe: null, eventId,
      ...(planSlot ? { planSlot } : {}),
      upload: 'none', patched: false,
      activityId: null, lastError: null,
    };
    if (this.persist) void putMeta(this.meta);
  }

  get kj(): number { return this.kjAcc; }
  get avgW(): number { return this.nextT ? this.wTotal / this.nextT : 0; }
  get avgHr(): number { return this.hrCount ? this.hrTotal / this.hrCount : 0; }

  private accumulateBikeTotals(): void {
    if (this.lastRawDist == null || this.lastRawKcal == null) {
      this.lastRawDist = live.dist;
      this.lastRawKcal = live.kcal;
      return;
    }
    const dDist = live.dist >= this.lastRawDist ? live.dist - this.lastRawDist : live.dist;
    const dKcal = live.kcal >= this.lastRawKcal ? live.kcal - this.lastRawKcal : live.kcal;
    if (dDist > 0) this.accDist += dDist;
    if (dKcal > 0) this.accKcal += dKcal;
    this.lastRawDist = live.dist;
    this.lastRawKcal = live.kcal;
  }

  /** Call for every bike frame while running. */
  onFrame(): void {
    this.accumulateBikeTotals();
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

  private recomputeFromSamples(): void {
    this.wTotal = 0;
    this.kjAcc = 0;
    this.hrTotal = 0;
    this.hrCount = 0;
    this.z5Seconds = 0;
    this.meta.maxW = 0;
    this.meta.maxHr = 0;
    for (const s of this.samples) {
      this.wTotal += s.w;
      this.kjAcc += s.w / 1000;
      this.meta.maxW = Math.max(this.meta.maxW, s.w);
      if (s.hr > 0) {
        this.hrTotal += s.hr;
        this.hrCount++;
        this.meta.maxHr = Math.max(this.meta.maxHr, s.hr);
        if (s.hr >= Z5_HR_FRAC * this.hrMax) this.z5Seconds++;
      }
    }
    const tail = this.samples[this.samples.length - 1];
    this.accDist = tail ? tail.dist : 0;
    this.accKcal = tail ? tail.kcal : 0;
    this.lastDist = this.accDist;
    this.lastKcal = this.accKcal;
    this.nextT = this.samples.length;
  }

  /**
   * Compacts recorded samples down to `targetSec` (dropping longest idle gaps first)
   * and recalculates live stats/aggregates.
   */
  trimTo(targetSec: number): boolean {
    const next = compactSamplesToDuration(this.samples, targetSec);
    if (next.length === this.samples.length) return false;
    this.samples.length = 0;
    this.samples.push(...next);
    this.recomputeFromSamples();
    this.meta.seconds = this.samples.length;
    if (this.persist) {
      void replaceSamples(this.meta.id, this.samples);
      void putMeta(this.meta, false);
    }
    return true;
  }

  /**
   * Fills every second up to `elapsed`. Heart rate comes from the beat nearest to that second's wall
   * time, so a late call back-fills real data, and a stretch with no beats records 0 (a gap) rather than
   * smearing the current value backwards.
   */
  tick(elapsed: number, targetRpm: number, now = Date.now()): void {
    const bike = bikeFresh();
    if (bike) this.accumulateBikeTotals();
    const batch: Sample[] = [];
    while (this.nextT <= Math.floor(elapsed)) {
      const w = this.frames && bike ? this.wSum / this.frames : bike ? live.w : 0;
      const cad = this.frames && bike ? this.cadSum / this.frames : bike ? live.cad : 0;
      const hr = hrAtTime(now - (elapsed - this.nextT) * 1000);
      const dist = this.accDist;
      const kcal = this.accKcal;
      this.lastDist = dist;
      this.lastKcal = kcal;
      const s: Sample = { t: this.nextT, w, cad, hr, dist, kcal, tr: targetRpm };
      this.samples.push(s);
      batch.push(s);

      this.kjAcc += w / 1000;
      this.wTotal += w;
      this.meta.maxW = Math.max(this.meta.maxW, w);
      if (hr > 0) {
        this.hrTotal += hr;
        this.hrCount++;
        this.meta.maxHr = Math.max(this.meta.maxHr, hr);
        if (hr >= Z5_HR_FRAC * this.hrMax) this.z5Seconds++;
      }
      this.wSum = this.cadSum = 0;
      this.frames = 0;
      this.nextT++;
    }
    if (this.persist) {
      if (batch.length === 1) void putSample(this.meta.id, batch[0]);
      else if (batch.length > 1) void putSamples(this.meta.id, batch);
    }
  }

  /** Freeze final stats into the meta record. */
  finalize(): WorkoutMeta {
    this.meta.status = 'finished';
    this.meta.seconds = this.samples.length;
    this.meta.kj = this.kjAcc;
    this.meta.avgW = this.avgW;
    this.meta.avgHr = this.avgHr;
    if (this.persist) void putMeta(this.meta);
    return this.meta;
  }
}
