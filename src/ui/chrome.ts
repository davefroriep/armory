import { $, ICON, esc } from './dom';
import { chipTap, chipView, linkStatus, type ChipKind } from '../ble/sources';
import { bikeFrame, hrFresh, live } from '../ble/telemetry';
import { PHASE1_PLAN } from '../domain/plan';
import { calendar, type TodayItem } from '../intervals/calendar';
import { REV, REV_STAMP } from '../rev';
import { EQUIPMENT_LABEL } from '../types';

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
  return it.match.programId ? `#/${eq}/p/${it.match.programId}${q}` : `#/${eq}${q}`;
};

/** "Today" planner card from the Intervals.icu calendar. */
export function calendarCard(items: TodayItem[], showUnmatched: boolean): string {
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
      body = `<div class="cal-empty"><b>✓ Today complete</b> · Up next: ${esc(up.step.title)}</div>`;
      foot = `<div class="cal-foot"><span class="cal-sub">Staged for ${esc(up.date)}</span><button class="link" data-cal-pull>Pull to Today</button></div>`;
    } else {
      body = `<div class="cal-empty">${st === 'loading' ? 'Loading today’s plan…' : 'Nothing planned for today'}</div>`;
    }
  } else {
    body = items
      .map((it) => {
        const eq = it.match.equipment;
        const mins = it.ev.moving_time ? Math.round(it.ev.moving_time / 60) : 0;
        const sub = eq ? EQUIPMENT_LABEL[eq] + (it.match.programId ? ' · tap to start' : ' · pick a program') : 'Not linked to equipment';
        const inner = `<span class="cal-ico ${eq ?? ''}">${eq ? ICON[eq] : ''}</span>
          <span class="cal-txt"><b>${esc(it.ev.name ?? 'Workout')}</b><small>${sub}${mins ? ` · ${mins} min` : ''}</small></span>`;
        return eq || showUnmatched ? (eq ? `<a class="cal-item" href="${href(it)}">${inner}<span class="chev">›</span></a>` : `<div class="cal-item dim">${inner}</div>`) : '';
      })
      .join('');
    if (showUnmatched && cur && !up) {
      foot = `<div class="cal-foot"><span class="cal-sub">${esc(cur.title)}</span><button class="link" data-cal-done>Mark Done</button></div>`;
    }
  }
  const err = st === 'error' ? `<div class="cal-note">${esc(calendar.error)}</div>` : '';
  return `<section class="card cal"><div class="card-h"><span>TODAY · INTERVALS.ICU ${pill}</span><button class="link" data-refresh>Refresh</button></div>${body}${foot}${err}</section>`;
}

export function wireCalendar(root: HTMLElement, repaint: () => void): () => void {
  const click = (e: Event) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-refresh]')) void calendar.refresh();
    else if (t.closest('[data-cal-done]')) void calendar.markCurrentDone();
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
