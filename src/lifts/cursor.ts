import { addSet, labels, removeExerciseAt, supersetGroups } from './logic';
import type { Exercise, LiftSet, SessionEx } from './types';

export interface CursorRef {
  exUid: string;
  setId?: string;
  explicit?: boolean;
}

export interface SetPointer {
  exIdx: number;
  setIdx: number;
  exUid: string;
  setId: string;
  label: string;
  setNumber: number;
  totalWorkingSets: number;
  isWarmup: boolean;
}

function groupSetSequence(exs: SessionEx[], g: { start: number; end: number }): { exIdx: number; setIdx: number }[] {
  const out: { exIdx: number; setIdx: number }[] = [];
  const warms: number[][] = [];
  const works: number[][] = [];
  let maxWarm = 0;
  let maxWork = 0;
  for (let k = g.start; k <= g.end; k++) {
    const se = exs[k];
    const wIdx: number[] = [];
    const nIdx: number[] = [];
    se.sets.forEach((s, si) => {
      if (s.kind === 'w') wIdx.push(si);
      else nIdx.push(si);
    });
    warms.push(wIdx);
    works.push(nIdx);
    maxWarm = Math.max(maxWarm, wIdx.length);
    maxWork = Math.max(maxWork, nIdx.length);
  }
  for (let r = 0; r < maxWarm; r++) {
    for (let off = 0; off < warms.length; off++) {
      const si = warms[off][r];
      if (si != null) out.push({ exIdx: g.start + off, setIdx: si });
    }
  }
  for (let r = 0; r < maxWork; r++) {
    for (let off = 0; off < works.length; off++) {
      const si = works[off][r];
      if (si != null) out.push({ exIdx: g.start + off, setIdx: si });
    }
  }
  return out;
}

/**
 * Planned execution sequence across all exercises, interleaving superset rounds (`1A[0], 1B[0], 1A[1], 1B[1]...`)
 * with warm-up rounds first and working rounds after within each superset group.
 */
export function plannedSetSequence(exs: SessionEx[]): { exIdx: number; setIdx: number }[] {
  const out: { exIdx: number; setIdx: number }[] = [];
  for (const g of supersetGroups(exs)) {
    out.push(...groupSetSequence(exs, g));
  }
  return out;
}

function makePointer(exs: SessionEx[], labs: string[], exIdx: number, setIdx: number): SetPointer | null {
  const se = exs[exIdx];
  const set = se?.sets[setIdx];
  if (!se || !set) return null;
  const totalWorkingSets = se.sets.filter((s) => s.kind !== 'w').length;
  const workingOrdinal = se.sets.slice(0, setIdx + 1).filter((s) => s.kind !== 'w').length;
  return {
    exIdx,
    setIdx,
    exUid: se.uid,
    setId: set.id,
    label: labs[exIdx] ?? String(exIdx + 1),
    setNumber: set.kind === 'w' ? setIdx + 1 : Math.max(1, workingOrdinal),
    totalWorkingSets,
    isWarmup: set.kind === 'w',
  };
}

/**
 * Resolves the exact target set row to highlight and log next.
 * Respects `cursor` if it points to an unlogged exercise/set, otherwise falls back to the first unlogged
 * slot in `plannedSetSequence(exs)`.
 */
export function resolveTargetSet(exs: SessionEx[], cursor?: CursorRef | null): SetPointer | null {
  if (!exs.length) return null;
  const labs = labels(exs);
  if (cursor) {
    const exIdx = exs.findIndex((e) => e.uid === cursor.exUid);
    if (exIdx >= 0) {
      const se = exs[exIdx];
      if (cursor.setId) {
        const si = se.sets.findIndex((s) => s.id === cursor.setId);
        if (si >= 0 && (!se.sets[si].done || cursor.explicit)) return makePointer(exs, labs, exIdx, si);
      }
      const firstOpen = se.sets.findIndex((s) => !s.done);
      if (firstOpen >= 0) return makePointer(exs, labs, exIdx, firstOpen);
    }
  }
  const seq = plannedSetSequence(exs);
  for (const step of seq) {
    if (!exs[step.exIdx]?.sets[step.setIdx]?.done) {
      return makePointer(exs, labs, step.exIdx, step.setIdx);
    }
  }
  return null;
}

/**
 * Advances the cursor after logging a set:
 * stays within the current exercise (or current superset pair) until all of its sets are complete,
 * then moves to the next unlogged exercise/superset group.
 */
export function advanceCursorAfterLog(exs: SessionEx[], justLogged: { exIdx: number; setIdx: number }): CursorRef | null {
  const groups = supersetGroups(exs);
  if (!groups.length) return null;
  const curGroupIdx = groups.findIndex((g) => justLogged.exIdx >= g.start && justLogged.exIdx <= g.end);
  if (curGroupIdx < 0) {
    const seq = plannedSetSequence(exs);
    const step = seq.find((p) => !exs[p.exIdx]?.sets[p.setIdx]?.done);
    return step ? { exUid: exs[step.exIdx].uid, setId: exs[step.exIdx].sets[step.setIdx].id } : null;
  }

  const curGroup = groups[curGroupIdx];
  const curSeq = groupSetSequence(exs, curGroup);
  const pos = curSeq.findIndex((p) => p.exIdx === justLogged.exIdx && p.setIdx === justLogged.setIdx);
  const start = pos >= 0 ? pos : -1;
  for (let offset = 1; offset <= curSeq.length; offset++) {
    const step = curSeq[(start + offset) % curSeq.length];
    const se = exs[step.exIdx];
    const set = se?.sets[step.setIdx];
    if (se && set && !set.done && !(step.exIdx === justLogged.exIdx && step.setIdx === justLogged.setIdx)) {
      return { exUid: se.uid, setId: set.id };
    }
  }

  for (let goff = 1; goff < groups.length; goff++) {
    const g = groups[(curGroupIdx + goff) % groups.length];
    for (const step of groupSetSequence(exs, g)) {
      const se = exs[step.exIdx];
      const set = se?.sets[step.setIdx];
      if (se && set && !set.done) {
        return { exUid: se.uid, setId: set.id };
      }
    }
  }
  return null;
}

/** Previews what the target set would be right after `current` is completed (for Wallboard `UP NEXT`). */
export function peekNextTargetSet(exs: SessionEx[], current: SetPointer | null): SetPointer | null {
  if (!current) return null;
  const nextRef = advanceCursorAfterLog(exs, { exIdx: current.exIdx, setIdx: current.setIdx });
  if (!nextRef) return null;
  const labs = labels(exs);
  const exIdx = exs.findIndex((e) => e.uid === nextRef.exUid);
  if (exIdx < 0) return null;
  const setIdx = nextRef.setId ? exs[exIdx].sets.findIndex((s) => s.id === nextRef.setId) : exs[exIdx].sets.findIndex((s) => !s.done);
  return setIdx >= 0 ? makePointer(exs, labs, exIdx, setIdx) : null;
}

/** Moves focus forward (`+1`) or backward (`-1`) across exercises with unlogged sets. */
export function stepExerciseCursor(exs: SessionEx[], current: SetPointer | null, dir: 1 | -1): CursorRef | null {
  if (!exs.length) return null;
  const curIdx = current ? current.exIdx : (dir > 0 ? -1 : 0);
  for (let step = 1; step <= exs.length; step++) {
    const idx = ((curIdx + dir * step) % exs.length + exs.length) % exs.length;
    const se = exs[idx];
    const open = se.sets.find((s) => !s.done);
    if (open) return { exUid: se.uid, setId: open.id };
  }
  return null;
}

const SPOKEN_PREFIX: Record<string, string> = {
  one: '1', two: '2', to: '2', too: '2', three: '3', four: '4', for: '4', five: '5', six: '6', seven: '7', eight: '8', nine: '9',
};
const SPOKEN_SUFFIX: Record<string, string> = {
  a: 'a', ay: 'a', eh: 'a', b: 'b', be: 'b', bee: 'b', c: 'c', see: 'c', sea: 'c', d: 'd', dee: 'd',
};

function normalizeLabelQuery(raw: string): string {
  const cleaned = raw.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').trim();
  const toks = cleaned.split(/\s+/).filter((t) => t && !['exercise', 'group', 'set', 'the', 'to'].includes(t));
  if (toks.length === 1 && /^[1-9]\d*[a-d]?$/.test(toks[0])) return toks[0];
  if (toks.length === 2) {
    const num = SPOKEN_PREFIX[toks[0]] ?? (/^[1-9]\d*$/.test(toks[0]) ? toks[0] : '');
    const letCh = SPOKEN_SUFFIX[toks[1]] ?? (/^[a-d]$/.test(toks[1]) ? toks[1] : '');
    if (num && letCh) return `${num}${letCh}`;
  }
  if (toks.length === 1 && SPOKEN_PREFIX[toks[0]]) return SPOKEN_PREFIX[toks[0]];
  return '';
}

/** Resolves 1-based working set number (falling back to overall index when no working sets match). */
export function findSetIndexByNumber(se: SessionEx, setNum: number): number {
  if (!Number.isFinite(setNum) || setNum < 1) return -1;
  let workingOrd = 0;
  for (let i = 0; i < se.sets.length; i++) {
    if (se.sets[i].kind !== 'w') {
      workingOrd += 1;
      if (workingOrd === setNum) return i;
    }
  }
  return setNum - 1 < se.sets.length ? setNum - 1 : -1;
}

/** Jumps the cursor to a superset/exercise label (`2B`, `"two b"`, `"exercise 1"`), exercise name, and optional 1-based set number. */
export function jumpToExerciseCursor(
  exs: SessionEx[],
  rawTarget: string,
  exOf: (id: string) => Exercise,
  setNum?: number,
): CursorRef | null {
  if (!exs.length) return null;
  const labs = labels(exs);
  const labQ = normalizeLabelQuery(rawTarget);
  const pickSet = (se: SessionEx): CursorRef | null => {
    if (setNum != null && setNum >= 1) {
      const si = findSetIndexByNumber(se, setNum);
      return si >= 0 ? { exUid: se.uid, setId: se.sets[si].id, explicit: true } : null;
    }
    const open = se.sets.find((s) => !s.done);
    if (open) return { exUid: se.uid, setId: open.id };
    const last = se.sets[se.sets.length - 1];
    return last ? { exUid: se.uid, setId: last.id, explicit: true } : { exUid: se.uid };
  };

  if (labQ) {
    let idx = labs.findIndex((l) => l.toLowerCase() === labQ);
    if (idx < 0 && /^[1-9]\d*$/.test(labQ)) {
      idx = labs.findIndex((l) => l.toLowerCase() === `${labQ}a`);
    }
    if (idx < 0 && /^[1-9]\d*$/.test(labQ)) {
      const ordIdx = Number(labQ) - 1;
      if (ordIdx >= 0 && ordIdx < exs.length) idx = ordIdx;
    }
    if (idx >= 0) return pickSet(exs[idx]);
  }

  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
  const q = norm(rawTarget).replace(/^exercise\s+/, '').trim();
  if (!q) return null;
  const qTokens = q.split(' ').filter((w) => w.length >= 2);
  let bestIdx = -1;
  let bestScore = 0;

  exs.forEach((se, idx) => {
    const name = norm(exOf(se.exId).name);
    const hasOpen = se.sets.some((s) => !s.done);
    let score = 0;
    if (name === q) score += 100;
    else if (name.includes(q) || q.includes(name)) score += 60;
    const nameTokens = name.split(' ');
    for (const qt of qTokens) {
      if (nameTokens.some((nt) => nt === qt || nt.startsWith(qt) || qt.startsWith(nt))) {
        score += 20;
      }
    }
    if (score > 0 && hasOpen) score += 10;
    if (score > bestScore) {
      bestScore = score;
      bestIdx = idx;
    }
  });

  if (bestIdx < 0) return null;
  return pickSet(exs[bestIdx]);
}

/** Jumps the cursor to 1-based `setNum` within the currently focused exercise. */
export function jumpToSetInCurrentExercise(
  exs: SessionEx[],
  cursor: CursorRef | null,
  setNum: number,
): CursorRef | null {
  if (!exs.length || !Number.isFinite(setNum) || setNum < 1) return null;
  const cur = resolveTargetSet(exs, cursor);
  const se = cur
    ? exs[cur.exIdx]
    : (cursor?.exUid ? exs.find((e) => e.uid === cursor.exUid) : undefined) ?? exs[0];
  const si = findSetIndexByNumber(se, setNum);
  if (si < 0) return null;
  return { exUid: se.uid, setId: se.sets[si].id, explicit: true };
}

export interface SetMutationResult {
  changed: boolean;
  cursor: CursorRef | null;
  exIdx: number;
  added?: LiftSet;
  deletedCount?: number;
}

/** Appends a set to the current target exercise (keeps focus on the first open set or the newly added one). */
export function applyAddSet(
  exs: SessionEx[],
  cursor: CursorRef | null,
  lastLoggedExUid: string | null,
  prevOf: (exId: string) => SessionEx | null,
): SetMutationResult {
  if (!exs.length) return { changed: false, cursor, exIdx: -1 };
  const cur = resolveTargetSet(exs, cursor);
  const targetSe = cur
    ? exs[cur.exIdx]
    : (lastLoggedExUid ? exs.find((e) => e.uid === lastLoggedExUid) : undefined) ?? exs[exs.length - 1];
  const exIdx = exs.indexOf(targetSe);
  const firstOpen = targetSe.sets.find((x) => !x.done);
  const added = addSet(targetSe, prevOf(targetSe.exId));
  return {
    changed: true,
    cursor: { exUid: targetSe.uid, setId: firstOpen ? firstOpen.id : added.id },
    exIdx,
    added,
  };
}

/** Appends a set to the previously logged exercise and focuses that newly added set for the next command. */
export function applyAddSetToLastEx(
  exs: SessionEx[],
  cursor: CursorRef | null,
  lastLoggedExUid: string | null,
  prevOf: (exId: string) => SessionEx | null,
): SetMutationResult {
  if (!exs.length) return { changed: false, cursor, exIdx: -1 };
  const cur = resolveTargetSet(exs, cursor);
  let prevSe = lastLoggedExUid ? exs.find((e) => e.uid === lastLoggedExUid) : undefined;
  if (!prevSe && cur && cur.exIdx > 0) prevSe = exs[cur.exIdx - 1];
  if (!prevSe) return { changed: false, cursor, exIdx: -1 };
  const exIdx = exs.indexOf(prevSe);
  const added = addSet(prevSe, prevOf(prevSe.exId));
  return {
    changed: true,
    cursor: { exUid: prevSe.uid, setId: added.id },
    exIdx,
    added,
  };
}

/** Deletes all unlogged sets from the current exercise only, removing the card if empty, and advances to the next open exercise. */
export function applyDeleteEmptySets(exs: SessionEx[], cursor: CursorRef | null): SetMutationResult {
  const cur = resolveTargetSet(exs, cursor);
  if (!cur) return { changed: false, cursor, exIdx: -1, deletedCount: 0 };
  const se = exs[cur.exIdx];
  const emptyCount = se.sets.filter((x) => !x.done).length;
  if (!emptyCount) return { changed: false, cursor, exIdx: cur.exIdx, deletedCount: 0 };

  let nextRef: CursorRef | null = null;
  for (let offset = 1; offset < exs.length; offset++) {
    const cand = exs[(cur.exIdx + offset) % exs.length];
    const open = cand.sets.find((x) => !x.done);
    if (open) {
      nextRef = { exUid: cand.uid, setId: open.id };
      break;
    }
  }

  const kept = se.sets.filter((x) => x.done);
  if (kept.length > 0) {
    se.sets = kept;
  } else {
    removeExerciseAt(exs, cur.exIdx);
  }
  return {
    changed: true,
    cursor: nextRef,
    exIdx: cur.exIdx,
    deletedCount: emptyCount,
  };
}
