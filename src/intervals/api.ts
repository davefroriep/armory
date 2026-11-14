import { getSettings } from '../store/settings';

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
  let res: Response;
  try {
    res = await fetch(BASE + path, { ...init, headers: { ...auth(), ...(init.headers ?? {}) } });
  } catch {
    throw new IntervalsError('Network error (offline?)');
  }
  if (!res.ok) {
    const txt = (await res.text().catch(() => '')).slice(0, 120);
    throw new IntervalsError(`Intervals.icu HTTP ${res.status}${txt ? ': ' + txt : ''}`, res.status);
  }
  return res;
}

const aid = () => encodeURIComponent(getSettings().athleteId.trim());

export async function testConnection(): Promise<string> {
  const j = await (await call(`/athlete/${aid()}`)).json();
  return j.name ?? aid();
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
export async function patchActivity(id: string, fields: { icu_rpe?: number; type?: string }): Promise<void> {
  await call(`/activity/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}
