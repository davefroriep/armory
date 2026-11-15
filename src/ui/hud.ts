import { bikeFrame, bikeFresh, hrFresh, live } from '../ble/telemetry';
import { sim } from '../ble/sim';
import { bikeStatusNote, ensureSensors } from '../ble/sources';
import { audio, buzz } from '../audio/audio';
import { WorkoutEngine } from '../domain/engine';
import { CORRIDOR_RPM, cueFor, hrCue, pacerDecision, type Cue, type PacerConfig } from '../domain/pacer';
import { recoveryLimitBpm } from '../domain/gate';
import { repNote, repStats, type RepStat } from '../domain/reps';
import { PHASE_COLOR, fmtDur, fmtRpm, programById, resolveProgram, targetWatts, totalSeconds, workSeconds, type Program, type Segment } from '../domain/programs';
import { wattsForRpm } from '../domain/airbikeCurve';
import { WindowAvg } from '../domain/smooth';
import { calendar } from '../intervals/calendar';
import { workoutService } from '../native/service';
import { getSettings, updateSettings } from '../store/settings';
import { putMeta } from '../store/db';
import { discard } from '../workout/sync';
import { Recorder } from '../workout/recorder';
import { ICON, esc, hms, mmss } from './dom';
import { topbar, wireChips } from './chrome';
import { ZONE_COLOR, hrZone } from './hr';
import { renderSummary } from './summaryView';
import { LeaveGuard } from './leaveGuard';

/** Display smoothing for cadence and power. The gauge needle + cue use their own window (Settings). */
const SMOOTH_MS = 3000;
const rpmTxt = (g: Segment): string => `${fmtRpm(g.rpm ?? 0)}${g.allOut ? '+' : ''}`;

export function hudScreen(root: HTMLElement, args: string[], query: URLSearchParams): () => void {
  const program = programById(args[0]);
  if (!program) {
    location.hash = '#/airbike';
    return () => {};
  }
  return new Hud(root, program, query.get('ev') ? Number(query.get('ev')) : null).mount();
}

class Hud {
  private baseProgram: Program;
  private p: Program;
  private offsetWatts = 0;
  private engine: WorkoutEngine;
  private rec: Recorder | null = null;
  private goal: number;
  private timer: ReturnType<typeof setInterval> | null = null;
  private offFrame: (() => void) | null = null;
  private offSmooth: (() => void) | null = null;
  private offChips: (() => void) | null = null;
  private lock: WakeLockSentinel | null = null;
  private lastIndex = -2;
  private lockSince: number | null = null;
  private lastChirp = 0;
  private lastSvc = 0;
  private completeShown = false;
  private view: 'hud' | 'summary' = 'hud';
  private cadAvg = new WindowAvg(SMOOTH_MS);
  private wAvg = new WindowAvg(SMOOTH_MS);
  private tlIndex = -2;
  private tlReady = true;
  private guard = new LeaveGuard(() => this.active);
  /** Live state of the gated recovery block (null outside gated blocks). */
  private gateState: { waiting: boolean; timerOnly: boolean; into: number; minSec: number; nominal: number; hrLimit: number } | null = null;
  private el!: Record<string, HTMLElement>;

  constructor(private root: HTMLElement, baseProgram: Program, private eventId: number | null) {
    this.baseProgram = baseProgram;
    this.p = resolveProgram(baseProgram, getSettings().ftp, 0);
    this.engine = new WorkoutEngine(this.p);
    this.goal = this.p.goal?.def ?? 0;
  }

  // ------------------------------------------------------------------ mount / teardown

  mount(): () => void {
    ensureSensors({ bike: true });
    this.paintHud();
    this.loop();
    this.offSmooth = bikeFrame.on((f) => {
      const t = Date.now();
      this.cadAvg.push(t, f.cad);
      this.wAvg.push(t, f.w);
    });
    this.timer = setInterval(() => this.loop(), 100);
    const vis = () => { if (!document.hidden && this.active) void this.acquireLock(); };
    const unload = (e: BeforeUnloadEvent) => { if (this.active) e.preventDefault(); };
    document.addEventListener('visibilitychange', vis);
    window.addEventListener('beforeunload', unload);
    return () => {
      if (this.timer) clearInterval(this.timer);
      this.guard.dispose();
      this.offFrame?.();
      this.offSmooth?.();
      this.offChips?.();
      audio.setMetronome(false, null);
      sim.setTarget(0);
      this.releaseLock();
      void workoutService.stop();
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
    const hasTrim = this.baseProgram.zone === 'Z5' || this.baseProgram.zone === 'Z2';
    const trimRow = hasTrim
      ? `<div class="trim" id="trimCtl">
          <button type="button" data-trim="-5" aria-label="Lower target 5 watts">−5 W</button>
          <button type="button" class="trim-mid" id="trimLbl" title="Tap to reset trim"></button>
          <button type="button" data-trim="5" aria-label="Raise target 5 watts">+5 W</button>
        </div>`
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
      ${trimRow}
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
        <button class="skip" id="skip" hidden>SKIP</button>
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
      'pauseSub', 'doneModal', 'ovRight', 'tl', 'sigNote', 'skip']) this.el[id] = q(id);
    if (segs.length) this.el.tlHead = q('tlHead');
    else this.el.tlFill = q('tlFill');
    if (hasTrim) this.el.trimLbl = q('trimLbl');

    this.offChips?.();
    this.offChips = wireChips(this.root);
    this.bind();
  }

  private adjustTarget(nextOffset: number): void {
    const clamped = Math.max(-80, Math.min(120, nextOffset));
    if (clamped === this.offsetWatts) return;
    this.offsetWatts = clamped;
    this.p = resolveProgram(this.baseProgram, getSettings().ftp, this.offsetWatts);
    this.engine.setProgram(this.p);
    this.loop();
  }

  private bind(): void {
    const on = (id: string, fn: () => void) => this.root.querySelector('#' + id)!.addEventListener('click', fn);
    on('skip', () => { if (this.engine.skip(Date.now()) && getSettings().haptics) buzz([60]); this.loop(); });
    on('go', () => (this.engine.state === 'ready' ? this.start() : this.pause()));
    on('resume', () => this.resume());
    on('finish', () => this.finish());
    on('doneFinish', () => this.finish());
    on('doneKeep', () => { this.el.doneModal.hidden = true; this.engine.resume(Date.now()); });
    on('metro', () => {
      audio.unlock();
      updateSettings({ metroEnabled: !getSettings().metroEnabled });
    });
    this.root.querySelectorAll<HTMLElement>('[data-trim]').forEach((b) =>
      b.addEventListener('click', () => this.adjustTarget(this.offsetWatts + Number(b.dataset.trim))));
    this.root.querySelector('#trimLbl')?.addEventListener('click', () => this.adjustTarget(0));
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
    this.guard.arm();
    this.lastIndex = -2;
    void this.acquireLock();
    void workoutService.start(`Armory Fit · ${this.p.name}`, 'Starting…');
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
    void workoutService.update(`Armory Fit · ${this.p.name}`, `PAUSED · ${hms(this.engine.elapsed(Date.now()))}`);
  }

  private resume(): void {
    this.el.pauseModal.hidden = true;
    this.engine.resume(Date.now());
  }

  private finish(): void {
    const now = Date.now();
    this.engine.finish(now);
    this.guard.disarm();
    if (this.rec) this.rec.tick(this.engine.elapsed(now), 0);
    audio.setMetronome(false, null);
    sim.setTarget(0);
    this.el.pauseModal.hidden = true;
    this.el.doneModal.hidden = true;
    this.releaseLock();
    void workoutService.stop();
    this.showSummary();
  }

  // ------------------------------------------------------------------ main loop (10 Hz)

  private loop(): void {
    if (this.view !== 'hud') return;
    const now = Date.now();
    const st = getSettings();
    const running = this.engine.state === 'running';
    // HR-gated recovery: end the block when HR is back at the target (floor / ceiling rules in domain/gate.ts)
    const hrNow = hrFresh() ? live.hr : null;
    const hrLimit = recoveryLimitBpm(st.hrMax, st.recoveryHrPct);
    if (running) {
      const released = this.engine.checkGate(now, { hr: hrNow, hrLimit, mode: st.recoveryMode, lead: st.metroLead || 5 });
      if (released !== null && st.haptics) buzz([80, 60, 80]);
    }
    const elapsed = this.engine.elapsed(now);
    const loc = this.engine.locate(now);
    const seg = loc.seg;
    const waiting = !!seg?.gate && this.engine.waitingOnGate(now);
    this.gateState = seg?.gate
      ? { waiting, timerOnly: st.recoveryMode === 'timed' || hrNow == null, into: loc.into, minSec: seg.gate.minSec, nominal: this.p.segments[loc.index]?.seconds ?? seg.seconds, hrLimit }
      : null;
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

    // 3 s smoothed readings drive the numbers, the cue text and the lock-in test
    const bike = bikeFresh();
    const cadS = bike ? this.cadAvg.value(now) ?? 0 : 0;
    const wS = bike ? this.wAvg.value(now) ?? 0 : 0;
    // bar needle + cue use their own (shorter) window so they agree with each other
    const needleMs = Number(st.needleSmooth) * 1000;
    const cadN = !bike ? 0 : needleMs <= 0 ? live.cad : this.cadAvg.value(now, needleMs) ?? 0;

    if (running && seg?.rpm != null && !seg.allOut && Math.abs(cadS - seg.rpm) <= CORRIDOR_RPM) this.lockSince ??= now;
    else this.lockSince = null;
    const lockedIn = this.lockSince != null && now - this.lockSince >= 3000;

    // metronome
    const cfg: PacerConfig = { mode: st.metroMode, lead: st.metroLead, tail: st.metroTail, autoMute: st.metroAutoMute };
    const dec = pacerDecision(this.p, loc, cfg, st.metroEnabled, lockedIn);
    audio.setMetronome(running && dec.play, dec.bpm);

    // cue + overshoot chirp (smoothed, so a single hard pedal stroke doesn't nag)
    let cue = running ? cueFor(seg, cadN, loc.into) : ({ level: 'idle', text: '', chirp: false } as Cue);
    if (running && cue.level !== 'bad') {
      const gateLimit = waiting && !this.gateState?.timerOnly ? hrLimit : null;
      const extra = hrCue(seg, hrNow ?? 0, st.hrMax, gateLimit);
      if (extra) cue = { ...extra, chirp: cue.chirp };
    }
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

    // lock-screen notification text (Android app only), every 5 s
    if (running && now - this.lastSvc > 5000) {
      this.lastSvc = now;
      const hr = hrFresh() ? live.hr : 0;
      void workoutService.update(`Armory Fit · ${this.p.name}`, `${mmss(elapsed)} · ${Math.round(wS)} W · ${hr || '--'} BPM`);
    }

    this.paint(loc.seg, loc.next, loc.remaining, loc.index, elapsed, cue, dec.bpm, dec.play, running, cadS, wS, cadN);
  }

  // ------------------------------------------------------------------ painting

  private paint(seg: Segment | null, next: Segment | null, remaining: number, index: number, elapsed: number,
    cue: Cue, bpm: number | null, playing: boolean, running: boolean, cadS: number, wS: number, cadN: number): void {
    const e = this.el;
    const p = this.p;
    const st = getSettings();
    const ready = this.engine.state === 'ready';
    const hr = hrFresh() ? live.hr : 0;
    const bike = bikeFresh();

    // banner
    const shown = ready ? p.segments[0] ?? null : seg;
    const color = shown ? PHASE_COLOR[shown.kind] : '#1e9bff';
    e.banner.style.setProperty('--ph', color);
    e.ph.textContent = ready ? 'READY' : this.engine.state === 'paused' ? 'PAUSED' : shown?.label ?? (p.goal ? 'GOAL' : 'FREE RIDE');
    e.tag.textContent = shown?.tag ?? '';
    e.tgt.textContent = shown ? segTarget(shown) : p.goal ? 'steady effort' : '';

    // countdown (whole seconds only)
    if (p.segments.length) {
      const gs = this.gateState;
      let shownLeft = remaining;
      if (gs?.waiting) {
        if (gs.timerOnly) shownLeft = Math.max(0, gs.nominal - gs.into);
        else shownLeft = Math.max(0, gs.minSec - gs.into);
      }
      const hrWait = !!gs?.waiting && !gs.timerOnly && gs.into >= gs.minSec;
      e.cdLabel.textContent = hrWait ? `RECOVER TO HR ≤ ${gs!.hrLimit}` : gs?.waiting && !gs.timerOnly ? 'MINIMUM REST' : next ? 'NEXT TRANSITION' : 'WORKOUT ENDS IN';
      e.cd.textContent = ready ? mmss(p.segments[0].seconds) : hrWait ? `+${mmss(Math.floor(gs!.into - gs!.minSec))}` : mmss(Math.ceil(shownLeft));
      e.cd.classList.toggle('hot', !ready && shownLeft <= 5 && running && !hrWait);
      e.next.innerHTML = ready
        ? `FIRST UP: ${esc(p.segments[0].label)}`
        : next ? `NEXT: <b>${esc(next.label)}</b>${next.rpm != null ? ` · ${rpmTxt(next)} RPM${next.allOut ? '' : ` · ${targetWatts(next)} W`}` : ' · full rest'}` : 'LAST BLOCK';
    } else if (p.goal?.type === 'time') {
      e.cd.textContent = mmss(Math.ceil(Math.max(0, this.goal * 60 - elapsed)));
    } else if (p.goal?.type === 'cal') {
      e.cd.textContent = String(Math.ceil(Math.max(0, this.goal - (this.rec?.lastKcal ?? 0))));
    } else {
      e.cd.textContent = mmss(elapsed);
    }

    // timeline
    if (p.segments.length) {
      const total = totalSeconds(p);
      (e.tlHead as HTMLElement).style.left = `${Math.min(100, this.progress(index, seg, elapsed, ready, total) * 100)}%`;
      if (index !== this.tlIndex || ready !== this.tlReady) {
        this.tlIndex = index;
        this.tlReady = ready;
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
      e.ovRight.textContent = `${Math.round(this.rec?.lastKcal ?? 0)} / ${this.goal} cal`;
    } else {
      e.ovRight.textContent = mmss(elapsed);
    }

    // cue + center bar (the bar uses the Settings → cadence bar smoothing window, default 2 s)
    e.cue.textContent = cue.text || '\u00a0';
    e.cue.className = `cue ${cue.level}`;
    this.paintGauge(shown, cadN, cue);

    // metrics: integers only
    e.hr.textContent = hr ? String(hr) : '--';
    const hz = hr ? hrZone(hr, st.hrMax) : null;
    e.hrSub.textContent = hz ? `Z${hz.z} · ${Math.round(hz.pct * 100)}%` : 'bpm';
    e.mHr.style.setProperty('--c', hz ? ZONE_COLOR[hz.z] : '#8a94a3');
    e.w.textContent = String(Math.round(wS));
    e.wSub.textContent = shown?.rpm != null && !ready ? `target ${segWatts(shown)}` : 'watts';
    e.cad.textContent = String(Math.round(cadS));
    e.cadSub.textContent = shown?.rpm != null ? `target ${rpmTxt(shown)}` : 'rpm';
    e.mCad.dataset.level = cue.level;
    e.mW.dataset.level = cue.level === 'bad' ? 'bad' : 'idle';
    if (e.trimLbl) {
      const ref = p.segments.find((g) => g.kind === 'work' || g.kind === 'steady');
      if (ref?.rpm != null) {
        const off = this.offsetWatts ? ` (${this.offsetWatts > 0 ? '+' : '−'}${Math.abs(this.offsetWatts)} W)` : '';
        e.trimLbl.textContent = `WORK ${targetWatts(ref)} W · ${fmtRpm(ref.rpm)} RPM${off}`;
      }
    }

    // strip
    e.sEl.textContent = hms(elapsed);
    e.sLeft.textContent = p.segments.length ? mmss(this.engine.remainingNominal(Date.now())) : String(Math.round(this.rec?.avgW ?? 0));
    e.sAvg.textContent = String(Math.round(this.rec?.avgW ?? 0));
    e.sCal.textContent = String(Math.round(this.rec?.lastKcal ?? 0));
    e.sX.textContent = p.zone === 'Z5' ? mmss(this.rec?.z5Seconds ?? 0) : ((this.rec?.lastDist ?? 0) / 1609.344).toFixed(2);

    // metronome button
    e.metro.classList.toggle('on', st.metroEnabled);
    e.metro.classList.toggle('playing', running && playing);
    e.metroTxt.textContent = !st.metroEnabled ? 'METRONOME OFF'
      : running && playing && bpm ? `${Math.round(bpm)} BPM`
      : st.metroMode === 'whole' ? 'METRONOME ON' : 'ARMED · TRANSITIONS';

    e.sigNote.textContent = !bike ? bikeStatusNote() || 'Waiting for AirBike signal…' : '';

    e.skip.hidden = !(running && seg && (seg.kind === 'rest' || seg.kind === 'warmup'));
    e.skip.textContent = seg?.kind === 'warmup' ? 'SKIP WARM-UP' : 'READY ▶';
    if (this.engine.state === 'paused') e.go.textContent = 'PAUSED';
    else if (running) e.go.textContent = 'PAUSE';
  }

  /** Timeline head position (0..1) on the nominal timeline; gated blocks fill up to their nominal length. */
  private progress(index: number, seg: Segment | null, elapsed: number, ready: boolean, total: number): number {
    if (ready || !seg || !total) return 0;
    const segs = this.p.segments;
    if (index >= segs.length - 1 && this.engine.locate(Date.now()).complete) return 1;
    let before = 0;
    for (let i = 0; i < index; i++) before += segs[i].seconds;
    const nominal = segs[index]?.seconds ?? seg.seconds;
    const into = this.engine.locate(Date.now()).into;
    const len = this.gateState?.waiting ? nominal : Math.max(1, seg.seconds);
    return Math.min(1, (before + Math.min(1, into / len) * nominal) / total);
  }

  /** Center-bar: target in the middle, smoothed needle left/right of it. */
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
    const reps = repStats(this.engine.segments(this.engine.elapsed(Date.now())), rec.samples);
    const note = repNote(reps);
    if (note) { meta.notes = note; void putMeta(meta); }
    const avgCad = rec.samples.length ? rec.samples.reduce((a, x) => a + x.cad, 0) / rec.samples.length : 0;
    const stats: [string, string][] = [
      ['AVG POWER', `${Math.round(meta.avgW)} W`],
      ['MAX POWER', `${Math.round(meta.maxW)} W`],
      ['AVG CADENCE', `${Math.round(avgCad)} rpm`],
      ['AVG HR', meta.avgHr ? `${Math.round(meta.avgHr)}` : '--'],
      ['MAX HR', meta.maxHr ? `${Math.round(meta.maxHr)}` : '--'],
      ['WORK', `${Math.round(meta.kj)} kJ`],
      ['ASSAULT CAL', `${Math.round(rec.lastKcal)}`],
      ['DISTANCE', `${(rec.lastDist / 1609.344).toFixed(2)} mi`],
      this.p.zone === 'Z5' ? ['Z5 BANK (≥88%)', mmss(rec.z5Seconds)] : ['TIME', hms(meta.seconds)],
    ];
    renderSummary(this.root, { title: this.p.name, meta, stats, extra: repTable(reps), backHref: '#/airbike' });
  }
}

function segTarget(g: Segment): string {
  if (g.rpm == null) return 'full rest';
  return g.allOut ? `${rpmTxt(g)} RPM · all out` : `${rpmTxt(g)} RPM · ${segWatts(g)}`;
}
const segWatts = (g: Segment): string => `${Math.round(wattsForRpm(g.rpm ?? 0))} W`;

const mmssR = (s: number): string => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;

/** Per-rep progress table for the summary (power / peak HR per work rep, real recovery length and end HR). */
function repTable(reps: RepStat[]): string {
  const work = reps.filter((r) => r.kind === 'work');
  if (!work.length) return '';
  const rest = reps.filter((r) => r.kind === 'rest');
  const rows = work.map((w, i) => {
    const r = rest[i];
    return `<tr><td>${w.n}</td><td>${Math.round(w.avgW)} W</td><td>${w.hrPeak || '--'}</td><td>${r ? mmssR(r.sec) : '--'}</td><td>${r?.hrEnd || '--'}</td></tr>`;
  }).join('');
  return `<h2 class="sec">REPS &amp; RECOVERY</h2><section class="card reps"><table><thead><tr><th>#</th><th>AVG</th><th>PEAK HR</th><th>REST</th><th>HR AT END</th></tr></thead><tbody>${rows}</tbody></table></section>`;
}
