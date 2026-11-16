import { sessionStats } from '../../lifts/calc';
import { routineFromSession } from '../../lifts/logic';
import { resyncSession } from '../../lifts/intervals';
import { liftSettings, fmtKg } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { fmtDuration, fmtVolume, sessionPRs } from '../../lifts/summary';
import { RPE_STEPS } from '../../domain/rpe';
import { getMeta, getSamples } from '../../store/db';
import { getSettings } from '../../store/settings';
import { retryPending } from '../../workout/sync';
import { revFooter } from '../chrome';
import { esc, hms } from '../dom';
import { ZONE_COLOR, hrZone } from '../hr';
import { openShareCard, stat } from './cards';
import { mount, promptSheet, toast } from './kit';

/** Post-workout: numbers, PRs, effort rating, share card. The Intervals.icu upload has already started by itself. */
export function summaryScreen(root: HTMLElement, args: string[]): () => void {
  return mount(() => {
    const s = lifts.sessions.get(args[0]);
    if (!s || s.status !== 'done') {
      location.replace('#/strength');
      return;
    }
    const unit = liftSettings().unit;
    const st = sessionStats(s, lifts.exOrStub);
    const prs = sessionPRs(s, lifts.prs());
    const prLines = s.exs.flatMap((e) => e.sets.filter((x) => lifts.prs().has(x.id)).map((x) => ({ e, x, h: lifts.prs().get(x.id)! })));

    root.innerHTML = `<main class="page summary">
      <h1 class="sum-title">${esc(s.name)}</h1>
      <div class="sum-sub">${new Date(s.startMs).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })} · ${fmtDuration(st.durationSec)}</div>
      <section class="stats four">${stat('SETS', String(st.sets))}${stat('VOLUME', fmtVolume(st.volumeKg, unit).replace(` ${unit}`, ''))}${stat('EXERCISES', String(st.exercises))}${stat('PRs', String(prs.count))}</section>
      ${prLines.length ? `<section class="card prs"><div class="card-h"><span>NEW RECORDS</span></div>${prLines.map(({ e, x, h }) => {
        const ex = lifts.exOrStub(e.exId);
        const wTxt = ex.type === 'bodyweight' && h.kg === 0 ? 'BW' : `${fmtKg(h.kg)} ${unit}`;
        return `<div class="prl"><i>★</i><b>${esc(ex.name)}</b><span>${h.n}RM · ${wTxt}${x.vids?.length ? ' 🎥' : ''}</span></div>`;
      }).join('')}</section>` : ''}
      <div id="hrzones"></div>
      <div id="sync" class="status busy"><span class="spinner"></span> Logging to Intervals.icu…</div>
      <h2 class="sec">HOW HARD WAS IT?</h2>
      <section class="rpe" id="rpe">${RPE_STEPS.map((r) => `<button data-rpe="${r.value}" class="rpe-${r.value}${s.rpe === r.value ? ' on' : ''}">${r.label}</button>`).join('')}</section>
      <button class="btn big" id="share">Share lift card</button>
      ${s.routineId && lifts.routines.has(s.routineId) ? '' : '<button class="btn big" id="asday">Save as a day</button>'}
      <a class="btn primary big" href="#/strength">Done</a>
      ${revFooter()}</main>`;

    const q = <T extends HTMLElement>(id: string): T | null => root.querySelector<T>('#' + id);

    // heart-rate zones (if a strap was connected)
    void (async () => {
      const samples = await getSamples(s.id);
      const hrs = samples.filter((x) => x.hr > 30);
      if (hrs.length < 30) return;
      const max = getSettings().hrMax;
      const secs = [0, 0, 0, 0, 0, 0];
      for (const x of hrs) secs[hrZone(x.hr, max).z]++;
      const total = Math.max(1, secs.reduce((a, b) => a + b, 0));
      const avg = Math.round(hrs.reduce((a, b) => a + b.hr, 0) / hrs.length);
      const bars = [1, 2, 3, 4, 5].map((z) => `<div class="zrow"><span>Z${z}</span><i><u style="width:${(secs[z] / total) * 100}%;background:${ZONE_COLOR[z]}"></u></i><b>${hms(secs[z])}</b></div>`).join('');
      const el = q('hrzones');
      if (el) el.innerHTML = `<section class="card zones"><div class="card-h"><span>HEART RATE · AVG ${avg} · MAX ${Math.max(...hrs.map((x) => x.hr))}</span></div>${bars}</section>`;
    })();

    let alive = true;
    const paintSync = async (): Promise<void> => {
      const m = await getMeta(s.id);
      const box = q('sync');
      if (!alive || !box || !m) return;
      if (m.upload === 'done') {
        box.className = 'status ok';
        box.innerHTML = `✓ Logged to Intervals.icu${m.activityId ? ` · <a class="link" target="_blank" rel="noopener" href="https://intervals.icu/activities/${esc(m.activityId)}">Open</a>` : ''}`;
        clearInterval(poll);
      } else if (m.lastError) {
        box.className = 'status warn';
        box.innerHTML = `Saved on this phone. Intervals.icu didn’t take it yet:<br><small>${esc(m.lastError)}</small><br><button class="btn sm" data-retry>Retry now</button>`;
      } else {
        box.className = 'status busy';
        box.innerHTML = '<span class="spinner"></span> Logging to Intervals.icu…';
      }
    };
    void paintSync();
    const poll = setInterval(() => void paintSync(), 1200);

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const rpe = t.closest<HTMLElement>('[data-rpe]');
      if (rpe) {
        s.rpe = Number(rpe.dataset.rpe);
        lifts.saveSession(s, true);
        root.querySelectorAll('#rpe button').forEach((b) => b.classList.toggle('on', b === rpe));
        void resyncSession(s.id).then(() => paintSync());
        return;
      }
      if (t.closest('[data-retry]')) { await retryPending(); void paintSync(); return; }
      if (t.closest('#share')) return void (await openShareCard(s));
      if (t.closest('#asday')) {
        const name = await promptSheet('Save as a day', s.name, 'Upper 1, Lower 2…');
        if (!name) return;
        const r = routineFromSession(s, name, lifts.routines.size);
        await lifts.saveRoutine(r);
        s.routineId = r.id;
        lifts.saveSession(s, true);
        q('asday')?.remove();
        toast(`“${name}” saved. Find it under My Days`);
      }
    });
    return () => { alive = false; clearInterval(poll); };
  });
}
