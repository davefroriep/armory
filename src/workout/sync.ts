import { buildTcx } from '../domain/tcx';
import { patchActivity, uploadActivity } from '../intervals/api';
import { allMeta, deleteWorkout, getSamples, putMeta } from '../store/db';
import type { WorkoutMeta } from '../types';

/**
 * Uploads a saved workout to Intervals.icu. Two idempotent steps (TCX upload, then RPE/type patch)
 * so a failure at either one is retried later without duplicating the activity.
 */
export async function syncWorkout(meta: WorkoutMeta): Promise<WorkoutMeta> {
  const m = { ...meta };
  try {
    if (!m.activityId) {
      const samples = await getSamples(m.id);
      const tcx = buildTcx(
        { startEpochMs: m.startEpochMs, seconds: m.seconds, kj: m.kj, samples },
        m.name,
      );
      const desc = `Armory Fit • ${fmt(m.seconds)} • avg ${Math.round(m.avgW)} W • ${Math.round(m.kj)} kJ` + (m.rpe ? ` • RPE ${m.rpe}/10` : '');
      m.activityId = await uploadActivity({
        tcx, name: m.name, description: desc, externalId: `armory-${m.id}`, pairedEventId: m.eventId,
      });
      await putMeta(m);
    }
    if (!m.patched) {
      await patchActivity(m.activityId, { type: 'VirtualRide', ...(m.rpe ? { icu_rpe: m.rpe } : {}) });
      m.patched = true;
    }
    m.upload = 'done';
    m.lastError = null;
  } catch (e) {
    m.upload = 'pending';
    m.lastError = (e as Error).message;
  }
  await putMeta(m);
  return m;
}

/** Retries anything saved but not yet uploaded (offline at the gym, bad key, ...). */
export async function retryPending(): Promise<number> {
  let done = 0;
  for (const m of await allMeta()) {
    if (m.status === 'saved' && m.upload === 'pending') {
      const r = await syncWorkout(m);
      if (r.upload === 'done') done++;
    }
  }
  return done;
}

export async function pendingCount(): Promise<number> {
  return (await allMeta()).filter((m) => m.status === 'saved' && m.upload === 'pending').length;
}

export async function discard(id: string): Promise<void> {
  await deleteWorkout(id);
}

function fmt(sec: number): string {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}
