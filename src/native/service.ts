import { Capacitor, registerPlugin, type PluginListenerHandle } from '@capacitor/core';

export interface PickedNativeVideo {
  picked?: boolean;
  error?: string;
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

export interface WallboardStatus {
  running: boolean;
  port: number;
  ip: string;
  url: string;
  clients: number;
  error?: string;
}

export interface NativeVoiceStatus {
  listening: boolean;
  granted: boolean;
  available: boolean;
  onDevice?: boolean;
  error?: string;
}

export interface NativeVoiceEvent {
  transcript: string;
  candidates?: string[];
  isFinal: boolean;
  source: 'mic' | 'lan';
  state: string;
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
  addListener(event: 'voiceResult', cb: (ev: NativeVoiceEvent) => void): Promise<PluginListenerHandle>;
  setKeepScreenOn(o: { on: boolean }): Promise<void>;
  setMetronome(o: { play: boolean; bpm: number; volume: number }): Promise<void>;
  startWallboard(o: { port: number; html: string }): Promise<WallboardStatus>;
  stopWallboard(): Promise<void>;
  getWallboardStatus(): Promise<WallboardStatus>;
  pushWallboardState(o: { stateJson: string }): Promise<{ clients: number }>;
  setVoiceListening(o: { enabled: boolean; biasPhrases: string[]; forceRestart?: boolean }): Promise<NativeVoiceStatus>;
  getVoiceStatus(): Promise<NativeVoiceStatus>;
  ensureVoicePermission(): Promise<{ granted: boolean }>;
  launchVideoCamera(): Promise<{ opened: boolean }>;
  pickDcimVideo(): Promise<PickedNativeVideo>;
  openVideoUri(o: { uri: string }): Promise<{ opened: boolean }>;
  getPlayableVideoUrl(o: { uri: string; mime?: string }): Promise<{ url?: string; error?: string }>;
  exportVideo(o: {
    uri?: string;
    httpUrl?: string;
    base64?: string;
    fileName?: string;
    title?: string;
    startMs?: number;
    endMs?: number;
    action: 'share' | 'save';
  }): Promise<{ ok: boolean; savedPath?: string; error?: string }>;
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

export async function getPlayableNativeVideoUrl(uri: string, mime?: string): Promise<string | null> {
  if (!plugin || !uri) return null;
  try {
    const res = await plugin.getPlayableVideoUrl({ uri, ...(mime ? { mime } : {}) });
    return res.url ?? null;
  } catch {
    return null;
  }
}

export async function exportNativeVideo(o: {
  uri?: string;
  httpUrl?: string;
  base64?: string;
  fileName?: string;
  title?: string;
  startMs?: number;
  endMs?: number;
  action: 'share' | 'save';
}): Promise<{ ok: boolean; savedPath?: string; error?: string }> {
  if (!plugin) return { ok: false, error: 'not native' };
  try { return await plugin.exportVideo(o); } catch (e) { return { ok: false, error: (e as Error).message }; }
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

export async function startNativeWallboard(port: number, html: string): Promise<WallboardStatus> {
  if (!plugin) return { running: false, port, ip: '127.0.0.1', url: `http://127.0.0.1:${port}`, clients: 0, error: 'Android APK required for LAN server' };
  try { return await plugin.startWallboard({ port, html }); }
  catch (e) { return { running: false, port, ip: '127.0.0.1', url: `http://127.0.0.1:${port}`, clients: 0, error: (e as Error).message }; }
}

export async function stopNativeWallboard(): Promise<void> {
  if (!plugin) return;
  try { await plugin.stopWallboard(); } catch { /* ignore */ }
}

export async function getNativeWallboardStatus(): Promise<WallboardStatus> {
  if (!plugin) return { running: false, port: 8765, ip: '127.0.0.1', url: 'http://127.0.0.1:8765', clients: 0 };
  try { return await plugin.getWallboardStatus(); }
  catch { return { running: false, port: 8765, ip: '127.0.0.1', url: 'http://127.0.0.1:8765', clients: 0 }; }
}

export async function pushNativeWallboardState(stateJson: string): Promise<number> {
  if (!plugin) return 0;
  try { return (await plugin.pushWallboardState({ stateJson })).clients; } catch { return 0; }
}

export async function setNativeVoiceListening(enabled: boolean, biasPhrases: string[], forceRestart = false): Promise<NativeVoiceStatus> {
  if (!plugin) return { listening: false, granted: false, available: false, error: 'Android APK required' };
  try { return await plugin.setVoiceListening({ enabled, biasPhrases, forceRestart }); }
  catch (e) { return { listening: false, granted: false, available: false, error: (e as Error).message }; }
}

export async function getNativeVoiceStatus(): Promise<NativeVoiceStatus> {
  if (!plugin) return { listening: false, granted: false, available: false };
  try { return await plugin.getVoiceStatus(); }
  catch { return { listening: false, granted: false, available: false }; }
}

export async function ensureNativeVoicePermission(): Promise<boolean> {
  if (!plugin) return true;
  try { return (await plugin.ensureVoicePermission()).granted; }
  catch { return false; }
}

export function onNativeVoiceResult(cb: (ev: NativeVoiceEvent) => void): () => void {
  if (!plugin) return () => {};
  let handle: PluginListenerHandle | null = null;
  let dead = false;
  void plugin.addListener('voiceResult', cb).then((h) => {
    if (dead) void h.remove();
    else handle = h;
  }).catch(() => {});
  return () => {
    dead = true;
    if (handle) void handle.remove();
  };
}
