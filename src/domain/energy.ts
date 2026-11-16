/**
 * Heart-rate based energy expenditure for sessions with no power meter (strength).
 *
 * Keytel et al. (2005), "Prediction of energy expenditure from heart rate monitoring during submaximal
 * exercise", J Sports Sci 23(3): the standard open regression behind most HR-only calorie estimates
 * (commercial watches use proprietary variants). It needs sex, body weight and age.
 *   male   kcal/min = (-55.0969 + 0.6309·HR + 0.1988·kg + 0.2017·age) / 4.184
 *   female kcal/min = (-20.4022 + 0.4472·HR - 0.1263·kg + 0.0740·age) / 4.184
 * Only seconds with a real heart-rate reading count, so sensor gaps never add invented energy.
 */
export interface BodyProfile {
  sex: 'M' | 'F';
  weightKg: number;
  ageYears: number;
}

export function keytelKcalPerMin(hr: number, p: BodyProfile): number {
  const kj = p.sex === 'F'
    ? -20.4022 + 0.4472 * hr - 0.1263 * p.weightKg + 0.074 * p.ageYears
    : -55.0969 + 0.6309 * hr + 0.1988 * p.weightKg + 0.2017 * p.ageYears;
  return Math.max(0, kj / 4.184);
}

/** Total kcal for 1 Hz samples (each sample = one second). Samples with hr <= 30 are gaps and skipped. */
export function kcalFromSamples(samples: { hr: number }[], p: BodyProfile): number {
  let kcal = 0;
  for (const s of samples) if (s.hr > 30) kcal += keytelKcalPerMin(s.hr, p) / 60;
  return Math.round(kcal);
}

/** Whole years between an ISO date (YYYY-MM-DD) and `at`; null if the date is missing or invalid. */
export function ageOn(birthDate: string | null | undefined, at: Date = new Date()): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(birthDate ?? '');
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  let age = at.getFullYear() - y;
  if (at.getMonth() + 1 < mo || (at.getMonth() + 1 === mo && at.getDate() < d)) age--;
  return age > 0 && age < 120 ? age : null;
}

/** Builds a profile from stored settings, or null if weight / birth date are not known yet. */
export function bodyProfile(s: { sex: 'M' | 'F'; weightKg: number | null; birthDate: string | null }, at: Date = new Date()): BodyProfile | null {
  const age = ageOn(s.birthDate, at);
  if (!s.weightKg || s.weightKg <= 0 || age == null) return null;
  return { sex: s.sex, weightKg: s.weightKg, ageYears: age };
}
