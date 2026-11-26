"""Real-time Dual-Stage Line-In Acoustic Echo Canceller (AEC) for The Armory.

Cancels loud gym house music captured from the Denon AVR-3313CI ``PRE OUT FL/FR``
feed (OptiPlex Realtek ``Line In``) out of the ceiling/room ``PowerConf S330`` mic
in real time at 16 kHz (< 1.5 ms CPU per 32 ms frame, 16 ms streaming latency):

  - **Auto-Bypass:** When Line In stays below ``-72 dBFS`` for > 1.5 s (Denon off
    or paused), bypasses both stages with zero alteration and zero latency.
  - **Zero-Jitter PLL + Auto GCC-PHAT Lag Lock:** Smoothly tracks hardware clock
    drift between the USB S330 and PCIe Realtek Line In without callback-jitter
    phase jumps, and automatically locks onto the acoustic + S330 DSP delay
    (~113-120 ms) whenever music starts.
  - **Stage 1 — Linear PBFDAF (320 ms tail, 16 ms blocks):** Partitioned-Block
    Frequency-Domain NLMS with soft double-talk protection. Erases ~10-11 dB of
    booming bass and direct echo with zero spectral-gating artifacts — ideal for
    ``openWakeWord`` fast Denon-duck detection.
  - **Stage 2 — Reference-Guided Spectral Echo Suppressor (RES, 288 ms tail):**
    Causal 50%-overlap sine-windowed multi-tap power-domain echo model that is
    completely immune to the S330's internal 4-mic beamformer phase switching.
    Erases ~17 dB total across 80-7500 Hz while letting near-end speech pop
    through cleanly — ideal for Silero VAD and CUDA ``faster-whisper``.
"""

from __future__ import annotations

import math
import threading
from typing import Any

import numpy as np

SAMPLE_RATE_16K = 16_000
BLOCK_SAMPLES = 256          # 16 ms hop / PBFDAF block
FFT_SIZE = 512               # 32 ms FFT window (2 * BLOCK_SAMPLES)
FREQ_BINS = FFT_SIZE // 2 + 1
RING_SIZE = 65_536           # 4.096 s circular reference buffer @ 16 kHz


def _dbfs(rms: float) -> float:
    return 20.0 * math.log10(max(1e-9, float(rms)))


def _make_sine_window(n: int) -> np.ndarray:
    """Perfect-reconstruction 50%-overlap COLA sine analysis/synthesis window."""
    idx = np.arange(n, dtype=np.float32)
    return np.sin(np.pi * (idx + 0.5) / float(n)).astype(np.float32)


def estimate_gcc_phat_lag(
    mic_16k: np.ndarray,
    ref_16k: np.ndarray,
    *,
    sr: int = SAMPLE_RATE_16K,
    min_ms: float = 35.0,
    max_ms: float = 220.0,
) -> tuple[int, float]:
    """Return ``(lag_samples, peak_score)`` where ``mic[n]`` echoes ``ref[n - lag]``."""
    n = min(int(mic_16k.size), int(ref_16k.size))
    if n < 1024:
        return 0, 0.0
    m = mic_16k[-n:].astype(np.float32, copy=False)
    r = ref_16k[-n:].astype(np.float32, copy=False)
    m = m - float(np.mean(m))
    r = r - float(np.mean(r))
    if float(np.max(np.abs(m))) < 1e-5 or float(np.max(np.abs(r))) < 1e-5:
        return 0, 0.0
    fft_n = 1 << (2 * n - 1).bit_length()
    G = np.fft.rfft(m, fft_n) * np.conj(np.fft.rfft(r, fft_n))
    G /= np.abs(G) + 1e-8
    cc = np.fft.irfft(G, fft_n)
    lo = max(1, int(round(min_ms * sr / 1000.0)))
    hi = min(n - 1, int(round(max_ms * sr / 1000.0)))
    if hi <= lo:
        return 0, 0.0
    sub = cc[lo:hi]
    pk_i = int(np.argmax(np.abs(sub)))
    return lo + pk_i, float(abs(sub[pk_i]))


class LineInAecProcessor:
    """Thread-safe streaming Dual-Stage Line-In Echo Canceller (16 kHz mono)."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        default_lag_ms: float = 118.0,
        precursor_margin_samples: int = 128,
        pbfdaf_partitions: int = 20,      # 20 * 16 ms = 320 ms linear reverb tail
        pbfdaf_mu: float = 0.45,
        res_taps: int = 18,               # 18 * 16 ms = 288 ms spectral reverb tail
        res_over_sub: float = 1.00,
        res_g_min: float = 0.20,
        active_ref_rms_threshold: float = 0.00025,  # ~ -72 dBFS
        idle_hold_sec: float = 1.5,
    ) -> None:
        self.enabled = bool(enabled)
        self._precursor = max(32, min(384, int(precursor_margin_samples)))
        self._default_target_lag = max(
            128,
            int(round(float(default_lag_ms) * SAMPLE_RATE_16K / 1000.0)) - self._precursor,
        )
        self._target_lag = float(self._default_target_lag)
        self._M = max(4, min(32, int(pbfdaf_partitions)))
        self._mu_max = float(pbfdaf_mu)
        self._res_taps = max(4, min(28, int(res_taps)))
        self._res_over_sub = float(res_over_sub)
        self._res_g_min = float(res_g_min)
        self._ref_rms_thr = float(active_ref_rms_threshold)
        self._idle_hold_blocks = max(10, int(round(float(idle_hold_sec) * SAMPLE_RATE_16K / BLOCK_SAMPLES)))

        self._ring_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._win = _make_sine_window(FFT_SIZE)

        # Stream connection metadata (populated by RoomVoiceService)
        self.stream_open: bool = False
        self.device_name: str = ""
        self.host_api: str = ""
        self.last_error: str | None = None

        self.reset()

    def reset(self) -> None:
        """Reset circular buffers, PLL cursor, and adaptive PBFDAF + RES weights."""
        with self._ring_lock:
            self._ref_ring = np.zeros(RING_SIZE, dtype=np.float32)
            self._ref_written: int = 0
            self._latest_ref_rms: float = 0.0
        with self._proc_lock:
            self._ref_cursor: float = -1.0
            self._gap_ema: float = self._target_lag
            self._target_lag = float(self._default_target_lag)
            self._lag_locked: bool = False
            self._measured_lag_ms: float = (self._default_target_lag + self._precursor) * 1000.0 / SAMPLE_RATE_16K

            # Pending sample buffers (in case mic callback isn't an exact multiple of 256)
            self._mic_pending = np.zeros(0, dtype=np.float32)
            self._ref_pending = np.zeros(0, dtype=np.float32)

            # Stage-1 PBFDAF state
            self._W = np.zeros((self._M, FREQ_BINS), dtype=np.complex64)
            self._X = np.zeros((self._M, FREQ_BINS), dtype=np.complex64)
            self._P = np.full(FREQ_BINS, 1e-5, dtype=np.float32)
            self._x_prev = np.zeros(BLOCK_SAMPLES, dtype=np.float32)

            # Stage-2 causal overlap-add RES state
            self._H_res = np.zeros((self._res_taps, FREQ_BINS), dtype=np.float32)
            self._R_hist = np.zeros((self._res_taps, FREQ_BINS), dtype=np.float32)
            self._P_res = np.full(FREQ_BINS, 1e-5, dtype=np.float32)
            self._res_e_prev = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
            self._res_r_prev = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
            self._res_ola_tail = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
            self._gain_smooth = np.ones(FREQ_BINS, dtype=np.float32)

            # Auto-delay GCC-PHAT calibration rolling window (~1.5 s)
            self._cal_len = 24_576
            self._cal_mic = np.zeros(self._cal_len, dtype=np.float32)
            self._cal_ref = np.zeros(self._cal_len, dtype=np.float32)
            self._cal_filled = 0
            self._cal_blocks_since = 0

            # Activity & telemetry
            self._active_hold: int = 0
            self._active: bool = False
            self._res_frames: int = 0
            self._dt_streak: int = 0
            self._mic_pow_ema: float = 1e-9
            self._clean_pow_ema: float = 1e-9
            self._erle_db: float = 0.0
            self._frames_processed: int = 0
            self._drift_ppm_est: float = 0.0

    def _reset_filters_locked(self) -> None:
        self._W.fill(0)
        self._X.fill(0)
        self._P.fill(1e-5)
        self._x_prev.fill(0)
        self._H_res.fill(0)
        self._R_hist.fill(0)
        self._P_res.fill(1e-5)
        self._res_e_prev.fill(0)
        self._res_r_prev.fill(0)
        self._res_ola_tail.fill(0)
        self._gain_smooth.fill(1.0)
        self._res_frames = 0
        self._dt_streak = 0

    def push_ref_samples(self, ref_16k: np.ndarray) -> None:
        """Append 16 kHz mono float32 Line-In samples into the lock-free-style ring buffer."""
        arr = np.asarray(ref_16k, dtype=np.float32).reshape(-1)
        n = int(arr.size)
        if n == 0:
            return
        rms = float(np.sqrt(np.mean(arr * arr)))
        with self._ring_lock:
            w_start = self._ref_written % RING_SIZE
            if n >= RING_SIZE:
                self._ref_ring[:] = arr[-RING_SIZE:]
            elif w_start + n <= RING_SIZE:
                self._ref_ring[w_start : w_start + n] = arr
            else:
                first = RING_SIZE - w_start
                self._ref_ring[w_start:] = arr[:first]
                self._ref_ring[: n - first] = arr[first:]
            self._ref_written += n
            self._latest_ref_rms = 0.65 * self._latest_ref_rms + 0.35 * rms

    def _read_ring_slice_locked(self, start_pos: float, n: int) -> np.ndarray:
        """Read ``n`` linearly interpolated samples starting at absolute position ``start_pos``."""
        i0 = int(math.floor(start_pos))
        frac = float(start_pos - i0)
        idx = (np.arange(n + 1, dtype=np.int64) + i0) % RING_SIZE
        vals = self._ref_ring[idx]
        if frac <= 1e-6:
            return vals[:n].copy()
        return ((1.0 - frac) * vals[:n] + frac * vals[1:]).astype(np.float32)

    def _fetch_aligned_ref_locked(self, n: int) -> tuple[np.ndarray, np.ndarray, float, bool]:
        """Pull ``n`` PLL-aligned reference samples + ``n`` unshifted recent ref samples for GCC-PHAT."""
        with self._ring_lock:
            written = self._ref_written
            ref_rms = self._latest_ref_rms
            if written < int(self._target_lag) + n + 512:
                zeros = np.zeros(n, dtype=np.float32)
                return zeros, zeros, ref_rms, False

            if self._ref_cursor < 0.0:
                self._ref_cursor = float(written - self._target_lag - n)
                self._gap_ema = self._target_lag

            # Gap between most-recent reference sample and the end of the slice we are about to read
            cur_gap = float(written) - (self._ref_cursor + float(n))
            if cur_gap < 128.0 or cur_gap > float(RING_SIZE - 4096):
                # Stream restart / overrun: re-anchor cleanly
                self._ref_cursor = float(written - self._target_lag - n)
                self._gap_ema = self._target_lag
                self._reset_filters_locked()
            else:
                self._gap_ema = 0.96 * self._gap_ema + 0.04 * cur_gap

            gap_err = self._gap_ema - self._target_lag
            # Gentle PLL nudge (max +/- 0.15 samp per 512-samp frame = +/-293 PPM, zero phase jitter)
            nudge = max(-0.15, min(0.15, gap_err * 0.0008))
            self._drift_ppm_est = 0.95 * self._drift_ppm_est + 0.05 * ((nudge / max(1.0, float(n))) * 1e6)

            read_start = self._ref_cursor
            aligned = self._read_ring_slice_locked(read_start, n)
            # Also read unshifted recent reference ending at `written` for auto-delay GCC-PHAT
            recent_start = float(written - n)
            recent = self._read_ring_slice_locked(recent_start, n)
            self._ref_cursor = read_start + float(n) + nudge
            return aligned, recent, ref_rms, True

    def _maybe_update_lag_locked(self, mic_blk: np.ndarray, ref_recent_blk: np.ndarray) -> None:
        """Periodically verify acoustic + S330 DSP bulk delay via GCC-PHAT."""
        n = int(mic_blk.size)
        if n <= 0 or n >= self._cal_len:
            return
        self._cal_mic[:-n] = self._cal_mic[n:]
        self._cal_mic[-n:] = mic_blk
        self._cal_ref[:-n] = self._cal_ref[n:]
        self._cal_ref[-n:] = ref_recent_blk
        self._cal_filled = min(self._cal_len, self._cal_filled + n)
        self._cal_blocks_since += 1

        check_interval = 96 if not self._lag_locked else 250  # ~1.5s initial lock, then every ~4.0s
        if self._cal_filled < 16_384 or self._cal_blocks_since < check_interval:
            return
        self._cal_blocks_since = 0

        lag_s, pk = estimate_gcc_phat_lag(self._cal_mic, self._cal_ref, sr=SAMPLE_RATE_16K)
        if pk < 0.065 or lag_s < 400:
            return
        want_target = float(max(256, min(3600, lag_s - self._precursor)))
        diff = want_target - self._target_lag
        self._measured_lag_ms = lag_s * 1000.0 / SAMPLE_RATE_16K
        self._lag_locked = True
        if abs(diff) > 256.0:
            # Large lag shift (e.g. hardware reconnect): re-center immediately
            self._target_lag = want_target
            self._ref_cursor = -1.0
            self._reset_filters_locked()
        elif abs(diff) > 96.0:
            # Moderate drift: smoothly update target; PLL pulls cursor cleanly
            self._target_lag = 0.70 * self._target_lag + 0.30 * want_target

    def _process_block_256_locked(self, d_cur: np.ndarray, x_cur: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Run one 256-sample (16 ms) block through Stage-1 PBFDAF + Stage-2 causal OLA RES."""
        # --- Stage 1: Linear PBFDAF (320 ms tail) ---
        x_blk = np.concatenate([self._x_prev, x_cur])
        self._x_prev = x_cur.copy()
        self._X[1:] = self._X[:-1]
        self._X[0] = np.fft.rfft(x_blk)

        y_cur = np.fft.irfft(np.sum(self._W * self._X, axis=0), FFT_SIZE)[BLOCK_SAMPLES:].astype(np.float32)
        e_cur = (d_cur - y_cur).astype(np.float32)

        r_pow = float(np.mean(x_cur * x_cur))
        if r_pow >= 1e-8:
            e_pow = float(np.mean(e_cur * e_cur))
            y_pow = float(np.mean(y_cur * y_cur))
            d_pow = float(np.mean(d_cur * d_cur))
            # Soft double-talk freeze: slow filter adaptation when near-end speech spikes above echo
            if y_pow > 1e-6 and e_pow > 1.35 * y_pow and d_pow > 1.50 * y_pow:
                eff_mu = self._mu_max * 0.08
            else:
                eff_mu = self._mu_max
            E_fft = np.fft.rfft(np.concatenate([np.zeros(BLOCK_SAMPLES, dtype=np.float32), e_cur]))
            Px = np.sum(np.abs(self._X) ** 2, axis=0)
            self._P = 0.85 * self._P + 0.15 * Px
            step = (eff_mu / (self._P + 1e-6)) * E_fft
            grad_t = np.fft.irfft(np.conj(self._X) * step[None, :], FFT_SIZE, axis=1)
            grad_t[:, BLOCK_SAMPLES:] = 0.0
            self._W += np.fft.rfft(grad_t, FFT_SIZE, axis=1).astype(np.complex64)

        # --- Stage 2: Causal 50%-Overlap Reference-Guided Spectral Echo Suppressor (RES) ---
        e_win = np.concatenate([self._res_e_prev, e_cur]) * self._win
        r_win = np.concatenate([self._res_r_prev, x_cur]) * self._win
        self._res_e_prev = e_cur.copy()
        self._res_r_prev = x_cur.copy()

        Ef = np.fft.rfft(e_win)
        Rf = np.fft.rfft(r_win)
        E_pow_f = (np.abs(Ef) ** 2).astype(np.float32)
        R_pow_f = (np.abs(Rf) ** 2).astype(np.float32)

        self._R_hist[1:] = self._R_hist[:-1]
        self._R_hist[0] = R_pow_f
        E_hat = np.sum(self._H_res * self._R_hist, axis=0)

        # Vocal-band double-talk detector (312 Hz .. 3500 Hz -> bins 10..112).
        # Let H_res bootstrap cleanly for the first ~0.8s (50 blocks) after music onset,
        # and cap continuous DT freeze at 3.0s so genuine room/speaker changes never latch.
        e_vox = float(np.sum(E_pow_f[10:112]))
        eh_vox = float(np.sum(E_hat[10:112]))
        r_tot = float(np.sum(self._R_hist[:, 10:112]))
        if r_tot > 1e-6:
            self._res_frames += 1
        raw_dt = (
            self._res_frames > 50
            and eh_vox > 1e-5
            and e_vox > 1.55 * eh_vox
        )
        if raw_dt:
            self._dt_streak += 1
        else:
            self._dt_streak = 0
        is_dt = raw_dt and (self._dt_streak <= 220)

        if r_tot > 1e-6:
            mu_p = 0.004 if is_dt else 0.14
            innov = np.minimum(E_pow_f - E_hat, 0.0) if is_dt else (E_pow_f - E_hat)
            self._P_res = 0.85 * self._P_res + 0.15 * np.sum(self._R_hist * self._R_hist, axis=0)
            self._H_res = np.maximum(
                0.0,
                self._H_res + (mu_p * innov / (self._P_res + 1e-7))[None, :] * self._R_hist,
            )

            # Light 3-bin spectral smoothing eliminates musical noise while preserving speech formants
            E_sm = 0.25 * np.roll(E_pow_f, 1) + 0.50 * E_pow_f + 0.25 * np.roll(E_pow_f, -1)
            Eh_sm = 0.25 * np.roll(E_hat, 1) + 0.50 * E_hat + 0.25 * np.roll(E_hat, -1)
            post_sig = np.maximum(0.0, E_sm - self._res_over_sub * Eh_sm)
            g_inst = np.clip(np.sqrt(post_sig / (E_sm + 1e-9)), self._res_g_min, 1.0)
        else:
            g_inst = np.ones(FREQ_BINS, dtype=np.float32)

        self._gain_smooth = np.where(
            g_inst > self._gain_smooth,
            0.72 * g_inst + 0.28 * self._gain_smooth,
            0.38 * g_inst + 0.62 * self._gain_smooth,
        ).astype(np.float32)

        y_frame = np.fft.irfft(Ef * self._gain_smooth, FFT_SIZE).astype(np.float32) * self._win
        clean_cur = (self._res_ola_tail + y_frame[:BLOCK_SAMPLES]).astype(np.float32)
        self._res_ola_tail = y_frame[BLOCK_SAMPLES:].copy()

        # Track live ERLE only on pure-echo frames (not during double-talk speech)
        if not is_dt and r_tot > 1e-6:
            d_p = float(np.mean(d_cur * d_cur))
            c_p = float(np.mean(clean_cur * clean_cur))
            self._mic_pow_ema = 0.94 * self._mic_pow_ema + 0.06 * max(1e-9, d_p)
            self._clean_pow_ema = 0.94 * self._clean_pow_ema + 0.06 * max(1e-9, c_p)
            self._erle_db = max(0.0, 10.0 * math.log10((self._mic_pow_ema + 1e-9) / (self._clean_pow_ema + 1e-9)))

        return e_cur, clean_cur

    def process_mic_frame(self, mic_16k: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
        """Process a 16 kHz mono mic frame; returns ``(linear_16k, clean_16k, aec_active)``."""
        mic = np.asarray(mic_16k, dtype=np.float32).reshape(-1)
        n = int(mic.size)
        if n == 0 or not self.enabled:
            self._active = False
            return mic, mic, False

        with self._proc_lock:
            ref_al, ref_recent, ref_rms, has_ref = self._fetch_aligned_ref_locked(n)
            if not has_ref:
                self._active = False
                return mic, mic, False

            if ref_rms >= self._ref_rms_thr:
                self._active_hold = self._idle_hold_blocks
            elif self._active_hold > 0:
                self._active_hold = max(0, self._active_hold - max(1, n // BLOCK_SAMPLES))

            if self._active_hold <= 0:
                if self._active:
                    self._active = False
                    self._erle_db = 0.0
                    self._mic_pending = np.zeros(0, dtype=np.float32)
                    self._ref_pending = np.zeros(0, dtype=np.float32)
                    self._res_ola_tail.fill(0)
                return mic, mic, False

            self._active = True
            self._frames_processed += 1
            self._maybe_update_lag_locked(mic, ref_recent)

            if self._mic_pending.size > 0:
                m_buf = np.concatenate([self._mic_pending, mic])
                r_buf = np.concatenate([self._ref_pending, ref_al])
            else:
                m_buf = mic
                r_buf = ref_al

            n_blocks = int(m_buf.size) // BLOCK_SAMPLES
            if n_blocks == 0:
                self._mic_pending = m_buf
                self._ref_pending = r_buf
                return mic, mic, True

            lin_parts: list[np.ndarray] = []
            cln_parts: list[np.ndarray] = []
            for b in range(n_blocks):
                a = b * BLOCK_SAMPLES
                e_blk, c_blk = self._process_block_256_locked(
                    m_buf[a : a + BLOCK_SAMPLES],
                    r_buf[a : a + BLOCK_SAMPLES],
                )
                lin_parts.append(e_blk)
                cln_parts.append(c_blk)

            rem = n_blocks * BLOCK_SAMPLES
            self._mic_pending = m_buf[rem:].copy()
            self._ref_pending = r_buf[rem:].copy()
            return np.concatenate(lin_parts), np.concatenate(cln_parts), True

    def status_dict(self) -> dict[str, Any]:
        with self._ring_lock:
            ref_rms = self._latest_ref_rms
            ref_written = self._ref_written
        return {
            "enabled": bool(self.enabled),
            "streamOpen": bool(self.stream_open),
            "device": self.device_name,
            "hostApi": self.host_api,
            "active": bool(self._active),
            "locked": bool(self._lag_locked),
            "lineInDb": round(_dbfs(ref_rms), 1) if ref_written > 0 else -99.0,
            "erleDb": round(float(self._erle_db), 1) if self._active else 0.0,
            "lagMs": round(float(self._measured_lag_ms), 1),
            "driftPpm": round(float(self._drift_ppm_est), 1),
            "frames": int(self._frames_processed),
            "error": self.last_error,
        }
