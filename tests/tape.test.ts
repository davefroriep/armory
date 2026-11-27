import { describe, expect, it, vi } from 'vitest';

vi.mock('../src/lifts/store', () => ({
  lifts: {
    videos: new Map(),
    sessions: new Map(),
    routines: new Map(),
    row: () => undefined,
    bestE1rm: () => null,
    previous: () => null,
    changed: { on: () => () => {} },
    saveVideoMeta: vi.fn(),
    addVideo: vi.fn(),
    saveSession: vi.fn(),
    exOrStub: (id: string) => ({ id, name: id, equip: 'smith', type: 'weight', primary: [], secondary: [] }),
  },
}));

vi.mock('../src/wallboard/sync', () => ({
  wallboard: { pushNow: vi.fn() },
}));

import {
  formatPreviewAge,
  GYM_CAM_PREVIEW_DRIVE_FALLBACK,
  gymCam,
  gymCamConfigLinkState,
} from '../src/lifts/gymCam';
import { lifts } from '../src/lifts/store';
import { castClipToWallboard, closeWallboardReplay, switchWallboardReplayView } from '../src/ui/lifts/video';
import { getWallboardReplay } from '../src/wallboard/state';
import { getSettings, updateSettings } from '../src/store/settings';
import {
  applyClipMarker,
  buildCoachAngleOptions,
  burstScrubPosition,
  computeClipZoomWindow,
  computeScrubWindow,
  findNearestFrameIndex,
  fmtRelativeToCheck,
  formatCamListSummary,
  isTapeWithinRetention,
  nextBurstPlayIndex,
  nextRecordedView,
  orderedRecordedViews,
  orderSheetCameras,
  pickNearestBurstIndex,
  shortCamLabel,
  tapeDaysRemaining,
} from '../src/lifts/tape';

describe('tape linking & scrubbing helpers', () => {
  it('picks the most recent burst ending before or near checkedMs (ignoring buddy sets later)', () => {
    const bursts = [
      { startMs: 100_000, endMs: 140_000 },
      { startMs: 220_000, endMs: 260_000 },
      { startMs: 310_000, endMs: 350_000 }, // buddy went after you checked off at 268_000
    ];
    expect(pickNearestBurstIndex([], 268_000)).toBe(-1);
    expect(pickNearestBurstIndex(bursts, 268_000)).toBe(1);
    expect(pickNearestBurstIndex(bursts, 263_000)).toBe(1); // slightly inside +5s tolerance
    expect(pickNearestBurstIndex(bursts, 142_000)).toBe(0);
    // Checked earlier than any recorded burst -> falls back to closest overall (index 0)
    expect(pickNearestBurstIndex(bursts, 80_000)).toBe(0);
  });

  it('computes 5m / 15m / All scrub windows clamped inside tape bounds', () => {
    const minMs = 1_000_000;
    const maxMs = minMs + 30 * 60_000; // 30m session

    expect(computeScrubWindow(minMs, maxMs, minMs + 10 * 60_000, null)).toEqual({
      winStart: minMs,
      winEnd: maxMs,
    });

    const w5 = computeScrubWindow(minMs, maxMs, minMs + 12 * 60_000, 5);
    expect(w5.winEnd - w5.winStart).toBe(5 * 60_000);
    expect(w5.winStart).toBe(minMs + 9.5 * 60_000);

    // Clamped near start
    const wStart = computeScrubWindow(minMs, maxMs, minMs + 30_000, 5);
    expect(wStart.winStart).toBe(minMs);
    expect(wStart.winEnd).toBe(minMs + 5 * 60_000);

    // Clamped near end
    const wEnd = computeScrubWindow(minMs, maxMs, maxMs - 20_000, 15);
    expect(wEnd.winEnd).toBe(maxMs);
    expect(wEnd.winStart).toBe(maxMs - 15 * 60_000);
  });

  it('updates IN/OUT clip markers cleanly while keeping [3s .. 120s] valid windows', () => {
    const minMs = 100_000;
    const maxMs = 400_000;

    // Moving IN past existing OUT auto-adjusts OUT to +45s
    expect(applyClipMarker(110_000, 140_000, 200_000, 'in', minMs, maxMs)).toEqual({
      inMs: 200_000,
      outMs: 245_000,
    });

    // Moving IN way too far back (>120s span) tightens OUT to +45s
    expect(applyClipMarker(250_000, 300_000, 120_000, 'in', minMs, maxMs)).toEqual({
      inMs: 120_000,
      outMs: 165_000,
    });

    // Setting OUT normally keeps existing valid IN
    expect(applyClipMarker(200_000, 245_000, 232_000, 'out', minMs, maxMs)).toEqual({
      inMs: 200_000,
      outMs: 232_000,
    });

    // Setting OUT before IN pulls IN back by 45s
    expect(applyClipMarker(250_000, 290_000, 180_000, 'out', minMs, maxMs)).toEqual({
      inMs: 135_000,
      outMs: 180_000,
    });
  });

  it('formats short camera names and check-off offsets', () => {
    expect(shortCamLabel('Rack Cam')).toBe('Rack');
    expect(shortCamLabel('Smith Cam')).toBe('Smith');
    expect(formatCamListSummary(['Rack Cam'])).toBe('Rack');
    expect(formatCamListSummary(['Smith Wide', 'Smith Cam'])).toBe('★ Smith Wide + Smith');
    expect(
      orderSheetCameras(['Fly Machine Cam'], ['Rack Cam', 'Smith Cam', 'Fly Machine Cam', 'Smith Wide']),
    ).toEqual(['Fly Machine Cam', 'Rack Cam', 'Smith Cam', 'Smith Wide']);
    expect(
      orderSheetCameras(['Smith Wide', 'Smith Cam'], ['Rack Cam', 'Smith Cam', 'Smith Wide']),
    ).toEqual(['Smith Wide', 'Smith Cam', 'Rack Cam']);
    expect(fmtRelativeToCheck(100_000, 102_000)).toBe('right at ✓');
    expect(fmtRelativeToCheck(100_000, 145_000)).toBe('45s before ✓');
    expect(fmtRelativeToCheck(100_000, 200_000)).toBe('1m 40s before ✓');
    expect(fmtRelativeToCheck(180_000, 120_000)).toBe('1m after ✓');
  });

  it('binary-searches local tape frames for synchronous 60 fps scrubbing', () => {
    expect(findNearestFrameIndex([], 1000)).toBe(-1);
    expect(findNearestFrameIndex([{ tsMs: 5000 }], 9999)).toBe(0);

    const frames = [
      { tsMs: 100_000 },
      { tsMs: 102_000 },
      { tsMs: 104_000 },
      { tsMs: 108_000 },
    ];
    expect(findNearestFrameIndex(frames, 90_000)).toBe(0);
    expect(findNearestFrameIndex(frames, 120_000)).toBe(3);
    expect(findNearestFrameIndex(frames, 104_000)).toBe(2);
    expect(findNearestFrameIndex(frames, 102_900)).toBe(1);
    expect(findNearestFrameIndex(frames, 103_000)).toBe(1); // midpoint ties to lower index
    expect(findNearestFrameIndex(frames, 103_200)).toBe(2);
    expect(findNearestFrameIndex(frames, 106_500)).toBe(3);
  });

  it('expands Scrub Tape timeline around IN/OUT clip markers across the full rail width', () => {
    const minMs = 1_000_000;
    const maxMs = minMs + 20 * 60_000; // 20m workout tape
    const inMs = minMs + 8 * 60_000;
    const outMs = inMs + 40_000; // 40s set (~3% of 20m)

    const wIn = computeClipZoomWindow(minMs, maxMs, inMs, outMs, 'in_set');
    expect(wIn.winStart).toBe(inMs - 8_000);
    expect(wIn.winEnd - wIn.winStart).toBeGreaterThanOrEqual(75_000);
    expect(wIn.winEnd - wIn.winStart).toBeLessThanOrEqual(110_000);

    const wBoth = computeClipZoomWindow(minMs, maxMs, inMs, outMs, 'both_set');
    // 40s * 0.14 = 5.6s pad on each side -> ~51.2s total window (~78% bar fill!)
    expect(wBoth.winStart).toBe(inMs - 5600);
    expect(wBoth.winEnd).toBe(outMs + 5600);
    expect(wBoth.winEnd - wBoth.winStart).toBe(51_200);

    // Clamps safely to tape edges
    const wEdge = computeClipZoomWindow(minMs, maxMs, minMs + 1000, minMs + 25_000, 'both_set');
    expect(wEdge.winStart).toBe(minMs);
    expect(wEdge.winEnd).toBe(minMs + 25_000 + 4000);
  });

  it('computes normalized burst scrub progress and playback frame stepping', () => {
    expect(burstScrubPosition(100_000, 140_000, 100_000)).toEqual({ pct: 0, elapsedSec: 0, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 120_000)).toEqual({ pct: 50, elapsedSec: 20, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 140_000)).toEqual({ pct: 100, elapsedSec: 40, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 90_000)).toEqual({ pct: 0, elapsedSec: 0, totalSec: 40 });
    expect(burstScrubPosition(100_000, 140_000, 180_000)).toEqual({ pct: 100, elapsedSec: 40, totalSec: 40 });

    const frames = [{ tsMs: 100_000 }, { tsMs: 102_000 }, { tsMs: 104_000 }];
    expect(nextBurstPlayIndex([], 100_000)).toBe(-1);
    expect(nextBurstPlayIndex([{ tsMs: 100_000 }], 100_000)).toBe(0);
    expect(nextBurstPlayIndex(frames, 100_000)).toBe(1);
    expect(nextBurstPlayIndex(frames, 101_900)).toBe(2);
    expect(nextBurstPlayIndex(frames, 104_000)).toBe(0);
  });

  it('tracks tape retention boundary and days remaining accurately', () => {
    const day = 86_400_000;
    const t0 = 1_763_500_000_000;
    expect(isTapeWithinRetention(null, 14, t0)).toBe(false);
    expect(isTapeWithinRetention(0, 14, t0)).toBe(false);
    expect(tapeDaysRemaining(null, 14, t0)).toBe(0);

    // Fresh clip & day 13.9
    expect(isTapeWithinRetention(t0, 14, t0 + 1000)).toBe(true);
    expect(tapeDaysRemaining(t0, 14, t0 + 1000)).toBe(14);
    expect(isTapeWithinRetention(t0, 14, t0 + 13.9 * day)).toBe(true);
    expect(tapeDaysRemaining(t0, 14, t0 + 13.9 * day)).toBe(1);

    // Exact 14-day boundary and day 15 -> expired
    expect(isTapeWithinRetention(t0, 14, t0 + 14 * day)).toBe(false);
    expect(tapeDaysRemaining(t0, 14, t0 + 14 * day)).toBe(0);
    expect(isTapeWithinRetention(t0, 14, t0 + 15 * day)).toBe(false);
    expect(tapeDaysRemaining(t0, 14, t0 + 15 * day)).toBe(0);

    // Custom retention (7d vs 21d)
    expect(isTapeWithinRetention(t0, 7, t0 + 8 * day)).toBe(false);
    expect(isTapeWithinRetention(t0, 21, t0 + 18 * day)).toBe(true);
    expect(tapeDaysRemaining(t0, 21, t0 + 18 * day)).toBe(3);
  });

  it('builds deduplicated spatial-ordered coach angle options and expires un-saved tape angles cleanly', () => {
    // 2-cam + 3-cam within retention follows configuredCameras order
    expect(
      buildCoachAngleOptions({
        primaryCamera: 'Smith Cam',
        tapeCameras: ['Rack Cam', 'Smith Cam', 'Low Knee Cam'],
        configuredCameras: ['Rack Cam', 'Smith Cam', 'Low Knee Cam'],
      }),
    ).toEqual([
      { camera: 'Rack Cam', label: 'Rack', kind: 'tape' },
      { camera: 'Smith Cam', label: 'Smith', kind: 'primary' },
      { camera: 'Low Knee Cam', label: 'Low Knee', kind: 'tape' },
    ]);

    // Post-14d expiry with no saved sibling leaves only primary (caller hides switcher when length <= 1)
    expect(
      buildCoachAngleOptions({
        primaryCamera: 'Rack Cam',
        tapeCameras: [],
        configuredCameras: ['Rack Cam', 'Smith Cam'],
      }),
    ).toEqual([{ camera: 'Rack Cam', label: 'Rack', kind: 'primary' }]);

    // Post-14d expiry WITH saved sibling retains permanent sibling angle
    expect(
      buildCoachAngleOptions({
        primaryCamera: 'Rack Cam',
        savedSiblingCameras: ['Smith Cam'],
        tapeCameras: [],
        configuredCameras: ['Rack Cam', 'Smith Cam'],
      }),
    ).toEqual([
      { camera: 'Rack Cam', label: 'Rack', kind: 'primary' },
      { camera: 'Smith Cam', label: 'Smith', kind: 'saved' },
    ]);

    // Deduplication: saved sibling that also has live tape keeps kind='saved'
    expect(
      buildCoachAngleOptions({
        primaryCamera: 'Rack Cam',
        savedSiblingCameras: ['Smith Cam', 'Smith Cam'],
        tapeCameras: ['Rack Cam', 'Smith Cam', 'Low Knee Cam'],
        configuredCameras: ['Rack Cam', 'Smith Cam', 'Low Knee Cam'],
      }),
    ).toEqual([
      { camera: 'Rack Cam', label: 'Rack', kind: 'primary' },
      { camera: 'Smith Cam', label: 'Smith', kind: 'saved' },
      { camera: 'Low Knee Cam', label: 'Low Knee', kind: 'tape' },
    ]);
  });

  it('resolves Gym Cam Config vs offline Drive Preview link states and caches /api/ping preview metadata', async () => {
    const now = 1_795_200_000_000;
    expect(formatPreviewAge(null, now)).toBe('Current camera views on Google Drive');
    expect(formatPreviewAge(now - 30_000, now)).toBe('Updated just now');
    expect(formatPreviewAge(now - 18 * 60_000, now)).toBe('Updated 18m ago');
    expect(formatPreviewAge(now - 4 * 3_600_000, now)).toBe('Updated 4h ago');

    const localState = gymCamConfigLinkState({
      online: true,
      probing: false,
      enabled: true,
      baseUrl: 'http://192.168.1.152:8766/',
      previewUrl: 'https://drive.google.com/file/d/abc123/view',
      previewUpdatedMs: now - 300_000,
      nowMs: now,
    });
    expect(localState).toEqual({
      mode: 'local',
      href: 'http://192.168.1.152:8766',
      btnLabel: 'Config ↗',
      btnPrimary: true,
      showOfflineBanner: false,
      previewHref: 'https://drive.google.com/file/d/abc123/view',
      bannerSub: '',
    });

    const probingOffline = gymCamConfigLinkState({
      online: false,
      probing: true,
      enabled: true,
      baseUrl: 'http://192.168.1.152:8766',
      previewUrl: '',
      previewUpdatedMs: null,
      nowMs: now,
    });
    expect(probingOffline.mode).toBe('offline');
    expect(probingOffline.btnLabel).toBe('Preview ↗');
    expect(probingOffline.btnPrimary).toBe(false);
    expect(probingOffline.showOfflineBanner).toBe(false);
    expect(probingOffline.href).toBe(GYM_CAM_PREVIEW_DRIVE_FALLBACK);

    const awayState = gymCamConfigLinkState({
      online: false,
      probing: false,
      enabled: true,
      baseUrl: 'http://192.168.1.152:8766',
      previewUrl: 'https://drive.google.com/file/d/abc123/view',
      previewUpdatedMs: now - 12 * 60_000,
      nowMs: now,
    });
    expect(awayState).toEqual({
      mode: 'offline',
      href: 'https://drive.google.com/file/d/abc123/view',
      btnLabel: 'Preview ↗',
      btnPrimary: false,
      showOfflineBanner: true,
      previewHref: 'https://drive.google.com/file/d/abc123/view',
      bannerSub: 'Updated 12m ago · Google Drive',
    });

    const origFetch = globalThis.fetch;
    const origSettings = getSettings();
    try {
      updateSettings({
        gymCamEnabled: true,
        gymCamUrl: 'http://192.168.1.152:8766',
        gymCamPreviewUrl: '',
        gymCamPreviewUpdatedMs: null,
      });
      globalThis.fetch = vi.fn(async () =>
        new Response(
          JSON.stringify({
            service: 'armory-cam',
            serverEpochMs: now,
            recording: false,
            paused: false,
            retentionDays: 14,
            cameras: ['Rack Cam', 'Smith Cam'],
            previewDriveUrl: 'https://drive.google.com/file/d/testfid9876543210abcdef/view',
            previewUpdatedMs: now - 60_000,
          }),
          { status: 200 },
        ),
      ) as unknown as typeof fetch;

      const onRes = await gymCam.probe(500, true);
      expect(onRes.online).toBe(true);
      expect(gymCam.online).toBe(true);
      expect(getSettings().gymCamPreviewUrl).toBe('https://drive.google.com/file/d/testfid9876543210abcdef/view');
      expect(getSettings().gymCamPreviewUpdatedMs).toBe(now - 60_000);

      // Strict probe transitions online -> offline on the first failed check
      globalThis.fetch = vi.fn(async () => {
        throw new Error('LAN unreachable');
      }) as unknown as typeof fetch;
      const offRes = await gymCam.probe(500, true);
      expect(offRes.online).toBe(false);
      expect(gymCam.online).toBe(false);
    } finally {
      globalThis.fetch = origFetch;
      updateSettings({
        gymCamEnabled: origSettings.gymCamEnabled,
        gymCamUrl: origSettings.gymCamUrl,
        gymCamPreviewUrl: origSettings.gymCamPreviewUrl,
        gymCamPreviewUpdatedMs: origSettings.gymCamPreviewUpdatedMs,
      });
    }
  });

  it('defaults Gym-Cam clip saves to silent (includeAudio=false) and supports opt-in room audio + muxing', async () => {
    const origFetch = globalThis.fetch;
    const origSettings = getSettings();
    const calls: { url: string; body?: Record<string, unknown> }[] = [];
    try {
      updateSettings({ gymCamEnabled: true, gymCamUrl: 'http://192.168.1.152:8766' });
      gymCam.online = true;

      globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({ url, body });
        if (url.includes('/api/tape/angles')) {
          return new Response(
            JSON.stringify({ ok: true, retentionDays: 14, cameras: ['Rack Cam', 'Smith Cam'], audioAvailable: true }),
            { status: 200 },
          );
        }
        if (url.includes('/api/clip/audio')) {
          return new Response(
            JSON.stringify({
              ok: true,
              lanUrl: '/clips/2026-11-21/bench_01.mp4',
              fileName: 'bench_01.mp4',
              bytes: body?.includeAudio ? 450_000 : 380_000,
              hasAudio: !!body?.includeAudio,
              audioSource: body?.includeAudio ? 'powerconf' : 'none',
            }),
            { status: 200 },
          );
        }
        return new Response(
          JSON.stringify({
            ok: true,
            lanUrl: '/clips/2026-11-21/bench_01.mp4',
            fileName: 'bench_01.mp4',
            camera: 'Rack Cam',
            durationSec: 12,
            startMs: 1_000_000,
            endMs: 1_012_000,
            bytes: body?.includeAudio ? 450_000 : 380_000,
            hasAudio: !!body?.includeAudio,
          }),
          { status: 200 },
        );
      }) as unknown as typeof fetch;

      const target = {
        session: { id: 's1', name: 'Upper', routineId: null, eventId: null, startMs: 990_000, endMs: null, status: 'active' as const, exs: [], rpe: null, bwKg: null },
        se: { uid: 'se1', exId: 'bench', sets: [] },
        set: { id: 'st1', w: 100, r: 5, done: true, kind: 'n' as const, vids: [] },
      };

      const silentMeta = await gymCam.saveWindowClip(target, 'Rack Cam', 1_000_000, 1_012_000);
      expect(calls[calls.length - 1].body?.includeAudio).toBe(false);
      expect(silentMeta?.hasAudio).toBe(false);

      const soundMeta = await gymCam.saveWindowClip(target, 'Rack Cam', 1_000_000, 1_012_000, { includeAudio: true });
      expect(calls[calls.length - 1].body?.includeAudio).toBe(true);
      expect(soundMeta?.hasAudio).toBe(true);

      const mediaInfo = await gymCam.getTapeMediaInfo('s1', 1_000_000, 1_012_000);
      expect(mediaInfo).toEqual({ cameras: ['Rack Cam', 'Smith Cam'], audioAvailable: true });
      expect(gymCam.roomAudioUrl(1_000_000, 1_012_000)).toBe(
        'http://192.168.1.152:8766/api/audio/clip?startMs=1000000&endMs=1012000',
      );

      const stripped = await gymCam.setClipAudio(soundMeta!, false);
      expect(stripped?.hasAudio).toBe(false);
      expect(stripped?.bytes).toBe(380_000);
    } finally {
      gymCam.online = false;
      globalThis.fetch = origFetch;
      updateSettings({ gymCamEnabled: origSettings.gymCamEnabled, gymCamUrl: origSettings.gymCamUrl });
    }
  });

  it('orders and cycles recorded multi-view Gym-Cam clips cleanly and returns null when no other view exists', () => {
    const cRack = { id: 'v_rack', camera: 'Rack Cam', lanUrl: 'http://192.168.1.152:8766/clips/rack.mp4' };
    const cRack2 = { id: 'v_rack2', camera: 'Rack Cam', lanUrl: 'http://192.168.1.152:8766/clips/rack2.mp4' };
    const cSmith = { id: 'v_smith', camera: 'Smith Cam', lanUrl: 'http://192.168.1.152:8766/clips/smith.mp4' };
    const cWide = { id: 'v_wide', camera: 'Smith Wide', lanUrl: 'http://192.168.1.152:8766/clips/wide.mp4' };
    const cPhone = { id: 'v_phone', camera: undefined, lanUrl: undefined };

    // Single view or same-camera duplicate + non-LAN phone clip -> no other views
    expect(orderedRecordedViews([cRack, cPhone], ['Rack Cam'])).toEqual([cRack]);
    expect(nextRecordedView([cRack, cPhone], { id: 'v_rack', camera: 'Rack Cam' }, ['Rack Cam'])).toBeNull();
    expect(orderedRecordedViews([cRack, cRack2], ['Rack Cam'])).toEqual([cRack2]);
    expect(nextRecordedView([cRack, cRack2], { id: 'v_rack2', camera: 'Rack Cam' }, ['Rack Cam'])).toBeNull();

    // Respects preferredCameras order (e.g. Pull-Up: Smith Wide first, then Smith Cam)
    const pullUpViews = orderedRecordedViews([cSmith, cWide], ['Smith Wide', 'Smith Cam']);
    expect(pullUpViews.map((v) => v.id)).toEqual(['v_wide', 'v_smith']);
    expect(nextRecordedView([cSmith, cWide], { id: 'v_wide' }, ['Smith Wide', 'Smith Cam'])).toEqual({
      next: cSmith,
      index: 1,
      total: 2,
    });
    expect(nextRecordedView([cSmith, cWide], { id: 'v_smith' }, ['Smith Wide', 'Smith Cam'])).toEqual({
      next: cWide,
      index: 0,
      total: 2,
    });

    // 3-view cycle wraps around cleanly (Rack -> Smith -> Smith Wide -> Rack)
    const prefs = ['Rack Cam', 'Smith Cam', 'Smith Wide'];
    const three = [cWide, cRack, cSmith];
    expect(nextRecordedView(three, { id: 'v_rack' }, prefs)).toEqual({ next: cSmith, index: 1, total: 3 });
    expect(nextRecordedView(three, { camera: 'Smith Cam' }, prefs)).toEqual({ next: cWide, index: 2, total: 3 });
    expect(nextRecordedView(three, { id: 'v_wide' }, prefs)).toEqual({ next: cRack, index: 0, total: 3 });
  });

  it('switches live Wallboard replay views end-to-end, preserving rate and resetting 2x playback, or reports "No other views"', () => {
    closeWallboardReplay();
    lifts.videos.clear();
    lifts.sessions.clear();
    try {
      // No active replay -> "No video playing"
      expect(switchWallboardReplayView('sess_sw')).toEqual({
        ok: false,
        text: 'No video playing on TV',
        speech: 'No video playing',
      });

      const v1 = {
        id: 'vid_smith',
        sessionId: 'sess_sw',
        exId: 'smith-machine-row',
        setId: 'set_1',
        ms: 1000,
        mime: 'video/mp4',
        bytes: 100_000,
        thumb: '',
        source: 'gymcam' as const,
        camera: 'Smith Cam',
        lanUrl: 'http://192.168.1.152:8766/clips/row_smith.mp4',
        durationSec: 12,
      };
      const v2 = {
        ...v1,
        id: 'vid_wide',
        camera: 'Smith Wide',
        lanUrl: 'http://192.168.1.152:8766/clips/row_wide.mp4',
      };
      lifts.videos.set(v1.id, v1);
      lifts.sessions.set('sess_sw', {
        id: 'sess_sw',
        name: 'Upper 1',
        routineId: null,
        eventId: null,
        startMs: 1000,
        endMs: null,
        status: 'active',
        exs: [{
          uid: 'se_row',
          exId: 'smith-machine-row',
          cams: ['Smith Cam', 'Smith Wide'],
          sets: [{ id: 'set_1', w: 50, r: 10, done: true, kind: 'n', vids: [v1.id] }],
        }],
        rpe: null,
        bwKg: null,
      });

      // Single recorded view -> keeps playing and reports "No other views"
      expect(castClipToWallboard(v1, { rate: 0.5, exName: 'Smith Machine Row', setLabel: 'Set 1' })).toBe(true);
      const before = getWallboardReplay()!;
      expect(before.id).toBe(v1.id);
      expect(switchWallboardReplayView('sess_sw')).toEqual({
        ok: false,
        text: 'No other views',
        speech: 'No other views',
      });
      expect(getWallboardReplay()?.id).toBe(v1.id);

      // Add second recorded view -> switches to Smith Wide, preserves 0.5x rate, sets view 2/2 and fresh 2x playback expiry
      lifts.videos.set(v2.id, v2);
      lifts.sessions.get('sess_sw')!.exs[0].sets[0].vids = [v1.id, v2.id];
      const sw1 = switchWallboardReplayView('sess_sw');
      expect(sw1).toEqual({
        ok: true,
        text: 'TV view → Smith Wide (2/2)',
        speech: 'Switching to Smith Wide',
      });
      const after1 = getWallboardReplay()!;
      expect(after1.id).toBe(v2.id);
      expect(after1.camera).toBe('Smith Wide');
      expect(after1.subtitle).toBe('Set 1 · Smith Wide');
      expect(after1.rate).toBe(0.5);
      expect(after1.viewIndex).toBe(2);
      expect(after1.viewCount).toBe(2);
      expect(after1.expiresAtMs).toBeGreaterThan(after1.startedAtMs);

      // Switching again cycles back to Smith Cam (1/2)
      const sw2 = switchWallboardReplayView('sess_sw');
      expect(sw2).toEqual({
        ok: true,
        text: 'TV view → Smith (1/2)',
        speech: 'Switching to Smith',
      });
      expect(getWallboardReplay()?.id).toBe(v1.id);
      expect(getWallboardReplay()?.viewIndex).toBe(1);
    } finally {
      closeWallboardReplay();
      lifts.videos.clear();
      lifts.sessions.clear();
    }
  });

  it('uses strict trigger-to-trigger endMs without +2s padding and sends stable requestId for armed & guest manual recordings', async () => {
    const origFetch = globalThis.fetch;
    const origSettings = getSettings();
    const calls: { url: string; body?: Record<string, unknown> }[] = [];
    const dateSpy = vi.spyOn(Date, 'now');
    try {
      updateSettings({ gymCamEnabled: true, gymCamUrl: 'http://192.168.1.152:8766' });
      gymCam.online = true;
      gymCam.clockOffsetMs = 0;

      globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({ url, body });
        return new Response(
          JSON.stringify({
            ok: true,
            lanUrl: '/clips/2026-11-26/bench_manual.mp4',
            fileName: 'bench_manual.mp4',
            camera: 'Rack Cam',
            durationSec: 20,
            startMs: body?.startMs,
            endMs: body?.endMs,
            bytes: 400_000,
          }),
          { status: 200 },
        );
      }) as unknown as typeof fetch;

      const sess = {
        id: 's_manual',
        name: 'Upper 1',
        routineId: null,
        eventId: null,
        startMs: 1_000_000,
        endMs: null,
        status: 'active' as const,
        exs: [],
        rpe: null,
        bwKg: null,
      };
      const se = { uid: 'se_bp', exId: 'barbell-bench-press', sets: [] };
      const set = { id: 'st_bp1', w: 100, r: 5, done: false, kind: 'n' as const, vids: [] };

      dateSpy.mockReturnValue(1_020_000);
      gymCam.armSet(sess, se, set, ['Rack Cam'], 1_000_000);
      const armedRes = await gymCam.stopAndSaveArmed(sess, se, set);
      expect(armedRes).toHaveLength(1);
      const armedBody = calls[0].body!;
      expect(armedBody.startMs).toBe(1_000_000);
      expect(armedBody.endMs).toBe(1_020_000);
      expect(armedBody.requestId).toBe('set_st_bp1_1000000_1020000');

      dateSpy.mockReturnValue(1_050_000);
      gymCam.armGuest(sess, 'Smith Cam');
      dateSpy.mockReturnValue(1_068_500);
      const guestRes = await gymCam.stopAndSaveGuest(sess);
      expect(guestRes).not.toBeNull();
      const guestBody = calls[1].body!;
      expect(guestBody.startMs).toBe(1_050_000);
      expect(guestBody.endMs).toBe(1_068_500);
      expect(guestBody.requestId).toBe('guest_1050000_1068500_Smith Cam');
    } finally {
      dateSpy.mockRestore();
      gymCam.cancelArm();
      gymCam.cancelGuest();
      gymCam.online = false;
      globalThis.fetch = origFetch;
      updateSettings({ gymCamEnabled: origSettings.gymCamEnabled, gymCamUrl: origSettings.gymCamUrl });
      lifts.videos.clear();
      lifts.sessions.clear();
    }
  });
});
