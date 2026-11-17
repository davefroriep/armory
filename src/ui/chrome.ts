import { $, ICON, esc, mmss } from './dom';
import { chipTap, chipView, linkStatus, type ChipKind } from '../ble/sources';
import { bikeFrame, hrBeat, hrFresh, live } from '../ble/telemetry';
import { PHASE1_PLAN, fmtWeekSpan, nextEquipmentSlot, parseYmd, projectPlan, type WeekDayCell } from '../domain/plan';
import { addDays, calendar, localDate, type TodayItem } from '../intervals/calendar';
import { active } from '../lifts/active';
import { isSessionStarted } from '../lifts/logic';
import { matchRoutineByTitle } from '../lifts/seed';
import { lifts } from '../lifts/store';
import { REV, REV_STAMP } from '../rev';
import { getSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import type { TodayCompletedItem } from './dayZoom';
import { openHrSheet } from './hrSheet';
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

const mountedResumeHosts = new WeakSet<HTMLElement>();

/** Pins a sticky "Resume ›" strip directly below `.bar` across screens whenever a lift session has started. */
export function mountTopResume(root: HTMLElement): () => void {
  if (mountedResumeHosts.has(root)) return () => {};
  mountedResumeHosts.add(root);

  let dead = false;
  let strip: HTMLAnchorElement | null = null;

  const sync = (): void => {
    if (dead) return;
    const act = lifts.active;
    const running = act && isSessionStarted(act) ? act : undefined;
    if (!running) {
      strip?.remove();
      return;
    }
    if (!strip) {
      strip = document.createElement('a');
      strip.className = 'top-resume';
      strip.href = '#/strength/session';
      strip.innerHTML = '<i class="rec-dot"></i><b></b><span></span><em>Resume ›</em>';
    }
    const nameEl = strip.children[1] as HTMLElement;
    const clockEl = strip.children[2] as HTMLElement;
    if (nameEl.textContent !== running.name) nameEl.textContent = running.name;
    const elapsed = mmss(active.elapsedSec());
    if (clockEl.textContent !== elapsed) clockEl.textContent = elapsed;

    const bar = root.querySelector<HTMLElement>('header.bar');
    if (bar) {
      const h = Math.round(bar.getBoundingClientRect().height);
      if (h > 0) strip.style.top = `${h}px`;
      if (bar.nextElementSibling !== strip) bar.after(strip);
    } else if (strip.parentElement !== root) {
      root.prepend(strip);
    }
  };

  const offTick = active.tick.on(sync);
  const offLifts = lifts.changed.on(sync);
  const obs = new MutationObserver(sync);
  obs.observe(root, { childList: true });
  sync();

  return () => {
    dead = true;
    mountedResumeHosts.delete(root);
    offTick();
    offLifts();
    obs.disconnect();
    strip?.remove();
  };
}

/** Keeps the chips live and tappable. Returns a cleanup. */
export function wireChips(root: HTMLElement, opts?: { resume?: boolean }): () => void {
  const paint = () => {
    root.querySelectorAll<HTMLElement>('[data-chip]').forEach((el) => {
      const v = chipView(el.dataset.chip as ChipKind);
      el.className = `chip ${v.cls}`;
      el.textContent = v.text;
    });
  };
  const click = (e: Event) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>('[data-chip]');
    if (!el) return;
    const kind = el.dataset.chip as ChipKind;
    if (kind === 'hr') openHrSheet();
    else chipTap(kind);
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
  const wantResume = opts?.resume ?? !root.classList.contains('hud');
  const offResume = wantResume ? mountTopResume(root) : null;
  return () => {
    root.removeEventListener('click', click);
    off();
    offHr();
    offBike();
    obs.disconnect();
    clearInterval(t);
    offResume?.();
  };
}

export const hrLive = () => (hrFresh() ? live.hr : 0);

const href = (it: TodayItem): string => {
  const pick = `?pick=${encodeURIComponent(it.planSlot)}`;
  if (it.equipment === 'strength') return `#/strength${pick}&start=1`;
  if (it.equipment === 'airbike') return it.programId ? `#/airbike/p/${it.programId}${pick}` : `#/airbike${pick}`;
  return `#/${it.equipment}${pick}`;
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
  const eq = it.equipment;
  const mins = it.slot.seconds ? Math.round(it.slot.seconds / 60) : 0;
  const r = eq === 'strength' ? matchRoutineByTitle(it.slot.name, lifts.sortedRoutines()) : undefined;
  const subParts = [
    eq === 'strength'
      ? `Strength${r ? ` · ${r.name}` : ''}`
      : EQUIPMENT_LABEL[eq] + (eq === 'airbike' && !it.programId ? ' · pick a program' : ''),
    mins ? `${mins} min` : '',
    it.carried ? 'carry-over' : '',
  ].filter(Boolean);
  const inner = `<span class="cal-ico ${eq}">${ICON[eq]}</span>
    <span class="cal-txt"><b>${esc(it.slot.name)}</b><small>${esc(subParts.join(' · '))}</small></span>`;
  const menu = `<button class="cal-more" type="button" data-cal-menu="${esc(it.planSlot)}" aria-label="Workout options">⋯</button>`;
  if (actionMode === 'strength') {
    return `<div class="cal-item" data-plan-card="${esc(it.planSlot)}"><div class="cal-link">${inner}</div><button class="btn sm primary" data-plan="${esc(it.planSlot)}">Start</button>${menu}</div>`;
  }
  return `<div class="cal-item"><a class="cal-link" href="${href(it)}">${inner}<span class="btn sm primary">Start</span></a>${menu}</div>`;
}

/** Locked Sunday–Saturday swipable week strip header on Home. */
export function weekStripCard(cells: WeekDayCell[], selectedYmd?: string | null, today = localDate(), anim = ''): string {
  const cols = cells.map((c) => {
    const dayNum = Number(c.ymd.slice(8));
    const sel = selectedYmd === c.ymd;
    const icos = c.equipments.slice(0, 2);
    const chk = c.kind === 'done' ? '<em class="wk-chk" aria-hidden="true">✓</em>' : '';
    const mid = c.kind === 'rest'
      ? '<span class="wk-rest">REST</span>'
      : `<span class="wk-icos${icos.length > 1 ? ' two' : ''}">${icos.map((eq) => `<i class="wk-ico ${eq}">${ICON[eq]}</i>`).join('')}</span>`;
    return `<button class="wk-d ${c.kind}${c.isToday ? ' today' : ''}${sel ? ' sel' : ''}" type="button" data-wk-ymd="${c.ymd}" aria-pressed="${sel}" aria-label="${DOW[c.dow]} ${dayNum}">
      ${chk}
      <span class="wk-dow">${DOW[c.dow]}</span>
      <span class="wk-num">${dayNum}</span>
      ${mid}
    </button>`;
  }).join('');
  const spanTxt = cells.length ? fmtWeekSpan(cells[0].ymd) : '';
  const offToday = selectedYmd != null && selectedYmd !== today;
  return `<section class="cal-dock" id="calDock" aria-label="Calendar strip">
    <div class="wk-bar-h">
      <span class="wk-span">${esc(spanTxt)}</span>
      <div class="wk-bar-r">
        ${offToday ? '<button class="wk-today-btn" type="button" data-back-today>Today ↩</button>' : ''}
        <button class="wk-arr" type="button" data-wk-shift="-7" aria-label="Previous week">‹</button>
        <button class="wk-arr" type="button" data-wk-shift="7" aria-label="Next week">›</button>
      </div>
    </div>
    <div class="wk-strip${anim ? ` ${anim}` : ''}" id="wkStrip">${cols}</div>
  </section>`;
}

/** Compact 4-icon machine dock pinned along the bottom of Home. */
export function machineDock(scheduled: Set<Equipment>): string {
  const SHORT: Record<Equipment, string> = { strength: 'Strength', treadmill: 'Treadmill', spin: 'Spin', airbike: 'AirBike' };
  const btns = (['strength', 'treadmill', 'spin', 'airbike'] as Equipment[]).map((eq) =>
    `<a class="hd-btn ${eq}${scheduled.has(eq) ? ' has-today' : ''}" href="#/${eq}">
      <span class="hd-ico">${ICON[eq]}</span>
      <span class="hd-lbl">${SHORT[eq]}</span>
    </a>`,
  ).join('');
  return `<nav class="home-dock" aria-label="Equipment">${btns}</nav>`;
}

/** Local-first TODAY bar sitting inside the scrollable Home body above the always-open day zoom card. */
export function programHeroCard(items: TodayItem[], completedToday: TodayCompletedItem[] = []): string {
  const s = getSettings();
  const cur = calendar.currentStep();
  const up = calendar.upcomingStep();
  const wk = cur?.week ?? PHASE1_PLAN.steps[Math.min(s.queue.stepIndex, PHASE1_PLAN.steps.length - 1)]?.week ?? 1;
  const stepNum = Math.min(s.queue.stepIndex + 1, PHASE1_PLAN.steps.length);
  const completedRows = completedToday.map((c) => `<button class="cal-item cal-done-btn" type="button" data-today-zoom>
    <span class="cal-link">
      <span class="cal-ico done ${c.equipment}">${ICON[c.equipment]}</span>
      <span class="cal-txt"><b>${esc(c.name)}</b><small>${esc(c.sub)}</small></span>
    </span>
    <span class="p-badge ok">✓ Details ›</span>
  </button>`).join('');

  let body = '';
  let foot = '';
  if (items.length > 0 || completedToday.length > 0) {
    body = completedRows + items.map((it) => calItemRow(it, 'link')).join('');
    if (items.length === 0 && up) {
      foot = `<div class="cal-foot"><span class="cal-sub">Next: ${esc(up.title)} (${esc(fmtPlanDate(up.date))})</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
    }
  } else if (up) {
    body = `<button class="cal-empty cal-done-empty" type="button" data-today-zoom><b>✓ Today complete</b> · Next: ${esc(up.title)}<small>Tap to view today’s details ›</small></button>`;
    foot = `<div class="cal-foot"><span class="cal-sub">Scheduled ${esc(fmtPlanDate(up.date))}</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
  } else {
    body = `<div class="cal-empty"><b>Rest · Nothing scheduled</b></div>`;
  }
  return `<section class="card cal prog-hero">
    <div class="card-h"><span><b class="today-pill">TODAY</b> · WK ${wk} · ${stepNum}/${PHASE1_PLAN.steps.length}</span><a class="prog-link" href="#/program?w=${wk}">Full Program ›</a></div>
    ${body}${foot}
  </section>`;
}

/** Equipment-scoped program card for Strength / AirBike / Spin / Treadmill bays. */
export function equipmentProgramCard(eq: Equipment, items: TodayItem[], actionMode: 'link' | 'strength' = 'link'): string {
  const s = getSettings();
  const today = localDate();
  const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
  const nextSlot = nextEquipmentSlot(proj, eq);
  const wk = calendar.currentStep()?.week ?? nextSlot?.p.step.week ?? 1;

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
  return `<section class="card cal">
    <div class="card-h"><span>${eq.toUpperCase()} · PROGRAM</span><a class="prog-link" href="#/program?w=${wk}">Wk ${wk} · Full Program ›</a></div>
    ${body}
  </section>`;
}

/** "Today" planner card from the local rolling schedule. */
export function calendarCard(items: TodayItem[], showUnmatched: boolean): string {
  if (!showUnmatched && items.length === 0) {
    return `<section class="card cal"><div class="card-h"><span>TODAY · PROGRAM</span></div><div class="cal-empty">Nothing scheduled today</div></section>`;
  }
  const cur = calendar.currentStep();
  const up = calendar.upcomingStep();
  const pill = cur && showUnmatched ? `<span class="cal-pill">STEP ${cur.index + 1}/${PHASE1_PLAN.steps.length}</span>` : '';
  let body = '';
  let foot = '';
  if (items.length === 0) {
    if (showUnmatched && up) {
      body = `<div class="cal-empty"><b>✓ Today complete</b> · Up next: ${esc(up.title)}</div>`;
      foot = `<div class="cal-foot"><span class="cal-sub">Scheduled ${esc(fmtPlanDate(up.date))}</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
    } else {
      body = `<div class="cal-empty">Nothing planned for today</div>`;
    }
  } else {
    body = items.map((it) => calItemRow(it, 'link')).join('');
  }
  return `<section class="card cal"><div class="card-h"><span>TODAY · PROGRAM ${pill}</span></div>${body}${foot}</section>`;
}

export async function openCalendarItemMenu(planSlot: string): Promise<void> {
  const it = calendar.items.find((x) => x.planSlot === planSlot);
  if (!it) return;
  const k = await actionSheet(it.slot.name, [
    { key: 'done', label: 'Mark done', sub: 'Did it outside Armory — saves locally & logs to Intervals.icu' },
    { key: 'skip', label: 'Skip', sub: 'Marks (skipped) & removes from today' },
    { key: 'push', label: 'Push to tomorrow', sub: 'Moves to tomorrow and holds schedule back' },
    { key: 'add', label: 'Add to tomorrow', sub: 'Carries to tomorrow and keeps tomorrow’s schedule' },
  ]);
  if (!k) return;
  try {
    if (k === 'done') { await calendar.markSlotDone(planSlot); toast('Marked done'); }
    else if (k === 'skip') { calendar.skipSlot(planSlot); toast('Workout skipped'); }
    else if (k === 'push') { calendar.pushSlotToTomorrow(planSlot); toast('Pushed to tomorrow'); }
    else if (k === 'add') { calendar.addSlotToTomorrow(planSlot); toast('Added to tomorrow'); }
  } catch (e) {
    toast((e as Error).message);
  }
}

export function wireCalendar(root: HTMLElement, repaint: () => void): () => void {
  const click = (e: Event) => {
    const t = e.target as HTMLElement;
    const menu = t.closest<HTMLElement>('[data-cal-menu]');
    if (menu) { void openCalendarItemMenu(menu.dataset.calMenu!); return; }
    if (t.closest('[data-refresh]')) void calendar.refresh();
    else if (t.closest('[data-cal-pull]')) calendar.pullTomorrowToToday();
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
