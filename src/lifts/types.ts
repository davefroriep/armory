// Lift tracker data model. Weights are stored in kilograms (canonical); the UI converts to lb/kg.

export const MUSCLES = ['ch', 'la', 'ub', 'tr', 'fd', 'sd', 'rd', 'bi', 'tri', 'fa', 'qu', 'ha', 'gl', 'ca', 'ab', 'lb', 'ad'] as const;
export type Muscle = (typeof MUSCLES)[number];

export type Equip = 'bb' | 'db' | 'cable' | 'machine' | 'smith' | 'bw' | 'other';
/** weight = load x reps · bodyweight = (bodyweight + added load) x reps · time = seconds (optional load) */
export type ExType = 'weight' | 'bodyweight' | 'time';
/** n = normal working set · w = warm-up (never counted) · d = drop set · f = to failure */
export type SetKind = 'n' | 'w' | 'd' | 'f';

export interface Exercise {
  id: string;
  name: string;
  type: ExType;
  equip: Equip;
  primary: Muscle[];
  secondary: Muscle[];
  custom?: boolean;
  /** Hidden from the picker (custom exercises that still have history). */
  archived?: boolean;
}

/** Persisted per-exercise row: a full definition for custom exercises, or just overrides for library ones. */
export interface ExRow extends Partial<Exercise> {
  id: string;
  /** Sticky note shown every time the exercise comes up ("seat 4, neutral handles"). */
  note?: string;
  /** Rest-timer length for this exercise (seconds). */
  restSec?: number;
  /** Per-exercise auto-rest override (when checking a set). */
  restAuto?: boolean;
  /** Permanent preferred Gym Cam ("Rack Cam" | "Smith Cam" | ... | "auto"). */
  camPref?: string;
}

export interface LiftSet {
  id: string;
  /** Load in kg (added load for bodyweight exercises). null = not entered. */
  w: number | null;
  /** Reps (seconds for time exercises). null = not entered. */
  r: number | null;
  done: boolean;
  kind: SetKind;
  rpe?: number;
  note?: string;
  /** Video ids linked to this set / attempt. */
  vids?: string[];
  /** The value is a suggestion (last time / forward-populated), not typed by the user. */
  wAuto?: boolean;
  rAuto?: boolean;
  /** Epoch ms the set was logged. */
  at?: number;
}

export interface SetRxSpec {
  reps?: number;
  pct?: number;
  loadLb?: number;
  loadKg?: number;
  rpe?: number;
  kind?: SetKind;
}

export interface ExerciseRx {
  reps?: string | number;
  rpe?: number | string;
  pct?: number;
  loadLb?: number;
  loadKg?: number;
  tmLb?: number;
  setsRx?: SetRxSpec[];
}

export interface SessionEx {
  uid: string;
  exId: string;
  sets: LiftSet[];
  note?: string;
  rx?: ExerciseRx;
  /** Superset with the next exercise (no rest between them). */
  ss?: boolean;
  /** Rest override in seconds for this exercise in this session. */
  rest?: number;
  /** Per-card auto-rest override for this session. */
  restAuto?: boolean;
  /** Original 0-based index inside the routine this session was started from. */
  slot?: number;
  /** Last explicitly chosen Gym-Cam station ("Rack Cam" | "Smith Cam") for this exercise in this session. */
  camPref?: string;
}

export interface SkippedEx {
  exId: string;
  plannedSets: number;
  doneSets: number;
  ss?: boolean;
}

export interface LiftSession {
  id: string;
  name: string;
  routineId: string | null;
  /** Intervals.icu planned-workout id this session fulfils. */
  eventId: number | null;
  /** Out-of-order / cherry-picked rolling plan slot (`"${stepIndex}:${slotIndex}"`). */
  planSlot?: string | null;
  startMs: number;
  endMs: number | null;
  pausedAt?: number | null;
  pausedMs?: number;
  status: 'active' | 'done';
  /** True once the workout has started (via Start button, set check, or camera). */
  started?: boolean;
  /** True when started explicitly via Start button or camera (prevents unchecking Set 1 from resetting clock). */
  manualStart?: boolean;
  exs: SessionEx[];
  /** Exercises or working sets left unlogged when the session was finished. */
  skipped?: SkippedEx[];
  note?: string;
  /** Session effort 3-10 (same scale as the rest of the app). */
  rpe: number | null;
  /** Bodyweight snapshot (kg) for bodyweight exercises. */
  bwKg: number | null;
  /** Optional OptiPlex Gym-Cam DVR session identifier for 2-week Session Tape lookup. */
  camSessionId?: string;
  /** True when lifting with a buddy so motion-based auto camera selection avoids hijacking clips. */
  buddyMode?: boolean;
}

export interface RoutineItem {
  exId: string;
  sets: number;
  ss?: boolean;
  rx?: ExerciseRx;
}

/** A repeating training day ("Upper 1"). Built by the user; nothing ships pre-programmed. */
export interface Routine {
  id: string;
  name: string;
  items: RoutineItem[];
  createdMs: number;
  order: number;
}

export interface VideoMeta {
  id: string;
  sessionId: string;
  exId: string;
  setId: string;
  ms: number;
  mime: string;
  bytes: number;
  /** Small JPEG data URL poster frame. */
  thumb: string;
  /** Original camera / DCIM filename (e.g. "PXL_20261116_173012345.mp4") for Google Drive DCIM Backup search. */
  fileName?: string;
  /** File's own lastModified epoch ms on device. */
  fileMs?: number;
  /** Clip duration in seconds if known. */
  durationSec?: number;
  /** Persistent Android content:// URI (Capacitor APK) or direct https:// video link. */
  uri?: string;
  /** True when only metadata + Drive/DCIM reference is stored (no video blob in IndexedDB). */
  linkOnly?: boolean;
  /** Google Drive search query when filename was inferred from MP4 timestamp (e.g. "PXL 20261116 1856 type:video"). */
  driveQuery?: string;
  /** Clip capture origin. */
  source?: 'gymcam' | 'phone' | 'dcim';
  /** Gym-Cam camera label (e.g. "Rack Cam" or "Smith Cam"). */
  camera?: string;
  /** Direct LAN HTTP stream/MP4 URL on the OptiPlex Gym-Cam DVR. */
  lanUrl?: string;
  /** Original clip start/end UTC epoch ms on Gym-Cam server (supports ±5s tape pulls). */
  clipStartMs?: number;
  clipEndMs?: number;
  /** Non-destructive in-app trim start/end in seconds within the clip. */
  trimStartSec?: number;
  trimEndSec?: number;
  /** True when the saved Gym-Cam MP4 contains baked room audio (PowerConf S330). */
  hasAudio?: boolean;
  /** True for Guest / Buddy clips stored outside set.vids tracking. */
  guest?: boolean;
}

export const uid = (): string => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
