import { exerciseSessions } from '../../lifts/calc';
import { EQUIP_LABEL, GROUPS, MUSCLE_LABEL } from '../../lifts/muscles';
import { lifts } from '../../lifts/store';
import { LIBRARY_BY_ID } from '../../lifts/library';
import type { Exercise } from '../../lifts/types';
import { esc } from '../dom';
import { mount, relDay, resumeBar, tabBar, toast, wireClock } from './kit';
import { exerciseForm } from './picker';

/** The master list: library + your custom lifts, with how often you do each. */
export function exercisesScreen(root: HTMLElement): () => void {
  return mount(() => {
    let q = '';
    let group = '';
    let mine = false;

    root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Exercises</h1>
        <div class="chips"><button class="btn sm" data-new>+ Custom</button></div></header>
      <main class="page has-tabs">
        <input class="lf-input" id="eq" placeholder="Search exercises" autocomplete="off">
        <div class="chips-row" id="eg"></div>
        <div id="el" class="elist"></div>
      </main><div id="rb">${resumeBar()}</div>${tabBar('exercises')}`;
    const $l = root.querySelector<HTMLElement>('#el')!;
    const $g = root.querySelector<HTMLElement>('#eg')!;
    const $rb = root.querySelector<HTMLElement>('#rb')!;

    const counts = (): Map<string, { n: number; last: number }> => {
      const m = new Map<string, { n: number; last: number }>();
      const fin = lifts.finished();
      for (const e of lifts.allExercises(true)) {
        const h = exerciseSessions(fin, e.id);
        if (h.length) m.set(e.id, { n: h.length, last: h[h.length - 1].s.startMs });
      }
      return m;
    };

    const drawChips = (): void => {
      const c = (k: string, l: string, on: boolean): string => `<button class="fchip${on ? ' on' : ''}" data-k="${k}">${l}</button>`;
      $g.innerHTML = c('', 'All', !group && !mine) + c('mine', 'Done before', mine) + GROUPS.map((g) => c(g.id, g.label, group === g.id)).join('');
    };
    const draw = (): void => {
      const cn = counts();
      const g = GROUPS.find((x) => x.id === group);
      const list = lifts.allExercises().filter((e: Exercise) => {
        if (q && !((LIBRARY_BY_ID.get(e.id)?.haystack ?? e.name.toLowerCase()) + ' ' + e.name.toLowerCase()).includes(q)) return false;
        if (g && !e.primary.some((m) => g.muscles.includes(m))) return false;
        if (mine && !cn.has(e.id)) return false;
        return true;
      });
      $l.innerHTML = list.length
        ? list.map((e) => {
            const c = cn.get(e.id);
            return `<a class="erow" href="#/strength/exercise/${e.id}"><span class="pk-main"><b>${esc(e.name)}${e.custom ? ' <em>CUSTOM</em>' : ''}</b><small>${esc(e.primary.map((m) => MUSCLE_LABEL[m]).join(' · '))} · ${EQUIP_LABEL[e.equip]}</small></span><span class="ecount">${c ? `<b>${c.n}×</b><small>${relDay(c.last).toLowerCase()}</small>` : ''}</span><span class="chev">›</span></a>`;
          }).join('')
        : '<div class="empty">No match. Tap + Custom to add it.</div>';
    };
    drawChips();
    draw();

    const offClock = wireClock(root);
    const offStore = lifts.changed.on(() => { draw(); $rb.innerHTML = resumeBar(); });
    root.querySelector('#eq')!.addEventListener('input', (e) => { q = (e.target as HTMLInputElement).value.trim().toLowerCase(); draw(); });
    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const k = t.closest<HTMLElement>('[data-k]');
      if (k) {
        const v = k.dataset.k!;
        if (v === 'mine') { mine = !mine; group = ''; } else { group = v; mine = false; }
        drawChips(); draw();
        return;
      }
      if (t.closest('[data-new]')) {
        const ex = await exerciseForm(null, q ? q.replace(/\b\w/g, (c) => c.toUpperCase()) : '');
        if (ex) { toast(`${ex.name} created`); draw(); }
      }
    });
    return () => { offClock(); offStore(); };
  });
}
