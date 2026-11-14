import './style.css';
import { retryPending } from './workout/sync';
import { airbikeScreen, stubScreen } from './ui/equipment';
import { homeScreen } from './ui/home';
import { hudScreen } from './ui/hud';
import { settingsScreen } from './ui/settings';

type Screen = (root: HTMLElement, args: string[], query: URLSearchParams) => (() => void) | void;

const routes: Record<string, Screen> = {
  '': homeScreen,
  airbike: (root, args, q) => (args[0] === 'p' ? hudScreen(root, args.slice(1), q) : airbikeScreen(root)),
  spin: stubScreen('spin'),
  treadmill: stubScreen('treadmill'),
  strength: stubScreen('strength'),
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
  root.className = 'screen';
  root.innerHTML = '';
  window.scrollTo(0, 0);
  cleanup = (routes[name] ?? routes[''])(root, args, new URLSearchParams(qs));
}

window.addEventListener('hashchange', route);
route();

// Retry any workouts that were saved while offline / before the API key was set.
const retry = () => { void retryPending().catch(() => {}); };
window.addEventListener('online', retry);
setTimeout(retry, 2500);

if ('serviceWorker' in navigator && location.protocol !== 'file:' && !import.meta.env.DEV) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
