import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const realFetch = globalThis.fetch;

// api.ts reads the key from localStorage-backed settings; give node a minimal one before importing it.
const store = new Map<string, string>();
vi.stubGlobal('localStorage', {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
});

const { createManualActivity, patchActivity } = await import('../src/intervals/api');
const { updateSettings } = await import('../src/store/settings');

describe('Intervals.icu manual lift activity', () => {
  const calls: { url: string; init: RequestInit }[] = [];
  beforeEach(() => {
    calls.length = 0;
    updateSettings({ apiKey: 'k3y', athleteId: 'i758105' });
    globalThis.fetch = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ id: 'i999' }), { status: 200 });
    }) as unknown as typeof fetch;
  });
  afterEach(() => { globalThis.fetch = realFetch; });

  it('POSTs the documented ActivityManual fields to /activities/manual with basic auth', async () => {
    const id = await createManualActivity({
      startLocalIso: '2026-11-16T07:30:00', name: 'Upper 1', description: 'Armory Fit · Upper 1', seconds: 3120,
      type: 'WeightTraining', externalId: 'armory-abc', pairedEventId: 42, kgLifted: 9570,
    });
    expect(id).toBe('i999');
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('https://intervals.icu/api/v1/athlete/i758105/activities/manual');
    expect(calls[0].init.method).toBe('POST');
    const body = JSON.parse(String(calls[0].init.body));
    expect(body).toEqual({
      start_date_local: '2026-11-16T07:30:00', type: 'WeightTraining', name: 'Upper 1', description: 'Armory Fit · Upper 1',
      moving_time: 3120, elapsed_time: 3120, external_id: 'armory-abc', kg_lifted: 9570, paired_event_id: 42,
    });
    const auth = (calls[0].init.headers as Record<string, string>).Authorization;
    expect(auth).toBe('Basic ' + btoa('API_KEY:k3y'));
  });

  it('PUTs description / RPE / kg_lifted to the activity', async () => {
    updateSettings({ apiKey: 'k3y', athleteId: 'i758105' });
    await patchActivity('i999', { type: 'WeightTraining', description: 'x', icu_rpe: 8 });
    expect(calls[0].url).toBe('https://intervals.icu/api/v1/activity/i999');
    expect(calls[0].init.method).toBe('PUT');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ type: 'WeightTraining', description: 'x', icu_rpe: 8 });
  });

  it('fails clearly when Intervals.icu rejects the request', async () => {
    updateSettings({ apiKey: 'k3y', athleteId: 'i758105' });
    globalThis.fetch = vi.fn(async () => new Response('nope', { status: 401 })) as unknown as typeof fetch;
    await expect(createManualActivity({ startLocalIso: 'x', name: 'n', description: '', seconds: 60, type: 'WeightTraining', externalId: 'e', pairedEventId: null }))
      .rejects.toThrow(/HTTP 401/);
  });
});
