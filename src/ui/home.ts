import { ensureSensors } from '../ble/sources';
import { PHASE1_PLAN, buildWeekGlimpse, dayOfWeek, projectPlan, shiftYmd } from '../domain/plan';
import { calendar, localDate } from '../intervals/calendar';
import { pendingCount, retryPending, syncWorkout, discard } from '../workout/sync';
import { allMeta, getSamples, putMeta } from '../store/db';
import { getSettings, settingsChanged } from '../store/settings';
import { summarize } from '../workout/summary';
import type { Equipment, WorkoutMeta } from '../types';
import { lifts } from '../lifts/store';
import { esc, mmss } from './dom';
import { confirmSheet, toast } from './lifts/kit';
import { machineDock, programHeroCard, revFooter, topbar, weekStripCard, wireCalendar, wireChips } from './chrome';
import { groupDayLogs, renderDayZoomCard, todayCompletedItems, type DayLogs } from './dayZoom';
import { currentZoomLookup, launchDayZoomSlot, launchDayZoomWorkout, openStepSheet, shiftScheduleToStep } from './program';

export function homeScreen(root: HTMLElement): () => void {
  root.classList.add('home-app');
  ensureSensors({ bike: false });
  void calendar.refresh();

  let dead = false;
  let banners = '';
  let lastToday = localDate();
  let selectedYmd = lastToday;
  let zoomAnim: '' | 'from-right' | 'from-left' | 'fade-in' = '';
  let weekAnim: '' | 'from-right' | 'from-left' = '';
  let dayLogsByDate = new Map<string, DayLogs>();
  let loggedByDate = new Map<string, { equipment: Equipment; name: string }[]>();
  let lastSwipeAt = 0;

  const paint = () => {
    if (dead) return;
    const s = getSettings();
    const today = localDate();
    if (today !== lastToday) {
      if (selectedYmd === lastToday) selectedYmd = today;
      lastToday = today;
    }
    const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
    const todayEquipment = calendar.items.map((i) => i.equipment);
    const lookup = currentZoomLookup();
    const completedToday = todayCompletedItems(dayLogsByDate.get(today), lookup, { today, proj, extras: s.queue.extras });
    const weekCells = buildWeekGlimpse({ today, anchorYmd: selectedYmd, proj, todayEquipment, extras: s.queue.extras, loggedByDate });
    const zoomHtml = renderDayZoomCard({
      ymd: selectedYmd,
      today,
      proj,
      dayLog: dayLogsByDate.get(selectedYmd),
      extras: s.queue.extras,
      restDay: s.restDay,
      anim: zoomAnim,
      lookup,
    });
    zoomAnim = '';
    const scheduled = new Set<Equipment>(todayEquipment);

    root.innerHTML = `${topbar({ cog: true })}
      ${weekStripCard(weekCells, selectedYmd, today, weekAnim)}
      <div class="home-today-dock">${programHeroCard(calendar.items, completedToday)}</div>
      <main class="page home-scroll">
        ${banners}
        ${zoomHtml}
        ${revFooter()}
      </main>
      ${machineDock(scheduled)}`;
    weekAnim = '';
  };

  const selectDay = (nextYmd: string): void => {
    if (nextYmd === selectedYmd) return;
    const prevWeek = shiftYmd(selectedYmd, -dayOfWeek(selectedYmd));
    const nextWeek = shiftYmd(nextYmd, -dayOfWeek(nextYmd));
    if (nextWeek !== prevWeek) {
      weekAnim = nextYmd > selectedYmd ? 'from-right' : 'from-left';
    }
    zoomAnim = nextYmd > selectedYmd ? 'from-right' : 'from-left';
    selectedYmd = nextYmd;
    paint();
    root.querySelector('.home-scroll')?.scrollTo({ top: 0 });
  };

  const loadDynamic = async () => {
    const [metas] = await Promise.all([allMeta(), lifts.ready]);
    if (dead) return;
    calendar.backfillLegacySkippedZ5(metas);
    const grouped = groupDayLogs(lifts.finished(), metas);
    dayLogsByDate = grouped.byDate;
    loggedByDate = grouped.summaryByDate;
    // A lift-tracker workout in progress is resumed from the top banner, never "recovered" as a bare HR file.
    const activeIncomplete = metas.filter((m) => m.status === 'active' && m.seconds === 0 && !lifts.sessions.has(m.id));
    const pend = await pendingCount();
    let h = '';
    for (const m of activeIncomplete) {
      const canResume = (m.equipment === 'airbike' || m.equipment === 'spin') && m.programId;
      h += `<section class="card warn"><b>Unfinished workout</b>
        <p>${esc(m.name)} — started ${new Date(m.startEpochMs).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. The app closed before you finished.</p>
        <div class="row">${canResume ? `<a class="btn primary sm" href="#/${esc(m.equipment)}/p/${esc(m.programId)}?resume=${esc(m.id)}">Resume</a>` : ''}<button class="btn ${canResume ? '' : 'primary '}sm" data-recover="${m.id}">Save &amp; upload</button><button class="btn sm" data-drop="${m.id}">Discard</button></div></section>`;
    }
    if (pend) {
      const why = metas.find((m) => m.status === 'saved' && m.upload === 'pending')?.lastError;
      h += `<section class="card warn"><b>${pend} workout${pend > 1 ? 's' : ''} waiting to upload</b>
        <p>Saved on this phone. ${esc(why ?? 'Intervals.icu wasn’t reachable.')}</p><div class="row"><button class="btn primary sm" data-retry>Retry now</button></div></section>`;
    }
    banners = h;
    paint();
  };

  const click = async (e: Event) => {
    if (Date.now() - lastSwipeAt < 260) {
      e.preventDefault();
      return;
    }
    const t = e.target as HTMLElement;
    const wkDay = t.closest<HTMLElement>('[data-wk-ymd]');
    if (wkDay) {
      selectDay(wkDay.dataset.wkYmd!);
      return;
    }
    const wkShift = t.closest<HTMLElement>('[data-wk-shift]');
    if (wkShift) {
      selectDay(shiftYmd(selectedYmd, Number(wkShift.dataset.wkShift)));
      return;
    }
    if (t.closest('[data-back-today]') || t.closest('[data-snap-today]') || t.closest('[data-today-zoom]')) {
      selectDay(localDate());
      root.querySelector('.home-scroll')?.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    const znav = t.closest<HTMLElement>('[data-znav]');
    if (znav) {
      selectDay(shiftYmd(selectedYmd, Number(znav.dataset.znav)));
      return;
    }
    const zact = t.closest<HTMLElement>('[data-zact]');
    if (zact) {
      const act = zact.dataset.zact!;
      if (act === 'do-slot' && zact.dataset.slot) {
        await launchDayZoomSlot(zact.dataset.slot);
      } else if (act === 'do-now') {
        await launchDayZoomWorkout(selectedYmd);
      } else if (act === 'shift') {
        const idx = Number(zact.dataset.stepIdx);
        if (await shiftScheduleToStep(idx)) {
          const today = localDate();
          selectedYmd = today;
          zoomAnim = 'fade-in';
          paint();
        }
      }
      return;
    }
    const peek = t.closest<HTMLElement>('[data-step-peek]');
    if (peek) {
      await openStepSheet(Number(peek.dataset.stepPeek));
      return;
    }
    const rec = t.closest<HTMLElement>('[data-recover]');
    if (rec) {
      const m = (await allMeta()).find((x) => x.id === rec.dataset.recover);
      if (m) await recover(m);
      await loadDynamic();
    }
    const drop = t.closest<HTMLElement>('[data-drop]');
    if (drop && await confirmSheet('Discard unfinished workout?', 'Drops any saved samples from this crashed session.', 'Discard', true)) {
      await discard(drop.dataset.drop!);
      await loadDynamic();
    }
    if (t.closest('[data-retry]')) {
      await retryPending();
      await loadDynamic();
    }
  };
  root.addEventListener('click', click);

  let swipe: { zone: 'week' | 'day'; id: number; startX: number; startY: number; startT: number; lastX: number; locked: 'x' | 'y' | null } | null = null;
  const swipeTargetEl = (zone: 'week' | 'day') =>
    root.querySelector<HTMLElement>(zone === 'week' ? '#wkStrip' : '#dayZoom');
  const resetSwipeEl = (zone: 'week' | 'day') => {
    const el = swipeTargetEl(zone);
    if (el) {
      el.style.transition = 'transform .16s ease, opacity .16s ease';
      el.style.transform = '';
      el.style.opacity = '';
    }
  };
  const onTouchStart = (e: TouchEvent) => {
    if (e.touches.length !== 1) { swipe = null; return; }
    const tgt = e.target as HTMLElement;
    const zone = tgt.closest('#calDock') ? 'week' : tgt.closest('#dayZoom') ? 'day' : null;
    if (!zone) { swipe = null; return; }
    const t = e.touches[0];
    swipe = { zone, id: t.identifier, startX: t.clientX, startY: t.clientY, startT: Date.now(), lastX: t.clientX, locked: null };
  };
  const onTouchMove = (e: TouchEvent) => {
    if (!swipe) return;
    const t = Array.from(e.touches).find((x) => x.identifier === swipe!.id);
    if (!t) return;
    const dx = t.clientX - swipe.startX;
    const dy = t.clientY - swipe.startY;
    swipe.lastX = t.clientX;
    if (!swipe.locked) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      swipe.locked = Math.abs(dx) > Math.abs(dy) * 1.25 ? 'x' : 'y';
    }
    if (swipe.locked !== 'x') return;
    const el = swipeTargetEl(swipe.zone);
    if (el) {
      el.style.transition = 'none';
      el.style.transform = `translateX(${(dx * 0.55).toFixed(1)}px)`;
      el.style.opacity = String(Math.max(0.45, 1 - Math.abs(dx) / 360));
    }
  };
  const onTouchEnd = () => {
    if (!swipe) return;
    const st = swipe;
    swipe = null;
    const dx = st.lastX - st.startX;
    const dt = Math.max(1, Date.now() - st.startT);
    if (st.locked === 'x' && (Math.abs(dx) >= 46 || (Math.abs(dx) >= 24 && Math.abs(dx) / dt > 0.45))) {
      lastSwipeAt = Date.now();
      const dir = dx < 0 ? 1 : -1;
      selectDay(shiftYmd(selectedYmd, st.zone === 'week' ? dir * 7 : dir));
      return;
    }
    if (st.locked === 'x') {
      if (Math.abs(dx) > 12) lastSwipeAt = Date.now();
      resetSwipeEl(st.zone);
    }
  };
  const onTouchCancel = () => {
    if (swipe?.locked === 'x') resetSwipeEl(swipe.zone);
    swipe = null;
  };
  root.addEventListener('touchstart', onTouchStart, { passive: true });
  root.addEventListener('touchmove', onTouchMove, { passive: true });
  root.addEventListener('touchend', onTouchEnd, { passive: true });
  root.addEventListener('touchcancel', onTouchCancel, { passive: true });

  paint();
  void loadDynamic();
  const offChips = wireChips(root);
  const offCal = wireCalendar(root, () => void loadDynamic());
  const offSet = settingsChanged.on(paint);
  const offLifts = lifts.changed.on(() => void loadDynamic());

  return () => {
    dead = true;
    root.classList.remove('home-app');
    root.removeEventListener('click', click);
    root.removeEventListener('touchstart', onTouchStart);
    root.removeEventListener('touchmove', onTouchMove);
    root.removeEventListener('touchend', onTouchEnd);
    root.removeEventListener('touchcancel', onTouchCancel);
    offChips();
    offCal();
    offSet();
    offLifts();
  };
}

/** Salvage a workout from its stored 1 Hz samples after a crash, then upload it. */
async function recover(m: WorkoutMeta): Promise<void> {
  const samples = await getSamples(m.id);
  if (samples.length < 10) {
    await discard(m.id);
    toast(`Only ${mmss(samples.length)} recorded — discarded`);
    return;
  }
  const rec: WorkoutMeta = { ...m, ...summarize(samples), status: 'saved', upload: 'pending' };
  await putMeta(rec);
  await syncWorkout(rec);
}
