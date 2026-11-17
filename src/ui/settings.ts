import { bikeLink, hrLink } from '../ble/devices';
import { transport } from '../ble/transport';
import { sim } from '../ble/sim';
import { ensureSensors } from '../ble/sources';
import { hasKey, syncAthleteFtp, testConnection } from '../intervals/api';
import { calendar } from '../intervals/calendar';
import { audio } from '../audio/audio';
import { recoveryLimitBpm } from '../domain/gate';
import { PHASE1_PLAN } from '../domain/plan';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { fmtNum, fromDisplay, liftSettings, toDisplay } from '../lifts/settings';
import { voice } from '../lifts/voice';
import { wallboard } from '../wallboard/sync';
import { backupCardHtml, wireBackupCard } from './backupUi';
import { revFooter, topbar, wireChips } from './chrome';
import { $, esc } from './dom';
import { toast } from './lifts/kit';

export function settingsScreen(root: HTMLElement): () => void {
  const s = getSettings();
  const seg = (name: string, val: string, opts: [string, string][]) =>
    `<div class="seg" data-seg="${name}">${opts.map(([v, l]) => `<button class="${v === val ? 'on' : ''}" data-v="${v}">${l}</button>`).join('')}</div>`;
  const tog = (name: string, on: boolean) => `<button class="switch ${on ? 'on' : ''}" data-tog="${name}" role="switch" aria-checked="${on}"><i></i></button>`;
  const num = (name: string, v: number, min: number, max: number) =>
    `<input class="num" type="number" inputmode="numeric" data-num="${name}" min="${min}" max="${max}" value="${v}">`;
  const ftpStatus = () => {
    const cur = getSettings();
    const u = liftSettings().unit;
    const wtTxt = cur.weightKg ? `${fmtNum(toDisplay(cur.weightKg, u))} ${u}${cur.weightDate ? ` (${cur.weightDate})` : ''}` : '—';
    return `Modeled eFTP: <b>${cur.intervalsEftp != null ? `${cur.intervalsEftp} W` : '—'}</b> · Manual: <b>${cur.intervalsSetFtp != null ? `${cur.intervalsSetFtp} W` : '—'}</b> · Scale: <b>${esc(wtTxt)}</b>`;
  };
  const unit = liftSettings().unit;
  const bwVal = s.weightKg ? fmtNum(toDisplay(s.weightKg, unit)) : '';

  root.innerHTML = `${topbar({ back: '#/', title: 'Settings' })}
  <main class="page settings">
    <h2 class="sec">TRAINING &amp; FTP</h2>
    <section class="card">
      <div class="line"><span>AirBike FTP<small>Scales Z2 &amp; Z5 % FTP targets (182 W baseline)</small></span>${num('ftp', s.ftp, 80, 450)}<em>W</em></div>
      <div class="line"><span>Bodyweight<small>Auto-pulls latest smart-scale weigh-in from Intervals.icu</small></span><input class="num" type="number" inputmode="decimal" step="0.1" id="bwIn" placeholder="195" value="${bwVal}"><em>${unit}</em></div>
      <div class="line"><span>Auto-sync eFTP from Intervals<small>Updates FTP automatically when Intervals models a new eFTP</small></span>${tog('ftpAutoSync', s.ftpAutoSync)}</div>
      <div class="hint" id="ftpInfo">${ftpStatus()}</div>
      <div class="row">
        <button class="btn sm" id="useEftp" ${s.intervalsEftp ? '' : 'disabled'}>Use modeled eFTP</button>
        <button class="btn sm" id="syncFtp">Sync from Intervals</button>
      </div>
    </section>

    <h2 class="sec">GYM DISPLAY &amp; VOICE</h2>
    <section class="card">
      <div class="line"><span>Smart TV Wallboard<small>Streams live 1 Hz HR, rest timer &amp; superset progression over Wi-Fi</small></span><button class="switch ${s.wallboardEnabled ? 'on' : ''}" id="wbTog" role="switch" aria-checked="${s.wallboardEnabled}"><i></i></button></div>
      <div class="hint" id="wbInfo"></div>
      <div class="row">
        <button class="btn sm" id="wbCopy">Copy TV URL</button>
        <a class="btn sm primary" href="#/wallboard">Open Preview &amp; Setup ›</a>
      </div>
      <div class="line" style="margin-top:8px"><span>Hands-Free Voice Commands<small>Active ONLY while Armory Fit is open on screen</small></span><button class="switch ${s.voiceEnabled ? 'on' : ''}" id="vxTog" role="switch" aria-checked="${s.voiceEnabled}"><i></i></button></div>
      <label class="field">Wake word<input id="vxWake" value="${esc(s.voiceWakeWord || 'armory')}" placeholder="armory" autocapitalize="off" autocomplete="off" spellcheck="false"></label>
      <p class="hint">Say: “Armory 225 for 8” · “Armory check” · “Armory next exercise” · “Armory 2B” · “Armory skip rest” · “Armory undo”.</p>
      <div class="hint" id="vxStatus"></div>
      <div class="row" style="margin-top:6px">
        <input class="lf-input" id="vxTest" style="flex:1;padding:8px 10px;font-size:.88rem" placeholder="Test phrase (e.g. armory 225 for 8)" autocomplete="off">
        <button class="btn sm" id="vxRun">Run</button>
      </div>
    </section>

    <h2 class="sec">INTERVALS.ICU</h2>
    <section class="card">
      <label class="field">Athlete ID<input id="aid" value="${s.athleteId}" autocapitalize="off" autocomplete="off" spellcheck="false"></label>
      <label class="field">API key<input id="key" type="password" value="${s.apiKey}" placeholder="Pre-configured" autocomplete="off" spellcheck="false"></label>
      <div class="row"><button class="btn sm" id="test">Test connection</button><span id="testout" class="hint"></span></div>
      <p class="hint">Pre-configured for your account on this device (Intervals → Settings → Developer Settings).</p>
    </section>

    <h2 class="sec">ROLLING PROGRAM · PHASE 1</h2>
    <section class="card">
      <div class="line"><span>Rest day<small>Excluded from projected dates &amp; shown as Rest</small></span><select class="lf-input sel" id="restDay"><option value="0">Sunday</option><option value="1">Monday</option><option value="2">Tuesday</option><option value="3">Wednesday</option><option value="4">Thursday</option><option value="5">Friday</option><option value="6">Saturday</option><option value="none">None — every day</option></select></div>
      <div class="hint" id="planStepInfo"></div>
      <div class="row">
        <a class="btn sm primary" href="#/program">Open Full Program ›</a>
        <button class="btn sm" id="stepPrev">← Prev</button>
        <button class="btn sm" id="stepNext">Next →</button>
        <button class="btn sm" id="stepSync">Sync Activities</button>
      </div>
    </section>

    <h2 class="sec">RECOVERIES · AIRBIKE Z5</h2>
    <section class="card">
      <div class="line"><span>Rest between reps<small>HR-gated: the rest never ends before the protocol's own rest, then stays easy until HR has recovered (capped at 5 min). Fixed timers: plain countdowns.</small></span></div>
      ${seg('recoveryMode', s.recoveryMode, [['gated', 'HR-gated'], ['timed', 'Fixed timers']])}
      <div class="line"><span>Recovered at<small>% of max HR (${recoveryLimitBpm(s.hrMax, s.recoveryHrPct)} bpm now). Falls back to the timer if the HR strap is silent.</small></span>${num('recoveryHrPct', s.recoveryHrPct, 50, 90)}<em>%</em></div>
      <div class="row"><button class="btn sm" id="resetProgs">Reset AirBike program list</button><span id="resetOut" class="hint"></span></div>
      <p class="hint">Tap Reorder on the AirBike screen to pin, move or hide programs.</p>
    </section>

    <h2 class="sec">METRONOME</h2>
    <section class="card">
      <div class="line"><span>When it plays<small>The button on the workout screen turns it on/off</small></span></div>
      ${seg('metroMode', s.metroMode, [['transitions', 'Only on transitions'], ['whole', 'Whole workout']])}
      <div class="line"><span>Start before transition<small>Lead-in at the upcoming tempo</small></span>${num('metroLead', s.metroLead, 0, 15)}<em>sec</em></div>
      <div class="line"><span>Keep going after<small>Transitions mode only</small></span>${num('metroTail', s.metroTail, 0, 60)}<em>sec</em></div>
      <div class="line"><span>Auto-mute when locked in<small>Stop early once cadence holds target ±1.5 RPM for 3 s</small></span>${tog('metroAutoMute', s.metroAutoMute)}</div>
      <div class="line"><span>Volume</span><input id="vol" type="range" min="0.1" max="1" step="0.05" value="${s.metroVolume}"></div>
      <p class="hint">Tempo = 2 × target RPM (one beat per leg stroke), so e.g. 54.5 RPM · 223 W ticks at 109 BPM.</p>
    </section>

    <h2 class="sec">WORKOUT</h2>
    <section class="card">
      <div class="line"><span>Transition chime<small>Sound on every phase change</small></span>${tog('chime', s.chime)}</div>
      <div class="line"><span>Vibration<small>Transitions and overshoot warnings</small></span>${tog('haptics', s.haptics)}</div>
      <div class="line"><span>Cadence bar smoothing<small>Averages the needle and the ON TARGET / PICK IT UP cue. More = steadier but laggier. Big numbers stay at 3 s.</small></span></div>
      ${seg('needleSmooth', s.needleSmooth, [['0', 'Raw'], ['1', '1 s'], ['2', '2 s'], ['3', '3 s']])}
      <div class="line"><span>Max heart rate<small>Drives HR zones &amp; the Z5 bank (≥ 88%)</small></span>${num('hrMax', s.hrMax, 120, 230)}<em>bpm</em></div>
    </section>

    <h2 class="sec">DEVICES</h2>
    <section class="card">
      <div class="row"><button class="btn sm" id="forget">Forget Polar H10 &amp; AirBike</button></div>
      <p class="hint">Remembers paired sensors so the app can reconnect silently. Use this if one stops reconnecting, then tap the chip to pair again.</p>
      <div class="line"><span>Simulator<small>Fake H10 + AirBike to try the app without hardware</small></span>${tog('simulator', s.simulator)}</div>
    </section>

    <h2 class="sec">BACKUP &amp; RESTORE</h2>
    ${backupCardHtml('Full phone backup: Strength, Cardio telemetry, Templates & Schedule.')}
    ${revFooter()}
  </main>`;

  const cleanupChips = wireChips(root);
  const paintFtpBox = () => {
    const cur = getSettings();
    $('#ftpInfo', root).innerHTML = ftpStatus();
    $<HTMLButtonElement>('#useEftp', root).disabled = !cur.intervalsEftp;
    $<HTMLInputElement>('[data-num="ftp"]', root).value = String(cur.ftp);
    const bwEl = root.querySelector<HTMLInputElement>('#bwIn');
    if (bwEl && document.activeElement !== bwEl) {
      bwEl.value = cur.weightKg ? fmtNum(toDisplay(cur.weightKg, liftSettings().unit)) : '';
    }
    $<HTMLSelectElement>('#restDay', root).value = cur.restDay == null ? 'none' : String(cur.restDay);
    const step = PHASE1_PLAN.steps[cur.queue.stepIndex];
    $('#planStepInfo', root).innerHTML = step
      ? `Current: <b>Step ${step.index + 1}/${PHASE1_PLAN.steps.length}</b> · ${esc(step.title)} (local schedule)`
      : '<b>Phase 1 complete!</b>';

    const wbTog = $<HTMLElement>('#wbTog', root);
    wbTog.classList.toggle('on', cur.wallboardEnabled);
    wbTog.setAttribute('aria-checked', String(cur.wallboardEnabled));
    const wbSt = wallboard.status;
    $('#wbInfo', root).innerHTML = wbSt.running
      ? `<b>● LIVE:</b> <code>${esc(wbSt.url)}</code> · ${wbSt.clients} display${wbSt.clients === 1 ? '' : 's'} connected`
      : wbSt.error ? `✗ ${esc(wbSt.error)}` : `Off · will serve on port ${cur.wallboardPort}`;

    const vxTog = $<HTMLElement>('#vxTog', root);
    vxTog.classList.toggle('on', cur.voiceEnabled);
    vxTog.setAttribute('aria-checked', String(cur.voiceEnabled));
    const vxEl = $('#vxStatus', root);
    if (voice.lastError) vxEl.innerHTML = `<span style="color:#fca5a5">${esc(voice.lastError)}</span>`;
    else if (voice.lastAction) vxEl.innerHTML = `Last: <b>${esc(voice.lastAction.text)}</b>${voice.lastHeard ? ` · Heard “${esc(voice.lastHeard)}”` : ''}`;
    else vxEl.textContent = cur.voiceEnabled ? (voice.listening ? `● Listening for “${cur.voiceWakeWord || 'armory'}”` : 'Ready while Armory Fit is open') : 'Off';
  };
  paintFtpBox();
  const offSet = settingsChanged.on(paintFtpBox);
  const offWb = wallboard.changed.on(paintFtpBox);
  const offVx = voice.changed.on(paintFtpBox);
  void wallboard.refreshStatus();
  if (hasKey()) void syncAthleteFtp(false).catch(() => {});

  const onClick = (e: Event) => {
    const t = e.target as HTMLElement;
    const sg = t.closest<HTMLElement>('[data-seg] button');
    if (sg) {
      const name = (sg.parentElement as HTMLElement).dataset.seg!;
      updateSettings({ [name]: sg.dataset.v } as never);
      sg.parentElement!.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === sg));
    }
    const tg = t.closest<HTMLElement>('[data-tog]');
    if (tg) {
      const name = tg.dataset.tog as 'ftpAutoSync' | 'metroAutoMute' | 'chime' | 'haptics' | 'simulator';
      const on = !getSettings()[name];
      updateSettings({ [name]: on });
      tg.classList.toggle('on', on);
      tg.setAttribute('aria-checked', String(on));
      if (name === 'ftpAutoSync' && on) void syncAthleteFtp(true).catch(() => {});
      if (name === 'simulator') {
        if (on) { hrLink.release(); bikeLink.release(); }
        else sim.stop();
        ensureSensors({ bike: false });
      }
    }
    if (t.closest('#stepPrev')) void calendar.setStep(getSettings().queue.stepIndex - 1);
    if (t.closest('#stepNext')) void calendar.setStep(getSettings().queue.stepIndex + 1);
    if (t.closest('#stepSync')) void calendar.refresh();
    if (t.closest('#useEftp')) {
      const ef = getSettings().intervalsEftp;
      if (ef) updateSettings({ ftp: ef });
    }
    if (t.closest('#syncFtp')) void syncFtp();
    if (t.closest('#forget')) void forget();
    if (t.closest('#resetProgs')) {
      updateSettings({ programOrder: [], programHidden: null });
      $('#resetOut', root).textContent = '✓ Back to the default list';
    }
    if (t.closest('#test')) void test();
    if (t.closest('#wbTog')) void wallboard.setEnabled(!getSettings().wallboardEnabled).then(paintFtpBox);
    if (t.closest('#wbCopy')) {
      const u = wallboard.status.url;
      void navigator.clipboard?.writeText(u).then(() => toast(`Copied ${u}`)).catch(() => toast(u));
    }
    if (t.closest('#vxTog')) void voice.setEnabled(!getSettings().voiceEnabled).then(paintFtpBox);
    if (t.closest('#vxRun')) {
      const phrase = $<HTMLInputElement>('#vxTest', root).value.trim();
      if (phrase) {
        const res = voice.runTextCommand(phrase, false);
        toast(res ? res.text : 'Did not match a lift voice command');
        paintFtpBox();
      }
    }
  };
  const onInput = (e: Event) => {
    const t = e.target as HTMLInputElement;
    if (t.id === 'aid') updateSettings({ athleteId: t.value.trim() });
    else if (t.id === 'vxWake') updateSettings({ voiceWakeWord: t.value.trim().toLowerCase() || 'armory' });
    else if (t.id === 'restDay') {
      updateSettings({ restDay: t.value === 'none' ? null : Number(t.value) });
      void calendar.refresh();
    } else if (t.id === 'key') {
      updateSettings({ apiKey: t.value.trim() });
      void calendar.refresh();
    } else if (t.id === 'vol') {
      updateSettings({ metroVolume: Number(t.value) });
      audio.setVolume(Number(t.value));
    } else if (t.id === 'bwIn') {
      const v = Number(t.value);
      if (Number.isFinite(v) && v >= 35 && v <= 450) {
        updateSettings({ weightKg: fromDisplay(v, liftSettings().unit), weightDate: null });
      }
    } else if (t.dataset.num) {
      const v = Number(t.value);
      if (Number.isFinite(v) && v >= Number(t.min) && v <= Number(t.max)) updateSettings({ [t.dataset.num]: v });
    }
  };
  root.addEventListener('click', onClick);
  root.addEventListener('input', onInput);

  async function syncFtp() {
    const infoEl = $('#ftpInfo', root);
    if (!hasKey()) { infoEl.textContent = 'Add an API key first'; return; }
    infoEl.textContent = 'Syncing from Intervals.icu…';
    try {
      await syncAthleteFtp(false);
      paintFtpBox();
    } catch (e) {
      infoEl.textContent = `✗ ${(e as Error).message}`;
    }
  }

  async function test() {
    const out = $('#testout', root);
    if (!hasKey()) { out.textContent = 'Paste an API key first'; return; }
    out.textContent = 'Testing…';
    try { out.textContent = `✓ Connected as ${await testConnection()}`; }
    catch (e) { out.textContent = `✗ ${(e as Error).message}`; }
  }

  async function forget() {
    hrLink.forget();
    bikeLink.forget();
    await transport.forgetAll();
    toast('Sensors forgotten — tap the HR chip to pair again');
  }

  const offBackup = wireBackupCard(root, paintFtpBox);

  return () => {
    offBackup();
    cleanupChips();
    offSet();
    offWb();
    offVx();
    root.removeEventListener('click', onClick);
    root.removeEventListener('input', onInput);
    if (hasKey()) void calendar.refresh();
  };
}
