import { LeaveGuard } from './leaveGuard';
import { hrFresh, live } from '../ble/telemetry';
import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { isNative, openBoostcamp, workoutService } from '../native/service';
import { bodyProfile, kcalFromSamples } from '../domain/energy';
import { getSettings } from '../store/settings';
import { discard } from '../workout/sync';
import { Recorder } from '../workout/recorder';
import { calendarCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';
import { $, hms, mmss } from './dom';
import { ZONE_COLOR, hrZone } from './hr';
import { renderSummary } from './summaryView';

/**
 * Strength = heart-rate logging only. You lift in Boostcamp; this records the H10 for the session
 * (like Wahoo's "Strength Train"), asks how hard it was, and uploads it to Intervals.icu.
 */
export function strengthScreen(root: HTMLElement, _args: string[], query: URLSearchParams): () => void {
  ensureSensors({ bike: false });
  void calendar.refresh();
  const eventId = query.get('ev') ? Number(query.get('ev')) : null;

  let rec: Recorder | null = null;
  let state: 'ready' | 'running' | 'paused' | 'summary' = 'ready';
  let t0 = 0;
  let accMs = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  let lock: WakeLockSentinel | null = null;
  let lastSvc = 0;
  let offChips: (() => void) | null = null;
  let offCal: (() => void) | null = null;
  let offRec: (() => void) | null = null;
  const guard = new LeaveGuard(() => state === 'running' || state === 'paused');

  const elapsed = () => (accMs + (state === 'running' ? Date.now() - t0 : 0)) / 1000;

  const paintReady = () => {
    root.innerHTML = `${topbar({ back: '#/', title: 'Strength', cog: true })}
      <main class="page">
        ${calendarCard(calendar.forEquipment('strength'), false)}
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
          <button class="metro" id="openBoostcamp" type="button">OPEN BOOSTCAMP</button>
          <button class="go" id="go">START</button>
        </div>
        <p class="hint c">Start logging, then lift in Boostcamp. Come back and finish to save your heart-rate session.</p>
        ${revFooter()}
      </main>
      <div class="modal" id="pauseModal" hidden>
        <div class="sheet"><h2>Paused</h2><p id="pauseSub"></p>
          <button class="btn primary big" id="resume">Resume</button>
          <button class="btn big" id="finish">Finish session</button></div></div>`;
    offChips?.();
    offChips = wireChips(root);
    offCal?.();
    offCal = wireCalendar(root, () => { if (state === 'ready') paintReady(); });
    $('#openBoostcamp', root).addEventListener('click', () => { void openBoostcamp(); });
    $('#go', root).addEventListener('click', () => (state === 'ready' ? start() : pause()));
    $('#resume', root).addEventListener('click', resume);
    $('#finish', root).addEventListener('click', finish);
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
        void workoutService.update('Armory Fit · Strength', `${mmss(el)} · ${hr || '--'} BPM`);
      }
    }
    const go = $('#go', root);
    go.textContent = state === 'ready' ? 'START' : state === 'paused' ? 'PAUSED' : 'PAUSE';
    go.classList.toggle('pause', state !== 'ready');
  };

  const start = () => {
    const ev = eventId == null ? undefined : calendar.items.find((i) => i.ev.id === eventId);
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    rec = new Recorder(id, 'strength', 'strength', 'Strength Train', ev?.ev.id ?? null, getSettings().hrMax);
    state = 'running';
    // Beat-driven recording: keeps logging while Android throttles this page's timers in the background.
    offRec?.();
    offRec = rec.attach(() => (state === 'running' ? { elapsed: elapsed(), targetRpm: 0 } : null));
    guard.arm();
    t0 = Date.now();
    accMs = 0;
    if (!isNative()) void navigator.wakeLock?.request('screen').then((l) => { lock = l; }).catch(() => {});
    void workoutService.start('Armory Fit · Strength', 'Logging heart rate…');
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
  const finish = () => {
    if (state === 'running') accMs += Date.now() - t0;
    rec?.tick(elapsed(), 0);
    offRec?.();
    offRec = null;
    state = 'summary';
    guard.disarm();
    void workoutService.stop();
    void lock?.release().catch(() => {});
    lock = null;
    offChips?.();
    offChips = null;
    const meta = rec?.finalize();
    if (!rec || !meta || meta.seconds < 5) {
      root.innerHTML = `${topbar({})}<main class="page"><section class="card"><h3>Nothing recorded</h3><p>No heart-rate data was captured.</p><a class="btn primary" href="#/strength">Back</a></section>${revFooter()}</main>`;
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
      title: 'Strength Train',
      meta,
      stats: [['AVG HR', meta.avgHr ? String(Math.round(meta.avgHr)) : '--'], ['MAX HR', meta.maxHr ? String(Math.round(meta.maxHr)) : '--'], ['TIME', hms(meta.seconds)], ['KCAL', kcal != null ? `~${kcal}` : '--']],
      extra: `<section class="card zones"><div class="card-h"><span>TIME IN HR ZONE</span></div>${bars}</section>`,
      backHref: '#/',
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
    offChips?.();
    offCal?.();
    void workoutService.stop();
    void lock?.release().catch(() => {});
    window.removeEventListener('beforeunload', unload);
  };
}
