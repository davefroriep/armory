import { Emitter, sleep } from '../emitter';
import { transport, type DeviceSpec } from './transport';
import { linkStatus } from './telemetry';

export type LinkState = 'unsupported' | 'idle' | 'connecting' | 'connected' | 'lost';

const CONNECT_TIMEOUT_MS = 10000;
const RETRY_MS = 1500;
/** A disconnect that never completes (device already gone) must not stall the next attempt. */
const DISCONNECT_WAIT_MS = 1500;
const WATCHDOG_MS = 1000;

/**
 * One BLE peripheral that reconnects by itself, on top of whichever transport is active.
 *
 * Web: `ensure()` silently re-attaches to a device the browser already granted; the first pairing
 * needs one tap (`pair()`). Native (Android app): the stored device id is connected directly.
 *
 * Self-healing:
 *  - failed attempts retry quickly (short timeout, fire-and-forget disconnect) so a bike that just
 *    rebooted (its radio comes up ~15 s after power) is picked up within a couple of seconds;
 *  - a "connected" link that stops delivering data (`isHealthy()`) is torn down and rebuilt;
 *  - `kick()` skips the retry wait (app resumed, chip tapped).
 */
export abstract class BleLink {
  state: LinkState = 'idle';
  deviceName = '';
  readonly changed = new Emitter();
  /** Connect attempts since the last successful connection. */
  attempts = 0;
  lastError = '';
  connectedAt = 0;

  protected id: string | null = null;
  protected wanted = false;
  private looping = false;
  private wake: (() => void) | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly storeKey: string, protected readonly spec: DeviceSpec) {
    if (!transport.supported()) this.state = 'unsupported';
  }

  protected abstract subscribe(id: string): Promise<void>;

  /** Connected links that return false for too long are treated as dead and reconnected. */
  protected isHealthy(): boolean {
    return true;
  }

  protected setState(s: LinkState): void {
    if (this.state === s) return;
    this.state = s;
    if (s === 'connected') {
      this.attempts = 0;
      this.lastError = '';
      this.connectedAt = Date.now();
      this.startWatchdog();
    } else {
      this.stopWatchdog();
    }
    this.changed.emit();
    linkStatus.emit();
  }

  get connected(): boolean {
    return this.state === 'connected';
  }

  get hasDevice(): boolean {
    return this.id != null || localStorage.getItem(this.storeKey) != null;
  }

  /** Human-readable status for the HUD ("try 3 · Connection timeout"). */
  diag(): string {
    if (this.state === 'unsupported') return 'Bluetooth is not available here';
    if (this.connected) return '';
    if (!this.hasDevice) return 'Tap the BIKE chip to pair';
    const why = this.lastError ? ` · ${this.lastError.replace(/\.$/, '')}` : '';
    return this.attempts ? `Reconnecting (try ${this.attempts}${why})` : 'Connecting…';
  }

  /** Start (or keep) trying to be connected, without any prompt. */
  async ensure(): Promise<void> {
    if (this.state === 'unsupported') return;
    this.wanted = true;
    if (this.connected || this.looping) return;
    if (!this.id) {
      this.id = await transport.known(this.spec, localStorage.getItem(this.storeKey));
      if (!this.id) {
        this.setState('idle');
        return;
      }
    }
    void this.connectLoop();
  }

  /** Skip any retry wait and try right now (app came to the foreground, chip tapped). */
  kick(): void {
    if (this.state === 'unsupported' || !this.wanted || this.connected) return;
    if (this.looping) this.wake?.();
    else void this.ensure();
  }

  /** Chooser (web: needs a user gesture). */
  async pair(): Promise<void> {
    if (this.state === 'unsupported') return;
    this.wanted = true;
    const id = await transport.pick(this.spec);
    if (!id) return;
    this.id = id;
    this.deviceName = transport.name(id);
    localStorage.setItem(this.storeKey, id);
    void this.connectLoop();
  }

  /** Stop reconnecting and drop the link (the stored pairing is kept). */
  release(): void {
    this.wanted = false;
    this.wake?.();
    if (this.id) void this.dropLink(this.id);
    if (this.state !== 'unsupported') this.setState('idle');
  }

  forget(): void {
    this.release();
    this.id = null;
    localStorage.removeItem(this.storeKey);
  }

  private async dropLink(id: string): Promise<void> {
    await Promise.race([transport.disconnect(id).catch(() => {}), sleep(DISCONNECT_WAIT_MS)]);
  }

  private onDrop = () => {
    if (!this.wanted) return;
    this.setState('lost');
    void this.connectLoop();
  };

  private startWatchdog(): void {
    this.stopWatchdog();
    this.watchdog = setInterval(() => {
      if (!this.connected || !this.wanted || this.isHealthy()) return;
      // "Connected" but silent: rebuild the link from scratch.
      this.lastError = 'No data from the bike';
      const id = this.id;
      this.setState('lost');
      if (id) void this.dropLink(id).then(() => void this.connectLoop());
    }, WATCHDOG_MS);
  }

  private stopWatchdog(): void {
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = null;
  }

  private nap(ms: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => { this.wake = null; resolve(); }, ms);
      this.wake = () => {
        clearTimeout(timer);
        this.wake = null;
        resolve();
      };
    });
  }

  private async connectLoop(): Promise<void> {
    if (this.looping) return;
    this.looping = true;
    try {
      while (this.wanted && this.id && this.state !== 'connected') {
        this.setState(this.state === 'lost' ? 'lost' : 'connecting');
        this.attempts++;
        try {
          await transport.connect(this.id, this.onDrop, CONNECT_TIMEOUT_MS);
          await this.subscribe(this.id);
          this.deviceName = transport.name(this.id);
          this.setState('connected');
          return;
        } catch (e) {
          this.lastError = (e as Error)?.message ?? String(e);
          void this.dropLink(this.id);
          if (!this.wanted) break;
          this.setState('lost');
          linkStatus.emit();
          await this.nap(RETRY_MS);
          console.debug('retry connect', this.constructor.name, e);
        }
      }
    } finally {
      this.looping = false;
    }
  }
}
