import { ICON, esc } from '../dom';
import { active } from '../../lifts/active';
import { lifts } from '../../lifts/store';

// ---------- icons ----------
const svg = (p: string): string => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
export const LI = {
  train: ICON.strength,
  history: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  progress: svg('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'),
  list: svg('<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>'),
  video: svg('<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3z"/>'),
  share: svg('<path d="M12 3v12M7 8l5-5 5 5M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"/>'),
  check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  trophy: svg('<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0zM17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  more: svg('<circle cx="5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="19" cy="12" r="1.2" fill="currentColor"/>'),
  trash: svg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  gear: ICON.gear,
};

// ---------- tab bar ----------
export type LiftTab = 'train' | 'history' | 'progress' | 'exercises';
const TABS: [LiftTab, string, string, string][] = [
  ['train', 'Train', '#/strength', LI.train],
  ['history', 'History', '#/strength/history', LI.history],
  ['progress', 'Progress', '#/strength/progress', LI.progress],
  ['exercises', 'Exercises', '#/strength/exercises', LI.list],
];

export const tabBar = (cur: LiftTab): string =>
  `<nav class="ltabs">${TABS.map(([id, label, href, ico]) => `<a href="${href}" class="${id === cur ? 'on' : ''}">${ico}<span>${label}</span></a>`).join('')}</nav>`;

const mmss = (sec: number): string => {
  const s = Math.max(0, Math.floor(sec));
  return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};
export { mmss as clock };

/** "Workout in progress" strip shown above the tab bar on every lift screen except the session itself. */
export function resumeBar(): string {
  const s = lifts.active;
  if (!s) return '';
  return `<a class="resume-bar" href="#/strength/session"><i class="rec-dot"></i><b>${esc(s.name)}</b><span data-clock>${mmss(active.elapsedSec())}</span><em>Resume ›</em></a>`;
}

/** Keeps `[data-clock]` live. Returns cleanup. */
export function wireClock(root: HTMLElement): () => void {
  const off = active.tick.on(() => root.querySelectorAll<HTMLElement>('[data-clock]').forEach((e) => { e.textContent = mmss(active.elapsedSec()); }));
  return off;
}

// ---------- sheets ----------
export interface Sheet {
  el: HTMLElement;
  body: HTMLElement;
  close: () => void;
}

export function openSheet(html: string, o: { full?: boolean; onClose?: () => void } = {}): Sheet {
  const el = document.createElement('div');
  el.className = 'modal lmodal';
  el.innerHTML = `<div class="sheet lsheet${o.full ? ' full' : ''}" role="dialog">${html}</div>`;
  const body = el.firstElementChild as HTMLElement;
  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    el.remove();
    o.onClose?.();
  };
  el.addEventListener('pointerdown', (e) => { if (e.target === el) close(); });
  document.body.appendChild(el);
  return { el, body, close };
}

/** Awaitable list-of-actions sheet. Resolves with the chosen key, or null if dismissed. */
export function actionSheet(title: string, items: { key: string; label: string; sub?: string; danger?: boolean; on?: boolean }[]): Promise<string | null> {
  return new Promise((resolve) => {
    let picked: string | null = null;
    const sh = openSheet(
      `${title ? `<h3 class="sh-title">${esc(title)}</h3>` : ''}<div class="alist">${items
        .map((i) => `<button class="aitem${i.danger ? ' danger' : ''}${i.on ? ' on' : ''}" data-k="${esc(i.key)}"><b>${esc(i.label)}</b>${i.sub ? `<small>${esc(i.sub)}</small>` : ''}</button>`)
        .join('')}</div><button class="btn big" data-k="">Cancel</button>`,
      { onClose: () => resolve(picked) },
    );
    sh.body.addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]');
      if (!b) return;
      picked = b.dataset.k || null;
      sh.close();
    });
  });
}

export function confirmSheet(title: string, text: string, ok: string, danger = false, cancel = 'Cancel'): Promise<boolean> {
  return new Promise((resolve) => {
    let yes = false;
    const sh = openSheet(
      `<h3 class="sh-title">${esc(title)}</h3>${text ? `<p class="sh-text">${esc(text)}</p>` : ''}
       <button class="btn ${danger ? 'danger' : 'primary'} big" data-y="1">${esc(ok)}</button>
       <button class="btn big" data-y="0">${esc(cancel)}</button>`,
      { onClose: () => resolve(yes) },
    );
    sh.body.addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-y]');
      if (!b) return;
      yes = b.dataset.y === '1';
      sh.close();
    });
  });
}

export function promptSheet(title: string, value = '', placeholder = '', multiline = false): Promise<string | null> {
  return new Promise((resolve) => {
    let out: string | null = null;
    const field = multiline
      ? `<textarea class="lf-input" id="pv" rows="3" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`
      : `<input class="lf-input" id="pv" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" enterkeyhint="done">`;
    const sh = openSheet(
      `<h3 class="sh-title">${esc(title)}</h3>${field}<button class="btn primary big" data-ok>Save</button><button class="btn big" data-no>Cancel</button>`,
      { onClose: () => resolve(out) },
    );
    const inp = sh.body.querySelector<HTMLInputElement>('#pv')!;
    setTimeout(() => { inp.focus(); inp.select(); }, 60);
    const ok = (): void => { out = inp.value.trim(); sh.close(); };
    sh.body.querySelector('[data-ok]')!.addEventListener('click', ok);
    sh.body.querySelector('[data-no]')!.addEventListener('click', () => sh.close());
    if (!multiline) inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
  });
}

// ---------- toast ----------
let toastEl: HTMLElement | null = null;
let toastTimer: ReturnType<typeof setTimeout> | null = null;

export function toast(msg: string, o: { undo?: () => void; ms?: number; kind?: 'pr' } = {}): void {
  toastEl?.remove();
  if (toastTimer) clearTimeout(toastTimer);
  const el = document.createElement('div');
  el.className = `ltoast${o.kind === 'pr' ? ' pr' : ''}`;
  el.innerHTML = `<span>${esc(msg)}</span>${o.undo ? '<button>UNDO</button>' : ''}`;
  if (o.undo) el.querySelector('button')!.addEventListener('click', () => { o.undo!(); el.remove(); });
  document.body.appendChild(el);
  toastEl = el;
  toastTimer = setTimeout(() => el.remove(), o.ms ?? (o.undo ? 5500 : 2600));
}

export const relDay = (ms: number): string => {
  const d = new Date(ms);
  const t = new Date();
  d.setHours(0, 0, 0, 0);
  t.setHours(0, 0, 0, 0);
  const n = Math.round((t.getTime() - d.getTime()) / 86_400_000);
  if (n <= 0) return 'Today';
  if (n === 1) return 'Yesterday';
  if (n < 7) return `${n} days ago`;
  if (n < 30) return `${Math.floor(n / 7)} wk ago`;
  return new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: n > 330 ? 'numeric' : undefined });
};

/** Mount helper: waits for the data store, then builds the screen; safe against the route changing meanwhile. */
export function mount(build: () => (() => void) | void): () => void {
  let cleanup: (() => void) | void;
  let dead = false;
  void Promise.all([lifts.ready, active.init()]).then(() => { if (!dead) cleanup = build(); });
  return () => { dead = true; cleanup?.(); };
}
