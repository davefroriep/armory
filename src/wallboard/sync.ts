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
import { buildWallboardState, wallboardSnapChanged, type WallboardState } from './state';

class WallboardController {
  status: WallboardStatus = {
    running: false,
    port: 8765,
    ip: '127.0.0.1',
    url: 'http://127.0.0.1:8765',
    clients: 0,
  };
  latestState: WallboardState = buildWallboardState();
  readonly changed = new Emitter<void>();

  private inited = false;
  private lastPushMs = 0;
  private pushTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private desiredOn = false;

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
    wallboardSnapChanged.on(throttled);
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
      this.stopHeartbeat();
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
      if (this.desiredOn) this.schedulePush(false);
    }, 1000);
  }

  private stopHeartbeat(): void {
    if (!this.heartbeat) return;
    clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private schedulePush(immediate: boolean): void {
    const now = Date.now();
    const gap = now - this.lastPushMs;
    if (immediate || gap >= 250) {
      if (this.pushTimer) { clearTimeout(this.pushTimer); this.pushTimer = null; }
      this.flushPush(now);
      return;
    }
    if (!this.pushTimer) {
      this.pushTimer = setTimeout(() => {
        this.pushTimer = null;
        this.flushPush(Date.now());
      }, 250 - gap);
    }
  }

  private flushPush(now: number): void {
    this.lastPushMs = now;
    const next = buildWallboardState(now);
    this.latestState = next;
    this.changed.emit();
    if (!this.desiredOn || !isNative()) return;
    void pushNativeWallboardState(JSON.stringify(next)).then((clients) => {
      if (clients !== this.status.clients) {
        this.status = { ...this.status, clients };
        this.changed.emit();
      }
    });
  }
}

export const wallboard = new WallboardController();
export function initWallboardSync(): void {
  wallboard.init();
}
