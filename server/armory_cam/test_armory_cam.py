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
import urllib.parse

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
            self.assertEqual(adb.last_wake_ms, 0)

            # 8. Regression: stale earlier workout must NOT sleep a fresh wake/open_gym after 2s
            cmds.clear()
            svc.wallboard.update_state({"mode": "lift", "title": "Old Workout"}, workout_active=True)
            with svc.wallboard.cond:
                svc.wallboard.last_active_push_ms = now_ms - 700_000
            svc.voice.set_wallboard_active(True)
            wake_open = svc._wake_wallboard_sync(force=True)
            self.assertTrue(wake_open["ok"])
            self.assertTrue(adb.tv_awake_requested)
            self.assertFalse(svc.wallboard.workout_active)
            self.assertEqual(svc.wallboard.mode, "standby")
            self.assertEqual(svc.wallboard.last_active_push_ms, 0)
            self.assertFalse(svc.voice.wallboard_active)
            # Even if workout_active were True with stale push, check_timers respects fresh last_wake_ms
            adb.last_wake_ms = now_ms
            res_after_wake = adb.check_timers(
                svc.wallboard_cfg,
                last_active_push_ms=now_ms - 700_000,
                workout_active=True,
                now_ms=now_ms + 2_000,
            )
            self.assertIsNone(res_after_wake)
            self.assertTrue(adb.tv_awake_requested)
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
        self.assertTrue(rv.is_hallucination("back, back, back, back, back, back, back"))
        self.assertFalse(rv.is_hallucination("Armory check"))
        self.assertTrue(rv.matches_wake_word("Armery 225 for 10"))

        t_asr = np.linspace(0.0, 0.25, 4_000, endpoint=False, dtype=np.float32)
        quiet_tone = (0.08 * np.sin(2.0 * np.pi * 400.0 * t_asr)).astype(np.float32)
        boosted_tone = rv.prepare_asr_audio(quiet_tone)
        self.assertGreaterEqual(float(np.max(np.abs(boosted_tone))), 0.65)
        self.assertLessEqual(float(np.max(np.abs(boosted_tone))), 0.80)
        near_silence = (0.001 * np.sin(2.0 * np.pi * 400.0 * t_asr)).astype(np.float32)
        self.assertLess(float(np.max(np.abs(rv.prepare_asr_audio(near_silence)))), 0.01)

        # Forced max_frames flush keeps trailing PRE_ROLL_FRAMES and does not reset VAD LSTM state
        class _AlwaysSpeechVad:
            def __init__(self) -> None:
                self.resets = 0

            def reset(self) -> None:
                self.resets += 1

            def score_frame(self, _frame: np.ndarray, rms_gate: float = 0.0025) -> float:
                return 0.9

        fake_vad = _AlwaysSpeechVad()
        det = rv.UtteranceDetector(vad=fake_vad, min_speech_ms=64, end_silence_ms=160, max_utterance_sec=0.70, rms_gate=0.0025)
        resets_before = fake_vad.resets
        speech_chunk = np.full(rv.VAD_FRAME_SAMPLES * 25, 0.05, dtype=np.float32)
        utts = det.push_samples(speech_chunk)
        self.assertEqual(len(utts), 1)
        self.assertEqual(len(det._pre_roll), rv.PRE_ROLL_FRAMES)
        self.assertEqual(fake_vad.resets, resets_before)

        prompt, hotwords = rv.build_whisper_prompt("armory", ["Barbell Bench Press", "Smith Machine Row"])
        self.assertIn("Barbell Bench Press", prompt)
        self.assertIn("Smith Machine Row", hotwords)
        self.assertIn("open the gym", hotwords)
        self.assertIn("close the gym", hotwords)
        self.assertIn("shut down", hotwords)

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
            self.assertIn("vxSetCommitMode", dash)
            self.assertIn("kokoro:am_michael", dash)
            self.assertIn("vxAbBox", dash)

            ping = client.get("/api/ping").get_json()
            self.assertIn("voice", ping)
            self.assertTrue(ping["voice"]["enabled"])
            self.assertFalse(ping["voice"]["listening"])

            # Default & legacy TTS voice normalization + config persistence across service reload
            self.assertEqual(rv.RoomVoiceConfig().tts_voice, "kokoro:am_michael")
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
                "kokoro:am_michael",
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

            chimes: list[str] = []
            svc.voice._chime_fn = lambda kind: chimes.append(kind) or True

            # Default setCommitMode is "voice": logged sets speak at +10% speed (no chime)
            self.assertEqual(svc.voice.cfg.set_commit_mode, "voice")
            self.assertNotEqual(
                rv._NeuralTtsSynthesizer._cache_key("50 for 10, check", "kokoro:am_michael", speed=1.0),
                rv._NeuralTtsSynthesizer._cache_key("50 for 10, check", "kokoro:am_michael", speed=1.10),
            )

            speed_calls: list[tuple[str, float]] = []
            orig_speak_conf = svc.voice.speak_confirmation

            def _spy_speak(text: str, *, speed: float = 1.0) -> bool:
                speed_calls.append((text, speed))
                return orig_speak_conf(text, speed=speed)

            svc.voice.speak_confirmation = _spy_speak  # type: ignore[method-assign]
            try:
                ack_res = client.post(
                    "/api/voice/ack",
                    json={
                        "armed": False,
                        "speech": "225 for 8, got it",
                        "text": "✓ 1 Low Bar Squat · 225 lb × 8",
                        "kind": "ok",
                    },
                )
                self.assertEqual(ack_res.status_code, 200)
                ack_json = ack_res.get_json()
                self.assertTrue(ack_json["spoke"])
                self.assertFalse(ack_json["chime"])
                self.assertEqual(chimes, [])
                self.assertEqual(spoken, ["225 for 8, got it"])

                st = client.get("/api/voice/status").get_json()
                self.assertEqual(st["recent"][0]["ackText"], "✓ 1 Low Bar Squat · 225 lb × 8")
                self.assertIn("ttsDevice", st)
                self.assertEqual(st["setCommitMode"], "voice")

                # Errors and non-set commands speak at 1.0x speed
                err_res = client.post(
                    "/api/voice/ack",
                    json={"armed": False, "speech": "Didn't catch that", "text": "Didn’t catch “blah”", "kind": "warn"},
                )
                self.assertEqual(err_res.status_code, 200)
                self.assertTrue(err_res.get_json()["spoke"])
                self.assertFalse(err_res.get_json()["chime"])

                act_res = client.post(
                    "/api/voice/ack",
                    json={"armed": False, "speech": "Rest skipped", "text": "Rest timer skipped", "kind": "info"},
                )
                self.assertEqual(act_res.status_code, 200)
                self.assertTrue(act_res.get_json()["spoke"])
                self.assertFalse(act_res.get_json()["chime"])
                self.assertEqual(spoken, ["225 for 8, got it", "Didn't catch that", "Rest skipped"])
                self.assertEqual(
                    speed_calls,
                    [("225 for 8, got it", 1.10), ("Didn't catch that", 1.0), ("Rest skipped", 1.0)],
                )
            finally:
                svc.voice.speak_confirmation = orig_speak_conf  # type: ignore[method-assign]

            # Per-ack setCommitMode="chime" override plays commit chime without speaking
            ack_ovr = client.post(
                "/api/voice/ack",
                json={
                    "armed": False,
                    "speech": "185 for 10, check",
                    "text": "✓ 1 Low Bar Squat · 185 lb × 10",
                    "kind": "ok",
                    "setCommitMode": "chime",
                },
            )
            self.assertEqual(ack_ovr.status_code, 200)
            self.assertTrue(ack_ovr.get_json()["spoke"])
            self.assertTrue(ack_ovr.get_json()["chime"])
            self.assertEqual(chimes, ["commit"])
            self.assertEqual(len(spoken), 3)

            # Switching config -> setCommitMode="chime" & ttsVoice="kokoro:am_michael" persists & defaults to chime
            vc_res = client.post("/api/voice/config", json={"setCommitMode": "chime", "ttsVoice": "kokoro:am_michael"})
            self.assertEqual(vc_res.status_code, 200)
            self.assertEqual(vc_res.get_json()["config"]["setCommitMode"], "chime")
            self.assertEqual(vc_res.get_json()["config"]["ttsVoice"], "kokoro:am_michael")
            st2 = client.get("/api/voice/status").get_json()
            self.assertEqual(st2["setCommitMode"], "chime")
            self.assertEqual(st2["ttsVoice"], "kokoro:am_michael")

            svc.voice._last_commit_chime_mono = 0.0
            ack_c = client.post(
                "/api/voice/ack",
                json={
                    "armed": False,
                    "speech": "135 for 12, got it",
                    "text": "✓ 2 Low Bar Squat · 135 lb × 12",
                    "kind": "ok",
                },
            )
            self.assertTrue(ack_c.get_json()["spoke"])
            self.assertTrue(ack_c.get_json()["chime"])
            self.assertEqual(chimes, ["commit", "commit"])
            err_still = client.post(
                "/api/voice/ack",
                json={"armed": False, "speech": "Need weight and reps", "text": "Need weight", "kind": "warn"},
            )
            self.assertTrue(err_still.get_json()["spoke"])
            self.assertFalse(err_still.get_json()["chime"])
            self.assertEqual(spoken[-1], "Need weight and reps")

            # Commit chime PCM loader yields crisp normalized mono float32 audio (0.15s..0.55s)
            chime_pcm, chime_sr = rv._load_commit_chime_pcm()
            self.assertGreater(chime_sr, 8000)
            self.assertGreaterEqual(len(chime_pcm) / float(chime_sr), 0.15)
            self.assertLessEqual(len(chime_pcm) / float(chime_sr), 0.55)
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

        self.assertEqual(dc.parse_display_voice_command("Armory, open the gym"), {"target": "open_gym", "targets": ["open_gym"], "action": "run", "speech": "", "silent": True})
        self.assertEqual(dc.parse_display_voice_command("Armory, close the gym"), {"target": "close_gym", "targets": ["close_gym"], "action": "run", "speech": "", "silent": True})
        for close_phrase in (
            "Armory, shut down",
            "Armory, close gym",
            "Armory, close up",
            "Armory, clothes the gym",
            "Armory, first the gym",
        ):
            self.assertEqual(
                dc.parse_display_voice_command(close_phrase),
                {"target": "close_gym", "targets": ["close_gym"], "action": "run", "speech": "", "silent": True},
                msg=close_phrase,
            )
        self.assertEqual(
            dc.parse_display_voice_command("Armory, shut down TV"),
            {"target": "tv", "targets": ["tv"], "action": "off", "speech": "TV off"},
        )
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV"), {"target": "tv", "targets": ["tv"], "action": "on", "speech": "TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on SmartSpin TV"), {"target": "spin", "targets": ["spin"], "action": "on", "speech": "SmartSpin TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV and SmartSpin TV"), {"target": "multi", "targets": ["tv", "spin"], "action": "on", "speech": "TV and SmartSpin TV on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV and wallboard"), {"target": "multi", "targets": ["tv", "wallboard"], "action": "on", "speech": "TV and Wallboard on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn off all TVs"), {"target": "all_tvs", "targets": ["tv", "wallboard", "spin"], "action": "off", "speech": "All TVs off"})
        self.assertEqual(dc.parse_display_voice_command("Armory turn off hisense")["action"], "off")
        self.assertEqual(dc.parse_display_voice_command("Armory switch primary to PC")["action"], "hdmi1")
        self.assertEqual(dc.parse_display_voice_command("Armory switch TV to Fire TV")["action"], "hdmi2")
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on Fire TV Cube"), {"target": "cube", "targets": ["cube"], "action": "on", "speech": "Fire TV Cube on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn off Fire TV Cube"), {"target": "cube", "targets": ["cube"], "action": "off", "speech": "Fire TV Cube off"})
        self.assertEqual(dc.parse_display_voice_command("Armory, sleep the cube"), {"target": "cube", "targets": ["cube"], "action": "off", "speech": "Fire TV Cube off"})
        self.assertEqual(dc.parse_display_voice_command("Armory, wake up Fire TV Cube"), {"target": "cube", "targets": ["cube"], "action": "on", "speech": "Fire TV Cube on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, switch on Fire TV Cube"), {"target": "cube", "targets": ["cube"], "action": "on", "speech": "Fire TV Cube on"})
        self.assertEqual(dc.parse_display_voice_command("Armory, switch off Fire TV Cube"), {"target": "cube", "targets": ["cube"], "action": "off", "speech": "Fire TV Cube off"})
        self.assertEqual(dc.parse_display_voice_command("Armory, Fire TV Cube home"), {"target": "cube", "targets": ["cube"], "action": "home", "speech": "Fire TV Cube home"})
        self.assertEqual(dc.parse_display_voice_command("Armory, turn on TV and Fire TV Cube"), {"target": "multi", "targets": ["tv", "cube"], "action": "on", "speech": "TV and Fire TV Cube on"})
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
            if "ro.product.model" in cmd:
                return 0, "AFTGAZL\n", ""
            if "dumpsys" in cmd and "power" in cmd:
                return 0, "  mWakefulness=Awake\nDisplay Power: state=ON\n", ""
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
        roku_posts.clear()
        wb_calls.clear()
        og = ctrl.execute("open_gym", "run")
        self.assertTrue(og["ok"])
        self.assertIn("PWON", denon_cmds)
        self.assertIn("SIMPLAY", denon_cmds)
        self.assertIn("MSMCH%20STEREO", denon_cmds)
        self.assertEqual(wb_calls, ["wake"])
        self.assertIn("http://192.168.1.88:8060/keypress/PowerOn", roku_posts)
        self.assertIn("http://192.168.1.88:8060/keypress/InputHDMI1", roku_posts)
        self.assertTrue(any("192.168.1.230:5555" in c and c[-3:] == ["input", "keyevent", "244"] for c in adb_cmds))
        self.assertTrue(any("https://www.youtube.com/playlist?list=PLgym99" in " ".join(c) and "192.168.1.151:5555" in c for c in adb_cmds))

        adb_cmds.clear()
        self.assertTrue(ctrl.execute("cube", "on")["ok"])
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "224"] for c in adb_cmds))
        self.assertFalse(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "26"] for c in adb_cmds))
        self.assertTrue(ctrl.execute("cube", "off")["ok"])
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "127"] for c in adb_cmds))
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "223"] for c in adb_cmds))
        self.assertTrue(ctrl.execute("cube", "home")["ok"])
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "3"] for c in adb_cmds))
        cube_probe = ctrl.probe_cube()
        self.assertTrue(cube_probe["ok"])
        self.assertEqual(cube_probe["model"], "AFTGAZL")
        self.assertEqual(cube_probe["wakefulness"], "Awake")
        self.assertEqual(cube_probe["displayPower"], "ON")

        adb_cmds.clear()
        denon_cmds.clear()
        roku_posts.clear()
        wb_calls.clear()
        cg = ctrl.execute("close_gym", "run")
        self.assertTrue(cg["ok"])
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "127"] for c in adb_cmds))
        self.assertTrue(any("192.168.1.151:5555" in c and c[-3:] == ["input", "keyevent", "223"] for c in adb_cmds))
        self.assertIn("PWSTANDBY", denon_cmds)
        self.assertNotIn("VSMONI1", denon_cmds)
        self.assertIn("http://192.168.1.88:8060/keypress/PowerOff", roku_posts)
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
            spoken_msgs: list[str] = []
            svc.voice._speak_fn = lambda txt: (spoken_msgs.append(txt), True)[1]
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            self.assertIn("Gym Displays &amp; Discrete Power Control", dash)
            self.assertIn("vxRoomCmdReadback", dash)
            self.assertIn("dpPrimHost", dash)
            self.assertIn("dpSpinHost", dash)
            self.assertIn("dpMusicUrl", dash)
            self.assertIn('data-dpact="cube:on"', dash)
            self.assertIn('data-dpact="cube:off"', dash)
            self.assertIn('data-dpact="cube:home"', dash)
            self.assertIn("dpProbeCubeBtn", dash)

            svc.voice._stop_event.clear()
            stream = svc.voice.subscribe_sse()
            _init_frame = next(stream)

            # Display voice command handled locally — must NOT emit to Pixel SSE
            disp_evt = svc.voice.inject_transcript("Armory, turn on SmartSpin TV")
            self.assertTrue(disp_evt["localHandled"])
            self.assertIn("SmartSpin TV on", disp_evt["ackText"])

            # Open gym & Close gym handled locally, silently (zero TTS confirmation)
            spoken_msgs.clear()
            svc.voice.set_context(active=True, readback=True)
            open_evt = svc.voice.inject_transcript("Armory, open the gym")
            self.assertTrue(open_evt["localHandled"])
            self.assertIn("Gym opened", open_evt["ackText"])
            self.assertEqual(spoken_msgs, [])

            close_evt = svc.voice.inject_transcript("Armory, close the gym")
            self.assertTrue(close_evt["localHandled"])
            self.assertEqual(close_evt["ackText"], "✓ Gym closed")
            self.assertEqual(spoken_msgs, [])

            # roomCommandReadback=False silences room/TV voice replies while workout readback stays enabled
            cfg_rc = client.post("/api/voice/config", json={"roomCommandReadback": False})
            self.assertEqual(cfg_rc.status_code, 200)
            self.assertFalse(cfg_rc.get_json()["config"]["roomCommandReadback"])
            self.assertFalse(client.get("/api/voice/status").get_json()["roomCommandReadback"])
            spoken_msgs.clear()
            quiet_tv = svc.voice.inject_transcript("Armory, turn on SmartSpin TV")
            self.assertTrue(quiet_tv["localHandled"])
            self.assertEqual(spoken_msgs, [])
            client.post("/api/voice/config", json={"roomCommandReadback": True})

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

            cube_act_res = client.post("/api/displays/action", json={"target": "cube", "action": "on"})
            self.assertEqual(cube_act_res.status_code, 200)
            self.assertTrue(cube_act_res.get_json()["ok"])

            cube_st_res = client.get("/api/displays/status?probe=cube")
            self.assertEqual(cube_st_res.status_code, 200)
            self.assertTrue(cube_st_res.get_json()["cube"]["probe"]["ok"])
            self.assertEqual(cube_st_res.get_json()["cube"]["probe"]["model"], "AFTGAZL")

            multi_res = client.post("/api/displays/action", json={"targets": ["tv", "wallboard"], "action": "on"})
            self.assertEqual(multi_res.status_code, 200)
            self.assertTrue(multi_res.get_json()["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_denon_ip_control_auto_provisioner_and_safety_guards(self) -> None:
        ha_cfg = ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15")
        denon_cli = ha.DenonAvrClient(lambda: ha_cfg, http_get=lambda _u, _t: (200, b""), sleep_fn=lambda _s: None)

        # 1. Dedicated radio form (Off in Standby -> Always On) with read-before-write + verify + cache noop
        tmp1 = Path(tempfile.mkdtemp(prefix="denon_ipctrl_1_"))
        try:
            mode_state = {"val": "STANDBY"}
            get_calls: list[str] = []
            post_calls: list[tuple[str, bytes]] = []

            def fake_get_1(url: str, _to: float) -> tuple[int, bytes]:
                get_calls.append(url)
                if url.endswith("/SETUP/l_menu.asp"):
                    return 200, b'<a href="/SETUP/NETWORK/d_ipcontrol.asp">IP Control</a>'
                if url.endswith("/SETUP/NETWORK/d_ipcontrol.asp"):
                    chk_a = " checked" if mode_state["val"] == "ALWAYS" else ""
                    chk_s = " checked" if mode_state["val"] == "STANDBY" else ""
                    html_doc = (
                        '<FORM name="ipcontrol" action="s_ipcontrol.asp" method="post">'
                        '<INPUT type="hidden" name="tokenId" value="42">'
                        f'<INPUT type="radio" name="radioIpControl" value="ALWAYS"{chk_a}>Always On'
                        f'<INPUT type="radio" name="radioIpControl" value="STANDBY"{chk_s}>Off In Standby'
                        '</FORM>'
                    )
                    return 200, html_doc.encode("ascii")
                return 404, b""

            def fake_post_1(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                post_calls.append((url, body))
                parsed = urllib.parse.parse_qs(body.decode("ascii"))
                mode_state["val"] = parsed["radioIpControl"][0]
                return 200, b""

            ctrl1 = dc.GymDisplayController(
                denon_client=denon_cli,
                http_get=fake_get_1,
                http_post=fake_post_1,
                sleep_fn=lambda _s: None,
                allow_network=False,
                state_dir=tmp1,
            )
            res1 = ctrl1.ensure_denon_ip_control_always_on()
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["state"], "verified_always_on")
            self.assertTrue(res1["changed"])
            self.assertEqual(len(post_calls), 1)
            self.assertTrue(post_calls[0][0].endswith("/SETUP/NETWORK/s_ipcontrol.asp"))
            post_qs = urllib.parse.parse_qs(post_calls[0][1].decode("ascii"))
            self.assertEqual(post_qs["tokenId"], ["42"])
            self.assertEqual(post_qs["radioIpControl"], ["ALWAYS"])
            self.assertTrue((tmp1 / "denon_ip_control_state.json").is_file())
            self.assertTrue((tmp1 / "denon_network_dump.json").is_file())
            self.assertEqual(ctrl1.status_dict()["denonIpControl"]["state"], "verified_always_on")

            # Subsequent non-forced call is instant cached no-op (zero extra HTTP calls)
            prev_gets = len(get_calls)
            res1_cached = ctrl1.ensure_denon_ip_control_always_on()
            self.assertEqual(res1_cached["state"], "verified_always_on")
            self.assertEqual(len(get_calls), prev_gets)
            self.assertEqual(len(post_calls), 1)

            # 2. Reload controller from same state_dir when already Always On -> zero POSTs
            post_calls.clear()
            ctrl1_reloaded = dc.GymDisplayController(
                denon_client=denon_cli,
                http_get=fake_get_1,
                http_post=fake_post_1,
                sleep_fn=lambda _s: None,
                allow_network=False,
                state_dir=tmp1,
            )
            res_already = ctrl1_reloaded.ensure_denon_ip_control_always_on(force=True)
            self.assertTrue(res_already["ok"])
            self.assertEqual(res_already["state"], "verified_always_on")
            self.assertFalse(res_already["changed"])
            self.assertEqual(post_calls, [])
        finally:
            shutil.rmtree(tmp1, ignore_errors=True)

        # 3. Safety Guard 1: reject combined DHCP / IP address form (zero POSTs, saves dump, needs_review)
        tmp2 = Path(tempfile.mkdtemp(prefix="denon_ipctrl_2_"))
        try:
            unsafe_posts: list[str] = []

            def fake_get_unsafe(url: str, _to: float) -> tuple[int, bytes]:
                if url.endswith("/SETUP/NETWORK/d_network.asp"):
                    html_doc = (
                        '<FORM name="net" action="s_network.asp">'
                        '<INPUT type="radio" name="radioDhcp" value="ON" checked>DHCP On'
                        '<INPUT type="text" name="txtIpAddress" value="192.168.1.15">'
                        '<INPUT type="radio" name="radioIpControl" value="ALWAYS">Always On'
                        '<INPUT type="radio" name="radioIpControl" value="STANDBY" checked>Off In Standby'
                        '</FORM>'
                    )
                    return 200, html_doc.encode("ascii")
                return 404, b""

            ctrl2 = dc.GymDisplayController(
                denon_client=denon_cli,
                http_get=fake_get_unsafe,
                http_post=lambda u, _b, _h, _t: (unsafe_posts.append(u), (200, b""))[1],
                sleep_fn=lambda _s: None,
                allow_network=False,
                state_dir=tmp2,
            )
            res2 = ctrl2.ensure_denon_ip_control_always_on()
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["state"], "needs_review")
            self.assertEqual(unsafe_posts, [])
            self.assertTrue((tmp2 / "denon_network_dump.json").is_file())
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # 4. <SELECT> form variant works cleanly & 5. Unreachable Denon stays pending
        tmp3 = Path(tempfile.mkdtemp(prefix="denon_ipctrl_3_"))
        try:
            sel_state = {"val": "0"}
            sel_posts: list[bytes] = []

            def fake_get_select(url: str, _to: float) -> tuple[int, bytes]:
                if url.endswith("/SETUP/NETWORK/d_netcontrol.asp"):
                    s0 = " selected" if sel_state["val"] == "0" else ""
                    s1 = " selected" if sel_state["val"] == "1" else ""
                    html_doc = (
                        '<FORM name="nc">'
                        '<SELECT name="listNetControl">'
                        f'<OPTION value="0"{s0}>Off In Standby</OPTION>'
                        f'<OPTION value="1"{s1}>Always On</OPTION>'
                        '</SELECT></FORM>'
                    )
                    return 200, html_doc.encode("ascii")
                return 404, b""

            def fake_post_select(url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                self.assertTrue(url.endswith("/SETUP/NETWORK/s_netcontrol.asp"))
                sel_posts.append(body)
                sel_state["val"] = urllib.parse.parse_qs(body.decode("ascii"))["listNetControl"][0]
                return 200, b""

            ctrl3 = dc.GymDisplayController(
                denon_client=denon_cli,
                http_get=fake_get_select,
                http_post=fake_post_select,
                sleep_fn=lambda _s: None,
                allow_network=False,
                state_dir=tmp3,
            )
            res3 = ctrl3.ensure_denon_ip_control_always_on()
            self.assertTrue(res3["ok"])
            self.assertEqual(res3["state"], "verified_always_on")
            self.assertEqual(len(sel_posts), 1)
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)

        tmp4 = Path(tempfile.mkdtemp(prefix="denon_ipctrl_4_"))
        try:
            def fake_get_down(_url: str, _to: float) -> tuple[int, bytes]:
                raise TimeoutError("timed out")

            ctrl4 = dc.GymDisplayController(
                denon_client=denon_cli,
                http_get=fake_get_down,
                http_post=lambda _u, _b, _h, _t: (200, b""),
                sleep_fn=lambda _s: None,
                allow_network=False,
                state_dir=tmp4,
            )
            res4 = ctrl4.ensure_denon_ip_control_always_on()
            self.assertFalse(res4["ok"])
            self.assertEqual(res4["state"], "pending")
            self.assertFalse(res4.get("reachable", True))
            self.assertEqual(ctrl4.get_denon_ip_control_state()["state"], "pending")
        finally:
            shutil.rmtree(tmp4, ignore_errors=True)

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

    def test_delete_clip_rejects_wildcards_and_traversal(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_del_guard_"))
        try:
            root = tmp / "camroot"
            drive_dir = tmp / "drive_clips"
            svc = ac.ArmoryCamService(root_dir=root, drive_backup_dir=drive_dir)
            app = ac.create_app(svc)
            client = app.test_client()

            day = "2026-11-19"
            (svc.clips_root / day).mkdir(parents=True, exist_ok=True)
            (drive_dir / day).mkdir(parents=True, exist_ok=True)
            fname = "Squat_0_abc120.mp4"
            local_clip = svc.clips_root / day / fname
            drive_clip = drive_dir / day / fname
            local_clip.write_bytes(b"mp4-bytes")
            drive_clip.write_bytes(b"mp4-bytes")
            svc.clips_index_path.write_text(
                json.dumps({"fileName": fname, "lanUrl": f"/clips/{day}/{fname}", "camera": "Rack Cam"}) + "\n",
                encoding="utf-8",
            )

            for bad_payload in (
                {"fileName": "*"},
                {"fileName": "*.mp4"},
                {"fileName": f"../{fname}"},
                {"lanUrl": f"/clips/{day}/*.mp4"},
                {"lanUrl": "/clips/2026-11-19/Squat_0_abc120.mp4", "fileName": "*.mp4"},
            ):
                res = client.post("/api/clip/delete", json=bad_payload)
                self.assertEqual(res.status_code, 400, msg=str(bad_payload))
                self.assertFalse(res.get_json()["ok"])

            self.assertTrue(local_clip.is_file())
            self.assertTrue(drive_clip.is_file())
            self.assertEqual(len(client.get("/api/clips").get_json()["clips"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cors_origin_enforcement_blocks_external_posts(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_cors_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            evil = client.post("/api/session/start", json={"sessionId": "x"}, headers={"Origin": "https://evil.example"})
            self.assertEqual(evil.status_code, 403)
            self.assertIsNone(evil.headers.get("Access-Control-Allow-Origin"))
            self.assertIsNone(evil.headers.get("Access-Control-Allow-Private-Network"))

            cap = client.post("/api/session/start", json={"sessionId": "s1"}, headers={"Origin": "capacitor://localhost"})
            self.assertEqual(cap.status_code, 200)
            self.assertEqual(cap.headers.get("Access-Control-Allow-Origin"), "capacitor://localhost")
            self.assertIsNone(cap.headers.get("Access-Control-Allow-Private-Network"))

            lan = client.post(
                "/api/session/stop",
                json={"sessionId": "s1"},
                headers={"Origin": "http://192.168.1.50:8766"},
            )
            self.assertEqual(lan.status_code, 200)
            self.assertEqual(lan.headers.get("Access-Control-Allow-Origin"), "http://192.168.1.50:8766")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sapi_removed_and_prompt_echo_filtering(self) -> None:
        self.assertFalse(hasattr(rv, "_SapiSynthesizer"))
        self.assertNotIn("sapi", rv.TTS_VOICES)
        self.assertEqual(rv.RoomVoiceConfig.from_dict({"ttsVoice": "sapi"}).tts_voice, "kokoro:am_michael")
        self.assertNotIn('value="sapi"', ac.DASHBOARD_HTML)

        prompt, _hotwords = rv.build_whisper_prompt("armory", ["Bench Press"])
        long_echo = " ".join(prompt.replace(",", " ").split()[:10])
        self.assertTrue(rv.is_prompt_echo(long_echo, prompt))
        for real_cmd in ("Armory Matt just hit a PR", "wake", "log 225 for 10", "Armory next exercise"):
            self.assertFalse(rv.is_prompt_echo(real_cmd, prompt), msg=real_cmd)

    def test_optiplex_config_backup_and_restore_roundtrip(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_cfg_bkp_"))
        try:
            root = tmp / "camroot"
            drive_dir = tmp / "drive_root" / "Clips"
            svc = ac.ArmoryCamService(root_dir=root, drive_backup_dir=drive_dir)
            (root / "shows.json").write_text(json.dumps({"shows": [{"id": "matt_pr"}]}), encoding="utf-8")
            (root / "tts_secrets.json").write_text(json.dumps({"openaiApiKey": "sk-test-123"}), encoding="utf-8")

            app = ac.create_app(svc)
            client = app.test_client()

            b_res = client.post("/api/config/backup", json={})
            self.assertEqual(b_res.status_code, 200)
            b_json = b_res.get_json()
            self.assertTrue(b_json["ok"])
            self.assertIn("config.json", b_json["files"])
            self.assertIn("shows.json", b_json["files"])
            self.assertIn("tts_secrets.json", b_json["files"])

            bkp_dir = tmp / "drive_root" / "OptiPlex"
            self.assertTrue((bkp_dir / "manifest.json").is_file())
            self.assertTrue((bkp_dir / "tts_secrets.json").is_file())

            (root / "shows.json").unlink()
            (root / "tts_secrets.json").unlink()

            r_res = client.post("/api/config/restore", json={"overwrite": False})
            self.assertEqual(r_res.status_code, 200)
            r_json = r_res.get_json()
            self.assertTrue(r_json["ok"])
            self.assertIn("shows.json", r_json["restored"])
            self.assertIn("tts_secrets.json", r_json["restored"])
            self.assertEqual(json.loads((root / "tts_secrets.json").read_text(encoding="utf-8"))["openaiApiKey"], "sk-test-123")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_denon_partial_ramp_restore_and_manual_override(self) -> None:
        state: dict[str, object] = {"vol": -16.0, "fail_after": None, "mv_calls": 0}

        def fake_get(url: str, _to: float) -> tuple[int, bytes]:
            if "formMainZone_MainZoneXmlStatus.xml" in url:
                xml = (
                    f"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect>"
                    f"<MasterVolume><value>{float(state['vol']):.1f}</value></MasterVolume><Mute><value>off</value></Mute></item>"
                )
                return 200, xml.encode("utf-8")
            if "?" in url:
                cmd = url.split("?", 1)[1]
                if cmd.startswith("MV"):
                    state["mv_calls"] = int(state["mv_calls"]) + 1
                    fail_after = state["fail_after"]
                    if fail_after is not None and int(state["mv_calls"]) > int(fail_after):
                        return 500, b""
                    raw = cmd[2:]
                    val = int(raw[:2]) + (0.5 if len(raw) == 3 and raw[2] == "5" else 0.0)
                    state["vol"] = val - 80.0
            return 200, b""

        cfg = ha.HouseAudioConfig(
            denon_enabled=True,
            denon_host="192.168.1.15",
            denon_duck_enabled=True,
            denon_duck_threshold_db=-20.0,
            denon_duck_drop_db=5.0,
            denon_ramp_steps=4,
            denon_ramp_step_ms=0,
        )
        denon = ha.DenonAvrClient(lambda: cfg, http_get=fake_get, sleep_fn=lambda _s: None, allow_network=True)

        # Partial duck-down (only first 2 MV commands succeed, volume reaches -18.5):
        # exit still restores cleanly to -16.0 rather than misclassifying as manual knob change.
        state["fail_after"] = 2
        with denon.duck_context() as d1:
            self.assertTrue(d1["ducked"])
            state["fail_after"] = None
        self.assertAlmostEqual(float(state["vol"]), -16.0)
        self.assertIsNone(denon.last_duck_info.get("restoreSkipped"))

        # External manual knob movement mid-duck is still preserved and never overwritten
        with denon.duck_context() as d2:
            self.assertTrue(d2["ducked"])
            state["vol"] = -12.0
        self.assertAlmostEqual(float(state["vol"]), -12.0)
        self.assertEqual(denon.last_duck_info.get("restoreSkipped"), "manual_volume_change")

    def test_adb_status_nonblocking_and_failed_sleep_rearms_retry(self) -> None:
        def offline_runner(cmd: list[str], _to: float) -> tuple[int, str, str]:
            args = cmd[1:]
            if args[:1] == ["devices"]:
                return 0, "List of devices attached\n192.168.1.204:5555\toffline\n", ""
            return 0, "", ""

        adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=offline_runner, settle_sec=0.0, wol_wait_sec=0.0)
        cfg = ac.WallboardConfig(enabled=True, fire_tv_host="192.168.1.204")

        # 1. Holding op_lock from another thread must not block status_dict()
        held = threading.Event()
        release = threading.Event()

        def blocker() -> None:
            with adb.op_lock:
                held.set()
                release.wait(timeout=2.0)

        t = threading.Thread(target=blocker, daemon=True)
        t.start()
        try:
            self.assertTrue(held.wait(timeout=1.0))
            t0 = time.monotonic()
            st = adb.status_dict(cfg)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.2)
            self.assertEqual(st["fireTvHost"], "192.168.1.204")
        finally:
            release.set()
            t.join(timeout=1.0)

        # 2. Failed auto-sleep on offline TV re-arms a backoff retry rather than clearing pending sleep
        adb.tv_awake_requested = True
        now_ms = int(time.time() * 1000)
        adb.pending_sleep_at_ms = now_ms - 1000
        res = adb.sleep_now(cfg, reason="workout_end", only_if_pending=True)
        self.assertFalse(res["ok"])
        self.assertIsNotNone(adb.pending_sleep_at_ms)
        self.assertGreater(adb.pending_sleep_at_ms or 0, now_ms)
        self.assertEqual(adb._sleep_retry_count, 1)

    def test_run_server_preserves_none_root_dir(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_run_srv_"))
        orig_sys_hook = sys.excepthook
        orig_thr_hook = threading.excepthook
        try:
            fake_svc = mock.MagicMock()
            fake_svc.port = 8766
            fake_svc.root = tmp
            fake_svc.ffmpeg = "ffmpeg"
            fake_srv = mock.MagicMock()
            with (
                mock.patch.object(ac, "default_root_dir", return_value=tmp),
                mock.patch.object(ac.logging.handlers, "RotatingFileHandler", return_value=mock.MagicMock()),
                mock.patch.object(ac.logging, "basicConfig"),
                mock.patch.object(ac, "ArmoryCamService", return_value=fake_svc) as m_svc,
                mock.patch.object(ac, "create_app", return_value=mock.MagicMock()),
                mock.patch.object(ac, "make_server", return_value=fake_srv),
            ):
                ac.run_server(root_dir=None)
            m_svc.assert_called_once_with(root_dir=None)
            fake_svc.start_background.assert_called_once()
            fake_srv.serve_forever.assert_called_once()
            fake_svc.stop_background.assert_called_once()
        finally:
            sys.excepthook = orig_sys_hook
            threading.excepthook = orig_thr_hook
            for h in list(ac.logging.getLogger().handlers):
                h.close()
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
            self.assertIn("I PR'd", prompt)
            self.assertIn("Matt PR'd", prompt)
            self.assertIn("Matt just got here", prompt)
            self.assertIn("Ryder is here", prompt)
            self.assertIn("Jared and Mark", prompt)

            sd_dir = svc.shows
            self.assertEqual(sd_dir.match_voice_command("Armory, Matt just hit a PR")["showId"], "matt_pr")
            self.assertEqual(sd_dir.match_voice_command("Hey Armory matt hit a p.r.")["showId"], "matt_pr")
            for pr_phrase in (
                "Armory, Matt PR'd",
                "Armory, I PR'd",
                "Armory, I P.R.'d",
                "Armory, Ryder just PR'd",
                "Armory, I just hit a PR",
                "Armory, we broke a PR",
                "Armory, new PR",
                "Armory, PR",
            ):
                self.assertEqual(sd_dir.match_voice_command(pr_phrase)["showId"], "matt_pr", msg=pr_phrase)
            self.assertEqual(sd_dir.match_voice_command("Armory Matt just got here")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Matt's in the gym")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory, Matt is here")["showId"], "matt_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Ryder is here")["showId"], "ryder_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Rider debut")["showId"], "ryder_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Jared and Mark are here")["showId"], "jared_mark_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Gabe is here")["showId"], "gabe_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory Josh is here")["showId"], "josh_walkin")
            self.assertEqual(sd_dir.match_voice_command("Armory stop show")["action"], "stop")
            self.assertIsNone(sd_dir.match_voice_command("Armory, incline press 185 for 6"))
            self.assertIsNone(sd_dir.match_voice_command("Armory 225 for 8"))
            self.assertIsNone(sd_dir.match_voice_command("Armory open the gym"))

            # Disabling matt_pr cleanly disables both trigger matching and the global PR fallback
            sd_dir.shows["matt_pr"].enabled = False
            self.assertIsNone(sd_dir.match_voice_command("Armory, I PR'd"))
            sd_dir.shows["matt_pr"].enabled = True

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
            # Wallboard TV MUST force-stop YouTube and relaunch /wallboard when 3-screen show finishes
            idx_yt_stop = next(
                (i for i, s in enumerate(joined_adb) if "192.168.1.204:5555" in s and "force-stop com.amazon.firetv.youtube" in s),
                -1,
            )
            self.assertGreaterEqual(idx_yt_stop, 0)
            self.assertTrue(
                any(
                    "192.168.1.204:5555" in s and "am start" in s and "/wallboard" in s
                    for s in joined_adb[idx_yt_stop:]
                )
            )

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
            for fn_sig in (
                "function syncShowActiveBadge(",
                "function populateFxHookInputs(",
                "function populateTtsProviders(",
                "function populateRyderDevPanel(",
                "function renderShows(",
            ):
                self.assertIn(fn_sig, dash)

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

            # Real studio SFX WAV hooks: HouseAudio pr_fanfare uses sfx/pr_bell_crowd.wav when present
            sent_bodies: list[bytes] = []
            (tmp / "sfx").mkdir(parents=True, exist_ok=True)
            bell_wav = sd.encode_stereo_pcm16_wav(
                np.ones((4800, 2), dtype=np.float32) * 0.25, sr=48000
            )
            (tmp / "sfx" / "pr_bell_crowd.wav").write_bytes(bell_wav)
            ham_bell = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(cube_enabled=True, cube_host="192.168.1.151"),
                allow_network=False,
                cube_http_post=lambda _u, body, _h, _t: (sent_bodies.append(body), (200, b'{"ok":true}'))[1],
            )
            tp = ham_bell.test_play(kind="pr_fanfare", target="cube")
            self.assertTrue(tp["ok"])
            self.assertEqual(sent_bodies[0], bell_wav)
            self.assertGreater(len(ha.synth_pr_fanfare()), 1000)

            # ShowDirector loads shows/fx/{thunder_rumble,thunder_crack,stadium_crowd}.wav when present
            fx_dir = tmp / "shows" / "fx"
            fx_dir.mkdir(parents=True, exist_ok=True)
            for fn in ("thunder_rumble.wav", "thunder_crack.wav", "stadium_crowd.wav"):
                marker = np.ones((sd.SHOW_SR * 2, 2), dtype=np.float32) * 0.20
                (fx_dir / fn).write_bytes(sd.encode_stereo_pcm16_wav(marker, sr=sd.SHOW_SR))
            rb = svc.shows.build_show_preview("ryder_walkin", force=True)
            self.assertTrue(rb["ok"])

            lst = client.get("/api/shows")
            self.assertEqual(lst.status_code, 200)
            j_lst = lst.get_json()
            self.assertTrue(j_lst["ok"])
            ids = {s["id"] for s in j_lst["shows"]}
            self.assertEqual(
                ids,
                {
                    "matt_pr",
                    "matt_walkin",
                    "ryder_walkin",
                    "jenna_walkin",
                    "avenleigh_walkin",
                    "jared_mark_walkin",
                    "jared_mark_vip",
                    "gabe_walkin",
                    "josh_walkin",
                },
            )

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

    def test_adb_stale_offline_recovery_autoplay_off_and_clean_disconnect(self) -> None:
        cmds: list[list[str]] = []
        evicted = {"done": False}

        def fake_runner(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            args = cmd[1:]
            cmds.append(args)
            if args == ["disconnect", "192.168.1.204:5555"]:
                return 0, "disconnected 192.168.1.204:5555", ""
            if args == ["reconnect", "offline"]:
                evicted["done"] = True
                return 0, "", ""
            if args == ["connect", "192.168.1.204:5555"]:
                if not evicted["done"]:
                    return 0, "already connected to 192.168.1.204:5555", ""
                return 0, "connected to 192.168.1.204:5555", ""
            if args == ["devices"]:
                st = "device" if evicted["done"] else "offline"
                return 0, f"List of devices attached\n192.168.1.204:5555\t{st}\n", ""
            if "ro.product.model" in args:
                return 0, "AFTBAMR311\n", ""
            return 0, "ok\n", ""

        adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=fake_runner, settle_sec=0.0, wol_wait_sec=0.0)
        cfg = ac.WallboardConfig(
            enabled=True,
            fire_tv_host="192.168.1.204",
            fire_tv_mac="08:97:98:1f:4c:2b",
            adb_port=5555,
            display_client="armory",
            suppress_screensaver=True,
        )

        # 1. Probe auto-recovers from stale "already connected" + offline, sets autoplay off, and disconnects cleanly
        probe = adb.connect_and_probe(cfg)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["state"], "ready")
        self.assertIn(["disconnect", "192.168.1.204:5555"], cmds)
        self.assertIn(["reconnect", "offline"], cmds)
        flat = [" ".join(c) for c in cmds]
        self.assertTrue(any("featured_item_video_enabled 0" in s for s in flat))
        self.assertTrue(any("featured_item_video_volume 0.0" in s for s in flat))
        self.assertEqual(cmds[-1], ["disconnect", "192.168.1.204:5555"])

        # 2. Wake also applies autoplay-off + screensaver suppression and disconnects cleanly
        evicted["done"] = False
        cmds.clear()
        wk = adb.wake_and_launch(cfg, "http://192.168.1.152:8766/wallboard", force=True)
        self.assertTrue(wk["ok"])
        flat_w = [" ".join(c) for c in cmds]
        self.assertTrue(any("featured_item_video_enabled 0" in s for s in flat_w))
        self.assertTrue(any("screensaver_enabled 0" in s for s in flat_w))
        self.assertEqual(cmds[-1], ["disconnect", "192.168.1.204:5555"])

        # 3. Sleep disconnects cleanly after KEYCODE_SLEEP
        cmds.clear()
        sl = adb.sleep_now(cfg, reason="manual")
        self.assertTrue(sl["ok"])
        self.assertTrue(any("KEYCODE_SLEEP" in " ".join(c) for c in cmds))
        self.assertEqual(cmds[-1], ["disconnect", "192.168.1.204:5555"])

    def test_house_audio_cube_wav_normalization_dedup_poll_and_timing(self) -> None:
        import io
        import wave

        # 1. 48 kHz stereo PCM16 WAV passes through untouched; 24 kHz mono upconverts to 48 kHz stereo PCM16
        stereo_pcm = np.ones((4800, 2), dtype=np.float32) * 0.25
        wav_48k_st = sd.encode_stereo_pcm16_wav(stereo_pcm, sr=48_000)
        self.assertEqual(ha.normalize_wav_for_cube(wav_48k_st), wav_48k_st)

        mono_24k = 0.3 * np.sin(2.0 * np.pi * 440.0 * np.linspace(0.0, 0.2, 4800, endpoint=False, dtype=np.float32))
        wav_24k_mono = ha.encode_mono_pcm_to_wav(mono_24k, 24_000)
        norm = ha.normalize_wav_for_cube(wav_24k_mono, target_sr=48_000, target_ch=2)
        with wave.open(io.BytesIO(norm), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 2)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 48_000)
            self.assertEqual(wf.getnframes(), 9600)

        # 2. status_dict probes Denon & Cube ONCE (not twice via resolve_voice_target), and test_play returns timing + normalized WAV
        counts = {"denon": 0, "health": 0}
        posted_wavs: list[bytes] = []

        def fake_denon_get(url: str, _to: float) -> tuple[int, bytes]:
            counts["denon"] += 1
            return 200, b"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect><MasterVolume><value>-20.0</value></MasterVolume><Mute><value>off</value></Mute></item>"

        def fake_cube_get(url: str, _to: float) -> tuple[int, bytes]:
            counts["health"] += 1
            return 200, b'{"ok":true,"version":"1.1","playCount":1}'

        def fake_cube_post(_url: str, body: bytes, _hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
            posted_wavs.append(body)
            return 200, b'{"ok":true,"queued":true}'

        tmp = Path(tempfile.mkdtemp(prefix="armory_ha_norm_"))
        try:
            mgr = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(cube_enabled=True, cube_host="192.168.1.151", output_route="auto"),
                allow_network=False,
                denon_http_get=fake_denon_get,
                cube_http_get=fake_cube_get,
                cube_http_post=fake_cube_post,
                sleep_fn=lambda _s: None,
            )
            st = mgr.status_dict(live_probe=True)
            self.assertEqual(st["activeRoute"], "cube")
            self.assertEqual(counts["denon"], 1)
            self.assertEqual(counts["health"], 1)

            tp = mgr.test_play(
                kind="tts",
                mode="overlay",
                target="cube",
                text="225 for 8, got it",
                synthesize_tts_fn=lambda _txt: (mono_24k, 24_000),
            )
            self.assertTrue(tp["ok"])
            self.assertIn("timing", tp)
            self.assertGreaterEqual(tp["timing"]["totalMs"], 0)
            self.assertEqual(len(posted_wavs), 1)
            with wave.open(io.BytesIO(posted_wavs[0]), "rb") as wf_post:
                self.assertEqual((wf_post.getnchannels(), wf_post.getframerate()), (2, 48_000))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_room_voice_device_cache_and_winmm_truncated_name_matching(self) -> None:
        # 1. list_audio_devices() caches PortAudio snapshot for TTL unless force_refresh=True
        fake_devs = [
            {"name": "Anker PowerConf S330", "hostapi": 0, "default_samplerate": 48000, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Speakers (2- Anker PowerConf S330)", "hostapi": 0, "default_samplerate": 48000, "max_input_channels": 0, "max_output_channels": 2},
        ]
        rv._AUDIO_DEVICES_CACHE = None
        rv._AUDIO_DEVICES_TS = 0.0
        with (
            mock.patch.object(rv.sd, "query_devices", return_value=fake_devs) as m_qdev,
            mock.patch.object(rv.sd, "query_hostapis", return_value={"name": "Windows WASAPI"}),
        ):
            s1 = rv.list_audio_devices()
            s2 = rv.list_audio_devices()
            self.assertEqual(len(s1["inputs"]), 1)
            self.assertEqual(s1["outputs"][0]["hostApi"], "Windows WASAPI")
            self.assertEqual(m_qdev.call_count, 1)
            rv.list_audio_devices(force_refresh=True)
            self.assertEqual(m_qdev.call_count, 2)
        rv._AUDIO_DEVICES_CACHE = None

        # 2. Windows 31-char WAVEOUTCAPSW truncation ('Speakers (2- Anker PowerConf S3') resolves cleanly
        winmm_devs = [
            (0, "Speakers (2- Anker PowerConf S3"),
            (1, "HISENSE (NVIDIA High Definition"),
        ]
        self.assertEqual(rv._find_winmm_device_id("PowerConf S330", wave_devices=winmm_devs), 0)
        self.assertEqual(rv._find_winmm_device_id("PowerConf", wave_devices=winmm_devs), 0)
        self.assertEqual(rv._find_winmm_device_id("", wave_devices=winmm_devs), rv.WAVE_MAPPER_ID)
        self.assertIsNone(rv._find_winmm_device_id("NonExistentMic", wave_devices=winmm_devs))

    def test_display_prestage_noop_and_hdmi_readiness(self) -> None:
        # 1. set_denon_monitor_out("AUTO") no-ops when radioMonitorOut is already AUTO (zero POSTs)
        posts: list[str] = []

        def fake_get(url: str, _to: float) -> tuple[int, bytes]:
            if "d_video.asp" in url:
                return 200, (
                    '<FORM name="video">'
                    '<input type="radio" name="radioMonitorOut" value="AUTO" checked>'
                    '<input type="radio" name="radioMonitorOut" value="1">'
                    "</FORM>"
                ).encode("ascii")
            if "192.168.1.220:8060/query/device-info" in url:
                return 200, b"<device-info><power-mode>PowerOn</power-mode></device-info>"
            if "192.168.1.220:8060/query/active-app" in url:
                return 200, b'<active-app><app id="tvinput.hdmi1" type="tvin">Receiver</app></active-app>'
            return 200, b"ok"

        def fake_post(url: str, _b: bytes, _h: dict[str, str] | None, _t: float) -> tuple[int, bytes]:
            posts.append(url)
            return 200, b"ok"

        adb_cmds: list[list[str]] = []
        hisense_state = {"power": "ON", "input": "com.mediatek.tis/.HdmiInputService/HW3"}

        def fake_runner(cmd: list[str], _to: float) -> tuple[int, str, str]:
            adb_cmds.append(cmd)
            joined = " ".join(cmd)
            if "devices" in cmd:
                return 0, "List of devices attached\n192.168.1.230:5555\tdevice\n", ""
            if "dumpsys power" in joined:
                return 0, f"mWakefulness=Awake\nDisplay Power: state={hisense_state['power']}\n", ""
            if "dumpsys tv_input" in joined:
                return 0, (
                    "com.mediatek.tis/.HdmiInputService/HW2\n"
                    "com.mediatek.tis/.HdmiInputService/HW3\n"
                    "com.mediatek.tis/.HdmiInputService/HW4\n"
                    f"  inputId: {hisense_state['input']}\n"
                ), ""
            return 0, "ok", ""

        fake_adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=fake_runner, settle_sec=0.0)
        ham = ha.HouseAudioManager(
            root_dir=Path(tempfile.mkdtemp(prefix="armory_disp_ha_")),
            config=ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15"),
            allow_network=False,
            denon_http_get=fake_get,
            sleep_fn=lambda _s: None,
        )
        try:
            ctrl = dc.GymDisplayController(
                config=dc.GymDisplaysConfig(
                    primary=dc.PrimaryDisplayConfig(host="192.168.1.230"),
                    spin=dc.SpinDisplayConfig(host="192.168.1.220", auto_denon_dual=True),
                ),
                adb_controller=fake_adb,
                denon_client=ham.denon,
                http_get=fake_get,
                http_post=fake_post,
                sleep_fn=lambda _s: None,
                allow_network=False,
            )

            mon = ctrl.set_denon_monitor_out("AUTO")
            self.assertTrue(mon["ok"])
            self.assertEqual(mon["via"], "noop_already_set")
            self.assertEqual(posts, [])

            # Hisense ON + HW3 (HDMI 2) -> alreadyReady=True, switched=False, zero keyevent, disconnects cleanly
            r_tv1 = ctrl.ensure_primary_hdmi_ready("hdmi2")
            self.assertTrue(r_tv1["ok"])
            self.assertTrue(r_tv1["alreadyReady"])
            self.assertFalse(r_tv1["switched"])
            self.assertFalse(any("keyevent" in " ".join(c) for c in adb_cmds))
            self.assertEqual(adb_cmds[-1][1:], ["disconnect", "192.168.1.230:5555"])

            # Hisense OFF on HW2 -> wakes & switches to 244 (HDMI 2) with NO Denon flip POST
            adb_cmds.clear()
            hisense_state["power"] = "OFF"
            hisense_state["input"] = "com.mediatek.tis/.HdmiInputService/HW2"
            r_tv2 = ctrl.ensure_primary_hdmi_ready("hdmi2")
            self.assertTrue(r_tv2["ok"])
            self.assertFalse(r_tv2["alreadyReady"])
            self.assertTrue(r_tv2["switched"])
            flat_adb = [" ".join(c) for c in adb_cmds]
            self.assertTrue(any("keyevent 224" in s for s in flat_adb))
            self.assertTrue(any("keyevent 244" in s for s in flat_adb))
            self.assertEqual(posts, [])

            # SmartSpin ON + tvinput.hdmi1 -> alreadyReady=True, zero keypress POSTs
            r_sp = ctrl.ensure_spin_hdmi_ready()
            self.assertTrue(r_sp["ok"])
            self.assertTrue(r_sp["alreadyReady"])
            self.assertFalse(r_sp["switched"])
            self.assertEqual(posts, [])
        finally:
            shutil.rmtree(ham.root_dir, ignore_errors=True)

    def test_multimedia_show_build_stage_routes_and_prestage_orchestration(self) -> None:
        ffmpeg_bin = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        if not ffmpeg_bin:
            self.skipTest("FFmpeg binary not present")

        tmp = Path(tempfile.mkdtemp(prefix="armory_mm_show_"))
        try:
            # Synthesize a crisp 3s 720p H.264 test source clip into shows/sources/
            src_dir = tmp / "shows" / "sources"
            src_dir.mkdir(parents=True, exist_ok=True)
            sample_vid = src_dir / "ryder_walkin_video.mp4"
            proc = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=30:duration=3.0",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=330:sample_rate=48000:duration=3.0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(sample_vid),
                ],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", "ignore"))

            trace: list[tuple[str, str]] = []
            adb_cmds: list[list[str]] = []

            def fake_runner(cmd: list[str], _to: float) -> tuple[int, str, str]:
                adb_cmds.append(cmd)
                joined = " ".join(cmd)
                if "devices" in cmd:
                    return 0, (
                        "List of devices attached\n"
                        "192.168.1.230:5555\tdevice\n"
                        "192.168.1.151:5555\tdevice\n"
                        "192.168.1.204:5555\tdevice\n"
                    ), ""
                if "dumpsys power" in joined:
                    return 0, "Display Power: state=ON\n", ""
                if "dumpsys tv_input" in joined:
                    return 0, (
                        "com.mediatek.tis/.HdmiInputService/HW2\n"
                        "com.mediatek.tis/.HdmiInputService/HW3\n"
                        "  inputId: com.mediatek.tis/.HdmiInputService/HW3\n"
                    ), ""
                return 0, "ok", ""

            fake_adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=fake_runner, settle_sec=0.0)

            def fake_http_get(url: str, _to: float) -> tuple[int, bytes]:
                trace.append(("http_get", url))
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, b"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect><MasterVolume><value>-30.0</value></MasterVolume><Mute><value>off</value></Mute></item>"
                if "d_video.asp" in url:
                    return 200, b'<FORM name="video"><input type="radio" name="radioMonitorOut" value="1" checked></FORM>'
                if "192.168.1.220:8060/query/device-info" in url:
                    return 200, b"<device-info><power-mode>Ready</power-mode></device-info>"
                if "192.168.1.220:8060/query/active-app" in url:
                    return 200, b"<active-app><app id=\"Home\">Home</app></active-app>"
                return 200, b"ok"

            def fake_http_post(url: str, _b: bytes, _h: dict[str, str] | None, _t: float) -> tuple[int, bytes]:
                trace.append(("http_post", url))
                return 200, b"ok"

            svc = ac.ArmoryCamService(root_dir=tmp)
            # Replace service controllers cleanly with offline mocked hardware + real ffmpeg ShowDirector
            ham = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15", cube_enabled=True, cube_host="192.168.1.151"),
                allow_network=False,
                denon_http_get=fake_http_get,
                cube_http_post=fake_http_post,
                sleep_fn=lambda _s: None,
            )
            wb_cfg = ac.WallboardConfig(fire_tv_host="192.168.1.204")
            disp = dc.GymDisplayController(
                config=dc.GymDisplaysConfig(
                    primary=dc.PrimaryDisplayConfig(host="192.168.1.230"),
                    spin=dc.SpinDisplayConfig(host="192.168.1.220", auto_denon_dual=True),
                    cube_host="192.168.1.151",
                ),
                adb_controller=fake_adb,
                denon_client=ham.denon,
                http_get=fake_http_get,
                http_post=fake_http_post,
                sleep_fn=lambda _s: None,
                allow_network=False,
            )
            svc.shows = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                displays=disp,
                adb_controller=fake_adb,
                wallboard_cfg_fn=lambda: wb_cfg,
                stage_base_url_fn=lambda: "http://192.168.1.152:8766",
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                http_get=fake_http_get,
                sleep_fn=lambda _s: None,
            )
            svc.shows.update_show("ryder_walkin", {"overlayAtSec": 1.2}, rebuild=False)
            svc.shows.update_fx_hooks({"lightsHost": "192.168.1.60", "fogHost": "192.168.1.61"})

            mm_res = svc.shows.build_show_multimedia("ryder_walkin", force=True)
            self.assertTrue(mm_res["ok"], msg=str(mm_res))
            self.assertTrue(mm_res["hasSourceVideo"])
            self.assertGreater(mm_res["introDurationSec"], 1.5)
            self.assertGreater(mm_res["totalDurationSec"], mm_res["introDurationSec"] + 2.0)

            mp4_out = svc.shows.show_mp4_path("ryder_walkin")
            self.assertTrue(mp4_out.is_file())
            ffprobe = sd._resolve_ffprobe_bin(ffmpeg_bin)
            pr = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt,sample_rate,channels", "-of", "json", str(mp4_out)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            streams = json.loads(pr.stdout)["streams"]
            v_st = next(s for s in streams if s["codec_type"] == "video")
            a_st = next(s for s in streams if s["codec_type"] == "audio")
            self.assertEqual((v_st["codec_name"], v_st["width"], v_st["height"], v_st["pix_fmt"]), ("h264", 1920, 1080, "yuv420p"))
            self.assertEqual((a_st["codec_name"], int(a_st["sample_rate"]), int(a_st["channels"])), ("aac", 48000, 2))

            app = ac.create_app(svc)
            client = app.test_client()
            st_page = client.get("/stage")
            self.assertEqual(st_page.status_code, 200)
            self.assertIn("stVid", st_page.get_data(as_text=True))

            wb_html = client.get("/wallboard").get_data(as_text=True)
            self.assertIn("armoryShowStage", wb_html)
            self.assertIn("vid.onerror", ac.WALLBOARD_STAGE_OVERLAY_SNIPPET)
            self.assertIn("safetyTimer", ac.WALLBOARD_STAGE_OVERLAY_SNIPPET)

            t_resp = client.get("/api/shows/time")
            self.assertEqual(t_resp.status_code, 200)
            self.assertIn("nowMs", t_resp.get_json())

            ack_resp = client.post("/api/shows/stage/ack", json={"role": "wallboard", "runId": 7, "state": "ready"})
            self.assertEqual(ack_resp.status_code, 200)

            rng_resp = client.get("/api/shows/ryder_walkin/show.mp4", headers={"Range": "bytes=0-511"})
            self.assertEqual(rng_resp.status_code, 206)
            self.assertEqual(len(rng_resp.get_data()), 512)

            # Subscribe to SSE stage queue BEFORE running sequence to assert exact stage event ordering
            q = svc.shows.subscribe_events(role="test_observer")
            res = svc.shows.fire_show("ryder_walkin", async_run=False)
            self.assertTrue(res["ok"], msg=str(res))
            self.assertTrue(res.get("multimedia"))

            evts: list[dict[str, Any]] = []
            while not q.empty():
                evts.append(q.get_nowait())
            svc.shows.unsubscribe_events(q, role="test_observer")
            stage_types = [e.get("type") for e in evts if str(e.get("type", "")).startswith("stage_")]
            self.assertEqual(stage_types, ["stage_prepare", "stage_play", "stage_stop"])

            joined_adb = [" ".join(c) for c in adb_cmds]
            self.assertTrue(any("192.168.1.151:5555" in s and "/stage?role=cube&audio=1" in s for s in joined_adb))
            self.assertTrue(any("192.168.1.204:5555" in s and "media volume --stream 3 --set 0" in s for s in joined_adb))
            self.assertTrue(any("192.168.1.204:5555" in s and "/wallboard" in s for s in joined_adb))
            idx_cube_exit = next(
                (i for i, s in enumerate(joined_adb) if "192.168.1.151:5555" in s and "input keyevent 4" in s),
                -1,
            )
            self.assertGreaterEqual(idx_cube_exit, 0)
            wb_launches = [s for s in joined_adb if "192.168.1.204:5555" in s and "am start" in s and "/wallboard" in s]
            self.assertGreaterEqual(len(wb_launches), 2)
            self.assertFalse(any("com.amazon.firetv.youtube" in s for s in joined_adb))

            # SSE /api/shows/events replays stage_stop on connect/reconnect when idle
            with client.get("/api/shows/events?role=test_sse", buffered=False) as ev_resp:
                it = iter(ev_resp.response)
                c1 = json.loads(next(it).decode("utf-8").removeprefix("data: ").strip())
                c2 = json.loads(next(it).decode("utf-8").removeprefix("data: ").strip())
                self.assertEqual(c1["type"], "hello")
                self.assertEqual(c2["type"], "stage_stop")

            # stop_active_show(restore_wallboard=True) relaunches /wallboard when idle; default does not
            adb_cmds.clear()
            svc.shows.stop_active_show()
            self.assertEqual(adb_cmds, [])
            svc.shows.stop_active_show(restore_wallboard=True)
            joined_idle = [" ".join(c) for c in adb_cmds]
            self.assertTrue(any("192.168.1.204:5555" in s and "am start" in s and "/wallboard" in s for s in joined_idle))

            svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_inline_speech_markup_parser_and_zero_phase_tempo_mastering(self) -> None:
        # 1. Inline pauses, scoped/nested speed & tempo, vol, style extraction, ElevenLabs v3 cues
        raw_script = (
            "[style:Booming championship announcer] "
            "[shouting] Ladies and gentlemen! [pause:100ms] "
            "[speed:+10%] Out of [tempo:1.25] Toms [/tempo] River [/speed] "
            '<break time="0.5s"/> [vol:+3dB] Ryder Curry!! [/vol]'
        )
        toks_clean, style_a = sd.parse_speech_markup(raw_script, keep_eleven_v3_tags=False)
        self.assertEqual(style_a, "Booming championship announcer")
        kinds_a = [(t["kind"], t.get("sec") or t.get("text"), t.get("speed"), t.get("gainDb")) for t in toks_clean]
        self.assertEqual(kinds_a[0], ("speech", "Ladies and gentlemen!", 1.0, 0.0))
        self.assertEqual(kinds_a[1], ("pause", 0.1, None, None))
        self.assertEqual(kinds_a[2], ("speech", "Out of", 1.1, 0.0))
        self.assertEqual(kinds_a[3], ("speech", "Toms", 1.25, 0.0))
        self.assertEqual(kinds_a[4], ("speech", "River", 1.1, 0.0))
        self.assertEqual(kinds_a[5], ("pause", 0.5, None, None))
        self.assertEqual(kinds_a[6], ("speech", "Ryder Curry!!", 1.0, 3.0))

        # ElevenLabs v3 keeps expressive bracket cues intact while non-v3 strips them
        toks_v3, _ = sd.parse_speech_markup(raw_script, keep_eleven_v3_tags=True)
        self.assertIn("[shouting]", toks_v3[0]["text"])

        # Explicit pause next to `...` never double-inserts a pause
        toks_dedup, _ = sd.parse_speech_markup("First ... [pause:120ms] Second")
        self.assertEqual([(t["kind"], t.get("sec")) for t in toks_dedup], [("speech", None), ("pause", 0.12), ("speech", None)])

        # 2. Zero-phase mastering produces clean stereo <= 0.90 peak
        t_arr = np.linspace(0.0, 0.5, 12_000, endpoint=False, dtype=np.float32)
        sine_mono = 0.7 * np.sin(2.0 * np.pi * 160.0 * t_arr)
        mastered = sd.master_announcer_stereo(sine_mono, 24_000, target_sr=sd.SHOW_SR, reverb_wet=0.06, boost_db=3.0)
        self.assertEqual(mastered.shape[1], 2)
        self.assertLessEqual(float(np.max(np.abs(mastered))), 0.91)

        # 3. Pitch-preserving tempo shift via FFmpeg atempo
        ffmpeg_bin = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        if ffmpeg_bin:
            one_sec = 0.4 * np.sin(2.0 * np.pi * 220.0 * np.linspace(0.0, 1.0, 24_000, endpoint=False, dtype=np.float32))
            fast = sd.time_stretch_mono_pcm(one_sec, 24_000, 1.20, ffmpeg_bin)
            slow = sd.time_stretch_mono_pcm(one_sec, 24_000, 0.85, ffmpeg_bin)
            self.assertLess(fast.size, 21_500)
            self.assertGreater(slow.size, 26_500)

    def test_cloud_tts_secrets_masking_custom_voice_and_fast_mezzanine(self) -> None:
        ffmpeg_bin = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        tmp = Path(tempfile.mkdtemp(prefix="armory_tts_cloud_"))
        try:
            posted: list[tuple[str, dict[str, str], dict[str, Any]]] = []
            gemini_calls: list[tuple[str, str, str]] = []

            # Pre-generate tiny real MP3 and WAV payloads using ffmpeg / wave for realistic decode tests
            tone_16k = 0.5 * np.sin(2.0 * np.pi * 180.0 * np.linspace(0.0, 0.4, 9600, endpoint=False, dtype=np.float32))
            tone_wav = sd.encode_stereo_pcm16_wav(tone_16k, sr=24_000)
            tone_pcm16_mono = (np.round(np.clip(tone_16k, -0.95, 0.95) * 32767.0).astype("<i2")).tobytes()
            mp3_bytes = b""
            if ffmpeg_bin:
                p_mp3 = subprocess.run(
                    [ffmpeg_bin, "-y", "-f", "wav", "-i", "pipe:0", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
                    input=tone_wav,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                if p_mp3.returncode == 0 and len(p_mp3.stdout) > 200:
                    mp3_bytes = p_mp3.stdout

            def fake_post(url: str, body: bytes, hdrs: dict[str, str] | None, _to: float) -> tuple[int, bytes]:
                posted.append((url, dict(hdrs or {}), json.loads(body.decode("utf-8"))))
                if "api.elevenlabs.io" in url and mp3_bytes:
                    return 200, mp3_bytes
                if "api.openai.com" in url:
                    return 200, tone_wav
                return 404, b"unsupported"

            def fake_get(url: str, _to: float) -> tuple[int, bytes]:
                if "api.elevenlabs.io/v1/voices" in url:
                    return 200, json.dumps({"voices": [{"voice_id": "customClone99", "name": "Dave Buffer Clone", "category": "cloned"}]}).encode("utf-8")
                return 404, b""

            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.shows = sd.ShowDirector(
                root_dir=tmp,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                http_get=fake_get,
                http_post=fake_post,
                gemini_synth_fn=lambda p, v, s: (gemini_calls.append((p, v, s)), tone_pcm16_mono)[1],
            )
            app = ac.create_app(svc)
            client = app.test_client()

            # 1. Saving API keys stores them outside git in tts_secrets.json and NEVER leaks raw secrets in GET/POST responses
            r_post = client.post(
                "/api/shows/tts-providers",
                json={
                    "elevenlabsKey": "sk_test_secret_el_1234",
                    "geminiKey": "AIzaSecretGem9876",
                    "openaiKey": "sk-proj-secret-oai-4321",
                    "elevenlabsModel": "eleven_v3",
                },
            )
            self.assertEqual(r_post.status_code, 200)
            txt_post = r_post.get_data(as_text=True)
            self.assertNotIn("sk_test_secret_el_1234", txt_post)
            self.assertNotIn("AIzaSecretGem9876", txt_post)
            self.assertNotIn("sk-proj-secret-oai-4321", txt_post)
            j_get = client.get("/api/shows/tts-providers").get_json()
            self.assertTrue(j_get["hasElevenlabsKey"])
            self.assertEqual(j_get["elevenlabsHint"], "••••1234")
            self.assertTrue(j_get["hasGeminiKey"])
            self.assertTrue(j_get["hasOpenaiKey"])
            self.assertTrue(any(v["id"] == "elevenlabs:customClone99" for v in j_get["voices"]))

            # 2. Gemini 2.5 Flash TTS honors inline tags + voiceStyle
            out_gem = tmp / "gem_test.wav"
            sd.synthesize_announcer_wav(
                "[shouting] Intro [pause:100ms] Ryder!",
                voice="gemini:Fenrir",
                out_wav=out_gem,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                voice_style="Championship announcer",
                tts_secrets=svc.shows.load_tts_secrets(),
                gemini_synth_fn=lambda p, v, s: (gemini_calls.append((p, v, s)), tone_pcm16_mono)[1],
            )
            self.assertTrue(out_gem.is_file())
            self.assertGreaterEqual(len(gemini_calls), 2)
            self.assertNotIn("[shouting]", gemini_calls[0][0])
            self.assertEqual(gemini_calls[0][1], "Fenrir")

            # 3. OpenAI gpt-4o-mini-tts sends instructions + wav format
            out_oai = tmp / "oai_test.wav"
            sd.synthesize_announcer_wav(
                "[speed:+15%] Faster hype! [/speed]",
                voice="openai:onyx",
                out_wav=out_oai,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                voice_style="Arena boss",
                tts_secrets=svc.shows.load_tts_secrets(),
                http_post=fake_post,
            )
            self.assertTrue(out_oai.is_file())
            oai_req = next(p for p in posted if "api.openai.com" in p[0])
            self.assertEqual(oai_req[2]["model"], "gpt-4o-mini-tts")
            self.assertEqual(oai_req[2]["instructions"], "Arena boss")

            # 4. ElevenLabs v3 preserves `[shouting]` inline acting tag & includes xi-api-key header
            if mp3_bytes:
                out_el = tmp / "el_test.wav"
                sd.synthesize_announcer_wav(
                    "[shouting] Making his debut!",
                    voice="elevenlabs:JBFqnCBsd6RMkjVDRZzb",
                    out_wav=out_el,
                    ffmpeg_bin=ffmpeg_bin,
                    allow_network=False,
                    tts_secrets=svc.shows.load_tts_secrets(),
                    http_post=fake_post,
                )
                el_req = next(p for p in posted if "api.elevenlabs.io" in p[0])
                self.assertEqual(el_req[1]["xi-api-key"], "sk_test_secret_el_1234")
                self.assertIn("[shouting]", el_req[2]["text"])
                self.assertEqual(el_req[2]["model_id"], "eleven_v3")

            # 5. Custom voice upload override & revert + cached H.264 mezzanine stream-copy rebuild
            up_voice = client.post(
                "/api/shows/ryder_walkin/upload-voice?slot=intro&filename=real_announcer.wav",
                data=tone_wav,
                content_type="audio/wav",
            )
            self.assertEqual(up_voice.status_code, 200)
            self.assertTrue(up_voice.get_json()["show"]["customIntroVoiceFile"].endswith(".wav"))
            clr_voice = client.post("/api/shows/ryder_walkin/config", json={"clearCustomIntroVoice": True})
            self.assertEqual(clr_voice.status_code, 200)
            self.assertEqual(clr_voice.get_json()["show"]["customIntroVoiceFile"], "")

            if ffmpeg_bin:
                sample_vid = tmp / "shows" / "sources" / "ryder_walkin_video.mp4"
                sample_vid.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        ffmpeg_bin, "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2.5",
                        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2.5",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample_vid),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                svc.shows.update_show("ryder_walkin", {"videoSourceFile": "ryder_walkin_video.mp4", "overlayAtSec": 1.0}, rebuild=False)
                b1 = svc.shows.build_show_multimedia("ryder_walkin", force=True)
                self.assertTrue(b1["ok"])
                norm_files = list((tmp / "shows" / "rendered").glob(".norm_*.mp4"))
                self.assertEqual(len(norm_files), 1)
                mtime1 = norm_files[0].stat().st_mtime_ns
                # Modify announcer script -> second rebuild MUST reuse cached .norm_*.mp4 without re-encoding video
                b2_cfg = svc.shows.update_show("ryder_walkin", {"announcerText": "Fast [pause:100ms] Mezzanine!!"}, rebuild=True)
                self.assertTrue(b2_cfg["ok"])
                self.assertEqual(norm_files[0].stat().st_mtime_ns, mtime1)

            svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stateless_voice_audition_without_mp4_rebuild(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_audition_"))
        try:
            tone_16k = 0.5 * np.sin(2.0 * np.pi * 190.0 * np.linspace(0.0, 0.4, 9600, endpoint=False, dtype=np.float32))
            tone_pcm16_mono = (np.round(np.clip(tone_16k, -0.95, 0.95) * 32767.0).astype("<i2")).tobytes()

            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.shows = sd.ShowDirector(
                root_dir=tmp,
                allow_network=False,
                gemini_synth_fn=lambda _p, _v, _s: tone_pcm16_mono,
            )
            svc.shows.update_tts_secrets({"geminiKey": "AIzaDummyKey9999"})

            # Pre-seed an existing show MP4 + capture shows.json snapshot to prove audition never touches either
            show_mp4 = svc.shows.show_mp4_path("ryder_walkin")
            show_mp4.parent.mkdir(parents=True, exist_ok=True)
            show_mp4.write_bytes(b"FAKE_MP4_HEADER_BYTES")
            mp4_mtime_before = show_mp4.stat().st_mtime_ns
            shows_json_before = svc.shows.config_path.read_text(encoding="utf-8")

            app = ac.create_app(svc)
            client = app.test_client()

            # Dashboard UI contains instant audition controls + Top-3 star helper
            dash = client.get("/").get_data(as_text=True)
            for token in ("devRyTestIntroBtn", "devRyTestOvBtn", "devRyTestFxChk", "auditionRyderVoice", "/api/shows/audition-voice", "TOP_VOICES"):
                self.assertIn(token, dash)

            # Dry intro audition
            r_dry = client.post(
                "/api/shows/audition-voice",
                json={
                    "slot": "intro",
                    "text": "Ryder [pause:100ms] Curry!!",
                    "voice": "gemini:Fenrir",
                    "voiceStyle": "HBO ring announcer",
                    "voiceReverbWet": 0.06,
                    "voiceBoostDb": 2.0,
                    "withFx": False,
                },
            )
            self.assertEqual(r_dry.status_code, 200)
            self.assertEqual(r_dry.mimetype, "audio/wav")
            self.assertTrue(r_dry.data.startswith(b"RIFF"))
            dur_dry = float(r_dry.headers.get("X-Armory-Duration-Sec") or "0")
            self.assertGreater(dur_dry, 0.2)

            # Mixed intro (+ thunder & crowd bed)
            r_fx = client.post(
                "/api/shows/audition-voice",
                json={
                    "slot": "intro",
                    "text": "Ryder Curry!!",
                    "voice": "gemini:Fenrir",
                    "withFx": True,
                },
            )
            self.assertEqual(r_fx.status_code, 200)
            self.assertTrue(r_fx.data.startswith(b"RIFF"))
            dur_fx = float(r_fx.headers.get("X-Armory-Duration-Sec") or "0")
            self.assertGreater(dur_fx, dur_dry + 3.0)

            # Overlay slot works even with withFx=True forced false cleanly
            r_ov = client.post(
                "/api/shows/audition-voice",
                json={
                    "slot": "overlay",
                    "text": "Welcome to The Armory.",
                    "voice": "gemini:Fenrir",
                    "withFx": True,
                },
            )
            self.assertEqual(r_ov.status_code, 200)
            self.assertTrue(r_ov.data.startswith(b"RIFF"))

            # Verify shows.json & show MP4 remained completely untouched
            self.assertEqual(show_mp4.stat().st_mtime_ns, mp4_mtime_before)
            self.assertEqual(svc.shows.config_path.read_text(encoding="utf-8"), shows_json_before)

            # Error handling: empty script or missing API key returns 400 JSON
            r_empty = client.post("/api/shows/audition-voice", json={"slot": "intro", "text": "   ", "voice": "kokoro:am_onyx"})
            self.assertEqual(r_empty.status_code, 400)
            self.assertFalse(r_empty.get_json()["ok"])

            r_nokey = client.post("/api/shows/audition-voice", json={"slot": "intro", "text": "Test!", "voice": "elevenlabs:nPczCjzI2devNBz1zQrb"})
            self.assertEqual(r_nokey.status_code, 400)
            self.assertIn("ElevenLabs API key missing", r_nokey.get_json()["error"])

            svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cube_media_snapshot_parser_and_bare_voice_stop_commands(self) -> None:
        # 1. Active Fire TV Cube YouTube session + mCurrentFocus
        sample_playing = (
            "  Sessions Stack - have 3 sessions:\n"
            "    starboard_media com.amazon.firetv.youtube/starboard_media (userId=0)\n"
            "      package=com.amazon.firetv.youtube\n"
            "      active=true\n"
            "      state=PlaybackState {state=3, position=243199, speed=1.0}\n"
            "    A2dpMediaBrowserService com.android.bluetooth/A2dpMediaBrowserService (userId=0)\n"
            "      package=com.android.bluetooth\n"
            "      active=true\n"
            "      state=null\n"
            "---ARMORY_WIN---\n"
            "  mCurrentFocus=Window{4518633 u0 com.amazon.firetv.youtube/dev.cobalt.app.MainActivity}\n"
        )
        snap1 = sd.parse_cube_media_snapshot(sample_playing)
        self.assertTrue(snap1["wasPlaying"])
        self.assertEqual(snap1["mediaPackage"], "com.amazon.firetv.youtube")
        self.assertEqual(snap1["mediaState"], 3)
        self.assertEqual(snap1["focusedComponent"], "com.amazon.firetv.youtube/dev.cobalt.app.MainActivity")
        self.assertEqual(snap1["focusedPackage"], "com.amazon.firetv.youtube")

        # 2. Paused session (state=2) + ignored Armory Wallboard focus
        sample_paused = (
            "    MediaBrowserService com.amazon.bueller.music/MediaBrowserService (userId=0)\n"
            "      package=com.amazon.bueller.music\n"
            "      active=false\n"
            "      state=PlaybackState {state=2, position=205313}\n"
            "---ARMORY_WIN---\n"
            "  mCurrentFocus=Window{999 u0 com.froriep.armorywallboard/.WallboardActivity}\n"
        )
        snap2 = sd.parse_cube_media_snapshot(sample_paused)
        self.assertFalse(snap2["wasPlaying"])
        self.assertEqual(snap2["mediaPackage"], "com.amazon.bueller.music")
        self.assertEqual(snap2["mediaState"], 2)
        self.assertEqual(snap2["focusedComponent"], "")

        # 3. Bare voice stop commands ("stop", "enough", "end", "cancel") + workout undo preservation
        tmp = Path(tempfile.mkdtemp(prefix="armory_vstop_"))
        try:
            sd_dir = sd.ShowDirector(root_dir=tmp, allow_network=False)
            for phrase in ("Armory, stop", "Armory, enough", "Armory, end", "Armory cancel", "Armory stop sim"):
                res = sd_dir.match_voice_command(phrase, workout_active=False)
                self.assertEqual(res, {"action": "stop"}, msg=phrase)
            # During an active Strength workout with NO running show, bare "Armory, cancel" yields to workout undo!
            self.assertIsNone(sd_dir.match_voice_command("Armory, cancel", workout_active=True))
            # While a show IS active, bare "Armory, cancel" stops the show even during a workout!
            sd_dir._active_state["active"] = True
            self.assertEqual(sd_dir.match_voice_command("Armory, cancel", workout_active=True), {"action": "stop"})
            # But explicit "Armory, stop" ALWAYS stops stage/show
            sd_dir._active_state["active"] = False
            self.assertEqual(sd_dir.match_voice_command("Armory, stop", workout_active=True), {"action": "stop"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_timed_speech_fx_cues_exclude_main_tv_and_cube_auto_resume(self) -> None:
        ffmpeg_bin = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        if not ffmpeg_bin:
            self.skipTest("FFmpeg binary not present")
        tmp = Path(tempfile.mkdtemp(prefix="armory_fx_resume_"))
        try:
            src_vid = tmp / "shows" / "sources" / "ryder_walkin_video.mp4"
            src_vid.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    ffmpeg_bin, "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4.5",
                    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=4.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src_vid),
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )

            adb_cmds: list[list[str]] = []
            http_calls: list[str] = []
            cube_state = {"playing": True, "stage_open": False}

            def fake_runner(cmd: list[str], _to: float) -> tuple[int, str, str]:
                adb_cmds.append(cmd)
                joined = " ".join(cmd)
                if "devices" in cmd:
                    return 0, "List of devices attached\n192.168.1.230:5555\tdevice\n192.168.1.151:5555\tdevice\n192.168.1.204:5555\tdevice\n", ""
                if "dumpsys media_session" in joined and "ARMORY_WIN" in joined:
                    st_num = 3 if cube_state["playing"] else 2
                    act_str = "true" if cube_state["playing"] else "false"
                    return 0, (
                        f"    starboard_media com.amazon.firetv.youtube/starboard_media (userId=0)\n"
                        f"      package=com.amazon.firetv.youtube\n      active={act_str}\n"
                        f"      state=PlaybackState {{state={st_num}, position=5000}}\n"
                        f"---ARMORY_WIN---\n"
                        f"  mCurrentFocus=Window{{111 u0 com.amazon.firetv.youtube/dev.cobalt.app.MainActivity}}\n"
                    ), ""
                if "am start" in joined and "armorywallboard" in joined:
                    cube_state["stage_open"] = True
                    return 0, "ok", ""
                if "am force-stop com.froriep.armorywallboard" in joined:
                    cube_state["stage_open"] = False
                    return 0, "ok", ""
                if "dumpsys window windows" in joined:
                    pkg = "com.froriep.armorywallboard/.WallboardActivity" if cube_state["stage_open"] else "com.amazon.firetv.youtube/dev.cobalt.app.MainActivity"
                    return 0, f"mCurrentFocus=Window{{222 u0 {pkg}}}", ""
                if "dumpsys media_session" in joined:
                    # Simulate paused right after force-stop, then playing after keyevent 126!
                    st_num = 2 if cube_state["playing"] else 0
                    return 0, (
                        f"    starboard_media com.amazon.firetv.youtube/starboard_media (userId=0)\n"
                        f"      package=com.amazon.firetv.youtube\n      active=false\n"
                        f"      state=PlaybackState {{state={st_num}, position=5000}}\n"
                    ), ""
                if "input keyevent 126" in joined:
                    cube_state["playing"] = True
                return 0, "ok", ""

            def fake_http_get(url: str, _to: float) -> tuple[int, bytes]:
                http_calls.append(url)
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, b"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect><MasterVolume><value>-30.0</value></MasterVolume></item>"
                if "d_video.asp" in url:
                    return 200, b'<FORM name="video"><input type="radio" name="radioMonitorOut" value="1" checked></FORM>'
                if "192.168.1.220:8060/query/device-info" in url:
                    return 200, b"<device-info><power-mode>Ready</power-mode></device-info>"
                return 200, b"ok"

            fake_adb = ac.FireTvAdbController(adb_bin="adb.exe", runner=fake_runner, settle_sec=0.0)
            ham = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15", cube_enabled=True, cube_host="192.168.1.151"),
                allow_network=False,
                denon_http_get=fake_http_get,
                cube_http_post=lambda _u, _b, _h, _t: (200, b"ok"),
                sleep_fn=lambda _s: None,
            )
            wb_cfg = ac.WallboardConfig(fire_tv_host="192.168.1.204")
            disp = dc.GymDisplayController(
                config=dc.GymDisplaysConfig(
                    primary=dc.PrimaryDisplayConfig(host="192.168.1.230"),
                    spin=dc.SpinDisplayConfig(host="192.168.1.220", auto_denon_dual=True),
                    cube_host="192.168.1.151",
                ),
                adb_controller=fake_adb,
                denon_client=ham.denon,
                http_get=fake_http_get,
                http_post=lambda _u, _b, _h, _t: (200, b"ok"),
                sleep_fn=lambda _s: None,
                allow_network=False,
            )
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.shows = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                displays=disp,
                adb_controller=fake_adb,
                wallboard_cfg_fn=lambda: wb_cfg,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                http_get=fake_http_get,
                sleep_fn=lambda _s: None,
            )
            svc.shows.update_fx_hooks({"lightsHost": "192.168.1.60", "fogHost": "192.168.1.61"})

            # 1. Multi-speech cues (1 intro + 2 video) + custom fxCues bake cleanly
            svc.shows.update_show(
                "ryder_walkin",
                {
                    "videoSourceFile": "ryder_walkin_video.mp4",
                    "introStartSec": 1.0,
                    "excludeMainTv": True,
                    "speechCues": [
                        {"id": "ic1", "target": "intro", "atSec": 3.5, "text": "Here comes Ryder!"},
                        {"id": "vc1", "target": "video", "atSec": 0.5, "text": "Part one!", "postDenonVol": 60},
                        {"id": "vc2", "target": "video", "atSec": 2.2, "text": "Part two!", "postDenonVol": 52},
                    ],
                    "fxCues": [
                        {"id": "fx1", "target": "video", "atSec": 1.0, "type": "flash", "durationSec": 0.25, "count": 2, "screenFlash": True},
                        {"id": "fx2", "target": "video", "atSec": 1.5, "type": "fog", "durationSec": 5.0, "count": 1, "screenFlash": False},
                    ],
                },
                rebuild=True,
            )
            mm_meta = svc.shows._read_show_meta("ryder_walkin")
            self.assertEqual(len(mm_meta.get("speechCueTimings") or []), 2)

            # 2. Live config update with rebuild=False preserves MP4 mtime!
            app = ac.create_app(svc)
            client = app.test_client()
            mp4_mtime = svc.shows.show_mp4_path("ryder_walkin").stat().st_mtime_ns
            r_quick = client.post(
                "/api/shows/ryder_walkin/config",
                json={"excludeMainTv": True, "fxCues": [{"id": "fx1", "target": "video", "atSec": 1.1, "type": "flash", "durationSec": 0.25, "count": 2, "screenFlash": True}, {"id": "fx2", "target": "video", "atSec": 1.5, "type": "fog", "durationSec": 5.0, "count": 1, "screenFlash": False}], "rebuild": False},
            )
            self.assertEqual(r_quick.status_code, 200)
            self.assertEqual(svc.shows.show_mp4_path("ryder_walkin").stat().st_mtime_ns, mp4_mtime)

            # 3. Fire show with exclude_main_tv=True and prev_media playing=True -> verify skip Hisense + flashEvents + auto-resume!
            q = svc.shows.subscribe_events(role="test_obs")
            res1 = svc.shows.fire_show("ryder_walkin", async_run=False, exclude_main_tv=True)
            self.assertTrue(res1["ok"])
            self.assertTrue(res1["prestage"]["tv"].get("excluded"))
            evts = []
            while not q.empty():
                evts.append(q.get_nowait())
            svc.shows.unsubscribe_events(q, role="test_obs")

            play_evt = next(e for e in evts if e.get("type") == "stage_play")
            self.assertEqual(len(play_evt.get("flashEvents") or []), 1)
            self.assertTrue(any("192.168.1.61/rpc/Switch.Set?id=0&on=true&toggle_after=5" in u for u in http_calls))

            joined_adb = [" ".join(c) for c in adb_cmds]
            # Primary Hisense (192.168.1.230) must NOT receive any ADB commands
            self.assertFalse(any("192.168.1.230" in s for s in joined_adb))
            # Cube must force-stop overlay + refocus YouTube + send keyevent 126
            self.assertTrue(any("192.168.1.151:5555" in s and "am force-stop com.froriep.armorywallboard" in s for s in joined_adb))
            self.assertTrue(any("192.168.1.151:5555" in s and "input keyevent 126" in s for s in joined_adb))

            # 4. When Cube was previously PAUSED (wasPlaying=False), MUST NOT send keyevent 126!
            adb_cmds.clear()
            cube_state["playing"] = False
            res2 = svc.shows.fire_show("ryder_walkin", async_run=False, exclude_main_tv=True)
            self.assertTrue(res2["ok"])
            joined_adb2 = [" ".join(c) for c in adb_cmds]
            self.assertFalse(any("192.168.1.151:5555" in s and "input keyevent 126" in s for s in joined_adb2))

            # 5. Dashboard HTML includes new Show Lab UI controls + Stage flash overlay
            dash = client.get("/").get_data(as_text=True)
            for tok in ("devRyExcludeMainTv", "devRyAddSpeechBtn", "devRyAddFxBtn", "devScrubClock", "devVidFlash", "devRySaveQuickBtn", "devRyIntroStart"):
                self.assertIn(tok, dash)
            self.assertIn("armoryStageFlash", client.get("/stage").get_data(as_text=True))

            svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dual_zone_house_lights_rack_smith_and_pr_drop_flashes(self) -> None:
        # 1. URL resolution for rack, smith, all, and legacy fallback
        fx_dual = sd.SmartFxConfig(lights_rack_host="192.168.1.71", lights_smith_host="192.168.1.72")
        self.assertEqual(fx_dual.resolve_lights_urls(True, "rack"), ["http://192.168.1.71/rpc/Switch.Set?id=0&on=true"])
        self.assertEqual(fx_dual.resolve_lights_urls(False, "smith"), ["http://192.168.1.72/rpc/Switch.Set?id=0&on=false"])
        self.assertEqual(
            fx_dual.resolve_lights_urls(True, "all"),
            ["http://192.168.1.71/rpc/Switch.Set?id=0&on=true", "http://192.168.1.72/rpc/Switch.Set?id=0&on=true"],
        )
        fx_leg = sd.SmartFxConfig(lights_host="192.168.1.70")
        self.assertEqual(fx_leg.resolve_lights_urls(True, "all"), ["http://192.168.1.70/rpc/Switch.Set?id=0&on=true"])
        self.assertEqual(fx_leg.resolve_lights_urls(True, "rack"), ["http://192.168.1.70/rpc/Switch.Set?id=0&on=true"])

        tmp = Path(tempfile.mkdtemp(prefix="armory_dual_lights_"))
        try:
            hits: list[str] = []

            def fake_get(url: str, _to: float) -> tuple[int, bytes]:
                hits.append(url)
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, b"<item><Power><value>ON</value></Power><MasterVolume><value>-30.0</value></MasterVolume></item>"
                return 200, b"ok"

            ham = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15", cube_enabled=True, cube_host="192.168.1.151"),
                allow_network=False,
                denon_http_get=fake_get,
                cube_http_post=lambda _u, _b, _h, _t: (200, b"ok"),
                sleep_fn=lambda _s: None,
            )
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.shows = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                allow_network=False,
                http_get=fake_get,
                sleep_fn=lambda _s: None,
            )
            app = ac.create_app(svc)
            client = app.test_client()

            # 2. Save dual-zone hosts & trigger manual Rack/Smith/Alt Flash endpoints
            r_rack_on = client.post(
                "/api/shows/fx-hooks",
                json={"lightsRackHost": "192.168.1.71", "lightsSmithHost": "192.168.1.72", "test": "rack_on"},
            )
            self.assertEqual(r_rack_on.status_code, 200)
            self.assertIn("http://192.168.1.71/rpc/Switch.Set?id=0&on=true", hits)

            hits.clear()
            client.post("/api/shows/fx-hooks", json={"lightsRackHost": "192.168.1.71", "lightsSmithHost": "192.168.1.72", "test": "smith_off"})
            self.assertEqual(hits, ["http://192.168.1.72/rpc/Switch.Set?id=0&on=false"])

            hits.clear()
            svc.shows._flash_lights(2, zone="alternate", end_on=True, on_sec=0.1, off_sec=0.1)
            # Step 1: Rack ON + Smith OFF, both OFF; Step 2: Smith ON + Rack OFF, end_on -> both ON
            self.assertIn("http://192.168.1.71/rpc/Switch.Set?id=0&on=true", hits)
            self.assertIn("http://192.168.1.72/rpc/Switch.Set?id=0&on=false", hits)
            self.assertEqual(
                set(hits[-2:]),
                {"http://192.168.1.71/rpc/Switch.Set?id=0&on=true", "http://192.168.1.72/rpc/Switch.Set?id=0&on=true"},
            )

            # 3. Voice commands for Rack / Smith / House lights
            hits.clear()
            self.assertEqual(svc.shows.match_voice_command("Armory, turn off rack lights"), {"action": "lights", "zone": "rack", "on": False})
            self.assertEqual(svc.shows.match_voice_command("Armory, smith lights on"), {"action": "lights", "zone": "smith", "on": True})
            self.assertEqual(svc.shows.match_voice_command("Armory, lights off"), {"action": "lights", "zone": "all", "on": False})
            hv = svc._handle_local_voice_command("Armory, turn off rack lights")
            self.assertIsNotNone(hv)
            self.assertTrue(hv["ok"])
            self.assertEqual(hv["speech"], "Rack lights off")
            self.assertIn("http://192.168.1.71/rpc/Switch.Set?id=0&on=false", hits)

            # 4. PR drop-in with lightsFlashCount=2 triggers dual-zone flashes cleanly
            hits.clear()
            svc.shows.update_show("matt_pr", {"lightsFlashCount": 2, "songDurationSec": 1.0}, rebuild=True)
            res_pr = svc.shows.fire_show("matt_pr", async_run=False)
            self.assertTrue(res_pr["ok"])
            self.assertTrue(any("192.168.1.71/rpc/Switch.Set" in u for u in hits))
            self.assertTrue(any("192.168.1.72/rpc/Switch.Set" in u for u in hits))

            # 5. Dashboard HTML exposes Rack & Smith controls
            dash = client.get("/").get_data(as_text=True)
            for tok in ("fxLightsRackHost", "fxLightsSmithHost", "fxRackOnBtn", "fxRackOffBtn", "fxSmithOnBtn", "fxSmithOffBtn", "fxTestAltFlashBtn"):
                self.assertIn(tok, dash)

            svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ring_disk_fallback_cuts_full_clip_despite_ingest_backlog(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_ring_fallback_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            client.post(
                "/api/config",
                json={
                    "cameras": [
                        {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "crop": [0.0, 0.0, 1.0, 1.0], "defish": False},
                    ]
                },
            )
            client.post("/api/session/start", json={"sessionId": "sess-lag", "name": "Deadlift Day"})

            ff = shutil.which("ffmpeg") or svc.ffmpeg
            base_dt = datetime(2026, 11, 21, 14, 34, 18)
            base_ms = int(base_dt.timestamp() * 1000)
            ring_dir = svc.ring_root / "rack_cam"
            ring_dir.mkdir(parents=True, exist_ok=True)

            # Write three 3-second segments ONLY to ring_root on disk without running _ingest_closed_segments
            for i in range(4):
                seg_dt = datetime.fromtimestamp((base_ms + i * 3000) / 1000.0)
                seg_path = ring_dir / f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                subprocess.run(
                    [
                        ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=20:d=3",
                        "-c:v", "libx264", "-g", "15", "-bf", "0", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                    ],
                    check=True,
                )

            found = svc.find_segments("Rack Cam", "sess-lag", base_ms + 2000, base_ms + 11000)
            self.assertEqual(len(found), 4)
            self.assertEqual(found[0].end_ms - found[0].start_ms, 3000)

            clip_res = client.post(
                "/api/clip",
                json={
                    "sessionId": "sess-lag",
                    "exName": "Deadlift",
                    "startMs": base_ms + 2000,
                    "endMs": base_ms + 11000,
                    "camera": "Rack Cam",
                },
            )
            self.assertEqual(clip_res.status_code, 200)
            clip_data = clip_res.get_json()
            self.assertTrue(clip_data["ok"])
            self.assertGreaterEqual(clip_data["durationSec"], 8)
            out_file = svc.clips_root / clip_data["lanUrl"].removeprefix("/clips/")
            dur_txt = subprocess.check_output(
                [svc.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out_file)],
                text=True,
            ).strip()
            self.assertGreaterEqual(float(dur_txt), 8.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_room_voice_speak_dedup_and_worker_coalescing(self) -> None:
        spoken_direct: list[str] = []
        v1 = rv.RoomVoiceService(
            config=rv.RoomVoiceConfig(enabled=True),
            speak_fn=lambda txt: spoken_direct.append(txt) or True,
            allow_hardware_audio=False,
        )
        self.assertTrue(v1.speak_confirmation("135 for 10, check"))
        self.assertTrue(v1.speak_confirmation("135 for 10, check"))
        self.assertTrue(v1.speak_confirmation("135 for 10, got it"))
        self.assertEqual(spoken_direct, ["135 for 10, check", "135 for 10, got it"])

        played: list[str] = []
        gate = threading.Event()
        done_all = threading.Event()

        class FakeTts:
            def synthesize(self, text: str, voice: str | None = None):
                return np.ones(1600, dtype=np.float32), 16_000

        v2 = rv.RoomVoiceService(
            config=rv.RoomVoiceConfig(enabled=True, tts_voice="en-US-AndrewMultilingualNeural"),
            allow_hardware_audio=False,
        )
        v2._tts = FakeTts()

        def fake_play(pcm, sr, **kwargs):
            played.append("played")
            if len(played) == 1:
                gate.wait(timeout=2.0)
            else:
                done_all.set()
            return True

        synthesized_texts: list[str] = []
        orig_synth = v2._tts.synthesize

        def tracked_synth(text: str, voice: str | None = None):
            synthesized_texts.append(text)
            return orig_synth(text, voice=voice)

        v2._tts.synthesize = tracked_synth
        v2._play_pcm_blocking = fake_play
        v2._started = True
        worker = threading.Thread(target=v2._speaker_worker_loop, daemon=True)
        worker.start()
        try:
            v2._speaker_queue.put_nowait(("tts", ("first", "v")))
            time.sleep(0.05)
            v2._speaker_queue.put_nowait(("tts", ("stale-2", "v")))
            v2._speaker_queue.put_nowait(("tts", ("stale-3", "v")))
            v2._speaker_queue.put_nowait(("tts", ("latest-4", "v")))
            gate.set()
            self.assertTrue(done_all.wait(timeout=2.0))
            self.assertEqual(synthesized_texts, ["first", "latest-4"])
        finally:
            v2._stop_event.set()
            v2._speaker_queue.put_nowait(("stop", None))
            worker.join(timeout=1.5)

    def test_room_voice_cuda_warmup_fallback_and_end_silence_default(self) -> None:
        cfg = rv.RoomVoiceConfig()
        self.assertEqual(cfg.end_silence_ms, 580)
        self.assertEqual(cfg.model_size, "small.en")
        self.assertAlmostEqual(cfg.vad_threshold, 0.48, places=2)
        svc = rv.RoomVoiceService(config=cfg, allow_hardware_audio=False)
        self.assertIn("asrDevice", svc.status_dict())

        # Prompt must lead with capitalized wake-word command pairs so Whisper never drops "Armory"
        prompt, hotwords = rv.build_whisper_prompt("armory", ["Lat Pulldown"])
        self.assertTrue(prompt.startswith("Armory, record this. Armory, done."))
        self.assertIn("Armory", hotwords)
        self.assertIn("record this", hotwords)

        # Idle Silero VAD LSTM reset prevents continuous music drift without interrupting speech
        class _IdleVad:
            def __init__(self) -> None:
                self.resets = 0

            def reset(self) -> None:
                self.resets += 1

            def score_frame(self, _frame: np.ndarray, rms_gate: float = 0.0025) -> float:
                return 0.05

        idle_vad = _IdleVad()
        det = rv.UtteranceDetector(vad=idle_vad, vad_threshold=0.48, rms_gate=0.0025, idle_reset_frames=10)
        music_chunk = np.full(rv.VAD_FRAME_SAMPLES * 25, 0.02, dtype=np.float32)
        self.assertEqual(det.push_samples(music_chunk), [])
        self.assertEqual(idle_vad.resets, 2)
        self.assertFalse(det.is_user_speaking())

        # Ready chime must NOT mute capture and must skip entirely if user is already speaking
        v_chime = rv.RoomVoiceService(config=cfg, allow_hardware_audio=True)
        v_chime._started = True
        v_chime._mute_until_mono = 0.0
        v_chime._detector._in_speech = True
        self.assertFalse(v_chime.play_ready_chime())
        self.assertEqual(v_chime._mute_until_mono, 0.0)
        v_chime._detector._in_speech = False
        self.assertTrue(v_chime.play_ready_chime())
        self.assertEqual(v_chime._mute_until_mono, 0.0)
        q_kind, _q_data = v_chime._speaker_queue.get_nowait()
        self.assertEqual(q_kind, "ready_chime")

        calls: list[tuple[str, str]] = []

        class FakeWhisperModel:
            def __init__(self, size: str, device: str = "cpu", compute_type: str = "int8", **kwargs):
                self.device = device
                self.compute_type = compute_type
                calls.append((device, compute_type))

            def transcribe(self, audio, **kwargs):
                if self.device == "cuda":
                    raise RuntimeError("cublas64_12.dll missing")
                return iter([]), None

        with (
            mock.patch("faster_whisper.WhisperModel", FakeWhisperModel),
            mock.patch("ctranslate2.get_cuda_device_count", return_value=1),
        ):
            model, asr_dev = rv._load_whisper_model("base.en")

        self.assertEqual(asr_dev, "cpu/int8")
        self.assertEqual(calls, [("cuda", "int8_float16"), ("cuda", "float16"), ("cpu", "int8")])

    def test_gemini_tts_coalesce_disk_cache_429_and_14day_scrubber_console(self) -> None:
        # 1. Gemini implicit '...' pause coalescing + disk cache reuse + 429 friendly message
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        tmp = Path(tempfile.mkdtemp(prefix="armory_console_scrub_"))
        try:
            cache_dir = tmp / "tts_cache"
            gem_calls: list[str] = []
            tone_pcm16 = (
                np.sin(2.0 * np.pi * 220.0 * np.linspace(0, 0.35, 8400, endpoint=False)) * 12000.0
            ).astype("<i2").tobytes()

            def fake_gem(prompt_txt: str, _v: str, _style: str) -> bytes:
                gem_calls.append(prompt_txt)
                return tone_pcm16

            out1 = tmp / "gem1.wav"
            sd.synthesize_announcer_wav(
                "Ladies and gentlemen... Straight out of Brick... Ryder Curry!!",
                voice="gemini:Fenrir",
                out_wav=out1,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                tts_cache_dir=cache_dir,
                voice_style="HBO ring announcer",
                tts_secrets={"geminiKey": "AIzaDummyTestKey"},
                gemini_synth_fn=fake_gem,
            )
            self.assertEqual(len(gem_calls), 1)
            self.assertIn("Ladies and gentlemen ... Straight out of Brick ... Ryder Curry!!", gem_calls[0])
            self.assertTrue(out1.is_file())

            # Re-rendering with different mastering settings hits disk cache -> 0 new Gemini requests
            out2 = tmp / "gem2.wav"
            sd.synthesize_announcer_wav(
                "Ladies and gentlemen... Straight out of Brick... Ryder Curry!!",
                voice="gemini:Fenrir",
                out_wav=out2,
                ffmpeg_bin=ffmpeg_bin,
                allow_network=False,
                tts_cache_dir=cache_dir,
                reverb_wet=0.14,
                boost_db=4.0,
                voice_style="HBO ring announcer",
                tts_secrets={"geminiKey": "AIzaDummyTestKey"},
                gemini_synth_fn=fake_gem,
            )
            self.assertEqual(len(gem_calls), 1)
            self.assertTrue(out2.is_file())

            err_msg = sd._format_tts_error(
                "gemini",
                RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded 'retryDelay': '21749s'"),
            )
            self.assertIn("10 requests/day", err_msg)
            self.assertIn("~6h 2m", err_msg)

            # 2. 14-day tape discovery (/api/tapes) & zero historical ring bleed
            svc = ac.ArmoryCamService(root_dir=tmp)
            try:
                svc.post_config = None
                cfg_cams = [
                    {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.2, "crop": [0, 0, 1, 1], "defish": False},
                    {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.2, "crop": [0, 0, 1, 1], "defish": False},
                ]
                app = ac.create_app(svc)
                client = app.test_client()
                client.post("/api/config", json={"cameras": cfg_cams})

                # Old session on 2026-11-18
                nov18 = svc.tapes_root / "sess_nov18" / "rack_cam"
                nov18.mkdir(parents=True, exist_ok=True)
                for nm in ("seg_20261118_195610.ts", "seg_20261118_195612.ts", "seg_20261118_195620.ts"):
                    (nov18 / nm).write_bytes(b"ts" * 100)
                t0 = ac.parse_segment_start_ms("seg_20261118_195610.ts")
                assert t0 is not None
                motion_rows = [
                    {"camera": "Rack Cam", "slug": "rack_cam", "file": f"seg_20261118_1956{10 + i * 2}.ts", "startMs": t0 + i * 2000, "endMs": t0 + (i + 1) * 2000, "energy": 4.2}
                    for i in range(6)
                ]
                (svc.tapes_root / "sess_nov18" / "motion.jsonl").write_text(
                    "\n".join(json.dumps(r) for r in motion_rows) + "\n", encoding="utf-8"
                )

                # Session on 2026-11-21
                nov21 = svc.tapes_root / "sess_nov21" / "smith_cam"
                nov21.mkdir(parents=True, exist_ok=True)
                (nov21 / "seg_20261121_141410.ts").write_bytes(b"ts" * 100)

                # Today's live ring segment on disk (must NOT bleed into sess_nov18 timeline!)
                live_ring = svc.ring_root / "rack_cam"
                live_ring.mkdir(parents=True, exist_ok=True)
                (live_ring / "seg_20261122_090000.ts").write_bytes(b"ts" * 100)
                t_ring_max = ac.parse_segment_start_ms("seg_20261122_090000.ts")
                assert t_ring_max is not None

                tapes_res = client.get("/api/tapes")
                self.assertEqual(tapes_res.status_code, 200)
                tapes_json = tapes_res.get_json()
                self.assertTrue(tapes_json["ok"])
                self.assertIsNotNone(tapes_json["live"])
                self.assertEqual([d["date"] for d in tapes_json["days"]], ["2026-11-21", "2026-11-18"])

                tl_res = client.get("/api/tape/timeline?sessionId=sess_nov18&camera=Rack%20Cam&buckets=60")
                self.assertEqual(tl_res.status_code, 200)
                tl_json = tl_res.get_json()
                self.assertTrue(tl_json["ok"])
                self.assertLess(tl_json["maxMs"], t_ring_max)
                self.assertIn("threshold", tl_json)
                self.assertGreaterEqual(len(tl_json["bursts"]), 1)
                self.assertIn("v", tl_json["activity"][0])

                # 3. 6-page console overhaul + scrubber UI markers & zero legacy alignment tokens
                dash = client.get("/").get_data(as_text=True)
                for tok in (
                    'data-nav="overview"',
                    'data-nav="cameras"',
                    'data-nav="footage"',
                    'data-nav="shows"',
                    'data-nav="gym"',
                    'data-nav="audio"',
                    'data-page="overview"',
                    'data-page="cameras"',
                    'data-page="footage"',
                    'data-page="shows"',
                    'data-page="gym"',
                    'data-page="audio"',
                    "tsTimelineCanvas",
                    "tsPlayBtn",
                    "tsSaveClipBtn",
                    "tsDayStrip",
                    "tsBurstList",
                ):
                    self.assertIn(tok, dash, msg=f"Missing token: {tok}")
                for dead_tok in ("alignBar", "pinRefBtn", "realignModeBtn", "driftNtfyBtn", "Secondary / Pinned"):
                    self.assertNotIn(dead_tok, dash)
            finally:
                svc.stop_background()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_personalized_walkins_smart_fogger_theater_hooks_and_no_beantown(self) -> None:
        # 1. SmartFxConfig theater URL resolution & fog fallback to theaterHost
        fx_t = sd.SmartFxConfig(theater_host="192.168.1.95")
        self.assertEqual(
            fx_t.resolve_theater_scene_url("jenna", duration_ms=48000, fog_ms=3500),
            "http://192.168.1.95/api/scene?mode=jenna&ms=48000&fog_ms=3500",
        )
        self.assertEqual(fx_t.resolve_theater_stop_url(), "http://192.168.1.95/api/stop")
        self.assertEqual(fx_t.resolve_fog_url(3.5), "http://192.168.1.95/api/trigger?sec=3.5")
        self.assertEqual(fx_t.resolve_theater_scene_url("invalid_mode"), "")

        tmp = Path(tempfile.mkdtemp(prefix="armory_vip_walkins_"))
        try:
            # Seed legacy shows.json with deprecated Beantown text on jared_mark_walkin to verify migration
            legacy_cfg = {
                "fxHooks": {"theaterHost": "192.168.1.95", "lightsHost": "192.168.1.60"},
                "shows": {
                    "jared_mark_walkin": {
                        "id": "jared_mark_walkin",
                        "name": "Jared & Mark Walk-In (All the Way From Beantown)",
                        "triggers": ["jared and mark are here", "beantown walk in"],
                        "announcerText": "All the way from Beantown... Jared and Mark!!",
                    }
                },
            }
            (tmp / "shows.json").write_text(json.dumps(legacy_cfg), encoding="utf-8")

            http_hits: list[str] = []

            def fake_get(url: str, _to: float) -> tuple[int, bytes]:
                http_hits.append(url)
                if "formMainZone_MainZoneXmlStatus.xml" in url:
                    return 200, b"<item><Power><value>ON</value></Power><MasterVolume><value>-30.0</value></MasterVolume></item>"
                return 200, b'{"ok":true}'

            ham = ha.HouseAudioManager(
                root_dir=tmp,
                config=ha.HouseAudioConfig(denon_enabled=True, denon_host="192.168.1.15", cube_enabled=True, cube_host="192.168.1.151"),
                allow_network=False,
                denon_http_get=fake_get,
                cube_http_post=lambda _u, _b, _h, _t: (200, b'{"ok":true}'),
                sleep_fn=lambda _s: None,
            )
            director = sd.ShowDirector(
                root_dir=tmp,
                house_audio=ham,
                allow_network=False,
                http_get=fake_get,
                sleep_fn=lambda _s: None,
            )

            # 2. Voice trigger matching for Jenna, Avenleigh (incl. ASR spellings), Jared & Mark Prism + VIP
            self.assertEqual(director.match_voice_command("Armory, Jenna just got here")["showId"], "jenna_walkin")
            self.assertEqual(director.match_voice_command("Armory, Avenleigh is here")["showId"], "avenleigh_walkin")
            self.assertEqual(director.match_voice_command("Armory, Avenly walk in")["showId"], "avenleigh_walkin")
            self.assertEqual(director.match_voice_command("Armory, Avenlee is here")["showId"], "avenleigh_walkin")
            self.assertEqual(director.match_voice_command("Armory, Jared and Mark are here")["showId"], "jared_mark_walkin")
            self.assertEqual(director.match_voice_command("Armory, Jared and Mark VIP")["showId"], "jared_mark_vip")

            # Assert zero Beantown/Boston references remain after migration
            for sid in ("jared_mark_walkin", "jared_mark_vip"):
                sp = director.get_show(sid)
                assert sp is not None
                blob = json.dumps(sp.to_dict()).lower()
                self.assertNotIn("beantown", blob)
                self.assertNotIn("boston", blob)

            # 3. Preview builds cleanly on non-thunder debut shows (add_thunder=False)
            p_jenna = director.build_show_preview("jenna_walkin", force=True)
            p_aven = director.build_show_preview("avenleigh_walkin", force=True)
            self.assertTrue(p_jenna["ok"])
            self.assertTrue(p_aven["ok"])

            # 4. Firing show with theaterHost fires /api/scene once (no double /api/trigger fog), restores lights & stops FX in finally
            http_hits.clear()
            # Clear youtube_url on a copy or fire jared_mark_vip without displays to test _run_show_sequence theater hooks
            director.update_show("jenna_walkin", {"youtubeUrl": "", "threeScreenVideo": False}, rebuild=False)
            res = director.fire_show("jenna_walkin", async_run=False)
            self.assertTrue(res["ok"])
            scene_calls = [u for u in http_hits if "/api/scene?" in u]
            trigger_calls = [u for u in http_hits if "/api/trigger?" in u]
            stop_calls = [u for u in http_hits if u == "http://192.168.1.95/api/stop"]
            self.assertEqual(len(scene_calls), 1)
            self.assertIn("mode=jenna", scene_calls[0])
            self.assertIn("fog_ms=3500", scene_calls[0])
            self.assertEqual(len(trigger_calls), 0)
            self.assertGreaterEqual(len(stop_calls), 1)
            self.assertIn("http://192.168.1.60/rpc/Switch.Set?id=0&on=false", http_hits)
            self.assertIn("http://192.168.1.60/rpc/Switch.Set?id=0&on=true", http_hits)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_duck_bed_hold_smoothness(self) -> None:
        sr = sd.SHOW_SR
        bed = np.ones((int(round(3.0 * sr)), 2), dtype=np.float32) * 0.5
        speech = np.zeros((int(round(1.6 * sr)), 2), dtype=np.float32)
        # Word 1 [0.0..0.4s], 300ms inter-word silence gap [0.4..0.7s], Word 2 [0.7..1.2s]
        speech[: int(0.4 * sr)] = 0.40
        speech[int(0.7 * sr) : int(1.2 * sr)] = 0.40
        ducked = sd.duck_bed_under_speech(bed, speech, int(0.4 * sr), sr=sr, duck_gain=0.16, base_gain=0.85)
        gap_mid = ducked[int(0.95 * sr) : int(1.00 * sr)]
        tail = ducked[int(2.60 * sr) : int(2.90 * sr)]
        self.assertLess(float(np.mean(np.abs(gap_mid))), 0.14)
        self.assertGreater(float(np.mean(np.abs(tail))), 0.35)

    def test_dj_seamless_intro_multimedia_and_walkin_beat1(self) -> None:
        ffmpeg_bin = ac.resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        if not ffmpeg_bin:
            self.skipTest("FFmpeg binary not present")

        tmp = Path(tempfile.mkdtemp(prefix="armory_dj_seamless_"))
        try:
            src_dir = tmp / "shows" / "sources"
            src_dir.mkdir(parents=True, exist_ok=True)
            vid_path = src_dir / "jenna_walkin_video.mp4"
            proc = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=30:duration=6.0",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=6.0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(vid_path),
                ],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", "ignore"))

            # Loud steady Beat-1 WAV for matt_walkin onset verification
            beat_pcm = np.ones((int(round(6.0 * sd.SHOW_SR)), 2), dtype=np.float32) * 0.45
            (src_dir / "matt_beat_test.wav").write_bytes(sd.encode_stereo_pcm16_wav(beat_pcm, sr=sd.SHOW_SR))

            director = sd.ShowDirector(root_dir=tmp, ffmpeg_bin=ffmpeg_bin, allow_network=False)
            director.update_show(
                "jenna_walkin",
                {"djSeamlessIntro": True, "djDropSec": 2.0, "videoSourceFile": "jenna_walkin_video.mp4", "overlayText": ""},
                rebuild=False,
            )
            mm_res = director.build_show_multimedia("jenna_walkin", force=True)
            self.assertTrue(mm_res["ok"], msg=str(mm_res))
            self.assertAlmostEqual(float(mm_res["introDurationSec"]), 2.0, delta=0.1)
            self.assertGreater(float(mm_res["totalDurationSec"]), 5.5)
            self.assertLess(float(mm_res["totalDurationSec"]), 6.5)

            director.update_show(
                "matt_walkin",
                {"sourceFile": "matt_beat_test.wav", "songStartSec": 0.0, "songDurationSec": 5.5, "fadeOutSec": 1.0, "addCheer": False},
                rebuild=False,
            )
            p_res = director.build_show_preview("matt_walkin", force=True)
            self.assertTrue(p_res["ok"], msg=str(p_res))
            wav_pcm = sd.read_wav_stereo_float(director.preview_wav_path("matt_walkin"))
            # Last 2.0s before outro fade must have strong beat energy (full-volume onset without muted ramp)
            mid_beat = wav_pcm[-int(round(2.5 * sd.SHOW_SR)) : -int(round(1.5 * sd.SHOW_SR))]
            self.assertGreater(float(np.sqrt(np.mean(mid_beat ** 2))), 0.20)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_show_lab_collapsible_cards_and_walkin_inline_tag_controls(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_show_lab_acc_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.shows.allow_network = False
            app = ac.create_app(svc)
            client = app.test_client()

            dash = client.get("/").get_data(as_text=True)
            # 1. Top-level Show Lab accordions start closed (no `open` attribute on launch)
            for tag_prefix in (
                '<details id="devRyderBox" class="dev-drop-box show-acc">',
                '<details id="ttsKeysCard" class="card show-acc"',
                '<details id="fxHooksCard" class="card show-acc"',
            ):
                self.assertIn(tag_prefix, dash)
                idx = dash.index(tag_prefix)
                close_gt = dash.index(">", idx)
                self.assertNotIn(" open", dash[idx:close_gt].lower())

            # 2. Expand/Collapse controls + shared inline hype/speed/voice helpers present
            for tok in (
                "shExpandAllBtn",
                "shCollapseAllBtn",
                "buildInlineTagBarHtml",
                "applyInlineTagToTextarea",
                "auditionShowVoice",
                "shVoice_",
                "shStyle_",
                "speed:+10%",
                "speed:+20%",
                "speed:-10%",
                "[excited]",
                "[shouting]",
                "vol:+3dB",
                "onShowAccToggle",
            ):
                self.assertIn(tok, dash, msg=f"Missing Show Lab token: {tok}")

            # 3. Updating script, voice & voiceStyle on any walk-in persists cleanly
            payload = {
                "announcerText": "[speed:+10%] [excited] Clear the floor! [shouting] JENNA!!",
                "announcerVoice": "kokoro:am_onyx",
                "voiceStyle": "High energy rockstar hype",
            }
            resp = client.post("/api/shows/jenna_walkin/config", json=payload)
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body["ok"], msg=str(body))
            self.assertEqual(body["show"]["announcerText"], payload["announcerText"])
            self.assertEqual(body["show"]["announcerVoice"], payload["announcerVoice"])
            self.assertEqual(body["show"]["voiceStyle"], payload["voiceStyle"])

            reloaded = sd.ShowDirector(root_dir=tmp, allow_network=False)
            jenna = reloaded.get_show("jenna_walkin")
            assert jenna is not None
            self.assertEqual(jenna.announcer_text, payload["announcerText"])
            self.assertEqual(jenna.announcer_voice, payload["announcerVoice"])
            self.assertEqual(jenna.voice_style, payload["voiceStyle"])
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_motion_engine_v2_slow_movement_and_inter_segment_continuity(self) -> None:
        # Static frames with subtle 1-LSB camera noise must stay well below threshold
        rng = np.random.default_rng(42)
        base = np.full((135, 240, 3), 80, dtype=np.uint8)
        f0 = np.clip(base.astype(np.int16) + rng.integers(-1, 2, size=base.shape), 0, 255).astype(np.uint8)
        f1 = np.clip(base.astype(np.int16) + rng.integers(-1, 2, size=base.shape), 0, 255).astype(np.uint8)
        f2 = np.clip(base.astype(np.int16) + rng.integers(-1, 2, size=base.shape), 0, 255).astype(np.uint8)
        self.assertLess(ac.compute_motion_energy_from_frames([f0, f1, f2]), 0.5)

        # Slow continuous shift across 3 frames (or across segment boundary via prev_frame) scores > 1.4
        s0 = np.full((135, 240, 3), 40, dtype=np.uint8)
        cv2.rectangle(s0, (60, 30), (110, 110), (220, 220, 220), -1)
        s1 = np.full((135, 240, 3), 40, dtype=np.uint8)
        cv2.rectangle(s1, (65, 30), (115, 110), (220, 220, 220), -1)
        s2 = np.full((135, 240, 3), 40, dtype=np.uint8)
        cv2.rectangle(s2, (70, 30), (120, 110), (220, 220, 220), -1)
        self.assertGreater(ac.compute_motion_energy_from_frames([s0, s1, s2]), 1.4)

        # Single static frame within segment vs shifted prev_frame still detects inter-segment motion
        self.assertGreater(ac.compute_motion_energy_from_frames([s2], prev_frame=s0), 1.4)

        # Masked mirror exclusion zone suppresses motion inside that quad to 0.0
        excl = [[(0.2, 0.1), (0.6, 0.1), (0.6, 0.9), (0.2, 0.9)]]
        self.assertEqual(ac.compute_motion_energy_from_frames([s0, s1, s2], exclusions=excl), 0.0)

    def test_always_on_ring_and_247_motion_archiving_lifecycle(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_m247_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()
            client.post("/api/config", json={
                "motionRecord247": True,
                "cameras": [
                    {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.2},
                    {"name": "Smith Cam", "slug": "smith_cam", "rtspUrl": "", "enabled": True, "motionThreshold": 2.2},
                ],
            })
            self.assertTrue(svc.motion_record_247)

            ff = shutil.which("ffmpeg") or svc.ffmpeg
            static_ts = tmp / "static.ts"
            active_ts = tmp / "active.ts"
            subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=10:d=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(static_ts)],
                check=True,
            )
            subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=320x240:r=10:d=2,geq=lum='if(lt(X\\,60*T)\\,240\\,10)':cb=128:cr=128",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(active_ts)],
                check=True,
            )

            rack_cfg = svc._find_camera("Rack Cam")
            assert rack_cfg is not None
            ring_rack = svc.ring_root / "rack_cam"
            ring_rack.mkdir(parents=True, exist_ok=True)
            base_dt = datetime(2026, 11, 22, 11, 0, 0)
            base_ms = int(base_dt.timestamp() * 1000)

            # 1. Idle quiet ring pre-roll segment (0s..6s) -> copied on trigger as pre-roll
            p0 = ring_rack / f"seg_{base_dt.strftime('%Y%m%d_%H%M%S')}.ts"
            shutil.copy2(static_ts, p0)
            old_t = time.time() - 2.0
            os.utime(p0, (old_t, old_t))
            svc._ingest_closed_segments(rack_cfg, time.time(), compute_energy=True)
            motion_sid = f"motion_{base_dt.strftime('%Y-%m-%d')}"
            self.assertFalse((svc.tapes_root / motion_sid).exists())

            # 2. High motion segment outside workout triggers motion_YYYY-MM-DD + copies pre-roll
            dt1 = datetime.fromtimestamp((base_ms + 3000) / 1000.0)
            p1 = ring_rack / f"seg_{dt1.strftime('%Y%m%d_%H%M%S')}.ts"
            shutil.copy2(active_ts, p1)
            os.utime(p1, (old_t, old_t))
            svc._ingest_closed_segments(rack_cfg, time.time(), compute_energy=True)
            self.assertTrue((svc.tapes_root / motion_sid / "rack_cam").is_dir())
            copied = sorted((svc.tapes_root / motion_sid / "rack_cam").glob("seg_*.ts"))
            self.assertEqual([c.name for c in copied], [p0.name, p1.name])
            self.assertGreater(svc.latest_energy.get("rack_cam", 0.0), 1.8)

            # Calling again never duplicates lines in motion.jsonl
            svc._copy_segment_to_session(motion_sid, rack_cfg, ac.SegmentInfo(path=p1, start_ms=base_ms + 3000, end_ms=base_ms + 5000, energy=8.0, camera="Rack Cam"))
            lines = (svc.tapes_root / motion_sid / "motion.jsonl").read_text(encoding="utf-8").splitlines()
            p1_lines = [ln for ln in lines if p1.name in ln]
            self.assertEqual(len(p1_lines), 1)

            # 3. During an active workout, segments go ONLY to the workout session, never to motion_YYYY-MM-DD
            client.post("/api/session/start", json={"sessionId": "workout_nov22", "name": "Upper 1"})
            dt2 = datetime.fromtimestamp((base_ms + 90_000) / 1000.0)
            p2 = ring_rack / f"seg_{dt2.strftime('%Y%m%d_%H%M%S')}.ts"
            shutil.copy2(active_ts, p2)
            os.utime(p2, (old_t, old_t))
            svc._ingest_closed_segments(rack_cfg, time.time(), compute_energy=True)
            self.assertTrue((svc.tapes_root / "workout_nov22" / "rack_cam" / p2.name).is_file())
            self.assertFalse((svc.tapes_root / motion_sid / "rack_cam" / p2.name).exists())
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nvr_scrubber_whole_day_source_filter_gap_frame_fmp4_stream_and_rescan(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_nvr_test_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            app = ac.create_app(svc)
            client = app.test_client()

            cfg_res = client.post("/api/config", json={
                "motionRecord247": False,
                "cameras": [
                    {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "crop": [0.0, 0.0, 1.0, 1.0]},
                    {"name": "Rack Alt", "slug": "rack_alt", "sourceSlug": "rack_cam", "rtspUrl": "", "enabled": True, "crop": [0.1, 0.1, 0.9, 0.9]},
                ],
            })
            self.assertEqual(cfg_res.status_code, 200)
            self.assertTrue(cfg_res.get_json()["ok"])
            self.assertFalse(client.get("/api/config").get_json()["motionRecord247"])
            client.post("/api/config", json={"motionRecord247": True})
            self.assertTrue(client.get("/api/config").get_json()["motionRecord247"])

            ff = shutil.which("ffmpeg") or svc.ffmpeg
            green_ts = tmp / "green.ts"
            blue_ts = tmp / "blue.ts"
            subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=320x240:r=20:d=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(green_ts)],
                check=True,
            )
            subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=320x240:r=20:d=2,geq=lum='if(lt(X\\,50*T)\\,230\\,20)':cb=128:cr=128",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(blue_ts)],
                check=True,
            )

            # Seed Nov 21 workout with two runs separated by a 60-minute gap, a 1-seg stub on same day,
            # a 24/7 motion tape on same day, AND a live ring segment (must not bleed into Nov 21).
            run1_ms = int(datetime(2026, 11, 21, 14, 0, 0).timestamp() * 1000)
            run2_ms = int(datetime(2026, 11, 21, 15, 0, 0).timestamp() * 1000)
            mot_ms = int(datetime(2026, 11, 21, 19, 30, 0).timestamp() * 1000)
            stub_ms = int(datetime(2026, 11, 21, 13, 0, 0).timestamp() * 1000)

            w_dir = svc.tapes_root / "w_nov21"
            (w_dir / "rack_cam").mkdir(parents=True, exist_ok=True)
            (w_dir / "session.json").write_text(json.dumps({"sessionId": "w_nov21", "name": "Lower 2", "kind": "workout", "startedAtMs": run1_ms}), encoding="utf-8")
            w_lines = []
            for base_t, count in ((run1_ms, 3), (run2_ms, 2)):
                for i in range(count):
                    tms = base_t + i * 2000
                    sn = f"seg_{datetime.fromtimestamp(tms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts"
                    shutil.copy2(blue_ts if i == 1 else green_ts, w_dir / "rack_cam" / sn)
                    w_lines.append(json.dumps({"camera": "Rack Cam", "slug": "rack_cam", "file": sn, "startMs": tms, "endMs": tms + 2000, "energy": 7.5 if i == 1 else 0.4}))
            (w_dir / "motion.jsonl").write_text("\n".join(w_lines) + "\n", encoding="utf-8")

            stub_dir = svc.tapes_root / "stub_nov21"
            (stub_dir / "rack_cam").mkdir(parents=True, exist_ok=True)
            shutil.copy2(green_ts, stub_dir / "rack_cam" / f"seg_{datetime.fromtimestamp(stub_ms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts")

            m_dir = svc.tapes_root / "motion_2026-11-21"
            (m_dir / "rack_cam").mkdir(parents=True, exist_ok=True)
            (m_dir / "session.json").write_text(json.dumps({"sessionId": "motion_2026-11-21", "name": "24/7 Motion (2026-11-21)", "kind": "motion", "startedAtMs": mot_ms}), encoding="utf-8")
            m_lines = []
            for i in range(2):
                tms = mot_ms + i * 2000
                sn = f"seg_{datetime.fromtimestamp(tms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts"
                shutil.copy2(blue_ts, m_dir / "rack_cam" / sn)
                m_lines.append(json.dumps({"camera": "Rack Cam", "slug": "rack_cam", "file": sn, "startMs": tms, "endMs": tms + 2000, "energy": 6.2}))
            (m_dir / "motion.jsonl").write_text("\n".join(m_lines) + "\n", encoding="utf-8")

            ring_dir = svc.ring_root / "rack_cam"
            ring_dir.mkdir(parents=True, exist_ok=True)
            ring_ms = int(datetime(2026, 11, 22, 9, 0, 0).timestamp() * 1000)
            shutil.copy2(green_ts, ring_dir / f"seg_{datetime.fromtimestamp(ring_ms / 1000.0).strftime('%Y%m%d_%H%M%S')}.ts")

            # 1. Zero live-ring bleed into historical workout session_samples()
            hist_samples = svc.session_samples("w_nov21")
            self.assertTrue(all(int(s["startMs"]) < ring_ms for s in hist_samples))

            # 2. /api/tapes?source=all|workout|motion suppresses 1-seg stub and exposes true durations & workoutNames
            t_all = client.get("/api/tapes?source=all").get_json()
            self.assertEqual(len(t_all["days"]), 1)
            day = t_all["days"][0]
            self.assertEqual(day["date"], "2026-11-21")
            self.assertEqual(day["tapeCount"], 2)  # stub suppressed!
            self.assertEqual(day["workoutNames"], ["Lower 2"])
            self.assertGreater(day["workoutSec"], 0)
            self.assertGreater(day["motionSec"], 0)
            self.assertLess(day["recordedSec"], 60)  # true recorded seconds, not 5.5h wall-clock span!

            t_mot = client.get("/api/tapes?source=motion").get_json()
            self.assertEqual(len(t_mot["days"]), 1)
            self.assertEqual(t_mot["days"][0]["tapes"][0]["kind"], "motion")

            t_wrk = client.get("/api/tapes?source=workout").get_json()
            self.assertEqual(t_wrk["days"][0]["tapes"][0]["sessionId"], "w_nov21")

            # 3. Whole-day timeline (__day__:2026-11-21) merges workout + 24/7 runs cleanly
            tl_day = client.get("/api/tape/timeline?sessionId=__day__:2026-11-21&camera=Rack%20Cam&source=all").get_json()
            self.assertTrue(tl_day["ok"])
            self.assertEqual(tl_day["segmentCount"], 7)
            self.assertEqual(len(tl_day["runs"]), 3)
            self.assertGreaterEqual(len(tl_day["samples"]), 7)
            self.assertIn("Rack Cam", tl_day["cameraActivity"])

            tl_only_mot = client.get("/api/tape/timeline?sessionId=__day__:2026-11-21&camera=Rack%20Cam&source=motion").get_json()
            self.assertEqual(tl_only_mot["segmentCount"], 2)
            self.assertEqual(len(tl_only_mot["runs"]), 1)

            # 4. Gap-aware /api/tape/frame?maxGapMs=10000 returns 204 inside 60m gap, 200 on footage
            gap_ts = run1_ms + 30 * 60_000
            gap_fr = client.get(f"/api/tape/frame?sessionId=__day__:2026-11-21&camera=Rack%20Cam&tsMs={gap_ts}&maxGapMs=10000")
            self.assertEqual(gap_fr.status_code, 204)
            ok_fr = client.get(f"/api/tape/frame?sessionId=__day__:2026-11-21&camera=Rack%20Cam&tsMs={run1_ms + 1000}&maxGapMs=10000")
            self.assertEqual(ok_fr.status_code, 200)
            self.assertTrue(ok_fr.data.startswith(b"\xff\xd8"))

            # 5. Continuous fMP4 /api/tape/stream.mp4 produces valid ISO BMFF boxes and 404 in empty gaps
            stream_gap = client.get(f"/api/tape/stream.mp4?sessionId=__day__:2026-11-21&camera=Rack%20Cam&startMs={gap_ts}")
            self.assertEqual(stream_gap.status_code, 404)
            self.assertTrue(stream_gap.get_json()["gap"])

            stream_ok = client.get(f"/api/tape/stream.mp4?sessionId=__day__:2026-11-21&camera=Rack%20Cam&startMs={run1_ms}&streamId=test_s1")
            self.assertEqual(stream_ok.status_code, 200)
            self.assertEqual(stream_ok.mimetype, "video/mp4")
            self.assertEqual(stream_ok.headers.get("Accept-Ranges"), "none")
            self.assertIn("X-Stream-Start-Ms", stream_ok.headers)
            self.assertIn("X-Stream-End-Ms", stream_ok.headers)
            mp4_bytes = stream_ok.data
            self.assertIn(b"ftyp", mp4_bytes[:64])
            self.assertIn(b"moov", mp4_bytes)
            self.assertIn(b"moof", mp4_bytes)

            stop_res = client.post("/api/tape/stream/stop", json={"streamId": "test_s1"})
            self.assertEqual(stop_res.status_code, 200)
            self.assertEqual(len(svc._stream_procs), 0)

            # 6. POST /api/tape/rescan writes motion_v2.json including virtual views ("Rack Alt")
            rescan_res = client.post("/api/tape/rescan", json={"sessionId": "__day__:2026-11-21", "force": True})
            self.assertEqual(rescan_res.status_code, 200)
            self.assertTrue(rescan_res.get_json()["ok"])
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if (w_dir / "motion_v2.json").is_file() and not svc._scan_active:
                    break
                time.sleep(0.05)
            self.assertTrue((w_dir / "motion_v2.json").is_file())
            v2_obj = json.loads((w_dir / "motion_v2.json").read_text(encoding="utf-8"))
            self.assertEqual(v2_obj["version"], ac.MOTION_RESCAN_VERSION)
            self.assertIn("rack_cam", v2_obj["views"])
            self.assertIn("rack_alt", v2_obj["views"])
            self.assertEqual(len(v2_obj["views"]["rack_alt"]["scores"]), 5)
            tl_alt = client.get("/api/tape/timeline?sessionId=w_nov21&camera=Rack%20Alt").get_json()
            self.assertEqual(tl_alt["scanStatus"], "ready")

            # 7. Dashboard HTML exposes NVR scrubber controls & zero legacy tokens
            dash = client.get("/").get_data(as_text=True)
            for tok in ("data-tssrc", "motionRecord247", "tsGapOverlay", "tsHoverPop", "tsSensSlider", "tsMotionOnlyBtn", "tsRescanBtn"):
                self.assertIn(tok, dash, msg=f"Missing NVR UI token: {tok}")
            for dead_tok in ("alignBar", "pinRefBtn", "realignModeBtn", "driftNtfyBtn", "Secondary / Pinned"):
                self.assertNotIn(dead_tok, dash)
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ingest_precaches_scrub_thumbs_and_parallel_bursts(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_precache_"))
        svc = ac.ArmoryCamService(root_dir=tmp)
        try:
            app = ac.create_app(svc)
            client = app.test_client()
            client.post(
                "/api/config",
                json={
                    "cameras": [
                        {"name": "Rack Cam", "slug": "rack_cam", "rtspUrl": "", "enabled": True, "defish": False},
                    ]
                },
            )
            ff = shutil.which("ffmpeg") or svc.ffmpeg
            rack_cfg = svc._find_camera("Rack Cam")
            assert rack_cfg is not None
            ring_dir = svc.ring_root / "rack_cam"
            ring_dir.mkdir(parents=True, exist_ok=True)
            svc.recording = True
            svc.paused = False
            svc.active_session_id = "sess_precache"

            base_ms = int(datetime(2026, 11, 23, 22, 0, 0).timestamp() * 1000)
            seg_names: list[str] = []
            for i in range(4):
                seg_dt = datetime.fromtimestamp((base_ms + i * 2000) / 1000.0)
                seg_name = f"seg_{seg_dt.strftime('%Y%m%d_%H%M%S')}.ts"
                seg_names.append(seg_name)
                seg_path = ring_dir / seg_name
                if i == 0:
                    subprocess.run(
                        [
                            ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=20:d=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg_path),
                        ],
                        check=True,
                    )
                else:
                    shutil.copy2(ring_dir / seg_names[0], seg_path)
                old_t = time.time() - 5.0
                os.utime(seg_path, (old_t, old_t))

            svc._ingest_closed_segments(rack_cfg, time.time(), compute_energy=True)

            ck = svc._crop_cache_key(rack_cfg)
            cache_dir = svc.thumbs_root / "rack_cam" / ck
            cached_jpgs = sorted(p.name for p in cache_dir.glob("*.jpg"))
            self.assertEqual(len(cached_jpgs), 4)
            for sn in seg_names:
                jpg_path = cache_dir / f"{Path(sn).stem}.jpg"
                self.assertTrue(jpg_path.is_file())
                self.assertTrue(jpg_path.read_bytes().startswith(b"\xff\xd8"))

            # Subsequent tape_frame_pack must hit the pre-cached thumbnails cleanly
            pack = svc.tape_frame_pack("Rack Cam", "sess_precache", count=4)
            self.assertTrue(pack["ok"])
            self.assertEqual(len(pack["frames"]), 4)
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_camera_stall_watchdog_and_stale_connected_status(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_wd_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)

            class _FakeHungProc:
                def __init__(self, pid: int) -> None:
                    self.pid = pid
                    self.killed = False

                def poll(self):
                    return -9 if self.killed else None

                def kill(self) -> None:
                    self.killed = True

            p_rack = _FakeHungProc(1001)
            p_smith = _FakeHungProc(1002)
            now_m = 1000.0
            with svc.lock:
                svc._procs["rack_cam"] = p_rack  # type: ignore[assignment]
                svc._procs["smith_cam"] = p_smith  # type: ignore[assignment]
                svc._proc_started_mono["rack_cam"] = now_m - 60.0
                svc._proc_started_mono["smith_cam"] = now_m - 60.0
                # Rack Cam is healthy (fresh frame + fresh segment)
                svc._last_frame_mono["rack_cam"] = now_m - 1.0
                svc._last_seg_mono["rack_cam"] = now_m - 2.0
                svc.cam_connected["rack_cam"] = True
                # Smith Cam hung 25s ago (zero frames, zero segments)
                svc._last_frame_mono["smith_cam"] = now_m - 25.0
                svc._last_seg_mono["smith_cam"] = now_m - 25.0
                svc.cam_connected["smith_cam"] = True

            # Before watchdog kills it, stale connected check must already report False for smith_cam
            with svc.lock:
                self.assertTrue(svc._is_source_connected_locked("rack_cam", now_mono=now_m))
                self.assertFalse(svc._is_source_connected_locked("smith_cam", now_mono=now_m))

            restarted = svc.check_camera_stall_watchdog(now_mono=now_m, stall_sec=20.0)
            self.assertEqual(restarted, ["smith_cam"])
            self.assertFalse(p_rack.killed)
            self.assertTrue(p_smith.killed)
            self.assertFalse(svc.cam_connected["smith_cam"])
            self.assertTrue(svc.cam_connected["rack_cam"])
            self.assertEqual(svc._cam_watchdog_restarts.get("smith_cam"), 1)

            # Orphaned 0-byte .ts file older than 30s must be cleaned up during ingest
            rack_cfg = svc._find_camera("Rack Cam")
            assert rack_cfg is not None
            ring_dir = svc.ring_root / "rack_cam"
            ring_dir.mkdir(parents=True, exist_ok=True)
            zero_ts = ring_dir / "seg_20261124_121503.ts"
            zero_ts.write_bytes(b"")
            old_t = time.time() - 60.0
            os.utime(zero_ts, (old_t, old_t))
            svc._ingest_closed_segments(rack_cfg, time.time(), compute_energy=False)
            self.assertFalse(zero_ts.exists())
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_proportional_denon_ducking_and_jarvis_openwakeword_flow(self) -> None:
        # 1. Exponential proportional duck curve: 0 dB <= Vol 45 (-35 dB), growing smoothly to ~18.5 dB @ Vol 70 & 24 dB @ Vol 75
        self.assertEqual(ha.compute_proportional_duck_db(-40.0), 0.0)
        self.assertEqual(ha.compute_proportional_duck_db(-35.0), 0.0)
        self.assertEqual(ha.compute_proportional_duck_db(-30.0), 1.5)
        self.assertEqual(ha.compute_proportional_duck_db(-25.0), 4.5)
        self.assertEqual(ha.compute_proportional_duck_db(-20.0), 8.0)
        self.assertEqual(ha.compute_proportional_duck_db(-15.0), 13.0)
        self.assertEqual(ha.compute_proportional_duck_db(-10.0), 18.0)
        self.assertEqual(ha.compute_proportional_duck_db(-5.0), 24.0)

        # 2. Fast 1-step begin_wake_duck -> nested duck_context -> smooth restore on end_wake_duck
        state = {"vol": -10.0, "cmds": []}

        def fake_denon(url: str, _timeout: float) -> tuple[int, bytes]:
            if "formMainZone_MainZoneXmlStatus.xml" in url:
                xml = (
                    f"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect>"
                    f"<MasterVolume><value>{state['vol']:.1f}</value></MasterVolume>"
                    f"<Mute><value>off</value></Mute><SurrMode><value>MULTI CH STEREO</value></SurrMode></item>"
                )
                return 200, xml.encode("utf-8")
            if "formiPhoneAppDirect.xml?" in url:
                cmd = url.split("?", 1)[1]
                state["cmds"].append(cmd)
                if cmd.startswith("MV"):
                    raw = cmd[2:]
                    val = float(raw[:2]) + (0.5 if len(raw) == 3 and raw[2] == "5" else 0.0)
                    state["vol"] = val - 80.0
                return 200, b""
            return 404, b""

        ha_cfg = ha.HouseAudioConfig(
            denon_enabled=True,
            denon_host="192.168.1.15",
            denon_duck_enabled=True,
            denon_duck_mode="proportional",
            denon_duck_threshold_db=-35.0,
            denon_duck_max_drop_db=24.0,
            denon_duck_span_db=30.0,
            denon_duck_exponent=1.55,
            denon_duck_floor_db=-35.0,
            denon_ramp_steps=3,
            denon_ramp_step_ms=0,
        )
        denon = ha.DenonAvrClient(lambda: ha_cfg, http_get=fake_denon, sleep_fn=lambda _s: None)

        # Below Vol 45 (-36 dB) -> zero ducking
        state["vol"] = -36.0
        r_low = denon.begin_wake_duck()
        self.assertFalse(r_low["ducked"])
        self.assertEqual(state["cmds"], [])

        # At Vol 70 (-10 dB) -> single-step instant dip to -28.0 dB (Vol 52.0)
        state["vol"] = -10.0
        r_hi = denon.begin_wake_duck(max_hold_sec=30.0, use_cache_sec=0.0)
        self.assertTrue(r_hi["ducked"])
        self.assertAlmostEqual(r_hi["dropDb"], 18.0)
        self.assertAlmostEqual(state["vol"], -28.0)
        self.assertEqual(len(state["cmds"]), 1)

        # Nested duck_context during TTS readback does not double-duck or prematurely restore
        with denon.duck_context() as nested:
            self.assertTrue(nested["ducked"])
            self.assertTrue(nested["nested"])
            self.assertAlmostEqual(state["vol"], -28.0)
        self.assertAlmostEqual(state["vol"], -28.0)

        # Release wake duck -> smooth 3-step restore back to -10.0 dB (MV70)
        r_end = denon.end_wake_duck()
        self.assertTrue(r_end["restored"])
        self.assertAlmostEqual(state["vol"], -10.0)
        self.assertEqual(state["cmds"][-1], "MV70")

        # 3. Jarvis wake prefix stripping in display_control & room_voice + SSE translation for Armory APK
        self.assertEqual(dc._strip_wake_prefix("Hey Jarvis, open the gym", wake_word="hey jarvis"), "open the gym")
        self.assertEqual(dc._strip_wake_prefix("Jarvis, next song", wake_word="hey jarvis"), "next song")
        self.assertEqual(rv.strip_wake_word("Hey Jarvis, record this.", "hey jarvis"), "record this.")
        self.assertEqual(rv.strip_wake_word("Hey Jarvis", "hey jarvis"), "")
        self.assertTrue(rv.matches_wake_word("Hey Jarvis, 225 for 8", "hey jarvis"))
        p_j, hw_j = rv.build_whisper_prompt("hey jarvis", ["Lat Pulldown"])
        self.assertTrue(p_j.startswith("Hey Jarvis, record this. Hey Jarvis, done."))
        self.assertIn("Hey Jarvis", hw_j)

        # 4. Streaming openWakeWord flow: wake -> Denon duck (no chime) -> lone wake keeps armed -> command translates SSE to client wake
        duck_events: list[str] = []

        class _FakeOwwSpotter:
            ready = True
            latest_score = 0.0
            threshold = 0.38
            model_name = "hey_jarvis_v0.1"

            def __init__(self) -> None:
                self.fire_next = False

            def reset(self) -> None:
                pass

            def push_samples(self, _pcm: np.ndarray, now_mono: float | None = None) -> tuple[bool, float]:
                if self.fire_next:
                    self.fire_next = False
                    self.latest_score = 0.99
                    return True, 0.99
                return False, 0.02

        spot = _FakeOwwSpotter()
        spoken: list[str] = []
        vcfg = rv.RoomVoiceConfig(wake_word="hey jarvis", wake_engine="openwakeword", wake_threshold=0.38)
        svc = rv.RoomVoiceService(
            config=vcfg,
            speak_fn=lambda txt: (spoken.append(txt), True)[1],
            allow_hardware_audio=False,
            wake_spotter=spot,
            wake_duck_callbacks={
                "begin": lambda: (duck_events.append("begin"), {"ducked": True, "dropDb": 18.5, "fromDb": -10.0, "toDb": -28.5})[1],
                "end": lambda: (duck_events.append("end"), {"restored": True})[1],
                "extend": lambda _s=6.0: duck_events.append("extend"),
            },
        )
        # Pixel APK pushes "armory" context -> must NOT overwrite server's "hey jarvis" wake word
        svc.set_context(active=True, wake_word="armory", readback=True)
        self.assertEqual(svc.status_dict()["wakeWord"], "hey jarvis")
        self.assertEqual(svc._client_wake_word, "armory")

        # Idle audio with no wake -> 0 utterances queued
        idle_chunk = np.full(rv.VAD_FRAME_SAMPLES * 4, 0.05, dtype=np.float32)
        svc._ingest_pcm16_frame(idle_chunk, muted=False, now_ms=10_000, now_mono=10.0)
        self.assertTrue(svc._utt_queue.empty())
        self.assertEqual(duck_events, [])

        # Wake detected -> arms immediately and calls begin_wake_duck
        spot.fire_next = True
        svc._ingest_pcm16_frame(idle_chunk, muted=False, now_ms=11_000, now_mono=11.0)
        self.assertGreater(svc.armed_until_ms, 11_000)
        # Wait briefly for wake callback thread
        for _ in range(30):
            if "begin" in duck_events:
                break
            time.sleep(0.02)
        self.assertIn("begin", duck_events)
        self.assertTrue(svc._wake_duck_held)

        # Lone "Hey Jarvis" transcribed during capture -> stays armed + stays ducked!
        lone = svc._emit_transcript("Hey Jarvis.", latency_ms=120, from_oww=True)
        self.assertFalse(lone["emitted"])
        self.assertTrue(lone.get("awaitingCommand"))
        self.assertTrue(svc._wake_duck_held)

        # Actual command "225 for 8" -> dashboard shows "Hey Jarvis, 225 for 8", SSE emits "Armory, 225 for 8" for Pixel APK!
        svc.sse_clients = 1
        em = svc._emit_transcript("225 for 8", latency_ms=280, from_oww=True)
        self.assertTrue(em["emitted"])
        self.assertEqual(em["transcript"], "Hey Jarvis, 225 for 8")
        self.assertEqual(svc._events[-1]["transcript"], "Armory, 225 for 8")
        self.assertEqual(svc._events[-1]["wakeWord"], "armory")

        # Ack with TTS speech -> speaks confirmation & releases wake duck once finished
        svc.handle_ack(speech="225 for 8, locked in", text="✓ 225 lb × 8", kind="ok")
        self.assertEqual(spoken, ["225 for 8, locked in"])
        self.assertIn("extend", duck_events)
        self.assertIn("end", duck_events)
        self.assertFalse(svc._wake_duck_held)

    def test_dual_stage_listening_vs_speaking_ducking_and_dashboard_api(self) -> None:
        state = {"vol": -10.0, "cmds": []}

        def fake_denon(url: str, _timeout: float) -> tuple[int, bytes]:
            if "formMainZone_MainZoneXmlStatus.xml" in url:
                xml = (
                    f"<item><Power><value>ON</value></Power><InputFuncSelect><value>MPLAY</value></InputFuncSelect>"
                    f"<MasterVolume><value>{state['vol']:.1f}</value></MasterVolume>"
                    f"<Mute><value>off</value></Mute><SurrMode><value>MULTI CH STEREO</value></SurrMode></item>"
                )
                return 200, xml.encode("utf-8")
            if "formiPhoneAppDirect.xml?" in url:
                cmd = url.split("?", 1)[1]
                state["cmds"].append(cmd)
                if cmd.startswith("MV"):
                    raw = cmd[2:]
                    val = float(raw[:2]) + (0.5 if len(raw) == 3 and raw[2] == "5" else 0.0)
                    state["vol"] = val - 80.0
                return 200, b""
            return 404, b""

        ha_cfg = ha.HouseAudioConfig(
            denon_enabled=True,
            denon_host="192.168.1.15",
            denon_duck_enabled=True,
            denon_duck_mode="proportional",
            denon_duck_threshold_db=-35.0,  # Vol 45
            denon_duck_max_drop_db=24.0,
            denon_duck_span_db=30.0,
            denon_duck_exponent=1.55,
            denon_duck_floor_db=-35.0,
            denon_output_duck_mode="proportional",
            denon_output_duck_threshold_db=-30.0,  # Vol 50
            denon_output_duck_max_drop_db=10.0,
            denon_output_duck_span_db=25.0,
            denon_output_duck_exponent=1.50,
            denon_output_duck_floor_db=-35.0,
            denon_ramp_steps=3,
            denon_ramp_step_ms=0,
        )
        denon = ha.DenonAvrClient(lambda: ha_cfg, http_get=fake_denon, sleep_fn=lambda _s: None)

        # 1. Listening Deep (-18 dB @ Vol 70 -> -28 dB) transitions smoothly to Speaking Mild (-7 dB @ Vol 70 -> -17 dB), then restores to -10 dB
        r_wake = denon.begin_wake_duck(max_hold_sec=30.0, use_cache_sec=0.0)
        self.assertTrue(r_wake["ducked"])
        self.assertEqual(r_wake["stage"], "listen")
        self.assertAlmostEqual(r_wake["dropDb"], 18.0)
        self.assertAlmostEqual(state["vol"], -28.0)

        with denon.duck_context(stage="output") as out_ctx:
            self.assertTrue(out_ctx["ducked"])
            self.assertTrue(out_ctx["nested"])
            self.assertAlmostEqual(out_ctx["toDb"], -17.0)
            self.assertAlmostEqual(state["vol"], -17.0)
        # Still held at output level until end_wake_duck releases
        self.assertAlmostEqual(state["vol"], -17.0)

        r_end = denon.end_wake_duck()
        self.assertTrue(r_end["restored"])
        self.assertAlmostEqual(state["vol"], -10.0)
        self.assertEqual(state["cmds"][-1], "MV70")

        # 2. Listening Deep + Speaking Off -> music restores immediately when Jarvis replies, and end_wake_duck avoids redundant MV ramp
        ha_cfg.denon_output_duck_mode = "off"
        state["cmds"].clear()
        r_wake2 = denon.begin_wake_duck(max_hold_sec=30.0, use_cache_sec=0.0)
        self.assertTrue(r_wake2["ducked"])
        self.assertAlmostEqual(state["vol"], -28.0)
        with denon.duck_context(stage="output") as out_off_ctx:
            self.assertTrue(out_off_ctx["nested"])
            self.assertAlmostEqual(state["vol"], -10.0)
        cmds_before_end = len(state["cmds"])
        r_end2 = denon.end_wake_duck()
        self.assertTrue(r_end2["restored"])
        self.assertTrue(r_end2.get("alreadyAtSaved"))
        self.assertEqual(len(state["cmds"]), cmds_before_end)

        # 3. Listening Off + Speaking Proportional -> wake leaves Denon untouched, standalone output speech still ducks -7 dB & restores
        ha_cfg.denon_duck_enabled = False
        ha_cfg.denon_output_duck_mode = "proportional"
        state["cmds"].clear()
        r_no_wake = denon.begin_wake_duck(max_hold_sec=30.0, use_cache_sec=0.0)
        self.assertFalse(r_no_wake["ducked"])
        self.assertEqual(r_no_wake["reason"], "disabled")
        self.assertEqual(state["cmds"], [])

        with denon.duck_context(stage="output") as solo_out:
            self.assertTrue(solo_out["ducked"])
            self.assertEqual(solo_out["stage"], "output")
            self.assertAlmostEqual(solo_out["dropDb"], 7.0)
            self.assertAlmostEqual(state["vol"], -17.0)
        self.assertAlmostEqual(state["vol"], -10.0)

        # 4. Dashboard HTML + /api/audio/config persistence + /api/audio/denon/test-duck stage support
        tmp = Path(tempfile.mkdtemp(prefix="armory_cam_dual_duck_"))
        try:
            svc = ac.ArmoryCamService(root_dir=tmp)
            svc.house_audio.denon._http_get = fake_denon
            svc.house_audio.denon._sleep = lambda _s: None
            app = ac.create_app(svc)
            client = app.test_client()

            dash_html = client.get("/").get_data(as_text=True)
            for marker in (
                "haListenPresets",
                "haOutPresets",
                "haListenDuckMode",
                "haOutDuckMode",
                "haListenPreview",
                "haOutPreview",
                "haTestListenDipBtn",
                "haTestOutDipBtn",
                "HA_LISTEN_PRESETS",
                "HA_OUT_PRESETS",
                "runDenonStageDip",
            ):
                self.assertIn(marker, dash_html)

            save_res = client.post(
                "/api/audio/config",
                json={
                    "denonEnabled": True,
                    "denonHost": "192.168.1.15",
                    "denonDuckEnabled": True,
                    "denonDuckMode": "proportional",
                    "denonDuckThresholdDb": -35.0,
                    "denonDuckMaxDropDb": 24.0,
                    "denonDuckSpanDb": 30.0,
                    "denonDuckExponent": 1.55,
                    "denonDuckFloorDb": -35.0,
                    "denonOutputDuckMode": "proportional",
                    "denonOutputDuckThresholdDb": -30.0,
                    "denonOutputDuckMaxDropDb": 10.0,
                    "denonOutputDuckSpanDb": 25.0,
                    "denonOutputDuckExponent": 1.50,
                    "denonOutputDuckFloorDb": -35.0,
                    "denonRampStepMs": 20,
                },
            )
            self.assertEqual(save_res.status_code, 200)
            saved_cfg = save_res.get_json()["config"]
            self.assertEqual(saved_cfg["denonDuckMode"], "proportional")
            self.assertEqual(saved_cfg["denonOutputDuckMode"], "proportional")
            self.assertAlmostEqual(saved_cfg["denonOutputDuckMaxDropDb"], 10.0)
            self.assertAlmostEqual(saved_cfg["denonOutputDuckThresholdDb"], -30.0)

            # Test stage="listen" (force=False) dips -18 dB at Vol 70, stage="output" dips -7 dB
            state["vol"] = -10.0
            t_listen = client.post("/api/audio/denon/test-duck", json={"stage": "listen", "force": False, "holdSec": 0.3}).get_json()
            self.assertTrue(t_listen["ok"], t_listen)
            self.assertEqual(t_listen["stage"], "listen")
            self.assertAlmostEqual(t_listen["duck"]["dropDb"], 18.0)
            self.assertAlmostEqual(state["vol"], -10.0)

            t_out = client.post("/api/audio/denon/test-duck", json={"stage": "output", "force": False, "holdSec": 0.3}).get_json()
            self.assertTrue(t_out["ok"], t_out)
            self.assertEqual(t_out["stage"], "output")
            self.assertAlmostEqual(t_out["duck"]["dropDb"], 7.0)
            self.assertAlmostEqual(state["vol"], -10.0)

            # Setting Speaking Duck to "off" skips dip when force=False
            client.post("/api/audio/config", json={"denonOutputDuckMode": "off"})
            t_off = client.post("/api/audio/denon/test-duck", json={"stage": "output", "force": False, "holdSec": 0.3}).get_json()
            self.assertFalse(t_off["ok"], t_off)
            self.assertEqual(t_off["duck"]["reason"], "output_off")
        finally:
            svc.stop_background()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
