import { resolvePrescription } from './prescriptions';
import { uid, type Exercise, type ExerciseRx, type LiftSession, type LiftSet, type Routine, type SessionEx, type SetKind, type VideoMeta } from './types';

export const newSet = (kind: SetKind = 'n', w: number | null = null, r: number | null = null, auto = false): LiftSet => ({
  id: uid(), w, r, done: false, kind, wAuto: auto && w != null, rAuto: auto && r != null,
});

const working = (sets: LiftSet[]): LiftSet[] => sets.filter((s) => s.kind !== 'w' && s.done);

/**
 * The set from last time that lines up with set `idx` (warm-ups pair with warm-ups, working sets with
 * working sets, by order). Used for the PREVIOUS column.
 */
export function prevSetFor(prev: SessionEx | null, se: SessionEx, idx: number): LiftSet | null {
  if (!prev) return null;
  const me = se.sets[idx];
  const before = se.sets.slice(0, idx).filter((s) => (s.kind === 'w') === (me.kind === 'w')).length;
  const pool = me.kind === 'w' ? prev.sets.filter((s) => s.kind === 'w' && s.done) : working(prev.sets);
  return pool[before] ?? null;
}

/** Suggestion for a not-yet-logged set: last time's matching set, else last time's final working set. */
function suggestionFor(prev: SessionEx | null, se: SessionEx, idx: number): LiftSet | null {
  const exact = prevSetFor(prev, se, idx);
  if (exact) return exact;
  if (!prev || se.sets[idx].kind === 'w') return null;
  const pool = working(prev.sets);
  return pool[pool.length - 1] ?? null;
}

/** Fills empty, not-yet-logged sets with last time's numbers (flagged as suggestions). */
export function prefill(se: SessionEx, prev: SessionEx | null): void {
  se.sets.forEach((s, i) => {
    if (s.done) return;
    const p = suggestionFor(prev, se, i);
    if (!p) return;
    if (s.w == null) { s.w = p.w; s.wAuto = p.w != null; }
    if (s.r == null) { s.r = p.r; s.rAuto = p.r != null; }
  });
}

export interface BuildExerciseOpts {
  rx?: ExerciseRx;
  ex?: Exercise;
  base1RmKg?: number | null;
  unit?: 'lb' | 'kg';
}

export function buildExercise(
  exId: string,
  nSets: number,
  prev: SessionEx | null,
  ss = false,
  slot?: number,
  opts?: BuildExerciseOpts,
): SessionEx {
  const rx = opts?.rx;
  const resolved = rx
    ? resolvePrescription(
        opts.ex ?? { id: exId, name: exId, type: exId === 'triceps-isometric-hold' ? 'time' : 'weight', equip: 'other', primary: [], secondary: [] },
        { rx, sets: nSets, base1RmKg: opts.base1RmKg, unit: opts.unit },
      )
    : null;
  const sets = resolved?.prescribedSets?.length
    ? resolved.prescribedSets.map((ps) => newSet(ps.kind ?? 'n', ps.wKg, ps.r, true))
    : Array.from({ length: Math.max(1, nSets) }, () => newSet());
  const se: SessionEx = { uid: uid(), exId, sets, ...(ss ? { ss } : {}), ...(slot != null ? { slot } : {}), ...(rx ? { rx } : {}) };
  prefill(se, prev);
  if (exId === 'triceps-isometric-hold' && !prev) {
    for (const s of se.sets) if (s.r == null) { s.r = 45; s.rAuto = true; }
  }
  return se;
}

/**
 * Boostcamp-style forward fill: values entered (or accepted) in set `from` flow into the later sets that
 * are still untouched suggestions. Warm-ups neither push nor receive.
 */
export function forward(se: SessionEx, from: number, fields: { w?: boolean; r?: boolean }): void {
  const src = se.sets[from];
  if (!src || src.kind === 'w') return;
  for (let j = from + 1; j < se.sets.length; j++) {
    const t = se.sets[j];
    if (t.done || t.kind === 'w') continue;
    if (fields.w && (t.wAuto || t.w == null)) { t.w = src.w; t.wAuto = src.w != null; }
    if (fields.r && (t.rAuto || t.r == null)) { t.r = src.r; t.rAuto = src.r != null; }
  }
}

export function editWeight(se: SessionEx, idx: number, w: number | null): void {
  const s = se.sets[idx];
  s.w = w;
  s.wAuto = false;
  forward(se, idx, { w: true });
}

export function editReps(se: SessionEx, idx: number, r: number | null): void {
  const s = se.sets[idx];
  s.r = r;
  s.rAuto = false;
  forward(se, idx, { r: true });
}

/** Can this set be logged as it stands? weight lifts need both numbers, bodyweight / time need reps. */
export function canLog(s: LiftSet, ex: Exercise | undefined): boolean {
  if (!s.r || s.r < 1) return false;
  if (!ex || ex.type === 'weight') return s.w != null && s.w >= 0;
  return true;
}

/** Toggle logged state. Returns the new state, or null when the set is incomplete. */
export function toggleDone(se: SessionEx, idx: number, ex: Exercise | undefined, now = Date.now()): boolean | null {
  const s = se.sets[idx];
  if (s.done) {
    s.done = false;
    delete s.at;
    return false;
  }
  if (!canLog(s, ex)) return null;
  if (s.w == null) s.w = 0;
  s.done = true;
  s.at = now;
  s.wAuto = false;
  s.rAuto = false;
  forward(se, idx, { w: true, r: true });
  return true;
}

/** + Set: a new set that copies the last set (as a suggestion), or last time's numbers if the card is empty. */
export function addSet(se: SessionEx, prev: SessionEx | null): LiftSet {
  const last = [...se.sets].reverse().find((s) => s.kind !== 'w') ?? se.sets[se.sets.length - 1];
  const s = newSet('n');
  if (last) {
    s.w = last.w; s.r = last.r;
    s.wAuto = last.w != null; s.rAuto = last.r != null;
    se.sets.push(s);
  } else {
    se.sets.push(s);
    prefill(se, prev);
  }
  return s;
}

/** - Set: drops the last set. Returns it (so the caller can offer Undo), or null if only one is left. */
export function removeLastSet(se: SessionEx): LiftSet | null {
  if (se.sets.length <= 1) return null;
  return se.sets.pop() ?? null;
}

export function deleteSet(se: SessionEx, idx: number): void {
  if (se.sets.length > 1) se.sets.splice(idx, 1);
}

/** Replace the exercise on a card: unlogged shape is kept, suggestions come from the new lift's history. */
export function swapExercise(se: SessionEx, exId: string, prev: SessionEx | null): void {
  const n = se.sets.length;
  se.exId = exId;
  se.sets = Array.from({ length: n }, () => newSet());
  delete se.note;
  delete se.rest;
  delete se.restAuto;
  prefill(se, prev);
}

export const hasLogged = (se: SessionEx): boolean => se.sets.some((s) => s.done);

export const hasVideo = (se: SessionEx): boolean => se.sets.some((s) => (s.vids?.length ?? 0) > 0);

export const hasSessionActivity = (s: LiftSession): boolean => s.exs.some((e) => hasLogged(e) || hasVideo(e));

export const isSessionStarted = (s: LiftSession): boolean => !!s.started || hasSessionActivity(s);

export const isSessionPaused = (s: LiftSession): boolean => !!s.started && s.status === 'active' && s.pausedAt != null;

export function elapsedSessionSec(s: LiftSession, now = Date.now()): number {
  if (!isSessionStarted(s)) return 0;
  const end = s.endMs ?? s.pausedAt ?? now;
  return Math.max(0, (end - s.startMs - (s.pausedMs ?? 0)) / 1000);
}

/** Starts an unstarted workout clock from zero (`startMs = now`). */
export function startSessionClock(s: LiftSession, now = Date.now(), manual = false): boolean {
  if (manual) s.manualStart = true;
  if (s.started) return false;
  s.startMs = now;
  s.started = true;
  delete s.pausedAt;
  delete s.pausedMs;
  return true;
}

export function pauseSessionClock(s: LiftSession, now = Date.now()): boolean {
  if (!isSessionStarted(s) || s.status !== 'active' || s.pausedAt != null) return false;
  s.pausedAt = now;
  return true;
}

export function resumeSessionClock(s: LiftSession, now = Date.now()): boolean {
  if (s.pausedAt == null) return false;
  s.pausedMs = (s.pausedMs ?? 0) + Math.max(0, now - s.pausedAt);
  s.pausedAt = null;
  return true;
}

/** Resets the workout clock back to unstarted preview (`startMs = now`) when auto-started by check only and nothing remains logged or video-linked. */
export function resetSessionClock(s: LiftSession, now = Date.now()): boolean {
  if (hasSessionActivity(s) || s.manualStart) return false;
  s.started = false;
  s.startMs = now;
  delete s.pausedAt;
  delete s.pausedMs;
  return true;
}

export interface SupersetSpan {
  start: number;
  end: number;
  ssIdx: number | null;
}

/** Group consecutive exercises linked by `ss: true`; ssIdx increments 0, 1, 2… across superset groups. */
export function supersetGroups(exs: { ss?: boolean }[]): SupersetSpan[] {
  const out: SupersetSpan[] = [];
  let ssCount = 0;
  for (let i = 0; i < exs.length; ) {
    let j = i;
    while (exs[j].ss && j + 1 < exs.length) j++;
    out.push({ start: i, end: j, ssIdx: j > i ? ssCount++ : null });
    i = j + 1;
  }
  return out;
}

// ---------- superset labelling (1A, 1B, 2 ...) ----------
export function labels(exs: { ss?: boolean }[]): string[] {
  const out: string[] = [];
  let group = 0;
  for (let i = 0; i < exs.length; ) {
    group++;
    let j = i;
    while (exs[j].ss && j + 1 < exs.length) j++;
    if (j === i) out.push(String(group));
    else for (let k = i; k <= j; k++) out.push(`${group}${String.fromCharCode(65 + k - i)}`);
    i = j + 1;
  }
  return out;
}

/** True when logging a set on `exs[i]` should skip the rest timer (still mid-superset). */
export const midSuperset = (exs: SessionEx[], i: number): boolean => !!exs[i]?.ss && i + 1 < exs.length;

/** Effective auto-rest state for card `i` (session override -> mid-superset off default -> exercise row -> global). */
export function effectiveRestAuto(exs: SessionEx[], i: number, rowRestAuto: boolean | undefined, globalRestAuto: boolean): boolean {
  const se = exs[i];
  if (!se) return false;
  if (se.restAuto !== undefined) return se.restAuto;
  if (midSuperset(exs, i)) return false;
  if (rowRestAuto !== undefined) return rowRestAuto;
  return globalRestAuto;
}

// ---------- finishing ----------
/** Returns remaining unlogged working exercises & set counts for explicit finish-time make-up scheduling. */
export function collectUnfinishedItems(s: LiftSession): { exId: string; sets: number; ss?: boolean }[] {
  const out: { exId: string; sets: number; ss?: boolean }[] = [];
  for (const e of s.exs) {
    const left = e.sets.filter((x) => !x.done && x.kind !== 'w').length;
    if (left > 0) out.push({ exId: e.exId, sets: left, ...(e.ss ? { ss: true } : {}) });
  }
  if (out.length) delete out[out.length - 1].ss;
  return out;
}

/** Drops unlogged sets and empty exercises when finishing a lift session (never marks sets as skipped). */
export function pruneUnlogged(s: LiftSession): number {
  let dropped = 0;
  for (const e of s.exs) {
    const keep = e.sets.filter((x) => x.done);
    dropped += e.sets.length - keep.length;
    e.sets = keep;
  }
  delete s.skipped;
  s.exs = s.exs.filter((e) => e.sets.length > 0);
  // a superset flag on the new last exercise would dangle
  if (s.exs.length) delete s.exs[s.exs.length - 1].ss;
  return dropped;
}

export const unloggedCount = (s: LiftSession): number => s.exs.reduce((n, e) => n + e.sets.filter((x) => !x.done).length, 0);

/** Routine reshaped from a finished session (set counts = logged working sets). */
export function routineFromSession(s: LiftSession, name: string, order: number): Routine {
  return {
    id: uid(),
    name,
    createdMs: Date.now(),
    order,
    items: s.exs.map((e) => ({ exId: e.exId, sets: Math.max(1, e.sets.filter((x) => x.kind !== 'w').length), ...(e.ss ? { ss: true } : {}) })),
  };
}

/** URL that locates a linked clip in Google Drive (direct share URL, timestamp query, or exact filename). */
export function driveUrlFor(meta: Pick<VideoMeta, 'fileName' | 'uri' | 'driveQuery'>): string | null {
  if (meta.uri && /^https?:\/\//i.test(meta.uri)) return meta.uri;
  const q = meta.driveQuery || meta.fileName;
  if (q) return `https://drive.google.com/drive/search?q=${encodeURIComponent(q)}`;
  return null;
}
