import { hasKey } from '../../intervals/api';
import { liftSettings, updateLiftSettings, type Unit } from '../../lifts/settings';
import { lifts, type LiftBackup } from '../../lifts/store';
import { shareImage } from '../../lifts/share';
import { allMeta } from '../../store/db';
import { retryPending } from '../../workout/sync';
import { revFooter } from '../chrome';
import { esc } from '../dom';
import { confirmSheet, mount, toast } from './kit';

/** Lift tracker settings: units, rest timer, camera, Intervals.icu status, backup. */
export function liftSettingsScreen(root: HTMLElement): () => void {
  return mount(() => {
    const paint = async (): Promise<void> => {
      const s = liftSettings();
      const metas = await allMeta();
      const pending = metas.filter((m) => m.status === 'saved' && m.upload === 'pending').length;
      const vidBytes = [...lifts.videos.values()].reduce((a, v) => a + v.bytes, 0);
      const seg = (name: string, val: string, opts: [string, string][]): string => `<div class="seg" data-seg="${name}">${opts.map(([v, l]) => `<button class="${v === val ? 'on' : ''}" data-v="${v}">${l}</button>`).join('')}</div>`;
      const tog = (name: string, on: boolean): string => `<button class="switch ${on ? 'on' : ''}" data-tog="${name}" role="switch" aria-checked="${on}"><i></i></button>`;
      root.innerHTML = `<header class="bar"><a class="icon-btn" href="#/strength" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Lift settings</h1></header>
        <main class="page settings">
          <h2 class="sec">LOGGING</h2>
          <section class="card">
            <div class="line"><span>Units</span>${seg('unit', s.unit, [['lb', 'Pounds'], ['kg', 'Kilograms']])}</div>
            <div class="line"><span>Default rest<small>Between sets, unless an exercise has its own</small></span><select class="lf-input sel" id="rest">${[30, 45, 60, 90, 120, 150, 180, 240, 300].map((n) => `<option value="${n}" ${n === s.restSec ? 'selected' : ''}>${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}</option>`).join('')}</select></div>
            <div class="line"><span>Auto-timer<small>Start rest timer automatically when checking a set</small></span>${tog('restAuto', s.restAuto)}</div>
            <div class="line"><span>Rest-over chime</span>${tog('restSound', s.restSound)}</div>
          </section>
          <h2 class="sec">LIFT VIDEOS</h2>
          <section class="card">
            <div class="line"><span>In-app camera delay<small>Fallback countdown on desktop</small></span>${seg('videoDelay', String(s.videoDelay), [['0', 'None'], ['3', '3 s'], ['5', '5 s'], ['10', '10 s']])}</div>
            <div class="hint">${lifts.videos.size} video${lifts.videos.size === 1 ? '' : 's'} linked${vidBytes ? ` · ${(vidBytes / 1048576).toFixed(0)} MB referenced` : ''}. DCIM filename links &amp; thumbnails are lightweight, stay synced in JSON backups, and search Google Drive DCIM Backup directly.</div>
          </section>
          <h2 class="sec">INTERVALS.ICU</h2>
          <section class="card">
            <div class="hint">${hasKey() ? '✓ Finished workouts upload automatically: the lift log as the description, heart-rate trace if the H10 was connected, effort rating, type Weight Training.' : 'No API key set. Add it in the main Settings to auto-log workouts.'}</div>
            ${pending ? `<div class="row"><b>${pending} waiting to upload</b><button class="btn sm primary" data-retry>Retry now</button></div>` : ''}
          </section>
          <h2 class="sec">YOUR DATA</h2>
          <section class="card">
            <div class="hint">${lifts.sessions.size} workouts · ${lifts.routines.size} days · stored on this phone.</div>
            <div class="row"><button class="btn sm" data-export>Export backup</button><button class="btn sm" data-import>Import backup</button></div>
            <input type="file" id="imp" accept="application/json,.json" hidden>
          </section>
          ${revFooter()}
        </main>`;
    };
    void paint();

    root.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const seg = t.closest<HTMLElement>('[data-seg] button');
      if (seg) {
        const name = seg.parentElement!.dataset.seg!;
        if (name === 'unit') updateLiftSettings({ unit: seg.dataset.v as Unit });
        else if (name === 'videoDelay') updateLiftSettings({ videoDelay: Number(seg.dataset.v) });
        return void paint();
      }
      const tog = t.closest<HTMLElement>('[data-tog]');
      if (tog) {
        const k = tog.dataset.tog as 'restAuto' | 'restSound';
        if (k === 'restAuto') await lifts.setMasterRestAuto(!liftSettings().restAuto);
        else updateLiftSettings({ restSound: !liftSettings().restSound });
        return void paint();
      }
      if (t.closest('[data-retry]')) { const n = await retryPending(); toast(n ? `${n} uploaded` : 'Still offline or key not accepted'); return void paint(); }
      if (t.closest('[data-export]')) {
        const blob = new Blob([JSON.stringify(lifts.exportAll(), null, 1)], { type: 'application/json' });
        try { await shareImage(blob, `armory-lifts-${new Date().toISOString().slice(0, 10)}.json`, 'Armory lift backup'); } catch { /* cancelled */ }
        return;
      }
      if (t.closest('[data-import]')) root.querySelector<HTMLInputElement>('#imp')!.click();
    });
    root.addEventListener('change', async (e) => {
      const t = e.target as HTMLInputElement;
      if (t.id === 'rest') { updateLiftSettings({ restSec: Number(t.value) }); return; }
      if (t.id !== 'imp' || !t.files?.[0]) return;
      try {
        const b = JSON.parse(await t.files[0].text()) as LiftBackup;
        if (!(await confirmSheet('Import backup?', `${b.sessions?.length ?? 0} workouts, ${b.routines?.length ?? 0} days. Matching ids are overwritten; nothing else changes.`, 'Import'))) return;
        const r = await lifts.importAll(b);
        toast(`Imported ${r.sessions} workouts, ${r.routines} days`);
        void paint();
      } catch (err) {
        toast(`Import failed: ${esc((err as Error).message)}`);
      }
    });
  });
}
