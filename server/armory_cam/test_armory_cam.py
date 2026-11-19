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

    def test_crop_rotate_normalization_and_vf_builder(self) -> None:
        self.assertEqual(ac.normalize_crop([0.25, 0.25, 0.75, 0.75]), (0.25, 0.25, 0.75, 0.75))
        self.assertEqual(ac.normalize_crop([0.9, 0.9, 0.92, 0.92]), (0.85, 0.85, 1.0, 1.0))
        self.assertEqual(ac.normalize_rotate(90), 90)
        self.assertEqual(ac.normalize_rotate(45), 0)
        self.assertIsNone(ac.build_ffmpeg_vf((0.0, 0.0, 1.0, 1.0), 0))
        vf = ac.build_ffmpeg_vf((0.25, 0.25, 0.75, 0.75), 90)
        self.assertIsNotNone(vf)
        self.assertIn("transpose=1", vf)
        self.assertIn("crop=", vf)

    def test_flask_api_synthetic_clip_crop_and_http_206_byte_range(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            # 1. Verify /api/ping + /api/config round-trip with crop & rotate
            ping = client.get("/api/ping").get_json()
            self.assertEqual(ping["service"], "armory-cam")
            self.assertEqual(ping["cameras"], ["Rack Cam", "Smith Cam"])
            self.assertFalse(ping["recording"])

            cfg_res = client.post(
                "/api/config",
                json={
                    "retentionDays": 21,
                    "cameras": [
                        {
                            "name": "Rack Cam",
                            "slug": "rack_cam",
                            "rtspUrl": "",
                            "enabled": True,
                            "motionThreshold": 2.0,
                            "crop": [0.25, 0.25, 0.75, 0.75],
                            "rotate": 0,
                        },
                        {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.0},
                    ],
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            saved_cfg = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_cfg["retentionDays"], 21)
            self.assertEqual(saved_cfg["cameras"][0]["crop"], [0.25, 0.25, 0.75, 0.75])

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

            # Verify 50% crop on 320x240 input yielded exact 160x120 FastStart MP4
            out_file = svc.clips_root / clip_data["lanUrl"].removeprefix("/clips/")
            probe_out = subprocess.check_output(
                [svc.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(out_file)],
                text=True,
            ).strip()
            self.assertEqual(probe_out, "160x120")

            status = client.get("/api/status").get_json()
            self.assertEqual(status["service"], "armory-cam")
            self.assertEqual(len(status["cameras"]), 2)

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

    def test_wallboard_relay_html_state_config_and_adb_lifecycle(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_wb_test_"))
        cmds: list[list[str]] = []
        dev_state = {"state": "device"}

        def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmds.append(cmd[1:])
            args = cmd[1:]
            if args[:1] == ["connect"]:
                return 0, f"connected to {args[1]}", ""
            if args[:1] == ["devices"]:
                return 0, f"List of devices attached\n192.168.1.99:5555\t{dev_state['state']}\n", ""
            if args[-2:] == ["getprop", "ro.product.model"]:
                return 0, "AFTR\n", ""
            return 0, "Status: ok\n", ""

        try:
            adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=fake_runner, settle_sec=0.0)
            svc = ac.ArmoryCamService(root_dir=tmp, adb_controller=adb)
            app = ac.create_app(svc)
            client = app.test_client()

            # 1. GET /wallboard extracts live WALLBOARD_HTML with dual-mount base-path helpers
            wb_page = client.get("/wallboard")
            self.assertEqual(wb_page.status_code, 200)
            html = wb_page.get_data(as_text=True)
            self.assertIn("Armory Fit · Gym Wallboard", html)
            self.assertIn("wbUrl('/state')", html)
            self.assertIn("wbUrl('/events')", html)

            # 2. GET /wallboard/state returns valid standby JSON before workout starts
            st0 = client.get("/wallboard/state").get_json()
            self.assertEqual(st0["mode"], "standby")

            # 3. POST /api/config persists wallboard settings without clobbering cameras
            cfg_res = client.post(
                "/api/config",
                json={
                    "wallboard": {
                        "fireTvHost": "192.168.1.99",
                        "adbPort": 5555,
                        "displayClient": "silk",
                        "wallboardUrl": "http://192.168.1.152:8766/wallboard",
                        "wakeOnStart": True,
                        "sleepOnStop": True,
                        "idleSleepSec": 600,
                    }
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            saved_cfg = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved_cfg["cameras"]), 2)
            self.assertEqual(saved_cfg["wallboard"]["fireTvHost"], "192.168.1.99")

            # 4. ADB probe handles authorized, unauthorized, and offline states
            probe_ok = client.post("/api/wallboard/adb/test", json={}).get_json()
            self.assertTrue(probe_ok["ok"])
            self.assertEqual(probe_ok["state"], "ready")
            self.assertEqual(probe_ok["model"], "AFTR")

            dev_state["state"] = "unauthorized"
            probe_unauth = client.post("/api/wallboard/adb/test", json={}).get_json()
            self.assertFalse(probe_unauth["ok"])
            self.assertEqual(probe_unauth["state"], "unauthorized")
            self.assertIn("Always allow", probe_unauth["message"])

            dev_state["state"] = "offline"
            probe_off = client.post("/api/wallboard/adb/test", json={}).get_json()
            self.assertFalse(probe_off["ok"])
            self.assertEqual(probe_off["state"], "offline")

            # 5. Active state push wakes TV with KEYCODE_WAKEUP, disables screensaver, and opens Silk
            dev_state["state"] = "device"
            cmds.clear()
            snap_lift = {
                "ts": 1_763_500_000_000,
                "mode": "lift",
                "lift": {"title": "Upper 1", "started": True, "paused": False, "setsDone": 1, "setsTotal": 10},
            }
            self.assertEqual(
                client.post(
                    "/api/wallboard/state",
                    json={"state": snap_lift, "workoutActive": True, "sourceUrl": "http://192.168.1.50:8765"},
                ).status_code,
                200,
            )
            import time as _time
            for _ in range(40):
                if adb.tv_awake_requested:
                    break
                _time.sleep(0.02)
            self.assertTrue(adb.tv_awake_requested)
            flat = [" ".join(c) for c in cmds]
            self.assertTrue(any("KEYCODE_WAKEUP" in x for x in flat))
            self.assertTrue(any("screensaver_enabled 0" in x for x in flat))
            self.assertTrue(any("com.amazon.cloud9/.BrowserActivity" in x for x in flat))
            self.assertEqual(client.get("/wallboard/state").get_json()["lift"]["title"], "Upper 1")

            # 6. Delayed sleep schedules timer, and active push cancels it; immediate sleep executes KEYCODE_SLEEP
            slp_sched = client.post(
                "/api/wallboard/sleep",
                json={"reason": "workout_finished", "delaySec": 30},
            ).get_json()
            self.assertEqual(slp_sched["scheduledDelaySec"], 30)
            self.assertIsNotNone(adb.pending_sleep_at_ms)

            self.assertEqual(
                client.post("/api/wallboard/state", json={"state": snap_lift, "workoutActive": True}).status_code,
                200,
            )
            self.assertIsNone(adb.pending_sleep_at_ms)

            cmds.clear()
            slp_now = client.post(
                "/api/wallboard/sleep",
                json={"reason": "manual", "delaySec": 0, "sync": True},
            ).get_json()
            self.assertTrue(slp_now["adb"]["ok"])
            flat2 = [" ".join(c) for c in cmds]
            self.assertTrue(any("screensaver_enabled 1" in x for x in flat2))
            self.assertTrue(any("KEYCODE_SLEEP" in x for x in flat2))
            self.assertFalse(adb.tv_awake_requested)

            # 7. Idle watchdog triggers KEYCODE_SLEEP after idleSleepSec elapses
            adb.tv_awake_requested = True
            cmds.clear()
            now_ms = int(_time.time() * 1000)
            res_idle = adb.check_timers(
                svc.wallboard_cfg,
                last_active_push_ms=now_ms - 700_000,
                workout_active=True,
                now_ms=now_ms,
            )
            self.assertIsNotNone(res_idle)
            self.assertTrue(any("KEYCODE_SLEEP" in " ".join(c) for c in cmds))
            self.assertFalse(adb.tv_awake_requested)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_per_camera_30s_burst_filtering_and_tape_timeline_frame_endpoints(self) -> None:
        base = 1_763_500_000_000
        # 1. Build synthetic 2s samples with an 11s Rack blip (skipped at 30s+) and a 34s Rack set
        # while Smith Cam has louder overlapping buddy motion (preserved when camera="Rack Cam").
        samples = []
        # 11s Rack blip (5 x 2s = 10s raw, +5s = 15s < 30s)
        for i in range(5):
            samples.append({"camera": "Rack Cam", "slug": "rack_cam", "startMs": base + i * 2000, "endMs": base + (i + 1) * 2000, "energy": 8.0})
        # 34s Rack set (17 x 2s = 34s raw, +5s = 39s >= 30s) at +60s..+94s
        for i in range(17):
            s0 = base + 60_000 + i * 2000
            samples.append({"camera": "Rack Cam", "slug": "rack_cam", "startMs": s0, "endMs": s0 + 2000, "energy": 6.5})
            samples.append({"camera": "Smith Cam", "slug": "smith_cam", "startMs": s0, "endMs": s0 + 2000, "energy": 14.0})

        rack_30 = ac.cluster_motion_bursts(samples, threshold=2.2, min_dur_ms=30_000, camera="Rack Cam")
        self.assertEqual(len(rack_30), 1)
        self.assertEqual(rack_30[0]["camera"], "Rack Cam")
        self.assertGreaterEqual(rack_30[0]["durationSec"], 30)

        # 2. HTTP /api/bursts?camera=Rack%20Cam&minDurSec=30 + /api/tape/timeline + /api/tape/frame
        tmp = Path(tempfile.mkdtemp(prefix="armory_tape_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            client.post("/api/config", json={
                "cameras": [
                    {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "crop": [0.25, 0.25, 0.75, 0.75]},
                    {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True},
                ]
            })
            client.post("/api/session/start", json={"sessionId": "sess-tape", "name": "Buddy Lift"})

            ff = shutil.which("ffmpeg") or svc.ffmpeg
            t0_dt = datetime(2026, 11, 18, 20, 10, 0)
            t0_ms = int(t0_dt.timestamp() * 1000)
            rack_dir = svc.tapes_root / "sess-tape" / "rack_cam"
            rack_dir.mkdir(parents=True, exist_ok=True)
            m_lines = []

            # 22 x 2s segments = 44s on Rack Cam: first 5 idle/short blip, then pause, then 14-seg (28s+pad=33s) set
            for i in range(22):
                seg_dt = datetime.fromtimestamp((t0_ms + i * 2000) / 1000.0)
                seg_name = f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                seg_path = rack_dir / seg_name
                if i in (0, 7, 15, 21):
                    subprocess.run(
                        [
                            ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=green:s=320x240:r=20:d=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                        ],
                        check=True,
                    )
                else:
                    shutil.copy2(rack_dir / f"seg_{datetime.fromtimestamp(t0_ms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts", seg_path)
                # 8..20 (13*2=26s raw + 5s pad = 31s) is 30s+; 1..4 (4*2=8s) is blip
                e = 9.0 if (1 <= i <= 4 or 8 <= i <= 20) else 0.4
                m_lines.append(json.dumps({
                    "camera": "Rack Cam",
                    "slug": "rack_cam",
                    "file": seg_name,
                    "startMs": t0_ms + i * 2000,
                    "endMs": t0_ms + (i + 1) * 2000,
                    "energy": e,
                }))
                m_lines.append(json.dumps({
                    "camera": "Smith Cam",
                    "slug": "smith_cam",
                    "file": seg_name,
                    "startMs": t0_ms + i * 2000,
                    "endMs": t0_ms + (i + 1) * 2000,
                    "energy": 18.0 if 8 <= i <= 20 else 0.3,
                }))
            (svc.tapes_root / "sess-tape" / "motion.jsonl").write_text("\n".join(m_lines) + "\n", encoding="utf-8")

            b_res = client.get(f"/api/bursts?sessionId=sess-tape&camera=Rack%20Cam&minDurSec=30&aroundMs={t0_ms + 42000}")
            self.assertEqual(b_res.status_code, 200)
            bursts = b_res.get_json()["bursts"]
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["camera"], "Rack Cam")
            self.assertGreaterEqual(bursts[0]["durationSec"], 30)
            self.assertTrue(bursts[0]["thumb"].startswith("data:image/jpeg;base64,"))

            tl_res = client.get("/api/tape/timeline?sessionId=sess-tape&camera=Rack%20Cam&buckets=20")
            self.assertEqual(tl_res.status_code, 200)
            tl = tl_res.get_json()
            self.assertEqual(tl["segmentCount"], 22)
            self.assertEqual(tl["minMs"], t0_ms)
            self.assertEqual(len(tl["activity"]), 20)
            self.assertEqual(len(tl["bursts30"]), 1)

            fr_res = client.get(f"/api/tape/frame?sessionId=sess-tape&camera=Rack%20Cam&tsMs={t0_ms + 30500}&w=480")
            self.assertEqual(fr_res.status_code, 200)
            self.assertTrue(fr_res.data.startswith(b"\xff\xd8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
