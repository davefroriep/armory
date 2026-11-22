import { getSettings, updateSettings } from '../store/settings';
import type { IntervalsActivityLite } from '../domain/plan';

export interface IntervalsEvent {
  id: number;
  external_id?: string | null;
  paired_activity_id?: string | null;
}

interface WellnessRowRaw {
  id?: string;
  weight?: number | null;
}

const BASE = 'https://intervals.icu/api/v1';

export class IntervalsError extends Error {
  constructor(message: string, readonly status = 0) {
    super(message);
  }
}

export const hasKey = (): boolean => !!getSettings().apiKey.trim();

function auth(): HeadersInit {
  return { Authorization: 'Basic ' + btoa('API_KEY:' + getSettings().apiKey.trim()) };
}

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  if (!hasKey()) throw new IntervalsError('No Intervals.icu API key set (Settings)');
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), 20_000) : null;
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      headers: { ...auth(), ...(init.headers ?? {}) },
      ...(ctrl && !init.signal ? { signal: ctrl.signal } : {}),
    });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw new IntervalsError('Request timed out');
    throw new IntervalsError('Network error (offline?)');
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!res.ok) {
    const retryAfter = res.status === 429 ? res.headers.get('Retry-After') : null;
    const txt = (await res.text().catch(() => '')).slice(0, 120);
    const raNote = retryAfter ? ` (retry after ${retryAfter}s)` : '';
    throw new IntervalsError(`Intervals.icu HTTP ${res.status}${raNote}${txt ? ': ' + txt : ''}`, res.status);
  }
  return res;
}

const aid = () => encodeURIComponent(getSettings().athleteId.trim());

export interface AthleteFtpInfo {
  name: string;
  eftp: number | null;
  setFtp: number | null;
  maxHr: number | null;
  sex: 'M' | 'F' | null;
  weightKg: number | null;
  birthDate: string | null;
}

interface SportSettingRaw {
  types?: string[];
  ftp?: number;
  indoor_ftp?: number;
  eftp?: number;
  max_hr?: number;
  mmp_model?: { ftp?: number };
}

export async function fetchAthleteProfile(): Promise<AthleteFtpInfo> {
  const j = await (await call(`/athlete/${aid()}`)).json();
  const list: SportSettingRaw[] = Array.isArray(j.sportSettings) ? j.sportSettings : [];
  const ss = list.find((x) => Array.isArray(x.types) && (x.types.includes('VirtualRide') || x.types.includes('Ride'))) ?? list[0];
  const rawEftp = ss?.mmp_model?.ftp ?? ss?.eftp ?? null;
  const rawSetFtp = ss?.indoor_ftp && ss.indoor_ftp > 0 ? ss.indoor_ftp : ss?.ftp ?? null;
  return {
    name: String(j.name ?? aid()),
    eftp: typeof rawEftp === 'number' && rawEftp > 0 ? Math.round(rawEftp) : null,
    setFtp: typeof rawSetFtp === 'number' && rawSetFtp > 0 ? Math.round(rawSetFtp) : null,
    maxHr: typeof ss?.max_hr === 'number' && ss.max_hr > 0 ? Math.round(ss.max_hr) : null,
    sex: j.sex === 'M' || j.sex === 'F' ? j.sex : null,
    weightKg: typeof j.icu_weight === 'number' && j.icu_weight > 0 ? j.icu_weight : typeof j.weight === 'number' && j.weight > 0 ? j.weight : null,
    birthDate: typeof j.icu_date_of_birth === 'string' && /^\d{4}-\d{2}-\d{2}/.test(j.icu_date_of_birth) ? j.icu_date_of_birth.slice(0, 10) : null,
  };
}

const ymdDaysAgo = (days: number): string => {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

export async function fetchLatestWellnessWeight(days = 90): Promise<{ weightKg: number; date: string } | null> {
  const oldest = ymdDaysAgo(days);
  const newest = ymdDaysAgo(0);
  const rows = (await (await call(`/athlete/${aid()}/wellness?oldest=${encodeURIComponent(oldest)}&newest=${encodeURIComponent(newest)}`)).json()) as WellnessRowRaw[];
  if (!Array.isArray(rows)) return null;
  const hit = [...rows]
    .filter((r) => typeof r?.weight === 'number' && (r.weight ?? 0) > 0 && typeof r.id === 'string')
    .sort((a, b) => String(b.id).localeCompare(String(a.id)))[0];
  return hit ? { weightKg: Number(hit.weight), date: String(hit.id) } : null;
}

export async function syncAthleteFtp(forceUseEftp = false): Promise<AthleteFtpInfo> {
  const [info, latestWt] = await Promise.all([
    fetchAthleteProfile(),
    fetchLatestWellnessWeight().catch(() => null),
  ]);
  const weightKg = latestWt?.weightKg ?? info.weightKg;
  const patch: Partial<ReturnType<typeof getSettings>> = {
    intervalsEftp: info.eftp,
    intervalsSetFtp: info.setFtp,
    ...(info.sex ? { sex: info.sex } : {}),
    ...(weightKg ? { weightKg } : {}),
    ...(latestWt?.date ? { weightDate: latestWt.date } : {}),
    ...(info.birthDate ? { birthDate: info.birthDate } : {}),
  };
  if ((forceUseEftp || getSettings().ftpAutoSync) && info.eftp) {
    patch.ftp = info.eftp;
  }
  updateSettings(patch);
  return { ...info, weightKg };
}

export async function testConnection(): Promise<string> {
  return (await syncAthleteFtp(false)).name;
}

export interface UploadArgs {
  tcx: string;
  name: string;
  description: string;
  externalId: string;
  pairedEventId: number | null;
}

/** Uploads a TCX and returns the new activity id. */
export async function uploadActivity(a: UploadArgs): Promise<string> {
  const form = new FormData();
  form.append('file', new Blob([a.tcx], { type: 'application/vnd.garmin.tcx+xml' }), `${a.externalId}.tcx`);
  const q = new URLSearchParams({ name: a.name, description: a.description, external_id: a.externalId });
  if (a.pairedEventId != null) q.set('paired_event_id', String(a.pairedEventId));
  const res = await call(`/athlete/${aid()}/activities?${q}`, { method: 'POST', body: form });
  const j = await res.json();
  const id = j?.id ?? j?.activities?.[0]?.id ?? (Array.isArray(j) ? j[0]?.id : undefined);
  if (!id) throw new IntervalsError('Upload returned no activity id');
  return String(id);
}

/** Patches RPE + sport type (VirtualRide, matching the existing Bowflex/AirBike history). */
export async function patchActivity(id: string, fields: { icu_rpe?: number; type?: string; description?: string; name?: string; kg_lifted?: number }): Promise<void> {
  await call(`/activity/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

export interface ManualActivityArgs {
  startLocalIso: string;
  name: string;
  description: string;
  seconds: number;
  type: string;
  externalId: string;
  pairedEventId: number | null;
  kgLifted?: number;
}

/** Creates an activity with no file (a lift session logged without a heart-rate strap). Returns the new id. */
export async function createManualActivity(a: ManualActivityArgs): Promise<string> {
  const res = await call(`/athlete/${aid()}/activities/manual`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_date_local: a.startLocalIso,
      type: a.type,
      name: a.name,
      description: a.description,
      moving_time: a.seconds,
      elapsed_time: a.seconds,
      external_id: a.externalId,
      ...(a.kgLifted ? { kg_lifted: a.kgLifted } : {}),
      ...(a.pairedEventId != null ? { paired_event_id: a.pairedEventId } : {}),
    }),
  });
  const j = await res.json();
  const id = j?.id ?? j?.activities?.[0]?.id ?? (Array.isArray(j) ? j[0]?.id : undefined);
  if (!id) throw new IntervalsError('Manual activity returned no id');
  return String(id);
}

export async function fetchEvents(oldest: string, newest: string): Promise<IntervalsEvent[]> {
  const res = await call(`/athlete/${aid()}/events?oldest=${encodeURIComponent(oldest)}&newest=${encodeURIComponent(newest)}`);
  return res.json();
}

export async function fetchActivities(oldest: string, newest: string): Promise<IntervalsActivityLite[]> {
  const res = await call(`/athlete/${aid()}/activities?oldest=${encodeURIComponent(oldest)}&newest=${encodeURIComponent(newest)}`);
  return res.json();
}

export async function deleteEvent(id: number): Promise<void> {
  await call(`/athlete/${aid()}/events/${id}`, { method: 'DELETE' });
}
