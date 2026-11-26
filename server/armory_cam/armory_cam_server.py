"""Armory Cam — OptiPlex LAN DVR, Live MJPEG Streamer, Motion-Burst Detector & Clip Server.

Listens on port 8766 (default) and serves the exact REST + HTTP 206 byte-range
contract consumed by Armory Fit (`src/lifts/gymCam.ts`), plus a real-time
web dashboard at `/` with interactive zoom/crop + rotate controls.

Configuration & media live in `ARMORY_CAM_ROOT` (default `S:\\ArmoryCam`),
outside the git workspace so RTSP credentials are never committed.
"""

from __future__ import annotations

import base64
import bisect
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import dataclass, field, replace
from datetime import datetime
import glob
import hashlib
import json
import logging
import logging.handlers
import math
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

import cv2
from flask import Flask, Response, jsonify, request
import numpy as np
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

try:
    from .room_voice import ATLAS_VOICE_CATALOG, RoomVoiceConfig, RoomVoiceService, strip_wake_word, wake_phrase_slug
except ImportError:
    from room_voice import ATLAS_VOICE_CATALOG, RoomVoiceConfig, RoomVoiceService, strip_wake_word, wake_phrase_slug

try:
    from .room_audio import RoomAudioRecorder
except ImportError:
    from room_audio import RoomAudioRecorder

try:
    from .house_audio import HouseAudioConfig, HouseAudioManager, encode_mono_pcm_to_wav, validate_sfx_filename
except ImportError:
    from house_audio import HouseAudioConfig, HouseAudioManager, encode_mono_pcm_to_wav, validate_sfx_filename

try:
    from .display_control import GymDisplayController, GymDisplaysConfig, parse_display_voice_command
except ImportError:
    from display_control import GymDisplayController, GymDisplaysConfig, parse_display_voice_command

try:
    from .show_director import ShowDirector
except ImportError:
    from show_director import ShowDirector

DEFAULT_PORT = 8766
DEFAULT_RETENTION_DAYS = 14
DEFAULT_PRE_ROLL_SEC = 120
DEFAULT_MOTION_THRESHOLD = 2.2
DEFAULT_ADB_PORT = 5555
DEFAULT_IDLE_SLEEP_SEC = 600
DEFAULT_SCRUB_THUMB_W = 280
DEFAULT_SCRUB_THUMB_Q = 58
MOTION_PRE_ROLL_SEC = 12
MOTION_POST_ROLL_SEC = 18
MOTION_RESCAN_VERSION = 2
MAX_STREAM_CONCURRENT = 4
TAPE_GAP_MS = 15_000
MIN_CROP_SPAN = 0.15
MIN_EXCL_SPAN = 0.04
MAX_EXCLUSIONS = 6
Quad = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
RING_KEEP_MIN_SEC = 300
PREVIEW_KEEP_SEC = 24 * 3600
PREVIEW_MAX_FILES = 120
BUMP_CHECK_INTERVAL_SEC = 600.0
BUMP_PX_THRESHOLD = 20.0
BUMP_DEG_THRESHOLD = 1.8
BUMP_NTFY_COOLDOWN_SEC = 6 * 3600.0
BUMP_REF_SIZE = (320, 180)
BUMP_LIVE_SIZE = (640, 360)
BUMP_OVERLAY_SIZE = (960, 540)
BUMP_ALIGNED_PX = 5.0
BUMP_ALIGNED_DEG = 0.4
BUMP_CLOSE_PX = 12.0
BUMP_CLOSE_DEG = 0.9
WYZE_V3_REF_W = 1920
WYZE_V3_REF_H = 1080
WYZE_V3_F_EQ = 965.0
PREVIEW_DRIVE_FILENAME = "Armory_Cam_Preview.jpg"
PREVIEW_DRIVE_INTERVAL_SEC = 1800.0
SEG_NAME_RE = re.compile(r"^seg_(\d{8})_(\d{6})\.ts$")

FALLBACK_WALLBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Fit · Gym Wallboard</title>
<style>html,body{height:100%;margin:0;background:#070b12;color:#f2f6fc;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;text-align:center}
.box{padding:32px;border:1px solid #1e2b42;border-radius:18px;background:#0f1724;max-width:680px}.mut{color:#8fa2bf;margin-top:8px;font-size:18px}
h1{margin:0;font-size:40px;letter-spacing:.08em}#st{font-size:28px;margin-top:16px;color:#38bdf8;font-weight:700}</style></head>
<body><div class="box"><h1>ARMORY FIT · WALLBOARD</h1><div id="st">Standby · Waiting for workout on Pixel</div>
<div class="mut" id="sub">Connect Armory Fit on Wi-Fi to start streaming live sets, rest timer &amp; HR.</div></div>
<script>
const paint = (d) => {
  const st = document.getElementById('st'), sub = document.getElementById('sub');
  if (!d) return;
  if (d.mode === 'lift' && d.lift) {
    st.textContent = d.lift.title + ' · ' + d.lift.setsDone + '/' + d.lift.setsTotal + ' sets';
    sub.textContent = d.hr && d.hr.fresh ? ('HR ' + d.hr.bpm + ' BPM') : 'Strength active';
  } else if (d.mode === 'cardio' && d.cardio) {
    st.textContent = d.cardio.title + ' · ' + d.cardio.phase;
    sub.textContent = Math.round(d.cardio.watts || 0) + ' W · ' + (d.cardio.countdownText || '');
  } else {
    st.textContent = (d.standby && d.standby.timeLabel) ? ('Standby · ' + d.standby.timeLabel) : 'Standby';
    sub.textContent = '';
  }
};
fetch('/wallboard/state?t=' + Date.now()).then(r => r.json()).then(paint).catch(() => {});
try { const es = new EventSource('/wallboard/events'); es.onmessage = (e) => { try { paint(JSON.parse(e.data)); } catch {} }; } catch {}
</script></body></html>"""

WALLBOARD_STAGE_OVERLAY_SNIPPET = """
<div id="armoryShowStage" style="display:none;position:fixed;inset:0;z-index:999999;background:#000;overflow:hidden">
  <video id="armoryStageVid" playsinline muted preload="auto" style="width:100%;height:100%;object-fit:contain;background:#000"></video>
  <div id="armoryStageFlash" style="pointer-events:none;position:fixed;inset:0;background:#fff;opacity:0;display:none;z-index:10"></div>
</div>
<script>
(function(){
  if (window._armoryStageInit) return;
  window._armoryStageInit = true;
  var box = document.getElementById('armoryShowStage');
  var vid = document.getElementById('armoryStageVid');
  var flashEl = document.getElementById('armoryStageFlash');
  if (!box || !vid) return;
  var role = 'wallboard';
  var clockOffsetMs = 0;
  var curRunId = 0;
  var readyAcked = false;
  var playingAcked = false;
  var startTimer = null;
  var safetyTimer = null;
  var syncIv = null;
  var flashRaf = null;
  var flashWindows = [];
  var activeStartMs = 0;

  function buildFlashWindows(list){
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length; i++){
      var item = list[i] || {};
      var at = Number(item.atSec || 0);
      var dur = Math.max(0.08, Number(item.durSec || 0.2));
      var gap = Math.max(0.08, Number(item.offSec || 0.22));
      var cnt = Math.max(1, Math.min(6, Number(item.count || 1)));
      for (var k = 0; k < cnt; k++){
        var t0 = at + k * (dur + gap);
        out.push({ t0: t0, t1: t0 + dur });
      }
    }
    return out;
  }

  function clearFlash(){
    if (flashRaf){ cancelAnimationFrame(flashRaf); flashRaf = null; }
    if (flashEl){ flashEl.style.opacity = '0'; flashEl.style.display = 'none'; }
  }

  function tickFlash(){
    if (!curRunId || vid.paused || vid.ended){
      clearFlash();
      return;
    }
    var t = vid.currentTime || 0;
    var lit = false;
    for (var i = 0; i < flashWindows.length; i++){
      if (t >= flashWindows[i].t0 && t < flashWindows[i].t1){ lit = true; break; }
    }
    if (flashEl){
      flashEl.style.display = lit ? 'block' : 'none';
      flashEl.style.opacity = lit ? '0.45' : '0';
    }
    flashRaf = requestAnimationFrame(tickFlash);
  }

  function syncClock(){
    var bestRtt = 999999;
    function sample(left){
      if (left <= 0) return;
      var t0 = Date.now();
      fetch('/api/shows/time?t=' + t0).then(function(r){ return r.json(); }).then(function(d){
        var t1 = Date.now(), rtt = t1 - t0;
        if (d && d.nowMs && rtt < bestRtt){
          bestRtt = rtt;
          clockOffsetMs = Number(d.nowMs) - (t0 + rtt * 0.5);
        }
        sample(left - 1);
      }).catch(function(){});
    }
    sample(3);
  }
  syncClock();
  setInterval(syncClock, 60000);

  function postAck(state){
    if (!curRunId) return;
    fetch('/api/shows/stage/ack', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ role: role, runId: curRunId, state: state })
    }).catch(function(){});
  }

  function clearPlayTimers(){
    if (startTimer){ clearTimeout(startTimer); startTimer = null; }
    if (safetyTimer){ clearTimeout(safetyTimer); safetyTimer = null; }
    if (syncIv){ clearInterval(syncIv); syncIv = null; }
    clearFlash();
  }

  function stopStage(){
    clearPlayTimers();
    flashWindows = [];
    curRunId = 0;
    readyAcked = false;
    activeStartMs = 0;
    vid.oncanplay = null;
    vid.onloadeddata = null;
    vid.onended = null;
    vid.onerror = null;
    try { vid.pause(); vid.playbackRate = 1.0; vid.removeAttribute('src'); vid.load(); } catch(e){}
    box.style.display = 'none';
  }

  function prepareStage(ev){
    clearPlayTimers();
    curRunId = Number(ev.runId || 0);
    readyAcked = false;
    activeStartMs = 0;
    flashWindows = buildFlashWindows(ev.flashEvents);
    box.style.display = 'block';
    vid.muted = true;
    vid.volume = 0;
    vid.playbackRate = 1.0;
    var url = String(ev.videoUrl || '');
    if (!url) return;
    var sendReady = function(){
      if (readyAcked || !curRunId) return;
      readyAcked = true;
      postAck('ready');
    };
    var fallbackT = setTimeout(sendReady, 2600);
    vid.oncanplay = function(){ clearTimeout(fallbackT); sendReady(); };
    vid.onloadeddata = function(){ try { vid.currentTime = 0; } catch(e){} };
    vid.onerror = function(){ stopStage(); };
    safetyTimer = setTimeout(stopStage, 30000);
    vid.onended = function(){
      var rid = curRunId;
      stopStage();
      if (rid) {
        fetch('/api/shows/stage/ack', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ role: role, runId: rid, state: 'ended' })
        }).catch(function(){});
      }
    };
    try { vid.pause(); } catch(e){}
    vid.src = url;
    vid.load();
  }

  function beginPlayback(){
    if (!curRunId) return;
    box.style.display = 'block';
    vid.muted = true;
    vid.volume = 0;
    var expected = activeStartMs ? Math.max(0, (Date.now() + clockOffsetMs - activeStartMs) / 1000.0) : 0;
    try {
      if (Math.abs((vid.currentTime || 0) - expected) > 0.18) vid.currentTime = expected;
    } catch(e){}
    vid.playbackRate = 1.0;
    var p = vid.play();
    if (p && typeof p.catch === 'function') p.catch(function(){});
    if (flashWindows.length){
      clearFlash();
      flashRaf = requestAnimationFrame(tickFlash);
    }
    if (syncIv) clearInterval(syncIv);
    syncIv = setInterval(function(){
      if (!activeStartMs || vid.paused || vid.ended) return;
      var exp = Math.max(0, (Date.now() + clockOffsetMs - activeStartMs) / 1000.0);
      var drift = exp - (vid.currentTime || 0);
      if (Math.abs(drift) >= 0.90) {
        try { vid.currentTime = exp; } catch(e){}
        vid.playbackRate = 1.0;
      } else if (drift > 0.16) {
        vid.playbackRate = 1.04;
      } else if (drift < -0.16) {
        vid.playbackRate = 0.96;
      } else {
        vid.playbackRate = 1.0;
      }
    }, 400);
  }

  function playStage(ev){
    var url = String(ev.videoUrl || '');
    if (Number(ev.runId || 0) !== curRunId || !vid.getAttribute('src')) {
      prepareStage(ev);
    } else if (Array.isArray(ev.flashEvents)) {
      flashWindows = buildFlashWindows(ev.flashEvents);
    }
    clearPlayTimers();
    curRunId = Number(ev.runId || 0);
    activeStartMs = Number(ev.startAtMs || (Date.now() + clockOffsetMs));
    var totalSec = Math.max(5, Number(ev.totalDurationSec || 60));
    var waitMs = (activeStartMs - clockOffsetMs) - Date.now();
    var safetyMs = Math.max(8000, Math.round(waitMs + (totalSec + 5) * 1000));
    safetyTimer = setTimeout(stopStage, safetyMs);
    if (waitMs > 18 && waitMs < 10000) {
      startTimer = setTimeout(beginPlayback, waitMs);
    } else {
      beginPlayback();
    }
  }

  try {
    var es = new EventSource('/api/shows/events?role=' + encodeURIComponent(role));
    es.onmessage = function(msg){
      try {
        var d = JSON.parse(msg.data);
        if (!d || !d.type) return;
        if (d.type === 'stage_prepare') prepareStage(d);
        else if (d.type === 'stage_play') playStage(d);
        else if (d.type === 'stage_stop') stopStage();
      } catch(e){}
    };
  } catch(e){}
})();
</script>
"""

STAGE_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Fit · Live Show Stage</title>
<style>
html,body{margin:0;padding:0;width:100%;height:100%;background:#000;overflow:hidden}
#stVid{width:100%;height:100%;object-fit:contain;background:#000;display:block}
</style></head>
<body>
<video id="stVid" playsinline preload="auto"></video>
<div id="armoryStageFlash" style="pointer-events:none;position:fixed;inset:0;background:#fff;opacity:0;display:none;z-index:10"></div>
<script>
(function(){
  var q = new URLSearchParams(location.search);
  var role = (q.get('role') || 'cube').toLowerCase().replace(/[^a-z0-9_-]/g, '') || 'cube';
  var useAudio = (q.get('audio') === '1' || (q.get('muted') !== '1' && role === 'cube'));
  var vid = document.getElementById('stVid');
  var flashEl = document.getElementById('armoryStageFlash');
  vid.muted = !useAudio;
  vid.volume = useAudio ? 1.0 : 0.0;

  var clockOffsetMs = 0;
  var curRunId = 0;
  var readyAcked = false;
  var playingAcked = false;
  var startTimer = null;
  var safetyTimer = null;
  var syncIv = null;
  var flashRaf = null;
  var flashWindows = [];
  var activeStartMs = 0;

  function buildFlashWindows(list){
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length; i++){
      var item = list[i] || {};
      var at = Number(item.atSec || 0);
      var dur = Math.max(0.08, Number(item.durSec || 0.2));
      var gap = Math.max(0.08, Number(item.offSec || 0.22));
      var cnt = Math.max(1, Math.min(6, Number(item.count || 1)));
      for (var k = 0; k < cnt; k++){
        var t0 = at + k * (dur + gap);
        out.push({ t0: t0, t1: t0 + dur });
      }
    }
    return out;
  }

  function clearFlash(){
    if (flashRaf){ cancelAnimationFrame(flashRaf); flashRaf = null; }
    if (flashEl){ flashEl.style.opacity = '0'; flashEl.style.display = 'none'; }
  }

  function tickFlash(){
    if (!curRunId || vid.paused || vid.ended){
      clearFlash();
      return;
    }
    var t = vid.currentTime || 0;
    var lit = false;
    for (var i = 0; i < flashWindows.length; i++){
      if (t >= flashWindows[i].t0 && t < flashWindows[i].t1){ lit = true; break; }
    }
    if (flashEl){
      flashEl.style.display = lit ? 'block' : 'none';
      flashEl.style.opacity = lit ? '0.45' : '0';
    }
    flashRaf = requestAnimationFrame(tickFlash);
  }

  function syncClock(){
    var bestRtt = 999999;
    function sample(left){
      if (left <= 0) return;
      var t0 = Date.now();
      fetch('/api/shows/time?t=' + t0).then(function(r){ return r.json(); }).then(function(d){
        var t1 = Date.now(), rtt = t1 - t0;
        if (d && d.nowMs && rtt < bestRtt){
          bestRtt = rtt;
          clockOffsetMs = Number(d.nowMs) - (t0 + rtt * 0.5);
        }
        sample(left - 1);
      }).catch(function(){});
    }
    sample(3);
  }
  syncClock();
  setInterval(syncClock, 60000);

  function postAck(state, rid){
    var targetId = Number(rid || curRunId || 0);
    if (!targetId) return;
    fetch('/api/shows/stage/ack', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ role: role, runId: targetId, state: state })
    }).catch(function(){});
  }

  function clearPlayTimers(){
    if (startTimer){ clearTimeout(startTimer); startTimer = null; }
    if (safetyTimer){ clearTimeout(safetyTimer); safetyTimer = null; }
    if (syncIv){ clearInterval(syncIv); syncIv = null; }
    clearFlash();
  }

  function stopStage(){
    clearPlayTimers();
    flashWindows = [];
    curRunId = 0;
    readyAcked = false;
    playingAcked = false;
    activeStartMs = 0;
    vid.oncanplay = null;
    vid.onloadeddata = null;
    vid.onplaying = null;
    vid.onended = null;
    vid.onerror = null;
    try { vid.pause(); vid.playbackRate = 1.0; vid.removeAttribute('src'); vid.load(); } catch(e){}
  }

  function prepareStage(ev){
    clearPlayTimers();
    curRunId = Number(ev.runId || 0);
    readyAcked = false;
    playingAcked = false;
    activeStartMs = 0;
    flashWindows = buildFlashWindows(ev.flashEvents);
    vid.muted = !useAudio;
    vid.volume = useAudio ? 1.0 : 0.0;
    vid.playbackRate = 1.0;
    var url = String(ev.videoUrl || '');
    if (!url) return;
    var sendReady = function(){
      if (readyAcked || !curRunId) return;
      readyAcked = true;
      postAck('ready', curRunId);
    };
    var fallbackT = setTimeout(sendReady, 4000);
    vid.oncanplay = function(){ clearTimeout(fallbackT); sendReady(); };
    vid.onloadeddata = function(){ try { vid.currentTime = 0; } catch(e){} };
    vid.onplaying = function(){
      if (!playingAcked && curRunId) {
        playingAcked = true;
        postAck('playing', curRunId);
      }
    };
    vid.onended = function(){
      var rid = curRunId;
      stopStage();
      postAck('ended', rid);
    };
    try { vid.pause(); } catch(e){}
    vid.src = url;
    vid.load();
  }

  function beginPlayback(){
    if (!curRunId) return;
    vid.muted = !useAudio;
    vid.volume = useAudio ? 1.0 : 0.0;
    var expected = activeStartMs ? Math.max(0, (Date.now() + clockOffsetMs - activeStartMs) / 1000.0) : 0;
    try {
      if (Math.abs((vid.currentTime || 0) - expected) > 0.18) vid.currentTime = expected;
    } catch(e){}
    vid.playbackRate = 1.0;
    var p = vid.play();
    if (p && typeof p.catch === 'function') {
      p.catch(function(){
        if (!vid.muted) { vid.muted = true; vid.play().catch(function(){}); }
      });
    }
    if (flashWindows.length){
      clearFlash();
      flashRaf = requestAnimationFrame(tickFlash);
    }
    if (!useAudio) {
      if (syncIv) clearInterval(syncIv);
      syncIv = setInterval(function(){
        if (!activeStartMs || vid.paused || vid.ended) return;
        var exp = Math.max(0, (Date.now() + clockOffsetMs - activeStartMs) / 1000.0);
        var drift = exp - (vid.currentTime || 0);
        if (Math.abs(drift) >= 0.90) {
          try { vid.currentTime = exp; } catch(e){}
          vid.playbackRate = 1.0;
        } else if (drift > 0.16) {
          vid.playbackRate = 1.04;
        } else if (drift < -0.16) {
          vid.playbackRate = 0.96;
        } else {
          vid.playbackRate = 1.0;
        }
      }, 400);
    }
  }

  function playStage(ev){
    var url = String(ev.videoUrl || '');
    if (Number(ev.runId || 0) !== curRunId || !vid.getAttribute('src')) {
      prepareStage(ev);
    } else if (Array.isArray(ev.flashEvents)) {
      flashWindows = buildFlashWindows(ev.flashEvents);
    }
    clearPlayTimers();
    curRunId = Number(ev.runId || 0);
    activeStartMs = Number(ev.startAtMs || (Date.now() + clockOffsetMs));
    var totalSec = Math.max(5, Number(ev.totalDurationSec || 60));
    var waitMs = (activeStartMs - clockOffsetMs) - Date.now();
    var safetyMs = Math.max(8000, Math.round(waitMs + (totalSec + 5) * 1000));
    safetyTimer = setTimeout(stopStage, safetyMs);
    if (waitMs > 18 && waitMs < 10000) {
      startTimer = setTimeout(beginPlayback, waitMs);
    } else {
      beginPlayback();
    }
  }

  var es = new EventSource('/api/shows/events?role=' + encodeURIComponent(role));
  es.onmessage = function(msg){
    try {
      var d = JSON.parse(msg.data);
      if (!d || !d.type) return;
      if (d.type === 'stage_prepare') prepareStage(d);
      else if (d.type === 'stage_play') playStage(d);
      else if (d.type === 'stage_stop') stopStage();
    } catch(e){}
  };
})();
</script>
</body></html>"""


def inject_wallboard_stage_overlay(html: str) -> str:
    """Inject muted SSE show-stage overlay into Wallboard HTML without modifying Android APK or src/."""
    if not isinstance(html, str) or 'id="armoryShowStage"' in html:
        return html
    idx = html.lower().rfind("</body>")
    if idx >= 0:
        return html[:idx] + WALLBOARD_STAGE_OVERLAY_SNIPPET + html[idx:]
    return html + WALLBOARD_STAGE_OVERLAY_SNIPPET


DEFAULT_DRIVE_BACKUP_BASE = Path(r"J:\My Drive\Backups\ArmoryFit Backup")
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
CALIBRATION_MILESTONES = (5, 15, 50)
DEFAULT_NTFY_TOPIC = "FroriepAntigravity"


def send_ntfy_alert(topic: str, title: str, message: str, tags: str = "movie_camera,bell") -> bool:
    """Send a push notification via ntfy.sh (best-effort, non-blocking failure)."""
    clean = topic.strip()
    if not clean:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(clean)}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": tags, "Priority": "default"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def default_root_dir() -> Path:
    env = os.environ.get("ARMORY_CAM_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(r"S:\ArmoryCam")


def default_drive_backup_dir() -> str:
    env = os.environ.get("ARMORY_CAM_DRIVE_DIR", "").strip()
    if env:
        return env
    if DEFAULT_DRIVE_BACKUP_BASE.exists():
        return str(DEFAULT_DRIVE_BACKUP_BASE / "Clips")
    return ""


def read_google_drive_file_id(path: Path) -> str | None:
    """Read Windows Google Drive for Desktop `user.drive.id` NTFS stream, returning Drive file ID or None."""
    if os.name != "nt":
        return None
    try:
        with open(f"{path}:user.drive.id", "r", encoding="utf-8") as fh:
            fid = fh.read().strip()
        if fid and DRIVE_ID_RE.match(fid) and not fid.lower().startswith(("local", "tmp")):
            return fid
    except OSError:
        pass
    return None


def google_drive_view_url(file_id: str | None) -> str | None:
    if not file_id:
        return None
    return f"https://drive.google.com/file/d/{file_id}/view"


def resolve_tool_bin(tool_name: str, env_key: str) -> str:
    """Resolve `ffmpeg` / `ffprobe` binary from env, local portable Tools, or PATH."""
    env_val = os.environ.get(env_key, "").strip()
    if env_val and Path(env_val).exists():
        return env_val
    exe = f"{tool_name}.exe" if os.name == "nt" else tool_name
    candidates = [
        Path(r"C:\Users\davef\Tools\ffmpeg\bin") / exe,
        Path(os.path.expanduser("~")) / "Tools" / "ffmpeg" / "bin" / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(tool_name)
    return found or exe


def resolve_adb_bin() -> str | None:
    """Resolve `adb` binary from env, PATH, WinGet PlatformTools, or Android SDK."""
    env_val = os.environ.get("ARMORY_ADB", "").strip()
    if env_val and Path(env_val).exists():
        return env_val
    found = shutil.which("adb")
    if found:
        return found
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local_app:
        candidates.append(Path(local_app) / "Microsoft" / "WinGet" / "Links" / "adb.exe")
        winget_glob = glob.glob(
            str(Path(local_app) / "Microsoft" / "WinGet" / "Packages" / "Google.PlatformTools*" / "platform-tools" / "adb.exe")
        )
        candidates.extend(Path(p) for p in winget_glob)
        candidates.append(Path(local_app) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    candidates.append(Path(r"C:\Users\davef\Tools\platform-tools\adb.exe"))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def detect_lan_ip() -> str:
    """Best-effort LAN IPv4 lookup for building the static OptiPlex wallboard URL."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.1.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "192.168.1.152"


def extract_wallboard_html_from_ts(ts_path: Path) -> str | None:
    """Extract literal `WALLBOARD_HTML` string from `src/wallboard/page.ts` if present."""
    if not ts_path.is_file():
        return None
    try:
        raw = ts_path.read_text(encoding="utf-8")
        marker = "export const WALLBOARD_HTML = `"
        start = raw.find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = raw.rfind("`;")
        if end <= start:
            return None
        html = raw[start:end]
        return html if "<!doctype html" in html[:200].lower() else None
    except Exception:
        return None


def _subprocess_no_window_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


_WIN_JOB_LOCK = threading.Lock()
_WIN_JOB_HANDLE: int | None = None


def _ensure_windows_kill_on_close_job() -> int | None:
    """Create a process-wide Windows Job Object with KILL_ON_JOB_CLOSE so hard kills never orphan ffmpeg."""
    global _WIN_JOB_HANDLE
    if os.name != "nt":
        return None
    with _WIN_JOB_LOCK:
        if _WIN_JOB_HANDLE is not None:
            return _WIN_JOB_HANDLE or None
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            k32.SetInformationJobObject.restype = ctypes.c_int
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.AssignProcessToJobObject.restype = ctypes.c_int

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [("r", ctypes.c_uint64 * 6)]

            class _BASIC(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _EXTENDED(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BASIC),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            hjob = k32.CreateJobObjectW(None, None)
            if not hjob:
                _WIN_JOB_HANDLE = 0
                return None
            info = _EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(hjob, 9, ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                k32.CloseHandle(hjob)
                _WIN_JOB_HANDLE = 0
                return None
            _WIN_JOB_HANDLE = int(hjob)
            return _WIN_JOB_HANDLE
        except Exception:
            _WIN_JOB_HANDLE = 0
            return None


def _assign_child_to_job(proc: subprocess.Popen[Any]) -> None:
    if os.name != "nt" or proc is None:
        return
    hjob = _ensure_windows_kill_on_close_job()
    if not hjob:
        return
    handle = getattr(proc, "_handle", None)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.AssignProcessToJobObject(ctypes.c_void_p(hjob), ctypes.c_void_p(int(handle)))
    except Exception:
        pass


def parse_segment_start_ms(file_name: str) -> int | None:
    """Parse local wall-clock epoch ms from FFmpeg `-strftime 1` name `seg_YYYYMMDD_HHMMSS.ts`."""
    m = SEG_NAME_RE.match(file_name)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def safe_slug(raw: str, fallback: str = "clip") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip()).strip("._-")
    return cleaned[:48] or fallback


def normalize_crop(raw: Any) -> tuple[float, float, float, float]:
    """Clamp normalized `[x0, y0, x1, y1]` crop box to valid bounds with >= MIN_CROP_SPAN."""
    try:
        vals = [float(v) for v in raw]
        if len(vals) != 4 or any(not np.isfinite(v) for v in vals):
            return (0.0, 0.0, 1.0, 1.0)
        x0, y0, x1, y1 = vals
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0 = max(0.0, min(1.0 - MIN_CROP_SPAN, x0))
        y0 = max(0.0, min(1.0 - MIN_CROP_SPAN, y0))
        x1 = max(x0 + MIN_CROP_SPAN, min(1.0, x1))
        y1 = max(y0 + MIN_CROP_SPAN, min(1.0, y1))
        return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
    except Exception:
        return (0.0, 0.0, 1.0, 1.0)


def _tri_area(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return 0.5 * abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def normalize_excl_box(raw: Any) -> tuple[float, float, float, float] | None:
    """Clamp normalized `[x0, y0, x1, y1]` exclusion box with >= MIN_EXCL_SPAN, or None if invalid."""
    try:
        vals = [float(v) for v in raw]
        if len(vals) != 4 or any(not np.isfinite(v) for v in vals):
            return None
        x0, y0, x1, y1 = vals
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0 = max(0.0, min(1.0 - MIN_EXCL_SPAN, x0))
        y0 = max(0.0, min(1.0 - MIN_EXCL_SPAN, y0))
        x1 = max(x0 + MIN_EXCL_SPAN, min(1.0, x1))
        y1 = max(y0 + MIN_EXCL_SPAN, min(1.0, y1))
        return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
    except Exception:
        return None


def normalize_excl_quad(raw: Any) -> Quad | None:
    """Normalize a 4-point quad `[[x,y], ...]` or legacy `[x0,y0,x1,y1]` rect to clamped 4-point Quad."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        box = normalize_excl_box(raw)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))

    pts: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        px_raw, py_raw = item
        if (
            not isinstance(px_raw, (int, float))
            or isinstance(px_raw, bool)
            or not isinstance(py_raw, (int, float))
            or isinstance(py_raw, bool)
        ):
            return None
        px, py = float(px_raw), float(py_raw)
        if not np.isfinite(px) or not np.isfinite(py):
            return None
        pts.append((round(max(0.0, min(1.0, px)), 4), round(max(0.0, min(1.0, py)), 4)))

    p0, p1, p2, p3 = pts[0], pts[1], pts[2], pts[3]
    area = max(
        _tri_area(p0, p1, p2) + _tri_area(p0, p2, p3),
        _tri_area(p0, p1, p3) + _tri_area(p1, p2, p3),
    )
    if area < 1e-4:
        return None
    return (p0, p1, p2, p3)


def normalize_exclusions(raw: Any) -> list[Quad]:
    """Normalize up to MAX_EXCLUSIONS 4-point quad (or legacy rect) motion exclusion masks."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[Quad] = []
    for item in raw:
        quad = normalize_excl_quad(item)
        if quad is not None:
            out.append(quad)
            if len(out) >= MAX_EXCLUSIONS:
                break
    return out


def normalize_rotate(raw: Any) -> int:
    try:
        v = int(raw) % 360
        return v if v in (0, 90, 180, 270) else 0
    except Exception:
        return 0


def normalize_tilt(raw: Any) -> float:
    try:
        v = float(raw)
    except Exception:
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = max(-45.0, min(45.0, round(v, 1)))
    return 0.0 if abs(v) < 0.05 else v


def tilted_frame_size(w: int, h: int, tilt: float) -> tuple[int, int]:
    t = normalize_tilt(tilt)
    if t == 0.0:
        return w, h
    rad = math.radians(t)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    nw = max(2, int((w * ca + h * sa) // 2) * 2)
    nh = max(2, int((w * sa + h * ca) // 2) * 2)
    return nw, nh


_V360_REMAP_LOCK = threading.Lock()
_V360_REMAP_CACHE: OrderedDict[tuple[Any, ...], tuple[np.ndarray, np.ndarray]] = OrderedDict()


def _fisheye_pixel_to_ray(px: float, py: float, cx: float, cy: float, f_eq: float) -> np.ndarray:
    dx, dy = px - cx, py - cy
    r = math.hypot(dx, dy)
    if r < 1e-9:
        return np.array([0.0, 0.0, 1.0], dtype=np.float64)
    th = r / f_eq
    return np.array([math.sin(th) * (dx / r), math.sin(th) * (dy / r), math.cos(th)], dtype=np.float64)


def _fisheye_ray_to_pixel(ray: np.ndarray, cx: float, cy: float, f_eq: float) -> tuple[float, float]:
    rx, ry, rz = float(ray[0]), float(ray[1]), max(-1.0, min(1.0, float(ray[2])))
    th = math.acos(rz)
    rxy = math.hypot(rx, ry)
    if rxy < 1e-9:
        return cx - 0.5, cy - 0.5
    s = f_eq * th / rxy
    return cx - 0.5 + rx * s, cy - 0.5 + ry * s


def compute_v360_params(
    src_w: int,
    src_h: int,
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    rotate: int = 0,
    tilt: float = 0.0,
) -> dict[str, Any]:
    """Compute Option 2 Virtual PTZ (`v360 fisheye -> flat`) parameters from normalized crop & tilt."""
    rot = normalize_rotate(rotate)
    rw, rh = (int(src_h), int(src_w)) if rot in (90, 270) else (int(src_w), int(src_h))
    rw = max(16, rw)
    rh = max(16, rh)
    ref_diag = math.hypot(WYZE_V3_REF_W, WYZE_V3_REF_H)
    f_eq = WYZE_V3_F_EQ * (math.hypot(rw, rh) / ref_diag)
    cx, cy = rw * 0.5, rh * 0.5
    ih_fov = 2.0 * math.degrees(cx / f_eq)
    iv_fov = 2.0 * math.degrees(cy / f_eq)

    tlt = normalize_tilt(tilt)
    M, fw, fh = view_transform_matrix(rw, rh, rotate=0, tilt=tlt)
    M_inv = np.linalg.inv(M)
    x0, y0, x1, y1 = normalize_crop(crop)

    def _to_raw(fx: float, fy: float) -> tuple[float, float]:
        pt = M_inv @ np.array([fx, fy, 1.0], dtype=np.float64)
        return float(pt[0]), float(pt[1])

    mcx, mcy = 0.5 * (x0 + x1) * fw, 0.5 * (y0 + y1) * fh
    rc = _fisheye_pixel_to_ray(*_to_raw(mcx, mcy), cx, cy, f_eq)
    yaw_deg = math.degrees(math.atan2(float(rc[0]), float(rc[2])))
    pitch_deg = -math.degrees(math.asin(max(-1.0, min(1.0, float(rc[1])))))
    roll_deg = -tlt

    yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
    Ry = np.array([[math.cos(yr), 0.0, math.sin(yr)], [0.0, 1.0, 0.0], [-math.sin(yr), 0.0, math.cos(yr)]])
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(pr), -math.sin(pr)], [0.0, math.sin(pr), math.cos(pr)]])
    R = Ry @ Rx

    edge_pts = [_to_raw(mcx, y0 * fh), _to_raw(mcx, y1 * fh), _to_raw(x0 * fw, mcy), _to_raw(x1 * fw, mcy)]
    uvs: list[tuple[float, float]] = []
    for px, py in edge_pts:
        rv = R.T @ _fisheye_pixel_to_ray(px, py, cx, cy, f_eq)
        rz = max(0.05, float(rv[2]))
        uvs.append((float(rv[0]) / rz, float(rv[1]) / rz))
    v_half = max(0.05, 0.5 * (abs(uvs[0][1]) + abs(uvs[1][1])))
    u_half = max(0.05, 0.5 * (abs(uvs[2][0]) + abs(uvs[3][0])))

    aspect = max(0.2, min(5.0, u_half / v_half))
    raw_cw = max(16.0, (x1 - x0) * fw)
    out_w = max(16, int(round(raw_cw / 2.0)) * 2)
    out_h = max(16, int(round((out_w / aspect) / 2.0)) * 2)
    v_half_eq = u_half * (out_h / out_w)

    rr = math.radians(roll_deg)
    Rz = np.array([[math.cos(rr), -math.sin(rr), 0.0], [math.sin(rr), math.cos(rr), 0.0], [0.0, 0.0, 1.0]])
    R_full = Ry @ Rx @ Rz

    pad_px = min(1.5, rw * 0.01)
    scale_fov = 1.0
    for _ in range(15):
        uh = u_half * scale_fov
        vh = v_half_eq * scale_fov
        xs_b = np.linspace(-uh, uh, 21)
        ys_b = np.linspace(-vh, vh, 21)
        border = (
            [(u, -vh, 1.0) for u in xs_b]
            + [(u, vh, 1.0) for u in xs_b]
            + [(-uh, v, 1.0) for v in ys_b]
            + [(uh, v, 1.0) for v in ys_b]
        )
        pxs = [_fisheye_ray_to_pixel(R_full @ (np.array(b) / math.sqrt(b[0] * b[0] + b[1] * b[1] + 1.0)), cx, cy, f_eq) for b in border]
        min_x = min(p[0] for p in pxs)
        max_x = max(p[0] for p in pxs)
        min_y = min(p[1] for p in pxs)
        max_y = max(p[1] for p in pxs)
        if min_x >= pad_px and max_x <= rw - 1.0 - pad_px and min_y >= pad_px and max_y <= rh - 1.0 - pad_px:
            break
        scale_fov *= 0.985

    u_half *= scale_fov
    v_half_eq = u_half * (out_h / out_w)
    return {
        "rw": rw,
        "rh": rh,
        "cx": cx,
        "cy": cy,
        "f_eq": f_eq,
        "ih_fov": ih_fov,
        "iv_fov": iv_fov,
        "out_w": out_w,
        "out_h": out_h,
        "u_half": u_half,
        "v_half_eq": v_half_eq,
        "hfov_deg": 2.0 * math.degrees(math.atan(u_half)),
        "vfov_deg": 2.0 * math.degrees(math.atan(v_half_eq)),
        "yaw_deg": yaw_deg,
        "pitch_deg": pitch_deg,
        "roll_deg": roll_deg,
        "R_full": R_full,
    }


def _get_v360_remap_maps(
    rw: int,
    rh: int,
    crop: tuple[float, float, float, float],
    tilt: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    x0, y0, x1, y1 = normalize_crop(crop)
    tlt = normalize_tilt(tilt)
    key = (int(rw), int(rh), round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4), tlt)
    with _V360_REMAP_LOCK:
        hit = _V360_REMAP_CACHE.get(key)
        if hit is not None:
            _V360_REMAP_CACHE.move_to_end(key)
            return hit

    p = compute_v360_params(rw, rh, crop=(x0, y0, x1, y1), rotate=0, tilt=tlt)
    ow, oh = int(p["out_w"]), int(p["out_h"])
    cx, cy, f_eq = float(p["cx"]), float(p["cy"]), float(p["f_eq"])
    uh, vh = float(p["u_half"]), float(p["v_half_eq"])
    R_full = p["R_full"]

    ys, xs = np.mgrid[0:oh, 0:ow].astype(np.float64)
    nx = ((xs + 0.5) / ow) * 2.0 - 1.0
    ny = ((ys + 0.5) / oh) * 2.0 - 1.0
    vx = nx * uh
    vy = ny * vh
    vz = np.ones_like(vx)
    inv_norm = 1.0 / np.sqrt(vx * vx + vy * vy + vz * vz)
    rays = np.stack([vx * inv_norm, vy * inv_norm, vz * inv_norm], axis=-1) @ R_full.T
    rx, ry, rz = rays[..., 0], rays[..., 1], np.clip(rays[..., 2], -1.0, 1.0)
    th = np.arccos(rz)
    rxy = np.hypot(rx, ry)
    scale = np.divide(f_eq * th, rxy, out=np.zeros_like(rxy), where=rxy > 1e-9)
    map_x = (cx - 0.5 + rx * scale).astype(np.float32)
    map_y = (cy - 0.5 + ry * scale).astype(np.float32)
    map1, map2 = cv2.convertMaps(map_x, map_y, cv2.CV_16SC2)

    with _V360_REMAP_LOCK:
        _V360_REMAP_CACHE[key] = (map1, map2)
        _V360_REMAP_CACHE.move_to_end(key)
        while len(_V360_REMAP_CACHE) > 32:
            _V360_REMAP_CACHE.popitem(last=False)
    return map1, map2


def build_ffmpeg_vf(
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    rotate: int = 0,
    scale_w: int | None = None,
    fps: int | None = None,
    tilt: float = 0.0,
    defish: bool = False,
    src_size: tuple[int, int] = (WYZE_V3_REF_W, WYZE_V3_REF_H),
    fast_defish: bool = False,
) -> str | None:
    """Build ordered FFmpeg `-vf` chain (rotate -> v360 PTZ or tilt+crop -> optional fps/scale)."""
    parts: list[str] = []
    rot = normalize_rotate(rotate)
    if rot == 90:
        parts.append("transpose=1")
    elif rot == 180:
        parts.append("hflip,vflip")
    elif rot == 270:
        parts.append("transpose=2")

    tlt = normalize_tilt(tilt)
    norm_crop = normalize_crop(crop)
    if defish:
        sw = max(16, int(src_size[0] or WYZE_V3_REF_W))
        sh = max(16, int(src_size[1] or WYZE_V3_REF_H))
        p = compute_v360_params(sw, sh, crop=norm_crop, rotate=rot, tilt=tlt)
        interp_mode = "linear" if fast_defish else "cubic"
        parts.append(
            f"v360=input=fisheye:ih_fov={p['ih_fov']:.4f}:iv_fov={p['iv_fov']:.4f}:output=flat:"
            f"w={p['out_w']}:h={p['out_h']}:h_fov={p['hfov_deg']:.2f}:v_fov={p['vfov_deg']:.2f}:"
            f"yaw={p['yaw_deg']:.2f}:pitch={p['pitch_deg']:.2f}:roll={p['roll_deg']:.2f}:interp={interp_mode}"
        )
    else:
        if tlt != 0.0:
            rad = math.radians(tlt)
            parts.append(
                f"rotate={rad:.6f}:"
                f"ow=max(2\\,trunc(rotw({rad:.6f})/2)*2):"
                f"oh=max(2\\,trunc(roth({rad:.6f})/2)*2):"
                "fillcolor=black"
            )

        x0, y0, x1, y1 = norm_crop
        if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
            w = max(MIN_CROP_SPAN, x1 - x0)
            h = max(MIN_CROP_SPAN, y1 - y0)
            parts.append(
                f"crop=max(2\\,trunc(iw*{w:.6f}/2)*2):max(2\\,trunc(ih*{h:.6f}/2)*2):"
                f"trunc(iw*{x0:.6f}/2)*2:trunc(ih*{y0:.6f}/2)*2"
            )

    if fps and int(fps) > 0:
        parts.append(f"fps={int(fps)}")
    if scale_w and int(scale_w) > 0:
        parts.append(f"scale={int(scale_w)}:-2")
    return ",".join(parts) if parts else None


def apply_frame_transform(
    frame: np.ndarray,
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    rotate: int = 0,
    apply_crop: bool = True,
    tilt: float = 0.0,
    defish: bool = False,
) -> np.ndarray:
    """Apply OpenCV rotation + Option 2 v360 Virtual PTZ defish (or affine tilt + crop) matching `build_ffmpeg_vf`."""
    if frame is None or frame.size == 0:
        return frame
    out = frame
    rot = normalize_rotate(rotate)
    if rot == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif rot == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)

    if defish and apply_crop:
        rh, rw = out.shape[:2]
        map1, map2 = _get_v360_remap_maps(rw, rh, normalize_crop(crop), normalize_tilt(tilt))
        return cv2.remap(out, map1, map2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))

    tlt = normalize_tilt(tilt)
    if tlt != 0.0:
        h0, w0 = out.shape[:2]
        nw, nh = tilted_frame_size(w0, h0, tlt)
        mat = cv2.getRotationMatrix2D((w0 / 2.0, h0 / 2.0), -tlt, 1.0)
        mat[0, 2] += (nw - w0) / 2.0
        mat[1, 2] += (nh - h0) / 2.0
        out = cv2.warpAffine(out, mat, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))

    if apply_crop:
        x0, y0, x1, y1 = normalize_crop(crop)
        if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
            h, w = out.shape[:2]
            span_w = max(MIN_CROP_SPAN, x1 - x0)
            span_h = max(MIN_CROP_SPAN, y1 - y0)
            cw = max(2, min(w, int((w * span_w) // 2) * 2))
            ch = max(2, min(h, int((h * span_h) // 2) * 2))
            cx0 = max(0, min(w - cw, int((w * x0) // 2) * 2))
            cy0 = max(0, min(h - ch, int((h * y0) // 2) * 2))
            out = out[cy0 : cy0 + ch, cx0 : cx0 + cw]
    return out


def view_transform_matrix(
    w: int,
    h: int,
    rotate: int = 0,
    tilt: float = 0.0,
) -> tuple[np.ndarray, int, int]:
    """Return 3x3 affine matrix `(M, fw, fh)` matching `apply_frame_transform` from raw `(w, h)` pixels."""
    rot = normalize_rotate(rotate)
    if rot == 90:
        mat = np.array([[0.0, -1.0, float(h)], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
        rw, rh = h, w
    elif rot == 180:
        mat = np.array([[-1.0, 0.0, float(w)], [0.0, -1.0, float(h)], [0.0, 0.0, 1.0]], dtype=np.float64)
        rw, rh = w, h
    elif rot == 270:
        mat = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, float(w)], [0.0, 0.0, 1.0]], dtype=np.float64)
        rw, rh = h, w
    else:
        mat = np.eye(3, dtype=np.float64)
        rw, rh = w, h

    tlt = normalize_tilt(tilt)
    if tlt != 0.0:
        nw, nh = tilted_frame_size(rw, rh, tlt)
        r2 = cv2.getRotationMatrix2D((rw / 2.0, rh / 2.0), -tlt, 1.0)
        r2[0, 2] += (nw - rw) / 2.0
        r2[1, 2] += (nh - rh) / 2.0
        mat = np.vstack([r2, [0.0, 0.0, 1.0]]) @ mat
        rw, rh = nw, nh
    return mat, rw, rh


_HANN_FULL = cv2.createHanningWindow(BUMP_REF_SIZE, cv2.CV_32F)
_HANN_HALF = cv2.createHanningWindow((BUMP_REF_SIZE[0] // 2, BUMP_REF_SIZE[1]), cv2.CV_32F)
_HANN_LIVE_FULL = cv2.createHanningWindow(BUMP_LIVE_SIZE, cv2.CV_32F)
_HANN_LIVE_HALF = cv2.createHanningWindow((BUMP_LIVE_SIZE[0] // 2, BUMP_LIVE_SIZE[1]), cv2.CV_32F)
_OVERLAY_COLORS_BGR: dict[str, tuple[int, int, int]] = {
    "cyan": (255, 238, 56),
    "lime": (105, 255, 92),
    "magenta": (245, 90, 255),
    "amber": (45, 200, 255),
}


def _bump_edge_map(gray_small: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray_small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_small, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return mag - float(np.mean(mag))


_CLAHE_ALIGN = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))


def estimate_rigid_alignment_orb(
    ref_gray: np.ndarray,
    cur_bgr: np.ndarray,
    *,
    work_size: tuple[int, int] = BUMP_LIVE_SIZE,
    nfeatures: int = 850,
    ref_width: float | None = None,
) -> dict[str, Any] | None:
    """Rotation- & occlusion-tolerant CLAHE+ORB+RANSAC rigid alignment (~18ms @ 640x360, ~60ms @ 960x540)."""
    if ref_gray is None or ref_gray.size == 0 or cur_bgr is None or cur_bgr.size == 0:
        return None
    ww, wh = int(work_size[0]), int(work_size[1])
    g_ref = cv2.cvtColor(ref_gray, cv2.COLOR_BGR2GRAY) if ref_gray.ndim == 3 else ref_gray
    g_cur = cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2GRAY) if cur_bgr.ndim == 3 else cur_bgr
    if g_ref.shape != (wh, ww):
        g_ref = cv2.resize(g_ref, (ww, wh), interpolation=cv2.INTER_AREA)
    if g_cur.shape != (wh, ww):
        g_cur = cv2.resize(g_cur, (ww, wh), interpolation=cv2.INTER_AREA)
    if float(np.std(g_cur)) < 6.0 or float(np.std(g_ref)) < 6.0:
        return None

    r_eq = _CLAHE_ALIGN.apply(g_ref)
    c_eq = _CLAHE_ALIGN.apply(g_cur)
    mask = np.full((wh, ww), 255, dtype=np.uint8)
    mask[: max(24, int(round(wh * 0.075))), : max(140, int(round(ww * 0.27)))] = 0

    orb = cv2.ORB_create(nfeatures=int(nfeatures), fastThreshold=12)
    kp1, des1 = orb.detectAndCompute(r_eq, mask)
    kp2, des2 = orb.detectAndCompute(c_eq, mask)
    if des1 is None or des2 is None or len(kp1) < 12 or len(kp2) < 12:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = bf.knnMatch(des1, des2, k=2)
    good = [pair[0] for pair in knn if len(pair) == 2 and pair[0].distance < 0.78 * pair[1].distance]
    if len(good) < 8:
        return None

    pts1 = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    pts2 = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    mat, inliers = cv2.estimateAffinePartial2D(
        pts1,
        pts2,
        method=cv2.RANSAC,
        ransacReprojThreshold=max(3.0, ww / 240.0),
        maxIters=2000,
    )
    if mat is None or inliers is None:
        return None
    n_inliers = int(inliers.sum())
    zoom = math.hypot(float(mat[0, 0]), float(mat[1, 0]))
    if n_inliers < 10 or not (0.93 <= zoom <= 1.07):
        return None

    cx, cy = ww / 2.0, wh / 2.0
    nx = float(mat[0, 0]) * cx + float(mat[0, 1]) * cy + float(mat[0, 2])
    ny = float(mat[1, 0]) * cx + float(mat[1, 1]) * cy + float(mat[1, 2])
    target_w = float(ref_width) if (ref_width is not None and ref_width > 0) else float(cur_bgr.shape[1])
    scale = target_w / float(ww)
    dx_px = (nx - cx) * scale
    dy_px = (ny - cy) * scale
    shift_px = math.hypot(dx_px, dy_px)
    d_angle = math.degrees(math.atan2(float(mat[1, 0]), float(mat[0, 0])))
    # Normalize matrix to BUMP_OVERLAY_SIZE (960x540) pixel coords for downstream view adaptation
    sx = BUMP_OVERLAY_SIZE[0] / float(ww)
    sy = BUMP_OVERLAY_SIZE[1] / float(wh)
    mat_960 = np.array(
        [
            [float(mat[0, 0]), float(mat[0, 1]) * (sx / sy), float(mat[0, 2]) * sx],
            [float(mat[1, 0]) * (sy / sx), float(mat[1, 1]), float(mat[1, 2]) * sy],
        ],
        dtype=np.float64,
    )
    # Map inlier strength to an intuitive 0.15..0.99 match confidence score
    resp = min(0.99, 0.18 + 0.81 * min(1.0, n_inliers / 160.0))
    return {
        "ok": True,
        "method": "orb",
        "dxPx": round(dx_px, 1),
        "dyPx": round(dy_px, 1),
        "shiftPx": round(shift_px, 1),
        "dAngle": round(d_angle, 2),
        "response": round(resp, 3),
        "inliers": n_inliers,
        "goodMatches": len(good),
        "zoom": round(zoom, 4),
        "matrix960": mat_960,
    }


def detect_camera_bump(
    ref_gray_small: np.ndarray,
    cur_bgr: np.ndarray,
    *,
    high_res: bool = False,
    ref_width: float | None = None,
) -> dict[str, Any]:
    """Camera bump & live realign estimator: 1.2ms phaseCorrelate in bg; rotation-proof ORB+RANSAC + 320p fallback in live mode."""
    if ref_gray_small is None or ref_gray_small.size == 0 or cur_bgr is None or cur_bgr.size == 0:
        return {
            "ok": False,
            "bumped": False,
            "dxPx": 0.0,
            "dyPx": 0.0,
            "shiftPx": 0.0,
            "dAngle": 0.0,
            "response": 0.0,
            "confident": False,
            "aligned": False,
            "close": False,
        }

    if high_res:
        orb_res = estimate_rigid_alignment_orb(
            ref_gray_small,
            cur_bgr,
            work_size=BUMP_LIVE_SIZE,
            nfeatures=850,
            ref_width=ref_width,
        )
        if orb_res is not None:
            shift_px = float(orb_res["shiftPx"])
            d_angle = float(orb_res["dAngle"])
            bumped = bool(shift_px >= BUMP_PX_THRESHOLD or abs(d_angle) >= BUMP_DEG_THRESHOLD)
            aligned = bool(shift_px <= BUMP_ALIGNED_PX and abs(d_angle) <= BUMP_ALIGNED_DEG)
            close = bool((not aligned) and shift_px <= BUMP_CLOSE_PX and abs(d_angle) <= BUMP_CLOSE_DEG)
            return {
                "ok": True,
                "method": "orb",
                "bumped": bumped,
                "dxPx": orb_res["dxPx"],
                "dyPx": orb_res["dyPx"],
                "shiftPx": orb_res["shiftPx"],
                "dAngle": orb_res["dAngle"],
                "response": orb_res["response"],
                "inliers": orb_res["inliers"],
                "confident": True,
                "aligned": aligned,
                "close": close,
            }

    # Always run phase correlation at robust 320x180 (coarse edges tolerate 2-3 deg roll & cross-lighting far better than 640x360)
    work_size = BUMP_REF_SIZE
    if ref_gray_small.shape != (work_size[1], work_size[0]):
        ref_small = cv2.resize(ref_gray_small, work_size, interpolation=cv2.INTER_AREA)
    else:
        ref_small = ref_gray_small
    cur_gray = cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2GRAY) if cur_bgr.ndim == 3 else cur_bgr
    cur_small = cv2.resize(cur_gray, work_size, interpolation=cv2.INTER_AREA)

    if float(np.std(cur_small)) < 6.0 or float(np.std(ref_small)) < 6.0:
        return {
            "ok": False,
            "bumped": False,
            "dxPx": 0.0,
            "dyPx": 0.0,
            "shiftPx": 0.0,
            "dAngle": 0.0,
            "response": 0.0,
            "confident": False,
            "aligned": False,
            "close": False,
            "reason": "low_contrast",
        }

    e_ref = _bump_edge_map(ref_small)
    e_cur = _bump_edge_map(cur_small)
    (dx_s, dy_s), resp = cv2.phaseCorrelate(e_ref, e_cur, _HANN_FULL)
    target_w = float(ref_width) if (ref_width is not None and ref_width > 0) else float(cur_bgr.shape[1])
    scale = max(target_w / float(work_size[0]), 1.0)
    dx_px = float(dx_s) * scale
    dy_px = float(dy_s) * scale
    shift_px = math.hypot(dx_px, dy_px)

    half_w = work_size[0] // 2
    (_, dy_l), resp_l = cv2.phaseCorrelate(e_ref[:, :half_w], e_cur[:, :half_w], _HANN_HALF)
    (_, dy_r), resp_r = cv2.phaseCorrelate(e_ref[:, half_w:], e_cur[:, half_w:], _HANN_HALF)
    d_angle = (
        math.degrees(math.atan2(float(dy_r - dy_l), float(half_w)))
        if (resp_l >= 0.10 and resp_r >= 0.10)
        else 0.0
    )
    confident = bool(resp >= 0.10)
    bumped = bool(resp >= 0.10 and (shift_px >= BUMP_PX_THRESHOLD or abs(d_angle) >= BUMP_DEG_THRESHOLD))
    aligned = bool(confident and shift_px <= BUMP_ALIGNED_PX and abs(d_angle) <= BUMP_ALIGNED_DEG)
    close = bool(confident and not aligned and shift_px <= BUMP_CLOSE_PX and abs(d_angle) <= BUMP_CLOSE_DEG)
    return {
        "ok": True,
        "method": "phase",
        "bumped": bumped,
        "dxPx": round(dx_px, 1),
        "dyPx": round(dy_px, 1),
        "shiftPx": round(shift_px, 1),
        "dAngle": round(d_angle, 2),
        "response": round(float(resp), 3),
        "confident": confident,
        "aligned": aligned,
        "close": close,
    }


def render_bump_overlay_png(ref_gray: np.ndarray, mode: str = "edges", color: str = "cyan") -> bytes:
    """Render a 960x540 transparent landmark wireframe or grayscale ghost PNG from a saved bump baseline."""
    if ref_gray.ndim == 3:
        gray = cv2.cvtColor(ref_gray, cv2.COLOR_BGR2GRAY)
    else:
        gray = ref_gray
    if gray.shape != (BUMP_OVERLAY_SIZE[1], BUMP_OVERLAY_SIZE[0]):
        interp = cv2.INTER_CUBIC if gray.shape[1] < BUMP_OVERLAY_SIZE[0] else cv2.INTER_AREA
        gray = cv2.resize(gray, BUMP_OVERLAY_SIZE, interpolation=interp)

    if mode == "ghost":
        ok, buf = cv2.imencode(".png", gray)
        return buf.tobytes() if ok else b""

    # Edge-preserving smooth kills wall texture/grain while keeping bold structural landmarks
    smooth = cv2.bilateralFilter(gray, d=7, sigmaColor=42, sigmaSpace=42)
    edges = cv2.Canny(smooth, 75, 175)
    # Mask top-left timestamp OSD box so changing clock digits don't clutter landmarks
    edges[:38, :255] = 0
    # Drop tiny speckle fragments (< 28 px) so only clean architectural/equipment outlines remain
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(edges, connectivity=8)
    clean = np.zeros_like(edges)
    for lbl in range(1, n_lbl):
        if stats[lbl, cv2.CC_STAT_AREA] >= 28:
            clean[labels == lbl] = 255
    if int(clean.any()) == 0:
        clean = edges

    k_halo = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    halo = cv2.dilate(clean, k_halo, iterations=1)
    rgba = np.zeros((BUMP_OVERLAY_SIZE[1], BUMP_OVERLAY_SIZE[0], 4), dtype=np.uint8)
    halo_m = halo > 0
    rgba[halo_m] = (0, 0, 0, 185)
    bgr = _OVERLAY_COLORS_BGR.get(color, _OVERLAY_COLORS_BGR["cyan"])
    edge_m = clean > 0
    rgba[edge_m] = (bgr[0], bgr[1], bgr[2], 255)
    ok, buf = cv2.imencode(".png", rgba)
    return buf.tobytes() if ok else b""


def compute_motion_energy_from_frames(
    frames: list[np.ndarray],
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    exclusions: Sequence[Any] = (),
    prev_frame: np.ndarray | None = None,
) -> float:
    """Compute motion score v2 (0..100) across sampled BGR frames + optional prev_frame inside ROI minus exclusion quads."""
    all_frames = ([prev_frame] + list(frames)) if prev_frame is not None else list(frames)
    if len(all_frames) < 2:
        return 0.0
    x0, y0, x1, y1 = normalize_crop(roi)
    excl_list = normalize_exclusions(list(exclusions)) if exclusions else []

    prep: list[np.ndarray] = []
    include_mask: np.ndarray | None = None
    for frm in all_frames:
        if frm is None or frm.size == 0:
            continue
        small = cv2.resize(frm, (240, 135), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        cx0, cx1 = int(round(x0 * w)), int(round(x1 * w))
        cy0, cy1 = int(round(y0 * h)), int(round(y1 * h))
        rx0, ry0 = max(0, cx0), max(0, cy0)
        rx1, ry1 = min(w, max(rx0 + 2, cx1)), min(h, max(ry0 + 2, cy1))
        crop = small[ry0:ry1, rx0:rx1]
        if include_mask is None and excl_list:
            mask_u8 = np.ones((crop.shape[0], crop.shape[1]), dtype=np.uint8)
            shift = 4
            scale = 1 << shift
            for quad in excl_list:
                pts = np.array(
                    [
                        [int(round((px * w - rx0) * scale)), int(round((py * h - ry0) * scale))]
                        for px, py in quad
                    ],
                    dtype=np.int32,
                )
                cv2.fillPoly(mask_u8, [pts], 0, shift=shift)
            include_mask = mask_u8.astype(bool)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        prep.append(cv2.GaussianBlur(gray, (3, 3), 0))

    if len(prep) < 2:
        return 0.0
    if include_mask is not None and not np.any(include_mask):
        return 0.0

    pairs: list[tuple[int, int]] = [(i, i + 1) for i in range(len(prep) - 1)]
    seg_start = 1 if prev_frame is not None else 0
    if len(prep) - seg_start >= 3:
        pairs.append((seg_start, len(prep) - 1))

    best = 0.0
    for idx_a, idx_b in pairs:
        diff = cv2.absdiff(prep[idx_a], prep[idx_b])
        vals = diff[include_mask] if include_mask is not None else diff
        mean_diff = float(np.mean(vals))
        active10 = float(np.mean(vals > 10)) * 100.0
        p98 = float(np.percentile(vals, 98))
        p98_bonus = max(0.0, p98 - 6.0) * 0.12
        score = round(0.65 * mean_diff + 0.35 * active10 + p98_bonus, 3)
        if score > best:
            best = score
    return min(100.0, best)


def extract_ts_sampled_frames_with_head(
    seg_path: Path, fallback_dur_ms: int = 2000, max_w: int = 480
) -> tuple[int, list[np.ndarray], np.ndarray | None]:
    """Read up to 4 downscaled BGR frames, duration (ms), and unshrunk opening frame in one sequential pass."""
    cap = cv2.VideoCapture(str(seg_path))
    if not cap.isOpened():
        return fallback_dur_ms, [], None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not np.isfinite(fps) or fps <= 1.0 or fps > 120.0:
            fps = 20.0

        def _shrink(frm: np.ndarray) -> np.ndarray:
            if max_w > 0 and frm.shape[1] > max_w:
                h2 = max(2, int(round(frm.shape[0] * (float(max_w) / float(frm.shape[1])))))
                return cv2.resize(frm, (max_w, h2), interpolation=cv2.INTER_AREA)
            return frm

        frames: list[np.ndarray] = []
        first_full: np.ndarray | None = None
        if total >= 4:
            target_set = {0, total // 3, (2 * total) // 3, total - 1}
            for i in range(total):
                if not cap.grab():
                    break
                if i in target_set:
                    ok, frm = cap.retrieve()
                    if ok and frm is not None:
                        if first_full is None:
                            first_full = frm
                        frames.append(_shrink(frm))
        else:
            # No reliable frame count: sample sequentially up to 4 frames spread across the file
            all_read: list[np.ndarray] = []
            while len(all_read) < 120:
                ok, frm = cap.read()
                if not ok or frm is None:
                    break
                all_read.append(frm)
            if all_read:
                first_full = all_read[0]
            if len(all_read) >= 4:
                n = len(all_read)
                for idx in sorted({0, n // 3, (2 * n) // 3, n - 1}):
                    frames.append(_shrink(all_read[idx]))
            else:
                frames = [_shrink(f) for f in all_read]

        dur_ms = int(round((total / fps) * 1000)) if total >= 2 else fallback_dur_ms
        if dur_ms < 400 or dur_ms > 20_000:
            dur_ms = fallback_dur_ms
        return dur_ms, frames, first_full
    finally:
        cap.release()


def extract_ts_sampled_frames(seg_path: Path, fallback_dur_ms: int = 2000, max_w: int = 480) -> tuple[int, list[np.ndarray]]:
    """Read up to 4 downscaled BGR frames and duration (ms) from an MPEG-TS segment in one sequential pass."""
    dur_ms, frames, _ = extract_ts_sampled_frames_with_head(seg_path, fallback_dur_ms=fallback_dur_ms, max_w=max_w)
    return dur_ms, frames


def analyze_ts_segment(
    seg_path: Path,
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    exclusions: Sequence[Any] = (),
    rotate: int = 0,
    fallback_dur_ms: int = 2000,
    tilt: float = 0.0,
) -> tuple[int, float]:
    """Read sampled frames from an MPEG-TS segment to get `(duration_ms, motion_energy)`."""
    dur_ms, raw_frames = extract_ts_sampled_frames(seg_path, fallback_dur_ms=fallback_dur_ms)
    if not raw_frames:
        return dur_ms, 0.0
    rot = normalize_rotate(rotate)
    tlt = normalize_tilt(tilt)
    frames = [apply_frame_transform(frm, rotate=rot, tilt=tlt, apply_crop=False) for frm in raw_frames]
    score = compute_motion_energy_from_frames(frames, roi=roi, exclusions=exclusions)
    return dur_ms, score


def encode_thumb_data_url(frame: np.ndarray, max_w: int = 200, quality: int = 65) -> str:
    if frame is None or frame.size == 0:
        return ""
    h, w = frame.shape[:2]
    tw = min(max_w, max(1, w))
    th = max(1, int(round(h * (tw / max(1, w)))))
    resized = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def extract_video_thumb(video_path: Path) -> str:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return ""
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(total // 2, 30))
        ok, frm = cap.read()
        if not ok or frm is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frm = cap.read()
        return encode_thumb_data_url(frm) if ok and frm is not None else ""
    finally:
        cap.release()


def measure_rolling_led_banding(video_path: Path, max_frames: int = 75) -> dict[str, float]:
    """Scan a short middle slice of an MP4/TS clip for 5 Hz US-AC LED rolling-shutter banding.

    Uses per-scanline horizontal medians (ignoring localized lifters or TVs) and temporal FFT
    around the 5 Hz beat frequency (|120 Hz - 5*25 fps| = 5.0 Hz). Fast (~60 ms for 75 frames).
    """
    empty = {"frames": 0.0, "fps": 0.0, "fPeakHz": 0.0, "amp5Med": 0.0, "ledP2p": 0.0}
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return empty
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        if not (12.0 <= fps <= 120.0):
            fps = 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > max_frames + 4:
            cap.set(cv2.CAP_PROP_POS_FRAMES, (total - max_frames) // 2)
        rows: list[np.ndarray] = []
        while len(rows) < max_frames:
            ok, frm = cap.read()
            if not ok or frm is None:
                break
            small = cv2.resize(frm, (160, 90), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            rows.append(np.median(gray, axis=1))
    finally:
        cap.release()

    n = len(rows)
    if n < 25:
        return {**empty, "frames": float(n), "fps": round(fps, 3)}
    mat = np.stack(rows, axis=0)  # (N, 90)
    dev = mat - np.median(mat, axis=0, keepdims=True)
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    spec = np.abs(np.fft.rfft(dev, axis=0)) * (2.0 / n)
    idx5 = np.where((freqs >= 4.2) & (freqs <= 5.8))[0]
    if len(idx5) == 0:
        return {**empty, "frames": float(n), "fps": round(fps, 3)}
    bin_energy = spec[idx5].mean(axis=1)
    best_idx = int(idx5[int(np.argmax(bin_energy))])
    f_peak = float(freqs[best_idx])
    amp5_med = float(np.median(spec[best_idx]))
    t = np.arange(n, dtype=np.float32)
    ph = np.exp(-2j * np.pi * f_peak * t / fps)[:, None]
    c5 = (2.0 / n) * (dev * ph).sum(axis=0)
    wave = np.real(c5[None, :] * np.exp(2j * np.pi * f_peak * t[:, None] / fps))
    led_p2p = float(np.percentile(wave, 98) - np.percentile(wave, 2))
    return {
        "frames": float(n),
        "fps": round(fps, 3),
        "fPeakHz": round(f_peak, 3),
        "amp5Med": round(amp5_med, 4),
        "ledP2p": round(led_p2p, 3),
    }


def deflicker_rolling_led_mp4(
    mp4_path: Path,
    ffmpeg_bin: str,
    *,
    min_amp5: float = 0.45,
    min_p2p: float = 2.2,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """Automatically remove 5 Hz rolling-shutter LED horizontal bands with zero motion blur.

    Zero re-encode cost when the clip is already clean (returns immediately after a ~60 ms pre-scan).
    When triggered, performs a single-pass streaming exact-period row-median Y-channel notch filter
    directly in YUV420p (preserving chroma and sharpness) and atomically replaces `mp4_path`.
    """
    metrics = measure_rolling_led_banding(mp4_path)
    amp5 = float(metrics.get("amp5Med", 0.0))
    p2p = float(metrics.get("ledP2p", 0.0))
    fps = float(metrics.get("fps", 25.0))
    f_peak = float(metrics.get("fPeakHz", 5.0)) or 5.0
    if amp5 < min_amp5 or p2p < min_p2p or fps < 12.0:
        return {"filtered": False, "before": metrics}

    k = max(3, min(12, int(round(fps / f_peak))))
    half = k // 2
    cap = cv2.VideoCapture(str(mp4_path))
    if not cap.isOpened():
        return {"filtered": False, "before": metrics, "error": "open_failed"}

    tmp_out = mp4_path.with_name(f".{mp4_path.stem}.deflicker_{uuid.uuid4().hex[:6]}.mp4")
    proc: subprocess.Popen[bytes] | None = None
    try:
        ok, first_bgr = cap.read()
        if not ok or first_bgr is None:
            return {"filtered": False, "before": metrics, "error": "empty_video"}
        h, w = first_bgr.shape[:2]
        if w < 16 or h < 16 or (w % 2) != 0 or (h % 2) != 0:
            return {"filtered": False, "before": metrics, "error": "odd_dims"}

        ky = max(5, int(round(h * 0.03)) | 1)
        col_step = 4 if w >= 320 else 1
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-s",
            f"{w}x{h}",
            "-r",
            f"{fps:.6f}",
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-preset",
            "superfast",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-sc_threshold",
            "0",
            "-bf",
            "0",
            "-crf",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(tmp_out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert proc.stdin is not None and proc.stderr is not None

        def emit_frame(yuv_buf: np.ndarray, rmed: np.ndarray, baseline: np.ndarray) -> None:
            band = (rmed - baseline).astype(np.float32)
            band = cv2.GaussianBlur(band.reshape(-1, 1), (1, ky), 0).ravel()
            np.clip(band, -20.0, 20.0, out=band)
            y_plane = yuv_buf[:h, :]
            y_corr = np.clip(y_plane.astype(np.float32) - band[:, None], 0.0, 255.0).astype(np.uint8)
            out_yuv = yuv_buf.copy()
            out_yuv[:h, :] = y_corr
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(out_yuv.tobytes())

        buf: deque[tuple[np.ndarray, np.ndarray]] = deque()
        rmed_sum = np.zeros(h, dtype=np.float64)
        last_full_baseline: np.ndarray | None = None
        written = 0
        leading_flushed = False

        def push_bgr(bgr: np.ndarray) -> None:
            nonlocal leading_flushed, written, last_full_baseline
            yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
            rmed = np.median(yuv[:h, ::col_step], axis=1).astype(np.float32)
            buf.append((yuv, rmed))
            rmed_sum[:] += rmed
            if len(buf) == k:
                baseline = (rmed_sum / float(k)).astype(np.float32)
                last_full_baseline = baseline
                if not leading_flushed:
                    for idx in range(half + 1):
                        emit_frame(buf[idx][0], buf[idx][1], baseline)
                        written += 1
                    leading_flushed = True
                else:
                    emit_frame(buf[half][0], buf[half][1], baseline)
                    written += 1
                _, old_rmed = buf.popleft()
                rmed_sum[:] -= old_rmed

        push_bgr(first_bgr)
        while True:
            ok, frm = cap.read()
            if not ok or frm is None:
                break
            if frm.shape[0] != h or frm.shape[1] != w:
                frm = cv2.resize(frm, (w, h), interpolation=cv2.INTER_AREA)
            push_bgr(frm)
        cap.release()

        # Drain remaining buffered tail frames using the final exact-period k-frame baseline
        if buf:
            baseline = (
                last_full_baseline
                if last_full_baseline is not None
                else (rmed_sum / float(len(buf))).astype(np.float32)
            )
            start_idx = half if leading_flushed else 0
            for idx in range(start_idx, len(buf)):
                emit_frame(buf[idx][0], buf[idx][1], baseline)
                written += 1

        proc.stdin.close()
        err_bytes = proc.stderr.read()
        proc.stderr.close()
        rc = proc.wait(timeout=timeout_sec)
        if rc != 0 or not tmp_out.is_file() or tmp_out.stat().st_size < 1000:
            return {
                "filtered": False,
                "before": metrics,
                "error": err_bytes.decode("utf-8", "replace").strip() or f"ffmpeg_rc_{rc}",
            }
        tmp_out.replace(mp4_path)
        return {
            "filtered": True,
            "kFrames": k,
            "framesWritten": written,
            "before": metrics,
        }
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        return {"filtered": False, "before": metrics, "error": str(exc)}
    finally:
        cap.release()
        tmp_out.unlink(missing_ok=True)


def cluster_motion_bursts(
    samples: list[dict[str, Any]],
    threshold: float = DEFAULT_MOTION_THRESHOLD,
    min_dur_ms: int = 4000,
    merge_gap_ms: int = 4000,
    pad_before_ms: int = 3000,
    pad_after_ms: int = 2000,
    camera: str | None = None,
    resolve_overlap: bool = True,
    min_raw_dur_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Group active motion samples into padded bursts, optionally isolated to one camera and min duration."""
    cam_q = camera.strip().lower() if camera and camera.strip() else None
    eff_merge_ms = max(merge_gap_ms, 6000) if min_dur_ms >= 30_000 else merge_gap_ms
    eff_min_raw = (
        min_raw_dur_ms
        if min_raw_dur_ms is not None
        else (max(4000, min_dur_ms - 10_000) if min_dur_ms >= 30_000 else min_dur_ms)
    )

    by_cam: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        if cam_q is not None:
            s_cam = str(s.get("camera", "")).strip().lower()
            s_slug = str(s.get("slug", "")).strip().lower()
            if s_cam != cam_q and s_slug != cam_q:
                continue
        if float(s.get("energy", 0.0)) >= threshold:
            by_cam.setdefault(str(s.get("camera", camera or "Rack Cam")), []).append(s)

    cam_bursts: list[dict[str, Any]] = []
    for cam, items in by_cam.items():
        ordered = sorted(items, key=lambda x: int(x["startMs"]))
        groups: list[list[dict[str, Any]]] = []
        for it in ordered:
            if not groups:
                groups.append([it])
                continue
            prev_end = int(groups[-1][-1]["endMs"])
            if int(it["startMs"]) - prev_end <= eff_merge_ms:
                groups[-1].append(it)
            else:
                groups.append([it])

        for grp in groups:
            raw_start = int(grp[0]["startMs"])
            raw_end = int(grp[-1]["endMs"])
            if raw_end - raw_start < eff_min_raw:
                continue
            b_start = max(0, raw_start - pad_before_ms)
            b_end = raw_end + pad_after_ms
            if b_end - b_start < min_dur_ms:
                continue
            energies = [float(x["energy"]) for x in grp]
            mean_e = sum(energies) / max(1, len(energies))
            peak_e = max(energies)
            cam_bursts.append(
                {
                    "id": f"{safe_slug(cam)}_{b_start}",
                    "camera": cam,
                    "startMs": b_start,
                    "endMs": b_end,
                    "rawStartMs": raw_start,
                    "rawEndMs": raw_end,
                    "durationSec": max(1, int(round((b_end - b_start) / 1000.0))),
                    "peakEnergy": round(peak_e, 2),
                    "meanEnergy": round(mean_e, 2),
                    "note": f"Motion peak {peak_e:.1f}" if cam_q or len(by_cam) <= 1 else f"Auto-picked {cam} (motion {peak_e:.1f})",
                }
            )

    if not resolve_overlap or cam_q is not None:
        return sorted(cam_bursts, key=lambda b: int(b["startMs"]))

    cam_bursts.sort(key=lambda b: (int(b["startMs"]), -float(b["meanEnergy"])))
    merged: list[dict[str, Any]] = []
    for b in cam_bursts:
        if not merged:
            merged.append(b)
            continue
        last = merged[-1]
        overlap = min(int(last["endMs"]), int(b["endMs"])) - max(int(last["startMs"]), int(b["startMs"]))
        if overlap > 0:
            if float(b["meanEnergy"]) > float(last["meanEnergy"]):
                merged[-1] = b
        else:
            merged.append(b)
    return merged


def select_camera_for_window(
    samples: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    preferred_camera: str | None = None,
    fallback_camera: str = "Rack Cam",
) -> str:
    """Pick explicit `preferred_camera` or the camera with strongest motion inside `[start_ms, end_ms]`."""
    if preferred_camera and preferred_camera.strip():
        return preferred_camera.strip()
    totals: dict[str, list[float]] = {}
    for s in samples:
        s0 = int(s["startMs"])
        s1 = int(s["endMs"])
        if s1 >= start_ms and s0 <= end_ms:
            totals.setdefault(str(s["camera"]), []).append(float(s.get("energy", 0.0)))
    if not totals:
        return fallback_camera
    best_cam = max(totals.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), max(kv[1])))[0]
    return best_cam or fallback_camera


def select_pr_window(
    bursts: list[dict[str, Any]],
    set_at_ms: int,
    prev_set_at_ms: int | None = None,
) -> tuple[int, int, str | None]:
    """Align a PR completion tap with the preceding active lift burst."""
    floor_ms = prev_set_at_ms if prev_set_at_ms and prev_set_at_ms < set_at_ms else (set_at_ms - 180_000)
    ceiling_ms = set_at_ms + 5000
    candidates = [
        b
        for b in bursts
        if int(b["endMs"]) >= floor_ms and int(b["startMs"]) <= ceiling_ms
    ]
    if candidates:
        before_tap = [b for b in candidates if int(b["endMs"]) <= set_at_ms + 3500]
        picked = before_tap[-1] if before_tap else candidates[-1]
        return int(picked["startMs"]), int(picked["endMs"]), str(picked["camera"])
    fallback_start = max(floor_ms, set_at_ms - 35_000)
    return int(fallback_start), int(set_at_ms + 2000), None


@dataclass
class SegmentInfo:
    path: Path
    start_ms: int
    end_ms: int
    energy: float
    camera: str
    kind: str = ""
    session_id: str = ""


@dataclass
class CameraConfig:
    name: str
    slug: str
    rtsp_url: str = ""
    enabled: bool = True
    motion_threshold: float = DEFAULT_MOTION_THRESHOLD
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    exclusions: list[Quad] = field(default_factory=list)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    rotate: int = 0
    tilt: float = 0.0
    defish: bool = True
    source_slug: str = ""

    def effective_roi(self) -> tuple[float, float, float, float]:
        if self.roi != (0.0, 0.0, 1.0, 1.0):
            return self.roi
        return self.crop

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "rtspUrl": self.rtsp_url,
            "enabled": self.enabled,
            "motionThreshold": self.motion_threshold,
            "roi": list(self.roi),
            "exclusions": [[[pt[0], pt[1]] for pt in q] for q in self.exclusions],
            "crop": list(self.crop),
            "rotate": self.rotate,
            "tilt": self.tilt,
            "defish": self.defish,
            "sourceSlug": self.source_slug,
        }


@dataclass
class _LiveChannel:
    slug: str
    cond: threading.Condition = field(default_factory=threading.Condition)
    seq: int = 0
    latest_jpeg: bytes = b""
    latest_ts: float = 0.0
    stop_flag: bool = False


class LiveStreamHub:
    """Thread-safe per-camera frame broadcaster fed directly by the single RTSP capture worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, _LiveChannel] = {}

    def stop_all(self) -> None:
        with self._lock:
            slugs = list(self._channels.keys())
        for slug in slugs:
            self.stop_camera(slug)

    def stop_camera(self, slug: str) -> None:
        with self._lock:
            ch = self._channels.pop(slug, None)
        if not ch:
            return
        with ch.cond:
            ch.stop_flag = True
            ch.cond.notify_all()

    def get_channel(self, slug: str) -> _LiveChannel:
        with self._lock:
            ch = self._channels.get(slug)
            if not ch:
                ch = _LiveChannel(slug=slug)
                self._channels[slug] = ch
            return ch

    def publish_frame(self, slug: str, jpg: bytes) -> None:
        ch = self.get_channel(slug)
        with ch.cond:
            ch.latest_jpeg = jpg
            ch.latest_ts = time.time()
            ch.seq += 1
            ch.cond.notify_all()

    def recent_frame(self, slug: str, max_age_sec: float = 2.0) -> bytes | None:
        with self._lock:
            ch = self._channels.get(slug)
        if not ch:
            return None
        with ch.cond:
            if ch.latest_jpeg and (time.time() - ch.latest_ts) <= max_age_sec:
                return ch.latest_jpeg
        return None


@dataclass
class WallboardConfig:
    enabled: bool = True
    fire_tv_host: str = ""
    fire_tv_mac: str = ""
    adb_port: int = DEFAULT_ADB_PORT
    display_client: str = "silk"
    custom_component: str = ""
    wallboard_url: str = ""
    wake_on_start: bool = True
    sleep_on_stop: bool = True
    idle_sleep_sec: int = DEFAULT_IDLE_SLEEP_SEC
    suppress_screensaver: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "WallboardConfig":
        if not isinstance(raw, dict):
            return cls()
        client = str(raw.get("displayClient", "silk")).strip().lower()
        if client not in ("armory", "silk", "fully", "custom"):
            client = "silk"
        try:
            adb_port = max(1, min(65535, int(raw.get("adbPort", DEFAULT_ADB_PORT))))
        except (TypeError, ValueError):
            adb_port = DEFAULT_ADB_PORT
        try:
            idle_sec = max(60, min(7200, int(raw.get("idleSleepSec", DEFAULT_IDLE_SLEEP_SEC))))
        except (TypeError, ValueError):
            idle_sec = DEFAULT_IDLE_SLEEP_SEC
        return cls(
            enabled=bool(raw.get("enabled", True)),
            fire_tv_host=str(raw.get("fireTvHost", "")).strip(),
            fire_tv_mac=str(raw.get("fireTvMac", "")).strip(),
            adb_port=adb_port,
            display_client=client,
            custom_component=str(raw.get("customComponent", "")).strip(),
            wallboard_url=str(raw.get("wallboardUrl", "")).strip(),
            wake_on_start=bool(raw.get("wakeOnStart", True)),
            sleep_on_stop=bool(raw.get("sleepOnStop", True)),
            idle_sleep_sec=idle_sec,
            suppress_screensaver=bool(raw.get("suppressScreensaver", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fireTvHost": self.fire_tv_host,
            "fireTvMac": self.fire_tv_mac,
            "adbPort": self.adb_port,
            "displayClient": self.display_client,
            "customComponent": self.custom_component,
            "wallboardUrl": self.wallboard_url,
            "wakeOnStart": self.wake_on_start,
            "sleepOnStop": self.sleep_on_stop,
            "idleSleepSec": self.idle_sleep_sec,
            "suppressScreensaver": self.suppress_screensaver,
        }


def send_wol_magic_packet(
    mac_str: str,
    broadcast_ips: tuple[str, ...] = ("255.255.255.255", "192.168.1.255"),
    port: int = 9,
) -> list[str]:
    """Send UDP Wake-on-LAN magic packets for each valid MAC in a comma/space-separated string."""
    sent: list[str] = []
    tokens = [tok for tok in re.split(r"[\s,;]+", (mac_str or "").strip()) if tok]
    if not tokens:
        return sent
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ports = (port,) if port != 9 else (9, 7)
        for tok in tokens:
            clean = re.sub(r"[:-]", "", tok).lower()
            if len(clean) != 12 or not re.fullmatch(r"[0-9a-f]{12}", clean):
                continue
            packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
            for bcast in broadcast_ips:
                for p_dst in ports:
                    try:
                        sock.sendto(packet, (bcast, p_dst))
                    except OSError:
                        pass
            sent.append(":".join(clean[i : i + 2] for i in range(0, 12, 2)))
    finally:
        sock.close()
    return sent


class FireTvAdbController:
    """Manages ADB-over-Wi-Fi wake, Silk/kiosk launch, screensaver suppression, and auto-sleep."""

    def __init__(
        self,
        adb_bin: str | None = None,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        settle_sec: float = 0.35,
        wol_sender: Callable[[str], list[str]] | None = None,
        wol_wait_sec: float = 2.0,
        wol_retries: int = 3,
    ) -> None:
        self.adb_bin = adb_bin if adb_bin is not None else resolve_adb_bin()
        self._runner = runner or self._default_runner
        self.settle_sec = max(0.0, float(settle_sec))
        self._wol_sender = wol_sender or send_wol_magic_packet
        self.wol_wait_sec = max(0.0, float(wol_wait_sec))
        self.wol_retries = max(1, int(wol_retries))
        self.state_lock = threading.RLock()
        self.lock = self.state_lock
        self.op_lock = threading.Lock()
        self.adb_state = "unconfigured"
        self.last_model = ""
        self.last_action = ""
        self.last_error = ""
        self.last_wake_ms = 0
        self.tv_awake_requested = False
        self.manual_sleep_active = False
        self.pending_sleep_at_ms: int | None = None
        self.pending_sleep_reason = ""
        self._wake_in_flight = False
        self._last_wake_fail_mono = 0.0
        self._sleep_retry_count = 0

    @staticmethod
    def _default_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **_subprocess_no_window_kwargs(),
            )
            return res.returncode, res.stdout or "", res.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"ADB command timed out after {timeout:.0f}s"
        except Exception as exc:
            return 1, "", str(exc)

    def _run(self, args: list[str], timeout: float = 6.0) -> tuple[int, str, str]:
        if not self.adb_bin:
            return 127, "", "adb.exe not found on OptiPlex"
        return self._runner([self.adb_bin, *args], timeout)

    @staticmethod
    def serial(cfg: WallboardConfig) -> str:
        host = cfg.fire_tv_host.strip()
        if not host:
            return ""
        if ":" in host:
            return host
        return f"{host}:{cfg.adb_port}"

    def _evict_serial(self, serial: str) -> None:
        if not serial:
            return
        self._run(["disconnect", serial], timeout=3.0)
        self._run(["reconnect", "offline"], timeout=3.0)

    def disconnect_serial(self, serial: str) -> None:
        """Close idle ADB TCP socket cleanly so targets entering sleep do not wedge adbd."""
        if serial:
            self._run(["disconnect", serial], timeout=3.0)

    def _probe_tcp_adbd_locked(self, serial: str) -> bool:
        """Detect an awake Fire TV whose adbd slot is wedged (accepts TCP on :5555 then EOFs 0 bytes)."""
        if self._runner != self._default_runner or ":" not in serial:
            return False
        host, _, port_s = serial.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.connect((host, port))
            sock.settimeout(0.5)
            return sock.recv(16) == b""
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _apply_wallboard_tv_settings(self, ser: str, *, suppress_screensaver: bool) -> None:
        """Disable Fire OS home-screen featured video/audio autoplay (and screensaver during wake)."""
        cmds = [
            "settings put global com.amazon.tv.settings.featured_item_video_enabled 0",
            "settings put global com.amazon.tv.settings.featured_item_video_volume 0.0",
        ]
        if suppress_screensaver:
            cmds.extend([
                "settings put secure screensaver_enabled 0",
                "settings put secure sleep_timeout 0",
                "settings put global wifi_sleep_policy 2",
            ])
        self._run(["-s", ser, "shell", "; ".join(cmds)], timeout=6.0)

    def _offline_diagnostic_message(self, ser: str, detail: str) -> str:
        if self._probe_tcp_adbd_locked(ser):
            return (
                f"Wallboard TV is awake on LAN ({ser} port open) but adbd is locked on a stale "
                "deep-sleep session — toggle ADB Debugging OFF/ON or reboot the 32\" TV once; "
                "future wakes now auto-disconnect cleanly."
            )
        return f"Fire TV unreachable at {ser}: {detail}"

    def _connect_and_status(self, serial: str, *, reset_offline: bool = False) -> tuple[str, str]:
        if reset_offline:
            self._evict_serial(serial)
        for attempt in range(2):
            rc_c, out_c, err_c = self._run(["connect", serial], timeout=6.0)
            rc_d, out_d, err_d = self._run(["devices"], timeout=5.0)
            combined_c = f"{out_c} {err_c}".strip()
            status: str | None = None
            detail = ""
            for line in out_d.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == serial:
                    st = parts[1].lower()
                    if st in ("device", "unauthorized"):
                        return st, combined_c
                    status = "offline"
                    detail = f"{line.strip()} ({combined_c})".strip()
                    break
            if status is None:
                if "failed to authenticate" in combined_c.lower() or "unauthorized" in combined_c.lower():
                    return "unauthorized", combined_c
                status = "offline"
                detail = combined_c or err_d.strip() or out_d.strip() or f"rc={rc_c}/{rc_d}"
            if attempt == 0 and not reset_offline and "already connected" in combined_c.lower():
                self._evict_serial(serial)
                continue
            if rc_c == 124:
                self._evict_serial(serial)
            return status, detail
        return "offline", "ADB connect retry exhausted"

    def connect_and_probe(self, cfg: WallboardConfig) -> dict[str, Any]:
        with self.state_lock:
            ser = self.serial(cfg)
            if not ser:
                self.adb_state = "unconfigured"
                self.last_error = ""
                return {
                    "ok": False,
                    "configured": False,
                    "state": "unconfigured",
                    "serial": "",
                    "model": "",
                    "message": 'Enter the 32" Fire TV IP address first.',
                }
        with self.op_lock:
            st, detail = self._connect_and_status(ser)
            if st == "device":
                try:
                    rc_m, out_m, _ = self._run(["-s", ser, "shell", "getprop", "ro.product.model"], timeout=4.0)
                    model = out_m.strip().splitlines()[0] if rc_m == 0 and out_m.strip() else ""
                    self._apply_wallboard_tv_settings(ser, suppress_screensaver=False)
                finally:
                    self.disconnect_serial(ser)
                with self.state_lock:
                    self.adb_state = "ready"
                    if model:
                        self.last_model = model
                    self.last_error = ""
                    self.last_action = f"probe ({ser})"
                    cur_model = self.last_model
                return {
                    "ok": True,
                    "configured": True,
                    "state": "ready",
                    "serial": ser,
                    "model": cur_model,
                    "message": f"Connected ({cur_model or ser})",
                }
            if st == "unauthorized":
                msg = "Click 'Always allow from this computer' on the 32\" Fire TV screen, then retry."
                with self.state_lock:
                    self.adb_state = "unauthorized"
                    self.last_error = msg
                return {
                    "ok": False,
                    "configured": True,
                    "state": "unauthorized",
                    "serial": ser,
                    "model": "",
                    "message": msg,
                }
            self._evict_serial(ser)
            msg = self._offline_diagnostic_message(ser, detail)
            with self.state_lock:
                self.adb_state = "offline"
                self.last_error = msg
            return {
                "ok": False,
                "configured": True,
                "state": "offline",
                "serial": ser,
                "model": "",
                "message": msg,
            }

    @staticmethod
    def _launch_cmd(cfg: WallboardConfig, wallboard_url: str, ser: str) -> list[str]:
        safe_url = shlex.quote(wallboard_url)
        if cfg.display_client == "armory":
            return [
                "-s", ser, "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
                "-d", safe_url, "-n", "com.froriep.armorywallboard/.WallboardActivity",
            ]
        if cfg.display_client == "fully":
            return [
                "-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                "-d", safe_url, "-n", "de.ozerov.fully/.MainActivity",
            ]
        if cfg.display_client == "custom":
            comp = cfg.custom_component.strip()
            if comp:
                return [
                    "-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", safe_url, "-n", shlex.quote(comp),
                ]
            return ["-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", safe_url]
        return [
            "-s", ser, "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
            "-d", safe_url, "-n", "com.amazon.cloud9/.BrowserActivity",
        ]

    def wake_and_launch(self, cfg: WallboardConfig, wallboard_url: str, force: bool = False) -> dict[str, Any]:
        with self.state_lock:
            self.pending_sleep_at_ms = None
            self.pending_sleep_reason = ""
            self._sleep_retry_count = 0
            if force:
                self.manual_sleep_active = False
            ser = self.serial(cfg)
            if not cfg.enabled or not ser:
                self.adb_state = "disabled" if not cfg.enabled else "unconfigured"
                self.last_error = ""
                return {
                    "ok": False,
                    "configured": bool(ser),
                    "state": self.adb_state,
                    "serial": ser,
                    "model": self.last_model,
                    "message": "Fire TV IP not configured yet" if not ser else "Wallboard ADB disabled",
                }
            now_ms = int(time.time() * 1000)
            if self.tv_awake_requested and not force and (now_ms - self.last_wake_ms) < 20_000:
                return {
                    "ok": True,
                    "skipped": True,
                    "configured": True,
                    "state": self.adb_state,
                    "serial": ser,
                    "model": self.last_model,
                    "message": "Fire TV wake already active",
                }
            if self._wake_in_flight and not force:
                return {
                    "ok": True,
                    "skipped": True,
                    "configured": True,
                    "state": self.adb_state,
                    "serial": ser,
                    "model": self.last_model,
                    "message": "Fire TV wake already in progress",
                }
            self._wake_in_flight = True

        try:
            with self.op_lock:
                st, detail = self._connect_and_status(ser)
                if st == "offline" and cfg.fire_tv_mac.strip():
                    sent_macs = self._wol_sender(cfg.fire_tv_mac)
                    if sent_macs:
                        for _ in range(self.wol_retries):
                            if self.wol_wait_sec > 0:
                                time.sleep(self.wol_wait_sec)
                            st, detail = self._connect_and_status(ser, reset_offline=True)
                            if st != "offline":
                                break
                if st == "unauthorized":
                    msg = "Click 'Always allow from this computer' on the 32\" Fire TV screen, then retry."
                    with self.state_lock:
                        self.adb_state = "unauthorized"
                        self.last_error = msg
                        self._last_wake_fail_mono = time.monotonic()
                    return {"ok": False, "configured": True, "state": "unauthorized", "serial": ser, "message": msg}
                if st != "device":
                    self._evict_serial(ser)
                    msg = self._offline_diagnostic_message(ser, detail)
                    with self.state_lock:
                        self.adb_state = "offline"
                        self.last_error = msg
                        self._last_wake_fail_mono = time.monotonic()
                    return {"ok": False, "configured": True, "state": "offline", "serial": ser, "message": msg}

                try:
                    rc_w, out_w, err_w = self._run(["-s", ser, "shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=5.0)
                    if rc_w != 0:
                        msg = f"Wake keyevent failed: {(err_w or out_w).strip()}"
                        with self.state_lock:
                            self.adb_state = "offline"
                            self.last_error = msg
                            self._last_wake_fail_mono = time.monotonic()
                        return {"ok": False, "configured": True, "state": "offline", "serial": ser, "message": msg}

                    if self.settle_sec > 0:
                        time.sleep(self.settle_sec)

                    self._apply_wallboard_tv_settings(ser, suppress_screensaver=cfg.suppress_screensaver)

                    rc_l, out_l, err_l = self._run(self._launch_cmd(cfg, wallboard_url, ser), timeout=8.0)
                    if cfg.display_client == "silk" and (rc_l != 0 or "Error" in f"{out_l}\n{err_l}"):
                        rc_l, out_l, err_l = self._run(
                            ["-s", ser, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(wallboard_url), "-p", "com.amazon.cloud9"],
                            timeout=8.0,
                        )

                    if rc_l != 0:
                        msg = f"Awoke TV but browser launch failed: {(err_l or out_l).strip()}"
                        with self.state_lock:
                            self.adb_state = "ready"
                            self.last_error = msg
                            self._last_wake_fail_mono = time.monotonic()
                        return {"ok": False, "configured": True, "state": "ready", "serial": ser, "message": msg}

                    with self.state_lock:
                        self.tv_awake_requested = True
                        self.manual_sleep_active = False
                        self.last_wake_ms = int(time.time() * 1000)
                        self.adb_state = "awake"
                        self.last_action = f"wake -> {cfg.display_client}"
                        self.last_error = ""
                        self._last_wake_fail_mono = 0.0
                        cur_model = self.last_model
                    return {
                        "ok": True,
                        "configured": True,
                        "state": "awake",
                        "serial": ser,
                        "model": cur_model,
                        "message": f"Awoke {cur_model or ser} & opened {cfg.display_client}",
                    }
                finally:
                    self.disconnect_serial(ser)
        finally:
            with self.state_lock:
                self._wake_in_flight = False

    def schedule_sleep(self, cfg: WallboardConfig, delay_sec: float = 0.0, reason: str = "") -> dict[str, Any]:
        if delay_sec <= 0:
            return self.sleep_now(cfg, reason=reason)
        with self.state_lock:
            self.pending_sleep_at_ms = int(time.time() * 1000) + int(delay_sec * 1000)
            self.pending_sleep_reason = reason or "workout_end"
            self._sleep_retry_count = 0
            return {
                "ok": True,
                "scheduledDelaySec": delay_sec,
                "reason": self.pending_sleep_reason,
                "message": f"Sleep scheduled in {int(delay_sec)}s",
            }

    def sleep_now(self, cfg: WallboardConfig, reason: str = "", *, only_if_pending: bool = False) -> dict[str, Any]:
        is_manual_sleep = reason in ("display_off", "manual")
        with self.op_lock:
            with self.state_lock:
                if only_if_pending and self.pending_sleep_at_ms is None and not self.tv_awake_requested:
                    return {
                        "ok": True,
                        "skipped": True,
                        "state": self.adb_state,
                        "serial": self.serial(cfg),
                        "message": "Sleep canceled by active workout",
                    }
                if is_manual_sleep:
                    self.manual_sleep_active = True
                ser = self.serial(cfg)
                if not ser:
                    self.pending_sleep_at_ms = None
                    self.pending_sleep_reason = ""
                    self.tv_awake_requested = False
                    self.last_wake_ms = 0
                    self._sleep_retry_count = 0
                    self.adb_state = "unconfigured"
                    self.last_action = f"sleep skipped ({reason or 'unconfigured'})"
                    return {
                        "ok": False,
                        "configured": False,
                        "state": "unconfigured",
                        "serial": "",
                        "message": "Fire TV IP not configured yet",
                    }
            st, detail = self._connect_and_status(ser)
            if st != "device":
                self._evict_serial(ser)
                msg = self._offline_diagnostic_message(ser, detail) if st == "offline" else f"Sleep failed ({st}): {detail}"
                with self.state_lock:
                    self.adb_state = st
                    self.last_error = msg
                    if reason != "manual" and self._sleep_retry_count < 3:
                        self._sleep_retry_count += 1
                        backoff = (30, 60, 120)[self._sleep_retry_count - 1]
                        self.pending_sleep_at_ms = int(time.time() * 1000) + backoff * 1000
                        self.pending_sleep_reason = reason or "retry_sleep"
                    else:
                        self.pending_sleep_at_ms = None
                        self.pending_sleep_reason = ""
                        self.tv_awake_requested = False
                        self.last_wake_ms = 0
                        self._sleep_retry_count = 0
                return {"ok": False, "configured": True, "state": st, "serial": ser, "message": msg}
            try:
                if cfg.suppress_screensaver:
                    self._run(
                        [
                            "-s", ser, "shell",
                            "settings put secure screensaver_enabled 1; settings put secure sleep_timeout 1200000",
                        ],
                        timeout=5.0,
                    )
                self._run(["-s", ser, "shell", "input", "keyevent", "KEYCODE_SLEEP"], timeout=5.0)
                if self.settle_sec > 0:
                    time.sleep(min(0.25, self.settle_sec))
            finally:
                self.disconnect_serial(ser)
            with self.state_lock:
                self.pending_sleep_at_ms = None
                self.pending_sleep_reason = ""
                self.tv_awake_requested = False
                self.last_wake_ms = 0
                self._sleep_retry_count = 0
                self.adb_state = "asleep"
                self.last_action = f"sleep ({reason or 'manual'})"
                self.last_error = ""
                cur_model = self.last_model
            return {
                "ok": True,
                "configured": True,
                "state": "asleep",
                "serial": ser,
                "model": cur_model,
                "message": f"Slept {cur_model or ser}",
            }

    def check_timers(
        self,
        cfg: WallboardConfig,
        last_active_push_ms: int,
        workout_active: bool,
        now_ms: int | None = None,
    ) -> dict[str, Any] | None:
        cur_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        reason_to_sleep: str | None = None
        with self.state_lock:
            if self.pending_sleep_at_ms is not None and cur_ms >= self.pending_sleep_at_ms:
                if cfg.sleep_on_stop:
                    reason_to_sleep = self.pending_sleep_reason or "workout_end"
                else:
                    self.pending_sleep_at_ms = None
                    self.pending_sleep_reason = ""
            elif (
                self.tv_awake_requested
                and workout_active
                and cfg.idle_sleep_sec > 0
                and last_active_push_ms > 0
                and (cur_ms - max(last_active_push_ms, self.last_wake_ms)) >= cfg.idle_sleep_sec * 1000
            ):
                reason_to_sleep = "idle_timeout"
        if reason_to_sleep:
            return self.sleep_now(cfg, reason=reason_to_sleep, only_if_pending=True)
        return None

    def status_dict(self, cfg: WallboardConfig) -> dict[str, Any]:
        with self.lock:
            now_ms = int(time.time() * 1000)
            pending_sec = (
                max(0, int((self.pending_sleep_at_ms - now_ms) / 1000))
                if self.pending_sleep_at_ms is not None
                else 0
            )
            return {
                "adbBinFound": bool(self.adb_bin),
                "adbBin": self.adb_bin or "",
                "adbState": self.adb_state,
                "fireTvHost": cfg.fire_tv_host,
                "fireTvMac": cfg.fire_tv_mac,
                "adbPort": cfg.adb_port,
                "fireTvModel": self.last_model,
                "tvAwake": self.tv_awake_requested,
                "manualSleepActive": self.manual_sleep_active,
                "lastAction": self.last_action,
                "lastError": self.last_error,
                "pendingSleepSec": pending_sec,
            }


class WallboardRelay:
    """Thread-safe OptiPlex mirror of Pixel's live Wallboard state with SSE fan-out."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.cache_html_path = root_dir / "wallboard_cache.html"
        self.standby_cache_path = root_dir / "wallboard_standby.json"
        self.repo_page_ts_path = Path(__file__).resolve().parents[2] / "src" / "wallboard" / "page.ts"
        self.cond = threading.Condition()
        self.seq = 0
        self.latest_json = ""
        self.mode = "standby"
        self.workout_active = False
        self.last_push_ms = 0
        self.last_active_push_ms = 0
        self.source_url = ""
        self.sse_clients = 0
        self.saved_standby: dict[str, Any] | None = self._load_saved_standby()

    def _load_saved_standby(self) -> dict[str, Any] | None:
        if not self.standby_cache_path.is_file():
            return None
        try:
            obj = json.loads(self.standby_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            isinstance(obj, dict)
            and isinstance(obj.get("ymd"), str)
            and isinstance(obj.get("todayItems"), list)
        ):
            rec: dict[str, Any] = {
                "ymd": obj["ymd"],
                "weekLabel": str(obj.get("weekLabel") or ""),
                "todayItems": list(obj["todayItems"]),
                "completedItems": list(obj.get("completedItems") or []) if isinstance(obj.get("completedItems"), list) else [],
                "nextUp": dict(obj["nextUp"]) if isinstance(obj.get("nextUp"), dict) else None,
                "upcomingDays": dict(obj["upcomingDays"]) if isinstance(obj.get("upcomingDays"), dict) else {},
            }
            if isinstance(obj.get("isRestDay"), bool):
                rec["isRestDay"] = obj["isRestDay"]
            return rec
        return None

    def _save_standby_schedule(
        self,
        ymd: str,
        week_label: str,
        today_items: list[Any],
        completed_items: list[Any] | None = None,
        next_up: dict[str, Any] | None = None,
        is_rest_day: bool | None = None,
        upcoming_days: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_upcoming: dict[str, Any] = {}
        if isinstance(upcoming_days, dict):
            for k, v in upcoming_days.items():
                if isinstance(k, str) and len(k) == 10 and isinstance(v, dict):
                    clean_upcoming[k] = {
                        "weekLabel": str(v.get("weekLabel") or ""),
                        "todayItems": list(v.get("todayItems") or []) if isinstance(v.get("todayItems"), list) else [],
                        "isRestDay": bool(v.get("isRestDay", False)),
                        "nextUp": dict(v["nextUp"]) if isinstance(v.get("nextUp"), dict) else None,
                    }
        rec: dict[str, Any] = {
            "ymd": ymd,
            "weekLabel": week_label,
            "todayItems": list(today_items),
            "completedItems": list(completed_items or []),
            "nextUp": dict(next_up) if isinstance(next_up, dict) else None,
            "upcomingDays": clean_upcoming,
        }
        if isinstance(is_rest_day, bool):
            rec["isRestDay"] = is_rest_day
        tmp = self.standby_cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        tmp.replace(self.standby_cache_path)
        return rec

    def _raw_html_source(self) -> str:
        repo_html: str | None = None
        if self.repo_page_ts_path.is_file():
            repo_html = extract_wallboard_html_from_ts(self.repo_page_ts_path)
        cache_html: str | None = None
        if self.cache_html_path.is_file():
            try:
                txt = self.cache_html_path.read_text(encoding="utf-8")
                if "<!doctype html" in txt[:200].lower():
                    cache_html = txt
            except Exception:
                pass
        if repo_html:
            return repo_html
        if cache_html:
            return cache_html
        return FALLBACK_WALLBOARD_HTML

    def get_html_version(self) -> str:
        raw = self._raw_html_source()
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def get_html(self) -> str:
        raw = self._raw_html_source()
        ver = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
        out = inject_wallboard_stage_overlay(raw)
        ver_tag = f'<script>window.__WB_HTML_VER={json.dumps(ver)};</script>'
        idx = out.lower().rfind("</body>")
        if idx >= 0:
            return out[:idx] + ver_tag + out[idx:]
        return out + ver_tag

    def save_uploaded_html(self, html: str) -> bool:
        if not isinstance(html, str) or "<!doctype html" not in html[:200].lower():
            return False
        tmp = self.cache_html_path.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(self.cache_html_path)
        return True

    def standby_state_dict(self, now_ms: int | None = None) -> dict[str, Any]:
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        dt = datetime.fromtimestamp(ts / 1000.0)
        hour12 = dt.strftime("%I").lstrip("0") or "12"
        ymd = dt.strftime("%Y-%m-%d")
        with self.cond:
            saved = dict(self.saved_standby) if self.saved_standby else None
        exact_sync = bool(saved and saved.get("ymd") == ymd)
        upcoming_day: dict[str, Any] | None = None
        if not exact_sync and saved and isinstance(saved.get("upcomingDays"), dict):
            cand = saved["upcomingDays"].get(ymd)
            if isinstance(cand, dict):
                upcoming_day = cand
        synced = bool(exact_sync or upcoming_day is not None)
        if exact_sync and saved:
            wk_lbl = str(saved.get("weekLabel") or "")
            t_items = list(saved.get("todayItems") or [])
            c_items = list(saved.get("completedItems") or [])
            nxt_up = dict(saved["nextUp"]) if isinstance(saved.get("nextUp"), dict) else None
            rest_flag = saved.get("isRestDay") if isinstance(saved.get("isRestDay"), bool) else None
        elif upcoming_day is not None:
            wk_lbl = str(upcoming_day.get("weekLabel") or "")
            t_items = list(upcoming_day.get("todayItems") or [])
            c_items = []
            nxt_up = dict(upcoming_day["nextUp"]) if isinstance(upcoming_day.get("nextUp"), dict) else None
            rest_flag = upcoming_day.get("isRestDay") if isinstance(upcoming_day.get("isRestDay"), bool) else None
        else:
            wk_lbl = ""
            t_items = []
            c_items = []
            nxt_up = None
            rest_flag = None
        return {
            "ts": ts,
            "htmlVersion": self.get_html_version(),
            "mode": "standby",
            "hr": {
                "bpm": 0,
                "fresh": False,
                "zone": 0,
                "pctMax": 0,
                "readyLimitBpm": 130,
                "drop60": 0,
                "readiness": "idle",
            },
            "voice": {
                "enabled": False,
                "listening": False,
                "armed": False,
                "wakeWord": "armory",
                "banner": None,
            },
            "standby": {
                "ymd": ymd,
                "dateLabel": dt.strftime("%a, %b ") + str(dt.day),
                "timeLabel": f"{hour12}:{dt.strftime('%M %p')}",
                "weekLabel": wk_lbl,
                "todayItems": t_items,
                "completedItems": c_items,
                "nextUp": nxt_up,
                **({"isRestDay": bool(rest_flag)} if isinstance(rest_flag, bool) else {}),
                "scheduleSynced": synced,
            },
        }

    def current_state_json(self, stale_ms: int = 900_000) -> str:
        with self.cond:
            now_ms = int(time.time() * 1000)
            if (
                self.mode != "standby"
                and self.latest_json
                and self.last_push_ms > 0
                and (now_ms - self.last_push_ms) <= stale_ms
            ):
                return self.latest_json
        return json.dumps(self.standby_state_dict(now_ms), separators=(",", ":"))

    def update_state(
        self,
        state_obj: dict[str, Any] | None,
        workout_active: bool,
        source_url: str = "",
    ) -> None:
        now_ms = int(time.time() * 1000)
        payload = dict(state_obj) if isinstance(state_obj, dict) else self.standby_state_dict(now_ms)
        payload["htmlVersion"] = self.get_html_version()
        saved_rec: dict[str, Any] | None = None
        if payload.get("mode") == "standby" and isinstance(payload.get("standby"), dict):
            sb = dict(payload["standby"])
            if sb.get("scheduleSynced") is not False:
                ymd = datetime.fromtimestamp(now_ms / 1000.0).strftime("%Y-%m-%d")
                existing_up = (self.saved_standby or {}).get("upcomingDays") if isinstance(self.saved_standby, dict) else {}
                up_in = sb.get("upcomingDays") if isinstance(sb.get("upcomingDays"), dict) else existing_up
                saved_rec = self._save_standby_schedule(
                    ymd,
                    str(sb.get("weekLabel") or ""),
                    list(sb.get("todayItems") or []),
                    list(sb.get("completedItems") or []) if isinstance(sb.get("completedItems"), list) else [],
                    dict(sb["nextUp"]) if isinstance(sb.get("nextUp"), dict) else None,
                    sb.get("isRestDay") if isinstance(sb.get("isRestDay"), bool) else None,
                    up_in if isinstance(up_in, dict) else None,
                )
                sb["ymd"] = ymd
                sb["scheduleSynced"] = True
                sb.pop("upcomingDays", None)
                payload["standby"] = sb
        compact = json.dumps(payload, separators=(",", ":")).replace("\r", " ").replace("\n", " ")
        with self.cond:
            if saved_rec is not None:
                self.saved_standby = saved_rec
            self.latest_json = compact
            self.workout_active = bool(workout_active)
            self.mode = str(payload.get("mode") or ("lift" if workout_active else "standby"))
            self.last_push_ms = now_ms
            if workout_active:
                self.last_active_push_ms = now_ms
            if source_url and source_url.strip():
                self.source_url = source_url.strip().rstrip("/")
            self.seq += 1
            self.cond.notify_all()

    def mark_active_wake(self, state_obj: dict[str, Any] | None = None, source_url: str = "") -> None:
        now_ms = int(time.time() * 1000)
        if isinstance(state_obj, dict):
            self.update_state(state_obj, workout_active=True, source_url=source_url)
            return
        with self.cond:
            self.workout_active = True
            self.last_push_ms = now_ms
            self.last_active_push_ms = now_ms
            if source_url and source_url.strip():
                self.source_url = source_url.strip().rstrip("/")

    def clear_stale_active(self, idle_sleep_sec: int, now_ms: int | None = None) -> bool:
        cur_ms = int(time.time() * 1000) if now_ms is None else now_ms
        stale_ms = max(60, int(idle_sleep_sec or DEFAULT_IDLE_SLEEP_SEC)) * 1000
        with self.cond:
            if not self.workout_active:
                return False
            if self.last_active_push_ms > 0 and (cur_ms - self.last_active_push_ms) < stale_ms:
                return False
            standby_payload = self.standby_state_dict(cur_ms)
            self.latest_json = json.dumps(standby_payload, separators=(",", ":")).replace("\r", " ").replace("\n", " ")
            self.workout_active = False
            self.mode = "standby"
            self.last_active_push_ms = 0
            self.seq += 1
            self.cond.notify_all()
            return True

    def status_dict(self) -> dict[str, Any]:
        with self.cond:
            return {
                "clients": self.sse_clients,
                "mode": self.mode,
                "workoutActive": self.workout_active,
                "lastPushMs": self.last_push_ms,
                "lastActivePushMs": self.last_active_push_ms,
            }


WAKE_STUDIO_CATALOG: tuple[dict[str, str], ...] = (
    {"phrase": "hey atlas", "slug": "hey_atlas", "label": "Hey Atlas", "kind": "custom"},
    {"phrase": "hey armory", "slug": "hey_armory", "label": "Hey Armory", "kind": "custom"},
    {"phrase": "armory", "slug": "armory", "label": "Armory (Bare)", "kind": "custom"},
    {"phrase": "hey jarvis", "slug": "hey_jarvis", "label": "Hey Jarvis", "kind": "stock"},
)
_WAKE_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.wav$", re.IGNORECASE)


class WakeWordTrainerManager:
    """Manages Dave's personal S330 wake-word recordings and custom openWakeWord ONNX training."""

    def __init__(self, root_dir: Path) -> None:
        self.root = Path(root_dir)
        self.models_dir = self.root / "models" / "openwakeword"
        self.train_root = self.root / "oww_training"
        self.lock = threading.Lock()
        self._running = False
        self._phrase = ""
        self._slug = ""
        self._stage = "Idle"
        self._progress_pct = 0
        self._started_ms = 0
        self._finished_ms = 0
        self._exit_code: int | None = None
        self._error: str | None = None
        self._logs: deque[str] = deque(maxlen=80)

    @staticmethod
    def resolve_entry(phrase_or_slug: str) -> dict[str, str] | None:
        q = re.sub(r"\s+", " ", str(phrase_or_slug or "").strip().lower())
        q_slug = wake_phrase_slug(q) if q else ""
        for item in WAKE_STUDIO_CATALOG:
            if q == item["phrase"] or q_slug == item["slug"]:
                return dict(item)
        return None

    @staticmethod
    def _clip_info(slug: str, wav_path: Path) -> dict[str, Any]:
        dur = 0.0
        try:
            with wave.open(str(wav_path), "rb") as wf:
                sr = max(1, int(wf.getframerate() or 16000))
                dur = round(float(wf.getnframes()) / float(sr), 2)
        except Exception:
            pass
        try:
            st = wav_path.stat()
            size_b = int(st.st_size)
            ts_ms = int(st.st_mtime * 1000)
        except OSError:
            size_b = 0
            ts_ms = 0
        return {
            "name": wav_path.name,
            "durationSec": dur,
            "sizeBytes": size_b,
            "tsMs": ts_ms,
            "url": f"/api/voice/wake-studio/clip/{slug}/{urllib.parse.quote(wav_path.name)}",
        }

    def user_dir_for(self, slug: str) -> Path:
        return self.train_root / slug / "user_positives"

    def studio_state(self, voice_svc: RoomVoiceService) -> dict[str, Any]:
        words: list[dict[str, Any]] = []
        active_phrase = str(voice_svc.cfg.wake_word or "").strip().lower()
        for item in WAKE_STUDIO_CATALOG:
            slug = item["slug"]
            onnx_p = self.models_dir / f"{slug}_v0.1.onnx"
            stock_p = self.models_dir / f"{slug}_v0.1.stock.onnx"
            tdir = self.train_root / slug
            udir = tdir / "user_positives"
            clips: list[dict[str, Any]] = []
            if udir.is_dir():
                wavs = sorted(
                    [p for p in udir.iterdir() if p.is_file() and _WAKE_CLIP_NAME_RE.match(p.name)],
                    key=lambda p: (p.stat().st_mtime, p.name),
                    reverse=True,
                )
                clips = [self._clip_info(slug, p) for p in wavs]
            metrics: dict[str, Any] | None = None
            mp = tdir / "metrics.json"
            if mp.is_file():
                try:
                    metrics = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    metrics = None
            words.append({
                **item,
                "active": active_phrase == item["phrase"],
                "modelExists": onnx_p.is_file(),
                "modelSizeKb": round(onnx_p.stat().st_size / 1024.0, 1) if onnx_p.is_file() else 0.0,
                "hasStockBackup": stock_p.is_file(),
                "synthCached": (tdir / "synth_pos_features.npy").is_file(),
                "clipCount": len(clips),
                "clips": clips,
                "metrics": metrics,
            })

        with self.lock:
            training = {
                "running": bool(self._running),
                "phrase": self._phrase,
                "slug": self._slug,
                "stage": self._stage,
                "progressPct": int(self._progress_pct),
                "startedMs": int(self._started_ms),
                "finishedMs": int(self._finished_ms),
                "exitCode": self._exit_code,
                "error": self._error,
                "logs": list(self._logs),
            }

        return {
            "activePhrase": active_phrase,
            "wakeEngine": voice_svc.cfg.wake_engine,
            "owwReady": bool(voice_svc._oww_spotter is not None and getattr(voice_svc._oww_spotter, "ready", False)),
            "enrollment": voice_svc.wake_enrollment_status(),
            "training": training,
            "words": words,
        }

    def delete_clip(self, slug: str, filename: str) -> bool:
        entry = self.resolve_entry(slug)
        if entry is None or not _WAKE_CLIP_NAME_RE.match(str(filename or "")):
            return False
        target = self.user_dir_for(entry["slug"]) / Path(filename).name
        if not target.is_file():
            return False
        target.unlink()
        return True

    def clear_clips(self, slug: str) -> int:
        entry = self.resolve_entry(slug)
        if entry is None:
            return 0
        udir = self.user_dir_for(entry["slug"])
        if not udir.is_dir():
            return 0
        removed = 0
        for p in list(udir.iterdir()):
            if p.is_file() and _WAKE_CLIP_NAME_RE.match(p.name):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def restore_stock(self, phrase_or_slug: str, voice_svc: RoomVoiceService | None = None) -> tuple[bool, str]:
        entry = self.resolve_entry(phrase_or_slug)
        if entry is None:
            return False, "Unknown wake word"
        slug = entry["slug"]
        stock_p = self.models_dir / f"{slug}_v0.1.stock.onnx"
        onnx_p = self.models_dir / f"{slug}_v0.1.onnx"
        if not stock_p.is_file():
            return False, "No stock backup found"
        shutil.copy2(stock_p, onnx_p)
        if voice_svc is not None and str(voice_svc.cfg.wake_word or "").strip().lower() == entry["phrase"]:
            voice_svc.reload_oww_model(entry["phrase"])
        return True, f"Restored stock {entry['label']}"

    def start_training(
        self,
        phrase_or_slug: str,
        *,
        force_full: bool = False,
        voice_svc: RoomVoiceService | None = None,
    ) -> tuple[bool, str]:
        entry = self.resolve_entry(phrase_or_slug)
        if entry is None:
            return False, "Unsupported wake word"
        if voice_svc is not None:
            voice_svc.stop_wake_enrollment()
        phrase = entry["phrase"]
        slug = entry["slug"]
        synth_cached = (self.train_root / slug / "synth_pos_features.npy").is_file()
        retrain_only = bool(synth_cached and not force_full)

        with self.lock:
            if self._running:
                return False, f"Already training '{self._phrase}'"
            self._running = True
            self._phrase = phrase
            self._slug = slug
            self._stage = "Retraining ONNX w/ your clips…" if retrain_only else "Synthesizing dataset & training…"
            self._progress_pct = 5
            self._started_ms = int(time.time() * 1000)
            self._finished_ms = 0
            self._exit_code = None
            self._error = None
            self._logs.clear()

        if slug == "hey_jarvis":
            onnx_p = self.models_dir / "hey_jarvis_v0.1.onnx"
            stock_p = self.models_dir / "hey_jarvis_v0.1.stock.onnx"
            if onnx_p.is_file() and not stock_p.is_file():
                try:
                    self.models_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(onnx_p, stock_p)
                except OSError:
                    pass

        threading.Thread(
            target=self._run_worker,
            args=(phrase, slug, retrain_only, voice_svc),
            name=f"armory-oww-train-{slug}",
            daemon=True,
        ).start()
        return True, f"Training started for {entry['label']}"

    def _run_worker(
        self,
        phrase: str,
        slug: str,
        retrain_only: bool,
        voice_svc: RoomVoiceService | None,
    ) -> None:
        script_p = Path(__file__).resolve().parent / "train_wake_word.py"
        cmd = [sys.executable, str(script_p), "--phrase", phrase]
        if retrain_only:
            cmd.append("--retrain-only")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        rc = 1
        err_msg: str | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                with self.lock:
                    self._logs.append(line)
                    if "[1/4]" in line:
                        self._stage, self._progress_pct = "Loading Kokoro voices…", 10
                    elif "[2/4]" in line:
                        self._stage, self._progress_pct = "Mining gym music backgrounds…", 20
                    elif "[3/4]" in line:
                        self._stage, self._progress_pct = "Synthesizing speakers & confusables…", 32
                    elif "[4/4]" in line:
                        self._stage, self._progress_pct = "Extracting ONNX embeddings…", 48
                    elif line.startswith("Training "):
                        self._stage, self._progress_pct = "Training wake MLP…", 55
                    elif "step " in line and "/" in line:
                        m_st = re.search(r"step\s+(\d+)/(\d+)", line)
                        if m_st:
                            cur_s = int(m_st.group(1))
                            tot_s = max(1, int(m_st.group(2)))
                            self._stage = f"Training MLP ({cur_s}/{tot_s})"
                            self._progress_pct = min(90, 55 + int(35.0 * cur_s / tot_s))
                    elif "Exported ONNX:" in line:
                        self._stage, self._progress_pct = "Exported ONNX model", 92
                    elif "BLIND GYM STRESS TEST" in line:
                        self._stage, self._progress_pct = "Running blind gym rock stress test…", 96
            rc = proc.wait()
            if rc != 0:
                err_msg = f"Trainer exited with code {rc}"
        except Exception as exc:
            err_msg = str(exc)
            with self.lock:
                self._logs.append(f"ERROR: {err_msg}")

        if rc == 0 and voice_svc is not None:
            if str(voice_svc.cfg.wake_word or "").strip().lower() == phrase:
                voice_svc.reload_oww_model(phrase)

        with self.lock:
            self._running = False
            self._finished_ms = int(time.time() * 1000)
            self._exit_code = int(rc)
            self._error = err_msg
            if rc == 0:
                self._stage = "Complete — ONNX deployed!"
                self._progress_pct = 100
            else:
                self._stage = f"Failed (code {rc})"


class ArmoryCamService:
    """Core DVR manager: config persistence, FFmpeg RTSP ring capture, motion indexing & clip cutting."""

    def __init__(
        self,
        root_dir: Path | None = None,
        adb_controller: FireTvAdbController | None = None,
        drive_backup_dir: str | Path | None = None,
        ntfy_topic: str | None = None,
        voice_autostart: bool | None = None,
    ) -> None:
        self.root = (root_dir or default_root_dir()).resolve()
        self.ring_root = self.root / "ring"
        self.tapes_root = self.root / "tapes"
        self.clips_root = self.root / "clips"
        self.previews_root = self.root / "previews"
        self.thumbs_root = self.root / "thumbs"
        self.bump_refs_root = self.root / "bump_refs"
        self.bump_state_path = self.bump_refs_root / "bump_state.json"
        self.logs_root = self.root / "logs"
        self.config_path = self.root / "config.json"
        self.clips_index_path = self.clips_root / "index.jsonl"
        self.cal_state_path = self.root / "calibration_state.json"
        self.preview_local_path = self.root / "preview_overview.jpg"
        for d in (
            self.root,
            self.ring_root,
            self.tapes_root,
            self.clips_root,
            self.previews_root,
            self.thumbs_root,
            self.bump_refs_root,
            self.logs_root,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = resolve_tool_bin("ffmpeg", "ARMORY_CAM_FFMPEG")
        self.ffprobe = resolve_tool_bin("ffprobe", "ARMORY_CAM_FFPROBE")
        self.log = logging.getLogger("armory-cam")
        self.live_hub = LiveStreamHub()
        self.wallboard_cfg = WallboardConfig()
        self.wallboard = WallboardRelay(self.root)
        self.adb = adb_controller or FireTvAdbController()
        self._voice_autostart = (root_dir is None) if voice_autostart is None else bool(voice_autostart)
        self.house_audio_cfg = HouseAudioConfig()
        self.house_audio = HouseAudioManager(
            root_dir=self.root,
            config=self.house_audio_cfg,
            adb_controller=self.adb,
            allow_network=self._voice_autostart,
        )
        self.displays_cfg = GymDisplaysConfig()
        self.displays = GymDisplayController(
            config=self.displays_cfg,
            adb_controller=self.adb,
            denon_client=self.house_audio.denon,
            wake_wallboard_fn=self._wake_wallboard_sync,
            sleep_wallboard_fn=self._sleep_wallboard_sync,
            wol_sender=send_wol_magic_packet if self._voice_autostart else None,
            allow_network=self._voice_autostart,
            state_dir=self.root,
        )
        self.shows = ShowDirector(
            root_dir=self.root,
            house_audio=self.house_audio,
            displays=self.displays,
            adb_controller=self.adb,
            wallboard_cfg_fn=lambda: self.wallboard_cfg,
            stage_base_url_fn=lambda: self.effective_wallboard_url(),
            ffmpeg_bin=self.ffmpeg,
            allow_network=self._voice_autostart,
        )
        self.lock = threading.RLock()
        self.port = DEFAULT_PORT
        self.retention_days = DEFAULT_RETENTION_DAYS
        self.audio_sync_offset_ms = -570
        self.room_audio = RoomAudioRecorder(
            self.root,
            ffmpeg_bin=self.ffmpeg,
            retention_days=self.retention_days,
            sync_offset_ms=self.audio_sync_offset_ms,
        )
        self.voice_cfg = RoomVoiceConfig()
        self.voice = RoomVoiceService(
            config=self.voice_cfg,
            on_fallback_transcript=self._forward_voice_to_wallboard_source,
            allow_hardware_audio=self._voice_autostart,
            root_dir=self.root,
            house_audio_router=self.house_audio.try_route_voice_pcm,
            local_command_handler=self._handle_local_voice_command,
            on_pcm_chunk=self.room_audio.push_pcm,
            on_aec_ref_chunk=self.room_audio.push_ref_pcm,
            wake_duck_callbacks=self.house_audio.wake_duck_callbacks(),
            show_active_fn=self.shows.is_active,
        )
        self.wake_trainer = WakeWordTrainerManager(self.root)
        self.pre_roll_sec = DEFAULT_PRE_ROLL_SEC
        if drive_backup_dir is not None:
            self.drive_backup_dir = str(drive_backup_dir).strip()
        elif root_dir is None:
            self.drive_backup_dir = default_drive_backup_dir()
        else:
            self.drive_backup_dir = ""
        if ntfy_topic is not None:
            self.ntfy_topic = ntfy_topic.strip()
        elif root_dir is None:
            self.ntfy_topic = DEFAULT_NTFY_TOPIC
        else:
            self.ntfy_topic = ""
        self.cameras: list[CameraConfig] = []
        self.bump_alert_ntfy: bool = True
        self.motion_record_247: bool = True
        self._bump_strikes: dict[str, int] = {}
        self._bump_last_ntfy: dict[str, float] = {}
        self._bump_last_shift: dict[str, float] = {}
        self._bump_last_check: dict[str, float] = {}
        self._bump_overlay_cache: dict[tuple[str, int, str, str], bytes] = {}
        self._seg_dims_cache: dict[str, tuple[int, int]] = {}

        self.active_session_id: str | None = None
        self.active_session_name: str = ""
        self.recording: bool = False
        self.paused: bool = False
        self.server_armed_clip: dict[str, Any] | None = None

        self.ring_segments: dict[str, list[SegmentInfo]] = {}
        self.seen_files: set[str] = set()
        self.latest_energy: dict[str, float] = {}
        self.cam_connected: dict[str, bool] = {}
        self._last_seg_frame: dict[tuple[str, int, float], tuple[int, np.ndarray]] = {}
        self._motion_archive_until_ms: int = 0
        self._motion_soft_streak: dict[str, int] = {}
        self._copied_session_keys: dict[str, set[tuple[str, str]]] = {}
        self._scan_lock = threading.Lock()
        self._scan_active: set[str] = set()
        self._scan_progress: dict[str, tuple[int, int]] = {}
        self._stream_lock = threading.Lock()
        self._stream_procs: dict[str, tuple[subprocess.Popen[bytes], float]] = {}
        self._interactive_busy_until: float = 0.0
        self._thumb_lock = threading.Lock()
        self._thumb_mem: OrderedDict[str, bytes] = OrderedDict()
        self._preview_locks_guard = threading.Lock()
        self._preview_locks: dict[str, threading.Lock] = {}
        self._preview_refresh_lock = threading.Lock()
        self._preview_last_refresh_s: float = 0.0
        self._preview_started_at_s: float = 0.0
        self._preview_updated_ms: int | None = None
        self._preview_drive_file_id: str | None = None
        self._preview_drive_url: str | None = None
        self._preview_drive_dest_str: str = ""

        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._proc_urls: dict[str, str] = {}
        self._proc_started_mono: dict[str, float] = {}
        self._last_frame_mono: dict[str, float] = {}
        self._last_seg_mono: dict[str, float] = {}
        self._cam_watchdog_restarts: dict[str, int] = {}
        self._worker_by_slug: dict[str, threading.Thread] = {}
        self._worker_threads: list[threading.Thread] = []
        self._clips_index_lock = threading.RLock()
        self._backup_lock = threading.Lock()
        self._last_camera_ui_mono: float = 0.0
        self._last_poll_ok_ms: int = 0
        self._cal_cache_sig: tuple[Any, ...] | None = None
        self._clips_cache_sig: tuple[int, int] | None = None
        self._clips_cache_rows: list[dict[str, Any]] = []
        self._drive_neg_cache: dict[str, float] = {}
        self._last_long_prune_mono: float = 0.0
        self._stop_event = threading.Event()
        self._started = False

        self.load_or_init_config()
        self._load_bump_state()
        self._rehydrate_ring_index()

    @staticmethod
    def _build_camera_configs(
        raw_items: list[dict[str, Any]],
        prev_by_slug: dict[str, CameraConfig] | None = None,
    ) -> list[CameraConfig]:
        used_slugs: set[str] = set()
        cams: list[CameraConfig] = []
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip() or f"Camera {idx + 1}"
            base_slug = safe_slug(str(item.get("slug") or name).lower(), f"cam_{idx + 1}")
            slug = base_slug
            suffix = 2
            while slug in used_slugs:
                slug = f"{base_slug[:40]}_{suffix}"
                suffix += 1
            used_slugs.add(slug)

            old = prev_by_slug.get(slug) if prev_by_slug else None
            raw_src = str(
                item["sourceSlug"] if "sourceSlug" in item else (old.source_slug if old else "")
            ).strip()
            src_slug = safe_slug(raw_src.lower(), "") if raw_src else ""
            rtsp_url = str(item["rtspUrl"]).strip() if "rtspUrl" in item else (old.rtsp_url if old else "")
            enabled = bool(item["enabled"]) if "enabled" in item else (old.enabled if old else True)
            thresh = (
                float(item["motionThreshold"])
                if "motionThreshold" in item
                else (old.motion_threshold if old else DEFAULT_MOTION_THRESHOLD)
            )
            roi = normalize_crop(item["roi"]) if "roi" in item else (old.roi if old else (0.0, 0.0, 1.0, 1.0))
            exclusions = (
                normalize_exclusions(item["exclusions"])
                if "exclusions" in item
                else (list(old.exclusions) if old else [])
            )
            crop = normalize_crop(item["crop"]) if "crop" in item else (old.crop if old else (0.0, 0.0, 1.0, 1.0))
            rotate = normalize_rotate(item["rotate"]) if "rotate" in item else (old.rotate if old else 0)
            tilt = normalize_tilt(item["tilt"]) if "tilt" in item else (old.tilt if old else 0.0)
            defish = bool(item["defish"]) if "defish" in item else (old.defish if old else True)
            cams.append(
                CameraConfig(
                    name=name,
                    slug=slug,
                    rtsp_url=rtsp_url,
                    enabled=enabled,
                    motion_threshold=thresh,
                    roi=roi,
                    exclusions=exclusions,
                    crop=crop,
                    rotate=rotate,
                    tilt=tilt,
                    defish=defish,
                    source_slug=src_slug,
                )
            )

        by_slug = {c.slug: c for c in cams}
        for c in cams:
            if not c.source_slug or c.source_slug == c.slug or c.source_slug not in by_slug:
                c.source_slug = ""
                continue
            cur_slug = c.source_slug
            seen = {c.slug}
            for _ in range(3):
                parent = by_slug.get(cur_slug)
                if not parent or not parent.source_slug or parent.source_slug in seen or parent.source_slug not in by_slug:
                    break
                seen.add(cur_slug)
                cur_slug = parent.source_slug
            if cur_slug == c.slug or by_slug[cur_slug].source_slug:
                c.source_slug = ""
            else:
                c.source_slug = cur_slug
                c.rtsp_url = ""
        return cams

    def _source_slug_for(self, cam: CameraConfig) -> str:
        if not cam.source_slug or cam.source_slug == cam.slug:
            return cam.slug
        by_slug = {c.slug: c for c in self.cameras}
        cur = cam.source_slug
        seen = {cam.slug}
        for _ in range(3):
            parent = by_slug.get(cur)
            if not parent:
                return cam.slug
            if not parent.source_slug or parent.source_slug in seen:
                return parent.slug
            seen.add(cur)
            cur = parent.source_slug
        return cam.slug

    def _is_virtual(self, cam: CameraConfig) -> bool:
        return self._source_slug_for(cam) != cam.slug

    def _source_camera(self, cam: CameraConfig) -> CameraConfig:
        src_slug = self._source_slug_for(cam)
        for c in self.cameras:
            if c.slug == src_slug:
                return c
        return cam

    def _views_for_source(self, source_slug: str, enabled_only: bool = True) -> list[CameraConfig]:
        out: list[CameraConfig] = []
        for c in self.cameras:
            if self._source_slug_for(c) == source_slug and (not enabled_only or c.enabled):
                out.append(c)
        return out

    def _physical_cameras(self) -> list[CameraConfig]:
        return [c for c in self.cameras if not self._is_virtual(c)]

    def load_or_init_config(self) -> None:
        default_cams = [
            CameraConfig(name="Rack Cam", slug="rack_cam"),
            CameraConfig(name="Smith Cam", slug="smith_cam"),
        ]
        if not self.config_path.exists():
            self.cameras = default_cams
            self.wallboard_cfg = WallboardConfig()
            self.voice_cfg = RoomVoiceConfig()
            self.voice.update_config(self.voice_cfg.to_dict())
            self.house_audio_cfg = self.house_audio.update_config({})
            self.displays_cfg = self.displays.update_config({})
            self.save_config()
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.port = int(raw.get("port", DEFAULT_PORT))
            self.retention_days = max(1, int(raw.get("retentionDays", DEFAULT_RETENTION_DAYS)))
            self.room_audio.retention_days = self.retention_days
            if "audioSyncOffsetMs" in raw and raw.get("audioSyncOffsetMs") is not None:
                self.audio_sync_offset_ms = max(-3000, min(3000, int(raw.get("audioSyncOffsetMs"))))
            self.room_audio.sync_offset_ms = self.audio_sync_offset_ms
            self.pre_roll_sec = max(30, int(raw.get("preRollSec", DEFAULT_PRE_ROLL_SEC)))
            if "driveBackupDir" in raw:
                self.drive_backup_dir = str(raw.get("driveBackupDir") or "").strip()
            self.bump_alert_ntfy = bool(raw.get("bumpAlertNtfy", True))
            self.motion_record_247 = bool(raw.get("motionRecord247", True))
            self.wallboard_cfg = WallboardConfig.from_dict(raw.get("wallboard"))
            self.voice_cfg = RoomVoiceConfig.from_dict(raw.get("voice"))
            self.voice.update_config(self.voice_cfg.to_dict())
            ha_raw = raw.get("houseAudio") if isinstance(raw.get("houseAudio"), dict) else {}
            self.house_audio_cfg = self.house_audio.update_config(ha_raw)
            disp_raw = raw.get("displays") if isinstance(raw.get("displays"), dict) else {}
            self.displays_cfg = self.displays.update_config(disp_raw)
            raw_cams = [item for item in raw.get("cameras", []) if isinstance(item, dict) and str(item.get("name", "")).strip()]
            loaded = self._build_camera_configs(raw_cams) if raw_cams else []
            self.cameras = loaded or default_cams
        except Exception as exc:
            self.log.warning("Failed parsing config.json (%s); using defaults", exc)
            self.cameras = default_cams

    def save_config(self) -> None:
        payload = {
            "port": self.port,
            "retentionDays": self.retention_days,
            "audioSyncOffsetMs": self.audio_sync_offset_ms,
            "preRollSec": self.pre_roll_sec,
            "driveBackupDir": self.drive_backup_dir,
            "bumpAlertNtfy": self.bump_alert_ntfy,
            "motionRecord247": self.motion_record_247,
            "cameras": [c.to_dict() for c in self.cameras],
            "wallboard": self.wallboard_cfg.to_dict(),
            "voice": self.voice_cfg.to_dict(),
            "houseAudio": self.house_audio_cfg.to_dict(),
            "displays": self.displays_cfg.to_dict(),
        }
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)
        if self._started:
            self.trigger_optiplex_config_backup_async()

    def _optiplex_backup_dir(self) -> Path | None:
        raw = self.drive_backup_dir.strip()
        if not raw:
            return None
        p = Path(raw)
        base = p.parent if p.name.lower() == "clips" else p
        if not base.exists() and not base.parent.exists():
            return None
        return base / "OptiPlex"

    def backup_optiplex_configs(self) -> dict[str, Any]:
        """Mirror OptiPlex JSON configs & TTS API keys atomically to `<DriveBackupRoot>/OptiPlex/`."""
        bdir = self._optiplex_backup_dir()
        if bdir is None:
            return {"ok": False, "skipped": True, "error": "Drive backup directory unavailable"}
        names = (
            "config.json",
            "shows.json",
            "tts_secrets.json",
            "voice_ab_runs.json",
            "wallboard_standby.json",
        )
        copied: list[str] = []
        with self._backup_lock:
            bdir.mkdir(parents=True, exist_ok=True)
            for fname in names:
                src = self.root / fname
                if not src.is_file():
                    continue
                try:
                    data = src.read_bytes()
                    dst = bdir / fname
                    tmp = bdir / f".{fname}.{threading.get_ident()}.tmp"
                    tmp.write_bytes(data)
                    tmp.replace(dst)
                    copied.append(fname)
                except OSError as exc:
                    self.log.debug("OptiPlex backup skip %s: %s", fname, exc)
            now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest = {"backedUpAt": now_iso, "host": socket.gethostname(), "files": copied}
            mtmp = bdir / f".manifest.{threading.get_ident()}.tmp"
            mtmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            mtmp.replace(bdir / "manifest.json")
        return {"ok": True, "dir": str(bdir), "files": copied, "backedUpAt": now_iso}

    def trigger_optiplex_config_backup_async(self) -> None:
        if not self.drive_backup_dir.strip():
            return

        def _worker() -> None:
            try:
                self.backup_optiplex_configs()
            except Exception as exc:
                self.log.debug("OptiPlex config backup error: %s", exc)

        threading.Thread(target=_worker, name="optiplex-cfg-backup", daemon=True).start()

    def restore_optiplex_configs(self, overwrite: bool = False) -> dict[str, Any]:
        """Restore OptiPlex JSON configs & TTS secrets from `<DriveBackupRoot>/OptiPlex/` after backing up current."""
        bdir = self._optiplex_backup_dir()
        if bdir is None or not bdir.is_dir():
            return {"ok": False, "error": "OptiPlex Drive backup folder not found"}
        names = (
            "config.json",
            "shows.json",
            "tts_secrets.json",
            "voice_ab_runs.json",
            "wallboard_standby.json",
        )
        restored: list[str] = []
        skipped: list[str] = []
        pre_dir = self.root / f"_pre_restore_{int(time.time())}"
        with self._backup_lock:
            for fname in names:
                src = bdir / fname
                if not src.is_file():
                    continue
                dst = self.root / fname
                if dst.is_file() and not overwrite:
                    skipped.append(fname)
                    continue
                raw = src.read_text(encoding="utf-8")
                json.loads(raw)  # Validate JSON before touching live files
                if dst.is_file():
                    pre_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, pre_dir / fname)
                tmp = self.root / f".{fname}.restore.tmp"
                tmp.write_text(raw, encoding="utf-8")
                tmp.replace(dst)
                restored.append(fname)
        if "config.json" in restored:
            with self.lock:
                self.load_or_init_config()
                if self._started:
                    self._restart_capture_workers()
        if "shows.json" in restored or "tts_secrets.json" in restored:
            try:
                self.shows._load_or_init()
            except Exception:
                pass
        return {
            "ok": True,
            "restored": restored,
            "skipped": skipped,
            "preRestoreBackup": str(pre_dir) if pre_dir.exists() else "",
        }

    def _forward_voice_to_wallboard_source(self, transcript: str) -> bool:
        clean = str(transcript or "").strip()
        if not clean:
            return False
        with self.wallboard.cond:
            target_base = self.wallboard.source_url
        if not target_base:
            return False
        req = urllib.request.Request(
            f"{target_base}/api/voice",
            data=json.dumps({"transcript": clean}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return int(getattr(resp, "status", 200)) < 400
        except Exception:
            return False

    def effective_wallboard_url(self) -> str:
        with self.lock:
            explicit = self.wallboard_cfg.wallboard_url.strip()
            port = self.port
        if explicit:
            return explicit
        return f"http://{detect_lan_ip()}:{port}/wallboard"

    def trigger_adb_wake_async(self, force: bool = False) -> None:
        with self.adb.state_lock:
            if not force:
                if self.adb._wake_in_flight:
                    return
                if self.adb._last_wake_fail_mono > 0.0 and (time.monotonic() - self.adb._last_wake_fail_mono) < 45.0:
                    return
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
            url = self.effective_wallboard_url()

        def _worker() -> None:
            try:
                res = self.adb.wake_and_launch(cfg, url, force=force)
                if not res.get("skipped"):
                    self.log.info("Wallboard ADB wake: %s", res.get("message", res))
            except Exception as exc:
                self.adb.last_error = str(exc)

        threading.Thread(target=_worker, name="wallboard-adb-wake", daemon=True).start()

    def trigger_adb_sleep_async(self, reason: str = "", delay_sec: float = 0.0) -> None:
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())

        def _worker() -> None:
            try:
                res = self.adb.schedule_sleep(cfg, delay_sec=delay_sec, reason=reason)
                self.log.info("Wallboard ADB sleep: %s", res.get("message", res))
            except Exception as exc:
                self.adb.last_error = str(exc)

        threading.Thread(target=_worker, name="wallboard-adb-sleep", daemon=True).start()

    def update_config(self, raw: dict[str, Any]) -> None:
        with self.lock:
            prev_by_slug = {c.slug: c for c in self.cameras}
            if "retentionDays" in raw:
                self.retention_days = max(1, min(90, int(raw["retentionDays"])))
                self.room_audio.retention_days = self.retention_days
            if "audioSyncOffsetMs" in raw and raw["audioSyncOffsetMs"] is not None:
                self.audio_sync_offset_ms = max(-3000, min(3000, int(raw["audioSyncOffsetMs"])))
                self.room_audio.sync_offset_ms = self.audio_sync_offset_ms
            if "preRollSec" in raw:
                self.pre_roll_sec = max(30, min(600, int(raw["preRollSec"])))
            drive_changed = False
            if "driveBackupDir" in raw:
                next_dir = str(raw.get("driveBackupDir") or "").strip()
                drive_changed = next_dir != self.drive_backup_dir and bool(next_dir)
                self.drive_backup_dir = next_dir
            if "bumpAlertNtfy" in raw:
                self.bump_alert_ntfy = bool(raw.get("bumpAlertNtfy"))
            if "motionRecord247" in raw:
                self.motion_record_247 = bool(raw.get("motionRecord247"))
            if isinstance(raw.get("wallboard"), dict):
                merged = {**self.wallboard_cfg.to_dict(), **raw["wallboard"]}
                self.wallboard_cfg = WallboardConfig.from_dict(merged)
            if isinstance(raw.get("voice"), dict):
                self.voice_cfg = self.voice.update_config(raw["voice"])
            if isinstance(raw.get("houseAudio"), dict):
                self.house_audio_cfg = self.house_audio.update_config(raw["houseAudio"])
            if isinstance(raw.get("displays"), dict):
                self.displays_cfg = self.displays.update_config(raw["displays"])
            if isinstance(raw.get("cameras"), list):
                next_cams = self._build_camera_configs(raw["cameras"], prev_by_slug=prev_by_slug)
                if next_cams:
                    self.cameras = next_cams
            for cam in self.cameras:
                media_slug = self._source_slug_for(cam)
                if media_slug != cam.slug and not self.ring_segments.get(cam.slug):
                    src_segs = self.ring_segments.get(media_slug, [])
                    if src_segs:
                        self.ring_segments[cam.slug] = [
                            SegmentInfo(
                                path=s.path,
                                start_ms=s.start_ms,
                                end_ms=s.end_ms,
                                energy=0.0,
                                camera=cam.name,
                            )
                            for s in src_segs
                        ]
            self.save_config()
            for slug, old in prev_by_slug.items():
                cur = next((c for c in self.cameras if c.slug == slug), None)
                has_enabled_views = bool(cur and not self._is_virtual(cur) and self._views_for_source(slug, enabled_only=True))
                if not cur or self._is_virtual(cur) or not has_enabled_views or cur.rtsp_url != old.rtsp_url:
                    self.live_hub.stop_camera(slug)
            if self._started:
                self._restart_capture_workers()
                if drive_changed:
                    threading.Thread(target=self.backfill_drive_clips, name="drive-clip-backfill", daemon=True).start()
            bump_refresh_slugs: list[str] = []
            if self.bump_alert_ntfy and ("cameras" in raw or "bumpAlertNtfy" in raw):
                for phys in self._physical_cameras():
                    if not phys.enabled:
                        continue
                    old_p = prev_by_slug.get(phys.slug)
                    ref_missing = not (self.bump_refs_root / f"{phys.slug}.png").is_file()
                    rtsp_changed = bool(old_p is None or old_p.rtsp_url != phys.rtsp_url)
                    if ref_missing or rtsp_changed:
                        bump_refresh_slugs.append(phys.slug)
                        self._bump_strikes.pop(phys.slug, None)
        if self._started and "cameras" in raw:
            threading.Thread(target=self._purge_orphaned_thumb_dirs, name="thumb-orphan-purge", daemon=True).start()
        if bump_refresh_slugs and self._started:
            def _refresh_missing_baselines(slugs: list[str] = bump_refresh_slugs) -> None:
                for sl in slugs:
                    self.capture_bump_baseline(sl)
            threading.Thread(target=_refresh_missing_baselines, name="bump-baseline-refresh", daemon=True).start()
        if self._started and ("cameras" in raw or "driveBackupDir" in raw):
            self._trigger_preview_refresh_async()

    def _rehydrate_ring_index(self) -> None:
        phys_segs: dict[str, list[SegmentInfo]] = {}
        for phys in self._physical_cameras():
            cdir = self.ring_root / phys.slug
            cdir.mkdir(parents=True, exist_ok=True)
            segs: list[SegmentInfo] = []
            for p in sorted(cdir.glob("seg_*.ts")):
                start_ms = parse_segment_start_ms(p.name)
                if start_ms is None:
                    continue
                self.seen_files.add(str(p))
                segs.append(SegmentInfo(path=p, start_ms=start_ms, end_ms=start_ms + 2000, energy=0.0, camera=phys.name))
            phys_segs[phys.slug] = segs
        for cam in self.cameras:
            src_slug = self._source_slug_for(cam)
            base_segs = phys_segs.get(src_slug, [])
            self.ring_segments[cam.slug] = [
                SegmentInfo(path=s.path, start_ms=s.start_ms, end_ms=s.end_ms, energy=0.0, camera=cam.name)
                for s in base_segs
            ]

    def start_background(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._preview_started_at_s = time.time()
            self._stop_event.clear()
            self._restart_capture_workers()
            poll_t = threading.Thread(target=self._segment_poll_loop, name="armory-cam-poller", daemon=True)
            poll_t.start()
            adb_t = threading.Thread(target=self._adb_timer_loop, name="wallboard-adb-timers", daemon=True)
            adb_t.start()
            backfill_t = threading.Thread(target=self.backfill_drive_clips, name="drive-clip-backfill", daemon=True)
            backfill_t.start()
            purge_t = threading.Thread(target=self._purge_orphaned_thumb_dirs, name="thumb-orphan-purge", daemon=True)
            purge_t.start()
            self._worker_threads = [t for t in self._worker_threads if t.is_alive()]
            self._worker_threads.extend([poll_t, adb_t, backfill_t, purge_t])
            self.room_audio.start()
            if self._voice_autostart:
                self.voice.start()
                denon_t = threading.Thread(
                    target=self._denon_ip_control_watch_loop,
                    name="denon-ip-control-watch",
                    daemon=True,
                )
                denon_t.start()
                self._worker_threads.append(denon_t)
                guard_t = threading.Thread(
                    target=self.house_audio.denon.restore_stale_volume_guard,
                    name="denon-vol-guard-restore",
                    daemon=True,
                )
                guard_t.start()
                self._worker_threads.append(guard_t)
        self.trigger_optiplex_config_backup_async()

    def _denon_ip_control_watch_loop(self) -> None:
        """One-shot watcher: when Denon comes online, verify or safely enable IP Control = Always On."""
        if self.displays.get_denon_ip_control_state().get("state") in ("verified_always_on", "needs_review"):
            return
        delay_sec = 5.0
        while not self._stop_event.is_set():
            if self._stop_event.wait(delay_sec):
                return
            delay_sec = 30.0
            if self.displays.get_denon_ip_control_state().get("state") in ("verified_always_on", "needs_review"):
                return
            if not self.displays.is_denon_tcp_reachable(timeout=0.8):
                continue
            if self._stop_event.wait(2.0):
                return
            try:
                res = self.displays.ensure_denon_ip_control_always_on()
            except Exception as exc:
                self.log.debug("Denon IP Control watcher error: %s", exc)
                delay_sec = 60.0
                continue
            state = res.get("state")
            if state == "verified_always_on":
                self.log.info("Denon AVR-3313CI IP Control verified Always On: %s", res)
                if self.ntfy_topic:
                    send_ntfy_alert(
                        self.ntfy_topic,
                        "Denon IP Control Enabled",
                        "Denon AVR-3313CI IP Control is now verified Always On. Voice 'Open the gym' can wake the Denon!",
                        tags="white_check_mark,loud_sound",
                    )
                return
            if state == "needs_review":
                self.log.info("Denon network web pages captured for review: %s", res)
                if self.ntfy_topic:
                    send_ntfy_alert(
                        self.ntfy_topic,
                        "Denon Network Setup Captured",
                        "Denon came online and its Network web pages were saved safely to denon_network_dump.json "
                        "(nothing modified). Tell Antigravity to inspect it, or set Setup > Network > IP Control > Always On on the Denon remote.",
                        tags="information_source,loud_sound",
                    )
                return
            delay_sec = 60.0

    def stop_background(self) -> None:
        t0 = time.monotonic()
        self.log.info("Shutting down ArmoryCamService background workers...")
        self._stop_event.set()
        self.shows.stop_active_show()
        self.voice.stop()
        self.room_audio.stop()
        self.live_hub.stop_all()
        with self._stream_lock:
            s_procs = [p for p, _ in self._stream_procs.values()]
            self._stream_procs.clear()
        for sp in s_procs:
            try:
                sp.terminate()
            except Exception:
                pass
        with self.lock:
            procs = list(self._procs.values())
            self._procs.clear()
            self._proc_urls.clear()
            self._started = False
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        deadline = time.monotonic() + 2.5
        for proc in procs:
            rem = max(0.05, deadline - time.monotonic())
            try:
                proc.wait(timeout=rem)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.log.info("ArmoryCamService stopped in %.2fs", time.monotonic() - t0)

    def _stop_proc(self, slug: str) -> None:
        self._proc_urls.pop(slug, None)
        proc = self._procs.pop(slug, None)
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _restart_capture_workers(self) -> None:
        phys_by_slug = {c.slug: c for c in self._physical_cameras()}
        for slug in list(self._procs.keys()):
            cur = phys_by_slug.get(slug)
            has_views = bool(cur and self._views_for_source(slug, enabled_only=True))
            if not cur or not has_views or not cur.rtsp_url or self._proc_urls.get(slug) != cur.rtsp_url:
                self._stop_proc(slug)
        for cam in self.cameras:
            self.ring_segments.setdefault(cam.slug, [])
        self._worker_threads = [t for t in self._worker_threads if t.is_alive()]
        for cam in self._physical_cameras():
            (self.ring_root / cam.slug).mkdir(parents=True, exist_ok=True)
            has_views = bool(self._views_for_source(cam.slug, enabled_only=True))
            if has_views and cam.rtsp_url:
                existing_t = self._worker_by_slug.get(cam.slug)
                if existing_t and existing_t.is_alive() and self._proc_urls.get(cam.slug) == cam.rtsp_url:
                    continue
                self.cam_connected[cam.slug] = False
                self._proc_urls[cam.slug] = cam.rtsp_url
                t = threading.Thread(
                    target=self._rtsp_capture_loop,
                    args=(cam,),
                    name=f"rtsp-{cam.slug}",
                    daemon=True,
                )
                t.start()
                self._worker_by_slug[cam.slug] = t
                self._worker_threads.append(t)
            else:
                self.cam_connected[cam.slug] = False

    def _rtsp_capture_loop(self, cam: CameraConfig) -> None:
        out_dir = self.ring_root / cam.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / "seg_%Y%m%d_%H%M%S.ts")
        backoff = 2.0
        while not self._stop_event.is_set():
            with self.lock:
                cur = next((c for c in self.cameras if c.slug == cam.slug), None)
                has_views = bool(cur and not self._is_virtual(cur) and self._views_for_source(cam.slug, enabled_only=True))
                if not cur or not has_views or cur.rtsp_url != cam.rtsp_url or not self._started:
                    return
            # Single RTSP connection outputting both 2s MPEG-TS ring segments AND 10fps MJPEG preview frames.
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "10000000",
                "-fflags",
                "+genpts",
                "-i",
                cam.rtsp_url,
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                pattern,
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "fps=10,scale=960:-2",
                "-q:v",
                "7",
                "-f",
                "mpjpeg",
                "pipe:1",
            ]
            t_start = time.time()
            proc = None
            err_lines: list[str] = []
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **_subprocess_no_window_kwargs(),
                )
                _assign_child_to_job(proc)
                with self.lock:
                    self._procs[cam.slug] = proc
                    self._proc_started_mono[cam.slug] = time.monotonic()
                    self._last_frame_mono[cam.slug] = 0.0
                    self._last_seg_mono[cam.slug] = 0.0

                def drain_stderr(pipe=proc.stderr, sink=err_lines):
                    if not pipe:
                        return
                    try:
                        for line in pipe:
                            txt = line.decode("utf-8", errors="ignore").strip()
                            if txt:
                                sink.append(txt)
                                if len(sink) > 6:
                                    del sink[:-6]
                    except Exception:
                        pass

                err_t = threading.Thread(target=drain_stderr, daemon=True)
                err_t.start()

                buf = bytearray()
                got_frame = False
                assert proc.stdout is not None
                while not self._stop_event.is_set():
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        s = buf.find(b"\xff\xd8")
                        if s < 0:
                            if len(buf) > 65_536:
                                del buf[:-2]
                            break
                        e = buf.find(b"\xff\xd9", s + 2)
                        if e < 0:
                            if s > 0:
                                del buf[:s]
                            break
                        jpg = bytes(buf[s : e + 2])
                        del buf[: e + 2]
                        if not got_frame:
                            got_frame = True
                            self.cam_connected[cam.slug] = True
                        self._last_frame_mono[cam.slug] = time.monotonic()
                        self.live_hub.publish_frame(cam.slug, jpg)

                proc.wait(timeout=3)
                err_t.join(timeout=1)
                self.cam_connected[cam.slug] = False
                with self.lock:
                    if self._procs.get(cam.slug) is proc:
                        self._procs.pop(cam.slug, None)
                        self._proc_started_mono.pop(cam.slug, None)
                if self._stop_event.is_set():
                    return
                if err_lines:
                    self.log.warning("[%s] FFmpeg exited (%s): %s", cam.name, proc.returncode, err_lines[-1])
            except Exception as exc:
                self.cam_connected[cam.slug] = False
                self.log.warning("[%s] RTSP worker error: %s", cam.name, exc)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            uptime = time.time() - t_start
            backoff = 2.0 if uptime > 20.0 else min(15.0, backoff * 1.5)
            self._stop_event.wait(backoff)

    def _adb_timer_loop(self) -> None:
        """Runs Wallboard idle/post-workout ADB sleep checks off the DVR segment poller thread."""
        while not self._stop_event.is_set():
            try:
                with self.lock:
                    wb_cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
                with self.wallboard.cond:
                    last_active_ms = self.wallboard.last_active_push_ms
                    workout_active = self.wallboard.workout_active
                res = self.adb.check_timers(wb_cfg, last_active_ms, workout_active)
                if isinstance(res, dict) and res.get("ok") and not res.get("skipped"):
                    if self.wallboard.clear_stale_active(wb_cfg.idle_sleep_sec):
                        self.voice.set_wallboard_active(False)
            except Exception as exc:
                self.log.debug("ADB timer loop error: %s", exc)
            self._stop_event.wait(2.0)

    def check_camera_stall_watchdog(
        self,
        now_mono: float | None = None,
        *,
        stall_sec: float = 20.0,
    ) -> list[str]:
        """Kill & trigger auto-reconnect for any physical camera whose FFmpeg process hung silently.

        Detects stalls where FFmpeg stays alive (blocking `proc.stdout.read`) without emitting
        either fresh MJPEG preview frames or non-empty `.ts` ring segments for `>= stall_sec`.
        """
        now_m = time.monotonic() if now_mono is None else float(now_mono)
        threshold = max(5.0, float(stall_sec))
        to_kill: list[tuple[CameraConfig, subprocess.Popen[bytes], float, float]] = []
        with self.lock:
            phys_cams = [c for c in self._physical_cameras() if c.enabled]
            for cam in phys_cams:
                proc = self._procs.get(cam.slug)
                started = float(self._proc_started_mono.get(cam.slug, 0.0) or 0.0)
                if proc is None or started <= 0.0:
                    continue
                try:
                    if proc.poll() is not None:
                        continue
                except Exception:
                    continue
                uptime = now_m - started
                if uptime < threshold:
                    continue
                last_frame = float(self._last_frame_mono.get(cam.slug, 0.0) or 0.0)
                last_seg = float(self._last_seg_mono.get(cam.slug, 0.0) or 0.0)
                frame_age = (now_m - last_frame) if last_frame > 0.0 else uptime
                seg_age = (now_m - last_seg) if last_seg > 0.0 else uptime
                if frame_age >= threshold or seg_age >= threshold:
                    self.cam_connected[cam.slug] = False
                    self._procs.pop(cam.slug, None)
                    self._proc_started_mono.pop(cam.slug, None)
                    self._cam_watchdog_restarts[cam.slug] = self._cam_watchdog_restarts.get(cam.slug, 0) + 1
                    to_kill.append((cam, proc, frame_age, seg_age))

        restarted: list[str] = []
        for cam, proc, frame_age, seg_age in to_kill:
            self.log.warning(
                "[%s] RTSP stall watchdog: killing hung FFmpeg PID=%s (frame_age=%.1fs, seg_age=%.1fs)",
                cam.name,
                getattr(proc, "pid", "?"),
                frame_age,
                seg_age,
            )
            try:
                proc.kill()
            except Exception:
                pass
            restarted.append(cam.slug)
        return restarted

    def _is_source_connected_locked(self, src_slug: str, now_mono: float | None = None) -> bool:
        """Return True only when the physical camera is connected AND its latest frame is fresh."""
        if not self.cam_connected.get(src_slug, False):
            return False
        last_frame = float(self._last_frame_mono.get(src_slug, 0.0) or 0.0)
        if last_frame > 0.0:
            now_m = time.monotonic() if now_mono is None else float(now_mono)
            if (now_m - last_frame) > 15.0:
                return False
        return True

    def _segment_poll_loop(self) -> None:
        last_prune = 0.0
        last_bump = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            try:
                with self.lock:
                    phys_cams = self._physical_cameras()
                for cam in phys_cams:
                    try:
                        self._ingest_closed_segments(cam, now, compute_energy=True)
                    except Exception as exc:
                        self.log.exception("[%s] Segment ingest error: %s", cam.name, exc)
                try:
                    self.check_camera_stall_watchdog()
                except Exception as exc:
                    self.log.debug("Camera stall watchdog error: %s", exc)
                if now - last_bump > 30.0:
                    last_bump = now
                    try:
                        self.check_camera_bump_background(now)
                    except Exception as exc:
                        self.log.debug("Background bump check error: %s", exc)
                enabled_phys_slugs = [c.slug for c in phys_cams if c.enabled]
                should_refresh_preview = False
                with self.lock:
                    if enabled_phys_slugs:
                        if self._preview_last_refresh_s == 0.0:
                            all_conn = all(self.cam_connected.get(s, False) for s in enabled_phys_slugs)
                            any_ready = any(
                                self.cam_connected.get(s, False) or bool(self.ring_segments.get(s))
                                for s in enabled_phys_slugs
                            )
                            if all_conn or (any_ready and (now - self._preview_started_at_s) >= 12.0):
                                self._preview_last_refresh_s = now
                                should_refresh_preview = True
                        elif (
                            (now - self._preview_last_refresh_s) >= PREVIEW_DRIVE_INTERVAL_SEC
                            and not (self.recording and not self.paused)
                        ):
                            self._preview_last_refresh_s = now
                            should_refresh_preview = True
                if should_refresh_preview:
                    try:
                        self._trigger_preview_refresh_async()
                    except Exception as exc:
                        self.log.debug("Preview refresh trigger error: %s", exc)
                if now - last_prune > 60.0:
                    last_prune = now
                    try:
                        self._prune_storage(now)
                    except Exception as exc:
                        self.log.exception("Storage prune error: %s", exc)
                self._last_poll_ok_ms = int(time.time() * 1000)
            except Exception as exc:
                self.log.exception("Segment poll loop unexpected error: %s", exc)
            self._stop_event.wait(0.8)

    def _ingest_closed_segments(
        self,
        cam: CameraConfig,
        now_s: float,
        *,
        compute_energy: bool = True,
    ) -> None:
        cdir = self.ring_root / cam.slug
        if not cdir.exists():
            return
        files = sorted(cdir.glob("seg_*.ts"))
        for p in files:
            key = str(p)
            if key in self.seen_files:
                continue
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            if st.st_size == 0 and (now_s - st.st_mtime) > 30.0:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if now_s - st.st_mtime < 0.7 or st.st_size < 1024:
                continue
            self._last_seg_mono[cam.slug] = time.monotonic()
            start_ms = parse_segment_start_ms(p.name)
            if start_ms is None:
                start_ms = int(st.st_mtime * 1000) - 2000
            with self.lock:
                views = self._views_for_source(cam.slug, enabled_only=True)
            if not views:
                with self.lock:
                    self.seen_files.add(key)
                continue

            if compute_energy:
                dur_ms, raw_frames, first_full = extract_ts_sampled_frames_with_head(p)
            else:
                dur_ms, raw_frames, first_full = 2000, [], None
            rot_cache: dict[tuple[int, float], list[np.ndarray]] = {}
            view_results: list[tuple[CameraConfig, SegmentInfo]] = []
            for v in views:
                if raw_frames:
                    rot_v = normalize_rotate(v.rotate)
                    tilt_v = normalize_tilt(v.tilt)
                    tkey = (rot_v, tilt_v)
                    if tkey not in rot_cache:
                        rot_cache[tkey] = (
                            [
                                apply_frame_transform(frm, rotate=rot_v, tilt=tilt_v, apply_crop=False)
                                for frm in raw_frames
                            ]
                            if tkey != (0, 0.0)
                            else raw_frames
                        )
                    tframes = rot_cache[tkey]
                    prev_entry = self._last_seg_frame.get((v.slug, rot_v, tilt_v))
                    prev_frm = (
                        prev_entry[1]
                        if prev_entry is not None and 0 < (start_ms - prev_entry[0]) <= 6000
                        else None
                    )
                    energy = (
                        compute_motion_energy_from_frames(
                            tframes, roi=v.effective_roi(), exclusions=v.exclusions, prev_frame=prev_frm
                        )
                        if tframes
                        else 0.0
                    )
                    if tframes:
                        self._last_seg_frame[(v.slug, rot_v, tilt_v)] = (start_ms + dur_ms, tframes[-1])
                else:
                    energy = 0.0
                info = SegmentInfo(
                    path=p,
                    start_ms=start_ms,
                    end_ms=start_ms + dur_ms,
                    energy=energy,
                    camera=v.name,
                )
                view_results.append((v, info))

            archived_views: list[tuple[CameraConfig, SegmentInfo]] = []
            with self.lock:
                self.seen_files.add(key)
                for v, info in view_results:
                    if compute_energy:
                        self.latest_energy[v.slug] = info.energy
                    bucket = self.ring_segments.setdefault(v.slug, [])
                    bucket.append(info)
                    if len(bucket) > 600:
                        del bucket[:-600]
                if self.recording and not self.paused and self.active_session_id:
                    for v, info in view_results:
                        self._copy_segment_to_session(self.active_session_id, v, info)
                    archived_views = list(view_results)
                elif self.motion_record_247 and compute_energy and view_results:
                    triggered = False
                    for v, info in view_results:
                        soft_th = max(1.0, float(v.motion_threshold) * 0.65)
                        hard_th = max(1.8, float(v.motion_threshold) * 0.95)
                        if info.energy >= soft_th:
                            self._motion_soft_streak[v.slug] = self._motion_soft_streak.get(v.slug, 0) + 1
                        else:
                            self._motion_soft_streak[v.slug] = 0
                        if info.energy >= hard_th or self._motion_soft_streak.get(v.slug, 0) >= 2:
                            triggered = True
                    seg_s0 = view_results[0][1].start_ms
                    seg_e0 = view_results[0][1].end_ms
                    if triggered:
                        motion_sid = f"motion_{datetime.fromtimestamp(seg_s0 / 1000.0).strftime('%Y-%m-%d')}"
                        self._ensure_motion_session_meta(motion_sid, seg_s0)
                        was_idle = seg_s0 > self._motion_archive_until_ms
                        self._motion_archive_until_ms = max(
                            self._motion_archive_until_ms, seg_e0 + MOTION_POST_ROLL_SEC * 1000
                        )
                        if was_idle:
                            pre_cutoff = seg_s0 - (MOTION_PRE_ROLL_SEC * 1000)
                            for c_all in self.cameras:
                                if not c_all.enabled:
                                    continue
                                for r_seg in self.ring_segments.get(c_all.slug, []):
                                    if r_seg.end_ms >= pre_cutoff and r_seg.start_ms <= seg_e0:
                                        self._copy_segment_to_session(motion_sid, c_all, r_seg)
                    if seg_s0 <= self._motion_archive_until_ms:
                        motion_sid = f"motion_{datetime.fromtimestamp(seg_s0 / 1000.0).strftime('%Y-%m-%d')}"
                        self._ensure_motion_session_meta(motion_sid, seg_s0)
                        for v, info in view_results:
                            self._copy_segment_to_session(motion_sid, v, info)
                        archived_views = list(view_results)

            # Pre-cache 280px scrub thumbnails outside self.lock reusing the already-decoded
            # opening frame (zero extra H.264 decode) so scrub packs & bursts hit 100% cache (~100ms).
            if archived_views and first_full is not None:
                for v, info in archived_views:
                    try:
                        self._get_or_create_seg_thumb(
                            v,
                            info.path,
                            max_w=DEFAULT_SCRUB_THUMB_W,
                            predecoded_frame=first_full,
                        )
                    except Exception:
                        pass

    def _ensure_motion_session_meta(self, motion_sid: str, started_ms: int) -> None:
        sdir = self.tapes_root / safe_slug(motion_sid, "motion")
        sdir.mkdir(parents=True, exist_ok=True)
        meta_path = sdir / "session.json"
        if not meta_path.is_file():
            try:
                meta_path.write_text(
                    json.dumps(
                        {
                            "sessionId": motion_sid,
                            "name": "24/7 Motion",
                            "kind": "motion",
                            "startedMs": int(started_ms),
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass

    def _copy_segment_to_session(self, session_id: str, cam: CameraConfig, info: SegmentInfo) -> None:
        clean_id = safe_slug(session_id, "session")
        media_slug = self._source_slug_for(cam)
        sdir = self.tapes_root / clean_id / media_slug
        sdir.mkdir(parents=True, exist_ok=True)
        dst = sdir / info.path.name
        if not dst.exists() and info.path.exists():
            try:
                os.link(info.path, dst)
            except OSError:
                try:
                    shutil.copy2(info.path, dst)
                except OSError:
                    return
        motion_file = self.tapes_root / clean_id / "motion.jsonl"
        seen_set = self._copied_session_keys.get(clean_id)
        if seen_set is None:
            seen_set = set()
            if motion_file.is_file():
                try:
                    for line in motion_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        seen_set.add((str(obj.get("slug") or obj.get("camera") or ""), str(obj.get("file") or "")))
                except Exception:
                    pass
            self._copied_session_keys[clean_id] = seen_set
        dedup_key = (cam.slug, info.path.name)
        if dedup_key in seen_set:
            return
        seen_set.add(dedup_key)
        sample = {
            "camera": cam.name,
            "slug": cam.slug,
            "file": info.path.name,
            "startMs": info.start_ms,
            "endMs": info.end_ms,
            "energy": info.energy,
            "v": MOTION_RESCAN_VERSION,
        }
        try:
            with motion_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample) + "\n")
        except OSError as exc:
            self.log.debug("Failed writing motion.jsonl: %s", exc)

    def _purge_orphaned_thumb_dirs(self) -> int:
        """Delete stale crop-key thumbnail directories no longer matching active camera views."""
        if not self.thumbs_root.is_dir():
            return 0
        with self.lock:
            expected: dict[str, set[str]] = {}
            for c in self.cameras:
                expected.setdefault(c.slug, set()).add(self._crop_cache_key(c))
        removed = 0
        try:
            with os.scandir(self.thumbs_root) as cam_entries:
                for cam_ent in cam_entries:
                    if not cam_ent.is_dir(follow_symlinks=False):
                        continue
                    valid_keys = expected.get(cam_ent.name, set())
                    try:
                        with os.scandir(cam_ent.path) as ck_entries:
                            for ck_ent in ck_entries:
                                if ck_ent.is_dir(follow_symlinks=False) and ck_ent.name not in valid_keys:
                                    shutil.rmtree(ck_ent.path, ignore_errors=True)
                                    removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def _prune_ring(self, now_s: float) -> None:
        ring_cutoff = now_s - max(RING_KEEP_MIN_SEC, self.pre_roll_sec * 2)
        with self.lock:
            phys_cams = self._physical_cameras()
        live_paths: set[str] = set()
        for cam in phys_cams:
            cdir = self.ring_root / cam.slug
            if not cdir.is_dir():
                continue
            try:
                with os.scandir(cdir) as entries:
                    for ent in entries:
                        if not ent.name.startswith("seg_") or not ent.name.endswith(".ts"):
                            continue
                        try:
                            if ent.stat(follow_symlinks=False).st_mtime < ring_cutoff:
                                Path(ent.path).unlink(missing_ok=True)
                            else:
                                live_paths.add(ent.path)
                        except OSError:
                            pass
            except OSError:
                pass
        with self.lock:
            cutoff_ms = int(ring_cutoff * 1000)
            for slug, segs in self.ring_segments.items():
                self.ring_segments[slug] = [s for s in segs if s.end_ms >= cutoff_ms]
            self.seen_files.intersection_update(live_paths)

    def _prune_long_term(self, now_s: float) -> None:
        tape_cutoff = now_s - (self.retention_days * 86_400)
        active_slug = safe_slug(self.active_session_id, "session") if self.active_session_id else ""
        if self.tapes_root.is_dir():
            try:
                with os.scandir(self.tapes_root) as entries:
                    for ent in entries:
                        if not ent.is_dir(follow_symlinks=False) or ent.name == active_slug:
                            continue
                        try:
                            if ent.stat(follow_symlinks=False).st_mtime < tape_cutoff:
                                shutil.rmtree(ent.path, ignore_errors=True)
                        except OSError:
                            pass
            except OSError:
                pass

        # Free-disk safety trim (< 15 GB free -> prune oldest inactive tapes)
        try:
            min_free = 15 * (1024**3)
            if self.tapes_root.is_dir() and shutil.disk_usage(self.root).free < min_free:
                cands: list[tuple[float, str]] = []
                with os.scandir(self.tapes_root) as entries:
                    for ent in entries:
                        if ent.is_dir(follow_symlinks=False) and ent.name != active_slug:
                            try:
                                cands.append((ent.stat(follow_symlinks=False).st_mtime, ent.path))
                            except OSError:
                                pass
                cands.sort(key=lambda x: x[0])
                for _, spath in cands:
                    if shutil.disk_usage(self.root).free >= min_free:
                        break
                    shutil.rmtree(spath, ignore_errors=True)
        except OSError:
            pass

        if self.thumbs_root.is_dir():
            stack = [self.thumbs_root]
            dirs_bottom_up: list[Path] = []
            while stack:
                cur_dir = stack.pop()
                try:
                    with os.scandir(cur_dir) as entries:
                        for ent in entries:
                            if ent.is_dir(follow_symlinks=False):
                                sub = Path(ent.path)
                                stack.append(sub)
                                dirs_bottom_up.append(sub)
                            elif ent.name.endswith(".jpg"):
                                try:
                                    if ent.stat(follow_symlinks=False).st_mtime < tape_cutoff:
                                        Path(ent.path).unlink(missing_ok=True)
                                except OSError:
                                    pass
                except OSError:
                    pass
            for dir_p in reversed(dirs_bottom_up):
                try:
                    with os.scandir(dir_p) as sub_it:
                        if next(sub_it, None) is None:
                            dir_p.rmdir()
                except OSError:
                    pass

        self.room_audio.prune(now_s, self.retention_days)

        if self.previews_root.is_dir():
            prev_cutoff = now_s - PREVIEW_KEEP_SEC
            kept_previews: list[tuple[float, Path]] = []
            try:
                with os.scandir(self.previews_root) as entries:
                    for ent in entries:
                        if not ent.is_file(follow_symlinks=False):
                            continue
                        p = Path(ent.path)
                        if ent.name.startswith("."):
                            try:
                                if ent.stat(follow_symlinks=False).st_mtime < now_s - 300:
                                    p.unlink(missing_ok=True)
                            except OSError:
                                pass
                            continue
                        if not ent.name.endswith(".mp4"):
                            continue
                        try:
                            mtime = ent.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            continue
                        if mtime < prev_cutoff:
                            try:
                                p.unlink(missing_ok=True)
                            except OSError:
                                pass
                        else:
                            kept_previews.append((mtime, p))
            except OSError:
                pass
            if len(kept_previews) > PREVIEW_MAX_FILES:
                kept_previews.sort(key=lambda item: item[0])
                for _, stale_p in kept_previews[: len(kept_previews) - PREVIEW_MAX_FILES]:
                    try:
                        stale_p.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _prune_storage(self, now_s: float, *, force_long: bool = False) -> None:
        self._prune_ring(now_s)
        now_m = time.monotonic()
        if force_long or self._last_long_prune_mono == 0.0 or (now_m - self._last_long_prune_mono) >= 900.0:
            self._last_long_prune_mono = now_m
            self._prune_long_term(now_s)

    def start_session(self, session_id: str, name: str = "") -> None:
        clean_id = safe_slug(session_id, f"sess_{int(time.time())}")
        with self.lock:
            self.active_session_id = clean_id
            self.active_session_name = name
            self.recording = True
            self.paused = False
            sdir = self.tapes_root / clean_id
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "session.json").write_text(
                json.dumps(
                    {
                        "sessionId": session_id,
                        "name": name,
                        "kind": "workout",
                        "startedMs": int(time.time() * 1000),
                    }
                ),
                encoding="utf-8",
            )
            pre_cutoff = int(time.time() * 1000) - (self.pre_roll_sec * 1000)
            for cam in self.cameras:
                for info in self.ring_segments.get(cam.slug, []):
                    if info.end_ms >= pre_cutoff:
                        self._copy_segment_to_session(clean_id, cam, info)

    def pause_session(self, session_id: str | None = None) -> None:
        with self.lock:
            if session_id and self.active_session_id and safe_slug(session_id) != self.active_session_id:
                return
            self.paused = True

    def resume_session(self, session_id: str | None = None) -> None:
        with self.lock:
            if session_id and not self.active_session_id:
                self.active_session_id = safe_slug(session_id)
            self.recording = True
            self.paused = False

    def stop_session(self, session_id: str | None = None, discard: bool = False) -> None:
        with self.lock:
            target = safe_slug(session_id) if session_id else self.active_session_id
            self.recording = False
            self.paused = False
            if target and target == self.active_session_id:
                self.active_session_id = None
            if discard and target:
                shutil.rmtree(self.tapes_root / target, ignore_errors=True)

    @staticmethod
    def _normalize_source_filter(raw: str | None) -> str:
        val = str(raw or "").strip().lower()
        if val in ("workout", "workouts", "w"):
            return "workout"
        if val in ("motion", "247", "nonworkout", "m"):
            return "motion"
        return "all"

    def _tape_meta(self, clean_id: str) -> dict[str, Any]:
        sdir = self.tapes_root / clean_id
        default_kind = "motion" if clean_id.startswith("motion_") else "workout"
        meta_file = sdir / "session.json"
        if meta_file.is_file():
            try:
                obj = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    kind = "motion" if (obj.get("kind") == "motion" or clean_id.startswith("motion_")) else "workout"
                    name = str(obj.get("name") or "").strip()
                    if not name:
                        name = "24/7 Motion" if kind == "motion" else clean_id
                    return {
                        "sessionId": str(obj.get("sessionId") or clean_id),
                        "name": name,
                        "kind": kind,
                        "startedMs": int(obj.get("startedMs") or 0),
                    }
            except Exception:
                pass
        return {
            "sessionId": clean_id,
            "name": "24/7 Motion" if default_kind == "motion" else clean_id,
            "kind": default_kind,
            "startedMs": 0,
        }

    def _tape_first_ts_ms(self, clean_id: str) -> int | None:
        sdir = self.tapes_root / clean_id
        if not sdir.is_dir():
            return None
        best: int | None = None
        try:
            for cam_ent in os.scandir(sdir):
                if not cam_ent.is_dir():
                    continue
                with os.scandir(cam_ent.path) as it:
                    for f_ent in it:
                        nm = f_ent.name
                        if not (nm.startswith("seg_") and nm.endswith(".ts")):
                            continue
                        ms = parse_segment_start_ms(nm)
                        if ms is not None and (best is None or ms < best):
                            best = ms
        except OSError:
            pass
        return best

    def _resolve_session_entries(self, session_id: str | None, source_filter: str = "all") -> list[tuple[str, str]]:
        raw = str(session_id or "").strip()
        if not raw:
            return []
        sf = self._normalize_source_filter(source_filter)
        if raw.startswith("__day__:"):
            day_str = raw[len("__day__:") :].strip()[:10]
            if not day_str:
                return []
            day_compact = day_str.replace("-", "")
            matched: list[tuple[int, str, str, bool]] = []
            if self.tapes_root.is_dir():
                try:
                    with os.scandir(self.tapes_root) as root_it:
                        entries = list(root_it)
                except OSError:
                    entries = []
                for ent in entries:
                    if not ent.is_dir():
                        continue
                    cid = ent.name
                    meta = self._tape_meta(cid)
                    kind = meta["kind"]
                    if sf != "all" and kind != sf:
                        continue
                    belongs = cid == f"motion_{day_str}"
                    first_ms: int | None = None
                    ts_set: set[int] = set()
                    try:
                        with os.scandir(ent.path) as sub_it:
                            subs = list(sub_it)
                        for sub in subs:
                            if not sub.is_dir():
                                continue
                            with os.scandir(sub.path) as it:
                                for f_ent in it:
                                    nm = f_ent.name
                                    if not (nm.startswith("seg_") and nm.endswith(".ts")):
                                        continue
                                    ms = parse_segment_start_ms(nm)
                                    if ms is not None:
                                        ts_set.add(ms)
                                        if first_ms is None or ms < first_ms:
                                            first_ms = ms
                                    if nm.startswith(f"seg_{day_compact}_"):
                                        belongs = True
                                    if belongs and len(ts_set) >= 2 and first_ms is not None:
                                        break
                            if belongs and len(ts_set) >= 2 and first_ms is not None:
                                break
                    except OSError:
                        continue
                    if not belongs and meta.get("startedMs"):
                        try:
                            if datetime.fromtimestamp(meta["startedMs"] / 1000.0).strftime("%Y-%m-%d") == day_str:
                                belongs = True
                        except Exception:
                            pass
                    if belongs and first_ms is not None:
                        matched.append((first_ms, cid, kind, len(ts_set) <= 1))
            non_stubs = [m for m in matched if not m[3]]
            kept = non_stubs if non_stubs else matched
            kept.sort(key=lambda item: item[0])
            return [(cid, kind) for _, cid, kind, _ in kept]

        clean_id = safe_slug(raw, "session")
        meta = self._tape_meta(clean_id)
        if sf != "all" and meta["kind"] != sf:
            return []
        return [(clean_id, meta["kind"])]

    def _load_motion_v2(self, clean_id: str) -> dict[str, Any]:
        p = self.tapes_root / clean_id / "motion_v2.json"
        if not p.is_file():
            return {}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) and obj.get("version") == MOTION_RESCAN_VERSION else {}
        except Exception:
            return {}

    def _save_motion_v2(self, clean_id: str, payload: dict[str, Any]) -> None:
        sdir = self.tapes_root / clean_id
        if not sdir.is_dir():
            return
        p = sdir / "motion_v2.json"
        tmp = sdir / f".motion_v2.{threading.get_ident()}.tmp"
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _read_first_ts_frame(self, seg_path: Path, max_w: int = 480) -> np.ndarray | None:
        cap = cv2.VideoCapture(str(seg_path))
        if not cap.isOpened():
            return None
        try:
            for _ in range(4):
                ok, frm = cap.read()
                if ok and frm is not None:
                    if max_w > 0 and frm.shape[1] > max_w:
                        h2 = max(2, int(round(frm.shape[0] * (float(max_w) / float(frm.shape[1])))))
                        return cv2.resize(frm, (max_w, h2), interpolation=cv2.INTER_AREA)
                    return frm
            return None
        finally:
            cap.release()

    def _rescan_session_motion(self, clean_id: str, force: bool = False) -> bool:
        sdir = self.tapes_root / clean_id
        if not sdir.is_dir():
            return False
        with self._scan_lock:
            if clean_id in self._scan_active:
                return False
            self._scan_active.add(clean_id)
        try:
            with self.lock:
                cams = [c for c in self.cameras if c.enabled]
            v2_doc = self._load_motion_v2(clean_id)
            views_doc: dict[str, Any] = dict(v2_doc.get("views") or {}) if isinstance(v2_doc, dict) else {}

            # Map existing motion.jsonl scores so intra-segment rapid motion is preserved via max(v1, v2)
            v1_scores: dict[tuple[str, str], float] = {}
            mf = sdir / "motion.jsonl"
            if mf.is_file():
                for line in mf.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        fn = str(obj.get("file") or "")
                        eng = float(obj.get("energy", 0.0))
                        sl = str(obj.get("slug") or "")
                        cn = str(obj.get("camera") or "").lower()
                        if sl:
                            v1_scores[(sl, fn)] = max(v1_scores.get((sl, fn), 0.0), eng)
                        if cn:
                            v1_scores[(cn, fn)] = max(v1_scores.get((cn, fn), 0.0), eng)
                    except Exception:
                        continue

            by_source: dict[str, list[CameraConfig]] = {}
            for c in cams:
                src_slug = self._source_slug_for(c)
                if (sdir / src_slug).is_dir():
                    by_source.setdefault(src_slug, []).append(c)
                elif (sdir / c.slug).is_dir():
                    by_source.setdefault(c.slug, []).append(c)

            if not by_source:
                return False

            work_sources: list[tuple[str, list[Path], list[CameraConfig]]] = []
            total_segs = 0
            for src_slug, src_views in by_source.items():
                segs = sorted((sdir / src_slug).glob("seg_*.ts"))
                if not segs:
                    continue
                need_views: list[CameraConfig] = []
                for v in src_views:
                    ck = self._crop_cache_key(v)
                    existing = views_doc.get(v.slug)
                    if force or not isinstance(existing, dict) or existing.get("cropKey") != ck:
                        need_views.append(v)
                        continue
                    sc_map = existing.get("scores") or {}
                    covered = sum(1 for sp in segs if sp.name in sc_map)
                    if covered < int(len(segs) * 0.85):
                        need_views.append(v)
                if need_views:
                    work_sources.append((src_slug, segs, need_views))
                    total_segs += len(segs)

            if not work_sources:
                return False

            done_segs = 0
            with self._scan_lock:
                self._scan_progress[clean_id] = (0, max(1, total_segs))

            for src_slug, segs, need_views in work_sources:
                first_frames: list[np.ndarray | None] = []
                chunk_size = 8
                with ThreadPoolExecutor(max_workers=2) as pool:
                    for off in range(0, len(segs), chunk_size):
                        # Yield CPU immediately while streaming, or during any scrub/seek for auto-backfill
                        while not self._stop_event.is_set() and (
                            bool(self._stream_procs)
                            or (not force and time.monotonic() < self._interactive_busy_until)
                        ):
                            self._stop_event.wait(0.25)
                        if self._stop_event.is_set():
                            return False
                        batch = segs[off : off + chunk_size]
                        for frm in pool.map(self._read_first_ts_frame, batch):
                            first_frames.append(frm)
                            done_segs += 1
                            if done_segs % 25 == 0 or done_segs == total_segs:
                                with self._scan_lock:
                                    self._scan_progress[clean_id] = (done_segs, max(1, total_segs))

                seg_times = [parse_segment_start_ms(sp.name) or 0 for sp in segs]
                rot_cache: dict[tuple[int, float], list[np.ndarray | None]] = {}
                for v in need_views:
                    rot_v = normalize_rotate(v.rotate)
                    tilt_v = normalize_tilt(v.tilt)
                    tkey = (rot_v, tilt_v)
                    if tkey not in rot_cache:
                        if tkey == (0, 0.0):
                            rot_cache[tkey] = first_frames
                        else:
                            rot_cache[tkey] = [
                                apply_frame_transform(f, rotate=rot_v, tilt=tilt_v, apply_crop=False)
                                if f is not None
                                else None
                                for f in first_frames
                            ]
                    tframes = rot_cache[tkey]
                    roi_v = v.effective_roi()
                    excl_v = v.exclusions
                    scores: dict[str, float] = {}
                    for i, sp in enumerate(segs):
                        cur_f = tframes[i]
                        prev_f = (
                            tframes[i - 1]
                            if i > 0 and 0 < (seg_times[i] - seg_times[i - 1]) <= 8000
                            else None
                        )
                        inter_e = 0.0
                        if prev_f is not None and cur_f is not None:
                            inter_e = compute_motion_energy_from_frames(
                                [prev_f, cur_f], roi=roi_v, exclusions=excl_v
                            )
                        elif cur_f is not None:
                            # Isolated segment or start of a run: inspect within-segment frames
                            _, intra_raw = extract_ts_sampled_frames(sp)
                            if len(intra_raw) >= 2:
                                intra_t = (
                                    [
                                        apply_frame_transform(f, rotate=rot_v, tilt=tilt_v, apply_crop=False)
                                        for f in intra_raw
                                    ]
                                    if tkey != (0, 0.0)
                                    else intra_raw
                                )
                                inter_e = compute_motion_energy_from_frames(
                                    intra_t, roi=roi_v, exclusions=excl_v
                                )
                        old_v1 = max(
                            v1_scores.get((v.slug, sp.name), 0.0),
                            v1_scores.get((v.name.lower(), sp.name), 0.0),
                        )
                        scores[sp.name] = round(max(old_v1, inter_e), 2)
                    views_doc[v.slug] = {"cropKey": self._crop_cache_key(v), "scores": scores}

            self._save_motion_v2(clean_id, {"version": MOTION_RESCAN_VERSION, "views": views_doc})
            return True
        finally:
            with self._scan_lock:
                self._scan_active.discard(clean_id)
                self._scan_progress.pop(clean_id, None)

    def _trigger_session_rescan_async(self, clean_id: str, force: bool = False) -> bool:
        sdir = self.tapes_root / clean_id
        if not sdir.is_dir():
            return False
        with self._scan_lock:
            if clean_id in self._scan_active:
                return False

        def _worker() -> None:
            try:
                self._rescan_session_motion(clean_id, force=force)
            except Exception as exc:
                self.log.debug("Background motion v2 scan error (%s): %s", clean_id, exc)

        threading.Thread(target=_worker, name=f"motion-scan-{clean_id[:16]}", daemon=True).start()
        return True

    def rescan_tapes(self, session_id: str, force: bool = True, source_filter: str = "all") -> dict[str, Any]:
        entries = self._resolve_session_entries(session_id, source_filter)
        started = 0
        scanning = 0
        for cid, _kind in entries:
            with self._scan_lock:
                if cid in self._scan_active:
                    scanning += 1
                    continue
            if self._trigger_session_rescan_async(cid, force=force):
                started += 1
        return {
            "ok": True,
            "started": started,
            "scanning": started + scanning,
            "scanStatus": "scanning" if (started + scanning) > 0 else "ready",
        }

    def session_samples(self, session_id: str | None = None, source_filter: str = "all") -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        with self.lock:
            active_slug = safe_slug(self.active_session_id, "session") if self.active_session_id else ""
            cams_by_slug = {c.slug: c for c in self.cameras}
            cams_by_name = {c.name.lower(): c for c in self.cameras}

        if session_id:
            entries = self._resolve_session_entries(session_id, source_filter)
            include_ring = False
            for clean_id, kind in entries:
                kind_ch = "m" if kind == "motion" else "w"
                if clean_id == active_slug:
                    include_ring = True
                v2_doc = self._load_motion_v2(clean_id)
                v2_views = v2_doc.get("views") if isinstance(v2_doc.get("views"), dict) else {}
                v2_score_by_slug: dict[str, dict[str, float]] = {}
                for v_slug, v_entry in v2_views.items():
                    if not isinstance(v_entry, dict):
                        continue
                    cfg_cam = cams_by_slug.get(v_slug)
                    if cfg_cam and v_entry.get("cropKey") and v_entry.get("cropKey") != self._crop_cache_key(cfg_cam):
                        continue
                    sc = v_entry.get("scores")
                    if isinstance(sc, dict):
                        v2_score_by_slug[v_slug] = sc

                motion_file = self.tapes_root / clean_id / "motion.jsonl"
                seen_in_session: set[tuple[str, str]] = set()
                if motion_file.exists():
                    for line in motion_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                            fn = str(obj.get("file", ""))
                            cam_name = str(obj.get("camera", ""))
                            slug = str(obj.get("slug") or "")
                            if not slug and cam_name.lower() in cams_by_name:
                                slug = cams_by_name[cam_name.lower()].slug
                            k = (cam_name, fn)
                            v2_e = float(v2_score_by_slug.get(slug, {}).get(fn, 0.0))
                            eng = max(float(obj.get("energy", 0.0)), v2_e)
                            seen_in_session.add((slug or cam_name.lower(), fn))
                            if k not in seen_keys:
                                seen_keys.add(k)
                                samples.append(
                                    {
                                        **obj,
                                        "slug": slug or obj.get("slug", ""),
                                        "energy": round(eng, 3),
                                        "kind": kind_ch,
                                        "sid": clean_id,
                                    }
                                )
                        except json.JSONDecodeError:
                            continue

                # Emit samples for virtual/new views present in motion_v2.json that weren't in motion.jsonl
                for v_slug, sc_map in v2_score_by_slug.items():
                    cfg_cam = cams_by_slug.get(v_slug)
                    cam_name = cfg_cam.name if cfg_cam else v_slug
                    for fn, raw_e in sc_map.items():
                        if (v_slug, fn) in seen_in_session or (cam_name, fn) in seen_keys:
                            continue
                        s_ms = parse_segment_start_ms(fn)
                        if s_ms is None:
                            continue
                        seen_keys.add((cam_name, fn))
                        samples.append(
                            {
                                "camera": cam_name,
                                "slug": v_slug,
                                "file": fn,
                                "startMs": s_ms,
                                "endMs": s_ms + 3000,
                                "energy": round(float(raw_e), 3),
                                "kind": kind_ch,
                                "sid": clean_id,
                            }
                        )
        else:
            include_ring = True

        if include_ring:
            with self.lock:
                for cam in self.cameras:
                    for info in self.ring_segments.get(cam.slug, []):
                        k = (info.camera, info.path.name)
                        if k not in seen_keys:
                            seen_keys.add(k)
                            samples.append(
                                {
                                    "camera": info.camera,
                                    "slug": cam.slug,
                                    "file": info.path.name,
                                    "startMs": info.start_ms,
                                    "endMs": info.end_ms,
                                    "energy": info.energy,
                                    "kind": "l",
                                    "sid": "",
                                }
                            )
        samples.sort(key=lambda x: int(x["startMs"]))
        return samples

    def _scan_ring_disk_segments(
        self,
        media_slug: str,
        slug: str,
        canon_name: str,
        by_name: dict[str, SegmentInfo],
    ) -> set[str]:
        """Scan raw ring `.ts` files on disk so clip cutting never waits on ingest backlog."""
        inferred: set[str] = set()
        ring_dirs = [self.ring_root / media_slug]
        if slug != media_slug:
            ring_dirs.append(self.ring_root / slug)
        for ring_dir in ring_dirs:
            if not ring_dir.exists():
                continue
            for p in sorted(ring_dir.glob("seg_*.ts")):
                if p.name in by_name:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size < 1024:
                    continue
                s_ms = parse_segment_start_ms(p.name)
                if s_ms is None:
                    s_ms = int(st.st_mtime * 1000) - 3000
                e_ms = max(s_ms + 3500, min(s_ms + 10_000, int(st.st_mtime * 1000)))
                by_name[p.name] = SegmentInfo(
                    path=p,
                    start_ms=s_ms,
                    end_ms=e_ms,
                    energy=0.0,
                    camera=canon_name,
                )
                inferred.add(p.name)
        return inferred

    @staticmethod
    def _reconcile_inferred_segments(
        ordered: list[SegmentInfo],
        inferred_names: set[str],
    ) -> list[SegmentInfo]:
        if not inferred_names or len(ordered) < 2:
            return ordered
        out: list[SegmentInfo] = []
        last_idx = len(ordered) - 1
        for idx, seg in enumerate(ordered):
            if idx < last_idx and seg.path.name in inferred_names:
                next_start = ordered[idx + 1].start_ms
                gap = next_start - seg.start_ms
                if 500 <= gap <= 12_000:
                    out.append(
                        SegmentInfo(
                            path=seg.path,
                            start_ms=seg.start_ms,
                            end_ms=next_start,
                            energy=seg.energy,
                            camera=seg.camera,
                            kind=seg.kind,
                            session_id=seg.session_id,
                        )
                    )
                    continue
            out.append(seg)
        return out

    def _motion_scores_for_session(
        self,
        clean_id: str,
        slug: str,
        canon_name: str,
        cam: CameraConfig | None,
    ) -> dict[str, tuple[int, int, float]]:
        motion_map: dict[str, tuple[int, int, float]] = {}
        motion_file = self.tapes_root / clean_id / "motion.jsonl"
        if motion_file.exists():
            for line in motion_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("slug") == slug or str(obj.get("camera") or "").lower() == canon_name.lower():
                        fn = str(obj["file"])
                        prev = motion_map.get(fn)
                        eng = float(obj.get("energy", 0.0))
                        if prev is None or eng > prev[2]:
                            motion_map[fn] = (int(obj["startMs"]), int(obj["endMs"]), eng)
                except Exception:
                    continue

        v2_doc = self._load_motion_v2(clean_id)
        v2_views = v2_doc.get("views") if isinstance(v2_doc.get("views"), dict) else {}
        v2_entry = v2_views.get(slug) if isinstance(v2_views, dict) else None
        if isinstance(v2_entry, dict):
            ck_ok = (cam is None) or (not v2_entry.get("cropKey")) or (v2_entry.get("cropKey") == self._crop_cache_key(cam))
            if ck_ok and isinstance(v2_entry.get("scores"), dict):
                for fn, raw_e in v2_entry["scores"].items():
                    e_v2 = float(raw_e or 0.0)
                    prev = motion_map.get(fn)
                    if prev is not None:
                        if e_v2 > prev[2]:
                            motion_map[fn] = (prev[0], prev[1], e_v2)
                    else:
                        s_ms = parse_segment_start_ms(fn)
                        if s_ms is not None:
                            motion_map[fn] = (s_ms, s_ms + 3000, e_v2)
        return motion_map

    def find_segments(
        self,
        camera_name: str,
        session_id: str,
        start_ms: int,
        end_ms: int,
        source_filter: str = "all",
    ) -> list[SegmentInfo]:
        cam = self._find_camera(camera_name)
        slug = cam.slug if cam else safe_slug(camera_name.lower(), "rack_cam")
        media_slug = self._source_slug_for(cam) if cam else slug
        canon_name = cam.name if cam else camera_name

        by_key: dict[str, SegmentInfo] = {}
        inferred_names: set[str] = set()
        with self.lock:
            active_slug = safe_slug(self.active_session_id, "session") if self.active_session_id else ""

        entries = self._resolve_session_entries(session_id, source_filter)
        entry_ids = {cid for cid, _ in entries}
        any_dir_exists = any((self.tapes_root / cid).is_dir() for cid, _ in entries)

        for clean_id, kind in entries:
            kind_ch = "m" if kind == "motion" else "w"
            motion_map = self._motion_scores_for_session(clean_id, slug, canon_name, cam)
            tape_dirs = [self.tapes_root / clean_id / media_slug]
            if slug != media_slug:
                tape_dirs.append(self.tapes_root / clean_id / slug)
            for tape_cam_dir in tape_dirs:
                if tape_cam_dir.exists():
                    for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                        prev = by_key.get(p.name)
                        if prev is not None and prev.kind == "w":
                            continue
                        m = motion_map.get(p.name)
                        s_ms = m[0] if m else (parse_segment_start_ms(p.name) or 0)
                        e_ms = m[1] if m else (s_ms + 3000)
                        eng = m[2] if m else 0.0
                        by_key[p.name] = SegmentInfo(
                            path=p,
                            start_ms=s_ms,
                            end_ms=e_ms,
                            energy=eng,
                            camera=canon_name,
                            kind=kind_ch,
                            session_id=clean_id,
                        )
                        if not m:
                            inferred_names.add(p.name)

        include_ring = (
            (not session_id)
            or (active_slug in entry_ids)
            or (not by_key and not str(session_id or "").startswith("__day__:") and not any_dir_exists)
        )
        if include_ring:
            with self.lock:
                for info in self.ring_segments.get(slug, []):
                    if info.path.exists() and info.path.name not in by_key:
                        by_key[info.path.name] = replace(info, kind="l", session_id="")
                if media_slug != slug:
                    for info in self.ring_segments.get(media_slug, []):
                        if info.path.exists() and info.path.name not in by_key:
                            by_key[info.path.name] = SegmentInfo(
                                path=info.path,
                                start_ms=info.start_ms,
                                end_ms=info.end_ms,
                                energy=0.0,
                                camera=canon_name,
                                kind="l",
                                session_id="",
                            )

            inferred_names.update(self._scan_ring_disk_segments(media_slug, slug, canon_name, by_key))

        ordered = self._reconcile_inferred_segments(sorted(by_key.values(), key=lambda s: s.start_ms), inferred_names)
        matched = [s for s in ordered if s.end_ms >= start_ms - 2500 and s.start_ms <= end_ms + 2500]
        sf = self._normalize_source_filter(source_filter)
        is_day_query = str(session_id or "").startswith("__day__:")
        needs_gap_fill = (
            bool(session_id)
            and not is_day_query
            and sf == "all"
            and (
                not matched
                or matched[0].start_ms > start_ms + 4000
                or matched[-1].end_ms < end_ms - 4000
            )
        )
        if needs_gap_fill:
            added_fallback = False
            if not include_ring:
                with self.lock:
                    for info in self.ring_segments.get(slug, []):
                        if info.path.exists() and info.path.name not in by_key:
                            by_key[info.path.name] = replace(info, kind="l", session_id="")
                            added_fallback = True
                    if media_slug != slug:
                        for info in self.ring_segments.get(media_slug, []):
                            if info.path.exists() and info.path.name not in by_key:
                                by_key[info.path.name] = SegmentInfo(
                                    path=info.path,
                                    start_ms=info.start_ms,
                                    end_ms=info.end_ms,
                                    energy=0.0,
                                    camera=canon_name,
                                    kind="l",
                                    session_id="",
                                )
                                added_fallback = True
                ring_inf = self._scan_ring_disk_segments(media_slug, slug, canon_name, by_key)
                if ring_inf:
                    inferred_names.update(ring_inf)
                    added_fallback = True

            day_ids: list[str] = []
            for ts_ms in (start_ms, end_ms):
                if ts_ms > 0:
                    try:
                        m_id = f"motion_{datetime.fromtimestamp(ts_ms / 1000.0).strftime('%Y-%m-%d')}"
                    except (OSError, OverflowError, ValueError):
                        continue
                    if m_id not in day_ids and m_id not in entry_ids:
                        day_ids.append(m_id)
            for m_id in day_ids:
                m_dir = self.tapes_root / m_id
                if not m_dir.is_dir():
                    continue
                m_motion_map = self._motion_scores_for_session(m_id, slug, canon_name, cam)
                m_tape_dirs = [m_dir / media_slug]
                if slug != media_slug:
                    m_tape_dirs.append(m_dir / slug)
                for tape_cam_dir in m_tape_dirs:
                    if not tape_cam_dir.exists():
                        continue
                    for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                        if p.name in by_key:
                            continue
                        m = m_motion_map.get(p.name)
                        s_ms = m[0] if m else (parse_segment_start_ms(p.name) or 0)
                        e_ms = m[1] if m else (s_ms + 3000)
                        if e_ms < start_ms - 5000 or s_ms > end_ms + 5000:
                            continue
                        eng = m[2] if m else 0.0
                        by_key[p.name] = SegmentInfo(
                            path=p,
                            start_ms=s_ms,
                            end_ms=e_ms,
                            energy=eng,
                            camera=canon_name,
                            kind="m",
                            session_id=m_id,
                        )
                        if not m:
                            inferred_names.add(p.name)
                        added_fallback = True

            if added_fallback:
                ordered = self._reconcile_inferred_segments(sorted(by_key.values(), key=lambda s: s.start_ms), inferred_names)
                matched = [s for s in ordered if s.end_ms >= start_ms - 2500 and s.start_ms <= end_ms + 2500]

        return matched

    def _find_camera(self, name_or_slug: str | None) -> CameraConfig | None:
        if not name_or_slug:
            return self.cameras[0] if self.cameras else None
        q = name_or_slug.strip().lower()
        for c in self.cameras:
            if c.name.lower() == q or c.slug.lower() == q:
                return c
        return self.cameras[0] if self.cameras else None

    def _find_camera_exact(self, name_or_slug: str | None) -> CameraConfig | None:
        if not name_or_slug:
            return None
        q = name_or_slug.strip().lower()
        if not q:
            return None
        with self.lock:
            cams = list(self.cameras)
        for c in cams:
            if c.name.lower() == q or c.slug.lower() == q:
                return c
        return None

    def _list_camera_segments(
        self,
        camera_name: str,
        session_id: str,
        source_filter: str = "all",
    ) -> list[SegmentInfo]:
        cam = self._find_camera(camera_name)
        slug = cam.slug if cam else safe_slug(camera_name.lower(), "rack_cam")
        media_slug = self._source_slug_for(cam) if cam else slug
        canon_name = cam.name if cam else camera_name
        by_name: dict[str, SegmentInfo] = {}
        inferred_names: set[str] = set()

        with self.lock:
            active_slug = safe_slug(self.active_session_id, "session") if self.active_session_id else ""

        entries = self._resolve_session_entries(session_id, source_filter)
        entry_ids = {cid for cid, _ in entries}
        any_dir_exists = any((self.tapes_root / cid).is_dir() for cid, _ in entries)

        for clean_id, kind in entries:
            kind_ch = "m" if kind == "motion" else "w"
            motion_map = self._motion_scores_for_session(clean_id, slug, canon_name, cam)
            tape_dirs = [self.tapes_root / clean_id / media_slug]
            if slug != media_slug:
                tape_dirs.append(self.tapes_root / clean_id / slug)
            for tape_cam_dir in tape_dirs:
                if tape_cam_dir.exists():
                    for p in sorted(tape_cam_dir.glob("seg_*.ts")):
                        prev = by_name.get(p.name)
                        if prev is not None and prev.kind == "w":
                            continue
                        m = motion_map.get(p.name)
                        s_ms = m[0] if m else parse_segment_start_ms(p.name)
                        if s_ms is None:
                            try:
                                s_ms = int(p.stat().st_mtime * 1000) - 3000
                            except OSError:
                                continue
                        e_ms = m[1] if m else (s_ms + 3000)
                        eng = m[2] if m else 0.0
                        by_name[p.name] = SegmentInfo(
                            path=p,
                            start_ms=s_ms,
                            end_ms=e_ms,
                            energy=eng,
                            camera=canon_name,
                            kind=kind_ch,
                            session_id=clean_id,
                        )
                        if not m:
                            inferred_names.add(p.name)

        include_ring = (
            (not session_id)
            or (active_slug in entry_ids)
            or (not by_name and not str(session_id or "").startswith("__day__:") and not any_dir_exists)
        )
        if include_ring:
            with self.lock:
                for info in self.ring_segments.get(slug, []):
                    if info.path.exists() and info.path.name not in by_name:
                        by_name[info.path.name] = replace(info, kind="l", session_id="")
                if media_slug != slug:
                    for info in self.ring_segments.get(media_slug, []):
                        if info.path.exists() and info.path.name not in by_name:
                            by_name[info.path.name] = SegmentInfo(
                                path=info.path,
                                start_ms=info.start_ms,
                                end_ms=info.end_ms,
                                energy=0.0,
                                camera=canon_name,
                                kind="l",
                                session_id="",
                            )

            inferred_names.update(self._scan_ring_disk_segments(media_slug, slug, canon_name, by_name))
        return self._reconcile_inferred_segments(sorted(by_name.values(), key=lambda s: s.start_ms), inferred_names)

    @staticmethod
    def _build_runs(segs: list[SegmentInfo], gap_ms: int = TAPE_GAP_MS) -> list[dict[str, Any]]:
        if not segs:
            return []
        runs: list[dict[str, Any]] = []
        cur_s = int(segs[0].start_ms)
        cur_e = int(segs[0].end_ms)
        cur_cnt = 1
        cur_max_e = float(segs[0].energy)
        cur_kinds: set[str] = {segs[0].kind or "w"}
        cur_sid = segs[0].session_id
        for s in segs[1:]:
            s0 = int(s.start_ms)
            s1 = int(s.end_ms)
            if s0 - cur_e > gap_ms:
                runs.append(
                    {
                        "startMs": cur_s,
                        "endMs": cur_e,
                        "durationSec": max(2, int(round((cur_e - cur_s) / 1000.0))),
                        "segmentCount": cur_cnt,
                        "maxEnergy": round(cur_max_e, 2),
                        "kind": "mixed" if len(cur_kinds) > 1 else next(iter(cur_kinds)),
                        "sid": cur_sid,
                    }
                )
                cur_s, cur_e, cur_cnt, cur_max_e = s0, s1, 1, float(s.energy)
                cur_kinds = {s.kind or "w"}
                cur_sid = s.session_id
            else:
                cur_e = max(cur_e, s1)
                cur_cnt += 1
                if float(s.energy) > cur_max_e:
                    cur_max_e = float(s.energy)
                cur_kinds.add(s.kind or "w")
        runs.append(
            {
                "startMs": cur_s,
                "endMs": cur_e,
                "durationSec": max(2, int(round((cur_e - cur_s) / 1000.0))),
                "segmentCount": cur_cnt,
                "maxEnergy": round(cur_max_e, 2),
                "kind": "mixed" if len(cur_kinds) > 1 else next(iter(cur_kinds)),
                "sid": cur_sid,
            }
        )
        return runs

    @staticmethod
    def _build_runs_from_timestamps(ts_list: list[int], gap_ms: int = TAPE_GAP_MS) -> list[dict[str, Any]]:
        if not ts_list:
            return []
        ordered = sorted(set(ts_list))
        runs: list[dict[str, Any]] = []
        r0 = ordered[0]
        r1 = r0 + 2500
        cnt = 1
        for ts in ordered[1:]:
            if ts - r1 > gap_ms:
                runs.append(
                    {
                        "startMs": r0,
                        "endMs": r1,
                        "durationSec": max(2, int(round((r1 - r0) / 1000.0))),
                        "segmentCount": cnt,
                    }
                )
                r0, r1, cnt = ts, ts + 2500, 1
            else:
                r1 = max(r1, ts + 2500)
                cnt += 1
        runs.append(
            {
                "startMs": r0,
                "endMs": r1,
                "durationSec": max(2, int(round((r1 - r0) / 1000.0))),
                "segmentCount": cnt,
            }
        )
        return runs

    def _check_session_scan_needed(self, entries: list[tuple[str, str]], cam: CameraConfig | None) -> tuple[str, int]:
        if not entries or cam is None:
            return "ready", 100
        ck = self._crop_cache_key(cam)
        src_slug = self._source_slug_for(cam)
        scanning_any = False
        done_sum = 0
        tot_sum = 0
        for cid, _ in entries:
            with self._scan_lock:
                if cid in self._scan_active:
                    scanning_any = True
                    d, t = self._scan_progress.get(cid, (0, 1))
                    done_sum += d
                    tot_sum += t
                    continue
            sdir = self.tapes_root / cid
            phys_dir = sdir / src_slug
            if not phys_dir.is_dir():
                phys_dir = sdir / cam.slug
            if not phys_dir.is_dir():
                continue
            seg_names = [f.name for f in phys_dir.glob("seg_*.ts")]
            if len(seg_names) < 2:
                continue
            v2_doc = self._load_motion_v2(cid)
            v2_entry = (v2_doc.get("views") or {}).get(cam.slug) if isinstance(v2_doc.get("views"), dict) else None
            needs_scan = True
            if isinstance(v2_entry, dict) and v2_entry.get("cropKey") == ck:
                sc_map = v2_entry.get("scores") or {}
                covered = sum(1 for nm in seg_names if nm in sc_map)
                if covered >= int(len(seg_names) * 0.85):
                    needs_scan = False
            if needs_scan:
                self._trigger_session_rescan_async(cid, force=False)
                scanning_any = True
                tot_sum += len(seg_names)
        if scanning_any:
            pct = int(round((done_sum / max(1, tot_sum)) * 100))
            return "scanning", max(1, min(99, pct))
        return "ready", 100

    def tape_timeline(
        self,
        camera_name: str,
        session_id: str,
        buckets: int = 80,
        source_filter: str = "all",
    ) -> dict[str, Any]:
        cam = self._find_camera(camera_name)
        canon = cam.name if cam else (camera_name or "Rack Cam")
        thresh = float(cam.motion_threshold if cam else DEFAULT_MOTION_THRESHOLD)
        segs = self._list_camera_segments(canon, session_id, source_filter=source_filter)
        if not segs:
            return {
                "ok": True,
                "camera": canon,
                "minMs": 0,
                "maxMs": 0,
                "segmentCount": 0,
                "recordedSec": 0,
                "threshold": round(thresh, 2),
                "runs": [],
                "samples": [],
                "allCamSamples": [],
                "cameraNames": [],
                "cameraActivity": {},
                "hasAudio": False,
                "scanStatus": "ready",
                "scanPct": 100,
                "activity": [],
                "bursts": [],
                "bursts30": [],
            }

        entries = self._resolve_session_entries(session_id, source_filter) if session_id else []
        scan_status, scan_pct = self._check_session_scan_needed(entries, cam)

        min_ms = int(segs[0].start_ms)
        max_ms = max(min_ms + 2000, max(int(s.end_ms) for s in segs))
        span = max(1, max_ms - min_ms)
        n_buckets = max(10, min(400, int(buckets)))
        bucket_max = [0.0] * n_buckets
        bucket_has_video = [False] * n_buckets

        for s in segs:
            bi0 = min(n_buckets - 1, max(0, int(((int(s.start_ms) - min_ms) / span) * n_buckets)))
            bi1 = min(n_buckets - 1, max(0, int(((max(int(s.start_ms), int(s.end_ms) - 1) - min_ms) / span) * n_buckets)))
            for bi in range(bi0, bi1 + 1):
                bucket_has_video[bi] = True

        all_samples = self.session_samples(session_id, source_filter=source_filter)
        cam_samples: list[dict[str, Any]] = []
        cam_l = canon.lower()
        slug_l = cam.slug.lower() if cam else ""
        sample_by_file: dict[str, float] = {}
        with self.lock:
            enabled_cams = [c for c in self.cameras if c.enabled]
        cam_names = [c.name for c in enabled_cams]
        if canon not in cam_names:
            cam_names.insert(0, canon)
        cam_idx_by_l = {c.name.lower(): idx for idx, c in enumerate(enabled_cams)}
        slug_idx_by_l = {c.slug.lower(): idx for idx, c in enumerate(enabled_cams)}
        cam_thresh_by_idx = {idx: float(c.motion_threshold) for idx, c in enumerate(enabled_cams)}
        cam_act: dict[str, dict[str, Any]] = {cn: {"activeSegs": 0, "maxE": 0.0} for cn in cam_names}
        all_cam_bins: dict[int, tuple[int, int, float, int, str]] = {}

        for s in all_samples:
            sc = str(s.get("camera", "")).strip().lower()
            ss = str(s.get("slug", "")).strip().lower()
            e = float(s.get("energy", 0.0))
            c_idx = slug_idx_by_l.get(ss) if ss in slug_idx_by_l else cam_idx_by_l.get(sc)
            if c_idx is not None:
                cn = enabled_cams[c_idx].name
                stat = cam_act[cn]
                if e >= max(1.0, cam_thresh_by_idx[c_idx] * 0.7):
                    stat["activeSegs"] += 1
                if e > stat["maxE"]:
                    stat["maxE"] = round(e, 2)
                s0 = int(s["startMs"])
                s1 = int(s["endMs"])
                k_bin = s0 // 2000
                cur_best = all_cam_bins.get(k_bin)
                k_ch = str(s.get("kind") or "w")
                if cur_best is None or e > cur_best[2]:
                    all_cam_bins[k_bin] = (s0, max(1000, s1 - s0), round(e, 2), c_idx, k_ch)

            if sc != cam_l and (not slug_l or ss != slug_l):
                continue
            cam_samples.append(s)
            fn = str(s.get("file") or "")
            if fn and e > sample_by_file.get(fn, -1.0):
                sample_by_file[fn] = e
            mid = (int(s["startMs"]) + int(s["endMs"])) // 2
            bi = min(n_buckets - 1, max(0, int(((mid - min_ms) / span) * n_buckets)))
            if e > bucket_max[bi]:
                bucket_max[bi] = e

        # Sync SegmentInfo energy with best sample energy
        enriched_segs: list[SegmentInfo] = []
        for s in segs:
            best_e = max(float(s.energy), sample_by_file.get(s.path.name, 0.0))
            enriched_segs.append(replace(s, energy=best_e))

        if not cam_samples:
            for s in enriched_segs:
                e = float(s.energy)
                cam_samples.append(
                    {
                        "camera": canon,
                        "slug": slug_l,
                        "startMs": int(s.start_ms),
                        "endMs": int(s.end_ms),
                        "energy": e,
                    }
                )
                mid = (int(s.start_ms) + int(s.end_ms)) // 2
                bi = min(n_buckets - 1, max(0, int(((mid - min_ms) / span) * n_buckets)))
                if e > bucket_max[bi]:
                    bucket_max[bi] = e

        runs = self._build_runs(enriched_segs)
        recorded_sec = sum(int(r["durationSec"]) for r in runs)
        compact_samples = [
            [
                int(s.start_ms),
                max(1000, int(s.end_ms - s.start_ms)),
                round(float(s.energy), 2),
                s.kind or "w",
            ]
            for s in enriched_segs
        ]
        all_cam_samples = [
            [row[0], row[1], row[2], row[3], row[4]]
            for _, row in sorted(all_cam_bins.items(), key=lambda kv: kv[0])
        ]

        activity = [
            {
                "tMs": int(min_ms + ((i + 0.5) / n_buckets) * span),
                "e": round(bucket_max[i], 2),
                "v": 1 if bucket_has_video[i] else 0,
            }
            for i in range(n_buckets)
        ]
        bursts = [
            {
                "startMs": b["startMs"],
                "endMs": b["endMs"],
                "durationSec": b["durationSec"],
                "peakEnergy": b.get("peakEnergy", 0.0),
            }
            for b in cluster_motion_bursts(
                cam_samples,
                threshold=thresh,
                min_dur_ms=8_000,
                camera=canon,
                resolve_overlap=False,
            )
        ]
        bursts30 = [
            {"startMs": b["startMs"], "endMs": b["endMs"], "durationSec": b["durationSec"]}
            for b in cluster_motion_bursts(
                cam_samples,
                threshold=thresh,
                min_dur_ms=30_000,
                camera=canon,
                resolve_overlap=False,
            )
        ]
        return {
            "ok": True,
            "camera": canon,
            "minMs": min_ms,
            "maxMs": max_ms,
            "segmentCount": len(enriched_segs),
            "recordedSec": recorded_sec,
            "threshold": round(thresh, 2),
            "runs": runs,
            "samples": compact_samples,
            "allCamSamples": all_cam_samples,
            "cameraNames": cam_names,
            "cameraActivity": cam_act,
            "hasAudio": self.room_audio.has_audio(min_ms, max_ms),
            "scanStatus": scan_status,
            "scanPct": scan_pct,
            "activity": activity,
            "bursts": bursts,
            "bursts30": bursts30,
        }

    def list_tapes(self, source_filter: str = "all") -> dict[str, Any]:
        """List rolling workout & 24/7 motion tapes grouped by local calendar day + live pre-roll ring buffer."""
        sf = self._normalize_source_filter(source_filter)
        with self.lock:
            cams = list(self.cameras)
            active_slug = safe_slug(self.active_session_id, "session") if self.active_session_id else ""
            ret_days = int(self.retention_days)
            ring_snapshot = {k: list(v) for k, v in self.ring_segments.items()}

        def _views_for_sources(phys_slugs: set[str]) -> list[str]:
            views = [c.name for c in cams if c.enabled and self._source_slug_for(c) in phys_slugs]
            return views or [c.name for c in cams if c.enabled]

        tapes: list[dict[str, Any]] = []
        if self.tapes_root.exists():
            try:
                sess_entries = sorted(os.scandir(self.tapes_root), key=lambda e: e.name)
            except OSError:
                sess_entries = []
            for s_ent in sess_entries:
                if not s_ent.is_dir():
                    continue
                sid = s_ent.name
                meta = self._tape_meta(sid)
                kind = meta["kind"]
                if sf != "all" and kind != sf:
                    continue
                min_ms: int | None = None
                max_ms: int | None = None
                seg_count = 0
                total_bytes = 0
                phys_slugs: set[str] = set()
                ts_set: set[int] = set()
                try:
                    sub_entries = list(os.scandir(s_ent.path))
                except OSError:
                    sub_entries = []
                for cam_ent in sub_entries:
                    if not cam_ent.is_dir():
                        continue
                    cam_segs = 0
                    try:
                        with os.scandir(cam_ent.path) as it:
                            for f_ent in it:
                                nm = f_ent.name
                                if not (nm.startswith("seg_") and nm.endswith(".ts") and f_ent.is_file()):
                                    continue
                                s_ms = parse_segment_start_ms(nm)
                                try:
                                    st = f_ent.stat()
                                    sz = int(st.st_size)
                                    if s_ms is None:
                                        s_ms = int(st.st_mtime * 1000) - 2000
                                except OSError:
                                    sz = 0
                                if s_ms is None:
                                    continue
                                ts_set.add(s_ms)
                                e_ms = s_ms + 2500
                                min_ms = s_ms if (min_ms is None or s_ms < min_ms) else min_ms
                                max_ms = e_ms if (max_ms is None or e_ms > max_ms) else max_ms
                                total_bytes += sz
                                seg_count += 1
                                cam_segs += 1
                    except OSError:
                        continue
                    if cam_segs > 0:
                        phys_slugs.add(cam_ent.name)

                if seg_count == 0 or min_ms is None or max_ms is None:
                    continue
                runs = self._build_runs_from_timestamps(sorted(ts_set))
                recorded_sec = sum(int(r["durationSec"]) for r in runs) if runs else max(2, int(round((max_ms - min_ms) / 1000.0)))
                span_sec = max(2, int(round((max_ms - min_ms) / 1000.0)))
                dt = datetime.fromtimestamp(min_ms / 1000.0)
                tapes.append(
                    {
                        "sessionId": sid,
                        "name": meta["name"],
                        "kind": kind,
                        "isLive": False,
                        "isActive": sid == active_slug,
                        "isStub": len(ts_set) <= 1,
                        "date": dt.strftime("%Y-%m-%d"),
                        "startMs": min_ms,
                        "endMs": max_ms,
                        "durationSec": recorded_sec,
                        "recordedSec": recorded_sec,
                        "spanSec": span_sec,
                        "runs": runs,
                        "segmentCount": seg_count,
                        "sizeMb": round(total_bytes / (1024.0 * 1024.0), 1),
                        "cameras": _views_for_sources(phys_slugs),
                        "hasAudio": self.room_audio.has_audio(min_ms, max_ms),
                    }
                )

        tapes.sort(key=lambda t: t["startMs"], reverse=True)

        # Group by date; filter out 1-chunk stubs on any day that already has real recordings
        tapes_by_day: dict[str, list[dict[str, Any]]] = {}
        for t in tapes:
            tapes_by_day.setdefault(t["date"], []).append(t)

        days: list[dict[str, Any]] = []
        for dkey in sorted(tapes_by_day.keys(), reverse=True):
            day_tapes = tapes_by_day[dkey]
            non_stubs = [t for t in day_tapes if not t.get("isStub")]
            kept = non_stubs if non_stubs else day_tapes
            dt = datetime.strptime(dkey, "%Y-%m-%d")
            w_tapes = [t for t in kept if t["kind"] == "workout"]
            m_tapes = [t for t in kept if t["kind"] == "motion"]
            rec_sec = sum(int(t["recordedSec"]) for t in kept)
            w_names = []
            for wt in w_tapes:
                nm = str(wt.get("name") or "").strip()
                if nm and nm != wt["sessionId"] and nm not in w_names:
                    w_names.append(nm)
            days.append(
                {
                    "date": dkey,
                    "label": f"{dt.strftime('%a %b')} {dt.day}",
                    "tapeCount": len(kept),
                    "totalDurationSec": rec_sec,
                    "recordedSec": rec_sec,
                    "workoutSec": sum(int(t["recordedSec"]) for t in w_tapes),
                    "motionSec": sum(int(t["recordedSec"]) for t in m_tapes),
                    "workoutCount": len(w_tapes),
                    "motionCount": len(m_tapes),
                    "workoutNames": w_names,
                    "totalMb": round(sum(float(t["sizeMb"]) for t in kept), 1),
                    "tapes": kept,
                }
            )

        # Live rolling ring entry (always present when physical ring has segments)
        live_min: int | None = None
        live_max: int | None = None
        live_segs = 0
        live_bytes = 0
        live_phys: set[str] = set()
        seen_paths: set[str] = set()
        if self.ring_root.exists():
            for cam in self._physical_cameras():
                cdir = self.ring_root / cam.slug
                if not cdir.is_dir():
                    continue
                cnt = 0
                try:
                    with os.scandir(cdir) as it:
                        for f_ent in it:
                            nm = f_ent.name
                            if not (nm.startswith("seg_") and nm.endswith(".ts") and f_ent.is_file()):
                                continue
                            s_ms = parse_segment_start_ms(nm)
                            try:
                                st = f_ent.stat()
                                sz = int(st.st_size)
                                if s_ms is None:
                                    s_ms = int(st.st_mtime * 1000) - 2000
                            except OSError:
                                sz = 0
                            if s_ms is None:
                                continue
                            seen_paths.add(f_ent.path)
                            e_ms = s_ms + 2500
                            live_min = s_ms if (live_min is None or s_ms < live_min) else live_min
                            live_max = e_ms if (live_max is None or e_ms > live_max) else live_max
                            live_bytes += sz
                            live_segs += 1
                            cnt += 1
                except OSError:
                    continue
                if cnt > 0:
                    live_phys.add(cam.slug)
        for slug_k, infos in ring_snapshot.items():
            for info in infos:
                p_str = str(info.path)
                if p_str in seen_paths or not info.path.exists():
                    continue
                seen_paths.add(p_str)
                try:
                    live_bytes += int(info.path.stat().st_size)
                except OSError:
                    pass
                live_min = info.start_ms if (live_min is None or info.start_ms < live_min) else live_min
                live_max = info.end_ms if (live_max is None or info.end_ms > live_max) else live_max
                live_segs += 1
                live_phys.add(slug_k)

        live_entry: dict[str, Any] | None = None
        if live_segs > 0 and live_min is not None and live_max is not None:
            dt_l = datetime.fromtimestamp(live_min / 1000.0)
            live_entry = {
                "sessionId": "",
                "isLive": True,
                "label": "Live Buffer",
                "date": dt_l.strftime("%Y-%m-%d"),
                "startMs": live_min,
                "endMs": live_max,
                "durationSec": max(2, int(round((live_max - live_min) / 1000.0))),
                "segmentCount": live_segs,
                "sizeMb": round(live_bytes / (1024.0 * 1024.0), 1),
                "cameras": _views_for_sources(live_phys),
                "hasAudio": self.room_audio.has_audio(live_min, live_max),
            }

        return {
            "ok": True,
            "retentionDays": ret_days,
            "live": live_entry,
            "days": days,
            "tapes": tapes,
        }

    @staticmethod
    def _crop_cache_key(cam: CameraConfig) -> str:
        x0, y0, x1, y1 = cam.crop
        base = (
            f"r{cam.rotate}_{int(round(x0 * 1000))}_{int(round(y0 * 1000))}_"
            f"{int(round(x1 * 1000))}_{int(round(y1 * 1000))}"
        )
        tlt = normalize_tilt(cam.tilt)
        if tlt != 0.0:
            base = f"{base}_t{int(round(tlt * 10))}"
        return f"{base}_d1" if cam.defish else base

    def _render_segment_thumb_jpeg(
        self,
        seg_path: Path,
        cam: CameraConfig | None,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
        quality: int = DEFAULT_SCRUB_THUMB_Q,
        predecoded_frame: np.ndarray | None = None,
    ) -> bytes | None:
        frm = predecoded_frame
        if frm is None:
            cap = cv2.VideoCapture(str(seg_path))
            if not cap.isOpened():
                return None
            try:
                # Read sequentially from the opening IDR keyframe (never call POS_MSEC/POS_FRAMES on MPEG-TS).
                for _ in range(5):
                    ok0, f0 = cap.read()
                    if ok0 and f0 is not None:
                        frm = f0
                        break
            finally:
                cap.release()
        if frm is None:
            return None
        if cam:
            frm = apply_frame_transform(frm, cam.crop, cam.rotate, apply_crop=True, tilt=cam.tilt, defish=cam.defish)
        h, w = frm.shape[:2]
        tw = max(180, min(720, int(max_w)))
        if w > tw:
            th = max(1, int(round(h * (tw / max(1, w)))))
            frm = cv2.resize(frm, (tw, th), interpolation=cv2.INTER_AREA)
        ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        return buf.tobytes() if ok_jpg else None

    def _put_mem_thumb(self, mem_key: str, jpg: bytes) -> None:
        with self._thumb_lock:
            self._thumb_mem[mem_key] = jpg
            self._thumb_mem.move_to_end(mem_key)
            while len(self._thumb_mem) > 1200:
                self._thumb_mem.popitem(last=False)

    def _get_or_create_seg_thumb(
        self,
        cam: CameraConfig | None,
        seg_path: Path,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
        quality: int = DEFAULT_SCRUB_THUMB_Q,
        predecoded_frame: np.ndarray | None = None,
    ) -> bytes | None:
        tw = max(180, min(720, int(max_w)))
        if cam is None or tw > DEFAULT_SCRUB_THUMB_W:
            return self._render_segment_thumb_jpeg(
                seg_path, cam, max_w=tw, quality=quality, predecoded_frame=predecoded_frame
            )
        ck = self._crop_cache_key(cam)
        mem_key = f"{cam.slug}/{ck}/{seg_path.stem}"
        with self._thumb_lock:
            hit = self._thumb_mem.get(mem_key)
            if hit is not None:
                self._thumb_mem.move_to_end(mem_key)
                return hit
        cache_dir = self.thumbs_root / cam.slug / ck
        cache_path = cache_dir / f"{seg_path.stem}.jpg"
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 100:
                jpg = cache_path.read_bytes()
                self._put_mem_thumb(mem_key, jpg)
                return jpg
        except OSError:
            pass
        jpg = self._render_segment_thumb_jpeg(
            seg_path,
            cam,
            max_w=DEFAULT_SCRUB_THUMB_W,
            quality=quality,
            predecoded_frame=predecoded_frame,
        )
        if not jpg:
            return None
        self._put_mem_thumb(mem_key, jpg)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_dir / f"{seg_path.stem}.{threading.get_ident()}.tmp"
            tmp_path.write_bytes(jpg)
            tmp_path.replace(cache_path)
        except OSError:
            pass
        return jpg

    def get_tape_frame_jpeg(
        self,
        camera_name: str,
        session_id: str,
        ts_ms: int,
        max_w: int = 480,
        max_gap_ms: int | None = None,
        source_filter: str = "all",
    ) -> bytes | None:
        self._interactive_busy_until = max(self._interactive_busy_until, time.monotonic() + 3.0)
        cam = self._find_camera(camera_name)
        segs = self._list_camera_segments(camera_name, session_id, source_filter=source_filter)
        if not segs:
            return None

        starts = [s.start_ms for s in segs]
        idx = bisect.bisect_right(starts, ts_ms) - 1
        cands: list[SegmentInfo] = []
        if idx >= 0:
            cands.append(segs[idx])
        if idx + 1 < len(segs):
            cands.append(segs[idx + 1])
        if not cands:
            cands.append(segs[0])
        best = min(cands, key=lambda s: abs(((s.start_ms + s.end_ms) // 2) - ts_ms))

        if max_gap_ms is not None:
            if best.start_ms <= ts_ms <= best.end_ms:
                dist = 0
            else:
                dist = min(abs(ts_ms - best.start_ms), abs(ts_ms - best.end_ms))
            if dist > int(max_gap_ms):
                return None

        if cam and int(max_w) <= DEFAULT_SCRUB_THUMB_W:
            cached = self._get_or_create_seg_thumb(cam, best.path, max_w=DEFAULT_SCRUB_THUMB_W)
            if cached:
                return cached

        cap = cv2.VideoCapture(str(best.path))
        if not cap.isOpened():
            return None
        try:
            # Read sequentially from the opening IDR keyframe (never call POS_MSEC/POS_FRAMES on MPEG-TS).
            first_frm = None
            for _ in range(5):
                ok0, f0 = cap.read()
                if ok0 and f0 is not None:
                    first_frm = f0
                    break
            if first_frm is None:
                return None

            frm = first_frm
            offset_ms = ts_ms - best.start_ms
            if 600 <= offset_ms <= 1900:
                grabs = min(20, max(1, int(offset_ms // 80)))
                ok_g = True
                for _ in range(grabs):
                    if not cap.grab():
                        ok_g = False
                        break
                if ok_g:
                    ok_r, f_later = cap.retrieve()
                    if ok_r and f_later is not None:
                        frm = f_later

            if cam:
                frm = apply_frame_transform(frm, cam.crop, cam.rotate, apply_crop=True, tilt=cam.tilt, defish=cam.defish)
            h, w = frm.shape[:2]
            tw = max(180, min(720, int(max_w)))
            if w > tw:
                th = max(1, int(round(h * (tw / max(1, w)))))
                frm = cv2.resize(frm, (tw, th), interpolation=cv2.INTER_AREA)
            ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
            return buf.tobytes() if ok_jpg else None
        finally:
            cap.release()

    def tape_frame_pack(
        self,
        camera_name: str,
        session_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        count: int = 80,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
        source_filter: str = "all",
    ) -> dict[str, Any]:
        self._interactive_busy_until = max(self._interactive_busy_until, time.monotonic() + 3.0)
        cam = self._find_camera(camera_name)
        canon = cam.name if cam else (camera_name or "Rack Cam")
        segs = self._list_camera_segments(canon, session_id, source_filter=source_filter)
        if not segs:
            return {
                "ok": True,
                "camera": canon,
                "minMs": 0,
                "maxMs": 0,
                "winStartMs": 0,
                "winEndMs": 0,
                "frames": [],
            }

        min_ms = int(segs[0].start_ms)
        max_ms = max(min_ms + 2000, max(int(s.end_ms) for s in segs))
        filtered = segs
        if start_ms is not None or end_ms is not None:
            w0 = int(start_ms) if start_ms is not None else min_ms
            w1 = int(end_ms) if end_ms is not None else max_ms
            sub = [s for s in segs if s.end_ms >= w0 - 2500 and s.start_ms <= w1 + 2500]
            if sub:
                filtered = sub

        n = max(1, min(180, int(count)))
        if len(filtered) <= n:
            picked = filtered
        elif n == 1:
            picked = [filtered[len(filtered) // 2]]
        else:
            seen_idx: set[int] = set()
            picked = []
            last = len(filtered) - 1
            for i in range(n):
                idx = int(round((i * last) / (n - 1)))
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    picked.append(filtered[idx])

        tw = max(180, min(720, int(max_w)))
        if len(picked) > 4:
            def _load_one(s: SegmentInfo) -> bytes | None:
                return self._get_or_create_seg_thumb(cam, s.path, max_w=tw)
            with ThreadPoolExecutor(max_workers=4) as pool:
                jpgs = list(pool.map(_load_one, picked))
        else:
            jpgs = [self._get_or_create_seg_thumb(cam, seg.path, max_w=tw) for seg in picked]

        frames: list[dict[str, Any]] = []
        for seg, jpg in zip(picked, jpgs):
            if jpg:
                frames.append(
                    {
                        "tsMs": (int(seg.start_ms) + int(seg.end_ms)) // 2,
                        "jpg": base64.b64encode(jpg).decode("ascii"),
                    }
                )
        return {
            "ok": True,
            "camera": canon,
            "minMs": min_ms,
            "maxMs": max_ms,
            "winStartMs": int(picked[0].start_ms),
            "winEndMs": int(picked[-1].end_ms),
            "frames": frames,
        }

    def bursts_for_session(
        self,
        session_id: str,
        around_ms: int | None = None,
        camera: str | None = None,
        min_dur_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        samples = self.session_samples(session_id)
        cam_cfg = self._find_camera(camera) if camera and camera.strip() else None
        thresh = (
            cam_cfg.motion_threshold
            if cam_cfg
            else min((c.motion_threshold for c in self.cameras), default=DEFAULT_MOTION_THRESHOLD)
        )
        min_dur_ms = int(round(max(1.0, min_dur_sec) * 1000)) if min_dur_sec is not None else 4000
        bursts = cluster_motion_bursts(
            samples,
            threshold=thresh,
            min_dur_ms=min_dur_ms,
            camera=(cam_cfg.name if cam_cfg else None),
            resolve_overlap=(cam_cfg is None),
        )
        limit = 20
        if around_ms is not None:
            bursts.sort(key=lambda b: abs(int(b["endMs"]) - around_ms))
            bursts = bursts[:limit]
            bursts.sort(key=lambda b: int(b["startMs"]))
        else:
            bursts = bursts[-limit:]

        self._interactive_busy_until = max(self._interactive_busy_until, time.monotonic() + 3.0)
        if bursts:
            segs_by_cam: dict[str, tuple[CameraConfig | None, list[SegmentInfo], list[int]]] = {}
            for b_item in bursts:
                cname = str(b_item["camera"])
                if cname not in segs_by_cam:
                    c_obj = self._find_camera(cname)
                    c_segs = self._list_camera_segments(cname, session_id)
                    segs_by_cam[cname] = (c_obj, c_segs, [s.start_ms for s in c_segs])

            def _load_burst_thumb(b_item: dict[str, Any]) -> str:
                cname = str(b_item["camera"])
                c_obj, c_segs, starts = segs_by_cam.get(cname, (None, [], []))
                if not c_segs:
                    return ""
                mid_ms = (int(b_item["startMs"]) + int(b_item["endMs"])) // 2
                idx = bisect.bisect_right(starts, mid_ms) - 1
                cands: list[SegmentInfo] = []
                if idx >= 0:
                    cands.append(c_segs[idx])
                if idx + 1 < len(c_segs):
                    cands.append(c_segs[idx + 1])
                if not cands:
                    cands.append(c_segs[0])
                best = min(cands, key=lambda s: abs(((s.start_ms + s.end_ms) // 2) - mid_ms))
                jpg = self._get_or_create_seg_thumb(c_obj, best.path, max_w=DEFAULT_SCRUB_THUMB_W)
                if not jpg:
                    return ""
                return "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")

            if len(bursts) > 1:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    thumbs = list(pool.map(_load_burst_thumb, bursts))
                for b_item, th in zip(bursts, thumbs):
                    b_item["thumb"] = th
            else:
                bursts[0]["thumb"] = _load_burst_thumb(bursts[0])
        return bursts

    def _thumb_near_ms(
        self,
        camera_name: str,
        session_id: str,
        target_ms: int,
        max_w: int = DEFAULT_SCRUB_THUMB_W,
    ) -> str:
        jpg = self.get_tape_frame_jpeg(camera_name, session_id, target_ms, max_w=max_w)
        if not jpg:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")

    def get_snapshot_jpeg(
        self,
        camera_name: str | None = None,
        raw: bool = False,
        hires: bool = False,
        rotate_override: int | None = None,
        tilt_override: float | None = None,
        crop_override: tuple[float, float, float, float] | None = None,
        defish_override: bool | None = None,
    ) -> bytes | None:
        cam = self._find_camera(camera_name)
        if not cam:
            return None
        eff_rot = normalize_rotate(rotate_override) if rotate_override is not None else cam.rotate
        eff_tilt = normalize_tilt(tilt_override) if tilt_override is not None else cam.tilt
        eff_crop = normalize_crop(crop_override) if crop_override is not None else cam.crop
        eff_defish = bool(defish_override) if defish_override is not None else cam.defish
        apply_defish = bool(eff_defish and not raw)
        media_slug = self._source_slug_for(cam)
        src_cam = self._source_camera(cam)

        if hires:
            with self.lock:
                segs = list(self.ring_segments.get(cam.slug) or self.ring_segments.get(media_slug, []))
            for seg in reversed(segs[-4:]):
                if seg.path.exists():
                    cap = cv2.VideoCapture(str(seg.path))
                    if cap.isOpened():
                        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                        if total > 1:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
                        ok, frm = cap.read()
                        if (not ok or frm is None) and total > 1:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            ok, frm = cap.read()
                        cap.release()
                        if ok and frm is not None:
                            frm = apply_frame_transform(
                                frm, eff_crop, eff_rot, apply_crop=not raw, tilt=eff_tilt, defish=apply_defish
                            )
                            ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                            if ok_jpg:
                                return buf.tobytes()

        live_jpg = self.live_hub.recent_frame(media_slug, max_age_sec=2.0)
        if live_jpg:
            if not raw and (eff_rot != 0 or eff_tilt != 0.0 or apply_defish or eff_crop != (0.0, 0.0, 1.0, 1.0)):
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    img = apply_frame_transform(img, eff_crop, eff_rot, apply_crop=True, tilt=eff_tilt, defish=apply_defish)
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        return buf.tobytes()
            elif eff_rot != 0 or eff_tilt != 0.0:
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    img = apply_frame_transform(img, eff_crop, eff_rot, apply_crop=False, tilt=eff_tilt)
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        return buf.tobytes()
            return live_jpg

        with self.lock:
            segs = list(self.ring_segments.get(cam.slug) or self.ring_segments.get(media_slug, []))
        for seg in reversed(segs[-4:]):
            if seg.path.exists():
                cap = cv2.VideoCapture(str(seg.path))
                if cap.isOpened():
                    ok, frm = cap.read()
                    cap.release()
                    if ok and frm is not None:
                        frm = apply_frame_transform(
                            frm, eff_crop, eff_rot, apply_crop=not raw, tilt=eff_tilt, defish=apply_defish
                        )
                        ok_jpg, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                        if ok_jpg:
                            return buf.tobytes()

        if cam.enabled and src_cam.rtsp_url:
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                src_cam.rtsp_url,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=5)
                if res.returncode == 0 and res.stdout.startswith(b"\xff\xd8"):
                    img = cv2.imdecode(np.frombuffer(res.stdout, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        img = apply_frame_transform(
                            img, eff_crop, eff_rot, apply_crop=not raw, tilt=eff_tilt, defish=apply_defish
                        )
                        ok_jpg, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                        if ok_jpg:
                            return buf.tobytes()
                    return res.stdout
            except Exception:
                pass
        return None

    def _probe_segment_dims(self, seg_path: Path) -> tuple[int, int]:
        key = str(seg_path.parent)
        cached = self._seg_dims_cache.get(key)
        if cached:
            return cached
        cap = cv2.VideoCapture(str(seg_path))
        if cap.isOpened():
            try:
                ok, frm = cap.read()
                if ok and frm is not None and frm.shape[1] > 0 and frm.shape[0] > 0:
                    dims = (int(frm.shape[1]), int(frm.shape[0]))
                    self._seg_dims_cache[key] = dims
                    return dims
            finally:
                cap.release()
        return (WYZE_V3_REF_W, WYZE_V3_REF_H)

    def _encode_segments_mp4(
        self,
        segs: list[SegmentInfo],
        cam_cfg: CameraConfig | None,
        start_ms: int,
        end_ms: int,
        out_path: Path,
        *,
        include_audio: bool = False,
        audio_path: Path | None = None,
        fast_defish: bool = False,
        deflicker: bool | None = None,
    ) -> bool:
        if not segs:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        src_size = self._probe_segment_dims(segs[0].path) if (cam_cfg and cam_cfg.defish) else (WYZE_V3_REF_W, WYZE_V3_REF_H)
        vf = (
            build_ffmpeg_vf(
                cam_cfg.crop,
                cam_cfg.rotate,
                tilt=cam_cfg.tilt,
                defish=cam_cfg.defish,
                src_size=src_size,
                fast_defish=fast_defish,
            )
            if cam_cfg
            else None
        )
        first_start_ms = segs[0].start_ms
        ss_sec = max(0.0, (start_ms - first_start_ms) / 1000.0)
        dur_sec = max(0.5, (end_ms - start_ms) / 1000.0)
        enc_timeout = max(60, int(dur_sec * 1.5) + 30)
        mux_timeout = max(45, int(dur_sec) + 30)
        tag = uuid.uuid4().hex[:8]
        concat_path = out_path.parent / f".concat_{tag}.txt"
        tmp_vid = out_path.with_name(f".{out_path.stem}.{tag}.vid.mp4")
        tmp_mux = out_path.with_name(f".{out_path.stem}.{tag}.mux.mp4")
        try:
            lines = []
            for s in segs:
                posix_p = s.path.resolve().as_posix().replace("'", r"'\''")
                lines.append(f"file '{posix_p}'")
            concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Always strip Wyze pinhole camera mic audio (-an); only mux PowerConf audio when explicitly requested
            cmd = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-ss",
                f"{ss_sec:.3f}",
                "-t",
                f"{dur_sec:.3f}",
                *(["-vf", vf] if vf else []),
                "-c:v",
                "libx264",
                "-preset",
                "superfast",
                "-g",
                "15",
                "-keyint_min",
                "15",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                str(tmp_vid),
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=enc_timeout)
            except subprocess.TimeoutExpired:
                self.log.warning("FFmpeg clip encode timed out after %ss", enc_timeout)
                tmp_vid.unlink(missing_ok=True)
                return False
            if res.returncode != 0 or not tmp_vid.exists() or tmp_vid.stat().st_size == 0:
                self.log.warning("FFmpeg clip failed: %s", res.stderr.strip())
                tmp_vid.unlink(missing_ok=True)
                return False

            do_deflicker = (not fast_defish) if deflicker is None else bool(deflicker)
            if do_deflicker:
                df_res = deflicker_rolling_led_mp4(tmp_vid, self.ffmpeg, timeout_sec=enc_timeout)
                if df_res.get("filtered"):
                    b = df_res.get("before") or {}
                    self.log.info(
                        "LED deflicker applied to %s (k=%s, amp5=%.2f, p2p=%.2f)",
                        out_path.name,
                        df_res.get("kFrames"),
                        float(b.get("amp5Med", 0.0)),
                        float(b.get("ledP2p", 0.0)),
                    )
                elif df_res.get("error"):
                    self.log.warning("LED deflicker skipped (%s): %s", out_path.name, df_res["error"])

            if include_audio and audio_path is not None and audio_path.is_file():
                mux_cmd = [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(tmp_vid),
                    "-i",
                    str(audio_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(tmp_mux),
                ]
                try:
                    mres = subprocess.run(mux_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=mux_timeout)
                except subprocess.TimeoutExpired:
                    self.log.warning("FFmpeg audio mux timed out after %ss", mux_timeout)
                    tmp_mux.unlink(missing_ok=True)
                    return False
                if mres.returncode != 0 or not tmp_mux.exists() or tmp_mux.stat().st_size == 0:
                    self.log.warning("FFmpeg audio mux failed: %s", mres.stderr.strip())
                    tmp_mux.unlink(missing_ok=True)
                    return False
                tmp_mux.replace(out_path)
                return True

            tmp_vid.replace(out_path)
            return True
        finally:
            concat_path.unlink(missing_ok=True)
            tmp_vid.unlink(missing_ok=True)
            tmp_mux.unlink(missing_ok=True)

    def tape_angles_for_window(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        norm_end = max(int(start_ms) + 1000, int(end_ms))
        with self.lock:
            cams = [c for c in self.cameras if c.enabled]
        avail: list[str] = []
        for cam in cams:
            segs = self.find_segments(cam.name, session_id, int(start_ms), norm_end)
            if any(s.end_ms > int(start_ms) and s.start_ms < norm_end for s in segs):
                avail.append(cam.name)
        return avail

    def stop_stream(self, stream_id: str = "") -> int:
        killed = 0
        with self._stream_lock:
            keys = [stream_id] if (stream_id and stream_id in self._stream_procs) else ([] if stream_id else list(self._stream_procs.keys()))
            targets = [self._stream_procs.pop(k)[0] for k in keys]
        for proc in targets:
            killed += 1
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return killed

    def stream_tape_fmp4(
        self,
        camera_name: str,
        session_id: str,
        start_ms: int,
        *,
        source_filter: str = "all",
        max_sec: int = 600,
        stream_id: str = "",
    ) -> tuple[int, int, Any] | None:
        """Open a low-latency (~330ms TTFF) fragmented MP4 FFmpeg stream across a contiguous tape run."""
        self._interactive_busy_until = max(self._interactive_busy_until, time.monotonic() + 5.0)
        cam_cfg = self._find_camera(camera_name)
        canon = cam_cfg.name if cam_cfg else (camera_name or "Rack Cam")
        segs = self._list_camera_segments(canon, session_id, source_filter=source_filter)
        if not segs:
            return None

        req_ms = int(start_ms)
        start_idx = -1
        for i, s in enumerate(segs):
            if s.end_ms > req_ms - 500:
                if s.start_ms <= req_ms + 10_000:
                    start_idx = i
                break
        if start_idx < 0:
            return None

        run_segs: list[SegmentInfo] = [segs[start_idx]]
        max_end_ms = max(req_ms, int(run_segs[0].start_ms)) + max(15, min(1800, int(max_sec))) * 1000
        for s in segs[start_idx + 1 :]:
            if s.start_ms - run_segs[-1].end_ms > 12_000:
                break
            if s.start_ms >= max_end_ms or len(run_segs) >= 300:
                break
            run_segs.append(s)

        actual_start_ms = max(req_ms, int(run_segs[0].start_ms))
        run_end_ms = int(run_segs[-1].end_ms)
        ss_sec = max(0.0, (actual_start_ms - int(run_segs[0].start_ms)) / 1000.0)

        src_size = self._probe_segment_dims(run_segs[0].path) if (cam_cfg and cam_cfg.defish) else (WYZE_V3_REF_W, WYZE_V3_REF_H)
        vf = (
            build_ffmpeg_vf(
                cam_cfg.crop,
                cam_cfg.rotate,
                tilt=cam_cfg.tilt,
                defish=cam_cfg.defish,
                src_size=src_size,
                fast_defish=True,
            )
            if cam_cfg
            else None
        )

        tag = uuid.uuid4().hex[:10]
        s_key = stream_id.strip() or f"anon_{tag}"
        concat_path = self.previews_root / f".stream_{tag}.txt"
        concat_lines = [
            "file '{}'".format(s.path.resolve().as_posix().replace("'", r"'\''"))
            for s in run_segs
        ]
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            *(["-ss", f"{ss_sec:.3f}"] if ss_sec >= 0.08 else []),
            *(["-vf", vf] if vf else []),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-sc_threshold",
            "0",
            "-bf",
            "0",
            "-crf",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "mp4",
            "-frag_duration",
            "500000",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]

        to_kill: list[subprocess.Popen[bytes]] = []
        with self._stream_lock:
            old = self._stream_procs.pop(s_key, None)
            if old is not None:
                to_kill.append(old[0])
            while len(self._stream_procs) >= MAX_STREAM_CONCURRENT:
                oldest_k = min(self._stream_procs.items(), key=lambda kv: kv[1][1])[0]
                to_kill.append(self._stream_procs.pop(oldest_k)[0])
        for old_p in to_kill:
            try:
                old_p.terminate()
            except Exception:
                pass

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **_subprocess_no_window_kwargs(),
            )
            _assign_child_to_job(proc)
        except Exception:
            concat_path.unlink(missing_ok=True)
            return None

        with self._stream_lock:
            self._stream_procs[s_key] = (proc, time.monotonic())

        def _gen():
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(32_768)
                    if not chunk:
                        break
                    yield chunk
            finally:
                with self._stream_lock:
                    cur = self._stream_procs.get(s_key)
                    if cur is not None and cur[0] is proc:
                        self._stream_procs.pop(s_key, None)
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                concat_path.unlink(missing_ok=True)

        return actual_start_ms, run_end_ms, _gen

    def cut_preview(
        self,
        session_id: str,
        start_ms: int,
        end_ms: int,
        camera_name: str,
        source_filter: str = "all",
    ) -> dict[str, Any] | None:
        cam_cfg = self._find_camera_exact(camera_name)
        if not cam_cfg:
            return None
        s_ms = int(start_ms)
        e_ms = min(s_ms + 180_000, max(s_ms + 1000, int(end_ms)))
        segs = self.find_segments(cam_cfg.name, session_id, s_ms, e_ms, source_filter=source_filter)
        if not any(s.end_ms > s_ms and s.start_ms < e_ms for s in segs):
            return None

        ck = self._crop_cache_key(cam_cfg)
        sid_slug = safe_slug(session_id or "ring", "ring")[:32]
        fname = f"{sid_slug}_{cam_cfg.slug}_{s_ms}_{e_ms}_{ck}.mp4"
        out_path = self.previews_root / fname

        with self._preview_locks_guard:
            p_lock = self._preview_locks.get(fname)
            if p_lock is None:
                p_lock = threading.Lock()
                self._preview_locks[fname] = p_lock

        with p_lock:
            if out_path.is_file() and out_path.stat().st_size > 1000:
                try:
                    os.utime(out_path, None)
                except OSError:
                    pass
                cached = True
            else:
                ok = self._encode_segments_mp4(segs, cam_cfg, s_ms, e_ms, out_path, fast_defish=True)
                if not ok:
                    return None
                cached = False

        actual_dur = self._probe_duration_sec(out_path) or max(1, int(round((e_ms - s_ms) / 1000.0)))
        return {
            "ok": True,
            "ephemeral": True,
            "cached": cached,
            "lanUrl": f"/previews/{fname}",
            "fileName": fname,
            "camera": cam_cfg.name,
            "durationSec": max(1, actual_dur),
            "startMs": s_ms,
            "endMs": e_ms,
            "bytes": out_path.stat().st_size,
        }

    def cut_clip(
        self,
        session_id: str,
        ex_name: str,
        start_ms: int,
        end_ms: int,
        camera_override: str | None = None,
        *,
        include_audio: bool = False,
        source_filter: str = "all",
    ) -> dict[str, Any] | None:
        if end_ms <= start_ms:
            end_ms = start_ms + 5000

        samples = self.session_samples(session_id, source_filter=source_filter)
        fallback_cam = next(
            (c.name for c in self.cameras if c.enabled and c.name == "Rack Cam"),
            next((c.name for c in self.cameras if c.enabled), "Rack Cam"),
        )
        chosen_cam = select_camera_for_window(
            samples,
            start_ms,
            end_ms,
            preferred_camera=camera_override,
            fallback_camera=fallback_cam,
        )
        call_now_ms = int(time.time() * 1000)
        cam_cfg_probe = self._find_camera(chosen_cam)
        media_slug_probe = (
            self._source_slug_for(cam_cfg_probe)
            if cam_cfg_probe
            else safe_slug(chosen_cam.lower(), "rack_cam")
        )
        with self.lock:
            proc = self._procs.get(media_slug_probe)
            is_capturing = bool(proc is not None and proc.poll() is None)

        segs = self.find_segments(chosen_cam, session_id, start_ms, end_ms, source_filter=source_filter)
        if is_capturing and int(end_ms) >= call_now_ms - 5000:
            wait_target_ms = min(int(end_ms), call_now_ms)
            deadline = time.monotonic() + 4.2
            while time.monotonic() < deadline:
                segs = self.find_segments(chosen_cam, session_id, start_ms, end_ms, source_filter=source_filter)
                ready = False
                if segs:
                    last_seg = segs[-1]
                    if last_seg.start_ms >= wait_target_ms:
                        ready = True
                    elif last_seg.end_ms >= wait_target_ms:
                        try:
                            ready = (time.time() - last_seg.path.stat().st_mtime) >= 0.6
                        except OSError:
                            ready = False
                if ready:
                    break
                if self._stop_event.wait(0.35):
                    break

        if not segs and not camera_override:
            for alt in self.cameras:
                if alt.name != chosen_cam:
                    alt_segs = self.find_segments(alt.name, session_id, start_ms, end_ms, source_filter=source_filter)
                    if alt_segs:
                        chosen_cam = alt.name
                        segs = alt_segs
                        break
        if not segs:
            return None

        cam_cfg = self._find_camera(chosen_cam)
        dur_sec = max(0.5, (end_ms - start_ms) / 1000.0)

        day_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(start_ms / 1000.0).strftime("%H%M%S")
        day_dir = self.clips_root / day_str
        day_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{safe_slug(ex_name or 'lift')}_{time_str}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = day_dir / out_name

        tmp_aud: Path | None = None
        has_aud = False
        try:
            if include_audio:
                tmp_aud = day_dir / f".aud_{out_path.stem}_{uuid.uuid4().hex[:6]}.m4a"
                has_aud = self.room_audio.extract_window_m4a(int(start_ms), int(end_ms), tmp_aud)
                if not has_aud:
                    tmp_aud.unlink(missing_ok=True)
                    tmp_aud = None
            if not self._encode_segments_mp4(
                segs,
                cam_cfg,
                int(start_ms),
                int(end_ms),
                out_path,
                include_audio=has_aud,
                audio_path=tmp_aud,
            ):
                return None
        finally:
            if tmp_aud is not None:
                tmp_aud.unlink(missing_ok=True)

        bytes_len = out_path.stat().st_size
        thumb = extract_video_thumb(out_path)
        actual_dur = self._probe_duration_sec(out_path) or int(round(dur_sec))
        drive_info = self.mirror_clip_to_drive(out_path, day_str)
        rel_url = f"/clips/{day_str}/{out_name}"
        meta = {
            "ok": True,
            "lanUrl": rel_url,
            "fileName": out_name,
            "camera": chosen_cam,
            "thumb": thumb,
            "durationSec": max(1, actual_dur),
            "startMs": int(start_ms),
            "endMs": int(end_ms),
            "bytes": bytes_len,
            "hasAudio": bool(has_aud),
            "audioSource": "powerconf" if has_aud else "none",
            "exName": ex_name,
            "createdAtMs": int(time.time() * 1000),
            **({"driveFileId": drive_info["driveFileId"]} if drive_info.get("driveFileId") else {}),
            **({"driveUrl": drive_info["driveUrl"]} if drive_info.get("driveUrl") else {}),
        }
        with self._clips_index_lock:
            with self.clips_index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in meta.items() if k != "thumb"}) + "\n")
            self._clips_cache_sig = None
            self._cal_cache_sig = None
        self.check_calibration_milestones()
        return meta

    def cut_clips_multi(
        self,
        session_id: str,
        ex_name: str,
        start_ms: int,
        end_ms: int,
        cameras: Sequence[str],
        *,
        include_audio: bool = False,
        source_filter: str = "all",
    ) -> list[dict[str, Any]]:
        clean: list[str] = []
        seen: set[str] = set()
        for raw_cam in cameras:
            name = str(raw_cam or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                clean.append(name)
        if len(clean) <= 1:
            single = self.cut_clip(
                session_id=session_id,
                ex_name=ex_name,
                start_ms=start_ms,
                end_ms=end_ms,
                camera_override=(clean[0] if clean else None),
                include_audio=include_audio,
                source_filter=source_filter,
            )
            return [single] if single else []

        with ThreadPoolExecutor(max_workers=min(4, len(clean))) as pool:
            futs = [
                pool.submit(
                    lambda c_nm: self.cut_clip(
                        session_id,
                        ex_name,
                        start_ms,
                        end_ms,
                        c_nm,
                        include_audio=include_audio,
                        source_filter=source_filter,
                    ),
                    cam_name,
                )
                for cam_name in clean
            ]
            out: list[dict[str, Any]] = []
            for fut in futs:
                try:
                    clip = fut.result()
                    if clip:
                        out.append(clip)
                except Exception as exc:
                    self.log.warning("Multi-cam clip failed: %s", exc)
            return out

    def mirror_clip_to_drive(self, src_path: Path, day_str: str) -> dict[str, str]:
        """Copy an encoded MP4 into Google Drive backup folder and read Drive file ID if synced."""
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        if not backup_dir or not src_path.is_file():
            return {}
        try:
            dest_root = Path(backup_dir)
            anchor = Path(dest_root.anchor) if dest_root.anchor else dest_root
            if dest_root.anchor and not anchor.exists():
                return {}
            dest_dir = dest_root / (day_str or datetime.now().strftime("%Y-%m-%d"))
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src_path.name
            src_size = src_path.stat().st_size
            if not dest.exists() or dest.stat().st_size != src_size:
                shutil.copy2(src_path, dest)
            fid = read_google_drive_file_id(dest)
            url = google_drive_view_url(fid)
            out: dict[str, str] = {"drivePath": str(dest)}
            if fid and url:
                out["driveFileId"] = fid
                out["driveUrl"] = url
            return out
        except Exception as exc:
            self.log.warning("Google Drive clip mirror failed for %s: %s", src_path.name, exc)
            return {}

    def backfill_drive_clips(self) -> int:
        """Mirror any existing OptiPlex clips missing from Google Drive backup folder."""
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        if not backup_dir or not self.clips_root.exists():
            return 0
        copied = 0
        for p in sorted(self.clips_root.glob("*/*.mp4")):
            if self._stop_event.is_set():
                break
            dest = Path(backup_dir) / p.parent.name / p.name
            try:
                need = not dest.exists() or dest.stat().st_size != p.stat().st_size
            except OSError:
                need = True
            if need and self.mirror_clip_to_drive(p, p.parent.name):
                copied += 1
        return copied

    def _drive_preview_path(self) -> Path | None:
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        if not backup_dir:
            return None
        dest_root = Path(backup_dir)
        anchor = Path(dest_root.anchor) if dest_root.anchor else dest_root
        if dest_root.anchor and not anchor.exists():
            return None
        base_dir = dest_root.parent if dest_root.name.lower() == "clips" else dest_root
        return base_dir / PREVIEW_DRIVE_FILENAME

    def preview_drive_info(self) -> dict[str, Any]:
        dest = self._drive_preview_path()
        dest_str = str(dest) if dest is not None else ""
        if dest is not None and dest.is_file():
            if dest_str != self._preview_drive_dest_str or not self._preview_drive_file_id:
                fid = read_google_drive_file_id(dest)
                self._preview_drive_dest_str = dest_str
                if fid:
                    self._preview_drive_file_id = fid
                    self._preview_drive_url = google_drive_view_url(fid)
        elif dest_str != self._preview_drive_dest_str:
            self._preview_drive_dest_str = dest_str
            self._preview_drive_file_id = None
            self._preview_drive_url = None

        updated_ms = self._preview_updated_ms
        if updated_ms is None and dest is not None and dest.is_file():
            try:
                updated_ms = int(dest.stat().st_mtime * 1000)
            except OSError:
                pass
        if updated_ms is None and self.preview_local_path.is_file():
            try:
                updated_ms = int(self.preview_local_path.stat().st_mtime * 1000)
            except OSError:
                pass

        return {
            "fileName": PREVIEW_DRIVE_FILENAME,
            "driveFileId": self._preview_drive_file_id,
            "driveUrl": self._preview_drive_url,
            "updatedMs": updated_ms,
            "localUrl": "/api/preview.jpg",
        }

    def render_preview_overview_jpeg(self) -> tuple[bytes | None, int, int]:
        with self.lock:
            enabled_cams = [replace(c, exclusions=list(c.exclusions)) for c in self.cameras if c.enabled]
            phys_names = {c.slug: c.name for c in self.cameras if not self._is_virtual(c)}
        if not enabled_cams:
            return None, 0, 0

        frames: list[tuple[CameraConfig, np.ndarray | None]] = []
        live_count = 0
        for cam in enabled_cams:
            frm: np.ndarray | None = None
            try:
                jpg = self.get_snapshot_jpeg(cam.name, raw=False, hires=True)
                if jpg:
                    decoded = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if decoded is not None and decoded.size > 0:
                        frm = decoded
                        live_count += 1
            except Exception:
                frm = None
            frames.append((cam, frm))

        if live_count == 0:
            return None, len(enabled_cams), 0

        n = len(enabled_cams)
        cols = 1 if n == 1 else (2 if n <= 4 else 3)
        rows = math.ceil(n / cols)
        pad = 24
        header_h = 64
        tile_hdr = 42
        cell_w = 760
        cell_h = 460
        total_w = pad + cols * (cell_w + pad)
        total_h = header_h + pad + rows * (tile_hdr + cell_h + pad)
        canvas = np.full((total_h, total_w, 3), (28, 17, 11), dtype=np.uint8)

        def _ascii(txt: str) -> str:
            return txt.encode("ascii", "replace").decode("ascii")

        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(canvas, "ARMORY CAM PREVIEW", (pad, 42), font, 0.85, (245, 248, 252), 2, cv2.LINE_AA)
        right_banner = _ascii(f"{live_count}/{n} views  |  {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
        (rw, _), _ = cv2.getTextSize(right_banner, font, 0.56, 1)
        cv2.putText(canvas, right_banner, (max(pad, total_w - pad - rw), 40), font, 0.56, (191, 162, 143), 1, cv2.LINE_AA)

        for idx, (cam, frm) in enumerate(frames):
            r, c = divmod(idx, cols)
            tx = pad + c * (cell_w + pad)
            ty = header_h + pad + r * (tile_hdr + cell_h + pad)
            cv2.rectangle(canvas, (tx, ty), (tx + cell_w - 1, ty + tile_hdr - 1), (43, 27, 18), -1)
            cv2.rectangle(canvas, (tx, ty + tile_hdr), (tx + cell_w - 1, ty + tile_hdr + cell_h - 1), (15, 9, 6), -1)
            cv2.rectangle(canvas, (tx, ty), (tx + cell_w - 1, ty + tile_hdr + cell_h - 1), (61, 43, 31), 1)

            title = _ascii(cam.name)
            cv2.putText(canvas, title, (tx + 12, ty + 27), font, 0.58, (245, 248, 252), 2, cv2.LINE_AA)

            meta_parts: list[str] = []
            if cam.source_slug:
                src_name = phys_names.get(cam.source_slug, cam.source_slug)
                meta_parts.append(f"via {src_name}")
            if cam.rotate:
                meta_parts.append(f"{cam.rotate} deg")
            if abs(cam.tilt) >= 0.05:
                meta_parts.append(f"{cam.tilt:+.1f} deg")
            if meta_parts:
                meta_txt = _ascii(" | ".join(meta_parts))
                (mw, _), _ = cv2.getTextSize(meta_txt, font, 0.45, 1)
                cv2.putText(
                    canvas,
                    meta_txt,
                    (max(tx + 12, tx + cell_w - 12 - mw), ty + 26),
                    font,
                    0.45,
                    (191, 162, 143),
                    1,
                    cv2.LINE_AA,
                )

            if frm is not None and frm.shape[0] > 0 and frm.shape[1] > 0:
                fh, fw = frm.shape[:2]
                scale = min(cell_w / fw, cell_h / fh)
                nw = max(1, min(cell_w, int(round(fw * scale))))
                nh = max(1, min(cell_h, int(round(fh * scale))))
                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                resized = cv2.resize(frm, (nw, nh), interpolation=interp)
                ox = tx + (cell_w - nw) // 2
                oy = ty + tile_hdr + (cell_h - nh) // 2
                canvas[oy : oy + nh, ox : ox + nw] = resized
            else:
                off_txt = "Camera Offline"
                (ow, oh), _ = cv2.getTextSize(off_txt, font, 0.65, 2)
                ox = tx + (cell_w - ow) // 2
                oy = ty + tile_hdr + (cell_h + oh) // 2
                cv2.putText(canvas, off_txt, (ox, oy), font, 0.65, (11, 158, 245), 2, cv2.LINE_AA)

        ok_jpg, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
        if not ok_jpg:
            return None, len(enabled_cams), live_count
        return buf.tobytes(), len(enabled_cams), live_count

    def refresh_drive_preview(self) -> dict[str, Any]:
        with self._preview_refresh_lock:
            jpg_bytes, total_views, live_views = self.render_preview_overview_jpeg()
            if not jpg_bytes:
                return {"ok": False, "error": "No live camera frames available", "views": total_views, "liveViews": 0}
            tmp_local = self.preview_local_path.with_suffix(".jpg.tmp")
            tmp_local.write_bytes(jpg_bytes)
            tmp_local.replace(self.preview_local_path)
            now_s = time.time()
            self._preview_last_refresh_s = now_s
            self._preview_updated_ms = int(now_s * 1000)

            dest = self._drive_preview_path()
            if dest is not None:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Write in-place so Google Drive for Desktop preserves the permanent file ID across revisions.
                    with open(dest, "wb") as fh:
                        fh.write(jpg_bytes)
                    fid = read_google_drive_file_id(dest)
                    self._preview_drive_dest_str = str(dest)
                    if fid:
                        self._preview_drive_file_id = fid
                        self._preview_drive_url = google_drive_view_url(fid)
                except Exception as exc:
                    self.log.warning("Failed writing Drive preview overview %s: %s", dest, exc)

            return {"ok": True, "views": total_views, "liveViews": live_views, **self.preview_drive_info()}

    def _trigger_preview_refresh_async(self) -> None:
        if self._preview_refresh_lock.locked():
            return

        def _worker() -> None:
            try:
                self.refresh_drive_preview()
            except Exception as exc:
                self.log.debug("Async preview refresh error: %s", exc)

        threading.Thread(target=_worker, name="drive-preview-refresh", daemon=True).start()

    def _probe_duration_sec(self, path: Path) -> int:
        try:
            res = subprocess.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            if res.returncode == 0 and res.stdout.strip():
                return max(1, int(round(float(res.stdout.strip()))))
        except Exception:
            pass
        return 0

    @staticmethod
    def _is_valid_clip_filename(name: str) -> bool:
        if not name or any(ch in name for ch in ("/", "\\", "*", "?", "[", "]", "\x00")):
            return False
        if ".." in name or name in (".", ".."):
            return False
        if not name.lower().endswith(".mp4"):
            return False
        return Path(name).name == name

    def recent_clips(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.clips_index_path.exists():
            return []
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        with self._clips_index_lock:
            try:
                st = self.clips_index_path.stat()
                sig = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                return []
            if sig != self._clips_cache_sig:
                parsed: list[dict[str, Any]] = []
                for line in reversed(self.clips_index_path.read_text(encoding="utf-8").splitlines()):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        parsed.append(obj)
                self._clips_cache_sig = sig
                self._clips_cache_rows = parsed
                self._drive_neg_cache.clear()
            rows = [dict(r) for r in self._clips_cache_rows[: max(0, int(limit))]]

        now_mono = time.monotonic()
        if backup_dir:
            b_path = Path(backup_dir)
            for item in rows:
                if item.get("driveUrl"):
                    continue
                fname = str(item.get("fileName") or "").strip()
                parts = [p for p in str(item.get("lanUrl") or "").split("/") if p]
                day_str = parts[1] if len(parts) >= 3 and parts[0] == "clips" else ""
                if not (fname and day_str and self._is_valid_clip_filename(fname)):
                    continue
                cache_key = f"{day_str}/{fname}"
                if self._drive_neg_cache.get(cache_key, 0.0) > now_mono:
                    continue
                fid = read_google_drive_file_id(b_path / day_str / fname)
                url = google_drive_view_url(fid)
                if fid and url:
                    item["driveFileId"] = fid
                    item["driveUrl"] = url
                    with self._clips_index_lock:
                        for cached_row in self._clips_cache_rows:
                            if cached_row.get("fileName") == fname:
                                cached_row["driveFileId"] = fid
                                cached_row["driveUrl"] = url
                else:
                    self._drive_neg_cache[cache_key] = now_mono + 60.0
        return rows

    def _resolve_clip_candidates(
        self, lan_url: str = "", file_name: str = ""
    ) -> tuple[str, str, list[str], list[Path]]:
        raw_url = str(lan_url or "").strip()
        raw_fname = str(file_name or "").strip()
        if raw_fname and not self._is_valid_clip_filename(raw_fname):
            return raw_url, "", [], []

        rel_path = ""
        if "/clips/" in raw_url:
            rel_path = raw_url.split("/clips/", 1)[1].split("?", 1)[0].strip("/")
        elif raw_url and not raw_fname:
            return raw_url, "", [], []

        parts = [p for p in rel_path.split("/") if p] if rel_path else []
        if any(p in (".", "..") or any(ch in p for ch in ("*", "?", "[", "]", "\\", "\x00")) for p in parts):
            return raw_url, "", [], []

        if parts:
            cand_name = parts[-1]
            if not self._is_valid_clip_filename(cand_name):
                return raw_url, "", [], []
            if len(parts) >= 2 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-2]):
                return raw_url, "", [], []
            target_name = cand_name
        else:
            target_name = raw_fname

        if not self._is_valid_clip_filename(target_name):
            return raw_url, "", parts, []

        candidates: list[Path] = []
        if len(parts) >= 2:
            candidates.append(self.clips_root / parts[-2] / target_name)
        elif self.clips_root.is_dir():
            try:
                with os.scandir(self.clips_root) as it:
                    for ent in it:
                        if not ent.is_dir(follow_symlinks=False):
                            continue
                        cand = Path(ent.path) / target_name
                        if cand.is_file():
                            candidates.append(cand)
                            break
            except OSError:
                pass
        return raw_url, target_name, parts, candidates

    def _find_clip_index_row(self, target_name: str) -> dict[str, Any] | None:
        if not target_name or not self.clips_index_path.exists():
            return None
        with self._clips_index_lock:
            lines = self.clips_index_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            fname = str(obj.get("fileName") or "")
            lurl = str(obj.get("lanUrl") or "")
            if fname == target_name or lurl.endswith(f"/{target_name}"):
                return obj
        return None

    def set_clip_audio(
        self,
        lan_url: str = "",
        file_name: str = "",
        start_ms: int | None = None,
        end_ms: int | None = None,
        *,
        include_audio: bool = False,
    ) -> tuple[dict[str, Any], int]:
        """Losslessly strip or mux 14-day PowerConf S330 room audio on an existing saved MP4 clip."""
        base_resolved = self.clips_root.resolve()
        _raw_url, target_name, _parts, candidates = self._resolve_clip_candidates(lan_url, file_name)
        if not target_name:
            return {"ok": False, "error": "Invalid clip filename"}, 400
        target: Path | None = None
        for cand in candidates:
            try:
                resolved = cand.resolve()
                resolved.relative_to(base_resolved)
            except Exception:
                continue
            if resolved.is_file():
                target = resolved
                break
        if target is None:
            return {"ok": False, "error": "Clip file not found"}, 404

        idx_row = self._find_clip_index_row(target.name) or {}
        if start_ms is None and idx_row.get("startMs") is not None:
            start_ms = int(idx_row["startMs"])
        if end_ms is None and idx_row.get("endMs") is not None:
            end_ms = int(idx_row["endMs"])

        day_str = target.parent.name
        rel_url = f"/clips/{day_str}/{target.name}"
        tag = uuid.uuid4().hex[:8]
        tmp_out = target.with_name(f".{target.stem}.{tag}.mp4")
        tmp_aud: Path | None = None
        try:
            if not include_audio:
                cmd = [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(target),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(tmp_out),
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
                if res.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                    return {"ok": False, "error": res.stderr.strip() or "Failed to strip audio"}, 500
                tmp_out.replace(target)
                aud_src = "none"
            else:
                if start_ms is None or end_ms is None or int(end_ms) <= int(start_ms):
                    return {"ok": False, "error": "Clip timestamps unavailable for audio mux"}, 400
                tmp_aud = target.with_name(f".aud_{target.stem}_{tag}.m4a")
                if not self.room_audio.extract_window_m4a(int(start_ms), int(end_ms), tmp_aud):
                    return {"ok": False, "error": "Room audio unavailable or expired (>14d)"}, 404
                cmd = [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(target),
                    "-i",
                    str(tmp_aud),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(tmp_out),
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
                if res.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                    return {"ok": False, "error": res.stderr.strip() or "Failed to mux room audio"}, 500
                tmp_out.replace(target)
                aud_src = "powerconf"
        finally:
            tmp_out.unlink(missing_ok=True)
            if tmp_aud is not None:
                tmp_aud.unlink(missing_ok=True)

        bytes_len = target.stat().st_size
        drive_info = self.mirror_clip_to_drive(target, day_str)
        updated = {
            **idx_row,
            "ok": True,
            "lanUrl": rel_url,
            "fileName": target.name,
            "bytes": bytes_len,
            "hasAudio": bool(include_audio),
            "audioSource": aud_src,
            **({"driveFileId": drive_info["driveFileId"]} if drive_info.get("driveFileId") else {}),
            **({"driveUrl": drive_info["driveUrl"]} if drive_info.get("driveUrl") else {}),
        }
        with self._clips_index_lock:
            lines: list[str] = []
            replaced = False
            if self.clips_index_path.exists():
                for line in self.clips_index_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        lines.append(line)
                        continue
                    fname = str(obj.get("fileName") or "")
                    lurl = str(obj.get("lanUrl") or "")
                    if fname == target.name or lurl == rel_url or lurl.endswith(f"/{target.name}"):
                        if not replaced:
                            lines.append(json.dumps({k: v for k, v in updated.items() if k != "thumb"}))
                            replaced = True
                        continue
                    lines.append(line)
            if not replaced:
                lines.append(json.dumps({k: v for k, v in updated.items() if k != "thumb"}))
            tmp_idx = self.clips_index_path.with_suffix(".jsonl.tmp")
            tmp_idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp_idx.replace(self.clips_index_path)
            self._clips_cache_sig = None
        return updated, 200

    def delete_clip(self, lan_url: str = "", file_name: str = "") -> dict[str, Any]:
        """Delete a saved MP4 from local `clips_root`, Google Drive mirror, and `index.jsonl` (preserves raw tapes)."""
        base_resolved = self.clips_root.resolve()
        _raw_url, target_name, parts, candidates = self._resolve_clip_candidates(lan_url, file_name)
        if not target_name:
            return {
                "ok": False,
                "error": "Invalid clip filename",
                "fileName": "",
                "localDeleted": False,
                "driveDeleted": False,
            }

        local_deleted = False
        drive_deleted = False
        with self.lock:
            backup_dir = self.drive_backup_dir.strip()
        backup_resolved = Path(backup_dir).resolve() if backup_dir else None

        with self._clips_index_lock:
            for cand in candidates:
                try:
                    resolved = cand.resolve()
                    rel = resolved.relative_to(base_resolved)
                except Exception:
                    continue
                if resolved.is_file():
                    try:
                        resolved.unlink()
                        local_deleted = True
                    except OSError:
                        pass
                if backup_resolved is not None and len(rel.parts) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel.parts[-2]):
                    d_file = (backup_resolved / rel.parts[-2] / rel.parts[-1]).resolve()
                    try:
                        d_file.relative_to(backup_resolved)
                    except ValueError:
                        d_file = None  # type: ignore[assignment]
                    if d_file is not None and d_file.is_file():
                        try:
                            d_file.unlink()
                            drive_deleted = True
                        except OSError:
                            pass
            if (
                backup_resolved is not None
                and not drive_deleted
                and len(parts) >= 2
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-2])
            ):
                d_file = (backup_resolved / parts[-2] / target_name).resolve()
                try:
                    d_file.relative_to(backup_resolved)
                    ok_drive_rel = True
                except ValueError:
                    ok_drive_rel = False
                if ok_drive_rel and d_file.is_file():
                    try:
                        d_file.unlink()
                        drive_deleted = True
                    except OSError:
                        pass
            if self.clips_index_path.exists():
                kept: list[str] = []
                for line in self.clips_index_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        kept.append(line)
                        continue
                    fname = str(obj.get("fileName") or "")
                    lurl = str(obj.get("lanUrl") or "")
                    if fname == target_name or lurl.endswith(f"/{target_name}"):
                        continue
                    kept.append(line)
                tmp = self.clips_index_path.with_suffix(".jsonl.tmp")
                tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                tmp.replace(self.clips_index_path)
            self._clips_cache_sig = None
            self._cal_cache_sig = None

        return {
            "ok": True,
            "fileName": target_name,
            "localDeleted": local_deleted,
            "driveDeleted": drive_deleted,
        }

    def _get_raw_frame_bgr(self, source_slug: str, *, prefer_live: bool = False) -> np.ndarray | None:
        """Fetch latest uncropped/unrotated BGR frame for `source_slug` from live MJPEG hub or ring TS."""
        if prefer_live:
            live_jpg = self.live_hub.recent_frame(source_slug, max_age_sec=3.0)
            if live_jpg:
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    return img
        with self.lock:
            segs = list(self.ring_segments.get(source_slug, []))
        for seg in reversed(segs[-4:]):
            if seg.path.exists():
                cap = cv2.VideoCapture(str(seg.path))
                if cap.isOpened():
                    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    ok, frm = (False, None)
                    if total > 1:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
                        ok, frm = cap.read()
                    if not ok or frm is None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frm = cap.read()
                    cap.release()
                    if ok and frm is not None:
                        return frm
        if not prefer_live:
            live_jpg = self.live_hub.recent_frame(source_slug, max_age_sec=3.0)
            if live_jpg:
                img = cv2.imdecode(np.frombuffer(live_jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    return img
        return None

    def _load_bump_state(self) -> None:
        """Restore persisted bump-alert cooldowns so restarts never re-spam recent bump alerts."""
        try:
            if not self.bump_state_path.is_file():
                return
            raw = json.loads(self.bump_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            last_ntfy = raw.get("lastNtfy")
            if isinstance(last_ntfy, dict):
                self._bump_last_ntfy = {
                    str(k): float(v) for k, v in last_ntfy.items() if isinstance(v, (int, float))
                }
            last_shift = raw.get("lastShiftPx")
            if isinstance(last_shift, dict):
                self._bump_last_shift = {
                    str(k): float(v) for k, v in last_shift.items() if isinstance(v, (int, float))
                }
        except Exception as exc:
            self.log.debug("Failed loading bump state: %s", exc)

    def _save_bump_state_locked(self) -> None:
        """Persist bump-alert cooldown timestamps & last alerted shift atomically."""
        try:
            payload = {
                "lastNtfy": {k: round(float(v), 3) for k, v in self._bump_last_ntfy.items()},
                "lastShiftPx": {k: round(float(v), 1) for k, v in self._bump_last_shift.items()},
            }
            tmp_p = self.bump_state_path.with_suffix(".tmp")
            tmp_p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp_p, self.bump_state_path)
        except Exception as exc:
            self.log.debug("Failed saving bump state: %s", exc)

    def _bump_ref_candidates(self, phys_slug: str) -> list[tuple[Path, np.ndarray]]:
        """Load primary (`<slug>.png`) and optional opposite-lighting (`<slug>_alt.png`) bump baselines."""
        out: list[tuple[Path, np.ndarray]] = []
        for fname in (f"{phys_slug}.png", f"{phys_slug}_alt.png"):
            p = self.bump_refs_root / fname
            if p.is_file():
                im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if im is not None and im.size > 0:
                    out.append((p, im))
        return out

    @staticmethod
    def _is_ir_frame(bgr: np.ndarray) -> bool:
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] < 3:
            return True
        small = cv2.resize(bgr, (160, 90), interpolation=cv2.INTER_AREA)
        return float(np.mean(np.abs(small[:, :, 0].astype(np.float32) - small[:, :, 2].astype(np.float32)))) < 2.2

    def capture_bump_baseline(self, camera_name: str | None = None) -> dict[str, Any]:
        """Save crisp 960x540 grayscale baseline PNGs into `bump_refs/<slug>.png` for bump alerts & realign HUD."""
        with self.lock:
            if camera_name:
                target = self._find_camera(camera_name)
                if not target:
                    return {"ok": False, "error": f"Unknown camera: {camera_name}"}
                phys_list = [self._source_camera(target)]
            else:
                phys_list = [p for p in self._physical_cameras() if p.enabled]

        saved: list[str] = []
        for phys in phys_list:
            raw_bgr = self._get_raw_frame_bgr(phys.slug, prefer_live=True)
            if raw_bgr is None:
                continue
            gray = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2GRAY) if raw_bgr.ndim == 3 else raw_bgr
            small = cv2.resize(gray, BUMP_OVERLAY_SIZE, interpolation=cv2.INTER_AREA)
            out_p = self.bump_refs_root / f"{phys.slug}.png"
            alt_p = self.bump_refs_root / f"{phys.slug}_alt.png"
            if cv2.imwrite(str(out_p), small):
                try:
                    if alt_p.is_file():
                        alt_p.unlink()
                except OSError:
                    pass
                with self.lock:
                    self._bump_strikes[phys.slug] = 0
                    self._bump_last_ntfy.pop(phys.slug, None)
                    self._bump_last_shift.pop(phys.slug, None)
                    self._bump_overlay_cache = {
                        k: v for k, v in self._bump_overlay_cache.items() if k[0] != phys.slug
                    }
                    self._save_bump_state_locked()
                saved.append(phys.slug)
        return {"ok": bool(saved), "saved": saved, "refDims": list(BUMP_OVERLAY_SIZE)}

    def live_bump_alignment(self, camera_name: str) -> dict[str, Any]:
        """Real-time alignment telemetry for the Realign Cam HUD (rotation-proof ORB+RANSAC + 320p fallback)."""
        with self.lock:
            target = self._find_camera(camera_name)
            if not target:
                return {"ok": False, "error": f"Unknown camera: {camera_name}"}
            phys = self._source_camera(target)
            segs = list(self.ring_segments.get(phys.slug, []))

        cands = self._bump_ref_candidates(phys.slug)
        if not cands:
            return {
                "ok": True,
                "slug": phys.slug,
                "name": phys.name,
                "hasRef": False,
                "confident": False,
                "aligned": False,
                "close": False,
                "bumped": False,
                "dxPx": 0.0,
                "dyPx": 0.0,
                "shiftPx": 0.0,
                "dAngle": 0.0,
                "response": 0.0,
                "guide": {"pitch": "ok", "yaw": "ok", "roll": "ok", "pitchPx": 0.0, "yawPx": 0.0, "rollDeg": 0.0},
            }

        primary_path, primary_gray = cands[0]
        ref_mtime_ms = int(primary_path.stat().st_mtime * 1000)
        cur_bgr = self._get_raw_frame_bgr(phys.slug, prefer_live=True)
        if cur_bgr is None:
            return {
                "ok": False,
                "slug": phys.slug,
                "name": phys.name,
                "hasRef": True,
                "refDims": [int(primary_gray.shape[1]), int(primary_gray.shape[0])],
                "refUpdatedMs": ref_mtime_ms,
                "error": "No live frame available",
            }

        native_w = float(self._probe_segment_dims(segs[-1].path)[0]) if (segs and segs[-1].path.exists()) else float(cur_bgr.shape[1])
        best_res: dict[str, Any] | None = None
        for _rp, r_gray in cands:
            cand_res = detect_camera_bump(r_gray, cur_bgr, high_res=True, ref_width=native_w)
            if best_res is None:
                best_res = cand_res
            else:
                b_key = (bool(best_res.get("confident")), best_res.get("method") == "orb", float(best_res.get("response", 0.0)))
                c_key = (bool(cand_res.get("confident")), cand_res.get("method") == "orb", float(cand_res.get("response", 0.0)))
                if c_key > b_key:
                    best_res = cand_res
        res = best_res or {"ok": False, "confident": False}
        dx_px = float(res.get("dxPx", 0.0))
        dy_px = float(res.get("dyPx", 0.0))
        d_ang = float(res.get("dAngle", 0.0))
        confident = bool(res.get("confident", False))
        # Physical camera correction directions (verified by synthetic warp & live landmarks):
        # dy < 0 -> scene moved UP -> camera tilted DOWN -> fix: tilt UP
        # dx < 0 -> scene moved LEFT -> camera panned RIGHT -> fix: pan LEFT
        # dAngle < 0 -> scene rotated CCW -> camera rolled CW -> fix: roll CCW
        pitch = "ok" if (not confident or abs(dy_px) < 4.0) else ("up" if dy_px < 0.0 else "down")
        yaw = "ok" if (not confident or abs(dx_px) < 4.0) else ("left" if dx_px < 0.0 else "right")
        roll = "ok" if (not confident or abs(d_ang) < 0.35) else ("ccw" if d_ang < 0.0 else "cw")
        return {
            **res,
            "slug": phys.slug,
            "name": phys.name,
            "hasRef": True,
            "refDims": [int(primary_gray.shape[1]), int(primary_gray.shape[0])],
            "refUpdatedMs": ref_mtime_ms,
            "guide": {
                "pitch": pitch,
                "yaw": yaw,
                "roll": roll,
                "pitchPx": round(abs(dy_px), 1),
                "yawPx": round(abs(dx_px), 1),
                "rollDeg": round(abs(d_ang), 2),
            },
        }

    def adapt_views_to_current_framing(self, camera_name: str) -> dict[str, Any]:
        """Auto-shift all crops, tilts, and mirror masks on `camera_name` to match its new position, then save baseline."""
        with self.lock:
            target = self._find_camera(camera_name)
            if not target:
                return {"ok": False, "error": f"Unknown camera: {camera_name}"}
            phys = self._source_camera(target)
            segs = list(self.ring_segments.get(phys.slug, []))

        cands = self._bump_ref_candidates(phys.slug)
        if not cands:
            return {"ok": False, "error": "No saved baseline reference to measure shift from"}

        cur_bgr = self._get_raw_frame_bgr(phys.slug, prefer_live=True)
        if cur_bgr is None:
            return {"ok": False, "error": "No live frame available"}
        native_w = float(self._probe_segment_dims(segs[-1].path)[0]) if (segs and segs[-1].path.exists()) else float(cur_bgr.shape[1])

        # High-precision 960x540 ORB+RANSAC across primary & optional alt lighting baselines
        best_orb: dict[str, Any] | None = None
        for _rp, r_gray in cands:
            cand = estimate_rigid_alignment_orb(
                r_gray,
                cur_bgr,
                work_size=BUMP_OVERLAY_SIZE,
                nfeatures=1500,
                ref_width=native_w,
            )
            if cand is not None and (best_orb is None or cand["inliers"] > best_orb["inliers"]):
                best_orb = cand

        w960, h540 = BUMP_OVERLAY_SIZE
        if best_orb is not None:
            mat_960 = best_orb["matrix960"]
            dx_px = float(best_orb["dxPx"])
            dy_px = float(best_orb["dyPx"])
            d_ang = float(best_orb["dAngle"])
            inliers = int(best_orb["inliers"])
            method = "orb"
        else:
            # Fallback: build rigid matrix from 320x180 phase correlation
            ph = detect_camera_bump(cands[0][1], cur_bgr, high_res=False, ref_width=native_w)
            if not ph.get("confident"):
                return {"ok": False, "error": "Camera view unclear or obstructed — step back and retry"}
            dx_px = float(ph["dxPx"])
            dy_px = float(ph["dyPx"])
            d_ang = float(ph["dAngle"])
            scale_to_960 = w960 / max(native_w, 1.0)
            rot_m = cv2.getRotationMatrix2D((w960 / 2.0, h540 / 2.0), -d_ang, 1.0)
            rot_m[0, 2] += dx_px * scale_to_960
            rot_m[1, 2] += dy_px * scale_to_960
            mat_960 = rot_m.astype(np.float64)
            inliers = 0
            method = "phase"

        mat_3 = np.vstack([mat_960, [0.0, 0.0, 1.0]])
        updated_views: list[dict[str, Any]] = []

        with self.lock:
            views = self._views_for_source(phys.slug, enabled_only=False)
            for v in views:
                old_crop = list(v.crop)
                old_tilt = float(v.tilt)
                old_excl = [[[pt[0], pt[1]] for pt in q] for q in v.exclusions]
                new_tilt = normalize_tilt(old_tilt - d_ang)

                r_old, old_fw, old_fh = view_transform_matrix(w960, h540, rotate=v.rotate, tilt=old_tilt)
                r_new, new_fw, new_fh = view_transform_matrix(w960, h540, rotate=v.rotate, tilt=new_tilt)
                comp = r_new @ mat_3 @ np.linalg.inv(r_old)

                def _map_norm(u: float, vv: float) -> tuple[float, float]:
                    vec = comp @ np.array([u * old_fw, vv * old_fh, 1.0], dtype=np.float64)
                    return (
                        max(0.0, min(1.0, float(vec[0]) / float(new_fw))),
                        max(0.0, min(1.0, float(vec[1]) / float(new_fh))),
                    )

                # Shift crop box via its center in exact tilted-view coordinates while preserving dimensions
                if tuple(old_crop) != (0.0, 0.0, 1.0, 1.0):
                    cx = (old_crop[0] + old_crop[2]) / 2.0
                    cy = (old_crop[1] + old_crop[3]) / 2.0
                    cw = old_crop[2] - old_crop[0]
                    ch = old_crop[3] - old_crop[1]
                    ncx, ncy = _map_norm(cx, cy)
                    nx0 = max(0.0, min(1.0 - cw, ncx - cw / 2.0))
                    ny0 = max(0.0, min(1.0 - ch, ncy - ch / 2.0))
                    # Preserve edge-pinned x0=0.0 (e.g. Fly Machine Cam) if still flush with left edge
                    if old_crop[0] <= 0.001 and nx0 <= 0.018:
                        nx0 = 0.0
                    nx1 = min(1.0, nx0 + cw)
                    ny1 = min(1.0, ny0 + ch)
                    v.crop = normalize_crop((round(nx0, 4), round(ny0, 4), round(nx1, 4), round(ny1, 4)))
                v.tilt = new_tilt

                if v.exclusions:
                    shifted_excls = []
                    for q in v.exclusions:
                        pts = [
                            (round(nu, 4), round(nv, 4))
                            for nu, nv in (_map_norm(pt[0], pt[1]) for pt in q)
                        ]
                        shifted_excls.append(pts)
                    v.exclusions = normalize_exclusions(shifted_excls)

                updated_views.append(
                    {
                        "name": v.name,
                        "slug": v.slug,
                        "oldCrop": old_crop,
                        "newCrop": list(v.crop),
                        "oldTilt": old_tilt,
                        "newTilt": float(v.tilt),
                        "exclusionsUpdated": len(old_excl),
                    }
                )
            self.save_config()

        # Refresh 960x540 baseline to current live frame (clears cooldown & stale alt ref)
        base_res = self.capture_bump_baseline(phys.slug)
        if self._started:
            self._trigger_preview_refresh_async()
        self.log.info(
            "Auto-adapted %d view(s) on %s (dx=%+.1fpx, dy=%+.1fpx, roll=%+.2fdeg, method=%s, inliers=%d)",
            len(updated_views),
            phys.name,
            dx_px,
            dy_px,
            d_ang,
            method,
            inliers,
        )
        return {
            "ok": True,
            "slug": phys.slug,
            "name": phys.name,
            "method": method,
            "inliers": inliers,
            "dxPx": round(dx_px, 1),
            "dyPx": round(dy_px, 1),
            "shiftPx": round(math.hypot(dx_px, dy_px), 1),
            "dAngle": round(d_ang, 2),
            "views": updated_views,
            "baselineSaved": bool(base_res.get("ok")),
        }

    def get_bump_overlay_png(self, camera_name: str, mode: str = "edges", color: str = "cyan") -> tuple[bytes | None, str]:
        """Return cached 960x540 transparent wireframe or grayscale ghost PNG for a physical camera baseline."""
        with self.lock:
            target = self._find_camera(camera_name)
            if not target:
                return None, f"Unknown camera: {camera_name}"
            phys = self._source_camera(target)

        ref_path = self.bump_refs_root / f"{phys.slug}.png"
        if not ref_path.is_file():
            return None, "No saved baseline reference"
        mtime_ns = int(ref_path.stat().st_mtime_ns)
        clean_mode = "ghost" if mode == "ghost" else "edges"
        clean_color = color if color in _OVERLAY_COLORS_BGR else "cyan"
        cache_key = (phys.slug, mtime_ns, clean_mode, clean_color)
        with self.lock:
            cached = self._bump_overlay_cache.get(cache_key)
        if cached is not None:
            return cached, ""

        ref_gray = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
        if ref_gray is None:
            return None, "Baseline unreadable"
        png_bytes = render_bump_overlay_png(ref_gray, mode=clean_mode, color=clean_color)
        if not png_bytes:
            return None, "Failed rendering overlay"
        with self.lock:
            self._bump_overlay_cache = {
                k: v for k, v in self._bump_overlay_cache.items() if k[0] != phys.slug or k[1] == mtime_ns
            }
            self._bump_overlay_cache[cache_key] = png_bytes
        return png_bytes, ""

    def check_camera_bump_background(self, now_s: float) -> list[dict[str, Any]]:
        """Quiet-gym bump guard: 0ms when off; lazily saves baseline, confirms 2x, fires ntfy alert only."""
        with self.lock:
            if not self.bump_alert_ntfy or not self.ntfy_topic:
                return []
            if self.recording and not self.paused:
                return []
            phys_cams = [p for p in self._physical_cameras() if p.enabled]
            topic = self.ntfy_topic

        events: list[dict[str, Any]] = []
        for phys in phys_cams:
            with self.lock:
                views = self._views_for_source(phys.slug, enabled_only=True)
                quiet = all(self.latest_energy.get(v.slug, 0.0) < 1.0 for v in views)
                has_media = (
                    self.cam_connected.get(phys.slug, False)
                    or bool(self.ring_segments.get(phys.slug))
                    or (self.live_hub.recent_frame(phys.slug, max_age_sec=10.0) is not None)
                )
            if not quiet or not has_media:
                continue

            ref_path = self.bump_refs_root / f"{phys.slug}.png"
            if not ref_path.is_file():
                self.capture_bump_baseline(phys.slug)
                continue

            min_interval = 30.0 if self._bump_strikes.get(phys.slug, 0) == 1 else BUMP_CHECK_INTERVAL_SEC
            last_chk = self._bump_last_check.get(phys.slug)
            if last_chk is not None and (now_s - last_chk) < min_interval:
                continue
            self._bump_last_check[phys.slug] = now_s

            ref_gray = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
            cur_bgr = self._get_raw_frame_bgr(phys.slug)
            if ref_gray is None or cur_bgr is None:
                continue

            cands = self._bump_ref_candidates(phys.slug)
            res: dict[str, Any] | None = None
            for _rp, rg in cands:
                cand_r = detect_camera_bump(rg, cur_bgr)
                if res is None or (bool(cand_r.get("ok")), float(cand_r.get("response", 0.0))) > (
                    bool(res.get("ok")),
                    float(res.get("response", 0.0)),
                ):
                    res = cand_r
            if not res or not res.get("ok"):
                continue
            if not res.get("bumped"):
                self._bump_strikes[phys.slug] = 0
                # Lazily capture opposite-lighting companion baseline (<slug>_alt.png) once aligned in a new lighting mode
                alt_path = self.bump_refs_root / f"{phys.slug}_alt.png"
                if (
                    not alt_path.is_file()
                    and float(res.get("shiftPx", 99.0)) <= 4.0
                    and abs(float(res.get("dAngle", 99.0))) <= 0.35
                    and 0.12 <= float(res.get("response", 0.0)) < 0.32
                ):
                    try:
                        g_cur = cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2GRAY) if cur_bgr.ndim == 3 else cur_bgr
                        cv2.imwrite(str(alt_path), cv2.resize(g_cur, BUMP_OVERLAY_SIZE, interpolation=cv2.INTER_AREA))
                    except Exception:
                        pass
                continue

            strikes = self._bump_strikes.get(phys.slug, 0) + 1
            self._bump_strikes[phys.slug] = strikes
            if strikes < 2:
                continue

            self._bump_strikes[phys.slug] = 0
            shift_px = float(res["shiftPx"])
            with self.lock:
                last_n = self._bump_last_ntfy.get(phys.slug)
                last_sh = self._bump_last_shift.get(phys.slug)
                in_cooldown = last_n is not None and 0.0 <= (now_s - last_n) < BUMP_NTFY_COOLDOWN_SEC
                escalated = bool(in_cooldown and last_sh is not None and shift_px >= (last_sh + BUMP_PX_THRESHOLD))
                can_ntfy = bool(topic and ((not in_cooldown) or escalated))
                if can_ntfy:
                    self._bump_last_ntfy[phys.slug] = now_s
                    self._bump_last_shift[phys.slug] = shift_px
                    self._save_bump_state_locked()
            self.log.info(
                "Camera bump detected on %s (%.1fpx, %+.1fdeg); notified=%s%s",
                phys.name,
                shift_px,
                float(res["dAngle"]),
                can_ntfy,
                " (escalated)" if escalated else ("" if can_ntfy else " (cooldown)"),
            )
            if can_ntfy:
                title = f"Armory Cam Bumped: {phys.name}"
                msg = (
                    f"{phys.name} appears bumped ({res['shiftPx']:.0f}px, {res['dAngle']:+.1f}°). "
                    f"Open http://{detect_lan_ip()}:{self.port}/#realign={phys.slug} to realign."
                )
                threading.Thread(
                    target=send_ntfy_alert,
                    args=(topic, title, msg),
                    kwargs={"tags": "movie_camera,warning"},
                    name="ntfy-cam-bump",
                    daemon=True,
                ).start()
            events.append({"slug": phys.slug, "shiftPx": res["shiftPx"], "dAngle": res["dAngle"], "notified": can_ntfy})
        return events

    def calibration_status(self) -> dict[str, Any]:
        """Return unique clean clip count and notified pose-calibration milestones."""
        names: set[str] = set()
        if self.clips_root.exists():
            try:
                with os.scandir(self.clips_root) as it:
                    for ent in it:
                        if not ent.is_dir(follow_symlinks=False):
                            continue
                        try:
                            with os.scandir(ent.path) as sub_it:
                                for f_ent in sub_it:
                                    if f_ent.name.lower().endswith(".mp4") and f_ent.is_file(follow_symlinks=False):
                                        names.add(f_ent.name)
                        except OSError:
                            pass
            except OSError:
                pass
        clip_count = len(names)

        notified: list[int] = []
        with self._clips_index_lock:
            if self.cal_state_path.is_file():
                try:
                    raw = json.loads(self.cal_state_path.read_text(encoding="utf-8"))
                    notified = [int(x) for x in raw.get("notified", []) if isinstance(x, (int, float))]
                except Exception:
                    notified = []
        next_m = next((m for m in CALIBRATION_MILESTONES if clip_count < m), None)
        return {
            "clipCount": clip_count,
            "milestones": list(CALIBRATION_MILESTONES),
            "notified": sorted(set(notified)),
            "nextMilestone": next_m,
        }

    def check_calibration_milestones(self) -> list[int]:
        """Persist newly reached calibration clip milestones (5 / 15 / 50) and fire ntfy alert once per tier."""
        with self._clips_index_lock:
            st = self.calibration_status()
            count = int(st["clipCount"])
            notified = list(st["notified"])
            crossed = [m for m in CALIBRATION_MILESTONES if count >= m and m not in notified]
            if not crossed:
                return []
            merged = sorted(set(notified + crossed))
            tmp = self.cal_state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"notified": merged, "clipCount": count, "updatedAtMs": int(time.time() * 1000)}),
                encoding="utf-8",
            )
            tmp.replace(self.cal_state_path)
        with self.lock:
            topic = self.ntfy_topic

        if topic:
            top = max(crossed)
            if top == 5:
                title = "Armory Fit: 5 Clean Clips Ready"
                msg = f"You now have {count} clean Gym Cam clips saved. Tell Antigravity 'calibrate pose trimming' anytime for the early unrack-to-rack pass."
            elif top == 15:
                title = "Armory Fit: 15 Clips Collected"
                msg = f"{count} clean clips across multiple lifts are ready for multi-exercise pose-trim tuning in Antigravity."
            else:
                title = "Armory Fit: 50 Clips Collected"
                msg = f"{count} clean clips saved! Ready for full unrack-to-rack calibration lock-in in Antigravity."
            threading.Thread(
                target=send_ntfy_alert,
                args=(topic, title, msg),
                name="ntfy-cal-milestone",
                daemon=True,
            ).start()
        return crossed

    def disk_stats(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        return {
            "root": str(self.root),
            "freeGb": round(usage.free / (1024**3), 1),
            "totalGb": round(usage.total / (1024**3), 1),
        }

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            cams = [
                {
                    "name": c.name,
                    "slug": c.slug,
                    "sourceSlug": c.source_slug,
                    "enabled": c.enabled,
                    "defish": c.defish,
                    "connected": self._is_source_connected_locked(self._source_slug_for(c)),
                    "watchdogRestarts": int(self._cam_watchdog_restarts.get(self._source_slug_for(c), 0)),
                    "latestEnergy": float(self.latest_energy.get(c.slug, 0.0)),
                    "motionThreshold": c.motion_threshold,
                    "ringSegments": len(
                        self.ring_segments.get(c.slug) or self.ring_segments.get(self._source_slug_for(c), [])
                    ),
                }
                for c in self.cameras
            ]
            wb_cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
            wb_url = self.effective_wallboard_url()
            rec = self.recording
            paused = self.paused
            bump_alert_ntfy = self.bump_alert_ntfy
            motion_record_247 = self.motion_record_247
            last_poll_ok_ms = self._last_poll_ok_ms

        wb_status = {
            **self.adb.status_dict(wb_cfg),
            **self.wallboard.status_dict(),
            "wallboardUrl": wb_url,
            "wakeOnStart": wb_cfg.wake_on_start,
            "sleepOnStop": wb_cfg.sleep_on_stop,
            "idleSleepSec": wb_cfg.idle_sleep_sec,
            "displayClient": wb_cfg.display_client,
            "enabled": wb_cfg.enabled,
        }
        return {
            "service": "armory-cam",
            "serverEpochMs": int(time.time() * 1000),
            "lastPollOkMs": last_poll_ok_ms,
            "recording": rec,
            "paused": paused,
            "bumpAlertNtfy": bump_alert_ntfy,
            "motionRecord247": motion_record_247,
            "disk": self.disk_stats(),
            "calibration": self.calibration_status(),
            "preview": self.preview_drive_info(),
            "armedClip": self.active_server_armed_clip(),
            "cameras": cams,
            "wallboard": wb_status,
            "voice": self.voice.status_dict(),
            "houseAudio": self.house_audio.status_dict(live_probe=False),
            "displays": self.displays.status_dict(live_probe=False),
            "shows": self.shows.status_dict(),
        }

    def _wake_wallboard_sync(self, force: bool = True) -> dict[str, Any]:
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
            url = self.effective_wallboard_url()
        if self.wallboard.clear_stale_active(cfg.idle_sleep_sec):
            self.voice.set_wallboard_active(False)
        return self.adb.wake_and_launch(cfg, url, force=force)

    def _sleep_wallboard_sync(self) -> dict[str, Any]:
        with self.lock:
            cfg = WallboardConfig.from_dict(self.wallboard_cfg.to_dict())
        return self.adb.schedule_sleep(cfg, delay_sec=0.0, reason="display_off")

    def arm_server_clip(self, camera: str | None = None, guest: bool = False) -> dict[str, Any]:
        """Idempotently arm OptiPlex server-hosted clip recording, preserving the original start timestamp."""
        now_ms = int(time.time() * 1000)
        with self.lock:
            cur = self.server_armed_clip
            if (
                isinstance(cur, dict)
                and (now_ms - int(cur.get("armedAtMs") or 0)) < 300_000
                and bool(cur.get("guest")) == bool(guest)
            ):
                if camera:
                    cur["camera"] = camera
                return dict(cur)
            entry = {
                "armedAtMs": now_ms,
                "camera": camera,
                "guest": bool(guest),
                "sessionId": self.active_session_id or "",
            }
            self.server_armed_clip = entry
            return dict(entry)

    def clear_server_armed_clip(self) -> dict[str, Any] | None:
        with self.lock:
            cur = self.server_armed_clip
            self.server_armed_clip = None
            return dict(cur) if isinstance(cur, dict) else None

    def active_server_armed_clip(self, max_age_ms: int = 300_000) -> dict[str, Any] | None:
        now_ms = int(time.time() * 1000)
        with self.lock:
            cur = self.server_armed_clip
            if not isinstance(cur, dict):
                return None
            if (now_ms - int(cur.get("armedAtMs") or 0)) >= max_age_ms:
                self.server_armed_clip = None
                return None
            return dict(cur)

    def cut_server_armed_or_recent_clip(self, *, mode: str = "armed", camera: str | None = None) -> list[dict[str, Any]]:
        """Cut a clip directly on OptiPlex when saving an armed clip or 'clip that' without the phone app."""
        now_ms = int(time.time() * 1000)
        armed = self.clear_server_armed_clip()
        with self.lock:
            enabled_cams = [c.name for c in self.cameras if c.enabled]
            active_sid = self.active_session_id or ""
            active_name = self.active_session_name or ""

        if mode == "armed":
            if not armed:
                return []
            start_ms = int(armed["armedAtMs"])
            end_ms = max(start_ms + 3000, now_ms + 1500)
            cam = camera or armed.get("camera")
            cams = [cam] if cam else (enabled_cams[:2] if enabled_cams else ["Rack Cam"])
            ex_name = "Guest Clip" if armed.get("guest") else (active_name or "Gym Clip")
            sid = str(armed.get("sessionId") or active_sid or f"motion_{datetime.now().strftime('%Y-%m-%d')}")
            return self.cut_clips_multi(
                session_id=sid,
                ex_name=ex_name,
                start_ms=start_ms,
                end_ms=end_ms,
                cameras=cams,
            )

        # mode == "clip_last"
        if armed and (now_ms - int(armed.get("armedAtMs") or 0)) >= 2000:
            start_ms = int(armed["armedAtMs"])
            end_ms = max(start_ms + 3000, now_ms + 1500)
            cam = camera or armed.get("camera")
            ex_name = "Guest Clip" if armed.get("guest") else (active_name or "Gym Clip")
            sid = str(armed.get("sessionId") or active_sid or f"motion_{datetime.now().strftime('%Y-%m-%d')}")
        else:
            start_ms = now_ms - 25_000
            end_ms = now_ms
            cam = camera
            ex_name = active_name or "Gym Clip"
            sid = active_sid or f"motion_{datetime.now().strftime('%Y-%m-%d')}"
        cams = [cam] if cam else ["Rack Cam"]
        return self.cut_clips_multi(
            session_id=sid,
            ex_name=ex_name,
            start_ms=start_ms,
            end_ms=end_ms,
            cameras=cams,
        )

    def _handle_local_voice_command(self, transcript: str) -> dict[str, Any] | None:
        show_match = self.shows.match_voice_command(
            transcript,
            wake_word=self.voice.cfg.wake_word,
            workout_active=bool(self.voice.context_active),
        )
        if show_match:
            if show_match.get("action") == "stop":
                self.shows.stop_active_show(restore_wallboard=True)
                return {
                    "handled": True,
                    "ok": True,
                    "target": "show",
                    "action": "stop",
                    "speech": "Show stopped",
                    "ackText": "⏹ Show stopped",
                }
            if show_match.get("action") == "lights":
                zone = str(show_match.get("zone") or "all")
                want_on = bool(show_match.get("on", True))
                res = self.shows._fire_lights_state(
                    want_on,
                    zone=zone,
                    tag=("lights_on" if want_on else "lights_off"),
                    label=f"({zone.upper()} LIGHTS {'ON' if want_on else 'OFF'})",
                )
                z_label = {"rack": "Rack lights", "smith": "Smith lights"}.get(zone, "House lights")
                state_word = "on" if want_on else "off"
                if res.get("skipped"):
                    speech = f"{z_label} relay IP not configured"
                    ack = f"⚠ {speech}"
                    ok = False
                else:
                    ok = bool(res.get("ok"))
                    speech = f"{z_label} {state_word}" if ok else f"{z_label} command failed"
                    ack = f"💡 {speech}" if ok else f"✗ {speech}"
                return {
                    "handled": True,
                    "ok": ok,
                    "target": "lights",
                    "zone": zone,
                    "on": want_on,
                    "speech": speech,
                    "ackText": ack,
                    "result": res,
                }
            res = self.shows.fire_show(
                str(show_match.get("showId") or ""),
                async_run=True,
                source="voice",
                caller=transcript[:120],
            )
            ok = bool(res.get("ok"))
            return {
                "handled": True,
                "ok": ok,
                "target": "show",
                "showId": show_match.get("showId", ""),
                "action": "fire",
                "speech": "",  # Keep S330 TTS silent so drop-in audio hits uninterrupted
                "ackText": f"🔥 {show_match.get('showName', 'Show')}" if ok else f"✗ {res.get('error', 'Show failed')}",
                "result": res,
            }

        parsed = parse_display_voice_command(transcript, wake_word=self.voice.cfg.wake_word)
        if parsed:
            targets = parsed.get("targets") or [parsed["target"]]
            if parsed["target"] == "multi" and isinstance(targets, list) and len(targets) > 1:
                res = self.displays.execute_many(targets, parsed["action"])
            else:
                res = self.displays.execute(parsed["target"], parsed["action"])
            silent = bool(parsed.get("silent"))
            ok = bool(res.get("ok"))
            raw_speech = str(parsed.get("speech") or "").strip()
            err_msg = str(res.get("error") or "Display command failed").strip()
            speech = "" if silent else (raw_speech if ok else err_msg)
            ack_label = raw_speech or str(res.get("message") or "Done").strip()
            return {
                "handled": True,
                "ok": ok,
                "target": parsed["target"],
                "targets": targets,
                "action": parsed["action"],
                "speech": speech,
                "ackText": f"✓ {ack_label}" if ok else f"✗ {err_msg}",
                "result": res,
            }

        cam_cmd = parse_camera_voice_command(transcript, wake_word=self.voice.cfg.wake_word)
        if cam_cmd:
            with self.voice.lock:
                phone_session_live = bool(
                    self.voice.sse_clients > 0 and (self.voice.context_active or self.voice.wallboard_active)
                )
            act = cam_cmd.get("action")
            cam = cam_cmd.get("camera")
            short_cam = re.sub(r"\s+cam$", "", cam, flags=re.IGNORECASE) if cam else ""
            if act == "arm":
                is_guest = bool(cam_cmd.get("guest"))
                armed_info = self.arm_server_clip(camera=cam, guest=is_guest)
                if not phone_session_live:
                    speech = (
                        f"Guest recording {short_cam}".strip()
                        if is_guest
                        else (f"Recording {short_cam}" if short_cam else "Recording")
                    )
                    return {
                        "handled": True,
                        "ok": True,
                        "target": "camera",
                        "action": "arm",
                        "speech": speech,
                        "ackText": f"🎥 {speech}…",
                        "armedClip": armed_info,
                    }
                # Active phone workout session connected: let SSE deliver to session.ts while keeping server_armed_clip latched
                return None
            if act in ("stop", "clip_last") or (
                act == "commit_done" and self.active_server_armed_clip() is not None
            ):
                if not phone_session_live:
                    clips = self.cut_server_armed_or_recent_clip(
                        mode="clip_last" if act == "clip_last" else "armed",
                        camera=cam,
                    )
                    ok = bool(clips)
                    speech = "Clip saved" if ok else "Could not save clip"
                    return {
                        "handled": True,
                        "ok": ok,
                        "target": "camera",
                        "action": act,
                        "speech": speech,
                        "ackText": "✓ Clip saved on OptiPlex" if ok else "✗ No matching video segments",
                        "clips": clips,
                    }
        return None

    def play_house_audio_test(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute an explicit House Audio / Mode Test Bench cue with direct S330 fallback support."""
        def _play_s330(pcm: np.ndarray, sr: int, kind: str) -> bool:
            return self.voice._play_pcm_blocking(
                pcm, sr, lead_ms=160, tail_ms=120, target_peak=0.84, kind=kind, allow_router=False
            )

        return self.house_audio.test_play(
            kind=str(params.get("kind") or "chime"),
            mode=params.get("mode"),
            usage=params.get("usage"),
            gain=params.get("gain"),
            target=str(params.get("target") or "auto"),
            text=str(params.get("text") or "225 for 8, got it"),
            sfx_file=str(params.get("sfxFile") or ""),
            with_denon_duck=bool(params.get("withDenonDuck", False)),
            synthesize_tts_fn=self.voice.synthesize_tts_pcm,
            play_s330_fn=_play_s330,
        )


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Console · OptiPlex DVR &amp; Gym Command</title>
<style>
  :root{--bg:#090c11;--side:#0d1219;--card:#131a24;--inset:#0b1017;--line:#202d3e;--fg:#eef3f9;--mut:#8b9cb3;--blue:#2ea0ff;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;overflow-x:hidden}
  [hidden]{display:none!important}
  .app-shell{display:flex;min-height:100vh}
  .sidebar{width:232px;flex-shrink:0;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;padding:16px 12px;gap:14px;z-index:40}
  .brand{display:flex;align-items:center;gap:10px;padding:4px 8px 10px;border-bottom:1px solid var(--line)}
  .brand-mark{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,#1d4ed8,#0ea5e9);display:flex;align-items:center;justify-content:center;font-weight:950;font-size:15px;color:#fff;letter-spacing:-.02em;box-shadow:0 4px 14px rgba(14,165,233,.28)}
  .brand-title{font-weight:900;font-size:15px;letter-spacing:.04em;line-height:1.15}
  .brand-sub{font-size:11px;color:var(--mut);font-weight:600}
  .nav-list{display:flex;flex-direction:column;gap:4px;flex:1}
  .nav-btn{width:100%;display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:10px;border:1px solid transparent;background:transparent;color:var(--mut);font:700 13px/1.25 inherit;cursor:pointer;text-align:left;transition:background-color .14s ease,color .14s ease,border-color .14s ease}
  .nav-btn svg{width:16px;height:16px;flex-shrink:0;opacity:.82}
  .nav-btn:hover{background:#151e2a;color:var(--fg)}
  .nav-btn.on{background:rgba(46,160,255,.14);border-color:rgba(46,160,255,.36);color:#eaf4ff}
  .nav-btn.on svg{opacity:1;color:var(--blue)}
  .nav-end{margin-left:auto;display:inline-flex;align-items:center;gap:5px}
  .dot{width:8px;height:8px;border-radius:50%;background:#334155}
  .dot.ok{background:var(--green);box-shadow:0 0 8px rgba(34,197,94,.65)}
  .dot.live{background:var(--blue);box-shadow:0 0 8px rgba(46,160,255,.65)}
  .dot.warn{background:var(--amber);box-shadow:0 0 8px rgba(245,158,11,.7)}
  .dot.err{background:var(--red)}
  .nav-mini{font-size:10px;font-weight:800;padding:1px 6px;border-radius:999px;background:#1a2433;color:var(--mut)}
  .side-foot{border-top:1px solid var(--line);padding-top:10px;display:flex;flex-direction:column;gap:6px;font-size:12px}
  .side-link{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:8px;background:#121923;border:1px solid var(--line);color:#9cc7f5;font-weight:700}
  .side-link:hover{border-color:var(--blue);color:#fff}
  .app-main{flex:1;min-width:0;display:flex;flex-direction:column}
  .topbar{position:sticky;top:0;z-index:35;background:rgba(9,12,17,.90);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 22px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .topbar-title{font-size:17px;font-weight:900;letter-spacing:.01em;margin:0}
  .topbar-meta{font-size:12px;color:var(--mut);margin-left:4px;font-variant-numeric:tabular-nums}
  .topbar-actions{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .page-wrap{width:100%;max-width:1360px;margin:0 auto;padding:18px 22px 36px}
  .page{display:none}
  .page.active{display:block}
  .page-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .page-head h1{margin:0;font-size:20px;font-weight:900;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .page-sub{font-size:12px;color:var(--mut);margin-top:2px}
  .eyebrow{font-size:10px;font-weight:900;letter-spacing:.09em;text-transform:uppercase;color:#7cc4ff;margin-bottom:4px}
  .kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
  .kpi-card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:13px 14px;cursor:pointer;transition:border-color .14s ease,transform .12s ease}
  .kpi-card:hover{border-color:rgba(46,160,255,.55);transform:translateY(-1px)}
  .kpi-lbl{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);display:flex;align-items:center;justify-content:space-between;gap:6px}
  .kpi-val{font-size:18px;font-weight:900;margin:5px 0 3px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .kpi-sub{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ov-split{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(260px,1fr);gap:14px;margin-top:14px}
  .deck-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:8px}
  .deck-group{background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:10px 12px}
  .deck-group .ctrl-row{margin-top:6px}
  .dp-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-bottom:12px}
  .dp-group{background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:10px 12px}
  .dp-group .ctrl-row{margin-top:6px}
  .pill{font-size:11px;padding:3px 10px;border-radius:999px;background:#1d2836;color:var(--mut);font-weight:800;letter-spacing:.04em}
  .pill.ok{background:rgba(34,197,94,.18);color:#86efac}
  .pill.live{background:rgba(30,155,255,.18);color:#7cc4ff}
  .pill.warn{background:rgba(245,158,11,.20);color:#fcd34d}
  .pill.err{background:rgba(239,68,68,.20);color:#fca5a5}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;margin-top:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
  .card + .card{margin-top:14px}
  .card h2{margin:0 0 10px;font-size:16px;font-weight:900;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
  .ts-head-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .ts-seg-ctrl{display:inline-flex;align-items:center;gap:2px;background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:3px}
  .ts-seg-btn{border:0;background:transparent;color:var(--mut);border-radius:7px;padding:4px 10px;font:800 11.5px/1.2 inherit;cursor:pointer;white-space:nowrap;transition:background-color .12s ease,color .12s ease}
  .ts-seg-btn:hover{color:var(--fg)}
  .ts-seg-btn.on{background:rgba(46,160,255,.22);color:#eaf4ff;box-shadow:inset 0 0 0 1px rgba(46,160,255,.45)}
  .ts-strip{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:5px 0 8px}
  .ts-pill{border:1px solid var(--line);background:var(--inset);color:var(--mut);border-radius:9px;padding:5px 10px;font:700 12px/1.25 inherit;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:border-color .12s ease,background-color .12s ease,color .12s ease}
  .ts-pill:hover{border-color:#3a5270;color:var(--fg)}
  .ts-pill.on{background:rgba(46,160,255,.18);border-color:var(--blue);color:#fff}
  .ts-pill-tag{font-size:10px;font-weight:900;padding:1px 5px;border-radius:5px;letter-spacing:.02em}
  .ts-pill-tag.w{background:rgba(34,197,94,.18);color:#86efac}
  .ts-pill-tag.m{background:rgba(168,85,247,.20);color:#d8b4fe}
  .ts-workspace{display:grid;grid-template-columns:minmax(0,1fr) 338px;gap:14px;align-items:start;margin-top:8px}
  .ts-player-col{min-width:0;display:flex;flex-direction:column;gap:8px}
  .ts-stage{position:relative;width:100%;aspect-ratio:16/9;max-height:clamp(280px,39vh,395px);background:#04070b;border-radius:12px;border:1px solid var(--line);overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:pointer;user-select:none}
  .ts-stage:fullscreen{max-height:none;border-radius:0;border:0}
  .ts-stage img,.ts-stage video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block;background:#000}
  .ts-stage img{z-index:1}
  .ts-stage video{z-index:2}
  .ts-hud{position:absolute;left:10px;top:10px;z-index:5;background:rgba(7,10,15,.84);border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:4px 10px;font:700 12px/1.3 ui-monospace,Consolas,monospace;color:#e2e8f0;font-variant-numeric:tabular-nums;pointer-events:none}
  .ts-stage-tools{position:absolute;right:10px;top:10px;z-index:6;display:flex;align-items:center;gap:6px}
  .ts-stage-tools .btn{background:rgba(9,13,19,.86)}
  .ts-buffering{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:7;background:rgba(7,10,15,.86);border:1px solid rgba(46,160,255,.45);border-radius:11px;padding:8px 14px;display:inline-flex;align-items:center;gap:9px;font:700 12.5px/1.2 inherit;color:#eaf4ff;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.6)}
  .ts-spin{width:15px;height:15px;border:2px solid rgba(124,196,255,.25);border-top-color:#38bdf8;border-radius:50%;animation:tsSpin .75s linear infinite}
  @keyframes tsSpin{to{transform:rotate(360deg)}}
  .ts-gap-overlay{position:absolute;inset:0;z-index:6;background:radial-gradient(circle at center,rgba(15,23,42,.92),rgba(4,7,11,.96));display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:20px;text-align:center}
  .ts-gap-title{font:900 16px/1.25 inherit;color:#f8fafc}
  .ts-gap-sub{font:600 12.5px/1.4 ui-monospace,Consolas,monospace;color:var(--mut)}
  .ts-skip-toast{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);z-index:7;background:rgba(15,23,42,.90);border:1px solid rgba(168,85,247,.55);color:#f3e8ff;padding:5px 12px;border-radius:999px;font:800 11.5px/1.2 inherit;pointer-events:none;box-shadow:0 6px 18px rgba(0,0,0,.5)}
  .ts-rail-wrap{position:relative;background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:8px 10px 7px}
  .ts-rail-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:7px}
  .ts-run-strip{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
  .ts-run-btn{border:1px solid #25364c;background:#121b27;color:#cbd5e1;border-radius:7px;padding:2px 7px;font:700 11px/1.25 ui-monospace,Consolas,monospace;cursor:pointer}
  .ts-run-btn:hover{border-color:var(--blue);color:#fff}
  .ts-run-btn.on{background:rgba(46,160,255,.20);border-color:var(--blue);color:#eaf4ff}
  #tsTimelineCanvas{width:100%;height:86px;display:block;cursor:crosshair;touch-action:none;border-radius:6px}
  .ts-hover-pop{position:absolute;bottom:calc(100% + 8px);transform:translateX(-50%);width:204px;background:#0a1019;border:1px solid var(--blue);border-radius:10px;padding:5px;box-shadow:0 10px 28px rgba(0,0,0,.78);pointer-events:none;z-index:25}
  .ts-hover-pop img{width:192px;height:108px;object-fit:cover;border-radius:6px;display:block;background:#04070b}
  .ts-hover-nogap{width:192px;height:66px;border-radius:6px;background:#0f172a;display:flex;align-items:center;justify-content:center;color:var(--mut);font:800 11px/1.2 inherit;border:1px dashed #334155}
  .ts-hover-meta{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-top:4px;font:700 11px/1.2 ui-monospace,Consolas,monospace;color:#f8fafc;padding:0 2px}
  .ts-rail-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:6px;font:600 11px/1.3 ui-monospace,Consolas,monospace;color:var(--mut);flex-wrap:wrap}
  .ts-legend{display:inline-flex;gap:11px;align-items:center;flex-wrap:wrap;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .ts-sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:middle}
  .ts-transport{display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:7px 9px}
  .ts-transport select{width:auto;padding:4px 8px;font-size:12px;font-weight:800}
  .ts-play-main{min-width:96px;font-size:12.5px}
  .ts-clip-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:11px;padding:7px 9px}
  .ts-sidebar{background:var(--inset);border:1px solid var(--line);border-radius:12px;padding:10px;display:flex;flex-direction:column;gap:8px;max-height:760px;position:sticky;top:64px}
  .ts-side-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .ts-side-title{font:900 13.5px/1.2 inherit;color:#f1f5f9;display:flex;align-items:center;gap:6px}
  .ts-filter-box{background:#0e1520;border:1px solid var(--line);border-radius:9px;padding:8px;display:flex;flex-direction:column;gap:7px}
  .ts-filter-row{display:flex;align-items:center;justify-content:space-between;gap:6px;font-size:11.5px;color:var(--mut)}
  .ts-filter-row input[type=range]{flex:1;accent-color:var(--blue)}
  .ts-bursts{display:flex;flex-direction:column;gap:6px;overflow-y:auto;padding-right:2px;flex:1;min-height:180px;max-height:560px}
  .ts-ev-card{display:grid;grid-template-columns:96px minmax(0,1fr);gap:8px;align-items:center;background:#101824;border:1px solid #223246;border-radius:9px;padding:6px;cursor:pointer;text-align:left;transition:border-color .12s ease,background-color .12s ease}
  .ts-ev-card:hover{border-color:var(--blue);background:#152030}
  .ts-ev-card.on{border-color:var(--green);background:rgba(34,197,94,.12)}
  .ts-ev-thumb{width:96px;height:54px;border-radius:6px;background:#05080d;object-fit:cover;display:block}
  .ts-ev-meta{min-width:0;display:flex;flex-direction:column;gap:3px}
  .ts-ev-top{display:flex;align-items:center;justify-content:space-between;gap:4px;font:800 12px/1.2 ui-monospace,Consolas,monospace;color:#f1f5f9}
  .ts-ev-sub{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:10.5px;color:var(--mut)}
  .ts-ev-bar{height:4px;border-radius:999px;background:#1b2838;overflow:hidden}
  .ts-ev-bar>span{display:block;height:100%;border-radius:999px}
  .ts-ev-foot{display:flex;align-items:center;justify-content:space-between;gap:4px}
  .ts-ev-clip{border:1px solid #2d4057;background:#162231;color:#cbd5e1;border-radius:5px;padding:1px 6px;font:800 10px/1.3 inherit;cursor:pointer}
  .ts-ev-clip:hover{border-color:var(--amber);color:#fde68a}
  @media (max-width:1100px){
    .ts-workspace{grid-template-columns:1fr}
    .ts-sidebar{position:static;max-height:none}
    .ts-bursts{max-height:380px}
  }
  .cam-head-l{display:flex;align-items:center;gap:8px;min-width:0;flex:1;flex-wrap:wrap}
  .cam-head-r{display:flex;align-items:center;gap:6px;flex-shrink:0}
  .cam-name-inp{width:170px;max-width:100%;padding:4px 8px!important;font-weight:800!important;font-size:14px!important;border-radius:8px!important}
  .stage{position:relative;width:100%;aspect-ratio:16/9;background:#06090d;border-radius:10px;border:1px solid var(--line);overflow:hidden;user-select:none;touch-action:none}
  .stage.rot90,.stage.rot270{width:auto;height:480px;max-width:100%;aspect-ratio:9/16;margin:0 auto}
  .stage img.stream,.stage canvas.stream{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none}
  .level-grid{position:absolute;inset:0;pointer-events:none;z-index:4;opacity:0;transition:opacity .12s ease;background-image:linear-gradient(to right,rgba(124,196,255,.32) 1px,transparent 1px),linear-gradient(to bottom,rgba(124,196,255,.32) 1px,transparent 1px);background-size:12.5% 12.5%;}
  .stage.leveling .level-grid{opacity:1}
  .tilt-num{width:58px!important;padding:4px 6px!important;font-size:12px!important;text-align:right;font-variant-numeric:tabular-nums}
  .axis-sel{width:auto!important;padding:4px 6px!important;font-size:12px!important;font-weight:700;border-radius:8px!important}
  .crop-layer{position:absolute;inset:0;pointer-events:auto}
  .scrim{position:absolute;background:rgba(4,7,11,.62);pointer-events:none;z-index:1}
  .crop-box{position:absolute;z-index:2;border:2px solid var(--blue);box-shadow:0 0 0 1px rgba(0,0,0,.6);cursor:move;touch-action:none}
  .crop-box::after{content:'';position:absolute;inset:0;border:1px dashed rgba(255,255,255,.28);pointer-events:none}
  .box-tag{position:absolute;left:4px;top:4px;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:900;letter-spacing:.06em;pointer-events:none;background:rgba(6,9,13,.82);color:var(--blue)}
  .crop-box.passive{border-style:dashed;opacity:.52;pointer-events:none;cursor:default}
  .crop-box.passive .handle{display:none}
  .excl-wrap{position:absolute;inset:0;pointer-events:none;z-index:3}
  .excl-wrap.passive{opacity:.6}
  .excl-wrap.passive .quad-poly{stroke-dasharray:6 4;pointer-events:none;cursor:default}
  .excl-wrap.passive .quad-dot{display:none}
  .excl-svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none}
  .quad-poly{fill:rgba(239,68,68,.24);stroke:#ef4444;stroke-width:2;pointer-events:all;cursor:move;transition:fill .12s,stroke .12s}
  .quad-poly.active{fill:rgba(239,68,68,.30);stroke:#fca5a5;stroke-width:2.4}
  .quad-poly.crossed{stroke:var(--amber);fill:rgba(245,158,11,.22)}
  .quad-tag{position:absolute;transform:translate(-50%,-50%);padding:1px 6px;border-radius:4px;font-size:10px;font-weight:900;letter-spacing:.06em;pointer-events:none;background:rgba(6,9,13,.84);color:#fecaca;border:1px solid rgba(239,68,68,.45);white-space:nowrap}
  .quad-dot{position:absolute;width:28px;height:28px;transform:translate(-50%,-50%);cursor:grab;pointer-events:auto;touch-action:none;z-index:6;display:flex;align-items:center;justify-content:center;background:transparent}
  .mm-canvas .quad-dot{width:34px;height:34px}
  .quad-ring{position:relative;width:16px;height:16px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1.5px rgba(239,68,68,.95),0 1px 6px rgba(0,0,0,.85);background:rgba(239,68,68,.10);pointer-events:none;transition:transform .08s ease,border-color .1s ease,box-shadow .1s ease}
  .mm-canvas .quad-ring{width:20px;height:20px}
  .quad-ring::before{content:'';position:absolute;left:50%;top:1px;bottom:1px;width:1px;transform:translateX(-50%);background:rgba(255,255,255,.55)}
  .quad-ring::after{content:'';position:absolute;top:50%;left:1px;right:1px;height:1px;transform:translateY(-50%);background:rgba(255,255,255,.55)}
  .quad-dot:hover .quad-ring,.quad-dot.dragging .quad-ring{border-color:#fde047;box-shadow:0 0 0 2px rgba(245,158,11,.95),0 0 12px rgba(250,204,21,.65);transform:scale(1.14)}
  .quad-dot.dragging{cursor:grabbing;z-index:8}
  .quad-num{position:absolute;left:100%;top:-2px;margin-left:2px;font-size:9px;font-weight:900;color:#fff;background:rgba(6,9,13,.88);padding:0 3px;border-radius:3px;border:1px solid rgba(255,255,255,.22);pointer-events:none;line-height:1.2}
  .handle{position:absolute;width:22px;height:22px;border-radius:50%;background:var(--blue);border:2px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.7);touch-action:none}
  .handle.nw{left:-11px;top:-11px;cursor:nwse-resize}
  .handle.ne{right:-11px;top:-11px;cursor:nesw-resize}
  .handle.sw{left:-11px;bottom:-11px;cursor:nesw-resize}
  .handle.se{right:-11px;bottom:-11px;cursor:nwse-resize}
  .crop-info{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin-top:8px;font-variant-numeric:tabular-nums}
  .ctrl-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
  .ctrl-row input[type=range]{flex:1;min-width:120px;accent-color:var(--blue)}
  .mbar-wrap{position:relative;height:10px;border-radius:999px;background:#0b1017;border:1px solid var(--line);overflow:hidden;margin-top:6px}
  .mbar-fill{position:absolute;left:0;top:0;bottom:0;background:var(--blue);width:0%;transition:width .25s}
  .mbar-fill.hot{background:var(--green)}
  .mbar-th{position:absolute;top:0;bottom:0;width:2px;background:var(--amber)}
  label{display:block;font-size:12px;color:var(--mut);margin:8px 0 4px}
  input[type=text],input[type=password],input[type=number],select,textarea{width:100%;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:#0c1118;color:var(--fg);font:inherit}
  .show-grid{display:flex;flex-direction:column;gap:8px;margin-top:8px}
  .show-item{background:#0c1118;border:1px solid var(--line);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}
  .show-acc{border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .show-acc[open]{border-color:rgba(46,160,255,.45)}
  .card.show-acc,.dev-drop-box.show-acc,.show-item.show-acc{padding:0;gap:0}
  .dev-drop-box.show-acc{margin-bottom:14px}
  .show-acc>summary.show-acc-sum{list-style:none;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;cursor:pointer;user-select:none;flex-wrap:wrap}
  .show-acc>summary.show-acc-sum::-webkit-details-marker{display:none}
  .show-acc>summary.show-acc-sum:hover{background:rgba(255,255,255,.03)}
  .show-acc-left{display:flex;align-items:center;gap:8px;min-width:0;flex:1;flex-wrap:wrap}
  .show-chev{display:inline-flex;align-items:center;justify-content:center;width:16px;color:var(--mut);font-size:12px;transition:transform .14s ease;flex-shrink:0}
  .show-acc[open]>summary .show-chev{transform:rotate(90deg);color:var(--blue)}
  .show-acc-title{font-weight:900;font-size:14px;line-height:1.25}
  .show-acc-sub{font-size:11px;color:var(--mut);line-height:1.35}
  .show-acc-acts{display:inline-flex;align-items:center;gap:6px;flex-shrink:0;flex-wrap:wrap}
  .show-acc-body{padding:10px 12px 12px;border-top:1px solid rgba(255,255,255,.07);display:flex;flex-direction:column;gap:10px}
  .sh-sub{background:#090d14;border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:0;overflow:hidden}
  .sh-sub>summary{list-style:none;padding:7px 10px;cursor:pointer;font-size:12px;font-weight:800;color:#9cc7f5;user-select:none;display:flex;align-items:center;gap:6px}
  .sh-sub>summary::-webkit-details-marker{display:none}
  .sh-sub>summary:hover{background:rgba(255,255,255,.03)}
  .sh-sub[open]>summary{border-bottom:1px solid rgba(255,255,255,.06)}
  .sh-sub-body{padding:8px 10px 10px;display:flex;flex-direction:column;gap:8px}
  .show-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap}
  .show-trigs{font-size:11px;color:var(--mut);line-height:1.5}
  .show-audio{width:100%;height:34px;border-radius:8px;background:#131a22}
  .dev-drop-box{background:#0b1018;border:1px solid #284160;border-radius:14px;padding:14px;margin-bottom:14px}
  .dev-stage{position:relative;border-radius:12px;border:2px solid #233040;background:#0a0e14;min-height:240px;padding:12px;display:flex;flex-direction:column;justify-content:space-between;gap:10px;overflow:hidden;transition:background-color .12s ease,box-shadow .12s ease,border-color .12s ease}
  .dev-stage.lights-off{background:#020305;border-color:#1e293b}
  .dev-stage.lights-on{background:#231f16;border-color:#facc15;box-shadow:inset 0 0 60px rgba(250,204,21,.22)}
  .dev-stage.flashing{background:#ffffff!important;border-color:#ffffff!important;box-shadow:0 0 60px rgba(255,255,255,.95)!important;color:#000!important}
  .dev-stage.fs{position:fixed;inset:12px;z-index:10060;margin:0;min-height:0}
  .fx-hud{display:flex;flex-wrap:wrap;gap:8px;align-items:center;z-index:2}
  .fx-tag{padding:5px 11px;border-radius:999px;font-size:12px;font-weight:900;letter-spacing:.05em;background:#131a22;color:#94a3b8;border:1px solid #233040}
  .fx-tag.fog{background:rgba(34,211,238,.18);color:#67e8f9;border-color:#06b6d4;box-shadow:0 0 18px rgba(34,211,238,.45)}
  .fx-tag.flash{background:#fef08a;color:#1e1b4b;border-color:#fff;box-shadow:0 0 22px rgba(255,255,255,.9)}
  .fx-tag.dark{background:#020617;color:#94a3b8;border-color:#334155}
  .fx-tag.lit{background:rgba(250,204,21,.22);color:#fde047;border-color:#facc15}
  .dev-yt-wrap{position:relative;width:100%;max-width:560px;aspect-ratio:16/9;background:#000;border-radius:8px;overflow:hidden;border:1px solid #233040;margin:0 auto;z-index:2}
  .dev-yt-wrap iframe{width:100%;height:100%;border:0}
  .dev-caption{text-align:center;font-weight:900;font-size:15px;color:#f8fafc;background:rgba(0,0,0,.62);border:1px solid rgba(255,255,255,.15);padding:8px 12px;border-radius:8px;z-index:2}
  .dev-log{font:11px/1.4 ui-monospace,Consolas,monospace;color:#7cc4ff;max-height:74px;overflow:auto;background:rgba(0,0,0,.45);padding:6px 8px;border-radius:6px;z-index:2}
  .tag-bar{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:6px}
  .tag-chip{background:#16202c;border:1px solid #2b3d54;color:#9cc7f5;border-radius:7px;padding:4px 9px;font:700 11px/1.3 ui-monospace,Consolas,monospace;cursor:pointer}
  .tag-chip:hover{background:#1d2b3c;border-color:#7cc4ff;color:#fff}
  .cue-list{display:flex;flex-direction:column;gap:8px;margin-top:6px}
  .cue-row{background:#0e151f;border:1px solid #213044;border-radius:9px;padding:8px;display:flex;flex-direction:column;gap:6px}
  .cue-row-top{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .cue-row-top select,.cue-row-top input[type=text],.cue-row-top input[type=number]{width:auto;padding:5px 8px;font-size:12px}
  .btn{background:var(--blue);color:#00131f;border:0;border-radius:9px;padding:7px 12px;font-weight:800;font-size:12px;cursor:pointer;transition:transform .08s ease,background-color .15s ease,border-color .15s ease,box-shadow .15s ease,filter .15s ease,color .15s ease;user-select:none}
  .btn:hover:not(:disabled){filter:brightness(1.12);box-shadow:0 2px 10px rgba(0,0,0,.35)}
  .btn:active:not(:disabled){transform:translateY(2px) scale(.96);filter:brightness(.92);box-shadow:inset 0 2px 5px rgba(0,0,0,.45)}
  .btn:disabled{opacity:.42;cursor:not-allowed}
  .btn.sm{padding:5px 10px}
  .btn.sec{background:#1d2836;color:var(--fg);border:1px solid var(--line)}
  .btn.sec.on{background:rgba(30,155,255,.2);border-color:var(--blue);color:#7cc4ff}
  .btn.ok{background:rgba(34,197,94,.22);border:1px solid rgba(34,197,94,.5);color:#86efac}
  .btn.red{background:rgba(239,68,68,.18);border:1px solid rgba(239,68,68,.55);color:#fecaca}
  .btn.red:hover:not(:disabled){background:rgba(239,68,68,.28)}
  .btn.saving{opacity:.82;cursor:wait}
  .btn.saved{background:#22c55e!important;border-color:#4ade80!important;color:#031a0c!important;box-shadow:0 0 0 2px rgba(34,197,94,.4),0 2px 12px rgba(34,197,94,.35)}
  .btn.save-err{background:#ef4444!important;border-color:#f87171!important;color:#fff!important}
  .btn.dirty{box-shadow:0 0 0 2px rgba(245,158,11,.55)}
  .mask-modal{position:fixed;inset:0;z-index:9999;background:rgba(4,7,11,.96);display:flex;flex-direction:column;padding:10px 14px;gap:8px;user-select:none}
  .mm-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;justify-content:space-between}
  .mm-title{font-weight:900;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .mm-tabs{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .mm-tab{display:inline-flex;align-items:center;gap:6px;background:#1d2836;border:1px solid var(--line);border-radius:9px;padding:3px 6px 3px 10px;font-size:12px;font-weight:800;color:var(--fg);cursor:pointer}
  .mm-tab.on{background:rgba(239,68,68,.22);border-color:var(--red);color:#fecaca}
  .mm-tab-del{border:0;background:rgba(239,68,68,.3);color:#fecaca;border-radius:5px;width:18px;height:18px;font:900 12px/1 system-ui,sans-serif;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0}
  .mm-tab-del:hover{background:var(--red);color:#fff}
  .mm-zoombar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 10px}
  .mm-zoombar input[type=range]{width:110px;accent-color:var(--blue)}
  .mm-view-sel{width:auto!important;min-width:170px;max-width:260px;padding:4px 8px!important;font-size:13px!important;font-weight:800;border-radius:8px!important}
  .mm-bar-grp{display:inline-flex;align-items:center;gap:5px;flex-wrap:wrap}
  .mm-sep{width:1px;height:18px;background:var(--line);margin:0 2px}
  .mm-canvas.leveling .level-grid{opacity:1}
  .mm-canvas .handle{width:26px;height:26px}
  .mm-canvas .handle.nw{left:-13px;top:-13px}
  .mm-canvas .handle.ne{right:-13px;top:-13px}
  .mm-canvas .handle.sw{left:-13px;bottom:-13px}
  .mm-canvas .handle.se{right:-13px;bottom:-13px}
  .mm-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12px;color:var(--mut);padding:0 4px;font-variant-numeric:tabular-nums;flex-wrap:wrap}
  .mm-viewport{flex:1;min-height:0;position:relative;overflow:hidden;background:#05080d;border:1px solid var(--line);border-radius:12px;touch-action:none;cursor:grab}
  .mm-viewport.panning{cursor:grabbing}
  .mm-canvas{position:absolute;transform-origin:0 0;touch-action:none}
  .mm-img{width:100%;height:100%;display:block;pointer-events:none}
  .mm-mbar{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
  .mm-mbar-track{position:relative;width:72px;height:8px;border-radius:999px;background:#0b1017;border:1px solid var(--line);overflow:hidden}
  .mm-mbar-fill{position:absolute;left:0;top:0;bottom:0;width:0%;background:var(--blue);transition:width .2s}
  .mm-mbar-fill.hot{background:var(--green)}
  .realign-modal{position:fixed;inset:0;z-index:10000;background:#05080d;display:flex;flex-direction:column;padding:10px 12px;gap:8px;user-select:none;overflow-y:auto}
  .realign-modal[hidden]{display:none!important}
  .ra-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px}
  .ra-top-l,.ra-top-r{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .ra-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;font-size:12px;font-weight:900;letter-spacing:.03em;border:1px solid var(--line);background:#16202c;color:#cbd5e1;font-variant-numeric:tabular-nums}
  .ra-pill.locked{background:rgba(34,197,94,.2);border-color:#22c55e;color:#86efac}
  .ra-pill.close{background:rgba(245,158,11,.2);border-color:#f59e0b;color:#fde68a}
  .ra-pill.shifted{background:rgba(239,68,68,.22);border-color:#ef4444;color:#fecaca}
  .ra-pill.hold{background:rgba(100,116,139,.22);border-color:#64748b;color:#e2e8f0}
  .ra-stage-wrap{flex:1 1 auto;min-height:200px;display:flex;align-items:center;justify-content:center;min-width:0}
  .ra-stage{position:relative;aspect-ratio:16/9;width:100%;max-width:min(100%,calc((100dvh - 240px) * 16 / 9));background:#000;border-radius:12px;overflow:hidden;border:2px solid var(--line);box-shadow:0 8px 32px rgba(0,0,0,.6);transition:border-color .18s,box-shadow .18s}
  .ra-stage.locked{border-color:#22c55e;box-shadow:0 0 0 2px rgba(34,197,94,.45),0 0 28px rgba(34,197,94,.32)}
  .ra-stage.close{border-color:#f59e0b;box-shadow:0 0 0 2px rgba(245,158,11,.35)}
  .ra-stage.shifted{border-color:#ef4444;box-shadow:0 0 0 2px rgba(239,68,68,.35)}
  .ra-media{position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none}
  .ra-overlay{transition:opacity .12s ease}
  .ra-overlay.blink-off{opacity:0!important}
  .ra-crops{z-index:3}
  .ra-edge{position:absolute;z-index:5;pointer-events:none;padding:5px 11px;border-radius:999px;font-weight:900;font-size:13px;letter-spacing:.03em;background:rgba(12,18,28,.86);border:1.5px solid #f59e0b;color:#fef3c7;box-shadow:0 4px 16px rgba(0,0,0,.55);opacity:0;transform:scale(.94);transition:opacity .16s ease,transform .16s ease;white-space:nowrap}
  .ra-edge.on{opacity:1;transform:scale(1)}
  .ra-edge.hot{border-color:#ef4444;background:rgba(127,29,29,.88);color:#fee2e2}
  .ra-edge.up{top:10px;left:50%;transform:translateX(-50%) scale(.94)}
  .ra-edge.up.on{transform:translateX(-50%) scale(1)}
  .ra-edge.down{bottom:10px;left:50%;transform:translateX(-50%) scale(.94)}
  .ra-edge.down.on{transform:translateX(-50%) scale(1)}
  .ra-edge.left{left:10px;top:50%;transform:translateY(-50%) scale(.94)}
  .ra-edge.left.on{transform:translateY(-50%) scale(1)}
  .ra-edge.right{right:10px;top:50%;transform:translateY(-50%) scale(.94)}
  .ra-edge.right.on{transform:translateY(-50%) scale(1)}
  .ra-edge.roll{top:10px;right:10px}
  .ra-banner-badge{position:absolute;z-index:6;left:50%;top:12px;transform:translateX(-50%);pointer-events:none;padding:6px 16px;border-radius:999px;font-weight:900;font-size:14px;letter-spacing:.04em;box-shadow:0 6px 20px rgba(0,0,0,.6);white-space:nowrap}
  .ra-banner-badge.lock{background:rgba(21,128,61,.94);border:1.5px solid #4ade80;color:#f0fdf4}
  .ra-banner-badge.hold{top:50%;transform:translate(-50%,-50%);background:rgba(15,23,42,.92);border:1.5px solid #64748b;color:#f1f5f9;font-size:15px}
  .ra-guide-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 12px;display:flex;flex-direction:column;gap:5px}
  .ra-guide-chips{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px;min-height:32px}
  .ra-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 13px;border-radius:10px;font-weight:900;font-size:16px;letter-spacing:.02em;font-variant-numeric:tabular-nums}
  .ra-chip.move{background:rgba(239,68,68,.2);border:1.5px solid rgba(239,68,68,.75);color:#fecaca}
  .ra-chip.nudge{background:rgba(245,158,11,.2);border:1.5px solid rgba(245,158,11,.75);color:#fde68a}
  .ra-chip.ok{background:rgba(34,197,94,.22);border:1.5px solid #22c55e;color:#bbf7d0}
  .ra-chip.hold{background:rgba(100,116,139,.2);border:1.5px solid #64748b;color:#e2e8f0}
  .ra-metrics{text-align:center;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
  .ra-ctrls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:7px 10px}
  .ra-ctrl-grp{display:inline-flex;align-items:center;gap:5px;flex-wrap:wrap}
  .ra-ctrls input[type=range]{width:92px;accent-color:var(--blue)}
  .ra-swatch{width:22px;height:22px;border-radius:999px;border:2px solid transparent;cursor:pointer;padding:0}
  .ra-swatch.on{border-color:#fff;box-shadow:0 0 0 2px rgba(255,255,255,.35)}
  .ws-pills{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:12px}
  .ws-pill{background:#0e151f;border:1px solid var(--line);border-radius:10px;padding:9px 11px;text-align:left;cursor:pointer;color:var(--fg);display:flex;flex-direction:column;gap:3px;transition:border-color .15s,background-color .15s}
  .ws-pill:hover{border-color:#3b82f6;background:#131d2b}
  .ws-pill.on{border-color:var(--blue);background:rgba(30,155,255,.14);box-shadow:0 0 0 1px rgba(30,155,255,.35)}
  .ws-pill-top{display:flex;justify-content:space-between;align-items:center;gap:6px;font-weight:800;font-size:13px}
  .ws-pill-sub{font-size:11px;color:var(--mut);display:flex;justify-content:space-between;gap:6px}
  .ws-prompt{background:#0b111a;border:1px solid #26364d;border-radius:10px;padding:12px 14px;margin:10px 0}
  .ws-prompt.rec{border-color:#22c55e;background:rgba(22,163,74,.10);box-shadow:0 0 0 1px rgba(34,197,94,.3)}
  .ws-prog-wrap{width:100%;height:8px;border-radius:999px;background:#0b1017;border:1px solid var(--line);overflow:hidden;margin:6px 0}
  .ws-prog-bar{height:100%;width:0%;background:linear-gradient(90deg,#0ea5e9,#22c55e);transition:width .25s ease}
  .ws-log{font:11px/1.4 ui-monospace,Consolas,monospace;color:#93c5fd;background:#080c12;border:1px solid var(--line);border-radius:8px;padding:8px;max-height:140px;overflow-y:auto;white-space:pre-wrap;margin-top:6px}
  .ws-clips{max-height:200px;overflow-y:auto;border:1px solid var(--line);border-radius:9px;background:#0b1017;margin-top:8px}
  .ws-clip-row{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 10px;border-top:1px solid rgba(255,255,255,.05);font-size:12px}
  .ws-clip-row:first-child{border-top:0}
  @media (max-width:640px){
    .realign-modal{padding:8px;gap:8px;justify-content:flex-start}
    .ra-top{gap:6px}
    .ra-top .mm-view-sel{min-width:118px;max-width:155px;font-size:12.5px!important}
    .ra-stage-wrap{flex:0 0 auto;min-height:0}
    .ra-stage{max-width:100%}
    .ra-chip{font-size:15px;padding:5px 11px}
    .ra-edge{font-size:11.5px;padding:4px 8px}
    .ra-ctrls{gap:6px;padding:7px 8px}
    .ra-ctrls input[type=range]{width:76px}
  }
  #toast{position:fixed;left:50%;bottom:20px;transform:translate(-50%,16px);opacity:0;pointer-events:none;z-index:10050;padding:10px 18px;border-radius:10px;font-weight:800;font-size:13px;background:#1d2836;color:var(--fg);border:1px solid var(--line);box-shadow:0 8px 24px rgba(0,0,0,.55);transition:opacity .18s ease,transform .18s ease}
  #toast.show{opacity:1;transform:translate(-50%,0)}
  #toast.ok{background:#15803d;border-color:#4ade80;color:#f0fdf4}
  #toast.err{background:#b91c1c;border-color:#f87171;color:#fef2f2}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 6px;border-top:1px solid var(--line)}
  a{color:#7cc4ff;text-decoration:none}
  code{background:#0d1218;padding:2px 6px;border-radius:6px;font-size:12px}
  @media (max-width:920px){
    .app-shell{flex-direction:column}
    .sidebar{width:100%;height:auto;position:sticky;top:0;border-right:0;border-bottom:1px solid var(--line);padding:10px 12px;gap:8px;flex-direction:row;align-items:center;overflow-x:auto}
    .brand{border-bottom:0;padding:0 10px 0 0;border-right:1px solid var(--line);flex-shrink:0}
    .brand-sub,.side-foot{display:none}
    .nav-list{flex-direction:row;gap:4px;overflow-x:auto}
    .nav-btn{width:auto;padding:7px 10px;white-space:nowrap}
    .topbar{position:static;padding:10px 14px}
    .page-wrap{padding:14px 14px 32px}
    .ov-split{grid-template-columns:1fr}
    .show-acc>summary.show-acc-sum{padding:9px 10px}
    .tag-chip{padding:6px 9px;font-size:11.5px}
  }
</style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">A</div>
      <div>
        <div class="brand-title">ARMORY</div>
        <div class="brand-sub">Gym Command Console</div>
      </div>
    </div>
    <nav class="nav-list" aria-label="Console sections">
      <button class="nav-btn on" type="button" data-nav="overview">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <span>Overview</span>
        <span class="nav-end"><span id="navDotOverview" class="dot ok"></span></span>
      </button>
      <button class="nav-btn" type="button" data-nav="cameras">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
        <span>Cameras</span>
        <span class="nav-end"><span id="navDotCams" class="dot live"></span></span>
      </button>
      <button class="nav-btn" type="button" data-nav="footage">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        <span>Footage</span>
        <span class="nav-end"><span id="navBadgeRet" class="nav-mini">14d</span></span>
      </button>
      <button class="nav-btn" type="button" data-nav="shows">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
        <span>Show Lab</span>
        <span class="nav-end"><span id="navDotShow" class="dot"></span></span>
      </button>
      <button class="nav-btn" type="button" data-nav="gym">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
        <span>Gym &amp; Displays</span>
        <span class="nav-end"><span id="navDotGym" class="dot"></span></span>
      </button>
      <button class="nav-btn" type="button" data-nav="audio">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4M8 22h8"/></svg>
        <span>Audio &amp; Voice</span>
        <span class="nav-end"><span id="navDotAudio" class="dot"></span></span>
      </button>
    </nav>
    <div class="side-foot">
      <a class="side-link" href="/wallboard" target="_blank"><span>Wallboard View</span><span>↗</span></a>
      <a class="side-link" href="/stage" target="_blank"><span>TV Stage Screen</span><span>↗</span></a>
      <div style="color:var(--mut);font-size:11px;padding:2px 4px">Shortcuts: keys <code>1</code>–<code>6</code></div>
    </div>
  </aside>

  <div class="app-main">
    <header class="topbar">
      <h1 id="pageTitle" class="topbar-title">Overview</h1>
      <span id="svcBadge" class="pill">Checking…</span>
      <span id="subInfo" class="topbar-meta">Loading OptiPlex status…</span>
      <div class="topbar-actions">
        <button class="btn red sm" id="topStopShowBtn" type="button" hidden>⏹ Stop Show</button>
      </div>
    </header>

    <div class="page-wrap">

      <!-- PAGE 1: OVERVIEW -->
      <section class="page active" data-page="overview">
        <div class="page-head">
          <div>
            <h1>Mission Control</h1>
            <div class="page-sub">Live status across OptiPlex DVR, displays, audio, voice mic &amp; entrance shows</div>
          </div>
          <div class="ctrl-row" style="margin-top:0">
            <button class="btn sec sm" type="button" onclick="navigatePage('footage')">⏱ Open 14-Day Scrubber</button>
            <button class="btn sm" type="button" onclick="navigatePage('shows')">⚡ Open Show Lab</button>
          </div>
        </div>

        <div class="kpi-grid">
          <div class="kpi-card" onclick="navigatePage('cameras')">
            <div class="kpi-lbl"><span>Cameras</span><span class="pill live" id="ovKpiCamsPill">LIVE</span></div>
            <div class="kpi-val" id="ovKpiCamsVal">—</div>
            <div class="kpi-sub" id="ovKpiCamsSub">Checking feeds…</div>
          </div>
          <div class="kpi-card" onclick="navigatePage('footage')">
            <div class="kpi-lbl"><span>14-Day Storage</span><span class="pill" id="ovKpiDiskPill">DVR</span></div>
            <div class="kpi-val" id="ovKpiDiskVal">—</div>
            <div class="kpi-sub" id="ovKpiDiskSub">Checking tapes…</div>
          </div>
          <div class="kpi-card" onclick="navigatePage('audio')">
            <div class="kpi-lbl"><span>Room Voice Mic</span><span class="pill" id="ovKpiVoicePill">…</span></div>
            <div class="kpi-val" id="ovKpiVoiceVal">—</div>
            <div class="kpi-sub" id="ovKpiVoiceSub">PowerConf S330</div>
          </div>
          <div class="kpi-card" onclick="navigatePage('audio')">
            <div class="kpi-lbl"><span>House Audio</span><span class="pill" id="ovKpiAudioPill">…</span></div>
            <div class="kpi-val" id="ovKpiAudioVal">—</div>
            <div class="kpi-sub" id="ovKpiAudioSub">Denon + Fire TV Cube</div>
          </div>
          <div class="kpi-card" onclick="navigatePage('gym')">
            <div class="kpi-lbl"><span>Gym Displays</span><span class="pill" id="ovKpiDispPill">…</span></div>
            <div class="kpi-val" id="ovKpiDispVal">—</div>
            <div class="kpi-sub" id="ovKpiDispSub">TV · Wallboard · Spin</div>
          </div>
          <div class="kpi-card" onclick="navigatePage('shows')">
            <div class="kpi-lbl"><span>Show Director</span><span class="pill" id="ovKpiShowPill">IDLE</span></div>
            <div class="kpi-val" id="ovKpiShowVal">Ready</div>
            <div class="kpi-sub" id="ovKpiShowSub">6 shows configured</div>
          </div>
        </div>

        <div class="card">
          <h2>
            <span>Quick Gym Control Deck</span>
            <span id="ovDeckOut" style="font-size:12px;color:var(--mut);font-weight:600">Discrete one-tap controls</span>
          </h2>
          <div class="deck-grid">
            <div class="deck-group">
              <div class="eyebrow">Gym Power Macros</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="open_gym:run">Open Gym</button>
                <button class="btn red sm" type="button" data-dpact="close_gym:run">Close Gym</button>
                <button class="btn sec sm" type="button" data-dpact="all_tvs:on">All TVs ON</button>
                <button class="btn sec sm" type="button" data-dpact="all_tvs:off">All TVs OFF</button>
              </div>
            </div>
            <div class="deck-group">
              <div class="eyebrow">Main TV &amp; Cube</div>
              <div class="ctrl-row">
                <button class="btn sec sm" type="button" data-dpact="tv:hdmi1">TV PC</button>
                <button class="btn sec sm" type="button" data-dpact="tv:hdmi2">TV Fire TV</button>
                <button class="btn ok sm" type="button" data-dpact="cube:on">Cube ON</button>
                <button class="btn sec sm" type="button" data-dpact="cube:off">Cube OFF</button>
                <button class="btn sec sm" type="button" data-dpact="cube:home">Home</button>
              </div>
            </div>
            <div class="deck-group">
              <div class="eyebrow">Receiver &amp; Lights</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="avr:on">Denon ON</button>
                <button class="btn sec sm" type="button" data-dpact="avr:off">Denon OFF</button>
                <button class="btn sec sm" type="button" id="ovLightsOnBtn">Lights ON</button>
                <button class="btn sec sm" type="button" id="ovLightsOffBtn">Lights OFF</button>
              </div>
            </div>
            <div class="deck-group">
              <div class="eyebrow">PR &amp; Show Triggers</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" onclick="fireShow('pr')">🔥 PR</button>
                <button class="btn red sm" type="button" onclick="stopShow()">⏹ Stop Show</button>
              </div>
            </div>
          </div>
        </div>

        <div class="ov-split">
          <div class="card">
            <h2>
              <span>Latest Footage &amp; Saved Clips</span>
              <button class="btn sec sm" type="button" onclick="navigatePage('footage')">Browse All 14 Days →</button>
            </h2>
            <div id="ovFootageSummary" style="font-size:12px;color:var(--mut);margin-bottom:8px">Loading tape index…</div>
            <div id="ovRecentClips" style="font-size:12px;color:var(--mut)">Loading clips…</div>
          </div>
          <div class="card" style="margin-top:0">
            <h2><span>Shortcuts &amp; Tools</span></h2>
            <div class="ctrl-row" style="margin-top:0">
              <button class="btn sec" type="button" onclick="navigatePage('cameras'); $('openEditorTop') && $('openEditorTop').click()">⤢ Full-Screen Crop &amp; Mask</button>
              <button class="btn sec" type="button" onclick="navigatePage('shows'); if ($('devRyderBox')) $('devRyderBox').open = true;">🎬 Ryder Intro Simulator</button>
              <button class="btn sec" type="button" onclick="navigatePage('gym')">📺 Configure Displays / IP</button>
              <button class="btn sec" type="button" onclick="navigatePage('audio')">🎙️ Test S330 Mic / Denon</button>
              <button class="btn sec" type="button" id="ovBackupCfgBtn">💾 Back up OptiPlex Configs</button>
            </div>
          </div>
        </div>
      </section>

      <!-- PAGE 2: CAMERAS -->
      <section class="page" data-page="cameras">
        <div class="page-head">
          <div>
            <h1>Camera Views, Crop &amp; Mirror Masks</h1>
            <div class="page-sub">Drag blue crop corners, <b>4-dot Mirror Masks</b>, or click <b>+ Add View</b> to crop extra stations from a camera.</div>
          </div>
          <div class="ctrl-row" style="margin-top:0">
            <button class="btn sec" id="openEditorTop" type="button">⤢ Full-Screen Editor</button>
            <button class="btn" id="saveAllTop" type="button">Save Camera Settings</button>
          </div>
        </div>

        <div id="camGrid" class="grid"></div>

        <div class="card" style="margin-top:14px">
          <h2>Retention, Buffer &amp; Google Drive Sync</h2>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div><label>Full Tape Retention (days)</label><input id="retDays" type="number" min="1" max="90"></div>
            <div><label>Pre-Roll Buffer (seconds)</label><input id="preRoll" type="number" min="30" max="600"></div>
          </div>
          <label>Google Drive Clip Backup Folder (auto-copied on save)</label>
          <input id="driveDir" type="text" placeholder="J:\\My Drive\\Backups\\ArmoryFit Backup\\Clips">
          <label style="display:flex;align-items:center;gap:8px;margin-top:10px;text-transform:none;font-size:13px;color:var(--fg)">
            <input id="bumpAlertNtfy" type="checkbox" style="width:auto;margin:0">
            <span>Camera Bump Alert (ntfy push if a camera gets bumped)</span>
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-top:8px;text-transform:none;font-size:13px;color:var(--fg)">
            <input id="motionRecord247" type="checkbox" style="width:auto;margin:0">
            <span>24/7 Motion Recording (auto-archive gym activity outside workouts with 12s pre-roll &amp; 18s post-roll)</span>
          </label>
          <div class="ctrl-row" style="margin-top:10px">
            <button class="btn" id="saveBtn" type="button">Save Camera Config &amp; Apply</button>
            <span id="saveMsg" style="color:var(--mut);font-size:12px"></span>
          </div>
        </div>
      </section>

      <!-- PAGE 3: FOOTAGE (14-DAY SCRUBBER & CLIPS) -->
      <section class="page" data-page="footage">
        <div class="page-head">
          <div>
            <h1>14-Day NVR Scrubber &amp; Saved Clips</h1>
            <div class="page-sub">Continuous fMP4 playback on one master timeline with Workout &amp; 24/7 Motion filtering, instant thumbnail scrub, and 0ms sensitivity controls.</div>
          </div>
          <div class="ts-head-tools">
            <div class="ts-seg-ctrl" role="group" aria-label="Source filter">
              <button class="ts-seg-btn on" type="button" data-tssrc="all">All</button>
              <button class="ts-seg-btn" type="button" data-tssrc="workout">Workouts</button>
              <button class="ts-seg-btn" type="button" data-tssrc="motion">24/7 Motion</button>
            </div>
            <button class="btn sec sm" id="tsRefreshBtn" type="button">↻ Refresh Tapes</button>
          </div>
        </div>

        <div class="card" id="tapeScrubCard">
          <h2>
            <span>14-Day Activity Tape Scrubber</span>
            <span style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span id="tsScanPill" class="pill live" hidden>Scanning v2…</span>
              <span id="tsStatusPill" class="pill live">READY</span>
            </span>
          </h2>
          <div class="eyebrow">Day</div>
          <div id="tsDayStrip" class="ts-strip"></div>
          <div id="tsSessionRow" hidden>
            <div class="eyebrow" style="margin-top:4px">View / Session</div>
            <div id="tsSessionStrip" class="ts-strip"></div>
          </div>
          <div class="eyebrow" style="margin-top:4px">Camera Angle</div>
          <div id="tsCamStrip" class="ts-strip"></div>

          <div class="ts-workspace">
            <div class="ts-player-col">
              <div class="ts-stage" id="tsStage">
                <div id="tsTimeHud" class="ts-hud">Select a session to scrub</div>
                <div class="ts-stage-tools" onclick="event.stopPropagation()">
                  <span id="tsPlayBadge" class="pill live" hidden>PLAYING</span>
                  <button class="btn sm sec" id="tsFsBtn" type="button" title="Fullscreen (F)">⤢</button>
                </div>
                <img id="tsFrameImg" alt="" hidden>
                <video id="tsVideo" muted playsinline hidden></video>
                <audio id="tsAudio" hidden></audio>
                <div id="tsBuffering" class="ts-buffering" hidden>
                  <span class="ts-spin"></span>
                  <span id="tsBufferLbl">Starting fMP4 stream…</span>
                </div>
                <div id="tsGapOverlay" class="ts-gap-overlay" hidden onclick="event.stopPropagation()">
                  <div class="ts-gap-title" id="tsGapTitle">No recording at this time</div>
                  <div class="ts-gap-sub" id="tsGapSub">Empty timeline gap</div>
                  <button class="btn sm" id="tsJumpNextRunBtn" type="button">Jump to Next Footage →</button>
                </div>
                <div id="tsSkipToast" class="ts-skip-toast" hidden></div>
              </div>

              <div class="ts-rail-wrap" id="tsRailWrap">
                <div class="ts-rail-bar">
                  <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                    <div class="ts-seg-ctrl" role="group" aria-label="Gap mode">
                      <button class="ts-seg-btn" type="button" id="tsGapCollapseBtn" data-tsgap="collapse">Collapse Gaps</button>
                      <button class="ts-seg-btn on" type="button" id="tsGapRealBtn" data-tsgap="real">Real Clock</button>
                    </div>
                    <div class="ts-seg-ctrl" role="group" aria-label="Timeline zoom">
                      <button class="ts-seg-btn on" type="button" data-tszoom="fit">Fit</button>
                      <button class="ts-seg-btn" type="button" data-tszoom="1800">30m</button>
                      <button class="ts-seg-btn" type="button" data-tszoom="600">10m</button>
                      <button class="ts-seg-btn" type="button" data-tszoom="180">3m</button>
                      <button class="ts-seg-btn" type="button" data-tszoom="60">1m</button>
                    </div>
                    <button class="btn sec sm" id="tsPanLeftBtn" type="button" hidden title="Pan earlier">◀</button>
                    <button class="btn sec sm" id="tsPanRightBtn" type="button" hidden title="Pan later">▶</button>
                  </div>
                  <div id="tsRunStrip" class="ts-run-strip"></div>
                </div>

                <div id="tsHoverPop" class="ts-hover-pop" hidden>
                  <img id="tsHoverImg" alt="" hidden>
                  <div id="tsHoverGap" class="ts-hover-nogap" hidden>No recording (gap)</div>
                  <div class="ts-hover-meta">
                    <span id="tsHoverTime">--:--:--</span>
                    <span id="tsHoverScore">0.0</span>
                  </div>
                </div>
                <canvas id="tsTimelineCanvas" width="1000" height="86"></canvas>
                <div class="ts-rail-foot">
                  <span id="tsStartLbl">--:--</span>
                  <span class="ts-legend">
                    <span><span class="ts-sw" style="background:#22c55e"></span>Workout</span>
                    <span><span class="ts-sw" style="background:#a855f7"></span>24/7 Motion</span>
                    <span><span class="ts-sw" style="background:#38bdf8"></span>Peak</span>
                    <span><span class="ts-sw" style="background:#f59e0b"></span>In–Out [ ]</span>
                  </span>
                  <span id="tsCurLbl" style="color:#eaf4ff">--:--:--</span>
                  <span id="tsEndLbl">--:--</span>
                </div>
              </div>

              <div class="ts-transport">
                <button class="btn sec sm" id="tsPrevBurstBtn" type="button" title="Previous motion event (,)">⏮ Prev</button>
                <button class="btn sec sm" type="button" data-tsstep="-10">-10s</button>
                <button class="btn sec sm" type="button" data-tsstep="-2">-2s</button>
                <button class="btn ok sm ts-play-main" id="tsPlayBtn" type="button" title="Play / Pause continuous stream (Space)">▶ Play</button>
                <button class="btn sec sm" type="button" data-tsstep="2">+2s</button>
                <button class="btn sec sm" type="button" data-tsstep="10">+10s</button>
                <button class="btn sec sm" id="tsNextBurstBtn" type="button" title="Next motion event (.)">Next ⏭</button>
                <select id="tsSpeedSel" aria-label="Playback speed" title="Playback speed">
                  <option value="0.25">0.25×</option>
                  <option value="0.5">0.5×</option>
                  <option value="1" selected>1×</option>
                  <option value="1.5">1.5×</option>
                  <option value="2">2×</option>
                  <option value="4">4×</option>
                </select>
                <button class="btn sec sm" id="tsMotionOnlyBtn" type="button" title="Auto-skip quiet rest zones during playback (M)">⚡ Motion Only</button>
                <button class="btn sec sm" id="tsAudioBtn" type="button" title="Toggle synced S330 room audio">🔇 Audio</button>
              </div>

              <div class="ts-clip-bar">
                <button class="btn sec sm" id="tsSetInBtn" type="button">Set In [</button>
                <button class="btn sec sm" id="tsSetOutBtn" type="button">Set Out ]</button>
                <button class="btn sec sm" id="tsClearRangeBtn" type="button">Clear</button>
                <span id="tsRangeInfo" style="font-size:11px;color:var(--mut);font-family:ui-monospace,Consolas,monospace">No range</span>
                <input id="tsClipLabel" type="text" placeholder="Clip name (e.g. Squat 315x3)" style="flex:1;min-width:165px;padding:5px 9px;font-size:12px">
                <label style="margin:0;font-size:12px;display:inline-flex;align-items:center;gap:5px;cursor:pointer;white-space:nowrap">
                  <input id="tsIncludeAudio" type="checkbox" style="width:auto;margin:0"> S330 Audio
                </label>
                <button class="btn sm" id="tsSaveClipBtn" type="button">💾 Save Clip to Drive</button>
                <span id="tsOut" style="font-size:12px;color:var(--mut)"></span>
              </div>
            </div>

            <aside class="ts-sidebar">
              <div class="ts-side-head">
                <div class="ts-side-title">
                  <span>Motion Events</span>
                  <span id="tsEvCountPill" class="pill ok">0</span>
                </div>
                <button class="btn sec sm" id="tsRescanBtn" type="button" title="Re-analyze recordings with Motion Engine v2">↻ Re-scan</button>
              </div>
              <div class="ts-filter-box">
                <div class="ts-filter-row">
                  <span>Sensitivity</span>
                  <input id="tsSensSlider" type="range" min="0.6" max="8.0" step="0.1" value="1.5">
                  <b id="tsSensVal" style="color:#eaf4ff;min-width:28px;text-align:right;font-family:ui-monospace,Consolas,monospace">1.5</b>
                </div>
                <div class="ts-filter-row">
                  <div class="ts-seg-ctrl" role="group" aria-label="Sensitivity presets">
                    <button class="ts-seg-btn" type="button" data-tssens="0.9">Fine 0.9</button>
                    <button class="ts-seg-btn on" type="button" data-tssens="1.5">Balanced 1.5</button>
                    <button class="ts-seg-btn" type="button" data-tssens="5.0">Heavy 5.0</button>
                  </div>
                </div>
                <div class="ts-filter-row">
                  <span>Min Dur</span>
                  <div class="ts-seg-ctrl" role="group" aria-label="Min duration">
                    <button class="ts-seg-btn" type="button" data-tsmindur="2">2s+</button>
                    <button class="ts-seg-btn on" type="button" data-tsmindur="5">5s+</button>
                    <button class="ts-seg-btn" type="button" data-tsmindur="12">12s+</button>
                    <button class="ts-seg-btn" type="button" data-tsmindur="25">25s+</button>
                  </div>
                </div>
                <div class="ts-filter-row">
                  <span>Scope</span>
                  <div class="ts-seg-ctrl" role="group" aria-label="Camera scope">
                    <button class="ts-seg-btn on" type="button" data-tsscope="cam">This Cam</button>
                    <button class="ts-seg-btn" type="button" data-tsscope="all">All Cams</button>
                  </div>
                </div>
              </div>
              <div id="tsBurstList" class="ts-bursts">
                <span style="font-size:12px;color:var(--mut)">Select a day/session above to inspect motion events.</span>
              </div>
            </aside>
          </div>
        </div>

        <div class="card" style="margin-top:14px">
          <h2>
            <span>Recent Saved Clips</span>
            <span id="calBadge" class="pill">0 / 5 clips for pose calibration</span>
          </h2>
          <div id="clipList" style="color:var(--mut)">No clips saved yet.</div>
        </div>
      </section>

      <!-- PAGE 4: SHOW LAB -->
      <section class="page" data-page="shows">
        <div class="page-head">
          <div>
            <h1>Walk-Ins &amp; PR Drop-Ins (Show Lab)</h1>
            <div id="shMsg" class="page-sub">
              All shows start collapsed — tap any show to edit scripts, hype/speed tags, voice &amp; timing, or fire right from its header. Studio voices auto-cache so speed/pause tweaks use zero cloud quota.
            </div>
          </div>
          <div class="ctrl-row" style="margin-top:0">
            <button class="btn sec sm" id="shExpandAllBtn" type="button">Expand All</button>
            <button class="btn sec sm" id="shCollapseAllBtn" type="button">Collapse All</button>
            <button class="btn red sm" id="shStopAllBtn" type="button">⏹ Stop Active Show</button>
            <span id="shBadge" class="pill">IDLE</span>
          </div>
        </div>

        <details id="devRyderBox" class="dev-drop-box show-acc">
          <summary class="show-acc-sum">
            <span class="show-acc-left">
              <span class="show-chev">▸</span>
              <span class="pill live">DEV DROP</span>
              <span class="show-acc-title">Ryder Curry Armory Debut</span>
              <span id="devKokoroPill" class="pill ok">Checking Voices…</span>
              <span id="devStagePill" class="pill">Checking MP4 &amp; Screens…</span>
            </span>
            <span class="show-acc-acts">
              <button class="btn sm ok" type="button" title="Simulate Ryder show locally" onclick="event.preventDefault(); event.stopPropagation(); $('devRyderBox').open = true; startLocalRyderSim(null)">🎬 Sim</button>
              <button class="btn sm" type="button" title="Fire Ryder live on screens + Denon" onclick="event.preventDefault(); event.stopPropagation(); if ($('devFireLiveBtn')) $('devFireLiveBtn').click()">🔥 Fire</button>
            </span>
          </summary>
          <div class="show-acc-body">
            <div class="ctrl-row" style="margin-top:0">
              <button class="btn sm ok" id="devSimFullBtn" type="button">🎬 Simulate Full Show (Local)</button>
              <button class="btn sm sec" id="devSimJumpBtn" type="button">⏩ Jump to 1:16 (Test 1:20 Overlay)</button>
              <button class="btn sm red" id="devSimStopBtn" type="button">⏹ Stop Sim / Live</button>
              <button class="btn sm sec" id="devSimFsBtn" type="button">⤢ Fullscreen</button>
              <button class="btn sm" id="devFireLiveBtn" type="button">🔥 Fire Ryder LIVE (Screens + Denon 70)</button>
              <label style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;margin:0;cursor:pointer">
                <input id="devRyExcludeMainTv" type="checkbox" style="width:auto;margin:0"> Exclude Main TV (Coding)
              </label>
            </div>

            <div id="devStage" class="dev-stage">
              <div class="fx-hud">
                <span id="devLightsHud" class="fx-tag">LIGHTS: STANDBY</span>
                <span id="devFogHud" class="fx-tag">FOG: OFF</span>
                <span id="devFlashHud" class="fx-tag">FLASH: READY</span>
                <span id="devDenonHud" class="fx-tag">DENON: STANDBY</span>
                <span id="devSimClock" class="fx-tag">0:00</span>
              </div>
              <div class="dev-yt-wrap" id="devYtWrap">
                <video id="devRyVideo" controls playsinline preload="metadata" style="width:100%;height:100%;background:#000;border-radius:8px"></video>
                <div id="devVidFlash" style="pointer-events:none;position:absolute;inset:0;background:#fff;opacity:0;border-radius:8px;transition:opacity 0.05s linear;z-index:4"></div>
              </div>
              <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:center;z-index:2">
                <span id="devScrubClock" class="pill" style="font-family:ui-monospace,Consolas,monospace;font-size:12px">MP4 0:00.0 · Intro 0:00.0</span>
                <button class="btn sm sec" type="button" data-scrub="-5">-5s</button>
                <button class="btn sm sec" type="button" data-scrub="-1">-1s</button>
                <button class="btn sm sec" type="button" data-scrub="-0.1">-0.1s</button>
                <button class="btn sm ok" id="devScrubPlayBtn" type="button">▶/⏸</button>
                <button class="btn sm sec" type="button" data-scrub="0.1">+0.1s</button>
                <button class="btn sm sec" type="button" data-scrub="1">+1s</button>
                <button class="btn sm sec" type="button" data-scrub="5">+5s</button>
              </div>
              <div id="devCaption" class="dev-caption">Click <b>Simulate Full Show</b> (or say <code>&ldquo;Armory, Ryder is here&rdquo;</code>) to watch the pre-baked 1080p MP4 + synchronized TV stage cues.</div>
              <div id="devLog" class="dev-log">[Show Lab] Ready.</div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px">
              <div>
                <label>Audition Intro (Thunder + Announcer + Crowd)</label>
                <audio class="show-audio" id="shAudio_ryder_walkin" controls preload="none" src="/api/shows/ryder_walkin/preview.wav"></audio>
                <div class="ctrl-row" style="margin-top:4px">
                  <button class="btn sm ok" id="devRyTestIntroBtn" type="button">▶ Test Intro Voice</button>
                  <label style="margin:0;font-size:11px;display:inline-flex;align-items:center;gap:4px;cursor:pointer">
                    <input id="devRyTestFxChk" type="checkbox" style="width:auto;margin:0"> + Thunder &amp; Crowd
                  </label>
                </div>
              </div>
              <div>
                <label>Audition Speech Box (Dry / Fast Test)</label>
                <audio class="show-audio" id="shOvAudio_ryder_walkin" controls preload="none" src="/api/shows/ryder_walkin/overlay.wav"></audio>
                <div class="ctrl-row" style="margin-top:4px">
                  <button class="btn sm ok" id="devRyTestOvBtn" type="button">▶ Test 1st Speech Box</button>
                </div>
              </div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px">
              <div><label>Studio Voice Engine</label><select id="devRyVoice"></select></div>
              <div><label>Intro Voice Start (s)</label><input id="devRyIntroStart" type="number" min="0" max="15" step="0.1" value="2.3"></div>
              <div><label>Hall Reverb (0.00–0.25)</label><input id="devRyRev" type="number" min="0" max="0.25" step="0.02" value="0.06"></div>
              <div><label>Voice Gain Boost (dB)</label><input id="devRyBoost" type="number" min="-4" max="6" step="0.5" value="2.0"></div>
              <div><label>Fog Burst (s)</label><input id="devRyFog" type="number" min="1" max="60" step="1" value="30"></div>
              <div><label>Pulse Strobe (s / int / on)</label><input id="devRyPulse" type="text" value="20 / 2.0 / 0.25" readonly style="opacity:.75"></div>
            </div>
            <div>
              <label>Voice Acting Style / Direction (used by Gemini 2.5 Flash TTS &amp; OpenAI gpt-4o-mini-tts)</label>
              <input id="devRyStyle" type="text" placeholder="Electric HBO championship boxing ring announcer in a packed arena...">
            </div>
            <div>
              <label>Main HBO Intro Script (starts at Intro Voice Start; supports inline <code>[pause:100ms]</code>, <code>[speed:+10%]</code>, <code>[vol:+3dB]</code>, <code>[shouting]</code>)</label>
              <textarea id="devRyIntroTxt" rows="3"></textarea>
            </div>
            <div class="tag-bar" id="devRyTagBar">
              <span style="font-size:11px;color:var(--mut);font-weight:700">Hype &amp; Speed:</span>
              <button class="tag-chip" type="button" data-tag=" [excited] ">⚡ Hype [excited]</button>
              <button class="tag-chip" type="button" data-tag=" [shouting] ">🔥 Shout [shouting]</button>
              <button class="tag-chip" type="button" data-tag=" [dramatic] ">🎭 [dramatic]</button>
              <button class="tag-chip" type="button" data-tag=" [rushed] ">💨 [rushed]</button>
              <button class="tag-chip" type="button" data-wrap="speed:+10%">⏩ Faster +10%</button>
              <button class="tag-chip" type="button" data-wrap="speed:+20%">⏩ Faster +20%</button>
              <button class="tag-chip" type="button" data-wrap="speed:-10%">🐢 Slower -10%</button>
              <button class="tag-chip" type="button" data-wrap="vol:+3dB">🔊 Louder +3dB</button>
              <button class="tag-chip" type="button" data-tag=" [pause:100ms] ">⏱ +0.1s</button>
              <button class="tag-chip" type="button" data-tag=" [pause:350ms] ">⏱ +0.35s</button>
              <button class="tag-chip" type="button" data-tag=" [pause:750ms] ">⏱ +0.75s</button>
            </div>
            <div style="border-top:1px solid rgba(255,255,255,.08);padding-top:8px">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                <span style="font-weight:800;font-size:12px;color:#7cc4ff">Timed Speech Boxes (Intro or Entrance Video · 📍 Stamp from Scrubber)</span>
                <button class="btn sm sec" id="devRyAddSpeechBtn" type="button">+ Add Speech Box</button>
              </div>
              <div id="devRySpeechList" class="cue-list"></div>
            </div>
            <div style="border-top:1px solid rgba(255,255,255,.08);padding-top:8px">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                <span style="font-weight:800;font-size:12px;color:#fde047">Timed FX / Screen Flash Cues (Zero MP4 Rebuild Required)</span>
                <button class="btn sm sec" id="devRyAddFxBtn" type="button">+ Add FX Cue</button>
              </div>
              <div id="devRyFxCueList" class="cue-list"></div>
            </div>
            <div class="ctrl-row" style="margin-top:0">
              <button class="btn sm" id="devRySaveBtn" type="button">Save &amp; Bake Into Show MP4</button>
              <button class="btn sm ok" id="devRySaveQuickBtn" type="button">Save Timings / FX Only (No MP4 Rebuild)</button>
              <button class="btn sm sec" id="devRyFetchVidBtn" type="button">↻ Fetch YouTube Source &amp; Rebuild MP4</button>
              <label style="margin:0;font-size:12px">Swap Entrance Video (.mp4): <input id="devRyVidFile" type="file" accept=".mp4,.mkv,.mov,.webm"></label>
              <span id="shOut_ryder_walkin" style="color:var(--mut);font-size:12px"></span>
            </div>
            <details class="sh-sub">
              <summary>🎙 Custom Intro / Overlay Voice Uploads (.mp3/.wav)</summary>
              <div class="sh-sub-body">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px">
                  <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                    <label style="margin:0;font-size:12px">Custom Intro Voice (.mp3/.wav): <input id="devRyIntroVoiceFile" type="file" accept=".mp3,.wav,.m4a,.ogg"></label>
                    <button class="btn sm sec" id="devRyIntroVoiceClr" type="button" hidden>Revert to TTS</button>
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                    <label style="margin:0;font-size:12px">Custom 1:20 Overlay (.mp3/.wav): <input id="devRyOvVoiceFile" type="file" accept=".mp3,.wav,.m4a,.ogg"></label>
                    <button class="btn sm sec" id="devRyOvVoiceClr" type="button" hidden>Revert to TTS</button>
                  </div>
                </div>
              </div>
            </details>
          </div>
        </details>

        <div class="card">
          <h2><span>PR &amp; Walk-In Shows</span><span id="shCountPill" class="pill">Loading…</span></h2>
          <div id="shGrid" class="show-grid">
            <div style="color:var(--mut);font-size:12px">Loading shows…</div>
          </div>
        </div>

        <details id="ttsKeysCard" class="card show-acc" style="margin-top:14px">
          <summary class="show-acc-sum">
            <span class="show-acc-left">
              <span class="show-chev">▸</span>
              <strong>Studio Voice API Keys</strong>
              <span class="show-acc-sub">ElevenLabs · Gemini 2.5 · OpenAI</span>
            </span>
            <span class="show-acc-acts">
              <span id="ttsKeysPill" class="pill">Checking…</span>
            </span>
          </summary>
          <div class="show-acc-body">
            <div style="font-size:11px;color:var(--mut)">
              Gemini Free Tier allows <code>10</code> requests/day (<code>...</code> pauses coalesce to 1 call &amp; segments auto-cache on disk); ElevenLabs Free gives <code>10,000</code> chars/mo; Kokoro Local is unlimited offline.
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px">
              <div><label>ElevenLabs API Key <span id="ttsElHint" style="color:#86efac"></span></label><input id="ttsElKey" type="password" autocomplete="off" placeholder="sk_... (elevenlabs.io free tier 10k/mo)"></div>
              <div><label>ElevenLabs Model</label><select id="ttsElModel"><option value="eleven_v3">eleven_v3 (Most Expressive + [shouting])</option><option value="eleven_multilingual_v2">eleven_multilingual_v2 (Ultra-Stable)</option></select></div>
              <div><label>Google Gemini AI Studio Key <span id="ttsGemHint" style="color:#86efac"></span></label><input id="ttsGemKey" type="password" autocomplete="off" placeholder="AIza... (aistudio.google.com free)"></div>
              <div><label>OpenAI API Key <span id="ttsOaiHint" style="color:#86efac"></span></label><input id="ttsOaiKey" type="password" autocomplete="off" placeholder="sk-... (gpt-4o-mini-tts)"></div>
            </div>
            <div class="ctrl-row" style="margin-top:0">
              <button class="btn sm" id="ttsSaveKeysBtn" type="button">Save API Keys &amp; Refresh Voices</button>
              <span id="ttsKeysOut" style="color:var(--mut);font-size:12px"></span>
            </div>
          </div>
        </details>

        <details id="fxHooksCard" class="card show-acc" style="margin-top:14px">
          <summary class="show-acc-sum">
            <span class="show-acc-left">
              <span class="show-chev">▸</span>
              <strong>Smart FX Hooks</strong>
              <span class="show-acc-sub">House Lights (Rack &amp; Smith) · Fog · Theater LEDs</span>
            </span>
          </summary>
          <div class="show-acc-body">
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px">
              <div><label>Armory Lights ON URL (Matter Bridge / HTTP)</label><input id="fxLightsOnUrl" type="text" placeholder="http://127.0.0.1:8787/api/armory-lights?on=1"></div>
              <div><label>Armory Lights OFF URL (Matter Bridge / HTTP)</label><input id="fxLightsOffUrl" type="text" placeholder="http://127.0.0.1:8787/api/armory-lights?on=0"></div>
              <div><label>Lights Lead Time (0–5s, sync offset)</label><input id="fxLightsLeadSec" type="number" min="0" max="5" step="0.1" value="0"></div>
              <div><label>Smart Fogger v3.1 IP / Host</label><input id="fxTheaterHost" type="text" placeholder="fx.local or 192.168.1.xxx"></div>
              <div><label>Fog Machine Relay / ESP32 IP</label><input id="fxFogHost" type="text" placeholder="192.168.1.xxx"></div>
              <div><label>Fog Trigger URL ({sec} placeholder)</label><input id="fxFogUrl" type="text" placeholder="http://.../rpc/Switch.Set?id=0&amp;on=true&amp;toggle_after={sec}"></div>
              <div><label>Default Fog Burst (1–60s)</label><input id="fxFogBurst" type="number" min="1" max="60" step="1" value="30"></div>
            </div>
            <details style="margin-top:0">
              <summary style="cursor:pointer;font-size:11px;color:var(--mut)">Optional Dual-Zone Shelly Overrides (Rack / Smith / Legacy IP)</summary>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px;margin-top:6px">
                <div><label>House Lights – Rack IP (Shelly)</label><input id="fxLightsRackHost" type="text" placeholder="192.168.1.xxx"></div>
                <div><label>Rack ON URL (optional override)</label><input id="fxLightsRackOnUrl" type="text" placeholder="http://.../rpc/Switch.Set?id=0&amp;on=true"></div>
                <div><label>Rack OFF URL (optional override)</label><input id="fxLightsRackOffUrl" type="text" placeholder="http://.../rpc/Switch.Set?id=0&amp;on=false"></div>
                <div><label>House Lights – Smith IP (Shelly)</label><input id="fxLightsSmithHost" type="text" placeholder="192.168.1.xxx"></div>
                <div><label>Smith ON URL (optional override)</label><input id="fxLightsSmithOnUrl" type="text" placeholder="http://.../rpc/Switch.Set?id=0&amp;on=true"></div>
                <div><label>Smith OFF URL (optional override)</label><input id="fxLightsSmithOffUrl" type="text" placeholder="http://.../rpc/Switch.Set?id=0&amp;on=false"></div>
                <div><label>Legacy House Lights IP</label><input id="fxLightsHost" type="text" placeholder="192.168.1.xxx"></div>
              </div>
            </details>
            <div class="ctrl-row" style="margin-top:0;flex-wrap:wrap;gap:6px">
              <button class="btn sm" id="fxSaveBtn" type="button">Save FX Hooks</button>
              <button class="btn sm ok" id="fxRackOnBtn" type="button">Rack ON</button>
              <button class="btn sm sec" id="fxRackOffBtn" type="button">Rack OFF</button>
              <button class="btn sm ok" id="fxSmithOnBtn" type="button">Smith ON</button>
              <button class="btn sm sec" id="fxSmithOffBtn" type="button">Smith OFF</button>
              <button class="btn sm ok" id="fxAllOnBtn" type="button">All Lights ON</button>
              <button class="btn sm sec" id="fxAllOffBtn" type="button">All Lights OFF</button>
              <button class="btn sec sm" id="fxTestFlashBtn" type="button">Test 2x Both Flash</button>
              <button class="btn sec sm" id="fxTestAltFlashBtn" type="button">Test 4x Alternate Rack ↔ Smith</button>
              <button class="btn sec sm" id="fxTestFogBtn" type="button">Test Fog Trigger</button>
              <select id="fxTheaterTestScene" style="width:auto;min-width:155px">
                <option value="jenna">jenna (Missy UV/Gold)</option>
                <option value="avenleigh">avenleigh (Pink VIP)</option>
                <option value="vip_gold">vip_gold (Velvet Rope)</option>
                <option value="vip_prism">vip_prism (Modern Prism)</option>
                <option value="thunder">thunder (Storm)</option>
                <option value="fireworks">fireworks (PR Pyro)</option>
                <option value="bench_hype">bench_hype (Forge Red)</option>
                <option value="walkin">walkin (Crimson)</option>
                <option value="pr">pr (PR Blast)</option>
              </select>
              <button class="btn sec sm" id="fxTestTheaterBtn" type="button">Test Theater Scene</button>
              <button class="btn sec sm" id="fxStopTheaterBtn" type="button">Stop Theater FX</button>
              <span id="fxOut" style="color:var(--mut);font-size:12px"></span>
            </div>
          </div>
        </details>
      </section>

      <!-- PAGE 5: GYM & DISPLAYS -->
      <section class="page" data-page="gym">
        <div class="page-head">
          <div>
            <h1>Gym &amp; Displays</h1>
            <div class="page-sub">Discrete power, HDMI inputs, Open/Close Gym macros, Denon monitor routing &amp; 32&quot; Wallboard kiosk control.</div>
          </div>
        </div>

        <div class="card">
          <h2>
            <span>Gym Displays &amp; Discrete Power Control</span>
            <span id="dpBadge" class="pill">READY</span>
          </h2>
          <div id="dpMsg" style="color:var(--mut);font-size:12px;margin-bottom:10px">
            Discrete IP power &amp; input commands (never toggles). Devices: <strong>TV</strong> (Hisense), <strong>Wallboard</strong>, <strong>SmartSpin TV</strong>, <strong>Fire TV Cube</strong>, and <strong>Denon</strong>. Voice: <code>&ldquo;Armory, open the gym&rdquo;</code>, <code>&ldquo;Armory, close the gym&rdquo;</code>, <code>&ldquo;Armory, turn on Fire TV Cube&rdquo;</code>, <code>&ldquo;Armory, sleep Fire TV Cube&rdquo;</code>, <code>&ldquo;Armory, turn on all TVs&rdquo;</code>, <code>&ldquo;Armory, switch TV to PC&rdquo;</code>.
          </div>

          <div class="dp-groups">
            <div class="dp-group">
              <div class="eyebrow">Gym Macros &amp; All TVs</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="open_gym:run">Open Gym</button>
                <button class="btn red sm" type="button" data-dpact="close_gym:run">Close Gym</button>
                <button class="btn ok sm" type="button" data-dpact="all_tvs:on">All TVs ON</button>
                <button class="btn sec sm" type="button" data-dpact="all_tvs:off">All TVs OFF</button>
              </div>
            </div>
            <div class="dp-group">
              <div class="eyebrow">Main TV (Hisense)</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="tv:on">TV ON</button>
                <button class="btn sec sm" type="button" data-dpact="tv:off">TV OFF</button>
                <button class="btn sec sm" type="button" data-dpact="tv:hdmi1">TV PC (HDMI 1)</button>
                <button class="btn sec sm" type="button" data-dpact="tv:hdmi2">TV Fire TV (HDMI 2)</button>
              </div>
            </div>
            <div class="dp-group">
              <div class="eyebrow">SmartSpin (TCL) &amp; Wallboard</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="spin:on">SmartSpin ON</button>
                <button class="btn sec sm" type="button" data-dpact="spin:off">SmartSpin OFF</button>
                <button class="btn ok sm" type="button" data-dpact="wallboard:on">Wallboard ON</button>
                <button class="btn sec sm" type="button" data-dpact="wallboard:off">Wallboard OFF</button>
              </div>
            </div>
            <div class="dp-group">
              <div class="eyebrow">Fire TV Cube</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="cube:on">Cube ON</button>
                <button class="btn sec sm" type="button" data-dpact="cube:off">Cube OFF</button>
                <button class="btn sec sm" type="button" data-dpact="cube:home">Cube Home</button>
              </div>
            </div>
            <div class="dp-group" style="grid-column:1 / -1">
              <div class="eyebrow">Denon AVR-3313CI</div>
              <div class="ctrl-row">
                <button class="btn ok sm" type="button" data-dpact="avr:on">Denon ON</button>
                <button class="btn sec sm" type="button" data-dpact="avr:off">Denon OFF</button>
                <button class="btn sec sm" type="button" data-dpact="avr:mch_stereo">Denon Multi-Ch</button>
                <button class="btn sec sm" type="button" data-dpact="avr:monitor1">Denon Monitor 1</button>
                <button class="btn sec sm" type="button" data-dpact="avr:monitor_auto">Denon Dual</button>
              </div>
            </div>
          </div>

          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">
            <div><label>TV IP (Hisense)</label><input id="dpPrimHost" type="text" value="192.168.1.230"></div>
            <div><label>TV Wi-Fi MAC</label><input id="dpPrimMac" type="text" value="bc:09:b9:e7:35:26"></div>
            <div><label>TV PC Input (HDMI 1)</label><input id="dpPrimHdmi1" type="text" value="KEYCODE_TV_INPUT_HDMI_1"></div>
            <div><label>TV Denon Input (HDMI 2)</label><input id="dpPrimHdmi2" type="text" value="KEYCODE_TV_INPUT_HDMI_2"></div>
            <div><label>SmartSpin TV IP (TCL)</label><input id="dpSpinHost" type="text" placeholder="192.168.1.xxx"></div>
            <div><label>SmartSpin MAC</label><input id="dpSpinMac" type="text" placeholder="xx:xx:xx:xx:xx:xx"></div>
            <div><label>SmartSpin HDMI Input</label><input id="dpSpinHdmi" type="text" value="InputHDMI1"></div>
            <div>
              <label>Auto Denon Monitor 1 / Dual</label>
              <select id="dpSpinDual"><option value="1">Yes (Single Sink when Spin OFF)</option><option value="0">No (Keep Denon on Auto Dual)</option></select>
            </div>
            <div>
              <label>Primary Denon Monitor</label>
              <select id="dpPrimDenonMon"><option value="1">Monitor 1 (Hisense)</option><option value="2">Monitor 2</option></select>
            </div>
            <div>
              <label>Open Gym Music (Cube)</label>
              <select id="dpOpenMusic"><option value="1">Yes (Launch YouTube Music)</option><option value="0">No</option></select>
            </div>
            <div><label>Fire TV Cube IP</label><input id="dpCubeHost" type="text" value="192.168.1.151"></div>
            <div><label>YouTube Music Playlist/Mix URL</label><input id="dpMusicUrl" type="text" placeholder="https://music.youtube.com/playlist?list=..."></div>
            <div><label>YouTube Music Search Fallback</label><input id="dpMusicQuery" type="text" value="workout music mix"></div>
          </div>
          <div class="ctrl-row" style="margin-top:10px">
            <button class="btn" id="dpSaveBtn" type="button">Save Displays</button>
            <button class="btn sec" id="dpProbePrimBtn" type="button">Probe TV ADB</button>
            <button class="btn sec" id="dpProbeCubeBtn" type="button">Probe Cube ADB</button>
            <button class="btn sec" id="dpFindRokuBtn" type="button">Find Roku TVs</button>
            <span id="dpOut" style="color:var(--mut);font-size:12px"></span>
          </div>
          <div id="dpRokuList" style="margin-top:8px;color:var(--mut);font-size:12px" hidden></div>
        </div>

        <div class="card">
          <h2>
            <span>32&quot; Fire TV Wallboard &amp; ADB Auto-Wake</span>
            <span style="display:flex;align-items:center;gap:8px">
              <a href="/wallboard" target="_blank" style="font-size:12px;font-weight:700">Open Wallboard ↗</a>
              <span id="wbBadge" class="pill">UNCONFIGURED</span>
            </span>
          </h2>
          <div id="wbMsg" style="color:var(--mut);font-size:12px;margin-bottom:8px">Permanent URL: <code>/wallboard</code></div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">
            <div><label>Fire TV IP / Hostname</label><input id="wbHost" type="text" placeholder="192.168.1.xxx"></div>
            <div><label>Fire TV MAC (Wake-on-LAN)</label><input id="wbMac" type="text" placeholder="08:97:98:xx:xx:xx"></div>
            <div><label>ADB Port</label><input id="wbPort" type="number" min="1" max="65535" value="5555"></div>
            <div>
              <label>Display Client</label>
              <select id="wbClient">
                <option value="armory">Armory Wallboard App (Kiosk APK)</option>
                <option value="silk">Amazon Silk</option>
                <option value="fully">Fully Kiosk Browser</option>
                <option value="custom">Custom App / URL Intent</option>
              </select>
            </div>
            <div><label>Idle Auto-Sleep (minutes)</label><input id="wbIdleMin" type="number" min="1" max="120" value="10"></div>
            <div>
              <label>Wake on Workout Start</label>
              <select id="wbWake"><option value="1">Yes (Auto-Wake)</option><option value="0">No</option></select>
            </div>
            <div>
              <label>Sleep When Workout Ends</label>
              <select id="wbSleep"><option value="1">Yes (30s delay)</option><option value="0">No</option></select>
            </div>
          </div>
          <div class="ctrl-row" style="margin-top:10px">
            <button class="btn" id="wbSaveBtn" type="button">Save Wallboard</button>
            <button class="btn sec" id="wbPairBtn" type="button">Connect / Pair ADB</button>
            <button class="btn ok" id="wbWakeBtn" type="button">Test Wake &amp; Launch</button>
            <button class="btn sec" id="wbSleepBtn" type="button">Sleep TV Now</button>
            <span id="wbOut" style="color:var(--mut);font-size:12px"></span>
          </div>
        </div>
      </section>

      <!-- PAGE 6: AUDIO & VOICE -->
      <section class="page" data-page="audio">
        <div class="page-head">
          <div>
            <h1>Audio &amp; Voice</h1>
            <div class="page-sub">Denon AVR-3313CI + Fire TV Cube audio mixer routing, and PowerConf S330 room mic Whisper relay.</div>
          </div>
        </div>

        <div class="card">
          <h2>
            <span>House Audio (Denon AVR-3313CI + Fire TV Cube Mixer)</span>
            <span style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span id="haDenonPill" class="pill">DENON…</span>
              <span id="haCubePill" class="pill">CUBE…</span>
            </span>
          </h2>
          <div id="haMsg" style="color:var(--mut);font-size:12px;margin-bottom:8px">Checking Denon AVR (<code>192.168.1.15</code>) &amp; Fire TV Cube mixer…</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px">
            <div>
              <label>Output Route</label>
              <select id="haRoute">
                <option value="s330">PowerConf S330 Only</option>
                <option value="auto">Auto: Cube (when MPLAY) -&gt; S330</option>
                <option value="cube">Fire TV Cube Only</option>
              </select>
            </div>
            <div><label>Denon AVR IP</label><input id="haDenonHost" type="text" value="192.168.1.15" placeholder="192.168.1.15"></div>
            <div><label>Fire TV Cube IP</label><input id="haCubeHost" type="text" placeholder="192.168.1.xxx"></div>
            <div>
              <label>Cube Mixer APK</label>
              <select id="haCubeEnabled">
                <option value="0">Disabled</option>
                <option value="1">Enabled (:8767)</option>
              </select>
            </div>
            <div>
              <label>Readback Mode</label>
              <select id="haReadMode">
                <option value="overlay">Overlay (No Pause/Duck)</option>
                <option value="duck">Duck (MAY_DUCK)</option>
                <option value="takeover">Takeover (Pause &amp; Resume)</option>
              </select>
            </div>
            <div>
              <label>Chime Mode</label>
              <select id="haChimeMode">
                <option value="overlay">Overlay (No Pause/Duck)</option>
                <option value="duck">Duck (MAY_DUCK)</option>
                <option value="takeover">Takeover (Pause &amp; Resume)</option>
              </select>
            </div>
            <div>
              <label>1RM / Hype Mode</label>
              <select id="haHypeMode">
                <option value="takeover">Takeover (Pause &amp; Resume)</option>
                <option value="duck">Duck (MAY_DUCK)</option>
                <option value="overlay">Overlay (No Pause/Duck)</option>
              </select>
            </div>
            <div>
              <label>Cube Audio Usage</label>
              <select id="haUsage">
                <option value="media">media (STREAM_MUSIC)</option>
                <option value="assist">assist (Guidance/TTS)</option>
                <option value="sonification">sonification (Chimes)</option>
                <option value="alarm">alarm (High Priority)</option>
              </select>
            </div>
            <div><label>Readback Gain (0.05–1.0)</label><input id="haReadGain" type="number" min="0.05" max="1.0" step="0.05" value="0.85"></div>
            <div><label>SFX Gain (0.05–1.0)</label><input id="haSfxGain" type="number" min="0.05" max="1.0" step="0.05" value="0.90"></div>
          </div>

          <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px">
              <div style="font-weight:700">Denon Voice Ducking Curves (Listening vs. Speaking)</div>
              <div style="font-size:11px;color:var(--mut)">Denon Vol = dB + 80 (e.g. Vol 45 = -35 dB · Vol 70 = -10 dB). Pick a preset or dial custom values, then click Save Audio.</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px">
              <div style="background:rgba(15,23,42,.55);border:1px solid var(--line);border-radius:10px;padding:10px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:8px">
                  <div style="font-weight:700;font-size:13px">🎙️ Listening Duck <span style="font-weight:400;color:var(--mut);font-size:11px">(on “Hey Jarvis” while you speak)</span></div>
                  <button class="btn sec" id="haTestListenDipBtn" type="button" style="padding:4px 8px;font-size:11px">Test Dip</button>
                </div>
                <div class="ctrl-row" style="gap:6px;margin-bottom:8px" id="haListenPresets">
                  <button class="btn sec" type="button" data-lpreset="off" style="padding:4px 9px;font-size:11px">Off</button>
                  <button class="btn sec" type="button" data-lpreset="mild" style="padding:4px 9px;font-size:11px">Mild (-10 dB)</button>
                  <button class="btn sec" type="button" data-lpreset="balanced" style="padding:4px 9px;font-size:11px">Balanced (-16 dB)</button>
                  <button class="btn sec" type="button" data-lpreset="deep" style="padding:4px 9px;font-size:11px">Deep (-24 dB)</button>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px">
                  <div>
                    <label>Curve Mode</label>
                    <select id="haListenDuckMode">
                      <option value="off">Off</option>
                      <option value="proportional">Proportional (Exp)</option>
                      <option value="fixed">Fixed Dip</option>
                    </select>
                  </div>
                  <div><label>No Duck &le; Vol</label><input id="haDuckThreshVol" type="number" min="15" max="80" step="1" value="45"></div>
                  <div><label id="haDuckDropLbl">Max Dip @ 75 (dB)</label><input id="haDuckMaxDrop" type="number" min="1" max="35" step="0.5" value="24.0"></div>
                  <div><label>Exp Curve (1–3)</label><input id="haDuckExp" type="number" min="1.0" max="3.0" step="0.05" value="1.55"></div>
                  <div><label>Floor (Min Vol)</label><input id="haDuckFloorVol" type="number" min="15" max="75" step="1" value="45"></div>
                </div>
                <div id="haListenPreview" style="margin-top:8px;font-size:11px;color:var(--mut);background:rgba(2,6,23,.5);border-radius:6px;padding:6px 8px;line-height:1.45"></div>
              </div>

              <div style="background:rgba(15,23,42,.55);border:1px solid var(--line);border-radius:10px;padding:10px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:8px">
                  <div style="font-weight:700;font-size:13px">🔊 Speaking / Output Duck <span style="font-weight:400;color:var(--mut);font-size:11px">(when Jarvis replies)</span></div>
                  <button class="btn sec" id="haTestOutDipBtn" type="button" style="padding:4px 8px;font-size:11px">Test Dip</button>
                </div>
                <div class="ctrl-row" style="gap:6px;margin-bottom:8px" id="haOutPresets">
                  <button class="btn sec" type="button" data-opreset="off" style="padding:4px 9px;font-size:11px">Off</button>
                  <button class="btn sec" type="button" data-opreset="mild" style="padding:4px 9px;font-size:11px">Mild (-10 dB)</button>
                  <button class="btn sec" type="button" data-opreset="balanced" style="padding:4px 9px;font-size:11px">Balanced (-14 dB)</button>
                  <button class="btn sec" type="button" data-opreset="match" style="padding:4px 9px;font-size:11px">Match Listening</button>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px">
                  <div>
                    <label>Curve Mode</label>
                    <select id="haOutDuckMode">
                      <option value="match">Match Listening</option>
                      <option value="off">Off (No Dip)</option>
                      <option value="proportional">Proportional (Exp)</option>
                      <option value="fixed">Fixed Dip</option>
                    </select>
                  </div>
                  <div><label>No Duck &le; Vol</label><input id="haOutDuckThreshVol" type="number" min="15" max="80" step="1" value="50"></div>
                  <div><label id="haOutDuckDropLbl">Max Dip @ 75 (dB)</label><input id="haOutDuckMaxDrop" type="number" min="1" max="35" step="0.5" value="10.0"></div>
                  <div><label>Exp Curve (1–3)</label><input id="haOutDuckExp" type="number" min="1.0" max="3.0" step="0.05" value="1.50"></div>
                  <div><label>Floor (Min Vol)</label><input id="haOutDuckFloorVol" type="number" min="15" max="75" step="1" value="45"></div>
                </div>
                <div id="haOutPreview" style="margin-top:8px;font-size:11px;color:var(--mut);background:rgba(2,6,23,.5);border-radius:6px;padding:6px 8px;line-height:1.45"></div>
              </div>
            </div>
          </div>
          <div class="ctrl-row" style="margin-top:10px">
            <button class="btn" id="haSaveBtn" type="button">Save Audio</button>
            <button class="btn sec" id="haDenonDipBtn" type="button">Test Denon Dip</button>
            <button class="btn sec" id="haCubeProbeBtn" type="button">Probe Cube ADB</button>
            <button class="btn ok" id="haCubeReviveBtn" type="button">Revive Cube Service</button>
            <span id="haOut" style="color:var(--mut);font-size:12px"></span>
          </div>
          <div id="haAdbDetails" style="margin-top:8px;color:var(--mut);font-size:12px" hidden></div>

          <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
            <div style="font-weight:700;margin-bottom:6px">One-Click Mode Test Bench</div>
            <div style="display:grid;grid-template-columns:minmax(180px,2fr) repeat(3,minmax(120px,1fr));gap:8px;align-items:end">
              <div><label>Test TTS Phrase</label><input id="haTestText" type="text" value="225 for 8, got it"></div>
              <div>
                <label>Target</label>
                <select id="haTestTarget">
                  <option value="auto">Auto Route</option>
                  <option value="cube">Fire TV Cube</option>
                  <option value="s330">PowerConf S330</option>
                </select>
              </div>
              <div>
                <label>Custom SFX (S:\\ArmoryCam\\sfx)</label>
                <select id="haSfxSel"><option value="">(None placed yet)</option></select>
              </div>
              <div>
                <label style="display:flex;align-items:center;gap:6px;margin-top:0;cursor:pointer">
                  <input id="haTestDenonDuck" type="checkbox" style="width:auto">
                  <span>Pair w/ Denon Dip</span>
                </label>
              </div>
            </div>
            <div class="ctrl-row" style="margin-top:8px">
              <button class="btn sec sm" type="button" data-hatest="tts:overlay">TTS Overlay</button>
              <button class="btn sec sm" type="button" data-hatest="tts:duck">TTS Duck</button>
              <button class="btn sec sm" type="button" data-hatest="tts:takeover">TTS Takeover</button>
              <button class="btn sec sm" type="button" data-hatest="chime:overlay">Chime Overlay</button>
              <button class="btn sec sm" type="button" data-hatest="rest_done:overlay">Rest Done</button>
              <button class="btn ok sm" type="button" data-hatest="pr_fanfare:takeover">PR Bell + Crowd (Takeover)</button>
              <button class="btn sec sm" type="button" data-hatest="pr_fanfare:duck">PR Bell + Crowd (Duck)</button>
              <button class="btn sec sm" type="button" id="haSfxPlayBtn">Play Custom SFX</button>
              <button class="btn red sm" type="button" id="haStopBtn">Stop</button>
              <span id="haTestOut" style="color:var(--mut);font-size:12px"></span>
            </div>
          </div>

          <details style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
            <summary style="cursor:pointer;font-weight:800;font-size:13px;color:#7cc4ff">Gym App Compatibility Matrix</summary>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:8px 0 6px">
              <b>Compatibility Log</b>
              <button class="btn sec sm" id="haMatrixResetBtn" type="button">Reset Matrix</button>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;align-items:end;margin-bottom:8px">
              <div>
                <label>App</label>
                <select id="haMxApp">
                  <option>Spotify</option>
                  <option>YouTube</option>
                  <option>Netflix</option>
                  <option>Prime Video</option>
                  <option>Amazon / Apple Music</option>
                  <option>Live TV / Sports</option>
                </select>
              </div>
              <div>
                <label>Mode</label>
                <select id="haMxMode">
                  <option value="overlay">overlay</option>
                  <option value="duck">duck</option>
                  <option value="takeover">takeover</option>
                </select>
              </div>
              <div>
                <label>Result</label>
                <select id="haMxStatus">
                  <option value="pass">pass</option>
                  <option value="warn">warn</option>
                  <option value="fail">fail</option>
                  <option value="untested">untested</option>
                </select>
              </div>
              <div><label>Observed Behavior</label><input id="haMxBehavior" type="text" placeholder="e.g. Clean mix, no pause"></div>
              <div><button class="btn sm" id="haMxSaveBtn" type="button">Log Result</button></div>
            </div>
            <div id="haMatrixTable" style="color:var(--mut);font-size:12px">Loading compatibility matrix…</div>
          </details>
        </div>

        <div class="card">
          <h2>
            <span>Room Voice Mic (PowerConf S330)</span>
            <span id="vxBadge" class="pill">CHECKING</span>
          </h2>
          <div id="vxMsg" style="color:var(--mut);font-size:12px;margin-bottom:8px">WASAPI Shared Mode opens automatically only during active Strength workouts so Windows Voice Typing (<code>Win + H</code>) works anytime.</div>
          <div class="mbar-wrap" style="margin-bottom:8px"><div id="vxMeter" class="mbar-fill"></div></div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">
            <div>
              <label>Room Mic Relay</label>
              <select id="vxEnabled"><option value="1">Enabled</option><option value="0">Disabled</option></select>
            </div>
            <div>
              <label>Listen Mode</label>
              <select id="vxMode"><option value="workout">Active Lift Workout</option><option value="always">Always Listening</option></select>
            </div>
            <div>
              <label>Input Microphone</label>
              <select id="vxDevice"><option value="PowerConf S330">PowerConf S330 (Auto)</option></select>
            </div>
            <div>
              <label>Spoken Readback (S330 Speaker)</label>
              <select id="vxReadback"><option value="1">Yes (Speak on S330)</option><option value="0">No (Pixel Only)</option></select>
            </div>
            <div>
              <label>Set Confirmation</label>
              <select id="vxSetCommitMode">
                <option value="voice">Voice (+10% Speed — Default)</option>
                <option value="chime">Commit Chime Only (Fast Bing)</option>
              </select>
            </div>
            <div>
              <label>Room / Gym Command Voice Reply</label>
              <select id="vxRoomCmdReadback">
                <option value="1">Yes (TVs, Lights, Music)</option>
                <option value="0">No (Silent — Workout Readback Only)</option>
              </select>
            </div>
            <div>
              <label>Wake Word (Streaming ONNX)</label>
              <select id="vxWakeWord">
                <option value="hey jarvis">Hey Jarvis (Built-in ONNX)</option>
                <option value="hey atlas">Hey Atlas (Custom ONNX)</option>
                <option value="hey armory">Hey Armory (Custom ONNX)</option>
                <option value="armory">Armory (Custom Bare ONNX)</option>
              </select>
            </div>
            <div>
              <label>Speaker Voice</label>
              <select id="vxTtsVoice">
                <optgroup label="Local GPU Instant (~80 ms · Offline)">
                  <option value="kokoro:am_onyx">Onyx (Deep Commanding Baritone)</option>
                  <option value="kokoro:am_fenrir">Fenrir (Bold Resonant)</option>
                  <option value="kokoro:onyx_fenrir_epic">Onyx + Fenrir Blend (Atlas)</option>
                  <option value="kokoro:onyx_george_hbo">Onyx + George Blend (Commander)</option>
                  <option value="kokoro:bm_george">George (British AI)</option>
                  <option value="kokoro:am_adam">Adam (Assertive American)</option>
                  <option value="kokoro:am_michael">Michael (Baseline — Fastest)</option>
                </optgroup>
                <optgroup label="ElevenLabs Flash v2.5 Studio HD (~350 ms · 0 ms Cached)">
                  <option value="elevenlabs:pNInz6obpgDQGcFmaJgB">Adam Studio (Dominant, Firm)</option>
                  <option value="elevenlabs:nPczCjzI2devNBz1zQrb">Brian Studio (Deep Broadcast)</option>
                  <option value="elevenlabs:SOYHLrjzK2X1ezoPC6cr">Harry Studio (Fierce Warrior)</option>
                  <option value="elevenlabs:pqHfZKP75CvOlQylNhV4">Bill Studio (Authoritative Baritone)</option>
                  <option value="elevenlabs:IKne3meq5aSn9XLyUdCD">Charlie Studio (Deep Confident)</option>
                  <option value="elevenlabs:JBFqnCBsd6RMkjVDRZzb">George Studio (Warm British)</option>
                  <option value="elevenlabs:onwK4e9ZLuTAKqWW03F9">Daniel Studio (Steady Broadcaster)</option>
                </optgroup>
                <optgroup label="Microsoft Cloud Neural">
                  <option value="en-US-AndrewMultilingualNeural">Andrew (Cloud Neural)</option>
                  <option value="en-US-AvaMultilingualNeural">Ava (Cloud Neural Female)</option>
                </optgroup>
              </select>
            </div>
            <div>
              <label id="vxModelLbl">Whisper Model</label>
              <select id="vxModel"><option value="base.en">base.en (~1.2s, balanced)</option><option value="tiny.en">tiny.en (fastest)</option><option value="small.en">small.en (accurate)</option><option value="medium.en">medium.en (deepest)</option></select>
            </div>
            <div>
              <label>VAD Threshold (0.15–0.90)</label>
              <input id="vxVad" type="number" min="0.15" max="0.90" step="0.05" value="0.45">
            </div>
          </div>
          <div class="ctrl-row" style="margin-top:10px">
            <button class="btn" id="vxSaveBtn" type="button">Save Voice</button>
            <a class="btn ok" id="vxAuditionLink" href="/voice-auditions" target="_blank" style="text-decoration:none">🎧 Audition Atlas Voices</a>
            <button class="btn sec" id="vxTestBtn" type="button">Test Mic (60s)</button>
            <button class="btn sec" id="vxSpeakBtn" type="button">Test Speaker</button>
            <button class="btn sec" id="vxInjectBtn" type="button">Send &ldquo;Armory check&rdquo;</button>
            <span id="vxOut" style="color:var(--mut);font-size:12px"></span>
          </div>
          <div id="vxRecent" style="margin-top:10px;color:var(--mut);font-size:12px">No voice utterances captured yet.</div>
          <details id="vxAbBox" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line);color:var(--mut);font-size:12px">
            <summary style="cursor:pointer;font-weight:800;font-size:13px;color:var(--fg)">Dual-Mic A/B Metrics (PowerConf S330 vs. Pixel Phone Mic)</summary>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:8px 0 6px">
              <div id="vxAbSummary">Enable Dual-Mic A/B Benchmark or run the 10-Phrase Shootout in Armory Fit to compare catch % and improvement.</div>
              <button class="btn sec sm" id="vxAbResetBtn" type="button">Reset Passive A/B</button>
            </div>
            <div id="vxAbRuns" style="margin-top:8px"></div>
            <div id="vxAbTrials" style="margin-top:8px"></div>
          </details>
        </div>

        <div class="card" id="wakeStudioCard">
          <h2>
            <span>Custom Wake Word Studio (S330 Voice Enrollment &amp; ONNX Trainer)</span>
            <span id="wsStatusPill" class="pill">READY</span>
          </h2>
          <div style="color:var(--mut);font-size:12px;margin-bottom:10px">Record ~20 natural reps of your voice through the PowerConf S330 across gym positions, then train &amp; hot-deploy your personalized ONNX wake model in ~45s.</div>
          <div id="wsWordPills" class="ws-pills"></div>
          <div id="wsWordMeta" style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mut);padding:8px 10px;background:#0e151f;border:1px solid var(--line);border-radius:9px">
            <span id="wsMetricsTxt">Loading wake model telemetry…</span>
            <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
              <button class="btn ok sm" id="wsActivateBtn" type="button">Set Active Wake Word</button>
              <button class="btn sec sm" id="wsRestoreStockBtn" type="button" hidden>Restore Stock Jarvis</button>
            </div>
          </div>
          <div id="wsPromptBox" class="ws-prompt">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
              <strong id="wsPromptTitle" style="font-size:14px">Guided 20-Rep Hands-Free Capture</strong>
              <span id="wsRepCounter" class="pill">0 / 20 reps</span>
            </div>
            <div id="wsPromptCue" style="font-size:13px;color:#e2e8f0;margin-top:4px">Pick a wake word above and click Start Guided Capture.</div>
            <div class="mbar-wrap" style="margin:8px 0 4px"><div id="wsMeter" class="mbar-fill"></div></div>
            <div id="wsLastEvent" style="font-size:11.5px;color:var(--mut)">Say only the wake phrase after each chime — training mixes in gym rock &amp; command tails automatically.</div>
          </div>
          <div class="ctrl-row" style="margin-top:8px">
            <button class="btn" id="wsEnrollBtn" type="button">🎙️ Start Guided Capture (20 Reps)</button>
            <button class="btn sec" id="wsDelLastBtn" type="button">↶ Delete Last</button>
            <button class="btn ok" id="wsTrainBtn" type="button">⚡ Train &amp; Deploy Model</button>
            <label style="display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:var(--mut);margin:0;cursor:pointer">
              <input id="wsForceFull" type="checkbox" style="width:auto"> Force Full Synth
            </label>
            <button class="btn red sm" id="wsClearBtn" type="button">Clear All Reps</button>
          </div>
          <div id="wsTrainBox" style="margin-top:10px" hidden>
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px">
              <strong id="wsTrainStage">Idle</strong>
              <span id="wsTrainPct" style="font-variant-numeric:tabular-nums">0%</span>
            </div>
            <div class="ws-prog-wrap"><div id="wsTrainBar" class="ws-prog-bar"></div></div>
            <pre id="wsTrainLog" class="ws-log"></pre>
          </div>
          <div style="margin-top:10px">
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--mut)">
              <strong id="wsClipsHdr" style="color:var(--fg)">Recorded Voice Clips (0)</strong>
              <span>16 kHz mono WAV · saved in S:\\ArmoryCam\\oww_training\\</span>
            </div>
            <div id="wsClipsList" class="ws-clips">
              <div style="padding:10px;font-size:12px;color:var(--mut)">No personal S330 clips recorded yet for this phrase.</div>
            </div>
          </div>
          <audio id="wsClipAudio" hidden></audio>
        </div>
      </section>

    </div>
  </div>
</div>

<div id="maskModal" class="mask-modal" hidden>
  <div class="mm-top">
    <div class="mm-title">
      <button class="btn sec sm" id="mmPrevView" type="button" title="Previous view ([)">◀</button>
      <select id="mmViewSel" class="mm-view-sel" aria-label="Select camera view"></select>
      <button class="btn sec sm" id="mmNextView" type="button" title="Next view (])">▶</button>
      <span id="mmTitle" hidden>View Calibration Studio</span>
      <div class="mm-mbar">
        <div class="mm-mbar-track"><div id="mmMotionBar" class="mm-mbar-fill"></div></div>
        <span id="mmMotionTxt">Motion: 0.0</span>
      </div>
    </div>
    <div class="mm-bar-grp">
      <button class="btn sec sm on" id="mmModeCrop" type="button">Crop &amp; Level (Blue)</button>
      <button class="btn sec sm" id="mmModeMask" type="button">Mirror Masks (Red)</button>
      <button class="btn sec sm" id="mmPreviewBtn" type="button" title="Toggle cropped preview (P)">Preview Cropped</button>
    </div>
    <div class="ctrl-row" style="margin-top:0">
      <button class="btn sec sm" id="mmHiresBtn" type="button">1080p Still</button>
      <button class="btn sec sm" id="mmRefreshBtn" type="button" hidden>↻ Grab Frame</button>
      <button class="btn ok sm" id="mmSaveBtn" type="button">Save View</button>
      <button class="btn sec sm" id="mmCloseBtn" type="button">Done</button>
    </div>
  </div>
  <div class="mm-zoombar">
    <div class="mm-bar-grp">
      <button class="btn sec sm" id="mmRotBtn" type="button">Rotate 0°</button>
      <button class="btn sec sm" id="mmTiltMinus" type="button" title="Rotate counter-clockwise 0.5°">−0.5°</button>
      <input type="range" id="mmTiltRange" min="-45" max="45" step="0.5" value="0" aria-label="Full-screen custom angle slider">
      <button class="btn sec sm" id="mmTiltPlus" type="button" title="Rotate clockwise 0.5°">+0.5°</button>
      <input type="number" class="tilt-num" id="mmTiltNum" min="-45" max="45" step="0.1" value="0.0" aria-label="Full-screen rotate angle">
      <button class="btn sec sm" id="mmTiltReset" type="button" title="Reset angle to 0°">0°</button>
      <button class="btn sec sm" id="mmGridBtn" type="button" title="Toggle plumb alignment grid (G)">Grid</button>
    </div>
    <div class="mm-sep"></div>
    <div class="mm-bar-grp" id="mmCropTools">
      <span style="font-size:12px;color:var(--mut)">Ratio:</span>
      <button class="btn sec sm on" type="button" data-mmasp="free">Free</button>
      <button class="btn sec sm" type="button" data-mmasp="1.7778">16:9</button>
      <button class="btn sec sm" type="button" data-mmasp="0.5625">9:16</button>
      <button class="btn sec sm" type="button" id="mmResetCropBtn">Reset Crop</button>
    </div>
    <div class="mm-bar-grp" id="mmMaskTools" hidden>
      <div id="mmMaskTabs" class="mm-tabs"></div>
      <button class="btn red sm" id="mmAddBtn" type="button">+ Add Mask</button>
      <button class="btn sec sm" id="mmClearBtn" type="button">Clear All</button>
    </div>
    <div class="mm-sep"></div>
    <div class="mm-bar-grp">
      <span style="font-size:12px;color:var(--mut)">Zoom:</span>
      <button class="btn sec sm" type="button" data-mmz="1">1×</button>
      <button class="btn sec sm" type="button" data-mmz="2">2×</button>
      <button class="btn sec sm" type="button" data-mmz="3">3×</button>
      <button class="btn sec sm" type="button" data-mmz="5">5×</button>
      <button class="btn sec sm" type="button" data-mmz="8">8×</button>
      <button class="btn sec sm" type="button" id="mmFitCropBtn">Fit Crop</button>
      <button class="btn sec sm" type="button" id="mmFitMaskBtn">Fit Mask</button>
      <input type="range" id="mmZoomRange" min="1" max="8" step="0.1" value="1">
      <span id="mmZoomVal" style="font-size:12px;font-weight:800;min-width:38px;font-variant-numeric:tabular-nums">1.0×</span>
    </div>
  </div>
  <div id="mmViewport" class="mm-viewport">
    <div id="mmCanvas" class="mm-canvas">
      <canvas id="mmLiveCanvas" class="mm-img"></canvas>
      <img id="mmStream" class="mm-img" alt="Full-screen calibration view" hidden>
      <div id="mmLevelGrid" class="level-grid"></div>
      <div class="scrim" id="mmScT"></div>
      <div class="scrim" id="mmScB"></div>
      <div class="scrim" id="mmScL"></div>
      <div class="scrim" id="mmScR"></div>
      <div id="mmCropRef" class="crop-box">
        <span class="box-tag">CROP</span>
        <div class="handle nw" data-handle="nw"></div>
        <div class="handle ne" data-handle="ne"></div>
        <div class="handle sw" data-handle="sw"></div>
        <div class="handle se" data-handle="se"></div>
      </div>
      <div id="mmExclWrap" class="excl-wrap">
        <svg id="mmSvg" class="excl-svg" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>
        <div id="mmDots"></div>
      </div>
    </div>
  </div>
  <div class="mm-foot">
    <span id="mmInfo"></span>
    <span id="mmHint">Drag blue crop handles to frame · Angle slider + Grid to level · Switch views via dropdown or [ / ]</span>
  </div>
</div>

<div id="realignModal" class="realign-modal" hidden>
  <div class="ra-top">
    <div class="ra-top-l">
      <select id="raCamSel" class="mm-view-sel" aria-label="Select physical camera to realign"></select>
      <span id="raStatusPill" class="ra-pill">CONNECTING</span>
    </div>
    <div class="ra-top-r">
      <button class="btn sec sm on" id="raChimeBtn" type="button" title="Play a pleasant chime when camera locks into green">🔊 Chime</button>
      <button class="btn sec sm" id="raCloseBtn" type="button">Done</button>
    </div>
  </div>
  <div class="ra-stage-wrap">
    <div id="raStage" class="ra-stage">
      <canvas id="raLiveCanvas" class="ra-media"></canvas>
      <img id="raOverlayImg" class="ra-media ra-overlay" alt="Saved landmark overlay">
      <svg id="raCropsSvg" class="ra-media ra-crops" viewBox="0 0 1600 900" preserveAspectRatio="none"></svg>
      <div id="raArrUp" class="ra-edge up">▲ TILT UP</div>
      <div id="raArrDown" class="ra-edge down">▼ TILT DOWN</div>
      <div id="raArrLeft" class="ra-edge left">◀ PAN LEFT</div>
      <div id="raArrRight" class="ra-edge right">▶ PAN RIGHT</div>
      <div id="raArrRoll" class="ra-edge roll">↺ ROLL CCW</div>
      <div id="raLockBadge" class="ra-banner-badge lock" hidden>✓ LOCKED</div>
      <div id="raHoldBadge" class="ra-banner-badge hold" hidden>✋ Step back / hold steady…</div>
    </div>
  </div>
  <div class="ra-guide-card">
    <div id="raGuideChips" class="ra-guide-chips">
      <span class="ra-chip hold">Measuring alignment…</span>
    </div>
    <div id="raMetrics" class="ra-metrics">Waiting for live frame…</div>
  </div>
  <div class="ra-ctrls">
    <div class="ra-ctrl-grp">
      <button class="btn sec sm on" id="raModeEdges" type="button" data-ramode="edges">Wireframe</button>
      <button class="btn sec sm" id="raModeGhost" type="button" data-ramode="ghost">Ghost</button>
      <button class="btn sec sm" id="raModeOff" type="button" data-ramode="off">Off</button>
    </div>
    <div class="ra-ctrl-grp" id="raColorGrp">
      <button class="ra-swatch on" type="button" data-racolor="cyan" style="background:#38eef8" title="Cyan wireframe" aria-label="Cyan"></button>
      <button class="ra-swatch" type="button" data-racolor="lime" style="background:#5cff69" title="Lime wireframe" aria-label="Lime"></button>
      <button class="ra-swatch" type="button" data-racolor="magenta" style="background:#ff5af5" title="Magenta wireframe" aria-label="Magenta"></button>
    </div>
    <div class="ra-ctrl-grp">
      <span style="font-size:12px;color:var(--mut)">Opacity</span>
      <input type="range" id="raOpacity" min="15" max="100" step="5" value="85" aria-label="Overlay opacity">
    </div>
    <div class="ra-ctrl-grp">
      <button class="btn sec sm" id="raBlinkBtn" type="button" title="Flash overlay at 1 Hz to compare">Blink</button>
      <button class="btn sec sm on" id="raCropsBtn" type="button" title="Show virtual view crops and mirror exclusion masks">Crops &amp; Masks</button>
    </div>
    <div class="ra-ctrl-grp" style="margin-left:auto">
      <button class="btn ok sm" id="raAdaptBtn" type="button" title="Automatically shift all crops, tilts, and mirror masks to match this camera position and save new baseline">✨ Auto-Shift Crops &amp; Masks</button>
      <button class="btn sec sm" id="raAcceptBtn" type="button" title="Save current camera position as the new alignment baseline without moving crops/masks">Accept Only</button>
    </div>
  </div>
</div>

<div id="rawFeeds" hidden></div>
<div id="toast" role="status" aria-live="polite"></div>

<script>
const $ = (id) => document.getElementById(id);
let cfg = null;
const previewCropped = {};
const aspectLock = {};
const editTarget = {};
let isDirty = false;
let toastTimer = null;
const btnTimers = new WeakMap();
let activePage = 'overview';
let latestStatusData = null;
let latestAudioData = null;
let latestClipRows = [];
let latestShowActive = { active: false };
let latestShowsList = [];

const tsState = {
  loaded: false,
  sourceFilter: 'all',
  days: [],
  live: null,
  selectedDayKey: '',
  sessionId: '',
  isLive: false,
  camera: 'Rack Cam',
  cameras: [],
  timeline: null,
  trackSegs: [],
  collapseGaps: false,
  manualGapModeKey: '',
  zoomMode: 'fit',
  viewStartFrac: 0,
  viewEndFrac: 1,
  curMs: 0,
  inMs: null,
  outMs: null,
  playing: false,
  buffering: false,
  playGen: 0,
  streamId: 'web_' + Math.random().toString(36).slice(2, 10),
  streamStartMs: 0,
  streamEndMs: 0,
  speed: 1,
  motionOnly: false,
  quietSinceMs: 0,
  skipCooldownUntil: 0,
  skipToastTimer: null,
  audioOn: false,
  audioChunkStart: 0,
  audioChunkEnd: 0,
  rafId: 0,
  dragMode: '',
  wasPlayingOnDrag: false,
  panStartX: 0,
  panStartFrac0: 0,
  hoverFrac: null,
  hoverMs: null,
  sensitivity: 1.5,
  minDurSec: 5,
  allCamsEvents: false,
  events: [],
  packCache: new Map(),
  activePack: [],
  packAbort: null,
  packTimer: null,
  frameTimer: null,
  frameAbort: null,
  frameObjUrl: '',
  scanPollTimer: null,
  evObserver: null,
  tapesAbort: null,
  tapesReqSeq: 0,
};

const mmState = {
  open: false,
  camIdx: 0,
  mode: 'crop',
  previewCropped: false,
  gridPinned: false,
  leveling: false,
  activeMask: 0,
  zoom: 1,
  panX: 0,
  panY: 0,
  hires: false,
  pendingFit: false,
  dragDot: null,
};
let mmHiresTimer = null;
let mmLevelTimer = null;

const raState = {
  open: false,
  slug: '',
  mode: 'edges',
  color: 'cyan',
  opacity: 0.85,
  blink: false,
  showCrops: true,
  chimeOn: true,
  wasLocked: false,
  lastChimeAt: 0,
  missStreak: 0,
  lastGood: null,
  pollTimer: null,
  blinkTimer: null,
  pollAbort: null,
  pollBusy: false,
  audioCtx: null,
};

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const MIN_SPAN = 0.15;
const MAX_EXCLUSIONS = 6;
const MM_MIN_ZOOM = 1;
const MM_MAX_ZOOM = 8;
const MM_PAN_MARGIN = 56;
const rawFeedUrls = {};

function makeUniqueSlug(rawName) {
  const base = (String(rawName || 'view').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^[._-]+|[._-]+$/g, '')).slice(0, 40) || 'view';
  const taken = new Set((cfg?.cameras || []).map(c => c.slug));
  if (!taken.has(base)) return base;
  let k = 2;
  while (taken.has(`${base}_${k}`)) k += 1;
  return `${base}_${k}`;
}

function sourceSlugOf(cam) {
  return (cam && cam.sourceSlug) ? cam.sourceSlug : (cam ? cam.slug : '');
}

function syncRawFeeds() {
  const box = $('rawFeeds');
  if (!box || !cfg) return;
  const wantFeeds = !document.hidden && (activePage === 'cameras' || (mmState && mmState.open) || (raState && raState.open));
  const activePhys = new Map();
  if (wantFeeds) {
    for (const c of cfg.cameras) {
      if (!c.sourceSlug && c.rtspUrl) {
        const hasEnabledView = cfg.cameras.some(v => sourceSlugOf(v) === c.slug && v.enabled);
        if (hasEnabledView) activePhys.set(c.slug, c);
      }
    }
  }
  Array.from(box.children).forEach(el => {
    const slug = el.dataset.rawSlug;
    const phys = activePhys.get(slug);
    if (!phys || rawFeedUrls[slug] !== phys.rtspUrl) {
      el.removeAttribute('src');
      el.remove();
      delete rawFeedUrls[slug];
    }
  });
  for (const [slug, phys] of activePhys.entries()) {
    if ($(`rawFeed-${slug}`)) continue;
    const img = document.createElement('img');
    img.id = `rawFeed-${slug}`;
    img.dataset.rawSlug = slug;
    rawFeedUrls[slug] = phys.rtspUrl;
    const setSrc = () => {
      img.src = `/api/stream?camera=${encodeURIComponent(phys.name)}&raw=1&t=${Date.now()}`;
    };
    img.onload = () => {
      img._sized = true;
      if (cfg && cfg.cameras) {
        cfg.cameras.forEach((cam, idx) => {
          if (sourceSlugOf(cam) === slug) syncCropOverlay(idx);
        });
      }
    };
    img.onerror = () => setTimeout(() => {
      if (img.isConnected && rawFeedUrls[slug]) setSrc();
    }, 2500);
    setSrc();
    box.appendChild(img);
  }
}

function normTilt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  const c = clamp(Math.round(n * 10) / 10, -45, 45);
  return Math.abs(c) < 0.05 ? 0 : c;
}

function tiltedSize(w, h, tilt) {
  const t = normTilt(tilt);
  if (t === 0) return [w, h];
  const rad = t * Math.PI / 180;
  const ca = Math.abs(Math.cos(rad));
  const sa = Math.abs(Math.sin(rad));
  const nw = Math.max(2, Math.floor((w * ca + h * sa) / 2) * 2);
  const nh = Math.max(2, Math.floor((w * sa + h * ca) / 2) * 2);
  return [nw, nh];
}

function viewFullDims(cam) {
  const rot = cam ? (cam.rotate || 0) : 0;
  const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
  const sw = (rawImg && rawImg.naturalWidth > 0) ? rawImg.naturalWidth : 1920;
  const sh = (rawImg && rawImg.naturalHeight > 0) ? rawImg.naturalHeight : 1080;
  const rw = (rot === 90 || rot === 270) ? sh : sw;
  const rh = (rot === 90 || rot === 270) ? sw : sh;
  return tiltedSize(rw, rh, cam ? (cam.tilt || 0) : 0);
}

function drawFeedToCanvas(canvas, rawImg, rot, tilt, crop, applyCrop) {
  if (!canvas || !rawImg || !rawImg.complete || rawImg.naturalWidth <= 0 || rawImg.naturalHeight <= 0) return;
  const sw = rawImg.naturalWidth;
  const sh = rawImg.naturalHeight;
  const r = ((rot % 360) + 360) % 360;
  const t = normTilt(tilt);
  const rw = (r === 90 || r === 270) ? sh : sw;
  const rh = (r === 90 || r === 270) ? sw : sh;
  const [fw, fh] = tiltedSize(rw, rh, t);
  let sx = 0, sy = 0, cw = fw, ch = fh;
  if (applyCrop && Array.isArray(crop) && crop.length === 4) {
    sx = clamp(crop[0], 0, 0.95) * fw;
    sy = clamp(crop[1], 0, 0.95) * fh;
    cw = Math.max(2, (clamp(crop[2], 0.05, 1) - clamp(crop[0], 0, 0.95)) * fw);
    ch = Math.max(2, (clamp(crop[3], 0.05, 1) - clamp(crop[1], 0, 0.95)) * fh);
  }
  const tw = Math.max(2, Math.round(cw));
  const th = Math.max(2, Math.round(ch));
  if (canvas.width !== tw) canvas.width = tw;
  if (canvas.height !== th) canvas.height = th;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.save();
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, tw, th);
  ctx.translate(-sx, -sy);
  if (t !== 0) {
    ctx.translate(fw / 2, fh / 2);
    ctx.rotate(t * Math.PI / 180);
    ctx.translate(-rw / 2, -rh / 2);
  }
  if (r === 90) {
    ctx.translate(rw, 0);
    ctx.rotate(Math.PI / 2);
  } else if (r === 180) {
    ctx.translate(rw, rh);
    ctx.rotate(Math.PI);
  } else if (r === 270) {
    ctx.translate(0, rh);
    ctx.rotate(-Math.PI / 2);
  }
  ctx.drawImage(rawImg, 0, 0, sw, sh);
  ctx.restore();
}

function drawAllCanvases() {
  if (document.hidden) return;
  if (typeof activePage !== 'undefined' && activePage !== 'cameras' && !mmState.open && !raState.open) return;
  if (!cfg || !cfg.cameras) return;
  cfg.cameras.forEach((c, idx) => {
    const rawImg = $(`rawFeed-${sourceSlugOf(c)}`);
    if (rawImg && !rawImg._sized && rawImg.naturalWidth > 0) {
      rawImg._sized = true;
      syncCropOverlay(idx);
    }
    const canvas = $(`stream-${idx}`);
    if (canvas && rawImg) {
      drawFeedToCanvas(canvas, rawImg, c.rotate || 0, c.tilt || 0, c.crop, !!previewCropped[idx]);
    }
  });
  if (raState.open && raState.slug) {
    const rawImg = $(`rawFeed-${raState.slug}`);
    const raCanvas = $('raLiveCanvas');
    if (rawImg && raCanvas) {
      drawFeedToCanvas(raCanvas, rawImg, 0, 0, [0, 0, 1, 1], false);
    }
  }
  if (mmState.open && !mmState.hires) {
    const cam = cfg.cameras[mmState.camIdx];
    const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
    const mmCanvas = $('mmLiveCanvas');
    if (cam && rawImg && mmCanvas) {
      if (mmState.pendingFit && rawImg.naturalWidth > 0) {
        mmState.pendingFit = false;
        if (mmState.mode === 'mask' && (cam.exclusions || []).length) {
          fitMaskInModal();
        } else {
          mmState.zoom = 1;
          clampModalPan();
          renderMaskModal();
        }
      }
      drawFeedToCanvas(
        mmCanvas,
        rawImg,
        cam.rotate || 0,
        cam.tilt || 0,
        mmState.previewCropped ? cam.crop : [0, 0, 1, 1],
        !!mmState.previewCropped
      );
    }
  }
}

function normQuad(item) {
  if (!Array.isArray(item) || item.length !== 4) return null;
  if (item.every(v => typeof v === 'number' && Number.isFinite(v))) {
    const x0 = clamp(Math.min(item[0], item[2]), 0, 1);
    const y0 = clamp(Math.min(item[1], item[3]), 0, 1);
    const x1 = clamp(Math.max(item[0], item[2]), 0, 1);
    const y1 = clamp(Math.max(item[1], item[3]), 0, 1);
    return [[+x0.toFixed(4), +y0.toFixed(4)], [+x1.toFixed(4), +y0.toFixed(4)], [+x1.toFixed(4), +y1.toFixed(4)], [+x0.toFixed(4), +y1.toFixed(4)]];
  }
  if (item.every(pt => Array.isArray(pt) && pt.length === 2 && Number.isFinite(Number(pt[0])) && Number.isFinite(Number(pt[1])))) {
    return item.map(pt => [+clamp(Number(pt[0]), 0, 1).toFixed(4), +clamp(Number(pt[1]), 0, 1).toFixed(4)]);
  }
  return null;
}

function defaultQuadFor(cam, k, viewNorm = null) {
  const b = viewNorm || (cam && Array.isArray(cam.crop) ? cam.crop : [0, 0, 1, 1]);
  const bw = Math.max(0.12, b[2] - b[0]);
  const bh = Math.max(0.12, b[3] - b[1]);
  const qw = Math.min(0.32, bw * 0.34);
  const qh = Math.min(0.42, bh * 0.44);
  const sx = (k % 3) * (bw * 0.08);
  const sy = Math.floor(k / 3) * (bh * 0.09);
  const x0 = clamp(b[0] + bw * 0.52 - sx, 0.01, 0.99 - qw);
  const y0 = clamp(b[1] + bh * 0.16 + sy, 0.01, 0.99 - qh);
  const x1 = +(x0 + qw).toFixed(4);
  const y1 = +(y0 + qh).toFixed(4);
  return [[+x0.toFixed(4), +y0.toFixed(4)], [x1, +y0.toFixed(4)], [x1, y1], [+x0.toFixed(4), y1]];
}

function quadBounds(q) {
  const xs = q.map(p => p[0]);
  const ys = q.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function quadCentroid(q) {
  return [
    (q[0][0] + q[1][0] + q[2][0] + q[3][0]) / 4,
    (q[0][1] + q[1][1] + q[2][1] + q[3][1]) / 4,
  ];
}

function segsCross(a, b, c, d) {
  const ccw = (p1, p2, p3) => (p3[1] - p1[1]) * (p2[0] - p1[0]) - (p2[1] - p1[1]) * (p3[0] - p1[0]);
  const d1 = ccw(c, d, a), d2 = ccw(c, d, b), d3 = ccw(a, b, c), d4 = ccw(a, b, d);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function quadHasCrossing(q) {
  if (!q || q.length !== 4) return false;
  return segsCross(q[0], q[1], q[2], q[3]) || segsCross(q[1], q[2], q[3], q[0]);
}

function showToast(msg, kind = 'ok') {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'show ' + kind;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 2400);
}

function allSaveButtons() {
  return [$('saveAllTop'), $('saveBtn'), $('wbSaveBtn'), $('mmSaveBtn'), ...document.querySelectorAll('[data-save]')].filter(Boolean);
}

function markDirty(flag = true) {
  isDirty = flag;
  allSaveButtons().forEach(b => b.classList.toggle('dirty', flag && !b.classList.contains('saved')));
}

function setBtnFeedback(btn, state, label, restoreMs = 0) {
  if (!btn) return;
  if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent;
  const oldT = btnTimers.get(btn);
  if (oldT) { clearTimeout(oldT); btnTimers.delete(btn); }
  btn.classList.remove('saving', 'saved', 'save-err');
  if (state) btn.classList.add(state);
  btn.textContent = label || btn.dataset.origLabel;
  btn.disabled = (state === 'saving');
  if (restoreMs > 0) {
    const tid = setTimeout(() => {
      btn.classList.remove('saving', 'saved', 'save-err');
      btn.textContent = btn.dataset.origLabel;
      btn.disabled = false;
      btn.classList.toggle('dirty', isDirty);
      btnTimers.delete(btn);
    }, restoreMs);
    btnTimers.set(btn, tid);
  }
}

function renderQuadLayer(svgEl, dotsEl, excls, opts = {}) {
  if (!svgEl || !dotsEl) return;
  const activeIdx = opts.activeIdx ?? -1;
  const dragDot = opts.dragDot || null;
  const showNums = !!opts.showNums;

  svgEl.innerHTML = excls.map((q, ei) => {
    const pts = q.map(p => `${(p[0] * 1000).toFixed(1)},${(p[1] * 1000).toFixed(1)}`).join(' ');
    const cls = ['quad-poly', activeIdx === ei ? 'active' : '', quadHasCrossing(q) ? 'crossed' : ''].filter(Boolean).join(' ');
    return `<polygon class="${cls}" fill-rule="evenodd" vector-effect="non-scaling-stroke" data-excl-idx="${ei}" points="${pts}"></polygon>`;
  }).join('');

  dotsEl.innerHTML = excls.map((q, ei) => {
    const c = quadCentroid(q);
    const tag = `<div class="quad-tag" style="left:${(c[0] * 100).toFixed(3)}%;top:${(c[1] * 100).toFixed(3)}%">MASK ${ei + 1}</div>`;
    const dots = q.map((p, ci) => {
      const isDrag = dragDot && dragDot.ei === ei && dragDot.ci === ci;
      return `<div class="quad-dot${isDrag ? ' dragging' : ''}" data-excl-idx="${ei}" data-corner-idx="${ci}" style="left:${(p[0] * 100).toFixed(3)}%;top:${(p[1] * 100).toFixed(3)}%"><div class="quad-ring"></div>${showNums ? `<span class="quad-num">${ci + 1}</span>` : ''}</div>`;
    }).join('');
    return tag + dots;
  }).join('');
}

function cropZoomFactor(c) {
  const w = Math.max(MIN_SPAN, c[2] - c[0]);
  const h = Math.max(MIN_SPAN, c[3] - c[1]);
  return Math.max(1 / w, 1 / h);
}

function setZoomAroundCenter(idx, zoom) {
  const c = cfg.cameras[idx].crop;
  const cx = (c[0] + c[2]) / 2;
  const cy = (c[1] + c[3]) / 2;
  const span = clamp(1 / zoom, MIN_SPAN, 1);
  const half = span / 2;
  let x0 = clamp(cx - half, 0, 1 - span);
  let y0 = clamp(cy - half, 0, 1 - span);
  cfg.cameras[idx].crop = [
    +x0.toFixed(4), +y0.toFixed(4), +(x0 + span).toFixed(4), +(y0 + span).toFixed(4)
  ];
  markDirty();
  syncCropOverlay(idx);
}

function applyScrimRects(scT, scB, scL, scR, c) {
  if (!scT || !scB || !scL || !scR) return;
  const lPct = c[0] * 100, tPct = c[1] * 100, wPct = (c[2] - c[0]) * 100, hPct = (c[3] - c[1]) * 100;
  scT.style.cssText = `left:0;top:0;right:0;height:${tPct}%;`;
  scB.style.cssText = `left:0;top:${tPct + hPct}%;right:0;bottom:0;`;
  scL.style.cssText = `left:0;top:${tPct}%;width:${lPct}%;height:${hPct}%;`;
  scR.style.cssText = `left:${lPct + wPct}%;top:${tPct}%;right:0;height:${hPct}%;`;
}

function applyCropDragDelta(camIdx, origCrop, mode, dx, dy, minS = MIN_SPAN) {
  let [x0, y0, x1, y1] = origCrop;
  const ow = x1 - x0, oh = y1 - y0;
  if (mode === 'move') {
    x0 = clamp(x0 + dx, 0, 1 - ow);
    y0 = clamp(y0 + dy, 0, 1 - oh);
    x1 = x0 + ow;
    y1 = y0 + oh;
  } else {
    if (mode.includes('w')) x0 = clamp(x0 + dx, 0, x1 - minS);
    if (mode.includes('e')) x1 = clamp(x1 + dx, x0 + minS, 1);
    if (mode.includes('n')) y0 = clamp(y0 + dy, 0, y1 - minS);
    if (mode.includes('s')) y1 = clamp(y1 + dy, y0 + minS, 1);

    const lock = aspectLock[camIdx];
    if (lock) {
      const [srcW, srcH] = viewFullDims(cfg.cameras[camIdx]);
      const targetNormRatio = lock * (srcH / srcW);
      let nw = x1 - x0;
      let nh = nw / targetNormRatio;
      if (nh < minS) { nh = minS; nw = nh * targetNormRatio; }
      if (mode.includes('n')) {
        y0 = y1 - nh;
        if (y0 < 0) { y0 = 0; nh = y1; nw = nh * targetNormRatio; }
      } else {
        y1 = y0 + nh;
        if (y1 > 1) { y1 = 1; nh = 1 - y0; nw = nh * targetNormRatio; }
      }
      if (mode.includes('w')) x0 = clamp(x1 - nw, 0, x1 - minS);
      else x1 = clamp(x0 + nw, x0 + minS, 1);
    }
  }
  return [+x0.toFixed(4), +y0.toFixed(4), +x1.toFixed(4), +y1.toFixed(4)];
}

function syncCropOverlay(idx) {
  const cam = cfg.cameras[idx];
  const c = cam.crop;
  const rawExcls = Array.isArray(cam.exclusions) ? cam.exclusions : (cam.exclusions = []);
  const excls = rawExcls.map(normQuad).filter(Boolean);
  cam.exclusions = excls;

  let mode = editTarget[idx] || 'crop';
  if (mode === 'excl' && !excls.length) {
    mode = 'crop';
    editTarget[idx] = 'crop';
  }
  const rot = cam.rotate || 0;
  const tilt = normTilt(cam.tilt || 0);
  const stage = $(`stage-${idx}`);
  const layer = $(`layer-${idx}`);
  if (!stage || !layer) return;
  stage.classList.toggle('rot90', rot === 90);
  stage.classList.toggle('rot270', rot === 270);

  const [stageFw, stageFh] = viewFullDims(cam);
  const showEditor = !previewCropped[idx];
  stage.style.aspectRatio = showEditor ? `${stageFw} / ${stageFh}` : '';
  layer.hidden = !showEditor;

  const isFullCrop = c[0] <= 0.001 && c[1] <= 0.001 && c[2] >= 0.999 && c[3] >= 0.999;

  const boxEl = $(`box-${idx}`);
  boxEl.style.cssText = `left:${c[0]*100}%;top:${c[1]*100}%;width:${(c[2]-c[0])*100}%;height:${(c[3]-c[1])*100}%;`;
  boxEl.classList.toggle('passive', mode === 'excl');

  const exclWrap = $(`exclWrap-${idx}`);
  const exclSvg = $(`exclSvg-${idx}`);
  const exclDots = $(`exclDots-${idx}`);
  if (exclWrap && exclSvg && exclDots) {
    exclWrap.classList.toggle('passive', mode !== 'excl');
    renderQuadLayer(exclSvg, exclDots, excls, {
      activeIdx: mode === 'excl' ? (mmState.open && mmState.camIdx === idx ? mmState.activeMask : 0) : -1,
      showNums: false,
    });
  }

  applyScrimRects($(`scT-${idx}`), $(`scB-${idx}`), $(`scL-${idx}`), $(`scR-${idx}`), c);

  const z = Math.min(3.5, Math.max(1.0, cropZoomFactor(c)));
  const slider = $(`zoom-${idx}`);
  if (slider) slider.value = z.toFixed(2);
  const [srcW, srcH] = viewFullDims(cam);
  const outW = Math.round((c[2] - c[0]) * srcW);
  const outH = Math.round((c[3] - c[1]) * srcH);
  const maskLabel = excls.length ? `${excls.length} mirror mask${excls.length === 1 ? '' : 's'} (Red)` : 'No mirror masks';
  const tiltTag = tilt !== 0 ? `${tilt > 0 ? '+' : ''}${tilt.toFixed(1)}°` : '';
  const rotLabel = tilt === 0 ? `Rotate ${rot}°` : (rot === 0 ? `Angle ${tiltTag}` : `Rotate ${rot}° (${tiltTag})`);
  $(`cropInfo-${idx}`).textContent = isFullCrop
    ? `Full frame · ${srcW}×${srcH} · ${rotLabel} · ${maskLabel}`
    : `Zoom ${z.toFixed(2)}× · ~${outW}×${outH} · ${rotLabel} · ${maskLabel}`;
  const lock = aspectLock[idx] || null;
  document.querySelectorAll(`[data-asp^="${idx}:"]`).forEach(x => {
    const r = x.dataset.asp.split(':')[1];
    x.classList.toggle('on', r === 'free' ? !lock : Math.abs(Number(r) - (lock || 0)) < 0.01);
  });
  const tiltSlider = $(`tilt-${idx}`);
  if (tiltSlider) tiltSlider.value = String(tilt);
  const tiltNum = $(`tiltNum-${idx}`);
  if (tiltNum && document.activeElement !== tiltNum) tiltNum.value = tilt.toFixed(1);
  const prevBtn = $(`prevBtn-${idx}`);
  if (prevBtn) prevBtn.classList.toggle('on', !!previewCropped[idx]);
  const rotBtn = $(`rotBtn-${idx}`);
  if (rotBtn) rotBtn.textContent = `Rotate ${rot}°`;
  const mCrop = $(`modeCrop-${idx}`);
  const mExcl = $(`modeExcl-${idx}`);
  const zExcl = $(`zoomExcl-${idx}`);
  const addExcl = $(`addExcl-${idx}`);
  if (mCrop) mCrop.classList.toggle('on', mode === 'crop');
  if (mExcl) {
    mExcl.classList.toggle('on', mode === 'excl');
    mExcl.disabled = !excls.length;
    mExcl.textContent = excls.length ? `Edit Masks (${excls.length})` : 'Edit Masks';
  }
  if (zExcl) zExcl.disabled = !excls.length;
  if (addExcl) addExcl.disabled = excls.length >= MAX_EXCLUSIONS;
}

function wireCropDrag(idx) {
  const layer = $(`layer-${idx}`);
  if (!layer) return;
  let drag = null;

  layer.addEventListener('pointerdown', (e) => {
    const mode = editTarget[idx] || 'crop';
    const dotEl = e.target.closest('.quad-dot');
    const polyEl = e.target.closest('.quad-poly');
    const handleEl = e.target.closest('[data-handle]');
    const cropEl = e.target.closest('.crop-box:not(.passive)');
    if (mode === 'excl' && (dotEl || polyEl)) {
      e.preventDefault();
      const rect = layer.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      layer.setPointerCapture(e.pointerId);
      const ei = Number((dotEl || polyEl).dataset.exclIdx);
      const q = cfg.cameras[idx].exclusions[ei];
      if (!q) return;
      drag = {
        pid: e.pointerId,
        dragKey: dotEl ? 'quad-dot' : 'quad-move',
        exclIdx: ei,
        cornerIdx: dotEl ? Number(dotEl.dataset.cornerIdx) : -1,
        sx: e.clientX,
        sy: e.clientY,
        rect,
        origQuad: q.map(p => [...p]),
      };
      return;
    }
    if (!cropEl) return;
    e.preventDefault();
    const rect = layer.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    layer.setPointerCapture(e.pointerId);
    drag = {
      pid: e.pointerId,
      dragKey: 'crop',
      minSpan: MIN_SPAN,
      mode: handleEl ? handleEl.dataset.handle : 'move',
      sx: e.clientX,
      sy: e.clientY,
      rect,
      orig: [...(cfg.cameras[idx].crop || [0, 0, 1, 1])],
    };
  });

  layer.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.pid) return;
    if (drag.dragKey === 'quad-dot') {
      const nx = +clamp((e.clientX - drag.rect.left) / drag.rect.width, 0, 1).toFixed(4);
      const ny = +clamp((e.clientY - drag.rect.top) / drag.rect.height, 0, 1).toFixed(4);
      cfg.cameras[idx].exclusions[drag.exclIdx][drag.cornerIdx] = [nx, ny];
      markDirty();
      syncCropOverlay(idx);
      return;
    }
    if (drag.dragKey === 'quad-move') {
      const dxRaw = (e.clientX - drag.sx) / drag.rect.width;
      const dyRaw = (e.clientY - drag.sy) / drag.rect.height;
      const [bx0, by0, bx1, by1] = quadBounds(drag.origQuad);
      const dx = clamp(dxRaw, -bx0, 1 - bx1);
      const dy = clamp(dyRaw, -by0, 1 - by1);
      cfg.cameras[idx].exclusions[drag.exclIdx] = drag.origQuad.map(p => [+(p[0] + dx).toFixed(4), +(p[1] + dy).toFixed(4)]);
      markDirty();
      syncCropOverlay(idx);
      return;
    }

    const dx = (e.clientX - drag.sx) / drag.rect.width;
    const dy = (e.clientY - drag.sy) / drag.rect.height;
    cfg.cameras[idx].crop = applyCropDragDelta(idx, drag.orig, drag.mode, dx, dy, drag.minSpan || MIN_SPAN);
    markDirty();
    syncCropOverlay(idx);
    if (mmState.open && mmState.camIdx === idx) renderMaskModal();
  });

  const endDrag = (e) => { if (drag && e.pointerId === drag.pid) drag = null; };
  layer.addEventListener('pointerup', endDrag);
  layer.addEventListener('pointercancel', endDrag);
}

function modalBaseMetrics() {
  const vp = $('mmViewport');
  const img = $('mmStream');
  const cam = cfg?.cameras?.[mmState.camIdx];
  const rot = cam ? (cam.rotate || 0) : 0;
  const tilt = cam ? normTilt(cam.tilt || 0) : 0;
  const rawImg = cam ? $(`rawFeed-${sourceSlugOf(cam)}`) : null;
  const vw = Math.max(120, vp ? vp.clientWidth : 800);
  const vh = Math.max(120, vp ? vp.clientHeight : 500);
  const rawW = (rawImg && rawImg.naturalWidth > 0) ? rawImg.naturalWidth : 1920;
  const rawH = (rawImg && rawImg.naturalHeight > 0) ? rawImg.naturalHeight : 1080;
  const rw = (rot === 90 || rot === 270) ? rawH : rawW;
  const rh = (rot === 90 || rot === 270) ? rawW : rawH;
  const [fw, fh] = tiltedSize(rw, rh, tilt);
  let ratio = fw / fh;
  if (mmState.previewCropped && cam && Array.isArray(cam.crop) && cam.crop.length === 4) {
    const c = cam.crop;
    const cw = Math.max(0.05, c[2] - c[0]) * fw;
    const ch = Math.max(0.05, c[3] - c[1]) * fh;
    ratio = cw / ch;
  }
  if (mmState.hires && img && img.naturalWidth > 0 && img.naturalHeight > 0) {
    ratio = img.naturalWidth / img.naturalHeight;
  }
  const maxW = Math.max(100, vw - 24);
  const maxH = Math.max(100, vh - 24);
  let baseW, baseH;
  if (maxW / maxH > ratio) {
    baseH = maxH;
    baseW = maxH * ratio;
  } else {
    baseW = maxW;
    baseH = maxW / ratio;
  }
  return { vw, vh, baseW, baseH };
}

function clampModalPan() {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const dispW = baseW * mmState.zoom;
  const dispH = baseH * mmState.zoom;
  if (dispW <= vw) mmState.panX = (vw - dispW) / 2;
  else mmState.panX = clamp(mmState.panX, vw - dispW - MM_PAN_MARGIN, MM_PAN_MARGIN);
  if (dispH <= vh) mmState.panY = (vh - dispH) / 2;
  else mmState.panY = clamp(mmState.panY, vh - dispH - MM_PAN_MARGIN, MM_PAN_MARGIN);
}

function setModalZoom(nextZoom, anchorClientX = null, anchorClientY = null) {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const oldDispW = baseW * mmState.zoom;
  const oldDispH = baseH * mmState.zoom;
  const vp = $('mmViewport');
  let ax = vw / 2, ay = vh / 2;
  if (vp && anchorClientX !== null && anchorClientY !== null) {
    const r = vp.getBoundingClientRect();
    ax = anchorClientX - r.left;
    ay = anchorClientY - r.top;
  }
  const ux = (ax - mmState.panX) / Math.max(1, oldDispW);
  const uy = (ay - mmState.panY) / Math.max(1, oldDispH);
  mmState.zoom = clamp(nextZoom, MM_MIN_ZOOM, MM_MAX_ZOOM);
  const newDispW = baseW * mmState.zoom;
  const newDispH = baseH * mmState.zoom;
  mmState.panX = ax - ux * newDispW;
  mmState.panY = ay - uy * newDispH;
  clampModalPan();
  renderMaskModal();
}

function centerModalOnNormBox(x0, y0, x1, y1, fillFraction = 0.62, minZ = 1.8, maxZ = 6.5) {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const bw = Math.max(0.05, x1 - x0);
  const bh = Math.max(0.05, y1 - y0);
  const zx = (vw * fillFraction) / (baseW * bw);
  const zy = (vh * fillFraction) / (baseH * bh);
  mmState.zoom = clamp(Math.min(zx, zy), minZ, maxZ);
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  mmState.panX = vw / 2 - cx * baseW * mmState.zoom;
  mmState.panY = vh / 2 - cy * baseH * mmState.zoom;
  clampModalPan();
  renderMaskModal();
}

function fitMaskInModal() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const excls = cam?.exclusions || [];
  if (!excls.length) {
    fitCropInModal();
    return;
  }
  const ei = clamp(mmState.activeMask, 0, excls.length - 1);
  mmState.activeMask = ei;
  const [x0, y0, x1, y1] = quadBounds(excls[ei]);
  centerModalOnNormBox(x0, y0, x1, y1, 0.58, 2.0, 6.5);
}

function fitCropInModal() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const c = cam?.crop || [0, 0, 1, 1];
  centerModalOnNormBox(c[0], c[1], c[2], c[3], 0.86, 1.0, 6.0);
}

function modalVisibleNormBox() {
  const { vw, vh, baseW, baseH } = modalBaseMetrics();
  const dispW = Math.max(1, baseW * mmState.zoom);
  const dispH = Math.max(1, baseH * mmState.zoom);
  const x0 = clamp((0 - mmState.panX) / dispW, 0, 0.9);
  const y0 = clamp((0 - mmState.panY) / dispH, 0, 0.9);
  const x1 = clamp((vw - mmState.panX) / dispW, x0 + 0.1, 1);
  const y1 = clamp((vh - mmState.panY) / dispH, y0 + 0.1, 1);
  return [x0, y0, x1, y1];
}

function scheduleModalHiresRefresh(immediate = false) {
  if (!mmState.open || !mmState.hires) return;
  if (mmHiresTimer) { clearTimeout(mmHiresTimer); mmHiresTimer = null; }
  if (immediate) {
    updateModalMediaSrc();
    return;
  }
  mmHiresTimer = setTimeout(() => {
    mmHiresTimer = null;
    if (mmState.open && mmState.hires) updateModalMediaSrc();
  }, 180);
}

function updateModalMediaSrc() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  const img = $('mmStream');
  const liveCvs = $('mmLiveCanvas');
  if (!cam || !img || !liveCvs) return;
  if (mmState.hires) {
    liveCvs.hidden = true;
    img.hidden = false;
    const rawFlag = mmState.previewCropped ? '0' : '1';
    const cropQ = mmState.previewCropped
      ? '&crop=' + (cam.crop || [0, 0, 1, 1]).map(v => Number(v).toFixed(4)).join(',')
      : '';
    const defishQ = `&defish=${cam.defish !== false ? '1' : '0'}`;
    img.src = `/api/snapshot?camera=${encodeURIComponent(cam.name)}&raw=${rawFlag}&hires=1&rotate=${cam.rotate || 0}&tilt=${normTilt(cam.tilt || 0)}${cropQ}${defishQ}&t=${Date.now()}`;
  } else {
    img.hidden = true;
    img.removeAttribute('src');
    liveCvs.hidden = false;
    drawAllCanvases();
  }
  const hBtn = $('mmHiresBtn');
  const rBtn = $('mmRefreshBtn');
  if (hBtn) {
    hBtn.classList.toggle('on', mmState.hires);
    hBtn.textContent = mmState.hires ? '▶ Live Stream' : '1080p Still';
  }
  if (rBtn) rBtn.hidden = !mmState.hires;
}

function populateModalViewSelect() {
  const sel = $('mmViewSel');
  if (!sel || !cfg || !Array.isArray(cfg.cameras)) return;
  const bySlug = new Map(cfg.cameras.map(x => [x.slug, x]));
  sel.innerHTML = cfg.cameras.map((c, i) => {
    const p = c.sourceSlug ? bySlug.get(c.sourceSlug) : null;
    const label = p ? `${c.name} (via ${p.name})` : c.name;
    return `<option value="${i}">${esc(label)}</option>`;
  }).join('');
  sel.value = String(clamp(mmState.camIdx, 0, Math.max(0, cfg.cameras.length - 1)));
}

function renderMaskModal() {
  if (!mmState.open || !cfg) return;
  const cam = cfg.cameras[mmState.camIdx];
  if (!cam) return;
  const excls = cam.exclusions || [];
  if (mmState.activeMask >= excls.length) mmState.activeMask = Math.max(0, excls.length - 1);

  populateModalViewSelect();
  $('mmTitle').textContent = `View Calibration · ${cam.name}`;
  const isMaskMode = mmState.mode === 'mask';
  const prevOn = !!mmState.previewCropped;
  if ($('mmModeCrop')) $('mmModeCrop').classList.toggle('on', !isMaskMode);
  if ($('mmModeMask')) $('mmModeMask').classList.toggle('on', isMaskMode);
  if ($('mmPreviewBtn')) $('mmPreviewBtn').classList.toggle('on', prevOn);
  if ($('mmCropTools')) $('mmCropTools').hidden = isMaskMode;
  if ($('mmMaskTools')) $('mmMaskTools').hidden = !isMaskMode;

  const rot = cam.rotate || 0;
  const tilt = normTilt(cam.tilt || 0);
  if ($('mmRotBtn')) $('mmRotBtn').textContent = `Rotate ${rot}°`;
  if ($('mmTiltRange')) $('mmTiltRange').value = String(tilt);
  const mmTiltNum = $('mmTiltNum');
  if (mmTiltNum && document.activeElement !== mmTiltNum) mmTiltNum.value = tilt.toFixed(1);
  if ($('mmGridBtn')) $('mmGridBtn').classList.toggle('on', !!mmState.gridPinned);

  const lock = aspectLock[mmState.camIdx] || null;
  document.querySelectorAll('[data-mmasp]').forEach(btn => {
    const r = btn.dataset.mmasp;
    btn.classList.toggle('on', r === 'free' ? !lock : Math.abs(Number(r) - (lock || 0)) < 0.01);
  });

  const tabsEl = $('mmMaskTabs');
  if (tabsEl) {
    tabsEl.innerHTML = excls.map((_, ei) => `
      <div class="mm-tab${ei === mmState.activeMask ? ' on' : ''}" data-mm-tab="${ei}">
        <span>Mask ${ei + 1}</span>
        <button class="mm-tab-del" type="button" data-mm-del="${ei}" title="Remove Mask ${ei + 1}">×</button>
      </div>
    `).join('');
  }
  const addBtn = $('mmAddBtn');
  if (addBtn) addBtn.disabled = excls.length >= MAX_EXCLUSIONS;
  const fitBtn = $('mmFitMaskBtn');
  if (fitBtn) fitBtn.disabled = !excls.length;

  clampModalPan();
  const { baseW, baseH } = modalBaseMetrics();
  const dispW = baseW * mmState.zoom;
  const dispH = baseH * mmState.zoom;
  const canvas = $('mmCanvas');
  if (canvas) {
    canvas.style.width = dispW.toFixed(1) + 'px';
    canvas.style.height = dispH.toFixed(1) + 'px';
    canvas.style.left = mmState.panX.toFixed(1) + 'px';
    canvas.style.top = mmState.panY.toFixed(1) + 'px';
    canvas.classList.toggle('leveling', !!(mmState.gridPinned || mmState.leveling));
  }

  const c = cam.crop || [0, 0, 1, 1];
  const scEls = [$('mmScT'), $('mmScB'), $('mmScL'), $('mmScR')];
  const cropRef = $('mmCropRef');
  const exclWrap = $('mmExclWrap');
  if (prevOn) {
    scEls.forEach(el => { if (el) el.hidden = true; });
    if (cropRef) cropRef.hidden = true;
    if (exclWrap) exclWrap.hidden = true;
  } else {
    scEls.forEach(el => { if (el) el.hidden = false; });
    applyScrimRects(scEls[0], scEls[1], scEls[2], scEls[3], c);
    if (cropRef) {
      cropRef.hidden = false;
      cropRef.style.cssText = `left:${c[0]*100}%;top:${c[1]*100}%;width:${(c[2]-c[0])*100}%;height:${(c[3]-c[1])*100}%;`;
      cropRef.classList.toggle('passive', isMaskMode);
    }
    if (exclWrap) {
      exclWrap.hidden = false;
      exclWrap.classList.toggle('passive', !isMaskMode);
    }
  }

  renderQuadLayer($('mmSvg'), $('mmDots'), excls, {
    activeIdx: isMaskMode ? mmState.activeMask : -1,
    dragDot: mmState.dragDot,
    showNums: isMaskMode,
  });

  const zr = $('mmZoomRange');
  const zv = $('mmZoomVal');
  if (zr) zr.value = mmState.zoom.toFixed(1);
  if (zv) zv.textContent = mmState.zoom.toFixed(1) + '×';

  const [srcW, srcH] = viewFullDims(cam);
  const outW = Math.round((c[2] - c[0]) * srcW);
  const outH = Math.round((c[3] - c[1]) * srcH);
  const isFullCrop = c[0] <= 0.001 && c[1] <= 0.001 && c[2] >= 0.999 && c[3] >= 0.999;
  const tiltTag = tilt !== 0 ? `${tilt > 0 ? '+' : ''}${tilt.toFixed(1)}°` : '';
  const rotLabel = tilt === 0 ? `Rotate ${rot}°` : (rot === 0 ? `Angle ${tiltTag}` : `Rotate ${rot}° (${tiltTag})`);
  const infoEl = $('mmInfo');
  if (infoEl) {
    infoEl.textContent = (isFullCrop ? `Full frame ${srcW}×${srcH}` : `Cropped ~${outW}×${outH}`) +
      ` · ${rotLabel} · ${excls.length} mask${excls.length === 1 ? '' : 's'}${prevOn ? ' · Preview Cropped' : ''}`;
  }

  const activeQ = excls[mmState.activeMask];
  const hint = $('mmHint');
  if (hint) {
    if (isMaskMode && activeQ && quadHasCrossing(activeQ)) {
      hint.innerHTML = `<span style="color:var(--amber);font-weight:800">Corners crossed on Mask ${mmState.activeMask + 1} — drag dots to uncross</span>`;
    } else if (isMaskMode && mmState.dragDot && excls[mmState.dragDot.ei]) {
      const pt = excls[mmState.dragDot.ei][mmState.dragDot.ci];
      hint.textContent = `Mask ${mmState.dragDot.ei + 1} · Corner ${mmState.dragDot.ci + 1} · px (${Math.round(pt[0] * srcW)}, ${Math.round(pt[1] * srcH)})`;
    } else if (prevOn) {
      hint.textContent = 'Showing cropped result · Wheel/pinch to zoom · Toggle Preview Cropped (P) to adjust blue box';
    } else if (isMaskMode) {
      hint.textContent = 'Drag 4 red dots onto corners · Drag mask to slide · Wheel/pinch or drag background to pan · Switch view [ / ]';
    } else {
      hint.textContent = 'Drag blue handles or box to frame · Angle slider + Grid (G) to level · Switch view [ / ] · Ctrl+S to save';
    }
  }
}

function fitInitialModalView() {
  const cam = cfg?.cameras?.[mmState.camIdx];
  if (!cam) return;
  if (mmState.mode === 'mask' && (cam.exclusions || []).length) {
    fitMaskInModal();
  } else {
    mmState.zoom = 1;
    clampModalPan();
    renderMaskModal();
  }
}

function openMaskModal(idx, opts = {}) {
  if (!cfg || !cfg.cameras[idx]) return;
  const normOpts = typeof opts === 'number' ? { mode: 'mask', focusMaskIdx: opts } : (opts || {});
  const mode = normOpts.mode === 'mask' ? 'mask' : 'crop';
  const focusMaskIdx = Number.isFinite(normOpts.focusMaskIdx) ? normOpts.focusMaskIdx : 0;

  if (previewCropped[idx]) previewCropped[idx] = false;
  editTarget[idx] = mode === 'mask' ? 'excl' : 'crop';
  syncCropOverlay(idx);

  mmState.open = true;
  mmState.camIdx = idx;
  mmState.mode = mode;
  mmState.previewCropped = false;
  mmState.activeMask = focusMaskIdx;
  mmState.hires = false;
  mmState.dragDot = null;
  mmState.pendingFit = true;

  $('maskModal').hidden = false;
  document.body.style.overflow = 'hidden';
  syncRawFeeds();
  updateModalMediaSrc();
  fitInitialModalView();
  drawAllCanvases();
}

function switchModalView(nextIdx) {
  if (!cfg || !cfg.cameras || !cfg.cameras.length) return;
  const n = cfg.cameras.length;
  const targetIdx = ((nextIdx % n) + n) % n;
  const oldIdx = mmState.camIdx;
  mmState.camIdx = targetIdx;
  mmState.activeMask = 0;
  mmState.previewCropped = false;
  mmState.dragDot = null;
  mmState.pendingFit = true;
  editTarget[targetIdx] = mmState.mode === 'mask' ? 'excl' : 'crop';
  if (oldIdx !== targetIdx && cfg.cameras[oldIdx]) syncCropOverlay(oldIdx);
  syncCropOverlay(targetIdx);
  updateModalMediaSrc();
  fitInitialModalView();
  drawAllCanvases();
}

function closeMaskModal() {
  if (!mmState.open) return;
  mmState.open = false;
  mmState.dragDot = null;
  mmState.leveling = false;
  if (mmHiresTimer) { clearTimeout(mmHiresTimer); mmHiresTimer = null; }
  $('maskModal').hidden = true;
  document.body.style.overflow = '';
  const img = $('mmStream');
  if (img) img.removeAttribute('src');
  syncRawFeeds();
  syncCropOverlay(mmState.camIdx);
}

function wireMaskModal() {
  const vp = $('mmViewport');
  const img = $('mmStream');
  if (!vp || !img) return;

  img.addEventListener('load', () => {
    if (!mmState.open) return;
    if (mmState.pendingFit) {
      mmState.pendingFit = false;
      fitInitialModalView();
    } else if (mmState.hires) {
      renderMaskModal();
    }
  });

  const activePointers = new Map();
  let drag = null;
  let pinch = null;

  vp.addEventListener('pointerdown', (e) => {
    if (!mmState.open) return;
    activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (activePointers.size === 2) {
      drag = null;
      mmState.dragDot = null;
      const pts = Array.from(activePointers.values());
      const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      const midX = (pts[0].x + pts[1].x) / 2;
      const midY = (pts[0].y + pts[1].y) / 2;
      const vpRect = vp.getBoundingClientRect();
      const { baseW, baseH } = modalBaseMetrics();
      const ax = midX - vpRect.left;
      const ay = midY - vpRect.top;
      pinch = {
        startDist: Math.max(10, dist),
        startZoom: mmState.zoom,
        ux: (ax - mmState.panX) / Math.max(1, baseW * mmState.zoom),
        uy: (ay - mmState.panY) / Math.max(1, baseH * mmState.zoom),
      };
      return;
    }

    e.preventDefault();
    vp.setPointerCapture(e.pointerId);
    const camIdx = mmState.camIdx;
    const canvas = $('mmCanvas');
    const rect = canvas.getBoundingClientRect();

    if (!mmState.previewCropped && mmState.mode === 'mask') {
      const dotEl = e.target.closest('.quad-dot');
      const polyEl = e.target.closest('.quad-poly');
      if (dotEl && rect.width && rect.height) {
        const ei = Number(dotEl.dataset.exclIdx);
        const ci = Number(dotEl.dataset.cornerIdx);
        mmState.activeMask = ei;
        mmState.dragDot = { ei, ci };
        drag = { pid: e.pointerId, type: 'dot', ei, ci };
        renderMaskModal();
        return;
      }
      if (polyEl && rect.width && rect.height) {
        const ei = Number(polyEl.dataset.exclIdx);
        const q = cfg.cameras[camIdx].exclusions[ei];
        if (!q) return;
        mmState.activeMask = ei;
        drag = {
          pid: e.pointerId,
          type: 'move',
          ei,
          sx: e.clientX,
          sy: e.clientY,
          w: rect.width,
          h: rect.height,
          origQuad: q.map(p => [...p]),
        };
        renderMaskModal();
        return;
      }
    }

    if (!mmState.previewCropped && mmState.mode === 'crop' && rect.width && rect.height) {
      const handleEl = e.target.closest('#mmCropRef [data-handle]');
      const cropEl = e.target.closest('#mmCropRef:not(.passive)');
      const orig = [...(cfg.cameras[camIdx].crop || [0, 0, 1, 1])];
      const isFull = (orig[2] - orig[0]) >= 0.998 && (orig[3] - orig[1]) >= 0.998;
      if (handleEl || (cropEl && !isFull)) {
        drag = {
          pid: e.pointerId,
          type: 'crop',
          mode: handleEl ? handleEl.dataset.handle : 'move',
          sx: e.clientX,
          sy: e.clientY,
          w: rect.width,
          h: rect.height,
          orig,
        };
        return;
      }
    }

    vp.classList.add('panning');
    drag = {
      pid: e.pointerId,
      type: 'pan',
      sx: e.clientX,
      sy: e.clientY,
      origPanX: mmState.panX,
      origPanY: mmState.panY,
    };
  });

  vp.addEventListener('pointermove', (e) => {
    if (!mmState.open) return;
    if (activePointers.has(e.pointerId)) activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pinch && activePointers.size >= 2) {
      const pts = Array.from(activePointers.values());
      const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
      const midX = (pts[0].x + pts[1].x) / 2;
      const midY = (pts[0].y + pts[1].y) / 2;
      const vpRect = vp.getBoundingClientRect();
      const { baseW, baseH } = modalBaseMetrics();
      mmState.zoom = clamp(pinch.startZoom * (dist / pinch.startDist), MM_MIN_ZOOM, MM_MAX_ZOOM);
      mmState.panX = (midX - vpRect.left) - pinch.ux * baseW * mmState.zoom;
      mmState.panY = (midY - vpRect.top) - pinch.uy * baseH * mmState.zoom;
      clampModalPan();
      renderMaskModal();
      return;
    }

    if (!drag || e.pointerId !== drag.pid) return;
    const camIdx = mmState.camIdx;
    if (drag.type === 'dot') {
      const rect = $('mmCanvas').getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const nx = +clamp((e.clientX - rect.left) / rect.width, 0, 1).toFixed(4);
      const ny = +clamp((e.clientY - rect.top) / rect.height, 0, 1).toFixed(4);
      cfg.cameras[camIdx].exclusions[drag.ei][drag.ci] = [nx, ny];
      markDirty();
      renderMaskModal();
      syncCropOverlay(camIdx);
      return;
    }
    if (drag.type === 'move') {
      const dxRaw = (e.clientX - drag.sx) / drag.w;
      const dyRaw = (e.clientY - drag.sy) / drag.h;
      const [bx0, by0, bx1, by1] = quadBounds(drag.origQuad);
      const dx = clamp(dxRaw, -bx0, 1 - bx1);
      const dy = clamp(dyRaw, -by0, 1 - by1);
      cfg.cameras[camIdx].exclusions[drag.ei] = drag.origQuad.map(p => [+(p[0] + dx).toFixed(4), +(p[1] + dy).toFixed(4)]);
      markDirty();
      renderMaskModal();
      syncCropOverlay(camIdx);
      return;
    }
    if (drag.type === 'crop') {
      const dx = (e.clientX - drag.sx) / drag.w;
      const dy = (e.clientY - drag.sy) / drag.h;
      cfg.cameras[camIdx].crop = applyCropDragDelta(camIdx, drag.orig, drag.mode, dx, dy, MIN_SPAN);
      markDirty();
      renderMaskModal();
      syncCropOverlay(camIdx);
      return;
    }
    if (drag.type === 'pan') {
      mmState.panX = drag.origPanX + (e.clientX - drag.sx);
      mmState.panY = drag.origPanY + (e.clientY - drag.sy);
      clampModalPan();
      renderMaskModal();
    }
  });

  const endPtr = (e) => {
    activePointers.delete(e.pointerId);
    if (activePointers.size < 2) pinch = null;
    if (drag && e.pointerId === drag.pid) {
      drag = null;
      mmState.dragDot = null;
      vp.classList.remove('panning');
      if (mmState.open) renderMaskModal();
    }
  };
  vp.addEventListener('pointerup', endPtr);
  vp.addEventListener('pointercancel', endPtr);

  vp.addEventListener('wheel', (e) => {
    if (!mmState.open) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    setModalZoom(mmState.zoom * factor, e.clientX, e.clientY);
  }, { passive: false });

  $('mmViewSel').onchange = (e) => switchModalView(Number(e.target.value));
  $('mmPrevView').onclick = () => switchModalView(mmState.camIdx - 1);
  $('mmNextView').onclick = () => switchModalView(mmState.camIdx + 1);

  $('mmModeCrop').onclick = () => {
    mmState.mode = 'crop';
    const wasPrev = mmState.previewCropped;
    mmState.previewCropped = false;
    editTarget[mmState.camIdx] = 'crop';
    syncCropOverlay(mmState.camIdx);
    if (wasPrev && mmState.hires) updateModalMediaSrc();
    else drawAllCanvases();
    renderMaskModal();
  };
  $('mmModeMask').onclick = () => {
    mmState.mode = 'mask';
    const wasPrev = mmState.previewCropped;
    mmState.previewCropped = false;
    const cam = cfg.cameras[mmState.camIdx];
    editTarget[mmState.camIdx] = (cam?.exclusions || []).length ? 'excl' : 'crop';
    syncCropOverlay(mmState.camIdx);
    if (wasPrev && mmState.hires) updateModalMediaSrc();
    else drawAllCanvases();
    renderMaskModal();
  };
  $('mmPreviewBtn').onclick = () => {
    mmState.previewCropped = !mmState.previewCropped;
    mmState.zoom = 1;
    clampModalPan();
    updateModalMediaSrc();
    drawAllCanvases();
    renderMaskModal();
  };

  $('mmRotBtn').onclick = () => {
    const idx = mmState.camIdx;
    const cam = cfg.cameras[idx];
    if (!cam) return;
    cam.rotate = ((cam.rotate || 0) + 90) % 360;
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    scheduleModalHiresRefresh(true);
    renderMaskModal();
  };

  const mmTiltRange = $('mmTiltRange');
  const mmTiltNum = $('mmTiltNum');
  const setModalLevel = (on) => {
    mmState.leveling = on;
    const cvs = $('mmCanvas');
    if (cvs) cvs.classList.toggle('leveling', !!(mmState.gridPinned || mmState.leveling));
  };
  if (mmTiltRange) {
    mmTiltRange.oninput = (e) => setViewTilt(mmState.camIdx, e.target.value, false);
    mmTiltRange.addEventListener('pointerdown', () => setModalLevel(true));
    mmTiltRange.addEventListener('focus', () => setModalLevel(true));
    mmTiltRange.addEventListener('pointerup', () => setModalLevel(false));
    mmTiltRange.addEventListener('pointercancel', () => setModalLevel(false));
    mmTiltRange.addEventListener('blur', () => setModalLevel(false));
  }
  if (mmTiltNum) {
    mmTiltNum.oninput = (e) => {
      const raw = String(e.target.value || '').trim();
      if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return;
      setViewTilt(mmState.camIdx, raw, false);
    };
    mmTiltNum.onchange = () => {
      setViewTilt(mmState.camIdx, mmTiltNum.value, false);
      mmTiltNum.value = normTilt(cfg.cameras[mmState.camIdx]?.tilt || 0).toFixed(1);
    };
    mmTiltNum.addEventListener('focus', () => setModalLevel(true));
    mmTiltNum.addEventListener('blur', () => {
      setModalLevel(false);
      mmTiltNum.value = normTilt(cfg.cameras[mmState.camIdx]?.tilt || 0).toFixed(1);
    });
  }
  $('mmTiltMinus').onclick = () => setViewTilt(mmState.camIdx, (cfg.cameras[mmState.camIdx]?.tilt || 0) - 0.5, true);
  $('mmTiltPlus').onclick = () => setViewTilt(mmState.camIdx, (cfg.cameras[mmState.camIdx]?.tilt || 0) + 0.5, true);
  $('mmTiltReset').onclick = () => setViewTilt(mmState.camIdx, 0, true);
  $('mmGridBtn').onclick = () => {
    mmState.gridPinned = !mmState.gridPinned;
    renderMaskModal();
  };
  $('mmResetCropBtn').onclick = () => {
    const idx = mmState.camIdx;
    if (!cfg.cameras[idx]) return;
    cfg.cameras[idx].crop = [0, 0, 1, 1];
    aspectLock[idx] = null;
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    scheduleModalHiresRefresh(true);
    renderMaskModal();
  };
  document.querySelectorAll('[data-mmasp]').forEach(btn => {
    btn.onclick = () => {
      const ratioStr = btn.dataset.mmasp;
      setCropAspectRatio(mmState.camIdx, ratioStr === 'free' ? null : Number(ratioStr));
    };
  });

  $('mmMaskTabs').addEventListener('click', (e) => {
    const delBtn = e.target.closest('[data-mm-del]');
    const camIdx = mmState.camIdx;
    const list = cfg.cameras[camIdx].exclusions || [];
    if (delBtn) {
      const ei = Number(delBtn.dataset.mmDel);
      if (ei >= 0 && ei < list.length) list.splice(ei, 1);
      if (mmState.activeMask >= list.length) mmState.activeMask = Math.max(0, list.length - 1);
      if (!list.length) editTarget[camIdx] = 'crop';
      markDirty();
      syncCropOverlay(camIdx);
      renderMaskModal();
      return;
    }
    const tab = e.target.closest('[data-mm-tab]');
    if (tab) {
      mmState.activeMask = Number(tab.dataset.mmTab);
      fitMaskInModal();
    }
  });

  $('mmAddBtn').onclick = () => {
    const cam = cfg.cameras[mmState.camIdx];
    const list = (cam.exclusions ||= []);
    if (list.length >= MAX_EXCLUSIONS) return;
    list.push(defaultQuadFor(cam, list.length, modalVisibleNormBox()));
    mmState.activeMask = list.length - 1;
    mmState.mode = 'mask';
    editTarget[mmState.camIdx] = 'excl';
    markDirty();
    syncCropOverlay(mmState.camIdx);
    fitMaskInModal();
  };

  $('mmClearBtn').onclick = () => {
    cfg.cameras[mmState.camIdx].exclusions = [];
    mmState.activeMask = 0;
    editTarget[mmState.camIdx] = 'crop';
    markDirty();
    syncCropOverlay(mmState.camIdx);
    renderMaskModal();
  };

  $('mmHiresBtn').onclick = () => {
    mmState.hires = !mmState.hires;
    updateModalMediaSrc();
  };

  $('mmRefreshBtn').onclick = () => {
    if (mmState.hires) updateModalMediaSrc();
  };

  $('mmSaveBtn').onclick = () => saveConfig($('mmSaveBtn'));
  $('mmCloseBtn').onclick = closeMaskModal;
  $('mmFitMaskBtn').onclick = fitMaskInModal;
  $('mmFitCropBtn').onclick = fitCropInModal;
  $('mmZoomRange').oninput = (e) => setModalZoom(Number(e.target.value));

  document.querySelectorAll('[data-mmz]').forEach(btn => {
    btn.onclick = () => setModalZoom(Number(btn.dataset.mmz));
  });

  window.addEventListener('resize', () => { if (mmState.open) renderMaskModal(); });
  window.addEventListener('keydown', (e) => {
    if (!mmState.open) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        saveConfig($('saveAllTop'));
      }
      return;
    }
    if (e.key === 'Escape') {
      closeMaskModal();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveConfig($('mmSaveBtn'));
    } else if (!e.target.closest('input,select,textarea')) {
      if (e.key === '[') switchModalView(mmState.camIdx - 1);
      else if (e.key === ']') switchModalView(mmState.camIdx + 1);
      else if (e.key.toLowerCase() === 'g') {
        mmState.gridPinned = !mmState.gridPinned;
        renderMaskModal();
      } else if (e.key.toLowerCase() === 'p') {
        $('mmPreviewBtn').click();
      }
    }
  });
}

function renderCameras() {
  const physList = cfg.cameras.filter(x => !x.sourceSlug);
  const physBySlug = new Map(physList.map(x => [x.slug, x]));

  $('camGrid').innerHTML = cfg.cameras.map((c, idx) => {
    const isVirt = !!c.sourceSlug;
    const parent = isVirt ? physBySlug.get(c.sourceSlug) : null;
    const headLeft = isVirt
      ? `<input type="text" class="cam-name-inp" id="name-${idx}" value="${esc(c.name)}" aria-label="View name">` +
        `<span class="pill">via ${esc(parent ? parent.name : c.sourceSlug)}</span>`
      : `<span>${esc(c.name)}</span>`;
    const fsBtn = `<button class="btn sec sm" type="button" data-fullscreen="${idx}" title="Open full-screen calibration editor for this view">⤢ Full Screen</button>`;
    const realignBtn = !isVirt
      ? `<button class="btn sec sm" type="button" data-realign="${esc(c.slug)}" title="Realign physical camera back onto saved landmarks">◎ Realign</button>`
      : '';
    const headRight = isVirt
      ? `${fsBtn}<span class="pill live" id="camState-${idx}">CONNECTING</span>` +
        `<button class="btn red sm" type="button" data-delview="${idx}" title="Remove view">Remove</button>`
      : `${realignBtn}${fsBtn}<button class="btn sec sm" type="button" data-addview="${idx}" title="Add another crop/mask view from this camera">+ Add View</button>` +
        `<span class="pill live" id="camState-${idx}">CONNECTING</span>`;
    const srcSelect = isVirt ? `
      <label>Source Camera</label>
      <select id="src-${idx}">
        ${physList.map(p => `<option value="${esc(p.slug)}"${p.slug === c.sourceSlug ? ' selected' : ''}>${esc(p.name)} (Shared Feed · No Extra RTSP)</option>`).join('')}
        <option value="">Own RTSP Camera (Physical)</option>
      </select>` : '';

    return `
    <div class="card">
      <h2>
        <div class="cam-head-l">${headLeft}</div>
        <div class="cam-head-r">${headRight}</div>
      </h2>
      <div class="stage" id="stage-${idx}">
        <canvas class="stream" id="stream-${idx}"></canvas>
        <div class="level-grid"></div>
        <div class="crop-layer" id="layer-${idx}">
          <div class="scrim" id="scT-${idx}"></div>
          <div class="scrim" id="scB-${idx}"></div>
          <div class="scrim" id="scL-${idx}"></div>
          <div class="scrim" id="scR-${idx}"></div>
          <div class="crop-box" id="box-${idx}">
            <span class="box-tag">CROP</span>
            <div class="handle nw" data-handle="nw"></div>
            <div class="handle ne" data-handle="ne"></div>
            <div class="handle sw" data-handle="sw"></div>
            <div class="handle se" data-handle="se"></div>
          </div>
          <div class="excl-wrap" id="exclWrap-${idx}">
            <svg class="excl-svg" id="exclSvg-${idx}" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg>
            <div id="exclDots-${idx}"></div>
          </div>
        </div>
      </div>
      <div class="crop-info">
        <span id="cropInfo-${idx}">Full frame</span>
        <span id="energyTxt-${idx}">Motion: 0.0</span>
      </div>
      <div class="mbar-wrap">
        <div class="mbar-fill" id="mbar-${idx}"></div>
        <div class="mbar-th" id="mth-${idx}" style="left:${clamp((c.motionThreshold / 20) * 100, 2, 98)}%"></div>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Edit:</span>
        <button class="btn sec sm on" type="button" data-emode="${idx}:crop" id="modeCrop-${idx}">Frame Crop (Blue)</button>
        <button class="btn sec sm" type="button" data-emode="${idx}:excl" id="modeExcl-${idx}" disabled>Edit Masks</button>
        <button class="btn sec sm" type="button" data-zoomexcl="${idx}" id="zoomExcl-${idx}" disabled>⤢ Zoom Masks</button>
        <button class="btn red sm" type="button" data-addexcl="${idx}" id="addExcl-${idx}">+ Mask Mirror (Red)</button>
        <button class="btn sec sm" type="button" data-exclclear="${idx}">Clear Masks</button>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Zoom</span>
        <input type="range" id="zoom-${idx}" min="1" max="3" step="0.05" value="1">
        <button class="btn sec sm" type="button" data-reset="${idx}">1×</button>
        <button class="btn sec sm" type="button" data-rot="${idx}" id="rotBtn-${idx}">Rotate 0°</button>
        <button class="btn sec sm" type="button" data-prev="${idx}" id="prevBtn-${idx}">Preview Crop</button>
      </div>
      <div class="ctrl-row" id="tiltRow-${idx}">
        <span style="font-size:12px;color:var(--mut)">Angle</span>
        <button class="btn sec sm" type="button" data-tiltstep="${idx}:-0.5" title="Rotate counter-clockwise 0.5°">−0.5°</button>
        <input type="range" id="tilt-${idx}" min="-45" max="45" step="0.5" value="${normTilt(c.tilt || 0)}">
        <button class="btn sec sm" type="button" data-tiltstep="${idx}:0.5" title="Rotate clockwise 0.5°">+0.5°</button>
        <input type="number" class="tilt-num" id="tiltNum-${idx}" min="-45" max="45" step="0.1" value="${normTilt(c.tilt || 0).toFixed(1)}" aria-label="Custom rotate angle in degrees">
        <button class="btn sec sm" type="button" data-tiltreset="${idx}" title="Reset angle to 0°">0°</button>
      </div>
      <div class="ctrl-row">
        <span style="font-size:12px;color:var(--mut)">Box Ratio:</span>
        <button class="btn sec sm on" type="button" data-asp="${idx}:free">Free</button>
        <button class="btn sec sm" type="button" data-asp="${idx}:1.7778">16:9</button>
        <button class="btn sec sm" type="button" data-asp="${idx}:0.5625">9:16</button>
        <button class="btn ok sm" type="button" data-save="${idx}" style="margin-left:auto">Save</button>
      </div>
      ${srcSelect}
      <div id="rtspRow-${idx}"${isVirt ? ' hidden' : ''}>
        <label>RTSP URL</label>
        <input type="text" id="rtsp-${idx}" value="${esc(c.rtspUrl || '')}" placeholder="rtsp://thingino:thingino@192.168.1.xxx:554/ch0">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        <div><label>Motion Threshold</label><input type="number" step="0.2" id="thresh-${idx}" value="${c.motionThreshold}"></div>
        <div>
          <label>Defish (v360)</label>
          <select id="defish-${idx}">
            <option value="1"${c.defish !== false ? ' selected' : ''}>ON (Virtual PTZ)</option>
            <option value="0"${c.defish === false ? ' selected' : ''}>OFF (Raw Fisheye)</option>
          </select>
        </div>
        <div><label>Enabled</label><select id="en-${idx}"><option value="1"${c.enabled?' selected':''}>Yes</option><option value="0"${!c.enabled?' selected':''}>No</option></select></div>
      </div>
    </div>`;
  }).join('');

  cfg.cameras.forEach((c, idx) => {
    syncCropOverlay(idx);
    wireCropDrag(idx);
    $(`zoom-${idx}`).oninput = (e) => setZoomAroundCenter(idx, Number(e.target.value));
    const stageEl = $(`stage-${idx}`);
    const tiltSlider = $(`tilt-${idx}`);
    const tiltNum = $(`tiltNum-${idx}`);
    const showLevel = (on) => { if (stageEl) stageEl.classList.toggle('leveling', on); };
    if (tiltSlider) {
      tiltSlider.oninput = (e) => setViewTilt(idx, e.target.value, false);
      tiltSlider.addEventListener('pointerdown', () => showLevel(true));
      tiltSlider.addEventListener('focus', () => showLevel(true));
      tiltSlider.addEventListener('pointerup', () => showLevel(false));
      tiltSlider.addEventListener('pointercancel', () => showLevel(false));
      tiltSlider.addEventListener('blur', () => showLevel(false));
    }
    if (tiltNum) {
      tiltNum.oninput = (e) => {
        const raw = String(e.target.value || '').trim();
        if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return;
        setViewTilt(idx, raw, false);
      };
      tiltNum.onchange = () => {
        setViewTilt(idx, tiltNum.value, false);
        tiltNum.value = normTilt(cfg.cameras[idx].tilt || 0).toFixed(1);
      };
      tiltNum.addEventListener('focus', () => showLevel(true));
      tiltNum.addEventListener('blur', () => {
        showLevel(false);
        tiltNum.value = normTilt(cfg.cameras[idx].tilt || 0).toFixed(1);
      });
    }
    $(`thresh-${idx}`).oninput = (e) => {
      const th = Number(e.target.value) || 2.2;
      cfg.cameras[idx].motionThreshold = th;
      const tick = $(`mth-${idx}`);
      if (tick) tick.style.left = clamp((th / 20) * 100, 2, 98) + '%';
      markDirty();
    };
    const nameInp = $(`name-${idx}`);
    if (nameInp) nameInp.oninput = () => markDirty();
    const srcSel = $(`src-${idx}`);
    if (srcSel) {
      srcSel.onchange = () => {
        const nextSrc = srcSel.value;
        cfg.cameras[idx].sourceSlug = nextSrc;
        const rRow = $(`rtspRow-${idx}`);
        if (rRow) rRow.hidden = !!nextSrc;
        markDirty();
      };
    }
    $(`rtsp-${idx}`).oninput = () => markDirty();
    $(`defish-${idx}`).onchange = (e) => {
      cfg.cameras[idx].defish = (e.target.value !== '0');
      markDirty();
    };
    $(`en-${idx}`).onchange = () => markDirty();
  });
  syncRawFeeds();
  drawAllCanvases();
}

const levelTimers = new Map();
function setViewTilt(idx, val, flashGrid = false) {
  if (!cfg || !cfg.cameras || !cfg.cameras[idx]) return;
  cfg.cameras[idx].tilt = normTilt(val);
  markDirty();
  syncCropOverlay(idx);
  drawAllCanvases();
  if (flashGrid) {
    const st = $(`stage-${idx}`);
    if (st) {
      st.classList.add('leveling');
      const old = levelTimers.get(idx);
      if (old) clearTimeout(old);
      levelTimers.set(idx, setTimeout(() => { st.classList.remove('leveling'); }, 900));
    }
  }
  if (mmState.open && mmState.camIdx === idx) {
    if (flashGrid) {
      mmState.leveling = true;
      if (mmLevelTimer) clearTimeout(mmLevelTimer);
      mmLevelTimer = setTimeout(() => {
        mmState.leveling = false;
        const cvs = $('mmCanvas');
        if (cvs) cvs.classList.toggle('leveling', !!(mmState.gridPinned || mmState.leveling));
      }, 900);
    }
    scheduleModalHiresRefresh(false);
    renderMaskModal();
  }
}

function setCropAspectRatio(idx, lock) {
  if (!cfg || !cfg.cameras || !cfg.cameras[idx]) return;
  aspectLock[idx] = lock;
  if (lock) {
    const cam = cfg.cameras[idx];
    const c = cam.crop || [0, 0, 1, 1];
    const cx = (c[0] + c[2]) / 2;
    const cy = (c[1] + c[3]) / 2;
    const [srcW, srcH] = viewFullDims(cam);
    const targetNormRatio = lock * (srcH / srcW);
    let nw = clamp(c[2] - c[0], MIN_SPAN, 1);
    let nh = nw / targetNormRatio;
    if (nh < MIN_SPAN) { nh = MIN_SPAN; nw = Math.min(1, nh * targetNormRatio); }
    if (nh > 1) { nh = 1; nw = clamp(nh * targetNormRatio, MIN_SPAN, 1); }
    const x0 = clamp(cx - nw / 2, 0, 1 - nw);
    const y0 = clamp(cy - nh / 2, 0, 1 - nh);
    cam.crop = [+x0.toFixed(4), +y0.toFixed(4), +(x0 + nw).toFixed(4), +(y0 + nh).toFixed(4)];
  }
  markDirty();
  syncCropOverlay(idx);
  drawAllCanvases();
  if (mmState.open && mmState.camIdx === idx) renderMaskModal();
}

$('camGrid').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.realign !== undefined) {
    openRealignModal(b.dataset.realign);
    return;
  }
  if (b.dataset.fullscreen !== undefined) {
    openMaskModal(Number(b.dataset.fullscreen), { mode: 'crop' });
    return;
  }
  if (b.dataset.addview !== undefined) {
    const idx = Number(b.dataset.addview);
    const parent = cfg.cameras[idx];
    if (!parent) return;
    const countOnParent = cfg.cameras.filter(x => sourceSlugOf(x) === parent.slug).length;
    const defName = `${parent.name.replace(/\\s+cam$/i, '')} View ${countOnParent + 1}`;
    const rawName = window.prompt(`New view name (from ${parent.name}):`, defName);
    if (!rawName || !rawName.trim()) return;
    const name = rawName.trim();
    const slug = makeUniqueSlug(name);
    cfg.cameras.push({
      name,
      slug,
      sourceSlug: parent.slug,
      rtspUrl: '',
      enabled: true,
      motionThreshold: 2.2,
      roi: [0, 0, 1, 1],
      exclusions: [],
      crop: [0, 0, 1, 1],
      rotate: parent.rotate || 0,
      tilt: 0,
      defish: true,
    });
    renderCameras();
    await saveConfig(null);
    showToast(`✓ Added view "${name}"`, 'ok');
    return;
  }
  if (b.dataset.delview !== undefined) {
    const idx = Number(b.dataset.delview);
    const c = cfg.cameras[idx];
    if (!c || !c.sourceSlug) return;
    if (!window.confirm(`Remove view "${c.name}"? Raw camera tape is unaffected.`)) return;
    if (mmState.open) closeMaskModal();
    cfg.cameras.splice(idx, 1);
    renderCameras();
    await saveConfig(null);
    showToast(`Removed "${c.name}"`, 'ok');
    return;
  }
  if (b.dataset.reset !== undefined) {
    const idx = Number(b.dataset.reset);
    cfg.cameras[idx].crop = [0, 0, 1, 1];
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    return;
  }
  if (b.dataset.rot !== undefined) {
    const idx = Number(b.dataset.rot);
    cfg.cameras[idx].rotate = ((cfg.cameras[idx].rotate || 0) + 90) % 360;
    markDirty();
    syncCropOverlay(idx);
    drawAllCanvases();
    if (previewCropped[idx]) saveConfig();
    return;
  }
  if (b.dataset.tiltstep !== undefined) {
    const [iStr, dStr] = b.dataset.tiltstep.split(':');
    const idx = Number(iStr);
    const delta = Number(dStr) || 0;
    setViewTilt(idx, (cfg.cameras[idx].tilt || 0) + delta, true);
    return;
  }
  if (b.dataset.tiltreset !== undefined) {
    const idx = Number(b.dataset.tiltreset);
    setViewTilt(idx, 0, true);
    return;
  }
  if (b.dataset.prev !== undefined) {
    const idx = Number(b.dataset.prev);
    previewCropped[idx] = !previewCropped[idx];
    syncCropOverlay(idx);
    drawAllCanvases();
    if (previewCropped[idx]) saveConfig();
    return;
  }
  if (b.dataset.asp !== undefined) {
    const [iStr, ratioStr] = b.dataset.asp.split(':');
    const idx = Number(iStr);
    setCropAspectRatio(idx, ratioStr === 'free' ? null : Number(ratioStr));
    return;
  }
  if (b.dataset.addexcl !== undefined) {
    const idx = Number(b.dataset.addexcl);
    const list = (cfg.cameras[idx].exclusions ||= []);
    let focusIdx = Math.max(0, list.length - 1);
    if (list.length < MAX_EXCLUSIONS) {
      list.push(defaultQuadFor(cfg.cameras[idx], list.length));
      focusIdx = list.length - 1;
      markDirty();
    }
    openMaskModal(idx, { mode: 'mask', focusMaskIdx: focusIdx });
    return;
  }
  if (b.dataset.zoomexcl !== undefined) {
    const idx = Number(b.dataset.zoomexcl);
    if (!(cfg.cameras[idx].exclusions || []).length) return;
    openMaskModal(idx, { mode: 'mask', focusMaskIdx: 0 });
    return;
  }
  if (b.dataset.exclclear !== undefined) {
    const idx = Number(b.dataset.exclclear);
    cfg.cameras[idx].exclusions = [];
    editTarget[idx] = 'crop';
    markDirty();
    syncCropOverlay(idx);
    return;
  }
  if (b.dataset.emode !== undefined) {
    const [iStr, mode] = b.dataset.emode.split(':');
    const idx = Number(iStr);
    if (mode === 'excl') {
      if (!(cfg.cameras[idx].exclusions || []).length) return;
      openMaskModal(idx, { mode: 'mask', focusMaskIdx: 0 });
      return;
    }
    editTarget[idx] = mode;
    syncCropOverlay(idx);
    return;
  }
  if (b.dataset.save !== undefined) {
    saveConfig(b);
  }
});

async function saveConfig(triggerBtn = null) {
  if (!cfg) return;
  if (triggerBtn) setBtnFeedback(triggerBtn, 'saving', 'Saving…');

  const cameras = cfg.cameras.map((c, idx) => {
    const nameInp = $(`name-${idx}`);
    const srcSel = $(`src-${idx}`);
    const sourceSlug = srcSel ? srcSel.value : (c.sourceSlug || '');
    const outCam = {
      ...c,
      name: nameInp ? (nameInp.value.trim() || c.name) : c.name,
      sourceSlug,
      rtspUrl: sourceSlug ? '' : ($(`rtsp-${idx}`) ? $(`rtsp-${idx}`).value.trim() : c.rtspUrl),
      motionThreshold: $(`thresh-${idx}`) ? (Number($(`thresh-${idx}`).value) || 2.2) : c.motionThreshold,
      tilt: normTilt(c.tilt || 0),
      defish: $(`defish-${idx}`) ? ($(`defish-${idx}`).value !== '0') : (c.defish !== false),
      enabled: $(`en-${idx}`) ? ($(`en-${idx}`).value === '1') : !!c.enabled,
    };
    delete outCam.primary;
    delete outCam.autoLevel;
    delete outCam.autoLevelAxis;
    return outCam;
  });
  const wallboard = {
    ...(cfg.wallboard || {}),
    fireTvHost: $('wbHost').value.trim(),
    fireTvMac: $('wbMac').value.trim(),
    adbPort: Number($('wbPort').value) || 5555,
    displayClient: $('wbClient').value || 'silk',
    idleSleepSec: clamp(Math.round((Number($('wbIdleMin').value) || 10) * 60), 60, 7200),
    wakeOnStart: $('wbWake').value === '1',
    sleepOnStop: $('wbSleep').value === '1',
  };
  const body = {
    retentionDays: Number($('retDays').value) || 14,
    preRollSec: Number($('preRoll').value) || 120,
    driveBackupDir: $('driveDir').value.trim(),
    bumpAlertNtfy: $('bumpAlertNtfy') ? $('bumpAlertNtfy').checked : (cfg.bumpAlertNtfy !== false),
    motionRecord247: $('motionRecord247') ? $('motionRecord247').checked : (cfg.motionRecord247 !== false),
    cameras,
    wallboard,
  };
  try {
    const resp = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const res = await resp.json();
    if (!res || !res.ok) throw new Error('Save rejected');
    const stamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    $('saveMsg').textContent = `Saved & applied live! (${stamp})`;
    $('wbOut').textContent = `Saved! (${stamp})`;
    cfg.cameras = cameras;
    cfg.wallboard = wallboard;
    cfg.bumpAlertNtfy = body.bumpAlertNtfy;
    cfg.motionRecord247 = body.motionRecord247;
    syncRawFeeds();
    markDirty(false);
    pollStatus();
    if (triggerBtn) {
      setBtnFeedback(triggerBtn, 'saved', '✓ Saved!', 2200);
      showToast(triggerBtn === $('mmSaveBtn') ? '✓ View settings saved & applied live' : '✓ Saved & applied live', 'ok');
    }
  } catch (err) {
    $('saveMsg').textContent = 'Save failed';
    $('wbOut').textContent = 'Save failed';
    if (triggerBtn) {
      setBtnFeedback(triggerBtn, 'save-err', '✗ Save Failed', 3000);
      showToast('✗ Save failed — check OptiPlex server', 'err');
    }
  }
}

function syncVoiceUI(vx) {
  if (!vx) return;
  const badge = $('vxBadge');
  if (badge) {
    let lbl = 'OFF';
    let cls = 'pill';
    if (!vx.enabled) { lbl = 'OFF'; cls = 'pill'; }
    else if (vx.error) { lbl = 'ERROR'; cls = 'pill'; }
    else if (vx.armed) { lbl = 'ARMED'; cls = 'pill ok'; }
    else if (vx.listening) { lbl = 'LISTENING'; cls = 'pill ok'; }
    else if (vx.loading) { lbl = 'LOADING…'; cls = 'pill live'; }
    else { lbl = vx.ready ? 'STANDBY' : 'INIT…'; cls = 'pill live'; }
    badge.textContent = lbl;
    badge.className = cls;
  }
  const meter = $('vxMeter');
  if (meter) {
    const pct = clamp((Number(vx.rms || 0) / 0.08) * 100, 0, 100);
    meter.style.width = pct.toFixed(1) + '%';
    meter.classList.toggle('hot', Number(vx.vad || 0) >= Number((vx.config && vx.config.vadThreshold) || 0.45));
  }
  const testBtn = $('vxTestBtn');
  if (testBtn) {
    testBtn.textContent = vx.testRemainingSec > 0 ? `Stop Test (${vx.testRemainingSec}s)` : 'Test Mic (60s)';
    testBtn.dataset.testing = vx.testRemainingSec > 0 ? '1' : '0';
  }
  const ml = $('vxModelLbl');
  if (ml) {
    ml.textContent = vx.asrDevice ? `Whisper Model (${String(vx.asrDevice).toUpperCase()})` : 'Whisper Model';
  }
  const msgEl = $('vxMsg');
  if (msgEl) {
    const parts = [
      `Mic: <b>${escHtml(vx.device || 'PowerConf S330')}</b>${vx.hostApi ? ` (${escHtml(vx.hostApi)})` : ''}`,
      `Model: <b>${escHtml(vx.modelSize || 'base.en')}</b>${vx.asrDevice ? ` (${escHtml(vx.asrDevice)})` : ''}`,
      `SSE: <b>${Number(vx.sseClients || 0)}</b>`,
    ];
    if (vx.aec && vx.aec.enabled) {
      if (vx.aec.active) {
        parts.push(`Line-In AEC: <b style="color:var(--green)">ACTIVE (-${Number(vx.aec.erleDb || 0).toFixed(1)} dB · ${Number(vx.aec.lagMs || 0).toFixed(0)}ms)</b>`);
      } else if (vx.aec.streamOpen) {
        parts.push(`Line-In AEC: <b>STANDBY (${Number(vx.aec.lineInDb || -99).toFixed(0)} dB)</b>`);
      } else {
        parts.push(`Line-In AEC: <span style="color:var(--amber)">${escHtml(vx.aec.error || 'Disconnected')}</span>`);
      }
    }
    if (vx.error) parts.push(`<span style="color:var(--amber)">${escHtml(vx.error)}</span>`);
    else if (!vx.listening && vx.enabled) parts.push('Shared mic closed until active Lift workout');
    msgEl.innerHTML = parts.join(' · ');
  }
  const recEl = $('vxRecent');
  if (recEl) {
    const rows = Array.isArray(vx.recent) ? vx.recent : [];
    if (!rows.length) {
      recEl.textContent = 'No voice utterances captured yet.';
    } else {
      recEl.innerHTML = `<table><thead><tr><th>Time</th><th>Heard</th><th>Wake</th><th>ASR</th><th>Pixel Ack</th></tr></thead><tbody>` +
        rows.map(r => {
          const ts = new Date(r.tsMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          return `<tr><td>${escHtml(ts)}</td><td>${escHtml(r.transcript)}</td><td>${r.emitted ? '<span class="pill ok">SENT</span>' : '<span class="pill">IGNORED</span>'}</td><td>${Number(r.latencyMs || 0)} ms</td><td>${escHtml(r.ackText || '—')}</td></tr>`;
        }).join('') +
        `</tbody></table>`;
    }
  }
}

async function pollVoiceStatus() {
  try {
    const vx = await fetch('/api/voice/status').then(r => r.json());
    if (vx.devices && Array.isArray(vx.devices.inputs)) {
      const sel = $('vxDevice');
      if (sel) {
        const cur = (vx.config && vx.config.deviceMatch) || sel.value || 'PowerConf S330';
        const seen = new Set(['PowerConf S330']);
        const opts = ['<option value="PowerConf S330">PowerConf S330 (Auto)</option>'];
        vx.devices.inputs.forEach(d => {
          if (d.name && !seen.has(d.name)) {
            seen.add(d.name);
            opts.push(`<option value="${escHtml(d.name)}">${escHtml(d.name)} (${escHtml(d.hostApi)})</option>`);
          }
        });
        if (cur && !seen.has(cur)) opts.push(`<option value="${escHtml(cur)}">${escHtml(cur)}</option>`);
        if (document.activeElement !== sel) {
          sel.innerHTML = opts.join('');
          sel.value = cur;
        }
      }
    }
    if (vx.config && cfg) cfg.voice = vx.config;
    syncVoiceUI(vx);
  } catch {}
}

async function saveVoiceConfig(triggerBtn) {
  const body = {
    enabled: $('vxEnabled').value === '1',
    listenMode: $('vxMode').value || 'workout',
    deviceMatch: $('vxDevice').value || 'PowerConf S330',
    speakerReadback: $('vxReadback').value === '1',
    roomCommandReadback: $('vxRoomCmdReadback') ? ($('vxRoomCmdReadback').value === '1') : true,
    setCommitMode: $('vxSetCommitMode').value || 'voice',
    wakeWord: $('vxWakeWord') ? ($('vxWakeWord').value || 'hey jarvis') : undefined,
    ttsVoice: $('vxTtsVoice').value || 'kokoro:am_michael',
    modelSize: $('vxModel').value || 'base.en',
    vadThreshold: Number($('vxVad').value) || 0.45,
  };
  try {
    const r = await fetch('/api/voice/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(x => x.json());
    if (!r || !r.ok) throw new Error('Rejected');
    if (cfg) cfg.voice = r.config;
    syncVoiceUI(r.status);
    $('vxOut').textContent = 'Voice settings saved!';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'saved', '✓ Saved!', 2000);
  } catch {
    $('vxOut').textContent = 'Save failed';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'save-err', '✗ Failed', 2500);
  }
}

async function resolveAbTrial(trialId, winner) {
  try {
    await fetch('/api/voice/ab/resolve', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ trialId, winner }) });
    await pollVoiceAb();
  } catch {}
}

async function pollVoiceAb() {
  try {
    const ab = await fetch('/api/voice/ab').then(r => r.json());
    const sum = ab.summary || {};
    const sumEl = $('vxAbSummary');
    if (sumEl) {
      if (!sum.totalTrials) {
        sumEl.textContent = 'No paired workout A/B trials recorded yet. Toggle Dual-Mic A/B Benchmark in Armory Fit Settings or during a lift.';
      } else {
        const sign = sum.deltaPts > 0 ? '+' : '';
        const relTxt = sum.relativeGainPct != null ? ` (${sum.relativeGainPct > 0 ? '+' : ''}${sum.relativeGainPct}% relative)` : '';
        const agreeTxt = sum.agreementPct != null ? ` · Agreement <b>${sum.agreementPct}%</b>` : '';
        sumEl.innerHTML =
          `Trials: <b>${sum.totalTrials}</b> · Room Catch: <b>${sum.roomCatchPct}%</b> (${sum.roomWins}/${sum.totalTrials}) · ` +
          `Phone Catch: <b>${sum.phoneCatchPct}%</b> (${sum.phoneWins}/${sum.totalTrials}) · ` +
          `Net: <span class="pill ${sum.deltaPts >= 0 ? 'ok' : ''}">${sign}${sum.deltaPts} pts${relTxt}</span>${agreeTxt} · ` +
          `Room-Only Saves: <b>${sum.roomOnly}</b> · Phone-Only: <b>${sum.phoneOnly}</b>`;
      }
    }
    const runsEl = $('vxAbRuns');
    if (runsEl) {
      const runs = Array.isArray(ab.runs) ? ab.runs : [];
      if (!runs.length) {
        runsEl.innerHTML = '';
      } else {
        runsEl.innerHTML = `<div style="margin:6px 0 4px;font-weight:700;color:var(--fg)">Saved 10-Phrase Shootout Runs</div>` +
          `<table><thead><tr><th>Placement</th><th>Phrases</th><th>Room Intent</th><th>Phone Intent</th><th>Improvement</th><th>Room Word %</th><th>Phone Word %</th></tr></thead><tbody>` +
          runs.map(r => {
            const dSign = r.deltaPts > 0 ? '+' : '';
            const ts = new Date(r.tsMs).toLocaleDateString([], { month: 'short', day: 'numeric' });
            return `<tr><td><b>${r.label}</b> <small>(${ts})</small></td><td>${r.phrasesCount}</td><td>${r.roomIntentPct}%</td><td>${r.phoneIntentPct}%</td><td><span class="pill ${r.deltaPts >= 0 ? 'ok' : ''}">${dSign}${r.deltaPts} pts</span></td><td>${r.roomWordPct}%</td><td>${r.phoneWordPct}%</td></tr>`;
          }).join('') +
          `</tbody></table>`;
      }
    }
    const trEl = $('vxAbTrials');
    if (trEl) {
      const trials = Array.isArray(ab.recentTrials) ? ab.recentTrials.slice(0, 12) : [];
      if (!trials.length) {
        trEl.innerHTML = '';
      } else {
        trEl.innerHTML = `<div style="margin:6px 0 4px;font-weight:700;color:var(--fg)">Recent Workout A/B Trials</div>` +
          `<table><thead><tr><th>Station</th><th>Room Mic (S330)</th><th>Phone Mic (Pixel)</th><th>Outcome</th></tr></thead><tbody>` +
          trials.map(t => {
            const rTxt = t.room ? `${t.room.transcript || '—'} <small>(${t.room.intentLabel || t.room.intentKey})</small>` : '—';
            const pTxt = t.phone ? `${t.phone.transcript || '—'} <small>(${t.phone.intentLabel || t.phone.intentKey})</small>` : '—';
            const resolveBtns = t.outcome === 'disagree' && !t.resolvedWinner
              ? ` <button class="btn sec sm" type="button" onclick="resolveAbTrial('${t.id}','room')">Room ✓</button> <button class="btn sec sm" type="button" onclick="resolveAbTrial('${t.id}','phone')">Phone ✓</button>`
              : (t.resolvedWinner ? ` (${t.resolvedWinner} ✓)` : '');
            return `<tr><td>${t.station || '—'}</td><td>${rTxt}</td><td>${pTxt}</td><td>${t.outcome}${resolveBtns}</td></tr>`;
          }).join('') +
          `</tbody></table>`;
      }
    }
  } catch {}
}

async function pollStatus() {
  try {
    const st = await fetch('/api/status').then(r => r.json());
    latestStatusData = st;
    const badge = $('svcBadge');
    badge.textContent = st.recording ? (st.paused ? 'PAUSED' : 'RECORDING') : 'STANDBY RING';
    badge.className = 'pill ' + (st.recording ? 'ok' : 'live');
    $('subInfo').innerHTML = `Storage: <code>${escHtml(st.disk.root)}</code> · <b>${st.disk.freeGb} GB</b> free / ${st.disk.totalGb} GB`;
    if ($('navBadgeRet') && cfg && cfg.retentionDays) $('navBadgeRet').textContent = `${cfg.retentionDays}d`;
    const bySlug = new Map((st.cameras || []).map(sc => [sc.slug, sc]));
    (cfg?.cameras || []).forEach((cam, idx) => {
      const c = bySlug.get(cam.slug) || st.cameras[idx];
      if (!c) return;
      const sEl = $(`camState-${idx}`);
      if (sEl) {
        sEl.textContent = c.connected ? 'LIVE 25 FPS' : 'RECONNECTING';
        sEl.className = 'pill ' + (c.connected ? 'ok' : '');
      }
      const eEl = $(`energyTxt-${idx}`);
      const mEl = $(`mbar-${idx}`);
      if (eEl) eEl.textContent = `Motion: ${c.latestEnergy.toFixed(2)} / ${c.motionThreshold}`;
      if (mEl) {
        mEl.style.width = clamp((c.latestEnergy / 20) * 100, 0, 100) + '%';
        mEl.classList.toggle('hot', c.latestEnergy >= c.motionThreshold);
      }
      if (mmState.open && mmState.camIdx === idx) {
        const mmTxt = $('mmMotionTxt');
        const mmBar = $('mmMotionBar');
        if (mmTxt) mmTxt.textContent = `Motion: ${c.latestEnergy.toFixed(2)} / ${c.motionThreshold}`;
        if (mmBar) {
          mmBar.style.width = clamp((c.latestEnergy / 20) * 100, 0, 100) + '%';
          mmBar.classList.toggle('hot', c.latestEnergy >= c.motionThreshold);
        }
      }
    });
    if (st.wallboard) {
      const wb = st.wallboard;
      const wbBadge = $('wbBadge');
      const stTxt = (wb.adbState || 'unconfigured').toUpperCase();
      wbBadge.textContent = wb.workoutActive ? `LIVE · ${(wb.mode || 'WORKOUT').toUpperCase()}` : stTxt;
      wbBadge.className = 'pill ' + ((wb.adbState === 'awake' || wb.adbState === 'ready' || wb.workoutActive) ? 'ok' : 'live');
      const parts = [
        `URL: <code>${escHtml(wb.wallboardUrl)}</code>`,
        `SSE Displays: <b>${Number(wb.clients || 0)}</b>`,
        `Mode: <b>${escHtml(wb.mode || 'standby')}</b>`,
        wb.adbBinFound ? (wb.fireTvModel ? `TV: <b>${escHtml(wb.fireTvModel)}</b>` : (wb.fireTvHost ? `TV: <code>${escHtml(wb.fireTvHost)}</code>` : 'TV IP not set yet')) : 'adb.exe missing',
      ];
      if (wb.pendingSleepSec > 0) parts.push(`Sleep in ${wb.pendingSleepSec}s`);
      if (wb.lastError) parts.push(`<span style="color:var(--amber)">${escHtml(wb.lastError)}</span>`);
      else if (wb.lastAction) parts.push(`Last: ${escHtml(wb.lastAction)}`);
      $('wbMsg').innerHTML = parts.join(' · ');
    }
    if (st.calibration) {
      const cal = st.calibration;
      const calEl = $('calBadge');
      if (calEl) {
        calEl.textContent = cal.nextMilestone
          ? `${cal.clipCount} / ${cal.nextMilestone} clips for pose calibration`
          : `${cal.clipCount} clips · 50+ full calibration ready`;
        calEl.className = 'pill ' + (cal.clipCount >= 5 ? 'ok' : '');
      }
    }
    if (st.voice) {
      syncVoiceUI(st.voice);
    }
    if (st.houseAudio) {
      syncHouseAudioUI(st.houseAudio);
    }
    if (st.displays) {
      syncDisplaysUI(st.displays);
    }
    if (st.shows && st.shows.active) {
      syncShowActiveBadge(st.shows.active);
    }
    syncOverview();
  } catch {}
}

function escHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const HA_LISTEN_PRESETS = {
  off:      { mode: 'off',          thrVol: 45, maxDrop: 24.0, exp: 1.55, floorVol: 45 },
  mild:     { mode: 'proportional', thrVol: 50, maxDrop: 10.0, exp: 1.50, floorVol: 45 },
  balanced: { mode: 'proportional', thrVol: 45, maxDrop: 16.0, exp: 1.55, floorVol: 45 },
  deep:     { mode: 'proportional', thrVol: 45, maxDrop: 24.0, exp: 1.55, floorVol: 45 },
};
const HA_OUT_PRESETS = {
  off:      { mode: 'off',          thrVol: 50, maxDrop: 10.0, exp: 1.50, floorVol: 45 },
  mild:     { mode: 'proportional', thrVol: 50, maxDrop: 10.0, exp: 1.50, floorVol: 45 },
  balanced: { mode: 'proportional', thrVol: 45, maxDrop: 14.0, exp: 1.55, floorVol: 45 },
  match:    { mode: 'match',        thrVol: 50, maxDrop: 10.0, exp: 1.50, floorVol: 45 },
};

function _calcCurveDrop(vol, mode, thrVol, maxDrop, exp, floorVol) {
  if (mode === 'off') return { drop: 0, target: vol };
  if (vol < thrVol) return { drop: 0, target: vol };
  let drop = 0;
  if (mode === 'fixed') {
    drop = Math.max(0.5, maxDrop);
  } else {
    const span = Math.max(5, 75 - thrVol);
    const frac = Math.min(1, Math.max(0, (vol - thrVol) / span));
    drop = Math.round(maxDrop * Math.pow(frac, Math.max(1, exp)) * 2) / 2;
  }
  const target = Math.round(Math.max(floorVol, vol - drop) * 2) / 2;
  const effDrop = Math.round((vol - target) * 2) / 2;
  return { drop: effDrop, target };
}

function updateDuckCurvePreviews() {
  const lMode = $('haListenDuckMode')?.value || 'off';
  const lThr = Number($('haDuckThreshVol')?.value ?? 45);
  const lMax = Number($('haDuckMaxDrop')?.value ?? 24);
  const lExp = Number($('haDuckExp')?.value ?? 1.55);
  const lFlr = Number($('haDuckFloorVol')?.value ?? 45);

  const lOff = lMode === 'off';
  if ($('haDuckDropLbl')) $('haDuckDropLbl').textContent = lMode === 'fixed' ? 'Fixed Dip (dB)' : 'Max Dip @ 75 (dB)';
  ['haDuckThreshVol', 'haDuckMaxDrop', 'haDuckFloorVol'].forEach(id => { if ($(id)) $(id).disabled = lOff; });
  if ($('haDuckExp')) $('haDuckExp').disabled = lMode !== 'proportional';

  const rungs = [45, 55, 65, 70, 75];
  if ($('haListenPreview')) {
    if (lOff) {
      $('haListenPreview').innerHTML = '<b>Off:</b> Music stays untouched when you say “Hey Jarvis”.';
    } else {
      const cells = rungs.map(v => {
        const r = _calcCurveDrop(v, lMode, lThr, lMax, lExp, lFlr);
        return `<span style="white-space:nowrap">Vol <b>${v}</b>: ${r.drop > 0 ? `<b style="color:#38bdf8">-${r.drop} dB</b> → ${r.target}` : '0 dB'}</span>`;
      });
      $('haListenPreview').innerHTML = cells.join(' &nbsp;·&nbsp; ');
    }
  }
  document.querySelectorAll('#haListenPresets [data-lpreset]').forEach(btn => {
    const p = HA_LISTEN_PRESETS[btn.dataset.lpreset];
    const match = p && p.mode === lMode && (p.mode === 'off' || (Math.abs(p.thrVol - lThr) < 0.5 && Math.abs(p.maxDrop - lMax) < 0.5 && Math.abs(p.exp - lExp) < 0.05));
    btn.classList.toggle('ok', !!match);
    btn.classList.toggle('sec', !match);
  });

  const oMode = $('haOutDuckMode')?.value || 'match';
  const oThr = Number($('haOutDuckThreshVol')?.value ?? 50);
  const oMax = Number($('haOutDuckMaxDrop')?.value ?? 10);
  const oExp = Number($('haOutDuckExp')?.value ?? 1.5);
  const oFlr = Number($('haOutDuckFloorVol')?.value ?? 45);

  const oDisabled = (oMode === 'off' || oMode === 'match');
  if ($('haOutDuckDropLbl')) $('haOutDuckDropLbl').textContent = oMode === 'fixed' ? 'Fixed Dip (dB)' : 'Max Dip @ 75 (dB)';
  ['haOutDuckThreshVol', 'haOutDuckMaxDrop', 'haOutDuckFloorVol'].forEach(id => { if ($(id)) $(id).disabled = oDisabled; });
  if ($('haOutDuckExp')) $('haOutDuckExp').disabled = oMode !== 'proportional';

  if ($('haOutPreview')) {
    if (oMode === 'off') {
      $('haOutPreview').innerHTML = '<b>Off:</b> Music restores immediately while Jarvis speaks over the towers.';
    } else if (oMode === 'match') {
      if (lOff) {
        $('haOutPreview').innerHTML = '<b>Match Listening:</b> Listening Duck is Off, so no dip during Jarvis’s reply either.';
      } else {
        const cells = rungs.map(v => {
          const r = _calcCurveDrop(v, lMode, lThr, lMax, lExp, lFlr);
          return `<span style="white-space:nowrap">Vol <b>${v}</b>: ${r.drop > 0 ? `<b style="color:#4ade80">-${r.drop} dB</b> → ${r.target}` : '0 dB'}</span>`;
        });
        $('haOutPreview').innerHTML = `<span style="color:var(--mut)">Holds Listening dip:</span> ` + cells.join(' &nbsp;·&nbsp; ');
      }
    } else {
      const cells = rungs.map(v => {
        const r = _calcCurveDrop(v, oMode, oThr, oMax, oExp, oFlr);
        return `<span style="white-space:nowrap">Vol <b>${v}</b>: ${r.drop > 0 ? `<b style="color:#4ade80">-${r.drop} dB</b> → ${r.target}` : '0 dB'}</span>`;
      });
      $('haOutPreview').innerHTML = cells.join(' &nbsp;·&nbsp; ');
    }
  }
  document.querySelectorAll('#haOutPresets [data-opreset]').forEach(btn => {
    const p = HA_OUT_PRESETS[btn.dataset.opreset];
    const match = p && p.mode === oMode && (p.mode === 'off' || p.mode === 'match' || (Math.abs(p.thrVol - oThr) < 0.5 && Math.abs(p.maxDrop - oMax) < 0.5 && Math.abs(p.exp - oExp) < 0.05));
    btn.classList.toggle('ok', !!match);
    btn.classList.toggle('sec', !match);
  });
}

function populateHouseAudioInputs(ha) {
  if (!ha) return;
  $('haRoute').value = ha.outputRoute || 's330';
  $('haDenonHost').value = ha.denonHost || '192.168.1.15';
  const lMode = !ha.denonDuckEnabled ? 'off' : (ha.denonDuckMode === 'proportional' ? 'proportional' : 'fixed');
  if ($('haListenDuckMode')) $('haListenDuckMode').value = lMode;
  const lThrDb = ha.denonDuckThresholdDb != null ? Number(ha.denonDuckThresholdDb) : -35.0;
  if ($('haDuckThreshVol')) $('haDuckThreshVol').value = String(Math.round(lThrDb + 80));
  const lDropVal = lMode === 'fixed' ? (ha.denonDuckDropDb != null ? ha.denonDuckDropDb : 5.0) : (ha.denonDuckMaxDropDb != null ? ha.denonDuckMaxDropDb : 24.0);
  if ($('haDuckMaxDrop')) $('haDuckMaxDrop').value = String(lDropVal);
  if ($('haDuckExp')) $('haDuckExp').value = String(ha.denonDuckExponent != null ? ha.denonDuckExponent : 1.55);
  const lFlrDb = ha.denonDuckFloorDb != null ? Number(ha.denonDuckFloorDb) : -35.0;
  if ($('haDuckFloorVol')) $('haDuckFloorVol').value = String(Math.round(lFlrDb + 80));

  const oMode = ha.denonOutputDuckMode || 'match';
  if ($('haOutDuckMode')) $('haOutDuckMode').value = oMode;
  const oThrDb = ha.denonOutputDuckThresholdDb != null ? Number(ha.denonOutputDuckThresholdDb) : -30.0;
  if ($('haOutDuckThreshVol')) $('haOutDuckThreshVol').value = String(Math.round(oThrDb + 80));
  const oDropVal = oMode === 'fixed' ? (ha.denonOutputDuckDropDb != null ? ha.denonOutputDuckDropDb : 5.0) : (ha.denonOutputDuckMaxDropDb != null ? ha.denonOutputDuckMaxDropDb : 12.0);
  if ($('haOutDuckMaxDrop')) $('haOutDuckMaxDrop').value = String(oDropVal);
  if ($('haOutDuckExp')) $('haOutDuckExp').value = String(ha.denonOutputDuckExponent != null ? ha.denonOutputDuckExponent : 1.50);
  const oFlrDb = ha.denonOutputDuckFloorDb != null ? Number(ha.denonOutputDuckFloorDb) : -35.0;
  if ($('haOutDuckFloorVol')) $('haOutDuckFloorVol').value = String(Math.round(oFlrDb + 80));

  $('haCubeHost').value = ha.cubeHost || '';
  $('haCubeEnabled').value = ha.cubeEnabled ? '1' : '0';
  $('haReadMode').value = ha.readbackMode || 'overlay';
  $('haChimeMode').value = ha.chimeMode || 'overlay';
  $('haHypeMode').value = ha.hypeMode || 'takeover';
  $('haUsage').value = ha.cubeUsage || 'media';
  $('haReadGain').value = String(ha.readbackGain != null ? ha.readbackGain : 0.85);
  $('haSfxGain').value = String(ha.sfxGain != null ? ha.sfxGain : 0.90);
  updateDuckCurvePreviews();
}

function renderAudioMatrix(mx) {
  const el = $('haMatrixTable');
  if (!el || !mx || !Array.isArray(mx.apps)) return;
  const modes = Array.isArray(mx.modes) ? mx.modes : ['overlay', 'duck', 'takeover'];
  const cells = mx.cells || {};
  const pillCls = { pass: 'ok', warn: 'warn', fail: 'err', untested: '' };
  el.innerHTML = `<table><thead><tr><th>Gym App</th>${modes.map(m => `<th>${escHtml(m.toUpperCase())}</th>`).join('')}</tr></thead><tbody>` +
    mx.apps.map(app => {
      const row = cells[app] || {};
      const cols = modes.map(mode => {
        const entry = row[mode] || { status: 'untested', behavior: '' };
        const st = entry.status || 'untested';
        const beh = entry.behavior ? ` <small>${escHtml(entry.behavior)}</small>` : '';
        return `<td style="cursor:pointer" data-mxapp="${escHtml(app)}" data-mxmode="${escHtml(mode)}"><span class="pill ${pillCls[st] || ''}">${escHtml(st.toUpperCase())}</span>${beh}</td>`;
      }).join('');
      return `<tr><td><b>${escHtml(app)}</b></td>${cols}</tr>`;
    }).join('') +
    `</tbody></table>`;
}

function syncHouseAudioUI(ha) {
  if (!ha) return;
  const d = ha.denon || {};
  const dp = $('haDenonPill');
  if (dp) {
    if (!d.ok) {
      dp.textContent = d.disabled ? 'DENON OFF' : 'DENON OFFLINE';
      dp.className = 'pill';
    } else {
      const volTxt = d.muted ? 'MUTED' : (d.volumeDb != null ? `${d.volumeDb} dB` : '--');
      dp.textContent = `DENON ${d.power || 'ON'} · ${d.input || '?'} · ${volTxt}`;
      dp.className = 'pill ' + (d.power === 'ON' ? 'ok' : 'live');
    }
  }
  const c = ha.cube || {};
  const cp = $('haCubePill');
  if (cp) {
    if (c.unconfigured) {
      cp.textContent = 'CUBE UNCONFIGURED';
      cp.className = 'pill';
    } else if (c.ok) {
      cp.textContent = `CUBE ONLINE${c.playing ? ' · PLAYING' : ''}`;
      cp.className = 'pill ok';
    } else {
      cp.textContent = 'CUBE OFFLINE';
      cp.className = 'pill warn';
    }
  }
  const msgEl = $('haMsg');
  if (msgEl) {
    const cCfg = ha.config || {};
    const lThrV = Math.round((cCfg.denonDuckThresholdDb != null ? Number(cCfg.denonDuckThresholdDb) : -35) + 80);
    const lDesc = !cCfg.denonDuckEnabled ? 'OFF' : (cCfg.denonDuckMode === 'proportional' ? `Exp >Vol ${lThrV} (max -${cCfg.denonDuckMaxDropDb ?? 24} dB)` : `Fixed >Vol ${lThrV} (-${cCfg.denonDuckDropDb ?? 5} dB)`);
    const oModeC = cCfg.denonOutputDuckMode || 'match';
    const oThrV = Math.round((cCfg.denonOutputDuckThresholdDb != null ? Number(cCfg.denonOutputDuckThresholdDb) : -30) + 80);
    const oDesc = oModeC === 'match' ? 'Match Listen' : (oModeC === 'off' ? 'OFF' : (oModeC === 'proportional' ? `Exp >Vol ${oThrV} (max -${cCfg.denonOutputDuckMaxDropDb ?? 12} dB)` : `Fixed -${cCfg.denonOutputDuckDropDb ?? 5} dB`));
    const parts = [
      `Active Route: <b>${escHtml((ha.activeRoute || 's330').toUpperCase())}</b>`,
      `Listen Duck: <b>${escHtml(lDesc)}</b>`,
      `Speak Duck: <b>${escHtml(oDesc)}</b>`,
    ];
    if (d.ok) {
      parts.push(`Surround: <b>${escHtml(d.surround || 'Unknown')}</b>`);
    } else if (d.error) {
      parts.push(`<span style="color:var(--amber)">Denon: ${escHtml(d.error)}</span>`);
    }
    if (c.ok) {
      parts.push(`Cube Vol: <b>${c.streamVol}/${c.streamMaxVol}</b> · Plays: <b>${c.playCount || 0}</b>${c.musicActive ? ' · Music Active' : ''}`);
    } else if (!c.unconfigured && c.error) {
      parts.push(`<span style="color:var(--amber)">Cube: ${escHtml(c.error)}</span>`);
    }
    msgEl.innerHTML = parts.join(' · ');
  }
  const tgtSel = $('haTestTarget');
  if (tgtSel) {
    const autoOpt = tgtSel.querySelector('option[value="auto"]');
    if (autoOpt) {
      autoOpt.textContent = `Auto Route (→ ${(ha.activeRoute || 's330').toUpperCase()})`;
    }
  }
  const sfxSel = $('haSfxSel');
  if (sfxSel && Array.isArray(ha.sfxFiles) && document.activeElement !== sfxSel) {
    const curSfx = sfxSel.value;
    if (!ha.sfxFiles.length) {
      sfxSel.innerHTML = '<option value="">(None placed yet)</option>';
    } else {
      sfxSel.innerHTML = ha.sfxFiles.map(f => `<option value="${escHtml(f.name)}">${escHtml(f.name)} (${Math.max(1, Math.round(f.bytes / 1024))} KB)</option>`).join('');
      if (curSfx && ha.sfxFiles.some(f => f.name === curSfx)) sfxSel.value = curSfx;
    }
  }
  if (ha.matrix) {
    renderAudioMatrix(ha.matrix);
  }
}

async function pollAudioStatus() {
  try {
    const ha = await fetch('/api/audio/status').then(r => r.json());
    latestAudioData = ha;
    if (ha && ha.config && cfg) cfg.houseAudio = ha.config;
    syncHouseAudioUI(ha);
    syncOverview();
  } catch {}
}

async function saveHouseAudioConfig(triggerBtn) {
  const lMode = $('haListenDuckMode')?.value || 'off';
  const lThrVol = Number($('haDuckThreshVol')?.value ?? 45);
  const lDropVal = Number($('haDuckMaxDrop')?.value ?? 24);
  const lExp = Number($('haDuckExp')?.value ?? 1.55);
  const lFloorVol = Number($('haDuckFloorVol')?.value ?? 45);

  const oMode = $('haOutDuckMode')?.value || 'match';
  const oThrVol = Number($('haOutDuckThreshVol')?.value ?? 50);
  const oDropVal = Number($('haOutDuckMaxDrop')?.value ?? 10);
  const oExp = Number($('haOutDuckExp')?.value ?? 1.50);
  const oFloorVol = Number($('haOutDuckFloorVol')?.value ?? 45);

  const body = {
    outputRoute: $('haRoute').value || 's330',
    denonHost: $('haDenonHost').value.trim() || '192.168.1.15',
    denonDuckEnabled: lMode !== 'off',
    denonDuckMode: lMode === 'fixed' ? 'fixed' : 'proportional',
    denonDuckThresholdDb: lThrVol - 80,
    denonDuckDropDb: lMode === 'fixed' ? lDropVal : 5.0,
    denonDuckMaxDropDb: lMode === 'proportional' ? lDropVal : (cfg?.houseAudio?.denonDuckMaxDropDb ?? 24.0),
    denonDuckSpanDb: Math.max(5, 75 - lThrVol),
    denonDuckExponent: lExp,
    denonDuckFloorDb: lFloorVol - 80,
    denonOutputDuckMode: oMode,
    denonOutputDuckThresholdDb: oThrVol - 80,
    denonOutputDuckDropDb: oMode === 'fixed' ? oDropVal : 5.0,
    denonOutputDuckMaxDropDb: oMode === 'proportional' ? oDropVal : (cfg?.houseAudio?.denonOutputDuckMaxDropDb ?? 10.0),
    denonOutputDuckSpanDb: Math.max(5, 75 - oThrVol),
    denonOutputDuckExponent: oExp,
    denonOutputDuckFloorDb: oFloorVol - 80,
    cubeHost: $('haCubeHost').value.trim(),
    cubeEnabled: $('haCubeEnabled').value === '1',
    readbackMode: $('haReadMode').value || 'overlay',
    chimeMode: $('haChimeMode').value || 'overlay',
    hypeMode: $('haHypeMode').value || 'takeover',
    cubeUsage: $('haUsage').value || 'media',
    readbackGain: Number($('haReadGain').value) || 0.85,
    sfxGain: Number($('haSfxGain').value) || 0.90,
  };
  try {
    const r = await fetch('/api/audio/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(x => x.json());
    if (!r || !r.ok) throw new Error('Rejected');
    if (cfg) cfg.houseAudio = r.config;
    populateHouseAudioInputs(r.config);
    syncHouseAudioUI(r.status);
    $('haOut').textContent = 'House audio settings saved!';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'saved', '✓ Saved!', 2000);
  } catch {
    $('haOut').textContent = 'Save failed';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'save-err', '✗ Failed', 2500);
  }
}

async function runHouseAudioTest(kind, mode, sfxFile = '') {
  $('haTestOut').textContent = `Running ${kind}${mode ? ` (${mode})` : ''}…`;
  try {
    const r = await fetch('/api/audio/test-play', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        kind,
        mode,
        usage: $('haUsage').value || 'media',
        target: $('haTestTarget').value || 'auto',
        text: $('haTestText').value.trim() || '225 for 8, got it',
        sfxFile,
        withDenonDuck: $('haTestDenonDuck').checked,
      }),
    }).then(x => x.json());
    const ms = r.timing && typeof r.timing.totalMs === 'number' ? ` · ${r.timing.totalMs} ms` : '';
    $('haTestOut').textContent = r.ok ? `✓ Played via ${(r.route || '').toUpperCase()} (${r.mode || mode})${ms}` : `✗ ${r.error || 'Failed'}`;
  } catch {
    $('haTestOut').textContent = '✗ Request failed';
  }
}

function populateDisplaysInputs(dc) {
  if (!dc) return;
  const p = dc.primary || {};
  const s = dc.spin || {};
  $('dpPrimHost').value = p.host || '192.168.1.230';
  $('dpPrimMac').value = p.mac || 'bc:09:b9:e7:35:26';
  $('dpPrimHdmi1').value = p.hdmiOptiplexInput || 'KEYCODE_TV_INPUT_HDMI_1';
  $('dpPrimHdmi2').value = p.hdmiDenonInput || 'KEYCODE_TV_INPUT_HDMI_2';
  $('dpSpinHost').value = s.host || '';
  $('dpSpinMac').value = s.mac || '';
  $('dpSpinHdmi').value = s.hdmiInput || 'InputHDMI1';
  $('dpSpinDual').value = s.autoDenonDual === false ? '0' : '1';
  if ($('dpPrimDenonMon')) $('dpPrimDenonMon').value = s.primaryDenonMonitor === '2' ? '2' : '1';
  if ($('dpOpenMusic')) $('dpOpenMusic').value = dc.openGymPlayMusic === false ? '0' : '1';
  if ($('dpCubeHost')) $('dpCubeHost').value = dc.cubeHost || '192.168.1.151';
  if ($('dpMusicUrl')) $('dpMusicUrl').value = dc.openGymMusicUrl || '';
  if ($('dpMusicQuery')) $('dpMusicQuery').value = dc.openGymMusicQuery || 'workout music mix';
}

function syncDisplaysUI(ds) {
  if (!ds) return;
  const spin = ds.spin || {};
  const prim = (ds.primary && ds.primary.probe) || {};
  const cube = (ds.cube && ds.cube.probe) || {};
  const badge = $('dpBadge');
  if (badge) {
    if (spin.ok) {
      badge.textContent = `TV · SMARTSPIN ${spin.powerMode === 'PowerOn' ? 'ON' : 'READY'}`;
      badge.className = 'pill ok';
    } else if (prim.ok) {
      badge.textContent = `TV ${prim.displayPower || 'OK'}`;
      badge.className = 'pill ok';
    } else if (cube.ok) {
      badge.textContent = 'TV READY';
      badge.className = 'pill ok';
    } else {
      badge.textContent = 'TV READY';
      badge.className = 'pill live';
    }
    if (cube.ok) {
      badge.textContent += ` · CUBE ${cube.displayPower || cube.wakefulness || 'OK'}`;
    }
  }
}

async function saveDisplaysConfig(triggerBtn) {
  const body = {
    primary: {
      host: $('dpPrimHost').value.trim(),
      mac: $('dpPrimMac').value.trim(),
      hdmiOptiplexInput: $('dpPrimHdmi1').value.trim(),
      hdmiDenonInput: $('dpPrimHdmi2').value.trim(),
    },
    spin: {
      host: $('dpSpinHost').value.trim(),
      mac: $('dpSpinMac').value.trim(),
      hdmiInput: $('dpSpinHdmi').value.trim() || 'InputHDMI1',
      autoDenonDual: $('dpSpinDual').value !== '0',
      primaryDenonMonitor: $('dpPrimDenonMon') ? $('dpPrimDenonMon').value : '1',
    },
    cubeHost: ($('dpCubeHost') ? $('dpCubeHost').value.trim() : '') || '192.168.1.151',
    openGymPlayMusic: $('dpOpenMusic') ? ($('dpOpenMusic').value !== '0') : true,
    openGymMusicUrl: $('dpMusicUrl') ? $('dpMusicUrl').value.trim() : '',
    openGymMusicQuery: ($('dpMusicQuery') ? $('dpMusicQuery').value.trim() : '') || 'workout music mix',
  };
  try {
    const r = await fetch('/api/displays/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(x => x.json());
    if (!r || !r.ok) throw new Error('Rejected');
    if (cfg) cfg.displays = r.config;
    populateDisplaysInputs(r.config);
    syncDisplaysUI(r.status);
    $('dpOut').textContent = 'Display settings saved!';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'saved', '✓ Saved!', 2000);
  } catch {
    $('dpOut').textContent = 'Save failed';
    if (triggerBtn) setBtnFeedback(triggerBtn, 'save-err', '✗ Failed', 2500);
  }
}

async function runDisplayAction(target, action) {
  if ($('dpOut')) $('dpOut').textContent = `Running ${target} ${action}…`;
  if ($('ovDeckOut')) $('ovDeckOut').textContent = `Running ${target} ${action}…`;
  try {
    const r = await fetch('/api/displays/action', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ target, action }),
    }).then(x => x.json());
    const txt = r.ok ? `✓ ${r.message || 'Done'}` : `✗ ${r.error || r.message || 'Failed'}`;
    if ($('dpOut')) $('dpOut').textContent = txt;
    if ($('ovDeckOut')) $('ovDeckOut').textContent = txt;
    showToast(txt, r.ok ? 'ok' : 'err');
    if (target === 'avr' || target === 'gym' || target === 'open_gym' || target === 'close_gym' || target === 'spin' || target === 'music' || target === 'cube') await pollAudioStatus();
  } catch {
    if ($('dpOut')) $('dpOut').textContent = '✗ Display command failed';
    if ($('ovDeckOut')) $('ovDeckOut').textContent = '✗ Display command failed';
  }
}

function pickDiscoveredRoku(host, mac) {
  $('dpSpinHost').value = host;
  if (mac) $('dpSpinMac').value = mac;
  saveDisplaysConfig($('dpSaveBtn'));
}

async function pollClips() {
  try {
    const clips = await fetch('/api/clips').then(r => r.json());
    latestClipRows = (clips && Array.isArray(clips.clips)) ? clips.clips : [];
    if (latestClipRows.length) {
      $('clipList').innerHTML = `<table><thead><tr><th>Clip</th><th>Camera</th><th>Duration</th><th>Size</th><th>Drive</th></tr></thead><tbody>` +
        latestClipRows.map(x => `<tr><td><a href="${escHtml(x.lanUrl)}" target="_blank">${escHtml(x.fileName)}</a></td><td>${escHtml(x.camera)}</td><td>${Number(x.durationSec || 0)}s</td><td>${(Number(x.bytes || 0)/1048576).toFixed(1)} MB</td><td>${x.driveUrl ? `<a href="${escHtml(x.driveUrl)}" target="_blank">Drive ↗</a>` : 'Syncing…'}</td></tr>`).join('') +
        `</tbody></table>`;
    } else {
      $('clipList').textContent = 'No clips saved yet.';
    }
    syncOverview();
  } catch {}
}

function pollLoop(fn, ms, pageFilter) {
  let busy = false;
  return setInterval(async () => {
    if (busy || document.hidden) return;
    if (pageFilter && !pageFilter()) return;
    busy = true;
    try { await fn(); } finally { busy = false; }
  }, ms);
}

function realignSlugFromHash() {
  const raw = String(window.location.hash || '').replace(/^#[/]?/, '');
  const m = /^realign=(.+)$/i.exec(raw);
  if (!m) return '';
  try { return decodeURIComponent(m[1]).trim(); } catch { return m[1].trim(); }
}

function unlockRealignAudio() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!raState.audioCtx) raState.audioCtx = new Ctx();
    if (raState.audioCtx.state === 'suspended') raState.audioCtx.resume().catch(() => {});
  } catch {}
}

function playRealignLockChime() {
  if (!raState.chimeOn) return;
  const nowMs = Date.now();
  if (nowMs - raState.lastChimeAt < 2500) return;
  raState.lastChimeAt = nowMs;
  try {
    unlockRealignAudio();
    const ctx = raState.audioCtx;
    if (!ctx) return;
    const t0 = ctx.currentTime + 0.01;
    [880, 1318.5].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, t0 + i * 0.11);
      gain.gain.setValueAtTime(0.0001, t0 + i * 0.11);
      gain.gain.linearRampToValueAtTime(0.11, t0 + i * 0.11 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.11 + 0.24);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(t0 + i * 0.11);
      osc.stop(t0 + i * 0.11 + 0.26);
    });
  } catch {}
}

function renderRealignCrops() {
  const svg = $('raCropsSvg');
  if (!svg) return;
  if (!raState.showCrops || !cfg || !cfg.cameras || !raState.slug) {
    svg.innerHTML = '';
    svg.hidden = true;
    return;
  }
  svg.hidden = false;
  const views = cfg.cameras.filter(c => c.enabled && sourceSlugOf(c) === raState.slug);
  const palette = ['#38bdf8', '#fbbf24', '#c084fc', '#4ade80'];
  const parts = [];
  views.forEach((v, vi) => {
    const col = palette[vi % palette.length];
    const cr = Array.isArray(v.crop) && v.crop.length === 4 ? v.crop : [0, 0, 1, 1];
    const x = +(cr[0] * 1600).toFixed(1);
    const y = +(cr[1] * 900).toFixed(1);
    const w = +((cr[2] - cr[0]) * 1600).toFixed(1);
    const h = +((cr[3] - cr[1]) * 900).toFixed(1);
    parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="none" stroke="${col}" stroke-width="2.6" stroke-dasharray="10 6" vector-effect="non-scaling-stroke"/>`);
    const tagW = Math.min(300, Math.max(108, v.name.length * 12.5 + 22));
    const tagH = 30;
    const tagX = clamp(x + 8, 8, 1592 - tagW);
    const hitsTopCenter = (y < 64) && (tagX + tagW > 600 && tagX < 1000);
    const tagY = hitsTopCenter ? clamp(y + h - tagH - 10, 8, 892 - tagH) : clamp(y + 8, 8, 892 - tagH);
    parts.push(`<rect x="${tagX}" y="${tagY}" width="${tagW}" height="${tagH}" rx="6" fill="rgba(5,8,13,0.86)" stroke="${col}" stroke-width="1.5"/>`);
    parts.push(`<text x="${tagX + 10}" y="${tagY + 20}" fill="${col}" font-size="18" font-weight="800" font-family="system-ui,sans-serif">${esc(v.name)}</text>`);
    (v.exclusions || []).forEach(q => {
      const nq = normQuad(q);
      if (!nq) return;
      const pts = nq.map(p => `${(p[0] * 1600).toFixed(1)},${(p[1] * 900).toFixed(1)}`).join(' ');
      parts.push(`<polygon points="${pts}" fill="rgba(239,68,68,0.16)" stroke="rgba(248,113,113,0.85)" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>`);
    });
  });
  svg.innerHTML = parts.join('');
}

function refreshRealignOverlay(bust = true) {
  const img = $('raOverlayImg');
  if (!img) return;
  const colorGrp = $('raColorGrp');
  if (colorGrp) colorGrp.hidden = raState.mode !== 'edges';
  document.querySelectorAll('[data-ramode]').forEach(b => b.classList.toggle('on', b.dataset.ramode === raState.mode));
  document.querySelectorAll('[data-racolor]').forEach(b => b.classList.toggle('on', b.dataset.racolor === raState.color));
  if (raState.mode === 'off' || !raState.slug) {
    img.hidden = true;
    img.removeAttribute('src');
    return;
  }
  img.hidden = false;
  img.style.opacity = String(raState.opacity);
  const ts = bust ? `&t=${Date.now()}` : '';
  img.src = `/api/bump/overlay.png?camera=${encodeURIComponent(raState.slug)}&mode=${encodeURIComponent(raState.mode)}&color=${encodeURIComponent(raState.color)}${ts}`;
}

function syncRealignBlink() {
  if (raState.blinkTimer) { clearInterval(raState.blinkTimer); raState.blinkTimer = null; }
  const img = $('raOverlayImg');
  if (img) img.classList.remove('blink-off');
  if ($('raBlinkBtn')) $('raBlinkBtn').classList.toggle('on', raState.blink);
  if (raState.open && raState.blink && raState.mode !== 'off') {
    let off = false;
    raState.blinkTimer = setInterval(() => {
      off = !off;
      if ($('raOverlayImg')) $('raOverlayImg').classList.toggle('blink-off', off);
    }, 650);
  }
}

function updateRealignHud(d) {
  const stage = $('raStage');
  const pill = $('raStatusPill');
  const chips = $('raGuideChips');
  const metrics = $('raMetrics');
  const lockBadge = $('raLockBadge');
  const holdBadge = $('raHoldBadge');
  const setEdge = (id, on, hot, txt) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle('on', !!on);
    el.classList.toggle('hot', !!hot);
    if (txt) el.textContent = txt;
  };
  if (!d || !d.ok) {
    if (stage) stage.className = 'ra-stage';
    if (pill) { pill.className = 'ra-pill hold'; pill.textContent = 'CONNECTING'; }
    if (lockBadge) lockBadge.hidden = true;
    if (holdBadge) holdBadge.hidden = true;
    ['raArrUp', 'raArrDown', 'raArrLeft', 'raArrRight', 'raArrRoll'].forEach(id => setEdge(id, false, false));
    if (chips) chips.innerHTML = `<span class="ra-chip hold">${esc((d && d.error) || 'Waiting for live camera frame…')}</span>`;
    if (metrics) metrics.textContent = 'Waiting for live stream…';
    return;
  }
  if (!d.hasRef) {
    if (stage) stage.className = 'ra-stage';
    if (pill) { pill.className = 'ra-pill hold'; pill.textContent = 'NO BASELINE'; }
    if (lockBadge) lockBadge.hidden = true;
    if (holdBadge) holdBadge.hidden = true;
    ['raArrUp', 'raArrDown', 'raArrLeft', 'raArrRight', 'raArrRoll'].forEach(id => setEdge(id, false, false));
    if (chips) chips.innerHTML = '<span class="ra-chip nudge">No saved baseline yet — click "Accept New Framing" to save landmarks</span>';
    if (metrics) metrics.textContent = 'Baseline will be saved at 960×540.';
    return;
  }
  const respPct = Math.round((Number(d.response) || 0) * 100);
  const refDimTxt = Array.isArray(d.refDims) ? `${d.refDims[0]}×${d.refDims[1]}` : '320×180';
  const refDateTxt = d.refUpdatedMs ? new Date(d.refUpdatedMs).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
  const adaptBtn = $('raAdaptBtn');

  if (!d.confident) {
    raState.missStreak = (raState.missStreak || 0) + 1;
    // Hysteresis: hold the last good readout across up to 2 transient misses (~0.8s) so HUD never spazzes
    if (raState.lastGood && raState.missStreak < 3) {
      return;
    }
    if (stage) stage.className = 'ra-stage';
    if (pill) { pill.className = 'ra-pill hold'; pill.textContent = 'HOLD STEADY'; }
    if (lockBadge) lockBadge.hidden = true;
    if (holdBadge) holdBadge.hidden = false;
    if (adaptBtn) adaptBtn.disabled = true;
    ['raArrUp', 'raArrDown', 'raArrLeft', 'raArrRight', 'raArrRoll'].forEach(id => setEdge(id, false, false));
    if (chips) chips.innerHTML = '<span class="ra-chip hold">✋ Step back / hold steady — view obstructed or moving</span>';
    if (metrics) metrics.textContent = `Match ${respPct}% · Ref ${refDimTxt}${refDateTxt ? ' (' + refDateTxt + ')' : ''}`;
    raState.wasLocked = false;
    return;
  }
  raState.missStreak = 0;
  raState.lastGood = d;
  const dx = Number(d.dxPx || 0);
  const dy = Number(d.dyPx || 0);
  const sh = Number(d.shiftPx || 0);
  const ang = Number(d.dAngle || 0);
  const mTxt = d.method === 'orb' && d.inliers ? `ORB ${d.inliers}pts` : `Match ${respPct}%`;
  if (metrics) {
    metrics.textContent = `Shift ${sh.toFixed(1)} px (dx ${dx >= 0 ? '+' : ''}${dx.toFixed(1)}, dy ${dy >= 0 ? '+' : ''}${dy.toFixed(1)}) · Roll ${ang >= 0 ? '+' : ''}${ang.toFixed(2)}° · ${mTxt} · Ref ${refDimTxt}${refDateTxt ? ' (' + refDateTxt + ')' : ''}`;
  }
  if (adaptBtn) adaptBtn.disabled = false;
  if (holdBadge) holdBadge.hidden = true;
  if (d.aligned) {
    if (stage) stage.className = 'ra-stage locked';
    if (pill) { pill.className = 'ra-pill locked'; pill.textContent = `✓ LOCKED · ${sh.toFixed(1)}px`; }
    if (lockBadge) { lockBadge.hidden = false; lockBadge.textContent = `✓ LOCKED (${sh.toFixed(1)} px · ${Math.abs(ang).toFixed(1)}°)`; }
    ['raArrUp', 'raArrDown', 'raArrLeft', 'raArrRight', 'raArrRoll'].forEach(id => setEdge(id, false, false));
    if (chips) chips.innerHTML = `<span class="ra-chip ok">✓ ALIGNED — ${sh.toFixed(1)} px · ${Math.abs(ang).toFixed(1)}° · All crops &amp; masks locked</span>`;
    if (!raState.wasLocked) playRealignLockChime();
    raState.wasLocked = true;
    return;
  }
  raState.wasLocked = false;
  if (lockBadge) lockBadge.hidden = true;
  const isClose = !!d.close;
  if (stage) stage.className = isClose ? 'ra-stage close' : 'ra-stage shifted';
  if (pill) {
    pill.className = isClose ? 'ra-pill close' : 'ra-pill shifted';
    pill.textContent = `${isClose ? 'CLOSE' : 'SHIFTED'} · ${sh.toFixed(0)}px`;
  }
  const g = d.guide || {};
  const pPx = Number(g.pitchPx || 0);
  const yPx = Number(g.yawPx || 0);
  const rDeg = Number(g.rollDeg || 0);
  setEdge('raArrUp', g.pitch === 'up', pPx >= 15, `▲ TILT UP ${Math.round(pPx)}px`);
  setEdge('raArrDown', g.pitch === 'down', pPx >= 15, `▼ TILT DOWN ${Math.round(pPx)}px`);
  setEdge('raArrLeft', g.yaw === 'left', yPx >= 15, `◀ PAN LEFT ${Math.round(yPx)}px`);
  setEdge('raArrRight', g.yaw === 'right', yPx >= 15, `▶ PAN RIGHT ${Math.round(yPx)}px`);
  setEdge(
    'raArrRoll',
    g.roll === 'ccw' || g.roll === 'cw',
    rDeg >= 1.0,
    g.roll === 'ccw' ? `↺ ROLL CCW ${rDeg.toFixed(1)}°` : `↻ ROLL CW ${rDeg.toFixed(1)}°`
  );
  const chipHtml = [];
  const clsFor = (hot) => (hot ? 'ra-chip move' : 'ra-chip nudge');
  if (g.pitch === 'up') chipHtml.push(`<span class="${clsFor(pPx >= 15)}">↑ TILT UP ${Math.round(pPx)} px</span>`);
  else if (g.pitch === 'down') chipHtml.push(`<span class="${clsFor(pPx >= 15)}">↓ TILT DOWN ${Math.round(pPx)} px</span>`);
  if (g.yaw === 'left') chipHtml.push(`<span class="${clsFor(yPx >= 15)}">← PAN LEFT ${Math.round(yPx)} px</span>`);
  else if (g.yaw === 'right') chipHtml.push(`<span class="${clsFor(yPx >= 15)}">→ PAN RIGHT ${Math.round(yPx)} px</span>`);
  if (g.roll === 'ccw') chipHtml.push(`<span class="${clsFor(rDeg >= 1.0)}">↺ ROLL CCW ${rDeg.toFixed(1)}°</span>`);
  else if (g.roll === 'cw') chipHtml.push(`<span class="${clsFor(rDeg >= 1.0)}">↻ ROLL CW ${rDeg.toFixed(1)}°</span>`);
  if (!chipHtml.length) chipHtml.push(`<span class="ra-chip nudge">Tiny tweak needed (${sh.toFixed(1)} px)</span>`);
  if (chips) chips.innerHTML = chipHtml.join('');
}

async function pollRealignLive() {
  if (!raState.open || !raState.slug || document.hidden || raState.pollBusy) return;
  raState.pollBusy = true;
  if (raState.pollAbort) { try { raState.pollAbort.abort(); } catch {} }
  const ac = new AbortController();
  raState.pollAbort = ac;
  const slug = raState.slug;
  try {
    const r = await fetch(`/api/bump/live?camera=${encodeURIComponent(slug)}`, { cache: 'no-store', signal: ac.signal });
    const d = await r.json();
    if (raState.open && raState.slug === slug) updateRealignHud(d);
  } catch (err) {
    if (err && err.name === 'AbortError') return;
  } finally {
    raState.pollBusy = false;
    if (raState.open && raState.slug === slug) {
      raState.pollTimer = setTimeout(pollRealignLive, 380);
    }
  }
}

function openRealignModal(camSlugOrName) {
  if (!cfg || !cfg.cameras) return;
  const physList = cfg.cameras.filter(c => !c.sourceSlug && c.enabled);
  if (!physList.length) { showToast('No enabled physical cameras', 'err'); return; }
  const needle = String(camSlugOrName || '').trim().toLowerCase();
  const matched = physList.find(c => c.slug.toLowerCase() === needle || c.name.toLowerCase() === needle) || physList[0];
  if (mmState.open) closeMaskModal();
  unlockRealignAudio();
  raState.open = true;
  raState.slug = matched.slug;
  raState.wasLocked = false;
  raState.missStreak = 0;
  raState.lastGood = null;
  if (raState.pollTimer) { clearTimeout(raState.pollTimer); raState.pollTimer = null; }
  if (raState.pollAbort) { try { raState.pollAbort.abort(); } catch {} raState.pollAbort = null; }
  const sel = $('raCamSel');
  if (sel) {
    sel.innerHTML = physList.map(p => `<option value="${esc(p.slug)}"${p.slug === matched.slug ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
  }
  const modal = $('realignModal');
  if (modal) modal.hidden = false;
  document.body.style.overflow = 'hidden';
  if ($('raOpacity')) $('raOpacity').value = String(Math.round(raState.opacity * 100));
  if ($('raCropsBtn')) $('raCropsBtn').classList.toggle('on', raState.showCrops);
  if ($('raChimeBtn')) $('raChimeBtn').classList.toggle('on', raState.chimeOn);
  syncRawFeeds();
  renderRealignCrops();
  refreshRealignOverlay(true);
  syncRealignBlink();
  updateRealignHud(null);
  drawAllCanvases();
  pollRealignLive();
}

function closeRealignModal() {
  if (!raState.open) return;
  raState.open = false;
  if (raState.pollTimer) { clearTimeout(raState.pollTimer); raState.pollTimer = null; }
  if (raState.pollAbort) { try { raState.pollAbort.abort(); } catch {} raState.pollAbort = null; }
  if (raState.blinkTimer) { clearInterval(raState.blinkTimer); raState.blinkTimer = null; }
  const modal = $('realignModal');
  if (modal) modal.hidden = true;
  document.body.style.overflow = '';
  if (realignSlugFromHash()) {
    try { window.history.replaceState(null, '', '#cameras'); } catch {}
  }
  syncRawFeeds();
}

function wireRealignModal() {
  if ($('raCloseBtn')) $('raCloseBtn').onclick = () => closeRealignModal();
  if ($('raCamSel')) {
    $('raCamSel').onchange = (e) => {
      if (!raState.open) return;
      openRealignModal(e.target.value);
    };
  }
  document.querySelectorAll('[data-ramode]').forEach(btn => {
    btn.onclick = () => {
      raState.mode = btn.dataset.ramode || 'edges';
      if (raState.mode === 'ghost' && raState.opacity > 0.7) {
        raState.opacity = 0.45;
        if ($('raOpacity')) $('raOpacity').value = '45';
      } else if (raState.mode === 'edges' && raState.opacity < 0.55) {
        raState.opacity = 0.85;
        if ($('raOpacity')) $('raOpacity').value = '85';
      }
      refreshRealignOverlay(true);
      syncRealignBlink();
    };
  });
  document.querySelectorAll('[data-racolor]').forEach(btn => {
    btn.onclick = () => {
      raState.color = btn.dataset.racolor || 'cyan';
      refreshRealignOverlay(true);
    };
  });
  if ($('raOpacity')) {
    $('raOpacity').oninput = (e) => {
      raState.opacity = clamp(Number(e.target.value) / 100, 0.15, 1);
      if ($('raOverlayImg')) $('raOverlayImg').style.opacity = String(raState.opacity);
    };
  }
  if ($('raBlinkBtn')) {
    $('raBlinkBtn').onclick = () => {
      raState.blink = !raState.blink;
      syncRealignBlink();
    };
  }
  if ($('raCropsBtn')) {
    $('raCropsBtn').onclick = () => {
      raState.showCrops = !raState.showCrops;
      $('raCropsBtn').classList.toggle('on', raState.showCrops);
      renderRealignCrops();
    };
  }
  if ($('raChimeBtn')) {
    $('raChimeBtn').onclick = () => {
      raState.chimeOn = !raState.chimeOn;
      $('raChimeBtn').classList.toggle('on', raState.chimeOn);
      $('raChimeBtn').textContent = raState.chimeOn ? '🔊 Chime' : '🔇 Muted';
      if (raState.chimeOn) { raState.lastChimeAt = 0; playRealignLockChime(); }
    };
  }
  if ($('raAdaptBtn')) {
    $('raAdaptBtn').onclick = async () => {
      if (!raState.slug) return;
      const views = (cfg?.cameras || []).filter(c => sourceSlugOf(c) === raState.slug);
      const vNames = views.map(v => v.name).join(', ') || raState.slug;
      const lg = raState.lastGood;
      const deltaTxt = lg ? `Shift ${Number(lg.shiftPx || 0).toFixed(1)} px, Roll ${Number(lg.dAngle || 0) >= 0 ? '+' : ''}${Number(lg.dAngle || 0).toFixed(2)}°` : 'measured shift';
      if (!window.confirm(`Auto-shift crops, tilts, and mirror masks for: ${vNames}\n(${deltaTxt}) and save this position as the new baseline?`)) return;
      const btn = $('raAdaptBtn');
      setBtnFeedback(btn, 'saving', 'Adapting…');
      try {
        const r = await fetch('/api/bump/adapt_views', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ camera: raState.slug }),
        }).then(x => x.json());
        if (!r || !r.ok) throw new Error((r && r.error) || 'Failed');
        const freshCfg = await fetch('/api/config').then(x => x.json());
        if (freshCfg && Array.isArray(freshCfg.cameras)) {
          freshCfg.cameras.forEach(c => { c.exclusions = (Array.isArray(c.exclusions) ? c.exclusions : []).map(normQuad).filter(Boolean); });
          cfg.cameras = freshCfg.cameras;
          renderCameras();
        }
        setBtnFeedback(btn, 'saved', '✓ Views Shifted', 2400);
        showToast(`✓ Shifted ${(r.views || []).length} view(s) & saved 960×540 baseline`, 'ok');
        raState.wasLocked = false;
        raState.missStreak = 0;
        raState.lastGood = null;
        renderRealignCrops();
        refreshRealignOverlay(true);
        if (raState.pollTimer) { clearTimeout(raState.pollTimer); raState.pollTimer = null; }
        pollRealignLive();
      } catch (err) {
        setBtnFeedback(btn, 'save-err', '✗ Failed', 2500);
        showToast(`✗ Auto-shift failed: ${err.message || err}`, 'err');
      }
    };
  }
  if ($('raAcceptBtn')) {
    $('raAcceptBtn').onclick = async () => {
      if (!raState.slug) return;
      const cam = (cfg?.cameras || []).find(c => c.slug === raState.slug);
      const cname = cam ? cam.name : raState.slug;
      if (!window.confirm(`Replace the saved alignment baseline for ${cname} with the CURRENT camera position?\n\nOnly do this if you intentionally moved the mount (remember to verify your crops & mirror masks afterward).`)) return;
      const btn = $('raAcceptBtn');
      setBtnFeedback(btn, 'saving', 'Saving…');
      try {
        const r = await fetch('/api/bump/baseline', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ camera: raState.slug }),
        }).then(x => x.json());
        if (!r || !r.ok) throw new Error((r && r.error) || 'Failed');
        setBtnFeedback(btn, 'saved', '✓ Baseline Saved', 2200);
        showToast(`✓ Saved new 960×540 baseline for ${cname}`, 'ok');
        raState.wasLocked = false;
        refreshRealignOverlay(true);
        if (raState.pollTimer) { clearTimeout(raState.pollTimer); raState.pollTimer = null; }
        pollRealignLive();
      } catch (err) {
        setBtnFeedback(btn, 'save-err', '✗ Failed', 2500);
        showToast(`✗ Failed saving baseline: ${err.message || err}`, 'err');
      }
    };
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && raState.open) {
      ev.preventDefault();
      closeRealignModal();
    }
  });
}

async function init() {
  cfg = await fetch('/api/config').then(r => r.json());
  (cfg.cameras || []).forEach(c => {
    c.exclusions = (Array.isArray(c.exclusions) ? c.exclusions : []).map(normQuad).filter(Boolean);
  });
  $('retDays').value = cfg.retentionDays;
  $('preRoll').value = cfg.preRollSec;
  $('driveDir').value = cfg.driveBackupDir || '';
  if ($('bumpAlertNtfy')) $('bumpAlertNtfy').checked = (cfg.bumpAlertNtfy !== false);
  if ($('motionRecord247')) $('motionRecord247').checked = (cfg.motionRecord247 !== false);
  const wb = cfg.wallboard || {};
  $('wbHost').value = wb.fireTvHost || '';
  $('wbMac').value = wb.fireTvMac || '';
  $('wbPort').value = wb.adbPort || 5555;
  $('wbClient').value = wb.displayClient || 'silk';
  $('wbIdleMin').value = Math.max(1, Math.round((wb.idleSleepSec || 600) / 60));
  $('wbWake').value = wb.wakeOnStart === false ? '0' : '1';
  $('wbSleep').value = wb.sleepOnStop === false ? '0' : '1';
  const vx = cfg.voice || {};
  $('vxEnabled').value = vx.enabled === false ? '0' : '1';
  $('vxMode').value = vx.listenMode === 'always' ? 'always' : 'workout';
  $('vxReadback').value = vx.speakerReadback === false ? '0' : '1';
  if ($('vxRoomCmdReadback')) $('vxRoomCmdReadback').value = vx.roomCommandReadback === false ? '0' : '1';
  $('vxSetCommitMode').value = vx.setCommitMode === 'chime' ? 'chime' : 'voice';
  if ($('vxWakeWord')) $('vxWakeWord').value = vx.wakeWord || 'hey jarvis';
  $('vxTtsVoice').value = vx.ttsVoice || 'kokoro:am_michael';
  $('vxModel').value = vx.modelSize || 'base.en';
  $('vxVad').value = String(vx.vadThreshold != null ? vx.vadThreshold : 0.45);
  populateHouseAudioInputs(cfg.houseAudio || {});
  try {
    const savedTgt = localStorage.getItem('armory.haTestTarget');
    if (savedTgt && ['auto', 'cube', 's330'].includes(savedTgt) && $('haTestTarget')) {
      $('haTestTarget').value = savedTgt;
    }
  } catch {}
  if ($('haTestTarget')) {
    $('haTestTarget').addEventListener('change', () => {
      try { localStorage.setItem('armory.haTestTarget', $('haTestTarget').value || 'auto'); } catch {}
    });
  }
  populateDisplaysInputs(cfg.displays || {});
  renderCameras();
  wireMaskModal();
  wireRealignModal();
  wireNavigation();
  wireTapeScrubber();
  navigatePage(pageFromHash(), false);
  const initRealign = realignSlugFromHash();
  if (initRealign) openRealignModal(initRealign);
  setInterval(drawAllCanvases, 100);
  await Promise.all([pollStatus(), pollVoiceStatus(), pollAudioStatus(), pollVoiceAb(), pollClips(), loadShows(), loadTapes()]);
  if (activePage === 'footage') ensureScrubberTimeline();
  pollLoop(pollStatus, 2000);
  pollLoop(pollVoiceStatus, 3000, () => activePage === 'audio' || activePage === 'overview');
  pollLoop(pollAudioStatus, 5000, () => activePage === 'audio' || activePage === 'gym' || activePage === 'overview');
  pollLoop(pollVoiceAb, 5000, () => activePage === 'audio');
  pollLoop(pollClips, 8000, () => activePage === 'overview' || activePage === 'footage');
  document.addEventListener('visibilitychange', () => {
    syncRawFeeds();
    if (!document.hidden) {
      pollStatus();
      if (raState.open) pollRealignLive();
      if (activePage === 'audio' || activePage === 'overview') pollVoiceStatus();
      if (activePage === 'audio' || activePage === 'gym' || activePage === 'overview') pollAudioStatus();
      if (activePage === 'footage' || activePage === 'overview') pollClips();
    }
  });
  if ($('ovBackupCfgBtn')) {
    $('ovBackupCfgBtn').onclick = async () => {
      const btn = $('ovBackupCfgBtn');
      setBtnFeedback(btn, 'saving', 'Backing up…');
      try {
        const r = await fetch('/api/config/backup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
        if (r && r.ok) {
          const n = Array.isArray(r.files) ? r.files.length : 0;
          setBtnFeedback(btn, 'saved', `✓ ${n} backed up`, 2500);
          showToast(`✓ Backed up ${n} OptiPlex config(s) to Drive`, 'ok');
        } else {
          setBtnFeedback(btn, 'save-err', '✗ Backup failed', 3000);
          showToast(`✗ ${(r && r.error) || 'OptiPlex backup unavailable'}`, 'err');
        }
      } catch {
        setBtnFeedback(btn, 'save-err', '✗ Backup failed', 3000);
        showToast('✗ OptiPlex backup failed', 'err');
      }
    };
  }
}

['retDays', 'preRoll', 'driveDir', 'wbHost', 'wbMac', 'wbPort', 'wbIdleMin'].forEach(id => {
  $(id).addEventListener('input', () => markDirty());
});
['wbClient', 'wbWake', 'wbSleep', 'bumpAlertNtfy', 'motionRecord247'].forEach(id => {
  if ($(id)) $(id).addEventListener('change', () => markDirty());
});

$('openEditorTop').onclick = () => {
  if (!cfg || !cfg.cameras || !cfg.cameras.length) return;
  openMaskModal(clamp(mmState.camIdx || 0, 0, cfg.cameras.length - 1), { mode: 'crop' });
};
$('saveBtn').onclick = () => saveConfig($('saveBtn'));
$('saveAllTop').onclick = () => saveConfig($('saveAllTop'));
$('wbSaveBtn').onclick = () => saveConfig($('wbSaveBtn'));
$('wbPairBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Connecting ADB…';
  const r = await fetch('/api/wallboard/adb/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
  $('wbOut').textContent = r.message || r.state || 'Done';
};
$('wbWakeBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Waking TV & launching…';
  const r = await fetch('/api/wallboard/wake', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ force: true, sync: true }) }).then(x => x.json());
  $('wbOut').textContent = (r.adb && r.adb.message) || 'Sent wake';
};
$('wbSleepBtn').onclick = async () => {
  await saveConfig();
  $('wbOut').textContent = 'Sleeping TV…';
  const r = await fetch('/api/wallboard/sleep', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ reason: 'manual', delaySec: 0, sync: true }) }).then(x => x.json());
  $('wbOut').textContent = (r.adb && r.adb.message) || 'Sent sleep';
};
$('vxSaveBtn').onclick = () => saveVoiceConfig($('vxSaveBtn'));
$('vxTestBtn').onclick = async () => {
  const testing = $('vxTestBtn').dataset.testing === '1';
  const sec = testing ? 0 : 60;
  const r = await fetch('/api/voice/test-listen', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ seconds: sec }) }).then(x => x.json());
  syncVoiceUI(r.status);
  $('vxOut').textContent = sec > 0 ? 'Test mic opened for 60s — say "Armory 225 for 8"' : 'Test mic closed';
};
$('vxSpeakBtn').onclick = async () => {
  $('vxOut').textContent = 'Testing PowerConf S330 speaker…';
  const r = await fetch('/api/voice/test-speak', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text: 'Armory room mic ready' }) }).then(x => x.json());
  $('vxOut').textContent = r.spoke ? '✓ Played on PowerConf S330' : 'Speaker device unavailable';
};
$('vxInjectBtn').onclick = async () => {
  const r = await fetch('/api/voice/inject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ transcript: 'Armory check' }) }).then(x => x.json());
  $('vxOut').textContent = r.ok ? 'Sent "Armory check" to Pixel SSE stream' : 'Failed';
  await pollVoiceStatus();
};
$('vxAbResetBtn').onclick = async () => {
  await fetch('/api/voice/ab/reset', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ clearRuns: false }) });
  $('vxOut').textContent = 'Cleared passive workout A/B trials';
  await pollVoiceAb();
};

const wsState = { data: null, selectedPhrase: 'hey atlas', timer: null };

function _wsSelectedWord() {
  const words = (wsState.data && Array.isArray(wsState.data.words)) ? wsState.data.words : [];
  return words.find(w => w.phrase === wsState.selectedPhrase) || words[0] || null;
}

function _wsSchedulePoll(fastMs) {
  if (wsState.timer) { clearTimeout(wsState.timer); wsState.timer = null; }
  if (!fastMs || activePage !== 'audio') return;
  wsState.timer = setTimeout(() => { pollWakeStudio(); }, fastMs);
}

function renderWakeStudio(st) {
  if (!st || !Array.isArray(st.words)) return;
  wsState.data = st;
  const enr = st.enrollment || {};
  const trn = st.training || {};
  if (enr.active && enr.phrase) wsState.selectedPhrase = enr.phrase;
  else if (!st.words.some(w => w.phrase === wsState.selectedPhrase)) wsState.selectedPhrase = st.activePhrase || 'hey atlas';
  const word = _wsSelectedWord();

  const pillsEl = $('wsWordPills');
  if (pillsEl) {
    pillsEl.innerHTML = st.words.map(w => {
      const sel = word && w.phrase === word.phrase;
      const badge = w.modelExists ? (w.kind === 'stock' && !w.hasStockBackup ? 'Stock ✓' : 'ONNX ✓') : 'Untrained';
      const liveTag = w.active ? '<span style="color:#4ade80;font-size:11px">★ LIVE</span>' : `<span style="color:var(--mut);font-size:11px">${badge}</span>`;
      return `<button type="button" class="ws-pill${sel ? ' on' : ''}" data-wsphrase="${escHtml(w.phrase)}">` +
        `<div class="ws-pill-top"><span>${escHtml(w.label)}</span>${liveTag}</div>` +
        `<div class="ws-pill-sub"><span>${Number(w.clipCount || 0)} S330 reps</span><span>${w.modelExists ? w.modelSizeKb + ' KB' : 'Needs train'}</span></div>` +
        `</button>`;
    }).join('');
    pillsEl.querySelectorAll('[data-wsphrase]').forEach(btn => {
      btn.onclick = () => { wsState.selectedPhrase = btn.dataset.wsphrase; renderWakeStudio(wsState.data); };
    });
  }

  const statusPill = $('wsStatusPill');
  if (statusPill) {
    if (trn.running) { statusPill.textContent = `TRAINING ${Number(trn.progressPct || 0)}%`; statusPill.className = 'pill live'; }
    else if (enr.active) { statusPill.textContent = `LISTENING ${Number(enr.sessionCaptured || 0)}/${Number(enr.targetReps || 20)}`; statusPill.className = 'pill ok'; }
    else if (word && word.active) { statusPill.textContent = 'ACTIVE'; statusPill.className = 'pill ok'; }
    else { statusPill.textContent = 'READY'; statusPill.className = 'pill'; }
  }

  const metaEl = $('wsMetricsTxt');
  if (metaEl && word) {
    const m = word.metrics;
    const parts = [`Model: <b>${word.modelExists ? `${word.slug}_v0.1.onnx (${word.modelSizeKb} KB)` : 'Not trained yet'}</b>`];
    if (m) {
      if (m.unseenRecallAt038 != null) parts.push(`Recall @0.38: <b>${(Number(m.unseenRecallAt038) * 100).toFixed(1)}%</b>`);
      if (m.val10hFphAt038 != null) parts.push(`False Alarms: <b>${Number(m.val10hFphAt038).toFixed(2)}/hr</b>`);
      if (m.userFilesCount != null) parts.push(`Baked Reps: <b>${m.userFilesCount}</b> (${m.userClipsUsed || 0} aug)`);
      if (m.trainedAt) parts.push(`Trained: <b>${escHtml(String(m.trainedAt).replace('T', ' ').slice(0, 16))}</b>`);
    } else if (word.kind === 'stock' && !word.hasStockBackup) {
      parts.push('Stock openWakeWord pretrained ONNX');
    }
    metaEl.innerHTML = parts.join(' &nbsp;·&nbsp; ');
  }

  const actBtn = $('wsActivateBtn');
  if (actBtn && word) {
    actBtn.disabled = !word.modelExists || !!word.active;
    actBtn.textContent = word.active ? '✓ Active Wake Word' : (word.modelExists ? `Use "${word.label}"` : 'Train Model First');
  }
  const restBtn = $('wsRestoreStockBtn');
  if (restBtn && word) {
    restBtn.hidden = !(word.slug === 'hey_jarvis' && word.hasStockBackup);
  }

  const pBox = $('wsPromptBox');
  if (pBox) pBox.classList.toggle('rec', !!enr.active);
  if ($('wsPromptTitle') && word) {
    $('wsPromptTitle').textContent = enr.active ? `Recording "${word.label}" on PowerConf S330…` : `Guided 20-Rep Capture — ${word.label}`;
  }
  if ($('wsRepCounter') && word) {
    $('wsRepCounter').textContent = enr.active
      ? `${Number(enr.sessionCaptured || 0)} / ${Number(enr.targetReps || 20)} session (${Number(word.clipCount || 0)} total)`
      : `${Number(word.clipCount || 0)} saved reps`;
  }
  if ($('wsPromptCue')) {
    $('wsPromptCue').textContent = enr.active
      ? (enr.prompt || 'Speak clearly toward PowerConf S330…')
      : `Click Start Guided Capture, then say "${word ? word.label : 'Hey Atlas'}" 20 times at your natural gym pace (chime between reps).`;
  }
  if ($('wsLastEvent')) {
    $('wsLastEvent').textContent = enr.lastEvent || 'Say only the wake phrase after each chime — training mixes in gym rock & command tails automatically.';
  }
  if ($('wsMeter')) {
    const pct = clamp((Number(enr.rms || 0) / 0.08) * 100, 0, 100);
    $('wsMeter').style.width = pct.toFixed(1) + '%';
    $('wsMeter').classList.toggle('hot', Number(enr.vad || 0) >= 0.38);
  }

  const enrBtn = $('wsEnrollBtn');
  if (enrBtn) {
    enrBtn.textContent = enr.active ? '⏹ Stop Capture' : '🎙️ Start Guided Capture (20 Reps)';
    enrBtn.className = enr.active ? 'btn red' : 'btn';
    enrBtn.disabled = !!trn.running;
  }
  const trainBtn = $('wsTrainBtn');
  if (trainBtn && word) {
    trainBtn.disabled = !!trn.running;
    trainBtn.textContent = trn.running ? `Training (${Number(trn.progressPct || 0)}%)…` : (word.synthCached ? '⚡ Train & Deploy (~45s)' : '⚡ Full Synth & Train (~3m)');
  }
  if ($('wsDelLastBtn')) $('wsDelLastBtn').disabled = !word || !word.clips || !word.clips.length || !!trn.running;
  if ($('wsClearBtn')) $('wsClearBtn').disabled = !word || !word.clips || !word.clips.length || !!trn.running;

  const tBox = $('wsTrainBox');
  if (tBox) {
    const showTrain = !!trn.running || (Array.isArray(trn.logs) && trn.logs.length > 0);
    tBox.hidden = !showTrain;
    if (showTrain) {
      if ($('wsTrainStage')) $('wsTrainStage').textContent = trn.stage || 'Idle';
      if ($('wsTrainPct')) $('wsTrainPct').textContent = `${Number(trn.progressPct || 0)}%`;
      if ($('wsTrainBar')) $('wsTrainBar').style.width = `${clamp(Number(trn.progressPct || 0), 0, 100)}%`;
      if ($('wsTrainLog')) {
        $('wsTrainLog').textContent = (trn.logs || []).slice(-18).join('\\n');
        $('wsTrainLog').scrollTop = $('wsTrainLog').scrollHeight;
      }
    }
  }

  if ($('wsClipsHdr') && word) $('wsClipsHdr').textContent = `Recorded Voice Clips — ${word.label} (${Number(word.clipCount || 0)})`;
  const listEl = $('wsClipsList');
  if (listEl && word) {
    const clips = Array.isArray(word.clips) ? word.clips : [];
    if (!clips.length) {
      listEl.innerHTML = '<div style="padding:10px;font-size:12px;color:var(--mut)">No personal S330 clips recorded yet for this phrase.</div>';
    } else {
      listEl.innerHTML = clips.map(c => {
        const ts = c.tsMs ? new Date(c.tsMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
        return `<div class="ws-clip-row">` +
          `<span><code>${escHtml(c.name)}</code> · <b>${Number(c.durationSec || 0).toFixed(2)}s</b> · <span style="color:var(--mut)">${escHtml(ts)}</span></span>` +
          `<span style="display:flex;gap:5px"><button class="btn sec sm" type="button" data-wsplay="${escHtml(c.url)}">▶</button><button class="btn red sm" type="button" data-wsdel="${escHtml(word.slug)}|${escHtml(c.name)}">✕</button></span>` +
          `</div>`;
      }).join('');
      listEl.querySelectorAll('[data-wsplay]').forEach(b => {
        b.onclick = () => {
          const au = $('wsClipAudio');
          if (!au) return;
          au.src = b.dataset.wsplay + '?t=' + Date.now();
          au.play().catch(() => {});
        };
      });
      listEl.querySelectorAll('[data-wsdel]').forEach(b => {
        b.onclick = async () => {
          const [sl, fn] = b.dataset.wsdel.split('|');
          const r = await fetch(`/api/voice/wake-studio/clip/${encodeURIComponent(sl)}/${encodeURIComponent(fn)}`, { method: 'DELETE' }).then(x => x.json());
          if (r && r.studio) renderWakeStudio(r.studio);
        };
      });
    }
  }

  if (enr.active) _wsSchedulePoll(350);
  else if (trn.running) _wsSchedulePoll(1000);
  else _wsSchedulePoll(0);
}

async function pollWakeStudio() {
  try {
    const r = await fetch('/api/voice/wake-studio', { cache: 'no-store' }).then(x => x.json());
    if (r && r.ok) renderWakeStudio(r);
  } catch {}
}

if ($('wsEnrollBtn')) {
  $('wsEnrollBtn').onclick = async () => {
    const enr = wsState.data && wsState.data.enrollment;
    const stop = !!(enr && enr.active);
    const body = stop ? { action: 'stop' } : { action: 'start', phrase: wsState.selectedPhrase, targetReps: 20 };
    const r = await fetch('/api/voice/wake-studio/enroll', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(x => x.json());
    if (r && r.studio) renderWakeStudio(r.studio);
  };
}
if ($('wsDelLastBtn')) {
  $('wsDelLastBtn').onclick = async () => {
    const w = _wsSelectedWord();
    if (!w || !w.clips || !w.clips.length) return;
    const c = w.clips[0];
    const r = await fetch(`/api/voice/wake-studio/clip/${encodeURIComponent(w.slug)}/${encodeURIComponent(c.name)}`, { method: 'DELETE' }).then(x => x.json());
    if (r && r.studio) renderWakeStudio(r.studio);
  };
}
if ($('wsClearBtn')) {
  $('wsClearBtn').onclick = async () => {
    const w = _wsSelectedWord();
    if (!w) return;
    const r = await fetch('/api/voice/wake-studio/clips/clear', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ slug: w.slug }) }).then(x => x.json());
    if (r && r.studio) renderWakeStudio(r.studio);
  };
}
if ($('wsTrainBtn')) {
  $('wsTrainBtn').onclick = async () => {
    const forceFull = !!($('wsForceFull') && $('wsForceFull').checked);
    const r = await fetch('/api/voice/wake-studio/train', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ phrase: wsState.selectedPhrase, forceFull }) }).then(x => x.json());
    if (r && r.studio) renderWakeStudio(r.studio);
    if (!r.ok) showToast(r.error || 'Could not start training', 'err');
  };
}
if ($('wsActivateBtn')) {
  $('wsActivateBtn').onclick = async () => {
    const r = await fetch('/api/voice/wake-studio/activate', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ phrase: wsState.selectedPhrase }) }).then(x => x.json());
    if (r && r.ok) {
      if ($('vxWakeWord') && r.config && r.config.wakeWord) $('vxWakeWord').value = r.config.wakeWord;
      if (cfg && r.config) cfg.voice = r.config;
      if (r.studio) renderWakeStudio(r.studio);
      showToast(`✓ Active wake word set to "${wsState.selectedPhrase}"`, 'ok');
    }
  };
}
if ($('wsRestoreStockBtn')) {
  $('wsRestoreStockBtn').onclick = async () => {
    const r = await fetch('/api/voice/wake-studio/restore-stock', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ phrase: 'hey jarvis' }) }).then(x => x.json());
    if (r && r.studio) renderWakeStudio(r.studio);
    if (r && r.ok) showToast('✓ Restored stock Hey Jarvis ONNX', 'ok');
  };
}
$('haSaveBtn').onclick = () => saveHouseAudioConfig($('haSaveBtn'));

function _applyDuckPreset(stage, key) {
  const preset = stage === 'listen' ? HA_LISTEN_PRESETS[key] : HA_OUT_PRESETS[key];
  if (!preset) return;
  if (stage === 'listen') {
    if ($('haListenDuckMode')) $('haListenDuckMode').value = preset.mode;
    if (preset.mode !== 'off') {
      if ($('haDuckThreshVol')) $('haDuckThreshVol').value = String(preset.thrVol);
      if ($('haDuckMaxDrop')) $('haDuckMaxDrop').value = String(preset.maxDrop);
      if ($('haDuckExp')) $('haDuckExp').value = String(preset.exp);
      if ($('haDuckFloorVol')) $('haDuckFloorVol').value = String(preset.floorVol);
    }
  } else {
    if ($('haOutDuckMode')) $('haOutDuckMode').value = preset.mode;
    if (preset.mode !== 'off' && preset.mode !== 'match') {
      if ($('haOutDuckThreshVol')) $('haOutDuckThreshVol').value = String(preset.thrVol);
      if ($('haOutDuckMaxDrop')) $('haOutDuckMaxDrop').value = String(preset.maxDrop);
      if ($('haOutDuckExp')) $('haOutDuckExp').value = String(preset.exp);
      if ($('haOutDuckFloorVol')) $('haOutDuckFloorVol').value = String(preset.floorVol);
    }
  }
  updateDuckCurvePreviews();
  if ($('haOut')) $('haOut').textContent = 'Preset staged — click Save Audio (or Test Dip) to apply.';
}

document.querySelectorAll('#haListenPresets [data-lpreset]').forEach(btn => {
  btn.addEventListener('click', () => _applyDuckPreset('listen', btn.dataset.lpreset));
});
document.querySelectorAll('#haOutPresets [data-opreset]').forEach(btn => {
  btn.addEventListener('click', () => _applyDuckPreset('output', btn.dataset.opreset));
});
[
  'haListenDuckMode', 'haDuckThreshVol', 'haDuckMaxDrop', 'haDuckExp', 'haDuckFloorVol',
  'haOutDuckMode', 'haOutDuckThreshVol', 'haOutDuckMaxDrop', 'haOutDuckExp', 'haOutDuckFloorVol',
].forEach(id => {
  const el = $(id);
  if (!el) return;
  const onEdit = () => {
    updateDuckCurvePreviews();
    if ($('haOut')) $('haOut').textContent = 'Curve edited — click Save Audio (or Test Dip) to apply.';
  };
  el.addEventListener('input', onEdit);
  el.addEventListener('change', onEdit);
});

async function runDenonStageDip(stage, force, triggerBtn) {
  const lbl = stage === 'listen' ? 'Listening' : 'Speaking';
  await saveHouseAudioConfig();
  if (triggerBtn) setBtnFeedback(triggerBtn, 'saving', 'Dipping…');
  $('haOut').textContent = `Testing ${lbl.toLowerCase()} Denon dip…`;
  try {
    const r = await fetch('/api/audio/denon/test-duck', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ holdSec: 1.4, stage, force }),
    }).then(x => x.json());
    if (r.ok && r.duck) {
      const fromVol = Math.round((Number(r.duck.fromDb) + 80) * 10) / 10;
      const toVol = Math.round((Number(r.duck.toDb) + 80) * 10) / 10;
      const drop = r.duck.dropDb != null ? Number(r.duck.dropDb) : Math.round((fromVol - toVol) * 10) / 10;
      $('haOut').textContent = `✓ ${lbl} dip: -${drop} dB (Vol ${fromVol} → ${toVol}) & restored`;
      if (triggerBtn) setBtnFeedback(triggerBtn, 'saved', `✓ -${drop} dB`, 2000);
    } else {
      const rsn = (r.duck && r.duck.reason) || 'offline';
      const friendly = {
        standby: 'Denon is in standby (turn Denon ON to preview dip)',
        disabled: `${lbl} duck is Off (0 dB dip)`,
        output_off: 'Speaking duck is Off (0 dB dip)',
        below_threshold: `Current Vol is below ${lbl.toLowerCase()} threshold (0 dB dip)`,
        at_floor: 'Already at/below floor volume (0 dB dip)',
        muted: 'Denon is muted',
      }[rsn] || `Skipped (${rsn})`;
      $('haOut').textContent = `${lbl} dip: ${friendly}`;
      if (triggerBtn) setBtnFeedback(triggerBtn, 'save-err', '0 dB', 2000);
    }
    await pollAudioStatus();
  } catch {
    $('haOut').textContent = `${lbl} Denon dip request failed`;
    if (triggerBtn) setBtnFeedback(triggerBtn, 'save-err', '✗ Failed', 2000);
  }
}

if ($('haTestListenDipBtn')) $('haTestListenDipBtn').onclick = () => runDenonStageDip('listen', false, $('haTestListenDipBtn'));
if ($('haTestOutDipBtn')) $('haTestOutDipBtn').onclick = () => runDenonStageDip('output', false, $('haTestOutDipBtn'));
$('haDenonDipBtn').onclick = () => runDenonStageDip('listen', true, $('haDenonDipBtn'));
$('haCubeProbeBtn').onclick = async () => {
  await saveHouseAudioConfig();
  $('haOut').textContent = 'Probing Cube ADB & surround settings…';
  try {
    const r = await fetch('/api/audio/cube/probe', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    $('haOut').textContent = r.message || r.state || 'Done';
    const box = $('haAdbDetails');
    if (box) {
      if (r.ok) {
        box.hidden = false;
        box.innerHTML = `Model: <b>${escHtml(r.model || '?')}</b> · Android ${escHtml(r.androidVersion || '?')} · Surround: <code>${escHtml(r.surroundOutput || r.surroundMode || 'unknown')}</code> · Mixer APK: <b>${r.apkInstalled ? 'Installed' : 'Not installed'}</b> · 3rd-party pkgs (${(r.thirdPartyPackages || []).length}): <code>${escHtml((r.thirdPartyPackages || []).join(', ') || 'none')}</code>`;
      } else {
        box.hidden = true;
      }
    }
    await pollAudioStatus();
  } catch {
    $('haOut').textContent = 'Cube probe failed';
  }
};
$('haCubeReviveBtn').onclick = async () => {
  await saveHouseAudioConfig();
  $('haOut').textContent = 'Starting Cube service over ADB…';
  try {
    const r = await fetch('/api/audio/cube/revive', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    $('haOut').textContent = r.message || r.state || 'Done';
    await pollAudioStatus();
  } catch {
    $('haOut').textContent = 'Cube revive failed';
  }
};
document.querySelectorAll('[data-hatest]').forEach(btn => {
  btn.addEventListener('click', () => {
    const [kind, mode] = btn.dataset.hatest.split(':');
    runHouseAudioTest(kind, mode);
  });
});
$('haSfxPlayBtn').onclick = () => {
  const f = $('haSfxSel').value;
  runHouseAudioTest('sfx', $('haHypeMode').value || 'takeover', f);
};
$('haStopBtn').onclick = async () => {
  await fetch('/api/audio/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  $('haTestOut').textContent = 'Stopped playback';
};
$('haMxSaveBtn').onclick = async () => {
  const r = await fetch('/api/audio/matrix', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      app: $('haMxApp').value,
      mode: $('haMxMode').value,
      status: $('haMxStatus').value,
      behavior: $('haMxBehavior').value.trim(),
    }),
  }).then(x => x.json());
  if (r && r.matrix) renderAudioMatrix(r.matrix);
};
$('haMatrixResetBtn').onclick = async () => {
  const r = await fetch('/api/audio/matrix', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ reset: true }) }).then(x => x.json());
  if (r && r.matrix) renderAudioMatrix(r.matrix);
};
$('haMatrixTable').addEventListener('click', (ev) => {
  const td = ev.target.closest('td[data-mxapp]');
  if (!td) return;
  $('haMxApp').value = td.dataset.mxapp;
  $('haMxMode').value = td.dataset.mxmode;
});
document.querySelectorAll('[data-dpact]').forEach(btn => {
  btn.addEventListener('click', () => {
    const [target, action] = btn.dataset.dpact.split(':');
    runDisplayAction(target, action);
  });
});
$('dpSaveBtn').onclick = () => saveDisplaysConfig($('dpSaveBtn'));
$('dpProbePrimBtn').onclick = async () => {
  await saveDisplaysConfig();
  $('dpOut').textContent = 'Probing Primary Display ADB…';
  try {
    const r = await fetch('/api/displays/status?probe=primary').then(x => x.json());
    const p = (r.primary && r.primary.probe) || {};
    $('dpOut').textContent = p.ok ? `✓ ${p.message}` : (p.message || 'Offline');
    syncDisplaysUI(r);
  } catch {
    $('dpOut').textContent = 'Probe failed';
  }
};
$('dpProbeCubeBtn').onclick = async () => {
  await saveDisplaysConfig();
  $('dpOut').textContent = 'Probing Fire TV Cube ADB…';
  try {
    const r = await fetch('/api/displays/status?probe=cube').then(x => x.json());
    const c = (r.cube && r.cube.probe) || {};
    $('dpOut').textContent = c.ok ? `✓ ${c.message}` : (c.message || 'Offline');
    syncDisplaysUI(r);
  } catch {
    $('dpOut').textContent = 'Cube probe failed';
  }
};
$('dpFindRokuBtn').onclick = async () => {
  $('dpOut').textContent = 'Scanning LAN for Roku TVs…';
  try {
    const r = await fetch('/api/displays/discover-roku', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    const box = $('dpRokuList');
    const items = Array.isArray(r.devices) ? r.devices : [];
    $('dpOut').textContent = items.length ? `Found ${items.length} Roku device(s)` : 'No Roku TVs responded on :8060';
    if (box) {
      if (!items.length) {
        box.hidden = true;
      } else {
        box.hidden = false;
        box.innerHTML = items.map(d => `<div style="margin-top:4px"><code>${escHtml(d.host)}</code> · <b>${escHtml(d.name || d.model || 'Roku')}</b> (${escHtml(d.location || 'No location')}, ${escHtml(d.powerMode || '')}, MAC ${escHtml(d.mac || '')}) <button class="btn ok sm" type="button" onclick="pickDiscoveredRoku('${escHtml(d.host)}','${escHtml(d.mac || '')}')">Use as SmartSpin</button></div>`).join('');
      }
    }
  } catch {
    $('dpOut').textContent = 'Roku discovery failed';
  }
};

let devRyderSpec = null;
let devRyVoicesCache = [];
const openShowIds = new Set();
const openShowSubIds = new Set();
const closedShowSubIds = new Set();
const showAuditionBlobUrls = {};
let rySimState = { running: false, timers: [], iv: null, ytPlayer: null, introEl: null, ovEl: null, fogLeft: 0, cueFlags: null, flashTimer: null };
let ryScrubRaf = null;

function parseTimeSec(val) {
  const s = String(val == null ? '' : val).trim();
  if (!s) return 0;
  const parts = s.split(':');
  let sec = 0;
  if (parts.length === 2) {
    sec = (parseFloat(parts[0]) || 0) * 60 + (parseFloat(parts[1]) || 0);
  } else if (parts.length === 3) {
    sec = (parseFloat(parts[0]) || 0) * 3600 + (parseFloat(parts[1]) || 0) * 60 + (parseFloat(parts[2]) || 0);
  } else {
    sec = parseFloat(s) || 0;
  }
  return Math.max(0, Math.min(300, Math.round(sec * 100) / 100));
}

function fmtTimeSec(sec) {
  const s = Math.max(0, Number(sec || 0));
  const mm = Math.floor(s / 60);
  const rem = s - mm * 60;
  const remStr = rem.toFixed(1).padStart(4, '0');
  return `${mm}:${remStr}`;
}

function getRyIntroSec() {
  return Number((devRyderSpec && devRyderSpec.introDurationSec) || 9.9);
}

function logDevCue(msg) {
  const el = $('devLog');
  if (!el) return;
  const row = document.createElement('div');
  row.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(row);
  el.scrollTop = el.scrollHeight;
}

function fxSetLights(mode) {
  const st = $('devStage'), hud = $('devLightsHud');
  if (!st || !hud) return;
  st.classList.remove('lights-off', 'lights-on');
  if (mode === 'off') {
    st.classList.add('lights-off');
    hud.textContent = '(LIGHTS OFF)';
    hud.className = 'fx-tag dark';
  } else if (mode === 'on') {
    st.classList.add('lights-on');
    hud.textContent = '(FULL HOUSE LIGHTS ON)';
    hud.className = 'fx-tag lit';
  } else {
    hud.textContent = 'LIGHTS: STANDBY';
    hud.className = 'fx-tag';
  }
}

function fxFlash(label, durMs) {
  const st = $('devStage'), fh = $('devFlashHud');
  if (!st) return;
  st.classList.add('flashing');
  if (fh) { fh.textContent = label || '(FLASH)'; fh.className = 'fx-tag flash'; }
  if (rySimState.flashTimer) clearTimeout(rySimState.flashTimer);
  rySimState.flashTimer = setTimeout(() => {
    st.classList.remove('flashing');
    if (fh) { fh.textContent = 'FLASH: READY'; fh.className = 'fx-tag'; }
  }, Math.max(120, Number(durMs || 220)));
}

function fxTriggerFog(sec) {
  const fh = $('devFogHud');
  rySimState.fogLeft = Math.max(1, Math.round(Number(sec || 30)));
  if (fh) { fh.textContent = `(TRIGGER FOG · ${rySimState.fogLeft}s)`; fh.className = 'fx-tag fog'; }
}

function fxApplyCue(ev) {
  if (!ev || !ev.tag) return;
  const tag = ev.tag;
  if (tag === 'lights_off') fxSetLights('off');
  else if (tag === 'lights_on') fxSetLights('on');
  else if (tag === 'fog') fxTriggerFog(ev.fogSec || 30);
  else if (tag === 'flash' || tag === 'pulse') fxFlash(ev.label || '(FLASH)', ev.durationMs || 220);
  if (ev.denonVol && $('devDenonHud')) $('devDenonHud').textContent = `DENON: VOL ${ev.denonVol}`;
  if (ev.caption && $('devCaption')) $('devCaption').textContent = ev.caption;
  else if (ev.label && $('devCaption')) $('devCaption').textContent = ev.label;
  logDevCue(ev.label || tag);
}

function ensureShowSse() {
  if (window._showSse) return;
  try {
    const es = new EventSource('/api/shows/events');
    es.onmessage = (msg) => {
      try {
        const d = JSON.parse(msg.data);
        if (!d) return;
        if (d.type === 'cue') fxApplyCue(d);
        else if (d.type === 'stage_stop') {
          stopLocalRyderSim();
          if ($('shMsg')) $('shMsg').textContent = '⏹ Show stopped safely.';
        }
      } catch {}
    };
    window._showSse = es;
  } catch {}
}

function buildRyFlashWindows() {
  const introSec = getRyIntroSec();
  const wins = [];
  const rows = document.querySelectorAll('#devRyFxCueList .ry-fx-row');
  rows.forEach(row => {
    const type = row.querySelector('.ry-fx-type').value;
    const screen = row.querySelector('.ry-fx-screen').checked;
    if (type !== 'flash' || !screen) return;
    const tgt = row.querySelector('.ry-fx-target').value;
    const at = parseTimeSec(row.querySelector('.ry-fx-at').value);
    const dur = Math.max(0.08, Number(row.querySelector('.ry-fx-dur').value || 0.2));
    const cnt = Math.max(1, Math.min(6, Number(row.querySelector('.ry-fx-cnt').value || 1)));
    const showAt = tgt === 'intro' ? at : (introSec + at);
    for (let k = 0; k < cnt; k++) {
      const t0 = showAt + k * (dur + 0.15);
      wins.push({ t0, t1: t0 + dur });
    }
  });
  return wins;
}

function updateRyScrubAndFlash() {
  const vid = $('devRyVideo');
  if (!vid) return;
  const t = vid.currentTime || 0;
  const introSec = getRyIntroSec();
  if ($('devScrubClock')) {
    const rel = t < introSec ? `Intro ${fmtTimeSec(t)}` : `Video +${fmtTimeSec(t - introSec)}`;
    $('devScrubClock').textContent = `MP4 ${fmtTimeSec(t)} · ${rel}`;
  }
  const vf = $('devVidFlash');
  if (vf) {
    if (!vid.paused && !vid.ended) {
      const wins = buildRyFlashWindows();
      const lit = wins.some(w => t >= w.t0 && t < w.t1);
      vf.style.opacity = lit ? '0.95' : '0';
    } else {
      vf.style.opacity = '0';
    }
  }
  if (!rySimState.running || !rySimState.cueFlags) return;
  const cf = rySimState.cueFlags;
  const spec = cf.spec;
  const mm = Math.floor(t / 60), ss = String(Math.floor(t % 60)).padStart(2, '0');
  if ($('devSimClock')) $('devSimClock').textContent = `MP4 ${mm}:${ss}`;
  if (!cf.lightning && t >= (spec.lightningFlashSec || 0.75)) {
    cf.lightning = true;
    fxApplyCue({ tag: 'flash', label: '(FLASH · LIGHTNING CRACK)', durationMs: 180 });
  }
  if (!cf.flash1 && t >= Math.max(0.5, introSec - 0.92)) {
    cf.flash1 = true;
    fxApplyCue({ tag: 'flash', label: '(FLASH 1/2)', durationMs: 200 });
  }
  if (!cf.flash2 && t >= Math.max(0.8, introSec - 0.48)) {
    cf.flash2 = true;
    fxApplyCue({ tag: 'flash', label: '(FLASH 2/2)', durationMs: 240 });
  }
  if (!cf.screens && t >= introSec) {
    cf.screens = true;
    fxApplyCue({ tag: 'screens', label: `(SCREENS LIVE · DENON ${spec.denonVol || 70})`, denonVol: spec.denonVol || 70 });
  }
  const pulseEnd = introSec + Number(spec.lightsPulseDurationSec || 20);
  if (t >= introSec && t < pulseEnd) {
    const iv = Number(spec.lightsPulseIntervalSec || 2);
    const pIdx = Math.floor((t - introSec) / iv) + 1;
    if (pIdx > cf.lastPulse) {
      cf.lastPulse = pIdx;
      fxFlash(`(FLASH 0.25s · Pulse ${pIdx})`, 250);
    }
  }
  if (!cf.lightsOn && t >= pulseEnd) {
    cf.lightsOn = true;
    fxApplyCue({ tag: 'lights_on', label: '(FULL HOUSE LIGHTS ON)' });
  }
  (cf.speechCues || []).forEach((sc, idx) => {
    const key = 'sp_' + idx;
    if (!cf.fired[key] && t >= sc.showAt) {
      cf.fired[key] = true;
      fxApplyCue({ tag: 'overlay', label: `(DUCK + SPEECH · "${sc.text}")`, caption: sc.text });
    }
    const dKey = 'dn_' + idx;
    if (sc.postDenonVol > 0 && !cf.fired[dKey] && t >= sc.showAt + sc.durSec) {
      cf.fired[dKey] = true;
      fxApplyCue({ tag: 'denon_drop', label: `(RESTORE · DENON → ${sc.postDenonVol})`, denonVol: sc.postDenonVol });
    }
  });
  (cf.fxCues || []).forEach((fc, idx) => {
    const key = 'fx_' + idx;
    if (!cf.fired[key] && t >= fc.showAt) {
      cf.fired[key] = true;
      if (fc.type === 'flash') fxFlash(`(FX FLASH ×${fc.count})`, fc.durationSec * 1000);
      else if (fc.type === 'lights_off') fxApplyCue({ tag: 'lights_off', label: '(LIGHTS OFF · FX)' });
      else if (fc.type === 'lights_on') fxApplyCue({ tag: 'lights_on', label: '(LIGHTS ON · FX)' });
      else if (fc.type === 'fog') fxApplyCue({ tag: 'fog', label: `(TRIGGER FOG · ${fc.durationSec}s)`, fogSec: fc.durationSec });
    }
  });
}

function initRyderScrubber() {
  const vid = $('devRyVideo');
  if (!vid || vid._ryScrubInit) return;
  vid._ryScrubInit = true;
  const tickLoop = () => {
    updateRyScrubAndFlash();
    if (!vid.paused && !vid.ended) {
      ryScrubRaf = requestAnimationFrame(tickLoop);
    } else {
      ryScrubRaf = null;
    }
  };
  vid.addEventListener('play', () => {
    if (!ryScrubRaf) ryScrubRaf = requestAnimationFrame(tickLoop);
  });
  ['pause', 'seeking', 'seeked', 'timeupdate', 'loadedmetadata'].forEach(evName => {
    vid.addEventListener(evName, updateRyScrubAndFlash);
  });
  vid.addEventListener('ended', () => {
    if (rySimState.running) {
      logDevCue('(SHOW COMPLETE)');
      stopLocalRyderSim();
    }
  });
}

function stopLocalRyderSim() {
  rySimState.running = false;
  rySimState.cueFlags = null;
  (rySimState.timers || []).forEach(id => clearTimeout(id));
  rySimState.timers = [];
  if (rySimState.iv) { clearInterval(rySimState.iv); rySimState.iv = null; }
  if (rySimState.flashTimer) { clearTimeout(rySimState.flashTimer); rySimState.flashTimer = null; }
  if (ryScrubRaf) { cancelAnimationFrame(ryScrubRaf); ryScrubRaf = null; }
  [rySimState.introEl, rySimState.ovEl, $('shAudio_ryder_walkin'), $('shOvAudio_ryder_walkin')].forEach(a => {
    if (a) { try { a.pause(); a.currentTime = 0; } catch {} }
  });
  const vid = $('devRyVideo');
  if (vid) {
    vid.onloadedmetadata = null;
    try { vid.pause(); } catch {}
  }
  const vf = $('devVidFlash');
  if (vf) vf.style.opacity = '0';
  if ($('devStage')) $('devStage').classList.remove('flashing');
  if ($('devFlashHud')) { $('devFlashHud').textContent = 'FLASH: READY'; $('devFlashHud').className = 'fx-tag'; }
  rySimState.fogLeft = 0;
  if ($('devFogHud')) { $('devFogHud').textContent = 'FOG: OFF'; $('devFogHud').className = 'fx-tag'; }
  if ($('devDenonHud')) $('devDenonHud').textContent = 'DENON: STANDBY';
  fxSetLights('standby');
}

async function startLocalRyderSim(jumpToSec) {
  stopLocalRyderSim();
  initRyderScrubber();
  rySimState.running = true;
  const spec = devRyderSpec || { denonVol: 70, fogBurstSec: 30, lightningFlashSec: 0.75, lightsPulseDurationSec: 20, lightsPulseIntervalSec: 2.0, lightsPulseOnSec: 0.25, introDurationSec: 9.9, announcerText: '' };
  const vid = $('devRyVideo');
  const introSec = Number(spec.introDurationSec || 9.9);

  if (!vid || !spec.showVideoReady) {
    const introEl = $('shAudio_ryder_walkin');
    rySimState.introEl = introEl;
    fxApplyCue({ tag: 'lights_off', label: '(LIGHTS OFF) · (TRIGGER FOG · 30s)', fogSec: spec.fogBurstSec || 30, denonVol: spec.denonVol || 70, caption: spec.announcerText || 'Thunder rumbling…' });
    if (introEl) {
      introEl.src = `/api/shows/ryder_walkin/preview.wav?t=${Date.now()}`;
      introEl.load();
      introEl.play().catch(() => {});
    }
    logDevCue('Playing intro WAV locally (upload/fetch entrance MP4 to preview full show video).');
    return;
  }

  const spRows = collectRySpeechCues().filter(c => c.target === 'video' && c.text).map(c => ({
    showAt: introSec + c.atSec,
    durSec: 4.0,
    text: c.text,
    postDenonVol: c.postDenonVol || 0,
  }));
  const fxRows = collectRyFxCues().map(fc => ({
    ...fc,
    showAt: fc.target === 'intro' ? fc.atSec : (introSec + fc.atSec),
  }));

  rySimState.cueFlags = {
    spec,
    lightning: false,
    flash1: false,
    flash2: false,
    screens: false,
    lightsOn: false,
    lastPulse: 0,
    fired: {},
    speechCues: spRows,
    fxCues: fxRows,
  };
  rySimState.iv = setInterval(() => {
    if (!rySimState.running) return;
    if (rySimState.fogLeft > 0) {
      rySimState.fogLeft -= 1;
      if ($('devFogHud')) {
        if (rySimState.fogLeft > 0) $('devFogHud').textContent = `(TRIGGER FOG · ${rySimState.fogLeft}s)`;
        else { $('devFogHud').textContent = 'FOG: DONE'; $('devFogHud').className = 'fx-tag'; }
      }
    }
  }, 1000);

  const seekTarget = jumpToSec != null ? Math.max(0, introSec + Number(jumpToSec)) : 0;
  if (jumpToSec != null) {
    fxSetLights('on');
    $('devDenonHud').textContent = `DENON: VOL ${spec.denonVol || 70}`;
    rySimState.cueFlags.lightning = rySimState.cueFlags.flash1 = rySimState.cueFlags.flash2 = rySimState.cueFlags.screens = rySimState.cueFlags.lightsOn = true;
    logDevCue(`⏩ Jumped MP4 to ${jumpToSec}s past intro (${seekTarget.toFixed(1)}s)…`);
  } else {
    fxApplyCue({ tag: 'lights_off', label: '(LIGHTS OFF) · (TRIGGER FOG · 30s)', fogSec: spec.fogBurstSec || 30, denonVol: spec.denonVol || 70, caption: spec.announcerText || 'Thunder rumbling…' });
  }

  const wantUrl = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  const startPlay = () => {
    if (!rySimState.running) return;
    try { vid.currentTime = seekTarget; } catch {}
    vid.play().catch(() => {});
  };
  vid.onloadedmetadata = startPlay;
  vid.src = wantUrl;
  vid.load();
}

function syncShowActiveBadge(active) {
  latestShowActive = active || { active: false };
  const isLive = Boolean(active && active.active);
  const b = $('shBadge');
  if (b) {
    if (isLive) {
      b.textContent = `LIVE · ${active.showName || active.showId} (${active.phase || 'running'})`;
      b.className = 'pill warn';
    } else {
      b.textContent = 'IDLE';
      b.className = 'pill ok';
    }
  }
  if ($('topStopShowBtn')) $('topStopShowBtn').hidden = !isLive;
  if ($('navDotShow')) $('navDotShow').className = 'dot ' + (isLive ? 'warn' : 'ok');
}

function populateFxHookInputs(fx) {
  if (!fx) return;
  if ($('fxLightsHost')) $('fxLightsHost').value = fx.lightsHost || '';
  if ($('fxLightsOffUrl')) $('fxLightsOffUrl').value = fx.lightsOffUrl || '';
  if ($('fxLightsOnUrl')) $('fxLightsOnUrl').value = fx.lightsOnUrl || '';
  if ($('fxLightsRackHost')) $('fxLightsRackHost').value = fx.lightsRackHost || '';
  if ($('fxLightsRackOffUrl')) $('fxLightsRackOffUrl').value = fx.lightsRackOffUrl || '';
  if ($('fxLightsRackOnUrl')) $('fxLightsRackOnUrl').value = fx.lightsRackOnUrl || '';
  if ($('fxLightsSmithHost')) $('fxLightsSmithHost').value = fx.lightsSmithHost || '';
  if ($('fxLightsSmithOffUrl')) $('fxLightsSmithOffUrl').value = fx.lightsSmithOffUrl || '';
  if ($('fxLightsSmithOnUrl')) $('fxLightsSmithOnUrl').value = fx.lightsSmithOnUrl || '';
  if ($('fxFogHost')) $('fxFogHost').value = fx.fogHost || '';
  if ($('fxTheaterHost')) $('fxTheaterHost').value = fx.theaterHost || '';
  if ($('fxFogUrl')) $('fxFogUrl').value = fx.fogTriggerUrl || '';
  if ($('fxFogBurst')) $('fxFogBurst').value = String(fx.fogBurstSec != null ? fx.fogBurstSec : 30);
  if ($('fxLightsLeadSec')) $('fxLightsLeadSec').value = String(fx.lightsLeadSec != null ? fx.lightsLeadSec : 0);
}

function populateTtsProviders(p) {
  if (!p) return;
  if ($('ttsElHint')) $('ttsElHint').textContent = p.hasElevenlabsKey ? `(Saved ${p.elevenlabsHint})` : '';
  if ($('ttsGemHint')) $('ttsGemHint').textContent = p.hasGeminiKey ? `(Saved ${p.geminiHint})` : '';
  if ($('ttsOaiHint')) $('ttsOaiHint').textContent = p.hasOpenaiKey ? `(Saved ${p.openaiHint})` : '';
  if ($('ttsElModel') && p.elevenlabsModel) $('ttsElModel').value = p.elevenlabsModel;
  const kp = $('ttsKeysPill');
  if (kp) {
    const active = [];
    if (p.hasElevenlabsKey) active.push('EL');
    if (p.hasGeminiKey) active.push('GEM');
    if (p.hasOpenaiKey) active.push('OAI');
    kp.textContent = active.length ? active.join(' · ') : 'Local Kokoro Only';
    kp.className = active.length ? 'pill ok' : 'pill';
  }
}

const TOP_VOICES = new Set([
  'elevenlabs:nPczCjzI2devNBz1zQrb',
  'gemini:Fenrir',
  'elevenlabs:N2lVS1w4EtoT3dr4eOWO',
  'edge:en-US-BrianMultilingualNeural',
  'kokoro:onyx_fenrir_epic',
  'kokoro:am_onyx',
]);

function buildGroupedVoiceOptionsHtml(voices, selectedId) {
  const list = Array.isArray(voices) ? voices : [];
  if (!list.length) {
    return selectedId ? `<option value="${escHtml(selectedId)}" selected>${escHtml(selectedId)}</option>` : '';
  }
  const groups = [
    ['elevenlabs', 'ElevenLabs Studio (Hyper-Realistic)'],
    ['gemini', 'Google Gemini 2.5 Flash TTS (Free AI Studio Key)'],
    ['openai', 'OpenAI gpt-4o-mini-tts (Directed)'],
    ['kokoro', 'Local Offline (Kokoro-82M FP32)'],
    ['edge', 'Free Cloud (Microsoft Edge HD)'],
  ];
  let html = '';
  groups.forEach(([eng, glabel]) => {
    const items = list.filter(v => v.engine === eng);
    if (!items.length) return;
    html += `<optgroup label="${escHtml(glabel)}">`;
    items.forEach(v => {
      const prefix = TOP_VOICES.has(v.id) ? '★ ' : '';
      const suffix = (v.requiresKey && !v.available) ? ' (needs API key)' : '';
      const sel = v.id === selectedId ? ' selected' : '';
      html += `<option value="${escHtml(v.id)}"${sel}>${escHtml(prefix + v.label + suffix)}</option>`;
    });
    html += '</optgroup>';
  });
  if (!html) {
    html = list.map(v => {
      const sel = v.id === selectedId ? ' selected' : '';
      return `<option value="${escHtml(v.id)}"${sel}>${escHtml((TOP_VOICES.has(v.id) ? '★ ' : '') + v.label)}</option>`;
    }).join('');
  }
  return html;
}

function buildInlineTagBarHtml(targetId) {
  const tid = escHtml(targetId || '');
  return `
    <div class="tag-bar sh-tag-bar" data-target="${tid}">
      <span style="font-size:11px;color:var(--mut);font-weight:700">Hype &amp; Speed:</span>
      <button class="tag-chip" type="button" data-tag=" [excited] ">⚡ Hype [excited]</button>
      <button class="tag-chip" type="button" data-tag=" [shouting] ">🔥 Shout [shouting]</button>
      <button class="tag-chip" type="button" data-tag=" [dramatic] ">🎭 [dramatic]</button>
      <button class="tag-chip" type="button" data-tag=" [rushed] ">💨 [rushed]</button>
      <button class="tag-chip" type="button" data-wrap="speed:+10%">⏩ Faster +10%</button>
      <button class="tag-chip" type="button" data-wrap="speed:+20%">⏩ Faster +20%</button>
      <button class="tag-chip" type="button" data-wrap="speed:-10%">🐢 Slower -10%</button>
      <button class="tag-chip" type="button" data-wrap="vol:+3dB">🔊 Louder +3dB</button>
      <button class="tag-chip" type="button" data-tag=" [pause:100ms] ">⏱ +0.1s</button>
      <button class="tag-chip" type="button" data-tag=" [pause:350ms] ">⏱ +0.35s</button>
      <button class="tag-chip" type="button" data-tag=" [pause:750ms] ">⏱ +0.75s</button>
    </div>`;
}

function applyInlineTagToTextarea(ta, btn) {
  if (!ta || !btn) return;
  const val = String(ta.value || '');
  const s0 = ta.selectionStart != null ? ta.selectionStart : val.length;
  const s1 = ta.selectionEnd != null ? ta.selectionEnd : val.length;
  const focused = ta.dataset.userFocused === '1';
  const trimLen = val.trimEnd().length;
  const tagRaw = btn.getAttribute('data-tag');
  const wrapTag = btn.getAttribute('data-wrap');
  let nextVal = val;
  let caretPos = s0;

  if (tagRaw) {
    const cleanTag = tagRaw.trim();
    const isEnergy = /^\\[(excited|shouting|dramatic|rushed)\\]$/i.test(cleanTag);
    if (!focused && isEnergy) {
      const rest = val.trimStart();
      const ins = rest ? `${cleanTag} ${rest}` : `${cleanTag} `;
      nextVal = ins;
      caretPos = cleanTag.length + 1;
    } else {
      const pos = s0;
      const before = val.slice(0, pos);
      const after = val.slice(pos);
      const lead = (before && !/\\s$/.test(before)) ? ' ' : '';
      const trail = (!after || /^\\s/.test(after)) ? (after ? '' : ' ') : ' ';
      const ins = `${lead}${cleanTag}${trail}`;
      nextVal = before + ins + after;
      caretPos = before.length + ins.length;
    }
  } else if (wrapTag) {
    if (s1 > s0) {
      const sel = val.slice(s0, s1);
      const closeTag = wrapTag.split(':')[0];
      const ins = `[${wrapTag}]${sel}[/${closeTag}]`;
      nextVal = val.slice(0, s0) + ins + val.slice(s1);
      caretPos = s0 + ins.length;
    } else {
      const isMid = focused && s0 > 0 && s0 < trimLen;
      if (!isMid) {
        const rest = val.trimStart();
        const prefix = `[${wrapTag}] `;
        nextVal = prefix + rest;
        caretPos = prefix.length;
      } else {
        const before = val.slice(0, s0);
        const after = val.slice(s0);
        const lead = (before && !/\\s$/.test(before)) ? ' ' : '';
        const trail = (after && !/^\\s/.test(after)) ? ' ' : '';
        const ins = `${lead}[${wrapTag}]${trail}`;
        nextVal = before + ins + after;
        caretPos = before.length + ins.length;
      }
    }
  }

  ta.value = nextVal;
  ta.dataset.userFocused = '1';
  ta.focus();
  ta.selectionStart = ta.selectionEnd = caretPos;
}

function onShowAccToggle(id, isOpen) {
  if (!id) return;
  if (isOpen) openShowIds.add(id);
  else openShowIds.delete(id);
}

function onShowSubToggle(key, isOpen) {
  if (!key) return;
  if (isOpen) {
    openShowSubIds.add(key);
    closedShowSubIds.delete(key);
  } else {
    openShowSubIds.delete(key);
    closedShowSubIds.add(key);
  }
}

function populateRyderDevPanel(ry, voices, kokoroReady, stageClients, ttsProviders) {
  populateTtsProviders(ttsProviders);
  const kp = $('devKokoroPill');
  if (kp) {
    const activeEngine = (ry && String(ry.announcerVoice || '').split(':')[0]) || 'kokoro';
    const tagMap = {
      elevenlabs: 'STUDIO CLOUD: ELEVENLABS',
      gemini: 'STUDIO CLOUD: GEMINI 2.5 TTS',
      openai: 'STUDIO CLOUD: OPENAI TTS',
      kokoro: kokoroReady ? 'LOCAL OFFLINE: KOKORO-82M' : 'STUDIO VOICE',
      edge: 'CLOUD FREE: EDGE HD',
    };
    kp.textContent = tagMap[activeEngine] || (kokoroReady ? 'LOCAL OFFLINE: KOKORO-82M' : 'STUDIO VOICE');
    kp.className = (activeEngine === 'elevenlabs' || activeEngine === 'gemini' || activeEngine === 'openai') ? 'pill ok' : 'pill live';
  }
  const sp = $('devStagePill');
  if (sp && ry) {
    const wbCnt = Number((stageClients && stageClients.wallboard) || 0);
    const mp4Info = ry.showVideoReady && ry.hasSourceVideo
      ? `MP4 READY (${Number(ry.showVideoDurationSec || 0).toFixed(0)}s)`
      : (ry.videoUploaded ? 'MP4 NEEDS REBUILD' : 'NO ENTRANCE MP4');
    sp.textContent = `${mp4Info} · WALLBOARD ${wbCnt > 0 ? 'CONNECTED' : 'IDLE'}`;
    sp.className = (ry.showVideoReady && ry.hasSourceVideo) ? 'pill ok' : 'pill warn';
  }
  const vSel = $('devRyVoice');
  if (Array.isArray(voices) && voices.length) devRyVoicesCache = voices;
  if (vSel && Array.isArray(voices) && voices.length) {
    vSel.innerHTML = buildGroupedVoiceOptionsHtml(voices, (ry && ry.announcerVoice) || 'kokoro:onyx_fenrir_epic');
  }
  if (!ry) return;
  devRyderSpec = ry;
  if (vSel) vSel.value = ry.announcerVoice || 'kokoro:onyx_fenrir_epic';
  if ($('devRyExcludeMainTv')) $('devRyExcludeMainTv').checked = Boolean(ry.excludeMainTv);
  if ($('devRyIntroStart')) $('devRyIntroStart').value = String(ry.introStartSec != null ? ry.introStartSec : 2.3);
  if ($('devRyRev')) $('devRyRev').value = String(ry.voiceReverbWet != null ? ry.voiceReverbWet : 0.06);
  if ($('devRyBoost')) $('devRyBoost').value = String(ry.voiceBoostDb != null ? ry.voiceBoostDb : 2.0);
  if ($('devRyFog')) $('devRyFog').value = String(ry.fogBurstSec || 30);
  if ($('devRyStyle')) $('devRyStyle').value = ry.voiceStyle || '';
  if ($('devRyIntroTxt')) $('devRyIntroTxt').value = ry.announcerText || '';
  if ($('devRyIntroVoiceClr')) $('devRyIntroVoiceClr').hidden = !ry.customIntroVoiceFile;
  if ($('devRyOvVoiceClr')) $('devRyOvVoiceClr').hidden = !ry.customOverlayVoiceFile;
  renderRySpeechRows(Array.isArray(ry.speechCues) ? ry.speechCues : [], devRyVoicesCache);
  renderRyFxRows(Array.isArray(ry.fxCues) ? ry.fxCues : []);
  if ($('shAudio_ryder_walkin')) $('shAudio_ryder_walkin').src = `/api/shows/ryder_walkin/preview.wav?t=${Date.now()}`;
  if ($('shOvAudio_ryder_walkin')) $('shOvAudio_ryder_walkin').src = `/api/shows/ryder_walkin/overlay.wav?t=${Date.now()}`;
  initRyderScrubber();
  const vid = $('devRyVideo');
  if (vid && ry.showVideoReady && !rySimState.running && (!vid.getAttribute('src') || !vid.currentSrc)) {
    vid.src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  }
  updateRyScrubAndFlash();
}

function buildVoiceOverrideOptions(voices, selected) {
  let out = `<option value="" ${!selected ? 'selected' : ''}>Show Default Voice</option>`;
  (Array.isArray(voices) ? voices : []).forEach(v => {
    const sel = selected === v.id ? 'selected' : '';
    out += `<option value="${escHtml(v.id)}" ${sel}>${escHtml(v.label)}</option>`;
  });
  return out;
}

function makeRySpeechRowHtml(c, voices, idx) {
  const cid = escHtml(c.id || `sc_${idx + 1}`);
  const tgt = c.target === 'intro' ? 'intro' : 'video';
  const atStr = escHtml(fmtTimeSec(c.atSec != null ? c.atSec : 80));
  const pvol = c.postDenonVol ? String(c.postDenonVol) : '';
  const txt = escHtml(c.text || '');
  return `
    <div class="cue-row ry-sp-row" data-cueid="${cid}">
      <div class="cue-row-top">
        <select class="ry-sp-target" title="Stage segment">
          <option value="video" ${tgt==='video'?'selected':''}>Entrance Video</option>
          <option value="intro" ${tgt==='intro'?'selected':''}>Intro Sequence</option>
        </select>
        <label style="margin:0;font-size:11px">At</label>
        <input class="ry-sp-at" type="text" value="${atStr}" style="width:76px" title="mm:ss.s or seconds">
        <button class="btn sm sec ry-sp-stamp" type="button" title="Capture current scrubber timestamp">📍 Stamp</button>
        <button class="btn sm sec ry-sp-jump" type="button" title="Seek video to 2s before cue">⏩ Jump</button>
        <label style="margin:0;font-size:11px">Denon After</label>
        <input class="ry-sp-vol" type="number" min="0" max="75" value="${escHtml(pvol)}" placeholder="—" style="width:62px" title="Optional Denon volume after speech">
        <select class="ry-sp-voice" style="max-width:210px">${buildVoiceOverrideOptions(voices, c.voice || '')}</select>
        <button class="btn sm ok ry-sp-test" type="button">▶ Test</button>
        <button class="btn sm red ry-sp-del" type="button" title="Remove speech box">✕</button>
      </div>
      <textarea class="ry-sp-txt" rows="2" placeholder="Enter speech box script (supports [pause:100ms], [speed:+10%], [shouting])">${txt}</textarea>
    </div>`;
}

function renderRySpeechRows(cues, voices) {
  const list = $('devRySpeechList');
  if (!list) return;
  const rows = Array.isArray(cues) ? cues : [];
  if (!rows.length) {
    list.innerHTML = '<div class="ry-empty" style="font-size:11px;color:var(--mut)">No timed speech boxes yet — click <b>+ Add Speech Box</b> to drop one onto the Intro or Entrance Video.</div>';
    return;
  }
  list.innerHTML = rows.map((c, idx) => makeRySpeechRowHtml(c, voices, idx)).join('');
}

function addRySpeechRow(cue) {
  const list = $('devRySpeechList');
  if (!list) return;
  const emptyEl = list.querySelector('.ry-empty');
  if (emptyEl) emptyEl.remove();
  const vid = $('devRyVideo');
  const t = (vid && Number.isFinite(vid.currentTime)) ? vid.currentTime : 0;
  const introSec = getRyIntroSec();
  const def = cue || (t > 0.1
    ? (t < introSec ? { target: 'intro', atSec: t } : { target: 'video', atSec: t - introSec })
    : { target: 'video', atSec: 80 });
  const idx = list.querySelectorAll('.ry-sp-row').length;
  list.insertAdjacentHTML('beforeend', makeRySpeechRowHtml(def, devRyVoicesCache, idx));
}

function collectRySpeechCues() {
  const rows = [];
  document.querySelectorAll('#devRySpeechList .ry-sp-row').forEach((row, idx) => {
    const pvRaw = row.querySelector('.ry-sp-vol').value.trim();
    const pvNum = pvRaw === '' ? 0 : Math.round(Number(pvRaw) || 0);
    rows.push({
      id: row.getAttribute('data-cueid') || `sc_${idx + 1}`,
      target: row.querySelector('.ry-sp-target').value === 'intro' ? 'intro' : 'video',
      atSec: parseTimeSec(row.querySelector('.ry-sp-at').value),
      text: row.querySelector('.ry-sp-txt').value.trim(),
      voice: row.querySelector('.ry-sp-voice').value || '',
      postDenonVol: pvNum,
      duckGain: 0.22,
    });
  });
  return rows;
}

function makeRyFxRowHtml(fc, idx) {
  const cid = escHtml(fc.id || `fx_${idx + 1}`);
  const tgt = fc.target === 'intro' ? 'intro' : 'video';
  const ftype = fc.type || 'flash';
  const fzone = fc.zone || 'all';
  const atStr = escHtml(fmtTimeSec(fc.atSec != null ? fc.atSec : 0));
  const dur = fc.durationSec != null ? fc.durationSec : (ftype === 'fog' ? 4.0 : 0.2);
  const cnt = fc.count != null ? fc.count : 1;
  const scr = fc.screenFlash !== false;
  return `
    <div class="cue-row ry-fx-row" data-fxid="${cid}">
      <div class="cue-row-top">
        <select class="ry-fx-target">
          <option value="video" ${tgt==='video'?'selected':''}>Entrance Video</option>
          <option value="intro" ${tgt==='intro'?'selected':''}>Intro Sequence</option>
        </select>
        <label style="margin:0;font-size:11px">At</label>
        <input class="ry-fx-at" type="text" value="${atStr}" style="width:76px">
        <button class="btn sm sec ry-fx-stamp" type="button" title="Capture current scrubber timestamp">📍 Stamp</button>
        <button class="btn sm sec ry-fx-jump" type="button" title="Seek video to 2s before FX cue">⏩ Jump</button>
        <select class="ry-fx-type">
          <option value="flash" ${ftype==='flash'?'selected':''}>Flash (Screen + Lights)</option>
          <option value="lights_off" ${ftype==='lights_off'?'selected':''}>Lights OFF</option>
          <option value="lights_on" ${ftype==='lights_on'?'selected':''}>Lights ON</option>
          <option value="fog" ${ftype==='fog'?'selected':''}>Fog Burst</option>
        </select>
        <select class="ry-fx-zone" title="Light zone">
          <option value="all" ${fzone==='all'?'selected':''}>Both Sides</option>
          <option value="rack" ${fzone==='rack'?'selected':''}>Rack Side</option>
          <option value="smith" ${fzone==='smith'?'selected':''}>Smith Side</option>
          <option value="alternate" ${fzone==='alternate'?'selected':''}>Alternate ↔</option>
        </select>
        <label style="margin:0;font-size:11px">Dur(s)</label>
        <input class="ry-fx-dur" type="number" min="0.1" max="60" step="0.1" value="${escHtml(String(dur))}" style="width:64px">
        <label style="margin:0;font-size:11px">Count</label>
        <input class="ry-fx-cnt" type="number" min="1" max="6" step="1" value="${escHtml(String(cnt))}" style="width:52px">
        <label style="margin:0;font-size:11px;display:inline-flex;align-items:center;gap:4px;cursor:pointer">
          <input class="ry-fx-screen" type="checkbox" ${scr?'checked':''} style="width:auto;margin:0"> Screen Flash
        </label>
        <button class="btn sm red ry-fx-del" type="button" title="Remove FX cue">✕</button>
      </div>
    </div>`;
}

function renderRyFxRows(cues) {
  const list = $('devRyFxCueList');
  if (!list) return;
  const rows = Array.isArray(cues) ? cues : [];
  if (!rows.length) {
    list.innerHTML = '<div class="ry-empty" style="font-size:11px;color:var(--mut)">No custom FX cues yet — scrub to an exact moment, click <b>+ Add FX Cue</b>, then hit <b>📍 Stamp</b>.</div>';
    return;
  }
  list.innerHTML = rows.map((fc, idx) => makeRyFxRowHtml(fc, idx)).join('');
}

function addRyFxRow(cue) {
  const list = $('devRyFxCueList');
  if (!list) return;
  const emptyEl = list.querySelector('.ry-empty');
  if (emptyEl) emptyEl.remove();
  const vid = $('devRyVideo');
  const t = (vid && Number.isFinite(vid.currentTime)) ? vid.currentTime : 0;
  const introSec = getRyIntroSec();
  const def = cue || (t < introSec ? { target: 'intro', atSec: t, type: 'flash' } : { target: 'video', atSec: t - introSec, type: 'flash' });
  const idx = list.querySelectorAll('.ry-fx-row').length;
  list.insertAdjacentHTML('beforeend', makeRyFxRowHtml(def, idx));
}

function collectRyFxCues() {
  const rows = [];
  document.querySelectorAll('#devRyFxCueList .ry-fx-row').forEach((row, idx) => {
    rows.push({
      id: row.getAttribute('data-fxid') || `fx_${idx + 1}`,
      target: row.querySelector('.ry-fx-target').value === 'intro' ? 'intro' : 'video',
      atSec: parseTimeSec(row.querySelector('.ry-fx-at').value),
      type: row.querySelector('.ry-fx-type').value || 'flash',
      zone: (row.querySelector('.ry-fx-zone') && row.querySelector('.ry-fx-zone').value) || 'all',
      durationSec: Math.max(0.1, Math.min(60, Number(row.querySelector('.ry-fx-dur').value || 0.2))),
      count: Math.max(1, Math.min(6, Math.round(Number(row.querySelector('.ry-fx-cnt').value || 1)))),
      screenFlash: Boolean(row.querySelector('.ry-fx-screen').checked),
    });
  });
  return rows;
}

function renderShows(data) {
  const grid = $('shGrid');
  if (!grid || !data) return;
  syncShowActiveBadge(data.active || {});
  populateFxHookInputs(data.fxHooks || {});
  const allShows = Array.isArray(data.shows) ? data.shows : [];
  latestShowsList = allShows;
  const ryder = allShows.find(s => s.id === 'ryder_walkin');
  populateRyderDevPanel(ryder, data.voices || [], Boolean(data.kokoroReady), data.stageClients || {}, data.ttsProviders || null);
  const others = allShows.filter(s => s.id !== 'ryder_walkin');
  if ($('shCountPill')) $('shCountPill').textContent = `${others.length} show${others.length === 1 ? '' : 's'}`;
  if (!others.length) {
    grid.innerHTML = '<div style="color:var(--mut);font-size:12px">No other shows configured.</div>';
    return;
  }
  const voicesList = (Array.isArray(data.voices) && data.voices.length) ? data.voices : devRyVoicesCache;
  grid.innerHTML = others.map(s => {
    const id = escHtml(s.id);
    const isOpen = openShowIds.has(s.id);
    const trigsList = Array.isArray(s.triggers) ? s.triggers : [];
    const trigs = trigsList.map(t => `<code>"Armory, ${escHtml(t)}"</code>`).join(' ');
    const trigHint = trigsList.length
      ? `<code>"Armory, ${escHtml(trigsList[0])}"</code>${trigsList.length > 1 ? ` <span class="show-acc-sub">+${trigsList.length - 1} more</span>` : ''}`
      : '';
    const srcTag = s.sourceUploaded ? `<span class="pill ok">CUSTOM: ${escHtml(s.sourceFile)}</span>` : '<span class="pill">DEMO TRACK</span>';
    const vidPill = (s.showVideoReady && s.hasSourceVideo) ? '<span class="pill ok">3-SCREEN MP4 READY</span>' : '';
    const durTag = s.previewReady ? `${s.previewDurationSec}s ready` : 'Not built';
    const theaterOpts = [
      ['', 'None'],
      ['jenna', 'jenna (Missy UV/Gold)'],
      ['avenleigh', 'avenleigh (Pink VIP)'],
      ['vip_prism', 'vip_prism (Modern Prism)'],
      ['vip_gold', 'vip_gold (Velvet Gold)'],
      ['walkin', 'walkin (Crimson Comet)'],
      ['pr', 'pr (PR Gold Burst)'],
      ['thunder', 'thunder (Storm Indigo)'],
      ['fireworks', 'fireworks (Pyro Sky)'],
      ['bench_hype', 'bench_hype (Forge Red)'],
    ].map(([val, lbl]) => `<option value="${val}" ${(s.theaterScene || '') === val ? 'selected' : ''}>${lbl}</option>`).join('');
    const voiceOpts = buildGroupedVoiceOptionsHtml(voicesList, s.announcerVoice || 'kokoro:onyx_fenrir_epic');
    const vidSubKey = `${s.id}:video`;
    const cfgSubKey = `${s.id}:cfg`;
    const vidSubOpen = openShowSubIds.has(vidSubKey);
    const cfgSubOpen = openShowSubIds.has(cfgSubKey) || (s.kind === 'pr_drop' && !closedShowSubIds.has(cfgSubKey));
    const videoSection = (s.showVideoReady && s.hasSourceVideo)
      ? `<details class="sh-sub" ${vidSubOpen ? 'open' : ''} ontoggle="onShowSubToggle('${vidSubKey}', this.open)">
          <summary>🎬 3-Screen Entrance Video</summary>
          <div class="sh-sub-body">
            <video controls preload="none" src="/api/shows/${id}/show.mp4" style="width:100%;max-height:220px;border-radius:8px;background:#000"></video>
          </div>
        </details>`
      : '';
    const scriptSection = s.kind !== 'pr_drop'
      ? `<div>
          <label>Announcer Script (supports <code>[excited]</code>, <code>[shouting]</code>, <code>[speed:+10%]</code>, <code>[vol:+3dB]</code>, <code>[pause:350ms]</code>)</label>
          <textarea id="shAnn_${id}" rows="3">${escHtml(s.announcerText || '')}</textarea>
          ${buildInlineTagBarHtml('shAnn_' + id)}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px">
          <div><label>Studio Voice Engine</label><select id="shVoice_${id}">${voiceOpts}</select></div>
          <div><label>Voice Acting Style / Direction</label><input id="shStyle_${id}" type="text" value="${escHtml(s.voiceStyle || '')}" placeholder="Electric hype announcer..."></div>
        </div>`
      : '';
    return `
      <details class="show-item show-acc" data-showid="${id}" ${isOpen ? 'open' : ''} ontoggle="onShowAccToggle('${id}', this.open)">
        <summary class="show-acc-sum">
          <span class="show-acc-left">
            <span class="show-chev">▸</span>
            <span class="show-acc-title">${escHtml(s.name)}</span>
            ${trigHint}
            ${vidPill}${srcTag}<span class="pill live">${durTag}</span>
          </span>
          <span class="show-acc-acts">
            <button class="btn sm ok" type="button" title="Preview locally" onclick="event.preventDefault(); event.stopPropagation(); previewShow('${id}')">▶</button>
            <button class="btn sm" type="button" title="Fire live (Denon ${s.denonVol})" onclick="event.preventDefault(); event.stopPropagation(); fireShow('${id}')">🔥 Fire</button>
          </span>
        </summary>
        <div class="show-acc-body">
          <audio class="show-audio" id="shAudio_${id}" controls preload="none" src="/api/shows/${id}/preview.wav"></audio>
          <div class="show-trigs">${trigs}</div>
          <div class="ctrl-row" style="margin-top:0">
            <button class="btn sm ok" type="button" onclick="previewShow('${id}')">▶ Preview Mix</button>
            <button class="btn sm" type="button" onclick="fireShow('${id}')">🔥 Fire Live (Denon ${s.denonVol})</button>
            <button class="btn sm sec" type="button" onclick="rebuildShow('${id}')">↻ Rebuild</button>
            ${s.kind !== 'pr_drop' ? `<button class="btn sm ok" type="button" onclick="auditionShowVoice('${id}', this)">🎙 Test Voice</button>` : ''}
          </div>
          ${scriptSection}
          <div class="ctrl-row" style="margin-top:0">
            <button class="btn sm" type="button" onclick="saveShowConfig('${id}', this)">Save &amp; Rebuild Mix</button>
            <span id="shOut_${id}" style="color:var(--mut);font-size:11px"></span>
          </div>
          ${videoSection}
          <details class="sh-sub" ${cfgSubOpen ? 'open' : ''} ontoggle="onShowSubToggle('${cfgSubKey}', this.open)">
            <summary>⚙ Audio, Timing, LED Scene &amp; Song Upload</summary>
            <div class="sh-sub-body">
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:6px">
                <div><label>Denon Vol</label><input id="shVol_${id}" type="number" min="30" max="75" value="${s.denonVol}"></div>
                <div><label>Cube Mode</label><select id="shMode_${id}">
                  <option value="duck" ${s.cubeMode==='duck'?'selected':''}>duck</option>
                  <option value="takeover" ${s.cubeMode==='takeover'?'selected':''}>takeover</option>
                  <option value="overlay" ${s.cubeMode==='overlay'?'selected':''}>overlay</option>
                </select></div>
                <div><label>Theater LED Scene</label><select id="shTheater_${id}">${theaterOpts}</select></div>
                <div><label>Start (s)</label><input id="shStart_${id}" type="number" min="0" step="0.25" value="${s.songStartSec}"></div>
                <div><label>Dur (s)</label><input id="shDur_${id}" type="number" min="0" max="65" step="0.5" value="${s.songDurationSec}"></div>
                <div><label>Fade (s)</label><input id="shFade_${id}" type="number" min="0.3" max="10" step="0.2" value="${s.fadeOutSec}"></div>
                <div><label>Flashes (0–6)</label><input id="shFlash_${id}" type="number" min="0" max="6" step="1" value="${s.lightsFlashCount || 0}"></div>
              </div>
              <div class="ctrl-row" style="margin-top:0">
                <label style="margin:0;font-size:11px;display:inline-flex;align-items:center;gap:4px;cursor:pointer">
                  <input id="shCheer_${id}" type="checkbox" ${s.addCheer ? 'checked' : ''} style="width:auto;margin:0"> Add Crowd Cheer
                </label>
              </div>
              <div class="ctrl-row" style="margin-top:0">
                <label style="margin:0;flex:1;min-width:170px">Swap Song (.mp3/.wav): <input id="shFile_${id}" type="file" accept=".mp3,.wav,.m4a,.ogg" onchange="uploadShowSong('${id}', this)"></label>
                ${s.sourceUploaded ? `<button class="btn sm sec" type="button" onclick="clearShowSong('${id}')">Revert Demo</button>` : ''}
              </div>
            </div>
          </details>
        </div>
      </details>`;
  }).join('');
}

async function loadShows() {
  ensureShowSse();
  try {
    const r = await fetch('/api/shows').then(x => x.json());
    renderShows(r);
  } catch (e) {
    console.error('loadShows error:', e);
  }
}

function previewShow(id) {
  const el = $('shAudio_' + id);
  if (!el) return;
  el.src = `/api/shows/${encodeURIComponent(id)}/preview.wav?t=${Date.now()}`;
  el.load();
  el.play().catch(() => {});
  if ($('shOut_' + id)) $('shOut_' + id).textContent = 'Playing locally in browser';
}

async function fireShow(id) {
  if ($('shOut_' + id)) $('shOut_' + id).textContent = 'Firing live on Denon…';
  if ($('ovDeckOut')) $('ovDeckOut').textContent = `Firing ${id}…`;
  try {
    const r = await fetch(`/api/shows/${encodeURIComponent(id)}/fire`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    const msg = r.ok ? `🔥 Fired (${r.showName || id})` : `✗ ${r.error || 'Failed'}`;
    if ($('shOut_' + id)) $('shOut_' + id).textContent = msg;
    if ($('ovDeckOut')) $('ovDeckOut').textContent = msg;
    showToast(msg, r.ok ? 'ok' : 'err');
    await pollStatus();
  } catch {
    if ($('shOut_' + id)) $('shOut_' + id).textContent = '✗ Fire failed';
  }
}

async function stopShow() {
  try {
    await fetch('/api/shows/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
    if ($('ovDeckOut')) $('ovDeckOut').textContent = '⏹ Show stopped safely';
    showToast('⏹ Show stopped safely', 'ok');
    await pollStatus();
  } catch {}
}

async function rebuildShow(id) {
  if ($('shOut_' + id)) $('shOut_' + id).textContent = 'Building mix…';
  try {
    const r = await fetch(`/api/shows/${encodeURIComponent(id)}/rebuild`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    if (r.status) renderShows(r.status);
    if ($('shOut_' + id)) $('shOut_' + id).textContent = r.ok ? `✓ Rebuilt (${r.durationSec}s)` : `✗ ${r.error || 'Failed'}`;
  } catch {
    if ($('shOut_' + id)) $('shOut_' + id).textContent = '✗ Rebuild failed';
  }
}

async function auditionShowVoice(id, btn) {
  const ta = $('shAnn_' + id);
  const text = (ta && ta.value.trim()) || '';
  const outEl = $('shOut_' + id);
  const audioEl = $('shAudio_' + id);
  if (!text) {
    if (outEl) outEl.textContent = '✗ Enter announcer script first';
    return;
  }
  const spec = (latestShowsList || []).find(x => x.id === id) || {};
  const chosenVoice = ($('shVoice_' + id) && $('shVoice_' + id).value) || spec.announcerVoice || 'kokoro:onyx_fenrir_epic';
  const voiceStyle = ($('shStyle_' + id) && $('shStyle_' + id).value.trim()) || '';
  if (btn) btn.disabled = true;
  if (outEl) outEl.textContent = 'Auditioning voice (mix untouched)…';
  try {
    const resp = await fetch('/api/shows/audition-voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slot: 'intro',
        text,
        voice: chosenVoice,
        voiceStyle,
        voiceReverbWet: spec.voiceReverbWet != null ? Number(spec.voiceReverbWet) : 0.09,
        voiceBoostDb: spec.voiceBoostDb != null ? Number(spec.voiceBoostDb) : 2.0,
        withFx: false,
      }),
    });
    if (!resp.ok) {
      let errMsg = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j && j.error) errMsg = j.error;
      } catch {}
      if (outEl) outEl.textContent = `✗ ${errMsg}`;
      return;
    }
    const durHdr = resp.headers.get('X-Armory-Duration-Sec') || '?';
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    if (showAuditionBlobUrls[id]) {
      try { URL.revokeObjectURL(showAuditionBlobUrls[id]); } catch {}
    }
    showAuditionBlobUrls[id] = url;
    if (audioEl) {
      audioEl.src = url;
      audioEl.load();
      audioEl.play().catch(() => {});
    }
    if (outEl) outEl.textContent = `✓ Playing voice audition (${durHdr}s — mix untouched)`;
  } catch (e) {
    if (outEl) outEl.textContent = `✗ Audition failed: ${(e && e.message) || 'error'}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveShowConfig(id, btn) {
  const body = {
    denonVol: Number($('shVol_' + id).value || 70),
    cubeMode: $('shMode_' + id).value || 'duck',
    songStartSec: Number($('shStart_' + id).value || 0),
    songDurationSec: Number($('shDur_' + id).value || 15),
    fadeOutSec: Number($('shFade_' + id).value || 2.5),
  };
  if ($('shFlash_' + id)) body.lightsFlashCount = Math.max(0, Math.min(6, Math.round(Number($('shFlash_' + id).value || 0))));
  if ($('shTheater_' + id)) body.theaterScene = $('shTheater_' + id).value || '';
  if ($('shCheer_' + id)) body.addCheer = Boolean($('shCheer_' + id).checked);
  if ($('shAnn_' + id)) body.announcerText = $('shAnn_' + id).value.trim();
  if ($('shVoice_' + id) && $('shVoice_' + id).value) body.announcerVoice = $('shVoice_' + id).value;
  if ($('shStyle_' + id)) body.voiceStyle = $('shStyle_' + id).value.trim();
  if ($('shYt_' + id)) body.youtubeUrl = $('shYt_' + id).value.trim();
  if ($('shOut_' + id)) $('shOut_' + id).textContent = 'Saving & rendering…';
  try {
    const r = await fetch(`/api/shows/${encodeURIComponent(id)}/config`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    }).then(x => x.json());
    if (r.status) renderShows(r.status);
    if ($('shOut_' + id)) $('shOut_' + id).textContent = r.ok ? '✓ Saved & rebuilt' : `✗ ${r.error || 'Failed'}`;
    if (btn) setBtnFeedback(btn, 'saved', '✓ Saved', 1800);
  } catch {
    if ($('shOut_' + id)) $('shOut_' + id).textContent = '✗ Save failed';
  }
}

async function uploadShowSong(id, inputEl) {
  const file = inputEl && inputEl.files && inputEl.files[0];
  if (!file) return;
  if ($('shOut_' + id)) $('shOut_' + id).textContent = `Uploading & trimming ${file.name}…`;
  try {
    const fd = new FormData();
    fd.append('file', file);
    if ($('shStart_' + id)) fd.append('songStartSec', $('shStart_' + id).value || '0');
    if ($('shDur_' + id)) fd.append('songDurationSec', $('shDur_' + id).value || '15');
    const r = await fetch(`/api/shows/${encodeURIComponent(id)}/upload`, { method: 'POST', body: fd }).then(x => x.json());
    if (r.status) renderShows(r.status);
    if ($('shOut_' + id)) $('shOut_' + id).textContent = r.ok ? `✓ Swapped to ${file.name}` : `✗ ${r.error || 'Upload failed'}`;
  } catch {
    if ($('shOut_' + id)) $('shOut_' + id).textContent = '✗ Upload failed';
  }
}

async function clearShowSong(id) {
  const r = await fetch(`/api/shows/${encodeURIComponent(id)}/config`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ clearSource: true }),
  }).then(x => x.json());
  if (r.status) renderShows(r.status);
}

async function saveFxHooks(extraBody) {
  const body = {
    lightsHost: ($('fxLightsHost') && $('fxLightsHost').value.trim()) || '',
    lightsOffUrl: ($('fxLightsOffUrl') && $('fxLightsOffUrl').value.trim()) || '',
    lightsOnUrl: ($('fxLightsOnUrl') && $('fxLightsOnUrl').value.trim()) || '',
    lightsRackHost: ($('fxLightsRackHost') && $('fxLightsRackHost').value.trim()) || '',
    lightsRackOffUrl: ($('fxLightsRackOffUrl') && $('fxLightsRackOffUrl').value.trim()) || '',
    lightsRackOnUrl: ($('fxLightsRackOnUrl') && $('fxLightsRackOnUrl').value.trim()) || '',
    lightsSmithHost: ($('fxLightsSmithHost') && $('fxLightsSmithHost').value.trim()) || '',
    lightsSmithOffUrl: ($('fxLightsSmithOffUrl') && $('fxLightsSmithOffUrl').value.trim()) || '',
    lightsSmithOnUrl: ($('fxLightsSmithOnUrl') && $('fxLightsSmithOnUrl').value.trim()) || '',
    fogHost: ($('fxFogHost') && $('fxFogHost').value.trim()) || '',
    theaterHost: ($('fxTheaterHost') && $('fxTheaterHost').value.trim()) || '',
    fogTriggerUrl: ($('fxFogUrl') && $('fxFogUrl').value.trim()) || '',
    fogBurstSec: Number(($('fxFogBurst') && $('fxFogBurst').value) || 4.0),
    lightsLeadSec: Number(($('fxLightsLeadSec') && $('fxLightsLeadSec').value) || 0.0),
    ...(extraBody || {}),
  };
  const r = await fetch('/api/shows/fx-hooks', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }).then(x => x.json());
  if ($('fxOut')) {
    if (r.ok && r.hookResult && r.hookResult.skipped) $('fxOut').textContent = `⚠ Relay IP not set for ${r.tested || 'zone'}`;
    else if (r.ok && r.hookResult && r.hookResult.ok === false) $('fxOut').textContent = `✗ ${r.hookResult.error || 'Hook failed'}`;
    else $('fxOut').textContent = r.ok ? (r.tested ? `✓ Tested ${r.tested}` : '✓ FX hooks saved') : `✗ ${r.error || 'Failed'}`;
  }
  return r;
}

if ($('shStopAllBtn')) $('shStopAllBtn').onclick = () => { stopLocalRyderSim(); stopShow(); };
if ($('devSimFullBtn')) $('devSimFullBtn').onclick = () => startLocalRyderSim(null);
if ($('devSimJumpBtn')) $('devSimJumpBtn').onclick = () => startLocalRyderSim(76);
if ($('devSimStopBtn')) $('devSimStopBtn').onclick = () => { stopLocalRyderSim(); stopShow(); };
if ($('devSimFsBtn')) $('devSimFsBtn').onclick = () => { if ($('devStage')) $('devStage').classList.toggle('fs'); };
if ($('devFireLiveBtn')) $('devFireLiveBtn').onclick = async () => {
  if ($('shOut_ryder_walkin')) $('shOut_ryder_walkin').textContent = 'Firing live…';
  try {
    const r = await fetch('/api/shows/ryder_walkin/fire', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ excludeMainTv: Boolean($('devRyExcludeMainTv') && $('devRyExcludeMainTv').checked) }),
    }).then(x => x.json());
    if ($('shOut_ryder_walkin')) $('shOut_ryder_walkin').textContent = r.ok ? `🔥 Fired (${r.showName || 'Ryder'})` : `✗ ${r.error || 'Failed'}`;
    await pollStatus();
  } catch {
    if ($('shOut_ryder_walkin')) $('shOut_ryder_walkin').textContent = '✗ Fire failed';
  }
};
if ($('devRyExcludeMainTv')) $('devRyExcludeMainTv').onchange = async () => {
  const checked = Boolean($('devRyExcludeMainTv').checked);
  if (devRyderSpec) devRyderSpec.excludeMainTv = checked;
  try {
    await fetch('/api/shows/ryder_walkin/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ excludeMainTv: checked, rebuild: false }),
    });
    if ($('shOut_ryder_walkin')) $('shOut_ryder_walkin').textContent = checked ? '✓ Main TV excluded (coding mode)' : '✓ Main TV included';
  } catch {}
};

document.querySelectorAll('#devRyderBox [data-scrub]').forEach(btn => {
  btn.addEventListener('click', () => {
    const vid = $('devRyVideo');
    if (!vid) return;
    if (!vid.getAttribute('src') && devRyderSpec && devRyderSpec.showVideoReady) {
      vid.src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
    }
    try { vid.pause(); } catch {}
    const delta = Number(btn.getAttribute('data-scrub') || 0);
    const maxD = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : 9999;
    try { vid.currentTime = Math.max(0, Math.min(maxD, (vid.currentTime || 0) + delta)); } catch {}
    updateRyScrubAndFlash();
  });
});
if ($('devScrubPlayBtn')) $('devScrubPlayBtn').onclick = () => {
  const vid = $('devRyVideo');
  if (!vid) return;
  initRyderScrubber();
  if (!vid.getAttribute('src') && devRyderSpec && devRyderSpec.showVideoReady) {
    vid.src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  }
  if (vid.paused) vid.play().catch(() => {});
  else vid.pause();
};

let activeRyTextarea = null;
if ($('devRyderBox')) {
  $('devRyderBox').addEventListener('focusin', (ev) => {
    if (ev.target && ev.target.tagName === 'TEXTAREA') {
      activeRyTextarea = ev.target;
      ev.target.dataset.userFocused = '1';
    }
  });
}
if ($('devRyTagBar')) $('devRyTagBar').addEventListener('click', (ev) => {
  const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-tag],button[data-wrap]') : null;
  if (!btn) return;
  const ta = activeRyTextarea || $('devRyIntroTxt');
  applyInlineTagToTextarea(ta, btn);
});
if ($('shGrid')) {
  $('shGrid').addEventListener('focusin', (ev) => {
    if (ev.target && ev.target.tagName === 'TEXTAREA') ev.target.dataset.userFocused = '1';
  });
  $('shGrid').addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('.sh-tag-bar button[data-tag], .sh-tag-bar button[data-wrap]') : null;
    if (!btn) return;
    const bar = btn.closest('.sh-tag-bar');
    const tid = bar && bar.getAttribute('data-target');
    const ta = tid ? $(tid) : null;
    applyInlineTagToTextarea(ta, btn);
  });
}
if ($('shExpandAllBtn')) $('shExpandAllBtn').onclick = () => {
  if ($('devRyderBox')) $('devRyderBox').open = true;
  if ($('ttsKeysCard')) $('ttsKeysCard').open = true;
  if ($('fxHooksCard')) $('fxHooksCard').open = true;
  document.querySelectorAll('#shGrid details.show-acc').forEach(el => {
    el.open = true;
    const sid = el.getAttribute('data-showid');
    if (sid) openShowIds.add(sid);
  });
};
if ($('shCollapseAllBtn')) $('shCollapseAllBtn').onclick = () => {
  if ($('devRyderBox')) $('devRyderBox').open = false;
  if ($('ttsKeysCard')) $('ttsKeysCard').open = false;
  if ($('fxHooksCard')) $('fxHooksCard').open = false;
  openShowIds.clear();
  document.querySelectorAll('#shGrid details.show-acc').forEach(el => {
    el.open = false;
  });
};
if ($('devRyAddSpeechBtn')) $('devRyAddSpeechBtn').onclick = () => addRySpeechRow(null);
if ($('devRyAddFxBtn')) $('devRyAddFxBtn').onclick = () => addRyFxRow(null);

function jumpVideoToCue(target, atVal) {
  const vid = $('devRyVideo');
  if (!vid) return;
  initRyderScrubber();
  if (!vid.getAttribute('src') && devRyderSpec && devRyderSpec.showVideoReady) {
    vid.src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  }
  const atSec = parseTimeSec(atVal);
  const absT = target === 'intro' ? atSec : (getRyIntroSec() + atSec);
  const seekT = Math.max(0, absT - 2.0);
  try { vid.currentTime = seekT; } catch {}
  vid.play().catch(() => {});
}

if ($('devRySpeechList')) {
  $('devRySpeechList').addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button') : null;
    if (!btn) return;
    const row = btn.closest('.ry-sp-row');
    if (!row) return;
    if (btn.classList.contains('ry-sp-del')) {
      row.remove();
      return;
    }
    if (btn.classList.contains('ry-sp-stamp')) {
      const vid = $('devRyVideo');
      const t = (vid && Number.isFinite(vid.currentTime)) ? vid.currentTime : 0;
      const introSec = getRyIntroSec();
      if (t < introSec) {
        row.querySelector('.ry-sp-target').value = 'intro';
        row.querySelector('.ry-sp-at').value = fmtTimeSec(t);
      } else {
        row.querySelector('.ry-sp-target').value = 'video';
        row.querySelector('.ry-sp-at').value = fmtTimeSec(t - introSec);
      }
      return;
    }
    if (btn.classList.contains('ry-sp-jump')) {
      jumpVideoToCue(row.querySelector('.ry-sp-target').value, row.querySelector('.ry-sp-at').value);
      return;
    }
    if (btn.classList.contains('ry-sp-test')) {
      const tgt = row.querySelector('.ry-sp-target').value;
      const txt = row.querySelector('.ry-sp-txt').value.trim();
      const vOver = row.querySelector('.ry-sp-voice').value || '';
      auditionRyderVoice(tgt === 'intro' ? 'intro' : 'overlay', { text: txt, voice: vOver, btn, playerId: 'shOvAudio_ryder_walkin' });
    }
  });
}

if ($('devRyFxCueList')) {
  $('devRyFxCueList').addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button') : null;
    if (!btn) return;
    const row = btn.closest('.ry-fx-row');
    if (!row) return;
    if (btn.classList.contains('ry-fx-del')) {
      row.remove();
      return;
    }
    if (btn.classList.contains('ry-fx-stamp')) {
      const vid = $('devRyVideo');
      const t = (vid && Number.isFinite(vid.currentTime)) ? vid.currentTime : 0;
      const introSec = getRyIntroSec();
      if (t < introSec) {
        row.querySelector('.ry-fx-target').value = 'intro';
        row.querySelector('.ry-fx-at').value = fmtTimeSec(t);
      } else {
        row.querySelector('.ry-fx-target').value = 'video';
        row.querySelector('.ry-fx-at').value = fmtTimeSec(t - introSec);
      }
      return;
    }
    if (btn.classList.contains('ry-fx-jump')) {
      jumpVideoToCue(row.querySelector('.ry-fx-target').value, row.querySelector('.ry-fx-at').value);
    }
  });
}

const ryAuditionBlobUrls = { intro: null, overlay: null };
async function auditionRyderVoice(slot, opts) {
  const cfg = opts || {};
  const isOv = slot === 'overlay';
  const btn = cfg.btn || $(isOv ? 'devRyTestOvBtn' : 'devRyTestIntroBtn');
  const firstRow = document.querySelector('#devRySpeechList .ry-sp-row');
  const defaultTxt = isOv ? (firstRow ? firstRow.querySelector('.ry-sp-txt').value.trim() : '') : (($('devRyIntroTxt') && $('devRyIntroTxt').value.trim()) || '');
  const text = cfg.text !== undefined ? String(cfg.text).trim() : defaultTxt;
  const audioEl = $(cfg.playerId || (isOv ? 'shOvAudio_ryder_walkin' : 'shAudio_ryder_walkin'));
  const outEl = $('shOut_ryder_walkin');
  if (!text) {
    if (outEl) outEl.textContent = `✗ Enter ${isOv ? 'speech box' : 'intro'} text first`;
    return;
  }
  const withFx = !isOv && !cfg.text && Boolean($('devRyTestFxChk') && $('devRyTestFxChk').checked);
  const defaultRowVoice = isOv && firstRow ? (firstRow.querySelector('.ry-sp-voice').value || '') : '';
  const chosenVoice = (cfg.voice || defaultRowVoice || ($('devRyVoice') && $('devRyVoice').value) || '');
  if (btn) btn.disabled = true;
  if (outEl) outEl.textContent = `Auditioning ${slot} voice (no MP4 rebuild)…`;
  try {
    const resp = await fetch('/api/shows/audition-voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slot,
        text,
        voice: chosenVoice,
        voiceStyle: ($('devRyStyle') && $('devRyStyle').value.trim()) || '',
        voiceReverbWet: Number(($('devRyRev') && $('devRyRev').value) || 0.06),
        voiceBoostDb: Number(($('devRyBoost') && $('devRyBoost').value) || 2.0),
        withFx,
      }),
    });
    if (!resp.ok) {
      let errMsg = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j && j.error) errMsg = j.error;
      } catch {}
      if (outEl) outEl.textContent = `✗ ${errMsg}`;
      return;
    }
    const durHdr = resp.headers.get('X-Armory-Duration-Sec') || '?';
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const cacheKey = cfg.playerId === 'shOvAudio_ryder_walkin' ? 'overlay' : slot;
    if (ryAuditionBlobUrls[cacheKey]) {
      try { URL.revokeObjectURL(ryAuditionBlobUrls[cacheKey]); } catch {}
    }
    ryAuditionBlobUrls[cacheKey] = url;
    if (audioEl) {
      audioEl.src = url;
      audioEl.load();
      audioEl.play().catch(() => {});
    }
    if (outEl) outEl.textContent = `✓ Playing ${slot} audition (${durHdr}s — MP4 untouched)`;
  } catch (e) {
    if (outEl) outEl.textContent = `✗ Audition failed: ${(e && e.message) || 'error'}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}
if ($('devRyTestIntroBtn')) $('devRyTestIntroBtn').onclick = () => auditionRyderVoice('intro');
if ($('devRyTestOvBtn')) $('devRyTestOvBtn').onclick = () => auditionRyderVoice('overlay');

async function saveRyderConfig(rebuildFlag) {
  const body = {
    excludeMainTv: Boolean($('devRyExcludeMainTv') && $('devRyExcludeMainTv').checked),
    introStartSec: Number(($('devRyIntroStart') && $('devRyIntroStart').value) || 2.3),
    announcerVoice: $('devRyVoice').value,
    voiceReverbWet: Number($('devRyRev').value || 0.06),
    voiceBoostDb: Number($('devRyBoost').value || 2.0),
    fogBurstSec: Number($('devRyFog').value || 30),
    voiceStyle: ($('devRyStyle') && $('devRyStyle').value.trim()) || '',
    announcerText: $('devRyIntroTxt').value.trim(),
    speechCues: collectRySpeechCues(),
    fxCues: collectRyFxCues(),
    rebuild: Boolean(rebuildFlag),
  };
  $('shOut_ryder_walkin').textContent = rebuildFlag ? 'Synthesizing voices & updating Show MP4…' : 'Saving timings & FX (no MP4 rebuild)…';
  try {
    const r = await fetch('/api/shows/ryder_walkin/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    }).then(x => x.json());
    if (r.status) renderShows(r.status);
    $('shOut_ryder_walkin').textContent = r.ok ? (rebuildFlag ? '✓ Rebuilt Show MP4' : '✓ Saved timings & FX (MP4 untouched)') : `✗ ${r.error || 'Failed'}`;
    if (rebuildFlag && r.ok && $('devRyVideo')) $('devRyVideo').src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  } catch {
    $('shOut_ryder_walkin').textContent = '✗ Save failed';
  }
}
if ($('devRySaveBtn')) $('devRySaveBtn').onclick = () => saveRyderConfig(true);
if ($('devRySaveQuickBtn')) $('devRySaveQuickBtn').onclick = () => saveRyderConfig(false);
if ($('ttsSaveKeysBtn')) $('ttsSaveKeysBtn').onclick = async () => {
  const payload = {
    elevenlabsModel: ($('ttsElModel') && $('ttsElModel').value) || 'eleven_v3',
  };
  if ($('ttsElKey') && $('ttsElKey').value.trim()) payload.elevenlabsKey = $('ttsElKey').value.trim();
  if ($('ttsGemKey') && $('ttsGemKey').value.trim()) payload.geminiKey = $('ttsGemKey').value.trim();
  if ($('ttsOaiKey') && $('ttsOaiKey').value.trim()) payload.openaiKey = $('ttsOaiKey').value.trim();
  if ($('ttsKeysOut')) $('ttsKeysOut').textContent = 'Saving keys & fetching voices…';
  try {
    const r = await fetch('/api/shows/tts-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(x => x.json());
    if (r.ok) {
      if ($('ttsElKey')) $('ttsElKey').value = '';
      if ($('ttsGemKey')) $('ttsGemKey').value = '';
      if ($('ttsOaiKey')) $('ttsOaiKey').value = '';
      await loadShows();
      if ($('ttsKeysOut')) $('ttsKeysOut').textContent = '✓ Saved outside git & refreshed voices';
    } else if ($('ttsKeysOut')) {
      $('ttsKeysOut').textContent = `✗ ${r.error || 'Save failed'}`;
    }
  } catch {
    if ($('ttsKeysOut')) $('ttsKeysOut').textContent = '✗ Save failed';
  }
};
async function uploadRyderCustomVoice(slot, inputEl) {
  const f = inputEl && inputEl.files && inputEl.files[0];
  if (!f) return;
  $('shOut_ryder_walkin').textContent = `Uploading custom ${slot} voice (${f.name})…`;
  try {
    const fd = new FormData();
    fd.append('file', f);
    const r = await fetch(`/api/shows/ryder_walkin/upload-voice?slot=${encodeURIComponent(slot)}`, { method: 'POST', body: fd }).then(x => x.json());
    if (r.status) renderShows(r.status);
    $('shOut_ryder_walkin').textContent = r.ok ? `✓ Custom ${slot} voice applied` : `✗ ${r.error || 'Upload failed'}`;
    if (r.ok && $('devRyVideo')) $('devRyVideo').src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  } catch {
    $('shOut_ryder_walkin').textContent = '✗ Voice upload failed';
  }
}
if ($('devRyIntroVoiceFile')) $('devRyIntroVoiceFile').onchange = (e) => uploadRyderCustomVoice('intro', e.target);
if ($('devRyOvVoiceFile')) $('devRyOvVoiceFile').onchange = (e) => uploadRyderCustomVoice('overlay', e.target);
if ($('devRyIntroVoiceClr')) $('devRyIntroVoiceClr').onclick = async () => {
  const r = await fetch('/api/shows/ryder_walkin/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ clearCustomIntroVoice: true }) }).then(x => x.json());
  if (r.status) renderShows(r.status);
};
if ($('devRyOvVoiceClr')) $('devRyOvVoiceClr').onclick = async () => {
  const r = await fetch('/api/shows/ryder_walkin/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ clearCustomOverlayVoice: true }) }).then(x => x.json());
  if (r.status) renderShows(r.status);
};
if ($('devRyFetchVidBtn')) $('devRyFetchVidBtn').onclick = async () => {
  $('shOut_ryder_walkin').textContent = 'Downloading YouTube video & encoding 1080p MP4…';
  try {
    const r = await fetch('/api/shows/ryder_walkin/fetch-video', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(x => x.json());
    if (r.status) renderShows(r.status);
    $('shOut_ryder_walkin').textContent = r.ok ? '✓ Downloaded & built MP4' : `✗ ${r.error || 'Fetch failed'}`;
    if (r.ok && $('devRyVideo')) $('devRyVideo').src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  } catch {
    $('shOut_ryder_walkin').textContent = '✗ Fetch failed';
  }
};
if ($('devRyVidFile')) $('devRyVidFile').onchange = async (ev) => {
  const f = ev.target && ev.target.files && ev.target.files[0];
  if (!f) return;
  $('shOut_ryder_walkin').textContent = `Uploading ${f.name} & encoding show MP4…`;
  try {
    const fd = new FormData();
    fd.append('file', f);
    const r = await fetch('/api/shows/ryder_walkin/upload-video', { method: 'POST', body: fd }).then(x => x.json());
    if (r.status) renderShows(r.status);
    $('shOut_ryder_walkin').textContent = r.ok ? `✓ Built MP4 from ${f.name}` : `✗ ${r.error || 'Upload failed'}`;
    if (r.ok && $('devRyVideo')) $('devRyVideo').src = `/api/shows/ryder_walkin/show.mp4?t=${Date.now()}`;
  } catch {
    $('shOut_ryder_walkin').textContent = '✗ Video upload failed';
  }
};
if ($('fxSaveBtn')) $('fxSaveBtn').onclick = () => saveFxHooks();
if ($('fxRackOnBtn')) $('fxRackOnBtn').onclick = () => saveFxHooks({ test: 'rack_on' });
if ($('fxRackOffBtn')) $('fxRackOffBtn').onclick = () => saveFxHooks({ test: 'rack_off' });
if ($('fxSmithOnBtn')) $('fxSmithOnBtn').onclick = () => saveFxHooks({ test: 'smith_on' });
if ($('fxSmithOffBtn')) $('fxSmithOffBtn').onclick = () => saveFxHooks({ test: 'smith_off' });
if ($('fxAllOnBtn')) $('fxAllOnBtn').onclick = () => saveFxHooks({ test: 'all_on' });
if ($('fxAllOffBtn')) $('fxAllOffBtn').onclick = () => saveFxHooks({ test: 'all_off' });
if ($('fxTestFlashBtn')) $('fxTestFlashBtn').onclick = () => saveFxHooks({ test: 'lights_flash' });
if ($('fxTestAltFlashBtn')) $('fxTestAltFlashBtn').onclick = () => saveFxHooks({ test: 'lights_alt_flash' });
if ($('fxTestFogBtn')) $('fxTestFogBtn').onclick = () => saveFxHooks({ test: 'fog' });
if ($('fxTestTheaterBtn')) $('fxTestTheaterBtn').onclick = () => saveFxHooks({ test: 'theater_scene', testScene: ($('fxTheaterTestScene') && $('fxTheaterTestScene').value) || 'jenna' });
if ($('fxStopTheaterBtn')) $('fxStopTheaterBtn').onclick = () => saveFxHooks({ test: 'theater_stop' });

const PAGE_TITLES = {
  overview: 'Overview',
  cameras: 'Cameras & Calibration',
  footage: 'Footage · 14-Day Scrubber',
  shows: 'Show Lab',
  gym: 'Gym & Displays',
  audio: 'Audio & Voice',
};

function pageFromHash() {
  if (realignSlugFromHash()) return 'cameras';
  const h = String(window.location.hash || '').replace(/^#[/]?/, '').toLowerCase();
  return Object.prototype.hasOwnProperty.call(PAGE_TITLES, h) ? h : 'overview';
}

function navigatePage(pageId, pushHash = true) {
  const prevPage = activePage;
  const next = Object.prototype.hasOwnProperty.call(PAGE_TITLES, pageId) ? pageId : 'overview';
  if (prevPage === 'footage' && next !== 'footage') {
    stopTapePlayback(false);
    stopTapeScanPoll();
  }
  if (prevPage === 'audio' && next !== 'audio' && typeof _wsSchedulePoll === 'function') {
    _wsSchedulePoll(0);
  }
  activePage = next;
  document.querySelectorAll('.nav-btn[data-nav]').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.nav === next);
  });
  document.querySelectorAll('.page[data-page]').forEach(sec => {
    sec.classList.toggle('active', sec.dataset.page === next);
  });
  if ($('pageTitle')) $('pageTitle').textContent = PAGE_TITLES[next] || 'Armory Console';
  if (pushHash && window.location.hash !== '#' + next) {
    try { window.history.replaceState(null, '', '#' + next); } catch { window.location.hash = next; }
  }
  syncRawFeeds();
  if (next === 'cameras') {
    requestAnimationFrame(() => {
      drawAllCanvases();
      if (cfg && cfg.cameras) cfg.cameras.forEach((_, idx) => syncCropOverlay(idx));
    });
  } else if (next === 'footage') {
    requestAnimationFrame(() => {
      ensureScrubberTimeline();
      drawTapeTimeline();
    });
    pollClips();
  } else if (next === 'audio') {
    pollVoiceStatus();
    pollAudioStatus();
    pollVoiceAb();
    pollWakeStudio();
  } else if (next === 'gym') {
    pollAudioStatus();
  } else if (next === 'overview') {
    pollClips();
  }
}

function wireNavigation() {
  document.querySelectorAll('.nav-btn[data-nav]').forEach(btn => {
    btn.addEventListener('click', () => navigatePage(btn.dataset.nav));
  });
  window.addEventListener('hashchange', () => {
    const rs = realignSlugFromHash();
    navigatePage(pageFromHash(), false);
    if (rs) openRealignModal(rs);
  });
  window.addEventListener('resize', () => {
    if (activePage === 'footage') drawTapeTimeline();
  });
  window.addEventListener('beforeunload', () => {
    if (tsState.playing && navigator.sendBeacon) {
      try {
        navigator.sendBeacon('/api/tape/stream/stop', new Blob([JSON.stringify({ streamId: tsState.streamId })], { type: 'application/json' }));
      } catch {}
    }
  });
  document.addEventListener('keydown', (ev) => {
    if (mmState.open || raState.open) return;
    const tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const pageKeys = ['overview', 'cameras', 'footage', 'shows', 'gym', 'audio'];
    const num = Number(ev.key);
    if (num >= 1 && num <= 6 && !Number.isNaN(num)) {
      navigatePage(pageKeys[num - 1]);
      return;
    }
    if (activePage === 'footage' && tsState.timeline && tsState.timeline.segmentCount > 0) {
      if (ev.key === 'ArrowLeft') { ev.preventDefault(); stepScrubSec(ev.shiftKey ? -10 : -2); }
      else if (ev.key === 'ArrowRight') { ev.preventDefault(); stepScrubSec(ev.shiftKey ? 10 : 2); }
      else if (ev.key === ' ') { ev.preventDefault(); playTapeWindow(); }
      else if (ev.key === '[') { setScrubInOut('in'); }
      else if (ev.key === ']') { setScrubInOut('out'); }
      else if (ev.key === ',') { ev.preventDefault(); jumpActivityBurst(-1); }
      else if (ev.key === '.') { ev.preventDefault(); jumpActivityBurst(1); }
      else if (ev.key === 'm' || ev.key === 'M') { ev.preventDefault(); toggleMotionOnly(); }
      else if (ev.key === 'f' || ev.key === 'F') { ev.preventDefault(); toggleStageFullscreen(); }
    }
  });
  if ($('topStopShowBtn')) $('topStopShowBtn').onclick = () => { stopLocalRyderSim(); stopShow(); };
  if ($('ovLightsOnBtn')) $('ovLightsOnBtn').onclick = () => saveFxHooks({ test: 'all_on' }).then(() => showToast('Lights ON command sent', 'ok'));
  if ($('ovLightsOffBtn')) $('ovLightsOffBtn').onclick = () => saveFxHooks({ test: 'all_off' }).then(() => showToast('Lights OFF command sent', 'ok'));
}

function fmtDurShort(totalSec) {
  const s = Math.max(0, Math.round(Number(totalSec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${rem}s`;
  return `${rem}s`;
}

function fmtClock(ms, withSeconds = true) {
  if (!ms) return '--:--';
  const d = new Date(ms);
  return d.toLocaleTimeString([], withSeconds
    ? { hour: 'numeric', minute: '2-digit', second: '2-digit' }
    : { hour: 'numeric', minute: '2-digit' });
}

function syncOverview() {
  const st = latestStatusData;
  if (st) {
    const cams = Array.isArray(st.cameras) ? st.cameras : [];
    const physConnected = cams.filter(c => c.connected).length;
    if ($('ovKpiCamsVal')) $('ovKpiCamsVal').textContent = `${physConnected} / ${cams.length} Live`;
    if ($('ovKpiCamsSub')) $('ovKpiCamsSub').textContent = st.recording ? 'Recording workout session' : 'Standby 2s ring active';
    if ($('ovKpiCamsPill')) {
      $('ovKpiCamsPill').textContent = physConnected > 0 ? 'ONLINE' : 'CHECK';
      $('ovKpiCamsPill').className = 'pill ' + (physConnected > 0 ? 'ok' : 'warn');
    }
    if ($('navDotCams')) $('navDotCams').className = 'dot ' + (physConnected > 0 ? 'ok' : 'warn');

    if (st.disk && $('ovKpiDiskVal')) {
      $('ovKpiDiskVal').textContent = `${st.disk.freeGb} GB free`;
      $('ovKpiDiskPill').textContent = `${cfg?.retentionDays || 14}D`;
      $('ovKpiDiskPill').className = 'pill live';
    }

    const vx = st.voice || {};
    if ($('ovKpiVoiceVal')) {
      const vState = !vx.enabled ? 'Disabled' : (vx.armed ? 'Armed' : (vx.listening ? 'Listening' : (vx.ready ? 'Standby' : 'Init…')));
      $('ovKpiVoiceVal').textContent = vState;
      $('ovKpiVoiceSub').textContent = `${vx.modelSize || 'base.en'} · ${vx.sseClients || 0} client(s)`;
      $('ovKpiVoicePill').textContent = vx.listening ? 'ACTIVE' : (vx.ready ? 'READY' : 'OFF');
      $('ovKpiVoicePill').className = 'pill ' + (vx.listening ? 'ok' : (vx.ready ? 'live' : ''));
    }

    const wb = st.wallboard || {};
    const dp = (cfg && cfg.displays) || {};
    const primHost = (dp.primary && dp.primary.host) || '';
    const spinHost = (dp.spin && dp.spin.host) || '';
    if ($('ovKpiDispVal')) {
      const wbTxt = wb.adbState ? wb.adbState.toUpperCase() : 'UNSET';
      $('ovKpiDispVal').textContent = `WB ${wbTxt}`;
      $('ovKpiDispSub').textContent = `${primHost ? 'TV OK' : 'No TV'} · ${spinHost ? 'Spin OK' : 'No Spin'} · Cube ${(dp.cubeHost || '192.168.1.151')}`;
      $('ovKpiDispPill').textContent = wb.workoutActive ? 'WORKOUT' : 'READY';
      $('ovKpiDispPill').className = 'pill ' + (wb.workoutActive ? 'ok' : 'live');
    }
    if ($('navDotGym')) $('navDotGym').className = 'dot ' + (wb.workoutActive ? 'ok' : 'live');
  }

  const ha = latestAudioData || (st && st.houseAudio);
  if (ha && $('ovKpiAudioVal')) {
    const d = ha.denon || {};
    const c = ha.cube || {};
    const dTxt = d.ok ? `Denon ${d.power || 'ON'}` : 'Denon Standby';
    const cTxt = c.ok ? (c.playing ? 'Cube Playing' : 'Cube Online') : 'Cube Offline';
    $('ovKpiAudioVal').textContent = dTxt;
    $('ovKpiAudioSub').textContent = `${cTxt} · Route ${(ha.config && ha.config.outputRoute) || 'auto'}`;
    $('ovKpiAudioPill').textContent = d.power === 'ON' ? 'ON' : (c.ok ? 'READY' : 'OFF');
    $('ovKpiAudioPill').className = 'pill ' + (d.power === 'ON' ? 'ok' : (c.ok ? 'live' : ''));
    if ($('navDotAudio')) {
      const vxOn = Boolean(st && st.voice && st.voice.listening);
      $('navDotAudio').className = 'dot ' + ((d.power === 'ON' || vxOn) ? 'ok' : (c.ok ? 'live' : ''));
    }
  }

  if ($('ovKpiShowVal')) {
    const act = latestShowActive || {};
    if (act.active) {
      $('ovKpiShowVal').textContent = act.showName || act.showId || 'Playing';
      $('ovKpiShowSub').textContent = `Phase: ${act.phase || 'running'}`;
      $('ovKpiShowPill').textContent = 'LIVE';
      $('ovKpiShowPill').className = 'pill warn';
    } else {
      $('ovKpiShowVal').textContent = 'Ready';
      $('ovKpiShowSub').textContent = `${(latestShowsList || []).length || 6} shows configured`;
      $('ovKpiShowPill').textContent = 'IDLE';
      $('ovKpiShowPill').className = 'pill ok';
    }
  }

  const days = tsState.days || [];
  const totalTapes = days.reduce((acc, d) => acc + (d.tapeCount || 0), 0);
  if ($('ovKpiDiskSub') && tsState.loaded) {
    $('ovKpiDiskSub').textContent = `${totalTapes} tape(s) across ${days.length} day(s)`;
  }
  if ($('ovFootageSummary')) {
    if (!tsState.loaded) {
      $('ovFootageSummary').textContent = 'Loading tape index…';
    } else {
      const latestDay = days[0];
      const liveTxt = tsState.live ? `Live buffer: <b>${fmtDurShort(tsState.live.durationSec)}</b>` : 'Live ring idle';
      const dayTxt = latestDay
        ? `Latest day: <b>${escHtml(latestDay.label)}</b> (${latestDay.tapeCount} tape(s), ${fmtDurShort(latestDay.recordedSec || latestDay.totalDurationSec)})`
        : 'No saved sessions yet';
      $('ovFootageSummary').innerHTML = `${dayTxt} · ${liveTxt}`;
    }
  }
  if ($('ovRecentClips')) {
    const top5 = (latestClipRows || []).slice(0, 5);
    if (!top5.length) {
      $('ovRecentClips').textContent = 'No clips saved yet.';
    } else {
      $('ovRecentClips').innerHTML = top5.map(x => `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-top:1px solid var(--line)"><a href="${x.lanUrl}" target="_blank">${escHtml(x.fileName)}</a><span>${escHtml(x.camera)} · ${x.durationSec}s</span></div>`).join('');
    }
  }
}

function stopTapeScanPoll() {
  if (tsState.scanPollTimer) {
    clearTimeout(tsState.scanPollTimer);
    tsState.scanPollTimer = null;
  }
}

async function loadTapes() {
  const seq = ++tsState.tapesReqSeq;
  if (tsState.tapesAbort) {
    try { tsState.tapesAbort.abort(); } catch {}
  }
  tsState.tapesAbort = new AbortController();
  try {
    const q = new URLSearchParams({ source: tsState.sourceFilter || 'all' });
    const r = await fetch(`/api/tapes?${q.toString()}`, {
      cache: 'no-store',
      signal: tsState.tapesAbort.signal,
    }).then(x => x.json());
    if (seq !== tsState.tapesReqSeq) return;
    tsState.loaded = true;
    tsState.days = Array.isArray(r.days) ? r.days : [];
    tsState.live = r.live || null;
    if (!tsState.selectedDayKey) {
      tsState.selectedDayKey = tsState.days.length ? tsState.days[0].date : (tsState.live ? 'live' : '');
    } else if (tsState.selectedDayKey !== 'live' && !tsState.days.some(d => d.date === tsState.selectedDayKey)) {
      tsState.selectedDayKey = tsState.days.length ? tsState.days[0].date : (tsState.live ? 'live' : '');
    }
    renderTapeDayStrip();
    if (activePage === 'footage') {
      ensureScrubberTimeline();
    }
    syncOverview();
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    if (seq === tsState.tapesReqSeq && $('tsStatusPill')) {
      $('tsStatusPill').textContent = 'ERR';
      $('tsStatusPill').className = 'pill err';
    }
  }
}

function ensureScrubberTimeline() {
  if (!tsState.loaded) return;
  if (!tsState.timeline && tsState.selectedDayKey) {
    selectTapeDay(tsState.selectedDayKey, true);
  } else {
    drawTapeTimeline();
  }
}

function renderTapeDayStrip() {
  const strip = $('tsDayStrip');
  if (!strip) return;
  const pills = [];
  if (tsState.live) {
    const on = tsState.selectedDayKey === 'live' ? ' on' : '';
    pills.push(`<button class="ts-pill${on}" type="button" data-tsday="live"><span class="dot live"></span>Live Buffer · ${fmtDurShort(tsState.live.durationSec)}</button>`);
  }
  tsState.days.forEach(d => {
    const on = tsState.selectedDayKey === d.date ? ' on' : '';
    const hasActive = (d.tapes || []).some(t => t.isActive);
    const dot = hasActive ? '<span class="dot ok"></span>' : '';
    const recSec = d.recordedSec != null ? d.recordedSec : d.totalDurationSec;
    const wNames = Array.isArray(d.workoutNames) && d.workoutNames.length ? d.workoutNames.slice(0, 2).join(', ') : '';
    const wTag = wNames ? `<span class="ts-pill-tag w">${escHtml(wNames)}</span>` : '';
    const mTag = (d.motionSec > 0) ? `<span class="ts-pill-tag m">24/7 ${fmtDurShort(d.motionSec)}</span>` : '';
    pills.push(`<button class="ts-pill${on}" type="button" data-tsday="${escHtml(d.date)}">${dot}<span>${escHtml(d.label)}</span>${wTag}<span style="color:var(--mut)">${fmtDurShort(recSec)}</span>${mTag}</button>`);
  });
  if (!pills.length) {
    const hint = tsState.sourceFilter === 'motion'
      ? 'No 24/7 motion archives yet — movement outside workouts records automatically when enabled.'
      : 'No rolling tapes yet.';
    strip.innerHTML = `<span style="color:var(--mut);font-size:12px">${hint}</span>`;
  } else {
    strip.innerHTML = pills.join('');
  }
  strip.querySelectorAll('[data-tsday]').forEach(b => {
    b.onclick = () => selectTapeDay(b.dataset.tsday, true);
  });
}

function unionCamerasForTapes(tapes) {
  const seen = [];
  (tapes || []).forEach(t => {
    (t.cameras || []).forEach(c => {
      if (c && !seen.includes(c)) seen.push(c);
    });
  });
  return seen.length ? seen : ['Rack Cam', 'Smith Cam'];
}

function selectTapeDay(dayKey, forceReload = false) {
  tsState.selectedDayKey = dayKey;
  renderTapeDayStrip();
  if (dayKey === 'live' && tsState.live) {
    if ($('tsSessionRow')) $('tsSessionRow').hidden = true;
    selectTapeSession('', tsState.live.cameras || [], true, forceReload);
    return;
  }
  const day = tsState.days.find(d => d.date === dayKey);
  if (!day || !(day.tapes || []).length) {
    if ($('tsSessionRow')) $('tsSessionRow').hidden = true;
    tsState.timeline = null;
    tsState.events = [];
    stopTapePlayback(false);
    renderTapeEvents();
    drawTapeTimeline();
    if ($('tsStatusPill')) { $('tsStatusPill').textContent = 'EMPTY'; $('tsStatusPill').className = 'pill warn'; }
    return;
  }
  const tapes = day.tapes;
  const wholeId = `__day__:${day.date}`;
  const dayCams = unionCamerasForTapes(tapes);
  const sessRow = $('tsSessionRow');
  const sessStrip = $('tsSessionStrip');
  const validIds = new Set([wholeId, ...tapes.map(t => t.sessionId)]);
  let chosenSid = (!tsState.isLive && validIds.has(tsState.sessionId)) ? tsState.sessionId : (tapes.length > 1 ? wholeId : tapes[0].sessionId);

  if (sessRow && sessStrip) {
    if (tapes.length > 1) {
      sessRow.hidden = false;
      const wholeOn = (!tsState.isLive && chosenSid === wholeId) ? ' on' : '';
      const recSec = day.recordedSec != null ? day.recordedSec : day.totalDurationSec;
      const parts = [
        `<button class="ts-pill${wholeOn}" type="button" data-tssess="${escHtml(wholeId)}"><span>Whole Day</span><span style="color:var(--mut)">${fmtDurShort(recSec)}</span></button>`,
      ];
      tapes.forEach(t => {
        const on = (!tsState.isLive && t.sessionId === chosenSid) ? ' on' : '';
        const dot = t.isActive ? '<span class="dot ok"></span>' : '';
        const kTag = t.kind === 'motion'
          ? '<span class="ts-pill-tag m">24/7</span>'
          : '<span class="ts-pill-tag w">Workout</span>';
        const title = (t.name && t.name !== t.sessionId) ? t.name : fmtClock(t.startMs, false);
        const tDur = t.recordedSec != null ? t.recordedSec : t.durationSec;
        parts.push(`<button class="ts-pill${on}" type="button" data-tssess="${escHtml(t.sessionId)}">${dot}${kTag}<span>${escHtml(title)}</span><span style="color:var(--mut)">${fmtDurShort(tDur)}</span></button>`);
      });
      sessStrip.innerHTML = parts.join('');
      sessStrip.querySelectorAll('[data-tssess]').forEach(b => {
        b.onclick = () => {
          const sid = b.dataset.tssess;
          const hit = tapes.find(x => x.sessionId === sid);
          const cams = sid === wholeId ? dayCams : ((hit && hit.cameras) || dayCams);
          sessStrip.querySelectorAll('[data-tssess]').forEach(x => x.classList.toggle('on', x.dataset.tssess === sid));
          selectTapeSession(sid, cams, false, true);
        };
      });
    } else {
      sessRow.hidden = true;
      chosenSid = tapes[0].sessionId;
    }
  }
  const hit = tapes.find(t => t.sessionId === chosenSid);
  const cams = chosenSid === wholeId ? dayCams : ((hit && hit.cameras) || dayCams);
  selectTapeSession(chosenSid, cams, false, forceReload);
}

function selectTapeSession(sessionId, cameras, isLive, resetRange = true) {
  const changed = tsState.sessionId !== sessionId || tsState.isLive !== isLive || resetRange;
  if (changed) {
    stopTapePlayback(false);
    stopTapeScanPoll();
    tsState.packCache.clear();
    tsState.activePack = [];
    tsState.zoomMode = 'fit';
    tsState.viewStartFrac = 0;
    tsState.viewEndFrac = 1;
    tsState.inMs = null;
    tsState.outMs = null;
    tsState.curMs = 0;
    syncZoomButtons();
  }
  tsState.sessionId = sessionId;
  tsState.isLive = Boolean(isLive);
  tsState.cameras = (cameras && cameras.length) ? cameras : ['Rack Cam', 'Smith Cam'];
  if (!tsState.cameras.includes(tsState.camera)) {
    tsState.camera = tsState.cameras[0];
  }
  renderTapeCamStrip();
  updateRangeInfo();
  loadTapeTimeline(!changed);
}

function renderTapeCamStrip() {
  const strip = $('tsCamStrip');
  if (!strip) return;
  const tl = tsState.timeline;
  const actMap = (tl && tl.cameraActivity) ? tl.cameraActivity : {};
  strip.innerHTML = tsState.cameras.map(cam => {
    const on = cam === tsState.camera ? ' on' : '';
    const stat = actMap[cam];
    let badge = '';
    if (stat && stat.activeSegs > 0) {
      badge = `<span class="ts-pill-tag w">${stat.activeSegs}</span>`;
    } else if (stat) {
      badge = `<span style="font-size:10px;color:#64748b">quiet</span>`;
    }
    return `<button class="ts-pill${on}" type="button" data-tscam="${escHtml(cam)}">${escHtml(cam)}${badge}</button>`;
  }).join('');
  strip.querySelectorAll('[data-tscam]').forEach(b => {
    b.onclick = () => selectTapeCamera(b.dataset.tscam);
  });
}

function selectTapeCamera(camName) {
  if (!camName || tsState.camera === camName) return;
  const wasPlaying = tsState.playing;
  const keepMs = tsState.curMs;
  stopTapePlayback(false);
  tsState.camera = camName;
  renderTapeCamStrip();
  loadTapeTimeline(true, { resumePlay: wasPlaying, targetMs: keepMs, userPickedCam: true });
}

function rebuildTrackMap() {
  const tl = tsState.timeline;
  tsState.trackSegs = [];
  if (!tl || !tl.segmentCount) return;
  const minMs = Number(tl.minMs || 0);
  const maxMs = Math.max(minMs + 2000, Number(tl.maxMs || 0));
  const spanMs = Math.max(1000, maxMs - minMs);
  const runs = (Array.isArray(tl.runs) && tl.runs.length)
    ? tl.runs.map((r, i) => ({
        idx: i,
        startMs: Number(r.startMs),
        endMs: Math.max(Number(r.startMs) + 1000, Number(r.endMs)),
        durationSec: Number(r.durationSec || Math.round((Number(r.endMs) - Number(r.startMs)) / 1000)),
        kind: r.kind || 'workout',
      }))
    : [{ idx: 0, startMs: minMs, endMs: maxMs, durationSec: Math.round(spanMs / 1000), kind: 'workout' }];

  const recordedMs = runs.reduce((acc, r) => acc + Math.max(1000, r.endMs - r.startMs), 0);
  const viewKey = `${tsState.selectedDayKey}|${tsState.sessionId}|${tsState.sourceFilter}`;
  if (tsState.manualGapModeKey !== viewKey) {
    tsState.collapseGaps = (runs.length > 1 && recordedMs / spanMs < 0.85);
  }
  if (runs.length <= 1) {
    tsState.collapseGaps = false;
  }
  syncGapModeButtons(runs.length > 1);

  if (!tsState.collapseGaps || runs.length <= 1) {
    tsState.trackSegs = [{ kind: 'run', idx: 0, startMs: minMs, endMs: maxMs, f0: 0, f1: 1 }];
  } else {
    const seamFrac = Math.min(0.015, 0.16 / (runs.length - 1));
    const usable = Math.max(0.5, 1 - seamFrac * (runs.length - 1));
    const totalRunMs = Math.max(1000, recordedMs);
    let f = 0;
    for (let i = 0; i < runs.length; i++) {
      const r = runs[i];
      const rSpan = Math.max(1000, r.endMs - r.startMs);
      const rW = (rSpan / totalRunMs) * usable;
      const f1 = (i === runs.length - 1) ? 1 : (f + rW);
      tsState.trackSegs.push({ kind: 'run', idx: i, startMs: r.startMs, endMs: r.endMs, f0: f, f1 });
      f = f1;
      if (i < runs.length - 1) {
        const gf1 = f + seamFrac;
        tsState.trackSegs.push({ kind: 'gap', idx: i, startMs: r.endMs, endMs: runs[i + 1].startMs, f0: f, f1: gf1 });
        f = gf1;
      }
    }
  }
  renderRunStrip(runs);
}

function renderRunStrip(runs) {
  const strip = $('tsRunStrip');
  if (!strip) return;
  if (!runs || runs.length <= 1) {
    strip.innerHTML = '';
    return;
  }
  const shown = runs.slice(0, 7);
  strip.innerHTML = shown.map((r, i) => {
    const active = tsState.curMs >= r.startMs - 1500 && tsState.curMs <= r.endMs + 1500 ? ' on' : '';
    return `<button class="ts-run-btn${active}" type="button" data-tsrun="${i}" title="${fmtClock(r.startMs)} – ${fmtClock(r.endMs)}">${fmtClock(r.startMs, false)} (${fmtDurShort(r.durationSec)})</button>`;
  }).join('') + (runs.length > 7 ? `<span style="font-size:10.5px;color:var(--mut)">+${runs.length - 7}</span>` : '');
  strip.querySelectorAll('[data-tsrun]').forEach(btn => {
    btn.onclick = () => {
      const r = runs[Number(btn.dataset.tsrun)];
      if (!r) return;
      if (tsState.playing) startTapePlayback(r.startMs);
      else scrubToMs(r.startMs, true);
    };
  });
}

function msToTrackFrac(ms) {
  const segs = tsState.trackSegs;
  if (!segs || !segs.length) return 0;
  if (ms <= segs[0].startMs) return 0;
  const last = segs[segs.length - 1];
  if (ms >= last.endMs) return 1;
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (ms <= s.endMs || i === segs.length - 1) {
      const span = Math.max(1, s.endMs - s.startMs);
      const t = clamp((ms - s.startMs) / span, 0, 1);
      return s.f0 + t * (s.f1 - s.f0);
    }
  }
  return 1;
}

function trackFracToMs(frac, snapGap = false) {
  const segs = tsState.trackSegs;
  const tl = tsState.timeline;
  if (!segs || !segs.length || !tl) return tl ? Number(tl.minMs || 0) : 0;
  const f = clamp(frac, 0, 1);
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (f <= s.f1 || i === segs.length - 1) {
      if (snapGap && s.kind === 'gap' && tsState.collapseGaps) {
        return segs[Math.min(segs.length - 1, i + 1)].startMs;
      }
      const fSpan = Math.max(1e-6, s.f1 - s.f0);
      const t = clamp((f - s.f0) / fSpan, 0, 1);
      return Math.round(s.startMs + t * (s.endMs - s.startMs));
    }
  }
  return Number(tl.maxMs || 0);
}

function findRunAt(ms) {
  const tl = tsState.timeline;
  const runs = (tl && Array.isArray(tl.runs) && tl.runs.length)
    ? tl.runs
    : (tl && tl.segmentCount ? [{ startMs: tl.minMs, endMs: tl.maxMs }] : []);
  for (let i = 0; i < runs.length; i++) {
    if (ms >= runs[i].startMs - 1500 && ms <= runs[i].endMs + 1500) {
      return { run: runs[i], index: i, runs };
    }
  }
  return { run: null, index: -1, runs };
}

function findGapAt(ms) {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return null;
  const runs = (Array.isArray(tl.runs) && tl.runs.length) ? tl.runs : [{ startMs: tl.minMs, endMs: tl.maxMs }];
  const info = findRunAt(ms);
  if (info.run) return null;
  const nextRun = runs.find(r => r.startMs > ms) || null;
  let prevRun = null;
  for (let i = runs.length - 1; i >= 0; i--) {
    if (runs[i].endMs < ms) { prevRun = runs[i]; break; }
  }
  return {
    startMs: prevRun ? prevRun.endMs : tl.minMs,
    endMs: nextRun ? nextRun.startMs : tl.maxMs,
    prevRun,
    nextRun,
    fallbackRun: nextRun || runs[0],
  };
}

function syncGapModeButtons(hasGaps) {
  if ($('tsGapCollapseBtn')) {
    $('tsGapCollapseBtn').classList.toggle('on', Boolean(tsState.collapseGaps));
    $('tsGapCollapseBtn').disabled = !hasGaps;
    $('tsGapCollapseBtn').title = hasGaps ? 'Hide empty multi-hour gaps on timeline' : 'Single continuous run (no gaps)';
  }
  if ($('tsGapRealBtn')) {
    $('tsGapRealBtn').classList.toggle('on', !tsState.collapseGaps);
  }
}

function syncZoomButtons() {
  document.querySelectorAll('[data-tszoom]').forEach(b => {
    b.classList.toggle('on', b.dataset.tszoom === tsState.zoomMode);
  });
  const zoomed = (tsState.viewEndFrac - tsState.viewStartFrac) < 0.995;
  if ($('tsPanLeftBtn')) $('tsPanLeftBtn').hidden = !zoomed;
  if ($('tsPanRightBtn')) $('tsPanRightBtn').hidden = !zoomed;
}

function setGapMode(mode) {
  const tl = tsState.timeline;
  if (!tl) return;
  const viewKey = `${tsState.selectedDayKey}|${tsState.sessionId}|${tsState.sourceFilter}`;
  tsState.manualGapModeKey = viewKey;
  tsState.collapseGaps = (mode === 'collapse');
  rebuildTrackMap();
  setZoomMode(tsState.zoomMode);
  drawTapeTimeline();
}

function setZoomMode(mode) {
  tsState.zoomMode = String(mode || 'fit');
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount || tsState.zoomMode === 'fit') {
    tsState.zoomMode = 'fit';
    tsState.viewStartFrac = 0;
    tsState.viewEndFrac = 1;
  } else {
    const zoomMs = Math.max(15_000, Number(tsState.zoomMode) * 1000);
    const effMs = tsState.collapseGaps
      ? Math.max(1000, Number(tl.recordedSec || 0) * 1000 || (tl.maxMs - tl.minMs))
      : Math.max(1000, Number(tl.maxMs || 0) - Number(tl.minMs || 0));
    const spanFrac = clamp(zoomMs / effMs, 0.012, 1);
    if (spanFrac >= 0.99) {
      tsState.zoomMode = 'fit';
      tsState.viewStartFrac = 0;
      tsState.viewEndFrac = 1;
    } else {
      const centerFrac = msToTrackFrac(tsState.curMs || tl.minMs);
      let s = centerFrac - spanFrac * 0.35;
      let e = s + spanFrac;
      if (s < 0) { s = 0; e = spanFrac; }
      if (e > 1) { e = 1; s = Math.max(0, 1 - spanFrac); }
      tsState.viewStartFrac = s;
      tsState.viewEndFrac = e;
    }
  }
  syncZoomButtons();
  drawTapeTimeline();
  preloadViewportPack();
}

function panViewport(dir) {
  const span = tsState.viewEndFrac - tsState.viewStartFrac;
  if (span >= 0.995) return;
  const shift = span * 0.45 * (dir > 0 ? 1 : -1);
  let s = clamp(tsState.viewStartFrac + shift, 0, 1 - span);
  tsState.viewStartFrac = s;
  tsState.viewEndFrac = s + span;
  drawTapeTimeline();
  preloadViewportPack();
}

function zoomAtCanvasFrac(cursorFracOnTrack, zoomIn) {
  const curSpan = tsState.viewEndFrac - tsState.viewStartFrac;
  const nextSpan = clamp(curSpan * (zoomIn ? 0.65 : 1.45), 0.012, 1);
  if (nextSpan >= 0.99) {
    setZoomMode('fit');
    return;
  }
  const rel = clamp((cursorFracOnTrack - tsState.viewStartFrac) / Math.max(1e-6, curSpan), 0, 1);
  let s = cursorFracOnTrack - rel * nextSpan;
  let e = s + nextSpan;
  if (s < 0) { s = 0; e = nextSpan; }
  if (e > 1) { e = 1; s = Math.max(0, 1 - nextSpan); }
  tsState.zoomMode = 'custom';
  tsState.viewStartFrac = s;
  tsState.viewEndFrac = e;
  syncZoomButtons();
  drawTapeTimeline();
  if (tsState.packTimer) clearTimeout(tsState.packTimer);
  tsState.packTimer = setTimeout(preloadViewportPack, 220);
}

async function loadTapeTimeline(preservePlayhead = false, opts = {}) {
  if (!opts.silent && $('tsStatusPill')) {
    $('tsStatusPill').textContent = 'LOADING…';
    $('tsStatusPill').className = 'pill live';
  }
  try {
    const q = new URLSearchParams({
      sessionId: tsState.sessionId || '',
      camera: tsState.camera || 'Rack Cam',
      source: tsState.sourceFilter || 'all',
      buckets: '240',
    });
    const tl = await fetch(`/api/tape/timeline?${q.toString()}`, { cache: 'no-store' }).then(x => x.json());
    tsState.timeline = tl;
    const nSegs = tl.segmentCount || 0;
    const recSec = Number(tl.recordedSec || 0);
    reclusterEventsOnly(false);

    // If auto-opened on a quiet camera while another camera has recorded motion, switch straight to the active angle
    if (!opts.userPickedCam && !opts.scanRefresh && !opts.silent && !tsState.isLive && nSegs > 0 && tsState.events.length === 0) {
      const actMap = tl.cameraActivity || {};
      let bestCam = '';
      let bestScore = 0;
      (tsState.cameras || []).forEach(cName => {
        if (cName === tsState.camera) return;
        const st = actMap[cName];
        const segs = st ? Number(st.activeSegs || 0) : 0;
        if (segs > bestScore) {
          bestScore = segs;
          bestCam = cName;
        }
      });
      if (bestCam && bestScore > 0) {
        tsState.camera = bestCam;
        renderTapeCamStrip();
        return loadTapeTimeline(false, { ...opts, userPickedCam: true });
      }
    }

    if ($('tsStatusPill')) {
      $('tsStatusPill').textContent = nSegs ? `${fmtDurShort(recSec)} · ${tsState.events.length} events` : 'EMPTY';
      $('tsStatusPill').className = 'pill ' + (nSegs ? 'ok' : 'warn');
    }
    if ($('tsScanPill')) {
      if (tl.scanStatus === 'scanning') {
        $('tsScanPill').hidden = false;
        $('tsScanPill').textContent = `Scanning v2 · ${tl.scanPct || 0}%`;
        $('tsScanPill').className = 'pill live';
      } else {
        $('tsScanPill').hidden = true;
      }
    }
    if ($('tsAudioBtn')) {
      const hasAud = Boolean(tl.hasAudio);
      $('tsAudioBtn').disabled = !hasAud;
      if (!hasAud && tsState.audioOn) {
        tsState.audioOn = false;
        $('tsAudioBtn').textContent = '🔇 No Audio';
      } else if (!hasAud) {
        $('tsAudioBtn').textContent = '🔇 No Audio';
      } else {
        $('tsAudioBtn').textContent = tsState.audioOn ? '🔊 Audio ON' : '🔇 Audio';
        $('tsAudioBtn').classList.toggle('on', tsState.audioOn);
      }
    }
    renderTapeCamStrip();
    rebuildTrackMap();
    renderTapeEvents();

    if ($('tsStartLbl')) $('tsStartLbl').textContent = fmtClock(tl.minMs);
    if ($('tsEndLbl')) $('tsEndLbl').textContent = fmtClock(tl.maxMs);

    if (opts.scanRefresh) {
      drawTapeTimeline();
      if (tl.scanStatus === 'scanning' && activePage === 'footage') scheduleTapeScanPoll();
      return;
    }

    if (nSegs > 0) {
      let targetMs = (opts.targetMs != null) ? Number(opts.targetMs) : tsState.curMs;
      if (!preservePlayhead || targetMs < tl.minMs || targetMs > tl.maxMs) {
        if (!tsState.isLive && tsState.events.length) {
          targetMs = Math.max(tl.minMs, tsState.events[0].startMs - 1500);
        } else if (!tsState.isLive && Array.isArray(tl.runs) && tl.runs.length) {
          targetMs = tl.runs[0].startMs;
        } else {
          targetMs = Math.max(tl.minMs, tl.maxMs - 10000);
        }
      }
      if (findGapAt(targetMs)) {
        const g = findGapAt(targetMs);
        if (g && g.fallbackRun) targetMs = g.fallbackRun.startMs;
      }
      if (tsState.packTimer) clearTimeout(tsState.packTimer);
      if (opts.resumePlay) {
        startTapePlayback(targetMs);
      } else {
        scrubToMs(targetMs, true);
        tsState.packTimer = setTimeout(() => { if (!tsState.playing) preloadViewportPack(); }, 350);
      }
      if (tl.scanStatus === 'scanning' && activePage === 'footage') scheduleTapeScanPoll();
    } else {
      hideGapOverlay();
      drawTapeTimeline();
    }
  } catch {
    if ($('tsStatusPill')) { $('tsStatusPill').textContent = 'ERR'; $('tsStatusPill').className = 'pill err'; }
  }
}

function scheduleTapeScanPoll() {
  stopTapeScanPoll();
  tsState.scanPollTimer = setTimeout(() => {
    if (activePage !== 'footage') return;
    loadTapeTimeline(true, { silent: true, scanRefresh: true });
  }, 2400);
}

function clusterSampleRows(rows, isAllCams, minDurOverrideSec = null) {
  const tl = tsState.timeline;
  if (!tl || !Array.isArray(rows) || !rows.length) return [];
  const sens = Number(tsState.sensitivity || 1.5);
  const minDurMs = Math.max(1000, Number(minDurOverrideSec != null ? minDurOverrideSec : (tsState.minDurSec || 5)) * 1000);
  const mergeGapMs = 4000;
  const camNames = Array.isArray(tl.cameraNames) ? tl.cameraNames : [];
  const raw = [];
  let cur = null;

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const s0 = Number(row[0]);
    const dur = Math.max(1000, Number(row[1] || 2000));
    const s1 = s0 + dur;
    const e = Number(row[2] || 0);
    if (e < sens) continue;
    const camName = isAllCams ? (camNames[Number(row[3])] || tsState.camera) : tsState.camera;
    const kind = isAllCams ? (row[4] || 'w') : (row[3] || 'w');
    const mid = Math.round((s0 + s1) / 2);

    if (cur && (s0 - cur.endMs) <= mergeGapMs && (!isAllCams || cur.camera === camName)) {
      if (s1 > cur.endMs) cur.endMs = s1;
      cur.segCount += 1;
      if (e > cur.peakEnergy) {
        cur.peakEnergy = e;
        cur.peakMs = mid;
      }
      if (kind === 'm' && cur.kind !== 'w') cur.kind = 'm';
    } else {
      if (cur) raw.push(cur);
      cur = {
        startMs: s0,
        endMs: s1,
        peakMs: mid,
        peakEnergy: e,
        segCount: 1,
        camera: camName,
        kind: kind === 'm' ? 'motion' : 'workout',
      };
    }
  }
  if (cur) raw.push(cur);

  return raw
    .filter(ev => (ev.endMs - ev.startMs) >= minDurMs)
    .map(ev => ({
      ...ev,
      durationSec: Math.round((ev.endMs - ev.startMs) / 1000),
      peakEnergy: Math.round(ev.peakEnergy * 10) / 10,
    }));
}

function reclusterEventsOnly(redraw = true) {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) {
    tsState.events = [];
    if (redraw) {
      renderTapeEvents();
      drawTapeTimeline();
    }
    return;
  }
  const rows = tsState.allCamsEvents && Array.isArray(tl.allCamSamples) && tl.allCamSamples.length
    ? tl.allCamSamples
    : (Array.isArray(tl.samples) ? tl.samples : []);
  tsState.events = clusterSampleRows(rows, tsState.allCamsEvents && rows === tl.allCamSamples);
  if ($('tsEvCountPill')) $('tsEvCountPill').textContent = String(tsState.events.length);
  if ($('tsStatusPill') && tl.scanStatus !== 'scanning') {
    $('tsStatusPill').textContent = `${fmtDurShort(Number(tl.recordedSec || 0))} · ${tsState.events.length} events`;
  }
  if (redraw) {
    renderTapeEvents();
    drawTapeTimeline();
  }
}

function renderTapeEvents() {
  const box = $('tsBurstList');
  if (!box) return;
  if (tsState.evObserver) {
    try { tsState.evObserver.disconnect(); } catch {}
  }
  const tl = tsState.timeline;
  const evs = tsState.events || [];
  if ($('tsEvCountPill')) $('tsEvCountPill').textContent = String(evs.length);
  if (!tl || !tl.segmentCount) {
    box.innerHTML = '<span style="font-size:12px;color:var(--mut)">Select a day/session above to inspect motion events.</span>';
    return;
  }
  if (!evs.length) {
    const rows = tsState.allCamsEvents && Array.isArray(tl.allCamSamples) && tl.allCamSamples.length
      ? tl.allCamSamples
      : (Array.isArray(tl.samples) ? tl.samples : []);
    const brief = tsState.minDurSec > 2 ? clusterSampleRows(rows, tsState.allCamsEvents && rows === tl.allCamSamples, 2) : [];
    const briefBtn = brief.length
      ? `<div style="margin-top:8px"><button class="btn sec sm" type="button" id="tsShowBriefBtn">Show ${brief.length} brief (2–4s) event(s)</button></div>`
      : '';
    box.innerHTML = `<div style="font-size:12px;color:var(--mut);padding:6px">No events ≥${tsState.minDurSec}s at sensitivity ${Number(tsState.sensitivity).toFixed(1)}.${briefBtn}</div>`;
    if ($('tsShowBriefBtn')) {
      $('tsShowBriefBtn').onclick = () => {
        tsState.minDurSec = 2;
        document.querySelectorAll('[data-tsmindur]').forEach(b => b.classList.toggle('on', b.dataset.tsmindur === '2'));
        reclusterEventsOnly(true);
      };
    }
    return;
  }

  box.innerHTML = evs.map((b, i) => {
    const active = tsState.curMs >= b.startMs - 1500 && tsState.curMs <= b.endMs + 1500 ? ' on' : '';
    const pct = clamp(Math.round((Math.log1p(b.peakEnergy) / Math.log1p(20)) * 100), 10, 100);
    const barColor = b.kind === 'motion' ? '#a855f7' : (b.peakEnergy >= tsState.sensitivity * 2 ? '#38bdf8' : '#22c55e');
    const srcTag = b.kind === 'motion'
      ? '<span class="ts-pill-tag m">24/7</span>'
      : '<span class="ts-pill-tag w">Workout</span>';
    const camTag = tsState.allCamsEvents ? `<span class="ts-pill-tag" style="background:#1b2838;color:#93c5fd">${escHtml(b.camera)}</span>` : '';
    const tq = new URLSearchParams({
      sessionId: tsState.sessionId || '',
      camera: b.camera || tsState.camera || 'Rack Cam',
      source: tsState.sourceFilter || 'all',
      tsMs: String(b.peakMs || b.startMs),
      w: '280',
      maxGapMs: '15000',
    });
    const thumbUrl = `/api/tape/frame?${tq.toString()}`;
    return `<div class="ts-ev-card${active}" role="button" tabindex="0" data-tsburst="${i}">
      <img class="ts-ev-thumb" data-src="${thumbUrl}" alt="">
      <div class="ts-ev-meta">
        <div class="ts-ev-top">
          <span>${fmtClock(b.startMs)}</span>
          <span style="color:#86efac">${b.durationSec}s</span>
        </div>
        <div class="ts-ev-sub">${srcTag}${camTag}<span>peak ${b.peakEnergy.toFixed(1)}</span></div>
        <div class="ts-ev-bar"><span style="width:${pct}%;background:${barColor}"></span></div>
        <div class="ts-ev-foot">
          <span style="font-size:10px;color:var(--mut)">Click to play</span>
          <button class="ts-ev-clip" type="button" data-evclip="${i}">Clip [ ]</button>
        </div>
      </div>
    </div>`;
  }).join('');

  if (typeof IntersectionObserver !== 'undefined') {
    tsState.evObserver = new IntersectionObserver((entries) => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const img = en.target;
        const ds = img.getAttribute('data-src');
        if (ds) {
          img.src = ds;
          img.removeAttribute('data-src');
        }
        tsState.evObserver.unobserve(img);
      });
    }, { root: box, rootMargin: '120px' });
    box.querySelectorAll('img[data-src]').forEach(img => tsState.evObserver.observe(img));
  } else {
    box.querySelectorAll('img[data-src]').forEach(img => { img.src = img.getAttribute('data-src'); });
  }

  box.querySelectorAll('.ts-ev-card[data-tsburst]').forEach(card => {
    card.onclick = (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest('[data-evclip]')) return;
      const b = evs[Number(card.dataset.tsburst)];
      if (!b) return;
      const startAt = Math.max(tl.minMs, b.startMs - 1500);
      if (b.camera && b.camera !== tsState.camera && tsState.cameras.includes(b.camera)) {
        stopTapePlayback(false);
        tsState.camera = b.camera;
        renderTapeCamStrip();
        loadTapeTimeline(true, { resumePlay: true, targetMs: startAt });
      } else {
        startTapePlayback(startAt);
      }
    };
  });
  box.querySelectorAll('[data-evclip]').forEach(btn => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const b = evs[Number(btn.dataset.evclip)];
      if (!b) return;
      tsState.inMs = Math.max(tl.minMs, b.startMs - 2000);
      tsState.outMs = Math.min(tl.maxMs, b.endMs + 2000);
      updateRangeInfo();
      drawTapeTimeline();
      showToast(`Range set: ${fmtClock(tsState.inMs)} → ${fmtClock(tsState.outMs)}`, 'ok');
    };
  });
}

function sampleEnergyAt(ms) {
  const tl = tsState.timeline;
  if (!tl) return 0;
  const rows = tl.samples;
  if (Array.isArray(rows) && rows.length) {
    let best = 0;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (ms >= r[0] && ms <= r[0] + r[1]) {
        if (r[2] > best) best = Number(r[2] || 0);
      }
    }
    if (best > 0) return best;
  }
  if (Array.isArray(tl.activity) && tl.activity.length) {
    const span = Math.max(1, tl.maxMs - tl.minMs);
    const idx = clamp(Math.floor(((ms - tl.minMs) / span) * tl.activity.length), 0, tl.activity.length - 1);
    return Number(tl.activity[idx].e || 0);
  }
  return 0;
}

function drawTapeTimeline() {
  const canvas = $('tsTimelineCanvas');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 40) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(rect.width * dpr);
  const h = Math.round(86 * dpr);
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#060a10';
  ctx.fillRect(0, 0, w, h);

  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) {
    ctx.fillStyle = '#64748b';
    ctx.font = `${12 * dpr}px system-ui,sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillText('No video segments in selected view', w / 2, h / 2 + 4 * dpr);
    return;
  }

  const v0 = tsState.viewStartFrac;
  const vSpan = Math.max(1e-6, tsState.viewEndFrac - tsState.viewStartFrac);
  const fracToX = (f) => ((f - v0) / vSpan) * w;

  const barTop = 10 * dpr;
  const rulerH = 18 * dpr;
  const laneH = h - barTop - rulerH;
  const rulerY = h - rulerH;

  // 1. Draw runs and gaps on track background
  const segs = tsState.trackSegs || [];
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (s.f1 < v0 || s.f0 > tsState.viewEndFrac) continue;
    const x0 = clamp(fracToX(s.f0), 0, w);
    const x1 = clamp(fracToX(s.f1), 0, w);
    const sw = Math.max(1, x1 - x0);
    if (s.kind === 'run') {
      ctx.fillStyle = '#0f1826';
      ctx.fillRect(x0, barTop, sw, laneH);
      ctx.fillStyle = '#1b293d';
      ctx.fillRect(x0, rulerY - 5 * dpr, sw, 5 * dpr);
    } else {
      ctx.fillStyle = tsState.collapseGaps ? 'rgba(245,158,11,0.28)' : '#070b11';
      ctx.fillRect(x0, barTop, sw, laneH);
      if (tsState.collapseGaps) {
        ctx.fillStyle = '#f59e0b';
        ctx.fillRect(x0 + sw * 0.5 - dpr, barTop, 2 * dpr, laneH);
      }
    }
  }

  // Real-clock gap zones when not collapsing gaps
  const runs = Array.isArray(tl.runs) ? tl.runs : [];
  if (!tsState.collapseGaps && runs.length > 1) {
    for (let i = 0; i < runs.length - 1; i++) {
      const gf0 = msToTrackFrac(runs[i].endMs);
      const gf1 = msToTrackFrac(runs[i + 1].startMs);
      if (gf1 < v0 || gf0 > tsState.viewEndFrac) continue;
      const gx0 = clamp(fracToX(gf0), 0, w);
      const gx1 = clamp(fracToX(gf1), 0, w);
      const gw = gx1 - gx0;
      if (gw < 2) continue;
      ctx.fillStyle = 'rgba(2,6,12,0.86)';
      ctx.fillRect(gx0, barTop, gw, laneH);
      ctx.strokeStyle = 'rgba(148,163,184,0.16)';
      ctx.lineWidth = dpr;
      ctx.beginPath();
      for (let hx = gx0; hx < gx1; hx += 10 * dpr) {
        ctx.moveTo(hx, rulerY);
        ctx.lineTo(Math.min(gx1, hx + 8 * dpr), barTop);
      }
      ctx.stroke();
      if (gw > 76 * dpr) {
        ctx.fillStyle = 'rgba(148,163,184,0.75)';
        ctx.font = `700 ${10 * dpr}px ui-monospace,Consolas,monospace`;
        ctx.textAlign = 'center';
        ctx.fillText(`GAP ${fmtDurShort((runs[i + 1].startMs - runs[i].endMs) / 1000)}`, gx0 + gw / 2, barTop + laneH * 0.5 + 3 * dpr);
      }
    }
  }

  // 2. Perceptual log-scaled motion bars from tl.samples
  const sens = Number(tsState.sensitivity || 1.5);
  const normE = (e) => clamp(Math.log1p(Math.max(0, e)) / Math.log1p(20), 0, 1);
  const samples = Array.isArray(tl.samples) ? tl.samples : [];
  if (samples.length) {
    const numCols = Math.max(120, Math.round(rect.width / 2.2));
    const colE = new Float32Array(numCols);
    const colKind = new Uint8Array(numCols); // 1=workout, 2=motion
    const avgSamplePx = (w / Math.max(1, samples.length));

    if (avgSamplePx >= 3.5 * dpr) {
      // Zoomed in: draw crisp exact-duration segment bars
      for (let i = 0; i < samples.length; i++) {
        const s = samples[i];
        const f0 = msToTrackFrac(s[0]);
        const f1 = msToTrackFrac(s[0] + s[1]);
        if (f1 < v0 || f0 > tsState.viewEndFrac) continue;
        const x0 = fracToX(f0);
        const x1 = fracToX(f1);
        const bw = Math.max(2 * dpr, x1 - x0 - 1 * dpr);
        const e = Number(s[2] || 0);
        if (e < 0.25) continue;
        const bh = Math.max(2 * dpr, normE(e) * (laneH - 5 * dpr));
        const isMot = s[3] === 'm';
        ctx.fillStyle = e >= sens
          ? (e >= sens * 2.2 ? (isMot ? '#e879f9' : '#38bdf8') : (isMot ? '#a855f7' : '#22c55e'))
          : '#38516e';
        ctx.fillRect(x0, rulerY - 5 * dpr - bh, bw, bh);
      }
    } else {
      // Overview: bin peaks into crisp columns so no spike is ever buried
      for (let i = 0; i < samples.length; i++) {
        const s = samples[i];
        const fMid = msToTrackFrac(s[0] + s[1] * 0.5);
        if (fMid < v0 || fMid > tsState.viewEndFrac) continue;
        const ci = clamp(Math.floor(((fMid - v0) / vSpan) * numCols), 0, numCols - 1);
        const e = Number(s[2] || 0);
        if (e >= colE[ci]) {
          colE[ci] = e;
          colKind[ci] = s[3] === 'm' ? 2 : 1;
        }
      }
      const colW = w / numCols;
      for (let ci = 0; ci < numCols; ci++) {
        const e = colE[ci];
        if (e < 0.25) continue;
        const bh = Math.max(2 * dpr, normE(e) * (laneH - 5 * dpr));
        const isMot = colKind[ci] === 2;
        ctx.fillStyle = e >= sens
          ? (e >= sens * 2.2 ? (isMot ? '#e879f9' : '#38bdf8') : (isMot ? '#a855f7' : '#22c55e'))
          : '#38516e';
        ctx.fillRect(ci * colW, rulerY - 5 * dpr - bh, Math.max(1.3 * dpr, colW - 0.4), bh);
      }
    }
  }

  // 3. Sensitivity threshold hairline
  const thY = rulerY - 5 * dpr - normE(sens) * (laneH - 5 * dpr);
  ctx.strokeStyle = 'rgba(245,158,11,0.48)';
  ctx.setLineDash([4 * dpr, 4 * dpr]);
  ctx.lineWidth = Math.max(1, dpr);
  ctx.beginPath();
  ctx.moveTo(0, thY);
  ctx.lineTo(w, thY);
  ctx.stroke();
  ctx.setLineDash([]);

  // 4. Detected event ribbon ticks at top
  (tsState.events || []).forEach(b => {
    const f0 = msToTrackFrac(b.startMs);
    const f1 = msToTrackFrac(b.endMs);
    if (f1 < v0 || f0 > tsState.viewEndFrac) return;
    const x0 = clamp(fracToX(f0), 0, w);
    const x1 = clamp(fracToX(f1), x0 + 2 * dpr, w);
    ctx.fillStyle = b.kind === 'motion' ? '#c084fc' : '#4ade80';
    ctx.fillRect(x0, 2 * dpr, x1 - x0, 5 * dpr);
  });

  // 5. In-Out clip range highlight & draggable handles
  if (tsState.inMs != null && tsState.outMs != null && tsState.outMs > tsState.inMs) {
    const ix0 = fracToX(msToTrackFrac(tsState.inMs));
    const ix1 = fracToX(msToTrackFrac(tsState.outMs));
    if (ix1 >= 0 && ix0 <= w) {
      const cx0 = clamp(ix0, 0, w);
      const cx1 = clamp(ix1, cx0 + 2 * dpr, w);
      ctx.fillStyle = 'rgba(245,158,11,0.20)';
      ctx.fillRect(cx0, barTop, cx1 - cx0, laneH);
      ctx.fillStyle = '#f59e0b';
      ctx.fillRect(cx0 - dpr, barTop, 2.5 * dpr, laneH);
      ctx.fillRect(cx1 - 1.5 * dpr, barTop, 2.5 * dpr, laneH);
      ctx.fillRect(cx0 - 4 * dpr, barTop, 9 * dpr, 12 * dpr);
      ctx.fillRect(cx1 - 5 * dpr, barTop, 9 * dpr, 12 * dpr);
    }
  } else if (tsState.inMs != null) {
    const ix0 = fracToX(msToTrackFrac(tsState.inMs));
    if (ix0 >= 0 && ix0 <= w) {
      ctx.fillStyle = '#f59e0b';
      ctx.fillRect(ix0 - dpr, barTop, 2.5 * dpr, laneH);
    }
  }

  // 6. Bottom time ruler ticks
  ctx.fillStyle = '#090e16';
  ctx.fillRect(0, rulerY, w, rulerH);
  ctx.fillStyle = '#8b9cb3';
  ctx.font = `600 ${10 * dpr}px ui-monospace,Consolas,monospace`;
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const f = v0 + (i / ticks) * vSpan;
    const tx = (i / ticks) * w;
    const tMs = trackFracToMs(f, false);
    ctx.textAlign = i === 0 ? 'left' : (i === ticks ? 'right' : 'center');
    const padX = i === 0 ? 4 * dpr : (i === ticks ? w - 4 * dpr : tx);
    ctx.fillRect(tx, rulerY, dpr, 3 * dpr);
    ctx.fillText(fmtClock(tMs, vSpan < 0.2), padX, h - 4 * dpr);
  }

  // 7. Hover crosshair
  if (tsState.hoverFrac != null && tsState.hoverFrac >= v0 && tsState.hoverFrac <= tsState.viewEndFrac) {
    const hx = fracToX(tsState.hoverFrac);
    ctx.fillStyle = 'rgba(255,255,255,0.40)';
    ctx.fillRect(hx, barTop, 1.2 * dpr, laneH);
  }

  // 8. Playhead needle
  const pf = msToTrackFrac(tsState.curMs);
  if (pf >= v0 && pf <= tsState.viewEndFrac) {
    const px = clamp(fracToX(pf), 2, w - 2);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(px - 1.5 * dpr, 0, 3 * dpr, rulerY);
    ctx.beginPath();
    ctx.arc(px, 6 * dpr, 5.5 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = '#2ea0ff';
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.6 * dpr;
    ctx.stroke();
  }
}

async function preloadViewportPack() {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return;
  const sMs = trackFracToMs(tsState.viewStartFrac, true);
  const eMs = Math.max(sMs + 2000, trackFracToMs(tsState.viewEndFrac, true));
  const key = `${tsState.sessionId}|${tsState.sourceFilter}|${tsState.camera}|${Math.round(sMs / 2000)}|${Math.round(eMs / 2000)}`;
  if (tsState.packCache.has(key)) {
    tsState.activePack = tsState.packCache.get(key);
    return;
  }
  if (tsState.packAbort) {
    try { tsState.packAbort.abort(); } catch {}
  }
  tsState.packAbort = new AbortController();
  const q = new URLSearchParams({
    sessionId: tsState.sessionId || '',
    camera: tsState.camera || 'Rack Cam',
    source: tsState.sourceFilter || 'all',
    startMs: String(sMs),
    endMs: String(eMs),
    count: '36',
    w: '280',
  });
  try {
    const r = await fetch(`/api/tape/pack?${q.toString()}`, { signal: tsState.packAbort.signal, cache: 'no-store' }).then(x => x.json());
    if (!r || !Array.isArray(r.frames)) return;
    const pack = r.frames.map(f => ({ tsMs: Number(f.tsMs), src: 'data:image/jpeg;base64,' + f.jpg }));
    pack.sort((a, b) => a.tsMs - b.tsMs);
    if (tsState.packCache.size >= 16) {
      const oldest = tsState.packCache.keys().next().value;
      if (oldest) tsState.packCache.delete(oldest);
    }
    tsState.packCache.set(key, pack);
    tsState.activePack = pack;
  } catch {}
}

function nearestPackThumb(ms, maxDistMs = 45_000) {
  const pack = tsState.activePack;
  if (!pack || !pack.length) return null;
  let lo = 0;
  let hi = pack.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pack[mid].tsMs < ms) lo = mid + 1;
    else hi = mid;
  }
  let best = pack[lo];
  if (lo > 0 && Math.abs(pack[lo - 1].tsMs - ms) < Math.abs(best.tsMs - ms)) {
    best = pack[lo - 1];
  }
  return Math.abs(best.tsMs - ms) <= maxDistMs ? best.src : null;
}

function showGapOverlay(gapInfo, atMs) {
  const ov = $('tsGapOverlay');
  if (!ov) return;
  ov.hidden = false;
  if ($('tsGapTitle')) $('tsGapTitle').textContent = `No recording at ${fmtClock(atMs)}`;
  const hasBounds = gapInfo && gapInfo.prevRun && gapInfo.nextRun;
  if ($('tsGapSub')) {
    $('tsGapSub').textContent = hasBounds
      ? `${fmtDurShort((gapInfo.nextRun.startMs - gapInfo.prevRun.endMs) / 1000)} gap (${fmtClock(gapInfo.prevRun.endMs, false)} → ${fmtClock(gapInfo.nextRun.startMs, false)})`
      : 'Outside recorded footage window';
  }
  const btn = $('tsJumpNextRunBtn');
  if (btn && gapInfo && gapInfo.fallbackRun) {
    btn.textContent = gapInfo.nextRun
      ? `Jump to ${fmtClock(gapInfo.nextRun.startMs, false)} →`
      : `Jump to ${fmtClock(gapInfo.fallbackRun.startMs, false)} →`;
    btn.onclick = () => scrubToMs(gapInfo.fallbackRun.startMs, true);
  }
}

function hideGapOverlay() {
  if ($('tsGapOverlay')) $('tsGapOverlay').hidden = true;
}

function updateHudAtMs(ms, gapInfo = null) {
  const eng = sampleEnergyAt(ms);
  const sens = Number(tsState.sensitivity || 1.5);
  const tag = gapInfo ? 'NO RECORDING (GAP)' : (eng >= sens ? `ACTIVE ${eng.toFixed(1)}` : `quiet ${eng.toFixed(1)}`);
  if ($('tsTimeHud')) $('tsTimeHud').textContent = `${tsState.camera} · ${fmtClock(ms)} · ${tag}`;
  if ($('tsCurLbl')) $('tsCurLbl').textContent = fmtClock(ms);
  document.querySelectorAll('#tsBurstList .ts-ev-card').forEach(card => {
    const b = tsState.events[Number(card.dataset.tsburst)];
    card.classList.toggle('on', Boolean(b && ms >= b.startMs - 1500 && ms <= b.endMs + 1500));
  });
  const tl = tsState.timeline;
  const runs = tl && Array.isArray(tl.runs) ? tl.runs : [];
  document.querySelectorAll('#tsRunStrip .ts-run-btn').forEach(btn => {
    const r = runs[Number(btn.dataset.tsrun)];
    btn.classList.toggle('on', Boolean(r && ms >= r.startMs - 1500 && ms <= r.endMs + 1500));
  });
}

function scrubToMs(ms, immediate = false) {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return;
  const clamped = Math.round(clamp(ms, tl.minMs, tl.maxMs));
  tsState.curMs = clamped;
  const gapInfo = findGapAt(clamped);
  updateHudAtMs(clamped, gapInfo);
  drawTapeTimeline();

  if (tsState.playing) return;
  if (gapInfo) {
    if (tsState.frameTimer) clearTimeout(tsState.frameTimer);
    showGapOverlay(gapInfo, clamped);
    return;
  }
  hideGapOverlay();

  const thumb = nearestPackThumb(clamped);
  if (thumb && $('tsFrameImg')) {
    $('tsFrameImg').hidden = false;
    $('tsFrameImg').src = thumb;
  }

  if (tsState.frameTimer) clearTimeout(tsState.frameTimer);
  const runFetch = async () => {
    if (tsState.frameAbort) {
      try { tsState.frameAbort.abort(); } catch {}
    }
    tsState.frameAbort = new AbortController();
    const q = new URLSearchParams({
      sessionId: tsState.sessionId || '',
      camera: tsState.camera || 'Rack Cam',
      source: tsState.sourceFilter || 'all',
      tsMs: String(clamped),
      w: '960',
      maxGapMs: '10000',
    });
    try {
      const resp = await fetch(`/api/tape/frame?${q.toString()}`, { signal: tsState.frameAbort.signal });
      if (resp.status === 204) {
        const g = findGapAt(tsState.curMs) || { fallbackRun: (tl.runs && tl.runs[0]) || { startMs: tl.minMs } };
        showGapOverlay(g, tsState.curMs);
        return;
      }
      if (!resp.ok) return;
      const blob = await resp.blob();
      const objUrl = URL.createObjectURL(blob);
      const prevUrl = tsState.frameObjUrl;
      tsState.frameObjUrl = objUrl;
      if ($('tsFrameImg')) {
        $('tsFrameImg').hidden = false;
        $('tsFrameImg').src = objUrl;
      }
      if (prevUrl) { try { URL.revokeObjectURL(prevUrl); } catch {} }
    } catch {}
  };
  if (immediate) runFetch();
  else tsState.frameTimer = setTimeout(runFetch, 95);
}

function flashSkipToast(msg) {
  const el = $('tsSkipToast');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  if (tsState.skipToastTimer) clearTimeout(tsState.skipToastTimer);
  tsState.skipToastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

function syncPlayControlsUI() {
  const btn = $('tsPlayBtn');
  if (btn) {
    btn.textContent = tsState.playing ? '⏸ Pause' : '▶ Play';
    btn.className = `btn sm ts-play-main ${tsState.playing ? 'sec on' : 'ok'}`;
  }
  if ($('tsPlayBadge')) {
    $('tsPlayBadge').hidden = !tsState.playing;
    if (tsState.playing) $('tsPlayBadge').textContent = `LIVE ${tsState.speed}×`;
  }
}

function startTapePlayback(startAtMs) {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return;
  hideGapOverlay();
  if (tsState.packTimer) {
    clearTimeout(tsState.packTimer);
    tsState.packTimer = null;
  }
  if (tsState.packAbort) {
    try { tsState.packAbort.abort(); } catch {}
  }
  if (tsState.frameAbort) {
    try { tsState.frameAbort.abort(); } catch {}
  }
  if (tsState.frameTimer) {
    clearTimeout(tsState.frameTimer);
    tsState.frameTimer = null;
  }

  let targetMs = Math.round(clamp(startAtMs != null ? startAtMs : (tsState.curMs || tl.minMs), tl.minMs, tl.maxMs));
  const gap = findGapAt(targetMs);
  if (gap && gap.fallbackRun) {
    targetMs = gap.fallbackRun.startMs;
  }
  const rInfo = findRunAt(targetMs);
  const run = rInfo.run || { startMs: tl.minMs, endMs: tl.maxMs };
  if (run.endMs - targetMs < 1500) {
    targetMs = Math.max(run.startMs, run.endMs - 2000);
  }

  tsState.playing = true;
  tsState.buffering = true;
  tsState.quietSinceMs = 0;
  tsState.skipCooldownUntil = performance.now() + 2500;
  const gen = ++tsState.playGen;
  tsState.streamStartMs = targetMs;
  tsState.streamEndMs = Math.min(run.endMs, targetMs + 600_000);
  tsState.curMs = targetMs;
  syncPlayControlsUI();

  if ($('tsBuffering')) $('tsBuffering').hidden = false;
  if ($('tsBufferLbl')) $('tsBufferLbl').textContent = `Starting ${tsState.camera} stream…`;

  const vid = $('tsVideo');
  const q = new URLSearchParams({
    sessionId: tsState.sessionId || '',
    camera: tsState.camera || 'Rack Cam',
    source: tsState.sourceFilter || 'all',
    startMs: String(targetMs),
    maxSec: '600',
    streamId: tsState.streamId,
    _: String(Date.now()),
  });

  if (vid) {
    vid.hidden = false;
    vid.muted = true;
    vid.style.opacity = '0';
    vid.src = `/api/tape/stream.mp4?${q.toString()}`;
    vid.playbackRate = tsState.speed || 1;
    vid.onplaying = () => {
      if (gen !== tsState.playGen) return;
      tsState.buffering = false;
      vid.style.opacity = '1';
      if ($('tsBuffering')) $('tsBuffering').hidden = true;
      vid.playbackRate = tsState.speed || 1;
    };
    vid.onwaiting = () => {
      if (gen !== tsState.playGen || !tsState.playing) return;
      if ($('tsBuffering')) $('tsBuffering').hidden = false;
      if ($('tsBufferLbl')) $('tsBufferLbl').textContent = 'Buffering stream…';
    };
    vid.onended = () => {
      if (gen !== tsState.playGen || !tsState.playing) return;
      const endedMs = tsState.streamStartMs + Math.round((vid.currentTime || 0) * 1000);
      if ((run.endMs - Math.max(endedMs, tsState.streamEndMs - 5000)) > 2500 && (vid.currentTime || 0) > 1) {
        startTapePlayback(endedMs);
        return;
      }
      const nextRun = (rInfo.runs || []).find(r => r.startMs > run.endMs + 1000);
      if (nextRun) {
        flashSkipToast(`Skipped ${fmtDurShort((nextRun.startMs - run.endMs) / 1000)} gap →`);
        startTapePlayback(nextRun.startMs);
      } else {
        stopTapePlayback(true);
        showToast('Reached end of footage', 'ok');
      }
    };
    vid.onerror = () => {
      if (gen !== tsState.playGen || !tsState.playing) return;
      stopTapePlayback(true);
      showToast('Stream unavailable for this segment', 'err');
    };
    vid.play().catch(() => {});
  }

  if (tsState.rafId) cancelAnimationFrame(tsState.rafId);
  tsState.rafId = requestAnimationFrame(() => tickTapePlayback(gen));
}

function tickTapePlayback(gen) {
  if (!tsState.playing || gen !== tsState.playGen) return;
  const vid = $('tsVideo');
  const tl = tsState.timeline;
  if (vid && tl && !vid.paused && vid.readyState >= 2) {
    const liveMs = Math.round(clamp(tsState.streamStartMs + (vid.currentTime || 0) * 1000, tl.minMs, tl.maxMs));
    tsState.curMs = liveMs;
    updateHudAtMs(liveMs, null);

    // Auto-pan zoomed viewport when playhead nears right edge
    const vSpan = tsState.viewEndFrac - tsState.viewStartFrac;
    if (vSpan < 0.995) {
      const pf = msToTrackFrac(liveMs);
      if (pf > tsState.viewStartFrac + vSpan * 0.92 && tsState.viewEndFrac < 1) {
        const s = clamp(pf - vSpan * 0.25, 0, 1 - vSpan);
        tsState.viewStartFrac = s;
        tsState.viewEndFrac = s + vSpan;
        preloadViewportPack();
      }
    }
    drawTapeTimeline();

    if (tsState.audioOn && tl.hasAudio) {
      syncTapeAudio(liveMs);
    }

    if (tsState.motionOnly && performance.now() > tsState.skipCooldownUntil && tsState.events.length) {
      const inEv = tsState.events.some(ev => liveMs >= ev.startMs - 2000 && liveMs <= ev.endMs + 2500);
      if (inEv) {
        tsState.quietSinceMs = 0;
      } else {
        if (!tsState.quietSinceMs) tsState.quietSinceMs = performance.now();
        if (performance.now() - tsState.quietSinceMs > 1600) {
          const nextEv = tsState.events.find(ev => ev.startMs > liveMs + 1000);
          if (nextEv) {
            const skipSec = Math.round((nextEv.startMs - liveMs) / 1000);
            flashSkipToast(`⚡ Skipped ${fmtDurShort(skipSec)} quiet`);
            startTapePlayback(Math.max(tl.minMs, nextEv.startMs - 1500));
            return;
          } else {
            stopTapePlayback(true);
            showToast('Reached end of motion events', 'ok');
            return;
          }
        }
      }
    }
  }
  tsState.rafId = requestAnimationFrame(() => tickTapePlayback(gen));
}

function stopTapePlayback(fetchHiRes = true) {
  const wasPlaying = tsState.playing;
  tsState.playing = false;
  tsState.buffering = false;
  tsState.playGen += 1;
  if (tsState.rafId) {
    cancelAnimationFrame(tsState.rafId);
    tsState.rafId = 0;
  }
  const vid = $('tsVideo');
  if (vid) {
    vid.onplaying = null;
    vid.onwaiting = null;
    vid.onended = null;
    vid.onerror = null;
    try { vid.pause(); } catch {}
    vid.removeAttribute('src');
    try { vid.load(); } catch {}
    vid.style.opacity = '0';
    vid.hidden = true;
  }
  const aud = $('tsAudio');
  if (aud) {
    try { aud.pause(); } catch {}
  }
  if ($('tsBuffering')) $('tsBuffering').hidden = true;
  if (wasPlaying) {
    fetch('/api/tape/stream/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ streamId: tsState.streamId }),
    }).catch(() => {});
  }
  syncPlayControlsUI();
  if (fetchHiRes && tsState.timeline && tsState.timeline.segmentCount) {
    scrubToMs(tsState.curMs, true);
    if (!tsState.activePack || !tsState.activePack.length) preloadViewportPack();
  }
}

function syncTapeAudio(curMs) {
  const aud = $('tsAudio');
  if (!aud || !tsState.audioOn || !tsState.playing) return;
  const chunkStart = Math.floor(curMs / 60_000) * 60_000;
  const chunkEnd = chunkStart + 60_000;
  const offset = Math.max(0, (curMs - chunkStart) / 1000);
  if (tsState.audioChunkStart !== chunkStart || !aud.src) {
    tsState.audioChunkStart = chunkStart;
    tsState.audioChunkEnd = chunkEnd;
    aud.src = `/api/audio/clip?startMs=${chunkStart}&endMs=${chunkEnd}`;
    aud.playbackRate = tsState.speed || 1;
    aud.onloadedmetadata = () => {
      try { aud.currentTime = offset; } catch {}
      if (tsState.audioOn && tsState.playing) aud.play().catch(() => {});
    };
    aud.onerror = () => {};
  } else {
    aud.playbackRate = tsState.speed || 1;
    if (aud.readyState >= 2 && Math.abs((aud.currentTime || 0) - offset) > 0.5) {
      try { aud.currentTime = offset; } catch {}
    }
    if (aud.paused && aud.readyState >= 2) {
      aud.play().catch(() => {});
    }
  }
}

function toggleTapeAudio() {
  const tl = tsState.timeline;
  if (!tl || !tl.hasAudio) {
    showToast('No S330 room audio for this window', 'err');
    return;
  }
  tsState.audioOn = !tsState.audioOn;
  const btn = $('tsAudioBtn');
  if (btn) {
    btn.textContent = tsState.audioOn ? '🔊 Audio ON' : '🔇 Audio';
    btn.classList.toggle('on', tsState.audioOn);
  }
  const aud = $('tsAudio');
  if (!tsState.audioOn && aud) {
    try { aud.pause(); } catch {}
  } else if (tsState.audioOn && tsState.playing) {
    tsState.audioChunkStart = 0;
    syncTapeAudio(tsState.curMs);
  }
}

function toggleMotionOnly() {
  tsState.motionOnly = !tsState.motionOnly;
  tsState.quietSinceMs = 0;
  if ($('tsMotionOnlyBtn')) $('tsMotionOnlyBtn').classList.toggle('on', tsState.motionOnly);
  if (tsState.motionOnly) showToast('⚡ Motion-Only playback ON (auto-skips quiet)', 'ok');
}

function toggleStageFullscreen() {
  const st = $('tsStage');
  if (!st) return;
  if (document.fullscreenElement === st) {
    document.exitFullscreen().catch(() => {});
  } else if (st.requestFullscreen) {
    st.requestFullscreen().catch(() => {});
  }
}

function stepScrubSec(deltaSec) {
  const wasPlaying = tsState.playing;
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return;
  const nextMs = Math.round(clamp((tsState.curMs || tl.minMs) + Number(deltaSec) * 1000, tl.minMs, tl.maxMs));
  if (wasPlaying) startTapePlayback(nextMs);
  else scrubToMs(nextMs, true);
}

function backToScrubMode() {
  stopTapePlayback(false);
}

function playTapeWindow() {
  if (tsState.playing) stopTapePlayback(true);
  else startTapePlayback(tsState.curMs);
}

function jumpActivityBurst(dir) {
  const tl = tsState.timeline;
  const bursts = tsState.events || [];
  if (!bursts.length) {
    showToast(`No motion events ≥${tsState.minDurSec}s at sensitivity ${Number(tsState.sensitivity).toFixed(1)}`, 'err');
    return;
  }
  const cur = tsState.curMs || tl.minMs;
  let pick = bursts[0];
  if (dir > 0) {
    pick = bursts.find(b => b.startMs > cur + 1500) || bursts[0];
  } else {
    const prev = bursts.filter(b => b.startMs < cur - 1500);
    pick = prev.length ? prev[prev.length - 1] : bursts[bursts.length - 1];
  }
  const targetMs = Math.max(tl.minMs, pick.startMs - 1500);
  if (pick.camera && pick.camera !== tsState.camera && tsState.cameras.includes(pick.camera)) {
    const wasPlaying = tsState.playing;
    stopTapePlayback(false);
    tsState.camera = pick.camera;
    renderTapeCamStrip();
    loadTapeTimeline(true, { resumePlay: wasPlaying, targetMs });
    return;
  }
  if (tsState.playing) startTapePlayback(targetMs);
  else scrubToMs(targetMs, true);
}

function updateRangeInfo() {
  const el = $('tsRangeInfo');
  if (!el) return;
  if (tsState.inMs != null && tsState.outMs != null && tsState.outMs > tsState.inMs) {
    el.textContent = `${fmtClock(tsState.inMs, false)}→${fmtClock(tsState.outMs, false)} (${fmtDurShort((tsState.outMs - tsState.inMs) / 1000)})`;
    el.style.color = '#fcd34d';
  } else if (tsState.inMs != null) {
    el.textContent = `IN ${fmtClock(tsState.inMs)}`;
    el.style.color = '#fcd34d';
  } else {
    el.textContent = 'Default +30s';
    el.style.color = 'var(--mut)';
  }
}

function setScrubInOut(which) {
  if (!tsState.curMs) return;
  if (which === 'in') {
    tsState.inMs = tsState.curMs;
    if (tsState.outMs != null && tsState.outMs <= tsState.inMs) tsState.outMs = null;
  } else if (which === 'out') {
    tsState.outMs = tsState.curMs;
    if (tsState.inMs != null && tsState.outMs <= tsState.inMs) tsState.inMs = Math.max(0, tsState.outMs - 10000);
  } else {
    tsState.inMs = null;
    tsState.outMs = null;
  }
  updateRangeInfo();
  drawTapeTimeline();
}

async function saveScrubberClip() {
  const tl = tsState.timeline;
  if (!tl || !tl.segmentCount) return;
  const sMs = (tsState.inMs != null && tsState.outMs != null && tsState.outMs > tsState.inMs)
    ? tsState.inMs
    : Math.max(tl.minMs, tsState.curMs - 4000);
  const eMs = (tsState.inMs != null && tsState.outMs != null && tsState.outMs > tsState.inMs)
    ? tsState.outMs
    : Math.min(tl.maxMs, sMs + 30_000);
  const exName = (($('tsClipLabel') && $('tsClipLabel').value.trim()) || 'scrub_clip');
  const includeAudio = Boolean($('tsIncludeAudio') && $('tsIncludeAudio').checked);
  if ($('tsOut')) $('tsOut').textContent = 'Cutting clip MP4 & syncing Drive…';
  try {
    const r = await fetch('/api/clip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sessionId: tsState.sessionId || '',
        source: tsState.sourceFilter || 'all',
        exName,
        startMs: sMs,
        endMs: eMs,
        cameras: [tsState.camera || 'Rack Cam'],
        includeAudio,
      }),
    }).then(x => x.json());
    if (!r || !r.ok) {
      if ($('tsOut')) $('tsOut').textContent = `✗ ${(r && r.error) || 'Save failed'}`;
      return;
    }
    if ($('tsOut')) {
      $('tsOut').innerHTML = `✓ Saved <a href="${escHtml(r.lanUrl)}" target="_blank">${escHtml(r.fileName)}</a> (${r.durationSec}s)`;
    }
    showToast(`✓ Saved ${r.fileName}`, 'ok');
    pollClips();
  } catch {
    if ($('tsOut')) $('tsOut').textContent = '✗ Save clip failed';
  }
}

async function triggerTapeRescan() {
  const btn = $('tsRescanBtn');
  if (btn) setBtnFeedback(btn, 'saving', 'Scanning…');
  try {
    await fetch('/api/tape/rescan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId: tsState.sessionId || '', source: tsState.sourceFilter || 'all', force: true }),
    });
    if ($('tsScanPill')) {
      $('tsScanPill').hidden = false;
      $('tsScanPill').textContent = 'Scanning v2…';
      $('tsScanPill').className = 'pill live';
    }
    scheduleTapeScanPoll();
    if (btn) setBtnFeedback(btn, 'saved', '✓ Started', 1600);
  } catch {
    if (btn) setBtnFeedback(btn, 'save-err', '✗ Failed', 2000);
  }
}

function updateHoverPopover(ev, canvas) {
  const pop = $('tsHoverPop');
  const tl = tsState.timeline;
  const rail = $('tsRailWrap');
  if (!pop || !tl || !tl.segmentCount || !rail) return;
  const rect = canvas.getBoundingClientRect();
  const railRect = rail.getBoundingClientRect();
  const relRatio = clamp((ev.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
  const trackFrac = tsState.viewStartFrac + relRatio * (tsState.viewEndFrac - tsState.viewStartFrac);
  const hMs = trackFracToMs(trackFrac, false);
  tsState.hoverFrac = trackFrac;
  tsState.hoverMs = hMs;

  const isGap = Boolean(findGapAt(hMs));
  const eng = sampleEnergyAt(hMs);
  const pxInRail = (ev.clientX - railRect.left);
  const halfPop = 104;
  const clampedLeft = clamp(pxInRail, halfPop, Math.max(halfPop, railRect.width - halfPop));
  pop.hidden = false;
  pop.style.left = `${clampedLeft.toFixed(1)}px`;
  if ($('tsHoverTime')) $('tsHoverTime').textContent = fmtClock(hMs);
  if ($('tsHoverScore')) {
    $('tsHoverScore').textContent = isGap ? 'GAP' : `motion ${eng.toFixed(1)}`;
    $('tsHoverScore').style.color = isGap ? '#94a3b8' : (eng >= tsState.sensitivity ? '#86efac' : '#cbd5e1');
  }

  const hImg = $('tsHoverImg');
  const hGap = $('tsHoverGap');
  if (isGap) {
    if (hImg) hImg.hidden = true;
    if (hGap) hGap.hidden = false;
  } else {
    const thumb = nearestPackThumb(hMs, 90_000);
    if (thumb && hImg) {
      if (hImg.src !== thumb) hImg.src = thumb;
      hImg.hidden = false;
      if (hGap) hGap.hidden = true;
    } else {
      if (hImg) hImg.hidden = true;
      if (hGap) hGap.hidden = true;
    }
  }
}

function wireTapeScrubber() {
  const canvas = $('tsTimelineCanvas');
  if (canvas) {
    const trackFracForEv = (ev) => {
      const rect = canvas.getBoundingClientRect();
      const rel = clamp((ev.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
      return {
        rel,
        frac: tsState.viewStartFrac + rel * (tsState.viewEndFrac - tsState.viewStartFrac),
        xPx: ev.clientX - rect.left,
        wPx: rect.width,
      };
    };

    canvas.addEventListener('pointerdown', (ev) => {
      const tl = tsState.timeline;
      if (!tl || !tl.segmentCount) return;
      canvas.setPointerCapture(ev.pointerId);
      const info = trackFracForEv(ev);

      // Hit-test draggable In/Out handles first
      const pxForMs = (ms) => ((msToTrackFrac(ms) - tsState.viewStartFrac) / Math.max(1e-6, tsState.viewEndFrac - tsState.viewStartFrac)) * info.wPx;
      if (tsState.inMs != null && Math.abs(info.xPx - pxForMs(tsState.inMs)) <= 10) {
        tsState.dragMode = 'in';
        return;
      }
      if (tsState.outMs != null && Math.abs(info.xPx - pxForMs(tsState.outMs)) <= 10) {
        tsState.dragMode = 'out';
        return;
      }
      if (ev.shiftKey && (tsState.viewEndFrac - tsState.viewStartFrac) < 0.995) {
        tsState.dragMode = 'pan';
        tsState.panStartX = ev.clientX;
        tsState.panStartFrac0 = tsState.viewStartFrac;
        return;
      }

      tsState.dragMode = 'scrub';
      tsState.wasPlayingOnDrag = tsState.playing;
      if (tsState.playing) stopTapePlayback(false);
      const targetMs = trackFracToMs(info.frac, true);
      scrubToMs(targetMs, false);
    });

    canvas.addEventListener('pointermove', (ev) => {
      const tl = tsState.timeline;
      if (!tl || !tl.segmentCount) return;
      const info = trackFracForEv(ev);
      updateHoverPopover(ev, canvas);

      if (tsState.dragMode === 'in') {
        const m = trackFracToMs(info.frac, true);
        tsState.inMs = tsState.outMs != null ? Math.min(m, tsState.outMs - 1000) : m;
        updateRangeInfo();
        drawTapeTimeline();
      } else if (tsState.dragMode === 'out') {
        const m = trackFracToMs(info.frac, true);
        tsState.outMs = tsState.inMs != null ? Math.max(m, tsState.inMs + 1000) : m;
        updateRangeInfo();
        drawTapeTimeline();
      } else if (tsState.dragMode === 'pan') {
        const span = tsState.viewEndFrac - tsState.viewStartFrac;
        const dxFrac = ((tsState.panStartX - ev.clientX) / Math.max(1, info.wPx)) * span;
        const s = clamp(tsState.panStartFrac0 + dxFrac, 0, 1 - span);
        tsState.viewStartFrac = s;
        tsState.viewEndFrac = s + span;
        drawTapeTimeline();
      } else if (tsState.dragMode === 'scrub') {
        scrubToMs(trackFracToMs(info.frac, true), false);
      } else {
        drawTapeTimeline();
      }
    });

    const endDrag = () => {
      const mode = tsState.dragMode;
      if (!mode) return;
      tsState.dragMode = '';
      if (mode === 'pan') {
        preloadViewportPack();
      } else if (mode === 'scrub') {
        if (tsState.wasPlayingOnDrag && !findGapAt(tsState.curMs)) {
          tsState.wasPlayingOnDrag = false;
          startTapePlayback(tsState.curMs);
        } else {
          tsState.wasPlayingOnDrag = false;
          scrubToMs(tsState.curMs, true);
        }
      }
    };
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);
    canvas.addEventListener('pointerleave', () => {
      tsState.hoverFrac = null;
      if ($('tsHoverPop')) $('tsHoverPop').hidden = true;
      drawTapeTimeline();
    });
    canvas.addEventListener('wheel', (ev) => {
      const tl = tsState.timeline;
      if (!tl || !tl.segmentCount) return;
      ev.preventDefault();
      const info = trackFracForEv(ev);
      zoomAtCanvasFrac(info.frac, ev.deltaY < 0);
    }, { passive: false });
  }

  document.querySelectorAll('[data-tssrc]').forEach(btn => {
    btn.onclick = () => {
      const next = btn.dataset.tssrc || 'all';
      if (tsState.sourceFilter === next) return;
      stopTapePlayback(false);
      tsState.sourceFilter = next;
      document.querySelectorAll('[data-tssrc]').forEach(b => b.classList.toggle('on', b.dataset.tssrc === next));
      tsState.timeline = null;
      tsState.sessionId = '';
      if ($('tsDayStrip')) $('tsDayStrip').innerHTML = `<span class="ts-pill" style="opacity:.7">Loading ${escHtml(next)}…</span>`;
      loadTapes();
    };
  });

  document.querySelectorAll('[data-tsgap]').forEach(btn => {
    btn.onclick = () => setGapMode(btn.dataset.tsgap);
  });
  document.querySelectorAll('[data-tszoom]').forEach(btn => {
    btn.onclick = () => setZoomMode(btn.dataset.tszoom);
  });
  if ($('tsPanLeftBtn')) $('tsPanLeftBtn').onclick = () => panViewport(-1);
  if ($('tsPanRightBtn')) $('tsPanRightBtn').onclick = () => panViewport(1);

  document.querySelectorAll('[data-tsstep]').forEach(btn => {
    btn.onclick = () => stepScrubSec(Number(btn.dataset.tsstep || 0));
  });
  if ($('tsStage')) {
    $('tsStage').addEventListener('click', () => playTapeWindow());
    $('tsStage').addEventListener('dblclick', () => toggleStageFullscreen());
  }
  if ($('tsFsBtn')) $('tsFsBtn').onclick = () => toggleStageFullscreen();
  if ($('tsRefreshBtn')) $('tsRefreshBtn').onclick = () => loadTapes().then(() => loadTapeTimeline(true));
  if ($('tsRescanBtn')) $('tsRescanBtn').onclick = () => triggerTapeRescan();
  if ($('tsPrevBurstBtn')) $('tsPrevBurstBtn').onclick = () => jumpActivityBurst(-1);
  if ($('tsNextBurstBtn')) $('tsNextBurstBtn').onclick = () => jumpActivityBurst(1);
  if ($('tsPlayBtn')) $('tsPlayBtn').onclick = () => playTapeWindow();
  if ($('tsMotionOnlyBtn')) $('tsMotionOnlyBtn').onclick = () => toggleMotionOnly();
  if ($('tsAudioBtn')) $('tsAudioBtn').onclick = () => toggleTapeAudio();
  if ($('tsSpeedSel')) $('tsSpeedSel').onchange = () => {
    const sp = Number($('tsSpeedSel').value || 1) || 1;
    tsState.speed = sp;
    if ($('tsVideo')) $('tsVideo').playbackRate = sp;
    if ($('tsAudio')) $('tsAudio').playbackRate = sp;
    syncPlayControlsUI();
  };

  if ($('tsSensSlider')) $('tsSensSlider').oninput = () => {
    const val = Number($('tsSensSlider').value || 1.5);
    tsState.sensitivity = val;
    if ($('tsSensVal')) $('tsSensVal').textContent = val.toFixed(1);
    document.querySelectorAll('[data-tssens]').forEach(b => b.classList.toggle('on', Math.abs(Number(b.dataset.tssens) - val) < 0.05));
    reclusterEventsOnly(true);
  };
  document.querySelectorAll('[data-tssens]').forEach(btn => {
    btn.onclick = () => {
      const val = Number(btn.dataset.tssens || 1.5);
      tsState.sensitivity = val;
      if ($('tsSensSlider')) $('tsSensSlider').value = String(val);
      if ($('tsSensVal')) $('tsSensVal').textContent = val.toFixed(1);
      document.querySelectorAll('[data-tssens]').forEach(b => b.classList.toggle('on', b === btn));
      reclusterEventsOnly(true);
    };
  });
  document.querySelectorAll('[data-tsmindur]').forEach(btn => {
    btn.onclick = () => {
      tsState.minDurSec = Number(btn.dataset.tsmindur || 5);
      document.querySelectorAll('[data-tsmindur]').forEach(b => b.classList.toggle('on', b === btn));
      reclusterEventsOnly(true);
    };
  });
  document.querySelectorAll('[data-tsscope]').forEach(btn => {
    btn.onclick = () => {
      tsState.allCamsEvents = (btn.dataset.tsscope === 'all');
      document.querySelectorAll('[data-tsscope]').forEach(b => b.classList.toggle('on', b === btn));
      reclusterEventsOnly(true);
    };
  });

  if ($('tsSetInBtn')) $('tsSetInBtn').onclick = () => setScrubInOut('in');
  if ($('tsSetOutBtn')) $('tsSetOutBtn').onclick = () => setScrubInOut('out');
  if ($('tsClearRangeBtn')) $('tsClearRangeBtn').onclick = () => setScrubInOut('clear');
  if ($('tsSaveClipBtn')) $('tsSaveClipBtn').onclick = () => saveScrubberClip();
}

init();
</script>
</body>
</html>
"""


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None | str:
    """Parse HTTP `Range: bytes=start-end`; returns `(start, end)`, `None` (no range), or `'invalid'`."""
    if not range_header:
        return None
    val = range_header.strip()
    if not val.startswith("bytes="):
        return "invalid"
    spec = val[len("bytes=") :].split(",")[0].strip()
    if "-" not in spec:
        return "invalid"
    left, right = spec.split("-", 1)
    try:
        if left == "":
            suffix = int(right)
            if suffix <= 0:
                return "invalid"
            start = max(0, file_size - suffix)
            end = file_size - 1
        elif right == "":
            start = int(left)
            end = file_size - 1
        else:
            start = int(left)
            end = min(int(right), file_size - 1)
    except ValueError:
        return "invalid"
    if start < 0 or end < start or start >= file_size:
        return "invalid"
    return (start, end)


def _is_allowed_origin(origin: str) -> bool:
    raw = str(origin or "").strip()
    if not raw:
        return True
    if raw in ("capacitor://localhost", "ionic://localhost"):
        return True
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower().rstrip(".")
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return False


def _parse_num(val: Any, default: Any, lo: float, hi: float, *, as_int: bool = False) -> Any:
    if val is None or val == "":
        return default
    try:
        n = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    clamped = max(lo, min(hi, n))
    return int(round(clamped)) if as_int else clamped


_VOICE_AUDITIONS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlas Voice Auditions — Armory Hub</title>
<style>
  :root{--bg:#0b1220;--card:#111b2e;--card2:#0f172a;--line:#23324d;--fg:#e2e8f0;--mut:#94a3b8;--accent:#38bdf8;--ok:#22c55e;--amber:#f59e0b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:18px}
  .wrap{max-width:1120px;margin:0 auto}
  .top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  h1{margin:0;font-size:22px;letter-spacing:-.01em}
  .sub{color:var(--mut);font-size:13px;margin-top:2px}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;background:#1e293b;border:1px solid var(--line);color:#cbd5e1}
  .pill.ok{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.5);color:#86efac}
  .pill.pro{background:rgba(56,189,248,.14);border-color:rgba(56,189,248,.45);color:#7dd3fc}
  .bar{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:18px}
  .bar .grow{flex:1 1 320px}
  label{display:block;font-size:11px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
  input[type=text]{width:100%;background:#09101d;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:9px 11px;font:14px/1.2 inherit}
  .sec-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:18px 0 10px}
  .sec-title h2{margin:0;font-size:16px}
  .sec-title span{font-size:12px;color:var(--mut)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}
  .vcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 14px;display:flex;flex-direction:column;gap:8px;transition:border-color .15s,box-shadow .15s}
  .vcard.active{border-color:var(--ok);box-shadow:0 0 0 1px rgba(34,197,94,.35)}
  .vcard.playing{border-color:var(--accent);box-shadow:0 0 0 1px rgba(56,189,248,.45)}
  .vhead{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .vname{font-size:15px;font-weight:800}
  .vmeta{font-size:11px;color:var(--mut);display:flex;gap:8px;flex-wrap:wrap}
  .vvibe{font-size:13px;color:#cbd5e1;min-height:36px}
  .vbtns{display:flex;gap:7px;flex-wrap:wrap;margin-top:2px}
  .btn{border:1px solid var(--line);background:#1b2942;color:var(--fg);border-radius:8px;padding:7px 11px;font:600 12px/1.1 inherit;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
  .btn:hover{background:#243554}
  .btn.pri{background:#0284c7;border-color:#0ea5e9;color:#fff}
  .btn.pri:hover{background:#0369a1}
  .btn.ok{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.55);color:#86efac}
  .btn:disabled{opacity:.55;cursor:wait}
  #statusMsg{font-size:12px;color:var(--mut);min-height:18px}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <h1>🎧 Atlas Voice Audition Booth</h1>
      <div class="sub">Compare instant local GPU Kokoro voices vs. ElevenLabs Flash v2.5 Studio HD voices — tap any sample, then set your favorite live.</div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span id="activeBadge" class="pill ok">Loading…</span>
      <a class="btn" href="/#audio">← Back to Audio &amp; Voice</a>
    </div>
  </div>

  <div class="bar">
    <div class="grow">
      <label for="phraseIn">Audition Line (default phrase plays instantaneously from pre-rendered WAVs)</label>
      <input id="phraseIn" type="text" value="Atlas online. 225 for 8, locked in. Recording Rack Cam. App not connected.">
    </div>
    <button class="btn" id="resetPhraseBtn" type="button">Reset Line</button>
    <button class="btn" id="stopBtn" type="button">⏹ Stop Audio</button>
  </div>
  <div id="statusMsg"></div>

  <div class="sec-title">
    <h2>⚡ Local GPU Instant Voices (Kokoro ONNX)</h2>
    <span>~80 ms generation · $0 forever · 100% offline on GTX 1650</span>
  </div>
  <div class="grid" id="localGrid"></div>

  <div class="sec-title">
    <h2>🎙️ ElevenLabs Flash v2.5 Studio HD Voices</h2>
    <span>~350 ms fresh · 0 ms on repeat phrases via OptiPlex WAV cache · Uses your live API key</span>
  </div>
  <div class="grid" id="elGrid"></div>
</div>
<audio id="player" preload="none"></audio>
<script>
let catalog = [];
let activeVoice = '';
let defaultText = '';
let playingSlug = '';
const player = document.getElementById('player');
const phraseIn = document.getElementById('phraseIn');
const statusMsg = document.getElementById('statusMsg');

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function updateActiveBadge() {
  const hit = catalog.find(v => v.id === activeVoice);
  const el = document.getElementById('activeBadge');
  el.textContent = hit ? `Live Voice: ${hit.name} (${hit.engine})` : `Live Voice: ${activeVoice || 'unknown'}`;
}

function renderGrids() {
  updateActiveBadge();
  const buildCard = (v) => {
    const isAct = v.id === activeVoice;
    const isPlay = v.slug === playingSlug;
    return `<div class="vcard ${isAct ? 'active' : ''} ${isPlay ? 'playing' : ''}" data-slug="${esc(v.slug)}">
      <div class="vhead">
        <div class="vname">${esc(v.name)}</div>
        <span class="pill ${isAct ? 'ok' : (v.tier === 'elevenlabs' ? 'pro' : '')}">${isAct ? 'LIVE ACTIVE' : esc(v.engine)}</span>
      </div>
      <div class="vmeta"><span>${esc(v.latency)}</span></div>
      <div class="vvibe">${esc(v.vibe)}</div>
      <div class="vbtns">
        <button class="btn pri" type="button" data-play="${esc(v.slug)}">${isPlay ? '🔊 Playing…' : '▶ Play Here'}</button>
        <button class="btn" type="button" data-gym="${esc(v.id)}">📢 Play in Gym</button>
        <button class="btn ${isAct ? 'ok' : ''}" type="button" data-use="${esc(v.id)}">${isAct ? '✓ Active' : 'Use This Voice'}</button>
      </div>
    </div>`;
  };
  document.getElementById('localGrid').innerHTML = catalog.filter(v => v.tier === 'local').map(buildCard).join('');
  const elVoices = catalog.filter(v => v.tier === 'elevenlabs');
  document.getElementById('elGrid').innerHTML = elVoices.length
    ? elVoices.map(buildCard).join('')
    : '<div style="color:var(--mut);font-size:13px">Add an ElevenLabs API key in Show Lab to unlock Studio HD voices.</div>';
}

async function playSample(slug) {
  const v = catalog.find(x => x.slug === slug);
  if (!v) return;
  const txt = phraseIn.value.trim();
  const qs = txt && txt !== defaultText ? `?text=${encodeURIComponent(txt)}&t=${Date.now()}` : '';
  playingSlug = slug;
  statusMsg.textContent = `Playing ${v.name} (${v.engine})…`;
  renderGrids();
  try {
    player.pause();
    player.src = `${v.url}${qs}`;
    await player.play();
  } catch (err) {
    playingSlug = '';
    statusMsg.textContent = `Playback error for ${v.name}: ${err && err.message ? err.message : err}`;
    renderGrids();
  }
}

async function playInGym(voiceId, btn) {
  const v = catalog.find(x => x.id === voiceId);
  const txt = phraseIn.value.trim() || defaultText;
  if (btn) btn.disabled = true;
  statusMsg.textContent = `Playing ${v ? v.name : voiceId} on gym speakers…`;
  try {
    const r = await fetch('/api/voice/auditions/play-gym', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ voiceId, text: txt }),
    }).then(x => x.json());
    statusMsg.textContent = r.ok ? `✓ Played ${v ? v.name : voiceId} in gym.` : `✗ ${r.error || 'Gym playback unavailable'}`;
  } catch {
    statusMsg.textContent = '✗ Gym playback request failed.';
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function setLiveVoice(voiceId, btn) {
  const v = catalog.find(x => x.id === voiceId);
  if (btn) btn.disabled = true;
  statusMsg.textContent = `Setting live voice to ${v ? v.name : voiceId}…`;
  try {
    const r = await fetch('/api/voice/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ttsVoice: voiceId }),
    }).then(x => x.json());
    if (r && r.ok) {
      activeVoice = (r.config && r.config.ttsVoice) || voiceId;
      statusMsg.textContent = `✓ Live Room Voice set to ${v ? v.name : voiceId}!`;
      renderGrids();
    } else {
      statusMsg.textContent = '✗ Could not save voice.';
    }
  } catch {
    statusMsg.textContent = '✗ Failed updating voice.';
  } finally {
    if (btn) btn.disabled = false;
  }
}

player.addEventListener('ended', () => {
  playingSlug = '';
  statusMsg.textContent = '';
  renderGrids();
});
document.getElementById('stopBtn').onclick = () => {
  player.pause();
  player.currentTime = 0;
  playingSlug = '';
  statusMsg.textContent = 'Stopped.';
  renderGrids();
};
document.getElementById('resetPhraseBtn').onclick = () => {
  phraseIn.value = defaultText;
};
document.addEventListener('click', (ev) => {
  const playBtn = ev.target.closest('[data-play]');
  if (playBtn) return playSample(playBtn.dataset.play);
  const gymBtn = ev.target.closest('[data-gym]');
  if (gymBtn) return playInGym(gymBtn.dataset.gym, gymBtn);
  const useBtn = ev.target.closest('[data-use]');
  if (useBtn) return setLiveVoice(useBtn.dataset.use, useBtn);
});

(async () => {
  const r = await fetch('/api/voice/auditions').then(x => x.json());
  catalog = Array.isArray(r.voices) ? r.voices : [];
  activeVoice = r.activeVoice || '';
  defaultText = r.defaultText || phraseIn.value;
  phraseIn.value = defaultText;
  renderGrids();
})();
</script>
</body>
</html>
"""


def create_app(service: ArmoryCamService) -> Flask:
    app = Flask("armory-cam")
    app.config["MAX_CONTENT_LENGTH"] = 260 * 1024 * 1024

    @app.before_request
    def check_mutating_origin() -> Response | None:
        origin = (request.headers.get("Origin") or "").strip()
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and origin and not _is_allowed_origin(origin):
            return jsonify({"ok": False, "error": "Origin not allowed"}), 403
        return None

    @app.after_request
    def add_cors(resp: Response) -> Response:
        origin = (request.headers.get("Origin") or "").strip()
        if origin:
            if _is_allowed_origin(origin):
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
        else:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges, X-Stream-Start-Ms, X-Stream-End-Ms"
        return resp

    @app.errorhandler(Exception)
    def handle_app_exception(exc: Exception) -> tuple[Response, int] | Response:
        if isinstance(exc, HTTPException):
            code = int(exc.code or 500)
            return jsonify({"ok": False, "error": str(exc.description or exc.name)}), code
        service.log.exception("Unhandled HTTP route error: %s", exc)
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    @app.route("/<path:dummy>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def handle_options(dummy: str = "") -> Response:
        return Response(status=204)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status=204)

    @app.get("/")
    def index() -> Response:
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/api/ping")
    def api_ping() -> Response:
        with service.lock:
            cams = [c.name for c in service.cameras if c.enabled]
            rec = service.recording
            paused = service.paused
            ret_days = service.retention_days
        prev = service.preview_drive_info()
        vx = service.voice.status_dict()
        payload: dict[str, Any] = {
            "service": "armory-cam",
            "serverEpochMs": int(time.time() * 1000),
            "recording": rec,
            "paused": paused,
            "retentionDays": ret_days,
            "cameras": cams,
            "armedClip": service.active_server_armed_clip(),
            "voice": {
                "enabled": vx["enabled"],
                "ready": vx["ready"],
                "listening": vx["listening"],
                "device": vx["device"],
            },
        }
        if prev.get("driveUrl"):
            payload["previewDriveUrl"] = prev["driveUrl"]
        if prev.get("updatedMs") is not None:
            payload["previewUpdatedMs"] = prev["updatedMs"]
        return jsonify(payload)

    @app.get("/api/preview.jpg")
    def api_preview_jpg() -> Response:
        if not service.preview_local_path.is_file():
            service.refresh_drive_preview()
        if not service.preview_local_path.is_file():
            return jsonify({"ok": False, "error": "Preview unavailable"}), 503
        resp = Response(service.preview_local_path.read_bytes(), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/preview/refresh")
    def api_preview_refresh() -> Response:
        res = service.refresh_drive_preview()
        return jsonify(res), (200 if res.get("ok") else 503)

    @app.get("/api/bump/live")
    def api_bump_live() -> Response:
        cam_name = request.args.get("camera") or ""
        if not cam_name:
            return jsonify({"ok": False, "error": "Missing camera parameter"}), 400
        res = service.live_bump_alignment(cam_name)
        resp = jsonify(res)
        resp.headers["Cache-Control"] = "no-store"
        return resp, (200 if res.get("ok") else 404)

    @app.get("/api/bump/overlay.png")
    def api_bump_overlay_png() -> Response:
        cam_name = request.args.get("camera") or ""
        mode = request.args.get("mode") or "edges"
        color = request.args.get("color") or "cyan"
        if not cam_name:
            return jsonify({"ok": False, "error": "Missing camera parameter"}), 400
        png_bytes, err = service.get_bump_overlay_png(cam_name, mode=mode, color=color)
        if not png_bytes:
            return jsonify({"ok": False, "error": err or "Overlay unavailable"}), 404
        resp = Response(png_bytes, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/bump/baseline")
    def api_bump_baseline() -> Response:
        body = request.get_json(silent=True) or {}
        cam_name = str(body.get("camera") or "").strip() or None
        res = service.capture_bump_baseline(cam_name)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/bump/adapt_views")
    def api_bump_adapt_views() -> Response:
        body = request.get_json(silent=True) or {}
        cam_name = str(body.get("camera") or "").strip()
        if not cam_name:
            return jsonify({"ok": False, "error": "Missing camera parameter"}), 400
        res = service.adapt_views_to_current_framing(cam_name)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(service.status_snapshot())

    @app.get("/api/config")
    def api_get_config() -> Response:
        with service.lock:
            cams = [
                {
                    **c.to_dict(),
                    "connected": bool(service.cam_connected.get(service._source_slug_for(c), False)),
                    "latestEnergy": float(service.latest_energy.get(c.slug, 0.0)),
                }
                for c in service.cameras
            ]
            payload = {
                "port": service.port,
                "retentionDays": service.retention_days,
                "audioSyncOffsetMs": service.audio_sync_offset_ms,
                "preRollSec": service.pre_roll_sec,
                "driveBackupDir": service.drive_backup_dir,
                "bumpAlertNtfy": service.bump_alert_ntfy,
                "motionRecord247": service.motion_record_247,
                "cameras": cams,
                "wallboard": service.wallboard_cfg.to_dict(),
                "voice": service.voice_cfg.to_dict(),
                "houseAudio": service.house_audio_cfg.to_dict(),
                "displays": service.displays_cfg.to_dict(),
                "disk": service.disk_stats(),
            }
        return jsonify(payload)

    @app.post("/api/config")
    def api_post_config() -> Response:
        body = request.get_json(silent=True) or {}
        service.update_config(body)
        return jsonify({"ok": True})

    @app.post("/api/config/backup")
    def api_config_backup() -> Response:
        res = service.backup_optiplex_configs()
        return jsonify(res), (200 if res.get("ok") else 500)

    @app.post("/api/config/restore")
    def api_config_restore() -> Response:
        body = request.get_json(silent=True) or {}
        overwrite = bool(body.get("overwrite", False)) if isinstance(body, dict) else False
        res = service.restore_optiplex_configs(overwrite=overwrite)
        return jsonify(res), (200 if res.get("ok") else 404)

    @app.get("/wallboard")
    @app.get("/wallboard/")
    def wallboard_page() -> Response:
        resp = Response(service.wallboard.get_html(), mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/wallboard/state")
    def wallboard_state() -> Response:
        resp = Response(service.wallboard.current_state_json(), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/wallboard/events")
    def wallboard_events() -> Response:
        def generate():
            with service.wallboard.cond:
                service.wallboard.sse_clients += 1
                last_seq = service.wallboard.seq
            try:
                last_ymd = datetime.now().strftime("%Y-%m-%d")
                initial = service.wallboard.current_state_json()
                yield f"data: {initial}\n\n".encode("utf-8")
                while not service._stop_event.is_set():
                    payload = None
                    with service.wallboard.cond:
                        if service.wallboard.seq == last_seq:
                            service.wallboard.cond.wait(timeout=15.0)
                        if service.wallboard.seq != last_seq:
                            last_seq = service.wallboard.seq
                            payload = service.wallboard.latest_json or service.wallboard.current_state_json()
                    cur_ymd = datetime.now().strftime("%Y-%m-%d")
                    if payload is None and cur_ymd != last_ymd:
                        last_ymd = cur_ymd
                        if service.wallboard.mode == "standby":
                            payload = service.wallboard.current_state_json()
                    elif payload is not None:
                        last_ymd = cur_ymd
                    if payload:
                        yield f"data: {payload}\n\n".encode("utf-8")
                    else:
                        yield b": ping\n\n"
            finally:
                with service.wallboard.cond:
                    service.wallboard.sse_clients = max(0, service.wallboard.sse_clients - 1)

        resp = Response(generate(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.post("/wallboard/voice")
    def wallboard_voice() -> Response:
        body = request.get_json(silent=True) or {}
        transcript = str(body.get("transcript") or "").strip()
        if not transcript:
            return jsonify({"ok": False, "error": "Empty transcript"}), 400
        with service.wallboard.cond:
            target_base = service.wallboard.source_url
        if not target_base:
            return jsonify({"ok": False, "error": "No active Pixel wallboard source URL"}), 503
        req = urllib.request.Request(
            f"{target_base}/api/voice",
            data=json.dumps({"transcript": transcript}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw = resp.read()
                return Response(raw or b'{"ok":true}', status=resp.status, mimetype="application/json")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/api/wallboard/html")
    def api_wallboard_html() -> Response:
        body = request.get_json(silent=True) or {}
        html = body.get("html")
        if not isinstance(html, str) or not service.wallboard.save_uploaded_html(html):
            return jsonify({"ok": False, "error": "Invalid HTML"}), 400
        return jsonify({"ok": True})

    @app.post("/api/wallboard/state")
    def api_wallboard_state() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        workout_active = bool(body.get("workoutActive", False))
        source_url = str(body.get("sourceUrl") or "")
        service.wallboard.update_state(state_obj, workout_active=workout_active, source_url=source_url)
        service.voice.set_wallboard_active(bool(workout_active and isinstance(state_obj, dict) and state_obj.get("mode") == "lift"))
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        if workout_active:
            with service.adb.lock:
                service.adb.pending_sleep_at_ms = None
                service.adb.pending_sleep_reason = ""
                should_wake = (
                    cfg.enabled
                    and cfg.wake_on_start
                    and bool(service.adb.serial(cfg))
                    and not service.adb.tv_awake_requested
                    and not service.adb.manual_sleep_active
                )
            if should_wake:
                service.trigger_adb_wake_async(force=False)
        else:
            with service.adb.lock:
                service.adb.manual_sleep_active = False
        return jsonify({"ok": True, "armedClip": service.active_server_armed_clip()})

    @app.post("/api/wallboard/wake")
    def api_wallboard_wake() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        source_url = str(body.get("sourceUrl") or "")
        force = bool(body.get("force", False))
        sync_flag = bool(body.get("sync", False))
        service.wallboard.mark_active_wake(state_obj=state_obj, source_url=source_url)
        if isinstance(state_obj, dict):
            service.voice.set_wallboard_active(state_obj.get("mode") == "lift")
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
            url = service.effective_wallboard_url()
        with service.adb.lock:
            blocked_by_manual_sleep = bool(not force and service.adb.manual_sleep_active)
        if not (cfg.enabled and cfg.wake_on_start) and not force:
            adb_res: dict[str, Any] = {"ok": False, "skipped": True, "message": "Wake on workout start disabled"}
        elif blocked_by_manual_sleep:
            adb_res = {
                "ok": True,
                "skipped": True,
                "state": service.adb.adb_state,
                "message": "Wallboard manually closed — say 'open the gym' or finish workout to re-enable auto-wake",
            }
        elif sync_flag:
            adb_res = service.adb.wake_and_launch(cfg, url, force=force)
        else:
            service.trigger_adb_wake_async(force=force)
            adb_res = {"queued": True, "state": service.adb.adb_state, "message": "Wake queued on OptiPlex"}
        return jsonify(
            {
                "ok": True,
                "wallboardUrl": url,
                "fireTvHost": cfg.fire_tv_host,
                "adb": adb_res,
            }
        )

    @app.post("/api/wallboard/sleep")
    def api_wallboard_sleep() -> Response:
        body = request.get_json(silent=True) or {}
        state_obj = body.get("state") if isinstance(body.get("state"), dict) else None
        reason = str(body.get("reason") or "workout_finished")
        delay_sec = float(_parse_num(body.get("delaySec"), 0.0, 0.0, 86400.0))
        sync_flag = bool(body.get("sync", False))
        service.wallboard.update_state(state_obj, workout_active=False)
        service.voice.set_wallboard_active(False)
        if reason in ("workout_finished", "workout_end"):
            with service.adb.lock:
                service.adb.manual_sleep_active = False
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        if reason != "manual" and not cfg.sleep_on_stop:
            return jsonify(
                {
                    "ok": True,
                    "scheduledDelaySec": 0,
                    "adb": {"ok": False, "skipped": True, "message": "Auto-sleep disabled"},
                }
            )
        if delay_sec == 0 and sync_flag:
            adb_res = service.adb.sleep_now(cfg, reason=reason)
        elif delay_sec > 0:
            adb_res = service.adb.schedule_sleep(cfg, delay_sec=delay_sec, reason=reason)
        else:
            service.trigger_adb_sleep_async(reason=reason, delay_sec=0.0)
            adb_res = {"queued": True, "state": service.adb.adb_state, "message": "Sleep queued on OptiPlex"}
        return jsonify({"ok": True, "scheduledDelaySec": delay_sec, "adb": adb_res})

    @app.post("/api/wallboard/adb/test")
    def api_wallboard_adb_test() -> Response:
        with service.lock:
            cfg = WallboardConfig.from_dict(service.wallboard_cfg.to_dict())
        res = service.adb.connect_and_probe(cfg)
        return jsonify(res)

    @app.get("/api/voice/status")
    def api_voice_status() -> Response:
        resp = jsonify(service.voice.status_dict(include_devices=True))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/voice/events")
    def api_voice_events() -> Response:
        wake_arg = request.args.get("wake")
        if wake_arg is not None and str(wake_arg).strip():
            service.voice.set_context(wake_word=str(wake_arg).strip())
        after_raw = request.args.get("afterId")
        after_id = int(after_raw) if after_raw and after_raw.isdigit() else None
        resp = Response(service.voice.subscribe_sse(after_id=after_id), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.post("/api/voice/context")
    def api_voice_context() -> Response:
        body = request.get_json(silent=True) or {}
        active_arg = bool(body["active"]) if "active" in body else None
        wake_arg = str(body["wakeWord"]) if "wakeWord" in body else None
        bias_arg = body.get("biasPhrases") if isinstance(body.get("biasPhrases"), list) else None
        rb_arg = bool(body["readback"]) if "readback" in body else None
        st = service.voice.set_context(
            active=active_arg,
            wake_word=wake_arg,
            bias_phrases=bias_arg,
            readback=rb_arg,
        )
        return jsonify({"ok": True, "status": st})

    @app.post("/api/voice/ack")
    def api_voice_ack() -> Response:
        body = request.get_json(silent=True) or {}
        armed_arg = bool(body["armed"]) if "armed" in body and body["armed"] is not None else None
        speech_arg = str(body.get("speech") or "")
        text_arg = str(body.get("text") or "")
        kind_arg = str(body.get("kind") or "")
        commit_mode_arg = str(body.get("setCommitMode") or "").strip().lower() or None
        res = service.voice.handle_ack(
            armed=armed_arg,
            speech=speech_arg,
            text=text_arg,
            kind=kind_arg,
            set_commit_mode=commit_mode_arg,
        )
        return jsonify(res)

    @app.post("/api/voice/config")
    def api_voice_config() -> Response:
        body = request.get_json(silent=True) or {}
        with service.lock:
            service.voice_cfg = service.voice.update_config(body)
            service.save_config()
            cfg_dict = service.voice_cfg.to_dict()
        return jsonify({"ok": True, "config": cfg_dict, "status": service.voice.status_dict()})

    @app.post("/api/voice/test-listen")
    def api_voice_test_listen() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("active") is False:
            sec = 0.0
        else:
            sec = float(body.get("seconds", 60) or 0)
        st = service.voice.start_test_listen(sec)
        return jsonify({"ok": True, "status": st})

    @app.post("/api/voice/test-speak")
    def api_voice_test_speak() -> Response:
        body = request.get_json(silent=True) or {}
        text = str(body.get("text") or "Armory room mic ready").strip()
        spoke = service.voice.speak_confirmation(text)
        return jsonify({"ok": True, "spoke": spoke, "text": text})

    @app.get("/api/voice/wake-studio")
    def api_voice_wake_studio() -> Response:
        resp = jsonify({"ok": True, **service.wake_trainer.studio_state(service.voice)})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/voice/wake-studio/enroll")
    def api_voice_wake_studio_enroll() -> Response:
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "start").strip().lower()
        if action == "stop":
            enr = service.voice.stop_wake_enrollment()
            return jsonify({"ok": True, "enrollment": enr, "studio": service.wake_trainer.studio_state(service.voice)})
        entry = WakeWordTrainerManager.resolve_entry(str(body.get("phrase") or ""))
        if entry is None:
            return jsonify({"ok": False, "error": "Unsupported wake word"}), 400
        target_reps = int(_parse_num(body.get("targetReps"), 20, 1, 60))
        enr = service.voice.start_wake_enrollment(entry["phrase"], target_reps=target_reps)
        return jsonify({"ok": True, "enrollment": enr, "studio": service.wake_trainer.studio_state(service.voice)})

    @app.get("/api/voice/wake-studio/clip/<slug>/<filename>")
    def api_voice_wake_studio_clip_get(slug: str, filename: str) -> Response:
        entry = WakeWordTrainerManager.resolve_entry(slug)
        if entry is None or not _WAKE_CLIP_NAME_RE.match(str(filename or "")):
            return Response(status=404)
        wav_p = service.wake_trainer.user_dir_for(entry["slug"]) / Path(filename).name
        if not wav_p.is_file():
            return Response(status=404)
        resp = Response(wav_p.read_bytes(), mimetype="audio/wav")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.delete("/api/voice/wake-studio/clip/<slug>/<filename>")
    def api_voice_wake_studio_clip_del(slug: str, filename: str) -> Response:
        if not service.wake_trainer.delete_clip(slug, filename):
            return jsonify({"ok": False, "error": "Clip not found"}), 404
        return jsonify({"ok": True, "studio": service.wake_trainer.studio_state(service.voice)})

    @app.post("/api/voice/wake-studio/clips/clear")
    def api_voice_wake_studio_clips_clear() -> Response:
        body = request.get_json(silent=True) or {}
        entry = WakeWordTrainerManager.resolve_entry(str(body.get("slug") or body.get("phrase") or ""))
        if entry is None:
            return jsonify({"ok": False, "error": "Unsupported wake word"}), 400
        removed = service.wake_trainer.clear_clips(entry["slug"])
        return jsonify({"ok": True, "removed": removed, "studio": service.wake_trainer.studio_state(service.voice)})

    @app.post("/api/voice/wake-studio/train")
    def api_voice_wake_studio_train() -> Response:
        body = request.get_json(silent=True) or {}
        phrase = str(body.get("phrase") or "").strip()
        force_full = bool(body.get("forceFull", False))
        ok, msg = service.wake_trainer.start_training(phrase, force_full=force_full, voice_svc=service.voice)
        if not ok:
            code = 409 if "Already training" in msg else 400
            return jsonify({"ok": False, "error": msg, "studio": service.wake_trainer.studio_state(service.voice)}), code
        return jsonify({"ok": True, "message": msg, "studio": service.wake_trainer.studio_state(service.voice)}), 202

    @app.post("/api/voice/wake-studio/restore-stock")
    def api_voice_wake_studio_restore() -> Response:
        body = request.get_json(silent=True) or {}
        phrase = str(body.get("phrase") or "hey jarvis").strip()
        ok, msg = service.wake_trainer.restore_stock(phrase, voice_svc=service.voice)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 400
        return jsonify({"ok": True, "message": msg, "studio": service.wake_trainer.studio_state(service.voice)})

    @app.post("/api/voice/wake-studio/activate")
    def api_voice_wake_studio_activate() -> Response:
        body = request.get_json(silent=True) or {}
        entry = WakeWordTrainerManager.resolve_entry(str(body.get("phrase") or ""))
        if entry is None:
            return jsonify({"ok": False, "error": "Unsupported wake word"}), 400
        with service.lock:
            service.voice_cfg = service.voice.update_config({"wakeWord": entry["phrase"], "wakeEngine": "openwakeword"})
            service.save_config()
            cfg_dict = service.voice_cfg.to_dict()
        service.voice.reload_oww_model(entry["phrase"])
        return jsonify({"ok": True, "config": cfg_dict, "studio": service.wake_trainer.studio_state(service.voice)})

    _AUDITION_DEFAULT_TEXT = "Atlas online. 225 for 8, locked in. Recording Rack Cam. App not connected."
    _AUDITION_BY_SLUG = {v["slug"]: v for v in ATLAS_VOICE_CATALOG}
    _AUDITION_BY_ID = {v["id"]: v for v in ATLAS_VOICE_CATALOG}

    @app.get("/voice-auditions")
    def voice_auditions_page() -> Response:
        resp = Response(_VOICE_AUDITIONS_HTML, mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/voice/auditions")
    def api_voice_auditions() -> Response:
        has_el = bool(service.voice._tts._load_elevenlabs_key())
        aud_dir = service.root / "voice_auditions"
        voices_out: list[dict[str, Any]] = []
        for item in ATLAS_VOICE_CATALOG:
            if item["tier"] == "elevenlabs" and not has_el:
                continue
            wav_p = aud_dir / f"{item['slug']}.wav"
            voices_out.append({
                **item,
                "cached": wav_p.is_file() and wav_p.stat().st_size > 1000,
                "url": f"/api/voice/auditions/{item['slug']}.wav",
            })
        resp = jsonify({
            "ok": True,
            "activeVoice": service.voice.cfg.tts_voice,
            "defaultText": _AUDITION_DEFAULT_TEXT,
            "hasElevenlabsKey": has_el,
            "voices": voices_out,
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/voice/auditions/<slug>.wav")
    def api_voice_audition_wav(slug: str) -> Response:
        meta = _AUDITION_BY_SLUG.get(str(slug or "").strip())
        if meta is None:
            return Response(status=404)
        custom_text = str(request.args.get("text") or "").strip()
        use_default = not custom_text or custom_text == _AUDITION_DEFAULT_TEXT
        aud_dir = service.root / "voice_auditions"
        wav_p = aud_dir / f"{meta['slug']}.wav"
        if use_default and wav_p.is_file() and wav_p.stat().st_size > 1000:
            resp = Response(wav_p.read_bytes(), mimetype="audio/wav")
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        phrase = custom_text if custom_text else _AUDITION_DEFAULT_TEXT
        rendered = service.voice.synthesize_tts_pcm(phrase, voice=meta["id"])
        if rendered is None:
            return jsonify({"ok": False, "error": f"Could not synthesize {meta['name']}"}), 500
        wav_bytes = encode_mono_pcm_to_wav(rendered[0], rendered[1])
        if use_default:
            try:
                aud_dir.mkdir(parents=True, exist_ok=True)
                tmp_p = wav_p.with_suffix(".wav.tmp")
                tmp_p.write_bytes(wav_bytes)
                tmp_p.replace(wav_p)
            except OSError:
                pass
        resp = Response(wav_bytes, mimetype="audio/wav")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/voice/auditions/play-gym")
    def api_voice_audition_play_gym() -> Response:
        body = request.get_json(silent=True) or {}
        voice_id = str(body.get("voiceId") or "").strip()
        if voice_id not in _AUDITION_BY_ID:
            return jsonify({"ok": False, "error": "Unknown voice"}), 400
        phrase = str(body.get("text") or _AUDITION_DEFAULT_TEXT).strip() or _AUDITION_DEFAULT_TEXT
        rendered = service.voice.synthesize_tts_pcm(phrase, voice=voice_id)
        if rendered is None:
            return jsonify({"ok": False, "error": "Synthesis failed"}), 500
        ok = service.voice._play_pcm_blocking(rendered[0], rendered[1], lead_ms=160, tail_ms=120, target_peak=0.84, kind="tts")
        return jsonify({"ok": bool(ok), "voiceId": voice_id})

    @app.post("/api/voice/inject")
    def api_voice_inject() -> Response:
        body = request.get_json(silent=True) or {}
        transcript = str(body.get("transcript") or "").strip()
        if not transcript:
            return jsonify({"ok": False, "error": "Empty transcript"}), 400
        entry = service.voice.inject_transcript(transcript)
        return jsonify({"ok": True, "entry": entry})

    @app.get("/api/voice/ab")
    def api_voice_ab() -> Response:
        resp = jsonify(service.voice.ab.snapshot_dict())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/voice/ab/sample")
    def api_voice_ab_sample() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid sample"}), 400
        with service.voice.lock:
            raw_recent = list(service.voice.recent_transcripts)
        trial, summary = service.voice.ab.record_sample(body, raw_room_recent=raw_recent)
        return jsonify({"ok": True, "trial": trial, "summary": summary})

    @app.post("/api/voice/ab/resolve")
    def api_voice_ab_resolve() -> Response:
        body = request.get_json(silent=True) or {}
        tid = str(body.get("trialId") or "").strip()
        winner = body.get("winner")
        summary = service.voice.ab.resolve_trial(tid, str(winner) if winner else None)
        if summary is None:
            return jsonify({"ok": False, "error": "Trial not found"}), 404
        return jsonify({"ok": True, "summary": summary})

    @app.post("/api/voice/ab/run")
    def api_voice_ab_run() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid run payload"}), 400
        run = service.voice.ab.save_run(body)
        return jsonify({"ok": True, "run": run, "runs": service.voice.ab.snapshot_dict()["runs"]})

    @app.post("/api/voice/ab/reset")
    def api_voice_ab_reset() -> Response:
        body = request.get_json(silent=True) or {}
        clear_runs = bool(body.get("clearRuns", False))
        summary = service.voice.ab.reset(clear_runs=clear_runs)
        return jsonify({"ok": True, "summary": summary})

    @app.get("/api/audio/status")
    def api_audio_status() -> Response:
        fresh = request.args.get("fresh") in ("1", "true", "yes")
        resp = jsonify(service.house_audio.status_dict(live_probe=True, force_probe=fresh))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/audio/config")
    def api_audio_config() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid config payload"}), 400
        with service.lock:
            service.house_audio_cfg = service.house_audio.update_config(body)
            service.save_config()
            cfg_dict = service.house_audio_cfg.to_dict()
        service.house_audio.invalidate_probe_cache()
        return jsonify({"ok": True, "config": cfg_dict, "status": service.house_audio.status_dict(live_probe=True, force_probe=True)})

    @app.post("/api/audio/denon/test-duck")
    def api_audio_denon_test_duck() -> Response:
        body = request.get_json(silent=True) or {}
        hold_sec = float(_parse_num(body.get("holdSec"), 1.2, 0.3, 5.0))
        drop_raw = body.get("dropDb")
        drop_db = _parse_num(drop_raw, None, 1.0, 30.0) if drop_raw is not None else None
        stage = "listen" if str(body.get("stage") or "").strip().lower() == "listen" else "output"
        force_flag = bool(body.get("force", True))
        with service.house_audio.denon.duck_context(drop_db=drop_db, force=force_flag, stage=stage) as duck_info:
            if duck_info.get("ducked"):
                service.house_audio.denon._sleep(hold_sec)
        service.house_audio.invalidate_probe_cache()
        return jsonify({
            "ok": bool(duck_info.get("ducked")),
            "stage": stage,
            "duck": duck_info,
            "denon": service.house_audio.denon.get_status(),
        })

    @app.post("/api/audio/cube/probe")
    def api_audio_cube_probe() -> Response:
        body = request.get_json(silent=True) or {}
        cfg = service.house_audio.get_config()
        host = str(body.get("host") or cfg.cube_host or "").strip()
        adb_port = int(_parse_num(body.get("adbPort"), cfg.cube_adb_port, 1, 65535, as_int=True))
        res = service.house_audio.cube.adb_probe_and_audit(service.adb, host, adb_port, cfg.cube_port)
        return jsonify(res)

    @app.post("/api/audio/cube/revive")
    def api_audio_cube_revive() -> Response:
        body = request.get_json(silent=True) or {}
        cfg = service.house_audio.get_config()
        host = str(body.get("host") or cfg.cube_host or "").strip()
        adb_port = int(_parse_num(body.get("adbPort"), cfg.cube_adb_port, 1, 65535, as_int=True))
        res = service.house_audio.cube.adb_ensure_service(service.adb, host, adb_port, cfg.cube_port)
        service.house_audio.invalidate_probe_cache()
        return jsonify(res)

    @app.post("/api/audio/test-play")
    def api_audio_test_play() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid payload"}), 400
        res = service.play_house_audio_test(body)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/audio/stop")
    def api_audio_stop() -> Response:
        cfg = service.house_audio.get_config()
        res = service.house_audio.cube.stop(cfg.cube_host, cfg.cube_port) if cfg.cube_host else {"ok": True, "stopped": False}
        service.voice.stop_s330_playback()
        return jsonify(res)

    @app.get("/api/audio/sfx/<path:filename>")
    def api_audio_sfx_file(filename: str) -> Response:
        valid = validate_sfx_filename(filename)
        if not valid or service.house_audio.sfx_dir is None:
            return Response(status=400)
        base = service.house_audio.sfx_dir.resolve()
        target = (base / valid).resolve()
        if target.parent != base:
            return Response(status=400)
        if not target.is_file():
            return Response(status=404)
        mime = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
        }.get(target.suffix.lower(), "application/octet-stream")
        resp = Response(target.read_bytes(), mimetype=mime)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/audio/matrix")
    def api_audio_get_matrix() -> Response:
        resp = jsonify({"ok": True, "matrix": service.house_audio.matrix.snapshot_dict()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/audio/matrix")
    def api_audio_post_matrix() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid matrix payload"}), 400
        if body.get("reset"):
            return jsonify({"ok": True, "matrix": service.house_audio.matrix.reset()})
        try:
            snap = service.house_audio.matrix.record(
                str(body.get("app") or ""),
                str(body.get("mode") or ""),
                status=str(body.get("status") or "untested"),
                behavior=str(body.get("behavior") or ""),
                notes=str(body.get("notes") or ""),
            )
            return jsonify({"ok": True, "matrix": snap})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/displays/status")
    def api_displays_status() -> Response:
        probe_raw = (request.args.get("probe") or "").strip().lower()
        if probe_raw == "cube":
            service.displays.probe_cube()
        elif probe_raw in ("primary", "tv"):
            service.displays.probe_primary()
        elif probe_raw in ("1", "true", "yes"):
            service.displays.probe_primary()
            service.displays.probe_cube()
        resp = jsonify(service.displays.status_dict(live_probe=(probe_raw in ("1", "true", "yes"))))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/displays/config")
    def api_displays_config() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid display config"}), 400
        with service.lock:
            service.displays_cfg = service.displays.update_config(body)
            service.save_config()
            cfg_dict = service.displays_cfg.to_dict()
        return jsonify({"ok": True, "config": cfg_dict, "status": service.displays.status_dict(live_probe=False)})

    @app.post("/api/displays/action")
    def api_displays_action() -> Response:
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip()
        targets = body.get("targets")
        target = str(body.get("target") or "").strip()
        if isinstance(targets, list) and targets:
            if not action:
                return jsonify({"ok": False, "error": "Provide action"}), 400
            res = service.displays.execute_many([str(t) for t in targets], action)
            service.house_audio.invalidate_probe_cache()
            return jsonify(res), (200 if res.get("ok") else 400)
        if not target or not action:
            return jsonify({"ok": False, "error": "Provide target and action"}), 400
        res = service.displays.execute(target, action)
        if target in ("avr", "cube", "gym", "open_gym", "close_gym", "spin", "music"):
            service.house_audio.invalidate_probe_cache()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/displays/discover-roku")
    def api_displays_discover_roku() -> Response:
        devices = service.displays.discover_roku_tvs()
        return jsonify({"ok": True, "devices": devices})

    @app.get("/stage")
    @app.get("/stage/")
    def stage_page() -> Response:
        resp = Response(STAGE_PAGE_HTML, mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/shows")
    def api_shows_list() -> Response:
        resp = jsonify({"ok": True, **service.shows.status_dict()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/shows/time")
    def api_shows_time() -> Response:
        resp = jsonify({"nowMs": int(time.time() * 1000)})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/shows/stage/ack")
    def api_shows_stage_ack() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid ACK payload"}), 400
        try:
            res = service.shows.record_stage_ack(
                role=str(body.get("role") or ""),
                run_id=int(body.get("runId") or 0),
                state=str(body.get("state") or ""),
            )
            return jsonify(res), (200 if res.get("ok") else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/shows/voices")
    def api_shows_voices() -> Response:
        resp = jsonify(service.shows.list_voices())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/shows/tts-providers", methods=["GET", "POST"])
    def api_shows_tts_providers() -> Response:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            try:
                status = service.shows.update_tts_secrets(body)
                service.trigger_optiplex_config_backup_async()
                return jsonify({"ok": True, **status, **service.shows.list_voices()})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        resp = jsonify({"ok": True, **service.shows.tts_secrets_status(), **service.shows.list_voices()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/shows/audition-voice")
    def api_shows_audition_voice() -> Response:
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid payload"}), 400
        try:
            rev_raw = body.get("voiceReverbWet")
            boost_raw = body.get("voiceBoostDb")
            wav_bytes, dur_sec = service.shows.audition_voice(
                text=str(body.get("text") or ""),
                voice=str(body.get("voice") or ""),
                voice_style=str(body.get("voiceStyle") or ""),
                reverb_wet=float(rev_raw) if rev_raw not in (None, "") else 0.06,
                boost_db=float(boost_raw) if boost_raw not in (None, "") else 2.0,
                with_fx=bool(body.get("withFx", False)),
                slot=str(body.get("slot") or "intro"),
            )
        except (ValueError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        resp = Response(wav_bytes, mimetype="audio/wav")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Armory-Duration-Sec"] = f"{dur_sec:.2f}"
        return resp

    @app.post("/api/shows/<show_id>/upload-voice")
    def api_show_upload_voice(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404
        slot = str(request.args.get("slot") or (request.form.get("slot") if request.form else "") or "intro").strip().lower()
        if "file" in request.files:
            up = request.files["file"]
            raw_name = str(up.filename or "voice.wav")
            file_bytes = up.read()
        else:
            raw_name = str(request.args.get("filename") or request.headers.get("X-Filename") or "voice.wav")
            file_bytes = request.get_data() or b""
        try:
            res = service.shows.save_uploaded_voice_clip(spec.id, slot, raw_name, file_bytes)
            return jsonify(res)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/api/shows/events")
    def api_shows_events() -> Response:
        role = str(request.args.get("role") or "dashboard").strip().lower() or "dashboard"
        q = service.shows.subscribe_events(role=role)

        def generate():
            try:
                init_ev = json.dumps({"type": "hello", **service.shows.list_voices()})
                yield f"data: {init_ev}\n\n"
                cur_stage = service.shows.current_stage_state()
                if cur_stage.get("type") in ("stage_prepare", "stage_play") and cur_stage.get("videoUrl"):
                    yield f"data: {json.dumps(cur_stage)}\n\n"
                elif cur_stage.get("type") == "stage_stop":
                    yield f"data: {json.dumps(cur_stage)}\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15.0)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                service.shows.unsubscribe_events(q, role=role)

        resp = Response(generate(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.get("/api/shows/<show_id>/preview.wav")
    def api_show_preview_wav(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404
        p_wav = service.shows.preview_wav_path(spec.id)
        if not p_wav.is_file() or p_wav.stat().st_size < 100:
            res = service.shows.build_show_preview(spec.id, force=True)
            if not res.get("ok") or not p_wav.is_file():
                return jsonify({"ok": False, "error": res.get("error") or "Preview build failed"}), 500
        resp = Response(p_wav.read_bytes(), mimetype="audio/wav")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/shows/<show_id>/overlay.wav")
    def api_show_overlay_wav(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None or not spec.overlay_text:
            return jsonify({"ok": False, "error": f"No overlay audio for show: {show_id}"}), 404
        o_wav = service.shows.overlay_wav_path(spec.id)
        if not o_wav.is_file() or o_wav.stat().st_size < 100:
            res = service.shows.build_show_overlay(spec.id, force=True)
            if not res.get("ok") or not o_wav.is_file():
                return jsonify({"ok": False, "error": res.get("error") or "Overlay build failed"}), 500
        resp = Response(o_wav.read_bytes(), mimetype="audio/wav")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/shows/<show_id>/show.mp4")
    def api_show_mp4(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404
        mp4 = service.shows.show_mp4_path(spec.id)
        if not mp4.is_file() or mp4.stat().st_size < 1000:
            res = service.shows.build_show_multimedia(spec.id, force=True)
            if not res.get("ok") or not mp4.is_file():
                return jsonify({"ok": False, "error": res.get("error") or "Show MP4 build failed"}), 500
        resp = _serve_mp4_from_root(service.shows.rendered_dir, f"{spec.id}_show.mp4")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/shows/<show_id>/rebuild")
    def api_show_rebuild(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404
        res = service.shows.build_show_preview(spec.id, force=True)
        if res.get("ok") and spec.three_screen_video and service.shows.ffmpeg_bin:
            res["multimedia"] = service.shows.build_show_multimedia(spec.id, force=True)
        return jsonify({**res, "status": service.shows.status_dict()}), (200 if res.get("ok") else 500)

    @app.post("/api/shows/<show_id>/fetch-video")
    def api_show_fetch_video(show_id: str) -> Response:
        res = service.shows.fetch_youtube_video_source(show_id)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/shows/<show_id>/upload-video")
    def api_show_upload_video(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404
        if "file" in request.files:
            up = request.files["file"]
            raw_name = str(up.filename or request.form.get("filename") or "entrance.mp4")
            file_bytes = up.read()
        else:
            raw_name = str(request.args.get("filename") or request.headers.get("X-Filename") or "")
            file_bytes = request.get_data() or b""
        try:
            res = service.shows.save_uploaded_video(spec.id, raw_name, file_bytes)
            return jsonify(res)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/shows/<show_id>/config")
    def api_show_config(show_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid show payload"}), 400
        rebuild = bool(body.get("rebuild", True))
        try:
            res = service.shows.update_show(show_id, body, rebuild=rebuild)
            service.trigger_optiplex_config_backup_async()
            return jsonify(res)
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/shows/<show_id>/upload")
    def api_show_upload(show_id: str) -> Response:
        spec = service.shows.get_show(show_id)
        if spec is None:
            return jsonify({"ok": False, "error": f"Unknown show: {show_id}"}), 404

        raw_name = ""
        file_bytes = b""
        song_start: float | None = None
        song_dur: float | None = None
        trim_sil: bool | None = None

        def _opt_float(val: Any) -> float | None:
            if val is None or val == "":
                return None
            return float(val)

        if "file" in request.files:
            up = request.files["file"]
            raw_name = str(up.filename or request.form.get("filename") or "track.mp3")
            file_bytes = up.read()
            song_start = _opt_float(request.form.get("songStartSec") or request.args.get("songStartSec"))
            song_dur = _opt_float(request.form.get("songDurationSec") or request.args.get("songDurationSec"))
            if "trimLeadingSilence" in request.form:
                trim_sil = str(request.form.get("trimLeadingSilence")).lower() not in ("0", "false", "no")
        else:
            raw_name = str(request.args.get("filename") or request.headers.get("X-Filename") or "")
            file_bytes = request.get_data() or b""
            song_start = _opt_float(request.args.get("songStartSec"))
            song_dur = _opt_float(request.args.get("songDurationSec"))
            if "trimLeadingSilence" in request.args:
                trim_sil = str(request.args.get("trimLeadingSilence")).lower() not in ("0", "false", "no")

        try:
            res = service.shows.save_uploaded_song(
                spec.id,
                raw_name,
                file_bytes,
                song_start_sec=song_start,
                song_duration_sec=song_dur,
                trim_leading_silence=trim_sil,
            )
            return jsonify(res)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/shows/<show_id>/fire")
    def api_show_fire(show_id: str) -> Response:
        body = request.get_json(silent=True)
        excl = bool(body["excludeMainTv"]) if isinstance(body, dict) and "excludeMainTv" in body else None
        ua = str(request.headers.get("User-Agent") or "").strip()[:80]
        ref = str(request.headers.get("Referer") or "").strip()[:60]
        caller = f"{request.remote_addr or '?'} | {ua}" + (f" | ref={ref}" if ref else "")
        res = service.shows.fire_show(show_id, async_run=True, exclude_main_tv=excl, source="http", caller=caller)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/shows/stop")
    def api_shows_stop() -> Response:
        res = service.shows.stop_active_show(restore_wallboard=True)
        return jsonify({**res, "status": service.shows.status_dict()})

    @app.post("/api/shows/fx-hooks")
    def api_shows_fx_hooks() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid FX payload"}), 400
        res = service.shows.update_fx_hooks(body)
        test_kind = str(body.get("test") or "").strip().lower()
        if test_kind == "lights_flash":
            service.shows._flash_lights(2, zone="all")
            res["tested"] = "lights_flash"
        elif test_kind == "lights_alt_flash":
            service.shows._flash_lights(4, zone="alternate")
            res["tested"] = "lights_alt_flash"
        elif test_kind in ("rack_on", "rack_off", "smith_on", "smith_off", "all_on", "all_off"):
            zone, state = test_kind.split("_", 1)
            want_on = state == "on"
            res["tested"] = test_kind
            res["hookResult"] = service.shows._fire_lights_state(
                want_on,
                zone=zone,
                tag=("lights_on" if want_on else "lights_off"),
                label=f"({zone.upper()} LIGHTS {'ON' if want_on else 'OFF'})",
            )
        elif test_kind == "fog":
            fog_url = service.shows.fx_hooks.resolve_fog_url()
            res["tested"] = "fog"
            res["hookResult"] = service.shows._fire_http_hook(fog_url) if fog_url else {"ok": False, "error": "Fog IP/URL not set"}
        elif test_kind == "theater_scene":
            test_sc = str(body.get("testScene") or "jenna").strip().lower()
            t_url = service.shows.fx_hooks.resolve_theater_scene_url(test_sc, duration_ms=12000, fog_ms=0)
            res["tested"] = f"theater_scene:{test_sc}"
            res["hookResult"] = (
                service.shows._fire_http_hook(t_url)
                if t_url
                else {"ok": False, "skipped": True, "error": "Smart Fogger v3.1 IP not set"}
            )
        elif test_kind == "theater_stop":
            s_url = service.shows.fx_hooks.resolve_theater_stop_url()
            res["tested"] = "theater_stop"
            res["hookResult"] = (
                service.shows._fire_http_hook(s_url)
                if s_url
                else {"ok": False, "skipped": True, "error": "Smart Fogger v3.1 IP not set"}
            )
        service.trigger_optiplex_config_backup_async()
        return jsonify(res)

    @app.get("/api/clips")
    def api_clips() -> Response:
        limit = int(_parse_num(request.args.get("limit"), 30, 1, 200, as_int=True))
        return jsonify({"clips": service.recent_clips(limit)})

    @app.post("/api/clip/delete")
    def api_clip_delete() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid delete payload"}), 400
        res = service.delete_clip(
            lan_url=str(body.get("lanUrl") or ""),
            file_name=str(body.get("fileName") or ""),
        )
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.get("/api/snapshot")
    def api_snapshot() -> Response:
        cam = request.args.get("camera")
        raw = request.args.get("raw") in ("1", "true", "yes")
        hires = request.args.get("hires") in ("1", "true", "yes")
        rot_arg = request.args.get("rotate")
        tilt_arg = request.args.get("tilt")
        crop_arg = request.args.get("crop")
        defish_arg = request.args.get("defish")
        rot_ov: int | None = None
        if rot_arg is not None:
            rot_ov = normalize_rotate(rot_arg)
        tilt_ov: float | None = None
        if tilt_arg is not None:
            tilt_ov = normalize_tilt(tilt_arg)
        crop_ov: tuple[float, float, float, float] | None = None
        if crop_arg is not None:
            parts = [p.strip() for p in crop_arg.split(",") if p.strip()]
            if len(parts) == 4:
                try:
                    crop_ov = normalize_crop([float(p) for p in parts])
                except Exception:
                    crop_ov = None
        defish_ov: bool | None = None
        if defish_arg is not None:
            defish_ov = defish_arg in ("1", "true", "yes")
        service._last_camera_ui_mono = time.monotonic()
        jpg = service.get_snapshot_jpeg(
            cam,
            raw=raw,
            hires=hires,
            rotate_override=rot_ov,
            tilt_override=tilt_ov,
            crop_override=crop_ov,
            defish_override=defish_ov,
        )
        if not jpg:
            return Response(status=404)
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/stream")
    def api_stream() -> Response:
        cam_name = request.args.get("camera")
        cropped = request.args.get("cropped") in ("1", "true", "yes")
        raw = request.args.get("raw") in ("1", "true", "yes")
        with service.lock:
            cam = service._find_camera(cam_name)
            if not cam or not cam.enabled:
                return Response(status=404)
            media_slug = service._source_slug_for(cam)
            src_cam = service._source_camera(cam)
            if not src_cam.rtsp_url:
                return Response(status=404)
        slug = cam.slug
        ch = service.live_hub.get_channel(media_slug)
        service._last_camera_ui_mono = time.monotonic()

        def generate():
            last_seq = 0
            last_frame_at = time.time()
            while True:
                with ch.cond:
                    if ch.seq == last_seq:
                        ch.cond.wait(timeout=1.5)
                    if ch.stop_flag:
                        break
                    if ch.seq == last_seq:
                        if time.time() - last_frame_at > 12.0:
                            break
                        continue
                    last_seq = ch.seq
                    raw_jpg = ch.latest_jpeg
                last_frame_at = time.time()
                service._last_camera_ui_mono = time.monotonic()

                payload = raw_jpg
                if not raw:
                    with service.lock:
                        cur = service._find_camera(slug) or cam
                        rot = cur.rotate
                        tilt = cur.tilt
                        crop = cur.crop
                        defish = cur.defish
                    if rot != 0 or tilt != 0.0 or (cropped and (defish or crop != (0.0, 0.0, 1.0, 1.0))):
                        img = cv2.imdecode(np.frombuffer(raw_jpg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            img = apply_frame_transform(img, crop, rot, apply_crop=cropped, tilt=tilt, defish=bool(defish and cropped))
                            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
                            if ok:
                                payload = buf.tobytes()

                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(payload)).encode("ascii")
                    + b"\r\n\r\n"
                    + payload
                    + b"\r\n"
                )

        resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.post("/api/session/start")
    def api_session_start() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or f"sess_{int(time.time())}")
        name = str(body.get("name") or "")
        service.start_session(sid, name)
        return jsonify({"ok": True})

    @app.post("/api/session/pause")
    def api_session_pause() -> Response:
        body = request.get_json(silent=True) or {}
        service.pause_session(body.get("sessionId"))
        return jsonify({"ok": True})

    @app.post("/api/session/resume")
    def api_session_resume() -> Response:
        body = request.get_json(silent=True) or {}
        service.resume_session(body.get("sessionId"))
        return jsonify({"ok": True})

    @app.post("/api/session/stop")
    def api_session_stop() -> Response:
        body = request.get_json(silent=True) or {}
        service.stop_session(body.get("sessionId"), discard=bool(body.get("discard", False)))
        return jsonify({"ok": True})

    @app.get("/api/bursts")
    def api_bursts() -> Response:
        sid = request.args.get("sessionId", "")
        around_ms = _parse_num(request.args.get("aroundMs"), None, 0, 9_000_000_000_000, as_int=True)
        cam = request.args.get("camera")
        min_dur_sec = _parse_num(request.args.get("minDurSec"), None, 0.1, 3600.0)
        bursts = service.bursts_for_session(sid, around_ms=around_ms, camera=cam, min_dur_sec=min_dur_sec)
        return jsonify({"bursts": bursts})

    @app.get("/api/tapes")
    def api_tapes() -> Response:
        src = request.args.get("source", "all")
        resp = jsonify(service.list_tapes(source_filter=src))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/tape/timeline")
    def api_tape_timeline() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        src = request.args.get("source", "all")
        buckets = int(_parse_num(request.args.get("buckets"), 80, 10, 400, as_int=True))
        resp = jsonify(service.tape_timeline(cam, sid, buckets=buckets, source_filter=src))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/tape/rescan")
    def api_tape_rescan() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or "")
        src = str(body.get("source") or "all")
        force = bool(body.get("force", True))
        return jsonify(service.rescan_tapes(sid, force=force, source_filter=src))

    @app.get("/api/tape/pack")
    def api_tape_pack() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        src = request.args.get("source", "all")
        start_ms = _parse_num(request.args.get("startMs"), None, 0, 9_000_000_000_000, as_int=True)
        end_ms = _parse_num(request.args.get("endMs"), None, 0, 9_000_000_000_000, as_int=True)
        count = int(_parse_num(request.args.get("count"), 80, 1, 240, as_int=True))
        max_w = int(_parse_num(request.args.get("w"), DEFAULT_SCRUB_THUMB_W, 80, 1920, as_int=True))
        resp = jsonify(
            service.tape_frame_pack(
                cam,
                sid,
                start_ms=start_ms,
                end_ms=end_ms,
                count=count,
                max_w=max_w,
                source_filter=src,
            )
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/tape/frame")
    def api_tape_frame() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        src = request.args.get("source", "all")
        now_ms = int(time.time() * 1000)
        ts_ms = int(_parse_num(request.args.get("tsMs"), now_ms, 0, 9_000_000_000_000, as_int=True))
        max_w = int(_parse_num(request.args.get("w"), 480, 80, 1920, as_int=True))
        max_gap_ms = _parse_num(request.args.get("maxGapMs"), None, 500, 3_600_000, as_int=True)
        jpg = service.get_tape_frame_jpeg(cam, sid, ts_ms, max_w=max_w, max_gap_ms=max_gap_ms, source_filter=src)
        if not jpg:
            if max_gap_ms is not None:
                return Response(status=204)
            return Response(status=404)
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    @app.get("/api/tape/stream.mp4")
    def api_tape_stream() -> Response:
        sid = request.args.get("sessionId", "")
        cam = request.args.get("camera", "Rack Cam")
        src = request.args.get("source", "all")
        stream_id = request.args.get("streamId", "")
        now_ms = int(time.time() * 1000)
        start_ms = int(_parse_num(request.args.get("startMs"), now_ms - 10_000, 0, 9_000_000_000_000, as_int=True))
        max_sec = int(_parse_num(request.args.get("maxSec"), 600, 15, 1800, as_int=True))
        res = service.stream_tape_fmp4(cam, sid, start_ms, source_filter=src, max_sec=max_sec, stream_id=stream_id)
        if res is None:
            return jsonify({"ok": False, "gap": True, "error": "No recording at this timestamp"}), 404
        actual_s, actual_e, gen_fn = res
        resp = Response(gen_fn(), mimetype="video/mp4", direct_passthrough=True)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Accept-Ranges"] = "none"
        resp.headers["X-Stream-Start-Ms"] = str(actual_s)
        resp.headers["X-Stream-End-Ms"] = str(actual_e)
        return resp

    @app.post("/api/tape/stream/stop")
    def api_tape_stream_stop() -> Response:
        body = request.get_json(silent=True) or {}
        stream_id = str(body.get("streamId") or "")
        return jsonify({"ok": True, "stopped": service.stop_stream(stream_id)})

    @app.get("/api/tape/angles")
    def api_tape_angles() -> Response:
        sid = request.args.get("sessionId", "")
        s_raw = request.args.get("startMs")
        e_raw = request.args.get("endMs")
        try:
            if s_raw is None or s_raw == "":
                raise ValueError("missing startMs")
            start_ms = int(float(s_raw))
            end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else start_ms + 5000
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid startMs/endMs"}), 400
        cams = service.tape_angles_for_window(sid, start_ms, end_ms)
        with service.lock:
            ret_days = service.retention_days
        audio_avail = service.room_audio.has_audio(start_ms, end_ms)
        resp = jsonify({"ok": True, "retentionDays": ret_days, "cameras": cams, "audioAvailable": audio_avail})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/audio/availability")
    def api_audio_availability() -> Response:
        s_raw = request.args.get("startMs")
        e_raw = request.args.get("endMs")
        try:
            if s_raw is None or s_raw == "":
                raise ValueError("missing startMs")
            start_ms = int(float(s_raw))
            end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else start_ms + 5000
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid startMs/endMs"}), 400
        with service.lock:
            ret_days = service.retention_days
        resp = jsonify({
            "ok": True,
            "available": service.room_audio.has_audio(start_ms, end_ms),
            "source": "powerconf",
            "retentionDays": ret_days,
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/audio/clip")
    def api_audio_clip() -> Response:
        s_raw = request.args.get("startMs")
        e_raw = request.args.get("endMs")
        try:
            if s_raw is None or s_raw == "":
                raise ValueError("missing startMs")
            s_ms = int(float(s_raw))
            e_ms = min(s_ms + 300_000, max(s_ms + 500, int(float(e_raw)) if e_raw is not None and e_raw != "" else s_ms + 5000))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid startMs/endMs"}), 400
        fname = f"aud_{s_ms}_{e_ms}.m4a"
        out_path = service.previews_root / fname
        if not out_path.is_file() or out_path.stat().st_size < 128:
            if not service.room_audio.extract_window_m4a(s_ms, e_ms, out_path):
                return jsonify({"ok": False, "error": "Room audio unavailable for this timestamp"}), 404
        resp = _serve_mp4_from_root(service.previews_root, fname, mimetype="audio/mp4")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/tape/preview")
    def api_tape_preview() -> Response:
        body = request.get_json(silent=True) or {}
        sid = str(body.get("sessionId") or "")
        cam = str(body.get("camera") or "").strip()
        s_raw = body.get("startMs")
        e_raw = body.get("endMs")
        try:
            if not cam or s_raw is None or s_raw == "":
                raise ValueError("missing camera or startMs")
            start_ms = int(float(s_raw))
            end_ms = int(float(e_raw)) if e_raw is not None and e_raw != "" else start_ms + 5000
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid camera or startMs/endMs"}), 400
        src = str(body.get("source") or "all")
        prev = service.cut_preview(sid, start_ms, end_ms, cam, source_filter=src)
        if not prev:
            return jsonify({"ok": False, "error": "No alternate camera tape for this timestamp"}), 404
        return jsonify(prev)

    @app.post("/api/clip")
    def api_clip() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid clip payload"}), 400
        sid = str(body.get("sessionId") or "")
        ex_name = str(body.get("exName") or body.get("exId") or "lift")
        now_ms = int(time.time() * 1000)
        start_ms = int(_parse_num(body.get("startMs"), now_ms - 20_000, 0, 9_000_000_000_000, as_int=True))
        end_ms = int(_parse_num(body.get("endMs"), now_ms, 0, 9_000_000_000_000, as_int=True))
        cam_override = body.get("camera")
        cams_raw = body.get("cameras")
        cams_list = (
            [str(x).strip() for x in cams_raw if isinstance(x, str) and str(x).strip()]
            if isinstance(cams_raw, list)
            else []
        )
        if not cams_list and cam_override:
            s_cam = str(cam_override).strip()
            if s_cam:
                cams_list = [s_cam]
        include_audio = bool(body.get("includeAudio", False))
        src = str(body.get("source") or "all")
        clips = service.cut_clips_multi(
            session_id=sid,
            ex_name=ex_name,
            start_ms=start_ms,
            end_ms=end_ms,
            cameras=cams_list,
            include_audio=include_audio,
            source_filter=src,
        )
        if not clips:
            service.log.warning(
                "POST /api/clip 404 (no segments): sid=%s ex=%s cams=%s window=%s..%s src=%s",
                sid, ex_name, cams_list, start_ms, end_ms, src,
            )
            return jsonify({"ok": False, "error": "No matching video segments found"}), 404
        service.log.info(
            "POST /api/clip saved %d clip(s): sid=%s ex=%s files=%s",
            len(clips), sid, ex_name, [c.get("fileName") for c in clips],
        )
        service.clear_server_armed_clip()
        return jsonify({**clips[0], "clips": clips})

    @app.post("/api/clip/auto-pr")
    def api_clip_auto_pr() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid auto-PR payload"}), 400
        sid = str(body.get("sessionId") or "")
        ex_name = str(body.get("exName") or body.get("exId") or "PR")
        now_ms = int(time.time() * 1000)
        set_at_ms = int(_parse_num(body.get("setAtMs"), now_ms, 0, 9_000_000_000_000, as_int=True))
        prev_ms = _parse_num(body.get("prevSetAtMs"), None, 0, 9_000_000_000_000, as_int=True)
        cam_raw = body.get("camera")
        cam_override = str(cam_raw).strip() if cam_raw else None
        cams_raw = body.get("cameras")
        cams_list = (
            [str(x).strip() for x in cams_raw if isinstance(x, str) and str(x).strip()]
            if isinstance(cams_raw, list)
            else []
        )
        if not cams_list and cam_override:
            cams_list = [cam_override]

        samples = service.session_samples(sid)
        if cams_list:
            allowed = set(cams_list)
            sub_samples = [s for s in samples if s.get("camera") in allowed]
            if sub_samples:
                samples = sub_samples
        min_thresh = min((c.motion_threshold for c in service.cameras), default=DEFAULT_MOTION_THRESHOLD)
        single_cam = cams_list[0] if len(cams_list) == 1 else None
        bursts = cluster_motion_bursts(
            samples,
            threshold=min_thresh,
            camera=single_cam,
            resolve_overlap=(single_cam is None),
        )
        start_ms, end_ms, detected_cam = select_pr_window(bursts, set_at_ms=set_at_ms, prev_set_at_ms=prev_ms)
        include_audio = bool(body.get("includeAudio", False))
        target_cams = cams_list or ([detected_cam] if detected_cam else [])
        clips = service.cut_clips_multi(
            session_id=sid,
            ex_name=f"{ex_name}_PR",
            start_ms=start_ms,
            end_ms=end_ms,
            cameras=target_cams,
            include_audio=include_audio,
        )
        if not clips:
            return jsonify({"ok": False, "error": "No PR video segments found"}), 404
        return jsonify({**clips[0], "clips": clips})

    @app.post("/api/clip/audio")
    def api_clip_audio() -> Response:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Invalid clip audio payload"}), 400
        lan_url = str(body.get("lanUrl") or "")
        file_name = str(body.get("fileName") or "")
        include_audio = bool(body.get("includeAudio", False))
        start_ms = _parse_num(body.get("startMs"), None, 0, 9_000_000_000_000, as_int=True)
        end_ms = _parse_num(body.get("endMs"), None, 0, 9_000_000_000_000, as_int=True)
        payload, status_code = service.set_clip_audio(
            lan_url=lan_url,
            file_name=file_name,
            start_ms=start_ms,
            end_ms=end_ms,
            include_audio=include_audio,
        )
        return jsonify(payload), status_code

    def _serve_mp4_from_root(root: Path, relpath: str, mimetype: str = "video/mp4") -> Response:
        base = root.resolve()
        target = (base / relpath).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return Response(status=403)
        if not target.is_file():
            return Response(status=404)

        size = target.stat().st_size
        parsed = parse_http_range(request.headers.get("Range"), size)
        if parsed == "invalid":
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

        start, end = (0, size - 1) if parsed is None else parsed
        length = end - start + 1

        def generate():
            remaining = length
            with target.open("rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(65_536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        status = 200 if parsed is None else 206
        resp = Response(generate(), status=status, mimetype=mimetype, direct_passthrough=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        if parsed is not None:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return resp

    @app.get("/clips/<path:relpath>")
    def serve_clip(relpath: str) -> Response:
        return _serve_mp4_from_root(service.clips_root, relpath)

    @app.get("/previews/<path:relpath>")
    def serve_preview(relpath: str) -> Response:
        return _serve_mp4_from_root(service.previews_root, relpath)

    return app


_CAM_VOICE_TOK_RE = r"smith\s+wide|fly\s+machine|chest\s+fly|pec\s+deck|leg\s+machine|rack|smith|fly|leg"
_CAM_VOICE_MAP = {
    "rack": "Rack Cam",
    "smith": "Smith Cam",
    "smith wide": "Smith Wide",
    "fly": "Fly Machine Cam",
    "fly machine": "Fly Machine Cam",
    "chest fly": "Fly Machine Cam",
    "pec deck": "Fly Machine Cam",
    "leg": "Leg Machine Cam",
    "leg machine": "Leg Machine Cam",
}


def _map_camera_voice_tok(tok: str | None) -> str | None:
    clean = re.sub(r"\s+", " ", str(tok or "").strip().lower())
    return _CAM_VOICE_MAP.get(clean)


def parse_camera_voice_command(transcript: str, wake_word: str = "hey armory") -> dict[str, Any] | None:
    """Parse camera arm/stop/clip/done voice triggers for OptiPlex-hosted fallback and server timestamp latching."""
    tail = strip_wake_word(transcript, wake_word).strip().lower()
    tail = re.sub(r"[—–\-/,]+", " ", tail)
    tail = re.sub(r"['’]", "", tail)
    tail = re.sub(r"^[.,!?:;\s]+|[.,!?:;\s]+$", "", tail)
    tail = re.sub(r"\s+", " ", tail).strip()
    if not tail:
        return None
    tail = re.sub(r"^(?:please|uh|um|hey|ok|okay|alright|go\s+ahead(?:\s+and)?)\s+", "", tail)
    tail = re.sub(r"\s+(?:please|thanks|thank\s+you|now|alright|okay|ok|yeah|yep|lets\s+go)$", "", tail).strip()
    if not tail:
        return None

    m_stop = re.match(
        rf"^(?:stop|end|finish)\s+(?:recording|camera|cam|guest(?:\s+clip|\s+recording)?)(?:\s+(?:on\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?)?$",
        tail,
    )
    if m_stop:
        return {"action": "stop", "camera": _map_camera_voice_tok(m_stop.group(1))}

    m_guest_a = re.match(
        rf"^(?:guest\s+(?:record|camera|cam|clip)|(?:record|start|arm)\s+guest(?:\s+clip|\s+set|\s+cam(?:era)?)?)(?:\s+(?:on\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?)?$",
        tail,
    )
    if m_guest_a:
        return {"action": "arm", "guest": True, "camera": _map_camera_voice_tok(m_guest_a.group(1))}

    m_guest_b = re.match(rf"^guest\s+({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?$", tail)
    if m_guest_b:
        return {"action": "arm", "guest": True, "camera": _map_camera_voice_tok(m_guest_b.group(1))}

    m_arm_a = re.match(
        rf"^(?:({_CAM_VOICE_TOK_RE})\s+)?(?:camera|cam|(?:record|film)(?:\s+(?:me|this(?:\s+set)?|my\s+set|that|set))?|(?:start|arm)\s+(?:camera|recording))(?:\s+(?:on\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?)?$",
        tail,
    )
    if m_arm_a:
        cam = _map_camera_voice_tok(m_arm_a.group(1) or m_arm_a.group(2))
        return {"action": "arm", "guest": False, "camera": cam}

    m_arm_b = re.match(
        rf"^(?:start|arm|record|film|camera)\s+(?:on\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?$",
        tail,
    )
    if m_arm_b:
        return {"action": "arm", "guest": False, "camera": _map_camera_voice_tok(m_arm_b.group(1))}

    m_clip_a = re.match(
        rf"^(?:clip\s+that|save\s+clip|clip\s+last(?:\s+set)?|clip\s+set)(?:\s+(?:on\s+|from\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?)?$",
        tail,
    )
    if m_clip_a:
        return {"action": "clip_last", "camera": _map_camera_voice_tok(m_clip_a.group(1))}

    m_clip_b = re.match(rf"^clip\s+(?:on\s+|from\s+)?(?:the\s+)?({_CAM_VOICE_TOK_RE})(?:\s+cam(?:era)?)?$", tail)
    if m_clip_b:
        return {"action": "clip_last", "camera": _map_camera_voice_tok(m_clip_b.group(1))}

    if re.match(r"^(?:check|check\s+it|check\s+set|log|log\s+it|log\s+set|done|got\s+it|crushed\s+it|nailed\s+it)$", tail):
        return {"action": "commit_done", "camera": None}

    return None


def run_server(root_dir: Path | None = None, port_override: int | None = None) -> None:
    eff_root = Path(root_dir) if root_dir is not None else default_root_dir()
    logs_root = eff_root / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)
    log_file = logs_root / "server.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def _log_sys_exc(exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.critical("Uncaught exception", exc_info=(exc_type, exc_val, exc_tb))

    def _log_thread_exc(args: Any) -> None:
        logging.critical(
            "Uncaught thread exception in %s",
            getattr(getattr(args, "thread", None), "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_sys_exc
    threading.excepthook = _log_thread_exc

    svc = ArmoryCamService(root_dir=root_dir)
    port = port_override or svc.port
    app = create_app(svc)
    svc.start_background()
    server = make_server("0.0.0.0", port, app, threaded=True)

    def _request_shutdown(signum: int, _frame: Any) -> None:
        logging.info("Received signal %s; shutting down cleanly...", signum)
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig_obj = getattr(signal, sig_name, None)
        if sig_obj is not None:
            try:
                signal.signal(sig_obj, _request_shutdown)
            except (OSError, ValueError):
                pass

    logging.info("Armory Cam listening on http://0.0.0.0:%d (root=%s, ffmpeg=%s)", port, svc.root, svc.ffmpeg)
    try:
        server.serve_forever()
    finally:
        svc.stop_background()


if __name__ == "__main__":
    p_arg = int(os.environ["ARMORY_CAM_PORT"]) if os.environ.get("ARMORY_CAM_PORT") else None
    run_server(port_override=p_arg)
