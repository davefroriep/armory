import { active } from '../../lifts/active';
import {
  gymCam, type GymCamBurst, type GymCamPreviewResult, type GymCamTapeFrame, type GymCamTimeline,
} from '../../lifts/gymCam';
import {
  camsForExercise, DEFAULT_CAM_OPTIONS, DEFAULT_FALLBACK_CAM, driveUrlFor, normalizeCamList,
} from '../../lifts/logic';
import {
  extractMp4CreationMs, formatPixelFilename, inferPixelVideoLink, sortDeviceVideosNewestFirst,
} from '../../lifts/mp4';
import { shareImage } from '../../lifts/share';
import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
import {
  applyClipMarker, buildCoachAngleOptions, burstScrubPosition, computeClipZoomWindow, computeScrubWindow,
  findNearestFrameIndex, fmtRelativeToCheck, isTapeWithinRetention, nextBurstPlayIndex, pickNearestBurstIndex,
  shortCamLabel, tapeDaysRemaining,
} from '../../lifts/tape';
import {
  computeTrimViewport, createTrimState, dragTrimHandle, fmtPreciseTime, FRAME_FPS, isTrimDirty, isTrimmed,
  jogTrimDelta, resolvePlayStart, roundMs, stepTrimFrames, type TrimFocus, type TrimState, type TrimViewport,
} from '../../lifts/trimmer';
import type { LiftSession, LiftSet, SessionEx, VideoMeta } from '../../lifts/types';
import { uid } from '../../lifts/types';
import {
  exportNativeVideo, getPlayableNativeVideoUrl, isNative, listNativeDeviceVideos,
  openNativeVideoUri, pickNativeDcimVideo, recordNativeVideo, type DeviceVideoEntry,
} from '../../native/service';
import { getSettings } from '../../store/settings';
import { setWallboardReplay } from '../../wallboard/state';
import { wallboard } from '../../wallboard/sync';
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
    let selectedIdx = -1;
    let previewGen = 0;
    let previewLoading = false;
    let previewUrl = '';
    let previewErr = '';
    let dragPickRail = false;

    const sh = openSheet(
      `<h3 class="sh-title">Select Video</h3>
       <div class="vpick-sub">Tap a clip to scrub &amp; preview · Newest first</div>
       <div class="vpick-preview" id="vpickPreview" hidden></div>
       <div id="vpickList"></div>
       <div class="vpick-foot" id="vpickFoot"></div>`,
      {
        full: true,
        onClose: () => {
          releasePreviewVideo();
          if (!done && !deferResolve) {
            done = true;
            resolve(resolved);
          }
        },
      },
    );

    const releasePreviewVideo = (): void => {
      dragPickRail = false;
      const vid = sh.body.querySelector<HTMLVideoElement>('#vpickVid');
      if (vid) {
        vid.pause();
        vid.removeAttribute('src');
        vid.load();
      }
    };

    const syncCardSel = (): void => {
      sh.body.querySelectorAll<HTMLElement>('[data-vidx]').forEach((card) => {
        card.classList.toggle('sel', Number(card.dataset.vidx) === selectedIdx);
      });
    };

    const syncPickScrub = (): void => {
      const vid = sh.body.querySelector<HTMLVideoElement>('#vpickVid');
      const fill = sh.body.querySelector<HTMLElement>('#vpickFill');
      const head = sh.body.querySelector<HTMLElement>('#vpickHead');
      const timeEl = sh.body.querySelector<HTMLElement>('#vpickTime');
      const playBtn = sh.body.querySelector<HTMLButtonElement>('[data-pplay]');
      const entry = entries[selectedIdx];
      const fallbackDur = entry?.durationSec ?? 0;
      const dur = vid && Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : fallbackDur;
      const cur = vid && Number.isFinite(vid.currentTime) ? vid.currentTime : 0;
      const pct = dur > 0 ? clamp((cur / dur) * 100, 0, 100) : 0;
      if (fill) fill.style.width = `${pct.toFixed(2)}%`;
      if (head) head.style.left = `${pct.toFixed(2)}%`;
      if (timeEl) timeEl.textContent = `${fmtVideoDuration(cur)} / ${fmtVideoDuration(dur)}`;
      if (playBtn) {
        const playing = !!vid && !vid.paused && !vid.ended;
        playBtn.classList.toggle('on', playing);
        playBtn.textContent = playing ? '⏸' : '▶';
      }
    };

    const renderList = (): void => {
      const listEl = sh.body.querySelector<HTMLElement>('#vpickList');
      const footEl = sh.body.querySelector<HTMLElement>('#vpickFoot');
      if (listEl) {
        listEl.innerHTML = entries.length
          ? `<div class="vpick-grid">${entries.map((v, idx) => `<button class="vpick-card${idx === selectedIdx ? ' sel' : ''}" type="button" data-vidx="${idx}">
              <span class="vpick-th" style="${v.thumb ? `background-image:url(${v.thumb})` : ''}">
                ${v.durationSec ? `<em class="vpick-dur">${esc(fmtVideoDuration(v.durationSec))}</em>` : ''}
              </span>
              <span class="vpick-meta">
                <b>${esc(fmtVideoTimestamp(v.fileMs))}</b>
                <small>${esc([v.bucket, v.bytes ? `${(v.bytes / 1048576).toFixed(1)} MB` : ''].filter(Boolean).join(' · ') || v.fileName)}</small>
              </span>
            </button>`).join('')}</div>`
          : '<p class="sh-text" style="text-align:center;padding:18px 0">No device videos found.</p>';
      }
      if (footEl) {
        footEl.innerHTML = `${hasMore ? `<button class="btn" type="button" data-vmore ${loadingMore ? 'disabled' : ''}>${loadingMore ? 'Loading…' : 'Show older videos'}</button>` : ''}
          <button class="btn" type="button" data-vsaf>Browse system files…</button>
          <button class="btn" type="button" data-vcancel>Cancel</button>`;
      }
    };

    const renderPreview = (): void => {
      const box = sh.body.querySelector<HTMLElement>('#vpickPreview');
      if (!box) return;
      const entry = entries[selectedIdx];
      if (!entry) {
        releasePreviewVideo();
        box.hidden = true;
        box.innerHTML = '';
        return;
      }
      releasePreviewVideo();
      box.hidden = false;
      const metaLine = [
        entry.bucket,
        entry.bytes ? `${(entry.bytes / 1048576).toFixed(1)} MB` : '',
        entry.durationSec ? fmtVideoDuration(entry.durationSec) : '',
      ].filter(Boolean).join(' · ') || entry.fileName;
      box.innerHTML = `
        <div class="vpick-phead">
          <span><b>${esc(fmtVideoTimestamp(entry.fileMs))}</b><small>${esc(metaLine)}</small></span>
          <button class="vpick-pclose" type="button" data-pclose aria-label="Close preview">✕</button>
        </div>
        <video class="vpick-vid" id="vpickVid" playsinline muted preload="auto"${entry.thumb ? ` poster="${esc(entry.thumb)}"` : ''}${previewUrl ? ` src="${esc(previewUrl)}"` : ''}></video>
        ${previewLoading ? '<div class="hint" style="text-align:center">Loading clip preview…</div>' : previewErr ? `<div class="hint" style="text-align:center">${esc(previewErr)}</div>` : ''}
        <div class="vburst-scrub-row">
          <button class="vburst-play" type="button" data-pplay aria-label="Play or pause preview" ${previewUrl ? '' : 'disabled'}>▶</button>
          <div class="vscrub-rail vburst-rail" id="vpickRail" data-prail>
            <div class="vburst-fill" id="vpickFill" style="width:0%"></div>
            <div class="vscrub-head" id="vpickHead" style="left:0%"></div>
          </div>
          <span class="vpick-ptime" id="vpickTime">0:00 / ${fmtVideoDuration(entry.durationSec || 0)}</span>
        </div>
        <button class="btn primary big" type="button" data-puse>Link This Clip</button>`;

      const vid = box.querySelector<HTMLVideoElement>('#vpickVid');
      if (vid) {
        const onSync = () => syncPickScrub();
        vid.addEventListener('loadedmetadata', onSync);
        vid.addEventListener('timeupdate', onSync);
        vid.addEventListener('play', onSync);
        vid.addEventListener('pause', onSync);
        vid.addEventListener('ended', onSync);
      }
      syncPickScrub();
    };

    const selectPreviewClip = async (idx: number): Promise<void> => {
      const entry = entries[idx];
      if (!entry) return;
      const gen = ++previewGen;
      selectedIdx = idx;
      previewLoading = true;
      previewUrl = '';
      previewErr = '';
      syncCardSel();
      renderPreview();
      sh.body.scrollTo({ top: 0, behavior: 'smooth' });

      const url = await getPlayableNativeVideoUrl(entry.uri, entry.mime);
      if (gen !== previewGen || done || selectedIdx !== idx) return;
      previewLoading = false;
      if (url) {
        previewUrl = url;
        previewErr = '';
      } else {
        previewUrl = '';
        previewErr = 'Inline preview unavailable — tap Link This Clip to attach it.';
      }
      renderPreview();
    };

    const applyPickRailPointer = (clientX: number): void => {
      const rail = sh.body.querySelector<HTMLElement>('#vpickRail');
      const vid = sh.body.querySelector<HTMLVideoElement>('#vpickVid');
      const entry = entries[selectedIdx];
      if (!rail || !entry) return;
      const rect = rail.getBoundingClientRect();
      const pct = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
      const fill = sh.body.querySelector<HTMLElement>('#vpickFill');
      const head = sh.body.querySelector<HTMLElement>('#vpickHead');
      const timeEl = sh.body.querySelector<HTMLElement>('#vpickTime');
      if (fill) fill.style.width = `${(pct * 100).toFixed(2)}%`;
      if (head) head.style.left = `${(pct * 100).toFixed(2)}%`;
      if (vid && Number.isFinite(vid.duration) && vid.duration > 0) {
        const nextSec = pct * vid.duration;
        vid.currentTime = nextSec;
        if (timeEl) timeEl.textContent = `${fmtVideoDuration(nextSec)} / ${fmtVideoDuration(vid.duration)}`;
      }
    };

    const finish = (meta: VideoMeta | null): void => {
      if (done) return;
      done = true;
      resolved = meta;
      releasePreviewVideo();
      sh.close();
      resolve(resolved);
    };

    renderList();

    sh.body.addEventListener('pointerdown', (e) => {
      const rail = (e.target as HTMLElement).closest<HTMLElement>('[data-prail]');
      if (!rail) return;
      const vid = sh.body.querySelector<HTMLVideoElement>('#vpickVid');
      if (vid && Number.isFinite(vid.duration) && vid.duration > 0) {
        vid.pause();
      }
      e.preventDefault();
      dragPickRail = true;
      rail.setPointerCapture(e.pointerId);
      applyPickRailPointer(e.clientX);
    });
    sh.body.addEventListener('pointermove', (e) => {
      if (!dragPickRail) return;
      applyPickRailPointer(e.clientX);
    });
    const endPickDrag = (): void => {
      dragPickRail = false;
    };
    sh.body.addEventListener('pointerup', endPickDrag);
    sh.body.addEventListener('pointercancel', endPickDrag);

    sh.body.addEventListener('click', async (e) => {
      const el = e.target as HTMLElement;
      const card = el.closest<HTMLElement>('[data-vidx]');
      if (card) {
        const idx = Number(card.dataset.vidx);
        if (idx === selectedIdx && !previewLoading) return;
        void selectPreviewClip(idx);
        return;
      }
      if (el.closest('[data-pclose]')) {
        ++previewGen;
        selectedIdx = -1;
        previewLoading = false;
        previewUrl = '';
        previewErr = '';
        syncCardSel();
        renderPreview();
        return;
      }
      if (el.closest('[data-pplay]') || el.id === 'vpickVid') {
        const vid = sh.body.querySelector<HTMLVideoElement>('#vpickVid');
        if (!vid || !previewUrl) return;
        if (vid.paused || vid.ended) {
          if (vid.ended) vid.currentTime = 0;
          await vid.play().catch(() => {});
        } else {
          vid.pause();
        }
        syncPickScrub();
        return;
      }
      const useBtn = el.closest<HTMLButtonElement>('[data-puse]');
      if (useBtn) {
        const entry = entries[selectedIdx];
        if (!entry) return;
        useBtn.disabled = true;
        useBtn.textContent = 'Linking…';
        releasePreviewVideo();
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
        renderList();
        const next = await listNativeDeviceVideos(200);
        loadingMore = false;
        if (next?.granted) {
          const curUri = entries[selectedIdx]?.uri;
          entries = sortDeviceVideosNewestFirst(next.videos);
          hasMore = !!next.hasMore;
          selectedIdx = curUri ? entries.findIndex((x) => x.uri === curUri) : -1;
        }
        if (!done) renderList();
        return;
      }
      if (el.closest('[data-vsaf]')) {
        deferResolve = true;
        releasePreviewVideo();
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

/** Quick 1-tap chooser asking which station camera(s) to arm, highlighting assigned angles and offering Record All when 2+ are assigned. */
export function pickRecordCameraSheet(
  exName: string,
  setLabel: string,
  preferredCams?: string | string[],
  buddyMode = false,
): Promise<{ kind: 'cam'; cameras: string[] } | { kind: 'link' } | { kind: 'cancel' }> {
  const prefNorm = normalizeCamList(preferredCams);
  const prefList = prefNorm.length ? prefNorm : [DEFAULT_FALLBACK_CAM];
  const baseCams = gymCam.cameras.length ? gymCam.cameras : [...DEFAULT_CAM_OPTIONS];
  const cams = [...new Set([...baseCams, ...prefList])];
  return new Promise((resolve) => {
    let out: { kind: 'cam'; cameras: string[] } | { kind: 'link' } | { kind: 'cancel' } = { kind: 'cancel' };
    const sh = openSheet(
      `<h3 class="sh-title">${esc(exName)} · ${esc(setLabel)}</h3>
       <div class="fl">${buddyMode ? 'WHICH CAMERA(S) TO RECORD? · 👥 BUDDY MODE' : 'WHICH CAMERA(S) TO RECORD?'}</div>
       ${prefList.length > 1
         ? `<button class="btn primary big" type="button" data-rcamall style="margin-bottom:8px">Record All Assigned (${esc(prefList.map(shortCamLabel).join(' + '))})</button>`
         : ''}
       <div class="vcam-pick${cams.length > 2 ? ' many' : ''}">
         ${cams.map((c) => {
           const assigned = prefList.includes(c);
           const sub = assigned
             ? (buddyMode ? '★ Assigned · Buddy lock · stops on ✓' : '★ Assigned · stops on ✓')
             : 'Record this angle · stops on ✓';
           return `
           <button class="vcam-btn${assigned ? ' sel' : ''}" type="button" data-rcam="${esc(c)}">
             <b>${esc(shortCamLabel(c))}</b>
             <small>${sub}</small>
           </button>`;
         }).join('')}
       </div>
       <button class="btn big" type="button" data-rlink>Link Existing Tape / Phone Video…</button>
       <button class="btn big" type="button" data-rcancel>Cancel</button>`,
      { onClose: () => resolve(out) },
    );
    sh.body.addEventListener('click', (e) => {
      const el = e.target as HTMLElement;
      if (el.closest('[data-rcamall]')) {
        out = { kind: 'cam', cameras: [...prefList] };
        sh.close();
        return;
      }
      const camBtn = el.closest<HTMLElement>('[data-rcam]');
      if (camBtn) {
        const picked = camBtn.dataset.rcam?.trim() || DEFAULT_FALLBACK_CAM;
        out = { kind: 'cam', cameras: [picked] };
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
  const tapeAgeOk = isTapeWithinRetention(t.session.endMs ?? t.session.startMs, gymCam.retentionDays);
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

  const camList = (): string[] => {
    const base = gymCam.cameras.length ? gymCam.cameras : [...DEFAULT_CAM_OPTIONS];
    const assigned = camsForExercise(ex, lifts.row(t.se.exId), t.se, base);
    return [...new Set([...base, ...assigned])];
  };

  const gymArmHtml = () => {
    if (!camAvail || t.session.status !== 'active' || t.set.done) return '';
    const assigned = camsForExercise(ex, lifts.row(t.se.exId), t.se, gymCam.cameras);
    const multi = assigned.length > 1
      ? `<button class="aitem on" type="button" data-varmall><b>Record All Assigned (${esc(assigned.map(shortCamLabel).join(' + '))})</b><small>Arms all assigned stations and auto-stops on ✓</small></button>`
      : '';
    return multi + camList().map((c) => {
      const isAssigned = assigned.includes(c);
      return `<button class="aitem${isAssigned ? ' on' : ''}" type="button" data-varmcam="${esc(c)}"><b>Record on ${esc(shortCamLabel(c))}${isAssigned ? ' ★' : ''}</b><small>Arms this set and auto-stops when you tap ✓</small></button>`;
    }).join('');
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
  let zoomMode: 'clip' | '5' | '15' | 'all' = '15';
  let customWin: { winStart: number; winEnd: number } | null = null;
  let inMarked = false;
  let outMarked = false;
  let dragScrub = false;

  let packFrames: GymCamTapeFrame[] = [];
  let packKey = '';
  let packLoading = false;
  let packAbort: AbortController | null = null;
  let lastPackIdx = -1;
  let activeFrameUrl = '';
  let scrubDirty = false;

  let burstCursorMs = 0;
  const burstPackCache = new Map<string, GymCamTapeFrame[]>();
  let burstPackKey = '';
  let burstPackLoading = false;
  let burstPackAbort: AbortController | null = null;
  let burstPrefetchAbort: AbortController | null = null;
  let lastBurstPackIdx = -1;
  let dragBurstScrub = false;
  let burstStageDrag: { pointerId: number; startX: number; startY: number; startCursorMs: number; active: boolean } | null = null;
  let burstPlayTimer: ReturnType<typeof setInterval> | null = null;
  let wantBurstAutoPlay = false;

  let previewAudioOn = false;
  let saveWithSound = false;
  let scrubListenOn = false;
  let previewAudioEl: HTMLAudioElement | null = null;
  let previewAudioKey = '';

  const stopPreviewAudio = (resetSrc = false): void => {
    scrubListenOn = false;
    if (previewAudioEl) {
      try {
        previewAudioEl.pause();
      } catch {
        // ignore pause errors
      }
      if (resetSrc) {
        previewAudioEl.removeAttribute('src');
        previewAudioEl.load();
        previewAudioKey = '';
      }
    }
    const lbtn = sh?.body.querySelector<HTMLButtonElement>('[data-slisten]');
    if (lbtn) lbtn.textContent = '▶ Listen';
  };

  const playPreviewAudioRange = (startMs: number, endMs: number, atMs = startMs): void => {
    if (!previewAudioOn || !gymCam.online || endMs <= startMs) return;
    if (!previewAudioEl) {
      previewAudioEl = document.createElement('audio');
      previewAudioEl.preload = 'auto';
      previewAudioEl.addEventListener('ended', () => {
        scrubListenOn = false;
        const lbtn = sh?.body.querySelector<HTMLButtonElement>('[data-slisten]');
        if (lbtn) lbtn.textContent = '▶ Listen';
      });
    }
    const key = `${Math.round(startMs)}|${Math.round(endMs)}`;
    const url = gymCam.roomAudioUrl(startMs, endMs);
    if (previewAudioKey !== key || previewAudioEl.getAttribute('src') !== url) {
      previewAudioKey = key;
      previewAudioEl.src = url;
      previewAudioEl.load();
    }
    const offsetSec = clamp((atMs - startMs) / 1000, 0, Math.max(0, (endMs - startMs) / 1000 - 0.1));
    const applyTime = () => {
      if (!previewAudioEl) return;
      try {
        if (Math.abs((previewAudioEl.currentTime || 0) - offsetSec) > 0.35) {
          previewAudioEl.currentTime = offsetSec;
        }
      } catch {
        // ignore seek before metadata
      }
      void previewAudioEl.play().catch(() => {});
    };
    if (previewAudioEl.readyState >= 1) {
      applyTime();
    } else {
      previewAudioEl.addEventListener('loadedmetadata', applyTime, { once: true });
      void previewAudioEl.play().catch(() => {});
    }
  };

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

  const burstKeyFor = (cam: string, b: GymCamBurst): string => `${cam}|${b.startMs}|${b.endMs}`;
  const currentBurst = (): GymCamBurst | null => (bursts.length ? bursts[clamp(burstIdx, 0, bursts.length - 1)] : null);

  let sh: ReturnType<typeof openSheet>;

  const stopBurstPlay = (): void => {
    if (burstPlayTimer != null) {
      clearInterval(burstPlayTimer);
      burstPlayTimer = null;
    }
    wantBurstAutoPlay = false;
    stopPreviewAudio(false);
    const btn = sh?.body.querySelector<HTMLButtonElement>('[data-bplay]');
    if (btn) {
      btn.classList.remove('on');
      btn.textContent = '▶';
    }
  };

  const clearBurstPacks = (): void => {
    stopBurstPlay();
    burstPackAbort?.abort();
    burstPackAbort = null;
    burstPrefetchAbort?.abort();
    burstPrefetchAbort = null;
    for (const frames of burstPackCache.values()) {
      for (const f of frames) URL.revokeObjectURL(f.url);
    }
    burstPackCache.clear();
    burstPackKey = '';
    burstPackLoading = false;
    lastBurstPackIdx = -1;
  };

  const cleanupResources = (): void => {
    clearPack();
    clearBurstPacks();
    stopPreviewAudio(true);
    previewAudioEl = null;
  };

  sh = openSheet(
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

  const currentWindow = (): { winStart: number; winEnd: number } => {
    if (zoomMode === 'clip' && customWin) return customWin;
    return computeScrubWindow(minMs, maxMs, winCenterMs || cursorMs, zoomMode === 'all' ? null : Number(zoomMode));
  };

  const zoomToClip = (mode: 'in_set' | 'both_set'): void => {
    if (!minMs || !maxMs) return;
    customWin = computeClipZoomWindow(minMs, maxMs, inMs, outMs, mode);
    zoomMode = 'clip';
    winCenterMs = Math.round((customWin.winStart + customWin.winEnd) / 2);
    lastPackIdx = -1;
  };

  const paintBurstFrameLocal = (): void => {
    const b = currentBurst();
    if (!b || !selectedCam) return;
    const pos = burstScrubPosition(b.startMs, b.endMs, burstCursorMs);
    const timeEl = sh.body.querySelector<HTMLElement>('#vburstTime');
    if (timeEl) {
      timeEl.textContent = `${fmtVideoDuration(pos.elapsedSec)} / ${fmtVideoDuration(pos.totalSec)} · ${fmtEpochClock(burstCursorMs)}`;
    }
    const headEl = sh.body.querySelector<HTMLElement>('#vburstHead');
    if (headEl) headEl.style.left = `${pos.pct.toFixed(2)}%`;
    const fillEl = sh.body.querySelector<HTMLElement>('#vburstFill');
    if (fillEl) fillEl.style.width = `${pos.pct.toFixed(2)}%`;

    const frames = burstPackCache.get(burstKeyFor(selectedCam, b));
    const img = sh.body.querySelector<HTMLImageElement>('#vburstImg');
    const spin = sh.body.querySelector<HTMLElement>('#vburstSpin');
    if (frames?.length) {
      const idx = findNearestFrameIndex(frames, burstCursorMs);
      if (idx >= 0) {
        const url = frames[idx].url;
        if (img && (lastBurstPackIdx !== idx || img.getAttribute('src') !== url || img.hidden)) {
          lastBurstPackIdx = idx;
          img.src = url;
          img.hidden = false;
        }
        if (spin) spin.hidden = true;
        return;
      }
    }
    if (img && b.thumb) {
      if (img.getAttribute('src') !== b.thumb || img.hidden) {
        img.src = b.thumb;
        img.hidden = false;
      }
    } else if (img) {
      img.hidden = true;
    }
    if (spin) {
      spin.hidden = !burstPackLoading;
      if (burstPackLoading) spin.textContent = 'Loading scrub frames…';
    }
  };

  const startBurstPlayLoop = (b: GymCamBurst, frames: GymCamTapeFrame[]): void => {
    if (burstPlayTimer != null) clearInterval(burstPlayTimer);
    const btn = sh.body.querySelector<HTMLButtonElement>('[data-bplay]');
    if (btn) {
      btn.classList.add('on');
      btn.textContent = '⏸';
    }
    if (previewAudioOn) {
      playPreviewAudioRange(b.startMs, b.endMs, burstCursorMs || b.startMs);
    }
    burstPlayTimer = setInterval(() => {
      const activeB = currentBurst();
      if (!sh.el.isConnected || viewMode !== 'bursts' || !activeB || !selectedCam || burstKeyFor(selectedCam, activeB) !== burstKeyFor(selectedCam, b)) {
        stopBurstPlay();
        return;
      }
      const nextIdx = nextBurstPlayIndex(frames, burstCursorMs);
      if (nextIdx < 0) {
        stopBurstPlay();
        return;
      }
      burstCursorMs = clamp(frames[nextIdx].tsMs, activeB.startMs, activeB.endMs);
      paintBurstFrameLocal();
    }, 180);
  };

  const prefetchBurstNeighbor = (camReq: string, idx: number): void => {
    if (idx < 0 || idx >= bursts.length) return;
    const nb = bursts[idx];
    const nKey = burstKeyFor(camReq, nb);
    if (burstPackCache.has(nKey)) return;
    burstPrefetchAbort?.abort();
    const ctrl = new AbortController();
    burstPrefetchAbort = ctrl;
    void gymCam.fetchTapePack(sid, camReq, nb.startMs, nb.endMs, 60, ctrl.signal).then((frames) => {
      if (ctrl.signal.aborted || !sh.el.isConnected || camReq !== selectedCam || burstPackCache.has(nKey)) {
        for (const f of frames) URL.revokeObjectURL(f.url);
        return;
      }
      burstPackCache.set(nKey, frames);
    });
  };

  const ensureBurstPack = (b: GymCamBurst): void => {
    if (!selectedCam) return;
    const key = burstKeyFor(selectedCam, b);
    const hit = burstPackCache.get(key);
    if (hit) {
      burstPackKey = key;
      burstPackLoading = false;
      paintBurstFrameLocal();
      if (wantBurstAutoPlay && !burstPlayTimer && hit.length) {
        wantBurstAutoPlay = false;
        startBurstPlayLoop(b, hit);
      }
      prefetchBurstNeighbor(selectedCam, burstIdx - 1);
      return;
    }
    if (burstPackKey === key && burstPackLoading) {
      paintBurstFrameLocal();
      return;
    }
    burstPackAbort?.abort();
    const ctrl = new AbortController();
    burstPackAbort = ctrl;
    burstPackKey = key;
    burstPackLoading = true;
    paintBurstFrameLocal();
    const camReq = selectedCam;
    void gymCam.fetchTapePack(sid, camReq, b.startMs, b.endMs, 60, ctrl.signal).then((frames) => {
      if (ctrl.signal.aborted || !sh.el.isConnected || camReq !== selectedCam) {
        for (const f of frames) URL.revokeObjectURL(f.url);
        return;
      }
      const prev = burstPackCache.get(key);
      if (prev) for (const oldF of prev) URL.revokeObjectURL(oldF.url);
      burstPackCache.set(key, frames);
      if (burstPackKey === key) burstPackLoading = false;
      lastBurstPackIdx = -1;
      if (viewMode === 'bursts' && currentBurst() && burstKeyFor(camReq, currentBurst()!) === key) {
        paintBurstFrameLocal();
        if (wantBurstAutoPlay) {
          wantBurstAutoPlay = false;
          if (frames.length) startBurstPlayLoop(b, frames);
          else stopBurstPlay();
        }
        prefetchBurstNeighbor(camReq, burstIdx - 1);
      }
    });
  };

  const toggleBurstPlay = (): void => {
    if (burstPlayTimer != null || wantBurstAutoPlay) {
      stopBurstPlay();
      return;
    }
    const b = currentBurst();
    if (!b || !selectedCam) return;
    const frames = burstPackCache.get(burstKeyFor(selectedCam, b));
    if (frames?.length) {
      startBurstPlayLoop(b, frames);
      return;
    }
    wantBurstAutoPlay = true;
    const btn = sh.body.querySelector<HTMLButtonElement>('[data-bplay]');
    if (btn) {
      btn.classList.add('on');
      btn.textContent = '⏸';
    }
    ensureBurstPack(b);
  };

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
        <div class="vcam-pick${cams.length > 2 ? ' many' : ''}">
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
      const curPos = clamp(burstCursorMs || Math.round((b.startMs + b.endMs) / 2), b.startMs, b.endMs);
      burstCursorMs = curPos;
      const pos = burstScrubPosition(b.startMs, b.endMs, curPos);
      const bKey = burstKeyFor(selectedCam, b);
      const cachedFrames = burstPackCache.get(bKey);
      const initBurstIdx = cachedFrames?.length ? findNearestFrameIndex(cachedFrames, curPos) : -1;
      const initBurstUrl = initBurstIdx >= 0 ? cachedFrames![initBurstIdx].url : (b.thumb || '');
      lastBurstPackIdx = initBurstIdx;
      const bSpan = Math.max(1, b.endMs - b.startMs);
      const bAct = (timeline?.activity ?? []).filter((pt) => pt.tMs >= b.startMs && pt.tMs <= b.endMs);
      const bMaxE = Math.max(4, ...bAct.map((pt) => pt.e));
      const bBarsHtml = bAct.map((pt) => {
        const leftPct = clamp(((pt.tMs - b.startMs) / bSpan) * 100, 0, 100);
        const hPct = pt.e > 0.3 ? clamp((pt.e / bMaxE) * 100, 10, 100) : 6;
        const hot = pt.e >= 2.2 ? ' hot' : '';
        return `<i class="vscrub-bar${hot}" style="left:${leftPct.toFixed(2)}%;height:${hPct.toFixed(0)}%"></i>`;
      }).join('');
      const rel = t.set.at != null ? fmtRelativeToCheck(b.endMs, checkedMs) : '';
      box.innerHTML = `
        ${headHtml}
        <div class="vburst-card">
          <div class="vscrub-stage vburst-stage" id="vburstStage" data-bstage>
            <img class="vscrub-img" id="vburstImg" alt="${esc(shortCamLabel(selectedCam))} burst preview" ${initBurstUrl ? `src="${initBurstUrl}"` : 'hidden'}>
            <div class="vburst-loading" id="vburstSpin" ${initBurstIdx >= 0 || !burstPackLoading ? 'hidden' : ''}>Loading scrub frames…</div>
            <div class="vscrub-ov l">${esc(shortCamLabel(selectedCam))} · ${burstIdx + 1}/${bursts.length}</div>
            <div class="vscrub-ov r" id="vburstTime">${fmtVideoDuration(pos.elapsedSec)} / ${fmtVideoDuration(pos.totalSec)} · ${fmtEpochClock(curPos)}</div>
          </div>
          <div class="vburst-scrub-row">
            <button class="vburst-play${burstPlayTimer || wantBurstAutoPlay ? ' on' : ''}" type="button" data-bplay aria-label="Play or pause burst preview">${burstPlayTimer || wantBurstAutoPlay ? '⏸' : '▶'}</button>
            <div class="vscrub-rail vburst-rail" id="vburstRail" data-brail>
              <div class="vburst-fill" id="vburstFill" style="width:${pos.pct.toFixed(2)}%"></div>
              <div class="vscrub-bars">${bBarsHtml}</div>
              <div class="vscrub-head" id="vburstHead" style="left:${pos.pct.toFixed(2)}%"></div>
            </div>
          </div>
          <div class="vscrub-edges"><span>0:00 (${fmtEpochClock(b.startMs)})</span><span>Drag bar or video to scrub</span><span>${fmtVideoDuration(pos.totalSec)}</span></div>
          <div class="vburst-bar">
            <div><b>Burst ${burstIdx + 1} of ${bursts.length} · ${b.durationSec}s</b>${rel ? `<span class="vburst-rel">${esc(rel)}</span>` : ''}</div>
            <small>${fmtEpochClock(b.startMs)} – ${fmtEpochClock(b.endMs)}</small>
          </div>
          ${bursts.length > 1 ? `<div class="vburst-nav">
            <button class="btn sm" type="button" data-bnav="-1" ${burstIdx <= 0 ? 'disabled' : ''}>◀ Older</button>
            <button class="btn sm" type="button" data-bnav="1" ${burstIdx >= bursts.length - 1 ? 'disabled' : ''}>Newer ▶</button>
          </div>` : ''}
          <div class="vsnd-opts">
            <label class="vsnd-chk"><input type="checkbox" data-vsnd-prev${previewAudioOn ? ' checked' : ''}><span>🔊 Preview sound</span></label>
            <label class="vsnd-chk"><input type="checkbox" data-vsnd-save${saveWithSound ? ' checked' : ''}><span>🎙 Include room audio</span></label>
          </div>
          <button class="btn primary big" type="button" data-bsave>${saveWithSound ? `Save Clip w/ Sound (${b.durationSec}s)` : `Save Clip (${b.durationSec}s)`} to ${esc(setLabel)}</button>
          <button class="btn sm" type="button" data-bscrub>Adjust IN/OUT Trim ▸</button>
        </div>`;
      ensureBurstPack(b);
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
            ${outMs > inMs ? `<button class="vscrub-z${zoomMode === 'clip' ? ' on' : ''}" type="button" data-szoom="clip">Fit Clip</button>` : ''}
            ${totalMin > 5 ? `<button class="vscrub-z${zoomMode === '5' ? ' on' : ''}" type="button" data-szoom="5">5m</button>` : ''}
            ${totalMin > 15 ? `<button class="vscrub-z${zoomMode === '15' ? ' on' : ''}" type="button" data-szoom="15">15m</button>` : ''}
            <button class="vscrub-z${zoomMode === 'all' ? ' on' : ''}" type="button" data-szoom="all">All</button>
          </div>
          <button class="vscrub-z" type="button" data-sjmp="chk">Jump to ✓</button>
        </div>

        <div class="vscrub-rail" id="vscrubRail" data-srail>
          <div class="vscrub-bars">${barsHtml}</div>
          ${winWidth > 0 ? `<div class="vscrub-win" style="left:${winLeft.toFixed(2)}%;width:${winWidth.toFixed(2)}%"></div>` : ''}
          ${chkInView ? `<div class="vscrub-chk" style="left:${chkPct.toFixed(2)}%"><span>✓</span></div>` : ''}
          <div class="vscrub-head" id="vscrubHead" style="left:${curPct.toFixed(2)}%"></div>
        </div>
        <div class="vscrub-edges"><span>${fmtEpochClock(winStart)}</span><span>${fmtEpochClock(winEnd)}</span></div>

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
        <div class="vsnd-opts">
          <label class="vsnd-chk"><input type="checkbox" data-vsnd-prev${previewAudioOn ? ' checked' : ''}><span>🔊 Preview sound</span></label>
          <button class="btn sm" type="button" data-slisten${previewAudioOn ? '' : ' hidden'}>${scrubListenOn ? '⏸ Stop Audio' : '▶ Listen'}</button>
          <label class="vsnd-chk"><input type="checkbox" data-vsnd-save${saveWithSound ? ' checked' : ''}><span>🎙 Include room audio</span></label>
        </div>
        <button class="btn primary big" type="button" data-ssave>${saveWithSound ? `Save Clip w/ Sound (${clipSec}s)` : `Save Clip (${clipSec}s)`} to ${esc(setLabel)}</button>
      </div>`;

    ensurePackForCurrentWindow();
  };

  const selectCamera = async (camName: string): Promise<void> => {
    const gen = ++loadGen;
    selectedCam = camName;
    loadingCam = true;
    autoScrubHint = '';
    clearPack();
    clearBurstPacks();
    scrubDirty = false;
    burstCursorMs = 0;
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
    inMarked = false;
    outMarked = false;
    customWin = null;
    zoomMode = totalMin > 15 ? '15' : 'all';

    if (bursts.length) {
      burstIdx = pickNearestBurstIndex(bursts, checkedMs);
      viewMode = 'bursts';
      const b = bursts[Math.max(0, burstIdx)];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      burstCursorMs = Math.round((b.startMs + b.endMs) / 2);
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
    if (viewMode === 'scrub' && minMs && maxMs) ensurePackForCurrentWindow();
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

  const applyBurstRailPointer = (clientX: number): void => {
    const rail = sh.body.querySelector<HTMLElement>('#vburstRail');
    const b = currentBurst();
    if (!rail || !b) return;
    const rect = rail.getBoundingClientRect();
    const pct = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    burstCursorMs = clamp(Math.round(b.startMs + pct * (b.endMs - b.startMs)), b.startMs, b.endMs);
    paintBurstFrameLocal();
  };

  sh.body.addEventListener('pointerdown', (e) => {
    const el = e.target as HTMLElement;
    const sRail = el.closest<HTMLElement>('[data-srail]');
    if (sRail) {
      e.preventDefault();
      dragScrub = true;
      sRail.setPointerCapture(e.pointerId);
      applyRailPointer(e.clientX);
      return;
    }
    const bRail = el.closest<HTMLElement>('[data-brail]');
    if (bRail) {
      e.preventDefault();
      stopBurstPlay();
      dragBurstScrub = true;
      bRail.setPointerCapture(e.pointerId);
      applyBurstRailPointer(e.clientX);
      return;
    }
    const bStage = el.closest<HTMLElement>('[data-bstage]');
    if (bStage) {
      const b = currentBurst();
      if (b) {
        burstStageDrag = {
          pointerId: e.pointerId,
          startX: e.clientX,
          startY: e.clientY,
          startCursorMs: burstCursorMs || Math.round((b.startMs + b.endMs) / 2),
          active: false,
        };
      }
    }
  });
  sh.body.addEventListener('pointermove', (e) => {
    if (dragScrub) {
      applyRailPointer(e.clientX);
      return;
    }
    if (dragBurstScrub) {
      applyBurstRailPointer(e.clientX);
      return;
    }
    if (burstStageDrag && e.pointerId === burstStageDrag.pointerId) {
      const dx = e.clientX - burstStageDrag.startX;
      const dy = e.clientY - burstStageDrag.startY;
      if (!burstStageDrag.active) {
        if (Math.abs(dy) >= 8 && Math.abs(dy) > Math.abs(dx)) {
          burstStageDrag = null;
          return;
        }
        if (Math.abs(dx) >= 6 && Math.abs(dx) > Math.abs(dy)) {
          burstStageDrag.active = true;
          stopBurstPlay();
          sh.body.querySelector<HTMLElement>('#vburstStage')?.setPointerCapture(e.pointerId);
        } else {
          return;
        }
      }
      e.preventDefault();
      const stage = sh.body.querySelector<HTMLElement>('#vburstStage');
      const b = currentBurst();
      if (!stage || !b) return;
      const w = Math.max(120, stage.getBoundingClientRect().width);
      const deltaMs = Math.round((dx / w) * Math.max(1000, b.endMs - b.startMs));
      burstCursorMs = clamp(burstStageDrag.startCursorMs + deltaMs, b.startMs, b.endMs);
      paintBurstFrameLocal();
    }
  });
  const endScrubDrag = (): void => {
    dragScrub = false;
    dragBurstScrub = false;
    burstStageDrag = null;
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
      const vidId = unlink.dataset.unlink!;
      const oldMeta = lifts.videos.get(vidId);
      await lifts.deleteVideo(vidId);
      if (oldMeta?.source === 'gymcam') void gymCam.deleteClip(oldMeta);
      await resetTimerIfIdle(t.session);
      redrawLinked();
      toast(oldMeta?.source === 'gymcam' ? 'Clip deleted from set, OptiPlex & Drive' : 'Video unlinked');
      return;
    }

    const pickCam = el.closest<HTMLElement>('[data-pickcam]');
    if (pickCam) {
      const picked = pickCam.dataset.pickcam!;
      t.se.cams = [picked];
      delete t.se.camPref;
      lifts.saveSession(t.session, true);
      void selectCamera(picked);
      return;
    }

    const vmode = el.closest<HTMLElement>('[data-vmode]');
    if (vmode) {
      stopBurstPlay();
      viewMode = vmode.dataset.vmode as 'bursts' | 'scrub';
      if (viewMode === 'scrub' && minMs && maxMs && !scrubDirty) {
        const b = bursts[burstIdx];
        if (b) {
          inMs = clamp(b.startMs, minMs, maxMs);
          outMs = clamp(b.endMs, inMs + 3000, maxMs);
          cursorMs = clamp(burstCursorMs || Math.round((inMs + outMs) / 2), inMs, outMs);
        }
        const { winStart, winEnd } = currentWindow();
        if (!winCenterMs || cursorMs < winStart || cursorMs > winEnd) winCenterMs = cursorMs;
      }
      renderGymCam();
      return;
    }

    if (el.closest('[data-bplay]')) {
      toggleBurstPlay();
      return;
    }

    const bnav = el.closest<HTMLElement>('[data-bnav]');
    if (bnav && bursts.length) {
      stopBurstPlay();
      burstIdx = clamp(burstIdx + Number(bnav.dataset.bnav), 0, bursts.length - 1);
      const b = bursts[burstIdx];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      burstCursorMs = Math.round((b.startMs + b.endMs) / 2);
      cursorMs = burstCursorMs;
      lastBurstPackIdx = -1;
      renderGymCam();
      return;
    }

    if (el.closest('[data-bscrub]') && bursts[burstIdx]) {
      stopBurstPlay();
      const b = bursts[burstIdx];
      inMs = minMs ? clamp(b.startMs, minMs, maxMs) : b.startMs;
      outMs = maxMs ? clamp(b.endMs, inMs + 3000, maxMs) : b.endMs;
      cursorMs = clamp(burstCursorMs || Math.round((inMs + outMs) / 2), inMs, outMs);
      inMarked = true;
      outMarked = true;
      zoomToClip('both_set');
      scrubDirty = true;
      viewMode = 'scrub';
      renderGymCam();
      return;
    }

    const szoom = el.closest<HTMLElement>('[data-szoom]');
    if (szoom) {
      const raw = szoom.dataset.szoom! as 'clip' | '5' | '15' | 'all';
      if (raw === 'clip') {
        zoomToClip(inMarked && !outMarked ? 'in_set' : 'both_set');
      } else {
        zoomMode = raw;
        customWin = null;
        winCenterMs = cursorMs;
        lastPackIdx = -1;
      }
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
        if (zoomMode === 'clip' && customWin) {
          const wSpan = customWin.winEnd - customWin.winStart;
          const ns = clamp(cursorMs - Math.round(wSpan / 2), minMs, Math.max(minMs, maxMs - wSpan));
          customWin = { winStart: ns, winEnd: Math.min(maxMs, ns + wSpan) };
        } else {
          winCenterMs = cursorMs;
        }
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
      if (side === 'in') {
        inMarked = true;
        zoomToClip(outMarked ? 'both_set' : 'in_set');
      } else {
        outMarked = true;
        zoomToClip('both_set');
      }
      renderGymCam();
      return;
    }

    const bsave = el.closest<HTMLButtonElement>('[data-bsave]');
    if (bsave && bursts[burstIdx] && selectedCam) {
      stopBurstPlay();
      const b = bursts[burstIdx];
      bsave.disabled = true;
      bsave.textContent = 'Saving…';
      const meta = await gymCam.saveWindowClip(t, selectedCam, b.startMs, b.endMs, {
        includeAudio: saveWithSound,
      });
      if (meta) {
        toast(meta.hasAudio ? 'Clip + room audio saved & linked' : 'Clip saved & linked');
        sh.close();
      } else {
        bsave.disabled = false;
        bsave.textContent = `${saveWithSound ? `Save Clip w/ Sound (${b.durationSec}s)` : `Save Clip (${b.durationSec}s)`} to ${setLabel}`;
        toast('Could not save clip from Gym Cam');
      }
      return;
    }

    const ssave = el.closest<HTMLButtonElement>('[data-ssave]');
    if (ssave && selectedCam && minMs && maxMs) {
      stopPreviewAudio(false);
      const dur = Math.max(1, Math.round((outMs - inMs) / 1000));
      ssave.disabled = true;
      ssave.textContent = 'Saving…';
      const meta = await gymCam.saveWindowClip(t, selectedCam, inMs, outMs, {
        includeAudio: saveWithSound,
      });
      if (meta) {
        toast(meta.hasAudio ? 'Clip + room audio saved & linked' : 'Clip saved & linked');
        sh.close();
      } else {
        ssave.disabled = false;
        ssave.textContent = `${saveWithSound ? `Save Clip w/ Sound (${dur}s)` : `Save Clip (${dur}s)`} to ${setLabel}`;
        toast('Could not save clip from Gym Cam');
      }
      return;
    }

    const sListen = el.closest<HTMLButtonElement>('[data-slisten]');
    if (sListen && minMs && maxMs) {
      if (scrubListenOn) {
        stopPreviewAudio(false);
      } else {
        scrubListenOn = true;
        sListen.textContent = '⏸ Stop Audio';
        playPreviewAudioRange(inMs, outMs, inMs);
      }
      return;
    }

    if (el.closest('[data-varmall]')) {
      const assigned = camsForExercise(ex, lifts.row(t.se.exId), t.se, gymCam.cameras);
      startTimerForVideo(t.session);
      t.se.cams = [...assigned];
      delete t.se.camPref;
      lifts.saveSession(t.session, true);
      gymCam.armSet(t.session, t.se, t.set, assigned);
      sh.close();
      toast(`Recording ${assigned.map(shortCamLabel).join(' + ')} · ${setLabel} — tap ✓ or red camera to stop`);
      return;
    }

    const warmCam = el.closest<HTMLElement>('[data-varmcam]')?.dataset.varmcam;
    if (warmCam) {
      startTimerForVideo(t.session);
      t.se.cams = [warmCam];
      delete t.se.camPref;
      lifts.saveSession(t.session, true);
      gymCam.armSet(t.session, t.se, t.set, [warmCam]);
      sh.close();
      toast(`Recording ${shortCamLabel(warmCam)} · ${setLabel} — tap ✓ or red camera to stop`);
      return;
    }

    const act = el.closest<HTMLElement>('[data-vact]')?.dataset.vact;
    if (act === 'gymarm') {
      const assigned = camsForExercise(ex, lifts.row(t.se.exId), t.se, gymCam.cameras);
      startTimerForVideo(t.session);
      gymCam.armSet(t.session, t.se, t.set, assigned);
      sh.close();
      toast(`Recording ${assigned.map(shortCamLabel).join(' + ')} · ${setLabel} — tap ✓ or camera to stop`);
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

  sh.body.addEventListener('change', (e) => {
    const inp = e.target as HTMLInputElement;
    if (inp.matches('[data-vsnd-prev]')) {
      previewAudioOn = inp.checked;
      const lbtn = sh.body.querySelector<HTMLButtonElement>('[data-slisten]');
      if (lbtn) lbtn.hidden = !previewAudioOn;
      if (!previewAudioOn) {
        stopPreviewAudio(false);
      } else if (viewMode === 'bursts' && burstPlayTimer != null) {
        const b = currentBurst();
        if (b) playPreviewAudioRange(b.startMs, b.endMs, burstCursorMs || b.startMs);
      }
      return;
    }
    if (inp.matches('[data-vsnd-save]')) {
      saveWithSound = inp.checked;
      const b = currentBurst();
      const bsave = sh.body.querySelector<HTMLButtonElement>('[data-bsave]');
      if (bsave && b && !bsave.disabled) {
        bsave.textContent = `${saveWithSound ? `Save Clip w/ Sound (${b.durationSec}s)` : `Save Clip (${b.durationSec}s)`} to ${setLabel}`;
      }
      const ssave = sh.body.querySelector<HTMLButtonElement>('[data-ssave]');
      if (ssave && minMs && maxMs && !ssave.disabled) {
        const dur = Math.max(1, Math.round((outMs - inMs) / 1000));
        ssave.textContent = `${saveWithSound ? `Save Clip w/ Sound (${dur}s)` : `Save Clip (${dur}s)`} to ${setLabel}`;
      }
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

let wbReplayTimer: ReturnType<typeof setTimeout> | null = null;

/** Clear any active form replay overlay on the Gym Wallboard TV. */
export function closeWallboardReplay(): void {
  if (wbReplayTimer) { clearTimeout(wbReplayTimer); wbReplayTimer = null; }
  setWallboardReplay(null);
  void wallboard.pushNow();
}

/** Cast a Gym-Cam clip onto the Fire TV Wallboard overlay for 2 auto-looped plays. */
export function castClipToWallboard(
  meta: VideoMeta,
  opts?: { rate?: number; exName?: string; setLabel?: string; trimStartSec?: number; trimEndSec?: number },
): boolean {
  const url = gymCam.resolveLanUrl(meta.lanUrl);
  if (!url) return false;
  const trimStartSec = Math.max(0, opts?.trimStartSec ?? meta.trimStartSec ?? 0);
  const trimEndSec = opts?.trimEndSec ?? meta.trimEndSec ?? (meta.durationSec ?? 0);
  const rate = opts?.rate && opts.rate > 0 ? opts.rate : 1;
  const rawSpan = trimEndSec > trimStartSec + 0.15 ? trimEndSec - trimStartSec : (meta.durationSec || 20);
  const durMs = Math.max(12_000, Math.min(180_000, Math.round(((rawSpan * 2) / rate) * 1000) + 4000));
  const title = opts?.exName || (meta.guest ? 'Guest Clip' : lifts.exOrStub(meta.exId).name);
  const subtitle = [opts?.setLabel, meta.camera].filter(Boolean).join(' · ');
  const now = Date.now();
  setWallboardReplay({
    id: meta.id,
    url,
    title,
    subtitle,
    camera: meta.camera || 'Gym Cam',
    rate,
    trimStartSec,
    trimEndSec,
    startedAtMs: now,
    expiresAtMs: now + durMs,
  });
  void wallboard.pushNow();
  if (wbReplayTimer) clearTimeout(wbReplayTimer);
  wbReplayTimer = setTimeout(() => {
    wbReplayTimer = null;
    setWallboardReplay(null);
    void wallboard.pushNow();
  }, durMs + 250);
  return true;
}

/** Play, slow-mo analyze, frame-jog, precision-trim, and export/share a lift clip. */
export async function playVideo(id: string, ctx?: { target?: VideoTarget; setLabel?: string }): Promise<void> {
  let meta = lifts.videos.get(id);
  if (!meta) return;
  let primaryBlob: Blob | null = sessionFiles.get(id) ?? (meta.linkOnly ? null : await lifts.videoBlob(id));
  if (!primaryBlob && !meta.linkOnly && !meta.fileName && !meta.uri && !meta.driveQuery && !meta.lanUrl) {
    toast('Video file is missing');
    return;
  }

  let primaryObjectUrl = primaryBlob ? URL.createObjectURL(primaryBlob) : '';
  let primaryPlayUrl = primaryObjectUrl || '';
  if (!primaryPlayUrl && isNative() && meta.uri?.startsWith('content://')) {
    primaryPlayUrl = (await getPlayableNativeVideoUrl(meta.uri, meta.mime)) ?? '';
  }
  const needsLanLoad = !primaryPlayUrl && !!meta.lanUrl;
  const canHaveDeck = !!(primaryPlayUrl || meta.lanUrl);

  const s = lifts.sessions.get(meta.sessionId);
  const se = ctx?.target?.se ?? s?.exs.find((e) => e.sets.some((x) => x.id === meta!.setId));
  const set = ctx?.target?.set ?? se?.sets.find((x) => x.id === meta!.setId);
  const resolvedTarget: VideoTarget | undefined =
    ctx?.target ?? (s && se && set ? { session: s, se, set } : undefined);
  const ex = lifts.exOrStub(meta.exId);
  const clipTitle = meta.guest ? 'Guest Clip' : ex.name;
  const driveUrl = driveUrlFor(meta);
  const isDirectDrive = !!(meta.uri && /^https?:\/\/drive\.google\.com\/file\/d\//i.test(meta.uri));
  const driveBtnLabel = isDirectDrive ? '▶ Play in Google Drive ↗' : '▶ Open in Google Drive ↗';
  const hasLocalUri = isNative() && !!meta.uri?.startsWith('content://');
  const setTxt = set ? `${set.w ? `${fmtKg(set.w)} ${liftSettings().unit}` : ex.type === 'bodyweight' ? 'BW' : '–'} × ${set.r ?? 0}` : '';
  const dateTxt = new Date(meta.fileMs ?? meta.ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  const subTxt = [setTxt, dateTxt, meta.camera].filter(Boolean).join(' · ');

  let st: TrimState = createTrimState(meta.durationSec ?? 0, meta.trimStartSec, meta.trimEndSec);
  let railZoomed = isTrimmed(st);
  let railView: TrimViewport = computeTrimViewport(st, railZoomed);
  let speed = 1;
  let shareOpen = false;
  let uiTicker: ReturnType<typeof setInterval> | null = null;
  let closed = false;
  const dlCtrl = new AbortController();

  let viewCam = meta.camera || '';
  let viewLanUrl = meta.lanUrl || '';
  let viewBlob: Blob | null = primaryBlob;
  let viewFileName = meta.fileName || '';
  let viewIsPreview = false;
  let viewSiblingMeta: VideoMeta | null = null;
  let loadingCam: string | null = null;
  let switchGen = 0;
  let restoreAfterLoad: { time: number; paused: boolean } | null = null;
  let tapeAnglesChecked = false;
  let tapeAngles: string[] = [];
  let tapeAudioAvail = false;
  let soundPreviewOn = false;
  let loadedAudUrl = '';
  let lastAnglesSig = '';
  const angleCache = new Map<string, { playUrl: string; objectUrl: string; blob: Blob | null; lanUrl: string; fileName: string }>();

  const clearAngleCache = (): void => {
    for (const entry of angleCache.values()) {
      if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
    }
    angleCache.clear();
  };

  const refreshRailView = (wantZoom: boolean): void => {
    railView = computeTrimViewport(st, wantZoom);
    railZoomed = railView.zoomed;
  };

  const posterHtml = meta.thumb
    ? `<div class="pv-poster" id="vpPoster"${primaryPlayUrl ? ' hidden' : ''} style="background-image:url(${meta.thumb})"><span class="pv-badge">${needsLanLoad ? 'Connecting to OptiPlex…' : (meta.source === 'gymcam' ? 'Gym Cam · Google Drive Backup' : 'DCIM Backup · Google Drive')}</span></div>`
    : `<div class="pv-poster" id="vpPoster" hidden><span class="pv-badge">Google Drive Backup</span></div>`;

  const clipRefMs = (): number | null => meta!.clipEndMs ?? meta!.clipStartMs ?? null;
  const withinRetention = (): boolean => isTapeWithinRetention(clipRefMs(), gymCam.retentionDays);
  const tapeAvail = (): boolean =>
    meta!.source === 'gymcam' &&
    gymCam.online &&
    meta!.clipStartMs != null &&
    withinRetention() &&
    (!tapeAnglesChecked || tapeAngles.length > 0);

  const getSavedSiblings = (): Map<string, VideoMeta> => {
    const out = new Map<string, VideoMeta>();
    for (const vidId of set?.vids ?? []) {
      if (vidId === meta!.id) continue;
      const v = lifts.videos.get(vidId);
      if (v && v.source === 'gymcam' && v.camera && v.camera !== meta!.camera && v.lanUrl) {
        if (!out.has(v.camera)) out.set(v.camera, v);
      }
    }
    return out;
  };

  const sh = openSheet(
    `<div class="vp-head">
       <div class="vp-title"><h3 class="sh-title">${esc(clipTitle)}</h3><p class="sh-text" id="vpSub">${esc(subTxt)}</p></div>
       <div class="vp-pills">
         ${canHaveDeck ? '<button class="vp-pill on" type="button" id="vpSaveTop" data-vtrim="save" hidden>Save Trim</button>' : ''}
         ${meta.source === 'gymcam' ? `<button class="vp-pill" type="button" id="vpTvBtn" data-vp="tv"${meta.lanUrl && gymCam.online ? '' : ' hidden'}>📺 TV</button>` : ''}
         <button class="vp-pill" type="button" data-vp="share">Share ↗</button>
       </div>
     </div>
     <div class="vp-stage">
       ${canHaveDeck ? `<video class="pv" id="vpVid"${primaryPlayUrl ? ` src="${esc(primaryPlayUrl)}"` : ' hidden'} playsinline muted autoplay loop preload="auto"></video><audio id="vpAud" preload="none" hidden></audio>` : ''}
       ${posterHtml}
       <p class="hint" id="vpLoadMsg"${needsLanLoad ? '' : ' hidden'} style="text-align:center">Loading from OptiPlex on Gym Wi-Fi…</p>
       <p class="hint" id="vpLanErr" hidden style="text-align:center">Away from Gym Wi-Fi — stream this clip from Google Drive below.</p>
     </div>
     ${canHaveDeck ? `
     <div class="vp-deck" id="vpDeck"${primaryPlayUrl ? '' : ' hidden'}>
       <div class="vp-readout">
         <span><span class="vp-time" id="vpTime">0:00.00 / 0:00.00</span> · <span class="vp-frame" id="vpFrame">f 0</span></span>
         <span class="vp-rbtns">
           <label class="vp-snd-chk" id="vpSoundLbl" hidden><input type="checkbox" id="vpSoundChk"><span>🔊 Sound</span></label>
           <button class="vp-zoom" type="button" id="vpZoomBtn" data-vzoom hidden>⤢ Full</button>
           <button class="vp-focus" type="button" id="vpFocus" data-vfocus-cycle>Full clip</button>
         </span>
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
       <div class="vp-edges" id="vpEdges" hidden><span id="vpEdgeL"></span><span id="vpEdgeR"></span></div>
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
       <div class="vp-angle-row" id="vpAngleRow" hidden>
         <div class="vp-angle-top">
           <small id="vpAngleLbl">Angles</small>
           <div class="vp-angle-pills" id="vpAnglePills"></div>
         </div>
         <div class="vp-angle-acts" id="vpAngleActs" hidden>
           <span class="vp-angle-note" id="vpAngleNote">Temp preview</span>
           <div class="vp-angle-btns">
             <button class="vp-nbtn" type="button" id="vpSwapPrimary" data-vcam-swap>Swap Primary</button>
             <button class="vp-nbtn pri" type="button" id="vpSaveBoth" data-vcam-saveboth>+ Save Both</button>
           </div>
         </div>
       </div>
       <div class="vp-tape-row" id="vpTapeRow"${tapeAvail() ? '' : ' hidden'}>
         <small id="vpTapeLbl">2-Wk Tape</small>
         <button class="vp-nbtn" type="button" data-vtape="5:0">−5s Earlier</button>
         <button class="vp-nbtn" type="button" data-vtape="0:5">+5s Later</button>
       </div>
       <div class="vp-audio-row" id="vpAudioRow" hidden>
         <small id="vpAudioLbl">🔇 Silent clip</small>
         <button class="vp-nbtn pri" type="button" id="vpAudioToggle" data-vaudio>+ Add Room Audio</button>
       </div>
     </div>` : ''}
     <div class="vp-drawer" id="vpShareBox" hidden>
       <div class="vp-dr-head">
         <span>Export &amp; Share</span>
         <button type="button" data-vp="share" aria-label="Close share">✕</button>
       </div>
       <div class="vp-share-aud" id="vpShareAudioBadge">🔇 Silent MP4 — safe to share</div>
       <button class="btn primary big" type="button" data-vexp="share">Share Video…</button>
       <button class="btn big" type="button" data-vexp="save">Save MP4 to Gallery</button>
       ${driveUrl ? `<a class="btn big" href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>${driveBtnLabel}</a>` : ''}
     </div>
     ${meta.fileName ? `<div class="vfile"><small>LINKED FILE${meta.driveQuery ? ' · TIMESTAMP MATCHED' : ''}</small><b id="vpFileName">${esc(meta.fileName)}</b></div>` : ''}
     ${driveUrl ? `<a class="btn primary big" id="vpDriveFallback"${canHaveDeck ? ' hidden' : ''} href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>${driveBtnLabel}</a>` : ''}
     ${hasLocalUri && !primaryPlayUrl ? '<button class="btn big" type="button" data-vlocal>▶ Play from phone DCIM</button>' : ''}
     ${ctx?.target ? '<button class="btn big" type="button" data-vmanage>+ Add / Manage Clips</button>' : ''}
     <div class="vp-foot-row">
       <button class="btn sm" type="button" data-vedit>Edit filename / URL</button>
       <button class="btn sm danger" type="button" data-del>${meta.linkOnly && meta.source !== 'gymcam' ? 'Unlink' : 'Delete'}</button>
     </div>
     <button class="btn big" type="button" data-ok>Close</button>`,
    {
      onClose: () => {
        closed = true;
        dlCtrl.abort();
        if (uiTicker) clearInterval(uiTicker);
        const a = sh.body.querySelector<HTMLAudioElement>('#vpAud');
        if (a) {
          a.pause();
          a.removeAttribute('src');
          a.load();
        }
        if (primaryObjectUrl) URL.revokeObjectURL(primaryObjectUrl);
        clearAngleCache();
      },
    },
  );

  const vid = sh.body.querySelector<HTMLVideoElement>('#vpVid');
  const aud = sh.body.querySelector<HTMLAudioElement>('#vpAud');
  const q = <T extends HTMLElement>(sel: string): T | null => sh.body.querySelector<T>(sel);

  const activeClipMeta = (): VideoMeta => (!viewIsPreview && viewSiblingMeta ? viewSiblingMeta : meta!);
  const activeHasBakedAudio = (): boolean => {
    if (viewIsPreview) return false;
    const am = activeClipMeta();
    return am.source !== 'gymcam' || am.hasAudio === true;
  };
  const activeLegacyMaybeAudio = (): boolean => {
    if (viewIsPreview || viewSiblingMeta) return false;
    return meta!.source === 'gymcam' && meta!.hasAudio === undefined;
  };
  const syncSoundPlayback = (forceSeek = false): void => {
    if (!vid) return;
    if (!soundPreviewOn) {
      vid.muted = true;
      if (aud && !aud.paused) aud.pause();
      return;
    }
    if (activeHasBakedAudio() || activeLegacyMaybeAudio()) {
      vid.muted = false;
      if (aud && !aud.paused) aud.pause();
      return;
    }
    vid.muted = true;
    const cStart = meta!.clipStartMs;
    const cEnd = meta!.clipEndMs ?? (cStart != null ? cStart + Math.max(5, meta!.durationSec ?? 20) * 1000 : null);
    if (!aud || !gymCam.online || cStart == null || cEnd == null || !tapeAudioAvail) {
      if (aud && !aud.paused) aud.pause();
      return;
    }
    const url = gymCam.roomAudioUrl(cStart, cEnd);
    if (loadedAudUrl !== url || aud.getAttribute('src') !== url) {
      loadedAudUrl = url;
      aud.src = url;
      aud.load();
    }
    aud.playbackRate = speed;
    const targetT = Math.max(0, vid.currentTime || 0);
    if (forceSeek || Math.abs((aud.currentTime || 0) - targetT) > 0.25) {
      try {
        if (aud.readyState >= 1) aud.currentTime = targetT;
      } catch {
        // ignore seek before ready
      }
    }
    if (vid.paused) {
      if (!aud.paused) aud.pause();
    } else if (aud.paused) {
      void aud.play().catch(() => {});
    }
  };

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
    if (soundPreviewOn && aud && loadedAudUrl && aud.readyState >= 1) {
      try {
        aud.currentTime = target;
      } catch {
        // ignore seek before ready
      }
    }
  };

  const fetchBlobUrl = async (
    lanUrl: string,
    bytesHint = 0,
  ): Promise<{ playUrl: string; objectUrl: string; blob: Blob | null } | null> => {
    if (bytesHint > MAX_LOCAL_CACHE_BYTES) {
      if (closed) return null;
      const directUrl = isNative() ? ((await getPlayableNativeVideoUrl(lanUrl, meta!.mime)) ?? lanUrl) : lanUrl;
      return { playUrl: directUrl, objectUrl: '', blob: null };
    }
    if (typeof fetch !== 'undefined') {
      const tCtrl = new AbortController();
      const onParentAbort = () => tCtrl.abort();
      dlCtrl.signal.addEventListener('abort', onParentAbort, { once: true });
      const timer = setTimeout(() => tCtrl.abort(), 12_000);
      try {
        const res = await fetch(lanUrl, { signal: tCtrl.signal, cache: 'no-store' });
        if (res.ok) {
          const dlBlob = await res.blob();
          if (closed) return null;
          if (dlBlob.size > 0 && dlBlob.size <= MAX_LOCAL_CACHE_BYTES) {
            const objectUrl = URL.createObjectURL(dlBlob);
            return { playUrl: objectUrl, objectUrl, blob: dlBlob };
          }
          if (dlBlob.size > MAX_LOCAL_CACHE_BYTES) {
            const directUrl = isNative() ? ((await getPlayableNativeVideoUrl(lanUrl, meta!.mime)) ?? lanUrl) : lanUrl;
            return { playUrl: directUrl, objectUrl: '', blob: null };
          }
        }
      } catch {
        // LAN unreachable — return null so off-LAN falls back cleanly
      } finally {
        clearTimeout(timer);
        dlCtrl.signal.removeEventListener('abort', onParentAbort);
      }
    }
    return null;
  };

  const resolveLanPlayable = async (lanUrl: string, bytesHint = 0): Promise<string> => {
    const res = await fetchBlobUrl(lanUrl, bytesHint);
    if (!res) return '';
    if (res.blob) {
      cacheSessionBlob(id, res.blob);
      primaryBlob = res.blob;
    }
    if (res.objectUrl) {
      if (primaryObjectUrl && primaryObjectUrl !== res.objectUrl) URL.revokeObjectURL(primaryObjectUrl);
      primaryObjectUrl = res.objectUrl;
    }
    primaryPlayUrl = res.playUrl;
    if (!viewIsPreview && !viewSiblingMeta) {
      viewBlob = primaryBlob;
      viewLanUrl = meta!.lanUrl || '';
      viewFileName = meta!.fileName || '';
      viewCam = meta!.camera || '';
    }
    return res.playUrl;
  };

  const refreshTapeAngles = async (): Promise<void> => {
    if (meta!.source !== 'gymcam' || !gymCam.online || meta!.clipStartMs == null || !withinRetention()) {
      tapeAngles = [];
      tapeAudioAvail = false;
      tapeAnglesChecked = false;
      syncUi();
      return;
    }
    const sid = s?.camSessionId ?? meta!.sessionId;
    const endMs = meta!.clipEndMs ?? (meta!.clipStartMs + Math.max(5, meta!.durationSec ?? 20) * 1000);
    const res = await gymCam.getTapeMediaInfo(sid, meta!.clipStartMs, endMs);
    if (closed) return;
    if (res !== null) {
      tapeAngles = res.cameras;
      tapeAudioAvail = res.audioAvailable;
      tapeAnglesChecked = true;
      syncUi();
    }
  };

  const restorePrimaryView = (): void => {
    if (!vid || !primaryPlayUrl) return;
    const resumeTime = Number.isFinite(vid.currentTime) ? vid.currentTime : st.playhead;
    const wasPaused = vid.paused;
    viewCam = meta!.camera || '';
    viewLanUrl = meta!.lanUrl || '';
    viewBlob = primaryBlob;
    viewFileName = meta!.fileName || '';
    viewIsPreview = false;
    viewSiblingMeta = null;
    loadingCam = null;
    restoreAfterLoad = { time: resumeTime, paused: wasPaused };
    vid.autoplay = !wasPaused;
    vid.src = primaryPlayUrl;
    vid.load();
    syncUi();
  };

  const switchToAngle = async (cam: string): Promise<void> => {
    if (!vid || !cam || (cam === viewCam && !loadingCam) || closed) return;
    const resumeTime = Number.isFinite(vid.currentTime) ? vid.currentTime : st.playhead;
    const wasPaused = vid.paused;
    const gen = ++switchGen;

    if (cam === (meta!.camera || '')) {
      if (!primaryPlayUrl) return;
      viewCam = meta!.camera || '';
      viewLanUrl = meta!.lanUrl || '';
      viewBlob = primaryBlob;
      viewFileName = meta!.fileName || '';
      viewIsPreview = false;
      viewSiblingMeta = null;
      loadingCam = null;
      restoreAfterLoad = { time: resumeTime, paused: wasPaused };
      vid.autoplay = !wasPaused;
      vid.src = primaryPlayUrl;
      vid.load();
      syncUi();
      return;
    }

    const sib = getSavedSiblings().get(cam);
    if (sib?.lanUrl) {
      const cKey = `sib|${sib.id}|${sib.lanUrl}`;
      let hit = angleCache.get(cKey);
      if (!hit) {
        loadingCam = cam;
        syncUi();
        const dl = await fetchBlobUrl(sib.lanUrl, sib.bytes);
        if (closed || gen !== switchGen) {
          if (dl?.objectUrl) URL.revokeObjectURL(dl.objectUrl);
          return;
        }
        if (!dl) {
          loadingCam = null;
          syncUi();
          toast(`Could not load ${shortCamLabel(cam)} clip`);
          return;
        }
        hit = { ...dl, lanUrl: sib.lanUrl, fileName: sib.fileName || '' };
        angleCache.set(cKey, hit);
      }
      viewCam = cam;
      viewLanUrl = hit.lanUrl;
      viewBlob = hit.blob;
      viewFileName = hit.fileName;
      viewIsPreview = false;
      viewSiblingMeta = sib;
      loadingCam = null;
      restoreAfterLoad = { time: resumeTime, paused: wasPaused };
      vid.autoplay = !wasPaused;
      vid.src = hit.playUrl;
      vid.load();
      syncUi();
      return;
    }

    if (!tapeAvail() || meta!.clipStartMs == null || meta!.clipEndMs == null) return;
    const cKey = `prev|${cam}|${meta!.clipStartMs}|${meta!.clipEndMs}`;
    let hit = angleCache.get(cKey);
    if (!hit) {
      loadingCam = cam;
      syncUi();
      const prev: GymCamPreviewResult | null = await gymCam.previewClipCamera(meta!, cam);
      if (closed || gen !== switchGen) return;
      if (!prev?.lanUrl) {
        loadingCam = null;
        syncUi();
        toast(`No ${shortCamLabel(cam)} tape for this timestamp`);
        return;
      }
      const dl = await fetchBlobUrl(prev.lanUrl, prev.bytes);
      if (closed || gen !== switchGen) {
        if (dl?.objectUrl) URL.revokeObjectURL(dl.objectUrl);
        return;
      }
      if (!dl) {
        loadingCam = null;
        syncUi();
        toast(`Could not load ${shortCamLabel(cam)} preview`);
        return;
      }
      hit = { ...dl, lanUrl: prev.lanUrl, fileName: prev.fileName };
      angleCache.set(cKey, hit);
    }
    viewCam = cam;
    viewLanUrl = hit.lanUrl;
    viewBlob = hit.blob;
    viewFileName = hit.fileName;
    viewIsPreview = true;
    viewSiblingMeta = null;
    loadingCam = null;
    restoreAfterLoad = { time: resumeTime, paused: wasPaused };
    vid.autoplay = !wasPaused;
    vid.src = hit.playUrl;
    vid.load();
    syncUi();
  };

  const syncUi = (): void => {
    if (closed) return;
    const subEl = q('#vpSub');
    if (subEl) {
      const camSub = viewCam ? (viewIsPreview ? `${viewCam} · preview` : viewCam) : '';
      subEl.textContent = [setTxt, dateTxt, camSub].filter(Boolean).join(' · ');
    }
    const tvBtn = q<HTMLButtonElement>('#vpTvBtn');
    if (tvBtn) tvBtn.hidden = !(gymCam.online && !!(viewLanUrl || meta!.lanUrl));

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
      const vStart = railView.end > railView.start ? railView.start : 0;
      const vEnd = railView.end > railView.start ? railView.end : total;
      const vSpan = Math.max(0.001, vEnd - vStart);
      const inPct = clamp(((st.start - vStart) / vSpan) * 100, 0, 100);
      const outPct = clamp(((st.end - vStart) / vSpan) * 100, inPct, 100);
      const headPct = clamp(((cur - vStart) / vSpan) * 100, 0, 100);
      if (railEl) railEl.classList.toggle('trimmed', trimmed);
      if (shadeL) shadeL.style.width = `${inPct}%`;
      if (shadeR) { shadeR.style.left = `${outPct}%`; shadeR.style.width = `${Math.max(0, 100 - outPct)}%`; }
      if (keepEl) { keepEl.style.left = `${inPct}%`; keepEl.style.width = `${Math.max(1, outPct - inPct)}%`; }
      if (headEl) headEl.style.left = `${headPct}%`;
      if (inH) { inH.style.left = `${inPct}%`; inH.classList.toggle('active', st.focus === 'in'); }
      if (outH) { outH.style.left = `${outPct}%`; outH.classList.toggle('active', st.focus === 'out'); }

      const zoomBtn = q<HTMLButtonElement>('#vpZoomBtn');
      if (zoomBtn) {
        zoomBtn.hidden = !trimmed || st.end - st.start >= total * 0.85;
        zoomBtn.textContent = railView.zoomed ? '⤢ Full' : '⤡ Fit Trim';
      }
      const edgesEl = q('#vpEdges');
      const edgeL = q('#vpEdgeL');
      const edgeR = q('#vpEdgeR');
      if (edgesEl) edgesEl.hidden = !railView.zoomed;
      if (edgeL) edgeL.textContent = fmtPreciseTime(vStart);
      if (edgeR) edgeR.textContent = fmtPreciseTime(vEnd);
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

    const tapeOk = tapeAvail();
    const daysLeft = tapeDaysRemaining(clipRefMs(), gymCam.retentionDays);
    const tapeRow = q('#vpTapeRow');
    const tapeLbl = q('#vpTapeLbl');
    if (tapeRow) tapeRow.hidden = !tapeOk;
    if (tapeLbl) tapeLbl.textContent = `Tape · ${daysLeft}d left`;

    const angleOpts =
      meta!.source === 'gymcam' && gymCam.online && !!primaryPlayUrl
        ? buildCoachAngleOptions({
            primaryCamera: meta!.camera,
            savedSiblingCameras: [...getSavedSiblings().keys()],
            tapeCameras: tapeOk ? (tapeAnglesChecked ? tapeAngles : gymCam.cameras) : [],
            configuredCameras: gymCam.cameras,
          })
        : [];
    const showAngles = angleOpts.length > 1;
    const angleRow = q('#vpAngleRow');
    const angleLbl = q('#vpAngleLbl');
    const anglePills = q('#vpAnglePills');
    const angleActs = q('#vpAngleActs');
    const angleNote = q('#vpAngleNote');
    const saveBothBtn = q<HTMLButtonElement>('#vpSaveBoth');
    if (angleRow) angleRow.hidden = !showAngles;
    if (angleLbl) angleLbl.textContent = tapeOk ? `Angles · ${daysLeft}d left` : 'Saved Angles';
    if (anglePills && showAngles) {
      const sig = angleOpts
        .map((o) => `${o.camera}:${o.kind}:${o.camera === viewCam ? 1 : 0}:${o.camera === loadingCam ? 1 : 0}`)
        .join('|');
      if (sig !== lastAnglesSig) {
        lastAnglesSig = sig;
        anglePills.innerHTML = angleOpts
          .map((o) => {
            const active = o.camera === viewCam;
            const loading = o.camera === loadingCam;
            const cls = `vp-ang${active ? ' on' : ''}${o.kind === 'saved' ? ' saved' : ''}`;
            const tag = o.kind === 'saved' ? ' ✓' : '';
            const txt = loading ? `${o.label}…` : `${o.label}${tag}`;
            return `<button class="${cls}" type="button" data-vangle="${esc(o.camera)}"${loading ? ' disabled' : ''}>${esc(txt)}</button>`;
          })
          .join('');
      }
    } else if (anglePills && lastAnglesSig !== '') {
      lastAnglesSig = '';
      anglePills.innerHTML = '';
    }
    if (angleActs) angleActs.hidden = !viewIsPreview;
    if (angleNote && viewIsPreview) angleNote.textContent = `Temp preview · expires in ${daysLeft}d`;
    if (saveBothBtn) saveBothBtn.hidden = !resolvedTarget;

    const soundLbl = q<HTMLElement>('#vpSoundLbl');
    const soundChk = q<HTMLInputElement>('#vpSoundChk');
    const hasBaked = activeHasBakedAudio();
    const legacyMaybe = activeLegacyMaybeAudio();
    const canListen = hasBaked || legacyMaybe || (tapeAvail() && tapeAudioAvail);
    if (soundLbl) soundLbl.hidden = !canListen;
    if (soundChk) soundChk.checked = soundPreviewOn;
    if (soundPreviewOn) syncSoundPlayback(false);
    else if (vid) {
      vid.muted = true;
      if (aud && !aud.paused) aud.pause();
    }

    const audioRow = q<HTMLElement>('#vpAudioRow');
    const audioLbl = q<HTMLElement>('#vpAudioLbl');
    const audioBtn = q<HTMLButtonElement>('#vpAudioToggle');
    const am = activeClipMeta();
    const canToggleAudio = !viewIsPreview && am.source === 'gymcam' && gymCam.online && !!(am.lanUrl || am.fileName);
    const canAddRoom = !hasBaked && !legacyMaybe && tapeAvail() && tapeAudioAvail;
    const canStrip = hasBaked || legacyMaybe;
    if (audioRow) audioRow.hidden = !(canToggleAudio && (canAddRoom || canStrip));
    if (audioLbl && audioBtn && canToggleAudio) {
      if (canStrip) {
        audioLbl.textContent = hasBaked ? '🔊 Includes room audio' : '🔊 Legacy clip may have audio';
        audioBtn.classList.remove('pri');
        audioBtn.textContent = 'Strip Sound';
      } else if (canAddRoom) {
        audioLbl.textContent = `🔇 Silent clip · ${daysLeft}d audio left`;
        audioBtn.classList.add('pri');
        audioBtn.textContent = '+ Add Room Audio';
      }
    }

    const shareAudBadge = q<HTMLElement>('#vpShareAudioBadge');
    if (shareAudBadge) {
      shareAudBadge.textContent = viewIsPreview
        ? '🔇 Temp angle preview — silent MP4'
        : hasBaked
          ? '🔊 MP4 includes room audio'
          : legacyMaybe
            ? '⚠ Legacy MP4 may include mic audio (tap Strip Sound first to silence)'
            : '🔇 Silent MP4 — safe to share';
    }
  };

  if (vid) {
    vid.addEventListener('seeked', () => {
      if (closed || pendingSeekSec == null) return;
      const next = pendingSeekSec;
      pendingSeekSec = null;
      vid.currentTime = next;
    });
    vid.onloadedmetadata = () => {
      const actualDur = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : 0;
      if (restoreAfterLoad) {
        const restore = restoreAfterLoad;
        restoreAfterLoad = null;
        if (st.dur <= 0 && actualDur > 0) {
          st = createTrimState(actualDur, meta!.trimStartSec, meta!.trimEndSec);
          refreshRailView(isTrimmed(st));
        }
        const capDur = actualDur > 0 ? actualDur : st.dur;
        const maxT = capDur > 0.05 ? capDur - 0.03 : capDur;
        const target = maxT > 0 ? clamp(restore.time, 0, maxT) : Math.max(0, restore.time);
        st = { ...st, playhead: target };
        vid.playbackRate = speed;
        seekVid(target);
        if (restore.paused) vid.pause();
        else void vid.play().catch(() => {});
        syncUi();
        return;
      }
      if (actualDur > 0) {
        st = createTrimState(actualDur, meta!.trimStartSec, meta!.trimEndSec);
        refreshRailView(isTrimmed(st));
        if (st.start > 0.015 && vid.currentTime < st.start) seekVid(st.start);
      }
      vid.playbackRate = speed;
      syncSoundPlayback(true);
      syncUi();
    };
    vid.onerror = () => {
      if ((viewIsPreview || viewSiblingMeta) && primaryPlayUrl) {
        const failedCam = viewCam || 'alternate';
        toast(`Could not load ${shortCamLabel(failedCam)} angle`);
        restorePrimaryView();
        return;
      }
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
        if (soundPreviewOn) syncSoundPlayback(true);
      }
      st = { ...st, playhead: st.dur > 0 ? clamp(vid.currentTime, 0, st.dur) : vid.currentTime };
      syncUi();
    };
    uiTicker = setInterval(() => {
      if (closed || !vid || vid.paused) return;
      if (isTrimmed(st) && vid.currentTime >= st.end - 0.025) {
        vid.currentTime = st.start;
        if (soundPreviewOn) syncSoundPlayback(true);
      }
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

    const viewBounds = (): { vStart: number; vEnd: number; vSpan: number } => {
      const vStart = railView.end > railView.start ? railView.start : 0;
      const vEnd = railView.end > railView.start ? railView.end : st.dur;
      return { vStart, vEnd, vSpan: Math.max(0.001, vEnd - vStart) };
    };

    const timeAtPtr = (clientX: number): number => {
      if (!trackEl || st.dur <= 0) return 0;
      const rect = trackEl.getBoundingClientRect();
      const { vStart, vEnd, vSpan } = viewBounds();
      return clamp(vStart + ((clientX - rect.left) / Math.max(1, rect.width)) * vSpan, vStart, vEnd);
    };

    const startSeekDrag = (mode: TrimFocus, e: PointerEvent): void => {
      if (!railEl || !trackEl || st.dur <= 0) return;
      e.stopPropagation();
      dragMode = mode;
      railEl.setPointerCapture(e.pointerId);
      vid.pause();
      const rect = trackEl.getBoundingClientRect();
      const { vStart, vSpan } = viewBounds();
      if (mode === 'in') {
        dragGrabOffsetPx = e.clientX - (rect.left + ((st.start - vStart) / vSpan) * rect.width);
        st = { ...st, focus: 'in', playhead: st.start };
      } else if (mode === 'out') {
        dragGrabOffsetPx = e.clientX - (rect.left + ((st.end - vStart) / vSpan) * rect.width);
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
    const endSeekDrag = (): void => {
      const finished = dragMode;
      dragMode = null;
      dragGrabOffsetPx = 0;
      if ((finished === 'in' || finished === 'out') && st.dur > 0) {
        refreshRailView(isTrimmed(st));
        syncUi();
      }
    };
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
      if (railZoomed && (st.focus === 'in' || st.focus === 'out') && (st.start < railView.start || st.end > railView.end)) {
        refreshRailView(true);
      }
      seekVid(st.playhead);
      syncUi();
    });
    const endJog = (): void => { jogEl?.classList.remove('drag'); };
    jogEl?.addEventListener('pointerup', endJog);
    jogEl?.addEventListener('pointercancel', endJog);

    if (needsLanLoad && meta.lanUrl) {
      const showDriveOfflineFallback = (): void => {
        const poster = q('#vpPoster');
        const loadMsg = q('#vpLoadMsg');
        const err = q('#vpLanErr');
        const drv = q('#vpDriveFallback');
        if (loadMsg) loadMsg.hidden = true;
        if (poster) poster.hidden = false;
        if (err) err.hidden = false;
        if (drv) drv.hidden = false;
      };
      void (async () => {
        if (!gymCam.online) {
          const pr = await gymCam.probe(1500);
          if (closed) return;
          if (!pr.online) {
            showDriveOfflineFallback();
            return;
          }
          void refreshTapeAngles();
        }
        const nextUrl = await resolveLanPlayable(meta!.lanUrl!, meta!.bytes);
        if (closed) return;
        const loadMsg = q('#vpLoadMsg');
        if (loadMsg) loadMsg.hidden = true;
        if (!nextUrl) {
          showDriveOfflineFallback();
          return;
        }
        const poster = q('#vpPoster');
        const deck = q('#vpDeck');
        vid.hidden = false;
        if (poster) poster.hidden = true;
        if (deck) deck.hidden = false;
        vid.src = nextUrl;
        vid.load();
        void vid.play().catch(() => {});
        syncUi();
      })();
    }
    if (gymCam.online && meta.source === 'gymcam' && meta.clipStartMs != null && withinRetention()) {
      void refreshTapeAngles();
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
      if (!viewIsPreview && isTrimDirty(st)) await persistTrim();
      const startMs = isTrimmed(st) ? Math.round(st.start * 1000) : undefined;
      const endMs = isTrimmed(st) ? Math.round(st.end * 1000) : undefined;
      const title = `${clipTitle}${setTxt ? ` · ${setTxt}` : ''}`;
      const expFileName = viewFileName || meta!.fileName || `${ex.id}-clip.mp4`;
      const expBlob = viewBlob;
      const expLanUrl = viewLanUrl || meta!.lanUrl;
      if (isNative()) {
        const uri = !viewIsPreview && !viewSiblingMeta && meta!.uri?.startsWith('content://') ? meta!.uri : undefined;
        const base64 = !uri && expBlob ? await blobToBase64(expBlob) : undefined;
        const httpUrl = !uri && !base64 && expLanUrl ? expLanUrl : undefined;
        if (!uri && !httpUrl && !base64) {
          toast('Clip not cached offline yet — connect to Gym Wi-Fi or Google Drive');
          return;
        }
        const res = await exportNativeVideo({
          uri,
          httpUrl,
          base64,
          fileName: expFileName,
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
      } else if (expBlob) {
        await shareImage(expBlob, expFileName, title);
      } else {
        toast('Install APK on Pixel to trim or share this clip');
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
      syncUi();
    }
  };

  syncUi();

  q<HTMLInputElement>('#vpSoundChk')?.addEventListener('change', (e) => {
    soundPreviewOn = (e.target as HTMLInputElement).checked;
    syncSoundPlayback(true);
    syncUi();
  });

  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-ok]')) { sh.close(); return; }

    const vp = t.closest<HTMLElement>('[data-vp]')?.dataset.vp;
    if (vp === 'tv') {
      const ok = castClipToWallboard(
        { ...meta!, lanUrl: viewLanUrl || meta!.lanUrl, camera: viewCam || meta!.camera },
        {
          rate: speed < 1 ? speed : 1,
          exName: clipTitle,
          setLabel: ctx?.setLabel || setTxt,
          trimStartSec: isTrimmed(st) ? roundMs(st.start) : meta!.trimStartSec,
          trimEndSec: isTrimmed(st) ? roundMs(st.end) : meta!.trimEndSec,
        },
      );
      toast(ok ? 'Playing on Wallboard TV' : 'TV replay requires a Gym Cam clip');
      return;
    }
    if (vp === 'share') {
      shareOpen = !shareOpen;
      syncUi();
      return;
    }

    if (t.closest('[data-vzoom]') && vid) {
      refreshRailView(!railView.zoomed);
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
      if (railZoomed && (st.start < railView.start || st.end > railView.end)) {
        refreshRailView(true);
      }
      seekVid(st.playhead);
      syncUi();
      return;
    }

    const vtrim = t.closest<HTMLElement>('[data-vtrim]')?.dataset.vtrim;
    if (vtrim && vid) {
      if (vtrim === 'reset') {
        vid.pause();
        st = { ...createTrimState(st.dur), savedStart: 0, savedEnd: st.dur };
        refreshRailView(false);
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

    const angleBtn = t.closest<HTMLButtonElement>('[data-vangle]');
    if (angleBtn?.dataset.vangle) {
      void switchToAngle(angleBtn.dataset.vangle);
      return;
    }

    if (t.closest('[data-vaudio]') && vid && !viewIsPreview) {
      const targetMeta = activeClipMeta();
      const currentlyBaked = targetMeta.hasAudio === true || (targetMeta.source === 'gymcam' && targetMeta.hasAudio === undefined && targetMeta.id === meta!.id);
      const nextInclude = !currentlyBaked;
      const btn = t.closest<HTMLButtonElement>('button');
      if (btn) {
        btn.disabled = true;
        btn.textContent = nextInclude ? 'Muxing…' : 'Stripping…';
      }
      try {
        const updated = await gymCam.setClipAudio(targetMeta, nextInclude);
        if (closed) return;
        if (updated?.lanUrl) {
          if (targetMeta.id === meta!.id) meta = updated;
          else viewSiblingMeta = updated;
          sessionFiles.delete(updated.id);
          clearAngleCache();
          const resumeTime = Number.isFinite(vid.currentTime) ? vid.currentTime : st.playhead;
          const wasPaused = vid.paused;
          const freshUrl = await fetchBlobUrl(`${updated.lanUrl}?v=${Date.now()}`, updated.bytes);
          if (!closed && freshUrl) {
            if (targetMeta.id === meta!.id) {
              if (freshUrl.objectUrl) {
                if (primaryObjectUrl && primaryObjectUrl !== freshUrl.objectUrl) URL.revokeObjectURL(primaryObjectUrl);
                primaryObjectUrl = freshUrl.objectUrl;
              }
              primaryBlob = freshUrl.blob;
              primaryPlayUrl = freshUrl.playUrl;
            }
            viewBlob = freshUrl.blob;
            viewLanUrl = updated.lanUrl;
            restoreAfterLoad = { time: resumeTime, paused: wasPaused };
            vid.autoplay = !wasPaused;
            vid.src = freshUrl.playUrl;
            vid.load();
          }
          toast(updated.hasAudio ? 'Room audio added to clip' : 'Sound stripped — silent MP4 saved');
        } else {
          toast(nextInclude ? 'Room audio unavailable for this clip' : 'Could not strip audio');
        }
      } finally {
        if (btn) btn.disabled = false;
      }
      syncUi();
      return;
    }

    if (t.closest('[data-vcam-saveboth]') && resolvedTarget && viewIsPreview && viewCam) {
      const btn = t.closest<HTMLButtonElement>('button');
      if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
      try {
        const saved = await gymCam.saveAdditionalAngle(resolvedTarget, meta!, viewCam);
        if (closed) return;
        if (saved?.lanUrl) {
          viewIsPreview = false;
          viewSiblingMeta = saved;
          viewLanUrl = saved.lanUrl;
          viewFileName = saved.fileName || viewFileName;
          toast(`Saved ${shortCamLabel(viewCam)} angle to set`);
        } else {
          toast(`Could not save ${shortCamLabel(viewCam)} angle`);
        }
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '+ Save Both'; }
      }
      syncUi();
      return;
    }

    if (t.closest('[data-vcam-swap]') && vid && viewIsPreview && viewCam && meta!.clipStartMs != null && meta!.clipEndMs != null) {
      const targetCam = viewCam;
      const oldShort = shortCamLabel(meta!.camera || 'current');
      const newShort = shortCamLabel(targetCam);
      const ok = await confirmSheet(
        `Replace ${oldShort} with ${newShort}?`,
        'Saves this angle permanently to OptiPlex & Google Drive and removes the old saved clip (14-day raw tape stays intact).',
        'Swap Primary',
        false,
      );
      if (!ok || closed) return;
      const resumeTime = Number.isFinite(vid.currentTime) ? vid.currentTime : st.playhead;
      const wasPaused = vid.paused;
      const btn = t.closest<HTMLButtonElement>('button');
      if (btn) { btn.disabled = true; btn.textContent = 'Swapping…'; }
      try {
        const updated = await gymCam.switchClipCamera(meta!, targetCam);
        if (closed) return;
        if (updated?.lanUrl) {
          meta = updated;
          sessionFiles.delete(id);
          clearAngleCache();
          viewIsPreview = false;
          viewSiblingMeta = null;
          viewCam = updated.camera || targetCam;
          viewLanUrl = updated.lanUrl;
          viewFileName = updated.fileName || '';
          const fileEl = q('#vpFileName');
          if (fileEl) fileEl.textContent = viewFileName;
          pendingSeekSec = null;
          const nextPlayUrl = await resolveLanPlayable(updated.lanUrl, updated.bytes);
          if (!closed && nextPlayUrl) {
            restoreAfterLoad = { time: resumeTime, paused: wasPaused };
            vid.autoplay = !wasPaused;
            vid.src = nextPlayUrl;
            vid.load();
          }
          toast(`Primary angle switched to ${newShort}`);
          void refreshTapeAngles();
        } else {
          toast(`Could not switch primary to ${newShort}`);
        }
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Swap Primary'; }
      }
      syncUi();
      return;
    }

    const vtape = t.closest<HTMLElement>('[data-vtape]');
    if (vtape && vid) {
      const [bStr, aStr] = vtape.dataset.vtape!.split(':');
      const wasPaused = vid.paused;
      const btn = t.closest<HTMLButtonElement>('button');
      if (btn) btn.disabled = true;
      const updated = await gymCam.extendTapeClip(meta!, Number(bStr), Number(aStr));
      if (btn) btn.disabled = false;
      if (closed) return;
      if (updated?.lanUrl) {
        meta = updated;
        sessionFiles.delete(id);
        clearAngleCache();
        viewIsPreview = false;
        viewSiblingMeta = null;
        viewCam = updated.camera || '';
        viewLanUrl = updated.lanUrl;
        viewFileName = updated.fileName || '';
        const fileEl = q('#vpFileName');
        if (fileEl) fileEl.textContent = viewFileName;
        st = createTrimState(updated.durationSec ?? st.dur, updated.trimStartSec, updated.trimEndSec);
        pendingSeekSec = null;
        restoreAfterLoad = null;
        const nextPlayUrl = await resolveLanPlayable(updated.lanUrl, updated.bytes);
        if (!closed && nextPlayUrl) {
          vid.autoplay = !wasPaused;
          vid.src = nextPlayUrl;
          vid.load();
          if (!wasPaused) void vid.play().catch(() => {});
        }
        toast('Extended clip from 2-Week Tape');
        void refreshTapeAngles();
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
      const targetMeta = activeClipMeta();
      const isGym = targetMeta.source === 'gymcam';
      const angleLbl = targetMeta.camera ? `${shortCamLabel(targetMeta.camera)} ` : '';
      const title = isGym
        ? (targetMeta.guest ? 'Delete Guest Clip?' : `Delete ${angleLbl}clip?`)
        : (targetMeta.linkOnly ? 'Unlink this video?' : 'Delete this video?');
      const msg = isGym
        ? (targetMeta.guest
          ? 'Removes this guest clip from this session, OptiPlex, and Google Drive (your 14-day raw tape stays intact).'
          : `Removes this ${angleLbl}clip from the set, OptiPlex, and Google Drive (any other saved angles & your 14-day raw tape stay intact).`)
        : (targetMeta.linkOnly ? 'Removes this link from the set (keeps the file on your phone & Drive).' : 'It is removed from this phone.');
      const okLbl = (targetMeta.linkOnly && !isGym) ? 'Unlink' : 'Delete';
      if (await confirmSheet(title, msg, okLbl, true)) {
        sessionFiles.delete(targetMeta.id);
        if (isGym) void gymCam.deleteClip(targetMeta);
        await lifts.deleteVideo(targetMeta.id);
        await resetTimerIfIdle(lifts.sessions.get(targetMeta.sessionId));
        const nextVidId = (set?.vids ?? []).find((vidId) => vidId !== targetMeta.id && lifts.videos.has(vidId));
        sh.close();
        toast(isGym ? (targetMeta.guest ? 'Guest clip deleted from session, OptiPlex & Drive' : `${angleLbl}clip deleted from set, OptiPlex & Drive`) : (targetMeta.linkOnly ? 'Video unlinked' : 'Video deleted'));
        if (nextVidId) void playVideo(nextVidId, ctx);
      }
    }
  });
}
