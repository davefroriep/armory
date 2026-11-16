import type { Equipment } from '../types';

export interface IntervalsEvent {
  id: number;
  name?: string;
  type?: string;
  category?: string;
  description?: string;
  start_date_local?: string;
  moving_time?: number;
  icu_training_load?: number;
  external_id?: string | null;
  paired_activity_id?: string | null;
}

export interface EventMatch {
  equipment: Equipment | null;
  /** AirBike program id when the calendar title identifies one exactly. */
  programId: string | null;
  zone: 'Z2' | 'Z5' | null;
}

/**
 * Turns a calendar title like "AirBike Z5 — Norwegian 4×4" or "Spin Bike Z2" into an equipment tab
 * (and, for AirBike, an exact program when the title names one).
 */
export function classifyEvent(ev: Pick<IntervalsEvent, 'name' | 'type'>): EventMatch {
  const n = (ev.name ?? '').toLowerCase().replace(/×/g, 'x').replace(/[–—]/g, '-');
  const type = (ev.type ?? '').toLowerCase();

  let equipment: Equipment | null = null;
  if (/air\s*-?bike|assault|echo bike|fan bike/.test(n)) equipment = 'airbike';
  else if (/spin|bowflex|\bc7\b|indoor cycle|ss2k/.test(n)) equipment = 'spin';
  else if (/treadmill|\brun\b|running|\bwalk|\bjog/.test(n) || type === 'run' || type === 'virtualrun') equipment = 'treadmill';
  else if (/strength|lift|weights?|squat|bench|deadlift|\bpush\b|\bpull\b|\blegs?\b|upper|lower/.test(n) || type === 'weighttraining') equipment = 'strength';

  const zone = /\bz2\b|zone ?2/.test(n) ? 'Z2' : /\bz5\b|zone ?5|vo2/.test(n) ? 'Z5' : null;

  let programId: string | null = null;
  if (equipment === 'airbike') {
    if (/norwegian|4x4/.test(n)) programId = 'n4x4';
    else if (/5x3|classic vo2/.test(n)) programId = 'v5x3';
    else if (/6x2|cardiac/.test(n)) programId = 'p6x2';
    else if (/60\/30|billat|ronnestad|rønnestad/.test(n)) programId = 'b6030';
    else if (/tabata|20\/10/.test(n)) programId = 'tabata';
    else if (/10\/20|sprint/.test(n)) programId = 'sprint1020';
    else if (zone === 'Z2') programId = 'z2';
    else if (/free ?ride/.test(n)) programId = 'free';
  }
  return { equipment, programId, zone };
}

/** Only planned workouts matter; notes, holidays, races, etc. are ignored. */
export const isPlannedWorkout = (ev: Pick<IntervalsEvent, 'category'>): boolean =>
  !ev.category || ev.category === 'WORKOUT';
