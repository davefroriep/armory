import type { LiftBackup } from '../lifts/store';
import type { LiftSettings } from '../lifts/settings';
import type { ExRow, LiftSession, Routine, VideoMeta } from '../lifts/types';
import type { Sample, WorkoutMeta } from '../types';
import type { Settings } from './settings';

export interface FullBackup {
  app: 'armory-fit-backup';
  v: 2;
  rev?: string;
  exportedMs: number;
  sessions: LiftSession[];
  routines: Routine[];
  exercises: ExRow[];
  videos: VideoMeta[];
  workoutsMeta: WorkoutMeta[];
  workoutSamples: Record<string, Sample[]>;
  liftSettings: LiftSettings;
  appSettings: Omit<Settings, 'apiKey'>;
}

/** Pure builder so v2 backup snapshots can be assembled and tested without IndexedDB or DOM. */
export function assembleFullBackup(args: {
  exportedMs: number;
  rev?: string;
  liftBackup: LiftBackup;
  workoutsMeta: WorkoutMeta[];
  workoutSamples: Record<string, Sample[]>;
  liftSettings: LiftSettings;
  appSettings: Settings;
}): FullBackup {
  const { apiKey: _stripKey, ...safeAppSettings } = args.appSettings;
  return {
    app: 'armory-fit-backup',
    v: 2,
    ...(args.rev ? { rev: args.rev } : {}),
    exportedMs: args.exportedMs,
    sessions: args.liftBackup.sessions,
    routines: args.liftBackup.routines,
    exercises: args.liftBackup.exercises,
    videos: args.liftBackup.videos ?? [],
    workoutsMeta: args.workoutsMeta,
    workoutSamples: args.workoutSamples,
    liftSettings: args.liftSettings,
    appSettings: safeAppSettings,
  };
}

export function parseBackupJson(raw: string): FullBackup | LiftBackup {
  const parsed = JSON.parse(raw) as Record<string, unknown> | null;
  if (parsed?.app === 'armory-fit-backup' && parsed.v === 2 && Array.isArray(parsed.sessions)) {
    return parsed as unknown as FullBackup;
  }
  if (parsed?.app === 'armory-lifts' && Array.isArray(parsed.sessions)) {
    return parsed as unknown as LiftBackup;
  }
  throw new Error('Not a valid Armory Fit backup JSON file');
}
