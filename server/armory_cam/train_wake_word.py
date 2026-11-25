"""Custom openWakeWord ONNX trainer for The Armory (e.g. `hey_armory.onnx`, `hey_atlas.onnx`).

Uses openWakeWord's exact frozen feature extractor (`melspectrogram.onnx` + `embedding_model.onnx`)
so training features have 100% parity with live inference in `room_voice.py`.

Features:
  - Multi-engine synthetic speaker diversity (48 Kokoro GPU voices + random speaker blends + 16 Edge Neural voices)
  - Strictly held-out unseen speakers (`6` Kokoro voices) for honest zero-shot generalization testing
  - Mid-sentence wake alignment (lone `"Hey Armory"` AND `"Hey Armory, <gym command>"` tails)
  - Gym acoustic augmentation (synthetic room reverb, far-field EQ tilt, real gym rock music mixing at 0..+25 dB SNR)
  - Massive negative coverage: ~160h ACAV100M pre-computed features + real gym rock tapes + 60+ adversarial near-homophones
  - Drop-in personal voice support: place `.wav`/`.m4a` clips in `S:\\ArmoryCam\\oww_training\\<slug>\\user_positives\\`
    and run `--retrain-only` to produce a voice-personalized ONNX model in <60 seconds.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import io
import json
import math
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from typing import Any
import wave

import numpy as np
from scipy.signal import butter, fftconvolve, lfilter, resample_poly, sosfilt
import torch
import torch.nn as nn

ROOT_DIR = Path(r"S:\ArmoryCam")
OWW_MODELS_DIR = ROOT_DIR / "models" / "openwakeword"
TRAIN_ROOT = ROOT_DIR / "oww_training"
NEG_DIR = TRAIN_ROOT / "negatives"
FFMPEG_BIN = r"C:\Users\davef\Tools\ffmpeg\bin\ffmpeg.exe"
SR = 16_000
CLIP_SAMPLES = 32_000  # 2.0 s @ 16 kHz -> yields 16+ embedding frames cleanly

# Hold out 6 diverse Kokoro voices strictly for blind unseen-speaker evaluation!
HELD_OUT_KOKORO_VOICES = frozenset({
    "am_onyx",
    "am_echo",
    "bm_george",
    "af_bella",
    "af_sky",
    "bf_emma",
})

EDGE_TRAIN_VOICES = (
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-GuyNeural",
    "en-US-EricNeural",
    "en-US-SteffanNeural",
    "en-US-RogerNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
    "en-GB-RyanNeural",
    "en-GB-ThomasNeural",
    "en-GB-SoniaNeural",
    "en-AU-WilliamNeural",
    "en-AU-NatashaNeural",
    "en-CA-LiamNeural",
)

EDGE_VAL_VOICES = (
    "en-US-ChristopherNeural",
    "en-GB-LibbyNeural",
    "en-IE-ConnorNeural",
    "en-CA-ClaraNeural",
)

GYM_COMMAND_TAILS = (
    "record this",
    "225 for 8",
    "185 for 10",
    "315 for 5",
    "done",
    "check",
    "start hold",
    "next exercise",
    "skip rest",
    "add set",
    "undo",
    "open the gym",
    "next song",
    "play music",
    "pause music",
    "record rack",
)


def adversarial_phrases_for(phrase: str) -> list[str]:
    clean = re.sub(r"\s+", " ", phrase.strip().lower())
    base = [
        "hey",
        "hey you",
        "hey now",
        "hey man",
        "hey bro",
        "hey buddy",
        "hey guys",
        "hey there",
        "hey everyone",
        "hey wait",
        "hey what",
        "hey look",
        "hey check",
        "hey sorry",
        "hey honey",
        "hey jarvis",
        "hey google",
        "hey siri",
        "hey alexa",
        "hey record this",
        "record this",
        "225 for 8",
        "185 for 10",
        "done",
        "start hold",
        "next exercise",
        "skip rest",
        "I'm already done",
        "I'm already",
        "I'm sorry",
        "I'm gonna record this",
        "ordinary",
        "every",
        "factory",
        "battery",
        "gallery",
        "salary",
        "celery",
        "mystery",
        "history",
        "victory",
        "delivery",
        "recovery",
        "category",
        "territory",
        "cemetery",
    ]
    if "armory" in clean:
        base.extend([
            "hey army",
            "army",
            "hey harmony",
            "harmony",
            "hey memory",
            "memory",
            "hey primary",
            "primary",
            "hey summary",
            "summary",
            "hey emory",
            "emory",
            "hey amory",
            "amory",
            "hey armor",
            "armor",
            "hey armored",
            "armored",
            "hey artery",
            "artery",
            "hey emery",
            "hey ivory",
            "hey anomaly",
            "hey already",
            "already",
            "our money",
            "hey our money",
            "hey marry me",
            "hey marie",
            "hey marty",
            "hey arnie",
            "hey harvey",
            "hey charlie",
            "hey barney",
            "hey rory",
            "hey corey",
            "hey tori",
            "hey lori",
            "hey dory",
            "hey maury",
            "hey aubrey",
            "hey audrey",
            "hey atlas",
            "armory",
            "the armory",
        ])
    elif "atlas" in clean:
        base.extend([
            "hey at last",
            "at last",
            "hey alice",
            "alice",
            "hey alex",
            "alex",
            "hey axis",
            "axis",
            "hey mantis",
            "hey cactus",
            "hey practice",
            "practice",
            "hey status",
            "status",
            "hey mattress",
            "hey palace",
            "hey dallas",
            "hey hatless",
            "hey headless",
            "hey careless",
            "hey armory",
            "atlas",
        ])
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in base:
        k = p.strip().lower()
        if k and k != clean and k not in seen:
            seen.add(k)
            out.append(p.strip())
    return out


def resample_16k(pcm: np.ndarray, src_sr: int) -> np.ndarray:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0 or src_sr == SR:
        return arr
    g = math.gcd(SR, int(src_sr))
    return resample_poly(arr, SR // g, int(src_sr) // g).astype(np.float32)


def trim_silence(pcm: np.ndarray, thr: float = 0.015, pad_samples: int = 800) -> np.ndarray:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    peak = float(np.max(np.abs(arr)))
    if peak < 1e-5:
        return arr
    nz = np.flatnonzero(np.abs(arr) >= peak * thr)
    if nz.size == 0:
        return arr
    s = max(0, int(nz[0]) - pad_samples)
    e = min(arr.size, int(nz[-1]) + pad_samples)
    return arr[s:e].copy()


def load_audio_16k(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        if sw == 2:
            arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif sw == 4:
            arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        if ch > 1:
            arr = arr[: (arr.size // ch) * ch].reshape(-1, ch).mean(axis=1)
        return resample_16k(arr, sr)
    raw = subprocess.check_output(
        [FFMPEG_BIN, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"],
        stderr=subprocess.DEVNULL,
    )
    return np.frombuffer(raw, dtype=np.float32).copy()


def synth_edge_16k(text: str, voice: str, rate_pct: int = 0, pitch_hz: int = 0) -> np.ndarray | None:
    import av
    import edge_tts

    rate_str = f"{rate_pct:+d}%"
    pitch_str = f"{pitch_hz:+d}Hz"

    async def _go() -> bytes:
        comm = edge_tts.Communicate(text, voice=voice, rate=rate_str, pitch=pitch_str)
        chunks: list[bytes] = []
        async for ch in comm.stream():
            if ch.get("type") == "audio":
                chunks.append(ch["data"])
        return b"".join(chunks)

    try:
        mp3 = asyncio.run(asyncio.wait_for(_go(), timeout=8.0))
        if not mp3:
            return None
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        parts: list[np.ndarray] = []
        with av.open(io.BytesIO(mp3), format="mp3") as container:
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    parts.append(rf.to_ndarray().reshape(-1))
            for rf in resampler.resample(None):
                parts.append(rf.to_ndarray().reshape(-1))
        if not parts:
            return None
        return trim_silence(np.concatenate(parts).astype(np.float32))
    except Exception:
        return None


def make_synthetic_rir(rng: random.Random) -> np.ndarray:
    """Create a realistic small/medium basement gym room impulse response (RT60 ~0.15-0.55s)."""
    rt60 = rng.uniform(0.14, 0.52)
    length = int(SR * min(0.60, rt60 * 1.15))
    t = np.arange(length, dtype=np.float32) / float(SR)
    decay = np.exp(-6.908 * t / rt60).astype(np.float32)
    np_rng = np.random.default_rng(rng.randint(0, 2**31 - 1))
    noise = np_rng.standard_normal(length).astype(np.float32)
    rir = noise * decay * rng.uniform(0.10, 0.32)
    # Direct path + 4 early wall/ceiling/floor reflections
    rir[0] = 1.0
    for _ in range(4):
        idx = rng.randint(int(0.004 * SR), min(length - 1, int(0.045 * SR)))
        rir[idx] += rng.uniform(-0.35, 0.35)
    # Gentle air absorption on reverb tail
    sos = butter(2, rng.uniform(3200.0, 6200.0) / (SR / 2.0), btype="low", output="sos")
    tail = sosfilt(sos, rir[1:]).astype(np.float32)
    rir[1:] = tail
    norm = float(np.sqrt(np.sum(rir * rir))) or 1.0
    return (rir / norm).astype(np.float32)


def augment_clip(
    voice_pcm: np.ndarray,
    rng: random.Random,
    bg_pool: list[np.ndarray],
    *,
    cmd_tail_pool: list[np.ndarray] | None = None,
    add_music_prob: float = 0.72,
    reverb_prob: float = 0.75,
    eq_prob: float = 0.65,
    trailing_tail_prob: float = 0.38,
) -> np.ndarray:
    """Build a 2.0s (32,000-sample) int16 clip with wake aligned cleanly near the right edge."""
    v = voice_pcm.astype(np.float32, copy=True)
    if v.size < 1600:
        v = np.pad(v, (0, 1600 - v.size))

    # 1. Slight random speed/pitch stretch (0.92x..1.08x)
    if rng.random() < 0.65:
        factor = rng.uniform(0.92, 1.08)
        new_len = max(1600, int(round(v.size * factor)))
        x_old = np.linspace(0.0, 1.0, v.size, dtype=np.float32)
        x_new = np.linspace(0.0, 1.0, new_len, dtype=np.float32)
        v = np.interp(x_new, x_old, v).astype(np.float32)

    # 2. Synthetic gym room reverb
    if rng.random() < reverb_prob:
        rir = make_synthetic_rir(rng)
        wet = fftconvolve(v, rir, mode="full")[: v.size + int(0.18 * SR)].astype(np.float32)
        v = wet

    # 3. Far-field microphone EQ tilt (off-axis / facing away simulation)
    if rng.random() < eq_prob:
        hp_hz = rng.uniform(75.0, 260.0)
        lp_hz = rng.uniform(3100.0, 7400.0)
        sos = butter(2, [hp_hz / (SR / 2.0), lp_hz / (SR / 2.0)], btype="band", output="sos")
        v = sosfilt(sos, v).astype(np.float32)

    # Normalize voice energy
    v_rms = float(np.sqrt(np.mean(v * v))) or 1e-4
    target_v_rms = rng.uniform(0.06, 0.24)
    v = v * (target_v_rms / v_rms)

    # 4. Place wake word so its END sits `0..340 ms` before the right edge of 32,000 samples
    # Optionally append the onset of a real gym command right after the wake word!
    trail_ms = rng.uniform(40.0, 340.0)
    trail_samples = int(round((trail_ms / 1000.0) * SR))
    out = np.zeros(CLIP_SAMPLES, dtype=np.float32)

    end_pos = CLIP_SAMPLES - trail_samples
    start_pos = end_pos - v.size
    if start_pos >= 0:
        out[start_pos:end_pos] += v
    else:
        # Clip longer than available room: keep the trailing end aligned at end_pos
        out[:end_pos] += v[-end_pos:]

    if cmd_tail_pool and rng.random() < trailing_tail_prob and trail_samples >= 1200:
        gap = rng.randint(0, min(trail_samples - 400, int(0.14 * SR)))
        tail_start = end_pos + gap
        avail = CLIP_SAMPLES - tail_start
        if avail >= 600:
            ct = rng.choice(cmd_tail_pool)
            ct_rms = float(np.sqrt(np.mean(ct * ct))) or 1e-4
            ct_scaled = ct[:avail] * (target_v_rms / ct_rms) * rng.uniform(0.75, 1.15)
            out[tail_start : tail_start + ct_scaled.size] += ct_scaled

    # 5. Mix real gym rock music / ambient room background at SNR -1 dB .. +24 dB
    if bg_pool and rng.random() < add_music_prob:
        bg_src = rng.choice(bg_pool)
        if bg_src.size > CLIP_SAMPLES:
            off = rng.randint(0, bg_src.size - CLIP_SAMPLES)
            bg = bg_src[off : off + CLIP_SAMPLES].copy()
        else:
            bg = np.resize(bg_src, CLIP_SAMPLES).copy()
        bg_rms = float(np.sqrt(np.mean(bg * bg))) or 1e-5
        snr_db = rng.uniform(-1.0, 22.0)
        desired_bg_rms = target_v_rms / (10.0 ** (snr_db / 20.0))
        out = out + bg * (desired_bg_rms / bg_rms)
    else:
        # Light ambient room floor noise so clean clips aren't digital zero
        np_rng = np.random.default_rng(rng.randint(0, 2**31 - 1))
        out = out + np_rng.standard_normal(CLIP_SAMPLES).astype(np.float32) * rng.uniform(0.0005, 0.006)

    # Soft clip + convert to int16 PCM
    peak = float(np.max(np.abs(out)))
    if peak > 0.96:
        out = out * (0.95 / peak)
    return np.round(np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16)


def mine_gym_backgrounds(max_segments: int = 180) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Mine real gym audio (loud music + room noise) from non-held-out dates (2026-11-21/22/24/25).

    Strictly skips `2026-11-23` so it stays 100% unseen for blind final evaluation.
    """
    tapes_root = ROOT_DIR / "audio_tapes"
    loud_beds: list[np.ndarray] = []
    ambient_beds: list[np.ndarray] = []
    rng = random.Random(42)

    candidate_files: list[Path] = []
    for day in ("2026-11-21", "2026-11-22", "2026-11-24", "2026-11-25"):
        d = tapes_root / day
        if not d.is_dir():
            continue
        files = sorted(d.glob("aud_*.m4a"))
        if len(files) > 220:
            step = max(1, len(files) // 220)
            files = files[::step]
        candidate_files.extend(files)

    rng.shuffle(candidate_files)
    for p in candidate_files:
        if len(loud_beds) >= max_segments and len(ambient_beds) >= 40:
            break
        try:
            arr = load_audio_16k(p)
        except Exception:
            continue
        if arr.size < SR * 5:
            continue
        rms = float(np.sqrt(np.mean(arr * arr)))
        if rms >= 0.008 and len(loud_beds) < max_segments:
            loud_beds.append(arr)
        elif 0.0008 <= rms < 0.008 and len(ambient_beds) < 40:
            ambient_beds.append(arr)
    return loud_beds, ambient_beds


def build_oww_feature_extractor():
    from openwakeword.utils import AudioFeatures

    return AudioFeatures(
        melspec_model_path=str(OWW_MODELS_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(OWW_MODELS_DIR / "embedding_model.onnx"),
        inference_framework="onnx",
        ncpu=4,
    )


def extract_last16_embeddings(fe: Any, clips_i16: list[np.ndarray], batch_size: int = 128) -> np.ndarray:
    """Convert list of 32,000-sample int16 clips to `(N, 16, 96)` float32 openWakeWord features."""
    if not clips_i16:
        return np.zeros((0, 16, 96), dtype=np.float32)
    out_parts: list[np.ndarray] = []
    for i in range(0, len(clips_i16), batch_size):
        batch = np.stack(clips_i16[i : i + batch_size], axis=0).astype(np.int16)
        embs = fe.embed_clips(batch, batch_size=batch_size, ncpu=1)  # (B, T, 96)
        # Keep the final 16 frames (aligned with the right edge where wake ends!)
        if embs.shape[1] >= 16:
            out_parts.append(embs[:, -16:, :].astype(np.float32))
        else:
            pad_t = 16 - embs.shape[1]
            padded = np.pad(embs, ((0, 0), (pad_t, 0), (0, 0)), mode="edge")
            out_parts.append(padded.astype(np.float32))
    return np.concatenate(out_parts, axis=0)


def extract_sliding_embeddings(fe: Any, long_pcm_list: list[np.ndarray], stride_frames: int = 4) -> np.ndarray:
    """Slice long negative gym audio clips into `(M, 16, 96)` sliding-window negative features."""
    windows: list[np.ndarray] = []
    for arr in long_pcm_list:
        if arr.size < CLIP_SAMPLES:
            continue
        i16 = np.round(np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
        # Feed in 10s chunks
        chunk_n = SR * 10
        chunks = [i16[j : j + chunk_n] for j in range(0, len(i16) - CLIP_SAMPLES + 1, chunk_n) if len(i16[j : j + chunk_n]) == chunk_n]
        if not chunks:
            chunks = [i16[:CLIP_SAMPLES]]
        embs = fe.embed_clips(np.stack(chunks, axis=0), batch_size=16, ncpu=1)  # (C, T, 96)
        for row in embs:
            for t in range(0, row.shape[0] - 16 + 1, stride_frames):
                windows.append(row[t : t + 16, :].astype(np.float32))
    if not windows:
        return np.zeros((0, 16, 96), dtype=np.float32)
    return np.stack(windows, axis=0)


class WakeWordMlp(nn.Module):
    """Exact openWakeWord-compatible 3-layer LayerNorm+ReLU MLP (`[B, 16, 96] -> [B, 1]`)."""

    def __init__(self, hidden_dim: int = 128, dropout: float = 0.25) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Flatten(),
            nn.Linear(16 * 96, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.net(x))

    def logits(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def generate_datasets(phrase: str, work_dir: Path) -> None:
    """Synthesize diverse positives, held-out unseen-speaker val positives, and hard adversarial negatives."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from room_voice import get_shared_kokoro

    rng = random.Random(20261125)
    kokoro, dev = get_shared_kokoro(ROOT_DIR / "models")
    voices_npz = np.load(ROOT_DIR / "models" / "kokoro" / "voices-v1.0.bin")
    all_kokoro = sorted(k for k in voices_npz.files if k.startswith(("am_", "af_", "bm_", "bf_")))
    train_kokoro = [v for v in all_kokoro if v not in HELD_OUT_KOKORO_VOICES]
    val_kokoro = [v for v in all_kokoro if v in HELD_OUT_KOKORO_VOICES]
    print(f"[1/4] Kokoro ({dev}): {len(train_kokoro)} train voices, {len(val_kokoro)} strictly held-out val voices")

    print("[2/4] Mining real gym background beds (2026-11-21/22/24; holding out 2026-11-23)...")
    loud_beds, ambient_beds = mine_gym_backgrounds(max_segments=42)
    all_beds = loud_beds + ambient_beds
    print(f"      Mined {len(loud_beds)} loud rock/music beds + {len(ambient_beds)} ambient beds")

    # Pre-synthesize short command tails (for mid-sentence wake alignment!)
    cmd_tails_pcm: list[np.ndarray] = []
    for idx, tail_txt in enumerate(GYM_COMMAND_TAILS):
        v_name = train_kokoro[idx % len(train_kokoro)]
        pcm, sr = kokoro.create(tail_txt, voice=v_name, speed=rng.uniform(0.95, 1.15), lang="en-us")
        cmd_tails_pcm.append(trim_silence(resample_16k(pcm, sr)))

    # Synthesize raw positive voice utterances across all training Kokoro voices + blends + Edge Neural
    phrase_variants = [phrase, f"{phrase}!", f"{phrase}.", f"{phrase},"]
    raw_train_pos: list[np.ndarray] = []
    print(f"[3/4] Synthesizing diverse '{phrase}' positives (Kokoro + blends + Edge Neural)...")
    for v_name in train_kokoro:
        for spd in (0.88, 0.98, 1.08, 1.18):
            txt = rng.choice(phrase_variants)
            pcm, sr = kokoro.create(txt, voice=v_name, speed=spd, lang="en-us")
            arr = trim_silence(resample_16k(pcm, sr))
            if arr.size >= 3200:
                raw_train_pos.append(arr)
    # 120 random 2-speaker Kokoro vector blends (novel vocal tracts!)
    for _ in range(120):
        v1, v2 = rng.sample(train_kokoro, 2)
        w = rng.uniform(0.25, 0.75)
        vec = kokoro.get_voice_style(v1) * w + kokoro.get_voice_style(v2) * (1.0 - w)
        pcm, sr = kokoro.create(rng.choice(phrase_variants), voice=vec, speed=rng.uniform(0.86, 1.20), lang="en-us")
        arr = trim_silence(resample_16k(pcm, sr))
        if arr.size >= 3200:
            raw_train_pos.append(arr)
    # Edge Neural voices (totally different vocoder & accents from Kokoro!)
    edge_pos_ok = 0
    for ev in EDGE_TRAIN_VOICES:
        for rate_p, pitch_h in ((0, 0), (-8, -4), (10, 4)):
            arr = synth_edge_16k(phrase, ev, rate_pct=rate_p, pitch_hz=pitch_h)
            if arr is not None and arr.size >= 3200:
                raw_train_pos.append(arr)
                edge_pos_ok += 1
    print(f"      Raw train positive utterances: {len(raw_train_pos)} (including {edge_pos_ok} Edge Neural)")

    # Strictly held-out unseen-speaker validation positives (clean + loud music!)
    raw_val_pos: list[np.ndarray] = []
    for v_name in val_kokoro:
        for spd in (0.90, 1.00, 1.12):
            pcm, sr = kokoro.create(phrase, voice=v_name, speed=spd, lang="en-us")
            arr = trim_silence(resample_16k(pcm, sr))
            if arr.size >= 3200:
                raw_val_pos.append(arr)
    for ev in EDGE_VAL_VOICES:
        arr = synth_edge_16k(phrase, ev, rate_pct=0, pitch_hz=0)
        if arr is not None and arr.size >= 3200:
            raw_val_pos.append(arr)
    print(f"      Held-out unseen-speaker val utterances: {len(raw_val_pos)}")

    # Synthesize hard adversarial negative phrases
    adv_list = adversarial_phrases_for(phrase)
    raw_adv_neg: list[np.ndarray] = []
    print(f"      Synthesizing {len(adv_list)} adversarial confusable phrases...")
    for idx, ap in enumerate(adv_list):
        # 5 distinct Kokoro speakers per adversarial phrase
        chosen = rng.sample(train_kokoro, min(5, len(train_kokoro)))
        for v_name in chosen:
            pcm, sr = kokoro.create(ap, voice=v_name, speed=rng.uniform(0.90, 1.16), lang="en-us")
            arr = trim_silence(resample_16k(pcm, sr))
            if arr.size >= 1600:
                raw_adv_neg.append(arr)
        # Plus 1 Edge Neural speaker for top confusables
        if idx < 30:
            ev = EDGE_TRAIN_VOICES[idx % len(EDGE_TRAIN_VOICES)]
            arr = synth_edge_16k(ap, ev)
            if arr is not None and arr.size >= 1600:
                raw_adv_neg.append(arr)
    print(f"      Raw adversarial negative utterances: {len(raw_adv_neg)}")

    # Augment & extract openWakeWord embeddings
    print("[4/4] Augmenting & extracting openWakeWord ONNX embeddings...")
    fe = build_oww_feature_extractor()

    # 4,200 augmented train positives
    pos_clips: list[np.ndarray] = []
    target_train_pos = 4200
    for i in range(target_train_pos):
        src = raw_train_pos[i % len(raw_train_pos)]
        pos_clips.append(
            augment_clip(
                src,
                rng,
                all_beds,
                cmd_tail_pool=cmd_tails_pcm,
                add_music_prob=0.72,
                reverb_prob=0.78,
                eq_prob=0.65,
                trailing_tail_prob=0.40,
            )
        )
    pos_emb = extract_last16_embeddings(fe, pos_clips)
    np.save(work_dir / "synth_pos_features.npy", pos_emb.astype(np.float16))
    print(f"      Saved synth_pos_features.npy: {pos_emb.shape}")

    # 300 unseen-speaker validation positives (half light/clean, half tough loud gym music!)
    val_pos_clips: list[np.ndarray] = []
    for i in range(300):
        src = raw_val_pos[i % len(raw_val_pos)]
        val_pos_clips.append(
            augment_clip(
                src,
                rng,
                loud_beds if (i % 2 == 1) else ambient_beds,
                cmd_tail_pool=cmd_tails_pcm,
                add_music_prob=0.90 if (i % 2 == 1) else 0.35,
                reverb_prob=0.75,
                eq_prob=0.60,
                trailing_tail_prob=0.40,
            )
        )
    val_pos_emb = extract_last16_embeddings(fe, val_pos_clips)
    np.save(work_dir / "val_pos_features.npy", val_pos_emb.astype(np.float16))
    print(f"      Saved val_pos_features.npy: {val_pos_emb.shape}")

    # 3,600 augmented hard adversarial negatives
    adv_clips: list[np.ndarray] = []
    target_adv = 3600
    for i in range(target_adv):
        src = raw_adv_neg[i % len(raw_adv_neg)]
        adv_clips.append(
            augment_clip(
                src,
                rng,
                all_beds,
                cmd_tail_pool=cmd_tails_pcm,
                add_music_prob=0.65,
                reverb_prob=0.70,
                eq_prob=0.60,
                trailing_tail_prob=0.25,
            )
        )
    adv_emb = extract_last16_embeddings(fe, adv_clips)
    np.save(work_dir / "adv_neg_features.npy", adv_emb.astype(np.float16))
    print(f"      Saved adv_neg_features.npy: {adv_emb.shape}")

    # Real gym rock/ambient negative sliding windows (from 2026-11-21/22/24)
    gym_neg_emb = extract_sliding_embeddings(fe, all_beds, stride_frames=3)
    np.save(work_dir / "gym_neg_features.npy", gym_neg_emb.astype(np.float16))
    print(f"      Saved gym_neg_features.npy: {gym_neg_emb.shape}")


def load_user_positives(work_dir: Path, fe: Any) -> np.ndarray:
    """Encode any personal voice recordings placed in `<work_dir>/user_positives/` with heavy augmentation."""
    user_dir = work_dir / "user_positives"
    user_dir.mkdir(parents=True, exist_ok=True)
    audio_files = [
        p for p in sorted(user_dir.iterdir())
        if p.is_file() and p.suffix.lower() in (".wav", ".m4a", ".mp3", ".ogg", ".flac")
    ]
    if not audio_files:
        return np.zeros((0, 16, 96), dtype=np.float32)

    rng = random.Random(777)
    loud_beds, ambient_beds = mine_gym_backgrounds(max_segments=24)
    all_beds = loud_beds + ambient_beds
    raw_clips = [trim_silence(load_audio_16k(p)) for p in audio_files]
    raw_clips = [c for c in raw_clips if c.size >= 3200]
    if not raw_clips:
        return np.zeros((0, 16, 96), dtype=np.float32)

    aug_clips: list[np.ndarray] = []
    reps = max(25, min(80, 1200 // len(raw_clips)))
    for src in raw_clips:
        for _ in range(reps):
            aug_clips.append(
                augment_clip(
                    src,
                    rng,
                    all_beds,
                    add_music_prob=0.70,
                    reverb_prob=0.45,  # Real gym clips already have natural room acoustics!
                    eq_prob=0.40,
                    trailing_tail_prob=0.0,
                )
            )
    emb = extract_last16_embeddings(fe, aug_clips)
    print(f"Encoded {len(raw_clips)} user voice clips -> {emb.shape[0]} personalized training samples")
    return emb


def eval_validation_fph(model: WakeWordMlp, val_neg_2d: np.ndarray, threshold: float = 0.50) -> tuple[int, float, float]:
    """Run model over 10.7h continuous validation_set_features.npy (481,345 frames -> 30,084 16-frame blocks)."""
    model.eval()
    n_frames = val_neg_2d.shape[0]
    # Stride 4 frames (320ms) for fast exact sliding evaluation across all 10.7 hours!
    starts = np.arange(0, n_frames - 16 + 1, 4)
    fp_events = 0
    cooldown = 0
    max_score = 0.0
    with torch.no_grad():
        for i in range(0, len(starts), 2048):
            idx = starts[i : i + 2048]
            batch = np.stack([val_neg_2d[s : s + 16] for s in idx], axis=0).astype(np.float32)
            probs = model(torch.from_numpy(batch)).squeeze(-1).numpy()
            bmax = float(probs.max())
            if bmax > max_score:
                max_score = bmax
            for p in probs:
                if cooldown > 0:
                    cooldown -= 1
                    continue
                if p >= threshold:
                    fp_events += 1
                    cooldown = 5  # ~1.6s debounce matching live openWakeWord cooldown
    hours = (n_frames * 0.080) / 3600.0
    return fp_events, fp_events / max(0.1, hours), max_score


def mine_hard_negatives(
    model: WakeWordMlp,
    acav_neg: np.ndarray,
    gym_neg: np.ndarray,
    adv_neg: np.ndarray,
    *,
    score_thr: float = 0.04,
    max_keep: int = 16_000,
) -> np.ndarray:
    """Sweep all 439k ACAV100M + gym + adversarial negatives and collect the hardest false-alarm clips."""
    model.eval()
    hard_parts: list[np.ndarray] = []
    total_found = 0
    with torch.no_grad():
        for name, arr in (("acav", acav_neg), ("gym", gym_neg), ("adv", adv_neg)):
            n = len(arr)
            found_src = 0
            for i in range(0, n, 4096):
                chunk = np.asarray(arr[i : i + 4096], dtype=np.float32)
                probs = model(torch.from_numpy(chunk)).squeeze(-1).numpy()
                mask = probs >= score_thr
                if np.any(mask):
                    hits = chunk[mask]
                    hard_parts.append(hits)
                    found_src += len(hits)
                    total_found += len(hits)
            print(f"    hard-mined {name}: {found_src:,} / {n:,} clips scoring >= {score_thr}")
    if not hard_parts:
        return np.zeros((0, 16, 96), dtype=np.float32)
    cat = np.concatenate(hard_parts, axis=0)
    if len(cat) > max_keep:
        rng = np.random.default_rng(123)
        cat = cat[rng.choice(len(cat), size=max_keep, replace=False)]
    print(f"    total mined hard-negative bank: {len(cat):,} clips")
    return cat


def train_and_export(phrase: str, work_dir: Path, out_onnx: Path, *, steps: int = 9000) -> dict[str, Any]:
    torch.manual_seed(42)
    np.random.seed(42)
    torch.set_num_threads(6)

    fe = build_oww_feature_extractor()
    synth_pos = np.load(work_dir / "synth_pos_features.npy").astype(np.float32)
    val_pos = np.load(work_dir / "val_pos_features.npy").astype(np.float32)
    adv_neg = np.load(work_dir / "adv_neg_features.npy").astype(np.float32)
    gym_neg = np.load(work_dir / "gym_neg_features.npy").astype(np.float32)
    user_pos = load_user_positives(work_dir, fe)

    acav_neg = np.load(NEG_DIR / "acav100m_subset.npy", mmap_mode="r")
    val_neg_2d = np.load(NEG_DIR / "validation_set_features.npy", mmap_mode="r")

    print(
        f"Training '{phrase}' MLP (2-stage hard-negative mining) | synth_pos={len(synth_pos)} user_pos={len(user_pos)} "
        f"adv_neg={len(adv_neg)} gym_neg={len(gym_neg)} acav_neg={len(acav_neg):,} val_pos={len(val_pos)}"
    )

    model = WakeWordMlp(hidden_dim=64, dropout=0.38)
    opt = torch.optim.AdamW(model.parameters(), lr=9.0e-4, weight_decay=2.0e-2)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=steps, eta_min=3e-5)
    rng = np.random.default_rng(42)

    hard_bank = np.zeros((0, 16, 96), dtype=np.float32)
    mine_steps = {int(steps * 0.50)}
    best_snapshots: list[tuple[float, dict[str, torch.Tensor]]] = []
    t0 = time.perf_counter()

    for step in range(1, steps + 1):
        if step in mine_steps:
            print(f"  [Hard-Negative Sweep @ step {step}]")
            hard_bank = mine_hard_negatives(model, acav_neg, gym_neg, adv_neg, score_thr=0.03, max_keep=18_000)

        model.train()
        progress = min(1.0, step / float(steps * 0.85))
        neg_weight = 8.0 + 160.0 * (progress ** 1.3)

        if len(user_pos) > 0:
            bx_pos = np.concatenate([
                user_pos[rng.integers(0, len(user_pos), size=48)],
                synth_pos[rng.integers(0, len(synth_pos), size=80)],
            ], axis=0)
        else:
            bx_pos = synth_pos[rng.integers(0, len(synth_pos), size=128)]

        acav_idx = rng.integers(0, len(acav_neg), size=1024)
        bx_acav = np.asarray(acav_neg[acav_idx], dtype=np.float32)
        bx_adv = adv_neg[rng.integers(0, len(adv_neg), size=256)]
        bx_gym = gym_neg[rng.integers(0, len(gym_neg), size=192)]

        # Regularization: mild feature jitter on positives + negatives prevents overfitting to training slices
        bx_pos = bx_pos + rng.normal(0.0, 0.04, size=bx_pos.shape).astype(np.float32)

        neg_blocks = [bx_acav, bx_adv, bx_gym]
        neg_w_blocks = [
            np.ones(len(bx_acav), dtype=np.float32) * (neg_weight * 0.9),
            np.ones(len(bx_adv), dtype=np.float32) * (neg_weight * 1.8),
            np.ones(len(bx_gym), dtype=np.float32) * (neg_weight * 1.8),
        ]
        if len(hard_bank) >= 32:
            bx_hard = hard_bank[rng.integers(0, len(hard_bank), size=192)]
            neg_blocks.append(bx_hard)
            neg_w_blocks.append(np.ones(len(bx_hard), dtype=np.float32) * (neg_weight * 2.5))

        bx_neg = np.concatenate(neg_blocks, axis=0)
        x_np = np.concatenate([bx_pos, bx_neg], axis=0)
        y_np = np.concatenate([
            np.ones(len(bx_pos), dtype=np.float32) * 0.985,
            np.zeros(len(bx_neg), dtype=np.float32),
        ])
        w_np = np.concatenate([np.ones(len(bx_pos), dtype=np.float32) * 3.0, *neg_w_blocks])

        x_t = torch.from_numpy(x_np)
        y_t = torch.from_numpy(y_np).unsqueeze(1)
        w_t = torch.from_numpy(w_np).unsqueeze(1)

        logits = model.logits(x_t)
        raw_loss = nn.functional.binary_cross_entropy_with_logits(logits, y_t, reduction="none")
        with torch.no_grad():
            probs = torch.sigmoid(logits)
            hard_boost = torch.where((y_t < 0.5) & (probs > 0.04), torch.tensor(10.0), torch.tensor(1.0))
        loss = (raw_loss * w_t * hard_boost).mean()

        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
        opt.step()
        sched.step()

        if step % 600 == 0 or step == steps:
            model.eval()
            with torch.no_grad():
                vp = model(torch.from_numpy(val_pos)).squeeze(-1).numpy()
                rec_50 = float(np.mean(vp >= 0.50))
                rec_38 = float(np.mean(vp >= 0.38))
                ap = model(torch.from_numpy(adv_neg[:1200])).squeeze(-1).numpy()
                adv_fp = float(np.mean(ap >= 0.50))
            fp_cnt, fph, v_max = eval_validation_fph(model, val_neg_2d, threshold=0.50)
            score = rec_50 - 1.1 * fph - 4.0 * adv_fp
            print(
                f"  step {step:4d}/{steps} ({time.perf_counter()-t0:4.1f}s) | loss={loss.item():.4f} "
                f"| unseen_recall@0.50={rec_50*100:.1f}% (@0.38={rec_38*100:.1f}%) "
                f"| val_10.7h_FP={fp_cnt} ({fph:.2f}/hr, max={v_max:.3f}) | adv_FP={adv_fp*100:.2f}%"
            )
            if step >= int(steps * 0.45):
                snap = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                best_snapshots.append((score, snap))
                best_snapshots.sort(key=lambda x: x[0], reverse=True)
                best_snapshots = best_snapshots[:3]

    # Average top snapshots (weight averaging stabilizes false positives!)
    if best_snapshots:
        avg_state: dict[str, torch.Tensor] = {}
        for k in best_snapshots[0][1].keys():
            avg_state[k] = torch.stack([s[1][k].float() for s in best_snapshots], dim=0).mean(dim=0)
        model.load_state_dict(avg_state)

    model.eval()
    with torch.no_grad():
        vp = model(torch.from_numpy(val_pos)).squeeze(-1).numpy()
        rec_50 = float(np.mean(vp >= 0.50))
        rec_38 = float(np.mean(vp >= 0.38))
    fp_cnt_50, fph_50, vmax = eval_validation_fph(model, val_neg_2d, threshold=0.50)
    fp_cnt_38, fph_38, _ = eval_validation_fph(model, val_neg_2d, threshold=0.38)
    print(
        f"\nFINAL AVERAGED MODEL: unseen_recall@0.50={rec_50*100:.1f}% (@0.38={rec_38*100:.1f}%) | "
        f"10.7h_val_FP@0.50={fp_cnt_50} ({fph_50:.2f}/hr) | @0.38={fp_cnt_38} ({fph_38:.2f}/hr, max={vmax:.3f})"
    )

    # Export clean ONNX opset 13 matching openWakeWord exact shape [1, 16, 96] -> [1, 1]
    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros((1, 16, 96), dtype=torch.float32)
    tmp_onnx = out_onnx.with_suffix(".onnx.tmp")
    torch.onnx.export(
        model,
        dummy,
        str(tmp_onnx),
        input_names=["x.1"],
        output_names=["prob"],
        opset_version=13,
        do_constant_folding=True,
        dynamo=False,
    )
    tmp_onnx.replace(out_onnx)
    print(f"Exported ONNX: {out_onnx} ({out_onnx.stat().st_size / 1024:.1f} KB)")

    metrics = {
        "phrase": phrase,
        "onnxPath": str(out_onnx),
        "unseenRecallAt050": round(rec_50, 4),
        "unseenRecallAt038": round(rec_38, 4),
        "val10hFpAt050": fp_cnt_50,
        "val10hFphAt050": round(fph_50, 3),
        "val10hFpAt038": fp_cnt_38,
        "val10hFphAt038": round(fph_38, 3),
        "userClipsUsed": int(len(user_pos)),
        "trainedAt": datetime.now().isoformat(timespec="seconds"),
    }
    (work_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def blind_gym_stress_test(phrase: str, onnx_path: Path) -> None:
    """Stress-test the exported ONNX inside live openwakeword.Model on untouched 2026-11-23 gym rock audio!"""
    from openwakeword.model import Model as OwwModel
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from room_voice import get_shared_kokoro

    mdl = OwwModel(
        wakeword_models=[str(onnx_path)],
        inference_framework="onnx",
        melspec_model_path=str(OWW_MODELS_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(OWW_MODELS_DIR / "embedding_model.onnx"),
    )
    key = list(mdl.models.keys())[0]

    # 1. Blind 2026-11-23 17.6-minute loud gym rock tape (100% unseen!)
    tape_dir = ROOT_DIR / "audio_tapes" / "2026-11-23"
    segs = []
    for p in sorted(tape_dir.glob("aud_*.m4a")):
        parts = p.stem.split("_")
        if len(parts) == 3:
            dt = datetime.fromtimestamp(int(parts[1]) / 1000.0)
            if dt.hour == 21 and 12 <= dt.minute <= 28:
                segs.append(p)
    rock_pcm = np.concatenate([load_audio_16k(p) for p in segs])
    rock_i16 = np.round(np.clip(rock_pcm, -1.0, 1.0) * 32767.0).astype(np.int16)

    mdl.reset()
    max_rock = 0.0
    fp_50, fp_38 = 0, 0
    cd50, cd38 = 0, 0
    for i in range(0, len(rock_i16) - 1280, 1280):
        sc = float(mdl.predict(rock_i16[i : i + 1280]).get(key, 0.0))
        if sc > max_rock:
            max_rock = sc
        if cd50 > 0:
            cd50 -= 1
        elif sc >= 0.50:
            fp_50 += 1
            cd50 = 18
        if cd38 > 0:
            cd38 -= 1
        elif sc >= 0.38:
            fp_38 += 1
            cd38 = 18
    print(f"\n=== BLIND GYM STRESS TEST ({onnx_path.name}) ===")
    print(f"  Untouched 2026-11-23 rock tape ({len(rock_pcm)/SR/60:.1f} min): max={max_rock:.3f} | FP@0.50={fp_50} | FP@0.38={fp_38}")

    # 2. Strictly held-out unseen speakers (am_onyx, am_echo, bm_george, af_bella) clean + mixed into 0 dB real rock!
    kokoro, _ = get_shared_kokoro(ROOT_DIR / "models")
    bed = rock_pcm[SR * 610 : SR * 625]  # middle of loud Set 3 rock song
    b_rms = float(np.sqrt(np.mean(bed * bed))) or 1e-4

    def score_phrase(txt: str, voice: str, mix_0db: bool = False) -> float:
        mdl.reset()
        pcm, sr = kokoro.create(txt, voice=voice, speed=1.0, lang="en-us")
        v = resample_16k(pcm, sr)
        pad = np.zeros(SR, dtype=np.float32)
        full = np.concatenate([pad * 0.5, v, pad])
        if mix_0db:
            v_rms = float(np.sqrt(np.mean(v * v))) or 1e-4
            bg = np.resize(bed, full.size) * (v_rms / b_rms)
            full = np.clip(full + bg, -0.98, 0.98)
        i16 = np.round(np.clip(full, -1.0, 1.0) * 32767.0).astype(np.int16)
        best = 0.0
        for j in range(0, len(i16) - 1280, 1280):
            s = float(mdl.predict(i16[j : j + 1280]).get(key, 0.0))
            if s > best:
                best = s
        return best

    unseen_vs = ("am_onyx", "am_echo", "bm_george", "af_bella")
    for label, txt, mix in (
        ("Unseen clean wake", f"{phrase}", False),
        ("Unseen clean + cmd", f"{phrase}, 225 for 8", False),
        ("Unseen 0dB rock wake", f"{phrase}", True),
        ("Unseen 0dB rock + cmd", f"{phrase}, record this", True),
        ("ADV: Hey Army", "Hey Army", False),
        ("ADV: Hey Harmony", "Hey Harmony", False),
        ("ADV: Hey Memory", "Hey Memory", False),
        ("ADV: Hey Buddy", "Hey Buddy", False),
        ("ADV: I'm already done", "I'm already done", False),
        ("ADV: Primary, record", "Primary, record this", False),
        ("ADV: Bare Armory", "Armory", False),
    ):
        scs = [score_phrase(txt, v, mix_0db=mix) for v in unseen_vs]
        print(f"  {label:24s}: " + ", ".join(f"{v}={s:.3f}" for v, s in zip(unseen_vs, scs)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Train custom openWakeWord ONNX wake word for The Armory")
    ap.add_argument("--phrase", default="hey armory", help="Wake phrase (default: 'hey armory')")
    ap.add_argument("--retrain-only", action="store_true", help="Reuse cached synthetic features + add user_positives/")
    ap.add_argument("--refresh-gym-neg", action="store_true", help="Re-mine real gym audio negative windows before training")
    ap.add_argument("--steps", type=int, default=9000, help="Training steps (default: 9000)")
    args = ap.parse_args()

    slug = re.sub(r"[^a-z0-9]+", "_", args.phrase.strip().lower()).strip("_") or "hey_armory"
    work_dir = TRAIN_ROOT / slug
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "user_positives").mkdir(parents=True, exist_ok=True)
    out_onnx = OWW_MODELS_DIR / f"{slug}_v0.1.onnx"

    if not args.retrain_only or not (work_dir / "synth_pos_features.npy").is_file():
        generate_datasets(args.phrase.strip(), work_dir)
    elif args.refresh_gym_neg:
        fe = build_oww_feature_extractor()
        loud_beds, ambient_beds = mine_gym_backgrounds(max_segments=180)
        gym_neg_emb = extract_sliding_embeddings(fe, loud_beds + ambient_beds, stride_frames=2)
        np.save(work_dir / "gym_neg_features.npy", gym_neg_emb.astype(np.float16))
        print(f"Refreshed gym_neg_features.npy: {gym_neg_emb.shape}")
    train_and_export(args.phrase.strip(), work_dir, out_onnx, steps=args.steps)
    blind_gym_stress_test(args.phrase.strip(), out_onnx)


if __name__ == "__main__":
    main()
