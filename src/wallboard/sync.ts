import { bikeFrame, hrBeat, linkStatus } from '../ble/telemetry';
import { Emitter } from '../emitter';
import { calendar } from '../intervals/calendar';
import { active } from '../lifts/active';
import { lifts } from '../lifts/store';
import { voice } from '../lifts/voice';
import {
  getNativeWallboardStatus, isNative, pushNativeWallboardState,
  startNativeWallboard, stopNativeWallboard, type WallboardStatus,
} from '../native/service';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { WALLBOARD_HTML } from './page';
import { isWallboardWorkoutActive } from './snapshot';
import { buildWallboardState, wallboardSnapChanged, type WallboardState } from './state';

export { isWallboardWorkoutActive };

class WallboardController {
  status: WallboardStatus = {
    running: false,
    port: 8765,
    ip: '127.0.0.1',
    url: 'http://127.0.0.1:8765',
    clients: 0,
  };
  latestState: WallboardState = buildWallboardState();
  relayOnline = false;
  readonly changed = new Emitter<void>();

  private inited = false;
  private lastPushMs = 0;
  private pushTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private desiredOn = false;

  private relayWasActive = false;
  private relayBusy = false;
  private relayBackoffUntil = 0;
  private relayHtmlSynced = false;
  private lastRelayJson = '';
  private lastRelayPushMs = 0;
  private lastStandbySig = '';
  private lastStandbyPushMs = 0;
  private lastSnapKey = '';

  private relayBaseUrl(): string {
    return (getSettings().gymCamUrl || 'http://192.168.1.152:8766').trim().replace(/\/+$/, '');
  }

  relayUrl(): string {
    return `${this.relayBaseUrl()}/wallboard`;
  }

  init(): void {
    if (this.inited) return;
    this.inited = true;

    const immediate = (): void => this.schedulePush(true);
    const throttled = (): void => this.schedulePush(false);

    lifts.changed.on(immediate);
    voice.changed.on(immediate);
    active.restChanged.on(immediate);
    calendar.changed.on(immediate);
    linkStatus.on(immediate);
    hrBeat.on(throttled);
    bikeFrame.on(throttled);
    active.tick.on(throttled);
    wallboardSnapChanged.on(() => {
      const s = buildWallboardState();
      const key = `${s.mode}|${s.cardio?.phase ?? ''}|${s.cardio?.paused ?? ''}|${s.replay?.url ?? ''}|${s.replay?.startedAtMs ?? ''}|${s.lift?.hold?.active ?? ''}`;
      if (key !== this.lastSnapKey) {
        this.lastSnapKey = key;
        immediate();
      } else {
        throttled();
      }
    });
    settingsChanged.on(() => {
      void this.syncFromSettings();
      immediate();
    });

    void this.syncFromSettings();
    this.schedulePush(true);
  }

  async setEnabled(on: boolean, port?: number): Promise<WallboardStatus> {
    updateSettings({
      wallboardEnabled: on,
      ...(port != null ? { wallboardPort: port } : {}),
    });
    return this.syncFromSettings();
  }

  async refreshStatus(): Promise<WallboardStatus> {
    if (!isNative()) return this.status;
    this.status = await getNativeWallboardStatus();
    this.changed.emit();
    return this.status;
  }

  async wakeRelayTv(): Promise<{ ok: boolean; message: string }> {
    await this.syncRelayHtml();
    const res = await this.postRelayJson('/api/wallboard/wake', {
      mode: this.latestState.mode,
      title: this.latestState.lift?.title ?? this.latestState.cardio?.title ?? 'Manual Wake',
      state: this.latestState,
      sourceUrl: this.status.running ? this.status.url : '',
      force: true,
      sync: true,
    }, 8000);
    if (!res) {
      this.setRelayOnline(false);
      return { ok: false, message: 'OptiPlex unreachable on home Wi-Fi' };
    }
    this.setRelayOnline(res.ok);
    try {
      const data = (await res.json()) as { ok?: boolean; adb?: { ok?: boolean; message?: string } };
      const adbOk = data?.adb?.ok;
      return {
        ok: typeof adbOk === 'boolean' ? adbOk : res.ok,
        message: data?.adb?.message || (res.ok ? 'Sent wake & launch to Fire TV' : `HTTP ${res.status}`),
      };
    } catch {
      return { ok: res.ok, message: res.ok ? 'Sent wake & launch to Fire TV' : `HTTP ${res.status}` };
    }
  }

  async sleepRelayTv(): Promise<{ ok: boolean; message: string }> {
    const res = await this.postRelayJson('/api/wallboard/sleep', {
      reason: 'manual',
      delaySec: 0,
      state: this.latestState,
      sync: true,
    }, 6000);
    if (!res) {
      this.setRelayOnline(false);
      return { ok: false, message: 'OptiPlex unreachable on home Wi-Fi' };
    }
    this.setRelayOnline(res.ok);
    try {
      const data = (await res.json()) as { ok?: boolean; adb?: { ok?: boolean; message?: string } };
      const adbOk = data?.adb?.ok;
      return {
        ok: typeof adbOk === 'boolean' ? adbOk : res.ok,
        message: data?.adb?.message || (res.ok ? 'Sent sleep command to Fire TV' : `HTTP ${res.status}`),
      };
    } catch {
      return { ok: res.ok, message: res.ok ? 'Sent sleep command to Fire TV' : `HTTP ${res.status}` };
    }
  }

  private async syncFromSettings(): Promise<WallboardStatus> {
    const s = getSettings();
    const want = s.wallboardEnabled;
    if (want && (!this.desiredOn || this.status.port !== s.wallboardPort)) {
      this.desiredOn = true;
      if (isNative()) {
        this.status = await startNativeWallboard(s.wallboardPort, WALLBOARD_HTML);
      } else {
        this.status = { ...this.status, running: false, port: s.wallboardPort, url: `http://localhost:${s.wallboardPort}` };
      }
      this.startHeartbeat();
      this.schedulePush(true);
      this.changed.emit();
    } else if (!want && (this.desiredOn || this.status.running)) {
      this.desiredOn = false;
      if (!this.relayWasActive) this.stopHeartbeat();
      if (isNative()) {
        await stopNativeWallboard();
      }
      this.status = { ...this.status, running: false, clients: 0 };
      this.changed.emit();
    }
    return this.status;
  }

  private startHeartbeat(): void {
    if (this.heartbeat) return;
    this.heartbeat = setInterval(() => {
      if (this.desiredOn || this.relayWasActive) this.schedulePush(false);
    }, 1000);
  }

  private stopHeartbeat(): void {
    if (!this.heartbeat) return;
    clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private setRelayOnline(on: boolean): void {
    if (this.relayOnline === on) return;
    this.relayOnline = on;
    this.changed.emit();
  }

  private async postRelayJson(path: string, body: unknown, timeoutMs: number): Promise<Response | null> {
    if (typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetch(`${this.relayBaseUrl()}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  private async syncRelayHtml(): Promise<void> {
    if (this.relayHtmlSynced) return;
    const r = await this.postRelayJson('/api/wallboard/html', { html: WALLBOARD_HTML }, 2000);
    if (r?.ok) this.relayHtmlSynced = true;
  }

  private async pushRelayWake(state: WallboardState, force: boolean): Promise<void> {
    const r = await this.postRelayJson('/api/wallboard/wake', {
      mode: state.mode,
      title: state.lift?.title ?? state.cardio?.title ?? 'Workout',
      state,
      sourceUrl: this.status.running ? this.status.url : '',
      force,
    }, 2500);
    this.setRelayOnline(!!r?.ok);
    if (!r?.ok) this.relayBackoffUntil = Date.now() + 15_000;
  }

  private async pushRelayState(state: WallboardState, json: string, now: number): Promise<void> {
    this.relayBusy = true;
    try {
      const r = await this.postRelayJson('/api/wallboard/state', {
        state,
        workoutActive: true,
        sourceUrl: this.status.running ? this.status.url : '',
      }, 1500);
      if (r?.ok) {
        this.setRelayOnline(true);
        this.lastRelayJson = json;
        this.lastRelayPushMs = now;
      } else {
        this.setRelayOnline(false);
        this.relayBackoffUntil = Date.now() + 15_000;
      }
    } finally {
      this.relayBusy = false;
    }
  }

  private async pushRelaySleep(state: WallboardState, reason: string, delaySec: number): Promise<void> {
    await this.postRelayJson('/api/wallboard/sleep', { reason, delaySec, state }, 2000);
  }

  private async pushRelayStandby(state: WallboardState, sig: string, now: number): Promise<void> {
    this.relayBusy = true;
    try {
      const r = await this.postRelayJson('/api/wallboard/state', {
        state,
        workoutActive: false,
        sourceUrl: this.status.running ? this.status.url : '',
      }, 1500);
      if (r?.ok) {
        this.setRelayOnline(true);
        this.lastStandbySig = sig;
        this.lastStandbyPushMs = now;
      } else {
        this.setRelayOnline(false);
        this.relayBackoffUntil = Date.now() + 15_000;
      }
    } finally {
      this.relayBusy = false;
    }
  }

  pushNow(): void {
    if (this.pushTimer) { clearTimeout(this.pushTimer); this.pushTimer = null; }
    this.flushPush(Date.now(), true);
  }

  private schedulePush(immediate: boolean): void {
    const now = Date.now();
    const gap = now - this.lastPushMs;
    if (immediate || gap >= 250) {
      if (this.pushTimer) { clearTimeout(this.pushTimer); this.pushTimer = null; }
      this.flushPush(now, immediate);
      return;
    }
    if (!this.pushTimer) {
      this.pushTimer = setTimeout(() => {
        this.pushTimer = null;
        this.flushPush(Date.now(), false);
      }, 250 - gap);
    }
  }

  private flushPush(now: number, forceRelay = false): void {
    this.lastPushMs = now;
    const next = buildWallboardState(now);
    this.latestState = next;
    this.changed.emit();

    const json = JSON.stringify(next);
    if (this.desiredOn && isNative()) {
      void pushNativeWallboardState(json).then((clients) => {
        if (clients !== this.status.clients) {
          this.status = { ...this.status, clients };
          this.changed.emit();
        }
      });
    }

    const s = getSettings();
    if (s.wallboardRelayEnabled && typeof fetch !== 'undefined' && typeof AbortController !== 'undefined') {
      const activeNow = isWallboardWorkoutActive(next) || !!next.replay;
      if (activeNow && !this.relayWasActive) {
        this.relayWasActive = true;
        this.relayBackoffUntil = 0;
        this.startHeartbeat();
        void this.syncRelayHtml();
        this.lastRelayJson = json;
        this.lastRelayPushMs = now;
        void this.pushRelayWake(next, false);
      } else if (activeNow) {
        if (!this.relayBusy && now >= this.relayBackoffUntil
          && (forceRelay || (json !== this.lastRelayJson && now - this.lastRelayPushMs >= 400) || now - this.lastRelayPushMs >= 5000)) {
          void this.pushRelayState(next, json, now);
        }
      } else if (this.relayWasActive) {
        this.relayWasActive = false;
        this.lastStandbySig = '';
        if (!this.desiredOn) this.stopHeartbeat();
        void this.pushRelaySleep(next, next.mode === 'standby' ? 'workout_finished' : 'workout_paused', 30);
      } else if (next.mode === 'standby' && next.standby) {
        const sig = JSON.stringify({ d: next.standby.dateLabel, w: next.standby.weekLabel, i: next.standby.todayItems });
        if (!this.relayBusy && now >= this.relayBackoffUntil
          && (sig !== this.lastStandbySig || (forceRelay && now - this.lastStandbyPushMs >= 5000))) {
          void this.pushRelayStandby(next, sig, now);
        }
      }
    } else if (this.relayWasActive) {
      this.relayWasActive = false;
      this.lastStandbySig = '';
      if (!this.desiredOn) this.stopHeartbeat();
    }
  }
}

export const wallboard = new WallboardController();
export function initWallboardSync(): void {
  wallboard.init();
}
