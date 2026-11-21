"""End-to-end tests for Armory Cam motion-burst detection, dual-cam auto-selection, and FFmpeg clip cutting."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import armory_cam_server as ac  # noqa: E402
import display_control as dc  # noqa: E402
import house_audio as ha  # noqa: E402
import room_audio as ra  # noqa: E402
import room_voice as rv  # noqa: E402
import show_director as sd  # noqa: E402


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

        # All enabled views compete equally on motion energy; explicit selection always wins
        extra_samples = samples + [
            {"camera": "Fly Machine Cam", "startMs": base + 10_000, "endMs": base + 14_000, "energy": 25.0},
        ]
        auto_picked = ac.select_camera_for_window(extra_samples, base + 10_000, base + 16_000)
        self.assertEqual(auto_picked, "Fly Machine Cam")
        explicit_pick = ac.select_camera_for_window(
            extra_samples,
            base + 10_000,
            base + 16_000,
            preferred_camera="Rack Cam",
        )
        self.assertEqual(explicit_pick, "Rack Cam")

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
            self.assertEqual(ping["retentionDays"], 14)
            self.assertFalse(ping["recording"])

            dash_html = client.get("/").get_data(as_text=True)
            self.assertIn("+ Mask Mirror (Red)", dash_html)
            self.assertIn("data-addexcl", dash_html)
            self.assertIn("maskModal", dash_html)
            self.assertIn("mmSaveBtn", dash_html)
            self.assertIn("quad-dot", dash_html)
            self.assertIn("Saved!", dash_html)

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
                            "roi": [0.3, 0.3, 0.7, 0.7],
                            "exclusions": [[0.55, 0.1, 0.9, 0.6]],
                            "crop": [0.25, 0.25, 0.75, 0.75],
                            "rotate": 0,
                            "defish": False,
                        },
                        {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.0},
                    ],
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            saved_cfg = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_cfg["retentionDays"], 21)
            self.assertEqual(client.get("/api/ping").get_json()["retentionDays"], 21)
            self.assertEqual(saved_cfg["cameras"][0]["crop"], [0.25, 0.25, 0.75, 0.75])
            self.assertEqual(saved_cfg["cameras"][0]["roi"], [0.3, 0.3, 0.7, 0.7])
            self.assertEqual(
                saved_cfg["cameras"][0]["exclusions"],
                [[[0.55, 0.1], [0.9, 0.1], [0.9, 0.6], [0.55, 0.6]]],
            )

            skew_quad = [[0.4, 0.1], [0.7, 0.2], [0.65, 0.8], [0.35, 0.7]]
            skew_res = client.post(
                "/api/config",
                json={
                    "cameras": [
                        {"name": "Rack Cam", "slug": "rack_cam", "exclusions": [skew_quad]},
                        {"name": "Smith Cam", "slug": "smith_cam"},
                    ]
                },
            )
            self.assertEqual(skew_res.status_code, 200)
            saved_skew = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_skew["cameras"][0]["exclusions"], [skew_quad])
            got_cfg = client.get("/api/config").get_json()
            self.assertEqual(got_cfg["cameras"][0]["exclusions"], [skew_quad])

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

            # Verify 50% crop on 320x240 input yielded exact 160x120 FastStart MP4 with zero B-frames (fast scrub)
            out_file = svc.clips_root / clip_data["lanUrl"].removeprefix("/clips/")
            probe_out = subprocess.check_output(
                [
                    svc.ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,has_b_frames",
                    "-of", "csv=p=0:s=x", str(out_file),
                ],
                text=True,
            ).strip()
            self.assertEqual(probe_out, "160x120x0")

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
            self.assertEqual(st0["standby"]["weekLabel"], "")
            self.assertFalse(st0["standby"]["scheduleSynced"])
            self.assertEqual(st0["standby"]["todayItems"], [])

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

    def test_wallboard_armory_kiosk_client_and_wol_recovery(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_wb_wol_test_"))
        cmds: list[list[str]] = []
        wol_calls: list[str] = []
        connect_attempts = {"n": 0}

        def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            args = cmd[1:]
            cmds.append(args)
            if args[:1] == ["connect"]:
                connect_attempts["n"] += 1
                return 0, f"connected to {args[1]}", ""
            if args[:1] == ["devices"]:
                # First attempt is offline; after WoL fires, second attempt succeeds
                st = "offline" if connect_attempts["n"] < 2 else "device"
                return 0, f"List of devices attached\n192.168.1.204:5555\t{st}\n", ""
            return 0, "Status: ok\n", ""

        def fake_wol(mac_str: str) -> list[str]:
            wol_calls.append(mac_str)
            return ["08:97:98:1f:4c:2b"]

        try:
            adb = ac.FireTvAdbController(
                adb_bin="adb.exe",
                runner=fake_runner,
                settle_sec=0.0,
                wol_sender=fake_wol,
                wol_wait_sec=0.0,
                wol_retries=2,
            )
            svc = ac.ArmoryCamService(root_dir=tmp, adb_controller=adb)
            app = ac.create_app(svc)
            client = app.test_client()

            cfg_res = client.post(
                "/api/config",
                json={
                    "wallboard": {
                        "fireTvHost": "192.168.1.204",
                        "fireTvMac": "08:97:98:1f:4c:2b, 08:97:98:22:59:6b",
                        "adbPort": 5555,
                        "displayClient": "armory",
                        "wallboardUrl": "http://192.168.1.152:8766/wallboard",
                    }
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            saved = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["wallboard"]["displayClient"], "armory")
            self.assertEqual(saved["wallboard"]["fireTvMac"], "08:97:98:1f:4c:2b, 08:97:98:22:59:6b")

            res = adb.wake_and_launch(svc.wallboard_cfg, "http://192.168.1.152:8766/wallboard", force=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["state"], "awake")
            self.assertEqual(wol_calls, ["08:97:98:1f:4c:2b, 08:97:98:22:59:6b"])
            flat = [" ".join(c) for c in cmds]
            self.assertTrue(any("com.froriep.armorywallboard/.WallboardActivity" in x for x in flat))
            self.assertTrue(any("wifi_sleep_policy 2" in x for x in flat))
            self.assertEqual(adb.status_dict(svc.wallboard_cfg)["fireTvMac"], "08:97:98:1f:4c:2b, 08:97:98:22:59:6b")
            self.assertEqual(ac.send_wol_magic_packet("invalid-mac"), [])

            # Standby schedule push (workoutActive=False) must NOT wake ADB and must persist across restarts
            cmds.clear()
            standby_payload = {
                "ts": int(time.time() * 1000),
                "mode": "standby",
                "standby": {
                    "dateLabel": "Friday, Nov 20",
                    "timeLabel": "10:00 PM",
                    "weekLabel": "Wk 1 · Step 4/24",
                    "todayItems": [
                        {
                            "title": "Lower 2",
                            "equipmentLabel": "Strength",
                            "carried": False,
                            "preview": "Deadlift (3) · Bulgarian Split Squat (3)",
                        }
                    ],
                },
            }
            sb_res = client.post(
                "/api/wallboard/state",
                json={"state": standby_payload, "workoutActive": False, "sourceUrl": ""},
            )
            self.assertEqual(sb_res.status_code, 200)
            self.assertEqual(cmds, [])
            self.assertTrue((tmp / "wallboard_standby.json").is_file())

            restarted_relay = ac.WallboardRelay(tmp)
            st_restarted = json.loads(restarted_relay.current_state_json())
            self.assertTrue(st_restarted["standby"]["scheduleSynced"])
            self.assertEqual(st_restarted["standby"]["weekLabel"], "Wk 1 · Step 4/24")
            self.assertEqual(st_restarted["standby"]["todayItems"][0]["title"], "Lower 2")
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

            # Batch /api/tape/pack builds and reuses crop-keyed cached JPEGs under thumbs/
            import base64 as _b64
            pack_res = client.get(
                f"/api/tape/pack?sessionId=sess-tape&camera=Rack%20Cam&startMs={t0_ms}&endMs={t0_ms + 44000}&count=8"
            )
            self.assertEqual(pack_res.status_code, 200)
            pack = pack_res.get_json()
            self.assertTrue(pack["ok"])
            self.assertEqual(pack["camera"], "Rack Cam")
            self.assertEqual(len(pack["frames"]), 8)
            for item in pack["frames"]:
                self.assertGreaterEqual(item["tsMs"], t0_ms)
                raw_jpg = _b64.b64decode(item["jpg"])
                self.assertTrue(raw_jpg.startswith(b"\xff\xd8"))

            rack_thumb_dirs = [d.name for d in (svc.thumbs_root / "rack_cam").iterdir() if d.is_dir()]
            self.assertEqual(len(rack_thumb_dirs), 1)
            cached_jpgs = list((svc.thumbs_root / "rack_cam" / rack_thumb_dirs[0]).glob("*.jpg"))
            self.assertGreaterEqual(len(cached_jpgs), 8)
            self.assertLessEqual(len(cached_jpgs), 9)

            # Updating crop creates a fresh cropKey cache folder (never serves stale thumbs)
            client.post("/api/config", json={
                "cameras": [
                    {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "crop": [0.1, 0.1, 0.9, 0.9]},
                    {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True},
                ]
            })
            pack_res2 = client.get(f"/api/tape/pack?sessionId=sess-tape&camera=Rack%20Cam&count=4")
            self.assertEqual(pack_res2.status_code, 200)
            rack_thumb_dirs2 = sorted(d.name for d in (svc.thumbs_root / "rack_cam").iterdir() if d.is_dir())
            self.assertEqual(len(rack_thumb_dirs2), 2)

            # /api/clip/auto-pr respects explicit camera override even when another camera has stronger motion
            pr_res = client.post(
                "/api/clip/auto-pr",
                json={
                    "sessionId": "sess-tape",
                    "exName": "Squat",
                    "setAtMs": t0_ms + 42_000,
                    "camera": "Rack Cam",
                },
            )
            self.assertEqual(pr_res.status_code, 200)
            self.assertEqual(pr_res.get_json()["camera"], "Rack Cam")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exclusion_zones_mask_mirror_motion(self) -> None:
        import cv2
        import numpy as np

        f0 = np.zeros((180, 320, 3), dtype=np.uint8)
        f_mirror = f0.copy()
        # Simulate bright flickering reflection only inside right-half mirror [0.65..0.95, 0.1..0.9]
        f_mirror[18:162, 208:304] = 245

        unmasked = ac.compute_motion_energy_from_frames([f0, f_mirror])
        masked = ac.compute_motion_energy_from_frames(
            [f0, f_mirror],
            exclusions=[(0.6, 0.05, 1.0, 0.95)],
        )
        self.assertGreater(unmasked, 5.0)
        self.assertLess(masked, 0.5)

        # Lifter moving on left side still scores high when mirror is masked
        f_lift = f_mirror.copy()
        f_lift[30:150, 30:120] = 230
        with_lifter = ac.compute_motion_energy_from_frames(
            [f0, f_lift],
            exclusions=[(0.6, 0.05, 1.0, 0.95)],
        )
        self.assertGreater(with_lifter, 5.0)

        # Angled / skewed 4-dot quad mask rejects mirror motion while preserving pocket pixels inside bounding box
        skew_quad = ((0.49, 0.07), (0.76, 0.07), (0.96, 0.93), (0.69, 0.93))
        f_skew_mirror = f0.copy()
        mirror_pts = np.array([[int(x * 320), int(y * 180)] for x, y in [(0.52, 0.12), (0.73, 0.12), (0.92, 0.88), (0.72, 0.88)]], dtype=np.int32)
        cv2.fillPoly(f_skew_mirror, [mirror_pts], (245, 245, 245))

        self.assertGreater(ac.compute_motion_energy_from_frames([f0, f_skew_mirror]), 5.0)
        self.assertLess(ac.compute_motion_energy_from_frames([f0, f_skew_mirror], exclusions=[skew_quad]), 0.5)

        # Lifter motion in lower-left pocket of skew_quad's bounding box [0.49..0.59, 0.60..0.92]
        # (outside skew_quad, inside axis-aligned rect [0.49, 0.07, 0.96, 0.93])
        f_pocket = f_skew_mirror.copy()
        f_pocket[108:166, 157:189] = 240
        pocket_with_quad = ac.compute_motion_energy_from_frames([f0, f_pocket], exclusions=[skew_quad])
        pocket_with_rect = ac.compute_motion_energy_from_frames(
            [f0, f_pocket], exclusions=[(0.49, 0.07, 0.96, 0.93)]
        )
        self.assertGreater(pocket_with_quad, 3.0)
        self.assertLess(pocket_with_rect, 0.5)

        # Degenerate collinear / zero-area quads rejected and MAX_EXCLUSIONS respected
        many = [
            [[0.0, 0.0], [0.5, 0.0], [0.5, 0.0], [0.0, 0.0]],  # collapsed area=0
            [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]],
        ] * 8
        normed = ac.normalize_exclusions(many)
        self.assertEqual(len(normed), ac.MAX_EXCLUSIONS)

        # Hi-res snapshot endpoint returns valid JPEG when ring segments exist
        tmp = Path(tempfile.mkdtemp(prefix="armory_snap_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            ff = shutil.which("ffmpeg") or svc.ffmpeg
            seg_dir = svc.ring_root / "rack_cam"
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_path = seg_dir / "seg_20261119_120000.ts"
            subprocess.run(
                [
                    ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=320x240:r=20:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                ],
                check=True,
            )
            svc.ring_segments["rack_cam"] = [
                ac.SegmentInfo(path=seg_path, start_ms=1_763_500_000_000, end_ms=1_763_500_001_000, energy=1.0, camera="Rack Cam")
            ]
            snap = client.get("/api/snapshot?camera=Rack%20Cam&raw=1&hires=1")
            self.assertEqual(snap.status_code, 200)
            self.assertEqual(snap.mimetype, "image/jpeg")
            self.assertTrue(snap.data.startswith(b"\xff\xd8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_drive_backup_mirror_and_backfill(self) -> None:
        from unittest import mock

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_drive_"))
        try:
            root = tmp / "camroot"
            drive_dir = tmp / "drive_clips"
            svc = ac.ArmoryCamService(root_dir=root, drive_backup_dir=drive_dir)
            app = ac.create_app(svc)
            client = app.test_client()

            cfg = client.get("/api/config").get_json()
            self.assertEqual(cfg["driveBackupDir"], str(drive_dir))

            t0_ms = int(datetime(2026, 11, 18, 20, 0, 0).timestamp() * 1000)
            rack_dir = svc.tapes_root / "sess-drv" / "rack_cam"
            rack_dir.mkdir(parents=True, exist_ok=True)
            seg_path = rack_dir / f"seg_{datetime.fromtimestamp(t0_ms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts"
            subprocess.run(
                [
                    svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=20:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                ],
                check=True,
            )
            svc.ring_segments["rack_cam"] = [
                ac.SegmentInfo(path=seg_path, start_ms=t0_ms, end_ms=t0_ms + 4000, energy=12.0, camera="Rack Cam")
            ]

            with mock.patch.object(ac, "read_google_drive_file_id", return_value="1AbCdEfGhIjKlMnOpQrStUvWxYz"):
                res = client.post(
                    "/api/clip",
                    json={"sessionId": "sess-drv", "exName": "Bench", "startMs": t0_ms, "endMs": t0_ms + 3000, "camera": "Rack Cam"},
                )
                self.assertEqual(res.status_code, 200)
                body = res.get_json()
                self.assertEqual(body["driveFileId"], "1AbCdEfGhIjKlMnOpQrStUvWxYz")
                self.assertEqual(body["driveUrl"], "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view")
                day_str = datetime.fromtimestamp(t0_ms / 1000.0).strftime("%Y-%m-%d")
                mirrored = drive_dir / day_str / body["fileName"]
                self.assertTrue(mirrored.is_file())

                # Simulate an existing older clip mirrored only after backfill ran
                pre_dir = svc.clips_root / "2026-11-17"
                pre_dir.mkdir(parents=True, exist_ok=True)
                pre_clip = pre_dir / "Squat_180000_aaaaaa.mp4"
                pre_clip.write_bytes(b"fake-mp4-data")
                copied = svc.backfill_drive_clips()
                self.assertEqual(copied, 1)
                self.assertTrue((drive_dir / "2026-11-17" / "Squat_180000_aaaaaa.mp4").is_file())

                clips_list = client.get("/api/clips?limit=200").get_json()["clips"]
                self.assertEqual(clips_list[0]["driveUrl"], "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_delete_clip_cleans_local_drive_and_calibration_milestones(self) -> None:
        from unittest import mock

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_del_"))
        try:
            root = tmp / "camroot"
            drive_dir = tmp / "drive_clips"
            svc = ac.ArmoryCamService(root_dir=root, drive_backup_dir=drive_dir, ntfy_topic="TestTopic")
            app = ac.create_app(svc)
            client = app.test_client()

            day = "2026-11-19"
            (svc.clips_root / day).mkdir(parents=True, exist_ok=True)
            (drive_dir / day).mkdir(parents=True, exist_ok=True)
            entries = []
            for idx in range(5):
                fname = f"Squat_{idx}_abc12{idx}.mp4"
                (svc.clips_root / day / fname).write_bytes(b"mp4-bytes")
                (drive_dir / day / fname).write_bytes(b"mp4-bytes")
                entries.append(json.dumps({"fileName": fname, "lanUrl": f"/clips/{day}/{fname}", "camera": "Rack Cam", "durationSec": 20, "bytes": 9}))
            svc.clips_index_path.write_text("\n".join(entries) + "\n", encoding="utf-8")

            # Milestone 5 fires once via ntfy and persists to calibration_state.json
            with mock.patch.object(ac, "send_ntfy_alert", return_value=True) as m_ntfy:
                crossed = svc.check_calibration_milestones()
                self.assertEqual(crossed, [5])
                # Wait briefly for daemon thread dispatch
                import time as _time
                _time.sleep(0.15)
                self.assertEqual(m_ntfy.call_count, 1)

                # Adding a 6th clip does not re-fire milestone 5
                (svc.clips_root / day / "Bench_6_xyz999.mp4").write_bytes(b"mp4-bytes")
                self.assertEqual(svc.check_calibration_milestones(), [])

            st = client.get("/api/status").get_json()
            self.assertEqual(st["calibration"]["clipCount"], 6)
            self.assertEqual(st["calibration"]["notified"], [5])
            self.assertEqual(st["calibration"]["nextMilestone"], 15)

            # Deleting clip removes local MP4, Google Drive MP4, and index.jsonl row
            del_res = client.post(
                "/api/clip/delete",
                json={"lanUrl": f"http://192.168.1.152:8766/clips/{day}/Squat_0_abc120.mp4", "fileName": "Squat_0_abc120.mp4"},
            )
            self.assertEqual(del_res.status_code, 200)
            body = del_res.get_json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["localDeleted"])
            self.assertTrue(body["driveDeleted"])
            self.assertFalse((svc.clips_root / day / "Squat_0_abc120.mp4").exists())
            self.assertFalse((drive_dir / day / "Squat_0_abc120.mp4").exists())
            remaining_names = [c["fileName"] for c in client.get("/api/clips").get_json()["clips"]]
            self.assertNotIn("Squat_0_abc120.mp4", remaining_names)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_alternate_angle_preview_endpoints_caching_isolation_and_pruning(self) -> None:
        from unittest import mock

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_prev_"))
        try:
            root = tmp / "camroot"
            drive_dir = tmp / "drive_clips"
            svc = ac.ArmoryCamService(root_dir=root, drive_backup_dir=drive_dir, ntfy_topic="TestTopic")
            app = ac.create_app(svc)
            client = app.test_client()

            t0_ms = int(datetime(2026, 11, 19, 10, 0, 0).timestamp() * 1000)
            for slug, color in (("rack_cam", "blue"), ("smith_cam", "green")):
                cam_dir = svc.tapes_root / "sess-alt" / slug
                cam_dir.mkdir(parents=True, exist_ok=True)
                for i in range(3):
                    seg_dt = datetime.fromtimestamp((t0_ms + i * 2000) / 1000.0)
                    seg_name = f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                    seg_path = cam_dir / seg_name
                    subprocess.run(
                        [
                            svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=20:d=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                        ],
                        check=True,
                    )

            ang_res = client.get(f"/api/tape/angles?sessionId=sess-alt&startMs={t0_ms}&endMs={t0_ms + 5000}")
            self.assertEqual(ang_res.status_code, 200)
            ang_data = ang_res.get_json()
            self.assertEqual(ang_data["retentionDays"], 14)
            self.assertEqual(ang_data["cameras"], ["Rack Cam", "Smith Cam"])

            # Non-overlapping timestamp & non-existent session report empty cameras
            miss_win = client.get(f"/api/tape/angles?sessionId=sess-alt&startMs={t0_ms + 120_000}&endMs={t0_ms + 130_000}").get_json()
            self.assertEqual(miss_win["cameras"], [])
            miss_sess = client.get(f"/api/tape/angles?sessionId=expired-sess&startMs={t0_ms}&endMs={t0_ms + 5000}").get_json()
            self.assertEqual(miss_sess["cameras"], [])

            with mock.patch.object(ac, "send_ntfy_alert", return_value=True) as m_ntfy:
                p1 = client.post(
                    "/api/tape/preview",
                    json={"sessionId": "sess-alt", "startMs": t0_ms, "endMs": t0_ms + 4000, "camera": "Smith Cam"},
                )
                self.assertEqual(p1.status_code, 200)
                d1 = p1.get_json()
                self.assertTrue(d1["ok"])
                self.assertTrue(d1["ephemeral"])
                self.assertFalse(d1["cached"])
                self.assertTrue(d1["lanUrl"].startswith("/previews/"))
                self.assertEqual(d1["camera"], "Smith Cam")
                self.assertGreater(d1["bytes"], 1000)

                # Byte-range 206 streaming + path traversal block on /previews/
                r206 = client.get(d1["lanUrl"], headers={"Range": "bytes=0-511"})
                self.assertEqual(r206.status_code, 206)
                self.assertEqual(len(r206.data), 512)
                self.assertEqual(r206.headers["Accept-Ranges"], "bytes")
                self.assertIn(client.get("/previews/../../config.json").status_code, (403, 404))

                # Zero side effects: no permanent clip, no Drive file, no index row, no calibration bump, no ntfy
                self.assertEqual(list(svc.clips_root.glob("*/*.mp4")), [])
                self.assertFalse(drive_dir.exists() and any(drive_dir.rglob("*")))
                self.assertFalse(svc.clips_index_path.exists() and bool(svc.clips_index_path.read_text(encoding="utf-8").strip()))
                self.assertEqual(svc.calibration_status()["clipCount"], 0)
                self.assertEqual(m_ntfy.call_count, 0)

                # Second call hits cached file
                p2 = client.post(
                    "/api/tape/preview",
                    json={"sessionId": "sess-alt", "startMs": t0_ms, "endMs": t0_ms + 4000, "camera": "Smith Cam"},
                )
                self.assertEqual(p2.status_code, 200)
                d2 = p2.get_json()
                self.assertTrue(d2["cached"])
                self.assertEqual(d2["fileName"], d1["fileName"])

                # Updating crop creates a fresh preview key
                client.post(
                    "/api/config",
                    json={"cameras": [{"name": "Smith Cam", "slug": "smith_cam", "crop": [0.1, 0.1, 0.9, 0.9]}]},
                )
                p3 = client.post(
                    "/api/tape/preview",
                    json={"sessionId": "sess-alt", "startMs": t0_ms, "endMs": t0_ms + 4000, "camera": "Smith Cam"},
                )
                self.assertEqual(p3.status_code, 200)
                d3 = p3.get_json()
                self.assertFalse(d3["cached"])
                self.assertNotEqual(d3["fileName"], d1["fileName"])

            # Unknown camera returns 404 (must not fall back to Rack Cam)
            ghost = client.post(
                "/api/tape/preview",
                json={"sessionId": "sess-alt", "startMs": t0_ms, "endMs": t0_ms + 4000, "camera": "Ghost Cam"},
            )
            self.assertEqual(ghost.status_code, 404)

            # Old ephemeral previews (>24h) are pruned while fresh ones remain
            old_path = svc.previews_root / d1["fileName"]
            fresh_path = svc.previews_root / d3["fileName"]
            stale_ts = time.time() - 25 * 3600
            os.utime(old_path, (stale_ts, stale_ts))
            svc._prune_storage(time.time())
            self.assertFalse(old_path.exists())
            self.assertTrue(fresh_path.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_virtual_views_shared_capture_independent_motion_and_coach_angles(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_virt_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            dash_html = client.get("/").get_data(as_text=True)
            self.assertIn("data-addview", dash_html)
            self.assertIn("sourceSlug", dash_html)

            res = client.post(
                "/api/config",
                json={
                    "cameras": [
                        {
                            "name": "Rack Cam",
                            "slug": "rack_cam",
                            "rtspUrl": "",
                            "enabled": True,
                            "motionThreshold": 2.0,
                            "crop": [0.0, 0.0, 1.0, 1.0],
                            "defish": False,
                        },
                        {
                            "name": "Smith Cam",
                            "slug": "smith_cam",
                            "rtspUrl": "",
                            "enabled": True,
                            "motionThreshold": 2.0,
                        },
                        {
                            "name": "Fly Machine Cam",
                            "slug": "fly_machine_cam",
                            "sourceSlug": "rack_cam",
                            "rtspUrl": "rtsp://should-be-cleared",
                            "enabled": True,
                            "motionThreshold": 2.0,
                            "crop": [0.0, 0.0, 0.45, 1.0],
                            "defish": False,
                        },
                        {
                            "name": "Smith Wide",
                            "slug": "smith_wide",
                            "sourceSlug": "rack_cam",
                            "enabled": True,
                            "motionThreshold": 2.0,
                            "crop": [0.55, 0.0, 1.0, 1.0],
                            "defish": False,
                        },
                    ]
                },
            )
            self.assertEqual(res.status_code, 200)
            saved_cfg = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            fly_cfg = next(c for c in saved_cfg["cameras"] if c["slug"] == "fly_machine_cam")
            self.assertEqual(fly_cfg["sourceSlug"], "rack_cam")
            self.assertEqual(fly_cfg["rtspUrl"], "")
            self.assertNotIn("primary", fly_cfg)

            self.assertEqual(
                [c.slug for c in svc._physical_cameras()],
                ["rack_cam", "smith_cam"],
            )
            self.assertFalse(svc._is_virtual(svc._find_camera("Rack Cam")))
            self.assertTrue(svc._is_virtual(svc._find_camera("Fly Machine Cam")))
            self.assertEqual(svc._source_slug_for(svc._find_camera("Fly Machine Cam")), "rack_cam")

            self.assertEqual(client.post("/api/session/start", json={"sessionId": "sess-virt", "name": "Virt Lift"}).status_code, 200)

            # Create 3 x 2s segments in ring/rack_cam only: seg 0 & 2 quiet, seg 1 & 2 have motion strictly in left 45%
            t0_dt = datetime(2026, 11, 19, 14, 0, 0)
            t0_ms = int(t0_dt.timestamp() * 1000)
            ring_rack = svc.ring_root / "rack_cam"
            ring_rack.mkdir(parents=True, exist_ok=True)
            for i in range(3):
                seg_dt = datetime.fromtimestamp((t0_ms + i * 2000) / 1000.0)
                seg_path = ring_rack / f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                vf_src = (
                    "color=c=black:s=320x240:r=20:d=2,drawbox=x=10:y=10:w=120:h=220:color=white@1.0:t=fill:enable='gte(n,2)'"
                    if i in (1, 2)
                    else "color=c=black:s=320x240:r=20:d=2"
                )
                subprocess.run(
                    [
                        svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", vf_src,
                        "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                    ],
                    check=True,
                )
                old_m = time.time() - 5.0
                os.utime(seg_path, (old_m, old_m))

            rack_phys = svc._find_camera("Rack Cam")
            svc._ingest_closed_segments(rack_phys, time.time())

            self.assertGreater(svc.latest_energy["fly_machine_cam"], 8.0)
            self.assertEqual(svc.latest_energy["smith_wide"], 0.0)
            self.assertTrue((svc.tapes_root / "sess-virt" / "rack_cam").is_dir())
            self.assertFalse((svc.tapes_root / "sess-virt" / "fly_machine_cam").exists())
            self.assertFalse((svc.tapes_root / "sess-virt" / "smith_wide").exists())

            # All enabled cameras participate equally (no primary/secondary suppression)
            auto_bursts = client.get("/api/bursts?sessionId=sess-virt").get_json()["bursts"]
            self.assertTrue(auto_bursts)
            self.assertIn(auto_bursts[0]["camera"], ("Rack Cam", "Fly Machine Cam"))
            fly_bursts = client.get("/api/bursts?sessionId=sess-virt&camera=Fly%20Machine%20Cam").get_json()["bursts"]
            self.assertEqual(len(fly_bursts), 1)
            self.assertEqual(fly_bursts[0]["camera"], "Fly Machine Cam")

            # Coach View tape angles include virtual views sharing Rack Cam tape
            ang_data = client.get(f"/api/tape/angles?sessionId=sess-virt&startMs={t0_ms}&endMs={t0_ms + 5000}").get_json()
            self.assertEqual(ang_data["cameras"], ["Rack Cam", "Fly Machine Cam", "Smith Wide"])

            # Cutting explicit clip on Fly Machine Cam uses virtual view's own [0,0,0.45,1] crop -> 144x240
            clip_res = client.post(
                "/api/clip",
                json={
                    "sessionId": "sess-virt",
                    "exName": "Chest Fly Machine",
                    "startMs": t0_ms + 2000,
                    "endMs": t0_ms + 5500,
                    "camera": "Fly Machine Cam",
                },
            )
            self.assertEqual(clip_res.status_code, 200)
            clip_data = clip_res.get_json()
            self.assertEqual(clip_data["camera"], "Fly Machine Cam")
            out_file = svc.clips_root / clip_data["lanUrl"].removeprefix("/clips/")
            probe_out = subprocess.check_output(
                [
                    svc.ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x", str(out_file),
                ],
                text=True,
            ).strip()
            self.assertEqual(probe_out, "144x240")

            # Snapshot & ephemeral preview also work on virtual views
            snap = client.get("/api/snapshot?camera=Fly%20Machine%20Cam")
            self.assertEqual(snap.status_code, 200)
            self.assertTrue(snap.data.startswith(b"\xff\xd8"))
            prev_res = client.post(
                "/api/tape/preview",
                json={"sessionId": "sess-virt", "startMs": t0_ms, "endMs": t0_ms + 3000, "camera": "Smith Wide"},
            )
            self.assertEqual(prev_res.status_code, 200)
            self.assertEqual(prev_res.get_json()["camera"], "Smith Wide")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tilt_normalization_vf_and_frame_parity(self) -> None:
        self.assertEqual(ac.normalize_tilt(None), 0.0)
        self.assertEqual(ac.normalize_tilt("nan"), 0.0)
        self.assertEqual(ac.normalize_tilt(0.02), 0.0)
        self.assertEqual(ac.normalize_tilt(-60), -45.0)
        self.assertEqual(ac.normalize_tilt(12.34), 12.3)
        self.assertEqual(ac.normalize_tilt(-3.5), -3.5)

        self.assertIsNone(ac.build_ffmpeg_vf((0.0, 0.0, 1.0, 1.0), rotate=0, tilt=0.0))
        vf = ac.build_ffmpeg_vf((0.1, 0.1, 0.9, 0.9), rotate=0, tilt=6.5)
        self.assertIsNotNone(vf)
        self.assertIn("rotate=", vf)
        self.assertIn("crop=", vf)
        self.assertLess(vf.index("rotate="), vf.index("crop="))

        w, h = 320, 180
        img = np.zeros((h, w, 3), dtype=np.uint8)
        cv2.rectangle(img, (24, 24), (296, 156), (180, 110, 60), 4)
        cv2.line(img, (160, 0), (160, 180), (50, 210, 90), 3)

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_tilt_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            inp = tmp / "in.png"
            outp = tmp / "out.png"
            cv2.imwrite(str(inp), img)

            # Exact pixel & shape parity across rotate + tilt before crop
            for rot_deg, tlt_deg in ((0, -8.5), (90, 12.5)):
                cv_rot = ac.apply_frame_transform(img, crop=(0.0, 0.0, 1.0, 1.0), rotate=rot_deg, apply_crop=False, tilt=tlt_deg)
                vf_rot = ac.build_ffmpeg_vf(crop=(0.0, 0.0, 1.0, 1.0), rotate=rot_deg, tilt=tlt_deg)
                self.assertIsNotNone(vf_rot)
                subprocess.run(
                    [svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(inp), "-vf", vf_rot, "-frames:v", "1", str(outp)],
                    check=True,
                )
                ff_rot = cv2.imread(str(outp))
                self.assertEqual(cv_rot.shape, ff_rot.shape)
                mad = float(np.mean(np.abs(cv_rot.astype(float) - ff_rot.astype(float))))
                self.assertLess(mad, 1.5)

            # Combined rotate + tilt + crop stays aligned within <=2px even-truncation tolerance
            crop = (0.1, 0.1, 0.9, 0.9)
            cv_crop = ac.apply_frame_transform(img, crop=crop, rotate=90, apply_crop=True, tilt=6.5)
            vf_crop = ac.build_ffmpeg_vf(crop=crop, rotate=90, tilt=6.5)
            self.assertIsNotNone(vf_crop)
            self.assertLess(vf_crop.index("transpose=1"), vf_crop.index("rotate="))
            self.assertLess(vf_crop.index("rotate="), vf_crop.index("crop="))
            subprocess.run(
                [svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(inp), "-vf", vf_crop, "-frames:v", "1", str(outp)],
                check=True,
            )
            ff_crop = cv2.imread(str(outp))
            self.assertEqual(cv_crop.shape, ff_crop.shape)
            self.assertLess(float(np.mean(np.abs(cv_crop.astype(float) - ff_crop.astype(float)))), 1.5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_virtual_views_independent_tilt_config_cache_and_clip_encode(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_virt_tilt_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            dash_html = client.get("/").get_data(as_text=True)
            self.assertIn("data-tiltstep", dash_html)
            self.assertIn("data-tiltreset", dash_html)
            self.assertIn("level-grid", dash_html)
            for fs_marker in (
                "openEditorTop",
                "data-fullscreen",
                "mmViewSel",
                "mmModeCrop",
                "mmModeMask",
                "mmPreviewBtn",
                "mmTiltRange",
                "mmGridBtn",
                "mmResetCropBtn",
            ):
                self.assertIn(fs_marker, dash_html)

            cfg_res = client.post(
                "/api/config",
                json={
                    "cameras": [
                        {
                            "name": "Rack Cam",
                            "slug": "rack_cam",
                            "rtspUrl": "rtsp://127.0.0.1:554/rack",
                            "enabled": True,
                            "motionThreshold": 2.2,
                            "crop": [0.0, 0.0, 1.0, 1.0],
                            "rotate": 0,
                            "tilt": -2.5,
                            "defish": False,
                        },
                        {
                            "name": "Fly Machine Cam",
                            "slug": "fly_machine_cam",
                            "sourceSlug": "rack_cam",
                            "rtspUrl": "",
                            "enabled": True,
                            "motionThreshold": 2.2,
                            "crop": [0.0, 0.1, 0.45, 0.9],
                            "rotate": 0,
                            "tilt": 6.0,
                            "defish": False,
                        },
                    ]
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            cfg_data = client.get("/api/config").get_json()
            by_slug = {c["slug"]: c for c in cfg_data["cameras"]}
            self.assertAlmostEqual(by_slug["rack_cam"]["tilt"], -2.5, places=1)
            self.assertAlmostEqual(by_slug["fly_machine_cam"]["tilt"], 6.0, places=1)

            cam_zero = ac.CameraConfig(name="Z", slug="z", crop=(0.0, 0.0, 1.0, 1.0), rotate=0, tilt=0.0, defish=False)
            cam_tilted = ac.CameraConfig(name="T", slug="t", crop=(0.0, 0.0, 1.0, 1.0), rotate=0, tilt=6.0, defish=False)
            cam_defish = ac.CameraConfig(name="D", slug="d", crop=(0.0, 0.0, 1.0, 1.0), rotate=0, tilt=0.0, defish=True)
            self.assertEqual(svc._crop_cache_key(cam_zero), "r0_0_0_1000_1000")
            self.assertEqual(svc._crop_cache_key(cam_tilted), "r0_0_0_1000_1000_t60")
            self.assertEqual(svc._crop_cache_key(cam_defish), "r0_0_0_1000_1000_d1")

            self.assertEqual(client.post("/api/session/start", json={"sessionId": "sess-tilt", "name": "Tilt Lift"}).status_code, 200)
            t0_dt = datetime(2026, 11, 19, 15, 0, 0)
            t0_ms = int(t0_dt.timestamp() * 1000)
            ring_rack = svc.ring_root / "rack_cam"
            ring_rack.mkdir(parents=True, exist_ok=True)
            for i in range(2):
                seg_dt = datetime.fromtimestamp((t0_ms + i * 2000) / 1000.0)
                seg_path = ring_rack / f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                vf_src = "color=c=black:s=320x240:r=20:d=2,drawbox=x=10:y=20:w=110:h=180:color=white@1.0:t=fill:enable='gte(n,2)'"
                subprocess.run(
                    [
                        svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", vf_src,
                        "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                    ],
                    check=True,
                )
                old_m = time.time() - 5.0
                os.utime(seg_path, (old_m, old_m))

            svc._ingest_closed_segments(svc._find_camera("Rack Cam"), time.time())
            self.assertGreater(svc.latest_energy["fly_machine_cam"], 5.0)

            snap = client.get("/api/snapshot?camera=Fly%20Machine%20Cam&raw=1&hires=1&tilt=4.5")
            self.assertEqual(snap.status_code, 200)
            snap_img = cv2.imdecode(np.frombuffer(snap.data, np.uint8), cv2.IMREAD_COLOR)
            self.assertIsNotNone(snap_img)
            exp_w, exp_h = ac.tilted_frame_size(320, 240, 4.5)
            self.assertEqual((snap_img.shape[1], snap_img.shape[0]), (exp_w, exp_h))

            snap_crop = client.get("/api/snapshot?camera=Fly%20Machine%20Cam&raw=0&hires=1&tilt=4.5&crop=0.1,0.1,0.7,0.8")
            self.assertEqual(snap_crop.status_code, 200)
            snap_crop_img = cv2.imdecode(np.frombuffer(snap_crop.data, np.uint8), cv2.IMREAD_COLOR)
            self.assertIsNotNone(snap_crop_img)
            exp_cropped = ac.apply_frame_transform(
                np.zeros((240, 320, 3), dtype=np.uint8),
                crop=(0.1, 0.1, 0.7, 0.8),
                rotate=0,
                apply_crop=True,
                tilt=4.5,
            )
            self.assertEqual(snap_crop_img.shape, exp_cropped.shape)

            clip_res = client.post(
                "/api/clip",
                json={
                    "sessionId": "sess-tilt",
                    "exName": "Chest Fly Machine",
                    "startMs": t0_ms,
                    "endMs": t0_ms + 3500,
                    "camera": "Fly Machine Cam",
                },
            )
            self.assertEqual(clip_res.status_code, 200)
            clip_data = clip_res.get_json()
            out_file = svc.clips_root / clip_data["lanUrl"].removeprefix("/clips/")
            self.assertTrue(out_file.is_file())
            self.assertGreater(out_file.stat().st_size, 1000)
            probe_out = subprocess.check_output(
                [
                    svc.ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x", str(out_file),
                ],
                text=True,
            ).strip()
            pw, ph = (int(x) for x in probe_out.split("x"))
            self.assertGreater(pw, 0)
            self.assertGreater(ph, 0)
            self.assertEqual(pw % 2, 0)
            self.assertEqual(ph % 2, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bump_ntfy_only_and_no_autolevel_realign_primary_artifacts(self) -> None:
        # 1. Lightweight phase-correlation bump detector: shift detected, lighting shift ignored
        w, h = 640, 360
        base = np.full((h, w, 3), 24, dtype=np.uint8)
        for x in range(80, 580, 60):
            cv2.line(base, (x, 20), (x, 340), (225, 225, 225), 4)
        for y in range(40, 340, 55):
            cv2.line(base, (60, y), (580, y), (185, 185, 185), 3)
        for k, (cx, cy) in enumerate([(130, 90), (270, 150), (410, 110), (500, 230), (210, 270), (350, 250)]):
            cv2.putText(base, f"R{k}", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (90, 210, 255), 2)
            cv2.circle(base, (cx + 18, cy + 16), 14, (240, 140, 70), 2)

        ref_gray_small = cv2.resize(cv2.cvtColor(base, cv2.COLOR_BGR2GRAY), ac.BUMP_REF_SIZE, interpolation=cv2.INTER_AREA)

        bright = np.clip(base.astype(np.int16) + 35, 0, 255).astype(np.uint8)
        res_bright = ac.detect_camera_bump(ref_gray_small, bright)
        self.assertTrue(res_bright["ok"], res_bright)
        self.assertFalse(res_bright["bumped"], res_bright)

        m_shift = np.float32([[1, 0, 28.0], [0, 1, -12.0]])
        shifted = cv2.warpAffine(base, m_shift, (w, h), borderValue=(24, 24, 24))
        res_shift = ac.detect_camera_bump(ref_gray_small, shifted)
        self.assertTrue(res_shift["ok"], res_shift)
        self.assertTrue(res_shift["bumped"], res_shift)
        self.assertGreater(res_shift["shiftPx"], ac.BUMP_PX_THRESHOLD)

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_bump_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp, ntfy_topic="TestBumpTopic")
            app = ac.create_app(svc)
            client = app.test_client()

            dash_html = client.get("/").get_data(as_text=True)
            self.assertIn("bumpAlertNtfy", dash_html)
            self.assertIn("defish-", dash_html)
            for removed in (
                "alignBar",
                "pinRefBtn",
                "realignModeBtn",
                "realignNowBtn",
                "driftNtfyBtn",
                "autoLvl-",
                "autoAxis-",
                "mmAutoLvlBtn",
                "mmAutoAxisSel",
                "mmRealignModeBtn",
                "mmRealignNowBtn",
                "mmDriftNtfyBtn",
                "Secondary / Pinned",
            ):
                self.assertNotIn(removed, dash_html)

            # Removed alignment endpoints must be gone (404/405 due to OPTIONS catch-all)
            for dead_url in ("/api/camera/auto-level", "/api/camera/reference", "/api/camera/realign"):
                self.assertIn(client.post(dead_url, json={}).status_code, (404, 405))

            # Legacy primary/autoLevel/realignMode/driftAlertNtfy keys are ignored & stripped from config
            cfg_res = client.post(
                "/api/config",
                json={
                    "bumpAlertNtfy": True,
                    "realignMode": "auto",
                    "driftAlertNtfy": True,
                    "cameras": [
                        {
                            "name": "Rack Cam",
                            "slug": "rack_cam",
                            "rtspUrl": "rtsp://127.0.0.1:554/rack",
                            "enabled": True,
                            "primary": True,
                            "autoLevel": True,
                            "autoLevelAxis": "vertical",
                            "crop": [0.25, 0.15, 0.75, 0.85],
                            "defish": True,
                        },
                        {
                            "name": "Fly Machine Cam",
                            "slug": "fly_machine_cam",
                            "sourceSlug": "rack_cam",
                            "enabled": True,
                            "primary": False,
                            "crop": [0.02, 0.25, 0.25, 0.88],
                            "defish": True,
                        },
                    ],
                },
            )
            self.assertEqual(cfg_res.status_code, 200)
            got_cfg = client.get("/api/config").get_json()
            self.assertTrue(got_cfg["bumpAlertNtfy"])
            self.assertNotIn("realignMode", got_cfg)
            self.assertNotIn("driftAlertNtfy", got_cfg)
            self.assertNotIn("alignment", got_cfg)
            for c_obj in got_cfg["cameras"]:
                self.assertNotIn("primary", c_obj)
                self.assertNotIn("autoLevel", c_obj)
                self.assertNotIn("autoLevelAxis", c_obj)
                self.assertTrue(c_obj["defish"])

            # Seed baseline in bump_refs/<slug>.png from unshifted frame
            ring_rack = svc.ring_root / "rack_cam"
            ring_rack.mkdir(parents=True, exist_ok=True)
            t0_ms = int(datetime(2026, 11, 19, 16, 0, 0).timestamp() * 1000)

            def _write_img_ts(img_bgr: np.ndarray, ts_ms: int) -> Path:
                png_p = tmp / f"frame_{ts_ms}.png"
                seg_dt = datetime.fromtimestamp(ts_ms / 1000.0)
                seg_p = ring_rack / f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                cv2.imwrite(str(png_p), img_bgr)
                subprocess.run(
                    [
                        svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-loop", "1", "-t", "1.2", "-i", str(png_p),
                        "-r", "15", "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p",
                        "-f", "mpegts", str(seg_p),
                    ],
                    check=True,
                )
                return seg_p

            ref_seg = _write_img_ts(base, t0_ms)
            svc.cam_connected["rack_cam"] = True
            svc.ring_segments["rack_cam"] = [ac.SegmentInfo(path=ref_seg, start_ms=t0_ms, end_ms=t0_ms + 1200, energy=0.0, camera="Rack Cam")]
            svc.latest_energy = {"rack_cam": 0.1, "fly_machine_cam": 0.1}

            # First quiet tick lazily saves baseline PNG in bump_refs without firing ntfy
            self.assertEqual(svc.check_camera_bump_background(900.0), [])
            self.assertTrue((svc.bump_refs_root / "rack_cam.png").is_file())
            self.assertFalse((svc.root / "refs").exists())

            # Bumped segment: requires 2 consecutive quiet strikes + respects 6h cooldown
            bumped_seg = _write_img_ts(shifted, t0_ms + 4000)
            svc.ring_segments["rack_cam"].append(
                ac.SegmentInfo(path=bumped_seg, start_ms=t0_ms + 4000, end_ms=t0_ms + 5200, energy=0.0, camera="Rack Cam")
            )
            with mock.patch.object(ac, "send_ntfy_alert", return_value=True) as m_ntfy:
                self.assertEqual(svc.check_camera_bump_background(1000.0), [])
                self.assertEqual(m_ntfy.call_count, 0)

                ev2 = svc.check_camera_bump_background(1035.0)
                self.assertEqual(len(ev2), 1)
                self.assertTrue(ev2[0]["notified"])
                time.sleep(0.15)
                self.assertEqual(m_ntfy.call_count, 1)

                # Within 6h cooldown -> no second alert
                svc._bump_strikes["rack_cam"] = 1
                svc.check_camera_bump_background(1070.0)
                time.sleep(0.1)
                self.assertEqual(m_ntfy.call_count, 1)

                # When bumpAlertNtfy is disabled -> zero bump image processing occurs
                svc.bump_alert_ntfy = False
                with mock.patch.object(ac, "detect_camera_bump") as m_det:
                    self.assertEqual(svc.check_camera_bump_background(50_000.0), [])
                    m_det.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_v360_defish_parity_cache_and_clip_encode(self) -> None:
        w, h = 640, 360
        src = np.full((h, w, 3), 24, dtype=np.uint8)
        for x in range(40, w, 60):
            cv2.line(src, (x, 10), (x, h - 10), (220, 220, 220), 3)
        for y in range(40, h, 50):
            cv2.line(src, (10, y), (w - 10, y), (140, 190, 240), 2)
        cv2.circle(src, (160, 180), 42, (80, 220, 120), 3)

        crop = (0.04, 0.20, 0.42, 0.88)
        tilt = 1.7
        vf = ac.build_ffmpeg_vf(crop=crop, rotate=0, tilt=tilt, defish=True, src_size=(w, h))
        self.assertIsNotNone(vf)
        self.assertIn("v360=input=fisheye", vf)

        cv_out = ac.apply_frame_transform(src, crop=crop, rotate=0, apply_crop=True, tilt=tilt, defish=True)
        _, png_bytes = cv2.imencode(".png", src)
        ff = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        proc = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "png", "-i", "-", "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"],
            input=png_bytes.tobytes(),
            capture_output=True,
            check=True,
        )
        ff_out = cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(cv_out.shape, ff_out.shape)
        mad = float(np.mean(np.abs(cv_out.astype(np.float32) - ff_out.astype(np.float32))))
        self.assertLess(mad, 5.0)

        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_v360_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            client.post("/api/config", json={
                "cameras": [{
                    "name": "Rack Cam",
                    "slug": "rack_cam",
                    "enabled": True,
                    "crop": [0.20, 0.15, 0.80, 0.90],
                    "tilt": 1.5,
                    "defish": True,
                }]
            })
            t0_ms = int(datetime(2026, 11, 20, 9, 0, 0).timestamp() * 1000)
            seg_dir = svc.tapes_root / "sess-v360" / "rack_cam"
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_p = seg_dir / f"seg_{datetime.fromtimestamp(t0_ms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts"
            png_p = tmp / "in.png"
            cv2.imwrite(str(png_p), src)
            subprocess.run(
                [svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-t", "2.0", "-i", str(png_p),
                 "-r", "15", "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_p)],
                check=True,
            )
            res = client.post("/api/clip", json={
                "sessionId": "sess-v360",
                "exName": "Squat",
                "startMs": t0_ms,
                "endMs": t0_ms + 2000,
                "camera": "Rack Cam",
            })
            self.assertEqual(res.status_code, 200)
            mp4 = svc.clips_root / res.get_json()["lanUrl"].removeprefix("/clips/")
            self.assertTrue(mp4.is_file())
            self.assertGreater(mp4.stat().st_size, 1000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multi_camera_clip_and_auto_pr(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_multi_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            client.post("/api/config", json={
                "cameras": [
                    {"name": "Rack Cam", "slug": "rack_cam", "enabled": True, "crop": [0.25, 0.1, 0.75, 0.9], "defish": False},
                    {"name": "Smith Cam", "slug": "smith_cam", "enabled": True, "crop": [0.25, 0.0, 0.75, 1.0], "defish": False},
                    {"name": "Smith Wide", "slug": "smith_wide", "sourceSlug": "rack_cam", "enabled": True, "crop": [0.45, 0.0, 0.95, 1.0], "defish": False},
                ]
            })
            t0_ms = int(datetime(2026, 11, 20, 10, 0, 0).timestamp() * 1000)
            rack_dir = svc.tapes_root / "sess-multi" / "rack_cam"
            rack_dir.mkdir(parents=True, exist_ok=True)
            motion_lines = []
            for i in range(3):
                seg_dt = datetime.fromtimestamp((t0_ms + i * 2000) / 1000.0)
                seg_p = rack_dir / f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                subprocess.run(
                    [svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"color=c={'white' if i == 1 else 'black'}:s=320x240:r=15:d=2",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_p)],
                    check=True,
                )
                motion_lines.append(json.dumps({
                    "camera": "Rack Cam", "slug": "rack_cam", "file": seg_p.name,
                    "startMs": t0_ms + i * 2000, "endMs": t0_ms + (i + 1) * 2000,
                    "energy": 9.0 if i == 1 else 0.2,
                }))
            (svc.tapes_root / "sess-multi" / "motion.jsonl").write_text("\n".join(motion_lines) + "\n", encoding="utf-8")

            res = client.post("/api/clip", json={
                "sessionId": "sess-multi",
                "exName": "Standing Calf Raise",
                "startMs": t0_ms + 1000,
                "endMs": t0_ms + 5000,
                "cameras": ["Rack Cam", "Smith Wide"],
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(len(data["clips"]), 2)
            self.assertEqual([c["camera"] for c in data["clips"]], ["Rack Cam", "Smith Wide"])
            for item in data["clips"]:
                self.assertTrue((svc.clips_root / item["lanUrl"].removeprefix("/clips/")).is_file())

            pr_res = client.post("/api/clip/auto-pr", json={
                "sessionId": "sess-multi",
                "exName": "Pull-up",
                "setAtMs": t0_ms + 5000,
                "cameras": ["Rack Cam", "Smith Wide"],
            })
            self.assertEqual(pr_res.status_code, 200)
            pr_data = pr_res.get_json()
            self.assertEqual(len(pr_data["clips"]), 2)
            self.assertEqual(pr_data["clips"][0]["startMs"], pr_data["clips"][1]["startMs"])
            self.assertEqual(pr_data["clips"][0]["endMs"], pr_data["clips"][1]["endMs"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_drive_preview_overview_render_routes_and_ping_fields(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_preview_"))
        try:
            drive_clips = tmp / "drive" / "Clips"
            drive_clips.mkdir(parents=True, exist_ok=True)
            drive_preview = drive_clips.parent / ac.PREVIEW_DRIVE_FILENAME
            sentinel_bytes = b"sentinel-keep-me"
            drive_preview.write_bytes(sentinel_bytes)

            svc = ac.ArmoryCamService(root_dir=tmp / "root", drive_backup_dir=drive_clips)
            app = ac.create_app(svc)
            client = app.test_client()

            client.post(
                "/api/config",
                json={
                    "cameras": [
                        {
                            "name": "Rack Cam",
                            "slug": "rack_cam",
                            "rtspUrl": "rtsp://127.0.0.1:554/rack",
                            "enabled": True,
                            "motionThreshold": 2.2,
                            "crop": [0.0, 0.0, 1.0, 1.0],
                            "rotate": 0,
                            "tilt": -1.5,
                        },
                        {
                            "name": "Fly Machine Cam",
                            "slug": "fly_machine_cam",
                            "sourceSlug": "rack_cam",
                            "rtspUrl": "",
                            "enabled": True,
                            "motionThreshold": 2.2,
                            "crop": [0.0, 0.05, 0.45, 0.95],
                            "rotate": 0,
                            "tilt": 5.0,
                        },
                    ]
                },
            )

            # With no live frames/segments, refresh returns 503 and preserves existing Drive preview
            no_res = client.post("/api/preview/refresh")
            self.assertEqual(no_res.status_code, 503)
            self.assertFalse(no_res.get_json()["ok"])
            self.assertEqual(drive_preview.read_bytes(), sentinel_bytes)

            # Generate synthetic MPEG-TS segment for Rack Cam
            t0_ms = int(datetime(2026, 11, 20, 6, 30, 0).timestamp() * 1000)
            seg_dir = svc.ring_root / "rack_cam"
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_p = seg_dir / "seg_20261120_063000.ts"
            png_p = tmp / "frame.png"
            img = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.rectangle(img, (20, 20), (620, 340), (60, 130, 200), 4)
            cv2.line(img, (320, 0), (320, 360), (90, 220, 120), 3)
            cv2.imwrite(str(png_p), img)
            subprocess.run(
                [
                    svc.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-loop", "1", "-t", "1.0", "-i", str(png_p),
                    "-r", "15", "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p",
                    "-f", "mpegts", str(seg_p),
                ],
                check=True,
            )
            seg_info = ac.SegmentInfo(path=seg_p, start_ms=t0_ms, end_ms=t0_ms + 1000, energy=0.0, camera="Rack Cam")
            svc.ring_segments["rack_cam"] = [seg_info]
            svc.ring_segments["fly_machine_cam"] = [seg_info]

            fake_fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
            with mock.patch.object(ac, "read_google_drive_file_id", return_value=fake_fid):
                ref_res = client.post("/api/preview/refresh")
                self.assertEqual(ref_res.status_code, 200)
                ref_json = ref_res.get_json()
                self.assertTrue(ref_json["ok"])
                self.assertEqual(ref_json["views"], 2)
                self.assertEqual(ref_json["liveViews"], 2)
                self.assertEqual(ref_json["driveUrl"], f"https://drive.google.com/file/d/{fake_fid}/view")
                self.assertIsNotNone(ref_json["updatedMs"])

                self.assertTrue(drive_preview.is_file())
                self.assertTrue(svc.preview_local_path.is_file())
                decoded = cv2.imread(str(drive_preview))
                self.assertIsNotNone(decoded)
                self.assertGreaterEqual(decoded.shape[1], 1500)

                jpg_res = client.get("/api/preview.jpg")
                self.assertEqual(jpg_res.status_code, 200)
                self.assertTrue(jpg_res.data.startswith(b"\xff\xd8"))
                self.assertEqual(jpg_res.headers.get("Cache-Control"), "no-store")

                ping_json = client.get("/api/ping").get_json()
                self.assertEqual(ping_json["previewDriveUrl"], f"https://drive.google.com/file/d/{fake_fid}/view")
                self.assertIsNotNone(ping_json["previewUpdatedMs"])

                status_json = client.get("/api/status").get_json()
                self.assertEqual(status_json["preview"]["driveUrl"], f"https://drive.google.com/file/d/{fake_fid}/view")
                self.assertIsNotNone(status_json["preview"]["updatedMs"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_room_voice_helpers_config_sse_and_ack(self) -> None:
        # Pure helpers: wake matching, hallucination guard, host API priority, and prompt seeding
        self.assertTrue(rv.matches_wake_word("Armory 225 for 8"))
        self.assertTrue(rv.matches_wake_word("Hey army, check"))
        self.assertFalse(rv.matches_wake_word("just chatting between sets"))
        self.assertTrue(rv.has_command_tail("Armory 225 for 8"))
        self.assertFalse(rv.has_command_tail("Armory"))
        self.assertTrue(rv.is_hallucination("Thank you."))
        self.assertTrue(rv.is_hallucination("..."))
        self.assertFalse(rv.is_hallucination("Armory check"))

        prompt, hotwords = rv.build_whisper_prompt("armory", ["Barbell Bench Press", "Smith Machine Row"])
        self.assertIn("Barbell Bench Press", prompt)
        self.assertIn("Smith Machine Row", hotwords)

        fake_devs = {
            "inputs": [
                {"index": 11, "name": "Anker PowerConf S330 (Anker PowerConf S330)", "hostApi": "Windows WDM-KS", "channels": 2, "sampleRate": 48000},
                {"index": 15, "name": "Anker PowerConf S330 (Anker PowerConf S330)", "hostApi": "Windows WASAPI", "channels": 2, "sampleRate": 48000},
            ],
            "outputs": [
                {"index": 10, "name": "Speakers (Anker PowerConf S330)", "hostApi": "Windows WDM-KS", "channels": 2, "sampleRate": 48000},
            ],
        }
        picked_in = rv.resolve_audio_device("in", "PowerConf S330", fake_devs)
        self.assertIsNotNone(picked_in)
        self.assertEqual(picked_in["hostApi"], "Windows WASAPI")
        picked_out = rv.resolve_audio_device("out", "PowerConf S330", fake_devs)
        self.assertIsNotNone(picked_out)
        self.assertEqual(picked_out["hostApi"], "Windows WDM-KS")

        tmp = Path(tempfile.mkdtemp(prefix="armory_voice_test_"))
        spoken: list[str] = []
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.voice._speak_fn = lambda txt: (spoken.append(txt), True)[1]
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            self.assertIn("Room Voice Mic (PowerConf S330)", dash)
            self.assertIn("vxTestBtn", dash)
            self.assertIn("vxSpeakBtn", dash)
            self.assertIn("vxSaveBtn", dash)
            self.assertIn("vxTtsVoice", dash)
            self.assertIn("vxAbBox", dash)

            ping = client.get("/api/ping").get_json()
            self.assertIn("voice", ping)
            self.assertTrue(ping["voice"]["enabled"])
            self.assertFalse(ping["voice"]["listening"])

            # Default & legacy TTS voice normalization + config persistence across service reload
            self.assertEqual(rv.RoomVoiceConfig().tts_voice, "en-US-AndrewMultilingualNeural")
            self.assertEqual(
                rv.RoomVoiceConfig.from_dict({"ttsVoice": "en-US-AndrewNeural"}).tts_voice,
                "en-US-AndrewMultilingualNeural",
            )
            self.assertEqual(
                rv.RoomVoiceConfig.from_dict({"ttsVoice": "en-US-AvaNeural"}).tts_voice,
                "en-US-AvaMultilingualNeural",
            )
            self.assertEqual(
                rv.RoomVoiceConfig.from_dict({"ttsVoice": "en-US-BrianNeural"}).tts_voice,
                "en-US-AndrewMultilingualNeural",
            )

            cfg_res = client.post(
                "/api/voice/config",
                json={"listenMode": "always", "modelSize": "tiny.en", "vadThreshold": 0.55, "ttsVoice": "en-US-AvaMultilingualNeural"},
            )
            self.assertEqual(cfg_res.status_code, 200)
            self.assertEqual(cfg_res.get_json()["config"]["listenMode"], "always")
            self.assertEqual(cfg_res.get_json()["config"]["ttsVoice"], "en-US-AvaMultilingualNeural")

            svc_reload = ac.ArmoryCamService(root_dir=tmp)
            self.assertEqual(svc_reload.voice_cfg.listen_mode, "always")
            self.assertEqual(svc_reload.voice_cfg.model_size, "tiny.en")
            self.assertEqual(svc_reload.voice_cfg.tts_voice, "en-US-AvaMultilingualNeural")
            self.assertAlmostEqual(svc_reload.voice_cfg.vad_threshold, 0.55)

            # Context update + SSE stream + inject + ack
            ctx_res = client.post(
                "/api/voice/context",
                json={"active": True, "wakeWord": "armory", "biasPhrases": ["Low Bar Squat"], "readback": True},
            )
            self.assertEqual(ctx_res.status_code, 200)
            self.assertTrue(ctx_res.get_json()["status"]["contextActive"])

            svc.voice._stop_event.clear()
            stream = svc.voice.subscribe_sse()
            first_frame = next(stream).decode("utf-8")
            self.assertIn('"type":"status"', first_frame)

            inj_res = client.post("/api/voice/inject", json={"transcript": "Armory 225 for 8"})
            self.assertEqual(inj_res.status_code, 200)
            self.assertTrue(inj_res.get_json()["entry"]["emitted"])

            second_frame = next(stream).decode("utf-8")
            self.assertIn('"type":"transcript"', second_frame)
            self.assertIn("Armory 225 for 8", second_frame)
            stream.close()

            ack_res = client.post(
                "/api/voice/ack",
                json={"armed": False, "speech": "225 for 8, got it", "text": "✓ 1 Low Bar Squat · 225 lb × 8"},
            )
            self.assertEqual(ack_res.status_code, 200)
            self.assertTrue(ack_res.get_json()["spoke"])
            self.assertEqual(spoken, ["225 for 8, got it"])

            st = client.get("/api/voice/status").get_json()
            self.assertEqual(st["recent"][0]["ackText"], "✓ 1 Low Bar Squat · 225 lb × 8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_speaker_padding_and_dual_mic_ab_tracker_endpoints(self) -> None:
        # 1. Speaker PCM padding & peak normalization prevents clipping
        tone = 0.25 * np.sin(2.0 * np.pi * 440.0 * np.linspace(0, 0.5, 12_000, endpoint=False, dtype=np.float32))
        padded = rv.prepare_speaker_pcm(tone, src_sr=24_000, dst_sr=48_000, dst_ch=2, lead_ms=420, tail_ms=300, target_peak=0.84)
        self.assertEqual(padded.ndim, 2)
        self.assertEqual(padded.shape[1], 2)
        expected_min = int(round(48_000 * (0.42 + 0.50 + 0.30)))
        self.assertGreaterEqual(padded.shape[0], expected_min - 8)
        self.assertLessEqual(float(np.max(np.abs(padded))), 0.85)
        self.assertLess(float(np.max(np.abs(padded[:10_000]))), 0.01)
        self.assertAlmostEqual(float(np.max(np.abs(padded[-10_000:]))), 0.0, places=5)

        # 2. A/B Tracker pairing, missed-wake raw attachment, metrics, adjudication, shootout runs, and persistence
        tmp = Path(tempfile.mkdtemp(prefix="armory_ab_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            t0 = 1_763_600_000_000

            # Trial 1: Both match within 4.5s
            client.post("/api/voice/ab/sample", json={
                "source": "mic", "tsMs": t0, "transcript": "Armory 225 for 8",
                "matched": True, "intentKey": "log:225:8", "intentLabel": "225 × 8", "hasWake": True, "station": "Barbell Bench Press", "executed": True,
            })
            res1 = client.post("/api/voice/ab/sample", json={
                "source": "room", "tsMs": t0 + 900, "transcript": "Armory 225 for 8",
                "matched": True, "intentKey": "log:225:8", "intentLabel": "225 × 8", "hasWake": True, "station": "Barbell Bench Press", "executed": False,
            }).get_json()
            self.assertEqual(res1["trial"]["outcome"], "both_match")

            # Trial 2: Room catches command while Phone misses
            client.post("/api/voice/ab/sample", json={
                "source": "room", "tsMs": t0 + 20_000, "transcript": "Armory skip rest",
                "matched": True, "intentKey": "rest_skip", "intentLabel": "Skip rest", "hasWake": True, "station": "Barbell Bench Press", "executed": True,
            })
            res2 = client.post("/api/voice/ab/sample", json={
                "source": "mic", "tsMs": t0 + 20_400, "transcript": "armory ...",
                "matched": False, "intentKey": "none", "intentLabel": "Unmatched", "hasWake": True, "station": "Barbell Bench Press", "executed": False,
            }).get_json()
            self.assertEqual(res2["trial"]["outcome"], "room_only")

            # Trial 3: Phone catches, Room missed wake (raw transcript attached from recent_transcripts)
            svc.voice.recent_transcripts.appendleft({
                "id": 99, "tsMs": t0 + 40_200, "transcript": "225 for 8", "wakeMatched": False, "emitted": False, "latencyMs": 950, "ackText": "",
            })
            res3 = client.post("/api/voice/ab/sample", json={
                "source": "mic", "tsMs": t0 + 40_000, "transcript": "Armory 225 for 8",
                "matched": True, "intentKey": "log:225:8", "intentLabel": "225 × 8", "hasWake": True, "station": "Smith Machine Row", "executed": True,
            }).get_json()
            self.assertEqual(res3["trial"]["outcome"], "phone_only")
            self.assertEqual(res3["trial"]["room"]["transcript"], "225 for 8")

            # Trial 4: Disagreement adjudicated in Room's favor
            client.post("/api/voice/ab/sample", json={
                "source": "mic", "tsMs": t0 + 60_000, "transcript": "Armory for 6",
                "matched": True, "intentKey": "log:r:6", "intentLabel": "For 6", "hasWake": True, "station": "Smith Machine Row", "executed": True,
            })
            t4 = client.post("/api/voice/ab/sample", json={
                "source": "room", "tsMs": t0 + 61_000, "transcript": "Armory for 8",
                "matched": True, "intentKey": "log:r:8", "intentLabel": "For 8", "hasWake": True, "station": "Smith Machine Row", "executed": False,
            }).get_json()["trial"]
            self.assertEqual(t4["outcome"], "disagree")

            resolved = client.post("/api/voice/ab/resolve", json={"trialId": t4["id"], "winner": "room"}).get_json()
            s = resolved["summary"]
            self.assertEqual(s["totalTrials"], 4)
            self.assertEqual(s["roomWins"], 3)
            self.assertEqual(s["phoneWins"], 2)
            self.assertAlmostEqual(s["roomCatchPct"], 75.0)
            self.assertAlmostEqual(s["phoneCatchPct"], 50.0)
            self.assertAlmostEqual(s["deltaPts"], 25.0)
            self.assertAlmostEqual(s["relativeGainPct"], 50.0)
            self.assertAlmostEqual(s["agreementPct"], 50.0)

            # Save a Guided 10-Phrase Shootout run and verify persistence across service reload
            run_res = client.post("/api/voice/ab/run", json={
                "label": "S330 Walking Desk",
                "phrasesCount": 10,
                "roomIntentPct": 90.0,
                "phoneIntentPct": 60.0,
                "roomWordPct": 93.4,
                "phoneWordPct": 71.2,
                "items": [{"phrase": "Armory 225 for 8", "roomMatch": True, "phoneMatch": False}],
            }).get_json()
            self.assertTrue(run_res["ok"])
            self.assertAlmostEqual(run_res["run"]["deltaPts"], 30.0)

            svc_reload = ac.ArmoryCamService(root_dir=tmp)
            snap = svc_reload.voice.ab.snapshot_dict()
            self.assertEqual(snap["summary"]["totalTrials"], 4)
            self.assertEqual(snap["runs"][0]["label"], "S330 Walking Desk")

            # Reset passive trials while keeping saved shootout runs
            rst = client.post("/api/voice/ab/reset", json={"clearRuns": False}).get_json()
            self.assertEqual(rst["summary"]["totalTrials"], 0)
            self.assertEqual(len(client.get("/api/voice/ab").get_json()["runs"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_denon_xml_mv_and_loud_only_ducking(self) -> None:
        self.assertEqual(ha.db_to_denon_mv(-21.0), "MV59")
        self.assertEqual(ha.db_to_denon_mv(-21.5), "MV585")
        self.assertEqual(ha.db_to_denon_mv(-80.0), "MV00")

        sample_xml = """<?xml version="1.0" encoding="utf-8" ?>
        <item>
          <Power><value>ON</value></Power>
          <InputFuncSelect><value>MPLAY</value></InputFuncSelect>
          <SurrMode><value>MULTI CH STEREO               </value></SurrMode>
          <MasterVolume><value>-21.0</value></MasterVolume>
          <Mute><value>off</value></Mute>
        </item>"""
        parsed = ha.parse_denon_status_xml(sample_xml)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["power"], "ON")
        self.assertEqual(parsed["input"], "MPLAY")
        self.assertAlmostEqual(parsed["volumeDb"], -21.0)
        self.assertAlmostEqual(parsed["volumeAbs"], 59.0)
        self.assertFalse(parsed["muted"])
        self.assertEqual(parsed["surround"], "MULTI CH STEREO")

        muted_xml = """<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect><MasterVolume><value>--</value></MasterVolume><Mute><value>on</value></Mute></item>"""
        parsed_muted = ha.parse_denon_status_xml(muted_xml)
        self.assertTrue(parsed_muted["ok"])
        self.assertIsNone(parsed_muted["volumeDb"])
        self.assertTrue(parsed_muted["muted"])

        # Fake Denon HTTP responder tracking volume + MV commands sent
        state = {"vol": -18.0, "power": "ON", "input": "MPLAY", "cmds": []}

        def fake_denon_get(url: str, _timeout: float) -> tuple[int, bytes]:
            if "formMainZone_MainZoneXmlStatus.xml" in url:
                xml = (
                    f"<item><Power><value>{state['power']}</value></Power>"
                    f"<InputFuncSelect><value>{state['input']}</value></InputFuncSelect>"
                    f"<MasterVolume><value>{state['vol']:.1f}</value></MasterVolume>"
                    f"<Mute><value>off</value></Mute><SurrMode><value>STEREO</value></SurrMode></item>"
                )
                return 200, xml.encode("utf-8")
            if "formiPhoneAppDirect.xml?" in url:
                cmd = url.split("?", 1)[1]
                state["cmds"].append(cmd)
                if cmd.startswith("MV") and cmd[2:].isdigit():
                    raw = cmd[2:]
                    val = float(raw[:2]) + (0.5 if len(raw) == 3 and raw[2] == "5" else 0.0)
                    state["vol"] = round(val - 80.0, 1)
                return 200, b""
            return 404, b""

        cfg = ha.HouseAudioConfig(
            denon_enabled=True,
            denon_host="192.168.1.15",
            denon_duck_enabled=False,
            denon_duck_threshold_db=-20.0,
            denon_duck_drop_db=5.0,
            denon_ramp_steps=3,
            denon_ramp_step_ms=0,
        )
        client = ha.DenonAvrClient(lambda: cfg, http_get=fake_denon_get, sleep_fn=lambda _s: None)

        # 1. Disabled by default -> sends 0 MV commands
        with client.duck_context() as d1:
            self.assertFalse(d1["ducked"])
            self.assertEqual(d1["reason"], "disabled")
        self.assertEqual(state["cmds"], [])

        # 2. Enabled, but current volume (-26.0) is below -20.0 threshold -> sends 0 MV commands
        cfg.denon_duck_enabled = True
        state["vol"] = -26.0
        with client.duck_context() as d2:
            self.assertFalse(d2["ducked"])
            self.assertEqual(d2["reason"], "below_threshold")
        self.assertEqual(state["cmds"], [])

        # 3. Loud (-16.0 >= -20.0) -> ramps smoothly down 5 dB to -21.0 and restores exact -16.0
        state["vol"] = -16.0
        with client.duck_context() as d3:
            self.assertTrue(d3["ducked"])
            self.assertAlmostEqual(state["vol"], -21.0)
        self.assertAlmostEqual(state["vol"], -16.0)
        self.assertGreaterEqual(len(state["cmds"]), 4)
        self.assertEqual(state["cmds"][-1], "MV64")

        # 4. Manual remote volume change (> 1 dB drift) during duck -> must NOT overwrite user's manual level
        state["cmds"].clear()
        state["vol"] = -16.0
        with client.duck_context() as d4:
            self.assertTrue(d4["ducked"])
            self.assertAlmostEqual(state["vol"], -21.0)
            state["vol"] = -12.0  # Dave cranked the knob manually mid-duck
        self.assertAlmostEqual(state["vol"], -12.0)
        self.assertEqual(client.last_duck_info.get("restoreSkipped"), "manual_volume_change")

    def test_house_audio_routing_matrix_and_flask_endpoints(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_ha_test_"))
        try:
            denon_state = {"power": "ON", "input": "MPLAY", "vol": -22.0}
            cube_calls: list[dict[str, object]] = []
            cube_healthy = {"ok": True}

            def fake_denon_get(url: str, _timeout: float) -> tuple[int, bytes]:
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    xml = (
                        f"<item><Power><value>{denon_state['power']}</value></Power>"
                        f"<InputFuncSelect><value>{denon_state['input']}</value></InputFuncSelect>"
                        f"<MasterVolume><value>{denon_state['vol']:.1f}</value></MasterVolume>"
                        f"<Mute><value>off</value></Mute><SurrMode><value>MULTI CH STEREO</value></SurrMode></item>"
                    )
                    return 200, xml.encode("utf-8")
                return 200, b""

            def fake_cube_get(url: str, _timeout: float) -> tuple[int, bytes]:
                if url.endswith("/health"):
                    if cube_healthy["ok"]:
                        return 200, b'{"ok":true,"pkg":"com.froriep.armoryaudio","port":8767,"playing":false,"streamVol":12,"streamMaxVol":15,"playCount":3}'
                    raise OSError("Cube unreachable")
                return 404, b""

            def fake_cube_post(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                cube_calls.append({"url": url, "bytes": len(body), "is_wav": body[:4] == b"RIFF"})
                if not cube_healthy["ok"]:
                    raise OSError("Cube down")
                return 200, b'{"ok":true,"played":true}'

            mgr = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(cube_enabled=True, cube_host="192.168.1.99", output_route="auto"),
                allow_network=True,
                denon_http_get=fake_denon_get,
                cube_http_get=fake_cube_get,
                cube_http_post=fake_cube_post,
                sleep_fn=lambda _s: None,
            )

            pcm = 0.3 * np.sin(2.0 * np.pi * 440.0 * np.linspace(0, 0.25, 12_000, endpoint=False, dtype=np.float32))
            # 1. Auto route with Denon ON + MPLAY + Cube healthy -> routes WAV to Cube
            r_cube = mgr.try_route_voice_pcm(pcm, 48_000, kind="tts")
            self.assertTrue(r_cube["handled"])
            self.assertEqual(r_cube["route"], "cube")
            self.assertEqual(len(cube_calls), 1)
            self.assertTrue(cube_calls[0]["is_wav"])
            self.assertIn("mode=overlay", str(cube_calls[0]["url"]))

            # 2. Denon switched away from MPLAY (e.g. CBL/SAT) -> cleanly falls back to S330
            denon_state["input"] = "CBL/SAT"
            r_s330 = mgr.try_route_voice_pcm(pcm, 48_000, kind="tts")
            self.assertFalse(r_s330["handled"])
            self.assertEqual(r_s330["route"], "s330")

            # 3. Forced Cube route fails when Cube POST raises -> cleanly falls back to S330
            denon_state["input"] = "MPLAY"
            mgr.update_config({"outputRoute": "cube"})
            cube_healthy["ok"] = False
            r_fail = mgr.try_route_voice_pcm(pcm, 48_000, kind="tts")
            self.assertFalse(r_fail["handled"])
            self.assertTrue(r_fail["fallback"])

            # 4. AudioMatrixStore persistence & reset
            snap1 = mgr.matrix.record("Spotify", "overlay", status="pass", behavior="Clean mix, zero pause")
            self.assertEqual(snap1["cells"]["Spotify"]["overlay"]["status"], "pass")
            reloaded = ha.AudioMatrixStore(root_dir=tmp)
            self.assertEqual(reloaded.snapshot_dict()["cells"]["Spotify"]["overlay"]["behavior"], "Clean mix, zero pause")
            snap_rst = reloaded.reset()
            self.assertEqual(snap_rst["cells"]["Spotify"]["overlay"]["status"], "untested")

            # 5. Flask API endpoints + dashboard UI + SFX path-traversal guard
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.house_audio.denon._http_get = fake_denon_get
            svc.house_audio.cube._http_get = fake_cube_get
            svc.house_audio.cube._http_post = fake_cube_post
            cube_healthy["ok"] = True
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            self.assertIn("House Audio (Denon AVR-3313CI + Fire TV Cube Mixer)", dash)
            self.assertIn("haDenonPill", dash)
            self.assertIn("haMatrixTable", dash)

            st_res = client.get("/api/audio/status")
            self.assertEqual(st_res.status_code, 200)
            st_json = st_res.get_json()
            self.assertTrue(st_json["denon"]["ok"])
            self.assertFalse(st_json["config"]["denonDuckEnabled"])

            cfg_res = client.post(
                "/api/audio/config",
                json={"cubeEnabled": True, "cubeHost": "192.168.1.99", "outputRoute": "auto", "denonDuckEnabled": False},
            )
            self.assertEqual(cfg_res.status_code, 200)
            self.assertEqual(cfg_res.get_json()["config"]["cubeHost"], "192.168.1.99")

            svc_reload = ac.ArmoryCamService(root_dir=tmp)
            self.assertEqual(svc_reload.house_audio_cfg.cube_host, "192.168.1.99")
            self.assertEqual(svc_reload.house_audio_cfg.output_route, "auto")

            tp_res = client.post(
                "/api/audio/test-play",
                json={"kind": "pr_fanfare", "mode": "takeover", "target": "cube"},
            )
            self.assertEqual(tp_res.status_code, 200)
            self.assertEqual(tp_res.get_json()["route"], "cube")

            mx_res = client.post(
                "/api/audio/matrix",
                json={"app": "YouTube", "mode": "duck", "status": "warn", "behavior": "Paused briefly"},
            )
            self.assertEqual(mx_res.status_code, 200)
            self.assertEqual(mx_res.get_json()["matrix"]["cells"]["YouTube"]["duck"]["status"], "warn")

            # Traversal protection on SFX route
            bad1 = client.get("/api/audio/sfx/../config.json")
            self.assertIn(bad1.status_code, (400, 404))
            bad2 = client.get("/api/audio/sfx/%2e%2e%2fconfig.json")
            self.assertIn(bad2.status_code, (400, 404))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_display_voice_parser_controller_and_flask_endpoints(self) -> None:
        # 1. Lifting/cardio phrases must NEVER trigger display commands
        for phrase in ("Armory 225 for 8", "Armory check", "Armory skip rest", "Armory replay", "Armory add 10 pounds"):
            self.assertIsNone(dc.parse_display_voice_command(phrase), msg=phrase)

        self.assertEqual(dc.parse_display_voice_command("Armory, open the gym"), {"target": "open_gym", "targets": ["open_gym"], "action": "run", "speech": "Opening the gym"})
        self.assertEqual(dc.parse_display_voice_command("Armory, close the gym"), {"target": "close_gym", "targets": ["close_gym"], "action": "run", "speech": "Closing the gym"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV"), {"target": "tv", "targets": ["tv"], "action": "on", "speech": "TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on SmartSpin TV"), {"target": "spin", "targets": ["spin"], "action": "on", "speech": "SmartSpin TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV and SmartSpin TV"), {"target": "multi", "targets": ["tv", "spin"], "action": "on", "speech": "TV and SmartSpin TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV and wallboard"), {"target": "multi", "targets": ["tv", "wallboard"], "action": "on", "speech": "TV and Wallboard on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn off all TVs"), {"target": "all_tvs", "targets": ["tv", "wallboard", "spin"], "action": "off", "speech": "All TVs off"})
        self.assertEqual(dc.parse_display_voice_command("Armory turn off hisense")["action"], "off")
        self.assertEqual(dc.parse_display_voice_command("Armory switch primary to PC")["action"], "hdmi1")
        self.assertEqual(dc.parse_display_voice_command("Armory switch TV to Fire TV")["action"], "hdmi2")
        self.assertEqual(dc.parse_display_voice_command("Armory sleep wallboard"), {"target": "wallboard", "targets": ["wallboard"], "action": "off", "speech": "Wallboard off"})
        self.assertEqual(dc.parse_display_voice_command("Armory turn off denon")["target"], "avr")
        self.assertEqual(dc.parse_display_voice_command("Armory, play music"), {"target": "music", "targets": ["music"], "action": "play", "speech": "Playing music"})
        self.assertEqual(dc.parse_display_voice_command("Armory, play 90s hip hop music"), {"target": "music", "targets": ["music"], "action": "play:90s hip hop", "speech": "Playing 90s hip hop"})
        self.assertEqual(dc.parse_display_voice_command("Armory, pause music"), {"target": "music", "targets": ["music"], "action": "pause", "speech": "Music paused"})
        self.assertEqual(dc.parse_display_voice_command("Armory, resume music"), {"target": "music", "targets": ["music"], "action": "resume", "speech": "Resuming music"})
        self.assertEqual(dc.parse_display_voice_command("Armory, next song"), {"target": "music", "targets": ["music"], "action": "next", "speech": "Next track"})
        self.assertEqual(dc.parse_display_voice_command("Armory, previous track"), {"target": "music", "targets": ["music"], "action": "prev", "speech": "Previous track"})
        self.assertIsNone(dc.parse_display_voice_command("Armory play replay video"))
        self.assertEqual(dc.normalize_youtube_tv_url("https://music.youtube.com/playlist?list=PL123"), "https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(
            dc.resolve_youtube_music_url(
                "workout music mix",
                http_get=lambda _u, _t: (200, b'<html>"videoId":"ld8nm6rcOm0"</html>'),
            ),
            "https://www.youtube.com/watch?v=ld8nm6rcOm0&list=RDld8nm6rcOm0",
        )
        self.assertTrue(dc.SpinDisplayConfig().auto_denon_dual)
        self.assertEqual(dc.SpinDisplayConfig().primary_denon_monitor, "1")

        # 2. Controller: Primary ADB discrete keyevents + Roku ECP + Denon + Wallboard + Open/Close Gym
        adb_cmds: list[list[str]] = []

        def fake_runner(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            adb_cmds.append(cmd)
            if "devices" in cmd:
                return 0, "List of devices attached\n192.168.1.230:5555\tdevice\n192.168.1.151:5555\tdevice\n", ""
            return 0, "connected", ""

        fake_adb = ac.FireTvAdbController(adb_bin="adb", runner=fake_runner, settle_sec=0.0)
        denon_cmds: list[str] = []

        def fake_denon_get(url: str, _to: float) -> tuple[int, bytes]:
            if "formiPhoneAppDirect.xml?" in url:
                denon_cmds.append(url.split("?", 1)[1])
                return 200, b""
            return 404, b""

        roku_posts: list[str] = []
        denon_posts: list[tuple[str, bytes]] = []
        mock_denon_video = {"serve_html": False}
        mock_roku_power = {"mode": "Ready"}

        def fake_ctrl_get(url: str, _to: float) -> tuple[int, bytes]:
            if mock_denon_video["serve_html"] and "d_video.asp" in url:
                return 200, b'<FORM name="video"><INPUT type="radio" name="radioMonitorOut" value="AUTO" checked><INPUT type="radio" name="radioVideoConvMode" value="OFF" checked></FORM>'
            if "192.168.1.88:8060/query/device-info" in url:
                return 200, f"<device-info><power-mode>{mock_roku_power['mode']}</power-mode></device-info>".encode("ascii")
            if "youtube.com/results" in url:
                return 200, b'{"videoId":"ld8nm6rcOm0"}'
            return 404, b""

        def fake_ctrl_post(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
            if "192.168.1.88:8060" in url:
                roku_posts.append(url)
            else:
                denon_posts.append((url, body))
            return 200, b""

        wb_calls: list[str] = []
        ha_cfg = ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15")
        denon_cli = ha.DenonAvrClient(lambda: ha_cfg, http_get=fake_denon_get, sleep_fn=lambda _s: None)
        ctrl = dc.GymDisplayController(
            config=dc.GymDisplaysConfig(
                primary=dc.PrimaryDisplayConfig(host="192.168.1.230", mac="bc:09:b9:e7:35:26"),
                spin=dc.SpinDisplayConfig(host="192.168.1.88", auto_denon_dual=True),
            ),
            adb_controller=fake_adb,
            denon_client=denon_cli,
            wake_wallboard_fn=lambda force=True: (wb_calls.append("wake"), {"ok": True, "message": "Wallboard on"})[1],
            sleep_wallboard_fn=lambda: (wb_calls.append("sleep"), {"ok": True, "message": "Wallboard off"})[1],
            http_get=fake_ctrl_get,
            http_post=fake_ctrl_post,
            sleep_fn=lambda _s: None,
            allow_network=False,
        )

        self.assertEqual(dc.resolve_hdmi_keyevent("com.mediatek.tis/.HdmiInputService/HW2", "243"), "243")
        self.assertEqual(dc.resolve_hdmi_keyevent("com.mediatek.tis/.HdmiInputService/HW3", "244"), "244")
        self.assertTrue(ctrl.execute("primary", "on")["ok"])
        self.assertTrue(any(c[-3:] == ["input", "keyevent", "224"] for c in adb_cmds))
        self.assertTrue(ctrl.execute("primary", "off")["ok"])
        self.assertTrue(any(c[-3:] == ["input", "keyevent", "223"] for c in adb_cmds))
        self.assertTrue(ctrl.execute("primary", "hdmi1")["ok"])
        self.assertTrue(any(c[-3:] == ["input", "keyevent", "243"] for c in adb_cmds))
        self.assertFalse(any("android.media.tv/passthrough/" in " ".join(c) for c in adb_cmds))
        denon_cmds.clear()
        self.assertTrue(ctrl.execute("tv", "hdmi2")["ok"])
        self.assertTrue(any(c[-3:] == ["input", "keyevent", "244"] for c in adb_cmds))
        # SmartSpin is Ready (OFF), so tv:hdmi2 syncs Denon to Monitor 1 automatically
        self.assertIn("VSMONI1", denon_cmds)

        mock_roku_power["mode"] = "PowerOn"
        denon_cmds.clear()
        self.assertTrue(ctrl.sync_denon_monitor_for_spin()["ok"])
        self.assertIn("VSMONIAUTO", denon_cmds)
        mock_roku_power["mode"] = "Ready"

        # Test Roku ECP + Denon VSMONIAUTO / VSMONI1 fallback
        self.assertTrue(ctrl.execute("spin", "on")["ok"])
        self.assertEqual(roku_posts, ["http://192.168.1.88:8060/keypress/PowerOn", "http://192.168.1.88:8060/keypress/InputHDMI1"])
        self.assertIn("VSMONIAUTO", denon_cmds)

        self.assertTrue(ctrl.execute("spin", "off")["ok"])
        self.assertEqual(roku_posts[-1], "http://192.168.1.88:8060/keypress/PowerOff")
        self.assertIn("VSMONI1", denon_cmds)

        # Also verify Denon AVR-3313CI s_video.asp read-before-write preserves unrelated fields
        mock_denon_video["serve_html"] = True
        m_res = ctrl.execute("avr", "monitor1")
        self.assertTrue(m_res["ok"])
        self.assertEqual(m_res["via"], "s_video.asp")
        self.assertEqual(len(denon_posts), 1)
        self.assertIn(b"radioMonitorOut=1", denon_posts[0][1])
        self.assertIn(b"radioVideoConvMode=OFF", denon_posts[0][1])

        self.assertTrue(ctrl.execute("wallboard", "on")["ok"])
        self.assertTrue(ctrl.execute("wallboard", "off")["ok"])
        self.assertEqual(wb_calls, ["wake", "sleep"])

        # Open Gym & Close Gym macros with normalized YouTube Music playlist URL
        ctrl.update_config({"openGymMusicUrl": "https://music.youtube.com/playlist?list=PLgym99"})
        adb_cmds.clear()
        denon_cmds.clear()
        wb_calls.clear()
        og = ctrl.execute("open_gym", "run")
        self.assertTrue(og["ok"])
        self.assertIn("PWON", denon_cmds)
        self.assertIn("SIMPLAY", denon_cmds)
        self.assertIn("MSMCH%20STEREO", denon_cmds)
        self.assertEqual(wb_calls, ["wake"])
        self.assertTrue(any("https://www.youtube.com/playlist?list=PLgym99" in " ".join(c) and "192.168.1.151:5555" in c for c in adb_cmds))

        adb_cmds.clear()
        denon_cmds.clear()
        wb_calls.clear()
        cg = ctrl.execute("close_gym", "run")
        self.assertTrue(cg["ok"])
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "127"] for c in adb_cmds))
        self.assertIn("PWSTANDBY", denon_cmds)
        self.assertEqual(wb_calls, ["sleep"])

        adb_cmds.clear()
        denon_cmds.clear()
        ctrl.update_config({"openGymMusicUrl": ""})
        m_play = ctrl.execute("music", "play:90s hip hop")
        self.assertTrue(m_play["ok"])
        self.assertIn("PWON", denon_cmds)
        self.assertIn("MSMCH%20STEREO", denon_cmds)
        self.assertTrue(any("ld8nm6rcOm0" in " ".join(c) and "192.168.1.151:5555" in c for c in adb_cmds))
        self.assertTrue(ctrl.execute("music", "next")["ok"])
        self.assertTrue(any("keyevent 87" in " ".join(c) for c in adb_cmds))
        self.assertTrue(ctrl.execute("music", "pause")["ok"])

        # 3. RoomVoiceService local command interception + Flask display endpoints
        tmp = Path(tempfile.mkdtemp(prefix="armory_disp_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp, adb_controller=fake_adb)
            svc.displays = ctrl
            svc.voice._speak_fn = lambda _txt: True
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            self.assertIn("Gym Displays &amp; Discrete Power Control", dash)
            self.assertIn("dpPrimHost", dash)
            self.assertIn("dpSpinHost", dash)
            self.assertIn("dpMusicUrl", dash)

            svc.voice._stop_event.clear()
            stream = svc.voice.subscribe_sse()
            _init_frame = next(stream)

            # Display voice command handled locally — must NOT emit to Pixel SSE
            disp_evt = svc.voice.inject_transcript("Armory, turn on SmartSpin TV")
            self.assertTrue(disp_evt["localHandled"])
            self.assertIn("SmartSpin TV on", disp_evt["ackText"])

            # Compound voice command handled locally
            wb_calls.clear()
            comp_evt = svc.voice.inject_transcript("Armory, turn on TV and wallboard")
            self.assertTrue(comp_evt["localHandled"])
            self.assertIn("TV and Wallboard on", comp_evt["ackText"])
            self.assertEqual(wb_calls, ["wake"])

            # Normal lifting command still emits over Pixel SSE
            lift_evt = svc.voice.inject_transcript("Armory 225 for 8")
            self.assertTrue(lift_evt["emitted"])
            self.assertFalse(lift_evt.get("localHandled", False))
            sse_frame = next(stream).decode("utf-8")
            self.assertIn("Armory 225 for 8", sse_frame)
            self.assertNotIn("SmartSpin", sse_frame)
            stream.close()

            cfg_res = client.post(
                "/api/displays/config",
                json={"spin": {"host": "192.168.1.77", "mac": "aa:bb:cc:dd:ee:ff"}},
            )
            self.assertEqual(cfg_res.status_code, 200)
            self.assertEqual(cfg_res.get_json()["config"]["spin"]["host"], "192.168.1.77")

            st_res = client.get("/api/displays/status")
            self.assertEqual(st_res.status_code, 200)
            self.assertEqual(st_res.get_json()["config"]["primary"]["host"], "192.168.1.230")

            act_res = client.post("/api/displays/action", json={"target": "tv", "action": "on"})
            self.assertEqual(act_res.status_code, 200)
            self.assertTrue(act_res.get_json()["ok"])

            multi_res = client.post("/api/displays/action", json={"targets": ["tv", "wallboard"], "action": "on"})
            self.assertEqual(multi_res.status_code, 200)
            self.assertTrue(multi_res.get_json()["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_room_audio_recorder_silent_clips_and_mux_endpoints(self) -> None:
        ffmpeg = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        ffprobe = ac.resolve_tool_bin("ffprobe", "ARMORY_CAM_FFPROBE")
        if not shutil.which(ffmpeg) and not Path(ffmpeg).is_file():
            self.skipTest("ffmpeg unavailable")

        def audio_stream_count(mp4_path: Path) -> int:
            res = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mp4_path)],
                capture_output=True, text=True, timeout=8,
            )
            return len([ln for ln in res.stdout.splitlines() if ln.strip()])

        def video_stream_count(mp4_path: Path) -> int:
            res = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mp4_path)],
                capture_output=True, text=True, timeout=8,
            )
            return len([ln for ln in res.stdout.splitlines() if ln.strip()])

        tmp = Path(tempfile.mkdtemp(prefix="armory_audio_clip_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp, voice_autostart=False)
            svc.update_config({
                "retentionDays": 14,
                "cameras": [{"name": "Rack Cam", "rtspUrl": "rtsp://127.0.0.1/none", "enabled": True}],
            })
            app = ac.create_app(svc)
            client = app.test_client()

            # Build synthetic 4s TS segment WITH embedded camera audio (simulating Wyze pinhole mic)
            base_ms = (int(time.time()) - 8) * 1000
            day_dir = svc.tapes_root / "sess_a" / "rack_cam"
            day_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(base_ms / 1000.0).strftime("%Y%m%d_%H%M%S")
            seg_ts = day_dir / f"seg_{stamp}.ts"
            gen_cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=15:d=4",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(seg_ts),
            ]
            subprocess.run(gen_cmd, check=True, timeout=20)
            (svc.tapes_root / "sess_a" / "motion.jsonl").write_text(
                json.dumps({
                    "slug": "rack_cam",
                    "camera": "Rack Cam",
                    "file": seg_ts.name,
                    "startMs": base_ms,
                    "endMs": base_ms + 4000,
                    "energy": 8.0,
                }) + "\n",
                encoding="utf-8",
            )

            # Feed matching 4s PowerConf S330 PCM into RoomAudioRecorder and flush to audio_tapes
            sr = 16_000
            t_arr = np.linspace(0.0, 4.0, sr * 4, endpoint=False, dtype=np.float32)
            pcm = 0.25 * np.sin(2.0 * np.pi * 440.0 * t_arr)
            svc.room_audio.push_pcm(pcm, sr, wall_end_s=(base_ms + 4000) / 1000.0)
            flushed = svc.room_audio.flush_now(blocking=True)
            self.assertIsNotNone(flushed)
            self.assertTrue(svc.room_audio.has_audio(base_ms, base_ms + 4000))

            # 1. Default POST /api/clip MUST strip Wyze audio (silent MP4)
            c_silent = client.post("/api/clip", json={
                "sessionId": "sess_a",
                "exName": "Squat",
                "startMs": base_ms,
                "endMs": base_ms + 3000,
                "camera": "Rack Cam",
            })
            self.assertEqual(c_silent.status_code, 200)
            j_silent = c_silent.get_json()
            self.assertFalse(j_silent["hasAudio"])
            self.assertEqual(j_silent["audioSource"], "none")
            silent_mp4 = svc.clips_root / j_silent["lanUrl"].split("/clips/", 1)[1]
            self.assertEqual(video_stream_count(silent_mp4), 1)
            self.assertEqual(audio_stream_count(silent_mp4), 0)

            # 2. Auto-PR MUST also default to silent (-an)
            c_pr = client.post("/api/clip/auto-pr", json={
                "sessionId": "sess_a",
                "exName": "Squat",
                "setAtMs": base_ms + 3000,
                "camera": "Rack Cam",
            })
            self.assertEqual(c_pr.status_code, 200)
            j_pr = c_pr.get_json()
            self.assertFalse(j_pr["hasAudio"])
            pr_mp4 = svc.clips_root / j_pr["lanUrl"].split("/clips/", 1)[1]
            self.assertEqual(audio_stream_count(pr_mp4), 0)

            # 3. Availability & Preview Audio endpoint
            ang = client.get(f"/api/tape/angles?sessionId=sess_a&startMs={base_ms}&endMs={base_ms + 3000}")
            self.assertEqual(ang.status_code, 200)
            self.assertTrue(ang.get_json()["audioAvailable"])

            av = client.get(f"/api/audio/availability?startMs={base_ms}&endMs={base_ms + 3000}")
            self.assertEqual(av.status_code, 200)
            self.assertTrue(av.get_json()["available"])

            aud_resp = client.get(f"/api/audio/clip?startMs={base_ms}&endMs={base_ms + 3000}")
            self.assertEqual(aud_resp.status_code, 200)
            self.assertGreater(len(aud_resp.data), 500)

            # 4. Losslessly Add Room Audio -> Verify AAC stream added; then Strip -> Verify silent again
            mux_resp = client.post("/api/clip/audio", json={
                "lanUrl": j_silent["lanUrl"],
                "fileName": j_silent["fileName"],
                "includeAudio": True,
            })
            self.assertEqual(mux_resp.status_code, 200)
            self.assertTrue(mux_resp.get_json()["hasAudio"])
            self.assertEqual(video_stream_count(silent_mp4), 1)
            self.assertEqual(audio_stream_count(silent_mp4), 1)

            strip_resp = client.post("/api/clip/audio", json={
                "lanUrl": j_silent["lanUrl"],
                "fileName": j_silent["fileName"],
                "includeAudio": False,
            })
            self.assertEqual(strip_resp.status_code, 200)
            self.assertFalse(strip_resp.get_json()["hasAudio"])
            self.assertEqual(audio_stream_count(silent_mp4), 0)

            # 5. 14-day prune drops expired room audio chunks automatically
            svc.room_audio.prune((base_ms / 1000.0) + (15 * 86_400), retention_days=14)
            self.assertFalse(flushed.exists())
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)


class ShowDirectorTests(unittest.TestCase):
    def test_voice_triggers_prompt_and_local_interception(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_show_trig_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            tts_spoken: list[str] = []
            svc.voice._speak_fn = lambda txt: (tts_spoken.append(txt), True)[1]

            prompt, hotwords = rv.build_whisper_prompt("armory")
            self.assertIn("Matt just hit a PR", prompt)
            self.assertIn("Matt just got here", prompt)
            self.assertIn("Ryder is here", prompt)
            self.assertIn("Jared and Mark", prompt)

            sd_dir = svc.shows
            self.assertEqual(sd_dir.match_voice_command("Armory, Matt just hit a PR")["showId"], "matt_pr")
            self.assertEqual(sd_dir.match_voice_command("Hey Armory matt hit a p.r.")["showId"], "matt_pr")
            self.assertEqual(sd_dir.match_voice_command("Armory Matt just got here")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Matt's in the gym")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory, Matt is here")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Ryder is here")["showId"], "ryder_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Rider debut")["showId"], "ryder_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Jared and Mark are here")["showId"], "jared_mark_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Gabe is here")["showId"], "gabe_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Josh is here")["showId"], "josh_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory stop show")["action"], "stop")
            self.assertIsNone(sd_dir.match_voice_command("Armory 225 for 8"))
            self.assertIsNone(sd_dir.match_voice_command("Armory open the gym"))

            # Voice injection for matt_pr fires locally, stays off Pixel SSE, and suppresses S330 TTS readback
            with mock.patch.object(svc.shows, "fire_show", return_value={"ok": True, "showId": "matt_pr", "showName": "Matt PR Drop-In"}) as m_fire:
                svc.voice._stop_event.clear()
                ev = svc.voice.inject_transcript("Armory, Matt just hit a PR")
                self.assertTrue(ev["localHandled"])
                m_fire.assert_called_once_with("matt_pr", async_run=True)
                self.assertEqual(tts_spoken, [])
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_anti_roller_coaster_ordering_and_emergency_stop(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_show_arc_"))
        try:
            events: list[tuple[str, Any]] = []
            denon_db = {"val": -30.0}  # MV50

            def fake_denon_get(url: str, _to: float) -> tuple[int, bytes]:
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, (
                        f"<item><Power><value>ON</value></Power>"
                        f"<InputFuncSelect><value>MPLAY</value></InputFuncSelect>"
                        f"<MasterVolume><value>{denon_db['val']:.1f}</value></MasterVolume>"
                        f"<Mute><value>off</value></Mute></item>"
                    ).encode("ascii")
                if "formiPhoneAppDirect.xml?" in url:
                    cmd = url.split("?", 1)[1]
                    events.append(("denon", cmd))
                    return 200, b""
                return 404, b""

            def fake_cube_post(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                events.append(("cube_play" if "/play" in url else "cube_stop", url, len(body)))
                return 200, b'{"ok":true,"queued":true}'

            ha_cfg = ha.HouseAudioConfig(
                denon_enabled=True,
                denon_host="192.168.1.15",
                cube_enabled=True,
                cube_host="192.168.1.151",
                cube_port=8767,
            )
            ham = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha_cfg,
                allow_network=False,
                denon_http_get=fake_denon_get,
                cube_http_post=fake_cube_post,
                sleep_fn=lambda _s: None,
            )
            director = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                allow_network=False,
                sleep_fn=lambda _s: None,
            )
            res = director.fire_show("matt_pr", async_run=False)
            self.assertTrue(res["ok"])

            # Verify strict ordering: Cube duck play BEFORE first upward Denon MV ramp
            cube_idx = next(i for i, e in enumerate(events) if e[0] == "cube_play")
            mv_indices = [i for i, e in enumerate(events) if e[0] == "denon" and str(e[1]).startswith("MV")]
            self.assertGreater(len(mv_indices), 2)
            self.assertLess(cube_idx, mv_indices[0])
            cube_url = events[cube_idx][1]
            self.assertIn("mode=duck", cube_url)
            self.assertIn("gain=1.00", cube_url)
            self.assertIn("sync=0", cube_url)

            # Denon ramps up to MV70 (-10 dB) and then BACK DOWN to MV50 (-30 dB), never exceeding MV70
            mv_cmds = [events[i][1] for i in mv_indices]
            self.assertIn("MV70", mv_cmds)
            self.assertEqual(mv_cmds[-1], "MV50")
            self.assertFalse(any(c > "MV75" for c in mv_cmds if len(c) == 4))

            # Emergency cancel mid-show MUST restore Denon down BEFORE calling cube.stop()
            events.clear()
            cancel_ev = threading.Event()

            def cancel_during_play(sec: float) -> None:
                if sec > 1.0:
                    cancel_ev.set()

            director_cancel = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                allow_network=False,
                sleep_fn=cancel_during_play,
            )
            spec = director_cancel.get_show("matt_pr")
            assert spec is not None
            director_cancel._active_run_id = 99
            c_res = director_cancel._run_show_sequence(spec, 99, cancel_ev)
            self.assertTrue(c_res.get("canceled"))
            stop_idx = next(i for i, e in enumerate(events) if e[0] == "cube_stop")
            restore_mv50_idx = max(i for i, e in enumerate(events) if e == ("denon", "MV50"))
            self.assertLess(restore_mv50_idx, stop_idx)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ryder_three_screen_and_fx_hooks_choreography(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_ryder_"))
        try:
            trace: list[tuple[str, str]] = []
            adb_cmds: list[list[str]] = []

            def fake_runner(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
                adb_cmds.append(cmd)
                if "devices" in cmd:
                    return 0, (
                        "List of devices attached\n"
                        "192.168.1.230:5555\tdevice\n"
                        "192.168.1.151:5555\tdevice\n"
                        "192.168.1.204:5555\tdevice\n"
                    ), ""
                return 0, "ok", ""

            fake_adb = ac.FireTvAdbController(adb_bin="adb", runner=fake_runner, settle_sec=0.0)

            def fake_http_get(url: str, _to: float) -> tuple[int, bytes]:
                trace.append(("http_get", url))
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, b"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect><MasterVolume><value>-30.0</value></MasterVolume><Mute><value>off</value></Mute></item>"
                if "192.168.1.88:8060/query/device-info" in url:
                    return 200, b"<device-info><power-mode>Ready</power-mode></device-info>"
                return 200, b"ok"

            def fake_http_post(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                trace.append(("http_post", url))
                return 200, b'{"ok":true,"queued":true}'

            ha_cfg = ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15", cube_enabled=True, cube_host="192.168.1.151")
            ham = ha.HouseAudioManager(root_dir=tmp, config=ha_cfg, allow_network=False, denon_http_get=fake_http_get, cube_http_post=fake_http_post, sleep_fn=lambda _s: None)
            wb_cfg = ac.WallboardConfig(fire_tv_host="192.168.1.204")
            disp = dc.GymDisplayController(
                config=dc.GymDisplaysConfig(
                    primary=dc.PrimaryDisplayConfig(host="192.168.1.230"),
                    spin=dc.SpinDisplayConfig(host="192.168.1.88", auto_denon_dual=True),
                    cube_host="192.168.1.151",
                ),
                adb_controller=fake_adb,
                denon_client=ham.denon,
                wake_wallboard_fn=lambda force=True: (trace.append(("wb", "wake")), {"ok": True})[1],
                http_get=fake_http_get,
                http_post=fake_http_post,
                sleep_fn=lambda _s: None,
                allow_network=False,
            )
            director = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                displays=disp,
                adb_controller=fake_adb,
                wallboard_cfg_fn=lambda: wb_cfg,
                allow_network=False,
                http_get=fake_http_get,
                sleep_fn=lambda _s: None,
            )
            director.update_fx_hooks({"lightsHost": "192.168.1.60", "fogHost": "192.168.1.61", "fogBurstSec": 4.5})
            res = director.fire_show("ryder_walkin", async_run=False)
            self.assertTrue(res["ok"])

            urls = [u for k, u in trace if k == "http_get"]
            self.assertIn("http://192.168.1.60/rpc/Switch.Set?id=0&on=false", urls)
            # Ryder uses dedicated 30s fog burst (while leaving other shows' 4.5s default intact)
            self.assertIn("http://192.168.1.61/rpc/Switch.Set?id=0&on=true&toggle_after=30", urls)
            # Lightning flash + 2x flash + 10x 0.25s strobe pulses + full lights on = 14+ ON triggers
            self.assertGreaterEqual(urls.count("http://192.168.1.60/rpc/Switch.Set?id=0&on=true"), 14)
            # Denon Dual HDMI Out (AUTO) + MV70 -> post-1:20 MV55 drop!
            self.assertTrue(any("VSMONIAUTO" in u for u in urls))
            self.assertTrue(any("MV70" in u for u in urls))
            mv70_idx = next(i for i, u in enumerate(urls) if "MV70" in u)
            last_mv55_idx = max(i for i, u in enumerate(urls) if "MV55" in u)
            self.assertGreater(last_mv55_idx, mv70_idx)
            mv_cmds = [u.split("?", 1)[1] for u in urls if "formiPhoneAppDirect.xml?MV" in u]
            self.assertEqual(mv_cmds[-1], "MV55")

            # Cube plays intro (takeover) then 1:20 overlay in mode=duck BEFORE Denon drops MV70->MV55
            cube_posts = [u for k, u in trace if k == "http_post" and "192.168.1.151:8767/play" in u]
            self.assertGreaterEqual(len(cube_posts), 2)
            self.assertIn("mode=duck", cube_posts[-1])
            self.assertIn("gain=1.00", cube_posts[-1])

            posts = [u for k, u in trace if k == "http_post"]
            self.assertIn("http://192.168.1.88:8060/keypress/PowerOn", posts)
            self.assertIn("http://192.168.1.88:8060/keypress/InputHDMI1", posts)
            self.assertIn(("wb", "wake"), trace)
            joined_adb = [" ".join(c) for c in adb_cmds]
            self.assertTrue(any("192.168.1.204:5555" in s and "media volume --stream 3 --set 0" in s for s in joined_adb))
            self.assertTrue(any("192.168.1.204:5555" in s and "05vwKMySzKw" in s for s in joined_adb))
            self.assertTrue(any("192.168.1.151:5555" in s and "05vwKMySzKw" in s for s in joined_adb))

            # Visual FX cues logged for on-screen SSE simulator
            cue_tags = [c["tag"] for c in director.recent_cues]
            for req_tag in ("lights_off", "fog", "flash", "screens", "pulse", "lights_on", "overlay", "denon_drop", "ended"):
                self.assertIn(req_tag, cue_tags)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_show_api_preview_onset_and_song_upload_trim(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_show_api_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            self.assertIn("Walk-Ins &amp; PR Drop-Ins (Show Lab)", dash)
            self.assertIn("devRyderBox", dash)
            self.assertIn("shStopAllBtn", dash)

            v_resp = client.get("/api/shows/voices")
            self.assertEqual(v_resp.status_code, 200)
            self.assertEqual(v_resp.get_json()["defaultVoice"], sd.DEFAULT_ANNOUNCER_VOICE)

            ov_resp = client.get("/api/shows/ryder_walkin/overlay.wav")
            self.assertEqual(ov_resp.status_code, 200)
            self.assertEqual(ov_resp.mimetype, "audio/wav")

            # Banned legacy robotic voice auto-upgrades cleanly & never clips on master chain
            upgraded = sd.ShowSpec.from_dict({"id": "t1", "announcerVoice": "en-US-ChristopherNeural"})
            self.assertEqual(upgraded.announcer_voice, sd.DEFAULT_ANNOUNCER_VOICE)
            raw_sig = 0.9 * np.sin(2.0 * np.pi * 140.0 * np.linspace(0, 0.5, 12000, endpoint=False, dtype=np.float32))
            mastered = sd.master_announcer_stereo(raw_sig, 24000, boost_db=5.0)
            self.assertLessEqual(float(np.max(np.abs(mastered))), 0.92)

            lst = client.get("/api/shows")
            self.assertEqual(lst.status_code, 200)
            j_lst = lst.get_json()
            self.assertTrue(j_lst["ok"])
            ids = {s["id"] for s in j_lst["shows"]}
            self.assertEqual(ids, {"matt_pr", "matt_walkin", "ryder_walkin", "jared_mark_walkin", "gabe_walkin", "josh_walkin"})

            # Preview WAV builds cleanly, has instant non-silent onset in first 150ms and clean faded tail
            prev = client.get("/api/shows/matt_pr/preview.wav")
            self.assertEqual(prev.status_code, 200)
            self.assertEqual(prev.mimetype, "audio/wav")
            prev_path = tmp / "shows" / "rendered" / "matt_pr_preview.wav"
            arr = sd.read_wav_stereo_float(prev_path)
            self.assertGreater(arr.shape[0], 10 * sd.SHOW_SR)
            first_rms = float(np.sqrt(np.mean(arr[: int(0.15 * sd.SHOW_SR)] ** 2)))
            tail_rms = float(np.sqrt(np.mean(arr[-int(0.15 * sd.SHOW_SR) :] ** 2)))
            self.assertGreater(first_rms, 0.05)
            self.assertLess(tail_rms, 0.04)

            # Path traversal upload MUST be rejected with HTTP 400
            bad_up = client.post(
                "/api/shows/matt_pr/upload?filename=../evil.wav",
                data=b"RIFF0000WAVE",
                content_type="application/octet-stream",
            )
            self.assertEqual(bad_up.status_code, 400)

            # Synthetic WAV with 0.6s dead leading silence + 2.5s loud tone -> upload strips leading silence!
            sr = sd.SHOW_SR
            sil = np.zeros((int(0.6 * sr), 2), dtype=np.float32)
            t = np.linspace(0.0, 2.5, int(2.5 * sr), endpoint=False, dtype=np.float32)
            tone = np.stack([0.8 * np.sin(2.0 * np.pi * 440.0 * t), 0.8 * np.sin(2.0 * np.pi * 440.0 * t)], axis=1)
            synth_wav = sd.encode_stereo_pcm16_wav(np.concatenate([sil, tone], axis=0), sr=sr)

            up = client.post(
                "/api/shows/matt_pr/upload?filename=custom_best.wav&songStartSec=0&songDurationSec=3&trimLeadingSilence=1",
                data=synth_wav,
                content_type="audio/wav",
            )
            self.assertEqual(up.status_code, 200)
            j_up = up.get_json()
            self.assertTrue(j_up["ok"])
            self.assertEqual(j_up["show"]["sourceFile"], "matt_pr_custom_best.wav")
            self.assertTrue((tmp / "shows" / "sources" / "matt_pr_custom_best.wav").is_file())
            rebuilt = sd.read_wav_stereo_float(prev_path)
            onset_rms = float(np.sqrt(np.mean(rebuilt[: int(0.10 * sr)] ** 2)))
            self.assertGreater(onset_rms, 0.05)

            # Config update persists across reload
            cfg_res = client.post("/api/shows/matt_pr/config", json={"denonVol": 68, "songStartSec": 1.25})
            self.assertEqual(cfg_res.status_code, 200)
            reloaded = sd.ShowDirector(root_dir=tmp, allow_network=False)
            self.assertEqual(reloaded.get_show("matt_pr").denon_vol, 68)
            self.assertAlmostEqual(reloaded.get_show("matt_pr").song_start_sec, 1.25)
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
