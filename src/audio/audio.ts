import { isNative, playNativeChime } from '../native/service';

export const ALERT_SAMPLE_RATE = 24_000;

export interface BellHit {
  at: number;
  freq: number;
  tau: number;
  amp?: number;
}

export const ALERT_SPECS: Record<'chime' | 'end' | 'chirp', { durSec: number; hits: BellHit[] }> = {
  // Pleasant warm C-major triad ("ding · ding · ding") with 40ms Bluetooth wake-up lead-in
  chime: {
    durSec: 1.30,
    hits: [
      { at: 0.04, freq: 523.25, tau: 0.13, amp: 0.78 },
      { at: 0.28, freq: 659.25, tau: 0.13, amp: 0.80 },
      { at: 0.52, freq: 783.99, tau: 0.24, amp: 0.86 },
    ],
  },
  // Warm 2-note resolving finish chime
  end: {
    durSec: 0.95,
    hits: [
      { at: 0.04, freq: 659.25, tau: 0.14, amp: 0.78 },
      { at: 0.28, freq: 523.25, tau: 0.22, amp: 0.82 },
    ],
  },
  // Crisp AirBike overshoot warning
  chirp: {
    durSec: 0.28,
    hits: [
      { at: 0.02, freq: 1320, tau: 0.045, amp: 0.75 },
      { at: 0.14, freq: 1320, tau: 0.045, amp: 0.75 },
    ],
  },
};

/** Synthesize warm acoustic bell/marimba hits into 16-bit little-endian mono PCM bytes. */
export function synthesizeBellPcm(durSec: number, hits: BellHit[], sampleRate = ALERT_SAMPLE_RATE): Uint8Array {
  const n = Math.max(1, Math.round(durSec * sampleRate));
  const f = new Float32Array(n);
  const fadeOutSamples = Math.min(n, Math.round(0.03 * sampleRate));
  for (const h of hits) {
    const start = Math.round(h.at * sampleRate);
    const amp = h.amp ?? 0.8;
    const attackSec = 0.007;
    const maxLen = Math.min(n, start + Math.round(h.tau * 5.5 * sampleRate));
    const w = 2 * Math.PI * h.freq;
    for (let i = start; i < maxLen; i++) {
      const dt = (i - start) / sampleRate;
      const att = dt < attackSec ? 0.5 * (1 - Math.cos((Math.PI * dt) / attackSec)) : 1;
      const env = amp * att * Math.exp(-dt / h.tau);
      const tone =
        Math.sin(w * dt) +
        0.26 * Math.sin(2 * w * dt) * Math.exp(-dt / 0.05) +
        0.08 * Math.sin(3 * w * dt) * Math.exp(-dt / 0.025);
      f[i] += env * tone;
    }
  }
  let peak = 1;
  for (let i = 0; i < n; i++) {
    const a = Math.abs(f[i]);
    if (a > peak) peak = a;
  }
  const scale = peak > 0.92 ? 0.92 / peak : 1;
  const pcm = new Uint8Array(n * 2);
  const dv = new DataView(pcm.buffer);
  for (let i = 0; i < n; i++) {
    const tail = i >= n - fadeOutSamples ? (n - 1 - i) / Math.max(1, fadeOutSamples) : 1;
    const sample = Math.max(-1, Math.min(1, f[i] * scale * tail));
    dv.setInt16(i * 2, Math.round(sample * 32767), true);
  }
  return pcm;
}

/** Wrap raw 16-bit mono PCM bytes with a canonical 44-byte RIFF/WAVE header. */
export function encodeWav(pcm: Uint8Array, sampleRate = ALERT_SAMPLE_RATE): Uint8Array {
  const out = new Uint8Array(44 + pcm.length);
  const dv = new DataView(out.buffer);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) out[off + i] = s.charCodeAt(i);
  };
  writeStr(0, 'RIFF');
  dv.setUint32(4, 36 + pcm.length, true);
  writeStr(8, 'WAVEfmt ');
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  writeStr(36, 'data');
  dv.setUint32(40, pcm.length, true);
  out.set(pcm, 44);
  return out;
}

const bytesToBase64 = (bytes: Uint8Array): string => {
  let bin = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
};

/**
 * Cadence metronome (WebAudio lookahead) + finite-duration WAV alerts (< 5s HTMLAudioElement on Web,
 * native AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK AudioTrack on APK) so alerts duck music cleanly and pop
 * right back to full volume without keeping a Bluetooth AudioContext open.
 */
class Audio {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private nextBeat = 0;
  private beatIdx = 0;
  private bpm = 0;
  private playing = false;

  private alertEl: HTMLAudioElement | null = null;
  private alertUnlocked = false;
  private alertPlaying = false;
  private alertCleanup: ReturnType<typeof setTimeout> | null = null;
  private silentUrl: string | null = null;
  private wavUrls = new Map<keyof typeof ALERT_SPECS, string>();
  private wavB64Cache = new Map<keyof typeof ALERT_SPECS, string>();
  volume = 0.8;

  private ensureEl(): HTMLAudioElement | null {
    if (typeof document === 'undefined') return null;
    this.alertEl ??= document.createElement('audio');
    return this.alertEl;
  }

  private getSilentUrl(): string {
    if (!this.silentUrl) {
      const wav = encodeWav(new Uint8Array(Math.round(0.05 * ALERT_SAMPLE_RATE) * 2));
      this.silentUrl = URL.createObjectURL(new Blob([wav.buffer as ArrayBuffer], { type: 'audio/wav' }));
    }
    return this.silentUrl;
  }

  private getPcm(kind: keyof typeof ALERT_SPECS): Uint8Array {
    const spec = ALERT_SPECS[kind];
    return synthesizeBellPcm(spec.durSec, spec.hits);
  }

  private getWavUrl(kind: keyof typeof ALERT_SPECS): string {
    let u = this.wavUrls.get(kind);
    if (!u) {
      const wav = encodeWav(this.getPcm(kind));
      u = URL.createObjectURL(new Blob([wav.buffer as ArrayBuffer], { type: 'audio/wav' }));
      this.wavUrls.set(kind, u);
    }
    return u;
  }

  getWavBase64(kind: keyof typeof ALERT_SPECS): string {
    let s = this.wavB64Cache.get(kind);
    if (!s) {
      s = bytesToBase64(encodeWav(this.getPcm(kind)));
      this.wavB64Cache.set(kind, s);
    }
    return s;
  }

  private teardownAlert(): void {
    if (this.alertCleanup) {
      clearTimeout(this.alertCleanup);
      this.alertCleanup = null;
    }
    this.alertPlaying = false;
    const el = this.alertEl;
    if (!el) return;
    el.onended = null;
    el.onerror = null;
    el.pause();
    el.removeAttribute('src');
    el.load();
  }

  /**
   * Must be called from a user gesture once (first checkmark / rest tap).
   * Silenty unlocks the reusable <audio> element with muted=true so Bluetooth
   * headphone audio / music is completely untouched until a timer actually fires.
   */
  unlock(): void {
    if (isNative() || this.alertUnlocked) return;
    const el = this.ensureEl();
    if (!el || this.alertPlaying) return;
    el.muted = true;
    el.volume = 0;
    el.src = this.getSilentUrl();
    void el.play().then(() => {
      if (!this.alertPlaying) {
        el.pause();
        el.removeAttribute('src');
        el.load();
        el.muted = false;
      }
      this.alertUnlocked = true;
    }).catch(() => { /* gesture rejected */ });
  }

  private playWebAlert(kind: keyof typeof ALERT_SPECS): void {
    const el = this.ensureEl();
    if (!el) return;
    this.teardownAlert();
    this.alertPlaying = true;
    el.muted = false;
    el.volume = Math.max(0, Math.min(1, this.volume));
    el.src = this.getWavUrl(kind);
    el.currentTime = 0;
    const done = () => this.teardownAlert();
    el.onended = done;
    el.onerror = done;
    const durMs = Math.ceil(ALERT_SPECS[kind].durSec * 1000);
    this.alertCleanup = setTimeout(done, durMs + 220);
    void el.play().then(() => {
      this.alertUnlocked = true;
    }).catch(done);
  }

  private playAlert(kind: keyof typeof ALERT_SPECS): void {
    if (isNative()) {
      void playNativeChime(this.getWavBase64(kind), this.volume).then((ok) => {
        if (!ok) this.playWebAlert(kind);
      });
      return;
    }
    this.playWebAlert(kind);
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  /** Start / retune / stop the AirBike cadence click track. */
  setMetronome(play: boolean, bpm: number | null): void {
    if (!play || !bpm) {
      this.stopMetronome();
      return;
    }
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
    if (this.ctx.state === 'suspended') void this.ctx.resume().catch(() => { /* ignore */ });
    this.bpm = bpm;
    if (this.playing) return;
    this.playing = true;
    this.beatIdx = 0;
    this.nextBeat = this.ctx.currentTime + 0.05;
    this.timer = setInterval(() => this.schedule(), 25);
  }

  private stopMetronome(): void {
    this.playing = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.ctx) {
      void this.ctx.close().catch(() => { /* ignore */ });
      this.ctx = null;
      this.master = null;
    }
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

  /** Rest / phase-change alert ("ding · ding · ding"). */
  chime(): void {
    this.playAlert('chime');
  }

  /** Overshoot warning (fast double chirp). */
  chirp(): void {
    this.playAlert('chirp');
  }

  end(): void {
    this.playAlert('end');
  }

  setVolume(v: number): void {
    this.volume = v;
    if (this.master) this.master.gain.value = v;
    if (this.alertEl) this.alertEl.volume = Math.max(0, Math.min(1, v));
  }
}

export const audio = new Audio();

export function buzz(pattern: number | number[]): void {
  try {
    navigator.vibrate?.(pattern);
  } catch { /* unsupported */ }
}
