import { Emitter } from '../emitter';
import { idbAll, idbDelete, idbGet, idbPut, idbPutMany } from '../store/db';
import { computePRs, doneSessions, previousPerformance, type PRHit } from './calc';
import { LIBRARY, LIBRARY_BY_ID } from './library';
import { buildExercise } from './logic';
import { PHASE1_ROUTINES, SEED_EX_ROWS, matchRoutineByTitle } from './seed';
import { updateLiftSettings } from './settings';
import type { ExRow, Exercise, LiftSession, Routine, SessionEx, VideoMeta } from './types';

interface BlobRow {
  id: string;
  blob: Blob;
}
const BLOB_PREFIX = 'blob:';

export interface LiftBackup {
  app: 'armory-lifts';
  v: 1;
  exportedMs: number;
  sessions: LiftSession[];
  routines: Routine[];
  exercises: ExRow[];
  videos?: VideoMeta[];
}

/**
 * In-memory mirror of the lift tracker's IndexedDB stores with write-through persistence. A few hundred
 * sessions are tiny, so every screen reads synchronously from here and analytics need no queries.
 */
class LiftStore {
  readonly sessions = new Map<string, LiftSession>();
  readonly routines = new Map<string, Routine>();
  readonly rows = new Map<string, ExRow>();
  readonly videos = new Map<string, VideoMeta>();
  readonly changed = new Emitter();
  version = 0;
  readonly ready: Promise<void>;

  private exCache = new Map<string, Exercise | undefined>();
  private prCache: { v: number; m: Map<string, PRHit> } | null = null;
  private timers = new Map<string, ReturnType<typeof setTimeout>>();

  constructor() {
    this.ready = this.load();
    const flush = (): void => void this.flush();
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', () => { if (document.hidden) flush(); });
  }

  private async load(): Promise<void> {
    const [s, r, x, v] = await Promise.all([
      idbAll<LiftSession>('lift_sessions'),
      idbAll<Routine>('lift_routines'),
      idbAll<ExRow>('lift_ex'),
      idbAll<VideoMeta | BlobRow>('lift_videos'),
    ]);
    s.forEach((e) => this.sessions.set(e.id, e));
    r.forEach((e) => this.routines.set(e.id, e));
    x.forEach((e) => this.rows.set(e.id, e));
    for (const e of v) if (!e.id.startsWith(BLOB_PREFIX)) this.videos.set(e.id, e as VideoMeta);

    const SEED_KEY = 'armory.lifts.seeded.v1';
    if (typeof localStorage !== 'undefined' && !localStorage.getItem(SEED_KEY)) {
      if (this.routines.size === 0) {
        PHASE1_ROUTINES.forEach((rt) => this.routines.set(rt.id, rt));
        await idbPutMany('lift_routines', PHASE1_ROUTINES);
      }
      for (const row of SEED_EX_ROWS) {
        if (!this.rows.has(row.id)) {
          this.rows.set(row.id, row);
          await idbPut('lift_ex', row);
        }
      }
      localStorage.setItem(SEED_KEY, '1');
    }

    const CLEAN_ROWS_KEY = 'armory.lifts.clean_timers_rows.v1';
    if (typeof localStorage !== 'undefined' && !localStorage.getItem(CLEAN_ROWS_KEY)) {
      for (const row of this.rows.values()) {
        if (row.restAuto != null) {
          delete row.restAuto;
          await idbPut('lift_ex', row);
        }
      }
      const actDirty = this.active;
      if (actDirty && actDirty.exs.some((se) => se.restAuto != null)) {
        actDirty.exs.forEach((se) => { delete se.restAuto; });
        await idbPut('lift_sessions', actDirty);
      }
      localStorage.setItem(CLEAN_ROWS_KEY, '1');
    }

    const STRIP_EX_NOTES_KEY = 'armory.lifts.strip_ex_notes.v1';
    if (typeof localStorage !== 'undefined' && !localStorage.getItem(STRIP_EX_NOTES_KEY)) {
      for (const row of this.rows.values()) {
        if (row.note != null) {
          delete row.note;
          await idbPut('lift_ex', row);
        }
      }
      const actNote = this.active;
      if (actNote && actNote.exs.some((se) => se.note != null)) {
        actNote.exs.forEach((se) => { delete se.note; });
        await idbPut('lift_sessions', actNote);
      }
      localStorage.setItem(STRIP_EX_NOTES_KEY, '1');
    }

    // Auto-heal an empty active workout launched from a planned day before routines were seeded.
    const act = this.active;
    if (act && act.exs.length === 0) {
      const rt = matchRoutineByTitle(act.name, this.sortedRoutines());
      if (rt) {
        act.routineId = rt.id;
        act.name = rt.name;
        act.exs = rt.items.flatMap((it, i) =>
          this.exercise(it.exId)
            ? [buildExercise(it.exId, it.sets, this.previous(it.exId), !!it.ss, i)]
            : [],
        );
        if (act.exs.length) delete act.exs[act.exs.length - 1].ss;
        await idbPut('lift_sessions', act);
      }
    }

    this.touch(false);
  }

  private touch(emit = true): void {
    this.version++;
    this.exCache.clear();
    if (emit) this.changed.emit();
  }

  // ---------- exercises ----------
  exercise(id: string): Exercise | undefined {
    if (this.exCache.has(id)) return this.exCache.get(id);
    const lib = LIBRARY_BY_ID.get(id);
    const row = this.rows.get(id);
    let ex: Exercise | undefined;
    if (lib) {
      const { haystack: _h, ...base } = lib;
      ex = row ? { ...base, ...pickDef(row) } : base;
    } else if (row?.custom && row.name) {
      ex = { id, name: row.name, type: row.type ?? 'weight', equip: row.equip ?? 'other', primary: row.primary ?? [], secondary: row.secondary ?? [], custom: true, archived: row.archived };
    }
    this.exCache.set(id, ex);
    return ex;
  }

  /** Never undefined: a deleted custom lift still renders in old history. */
  exOrStub = (id: string): Exercise => this.exercise(id) ?? { id, name: 'Removed exercise', type: 'weight', equip: 'other', primary: [], secondary: [] };

  /** Library + custom, alphabetical. */
  allExercises(includeArchived = false): Exercise[] {
    const out: Exercise[] = LIBRARY.map((l) => this.exercise(l.id)!);
    for (const r of this.rows.values()) if (r.custom) { const e = this.exercise(r.id); if (e && (includeArchived || !e.archived)) out.push(e); }
    return out.sort((a, b) => a.name.localeCompare(b.name));
  }

  row(id: string): ExRow | undefined {
    return this.rows.get(id);
  }

  async saveRow(row: ExRow): Promise<void> {
    this.rows.set(row.id, row);
    this.touch();
    await idbPut('lift_ex', row);
  }

  /** Toggle global default auto-rest and clear per-exercise / active-card overrides so all cards align cleanly. */
  async setMasterRestAuto(on: boolean): Promise<void> {
    updateLiftSettings({ restAuto: on });
    for (const row of this.rows.values()) {
      if (row.restAuto !== undefined) {
        const next = { ...row };
        delete next.restAuto;
        this.rows.set(next.id, next);
        await idbPut('lift_ex', next);
      }
    }
    const act = this.active;
    if (act) {
      let changed = false;
      for (const se of act.exs) {
        if (se.restAuto !== undefined) {
          delete se.restAuto;
          changed = true;
        }
      }
      if (changed) {
        this.saveSession(act);
        return;
      }
    }
    this.touch();
  }

  async createCustom(def: Omit<Exercise, 'id' | 'custom'>): Promise<Exercise> {
    const id = `c-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
    const row: ExRow = { id, custom: true, ...def };
    await this.saveRow(row);
    return this.exercise(id)!;
  }

  isUsed(exId: string): boolean {
    for (const s of this.sessions.values()) if (s.exs.some((e) => e.exId === exId)) return true;
    for (const r of this.routines.values()) if (r.items.some((i) => i.exId === exId)) return true;
    return false;
  }

  /** Custom lifts with history are archived (hidden from pickers); unused ones are removed. */
  async deleteCustom(id: string): Promise<void> {
    const row = this.rows.get(id);
    if (!row?.custom) return;
    let used = false;
    for (const s of this.sessions.values()) if (s.exs.some((e) => e.exId === id)) used = true;
    if (used) await this.saveRow({ ...row, archived: true });
    else {
      this.rows.delete(id);
      this.touch();
      await idbDelete('lift_ex', id);
    }
  }

  // ---------- sessions ----------
  /** Debounced write-through for the session being edited. */
  saveSession(s: LiftSession, immediate = false): void {
    this.sessions.set(s.id, s);
    this.touch();
    const old = this.timers.get(s.id);
    if (old) clearTimeout(old);
    if (immediate) { this.timers.delete(s.id); void idbPut('lift_sessions', s); return; }
    this.timers.set(s.id, setTimeout(() => { this.timers.delete(s.id); void idbPut('lift_sessions', this.sessions.get(s.id) ?? s); }, 300));
  }

  async flush(): Promise<void> {
    for (const [id, t] of this.timers) {
      clearTimeout(t);
      const s = this.sessions.get(id);
      if (s) await idbPut('lift_sessions', s);
    }
    this.timers.clear();
  }

  async deleteSession(id: string): Promise<void> {
    const t = this.timers.get(id);
    if (t) clearTimeout(t);
    this.timers.delete(id);
    this.sessions.delete(id);
    for (const v of [...this.videos.values()]) if (v.sessionId === id) await this.deleteVideo(v.id, false);
    this.touch();
    await idbDelete('lift_sessions', id);
  }

  get active(): LiftSession | undefined {
    for (const s of this.sessions.values()) if (s.status === 'active') return s;
    return undefined;
  }

  finished(): LiftSession[] {
    return doneSessions(this.sessions.values());
  }

  previous(exId: string, beforeMs = Infinity, excludeId?: string): SessionEx | null {
    return previousPerformance(this.sessions.values(), exId, beforeMs, excludeId);
  }

  prs(): Map<string, PRHit> {
    if (this.prCache?.v === this.version) return this.prCache.m;
    const m = computePRs(this.finished(), this.exOrStub);
    this.prCache = { v: this.version, m };
    return m;
  }

  // ---------- routines ----------
  sortedRoutines(): Routine[] {
    return [...this.routines.values()].sort((a, b) => a.order - b.order || a.createdMs - b.createdMs);
  }

  async saveRoutine(r: Routine): Promise<void> {
    this.routines.set(r.id, r);
    this.touch();
    await idbPut('lift_routines', r);
  }

  async deleteRoutine(id: string): Promise<void> {
    this.routines.delete(id);
    this.touch();
    await idbDelete('lift_routines', id);
  }

  /** Swap every routine's use of `from` for `to` ("everywhere"). Returns routines touched. */
  async replaceEverywhere(from: string, to: string): Promise<number> {
    const changed: Routine[] = [];
    for (const r of this.routines.values()) {
      if (r.items.some((i) => i.exId === from)) {
        changed.push({ ...r, items: r.items.map((i) => (i.exId === from ? { ...i, exId: to } : i)) });
      }
    }
    changed.forEach((r) => this.routines.set(r.id, r));
    this.touch();
    await idbPutMany('lift_routines', changed);
    return changed.length;
  }

  // ---------- videos ----------
  async addVideo(meta: VideoMeta, blob?: Blob): Promise<void> {
    this.videos.set(meta.id, meta);
    this.touch();
    await idbPut('lift_videos', meta);
    if (blob) await idbPut('lift_videos', { id: BLOB_PREFIX + meta.id, blob } as BlobRow);
  }

  async saveVideoMeta(meta: VideoMeta): Promise<void> {
    this.videos.set(meta.id, meta);
    this.touch();
    await idbPut('lift_videos', meta);
  }

  async videoBlob(id: string): Promise<Blob | null> {
    const hit = await idbGet<BlobRow>('lift_videos', BLOB_PREFIX + id);
    return hit?.blob ?? null;
  }

  async deleteVideo(id: string, unlink = true): Promise<void> {
    const meta = this.videos.get(id);
    this.videos.delete(id);
    if (unlink && meta) {
      const s = this.sessions.get(meta.sessionId);
      if (s) {
        for (const e of s.exs) for (const set of e.sets) if (set.vids?.includes(id)) set.vids = set.vids.filter((v) => v !== id);
        this.saveSession(s, true);
      }
    }
    this.touch();
    await idbDelete('lift_videos', id);
    await idbDelete('lift_videos', BLOB_PREFIX + id);
  }

  videosForExercise(exId: string): VideoMeta[] {
    return [...this.videos.values()].filter((v) => v.exId === exId).sort((a, b) => b.ms - a.ms);
  }

  // ---------- backup ----------
  exportAll(): LiftBackup {
    return {
      app: 'armory-lifts', v: 1, exportedMs: Date.now(),
      sessions: [...this.sessions.values()],
      routines: [...this.routines.values()],
      exercises: [...this.rows.values()],
      videos: [...this.videos.values()].filter((v) => v.linkOnly || v.fileName || v.uri),
    };
  }

  /** Merge a backup in (existing ids are overwritten, nothing else is touched). */
  async importAll(b: LiftBackup): Promise<{ sessions: number; routines: number; exercises: number }> {
    if (b?.app !== 'armory-lifts' || !Array.isArray(b.sessions)) throw new Error('Not an Armory lift backup');
    const sessions = b.sessions.filter((s) => s?.id && Array.isArray(s.exs) && s.status !== 'active');
    const routines = (b.routines ?? []).filter((r) => r?.id && Array.isArray(r.items));
    const exercises = (b.exercises ?? []).filter((r) => r?.id);
    const videos = (b.videos ?? []).filter((v) => v?.id && v?.sessionId && v?.exId && v?.setId);
    sessions.forEach((s) => this.sessions.set(s.id, s));
    routines.forEach((r) => this.routines.set(r.id, r));
    exercises.forEach((r) => this.rows.set(r.id, r));
    videos.forEach((v) => this.videos.set(v.id, v));
    this.touch();
    await idbPutMany('lift_sessions', sessions);
    await idbPutMany('lift_routines', routines);
    await idbPutMany('lift_ex', exercises);
    if (videos.length) await idbPutMany('lift_videos', videos);
    return { sessions: sessions.length, routines: routines.length, exercises: exercises.length };
  }
}

function pickDef(r: ExRow): Partial<Exercise> {
  const o: Partial<Exercise> = {};
  if (r.name) o.name = r.name;
  if (r.type) o.type = r.type;
  if (r.equip) o.equip = r.equip;
  if (r.primary) o.primary = r.primary;
  if (r.secondary) o.secondary = r.secondary;
  return o;
}

export const lifts = new LiftStore();
