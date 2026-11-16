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

export interface SessionEx {
  uid: string;
  exId: string;
  sets: LiftSet[];
  note?: string;
  /** Superset with the next exercise (no rest between them). */
  ss?: boolean;
  /** Rest override in seconds for this exercise in this session. */
  rest?: number;
  /** Per-card auto-rest override for this session. */
  restAuto?: boolean;
  /** Original 0-based index inside the routine this session was started from. */
  slot?: number;
}

export interface LiftSession {
  id: string;
  name: string;
  routineId: string | null;
  /** Intervals.icu planned-workout id this session fulfils. */
  eventId: number | null;
  startMs: number;
  endMs: number | null;
  status: 'active' | 'done';
  /** True once at least one set has been checked off (starts elapsed clock & HR recording). */
  started?: boolean;
  exs: SessionEx[];
  note?: string;
  /** Session effort 3-10 (same scale as the rest of the app). */
  rpe: number | null;
  /** Bodyweight snapshot (kg) for bodyweight exercises. */
  bwKg: number | null;
}

export interface RoutineItem {
  exId: string;
  sets: number;
  ss?: boolean;
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
}

export const uid = (): string => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
