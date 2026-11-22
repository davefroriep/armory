import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Exercise, LiftSession } from '../src/lifts/types';
import { KG_PER_LB } from '../src/lifts/settings';

const mockState = vi.hoisted(() => {
  const speakCalls: string[] = [];
  const exBench: Exercise = {
    id: 'barbell-bench-press',
    name: 'Barbell Bench Press',
    equip: 'bb',
    type: 'weight',
    primary: ['ch'],
    secondary: ['tr'],
  };
  let activeSess: LiftSession | undefined;
  return {
    speakCalls,
    exBench,
    get activeSess() { return activeSess; },
    set activeSess(v: LiftSession | undefined) { activeSess = v; },
    saveCalls: 0,
  };
});

vi.mock('../src/audio/audio', () => ({ buzz: vi.fn(), audio: { unlock: vi.fn(), chime: vi.fn() } }));

vi.mock('../src/native/service', () => ({
  isNative: () => false,
  ensureNativeVoicePermission: vi.fn(async () => true),
  getNativeVoiceStatus: vi.fn(async () => ({ listening: false, granted: false, available: false })),
  onNativeVoiceResult: vi.fn(() => () => {}),
  setNativeVoiceListening: vi.fn(async () => ({ listening: false, granted: false, available: false })),
  speakNativeVoice: vi.fn(async (text: string) => { mockState.speakCalls.push(text); }),
}));

vi.mock('../src/lifts/store', async () => {
  const { Emitter } = await import('../src/emitter');
  return {
    lifts: {
      changed: new Emitter<void>(),
      get active() { return mockState.activeSess; },
      exOrStub: () => mockState.exBench,
      previous: () => null,
      saveSession: () => { mockState.saveCalls += 1; },
    },
  };
});

vi.mock('../src/lifts/active', async () => {
  const { Emitter } = await import('../src/emitter');
  return {
    active: {
      tick: new Emitter<void>(),
      restChanged: new Emitter<void>(),
      rest: null,
      ensureStarted: vi.fn(),
      afterSetLogged: vi.fn(),
      clearRest: vi.fn(),
      adjustRest: vi.fn(),
      pause: vi.fn(() => false),
      resume: vi.fn(() => false),
      resetIfNoneLogged: vi.fn(),
      restSecFor: () => 90,
      startRest: vi.fn(),
    },
  };
});

vi.mock('../src/lifts/gymCam', async () => {
  const { Emitter } = await import('../src/emitter');
  return {
    gymCam: {
      changed: new Emitter<void>(),
      online: false,
      isArmed: () => false,
      stopAndSaveArmed: vi.fn(async () => []),
    },
  };
});

import { getSettings, updateSettings } from '../src/store/settings';
import { voice, type AbMicSample } from '../src/lifts/voice';
import { SET_CONFIRM_TAGS } from '../src/lifts/voiceParser';

function makeBenchSession(id: string): LiftSession {
  return {
    id,
    name: 'Upper 1',
    routineId: null,
    eventId: null,
    startMs: 1_000_000,
    endMs: null,
    status: 'active',
    started: true,
    exs: [{
      uid: 'u1',
      exId: 'barbell-bench-press',
      sets: [
        { id: 's1', w: 185 * KG_PER_LB, r: 8, done: false, kind: 'n', wAuto: true, rAuto: true },
        { id: 's2', w: 185 * KG_PER_LB, r: 8, done: false, kind: 'n', wAuto: true, rAuto: true },
      ],
    }],
    rpe: null,
    bwKg: 88.5,
  };
}

describe('VoiceController room source relay', () => {
  beforeEach(() => {
    mockState.speakCalls.length = 0;
    mockState.saveCalls = 0;
    voice.listening = false;
    voice.roomOnline = false;
    voice.armedUntilMs = 0;
    voice.lastAction = null;
    voice.lastHeard = '';
    (voice as unknown as { lastSpokenText: string; lastSpokenAt: number }).lastSpokenText = '';
    (voice as unknown as { lastSpokenText: string; lastSpokenAt: number }).lastSpokenAt = 0;
    (voice as unknown as { lastCommitModeLocalAt: number }).lastCommitModeLocalAt = 0;
    updateSettings({ voiceSetCommitMode: 'voice' });
  });

  it('logs set from room source without turning on phone listening, deduplicates, and skips Pixel TTS when OptiPlex ack speaks', async () => {
    const sess = makeBenchSession('sess_room_1');
    mockState.activeSess = sess;
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, spoke: true }),
    }));
    vi.stubGlobal('fetch', fetchMock);
    try {
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8',
        isFinal: true,
        source: 'room',
        state: 'room',
      });
      await new Promise((r) => setTimeout(r, 15));

      expect(voice.listening).toBe(false);
      expect(sess.exs[0].sets[0].done).toBe(true);
      expect(Math.round((sess.exs[0].sets[0].w ?? 0) / KG_PER_LB)).toBe(225);
      expect(sess.exs[0].sets[0].r).toBe(8);
      expect(mockState.saveCalls).toBe(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toContain('/api/voice/ack');
      const ackBody = JSON.parse(String(init.body)) as { armed?: boolean; speech?: string };
      expect(ackBody.armed).toBe(false);
      expect(ackBody.speech).toMatch(new RegExp(`^225 for 8, (${SET_CONFIRM_TAGS.join('|')})$`));
      expect(mockState.speakCalls).toEqual([]);

      // Immediate duplicate from room source is ignored
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8',
        isFinal: true,
        source: 'room',
        state: 'room',
      });
      expect(sess.exs[0].sets[1].done).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('falls back to Pixel TTS readback if OptiPlex ack returns spoke=false or fails', async () => {
    const sess = makeBenchSession('sess_room_2');
    mockState.activeSess = sess;
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, spoke: false }),
    }));
    vi.stubGlobal('fetch', fetchMock);
    try {
      voice.receiveExternalEvent({
        transcript: 'Armory 195 for 10',
        isFinal: true,
        source: 'room',
        state: 'room',
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(sess.exs[0].sets[0].done).toBe(true);
      expect(mockState.speakCalls).toHaveLength(1);
      const re1 = new RegExp(`^195 for 10, (${SET_CONFIRM_TAGS.join('|')})$`);
      const m1 = mockState.speakCalls[0].match(re1);
      expect(m1).not.toBeNull();

      voice.receiveExternalEvent({
        transcript: 'Armory 205 for 8',
        isFinal: true,
        source: 'room',
        state: 'room',
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(sess.exs[0].sets[1].done).toBe(true);
      expect(mockState.speakCalls).toHaveLength(2);
      const re2 = new RegExp(`^205 for 8, (${SET_CONFIRM_TAGS.join('|')})$`);
      const m2 = mockState.speakCalls[1].match(re2);
      expect(m2).not.toBeNull();
      expect(m2![1]).not.toBe(m1![1]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('logs set once during passive Dual-Mic A/B Benchmark while posting both samples and isolates Shootout capture', async () => {
    const sess = makeBenchSession('sess_ab_1');
    mockState.activeSess = sess;
    const abPosts: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/api/voice/ab/sample') && init?.body) {
        abPosts.push(JSON.parse(String(init.body)));
        return { ok: true, json: async () => ({ ok: true }) };
      }
      return { ok: true, json: async () => ({ ok: true, spoke: true }) };
    });
    vi.stubGlobal('fetch', fetchMock);
    updateSettings({ voiceAbCompare: true });
    try {
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8',
        isFinal: true,
        source: 'mic',
        state: 'listening',
      });
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8',
        isFinal: true,
        source: 'room',
        state: 'room',
      });
      await new Promise((r) => setTimeout(r, 20));

      // Only Set 1 is logged; Set 2 was NOT double-logged by the second mic!
      expect(sess.exs[0].sets[0].done).toBe(true);
      expect(sess.exs[0].sets[1].done).toBe(false);
      expect(abPosts).toHaveLength(2);
      expect(abPosts[0]).toMatchObject({ source: 'mic', matched: true, intentKey: 'log:225:8:_', executed: true });
      expect(abPosts[1]).toMatchObject({ source: 'room', matched: true, intentKey: 'log:225:8:_', executed: false });

      // Shootout mode captures both mics without modifying workout sets
      const captured: AbMicSample[] = [];
      await voice.startShootout((smp) => captured.push(smp));
      voice.receiveExternalEvent({ transcript: 'Armory skip rest', isFinal: true, source: 'mic', state: 'listening' });
      voice.receiveExternalEvent({ transcript: 'Armory skip rest', isFinal: true, source: 'room', state: 'room' });
      expect(captured).toHaveLength(2);
      expect(captured.map((c) => c.source)).toEqual(['mic', 'room']);
      expect(sess.exs[0].sets[1].done).toBe(false);
    } finally {
      voice.stopShootout();
      updateSettings({ voiceAbCompare: false });
      vi.unstubAllGlobals();
    }
  });

  it('commits stable phone mic partial transcripts after 650ms under background noise and dedups late isFinal', () => {
    const sess = makeBenchSession('sess_partial_commit');
    mockState.activeSess = sess;
    vi.useFakeTimers();
    try {
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8',
        isFinal: false,
        source: 'mic',
        state: 'listening',
      });
      expect(sess.exs[0].sets[0].done).toBe(false);

      vi.advanceTimersByTime(700);
      expect(sess.exs[0].sets[0].done).toBe(true);
      expect(Math.round((sess.exs[0].sets[0].w ?? 0) / KG_PER_LB)).toBe(225);
      expect(sess.exs[0].sets[0].r).toBe(8);
      expect(mockState.saveCalls).toBe(1);

      // Late final arrival for the same command must not double-log Set 2
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 8 please',
        isFinal: true,
        source: 'mic',
        state: 'listening',
      });
      expect(sess.exs[0].sets[1].done).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('routes phone-mic readback to OptiPlex Andrew speaker when room stream is online and deduplicates identical speech within 2.5s', async () => {
    const sess = makeBenchSession('sess_unified_voice');
    mockState.activeSess = sess;
    voice.roomOnline = true;
    updateSettings({ voiceRoomEnabled: true, gymCamEnabled: true });
    const ackBodies: { speech?: string; setCommitMode?: string }[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/api/voice/ack') && init?.body) {
        ackBodies.push(JSON.parse(String(init.body)) as { speech?: string; setCommitMode?: string });
        return { ok: true, json: async () => ({ ok: true, spoke: true }) };
      }
      return { ok: true, json: async () => ({ ok: true }) };
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 5',
        isFinal: true,
        source: 'mic',
        state: 'listening',
      });
      await new Promise((r) => setTimeout(r, 15));

      expect(sess.exs[0].sets[0].done).toBe(true);
      expect(ackBodies).toHaveLength(1);
      expect(ackBodies[0].speech).toMatch(new RegExp(`^225 for 5, (${SET_CONFIRM_TAGS.join('|')})$`));
      expect(ackBodies[0].setCommitMode).toBe('voice');
      expect(mockState.speakCalls).toEqual([]);

      // Repeating identical confirmation text within 2.5s via direct mic fallback does not double-speak
      voice.roomOnline = false;
      (voice as unknown as { speakWithSource: (s: string | null, src: 'mic' | 'room', n: { text: string; kind: 'ok'; atMs: number }) => void })
        .speakWithSource(ackBodies[0].speech!, 'mic', { text: 'dup', kind: 'ok', atMs: Date.now() });
      expect(mockState.speakCalls).toEqual([]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('syncs setCommitMode to /api/voice/config, forwards setCommitMode in /api/voice/ack, and skips Pixel TTS on logged sets when chime mode is active offline', async () => {
    const sess = makeBenchSession('sess_commit_mode');
    mockState.activeSess = sess;
    updateSettings({ voiceRoomEnabled: true, gymCamEnabled: true, voiceReadback: true });
    const configBodies: { setCommitMode?: string }[] = [];
    const ackBodies: { speech?: string; text?: string; kind?: string; setCommitMode?: string }[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/api/voice/config') && init?.body) {
        configBodies.push(JSON.parse(String(init.body)) as { setCommitMode?: string });
        return { ok: true, json: async () => ({ ok: true }) };
      }
      if (String(url).includes('/api/voice/ack') && init?.body) {
        ackBodies.push(JSON.parse(String(init.body)) as { speech?: string; text?: string; kind?: string; setCommitMode?: string });
        return { ok: true, json: async () => ({ ok: true, spoke: false }) };
      }
      return { ok: true, json: async () => ({ ok: true }) };
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      voice.setCommitMode('chime');
      await new Promise((r) => setTimeout(r, 15));
      expect(getSettings().voiceSetCommitMode).toBe('chime');
      expect(configBodies).toEqual([{ setCommitMode: 'chime' }]);

      // Online room set log forwards setCommitMode:'chime' and does NOT fall back to Pixel TTS even if spoke=false
      voice.roomOnline = true;
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 6',
        isFinal: true,
        source: 'mic',
        state: 'listening',
      });
      await new Promise((r) => setTimeout(r, 15));
      expect(ackBodies).toHaveLength(1);
      expect(ackBodies[0].setCommitMode).toBe('chime');
      expect(mockState.speakCalls).toEqual([]);

      // Offline logged set in chime mode skips Pixel TTS; switching back to voice enables spoken readback
      voice.roomOnline = false;
      (voice as unknown as { lastSpokenText: string; lastSpokenAt: number }).lastSpokenAt = 0;
      voice.receiveExternalEvent({
        transcript: 'Armory 225 for 7',
        isFinal: true,
        source: 'mic',
        state: 'listening',
      });
      await new Promise((r) => setTimeout(r, 15));
      expect(sess.exs[0].sets[1].done).toBe(true);
      expect(mockState.speakCalls).toEqual([]);

      voice.setCommitMode('voice');
      expect(getSettings().voiceSetCommitMode).toBe('voice');
      (voice as unknown as { speakWithSource: (s: string | null, src: 'mic' | 'room', n: { text: string; kind: 'ok'; atMs: number }) => void })
        .speakWithSource('225 for 9, check', 'mic', { text: '✓ 2 Barbell Bench Press · 225 lb × 9', kind: 'ok', atMs: Date.now() });
      expect(mockState.speakCalls).toEqual(['225 for 9, check']);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('goes back to previous edited set on "Armory, back" / "Armory, undo" and resets it to blanks while restoring forward suggestions', () => {
    const sess = makeBenchSession('sess_back_undo');
    mockState.activeSess = sess;
    voice.cursor = null;

    // 1. Log Set 1 with RPE -> forward-fills Set 2 and advances cursor to Set 2
    voice.receiveExternalEvent({
      transcript: 'Armory 225 for 8 rpe 8.5',
      isFinal: true,
      source: 'mic',
      state: 'listening',
    });
    expect(sess.exs[0].sets[0].done).toBe(true);
    expect(Math.round((sess.exs[0].sets[0].w ?? 0) / KG_PER_LB)).toBe(225);
    expect(sess.exs[0].sets[0].r).toBe(8);
    expect(sess.exs[0].sets[0].rpe).toBe(8.5);
    expect(Math.round((sess.exs[0].sets[1].w ?? 0) / KG_PER_LB)).toBe(225);
    expect(voice.cursor).toEqual({ exUid: 'u1', setId: 's2' });

    // 2. "Armory, back" returns cursor to s1, blanks s1, restores s2 auto suggestion, and clears rest timer
    voice.receiveExternalEvent({
      transcript: 'Armory, back',
      isFinal: true,
      source: 'mic',
      state: 'listening',
    });
    expect(voice.cursor).toEqual({ exUid: 'u1', setId: 's1' });
    expect(sess.exs[0].sets[0]).toMatchObject({ id: 's1', w: null, r: null, done: false });
    expect(sess.exs[0].sets[0].wAuto).toBeUndefined();
    expect(sess.exs[0].sets[0].rAuto).toBeUndefined();
    expect(sess.exs[0].sets[0].rpe).toBeUndefined();
    expect(sess.exs[0].sets[0].at).toBeUndefined();
    expect(Math.round((sess.exs[0].sets[1].w ?? 0) / KG_PER_LB)).toBe(185);
    expect(sess.exs[0].sets[1].wAuto).toBe(true);
    expect(sess.exs[0].sets[1].r).toBe(8);
    expect(sess.exs[0].sets[1].rAuto).toBe(true);

    // 3. Re-speaking the corrected numbers logs directly onto s1
    voice.receiveExternalEvent({
      transcript: 'Armory 205 for 10',
      isFinal: true,
      source: 'mic',
      state: 'listening',
    });
    expect(sess.exs[0].sets[0].done).toBe(true);
    expect(Math.round((sess.exs[0].sets[0].w ?? 0) / KG_PER_LB)).toBe(205);
    expect(sess.exs[0].sets[0].r).toBe(10);

    // 4. Manual touch edit tracked via recordEditedSet + "Armory, undo" blanks s2 and moves cursor to s2
    voice.recordEditedSet(sess.id, 'u1', 's2', sess.exs[0].sets);
    sess.exs[0].sets[1].w = 215 * KG_PER_LB;
    sess.exs[0].sets[1].r = 9;
    sess.exs[0].sets[1].done = true;
    sess.exs[0].sets[1].at = Date.now();
    voice.receiveExternalEvent({
      transcript: 'Armory, undo',
      isFinal: true,
      source: 'mic',
      state: 'listening',
    });
    expect(voice.cursor).toEqual({ exUid: 'u1', setId: 's2' });
    expect(sess.exs[0].sets[1]).toMatchObject({ id: 's2', w: null, r: null, done: false });

    // 5. Fallback when undoStack is empty still clears the latest completed set (s1) to blanks
    voice.receiveExternalEvent({
      transcript: 'Armory go back',
      isFinal: true,
      source: 'mic',
      state: 'listening',
    });
    expect(voice.cursor).toEqual({ exUid: 'u1', setId: 's1' });
    expect(sess.exs[0].sets[0]).toMatchObject({ id: 's1', w: null, r: null, done: false });
  });
});
