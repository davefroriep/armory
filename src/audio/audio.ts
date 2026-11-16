/** WebAudio metronome + chimes. Lookahead scheduler so beats stay on time under UI load. */
class Audio {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private nextBeat = 0;
  private beatIdx = 0;
  private bpm = 0;
  private playing = false;
  private unlocked = false;
  private busyUntil = 0;
  volume = 0.8;

  private clearIdle(): void {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  private scheduleIdleSuspend(delayMs: number): void {
    this.clearIdle();
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      if (!this.playing && this.ctx && this.ctx.state === 'running' && Date.now() >= this.busyUntil) {
        void this.ctx.suspend().catch(() => { /* ignore */ });
      }
    }, delayMs);
  }

  /**
   * Must be called from a user gesture at least once (Start / metronome tap).
   * Uses 'playback' latencyHint and suspends when idle so Android Bluetooth
   * headphones do not stay stuck in low-latency audio routing.
   */
  unlock(): void {
    if (!this.ctx) {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      try {
        this.ctx = new AC({ latencyHint: 'playback' });
      } catch {
        this.ctx = new AC();
      }
      this.master = this.ctx.createGain();
      this.master.connect(this.ctx.destination);
    }
    if (this.master) this.master.gain.value = this.volume;
    if (this.unlocked) return;
    const settle = () => {
      this.unlocked = true;
      if (!this.playing && Date.now() >= this.busyUntil) this.scheduleIdleSuspend(40);
    };
    if (this.ctx.state === 'suspended') {
      void this.ctx.resume().then(settle).catch(() => { /* gesture rejected */ });
    } else {
      settle();
    }
  }

  private playBurst(durSec: number, fn: (t: number) => void): void {
    if (!this.ctx) return;
    this.clearIdle();
    this.busyUntil = Math.max(this.busyUntil, Date.now() + durSec * 1000 + 80);
    const fire = () => {
      if (!this.ctx) return;
      fn(this.ctx.currentTime);
      if (!this.playing) this.scheduleIdleSuspend(Math.ceil(durSec * 1000) + 150);
    };
    if (this.ctx.state === 'suspended') {
      void this.ctx.resume().then(fire).catch(() => { /* ignore */ });
    } else {
      fire();
    }
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  /** Start / retune / stop the click track. */
  setMetronome(play: boolean, bpm: number | null): void {
    if (!play || !bpm) {
      this.stopMetronome();
      return;
    }
    this.unlock();
    this.clearIdle();
    if (this.ctx?.state === 'suspended') void this.ctx.resume().catch(() => { /* ignore */ });
    this.bpm = bpm;
    if (this.playing || !this.ctx) return;
    this.playing = true;
    this.beatIdx = 0;
    this.nextBeat = this.ctx.currentTime + 0.05;
    this.timer = setInterval(() => this.schedule(), 25);
  }

  private stopMetronome(): void {
    this.playing = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.ctx?.state === 'running') this.scheduleIdleSuspend(150);
  }

  private schedule(): void {
    if (!this.ctx || !this.playing) return;
    // Background tabs throttle timers to ~1 Hz, so look further ahead when hidden.
    const ahead = document.hidden ? 1.6 : 0.15;
    while (this.nextBeat < this.ctx.currentTime + ahead) {
      this.click(this.nextBeat, this.beatIdx % 2 === 0);
      this.nextBeat += 60 / this.bpm;
      this.beatIdx++;
    }
  }

  private tone(at: number, freq: number, dur: number, gain: number, type: OscillatorType = 'sine'): void {
    if (!this.ctx || !this.master) return;
    const o = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    o.type = type;
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, at);
    g.gain.exponentialRampToValueAtTime(gain, at + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    o.connect(g).connect(this.master);
    o.start(at);
    o.stop(at + dur + 0.02);
  }

  /** Left/right stroke click: slightly higher on the left-leg beat. */
  private click(at: number, accent: boolean): void {
    this.tone(at, accent ? 1500 : 1100, 0.045, accent ? 0.9 : 0.6, 'square');
  }

  /** Phase-change chime (rising double tone). */
  chime(): void {
    this.playBurst(0.38, (t) => {
      this.tone(t, 880, 0.12, 0.7);
      this.tone(t + 0.13, 1320, 0.22, 0.7);
    });
  }

  /** Overshoot warning (fast double chirp). */
  chirp(): void {
    this.playBurst(0.20, (t) => {
      this.tone(t, 2100, 0.07, 0.8, 'sawtooth');
      this.tone(t + 0.1, 2100, 0.07, 0.8, 'sawtooth');
    });
  }

  end(): void {
    this.playBurst(0.42, (t) => {
      this.tone(t, 660, 0.4, 0.7);
    });
  }

  setVolume(v: number): void {
    this.volume = v;
    if (this.master) this.master.gain.value = v;
  }
}

export const audio = new Audio();

export function buzz(pattern: number | number[]): void {
  try {
    navigator.vibrate?.(pattern);
  } catch { /* unsupported */ }
}
