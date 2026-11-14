import { BleLink } from './link';
import { parseBikeLine, parseHeartRate, LineBuffer } from './parsers';
import { pushBike, pushHr } from './telemetry';

const HR_SERVICE = 'heart_rate';
const HR_CHAR = 'heart_rate_measurement';

class PolarLink extends BleLink {
  protected requestOptions(): RequestDeviceOptions {
    return { filters: [{ services: [HR_SERVICE] }], optionalServices: [HR_SERVICE, 'battery_service'] };
  }
  protected matches(d: BluetoothDevice): boolean {
    return /polar/i.test(d.name ?? '');
  }
  protected async subscribe(server: BluetoothRemoteGATTServer): Promise<void> {
    const svc = await server.getPrimaryService(HR_SERVICE);
    const ch = await svc.getCharacteristic(HR_CHAR);
    ch.addEventListener('characteristicvaluechanged', () => {
      if (ch.value) pushHr(parseHeartRate(ch.value));
    });
    await ch.startNotifications();
  }
}

export const NUS_SERVICE = '6e400001-b5a3-f393-e0a9-e50e24dcca9e';
const NUS_TX = '6e400003-b5a3-f393-e0a9-e50e24dcca9e';
const NUS_RX = '6e400002-b5a3-f393-e0a9-e50e24dcca9e';

class AirbikeLink extends BleLink {
  private lines = new LineBuffer();
  private rx: BluetoothRemoteGATTCharacteristic | null = null;

  protected requestOptions(): RequestDeviceOptions {
    return { filters: [{ services: [NUS_SERVICE] }, { name: 'AssaultBike' }], optionalServices: [NUS_SERVICE] };
  }
  protected matches(d: BluetoothDevice): boolean {
    return d.name === 'AssaultBike';
  }
  protected async subscribe(server: BluetoothRemoteGATTServer): Promise<void> {
    const svc = await server.getPrimaryService(NUS_SERVICE);
    const tx = await svc.getCharacteristic(NUS_TX);
    this.rx = await svc.getCharacteristic(NUS_RX).catch(() => null);
    this.lines.reset();
    tx.addEventListener('characteristicvaluechanged', () => {
      if (!tx.value) return;
      for (const line of this.lines.push(new Uint8Array(tx.value.buffer, tx.value.byteOffset, tx.value.byteLength))) {
        const f = parseBikeLine(line);
        if (f) pushBike(f);
      }
    });
    await tx.startNotifications();
  }

  /** ASCII command to the bike (SET_RATIO, RESET, ...). Not used by the HUD yet. */
  async command(cmd: string): Promise<void> {
    await this.rx?.writeValueWithoutResponse(new TextEncoder().encode(cmd + '\n'));
  }
}

export const hrLink = new PolarLink('armory.ble.polar');
export const bikeLink = new AirbikeLink('armory.ble.airbike');
