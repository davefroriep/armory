import { RPE_STEPS, rpeLabel } from '../domain/rpe';
import { getMeta, getSamples, putMeta } from '../store/db';
import { getSettings } from '../store/settings';
import { EQUIPMENT_LABEL } from '../types';
import { discard, syncWorkout } from '../workout/sync';
import { revFooter, topbar } from './chrome';
import { esc, hms } from './dom';
import { ZONE_COLOR, hrZone } from './hr';
import { confirmSheet, mount, toast } from './lifts/kit';

/** Detailed cardio / outdoor HR workout view with HR zones, RPE editing, Intervals link, and Delete. */
export function cardioWorkoutScreen(root: HTMLElement, args: string[]): () => void {
  const id = args[0];
  let dead = false;
  return mount(() => {
    const paint = async (): Promise<void> => {
      const m = await getMeta(id);
      if (dead) return;
      if (!m || m.status !== 'saved') {
        location.replace('#/airbike');
        return;
      }
      const samples = await getSamples(id);
      if (dead) return;
      const hrs = samples.filter((x) => x.hr > 30);
      const maxHrCfg = getSettings().hrMax;
      const secs = [0, 0, 0, 0, 0, 0];
      for (const x of hrs) secs[hrZone(x.hr, maxHrCfg).z]++;
      const totalHr = Math.max(1, secs.slice(1).reduce((a, b) => a + b, 0));
      const zoneBars = hrs.length >= 10
        ? `<section class="card zones"><div class="card-h"><span>HEART RATE ZONES</span></div>${
            [1, 2, 3, 4, 5].map((z) => `<div class="zrow"><span>Z${z}</span><i><u style="width:${(secs[z] / totalHr) * 100}%;background:${ZONE_COLOR[z]}"></u></i><b>${hms(secs[z])}</b></div>`).join('')
          }</section>`
        : '';

      const avgCad = samples.length ? Math.round(samples.reduce((a, x) => a + x.cad, 0) / samples.length) : 0;
      const distMi = samples.length ? (samples[samples.length - 1].dist / 1609.344) : 0;
      const statCards: [string, string][] = [
        ['TIME', hms(m.seconds)],
        m.avgW > 0 ? ['AVG W', `${m.avgW} W`] : ['CADENCE', avgCad ? `${avgCad} rpm` : '--'],
        m.maxW > 0 ? ['MAX W', `${m.maxW} W`] : ['WORK', m.kj > 0 ? `${m.kj} kJ` : '--'],
        m.avgHr > 0 ? ['AVG HR', `${m.avgHr}`] : ['MAX HR', m.maxHr ? `${m.maxHr}` : '--'],
      ];
      if (m.avgW > 0 && m.kj > 0) statCards.push(['WORK', `${m.kj} kJ`]);
      if (distMi >= 0.05) statCards.push(['DIST', `${distMi.toFixed(2)} mi`]);

      const syncHtml = m.upload === 'done'
        ? `<div class="status ok">✓ Synced to Intervals.icu${m.rpe ? ` · ${esc(rpeLabel(m.rpe))} (RPE ${m.rpe})` : ''}${m.activityId ? ` · <a class="link" target="_blank" rel="noopener" href="https://intervals.icu/activities/${esc(m.activityId)}">Open ↗</a>` : ''}</div>`
        : `<div class="status warn">Saved on this phone.${m.lastError ? ` ${esc(m.lastError)}` : ''} <button class="btn sm" data-act="retry">Retry upload</button></div>`;

      const backTo = m.equipment === 'strength' ? '#/strength/history' : m.equipment === 'airbike' ? '#/airbike' : '#/';
      root.innerHTML = `${topbar({ back: backTo, title: EQUIPMENT_LABEL[m.equipment] })}
        <main class="page summary">
          <h1 class="sum-title">${esc(m.name)}</h1>
          <div class="sum-sub">${new Date(m.startEpochMs).toLocaleString([], { weekday: 'long', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</div>
          <section class="stats${statCards.length === 4 ? ' four' : ''}">${statCards.map(([l, v]) => `<div><small>${l}</small><b>${esc(v)}</b></div>`).join('')}</section>
          ${m.notes ? `<section class="card"><div class="card-h"><span>SPLITS &amp; RECOVERY</span></div><p class="note-p">${esc(m.notes)}</p></section>` : ''}
          ${zoneBars}
          ${syncHtml}
          <h2 class="sec">EFFORT RATING (RPE)</h2>
          <section class="rpe" id="rpe">${RPE_STEPS.map((r) => `<button data-rpe="${r.value}" class="rpe-${r.value}${m.rpe === r.value ? ' on' : ''}">${r.label}</button>`).join('')}</section>
          <button class="btn danger big" type="button" data-act="del">Delete workout</button>
          ${revFooter()}
        </main>`;
    };
    void paint();

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const m = await getMeta(id);
      if (!m) return;
      const rpe = t.closest<HTMLElement>('[data-rpe]');
      if (rpe) {
        m.rpe = Number(rpe.dataset.rpe);
        m.patched = false;
        m.upload = 'pending';
        await putMeta(m);
        root.querySelectorAll('#rpe button').forEach((b) => b.classList.toggle('on', b === rpe));
        await syncWorkout(m);
        toast(`Saved RPE ${m.rpe}/10`);
        void paint();
        return;
      }
      const act = t.closest<HTMLElement>('[data-act]')?.dataset.act;
      if (act === 'retry') {
        const res = await syncWorkout(m);
        toast(res.upload === 'done' ? 'Synced to Intervals.icu' : (res.lastError || 'Sync failed'));
        void paint();
        return;
      }
      if (act === 'del') {
        const backTo = m.equipment === 'strength' ? '#/strength/history' : '#/';
        if (await confirmSheet('Delete workout?', 'Removes this workout and its 1 Hz samples from your phone. Any Intervals.icu copy stays.', 'Delete', true)) {
          await discard(id);
          toast('Workout deleted');
          location.hash = backTo;
        }
      }
    });
    return () => { dead = true; };
  });
}
