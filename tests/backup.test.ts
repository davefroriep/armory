import { describe, expect, it } from 'vitest';
import { DEFAULTS } from '../src/store/settings';
import { assembleFullBackup, parseBackupJson } from '../src/store/backupCodec';

describe('Armory Fit v2 full backup & restore parser', () => {
  it('assembles a complete v2 backup with cardio samples, lift sessions, and queue while preserving apiKey by default', () => {
    const out = assembleFullBackup({
      exportedMs: 1_763_300_000_000,
      rev: 'v0.36',
      liftBackup: {
        app: 'armory-lifts',
        v: 1,
        exportedMs: 1_763_300_000_000,
        sessions: [
          {
            id: 'ls-1',
            name: 'Upper 1',
            routineId: 'day-upper-1',
            eventId: null,
            startMs: 1_763_290_000_000,
            endMs: 1_763_293_000_000,
            status: 'done',
            exs: [],
            skipped: [{ exId: 'pull-up', plannedSets: 3, doneSets: 0 }],
            rpe: 7,
            bwKg: 88.5,
          },
        ],
        routines: [],
        exercises: [],
        videos: [],
      },
      workoutsMeta: [
        {
          id: 'cb-1',
          equipment: 'airbike',
          programId: 'v5x3',
          name: 'AirBike Z5 — Classic VO2 5×3',
          startEpochMs: 1_763_295_000_000,
          status: 'saved',
          seconds: 1800,
          kj: 280,
          avgW: 155,
          maxW: 245,
          avgHr: 148,
          maxHr: 171,
          rpe: 8,
          eventId: null,
          upload: 'done',
          patched: true,
          activityId: 'i999',
          lastError: null,
        },
      ],
      workoutSamples: {
        'cb-1': [{ t: 0, w: 127, cad: 44, hr: 112, dist: 5, kcal: 0.2, tr: 44 }],
      },
      liftSettings: {
        unit: 'lb',
        restSec: 90,
        restAuto: false,
        restSound: true,
        showTimer: false,
        showAutoTimer: false,
        videoDelay: 5,
      },
      appSettings: {
        ...DEFAULTS,
        apiKey: 'secret-should-not-export',
        ftp: 190,
      },
    });

    expect(out.app).toBe('armory-fit-backup');
    expect(out.v).toBe(2);
    expect(out.schema).toBe(3);
    expect('apiKey' in out.appSettings).toBe(true);
    expect(out.appSettings.ftp).toBe(190);

    const withSecret = assembleFullBackup({
      exportedMs: out.exportedMs,
      liftBackup: { app: 'armory-lifts', v: 1, exportedMs: out.exportedMs, sessions: out.sessions, routines: out.routines, exercises: out.exercises, videos: out.videos },
      workoutsMeta: out.workoutsMeta,
      workoutSamples: out.workoutSamples,
      liftSettings: out.liftSettings,
      appSettings: { ...out.appSettings, apiKey: 'test-key-xyz' } as any,
    });
    expect(withSecret.appSettings.apiKey).toBe('test-key-xyz');

    const stripped = assembleFullBackup({
      exportedMs: out.exportedMs,
      liftBackup: { app: 'armory-lifts', v: 1, exportedMs: out.exportedMs, sessions: out.sessions, routines: out.routines, exercises: out.exercises, videos: out.videos },
      workoutsMeta: out.workoutsMeta,
      workoutSamples: out.workoutSamples,
      liftSettings: out.liftSettings,
      appSettings: { ...out.appSettings, apiKey: 'test-key-xyz' } as any,
      includeSecrets: false,
    });
    expect('apiKey' in stripped.appSettings).toBe(false);
    expect(parseBackupJson(JSON.stringify({ ...out, v: 3 })).v).toBe(3);
    expect(out.sessions[0].skipped).toEqual([{ exId: 'pull-up', plannedSets: 3, doneSets: 0 }]);
    expect(out.workoutsMeta).toHaveLength(1);
    expect(out.workoutSamples['cb-1']).toHaveLength(1);

    const parsed = parseBackupJson(JSON.stringify(out));
    expect(parsed.app).toBe('armory-fit-backup');
  });

  it('still parses legacy v1 armory-lifts backups and rejects unknown JSON', () => {
    const legacy = parseBackupJson(JSON.stringify({ app: 'armory-lifts', v: 1, exportedMs: 1, sessions: [], routines: [], exercises: [] }));
    expect(legacy.app).toBe('armory-lifts');
    expect(() => parseBackupJson(JSON.stringify({ app: 'other', sessions: [] }))).toThrow(/Not a valid Armory Fit backup/);
  });
});
