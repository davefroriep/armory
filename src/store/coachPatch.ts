import type { ExtraWorkout, PlanStepOverride } from '../domain/plan';
import { clampProgramTarget, programById } from '../domain/programs';
import { normalizeCamList } from '../lifts/logic';
import { lifts } from '../lifts/store';
import type { ExRow, Routine } from '../lifts/types';
import { toast } from '../ui/lifts/kit';
import { DEFAULT_COACH_SYNC_URL, getSettings, updateSettings, type Settings } from './settings';

export type CoachPatchableSettings = Partial<Pick<
  Settings,
  'ftp' | 'hrMax' | 'recoveryMode' | 'recoveryHrPct' | 'restDay' | 'wallboardRelayEnabled' | 'gymCamEnabled' | 'voiceRoomEnabled'
>>;

export interface CoachPatch {
  /** Globally unique patch id (e.g. "2026-11-25-z5-targets-v1"). Applied at most once per device. */
  id: string;
  /** Short human-readable summary shown in the 1-line confirmation toast. */
  label?: string;
  /** Optional YYYY-MM-DD expiration date; patches past this date are marked skipped without applying. */
  expiresAfter?: string;
  /** Direct per-program work targets (whole RPM for AirBike, watts for Spin ERG). */
  programTargets?: Record<string, number>;
  /** Safe non-secret preference updates (never touches apiKey or athleteId). */
  settings?: CoachPatchableSettings;
  /** Upsert lifting routines by `id` into IndexedDB (`lift_routines`). */
  routines?: Routine[];
  /** Update assigned camera arrays per `exId` in IndexedDB (`lift_ex`). */
  exerciseCams?: Record<string, string[]>;
  /** Append one-off carry-over / extra workouts (deduplicated by `id`). */
  extras?: ExtraWorkout[];
  /** Declarative per-step overrides applied on top of the active RollingPlan. */
  planStepOverrides?: Record<number, PlanStepOverride>;
}

export interface CoachPatchFeed {
  schema: number;
  patches: CoachPatch[];
}

export interface CoachPatchDeps {
  getSettings: () => Settings;
  updateSettings: (patch: Partial<Settings>) => Settings;
  todayYmd: () => string;
  nowMs: () => number;
  upsertRoutine: (r: Routine) => Promise<void>;
  updateExerciseCams: (exId: string, cams: string[]) => Promise<void>;
}

export interface CoachApplyResult {
  applied: CoachPatch[];
  skippedExpired: number;
  latestLabel: string | null;
}

export interface CoachSyncResult {
  ok: boolean;
  throttled?: boolean;
  offline?: boolean;
  appliedCount: number;
  latestLabel: string | null;
  error?: string;
}

const THROTTLE_MS = 5 * 60_000;
const FETCH_TIMEOUT_MS = 6_000;
let lastAttemptMs = 0;
let inflight: Promise<CoachSyncResult> | null = null;

const localYmd = (d = new Date()): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

const defaultDeps: CoachPatchDeps = {
  getSettings,
  updateSettings,
  todayYmd: () => localYmd(),
  nowMs: () => Date.now(),
  upsertRoutine: async (r) => {
    await lifts.ready;
    await lifts.saveRoutine(r);
  },
  updateExerciseCams: async (exId, cams) => {
    await lifts.ready;
    const prev = lifts.row(exId);
    const next: ExRow = { ...(prev ?? { id: exId }), cams: [...cams] };
    delete next.camPref;
    await lifts.saveRow(next);
  },
};

function pickAllowedSettings(raw?: CoachPatchableSettings): Partial<Settings> {
  if (!raw || typeof raw !== 'object') return {};
  const out: Partial<Settings> = {};
  if (typeof raw.ftp === 'number' && Number.isFinite(raw.ftp) && raw.ftp >= 80 && raw.ftp <= 450) {
    out.ftp = Math.round(raw.ftp);
  }
  if (typeof raw.hrMax === 'number' && Number.isFinite(raw.hrMax) && raw.hrMax >= 120 && raw.hrMax <= 230) {
    out.hrMax = Math.round(raw.hrMax);
  }
  if (raw.recoveryMode === 'timed' || raw.recoveryMode === 'gated') {
    out.recoveryMode = raw.recoveryMode;
  }
  if (typeof raw.recoveryHrPct === 'number' && Number.isFinite(raw.recoveryHrPct) && raw.recoveryHrPct >= 50 && raw.recoveryHrPct <= 90) {
    out.recoveryHrPct = Math.round(raw.recoveryHrPct);
  }
  if (raw.restDay === null || (typeof raw.restDay === 'number' && Number.isInteger(raw.restDay) && raw.restDay >= 0 && raw.restDay <= 6)) {
    out.restDay = raw.restDay;
  }
  if (typeof raw.wallboardRelayEnabled === 'boolean') out.wallboardRelayEnabled = raw.wallboardRelayEnabled;
  if (typeof raw.gymCamEnabled === 'boolean') out.gymCamEnabled = raw.gymCamEnabled;
  if (typeof raw.voiceRoomEnabled === 'boolean') out.voiceRoomEnabled = raw.voiceRoomEnabled;
  return out;
}

/**
 * Applies any unapplied patches from `feed` in chronological order, recording each processed `id`
 * in `settings.appliedCoachPatches` so local edits made later on the phone are never overwritten.
 */
export async function applyCoachPatches(
  feed: CoachPatchFeed,
  deps: CoachPatchDeps = defaultDeps,
): Promise<CoachApplyResult> {
  if (!feed || feed.schema !== 1 || !Array.isArray(feed.patches)) {
    return { applied: [], skippedExpired: 0, latestLabel: null };
  }
  const today = deps.todayYmd();
  const applied: CoachPatch[] = [];
  let skippedExpired = 0;

  const cur0 = deps.getSettings();
  const seen = new Set(cur0.appliedCoachPatches ?? []);
  const nextTargets = { ...(cur0.programTargets ?? {}) };
  const nextOverrides: Record<number, PlanStepOverride> = { ...(cur0.planStepOverrides ?? {}) };
  let nextQueue = { ...cur0.queue, extras: [...(cur0.queue.extras ?? [])] };
  let settingsDelta: Partial<Settings> = {};
  let mutatedSettings = false;
  let mutatedSeen = false;

  for (const p of feed.patches) {
    const id = typeof p?.id === 'string' ? p.id.trim() : '';
    if (!id || seen.has(id)) continue;

    if (typeof p.expiresAfter === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(p.expiresAfter) && today > p.expiresAfter) {
      seen.add(id);
      mutatedSeen = true;
      skippedExpired++;
      continue;
    }

    if (p.programTargets && typeof p.programTargets === 'object') {
      for (const [progId, rawVal] of Object.entries(p.programTargets)) {
        if (typeof rawVal !== 'number' || !Number.isFinite(rawVal) || rawVal <= 0) continue;
        const prog = programById(progId);
        nextTargets[progId] = prog ? clampProgramTarget(prog, rawVal) : Math.round(rawVal);
        mutatedSettings = true;
      }
    }

    const safeSet = pickAllowedSettings(p.settings);
    if (Object.keys(safeSet).length > 0) {
      settingsDelta = { ...settingsDelta, ...safeSet };
      mutatedSettings = true;
    }

    if (Array.isArray(p.extras) && p.extras.length > 0) {
      const existingIds = new Set(nextQueue.extras.map((x) => x.id));
      for (const ex of p.extras) {
        if (!ex?.id || !ex?.name || existingIds.has(ex.id)) continue;
        nextQueue.extras.push({ ...ex });
        existingIds.add(ex.id);
        mutatedSettings = true;
      }
    }

    if (p.planStepOverrides && typeof p.planStepOverrides === 'object') {
      for (const [k, ov] of Object.entries(p.planStepOverrides)) {
        const idx = Number(k);
        if (!Number.isInteger(idx) || idx < 0 || !ov || !Array.isArray(ov.slots) || ov.slots.length === 0) continue;
        nextOverrides[idx] = {
          ...(typeof ov.title === 'string' && ov.title.trim() ? { title: ov.title.trim() } : {}),
          slots: ov.slots.map((sl) => ({ ...sl })),
        };
        mutatedSettings = true;
      }
    }

    if (Array.isArray(p.routines)) {
      for (const rt of p.routines) {
        if (!rt?.id || !rt?.name || !Array.isArray(rt.items)) continue;
        await deps.upsertRoutine(rt);
      }
    }

    if (p.exerciseCams && typeof p.exerciseCams === 'object') {
      for (const [exId, rawCams] of Object.entries(p.exerciseCams)) {
        const cams = normalizeCamList(rawCams);
        if (exId && cams.length > 0) {
          await deps.updateExerciseCams(exId, cams);
        }
      }
    }

    seen.add(id);
    mutatedSeen = true;
    applied.push(p);
  }

  const now = deps.nowMs();
  if (mutatedSettings || mutatedSeen) {
    deps.updateSettings({
      ...settingsDelta,
      ...(mutatedSettings ? { programTargets: nextTargets, planStepOverrides: nextOverrides, queue: nextQueue } : {}),
      appliedCoachPatches: Array.from(seen),
      coachSyncLastMs: now,
    });
  } else {
    deps.updateSettings({ coachSyncLastMs: now });
  }

  const latestLabel = applied.length
    ? (applied[applied.length - 1].label?.trim() || `Applied ${applied.length} coach update${applied.length > 1 ? 's' : ''}`)
    : null;
  return { applied, skippedExpired, latestLabel };
}

/**
 * Fetches the remote `coach-patch.json` feed silently in the background (works over 5G & Wi-Fi,
 * completely offline-safe). Throttled to once per 5 min unless `force: true`.
 */
export function syncCoachPatches(opts: { force?: boolean; silent?: boolean } = {}): Promise<CoachSyncResult> {
  if (inflight) return inflight;
  const now = Date.now();
  if (!opts.force && lastAttemptMs > 0 && now - lastAttemptMs < THROTTLE_MS) {
    return Promise.resolve({ ok: true, throttled: true, appliedCount: 0, latestLabel: null });
  }
  lastAttemptMs = now;

  const run = async (): Promise<CoachSyncResult> => {
    const baseUrl = (getSettings().coachSyncUrl || DEFAULT_COACH_SYNC_URL).trim();
    if (!baseUrl || typeof fetch === 'undefined') {
      return { ok: false, offline: true, appliedCount: 0, latestLabel: null };
    }
    const sep = baseUrl.includes('?') ? '&' : '?';
    const url = `${baseUrl}${sep}t=${Math.floor(Date.now() / 60_000)}`;
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS) : null;
    try {
      const res = await fetch(url, { cache: 'no-store', ...(ctrl ? { signal: ctrl.signal } : {}) });
      if (!res.ok) {
        return { ok: false, appliedCount: 0, latestLabel: null, error: `HTTP ${res.status}` };
      }
      const feed = (await res.json()) as CoachPatchFeed;
      const out = await applyCoachPatches(feed);
      if (out.applied.length > 0 && out.latestLabel) {
        toast(out.latestLabel);
      }
      return { ok: true, appliedCount: out.applied.length, latestLabel: out.latestLabel };
    } catch (e) {
      return {
        ok: false,
        offline: true,
        appliedCount: 0,
        latestLabel: null,
        error: (e as Error)?.message || 'Offline',
      };
    } finally {
      if (timer) clearTimeout(timer);
    }
  };

  inflight = run().finally(() => { inflight = null; });
  return inflight;
}
