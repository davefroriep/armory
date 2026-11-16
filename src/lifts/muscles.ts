import type { Equip, Muscle } from './types';

export const MUSCLE_LABEL: Record<Muscle, string> = {
  ch: 'Chest', la: 'Lats', ub: 'Upper Back', tr: 'Traps', fd: 'Front Delts', sd: 'Side Delts', rd: 'Rear Delts',
  bi: 'Biceps', tri: 'Triceps', fa: 'Forearms', qu: 'Quads', ha: 'Hamstrings', gl: 'Glutes', ca: 'Calves',
  ab: 'Abs', lb: 'Lower Back', ad: 'Adductors',
};

export interface MuscleGroup {
  id: string;
  label: string;
  muscles: Muscle[];
}

/** Display groups (picker filters + analytics headings). */
export const GROUPS: MuscleGroup[] = [
  { id: 'chest', label: 'Chest', muscles: ['ch'] },
  { id: 'back', label: 'Back', muscles: ['la', 'ub', 'tr', 'lb'] },
  { id: 'shoulders', label: 'Shoulders', muscles: ['fd', 'sd', 'rd'] },
  { id: 'arms', label: 'Arms', muscles: ['bi', 'tri', 'fa'] },
  { id: 'legs', label: 'Legs', muscles: ['qu', 'ha', 'gl', 'ad', 'ca'] },
  { id: 'core', label: 'Core', muscles: ['ab'] },
];

export const groupOf = (m: Muscle): MuscleGroup => GROUPS.find((g) => g.muscles.includes(m))!;

export const EQUIP_LABEL: Record<Equip, string> = {
  bb: 'Barbell', db: 'Dumbbell', cable: 'Cable', machine: 'Machine', smith: 'Smith', bw: 'Bodyweight', other: 'Other',
};
export const EQUIPS: Equip[] = ['bb', 'db', 'cable', 'machine', 'smith', 'bw', 'other'];

/**
 * Fractional set counting: a hard set counts 1.0 toward each primary mover and 0.5 toward each secondary
 * mover. This is the method the volume literature (Pelland 2024, Schoenfeld) supports and what Hevy/Strong-
 * style trackers use; warm-ups never count.
 */
export const PRIMARY_WEIGHT = 1;
export const SECONDARY_WEIGHT = 0.5;
/** Weekly hard-set band shown on the analytics bars (evidence-based hypertrophy range). */
export const WEEKLY_BAND: [number, number] = [10, 20];
