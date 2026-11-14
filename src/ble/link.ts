import { Emitter, sleep } from '../emitter';
import { linkStatus } from './telemetry';

export type LinkState = 'unsupported' | 'idle' | 'connecting' | 'connected' | 'lost';

const CONNECT_TIMEOUT_MS = 12000;
const RETRY_MS = 3000;

/**
 * One Web Bluetooth peripheral that reconnects by itself.
 *
 * Launch behaviour: `ensure()` silently re-attaches to a device this browser has already been
 * granted (navigator.bluetooth.getDevices()). The very first pairing - and any browser that does not
 * keep grants - needs one tap on the chip (`pair()`), because Chrome only allows the chooser from a
 * user gesture.
 */
export abstract class BleLink {
  state: LinkState = 'idle';
  deviceName = '';
  readonly changed = new Emitter();

  protected device: BluetoothDevice | null = null;
  protected wanted = false;
  private looping = false;

  constructor(private readonly storeKey: string) {
    if (typeof navigator === 'undefined' || !navigator.bluetooth) this.state = 'unsupported';
  }

  protected abstract requestOptions(): RequestDeviceOptions;
  protected abstract matches(d: BluetoothDevice): boolean;
  protected abstract subscribe(server: BluetoothRemoteGATTServer): Promise<void>;

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
    if (!this.device) {
      const granted = await this.grantedDevice();
      if (!granted) {
        this.setState('idle');
        return;
      }
      this.attach(granted);
    }
    void this.connectLoop();
  }

  /** Needs a user gesture: opens the chooser the first time. */
  async pair(): Promise<void> {
    if (this.state === 'unsupported') return;
    this.wanted = true;
    try {
      const d = await navigator.bluetooth.requestDevice(this.requestOptions());
      this.attach(d);
      void this.connectLoop();
    } catch (e) {
      if ((e as DOMException).name !== 'NotFoundError') console.warn('pair failed', e);
    }
  }

  /** Stop reconnecting and drop the GATT link (the browser keeps the grant). */
  release(): void {
    this.wanted = false;
    try {
      this.device?.gatt?.disconnect();
    } catch { /* already gone */ }
    if (this.state !== 'unsupported') this.setState('idle');
  }

  private async grantedDevice(): Promise<BluetoothDevice | null> {
    try {
      if (!navigator.bluetooth.getDevices) return null;
      const list = await navigator.bluetooth.getDevices();
      const saved = localStorage.getItem(this.storeKey);
      return list.find((d) => d.id === saved) ?? list.find((d) => this.matches(d)) ?? null;
    } catch {
      return null;
    }
  }

  private attach(d: BluetoothDevice): void {
    if (this.device && this.device !== d) this.device.removeEventListener('gattserverdisconnected', this.onDrop);
    this.device = d;
    this.deviceName = d.name ?? '';
    localStorage.setItem(this.storeKey, d.id);
    d.removeEventListener('gattserverdisconnected', this.onDrop);
    d.addEventListener('gattserverdisconnected', this.onDrop);
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
      while (this.wanted && this.device && !this.device.gatt?.connected) {
        this.setState(this.state === 'lost' ? 'lost' : 'connecting');
        try {
          const server = await Promise.race([
            this.device.gatt!.connect(),
            sleep(CONNECT_TIMEOUT_MS).then(() => { throw new Error('connect timeout'); }),
          ]);
          await this.subscribe(server);
          this.setState('connected');
          return;
        } catch (e) {
          try { this.device.gatt?.disconnect(); } catch { /* ignore */ }
          if (!this.wanted) break;
          this.setState('lost');
          await sleep(RETRY_MS);
          console.debug('retry connect', this.constructor.name, e);
        }
      }
      if (this.device?.gatt?.connected && this.wanted) this.setState('connected');
    } finally {
      this.looping = false;
    }
  }
}
