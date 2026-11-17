import { ensureSensors } from '../ble/sources';
import { PHASE1_PLAN, buildWeekGlimpse, projectPlan } from '../domain/plan';
import { calendar, localDate } from '../intervals/calendar';
import { pendingCount, retryPending, syncWorkout, discard } from '../workout/sync';
import { allMeta, getSamples, putMeta } from '../store/db';
import { getSettings, settingsChanged } from '../store/settings';
import { summarize } from '../workout/summary';
import { EQUIPMENT_LABEL, type Equipment, type WorkoutMeta } from '../types';
import { active } from '../lifts/active';
import { isSessionStarted } from '../lifts/logic';
import { lifts } from '../lifts/store';
import { ICON, esc, mmss } from './dom';
import { programHeroCard, revFooter, topbar, weekStripCard, wireCalendar, wireChips } from './chrome';
import { collectLoggedByDate, openStepSheet, openWeekDayPeek } from './program';

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
  let loggedByDate = new Map<string, { equipment: Equipment; name: string }[]>();

  const paint = () => {
    if (dead) return;
    const s = getSettings();
    const today = localDate();
    const proj = projectPlan(PHASE1_PLAN, s.queue, today, s.restDay);
    const todayEquipment = calendar.items.map((i) => i.match.equipment).filter((e): e is Equipment => !!e);
    const weekCells = buildWeekGlimpse({ today, proj, todayEquipment, loggedByDate });
    const scheduled = new Set(todayEquipment);
    const live = lifts.active && isSessionStarted(lifts.active) ? lifts.active : undefined;
    const liveInfo = live ? { name: live.name, elapsed: mmss(active.elapsedSec()) } : undefined;

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
        ${weekStripCard(weekCells)}
        ${programHeroCard(calendar.items, liveInfo)}
        <div class="tiles4">${tiles}</div>
        ${revFooter()}
      </main>`;
  };

  const loadDynamic = async () => {
    const [metas, logged] = await Promise.all([allMeta(), collectLoggedByDate()]);
    if (dead) return;
    loggedByDate = logged;
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

  const click = async (e: Event) => {
    const t = e.target as HTMLElement;
    const wkDay = t.closest<HTMLElement>('[data-wk-ymd]');
    if (wkDay) {
      await openWeekDayPeek(wkDay.dataset.wkYmd!);
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

  paint();
  void loadDynamic();
  const offChips = wireChips(root);
  const offCal = wireCalendar(root, () => void loadDynamic());
  const offSet = settingsChanged.on(paint);
  const offLifts = lifts.changed.on(() => void loadDynamic());

  return () => {
    dead = true;
    root.removeEventListener('click', click);
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
