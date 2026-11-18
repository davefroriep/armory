import { ensureSensors } from '../../ble/sources';
import { PHASE1_PLAN, parseExtraKey, parseSlotKey } from '../../domain/plan';
import { calendar } from '../../intervals/calendar';
import { active } from '../../lifts/active';
import { buildExercise, isSessionStarted } from '../../lifts/logic';
import { matchRoutineByTitle } from '../../lifts/seed';
import { liftSettings } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import type { Routine, RoutineItem } from '../../lifts/types';
import { getSettings, settingsChanged } from '../../store/settings';
import { esc } from '../dom';
import { equipmentProgramCard, revFooter, wireCalendar, wireChips } from '../chrome';
import { openStepSheet } from '../program';
import { LI, actionSheet, confirmSheet, mount, relDay, tabBar, toast } from './kit';

/** Start a routine (or an empty workout) and open the session screen. */
export async function startWorkout(o: { name: string; routine?: Routine; items?: RoutineItem[]; eventId?: number | null; planSlot?: string | null; replace?: boolean }): Promise<void> {
  if (lifts.active) {
    if (!isSessionStarted(lifts.active)) {
      await active.discard();
    } else {
      if (o.replace) location.replace('#/strength/session');
      else {
        location.hash = '#/strength/session';
        toast('Finish or discard the workout in progress first');
      }
      return;
    }
  }
  const items = o.items ?? o.routine?.items ?? [];
  const unit = liftSettings().unit;
  const exs = items.flatMap((it, i) => {
    const ex = lifts.exercise(it.exId);
    return ex
      ? [buildExercise(it.exId, it.sets, lifts.previous(it.exId), !!it.ss, o.routine ? i : undefined, { rx: it.rx, ex, base1RmKg: lifts.bestE1rm(it.exId), unit })]
      : [];
  });
  if (exs.length) delete exs[exs.length - 1].ss;
  await active.start({ name: o.name, routineId: o.routine?.id ?? null, eventId: o.eventId ?? null, planSlot: o.planSlot ?? null, exs });
  if (o.replace) location.replace('#/strength/session');
  else location.hash = '#/strength/session';
}

const lastDone = (r: Routine): number | null => {
  let best: number | null = null;
  for (const s of lifts.finished()) if (s.routineId === r.id && (best == null || s.startMs > best)) best = s.startMs;
  return best;
};

export function trainScreen(root: HTMLElement, query: URLSearchParams): () => void {
  ensureSensors({ bike: false });
  void calendar.refresh();
  const pickParam = query.get('pick');
  const autoStart = query.get('start') === '1' && !!pickParam;

  return mount(() => {
    let dead = false;
    let autoStarted = false;
    let templatesOpen = false;

    const paint = (): void => {
      if (dead) return;
      const act = lifts.active;
      const running = act && isSessionStarted(act) ? act : undefined;
      const routines = lifts.sortedRoutines();
      const planned = calendar.forEquipment('strength');
      const plannedHtml = equipmentProgramCard('strength', planned, 'strength');

      const days = routines.length
        ? routines.map((r) => {
            const names = r.items.map((i) => lifts.exOrStub(i.exId).name);
            const ld = lastDone(r);
            return `<section class="rcard" data-rid="${r.id}">
              <div class="rc-h"><b>${esc(r.name)}</b><button class="xmore" data-act="rmenu" aria-label="Day options">⋯</button></div>
              <div class="rc-ex">${esc(names.slice(0, 4).join(' · '))}${names.length > 4 ? ` · +${names.length - 4}` : ''}</div>
              <div class="rc-f"><small>${r.items.length} exercises${ld ? ` · last ${relDay(ld).toLowerCase()}` : ''}</small><button class="btn sm primary" data-act="start">Start</button></div></section>`;
          }).join('')
        : `<div class="empty">No templates yet — add one or finish a custom workout and save it as a day.</div>`;

      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Strength</h1>
          <div class="chips"><button class="chip" data-chip="hr"></button><a class="icon-btn" href="#/strength/settings" aria-label="Lift settings">${LI.gear}</a></div></header>
        <main class="page has-tabs">
          ${plannedHtml}
          <button class="custom-wk" type="button" data-act="custom">
            <span class="cw-ico">${LI.plus}</span>
            <span class="cw-txt">
              <b>Custom Workout</b>
              <small>Freestyle — add exercises &amp; sets live as you go</small>
            </span>
            <em class="cw-cta">${running && !running.routineId ? 'Open ›' : 'Start ›'}</em>
          </button>
          <details class="hid tpl-drawer" id="tplDrawer"${templatesOpen ? ' open' : ''}>
            <summary>DAY TEMPLATES · ${routines.length}</summary>
            <div class="tpl-head"><span class="hint">Reusable lifting templates</span><a class="link" href="#/strength/routine/new">+ New day</a></div>
            <div class="tpl-list">${days}</div>
          </details>
          <a class="hr-only" href="#/strength/hr">Ruck / outdoor HR session <small>(Polar H10 + pack weight)</small> ›</a>
          ${revFooter()}
        </main>${tabBar('train')}`;
      if (pickParam != null) {
        for (const el of root.querySelectorAll<HTMLElement>('[data-plan-card]')) {
          if (el.dataset.planCard === pickParam) el.classList.add('hot');
        }
      }
    };
    paint();

    if (autoStart && pickParam) {
      void (async () => {
        if (autoStarted) return;
        autoStarted = true;
        if (lifts.active && isSessionStarted(lifts.active)) { location.replace('#/strength/session'); return; }
        if (dead) return;
        const exId = parseExtraKey(pickParam);
        if (exId) {
          const ex = getSettings().queue.extras?.find((x) => x.id === exId);
          if (ex) await startWorkout({ name: ex.name, items: ex.items, planSlot: pickParam, replace: true });
          return;
        }
        const parsed = parseSlotKey(pickParam);
        const slot = parsed ? PHASE1_PLAN.steps[parsed.stepIndex]?.slots[parsed.slotIndex] : undefined;
        if (!slot) return;
        const r = matchRoutineByTitle(slot.name, lifts.sortedRoutines());
        await startWorkout({ name: r?.name ?? slot.name, routine: r, planSlot: pickParam, replace: true });
      })();
    }

    const offCal = wireCalendar(root, paint);
    const offChips = wireChips(root);
    const offStore = lifts.changed.on(paint);
    const offSet = settingsChanged.on(paint);

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      if (t.closest('#tplDrawer > summary')) {
        setTimeout(() => { templatesOpen = !!root.querySelector<HTMLDetailsElement>('#tplDrawer')?.open; }, 0);
        return;
      }
      const peek = t.closest<HTMLElement>('[data-step-peek]');
      if (peek) return void (await openStepSheet(Number(peek.dataset.stepPeek)));
      if (t.closest('[data-act="custom"]')) {
        if (!lifts.active) return void (await startWorkout({ name: 'Custom Workout' }));
        if (!lifts.active.routineId) { location.hash = '#/strength/session'; return; }
        if (!isSessionStarted(lifts.active)) {
          await active.discard();
          return void (await startWorkout({ name: 'Custom Workout' }));
        }
        const curName = lifts.active.name;
        const ok = await confirmSheet(
          'Workout in progress',
          `“${curName}” is currently active. Discard it to start a fresh Custom Workout, or resume ${curName}?`,
          'Discard & start Custom',
          true,
          `Resume ${curName}`,
        );
        if (ok) {
          await active.discard();
          await startWorkout({ name: 'Custom Workout' });
        } else {
          location.hash = '#/strength/session';
        }
        return;
      }
      if (t.closest('[data-cal-menu]')) return;
      const plan = t.closest<HTMLElement>('[data-plan], [data-plan-card]');
      if (plan) {
        const pick = plan.dataset.plan ?? plan.dataset.planCard ?? '';
        const it = calendar.items.find((i) => i.planSlot === pick);
        if (!it) return;
        if (it.items?.length) {
          return void (await startWorkout({ name: it.slot.name, items: it.items, planSlot: it.planSlot }));
        }
        const r = matchRoutineByTitle(it.slot.name, lifts.sortedRoutines());
        return void (await startWorkout({ name: r?.name ?? it.slot.name, routine: r, planSlot: it.planSlot }));
      }
      const card = t.closest<HTMLElement>('[data-rid]');
      if (!card) return;
      const r = lifts.routines.get(card.dataset.rid!)!;
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
        return;
      }
      const match = calendar.forEquipment('strength').find((it) => !it.items?.length && matchRoutineByTitle(it.slot.name, [r]));
      await startWorkout({ name: r.name, routine: r, ...(match ? { planSlot: match.planSlot } : {}) });
    });

    return () => { dead = true; offCal(); offChips(); offStore(); offSet(); };
  });
}
