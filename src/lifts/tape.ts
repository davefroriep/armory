/** Pure helpers for Gym Cam burst selection and tape scrubbing timelines. */

/** Returns the index of the most recent burst ending at/before `checkedMs + 5s`, falling back to closest overall. */
export function pickNearestBurstIndex(
  bursts: readonly { startMs: number; endMs: number }[],
  checkedMs: number,
): number {
  if (!bursts.length) return -1;
  let bestPriorIdx = -1;
  let bestPriorEnd = -Infinity;
  let fallbackIdx = 0;
  let fallbackDiff = Infinity;

  for (let i = 0; i < bursts.length; i++) {
    const end = bursts[i].endMs;
    if (end <= checkedMs + 5000 && end >= bestPriorEnd) {
      bestPriorEnd = end;
      bestPriorIdx = i;
    }
    const diff = Math.abs(end - checkedMs);
    if (diff < fallbackDiff) {
      fallbackDiff = diff;
      fallbackIdx = i;
    }
  }
  return bestPriorIdx >= 0 ? bestPriorIdx : fallbackIdx;
}

/** Computes visible `[winStart, winEnd]` ms window centered on `focusMs` for `5m` / `15m` / `All` zoom. */
export function computeScrubWindow(
  minMs: number,
  maxMs: number,
  focusMs: number,
  zoomMin: number | null,
): { winStart: number; winEnd: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 2000, maxMs);
  if (zoomMin == null || hi - lo <= zoomMin * 60_000) {
    return { winStart: lo, winEnd: hi };
  }
  const span = zoomMin * 60_000;
  const half = Math.round(span / 2);
  const winStart = Math.max(lo, Math.min(hi - span, focusMs - half));
  return { winStart, winEnd: Math.min(hi, winStart + span) };
}

/** Computes zoomed `[winStart, winEnd]` ms range expanding IN/OUT trim markers across the full scrub rail. */
export function computeClipZoomWindow(
  minMs: number,
  maxMs: number,
  inMs: number,
  outMs: number,
  mode: 'in_set' | 'both_set',
): { winStart: number; winEnd: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 2000, maxMs);
  const cIn = Math.max(lo, Math.min(hi, Math.min(inMs, outMs)));
  const cOut = Math.max(Math.min(hi, cIn + 2000), Math.min(hi, Math.max(inMs, outMs)));
  const span = Math.max(3_000, cOut - cIn);
  if (mode === 'in_set') {
    const winStart = Math.max(lo, cIn - 8_000);
    const winEnd = Math.min(hi, Math.max(cOut + 20_000, cIn + Math.max(75_000, Math.round(span * 1.6))));
    return { winStart, winEnd: Math.max(winStart + 4_000, winEnd) };
  }
  const pad = Math.min(15_000, Math.max(4_000, Math.round(span * 0.14)));
  const winStart = Math.max(lo, cIn - pad);
  const winEnd = Math.min(hi, cOut + pad);
  return { winStart, winEnd: Math.max(winStart + 4_000, winEnd) };
}

/** Updates clip IN/OUT marker from `cursorMs`, enforcing a valid `[3s .. 120s]` clip span inside `[minMs, maxMs]`. */
export function applyClipMarker(
  inMs: number,
  outMs: number,
  cursorMs: number,
  side: 'in' | 'out',
  minMs: number,
  maxMs: number,
): { inMs: number; outMs: number } {
  const lo = Math.min(minMs, maxMs);
  const hi = Math.max(lo + 3000, maxMs);
  const cur = Math.max(lo, Math.min(hi, Math.round(cursorMs)));
  if (side === 'in') {
    const nextIn = Math.min(cur, hi - 3000);
    let nextOut = outMs;
    if (nextOut < nextIn + 3000 || nextOut - nextIn > 120_000) {
      nextOut = Math.min(hi, Math.max(nextIn + 3000, nextIn + 45_000));
    }
    return { inMs: nextIn, outMs: nextOut };
  }
  const nextOut = Math.max(cur, lo + 3000);
  let nextIn = inMs;
  if (nextIn > nextOut - 3000 || nextOut - nextIn > 120_000) {
    nextIn = Math.max(lo, Math.min(nextOut - 3000, nextOut - 45_000));
  }
  return { inMs: nextIn, outMs: nextOut };
}

/** Short display label (`"Rack Cam"` -> `"Rack"`, `"Smith Cam"` -> `"Smith"`). */
export function shortCamLabel(name: string): string {
  return name.trim().replace(/\s+cam$/i, '') || name;
}

/** Formats relative offset between `targetMs` and set check time `checkedMs`. */
export function fmtRelativeToCheck(targetMs: number, checkedMs: number): string {
  const dSec = Math.round((checkedMs - targetMs) / 1000);
  if (Math.abs(dSec) <= 3) return 'right at ✓';
  const abs = Math.abs(dSec);
  const m = Math.floor(abs / 60);
  const s = abs % 60;
  const span = m > 0 ? (s ? `${m}m ${s}s` : `${m}m`) : `${abs}s`;
  return dSec > 0 ? `${span} before ✓` : `${span} after ✓`;
}

/** Binary-searches sorted tape frames by `tsMs` and returns index closest to `targetMs` (tie-breaks lower). */
export function findNearestFrameIndex(frames: readonly { tsMs: number }[], targetMs: number): number {
  const n = frames.length;
  if (n === 0) return -1;
  if (n === 1) return 0;
  if (targetMs <= frames[0].tsMs) return 0;
  if (targetMs >= frames[n - 1].tsMs) return n - 1;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (frames[mid].tsMs < targetMs) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) {
    const dPrev = Math.abs(targetMs - frames[lo - 1].tsMs);
    const dCur = Math.abs(frames[lo].tsMs - targetMs);
    if (dPrev <= dCur) return lo - 1;
  }
  return lo;
}

/** Computes percentage progress, elapsed seconds, and total seconds inside a burst `[startMs, endMs]`. */
export function burstScrubPosition(
  startMs: number,
  endMs: number,
  cursorMs: number,
): { pct: number; elapsedSec: number; totalSec: number } {
  const lo = Math.min(startMs, endMs);
  const hi = Math.max(lo + 1000, endMs);
  const clamped = Math.max(lo, Math.min(hi, cursorMs));
  return {
    pct: ((clamped - lo) / (hi - lo)) * 100,
    elapsedSec: Math.max(0, Math.round((clamped - lo) / 1000)),
    totalSec: Math.max(1, Math.round((hi - lo) / 1000)),
  };
}

/** Returns the next frame index (wrapping at the end) for inline burst fast-preview playback. */
export function nextBurstPlayIndex(frames: readonly { tsMs: number }[], currentCursorMs: number): number {
  if (!frames.length) return -1;
  const cur = findNearestFrameIndex(frames, currentCursorMs);
  return (cur + 1) % frames.length;
}

const DAY_MS = 86_400_000;

/** Returns true while a Gym-Cam clip/session timestamp is still within OptiPlex tape retention (`retentionDays`). */
export function isTapeWithinRetention(
  refMs: number | null | undefined,
  retentionDays = 14,
  nowMs = Date.now(),
): boolean {
  if (refMs == null || !Number.isFinite(refMs) || refMs <= 0) return false;
  const days = Math.max(1, Number.isFinite(retentionDays) ? retentionDays : 14);
  return nowMs - refMs < days * DAY_MS;
}

/** Whole remaining days (rounded up, min 0) before raw 2-week tape auto-prunes on OptiPlex. */
export function tapeDaysRemaining(
  refMs: number | null | undefined,
  retentionDays = 14,
  nowMs = Date.now(),
): number {
  if (refMs == null || !Number.isFinite(refMs) || refMs <= 0) return 0;
  const days = Math.max(1, Number.isFinite(retentionDays) ? retentionDays : 14);
  return Math.max(0, Math.ceil((refMs + days * DAY_MS - nowMs) / DAY_MS));
}

export interface CoachAngleOption {
  camera: string;
  label: string;
  kind: 'primary' | 'saved' | 'tape';
}

/** Builds deduplicated, configured-order angle switcher options across primary, saved siblings, and raw tape. */
export function buildCoachAngleOptions(opts: {
  primaryCamera?: string;
  savedSiblingCameras?: readonly string[];
  tapeCameras?: readonly string[];
  configuredCameras?: readonly string[];
}): CoachAngleOption[] {
  const clean = (s?: string): string => (s ?? '').trim();
  const primary = clean(opts.primaryCamera);
  const savedSet = new Set<string>();
  const availSet = new Set<string>();

  if (primary) availSet.add(primary);
  for (const raw of opts.savedSiblingCameras ?? []) {
    const cam = clean(raw);
    if (cam) {
      savedSet.add(cam);
      availSet.add(cam);
    }
  }
  for (const raw of opts.tapeCameras ?? []) {
    const cam = clean(raw);
    if (cam) availSet.add(cam);
  }
  if (!availSet.size) return [];

  const ordered: string[] = [];
  const seen = new Set<string>();
  const push = (cam: string): void => {
    if (!cam || !availSet.has(cam) || seen.has(cam)) return;
    seen.add(cam);
    ordered.push(cam);
  };

  for (const raw of opts.configuredCameras ?? []) push(clean(raw));
  push(primary);
  for (const raw of opts.savedSiblingCameras ?? []) push(clean(raw));
  for (const raw of opts.tapeCameras ?? []) push(clean(raw));

  return ordered.map((camera) => ({
    camera,
    label: shortCamLabel(camera),
    kind: camera === primary ? 'primary' : savedSet.has(camera) ? 'saved' : 'tape',
  }));
}

export interface ReplayableClipView {
  id: string;
  camera?: string;
  lanUrl?: string;
}

/**
 * Returns the deduplicated, preferred-order list of recorded Gym-Cam views for a set
 * (keeping only clips with a playable `lanUrl`, letting the newest clip win per camera).
 */
export function orderedRecordedViews<T extends ReplayableClipView>(
  clips: readonly T[],
  preferredCameras?: readonly string[],
): T[] {
  const byKey = new Map<string, T>();
  const keyOrder: string[] = [];
  for (const clip of clips) {
    if (!clip?.lanUrl?.trim()) continue;
    const camKey = (clip.camera ?? '').trim().toLowerCase() || `id:${clip.id}`;
    if (!byKey.has(camKey)) keyOrder.push(camKey);
    byKey.set(camKey, clip);
  }
  const out: T[] = [];
  const used = new Set<string>();
  for (const rawCam of preferredCameras ?? []) {
    const key = rawCam.trim().toLowerCase();
    if (!key || used.has(key)) continue;
    const hit = byKey.get(key);
    if (hit) {
      used.add(key);
      out.push(hit);
    }
  }
  for (const key of keyOrder) {
    if (used.has(key)) continue;
    const hit = byKey.get(key);
    if (hit) {
      used.add(key);
      out.push(hit);
    }
  }
  return out;
}

/**
 * Picks the next recorded view after the currently replaying clip/camera (wrapping around).
 * Returns `null` when fewer than 2 distinct recorded views exist.
 */
export function nextRecordedView<T extends ReplayableClipView>(
  clips: readonly T[],
  current: { id?: string; camera?: string },
  preferredCameras?: readonly string[],
): { next: T; index: number; total: number } | null {
  const views = orderedRecordedViews(clips, preferredCameras);
  if (views.length < 2) return null;
  const curId = current.id ?? '';
  const curCam = (current.camera ?? '').trim().toLowerCase();
  let curIdx = curId ? views.findIndex((v) => v.id === curId) : -1;
  if (curIdx < 0 && curCam) {
    curIdx = views.findIndex((v) => (v.camera ?? '').trim().toLowerCase() === curCam);
  }
  const nextIdx = curIdx >= 0 ? (curIdx + 1) % views.length : 0;
  return { next: views[nextIdx], index: nextIdx, total: views.length };
}
