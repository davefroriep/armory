import { describe, expect, it } from 'vitest';
import {
  applyClipMarker,
  computeScrubWindow,
  fmtRelativeToCheck,
  pickNearestBurstIndex,
  shortCamLabel,
} from '../src/lifts/tape';

describe('tape linking & scrubbing helpers', () => {
  it('picks the most recent burst ending before or near checkedMs (ignoring buddy sets later)', () => {
    const bursts = [
      { startMs: 100_000, endMs: 140_000 },
      { startMs: 220_000, endMs: 260_000 },
      { startMs: 310_000, endMs: 350_000 }, // buddy went after you checked off at 268_000
    ];
    expect(pickNearestBurstIndex([], 268_000)).toBe(-1);
    expect(pickNearestBurstIndex(bursts, 268_000)).toBe(1);
    expect(pickNearestBurstIndex(bursts, 263_000)).toBe(1); // slightly inside +5s tolerance
    expect(pickNearestBurstIndex(bursts, 142_000)).toBe(0);
    // Checked earlier than any recorded burst -> falls back to closest overall (index 0)
    expect(pickNearestBurstIndex(bursts, 80_000)).toBe(0);
  });

  it('computes 5m / 15m / All scrub windows clamped inside tape bounds', () => {
    const minMs = 1_000_000;
    const maxMs = minMs + 30 * 60_000; // 30m session

    expect(computeScrubWindow(minMs, maxMs, minMs + 10 * 60_000, null)).toEqual({
      winStart: minMs,
      winEnd: maxMs,
    });

    const w5 = computeScrubWindow(minMs, maxMs, minMs + 12 * 60_000, 5);
    expect(w5.winEnd - w5.winStart).toBe(5 * 60_000);
    expect(w5.winStart).toBe(minMs + 9.5 * 60_000);

    // Clamped near start
    const wStart = computeScrubWindow(minMs, maxMs, minMs + 30_000, 5);
    expect(wStart.winStart).toBe(minMs);
    expect(wStart.winEnd).toBe(minMs + 5 * 60_000);

    // Clamped near end
    const wEnd = computeScrubWindow(minMs, maxMs, maxMs - 20_000, 15);
    expect(wEnd.winEnd).toBe(maxMs);
    expect(wEnd.winStart).toBe(maxMs - 15 * 60_000);
  });

  it('updates IN/OUT clip markers cleanly while keeping [3s .. 120s] valid windows', () => {
    const minMs = 100_000;
    const maxMs = 400_000;

    // Moving IN past existing OUT auto-adjusts OUT to +45s
    expect(applyClipMarker(110_000, 140_000, 200_000, 'in', minMs, maxMs)).toEqual({
      inMs: 200_000,
      outMs: 245_000,
    });

    // Moving IN way too far back (>120s span) tightens OUT to +45s
    expect(applyClipMarker(250_000, 300_000, 120_000, 'in', minMs, maxMs)).toEqual({
      inMs: 120_000,
      outMs: 165_000,
    });

    // Setting OUT normally keeps existing valid IN
    expect(applyClipMarker(200_000, 245_000, 232_000, 'out', minMs, maxMs)).toEqual({
      inMs: 200_000,
      outMs: 232_000,
    });

    // Setting OUT before IN pulls IN back by 45s
    expect(applyClipMarker(250_000, 290_000, 180_000, 'out', minMs, maxMs)).toEqual({
      inMs: 135_000,
      outMs: 180_000,
    });
  });

  it('formats short camera names and check-off offsets', () => {
    expect(shortCamLabel('Rack Cam')).toBe('Rack');
    expect(shortCamLabel('Smith Cam')).toBe('Smith');
    expect(fmtRelativeToCheck(100_000, 102_000)).toBe('right at ✓');
    expect(fmtRelativeToCheck(100_000, 145_000)).toBe('45s before ✓');
    expect(fmtRelativeToCheck(100_000, 200_000)).toBe('1m 40s before ✓');
    expect(fmtRelativeToCheck(180_000, 120_000)).toBe('1m after ✓');
  });
});
