import { addWeeks, e1rm, muscleSets, weekStart, totalLoadKg, sessionStats, isWorking, type ExSession } from '../../lifts/calc';
import { GROUPS, MUSCLE_LABEL, WEEKLY_BAND } from '../../lifts/muscles';
import { fmtKg, liftSettings, toDisplay } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { fmtVolume } from '../../lifts/summary';
import { esc } from '../dom';
import { barChart } from './charts';
import { LI, mount, relDay, resumeBar, tabBar, wireClock } from './kit';
import { playVideo } from './video';

/** Progress = a few honest numbers: weekly sets per muscle, consistency, your best lifts, PRs, clips. */
export function progressScreen(root: HTMLElement): () => void {
  return mount(() => {
    let off = 0;

    const paint = (): void => {
      const unit = liftSettings().unit;
      const exOf = lifts.exOrStub;
      const sessions = lifts.finished();
      const now = Date.now();
      const thisWk = weekStart(now);
      const wk0 = addWeeks(thisWk, off);
      const wk1 = addWeeks(wk0, 1);
      const inWk = sessions.filter((s) => s.startMs >= wk0 && s.startMs < wk1);
      let sets = 0;
      let vol = 0;
      for (const s of inWk) { const st = sessionStats(s, exOf); sets += st.sets; vol += st.volumeKg; }
      const wkLabel = off === 0 ? 'This week' : off === -1 ? 'Last week' : `${new Date(wk0).toLocaleDateString([], { month: 'short', day: 'numeric' })} – ${new Date(wk1 - 1).toLocaleDateString([], { month: 'short', day: 'numeric' })}`;

      // muscles
      const ms = muscleSets(sessions, exOf, wk0, wk1);
      const scaleMax = Math.max(WEEKLY_BAND[1] + 4, ...Object.values(ms));
      const [lo, hi] = WEEKLY_BAND;
      const muscleRows = GROUPS.map((g) => {
        const rows = g.muscles.map((m) => {
          const v = ms[m];
          const cls = v === 0 ? 'zero' : v < lo ? 'low' : v <= hi ? 'ok' : 'high';
          return `<div class="mrow ${cls}"><span>${MUSCLE_LABEL[m]}</span><i><u style="width:${(v / scaleMax) * 100}%"></u><s style="left:${(lo / scaleMax) * 100}%"></s><s style="left:${(hi / scaleMax) * 100}%"></s></i><b>${v ? (Number.isInteger(v) ? v : v.toFixed(1)) : '–'}</b></div>`;
        }).join('');
        return `<div class="mgrp"><h4>${g.label.toUpperCase()}</h4>${rows}</div>`;
      }).join('');

      // consistency: workouts per week, last 12
      const bars = Array.from({ length: 12 }, (_, i) => {
        const a = addWeeks(thisWk, i - 11);
        const b = addWeeks(a, 1);
        const n = sessions.filter((s) => s.startMs >= a && s.startMs < b).length;
        return { label: i === 11 ? 'now' : new Date(a).toLocaleDateString([], { month: 'numeric', day: 'numeric' }), v: n, cur: i === 11 };
      });

      // best lifts
      const byEx = new Map<string, ExSession[]>();
      for (const s of sessions) for (const e of s.exs) if (e.sets.some(isWorking) && !byEx.get(e.exId)?.some((h) => h.s === s)) byEx.set(e.exId, [...(byEx.get(e.exId) ?? []), { s, e }]);
      const top = [...byEx.entries()].filter(([id]) => lifts.exOrStub(id).type !== 'time').sort((a, b) => b[1].length - a[1].length).slice(0, 8).map(([id, hist]) => {
        const ex = exOf(id);
        let best = 0;
        for (const { s, e } of hist) for (const set of e.sets) if (isWorking(set) && set.r) best = Math.max(best, e1rm(totalLoadKg(set, ex, s.bwKg), set.r) ?? 0);
        const last = hist[hist.length - 1].s.startMs;
        return `<a class="lrow" href="#/strength/exercise/${id}"><span class="lr-main"><b>${esc(ex.name)}</b><small>${hist.length} session${hist.length === 1 ? '' : 's'} · ${relDay(last).toLowerCase()}</small></span><span class="lr-e1"><b>${best ? Math.round(toDisplay(best)) : '–'}</b><small>est 1RM</small></span><span class="chev">›</span></a>`;
      }).join('');

      // recent PRs
      const prs = lifts.prs();
      const recent: { ms: number; exId: string; n: number; kg: number; vid?: string }[] = [];
      for (const s of [...sessions].reverse()) {
        for (const e of s.exs) for (const set of e.sets) { const h = prs.get(set.id); if (h) recent.push({ ms: s.startMs, exId: e.exId, n: h.n, kg: h.kg, vid: set.vids?.[0] }); }
        if (recent.length >= 8) break;
      }
      const prHtml = recent.slice(0, 8).map((r) => {
        const ex = exOf(r.exId);
        const wTxt = ex.type === 'bodyweight' && r.kg === 0 ? 'BW' : `${fmtKg(r.kg)} ${unit}`;
        return `<div class="prl"><i>★</i><b>${esc(ex.name)}</b><span>${r.n}RM · ${wTxt}</span><small>${relDay(r.ms)}</small>${r.vid ? `<button class="vmini" data-play="${r.vid}" aria-label="Play">${LI.video}</button>` : ''}</div>`;
      }).join('');

      const clips = [...lifts.videos.values()].sort((a, b) => b.ms - a.ms).slice(0, 12);

      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Progress</h1></header>
        <main class="page has-tabs">
          <section class="card">
            <div class="cm-h"><button class="stp" data-wk="-1" aria-label="Previous week">‹</button><b>${wkLabel}</b><button class="stp" data-wk="1" aria-label="Next week" ${off >= 0 ? 'disabled' : ''}>›</button></div>
            <div class="stats three flat"><div><small>WORKOUTS</small><b>${inWk.length}</b></div><div><small>SETS</small><b>${sets}</b></div><div><small>VOLUME</small><b>${fmtVolume(vol, unit).replace(` ${unit}`, '')}</b></div></div>
          </section>
          <section class="card"><div class="card-h"><span>HARD SETS BY MUSCLE</span><span class="legend">${lo}–${hi} / wk</span></div>${muscleRows}
            <p class="hint">Primary movers count 1 set, secondary movers ½. Warm-ups never count.</p></section>
          <section class="card"><div class="card-h"><span>WORKOUTS PER WEEK</span></div>${barChart(bars)}</section>
          ${top ? `<section class="card"><div class="card-h"><span>YOUR LIFTS</span><a class="link" href="#/strength/exercises">All exercises</a></div>${top}</section>` : ''}
          ${prHtml ? `<section class="card prs"><div class="card-h"><span>RECENT RECORDS</span></div>${prHtml}</section>` : ''}
          ${clips.length ? `<section class="card"><div class="card-h"><span>LIFT VIDEOS</span></div><div class="vscroll">${clips.map((v) => `<button class="vthumb lg" data-play="${v.id}" style="background-image:url(${v.thumb})"><i>▶</i><small>${esc(exOf(v.exId).name)}</small></button>`).join('')}</div></section>` : ''}
          ${sessions.length ? '' : '<div class="empty big">Nothing to chart yet.<br>Your numbers show up after the first workout.</div>'}
        </main>${resumeBar()}${tabBar('progress')}`;
    };
    paint();
    const offClock = wireClock(root);
    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const w = t.closest<HTMLElement>('[data-wk]');
      if (w && !w.hasAttribute('disabled')) { off = Math.min(0, off + Number(w.dataset.wk)); return paint(); }
      const p = t.closest<HTMLElement>('[data-play]');
      if (p) await playVideo(p.dataset.play!);
    });
    const offStore = lifts.changed.on(paint);
    return () => { offClock(); offStore(); };
  });
}
