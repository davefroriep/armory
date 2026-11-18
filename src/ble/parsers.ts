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
  fan?: number;
  rfan?: number;
  pls?: number;
  dt?: number;
  ratio?: number;
  dev?: 'airbike' | 'spin';
  lvl?: number;
  tlvl?: number;
  tgt_w?: number;
  mode?: number;
  pot?: number;
  pos?: number;
}

/** AirBike / SS2K Spin 5 Hz NUS frame: {"dev":"airbike"|"spin","w":127.0,"cad":44.0,...} */
export function parseBikeLine(line: string): BikeFrame | null {
  try {
    const o = JSON.parse(line);
    if (typeof o !== 'object' || o === null || typeof o.w !== 'number' || typeof o.cad !== 'number') return null;
    if (o.dev && o.dev !== 'airbike' && o.dev !== 'spin') return null;
    const out: BikeFrame = {
      w: o.w,
      cad: o.cad,
      dist: Number(o.dist) || 0,
      kcal: Number(o.kcal) || 0,
      spd: Number(o.spd) || 0,
      ...(typeof o.fan === 'number' ? { fan: o.fan } : {}),
      ...(typeof o.rfan === 'number' ? { rfan: o.rfan } : {}),
      ...(typeof o.pls === 'number' ? { pls: o.pls } : {}),
      ...(typeof o.dt === 'number' ? { dt: o.dt } : {}),
      ...(typeof o.ratio === 'number' ? { ratio: o.ratio } : {}),
    };
    if (o.dev === 'spin') {
      out.dev = 'spin';
      if (typeof o.lvl === 'number') out.lvl = o.lvl;
      if (typeof o.tlvl === 'number') out.tlvl = o.tlvl;
      if (typeof o.tgt_w === 'number') out.tgt_w = o.tgt_w;
      if (typeof o.mode === 'number') out.mode = o.mode;
      if (typeof o.pot === 'number') out.pot = o.pot;
      if (typeof o.pos === 'number') out.pos = o.pos;
    }
    return out;
  } catch {
    return null;
  }
}
