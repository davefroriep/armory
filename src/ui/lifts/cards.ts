import { sessionStats } from '../../lifts/calc';
import { getSamples, getMeta } from '../../store/db';
import { hrSummary } from '../../lifts/intervals';
import { renderShareCard, shareImage } from '../../lifts/share';
import { liftSettings } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { calendar, localDate, addDays } from '../../intervals/calendar';
import { effectiveSkipped, fmtDuration, fmtVolume, formatSkippedItem, sessionPRs } from '../../lifts/summary';
import type { LiftSession } from '../../lifts/types';
import { esc } from '../dom';
import { openSheet, toast } from './kit';

export const stat = (label: string, value: string): string => `<div><small>${label}</small><b>${value}</b></div>`;

/** Skipped / partially logged exercises card for Summary and Workout History with 1-tap same-day / tomorrow scheduling. */
export function skippedCard(s: LiftSession): string {
  const skipped = effectiveSkipped(s);
  if (!skipped.length) return '';
  const rows = skipped.map((sk) => {
    const name = lifts.exOrStub(sk.exId).name;
    const status = sk.doneSets === 0
      ? `0/${sk.plannedSets} sets`
      : `${sk.doneSets}/${sk.plannedSets} sets · ${sk.plannedSets - sk.doneSets} skipped`;
    return `<div class="wsk-row"><b>${esc(name)}</b><span>${esc(status)}</span></div>`;
  }).join('');
  const existing = calendar.makeupForSession(s.id);
  const today = localDate();
  const tomorrow = addDays(today, 1);
  let note = '';
  if (existing?.doneDate) note = `<p class="wsk-note">✓ Make-up completed</p>`;
  else if (existing && !existing.skippedDate) {
    const lbl = existing.date <= today ? 'Today' : existing.date === tomorrow ? 'Tomorrow' : existing.date;
    note = `<p class="wsk-note">✓ Scheduled for ${esc(lbl)} (${existing.name})</p>`;
  }
  return `<section class="card wskipped" data-sk-card>
    <div class="card-h"><span>SKIPPED</span></div>
    ${rows}
    ${note}
    <div class="wsk-acts">
      <button class="btn sm" type="button" data-sk-when="today">Do later today</button>
      <button class="btn sm" type="button" data-sk-when="tomorrow">Carry to tomorrow</button>
    </div>
  </section>`;
}

export function wireSkippedCard(root: HTMLElement, getSession: () => LiftSession | undefined, onScheduled?: () => void): () => void {
  const onClick = (e: Event): void => {
    const btn = (e.target as HTMLElement).closest<HTMLElement>('[data-sk-when]');
    if (!btn) return;
    const s = getSession();
    if (!s) return;
    const skipped = effectiveSkipped(s);
    if (!skipped.length) return;
    const when = btn.dataset.skWhen as 'today' | 'tomorrow';
    const items = skipped.map((sk) => ({
      exId: sk.exId,
      sets: Math.max(1, sk.plannedSets - sk.doneSets),
      ...(sk.ss ? { ss: true } : {}),
    }));
    const description = `Skipped from ${s.name}: ${skipped.map((sk) => formatSkippedItem(sk, lifts.exOrStub)).join(' · ')}`;
    calendar.scheduleSkippedMakeup({ sessionId: s.id, name: s.name, items, when, description });
    toast(when === 'today' ? 'Added to Today’s workouts' : 'Carried over to Tomorrow');
    onScheduled?.();
  };
  root.addEventListener('click', onClick);
  return () => root.removeEventListener('click', onClick);
}

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
