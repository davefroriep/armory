import type { Exercise } from './types';

export interface CoachPrescription {
  repTarget: string;
  rpeTarget: string;
  cue: string;
  sampleRep: number;
}

interface Phase1Spec {
  repTarget: string;
  tier: 'compound' | 'isolation' | 'primer' | 'tendon';
  cue: string;
  sampleRep: number;
}

const PHASE1_RX: Record<string, Phase1Spec> = {
  'low-bar-squat': { repTarget: '6–8 reps', tier: 'compound', cue: 'AM Knee Check · controlled depth', sampleRep: 8 },
  'romanian-deadlift': { repTarget: '8–10 reps', tier: 'compound', cue: 'Hinge back · 2s eccentric stretch', sampleRep: 10 },
  'standing-calf-raise': { repTarget: '12–15 reps', tier: 'isolation', cue: '1s pause at top & bottom stretch', sampleRep: 15 },
  'cable-crunch': { repTarget: '12–15 reps', tier: 'isolation', cue: 'Ribs to pelvis · full exhale', sampleRep: 15 },
  'triceps-isometric-hold': { repTarget: '45s hold', tier: 'primer', cue: '90° elbow · motionless tendon primer', sampleRep: 45 },
  'barbell-bench-press': { repTarget: '6–8 reps', tier: 'compound', cue: 'Controlled touch · match right arm', sampleRep: 8 },
  'smith-machine-row': { repTarget: '10–12 reps', tier: 'compound', cue: 'Drive elbows back · squeeze lats', sampleRep: 12 },
  'seated-dumbbell-shoulder-press': { repTarget: '8–10 reps', tier: 'compound', cue: 'Smooth press · match right triceps', sampleRep: 10 },
  'pull-up': { repTarget: '6–10 reps', tier: 'compound', cue: 'Full hang to chin over bar', sampleRep: 8 },
  'dumbbell-lateral-raise': { repTarget: '12–15 reps', tier: 'isolation', cue: 'Lead with elbows · smooth control', sampleRep: 12 },
  'cable-triceps-pushdown': { repTarget: '12–15 reps', tier: 'tendon', cue: '1:1:1 tempo · hold to right arm tolerance', sampleRep: 12 },
  deadlift: { repTarget: '5–6 reps', tier: 'compound', cue: 'Pull slack out · crisp bar speed', sampleRep: 5 },
  'bulgarian-split-squat': { repTarget: '8–10 / leg', tier: 'compound', cue: 'AM Knee Check · smooth knee track', sampleRep: 10 },
  'smith-machine-overhead-press': { repTarget: '8–10 reps', tier: 'compound', cue: 'Smooth press · leave 2 reps in tank', sampleRep: 10 },
  'lat-pulldown': { repTarget: '10–12 reps', tier: 'compound', cue: 'Chest tall · pull elbows to ribs', sampleRep: 12 },
  'chest-fly-machine': { repTarget: '12–15 reps', tier: 'isolation', cue: 'Controlled stretch · squeeze front', sampleRep: 12 },
  'dumbbell-row': { repTarget: '10–12 / side', tier: 'compound', cue: 'Full stretch · drive elbow to hip', sampleRep: 10 },
  'rear-delt-fly': { repTarget: '12–15 reps', tier: 'isolation', cue: 'Wide arc · rear delts', sampleRep: 15 },
  'dumbbell-curl': { repTarget: '10–12 reps', tier: 'isolation', cue: 'Elbows pinned · full supination', sampleRep: 12 },
};

function rpeForTier(tier: Phase1Spec['tier'], week: number): string {
  if (tier === 'primer') return 'RPE 5 (primer)';
  if (tier === 'tendon') return 'RPE 7–7.5 (2–3 RIR)';
  if (tier === 'compound') {
    if (week <= 1) return 'RPE 7 (3 RIR)';
    if (week === 2) return 'RPE 7.5 (2–3 RIR)';
    if (week === 3) return 'RPE 7.5–8 (2 RIR)';
    return 'RPE 8 (2 RIR)';
  }
  if (week <= 1) return 'RPE 7.5 (2–3 RIR)';
  if (week <= 3) return 'RPE 8 (1–2 RIR)';
  return 'RPE 8–8.5 (1–2 RIR)';
}

/** Returns week-scaled autoregulated rep-window, RPE/RIR target, and execution cue for an exercise. */
export function prescriptionFor(ex: Exercise, week = 2): CoachPrescription {
  const w = Math.max(1, Math.floor(week));
  const spec = PHASE1_RX[ex.id];
  if (spec) {
    return {
      repTarget: spec.repTarget,
      rpeTarget: rpeForTier(spec.tier, w),
      cue: spec.cue,
      sampleRep: spec.sampleRep,
    };
  }
  const compoundRpe = rpeForTier('compound', w);
  const isoRpe = rpeForTier('isolation', w);
  if (ex.type === 'time') return { repTarget: '45s hold', rpeTarget: 'RPE 7', cue: '', sampleRep: 45 };
  if (ex.type === 'bodyweight') return { repTarget: '8–12 reps', rpeTarget: compoundRpe, cue: '', sampleRep: 10 };
  if (ex.equip === 'bb') return { repTarget: '6–8 reps', rpeTarget: compoundRpe, cue: '', sampleRep: 8 };
  if (ex.equip === 'db' || ex.equip === 'smith') return { repTarget: '8–12 reps', rpeTarget: compoundRpe, cue: '', sampleRep: 10 };
  return { repTarget: '12–15 reps', rpeTarget: isoRpe, cue: '', sampleRep: 12 };
}
