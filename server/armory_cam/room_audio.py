"""Rolling 14-day PowerConf S330 room audio capture and on-demand window extraction.

Keeps saved workout clips silent by default (`-an`) while buffering PowerConf S330
room microphone audio into 10s mono AAC chunks (`audio_tapes/YYYY-MM-DD/aud_<startMs>_<endMs>.m4a`)
and a 35s RAM tail ring for sub-second clip responsiveness.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import logging
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid
import wave

import numpy as np


_SEG_NAME_RE = re.compile(r"^(aud|ref)_(\d+)_(\d+)\.m4a$")


class RoomAudioRecorder:
    """Thread-safe rolling room audio recorder, extractor, and 14-day retention pruner."""

    def __init__(
        self,
        root_dir: Path,
        ffmpeg_bin: str = "ffmpeg",
        segment_sec: float = 10.0,
        retention_days: int = 14,
        bitrate: str = "96k",
        ram_tail_sec: float = 35.0,
        sync_offset_ms: int = 0,
        ref_active_rms: float = 0.00025,
    ) -> None:
        self.root = Path(root_dir).resolve()
        self.audio_tapes_root = self.root / "audio_tapes"
        self.previews_root = self.root / "previews"
        self.audio_tapes_root.mkdir(parents=True, exist_ok=True)
        self.previews_root.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = str(ffmpeg_bin or "ffmpeg")
        self.segment_sec = max(2.0, float(segment_sec))
        self.retention_days = max(1, int(retention_days))
        self.bitrate = str(bitrate or "96k")
        self.ram_tail_ms = max(10_000, int(round(ram_tail_sec * 1000.0)))
        self.sync_offset_ms = max(-3000, min(3000, int(sync_offset_ms)))
        self.ref_active_rms = float(ref_active_rms)
        self.log = logging.getLogger("armory-room-audio")

        self._lock = threading.Lock()
        self._recent_blocks: deque[tuple[int, int, int, bytes]] = deque()
        self._cur_chunks: list[bytes] = []
        self._cur_start_ms: int = 0
        self._cur_end_ms: int = 0
        self._cur_sr: int = 0
        self._cur_samples: int = 0

        # Secondary paired Denon Line-In reference archiver (writes ref_*.m4a ONLY when music is playing)
        self._ref_chunks: list[bytes] = []
        self._ref_start_ms: int = 0
        self._ref_end_ms: int = 0
        self._ref_sr: int = 0
        self._ref_samples: int = 0
        self._ref_hold_until_ms: int = 0

        self._write_q: queue.Queue[tuple[int, int, int, bytes, str] | None] = queue.Queue(maxsize=48)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._worker = threading.Thread(target=self._writer_loop, name="armory-room-audio", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        self.flush_now(blocking=False)
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            w = self._worker
            self._worker = None
        try:
            self._write_q.put_nowait(None)
        except queue.Full:
            pass
        if w is not None:
            w.join(timeout=3.0)

    def _enqueue_locked(self, block: tuple[int, int, int, bytes, str]) -> None:
        if not self._started:
            return
        try:
            self._write_q.put_nowait(block)
        except queue.Full:
            try:
                _ = self._write_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._write_q.put_nowait(block)
            except queue.Full:
                pass

    def _pop_pending_locked(self) -> tuple[int, int, int, bytes, str] | None:
        if not self._cur_chunks or self._cur_samples <= 0 or self._cur_sr <= 0:
            self._cur_chunks.clear()
            self._cur_samples = 0
            return None
        raw = b"".join(self._cur_chunks)
        block = (self._cur_start_ms, self._cur_end_ms, self._cur_sr, raw, "aud")
        self._cur_chunks.clear()
        self._cur_samples = 0
        self._cur_start_ms = 0
        self._cur_end_ms = 0
        return block

    def _pop_pending_ref_locked(self) -> tuple[int, int, int, bytes, str] | None:
        if not self._ref_chunks or self._ref_samples <= 0 or self._ref_sr <= 0:
            self._ref_chunks.clear()
            self._ref_samples = 0
            return None
        raw = b"".join(self._ref_chunks)
        block = (self._ref_start_ms, self._ref_end_ms, self._ref_sr, raw, "ref")
        self._ref_chunks.clear()
        self._ref_samples = 0
        self._ref_start_ms = 0
        self._ref_end_ms = 0
        return block

    def push_ref_pcm(self, mono_f32: np.ndarray, sr: int, wall_end_s: float | None = None) -> None:
        """Archive paired Denon Line-In mono PCM (`ref_*.m4a`) ONLY while music is actively playing."""
        arr = np.asarray(mono_f32, dtype=np.float32).reshape(-1)
        if arr.size == 0 or sr <= 0:
            return
        now_s = time.time() if wall_end_s is None else float(wall_end_s)
        end_ms = int(round(now_s * 1000.0))
        dur_ms = max(1, int(round((arr.size * 1000.0) / float(sr))))
        start_ms = end_ms - dur_ms
        rms = float(np.sqrt(np.mean(arr * arr)))

        completed: tuple[int, int, int, bytes, str] | None = None
        with self._lock:
            if rms >= self.ref_active_rms:
                self._ref_hold_until_ms = end_ms + 3_000
            if end_ms > self._ref_hold_until_ms:
                if self._ref_chunks:
                    completed = self._pop_pending_ref_locked()
                    if completed is not None:
                        self._enqueue_locked(completed)
                return

            pcm_i16 = np.round(np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            if self._ref_chunks and (self._ref_sr != int(sr) or abs(start_ms - self._ref_end_ms) > 1500):
                completed = self._pop_pending_ref_locked()
            if not self._ref_chunks:
                self._ref_start_ms = start_ms
                self._ref_sr = int(sr)
            self._ref_end_ms = end_ms
            self._ref_samples += int(arr.size)
            self._ref_chunks.append(pcm_i16)
            if completed is None and (self._ref_samples / float(self._ref_sr)) >= self.segment_sec:
                completed = self._pop_pending_ref_locked()
            if completed is not None:
                self._enqueue_locked(completed)

    def push_pcm(self, mono_f32: np.ndarray, sr: int, wall_end_s: float | None = None) -> None:
        """Append mono float32 microphone samples into the RAM tail & 10s disk segmenter."""
        arr = np.asarray(mono_f32, dtype=np.float32).reshape(-1)
        if arr.size == 0 or sr <= 0:
            return
        now_s = time.time() if wall_end_s is None else float(wall_end_s)
        end_ms = int(round(now_s * 1000.0))
        dur_ms = max(1, int(round((arr.size * 1000.0) / float(sr))))
        start_ms = end_ms - dur_ms
        pcm_i16 = np.round(np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

        completed: tuple[int, int, int, bytes] | None = None
        with self._lock:
            self._recent_blocks.append((start_ms, end_ms, int(sr), pcm_i16))
            cutoff_ms = end_ms - self.ram_tail_ms
            while self._recent_blocks and self._recent_blocks[0][1] < cutoff_ms:
                self._recent_blocks.popleft()

            if self._cur_chunks and (self._cur_sr != int(sr) or abs(start_ms - self._cur_end_ms) > 1500):
                completed = self._pop_pending_locked()

            if not self._cur_chunks:
                self._cur_start_ms = start_ms
                self._cur_sr = int(sr)
            self._cur_end_ms = end_ms
            self._cur_samples += int(arr.size)
            self._cur_chunks.append(pcm_i16)

            if completed is None and (self._cur_samples / float(self._cur_sr)) >= self.segment_sec:
                completed = self._pop_pending_locked()

            if completed is not None:
                self._enqueue_locked(completed)

    def flush_now(self, *, blocking: bool = True) -> Path | None:
        """Flush any buffered samples cleanly to disk (optionally synchronously)."""
        with self._lock:
            block = self._pop_pending_locked()
            ref_block = self._pop_pending_ref_locked()
        out_p: Path | None = None
        if block is not None:
            if blocking:
                out_p = self._write_block_to_disk(block)
            else:
                with self._lock:
                    self._enqueue_locked(block)
        if ref_block is not None:
            if blocking:
                self._write_block_to_disk(ref_block)
            else:
                with self._lock:
                    self._enqueue_locked(ref_block)
        return out_p

    def _write_block_to_disk(self, block: tuple[int, int, int, bytes] | tuple[int, int, int, bytes, str]) -> Path | None:
        if len(block) >= 5:
            start_ms, end_ms, sr, pcm_bytes, prefix = block[0], block[1], block[2], block[3], block[4]
        else:
            start_ms, end_ms, sr, pcm_bytes = block[0], block[1], block[2], block[3]
            prefix = "aud"
        if not pcm_bytes or end_ms <= start_ms or sr <= 0:
            return None
        day_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d")
        day_dir = self.audio_tapes_root / day_str
        day_dir.mkdir(parents=True, exist_ok=True)
        out_path = day_dir / f"{prefix}_{start_ms}_{end_ms}.m4a"
        tmp_path = day_dir / f".tmp_{prefix}_{start_ms}_{end_ms}_{uuid.uuid4().hex[:6]}.m4a"
        cmd = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "aac",
            "-b:a",
            self.bitrate,
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]
        try:
            res = subprocess.run(cmd, input=pcm_bytes, capture_output=True, timeout=30)
            if res.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                self.log.warning("RoomAudio encode failed: %s", res.stderr.decode("utf-8", errors="ignore").strip())
                tmp_path.unlink(missing_ok=True)
                return None
            tmp_path.replace(out_path)
            return out_path
        except Exception as exc:
            self.log.warning("RoomAudio write error: %s", exc)
            tmp_path.unlink(missing_ok=True)
            return None

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or not self._write_q.empty():
            try:
                item = self._write_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                continue
            self._write_block_to_disk(item)

    def _find_disk_segments(self, start_ms: int, end_ms: int) -> list[tuple[int, int, Path]]:
        if not self.audio_tapes_root.exists() or end_ms <= start_ms:
            return []
        d0 = datetime.fromtimestamp(max(0, start_ms - 60_000) / 1000.0).date()
        d1 = datetime.fromtimestamp(max(0, end_ms + 60_000) / 1000.0).date()
        out: list[tuple[int, int, Path]] = []
        cur = d0
        while cur <= d1:
            day_dir = self.audio_tapes_root / cur.strftime("%Y-%m-%d")
            if day_dir.is_dir():
                for p in day_dir.glob("aud_*.m4a"):
                    m = _SEG_NAME_RE.match(p.name)
                    if not m or m.group(1) != "aud":
                        continue
                    s_ms = int(m.group(2))
                    e_ms = int(m.group(3))
                    if e_ms > start_ms and s_ms < end_ms and p.stat().st_size > 128:
                        out.append((s_ms, e_ms, p))
            cur += timedelta(days=1)
        out.sort(key=lambda item: item[0])
        return out

    def _snapshot_ram_blocks(self, start_ms: int, end_ms: int) -> list[tuple[int, int, int, bytes]]:
        with self._lock:
            return [
                blk
                for blk in self._recent_blocks
                if blk[1] > start_ms and blk[0] < end_ms
            ]

    def has_audio(self, start_ms: int, end_ms: int) -> bool:
        """Return True if disk segments or RAM tail cover >= 300ms within `[start_ms, end_ms]`."""
        s_ms = int(start_ms)
        e_ms = int(end_ms)
        if e_ms <= s_ms:
            return False
        overlap_ms = 0
        for ds, de, _p in self._find_disk_segments(s_ms, e_ms):
            overlap_ms += max(0, min(e_ms, de) - max(s_ms, ds))
            if overlap_ms >= 300:
                return True
        covered_until = max((de for _ds, de, _p in self._find_disk_segments(s_ms, e_ms)), default=s_ms)
        for rs, re_ms, _sr, _raw in self._snapshot_ram_blocks(covered_until, e_ms):
            overlap_ms += max(0, min(e_ms, re_ms) - max(s_ms, rs))
            if overlap_ms >= 300:
                return True
        return False

    def extract_window_m4a(
        self,
        start_ms: int,
        end_ms: int,
        out_path: Path,
        *,
        apply_sync_offset: bool = True,
    ) -> bool:
        """Extract wall-clock aligned `[start_ms, end_ms]` PowerConf S330 AAC audio to `out_path`.

        Sign convention matches FFmpeg/VLC: negative `sync_offset_ms` (e.g. `-570`) shifts
        audio EARLIER relative to video by seeking forward (`start_ms - sync_offset_ms`) in
        the audio tape, compensating for RTSP video buffer lead vs room mic latency.
        """
        offset = int(self.sync_offset_ms) if apply_sync_offset else 0
        s_ms = int(start_ms) - offset
        e_ms = max(s_ms + 500, int(end_ms) - offset)
        dur_sec = max(0.5, (e_ms - s_ms) / 1000.0)

        disk_segs = self._find_disk_segments(s_ms, e_ms)
        covered_until = disk_segs[-1][1] if disk_segs else s_ms
        ram_blocks = self._snapshot_ram_blocks(covered_until, e_ms)
        if not disk_segs and not ram_blocks:
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex[:8]
        tmp_wav: Path | None = None
        tmp_parts: list[tuple[int, int, Path]] = list(disk_segs)

        try:
            if ram_blocks:
                target_sr = ram_blocks[0][2]
                same_sr_chunks = [b[3] for b in ram_blocks if b[2] == target_sr]
                if same_sr_chunks:
                    ram_s = ram_blocks[0][0]
                    ram_e = max(b[1] for b in ram_blocks if b[2] == target_sr)
                    tmp_wav = out_path.with_name(f".ram_{uid}.wav")
                    with wave.open(str(tmp_wav), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(target_sr)
                        wf.writeframes(b"".join(same_sr_chunks))
                    tmp_parts.append((ram_s, ram_e, tmp_wav))

            if not tmp_parts:
                return False
            tmp_parts.sort(key=lambda item: item[0])

            first_s = tmp_parts[0][0]
            lead_ms = max(0, first_s - s_ms)
            seek_sec = max(0.0, (s_ms - first_s) / 1000.0)
            tmp_out = out_path.with_name(f".aud_{uid}.m4a")
            concat_txt = out_path.with_name(f".aconcat_{uid}.txt")

            try:
                if len(tmp_parts) == 1:
                    inp_args = ["-i", str(tmp_parts[0][2])]
                else:
                    lines = []
                    for _ps, _pe, p in tmp_parts:
                        posix_p = p.resolve().as_posix().replace("'", r"'\''")
                        lines.append(f"file '{posix_p}'")
                    concat_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    inp_args = ["-f", "concat", "-safe", "0", "-i", str(concat_txt)]

                afilters: list[str] = ["aresample=48000"]
                if lead_ms > 40:
                    afilters.append(f"adelay={lead_ms}:all=1")
                afilters.append("apad")

                cmd = [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    *inp_args,
                    *(["-ss", f"{seek_sec:.3f}"] if seek_sec > 0.04 else []),
                    "-af",
                    ",".join(afilters),
                    "-t",
                    f"{dur_sec:.3f}",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-c:a",
                    "aac",
                    "-b:a",
                    self.bitrate,
                    "-movflags",
                    "+faststart",
                    str(tmp_out),
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
                if res.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                    self.log.warning("RoomAudio extract failed: %s", res.stderr.strip())
                    tmp_out.unlink(missing_ok=True)
                    return False
                tmp_out.replace(out_path)
                return True
            finally:
                concat_txt.unlink(missing_ok=True)
                tmp_out.unlink(missing_ok=True)
        finally:
            if tmp_wav is not None:
                tmp_wav.unlink(missing_ok=True)

    def prune(self, now_s: float, retention_days: int | None = None) -> None:
        """Remove audio tape segments older than `retention_days` (and preview slices >24h)."""
        days = max(1, int(retention_days if retention_days is not None else self.retention_days))
        cutoff_s = float(now_s) - (days * 86_400)
        cutoff_ms = int(cutoff_s * 1000.0)
        if self.audio_tapes_root.exists():
            for pat in ("aud_*.m4a", "ref_*.m4a"):
                for p in list(self.audio_tapes_root.rglob(pat)):
                    try:
                        m = _SEG_NAME_RE.match(p.name)
                        expired = (int(m.group(3)) < cutoff_ms) if m else (p.stat().st_mtime < cutoff_s)
                        if expired:
                            p.unlink(missing_ok=True)
                    except OSError:
                        pass
            for p in list(self.audio_tapes_root.rglob(".tmp_*")):
                try:
                    if p.stat().st_mtime < float(now_s) - 300:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
            for d in sorted(self.audio_tapes_root.iterdir(), reverse=True):
                if d.is_dir():
                    try:
                        if not any(d.iterdir()):
                            d.rmdir()
                    except OSError:
                        pass

        if self.previews_root.exists():
            prev_cutoff_s = float(now_s) - (24 * 3600)
            for p in list(self.previews_root.glob("aud_*.m4a")):
                try:
                    if p.stat().st_mtime < prev_cutoff_s:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
