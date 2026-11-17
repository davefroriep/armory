import { recoveryLimitBpm } from '../domain/gate';
import { PHASE1_PLAN, type LocalTodayItem } from '../domain/plan';
import { sessionStats } from '../lifts/calc';
import { peekNextTargetSet, resolveTargetSet, type CursorRef } from '../lifts/cursor';
import { labels, prevSetFor, supersetGroups } from '../lifts/logic';
import { MUSCLE_LABEL } from '../lifts/muscles';
import { matchRoutineByTitle } from '../lifts/seed';
import { fmtKg } from '../lifts/settings';
import { fmtVolume, setText } from '../lifts/summary';
import type { Exercise, LiftSession, Routine, SessionEx } from '../lifts/types';
import { EQUIPMENT_LABEL } from '../types';
import { hrDropStats, hrZone } from '../ui/hr';
import type { Settings } from '../store/settings';

export interface WallboardCardioSnapshot {
  title: string;
  phase: string;
  phaseTone: 'warm' | 'work' | 'float' | 'rest' | 'cool' | 'done';
  repText: string;
  countdownText: string;
  targetText: string;
  watts: number;
  targetWatts: number | null;
  rpm: number;
  targetRpm: number | null;
  elapsedSec: number;
  kcal: number;
  z5Sec: number;
  paused: boolean;
}

export interface ActiveHoldSnapshot {
  exUid: string;
  setId: string;
  label: string;
  endMs: number;
  totalSec: number;
}

export interface WallboardVoiceBanner {
  text: string;
  kind: 'ok' | 'info' | 'warn';
  atMs: number;
}

export interface WallboardHrState {
  bpm: number;
  fresh: boolean;
  zone: number;
  pctMax: number;
  readyLimitBpm: number;
  drop60: number;
  readiness: 'resting' | 'recovering' | 'ready' | 'idle';
}

export interface WallboardLiftExerciseSummary {
  label: string;
  name: string;
  doneSets: number;
  totalSets: number;
  isCurrent: boolean;
  isDone: boolean;
  ssGroup: number | null;
}

export interface WallboardState {
  ts: number;
  mode: 'lift' | 'cardio' | 'standby';
  hr: WallboardHrState;
  voice: {
    enabled: boolean;
    listening: boolean;
    armed: boolean;
    wakeWord: string;
    banner: WallboardVoiceBanner | null;
  };
  lift?: {
    title: string;
    elapsedSec: number;
    paused: boolean;
    started: boolean;
    setsDone: number;
    setsTotal: number;
    volumeText: string;
    unit: 'lb' | 'kg';
    rest: { active: boolean; remainingSec: number; totalSec: number; label: string; over: boolean };
    hold: { active: boolean; remainingSec: number; totalSec: number; label: string } | null;
    current: {
      label: string;
      isSuperset: boolean;
      exName: string;
      muscles: string;
      note: string;
      exType: 'weight' | 'bodyweight' | 'time';
      setNumber: number;
      totalSets: number;
      weightText: string;
      repsText: string;
      isAuto: boolean;
      prevText: string;
      sets: { label: string; text: string; done: boolean; isCurrent: boolean }[];
    } | null;
    next: {
      label: string;
      exName: string;
      setNumber: number;
      totalSets: number;
      targetText: string;
    } | null;
    exercises: WallboardLiftExerciseSummary[];
  };
  cardio?: WallboardCardioSnapshot;
  standby?: {
    dateLabel: string;
    timeLabel: string;
    weekLabel: string;
    todayItems: { title: string; equipmentLabel: string; carried: boolean; preview: string }[];
  };
}

export interface WallboardBuildInput {
  now: number;
  settings: Pick<Settings, 'hrMax' | 'recoveryHrPct' | 'voiceEnabled' | 'voiceWakeWord' | 'queue'>;
  unit: 'lb' | 'kg';
  hr: { bpm: number; fresh: boolean; recentPoints: { at: number; hr: number }[] };
  voice: { listening: boolean; armed: boolean; banner: WallboardVoiceBanner | null; cursor: CursorRef | null };
  cardioSnap: WallboardCardioSnapshot | null;
  holdSnap: ActiveHoldSnapshot | null;
  activeSession: LiftSession | undefined;
  elapsedSec: number;
  rest: { endMs: number; totalSec: number; label: string } | null;
  todayItems: LocalTodayItem[];
  routines: Routine[];
  exOf: (exId: string) => Exercise;
  noteOf: (exId: string) => string;
  prevOf: (se: SessionEx) => SessionEx | null;
}

function formatWeightReps(wKg: number | null, r: number | null, type: Exercise['type'], u: 'lb' | 'kg'): { weightText: string; repsText: string } {
  if (type === 'time') {
    const wt = wKg && wKg > 0 ? `${fmtKg(wKg)} ${u}` : 'HOLD';
    return { weightText: wt, repsText: `${r ?? 45}s` };
  }
  if (type === 'bodyweight') {
    const wt = wKg && wKg > 0 ? `BW + ${fmtKg(wKg)} ${u}` : 'BW';
    return { weightText: wt, repsText: r != null ? `${r} reps` : '—' };
  }
  return {
    weightText: wKg != null ? `${fmtKg(wKg)} ${u}` : '—',
    repsText: r != null ? `${r} reps` : '—',
  };
}

/** Pure, Node-testable snapshot builder transforming local gym telemetry and workout state into TV wallboard JSON. */
export function buildWallboardSnapshot(inp: WallboardBuildInput): WallboardState {
  const { now, settings: st, unit: u } = inp;
  const fresh = inp.hr.fresh;
  const bpm = fresh ? inp.hr.bpm : 0;
  const z = bpm > 0 ? hrZone(bpm, st.hrMax) : { z: 0, pct: 0 };
  const readyLimitBpm = recoveryLimitBpm(st.hrMax, st.recoveryHrPct);
  const drop60 = bpm > 0 ? (hrDropStats(inp.hr.recentPoints, bpm, now)?.drop ?? 0) : 0;
  const restObj = inp.rest;
  const restRemSec = restObj ? Math.ceil((restObj.endMs - now) / 1000) : 0;

  let readiness: WallboardHrState['readiness'] = 'idle';
  if (restObj && restRemSec > 0) readiness = 'resting';
  else if (fresh && bpm > 0) readiness = bpm <= readyLimitBpm ? 'ready' : 'recovering';

  const voiceState = {
    enabled: st.voiceEnabled,
    listening: inp.voice.listening,
    armed: inp.voice.armed,
    wakeWord: st.voiceWakeWord || 'armory',
    banner: inp.voice.banner && now - inp.voice.banner.atMs < 6000 ? inp.voice.banner : null,
  };

  const hr: WallboardHrState = {
    bpm,
    fresh,
    zone: z.z,
    pctMax: bpm > 0 ? Math.round(z.pct * 100) : 0,
    readyLimitBpm,
    drop60,
    readiness,
  };

  if (inp.cardioSnap) {
    return { ts: now, mode: 'cardio', hr, voice: voiceState, cardio: { ...inp.cardioSnap } };
  }

  const s = inp.activeSession;
  if (s) {
    const labs = labels(s.exs);
    const groups = supersetGroups(s.exs);
    const ssMap = new Map<number, number | null>();
    for (const g of groups) {
      for (let k = g.start; k <= g.end; k++) ssMap.set(k, g.ssIdx);
    }
    const curPtr = resolveTargetSet(s.exs, inp.voice.cursor);
    const nxtPtr = peekNextTargetSet(s.exs, curPtr);
    const stats = sessionStats(s, inp.exOf);
    const setsTotal = s.exs.reduce((n, se) => n + se.sets.length, 0);
    const setsDone = s.exs.reduce((n, se) => n + se.sets.filter((x) => x.done).length, 0);

    let current: NonNullable<WallboardState['lift']>['current'] = null;
    if (curPtr) {
      const se = s.exs[curPtr.exIdx];
      const ex = inp.exOf(se.exId);
      const set = se.sets[curPtr.setIdx];
      const prev = prevSetFor(inp.prevOf(se), se, curPtr.setIdx);
      const wr = formatWeightReps(set.w, set.r, ex.type, u);
      let wc = 0;
      const sets = se.sets.map((x, idx) => {
        const lbl = x.kind === 'w' ? 'W' : String(++wc);
        return { label: lbl, text: setText(x, ex, u), done: x.done, isCurrent: idx === curPtr.setIdx };
      });
      current = {
        label: curPtr.label,
        isSuperset: ssMap.get(curPtr.exIdx) != null,
        exName: ex.name,
        muscles: ex.primary.map((m) => MUSCLE_LABEL[m]).join(' · '),
        note: inp.noteOf(se.exId),
        exType: ex.type,
        setNumber: curPtr.setNumber,
        totalSets: curPtr.totalWorkingSets || se.sets.length,
        weightText: wr.weightText,
        repsText: wr.repsText,
        isAuto: !!(set.wAuto || set.rAuto),
        prevText: prev ? setText(prev, ex, u) : '',
        sets,
      };
    }

    let next: NonNullable<WallboardState['lift']>['next'] = null;
    if (nxtPtr) {
      const se = s.exs[nxtPtr.exIdx];
      const ex = inp.exOf(se.exId);
      const set = se.sets[nxtPtr.setIdx];
      const wr = formatWeightReps(set.w, set.r, ex.type, u);
      next = {
        label: nxtPtr.label,
        exName: ex.name,
        setNumber: nxtPtr.setNumber,
        totalSets: nxtPtr.totalWorkingSets || se.sets.length,
        targetText: ex.type === 'time' ? wr.repsText : `${wr.weightText} × ${wr.repsText}`,
      };
    }

    const exercises: WallboardLiftExerciseSummary[] = s.exs.map((se, i) => {
      const doneCount = se.sets.filter((x) => x.done).length;
      return {
        label: labs[i] ?? String(i + 1),
        name: inp.exOf(se.exId).name,
        doneSets: doneCount,
        totalSets: se.sets.length,
        isCurrent: curPtr?.exIdx === i,
        isDone: doneCount === se.sets.length && se.sets.length > 0,
        ssGroup: ssMap.get(i) ?? null,
      };
    });

    const activeHold = inp.holdSnap && inp.holdSnap.endMs > now
      ? {
          active: true,
          remainingSec: Math.max(1, Math.ceil((inp.holdSnap.endMs - now) / 1000)),
          totalSec: inp.holdSnap.totalSec,
          label: inp.holdSnap.label,
        }
      : null;

    return {
      ts: now,
      mode: 'lift',
      hr,
      voice: voiceState,
      lift: {
        title: s.name,
        elapsedSec: inp.elapsedSec,
        paused: s.pausedAt != null,
        started: !!s.started || setsDone > 0,
        setsDone,
        setsTotal,
        volumeText: stats.volumeKg > 0 ? fmtVolume(stats.volumeKg, u) : '0',
        unit: u,
        rest: {
          active: !!restObj,
          remainingSec: restRemSec,
          totalSec: restObj?.totalSec ?? 90,
          label: restObj?.label ?? '',
          over: restObj ? restObj.endMs <= now : false,
        },
        hold: activeHold,
        current,
        next,
        exercises,
      },
    };
  }

  const d = new Date(now);
  const step = PHASE1_PLAN.steps[st.queue.stepIndex];
  const weekLabel = step ? `Wk ${step.week} · Step ${step.index + 1}/${PHASE1_PLAN.steps.length}` : 'Phase 1 Complete';
  const todayItems = inp.todayItems.map((it) => {
    let preview = it.slot.description || '';
    if (it.equipment === 'strength') {
      const itemNames = it.items?.length
        ? it.items.map((x) => `${inp.exOf(x.exId).name} (${x.sets})`)
        : (matchRoutineByTitle(it.slot.name, inp.routines)?.items ?? []).map((x) => `${inp.exOf(x.exId).name} (${x.sets})`);
      if (itemNames.length) preview = itemNames.slice(0, 4).join(' · ');
    }
    return {
      title: it.slot.name,
      equipmentLabel: EQUIPMENT_LABEL[it.equipment] ?? it.equipment,
      carried: !!it.carried,
      preview,
    };
  });

  return {
    ts: now,
    mode: 'standby',
    hr,
    voice: voiceState,
    standby: {
      dateLabel: d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' }),
      timeLabel: d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
      weekLabel,
      todayItems,
    },
  };
}
