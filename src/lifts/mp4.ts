/** Seconds between ISO BMFF epoch (1904-01-01T00:00:00Z) and Unix epoch (1970-01-01T00:00:00Z). */
const MP4_EPOCH_OFFSET_SEC = 2_082_844_800;
const MIN_VALID_MS = Date.UTC(2010, 0, 1);
const MAX_VALID_MS = Date.UTC(2045, 0, 1);
const MVHD_TAG = 0x6d766864; // 'mvhd'
const MOOV_TAG = 0x6d6f6f76; // 'moov'

const pad2 = (n: number): string => String(n).padStart(2, '0');
const pad3 = (n: number): string => String(n).padStart(3, '0');

function readMvhdAt(v: DataView, boxStart: number, boxEnd: number): number | null {
  if (boxStart + 32 > boxEnd) return null;
  const ver = v.getUint8(boxStart + 8);
  const flags = (v.getUint8(boxStart + 9) << 16) | (v.getUint8(boxStart + 10) << 8) | v.getUint8(boxStart + 11);
  if ((ver !== 0 && ver !== 1) || flags !== 0) return null;
  let creationSec = 0;
  let timescale = 0;
  if (ver === 0) {
    creationSec = v.getUint32(boxStart + 12);
    timescale = v.getUint32(boxStart + 20);
  } else {
    if (boxStart + 44 > boxEnd) return null;
    creationSec = v.getUint32(boxStart + 12) * 4294967296 + v.getUint32(boxStart + 16);
    timescale = v.getUint32(boxStart + 28);
  }
  if (timescale <= 0 || creationSec <= MP4_EPOCH_OFFSET_SEC) return null;
  const ms = (creationSec - MP4_EPOCH_OFFSET_SEC) * 1000;
  return ms >= MIN_VALID_MS && ms <= MAX_VALID_MS ? ms : null;
}

/** Extract UTC creation timestamp (ms) from an MP4 `moov > mvhd` header inside `buf`. */
export function parseMvhdCreationMs(buf: ArrayBuffer): number | null {
  const v = new DataView(buf);
  const len = buf.byteLength;

  // Pass 1: clean ISO BMFF box walk from aligned start (faststart / head chunk).
  let off = 0;
  while (off + 8 <= len) {
    const size32 = v.getUint32(off);
    const tag = v.getUint32(off + 4);
    let size = size32;
    let hdr = 8;
    if (size32 === 1) {
      if (off + 16 > len) break;
      size = v.getUint32(off + 8) * 4294967296 + v.getUint32(off + 12);
      hdr = 16;
    } else if (size32 === 0) {
      size = len - off;
    }
    if (size < hdr || off + size > len && tag !== MOOV_TAG) break;
    if (tag === MOOV_TAG) {
      let c = off + hdr;
      const moovEnd = Math.min(len, off + size);
      while (c + 8 <= moovEnd) {
        const cs = v.getUint32(c);
        const ct = v.getUint32(c + 4);
        if (cs < 8 || c + cs > moovEnd) break;
        if (ct === MVHD_TAG) {
          const hit = readMvhdAt(v, c, c + cs);
          if (hit != null) return hit;
        }
        c += cs;
      }
      break;
    }
    off += size;
  }

  // Pass 2: signature scan (handles unaligned tail slices when `moov` sits after `mdat`).
  for (let i = 4; i + 32 <= len; i++) {
    if (v.getUint32(i) !== MVHD_TAG) continue;
    const boxSize = v.getUint32(i - 4);
    if (boxSize < 100 || boxSize > 256 || i - 4 + boxSize > len) continue;
    const hit = readMvhdAt(v, i - 4, i - 4 + boxSize);
    if (hit != null) return hit;
  }
  return null;
}

/** Read only the head/tail slices of an MP4/MOV Blob (<5ms, zero copy) to extract creation UTC ms. */
export async function extractMp4CreationMs(blob: Blob): Promise<number | null> {
  try {
    const head = await blob.slice(0, 262_144).arrayBuffer();
    const h = parseMvhdCreationMs(head);
    if (h != null) return h;
    if (blob.size > 262_144) {
      const tail = await blob.slice(Math.max(0, blob.size - 524_288)).arrayBuffer();
      return parseMvhdCreationMs(tail);
    }
  } catch { /* ignore unreadable slice */ }
  return null;
}

const utcParts = (ms: number): { ymd: string; hms: string; msec: string } => {
  const d = new Date(ms);
  return {
    ymd: `${d.getUTCFullYear()}${pad2(d.getUTCMonth() + 1)}${pad2(d.getUTCDate())}`,
    hms: `${pad2(d.getUTCHours())}${pad2(d.getUTCMinutes())}${pad2(d.getUTCSeconds())}`,
    msec: pad3(d.getUTCMilliseconds()),
  };
};

/** Format a Pixel-style UTC timestamp filename (`PXL_YYYYMMDD_HHMMSSmmm.mp4`). */
export function formatPixelFilename(utcMs: number): string {
  const { ymd, hms, msec } = utcParts(utcMs);
  return `PXL_${ymd}_${hms}${msec}.mp4`;
}

/**
 * When Android Photo Picker hides the real `PXL_...mp4` name behind a numeric id (`100000xxxx.mp4`),
 * derive a timestamp-based display name + Google Drive `type:video` search query from the MP4 timestamp.
 */
export function inferPixelVideoLink(rawName: string, endMs: number, durationSec: number): { fileName: string; driveQuery?: string } {
  if (!/^\d{6,}\.\w+$/.test(rawName)) return { fileName: rawName };
  const startMs = Math.max(0, endMs - Math.max(0, durationSec) * 1000);
  const p0 = utcParts(startMs - 2000);
  const p1 = utcParts(endMs + 2000);
  let commonHms = '';
  for (let i = 0; i < 6 && p0.hms[i] === p1.hms[i]; i++) commonHms += p0.hms[i];
  const sameDay = p0.ymd === p1.ymd;
  const disp = utcParts(startMs);
  const fileName = `PXL_${disp.ymd}_${disp.hms.slice(0, 4)}xx.mp4`;
  const driveQuery = sameDay
    ? `PXL ${p0.ymd}${commonHms.length >= 2 ? ` ${commonHms.slice(0, 4)}` : ''} type:video`
    : `PXL ${p1.ymd} type:video`;
  return { fileName, driveQuery };
}

/** Sort device video entries newest-first (fileMs desc, tie-broken reverse by filename). */
export function sortDeviceVideosNewestFirst<T extends { fileMs: number; fileName: string }>(list: readonly T[]): T[] {
  return [...list].sort((a, b) => (b.fileMs - a.fileMs) || b.fileName.localeCompare(a.fileName));
}
