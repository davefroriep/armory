import { describe, expect, it } from 'vitest';
import {
  PHASE1_PLAN, buildWeekGlimpse, defaultQueueState, planExternalId, projectPlan, reconcileQueue, slotKey,
} from '../src/domain/plan';
import type { IntervalsEvent } from '../src/intervals/parse';
import type { Equipment } from '../src/types';

describe('rolling queue auto-stager', () => {
  it('stages Step 2 (index 1: Upper 1 + Z5 #1) on initial run', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: defaultQueueState(),
      events: [],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(1);
    expect(res.nextState.stagedDate).toBe('2026-11-16');
    expect(res.creates).toHaveLength(2);
    expect(res.creates[0].external_id).toBe(planExternalId('phase1', 1, 0));
    expect(res.creates[0].type).toBe('WeightTraining');
    expect(res.creates[1].external_id).toBe(planExternalId('phase1', 1, 1));
    expect(res.creates[1].type).toBe('VirtualRide');
  });

  it('keeps the second slot staged when only the first slot of a double day finishes', () => {
    const ev0: IntervalsEvent = {
      id: 101,
      external_id: planExternalId('phase1', 1, 0),
      start_date_local: '2026-11-16T00:00:00',
      paired_activity_id: 'act-str-1',
    };
    const ev1: IntervalsEvent = {
      id: 102,
      external_id: planExternalId('phase1', 1, 1),
      start_date_local: '2026-11-16T00:00:00',
      paired_activity_id: null,
    };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), stagedDate: '2026-11-16' },
      events: [ev0, ev1],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(1);
    expect(res.nextState.completedSlots).toEqual([0]);
    expect(res.deletes).toEqual([]);
    expect(res.moves).toEqual([]);
    expect(res.creates).toEqual([]);
  });

  it('advances to Step 3 and stages for tomorrow once all slots finish', () => {
    const ev0: IntervalsEvent = {
      id: 101,
      external_id: planExternalId('phase1', 1, 0),
      start_date_local: '2026-11-16T00:00:00',
      paired_activity_id: 'act-str-1',
    };
    const ev1: IntervalsEvent = {
      id: 102,
      external_id: planExternalId('phase1', 1, 1),
      start_date_local: '2026-11-16T00:00:00',
      paired_activity_id: 'act-bike-1',
    };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), stagedDate: '2026-11-16' },
      events: [ev0, ev1],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.stagedDate).toBe('2026-11-17');
    expect(res.creates).toHaveLength(1);
    expect(res.creates[0].external_id).toBe(planExternalId('phase1', 2, 0));
    expect(res.creates[0].start_date_local).toBe('2026-11-17T00:00:00');
  });

  it('slides an unfinished staged workout forward to today when a day is missed', () => {
    const ev: IntervalsEvent = {
      id: 201,
      external_id: planExternalId('phase1', 2, 0),
      start_date_local: '2026-11-17T00:00:00',
      paired_activity_id: null,
    };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-17', consumedIds: [] },
      events: [ev],
      activities: [],
      today: '2026-11-19',
      tomorrow: '2026-11-20',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.stagedDate).toBe('2026-11-19');
    expect(res.moves).toEqual([{ id: 201, date: '2026-11-19' }]);
    expect(res.creates).toEqual([]);
  });

  it('matches an unpaired external activity by sport type and deletes stale unpaired events', () => {
    const stale: IntervalsEvent = {
      id: 201,
      external_id: planExternalId('phase1', 2, 0),
      start_date_local: '2026-11-19T00:00:00',
      paired_activity_id: null,
    };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-19', consumedIds: [] },
      events: [stale],
      activities: [{ id: 'garmin-99', type: 'VirtualRide', start_date_local: '2026-11-19T07:30:00' }],
      today: '2026-11-19',
      tomorrow: '2026-11-20',
    });
    expect(res.nextState.stepIndex).toBe(3);
    expect(res.nextState.consumedIds).toContain('garmin-99');
    expect(res.deletes).toEqual([201]);
    expect(res.creates[0].external_id).toBe(planExternalId('phase1', 3, 0));
    expect(res.creates[0].start_date_local).toBe('2026-11-20T00:00:00');
  });

  it('skips only the resolved slot on a 2-slot day and leaves the other workout on today', () => {
    const ev0: IntervalsEvent = { id: 101, external_id: planExternalId('phase1', 1, 0), start_date_local: '2026-11-16T00:00:00', paired_activity_id: null };
    const ev1: IntervalsEvent = { id: 102, external_id: planExternalId('phase1', 1, 1), start_date_local: '2026-11-16T00:00:00', paired_activity_id: null };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), completedSlots: [1], stagedDate: '2026-11-16' },
      events: [ev0, ev1],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(1);
    expect(res.deletes).toEqual([102]);
    expect(res.moves).toEqual([]);
    expect(res.creates).toEqual([]);
  });

  it('pushes only one deferred slot to tomorrow and advances only once both finish', () => {
    const ev0: IntervalsEvent = { id: 101, external_id: planExternalId('phase1', 1, 0), start_date_local: '2026-11-16T00:00:00', paired_activity_id: null };
    const ev1: IntervalsEvent = { id: 102, external_id: planExternalId('phase1', 1, 1), start_date_local: '2026-11-16T00:00:00', paired_activity_id: null };
    const r1 = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), stagedDate: '2026-11-16', slotDates: { 1: '2026-11-17' } },
      events: [ev0, ev1],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(r1.nextState.stepIndex).toBe(1);
    expect(r1.moves).toEqual([{ id: 102, date: '2026-11-17' }]);
    // Slot 0 finishes today -> stays on step 1 with slot 1 still on tomorrow
    const r2 = reconcileQueue({
      plan: PHASE1_PLAN,
      state: r1.nextState,
      events: [{ ...ev0, paired_activity_id: 'act-str' }, { ...ev1, start_date_local: '2026-11-17T00:00:00' }],
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(r2.nextState.stepIndex).toBe(1);
    expect(r2.nextState.completedSlots).toEqual([0]);
    // On tomorrow, once slot 1 finishes, queue advances cleanly to step 2
    const r3 = reconcileQueue({
      plan: PHASE1_PLAN,
      state: r2.nextState,
      events: [{ ...ev0, paired_activity_id: 'act-str' }, { ...ev1, start_date_local: '2026-11-17T00:00:00', paired_activity_id: 'act-z5' }],
      activities: [],
      today: '2026-11-17',
      tomorrow: '2026-11-18',
    });
    expect(r3.nextState.stepIndex).toBe(2);
    expect(r3.nextState.slotDates).toEqual({});
  });

  it('ignores activities already paired to a carry-over event when matching unpaired slots', () => {
    const z2: IntervalsEvent = { id: 201, external_id: planExternalId('phase1', 2, 0), start_date_local: '2026-11-17T00:00:00', paired_activity_id: null };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-17', slotDates: {}, consumedIds: [] },
      events: [z2],
      activities: [{ id: 'carry-z5-done', type: 'VirtualRide', start_date_local: '2026-11-17T07:00:00', paired_event_id: 999 }],
      today: '2026-11-17',
      tomorrow: '2026-11-18',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.completedSlots).toEqual([]);
    expect(res.deletes).toEqual([]);
  });

  it('projects future weeks skipping restDay (Sunday) and consuming zero days for doneEarly steps', () => {
    const state = {
      ...defaultQueueState(),
      stepIndex: 1, // Mon 2026-11-16 = Step 1 (Wk1 Day 2)
      stagedDate: '2026-11-16',
      doneEarly: { [slotKey(4, 0)]: '2026-11-15' }, // Step 4 (Upper 2) done early
      stepDates: { 0: '2026-11-15' },
    };
    const proj = projectPlan(PHASE1_PLAN, state, '2026-11-16', 0);
    expect(proj[0]).toMatchObject({ status: 'done', date: '2026-11-15' });
    expect(proj[1]).toMatchObject({ status: 'current', date: '2026-11-16' }); // Mon
    expect(proj[2]).toMatchObject({ status: 'upcoming', date: '2026-11-17' }); // Tue
    expect(proj[3]).toMatchObject({ status: 'upcoming', date: '2026-11-18' }); // Wed
    expect(proj[4]).toMatchObject({ status: 'early', date: '2026-11-15' }); // skipped on projection
    expect(proj[5]).toMatchObject({ status: 'upcoming', date: '2026-11-19' }); // Thu (pulled in)
    expect(proj[6]).toMatchObject({ status: 'upcoming', date: '2026-11-20' }); // Fri
    expect(proj[7]).toMatchObject({ status: 'upcoming', date: '2026-11-21' }); // Sat
    // With Step 4 not done early, Step 7 falls on Sun 2026-11-22 -> skips to Mon 2026-11-23
    const projNormal = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 1, stagedDate: '2026-11-16' }, '2026-11-16', 0);
    expect(projNormal[6].date).toBe('2026-11-21'); // Sat
    expect(projNormal[7].date).toBe('2026-11-23'); // Sun 22 skipped -> Mon 23
  });

  it('auto-skips completed doneEarly future steps on advance and respects restDay', () => {
    // Active = Step 5 (Wk1 Day 6, Z5 #2), Step 6 (Wk2 Day 1 Lower 1) already doneEarly, Step 7 (Upper 1 + Z5) has slot 0 doneEarly
    const ev: IntervalsEvent = { id: 501, external_id: planExternalId('phase1', 5, 0), start_date_local: '2026-11-21T00:00:00', paired_activity_id: 'act-z5-2' };
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: {
        planId: 'phase1',
        stepIndex: 5,
        completedSlots: [],
        stagedDate: '2026-11-21', // Sat
        doneEarly: { [slotKey(6, 0)]: '2026-11-20', [slotKey(7, 0)]: '2026-11-20' },
        consumedIds: [],
      },
      events: [ev],
      activities: [],
      today: '2026-11-21',
      tomorrow: '2026-11-22', // Sunday!
      restDay: 0,
    });
    expect(res.nextState.stepIndex).toBe(7); // skipped step 6
    expect(res.nextState.completedSlots).toEqual([0]); // slot 0 absorbed from doneEarly
    expect(res.nextState.stagedDate).toBe('2026-11-23'); // Sun 22 rest day skipped -> Mon 23
    expect(res.creates).toHaveLength(1);
    expect(res.creates[0].external_id).toBe(planExternalId('phase1', 7, 1));
    expect(res.creates[0].start_date_local).toBe('2026-11-23T00:00:00');
  });

  it('attributes out-of-order Upper 2 or Z2 audibles to future matching slots without clearing Lower 2 or Z5', () => {
    // Step 3 is Lower 2; user logs "Upper 2" -> credits Step 4 (Upper 2), leaves Step 3 incomplete
    const evL2: IntervalsEvent = { id: 301, external_id: planExternalId('phase1', 3, 0), start_date_local: '2026-11-18T00:00:00', paired_activity_id: null };
    const rLift = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 3, completedSlots: [], stagedDate: '2026-11-18', consumedIds: [] },
      events: [evL2],
      activities: [{ id: 'lift-u2', type: 'WeightTraining', name: 'Upper 2', start_date_local: '2026-11-18T18:00:00' }],
      today: '2026-11-18',
      tomorrow: '2026-11-19',
    });
    expect(rLift.nextState.stepIndex).toBe(3);
    expect(rLift.nextState.completedSlots).toEqual([]);
    expect(rLift.nextState.doneEarly?.[slotKey(4, 0)]).toBe('2026-11-18');

    // Step 5 is AirBike Z5; user rides "Spin Bike Z2" -> credits next Z2 step (Step 8) without completing Z5
    const evZ5: IntervalsEvent = { id: 502, external_id: planExternalId('phase1', 5, 0), start_date_local: '2026-11-20T00:00:00', paired_activity_id: null };
    const rCardio = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 5, completedSlots: [], stagedDate: '2026-11-20', consumedIds: [] },
      events: [evZ5],
      activities: [{ id: 'spin-z2', type: 'VirtualRide', name: 'Spin Bike Z2', start_date_local: '2026-11-20T07:00:00' }],
      today: '2026-11-20',
      tomorrow: '2026-11-21',
    });
    expect(rCardio.nextState.stepIndex).toBe(5);
    expect(rCardio.nextState.doneEarly?.[slotKey(8, 0)]).toBe('2026-11-20');
  });

  it('builds a Sunday-Saturday week glimpse with done, today, upcoming, and rest cells', () => {
    const today = '2026-11-16'; // Mon
    const proj = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 1, stagedDate: today }, today, 0);
    const logged = new Map<string, { equipment: Equipment; name: string }[]>([
      ['2026-11-15', [{ equipment: 'strength', name: 'Lower 1' }]],
    ]);
    const cells = buildWeekGlimpse({ today, proj, loggedByDate: logged });
    expect(cells).toHaveLength(7);
    expect(cells.map((c) => c.ymd)).toEqual([
      '2026-11-15', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20', '2026-11-21',
    ]);
    expect(cells[0]).toMatchObject({ kind: 'done', equipments: ['strength'] }); // Sun logged
    expect(cells[1]).toMatchObject({ isToday: true, kind: 'today', equipments: ['strength', 'airbike'] }); // Mon Step 1
    expect(cells[2]).toMatchObject({ kind: 'upcoming', equipments: ['spin'] }); // Tue Step 2
    expect(cells[3]).toMatchObject({ kind: 'upcoming', equipments: ['strength'] }); // Wed Step 3
    // When Tue Step 2 was pushed to Thu (skipping Wed? or e.g. empty week with restDay 0 on Sun)
    const restProj = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 1, stagedDate: '2026-11-16' }, today, 0);
    const emptySun = buildWeekGlimpse({ today, proj: restProj, loggedByDate: new Map() });
    expect(emptySun[0].kind).toBe('rest');
  });
});
