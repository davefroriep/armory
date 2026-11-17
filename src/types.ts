export type Equipment = 'strength' | 'treadmill' | 'spin' | 'airbike';

export const EQUIPMENT_LABEL: Record<Equipment, string> = {
  strength: 'Strength',
  treadmill: 'Treadmill',
  spin: 'Spin Bike',
  airbike: 'AirBike',
};

/** One recorded second of a workout. */
export interface Sample {
  /** Active seconds since workout start. */
  t: number;
  w: number;
  cad: number;
  hr: number;
  /** Cumulative session metres. */
  dist: number;
  /** Cumulative session Assault calories. */
  kcal: number;
  /** Target RPM for this second (0 = none). */
  tr: number;
}

export type UploadState = 'none' | 'pending' | 'done';

export interface WorkoutMeta {
  id: string;
  equipment: Equipment;
  programId: string;
  name: string;
  startEpochMs: number;
  /** 'active' until Finish; recovered on next launch if the app died mid-workout. */
  status: 'active' | 'finished' | 'saved' | 'discarded';
  seconds: number;
  kj: number;
  avgW: number;
  maxW: number;
  avgHr: number;
  maxHr: number;
  rpe: number | null;
  eventId: number | null;
  /** Out-of-order / cherry-picked rolling plan slot (`"${stepIndex}:${slotIndex}"`). */
  planSlot?: string | null;
  upload: UploadState;
  /** True once the Intervals activity has its RPE / type patched. */
  patched: boolean;
  activityId: string | null;
  lastError: string | null;
  /** Per-rep / recovery progress line appended to the Intervals.icu description. */
  notes?: string;
}
