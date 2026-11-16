import './style.css';
import { retryPending } from './workout/sync';
import { airbikeScreen, stubScreen } from './ui/equipment';
import { homeScreen } from './ui/home';
import { hudScreen } from './ui/hud';
import { settingsScreen } from './ui/settings';
import { liftsScreen } from './ui/lifts';
import { initSetupLinks } from './setup';
import { isNative } from './native/service';
import { kickSensors, releaseBike } from './ble/sources';

type Screen = (root: HTMLElement, args: string[], query: URLSearchParams) => (() => void) | void;

const routes: Record<string, Screen> = {
  '': homeScreen,
  airbike: (root, args, q) => (args[0] === 'p' ? hudScreen(root, args.slice(1), q) : airbikeScreen(root)),
  spin: stubScreen('spin'),
  treadmill: stubScreen('treadmill'),
  strength: liftsScreen,
  settings: settingsScreen,
};

const root = document.getElementById('app')!;
let cleanup: (() => void) | void;
let routeKey = '';

function route(): void {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const [name = '', ...args] = path.split('/').filter(Boolean);
  const key = path;
  if (key === routeKey && cleanup) return;
  routeKey = key;
  cleanup?.();
  // The bike link lives across every AirBike screen (list <-> workout); only leaving AirBike drops it.
  if (name !== 'airbike') releaseBike();
  root.className = 'screen';
  root.innerHTML = '';
  window.scrollTo(0, 0);
  cleanup = (routes[name] ?? routes[''])(root, args, new URLSearchParams(qs));
}

initSetupLinks();
// Coming back to the app (or the phone waking up) retries a stalled sensor link immediately.
document.addEventListener('visibilitychange', () => { if (!document.hidden) kickSensors(); });
window.addEventListener('hashchange', route);
route();

// Retry any workouts that were saved while offline / before the API key was set.
const retry = () => { void retryPending().catch(() => {}); };
window.addEventListener('online', retry);
setTimeout(retry, 2500);

if ('serviceWorker' in navigator && location.protocol !== 'file:' && !import.meta.env.DEV && !isNative()) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
