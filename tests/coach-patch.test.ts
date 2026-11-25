import { describe, expect, it } from 'vitest';
import { PHASE1_PLAN, applyPlanOverrides } from '../src/domain/plan';
import { applyCoachPatches, type CoachPatchDeps, type CoachPatchFeed } from '../src/store/coachPatch';
import { DEFAULTS, type Settings } from '../src/store/settings';
import type { Routine } from '../src/lifts/types';

function makeHarness(initial?: Partial<Settings>) {
  let state: Settings = {
    ...DEFAULTS,
    ...initial,
    programTargets: { ...(initial?.programTargets ?? {}) },
    appliedCoachPatches: [...(initial?.appliedCoachPatches ?? [])],
    planStepOverrides: { ...(initial?.planStepOverrides ?? {}) },
    queue: { ...DEFAULTS.queue, ...(initial?.queue ?? {}), extras: [...(initial?.queue?.extras ?? [])] },
  };
  const routines = new Map<string, Routine>();
  const cams = new Map<string, string[]>();
  const deps: CoachPatchDeps = {
    getSettings: () => state,
    updateSettings: (patch) => {
      state = { ...state, ...patch };
      return state;
    },
    todayYmd: () => '2026-11-25',
    nowMs: () => 1_795_600_000_000,
    upsertRoutine: async (r) => { routines.set(r.id, r); },
    updateExerciseCams: async (exId, list) => { cams.set(exId, list); },
  };
  return { deps, getState: () => state, routines, cams };
}

describe('Remote Coach Patch Sync (zero-APK config & programming)', () => {
  it('applies cardio targets, settings, routines, cams, extras, and plan overrides exactly once', async () => {
    const h = makeHarness({ apiKey: 'secret-key-untouched', programTargets: { b6030: 59 } });
    const feed: CoachPatchFeed = {
      schema: 1,
      patches: [
        {
          id: '2026-11-25-z5-targets-v1',
          label: 'Updated AirBike Z5 targets',
          programTargets: { n4x4: 56, v5x3: 57, b6030: 59, 'spin-n4x4': 225 },
          settings: { ftp: 190, hrMax: 180, restDay: 0 },
          exerciseCams: { 'rear-delt-fly': ['Rack Cam'] },
          routines: [{
            id: 'day-upper-2',
            name: 'Upper 2',
            createdMs: 1_795_600_000_000,
            order: 3,
            items: [{ exId: 'triceps-isometric-hold', sets: 2, rx: { reps: '45s', rpe: 5 } }],
          }],
          extras: [{
            id: 'extra-z2-walk',
            name: 'Incline Treadmill Z2 (35m)',
            date: '2026-11-25',
            equipment: 'treadmill',
            seconds: 2100,
            description: '3.2 mph @ 5% incline, HR 122-132 bpm',
          }],
          planStepOverrides: {
            8: {
              title: 'Wk 2 · Day 3 — Zone 2 Base (Spin + Incline Walk)',
              slots: [{
                equipment: 'spin',
                programId: 'spin-z2',
                intervalsType: 'VirtualRide',
                name: 'Spin Bike Z2 (45m)',
                description: '45m @ 129 W',
                seconds: 2700,
              }],
            },
          },
        },
      ],
    };

    const r1 = await applyCoachPatches(feed, h.deps);
    expect(r1.applied).toHaveLength(1);
    expect(r1.latestLabel).toBe('Updated AirBike Z5 targets');
    expect(h.getState().programTargets).toEqual({ n4x4: 56, v5x3: 57, b6030: 59, 'spin-n4x4': 225 });
    expect(h.getState().ftp).toBe(190);
    expect(h.getState().apiKey).toBe('secret-key-untouched');
    expect(h.getState().appliedCoachPatches).toEqual(['2026-11-25-z5-targets-v1']);
    expect(h.routines.get('day-upper-2')?.name).toBe('Upper 2');
    expect(h.cams.get('rear-delt-fly')).toEqual(['Rack Cam']);
    expect(h.getState().queue.extras?.map((x) => x.id)).toEqual(['extra-z2-walk']);

    const overriddenPlan = applyPlanOverrides(PHASE1_PLAN, h.getState().planStepOverrides);
    expect(overriddenPlan.steps[8].title).toBe('Wk 2 · Day 3 — Zone 2 Base (Spin + Incline Walk)');
    expect(PHASE1_PLAN.steps[8].title).not.toBe(overriddenPlan.steps[8].title);

    // Athlete manually adjusts n4x4 on phone to 58 RPM; re-running the same feed must NOT overwrite it!
    h.deps.updateSettings({ programTargets: { ...h.getState().programTargets, n4x4: 58 } });
    const r2 = await applyCoachPatches(feed, h.deps);
    expect(r2.applied).toHaveLength(0);
    expect(h.getState().programTargets.n4x4).toBe(58);
  });

  it('skips expired patches without applying and clamps out-of-range targets', async () => {
    const h = makeHarness();
    const feed: CoachPatchFeed = {
      schema: 1,
      patches: [
        {
          id: 'expired-patch',
          expiresAfter: '2026-11-20',
          programTargets: { n4x4: 60 },
        },
        {
          id: 'clamped-patch',
          programTargets: { n4x4: 999, 'spin-n4x4': 10 },
        },
      ],
    };
    const res = await applyCoachPatches(feed, h.deps);
    expect(res.skippedExpired).toBe(1);
    expect(res.applied.map((p) => p.id)).toEqual(['clamped-patch']);
    expect(h.getState().programTargets.n4x4).toBe(85);
    expect(h.getState().programTargets['spin-n4x4']).toBe(50);
  });
});
