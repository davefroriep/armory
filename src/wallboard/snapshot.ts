import { recoveryLimitBpm } from '../domain/gate';
import { PHASE1_PLAN, type LocalTodayItem } from '../domain/plan';
import { sessionStats } from '../lifts/calc';
import { peekNextTargetSet, resolveTargetSet, type CursorRef, type SetPointer } from '../lifts/cursor';
import { canLog, labels, prevSetFor, supersetGroups } from '../lifts/logic';
import { MUSCLE_LABEL } from '../lifts/muscles';
import { resolvePrescription, type CoachPrescription } from '../lifts/prescriptions';
import { matchRoutineByTitle } from '../lifts/seed';
import { fmtKg } from '../lifts/settings';
import { condenseSets, fmtVolume, setText } from '../lifts/summary';
import type { Exercise, LiftSession, LiftSet, Routine, SessionEx } from '../lifts/types';
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
  subText: string;
  isPrescribedLoad: boolean;
}

export interface WallboardSetSlot {
  label: string;
  state: 'done' | 'active' | 'queued';
  kind: 'n' | 'w' | 'd' | 'f';
  exType: 'weight' | 'bodyweight' | 'time';
  weightVal: string;
  weightPlaceholder: string;
  repsVal: string;
  repsPlaceholder: string;
  isGhost: boolean;
  rpeText: string;
  hasPrev: boolean;
  prevText: string;
}

export interface WallboardVoiceHint {
  micState: 'armed' | 'ready' | 'standby' | 'off';
  headline: string;
  chips: { tag: string; say: string; primary?: boolean }[];
}

export interface WallboardStationCard {
  label: string;
  exName: string;
  exType: 'weight' | 'bodyweight' | 'time';
  isActive: boolean;
  repTarget: string;
  rpeTarget: string;
  lastBenchmark: string;
  sets: WallboardSetSlot[];
  voiceHint: WallboardVoiceHint | null;
}

export interface WallboardOnDeckBlock {
  label: string;
  isSuperset: boolean;
  items: {
    label: string;
    exName: string;
    sets: number;
    repTarget: string;
    rpeTarget: string;
    lastText: string;
  }[];
}

export interface WallboardLiftStage {
  kind: 'single' | 'superset';
  kicker: string;
  stations: WallboardStationCard[];
  onDeck: WallboardOnDeckBlock | null;
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
    stage: WallboardLiftStage | null;
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
  noteOf?: (exId: string) => string;
  e1rmOf?: (exId: string) => number | null;
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

function formatPrevBenchmark(prevSe: SessionEx | null, se: SessionEx, ex: Exercise, refIdx: number, u: 'lb' | 'kg'): string {
  if (!prevSe) return '';
  const ref = prevSetFor(prevSe, se, refIdx)
    ?? [...prevSe.sets].reverse().find((x) => x.done && x.kind !== 'w')
    ?? [...prevSe.sets].reverse().find((x) => x.done);
  if (!ref) return '';
  const txt = setText(ref, ex, u);
  return ex.type === 'weight' ? `${txt} ${u}` : txt;
}

function formatSetSlot(
  x: LiftSet,
  label: string,
  state: WallboardSetSlot['state'],
  ex: Exercise,
  rx: CoachPrescription,
  prev: LiftSet | null,
  u: 'lb' | 'kg',
): WallboardSetSlot {
  let weightVal = '';
  let weightPlaceholder = `___ ${u}`;
  let repsVal = '';
  let repsPlaceholder = '___ reps';

  if (ex.type === 'time') {
    weightVal = x.w && x.w > 0 ? `${fmtKg(x.w)} ${u}` : (x.done ? 'HOLD' : '');
    weightPlaceholder = 'HOLD';
    repsVal = x.r != null ? `${x.r}s` : '';
    repsPlaceholder = `${rx.sampleRep}s`;
  } else if (ex.type === 'bodyweight') {
    weightVal = x.w && x.w > 0 ? `+${fmtKg(x.w)} ${u}` : 'BW';
    weightPlaceholder = 'BW';
    repsVal = x.r != null ? `${x.r} reps` : '';
  } else {
    weightVal = x.w != null ? `${fmtKg(x.w)} ${u}` : '';
    repsVal = x.r != null ? `${x.r} reps` : '';
  }

  return {
    label,
    state,
    kind: x.kind,
    exType: ex.type,
    weightVal,
    weightPlaceholder,
    repsVal,
    repsPlaceholder,
    isGhost: !x.done && !!(x.wAuto || x.rAuto),
    rpeText: x.rpe != null ? `@${x.rpe}` : '',
    hasPrev: !!prev,
    prevText: prev ? setText(prev, ex, u) : '',
  };
}

function buildVoiceHint(
  ex: Exercise,
  activeSet: LiftSet,
  curPtr: SetPointer,
  rx: CoachPrescription,
  st: Pick<Settings, 'voiceEnabled' | 'voiceWakeWord'>,
  voice: { listening: boolean; armed: boolean },
): WallboardVoiceHint {
  const wakeRaw = (st.voiceWakeWord || 'armory').trim() || 'armory';
  const wakeDisp = wakeRaw.charAt(0).toUpperCase() + wakeRaw.slice(1);
  const prefix = voice.armed ? '' : `${wakeDisp}, `;
  const micState: WallboardVoiceHint['micState'] = voice.armed
    ? 'armed'
    : voice.listening
      ? 'ready'
      : st.voiceEnabled
        ? 'standby'
        : 'off';

  const readyToConfirm = canLog(activeSet, ex);
  const headline = micState === 'armed'
    ? 'LISTENING…'
    : micState === 'off'
      ? 'MIC OFF'
      : '';

  if (ex.type === 'time') {
    return {
      micState,
      headline,
      chips: [
        { tag: '', say: `"${prefix}start hold"`, primary: true },
        { tag: '', say: readyToConfirm ? `"${prefix}Done!"` : `"${prefix}${rx.sampleRep} seconds"` },
      ],
    };
  }
  if (ex.type === 'bodyweight') {
    if (readyToConfirm) {
      const bumpR = (activeSet.r ?? rx.sampleRep) + 1;
      return {
        micState,
        headline,
        chips: [
          { tag: '', say: `"${prefix}Done!"`, primary: true },
          { tag: '', say: `"${prefix}for ${bumpR}"` },
          { tag: '', say: `"${prefix}25 for 6"` },
        ],
      };
    }
    return {
      micState,
      headline,
      chips: [
        { tag: '', say: `"${prefix}bodyweight for ${rx.sampleRep}"`, primary: true },
      ],
    };
  }

  const exW = activeSet.w != null
    ? fmtKg(activeSet.w)
    : ex.equip === 'db'
      ? '50'
      : (ex.equip === 'bb' || ex.equip === 'smith')
        ? '135'
        : '70';
  if (readyToConfirm) {
    const curWNum = Number(fmtKg(activeSet.w!));
    const bumpW = Number.isFinite(curWNum) ? String(Math.round((curWNum + 5) * 10) / 10) : fmtKg(activeSet.w!);
    const curR = activeSet.r ?? rx.sampleRep;
    const bumpR = curR + 1;
    return {
      micState,
      headline,
      chips: [
        { tag: '', say: `"${prefix}Done!"`, primary: true },
        { tag: '', say: `"${prefix}for ${bumpR}"` },
        { tag: '', say: `"${prefix}${bumpW} for ${curR}"` },
      ],
    };
  }
  return {
    micState,
    headline,
    chips: [
      { tag: '', say: `"${prefix}${exW} for ${rx.sampleRep}"`, primary: true },
    ],
  };
}

function buildLiftStage(
  s: LiftSession,
  labs: string[],
  groups: ReturnType<typeof supersetGroups>,
  curPtr: SetPointer | null,
  inp: WallboardBuildInput,
  planWeek: number,
  u: 'lb' | 'kg',
): WallboardLiftStage | null {
  if (!curPtr) return null;
  const curGroupIdx = groups.findIndex((g) => curPtr.exIdx >= g.start && curPtr.exIdx <= g.end);
  if (curGroupIdx < 0) return null;
  const curGroup = groups[curGroupIdx];
  const isSupersetStage = curGroup.ssIdx !== null && curGroup.end > curGroup.start;

  const stations: WallboardStationCard[] = [];
  for (let k = curGroup.start; k <= curGroup.end; k++) {
    const se = s.exs[k];
    const ex = inp.exOf(se.exId);
    const isActive = k === curPtr.exIdx;
    const rx = resolvePrescription(ex, {
      rx: se.rx,
      sets: se.sets.length,
      week: planWeek,
      base1RmKg: inp.e1rmOf?.(se.exId) ?? null,
      unit: u,
    });
    const prevSe = inp.prevOf(se);
    const firstOpenIdx = se.sets.findIndex((x) => !x.done);
    const refIdx = isActive ? curPtr.setIdx : Math.max(0, firstOpenIdx);
    const lastBenchmark = formatPrevBenchmark(prevSe, se, ex, refIdx, u);

    let wc = 0;
    const sets: WallboardSetSlot[] = se.sets.map((x, idx) => {
      const setLab = x.kind === 'w' ? 'W' : String(++wc);
      const state: WallboardSetSlot['state'] = x.done
        ? 'done'
        : isActive && idx === curPtr.setIdx
          ? 'active'
          : 'queued';
      const prev = prevSetFor(prevSe, se, idx);
      return formatSetSlot(x, setLab, state, ex, rx, prev, u);
    });

    stations.push({
      label: labs[k] ?? String(k + 1),
      exName: ex.name,
      exType: ex.type,
      isActive,
      repTarget: rx.repTarget,
      rpeTarget: rx.rpeTarget,
      lastBenchmark,
      sets,
      voiceHint: isActive ? buildVoiceHint(ex, se.sets[curPtr.setIdx], curPtr, rx, inp.settings, inp.voice) : null,
    });
  }

  let onDeck: WallboardOnDeckBlock | null = null;
  for (let off = 1; off < groups.length; off++) {
    const cand = groups[(curGroupIdx + off) % groups.length];
    let hasOpen = false;
    for (let k = cand.start; k <= cand.end; k++) {
      if (s.exs[k].sets.some((x) => !x.done)) {
        hasOpen = true;
        break;
      }
    }
    if (!hasOpen) continue;
    const items: WallboardOnDeckBlock['items'] = [];
    for (let k = cand.start; k <= cand.end; k++) {
      const se = s.exs[k];
      const ex = inp.exOf(se.exId);
      const rx = resolvePrescription(ex, {
        rx: se.rx,
        sets: se.sets.length,
        week: planWeek,
        base1RmKg: inp.e1rmOf?.(se.exId) ?? null,
        unit: u,
      });
      const prevSe = inp.prevOf(se);
      const lastText = formatPrevBenchmark(prevSe, se, ex, 0, u);
      items.push({
        label: labs[k] ?? String(k + 1),
        exName: ex.name,
        sets: se.sets.filter((x) => !x.done).length,
        repTarget: rx.repTarget,
        rpeTarget: rx.rpeTarget,
        lastText,
      });
    }
    onDeck = {
      label: labs.slice(cand.start, cand.end + 1).join(' + '),
      isSuperset: cand.ssIdx !== null && cand.end > cand.start,
      items,
    };
    break;
  }

  const kicker = isSupersetStage
    ? `SUPERSET · ROUND ${curPtr.setNumber} OF ${curPtr.totalWorkingSets} · ALTERNATE ${stations.map((x) => x.label).join(' ↔ ')}`
    : `STRAIGHT SETS · EXERCISE ${curPtr.label} · SET ${curPtr.setNumber} OF ${curPtr.totalWorkingSets}`;

  return { kind: isSupersetStage ? 'superset' : 'single', kicker, stations, onDeck };
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
    const planWeek = PHASE1_PLAN.steps[st.queue.stepIndex]?.week ?? 2;
    const stage = buildLiftStage(s, labs, groups, curPtr, inp, planWeek, u);
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
        note: '',
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
      const ex = inp.exOf(se.exId);
      const rx = resolvePrescription(ex, {
        rx: se.rx,
        sets: se.sets.length,
        week: planWeek,
        base1RmKg: inp.e1rmOf?.(se.exId) ?? null,
        unit: u,
      });
      const isDone = doneCount === se.sets.length && se.sets.length > 0;
      const isPrescribedLoad = !!(rx.prescribedSets && rx.prescribedSets.some((ps) => ps.wKg != null));
      let subText = '';
      if (isDone) {
        subText = condenseSets(se, ex, u) || rx.roadmapLine;
      } else if (isPrescribedLoad) {
        subText = rx.roadmapLine;
      } else {
        const hasFullNums = (x: LiftSet): boolean =>
          ex.type === 'weight' ? (x.w != null && x.r != null && x.r > 0) : (x.r != null && x.r > 0);
        const refSet = se.sets.find((x) => x.kind !== 'w' && x.done && hasFullNums(x))
          ?? se.sets.find((x) => x.kind !== 'w' && hasFullNums(x));
        let bench = '';
        if (refSet) {
          const txt = setText(refSet, ex, u);
          bench = ex.type === 'weight' ? `${txt} ${u}` : txt;
        } else {
          bench = formatPrevBenchmark(inp.prevOf(se), se, ex, 0, u);
        }
        const dupTimeBench = ex.type === 'time' && (!refSet?.w || refSet.w <= 0) && !!bench && rx.roadmapLine.includes(bench);
        subText = bench && !dupTimeBench ? `${rx.roadmapLine} · ${bench}` : rx.roadmapLine;
      }
      return {
        label: labs[i] ?? String(i + 1),
        name: ex.name,
        doneSets: doneCount,
        totalSets: se.sets.length,
        isCurrent: curPtr?.exIdx === i,
        isDone,
        ssGroup: ssMap.get(i) ?? null,
        subText,
        isPrescribedLoad,
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
        stage,
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

/** True whenever an active (non-paused) strength or cardio workout is underway. */
export function isWallboardWorkoutActive(st: WallboardState): boolean {
  if (st.mode === 'cardio' && !!st.cardio && !st.cardio.paused) return true;
  if (st.mode === 'lift' && !!st.lift?.started && !st.lift.paused) return true;
  return false;
}
