import { describe, expect, it } from 'vitest';
import { parentHash } from '../src/nav';
import { LIBRARY_BY_ID } from '../src/lifts/library';
import { PHASE1_ROUTINES } from '../src/lifts/seed';
import { defaultQueueState } from '../src/domain/plan';
import { KG_PER_LB } from '../src/lifts/settings';
import {
  advanceCursorAfterLog, applyAddSet, applyAddSetToLastEx, applyDeleteEmptySets,
  jumpToExerciseCursor, peekNextTargetSet, plannedSetSequence, resolveTargetSet, stepExerciseCursor,
} from '../src/lifts/cursor';
import { parseVoiceTranscript } from '../src/lifts/voiceParser';
import { buildWallboardSnapshot } from '../src/wallboard/snapshot';
import type { Exercise, LiftSession, LiftSet, SessionEx } from '../src/lifts/types';

const exOf = (id: string): Exercise =>
  LIBRARY_BY_ID.get(id) ?? { id, name: id, equip: 'other', type: 'weight', primary: ['ch'], secondary: [] };

let seq = 0;
const mkSet = (wLb: number | null, r: number | null, o: Partial<LiftSet> = {}): LiftSet => ({
  id: `vs${++seq}`,
  w: wLb == null ? null : wLb * KG_PER_LB,
  r,
  done: false,
  kind: 'n',
  ...o,
});

const mkUpper1 = (): SessionEx[] => [
  { uid: 'u_iso', exId: 'triceps-isometric-hold', sets: [mkSet(50, 45), mkSet(50, 45)] },
  { uid: 'u_bench', exId: 'barbell-bench-press', ss: true, sets: [mkSet(225, 8), mkSet(225, 8), mkSet(225, 8)] },
  { uid: 'u_row', exId: 'smith-machine-row', sets: [mkSet(100, 15), mkSet(100, 15), mkSet(100, 15)] },
  { uid: 'u_ohp', exId: 'seated-dumbbell-shoulder-press', ss: true, sets: [mkSet(50, 10), mkSet(50, 10)] },
  { uid: 'u_pull', exId: 'pull-up', sets: [mkSet(0, 10), mkSet(0, 10)] },
];

describe('superset cursor & alternation', () => {
  it('interleaves superset pairs round-by-round (1 -> 2A -> 2B -> 2A -> 2B -> 3A -> 3B)', () => {
    const exs = mkUpper1();
    const order = plannedSetSequence(exs).map((p) => `${exs[p.exIdx].uid}:${p.setIdx}`);
    expect(order).toEqual([
      'u_iso:0', 'u_iso:1',
      'u_bench:0', 'u_row:0',
      'u_bench:1', 'u_row:1',
      'u_bench:2', 'u_row:2',
      'u_ohp:0', 'u_pull:0',
      'u_ohp:1', 'u_pull:1',
    ]);

    // Finish Iso Holds so 2A Bench Set 1 is UP NOW and 2B Row Set 1 is UP NEXT
    exs[0].sets[0].done = true;
    exs[0].sets[1].done = true;
    const p1 = resolveTargetSet(exs, null)!;
    expect(p1.label).toBe('2A');
    expect(p1.setNumber).toBe(1);
    const n1 = peekNextTargetSet(exs, p1)!;
    expect(n1.label).toBe('2B');
    expect(n1.setNumber).toBe(1);

    // Logging 2A Set 1 advances cursor cleanly to 2B Set 1, then back to 2A Set 2
    exs[1].sets[0].done = true;
    const cur2 = advanceCursorAfterLog(exs, { exIdx: 1, setIdx: 0 });
    const p2 = resolveTargetSet(exs, cur2)!;
    expect(p2.label).toBe('2B');
    expect(p2.setNumber).toBe(1);

    exs[2].sets[0].done = true;
    const cur3 = advanceCursorAfterLog(exs, { exIdx: 2, setIdx: 0 });
    const p3 = resolveTargetSet(exs, cur3)!;
    expect(p3.label).toBe('2A');
    expect(p3.setNumber).toBe(2);
  });

  it('interleaves warm-up sets before working sets inside a superset', () => {
    const exs: SessionEx[] = [
      { uid: 'a', exId: 'barbell-bench-press', ss: true, sets: [mkSet(135, 8, { kind: 'w' }), mkSet(225, 8)] },
      { uid: 'b', exId: 'smith-machine-row', sets: [mkSet(70, 10, { kind: 'w' }), mkSet(100, 12)] },
    ];
    expect(plannedSetSequence(exs)).toEqual([
      { exIdx: 0, setIdx: 0 },
      { exIdx: 1, setIdx: 0 },
      { exIdx: 0, setIdx: 1 },
      { exIdx: 1, setIdx: 1 },
    ]);
  });

  it('supports next/previous exercise stepping and jumping to spoken label or name', () => {
    const exs = mkUpper1();
    const cur0 = resolveTargetSet(exs, { exUid: 'u_bench', setId: exs[1].sets[0].id });
    const step1 = stepExerciseCursor(exs, cur0, 1);
    expect(resolveTargetSet(exs, step1)!.label).toBe('2B');
    const step2 = stepExerciseCursor(exs, resolveTargetSet(exs, step1), 1);
    expect(resolveTargetSet(exs, step2)!.label).toBe('3A');

    // Jump by spoken label ("two b") or exercise name ("pull ups")
    const j2b = jumpToExerciseCursor(exs, 'two b', exOf);
    expect(resolveTargetSet(exs, j2b)!.label).toBe('2B');
    const jPull = jumpToExerciseCursor(exs, 'pull ups', exOf);
    const pPull = resolveTargetSet(exs, jPull)!;
    expect(pPull.label).toBe('3B');
    expect(pPull.setNumber).toBe(1);

    // Logging 3B Set 1 moves sequentially to the next round (3A Set 2), and wraps back to 2A later
    exs[4].sets[0].done = true;
    const afterPull = advanceCursorAfterLog(exs, { exIdx: 4, setIdx: 0 });
    expect(resolveTargetSet(exs, afterPull)!.label).toBe('3A');
  });

  it('handles add set, add set to last exercise (focused), and delete empty sets with advance', () => {
    const exs = mkUpper1();
    exs[0].sets.forEach((s) => { s.done = true; });
    exs[1].sets.forEach((s) => { s.done = true; });
    exs[2].sets[0].done = true;
    // Current target is 2B Row Set 2, last logged was 2B Row (or user switched after 2A Bench)
    let cur = { exUid: 'u_row', setId: exs[2].sets[1].id };

    // "armory add set" adds set 4 to 2B Row while keeping focus on open Set 2
    const rAdd = applyAddSet(exs, cur, 'u_row', () => null);
    expect(rAdd.changed).toBe(true);
    expect(exs[2].sets).toHaveLength(4);
    expect(rAdd.cursor).toEqual({ exUid: 'u_row', setId: exs[2].sets[1].id });

    // User just finished 2A Bench and cursor advanced to 2B Row, then says "armory add set to last exercise"
    const rPrev = applyAddSetToLastEx(exs, rAdd.cursor, 'u_bench', () => null);
    expect(rPrev.changed).toBe(true);
    expect(exs[1].sets).toHaveLength(4);
    expect(rPrev.added).toBeDefined();
    expect(rPrev.cursor).toEqual({ exUid: 'u_bench', setId: rPrev.added!.id });
    expect(resolveTargetSet(exs, rPrev.cursor)!.label).toBe('2A');
    expect(resolveTargetSet(exs, rPrev.cursor)!.setId).toBe(rPrev.added!.id);

    // Move back to 2B Row and say "armory delete empty sets": removes 3 empty sets on Row ONLY, keeps Set 1, advances to 3A
    const rDel = applyDeleteEmptySets(exs, { exUid: 'u_row', setId: exs[2].sets[1].id });
    expect(rDel.changed).toBe(true);
    expect(rDel.deletedCount).toBe(3);
    expect(exs[2].sets).toHaveLength(1);
    expect(exs[2].sets[0].done).toBe(true);
    // Remaining empty set on 2A Bench (the newly added Set 4!) or next open exercise is focused cleanly
    expect(resolveTargetSet(exs, rDel.cursor)!.label).toBe('3A');

    // Delete empty sets when all sets of an exercise are unlogged drops that card cleanly and advances
    const rDropAll = applyDeleteEmptySets(exs, { exUid: 'u_ohp', setId: exs[3].sets[0].id });
    expect(rDropAll.changed).toBe(true);
    expect(rDropAll.deletedCount).toBe(2);
    expect(exs.some((e) => e.uid === 'u_ohp')).toBe(false);
    expect(resolveTargetSet(exs, rDropAll.cursor)!.exUid).toBe('u_pull');
  });
});

describe('voice transcript parser', () => {
  it('parses wake-word gated lift, navigation, rest & hold commands and rejects ambient noise', () => {
    expect(parseVoiceTranscript('225 for 8')).toBeNull();
    expect(parseVoiceTranscript('turn the volume up')).toBeNull();

    expect(parseVoiceTranscript('Armory 225 for 8')).toEqual({ kind: 'log_current', weight: 225, reps: 8 });
    expect(parseVoiceTranscript('armory - two twenty five by eight')).toEqual({ kind: 'log_current', weight: 225, reps: 8 });
    expect(parseVoiceTranscript('Hey Armory, check')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('armory 185 four 10 rpe 8.5')).toEqual({ kind: 'log_current', weight: 185, reps: 10, rpe: 8.5 });
    expect(parseVoiceTranscript('armory bodyweight for 12')).toEqual({ kind: 'log_current', bodyweight: true, weight: 0, reps: 12 });
    expect(parseVoiceTranscript('armory 45 seconds')).toEqual({ kind: 'log_current', seconds: 45 });
    expect(parseVoiceTranscript('armory next exercise')).toEqual({ kind: 'next_ex' });
    expect(parseVoiceTranscript('armory 2b')).toEqual({ kind: 'jump_ex', target: '2b' });
    expect(parseVoiceTranscript('armory go to pull ups')).toEqual({ kind: 'jump_ex', target: 'pull ups' });
    expect(parseVoiceTranscript('armory skip rest')).toEqual({ kind: 'rest_skip' });
    expect(parseVoiceTranscript('armory add 30 seconds')).toEqual({ kind: 'rest_add', deltaSec: 30 });
    expect(parseVoiceTranscript('armory start hold 45')).toEqual({ kind: 'start_hold', seconds: 45 });
    expect(parseVoiceTranscript('armory add set')).toEqual({ kind: 'add_set' });
    expect(parseVoiceTranscript('armory one more set')).toEqual({ kind: 'add_set' });
    expect(parseVoiceTranscript('armory add set to last exercise')).toEqual({ kind: 'add_set_prev_ex' });
    expect(parseVoiceTranscript('armory one more set on previous exercise')).toEqual({ kind: 'add_set_prev_ex' });
    expect(parseVoiceTranscript('armory delete empty sets')).toEqual({ kind: 'delete_empty_sets' });
    expect(parseVoiceTranscript('armory undo')).toEqual({ kind: 'undo' });

    // Two-beat wake flow
    expect(parseVoiceTranscript('Armory')).toEqual({ kind: 'arm_wake' });
    expect(parseVoiceTranscript('one thirty five for twelve', { armed: true })).toEqual({ kind: 'log_current', weight: 135, reps: 12 });
  });
});

describe('wallboard snapshot & routing', () => {
  it('builds Lift mode with 2A up now, 2B up next, HR readiness & voice confirmation', () => {
    const exs = mkUpper1();
    exs[0].sets[0].done = true;
    exs[0].sets[1].done = true;
    const session: LiftSession = {
      id: 's1',
      name: 'Upper 1',
      routineId: null,
      eventId: null,
      startMs: 1_000_000,
      endMs: null,
      status: 'active',
      started: true,
      exs,
      rpe: null,
      bwKg: 88.5,
    };
    const settings = {
      hrMax: 180,
      recoveryHrPct: 72,
      voiceEnabled: true,
      voiceWakeWord: 'armory',
      queue: defaultQueueState(),
    };
    const now = 2_000_000;
    const snapReady = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 118, fresh: true, recentPoints: [{ at: now - 30_000, hr: 145 }, { at: now, hr: 118 }] },
      voice: { listening: true, armed: false, banner: { text: '✓ 2B Row · 100 lb × 15', kind: 'ok', atMs: now - 1000 }, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: session,
      elapsedSec: 600,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      noteOf: (id) => (id === 'barbell-bench-press' ? 'Pins on hole 5' : ''),
      prevOf: () => null,
    });
    expect(snapReady.mode).toBe('lift');
    expect(snapReady.hr.readiness).toBe('ready');
    expect(snapReady.hr.readyLimitBpm).toBe(130);
    expect(snapReady.hr.drop60).toBe(27);
    expect(snapReady.voice.banner?.text).toContain('100 lb');
    expect(snapReady.lift!.current!.label).toBe('2A');
    expect(snapReady.lift!.current!.note).toBe('Pins on hole 5');
    expect(snapReady.lift!.next!.label).toBe('2B');

    // Recovering state when HR > 130 and no active rest timer
    const snapRec = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 148, fresh: true, recentPoints: [{ at: now, hr: 148 }] },
      voice: { listening: true, armed: true, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: session,
      elapsedSec: 600,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      noteOf: () => '',
      prevOf: () => null,
    });
    expect(snapRec.hr.readiness).toBe('recovering');
    expect(snapRec.voice.armed).toBe(true);
    expect(parentHash('#/wallboard')).toBe('#/settings');
  });

  it('builds Cardio & Standby modes cleanly', () => {
    const settings = {
      hrMax: 180,
      recoveryHrPct: 72,
      voiceEnabled: false,
      voiceWakeWord: 'armory',
      queue: defaultQueueState(),
    };
    const now = Date.UTC(2026, 10, 17, 17, 0, 0);
    const cardio = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 162, fresh: true, recentPoints: [{ at: now, hr: 162 }] },
      voice: { listening: false, armed: false, banner: null, cursor: null },
      cardioSnap: {
        title: 'Norwegian 4×4', phase: 'WORK', phaseTone: 'work', repText: 'REP 2/4',
        countdownText: '2:15', targetText: '52.5 RPM · 202 W', watts: 205, targetWatts: 202,
        rpm: 52.8, targetRpm: 52.5, elapsedSec: 720, kcal: 180, z5Sec: 210, paused: false,
      },
      holdSnap: null,
      activeSession: undefined,
      elapsedSec: 0,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      noteOf: () => '',
      prevOf: () => null,
    });
    expect(cardio.mode).toBe('cardio');
    expect(cardio.cardio!.phase).toBe('WORK');

    const standby = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 0, fresh: false, recentPoints: [] },
      voice: { listening: false, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: undefined,
      elapsedSec: 0,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      noteOf: () => '',
      prevOf: () => null,
    });
    expect(standby.mode).toBe('standby');
    expect(standby.standby!.weekLabel).toContain('Wk 1');
  });
});
