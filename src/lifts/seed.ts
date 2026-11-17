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
      { exId: 'low-bar-squat', sets: 3 },
      { exId: 'romanian-deadlift', sets: 3 },
      { exId: 'standing-calf-raise', sets: 3, ss: true },
      { exId: 'cable-crunch', sets: 3 },
    ],
  },
  {
    id: 'day-upper-1',
    name: 'Upper 1',
    createdMs: 2,
    order: 1,
    items: [
      { exId: 'triceps-isometric-hold', sets: 2 },
      { exId: 'barbell-bench-press', sets: 3, ss: true },
      { exId: 'smith-machine-row', sets: 3 },
      { exId: 'seated-dumbbell-shoulder-press', sets: 3, ss: true },
      { exId: 'pull-up', sets: 3 },
      { exId: 'dumbbell-lateral-raise', sets: 3, ss: true },
      { exId: 'cable-triceps-pushdown', sets: 3 },
    ],
  },
  {
    id: 'day-lower-2',
    name: 'Lower 2',
    createdMs: 3,
    order: 2,
    items: [
      { exId: 'deadlift', sets: 3 },
      { exId: 'bulgarian-split-squat', sets: 3 },
      { exId: 'standing-calf-raise', sets: 3, ss: true },
      { exId: 'cable-crunch', sets: 3 },
    ],
  },
  {
    id: 'day-upper-2',
    name: 'Upper 2',
    createdMs: 4,
    order: 3,
    items: [
      { exId: 'triceps-isometric-hold', sets: 2 },
      { exId: 'smith-machine-overhead-press', sets: 3, ss: true },
      { exId: 'lat-pulldown', sets: 3 },
      { exId: 'chest-fly-machine', sets: 3, ss: true },
      { exId: 'dumbbell-row', sets: 3 },
      { exId: 'rear-delt-fly', sets: 3, ss: true },
      { exId: 'dumbbell-curl', sets: 3 },
    ],
  },
];

export const SEED_EX_ROWS: ExRow[] = [
  { id: 'triceps-isometric-hold', restSec: 45 },
];

/** Planned-event title -> matching day (e.g. "Strength Train — Wk 1 · Day 2 Upper 1" -> "Upper 1"). */
export function matchRoutineByTitle(title: string, routines: Routine[]): Routine | undefined {
  const n = title.toLowerCase();
  return routines
    .filter((r) => r.name.trim() && n.includes(r.name.toLowerCase()))
    .sort((a, b) => b.name.length - a.name.length)[0];
}
