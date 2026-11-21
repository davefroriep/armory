import { buzz } from '../audio/audio';
import { Emitter } from '../emitter';
import {
  ensureNativeVoicePermission, getNativeVoiceStatus, isNative, onNativeVoiceResult,
  setNativeVoiceListening, speakNativeVoice, type NativeVoiceEvent,
} from '../native/service';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { active } from './active';
import {
  advanceCursorAfterLog, applyAddSet, applyAddSetToLastEx, applyDeleteEmptySets,
  jumpToExerciseCursor, resolveTargetSet, stepExerciseCursor, type CursorRef,
} from './cursor';
import { gymCam } from './gymCam';
import { canLog, editReps, editWeight, labels, toggleDone } from './logic';
import { fmtKg, fromDisplay, liftSettings } from './settings';
import { lifts } from './store';
import {
  defaultReadbackForIntent, describeVoiceIntent, hasCommandTailAfterWake, hasWakeWord,
  parseVoiceTranscript, pickSetConfirmTag, voiceIntentKey, type VoiceIntent,
} from './voiceParser';

export interface VoiceActionNotice {
  text: string;
  kind: 'ok' | 'info' | 'warn';
  atMs: number;
  speech?: string | null;
}

export interface AbMicSample {
  source: NativeVoiceEvent['source'];
  tsMs: number;
  transcript: string;
  matched: boolean;
  intentKey: string;
  intentLabel: string;
  hasWake: boolean;
  station?: string;
  executed?: boolean;
}

export interface AbSummary {
  totalTrials: number;
  bothMatch: number;
  roomOnly: number;
  phoneOnly: number;
  disagree: number;
  bothMissed: number;
  roomWins: number;
  phoneWins: number;
  roomCatchPct: number;
  phoneCatchPct: number;
  deltaPts: number;
  relativeGainPct: number | null;
  agreementPct: number | null;
  byStation: { station: string; trials: number; roomCatchPct: number; phoneCatchPct: number; deltaPts: number }[];
}

export interface AbShootoutRun {
  id: string;
  tsMs: number;
  label: string;
  phrasesCount: number;
  roomIntentPct: number;
  phoneIntentPct: number;
  deltaPts: number;
  roomWordPct: number;
  phoneWordPct: number;
  items?: unknown[];
}

type UndoEntry =
  | {
      type: 'set';
      sessionId: string;
      exUid: string;
      setId: string;
      w: number | null;
      r: number | null;
      wAuto?: boolean;
      rAuto?: boolean;
      rpe?: number;
      prevLastLoggedExUid: string | null;
    }
  | {
      type: 'exs';
      sessionId: string;
      exsJson: string;
      prevCursor: CursorRef | null;
      prevLastLoggedExUid: string | null;
      label: string;
    };

interface WebSpeechRec {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal?: boolean }> }) => void) | null;
  onerror: ((e: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
}

const ARM_WINDOW_MS = 6500;
const BASE_BIAS = [
  'armory', 'check', 'done', 'log', 'undo', 'next exercise', 'previous exercise',
  'add set', 'add set to last exercise', 'delete empty sets',
  'skip rest', 'add 30 seconds', 'start hold', 'bodyweight', 'pause workout', 'resume workout', 'finish workout', 'rpe',
  'camera', 'record set', 'record rack', 'record smith', 'rack camera', 'smith camera', 'clip that', 'save clip', 'clip rack', 'clip smith',
  'replay', 'replay slow', 'play set', 'close video', 'guest record', 'guest rack', 'guest smith',
  'buddy mode', 'buddy mode on', 'buddy mode off', 'solo mode',
];

class VoiceController {
  cursor: CursorRef | null = null;
  armedUntilMs = 0;
  listening = false;
  granted = false;
  roomOnline = false;
  roomListening = false;
  roomReady = false;
  roomDevice = '';
  roomError: string | null = null;
  abSummary: AbSummary | null = null;
  abRuns: AbShootoutRun[] = [];
  shootoutActive = false;
  lastSource: NativeVoiceEvent['source'] | null = null;
  lastHeard = '';
  lastError: string | null = null;
  lastAction: VoiceActionNotice | null = null;
  holdStarter: ((exUid: string, setId: string, seconds?: number) => boolean) | null = null;
  finishTrigger: (() => void) | null = null;
  camHandler: ((mode: 'arm' | 'clip_last' | 'guest', camera?: string) => Promise<string | null> | string | null) | null = null;
  replayHandler: ((req: { exQuery?: string; setNum?: number; slow?: boolean; close?: boolean }) => Promise<string | null> | string | null) | null = null;
  buddyHandler: ((on?: boolean) => string | null) | null = null;

  readonly changed = new Emitter<void>();
  readonly toasted = new Emitter<{ msg: string; kind?: 'pr' }>();

  private sessionIdBound = '';
  private lastLoggedExUid: string | null = null;
  private liftScreenMounted = false;
  private undoStack: UndoEntry[] = [];
  private inited = false;
  private lastFinalSig = '';
  private lastFinalAt = 0;
  private nativeDesired = false;
  private webRec: WebSpeechRec | null = null;
  private armTimer: ReturnType<typeof setTimeout> | null = null;
  private roomEs: EventSource | null = null;
  private roomRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private roomRetryMs = 2000;
  private lastRoomEventId = 0;
  private lastRoomCfgSig = '';
  private shootoutListener: ((sample: AbMicSample) => void) | null = null;
  private abExecAtMs = 0;
  private abExecSource: NativeVoiceEvent['source'] | null = null;
  private lastConfirmTag: string | null = null;

  isArmed(now = Date.now()): boolean {
    return now < this.armedUntilMs;
  }

  isAnyListening(): boolean {
    return this.listening || this.roomListening;
  }

  init(): void {
    if (this.inited) return;
    this.inited = true;
    onNativeVoiceResult((ev) => this.handleEvent(ev));
    lifts.changed.on(() => this.syncCursorToSession());
    settingsChanged.on(() => {
      this.syncSessionFromSettings();
    });
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && (this.liftScreenMounted || this.shootoutActive)) this.ensureRoomConnected();
      });
    }
    this.syncSessionFromSettings();
  }

  setLiftScreenMounted(mounted: boolean): void {
    this.liftScreenMounted = mounted;
    this.syncSessionFromSettings();
  }

  recordLoggedExercise(exUid: string): void {
    this.syncCursorToSession();
    this.lastLoggedExUid = exUid;
  }

  setCursor(ref: CursorRef | null): void {
    this.syncCursorToSession();
    this.cursor = ref;
    this.changed.emit();
  }

  /** Computes speech biasing phrases (wake word + active workout exercises + user custom aliases). */
  biasPhrases(): string[] {
    const out = new Set<string>(BASE_BIAS);
    const st = getSettings();
    const wake = st.voiceWakeWord.trim().toLowerCase();
    if (wake) out.add(wake);
    for (const list of Object.values(st.voiceCustomAliases ?? {})) {
      for (const raw of list ?? []) {
        const clean = raw.replace(/#/g, '').trim();
        if (clean) out.add(clean);
      }
    }
    const s = lifts.active;
    if (s) {
      for (const se of s.exs) {
        out.add(lifts.exOrStub(se.exId).name);
      }
    }
    return [...out].slice(0, 60);
  }

  refreshBiasPhrases(): void {
    if (this.listening && isNative()) {
      void setNativeVoiceListening(true, this.biasPhrases(), false);
    }
    if (this.roomOnline && this.liftScreenMounted && getSettings().voiceRoomEnabled) {
      void this.pushRoomContext(true);
    }
  }

  setRoomEnabled(on: boolean): void {
    updateSettings({ voiceRoomEnabled: on });
  }

  async setAbCompare(on: boolean): Promise<boolean> {
    if (on && isNative()) {
      const ok = await ensureNativeVoicePermission();
      if (!ok) {
        this.toasted.emit({ msg: 'Microphone permission needed for Dual-Mic A/B benchmark' });
        return false;
      }
    }
    updateSettings({ voiceAbCompare: on });
    if (on) void this.fetchAbSummary();
    return true;
  }

  async startShootout(cb: (sample: AbMicSample) => void): Promise<boolean> {
    if (isNative()) {
      const ok = await ensureNativeVoicePermission();
      if (!ok) {
        this.toasted.emit({ msg: 'Microphone permission denied' });
        return false;
      }
    }
    this.shootoutListener = cb;
    this.shootoutActive = true;
    this.syncSessionFromSettings();
    this.changed.emit();
    return true;
  }

  stopShootout(): void {
    if (!this.shootoutActive && !this.shootoutListener) return;
    this.shootoutActive = false;
    this.shootoutListener = null;
    this.syncSessionFromSettings();
    this.changed.emit();
  }

  async fetchAbSummary(): Promise<AbSummary | null> {
    const s = getSettings();
    if (!s.gymCamEnabled || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1800);
    try {
      const res = await fetch(`${this.roomBaseUrl()}/api/voice/ab`, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) return null;
      const d = (await res.json()) as { summary?: AbSummary; runs?: AbShootoutRun[] };
      if (d.summary) this.abSummary = d.summary;
      if (Array.isArray(d.runs)) this.abRuns = d.runs;
      this.changed.emit();
      return this.abSummary;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async saveShootoutRun(run: Omit<AbShootoutRun, 'id' | 'deltaPts'>): Promise<AbShootoutRun | null> {
    const res = await this.postRoomJson<{ ok?: boolean; run?: AbShootoutRun; runs?: AbShootoutRun[] }>('/api/voice/ab/run', run, 2500);
    if (Array.isArray(res?.runs)) this.abRuns = res!.runs;
    this.changed.emit();
    return res?.run ?? null;
  }

  async resetAbStats(clearRuns = false): Promise<AbSummary | null> {
    const res = await this.postRoomJson<{ ok?: boolean; summary?: AbSummary }>('/api/voice/ab/reset', { clearRuns }, 2000);
    if (res?.summary) this.abSummary = res.summary;
    if (clearRuns) this.abRuns = [];
    this.changed.emit();
    return this.abSummary;
  }

  private recordAbSample(sample: AbMicSample): void {
    void this.postRoomJson<{ ok?: boolean; summary?: AbSummary }>('/api/voice/ab/sample', sample, 1500).then((res) => {
      if (res?.summary) {
        this.abSummary = res.summary;
        this.changed.emit();
      }
    });
  }

  ensureRoomConnected(): void {
    const s = getSettings();
    const want = (this.shootoutActive || ((s.voiceRoomEnabled || s.voiceAbCompare) && this.liftScreenMounted))
      && s.gymCamEnabled
      && typeof EventSource !== 'undefined';
    if (!want) return;
    if (!this.roomEs || this.roomEs.readyState === 2) {
      if (this.roomRetryTimer) { clearTimeout(this.roomRetryTimer); this.roomRetryTimer = null; }
      this.openRoomEs();
    } else if (this.roomEs.readyState === 1) {
      void this.pushRoomContext(true);
    }
  }

  reconnectRoom(): void {
    this.closeRoomEs(false);
    this.roomRetryMs = 2000;
    this.syncRoomStream();
  }

  async refreshRoomStatus(): Promise<void> {
    const s = getSettings();
    if (!s.gymCamEnabled || typeof fetch === 'undefined' || typeof AbortController === 'undefined') {
      this.roomOnline = false;
      this.roomListening = false;
      this.roomReady = false;
      this.changed.emit();
      return;
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    try {
      const res = await fetch(`${this.roomBaseUrl()}/api/voice/status`, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = (await res.json()) as {
        ready?: boolean;
        listening?: boolean;
        device?: string;
        error?: string | null;
      };
      this.roomOnline = true;
      this.roomReady = !!d.ready;
      this.roomListening = !!d.listening;
      this.roomDevice = d.device || '';
      this.roomError = d.error ?? null;
    } catch {
      this.roomOnline = false;
      this.roomListening = false;
      this.roomReady = false;
    } finally {
      clearTimeout(timer);
      this.changed.emit();
    }
  }

  /** Receives an external voice event (used by Room Mic SSE and tests). */
  receiveExternalEvent(ev: NativeVoiceEvent): void {
    this.handleEvent(ev);
  }

  async setEnabled(on: boolean): Promise<boolean> {
    updateSettings({ voiceEnabled: on });
    if (on && !this.liftScreenMounted && isNative()) {
      const ok = await ensureNativeVoicePermission();
      if (!ok) {
        updateSettings({ voiceEnabled: false });
        this.toasted.emit({ msg: 'Microphone permission denied' });
        this.changed.emit();
        return false;
      }
      this.changed.emit();
      return true;
    }
    return this.applyListening(on && this.liftScreenMounted);
  }

  async restart(): Promise<boolean> {
    updateSettings({ voiceEnabled: true });
    if (!this.liftScreenMounted) return false;
    const ok = await this.applyListening(true, true);
    this.toasted.emit({ msg: 'Voice mic restarted' });
    return ok;
  }

  async refreshStatus(): Promise<void> {
    if (!isNative()) return;
    const st = await getNativeVoiceStatus();
    this.listening = st.listening;
    this.granted = st.granted;
    this.lastError = st.error ?? null;
    this.changed.emit();
  }

  /** Directly executes a phrase (used by `#/settings` test runner and tests). */
  runTextCommand(raw: string, requireWake = false): VoiceActionNotice | null {
    const s = getSettings();
    this.lastHeard = raw.trim();
    const intent = parseVoiceTranscript(raw, {
      wakeWord: s.voiceWakeWord,
      armed: this.isArmed(),
      requireWake,
      customAliases: s.voiceCustomAliases,
    });
    if (!intent) return null;
    return this.executeIntent(intent, false);
  }

  /** Tests any phrase in `#/settings` (works with or without an active workout) and speaks readback. */
  testPhrase(raw: string): { matched: boolean; text: string; speech: string | null } {
    const s = getSettings();
    const trimmed = raw.trim();
    this.lastHeard = trimmed;
    const intent = parseVoiceTranscript(trimmed, {
      wakeWord: s.voiceWakeWord,
      armed: this.isArmed(),
      requireWake: false,
      customAliases: s.voiceCustomAliases,
    });
    if (!intent) {
      this.notice(`Didn’t catch “${trimmed}”`, 'warn', false, "Didn't catch that");
      this.speak("Didn't catch that");
      return { matched: false, text: 'Did not match a lift voice command', speech: "Didn't catch that" };
    }
    if (lifts.active) {
      const res = this.executeIntent(intent, true);
      return { matched: true, text: res.text, speech: res.speech ?? null };
    }
    const text = `✓ Matched: ${describeVoiceIntent(intent)}`;
    const speech = defaultReadbackForIntent(intent);
    this.notice(text, 'info', false, speech);
    if (speech) this.speak(speech);
    return { matched: true, text, speech };
  }

  speak(text: string | null | undefined): void {
    const clean = text?.trim();
    if (!clean || !getSettings().voiceReadback) return;
    void speakNativeVoice(clean);
  }

  private syncCursorToSession(): void {
    const curId = lifts.active?.id ?? '';
    if (curId !== this.sessionIdBound) {
      this.sessionIdBound = curId;
      this.cursor = null;
      this.lastLoggedExUid = null;
      this.undoStack = [];
    }
  }

  private syncSessionFromSettings(): void {
    const s = getSettings();
    const want = this.shootoutActive || ((s.voiceEnabled || s.voiceAbCompare) && this.liftScreenMounted);
    if (want && (!this.listening || (isNative() && !this.nativeDesired)) && (isNative() || typeof window !== 'undefined')) {
      void this.applyListening(true);
    } else if (!want && (this.listening || this.webRec || (isNative() && this.nativeDesired))) {
      void this.applyListening(false);
    }
    this.syncRoomStream();
  }

  private roomBaseUrl(): string {
    return (getSettings().gymCamUrl || 'http://192.168.1.152:8766').trim().replace(/\/+$/, '');
  }

  private syncRoomStream(): void {
    const s = getSettings();
    const want = (this.shootoutActive || ((s.voiceRoomEnabled || s.voiceAbCompare) && this.liftScreenMounted))
      && s.gymCamEnabled
      && typeof EventSource !== 'undefined';
    const cfgSig = `${this.roomBaseUrl()}|${s.voiceWakeWord}|${s.voiceReadback}`;
    if (!want) {
      if (this.roomEs || this.roomRetryTimer || this.roomOnline || this.roomListening) {
        this.closeRoomEs(true);
      }
      this.lastRoomCfgSig = '';
      return;
    }
    if (this.lastRoomCfgSig && this.lastRoomCfgSig !== cfgSig) {
      this.closeRoomEs(false);
      this.roomRetryMs = 2000;
    }
    this.lastRoomCfgSig = cfgSig;
    if (!this.roomEs && !this.roomRetryTimer) {
      this.openRoomEs();
    } else if (this.roomEs?.readyState === 1) {
      void this.pushRoomContext(true);
    }
  }

  private openRoomEs(): void {
    const s = getSettings();
    const wake = encodeURIComponent((s.voiceWakeWord || 'armory').trim() || 'armory');
    const url = `${this.roomBaseUrl()}/api/voice/events?wake=${wake}${this.lastRoomEventId ? `&afterId=${this.lastRoomEventId}` : ''}`;
    try {
      const es = new EventSource(url);
      this.roomEs = es;
      es.onopen = () => {
        if (this.roomEs !== es) return;
        this.roomRetryMs = 2000;
        this.roomOnline = true;
        this.roomError = null;
        void this.pushRoomContext(true);
        this.changed.emit();
      };
      es.onmessage = (msg) => {
        if (this.roomEs !== es) return;
        try {
          const d = JSON.parse(msg.data) as {
            type?: string;
            id?: number;
            transcript?: string;
            candidates?: string[];
            ready?: boolean;
            listening?: boolean;
            device?: string;
            error?: string | null;
          };
          if (d.type === 'status') {
            this.roomOnline = true;
            this.roomReady = !!d.ready;
            this.roomListening = !!d.listening;
            this.roomDevice = d.device || '';
            this.roomError = d.error ?? null;
            this.changed.emit();
            return;
          }
          if (d.type === 'transcript' && d.transcript) {
            const evId = Number(d.id || 0);
            if (evId > 0) {
              if (evId <= this.lastRoomEventId) return;
              this.lastRoomEventId = evId;
            }
            this.handleEvent({
              transcript: d.transcript,
              candidates: Array.isArray(d.candidates) ? d.candidates : [d.transcript],
              isFinal: true,
              source: 'room',
              state: 'room',
            });
          }
        } catch {
          /* ignore malformed SSE frame */
        }
      };
      es.onerror = () => {
        if (this.roomEs !== es) return;
        try { es.close(); } catch { /* ignore */ }
        this.roomEs = null;
        this.roomOnline = false;
        this.roomListening = false;
        this.changed.emit();
        if (this.liftScreenMounted && getSettings().voiceRoomEnabled && getSettings().gymCamEnabled) {
          if (this.roomRetryTimer) clearTimeout(this.roomRetryTimer);
          const delay = this.roomRetryMs;
          this.roomRetryMs = Math.min(15_000, Math.round(this.roomRetryMs * 1.8));
          this.roomRetryTimer = setTimeout(() => {
            this.roomRetryTimer = null;
            this.syncRoomStream();
          }, delay);
        }
      };
    } catch {
      this.roomOnline = false;
      this.roomListening = false;
      this.changed.emit();
    }
  }

  private closeRoomEs(notifyInactive: boolean): void {
    if (this.roomRetryTimer) {
      clearTimeout(this.roomRetryTimer);
      this.roomRetryTimer = null;
    }
    if (this.roomEs) {
      const old = this.roomEs;
      this.roomEs = null;
      try { old.close(); } catch { /* ignore */ }
    }
    this.roomListening = false;
    this.roomOnline = false;
    if (notifyInactive) void this.pushRoomContext(false);
    this.changed.emit();
  }

  private async postRoomJson<T>(path: string, body: unknown, timeoutMs = 1500): Promise<T | null> {
    if (typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.roomBaseUrl()}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok) return null;
      return (await res.json()) as T;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  private async pushRoomContext(activeNow: boolean): Promise<void> {
    const s = getSettings();
    await this.postRoomJson('/api/voice/context', {
      active: activeNow,
      wakeWord: (s.voiceWakeWord || 'armory').trim() || 'armory',
      biasPhrases: activeNow ? this.biasPhrases() : [],
      readback: s.voiceReadback,
    });
  }

  private async ackRoom(payload: {
    armed: boolean;
    speech: string;
    text: string;
    kind: VoiceActionNotice['kind'];
  }): Promise<boolean> {
    const res = await this.postRoomJson<{ ok?: boolean; spoke?: boolean }>('/api/voice/ack', payload, 1500);
    return !!res?.spoke;
  }

  private async applyListening(on: boolean, forceRestart = false): Promise<boolean> {
    if (isNative()) {
      this.nativeDesired = on;
      const res = await setNativeVoiceListening(on, on ? this.biasPhrases() : [], forceRestart);
      this.listening = res.listening;
      this.granted = res.granted;
      this.lastError = res.error ?? null;
      if (!res.granted && on && res.error) {
        updateSettings({ voiceEnabled: false });
        this.toasted.emit({ msg: res.error });
      }
      this.changed.emit();
      return res.listening;
    }

    // Desktop browser fallback when running Vite dev preview outside Android APK
    this.stopWebRec();
    if (!on || typeof window === 'undefined') {
      this.listening = false;
      this.changed.emit();
      return false;
    }
    const SR = (window as unknown as { SpeechRecognition?: new () => WebSpeechRec; webkitSpeechRecognition?: new () => WebSpeechRec }).SpeechRecognition
      ?? (window as unknown as { webkitSpeechRecognition?: new () => WebSpeechRec }).webkitSpeechRecognition;
    if (!SR) {
      this.listening = false;
      this.lastError = 'Install Armory Fit APK on Pixel for on-device voice commands';
      this.changed.emit();
      return false;
    }
    try {
      const rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = 'en-US';
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const item = e.results[i];
          const txt = item?.[0]?.transcript?.trim() ?? '';
          if (txt) this.handleEvent({ transcript: txt, isFinal: !!item.isFinal, source: 'mic', state: 'listening' });
        }
      };
      rec.onerror = (e) => {
        if (e.error === 'not-allowed') {
          updateSettings({ voiceEnabled: false });
          this.listening = false;
          this.lastError = 'Microphone permission denied';
          this.changed.emit();
        }
      };
      rec.onend = () => {
        if (getSettings().voiceEnabled && this.webRec === rec) {
          try { rec.start(); } catch { /* ignore */ }
        }
      };
      rec.start();
      this.webRec = rec;
      this.listening = true;
      this.granted = true;
      this.lastError = null;
      this.changed.emit();
      return true;
    } catch (err) {
      this.listening = false;
      this.lastError = (err as Error).message;
      this.changed.emit();
      return false;
    }
  }

  private stopWebRec(): void {
    if (!this.webRec) return;
    const old = this.webRec;
    this.webRec = null;
    try { old.stop(); } catch { /* ignore */ }
  }

  private armNow(): void {
    this.armedUntilMs = Date.now() + ARM_WINDOW_MS;
    if (this.armTimer) clearTimeout(this.armTimer);
    this.armTimer = setTimeout(() => {
      this.armTimer = null;
      if (!this.isArmed()) this.changed.emit();
    }, ARM_WINDOW_MS + 40);
  }

  private handleEvent(ev: NativeVoiceEvent): void {
    if (ev.source === 'mic') {
      if (ev.state === 'paused_bg' || ev.state === 'stopped') {
        this.listening = false;
        this.armedUntilMs = 0;
        this.changed.emit();
        return;
      }
      if (ev.state === 'recovering') {
        this.listening = false;
        this.lastError = ev.error ?? 'Recovering mic…';
        this.changed.emit();
        return;
      }
      if (ev.state === 'error') {
        this.listening = false;
        this.lastError = ev.error ?? 'Voice recognition error';
        this.changed.emit();
        return;
      }
      if (ev.state === 'listening') {
        this.listening = true;
        this.lastError = null;
      }
    }

    const txt = (ev.transcript ?? '').trim();
    const pool = (ev.candidates?.length ? ev.candidates : [txt])
      .map((s) => s.trim())
      .filter(Boolean);
    if (!pool.length) {
      this.changed.emit();
      return;
    }

    const s = getSettings();
    const wakeWord = s.voiceWakeWord;
    if (!ev.isFinal && ev.source !== 'lan' && ev.source !== 'room') {
      this.lastHeard = pool[0];
      if (!this.shootoutActive && !this.isArmed() && pool.some((c) => hasWakeWord(c, wakeWord))) {
        this.armNow();
        buzz(20);
      }
      this.changed.emit();
      return;
    }

    const now = Date.now();
    const sig = pool[0].toLowerCase();
    const abActive = !this.shootoutActive && s.voiceAbCompare;
    const sameSource = ev.source === this.lastSource;
    if (this.shootoutActive || abActive) {
      if (sameSource && sig === this.lastFinalSig && now - this.lastFinalAt < 450) return;
    } else {
      const dedupMs = !sameSource ? 2200 : 450;
      if (sig === this.lastFinalSig && now - this.lastFinalAt < dedupMs) return;
    }
    this.lastFinalSig = sig;
    this.lastFinalAt = now;
    this.lastSource = ev.source;

    const customAliases = s.voiceCustomAliases;
    if (this.shootoutActive && this.shootoutListener) {
      let matchedCand: string | null = null;
      let intent: VoiceIntent | null = null;
      for (const cand of pool) {
        const hit = parseVoiceTranscript(cand, { wakeWord, armed: false, customAliases });
        if (hit) {
          matchedCand = cand;
          intent = hit;
          break;
        }
      }
      const heard = matchedCand ?? pool[0];
      this.lastHeard = heard;
      const sample: AbMicSample = {
        source: ev.source,
        tsMs: now,
        transcript: heard,
        matched: !!intent && intent.kind !== 'arm_wake',
        intentKey: voiceIntentKey(intent),
        intentLabel: intent ? describeVoiceIntent(intent) : 'Unmatched',
        hasWake: pool.some((c) => hasWakeWord(c, wakeWord)),
      };
      this.shootoutListener(sample);
      this.changed.emit();
      return;
    }

    const armed = this.isArmed();
    let matchedCand: string | null = null;
    let intent: VoiceIntent | null = null;
    for (const cand of pool) {
      const hit = parseVoiceTranscript(cand, { wakeWord, armed, customAliases });
      if (hit) {
        matchedCand = cand;
        intent = hit;
        break;
      }
    }
    const heard = matchedCand ?? pool[0];
    this.lastHeard = heard;

    if (abActive) {
      const matchedCmd = !!intent && intent.kind !== 'arm_wake';
      const hasWake = pool.some((c) => hasWakeWord(c, wakeWord));
      const hasTail = hasCommandTailAfterWake(pool[0], wakeWord, armed);
      const suppressed = this.abExecSource != null && ev.source !== this.abExecSource && (now - this.abExecAtMs) < 4500;
      const sess = lifts.active;
      const curPtr = sess ? resolveTargetSet(sess.exs, this.cursor) : null;
      const station = curPtr && sess ? lifts.exOrStub(sess.exs[curPtr.exIdx].exId).name : '';
      if (matchedCmd || hasTail || hasWake) {
        this.recordAbSample({
          source: ev.source,
          tsMs: now,
          transcript: heard,
          matched: matchedCmd,
          intentKey: voiceIntentKey(intent),
          intentLabel: intent ? describeVoiceIntent(intent) : (hasTail ? 'Unmatched' : 'Wake only'),
          hasWake,
          station,
          executed: !suppressed && matchedCmd,
        });
      }
      if (suppressed) {
        this.changed.emit();
        return;
      }
      if (matchedCmd) {
        this.abExecAtMs = now;
        this.abExecSource = ev.source;
      }
    }

    if (!intent) {
      if (hasCommandTailAfterWake(pool[0], wakeWord, armed)) {
        this.armedUntilMs = 0;
        const n = this.notice(`Didn’t catch “${pool[0]}”`, 'warn', true, "Didn't catch that");
        this.speakWithSource("Didn't catch that", ev.source, n);
      } else {
        this.changed.emit();
      }
      return;
    }
    this.executeIntent(intent, true, ev.source);
  }

  private speakWithSource(
    speech: string | null | undefined,
    source: NativeVoiceEvent['source'],
    notice: VoiceActionNotice,
  ): void {
    const cleanSpeech = getSettings().voiceReadback && speech ? speech.trim() : '';
    if (source === 'room') {
      void this.ackRoom({
        armed: this.isArmed(),
        speech: cleanSpeech,
        text: notice.text,
        kind: notice.kind,
      }).then((spoke) => {
        if (cleanSpeech && !spoke) this.speak(cleanSpeech);
      });
      return;
    }
    if (cleanSpeech) this.speak(cleanSpeech);
  }

  private notice(
    text: string,
    kind: VoiceActionNotice['kind'],
    toastIt = true,
    speech: string | null = null,
  ): VoiceActionNotice {
    const n: VoiceActionNotice = { text, kind, atMs: Date.now(), ...(speech ? { speech } : {}) };
    this.lastAction = n;
    this.changed.emit();
    if (toastIt) this.toasted.emit({ msg: text });
    return n;
  }

  private executeIntent(
    intent: VoiceIntent,
    speakIt = false,
    source: NativeVoiceEvent['source'] = 'mic',
  ): VoiceActionNotice {
    const n = this.runIntent(intent);
    if (source === 'room') {
      this.speakWithSource(speakIt ? (n.speech ?? null) : null, 'room', n);
    } else if (speakIt && n.speech) {
      this.speak(n.speech);
    }
    return n;
  }

  private runIntent(intent: VoiceIntent): VoiceActionNotice {
    this.syncCursorToSession();
    if (intent.kind === 'arm_wake') {
      this.armNow();
      buzz(20);
      return this.notice('Listening… say weight & reps or "check"', 'info', false, null);
    }

    const fallbackSpeech = defaultReadbackForIntent(intent);
    this.armedUntilMs = 0;
    const s = lifts.active;
    if (!s) {
      return this.notice('Start a Strength workout to use voice set commands', 'warn', true, 'Start a workout first');
    }

    if (intent.kind === 'rest_skip') {
      active.clearRest();
      buzz(20);
      return this.notice('Rest timer skipped', 'info', true, fallbackSpeech);
    }
    if (intent.kind === 'rest_add') {
      if (!active.rest) {
        return this.notice('No rest timer running', 'warn', true, 'No rest running');
      }
      active.adjustRest(intent.deltaSec);
      buzz(20);
      return this.notice(`${intent.deltaSec >= 0 ? '+' : ''}${intent.deltaSec}s rest`, 'info', true, fallbackSpeech);
    }
    if (intent.kind === 'pause_workout') {
      if (active.pause()) return this.notice('Workout paused', 'info', true, fallbackSpeech);
      return this.notice('Workout is not running', 'warn', true, 'Not running');
    }
    if (intent.kind === 'resume_workout') {
      if (active.resume()) return this.notice('Workout resumed', 'info', true, fallbackSpeech);
      return this.notice('Workout is not paused', 'warn', true, 'Not paused');
    }
    if (intent.kind === 'finish_workout') {
      if (this.finishTrigger) {
        buzz(25);
        this.finishTrigger();
        return this.notice('Finishing workout…', 'info', false, fallbackSpeech);
      }
      return this.notice('Open active Strength workout to finish', 'warn', true, fallbackSpeech);
    }
    if (intent.kind === 'cam_arm' || intent.kind === 'cam_clip_last' || intent.kind === 'cam_guest') {
      if (!this.camHandler) {
        return this.notice('Open active Strength workout to use camera commands', 'warn', true, 'Open a workout first');
      }
      const mode = intent.kind === 'cam_arm' ? 'arm' : intent.kind === 'cam_guest' ? 'guest' : 'clip_last';
      void Promise.resolve(this.camHandler(mode, intent.camera)).then((msg) => {
        if (msg) this.notice(msg, 'info');
      });
      buzz(25);
      return this.notice(
        mode === 'arm' ? 'Camera toggled…' : mode === 'guest' ? 'Guest camera toggled…' : 'Clipping last set…',
        'info',
        false,
        fallbackSpeech,
      );
    }
    if (intent.kind === 'wb_replay' || intent.kind === 'wb_close_replay') {
      if (!this.replayHandler) {
        return this.notice('Open active Strength workout to replay on TV', 'warn', true, 'Open a workout first');
      }
      const req = intent.kind === 'wb_close_replay'
        ? { close: true }
        : { exQuery: intent.exQuery, setNum: intent.setNum, slow: intent.slow };
      void Promise.resolve(this.replayHandler(req)).then((msg) => {
        if (msg) this.notice(msg, 'info');
      });
      buzz(20);
      return this.notice(
        intent.kind === 'wb_close_replay' ? 'Closing TV replay…' : 'Sending replay to TV…',
        'info',
        false,
        fallbackSpeech,
      );
    }
    if (intent.kind === 'buddy_mode') {
      if (this.buddyHandler) {
        const msg = this.buddyHandler(intent.on) || fallbackSpeech || 'Buddy mode updated';
        buzz(20);
        return this.notice(msg, 'info', true, msg);
      }
      const next = intent.on ?? !getSettings().gymCamBuddyMode;
      updateSettings({ gymCamBuddyMode: next });
      const act = active.session;
      if (act) {
        act.buddyMode = next;
        lifts.saveSession(act, true);
      }
      buzz(20);
      const speech = next ? 'Buddy mode on' : 'Buddy mode off';
      return this.notice(speech, 'info', true, speech);
    }

    const cur = resolveTargetSet(s.exs, this.cursor);
    const pushExsUndo = (label: string): void => {
      this.undoStack.push({
        type: 'exs',
        sessionId: s.id,
        exsJson: JSON.stringify(s.exs),
        prevCursor: this.cursor,
        prevLastLoggedExUid: this.lastLoggedExUid,
        label,
      });
      if (this.undoStack.length > 20) this.undoStack.shift();
    };
    const prevOf = (exId: string) => lifts.previous(exId, s.startMs, s.id);

    if (intent.kind === 'add_set') {
      const targetSe = cur ? s.exs[cur.exIdx] : (this.lastLoggedExUid ? s.exs.find((e) => e.uid === this.lastLoggedExUid) : undefined) ?? s.exs[s.exs.length - 1];
      if (!targetSe) return this.notice('Add an exercise first', 'warn', true, 'Add an exercise first');
      const name = lifts.exOrStub(targetSe.exId).name;
      pushExsUndo(name);
      const res = applyAddSet(s.exs, this.cursor, this.lastLoggedExUid, prevOf);
      this.cursor = res.cursor;
      lifts.saveSession(s, true);
      buzz(25);
      const setCount = targetSe.sets.filter((x) => x.kind !== 'w').length;
      return this.notice(`+ Set ${setCount} added · ${name}`, 'info', true, `Set ${setCount} added`);
    }

    if (intent.kind === 'add_set_prev_ex') {
      let prevSe = this.lastLoggedExUid ? s.exs.find((e) => e.uid === this.lastLoggedExUid) : undefined;
      if (!prevSe && cur && cur.exIdx > 0) prevSe = s.exs[cur.exIdx - 1];
      if (!prevSe) return this.notice('No previous exercise to add a set to', 'warn', true, 'No previous exercise');
      const name = lifts.exOrStub(prevSe.exId).name;
      pushExsUndo(name);
      const res = applyAddSetToLastEx(s.exs, this.cursor, this.lastLoggedExUid, prevOf);
      if (!res.changed) return this.notice('No previous exercise to add a set to', 'warn', true, 'No previous exercise');
      this.cursor = res.cursor;
      const labs = labels(s.exs);
      lifts.saveSession(s, true);
      buzz(25);
      const setCount = prevSe.sets.filter((x) => x.kind !== 'w').length;
      return this.notice(`+ Set ${setCount} on ${labs[res.exIdx] ?? ''} ${name}`, 'info', true, fallbackSpeech);
    }

    if (intent.kind === 'delete_empty_sets') {
      if (!cur) return this.notice('No empty sets on current exercise', 'warn', true, 'No empty sets');
      const seCur = s.exs[cur.exIdx];
      const nameCur = lifts.exOrStub(seCur.exId).name;
      const snapJson = JSON.stringify(s.exs);
      const snapCursor = this.cursor;
      const snapLast = this.lastLoggedExUid;
      const res = applyDeleteEmptySets(s.exs, this.cursor);
      if (!res.changed || !res.deletedCount) {
        return this.notice('No empty sets on current exercise', 'warn', true, 'No empty sets');
      }
      this.undoStack.push({
        type: 'exs',
        sessionId: s.id,
        exsJson: snapJson,
        prevCursor: snapCursor,
        prevLastLoggedExUid: snapLast,
        label: nameCur,
      });
      if (this.undoStack.length > 20) this.undoStack.shift();
      this.cursor = res.cursor;
      lifts.saveSession(s, true);
      buzz(25);
      const nextPtr = resolveTargetSet(s.exs, this.cursor);
      const nextLabel = nextPtr ? ` → ${nextPtr.label} ${lifts.exOrStub(s.exs[nextPtr.exIdx].exId).name}` : '';
      return this.notice(
        `Deleted ${res.deletedCount} empty set${res.deletedCount === 1 ? '' : 's'} · ${nameCur}${nextLabel}`,
        'info',
        true,
        `Deleted ${res.deletedCount} empty ${res.deletedCount === 1 ? 'set' : 'sets'}`,
      );
    }

    if (intent.kind === 'next_ex' || intent.kind === 'prev_ex') {
      const next = stepExerciseCursor(s.exs, cur, intent.kind === 'next_ex' ? 1 : -1);
      if (!next) return this.notice('All sets already logged', 'info', true, 'All sets logged');
      this.cursor = next;
      const ptr = resolveTargetSet(s.exs, this.cursor);
      const exTitle = ptr ? lifts.exOrStub(s.exs[ptr.exIdx].exId).name : '';
      buzz(20);
      return this.notice(
        ptr ? `Target → ${ptr.label} ${exTitle} (Set ${ptr.setNumber})` : 'Moved exercise',
        'info',
        true,
        exTitle ? `Up now, ${exTitle}` : fallbackSpeech,
      );
    }

    if (intent.kind === 'jump_ex') {
      const jumped = jumpToExerciseCursor(s.exs, intent.target, lifts.exOrStub);
      if (!jumped) return this.notice(`Could not find "${intent.target}" in workout`, 'warn', true, "Didn't find that exercise");
      this.cursor = jumped;
      const ptr = resolveTargetSet(s.exs, this.cursor);
      const exTitle = ptr ? lifts.exOrStub(s.exs[ptr.exIdx].exId).name : intent.target;
      buzz(20);
      return this.notice(
        ptr ? `Target → ${ptr.label} ${lifts.exOrStub(s.exs[ptr.exIdx].exId).name}` : 'Jumped exercise',
        'info',
        true,
        `Up now, ${exTitle}`,
      );
    }

    if (intent.kind === 'undo') {
      const last = this.undoStack.pop();
      if (!last || last.sessionId !== s.id) return this.notice('Nothing to undo', 'warn', true, 'Nothing to undo');
      if (last.type === 'exs') {
        s.exs = JSON.parse(last.exsJson);
        this.cursor = last.prevCursor;
        this.lastLoggedExUid = last.prevLastLoggedExUid;
        void active.resetIfNoneLogged(s);
        lifts.saveSession(s, true);
        buzz(25);
        return this.notice(`↶ Undid ${last.label}`, 'warn', true, `Undid ${last.label}`);
      }
      const se = s.exs.find((e) => e.uid === last.exUid);
      const set = se?.sets.find((x) => x.id === last.setId);
      if (!se || !set) return this.notice('Set no longer exists', 'warn', true, 'Set no longer exists');
      set.done = false;
      set.w = last.w;
      set.r = last.r;
      if (last.wAuto) set.wAuto = true; else delete set.wAuto;
      if (last.rAuto) set.rAuto = true; else delete set.rAuto;
      if (last.rpe != null) set.rpe = last.rpe; else delete set.rpe;
      this.cursor = { exUid: se.uid, setId: set.id };
      this.lastLoggedExUid = last.prevLastLoggedExUid;
      void active.resetIfNoneLogged(s);
      lifts.saveSession(s, true);
      buzz(25);
      return this.notice(`↶ Undid ${lifts.exOrStub(se.exId).name}`, 'warn', true, 'Undid last set');
    }

    if (!cur) return this.notice('All sets in this workout are complete!', 'ok', true, 'Workout complete');
    const se = s.exs[cur.exIdx];
    const set = se.sets[cur.setIdx];
    const ex = lifts.exOrStub(se.exId);
    const u = liftSettings().unit;

    if (intent.kind === 'start_hold') {
      if (ex.type !== 'time') {
        active.ensureStarted(s);
        active.startRest(intent.seconds ?? active.restSecFor(se), ex.name);
        return this.notice(`Rest timer started · ${ex.name}`, 'info', true, 'Rest started');
      }
      if (this.holdStarter?.(se.uid, set.id, intent.seconds)) {
        buzz(25);
        return this.notice(`▶ Hold countdown · ${intent.seconds ?? set.r ?? 45}s`, 'info', true, fallbackSpeech);
      }
      return this.notice('Open active workout view to run hold countdown', 'warn', true, 'Open active workout');
    }

    if (intent.kind === 'set_only') {
      if (intent.weight != null) editWeight(se, cur.setIdx, fromDisplay(intent.weight, u));
      if (intent.reps != null) editReps(se, cur.setIdx, intent.reps);
      this.cursor = { exUid: se.uid, setId: set.id };
      lifts.saveSession(s, true);
      return this.notice(`Updated ${cur.label} ${ex.name}`, 'info', true, fallbackSpeech);
    }

    // intent.kind === 'log_current'
    const undoSnap: UndoEntry = {
      type: 'set',
      sessionId: s.id,
      exUid: se.uid,
      setId: set.id,
      w: set.w,
      r: set.r,
      wAuto: set.wAuto,
      rAuto: set.rAuto,
      rpe: set.rpe,
      prevLastLoggedExUid: this.lastLoggedExUid,
    };
    if (intent.bodyweight) {
      editWeight(se, cur.setIdx, 0);
    } else if (intent.weight != null) {
      editWeight(se, cur.setIdx, fromDisplay(intent.weight, u));
    }
    if (ex.type === 'time') {
      const sec = intent.seconds ?? intent.reps;
      if (sec != null) editReps(se, cur.setIdx, sec);
    } else if (intent.reps != null) {
      editReps(se, cur.setIdx, intent.reps);
    }
    if (intent.rpe != null) set.rpe = intent.rpe;

    if (!canLog(set, ex)) {
      return this.notice(`Need weight & reps for ${cur.label} ${ex.name} (e.g. "Armory 135 for 10")`, 'warn', true, 'Need weight and reps');
    }
    const wasArmed = gymCam.isArmed(set.id);
    const logged = toggleDone(se, cur.setIdx, ex);
    if (!logged) {
      return this.notice(`Could not log ${ex.name}`, 'warn', true, 'Could not log set');
    }
    this.undoStack.push(undoSnap);
    if (this.undoStack.length > 20) this.undoStack.shift();

    this.lastLoggedExUid = se.uid;
    active.ensureStarted(s);
    active.afterSetLogged(s, cur.exIdx);
    this.cursor = advanceCursorAfterLog(s.exs, { exIdx: cur.exIdx, setIdx: cur.setIdx });
    lifts.saveSession(s, true);
    if (wasArmed) {
      void gymCam.stopAndSaveArmed(s, se, set);
    }
    buzz([35, 30, 50]);

    const valTxt = ex.type === 'time'
      ? `${set.r ?? 0}s${set.w ? ` @ ${fmtKg(set.w)} ${u}` : ''}`
      : ex.type === 'bodyweight' && !set.w
        ? `BW × ${set.r ?? 0}`
        : `${set.w != null ? fmtKg(set.w) : '0'} ${u} × ${set.r ?? 0}`;
    const confirmTag = pickSetConfirmTag(this.lastConfirmTag);
    this.lastConfirmTag = confirmTag;
    const hasSpokenNums = intent.weight != null || intent.reps != null || intent.seconds != null;
    const spokenCheck = ex.type === 'time'
      ? `${set.r ?? 0} seconds, ${confirmTag}`
      : ex.type === 'bodyweight' && !set.w
        ? `Bodyweight for ${set.r ?? 0}, ${confirmTag}`
        : set.w != null
          ? `${fmtKg(set.w)} for ${set.r ?? 0}, ${confirmTag}`
          : `For ${set.r ?? 0}, ${confirmTag}`;
    const spoken = hasSpokenNums ? defaultReadbackForIntent(intent, confirmTag) : spokenCheck;
    return this.notice(`✓ ${cur.label} ${ex.name} · ${valTxt}`, 'ok', true, spoken);
  }
}

export const voice = new VoiceController();
