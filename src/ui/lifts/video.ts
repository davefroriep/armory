import { active } from '../../lifts/active';
import { gymCam, type GymCamBurst } from '../../lifts/gymCam';
import { driveUrlFor } from '../../lifts/logic';
import {
  extractMp4CreationMs, formatPixelFilename, inferPixelVideoLink, sortDeviceVideosNewestFirst,
} from '../../lifts/mp4';
import { shareImage } from '../../lifts/share';
import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
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

/**
 * Per-row video sheet: Gym-Cam Smart Burst Snap (when online), linked clips, Record Video & Select Video.
 */
export async function openSetVideoSheet(t: VideoTarget, setLabel: string): Promise<void> {
  const ex = lifts.exOrStub(t.se.exId);
  const native = isNative();
  const mobile = isMobileWeb();
  const camOnline = gymCam.online && t.session.status === 'active';
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

  const gymArmHtml = camOnline && !t.set.done
    ? `<button class="aitem on" type="button" data-vact="gymarm"><b>Record on Gym Cam</b><small>Arms this set and auto-stops when you tap ✓</small></button>`
    : '';

  const actionsHtml = native
    ? `<div class="alist">
         ${gymArmHtml}
         <button class="aitem" type="button" data-vact="cam"><b>Record Video</b><small>Phone camera</small></button>
         <button class="aitem" type="button" data-vact="pick"><b>Select Video</b><small>Newest phone videos</small></button>
       </div>`
    : mobile
      ? `<div class="alist">
           ${gymArmHtml}
           <label class="aitem" data-vact="cam"><input type="file" accept="video/*" capture="environment" data-vinp="cam" hidden><b>Record Video</b></label>
           <label class="aitem" data-vact="pick"><input type="file" accept="video/*" data-vinp="pick" hidden><b>Select Video</b></label>
         </div>`
      : `<div class="alist">
           ${gymArmHtml}
           <button class="aitem" type="button" data-vact="cam"><b>Record Video</b></button>
           <label class="aitem" data-vact="pick"><input type="file" accept="video/*" data-vinp="pick" hidden><b>Select Video</b></label>
         </div>`;

  const sh = openSheet(
    `<h3 class="sh-title">${esc(ex.name)} · ${esc(setLabel)}</h3>
     <div id="vlinked">${linkedRows()}</div>
     <div id="vburst"${camOnline ? '' : ' hidden'}></div>
     ${actionsHtml}
     <button class="btn big" type="button" data-vok>Done</button>`,
    { onClose: resolveClosed },
  );
  const redraw = (): void => { sh.body.querySelector('#vlinked')!.innerHTML = linkedRows(); };

  // Sheet A: Smart Burst Snap when OptiPlex Gym Cam is on LAN
  let bursts: GymCamBurst[] = [];
  let burstIdx = 0;
  let burstStartDelta = 0;
  let burstEndDelta = 0;
  let burstCamOverride = '';
  let fineOpen = false;

  const drawBursts = (): void => {
    const box = sh.body.querySelector<HTMLElement>('#vburst');
    if (!box) return;
    if (!bursts.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const b = bursts[burstIdx];
    const camName = burstCamOverride || b.camera || 'Rack Cam';
    const altCam = camName === 'Rack Cam' ? 'Smith Cam' : 'Rack Cam';
    const effStart = b.startMs + burstStartDelta * 1000;
    const effEnd = Math.max(effStart + 3000, b.endMs + burstEndDelta * 1000);
    const effSec = Math.round((effEnd - effStart) / 1000);
    box.innerHTML = `
      <div class="fl">GYM CAM · MOTION BURST</div>
      <div class="vburst-card">
        <div class="vburst-top">
          <div class="vburst-th" style="${b.thumb ? `background-image:url(${b.thumb})` : ''}"></div>
          <div class="vburst-meta">
            <div><b>${esc(camName)}</b> <small>${effSec}s</small></div>
            <small>${fmtEpochClock(effStart)} – ${fmtEpochClock(effEnd)}</small>
            ${b.note ? `<small>${esc(b.note)}</small>` : ''}
          </div>
        </div>
        ${bursts.length > 1 ? `<div class="vburst-nav">
          <button class="btn sm" type="button" data-bnav="-1">◀ Prev Burst</button>
          <button class="btn sm" type="button" data-bnav="1">Next Burst ▶</button>
        </div>` : ''}
        <button class="btn primary big" type="button" data-bsave>Save Clip to ${esc(setLabel)}</button>
        <button class="vburst-tog" type="button" data-btog>${fineOpen ? 'Hide fine-tune ▴' : 'Fine-tune ±5s or switch camera ▾'}</button>
        ${fineOpen ? `<div class="vburst-fine">
          <div class="vburst-nudge">
            <div><span>Start</span><button class="btn sm" type="button" data-bnudge="s:-5">−5s</button><button class="btn sm" type="button" data-bnudge="s:5">+5s</button></div>
            <div><span>End</span><button class="btn sm" type="button" data-bnudge="e:-5">−5s</button><button class="btn sm" type="button" data-bnudge="e:5">+5s</button></div>
          </div>
          <button class="btn sm" type="button" data-bcam="${esc(altCam)}">Switch to ${esc(altCam)}</button>
        </div>` : ''}
      </div>`;
  };

  if (camOnline) {
    const box = sh.body.querySelector<HTMLElement>('#vburst')!;
    box.innerHTML = '<div class="hint">Checking OptiPlex Session Tape…</div>';
    void gymCam.listBursts(t.session.camSessionId ?? t.session.id, t.set.at).then((list) => {
      if (!sh.el.isConnected) return;
      bursts = list;
      if (bursts.length) {
        const ref = (t.set.at ?? Date.now()) + gymCam.clockOffsetMs;
        let bestIdx = bursts.length - 1;
        let bestDiff = Infinity;
        bursts.forEach((b, idx) => {
          const d = Math.abs(b.endMs - ref);
          if (d < bestDiff) { bestDiff = d; bestIdx = idx; }
        });
        burstIdx = bestIdx;
      }
      drawBursts();
    });
  }

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
    if (play) { await playVideo(play.dataset.play!, { target: t, setLabel }); redraw(); return; }
    const unlink = el.closest<HTMLElement>('[data-unlink]');
    if (unlink) {
      await lifts.deleteVideo(unlink.dataset.unlink!);
      await resetTimerIfIdle(t.session);
      redraw();
      toast('Video unlinked');
      return;
    }
    const bnav = el.closest<HTMLElement>('[data-bnav]');
    if (bnav && bursts.length) {
      burstIdx = (burstIdx + Number(bnav.dataset.bnav) + bursts.length) % bursts.length;
      burstStartDelta = 0;
      burstEndDelta = 0;
      burstCamOverride = '';
      drawBursts();
      return;
    }
    if (el.closest('[data-btog]')) {
      fineOpen = !fineOpen;
      drawBursts();
      return;
    }
    const bnudge = el.closest<HTMLElement>('[data-bnudge]');
    if (bnudge) {
      const [side, deltaStr] = bnudge.dataset.bnudge!.split(':');
      if (side === 's') burstStartDelta += Number(deltaStr);
      else burstEndDelta += Number(deltaStr);
      drawBursts();
      return;
    }
    const bcam = el.closest<HTMLElement>('[data-bcam]');
    if (bcam) {
      burstCamOverride = bcam.dataset.bcam!;
      drawBursts();
      return;
    }
    const bsave = el.closest<HTMLButtonElement>('[data-bsave]');
    if (bsave && bursts[burstIdx]) {
      bsave.disabled = true;
      bsave.textContent = 'Saving…';
      const meta = await gymCam.saveBurstClip(t, bursts[burstIdx], burstStartDelta, burstEndDelta, burstCamOverride || undefined);
      if (meta) {
        toast('Clip saved & linked');
        sh.close();
      } else {
        bsave.disabled = false;
        bsave.textContent = `Save Clip to ${setLabel}`;
        toast('Could not save clip from Gym Cam');
      }
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
  const blob = sessionFiles.get(id) ?? (meta.linkOnly ? null : await lifts.videoBlob(id));
  if (!blob && !meta.linkOnly && !meta.fileName && !meta.uri && !meta.driveQuery && !meta.lanUrl) {
    toast('Video file is missing');
    return;
  }

  const objectUrl = blob ? URL.createObjectURL(blob) : '';
  let playUrl = objectUrl || meta.lanUrl || '';
  if (!playUrl && isNative() && meta.uri?.startsWith('content://')) {
    playUrl = (await getPlayableNativeVideoUrl(meta.uri, meta.mime)) ?? '';
  }

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

  const posterHtml = meta.thumb
    ? `<div class="pv-poster" id="vpPoster"${playUrl ? ' hidden' : ''} style="background-image:url(${meta.thumb})"><span class="pv-badge">${meta.source === 'gymcam' ? 'Gym Cam · Google Drive' : 'DCIM Backup · Google Drive'}</span></div>`
    : `<div class="pv-poster" id="vpPoster" hidden><span class="pv-badge">Google Drive Backup</span></div>`;

  const tapeAvail = (): boolean => meta!.source === 'gymcam' && gymCam.online && meta!.clipStartMs != null;

  const sh = openSheet(
    `<div class="vp-head">
       <div class="vp-title"><h3 class="sh-title">${esc(ex.name)}</h3><p class="sh-text">${esc(subTxt)}</p></div>
       <div class="vp-pills">
         ${playUrl ? '<button class="vp-pill on" type="button" id="vpSaveTop" data-vtrim="save" hidden>Save Trim</button>' : ''}
         <button class="vp-pill" type="button" data-vp="share">Share ↗</button>
       </div>
     </div>
     <div class="vp-stage">
       ${playUrl ? `<video class="pv" id="vpVid" src="${esc(playUrl)}" playsinline muted autoplay loop preload="metadata"></video>` : ''}
       ${posterHtml}
       <p class="hint" id="vpLanErr" hidden style="text-align:center">OptiPlex LAN stream unavailable away from home — open via Google Drive below.</p>
     </div>
     ${playUrl ? `
     <div class="vp-deck" id="vpDeck">
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
     ${!playUrl && driveUrl ? `<a class="btn primary big" href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>Open in Google Drive ↗</a>` : ''}
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
        if (uiTicker) clearInterval(uiTicker);
        if (objectUrl) URL.revokeObjectURL(objectUrl);
      },
    },
  );

  const vid = sh.body.querySelector<HTMLVideoElement>('#vpVid');
  const q = <T extends HTMLElement>(sel: string): T | null => sh.body.querySelector<T>(sel);

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
    vid.onloadedmetadata = () => {
      if (Number.isFinite(vid.duration) && vid.duration > 0) {
        st = createTrimState(vid.duration, meta!.trimStartSec, meta!.trimEndSec);
        if (st.start > 0.015 && vid.currentTime < st.start) vid.currentTime = st.start;
      }
      vid.playbackRate = speed;
      syncUi();
    };
    vid.onerror = () => {
      const deck = q('#vpDeck');
      const poster = q('#vpPoster');
      const err = q('#vpLanErr');
      vid.hidden = true;
      if (deck) deck.hidden = true;
      if (poster) poster.hidden = false;
      if (err) err.hidden = false;
    };
    vid.onclick = () => {
      if (vid.paused) {
        st = resolvePlayStart(st);
        vid.currentTime = st.playhead;
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

    // Bracketed Start / End grab rails + seek track (zero-jump handle offset)
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
      vid.currentTime = st.playhead;
      syncUi();
    };

    inH?.addEventListener('pointerdown', (e) => startSeekDrag('in', e));
    outH?.addEventListener('pointerdown', (e) => startSeekDrag('out', e));
    trackEl?.addEventListener('pointerdown', (e) => startSeekDrag('playhead', e));
    railEl?.addEventListener('pointermove', (e) => {
      if (!dragMode || !railEl.hasPointerCapture(e.pointerId)) return;
      st = dragTrimHandle(st, dragMode, timeAtPtr(e.clientX - dragGrabOffsetPx));
      vid.currentTime = st.playhead;
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
      vid.currentTime = st.playhead;
      syncUi();
    });
    const endJog = (): void => { jogEl?.classList.remove('drag'); };
    jogEl?.addEventListener('pointerup', endJog);
    jogEl?.addEventListener('pointercancel', endJog);
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
      vid.currentTime = st.playhead;
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
        vid.currentTime = st.playhead;
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
      vid.currentTime = st.playhead;
      syncUi();
      return;
    }

    const vtrim = t.closest<HTMLElement>('[data-vtrim]')?.dataset.vtrim;
    if (vtrim && vid) {
      if (vtrim === 'reset') {
        vid.pause();
        st = { ...createTrimState(st.dur), savedStart: 0, savedEnd: st.dur };
        vid.currentTime = st.playhead;
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
        st = createTrimState(updated.durationSec ?? st.dur, updated.trimStartSec, updated.trimEndSec);
        vid.src = updated.lanUrl;
        vid.load();
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
