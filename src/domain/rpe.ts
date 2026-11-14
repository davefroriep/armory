/**
 * Perceived-effort scale shown at the end of a workout. Eight plain-language steps that map to
 * RPE 3-10 on Intervals.icu (nothing here is ever a 1 or 2; those are a stroll around the block).
 */
export interface RpeStep {
  value: number;
  label: string;
}

export const RPE_STEPS: readonly RpeStep[] = [
  { value: 3, label: 'Very easy' },
  { value: 4, label: 'Easy' },
  { value: 5, label: 'Sort of easy' },
  { value: 6, label: 'Medium effort' },
  { value: 7, label: 'Sort of hard' },
  { value: 8, label: 'Hard' },
  { value: 9, label: 'Very hard' },
  { value: 10, label: 'Extreme' },
];

export const rpeLabel = (v: number): string => RPE_STEPS.find((s) => s.value === v)?.label ?? '';
