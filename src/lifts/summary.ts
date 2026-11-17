import { isWorking, sessionStats, type ExOf, type SessionStats } from './calc';
import { fmtNum, toDisplay, type Unit } from './settings';
import type { Exercise, LiftSession, LiftSet, SessionEx } from './types';
import type { PRHit } from './calc';

const working = (e: SessionEx): LiftSet[] => e.sets.filter(isWorking);

const sameSet = (a: LiftSet, b: LiftSet): boolean => (a.w ?? 0) === (b.w ?? 0) && a.r === b.r;

/** One set as text: "225×5", "BW×8", "+45×8", "45s". */
export function setText(s: LiftSet, ex: Exercise | undefined, unit: Unit): string {
  const w = toDisplay(s.w ?? 0, unit);
  if (ex?.type === 'time') return `${s.r ?? 0}s${w ? ` @ ${fmtNum(w)}` : ''}`;
  if (ex?.type === 'bodyweight') return `${w ? '+' + fmtNum(w) : 'BW'}×${s.r ?? 0}`;
  return `${fmtNum(w)}×${s.r ?? 0}`;
}

/** "3×5 @ 225" when every working set matches, otherwise each set in order. */
export function condenseSets(e: SessionEx, ex: Exercise | undefined, unit: Unit): string {
  const w = working(e);
  if (!w.length) return '';
  const u = unit;
  if (w.every((s) => sameSet(s, w[0]))) {
    const load = toDisplay(w[0].w ?? 0, unit);
    if (ex?.type === 'time') return `${w.length}×${w[0].r}s${load ? ` @ ${fmtNum(load)} ${u}` : ''}`;
    if (ex?.type === 'bodyweight') return `${w.length}×${w[0].r}${load ? ` @ +${fmtNum(load)} ${u}` : ' BW'}`;
    return `${w.length}×${w[0].r} @ ${fmtNum(load)} ${u}`;
  }
  const list = w.map((s) => setText(s, ex, unit)).join(' · ');
  return ex?.type === 'weight' || !ex ? `${list} ${u}` : list;
}

/** Short form for the share card: uniform, a short list, or "5 sets · top 225×5". */
export function shortLine(e: SessionEx, ex: Exercise | undefined, unit: Unit): string {
  const full = condenseSets(e, ex, unit);
  if (full.length <= 26) return full;
  const top = topSet(e, ex);
  return top ? `${working(e).length} sets · top ${setText(top, ex, unit)}` : full;
}

export function topSet(e: SessionEx, ex: Exercise | undefined): LiftSet | null {
  const w = working(e);
  if (!w.length) return null;
  if (ex?.type === 'time') return w.reduce((a, b) => ((b.r ?? 0) > (a.r ?? 0) ? b : a));
  return w.reduce((a, b) => ((b.w ?? 0) > (a.w ?? 0) || ((b.w ?? 0) === (a.w ?? 0) && (b.r ?? 0) > (a.r ?? 0)) ? b : a));
}

export function fmtVolume(kg: number, unit: Unit): string {
  return `${Math.round(toDisplay(kg, unit)).toLocaleString('en-US')} ${unit}`;
}

export const fmtDuration = (sec: number): string => {
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m} min`;
};

export interface SessionPRs {
  /** Count of set-level PRs in the session. */
  count: number;
  /** Best PR per exercise uid, for lines like "★ 5RM". */
  byEx: Map<string, PRHit>;
}

export function sessionPRs(s: LiftSession, prs: Map<string, PRHit>): SessionPRs {
  const byEx = new Map<string, PRHit>();
  let count = 0;
  for (const e of s.exs) {
    for (const set of e.sets) {
      const h = prs.get(set.id);
      if (!h) continue;
      count++;
      const cur = byEx.get(e.uid);
      if (!cur || h.kg > cur.kg) byEx.set(e.uid, h);
    }
  }
  return { count, byEx };
}

export interface DescribeOpts {
  unit: Unit;
  exOf: ExOf;
  prs: Map<string, PRHit>;
  avgHr?: number;
  maxHr?: number;
  kcal?: number | null;
}

/** The Intervals.icu activity description: a compact, human-readable lift log. */
export function intervalsDescription(s: LiftSession, o: DescribeOpts): string {
  const st: SessionStats = sessionStats(s, o.exOf);
  const pr = sessionPRs(s, o.prs);
  const head = [
    `${Math.round(st.durationSec / 60)} min`,
    `${st.sets} sets`,
    fmtVolume(st.volumeKg, o.unit),
    pr.count ? `${pr.count} PR${pr.count > 1 ? 's' : ''}` : '',
  ].filter(Boolean).join(' · ');
  const lines = s.exs
    .map((e) => {
      const ex = o.exOf(e.exId);
      const line = condenseSets(e, ex, o.unit);
      if (!line) return '';
      const h = pr.byEx.get(e.uid);
      return `${ex?.name ?? 'Exercise'}: ${line}${h ? `  ★ ${h.n}RM PR` : ''}`;
    })
    .filter(Boolean);
  const tail = [
    o.avgHr ? `avg HR ${Math.round(o.avgHr)}` : '',
    o.maxHr ? `max HR ${Math.round(o.maxHr)}` : '',
    o.kcal ? `~${o.kcal} kcal` : '',
    s.rpe ? `RPE ${s.rpe}/10` : '',
  ].filter(Boolean).join(' · ');
  return [
    `Armory Fit · ${s.name}`,
    head,
    '',
    ...lines,
    ...(tail ? ['', tail] : []),
    ...(s.note ? ['', s.note] : []),
  ].join('\n');
}
