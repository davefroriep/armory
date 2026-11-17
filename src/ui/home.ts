import { ensureSensors } from '../ble/sources';
import { PHASE1_PLAN, buildWeekGlimpse, projectPlan, shiftYmd } from '../domain/plan';
import { calendar, localDate } from '../intervals/calendar';
import { pendingCount, retryPending, syncWorkout, discard } from '../workout/sync';
import { allMeta, getSamples, putMeta } from '../store/db';
import { getSettings, settingsChanged } from '../store/settings';
import { summarize } from '../workout/summary';
import { EQUIPMENT_LABEL, type Equipment, type WorkoutMeta } from '../types';
import { lifts } from '../lifts/store';
import { ICON, esc, mmss } from './dom';
import { programHeroCard, revFooter, topbar, weekStripCard, wireCalendar, wireChips } from './chrome';
import { groupDayLogs, renderDayZoomCard, todayCompletedItems, type DayLogs } from './dayZoom';
import { currentZoomLookup, launchDayZoomWorkout, openStepSheet, shiftScheduleToStep } from './program';

const TILES: Equipment[] = ['strength', 'treadmill', 'spin', 'airbike'];
const SUB: Record<Equipment, string> = {
  strength: 'Rack · Barbell · Cables',
  treadmill: 'Horizon T101',
  spin: 'Bowflex C7',
  airbike: 'Assault AirBike Elite',
};

export function homeScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: false });
  void calendar.refresh();

  let dead = false;
  let banners = '';
  let selectedYmd: string | null = null;
  let zoomAnim: '' | 'from-right' | 'from-left' | 'fade-in' = '';
  let dayLogsByDate = new Map<string, DayLogs>();
  let loggedByDate = new Map<string, { equipment: Equipment; name: string }[]>();
  let lastSwipeAt = 0;

  const paint = () => {
    if (dead) return;
    const s = getSettings();
    const today = localDate();
    const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
    const todayEquipment = calendar.items.map((i) => i.match.equipment).filter((e): e is Equipment => !!e);
    const lookup = currentZoomLookup();
    const completedToday = todayCompletedItems(dayLogsByDate.get(today), lookup);
    const weekCells = buildWeekGlimpse({ today, anchorYmd: selectedYmd ?? today, proj, todayEquipment, loggedByDate });
    const zoomHtml = selectedYmd
      ? renderDayZoomCard({ ymd: selectedYmd, today, proj, dayLog: dayLogsByDate.get(selectedYmd), restDay: s.restDay, anim: zoomAnim, lookup })
      : '';
    zoomAnim = '';
    const scheduled = new Set(todayEquipment);

    const tiles = TILES.map(
      (eq) => `<a class="tile ${eq}${scheduled.has(eq) ? ' has-today' : ''}" href="#/${eq}">
        <span class="tile-ico">${ICON[eq]}</span>
        <span class="tile-name">${EQUIPMENT_LABEL[eq]}</span>
        <span class="tile-sub">${SUB[eq]}</span>
      </a>`,
    ).join('');
    root.innerHTML = `${topbar({ cog: true })}
      <main class="page">
        ${banners}
        ${weekStripCard(weekCells, selectedYmd)}
        ${programHeroCard(calendar.items, completedToday)}
        ${zoomHtml}
        <div class="tiles4">${tiles}</div>
        ${revFooter()}
      </main>`;
  };

  const loadDynamic = async () => {
    const [metas] = await Promise.all([allMeta(), lifts.ready]);
    if (dead) return;
    const grouped = groupDayLogs(lifts.finished(), metas);
    dayLogsByDate = grouped.byDate;
    loggedByDate = grouped.summaryByDate;
    // A lift-tracker workout in progress is resumed from the hero banner, never "recovered" as a bare HR file.
    const activeIncomplete = metas.filter((m) => m.status === 'active' && m.seconds === 0 && !lifts.sessions.has(m.id));
    const pend = await pendingCount();
    let h = '';
    for (const m of activeIncomplete) {
      h += `<section class="card warn"><b>Unfinished workout</b>
        <p>${esc(m.name)} — started ${new Date(m.startEpochMs).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. The app closed before you finished.</p>
        <div class="row"><button class="btn primary sm" data-recover="${m.id}">Save &amp; upload</button><button class="btn sm" data-drop="${m.id}">Discard</button></div></section>`;
    }
    if (pend) {
      const why = metas.find((m) => m.status === 'saved' && m.upload === 'pending')?.lastError;
      h += `<section class="card warn"><b>${pend} workout${pend > 1 ? 's' : ''} waiting to upload</b>
        <p>Saved on this phone. ${esc(why ?? 'Intervals.icu wasn’t reachable.')}</p><div class="row"><button class="btn primary sm" data-retry>Retry now</button></div></section>`;
    }
    banners = h;
    paint();
  };

  const toggleSelectYmd = (ymd: string) => {
    if (selectedYmd === ymd) {
      selectedYmd = null;
    } else {
      zoomAnim = !selectedYmd ? 'fade-in' : ymd > selectedYmd ? 'from-right' : 'from-left';
      selectedYmd = ymd;
    }
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
      toggleSelectYmd(wkDay.dataset.wkYmd!);
      return;
    }
    if (t.closest('[data-today-zoom]')) {
      toggleSelectYmd(localDate());
      return;
    }
    if (t.closest('[data-inline-dismiss]')) {
      selectedYmd = null;
      paint();
      return;
    }
    const znav = t.closest<HTMLElement>('[data-znav]');
    if (znav && selectedYmd) {
      const dir = Number(znav.dataset.znav);
      zoomAnim = dir > 0 ? 'from-right' : 'from-left';
      selectedYmd = shiftYmd(selectedYmd, dir);
      paint();
      return;
    }
    const zact = t.closest<HTMLElement>('[data-zact]');
    if (zact) {
      const act = zact.dataset.zact!;
      if (act === 'today') {
        const today = localDate();
        if (selectedYmd !== today) {
          zoomAnim = !selectedYmd ? 'fade-in' : today > selectedYmd ? 'from-right' : 'from-left';
          selectedYmd = today;
          paint();
        }
      } else if (act === 'do-now' && selectedYmd) {
        await launchDayZoomWorkout(selectedYmd);
      } else if (act === 'shift') {
        const idx = Number(zact.dataset.stepIdx);
        if (await shiftScheduleToStep(idx)) {
          selectedYmd = localDate();
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
    if (drop && confirm('Discard this unfinished workout?')) {
      await discard(drop.dataset.drop!);
      await loadDynamic();
    }
    if (t.closest('[data-retry]')) {
      await retryPending();
      await loadDynamic();
    }
  };
  root.addEventListener('click', click);

  let swipe: { id: number; startX: number; startY: number; startT: number; lastX: number; locked: 'x' | 'y' | null } | null = null;
  const resetZoomTransform = () => {
    const card = root.querySelector<HTMLElement>('#dayZoom');
    if (card) {
      card.style.transition = 'transform .16s ease, opacity .16s ease';
      card.style.transform = '';
      card.style.opacity = '';
    }
  };
  const onTouchStart = (e: TouchEvent) => {
    if (e.touches.length !== 1 || !selectedYmd) { swipe = null; return; }
    if (!(e.target as HTMLElement).closest('#dayZoom')) { swipe = null; return; }
    const t = e.touches[0];
    swipe = { id: t.identifier, startX: t.clientX, startY: t.clientY, startT: Date.now(), lastX: t.clientX, locked: null };
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
    const card = root.querySelector<HTMLElement>('#dayZoom');
    if (card) {
      card.style.transition = 'none';
      card.style.transform = `translateX(${(dx * 0.55).toFixed(1)}px)`;
      card.style.opacity = String(Math.max(0.45, 1 - Math.abs(dx) / 360));
    }
  };
  const onTouchEnd = () => {
    if (!swipe) return;
    const st = swipe;
    swipe = null;
    const dx = st.lastX - st.startX;
    const dt = Math.max(1, Date.now() - st.startT);
    if (st.locked === 'x' && selectedYmd && (Math.abs(dx) >= 50 || (Math.abs(dx) >= 26 && Math.abs(dx) / dt > 0.45))) {
      lastSwipeAt = Date.now();
      const dir = dx < 0 ? 1 : -1;
      zoomAnim = dir > 0 ? 'from-right' : 'from-left';
      selectedYmd = shiftYmd(selectedYmd, dir);
      paint();
      return;
    }
    if (st.locked === 'x') {
      if (Math.abs(dx) > 12) lastSwipeAt = Date.now();
      resetZoomTransform();
    }
  };
  const onTouchCancel = () => {
    if (swipe?.locked === 'x') resetZoomTransform();
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
    alert(`Only ${mmss(samples.length)} was recorded, so it was discarded.`);
    return;
  }
  const rec: WorkoutMeta = { ...m, ...summarize(samples), status: 'saved', upload: 'pending' };
  await putMeta(rec);
  await syncWorkout(rec);
}
