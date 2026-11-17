import { audio } from '../../audio/audio';
import { hasKey } from '../../intervals/api';
import { canShowRestOverlay, isNative, openAppInfoSettings, requestRestOverlayPermission } from '../../native/service';
import { liftSettings, updateLiftSettings, type Unit } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { allMeta } from '../../store/db';
import { retryPending } from '../../workout/sync';
import { backupCardHtml, wireBackupCard } from '../backupUi';
import { revFooter } from '../chrome';
import { mount, openSheet, toast } from './kit';

/** Lift tracker settings: units, rest timer, camera, Intervals.icu status, backup. */
export function liftSettingsScreen(root: HTMLElement): () => void {
  return mount(() => {
    const paint = async (): Promise<void> => {
      const s = liftSettings();
      const [metas, overlayOn] = await Promise.all([allMeta(), canShowRestOverlay()]);
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
            <div class="line"><span>Show timer button<small>Show ▶ rest timer button on exercise cards</small></span>${tog('showTimer', s.showTimer)}</div>
            <div class="line"><span>Show auto-timer<small>Show Auto toggle pill on exercise cards</small></span>${tog('showAutoTimer', s.showAutoTimer)}</div>
            <div class="line"><span>Auto-timer<small>Start rest timer automatically when checking a set</small></span>${tog('restAuto', s.restAuto)}</div>
            <div class="line"><span>Rest-over chime<small>Ducks music cleanly during 3-ding alert</small></span><div class="row">${tog('restSound', s.restSound)}<button class="btn sm" data-test-chime>Test</button></div></div>
            ${isNative() ? `<div class="line"><span>Floating timer overlay<small>Top mini-bar over other apps when minimized</small></span><button class="btn sm${overlayOn ? '' : ' primary'}" data-overlay>${overlayOn ? 'Enabled' : 'Enable ›'}</button></div>` : ''}
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
          <h2 class="sec">BACKUP &amp; RESTORE</h2>
          ${backupCardHtml(`${lifts.sessions.size} workouts · ${lifts.routines.size} days · stored on this phone.`)}
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
        const k = tog.dataset.tog as 'restAuto' | 'restSound' | 'showTimer' | 'showAutoTimer';
        if (k === 'restAuto') await lifts.setMasterRestAuto(!liftSettings().restAuto);
        else updateLiftSettings({ [k]: !liftSettings()[k] });
        return void paint();
      }
      if (t.closest('[data-test-chime]')) { audio.unlock(); audio.chime(); return; }
      if (t.closest('[data-overlay]')) {
        if (await canShowRestOverlay()) {
          await requestRestOverlayPermission();
          return;
        }
        const sh = openSheet(
          `<h3 class="sh-title">Unlock floating overlay</h3>
           <p class="sh-text">Android blocks "Display over other apps" on sideloaded APKs until you allow it once:<br><br>
           1. Tap <b>App Info</b> below -> tap <b>⋮</b> (top right) -> <b>Allow restricted settings</b><br>
           2. Come back -> tap <b>Overlay Toggle</b> and switch it on</p>
           <button class="btn primary big" type="button" data-ov="info">1. Open App Info</button>
           <button class="btn big" type="button" data-ov="perm">2. Open Overlay Toggle</button>
           <button class="btn big" type="button" data-ov="close">Done</button>`,
        );
        sh.body.addEventListener('click', (ev) => {
          const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-ov]');
          if (!b) return;
          if (b.dataset.ov === 'info') void openAppInfoSettings();
          else if (b.dataset.ov === 'perm') void requestRestOverlayPermission();
          else sh.close();
        });
        return;
      }
      if (t.closest('[data-retry]')) { const n = await retryPending(); toast(n ? `${n} uploaded` : 'Still offline or key not accepted'); return void paint(); }
    });
    root.addEventListener('change', (e) => {
      const t = e.target as HTMLInputElement;
      if (t.id === 'rest') updateLiftSettings({ restSec: Number(t.value) });
    });
    const offBackup = wireBackupCard(root, () => { void paint(); });
    const refreshVis = () => { if (!document.hidden) void paint(); };
    document.addEventListener('visibilitychange', refreshVis);
    return () => {
      offBackup();
      document.removeEventListener('visibilitychange', refreshVis);
    };
  });
}
