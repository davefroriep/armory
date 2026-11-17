import './style.css';
import { App } from '@capacitor/app';
import { retryPending } from './workout/sync';
import { airbikeScreen, stubScreen } from './ui/equipment';
import { homeScreen } from './ui/home';
import { hudScreen } from './ui/hud';
import { programScreen } from './ui/program';
import { settingsScreen } from './ui/settings';
import { liftsScreen } from './ui/lifts';
import { initSetupLinks } from './setup';
import { calendar } from './intervals/calendar';
import { isNative } from './native/service';
import { kickSensors, releaseBike } from './ble/sources';
import { parentHash } from './nav';
import { syncModalInert, wireModalBackdrop } from './ui/dom';

type Screen = (root: HTMLElement, args: string[], query: URLSearchParams) => (() => void) | void;

const routes: Record<string, Screen> = {
  '': homeScreen,
  program: programScreen,
  airbike: (root, args, q) => (args[0] === 'p' ? hudScreen(root, args.slice(1), q) : airbikeScreen(root)),
  spin: stubScreen('spin'),
  treadmill: stubScreen('treadmill'),
  strength: liftsScreen,
  settings: settingsScreen,
};

const root = document.getElementById('app')!;
let cleanup: (() => void) | void;
let routeKey = '';

export type LeaveGuard = (targetHash: string) => Promise<boolean>;
let leaveGuard: LeaveGuard | null = null;
let bypassGuard = false;
let guardBusy = false;

/** Register an async confirmation guard before leaving the active route (e.g. running workout). */
export function setLeaveGuard(fn: LeaveGuard | null): void {
  leaveGuard = fn;
}

/** Allow the next programmatic hash navigation to bypass the active leave guard. */
export function allowNextRoute(): void {
  bypassGuard = true;
}

function route(): void {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const [name = '', ...args] = path.split('/').filter(Boolean);
  const key = path;
  if (key === routeKey && cleanup) return;
  if (bypassGuard) {
    bypassGuard = false;
  } else if (leaveGuard && routeKey) {
    const restoreHash = `#/${routeKey}`;
    const targetHash = `#/${raw}`;
    history.pushState(null, '', restoreHash);
    if (!guardBusy) {
      guardBusy = true;
      void leaveGuard(targetHash).then((allow) => {
        guardBusy = false;
        if (allow) {
          leaveGuard = null;
          bypassGuard = true;
          location.hash = targetHash;
        }
      });
    }
    return;
  }
  leaveGuard = null;
  routeKey = key;
  cleanup?.();
  document.querySelectorAll<HTMLElement>('.lmodal, [data-exit-sheet]').forEach((m) => m.dispatchEvent(new CustomEvent('armory:dismiss')));
  syncModalInert();
  // The bike link lives across every AirBike screen (list <-> workout); only leaving AirBike drops it.
  if (name !== 'airbike') releaseBike();
  root.className = 'screen';
  root.innerHTML = '';
  window.scrollTo(0, 0);
  cleanup = (routes[name] ?? routes[''])(root, args, new URLSearchParams(qs));
}

function promptExit(): void {
  if (document.querySelector('.modal[data-exit-sheet]')) return;
  const el = document.createElement('div');
  el.className = 'modal';
  el.dataset.exitSheet = '1';
  el.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">
    <h2>Exit Armory Fit?</h2>
    <p>Are you sure you want to exit?</p>
    <button class="btn primary big" type="button" data-exit="0">No</button>
    <button class="btn danger big" type="button" data-exit="1">Yes</button>
  </div>`;
  let closed = false;
  let offBackdrop = () => {};
  const close = (): void => {
    if (closed) return;
    closed = true;
    offBackdrop();
    el.remove();
    syncModalInert();
  };
  offBackdrop = wireModalBackdrop(el, close);
  el.addEventListener('click', (e) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('[data-exit]');
    if (!b) return;
    close();
    if (b.dataset.exit === '1') void App.exitApp();
  });
  document.body.appendChild(el);
  syncModalInert();
}

initSetupLinks();
// Coming back to the app (or the phone waking up) retries a stalled sensor link immediately.
document.addEventListener('visibilitychange', () => { if (!document.hidden) kickSensors(); });
window.addEventListener('hashchange', route);
if (isNative()) {
  void App.addListener('appStateChange', ({ isActive }) => {
    if (isActive) {
      kickSensors();
      calendar.checkDayRollover(true);
    }
  });
  void App.addListener('backButton', () => {
    const modals = document.querySelectorAll<HTMLElement>('.lmodal:not([hidden]), .modal:not([hidden])');
    const topModal = modals[modals.length - 1];
    if (topModal) {
      topModal.dispatchEvent(new CustomEvent('armory:dismiss'));
      return;
    }
    const backToday = document.querySelector<HTMLElement>('[data-back-today]');
    if (backToday) {
      backToday.click();
      return;
    }
    const up = parentHash(routeKey);
    if (up === null) {
      promptExit();
      return;
    }
    location.hash = up;
  });
}
route();

// Retry any workouts that were saved while offline / before the API key was set.
const retry = () => { void retryPending().catch(() => {}); };
window.addEventListener('online', retry);
setTimeout(retry, 2500);

if ('serviceWorker' in navigator && location.protocol !== 'file:' && !import.meta.env.DEV && !isNative()) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
