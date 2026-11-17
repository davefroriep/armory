import { describe, expect, it } from 'vitest';
import {
  PHASE1_PLAN, buildWeekGlimpse, defaultQueueState, fmtWeekSpan, projectPlan, reconcileQueue, slotKey, todayItemsFromQueue,
} from '../src/domain/plan';
import type { Equipment } from '../src/types';

describe('local rolling schedule & queue reconciliation', () => {
  it('returns today items synchronously for Step 2 (index 1: Upper 1 + Z5 #1) without Intervals events', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: defaultQueueState(),
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(1);
    expect(res.nextState.stagedDate).toBe('2026-11-16');

    const todayItems = todayItemsFromQueue(PHASE1_PLAN, res.nextState, '2026-11-16');
    expect(todayItems).toHaveLength(2);
    expect(todayItems.map((i) => i.planSlot)).toEqual(['1:0', '1:1']);
    expect(todayItems[0].equipment).toBe('strength');
    expect(todayItems[1].equipment).toBe('airbike');
  });

  it('keeps the second slot on Today when only the first slot finishes', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), completedSlots: [0], stagedDate: '2026-11-16' },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(1);
    const items = todayItemsFromQueue(PHASE1_PLAN, res.nextState, '2026-11-16');
    expect(items.map((i) => i.planSlot)).toEqual(['1:1']);
  });

  it('advances to Step 3 tomorrow once all slots finish', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), completedSlots: [0, 1], stagedDate: '2026-11-16' },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.stagedDate).toBe('2026-11-17');
    expect(todayItemsFromQueue(PHASE1_PLAN, res.nextState, '2026-11-16')).toEqual([]);
  });

  it('slides an unfinished day forward to today automatically when a day is missed', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-17', consumedIds: [] },
      activities: [],
      today: '2026-11-19',
      tomorrow: '2026-11-20',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.stagedDate).toBe('2026-11-19');
    expect(todayItemsFromQueue(PHASE1_PLAN, res.nextState, '2026-11-19').map((i) => i.planSlot)).toEqual(['2:0']);
  });

  it('auto-completes a Spin Z2 slot from an external MyWhoosh activity while ignoring Armory uploads', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-19', consumedIds: [] },
      activities: [
        { id: 'armory-echo', type: 'VirtualRide', external_id: 'armory-abc', start_date_local: '2026-11-19T06:00:00' },
        { id: 'mywhoosh-99', type: 'VirtualRide', name: 'MyWhoosh Free Ride', start_date_local: '2026-11-19T07:30:00' },
      ],
      today: '2026-11-19',
      tomorrow: '2026-11-20',
    });
    expect(res.nextState.stepIndex).toBe(3);
    expect(res.nextState.consumedIds).toEqual(['mywhoosh-99']);
    expect(res.nextState.stagedDate).toBe('2026-11-20');
  });

  it('tracks skipped slots distinctly from completed slots across advance and projection', () => {
    // User skips Z5 (1:1) on Mon while leaving Upper 1 (1:0) pending
    const mid = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), stagedDate: '2026-11-16', skippedSlots: { '1:1': '2026-11-16' } },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(mid.nextState.stepIndex).toBe(1);
    expect(todayItemsFromQueue(PHASE1_PLAN, mid.nextState, '2026-11-16').map((i) => i.planSlot)).toEqual(['1:0']);

    // Upper 1 (1:0) finishes -> Step 1 advances to Step 2, while 1:1 stays skipped (NOT slotDone)
    const after = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...mid.nextState, completedSlots: [0] },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(after.nextState.stepIndex).toBe(2);
    const proj = projectPlan(PHASE1_PLAN, after.nextState, '2026-11-17', 0);
    expect(proj[1].slotStates).toEqual(['done', 'skipped']);
    expect(proj[1].slotDone).toEqual([true, false]);
    expect(proj[1].slotDates[1]).toBe('2026-11-16');
  });

  it('handles Push to tomorrow vs Add to tomorrow (carryOver) cleanly', () => {
    // Push slot 1 to tomorrow: holds step 1 until slot 1 completes
    const pushed = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), completedSlots: [0], stagedDate: '2026-11-16', slotDates: { 1: '2026-11-17' } },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(pushed.nextState.stepIndex).toBe(1);
    expect(todayItemsFromQueue(PHASE1_PLAN, pushed.nextState, '2026-11-17').map((i) => i.planSlot)).toEqual(['1:1']);

    // Add slot 1 to tomorrow (carryOver): advances queue to Step 2 while surfacing 1:1 + 2:0 on tomorrow
    const carried = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { ...defaultQueueState(), completedSlots: [0], stagedDate: '2026-11-16', carryOver: [{ planSlot: '1:1', date: '2026-11-17' }] },
      activities: [],
      today: '2026-11-16',
      tomorrow: '2026-11-17',
    });
    expect(carried.nextState.stepIndex).toBe(2);
    expect(carried.nextState.stagedDate).toBe('2026-11-17');
    const tomorrowItems = todayItemsFromQueue(PHASE1_PLAN, carried.nextState, '2026-11-17');
    expect(tomorrowItems.map((i) => i.planSlot)).toEqual(['1:1', '2:0']);
    expect(tomorrowItems[0].carried).toBe(true);
  });

  it('ignores activities already paired to an external event when matching unpaired slots', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 2, completedSlots: [], stagedDate: '2026-11-17', consumedIds: [] },
      activities: [{ id: 'carry-z5-done', type: 'VirtualRide', start_date_local: '2026-11-17T07:00:00', paired_event_id: 999 }],
      today: '2026-11-17',
      tomorrow: '2026-11-18',
    });
    expect(res.nextState.stepIndex).toBe(2);
    expect(res.nextState.completedSlots).toEqual([]);
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

    const projNormal = projectPlan(PHASE1_PLAN, { ...defaultQueueState(), stepIndex: 1, stagedDate: '2026-11-16' }, '2026-11-16', 0);
    expect(projNormal[6].date).toBe('2026-11-21'); // Sat
    expect(projNormal[7].date).toBe('2026-11-23'); // Sun 22 skipped -> Mon 23
  });

  it('auto-skips completed doneEarly future steps on advance and respects restDay', () => {
    const res = reconcileQueue({
      plan: PHASE1_PLAN,
      state: {
        planId: 'phase1',
        stepIndex: 5,
        completedSlots: [0],
        stagedDate: '2026-11-21', // Sat
        doneEarly: { [slotKey(6, 0)]: '2026-11-20', [slotKey(7, 0)]: '2026-11-20' },
        consumedIds: [],
      },
      activities: [],
      today: '2026-11-21',
      tomorrow: '2026-11-22', // Sunday!
      restDay: 0,
    });
    expect(res.nextState.stepIndex).toBe(7); // skipped step 6
    expect(res.nextState.completedSlots).toEqual([0]); // slot 0 absorbed from doneEarly
    expect(res.nextState.stagedDate).toBe('2026-11-23'); // Sun 22 rest day skipped -> Mon 23
  });

  it('attributes out-of-order Upper 2 or Z2 audibles to future matching slots without clearing Lower 2 or Z5', () => {
    const rLift = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 3, completedSlots: [], stagedDate: '2026-11-18', consumedIds: [] },
      activities: [{ id: 'lift-u2', type: 'WeightTraining', name: 'Upper 2', start_date_local: '2026-11-18T18:00:00' }],
      today: '2026-11-18',
      tomorrow: '2026-11-19',
    });
    expect(rLift.nextState.stepIndex).toBe(3);
    expect(rLift.nextState.completedSlots).toEqual([]);
    expect(rLift.nextState.doneEarly?.[slotKey(4, 0)]).toBe('2026-11-18');

    const rCardio = reconcileQueue({
      plan: PHASE1_PLAN,
      state: { planId: 'phase1', stepIndex: 5, completedSlots: [], stagedDate: '2026-11-20', consumedIds: [] },
      activities: [{ id: 'spin-z2', type: 'VirtualRide', name: 'Spin Bike Z2', start_date_local: '2026-11-20T07:00:00' }],
      today: '2026-11-20',
      tomorrow: '2026-11-21',
    });
    expect(rCardio.nextState.stepIndex).toBe(5);
    expect(rCardio.nextState.doneEarly?.[slotKey(8, 0)]).toBe('2026-11-20');
  });

  it('builds a Sunday-Saturday week glimpse, supports week anchors, and formats week spans', () => {
    expect(fmtWeekSpan('2026-11-15')).toBe('NOV 15 – 21');
    expect(fmtWeekSpan('2026-11-29')).toBe('NOV 29 – DEC 5');

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

    const futureCells = buildWeekGlimpse({ today, anchorYmd: '2026-11-25', proj, loggedByDate: new Map() });
    expect(futureCells.map((c) => c.ymd)).toEqual([
      '2026-11-22', '2026-11-23', '2026-11-24', '2026-11-25', '2026-11-26', '2026-11-27', '2026-11-28',
    ]);
    expect(futureCells.every((c) => !c.isToday && !c.isPast)).toBe(true);
    expect(futureCells[0].kind).toBe('rest'); // Sunday rest day
  });
});
