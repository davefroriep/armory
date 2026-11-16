import { lifts } from '../../lifts/store';
import { fmtKg, liftSettings } from '../../lifts/settings';
import type { LiftSession, LiftSet, SessionEx, VideoMeta } from '../../lifts/types';
import { uid } from '../../lifts/types';
import { esc } from '../dom';
import { confirmSheet, openSheet, toast } from './kit';

export interface VideoTarget {
  session: LiftSession;
  se: SessionEx;
  set: LiftSet;
}

const MAX_BYTES = 400 * 1024 * 1024;

const pickMime = (): string => {
  const c = ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
  return c.find((m) => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) ?? '';
};

/** Poster frame (small JPEG data URL) so lists don't have to decode video. */
async function thumbFromBlob(blob: Blob): Promise<string> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const v = document.createElement('video');
    v.muted = true;
    v.playsInline = true;
    v.preload = 'auto';
    const done = (r: string): void => { URL.revokeObjectURL(url); resolve(r); };
    const timer = setTimeout(() => done(''), 5000);
    v.onloadeddata = () => { v.currentTime = Math.min(0.4, (v.duration || 1) / 2); };
    v.onseeked = () => {
      clearTimeout(timer);
      const c = document.createElement('canvas');
      const w = 200;
      c.width = w;
      c.height = Math.round((w * (v.videoHeight || 9)) / (v.videoWidth || 16));
      c.getContext('2d')!.drawImage(v, 0, 0, c.width, c.height);
      done(c.toDataURL('image/jpeg', 0.6));
    };
    v.onerror = () => { clearTimeout(timer); done(''); };
    v.src = url;
  });
}

async function store(t: VideoTarget, blob: Blob, mime: string): Promise<VideoMeta> {
  void navigator.storage?.persist?.();
  const meta: VideoMeta = {
    id: uid(), sessionId: t.session.id, exId: t.se.exId, setId: t.set.id, ms: Date.now(),
    mime: mime || blob.type || 'video/mp4', bytes: blob.size, thumb: await thumbFromBlob(blob),
  };
  await lifts.addVideo(meta, blob);
  t.set.vids = [...(t.set.vids ?? []), meta.id];
  lifts.saveSession(t.session, true);
  return meta;
}

/** Pick a video from the phone (camera roll / files) and link it to the set. */
export function attachVideo(t: VideoTarget): Promise<VideoMeta | null> {
  return new Promise((resolve) => {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'video/*';
    inp.onchange = async () => {
      const f = inp.files?.[0];
      if (!f) return resolve(null);
      if (f.size > MAX_BYTES) { toast('That video is over 400 MB'); return resolve(null); }
      toast('Saving video…');
      resolve(await store(t, f, f.type));
    };
    inp.addEventListener('cancel', () => resolve(null));
    inp.click();
  });
}

/**
 * In-app camera: preview, countdown (time to step back to the bar), record, review, then link to the set.
 * Video only (no microphone permission needed).
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
        msg('Camera unavailable. Allow camera access, or use Attach to pick a video from your phone.');
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
      if (count) return; // countdown running; ignore
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
      end(await store(t, blob, mime));
    });

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') msg('Recording isn’t supported here. Use Attach to pick a video from your phone.');
    else void open();
  });
}

/** Play a stored clip with its set details. */
export async function playVideo(id: string): Promise<void> {
  const meta = lifts.videos.get(id);
  if (!meta) return;
  const blob = await lifts.videoBlob(id);
  if (!blob) { toast('Video file is missing'); return; }
  const url = URL.createObjectURL(blob);
  const s = lifts.sessions.get(meta.sessionId);
  const set = s?.exs.flatMap((e) => e.sets).find((x) => x.id === meta.setId);
  const ex = lifts.exOrStub(meta.exId);
  const sh = openSheet(
    `<video class="pv" src="${url}" controls autoplay playsinline loop></video>
     <h3 class="sh-title">${esc(ex.name)}</h3>
     <p class="sh-text">${set ? `${fmtKg(set.w ?? 0)} ${liftSettings().unit} × ${set.r ?? 0}` : ''} · ${new Date(meta.ms).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })} · ${(meta.bytes / 1048576).toFixed(1)} MB</p>
     <button class="btn danger big" data-del>Delete video</button><button class="btn big" data-ok>Close</button>`,
    { onClose: () => URL.revokeObjectURL(url) },
  );
  sh.body.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-ok]')) sh.close();
    else if (t.closest('[data-del]')) {
      if (await confirmSheet('Delete this video?', 'It is removed from this phone.', 'Delete', true)) {
        await lifts.deleteVideo(id);
        sh.close();
        toast('Video deleted');
      }
    }
  });
}
