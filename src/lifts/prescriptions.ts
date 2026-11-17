import { KG_PER_LB, type Unit } from './settings';
import type { Exercise, ExerciseRx, SetKind, SetRxSpec } from './types';

export interface CoachPrescription {
  repTarget: string;
  rpeTarget: string;
  sampleRep: number;
}

export interface ResolvedPrescription extends CoachPrescription {
  roadmapLine: string;
  prescribedSets: { wKg: number | null; r: number | null; kind?: SetKind }[] | null;
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

function roundLoadKg(rawKg: number, unit: Unit): number {
  if (unit === 'lb') {
    const rawLb = rawKg / KG_PER_LB;
    const step = rawLb < 30 ? 2.5 : 5;
    const roundedLb = Math.max(0, Math.round(rawLb / step) * step);
    return roundedLb * KG_PER_LB;
  }
  const step = 2.5;
  return Math.max(0, Math.round(rawKg / step) * step);
}

function fmtLoadDisp(wKg: number, unit: Unit): string {
  const val = unit === 'kg'
    ? Math.round(wKg * 10) / 10
    : Math.round((wKg / KG_PER_LB) * 10) / 10;
  return String(val);
}

function parseSampleRep(val: string | number | undefined, fallback: number): number {
  if (typeof val === 'number' && Number.isFinite(val) && val > 0) return Math.round(val);
  if (typeof val === 'string') {
    const nums = val.match(/\d+/g);
    if (nums?.length) {
      const last = Number(nums[nums.length - 1]);
      if (Number.isFinite(last) && last > 0) return last;
    }
  }
  return fallback;
}

/**
 * Universal prescription resolver supporting explicit `ExerciseRx` (RPE/rep-range, % 1RM/TM, or
 * fixed/ramping `setsRx`) with automatic fallback to `prescriptionFor()` for ad-hoc exercises.
 */
export function resolvePrescription(
  ex: Exercise | undefined,
  opts?: { rx?: ExerciseRx; sets?: number; week?: number; base1RmKg?: number | null; unit?: Unit },
): ResolvedPrescription {
  const exSafe: Exercise = ex ?? { id: '', name: '', type: 'weight', equip: 'other', primary: [], secondary: [] };
  const week = opts?.week ?? 2;
  const unit: Unit = opts?.unit ?? 'lb';
  const rx = opts?.rx;
  const nSets = Math.max(1, opts?.sets ?? rx?.setsRx?.length ?? 3);
  const base = prescriptionFor(exSafe, week);

  if (!rx) {
    const shortRep = base.repTarget.replace(/\s*(?:reps?|hold|\/\s*leg|\/\s*side)$/i, '').trim();
    return {
      ...base,
      roadmapLine: `${nSets} × ${shortRep} · ${base.rpeTarget}`,
      prescribedSets: null,
    };
  }

  const rawRpe = rx.rpe != null && rx.rpe !== '' ? String(rx.rpe).trim() : '';
  const rpeStr = rawRpe ? (/^RPE\b/i.test(rawRpe) ? rawRpe.replace(/^rpe/i, 'RPE') : `RPE ${rawRpe}`) : '';
  const lastRxRep = rx.setsRx?.length ? rx.setsRx[rx.setsRx.length - 1]?.reps : undefined;
  const sampleRep = parseSampleRep(lastRxRep ?? rx.reps, base.sampleRep);
  const ref1RmKg = rx.tmLb != null ? rx.tmLb * KG_PER_LB : (opts?.base1RmKg ?? null);

  let rawSpecs: SetRxSpec[] = [];
  if (rx.setsRx?.length) {
    rawSpecs = rx.setsRx;
  } else if (rx.loadLb != null || rx.loadKg != null || rx.pct != null) {
    rawSpecs = Array.from({ length: nSets }, () => ({
      reps: typeof rx.reps === 'number' ? rx.reps : sampleRep,
      pct: rx.pct,
      loadLb: rx.loadLb,
      loadKg: rx.loadKg,
      kind: 'n' as SetKind,
    }));
  }

  const prescribedSets = rawSpecs.length > 0
    ? rawSpecs.map((sp) => {
        let wKg: number | null = null;
        if (sp.loadKg != null) wKg = sp.loadKg;
        else if (sp.loadLb != null) wKg = sp.loadLb * KG_PER_LB;
        else if (sp.pct != null && ref1RmKg != null && ref1RmKg > 0) {
          wKg = roundLoadKg(ref1RmKg * (sp.pct / 100), unit);
        }
        const r = sp.reps ?? (typeof rx.reps === 'number' ? rx.reps : null);
        return { wKg, r, kind: sp.kind ?? 'n' };
      })
    : null;

  let repTarget = base.repTarget;
  if (rx.setsRx?.length) {
    const setReps = rx.setsRx.map((s) => s.reps ?? sampleRep);
    if (new Set(setReps).size > 1) {
      repTarget = `${setReps.join('/')} reps`;
    } else {
      const onlyRep = setReps[0] ?? sampleRep;
      repTarget = exSafe.type === 'time' ? `${onlyRep}s hold` : `${onlyRep} reps`;
    }
  } else if (exSafe.type === 'time') {
    const raw = rx.reps != null ? String(rx.reps).trim() : base.repTarget;
    repTarget = typeof rx.reps === 'number' ? `${rx.reps}s hold` : (/hold$/i.test(raw) ? raw : `${raw} hold`);
  } else if (rx.reps != null) {
    const raw = String(rx.reps).trim();
    repTarget = typeof rx.reps === 'number'
      ? `${rx.reps} reps`
      : (/(?:reps?|\/\s*leg|\/\s*side)$/i.test(raw) ? raw : `${raw} reps`);
  }

  const setPcts = (rx.setsRx ?? []).map((s) => s.pct).filter((p): p is number => p != null);
  const pctLabel = rx.pct != null
    ? `${rx.pct}%`
    : setPcts.length
      ? (new Set(setPcts).size === 1 ? `${setPcts[0]}%` : `${setPcts.join('/')}%`)
      : '';
  const rpeTarget = [pctLabel, rpeStr].filter(Boolean).join(' · ');

  const shortRep = exSafe.type === 'time'
    ? (typeof rx.reps === 'number' ? `${rx.reps}s` : String(rx.reps ?? base.repTarget).replace(/\s*hold$/i, '').trim())
    : String(rx.reps ?? sampleRep).replace(/\s*(?:reps?|\/\s*leg|\/\s*side)$/i, '').trim();

  let roadmapLine = '';
  if (prescribedSets && prescribedSets.every((ps) => ps.wKg != null && ps.r != null)) {
    const firstW = fmtLoadDisp(prescribedSets[0].wKg!, unit);
    const firstR = prescribedSets[0].r!;
    const uniform = prescribedSets.every(
      (ps) => fmtLoadDisp(ps.wKg!, unit) === firstW && ps.r === firstR && (ps.kind ?? 'n') === (prescribedSets[0].kind ?? 'n'),
    );
    if (uniform) {
      const pctSuffix = pctLabel ? ` (${pctLabel})` : '';
      roadmapLine = `${prescribedSets.length} × ${firstR} @ ${firstW} ${unit}${pctSuffix}${rpeStr ? ` · ${rpeStr}` : ''}`;
    } else {
      roadmapLine = `${prescribedSets.map((ps) => `${fmtLoadDisp(ps.wKg!, unit)}×${ps.r}`).join(' · ')} ${unit}${rpeStr ? ` · ${rpeStr}` : ''}`;
    }
  } else if (rx.pct != null) {
    roadmapLine = `${nSets} × ${shortRep} @ ${rx.pct}%${rpeStr ? ` · ${rpeStr}` : ''}`;
  } else if (prescribedSets && pctLabel) {
    roadmapLine = `${repTarget.replace(/\s*reps$/i, '')} @ ${pctLabel}${rpeStr ? ` · ${rpeStr}` : ''}`;
  } else {
    roadmapLine = `${nSets} × ${shortRep}${rpeStr ? ` · ${rpeStr}` : ''}`;
  }

  return {
    repTarget,
    rpeTarget,
    sampleRep,
    roadmapLine,
    prescribedSets,
  };
}
