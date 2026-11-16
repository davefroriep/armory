/** Tiny dependency-free SVG charts (dark theme). */

const BLUE = '#1e9bff';
const GRID = '#232a33';
const MUT = '#8a94a3';

export interface Pt {
  x: number;
  y: number;
  hi?: boolean;
}

/** Line chart with points; `hi` points get a gold ring (PRs). */
export function lineChart(pts: Pt[], o: { w?: number; h?: number; fmtY?: (v: number) => string; fmtX?: (v: number) => string } = {}): string {
  const w = o.w ?? 340;
  const h = o.h ?? 170;
  if (pts.length < 2) return `<div class="empty">Log this lift in two or more sessions to see a trend.</div>`;
  const padL = 38;
  const padR = 10;
  const padT = 12;
  const padB = 24;
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  let y0 = Math.min(...ys);
  let y1 = Math.max(...ys);
  const span = Math.max(y1 - y0, y1 * 0.06, 1);
  y0 = y0 - span * 0.15;
  y1 = y1 + span * 0.15;
  const X = (v: number): number => padL + ((v - x0) / Math.max(1, x1 - x0)) * (w - padL - padR);
  const Y = (v: number): number => padT + (1 - (v - y0) / (y1 - y0)) * (h - padT - padB);
  const fy = o.fmtY ?? ((v) => String(Math.round(v)));
  const fx = o.fmtX ?? ((v) => new Date(v).toLocaleDateString([], { month: 'short', day: 'numeric' }));
  const ticks = [0, 0.5, 1].map((f) => y0 + (y1 - y0) * f);
  const grid = ticks.map((t) => `<line x1="${padL}" x2="${w - padR}" y1="${Y(t)}" y2="${Y(t)}" stroke="${GRID}" stroke-width="1"/><text x="${padL - 6}" y="${Y(t) + 4}" text-anchor="end" fill="${MUT}" font-size="10">${fy(t)}</text>`).join('');
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const area = `${path} L${X(x1).toFixed(1)},${h - padB} L${X(x0).toFixed(1)},${h - padB} Z`;
  const dots = pts.map((p) => `<circle cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="${p.hi ? 4.5 : 3}" fill="${p.hi ? '#fbbf24' : BLUE}" stroke="#0b0e12" stroke-width="1.5"/>`).join('');
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img">
    ${grid}
    <path d="${area}" fill="${BLUE}" fill-opacity=".10"/>
    <path d="${path}" fill="none" stroke="${BLUE}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}
    <text x="${padL}" y="${h - 6}" fill="${MUT}" font-size="10">${fx(x0)}</text>
    <text x="${w - padR}" y="${h - 6}" fill="${MUT}" font-size="10" text-anchor="end">${fx(x1)}</text></svg>`;
}

/** Vertical bars with labels under each. */
export function barChart(bars: { label: string; v: number; cur?: boolean }[], o: { w?: number; h?: number; fmt?: (v: number) => string } = {}): string {
  const w = o.w ?? 340;
  const h = o.h ?? 130;
  const max = Math.max(1, ...bars.map((b) => b.v));
  const padB = 20;
  const padT = 16;
  const bw = (w - 8) / bars.length;
  const fmt = o.fmt ?? String;
  const out = bars.map((b, i) => {
    const bh = (b.v / max) * (h - padB - padT);
    const x = 4 + i * bw + bw * 0.18;
    const y = h - padB - bh;
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.64).toFixed(1)}" height="${Math.max(bh, b.v ? 2 : 0).toFixed(1)}" rx="3" fill="${b.cur ? BLUE : '#2b3a4d'}"/>
      ${b.v ? `<text x="${(x + bw * 0.32).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" fill="${b.cur ? '#fff' : MUT}" font-size="9.5">${fmt(b.v)}</text>` : ''}
      <text x="${(x + bw * 0.32).toFixed(1)}" y="${h - 6}" text-anchor="middle" fill="${MUT}" font-size="9">${b.label}</text>`;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img">${out}</svg>`;
}
