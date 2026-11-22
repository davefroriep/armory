import { Emitter } from '../emitter';
import { defaultQueueState } from '../domain/plan';
import { calendar, localDate } from '../intervals/calendar';
import { shareImage } from '../lifts/share';
import { liftSettings, liftSettingsChanged, updateLiftSettings } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { isNative, pickNativeBackupFile, writeNativeBackupFile } from '../native/service';
import { REV } from '../rev';
import type { Sample } from '../types';
import { allMeta, getSamples, metaSaved, putMeta, putSamples } from './db';
import { getSettings, settingsChanged, updateSettings, type Settings } from './settings';
import { assembleFullBackup, parseBackupJson, type FullBackup } from './backupCodec';

export { assembleFullBackup, parseBackupJson, type FullBackup } from './backupCodec';

export const BACKUP_DIR_LABEL = 'Documents/ArmoryFit Backup';
const STATUS_KEY = 'armory.backup.status.v1';

export interface BackupStatus {
  atMs: number;
  ymd: string;
  path: string;
  lifts: number;
  cardio: number;
  routines: number;
  error?: string;
}

export interface RestoreResult {
  kind: 'v2' | 'v1';
  lifts: number;
  cardio: number;
  routines: number;
  exercises: number;
}

export const backupChanged = new Emitter<BackupStatus | null>();

export function lastBackupStatus(): BackupStatus | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    const raw = localStorage.getItem(STATUS_KEY);
    return raw ? (JSON.parse(raw) as BackupStatus) : null;
  } catch {
    return null;
  }
}

function saveStatus(st: BackupStatus): void {
  if (typeof localStorage !== 'undefined') localStorage.setItem(STATUS_KEY, JSON.stringify(st));
  backupChanged.emit(st);
}

export async function buildFullBackup(opts: { includeSamples?: boolean; includeSecrets?: boolean } = {}): Promise<FullBackup> {
  const { includeSamples = true, includeSecrets = true } = opts;
  await lifts.ready;
  const metas = (await allMeta()).filter((m) => m.status === 'saved');
  const workoutSamples: Record<string, Sample[]> = {};
  if (includeSamples) {
    await Promise.all(
      metas.map(async (m) => {
        const smp = await getSamples(m.id);
        if (smp.length) workoutSamples[m.id] = smp;
      }),
    );
  }
  return assembleFullBackup({
    exportedMs: Date.now(),
    rev: REV,
    liftBackup: lifts.exportAll(),
    workoutsMeta: metas,
    workoutSamples,
    liftSettings: liftSettings(),
    appSettings: getSettings(),
    includeSecrets,
  });
}

let inflightBackup: Promise<BackupStatus> | null = null;

/** Writes `armory-fit-latest.json` (with 1 Hz samples) and compact daily `armory-fit-YYYY-MM-DD.json` to `Documents/ArmoryFit Backup`. */
export async function runBackupNow(): Promise<BackupStatus> {
  if (inflightBackup) return inflightBackup;
  inflightBackup = (async () => {
    const data = await buildFullBackup({ includeSamples: true, includeSecrets: true });
    const latestJson = JSON.stringify(data);
    const dailyJson = JSON.stringify({ ...data, workoutSamples: {} });
    const today = localDate();
    const st: BackupStatus = {
      atMs: data.exportedMs,
      ymd: today,
      path: `${BACKUP_DIR_LABEL}/armory-fit-latest.json`,
      lifts: data.sessions.filter((s) => s.status === 'done').length,
      cardio: data.workoutsMeta.filter((m) => m.equipment !== 'strength').length,
      routines: data.routines.length,
    };
    if (isNative()) {
      const [latestRes, dailyRes] = await Promise.all([
        writeNativeBackupFile('armory-fit-latest.json', latestJson),
        writeNativeBackupFile(`armory-fit-${today}.json`, dailyJson),
      ]);
      if (!latestRes.saved && !dailyRes.saved) {
        st.error = latestRes.error || dailyRes.error || 'Could not write to Documents/ArmoryFit Backup';
      } else if (latestRes.path) {
        st.path = latestRes.path;
      }
    }
    saveStatus(st);
    return st;
  })().finally(() => {
    inflightBackup = null;
  });
  return inflightBackup;
}

/** Shares/downloads the complete backup file (useful on web or manual archive sharing). */
export async function shareFullBackup(): Promise<FullBackup> {
  const data = await buildFullBackup({ includeSamples: true, includeSecrets: true });
  const blob = new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' });
  await shareImage(blob, `armory-fit-${localDate()}.json`, 'Armory Fit full backup');
  return data;
}

/** Restores either a v2/v3 full Armory Fit backup or a legacy v1 armory-lifts backup. */
export async function restoreFromJson(raw: string): Promise<RestoreResult> {
  const b = parseBackupJson(raw);
  if (isNative()) {
    try {
      const pre = await buildFullBackup({ includeSamples: false, includeSecrets: true });
      await writeNativeBackupFile(`pre-restore-${Date.now()}.json`, JSON.stringify(pre));
    } catch { /* best-effort safety snapshot */ }
  }
  const res = await lifts.importAll(
    b.app === 'armory-fit-backup'
      ? { app: 'armory-lifts', v: 1, exportedMs: b.exportedMs, sessions: b.sessions, routines: b.routines, exercises: b.exercises, videos: b.videos }
      : b,
  );
  if (b.app !== 'armory-fit-backup') {
    return { kind: 'v1', lifts: res.sessions, cardio: 0, routines: res.routines, exercises: res.exercises };
  }

  let cardioCount = 0;
  for (const m of b.workoutsMeta ?? []) {
    if (!m?.id || m.status !== 'saved') continue;
    await putMeta(m, false);
    const smp = b.workoutSamples?.[m.id];
    if (Array.isArray(smp) && smp.length) await putSamples(m.id, smp);
    if (m.equipment !== 'strength') cardioCount++;
  }
  if (b.liftSettings && typeof b.liftSettings === 'object') {
    updateLiftSettings(b.liftSettings);
  }
  if (b.appSettings && typeof b.appSettings === 'object') {
    const incoming = b.appSettings as Partial<Settings>;
    const { apiKey: rawKey, queue: rawQueue, ...rest } = incoming;
    const nextKey = typeof rawKey === 'string' && rawKey.trim() ? rawKey.trim() : getSettings().apiKey;
    const nextQueue = rawQueue && typeof rawQueue === 'object' ? { ...defaultQueueState(), ...rawQueue } : getSettings().queue;
    updateSettings({ ...rest, queue: nextQueue, apiKey: nextKey });
  }
  void calendar.refresh();
  return { kind: 'v2', lifts: res.sessions, cardio: cardioCount, routines: res.routines, exercises: res.exercises };
}

/** Opens native picker (`Documents/ArmoryFit Backup`) or browser file input, returning selected filename + content. */
export async function pickBackupText(): Promise<{ fileName: string; content: string } | null> {
  if (isNative()) {
    const res = await pickNativeBackupFile();
    if (!res.picked || !res.content) return null;
    return { fileName: res.fileName || 'backup.json', content: res.content };
  }
  return new Promise((resolve) => {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'application/json,.json';
    inp.onchange = async () => {
      const f = inp.files?.[0];
      resolve(f ? { fileName: f.name, content: await f.text() } : null);
    };
    inp.addEventListener('cancel', () => resolve(null));
    inp.click();
  });
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null;

export function scheduleAutoBackup(): void {
  if (!isNative()) return;
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    void runBackupNow().catch(() => {});
  }, 2500);
}

export function ensureDailyBackup(): void {
  if (!isNative()) return;
  const today = localDate();
  if (lastBackupStatus()?.ymd !== today) {
    scheduleAutoBackup();
  }
}

let autoWired = false;
let lastLiftSig = '';
let lastSettingsSig = '';

/** Watches finished sessions, routines, exercise rows, and settings so DriveSync always has a fresh snapshot. */
export function initAutoBackup(): void {
  if (autoWired || !isNative()) return;
  autoWired = true;
  void lifts.ready.then(() => {
    const liftSig = (): string => {
      const doneSig = lifts
        .finished()
        .map((s) => `${s.id}:${s.endMs ?? s.startMs}:${s.exs.reduce((n, e) => n + e.sets.filter((x) => x.done).length, 0)}`)
        .join(',');
      return `${doneSig}|${JSON.stringify([...lifts.routines.values()])}|${JSON.stringify([...lifts.rows.values()])}`;
    };
    lastLiftSig = liftSig();
    lastSettingsSig = JSON.stringify(getSettings());
    ensureDailyBackup();

    lifts.changed.on(() => {
      const next = liftSig();
      if (next !== lastLiftSig) {
        lastLiftSig = next;
        scheduleAutoBackup();
      }
    });
    settingsChanged.on((s) => {
      const nextS = JSON.stringify(s);
      if (nextS !== lastSettingsSig) {
        lastSettingsSig = nextS;
        scheduleAutoBackup();
      }
    });
    liftSettingsChanged.on(() => {
      scheduleAutoBackup();
    });
    metaSaved.on(() => {
      scheduleAutoBackup();
    });
  });
}
