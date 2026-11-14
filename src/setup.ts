import { App } from '@capacitor/app';
import { calendar } from './intervals/calendar';
import { isNative } from './native/service';
import { updateSettings } from './store/settings';

/**
 * One-time Intervals.icu hookup without ever putting the key in the repo.
 *   PWA:      https://davefroriep.github.io/armory/#setup=<key>[&aid=<athleteId>]
 *   Android:  armory://setup?key=<key>[&aid=<athleteId>]
 * The key is stored in this device's localStorage and the URL fragment is stripped immediately
 * (fragments are never sent to the server).
 */
export function applySetup(key: string, aid?: string | null): boolean {
  const k = key.trim();
  if (!/^[A-Za-z0-9]{16,64}$/.test(k)) return false;
  updateSettings({ apiKey: k, ...(aid && /^i?\d+$/.test(aid) ? { athleteId: aid.startsWith('i') ? aid : `i${aid}` } : {}) });
  void calendar.refresh();
  toast('Intervals.icu connected ✓');
  return true;
}

function toast(msg: string): void {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

export function initSetupLinks(): void {
  // Web: #setup=<key>
  const m = location.hash.match(/[#&]setup=([^&]+)/);
  if (m) {
    const aid = location.hash.match(/[#&]aid=([^&]+)/)?.[1];
    applySetup(decodeURIComponent(m[1]), aid);
    history.replaceState(null, '', location.pathname + location.search + '#/');
  }
  // Android: armory://setup?key=...
  if (isNative()) {
    void App.addListener('appUrlOpen', ({ url }) => {
      try {
        const u = new URL(url.replace(/^armory:\/\//, 'https://armory.invalid/'));
        if (u.pathname.replace(/\//g, '') === 'setup') applySetup(u.searchParams.get('key') ?? '', u.searchParams.get('aid'));
      } catch { /* ignore */ }
    });
  }
}
