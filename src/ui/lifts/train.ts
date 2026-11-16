import { ensureSensors } from '../../ble/sources';
import { calendar } from '../../intervals/calendar';
import { active } from '../../lifts/active';
import { buildExercise } from '../../lifts/logic';
import { lifts } from '../../lifts/store';
import type { Routine, RoutineItem } from '../../lifts/types';
import { ICON, esc } from '../dom';
import { revFooter, wireCalendar, wireChips } from '../chrome';
import { LI, actionSheet, clock, confirmSheet, mount, relDay, tabBar, toast, wireClock } from './kit';

/** Start a routine (or an empty workout) and open the session screen. */
export async function startWorkout(o: { name: string; routine?: Routine; items?: RoutineItem[]; eventId?: number | null }): Promise<void> {
  if (lifts.active) {
    location.hash = '#/strength/session';
    toast('Finish or discard the workout in progress first');
    return;
  }
  const items = o.items ?? o.routine?.items ?? [];
  const exs = items.flatMap((it, i) =>
    lifts.exercise(it.exId)
      ? [buildExercise(it.exId, it.sets, lifts.previous(it.exId), !!it.ss, o.routine ? i : undefined)]
      : [],
  );
  if (exs.length) delete exs[exs.length - 1].ss;
  await active.start({ name: o.name, routineId: o.routine?.id ?? null, eventId: o.eventId ?? null, exs });
  location.hash = '#/strength/session';
}

const lastDone = (r: Routine): number | null => {
  let best: number | null = null;
  for (const s of lifts.finished()) if (s.routineId === r.id && (best == null || s.startMs > best)) best = s.startMs;
  return best;
};

/** Planned-event title → the user's routine whose name appears in it ("... Day 2 Upper 1" → "Upper 1"). */
function routineForEvent(name: string): Routine | undefined {
  const n = name.toLowerCase();
  return lifts.sortedRoutines().filter((r) => n.includes(r.name.toLowerCase())).sort((a, b) => b.name.length - a.name.length)[0];
}

export function trainScreen(root: HTMLElement, query: URLSearchParams): () => void {
  ensureSensors({ bike: false });
  void calendar.refresh();
  const evParam = query.get('ev') ? Number(query.get('ev')) : null;

  return mount(() => {
    const paint = (): void => {
      const act = lifts.active;
      const routines = lifts.sortedRoutines();
      const planned = calendar.forEquipment('strength');

      const resume = act
        ? `<a class="card resume" href="#/strength/session"><i class="rec-dot"></i><span><b>${esc(act.name)}</b><small>Workout in progress · <span data-clock>${clock(active.elapsedSec())}</span></small></span><em>Resume ›</em></a>`
        : '';

      const plannedHtml = calendar.status === 'nokey'
        ? ''
        : `<section class="card cal"><div class="card-h"><span>TODAY · INTERVALS.ICU</span><button class="link" data-refresh>Refresh</button></div>
          ${planned.length
            ? planned.map((it) => {
                const r = routineForEvent(it.ev.name ?? '');
                const mins = it.ev.moving_time ? Math.round(it.ev.moving_time / 60) : 0;
                return `<div class="cal-item${evParam === it.ev.id ? ' hot' : ''}"><span class="cal-ico strength">${ICON.strength}</span>
                  <span class="cal-txt"><b>${esc(it.ev.name ?? 'Strength')}</b><small>${r ? `Your “${esc(r.name)}” day` : 'No matching day yet'}${mins ? ` · ${mins} min` : ''}</small></span>
                  <button class="btn sm primary" data-plan="${it.ev.id}">Start</button></div>`;
              }).join('')
            : `<div class="cal-empty">${calendar.status === 'loading' ? 'Loading today’s plan…' : 'Nothing strength-related planned today'}</div>`}
          ${calendar.status === 'error' ? `<div class="cal-note">${esc(calendar.error)}</div>` : ''}</section>`;

      const days = routines.length
        ? routines.map((r) => {
            const names = r.items.map((i) => lifts.exOrStub(i.exId).name);
            const ld = lastDone(r);
            return `<section class="rcard" data-rid="${r.id}">
              <div class="rc-h"><b>${esc(r.name)}</b><button class="xmore" data-act="rmenu" aria-label="Day options">⋯</button></div>
              <div class="rc-ex">${esc(names.slice(0, 4).join(' · '))}${names.length > 4 ? ` · +${names.length - 4}` : ''}</div>
              <div class="rc-f"><small>${r.items.length} exercises${ld ? ` · last ${relDay(ld).toLowerCase()}` : ''}</small><button class="btn sm primary" data-act="start">Start</button></div></section>`;
          }).join('')
        : `<div class="card empty-card"><b>No days yet</b><p>A day is a repeating workout, like “Upper 1”. Build one, or run a workout and save it as a day when you finish.</p></div>`;

      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Strength</h1>
          <div class="chips"><button class="chip" data-chip="hr"></button><a class="icon-btn" href="#/strength/settings" aria-label="Lift settings">${LI.gear}</a></div></header>
        <main class="page has-tabs">
          ${resume}${act ? '' : plannedHtml}
          ${act ? '' : '<button class="btn primary big" data-act="empty">Start empty workout</button>'}
          <div class="sec-row"><h2 class="sec">MY DAYS</h2><a class="link" href="#/strength/routine/new">+ New day</a></div>
          ${days}
          <a class="hr-only" href="#/strength/hr">Heart-rate only session <small>(lift in another app)</small> ›</a>
          ${revFooter()}
        </main>${tabBar('train')}`;
    };
    paint();

    const offCal = wireCalendar(root, paint);
    const offChips = wireChips(root);
    const offStore = lifts.changed.on(paint);
    const offClock = wireClock(root);

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      if (t.closest('[data-act="empty"]')) return void (await startWorkout({ name: 'Workout' }));
      const plan = t.closest<HTMLElement>('[data-plan]');
      if (plan) {
        const it = calendar.items.find((i) => i.ev.id === Number(plan.dataset.plan));
        if (!it) return;
        const r = routineForEvent(it.ev.name ?? '');
        return void (await startWorkout({ name: r?.name ?? it.ev.name ?? 'Strength', routine: r, eventId: it.ev.id }));
      }
      const card = t.closest<HTMLElement>('[data-rid]');
      if (!card) return;
      const r = lifts.routines.get(card.dataset.rid!)!;
      if (t.closest('[data-act="start"]')) return void (await startWorkout({ name: r.name, routine: r }));
      if (t.closest('[data-act="rmenu"]')) {
        const k = await actionSheet(r.name, [
          { key: 'edit', label: 'Edit day' },
          { key: 'dup', label: 'Duplicate' },
          { key: 'up', label: 'Move up' },
          { key: 'down', label: 'Move down' },
          { key: 'del', label: 'Delete day', danger: true },
        ]);
        if (k === 'edit') location.hash = `#/strength/routine/${r.id}`;
        else if (k === 'dup') await lifts.saveRoutine({ ...r, id: `${r.id}-${Date.now().toString(36)}`, name: `${r.name} copy`, order: r.order + 0.5, createdMs: Date.now(), items: r.items.map((i) => ({ ...i })) });
        else if (k === 'up' || k === 'down') {
          const list = lifts.sortedRoutines();
          const i = list.findIndex((x) => x.id === r.id);
          const j = k === 'up' ? i - 1 : i + 1;
          if (j >= 0 && j < list.length) {
            [list[i], list[j]] = [list[j], list[i]];
            for (const [n, x] of list.entries()) await lifts.saveRoutine({ ...x, order: n });
          }
        } else if (k === 'del' && (await confirmSheet(`Delete “${r.name}”?`, 'Past workouts stay in History.', 'Delete day', true))) {
          await lifts.deleteRoutine(r.id);
        }
      }
    });

    return () => { offCal(); offChips(); offStore(); offClock(); };
  });
}
