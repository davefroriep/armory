import { driveUrlFor } from '../../lifts/logic';
import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
import type { LiftSession, LiftSet, SessionEx, VideoMeta } from '../../lifts/types';
import { uid } from '../../lifts/types';
import { isNative, launchNativeVideoCamera, openNativeVideoUri, pickNativeDcimVideo } from '../../native/service';
import { esc } from '../dom';
import { confirmSheet, openSheet, promptSheet, toast } from './kit';

export { driveUrlFor };

export interface VideoTarget {
  session: LiftSession;
  se: SessionEx;
  set: LiftSet;
}

const MAX_BYTES = 400 * 1024 * 1024;
const VIDEO_EXT = /\.(mp4|mov|webm|mkv|m4v|3gp)$/i;
const sessionFiles = new Map<string, Blob>();

const pickMime = (): string => {
  const c = ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
  return c.find((m) => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) ?? '';
};

/** Poster frame (small JPEG data URL) + duration so lists don't have to decode or store whole videos. */
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

async function storeBlobClip(t: VideoTarget, blob: Blob, mime: string): Promise<VideoMeta> {
  void navigator.storage?.persist?.();
  const { thumb, durationSec } = await probeBlob(blob);
  const meta: VideoMeta = {
    id: uid(), sessionId: t.session.id, exId: t.se.exId, setId: t.set.id, ms: Date.now(),
    mime: mime || blob.type || 'video/mp4', bytes: blob.size, thumb,
    ...(durationSec ? { durationSec } : {}),
  };
  await lifts.addVideo(meta, blob);
  t.set.vids = [...(t.set.vids ?? []), meta.id];
  lifts.saveSession(t.session, true);
  return meta;
}

async function storeLinkClip(t: VideoTarget, o: Partial<VideoMeta> & { fileName?: string }): Promise<VideoMeta> {
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
    ...(o.fileName ? { fileName: o.fileName } : {}),
    ...(o.fileMs ? { fileMs: o.fileMs } : {}),
    ...(o.durationSec ? { durationSec: o.durationSec } : {}),
    ...(o.uri ? { uri: o.uri } : {}),
  };
  await lifts.addVideo(meta);
  t.set.vids = [...(t.set.vids ?? []), meta.id];
  lifts.saveSession(t.session, true);
  return meta;
}

/**
 * Launch the device Camera app directly in video mode (Android native/PWA) so it saves straight to DCIM,
 * or open the in-app camera on desktop browsers.
 */
export async function launchCameraForSet(t: VideoTarget): Promise<VideoMeta | 'external' | null> {
  if (isNative()) {
    const ok = await launchNativeVideoCamera();
    if (ok) return 'external';
  }
  if (/Android/i.test(navigator.userAgent)) {
    const a = document.createElement('a');
    a.href = 'intent://#Intent;action=android.media.action.VIDEO_CAMERA;end';
    document.body.appendChild(a);
    a.click();
    a.remove();
    return 'external';
  }
  return recordVideo(t);
}

/**
 * Pick a video from the phone's local storage (DCIM/Camera) and store only its filename + poster frame + URI
 * so Google Drive's DCIM Backup can always locate it later with zero duplicate blob storage.
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
    });
  }
  return new Promise((resolve) => {
    const inp = document.createElement('input');
    inp.type = 'file';
    // On Android, `*/*` opens Files / DocumentsUI (remembering DCIM/Camera and preserving real PXL_* filenames)
    // instead of the Android Photo Picker which strips filenames into numeric IDs.
    inp.accept = /Android/i.test(navigator.userAgent) ? '*/*' : 'video/*';
    inp.onchange = async () => {
      const f = inp.files?.[0];
      if (!f) return resolve(null);
      if (!(f.type.startsWith('video/') || VIDEO_EXT.test(f.name))) {
        toast('Pick a video file (e.g. .mp4 from DCIM/Camera)');
        return resolve(null);
      }
      if (f.size > MAX_BYTES) toast('Large video linked by filename (0 MB copied)');
      const { thumb, durationSec } = await probeBlob(f);
      let fileName = f.name;
      if (/^\d{6,}\.\w+$/.test(fileName)) {
        const edited = await promptSheet('Confirm DCIM filename', fileName, 'PXL_YYYYMMDD_HHMMSSXXX.mp4');
        if (edited) fileName = edited;
      }
      const meta = await storeLinkClip(t, {
        fileName,
        bytes: f.size,
        fileMs: f.lastModified || Date.now(),
        mime: f.type || 'video/mp4',
        durationSec,
        thumb,
      });
      sessionFiles.set(meta.id, f);
      resolve(meta);
    };
    inp.addEventListener('cancel', () => resolve(null));
    inp.click();
  });
}

/** Manually link a DCIM filename (`PXL_...mp4`) or a direct Google Drive share link. */
export async function linkManualVideo(t: VideoTarget, initial = ''): Promise<VideoMeta | null> {
  const raw = await promptSheet('Link video filename or URL', initial, 'PXL_20261116_173012345.mp4 or https://drive.google.com/…');
  if (!raw) return null;
  const isUrl = /^https?:\/\//i.test(raw);
  const fileName = isUrl ? raw.split('/').pop()?.split('?')[0] || 'Drive video' : raw;
  return storeLinkClip(t, { fileName, ...(isUrl ? { uri: raw } : {}) });
}

/** Legacy helper alias kept for internal callers. */
export const attachVideo = (t: VideoTarget): Promise<VideoMeta | null> => linkDcimVideo(t);

/**
 * Per-row video sheet: inspect linked videos on this set, launch phone Camera in video mode,
 * or select a file from DCIM/Camera to pull its filename for Drive DCIM Backup matching.
 */
export async function openSetVideoSheet(t: VideoTarget, setLabel: string): Promise<void> {
  const ex = lifts.exOrStub(t.se.exId);
  let resolveClosed: () => void = () => {};
  const closed = new Promise<void>((res) => { resolveClosed = res; });
  const linkedRows = (): string => {
    const items = (t.set.vids ?? []).map((id) => lifts.videos.get(id)).filter((v): v is VideoMeta => !!v);
    if (!items.length) return '';
    return `<div class="fl">LINKED ON THIS SET</div>
      ${items.map((v) => `<div class="vlink-row">
        <button class="vlink-main" type="button" data-play="${v.id}">
          <span class="vlink-th" style="${v.thumb ? `background-image:url(${v.thumb})` : ''}"><i>▶</i></span>
          <span class="vlink-meta"><b>${esc(v.fileName || 'Recorded clip')}</b><small>${v.durationSec ? `${v.durationSec}s · ` : ''}${v.bytes ? `${(v.bytes / 1048576).toFixed(1)} MB · ` : ''}Tap to open / search Drive</small></span>
        </button>
        <button class="vlink-rm" type="button" data-unlink="${v.id}" aria-label="Unlink video">✕</button>
      </div>`).join('')}`;
  };
  const sh = openSheet(
    `<h3 class="sh-title">${esc(ex.name)} · ${esc(setLabel)}</h3>
     <div id="vlinked">${linkedRows()}</div>
     <div class="hint" id="vcam-hint" hidden style="color:#93c5fd">Camera opened — once done recording, tap <b>Select file from DCIM</b> below to link its filename.</div>
     <div class="fl">LINK VIDEO</div>
     <div class="alist">
       <button class="aitem" type="button" data-vact="cam"><b>Launch Camera (Video)</b><small>Open phone camera in video mode (saves to DCIM)</small></button>
       <button class="aitem" type="button" data-vact="pick"><b>Select file from DCIM</b><small>Pulls filename only — matches Google Drive DCIM Backup even while still uploading</small></button>
       <button class="aitem" type="button" data-vact="manual"><b>Paste link or filename</b><small>Enter PXL_…mp4 filename or Google Drive URL</small></button>
     </div>
     <button class="btn big" type="button" data-vok>Done</button>`,
    { onClose: resolveClosed },
  );
  const redraw = (): void => { sh.body.querySelector('#vlinked')!.innerHTML = linkedRows(); };
  sh.body.addEventListener('click', async (e) => {
    const el = e.target as HTMLElement;
    const play = el.closest<HTMLElement>('[data-play]');
    if (play) { await playVideo(play.dataset.play!); redraw(); return; }
    const unlink = el.closest<HTMLElement>('[data-unlink]');
    if (unlink) {
      await lifts.deleteVideo(unlink.dataset.unlink!);
      redraw();
      toast('Video unlinked');
      return;
    }
    const act = el.closest<HTMLElement>('[data-vact]')?.dataset.vact;
    if (act === 'cam') {
      const res = await launchCameraForSet(t);
      if (res === 'external') {
        sh.body.querySelector<HTMLElement>('#vcam-hint')!.hidden = false;
      } else if (res) {
        redraw();
        toast('Video linked to set');
      }
    } else if (act === 'pick') {
      const meta = await linkDcimVideo(t);
      if (meta) {
        redraw();
        toast(`Linked ${meta.fileName ?? 'video'}`);
      }
    } else if (act === 'manual') {
      const meta = await linkManualVideo(t);
      if (meta) {
        redraw();
        toast(`Linked ${meta.fileName ?? 'video'}`);
      }
    } else if (el.closest('[data-vok]')) {
      sh.close();
    }
  });
  await closed;
}

/**
 * In-app camera: preview, countdown (time to step back to the bar), record, review, then link to the set.
 * Used as fallback on desktop browsers without native Android Intent camera launch.
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
        msg('Camera unavailable. Allow camera access, or use Select file from DCIM.');
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
      end(await storeBlobClip(t, blob, mime));
    });

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') msg('Recording isn’t supported here. Use Select file from DCIM.');
    else void open();
  });
}

/** Play or recall a stored/linked video with Google Drive DCIM Backup lookup and local phone playback. */
export async function playVideo(id: string): Promise<void> {
  const meta = lifts.videos.get(id);
  if (!meta) return;
  const blob = sessionFiles.get(id) ?? (meta.linkOnly ? null : await lifts.videoBlob(id));
  if (!blob && !meta.linkOnly && !meta.fileName && !meta.uri) {
    toast('Video file is missing');
    return;
  }
  const url = blob ? URL.createObjectURL(blob) : '';
  const s = lifts.sessions.get(meta.sessionId);
  const set = s?.exs.flatMap((e) => e.sets).find((x) => x.id === meta.setId);
  const ex = lifts.exOrStub(meta.exId);
  const driveUrl = driveUrlFor(meta);
  const hasLocalUri = isNative() && !!meta.uri?.startsWith('content://');
  const setTxt = set ? `${set.w ? `${fmtKg(set.w)} ${liftSettings().unit}` : ex.type === 'bodyweight' ? 'BW' : '–'} × ${set.r ?? 0}` : '';
  const dateTxt = new Date(meta.fileMs ?? meta.ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  const sizeTxt = meta.bytes ? ` · ${(meta.bytes / 1048576).toFixed(1)} MB` : '';
  const durTxt = meta.durationSec ? ` · ${meta.durationSec}s` : '';

  const preview = url
    ? `<video class="pv" src="${url}" controls autoplay playsinline loop></video>`
    : meta.thumb
      ? `<div class="pv-poster" style="background-image:url(${meta.thumb})"><span class="pv-badge">DCIM Backup · Google Drive</span></div>`
      : '';

  const sh = openSheet(
    `${preview}
     <h3 class="sh-title">${esc(ex.name)}</h3>
     <p class="sh-text">${setTxt}${setTxt ? ' · ' : ''}${dateTxt}${durTxt}${sizeTxt}</p>
     ${meta.fileName ? `<div class="vfile"><small>LINKED FILE (DCIM BACKUP)</small><b>${esc(meta.fileName)}</b></div>` : ''}
     ${driveUrl ? `<a class="btn primary big" href="${esc(driveUrl)}" target="_blank" rel="noopener" data-vdrive>Open in Google Drive ↗</a>
       <p class="hint" style="text-align:center">Searches your Google Drive backup for this exact filename (big videos may take a minute to finish syncing).</p>` : ''}
     ${hasLocalUri ? '<button class="btn big" type="button" data-vlocal>▶ Play from phone DCIM</button>' : ''}
     <button class="btn sm" type="button" data-vedit>Edit filename / URL</button>
     <button class="btn danger big" type="button" data-del>${meta.linkOnly ? 'Unlink video' : 'Delete video'}</button>
     <button class="btn big" type="button" data-ok>Close</button>`,
    { onClose: () => { if (url) URL.revokeObjectURL(url); } },
  );
  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-ok]')) sh.close();
    else if (t.closest('[data-vlocal]')) {
      const ok = await openNativeVideoUri(meta.uri!);
      if (!ok) toast('Local file moved or unavailable — use Google Drive above');
    } else if (t.closest('[data-vedit]')) {
      const v = await promptSheet('Filename or Drive URL', meta.uri && /^https?:\/\//i.test(meta.uri) ? meta.uri : (meta.fileName ?? ''), 'PXL_20261116_173012345.mp4');
      if (!v) return;
      const isUrl = /^https?:\/\//i.test(v);
      const next: VideoMeta = {
        ...meta,
        linkOnly: true,
        fileName: isUrl ? v.split('/').pop()?.split('?')[0] || 'Drive video' : v,
        ...(isUrl ? { uri: v } : {}),
      };
      await lifts.saveVideoMeta(next);
      sh.close();
      toast('Video link updated');
      void playVideo(meta.id);
    } else if (t.closest('[data-del]')) {
      const msg = meta.linkOnly ? 'Removes this filename/Drive link from the set (keeps the file on your phone & Drive).' : 'It is removed from this phone.';
      if (await confirmSheet(meta.linkOnly ? 'Unlink this video?' : 'Delete this video?', msg, meta.linkOnly ? 'Unlink' : 'Delete', true)) {
        sessionFiles.delete(id);
        await lifts.deleteVideo(id);
        sh.close();
        toast(meta.linkOnly ? 'Video unlinked' : 'Video deleted');
      }
    }
  });
}
