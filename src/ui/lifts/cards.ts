import { sessionStats } from '../../lifts/calc';
import { getSamples, getMeta } from '../../store/db';
import { hrSummary } from '../../lifts/intervals';
import { renderShareCard, shareImage } from '../../lifts/share';
import { liftSettings } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { fmtDuration, fmtVolume, sessionPRs } from '../../lifts/summary';
import type { LiftSession } from '../../lifts/types';
import type { WorkoutMeta } from '../../types';
import { ICON, esc, hms } from '../dom';
import { openSheet, toast } from './kit';

export const stat = (label: string, value: string): string => `<div><small>${label}</small><b>${value}</b></div>`;

/** Compact history row for a finished session. */
export function sessionRow(s: LiftSession): string {
  const unit = liftSettings().unit;
  const st = sessionStats(s, lifts.exOrStub);
  const pr = sessionPRs(s, lifts.prs()).count;
  const names = s.exs.filter((e) => e.sets.some((x) => x.done)).map((e) => lifts.exOrStub(e.exId).name);
  const d = new Date(s.startMs);
  return `<a class="hrow" href="#/strength/workout/${s.id}">
    <span class="hdate"><b>${d.getDate()}</b><small>${d.toLocaleDateString([], { month: 'short' })}</small></span>
    <span class="hmain"><b>${esc(s.name)}${pr ? ` <em class="prpill">★ ${pr}</em>` : ''}</b>
      <small>${esc(names.slice(0, 3).join(' · '))}${names.length > 3 ? ` · +${names.length - 3}` : ''}</small>
      <small class="hstats">${fmtDuration(st.durationSec)} · ${st.sets} sets · ${fmtVolume(st.volumeKg, unit)}</small></span>
    <span class="chev">›</span></a>`;
}

/** Compact history row for a saved Ruck session. */
export function ruckRow(m: WorkoutMeta): string {
  const d = new Date(m.startEpochMs);
  const details = [
    hms(m.seconds),
    m.avgHr > 0 ? `${Math.round(m.avgHr)} bpm` : '',
    m.notes ? m.notes : '',
  ].filter(Boolean).join(' · ');
  return `<a class="hrow" href="#/airbike/w/${m.id}">
    <span class="cal-ico sm ruck">${ICON.ruck}</span>
    <span class="hmain"><b>${esc(m.name)} <em class="prpill" style="background:rgba(245,158,11,.16);color:#fbbf24">Ruck</em></b>
      <small>${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}${details ? ` · ${esc(details)}` : ''}</small></span>
    <span class="chev">›</span></a>`;
}

/** Preview + share the condensed lift card for a session. */
export async function openShareCard(s: LiftSession): Promise<void> {
  const sh = openSheet(`<h3 class="sh-title">Lift card</h3><div class="sc-wrap"><div class="sc-load"><span class="spinner"></span></div></div>
    <button class="btn primary big" data-share disabled>Share</button><button class="btn big" data-ok>Close</button>`);
  const wrap = sh.body.querySelector<HTMLElement>('.sc-wrap')!;
  const btn = sh.body.querySelector<HTMLButtonElement>('[data-share]')!;
  sh.body.querySelector('[data-ok]')!.addEventListener('click', () => sh.close());
  let blob: Blob;
  try {
    const meta = await getMeta(s.id);
    const hr = meta ? hrSummary(meta, await getSamples(s.id)) : null;
    blob = await renderShareCard(s, hr?.has ? { avg: hr.avg, max: hr.max } : undefined);
  } catch (e) {
    wrap.innerHTML = `<div class="empty">${esc((e as Error).message)}</div>`;
    return;
  }
  const url = URL.createObjectURL(blob);
  wrap.innerHTML = `<img class="sc-img" src="${url}" alt="Lift card">`;
  btn.disabled = false;
  const name = `lift-${new Date(s.startMs).toISOString().slice(0, 10)}-${s.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.png`;
  btn.addEventListener('click', async () => {
    try {
      const r = await shareImage(blob, name, `${s.name} · Armory Fit`);
      if (r === 'saved') toast('Card saved to downloads');
    } catch (e) {
      if ((e as Error).name !== 'AbortError') toast('Could not share the card');
    }
  });
  const obs = new MutationObserver(() => { if (!document.body.contains(sh.el)) { URL.revokeObjectURL(url); obs.disconnect(); } });
  obs.observe(document.body, { childList: true });
}
