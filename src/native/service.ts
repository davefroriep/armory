import { Capacitor, registerPlugin } from '@capacitor/core';

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

interface WorkoutServicePlugin {
  start(o: { title: string; text: string }): Promise<void>;
  update(o: { title: string; text: string }): Promise<void>;
  stop(): Promise<void>;
  openApp(o: { pkg: string }): Promise<{ opened: boolean }>;
  launchVideoCamera(): Promise<{ opened: boolean }>;
  pickDcimVideo(): Promise<PickedNativeVideo>;
  openVideoUri(o: { uri: string }): Promise<{ opened: boolean }>;
}

export const isNative = (): boolean => Capacitor.isNativePlatform();

const plugin = isNative() ? registerPlugin<WorkoutServicePlugin>('WorkoutService') : null;

const BOOSTCAMP_PKG = 'com.bpmhealth.boostcamp';
const BOOSTCAMP_WEB = 'https://www.boostcamp.app/';

/**
 * Android foreground service (custom plugin, android/.../WorkoutService*.java): keeps the process,
 * Bluetooth link and CPU alive with the screen off, and shows a live lock-screen notification.
 * No-ops in the browser build.
 */
export const workoutService = {
  start: (title: string, text: string) => plugin?.start({ title, text }).catch((e) => console.warn('svc start', e)),
  update: (title: string, text: string) => plugin?.update({ title, text }).catch(() => {}),
  stop: () => plugin?.stop().catch(() => {}),
};

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
