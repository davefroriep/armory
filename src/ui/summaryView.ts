import { RPE_STEPS, rpeLabel } from '../domain/rpe';
import { calendar, localDate } from '../intervals/calendar';
import { putMeta } from '../store/db';
import type { WorkoutMeta } from '../types';
import { discard, syncWorkout } from '../workout/sync';
import { allowNextRoute } from '../main';
import { revFooter } from './chrome';
import { esc, hms } from './dom';
import { confirmSheet } from './lifts/kit';

export interface SummaryOpts {
  title: string;
  meta: WorkoutMeta;
  /** [label, value] cards. */
  stats: [string, string][];
  /** Extra HTML between stats and the RPE picker (e.g. HR zone bars). */
  extra?: string;
  backHref?: string;
}

/** Shared end-of-workout screen: stats, effort rating, save & upload / discard. */
export function renderSummary(root: HTMLElement, o: SummaryOpts): void {
  const { meta } = o;
  const stats = o.stats.map(([l, v]) => `<div><small>${l}</small><b>${v}</b></div>`).join('');
  root.innerHTML = `<main class="page summary">
    <h1 class="sum-title">${esc(o.title)}</h1>
    <div class="sum-sub">${new Date(meta.startEpochMs).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })} · ${hms(meta.seconds)}</div>
    <section class="stats${o.stats.length === 4 ? ' four' : ''}">${stats}</section>
    ${o.extra ?? ''}
    <h2 class="sec">HOW HARD WAS IT?</h2>
    <section class="rpe" id="rpe">
      ${RPE_STEPS.map((s) => `<button data-rpe="${s.value}" class="rpe-${s.value}">${s.label}</button>`).join('')}
    </section>
    <div class="sum-foot">
      <div id="saveBox">
        <button class="btn primary big" id="save" disabled>Save &amp; upload to Intervals.icu</button>
        <button class="btn danger" id="discard">Discard workout</button>
      </div>
      ${revFooter()}
    </div>
  </main>`;

  let rpe = 0;
  const q = <T extends HTMLElement>(id: string) => root.querySelector<T>('#' + id)!;
  q('rpe').addEventListener('click', (ev) => {
    const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-rpe]');
    if (!b) return;
    rpe = Number(b.dataset.rpe);
    root.querySelectorAll('#rpe button').forEach((x) => x.classList.toggle('on', x === b));
    q<HTMLButtonElement>('save').disabled = false;
  });
  q('discard').addEventListener('click', async () => {
    if (!(await confirmSheet('Discard this workout?', 'It will not be saved or uploaded.', 'Discard', true))) return;
    await discard(meta.id);
    allowNextRoute();
    location.hash = '#/';
  });
  q('save').addEventListener('click', async () => {
    const box = q('saveBox');
    meta.rpe = rpe; // 3-10 on the backend ("Very easy" = 3 ... "Extreme" = 10)
    meta.status = 'saved';
    meta.upload = 'pending';
    await putMeta(meta);
    if (meta.planSlot) calendar.markSlotDoneEarly(meta.planSlot, null, localDate(new Date(meta.startEpochMs)));
    box.innerHTML = `<div class="status busy"><span class="spinner"></span> Saving &amp; uploading…</div>`;
    const r = await syncWorkout(meta);
    if (r.upload === 'done') {
      box.innerHTML = `<div class="status ok">✓ Synced to Intervals.icu · ${esc(rpeLabel(rpe))} (RPE ${rpe})</div>
        <div class="sum-sec-row">
          <a class="btn" href="https://intervals.icu/activities/${esc(r.activityId ?? '')}" target="_blank" rel="noopener">Open in Intervals.icu</a>
          <a class="btn primary" href="#/">Done</a>
        </div>`;
    } else {
      box.innerHTML = `<div class="status warn">Saved on this phone. Upload didn’t go through:<br><small>${esc(r.lastError ?? 'unknown error')}</small><br><small>It will retry automatically next time you open the app.</small></div>
        <a class="btn primary big" href="#/">Done</a>`;
    }
  });
}
