import { describe, expect, it } from 'vitest';
import {
  applyClipMarker,
  burstScrubPosition,
  computeClipZoomWindow,
  computeScrubWindow,
  findNearestFrameIndex,
  fmtRelativeToCheck,
  nextBurstPlayIndex,
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

  it('binary-searches local tape frames for synchronous 60 fps scrubbing', () => {
    expect(findNearestFrameIndex([], 1000)).toBe(-1);
    expect(findNearestFrameIndex([{ tsMs: 5000 }], 9999)).toBe(0);

    const frames = [
      { tsMs: 100_000 },
      { tsMs: 102_000 },
      { tsMs: 104_000 },
      { tsMs: 108_000 },
    ];
    expect(findNearestFrameIndex(frames, 90_000)).toBe(0);
    expect(findNearestFrameIndex(frames, 120_000)).toBe(3);
    expect(findNearestFrameIndex(frames, 104_000)).toBe(2);
    expect(findNearestFrameIndex(frames, 102_900)).toBe(1);
    expect(findNearestFrameIndex(frames, 103_000)).toBe(1); // midpoint ties to lower index
    expect(findNearestFrameIndex(frames, 103_200)).toBe(2);
    expect(findNearestFrameIndex(frames, 106_500)).toBe(3);
  });

  it('expands Scrub Tape timeline around IN/OUT clip markers across the full rail width', () => {
    const minMs = 1_000_000;
    const maxMs = minMs + 20 * 60_000; // 20m workout tape
    const inMs = minMs + 8 * 60_000;
    const outMs = inMs + 40_000; // 40s set (~3% of 20m)

    const wIn = computeClipZoomWindow(minMs, maxMs, inMs, outMs, 'in_set');
    expect(wIn.winStart).toBe(inMs - 8_000);
    expect(wIn.winEnd - wIn.winStart).toBeGreaterThanOrEqual(75_000);
    expect(wIn.winEnd - wIn.winStart).toBeLessThanOrEqual(110_000);

    const wBoth = computeClipZoomWindow(minMs, maxMs, inMs, outMs, 'both_set');
    // 40s * 0.14 = 5.6s pad on each side -> ~51.2s total window (~78% bar fill!)
    expect(wBoth.winStart).toBe(inMs - 5600);
    expect(wBoth.winEnd).toBe(outMs + 5600);
    expect(wBoth.winEnd - wBoth.winStart).toBe(51_200);

    // Clamps safely to tape edges
    const wEdge = computeClipZoomWindow(minMs, maxMs, minMs + 1000, minMs + 25_000, 'both_set');
    expect(wEdge.winStart).toBe(minMs);
    expect(wEdge.winEnd).toBe(minMs + 25_000 + 4000);
  });

  it('computes normalized burst scrub progress and playback frame stepping', () => {
    expect(burstScrubPosition(100_000, 140_000, 100_000)).toEqual({ pct: 0, elapsedSec: 0, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 120_000)).toEqual({ pct: 50, elapsedSec: 20, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 140_000)).toEqual({ pct: 100, elapsedSec: 40, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 90_000)).toEqual({ pct: 0, elapsedSec: 0, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 180_000)).toEqual({ pct: 100, elapsedSec: 40, totalSec: 40 });

    const frames = [{ tsMs: 100_000 }, { tsMs: 102_000 }, { tsMs: 104_000 }];
    expect(nextBurstPlayIndex([], 100_000)).toBe(-1);
    expect(nextBurstPlayIndex([{ tsMs: 100_000 }], 100_000)).toBe(0);
    expect(nextBurstPlayIndex(frames, 100_000)).toBe(1);
    expect(nextBurstPlayIndex(frames, 101_900)).toBe(2);
    expect(nextBurstPlayIndex(frames, 104_000)).toBe(0);
  });
});
