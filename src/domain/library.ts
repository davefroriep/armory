import type { Program } from './programs';

export interface Arranged {
  visible: Program[];
  hidden: Program[];
}

/**
 * Applies the athlete's saved order / hidden list to the built-in program library.
 * `hidden === null` means "never customised": use each program's own default.
 * New programs added in a later release slot in after the ones the athlete has already arranged.
 */
export function arrange(all: readonly Program[], order: readonly string[], hidden: readonly string[] | null): Arranged {
  const pool = all.filter((p) => p.group !== 'free');
  const rank = (p: Program): number => {
    const i = order.indexOf(p.id);
    return i === -1 ? order.length + pool.indexOf(p) : i;
  };
  const sorted = [...pool].sort((a, b) => rank(a) - rank(b));
  const isHidden = (p: Program): boolean => (hidden ? hidden.includes(p.id) : !!p.hidden);
  return { visible: sorted.filter((p) => !isHidden(p)), hidden: sorted.filter(isHidden) };
}

export type MoveDir = 'top' | 'up' | 'down';

/** New full order after moving one visible program; hidden programs keep their relative slots after the visible ones. */
export function moveProgram(a: Arranged, id: string, dir: MoveDir): string[] {
  const ids = a.visible.map((p) => p.id);
  const i = ids.indexOf(id);
  if (i === -1) return [...ids, ...a.hidden.map((p) => p.id)];
  ids.splice(i, 1);
  const to = dir === 'top' ? 0 : dir === 'up' ? Math.max(0, i - 1) : Math.min(ids.length, i + 1);
  ids.splice(to, 0, id);
  return [...ids, ...a.hidden.map((p) => p.id)];
}

/** Hidden list after hiding or restoring one program (always an explicit list once customised). */
export function toggleHidden(a: Arranged, id: string, hide: boolean): string[] {
  const set = new Set(a.hidden.map((p) => p.id));
  if (hide) set.add(id);
  else set.delete(id);
  return [...set];
}

/** Restored programs go to the bottom of the visible list rather than jumping back to an old slot. */
export function restoreOrder(a: Arranged, id: string): string[] {
  const ids = a.visible.map((p) => p.id);
  return [...ids, id, ...a.hidden.map((p) => p.id).filter((x) => x !== id)];
}
