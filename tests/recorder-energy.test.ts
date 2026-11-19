import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/store/db', () => ({ putMeta: vi.fn(), putSample: vi.fn() }));

import { ageOn, bodyProfile, kcalFromSamples, keytelKcalPerMin } from '../src/domain/energy';
import { buildTcx } from '../src/domain/tcx';
import { clearHrLog, hrAtTime, live, pushBike, pushHr, resetSpinOdometer } from '../src/ble/telemetry';
import { Recorder } from '../src/workout/recorder';

const T0 = 1_800_000_000_000;

describe('timestamped heart-rate log', () => {
  beforeEach(clearHrLog);

  it('returns the nearest beat and 0 when no beat is close enough', () => {
    pushHr(100, T0);
    pushHr(110, T0 + 1000);
    pushHr(120, T0 + 2000);
    expect(hrAtTime(T0 + 1100)).toBe(110);
    expect(hrAtTime(T0 + 1600)).toBe(120);
    expect(hrAtTime(T0 + 2000 + 3000)).toBe(120); // exactly at the match window edge
    expect(hrAtTime(T0 + 2000 + 3001)).toBe(0); // sensor silent: a gap, not a guess
    expect(hrAtTime(T0 - 5000)).toBe(0);
  });

  it('treats a 0 bpm beat (lost contact) as a gap', () => {
    pushHr(0, T0);
    expect(hrAtTime(T0)).toBe(0);
  });
});

describe('Recorder back-fill after a stalled UI timer', () => {
  beforeEach(clearHrLog);

  it('fills a late tick with the real per-second beats instead of a flat plateau', () => {
    // 2 minutes of live beats arrive while the page timer is throttled (no tick calls)
    for (let i = 0; i <= 120; i++) pushHr(100 + (i % 40), T0 + i * 1000);
    const rec = new Recorder('t1', 'strength', 'strength', 'Strength Train', null, 180);
    rec.tick(120, 0, T0 + 120_000); // one late tick catches up
    expect(rec.samples).toHaveLength(121);
    expect(rec.samples.map((s) => s.hr)).toEqual(Array.from({ length: 121 }, (_, i) => 100 + (i % 40)));
    expect(new Set(rec.samples.map((s) => s.hr)).size).toBe(40);
  });

  it('records 0 (not the latest value) for a stretch where the sensor sent nothing', () => {
    for (let i = 0; i <= 20; i++) pushHr(120, T0 + i * 1000);
    for (let i = 60; i <= 80; i++) pushHr(140, T0 + i * 1000); // 40 s silent in between
    const rec = new Recorder('t2', 'strength', 'strength', 'Strength Train', null, 180);
    rec.tick(80, 0, T0 + 80_000);
    const hr = rec.samples.map((s) => s.hr);
    expect(hr[10]).toBe(120);
    expect(hr[40]).toBe(0);
    expect(hr[70]).toBe(140);
    expect(rec.meta.maxHr).toBe(140);
    // average only over seconds that had a reading
    const real = hr.filter((x) => x > 0);
    expect(rec.avgHr).toBeCloseTo(real.reduce((a, b) => a + b, 0) / real.length);
  });

  it('attach(): every heart-rate beat advances the recording without any timer', () => {
    const rec = new Recorder('t3', 'strength', 'strength', 'Strength Train', null, 180);
    let elapsed = 0;
    let running = true;
    const off = rec.attach(() => (running ? { elapsed, targetRpm: 0 } : null));
    const now = Date.now();
    for (let i = 0; i < 5; i++) {
      elapsed = i;
      pushHr(90 + i, now - (4 - i) * 1000 + 0);
    }
    expect(rec.samples.length).toBe(5);
    running = false;
    elapsed = 99;
    pushHr(95);
    expect(rec.samples.length).toBe(5); // paused / finished: ignored
    off();
    running = true;
    pushHr(96);
    expect(rec.samples.length).toBe(5); // detached
  });
});

describe('HR-based calories (Keytel et al. 2005)', () => {
  it('matches the published male / female equations', () => {
    // male: (-55.0969 + 0.6309*109 + 0.1988*89.7 + 0.2017*45) / 4.184 = 9.70 kcal/min
    expect(keytelKcalPerMin(109, { sex: 'M', weightKg: 89.7, ageYears: 45 })).toBeCloseTo(9.7, 1);
    // female: (-20.4022 + 0.4472*130 - 0.1263*65 + 0.074*30) / 4.184 = 7.59 kcal/min
    expect(keytelKcalPerMin(130, { sex: 'F', weightKg: 65, ageYears: 30 })).toBeCloseTo(7.59, 1);
    expect(keytelKcalPerMin(30, { sex: 'F', weightKg: 150, ageYears: 20 })).toBe(0); // never negative
  });

  it('sums per second, skipping sensor gaps', () => {
    const p = { sex: 'M' as const, weightKg: 90, ageYears: 45 };
    const per = keytelKcalPerMin(110, p);
    const samples = [...Array(600).fill({ hr: 110 }), ...Array(600).fill({ hr: 0 })];
    expect(kcalFromSamples(samples, p)).toBe(Math.round(per * 10));
  });

  it('computes age from an ISO birth date and builds a profile only when complete', () => {
    expect(ageOn('1981-10-25', new Date(2026, 10, 15))).toBe(45);
    expect(ageOn('1981-10-25', new Date(2026, 9, 24))).toBe(44);
    expect(ageOn(null)).toBeNull();
    expect(ageOn('garbage')).toBeNull();
    expect(bodyProfile({ sex: 'M', weightKg: null, birthDate: '1981-10-25' })).toBeNull();
    expect(bodyProfile({ sex: 'M', weightKg: 89.7, birthDate: null })).toBeNull();
    expect(bodyProfile({ sex: 'M', weightKg: 89.7, birthDate: '1981-10-25' }, new Date(2026, 10, 15)))
      .toEqual({ sex: 'M', weightKg: 89.7, ageYears: 45 });
  });

  it('writes the estimate into the TCX <Calories> (strength) instead of 0 kJ', () => {
    const samples = [{ t: 0, w: 0, cad: 0, hr: 110, dist: 0 }];
    const hrTcx = buildTcx({ startEpochMs: T0, seconds: 1, kj: 0, kcal: 412, samples, kind: 'hr' }, 'Strength Train');
    expect(hrTcx).toContain('<Calories>412</Calories>');
    const bikeTcx = buildTcx({ startEpochMs: T0, seconds: 1, kj: 250.4, samples, kind: 'bike' }, 'Ride');
    expect(bikeTcx).toContain('<Calories>250</Calories>');
  });

  it('integrates virtual distance and 1:1 kJ=kcal odometer for SS2K Spin frames', () => {
    resetSpinOdometer();
    pushBike({ dev: 'spin', w: 200, cad: 85, dist: 0, kcal: 0, spd: 0, tgt_w: 200, mode: 1 }, T0);
    pushBike({ dev: 'spin', w: 200, cad: 85, dist: 0, kcal: 0, spd: 0, tgt_w: 200, mode: 1 }, T0 + 1000);
    expect(live.dev).toBe('spin');
    expect(live.spd).toBeGreaterThan(25);
    expect(live.dist).toBeGreaterThan(7);
    expect(live.kcal).toBeCloseTo(0.2, 2); // 200 W * 1 s = 0.2 kJ = 0.2 kcal
  });

  it('integrates Spin virtual odometer even when spd carries stepper Hz and ignores >2s idle gaps', () => {
    resetSpinOdometer();
    pushBike({ dev: 'spin', w: 200, cad: 85, dist: 0, kcal: 0, spd: 3000, tgt_w: 200, mode: 1 }, T0);
    pushBike({ dev: 'spin', w: 200, cad: 85, dist: 0, kcal: 0, spd: 3000, tgt_w: 200, mode: 1 }, T0 + 1000);
    expect(live.spd).toBeGreaterThan(25);
    expect(live.spd).toBeLessThan(60);
    expect(live.dist).toBeGreaterThan(7);
    expect(live.kcal).toBeCloseTo(0.2, 2);
    const distAfter1s = live.dist;
    const kcalAfter1s = live.kcal;
    // >2s gap must not add phantom distance or kcal
    pushBike({ dev: 'spin', w: 200, cad: 85, dist: 0, kcal: 0, spd: 3000, tgt_w: 200, mode: 1 }, T0 + 10_000);
    expect(live.dist).toBe(distAfter1s);
    expect(live.kcal).toBe(kcalAfter1s);
  });
});
