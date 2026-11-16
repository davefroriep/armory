import { MUSCLES, type Exercise, type LiftSession, type LiftSet, type Muscle, type SessionEx } from './types';
import { PRIMARY_WEIGHT, SECONDARY_WEIGHT } from './muscles';

export type ExOf = (id: string) => Exercise | undefined;

/** Highest rep count tracked for rep records / PR detection. */
export const MAX_RM = 12;

// ---------- sets ----------
/** A logged working set (warm-ups never count toward volume, records or PRs). */
export const isWorking = (s: LiftSet): boolean => s.done && s.kind !== 'w';

/** Total load moved in kg: added load for weight/time lifts, bodyweight + added for bodyweight lifts. */
export function totalLoadKg(s: LiftSet, ex: Exercise | undefined, bwKg: number | null): number {
  const w = s.w ?? 0;
  return ex?.type === 'bodyweight' ? (bwKg ?? 0) + w : w;
}

export function setVolumeKg(s: LiftSet, ex: Exercise | undefined, bwKg: number | null): number {
  if (!isWorking(s) || !ex || ex.type === 'time') return 0;
  return totalLoadKg(s, ex, bwKg) * (s.r ?? 0);
}

/** Estimated 1RM (Epley). Meaningful up to ~12 reps; null beyond that or without data. */
export function e1rm(loadKg: number, reps: number): number | null {
  if (!(loadKg > 0) || !(reps >= 1) || reps > MAX_RM) return null;
  return reps === 1 ? loadKg : loadKg * (1 + reps / 30);
}

// ---------- session stats ----------
export interface SessionStats {
  exercises: number;
  sets: number;
  reps: number;
  volumeKg: number;
  durationSec: number;
}

export function sessionStats(s: LiftSession, exOf: ExOf): SessionStats {
  let sets = 0;
  let reps = 0;
  let volumeKg = 0;
  let exercises = 0;
  for (const e of s.exs) {
    const ex = exOf(e.exId);
    let any = false;
    for (const set of e.sets) {
      if (!isWorking(set)) continue;
      any = true;
      sets++;
      if (ex?.type !== 'time') reps += set.r ?? 0;
      volumeKg += setVolumeKg(set, ex, s.bwKg);
    }
    if (any) exercises++;
  }
  const end = s.endMs ?? Date.now();
  return { exercises, sets, reps, volumeKg, durationSec: Math.max(0, Math.round((end - s.startMs) / 1000)) };
}

export const doneSessions = (all: Iterable<LiftSession>): LiftSession[] =>
  [...all].filter((s) => s.status === 'done').sort((a, b) => a.startMs - b.startMs);

// ---------- history of one exercise ----------
export interface ExSession {
  s: LiftSession;
  e: SessionEx;
}

/** Every finished session containing the exercise, oldest first (first instance per session). */
export function exerciseSessions(sessions: LiftSession[], exId: string): ExSession[] {
  const out: ExSession[] = [];
  for (const s of sessions) {
    const e = s.exs.find((x) => x.exId === exId && x.sets.some(isWorking));
    if (e) out.push({ s, e });
  }
  return out.sort((a, b) => a.s.startMs - b.s.startMs);
}

/** The most recent finished performance before `beforeMs` (for the PREVIOUS column and auto-fill). */
export function previousPerformance(sessions: Iterable<LiftSession>, exId: string, beforeMs = Infinity, excludeId?: string): SessionEx | null {
  let best: ExSession | null = null;
  for (const s of sessions) {
    if (s.status !== 'done' || s.id === excludeId || s.startMs >= beforeMs) continue;
    const e = s.exs.find((x) => x.exId === exId && x.sets.some((z) => z.done));
    if (e && (!best || s.startMs > best.s.startMs)) best = { s, e };
  }
  return best ? best.e : null;
}

// ---------- rep records ----------
export interface RepBest {
  kg: number;
  reps: number;
  ms: number;
  sessionId: string;
  setId: string;
  vidId?: string;
}

/** Heaviest load for at least n reps, for n = 1..MAX_RM. Ties keep the earliest. */
export function repBests(hist: ExSession[]): (RepBest | null)[] {
  const best: (RepBest | null)[] = Array(MAX_RM + 1).fill(null);
  for (const { s, e } of hist) {
    for (const set of e.sets) {
      if (!isWorking(set) || !set.r || set.r < 1) continue;
      const kg = set.w ?? 0;
      const vidId = set.vids?.[0];
      for (let n = 1; n <= Math.min(set.r, MAX_RM); n++) {
        const b = best[n];
        if (!b || kg > b.kg + 0.005) {
          best[n] = { kg, reps: set.r, ms: s.startMs, sessionId: s.id, setId: set.id, ...(vidId ? { vidId } : {}) };
        } else if (Math.abs(kg - b.kg) <= 0.005 && !b.vidId && vidId) {
          b.vidId = vidId;
        }
      }
    }
  }
  return best;
}

export interface PRHit {
  /** Rep count the record is for. */
  n: number;
  kg: number;
}

/**
 * Chronological PR pass over all finished sessions. A set is a PR when its load beats every earlier
 * set of >= that many reps (and the exercise has history; the very first time is not flagged).
 * Derived, never stored, so edits and imports stay consistent.
 */
export function computePRs(sessions: LiftSession[], exOf: ExOf): Map<string, PRHit> {
  const bests = new Map<string, number[]>();
  const hits = new Map<string, PRHit>();
  for (const s of [...sessions].sort((a, b) => a.startMs - b.startMs)) {
    if (s.status !== 'done') continue;
    const hadHistory = new Set(bests.keys());
    for (const e of s.exs) {
      const ex = exOf(e.exId);
      if (ex?.type === 'time') continue;
      let b = bests.get(e.exId);
      if (!b) {
        b = Array(MAX_RM + 1).fill(-1);
        bests.set(e.exId, b);
      }
      for (const set of e.sets) {
        if (!isWorking(set) || !set.r || set.r < 1) continue;
        const kg = set.w ?? 0;
        const n = Math.min(set.r, MAX_RM);
        if (hadHistory.has(e.exId) && set.r <= MAX_RM && kg > b[n] + 0.005 && (kg > 0 || ex?.type === 'bodyweight')) hits.set(set.id, { n: set.r, kg });
        for (let i = 1; i <= n; i++) if (kg > b[i]) b[i] = kg;
      }
    }
  }
  return hits;
}

// ---------- muscle volume ----------
/** Fractional hard sets per muscle (primary 1.0, secondary 0.5) for sessions starting in [from, to). */
export function muscleSets(sessions: LiftSession[], exOf: ExOf, from: number, to: number): Record<Muscle, number> {
  const out = Object.fromEntries(MUSCLES.map((m) => [m, 0])) as Record<Muscle, number>;
  for (const s of sessions) {
    if (s.status !== 'done' || s.startMs < from || s.startMs >= to) continue;
    for (const e of s.exs) {
      const ex = exOf(e.exId);
      if (!ex) continue;
      const n = e.sets.filter(isWorking).length;
      if (!n) continue;
      for (const m of ex.primary) out[m] += n * PRIMARY_WEIGHT;
      for (const m of ex.secondary) if (!ex.primary.includes(m)) out[m] += n * SECONDARY_WEIGHT;
    }
  }
  return out;
}

// ---------- dates ----------
export const DAY_MS = 86_400_000;

/** Local Monday 00:00 of the week containing `ms`. */
export function weekStart(ms: number): number {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  const dow = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - dow);
  return d.getTime();
}

export const addWeeks = (ms: number, n: number): number => {
  const d = new Date(ms);
  d.setDate(d.getDate() + n * 7);
  return d.getTime();
};

export const dayKey = (ms: number): string => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

// ---------- e1RM trend ----------
export interface TrendPoint {
  ms: number;
  e1rm: number;
  topKg: number;
  topReps: number;
}

/** Best estimated 1RM per session for one exercise. */
export function e1rmTrend(hist: ExSession[], ex: Exercise | undefined): TrendPoint[] {
  const out: TrendPoint[] = [];
  for (const { s, e } of hist) {
    let best: TrendPoint | null = null;
    for (const set of e.sets) {
      if (!isWorking(set) || !set.r) continue;
      const load = totalLoadKg(set, ex, s.bwKg);
      const v = e1rm(load, set.r);
      if (v != null && (!best || v > best.e1rm)) best = { ms: s.startMs, e1rm: v, topKg: set.w ?? 0, topReps: set.r };
    }
    if (best) out.push(best);
  }
  return out;
}
