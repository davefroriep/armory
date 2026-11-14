import { describe, expect, it } from 'vitest';
import { WindowAvg } from '../src/domain/smooth';
import { RPE_STEPS, rpeLabel } from '../src/domain/rpe';
import { buildTcx } from '../src/domain/tcx';

describe('3 s smoothing', () => {
  it('averages the trailing window and drops old samples', () => {
    const a = new WindowAvg(3000);
    a.push(0, 50);
    a.push(1000, 56);
    a.push(2000, 53);
    expect(a.value(2000)).toBeCloseTo(53);
    expect(a.value(3500)).toBeCloseTo(54.5); // t=0 sample fell out of the window
    expect(a.value(10000)).toBeNull(); // sensor went silent
  });
  it('a single hard pedal stroke barely moves the smoothed number', () => {
    const a = new WindowAvg(3000);
    for (let t = 0; t < 3000; t += 200) a.push(t, 55);
    a.push(3000, 62);
    expect(Math.round(a.value(3000)!)).toBe(55); // +7 RPM spike moves the average by ~0.4
  });
});

describe('effort scale', () => {
  it('has 8 plain-language steps mapping to RPE 3-10', () => {
    expect(RPE_STEPS.map((s) => s.value)).toEqual([3, 4, 5, 6, 7, 8, 9, 10]);
    expect(RPE_STEPS.map((s) => s.label)).toEqual([
      'Very easy', 'Easy', 'Sort of easy', 'Medium effort', 'Sort of hard', 'Hard', 'Very hard', 'Extreme',
    ]);
    expect(rpeLabel(6)).toBe('Medium effort');
  });
});

describe('HR-only TCX (strength)', () => {
  it('has no power, cadence or distance', () => {
    const xml = buildTcx(
      { startEpochMs: Date.UTC(2026, 10, 14, 20), seconds: 2, kj: 0, kind: 'hr', samples: [
        { t: 0, w: 0, cad: 0, hr: 110, dist: 0 }, { t: 1, w: 0, cad: 0, hr: 112, dist: 0 }] },
      'Strength Train',
    );
    expect(xml).toContain('Sport="Other"');
    expect(xml).not.toContain('Watts');
    expect(xml).not.toContain('Cadence');
    expect(xml).not.toContain('DistanceMeters');
    expect(xml.match(/<HeartRateBpm>/g)!.length).toBe(2);
  });
});
