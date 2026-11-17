import type { Exercise } from './types';

export interface CoachPrescription {
  repTarget: string;
  rpeTarget: string;
  sampleRep: number;
}

interface Phase1Spec {
  repTarget: string;
  tier: 'compound' | 'isolation' | 'primer' | 'tendon';
  sampleRep: number;
}

const PHASE1_RX: Record<string, Phase1Spec> = {
  'low-bar-squat': { repTarget: '6–8 reps', tier: 'compound', sampleRep: 8 },
  'romanian-deadlift': { repTarget: '8–10 reps', tier: 'compound', sampleRep: 10 },
  'standing-calf-raise': { repTarget: '12–15 reps', tier: 'isolation', sampleRep: 15 },
  'cable-crunch': { repTarget: '12–15 reps', tier: 'isolation', sampleRep: 15 },
  'triceps-isometric-hold': { repTarget: '45s hold', tier: 'primer', sampleRep: 45 },
  'barbell-bench-press': { repTarget: '6–8 reps', tier: 'compound', sampleRep: 8 },
  'smith-machine-row': { repTarget: '10–12 reps', tier: 'compound', sampleRep: 12 },
  'seated-dumbbell-shoulder-press': { repTarget: '8–10 reps', tier: 'compound', sampleRep: 10 },
  'pull-up': { repTarget: '6–10 reps', tier: 'compound', sampleRep: 8 },
  'dumbbell-lateral-raise': { repTarget: '12–15 reps', tier: 'isolation', sampleRep: 12 },
  'cable-triceps-pushdown': { repTarget: '12–15 reps', tier: 'tendon', sampleRep: 12 },
  deadlift: { repTarget: '5–6 reps', tier: 'compound', sampleRep: 5 },
  'bulgarian-split-squat': { repTarget: '8–10 / leg', tier: 'compound', sampleRep: 10 },
  'smith-machine-overhead-press': { repTarget: '8–10 reps', tier: 'compound', sampleRep: 10 },
  'lat-pulldown': { repTarget: '10–12 reps', tier: 'compound', sampleRep: 12 },
  'chest-fly-machine': { repTarget: '12–15 reps', tier: 'isolation', sampleRep: 12 },
  'dumbbell-row': { repTarget: '10–12 / side', tier: 'compound', sampleRep: 10 },
  'rear-delt-fly': { repTarget: '12–15 reps', tier: 'isolation', sampleRep: 15 },
  'dumbbell-curl': { repTarget: '10–12 reps', tier: 'isolation', sampleRep: 12 },
};

function rpeForTier(tier: Phase1Spec['tier'], week: number): string {
  if (tier === 'primer') return 'RPE 5';
  if (tier === 'tendon') return 'RPE 7–7.5';
  if (tier === 'compound') {
    if (week <= 1) return 'RPE 7';
    if (week === 2) return 'RPE 7.5';
    if (week === 3) return 'RPE 7.5–8';
    return 'RPE 8';
  }
  if (week <= 1) return 'RPE 7.5';
  if (week <= 3) return 'RPE 8';
  return 'RPE 8–8.5';
}

/** Returns week-scaled autoregulated rep-window and RPE target for an exercise. */
export function prescriptionFor(ex: Exercise, week = 2): CoachPrescription {
  const w = Math.max(1, Math.floor(week));
  const spec = PHASE1_RX[ex.id];
  if (spec) {
    return {
      repTarget: spec.repTarget,
      rpeTarget: rpeForTier(spec.tier, w),
      sampleRep: spec.sampleRep,
    };
  }
  const compoundRpe = rpeForTier('compound', w);
  const isoRpe = rpeForTier('isolation', w);
  if (ex.type === 'time') return { repTarget: '45s hold', rpeTarget: 'RPE 7', sampleRep: 45 };
  if (ex.type === 'bodyweight') return { repTarget: '8–12 reps', rpeTarget: compoundRpe, sampleRep: 10 };
  if (ex.equip === 'bb') return { repTarget: '6–8 reps', rpeTarget: compoundRpe, sampleRep: 8 };
  if (ex.equip === 'db' || ex.equip === 'smith') return { repTarget: '8–12 reps', rpeTarget: compoundRpe, sampleRep: 10 };
  return { repTarget: '12–15 reps', rpeTarget: isoRpe, sampleRep: 12 };
}
