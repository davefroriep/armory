import { buzz } from '../audio/audio';
import { Emitter } from '../emitter';
import {
  getNativeVoiceStatus, isNative, onNativeVoiceResult, setNativeVoiceListening,
  type NativeVoiceEvent,
} from '../native/service';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { active } from './active';
import {
  advanceCursorAfterLog, jumpToExerciseCursor, resolveTargetSet, stepExerciseCursor,
  type CursorRef,
} from './cursor';
import { canLog, editReps, editWeight, toggleDone } from './logic';
import { fmtKg, fromDisplay, liftSettings } from './settings';
import { lifts } from './store';
import { parseVoiceTranscript, type VoiceIntent } from './voiceParser';

export interface VoiceActionNotice {
  text: string;
  kind: 'ok' | 'info' | 'warn';
  atMs: number;
}

interface UndoEntry {
  sessionId: string;
  exUid: string;
  setId: string;
  w: number | null;
  r: number | null;
  wAuto?: boolean;
  rAuto?: boolean;
  rpe?: number;
}

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
  'skip rest', 'add 30 seconds', 'start hold', 'bodyweight', 'pause workout', 'resume workout', 'rpe',
];

class VoiceController {
  cursor: CursorRef | null = null;
  armedUntilMs = 0;
  listening = false;
  granted = false;
  lastHeard = '';
  lastError: string | null = null;
  lastAction: VoiceActionNotice | null = null;
  holdStarter: ((exUid: string, setId: string, seconds?: number) => boolean) | null = null;

  readonly changed = new Emitter<void>();
  readonly toasted = new Emitter<{ msg: string; kind?: 'pr' }>();

  private sessionIdBound = '';
  private undoStack: UndoEntry[] = [];
  private inited = false;
  private lastFinalSig = '';
  private lastFinalAt = 0;
  private webRec: WebSpeechRec | null = null;
  private armTimer: ReturnType<typeof setTimeout> | null = null;

  isArmed(now = Date.now()): boolean {
    return now < this.armedUntilMs;
  }

  init(): void {
    if (this.inited) return;
    this.inited = true;
    onNativeVoiceResult((ev) => this.handleEvent(ev));
    lifts.changed.on(() => this.syncCursorToSession());
    settingsChanged.on(() => {
      this.syncSessionFromSettings();
    });
    this.syncSessionFromSettings();
  }

  setCursor(ref: CursorRef | null): void {
    this.syncSessionFromSettings();
    this.cursor = ref;
    this.changed.emit();
  }

  /** Computes speech biasing phrases (wake word + active workout exercises & labels). */
  biasPhrases(): string[] {
    const out = new Set<string>(BASE_BIAS);
    const wake = getSettings().voiceWakeWord.trim().toLowerCase();
    if (wake) out.add(wake);
    const s = lifts.active;
    if (s) {
      for (const se of s.exs) {
        out.add(lifts.exOrStub(se.exId).name);
      }
    }
    return [...out].slice(0, 40);
  }

  async setEnabled(on: boolean): Promise<boolean> {
    updateSettings({ voiceEnabled: on });
    return this.applyListening(on);
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
    });
    if (!intent) return null;
    return this.executeIntent(intent);
  }

  private syncCursorToSession(): void {
    const curId = lifts.active?.id ?? '';
    if (curId !== this.sessionIdBound) {
      this.sessionIdBound = curId;
      this.cursor = null;
      this.undoStack = [];
    }
  }

  private syncSessionFromSettings(): void {
    const want = getSettings().voiceEnabled;
    if (want && !this.listening && (isNative() || typeof window !== 'undefined')) {
      void this.applyListening(true);
    } else if (!want && (this.listening || this.webRec)) {
      void this.applyListening(false);
    }
  }

  private async applyListening(on: boolean): Promise<boolean> {
    if (isNative()) {
      const res = await setNativeVoiceListening(on, on ? this.biasPhrases() : []);
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
    if (ev.state === 'paused_bg' || ev.state === 'stopped') {
      this.listening = false;
      this.armedUntilMs = 0;
      this.changed.emit();
      return;
    }
    if (ev.state === 'error') {
      this.listening = false;
      this.lastError = ev.error ?? 'Voice recognition error';
      this.changed.emit();
      return;
    }
    if (ev.state === 'listening' && !this.listening) {
      this.listening = true;
      this.lastError = null;
    }

    const txt = (ev.transcript ?? '').trim();
    if (!txt) {
      this.changed.emit();
      return;
    }

    const wakeWord = getSettings().voiceWakeWord;
    if (!ev.isFinal && ev.source !== 'lan') {
      if (!this.isArmed() && parseVoiceTranscript(txt, { wakeWord })?.kind === 'arm_wake') {
        this.armNow();
        this.lastHeard = txt;
        this.changed.emit();
      }
      return;
    }

    const now = Date.now();
    const sig = txt.toLowerCase();
    if (sig === this.lastFinalSig && now - this.lastFinalAt < 450) return;
    this.lastFinalSig = sig;
    this.lastFinalAt = now;
    this.lastHeard = txt;

    const intent = parseVoiceTranscript(txt, { wakeWord, armed: this.isArmed() });
    if (!intent) {
      this.changed.emit();
      return;
    }
    this.executeIntent(intent);
  }

  private notice(text: string, kind: VoiceActionNotice['kind'], toastIt = true): VoiceActionNotice {
    const n: VoiceActionNotice = { text, kind, atMs: Date.now() };
    this.lastAction = n;
    this.changed.emit();
    if (toastIt) this.toasted.emit({ msg: text });
    return n;
  }

  private executeIntent(intent: VoiceIntent): VoiceActionNotice {
    this.syncCursorToSession();
    if (intent.kind === 'arm_wake') {
      this.armNow();
      buzz(20);
      return this.notice('Listening… say weight & reps or "check"', 'info', false);
    }

    this.armedUntilMs = 0;
    const s = lifts.active;
    if (!s) {
      return this.notice('Start a Strength workout to use voice set commands', 'warn');
    }

    if (intent.kind === 'rest_skip') {
      active.clearRest();
      buzz(20);
      return this.notice('Rest timer skipped', 'info');
    }
    if (intent.kind === 'rest_add') {
      if (!active.rest) {
        return this.notice('No rest timer running', 'warn');
      }
      active.adjustRest(intent.deltaSec);
      buzz(20);
      return this.notice(`${intent.deltaSec >= 0 ? '+' : ''}${intent.deltaSec}s rest`, 'info');
    }
    if (intent.kind === 'pause_workout') {
      if (active.pause()) return this.notice('Workout paused', 'info');
      return this.notice('Workout is not running', 'warn');
    }
    if (intent.kind === 'resume_workout') {
      if (active.resume()) return this.notice('Workout resumed', 'info');
      return this.notice('Workout is not paused', 'warn');
    }

    const cur = resolveTargetSet(s.exs, this.cursor);
    if (intent.kind === 'next_ex' || intent.kind === 'prev_ex') {
      const next = stepExerciseCursor(s.exs, cur, intent.kind === 'next_ex' ? 1 : -1);
      if (!next) return this.notice('All sets already logged', 'info');
      this.cursor = next;
      const ptr = resolveTargetSet(s.exs, this.cursor);
      buzz(20);
      return this.notice(ptr ? `Target → ${ptr.label} ${lifts.exOrStub(s.exs[ptr.exIdx].exId).name} (Set ${ptr.setNumber})` : 'Moved exercise', 'info');
    }

    if (intent.kind === 'jump_ex') {
      const jumped = jumpToExerciseCursor(s.exs, intent.target, lifts.exOrStub);
      if (!jumped) return this.notice(`Could not find "${intent.target}" in workout`, 'warn');
      this.cursor = jumped;
      const ptr = resolveTargetSet(s.exs, this.cursor);
      buzz(20);
      return this.notice(ptr ? `Target → ${ptr.label} ${lifts.exOrStub(s.exs[ptr.exIdx].exId).name}` : 'Jumped exercise', 'info');
    }

    if (intent.kind === 'undo') {
      const last = this.undoStack.pop();
      if (!last || last.sessionId !== s.id) return this.notice('Nothing to undo', 'warn');
      const se = s.exs.find((e) => e.uid === last.exUid);
      const set = se?.sets.find((x) => x.id === last.setId);
      if (!se || !set) return this.notice('Set no longer exists', 'warn');
      set.done = false;
      set.w = last.w;
      set.r = last.r;
      if (last.wAuto) set.wAuto = true; else delete set.wAuto;
      if (last.rAuto) set.rAuto = true; else delete set.rAuto;
      if (last.rpe != null) set.rpe = last.rpe; else delete set.rpe;
      this.cursor = { exUid: se.uid, setId: set.id };
      void active.resetIfNoneLogged(s);
      lifts.saveSession(s, true);
      buzz(25);
      return this.notice(`↶ Undid ${lifts.exOrStub(se.exId).name}`, 'warn');
    }

    if (!cur) return this.notice('All sets in this workout are complete!', 'ok');
    const se = s.exs[cur.exIdx];
    const set = se.sets[cur.setIdx];
    const ex = lifts.exOrStub(se.exId);
    const u = liftSettings().unit;

    if (intent.kind === 'start_hold') {
      if (ex.type !== 'time') {
        active.ensureStarted(s);
        active.startRest(intent.seconds ?? active.restSecFor(se), ex.name);
        return this.notice(`Rest timer started · ${ex.name}`, 'info');
      }
      if (this.holdStarter?.(se.uid, set.id, intent.seconds)) {
        buzz(25);
        return this.notice(`▶ Hold countdown · ${intent.seconds ?? set.r ?? 45}s`, 'info');
      }
      return this.notice('Open active workout view to run hold countdown', 'warn');
    }

    if (intent.kind === 'set_only') {
      if (intent.weight != null) editWeight(se, cur.setIdx, fromDisplay(intent.weight, u));
      if (intent.reps != null) editReps(se, cur.setIdx, intent.reps);
      this.cursor = { exUid: se.uid, setId: set.id };
      lifts.saveSession(s, true);
      return this.notice(`Updated ${cur.label} ${ex.name}`, 'info');
    }

    // intent.kind === 'log_current'
    const undoSnap: UndoEntry = {
      sessionId: s.id,
      exUid: se.uid,
      setId: set.id,
      w: set.w,
      r: set.r,
      wAuto: set.wAuto,
      rAuto: set.rAuto,
      rpe: set.rpe,
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
      return this.notice(`Need weight & reps for ${cur.label} ${ex.name} (e.g. "Armory 135 for 10")`, 'warn');
    }
    const logged = toggleDone(se, cur.setIdx, ex);
    if (!logged) {
      return this.notice(`Could not log ${ex.name}`, 'warn');
    }
    this.undoStack.push(undoSnap);
    if (this.undoStack.length > 20) this.undoStack.shift();

    active.ensureStarted(s);
    active.afterSetLogged(s, cur.exIdx);
    this.cursor = advanceCursorAfterLog(s.exs, { exIdx: cur.exIdx, setIdx: cur.setIdx });
    lifts.saveSession(s, true);
    buzz([35, 30, 50]);

    const valTxt = ex.type === 'time'
      ? `${set.r ?? 0}s${set.w ? ` @ ${fmtKg(set.w)} ${u}` : ''}`
      : ex.type === 'bodyweight' && !set.w
        ? `BW × ${set.r ?? 0}`
        : `${set.w != null ? fmtKg(set.w) : '0'} ${u} × ${set.r ?? 0}`;
    return this.notice(`✓ ${cur.label} ${ex.name} · ${valTxt}`, 'ok');
  }
}

export const voice = new VoiceController();
