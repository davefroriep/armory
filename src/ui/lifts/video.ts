import { active } from '../../lifts/active';
import { gymCam, type GymCamBurst } from '../../lifts/gymCam';
import { driveUrlFor } from '../../lifts/logic';
import { extractMp4CreationMs, formatPixelFilename, inferPixelVideoLink } from '../../lifts/mp4';
import { shareImage } from '../../lifts/share';
import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
import type { LiftSession, LiftSet, SessionEx, VideoMeta } from '../../lifts/types';
import { uid } from '../../lifts/types';
import {
  exportNativeVideo, getPlayableNativeVideoUrl, isNative, launchNativeVideoCamera,
  openNativeVideoUri, pickNativeDcimVideo,
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
const round1 = (v: number): number => Math.round(v * 10) / 10;

const fmtTenths = (sec: number): string => {
  const s = Math.max(0, round1(sec));
  const m = Math.floor(s / 60);
  const rem = (s - m * 60).toFixed(1).padStart(4, '0');
  return `${m}:${rem}`;
};

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

/**
 * Pick a video from the phone (native DCIM/Camera on APK, or Video picker on Web)
 * preserving filename & Google Drive DCIM Backup link without copying the file into IndexedDB.
 */
export async function linkDcimVideo(t: VideoTarget): Promise<VideoMeta | null> {
  if (isNative()) {
    const p = await pickNativeDcimVideo();
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
    ? `<div class="hint" id="vcam-hint" hidden style="color:#93c5fd">Camera opened — tap <b>Select Video</b> when finished.</div>
       <div class="alist">
         ${gymArmHtml}
         <button class="aitem" type="button" data-vact="cam"><b>Record Video</b><small>Phone camera</small></button>
         <button class="aitem" type="button" data-vact="pick"><b>Select Video</b><small>Phone DCIM / Gallery</small></button>
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
        const ok = await launchNativeVideoCamera();
        if (ok) sh.body.querySelector<HTMLElement>('#vcam-hint')!.hidden = false;
        else { await resetTimerIfIdle(t.session); toast('Could not open Camera app'); }
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

  let dur = meta.durationSec ?? 0;
  let draftStart = Math.max(0, meta.trimStartSec ?? 0);
  let draftEnd = meta.trimEndSec != null && meta.trimEndSec > draftStart ? meta.trimEndSec : dur;
  let speed = 1;
  let trimOpen = false;
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
         ${playUrl ? '<button class="vp-pill" type="button" data-vp="trim">Trim</button>' : ''}
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
         <span class="vp-time" id="vpTime">0:00.0 / 0:00.0</span>
         <span class="vp-frame" id="vpFrame">f 0</span>
       </div>
       <div class="vp-seek" id="vpSeek">
         <div class="vp-seek-keep" id="vpKeep" hidden></div>
         <div class="vp-seek-fill" id="vpFill"></div>
         <button class="vp-handle in" id="vpInH" type="button" aria-label="Trim start handle" hidden><span>[</span></button>
         <button class="vp-handle out" id="vpOutH" type="button" aria-label="Trim end handle" hidden><span>]</span></button>
       </div>
       <div class="jog-strip" id="vpJog"><i class="jog-needle"></i><span>◀ Drag to jog frame-by-frame ▶</span></div>
       <div class="vp-bar">
         <div class="vp-speeds">
           <button class="vp-spd" type="button" data-vspeed="0.25">0.25×</button>
           <button class="vp-spd" type="button" data-vspeed="0.5">0.5×</button>
           <button class="vp-spd on" type="button" data-vspeed="1">1×</button>
         </div>
         <div class="vp-trans">
           <button class="vp-btn" type="button" data-vstep="-1">◀ 1f</button>
           <button class="vp-btn play" type="button" data-vplay>⏸</button>
           <button class="vp-btn" type="button" data-vstep="1">1f ▶</button>
         </div>
       </div>
     </div>
     <div class="vp-drawer" id="vpTrimBox" hidden>
       <div class="vp-dr-head">
         <span>Window: <b id="vpTrimSpan">0:00.0 – 0:00.0</b> · <span id="vpTrimDur">0.0s</span></span>
         <button type="button" data-vp="trim" aria-label="Close trim">✕</button>
       </div>
       <div class="vp-trim-2">
         <button class="vp-tbtn" type="button" data-vtrim="inHere">⇤ Start Here</button>
         <button class="vp-tbtn" type="button" data-vtrim="outHere">End Here ⇥</button>
       </div>
       <div class="vp-nudge-row">
         <div><small>Start</small><button class="vp-nbtn" type="button" data-vnudge="in:-0.5">−0.5s</button><button class="vp-nbtn" type="button" data-vnudge="in:0.5">+0.5s</button></div>
         <div><small>End</small><button class="vp-nbtn" type="button" data-vnudge="out:-0.5">−0.5s</button><button class="vp-nbtn" type="button" data-vnudge="out:0.5">+0.5s</button></div>
       </div>
       <div class="vp-tape-row" id="vpTapeRow"${tapeAvail() ? '' : ' hidden'}>
         <small>2-Wk Tape</small>
         <button class="vp-nbtn" type="button" data-vtape="5:0">−5s Earlier</button>
         <button class="vp-nbtn" type="button" data-vtape="0:5">+5s Later</button>
       </div>
       <div class="vp-trim-acts">
         <button class="vp-tbtn" type="button" data-vtrim="loop">▶ Preview</button>
         <button class="vp-tbtn" type="button" data-vtrim="reset">Reset</button>
         <button class="vp-tbtn pri" type="button" data-vtrim="save">Save Trim</button>
         <button class="vp-tbtn acc" type="button" data-vtrim="share">Trim &amp; Share ↗</button>
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

  const hasTrimBounds = (): boolean => dur > 0 && (draftStart > 0.05 || draftEnd < dur - 0.05);

  const syncUi = (): void => {
    if (closed) return;
    const trimPill = q('[data-vp="trim"]');
    const sharePill = q('[data-vp="share"]');
    const trimBox = q('#vpTrimBox');
    const shareBox = q('#vpShareBox');
    trimPill?.classList.toggle('on', trimOpen);
    sharePill?.classList.toggle('on', shareOpen);
    if (trimBox) trimBox.hidden = !trimOpen;
    if (shareBox) shareBox.hidden = !shareOpen;

    if (!vid) return;
    const total = dur > 0 ? dur : (Number.isFinite(vid.duration) ? vid.duration : 0);
    const cur = clamp(vid.currentTime || 0, 0, Math.max(0.01, total));
    const timeEl = q('#vpTime');
    const frameEl = q('#vpFrame');
    const fillEl = q('#vpFill');
    const jogEl = q('#vpJog');
    const playBtn = q('[data-vplay]');
    if (timeEl) timeEl.textContent = `${fmtTenths(cur)} / ${fmtTenths(total)}`;
    if (frameEl) frameEl.textContent = `f ${Math.round(cur * 30)}`;
    if (fillEl && total > 0) fillEl.style.width = `${clamp((cur / total) * 100, 0, 100)}%`;
    if (jogEl) jogEl.style.backgroundPositionX = `${cur * 36}px`;
    if (playBtn) playBtn.textContent = vid.paused ? '▶' : '⏸';

    sh.body.querySelectorAll<HTMLElement>('[data-vspeed]').forEach((b) => {
      b.classList.toggle('on', Number(b.dataset.vspeed) === speed);
    });

    const showMarkers = trimOpen || hasTrimBounds();
    const keepEl = q('#vpKeep');
    const inH = q('#vpInH');
    const outH = q('#vpOutH');
    if (keepEl && total > 0) {
      keepEl.hidden = !showMarkers;
      const inPct = clamp((draftStart / total) * 100, 0, 100);
      const outPct = clamp((draftEnd / total) * 100, inPct, 100);
      keepEl.style.left = `${inPct}%`;
      keepEl.style.width = `${Math.max(1, outPct - inPct)}%`;
      if (inH) { inH.hidden = !trimOpen; inH.style.left = `${inPct}%`; }
      if (outH) { outH.hidden = !trimOpen; outH.style.left = `${outPct}%`; }
    }
    const spanEl = q('#vpTrimSpan');
    const durEl = q('#vpTrimDur');
    if (spanEl) spanEl.textContent = `${fmtTenths(draftStart)} – ${fmtTenths(draftEnd)}`;
    if (durEl) durEl.textContent = `${Math.max(0.5, round1(draftEnd - draftStart)).toFixed(1)}s`;
    const tapeRow = q('#vpTapeRow');
    if (tapeRow) tapeRow.hidden = !tapeAvail();
  };

  if (vid) {
    vid.onloadedmetadata = () => {
      if (Number.isFinite(vid.duration) && vid.duration > 0) {
        dur = round1(vid.duration);
        draftEnd = meta!.trimEndSec != null && meta!.trimEndSec > 0 ? Math.min(dur, meta!.trimEndSec) : dur;
        draftStart = clamp(meta!.trimStartSec ?? 0, 0, Math.max(0, draftEnd - 0.5));
        if (draftStart > 0.05 && vid.currentTime < draftStart) vid.currentTime = draftStart;
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
        if (hasTrimBounds() && (vid.currentTime < draftStart || vid.currentTime >= draftEnd - 0.05)) {
          vid.currentTime = draftStart;
        }
        void vid.play().catch(() => {});
      } else {
        vid.pause();
      }
      syncUi();
    };
    vid.ontimeupdate = () => {
      if (!vid.paused && hasTrimBounds() && vid.currentTime >= draftEnd - 0.03) {
        vid.currentTime = draftStart;
      }
      syncUi();
    };
    uiTicker = setInterval(() => {
      if (closed || !vid) return;
      if (!vid.paused) {
        if (hasTrimBounds() && vid.currentTime >= draftEnd - 0.03) vid.currentTime = draftStart;
        syncUi();
      }
    }, 45);

    // Draggable Start / End handles and seek bar
    const seekEl = q('#vpSeek');
    const inH = q('#vpInH');
    const outH = q('#vpOutH');
    let dragMode: 'seek' | 'in' | 'out' | null = null;

    const timeAtPtr = (clientX: number): number => {
      if (!seekEl || dur <= 0) return 0;
      const rect = seekEl.getBoundingClientRect();
      return clamp(((clientX - rect.left) / Math.max(1, rect.width)) * dur, 0, dur);
    };

    const startSeekDrag = (mode: 'seek' | 'in' | 'out', e: PointerEvent): void => {
      if (!seekEl || dur <= 0) return;
      e.stopPropagation();
      dragMode = mode;
      seekEl.setPointerCapture(e.pointerId);
      vid.pause();
      const t = round1(timeAtPtr(e.clientX));
      if (mode === 'in') { draftStart = clamp(t, 0, draftEnd - 0.5); vid.currentTime = draftStart; }
      else if (mode === 'out') { draftEnd = clamp(t, draftStart + 0.5, dur); vid.currentTime = draftEnd; }
      else { vid.currentTime = t; }
      syncUi();
    };
    inH?.addEventListener('pointerdown', (e) => startSeekDrag('in', e));
    outH?.addEventListener('pointerdown', (e) => startSeekDrag('out', e));
    seekEl?.addEventListener('pointerdown', (e) => startSeekDrag('seek', e));
    seekEl?.addEventListener('pointermove', (e) => {
      if (!dragMode || !seekEl.hasPointerCapture(e.pointerId)) return;
      const t = round1(timeAtPtr(e.clientX));
      if (dragMode === 'in') { draftStart = clamp(t, 0, draftEnd - 0.5); vid.currentTime = draftStart; }
      else if (dragMode === 'out') { draftEnd = clamp(t, draftStart + 0.5, dur); vid.currentTime = draftEnd; }
      else { vid.currentTime = t; }
      syncUi();
    });
    const endSeekDrag = (): void => { dragMode = null; };
    seekEl?.addEventListener('pointerup', endSeekDrag);
    seekEl?.addEventListener('pointercancel', endSeekDrag);

    // Tactile scrolling Thumb Jog Strip (~3.0s across full width)
    const jogEl = q('#vpJog');
    let jogStartX = 0;
    let jogStartSec = 0;
    jogEl?.addEventListener('pointerdown', (e) => {
      if (dur <= 0) return;
      jogEl.setPointerCapture(e.pointerId);
      jogStartX = e.clientX;
      jogStartSec = vid.currentTime;
      vid.pause();
      jogEl.classList.add('drag');
      syncUi();
    });
    jogEl?.addEventListener('pointermove', (e) => {
      if (!jogEl.hasPointerCapture(e.pointerId) || dur <= 0) return;
      const secPerPx = 3.0 / Math.max(220, jogEl.clientWidth);
      vid.currentTime = clamp(jogStartSec + (e.clientX - jogStartX) * secPerPx, 0, dur);
      syncUi();
    });
    const endJog = (): void => { jogEl?.classList.remove('drag'); };
    jogEl?.addEventListener('pointerup', endJog);
    jogEl?.addEventListener('pointercancel', endJog);
  }

  const persistTrim = async (): Promise<void> => {
    const next: VideoMeta = { ...meta! };
    if (hasTrimBounds()) {
      next.trimStartSec = round1(draftStart);
      next.trimEndSec = round1(draftEnd);
    } else {
      delete next.trimStartSec;
      delete next.trimEndSec;
    }
    meta = next;
    await lifts.saveVideoMeta(next);
  };

  const runExport = async (action: 'share' | 'save', btn?: HTMLButtonElement | null): Promise<void> => {
    const orig = btn?.textContent ?? '';
    if (btn) { btn.disabled = true; btn.textContent = action === 'save' ? 'Saving…' : 'Preparing…'; }
    try {
      const startMs = hasTrimBounds() ? Math.round(draftStart * 1000) : undefined;
      const endMs = hasTrimBounds() ? Math.round(draftEnd * 1000) : undefined;
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
    }
  };

  syncUi();

  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-ok]')) { sh.close(); return; }

    const vp = t.closest<HTMLElement>('[data-vp]')?.dataset.vp;
    if (vp === 'trim') {
      trimOpen = !trimOpen;
      if (trimOpen) shareOpen = false;
      syncUi();
      return;
    }
    if (vp === 'share') {
      shareOpen = !shareOpen;
      if (shareOpen) trimOpen = false;
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
        if (hasTrimBounds() && (vid.currentTime < draftStart || vid.currentTime >= draftEnd - 0.05)) {
          vid.currentTime = draftStart;
        }
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
      const total = dur > 0 ? dur : (Number.isFinite(vid.duration) ? vid.duration : 0);
      vid.currentTime = clamp(vid.currentTime + Number(step.dataset.vstep) / 30, 0, total);
      syncUi();
      return;
    }

    const vtrim = t.closest<HTMLElement>('[data-vtrim]')?.dataset.vtrim;
    if (vtrim && vid) {
      if (vtrim === 'inHere') {
        vid.pause();
        draftStart = clamp(round1(vid.currentTime), 0, Math.max(0, draftEnd - 0.5));
      } else if (vtrim === 'outHere') {
        vid.pause();
        draftEnd = clamp(round1(vid.currentTime), Math.min(dur, draftStart + 0.5), dur);
      } else if (vtrim === 'loop') {
        vid.currentTime = draftStart;
        void vid.play().catch(() => {});
      } else if (vtrim === 'reset') {
        draftStart = 0;
        draftEnd = dur;
        await persistTrim();
        toast('Trim reset');
      } else if (vtrim === 'save') {
        await persistTrim();
        toast('Clip trimmed');
      } else if (vtrim === 'share') {
        await persistTrim();
        await runExport('share', t.closest('button'));
      }
      syncUi();
      return;
    }

    const vnudge = t.closest<HTMLElement>('[data-vnudge]');
    if (vnudge && vid) {
      vid.pause();
      const [side, deltaStr] = vnudge.dataset.vnudge!.split(':');
      const d = Number(deltaStr);
      if (side === 'in') {
        draftStart = clamp(round1(draftStart + d), 0, Math.max(0, draftEnd - 0.5));
        vid.currentTime = draftStart;
      } else {
        draftEnd = clamp(round1(draftEnd + d), Math.min(dur, draftStart + 0.5), dur);
        vid.currentTime = draftEnd;
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
        dur = updated.durationSec ?? dur;
        draftStart = updated.trimStartSec ?? 0;
        draftEnd = updated.trimEndSec ?? dur;
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
