import { sessionStats, dayKey } from '../../lifts/calc';
import { liftSettings, fmtKg } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { condenseSets, fmtDuration, fmtVolume, sessionPRs } from '../../lifts/summary';
import { startWorkout } from './train';
import { getMeta, getSamples } from '../../store/db';
import { hrSummary } from '../../lifts/intervals';
import { esc } from '../dom';
import { openShareCard, sessionRow, stat } from './cards';
import { LI, actionSheet, confirmSheet, mount, resumeBar, tabBar, toast, wireClock } from './kit';
import { playVideo } from './video';

/** Month grid with a dot on every day you trained, then the list. */
export function historyScreen(root: HTMLElement): () => void {
  return mount(() => {
    const now = new Date();
    let y = now.getFullYear();
    let m = now.getMonth();
    let sel: string | null = null;

    const paint = (): void => {
      const all = lifts.finished().reverse();
      const byDay = new Map<string, typeof all>();
      for (const s of all) {
        const k = dayKey(s.startMs);
        byDay.set(k, [...(byDay.get(k) ?? []), s]);
      }
      const first = new Date(y, m, 1);
      const lead = (first.getDay() + 6) % 7;
      const days = new Date(y, m + 1, 0).getDate();
      const today = dayKey(Date.now());
      let cells = '';
      for (let i = 0; i < lead; i++) cells += '<i></i>';
      for (let d = 1; d <= days; d++) {
        const k = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        const n = byDay.get(k)?.length ?? 0;
        cells += `<button class="cal-d${n ? ' has' : ''}${k === today ? ' today' : ''}${k === sel ? ' sel' : ''}" data-day="${k}" ${n ? '' : 'disabled'}>${d}${n ? '<u></u>' : ''}</button>`;
      }
      const inMonth = all.filter((s) => { const d = new Date(s.startMs); return d.getFullYear() === y && d.getMonth() === m; });
      const list = sel ? byDay.get(sel) ?? [] : all;
      let html = '';
      let lastMonth = '';
      for (const s of list) {
        const mk = new Date(s.startMs).toLocaleDateString([], { month: 'long', year: 'numeric' });
        if (!sel && mk !== lastMonth) { html += `<h2 class="sec">${mk.toUpperCase()}</h2>`; lastMonth = mk; }
        html += sessionRow(s);
      }
      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>History</h1></header>
        <main class="page has-tabs">
          <section class="card cal-month">
            <div class="cm-h"><button class="stp" data-nav="-1" aria-label="Previous month">‹</button><b>${new Date(y, m, 1).toLocaleDateString([], { month: 'long', year: 'numeric' })}</b><button class="stp" data-nav="1" aria-label="Next month">›</button></div>
            <div class="cm-dow"><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span><span>S</span></div>
            <div class="cm-grid">${cells}</div>
            <div class="cm-f">${inMonth.length} workout${inMonth.length === 1 ? '' : 's'} this month${sel ? ` · <button class="link" data-clear>Show all</button>` : ''}</div>
          </section>
          ${html || '<div class="empty big">No workouts yet.<br>Finish one and it lands here.</div>'}
        </main>${resumeBar()}${tabBar('history')}`;
    };
    paint();
    const offClock = wireClock(root);
    const offStore = lifts.changed.on(paint);
    root.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const nav = t.closest<HTMLElement>('[data-nav]');
      if (nav) { m += Number(nav.dataset.nav); if (m < 0) { m = 11; y--; } if (m > 11) { m = 0; y++; } sel = null; return paint(); }
      const d = t.closest<HTMLElement>('[data-day]');
      if (d && !d.hasAttribute('disabled')) { sel = sel === d.dataset.day ? null : d.dataset.day!; return paint(); }
      if (t.closest('[data-clear]')) { sel = null; paint(); }
    });
    return () => { offClock(); offStore(); };
  });
}

/** One finished workout: every set, PRs, videos, share, edit, repeat, delete. */
export function workoutScreen(root: HTMLElement, args: string[]): () => void {
  return mount(() => {
    const id = args[0];
    let gen = 0;
    const paint = (): void => {
      const s = lifts.sessions.get(id);
      if (!s || s.status !== 'done') {
        location.replace('#/strength/history');
        return;
      }
      const myGen = ++gen;
      const unit = liftSettings().unit;
      const st = sessionStats(s, lifts.exOrStub);
      const prs = lifts.prs();
      const pr = sessionPRs(s, prs);

      const exHtml = s.exs.map((e) => {
        const ex = lifts.exOrStub(e.exId);
        let n = 0;
        const rows = e.sets.map((set) => {
          const label = set.kind === 'w' ? 'W' : set.kind === 'd' ? 'D' : set.kind === 'f' ? 'F' : String(++n);
          const h = prs.get(set.id);
          const w = ex.type === 'bodyweight' ? (set.w ? `+${fmtKg(set.w)}` : 'BW') : ex.type === 'time' ? (set.w ? fmtKg(set.w) : '–') : fmtKg(set.w ?? 0);
          const v = (set.vids ?? []).filter((vid) => lifts.videos.has(vid)).map((vid) => `<button class="vmini" data-play="${vid}" aria-label="Play video">${LI.video}</button>`).join('');
          return `<div class="wrow k-${set.kind}"><span class="snum">${label}</span><span class="wv">${w} <small>${ex.type === 'time' ? '' : unit}</small></span><span class="wr">× ${set.r ?? 0}${ex.type === 'time' ? 's' : ''}</span>
            <span class="wx">${set.rpe ? `<em>@${set.rpe}</em>` : ''}${h ? `<em class="prpill">★ ${h.n}RM</em>` : ''}${v}</span></div>${set.note ? `<div class="wnote">${esc(set.note)}</div>` : ''}`;
        }).join('');
        return `<section class="card wex"><div class="wex-h"><a href="#/strength/exercise/${e.exId}"><b>${esc(ex.name)}</b></a><small>${esc(condenseSets(e, ex, unit))}</small></div>${rows}</section>`;
      }).join('');

      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength/history" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>${esc(s.name)}</h1>
          <div class="chips"><button class="icon-btn" data-act="share" aria-label="Share">${LI.share}</button><button class="icon-btn" data-act="menu" aria-label="More">${LI.more}</button></div></header>
        <main class="page">
          <div class="sum-sub">${new Date(s.startMs).toLocaleString([], { weekday: 'long', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</div>
          <section class="stats four">${stat('TIME', fmtDuration(st.durationSec))}${stat('SETS', String(st.sets))}${stat('VOLUME', fmtVolume(st.volumeKg, unit).replace(` ${unit}`, ''))}${stat('PRs', String(pr.count))}</section>
          <div id="hrline"></div>
          ${s.note ? `<section class="card"><p class="note-p">${esc(s.note)}</p></section>` : ''}
          ${exHtml}
          <button class="btn big" data-act="repeat">Do this workout again</button>
          <a class="btn big" href="#/strength/session/${s.id}">Edit sets</a>
        </main>`;

      void (async () => {
        const meta = await getMeta(s.id);
        const samples = meta ? await getSamples(s.id) : [];
        if (myGen !== gen) return;
        const el = root.querySelector('#hrline');
        if (!meta || !el) return;
        const hr = hrSummary(meta, samples);
        const bits = [
          hr.has ? `♥ avg ${Math.round(hr.avg)} · max ${Math.round(hr.max)}` : '',
          hr.kcal ? `~${hr.kcal} kcal` : '',
          s.rpe ? `effort ${s.rpe}/10` : '',
          meta.activityId ? `<a class="link" target="_blank" rel="noopener" href="https://intervals.icu/activities/${esc(meta.activityId)}">Intervals.icu ↗</a>` : meta.upload === 'pending' ? 'upload pending' : '',
        ].filter(Boolean);
        if (bits.length) el.innerHTML = `<div class="hrline">${bits.join(' · ')}</div>`;
      })();
    };
    paint();
    const offStore = lifts.changed.on(paint);

    root.addEventListener('click', async (e) => {
      const s = lifts.sessions.get(id);
      if (!s) return;
      const t = e.target as HTMLElement;
      const p = t.closest<HTMLElement>('[data-play]');
      if (p) return void (await playVideo(p.dataset.play!));
      const a = t.closest<HTMLElement>('[data-act]')?.dataset.act;
      if (a === 'share') return void (await openShareCard(s));
      if (a === 'repeat') {
        const r = s.routineId ? lifts.routines.get(s.routineId) : undefined;
        if (r) return void (await startWorkout({ name: r.name, routine: r }));
        const items = s.exs.map((x) => ({ exId: x.exId, sets: Math.max(1, x.sets.filter((z) => z.kind !== 'w').length), ...(x.ss ? { ss: true } : {}) }));
        return void (await startWorkout({ name: s.name, items }));
      }
      if (a === 'menu') {
        const k = await actionSheet(s.name, [{ key: 'edit', label: 'Edit sets' }, { key: 'del', label: 'Delete workout', danger: true }]);
        if (k === 'edit') location.hash = `#/strength/session/${s.id}`;
        if (k === 'del' && (await confirmSheet('Delete workout?', 'Removes it from History and its videos from this phone. The Intervals.icu activity stays.', 'Delete', true))) {
          await lifts.deleteSession(s.id);
          toast('Workout deleted');
          location.hash = '#/strength/history';
        }
      }
    });
    return () => { offStore(); };
  });
}
