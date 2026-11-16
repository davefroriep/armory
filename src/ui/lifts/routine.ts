import { lifts } from '../../lifts/store';
import { uid, type Routine } from '../../lifts/types';
import { esc } from '../dom';
import { LI, confirmSheet, mount, toast } from './kit';
import { pickExercises } from './picker';

/** Build / edit a repeating training day. Nothing is pre-programmed; this is only what you put in. */
export function routineScreen(root: HTMLElement, args: string[]): () => void {
  return mount(() => {
    const isNew = args[0] === 'new';
    const existing = isNew ? undefined : lifts.routines.get(args[0]);
    if (!isNew && !existing) {
      location.replace('#/strength');
      return;
    }
    const draft: Routine = existing
      ? { ...existing, items: existing.items.map((i) => ({ ...i })) }
      : { id: uid(), name: '', items: [], createdMs: Date.now(), order: lifts.routines.size };

    const paint = (): void => {
      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>${isNew ? 'New day' : 'Edit day'}</h1>
        <div class="chips"><button class="btn sm primary" data-act="save" ${draft.name.trim() && draft.items.length ? '' : 'disabled'}>Save</button></div></header>
        <main class="page">
          <label class="field">DAY NAME<input class="lf-input" id="rn" value="${esc(draft.name)}" placeholder="Upper 1, Lower 2, Push…" autocomplete="off"></label>
          <div class="sec-row"><h2 class="sec">EXERCISES</h2></div>
          ${draft.items.map((it, i) => {
            const ex = lifts.exOrStub(it.exId);
            return `<section class="ritem" data-i="${i}">
              <div class="ri-h"><span class="xlab">${i + 1}</span><b>${esc(ex.name)}</b><button class="xmore" data-act="rm" aria-label="Remove">${LI.trash}</button></div>
              <div class="ri-c">
                <span class="stepper"><button class="stp" data-act="minus">−</button><b>${it.sets}</b><button class="stp" data-act="plus">+</button><small>${ex.type === 'time' ? 'holds' : 'sets'}</small></span>
                <button class="chipbtn${it.ss ? ' on' : ''}" data-act="ss" ${i === draft.items.length - 1 ? 'disabled' : ''}>Superset ↓</button>
                <button class="stp" data-act="up" ${i === 0 ? 'disabled' : ''}>↑</button><button class="stp" data-act="down" ${i === draft.items.length - 1 ? 'disabled' : ''}>↓</button>
              </div></section>`;
          }).join('') || '<div class="empty">Add the exercises for this day.</div>'}
          <button class="btn big addex" data-act="add">+ Add exercise</button>
          ${isNew ? '' : '<button class="btn danger big" data-act="del">Delete day</button>'}
        </main>`;
    };
    paint();

    root.addEventListener('input', (e) => {
      const t = e.target as HTMLInputElement;
      if (t.id !== 'rn') return;
      draft.name = t.value;
      const save = root.querySelector<HTMLButtonElement>('[data-act="save"]');
      if (save) save.disabled = !(draft.name.trim() && draft.items.length);
    });
    root.addEventListener('click', async (e) => {
      const a = (e.target as HTMLElement).closest<HTMLElement>('[data-act]');
      if (!a) return;
      const act = a.dataset.act!;
      const i = Number(a.closest<HTMLElement>('[data-i]')?.dataset.i ?? -1);
      const it = draft.items[i];
      if (act === 'add') {
        const picks = await pickExercises({ mode: 'add' });
        picks.forEach((p) => draft.items.push({ exId: p.id, sets: p.type === 'time' ? 2 : 3 }));
        paint();
      } else if (act === 'plus' && it) { it.sets = Math.min(12, it.sets + 1); paint(); }
      else if (act === 'minus' && it) { it.sets = Math.max(1, it.sets - 1); paint(); }
      else if (act === 'ss' && it) { if (it.ss) delete it.ss; else it.ss = true; paint(); }
      else if ((act === 'up' || act === 'down') && it) {
        const j = act === 'up' ? i - 1 : i + 1;
        [draft.items[i], draft.items[j]] = [draft.items[j], draft.items[i]];
        delete draft.items[draft.items.length - 1].ss;
        paint();
      } else if (act === 'rm' && it) { draft.items.splice(i, 1); if (draft.items.length) delete draft.items[draft.items.length - 1].ss; paint(); }
      else if (act === 'save') {
        draft.name = draft.name.trim();
        await lifts.saveRoutine(draft);
        toast(`${draft.name} saved`);
        location.hash = '#/strength';
      } else if (act === 'del' && existing && (await confirmSheet(`Delete “${existing.name}”?`, 'Past workouts stay in History.', 'Delete day', true))) {
        await lifts.deleteRoutine(existing.id);
        location.hash = '#/strength';
      }
    });
  });
}
