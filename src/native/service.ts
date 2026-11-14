import { Capacitor, registerPlugin } from '@capacitor/core';

interface WorkoutServicePlugin {
  start(o: { title: string; text: string }): Promise<void>;
  update(o: { title: string; text: string }): Promise<void>;
  stop(): Promise<void>;
}

export const isNative = (): boolean => Capacitor.isNativePlatform();

const plugin = isNative() ? registerPlugin<WorkoutServicePlugin>('WorkoutService') : null;

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
