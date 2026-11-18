import { Emitter } from '../emitter';
import { getSettings } from '../store/settings';
import { lifts } from './store';
import { uid, type LiftSession, type LiftSet, type SessionEx, type VideoMeta } from './types';

export interface GymCamBurst {
  id: string | number;
  camera: string;
  startMs: number;
  endMs: number;
  durationSec: number;
  thumb?: string;
  note?: string;
}

export interface GymCamProbeInfo {
  online: boolean;
  recording: boolean;
  paused: boolean;
  cameras: string[];
  latencyMs: number;
}

export interface GymCamTarget {
  session: LiftSession;
  se: SessionEx;
  set: LiftSet;
}

interface ClipApiResponse {
  ok?: boolean;
  lanUrl?: string;
  fileName?: string;
  camera?: string;
  thumb?: string;
  durationSec?: number;
  startMs?: number;
  endMs?: number;
  bytes?: number;
}

class GymCamController {
  online = false;
  recording = false;
  paused = false;
  cameras: string[] = [];
  clockOffsetMs = 0;
  armed: { sessionId: string; exUid: string; setId: string; startEpochMs: number } | null = null;

  readonly changed = new Emitter<void>();

  baseUrl(): string {
    return (getSettings().gymCamUrl || 'http://192.168.1.152:8766').trim().replace(/\/+$/, '');
  }

  serverNowMs(): number {
    return Date.now() + this.clockOffsetMs;
  }

  isArmed(setId: string): boolean {
    return this.armed?.setId === setId;
  }

  cancelArm(): void {
    if (!this.armed) return;
    this.armed = null;
    this.changed.emit();
  }

  snapshotUrl(camera?: string): string {
    const q = camera ? `?camera=${encodeURIComponent(camera)}&t=${Date.now()}` : `?t=${Date.now()}`;
    return `${this.baseUrl()}/api/snapshot${q}`;
  }

  async probe(timeoutMs = 800): Promise<GymCamProbeInfo> {
    if (!getSettings().gymCamEnabled || typeof fetch === 'undefined' || typeof AbortController === 'undefined') {
      const was = this.online || this.recording;
      this.online = false;
      this.recording = false;
      this.paused = false;
      this.cameras = [];
      if (was) this.changed.emit();
      return { online: false, recording: false, paused: false, cameras: [], latencyMs: 0 };
    }

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const t0 = Date.now();
    try {
      const res = await fetch(`${this.baseUrl()}/api/ping`, { signal: ctrl.signal, cache: 'no-store' });
      const latencyMs = Math.max(1, Date.now() - t0);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as {
        service?: string;
        serverEpochMs?: number;
        recording?: boolean;
        paused?: boolean;
        cameras?: string[];
      };
      if (data?.service !== 'armory-cam') throw new Error('Not armory-cam');
      this.clockOffsetMs = typeof data.serverEpochMs === 'number' ? data.serverEpochMs - Date.now() : 0;
      this.online = true;
      this.recording = !!data.recording;
      this.paused = !!data.paused;
      this.cameras = Array.isArray(data.cameras) ? data.cameras : [];
      this.changed.emit();
      return { online: true, recording: this.recording, paused: this.paused, cameras: this.cameras, latencyMs };
    } catch {
      const was = this.online || this.recording;
      this.online = false;
      this.recording = false;
      this.paused = false;
      this.cameras = [];
      if (was) this.changed.emit();
      return { online: false, recording: false, paused: false, cameras: [], latencyMs: 0 };
    } finally {
      clearTimeout(timer);
    }
  }

  private async postJson<T>(path: string, body: unknown, timeoutMs = 6000): Promise<T | null> {
    if (typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl()}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok) return null;
      return (await res.json()) as T;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async sessionEnter(s: LiftSession): Promise<void> {
    if (!this.online || !getSettings().gymCamEnabled) return;
    s.camSessionId ??= s.id;
    const r = await this.postJson<{ ok?: boolean }>('/api/session/start', { sessionId: s.camSessionId, name: s.name });
    if (r?.ok) {
      this.recording = true;
      this.paused = false;
      this.changed.emit();
    }
  }

  async sessionPause(s: LiftSession): Promise<void> {
    if (!this.online) return;
    await this.postJson('/api/session/pause', { sessionId: s.camSessionId ?? s.id });
    this.paused = true;
    this.changed.emit();
  }

  async sessionResume(s: LiftSession): Promise<void> {
    if (!this.online) return;
    await this.postJson('/api/session/resume', { sessionId: s.camSessionId ?? s.id });
    this.paused = false;
    this.recording = true;
    this.changed.emit();
  }

  async sessionFinish(s: LiftSession): Promise<void> {
    this.armed = null;
    if (!this.online) return;
    await this.postJson('/api/session/stop', { sessionId: s.camSessionId ?? s.id, discard: false });
    this.recording = false;
    this.paused = false;
    this.changed.emit();
  }

  async sessionDiscard(s: LiftSession): Promise<void> {
    this.armed = null;
    if (!this.online) return;
    await this.postJson('/api/session/stop', { sessionId: s.camSessionId ?? s.id, discard: true });
    this.recording = false;
    this.paused = false;
    this.changed.emit();
  }

  armSet(s: LiftSession, se: SessionEx, set: LiftSet): boolean {
    if (!this.online) return false;
    this.armed = { sessionId: s.id, exUid: se.uid, setId: set.id, startEpochMs: this.serverNowMs() };
    this.changed.emit();
    return true;
  }

  private resolveLanUrl(raw?: string): string {
    if (!raw) return '';
    return /^https?:\/\//i.test(raw) ? raw : `${this.baseUrl()}${raw.startsWith('/') ? '' : '/'}${raw}`;
  }

  private async persistClipFromServer(target: GymCamTarget, clip: ClipApiResponse): Promise<VideoMeta | null> {
    const lanUrl = this.resolveLanUrl(clip.lanUrl);
    if (!lanUrl) return null;
    const meta: VideoMeta = {
      id: uid(),
      sessionId: target.session.id,
      exId: target.se.exId,
      setId: target.set.id,
      ms: Date.now(),
      mime: 'video/mp4',
      bytes: clip.bytes ?? 0,
      thumb: clip.thumb ?? '',
      linkOnly: true,
      source: 'gymcam',
      camera: clip.camera || 'Rack Cam',
      lanUrl,
      ...(clip.fileName ? { fileName: clip.fileName } : {}),
      ...(clip.durationSec ? { durationSec: clip.durationSec } : {}),
      ...(clip.startMs != null ? { clipStartMs: clip.startMs } : {}),
      ...(clip.endMs != null ? { clipEndMs: clip.endMs } : {}),
    };
    await lifts.addVideo(meta);
    target.set.vids = [...(target.set.vids ?? []), meta.id];
    lifts.saveSession(target.session, true);
    this.changed.emit();
    return meta;
  }

  async stopAndSaveArmed(s: LiftSession, se: SessionEx, set: LiftSet): Promise<VideoMeta | null> {
    if (!this.armed || this.armed.setId !== set.id) return null;
    const startMs = this.armed.startEpochMs;
    const endMs = this.serverNowMs() + 2000;
    this.armed = null;
    this.changed.emit();
    if (!this.online) return null;
    const exName = lifts.exOrStub(se.exId).name;
    const res = await this.postJson<ClipApiResponse>('/api/clip', {
      sessionId: s.camSessionId ?? s.id,
      exId: se.exId,
      exName,
      setId: set.id,
      startMs,
      endMs,
    }, 15_000);
    return res?.lanUrl ? this.persistClipFromServer({ session: s, se, set }, res) : null;
  }

  /**
   * Auto-clips a PR set using server motion-burst alignment while filtering out
   * rapid batch check-check-check timestamps (<20s apart).
   */
  async autoClipPr(s: LiftSession, se: SessionEx, set: LiftSet): Promise<VideoMeta | null> {
    if (!this.online || !getSettings().gymCamAutoPr) return null;
    const atMs = set.at ?? Date.now();
    let prevLocalAt = 0;
    for (const ex of s.exs) {
      for (const st of ex.sets) {
        if (st.id !== set.id && st.done && st.at != null && st.at < atMs - 20_000 && st.at > prevLocalAt) {
          prevLocalAt = st.at;
        }
      }
    }
    const res = await this.postJson<ClipApiResponse>('/api/clip/auto-pr', {
      sessionId: s.camSessionId ?? s.id,
      exId: se.exId,
      exName: lifts.exOrStub(se.exId).name,
      setId: set.id,
      setAtMs: atMs + this.clockOffsetMs,
      ...(prevLocalAt > 0 ? { prevSetAtMs: prevLocalAt + this.clockOffsetMs } : {}),
    }, 15_000);
    return res?.lanUrl ? this.persistClipFromServer({ session: s, se, set }, res) : null;
  }

  async listBursts(sessionId: string, aroundMs?: number): Promise<GymCamBurst[]> {
    if (!this.online || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return [];
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 2500);
    try {
      const q = new URLSearchParams({ sessionId });
      if (aroundMs != null) q.set('aroundMs', String(Math.round(aroundMs)));
      const res = await fetch(`${this.baseUrl()}/api/bursts?${q.toString()}`, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) return [];
      const data = (await res.json()) as { bursts?: GymCamBurst[] };
      return Array.isArray(data?.bursts) ? data.bursts : [];
    } catch {
      return [];
    } finally {
      clearTimeout(timer);
    }
  }

  async saveBurstClip(
    target: GymCamTarget,
    burst: GymCamBurst,
    startDeltaSec = 0,
    endDeltaSec = 0,
    cameraOverride?: string,
  ): Promise<VideoMeta | null> {
    if (!this.online) return null;
    const res = await this.postJson<ClipApiResponse>('/api/clip', {
      sessionId: target.session.camSessionId ?? target.session.id,
      exId: target.se.exId,
      exName: lifts.exOrStub(target.se.exId).name,
      setId: target.set.id,
      startMs: burst.startMs + startDeltaSec * 1000,
      endMs: burst.endMs + endDeltaSec * 1000,
      camera: cameraOverride || burst.camera,
    }, 15_000);
    return res?.lanUrl ? this.persistClipFromServer(target, res) : null;
  }

  async extendTapeClip(meta: VideoMeta, addBeforeSec: number, addAfterSec: number): Promise<VideoMeta | null> {
    if (!this.online || meta.clipStartMs == null || meta.clipEndMs == null) return null;
    const nextStartMs = Math.max(0, meta.clipStartMs - addBeforeSec * 1000);
    const nextEndMs = Math.max(nextStartMs + 2000, meta.clipEndMs + addAfterSec * 1000);
    const s = lifts.sessions.get(meta.sessionId);
    const res = await this.postJson<ClipApiResponse>('/api/clip', {
      sessionId: s?.camSessionId ?? meta.sessionId,
      exId: meta.exId,
      exName: lifts.exOrStub(meta.exId).name,
      setId: meta.setId,
      startMs: nextStartMs,
      endMs: nextEndMs,
      ...(meta.camera ? { camera: meta.camera } : {}),
    }, 15_000);
    if (!res?.lanUrl) return null;
    const next: VideoMeta = {
      ...meta,
      lanUrl: this.resolveLanUrl(res.lanUrl),
      fileName: res.fileName || meta.fileName,
      camera: res.camera || meta.camera,
      thumb: res.thumb || meta.thumb,
      durationSec: res.durationSec ?? meta.durationSec,
      clipStartMs: res.startMs ?? nextStartMs,
      clipEndMs: res.endMs ?? nextEndMs,
    };
    if (addBeforeSec !== 0) {
      if (next.trimStartSec != null) next.trimStartSec = Math.max(0, next.trimStartSec + addBeforeSec);
      if (next.trimEndSec != null) next.trimEndSec = Math.max(0.5, next.trimEndSec + addBeforeSec);
    }
    await lifts.saveVideoMeta(next);
    this.changed.emit();
    return next;
  }
}

export const gymCam = new GymCamController();
