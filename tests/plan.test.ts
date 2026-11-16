import { describe, expect, it } from 'vitest';
import { PHASE1_PLAN, defaultQueueState, planExternalId, reconcileQueue } from '../src/domain/plan';
import type { IntervalsEvent } from '../src/intervals/parse';

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
});
