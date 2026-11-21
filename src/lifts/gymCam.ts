import { Emitter } from '../emitter';
import { getSettings, updateSettings } from '../store/settings';
import { lifts } from './store';
import { uid, type LiftSession, type LiftSet, type SessionEx, type VideoMeta } from './types';

export const GYM_CAM_PREVIEW_DRIVE_FALLBACK = 'https://drive.google.com/drive/search?q=Armory_Cam_Preview.jpg';

export function formatPreviewAge(updatedMs: number | null, nowMs = Date.now()): string {
  if (updatedMs == null || !Number.isFinite(updatedMs) || updatedMs <= 0) {
    return 'Current camera views on Google Drive';
  }
  const diffMs = Math.max(0, nowMs - updatedMs);
  if (diffMs < 90_000) return 'Updated just now';
  const diffMin = Math.round(diffMs / 60_000);
  if (diffMin < 60) return `Updated ${diffMin}m ago`;
  const diffHr = Math.round(diffMs / 3_600_000);
  if (diffHr < 36) return `Updated ${diffHr}h ago`;
  const monDay = new Date(updatedMs).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return `Updated ${monDay}`;
}

export interface GymCamConfigLinkState {
  mode: 'local' | 'offline';
  href: string;
  btnLabel: string;
  btnPrimary: boolean;
  showOfflineBanner: boolean;
  previewHref: string;
  bannerSub: string;
}

export function gymCamConfigLinkState(args: {
  online: boolean;
  probing: boolean;
  enabled: boolean;
  baseUrl: string;
  previewUrl: string;
  previewUpdatedMs: number | null;
  nowMs?: number;
}): GymCamConfigLinkState {
  const rawBase = (args.baseUrl || '').trim().replace(/\/+$/, '');
  const configHref = /^https?:\/\//i.test(rawBase) ? rawBase : 'http://192.168.1.152:8766';
  const rawPrev = (args.previewUrl || '').trim();
  const previewHref = /^https?:\/\//i.test(rawPrev) ? rawPrev : GYM_CAM_PREVIEW_DRIVE_FALLBACK;
  if (args.enabled && args.online) {
    return {
      mode: 'local',
      href: configHref,
      btnLabel: 'Config ↗',
      btnPrimary: true,
      showOfflineBanner: false,
      previewHref,
      bannerSub: '',
    };
  }
  const updatedMs =
    args.previewUpdatedMs != null && Number.isFinite(args.previewUpdatedMs) && args.previewUpdatedMs > 0
      ? args.previewUpdatedMs
      : null;
  const bannerSub = updatedMs
    ? `${formatPreviewAge(updatedMs, args.nowMs)} · Google Drive`
    : 'Current camera views on Google Drive';
  return {
    mode: 'offline',
    href: previewHref,
    btnLabel: 'Preview ↗',
    btnPrimary: false,
    showOfflineBanner: !args.probing || !args.enabled,
    previewHref,
    bannerSub,
  };
}

export interface GymCamBurst {
  id: string | number;
  camera: string;
  startMs: number;
  endMs: number;
  durationSec: number;
  thumb?: string;
  note?: string;
}

export interface GymCamTimeline {
  ok: boolean;
  camera: string;
  minMs: number;
  maxMs: number;
  segmentCount: number;
  activity: { tMs: number; e: number }[];
  bursts30: { startMs: number; endMs: number; durationSec: number }[];
}

export interface GymCamTapeFrame {
  tsMs: number;
  url: string;
}

export interface GymCamProbeInfo {
  online: boolean;
  recording: boolean;
  paused: boolean;
  retentionDays: number;
  cameras: string[];
  latencyMs: number;
  previewDriveUrl?: string;
  previewUpdatedMs?: number | null;
}

export interface GymCamPreviewResult {
  lanUrl: string;
  fileName: string;
  camera: string;
  durationSec: number;
  startMs: number;
  endMs: number;
  bytes: number;
  cached?: boolean;
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
  hasAudio?: boolean;
  audioSource?: string;
  driveFileId?: string;
  driveUrl?: string;
}

class GymCamController {
  online = false;
  recording = false;
  paused = false;
  retentionDays = 14;
  cameras: string[] = [];
  clockOffsetMs = 0;
  armed: { sessionId: string; exUid: string; setId: string; startEpochMs: number; camera?: string } | null = null;
  savingSetId: string | null = null;
  guestArmed: { sessionId: string; startEpochMs: number; camera?: string } | null = null;
  savingGuest = false;
  private probeMisses = 0;
  private driveSyncInFlight = false;
  private lastSyncMs = 0;

  readonly changed = new Emitter<void>();

  baseUrl(): string {
    return (getSettings().gymCamUrl || 'http://192.168.1.152:8766').trim().replace(/\/+$/, '');
  }

  configUrl(): string {
    return this.baseUrl();
  }

  previewDriveUrl(): string {
    const u = (getSettings().gymCamPreviewUrl || '').trim();
    return /^https?:\/\//i.test(u) ? u : GYM_CAM_PREVIEW_DRIVE_FALLBACK;
  }

  previewUpdatedMs(): number | null {
    const v = getSettings().gymCamPreviewUpdatedMs;
    return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null;
  }

  serverNowMs(): number {
    return Date.now() + this.clockOffsetMs;
  }

  isArmed(setId: string): boolean {
    return this.armed?.setId === setId;
  }

  isSaving(setId: string): boolean {
    return this.savingSetId === setId;
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

  async probe(timeoutMs = 2500, strict = false): Promise<GymCamProbeInfo> {
    if (!getSettings().gymCamEnabled || typeof fetch === 'undefined' || typeof AbortController === 'undefined') {
      const was = this.online || this.recording;
      this.probeMisses = 0;
      this.online = false;
      this.recording = false;
      this.paused = false;
      this.cameras = [];
      if (was) this.changed.emit();
      return {
        online: false,
        recording: false,
        paused: false,
        retentionDays: this.retentionDays,
        cameras: [],
        latencyMs: 0,
        previewDriveUrl: this.previewDriveUrl(),
        previewUpdatedMs: this.previewUpdatedMs(),
      };
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
        retentionDays?: number;
        cameras?: string[];
        previewDriveUrl?: string;
        previewUpdatedMs?: number | null;
      };
      if (data?.service !== 'armory-cam') throw new Error('Not armory-cam');
      this.probeMisses = 0;
      this.clockOffsetMs = typeof data.serverEpochMs === 'number' ? data.serverEpochMs - Date.now() : 0;
      this.online = true;
      this.recording = !!data.recording;
      this.paused = !!data.paused;
      if (typeof data.retentionDays === 'number' && Number.isFinite(data.retentionDays) && data.retentionDays >= 1) {
        this.retentionDays = Math.round(data.retentionDays);
      }
      this.cameras = Array.isArray(data.cameras) ? data.cameras : [];
      const curSettings = getSettings();
      const nextPreviewUrl =
        typeof data.previewDriveUrl === 'string' && /^https?:\/\//i.test(data.previewDriveUrl.trim())
          ? data.previewDriveUrl.trim()
          : '';
      const nextUpdatedMs =
        typeof data.previewUpdatedMs === 'number' && Number.isFinite(data.previewUpdatedMs) && data.previewUpdatedMs > 0
          ? Math.round(data.previewUpdatedMs)
          : null;
      const patch: Partial<Pick<typeof curSettings, 'gymCamPreviewUrl' | 'gymCamPreviewUpdatedMs'>> = {};
      if (nextPreviewUrl && nextPreviewUrl !== curSettings.gymCamPreviewUrl) {
        patch.gymCamPreviewUrl = nextPreviewUrl;
      }
      if (nextUpdatedMs !== null && nextUpdatedMs !== curSettings.gymCamPreviewUpdatedMs) {
        patch.gymCamPreviewUpdatedMs = nextUpdatedMs;
      }
      if (patch.gymCamPreviewUrl !== undefined || patch.gymCamPreviewUpdatedMs !== undefined) {
        updateSettings(patch);
      }
      this.changed.emit();
      if (Date.now() - this.lastSyncMs > 60_000) {
        this.lastSyncMs = Date.now();
        void this.syncDriveLinks();
      }
      return {
        online: true,
        recording: this.recording,
        paused: this.paused,
        retentionDays: this.retentionDays,
        cameras: this.cameras,
        latencyMs,
        previewDriveUrl: this.previewDriveUrl(),
        previewUpdatedMs: this.previewUpdatedMs(),
      };
    } catch {
      this.probeMisses += 1;
      if (strict || !this.online || this.probeMisses >= 2) {
        const was = this.online || this.recording;
        this.online = false;
        this.recording = false;
        this.paused = false;
        this.cameras = [];
        if (was) this.changed.emit();
      }
      return {
        online: this.online,
        recording: this.recording,
        paused: this.paused,
        retentionDays: this.retentionDays,
        cameras: this.cameras,
        latencyMs: 0,
        previewDriveUrl: this.previewDriveUrl(),
        previewUpdatedMs: this.previewUpdatedMs(),
      };
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
    this.guestArmed = null;
    if (!this.online) return;
    await this.postJson('/api/session/stop', { sessionId: s.camSessionId ?? s.id, discard: false });
    this.recording = false;
    this.paused = false;
    this.changed.emit();
  }

  async sessionDiscard(s: LiftSession): Promise<void> {
    this.armed = null;
    this.guestArmed = null;
    if (!this.online) return;
    await this.postJson('/api/session/stop', { sessionId: s.camSessionId ?? s.id, discard: true });
    this.recording = false;
    this.paused = false;
    this.changed.emit();
  }

  armSet(s: LiftSession, se: SessionEx, set: LiftSet, camera?: string): boolean {
    if (!this.online) return false;
    const cam = camera?.trim() || undefined;
    this.armed = { sessionId: s.id, exUid: se.uid, setId: set.id, startEpochMs: this.serverNowMs(), ...(cam ? { camera: cam } : {}) };
    this.changed.emit();
    return true;
  }

  resolveLanUrl(raw?: string): string {
    if (!raw) return '';
    return /^https?:\/\//i.test(raw) ? raw : `${this.baseUrl()}${raw.startsWith('/') ? '' : '/'}${raw}`;
  }

  /** Upgrade Gym-Cam VideoMeta rows with direct Google Drive `file/d/.../view` URLs resolved on OptiPlex. */
  async syncDriveLinks(): Promise<number> {
    if (!this.online || this.driveSyncInFlight || typeof fetch === 'undefined') return 0;
    const missing = [...lifts.videos.values()].filter(
      (v) => v.source === 'gymcam' && !!v.fileName && !/^https?:\/\/drive\.google\.com\/file\/d\//i.test(v.uri ?? ''),
    );
    if (!missing.length) return 0;
    this.driveSyncInFlight = true;
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 5000) : null;
    try {
      const res = await fetch(`${this.baseUrl()}/api/clips?limit=200`, {
        ...(ctrl ? { signal: ctrl.signal } : {}),
        cache: 'no-store',
      });
      if (!res.ok) return 0;
      const body = (await res.json()) as { clips?: { fileName?: string; driveUrl?: string }[] };
      const byFile = new Map<string, string>();
      for (const c of body.clips ?? []) {
        if (c.fileName && c.driveUrl && /^https?:\/\//i.test(c.driveUrl)) {
          byFile.set(c.fileName, c.driveUrl);
        }
      }
      let updated = 0;
      for (const v of missing) {
        const url = v.fileName ? byFile.get(v.fileName) : undefined;
        if (url && url !== v.uri) {
          await lifts.saveVideoMeta({ ...v, uri: url });
          updated++;
        }
      }
      if (updated) this.changed.emit();
      return updated;
    } catch {
      return 0;
    } finally {
      if (timer) clearTimeout(timer);
      this.driveSyncInFlight = false;
    }
  }

  armGuest(s: LiftSession, camera?: string): void {
    const cam = camera?.trim();
    this.guestArmed = {
      sessionId: s.camSessionId ?? s.id,
      startEpochMs: this.serverNowMs(),
      ...(cam ? { camera: cam } : {}),
    };
    this.changed.emit();
  }

  cancelGuest(): void {
    if (!this.guestArmed) return;
    this.guestArmed = null;
    this.changed.emit();
  }

  async stopAndSaveGuest(s: LiftSession): Promise<VideoMeta | null> {
    if (!this.guestArmed || this.savingGuest) return null;
    const { startEpochMs: startMs, camera, sessionId } = this.guestArmed;
    const endMs = Math.max(startMs + 3000, this.serverNowMs() + 2000);
    this.guestArmed = null;
    this.savingGuest = true;
    this.changed.emit();
    try {
      const body = {
        sessionId: sessionId || (s.camSessionId ?? s.id),
        exId: '_guest',
        exName: 'Guest Clip',
        setId: `_guest_${uid()}`,
        startMs,
        endMs,
        ...(camera ? { camera } : {}),
      };
      let res = await this.postJson<ClipApiResponse>('/api/clip', body, 45_000);
      if (!res) {
        await new Promise((r) => setTimeout(r, 1200));
        res = await this.postJson<ClipApiResponse>('/api/clip', body, 45_000);
      }
      const lanUrl = this.resolveLanUrl(res?.lanUrl);
      if (!lanUrl || !res) return null;
      this.online = true;
      this.probeMisses = 0;
      const meta: VideoMeta = {
        id: uid(),
        sessionId: s.id,
        exId: '_guest',
        setId: '_guest',
        ms: Date.now(),
        mime: 'video/mp4',
        bytes: res.bytes ?? 0,
        thumb: res.thumb ?? '',
        linkOnly: true,
        guest: true,
        source: 'gymcam',
        camera: res.camera || camera || 'Rack Cam',
        lanUrl,
        ...(res.fileName ? { fileName: res.fileName } : {}),
        ...(res.durationSec ? { durationSec: res.durationSec } : {}),
        ...(res.startMs != null ? { clipStartMs: res.startMs } : {}),
        ...(res.endMs != null ? { clipEndMs: res.endMs } : {}),
        ...(res.hasAudio !== undefined ? { hasAudio: !!res.hasAudio } : {}),
        ...(res.driveUrl ? { uri: res.driveUrl } : {}),
      };
      await lifts.addVideo(meta);
      this.changed.emit();
      return meta;
    } finally {
      this.savingGuest = false;
      this.changed.emit();
    }
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
      ...(clip.hasAudio !== undefined ? { hasAudio: !!clip.hasAudio } : {}),
      ...(clip.driveUrl ? { uri: clip.driveUrl } : {}),
    };
    await lifts.addVideo(meta);
    target.set.vids = [...(target.set.vids ?? []), meta.id];
    lifts.saveSession(target.session, true);
    this.changed.emit();
    return meta;
  }

  async stopAndSaveArmed(s: LiftSession, se: SessionEx, set: LiftSet): Promise<VideoMeta | null> {
    if (!this.armed || this.armed.setId !== set.id || this.savingSetId === set.id) return null;
    const { startEpochMs: startMs, camera } = this.armed;
    const endMs = Math.max(startMs + 3000, this.serverNowMs() + 2000);
    this.armed = null;
    this.savingSetId = set.id;
    this.changed.emit();
    try {
      const exName = lifts.exOrStub(se.exId).name;
      const body = {
        sessionId: s.camSessionId ?? s.id,
        exId: se.exId,
        exName,
        setId: set.id,
        startMs,
        endMs,
        ...(camera ? { camera } : {}),
      };
      let res = await this.postJson<ClipApiResponse>('/api/clip', body, 45_000);
      if (!res) {
        await new Promise((r) => setTimeout(r, 1200));
        res = await this.postJson<ClipApiResponse>('/api/clip', body, 45_000);
      }
      if (res?.lanUrl) {
        this.online = true;
        this.probeMisses = 0;
        return await this.persistClipFromServer({ session: s, se, set }, res);
      }
      return null;
    } finally {
      if (this.savingSetId === set.id) this.savingSetId = null;
      this.changed.emit();
    }
  }

  /**
   * Auto-clips a PR set using server motion-burst alignment while filtering out
   * rapid batch check-check-check timestamps (<20s apart).
   */
  async autoClipPr(s: LiftSession, se: SessionEx, set: LiftSet, camera?: string, force = false): Promise<VideoMeta | null> {
    if (!this.online || (!force && !getSettings().gymCamAutoPr)) return null;
    const atMs = set.at ?? Date.now();
    let prevLocalAt = 0;
    for (const ex of s.exs) {
      for (const st of ex.sets) {
        if (st.id !== set.id && st.done && st.at != null && st.at < atMs - 20_000 && st.at > prevLocalAt) {
          prevLocalAt = st.at;
        }
      }
    }
    const cam = camera?.trim();
    const res = await this.postJson<ClipApiResponse>('/api/clip/auto-pr', {
      sessionId: s.camSessionId ?? s.id,
      exId: se.exId,
      exName: lifts.exOrStub(se.exId).name,
      setId: set.id,
      setAtMs: atMs + this.clockOffsetMs,
      ...(prevLocalAt > 0 ? { prevSetAtMs: prevLocalAt + this.clockOffsetMs } : {}),
      ...(cam ? { camera: cam } : {}),
    }, 45_000);
    return res?.lanUrl ? this.persistClipFromServer({ session: s, se, set }, res) : null;
  }

  async listBursts(
    sessionId: string,
    aroundMs?: number,
    opts?: { camera?: string; minDurSec?: number },
  ): Promise<GymCamBurst[]> {
    if (!this.online || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return [];
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 4500);
    try {
      const q = new URLSearchParams({ sessionId });
      if (aroundMs != null) q.set('aroundMs', String(Math.round(aroundMs)));
      if (opts?.camera) q.set('camera', opts.camera);
      if (opts?.minDurSec != null) q.set('minDurSec', String(opts.minDurSec));
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

  async getTapeTimeline(sessionId: string, camera: string): Promise<GymCamTimeline | null> {
    if (!this.online || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 3500);
    try {
      const q = new URLSearchParams({ sessionId, camera });
      const res = await fetch(`${this.baseUrl()}/api/tape/timeline?${q.toString()}`, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) return null;
      return (await res.json()) as GymCamTimeline;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async fetchTapePack(
    sessionId: string,
    camera: string,
    startMs?: number,
    endMs?: number,
    count = 80,
    signal?: AbortSignal,
  ): Promise<GymCamTapeFrame[]> {
    if (!this.online || typeof fetch === 'undefined') return [];
    try {
      const q = new URLSearchParams({
        sessionId,
        camera,
        count: String(count),
        w: '280',
      });
      if (startMs != null) q.set('startMs', String(Math.round(startMs)));
      if (endMs != null) q.set('endMs', String(Math.round(endMs)));
      const res = await fetch(`${this.baseUrl()}/api/tape/pack?${q.toString()}`, { signal, cache: 'no-store' });
      if (!res.ok) return [];
      const data = (await res.json()) as { frames?: { tsMs?: number; jpg?: string }[] };
      const rawFrames = Array.isArray(data?.frames) ? data.frames : [];
      const out: GymCamTapeFrame[] = [];
      for (const item of rawFrames) {
        if (typeof item?.tsMs !== 'number' || typeof item?.jpg !== 'string' || !item.jpg) continue;
        const bin = atob(item.jpg);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const blob = new Blob([bytes], { type: 'image/jpeg' });
        out.push({ tsMs: item.tsMs, url: URL.createObjectURL(blob) });
      }
      out.sort((a, b) => a.tsMs - b.tsMs);
      return out;
    } catch {
      return [];
    }
  }

  async fetchTapeFrameBlobUrl(
    sessionId: string,
    camera: string,
    tsMs: number,
    signal?: AbortSignal,
    width = 280,
  ): Promise<string | null> {
    if (!this.online || typeof fetch === 'undefined') return null;
    try {
      const q = new URLSearchParams({
        sessionId,
        camera,
        tsMs: String(Math.round(tsMs)),
        w: String(width),
      });
      const res = await fetch(`${this.baseUrl()}/api/tape/frame?${q.toString()}`, { signal });
      if (!res.ok) return null;
      const blob = await res.blob();
      return URL.createObjectURL(blob);
    } catch {
      return null;
    }
  }

  async saveBurstClip(
    target: GymCamTarget,
    burst: GymCamBurst,
    startDeltaSec = 0,
    endDeltaSec = 0,
    cameraOverride?: string,
    opts?: { includeAudio?: boolean },
  ): Promise<VideoMeta | null> {
    return this.saveWindowClip(
      target,
      cameraOverride || burst.camera,
      burst.startMs + startDeltaSec * 1000,
      burst.endMs + endDeltaSec * 1000,
      opts,
    );
  }

  async saveWindowClip(
    target: GymCamTarget,
    camera: string,
    startMs: number,
    endMs: number,
    opts?: { includeAudio?: boolean },
  ): Promise<VideoMeta | null> {
    if (!this.online) return null;
    const res = await this.postJson<ClipApiResponse>('/api/clip', {
      sessionId: target.session.camSessionId ?? target.session.id,
      exId: target.se.exId,
      exName: lifts.exOrStub(target.se.exId).name,
      setId: target.set.id,
      startMs: Math.round(startMs),
      endMs: Math.round(endMs),
      camera,
      includeAudio: !!opts?.includeAudio,
    }, 45_000);
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
      exName: meta.guest ? 'Guest Clip' : lifts.exOrStub(meta.exId).name,
      setId: meta.setId,
      startMs: nextStartMs,
      endMs: nextEndMs,
      includeAudio: !!meta.hasAudio,
      ...(meta.camera ? { camera: meta.camera } : {}),
    }, 45_000);
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
      ...(res.hasAudio !== undefined ? { hasAudio: !!res.hasAudio } : {}),
      ...(res.driveUrl ? { uri: res.driveUrl } : {}),
    };
    if (addBeforeSec !== 0) {
      if (next.trimStartSec != null) next.trimStartSec = Math.max(0, next.trimStartSec + addBeforeSec);
      if (next.trimEndSec != null) next.trimEndSec = Math.max(0.5, next.trimEndSec + addBeforeSec);
    }
    await lifts.saveVideoMeta(next);
    this.changed.emit();
    if (meta.fileName && res.fileName && res.fileName !== meta.fileName) {
      void this.deleteClip(meta);
    }
    return next;
  }

  async deleteClip(meta: Pick<VideoMeta, 'lanUrl' | 'fileName' | 'source'>): Promise<boolean> {
    if (!this.online || meta.source !== 'gymcam' || (!meta.lanUrl && !meta.fileName)) return false;
    const res = await this.postJson<{ ok?: boolean }>('/api/clip/delete', {
      lanUrl: meta.lanUrl ?? '',
      fileName: meta.fileName ?? '',
    }, 8000);
    return !!res?.ok;
  }

  async switchClipCamera(meta: VideoMeta, nextCamera: string): Promise<VideoMeta | null> {
    const cam = nextCamera.trim();
    if (!this.online || !cam || meta.clipStartMs == null || meta.clipEndMs == null) return null;
    const s = lifts.sessions.get(meta.sessionId);
    const res = await this.postJson<ClipApiResponse>('/api/clip', {
      sessionId: s?.camSessionId ?? meta.sessionId,
      exId: meta.exId,
      exName: meta.guest ? 'Guest Clip' : lifts.exOrStub(meta.exId).name,
      setId: meta.setId,
      startMs: meta.clipStartMs,
      endMs: meta.clipEndMs,
      camera: cam,
      includeAudio: !!meta.hasAudio,
    }, 45_000);
    if (!res?.lanUrl) return null;
    const next: VideoMeta = {
      ...meta,
      lanUrl: this.resolveLanUrl(res.lanUrl),
      fileName: res.fileName || meta.fileName,
      camera: res.camera || cam,
      thumb: res.thumb || meta.thumb,
      bytes: res.bytes ?? meta.bytes,
      durationSec: res.durationSec ?? meta.durationSec,
      ...(res.hasAudio !== undefined ? { hasAudio: !!res.hasAudio } : {}),
      ...(res.driveUrl ? { uri: res.driveUrl } : {}),
    };
    await lifts.saveVideoMeta(next);
    this.changed.emit();
    if (meta.fileName && res.fileName && res.fileName !== meta.fileName) {
      void this.deleteClip(meta);
    }
    return next;
  }

  roomAudioUrl(startMs: number, endMs: number): string {
    return `${this.baseUrl()}/api/audio/clip?startMs=${Math.round(startMs)}&endMs=${Math.round(endMs)}`;
  }

  async checkRoomAudio(startMs: number, endMs: number): Promise<boolean> {
    if (!this.online || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return false;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 3500);
    try {
      const q = new URLSearchParams({
        startMs: String(Math.round(startMs)),
        endMs: String(Math.round(endMs)),
      });
      const res = await fetch(`${this.baseUrl()}/api/audio/availability?${q.toString()}`, {
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (!res.ok) return false;
      const data = (await res.json()) as { available?: boolean; retentionDays?: number };
      if (typeof data.retentionDays === 'number' && Number.isFinite(data.retentionDays) && data.retentionDays >= 1) {
        this.retentionDays = Math.round(data.retentionDays);
      }
      return !!data.available;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async getTapeMediaInfo(
    sessionId: string,
    startMs: number,
    endMs: number,
  ): Promise<{ cameras: string[]; audioAvailable: boolean } | null> {
    if (!this.online || typeof fetch === 'undefined' || typeof AbortController === 'undefined') return null;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 3500);
    try {
      const q = new URLSearchParams({
        sessionId,
        startMs: String(Math.round(startMs)),
        endMs: String(Math.round(endMs)),
      });
      const res = await fetch(`${this.baseUrl()}/api/tape/angles?${q.toString()}`, {
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { retentionDays?: number; cameras?: string[]; audioAvailable?: boolean };
      if (typeof data.retentionDays === 'number' && Number.isFinite(data.retentionDays) && data.retentionDays >= 1) {
        this.retentionDays = Math.round(data.retentionDays);
      }
      return {
        cameras: Array.isArray(data.cameras) ? data.cameras : [],
        audioAvailable: !!data.audioAvailable,
      };
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async getTapeAngles(sessionId: string, startMs: number, endMs: number): Promise<string[] | null> {
    const info = await this.getTapeMediaInfo(sessionId, startMs, endMs);
    return info ? info.cameras : null;
  }

  async setClipAudio(meta: VideoMeta, includeAudio: boolean): Promise<VideoMeta | null> {
    if (!this.online || meta.source !== 'gymcam' || (!meta.lanUrl && !meta.fileName)) return null;
    const res = await this.postJson<ClipApiResponse>(
      '/api/clip/audio',
      {
        lanUrl: meta.lanUrl ?? '',
        fileName: meta.fileName ?? '',
        ...(meta.clipStartMs != null ? { startMs: meta.clipStartMs } : {}),
        ...(meta.clipEndMs != null ? { endMs: meta.clipEndMs } : {}),
        includeAudio,
      },
      45_000,
    );
    if (!res?.ok) return null;
    const next: VideoMeta = {
      ...meta,
      ...(res.lanUrl ? { lanUrl: this.resolveLanUrl(res.lanUrl) } : {}),
      ...(res.fileName ? { fileName: res.fileName } : {}),
      bytes: res.bytes ?? meta.bytes,
      hasAudio: !!res.hasAudio,
      ...(res.driveUrl ? { uri: res.driveUrl } : {}),
    };
    await lifts.saveVideoMeta(next);
    this.changed.emit();
    return next;
  }

  async previewClipCamera(meta: VideoMeta, nextCamera: string): Promise<GymCamPreviewResult | null> {
    const cam = nextCamera.trim();
    if (!this.online || !cam || meta.clipStartMs == null || meta.clipEndMs == null) return null;
    const s = lifts.sessions.get(meta.sessionId);
    const res = await this.postJson<ClipApiResponse & { cached?: boolean }>(
      '/api/tape/preview',
      {
        sessionId: s?.camSessionId ?? meta.sessionId,
        startMs: meta.clipStartMs,
        endMs: meta.clipEndMs,
        camera: cam,
      },
      45_000,
    );
    const lanUrl = this.resolveLanUrl(res?.lanUrl);
    if (!res?.ok || !lanUrl) return null;
    return {
      lanUrl,
      fileName: res.fileName || '',
      camera: res.camera || cam,
      durationSec: res.durationSec ?? meta.durationSec ?? 0,
      startMs: res.startMs ?? meta.clipStartMs,
      endMs: res.endMs ?? meta.clipEndMs,
      bytes: res.bytes ?? 0,
      ...(res.cached !== undefined ? { cached: res.cached } : {}),
    };
  }

  async saveAdditionalAngle(target: GymCamTarget, meta: VideoMeta, nextCamera: string): Promise<VideoMeta | null> {
    const cam = nextCamera.trim();
    if (!this.online || !cam || meta.clipStartMs == null || meta.clipEndMs == null) return null;
    const saved = await this.saveWindowClip(target, cam, meta.clipStartMs, meta.clipEndMs, {
      includeAudio: !!meta.hasAudio,
    });
    if (saved && (meta.trimStartSec != null || meta.trimEndSec != null)) {
      if (meta.trimStartSec != null) saved.trimStartSec = meta.trimStartSec;
      if (meta.trimEndSec != null) saved.trimEndSec = meta.trimEndSec;
      await lifts.saveVideoMeta(saved);
    }
    return saved;
  }
}

export const gymCam = new GymCamController();
