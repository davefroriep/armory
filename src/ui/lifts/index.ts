import './lifts.css';
import { syncModalInert } from '../dom';
import { strengthHrScreen } from '../strength';
import { exerciseScreen } from './exercise';
import { exercisesScreen } from './exercises';
import { historyScreen, workoutScreen } from './history';
import { routineScreen } from './routine';
import { sessionScreen } from './session';
import { liftSettingsScreen } from './settings';
import { summaryScreen } from './summary';
import { trainScreen } from './train';
import { progressScreen } from './progress';

type Build = (host: HTMLElement, args: string[], query: URLSearchParams) => (() => void) | void;

const SUB: Record<string, Build> = {
  '': (h, _a, q) => trainScreen(h, q),
  history: (h) => historyScreen(h),
  progress: (h) => progressScreen(h),
  exercises: (h) => exercisesScreen(h),
  session: (h, a) => sessionScreen(h, a),
  summary: (h, a) => summaryScreen(h, a),
  workout: (h, a) => workoutScreen(h, a),
  routine: (h, a) => routineScreen(h, a),
  exercise: (h, a) => exerciseScreen(h, a),
  settings: (h) => liftSettingsScreen(h),
  hr: (h, a, q) => strengthHrScreen(h, a, q),
};

/**
 * #/strength/... : the lift tracker. Each sub-screen gets its own host element, so its event listeners die
 * with it (the shared #app root outlives every route).
 */
export function liftsScreen(root: HTMLElement, args: string[], query: URLSearchParams): () => void {
  const [sub = '', ...rest] = args;
  const host = document.createElement('div');
  host.className = 'lhost';
  root.appendChild(host);
  const cleanup = (SUB[sub] ?? SUB[''])(host, rest, query);
  return () => {
    cleanup?.();
    document.querySelectorAll('.lmodal').forEach((e) => e.dispatchEvent(new CustomEvent('armory:dismiss')));
    document.querySelectorAll('.lmodal, .camera, .ltoast').forEach((e) => e.remove());
    syncModalInert();
    document.body.classList.remove('dock-open');
    host.remove();
  };
}
