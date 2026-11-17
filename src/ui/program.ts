import {
  PHASE1_PLAN, WEEK_THEMES, dayOfWeek, parseYmd, projectPlan, slotKey,
  type ProjectedStep,
} from '../domain/plan';
import { addDays, calendar, localDate } from '../intervals/calendar';
import { labels } from '../lifts/logic';
import { matchRoutineByTitle } from '../lifts/seed';
import { liftSettings } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { setText } from '../lifts/summary';
import { allMeta } from '../store/db';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { revFooter, topbar, wireChips } from './chrome';
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
  if (p.status === 'early') return `<span class="p-badge ok">✓ Done early${p.date ? ` · ${esc(fmtShortDate(p.date, today))}` : ''}</span>`;
  if (p.status === 'current') return `<span class="p-badge cur">● Current · ${esc(p.date ? fmtShortDate(p.date, today) : 'Today')}</span>`;
  return `<span class="p-badge">~${esc(p.date ? fmtShortDate(p.date, today) : 'TBD')}</span>`;
}

/** Safe read-only inspection sheet for any step in the 4-week block, with out-of-order controls. */
export async function openStepSheet(stepIndex: number): Promise<void> {
  await lifts.ready;
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const p = proj[stepIndex];
  if (!p) return;

  const routines = lifts.sortedRoutines();
  const unit = liftSettings().unit;
  const slotsHtml = p.step.slots.map((slot, si) => {
    const done = p.slotDone[si];
    const mins = Math.round(slot.seconds / 60);
    if (slot.equipment === 'strength') {
      const r = matchRoutineByTitle(slot.name, routines);
      let detail = `<p class="sh-text">${esc(slot.description)}</p>`;
      if (r) {
        const labs = labels(r.items);
        const rows = r.items.map((it, idx) => {
          const ex = lifts.exOrStub(it.exId);
          const note = lifts.row(it.exId)?.note;
          const prev = lifts.previous(it.exId);
          const best = prev?.sets.filter((x) => x.done && x.kind !== 'w').slice(-1)[0];
          const prevTxt = best ? setText(best, ex, unit) : '';
          return `<div class="sh-ex">
            <span class="xlab">${labs[idx]}</span>
            <span class="sh-ex-m"><b>${esc(ex.name)}</b><small>${it.sets} sets${note ? ` · ${esc(note)}` : ''}</small></span>
            ${prevTxt ? `<em>Last: ${esc(prevTxt)}</em>` : ''}
          </div>`;
        }).join('');
        detail = `<div class="sh-exlist">${rows}</div>
          <div class="sh-subrow"><a class="link" href="#/strength/routine/${r.id}">Edit template ›</a></div>`;
      }
      return `<div class="sh-slot">
        <div class="sh-slot-h">
          <span class="cal-ico strength">${ICON.strength}</span>
          <span><b>${esc(r?.name ?? slot.name)}</b><small>Strength · ${mins} min</small></span>
          ${done ? '<span class="p-badge ok">✓ Done</span>' : ''}
        </div>
        ${detail}
      </div>`;
    }
    return `<div class="sh-slot">
      <div class="sh-slot-h">
        <span class="cal-ico ${slot.equipment}">${ICON[slot.equipment]}</span>
        <span><b>${esc(slot.name)}</b><small>${EQUIPMENT_LABEL[slot.equipment]} · ${mins} min</small></span>
        ${done ? '<span class="p-badge ok">✓ Done</span>' : ''}
      </div>
      <p class="sh-text">${esc(slot.description)}</p>
    </div>`;
  }).join('');

  const acts: string[] = [];
  if (p.status === 'current') {
    p.step.slots.forEach((slot, si) => {
      if (p.slotDone[si]) return;
      const r = slot.equipment === 'strength' ? matchRoutineByTitle(slot.name, routines) : undefined;
      const lbl = slot.equipment === 'strength' ? (r?.name ?? 'Lifting') : slot.name;
      acts.push(`<button class="btn primary big" type="button" data-sact="cur:${si}">Start ${esc(lbl)}</button>`);
    });
  } else if (p.status === 'upcoming') {
    p.step.slots.forEach((slot, si) => {
      if (p.slotDone[si]) return;
      const r = slot.equipment === 'strength' ? matchRoutineByTitle(slot.name, routines) : undefined;
      if (slot.equipment === 'strength') {
        acts.push(`<button class="btn primary big" type="button" data-sact="early:${si}">Do ${esc(r?.name ?? 'Lifting')} Today</button>`);
      } else if (slot.equipment === 'airbike') {
        acts.push(`<button class="btn primary big" type="button" data-sact="early:${si}">Do ${esc(slot.name)} Today</button>`);
      } else {
        acts.push(`<button class="btn primary big" type="button" data-sact="ext:${si}">Mark ${esc(slot.name)} Done Today</button>`);
      }
    });
    acts.push(`<button class="btn big" type="button" data-sact="move">Move Queue Pointer Here</button>`);
  } else {
    acts.push(`<button class="btn big" type="button" data-sact="move">Move Queue Back Here</button>`);
  }
  acts.push(`<button class="btn big" type="button" data-sact="close">Close</button>`);

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
      sh.close();
      const ev = calendar.items.find((x) => x.match.equipment === slot.equipment);
      if (slot.equipment === 'strength') {
        const r = matchRoutineByTitle(slot.name, routines);
        await startWorkout({ name: r?.name ?? slot.name, routine: r, eventId: ev?.ev.id ?? null });
      } else if (slot.equipment === 'airbike') {
        location.hash = slot.programId ? `#/airbike/p/${slot.programId}${ev ? `?ev=${ev.ev.id}` : ''}` : '#/airbike';
      } else {
        location.hash = `#/${slot.equipment}`;
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
        location.hash = slot.programId ? `#/airbike/p/${slot.programId}?pick=${pick}` : '#/airbike';
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
      const ok = await confirmSheet(
        `Move active day to Wk ${p.step.week} · Day ${p.step.day}?`,
        'Stages this day on Today in Intervals.icu and projects future weeks forward from here.',
        'Move Queue Here',
      );
      if (ok) {
        await calendar.setStep(stepIndex);
        toast(`Queue set to Wk ${p.step.week} · Day ${p.step.day}`);
      }
    }
  });
}

/** Quick sheet opened when tapping any Sunday–Saturday day in the top week glimpse strip. */
export async function openWeekDayPeek(ymd: string): Promise<void> {
  const loggedByDate = await collectLoggedByDate();
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const { y, m, d } = parseYmd(ymd);
  const fullDate = new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  const isRestPref = s.restDay != null && dayOfWeek(ymd) === s.restDay;

  const logged = loggedByDate.get(ymd) ?? [];
  const scheduled = ymd >= today
    ? proj.filter((p) => (p.status === 'current' || p.status === 'upcoming') && p.step.slots.some((_, si) => !p.slotDone[si] && p.slotDates[si] === ymd))
    : [];

  let body = '';
  if (logged.length) {
    body += `<div class="fl">COMPLETED</div>` + logged.map((l) =>
      `<div class="sh-peek-row"><span class="cal-ico ${l.equipment}">${ICON[l.equipment]}</span><span><b>${esc(l.name)}</b><small>${EQUIPMENT_LABEL[l.equipment]}</small></span><span class="p-badge ok">✓</span></div>`,
    ).join('');
  }
  if (scheduled.length) {
    body += `<div class="fl">${ymd === today ? 'TODAY’S PLAN' : 'PROJECTED'}</div>` + scheduled.map((p) => {
      const slots = p.step.slots.filter((_, si) => !p.slotDone[si] && p.slotDates[si] === ymd);
      const eq = slots[0]?.equipment ?? 'strength';
      return `<button class="aitem sh-peek-btn" type="button" data-zoom="${p.step.index}">
        <b>${esc(p.step.title)}</b>
        <small>${esc(slots.map((sl) => `${sl.name} · ${Math.round(sl.seconds / 60)}m`).join(' + '))} · Tap to zoom in ›</small>
        <span class="peek-ico ${eq}">${ICON[eq]}</span>
      </button>`;
    }).join('');
  }
  if (!logged.length && !scheduled.length) {
    body = `<div class="sh-rest">
      <b>Rest · Nothing scheduled</b>
      <small>${isRestPref ? 'Configured weekly rest day — excluded from future date projections.' : 'Open training day — tap a future day in Program Overview to pull ahead.'}</small>
    </div>`;
  }

  const sh = openSheet(
    `<div class="sh-meta"><span class="p-badge${ymd === today ? ' cur' : ''}">${esc(fmtShortDate(ymd, today))}</span></div>
     <h3 class="sh-title">${esc(fullDate)}</h3>
     ${body}
     <a class="btn primary big" href="#/program">Open Program Overview ›</a>
     <button class="btn big" type="button" data-close>Close</button>`,
  );
  sh.body.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('a') || t.closest('[data-close]')) { sh.close(); return; }
    const z = t.closest<HTMLElement>('[data-zoom]');
    if (z) {
      const idx = Number(z.dataset.zoom);
      sh.close();
      void openStepSheet(idx);
    }
  });
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

      const doneCount = proj.filter((p) => p.status === 'done' || p.status === 'early').length;
      const total = PHASE1_PLAN.steps.length;
      const pct = Math.round((doneCount / total) * 100);

      const weekTabs = [1, 2, 3, 4].map((wk) => {
        const wkSteps = proj.filter((p) => p.step.week === wk);
        const allDone = wkSteps.every((p) => p.status === 'done' || p.status === 'early');
        const hasCur = wkSteps.some((p) => p.status === 'current');
        return `<button class="p-tab${wk === w ? ' on' : ''}${allDone ? ' done' : ''}" type="button" data-w="${wk}">
          Wk ${wk}${allDone ? ' ✓' : hasCur ? ' ●' : ''}
        </button>`;
      }).join('');

      const stepsHtml = proj.filter((p) => p.step.week === w).map((p) => {
        const cleanTitle = p.step.title.replace(/^Wk \d+ · /, '');
        const slots = p.step.slots.map((sl, si) => {
          const mins = Math.round(sl.seconds / 60);
          const done = p.slotDone[si];
          const loggedToday = p.status === 'current' && p.slotDone[si] && loggedByDate.has(today);
          return `<div class="p-slot${done ? ' done' : ''}">
            <span class="p-sico ${sl.equipment}">${ICON[sl.equipment]}</span>
            <span class="p-sname">${esc(sl.name)}</span>
            <small>${done || loggedToday ? '✓' : `${mins}m`}</small>
          </div>`;
        }).join('');
        return `<button class="p-step ${p.status}" type="button" data-step="${p.step.index}">
          <div class="p-step-h"><b>${esc(cleanTitle)}</b>${statusPillHtml(p, today)}</div>
          <div class="p-slots">${slots}</div>
        </button>`;
      }).join('');

      root.innerHTML = `${topbar({ back: '#/', title: 'Program Overview', cog: true })}
        <main class="page">
          <section class="card p-block">
            <div class="card-h"><span>ARMORY HYBRID · PHASE 1</span><button class="link" data-rest-pick>Rest: ${esc(restDayLabel(s.restDay))} ▾</button></div>
            <div class="p-prog-row"><b>${doneCount} / ${total} sessions</b><span>${pct}% · Target Jan 8 10K</span></div>
            <div class="p-bar"><u style="width:${pct}%"></u></div>
          </section>
          <div class="p-tabs">${weekTabs}</div>
          <div class="p-theme"><b>Week ${w}</b> · ${esc(WEEK_THEMES[w] ?? '')}</div>
          <div class="p-list">${stepsHtml}</div>
          ${revFooter()}
        </main>`;
    };

    const load = async (): Promise<void> => {
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
