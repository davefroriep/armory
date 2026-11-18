"""End-to-end tests for Armory Cam motion-burst detection, dual-cam auto-selection, and FFmpeg clip cutting."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import armory_cam_server as ac  # noqa: E402


class ArmoryCamTests(unittest.TestCase):
    def test_motion_burst_clustering_and_dual_camera_collision_resolution(self) -> None:
        base = 1_763_500_000_000
        samples = [
            # Idle mirror flicker on both cameras
            {"camera": "Rack Cam", "startMs": base, "endMs": base + 2000, "energy": 0.4},
            {"camera": "Smith Cam", "startMs": base, "endMs": base + 2000, "energy": 0.5},
            # Squat set in Rack Cam (stronger) with faint mirror reflection in Smith Cam
            {"camera": "Rack Cam", "startMs": base + 10_000, "endMs": base + 12_000, "energy": 8.4},
            {"camera": "Rack Cam", "startMs": base + 12_000, "endMs": base + 14_000, "energy": 11.2},
            {"camera": "Rack Cam", "startMs": base + 14_000, "endMs": base + 16_000, "energy": 9.1},
            {"camera": "Smith Cam", "startMs": base + 10_000, "endMs": base + 12_000, "energy": 3.0},
            {"camera": "Smith Cam", "startMs": base + 12_000, "endMs": base + 14_000, "energy": 3.2},
            # Later Smith row set in Smith Cam
            {"camera": "Smith Cam", "startMs": base + 90_000, "endMs": base + 92_000, "energy": 9.5},
            {"camera": "Smith Cam", "startMs": base + 92_000, "endMs": base + 94_000, "energy": 10.4},
            {"camera": "Smith Cam", "startMs": base + 94_000, "endMs": base + 96_000, "energy": 8.8},
        ]
        bursts = ac.cluster_motion_bursts(samples, threshold=2.2)
        self.assertEqual(len(bursts), 2)
        self.assertEqual(bursts[0]["camera"], "Rack Cam")
        self.assertEqual(bursts[1]["camera"], "Smith Cam")
        self.assertEqual(bursts[0]["startMs"], base + 7000)
        self.assertEqual(bursts[0]["endMs"], base + 18000)

        picked = ac.select_camera_for_window(samples, base + 10_000, base + 16_000)
        self.assertEqual(picked, "Rack Cam")
        overridden = ac.select_camera_for_window(samples, base + 10_000, base + 16_000, preferred_camera="Smith Cam")
        self.assertEqual(overridden, "Smith Cam")

    def test_select_pr_window_respects_prev_set_and_batch_boundary(self) -> None:
        base = 1_763_500_000_000
        bursts = [
            {"id": "b1", "camera": "Rack Cam", "startMs": base + 10_000, "endMs": base + 22_000},
            {"id": "b2", "camera": "Rack Cam", "startMs": base + 120_000, "endMs": base + 135_000},
        ]
        # PR tapped right after b2 with prev set ending at +60s -> picks b2
        s_ms, e_ms, cam = ac.select_pr_window(bursts, set_at_ms=base + 137_000, prev_set_at_ms=base + 60_000)
        self.assertEqual((s_ms, e_ms, cam), (base + 120_000, base + 135_000, "Rack Cam"))

        # No burst in window -> falls back cleanly
        fs, fe, fcam = ac.select_pr_window([], set_at_ms=base + 200_000, prev_set_at_ms=base + 100_000)
        self.assertEqual((fs, fe, fcam), (base + 165_000, base + 202_000, None))

    def test_flask_api_synthetic_clip_and_http_206_byte_range(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            # 1. Verify /api/ping + /api/config round-trip
            ping = client.get("/api/ping").get_json()
            self.assertEqual(ping["service"], "armory-cam")
            self.assertEqual(ping["cameras"], ["Rack Cam", "Smith Cam"])
            self.assertFalse(ping["recording"])

            cfg_res = client.post(
                "/api/config",
                json={
                    "retentionDays": 21,
                    "cameras": [
                        {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.0},
                        {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.0},
                    ],
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            self.assertEqual(json.loads((tmp / "config.json").read_text(encoding="utf-8"))["retentionDays"], 21)

            # 2. Start session and synthesize real 2s MPEG-TS segments in Rack Cam ring
            self.assertEqual(client.post("/api/session/start", json={"sessionId": "sess-1", "name": "Lower 1"}).status_code, 200)

            ff = shutil.which("ffmpeg") or svc.ffmpeg
            base_dt = datetime(2026, 11, 18, 17, 50, 0)
            base_ms = int(base_dt.timestamp() * 1000)
            rack_tape = svc.tapes_root / "sess-1" / "rack_cam"
            rack_tape.mkdir(parents=True, exist_ok=True)
            motion_lines = []

            for i in range(4):
                seg_dt = datetime.fromtimestamp((base_ms + i * 2000) / 1000.0)
                seg_name = f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                seg_path = rack_tape / seg_name
                color = "blue" if i in (1, 2) else "black"
                subprocess.run(
                    [
                        ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=20:d=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                    ],
                    check=True,
                )
                motion_lines.append(
                    json.dumps(
                        {
                            "camera": "Rack Cam",
                            "slug": "rack_cam",
                            "file": seg_name,
                            "startMs": base_ms + i * 2000,
                            "endMs": base_ms + (i + 1) * 2000,
                            "energy": 9.5 if i in (1, 2) else 0.3,
                        }
                    )
                )
            (svc.tapes_root / "sess-1" / "motion.jsonl").write_text("\n".join(motion_lines) + "\n", encoding="utf-8")

            # 3. Cut a 4s clip and stream it via HTTP 206 Partial Content
            clip_res = client.post(
                "/api/clip",
                json={
                    "sessionId": "sess-1",
                    "exId": "low-bar-squat",
                    "exName": "Low-Bar Squat",
                    "setId": "s1",
                    "startMs": base_ms + 2000,
                    "endMs": base_ms + 6000,
                },
            )
            self.assertEqual(clip_res.status_code, 200)
            clip_data = clip_res.get_json()
            self.assertTrue(clip_data["ok"])
            self.assertEqual(clip_data["camera"], "Rack Cam")
            self.assertTrue(clip_data["lanUrl"].startswith("/clips/"))
            self.assertGreater(clip_data["bytes"], 1000)
            self.assertTrue(clip_data["thumb"].startswith("data:image/jpeg;base64,"))

            r206 = client.get(clip_data["lanUrl"], headers={"Range": "bytes=0-1023"})
            self.assertEqual(r206.status_code, 206)
            self.assertEqual(len(r206.data), 1024)
            self.assertEqual(r206.headers["Accept-Ranges"], "bytes")
            self.assertTrue(r206.headers["Content-Range"].startswith(f"bytes 0-1023/{clip_data['bytes']}"))

            # 4. Path traversal check
            self.assertIn(client.get("/clips/../../config.json").status_code, (403, 404))

            # 5. Pause/Resume/Stop
            self.assertEqual(client.post("/api/session/pause", json={"sessionId": "sess-1"}).status_code, 200)
            self.assertTrue(client.get("/api/ping").get_json()["paused"])
            self.assertEqual(client.post("/api/session/resume", json={"sessionId": "sess-1"}).status_code, 200)
            self.assertFalse(client.get("/api/ping").get_json()["paused"])
            self.assertEqual(client.post("/api/session/stop", json={"sessionId": "sess-1", "discard": False}).status_code, 200)
            self.assertFalse(client.get("/api/ping").get_json()["recording"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
