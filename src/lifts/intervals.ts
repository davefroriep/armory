import { bodyProfile, kcalFromSamples } from '../domain/energy';
import { buildTcx } from '../domain/tcx';
import { createManualActivity, patchActivity, syncAthleteFtp, uploadActivity } from '../intervals/api';
import { calendar, localDate } from '../intervals/calendar';
import { getSettings } from '../store/settings';
import { getMeta, getSamples, putMeta } from '../store/db';
import type { WorkoutMeta } from '../types';
import { sessionStats } from './calc';
import { liftSettings } from './settings';
import { lifts } from './store';
import { intervalsDescription } from './summary';
import type { LiftSession } from './types';

const localIso = (ms: number): string => {
  const d = new Date(ms);
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/** Heart-rate summary for a recorded second-by-second session (null when no strap data). */
export function hrSummary(meta: WorkoutMeta, samples: { hr: number }[]): { avg: number; max: number; kcal: number | null; has: boolean } {
  const hrs = samples.filter((s) => s.hr > 30);
  const has = hrs.length >= 30;
  const prof = bodyProfile(getSettings());
  return {
    has,
    avg: has ? hrs.reduce((a, s) => a + s.hr, 0) / hrs.length : 0,
    max: has ? Math.max(...hrs.map((s) => s.hr)) : 0,
    kcal: has && prof ? kcalFromSamples(samples as never[], prof) : null,
  };
}

/**
 * Uploads a finished lift session to Intervals.icu, fully automatically:
 *  1. activity created (HR strap data -> TCX with the heart-rate trace, otherwise a manual activity),
 *  2. patched with type WeightTraining, the lift log as the description, and the effort rating.
 * Both steps are idempotent so a failure at either is simply retried later (offline at the gym is fine).
 */
const chain = new Map<string, Promise<unknown>>();

/** Serialised per session, so tapping an effort rating mid-upload can never create a second activity. */
export function syncLiftWorkout(meta: WorkoutMeta, s: LiftSession): Promise<WorkoutMeta> {
  const prev = chain.get(meta.id) ?? Promise.resolve();
  const next = prev.catch(() => {}).then(async () => runSync((await getMeta(meta.id)) ?? meta, s));
  chain.set(meta.id, next);
  void next.finally(() => { if (chain.get(meta.id) === next) chain.delete(meta.id); });
  return next;
}

async function runSync(meta: WorkoutMeta, s: LiftSession): Promise<WorkoutMeta> {
  const m = { ...meta };
  try {
    const samples = await getSamples(m.id);
    let hr = hrSummary(m, samples);
    if (hr.has && hr.kcal == null && !bodyProfile(getSettings())) {
      await syncAthleteFtp(false).catch(() => {});
      hr = hrSummary(m, samples);
    }
    const unit = liftSettings().unit;
    const desc = intervalsDescription({ ...s, rpe: m.rpe ?? s.rpe }, { unit, exOf: lifts.exOrStub, prs: lifts.prs(), avgHr: hr.avg, maxHr: hr.max, kcal: hr.kcal });
    const stats = sessionStats(s, lifts.exOrStub);
    const secs = Math.max(60, stats.durationSec);
    const kgLifted = Math.round(stats.volumeKg);

    if (!m.activityId) {
      const ext = `armory-${m.id}`;
      if (hr.has) {
        const tcx = buildTcx({ startEpochMs: m.startEpochMs, seconds: m.seconds, kj: 0, kcal: hr.kcal ?? undefined, samples, kind: 'hr' }, m.name);
        m.activityId = await uploadActivity({ tcx, name: m.name, description: `Armory Fit · ${m.name}`, externalId: ext, pairedEventId: m.eventId });
      } else {
        m.activityId = await createManualActivity({ startLocalIso: localIso(m.startEpochMs), name: m.name, description: `Armory Fit · ${m.name}`, seconds: secs, type: 'WeightTraining', externalId: ext, pairedEventId: m.eventId, kgLifted });
      }
      await putMeta(m);
    }
    if (!m.patched) {
      await patchActivity(m.activityId, { type: 'WeightTraining', name: m.name, description: desc, ...(m.rpe ? { icu_rpe: m.rpe } : {}) });
      m.patched = true;
      // Intervals.icu has a native "kg lifted" field; best-effort so an unexpected rejection never blocks the log.
      if (kgLifted > 0) await patchActivity(m.activityId, { kg_lifted: kgLifted }).catch(() => {});
    }
    m.upload = 'done';
    m.lastError = null;
  } catch (e) {
    m.upload = 'pending';
    m.lastError = (e as Error).message;
  }
  await putMeta(m);
  if (m.upload === 'done') {
    if (s.planSlot) calendar.markSlotDoneEarly(s.planSlot, m.activityId, localDate(new Date(s.startMs)));
    void calendar.refresh();
  }
  return m;
}

/** After the effort rating or an edit to a saved session: re-send the description / RPE. */
export async function resyncSession(sessionId: string): Promise<WorkoutMeta | null> {
  const meta = await getMeta(sessionId);
  const s = lifts.sessions.get(sessionId);
  if (!meta || !s || meta.status !== 'saved') return null;
  meta.rpe = s.rpe;
  meta.patched = false;
  meta.upload = 'pending';
  await putMeta(meta);
  return syncLiftWorkout(meta, s);
}
