import type { ExRow, Routine } from './types';

/**
 * Dave's 4 Phase 1 lifting days ("Armory Hybrid: Phase 1"). Seeded once when the device has no days
 * yet so tapping a planned Intervals.icu strength workout opens the right lifts straight away; once on
 * the device, they are ordinary user-editable days (nothing locked or forced).
 */
export const PHASE1_ROUTINES: Routine[] = [
  {
    id: 'day-lower-1',
    name: 'Lower 1',
    createdMs: 1,
    order: 0,
    items: [
      { exId: 'low-bar-squat', sets: 3, rx: { reps: '6–8', rpe: 7.5 } },
      { exId: 'smith-machine-romanian-deadlift', sets: 3, rx: { reps: '8–10', rpe: 7.5 } },
      { exId: 'standing-calf-raise', sets: 3, ss: true, rx: { reps: '12–15', rpe: 8 } },
      { exId: 'cable-crunch', sets: 3, rx: { reps: '12–15', rpe: 8 } },
    ],
  },
  {
    id: 'day-upper-1',
    name: 'Upper 1',
    createdMs: 2,
    order: 1,
    items: [
      { exId: 'triceps-isometric-hold', sets: 2, rx: { reps: '45s', rpe: 5 } },
      { exId: 'barbell-bench-press', sets: 3, ss: true, rx: { reps: '6–8', rpe: 7.5 } },
      { exId: 'smith-machine-row', sets: 3, rx: { reps: '10–12', rpe: 7.5 } },
      { exId: 'seated-dumbbell-shoulder-press', sets: 3, ss: true, rx: { reps: '8–10', rpe: 7.5 } },
      { exId: 'pull-up', sets: 3, rx: { reps: '6–10', rpe: 7.5 } },
      { exId: 'dumbbell-lateral-raise', sets: 3, ss: true, rx: { reps: '12–15', rpe: 8 } },
      { exId: 'cable-triceps-pushdown', sets: 3, rx: { reps: '12–15', rpe: '7–7.5' } },
    ],
  },
  {
    id: 'day-lower-2',
    name: 'Lower 2',
    createdMs: 3,
    order: 2,
    items: [
      { exId: 'deadlift', sets: 3, rx: { reps: '5–6', rpe: 7.5 } },
      { exId: 'bulgarian-split-squat', sets: 3, rx: { reps: '8–10', rpe: 7.5 } },
      { exId: 'standing-calf-raise', sets: 3, ss: true, rx: { reps: '12–15', rpe: 8 } },
      { exId: 'cable-crunch', sets: 3, rx: { reps: '12–15', rpe: 8 } },
    ],
  },
  {
    id: 'day-upper-2',
    name: 'Upper 2',
    createdMs: 4,
    order: 3,
    items: [
      { exId: 'triceps-isometric-hold', sets: 2, rx: { reps: '45s', rpe: 5 } },
      { exId: 'smith-machine-overhead-press', sets: 3, ss: true, rx: { reps: '8–10', rpe: 7.5 } },
      { exId: 'lat-pulldown', sets: 3, rx: { reps: '10–12', rpe: 7.5 } },
      { exId: 'chest-fly-machine', sets: 3, ss: true, rx: { reps: '12–15', rpe: 8 } },
      { exId: 'dumbbell-row', sets: 3, rx: { reps: '10–12', rpe: 7.5 } },
      { exId: 'rear-delt-fly', sets: 3, ss: true, rx: { reps: '12–15', rpe: 8 } },
      { exId: 'dumbbell-curl', sets: 3, rx: { reps: '10–12', rpe: 8 } },
    ],
  },
];

/** Dave's verified camera assignments across all 19 Phase 1 exercises (equal views, Rack Cam fallback). */
export const PHASE1_EXERCISE_CAMS: Record<string, string[]> = {
  'low-bar-squat': ['Rack Cam'],
  'smith-machine-romanian-deadlift': ['Smith Cam'],
  'standing-calf-raise': ['Smith Cam', 'Smith Wide'],
  'cable-crunch': ['Rack Cam'],
  'triceps-isometric-hold': ['Rack Cam'],
  'barbell-bench-press': ['Rack Cam'],
  'smith-machine-row': ['Smith Cam', 'Smith Wide'],
  'seated-dumbbell-shoulder-press': ['Rack Cam', 'Smith Cam', 'Smith Wide'],
  'pull-up': ['Smith Wide', 'Smith Cam'],
  'dumbbell-lateral-raise': ['Smith Wide', 'Smith Cam'],
  'cable-triceps-pushdown': ['Rack Cam'],
  deadlift: ['Rack Cam'],
  'bulgarian-split-squat': ['Smith Cam', 'Smith Wide'],
  'smith-machine-overhead-press': ['Smith Cam', 'Smith Wide'],
  'lat-pulldown': ['Rack Cam'],
  'chest-fly-machine': ['Fly Machine Cam'],
  'dumbbell-row': ['Rack Cam', 'Smith Cam', 'Smith Wide'],
  'rear-delt-fly': ['Rack Cam'],
  'dumbbell-curl': ['Rack Cam', 'Smith Cam', 'Smith Wide'],
};

export const SEED_EX_ROWS: ExRow[] = Object.entries(PHASE1_EXERCISE_CAMS).map(([id, cams]) => ({
  id,
  ...(id === 'triceps-isometric-hold' ? { restSec: 45 } : {}),
  cams: [...cams],
}));

/** Planned-event title -> matching day (e.g. "Strength Train — Wk 1 · Day 2 Upper 1" -> "Upper 1"). */
export function matchRoutineByTitle(title: string, routines: Routine[]): Routine | undefined {
  const n = title.toLowerCase();
  return routines
    .filter((r) => r.name.trim() && n.includes(r.name.toLowerCase()))
    .sort((a, b) => b.name.length - a.name.length)[0];
}
