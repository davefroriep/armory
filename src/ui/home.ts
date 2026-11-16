import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { pendingCount, retryPending, syncWorkout, discard } from '../workout/sync';
import { allMeta, getSamples, putMeta } from '../store/db';
import { summarize } from '../workout/summary';
import { EQUIPMENT_LABEL, type Equipment, type WorkoutMeta } from '../types';
import { lifts } from '../lifts/store';
import { ICON, esc, mmss } from './dom';
import { calendarCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';

const TILES: Equipment[] = ['strength', 'treadmill', 'spin', 'airbike'];
const SUB: Record<Equipment, string> = {
  strength: 'Lift tracker',
  treadmill: 'Horizon T101',
  spin: 'Bowflex C7',
  airbike: 'Assault AirBike Elite',
};

export function homeScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: false });
  void calendar.refresh();

  let dead = false;
  let banners = '';
  const paint = () => {
    if (dead) return;
    const tiles = TILES.map(
      (eq) => `<a class="tile ${eq}" href="#/${eq}">
        <span class="tile-ico">${ICON[eq]}</span>
        <span class="tile-name">${EQUIPMENT_LABEL[eq]}</span>
        <span class="tile-sub">${SUB[eq]}</span>
      </a>`,
    ).join('');
    root.innerHTML = `${topbar({ cog: true })}
      <main class="page">
        ${banners}
        ${calendarCard(calendar.items, true)}
        <div class="tiles4">${tiles}</div>
        ${revFooter()}
      </main>`;
  };

  const loadBanners = async () => {
    const metas = await allMeta();
    await lifts.ready;
    if (dead) return;
    // A lift-tracker workout in progress is resumed from the Strength tile, never "recovered" as a bare HR file.
    const live = lifts.active;
    const active = metas.filter((m) => m.status === 'active' && m.seconds === 0 && !lifts.sessions.has(m.id));
    const pend = await pendingCount();
    let h = live
      ? `<section class="card warn"><b>Workout in progress</b><p>${esc(live.name)} — started ${new Date(live.startMs).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.</p><div class="row"><a class="btn primary sm" href="#/strength/session">Resume</a></div></section>`
      : '';
    for (const m of active) {
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
    const rec = t.closest<HTMLElement>('[data-recover]');
    if (rec) {
      const m = (await allMeta()).find((x) => x.id === rec.dataset.recover);
      if (m) await recover(m);
      await loadBanners();
    }
    const drop = t.closest<HTMLElement>('[data-drop]');
    if (drop && confirm('Discard this unfinished workout?')) {
      await discard(drop.dataset.drop!);
      await loadBanners();
    }
    if (t.closest('[data-retry]')) {
      await retryPending();
      await loadBanners();
    }
  };
  root.addEventListener('click', click);

  paint();
  void loadBanners();
  const offChips = wireChips(root);
  const offCal = wireCalendar(root, paint);

  return () => {
    dead = true;
    root.removeEventListener('click', click);
    offChips();
    offCal();
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
