import { BleClient } from '@capacitor-community/bluetooth-le';
import { isNative } from '../native/service';

/** What a link looks for. Full 128-bit UUIDs (the native plugin requires them). */
export interface DeviceSpec {
  services: string[];
  /** Exact advertised names accepted in addition to the service filter (web chooser only). */
  names?: string[];
  /** Used to re-find an already-granted device without the chooser (web). */
  matches(name: string): boolean;
}

/**
 * Bluetooth transport. Same app code runs on both:
 *  - WebTransport: Web Bluetooth in Chrome (the GitHub Pages PWA).
 *  - NativeTransport: Capacitor + @capacitor-community/bluetooth-le (the Android app), which talks to
 *    Android's native GATT stack and stays connected under the foreground service.
 */
export interface BleTransport {
  readonly kind: 'web' | 'native';
  supported(): boolean;
  /** Chooser. Web needs a user gesture; native shows its own picker. Returns a device id. */
  pick(spec: DeviceSpec): Promise<string | null>;
  /** A device we may reconnect to without a chooser, or null. */
  known(spec: DeviceSpec, storedId: string | null): Promise<string | null>;
  connect(id: string, onDisconnect: () => void, timeoutMs: number): Promise<void>;
  disconnect(id: string): Promise<void>;
  subscribe(id: string, service: string, char: string, cb: (dv: DataView) => void): Promise<void>;
  write(id: string, service: string, char: string, data: Uint8Array): Promise<void>;
  name(id: string): string;
  forgetAll(): Promise<void>;
}

// ------------------------------------------------------------------------------------------- web

class WebTransport implements BleTransport {
  readonly kind = 'web' as const;
  private devices = new Map<string, BluetoothDevice>();
  private drops = new Map<string, () => void>();

  supported(): boolean {
    return typeof navigator !== 'undefined' && !!navigator.bluetooth;
  }

  async pick(spec: DeviceSpec): Promise<string | null> {
    try {
      const filters: BluetoothLEScanFilter[] = [
        ...spec.services.map((s) => ({ services: [s] })),
        ...(spec.names ?? []).map((name) => ({ name })),
      ];
      const d = await navigator.bluetooth.requestDevice({ filters, optionalServices: spec.services });
      return this.remember(d);
    } catch (e) {
      if ((e as DOMException).name !== 'NotFoundError') console.warn('pick failed', e);
      return null;
    }
  }

  async known(spec: DeviceSpec, storedId: string | null): Promise<string | null> {
    try {
      if (!navigator.bluetooth.getDevices) return null;
      const list = await navigator.bluetooth.getDevices();
      const d = list.find((x) => x.id === storedId) ?? list.find((x) => spec.matches(x.name ?? ''));
      return d ? this.remember(d) : null;
    } catch {
      return null;
    }
  }

  private remember(d: BluetoothDevice): string {
    this.devices.set(d.id, d);
    d.removeEventListener('gattserverdisconnected', this.onGatt);
    d.addEventListener('gattserverdisconnected', this.onGatt);
    return d.id;
  }

  private onGatt = (e: Event) => this.drops.get((e.target as BluetoothDevice).id)?.();

  private dev(id: string): BluetoothDevice {
    const d = this.devices.get(id);
    if (!d) throw new Error('unknown device');
    return d;
  }

  async connect(id: string, onDisconnect: () => void, timeoutMs: number): Promise<void> {
    const d = this.dev(id);
    this.drops.set(id, onDisconnect);
    await Promise.race([
      d.gatt!.connect(),
      new Promise((_, rej) => setTimeout(() => rej(new Error('connect timeout')), timeoutMs)),
    ]);
  }

  async disconnect(id: string): Promise<void> {
    this.drops.delete(id);
    try { this.devices.get(id)?.gatt?.disconnect(); } catch { /* gone */ }
  }

  async subscribe(id: string, service: string, char: string, cb: (dv: DataView) => void): Promise<void> {
    const svc = await this.dev(id).gatt!.getPrimaryService(service);
    const ch = await svc.getCharacteristic(char);
    ch.addEventListener('characteristicvaluechanged', () => { if (ch.value) cb(ch.value); });
    await ch.startNotifications();
  }

  async write(id: string, service: string, char: string, data: Uint8Array): Promise<void> {
    const svc = await this.dev(id).gatt!.getPrimaryService(service);
    const ch = await svc.getCharacteristic(char);
    await ch.writeValueWithoutResponse(data as BufferSource);
  }

  name(id: string): string {
    return this.devices.get(id)?.name ?? '';
  }

  async forgetAll(): Promise<void> {
    try {
      for (const d of (await navigator.bluetooth?.getDevices?.()) ?? []) {
        await (d as unknown as { forget?: () => Promise<void> }).forget?.();
      }
    } catch { /* unsupported */ }
  }
}

// ----------------------------------------------------------------------------------------- native

class NativeTransport implements BleTransport {
  readonly kind = 'native' as const;
  private ready: Promise<void> | null = null;
  private names = new Map<string, string>();

  supported(): boolean {
    return true;
  }

  private init(): Promise<void> {
    // Requests BLUETOOTH_SCAN / BLUETOOTH_CONNECT on first use (Android 12+).
    this.ready ??= BleClient.initialize({ androidNeverForLocation: true });
    return this.ready;
  }

  async pick(spec: DeviceSpec): Promise<string | null> {
    try {
      await this.init();
      const d = await BleClient.requestDevice({ services: spec.services });
      this.names.set(d.deviceId, d.name ?? '');
      return d.deviceId;
    } catch (e) {
      console.warn('native pick failed', e);
      return null;
    }
  }

  /** Native connects straight to a stored MAC, no scan needed. */
  async known(_spec: DeviceSpec, storedId: string | null): Promise<string | null> {
    if (!storedId) return null;
    await this.init().catch(() => {});
    return storedId;
  }

  async connect(id: string, onDisconnect: () => void, timeoutMs: number): Promise<void> {
    await this.init();
    await BleClient.connect(id, () => onDisconnect(), { timeout: timeoutMs });
  }

  async disconnect(id: string): Promise<void> {
    try { await BleClient.disconnect(id); } catch { /* not connected */ }
  }

  async subscribe(id: string, service: string, char: string, cb: (dv: DataView) => void): Promise<void> {
    try {
      await BleClient.startNotifications(id, service, char, cb);
    } catch (e) {
      // Reconnecting to a peripheral Android still holds an ACL link to can skip service discovery
      // ("Characteristic not found"). Force a fresh discovery (also clears the GATT cache) and retry once.
      if (!/not found|service/i.test((e as Error)?.message ?? '')) throw e;
      await BleClient.discoverServices(id);
      await BleClient.startNotifications(id, service, char, cb);
    }
  }

  async write(id: string, service: string, char: string, data: Uint8Array): Promise<void> {
    await BleClient.writeWithoutResponse(id, service, char, new DataView(data.buffer, data.byteOffset, data.byteLength));
  }

  name(id: string): string {
    return this.names.get(id) ?? '';
  }

  async forgetAll(): Promise<void> { /* ids live in localStorage; cleared by the caller */ }
}

export const transport: BleTransport = isNative() ? new NativeTransport() : new WebTransport();
