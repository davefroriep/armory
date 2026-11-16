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

interface WorkoutServicePlugin {
  start(o: { title: string; text: string }): Promise<void>;
  update(o: { title: string; text: string }): Promise<void>;
  stop(): Promise<void>;
  setRest(o: { remainingMs: number; totalSec: number; label: string; sound: boolean; volume: number; wavBase64?: string }): Promise<void>;
  clearRest(): Promise<void>;
  canOverlay(): Promise<{ granted: boolean }>;
  requestOverlayPermission(): Promise<void>;
  addListener(event: 'restSync', cb: (ev: NativeRestEvent) => void): Promise<PluginListenerHandle>;
  openApp(o: { pkg: string }): Promise<{ opened: boolean }>;
  launchVideoCamera(): Promise<{ opened: boolean }>;
  pickDcimVideo(): Promise<PickedNativeVideo>;
  openVideoUri(o: { uri: string }): Promise<{ opened: boolean }>;
  playChime(o: { wavBase64?: string; volume: number }): Promise<void>;
}

export const isNative = (): boolean => Capacitor.isNativePlatform();

const plugin = isNative() ? registerPlugin<WorkoutServicePlugin>('WorkoutService') : null;

const BOOSTCAMP_PKG = 'com.bpmhealth.boostcamp';
const BOOSTCAMP_WEB = 'https://www.boostcamp.app/';

/**
 * Android foreground service (custom plugin, android/.../WorkoutService*.java): keeps the process,
 * Bluetooth link, rest timer notification + overlay and chimes alive with the screen off or app minimized.
 * No-ops in the browser build.
 */
export const workoutService = {
  start: (title: string, text: string) => plugin?.start({ title, text }).catch((e) => console.warn('svc start', e)),
  update: (title: string, text: string) => plugin?.update({ title, text }).catch(() => {}),
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

/** Launches the installed Boostcamp Android app directly (never the web install prompt when installed). */
export async function openBoostcamp(): Promise<void> {
  if (plugin) {
    try {
      const r = await plugin.openApp({ pkg: BOOSTCAMP_PKG });
      if (r?.opened) return;
    } catch { /* fall through */ }
    window.open(`market://details?id=${BOOSTCAMP_PKG}`, '_system');
    return;
  }
  if (/Android/i.test(navigator.userAgent)) {
    location.href = `intent://#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;package=${BOOSTCAMP_PKG};S.browser_fallback_url=${encodeURIComponent(BOOSTCAMP_WEB)};end`;
    return;
  }
  window.open(BOOSTCAMP_WEB, '_blank', 'noopener');
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

export async function playNativeChime(wavBase64: string, volume: number): Promise<boolean> {
  if (!plugin || !wavBase64) return false;
  try { await plugin.playChime({ wavBase64, volume }); return true; } catch { return false; }
}
