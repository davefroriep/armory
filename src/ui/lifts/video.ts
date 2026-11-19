import { active } from '../../lifts/active';
import { gymCam, type GymCamBurst, type GymCamTapeFrame, type GymCamTimeline } from '../../lifts/gymCam';
import { driveUrlFor } from '../../lifts/logic';
import {
  extractMp4CreationMs, formatPixelFilename, inferPixelVideoLink, sortDeviceVideosNewestFirst,
} from '../../lifts/mp4';
import { shareImage } from '../../lifts/share';
import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
import {
  applyClipMarker, computeScrubWindow, findNearestFrameIndex, fmtRelativeToCheck, pickNearestBurstIndex, shortCamLabel,
} from '../../lifts/tape';
import {
  createTrimState, dragTrimHandle, fmtPreciseTime, FRAME_FPS, isTrimDirty, isTrimmed,
  jogTrimDelta, resolvePlayStart, roundMs, stepTrimFrames, type TrimFocus, type TrimState,
} from '../../lifts/trimmer';
import type { LiftSession, LiftSet, SessionEx, VideoMeta } from '../../lifts/types';
import { uid } from '../../lifts/types';
import {
  exportNativeVideo, getPlayableNativeVideoUrl, isNative, listNativeDeviceVideos,
  openNativeVideoUri, pickNativeDcimVideo, recordNativeVideo, type DeviceVideoEntry,
} from '../../native/service';
import { getSettings } from '../../store/settings';
import { esc } from '../dom';
import { confirmSheet, openSheet, promptSheet, toast } from './kit';

export { driveUrlFor };

export interface VideoTarget {
  session: LiftSession;
  se: SessionEx;
  set: LiftSet;
}

const VIDEO_EXT = /\.(mp4|mov|webm|mkv|m4v|3gp)$/i;
const sessionFiles = new Map<string, Blob>();
const MAX_LOCAL_CACHE_BYTES = 64 * 1024 * 1024;
const MAX_SESSION_BLOBS = 6;
const cacheSessionBlob = (id: string, blob: Blob): void => {
  if (sessionFiles.has(id)) sessionFiles.delete(id);
  sessionFiles.set(id, blob);
  while (sessionFiles.size > MAX_SESSION_BLOBS) {
    const oldest = sessionFiles.keys().next().value;
    if (oldest !== undefined) sessionFiles.delete(oldest);
    else break;
  }
};
const isMobileWeb = (): boolean => !isNative() && /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

const fmtEpochClock = (ms: number): string =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

const blobToBase64 = (blob: Blob): Promise<string> =>
  new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] ?? '');
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });

const startTimerForVideo = (s: LiftSession): void => {
  if (s.status === 'active') active.ensureStarted(s, true);
};

const resetTimerIfIdle = async (s: LiftSession | undefined): Promise<void> => {
  if (s?.status === 'active') await active.resetIfNoneLogged(s);
};

const pickMime = (): string => {
  const c = ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
  return c.find((m) => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) ?? '';
};

/** Poster frame (small JPEG data URL) + duration so lists don't have to decode or store large videos. */
async function probeBlob(blob: Blob): Promise<{ thumb: string; durationSec: number }> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const v = document.createElement('video');
    v.muted = true;
    v.playsInline = true;
    v.preload = 'metadata';
    let durationSec = 0;
    const done = (thumb: string): void => { URL.revokeObjectURL(url); resolve({ thumb, durationSec }); };
    const timer = setTimeout(() => done(''), 5000);
    v.onloadeddata = () => {
      if (Number.isFinite(v.duration) && v.duration > 0) durationSec = Math.round(v.duration);
      v.currentTime = Math.min(0.4, (v.duration || 1) / 2);
    };
    v.onseeked = () => {
      clearTimeout(timer);
      const c = document.createElement('canvas');
      const w = 200;
      c.width = w;
      c.height = Math.max(1, Math.round((w * (v.videoHeight || 9)) / (v.videoWidth || 16)));
      c.getContext('2d')!.drawImage(v, 0, 0, c.width, c.height);
      done(c.toDataURL('image/jpeg', 0.6));
    };
    v.onerror = () => { clearTimeout(timer); done(''); };
    v.src = url;
  });
}

async function storeBlobClip(t: VideoTarget, blob: Blob, mime: string, fileName?: string): Promise<VideoMeta> {
  void navigator.storage?.persist?.();
  startTimerForVideo(t.session);
  const { thumb, durationSec } = await probeBlob(blob);
  const meta: VideoMeta = {
    id: uid(), sessionId: t.session.id, exId: t.se.exId, setId: t.set.id, ms: Date.now(),
    mime: mime || blob.type || 'video/mp4', bytes: blob.size, thumb, source: 'phone',
    ...(fileName ? { fileName } : {}),
    ...(durationSec ? { durationSec } : {}),
  };
  await lifts.addVideo(meta, blob);
  t.set.vids = [...(t.set.vids ?? []), meta.id];
  lifts.saveSession(t.session, true);
  return meta;
}

async function storeLinkClip(t: VideoTarget, o: Partial<VideoMeta> & { fileName?: string }): Promise<VideoMeta> {
  startTimerForVideo(t.session);
  const meta: VideoMeta = {
    id: uid(),
    sessionId: t.session.id,
    exId: t.se.exId,
    setId: t.set.id,
    ms: Date.now(),
    mime: o.mime || 'video/mp4',
    bytes: o.bytes ?? 0,
    thumb: o.thumb ?? '',
    linkOnly: true,
    source: o.source ?? 'dcim',
    ...(o.fileName ? { fileName: o.fileName } : {}),
    ...(o.driveQuery ? { driveQuery: o.driveQuery } : {}),
    ...(o.fileMs ? { fileMs: o.fileMs } : {}),
    ...(o.durationSec ? { durationSec: o.durationSec } : {}),
    ...(o.uri ? { uri: o.uri } : {}),
  };
  await lifts.addVideo(meta);
  t.set.vids = [...(t.set.vids ?? []), meta.id];
  lifts.saveSession(t.session, true);
  return meta;
}

async function handlePickedFile(t: VideoTarget, f: File, mode: 'capture' | 'pick'): Promise<VideoMeta | null> {
  if (!(f.type.startsWith('video/') || VIDEO_EXT.test(f.name))) {
    toast('Pick a video file (.mp4, .mov, .webm)');
    await resetTimerIfIdle(t.session);
    return null;
  }
  startTimerForVideo(t.session);
  const { thumb, durationSec } = await probeBlob(f);
  if (mode === 'capture') {
    const startMs = Math.max(0, (f.lastModified || Date.now()) - durationSec * 1000);
    const fileName = /^PXL_/i.test(f.name) ? f.name : formatPixelFilename(startMs);
    const meta = await storeBlobClip(t, f, f.type, fileName);
    if (!meta.thumb && thumb) { meta.thumb = thumb; await lifts.saveVideoMeta(meta); }
    return meta;
  }
  const endMs = (await extractMp4CreationMs(f)) ?? (f.lastModified || Date.now());
  const { fileName, driveQuery } = inferPixelVideoLink(f.name, endMs, durationSec);
  const meta = await storeLinkClip(t, {
    fileName,
    driveQuery,
    bytes: f.size,
    fileMs: f.lastModified || endMs,
    mime: f.type || 'video/mp4',
    durationSec,
    thumb,
    source: 'dcim',
  });
  sessionFiles.set(meta.id, f);
  return meta;
}

const fmtVideoDuration = (sec: number): string => {
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, '0')}`;
};

const fmtVideoTimestamp = (ms: number): string => {
  if (!ms) return 'Recent video';
  const d = new Date(ms);
  const now = new Date();
  const ymd = (x: Date) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  const yest = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (ymd(d) === ymd(now)) return `Today · ${time}`;
  if (ymd(d) === ymd(yest)) return `Yesterday · ${time}`;
  const date = d.toLocaleDateString([], {
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  });
  return `${date} · ${time}`;
};

async function pickDcimFromSaf(t: VideoTarget): Promise<VideoMeta | null> {
  const p = await pickNativeDcimVideo();
  if (p?.error) {
    toast('Pick a video file (.mp4, .mov, .webm)');
    await resetTimerIfIdle(t.session);
    return null;
  }
  if (!p?.picked || !p.fileName) return null;
  return storeLinkClip(t, {
    fileName: p.fileName,
    bytes: p.bytes ?? 0,
    fileMs: p.fileMs,
    mime: p.mime || 'video/mp4',
    durationSec: p.durationSec,
    thumb: p.thumb ?? '',
    uri: p.uri,
    source: 'dcim',
  });
}

async function openNativeVideoPickerSheet(t: VideoTarget): Promise<VideoMeta | null> {
  const initial = await listNativeDeviceVideos(48);
  if (!initial?.granted) {
    return pickDcimFromSaf(t);
  }
  return new Promise((resolve) => {
    let resolved: VideoMeta | null = null;
    let done = false;
    let deferResolve = false;
    let loadingMore = false;
    let entries: DeviceVideoEntry[] = sortDeviceVideosNewestFirst(initial.videos);
    let hasMore = !!initial.hasMore;

    const renderBody = (): string => {
      const cards = entries.length
        ? `<div class="vpick-grid">${entries.map((v, idx) => `<button class="vpick-card" type="button" data-vidx="${idx}">
            <span class="vpick-th" style="${v.thumb ? `background-image:url(${v.thumb})` : ''}">
              ${v.durationSec ? `<em class="vpick-dur">${esc(fmtVideoDuration(v.durationSec))}</em>` : ''}
            </span>
            <span class="vpick-meta">
              <b>${esc(fmtVideoTimestamp(v.fileMs))}</b>
              <small>${esc([v.bucket, v.bytes ? `${(v.bytes / 1048576).toFixed(1)} MB` : ''].filter(Boolean).join(' · ') || v.fileName)}</small>
            </span>
          </button>`).join('')}</div>`
        : '<p class="sh-text" style="text-align:center;padding:18px 0">No device videos found.</p>';

      return `<h3 class="sh-title">Select Video</h3>
        <div class="vpick-sub">Newest first · Videos only</div>
        ${cards}
        <div class="vpick-foot">
          ${hasMore ? `<button class="btn" type="button" data-vmore ${loadingMore ? 'disabled' : ''}>${loadingMore ? 'Loading…' : 'Show older videos'}</button>` : ''}
          <button class="btn" type="button" data-vsaf>Browse system files…</button>
          <button class="btn" type="button" data-vcancel>Cancel</button>
        </div>`;
    };

    const finish = (meta: VideoMeta | null): void => {
      if (done) return;
      done = true;
      resolved = meta;
      sh.close();
      resolve(resolved);
    };

    const sh = openSheet(renderBody(), {
      full: true,
      onClose: () => {
        if (!done && !deferResolve) {
          done = true;
          resolve(resolved);
        }
      },
    });

    sh.body.addEventListener('click', async (e) => {
      const el = e.target as HTMLElement;
      const card = el.closest<HTMLElement>('[data-vidx]');
      if (card) {
        const entry = entries[Number(card.dataset.vidx)];
        if (!entry) return;
        card.setAttribute('disabled', 'disabled');
        const meta = await storeLinkClip(t, {
          fileName: entry.fileName,
          bytes: entry.bytes,
          fileMs: entry.fileMs || undefined,
          mime: entry.mime || 'video/mp4',
          durationSec: entry.durationSec || undefined,
          thumb: entry.thumb,
          uri: entry.uri,
          source: 'dcim',
        });
        finish(meta);
        return;
      }
      if (el.closest('[data-vmore]') && !loadingMore) {
        loadingMore = true;
        sh.body.innerHTML = renderBody();
        const next = await listNativeDeviceVideos(200);
        loadingMore = false;
        if (next?.granted) {
          entries = sortDeviceVideosNewestFirst(next.videos);
          hasMore = !!next.hasMore;
        }
        if (!done) sh.body.innerHTML = renderBody();
        return;
      }
      if (el.closest('[data-vsaf]')) {
        deferResolve = true;
        sh.close();
        const meta = await pickDcimFromSaf(t);
        finish(meta);
        return;
      }
      if (el.closest('[data-vcancel]')) {
        finish(null);
      }
    });
  });
}

/**
 * Pick a video from the phone (newest-first native video gallery on APK, or Video picker on Web)
 * preserving filename & Google Drive DCIM Backup link without copying the file into IndexedDB.
 */
export async function linkDcimVideo(t: VideoTarget): Promise<VideoMeta | null> {
  if (isNative()) {
    return openNativeVideoPickerSheet(t);
  }
  return new Promise((resolve) => {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'video/*';
    inp.onchange = async () => resolve(inp.files?.[0] ? await handlePickedFile(t, inp.files[0], 'pick') : null);
    inp.addEventListener('cancel', () => resolve(null));
    inp.click();
  });
}

/** Legacy alias for callers. */
export const attachVideo = (t: VideoTarget): Promise<VideoMeta | null> => linkDcimVideo(t);

/** Quick 1-tap chooser asking "Rack or Smith?" before arming Gym Cam on an unchecked set. */
export function pickRecordCameraSheet(
  exName: string,
  setLabel: string,
): Promise<{ kind: 'cam'; camera: string } | { kind: 'link' } | { kind: 'cancel' }> {
  const cams = gymCam.cameras.length ? gymCam.cameras : ['Rack Cam', 'Smith Cam'];
  return new Promise((resolve) => {
    let out: { kind: 'cam'; camera: string } | { kind: 'link' } | { kind: 'cancel' } = { kind: 'cancel' };
    const sh = openSheet(
      `<h3 class="sh-title">${esc(exName)} · ${esc(setLabel)}</h3>
       <div class="fl">WHICH CAMERA TO RECORD?</div>
       <div class="vcam-pick">
         ${cams.map((c) => `
           <button class="vcam-btn" type="button" data-rcam="${esc(c)}">
             <b>${esc(shortCamLabel(c))}</b>
             <small>Auto-stops when you tap ✓</small>
           </button>`).join('')}
       </div>
       <button class="btn big" type="button" data-rlink>Link Existing Tape / Phone Video…</button>
       <button class="btn big" type="button" data-rcancel>Cancel</button>`,
      { onClose: () => resolve(out) },
    );
    sh.body.addEventListener('click', (e) => {
      const el = e.target as HTMLElement;
      const camBtn = el.closest<HTMLElement>('[data-rcam]');
      if (camBtn) {
        out = { kind: 'cam', camera: camBtn.dataset.rcam! };
        sh.close();
        return;
      }
      if (el.closest('[data-rlink]')) {
        out = { kind: 'link' };
        sh.close();
        return;
      }
      if (el.closest('[data-rcancel]')) {
        sh.close();
      }
    });
  });
}

/**
 * Per-row video sheet: explicit Rack/Smith Gym-Cam linking (30s+ bursts + live tape scrubber),
 * linked clips, Record Video & Select Video.
 */
export async function openSetVideoSheet(t: VideoTarget, setLabel: string): Promise<void> {
  const ex = lifts.exOrStub(t.se.exId);
  const native = isNative();
  const mobile = isMobileWeb();
  const tapeAgeOk = Date.now() - (t.session.endMs ?? t.session.startMs) < 14 * 86_400_000;
  let camAvail = gymCam.online && tapeAgeOk;
  const sid = t.session.camSessionId ?? t.session.id;
  const checkedMs = (t.set.at ?? Date.now()) + gymCam.clockOffsetMs;

  let resolveClosed: () => void = () => {};
  const closed = new Promise<void>((res) => { resolveClosed = res; });

  const linkedRows = (): string => {
    const items = (t.set.vids ?? []).map((id) => lifts.videos.get(id)).filter((v): v is VideoMeta => !!v);
    if (!items.length) return '';
    return `<div class="fl">LINKED ON THIS SET</div>
      ${items.map((v) => `<div class="vlink-row">
        <button class="vlink-main" type="button" data-play="${v.id}">
          <span class="vlink-th" style="${v.thumb ? `background-image:url(${v.thumb})` : ''}"><i>▶</i></span>
          <span class="vlink-meta"><b>${esc(v.fileName || v.camera || 'Recorded clip')}</b><small>${v.camera ? `${esc(v.camera)} · ` : ''}${v.durationSec ? `${v.durationSec}s · ` : ''}${v.bytes ? `${(v.bytes / 1048576).toFixed(1)} MB · ` : ''}Tap for slow-mo</small></span>
        </button>
        <button class="vlink-rm" type="button" data-unlink="${v.id}" aria-label="Unlink video">✕</button>
      </div>`).join('')}`;
  };

  const camList = (): string[] => (gymCam.cameras.length ? gymCam.cameras : ['Rack Cam', 'Smith Cam']);

  const gymArmHtml = () => {
    if (!camAvail || t.session.status !== 'active' || t.set.done) return '';
    return camList().map((c) =>
      `<button class="aitem on" type="button" data-varmcam="${esc(c)}"><b>Record on ${esc(shortCamLabel(c))}</b><small>Arms this set and auto-stops when you tap ✓</small></button>`
    ).join('');
  };

  const actionsHtml = () => native
    ? `<div class="alist">
         ${gymArmHtml()}
         <button class="aitem" type="button" data-vact="cam"><b>Record Video</b><small>Phone camera</small></button>
         <button class="aitem" type="button" data-vact="pick"><b>Select Video</b><small>Newest phone videos</small></button>
       </div>`
    : mobile
      ? `<div class="alist">
           ${gymArmHtml()}
           <label class="aitem" data-vact="cam"><input type="file" accept="video/*" capture="environment" data-vinp="cam" hidden><b>Record Video</b></label>
           <label class="aitem" data-vact="pick"><input type="file" accept="video/*" data-vinp="pick" hidden><b>Select Video</b></label>
         </div>`
      : `<div class="alist">
           ${gymArmHtml()}
           <button class="aitem" type="button" data-vact="cam"><b>Record Video</b></button>
           <label class="aitem" data-vact="pick"><input type="file" accept="video/*" data-vinp="pick" hidden><b>Select Video</b></label>
         </div>`;

  let selectedCam: string | null = null;
  let viewMode: 'bursts' | 'scrub' = 'bursts';
  let loadingCam = false;
  let loadGen = 0;
  let autoScrubHint = '';

  let bursts: GymCamBurst[] = [];
  let burstIdx = 0;

  let timeline: GymCamTimeline | null = null;
  let minMs = 0;
  let maxMs = 0;
  let winCenterMs = 0;
  let cursorMs = 0;
  let inMs = 0;
  let outMs = 0;
  let zoomMin: number | null = 15;
  let dragScrub = false;

  let packFrames: GymCamTapeFrame[] = [];
  let packKey = '';
  let packLoading = false;
  let packAbort: AbortController | null = null;
  let lastPackIdx = -1;
  let activeFrameUrl = '';
  let scrubDirty = false;

  const clearPack = (): void => {
    packAbort?.abort();
    packAbort = null;
    packLoading = false;
    packKey = '';
    lastPackIdx = -1;
    activeFrameUrl = '';
    for (const f of packFrames) URL.revokeObjectURL(f.url);
    packFrames = [];
  };

  const cleanupResources = (): void => {
    clearPack();
  };

  const sh = openSheet(
    `<h3 class="sh-title">${esc(ex.name)} · ${esc(setLabel)}</h3>
     <div id="vlinked">${linkedRows()}</div>
     <div id="vgymcam"></div>
     <div id="vactions">${actionsHtml()}</div>
     <button class="btn big" type="button" data-vok>Done</button>`,
    {
      full: camAvail,
      onClose: () => {
        cleanupResources();
        resolveClosed();
      },
    },
  );

  const redrawLinked = (): void => { sh.body.querySelector('#vlinked')!.innerHTML = linkedRows(); };
  const redrawActions = (): void => { sh.body.querySelector('#vactions')!.innerHTML = actionsHtml(); };

  const currentWindow = (): { winStart: number; winEnd: number } =>
    computeScrubWindow(minMs, maxMs, winCenterMs || cursorMs, zoomMin);

  const paintScrubFrameLocal = (): void => {
    const img = sh.body.querySelector<HTMLImageElement>('#vscrubImg');
    const spin = sh.body.querySelector<HTMLElement>('#vscrubSpin');
    const idx = findNearestFrameIndex(packFrames, cursorMs);
    if (idx >= 0) {
      const url = packFrames[idx].url;
      activeFrameUrl = url;
      if (img && (lastPackIdx !== idx || img.getAttribute('src') !== url || img.hidden)) {
        lastPackIdx = idx;
        img.src = url;
        img.hidden = false;
      }
      if (spin) spin.hidden = true;
      return;
    }
    if (img && !activeFrameUrl) img.hidden = true;
    if (spin) {
      spin.hidden = false;
      spin.textContent = packLoading ? 'Loading local tape frames…' : 'No frames in window';
    }
  };

  const ensurePackForCurrentWindow = (): void => {
    if (!selectedCam || !minMs || !maxMs) return;
    const { winStart, winEnd } = currentWindow();
    const key = `${selectedCam}|${winStart}|${winEnd}`;
    if (packKey === key && (packFrames.length || packLoading)) {
      paintScrubFrameLocal();
      return;
    }
    packAbort?.abort();
    const ctrl = new AbortController();
    packAbort = ctrl;
    packKey = key;
    packLoading = true;
    paintScrubFrameLocal();
    const camReq = selectedCam;
    void gymCam.fetchTapePack(sid, camReq, winStart, winEnd, 80, ctrl.signal).then((frames) => {
      if (ctrl.signal.aborted || !sh.el.isConnected || camReq !== selectedCam || packKey !== key) {
        for (const f of frames) URL.revokeObjectURL(f.url);
        return;
      }
      for (const oldF of packFrames) URL.revokeObjectURL(oldF.url);
      packFrames = frames;
      packLoading = false;
      lastPackIdx = -1;
      paintScrubFrameLocal();
    });
  };

  const paintScrubOverlaysOnly = (): void => {
    const timePill = sh.body.querySelector<HTMLElement>('#vscrubTime');
    if (timePill) {
      const rel = t.set.at != null ? ` · ${fmtRelativeToCheck(cursorMs, checkedMs)}` : '';
      timePill.textContent = `${fmtEpochClock(cursorMs)}${rel}`;
    }
    const head = sh.body.querySelector<HTMLElement>('#vscrubHead');
    if (head && maxMs > minMs) {
      const { winStart, winEnd } = currentWindow();
      const pct = clamp(((cursorMs - winStart) / Math.max(1, winEnd - winStart)) * 100, 0, 100);
      head.style.left = `${pct.toFixed(2)}%`;
    }
  };

  const renderGymCam = (): void => {
    const box = sh.body.querySelector<HTMLElement>('#vgymcam');
    if (!box) return;
    if (!camAvail) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    const cams = camList();

    if (!selectedCam) {
      box.innerHTML = `
        <div class="fl">GYM CAM · RACK OR SMITH?</div>
        <div class="vcam-pick">
          ${cams.map((c) => `
            <button class="vcam-btn" type="button" data-pickcam="${esc(c)}">
              <b>${esc(shortCamLabel(c))}</b>
              <small>30s+ sets · Scrub tape</small>
            </button>`).join('')}
        </div>`;
      return;
    }

    const headHtml = `
      <div class="vcam-head">
        <div class="vcam-pills">
          ${cams.map((c) => `<button class="vcam-pill${c === selectedCam ? ' on' : ''}" type="button" data-pickcam="${esc(c)}">${esc(shortCamLabel(c))}</button>`).join('')}
        </div>
        <div class="vcam-tabs">
          <button class="vcam-tab${viewMode === 'bursts' ? ' on' : ''}" type="button" data-vmode="bursts">Bursts (30s+)${!loadingCam && bursts.length ? ` · ${bursts.length}` : ''}</button>
          <button class="vcam-tab${viewMode === 'scrub' ? ' on' : ''}" type="button" data-vmode="scrub">Scrub Tape</button>
        </div>
      </div>`;

    if (loadingCam) {
      box.innerHTML = `${headHtml}<div class="vburst-card"><div class="hint" style="padding:18px 4px;text-align:center">Loading ${esc(shortCamLabel(selectedCam))} tape &amp; 30s+ bursts…</div></div>`;
      return;
    }

    if (viewMode === 'bursts' && bursts.length) {
      const b = bursts[clamp(burstIdx, 0, bursts.length - 1)];
      const rel = t.set.at != null ? fmtRelativeToCheck(b.endMs, checkedMs) : '';
      box.innerHTML = `
        ${headHtml}
        <div class="vburst-card">
          ${b.thumb ? `<img class="vburst-img" src="${b.thumb}" alt="${esc(shortCamLabel(selectedCam))} burst preview">` : `<div class="vburst-img empty"><span>No preview thumbnail</span></div>`}
          <div class="vburst-bar">
            <div><b>Burst ${burstIdx + 1} of ${bursts.length} · ${b.durationSec}s</b>${rel ? `<span class="vburst-rel">${esc(rel)}</span>` : ''}</div>
            <small>${fmtEpochClock(b.startMs)} – ${fmtEpochClock(b.endMs)}</small>
          </div>
          ${bursts.length > 1 ? `<div class="vburst-nav">
            <button class="btn sm" type="button" data-bnav="-1" ${burstIdx <= 0 ? 'disabled' : ''}>◀ Older</button>
            <button class="btn sm" type="button" data-bnav="1" ${burstIdx >= bursts.length - 1 ? 'disabled' : ''}>Newer ▶</button>
          </div>` : ''}
          <button class="btn primary big" type="button" data-bsave>Save Clip (${b.durationSec}s) to ${esc(setLabel)}</button>
          <button class="btn sm" type="button" data-bscrub>Scrub / Adjust Window ▸</button>
        </div>`;
      return;
    }

    if (!minMs || !maxMs || (timeline?.segmentCount ?? 0) === 0) {
      box.innerHTML = `${headHtml}<div class="vburst-card"><div class="hint" style="padding:16px 4px;text-align:center">No recorded segments on ${esc(shortCamLabel(selectedCam))} for this session yet.</div></div>`;
      return;
    }

    const { winStart, winEnd } = currentWindow();
    const span = Math.max(1, winEnd - winStart);
    const curPct = clamp(((cursorMs - winStart) / span) * 100, 0, 100);
    const inPct = clamp(((inMs - winStart) / span) * 100, 0, 100);
    const outPct = clamp(((outMs - winStart) / span) * 100, 0, 100);
    const winLeft = Math.min(inPct, outPct);
    const winWidth = Math.max(inMs < winEnd && outMs > winStart ? 1 : 0, Math.abs(outPct - inPct));
    const chkInView = checkedMs >= winStart && checkedMs <= winEnd;
    const chkPct = clamp(((checkedMs - winStart) / span) * 100, 0, 100);

    const visActivity = (timeline?.activity ?? []).filter((pt) => pt.tMs >= winStart && pt.tMs <= winEnd);
    const maxEnergy = Math.max(4, ...visActivity.map((pt) => pt.e));
    const barsHtml = visActivity.map((pt) => {
      const leftPct = clamp(((pt.tMs - winStart) / span) * 100, 0, 100);
      const hPct = pt.e > 0.3 ? clamp((pt.e / maxEnergy) * 100, 10, 100) : 6;
      const hot = pt.e >= 2.2 ? ' hot' : '';
      return `<i class="vscrub-bar${hot}" style="left:${leftPct.toFixed(2)}%;height:${hPct.toFixed(0)}%"></i>`;
    }).join('');

    const rel = t.set.at != null ? ` · ${fmtRelativeToCheck(cursorMs, checkedMs)}` : '';
    const clipSec = Math.max(1, Math.round((outMs - inMs) / 1000));
    const totalMin = (maxMs - minMs) / 60_000;
    const initIdx = findNearestFrameIndex(packFrames, cursorMs);
    const initUrl = initIdx >= 0 ? packFrames[initIdx].url : '';
    lastPackIdx = initIdx;
    if (initUrl) activeFrameUrl = initUrl;

    box.innerHTML = `
      ${headHtml}
      ${autoScrubHint ? `<div class="hint" style="margin:2px 0 6px">${esc(autoScrubHint)}</div>` : ''}
      <div class="vburst-card vscrub-card">
        <div class="vscrub-stage">
          <img class="vscrub-img" id="vscrubImg" alt="${esc(shortCamLabel(selectedCam))} frame" ${initUrl ? `src="${initUrl}"` : 'hidden'}>
          <div class="vscrub-spin" id="vscrubSpin" ${initUrl ? 'hidden' : ''}>Loading local tape frames…</div>
          <div class="vscrub-ov l">${esc(shortCamLabel(selectedCam))}</div>
          <div class="vscrub-ov r" id="vscrubTime">${fmtEpochClock(cursorMs)}${esc(rel)}</div>
        </div>

        <div class="vscrub-zrow">
          <div class="vscrub-zpills">
            ${totalMin > 5 ? `<button class="vscrub-z${zoomMin === 5 ? ' on' : ''}" type="button" data-szoom="5">5m</button>` : ''}
            ${totalMin > 15 ? `<button class="vscrub-z${zoomMin === 15 ? ' on' : ''}" type="button" data-szoom="15">15m</button>` : ''}
            <button class="vscrub-z${zoomMin == null ? ' on' : ''}" type="button" data-szoom="all">All</button>
          </div>
          <button class="vscrub-z" type="button" data-sjmp="chk">Jump to ✓</button>
        </div>

        <div class="vscrub-rail" id="vscrubRail" data-srail>
          <div class="vscrub-bars">${barsHtml}</div>
          ${winWidth > 0 ? `<div class="vscrub-win" style="left:${winLeft.toFixed(2)}%;width:${winWidth.toFixed(2)}%"></div>` : ''}
          ${chkInView ? `<div class="vscrub-chk" style="left:${chkPct.toFixed(2)}%"><span>✓</span></div>` : ''}
          <div class="vscrub-head" id="vscrubHead" style="left:${curPct.toFixed(2)}%"></div>
        </div>

        <div class="vscrub-jumps">
          <button class="btn sm" type="button" data-sjmp="-60000">−1m</button>
          <button class="btn sm" type="button" data-sjmp="-15000">−15s</button>
          <button class="btn sm" type="button" data-sjmp="-5000">−5s</button>
          <button class="btn sm" type="button" data-sjmp="5000">+5s</button>
          <button class="btn sm" type="button" data-sjmp="15000">+15s</button>
          <button class="btn sm" type="button" data-sjmp="60000">+1m</button>
        </div>

        <div class="vscrub-marks">
          <button class="btn sm" type="button" data-smark="in"><b>Set Start (IN)</b><small>${fmtEpochClock(inMs)}</small></button>
          <button class="btn sm" type="button" data-smark="out"><b>Set End (OUT)</b><small>${fmtEpochClock(outMs)}</small></button>
        </div>

        <div class="vscrub-sum">Clip: <b>${fmtEpochClock(inMs)} → ${fmtEpochClock(outMs)}</b> · <b>${clipSec}s</b></div>
        <button class="btn primary big" type="button" data-ssave>Save Clip (${clipSec}s) to ${esc(setLabel)}</button>
      </div>`;

    ensurePackForCurrentWindow();
  };

  const selectCamera = async (camName: string): Promise<void> => {
    const gen = ++loadGen;
    selectedCam = camName;
    loadingCam = true;
    autoScrubHint = '';
    clearPack();
    scrubDirty = false;
    bursts = [];
    timeline = null;
    sh.body.classList.add('full');
    renderGymCam();

    const [bList, tl] = await Promise.all([
      gymCam.listBursts(sid, checkedMs, { camera: camName, minDurSec: 30 }),
      gymCam.getTapeTimeline(sid, camName),
    ]);
    if (gen !== loadGen || !sh.el.isConnected) return;

    bursts = bList;
    timeline = tl;
    minMs = tl?.minMs ?? 0;
    maxMs = tl?.maxMs ?? 0;
    const totalMin = minMs && maxMs ? (maxMs - minMs) / 60_000 : 0;
    zoomMin = totalMin > 15 ? 15 : null;

    if (bursts.length) {
      burstIdx = pickNearestBurstIndex(bursts, checkedMs);
      viewMode = 'bursts';
      const b = bursts[Math.max(0, burstIdx)];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      cursorMs = Math.round((inMs + outMs) / 2);
    } else {
      viewMode = 'scrub';
      if (minMs && maxMs) {
        outMs = clamp(checkedMs + 3000, Math.min(maxMs, minMs + 3000), maxMs);
        inMs = clamp(outMs - 45_000, minMs, Math.max(minMs, outMs - 3000));
        cursorMs = clamp(checkedMs - 20_000, minMs, maxMs);
        autoScrubHint = `No 30s+ bursts on ${shortCamLabel(camName)} — scrub tape directly`;
      }
    }
    winCenterMs = cursorMs;
    loadingCam = false;
    if (minMs && maxMs) ensurePackForCurrentWindow();
    renderGymCam();
  };

  renderGymCam();
  if (!camAvail && tapeAgeOk && getSettings().gymCamEnabled) {
    void gymCam.probe(500).then((r) => {
      if (!sh.el.isConnected || !r.online) return;
      camAvail = true;
      sh.body.classList.add('full');
      renderGymCam();
      redrawActions();
    });
  }

  // Timeline drag scrubbing (60 fps local RAM frame lookup — zero network traffic on drag)
  const applyRailPointer = (clientX: number): void => {
    const rail = sh.body.querySelector<HTMLElement>('#vscrubRail');
    if (!rail || !minMs || !maxMs) return;
    const rect = rail.getBoundingClientRect();
    const pct = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    const { winStart, winEnd } = currentWindow();
    cursorMs = clamp(Math.round(winStart + pct * (winEnd - winStart)), minMs, maxMs);
    paintScrubOverlaysOnly();
    paintScrubFrameLocal();
  };

  sh.body.addEventListener('pointerdown', (e) => {
    const el = e.target as HTMLElement;
    const rail = el.closest<HTMLElement>('[data-srail]');
    if (!rail) return;
    e.preventDefault();
    dragScrub = true;
    rail.setPointerCapture(e.pointerId);
    applyRailPointer(e.clientX);
  });
  sh.body.addEventListener('pointermove', (e) => {
    if (!dragScrub) return;
    applyRailPointer(e.clientX);
  });
  const endScrubDrag = (): void => {
    if (!dragScrub) return;
    dragScrub = false;
  };
  sh.body.addEventListener('pointerup', endScrubDrag);
  sh.body.addEventListener('pointercancel', endScrubDrag);

  sh.body.querySelectorAll<HTMLInputElement>('[data-vinp]').forEach((inp) => {
    const kind = inp.dataset.vinp as 'cam' | 'pick';
    if (kind === 'cam') inp.addEventListener('click', () => startTimerForVideo(t.session));
    inp.addEventListener('cancel', () => { if (kind === 'cam') void resetTimerIfIdle(t.session); });
    inp.addEventListener('change', async () => {
      const f = inp.files?.[0];
      if (!f) {
        if (kind === 'cam') await resetTimerIfIdle(t.session);
        return;
      }
      const meta = await handlePickedFile(t, f, kind === 'cam' ? 'capture' : 'pick');
      if (meta) {
        toast(kind === 'cam' ? 'Video recorded & linked' : `Linked ${meta.fileName ?? 'video'}`);
        sh.close();
      }
    });
  });

  sh.body.addEventListener('click', async (e) => {
    const el = e.target as HTMLElement;
    if (el instanceof HTMLInputElement) return;

    const play = el.closest<HTMLElement>('[data-play]');
    if (play) { await playVideo(play.dataset.play!, { target: t, setLabel }); redrawLinked(); return; }

    const unlink = el.closest<HTMLElement>('[data-unlink]');
    if (unlink) {
      await lifts.deleteVideo(unlink.dataset.unlink!);
      await resetTimerIfIdle(t.session);
      redrawLinked();
      toast('Video unlinked');
      return;
    }

    const pickCam = el.closest<HTMLElement>('[data-pickcam]');
    if (pickCam) {
      void selectCamera(pickCam.dataset.pickcam!);
      return;
    }

    const vmode = el.closest<HTMLElement>('[data-vmode]');
    if (vmode) {
      viewMode = vmode.dataset.vmode as 'bursts' | 'scrub';
      if (viewMode === 'scrub' && minMs && maxMs && !scrubDirty) {
        const b = bursts[burstIdx];
        if (b) {
          inMs = clamp(b.startMs, minMs, maxMs);
          outMs = clamp(b.endMs, inMs + 3000, maxMs);
          cursorMs = Math.round((inMs + outMs) / 2);
        }
        const { winStart, winEnd } = currentWindow();
        if (!winCenterMs || cursorMs < winStart || cursorMs > winEnd) winCenterMs = cursorMs;
      }
      renderGymCam();
      return;
    }

    const bnav = el.closest<HTMLElement>('[data-bnav]');
    if (bnav && bursts.length) {
      burstIdx = clamp(burstIdx + Number(bnav.dataset.bnav), 0, bursts.length - 1);
      const b = bursts[burstIdx];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      cursorMs = Math.round((inMs + outMs) / 2);
      renderGymCam();
      return;
    }

    if (el.closest('[data-bscrub]') && bursts[burstIdx]) {
      const b = bursts[burstIdx];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      cursorMs = Math.round((inMs + outMs) / 2);
      winCenterMs = cursorMs;
      scrubDirty = true;
      viewMode = 'scrub';
      renderGymCam();
      return;
    }

    const szoom = el.closest<HTMLElement>('[data-szoom]');
    if (szoom) {
      const raw = szoom.dataset.szoom!;
      zoomMin = raw === 'all' ? null : Number(raw);
      winCenterMs = cursorMs;
      lastPackIdx = -1;
      renderGymCam();
      return;
    }

    const sjmp = el.closest<HTMLElement>('[data-sjmp]');
    if (sjmp && minMs && maxMs) {
      const v = sjmp.dataset.sjmp!;
      cursorMs = v === 'chk' ? clamp(checkedMs, minMs, maxMs) : clamp(cursorMs + Number(v), minMs, maxMs);
      const { winStart, winEnd } = currentWindow();
      if (cursorMs >= winStart && cursorMs <= winEnd) {
        paintScrubOverlaysOnly();
        paintScrubFrameLocal();
      } else {
        winCenterMs = cursorMs;
        lastPackIdx = -1;
        renderGymCam();
      }
      return;
    }

    const smark = el.closest<HTMLElement>('[data-smark]');
    if (smark && minMs && maxMs) {
      scrubDirty = true;
      const side = smark.dataset.smark as 'in' | 'out';
      const next = applyClipMarker(inMs, outMs, cursorMs, side, minMs, maxMs);
      inMs = next.inMs;
      outMs = next.outMs;
      renderGymCam();
      return;
    }

    const bsave = el.closest<HTMLButtonElement>('[data-bsave]');
    if (bsave && bursts[burstIdx] && selectedCam) {
      const b = bursts[burstIdx];
      bsave.disabled = true;
      bsave.textContent = 'Saving…';
      const meta = await gymCam.saveWindowClip(t, selectedCam, b.startMs, b.endMs);
      if (meta) {
        toast('Clip saved & linked');
        sh.close();
      } else {
        bsave.disabled = false;
        bsave.textContent = `Save Clip (${b.durationSec}s) to ${setLabel}`;
        toast('Could not save clip from Gym Cam');
      }
      return;
    }

    const ssave = el.closest<HTMLButtonElement>('[data-ssave]');
    if (ssave && selectedCam && minMs && maxMs) {
      const dur = Math.max(1, Math.round((outMs - inMs) / 1000));
      ssave.disabled = true;
      ssave.textContent = 'Saving…';
      const meta = await gymCam.saveWindowClip(t, selectedCam, inMs, outMs);
      if (meta) {
        toast('Clip saved & linked');
        sh.close();
      } else {
        ssave.disabled = false;
        ssave.textContent = `Save Clip (${dur}s) to ${setLabel}`;
        toast('Could not save clip from Gym Cam');
      }
      return;
    }

    const warmCam = el.closest<HTMLElement>('[data-varmcam]')?.dataset.varmcam;
    if (warmCam) {
      startTimerForVideo(t.session);
      gymCam.armSet(t.session, t.se, t.set, warmCam);
      sh.close();
      toast(`Recording ${shortCamLabel(warmCam)} · ${setLabel} — tap ✓ or red camera to stop`);
      return;
    }

    const act = el.closest<HTMLElement>('[data-vact]')?.dataset.vact;
    if (act === 'gymarm') {
      startTimerForVideo(t.session);
      gymCam.armSet(t.session, t.se, t.set);
      sh.close();
      toast(`Recording ${setLabel} — tap ✓ or camera to stop`);
      return;
    }
    if (act === 'cam') {
      if (native) {
        startTimerForVideo(t.session);
        const p = await recordNativeVideo();
        if (p?.picked && p.uri && p.fileName) {
          const meta = await storeLinkClip(t, {
            fileName: p.fileName,
            bytes: p.bytes ?? 0,
            fileMs: p.fileMs,
            mime: p.mime || 'video/mp4',
            durationSec: p.durationSec,
            thumb: p.thumb ?? '',
            uri: p.uri,
            source: 'phone',
          });
          toast(`Video recorded & linked (${meta.fileName ?? 'clip'})`);
          sh.close();
        } else {
          await resetTimerIfIdle(t.session);
          if (p?.error) toast(p.error);
        }
      } else if (!mobile) {
        startTimerForVideo(t.session);
        sh.el.style.visibility = 'hidden';
        const meta = await recordVideo(t);
        sh.el.style.visibility = '';
        if (meta) { toast('Video recorded & linked'); sh.close(); }
        else await resetTimerIfIdle(t.session);
      }
    } else if (act === 'pick' && native) {
      const meta = await linkDcimVideo(t);
      if (meta) { toast(`Linked ${meta.fileName ?? 'video'}`); sh.close(); }
    } else if (el.closest('[data-vok]')) {
      sh.close();
    }
  });
  await closed;
}

/**
 * In-app camera: preview, countdown (time to step back to the bar), record, review, then link to the set.
 * Used as fallback on desktop browsers without native camera capture.
 */
export function recordVideo(t: VideoTarget): Promise<VideoMeta | null> {
  return new Promise((resolve) => {
    const ex = lifts.exOrStub(t.se.exId);
    const label = `${ex.name} · ${t.set.w != null ? fmtKg(t.set.w) : ''}${t.set.r != null ? ` × ${t.set.r}` : ''}`;
    const el = document.createElement('div');
    el.className = 'camera';
    el.innerHTML = `<video class="cam-v" autoplay muted playsinline></video>
      <div class="cam-top"><button class="cam-x" data-x aria-label="Close">✕</button><span>${esc(label)}</span><button class="cam-x" data-flip aria-label="Flip camera">⟲</button></div>
      <div class="cam-count" hidden></div>
      <div class="cam-msg" hidden></div>
      <div class="cam-bar"><button class="cam-rec" data-rec aria-label="Record"><i></i></button></div>
      <div class="cam-review" hidden><button class="btn" data-retake>Retake</button><button class="btn primary" data-save>Save to set</button></div>`;
    document.body.appendChild(el);
    const vid = el.querySelector<HTMLVideoElement>('.cam-v')!;
    const q = <T extends HTMLElement>(s: string): T => el.querySelector<T>(s)!;

    let stream: MediaStream | null = null;
    let rec: MediaRecorder | null = null;
    let chunks: Blob[] = [];
    let facing: 'environment' | 'user' = 'environment';
    let blob: Blob | null = null;
    let mime = '';
    let tick: ReturnType<typeof setInterval> | null = null;
    let stopTimer: ReturnType<typeof setTimeout> | null = null;
    let count: ReturnType<typeof setInterval> | null = null;
    let finished = false;

    const stopStream = (): void => { stream?.getTracks().forEach((x) => x.stop()); stream = null; };
    const end = (m: VideoMeta | null): void => {
      if (finished) return;
      finished = true;
      if (tick) clearInterval(tick);
      if (count) clearInterval(count);
      if (stopTimer) clearTimeout(stopTimer);
      stopStream();
      if (vid.src.startsWith('blob:')) URL.revokeObjectURL(vid.src);
      el.remove();
      resolve(m);
    };

    const msg = (m: string): void => { const b = q('.cam-msg'); b.textContent = m; b.hidden = !m; };

    const open = async (): Promise<void> => {
      stopStream();
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
        vid.srcObject = stream;
        msg('');
      } catch {
        msg('Camera unavailable. Allow camera access, or use Select Video.');
      }
    };

    const startRecording = (): void => {
      if (!stream) return;
      mime = pickMime();
      chunks = [];
      rec = new MediaRecorder(stream, { ...(mime ? { mimeType: mime } : {}), videoBitsPerSecond: 3_000_000 });
      rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      rec.onstop = () => {
        blob = new Blob(chunks, { type: mime || 'video/mp4' });
        stopStream();
        vid.srcObject = null;
        vid.muted = false;
        vid.controls = true;
        vid.loop = true;
        vid.src = URL.createObjectURL(blob);
        void vid.play().catch(() => {});
        q('.cam-bar').hidden = true;
        q('.cam-review').hidden = false;
      };
      rec.start(1000);
      el.classList.add('recording');
      const t0 = Date.now();
      const clockEl = q('.cam-count');
      clockEl.hidden = false;
      clockEl.classList.add('rec');
      tick = setInterval(() => { const s = Math.floor((Date.now() - t0) / 1000); clockEl.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }, 250);
      stopTimer = setTimeout(stopRecording, 120_000);
    };
    const stopRecording = (): void => {
      if (tick) clearInterval(tick);
      if (stopTimer) clearTimeout(stopTimer);
      el.classList.remove('recording');
      q('.cam-count').hidden = true;
      if (rec && rec.state !== 'inactive') rec.stop();
    };

    q('[data-rec]').addEventListener('click', () => {
      if (el.classList.contains('recording')) return stopRecording();
      if (count) return;
      const delay = liftSettings().videoDelay;
      if (!delay) return startRecording();
      let n = delay;
      const c = q('.cam-count');
      c.hidden = false;
      c.classList.remove('rec');
      c.textContent = String(n);
      count = setInterval(() => {
        n--;
        if (n <= 0) { clearInterval(count!); count = null; c.hidden = true; startRecording(); } else c.textContent = String(n);
      }, 1000);
    });
    q('[data-flip]').addEventListener('click', () => { if (!rec || rec.state === 'inactive') { facing = facing === 'environment' ? 'user' : 'environment'; void open(); } });
    q('[data-x]').addEventListener('click', async () => {
      if (blob && !(await confirmSheet('Discard this video?', '', 'Discard', true, 'Keep'))) return;
      end(null);
    });
    q('[data-retake]').addEventListener('click', () => {
      blob = null;
      if (vid.src.startsWith('blob:')) URL.revokeObjectURL(vid.src);
      vid.removeAttribute('src');
      vid.controls = false;
      vid.loop = false;
      vid.muted = true;
      q('.cam-bar').hidden = false;
      q('.cam-review').hidden = true;
      void open();
    });
    q('[data-save]').addEventListener('click', async () => {
      if (!blob) return;
      (q('[data-save]') as HTMLButtonElement).disabled = true;
      end(await storeBlobClip(t, blob, mime, formatPixelFilename(Date.now())));
    });

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') msg('Recording isn’t supported here. Use Select Video.');
    else void open();
  });
}

/** Play, slow-mo analyze, frame-jog, precision-trim, and export/share a lift clip. */
export async function playVideo(id: string, ctx?: { target?: VideoTarget; setLabel?: string }): Promise<void> {
  let meta = lifts.videos.get(id);
  if (!meta) return;
  let blob: Blob | null = sessionFiles.get(id) ?? (meta.linkOnly ? null : await lifts.videoBlob(id));
  if (!blob && !meta.linkOnly && !meta.fileName && !meta.uri && !meta.driveQuery && !meta.lanUrl) {
    toast('Video file is missing');
    return;
  }

  let activeObjectUrl = blob ? URL.createObjectURL(blob) : '';
  let playUrl = activeObjectUrl || '';
  if (!playUrl && isNative() && meta.uri?.startsWith('content://')) {
    playUrl = (await getPlayableNativeVideoUrl(meta.uri, meta.mime)) ?? '';
  }
  const needsLanLoad = !playUrl && !!meta.lanUrl;
  const canHaveDeck = !!(playUrl || meta.lanUrl);

  const s = lifts.sessions.get(meta.sessionId);
  const set = s?.exs.flatMap((e) => e.sets).find((x) => x.id === meta!.setId);
  const ex = lifts.exOrStub(meta.exId);
  const driveUrl = driveUrlFor(meta);
  const hasLocalUri = isNative() && !!meta.uri?.startsWith('content://');
  const setTxt = set ? `${set.w ? `${fmtKg(set.w)} ${liftSettings().unit}` : ex.type === 'bodyweight' ? 'BW' : '–'} × ${set.r ?? 0}` : '';
  const dateTxt = new Date(meta.fileMs ?? meta.ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  const subTxt = [setTxt, dateTxt, meta.camera].filter(Boolean).join(' · ');

  let st: TrimState = createTrimState(meta.durationSec ?? 0, meta.trimStartSec, meta.trimEndSec);
  let speed = 1;
  let shareOpen = false;
  let uiTicker: ReturnType<typeof setInterval> | null = null;
  let closed = false;
  const dlCtrl = new AbortController();

  const posterHtml = meta.thumb
    ? `<div class="pv-poster" id="vpPoster"${playUrl ? ' hidden' : ''} style="background-image:url(${meta.thumb})"><span class="pv-badge">${needsLanLoad ? 'Loading local clip…' : (meta.source === 'gymcam' ? 'Gym Cam · Google Drive' : 'DCIM Backup · Google Drive')}</span></div>`
    : `<div class="pv-poster" id="vpPoster" hidden><span class="pv-badge">Google Drive Backup</span></div>`;

  const tapeAvail = (): boolean => meta!.source === 'gymcam' && gymCam.online && meta!.clipStartMs != null;

  const sh = openSheet(
    `<div class="vp-head">
       <div class="vp-title"><h3 class="sh-title">${esc(ex.name)}</h3><p class="sh-text">${esc(subTxt)}</p></div>
       <div class="vp-pills">
         ${canHaveDeck ? '<button class="vp-pill on" type="button" id="vpSaveTop" data-vtrim="save" hidden>Save Trim</button>' : ''}
         <button class="vp-pill" type="button" data-vp="share">Share ↗</button>
       </div>
     </div>
     <div class="vp-stage">
       ${canHaveDeck ? `<video class="pv" id="vpVid"${playUrl ? ` src="${esc(playUrl)}"` : ' hidden'} playsinline muted autoplay loop preload="auto"></video>` : ''}
       ${posterHtml}
       <p class="hint" id="vpLoadMsg"${needsLanLoad ? '' : ' hidden'} style="text-align:center">Downloading clip to phone RAM for 60 fps scrubbing…</p>
       <p class="hint" id="vpLanErr" hidden style="text-align:center">OptiPlex LAN stream unavailable away from home — open via Google Drive below.</p>
     </div>
     ${canHaveDeck ? `
     <div class="vp-deck" id="vpDeck"${playUrl ? '' : ' hidden'}>
       <div class="vp-readout">
         <span><span class="vp-time" id="vpTime">0:00.00 / 0:00.00</span> · <span class="vp-frame" id="vpFrame">f 0</span></span>
         <button class="vp-focus" type="button" id="vpFocus" data-vfocus-cycle>Full clip</button>
       </div>
       <div class="vp-rail" id="vpSeek">
         <div class="vp-track" id="vpTrack">
           <div class="vp-shade l" id="vpShadeL"></div>
           <div class="vp-shade r" id="vpShadeR"></div>
           <div class="vp-window" id="vpKeep"></div>
           <div class="vp-head-line" id="vpHead"></div>
           <button class="vp-bar-h in" id="vpInH" type="button" aria-label="Trim start bar"><span>‹</span></button>
           <button class="vp-bar-h out" id="vpOutH" type="button" aria-label="Trim end bar"><span>›</span></button>
         </div>
       </div>
       <div class="jog-strip" id="vpJog"><i class="jog-needle"></i><span id="vpJogLbl">◀ Drag to jog frame-by-frame ▶</span></div>
       <div class="vp-step-row">
         <button class="vp-btn step" type="button" data-vstep="-5">−5f</button>
         <button class="vp-btn step" type="button" data-vstep="-1">−1f</button>
         <button class="vp-btn play" type="button" data-vplay>⏸</button>
         <button class="vp-btn step" type="button" data-vstep="1">+1f</button>
         <button class="vp-btn step" type="button" data-vstep="5">+5f</button>
       </div>
       <div class="vp-sub-bar">
         <div class="vp-speeds">
           <button class="vp-spd" type="button" data-vspeed="0.25">0.25×</button>
           <button class="vp-spd" type="button" data-vspeed="0.5">0.5×</button>
           <button class="vp-spd on" type="button" data-vspeed="1">1×</button>
         </div>
         <div class="vp-trim-quick" id="vpTrimQuick">
           <span class="vp-trim-hint" id="vpTrimHint">Drag <b>‹</b> or <b>›</b> bars to trim</span>
           <button class="vp-qbtn" type="button" id="vpTrimReset" data-vtrim="reset" hidden>Reset</button>
           <button class="vp-qbtn pri" type="button" id="vpTrimSave" data-vtrim="save" hidden>Save</button>
           <button class="vp-qbtn acc" type="button" id="vpTrimShare" data-vtrim="share" hidden>Trim &amp; Share ↗</button>
         </div>
       </div>
       <div class="vp-tape-row" id="vpTapeRow"${tapeAvail() ? '' : ' hidden'}>
         <small>2-Wk Tape</small>
         <button class="vp-nbtn" type="button" data-vtape="5:0">−5s Earlier</button>
         <button class="vp-nbtn" type="button" data-vtape="0:5">+5s Later</button>
       </div>
     </div>` : ''}
     <div class="vp-drawer" id="vpShareBox" hidden>
       <div class="vp-dr-head">
         <span>Export &amp; Share</span>
         <button type="button" data-vp="share" aria-label="Close share">✕</button>
       </div>
       <button class="btn primary big" type="button" data-vexp="share">Share Video…</button>
       <button class="btn big" type="button" data-vexp="save">Save MP4 to Gallery</button>
       ${driveUrl ? `<a class="btn big" href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>Open in Google Drive ↗</a>` : ''}
     </div>
     ${meta.fileName ? `<div class="vfile"><small>LINKED FILE${meta.driveQuery ? ' · TIMESTAMP MATCHED' : ''}</small><b>${esc(meta.fileName)}</b></div>` : ''}
     ${driveUrl ? `<a class="btn primary big" id="vpDriveFallback"${canHaveDeck ? ' hidden' : ''} href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>Open in Google Drive ↗</a>` : ''}
     ${hasLocalUri && !playUrl ? '<button class="btn big" type="button" data-vlocal>▶ Play from phone DCIM</button>' : ''}
     ${ctx?.target ? '<button class="btn big" type="button" data-vmanage>+ Add / Manage Clips</button>' : ''}
     <div class="vp-foot-row">
       <button class="btn sm" type="button" data-vedit>Edit filename / URL</button>
       <button class="btn sm danger" type="button" data-del>${meta.linkOnly ? 'Unlink' : 'Delete'}</button>
     </div>
     <button class="btn big" type="button" data-ok>Close</button>`,
    {
      onClose: () => {
        closed = true;
        dlCtrl.abort();
        if (uiTicker) clearInterval(uiTicker);
        if (activeObjectUrl) URL.revokeObjectURL(activeObjectUrl);
      },
    },
  );

  const vid = sh.body.querySelector<HTMLVideoElement>('#vpVid');
  const q = <T extends HTMLElement>(sel: string): T | null => sh.body.querySelector<T>(sel);

  let pendingSeekSec: number | null = null;
  const seekVid = (sec: number): void => {
    if (!vid) return;
    const target = st.dur > 0 ? clamp(sec, 0, st.dur) : Math.max(0, sec);
    if (!vid.seeking) {
      pendingSeekSec = null;
      vid.currentTime = target;
    } else {
      pendingSeekSec = target;
    }
  };

  const resolveLanPlayable = async (lanUrl: string, bytesHint = 0): Promise<string> => {
    if ((!bytesHint || bytesHint <= MAX_LOCAL_CACHE_BYTES) && typeof fetch !== 'undefined') {
      const tCtrl = new AbortController();
      const onParentAbort = () => tCtrl.abort();
      dlCtrl.signal.addEventListener('abort', onParentAbort, { once: true });
      const timer = setTimeout(() => tCtrl.abort(), 12_000);
      try {
        const res = await fetch(lanUrl, { signal: tCtrl.signal, cache: 'no-store' });
        if (res.ok) {
          const dlBlob = await res.blob();
          if (!closed && dlBlob.size > 0 && dlBlob.size <= MAX_LOCAL_CACHE_BYTES) {
            cacheSessionBlob(id, dlBlob);
            blob = dlBlob;
            const prevUrl = activeObjectUrl;
            activeObjectUrl = URL.createObjectURL(dlBlob);
            if (prevUrl) URL.revokeObjectURL(prevUrl);
            return activeObjectUrl;
          }
        }
      } catch {
        // Fallback below if LAN download fails
      } finally {
        clearTimeout(timer);
        dlCtrl.signal.removeEventListener('abort', onParentAbort);
      }
    }
    if (closed) return '';
    return isNative() ? ((await getPlayableNativeVideoUrl(lanUrl, meta!.mime)) ?? lanUrl) : lanUrl;
  };

  const syncUi = (): void => {
    if (closed) return;
    const sharePill = q('[data-vp="share"]');
    const shareBox = q('#vpShareBox');
    sharePill?.classList.toggle('on', shareOpen);
    if (shareBox) shareBox.hidden = !shareOpen;

    const trimmed = isTrimmed(st);
    const dirty = isTrimDirty(st);
    const saveTop = q<HTMLButtonElement>('#vpSaveTop');
    if (saveTop) saveTop.hidden = !dirty;

    if (!vid) return;
    const total = st.dur > 0 ? st.dur : (Number.isFinite(vid.duration) ? roundMs(vid.duration) : 0);
    const cur = st.dur > 0 ? clamp(st.playhead, 0, total || 0) : (vid.currentTime || 0);
    const edgeFocus = st.focus === 'in' || st.focus === 'out';
    const winDur = Math.max(0, roundMs(st.end - st.start));

    const timeEl = q('#vpTime');
    const frameEl = q('#vpFrame');
    const focusEl = q('#vpFocus');
    const railEl = q('#vpSeek');
    const shadeL = q('#vpShadeL');
    const shadeR = q('#vpShadeR');
    const keepEl = q('#vpKeep');
    const headEl = q('#vpHead');
    const inH = q('#vpInH');
    const outH = q('#vpOutH');
    const jogEl = q('#vpJog');
    const jogLbl = q('#vpJogLbl');
    const playBtn = q('[data-vplay]');

    if (timeEl) timeEl.textContent = `${fmtPreciseTime(cur)} / ${fmtPreciseTime(total)}`;
    if (frameEl) frameEl.textContent = `f ${Math.round(cur * FRAME_FPS)}`;

    if (focusEl) {
      focusEl.classList.toggle('edge', edgeFocus);
      if (st.focus === 'in') focusEl.textContent = `◀ Start · ${fmtPreciseTime(st.start)} (${winDur.toFixed(1)}s)`;
      else if (st.focus === 'out') focusEl.textContent = `End ▶ · ${fmtPreciseTime(st.end)} (${winDur.toFixed(1)}s)`;
      else focusEl.textContent = trimmed ? `Clip · ${winDur.toFixed(1)}s` : 'Full clip';
    }

    if (total > 0) {
      const inPct = clamp((st.start / total) * 100, 0, 100);
      const outPct = clamp((st.end / total) * 100, inPct, 100);
      const headPct = clamp((cur / total) * 100, 0, 100);
      if (railEl) railEl.classList.toggle('trimmed', trimmed);
      if (shadeL) shadeL.style.width = `${inPct}%`;
      if (shadeR) { shadeR.style.left = `${outPct}%`; shadeR.style.width = `${Math.max(0, 100 - outPct)}%`; }
      if (keepEl) { keepEl.style.left = `${inPct}%`; keepEl.style.width = `${Math.max(1, outPct - inPct)}%`; }
      if (headEl) headEl.style.left = `${headPct}%`;
      if (inH) { inH.style.left = `${inPct}%`; inH.classList.toggle('active', st.focus === 'in'); }
      if (outH) { outH.style.left = `${outPct}%`; outH.classList.toggle('active', st.focus === 'out'); }
    }

    if (jogEl) {
      jogEl.style.backgroundPositionX = `${cur * 42}px`;
      jogEl.classList.toggle('edge-focus', edgeFocus);
    }
    if (jogLbl) {
      jogLbl.textContent = st.focus === 'in'
        ? '◀ Fine-jog Start bar ▶'
        : st.focus === 'out'
          ? '◀ Fine-jog End bar ▶'
          : '◀ Drag to jog frame-by-frame ▶';
    }
    if (playBtn) playBtn.textContent = vid.paused ? '▶' : '⏸';

    sh.body.querySelectorAll<HTMLElement>('[data-vstep]').forEach((b) => {
      b.classList.toggle('edge', edgeFocus);
    });
    sh.body.querySelectorAll<HTMLElement>('[data-vspeed]').forEach((b) => {
      b.classList.toggle('on', Number(b.dataset.vspeed) === speed);
    });

    const showActs = trimmed || dirty;
    const hintEl = q('#vpTrimHint');
    const resetEl = q('#vpTrimReset');
    const saveEl = q<HTMLButtonElement>('#vpTrimSave');
    const shareTrimEl = q('#vpTrimShare');
    if (hintEl) hintEl.hidden = showActs;
    if (resetEl) resetEl.hidden = !showActs;
    if (saveEl) {
      saveEl.hidden = !showActs;
      saveEl.disabled = showActs && !dirty;
      saveEl.textContent = dirty ? 'Save' : '✓ Saved';
    }
    if (shareTrimEl) shareTrimEl.hidden = !trimmed;

    const tapeRow = q('#vpTapeRow');
    if (tapeRow) tapeRow.hidden = !tapeAvail();
  };

  if (vid) {
    vid.addEventListener('seeked', () => {
      if (closed || pendingSeekSec == null) return;
      const next = pendingSeekSec;
      pendingSeekSec = null;
      vid.currentTime = next;
    });
    vid.onloadedmetadata = () => {
      if (Number.isFinite(vid.duration) && vid.duration > 0) {
        st = createTrimState(vid.duration, meta!.trimStartSec, meta!.trimEndSec);
        if (st.start > 0.015 && vid.currentTime < st.start) seekVid(st.start);
      }
      vid.playbackRate = speed;
      syncUi();
    };
    vid.onerror = () => {
      const deck = q('#vpDeck');
      const poster = q('#vpPoster');
      const err = q('#vpLanErr');
      const loadMsg = q('#vpLoadMsg');
      const drv = q('#vpDriveFallback');
      vid.hidden = true;
      if (loadMsg) loadMsg.hidden = true;
      if (deck) deck.hidden = true;
      if (poster) poster.hidden = false;
      if (err) err.hidden = false;
      if (drv) drv.hidden = false;
    };
    vid.onclick = () => {
      if (vid.paused) {
        st = resolvePlayStart(st);
        seekVid(st.playhead);
        void vid.play().catch(() => {});
      } else {
        vid.pause();
      }
      syncUi();
    };
    vid.ontimeupdate = () => {
      if (vid.paused) return;
      if (isTrimmed(st) && vid.currentTime >= st.end - 0.025) {
        vid.currentTime = st.start;
      }
      st = { ...st, playhead: st.dur > 0 ? clamp(vid.currentTime, 0, st.dur) : vid.currentTime };
      syncUi();
    };
    uiTicker = setInterval(() => {
      if (closed || !vid || vid.paused) return;
      if (isTrimmed(st) && vid.currentTime >= st.end - 0.025) vid.currentTime = st.start;
      st = { ...st, playhead: st.dur > 0 ? clamp(vid.currentTime, 0, st.dur) : vid.currentTime };
      syncUi();
    }, 45);

    // Bracketed Start / End grab rails + seek track (zero-jump handle offset & coalesced seek)
    const railEl = q('#vpSeek');
    const trackEl = q('#vpTrack');
    const inH = q('#vpInH');
    const outH = q('#vpOutH');
    let dragMode: TrimFocus | null = null;
    let dragGrabOffsetPx = 0;

    const timeAtPtr = (clientX: number): number => {
      if (!trackEl || st.dur <= 0) return 0;
      const rect = trackEl.getBoundingClientRect();
      return clamp(((clientX - rect.left) / Math.max(1, rect.width)) * st.dur, 0, st.dur);
    };

    const startSeekDrag = (mode: TrimFocus, e: PointerEvent): void => {
      if (!railEl || !trackEl || st.dur <= 0) return;
      e.stopPropagation();
      dragMode = mode;
      railEl.setPointerCapture(e.pointerId);
      vid.pause();
      const rect = trackEl.getBoundingClientRect();
      if (mode === 'in') {
        dragGrabOffsetPx = e.clientX - (rect.left + (st.start / st.dur) * rect.width);
        st = { ...st, focus: 'in', playhead: st.start };
      } else if (mode === 'out') {
        dragGrabOffsetPx = e.clientX - (rect.left + (st.end / st.dur) * rect.width);
        st = { ...st, focus: 'out', playhead: st.end };
      } else {
        dragGrabOffsetPx = 0;
        st = dragTrimHandle(st, 'playhead', timeAtPtr(e.clientX));
      }
      seekVid(st.playhead);
      syncUi();
    };

    inH?.addEventListener('pointerdown', (e) => startSeekDrag('in', e));
    outH?.addEventListener('pointerdown', (e) => startSeekDrag('out', e));
    trackEl?.addEventListener('pointerdown', (e) => startSeekDrag('playhead', e));
    railEl?.addEventListener('pointermove', (e) => {
      if (!dragMode || !railEl.hasPointerCapture(e.pointerId)) return;
      st = dragTrimHandle(st, dragMode, timeAtPtr(e.clientX - dragGrabOffsetPx));
      seekVid(st.playhead);
      syncUi();
    });
    const endSeekDrag = (): void => { dragMode = null; dragGrabOffsetPx = 0; };
    railEl?.addEventListener('pointerup', endSeekDrag);
    railEl?.addEventListener('pointercancel', endSeekDrag);

    // Tactile scrolling Thumb Jog Strip (~2.5s across full width, coupled to active bar)
    const jogEl = q('#vpJog');
    let jogStartX = 0;
    let jogBase: TrimState = st;
    jogEl?.addEventListener('pointerdown', (e) => {
      if (st.dur <= 0) return;
      jogEl.setPointerCapture(e.pointerId);
      jogStartX = e.clientX;
      jogBase = st;
      vid.pause();
      jogEl.classList.add('drag');
      syncUi();
    });
    jogEl?.addEventListener('pointermove', (e) => {
      if (!jogEl.hasPointerCapture(e.pointerId) || st.dur <= 0) return;
      const secPerPx = 2.5 / Math.max(220, jogEl.clientWidth);
      st = jogTrimDelta(jogBase, (e.clientX - jogStartX) * secPerPx);
      seekVid(st.playhead);
      syncUi();
    });
    const endJog = (): void => { jogEl?.classList.remove('drag'); };
    jogEl?.addEventListener('pointerup', endJog);
    jogEl?.addEventListener('pointercancel', endJog);

    if (needsLanLoad && meta.lanUrl) {
      void resolveLanPlayable(meta.lanUrl, meta.bytes).then((nextUrl) => {
        if (closed) return;
        const poster = q('#vpPoster');
        const loadMsg = q('#vpLoadMsg');
        const deck = q('#vpDeck');
        const err = q('#vpLanErr');
        const drv = q('#vpDriveFallback');
        if (loadMsg) loadMsg.hidden = true;
        if (!nextUrl) {
          if (poster) poster.hidden = false;
          if (err) err.hidden = false;
          if (drv) drv.hidden = false;
          return;
        }
        vid.hidden = false;
        if (poster) poster.hidden = true;
        if (deck) deck.hidden = false;
        vid.src = nextUrl;
        vid.load();
        void vid.play().catch(() => {});
        syncUi();
      });
    }
  }

  const persistTrim = async (): Promise<void> => {
    const next: VideoMeta = { ...meta! };
    if (isTrimmed(st)) {
      next.trimStartSec = roundMs(st.start);
      next.trimEndSec = roundMs(st.end);
    } else {
      delete next.trimStartSec;
      delete next.trimEndSec;
    }
    meta = next;
    st = { ...st, savedStart: st.start, savedEnd: st.end };
    await lifts.saveVideoMeta(next);
  };

  const runExport = async (action: 'share' | 'save', btn?: HTMLButtonElement | null): Promise<void> => {
    const orig = btn?.textContent ?? '';
    if (btn) { btn.disabled = true; btn.textContent = action === 'save' ? 'Saving…' : 'Preparing…'; }
    try {
      if (isTrimDirty(st)) await persistTrim();
      const startMs = isTrimmed(st) ? Math.round(st.start * 1000) : undefined;
      const endMs = isTrimmed(st) ? Math.round(st.end * 1000) : undefined;
      const title = `${ex.name}${setTxt ? ` · ${setTxt}` : ''}`;
      if (isNative()) {
        const uri = meta!.uri?.startsWith('content://') ? meta!.uri : undefined;
        const httpUrl = !uri && meta!.lanUrl ? meta!.lanUrl : undefined;
        const base64 = !uri && !httpUrl && blob ? await blobToBase64(blob) : undefined;
        if (!uri && !httpUrl && !base64) {
          toast('Video file not cached on phone — open via Google Drive');
          return;
        }
        const res = await exportNativeVideo({
          uri,
          httpUrl,
          base64,
          fileName: meta!.fileName || `${ex.id}-clip.mp4`,
          title,
          startMs,
          endMs,
          action,
        });
        if (res.ok) {
          toast(action === 'save' ? `Saved to ${res.savedPath || 'Movies/ArmoryFit'}` : 'Share sheet opened');
        } else {
          toast(res.error || 'Export failed');
        }
      } else if (blob) {
        await shareImage(blob, meta!.fileName || 'lift-video.mp4', title);
      } else {
        toast('Install APK on Pixel to trim or share this clip');
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
      syncUi();
    }
  };

  syncUi();

  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-ok]')) { sh.close(); return; }

    const vp = t.closest<HTMLElement>('[data-vp]')?.dataset.vp;
    if (vp === 'share') {
      shareOpen = !shareOpen;
      syncUi();
      return;
    }

    if (t.closest('[data-vfocus-cycle]') && vid) {
      vid.pause();
      const nextFocus: TrimFocus = st.focus === 'playhead' ? 'in' : st.focus === 'in' ? 'out' : 'playhead';
      const jump = nextFocus === 'in' ? st.start : nextFocus === 'out' ? st.end : st.playhead;
      st = { ...st, focus: nextFocus, playhead: jump };
      seekVid(st.playhead);
      syncUi();
      return;
    }

    const spd = t.closest<HTMLElement>('[data-vspeed]');
    if (spd && vid) {
      speed = Number(spd.dataset.vspeed);
      vid.playbackRate = speed;
      syncUi();
      return;
    }
    if (t.closest('[data-vplay]') && vid) {
      if (vid.paused) {
        st = resolvePlayStart(st);
        seekVid(st.playhead);
        void vid.play().catch(() => {});
      } else {
        vid.pause();
      }
      syncUi();
      return;
    }
    const step = t.closest<HTMLElement>('[data-vstep]');
    if (step && vid) {
      vid.pause();
      st = stepTrimFrames(st, Number(step.dataset.vstep), FRAME_FPS);
      seekVid(st.playhead);
      syncUi();
      return;
    }

    const vtrim = t.closest<HTMLElement>('[data-vtrim]')?.dataset.vtrim;
    if (vtrim && vid) {
      if (vtrim === 'reset') {
        vid.pause();
        st = { ...createTrimState(st.dur), savedStart: 0, savedEnd: st.dur };
        seekVid(st.playhead);
        await persistTrim();
        toast('Trim reset');
      } else if (vtrim === 'save') {
        await persistTrim();
        toast('Clip trimmed');
      } else if (vtrim === 'share') {
        await runExport('share', t.closest('button'));
      }
      syncUi();
      return;
    }

    const vtape = t.closest<HTMLElement>('[data-vtape]');
    if (vtape && vid) {
      const [bStr, aStr] = vtape.dataset.vtape!.split(':');
      const btn = t.closest<HTMLButtonElement>('button');
      if (btn) btn.disabled = true;
      const updated = await gymCam.extendTapeClip(meta!, Number(bStr), Number(aStr));
      if (btn) btn.disabled = false;
      if (updated?.lanUrl) {
        meta = updated;
        sessionFiles.delete(id);
        st = createTrimState(updated.durationSec ?? st.dur, updated.trimStartSec, updated.trimEndSec);
        pendingSeekSec = null;
        const nextPlayUrl = await resolveLanPlayable(updated.lanUrl, updated.bytes);
        if (!closed && nextPlayUrl) {
          vid.src = nextPlayUrl;
          vid.load();
          void vid.play().catch(() => {});
        }
        toast('Extended clip from 2-Week Tape');
      } else {
        toast('Tape window unavailable on OptiPlex');
      }
      syncUi();
      return;
    }

    const vexp = t.closest<HTMLElement>('[data-vexp]');
    if (vexp) {
      await runExport(vexp.dataset.vexp as 'share' | 'save', t.closest('button'));
      return;
    }

    if (t.closest('[data-vmanage]') && ctx?.target) {
      sh.close();
      await openSetVideoSheet(ctx.target, ctx.setLabel ?? 'Set');
      return;
    }

    if (t.closest('[data-vlocal]')) {
      const ok = await openNativeVideoUri(meta!.uri!);
      if (!ok) toast('Local file moved or unavailable — use Google Drive above');
    } else if (t.closest('[data-vedit]')) {
      const v = await promptSheet('Filename or Drive URL', meta!.uri && /^https?:\/\//i.test(meta!.uri) ? meta!.uri : (meta!.fileName ?? ''), 'PXL_20261116_173012345.mp4');
      if (!v) return;
      const isUrl = /^https?:\/\//i.test(v);
      const next: VideoMeta = {
        ...meta!,
        linkOnly: true,
        fileName: isUrl ? v.split('/').pop()?.split('?')[0] || 'Drive video' : v,
        ...(isUrl ? { uri: v } : {}),
      };
      delete next.driveQuery;
      await lifts.saveVideoMeta(next);
      sh.close();
      toast('Video link updated');
      void playVideo(meta!.id, ctx);
    } else if (t.closest('[data-del]')) {
      const msg = meta!.linkOnly ? 'Removes this link from the set (keeps the file on your phone & Drive).' : 'It is removed from this phone.';
      if (await confirmSheet(meta!.linkOnly ? 'Unlink this video?' : 'Delete this video?', msg, meta!.linkOnly ? 'Unlink' : 'Delete', true)) {
        sessionFiles.delete(id);
        await lifts.deleteVideo(id);
        await resetTimerIfIdle(lifts.sessions.get(meta!.sessionId));
        sh.close();
        toast(meta!.linkOnly ? 'Video unlinked' : 'Video deleted');
      }
    }
  });
}
