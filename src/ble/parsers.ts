/** Pure parsers for the BLE payloads (unit-tested). */

/** Standard Heart Rate Measurement (0x2A37): flags bit0 selects uint16 vs uint8 BPM. */
export function parseHeartRate(dv: DataView): number {
  const flags = dv.getUint8(0);
  return flags & 0x01 ? dv.getUint16(1, true) : dv.getUint8(1);
}

/** Splits a notification stream into newline-terminated lines (NUS frames are `{...}\n`). */
export class LineBuffer {
  private buf = '';
  private dec = new TextDecoder();
  push(chunk: ArrayBuffer | Uint8Array): string[] {
    this.buf += this.dec.decode(chunk, { stream: true });
    const parts = this.buf.split('\n');
    this.buf = parts.pop() ?? '';
    return parts.map((p) => p.trim()).filter(Boolean);
  }
  reset(): void {
    this.buf = '';
  }
}

export interface BikeFrame {
  w: number;
  cad: number;
  dist: number;
  kcal: number;
  spd: number;
}

/** AirBike 5 Hz NUS frame: {"dev":"airbike","w":127.0,"cad":44.0,"dist":452.1,"kcal":15.42,"spd":16.87,...} */
export function parseBikeLine(line: string): BikeFrame | null {
  try {
    const o = JSON.parse(line);
    if (typeof o !== 'object' || o === null || typeof o.w !== 'number' || typeof o.cad !== 'number') return null;
    if (o.dev && o.dev !== 'airbike') return null;
    return { w: o.w, cad: o.cad, dist: Number(o.dist) || 0, kcal: Number(o.kcal) || 0, spd: Number(o.spd) || 0 };
  } catch {
    return null;
  }
}
