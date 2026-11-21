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
      stopAndSaveArmed: vi.fn(),
    },
  };
});

import { voice } from '../src/lifts/voice';

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
    voice.armedUntilMs = 0;
    voice.lastAction = null;
    voice.lastHeard = '';
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
      expect(JSON.parse(String(init.body))).toMatchObject({
        armed: false,
        speech: '225 for 8, got it',
      });
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
      expect(mockState.speakCalls).toEqual(['195 for 10, got it']);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
