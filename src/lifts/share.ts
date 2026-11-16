import { isNative } from '../native/service';
import { sessionStats } from './calc';
import { fmtDuration, fmtVolume, sessionPRs, shortLine } from './summary';
import { liftSettings } from './settings';
import { lifts } from './store';
import type { LiftSession } from './types';

const W = 1080;
const H = 1350;
const BLUE = '#1e9bff';
const MUT = '#8a94a3';

function trunc(ctx: CanvasRenderingContext2D, text: string, max: number): string {
  if (ctx.measureText(text).width <= max) return text;
  let t = text;
  while (t.length > 1 && ctx.measureText(t + '…').width > max) t = t.slice(0, -1);
  return t + '…';
}

/**
 * The shareable "lift card": one condensed line per exercise, the headline numbers, PR stars. No photos,
 * no profile, nothing social: just a clean PNG of the day.
 */
export async function renderShareCard(s: LiftSession, hr?: { avg: number; max: number }): Promise<Blob> {
  const c = document.createElement('canvas');
  c.width = W;
  c.height = H;
  const ctx = c.getContext('2d')!;
  const unit = liftSettings().unit;
  const exOf = lifts.exOrStub;
  const st = sessionStats(s, exOf);
  const prs = sessionPRs(s, lifts.prs());
  const font = (w: number, px: number): string => `${w} ${px}px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;

  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, '#0d1722');
  g.addColorStop(0.45, '#05080c');
  g.addColorStop(1, '#000');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = BLUE;
  ctx.fillRect(0, 0, W, 10);

  const pad = 72;
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#fff';
  ctx.font = font(900, 40);
  ctx.fillText('ARMORY', pad, 112);
  const aw = ctx.measureText('ARMORY').width;
  ctx.fillStyle = BLUE;
  ctx.fillText('FIT', pad + aw + 12, 112);
  ctx.fillStyle = MUT;
  ctx.font = font(600, 34);
  ctx.textAlign = 'right';
  ctx.fillText(new Date(s.startMs).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }), W - pad, 110);
  ctx.textAlign = 'left';

  ctx.fillStyle = '#fff';
  ctx.font = font(800, 92);
  ctx.fillText(trunc(ctx, s.name, W - pad * 2), pad, 250);

  // stat tiles
  const tiles: [string, string][] = [
    ['SETS', String(st.sets)],
    ['VOLUME', fmtVolume(st.volumeKg, unit).replace(` ${unit}`, '')],
    ['TIME', fmtDuration(st.durationSec).replace(' min', 'm')],
    [prs.count ? 'PRs' : hr?.avg ? 'AVG HR' : 'EXERCISES', prs.count ? String(prs.count) : hr?.avg ? String(Math.round(hr.avg)) : String(st.exercises)],
  ];
  const tw = (W - pad * 2 - 3 * 20) / 4;
  tiles.forEach(([label, val], i) => {
    const x = pad + i * (tw + 20);
    ctx.fillStyle = '#111a24';
    ctx.beginPath();
    ctx.roundRect(x, 300, tw, 150, 24);
    ctx.fill();
    ctx.fillStyle = MUT;
    ctx.font = font(800, 24);
    ctx.fillText(label, x + 24, 345);
    ctx.fillStyle = label === 'PRs' ? '#fbbf24' : '#fff';
    ctx.font = font(800, val.length > 6 ? 46 : 60);
    ctx.fillText(val, x + 24, 418);
  });
  ctx.fillStyle = MUT;
  ctx.font = font(700, 26);
  ctx.fillText(unit === 'lb' ? 'VOLUME IN LB' : 'VOLUME IN KG', pad, 492);

  // exercise lines
  const exs = s.exs.filter((e) => e.sets.some((x) => x.done && x.kind !== 'w'));
  const top = 530;
  const bottom = hr?.avg ? 1210 : 1250;
  const maxRows = 13;
  const shown = exs.length > maxRows ? exs.slice(0, maxRows - 1) : exs;
  const more = exs.length - shown.length;
  const rows = shown.length + (more ? 1 : 0);
  const rh = Math.min(76, Math.max(46, Math.floor((bottom - top) / Math.max(rows, 1))));
  const nameFont = Math.round(rh * 0.42);
  shown.forEach((e, i) => {
    const y = top + i * rh;
    ctx.fillStyle = i % 2 ? 'rgba(255,255,255,0.025)' : 'rgba(255,255,255,0.05)';
    ctx.beginPath();
    ctx.roundRect(pad - 16, y, W - pad * 2 + 32, rh - 6, 14);
    ctx.fill();
    const ex = exOf(e.exId);
    const line = shortLine(e, ex, unit);
    const pr = prs.byEx.get(e.uid);
    ctx.font = font(700, nameFont);
    ctx.textAlign = 'right';
    ctx.fillStyle = BLUE;
    const rightX = W - pad;
    ctx.fillText(line, rightX, y + rh / 2 + nameFont * 0.32);
    const lw = ctx.measureText(line).width;
    ctx.textAlign = 'left';
    ctx.fillStyle = '#fff';
    ctx.font = font(700, nameFont);
    const nameMax = W - pad * 2 - lw - 40 - (pr ? nameFont * 1.3 : 0);
    const nm = trunc(ctx, ex.name, nameMax);
    ctx.fillText(nm, pad, y + rh / 2 + nameFont * 0.32);
    if (pr) {
      ctx.fillStyle = '#fbbf24';
      ctx.fillText('★', pad + ctx.measureText(nm).width + 14, y + rh / 2 + nameFont * 0.32);
    }
  });
  if (more) {
    ctx.fillStyle = MUT;
    ctx.font = font(700, nameFont);
    ctx.fillText(`+ ${more} more exercise${more > 1 ? 's' : ''}`, pad, top + shown.length * rh + rh / 2 + nameFont * 0.32);
  }

  if (hr?.avg) {
    ctx.fillStyle = MUT;
    ctx.font = font(700, 34);
    ctx.fillText(`♥ avg ${Math.round(hr.avg)} · max ${Math.round(hr.max)} bpm`, pad, 1262);
  }
  ctx.fillStyle = '#3b4452';
  ctx.font = font(700, 28);
  ctx.textAlign = 'right';
  ctx.fillText('logged with Armory Fit', W - pad, 1296);
  ctx.textAlign = 'left';

  return new Promise((resolve, reject) => c.toBlob((b) => (b ? resolve(b) : reject(new Error('Could not render card'))), 'image/png'));
}

const toBase64 = (blob: Blob): Promise<string> =>
  new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(',')[1] ?? '');
    r.onerror = () => rej(r.error);
    r.readAsDataURL(blob);
  });

/** Native share sheet on Android (Capacitor), Web Share with files in the browser, else a download. */
export async function shareImage(blob: Blob, filename: string, title: string): Promise<'shared' | 'saved'> {
  if (isNative()) {
    const [{ Filesystem, Directory }, { Share }] = await Promise.all([import('@capacitor/filesystem'), import('@capacitor/share')]);
    const w = await Filesystem.writeFile({ path: filename, data: await toBase64(blob), directory: Directory.Cache });
    await Share.share({ title, files: [w.uri] });
    return 'shared';
  }
  const file = new File([blob], filename, { type: 'image/png' });
  if (navigator.canShare?.({ files: [file] })) {
    await navigator.share({ files: [file], title });
    return 'shared';
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  return 'saved';
}
