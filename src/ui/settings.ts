import { bikeLink, hrLink, spinLink } from '../ble/devices';
import { transport } from '../ble/transport';
import { sim } from '../ble/sim';
import { ensureSensors } from '../ble/sources';
import { hasKey, syncAthleteFtp, testConnection } from '../intervals/api';
import { calendar } from '../intervals/calendar';
import { audio } from '../audio/audio';
import { recoveryLimitBpm } from '../domain/gate';
import { PHASE1_PLAN } from '../domain/plan';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { liftSettings, toDisplay, fmtNum, fromDisplay } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { gymCam, gymCamConfigLinkState } from '../lifts/gymCam';
import { voice } from '../lifts/voice';
import { VOICE_ALIAS_SLOTS, VOICE_COMMAND_GROUPS, type VoiceAliasSlot } from '../lifts/voiceParser';
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
      <div class="line"><span>Cycling &amp; AirBike FTP<small>Scales AirBike &amp; Spin Z2/Z5 % FTP targets (182 W baseline)</small></span>${num('ftp', s.ftp, 80, 450)}<em>W</em></div>
      <div class="line"><span>Bodyweight<small>Auto-pulls latest smart-scale weigh-in from Intervals.icu</small></span><input class="num" type="number" inputmode="decimal" step="0.1" id="bwIn" placeholder="195" value="${bwVal}"><em>${unit}</em></div>
      <div class="line"><span>Auto-sync eFTP from Intervals<small>Updates FTP automatically when Intervals models a new eFTP</small></span>${tog('ftpAutoSync', s.ftpAutoSync)}</div>
      <div class="hint" id="ftpInfo">${ftpStatus()}</div>
      <div class="row">
        <button class="btn sm" id="useEftp" ${s.intervalsEftp ? '' : 'disabled'}>Use modeled eFTP</button>
        <button class="btn sm" id="syncFtp">Sync from Intervals</button>
      </div>
    </section>

    <h2 class="sec">GYM DISPLAY &amp; CAMERAS</h2>
    <section class="card">
      <div class="line"><span>Auto-Wake 32&quot; TV on Workout<small>Streams wallboard via OptiPlex &amp; wakes Fire TV over ADB when a workout starts</small></span>${tog('wallboardRelayEnabled', s.wallboardRelayEnabled)}</div>
      <div class="line"><span>Smart TV Wallboard (Pixel Direct)<small>Optional local HTTP/SSE server hosted directly on this Pixel</small></span><button class="switch ${s.wallboardEnabled ? 'on' : ''}" id="wbTog" role="switch" aria-checked="${s.wallboardEnabled}"><i></i></button></div>
      <div class="hint" id="wbInfo"></div>
      <div class="row">
        <button class="btn sm" id="wbCopy">Copy TV URL</button>
        <a class="btn sm primary" href="#/wallboard">Open Preview &amp; Setup ›</a>
      </div>
      <div class="line" style="margin-top:10px"><span>Gym Camera DVR<small>Probes OptiPlex silently on home Wi-Fi; stays hidden away</small></span>${tog('gymCamEnabled', s.gymCamEnabled)}</div>
      <div class="line"><span>Auto-clip PR sets (★)<small>Automatically clips new 1RM–40RM personal records</small></span>${tog('gymCamAutoPr', s.gymCamAutoPr)}</div>
      <div class="line"><span>Lifting With a Buddy<small>Locks Auto camera fallbacks to your exercise default (or Rack) across workouts</small></span>${tog('gymCamBuddyMode', s.gymCamBuddyMode)}</div>
      <div class="field">
        <label for="camUrl">Gym Camera DVR URL</label>
        <div class="cam-url-row">
          <input id="camUrl" value="${esc(s.gymCamUrl || 'http://192.168.1.152:8766')}" placeholder="http://192.168.1.152:8766" autocapitalize="off" autocomplete="off" spellcheck="false">
          <a class="btn sm cam-url-btn" id="camCfgBtn" href="${esc(s.gymCamUrl || 'http://192.168.1.152:8766')}" target="_blank" rel="noopener">Config ↗</a>
        </div>
      </div>
      <a class="cam-preview-link" id="camPreviewBanner" href="${esc(gymCam.previewDriveUrl())}" target="_blank" rel="noopener" hidden>
        <span class="cam-prev-txt"><b>Realtime adjustments not available</b> — Preview ↗</span>
        <small id="camPreviewSub"></small>
      </a>
      <div class="row" style="margin-top:2px"><button class="btn sm" id="camTest">Test Cam</button><span id="camOut" class="hint"></span></div>
    </section>

    <h2 class="sec">VOICE COMMANDS</h2>
    <section class="card">
      <div class="line"><span>Room Mic (OptiPlex PowerConf S330)<small>Default on Armory Wi-Fi · listens on OptiPlex during active Strength workouts</small></span>${tog('voiceRoomEnabled', s.voiceRoomEnabled)}</div>
      <div class="line"><span>Phone Mic (Pixel On-Device)<small>Off by default · optional fallback when away from home Wi-Fi</small></span><button class="switch ${s.voiceEnabled ? 'on' : ''}" id="vxTog" role="switch" aria-checked="${s.voiceEnabled}"><i></i></button></div>
      <div class="line"><span>Spoken Voice Readback<small>Plays confirmations on PowerConf S330 speaker (or Pixel when away)</small></span>${tog('voiceReadback', s.voiceReadback)}</div>
      <label class="field">Wake word<input id="vxWake" value="${esc(s.voiceWakeWord || 'armory')}" placeholder="armory" autocapitalize="off" autocomplete="off" spellcheck="false"></label>
      <div class="hint" id="vxStatus"></div>
      <div class="row" style="margin-top:4px">
        <input class="lf-input" id="vxTest" style="flex:1;padding:8px 10px;font-size:.88rem" placeholder="Test phrase (e.g. armory by 10)" autocomplete="off">
        <button class="btn sm" id="vxRun">Run</button>
      </div>
      <div class="fl" style="margin-top:6px">MY CUSTOM PHRASES</div>
      <label class="field">When I say a phrase for<select class="lf-input sel" id="vxAliasSlot">${VOICE_ALIAS_SLOTS.map((sl) => `<option value="${sl.slot}">${esc(sl.label)}</option>`).join('')}</select></label>
      <div class="row">
        <input class="lf-input" id="vxAliasPhrase" style="flex:1;padding:8px 10px;font-size:.88rem" placeholder="${esc(VOICE_ALIAS_SLOTS[0].hint)}" autocapitalize="off" autocomplete="off">
        <button class="btn sm primary" id="vxAliasAdd">Add</button>
      </div>
      <div class="vx-chips" id="vxAliasList"></div>
      <details class="vx-ref">
        <summary>All Voice Commands &amp; Variations (tap any to test)</summary>
        <div id="vxCatalog"></div>
      </details>
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
      <div class="row"><button class="btn sm" id="forget">Forget Polar H10, AirBike &amp; Spin</button></div>
      <p class="hint">Remembers paired sensors so the app can reconnect silently. Use this if one stops reconnecting, then tap the chip to pair again.</p>
      <div class="line"><span>Simulator<small>Fake H10 + AirBike to try the app without hardware</small></span>${tog('simulator', s.simulator)}</div>
    </section>

    <h2 class="sec">BACKUP &amp; RESTORE</h2>
    ${backupCardHtml('Full phone backup: Strength, Cardio telemetry, Templates & Schedule.')}
    ${revFooter()}
  </main>`;

  const cleanupChips = wireChips(root);
  let dead = false;
  let camProbing = false;
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
    const relayTog = root.querySelector<HTMLElement>('[data-tog="wallboardRelayEnabled"]');
    if (relayTog) {
      relayTog.classList.toggle('on', cur.wallboardRelayEnabled);
      relayTog.setAttribute('aria-checked', String(cur.wallboardRelayEnabled));
    }
    const wbSt = wallboard.status;
    const localTxt = wbSt.running
      ? `<b>● Pixel LIVE:</b> <code>${esc(wbSt.url)}</code> · ${wbSt.clients} display${wbSt.clients === 1 ? '' : 's'}`
      : wbSt.error ? `✗ ${esc(wbSt.error)}` : `Pixel server off (port ${cur.wallboardPort})`;
    $('#wbInfo', root).innerHTML = cur.wallboardRelayEnabled
      ? `<b>TV URL (OptiPlex):</b> <code>${esc(wallboard.relayUrl())}</code> · ${localTxt}`
      : localTxt;

    const vxRoomTog = root.querySelector<HTMLElement>('[data-tog="voiceRoomEnabled"]');
    if (vxRoomTog) {
      vxRoomTog.classList.toggle('on', cur.voiceRoomEnabled);
      vxRoomTog.setAttribute('aria-checked', String(cur.voiceRoomEnabled));
    }
    const vxTog = $<HTMLElement>('#vxTog', root);
    vxTog.classList.toggle('on', cur.voiceEnabled);
    vxTog.setAttribute('aria-checked', String(cur.voiceEnabled));
    const vxRb = root.querySelector<HTMLElement>('[data-tog="voiceReadback"]');
    if (vxRb) {
      vxRb.classList.toggle('on', cur.voiceReadback);
      vxRb.setAttribute('aria-checked', String(cur.voiceReadback));
    }
    const vxEl = $('#vxStatus', root);
    const roomPart = !cur.voiceRoomEnabled
      ? 'Room Mic off'
      : voice.roomListening
        ? `● Room Mic listening (${esc(voice.roomDevice || 'PowerConf S330')})`
        : voice.roomOnline
          ? `✓ Room Mic ready (${esc(voice.roomDevice || 'PowerConf S330')})`
          : 'Room Mic offline (away from Armory Wi-Fi)';
    const phonePart = cur.voiceEnabled
      ? (voice.listening ? '● Pixel mic listening' : 'Pixel mic ready on workout')
      : 'Pixel mic off';
    if (voice.lastError) vxEl.innerHTML = `${roomPart} · <span style="color:#fca5a5">${esc(voice.lastError)}</span>`;
    else if (voice.lastAction) vxEl.innerHTML = `${roomPart} · Last: <b>${esc(voice.lastAction.text)}</b>${voice.lastHeard ? ` · Heard “${esc(voice.lastHeard)}”` : ''}`;
    else vxEl.innerHTML = `${roomPart} · ${phonePart}${voice.lastHeard ? ` · Heard “${esc(voice.lastHeard)}”` : ''}`;

    const aliasListEl = root.querySelector<HTMLElement>('#vxAliasList');
    const aliases = cur.voiceCustomAliases ?? {};
    if (aliasListEl) {
      const slotLabel = new Map(VOICE_ALIAS_SLOTS.map((sl) => [sl.slot, sl.label]));
      const chips: string[] = [];
      for (const sl of VOICE_ALIAS_SLOTS) {
        for (const p of aliases[sl.slot] ?? []) {
          chips.push(`<button class="vx-chip" type="button" data-rm-slot="${sl.slot}" data-rm-phrase="${esc(p)}"><b>“${esc(p)}”</b><span>→ ${esc(slotLabel.get(sl.slot) ?? sl.slot)}</span><i>✕</i></button>`);
        }
      }
      aliasListEl.innerHTML = chips.length
        ? chips.join('')
        : '<span class="hint">No custom phrases yet — add any phrase you naturally say above.</span>';
    }

    const catEl = root.querySelector<HTMLElement>('#vxCatalog');
    if (catEl) {
      const wake = (cur.voiceWakeWord || 'armory').trim() || 'armory';
      catEl.innerHTML = VOICE_COMMAND_GROUPS.map((g) => {
        const extra = g.slot ? (aliases[g.slot] ?? []) : [];
        return `<div class="vx-grp">
          <b>${esc(g.title)}</b>
          ${g.note ? `<small>${esc(g.note)}</small>` : ''}
          <div class="vx-exs">
            ${g.examples.map((ex) => {
              const exShown = `${wake} ${ex}`;
              return `<button class="vx-ex" type="button" data-vxex="${esc(exShown)}">${esc(exShown)}</button>`;
            }).join('')}
            ${extra.map((ex) => {
              const exShown = `${wake} ${ex.replace(/#/g, '10')}`;
              return `<button class="vx-ex custom" type="button" data-vxex="${esc(exShown)}">★ ${esc(exShown)}</button>`;
            }).join('')}
          </div>
        </div>`;
      }).join('');
    }

    const camIn = root.querySelector<HTMLInputElement>('#camUrl');
    if (camIn && document.activeElement !== camIn) {
      camIn.value = cur.gymCamUrl || 'http://192.168.1.152:8766';
    }
    const camLink = gymCamConfigLinkState({
      online: gymCam.online,
      probing: camProbing,
      enabled: cur.gymCamEnabled,
      baseUrl: cur.gymCamUrl || 'http://192.168.1.152:8766',
      previewUrl: cur.gymCamPreviewUrl || '',
      previewUpdatedMs: cur.gymCamPreviewUpdatedMs ?? null,
    });
    const camBtn = root.querySelector<HTMLAnchorElement>('#camCfgBtn');
    if (camBtn) {
      camBtn.href = camLink.href;
      camBtn.textContent = camLink.btnLabel;
      camBtn.classList.toggle('primary', camLink.btnPrimary);
    }
    const prevBanner = root.querySelector<HTMLAnchorElement>('#camPreviewBanner');
    if (prevBanner) {
      prevBanner.href = camLink.previewHref;
      prevBanner.hidden = !camLink.showOfflineBanner;
    }
    const prevSub = root.querySelector<HTMLElement>('#camPreviewSub');
    if (prevSub) prevSub.textContent = camLink.bannerSub;
    const buddyTg = root.querySelector<HTMLElement>('[data-tog="gymCamBuddyMode"]');
    if (buddyTg) {
      buddyTg.classList.toggle('on', !!cur.gymCamBuddyMode);
      buddyTg.setAttribute('aria-checked', String(!!cur.gymCamBuddyMode));
    }
  };
  const runCamProbe = async (showBusyText: boolean): Promise<void> => {
    const out = root.querySelector<HTMLElement>('#camOut');
    if (showBusyText && out) out.textContent = 'Probing…';
    camProbing = getSettings().gymCamEnabled;
    paintFtpBox();
    const r = await gymCam.probe(1500, true);
    if (dead) return;
    camProbing = false;
    paintFtpBox();
    if (showBusyText && out) {
      out.textContent = r.online
        ? `✓ Connected (${r.latencyMs} ms${r.cameras.length ? ` · ${r.cameras.join(', ')}` : ''})`
        : 'Offline (normal when away from Armory Wi-Fi)';
    }
  };
  void runCamProbe(false);
  const offSet = settingsChanged.on(paintFtpBox);
  const offWb = wallboard.changed.on(paintFtpBox);
  const offVx = voice.changed.on(paintFtpBox);
  const offCam = gymCam.changed.on(paintFtpBox);
  void wallboard.refreshStatus();
  void voice.refreshRoomStatus();
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
      const name = tg.dataset.tog as 'ftpAutoSync' | 'metroAutoMute' | 'chime' | 'haptics' | 'simulator' | 'gymCamEnabled' | 'gymCamAutoPr' | 'gymCamBuddyMode' | 'wallboardRelayEnabled' | 'voiceRoomEnabled' | 'voiceReadback';
      const on = !getSettings()[name];
      updateSettings({ [name]: on });
      tg.classList.toggle('on', on);
      tg.setAttribute('aria-checked', String(on));
      if (name === 'ftpAutoSync' && on) void syncAthleteFtp(true).catch(() => {});
      if (name === 'gymCamEnabled') void runCamProbe(false);
      if (name === 'voiceRoomEnabled') void voice.refreshRoomStatus();
      if (name === 'gymCamBuddyMode' && lifts.active) {
        lifts.active.buddyMode = on;
        lifts.saveSession(lifts.active, true);
      }
      if (name === 'simulator') {
        if (on) { hrLink.release(); bikeLink.release(); spinLink.release(); }
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
      updateSettings({ programOrder: [], programHidden: null, spinProgramOrder: [], spinProgramHidden: null });
      $('#resetOut', root).textContent = '✓ Back to the default list';
    }
    if (t.closest('#test')) void test();
    if (t.closest('#wbTog')) void wallboard.setEnabled(!getSettings().wallboardEnabled).then(paintFtpBox);
    if (t.closest('#wbCopy')) {
      const u = getSettings().wallboardRelayEnabled ? wallboard.relayUrl() : wallboard.status.url;
      void navigator.clipboard?.writeText(u).then(() => toast(`Copied ${u}`)).catch(() => toast(u));
    }
    if (t.closest('#vxTog')) void voice.setEnabled(!getSettings().voiceEnabled).then(paintFtpBox);
    if (t.closest('#vxRun')) {
      const phrase = $<HTMLInputElement>('#vxTest', root).value.trim();
      if (phrase) {
        const res = voice.testPhrase(phrase);
        toast(res.text);
        paintFtpBox();
      }
    }
    if (t.closest('#vxAliasAdd')) {
      const slot = $<HTMLSelectElement>('#vxAliasSlot', root).value as VoiceAliasSlot;
      const inp = $<HTMLInputElement>('#vxAliasPhrase', root);
      const phrase = inp.value.trim().toLowerCase();
      if (!phrase) { toast('Enter a phrase to add'); return; }
      const curAliases = { ...(getSettings().voiceCustomAliases ?? {}) };
      const list = [...(curAliases[slot] ?? [])];
      if (!list.some((x) => x.toLowerCase() === phrase)) {
        list.push(phrase);
        curAliases[slot] = list;
        updateSettings({ voiceCustomAliases: curAliases });
        voice.refreshBiasPhrases();
        toast(`Added “${phrase}”`);
      }
      inp.value = '';
      paintFtpBox();
    }
    const rmChip = t.closest<HTMLElement>('[data-rm-slot]');
    if (rmChip) {
      const slot = rmChip.dataset.rmSlot as VoiceAliasSlot;
      const phrase = rmChip.dataset.rmPhrase ?? '';
      const curAliases = { ...(getSettings().voiceCustomAliases ?? {}) };
      const nextList = (curAliases[slot] ?? []).filter((x) => x !== phrase);
      if (nextList.length) curAliases[slot] = nextList;
      else delete curAliases[slot];
      updateSettings({ voiceCustomAliases: curAliases });
      voice.refreshBiasPhrases();
      toast(`Removed “${phrase}”`);
      paintFtpBox();
    }
    const exBtn = t.closest<HTMLElement>('[data-vxex]');
    if (exBtn) {
      const phrase = exBtn.dataset.vxex ?? '';
      if (phrase) {
        const inp = $<HTMLInputElement>('#vxTest', root);
        inp.value = phrase;
        const res = voice.testPhrase(phrase);
        toast(res.text);
        paintFtpBox();
      }
    }
    if (t.closest('#camTest')) {
      void runCamProbe(true);
    }
  };
  const onInput = (e: Event) => {
    const t = e.target as HTMLInputElement;
    if (t.id === 'aid') updateSettings({ athleteId: t.value.trim() });
    else if (t.id === 'vxWake') updateSettings({ voiceWakeWord: t.value.trim().toLowerCase() || 'armory' });
    else if (t.id === 'vxAliasSlot') {
      const slotMeta = VOICE_ALIAS_SLOTS.find((sl) => sl.slot === t.value);
      const pInp = root.querySelector<HTMLInputElement>('#vxAliasPhrase');
      if (slotMeta && pInp) pInp.placeholder = slotMeta.hint;
    }
    else if (t.id === 'camUrl') updateSettings({ gymCamUrl: t.value.trim().replace(/\/+$/, '') || 'http://192.168.1.152:8766' });
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
    spinLink.forget();
    await transport.forgetAll();
    toast('Sensors forgotten — tap the HR or Bike chip to pair again');
  }

  const offBackup = wireBackupCard(root, paintFtpBox);

  return () => {
    dead = true;
    offBackup();
    cleanupChips();
    offSet();
    offWb();
    offVx();
    offCam();
    root.removeEventListener('click', onClick);
    root.removeEventListener('input', onInput);
    if (hasKey()) void calendar.refresh();
  };
}
