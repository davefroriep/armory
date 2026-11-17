import {
  PHASE1_PLAN, WEEK_THEMES, extraKey, parseYmd, projectPlan, slotKey,
  type ProjectedStep,
} from '../domain/plan';
import { addDays, calendar, localDate } from '../intervals/calendar';
import { matchRoutineByTitle } from '../lifts/seed';
import { liftSettings } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { allMeta } from '../store/db';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { revFooter, topbar, wireChips } from './chrome';
import {
  localYmdFromMs, renderCompletedCardioSlot, renderCompletedLiftSlot,
  renderPlannedSlot, resolveDayZoom, type ZoomLookup,
} from './dayZoom';
import { ICON, esc } from './dom';
import { actionSheet, confirmSheet, mount, openSheet, toast } from './lifts/kit';
import { startWorkout } from './lifts/train';

const DAYS_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const DAYS_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export const restDayLabel = (d: number | null, full = false): string =>
  d == null ? (full ? 'No fixed rest day' : 'None') : (full ? DAYS_FULL[d] : DAYS_SHORT[d]);

export function fmtShortDate(ymd: string, today = localDate()): string {
  if (ymd === today) return 'Today';
  if (ymd === addDays(today, 1)) return 'Tomorrow';
  const { y, m, d } = parseYmd(ymd);
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}

/** Merges finished lifts and saved cardio workouts by local YYYY-MM-DD date. */
export async function collectLoggedByDate(): Promise<Map<string, { equipment: Equipment; name: string }[]>> {
  await lifts.ready;
  const map = new Map<string, { equipment: Equipment; name: string }[]>();
  const add = (ymd: string, equipment: Equipment, name: string): void => {
    const arr = map.get(ymd) ?? [];
    arr.push({ equipment, name });
    map.set(ymd, arr);
  };
  for (const s of lifts.finished()) {
    add(localDate(new Date(s.startMs)), 'strength', s.name);
  }
  const metas = await allMeta();
  for (const m of metas) {
    if (m.status === 'saved' && m.equipment !== 'strength') {
      add(localDate(new Date(m.startEpochMs)), m.equipment, m.name);
    }
  }
  return map;
}

function statusPillHtml(p: ProjectedStep, today: string): string {
  if (p.status === 'done') return `<span class="p-badge ok">✓ Done${p.date ? ` · ${esc(fmtShortDate(p.date, today))}` : ''}</span>`;
  if (p.status === 'skipped') return `<span class="p-badge sk">Skipped${p.date ? ` · ${esc(fmtShortDate(p.date, today))}` : ''}</span>`;
  if (p.status === 'early') return `<span class="p-badge ok">✓ Done early${p.date ? ` · ${esc(fmtShortDate(p.date, today))}` : ''}</span>`;
  if (p.status === 'current') return `<span class="p-badge cur">● Current · ${esc(p.date ? fmtShortDate(p.date, today) : 'Today')}</span>`;
  return `<span class="p-badge">~${esc(p.date ? fmtShortDate(p.date, today) : 'TBD')}</span>`;
}

export function currentZoomLookup(): ZoomLookup {
  return {
    exOf: lifts.exOrStub,
    rowOf: (id) => lifts.row(id),
    prevOf: (id) => lifts.previous(id),
    routines: lifts.sortedRoutines(),
    prs: lifts.prs(),
    unit: liftSettings().unit,
  };
}

/** Shifts the rolling queue to `stepIndex` after user confirmation. */
export async function shiftScheduleToStep(stepIndex: number): Promise<boolean> {
  const step = PHASE1_PLAN.steps[stepIndex];
  if (!step) return false;
  const ok = await confirmSheet(
    `Shift schedule to Wk ${step.week} · Day ${step.day}?`,
    'Makes this day Today and projects future weeks forward from here.',
    'Shift schedule to here',
  );
  if (!ok) return false;
  calendar.setStep(stepIndex);
  toast(`Schedule shifted to Wk ${step.week} · Day ${step.day}`);
  return true;
}

/** Safe read-only inspection sheet for any step in the 4-week block, with out-of-order controls. */
export async function openStepSheet(stepIndex: number): Promise<void> {
  const [metas] = await Promise.all([allMeta(), lifts.ready]);
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const p = proj[stepIndex];
  if (!p) return;

  const lookup = currentZoomLookup();
  const routines = lookup.routines;
  const finishedLifts = lifts.finished();
  const savedCardio = metas.filter((m) => m.status === 'saved' && m.equipment !== 'strength');

  const slotsHtml = p.step.slots.map((slot, si) => {
    const state = p.slotStates[si];
    const slotDate = p.slotDates[si] ?? p.date;
    const key = slotKey(stepIndex, si);
    if (state === 'done') {
      if (slot.equipment === 'strength') {
        const ls = finishedLifts.find((x) => x.planSlot === key || (!!slotDate && localYmdFromMs(x.startMs) === slotDate));
        if (ls) return renderCompletedLiftSlot(ls, lookup);
      } else {
        const cm = savedCardio.find((x) => x.equipment === slot.equipment && (x.planSlot === key || (!!slotDate && localYmdFromMs(x.startEpochMs) === slotDate)));
        if (cm) return renderCompletedCardioSlot(cm);
      }
    }
    return renderPlannedSlot(slot, state, lookup);
  }).join('');

  const acts: string[] = [];
  if (p.status === 'current') {
    p.step.slots.forEach((slot, si) => {
      if (p.slotStates[si] !== 'pending') return;
      const r = slot.equipment === 'strength' ? matchRoutineByTitle(slot.name, routines) : undefined;
      const lbl = slot.equipment === 'strength' ? (r?.name ?? 'Lifting') : slot.name;
      acts.push(`<button class="btn primary big" type="button" data-sact="cur:${si}">Start ${esc(lbl)}</button>`);
    });
  } else if (p.status === 'upcoming') {
    p.step.slots.forEach((slot, si) => {
      if (p.slotStates[si] !== 'pending') return;
      const r = slot.equipment === 'strength' ? matchRoutineByTitle(slot.name, routines) : undefined;
      if (slot.equipment === 'strength') {
        acts.push(`<button class="btn primary big" type="button" data-sact="early:${si}">Do ${esc(r?.name ?? 'Lifting')} Today</button>`);
      } else if (slot.equipment === 'airbike') {
        acts.push(`<button class="btn primary big" type="button" data-sact="early:${si}">Do ${esc(slot.name)} Today</button>`);
      } else {
        acts.push(`<button class="btn primary big" type="button" data-sact="ext:${si}">Mark ${esc(slot.name)} Done Today</button>`);
      }
    });
    acts.push('<button class="btn big" type="button" data-sact="move">Shift schedule to here</button>');
  } else {
    acts.push('<button class="btn big" type="button" data-sact="move">Shift schedule to here</button>');
  }
  acts.push('<button class="btn big" type="button" data-sact="close">Close</button>');

  const sh = openSheet(
    `<div class="sh-meta">${statusPillHtml(p, today)}<span class="sh-week">Wk ${p.step.week} · ${esc(WEEK_THEMES[p.step.week] ?? '')}</span></div>
     <h3 class="sh-title">${esc(p.step.title)}</h3>
     ${slotsHtml}
     <div class="sh-acts">${acts.join('')}</div>`,
    { full: p.step.slots.some((sl) => sl.equipment === 'strength') },
  );

  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('a')) { sh.close(); return; }
    const b = t.closest<HTMLElement>('[data-sact]');
    if (!b) return;
    const act = b.dataset.sact!;
    if (act === 'close') { sh.close(); return; }

    if (act.startsWith('cur:')) {
      const si = Number(act.slice(4));
      const slot = p.step.slots[si];
      const pick = slotKey(stepIndex, si);
      sh.close();
      if (slot.equipment === 'strength') {
        const r = matchRoutineByTitle(slot.name, routines);
        await startWorkout({ name: r?.name ?? slot.name, routine: r, planSlot: pick });
      } else if (slot.equipment === 'airbike') {
        location.hash = slot.programId ? `#/airbike/p/${slot.programId}?pick=${pick}` : `#/airbike?pick=${pick}`;
      } else {
        location.hash = `#/${slot.equipment}?pick=${pick}`;
      }
      return;
    }

    if (act.startsWith('early:')) {
      const si = Number(act.slice(6));
      const slot = p.step.slots[si];
      const pick = slotKey(stepIndex, si);
      sh.close();
      if (slot.equipment === 'strength') {
        const r = matchRoutineByTitle(slot.name, routines);
        await startWorkout({ name: r?.name ?? slot.name, routine: r, planSlot: pick });
      } else if (slot.equipment === 'airbike') {
        location.hash = slot.programId ? `#/airbike/p/${slot.programId}?pick=${pick}` : `#/airbike?pick=${pick}`;
      }
      return;
    }

    if (act.startsWith('ext:')) {
      const si = Number(act.slice(4));
      const slot = p.step.slots[si];
      sh.close();
      const ok = await confirmSheet(
        `Mark “${slot.name}” done today?`,
        'Logs it for today and automatically skips this slot when your rolling schedule catches up.',
        'Mark Done Today',
      );
      if (ok) {
        await calendar.completeFutureSlotExternally(stepIndex, si);
        toast('Marked done early');
      }
      return;
    }

    if (act === 'move') {
      sh.close();
      await shiftScheduleToStep(stepIndex);
    }
  });
}

/** Launches any pending workout (regular or carry-over extra) scheduled on `ymd`. */
export async function launchDayZoomWorkout(ymd: string): Promise<void> {
  await lifts.ready;
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const res = resolveDayZoom({ ymd, today, proj, extras: s.queue.extras, restDay: s.restDay });
  const routines = lifts.sortedRoutines();

  const options: { label: string; sub: string; run: () => Promise<void> }[] = [
    ...res.pendingExtras.map((x) => ({
      label: x.name,
      sub: `${EQUIPMENT_LABEL[x.equipment]} · Carry-over · ${Math.round(x.seconds / 60)} min`,
      run: async () => {
        const pick = extraKey(x.id);
        if (x.equipment === 'strength') {
          await startWorkout({ name: x.name, items: x.items, planSlot: pick });
        } else {
          location.hash = `#/${x.equipment}?pick=${encodeURIComponent(pick)}`;
        }
      },
    })),
    ...res.pendingSlots.map(({ p, slot, slotIndex }) => ({
      label: slot.equipment === 'strength'
        ? (matchRoutineByTitle(slot.name, routines)?.name ?? slot.name)
        : slot.name,
      sub: `${EQUIPMENT_LABEL[slot.equipment]} · ${Math.round(slot.seconds / 60)} min`,
      run: async () => {
        const pick = slotKey(p.step.index, slotIndex);
        if (slot.equipment === 'strength') {
          const r = matchRoutineByTitle(slot.name, routines);
          await startWorkout({ name: r?.name ?? slot.name, routine: r, planSlot: pick });
        } else if (slot.equipment === 'airbike') {
          location.hash = slot.programId ? `#/airbike/p/${slot.programId}?pick=${pick}` : `#/airbike?pick=${pick}`;
        } else if (p.status === 'current') {
          location.hash = `#/${slot.equipment}?pick=${pick}`;
        } else {
          const ok = await confirmSheet(
            `Mark “${slot.name}” done today?`,
            'Logs it for today and automatically skips this slot when your rolling schedule catches up.',
            'Mark Done Today',
          );
          if (ok) {
            await calendar.completeFutureSlotExternally(p.step.index, slotIndex);
            toast('Marked done early');
          }
        }
      },
    })),
  ];

  if (!options.length) return;
  let chosen = options[0];
  if (options.length > 1) {
    const k = await actionSheet(
      'Do which workout now?',
      options.map((opt, idx) => ({ key: String(idx), label: opt.label, sub: opt.sub })),
    );
    if (!k) return;
    chosen = options[Number(k)] ?? chosen;
  }
  await chosen.run();
}

/** 4-week zoomable block overview screen (`#/program`). */
export function programScreen(root: HTMLElement, _args: string[], query: URLSearchParams): () => void {
  const qW = Number(query.get('w'));
  let selectedWeek = qW >= 1 && qW <= 4 ? qW : 0;

  return mount(() => {
    let dead = false;
    let loggedByDate = new Map<string, { equipment: Equipment; name: string }[]>();

    const paint = (): void => {
      if (dead) return;
      const s = getSettings();
      const today = localDate();
      const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
      const curIdx = Math.min(s.queue.stepIndex, PHASE1_PLAN.steps.length - 1);
      const activeWeek = PHASE1_PLAN.steps[curIdx]?.week ?? 1;
      const w = selectedWeek >= 1 && selectedWeek <= 4 ? selectedWeek : activeWeek;
      selectedWeek = w;

      const doneCount = proj.filter((p) => p.slotStates.some((st) => st === 'done')).length;
      const total = PHASE1_PLAN.steps.length;
      const pct = Math.round((doneCount / total) * 100);

      const weekTabs = [1, 2, 3, 4].map((wk) => {
        const wkSteps = proj.filter((p) => p.step.week === wk);
        const allResolved = wkSteps.every((p) => p.slotStates.every((st) => st !== 'pending'));
        const anyDone = wkSteps.some((p) => p.slotStates.some((st) => st === 'done'));
        const hasCur = wkSteps.some((p) => p.status === 'current');
        return `<button class="p-tab${wk === w ? ' on' : ''}${allResolved && anyDone ? ' done' : ''}" type="button" data-w="${wk}">
          Wk ${wk}${allResolved && anyDone ? ' ✓' : hasCur ? ' ●' : ''}
        </button>`;
      }).join('');

      const weekSteps = proj.filter((p) => p.step.week === w);
      const wkDates = weekSteps.map((p) => p.date).filter((d): d is string => !!d).sort();
      const wkDateSpan = wkDates.length >= 2
        ? `${fmtShortDate(wkDates[0], today)} – ${fmtShortDate(wkDates[wkDates.length - 1], today)}`
        : wkDates[0]
          ? fmtShortDate(wkDates[0], today)
          : '';

      const stepsHtml = weekSteps.map((p) => {
        const cleanTitle = p.step.title.replace(/^Wk \d+ · /, '');
        const slots = p.step.slots.map((sl, si) => {
          const mins = Math.round(sl.seconds / 60);
          const st = p.slotStates[si];
          const loggedToday = p.status === 'current' && st === 'done' && loggedByDate.has(today);
          const cls = st === 'skipped' ? ' skipped' : (st === 'done' || loggedToday) ? ' done' : '';
          const tag = st === 'skipped'
            ? '<small class="sk-tag">(skipped)</small>'
            : `<small>${st === 'done' || loggedToday ? '✓' : `${mins}m`}</small>`;
          return `<div class="p-slot${cls}">
            <span class="p-sico ${sl.equipment}">${ICON[sl.equipment]}</span>
            <span class="p-sname">${esc(sl.name)}</span>
            ${tag}
          </div>`;
        }).join('');
        return `<button class="p-step ${p.status}" type="button" data-step="${p.step.index}">
          <div class="p-step-h"><b>${esc(cleanTitle)}</b>${statusPillHtml(p, today)}</div>
          <div class="p-slots">${slots}</div>
        </button>`;
      }).join('');

      root.innerHTML = `${topbar({ back: '#/', title: 'Full Program', cog: true })}
        <main class="page">
          <section class="card p-block">
            <div class="card-h"><span>ARMORY HYBRID · PHASE 1</span><button class="link" data-rest-pick>Rest: ${esc(restDayLabel(s.restDay))} ▾</button></div>
            <div class="p-prog-row"><b>${doneCount} / ${total} sessions</b><span>${pct}% · Target Jan 8 10K</span></div>
            <div class="p-bar"><u style="width:${pct}%"></u></div>
          </section>
          <div class="p-tabs">${weekTabs}</div>
          <div class="p-theme"><b>Week ${w}</b>${wkDateSpan ? ` <span class="p-dates">(${esc(wkDateSpan)})</span>` : ''} · ${esc(WEEK_THEMES[w] ?? '')}</div>
          <div class="p-list">${stepsHtml}</div>
          ${revFooter()}
        </main>`;
    };

    const load = async (): Promise<void> => {
      const metas = await allMeta();
      calendar.backfillLegacySkippedZ5(metas);
      loggedByDate = await collectLoggedByDate();
      paint();
    };
    paint();
    void load();

    const click = async (e: Event): Promise<void> => {
      const t = e.target as HTMLElement;
      const wb = t.closest<HTMLElement>('[data-w]');
      if (wb) { selectedWeek = Number(wb.dataset.w); paint(); return; }
      const sb = t.closest<HTMLElement>('[data-step]');
      if (sb) { await openStepSheet(Number(sb.dataset.step)); return; }
      if (t.closest('[data-rest-pick]')) {
        const cur = getSettings().restDay;
        const opts = [
          ...DAYS_FULL.map((name, idx) => ({
            key: String(idx),
            label: name,
            sub: 'Shown as Rest · Nothing scheduled in projections',
            on: cur === idx,
          })),
          { key: 'none', label: 'No fixed rest day', sub: 'Project 7 training days/week', on: cur == null },
        ];
        const k = await actionSheet('Weekly Rest Day', opts);
        if (!k) return;
        updateSettings({ restDay: k === 'none' ? null : Number(k) });
        void calendar.refresh();
      }
    };
    root.addEventListener('click', click);
    const offChips = wireChips(root);
    const offCal = calendar.changed.on(() => void load());
    const offSet = settingsChanged.on(paint);
    const offStore = lifts.changed.on(() => void load());
    return () => {
      dead = true;
      root.removeEventListener('click', click);
      offChips();
      offCal();
      offSet();
      offStore();
    };
  });
}
