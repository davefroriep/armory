import { Capacitor, registerPlugin, type PluginListenerHandle } from '@capacitor/core';

export interface PickedNativeVideo {
  picked?: boolean;
  uri?: string;
  fileName?: string;
  bytes?: number;
  fileMs?: number;
  mime?: string;
  durationSec?: number;
  thumb?: string;
}

export interface NativeRestEvent {
  action: 'fired' | 'adjust' | 'clear';
  remainingMs: number;
  totalSec: number;
}

export interface NativeBackupWriteResult {
  saved: boolean;
  path?: string;
  error?: string;
}

export interface NativeBackupPickResult {
  picked: boolean;
  fileName?: string;
  content?: string;
  error?: string;
}

interface WorkoutServicePlugin {
  start(o: { title: string; text: string; startMs?: number; wavBase64?: string }): Promise<void>;
  update(o: { title: string; text: string; startMs?: number }): Promise<void>;
  stop(): Promise<void>;
  setRest(o: { remainingMs: number; totalSec: number; label: string; sound: boolean; volume: number; wavBase64?: string }): Promise<void>;
  clearRest(): Promise<void>;
  canOverlay(): Promise<{ granted: boolean }>;
  requestOverlayPermission(): Promise<void>;
  openAppSettings(): Promise<void>;
  addListener(event: 'restSync', cb: (ev: NativeRestEvent) => void): Promise<PluginListenerHandle>;
  setKeepScreenOn(o: { on: boolean }): Promise<void>;
  setMetronome(o: { play: boolean; bpm: number; volume: number }): Promise<void>;
  launchVideoCamera(): Promise<{ opened: boolean }>;
  pickDcimVideo(): Promise<PickedNativeVideo>;
  openVideoUri(o: { uri: string }): Promise<{ opened: boolean }>;
  playChime(o: { wavBase64?: string; volume: number }): Promise<void>;
  writeBackupFile(o: { fileName: string; content: string }): Promise<NativeBackupWriteResult>;
  pickBackupFile(): Promise<NativeBackupPickResult>;
  forceClose(): Promise<void>;
}

export const isNative = (): boolean => Capacitor.isNativePlatform();

const plugin = isNative() ? registerPlugin<WorkoutServicePlugin>('WorkoutService') : null;

/**
 * Android foreground service (custom plugin, android/.../WorkoutService*.java): keeps the process,
 * Bluetooth link, rest timer notification + overlay and chimes alive with the screen off or app minimized.
 * No-ops in the browser build.
 */
export const workoutService = {
  start: (title: string, text: string, startMs?: number, wavBase64?: string) =>
    plugin?.start({ title, text, ...(startMs ? { startMs } : {}), ...(wavBase64 ? { wavBase64 } : {}) }).catch((e) => console.warn('svc start', e)),
  update: (title: string, text: string, startMs?: number) =>
    plugin?.update({ title, text, ...(startMs ? { startMs } : {}) }).catch(() => {}),
  stop: () => plugin?.stop().catch(() => {}),
  setRest: (o: { remainingMs: number; totalSec: number; label: string; sound: boolean; volume: number; wavBase64?: string }) =>
    plugin?.setRest(o).catch(() => {}),
  clearRest: () => plugin?.clearRest().catch(() => {}),
};

export async function canShowRestOverlay(): Promise<boolean> {
  if (!plugin) return false;
  try { return (await plugin.canOverlay()).granted; } catch { return false; }
}

export async function requestRestOverlayPermission(): Promise<void> {
  if (!plugin) return;
  try { await plugin.requestOverlayPermission(); } catch { /* ignore */ }
}

export async function openAppInfoSettings(): Promise<void> {
  if (!plugin) return;
  try { await plugin.openAppSettings(); } catch { /* ignore */ }
}

export function onNativeRestSync(cb: (ev: NativeRestEvent) => void): () => void {
  if (!plugin) return () => {};
  let handle: PluginListenerHandle | null = null;
  let dead = false;
  void plugin.addListener('restSync', cb).then((h) => {
    if (dead) void h.remove();
    else handle = h;
  }).catch(() => {});
  return () => {
    dead = true;
    if (handle) void handle.remove();
  };
}

export async function setKeepScreenOn(on: boolean): Promise<void> {
  if (!plugin) return;
  try { await plugin.setKeepScreenOn({ on }); } catch { /* ignore */ }
}

export async function setNativeMetronome(play: boolean, bpm: number, volume: number): Promise<void> {
  if (!plugin) return;
  try { await plugin.setMetronome({ play, bpm, volume }); } catch { /* ignore */ }
}

export async function launchNativeVideoCamera(): Promise<boolean> {
  if (!plugin) return false;
  try { return (await plugin.launchVideoCamera()).opened; } catch { return false; }
}

export async function pickNativeDcimVideo(): Promise<PickedNativeVideo | null> {
  if (!plugin) return null;
  try { return await plugin.pickDcimVideo(); } catch { return null; }
}

export async function openNativeVideoUri(uri: string): Promise<boolean> {
  if (!plugin || !uri) return false;
  try { return (await plugin.openVideoUri({ uri })).opened; } catch { return false; }
}

/** Terminates any foreground workout service, removes the Android Recents task, and exits the process. */
export async function forceCloseApp(): Promise<void> {
  if (plugin) await plugin.forceClose();
}

export async function playNativeChime(wavBase64: string, volume: number): Promise<boolean> {
  if (!plugin || !wavBase64) return false;
  try { await plugin.playChime({ wavBase64, volume }); return true; } catch { return false; }
}

export async function writeNativeBackupFile(fileName: string, content: string): Promise<NativeBackupWriteResult> {
  if (!plugin) return { saved: false, error: 'not native' };
  try { return await plugin.writeBackupFile({ fileName, content }); } catch (e) { return { saved: false, error: (e as Error).message }; }
}

export async function pickNativeBackupFile(): Promise<NativeBackupPickResult> {
  if (!plugin) return { picked: false };
  try { return await plugin.pickBackupFile(); } catch (e) { return { picked: false, error: (e as Error).message }; }
}
