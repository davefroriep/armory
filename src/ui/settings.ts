import { bikeLink, hrLink } from '../ble/devices';
import { transport } from '../ble/transport';
import { sim } from '../ble/sim';
import { ensureSensors } from '../ble/sources';
import { hasKey, testConnection } from '../intervals/api';
import { calendar } from '../intervals/calendar';
import { audio } from '../audio/audio';
import { getSettings, updateSettings } from '../store/settings';
import { REV, REV_STAMP } from '../rev';
import { topbar, wireChips } from './chrome';
import { $ } from './dom';

export function settingsScreen(root: HTMLElement): () => void {
  const s = getSettings();
  const seg = (name: string, val: string, opts: [string, string][]) =>
    `<div class="seg" data-seg="${name}">${opts.map(([v, l]) => `<button class="${v === val ? 'on' : ''}" data-v="${v}">${l}</button>`).join('')}</div>`;
  const tog = (name: string, on: boolean) => `<button class="switch ${on ? 'on' : ''}" data-tog="${name}" role="switch" aria-checked="${on}"><i></i></button>`;
  const num = (name: string, v: number, min: number, max: number) =>
    `<input class="num" type="number" inputmode="numeric" data-num="${name}" min="${min}" max="${max}" value="${v}">`;

  root.innerHTML = `${topbar({ back: '#/', title: 'Settings' })}
  <main class="page settings">
    <h2 class="sec">INTERVALS.ICU</h2>
    <section class="card">
      <label class="field">Athlete ID<input id="aid" value="${s.athleteId}" autocapitalize="off" autocomplete="off" spellcheck="false"></label>
      <label class="field">API key<input id="key" type="password" value="${s.apiKey}" placeholder="Paste once — stays on this phone" autocomplete="off" spellcheck="false"></label>
      <div class="row"><button class="btn sm" id="test">Test connection</button><span id="testout" class="hint"></span></div>
      <p class="hint">Intervals → Settings → Developer Settings → API Key. Stored only in this browser, never in the app’s code.</p>
    </section>

    <h2 class="sec">METRONOME</h2>
    <section class="card">
      <div class="line"><span>When it plays<small>The ♪ button on the workout screen turns it on/off</small></span></div>
      ${seg('metroMode', s.metroMode, [['transitions', 'Only on transitions'], ['whole', 'Whole workout']])}
      <div class="line"><span>Start before transition<small>Lead-in at the upcoming tempo</small></span>${num('metroLead', s.metroLead, 0, 15)}<em>sec</em></div>
      <div class="line"><span>Keep going after<small>Transitions mode only</small></span>${num('metroTail', s.metroTail, 0, 60)}<em>sec</em></div>
      <div class="line"><span>Auto-mute when locked in<small>Stop early once cadence holds target ±1.5 RPM for 3 s</small></span>${tog('metroAutoMute', s.metroAutoMute)}</div>
      <div class="line"><span>Volume</span><input id="vol" type="range" min="0.1" max="1" step="0.05" value="${s.metroVolume}"></div>
      <p class="hint">Tempo = 2 × target RPM (one beat per leg stroke), so e.g. 55 RPM · 223 W ticks at 109 BPM.</p>
    </section>

    <h2 class="sec">WORKOUT</h2>
    <section class="card">
      <div class="line"><span>Transition chime<small>Sound on every phase change</small></span>${tog('chime', s.chime)}</div>
      <div class="line"><span>Vibration<small>Transitions and overshoot warnings</small></span>${tog('haptics', s.haptics)}</div>
      <div class="line"><span>Max heart rate<small>Drives HR zones &amp; the Z5 bank (≥ 88%)</small></span>${num('hrMax', s.hrMax, 120, 230)}<em>bpm</em></div>
    </section>

    <h2 class="sec">DEVICES</h2>
    <section class="card">
      <div class="row"><button class="btn sm" id="forget">Forget Polar H10 &amp; AirBike</button></div>
      <p class="hint">Chrome remembers a paired sensor so the app can reconnect silently. Use this if one stops reconnecting, then tap the chip to pair again.</p>
      <div class="line"><span>Simulator<small>Fake H10 + AirBike to try the app without hardware</small></span>${tog('simulator', s.simulator)}</div>
    </section>
    <footer class="rev">Armory Fit · Rev ${REV} — ${REV_STAMP}</footer>
  </main>`;

  const cleanupChips = wireChips(root);

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
      const name = tg.dataset.tog as 'metroAutoMute' | 'chime' | 'haptics' | 'simulator';
      const on = !getSettings()[name];
      updateSettings({ [name]: on });
      tg.classList.toggle('on', on);
      tg.setAttribute('aria-checked', String(on));
      if (name === 'simulator') {
        if (on) { hrLink.release(); bikeLink.release(); }
        else sim.stop();
        ensureSensors({ bike: false });
      }
    }
    if (t.closest('#forget')) void forget();
    if (t.closest('#test')) void test();
  };
  const onInput = (e: Event) => {
    const t = e.target as HTMLInputElement;
    if (t.id === 'aid') updateSettings({ athleteId: t.value.trim() });
    else if (t.id === 'key') {
      updateSettings({ apiKey: t.value.trim() });
      void calendar.refresh();
    } else if (t.id === 'vol') {
      updateSettings({ metroVolume: Number(t.value) });
      audio.setVolume(Number(t.value));
    } else if (t.dataset.num) {
      const v = Number(t.value);
      if (Number.isFinite(v) && v >= Number(t.min) && v <= Number(t.max)) updateSettings({ [t.dataset.num]: v });
    }
  };
  root.addEventListener('click', onClick);
  root.addEventListener('input', onInput);

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
    alert('Sensors forgotten. Tap the heart-rate chip on the home screen to pair again.');
  }

  return () => {
    cleanupChips();
    root.removeEventListener('click', onClick);
    root.removeEventListener('input', onInput);
    if (hasKey()) void calendar.refresh();
  };
}
