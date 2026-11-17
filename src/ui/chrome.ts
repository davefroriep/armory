import { $, ICON, esc } from './dom';
import { chipTap, chipView, linkStatus, type ChipKind } from '../ble/sources';
import { bikeFrame, hrBeat, hrFresh, live } from '../ble/telemetry';
import { PHASE1_PLAN, nextEquipmentSlot, parseYmd, projectPlan, type WeekDayCell } from '../domain/plan';
import { hasKey } from '../intervals/api';
import { addDays, calendar, localDate, type TodayItem } from '../intervals/calendar';
import { matchRoutineByTitle } from '../lifts/seed';
import { lifts } from '../lifts/store';
import { REV, REV_STAMP } from '../rev';
import { getSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { actionSheet, toast } from './lifts/kit';

export interface BarOpts {
  title?: string;
  back?: string;
  bike?: boolean;
  cog?: boolean;
}

export function topbar(o: BarOpts): string {
  return `<header class="bar">
    ${o.back ? `<a class="icon-btn" href="${o.back}" aria-label="Back">${ICON.back}</a>` : '<span class="brandmark">ARMORY<b>FIT</b></span>'}
    ${o.back && o.title ? `<h1>${esc(o.title)}</h1>` : '<span class="grow"></span>'}
    <div class="chips">
      ${o.bike ? '<button class="chip" data-chip="bike"></button>' : ''}
      <button class="chip" data-chip="hr"></button>
      ${o.cog ? `<a class="icon-btn" href="#/settings" aria-label="Settings">${ICON.gear}</a>` : ''}
    </div>
  </header>`;
}

/** Keeps the chips live and tappable. Returns a cleanup. */
export function wireChips(root: HTMLElement): () => void {
  const paint = () => {
    root.querySelectorAll<HTMLElement>('[data-chip]').forEach((el) => {
      const v = chipView(el.dataset.chip as ChipKind);
      el.className = `chip ${v.cls}`;
      el.textContent = v.text;
    });
  };
  const click = (e: Event) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>('[data-chip]');
    if (el) chipTap(el.dataset.chip as ChipKind);
  };
  root.addEventListener('click', click);
  const off = linkStatus.on(paint);
  const offHr = hrBeat.on(paint);
  const t = setInterval(paint, 1000);
  // Live RPM on the bike chip: repaint on bike frames, at most ~4x a second.
  let lastPaint = 0;
  const offBike = bikeFrame.on(() => {
    const n = performance.now();
    if (n - lastPaint < 250) return;
    lastPaint = n;
    paint();
  });
  // screens re-render their header on every repaint; repaint the chips right away
  const obs = new MutationObserver(paint);
  obs.observe(root, { childList: true });
  paint();
  return () => {
    root.removeEventListener('click', click);
    off();
    offHr();
    offBike();
    obs.disconnect();
    clearInterval(t);
  };
}

export const hrLive = () => (hrFresh() ? live.hr : 0);

const href = (it: TodayItem): string => {
  const eq = it.match.equipment;
  if (!eq) return '#/';
  const q = `?ev=${it.ev.id}`;
  if (eq === 'strength') return `#/strength${q}&start=1`;
  return it.match.programId ? `#/${eq}/p/${it.match.programId}${q}` : `#/${eq}${q}`;
};

const DOW = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

function fmtPlanDate(ymd: string): string {
  const today = localDate();
  if (ymd === today) return 'Today';
  if (ymd === addDays(today, 1)) return 'Tomorrow';
  const { y, m, d } = parseYmd(ymd);
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}

function calItemRow(it: TodayItem, actionMode: 'link' | 'strength' = 'link'): string {
  const eq = it.match.equipment;
  const mins = it.ev.moving_time ? Math.round(it.ev.moving_time / 60) : 0;
  const r = eq === 'strength' ? matchRoutineByTitle(it.ev.name ?? '', lifts.sortedRoutines()) : undefined;
  const sub = eq === 'strength'
    ? `Strength${r ? ` · ${r.name}` : ''}`
    : eq
      ? EQUIPMENT_LABEL[eq] + (it.match.programId ? '' : ' · pick a program')
      : 'Not linked to equipment';
  const inner = `<span class="cal-ico ${eq ?? ''}">${eq ? ICON[eq] : ''}</span>
    <span class="cal-txt"><b>${esc(it.ev.name ?? 'Workout')}</b><small>${esc(sub)}${mins ? ` · ${mins} min` : ''}</small></span>`;
  const menu = `<button class="cal-more" type="button" data-cal-menu="${it.ev.id}" aria-label="Workout options">⋯</button>`;
  if (actionMode === 'strength') {
    return `<div class="cal-item" data-plan-card="${it.ev.id}"><div class="cal-link">${inner}</div><button class="btn sm primary" data-plan="${it.ev.id}">Start</button>${menu}</div>`;
  }
  return eq
    ? `<div class="cal-item"><a class="cal-link" href="${href(it)}">${inner}<span class="btn sm primary">Start</span></a>${menu}</div>`
    : `<div class="cal-item dim"><div class="cal-link">${inner}</div>${menu}</div>`;
}

/** Top Sunday–Saturday week glimpse strip on Home. */
export function weekStripCard(cells: WeekDayCell[]): string {
  const cols = cells.map((c) => {
    const dayNum = Number(c.ymd.slice(8));
    const mid = c.kind === 'rest'
      ? '<span class="wk-rest">REST</span>'
      : `<span class="wk-icos">${c.equipments.slice(0, 2).map((eq) => `<i class="wk-ico ${eq}">${ICON[eq]}</i>`).join('')}${c.kind === 'done' ? '<em class="wk-chk">✓</em>' : ''}</span>`;
    return `<button class="wk-d ${c.kind}${c.isToday ? ' today' : ''}" type="button" data-wk-ymd="${c.ymd}" aria-label="${DOW[c.dow]} ${dayNum}">
      <span class="wk-dow">${DOW[c.dow]}</span>
      <span class="wk-num">${dayNum}</span>
      ${mid}
    </button>`;
  }).join('');
  return `<section class="wk-strip" aria-label="Week glimpse">${cols}</section>`;
}

/** Double-wide Program Hero card sitting directly above the 4 equipment bays on Home. */
export function programHeroCard(items: TodayItem[], liveLift?: { name: string; elapsed: string }): string {
  const s = getSettings();
  const st = calendar.status;
  const cur = calendar.currentStep();
  const up = calendar.upcomingStep();
  const wk = cur?.week ?? PHASE1_PLAN.steps[Math.min(s.queue.stepIndex, PHASE1_PLAN.steps.length - 1)]?.week ?? 1;
  const stepNum = Math.min(s.queue.stepIndex + 1, PHASE1_PLAN.steps.length);
  const pill = `<span class="cal-pill">WK ${wk} · ${stepNum}/${PHASE1_PLAN.steps.length}</span>`;

  const liveRow = liveLift
    ? `<a class="prog-live" href="#/strength/session"><i class="rec-dot"></i><b>${esc(liveLift.name)}</b><span>${esc(liveLift.elapsed)}</span><em>Resume ›</em></a>`
    : '';

  let body = '';
  let foot = '';
  if (!hasKey() && items.length === 0) {
    body = `<a class="cal-empty" href="#/settings">Add your Intervals.icu API key to sync your plan →</a>`;
  } else if (items.length > 0) {
    body = items.map((it) => calItemRow(it, 'link')).join('');
  } else if (up) {
    body = `<div class="cal-empty"><b>✓ Today complete</b> · Next: ${esc(up.title)}</div>`;
    foot = `<div class="cal-foot"><span class="cal-sub">Scheduled ${esc(fmtPlanDate(up.date))}</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
  } else if (st === 'loading') {
    body = `<div class="cal-empty">Loading today’s plan…</div>`;
  } else {
    body = `<div class="cal-empty"><b>Rest · Nothing scheduled</b></div>`;
  }
  const err = st === 'error' ? `<div class="cal-note">${esc(calendar.error)}</div>` : '';
  return `<section class="card cal prog-hero">
    <div class="card-h"><span>ARMORY HYBRID · PHASE 1 ${pill}</span><a class="prog-link" href="#/program">Program ›</a></div>
    ${liveRow}${body}${foot}${err}
  </section>`;
}

/** Equipment-scoped program card for Strength / AirBike / Spin / Treadmill bays. */
export function equipmentProgramCard(eq: Equipment, items: TodayItem[], actionMode: 'link' | 'strength' = 'link'): string {
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const nextSlot = nextEquipmentSlot(proj, eq);
  const wk = calendar.currentStep()?.week ?? nextSlot?.p.step.week ?? 1;
  const st = calendar.status;

  let body = '';
  if (items.length > 0) {
    body = items.map((it) => calItemRow(it, actionMode)).join('');
  } else if (nextSlot) {
    const clean = nextSlot.p.step.title.replace(/^Wk \d+ · /, '');
    body = `<div class="cal-next">
      <span><b>Nothing scheduled today</b><small>Next: Wk ${nextSlot.p.step.week} · ${esc(clean)} (~${esc(fmtPlanDate(nextSlot.p.date ?? today))})</small></span>
      <button class="btn sm" type="button" data-step-peek="${nextSlot.p.step.index}">Preview</button>
    </div>`;
  } else {
    body = `<div class="cal-empty">Nothing scheduled</div>`;
  }
  const err = st === 'error' ? `<div class="cal-note">${esc(calendar.error)}</div>` : '';
  return `<section class="card cal">
    <div class="card-h"><span>${eq.toUpperCase()} · PROGRAM</span><span class="row"><button class="link" data-refresh>Refresh</button><a class="prog-link" href="#/program?w=${wk}">Wk ${wk} · Program ›</a></span></div>
    ${body}${err}
  </section>`;
}

/** "Today" planner card from the Intervals.icu calendar. */
export function calendarCard(items: TodayItem[], showUnmatched: boolean): string {
  if (!showUnmatched && items.length === 0) {
    return `<section class="card cal"><div class="card-h"><span>TODAY · INTERVALS.ICU</span><button class="link" data-refresh>Refresh</button></div><div class="cal-empty">${calendar.status === 'loading' ? 'Loading today’s plan…' : 'Nothing scheduled today'}</div></section>`;
  }
  const st = calendar.status;
  const cur = calendar.currentStep();
  const up = calendar.upcomingStep();
  const pill = cur && showUnmatched ? `<span class="cal-pill">STEP ${cur.index + 1}/${PHASE1_PLAN.steps.length}</span>` : '';
  let body = '';
  let foot = '';
  if (st === 'nokey') {
    body = `<a class="cal-empty" href="#/settings">Add your Intervals.icu API key to see today's plan →</a>`;
  } else if (items.length === 0) {
    if (showUnmatched && up) {
      body = `<div class="cal-empty"><b>✓ Today complete</b> · Up next: ${esc(up.title)}</div>`;
      foot = `<div class="cal-foot"><span class="cal-sub">Staged for ${esc(up.date)}</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
    } else {
      body = `<div class="cal-empty">${st === 'loading' ? 'Loading today’s plan…' : 'Nothing planned for today'}</div>`;
    }
  } else {
    body = items.filter((it) => it.match.equipment || showUnmatched).map((it) => calItemRow(it, 'link')).join('');
  }
  const err = st === 'error' ? `<div class="cal-note">${esc(calendar.error)}</div>` : '';
  return `<section class="card cal"><div class="card-h"><span>TODAY · INTERVALS.ICU ${pill}</span><button class="link" data-refresh>Refresh</button></div>${body}${foot}${err}</section>`;
}

export async function openCalendarItemMenu(evId: number): Promise<void> {
  const it = calendar.items.find((x) => x.ev.id === evId);
  if (!it) return;
  const k = await actionSheet(it.ev.name ?? 'Planned workout', [
    { key: 'done', label: 'Mark done', sub: 'Did it outside Armory — logs & pairs on Intervals.icu' },
    { key: 'skip', label: 'Skip', sub: 'Removes just this workout from today' },
    { key: 'push', label: 'Push to tomorrow', sub: 'Moves to tomorrow and shifts schedule back' },
    { key: 'add', label: 'Add to tomorrow', sub: 'Moves to tomorrow and keeps tomorrow’s schedule' },
  ]);
  if (!k) return;
  try {
    if (k === 'done') { await calendar.markItemDone(evId); toast('Marked done'); }
    else if (k === 'skip') { await calendar.skipItem(evId); toast('Workout skipped'); }
    else if (k === 'push') { await calendar.pushItemToTomorrow(evId); toast('Pushed to tomorrow'); }
    else if (k === 'add') { await calendar.addItemToTomorrow(evId); toast('Added to tomorrow'); }
  } catch (e) {
    toast((e as Error).message);
  }
}

export function wireCalendar(root: HTMLElement, repaint: () => void): () => void {
  const click = (e: Event) => {
    const t = e.target as HTMLElement;
    const menu = t.closest<HTMLElement>('[data-cal-menu]');
    if (menu) { void openCalendarItemMenu(Number(menu.dataset.calMenu)); return; }
    if (t.closest('[data-refresh]')) void calendar.refresh();
    else if (t.closest('[data-cal-pull]')) void calendar.pullTomorrowToToday();
  };
  root.addEventListener('click', click);
  const off = calendar.changed.on(repaint);
  return () => {
    root.removeEventListener('click', click);
    off();
  };
}

export const revFooter = (): string => `<footer class="rev">Rev ${REV} — ${REV_STAMP}</footer>`;

export { $ };
