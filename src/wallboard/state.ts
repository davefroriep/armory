import { Emitter } from '../emitter';
import { hrFresh, live, recentHrPoints } from '../ble/telemetry';
import { active } from '../lifts/active';
import { gymCam } from '../lifts/gymCam';
import { liftSettings } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { shortCamLabel } from '../lifts/tape';
import { voice } from '../lifts/voice';
import { calendar } from '../intervals/calendar';
import { allMeta } from '../store/db';
import { getSettings } from '../store/settings';
import type { WorkoutMeta } from '../types';
import {
  buildWallboardSnapshot, type ActiveHoldSnapshot, type WallboardCameraState, type WallboardCardioSnapshot,
  type WallboardHrState, type WallboardLiftExerciseSummary, type WallboardReplayState,
  type WallboardStandbyCompletedItem, type WallboardState,
} from './snapshot';

export type {
  ActiveHoldSnapshot, WallboardCameraState, WallboardCardioSnapshot, WallboardHrState,
  WallboardLiftExerciseSummary, WallboardReplayState, WallboardStandbyCompletedItem, WallboardState,
};

let cardioSnap: WallboardCardioSnapshot | null = null;
let holdSnap: ActiveHoldSnapshot | null = null;
let replaySnap: WallboardReplayState | null = null;
let cachedCardioMetas: WorkoutMeta[] = [];
let cachedCardioSig = '';
export const wallboardSnapChanged = new Emitter<void>();

/** Refreshes cached saved cardio metas from IndexedDB so Standby reflects completed cardio today synchronously. */
export async function refreshWallboardCardioCache(): Promise<void> {
  try {
    const metas = await allMeta();
    const saved = metas.filter((m) => m.status === 'saved' && m.equipment !== 'strength');
    const sig = saved.map((m) => `${m.id}:${m.startEpochMs}:${m.seconds}`).join('|');
    cachedCardioMetas = saved;
    if (sig !== cachedCardioSig) {
      cachedCardioSig = sig;
      wallboardSnapChanged.emit();
    }
  } catch {
    /* IndexedDB unavailable in Node test environments */
  }
}

export function setWallboardCardioState(snap: WallboardCardioSnapshot | null): void {
  cardioSnap = snap;
  wallboardSnapChanged.emit();
}

export function setWallboardHoldState(snap: ActiveHoldSnapshot | null): void {
  holdSnap = snap;
  wallboardSnapChanged.emit();
}

export function getWallboardReplay(): WallboardReplayState | null {
  return replaySnap;
}

export function setWallboardReplay(snap: WallboardReplayState | null): void {
  replaySnap = snap;
  wallboardSnapChanged.emit();
}

/** Reads live store/BLE singletons and builds the current TV wallboard snapshot. */
export function buildWallboardState(now = Date.now()): WallboardState {
  const s = lifts.active;
  const camArmed = gymCam.armed;
  const camGuest = gymCam.guestArmed;
  const camSaving = !!gymCam.savingSetId || gymCam.savingGuest;
  const camera: WallboardCameraState | null = (camArmed || camGuest || camSaving)
    ? {
        recording: !!(camArmed || camGuest),
        saving: camSaving,
        guest: !!(camGuest || gymCam.savingGuest),
        cameras: camGuest
          ? (camGuest.camera ? [shortCamLabel(camGuest.camera)] : [])
          : camArmed
            ? (camArmed.cameras.length ? camArmed.cameras : [camArmed.camera]).map(shortCamLabel).filter(Boolean)
            : [],
        ...(camGuest?.startEpochMs || camArmed?.startEpochMs
          ? { startedAtMs: camGuest?.startEpochMs ?? camArmed?.startEpochMs }
          : {}),
      }
    : null;

  return buildWallboardSnapshot({
    now,
    settings: getSettings(),
    unit: liftSettings().unit,
    hr: { bpm: live.hr, fresh: hrFresh(now), recentPoints: recentHrPoints(60_000, now) },
    voice: {
      listening: voice.isAnyListening(),
      armed: voice.isArmed(now),
      banner: voice.lastAction,
      cursor: voice.cursor,
    },
    camera,
    cardioSnap,
    holdSnap,
    replaySnap,
    activeSession: s,
    elapsedSec: s ? active.elapsedSec() : 0,
    rest: active.rest,
    todayItems: calendar.items,
    finishedTodayLifts: lifts.finished(),
    finishedTodayCardio: cachedCardioMetas,
    routines: [...lifts.routines.values()],
    exOf: lifts.exOrStub,
    e1rmOf: (exId) => lifts.bestE1rm(exId),
    prevOf: (se) => (s ? lifts.previous(se.exId, s.startMs, s.id) : null),
  });
}
