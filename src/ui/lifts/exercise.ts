import { PR_MAX_REPS, e1rmTrend, exerciseSessions, repBests, isWorking } from '../../lifts/calc';
import { gymCam } from '../../lifts/gymCam';
import { inferredCamForExercise, preferredCamForExercise } from '../../lifts/logic';
import { EQUIP_LABEL, MUSCLE_LABEL } from '../../lifts/muscles';
import { fmtKg, liftSettings, toDisplay } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { condenseSets } from '../../lifts/summary';
import { shortCamLabel } from '../../lifts/tape';
import { esc } from '../dom';
import { lineChart } from './charts';
import { LI, actionSheet, clock, confirmSheet, mount, relDay, resumeBar, tabBar, toast, wireClock } from './kit';
import { exerciseForm } from './picker';
import { playVideo } from './video';

const DAY = 86_400_000;
const RANGES: [string, number][] = [['3M', 90], ['6M', 180], ['1Y', 365], ['All', 0]];

/** Everything about one lift: rep records (1RM-5RM and beyond), est. 1RM trend, clips, history. */
export function exerciseScreen(root: HTMLElement, args: string[]): () => void {
  return mount(() => {
    const exId = args[0];
    let ex = lifts.exercise(exId);
    if (!ex) {
      location.replace('#/strength/exercises');
      return;
    }
    let range = 1;
    let more = false;

    const paint = (): void => {
      ex = lifts.exercise(exId)!;
      const unit = liftSettings().unit;
      const hist = exerciseSessions(lifts.finished(), exId);
      const bests = repBests(hist);
      const trend = e1rmTrend(hist, ex);
      const prs = lifts.prs();
      const isTime = ex.type === 'time';
      const row = lifts.row(exId);

      let heaviest = 0;
      let heaviestReps = 0;
      let bestHold = 0;
      for (const { e } of hist) for (const s of e.sets) {
        if (!isWorking(s)) continue;
        if (isTime) bestHold = Math.max(bestHold, s.r ?? 0);
        else if ((s.w ?? 0) > heaviest || ((s.w ?? 0) === heaviest && (s.r ?? 0) > heaviestReps)) { heaviest = s.w ?? 0; heaviestReps = s.r ?? 0; }
      }
      const bestE1 = trend.length ? Math.max(...trend.map((t) => t.e1rm)) : 0;

      const recRow = (n: number): string => {
        const b = bests[n];
        const vid = b?.vidId;
        const val = b ? (ex!.type === 'bodyweight' && b.kg === 0 ? 'BW' : `${fmtKg(b.kg)} <small>${unit}</small>`) : '–';
        const dateStr = b ? new Date(b.ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: '2-digit' }) : '';
        return `<div class="rec-row${b ? '' : ' none'}">
          <button class="rec-main" ${b ? `data-sess="${b.sessionId}"` : 'disabled'}>
            <span class="rm">${n}RM</span><b>${val}</b><small>${dateStr}</small>
          </button>
          ${vid && lifts.videos.has(vid) ? `<button class="vmini" data-play="${vid}" aria-label="Play video">${LI.video}</button>` : '<span></span>'}
        </div>`;
      };

      const days = RANGES[range][1];
      const cut = days ? Date.now() - days * DAY : 0;
      const prSessions = new Set(hist.filter(({ e }) => e.sets.some((s) => prs.has(s.id))).map(({ s }) => s.startMs));
      const pts = trend.filter((t) => t.ms >= cut).map((t) => ({ x: t.ms, y: toDisplay(t.e1rm), hi: prSessions.has(t.ms) }));
      const clips = lifts.videosForExercise(exId);
      const sessList = [...hist].reverse().slice(0, 25);

      const rmList = more
        ? [
            ...Array.from({ length: 12 }, (_, i) => i + 1),
            ...Array.from({ length: PR_MAX_REPS - 12 }, (_, i) => i + 13).filter((n) => bests[n] != null && bests[n]!.reps === n),
          ]
        : [1, 2, 3, 4, 5];

      root.innerHTML = `<header class="bar"><button class="icon-btn" data-act="back" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></button><h1>${esc(ex.name)}</h1>
          <div class="chips"><button class="icon-btn" data-act="menu" aria-label="More">${LI.more}</button></div></header>
        <main class="page has-tabs">
          <div class="mtags">${ex.primary.map((m) => `<span class="mtag p">${MUSCLE_LABEL[m]}</span>`).join('')}${ex.secondary.map((m) => `<span class="mtag">${MUSCLE_LABEL[m]}</span>`).join('')}<span class="mtag eq">${EQUIP_LABEL[ex.equip]}</span></div>
          <section class="stats four">
            <div><small>${isTime ? 'BEST HOLD' : 'EST 1RM'}</small><b>${isTime ? (bestHold ? `${bestHold}s` : '–') : bestE1 ? Math.round(toDisplay(bestE1)) : '–'}</b></div>
            <div><small>HEAVIEST</small><b>${heaviest ? `${fmtKg(heaviest)}<em>×${heaviestReps}</em>` : isTime ? '–' : hist.length ? 'BW' : '–'}</b></div>
            <div><small>SESSIONS</small><b>${hist.length}</b></div>
            <div><small>LAST</small><b class="sm">${hist.length ? relDay(hist[hist.length - 1].s.startMs) : '–'}</b></div></section>
          ${isTime ? '' : `<section class="card"><div class="card-h"><span>REP RECORDS</span><button class="link" data-act="more">${more ? 'Show less' : 'Show more'}</button></div>
            <div class="recs">${rmList.map((n) => recRow(n)).join('')}</div>
            <p class="hint">Heaviest load for at least that many reps (1RM–${PR_MAX_REPS}RM). Tap a record to open that workout (or play its video).</p></section>
            <section class="card"><div class="card-h"><span>EST 1RM · ${unit.toUpperCase()}</span><div class="seg mini">${RANGES.map(([l], i) => `<button class="${i === range ? 'on' : ''}" data-range="${i}">${l}</button>`).join('')}</div></div>${lineChart(pts)}</section>`}
          ${clips.length ? `<section class="card"><div class="card-h"><span>VIDEOS</span></div><div class="vgrid">${clips.map((v) => {
            const set = lifts.sessions.get(v.sessionId)?.exs.flatMap((x) => x.sets).find((x) => x.id === v.setId);
            const lbl = set ? `${fmtKg(set.w ?? 0)}×${set.r ?? 0}` : (v.fileName ?? '');
            return `<button class="vthumb lg" data-play="${v.id}" style="${v.thumb ? `background-image:url(${v.thumb})` : ''}"><i>▶</i><small>${esc(lbl)} · ${new Date(v.ms).toLocaleDateString([], { month: 'short', day: 'numeric' })}</small></button>`;
          }).join('')}</div></section>` : ''}
          <section class="card"><div class="card-h"><span>HISTORY</span></div>${sessList.length ? sessList.map(({ s, e }) => `<a class="ehr" href="#/strength/workout/${s.id}"><b>${new Date(s.startMs).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}</b><span>${esc(condenseSets(e, ex, unit))}</span></a>`).join('') : '<div class="empty">Not logged yet.</div>'}</section>
          <section class="card">
            <div class="line"><span>Rest timer<small>For this exercise</small></span><button class="btn sm" data-act="rest">${clock(row?.restSec ?? liftSettings().restSec)}</button></div>
            <div class="line"><span>Preferred camera<small>Auto station for this exercise</small></span><button class="btn sm" data-act="cam">${(() => {
              const res = preferredCamForExercise(ex, row);
              const inf = inferredCamForExercise(ex);
              if (!row?.camPref) return inf ? `${shortCamLabel(inf)} (default)` : 'Auto';
              return row.camPref === 'auto' ? 'Auto' : shortCamLabel(res || row.camPref);
            })()}</button></div>
          </section>
        </main>${resumeBar()}${tabBar('exercises')}`;
    };
    paint();
    const offClock = wireClock(root);
    const offStore = lifts.changed.on(paint);

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const p = t.closest<HTMLElement>('[data-play]');
      if (p) return void (await playVideo(p.dataset.play!));
      const sess = t.closest<HTMLElement>('[data-sess]');
      if (sess) { location.hash = `#/strength/workout/${sess.dataset.sess}`; return; }
      const r = t.closest<HTMLElement>('[data-range]');
      if (r) { range = Number(r.dataset.range); return paint(); }
      const a = t.closest<HTMLElement>('[data-act]')?.dataset.act;
      if (a === 'back') { if (history.length > 1) history.back(); else location.hash = '#/strength/exercises'; return; }
      if (a === 'more') { more = !more; return paint(); }
      if (a === 'rest') {
        const opts = [30, 45, 60, 90, 120, 150, 180, 240, 300];
        const k = await actionSheet('Rest timer', opts.map((o) => ({ key: String(o), label: clock(o), on: o === (lifts.row(exId)?.restSec ?? liftSettings().restSec) })));
        if (k) await lifts.saveRow({ ...(lifts.row(exId) ?? { id: exId }), restSec: Number(k) });
        return;
      }
      if (a === 'cam') {
        const curEx = lifts.exercise(exId)!;
        const curRow = lifts.row(exId);
        const cams = gymCam.cameras.length ? gymCam.cameras : ['Rack Cam', 'Smith Cam'];
        const inf = inferredCamForExercise(curEx);
        const k = await actionSheet('Preferred camera', [
          { key: '__default__', label: inf ? `Default (${shortCamLabel(inf)})` : 'Default (Auto)', on: !curRow?.camPref },
          ...cams.map((c) => ({ key: c, label: shortCamLabel(c), sub: c, on: curRow?.camPref === c })),
          ...(inf ? [{ key: 'auto', label: 'Auto (Motion)', sub: 'Override default with strongest motion', on: curRow?.camPref === 'auto' }] : []),
        ]);
        if (k) {
          const nextRow = { ...(curRow ?? { id: exId }) };
          if (k === '__default__') delete nextRow.camPref;
          else nextRow.camPref = k;
          await lifts.saveRow(nextRow);
        }
        return;
      }
      if (a === 'menu') {
        const cur = lifts.exercise(exId)!;
        const k = await actionSheet(cur.name, [
          ...(cur.custom ? [{ key: 'edit', label: 'Edit exercise' }, { key: 'del', label: 'Delete exercise', danger: true }] : [{ key: 'muscles', label: 'Edit muscle mapping', sub: 'Changes how its sets count toward each muscle' }]),
        ]);
        if (k === 'edit' || k === 'muscles') await exerciseForm(cur);
        else if (k === 'del' && (await confirmSheet(`Delete ${cur.name}?`, lifts.isUsed(exId) ? 'It has history, so it is hidden from pickers but past workouts keep it.' : '', 'Delete', true))) {
          await lifts.deleteCustom(exId);
          toast('Exercise deleted');
          location.hash = '#/strength/exercises';
        }
      }
    });
    return () => { offClock(); offStore(); };
  });
}
