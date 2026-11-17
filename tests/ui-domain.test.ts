import { describe, expect, it } from 'vitest';
import { WindowAvg } from '../src/domain/smooth';
import { RPE_STEPS, rpeLabel } from '../src/domain/rpe';
import { buildTcx } from '../src/domain/tcx';
import { intensityColor, profileBar } from '../src/domain/profile';
import { PROGRAMS, resolveProgram, BASE_FTP } from '../src/domain/programs';
import { clearHrLog, currentStrapBeats, pushHr, recentHrPoints } from '../src/ble/telemetry';
import { PHASE1_PLAN, UPPER1_FINISH_ITEMS, defaultQueueState, projectPlan, type ExtraWorkout } from '../src/domain/plan';
import { msUntilNextLocalMidnight } from '../src/intervals/calendar';
import { PHASE1_ROUTINES } from '../src/lifts/seed';
import { KG_PER_LB, liftSettings } from '../src/lifts/settings';
import type { LiftSession } from '../src/lifts/types';
import { groupDayLogs, renderDayZoomCard, todayCompletedItems, type ZoomLookup } from '../src/ui/dayZoom';
import { hrDropStats } from '../src/ui/hr';

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
  it('can read a shorter sub-window of the same samples (needle smoothing)', () => {
    const a = new WindowAvg(3000);
    for (let t = 0; t <= 2000; t += 1000) a.push(t, 50);
    for (let t = 2200; t <= 3000; t += 200) a.push(t, 56);
    expect(a.value(3000, 800)).toBeCloseTo(56); // last 0.8 s: only the 56s
    expect(a.value(3000, 2000)).toBeCloseTo((2 * 50 + 5 * 56) / 7); // last 2 s: t=1000..3000
    expect(a.value(3000)).toBeCloseTo((50 * 3 + 56 * 5) / 8); // full 3 s window
    expect(a.value(3000, 99999)).toBeCloseTo(a.value(3000)!); // never wider than the window
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

describe('workout profile bars', () => {
  const n4x4 = PROGRAMS.find((p) => p.id === 'n4x4')!;
  it('draws work taller and hotter than rest, warm-up and cool-down', () => {
    const bars = resolveProgram(n4x4, BASE_FTP).segments.map((g) => ({ k: g.kind, ...profileBar(g, BASE_FTP) }));
    const work = bars.filter((b) => b.k === 'work');
    const easy = bars.filter((b) => b.k !== 'work');
    expect(work).toHaveLength(4);
    for (const w of work) for (const e of easy) expect(w.h).toBeGreaterThan(e.h);
    expect(work.every((w) => w.color === intensityColor(111))).toBe(true); // 52.5 RPM = 202 W = 111 % FTP -> Z5 orange
    expect(easy.every((e) => e.pct < 76)).toBe(true);
  });
  it('puts all-out efforts at full height and in the top colour', () => {
    const sprint = PROGRAMS.find((p) => p.id === 'sprint1020')!;
    const hard = sprint.segments.find((g) => g.allOut)!;
    expect(profileBar(hard, BASE_FTP)).toMatchObject({ h: 1, color: intensityColor(0, true) });
  });
  it('uses the athlete FTP, so a higher FTP lowers the same target to a cooler colour', () => {
    const w = n4x4.segments.find((g) => g.kind === 'work')!;
    expect(profileBar(w, 182).pct).toBeGreaterThan(profileBar(w, 260).pct);
    expect(profileBar(w, 182).h).toBeGreaterThan(profileBar(w, 260).h);
  });
  it('colours by power zone edges', () => {
    expect([40, 60, 80, 100, 115, 130].map((p) => intensityColor(p))).toEqual(
      ['#6b7a90', '#3b9cff', '#22c55e', '#facc15', '#fb923c', '#ef4444']);
  });
});

describe('live HR sheet telemetry helpers', () => {
  it('filters recent HR beats inside the window and ignores zeros', () => {
    clearHrLog();
    pushHr(110, 10_000);
    pushHr(0, 70_000);
    pushHr(142, 95_000);
    pushHr(128, 125_000);
    expect(recentHrPoints(60_000, 130_000)).toEqual([
      { at: 95_000, hr: 142 },
      { at: 125_000, hr: 128 },
    ]);
  });

  it('extracts current contiguous strap session (breaking at >60s gap) at 1 Hz', () => {
    clearHrLog();
    pushHr(95, 1_000);
    pushHr(130, 100_100);
    pushHr(132, 100_800); // same UTC second -> deduplicated to latest
    pushHr(135, 102_000);
    const beats = currentStrapBeats(60_000);
    expect(beats).toEqual([
      { at: 100_800, hr: 132 },
      { at: 102_000, hr: 135 },
    ]);
  });

  it('computes 60s HR drop, peak, and low accurately', () => {
    const pts = [
      { at: 1_000, hr: 170 }, // outside 60s window for now=100_000
      { at: 55_000, hr: 152 },
      { at: 75_000, hr: 138 },
      { at: 95_000, hr: 124 },
    ];
    expect(hrDropStats(pts, 124, 100_000, 60_000)).toEqual({
      peak: 152,
      peakAt: 55_000,
      low: 124,
      drop: 28,
    });
    expect(hrDropStats([], 0, 100_000, 60_000)).toBeNull();
    expect(hrDropStats([{ at: 95_000, hr: 140 }], 145, 100_000, 60_000)).toEqual({
      peak: 145,
      peakAt: 100_000,
      low: 140,
      drop: 0,
    });
  });

  it('defaults lift timer UI & auto-timer to clean off state', () => {
    const s = liftSettings();
    expect(s.restAuto).toBe(false);
    expect(s.showTimer).toBe(false);
    expect(s.showAutoTimer).toBe(false);
  });

  it('computes midnight rollover delay accurately before and after midnight', () => {
    expect(msUntilNextLocalMidnight(new Date(2026, 10, 17, 23, 59, 50, 0))).toBe(12_000);
    expect(msUntilNextLocalMidnight(new Date(2026, 10, 17, 0, 0, 5, 0))).toBe(86_397_000);
  });

  it('renders completed lift sets, upcoming day-zoom actions, and Today omit-shift rules cleanly', () => {
    const today = '2026-11-17';
    const w225Kg = 225 * KG_PER_LB;
    const benchName = 'Barbell Bench Press';
    const lookup: ZoomLookup = {
      exOf: (id) => ({
        id,
        name: id === 'barbell-bench-press' ? benchName : id,
        type: 'weight',
        equip: 'bb',
        primary: ['ch'],
        secondary: [],
      }),
      rowOf: () => undefined,
      prevOf: () => null,
      routines: PHASE1_ROUTINES,
      prs: new Map(),
      unit: 'lb',
    };

    const doneSession: LiftSession = {
      id: 'sess-1',
      name: 'Upper 1',
      routineId: 'day-upper-1',
      eventId: null,
      startMs: new Date(2026, 10, 17, 7, 15, 0).getTime(),
      endMs: new Date(2026, 10, 17, 8, 5, 0).getTime(),
      status: 'done',
      exs: [{
        uid: 'u1',
        exId: 'barbell-bench-press',
        sets: [
          { id: 's1', w: w225Kg, r: 5, done: true, kind: 'n' },
          { id: 's2', w: w225Kg, r: 5, done: true, kind: 'n' },
          { id: 's3', w: w225Kg, r: 5, done: true, kind: 'n' },
        ],
      }],
      skipped: [{ exId: 'pull-up', plannedSets: 3, doneSets: 0 }],
      rpe: 7,
      bwKg: 82,
    };

    const grouped = groupDayLogs([doneSession], []);
    const todayLog = grouped.byDate.get(today);
    const items = todayCompletedItems(todayLog, lookup);
    expect(items).toHaveLength(1);
    expect(items[0].name).toBe('Upper 1');
    expect(items[0].sub).toContain('3 sets');

    const projDoneToday = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 2, stagedDate: '2026-11-18' }, today, 0);
    const doneHtml = renderDayZoomCard({
      ymd: today,
      today,
      proj: projDoneToday,
      dayLog: todayLog,
      restDay: 0,
      lookup,
    });
    expect(doneHtml).toContain(benchName);
    expect(doneHtml).toContain('3×5 @ 225 lb');
    expect(doneHtml).toContain('✓ Done');
    expect(doneHtml).not.toContain('sh-skipped');
    expect(doneHtml).not.toContain('pull-up (0/3 sets)');
    expect(doneHtml).toContain('Full workout &amp; edit ›');

    const projActiveToday = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 1, stagedDate: today }, today, 0);
    const tomorrowHtml = renderDayZoomCard({
      ymd: '2026-11-18',
      today,
      proj: projActiveToday,
      restDay: 0,
      lookup,
    });
    expect(tomorrowHtml).toContain('Do this workout now');
    expect(tomorrowHtml).toContain('Shift schedule to here');
    expect(tomorrowHtml).toContain('View Full Program');
    expect(tomorrowHtml).toContain('data-back-today');
    expect(tomorrowHtml).not.toContain('data-inline-dismiss');
    expect(tomorrowHtml).not.toContain('Move Queue Pointer Here');

    const todayPendingHtml = renderDayZoomCard({
      ymd: today,
      today,
      proj: projActiveToday,
      restDay: 0,
      lookup,
    });
    expect(todayPendingHtml).toContain('Do this workout now');
    expect(todayPendingHtml).toContain('data-zact="do-slot" data-slot="1:0"');
    expect(todayPendingHtml).toContain('data-zact="do-slot" data-slot="1:1"');
    expect(todayPendingHtml).not.toContain('data-zact="do-now"');
    expect(todayPendingHtml).toContain('View Full Program');
    expect(todayPendingHtml).not.toContain('Shift schedule to here');
    expect(todayPendingHtml).not.toContain('data-back-today');

    const projMixedSkipped = projectPlan(
      PHASE1_PLAN,
      { ...defaultQueueState(), stepIndex: 2, stagedDate: today, skippedSlots: { '1:1': '2026-11-16' }, stepDates: { 1: '2026-11-16' } },
      today,
      0,
    );
    const mixedHtml = renderDayZoomCard({
      ymd: '2026-11-16',
      today,
      proj: projMixedSkipped,
      restDay: 0,
      lookup,
    });
    expect(mixedHtml).toContain('sh-slot done');
    expect(mixedHtml).toContain('sh-slot skipped');
    expect(mixedHtml).toContain('<span class="p-badge sk">(skipped)</span>');
    const skippedBlock = mixedHtml.split('sh-slot skipped')[1] ?? '';
    expect(skippedBlock).not.toContain('✓ Done');

    const projAllSkipped = projectPlan(
      PHASE1_PLAN,
      { ...defaultQueueState(), stepIndex: 3, stagedDate: today, skippedSlots: { '2:0': '2026-11-16' }, stepDates: { 2: '2026-11-16' } },
      today,
      0,
    );
    const allSkippedHtml = renderDayZoomCard({
      ymd: '2026-11-16',
      today,
      proj: projAllSkipped,
      restDay: 0,
      lookup,
    });
    expect(allSkippedHtml).toContain('(skipped)');
    expect(allSkippedHtml).not.toContain('✓ Done');

    const extra: ExtraWorkout = {
      id: 'u1-finish-2026-11-16',
      name: 'Upper 1 (Finish)',
      date: today,
      equipment: 'strength',
      seconds: 1800,
      description: 'Skipped yesterday',
      items: UPPER1_FINISH_ITEMS,
    };
    const extraHtml = renderDayZoomCard({
      ymd: today,
      today,
      proj: projActiveToday,
      extras: [extra],
      restDay: 0,
      lookup,
    });
    expect(extraHtml).toContain('Upper 1 (Finish)');
    expect(extraHtml).toContain('data-slot="extra:u1-finish-2026-11-16"');
    expect(extraHtml).toContain('seated-dumbbell-shoulder-press');
    expect(extraHtml).toContain('pull-up');
    expect(extraHtml).toContain('dumbbell-lateral-raise');
    expect(extraHtml).toContain('cable-triceps-pushdown');

    const projRemoved = projectPlan(
      PHASE1_PLAN,
      { ...defaultQueueState(), stepIndex: 3, stagedDate: today, removedSlots: { '2:0': '2026-11-16' }, stepDates: { 2: '2026-11-16' } },
      today,
      0,
    );
    const removedHtml = renderDayZoomCard({
      ymd: '2026-11-16',
      today,
      proj: projRemoved,
      restDay: 0,
      lookup,
    });
    expect(removedHtml).not.toContain('(skipped)');
    expect(removedHtml).not.toContain('✓ Done');
    expect(removedHtml).not.toContain('Spin Bike Z2');
  });
});
