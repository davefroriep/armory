import { describe, expect, it } from 'vitest';
import { ALERT_SAMPLE_RATE, ALERT_SPECS, encodeWav, synthesizeBellPcm } from '../src/audio/audio';
import { parentHash } from '../src/nav';
import {
  MAX_RM, PR_MAX_REPS, computePRBaseline, computePRs, computeSessionPRs, e1rm, e1rmTrend, exerciseSessions, muscleSets, previousPerformance, repBests, sessionStats,
  setVolumeKg, weekStart, addWeeks, DAY_MS,
} from '../src/lifts/calc';
import { LIBRARY, LIBRARY_BY_ID } from '../src/lifts/library';
import { buildBoostcampSessions } from '../src/lifts/boostcampSeed';
import {
  addSet, buildExercise, canLog, collectUnfinishedItems, camsForExercise, defaultCamsForExercise, DEFAULT_FALLBACK_CAM,
  driveUrlFor, editReps, editWeight, effectiveRestAuto, elapsedSessionSec, estimateActiveSessionSec, hasSessionActivity, inferredCamForExercise,
  isSessionPaused, isSessionStarted, labels, midSuperset, newSet, normalizeCamList, pauseSessionClock,
  preferredCamForExercise, prevSetFor, pruneUnlogged, removeExerciseAt, removeLastSet, resetSessionClock, resumeSessionClock, routineFromSession,
  setSessionElapsedSec, startSessionClock, supersetGroups, swapExercise, toggleDone,
} from '../src/lifts/logic';
import {
  extractMp4CreationMs, inferPixelVideoLink, parseMvhdCreationMs, sortDeviceVideosNewestFirst,
} from '../src/lifts/mp4';
import {
  computeTrimViewport, createTrimState, dragTrimHandle, fmtPreciseTime, isTrimDirty, isTrimmed, jogTrimDelta,
  MIN_TRIM_SPAN_SEC, resolvePlayStart, stepTrimFrames,
} from '../src/lifts/trimmer';
import { EQUIPS, GROUPS } from '../src/lifts/muscles';
import { PHASE1_EXERCISE_CAMS, PHASE1_ROUTINES, SEED_EX_ROWS, matchRoutineByTitle } from '../src/lifts/seed';
import { applyCamsAndSmithRdlMigration } from '../src/lifts/store';
import { KG_PER_LB, fmtNum, fromDisplay, stepsFor, toDisplay } from '../src/lifts/settings';
import { condenseSets, intervalsDescription, shortLine } from '../src/lifts/summary';
import { MUSCLES, type ExRow, type Exercise, type LiftSession, type LiftSet, type Routine, type SessionEx } from '../src/lifts/types';

const lb = (v: number): number => v * KG_PER_LB;
const ex = (id: string, type: Exercise['type'] = 'weight', primary: Exercise['primary'] = ['ch'], secondary: Exercise['secondary'] = []): Exercise => ({ id, name: id, type, equip: 'bb', primary, secondary });
const BENCH = ex('bench', 'weight', ['ch'], ['fd', 'tri']);
const SQUAT = ex('squat', 'weight', ['qu', 'gl'], ['ad', 'lb']);
const exOf = (id: string): Exercise | undefined => ({ bench: BENCH, squat: SQUAT, curl: ex('curl', 'weight', ['bi']), hold: ex('hold', 'time', ['tri']), pullup: ex('pullup', 'bodyweight', ['la']) } as Record<string, Exercise>)[id];

let n = 0;
const set = (w: number | null, r: number | null, o: Partial<LiftSet> = {}): LiftSet => ({ id: `s${++n}`, w, r, done: true, kind: 'n', ...o });
const sx = (exId: string, sets: LiftSet[], o: Partial<SessionEx> = {}): SessionEx => ({ uid: `u${++n}`, exId, sets, ...o });
const sess = (id: string, startMs: number, exs: SessionEx[], o: Partial<LiftSession> = {}): LiftSession => ({
  id, name: id, routineId: null, eventId: null, startMs, endMs: startMs + 3_600_000, status: 'done', exs, rpe: null, bwKg: 88.5, ...o,
});

describe('exercise library', () => {
  it('has unique ids, valid muscles/equipment and at least one primary muscle', () => {
    const ids = new Set<string>();
    for (const e of LIBRARY) {
      expect(ids.has(e.id), `duplicate id ${e.id}`).toBe(false);
      ids.add(e.id);
      expect(e.primary.length, e.name).toBeGreaterThan(0);
      for (const m of [...e.primary, ...e.secondary]) expect(MUSCLES).toContain(m);
      expect(EQUIPS).toContain(e.equip);
      expect(['weight', 'bodyweight', 'time']).toContain(e.type);
      expect(e.secondary.filter((m) => e.primary.includes(m)), `${e.name} lists a muscle as both`).toEqual([]);
    }
    expect(LIBRARY.length).toBeGreaterThan(120);
  });
  it('covers every muscle in at least one display group and one exercise', () => {
    const grouped = new Set(GROUPS.flatMap((g) => g.muscles));
    for (const m of MUSCLES) {
      expect(grouped.has(m), m).toBe(true);
      expect(LIBRARY.some((e) => e.primary.includes(m)), m).toBe(true);
    }
  });
  it('knows the home-gym staples', () => {
    for (const id of ['belt-squat', 'lying-leg-curl', 'glute-ham-raise', 'triceps-isometric-hold', 'cable-leg-press', 'smith-machine-romanian-deadlift']) {
      expect(LIBRARY_BY_ID.has(id), id).toBe(true);
    }
    expect(LIBRARY_BY_ID.get('triceps-isometric-hold')!.type).toBe('time');
  });
});

describe('units', () => {
  it('round-trips pounds without float noise and provides custom +/-2.5/+/-5/+/-10 weight steps', () => {
    for (const v of [45, 135, 137.5, 225, 315, 2.5]) expect(toDisplay(fromDisplay(v, 'lb'), 'lb')).toBe(v);
    expect(fmtNum(toDisplay(lb(135), 'lb'))).toBe('135');
    expect(toDisplay(100, 'kg')).toBe(100);
    expect(stepsFor('lb')).toEqual([-10, -5, -2.5, 2.5, 5, 10]);
    expect(stepsFor('kg')).toEqual([-5, -2.5, 2.5, 5]);
  });
});

describe('auto-fill from last time + forward fill', () => {
  const prev = sx('bench', [set(lb(135), 8), set(lb(155), 6), set(lb(175), 5)]);

  it('fills each set from the matching set last time, flagged as suggestions', () => {
    const e = buildExercise('bench', 3, prev);
    expect(e.sets.map((s) => [toDisplay(s.w!, 'lb'), s.r])).toEqual([[135, 8], [155, 6], [175, 5]]);
    expect(e.sets.every((s) => s.wAuto && s.rAuto && !s.done)).toBe(true);
  });
  it('sets beyond last time reuse the final set; empty history stays blank', () => {
    const e = buildExercise('bench', 5, prev);
    expect(toDisplay(e.sets[4].w!, 'lb')).toBe(175);
    const blank = buildExercise('bench', 3, null);
    expect(blank.sets.every((s) => s.w == null && s.r == null)).toBe(true);
  });
  it('typing a weight flows forward into later untouched sets only', () => {
    const e = buildExercise('bench', 4, prev);
    editWeight(e, 1, lb(160));
    expect(toDisplay(e.sets[0].w!, 'lb')).toBe(135); // before: untouched
    expect(toDisplay(e.sets[2].w!, 'lb')).toBe(160);
    expect(toDisplay(e.sets[3].w!, 'lb')).toBe(160);
    editWeight(e, 2, lb(165)); // a later edit overrides the forwarded value, and flows on
    expect(toDisplay(e.sets[3].w!, 'lb')).toBe(165);
    expect(toDisplay(e.sets[1].w!, 'lb')).toBe(160);
  });
  it('never overwrites a value the user typed, nor a logged set', () => {
    const e = buildExercise('bench', 3, prev);
    editWeight(e, 2, lb(200)); // user typed set 3
    editWeight(e, 0, lb(140));
    expect(toDisplay(e.sets[2].w!, 'lb')).toBe(200);
    toggleDone(e, 1, BENCH);
    editReps(e, 0, 10);
    expect(e.sets[1].r).toBe(6); // logged set untouched
  });
  it('deleting a number clears later untouched auto suggestions in real time without touching user-typed or logged sets', () => {
    const e = buildExercise('bench', 3, null);
    editWeight(e, 0, lb(135));
    editReps(e, 0, 10);
    expect(e.sets.map((s) => toDisplay(s.w!, 'lb'))).toEqual([135, 135, 135]);
    expect(e.sets.map((s) => s.r)).toEqual([10, 10, 10]);
    editWeight(e, 0, null);
    editReps(e, 0, null);
    expect(e.sets.map((s) => s.w)).toEqual([null, null, null]);
    expect(e.sets.map((s) => s.r)).toEqual([null, null, null]);
    expect(e.sets.every((s) => !s.wAuto && !s.rAuto)).toBe(true);
    // User-typed set 3 stays intact if set 1 is cleared
    editWeight(e, 0, lb(135));
    editWeight(e, 2, lb(185));
    editWeight(e, 0, null);
    expect(e.sets.map((s) => (s.w == null ? null : toDisplay(s.w, 'lb')))).toEqual([null, null, 185]);
  });
  it('logging a set forwards both weight and reps', () => {
    const e = buildExercise('bench', 3, null);
    editWeight(e, 0, lb(135));
    editReps(e, 0, 10);
    expect(toggleDone(e, 0, BENCH)).toBe(true);
    expect(e.sets[1].w).toBeCloseTo(lb(135));
    expect(e.sets[2].r).toBe(10);
    expect(e.sets[0].done && e.sets[0].at).toBeTruthy();
  });
  it('refuses to log an incomplete weight set but allows bodyweight with reps only', () => {
    const e = buildExercise('bench', 1, null);
    expect(toggleDone(e, 0, BENCH)).toBeNull();
    editReps(e, 0, 5);
    expect(canLog(e.sets[0], BENCH)).toBe(false);
    const pu = buildExercise('pullup', 1, null);
    editReps(pu, 0, 8);
    expect(toggleDone(pu, 0, exOf('pullup'))).toBe(true);
    expect(pu.sets[0].w).toBe(0);
  });
  it('warm-ups pair with warm-ups and never forward into working sets', () => {
    const p = sx('bench', [set(lb(95), 8, { kind: 'w' }), set(lb(135), 5), set(lb(135), 5)]);
    const e: SessionEx = sx('bench', [newSet('w'), newSet('n'), newSet('n')]);
    expect(toDisplay(prevSetFor(p, e, 0)!.w!, 'lb')).toBe(95);
    expect(toDisplay(prevSetFor(p, e, 1)!.w!, 'lb')).toBe(135);
    editWeight(e, 0, lb(65));
    expect(e.sets[1].w).toBeNull();
  });
  it('+ Set copies the last working set; - Set refuses to empty the card', () => {
    const e = buildExercise('bench', 2, prev);
    editWeight(e, 1, lb(165));
    addSet(e, prev);
    expect(e.sets).toHaveLength(3);
    expect(toDisplay(e.sets[2].w!, 'lb')).toBe(165);
    expect(removeLastSet(e)).not.toBeNull();
    removeLastSet(e);
    expect(removeLastSet(e)).toBeNull();
    expect(e.sets).toHaveLength(1);
  });
  it('swap keeps the shape, preserves slot, and pulls suggestions from the new exercise history', () => {
    const e = buildExercise('bench', 3, prev, false, 2);
    expect(e.slot).toBe(2);
    toggleDone(e, 0, BENCH);
    swapExercise(e, 'curl', sx('curl', [set(lb(30), 12)]));
    expect(e.exId).toBe('curl');
    expect(e.slot).toBe(2);
    expect(e.sets).toHaveLength(3);
    expect(e.sets.every((s) => !s.done && toDisplay(s.w!, 'lb') === 30 && s.r === 12)).toBe(true);
  });
});

describe('supersets & finishing', () => {
  it('aligns 3 sets 1-to-1 regardless of session age and returns null for extra sets beyond previous', () => {
    const now = new Date(2026, 6, 1, 9).getTime();
    const ancient = sess('old', now - 180 * DAY_MS, [sx('bench', [set(100, 8), set(105, 8), set(110, 6)])]);
    const prev = previousPerformance([ancient], 'bench', now)!;
    const today4 = buildExercise('bench', 4, prev);
    expect([0, 1, 2, 3].map((i) => prevSetFor(prev, today4, i)?.w ?? null)).toEqual([100, 105, 110, null]);
  });
  it('labels groups 1A/1B and singles and builds spanning superset groups for Upper 1', () => {
    const exs = [sx('a', [], { ss: true }), sx('b', []), sx('c', []), sx('d', [], { ss: true }), sx('e', [], { ss: true }), sx('f', [])];
    expect(labels(exs)).toEqual(['1A', '1B', '2', '3A', '3B', '3C']);
    expect(midSuperset(exs, 0)).toBe(true);
    expect(midSuperset(exs, 1)).toBe(false);
    expect(supersetGroups(exs)).toEqual([
      { start: 0, end: 1, ssIdx: 0 },
      { start: 2, end: 2, ssIdx: null },
      { start: 3, end: 5, ssIdx: 1 },
    ]);
    const upper1 = PHASE1_ROUTINES.find((r) => r.name === 'Upper 1')!;
    expect(labels(upper1.items)).toEqual(['1', '2A', '2B', '3A', '3B', '4A', '4B']);
    expect(supersetGroups(upper1.items)).toEqual([
      { start: 0, end: 0, ssIdx: null },
      { start: 1, end: 2, ssIdx: 0 },
      { start: 3, end: 4, ssIdx: 1 },
      { start: 5, end: 6, ssIdx: 2 },
    ]);
  });
  it('computes effective auto-rest with mid-superset off default and explicit overrides', () => {
    const exs = [sx('a', [], { ss: true }), sx('b', []), sx('c', [])];
    expect(effectiveRestAuto(exs, 0, true, true)).toBe(false); // 1A mid-superset off by default
    expect(effectiveRestAuto(exs, 1, undefined, true)).toBe(true); // 1B uses global
    expect(effectiveRestAuto(exs, 2, false, true)).toBe(false); // row override beats global
    exs[0].restAuto = true;
    expect(effectiveRestAuto(exs, 0, undefined, false)).toBe(true); // explicit card toggle wins over mid-superset
    exs[2].restAuto = true;
    expect(effectiveRestAuto(exs, 2, false, false)).toBe(true); // explicit session override wins
  });
  it('collects unfinished working sets for finish-prompt and prunes unlogged sets without setting skipped', () => {
    const s = sess('x', 0, [
      sx('bench', [set(lb(95), 8, { kind: 'w', done: false }), set(lb(135), 5), set(null, null, { done: false })], { ss: true }),
      sx('curl', [set(null, null, { done: false }), set(null, null, { done: false }), set(null, null, { done: false })]),
    ], { status: 'active' });
    expect(collectUnfinishedItems(s)).toEqual([
      { exId: 'bench', sets: 1, ss: true },
      { exId: 'curl', sets: 3 },
    ]);
    expect(pruneUnlogged(s)).toBe(5);
    expect(s.exs).toHaveLength(1);
    expect(s.exs[0].sets).toHaveLength(1);
    expect(s.exs[0].ss).toBeUndefined();
    expect(s.skipped).toBeUndefined();

    // Warm-up-only unlogged sets do not appear in unfinished finish prompt or skipped
    const clean = sess('y', 0, [sx('bench', [set(lb(95), 8, { kind: 'w', done: false }), set(lb(135), 5)])], { status: 'active' });
    expect(collectUnfinishedItems(clean)).toEqual([]);
    expect(pruneUnlogged(clean)).toBe(1);
    expect(clean.skipped).toBeUndefined();
  });
  it('turns a session into a day with logged working-set counts', () => {
    const s = sess('x', 0, [sx('bench', [set(lb(95), 8, { kind: 'w' }), set(lb(135), 5), set(lb(135), 5)], { ss: true }), sx('curl', [set(lb(30), 10)])]);
    const r = routineFromSession(s, 'Push', 3);
    expect(r.items).toEqual([{ exId: 'bench', sets: 2, ss: true }, { exId: 'curl', sets: 1 }]);
  });
});

describe('e1RM, rep records, PRs', () => {
  it('estimates 1RM with Epley and refuses junk', () => {
    expect(e1rm(100, 1)).toBe(100);
    expect(e1rm(100, 5)).toBeCloseTo(116.67, 1);
    expect(e1rm(100, MAX_RM + 1)).toBeNull();
    expect(e1rm(0, 5)).toBeNull();
  });

  const t0 = new Date(2026, 0, 5, 9).getTime();
  const s1 = sess('s1', t0, [sx('bench', [set(100, 5), set(100, 5), set(90, 8)])]);
  const s2 = sess('s2', t0 + 7 * DAY_MS, [sx('bench', [set(105, 5), set(110, 3), set(60, 10, { kind: 'w' })])]);
  const s3 = sess('s3', t0 + 14 * DAY_MS, [sx('bench', [set(105, 5), set(112, 3)])]);

  it('"heaviest for at least n reps" bests, ties keep the earliest', () => {
    const b = repBests(exerciseSessions([s1, s2, s3], 'bench'));
    expect(b[1]!.kg).toBe(112);
    expect(b[3]!.kg).toBe(112);
    expect(b[4]!.kg).toBe(105);
    expect(b[5]!.kg).toBe(105);
    expect(b[5]!.sessionId).toBe('s2'); // 105x5 first done in s2 (s3 tied it)
    expect(b[8]!.kg).toBe(90);
    expect(b[9]).toBeNull();
  });
  it('warm-ups never count toward records', () => {
    const b = repBests(exerciseSessions([s2], 'bench'));
    expect(b[10]).toBeNull(); // the 60x10 was a warm-up
  });
  it('flags PRs chronologically; the first session and ties are not PRs', () => {
    const prs = computePRs([s3, s1, s2], exOf); // order-independent input
    const flagged = (s: LiftSession): number[] => s.exs.flatMap((e) => e.sets).filter((x) => prs.has(x.id)).map((x) => x.w!);
    expect(flagged(s1)).toEqual([]);
    expect(flagged(s2)).toEqual([105, 110]); // 105x5 beats 100x5; 110x3 beats 100x3
    expect(flagged(s3)).toEqual([112]); // 105x5 ties, 112x3 beats 110
    expect(prs.get(s3.exs[0].sets[1].id)).toEqual({ n: 3, kg: 112 });
  });
  it('a set only counts once per session (second identical set is not a PR)', () => {
    const a = sess('a', t0, [sx('bench', [set(100, 5)])]);
    const b = sess('b', t0 + DAY_MS, [sx('bench', [set(105, 5), set(105, 5)])]);
    const prs = computePRs([a, b], exOf);
    expect(b.exs[0].sets.filter((x) => prs.has(x.id))).toHaveLength(1);
  });
  it('time exercises have no weight PRs; trend takes the best e1RM per session', () => {
    const h1 = sess('h1', t0, [sx('hold', [set(0, 45)])]);
    const h2 = sess('h2', t0 + DAY_MS, [sx('hold', [set(0, 60)])]);
    expect(computePRs([h1, h2], exOf).size).toBe(0);
    const tr = e1rmTrend(exerciseSessions([s1, s2, s3], 'bench'), BENCH);
    expect(tr).toHaveLength(3);
    expect(tr[0].e1rm).toBeCloseTo(100 * (1 + 5 / 30), 1); // 100x5 beats 90x8 (=114)
    expect(tr[2].topKg).toBe(112); // 112x3 (123.2) edges 105x5 (122.5)
  });
  it('flags unweighted bodyweight rep PRs while ignoring zero-kg weighted sets', () => {
    const p1 = sess('p1', t0, [sx('pullup', [set(0, 8)]), sx('bench', [set(0, 8)])]);
    const p2 = sess('p2', t0 + DAY_MS, [sx('pullup', [set(0, 8), set(0, 10)]), sx('bench', [set(0, 10)])]);
    const prs = computePRs([p1, p2], exOf);
    expect(prs.has(p2.exs[0].sets[0].id)).toBe(false); // tie
    expect(prs.get(p2.exs[0].sets[1].id)).toEqual({ n: 10, kg: 0 }); // BW x 10 PR
    expect(prs.has(p2.exs[1].sets[0].id)).toBe(false); // 0 kg bench is not a PR
  });
  it('attaches video from a later tied set while keeping the earliest PR date', () => {
    const v1 = sess('v1', t0, [sx('bench', [set(100, 5)])]);
    const v2 = sess('v2', t0 + 7 * DAY_MS, [sx('bench', [set(100, 5, { vids: ['vid-99'] })])]);
    const b = repBests(exerciseSessions([v1, v2], 'bench'));
    expect(b[5]!.sessionId).toBe('v1');
    expect(b[5]!.ms).toBe(t0);
    expect(b[5]!.vidId).toBe('vid-99');
  });
  it('previous performance is the latest finished session before the cutoff', () => {
    const p = previousPerformance([s1, s2, s3], 'bench', s3.startMs)!;
    expect(p).toBe(s2.exs[0]);
    expect(previousPerformance([s1], 'curl')).toBeNull();
    expect(previousPerformance([{ ...s2, status: 'active' }], 'bench')).toBeNull();
  });

  it('tracks high-rep 13RM..40RM PRs and repBests with video links while keeping Epley e1rm capped at 12 and ignoring volume-only extra sets', () => {
    expect(MAX_RM).toBe(12);
    expect(PR_MAX_REPS).toBe(40);
    expect(e1rm(lb(225), 15)).toBeNull();

    const hr1 = sess('hr1', t0, [sx('bench', [set(lb(225), 15)])]);
    const hr2 = sess('hr2', t0 + 7 * DAY_MS, [sx('bench', [set(lb(225), 18, { vids: ['vid-225x18'] })])]);
    const prs = computePRs([hr1, hr2], exOf);
    expect(prs.get(hr2.exs[0].sets[0].id)).toEqual({ n: 18, kg: lb(225) });

    const bests = repBests(exerciseSessions([hr1, hr2], 'bench'));
    expect(bests[18]!.reps).toBe(18);
    expect(bests[18]!.vidId).toBe('vid-225x18');
    expect(bests[18]!.kg).toBeCloseTo(lb(225));

    // Additional working sets at the exact same weight x reps produce zero volume-only PRs
    const vol1 = sess('vol1', t0 + 14 * DAY_MS, [sx('bench', [set(lb(185), 8), set(lb(185), 8), set(lb(185), 8)])]);
    const vol2 = sess('vol2', t0 + 21 * DAY_MS, [sx('bench', [set(lb(185), 8), set(lb(185), 8), set(lb(185), 8), set(lb(185), 8)])]);
    const volPrs = computePRs([vol1, vol2], exOf);
    expect(volPrs.size).toBe(0);

    // Sets above 40 reps clamp repBests to PR_MAX_REPS (40RM) and do not emit 45RM hits
    const ultra1 = sess('u1', t0 + 28 * DAY_MS, [sx('bench', [set(lb(125), 40)])]);
    const ultra2 = sess('u2', t0 + 35 * DAY_MS, [sx('bench', [set(lb(135), 45)])]);
    const ultraBests = repBests(exerciseSessions([ultra1, ultra2], 'bench'));
    expect(ultraBests).toHaveLength(PR_MAX_REPS + 1);
    expect(ultraBests[PR_MAX_REPS]!.kg).toBeCloseTo(lb(135));
    expect(ultraBests[PR_MAX_REPS + 1]).toBeUndefined();
    const ultraPrs = computePRs([ultra1, ultra2], exOf);
    expect(ultraPrs.has(ultra2.exs[0].sets[0].id)).toBe(false);
  });
});

describe('precision active-edge trimmer', () => {
  it('couples Left/Right grab bars, ±5f/±1f stepping, Thumb Jog, and playhead preview in lockstep', () => {
    const s0 = createTrimState(20);
    expect(s0.start).toBe(0);
    expect(s0.end).toBe(20);
    expect(s0.focus).toBe('playhead');
    expect(isTrimmed(s0)).toBe(false);
    expect(isTrimDirty(s0)).toBe(false);

    // Drag Left (Start) rail to 4.000s and Right (End) rail to 14.000s
    const s1 = dragTrimHandle(s0, 'in', 4.0);
    expect(s1.focus).toBe('in');
    expect(s1.start).toBe(4);
    expect(s1.playhead).toBe(4);
    expect(isTrimmed(s1)).toBe(true);
    expect(isTrimDirty(s1)).toBe(true);

    // ±5f / ±1f moves BOTH start rail and preview playhead at 1/30s (no 0.1s rounding drift)
    const s1p5 = stepTrimFrames(s1, 5);
    expect(s1p5.start).toBe(4.167);
    expect(s1p5.playhead).toBe(4.167);
    expect(fmtPreciseTime(s1p5.start)).toBe('0:04.17');

    const s1p6 = stepTrimFrames(s1p5, 1);
    expect(s1p6.start).toBe(4.2);
    expect(s1p6.playhead).toBe(4.2);

    // Grab Right (End) rail and nudge via jog strip + -5f
    const s2 = dragTrimHandle(s1p6, 'out', 14.0);
    expect(s2.focus).toBe('out');
    expect(s2.end).toBe(14);
    expect(s2.playhead).toBe(14);

    const s2j = jogTrimDelta(s2, -0.5);
    expect(s2j.end).toBe(13.5);
    expect(s2j.playhead).toBe(13.5);

    const s2m5 = stepTrimFrames(s2j, -5);
    expect(s2m5.end).toBe(13.333);
    expect(s2m5.playhead).toBe(13.333);

    // Clamps by MIN_TRIM_SPAN_SEC (0.25s) so Start and End rails never cross
    const crossed = dragTrimHandle(s2m5, 'in', 20);
    expect(crossed.start).toBeCloseTo(13.333 - MIN_TRIM_SPAN_SEC, 3);

    // Pressing Play after dialing End rail automatically jumps playhead back to Start and focuses playhead
    const playing = resolvePlayStart(s2m5);
    expect(playing.focus).toBe('playhead');
    expect(playing.playhead).toBe(s2m5.start);

    // Focusing playhead lets ±1f step just the preview frame without altering saved Start/End bounds
    const steppedHead = stepTrimFrames(playing, 3);
    expect(steppedHead.playhead).toBe(4.3);
    expect(steppedHead.start).toBe(4.2);
    expect(steppedHead.end).toBe(13.333);

    // Expanding a heavily trimmed window zooms viewport to fill the rail left-to-right
    const longFull = createTrimState(60);
    expect(computeTrimViewport(longFull, true)).toEqual({ start: 0, end: 60, zoomed: false });

    const trimmed80 = dragTrimHandle(dragTrimHandle(longFull, 'in', 24), 'out', 36); // 12s kept (20% of 60s)
    expect(computeTrimViewport(trimmed80, false)).toEqual({ start: 0, end: 60, zoomed: false });
    const vp = computeTrimViewport(trimmed80, true);
    expect(vp.zoomed).toBe(true);
    expect(vp.start).toBeCloseTo(23.04, 2); // 12 * 0.08 = 0.96s pad
    expect(vp.end).toBeCloseTo(36.96, 2);
    expect((36 - 24) / (vp.end - vp.start)).toBeGreaterThan(0.85);

    // Clamps safely at boundaries & stays unzoomed when >=85% of clip is kept
    const nearEdge = dragTrimHandle(longFull, 'out', 8);
    expect(computeTrimViewport(nearEdge, true)).toEqual({ start: 0, end: 8.64, zoomed: true });
    const mostlyKept = dragTrimHandle(dragTrimHandle(longFull, 'in', 2), 'out', 58);
    expect(computeTrimViewport(mostlyKept, true)).toEqual({ start: 0, end: 60, zoomed: false });
  });
});

describe('volume & muscle sets', () => {
  const wk = weekStart(new Date(2026, 0, 7, 12).getTime()); // Wed -> Mon 5 Jan
  const s = sess('w1', wk + DAY_MS, [
    sx('bench', [set(100, 5), set(100, 5), set(60, 10, { kind: 'w' }), set(100, 5, { done: false })]),
    sx('squat', [set(140, 5), set(140, 5), set(140, 5)]),
  ]);

  it('week starts on Monday, local', () => {
    expect(new Date(wk).getDay()).toBe(1);
    expect(new Date(wk).getHours()).toBe(0);
    expect(weekStart(wk + 6 * DAY_MS + 5 * 3_600_000)).toBe(wk);
    expect(addWeeks(wk, 1)).toBe(weekStart(wk + 8 * DAY_MS));
  });
  it('counts primary movers 1.0 and secondary 0.5; warm-ups and unlogged sets never count', () => {
    const m = muscleSets([s], exOf, wk, addWeeks(wk, 1));
    expect(m.ch).toBe(2);
    expect(m.fd).toBe(1);
    expect(m.tri).toBe(1);
    expect(m.qu).toBe(3);
    expect(m.gl).toBe(3);
    expect(m.ad).toBe(1.5);
    expect(m.bi).toBe(0);
  });
  it('respects the date window', () => {
    const none = muscleSets([s], exOf, addWeeks(wk, 1), addWeeks(wk, 2));
    expect(Object.values(none).every((v) => v === 0)).toBe(true);
  });
  it('session stats: volume only from logged working sets; bodyweight adds body mass; time sets are volume-free', () => {
    const st = sessionStats(s, exOf);
    expect(st.sets).toBe(5);
    expect(st.volumeKg).toBe(2 * 500 + 3 * 700);
    expect(st.exercises).toBe(2);
    const bw = sess('bw', wk, [sx('pullup', [set(10, 5)]), sx('hold', [set(0, 45)])], { bwKg: 90 });
    expect(setVolumeKg(bw.exs[0].sets[0], exOf('pullup'), 90)).toBe(100 * 5);
    expect(setVolumeKg(bw.exs[1].sets[0], exOf('hold'), 90)).toBe(0);
  });
});

describe('summary text', () => {
  const unit = 'lb' as const;
  it('condenses uniform and mixed sets', () => {
    expect(condenseSets(sx('bench', [set(lb(175), 5), set(lb(175), 5), set(lb(175), 5)]), BENCH, unit)).toBe('3×5 @ 175 lb');
    expect(condenseSets(sx('bench', [set(lb(135), 8), set(lb(155), 6), set(lb(175), 5)]), BENCH, unit)).toBe('135×8 · 155×6 · 175×5 lb');
    expect(condenseSets(sx('bench', [set(lb(95), 8, { kind: 'w' }), set(lb(135), 5)]), BENCH, unit)).toBe('1×5 @ 135 lb');
    expect(condenseSets(sx('pullup', [set(0, 8), set(0, 8)]), exOf('pullup'), unit)).toBe('2×8 BW');
    expect(condenseSets(sx('hold', [set(0, 45), set(0, 45)]), exOf('hold'), unit)).toBe('2×45s');
  });
  it('share-card line falls back to top set when long', () => {
    const e = sx('bench', [set(lb(135), 8), set(lb(155), 6), set(lb(175), 5), set(lb(185), 3), set(lb(195), 1)]);
    expect(shortLine(e, BENCH, unit)).toBe('5 sets · top 195×1');
  });
  it('writes the Intervals.icu description', () => {
    const s = sess('d', new Date(2026, 0, 5, 7).getTime(), [sx('bench', [set(lb(175), 5), set(lb(175), 5)]), sx('curl', [set(lb(30), 12)])], { name: 'Upper 1', rpe: 8, endMs: new Date(2026, 0, 5, 7, 52).getTime() });
    const desc = intervalsDescription(s, { unit, exOf, prs: new Map([[s.exs[0].sets[0].id, { n: 5, kg: lb(175) }]]), avgHr: 118, maxHr: 151, kcal: 310 });
    expect(desc).toBe([
      'Armory Fit · Upper 1',
      '52 min · 3 sets · 2,110 lb · 1 PR',
      '',
      'bench: 2×5 @ 175 lb  ★ 5RM PR',
      'curl: 1×12 @ 30 lb',
      '',
      'avg HR 118 · max HR 151 · ~310 kcal · RPE 8/10',
    ].join('\n'));

    const partial = sess('d2', new Date(2026, 0, 6, 7).getTime(), [sx('bench', [set(lb(175), 5), set(lb(175), 5)])], {
      name: 'Upper 1',
      endMs: new Date(2026, 0, 6, 7, 30).getTime(),
      skipped: [
        { exId: 'bench', plannedSets: 3, doneSets: 2 },
        { exId: 'curl', plannedSets: 3, doneSets: 0 },
      ],
    });
    const descSkip = intervalsDescription(partial, { unit, exOf, prs: new Map() });
    expect(descSkip).toBe([
      'Armory Fit · Upper 1',
      '30 min · 2 sets · 1,750 lb',
      '',
      'bench: 2×5 @ 175 lb',
    ].join('\n'));
    expect(descSkip).not.toContain('Skipped:');
  });
});

describe('Phase 1 routine seed', () => {
  it('resolves every seeded exercise in the library, matches planned titles, and pre-fills the 45s iso hold', () => {
    expect(PHASE1_ROUTINES.map((r) => r.name)).toEqual(['Lower 1', 'Upper 1', 'Lower 2', 'Upper 2']);
    for (const r of PHASE1_ROUTINES) {
      for (const it of r.items) expect(LIBRARY_BY_ID.has(it.exId), `${r.name}:${it.exId}`).toBe(true);
    }
    expect(matchRoutineByTitle('Strength Train — Wk 1 · Day 2 Upper 1', PHASE1_ROUTINES)?.name).toBe('Upper 1');
    expect(matchRoutineByTitle('Strength Train — Wk 1 · Day 5 Upper 2', PHASE1_ROUTINES)?.name).toBe('Upper 2');
    const hold = buildExercise('triceps-isometric-hold', 2, null);
    expect(hold.sets.map((s) => [s.r, s.rAuto])).toEqual([[45, true], [45, true]]);
  });
  it('treats active workouts as unstarted preview until started, set checked, or video linked; and builds Drive DCIM URLs', () => {
    const preview = sess('prev', Date.now(), [sx('bench', [set(lb(135), 8, { done: false })])], { status: 'active', started: false });
    expect(hasSessionActivity(preview)).toBe(false);
    expect(isSessionStarted(preview)).toBe(false);
    preview.exs[0].sets[0].vids = ['vid-1'];
    expect(hasSessionActivity(preview)).toBe(true);
    expect(isSessionStarted(preview)).toBe(true);
    preview.exs[0].sets[0].vids = [];
    expect(hasSessionActivity(preview)).toBe(false);
    expect(isSessionStarted(preview)).toBe(false);
    preview.started = true;
    expect(isSessionStarted(preview)).toBe(true);
    const checkedNoFlag = sess('chk', Date.now(), [sx('bench', [set(lb(135), 8, { done: true })])], { status: 'active' });
    expect(isSessionStarted(checkedNoFlag)).toBe(true);

    // Clock check -> uncheck -> re-check must restart from 0 at the new check time (never resume old count)
    const live = sess('clk', 1_000_000, [sx('bench', [set(lb(135), 8, { done: false })])], { status: 'active', started: false });
    toggleDone(live.exs[0], 0, BENCH, 1_030_000);
    expect(startSessionClock(live, 1_030_000)).toBe(true);
    expect(live.started).toBe(true);
    expect(live.startMs).toBe(1_030_000);
    // Uncheck at +15s -> resets clock & clears started
    toggleDone(live.exs[0], 0, BENCH, 1_045_000);
    expect(resetSessionClock(live, 1_045_000)).toBe(true);
    expect(live.started).toBe(false);
    expect(live.startMs).toBe(1_045_000);
    expect(isSessionStarted(live)).toBe(false);
    // Re-check 20s later -> starts fresh at 1_065_000 (0:00 elapsed), not 0:35
    toggleDone(live.exs[0], 0, BENCH, 1_065_000);
    expect(startSessionClock(live, 1_065_000)).toBe(true);
    expect(live.started).toBe(true);
    expect(live.startMs).toBe(1_065_000);

    // Explicit Start button or camera starts session with manualStart so unchecking Set 1 keeps clock running
    const manual = sess('man', 2_000_000, [sx('bench', [set(lb(135), 8, { done: false })])], { status: 'active', started: false });
    expect(startSessionClock(manual, 2_020_000, true)).toBe(true);
    expect(manual.started).toBe(true);
    expect(manual.manualStart).toBe(true);
    expect(manual.startMs).toBe(2_020_000);
    expect(isSessionStarted(manual)).toBe(true);
    expect(resetSessionClock(manual, 2_030_000)).toBe(false);
    toggleDone(manual.exs[0], 0, BENCH, 2_040_000);
    toggleDone(manual.exs[0], 0, BENCH, 2_050_000);
    expect(resetSessionClock(manual, 2_050_000)).toBe(false);
    expect(manual.started).toBe(true);
    expect(manual.startMs).toBe(2_020_000);

    // Check auto-start upgraded by camera/Start preserves startMs and prevents subsequent reset on uncheck
    const upgraded = sess('upg', 3_000_000, [sx('bench', [set(lb(135), 8, { done: false })])], { status: 'active', started: false });
    toggleDone(upgraded.exs[0], 0, BENCH, 3_010_000);
    expect(startSessionClock(upgraded, 3_010_000, false)).toBe(true);
    expect(startSessionClock(upgraded, 3_015_000, true)).toBe(false);
    expect(upgraded.manualStart).toBe(true);
    expect(upgraded.startMs).toBe(3_010_000);
    toggleDone(upgraded.exs[0], 0, BENCH, 3_020_000);
    expect(resetSessionClock(upgraded, 3_020_000)).toBe(false);
    expect(upgraded.started).toBe(true);
    expect(driveUrlFor({ fileName: 'PXL_20261116_173012345.mp4' })).toBe('https://drive.google.com/drive/search?q=PXL_20261116_173012345.mp4');
    expect(driveUrlFor({ fileName: 'PXL_20261116_1856xx.mp4', driveQuery: 'PXL 20261116 1856 type:video' })).toBe('https://drive.google.com/drive/search?q=PXL%2020261116%201856%20type%3Avideo');
    expect(driveUrlFor({ fileName: 'clip.mp4', uri: 'https://drive.google.com/file/d/abc/view' })).toBe('https://drive.google.com/file/d/abc/view');
    expect(driveUrlFor({ fileName: 'OptiPlex_clip.mp4', source: 'gymcam' })).toBe('https://drive.google.com/drive/search?q=OptiPlex_clip.mp4');
    expect(driveUrlFor({ fileName: 'OptiPlex_clip.mp4', source: 'gymcam', uri: 'https://drive.google.com/file/d/xyz123/view' })).toBe('https://drive.google.com/file/d/xyz123/view');
    expect(driveUrlFor({})).toBeNull();
  });

  it('extracts UTC creation time from MP4 mvhd (head or tail moov, v0 and v1) and infers Pixel Drive query', async () => {
    const EPOCH_OFFSET = 2_082_844_800;
    const targetMs = Date.UTC(2026, 10, 16, 18, 56, 35);
    const mp4Sec = Math.floor(targetMs / 1000) + EPOCH_OFFSET;

    // Build synthetic ftyp + huge mdat + tail moov(mvhd v0)
    const buildMvhd = (version: 0 | 1): Uint8Array => {
      const mvhdSize = version === 0 ? 108 : 120;
      const b = new Uint8Array(mvhdSize);
      const dv = new DataView(b.buffer);
      dv.setUint32(0, mvhdSize);
      dv.setUint32(4, 0x6d766864); // mvhd
      dv.setUint8(8, version);
      if (version === 0) {
        dv.setUint32(12, mp4Sec);
        dv.setUint32(20, 1000);
      } else {
        dv.setUint32(12, Math.floor(mp4Sec / 4294967296));
        dv.setUint32(16, mp4Sec >>> 0);
        dv.setUint32(28, 1000);
      }
      return b;
    };

    const mvhd0 = buildMvhd(0);
    const moov = new Uint8Array(8 + mvhd0.length);
    const mdv = new DataView(moov.buffer);
    mdv.setUint32(0, moov.length);
    mdv.setUint32(4, 0x6d6f6f76); // moov
    moov.set(mvhd0, 8);

    const ftyp = new Uint8Array(20);
    new DataView(ftyp.buffer).setUint32(0, 20);
    new DataView(ftyp.buffer).setUint32(4, 0x66747970);
    const bigMdat = new Uint8Array(300_000);
    new DataView(bigMdat.buffer).setUint32(0, bigMdat.length);
    new DataView(bigMdat.buffer).setUint32(4, 0x6d646174);
    const blob = new Blob([ftyp, bigMdat, moov]);
    expect(await extractMp4CreationMs(blob)).toBe(targetMs);

    const v1 = buildMvhd(1);
    expect(parseMvhdCreationMs(v1.buffer as ArrayBuffer)).toBe(targetMs);

    // Unmasked Pixel name passes straight through
    expect(inferPixelVideoLink('PXL_20261116_185612345.mp4', targetMs, 20)).toEqual({ fileName: 'PXL_20261116_185612345.mp4' });
    // Masked Android Photo Picker id uses MP4 timestamp window
    expect(inferPixelVideoLink('1000048291.mp4', targetMs, 20)).toEqual({
      fileName: 'PXL_20261116_1856xx.mp4',
      driveQuery: 'PXL 20261116 1856 type:video',
    });
  });

  it('builds a valid <5s 3-ding WAV alert with leading headroom and full-amplitude bell strikes', () => {
    const { durSec, hits } = ALERT_SPECS.chime;
    expect(durSec).toBeLessThan(5);
    expect(hits).toHaveLength(3);
    const pcm = synthesizeBellPcm(durSec, hits);
    const wav = encodeWav(pcm);
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe('RIFF');
    expect(String.fromCharCode(...wav.slice(8, 12))).toBe('WAVE');
    expect(wav.length).toBe(44 + Math.round(durSec * ALERT_SAMPLE_RATE) * 2);
    const dv = new DataView(wav.buffer, wav.byteOffset);
    expect(dv.getUint32(24, true)).toBe(ALERT_SAMPLE_RATE);
    // Lead-in silence (first 25ms) prevents Bluetooth A2DP wake clip, and peaks reach strong amplitude
    const pdv = new DataView(pcm.buffer, pcm.byteOffset);
    for (let i = 0; i < Math.round(0.025 * ALERT_SAMPLE_RATE); i++) {
      expect(pdv.getInt16(i * 2, true)).toBe(0);
    }
    let peak = 0;
    for (let i = 0; i < pcm.length / 2; i++) {
      peak = Math.max(peak, Math.abs(pdv.getInt16(i * 2, true)));
    }
    expect(peak).toBeGreaterThan(20_000);
  });

  it('resolves hierarchical parentHash toward main menu instead of random back history', () => {
    expect(parentHash('')).toBeNull();
    expect(parentHash('#/')).toBeNull();
    expect(parentHash('#/program')).toBe('#/');
    expect(parentHash('#/strength')).toBe('#/');
    expect(parentHash('#/strength/history')).toBe('#/');
    expect(parentHash('#/strength/progress')).toBe('#/');
    expect(parentHash('#/strength/exercises')).toBe('#/');
    expect(parentHash('#/airbike')).toBe('#/');
    expect(parentHash('#/settings')).toBe('#/');
    expect(parentHash('#/strength/session')).toBe('#/');
    expect(parentHash('#/strength/hr')).toBe('#/');
    expect(parentHash('#/strength/session/s1')).toBe('#/strength/workout/s1');
    expect(parentHash('#/strength/routine/r1')).toBe('#/strength');
    expect(parentHash('#/strength/settings')).toBe('#/strength');
    expect(parentHash('#/strength/summary/s1')).toBe('#/strength/history');
    expect(parentHash('#/strength/workout/s1')).toBe('#/strength/history');
    expect(parentHash('#/strength/exercise/bench')).toBe('#/strength/exercises');
    expect(parentHash('#/airbike/p/1')).toBe('#/');
    expect(parentHash('#/airbike/w/w1')).toBe('#/airbike');
  });

  it('pauses and resumes active lift sessions without counting multi-hour break time', () => {
    const s = sess('pause', 1_000_000, [sx('bench', [set(lb(135), 8, { done: false })])], {
      status: 'active',
      started: true,
      manualStart: false,
      startMs: 1_000_000,
      endMs: null,
    });
    expect(isSessionPaused(s)).toBe(false);
    expect(pauseSessionClock(s, 1_600_000)).toBe(true);
    expect(isSessionPaused(s)).toBe(true);
    // Double-pause is a no-op
    expect(pauseSessionClock(s, 2_000_000)).toBe(false);
    // 2 hours later: active elapsed and stats remain frozen at 600s (10 min)
    expect(elapsedSessionSec(s, 8_800_000)).toBe(600);
    expect(sessionStats({ ...s, endMs: 8_800_000 }, exOf).durationSec).toBe(600);

    // Resume 2 hours later, train 5 more minutes -> 900s total (15 min)
    expect(resumeSessionClock(s, 8_800_000)).toBe(true);
    expect(isSessionPaused(s)).toBe(false);
    expect(s.pausedMs).toBe(7_200_000);
    expect(elapsedSessionSec(s, 9_100_000)).toBe(900);
    expect(sessionStats({ ...s, endMs: 9_100_000 }, exOf).durationSec).toBe(900);

    // Resetting an auto-started paused session clears pause tracking
    expect(pauseSessionClock(s, 9_100_000)).toBe(true);
    expect(resetSessionClock(s, 9_200_000)).toBe(true);
    expect(s.pausedAt ?? null).toBeNull();
    expect(s.pausedMs ?? 0).toBe(0);
    expect(isSessionPaused(s)).toBe(false);
  });

  it('edits active, paused, unstarted, and finished session durations without shifting set timestamps', () => {
    const t0 = 1_700_000_000_000;
    const setAt = t0 + 180_000;
    const sixHoursLater = t0 + 21_600_000;
    const running = sess('edit-run', t0, [sx('bench', [set(lb(135), 8, { done: true, at: setAt })])], {
      status: 'active',
      started: true,
      manualStart: false,
      startMs: t0,
      endMs: null,
    });
    expect(setSessionElapsedSec(running, 2700, sixHoursLater)).toBe(true);
    expect(running.startMs).toBe(t0);
    expect(running.pausedMs).toBe(21_600_000 - 2_700_000);
    expect(running.manualStart).toBe(true);
    expect(running.exs[0].sets[0].at).toBe(setAt);
    expect(elapsedSessionSec(running, sixHoursLater)).toBe(2700);
    expect(elapsedSessionSec(running, sixHoursLater + 60_000)).toBe(2760);
    expect(sessionStats({ ...running, endMs: sixHoursLater }, exOf).durationSec).toBe(2700);

    // Paused session stays paused at the newly edited duration and resumes cleanly from there
    const paused = sess('edit-pause', t0, [sx('bench', [set(lb(135), 8, { done: true, at: setAt })])], {
      status: 'active',
      started: true,
      manualStart: true,
      startMs: t0,
      endMs: null,
      pausedAt: sixHoursLater,
    });
    expect(setSessionElapsedSec(paused, 1800, sixHoursLater + 300_000)).toBe(true);
    expect(isSessionPaused(paused)).toBe(true);
    expect(elapsedSessionSec(paused, sixHoursLater + 600_000)).toBe(1800);
    expect(resumeSessionClock(paused, sixHoursLater + 600_000)).toBe(true);
    expect(elapsedSessionSec(paused, sixHoursLater + 660_000)).toBe(1860);

    // Lengthening active session past wall-clock time shifts startMs earlier and clears pausedMs
    expect(setSessionElapsedSec(running, 25_200, sixHoursLater)).toBe(true);
    expect(running.pausedMs ?? 0).toBe(0);
    expect(running.startMs).toBe(sixHoursLater - 25_200_000);
    expect(elapsedSessionSec(running, sixHoursLater)).toBe(25_200);

    // Unstarted 0:00 session: 0 is no-op, positive duration starts & sets manualStart
    const unstarted = sess('edit-new', t0, [sx('bench', [set(lb(135), 8, { done: false })])], {
      status: 'active',
      started: false,
      startMs: t0,
      endMs: null,
    });
    expect(setSessionElapsedSec(unstarted, 0, t0 + 5_000)).toBe(false);
    expect(setSessionElapsedSec(unstarted, 900, t0 + 10_000)).toBe(true);
    expect(unstarted.started).toBe(true);
    expect(unstarted.manualStart).toBe(true);
    expect(elapsedSessionSec(unstarted, t0 + 10_000)).toBe(900);

    // Finished session: shortening sets pausedMs (keeping startMs/endMs); lengthening updates endMs (keeping startMs)
    const doneSess = sess('edit-done', t0, [sx('bench', [set(lb(185), 5, { done: true, at: setAt })])], {
      status: 'done',
      started: true,
      startMs: t0,
      endMs: sixHoursLater,
    });
    expect(setSessionElapsedSec(doneSess, 3000, sixHoursLater + 100_000)).toBe(true);
    expect(doneSess.startMs).toBe(t0);
    expect(doneSess.endMs).toBe(sixHoursLater);
    expect(sessionStats(doneSess, exOf).durationSec).toBe(3000);

    expect(setSessionElapsedSec(doneSess, 28_800, sixHoursLater + 100_000)).toBe(true);
    expect(doneSess.startMs).toBe(t0);
    expect(doneSess.pausedMs ?? 0).toBe(0);
    expect(doneSess.endMs).toBe(t0 + 28_800_000);
    expect(sessionStats(doneSess, exOf).durationSec).toBe(28_800);
  });

  it('estimates true active duration across trailing and mid-workout idle gaps', () => {
    const t0 = 1_700_000_000_000;
    // 6h forgotten-to-pause trailing gap: last set logged at 35m -> estimates 35m (2100s)
    const trailing = sess('est-trail', t0, [
      sx('bench', [
        set(lb(185), 5, { done: true, at: t0 + 300_000 }),
        set(lb(185), 5, { done: true, at: t0 + 1_200_000 }),
        set(lb(185), 5, { done: true, at: t0 + 2_100_000 }),
      ]),
    ], { status: 'active', started: true, startMs: t0, endMs: null });
    expect(estimateActiveSessionSec(trailing, t0 + 21_600_000)).toBe(2100);

    // 5h mid-workout break (e.g. morning 10m + 5h break + afternoon 10m) -> collapses 5h gap to 120s
    const midGap = sess('est-mid', t0, [
      sx('bench', [
        set(lb(185), 5, { done: true, at: t0 + 300_000 }),
        set(lb(185), 5, { done: true, at: t0 + 600_000 }),
        set(lb(185), 5, { done: true, at: t0 + 18_600_000 }),
        set(lb(185), 5, { done: true, at: t0 + 19_200_000 }),
      ]),
    ], { status: 'active', started: true, startMs: t0, endMs: null });
    expect(estimateActiveSessionSec(midGap, t0 + 19_260_000)).toBe(1380);

    // Normal continuous workout (all set gaps <= 15m) returns actual duration
    const normal = sess('est-norm', t0, [
      sx('bench', [
        set(lb(185), 5, { done: true, at: t0 + 300_000 }),
        set(lb(185), 5, { done: true, at: t0 + 900_000 }),
        set(lb(185), 5, { done: true, at: t0 + 1_500_000 }),
      ]),
    ], { status: 'active', started: true, startMs: t0, endMs: null });
    expect(estimateActiveSessionSec(normal, t0 + 1_620_000)).toBe(1620);

    // No stamped sets returns null
    const noStamps = sess('est-empty', t0, [sx('bench', [set(lb(185), 5, { done: false })])], {
      status: 'active', started: true, startMs: t0, endMs: null,
    });
    expect(estimateActiveSessionSec(noStamps, t0 + 3_600_000)).toBeNull();
  });
});

describe('Boostcamp history seed & SBD rep maxes', () => {
  it('materializes all 136 Boostcamp sessions with valid library slugs, Phase 1 PREVIOUS baselines, and true SBD rep maxes', () => {
    const sessions = buildBoostcampSessions();
    expect(sessions).toHaveLength(136);
    const totalSets = sessions.reduce((acc, s) => acc + s.exs.reduce((nSets, e) => nSets + e.sets.length, 0), 0);
    expect(totalSets).toBeGreaterThan(1800);

    for (const s of sessions) {
      expect(s.status).toBe('done');
      for (const e of s.exs) {
        expect(LIBRARY_BY_ID.has(e.exId), `${s.id}:${e.exId}`).toBe(true);
      }
    }

    for (const r of PHASE1_ROUTINES) {
      for (const it of r.items) {
        expect(LIBRARY_BY_ID.has(it.exId), `missing library slug ${it.exId}`).toBe(true);
        if (it.exId === 'smith-machine-romanian-deadlift') continue;
        expect(previousPerformance(sessions, it.exId), `missing PREVIOUS for ${it.exId}`).not.toBeNull();
      }
    }
    expect(previousPerformance(sessions, 'romanian-deadlift'), 'missing PREVIOUS for historical romanian-deadlift').not.toBeNull();

    // Low Bar Squat merges barbell + low-bar history: latest 185x8 PREVIOUS and full 415 1RM..275x20 records
    const sqPrev = previousPerformance(sessions, 'low-bar-squat')!;
    expect(Math.round(toDisplay(sqPrev.sets[0].w!, 'lb'))).toBe(185);
    expect(sqPrev.sets[0].r).toBe(8);
    const sqBests = repBests(exerciseSessions(sessions, 'low-bar-squat'));
    expect(Math.round(toDisplay(sqBests[1]!.kg, 'lb'))).toBe(415);
    expect(Math.round(toDisplay(sqBests[2]!.kg, 'lb'))).toBe(385);
    expect(Math.round(toDisplay(sqBests[5]!.kg, 'lb'))).toBe(355);
    expect(Math.round(toDisplay(sqBests[8]!.kg, 'lb'))).toBe(340);
    expect(Math.round(toDisplay(sqBests[11]!.kg, 'lb'))).toBe(305);
    expect(Math.round(toDisplay(sqBests[20]!.kg, 'lb'))).toBe(275);
    expect(sqBests[20]!.reps).toBe(20);

    // Barbell Bench Press 1RM..17RM
    const bpBests = repBests(exerciseSessions(sessions, 'barbell-bench-press'));
    expect(Math.round(toDisplay(bpBests[1]!.kg, 'lb'))).toBe(315);
    expect(Math.round(toDisplay(bpBests[2]!.kg, 'lb'))).toBe(295);
    expect(Math.round(toDisplay(bpBests[8]!.kg, 'lb'))).toBe(275);
    expect(Math.round(toDisplay(bpBests[10]!.kg, 'lb'))).toBe(265);
    expect(Math.round(toDisplay(bpBests[17]!.kg, 'lb'))).toBe(225);
    expect(bpBests[17]!.reps).toBe(17);

    // Deadlift 1RM..18RM
    const dlBests = repBests(exerciseSessions(sessions, 'deadlift'));
    expect(Math.round(toDisplay(dlBests[1]!.kg, 'lb'))).toBe(455);
    expect(Math.round(toDisplay(dlBests[3]!.kg, 'lb'))).toBe(385);
    expect(Math.round(toDisplay(dlBests[7]!.kg, 'lb'))).toBe(355);
    expect(Math.round(toDisplay(dlBests[10]!.kg, 'lb'))).toBe(340);
    expect(Math.round(toDisplay(dlBests[12]!.kg, 'lb'))).toBe(310);
    expect(Math.round(toDisplay(dlBests[17]!.kg, 'lb'))).toBe(295);
    expect(Math.round(toDisplay(dlBests[18]!.kg, 'lb'))).toBe(270);

    // Freestyle ramp warm-ups tagged 'w' so working sets stay clean for PREVIOUS
    const nov11 = sessions.find((s) => s.id === 'bc-ZV8fPmFLS1L3jaKs')!;
    const benchNov11 = nov11.exs.find((e) => e.exId === 'barbell-bench-press')!;
    expect(benchNov11.sets[0].kind).toBe('w'); // 45x10
    expect(benchNov11.sets[1].kind).toBe('w'); // 95x10
    expect(benchNov11.sets[2].kind).toBe('n'); // 135x10 working
  });

  it('sorts device videos newest-first by timestamp then filename so newest clips never sink to the bottom', () => {
    const oldest = { fileName: 'PXL_20220501_120000.mp4', fileMs: Date.UTC(2022, 4, 1, 12) };
    const mid = { fileName: 'PXL_20250910_140000.mp4', fileMs: Date.UTC(2025, 8, 10, 14) };
    const tieOlderName = { fileName: 'PXL_20261118_090000.mp4', fileMs: Date.UTC(2026, 10, 18, 14) };
    const newest = { fileName: 'PXL_20261118_140000.mp4', fileMs: Date.UTC(2026, 10, 18, 14) };
    const sorted = sortDeviceVideosNewestFirst([oldest, tieOlderName, mid, newest]);
    expect(sorted.map((v) => v.fileName)).toEqual([
      'PXL_20261118_140000.mp4',
      'PXL_20261118_090000.mp4',
      'PXL_20250910_140000.mp4',
      'PXL_20220501_120000.mp4',
    ]);
  });

  it('resolves mandatory multi-cam defaults, never maps rear-delt-fly to Fly Machine Cam, and falls back to Rack Cam', () => {
    expect(defaultCamsForExercise({ id: 'smith-machine-row', name: 'Smith Machine Row', equip: 'smith' })).toEqual(['Smith Cam', 'Smith Wide']);
    expect(defaultCamsForExercise({ id: 'smith-machine-romanian-deadlift', name: 'Smith Machine Romanian Deadlift', equip: 'smith' })).toEqual(['Smith Cam', 'Smith Wide']);
    expect(defaultCamsForExercise({ id: 'pull-up', name: 'Pull-Up', equip: 'bw' })).toEqual(['Smith Wide', 'Smith Cam']);
    expect(defaultCamsForExercise({ id: 'low-bar-squat', name: 'Low Bar Squat', equip: 'bb' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'barbell-bench-press', name: 'Barbell Bench Press', equip: 'bb' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'deadlift', name: 'Deadlift', equip: 'bb' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'lat-pulldown', name: 'Lat Pulldown', equip: 'cable' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'cable-triceps-pushdown', name: 'Cable Triceps Pushdown', equip: 'cable' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'dumbbell-curl', name: 'Dumbbell Curl', equip: 'db' })).toEqual(['Rack Cam', 'Smith Cam', 'Smith Wide']);
    expect(defaultCamsForExercise({ id: 'chest-fly-machine', name: 'Chest Fly Machine', equip: 'machine' })).toEqual(['Fly Machine Cam']);
    expect(defaultCamsForExercise({ id: 'rear-delt-fly', name: 'Rear Delt Fly', equip: 'db' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'reverse-pec-deck', name: 'Reverse Pec Deck', equip: 'machine' })).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise({ id: 'leg-extension', name: 'Leg Extension', equip: 'machine' })).toEqual(['Leg Machine Cam']);
    expect(defaultCamsForExercise({ id: 'lying-leg-curl', name: 'Lying Leg Curl', equip: 'machine' })).toEqual(['Leg Machine Cam']);
    expect(defaultCamsForExercise({ id: 'custom-stretch', name: 'Custom Stretch', equip: 'other' })).toEqual([DEFAULT_FALLBACK_CAM]);

    expect(inferredCamForExercise({ name: 'Smith Machine Row', equip: 'smith' })).toBe('Smith Cam');
    expect(inferredCamForExercise({ name: 'Dumbbell Curl', equip: 'db' })).toBe('Rack Cam');
    expect(inferredCamForExercise({ name: 'Reverse Pec Deck', equip: 'machine' })).toBe('Rack Cam');

    // Available camera matching falls back to Rack Cam when assigned station is absent
    const chestFly = { id: 'chest-fly-machine', name: 'Chest Fly Machine', equip: 'machine' as const };
    const legExt = { id: 'leg-extension', name: 'Leg Extension', equip: 'machine' as const };
    expect(defaultCamsForExercise(chestFly, ['Rack Cam', 'Smith Cam'])).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise(legExt, ['Rack Cam', 'Smith Cam'])).toEqual(['Rack Cam']);
    expect(defaultCamsForExercise(chestFly, ['Rack Cam', 'Smith Cam', 'Fly Machine Cam'])).toEqual(['Fly Machine Cam']);
    expect(defaultCamsForExercise(legExt, ['Rack Cam', 'Smith Cam', 'Leg Machine Cam'])).toEqual(['Leg Machine Cam']);

    // Mandatory camsForExercise precedence + legacy 'auto' stripping -> never empty
    const curl = { id: 'dumbbell-curl', name: 'Dumbbell Curl', equip: 'db' as const };
    const bench = { id: 'barbell-bench-press', name: 'Barbell Bench Press', equip: 'bb' as const };
    expect(normalizeCamList(['auto', 'Smith Cam', 'smith cam', ''])).toEqual(['Smith Cam']);
    expect(camsForExercise(curl, null)).toEqual(['Rack Cam', 'Smith Cam', 'Smith Wide']);
    expect(camsForExercise(curl, { cams: ['Rack Cam'] })).toEqual(['Rack Cam']);
    expect(camsForExercise(curl, { cams: ['Rack Cam'] }, { cams: ['Smith Cam', 'Smith Wide'] })).toEqual(['Smith Cam', 'Smith Wide']);
    expect(camsForExercise(bench, { camPref: 'auto' })).toEqual(['Rack Cam']);
    expect(preferredCamForExercise(bench, { camPref: 'auto' })).toBe('Rack Cam');
    expect(preferredCamForExercise(bench, { camPref: 'Low Knee Cam' })).toBe('Low Knee Cam');
  });

  it('seeds all 19 Phase 1 exercise camera mappings, swaps Lower 1 to Smith RDL, and migrates legacy ExRow/Routine data', () => {
    const phase1Ids = Object.keys(PHASE1_EXERCISE_CAMS);
    expect(phase1Ids).toHaveLength(19);
    const seedById = new Map(SEED_EX_ROWS.map((r) => [r.id, r]));
    for (const id of phase1Ids) {
      expect(PHASE1_EXERCISE_CAMS[id].length).toBeGreaterThanOrEqual(1);
      expect(seedById.get(id)?.cams).toEqual(PHASE1_EXERCISE_CAMS[id]);
    }
    expect(seedById.get('rear-delt-fly')?.cams).toEqual(['Rack Cam']);
    expect(seedById.get('chest-fly-machine')?.cams).toEqual(['Fly Machine Cam']);
    expect(seedById.get('smith-machine-romanian-deadlift')?.cams).toEqual(['Smith Cam']);
    expect(seedById.get('triceps-isometric-hold')?.restSec).toBe(45);

    const lower1Seed = PHASE1_ROUTINES.find((r) => r.id === 'day-lower-1')!;
    expect(lower1Seed.items.map((it) => it.exId)).toContain('smith-machine-romanian-deadlift');
    expect(lower1Seed.items.map((it) => it.exId)).not.toContain('romanian-deadlift');
    expect(lower1Seed.items[1].rx).toBeTruthy();

    const routines = new Map<string, Routine>([
      ['day-lower-1', {
        id: 'day-lower-1',
        name: 'Lower 1 · Squat & RDL',
        createdMs: 1,
        order: 0,
        items: [
          { exId: 'low-bar-squat', sets: 3 },
          { exId: 'romanian-deadlift', sets: 3 },
          { exId: 'standing-calf-raise', sets: 3, ss: true },
          { exId: 'cable-crunch', sets: 3 },
        ],
      }],
    ]);
    const rows = new Map<string, ExRow>([
      ['barbell-bench-press', { id: 'barbell-bench-press', camPref: 'auto' }],
      ['custom-move', { id: 'custom-move', custom: true, name: 'Custom Move', type: 'weight', equip: 'other', primary: ['ch'], secondary: [], camPref: 'Smith Wide' }],
    ]);
    const mig = applyCamsAndSmithRdlMigration({
      routines,
      rows,
      resolveEx: (id) => LIBRARY_BY_ID.get(id),
      prevFor: () => null,
      bestE1For: () => 225 * KG_PER_LB,
      unit: 'lb',
    });
    expect(mig.changedRoutines).toHaveLength(1);
    expect(routines.get('day-lower-1')!.items[1].exId).toBe('smith-machine-romanian-deadlift');
    expect(rows.get('barbell-bench-press')!.cams).toEqual(['Rack Cam']);
    expect(rows.get('barbell-bench-press')!.camPref).toBeUndefined();
    expect(rows.get('custom-move')!.cams).toEqual(['Smith Wide']);
    expect(rows.get('rear-delt-fly')!.cams).toEqual(['Rack Cam']);
  });
});

describe('Wave 2 lifting logic & PR parity', () => {
  it('protects prescribed ramp sets with rxLock, unlocks only the edited set, and clears rx on swapExercise', () => {
    const se = buildExercise('barbell-bench-press', 3, null, false, undefined, {
      rx: {
        setsRx: [
          { loadLb: 135, reps: 8 },
          { loadLb: 155, reps: 6 },
          { loadLb: 175, reps: 4 },
        ],
      },
      unit: 'lb',
    });
    expect(se.sets.every((s) => s.rxLock === true)).toBe(true);
    editWeight(se, 0, fromDisplay(145, 'lb'));
    expect(se.sets[0].rxLock).toBeUndefined();
    expect(Math.round(toDisplay(se.sets[1].w!, 'lb'))).toBe(155);
    expect(Math.round(toDisplay(se.sets[2].w!, 'lb'))).toBe(175);
    expect(se.sets[1].rxLock).toBe(true);

    swapExercise(se, 'dumbbell-bench-press', null);
    expect(se.rx).toBeUndefined();
    expect(se.sets.every((s) => !s.rxLock)).toBe(true);
  });

  it('clears orphaned superset flags in removeExerciseAt and pruneUnlogged', () => {
    const exA = buildExercise('barbell-bench-press', 2, null, true);
    const exB = buildExercise('smith-machine-row', 2, null, false);
    const exs = [exA, exB];
    removeExerciseAt(exs, 1);
    expect(exs).toHaveLength(1);
    expect(exs[0].ss).toBeUndefined();

    const s: LiftSession = {
      id: 'sess-ss',
      name: 'Upper',
      routineId: null,
      eventId: null,
      startMs: 1000,
      endMs: null,
      status: 'active',
      rpe: null,
      bwKg: null,
      exs: [
        { uid: 'u1', exId: 'barbell-bench-press', ss: true, sets: [{ ...newSet('n', lb(185), 8), done: true }] },
        { uid: 'u2', exId: 'smith-machine-row', sets: [newSet('n', lb(135), 10)] },
      ],
    };
    pruneUnlogged(s);
    expect(s.exs).toHaveLength(1);
    expect(s.exs[0].ss).toBeUndefined();
  });

  it('previousPerformance prefers finished sessions with logged working sets over warmup-only sessions', () => {
    const olderWorking: LiftSession = {
      id: 's1', name: 'Push', routineId: null, eventId: null, startMs: 1000, endMs: 2000, status: 'done', rpe: 8, bwKg: null,
      exs: [{ uid: 'e1', exId: 'barbell-bench-press', sets: [{ ...newSet('n', lb(185), 8), done: true }] }],
    };
    const newerWarmupOnly: LiftSession = {
      id: 's2', name: 'Push', routineId: null, eventId: null, startMs: 3000, endMs: 4000, status: 'done', rpe: 6, bwKg: null,
      exs: [{ uid: 'e2', exId: 'barbell-bench-press', sets: [{ ...newSet('w', lb(95), 8), done: true }] }],
    };
    const prev = previousPerformance([olderWorking, newerWarmupOnly], 'barbell-bench-press');
    expect(prev?.uid).toBe('e1');
  });

  it('computeSessionPRs + computePRBaseline matches computePRs for the latest session', () => {
    const exOf = (id: string) => LIBRARY_BY_ID.get(id);
    const s1: LiftSession = {
      id: 's1', name: 'Upper 1', routineId: null, eventId: null, startMs: 1000, endMs: 2000, status: 'done', rpe: 8, bwKg: null,
      exs: [{
        uid: 'e1', exId: 'barbell-bench-press',
        sets: [
          { ...newSet('w', lb(135), 8), id: 'w1', done: true },
          { ...newSet('n', lb(185), 8), id: 'a1', done: true },
        ],
      }],
    };
    const s2: LiftSession = {
      id: 's2', name: 'Upper 2', routineId: null, eventId: null, startMs: 5000, endMs: 6000, status: 'done', rpe: 8, bwKg: null,
      exs: [{
        uid: 'e2', exId: 'barbell-bench-press',
        sets: [
          { ...newSet('n', lb(195), 8), id: 'b1', done: true },
          { ...newSet('n', lb(180), 8), id: 'b2', done: true },
        ],
      }],
    };
    const full = computePRs([s1, s2], exOf);
    const base = computePRBaseline([s1], exOf);
    const fast = computeSessionPRs(s2, base, exOf);
    expect([...fast.keys()]).toEqual(['b1']);
    expect([...fast.entries()]).toEqual([...full.entries()].filter(([id]) => id.startsWith('b')));
  });
});
