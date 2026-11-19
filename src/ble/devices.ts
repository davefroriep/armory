import { BleLink } from './link';
import { parseBikeLine, parseHeartRate, LineBuffer } from './parsers';
import { live, pushBike, pushHr } from './telemetry';
import { transport } from './transport';

const HR_SERVICE = '0000180d-0000-1000-8000-00805f9b34fb';
const HR_CHAR = '00002a37-0000-1000-8000-00805f9b34fb';

class PolarLink extends BleLink {
  constructor() {
    super('armory.ble.polar', { services: [HR_SERVICE], matches: (n) => /polar/i.test(n) });
  }
  protected async subscribe(id: string): Promise<void> {
    await transport.subscribe(id, HR_SERVICE, HR_CHAR, (dv) => pushHr(parseHeartRate(dv)));
  }
}

export const NUS_SERVICE = '6e400001-b5a3-f393-e0a9-e50e24dcca9e';
const NUS_TX = '6e400003-b5a3-f393-e0a9-e50e24dcca9e';
const NUS_RX = '6e400002-b5a3-f393-e0a9-e50e24dcca9e';

const BIKE_SILENCE_MS = 6000;
const CMD_PACE_MS = 35;

class NusBikeLink extends BleLink {
  private lines = new LineBuffer();
  private txChain: Promise<void> = Promise.resolve();
  private lastTxAt = 0;

  /** Both bikes stream at 5 Hz even when idle, so 6 s of silence means the link is dead. */
  protected isHealthy(): boolean {
    return Date.now() - Math.max(this.connectedAt, live.bikeAt) < BIKE_SILENCE_MS;
  }

  protected async subscribe(id: string): Promise<void> {
    this.lines.reset();
    await transport.subscribe(id, NUS_SERVICE, NUS_TX, (dv) => {
      for (const line of this.lines.push(new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength))) {
        const f = parseBikeLine(line);
        if (f) pushBike(f);
      }
    });
  }

  /** ASCII command to the bike, serialized & paced so Android GATT / ESP32 BLE RX never drop back-to-back writes. */
  command(cmd: string): Promise<void> {
    const run = async (): Promise<void> => {
      if (!this.id || !this.connected) return;
      const wait = CMD_PACE_MS - (Date.now() - this.lastTxAt);
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      if (!this.id || !this.connected) return;
      this.lastTxAt = Date.now();
      await transport.write(this.id, NUS_SERVICE, NUS_RX, new TextEncoder().encode(cmd + '\n'));
    };
    const next = this.txChain.then(run, run);
    this.txChain = next.catch(() => {});
    return next;
  }
}

export const hrLink = new PolarLink();
export const bikeLink = new NusBikeLink('armory.ble.airbike', {
  services: [NUS_SERVICE],
  names: ['AssaultBike'],
  matches: (n) => n === 'AssaultBike',
});
export const spinLink = new NusBikeLink('armory.ble.spin', {
  services: [NUS_SERVICE],
  names: ['SS2K_Tuner'],
  matches: (n) => /^SS2K/i.test(n),
});
