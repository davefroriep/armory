"""Render studio-grade 1080p audio-reactive 3-screen walk-in videos for The Armory.

Downloads (if needed) and trims real audio stems at musical bar boundaries,
computes 30 fps 32-band FFT + sub-bass/snare envelopes, and pipes razor-sharp
1920x1080 H.264 Main L4.0 (`-bf 0`) + 48 kHz AAC MP4s directly into
`S:\\ArmoryCam\\shows\\sources\\`.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import threading
import wave

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


FFMPEG_BIN = Path(r"C:\Users\davef\Tools\ffmpeg\bin\ffmpeg.exe")
SOURCES_DIR = Path(r"S:\ArmoryCam\shows\sources")
AUDIO_CACHE_DIR = SOURCES_DIR / "_audio_cache"

WIDTH = 1920
HEIGHT = 1080
FPS = 30
HALF_W = 480
HALF_H = 270
SR = 48_000

FONT_SANS_BOLD = Path(r"C:\Windows\Fonts\bahnschrift.ttf")
FONT_SERIF_BOLD = Path(r"C:\Windows\Fonts\georgiab.ttf")
FONT_UI_SEMI = Path(r"C:\Windows\Fonts\seguisb.ttf")


@dataclass(frozen=True)
class ThemeSpec:
    id: str
    out_filename: str
    raw_audio_name: str
    youtube_url: str
    start_sec: float
    end_sec: float | None
    fade_out_sec: float
    eyebrow: str
    headline: str
    footer: str
    bg_deep_bgr: tuple[int, int, int]
    bg_mid_bgr: tuple[int, int, int]
    primary_bgr: tuple[int, int, int]
    secondary_bgr: tuple[int, int, int]
    accent_bgr: tuple[int, int, int]
    headline_rgb: tuple[int, int, int]
    glow_rgb: tuple[int, int, int]
    style: str
    bpm: float
    use_serif: bool = False
    drop_sec: float = 5.0
    drum_teaser_sec: float = 0.0


THEMES: list[ThemeSpec] = [
    ThemeSpec(
        id="jenna",
        out_filename="jenna_walkin_video.mp4",
        raw_audio_name="jenna_raw.wav",
        youtube_url="https://www.youtube.com/watch?v=aOt4xRE05wQ",
        start_sec=0.15,
        end_sec=27.00,
        fade_out_sec=2.0,
        eyebrow="H E A D L I N I N G   T O N I G H T",
        headline="JENNA",
        footer="L I V E   A T   T H E   A R M O R Y",
        bg_deep_bgr=(28, 6, 20),
        bg_mid_bgr=(92, 18, 64),
        primary_bgr=(255, 65, 195),
        secondary_bgr=(45, 195, 255),
        accent_bgr=(240, 235, 255),
        headline_rgb=(255, 250, 255),
        glow_rgb=(215, 70, 255),
        style="rockstar",
        bpm=125.0,
        drop_sec=5.67,
    ),
    ThemeSpec(
        id="avenleigh",
        out_filename="avenleigh_walkin_video.mp4",
        raw_audio_name="avenleigh_raw.wav",
        youtube_url="https://www.youtube.com/shorts/ET3HB1xzx14",
        start_sec=26.00,
        end_sec=44.72,
        fade_out_sec=1.8,
        eyebrow="L I V E   O N   T H E   V I P   S T A G E",
        headline="AVENLEIGH",
        footer="T H E   A R M O R Y",
        bg_deep_bgr=(44, 8, 34),
        bg_mid_bgr=(125, 22, 118),
        primary_bgr=(165, 45, 255),
        secondary_bgr=(235, 175, 255),
        accent_bgr=(255, 245, 255),
        headline_rgb=(255, 246, 252),
        glow_rgb=(255, 75, 185),
        style="pink_pop",
        bpm=130.0,
        drop_sec=7.34,
    ),
    ThemeSpec(
        id="jared_mark_prism",
        out_filename="jared_mark_prism_video.mp4",
        raw_audio_name="whitney_raw.wav",
        youtube_url="https://www.youtube.com/watch?v=id2-K3daNRQ",
        start_sec=62.55,
        end_sec=98.70,
        fade_out_sec=2.2,
        eyebrow="G U E S T S   O F   H O N O R",
        headline="JARED & MARK",
        footer="L I V E   A T   T H E   A R M O R Y",
        bg_deep_bgr=(24, 14, 10),
        bg_mid_bgr=(64, 40, 28),
        primary_bgr=(255, 238, 210),
        secondary_bgr=(255, 170, 125),
        accent_bgr=(255, 255, 255),
        headline_rgb=(248, 252, 255),
        glow_rgb=(170, 225, 255),
        style="prism",
        bpm=119.0,
        drop_sec=8.00,
        drum_teaser_sec=6.74,
    ),
    ThemeSpec(
        id="jared_mark_vip",
        out_filename="jared_mark_vip_video.mp4",
        raw_audio_name="whitney_raw.wav",
        youtube_url="https://www.youtube.com/watch?v=id2-K3daNRQ",
        start_sec=62.55,
        end_sec=98.70,
        fade_out_sec=2.2,
        eyebrow="T H E   V I P s   H A V E   A R R I V E D",
        headline="JARED & MARK",
        footer="L I V E   A T   T H E   A R M O R Y",
        bg_deep_bgr=(36, 16, 8),
        bg_mid_bgr=(72, 42, 18),
        primary_bgr=(60, 195, 255),
        secondary_bgr=(195, 235, 255),
        accent_bgr=(230, 250, 255),
        headline_rgb=(255, 245, 215),
        glow_rgb=(255, 195, 75),
        style="velvet_gold",
        bpm=119.0,
        use_serif=True,
        drop_sec=8.00,
        drum_teaser_sec=6.74,
    ),
]


def _no_window_kwargs() -> dict:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def ensure_raw_audio(theme: ThemeSpec) -> Path:
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_wav = AUDIO_CACHE_DIR / theme.raw_audio_name
    if raw_wav.is_file() and raw_wav.stat().st_size > 10_000:
        return raw_wav
    import yt_dlp  # type: ignore

    stem = raw_wav.stem
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(AUDIO_CACHE_DIR / f"{stem}.%(ext)s"),
        "overwrites": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": str(FFMPEG_BIN.parent),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(theme.youtube_url, download=True)
    if not raw_wav.is_file():
        raise RuntimeError(f"Missing downloaded WAV: {raw_wav}")
    return raw_wav


def _xfade_stereo(a: np.ndarray, b: np.ndarray, xf_sec: float) -> np.ndarray:
    n = min(int(round(xf_sec * SR)), a.shape[0] // 2, b.shape[0] // 2)
    if n <= 1:
        return np.vstack([a, b]).astype(np.float32)
    t = np.linspace(0.0, math.pi * 0.5, n, dtype=np.float32)[:, None]
    mid = a[-n:] * np.cos(t) + b[:n] * np.sin(t)
    return np.vstack([a[:-n], mid, b[n:]]).astype(np.float32)


def prepare_trimmed_wav(theme: ThemeSpec) -> tuple[Path, np.ndarray, float]:
    raw_wav = ensure_raw_audio(theme)
    trimmed_wav = AUDIO_CACHE_DIR / f"{theme.id}_trimmed.wav"

    with wave.open(str(raw_wav), "rb") as wf:
        in_sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw_bytes = wf.readframes(wf.getnframes())

    if sw != 2:
        raise RuntimeError(f"Expected 16-bit PCM in {raw_wav}, got sample width {sw}")
    pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 1:
        pcm = np.stack([pcm, pcm], axis=1)
    else:
        pcm = pcm.reshape(-1, ch)[:, :2]

    if in_sr != SR:
        from scipy.signal import resample_poly

        g = math.gcd(in_sr, SR)
        pcm = resample_poly(pcm, SR // g, in_sr // g, axis=0).astype(np.float32)

    fi_8ms = min(pcm.shape[0] // 8, max(2, int(round(0.008 * SR))))
    if theme.id == "jenna":
        # 3-bar "Music make you lose control" buildup -> exact "LET'S GO!" beat drop at 5.67s -> clean exit at 27.00s
        lp = pcm[int(0.15 * SR):int(2.05 * SR)].copy()
        lp[:fi_8ms] *= np.linspace(0.0, 1.0, fi_8ms, dtype=np.float32)[:, None]
        main = pcm[int(0.15 * SR):int(27.00 * SR)].copy()
        clip = _xfade_stereo(lp, main, 0.030)
    elif theme.id == "avenleigh":
        # Clean 7.34s synth riser buildup -> "LA LA LA" laser chorus drop repeated cleanly twice
        b1 = pcm[int(26.00 * SR):int(27.85 * SR)].copy()
        b1[:fi_8ms] *= np.linspace(0.0, 1.0, fi_8ms, dtype=np.float32)[:, None]
        main = pcm[int(26.00 * SR):int(44.45 * SR)].copy()
        rep = pcm[int(29.70 * SR):].copy()
        clip = _xfade_stereo(_xfade_stereo(_xfade_stereo(b1, b1.copy(), 0.030), main, 0.030), rep, 0.045)
    elif theme.id in ("jared_mark_prism", "jared_mark_vip"):
        # Bar-1 sparkle -> Bars 1+2 claps -> iconic "duh-duh-duh-DUM" drum fill (6.74..8.00s) -> "OH! I Wanna Dance With Somebody!"
        b1 = pcm[int(0.75 * SR):int(2.77 * SR)].copy()
        b1[:fi_8ms] *= np.linspace(0.0, 1.0, fi_8ms, dtype=np.float32)[:, None]
        b12 = pcm[int(0.75 * SR):int(5.58 * SR)].copy()
        drop_hook = pcm[int(62.55 * SR):int(98.70 * SR)].copy()
        clip = _xfade_stereo(_xfade_stereo(b1, b12, 0.050), drop_hook, 0.060)
    else:
        i0 = max(0, min(pcm.shape[0] - 1, int(round(theme.start_sec * SR))))
        i1 = pcm.shape[0] if theme.end_sec is None else min(pcm.shape[0], int(round(theme.end_sec * SR)))
        clip = pcm[i0:i1].copy()
        fi_n = min(clip.shape[0] // 4, int(round(0.015 * SR)))
        if fi_n > 2:
            clip[:fi_n] *= np.linspace(0.0, 1.0, fi_n, dtype=np.float32)[:, None]

    dur_sec = float(clip.shape[0]) / float(SR)

    rms = float(np.sqrt(np.mean(clip ** 2))) if clip.size else 0.0
    if rms > 1e-4:
        clip = clip * min(0.22 / rms, 2.5)
    pk = float(np.max(np.abs(clip))) if clip.size else 0.0
    if pk > 0.92:
        clip = clip * (0.92 / pk)
    clip = clip.astype(np.float32)

    fo_n = min(clip.shape[0] // 2, int(round(theme.fade_out_sec * SR)))
    if fo_n > 8:
        t_fo = np.linspace(0.0, math.pi * 0.5, fo_n, dtype=np.float32)[:, None]
        clip[-fo_n:] *= np.cos(t_fo) ** 2

    int16_pcm = np.clip(np.round(clip * 32767.0), -32767, 32767).astype("<i2")
    with wave.open(str(trimmed_wav), "wb") as wf_out:
        wf_out.setnchannels(2)
        wf_out.setsampwidth(2)
        wf_out.setframerate(SR)
        wf_out.writeframes(int16_pcm.tobytes())

    return trimmed_wav, clip, dur_sec


def analyze_audio_features(pcm_stereo: np.ndarray, n_frames: int) -> dict[str, np.ndarray]:
    """Compute per-frame 32-band FFT spectrum, sub-bass kick, snare spikes, and chorus swell."""
    mono = pcm_stereo.mean(axis=1)
    n_samples = len(mono)
    fft_n = 2048
    half_fft = fft_n // 2 + 1
    win = np.hanning(fft_n).astype(np.float32)
    freqs = np.fft.rfftfreq(fft_n, d=1.0 / SR)

    edges = np.geomspace(48.0, 11_000.0, 33)
    band_masks = [(freqs >= edges[b]) & (freqs < edges[b + 1]) for b in range(32)]
    for b, m in enumerate(band_masks):
        if not np.any(m):
            idx = int(np.argmin(np.abs(freqs - 0.5 * (edges[b] + edges[b + 1]))))
            m[idx] = True

    kick_mask = (freqs >= 42.0) & (freqs <= 135.0)
    snare_mask = (freqs >= 1900.0) & (freqs <= 6200.0)

    raw_bands = np.zeros((n_frames, 32), dtype=np.float32)
    raw_kick = np.zeros(n_frames, dtype=np.float32)
    raw_snare = np.zeros(n_frames, dtype=np.float32)
    hop = float(SR) / float(FPS)

    for f in range(n_frames):
        center = int(round((f + 0.5) * hop))
        s0 = center - fft_n // 2
        seg = np.zeros(fft_n, dtype=np.float32)
        src0 = max(0, s0)
        src1 = min(n_samples, s0 + fft_n)
        dst0 = src0 - s0
        if src1 > src0:
            seg[dst0 : dst0 + (src1 - src0)] = mono[src0:src1]
        mag = np.abs(np.fft.rfft(seg * win)) / float(fft_n)
        raw_kick[f] = float(np.sqrt(np.mean(mag[kick_mask] ** 2)))
        raw_snare[f] = float(np.sqrt(np.mean(mag[snare_mask] ** 2)))
        for b in range(32):
            raw_bands[f, b] = float(np.sqrt(np.mean(mag[band_masks[b]] ** 2)))

    # Compressing log-power perceptually and normalizing cleanly per band
    bands = np.log1p(raw_bands * 420.0)
    p96 = np.percentile(bands, 97, axis=0, keepdims=True)
    bands = np.clip(bands / np.maximum(p96, 1e-4), 0.0, 1.0)

    smoothed = np.zeros_like(bands)
    cur = np.zeros(32, dtype=np.float32)
    for f in range(n_frames):
        target = bands[f]
        rise = target > cur
        cur = np.where(rise, cur * 0.35 + target * 0.65, cur * 0.78 + target * 0.22)
        smoothed[f] = cur

    kick_diff = np.maximum(0.0, raw_kick - np.concatenate([[raw_kick[0]], raw_kick[:-1]]) * 0.72)
    kick_n = np.clip(kick_diff / max(1e-4, float(np.percentile(kick_diff, 96))), 0.0, 1.0)
    kick_env = np.zeros(n_frames, dtype=np.float32)
    kv = 0.0
    for f in range(n_frames):
        kv = max(float(kick_n[f]), kv * 0.74)
        kick_env[f] = kv

    snare_diff = np.maximum(0.0, raw_snare - np.concatenate([[raw_snare[0]], raw_snare[:-1]]) * 0.75)
    snare_n = np.clip(snare_diff / max(1e-4, float(np.percentile(snare_diff, 97))), 0.0, 1.0)
    snare_env = np.zeros(n_frames, dtype=np.float32)
    sv = 0.0
    for f in range(n_frames):
        sv = max(float(snare_n[f]), sv * 0.70)
        snare_env[f] = sv

    rms_per_frame = smoothed.mean(axis=1)
    k_win = int(FPS * 1.4)
    kernel = np.ones(k_win, dtype=np.float32) / float(k_win)
    chorus_raw = np.convolve(rms_per_frame, kernel, mode="same")
    c_lo, c_hi = float(np.percentile(chorus_raw, 15)), float(np.percentile(chorus_raw, 96))
    chorus_env = np.clip((chorus_raw - c_lo) / max(0.05, c_hi - c_lo), 0.0, 1.0)

    return {
        "bands": smoothed,
        "kick": kick_env,
        "snare": snare_env,
        "chorus": chorus_env,
    }


def _fit_font(path: Path, text: str, max_width: int, start_size: int) -> ImageFont.FreeTypeFont:
    size = start_size
    dummy = ImageDraw.Draw(Image.new("RGBA", (32, 32)))
    while size >= 44:
        font = ImageFont.truetype(str(path), size)
        box = dummy.textbbox((0, 0), text, font=font)
        if (box[2] - box[0]) <= max_width:
            return font
        size -= 6
    return ImageFont.truetype(str(path), 44)


def build_typography_plate(theme: ThemeSpec, *, include_headline: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Render supersampled 1920x1080 BGRA nameplate with dark vignette & neon/gold halo."""
    # Contrast backing vignette centered around text
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float32)
    dx = (xx - WIDTH * 0.5) / (WIDTH * 0.44)
    dy = (yy - HEIGHT * 0.46) / (HEIGHT * 0.30)
    vig_alpha = np.clip(0.62 * np.exp(-(dx * dx + dy * dy) * 1.15), 0.0, 0.65)
    plate_pil = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    vig_img = Image.new("RGBA", (WIDTH, HEIGHT), (4, 4, 8, 0))
    vig_a = Image.fromarray(np.clip(vig_alpha * 255.0, 0, 255).astype(np.uint8), mode="L")
    vig_img.putalpha(vig_a)
    plate_pil.alpha_composite(vig_img)

    # Outer glow layer
    glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    dg = ImageDraw.Draw(glow_layer)
    f_head_path = FONT_SERIF_BOLD if theme.use_serif else FONT_SANS_BOLD
    f_head = _fit_font(f_head_path, theme.headline, 1480, 178)
    f_eye = ImageFont.truetype(str(FONT_UI_SEMI), 30)
    f_foot = ImageFont.truetype(str(FONT_UI_SEMI), 32)

    cx = WIDTH // 2
    cy_head = 468
    cy_eye = 328
    cy_foot = 602

    gr, gg, gb = theme.glow_rgb
    if include_headline:
        dg.text((cx, cy_head), theme.headline, font=f_head, fill=(gr, gg, gb, 215), anchor="mm")
        glow_blur = glow_layer.filter(ImageFilter.GaussianBlur(radius=22))
        plate_pil.alpha_composite(glow_blur)
        plate_pil.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(radius=6)))

    # Crisp foreground typography + subtle hairline framing rules
    fg = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    df = ImageDraw.Draw(fg)

    # Thin gold/colored horizontal accent rules flanking eyebrow
    eb_box = df.textbbox((cx, cy_eye), theme.eyebrow, font=f_eye, anchor="mm")
    rule_col = (gr, gg, gb, 210)
    pad_r = 26
    rule_w = 110
    df.line([(eb_box[0] - pad_r - rule_w, cy_eye), (eb_box[0] - pad_r, cy_eye)], fill=rule_col, width=3)
    df.line([(eb_box[2] + pad_r, cy_eye), (eb_box[2] + pad_r + rule_w, cy_eye)], fill=rule_col, width=3)

    # Drop shadow then sharp fill
    df.text((cx, cy_eye + 2), theme.eyebrow, font=f_eye, fill=(0, 0, 0, 190), anchor="mm")
    df.text((cx, cy_eye), theme.eyebrow, font=f_eye, fill=(242, 244, 250, 238), anchor="mm")

    if include_headline:
        hr, hg, hb = theme.headline_rgb
        df.text((cx, cy_head + 6), theme.headline, font=f_head, fill=(0, 0, 0, 215), anchor="mm")
        df.text((cx, cy_head), theme.headline, font=f_head, fill=(hr, hg, hb, 255), anchor="mm")

    df.text((cx, cy_foot + 2), theme.footer, font=f_foot, fill=(0, 0, 0, 190), anchor="mm")
    df.text((cx, cy_foot), theme.footer, font=f_foot, fill=(gr, gg, gb, 240), anchor="mm")

    plate_pil.alpha_composite(fg)

    rgba = np.array(plate_pil, dtype=np.uint8)
    bgr = rgba[:, :, [2, 1, 0]].astype(np.uint16)
    alpha = rgba[:, :, 3:4].astype(np.uint16)
    fg_prem = (bgr * alpha) // 255
    inv_alpha = 255 - alpha
    return fg_prem, inv_alpha


def _hsv_bgr_table() -> np.ndarray:
    lut = np.zeros((256, 3), dtype=np.float32)
    for h in range(256):
        c = cv2.cvtColor(np.array([[[int(h * 180 / 256), 215, 255]]], dtype=np.uint8), cv2.COLOR_HSV2BGR)[0, 0]
        lut[h] = c.astype(np.float32) / 255.0
    return lut


class ParticleField:
    """Vectorized particle & firework effects synced cleanly to music dynamics."""

    def __init__(self, theme: ThemeSpec, seed: int = 42) -> None:
        self.theme = theme
        self.rng = np.random.default_rng(seed)
        self.n = 95
        self.x = self.rng.uniform(0, WIDTH, self.n).astype(np.float32)
        self.y = self.rng.uniform(0, HEIGHT, self.n).astype(np.float32)
        self.size = self.rng.uniform(2.2, 7.5 if theme.style in ("prism", "velvet_gold") else 5.0, self.n).astype(np.float32)
        self.phase = self.rng.uniform(0, 6.28, self.n).astype(np.float32)
        if theme.style == "pink_pop":
            self.vx = self.rng.uniform(-1.2, 1.2, self.n).astype(np.float32)
            self.vy = self.rng.uniform(1.1, 3.0, self.n).astype(np.float32)
        elif theme.style == "prism":
            self.vx = self.rng.uniform(0.35, 1.25, self.n).astype(np.float32)
            self.vy = self.rng.uniform(-0.8, 0.35, self.n).astype(np.float32)
        else:
            self.vx = self.rng.uniform(-0.8, 0.8, self.n).astype(np.float32)
            self.vy = self.rng.uniform(-2.6, -0.9, self.n).astype(np.float32)

        # Firework shells for Avenleigh chorus explosions
        self.fw_shells: list[dict] = []
        self.last_fw_frame = -20

    def step_and_draw(self, layer: np.ndarray, f: int, t: float, kick: float, snare: float, chorus: float) -> None:
        speed_mul = 1.0 + 1.15 * kick
        self.x = (self.x + self.vx * speed_mul) % WIDTH
        self.y = (self.y + self.vy * speed_mul) % HEIGHT

        st = self.theme.style
        if st == "rockstar":
            for i in range(self.n):
                ix, iy = int(self.x[i]), int(self.y[i])
                tw = 0.55 + 0.45 * math.sin(t * 4.5 + float(self.phase[i]))
                col = self.theme.secondary_bgr if (i % 3 != 0) else self.theme.primary_bgr
                c = tuple(int(v * tw) for v in col)
                r = max(2, int(self.size[i] * (0.9 + 0.35 * kick)))
                cv2.circle(layer, (ix, iy), r, c, -1, cv2.LINE_AA)
        elif st == "pink_pop":
            for i in range(self.n):
                ix, iy = int(self.x[i]), int(self.y[i])
                tw = 0.5 + 0.5 * math.sin(t * 6.0 + float(self.phase[i]))
                if i % 4 == 0:
                    # 4-point diamond star twinkle
                    arm = int(self.size[i] * (1.2 + 0.8 * tw))
                    c = tuple(int(v * tw) for v in self.theme.accent_bgr)
                    cv2.line(layer, (ix - arm, iy), (ix + arm, iy), c, 2, cv2.LINE_AA)
                    cv2.line(layer, (ix, iy - arm), (ix, iy + arm), c, 2, cv2.LINE_AA)
                else:
                    col = self.theme.primary_bgr if (i % 2 == 0) else self.theme.secondary_bgr
                    c = tuple(int(v * (0.6 + 0.4 * tw)) for v in col)
                    cv2.circle(layer, (ix, iy), max(2, int(self.size[i])), c, -1, cv2.LINE_AA)

            # Pink firework blooms during energetic drops / chorus
            if (chorus > 0.48 or kick > 0.78) and (f - self.last_fw_frame >= 16) and len(self.fw_shells) < 4:
                self.last_fw_frame = f
                fx = float(self.rng.uniform(260, WIDTH - 260))
                fy = float(self.rng.uniform(140, 420))
                ang = np.linspace(0, 2.0 * math.pi, 26, endpoint=False) + self.rng.uniform(0, 0.3)
                spd = self.rng.uniform(4.5, 9.5, 26)
                is_white = bool(self.rng.integers(0, 2))
                self.fw_shells.append({
                    "x": fx,
                    "y": fy,
                    "ang": ang,
                    "spd": spd,
                    "age": 0,
                    "max_age": 28,
                    "col": (245, 225, 255) if is_white else (180, 95, 255),
                })

            alive: list[dict] = []
            for sh in self.fw_shells:
                age = sh["age"]
                if age < sh["max_age"]:
                    prog = float(age) / float(sh["max_age"])
                    alpha = max(0.0, 1.0 - prog ** 1.3)
                    dist = (1.0 - (1.0 - prog) ** 2.2) * 185.0
                    c_sp = tuple(int(v * alpha) for v in sh["col"])
                    for k_i in range(len(sh["ang"])):
                        a = float(sh["ang"][k_i])
                        r_d = dist * (float(sh["spd"][k_i]) / 7.5)
                        px = int(sh["x"] + math.cos(a) * r_d)
                        py = int(sh["y"] + math.sin(a) * r_d + prog * prog * 45.0)
                        px0 = int(sh["x"] + math.cos(a) * max(0.0, r_d - 22.0))
                        py0 = int(sh["y"] + math.sin(a) * max(0.0, r_d - 22.0) + max(0.0, prog - 0.1) ** 2 * 45.0)
                        cv2.line(layer, (px0, py0), (px, py), c_sp, 2, cv2.LINE_AA)
                        cv2.circle(layer, (px, py), 3, c_sp, -1, cv2.LINE_AA)
                    sh["age"] += 1
                    alive.append(sh)
            self.fw_shells = alive

        elif st == "prism":
            prism_cols = [
                (255, 235, 190),
                (245, 180, 255),
                (170, 245, 255),
                (255, 255, 255),
            ]
            for i in range(self.n // 2):
                ix, iy = int(self.x[i]), int(self.y[i])
                tw = 0.45 + 0.45 * math.sin(t * 2.2 + float(self.phase[i]))
                col = prism_cols[i % len(prism_cols)]
                c = tuple(int(v * tw * 0.72) for v in col)
                cv2.circle(layer, (ix, iy), max(4, int(self.size[i] * 1.6)), c, -1, cv2.LINE_AA)

        else:  # velvet_gold
            for i in range(self.n):
                ix, iy = int(self.x[i]), int(self.y[i])
                tw = 0.55 + 0.45 * math.sin(t * 3.8 + float(self.phase[i]))
                col = self.theme.primary_bgr if (i % 3 != 0) else self.theme.secondary_bgr
                c = tuple(int(v * tw * (0.75 + 0.25 * snare)) for v in col)
                cv2.circle(layer, (ix, iy), max(2, int(self.size[i])), c, -1, cv2.LINE_AA)
            # Champagne diamond glints on snare hits
            if snare > 0.55:
                for g_i in range(4):
                    gx = int((self.x[(f * 7 + g_i * 19) % self.n]))
                    gy = int((self.y[(f * 11 + g_i * 23) % self.n]))
                    arm = int(16 + 22 * snare)
                    gc = (210, 242, 255)
                    cv2.line(layer, (gx - arm, gy), (gx + arm, gy), gc, 2, cv2.LINE_AA)
                    cv2.line(layer, (gx, gy - arm), (gx, gy + arm), gc, 2, cv2.LINE_AA)


def render_theme_video(theme: ThemeSpec) -> Path:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    out_mp4 = SOURCES_DIR / theme.out_filename
    tmp_mp4 = out_mp4.with_name(f".{out_mp4.stem}.tmp{out_mp4.suffix}")

    trimmed_wav, pcm_stereo, dur_sec = prepare_trimmed_wav(theme)
    n_frames = max(30, int(math.floor(dur_sec * FPS)))
    feats = analyze_audio_features(pcm_stereo, n_frames)
    bands_arr = feats["bands"]
    kick_arr = feats["kick"]
    snare_arr = feats["snare"]
    chorus_arr = feats["chorus"]

    fg_pre, inv_pre = build_typography_plate(theme, include_headline=False)
    fg_prem, inv_alpha = build_typography_plate(theme, include_headline=True)
    if len(theme.headline) > 10:
        halo_mult1 = 2.58 if theme.use_serif else 2.25
        halo_mult2 = 2.66 if theme.use_serif else 2.33
    else:
        halo_mult1 = 1.75
        halo_mult2 = 1.83
    particles = ParticleField(theme, seed=abs(hash(theme.id)) % 10_000)

    # Half-res grid for silky gradients and sweeping volumetric beams
    hy, hx = np.mgrid[0:HALF_H, 0:HALF_W].astype(np.float32)
    nx = (hx - HALF_W * 0.5) / (HALF_W * 0.5)
    ny = (hy - HALF_H * 0.46) / (HALF_H * 0.54)
    rad2 = nx * nx + ny * ny
    rad_glow_mask = np.exp(-rad2 * 1.45).astype(np.float32)

    bg_deep = np.array(theme.bg_deep_bgr, dtype=np.float32)
    bg_mid = np.array(theme.bg_mid_bgr, dtype=np.float32)
    p_col = np.array(theme.primary_bgr, dtype=np.float32)
    s_col = np.array(theme.secondary_bgr, dtype=np.float32)
    base_bg = bg_deep[None, None, :] * (1.0 - 0.8 * rad_glow_mask[:, :, None]) + bg_mid[None, None, :] * (
        0.85 * rad_glow_mask[:, :, None]
    )

    hsv_lut = _hsv_bgr_table()

    cmd = [
        str(FFMPEG_BIN),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        "-i",
        str(trimmed_wav),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-profile:v",
        "main",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-g",
        str(FPS * 2),
        "-bf",
        "0",
        "-video_track_timescale",
        "90000",
        "-crf",
        "20",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(SR),
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(tmp_mp4),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **_no_window_kwargs(),
    )
    assert proc.stdin is not None

    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        # Drain stderr concurrently to prevent the Windows 4 KB pipe deadlock while writing stdin.
        pipe = proc.stderr
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    peak_caps = np.zeros(32, dtype=np.float32)
    # 64 symmetric mirrored EQ bars across the wide lower stage
    n_half = 32
    bar_gap = 4
    bar_w = 18
    cx_stage = WIDTH // 2
    y_eq = 918

    try:
        print(f"START {theme.id}: {n_frames} frames ({dur_sec:.2f}s, drop@{theme.drop_sec:.2f}s)", flush=True)
        for f in range(n_frames):
            t = float(f) / float(FPS)
            is_dropped = t >= theme.drop_sec
            pre_prog = min(1.0, max(0.0, t / max(0.5, theme.drop_sec)))
            drum_pulse = 0.0
            if (not is_dropped) and theme.drum_teaser_sec > 0.0 and t >= theme.drum_teaser_sec:
                dt_fill = t - theme.drum_teaser_sec
                beat_pos = (dt_fill * 3.6) % 1.0
                drum_pulse = max(0.0, 1.0 - beat_pos * 2.2)

            kick = float(kick_arr[f])
            snare = float(snare_arr[f])
            chorus = float(chorus_arr[f])
            bands = bands_arr[f]

            # 1. Half-res volumetric stage backdrop + sweeping beam lobes
            bg_half = base_bg.copy()
            if is_dropped:
                pulse_boost = 0.20 + 0.55 * kick + 0.25 * chorus
            else:
                bg_half *= (0.45 + 0.45 * pre_prog)
                pulse_boost = 0.06 + 0.18 * pre_prog + 0.45 * drum_pulse
            bg_half += p_col[None, None, :] * (rad_glow_mask[:, :, None] * pulse_boost * 0.45)

            beam_half = np.zeros((HALF_H, HALF_W, 3), dtype=np.float32)
            if theme.style == "pink_pop":
                # 130 BPM disco-ball rotating rays from top center
                beat_rad = t * (theme.bpm / 60.0) * 0.65
                for k_r in range(6):
                    ang = -0.85 + 0.34 * k_r + 0.14 * math.sin(beat_rad + k_r * 0.9)
                    x_bot = int(HALF_W * 0.5 + math.tan(ang) * HALF_H * 1.15)
                    w_bot = 36
                    pts = np.array(
                        [[HALF_W // 2 - 12, -10], [HALF_W // 2 + 12, -10], [x_bot + w_bot, HALF_H + 10], [x_bot - w_bot, HALF_H + 10]],
                        dtype=np.int32,
                    )
                    c_ray = p_col if (k_r % 2 == 0) else s_col
                    cv2.fillConvexPoly(beam_half, pts, tuple(float(v) * (0.24 + 0.22 * kick) for v in c_ray))
            elif theme.style == "prism":
                # Clean architectural diagonal glass-prism refraction bands
                for k_r in range(4):
                    sweep = (t * 0.16 + k_r * 0.25) % 1.0
                    x_top = int(-120 + sweep * (HALF_W + 240))
                    pts = np.array(
                        [[x_top - 28, 0], [x_top + 28, 0], [x_top + 145, HALF_H], [x_top + 85, HALF_H]],
                        dtype=np.int32,
                    )
                    hue_idx = int(((k_r * 46) + int(t * 24)) % 256)
                    c_prism = hsv_lut[hue_idx] * 255.0
                    cv2.fillConvexPoly(beam_half, pts, tuple(float(v) * (0.18 + 0.16 * chorus) for v in c_prism))
            else:
                # Dual sweeping top-corner arena spotlights
                for side, sign, col_b in ((0, 1, p_col), (1, -1, s_col)):
                    sx = 45 if side == 0 else (HALF_W - 45)
                    sw_target = int(HALF_W * 0.5 + sign * 110 * math.sin(t * 0.72 + side * 1.6))
                    pts = np.array(
                        [[sx - 10, -8], [sx + 10, -8], [sw_target + 88, HALF_H + 8], [sw_target - 88, HALF_H + 8]],
                        dtype=np.int32,
                    )
                    cv2.fillConvexPoly(beam_half, pts, tuple(float(v) * (0.30 + 0.28 * kick) for v in col_b))

            if not is_dropped:
                beam_half *= (0.26 + 0.34 * pre_prog + 0.65 * drum_pulse)

            beam_half = cv2.GaussianBlur(beam_half, (0, 0), sigmaX=12.0, sigmaY=12.0)
            comb_half = np.clip(bg_half + beam_half, 0, 255).astype(np.uint8)
            frame = cv2.resize(comb_half, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)

            # 2. Crisp 1080p additive glow layer (halo ring + post-drop spectrum bars + particles)
            fx_layer = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)

            # Audio-reactive central spotlight halo ring (wider for long headlines like JARED & MARK)
            if is_dropped:
                halo_r = int(282 + 28 * kick + 14 * chorus)
                halo_col1 = tuple(int(v * (0.55 + 0.45 * kick)) for v in theme.primary_bgr)
                halo_col2 = tuple(int(v * (0.40 + 0.45 * snare)) for v in theme.secondary_bgr)
            else:
                halo_r = int(235 + 47 * pre_prog + 18 * drum_pulse)
                k_pre = 0.25 + 0.40 * pre_prog + 0.55 * drum_pulse
                halo_col1 = tuple(min(255, int(v * k_pre)) for v in theme.primary_bgr)
                halo_col2 = tuple(min(255, int(v * (k_pre * 0.85))) for v in theme.secondary_bgr)
            cv2.ellipse(fx_layer, (cx_stage, 468), (int(halo_r * halo_mult1), int(halo_r * 0.78)), 0, 0, 360, halo_col1, 4, cv2.LINE_AA)
            cv2.ellipse(fx_layer, (cx_stage, 468), (int(halo_r * halo_mult2), int(halo_r * 0.83)), 0, 0, 360, halo_col2, 2, cv2.LINE_AA)

            if is_dropped:
                # Mirrored 32-band symmetric equalizer + reflection + peak caps
                peak_caps = np.maximum(bands, peak_caps * 0.945 - 0.004)
                step_x = bar_w + bar_gap
                for b in range(n_half):
                    val = float(bands[b])
                    cap = float(peak_caps[b])
                    h_px = max(6, int(val * 195.0))
                    cap_y = y_eq - max(8, int(cap * 195.0)) - 6
                    frac = float(b) / float(n_half - 1)
                    if theme.style == "prism":
                        col_rgb = hsv_lut[int((b * 6 + int(t * 28)) % 256)] * (195.0 + 60.0 * val)
                        b_col = (int(col_rgb[0]), int(col_rgb[1]), int(col_rgb[2]))
                    else:
                        b_col = (
                            int(theme.primary_bgr[0] * (1.0 - frac) + theme.secondary_bgr[0] * frac),
                            int(theme.primary_bgr[1] * (1.0 - frac) + theme.secondary_bgr[1] * frac),
                            int(theme.primary_bgr[2] * (1.0 - frac) + theme.secondary_bgr[2] * frac),
                        )
                    r_col = tuple(int(c * 0.28) for c in b_col)
                    cap_col = theme.accent_bgr

                    for dir_sign in (-1, 1):
                        x_mid = cx_stage + dir_sign * ((b * step_x) + step_x // 2)
                        x0 = x_mid - bar_w // 2
                        x1 = x_mid + bar_w // 2
                        cv2.rectangle(fx_layer, (x0, y_eq - h_px), (x1, y_eq), b_col, -1)
                        # Peak cap
                        cv2.rectangle(fx_layer, (x0, cap_y), (x1, cap_y + 3), cap_col, -1)
                        # Stage floor reflection
                        ref_h = max(4, int(h_px * 0.34))
                        cv2.rectangle(fx_layer, (x0, y_eq + 6), (x1, y_eq + 6 + ref_h), r_col, -1)

                # Particles / Firework blooms
                particles.step_and_draw(fx_layer, f, t, kick, snare, chorus)

            # Soft glow pass + crisp add
            soft_glow = cv2.GaussianBlur(fx_layer, (0, 0), sigmaX=7.0, sigmaY=7.0)
            frame = cv2.add(frame, cv2.addWeighted(fx_layer, 0.92, soft_glow, 0.55, 0))

            # 3. Composite pre-rendered razor-sharp PIL typography plate (pre-drop eyebrow/footer -> post-drop hero)
            bg_u16 = frame.astype(np.uint16)
            if is_dropped:
                frame = np.clip((bg_u16 * inv_alpha) // 255 + fg_prem, 0, 255).astype(np.uint8)
                dt_drop = t - theme.drop_sec
                if 0.0 <= dt_drop < 0.22:
                    flash_k = ((1.0 - dt_drop / 0.22) ** 2) * 0.48
                    flash_col = np.array(
                        [220.0 + 35.0 * theme.primary_bgr[c] / 255.0 for c in range(3)],
                        dtype=np.float32,
                    )
                    frame = np.clip(frame.astype(np.float32) + flash_col[None, None, :] * flash_k, 0, 255).astype(np.uint8)
            else:
                frame = np.clip((bg_u16 * inv_pre) // 255 + fg_pre, 0, 255).astype(np.uint8)

            # 4. Gentle entrance fade-in (0.35s) and show tail fade-out (1.0s)
            if t < 0.35:
                k_fade = t / 0.35
                frame = (frame.astype(np.float32) * k_fade).astype(np.uint8)
            elif (dur_sec - t) < 1.0:
                k_fade = max(0.0, (dur_sec - t) / 1.0)
                frame = (frame.astype(np.float32) * k_fade).astype(np.uint8)

            proc.stdin.write(frame.tobytes())
            if (f + 1) % 300 == 0 or (f + 1) == n_frames:
                print(f"  {theme.id}: {f + 1}/{n_frames}", flush=True)

        proc.stdin.close()
        rc = proc.wait(timeout=180)
        err_thread.join(timeout=5)
        stderr_bytes = b"".join(stderr_chunks)
        if rc != 0 or not tmp_mp4.is_file() or tmp_mp4.stat().st_size < 10_000:
            err = stderr_bytes.decode("utf-8", errors="ignore")[-400:]
            raise RuntimeError(f"FFmpeg encode failed ({theme.id}): {err}")
        tmp_mp4.replace(out_mp4)
        print(f"OK {theme.id}: {out_mp4} ({out_mp4.stat().st_size / (1024*1024):.2f} MB, {dur_sec:.2f}s)", flush=True)
        return out_mp4
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        err_thread.join(timeout=2)
        if tmp_mp4.is_file():
            try:
                tmp_mp4.unlink()
            except Exception:
                pass


def main() -> None:
    for theme in THEMES:
        render_theme_video(theme)


if __name__ == "__main__":
    main()
