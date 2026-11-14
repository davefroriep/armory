import { Emitter, sleep } from '../emitter';
import { transport, type DeviceSpec } from './transport';
import { linkStatus } from './telemetry';

export type LinkState = 'unsupported' | 'idle' | 'connecting' | 'connected' | 'lost';

const CONNECT_TIMEOUT_MS = 12000;
const RETRY_MS = 3000;

/**
 * One BLE peripheral that reconnects by itself, on top of whichever transport is active.
 *
 * Web: `ensure()` silently re-attaches to a device the browser already granted; the first pairing
 * needs one tap (`pair()`). Native (Android app): the stored device id is connected directly.
 */
export abstract class BleLink {
  state: LinkState = 'idle';
  deviceName = '';
  readonly changed = new Emitter();

  protected id: string | null = null;
  protected wanted = false;
  private looping = false;

  constructor(private readonly storeKey: string, protected readonly spec: DeviceSpec) {
    if (!transport.supported()) this.state = 'unsupported';
  }

  protected abstract subscribe(id: string): Promise<void>;

  protected setState(s: LinkState): void {
    if (this.state === s) return;
    this.state = s;
    this.changed.emit();
    linkStatus.emit();
  }

  get connected(): boolean {
    return this.state === 'connected';
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
    if (this.id) void transport.disconnect(this.id);
    if (this.state !== 'unsupported') this.setState('idle');
  }

  forget(): void {
    this.release();
    this.id = null;
    localStorage.removeItem(this.storeKey);
  }

  private onDrop = () => {
    if (!this.wanted) return;
    this.setState('lost');
    void this.connectLoop();
  };

  private async connectLoop(): Promise<void> {
    if (this.looping) return;
    this.looping = true;
    try {
      while (this.wanted && this.id && this.state !== 'connected') {
        this.setState(this.state === 'lost' ? 'lost' : 'connecting');
        try {
          await transport.connect(this.id, this.onDrop, CONNECT_TIMEOUT_MS);
          await this.subscribe(this.id);
          this.deviceName = transport.name(this.id);
          this.setState('connected');
          return;
        } catch (e) {
          await transport.disconnect(this.id);
          if (!this.wanted) break;
          this.setState('lost');
          await sleep(RETRY_MS);
          console.debug('retry connect', this.constructor.name, e);
        }
      }
    } finally {
      this.looping = false;
    }
  }
}
