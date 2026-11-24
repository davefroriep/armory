import { describe, expect, it } from 'vitest';
import { parentHash } from '../src/nav';
import { LIBRARY_BY_ID } from '../src/lifts/library';
import { PHASE1_ROUTINES } from '../src/lifts/seed';
import { defaultQueueState } from '../src/domain/plan';
import { DEFAULTS, ROOM_VOICE_MIG_KEY, migrateRoomVoiceDefault } from '../src/store/settings';
import { KG_PER_LB } from '../src/lifts/settings';
import {
  advanceCursorAfterLog, applyAddSet, applyAddSetToLastEx, applyDeleteEmptySets,
  jumpToExerciseCursor, peekNextTargetSet, plannedSetSequence, resolveTargetSet, stepExerciseCursor,
} from '../src/lifts/cursor';
import { buildExercise, driveUrlFor, secondsSinceLastSet } from '../src/lifts/logic';
import {
  AB_SHOOTOUT_PHRASES, SET_CONFIRM_TAGS, defaultReadbackForIntent, hasCommandTailAfterWake, hasWakeWord,
  parseVoiceTranscript, pickSetConfirmTag, voiceIntentKey, voiceTokenMatchPct,
} from '../src/lifts/voiceParser';
import { prescriptionFor, resolvePrescription } from '../src/lifts/prescriptions';
import { TV_H, TV_W, WALLBOARD_HTML, computeTvPreviewFit } from '../src/wallboard/page';
import { buildWallboardSnapshot, isWallboardWorkoutActive, type WallboardReplayState } from '../src/wallboard/snapshot';
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

  it('keeps straight-set cursor on current exercise (e.g. Deadlifts) until every set is done before moving to Bulgarians', () => {
    const lower2: SessionEx[] = [
      { uid: 'u_dl', exId: 'deadlift', sets: [mkSet(135, 10), mkSet(225, 10), mkSet(225, 10)] },
      { uid: 'u_bss', exId: 'bulgarian-split-squat', sets: [mkSet(30, 10), mkSet(30, 10), mkSet(30, 10)] },
      { uid: 'u_calf', exId: 'standing-calf-raise', ss: true, sets: [mkSet(140, 12), mkSet(140, 12)] },
      { uid: 'u_crunch', exId: 'cable-crunch', sets: [mkSet(80, 15), mkSet(80, 15)] },
    ];
    // Even if Set 3 on Deadlift is checked while Set 1 or Set 2 is still open, cursor stays on Deadlift (NOT Bulgarians!)
    lower2[0].sets[0].done = true;
    lower2[0].sets[2].done = true;
    const stayOnDl = advanceCursorAfterLog(lower2, { exIdx: 0, setIdx: 2 });
    const dlPtr = resolveTargetSet(lower2, stayOnDl)!;
    expect(dlPtr.exUid).toBe('u_dl');
    expect(dlPtr.setId).toBe(lower2[0].sets[1].id);
    expect(dlPtr.label).toBe('1');

    // Once all 3 Deadlift sets are done, cursor moves to Bulgarian Split Squat Set 1
    lower2[0].sets[1].done = true;
    const moveToBss = advanceCursorAfterLog(lower2, { exIdx: 0, setIdx: 1 });
    const bssPtr = resolveTargetSet(lower2, moveToBss)!;
    expect(bssPtr.exUid).toBe('u_bss');
    expect(bssPtr.label).toBe('2');
    expect(bssPtr.setNumber).toBe(1);
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
    expect(parseVoiceTranscript('armory finish workout')).toEqual({ kind: 'finish_workout' });
    expect(parseVoiceTranscript('Armory camera')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record set')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record this')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record me')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record this set')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record my set')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory film this')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory film me')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory film this set')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory film my set')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory start recording')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory stop recording')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory start camera')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('Armory record smith')).toEqual({ kind: 'cam_arm', camera: 'Smith Cam' });
    expect(parseVoiceTranscript('armory record this on smith')).toEqual({ kind: 'cam_arm', camera: 'Smith Cam' });
    expect(parseVoiceTranscript('armory film me on rack')).toEqual({ kind: 'cam_arm', camera: 'Rack Cam' });
    expect(parseVoiceTranscript('armory record this lets go')).toEqual({ kind: 'cam_arm' });
    expect(parseVoiceTranscript('armory record this on smith lets go')).toEqual({ kind: 'cam_arm', camera: 'Smith Cam' });
    expect(parseVoiceTranscript('Armory record smith wide')).toEqual({ kind: 'cam_arm', camera: 'Smith Wide' });
    expect(parseVoiceTranscript('Armory record fly machine')).toEqual({ kind: 'cam_arm', camera: 'Fly Machine Cam' });
    expect(parseVoiceTranscript('armory rack camera')).toEqual({ kind: 'cam_arm', camera: 'Rack Cam' });
    expect(parseVoiceTranscript('Armory clip that')).toEqual({ kind: 'cam_clip_last' });
    expect(parseVoiceTranscript('armory save clip')).toEqual({ kind: 'cam_clip_last' });
    expect(parseVoiceTranscript('armory clip smith')).toEqual({ kind: 'cam_clip_last', camera: 'Smith Cam' });
    expect(parseVoiceTranscript('armory clip leg machine')).toEqual({ kind: 'cam_clip_last', camera: 'Leg Machine Cam' });
    expect(parseVoiceTranscript('armory undo')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory, back')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory go back')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory back one')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory undo that')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory undo last set')).toEqual({ kind: 'undo' });
    expect(parseVoiceTranscript('Armory back exercise')).toEqual({ kind: 'prev_ex' });
    expect(parseVoiceTranscript('Armory go to back extension')).toEqual({ kind: 'jump_ex', target: 'back extension' });
    expect(parseVoiceTranscript('Armory back squat')).toBeNull();

    // Two-beat wake flow & natural dumbbell/same shortcuts
    expect(parseVoiceTranscript('Armory')).toEqual({ kind: 'arm_wake' });
    expect(parseVoiceTranscript('one thirty five for twelve', { armed: true })).toEqual({ kind: 'log_current', weight: 135, reps: 12 });
    expect(parseVoiceTranscript('armory same')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('armory repeat')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('armory 50s for 10')).toEqual({ kind: 'log_current', weight: 50, reps: 10 });
    expect(parseVoiceTranscript('armory fifties for ten')).toEqual({ kind: 'log_current', weight: 50, reps: 10 });
    expect(parseVoiceTranscript('armory thirty fives for twelve')).toEqual({ kind: 'log_current', weight: 35, reps: 12 });
    expect(parseVoiceTranscript('armory 45s')).toEqual({ kind: 'log_current', seconds: 45 });
    expect(parseVoiceTranscript('Armory, Done!')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory, 135 for 12')).toEqual({ kind: 'log_current', weight: 135, reps: 12 });
    expect(parseVoiceTranscript('Armory, for 15')).toEqual({ kind: 'log_current', reps: 15 });
    expect(parseVoiceTranscript("Armory's 225 for 8")).toEqual({ kind: 'log_current', weight: 225, reps: 8 });
    expect(parseVoiceTranscript('army')).toEqual({ kind: 'arm_wake' });
    expect(parseVoiceTranscript('amory done')).toEqual({ kind: 'log_current' });
    expect(hasWakeWord('Armory 22', 'armory')).toBe(true);
    expect(hasWakeWord("Armory's", 'armory')).toBe(true);
    expect(hasWakeWord('just talking', 'armory')).toBe(false);
  });

  it('supports by/for/did reps-only phrases, completion synonyms, custom aliases, and spoken readback helpers', () => {
    // Reps-only variations & homophones
    expect(parseVoiceTranscript('Armory for 10')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory by 10')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory buy 10')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory four 10')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory four ten')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory did 5')).toEqual({ kind: 'log_current', reps: 5 });
    expect(parseVoiceTranscript('Armory got 8')).toEqual({ kind: 'log_current', reps: 8 });
    expect(parseVoiceTranscript('Armory hit 6')).toEqual({ kind: 'log_current', reps: 6 });
    expect(parseVoiceTranscript('Armory x 8')).toEqual({ kind: 'log_current', reps: 8 });
    expect(parseVoiceTranscript('Armory times 10')).toEqual({ kind: 'log_current', reps: 10 });
    expect(parseVoiceTranscript('Armory by 5 rpe 8.5')).toEqual({ kind: 'log_current', reps: 5, rpe: 8.5 });
    // Ensure 400+ weights like "four ten for five" don't break
    expect(parseVoiceTranscript('Armory four ten for five')).toEqual({ kind: 'log_current', weight: 410, reps: 5 });

    // Natural set-completion synonyms
    expect(parseVoiceTranscript('Armory crushed it')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory nailed it')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory good set')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory boom')).toEqual({ kind: 'log_current' });

    // Punctuated Whisper transcripts & trailing TV/music bleed under noisy gym rooms
    expect(parseVoiceTranscript('Armory, 225 for five.')).toEqual({ kind: 'log_current', weight: 225, reps: 5 });
    expect(parseVoiceTranscript('Armory. 135. For ten!')).toEqual({ kind: 'log_current', weight: 135, reps: 10 });
    expect(parseVoiceTranscript('Armory, 22.5 for 8 rpe 8.5.')).toEqual({ kind: 'log_current', weight: 22.5, reps: 8, rpe: 8.5 });
    expect(parseVoiceTranscript('Armory 225 for 10 see you on the screen')).toEqual({ kind: 'log_current', weight: 225, reps: 10 });
    expect(parseVoiceTranscript('Armory skip rest please')).toEqual({ kind: 'rest_skip' });
    expect(parseVoiceTranscript('Armory please check thanks')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory done alright guys')).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armery 185 for 8')).toEqual({ kind: 'log_current', weight: 185, reps: 8 });
    expect(parseVoiceTranscript('Armory purple banana')).toBeNull();

    // Custom user variations
    const customAliases = {
      check_set: ['send it'],
      reps_only: ['knocked out #', 'pushed'],
      undo: ['my bad'],
    };
    expect(parseVoiceTranscript('Armory send it', { customAliases })).toEqual({ kind: 'log_current' });
    expect(parseVoiceTranscript('Armory knocked out 12', { customAliases })).toEqual({ kind: 'log_current', reps: 12 });
    expect(parseVoiceTranscript('Armory pushed 8', { customAliases })).toEqual({ kind: 'log_current', reps: 8 });
    expect(parseVoiceTranscript('Armory my bad', { customAliases })).toEqual({ kind: 'undo' });

    // Wake tail gating for "Didn't catch that" & concise spoken readback strings
    expect(hasCommandTailAfterWake('just chatting in the gym', 'armory', false)).toBe(false);
    expect(hasCommandTailAfterWake('Armory', 'armory', false)).toBe(false);
    expect(hasCommandTailAfterWake('Armory purple banana', 'armory', false)).toBe(true);
    expect(hasCommandTailAfterWake('purple banana', 'armory', true)).toBe(true);

    expect(defaultReadbackForIntent({ kind: 'arm_wake' })).toBeNull();
    expect(defaultReadbackForIntent({ kind: 'log_current', reps: 10 })).toBe('For 10, got it');
    expect(defaultReadbackForIntent({ kind: 'log_current', weight: 225, reps: 8 })).toBe('225 for 8, got it');
    expect(defaultReadbackForIntent({ kind: 'log_current', weight: 225, reps: 10 }, 'locked in')).toBe('225 for 10, locked in');
    expect(defaultReadbackForIntent({ kind: 'log_current' })).toBe('Checked, got it');
    expect(defaultReadbackForIntent({ kind: 'log_current' }, 'check')).toBe('Set checked');
    expect(defaultReadbackForIntent({ kind: 'undo' })).toBe('Undid last');

    expect([...SET_CONFIRM_TAGS]).toEqual(['check', 'got it']);
    let prevTag: string | null = null;
    for (let i = 0; i < 20; i++) {
      const next = pickSetConfirmTag(prevTag);
      expect(SET_CONFIRM_TAGS).toContain(next);
      expect(next).not.toBe(prevTag);
      prevTag = next;
    }
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
    expect(snapReady.lift!.current!.note).toBe('');
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

  it('hardens WALLBOARD_HTML overflow & compact TV heights and computes 16:9 preview scaling', () => {
    expect(WALLBOARD_HTML).toMatch(/\.card\s*\{[^}]*overflow:\s*hidden/);
    expect(WALLBOARD_HTML).toMatch(/\.cur-body\s*\{[^}]*overflow:\s*hidden/);
    expect(WALLBOARD_HTML).toContain('@media (max-height:700px)');
    expect(WALLBOARD_HTML).toContain('.ex-ss');
    expect(WALLBOARD_HTML).toContain('.ex-main');
    expect(WALLBOARD_HTML).toContain('.wb-chk');
    expect(WALLBOARD_HTML).toContain('.hr-slim');
    expect(WALLBOARD_HTML).not.toContain("' ?'");
    expect(WALLBOARD_HTML).not.toContain('SUGGESTED');
    expect(WALLBOARD_HTML).not.toContain('PARTNER');
    expect(WALLBOARD_HTML).not.toContain('Start a workout');
    expect(WALLBOARD_HTML).not.toContain('Start any workout');
    expect(WALLBOARD_HTML).not.toContain('Finish workout on your Pixel');
    expect(WALLBOARD_HTML).not.toContain('Connect Polar H10');
    expect(WALLBOARD_HTML).not.toContain('Waiting for chest strap');
    expect(WALLBOARD_HTML).not.toContain('Say “Armory close video”');
    expect(WALLBOARD_HTML).toContain('scheduleSynced === false');

    expect(TV_W).toBe(1600);
    expect(TV_H).toBe(900);

    const inlineFit = computeTvPreviewFit(360, 0, false);
    expect(inlineFit.rotated).toBe(false);
    expect(inlineFit.scale).toBeCloseTo(360 / 1600, 5);
    expect(inlineFit.boxW).toBe(360);
    expect(inlineFit.boxH).toBe(203);

    const portraitFs = computeTvPreviewFit(412, 915, true);
    expect(portraitFs.rotated).toBe(true);
    expect(portraitFs.scale).toBeCloseTo((412 - 20) / 900, 5);
    expect(portraitFs.boxW).toBe(392);
    expect(portraitFs.boxH).toBe(697);

    const landscapeFs = computeTvPreviewFit(915, 412, true);
    expect(landscapeFs.rotated).toBe(false);
    expect(landscapeFs.scale).toBeCloseTo((412 - 48) / 900, 5);
  });

  it('provides week-aware RPE/rep prescriptions (zero RIR/cues) and builds single vs superset cockpit stages', () => {
    expect(prescriptionFor(exOf('seated-dumbbell-shoulder-press'), 1).rpeTarget).toBe('RPE 7');
    expect(prescriptionFor(exOf('seated-dumbbell-shoulder-press'), 2)).toEqual({
      repTarget: '8–10 reps',
      rpeTarget: 'RPE 7.5',
      sampleRep: 10,
    });
    expect(prescriptionFor(exOf('triceps-isometric-hold'), 3).rpeTarget).toBe('RPE 5');
    expect(prescriptionFor(exOf('cable-triceps-pushdown'), 4)).toEqual({
      repTarget: '12–15 reps',
      rpeTarget: 'RPE 7–7.5',
      sampleRep: 12,
    });
    expect(prescriptionFor(exOf('low-bar-squat'), 4)).toEqual({
      repTarget: '6–8 reps',
      rpeTarget: 'RPE 8',
      sampleRep: 8,
    });
    expect(prescriptionFor(exOf('dumbbell-curl'), 2)).toEqual({
      repTarget: '10–12 reps',
      rpeTarget: 'RPE 8',
      sampleRep: 12,
    });

    const now = 3_000_000;
    const settings = {
      hrMax: 180,
      recoveryHrPct: 72,
      voiceEnabled: true,
      voiceWakeWord: 'armory',
      queue: defaultQueueState(),
    };

    // 1. Straight-set single stage (Triceps Iso Primer) with On-Deck superset pair 2A + 2B
    const singleExs = mkUpper1();
    const singleSession: LiftSession = {
      id: 's_single',
      name: 'Upper 1',
      routineId: null,
      eventId: null,
      startMs: 1_000_000,
      endMs: null,
      status: 'active',
      started: false,
      exs: singleExs,
      rpe: null,
      bwKg: 88.5,
    };
    const singleSnap = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 92, fresh: true, recentPoints: [{ at: now, hr: 92 }] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: singleSession,
      elapsedSec: 0,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(JSON.stringify(singleSnap)).not.toContain('RIR');
    const stSingle = singleSnap.lift!.stage!;
    expect(stSingle.kind).toBe('single');
    expect(stSingle.stations).toHaveLength(1);
    expect(stSingle.stations[0].label).toBe('1');
    expect(stSingle.stations[0].voiceHint!.chips.map((c) => c.say)).toEqual([
      '"Armory, start hold"',
      '"Armory, Done!"',
    ]);
    expect(stSingle.onDeck!.isSuperset).toBe(true);
    expect(stSingle.onDeck!.label).toBe('2A + 2B');

    // 2. Superset shared workspace (2A + 2B) with distinct delta voice chips & deeper ghost state
    const ssExs = mkUpper1();
    ssExs[0].sets.forEach((s) => { s.done = true; });
    ssExs[1].sets.forEach((s) => { s.wAuto = true; s.rAuto = true; });
    ssExs[2].sets.forEach((s) => { s.wAuto = true; s.rAuto = true; });
    const ssSession: LiftSession = { ...singleSession, id: 's_ss', started: true, exs: ssExs };
    const ssSnap = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 120, fresh: true, recentPoints: [{ at: now, hr: 120 }] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: ssSession,
      elapsedSec: 240,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(JSON.stringify(ssSnap)).not.toContain('RIR');
    const stSs = ssSnap.lift!.stage!;
    expect(stSs.kind).toBe('superset');
    expect(stSs.stations).toHaveLength(2);
    expect(stSs.stations[0].isActive).toBe(true);
    expect(stSs.stations[0].voiceHint!.chips.map((c) => c.say)).toEqual([
      '"Armory, Done!"',
      '"Armory, for 9"',
      '"Armory, 230 for 8"',
    ]);
    expect(stSs.stations[0].sets[0].state).toBe('active');
    expect(stSs.stations[0].sets[0].isGhost).toBe(true);
    expect(stSs.stations[1].isActive).toBe(false);
    expect(stSs.stations[1].voiceHint).toBeNull();
    expect(stSs.stations[1].sets[0].state).toBe('queued');
    expect(stSs.stations[1].sets[0].isGhost).toBe(true);

    // 3. Blank un-prefilled weighted exercise shows single clean prompt & never suggests "done"
    const blankExs: SessionEx[] = [
      { uid: 'b_ohp', exId: 'seated-dumbbell-shoulder-press', sets: [mkSet(null, null), mkSet(null, null)] },
    ];
    const blankSession: LiftSession = { ...singleSession, id: 's_blank', exs: blankExs };
    const blankSnap = buildWallboardSnapshot({
      now,
      settings,
      unit: 'lb',
      hr: { bpm: 112, fresh: true, recentPoints: [] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: blankSession,
      elapsedSec: 30,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    const blankStation = blankSnap.lift!.stage!.stations[0];
    expect(blankStation.sets[0].weightVal).toBe('');
    expect(blankStation.sets[0].repsVal).toBe('');
    expect(blankStation.sets[0].isGhost).toBe(false);
    expect(blankStation.voiceHint!.chips.map((c) => c.say)).toEqual([
      '"Armory, 50 for 10"',
    ]);
    expect(blankStation.voiceHint!.chips.some((c) => /done/i.test(c.say))).toBe(false);
  });

  it('resolves Universal ExerciseRx across RPE, % 1RM, and fixed/ramp setsRx in exercises & Wallboard 2-line roadmap', () => {
    const squat = exOf('low-bar-squat');
    const bench = exOf('barbell-bench-press');
    const ohp = exOf('seated-dumbbell-shoulder-press');

    // 1. RPE mode
    const rpeRx = resolvePrescription(ohp, { rx: { reps: '8–10', rpe: 7.5 }, sets: 3, unit: 'lb' });
    expect(rpeRx.repTarget).toBe('8–10 reps');
    expect(rpeRx.rpeTarget).toBe('RPE 7.5');
    expect(rpeRx.roadmapLine).toBe('3 × 8–10 · RPE 7.5');
    expect(rpeRx.prescribedSets).toBeNull();

    // 2. Percentage mode (75% of 420 lb = 315 lb x 12)
    const pctSe = buildExercise('barbell-bench-press', 3, null, false, 0, {
      rx: { pct: 75, reps: 12 },
      ex: bench,
      base1RmKg: 420 * KG_PER_LB,
      unit: 'lb',
    });
    expect(pctSe.rx).toEqual({ pct: 75, reps: 12 });
    expect(pctSe.sets).toHaveLength(3);
    expect(Math.round((pctSe.sets[0].w ?? 0) / KG_PER_LB)).toBe(315);
    expect(pctSe.sets[0].r).toBe(12);
    expect(pctSe.sets[0].wAuto).toBe(true);
    expect(pctSe.sets[0].rAuto).toBe(true);

    // 3. Fixed/ramp setsRx mode (275x5, 295x5, 315x12)
    const rampRx = {
      setsRx: [
        { loadLb: 275, reps: 5 },
        { loadLb: 295, reps: 5 },
        { loadLb: 315, reps: 12 },
      ],
    };
    const rampSe = buildExercise('low-bar-squat', 3, null, false, 1, { rx: rampRx, ex: squat, unit: 'lb' });
    expect(rampSe.sets.map((s) => [Math.round((s.w ?? 0) / KG_PER_LB), s.r])).toEqual([
      [275, 5],
      [295, 5],
      [315, 12],
    ]);

    // 4. Right-column 2-line Wallboard roadmap shows completed, RPE, %, and ramp sets from 0:00
    const completedOhp = buildExercise('seated-dumbbell-shoulder-press', 2, null, false, 2, {
      rx: { reps: '8–10', rpe: 7.5 },
      ex: ohp,
      unit: 'lb',
    });
    completedOhp.sets[0] = { ...completedOhp.sets[0], w: 55 * KG_PER_LB, r: 10, done: true, wAuto: false, rAuto: false };
    completedOhp.sets[1] = { ...completedOhp.sets[1], w: 55 * KG_PER_LB, r: 10, done: true, wAuto: false, rAuto: false };

    const pendingRpe = buildExercise('seated-dumbbell-shoulder-press', 3, null, false, 3, {
      rx: { reps: '8–10', rpe: 7.5 },
      ex: ohp,
      unit: 'lb',
    });

    const snap = buildWallboardSnapshot({
      now: 4_000_000,
      settings: {
        hrMax: 180,
        recoveryHrPct: 72,
        voiceEnabled: true,
        voiceWakeWord: 'armory',
        queue: defaultQueueState(),
      },
      unit: 'lb',
      hr: { bpm: 110, fresh: true, recentPoints: [] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: {
        id: 's_rx',
        name: 'Upper / Power Mix',
        routineId: null,
        eventId: null,
        startMs: 3_500_000,
        endMs: null,
        status: 'active',
        started: true,
        exs: [pctSe, rampSe, completedOhp, pendingRpe],
        rpe: null,
        bwKg: 88.5,
      },
      elapsedSec: 120,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      e1rmOf: (id) => (id === 'barbell-bench-press' ? 420 * KG_PER_LB : null),
      prevOf: () => null,
    });

    const roadmap = snap.lift!.exercises;
    expect(roadmap[0].isPrescribedLoad).toBe(true);
    expect(roadmap[0].subText).toBe('3 × 12 @ 315 lb (75%)');
    expect(roadmap[1].isPrescribedLoad).toBe(true);
    expect(roadmap[1].subText).toBe('275×5 · 295×5 · 315×12 lb');
    expect(roadmap[2].isDone).toBe(true);
    expect(roadmap[2].subText).toBe('2×10 @ 55 lb');
    expect(roadmap[3].isPrescribedLoad).toBe(false);
    expect(roadmap[3].subText).toBe('3 × 8–10 · RPE 7.5');
    expect(JSON.stringify(snap)).not.toContain('RIR');
  });

  it('detects active workout transitions cleanly for OptiPlex relay and supports dual-mount WALLBOARD_HTML paths', () => {
    expect(WALLBOARD_HTML).toContain("wbUrl('/state')");
    expect(WALLBOARD_HTML).toContain("wbUrl('/events')");
    expect(WALLBOARD_HTML).toContain("location.pathname.endsWith('/wallboard')");
    expect(WALLBOARD_HTML).toContain('<div class="top-mid">\' + banner + \'</div>');
    expect(WALLBOARD_HTML).not.toContain("</div>' + banner + main");

    const standby = buildWallboardSnapshot({
      now: 1_000,
      settings: { hrMax: 180, recoveryHrPct: 72, voiceEnabled: false, voiceWakeWord: 'armory', queue: defaultQueueState() },
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
      prevOf: () => null,
    });
    expect(isWallboardWorkoutActive(standby)).toBe(false);

    const previewLift = buildWallboardSnapshot({
      now: 1_000,
      settings: { hrMax: 180, recoveryHrPct: 72, voiceEnabled: false, voiceWakeWord: 'armory', queue: defaultQueueState() },
      unit: 'lb',
      hr: { bpm: 0, fresh: false, recentPoints: [] },
      voice: { listening: false, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: { id: 'p1', name: 'Upper 1', routineId: null, eventId: null, startMs: 1_000, endMs: null, status: 'active', started: false, exs: mkUpper1(), rpe: null, bwKg: 88.5 },
      elapsedSec: 0,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(isWallboardWorkoutActive(previewLift)).toBe(false);

    const activeLift = { ...previewLift, lift: { ...previewLift.lift!, started: true, paused: false } };
    expect(isWallboardWorkoutActive(activeLift)).toBe(true);

    const pausedLift = { ...previewLift, lift: { ...previewLift.lift!, started: true, paused: true } };
    expect(isWallboardWorkoutActive(pausedLift)).toBe(false);

    const activeCardio = {
      ...standby,
      mode: 'cardio' as const,
      cardio: {
        title: 'AirBike Z2', phase: 'STEADY', phaseTone: 'work' as const, repText: '1/1',
        countdownText: '25:00', targetText: '129 W', watts: 129, targetWatts: 129,
        rpm: 50, targetRpm: 50, elapsedSec: 30, kcal: 5, z5Sec: 0, paused: false,
      },
    };
    expect(isWallboardWorkoutActive(activeCardio)).toBe(true);
    expect(isWallboardWorkoutActive({ ...activeCardio, cardio: { ...activeCardio.cardio, paused: true } })).toBe(false);
  });

  it('tracks since-last-set timer, Wallboard TV replay snapshots & expiry, Drive fallback suppression, and v0.70 voice intents', () => {
    const sess: LiftSession = {
      id: 's_v70',
      name: 'Upper 1',
      routineId: null,
      eventId: null,
      startMs: 100_000,
      endMs: null,
      status: 'active',
      started: true,
      exs: mkUpper1(),
      rpe: null,
      bwKg: 88.5,
    };
    expect(secondsSinceLastSet(sess, 120_000)).toBeNull();

    sess.exs[0].sets[0] = { ...sess.exs[0].sets[0], done: true, at: 150_000 };
    expect(secondsSinceLastSet(sess, 195_500)).toBe(45);

    sess.exs[0].sets[1] = { ...sess.exs[0].sets[1], done: true, at: 210_000 };
    expect(secondsSinceLastSet(sess, 212_000)).toBe(2);

    expect(secondsSinceLastSet({ ...sess, pausedAt: 220_000 }, 999_000)).toBe(10);
    expect(secondsSinceLastSet({ ...sess, endMs: 250_000 }, 300_000)).toBeNull();

    expect(driveUrlFor({ fileName: 'Squat_Set1.mp4', source: 'gymcam' })).toBe('https://drive.google.com/drive/search?q=Squat_Set1.mp4');
    expect(driveUrlFor({ fileName: 'Squat_Set1.mp4', source: 'gymcam', uri: 'https://drive.google.com/file/d/direct123/view' })).toBe('https://drive.google.com/file/d/direct123/view');

    const replaySnap: WallboardReplayState = {
      id: 'v1',
      url: 'http://192.168.1.152:8766/api/clip/Squat_Set1.mp4',
      title: 'Barbell Bench Press',
      subtitle: 'Set 2 · Rack Cam',
      camera: 'Rack Cam',
      rate: 0.5,
      trimStartSec: 4.5,
      trimEndSec: 18.2,
      startedAtMs: 210_000,
      expiresAtMs: 250_000,
    };
    const wbActive = buildWallboardSnapshot({
      now: 225_000,
      settings: { hrMax: 180, recoveryHrPct: 72, voiceEnabled: true, voiceWakeWord: 'armory', queue: defaultQueueState() },
      unit: 'lb',
      hr: { bpm: 132, fresh: true, recentPoints: [] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      replaySnap,
      activeSession: sess,
      elapsedSec: 125,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(wbActive.lift?.sinceLastSetSec).toBe(15);
    expect(wbActive.replay).toEqual(replaySnap);
    expect(WALLBOARD_HTML).toContain('SINCE LAST SET');
    expect(WALLBOARD_HTML).toContain('id="wbReplay"');

    const wbExpired = buildWallboardSnapshot({
      now: 255_000,
      settings: { hrMax: 180, recoveryHrPct: 72, voiceEnabled: true, voiceWakeWord: 'armory', queue: defaultQueueState() },
      unit: 'lb',
      hr: { bpm: 132, fresh: true, recentPoints: [] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      replaySnap,
      activeSession: sess,
      elapsedSec: 155,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(wbExpired.replay).toBeUndefined();

    expect(parseVoiceTranscript('Armory replay')).toEqual({ kind: 'wb_replay' });
    expect(parseVoiceTranscript('Armory replay slow')).toEqual({ kind: 'wb_replay', slow: true });
    expect(parseVoiceTranscript('Armory play set 2')).toEqual({ kind: 'wb_replay', setNum: 2 });
    expect(parseVoiceTranscript('Armory play bench set three slow mo')).toEqual({ kind: 'wb_replay', exQuery: 'bench', setNum: 3, slow: true });
    expect(parseVoiceTranscript('Armory switch views')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch view')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory next view')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory other view')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch angles')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch camera')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch to next view')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch views alright guys')).toEqual({ kind: 'wb_switch_view' });
    expect(parseVoiceTranscript('Armory switch to bench')).toEqual({ kind: 'jump_ex', target: 'bench' });
    expect(parseVoiceTranscript('Armory close video')).toEqual({ kind: 'wb_close_replay' });
    expect(parseVoiceTranscript('Armory guest record')).toEqual({ kind: 'cam_guest' });
    expect(parseVoiceTranscript('Armory guest smith')).toEqual({ kind: 'cam_guest', camera: 'Smith Cam' });
    expect(parseVoiceTranscript('Armory guest fly')).toEqual({ kind: 'cam_guest', camera: 'Fly Machine Cam' });
    expect(defaultReadbackForIntent({ kind: 'wb_replay', slow: true })).toBe('Replaying slow motion on TV');
    expect(defaultReadbackForIntent({ kind: 'wb_switch_view' })).toBe('Switching views');
    expect(voiceIntentKey({ kind: 'wb_switch_view' })).toBe('wb_switch_view');
    expect(defaultReadbackForIntent({ kind: 'wb_close_replay' })).toBe('Closing video');
    expect(defaultReadbackForIntent({ kind: 'cam_guest', camera: 'Smith Cam' })).toBe('Guest camera Smith');

    expect(DEFAULTS.gymCamBuddyMode).toBe(false);
    expect(parseVoiceTranscript('Armory buddy mode on')).toEqual({ kind: 'buddy_mode', on: true });
    expect(parseVoiceTranscript('Armory enable buddy mode')).toEqual({ kind: 'buddy_mode', on: true });
    expect(parseVoiceTranscript('Armory lifting with a buddy')).toEqual({ kind: 'buddy_mode', on: true });
    expect(parseVoiceTranscript('Armory buddy mode off')).toEqual({ kind: 'buddy_mode', on: false });
    expect(parseVoiceTranscript('Armory solo mode')).toEqual({ kind: 'buddy_mode', on: false });
    expect(parseVoiceTranscript('Armory buddy mode')).toEqual({ kind: 'buddy_mode' });
    expect(parseVoiceTranscript('Armory toggle buddy mode')).toEqual({ kind: 'buddy_mode' });
    expect(defaultReadbackForIntent({ kind: 'buddy_mode', on: true })).toBe('Buddy mode on');
    expect(defaultReadbackForIntent({ kind: 'buddy_mode', on: false })).toBe('Buddy mode off');
    expect(defaultReadbackForIntent({ kind: 'buddy_mode' })).toBe('Buddy mode toggled');
  });

  it('defaults to Room Mic ON + Phone Mic OFF, runs one-time migration cleanly, and reflects Room Mic ready on Wallboard', () => {
    expect(DEFAULTS.voiceRoomEnabled).toBe(true);
    expect(DEFAULTS.voiceEnabled).toBe(false);

    const store = new Map<string, string>();
    const memStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v); },
    };

    const existing = { ...DEFAULTS, voiceEnabled: true, voiceRoomEnabled: false };
    const first = migrateRoomVoiceDefault(existing, memStorage);
    expect(first.migrated).toBe(true);
    expect(first.settings.voiceEnabled).toBe(false);
    expect(first.settings.voiceRoomEnabled).toBe(true);
    expect(memStorage.getItem(ROOM_VOICE_MIG_KEY)).toBe('1');

    // Subsequent launches do NOT override explicit user toggle once migration key is set
    const reEnabled = { ...first.settings, voiceEnabled: true };
    const second = migrateRoomVoiceDefault(reEnabled, memStorage);
    expect(second.migrated).toBe(false);
    expect(second.settings.voiceEnabled).toBe(true);

    const now = 5_000_000;
    const roomSess: LiftSession = {
      id: 's_room',
      name: 'Upper 1',
      routineId: null,
      eventId: null,
      startMs: now - 60_000,
      endMs: null,
      status: 'active',
      started: true,
      exs: mkUpper1(),
      rpe: null,
      bwKg: 88.5,
    };
    const wbRoom = buildWallboardSnapshot({
      now,
      settings: {
        hrMax: 180,
        recoveryHrPct: 72,
        voiceEnabled: false,
        voiceRoomEnabled: true,
        voiceWakeWord: 'armory',
        queue: defaultQueueState(),
      },
      unit: 'lb',
      hr: { bpm: 112, fresh: true, recentPoints: [] },
      voice: { listening: true, armed: false, banner: null, cursor: null },
      cardioSnap: null,
      holdSnap: null,
      activeSession: roomSess,
      elapsedSec: 60,
      rest: null,
      todayItems: [],
      routines: PHASE1_ROUTINES,
      exOf,
      prevOf: () => null,
    });
    expect(wbRoom.voice.enabled).toBe(true);
    expect(wbRoom.voice.listening).toBe(true);
    expect(wbRoom.lift!.stage!.stations[0].voiceHint!.micState).toBe('ready');
    expect(DEFAULTS.voiceAbCompare).toBe(false);

    // Every guided shootout phrase parses cleanly to its canonical expectedIntentKey
    expect(AB_SHOOTOUT_PHRASES).toHaveLength(10);
    for (const item of AB_SHOOTOUT_PHRASES) {
      const parsed = parseVoiceTranscript(item.phrase);
      expect(voiceIntentKey(parsed)).toBe(item.expectedIntentKey);
      expect(voiceTokenMatchPct(item.phrase, item.phrase)).toBe(100);
    }
    expect(voiceTokenMatchPct('armory two twenty five for eight', 'Armory 225 for 8')).toBe(100);
    expect(voiceTokenMatchPct('armory skip rest', 'Armory next exercise')).toBeLessThan(60);
  });
});

describe('Wave 2 Wallboard & Voice bounds hardening', () => {
  it('compiles WALLBOARD_HTML inline script cleanly and includes the stale stream watchdog', () => {
    const match = WALLBOARD_HTML.match(/<script>([\s\S]*?)<\/script>/);
    expect(match).not.toBeNull();
    expect(() => new Function(match![1])).not.toThrow();
    expect(WALLBOARD_HTML).toContain('replayLoading');
    expect(WALLBOARD_HTML).toContain("VIEW ' + replayMeta.viewIndex");
    expect(WALLBOARD_HTML).toContain('staleBanner');
    expect(WALLBOARD_HTML).toContain('PHONE PAUSED');
  });

  it('rejects extreme out-of-range numeric voice arguments and noisy armed-mode tails', () => {
    expect(parseVoiceTranscript('armory 5000 for 8')).toBeNull();
    expect(parseVoiceTranscript('armory 225 for 999')).toBeNull();
    expect(parseVoiceTranscript('armory 9999 seconds')).toBeNull();
    expect(parseVoiceTranscript('armory add 999 seconds')).toBeNull();
    expect(parseVoiceTranscript('check out that song', { armed: true })).toBeNull();
  });
});
