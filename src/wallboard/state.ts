import { Emitter } from '../emitter';
import { hrFresh, live, recentHrPoints } from '../ble/telemetry';
import { active } from '../lifts/active';
import { liftSettings } from '../lifts/settings';
import { lifts } from '../lifts/store';
import { voice } from '../lifts/voice';
import { calendar } from '../intervals/calendar';
import { getSettings } from '../store/settings';
import {
  buildWallboardSnapshot, type ActiveHoldSnapshot, type WallboardCardioSnapshot,
  type WallboardHrState, type WallboardLiftExerciseSummary, type WallboardState,
} from './snapshot';

export type {
  ActiveHoldSnapshot, WallboardCardioSnapshot, WallboardHrState,
  WallboardLiftExerciseSummary, WallboardState,
};

let cardioSnap: WallboardCardioSnapshot | null = null;
let holdSnap: ActiveHoldSnapshot | null = null;
export const wallboardSnapChanged = new Emitter<void>();

export function setWallboardCardioState(snap: WallboardCardioSnapshot | null): void {
  cardioSnap = snap;
  wallboardSnapChanged.emit();
}

export function setWallboardHoldState(snap: ActiveHoldSnapshot | null): void {
  holdSnap = snap;
  wallboardSnapChanged.emit();
}

/** Reads live store/BLE singletons and builds the current TV wallboard snapshot. */
export function buildWallboardState(now = Date.now()): WallboardState {
  const s = lifts.active;
  return buildWallboardSnapshot({
    now,
    settings: getSettings(),
    unit: liftSettings().unit,
    hr: { bpm: live.hr, fresh: hrFresh(now), recentPoints: recentHrPoints(60_000, now) },
    voice: {
      listening: voice.listening,
      armed: voice.isArmed(now),
      banner: voice.lastAction,
      cursor: voice.cursor,
    },
    cardioSnap,
    holdSnap,
    activeSession: s,
    elapsedSec: s ? active.elapsedSec() : 0,
    rest: active.rest,
    todayItems: calendar.items,
    routines: [...lifts.routines.values()],
    exOf: lifts.exOrStub,
    e1rmOf: (exId) => lifts.bestE1rm(exId),
    prevOf: (se) => (s ? lifts.previous(se.exId, s.startMs, s.id) : null),
  });
}
