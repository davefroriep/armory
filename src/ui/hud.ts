import { bikeFrame, bikeFresh, hrFresh, live } from '../ble/telemetry';
import { sim } from '../ble/sim';
import { ensureSensors, releaseBike } from '../ble/sources';
import { audio, buzz } from '../audio/audio';
import { WorkoutEngine } from '../domain/engine';
import { CORRIDOR_RPM, cueFor, pacerDecision, type Cue, type PacerConfig } from '../domain/pacer';
import { PHASE_COLOR, fmtDur, programById, targetWatts, totalSeconds, workSeconds, type Program, type Segment } from '../domain/programs';
import { wattsForRpm } from '../domain/airbikeCurve';
import { calendar } from '../intervals/calendar';
import { getSettings, updateSettings } from '../store/settings';
import { putMeta } from '../store/db';
import { Recorder } from '../workout/recorder';
import { discard, syncWorkout } from '../workout/sync';
import { ICON, countdown, esc, hms, mmss } from './dom';
import { topbar, wireChips } from './chrome';

const RPE_LABEL = ['', 'Very easy', 'Very easy', 'Easy', 'Easy', 'Moderate', 'Moderate', 'Hard', 'Hard', 'Very hard', 'Max effort'];

const hrZone = (hr: number, max: number): { z: number; pct: number } => {
  const pct = hr / max;
  return { pct, z: pct < 0.6 ? 1 : pct < 0.7 ? 2 : pct < 0.8 ? 3 : pct < 0.88 ? 4 : 5 };
};
const ZONE_COLOR = ['', '#8a94a3', '#38bdf8', '#22c55e', '#f59e0b', '#ff4d2e'];

export function hudScreen(root: HTMLElement, args: string[], query: URLSearchParams): () => void {
  const program = programById(args[0]);
  if (!program) {
    location.hash = '#/airbike';
    return () => {};
  }
  return new Hud(root, program, query.get('ev') ? Number(query.get('ev')) : null).mount();
}

class Hud {
  private engine: WorkoutEngine;
  private rec: Recorder | null = null;
  private goal: number;
  private timer: ReturnType<typeof setInterval> | null = null;
  private offFrame: (() => void) | null = null;
  private offChips: (() => void) | null = null;
  private lock: WakeLockSentinel | null = null;
  private lastIndex = -2;
  private lockSince: number | null = null;
  private lastChirp = 0;
  private completeShown = false;
  private view: 'hud' | 'summary' = 'hud';
  private el!: Record<string, HTMLElement>;

  constructor(private root: HTMLElement, private p: Program, private eventId: number | null) {
    this.engine = new WorkoutEngine(p);
    this.goal = p.goal?.def ?? 0;
  }

  // ------------------------------------------------------------------ mount / teardown

  mount(): () => void {
    ensureSensors({ bike: true });
    this.paintHud();
    this.timer = setInterval(() => this.loop(), 100);
    const vis = () => { if (!document.hidden && this.active) void this.acquireLock(); };
    const unload = (e: BeforeUnloadEvent) => { if (this.active) e.preventDefault(); };
    document.addEventListener('visibilitychange', vis);
    window.addEventListener('beforeunload', unload);
    return () => {
      if (this.timer) clearInterval(this.timer);
      this.offFrame?.();
      this.offChips?.();
      audio.setMetronome(false, null);
      sim.setTarget(0);
      this.releaseLock();
      releaseBike();
      document.removeEventListener('visibilitychange', vis);
      window.removeEventListener('beforeunload', unload);
    };
  }

  private get active(): boolean {
    return this.engine.state === 'running' || this.engine.state === 'paused';
  }

  private async acquireLock(): Promise<void> {
    try { this.lock = (await navigator.wakeLock?.request('screen')) ?? null; } catch { /* denied */ }
  }
  private releaseLock(): void {
    void this.lock?.release().catch(() => {});
    this.lock = null;
  }

  // ------------------------------------------------------------------ HUD markup

  private paintHud(): void {
    const p = this.p;
    const segs = p.segments;
    const total = totalSeconds(p);
    const tl = segs.length
      ? `<div class="tl" id="tl">${segs.map((g, i) =>
          `<span class="tl-seg" data-i="${i}" style="flex:${g.seconds} 1 0;background:${PHASE_COLOR[g.kind]}"></span>`).join('')}<span class="tl-head" id="tlHead"></span></div>`
      : `<div class="tl plain" id="tl"><span class="tl-fill" id="tlFill"></span></div>`;
    const overview = segs.length
      ? `${fmtDur(total)} total${workSeconds(p) ? ` · ${fmtDur(workSeconds(p))} work` : ''}`
      : p.goal ? '' : 'No program';
    const goalCtl = p.goal
      ? `<div class="goal" id="goalCtl"><button data-goal="-1" aria-label="Less">−</button><div><b id="goalVal">${this.goal}</b> <small>${p.goal.unit}</small></div><button data-goal="1" aria-label="More">+</button></div>`
      : '';

    this.root.innerHTML = `<div class="hud">
      ${topbar({ back: '#/airbike', title: p.name, bike: true })}
      <div class="hud-top">
        <div class="overview"><span id="ovText">${overview}</span><span id="ovRight"></span></div>
        ${tl}
      </div>
      <div class="banner" id="banner">
        <span class="ph" id="ph">READY</span><span class="tag" id="tag"></span><span class="tgt" id="tgt"></span>
      </div>
      <div class="big">
        <small id="cdLabel">${segs.length ? 'NEXT TRANSITION' : p.goal ? (p.goal.type === 'time' ? 'TIME LEFT' : 'CAL TO GO') : 'ELAPSED'}</small>
        <div class="cd" id="cd">${segs.length ? mmss(segs[0].seconds) : p.goal?.type === 'time' ? mmss(this.goal * 60) : p.goal ? String(this.goal) : '0:00'}</div>
        <div class="next" id="next">${segs.length ? `FIRST UP: ${esc(segs[0].label)}` : '&nbsp;'}</div>
      </div>
      ${goalCtl}
      <div class="cue" id="cue">&nbsp;</div>
      <div class="gauge" id="gauge"><span class="g-zone" id="gZone"></span><span class="g-mid"></span><span class="g-needle" id="gNeedle"></span></div>
      <div class="tiles3">
        <div class="metric" id="mHr"><small>HEART RATE</small><b id="hr">--</b><em id="hrSub">bpm</em></div>
        <div class="metric" id="mW"><small>POWER</small><b id="w">0</b><em id="wSub">watts</em></div>
        <div class="metric" id="mCad"><small>CADENCE</small><b id="cad">0</b><em id="cadSub">rpm</em></div>
      </div>
      <div class="strip">
        <div><small>ELAPSED</small><b id="sEl">0:00</b></div>
        <div><small>${segs.length ? 'LEFT' : 'AVG W'}</small><b id="sLeft">${segs.length ? mmss(total) : '0'}</b></div>
        <div><small>AVG W</small><b id="sAvg">0</b></div>
        <div><small>KCAL</small><b id="sCal">0</b></div>
        <div><small>${p.zone === 'Z5' ? 'Z5 BANK' : 'DIST MI'}</small><b id="sX">${p.zone === 'Z5' ? '0:00' : '0.00'}</b></div>
      </div>
      <div class="ctrl">
        <button class="metro" id="metro">${ICON.note}<span id="metroTxt">METRONOME</span></button>
        <button class="go" id="go">START</button>
      </div>
      <div class="rt-note" id="sigNote"></div>
    </div>
    <div class="modal" id="pauseModal" hidden>
      <div class="sheet"><h2>Paused</h2><p id="pauseSub"></p>
        <button class="btn primary big" id="resume">Resume</button>
        <button class="btn big" id="finish">Finish workout</button></div></div>
    <div class="modal" id="doneModal" hidden>
      <div class="sheet"><h2>Workout complete 🎉</h2><p>Nice work. Finish to rate it and save.</p>
        <button class="btn primary big" id="doneFinish">Finish &amp; rate</button>
        <button class="btn big" id="doneKeep">Keep riding</button></div></div>`;

    const q = (id: string) => this.root.querySelector<HTMLElement>('#' + id)!;
    this.el = {} as Record<string, HTMLElement>;
    for (const id of ['banner', 'ph', 'tag', 'tgt', 'cdLabel', 'cd', 'next', 'cue', 'gauge', 'gZone', 'gNeedle', 'mHr', 'mW', 'mCad',
      'hr', 'hrSub', 'w', 'wSub', 'cad', 'cadSub', 'sEl', 'sLeft', 'sAvg', 'sCal', 'sX', 'metro', 'metroTxt', 'go', 'pauseModal',
      'pauseSub', 'doneModal', 'ovRight', 'tl', 'sigNote']) this.el[id] = q(id);
    if (segs.length) this.el.tlHead = q('tlHead');
    else this.el.tlFill = q('tlFill');

    this.offChips?.();
    this.offChips = wireChips(this.root);
    this.bind();
  }

  private bind(): void {
    const on = (id: string, fn: () => void) => this.root.querySelector('#' + id)!.addEventListener('click', fn);
    on('go', () => (this.engine.state === 'ready' ? this.start() : this.pause()));
    on('resume', () => this.resume());
    on('finish', () => this.finish());
    on('doneFinish', () => this.finish());
    on('doneKeep', () => { this.el.doneModal.hidden = true; this.engine.resume(Date.now()); });
    on('metro', () => {
      audio.unlock();
      updateSettings({ metroEnabled: !getSettings().metroEnabled });
    });
    this.root.querySelectorAll<HTMLElement>('[data-goal]').forEach((b) =>
      b.addEventListener('click', () => {
        if (this.engine.state !== 'ready' || !this.p.goal) return;
        const g = this.p.goal;
        this.goal = Math.min(g.max, Math.max(g.min, this.goal + Number(b.dataset.goal) * g.step));
        this.root.querySelector('#goalVal')!.textContent = String(this.goal);
      }));
    // Back arrow mid-workout opens the pause sheet instead of dropping the session.
    this.root.querySelector<HTMLAnchorElement>('.bar a.icon-btn')?.addEventListener('click', (e) => {
      if (this.active) { e.preventDefault(); this.pause(); }
    });
  }

  // ------------------------------------------------------------------ actions

  private start(): void {
    audio.unlock();
    audio.setVolume(getSettings().metroVolume);
    const s = getSettings();
    const ev = this.eventId == null ? undefined : calendar.items.find((i) => i.ev.id === this.eventId);
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    this.rec = new Recorder(id, 'airbike', this.p.id, this.p.intervalsName, ev?.ev.id ?? null, s.hrMax);
    this.offFrame = bikeFrame.on(() => { if (this.engine.state === 'running') this.rec?.onFrame(); });
    this.engine.start(Date.now());
    this.lastIndex = -2;
    void this.acquireLock();
    this.el.go.textContent = 'PAUSE';
    this.el.go.classList.add('pause');
    this.root.querySelector('#goalCtl')?.setAttribute('hidden', '');
  }

  private pause(): void {
    if (this.engine.state !== 'running') return;
    this.engine.pause(Date.now());
    audio.setMetronome(false, null);
    this.el.pauseSub.textContent = `${hms(this.engine.elapsed(Date.now()))} elapsed`;
    this.el.pauseModal.hidden = false;
  }

  private resume(): void {
    this.el.pauseModal.hidden = true;
    this.engine.resume(Date.now());
  }

  private finish(): void {
    const now = Date.now();
    this.engine.finish(now);
    if (this.rec) this.rec.tick(this.engine.elapsed(now), 0);
    audio.setMetronome(false, null);
    sim.setTarget(0);
    this.el.pauseModal.hidden = true;
    this.el.doneModal.hidden = true;
    this.releaseLock();
    this.showSummary();
  }

  // ------------------------------------------------------------------ main loop (10 Hz)

  private loop(): void {
    if (this.view !== 'hud') return;
    const now = Date.now();
    const st = getSettings();
    const running = this.engine.state === 'running';
    const elapsed = this.engine.elapsed(now);
    const loc = this.engine.locate(now);
    const seg = loc.seg;
    const targetRpm = seg?.rpm ?? (this.p.segments.length === 0 ? this.p.paceRpm ?? 0 : 0);

    sim.setTarget(running ? targetRpm : 0);
    if (running) this.rec?.tick(elapsed, targetRpm);

    // transitions
    if (running && loc.index !== this.lastIndex) {
      if (this.lastIndex >= 0 && this.p.segments.length) {
        if (st.chime) audio.chime();
        if (st.haptics) buzz([200, 80, 200]);
      }
      this.lastIndex = loc.index;
      this.lockSince = null;
    }

    // lock-in tracking (cadence within corridor for 3 s)
    const cad = bikeFresh() ? live.cad : 0;
    if (running && seg?.rpm != null && !seg.allOut && Math.abs(cad - seg.rpm) <= CORRIDOR_RPM) this.lockSince ??= now;
    else this.lockSince = null;
    const lockedIn = this.lockSince != null && now - this.lockSince >= 3000;

    // metronome
    const cfg: PacerConfig = { mode: st.metroMode, lead: st.metroLead, tail: st.metroTail, autoMute: st.metroAutoMute };
    const dec = pacerDecision(this.p, loc, cfg, st.metroEnabled, lockedIn);
    audio.setMetronome(running && dec.play, dec.bpm);

    // cue + overshoot chirp
    const cue = running ? cueFor(seg, cad, loc.into) : { level: 'idle', text: '', chirp: false } as Cue;
    if (cue.chirp && now - this.lastChirp > 6000) {
      this.lastChirp = now;
      audio.chirp();
      if (st.haptics) buzz([120, 60, 120]);
    }

    // goal / completion
    if (running && !this.completeShown) {
      const done = (this.p.segments.length && loc.complete)
        || (this.p.goal?.type === 'time' && elapsed >= this.goal * 60)
        || (this.p.goal?.type === 'cal' && (this.rec?.lastKcal ?? 0) >= this.goal);
      if (done) {
        this.completeShown = true;
        this.engine.pause(now);
        audio.setMetronome(false, null);
        audio.end();
        if (st.haptics) buzz([300, 100, 300, 100, 300]);
        this.el.doneModal.hidden = false;
      }
    }

    this.paint(loc.seg, loc.next, loc.remaining, loc.index, elapsed, cue, dec.bpm, dec.play, running);
  }

  // ------------------------------------------------------------------ painting

  private paint(seg: Segment | null, next: Segment | null, remaining: number, index: number, elapsed: number,
    cue: Cue, bpm: number | null, playing: boolean, running: boolean): void {
    const e = this.el;
    const p = this.p;
    const st = getSettings();
    const ready = this.engine.state === 'ready';
    const hr = hrFresh() ? live.hr : 0;
    const bike = bikeFresh();
    const w = bike ? live.w : 0;
    const cad = bike ? live.cad : 0;

    // banner
    const shown = ready ? p.segments[0] ?? null : seg;
    const color = shown ? PHASE_COLOR[shown.kind] : '#1e9bff';
    e.banner.style.setProperty('--ph', color);
    e.ph.textContent = ready ? 'READY' : this.engine.state === 'paused' ? 'PAUSED' : shown?.label ?? (p.goal ? 'GOAL' : 'FREE RIDE');
    e.tag.textContent = shown?.tag ?? '';
    e.tgt.textContent = shown ? segTarget(shown) : p.goal ? 'steady effort' : '';

    // countdown
    if (p.segments.length) {
      e.cdLabel.textContent = next ? 'NEXT TRANSITION' : 'WORKOUT ENDS IN';
      e.cd.textContent = ready ? mmss(p.segments[0].seconds) : countdown(remaining);
      e.cd.classList.toggle('hot', !ready && remaining <= 5 && running);
      e.next.innerHTML = ready
        ? `FIRST UP: ${esc(p.segments[0].label)}`
        : next ? `NEXT: <b>${esc(next.label)}</b>${next.rpm != null ? ` · ${next.rpm}${next.allOut ? '+' : ''} RPM${next.allOut ? '' : ` · ${targetWatts(next)} W`}` : ' · full rest'}` : 'LAST BLOCK';
    } else if (p.goal?.type === 'time') {
      e.cd.textContent = mmss(Math.max(0, this.goal * 60 - elapsed));
    } else if (p.goal?.type === 'cal') {
      e.cd.textContent = Math.max(0, this.goal - (this.rec?.lastKcal ?? 0)).toFixed(1);
    } else {
      e.cd.textContent = mmss(elapsed);
    }

    // timeline
    if (p.segments.length) {
      const total = totalSeconds(p);
      (e.tlHead as HTMLElement).style.left = `${Math.min(100, (elapsed / total) * 100)}%`;
      if (index !== this._tlIndex || ready !== this._tlReady) {
        this._tlIndex = index;
        this._tlReady = ready;
        e.tl.querySelectorAll<HTMLElement>('.tl-seg').forEach((s, i) => {
          s.classList.toggle('past', !ready && i < index);
          s.classList.toggle('cur', !ready && i === index);
        });
      }
      e.ovRight.textContent = `${mmss(elapsed)} / ${mmss(total)}`;
    } else if (p.goal?.type === 'time') {
      (e.tlFill as HTMLElement).style.width = `${Math.min(100, (elapsed / (this.goal * 60)) * 100)}%`;
      e.ovRight.textContent = `${mmss(elapsed)} / ${mmss(this.goal * 60)}`;
    } else if (p.goal?.type === 'cal') {
      (e.tlFill as HTMLElement).style.width = `${Math.min(100, ((this.rec?.lastKcal ?? 0) / this.goal) * 100)}%`;
      e.ovRight.textContent = `${(this.rec?.lastKcal ?? 0).toFixed(1)} / ${this.goal} cal`;
    } else {
      e.ovRight.textContent = mmss(elapsed);
    }

    // cue
    e.cue.textContent = cue.text || '\u00a0';
    e.cue.className = `cue ${cue.level}`;
    this.paintGauge(shown, cad, cue);

    // metrics
    e.hr.textContent = hr ? String(hr) : '--';
    const hz = hr ? hrZone(hr, st.hrMax) : null;
    e.hrSub.textContent = hz ? `Z${hz.z} · ${Math.round(hz.pct * 100)}%` : 'bpm';
    e.mHr.style.setProperty('--c', hz ? ZONE_COLOR[hz.z] : '#8a94a3');
    e.w.textContent = String(Math.round(w));
    e.wSub.textContent = shown?.rpm != null && !ready ? `target ${segWatts(shown)}` : 'watts';
    e.cad.textContent = cad ? cad.toFixed(1) : '0';
    e.cadSub.textContent = shown?.rpm != null ? `target ${shown.rpm}${shown.allOut ? '+' : ''}` : 'rpm';
    e.mCad.dataset.level = cue.level;
    e.mW.dataset.level = cue.level === 'bad' ? 'bad' : 'idle';

    // strip
    e.sEl.textContent = hms(elapsed);
    e.sLeft.textContent = p.segments.length ? mmss(Math.max(0, totalSeconds(p) - elapsed)) : String(Math.round(this.rec?.avgW ?? 0));
    e.sAvg.textContent = String(Math.round(this.rec?.avgW ?? 0));
    e.sCal.textContent = (this.rec?.lastKcal ?? 0).toFixed(1);
    e.sX.textContent = p.zone === 'Z5' ? mmss(this.rec?.z5Seconds ?? 0) : ((this.rec?.lastDist ?? 0) / 1609.344).toFixed(2);

    // metronome button
    e.metro.classList.toggle('on', st.metroEnabled);
    e.metro.classList.toggle('playing', running && playing);
    e.metroTxt.textContent = !st.metroEnabled ? 'METRONOME OFF'
      : running && playing && bpm ? `${Math.round(bpm)} BPM`
      : st.metroMode === 'whole' ? 'METRONOME ON' : 'ARMED · TRANSITIONS';

    // sensor note
    e.sigNote.textContent = !bike ? 'Waiting for AirBike signal…' : '';

    // go button state
    if (this.engine.state === 'paused') { e.go.textContent = 'PAUSED'; }
    else if (running) { e.go.textContent = 'PAUSE'; }
  }

  private _tlIndex = -2;
  private _tlReady = true;

  private paintGauge(seg: Segment | null, cad: number, cue: Cue): void {
    const e = this.el;
    if (!seg || seg.rpm == null) {
      e.gauge.classList.add('off');
      return;
    }
    e.gauge.classList.remove('off');
    const lo = seg.allOut ? seg.rpm - 15 : seg.rpm - 10;
    const hi = seg.allOut ? seg.rpm + 25 : seg.rpm + 10;
    const pct = (v: number) => Math.min(100, Math.max(0, ((v - lo) / (hi - lo)) * 100));
    const zl = seg.allOut ? pct(seg.rpm) : pct(seg.rpm - CORRIDOR_RPM);
    const zh = seg.allOut ? 100 : pct(seg.rpm + CORRIDOR_RPM);
    e.gZone.style.left = `${zl}%`;
    e.gZone.style.width = `${zh - zl}%`;
    e.gNeedle.style.left = `${pct(cad)}%`;
    e.gauge.dataset.level = cue.level;
  }

  // ------------------------------------------------------------------ summary / save

  private showSummary(): void {
    this.view = 'summary';
    this.offChips?.();
    this.offChips = null;
    const rec = this.rec;
    const meta = rec ? rec.finalize() : null;
    if (!rec || !meta || meta.seconds < 5) {
      this.root.innerHTML = `${topbar({})}<main class="page"><section class="card"><h3>Nothing recorded</h3><p>The workout ended before any data was captured.</p>
        <a class="btn primary" href="#/airbike">Back to AirBike</a></section></main>`;
      if (meta) void discard(meta.id);
      return;
    }
    const p = this.p;
    const stat = (l: string, v: string) => `<div><small>${l}</small><b>${v}</b></div>`;
    this.root.innerHTML = `<main class="page summary">
      <h1 class="sum-title">${esc(p.name)}</h1>
      <div class="sum-sub">${new Date(meta.startEpochMs).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })}</div>
      <section class="stats">
        ${stat('TIME', hms(meta.seconds))}
        ${stat('AVG POWER', `${Math.round(meta.avgW)} W`)}
        ${stat('MAX POWER', `${Math.round(meta.maxW)} W`)}
        ${stat('AVG HR', meta.avgHr ? `${Math.round(meta.avgHr)}` : '--')}
        ${stat('MAX HR', meta.maxHr ? `${Math.round(meta.maxHr)}` : '--')}
        ${stat('WORK', `${Math.round(meta.kj)} kJ`)}
        ${stat('ASSAULT CAL', rec.lastKcal.toFixed(1))}
        ${stat('AVG CADENCE', `${rec.samples.length ? (rec.samples.reduce((a, x) => a + x.cad, 0) / rec.samples.length).toFixed(1) : '--'} rpm`)}
        ${stat('DISTANCE', `${(rec.lastDist / 1609.344).toFixed(2)} mi`)}
        ${p.zone === 'Z5' ? stat('Z5 BANK (≥88%)', mmss(rec.z5Seconds)) : ''}
      </section>
      <h2 class="sec">HOW HARD WAS IT?</h2>
      <section class="card">
        <div class="rpe" id="rpe">${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => `<button data-rpe="${n}">${n}</button>`).join('')}</div>
        <div class="rpe-label" id="rpeLabel">Rate perceived effort (1–10)</div>
      </section>
      <div id="saveBox">
        <button class="btn primary big" id="save" disabled>Save &amp; upload to Intervals.icu</button>
        <button class="btn danger big" id="discard">Discard workout</button>
      </div>
    </main>`;

    let rpe = 0;
    const q = <T extends HTMLElement>(id: string) => this.root.querySelector<T>('#' + id)!;
    q('rpe').addEventListener('click', (ev) => {
      const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-rpe]');
      if (!b) return;
      rpe = Number(b.dataset.rpe);
      this.root.querySelectorAll('#rpe button').forEach((x) => x.classList.toggle('on', x === b));
      q('rpeLabel').textContent = `${rpe}/10 · ${RPE_LABEL[rpe]}`;
      q<HTMLButtonElement>('save').disabled = false;
    });
    q('discard').addEventListener('click', async () => {
      if (!confirm('Discard this workout? It will not be saved or uploaded.')) return;
      await discard(meta.id);
      location.hash = '#/airbike';
    });
    q('save').addEventListener('click', async () => {
      const box = q('saveBox');
      meta.rpe = rpe;
      meta.status = 'saved';
      meta.upload = 'pending';
      await putMeta(meta);
      box.innerHTML = `<div class="status busy"><span class="spinner"></span> Saving &amp; uploading…</div>`;
      const r = await syncWorkout(meta);
      if (r.upload === 'done') {
        box.innerHTML = `<div class="status ok">✓ Synced to Intervals.icu</div>
          <a class="btn big" href="https://intervals.icu/activities/${esc(r.activityId ?? '')}" target="_blank" rel="noopener">Open in Intervals.icu</a>
          <a class="btn primary big" href="#/">Done</a>`;
      } else {
        box.innerHTML = `<div class="status warn">Saved on this phone. Upload didn’t go through:<br><small>${esc(r.lastError ?? 'unknown error')}</small><br><small>It will retry automatically next time you open the app.</small></div>
          <a class="btn primary big" href="#/">Done</a>`;
      }
    });
  }
}

function segTarget(g: Segment): string {
  if (g.rpm == null) return 'full rest';
  return g.allOut ? `${g.rpm}+ RPM · all out` : `${g.rpm} RPM · ${segWatts(g)}`;
}
const segWatts = (g: Segment): string => `${Math.round(wattsForRpm(g.rpm ?? 0))} W`;
