import { describe, expect, it } from 'vitest';
import { ALERT_SAMPLE_RATE, ALERT_SPECS, encodeWav, synthesizeBellPcm } from '../src/audio/audio';
import { parentHash } from '../src/nav';
import {
  MAX_RM, PR_MAX_REPS, computePRs, e1rm, e1rmTrend, exerciseSessions, muscleSets, previousPerformance, repBests, sessionStats,
  setVolumeKg, weekStart, addWeeks, DAY_MS,
} from '../src/lifts/calc';
import { LIBRARY, LIBRARY_BY_ID } from '../src/lifts/library';
import {
  addSet, buildExercise, canLog, collectUnfinishedItems, driveUrlFor, editReps, editWeight, effectiveRestAuto, elapsedSessionSec,
  hasSessionActivity, isSessionPaused, isSessionStarted, labels, midSuperset, newSet, pauseSessionClock,
  prevSetFor, pruneUnlogged, removeLastSet, resetSessionClock, resumeSessionClock, routineFromSession,
  startSessionClock, supersetGroups, swapExercise, toggleDone,
} from '../src/lifts/logic';
import { extractMp4CreationMs, inferPixelVideoLink, parseMvhdCreationMs } from '../src/lifts/mp4';
import { EQUIPS, GROUPS } from '../src/lifts/muscles';
import { PHASE1_ROUTINES, matchRoutineByTitle } from '../src/lifts/seed';
import { KG_PER_LB, fmtNum, fromDisplay, stepsFor, toDisplay } from '../src/lifts/settings';
import { condenseSets, intervalsDescription, shortLine } from '../src/lifts/summary';
import { MUSCLES, type Exercise, type LiftSession, type LiftSet, type SessionEx } from '../src/lifts/types';

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
  it('round-trips pounds without float noise and provides custom +/-5/+/-10 weight steps', () => {
    for (const v of [45, 135, 137.5, 225, 315, 2.5]) expect(toDisplay(fromDisplay(v, 'lb'), 'lb')).toBe(v);
    expect(fmtNum(toDisplay(lb(135), 'lb'))).toBe('135');
    expect(toDisplay(100, 'kg')).toBe(100);
    expect(stepsFor('lb')).toEqual([-10, -5, 5, 10]);
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
});
