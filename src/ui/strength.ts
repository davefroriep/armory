import { LeaveGuard } from './leaveGuard';
import { hrFresh, live } from '../ble/telemetry';
import { ensureSensors } from '../ble/sources';
import { isNative, workoutService } from '../native/service';
import { bodyProfile, kcalFromSamples } from '../domain/energy';
import { getSettings } from '../store/settings';
import { discard } from '../workout/sync';
import { Recorder } from '../workout/recorder';
import { allowNextRoute } from '../main';
import { revFooter, topbar, wireChips } from './chrome';
import { $, esc, hms, mmss } from './dom';
import { ZONE_COLOR, hrZone } from './hr';
import { confirmSheet } from './lifts/kit';
import { renderSummary } from './summaryView';
import { setWallboardCardioState } from '../wallboard/state';

const PACK_KEY = 'armory.ruck.packLb';
const PACK_OPTIONS = [0, 20, 30, 35, 45];

function loadPackLb(): number {
  if (typeof localStorage === 'undefined') return 35;
  const v = Number(localStorage.getItem(PACK_KEY));
  return Number.isFinite(v) && PACK_OPTIONS.includes(v) ? v : 35;
}

/**
 * Outdoor Ruck / HR-only logger (`#/strength/hr`) powered by the Polar H10 chest strap.
 * Records 1 Hz HR + Keytel kcal and uploads to Intervals.icu as a Walk/Hike activity.
 */
export function strengthHrScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: false });

  let packLb = loadPackLb();
  let rec: Recorder | null = null;
  let state: 'ready' | 'running' | 'paused' | 'summary' = 'ready';
  let t0 = 0;
  let accMs = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  let lock: WakeLockSentinel | null = null;
  let lastSvc = 0;
  let offChips: (() => void) | null = null;
  let offRec: (() => void) | null = null;
  const guard = new LeaveGuard(() => state === 'running' || state === 'paused');

  const sessionTitle = (): string => (packLb > 0 ? `Ruck · ${packLb} lb` : 'Outdoor HR');
  const elapsed = () => (accMs + (state === 'running' ? Date.now() - t0 : 0)) / 1000;

  const paintReady = () => {
    const chips = PACK_OPTIONS.map((w) =>
      `<button class="fchip${packLb === w ? ' on' : ''}" type="button" data-pack="${w}">${w === 0 ? 'No pack' : `${w} lb`}</button>`,
    ).join('');
    root.innerHTML = `${topbar({ back: '#/', title: 'Ruck / Outdoor HR', cog: true })}
      <main class="page">
        <section class="card">
          <div class="card-h"><span>PACK WEIGHT</span></div>
          <div class="chips-row wrap">${chips}</div>
        </section>
        <section class="card strength">
          <small class="lbl">HEART RATE</small>
          <div class="hr-big" id="hrBig">--</div>
          <div class="hr-sub" id="hrSub">connect your Polar H10</div>
          <div class="strip s3">
            <div><small>ELAPSED</small><b id="sEl">0:00</b></div>
            <div><small>AVG HR</small><b id="sAvg">--</b></div>
            <div><small>MAX HR</small><b id="sMax">--</b></div>
          </div>
        </section>
        <div class="ctrl nopad">
          <button class="go" id="go">START ${esc(sessionTitle().toUpperCase())}</button>
        </div>
        <p class="hint c">Logs 1 Hz Polar H10 heart rate + calories to Intervals.icu (Walk activity for ruck/outdoor walks).</p>
        ${revFooter()}
      </main>
      <div class="modal" id="pauseModal" hidden>
        <div class="sheet"><h2>Paused</h2><p id="pauseSub"></p>
          <button class="btn primary big" id="resume">Resume</button>
          <button class="btn big" id="finish">Finish session</button>
          <button class="btn danger big" id="discardRuck">Discard workout</button></div></div>`;
    offChips?.();
    offChips = wireChips(root);
    root.querySelectorAll<HTMLElement>('[data-pack]').forEach((b) =>
      b.addEventListener('click', () => {
        if (state !== 'ready') return;
        packLb = Number(b.dataset.pack);
        if (typeof localStorage !== 'undefined') localStorage.setItem(PACK_KEY, String(packLb));
        root.querySelectorAll<HTMLElement>('[data-pack]').forEach((x) => x.classList.toggle('on', x === b));
        $('#go', root).textContent = `START ${sessionTitle().toUpperCase()}`;
      }),
    );
    $('#go', root).addEventListener('click', () => (state === 'ready' ? start() : pause()));
    $('#resume', root).addEventListener('click', resume);
    $('#finish', root).addEventListener('click', finish);
    $('#discardRuck', root).addEventListener('click', () => void discardSession());
  };

  const paintLive = () => {
    if (state === 'summary') return;
    const hr = hrFresh() ? live.hr : 0;
    const z = hr ? hrZone(hr, getSettings().hrMax) : null;
    const big = $('#hrBig', root);
    big.textContent = hr ? String(hr) : '--';
    big.style.color = z ? ZONE_COLOR[z.z] : '';
    $('#hrSub', root).textContent = z ? `Zone ${z.z} · ${Math.round(z.pct * 100)}% of max` : 'connect your Polar H10';
    const el = elapsed();
    $('#sEl', root).textContent = hms(el);
    if (rec) {
      rec.tick(el, 0);
      $('#sAvg', root).textContent = rec.avgHr ? String(Math.round(rec.avgHr)) : '--';
      $('#sMax', root).textContent = rec.meta.maxHr ? String(Math.round(rec.meta.maxHr)) : '--';
      if (state === 'running' && Date.now() - lastSvc > 5000) {
        lastSvc = Date.now();
        void workoutService.update(`Armory Fit · ${sessionTitle()}`, `${mmss(el)} · ${hr || '--'} BPM`);
      }
    }
    const go = $('#go', root);
    go.textContent = state === 'ready' ? `START ${sessionTitle().toUpperCase()}` : state === 'paused' ? 'PAUSED' : 'PAUSE';
    go.classList.toggle('pause', state !== 'ready');
    if (state === 'running' || state === 'paused') {
      setWallboardCardioState({
        title: sessionTitle(),
        phase: state === 'paused' ? 'PAUSED' : 'OUTDOOR / RUCK',
        phaseTone: state === 'paused' ? 'done' : 'work',
        repText: packLb > 0 ? `${packLb} lb pack` : 'Bodyweight HR',
        countdownText: hms(el),
        targetText: z ? `Zone ${z.z} · ${Math.round(z.pct * 100)}% HRmax` : 'Polar H10 Live HR',
        watts: 0,
        targetWatts: null,
        rpm: 0,
        targetRpm: null,
        elapsedSec: el,
        kcal: 0,
        z5Sec: 0,
        paused: state === 'paused',
      });
    }
  };

  const start = () => {
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const title = sessionTitle();
    rec = new Recorder(id, 'strength', 'ruck', title, null, getSettings().hrMax);
    if (packLb > 0) rec.meta.notes = `Pack weight: ${packLb} lb`;
    state = 'running';
    offRec?.();
    offRec = rec.attach(() => (state === 'running' ? { elapsed: elapsed(), targetRpm: 0 } : null));
    guard.arm();
    t0 = Date.now();
    accMs = 0;
    if (!isNative()) void navigator.wakeLock?.request('screen').then((l) => { lock = l; }).catch(() => {});
    void workoutService.start(`Armory Fit · ${title}`, 'Logging heart rate…');
  };
  const pause = () => {
    if (state !== 'running') return;
    accMs += Date.now() - t0;
    state = 'paused';
    $('#pauseSub', root).textContent = `${hms(elapsed())} elapsed`;
    $('#pauseModal', root).hidden = false;
  };
  const resume = () => {
    $('#pauseModal', root).hidden = true;
    t0 = Date.now();
    state = 'running';
  };
  const discardSession = async () => {
    if (!(await confirmSheet('Discard workout?', 'Everything recorded so far is lost.', 'Discard', true))) return;
    if (timer) { clearInterval(timer); timer = null; }
    rec?.tick(elapsed(), 0);
    offRec?.();
    offRec = null;
    state = 'summary';
    guard.dispose();
    setWallboardCardioState(null);
    void workoutService.stop();
    void lock?.release().catch(() => {});
    lock = null;
    $('#pauseModal', root).hidden = true;
    if (rec) {
      await discard(rec.meta.id);
      rec = null;
    }
    allowNextRoute();
    location.hash = '#/';
  };
  const finish = () => {
    if (state === 'running') accMs += Date.now() - t0;
    rec?.tick(elapsed(), 0);
    offRec?.();
    offRec = null;
    state = 'summary';
    guard.disarm();
    setWallboardCardioState(null);
    void workoutService.stop();
    void lock?.release().catch(() => {});
    lock = null;
    offChips?.();
    offChips = null;
    const meta = rec?.finalize();
    if (!rec || !meta || meta.seconds < 5) {
      root.innerHTML = `${topbar({})}<main class="page"><section class="card"><h3>Nothing recorded</h3><p>No heart-rate data was captured.</p><a class="btn primary" href="#/">Back to Home</a></section>${revFooter()}</main>`;
      if (meta) void discard(meta.id);
      return;
    }
    const max = getSettings().hrMax;
    const secs = [0, 0, 0, 0, 0, 0];
    for (const s of rec.samples) if (s.hr > 0) secs[hrZone(s.hr, max).z]++;
    const prof = bodyProfile(getSettings());
    const kcal = prof ? kcalFromSamples(rec.samples, prof) : null;
    const total = Math.max(1, secs.reduce((a, b) => a + b, 0));
    const bars = [1, 2, 3, 4, 5].map((z) =>
      `<div class="zrow"><span>Z${z}</span><i><u style="width:${(secs[z] / total) * 100}%;background:${ZONE_COLOR[z]}"></u></i><b>${mmss(secs[z])}</b></div>`).join('');
    renderSummary(root, {
      title: meta.name,
      meta,
      stats: [['AVG HR', meta.avgHr ? String(Math.round(meta.avgHr)) : '--'], ['MAX HR', meta.maxHr ? String(Math.round(meta.maxHr)) : '--'], ['TIME', hms(meta.seconds)], ['KCAL', kcal != null ? `~${kcal}` : '--']],
      extra: `<section class="card zones"><div class="card-h"><span>TIME IN HR ZONE</span></div>${bars}</section>`,
      backHref: '#/strength',
    });
  };

  paintReady();
  timer = setInterval(paintLive, 250);

  const unload = (e: BeforeUnloadEvent) => { if (state === 'running' || state === 'paused') e.preventDefault(); };
  window.addEventListener('beforeunload', unload);
  return () => {
    if (timer) clearInterval(timer);
    offRec?.();
    guard.dispose();
    setWallboardCardioState(null);
    offChips?.();
    void workoutService.stop();
    void lock?.release().catch(() => {});
    window.removeEventListener('beforeunload', unload);
  };
}
