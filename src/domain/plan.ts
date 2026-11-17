import type { Equipment } from '../types';
import type { IntervalsEvent } from '../intervals/parse';

export interface PlanSlot {
  equipment: Equipment;
  /** Optional AirBike program id when applicable. */
  programId?: string;
  intervalsType: 'WeightTraining' | 'VirtualRide' | 'VirtualRun';
  name: string;
  description: string;
  seconds: number;
}

export interface PlanStep {
  index: number;
  week: number;
  day: number;
  title: string;
  slots: PlanSlot[];
}

export interface RollingPlan {
  id: string;
  title: string;
  steps: PlanStep[];
}

export interface QueueState {
  planId: string;
  /** 0-based index into `plan.steps` (`plan.steps.length` when finished). */
  stepIndex: number;
  /** Slot indices already completed within `stepIndex`. */
  completedSlots: number[];
  /** YYYY-MM-DD the active `stepIndex` is currently staged for (can be tomorrow if today finished). */
  stagedDate: string | null;
  /** Optional per-slot YYYY-MM-DD override when individual slots are pushed to tomorrow. */
  slotDates?: Record<number, string>;
  /** Future slots completed out of order: key `"${stepIndex}:${slotIndex}"` -> `"YYYY-MM-DD"`. */
  doneEarly?: Record<string, string>;
  /** Actual completion dates by `stepIndex` -> `"YYYY-MM-DD"`. */
  stepDates?: Record<number, string>;
  /** Intervals.icu activity IDs already consumed so we never double-advance. */
  consumedIds: string[];
}

export interface IntervalsActivityLite {
  id: string;
  name?: string;
  type?: string;
  start_date_local?: string;
  paired_event_id?: number | null;
}

export interface EventCreateSpec {
  external_id: string;
  start_date_local: string;
  category: 'WORKOUT';
  type: 'WeightTraining' | 'VirtualRide' | 'VirtualRun';
  name: string;
  description: string;
  moving_time: number;
}

export interface QueueOps {
  nextState: QueueState;
  targetDate: string | null;
  deletes: number[];
  moves: { id: number; date: string }[];
  creates: EventCreateSpec[];
}

const PREFIX = 'armory-q-';

export const planExternalId = (planId: string, stepIndex: number, slotIndex: number): string =>
  `${PREFIX}${planId}-s${stepIndex}-${slotIndex}`;

export function isPlanExternalId(extId: string | null | undefined, planId?: string): boolean {
  if (!extId || !extId.startsWith(PREFIX)) return false;
  if (!planId) return true;
  return extId.startsWith(`${PREFIX}${planId}-s`);
}

export function parsePlanExternalId(extId: string | null | undefined): { planId: string; stepIndex: number; slotIndex: number } | null {
  if (!extId || !extId.startsWith(PREFIX)) return null;
  const m = extId.match(/^armory-q-(.+)-s(\d+)-(\d+)$/);
  if (!m) return null;
  return { planId: m[1], stepIndex: Number(m[2]), slotIndex: Number(m[3]) };
}

export function activityMatchesSlot(act: Pick<IntervalsActivityLite, 'type' | 'name'>, slot: PlanSlot): boolean {
  const t = (act.type ?? '').toLowerCase();
  if (slot.equipment === 'strength') return t === 'weighttraining' || t === 'workout' || t === 'strengthtraining';
  if (slot.equipment === 'airbike' || slot.equipment === 'spin') return t === 'virtualride' || t === 'ride' || t === 'indoorcycling';
  if (slot.equipment === 'treadmill') return t === 'virtualrun' || t === 'run' || t === 'walk';
  return false;
}

export const slotKey = (stepIndex: number, slotIndex: number): string => `${stepIndex}:${slotIndex}`;

export function parseSlotKey(key: string | null | undefined): { stepIndex: number; slotIndex: number } | null {
  if (!key) return null;
  const m = key.match(/^(\d+):(\d+)$/);
  return m ? { stepIndex: Number(m[1]), slotIndex: Number(m[2]) } : null;
}

export const WEEK_THEMES: Record<number, string> = {
  1: '~60% Ramp · Base & Joint Tolerance (No Impact)',
  2: '~65% Ramp · Aerobic Volume (Final No-Impact Week)',
  3: '~70% Ramp · First Galloway 30s/90s Treadmill Intro',
  4: '~75% Ramp · Galloway 30s/60s + Phase 1 Peak',
};

export function ymdOf(y: number, m: number, d: number): string {
  const dt = new Date(y, m - 1, d);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
}

export function parseYmd(ymd: string): { y: number; m: number; d: number } {
  const [y, m, d] = ymd.split('-').map(Number);
  return { y, m, d };
}

export function shiftYmd(ymd: string, deltaDays: number): string {
  const { y, m, d } = parseYmd(ymd);
  return ymdOf(y, m, d + deltaDays);
}

/** Local day of week: 0 = Sun .. 6 = Sat. */
export function dayOfWeek(ymd: string): number {
  const { y, m, d } = parseYmd(ymd);
  return new Date(y, m - 1, d).getDay();
}

/** Skips `restDay` when scheduling future days (`0` = Sun .. `6` = Sat, `null` = none). */
export function nextTrainingDate(ymd: string, restDay: number | null): string {
  return restDay != null && dayOfWeek(ymd) === restDay ? shiftYmd(ymd, 1) : ymd;
}

type RoutineTag = 'lower 1' | 'upper 1' | 'lower 2' | 'upper 2';
type CardioTag = 'z2' | 'z5';

function routineTag(name?: string): RoutineTag | null {
  const n = (name ?? '').toLowerCase();
  if (n.includes('lower 1')) return 'lower 1';
  if (n.includes('upper 1')) return 'upper 1';
  if (n.includes('lower 2')) return 'lower 2';
  if (n.includes('upper 2')) return 'upper 2';
  return null;
}

function cardioTag(name?: string): CardioTag | null {
  const n = (name ?? '').toLowerCase();
  if (/\bz2\b|zone\s*2/.test(n)) return 'z2';
  if (/\bz5\b|vo2|4[x×]4|5[x×]3|60\/30|billat|norwegian/.test(n)) return 'z5';
  return null;
}

function hasExplicitConflict(actName: string | undefined, slotName: string): boolean {
  const ar = routineTag(actName);
  const sr = routineTag(slotName);
  if (ar && sr && ar !== sr) return true;
  const ac = cardioTag(actName);
  const sc = cardioTag(slotName);
  return !!(ac && sc && ac !== sc);
}

function matchesExplicitTag(actName: string | undefined, slotName: string): boolean {
  const ar = routineTag(actName);
  const sr = routineTag(slotName);
  if (ar && sr && ar === sr) return true;
  const ac = cardioTag(actName);
  const sc = cardioTag(slotName);
  return !!(ac && sc && ac === sc);
}

/**
 * Default state starts at Step 2 (index 1: Wk 1 · Day 2 Upper 1 + Z5 #1)
 * because Day 1 was already completed on 2026-11-15.
 */
export const defaultQueueState = (): QueueState => ({
  planId: PHASE1_PLAN.id,
  stepIndex: 1,
  completedSlots: [],
  stagedDate: null,
  slotDates: {},
  consumedIds: [],
});

const strengthSlot = (week: number, day: number, label: string, desc: string, mins = 50): PlanSlot => ({
  equipment: 'strength',
  intervalsType: 'WeightTraining',
  name: `Strength Train — Wk ${week} · Day ${day} ${label}`,
  description: desc,
  seconds: mins * 60,
});

const z5Slot = (programId: 'v5x3' | 'n4x4' | 'b6030', label: string, desc: string, mins: number): PlanSlot => ({
  equipment: 'airbike',
  programId,
  intervalsType: 'VirtualRide',
  name: label,
  description: desc,
  seconds: mins * 60,
});

const z2Slot = (mins: number, desc: string): PlanSlot => ({
  equipment: 'spin',
  intervalsType: 'VirtualRide',
  name: `Spin Bike Z2 (${mins}m)`,
  description: desc,
  seconds: mins * 60,
});

const runSlot = (name: string, mins: number, desc: string): PlanSlot => ({
  equipment: 'treadmill',
  intervalsType: 'VirtualRun',
  name,
  description: desc,
  seconds: mins * 60,
});

function buildPhase1Steps(): PlanStep[] {
  const weeks = [
    {
      w: 1, pct: '~60%',
      z5a: z5Slot('v5x3', 'AirBike Z5 — Classic VO2 5×3', '5m WU + 5x3m @ 53.5 RPM (220-228 W) / 2m easy. Chase HR 155+.', 30),
      mid: [z2Slot(45, 'Zone 2 Spin: 45m @ 128-130 W (82-88 RPM, HR 122-132 bpm). No impact.')],
      z5b: [z5Slot('b6030', 'AirBike Z5 — Billat 60/30', '2x8x(60s @ 55 RPM / 30s float @ 40 RPM). Or 11.5m Z5 Snack.', 33)],
    },
    {
      w: 2, pct: '~65%',
      z5a: z5Slot('n4x4', 'AirBike Z5 — Norwegian 4×4', '5m WU + 4x4m @ 52.5-54.5 RPM (218-225 W) / 3m easy. Chase HR 155+.', 33),
      mid: [z2Slot(45, 'Zone 2 Spin: 45m @ 128-130 W (82-88 RPM, HR 122-132 bpm). Final no-impact week.')],
      z5b: [z5Slot('v5x3', 'AirBike Z5 — Classic VO2 5×3', '5m WU + 5x3m @ 53.5 RPM / 2m easy. Or 10.5m express Z5 snack.', 30)],
    },
    {
      w: 3, pct: '~70%',
      z5a: z5Slot('n4x4', 'AirBike Z5 — Norwegian 4×4', '5m WU + 4x4m @ 52.5-54.5 RPM / 3m easy. Chase HR 155+.', 33),
      mid: [
        runSlot('Treadmill Galloway 30s/90s (20m)', 20, 'FIRST GALLOWAY: 30s easy jog / 90s walk x 10 @ 0.5-1% incline, 170-180 spm.'),
        z2Slot(25, 'Zone 2 Spin Top-Off: 25m @ 128-130 W (82-88 RPM, HR 122-132 bpm).'),
      ],
      z5b: [z5Slot('b6030', 'AirBike Z5 — Billat 60/30', '2x8x(60s @ 55 RPM / 30s float @ 40 RPM).', 33)],
    },
    {
      w: 4, pct: '~75%',
      z5a: z5Slot('n4x4', 'AirBike Z5 — Norwegian 4×4', '5m WU + 4x4m @ 52.5-54.5 RPM / 3m easy. Chase HR 155+.', 33),
      mid: [
        runSlot('Treadmill Galloway 30s/60s (28m)', 28, 'Galloway 30s jog / 60s walk for 28m, 170-180 spm.'),
        z2Slot(20, 'Zone 2 Spin Top-Off: 20m @ 128-130 W (82-88 RPM, HR 122-132 bpm).'),
      ],
      z5b: [
        z5Slot('v5x3', 'AirBike Z5 — Classic VO2 5×3', '5m WU + 5x3m @ 53.5 RPM / 2m easy.', 30),
        runSlot('Treadmill Galloway Shakeout (15m)', 15, 'Easy Galloway 30s jog / 60s walk (15m) shakeout.'),
      ],
    },
  ];

  const steps: PlanStep[] = [];
  for (const cfg of weeks) {
    const w = cfg.w;
    steps.push({
      index: steps.length,
      week: w,
      day: 1,
      title: `Wk ${w} · Day 1 — Lower 1 (Squat + RDL)`,
      slots: [strengthSlot(w, 1, 'Lower 1', `Low Bar Squat · RDL · Calf Raise / Cable Crunch (${cfg.pct} ramp).`, 55)],
    });
    steps.push({
      index: steps.length,
      week: w,
      day: 2,
      title: `Wk ${w} · Day 2 — Upper 1 + Z5 #1`,
      slots: [
        strengthSlot(w, 2, 'Upper 1', `Triceps Iso Primer · Bench / Smith Row · DB OHP / Pull-Up · Lat Raise / Pushdown (${cfg.pct}).`, 45),
        cfg.z5a,
      ],
    });
    steps.push({
      index: steps.length,
      week: w,
      day: 3,
      title: `Wk ${w} · Day 3 — ${w < 3 ? 'Zone 2 Base' : 'Galloway + Zone 2'}`,
      slots: cfg.mid,
    });
    steps.push({
      index: steps.length,
      week: w,
      day: 4,
      title: `Wk ${w} · Day 4 — Lower 2 (Deadlift + Split Squat)`,
      slots: [strengthSlot(w, 4, 'Lower 2', `Deadlift · DB Bulgarian Split Squat · Calf Raise / Cable Crunch (${cfg.pct}).`, 55)],
    });
    steps.push({
      index: steps.length,
      week: w,
      day: 5,
      title: `Wk ${w} · Day 5 — Upper 2`,
      slots: [strengthSlot(w, 5, 'Upper 2', `Triceps Iso Primer · Smith OHP / Lat Pulldown · Chest Fly / DB Row · Rear Delt / Curl (${cfg.pct}).`, 45)],
    });
    steps.push({
      index: steps.length,
      week: w,
      day: 6,
      title: `Wk ${w} · Day 6 — Z5 #2${w === 4 ? ' + Shakeout' : ''}`,
      slots: cfg.z5b,
    });
  }
  return steps;
}

export const PHASE1_PLAN: RollingPlan = {
  id: 'phase1',
  title: 'Armory Hybrid: Phase 1 (10K Base)',
  steps: buildPhase1Steps(),
};

/**
 * Pure rolling-queue reconciliation: matches paired events or recent completed activities against the
 * current step, advances when all slots of the step are complete, and computes the exact Intervals.icu
 * event creates/moves/deletes needed so only the current active step is staged.
 */
export function reconcileQueue(args: {
  plan: RollingPlan;
  state: QueueState;
  events: IntervalsEvent[];
  activities: IntervalsActivityLite[];
  today: string;
  tomorrow: string;
  restDay?: number | null;
}): QueueOps {
  const { plan, state, events, activities, today, tomorrow } = args;
  const restDay = args.restDay ?? null;
  let stepIndex = Math.max(0, Math.min(state.stepIndex, plan.steps.length));
  const consumed = new Set(state.consumedIds);
  let completedSlots = new Set(state.completedSlots);
  let slotDates: Record<number, string> = { ...(state.slotDates ?? {}) };
  const doneEarly: Record<string, string> = { ...(state.doneEarly ?? {}) };
  const stepDates: Record<number, string> = { ...(state.stepDates ?? {}) };
  let stagedDate = state.stagedDate;
  let justAdvanced = false;

  const absorbEarlyForCurrent = (): string => {
    let latest = '';
    if (stepIndex >= plan.steps.length) return latest;
    const step = plan.steps[stepIndex];
    for (let si = 0; si < step.slots.length; si++) {
      const k = slotKey(stepIndex, si);
      const d = doneEarly[k];
      if (d) {
        completedSlots.add(si);
        delete doneEarly[k];
        delete slotDates[si];
        if (d > latest) latest = d;
      }
    }
    return latest;
  };

  let curLatest = absorbEarlyForCurrent();

  if (stepIndex < plan.steps.length) {
    const step = plan.steps[stepIndex];
    // 1. Check paired events for the current step
    for (let si = 0; si < step.slots.length; si++) {
      const ext = planExternalId(plan.id, stepIndex, si);
      const ev = events.find((e) => e.external_id === ext);
      if (ev?.paired_activity_id) {
        completedSlots.add(si);
        consumed.add(String(ev.paired_activity_id));
        delete slotDates[si];
      }
    }

    // 2. Check recent unpaired external activities matching any remaining slot (or future audible slot)
    const since = stagedDate && stagedDate <= today ? stagedDate : today;
    const recent = activities
      .filter((a) => !a.paired_event_id && !consumed.has(String(a.id)) && (a.start_date_local ?? '').slice(0, 10) >= since)
      .sort((a, b) => (a.start_date_local ?? '').localeCompare(b.start_date_local ?? ''));

    for (const act of recent) {
      const idx = step.slots.findIndex(
        (sl, si) => !completedSlots.has(si) && activityMatchesSlot(act, sl) && !hasExplicitConflict(act.name, sl.name),
      );
      if (idx >= 0) {
        completedSlots.add(idx);
        consumed.add(String(act.id));
        delete slotDates[idx];
        continue;
      }
      if (routineTag(act.name) || cardioTag(act.name)) {
        let matched = false;
        for (let sIdx = stepIndex + 1; sIdx < plan.steps.length && !matched; sIdx++) {
          const fut = plan.steps[sIdx];
          for (let slIdx = 0; slIdx < fut.slots.length; slIdx++) {
            const k = slotKey(sIdx, slIdx);
            if (!doneEarly[k] && activityMatchesSlot(act, fut.slots[slIdx]) && matchesExplicitTag(act.name, fut.slots[slIdx].name)) {
              doneEarly[k] = (act.start_date_local ?? today).slice(0, 10);
              consumed.add(String(act.id));
              matched = true;
              break;
            }
          }
        }
      }
    }

    // 3. Advance to next incomplete step if all slots of the current step are done (auto-skipping steps finished early)
    if (completedSlots.size >= step.slots.length) {
      stepDates[stepIndex] = curLatest || today;
      stepIndex = Math.min(stepIndex + 1, plan.steps.length);
      completedSlots = new Set();
      slotDates = {};
      justAdvanced = true;
      while (stepIndex < plan.steps.length) {
        curLatest = absorbEarlyForCurrent();
        if (completedSlots.size >= plan.steps[stepIndex].slots.length) {
          stepDates[stepIndex] = curLatest || today;
          stepIndex = Math.min(stepIndex + 1, plan.steps.length);
          completedSlots = new Set();
          slotDates = {};
        } else {
          break;
        }
      }
      stagedDate = stepIndex < plan.steps.length ? nextTrainingDate(tomorrow, restDay) : null;
    }
  }

  for (const [k, d] of Object.entries(slotDates)) {
    if (d <= today) delete slotDates[Number(k)];
  }

  // Decide targetDate for the resulting active step
  let targetDate: string | null = null;
  if (stepIndex < plan.steps.length) {
    if (justAdvanced) targetDate = stagedDate!;
    else if (stagedDate && stagedDate > today) targetDate = stagedDate;
    else targetDate = today;
    stagedDate = targetDate;
  } else {
    stagedDate = null;
  }

  // Reconcile Intervals.icu events: keep only uncompleted slots of `stepIndex` on each slot's target date
  const desired = new Map<string, { slot: PlanSlot; date: string }>();
  if (stepIndex < plan.steps.length && targetDate) {
    const step = plan.steps[stepIndex];
    step.slots.forEach((slot, si) => {
      if (!completedSlots.has(si)) {
        const d = slotDates[si] && slotDates[si] > targetDate ? slotDates[si] : targetDate;
        desired.set(planExternalId(plan.id, stepIndex, si), { slot, date: d });
      }
    });
  }

  const deletes: number[] = [];
  const moves: { id: number; date: string }[] = [];
  const creates: EventCreateSpec[] = [];
  const seenExt = new Set<string>();

  for (const ev of events) {
    if (!isPlanExternalId(ev.external_id, plan.id)) continue;
    const ext = ev.external_id!;
    seenExt.add(ext);
    if (ev.paired_activity_id) continue;
    const want = desired.get(ext);
    if (!want || !targetDate) {
      deletes.push(ev.id);
    } else if ((ev.start_date_local ?? '').slice(0, 10) !== want.date) {
      moves.push({ id: ev.id, date: want.date });
    }
  }

  if (targetDate) {
    for (const [ext, want] of desired) {
      if (seenExt.has(ext)) continue;
      creates.push({
        external_id: ext,
        start_date_local: `${want.date}T00:00:00`,
        category: 'WORKOUT',
        type: want.slot.intervalsType,
        name: want.slot.name,
        description: want.slot.description,
        moving_time: want.slot.seconds,
      });
    }
  }

  // Keep consumedIds bounded to the last 40 entries
  const consumedList = Array.from(consumed).slice(-40);
  return {
    nextState: {
      planId: plan.id,
      stepIndex,
      completedSlots: Array.from(completedSlots).sort((a, b) => a - b),
      stagedDate,
      slotDates,
      doneEarly,
      stepDates,
      consumedIds: consumedList,
    },
    targetDate,
    deletes,
    moves,
    creates,
  };
}

export interface ProjectedStep {
  step: PlanStep;
  status: 'done' | 'early' | 'current' | 'upcoming';
  date: string | null;
  slotDone: boolean[];
  slotDates: (string | null)[];
}

/** Projects calendar dates across the entire rolling block, skipping `restDay` and steps finished early. */
export function projectPlan(plan: RollingPlan, state: QueueState, today: string, restDay: number | null): ProjectedStep[] {
  const out: ProjectedStep[] = [];
  let cursor: string | null = null;

  for (let i = 0; i < plan.steps.length; i++) {
    const step = plan.steps[i];
    if (i < state.stepIndex) {
      const d = state.stepDates?.[i] ?? null;
      out.push({ step, status: 'done', date: d, slotDone: step.slots.map(() => true), slotDates: step.slots.map(() => d) });
      continue;
    }

    if (i === state.stepIndex) {
      const slotDone = step.slots.map((_, si) => state.completedSlots.includes(si) || !!state.doneEarly?.[slotKey(i, si)]);
      const baseDate = state.stagedDate && state.stagedDate > today ? state.stagedDate : today;
      const slotDates = step.slots.map((_, si) => {
        const early = state.doneEarly?.[slotKey(i, si)];
        if (early) return early;
        if (slotDone[si]) return today;
        const ov = state.slotDates?.[si];
        return ov && ov > baseDate ? ov : baseDate;
      });
      const pendingDates = slotDates.filter((_, si) => !slotDone[si]) as string[];
      const stepDate = pendingDates.length ? [...pendingDates].sort()[0] : baseDate;
      cursor = pendingDates.length ? [...pendingDates].sort()[pendingDates.length - 1] : baseDate;
      out.push({ step, status: 'current', date: stepDate, slotDone, slotDates });
      continue;
    }

    const earlyDates = step.slots.map((_, si) => state.doneEarly?.[slotKey(i, si)] ?? null);
    const slotDone = earlyDates.map(Boolean);
    if (slotDone.every(Boolean)) {
      const latest = earlyDates.filter((d): d is string => !!d).sort().pop() ?? null;
      out.push({ step, status: 'early', date: latest, slotDone, slotDates: earlyDates });
      continue;
    }

    cursor = cursor === null
      ? nextTrainingDate(shiftYmd(today, 1), restDay)
      : nextTrainingDate(shiftYmd(cursor, 1), restDay);
    const slotDates = earlyDates.map((d) => d ?? cursor);
    out.push({ step, status: 'upcoming', date: cursor, slotDone, slotDates });
  }
  return out;
}

export function nextEquipmentSlot(proj: ProjectedStep[], eq: Equipment): { p: ProjectedStep; slot: PlanSlot; slotIndex: number } | null {
  for (const p of proj) {
    if (p.status === 'done' || p.status === 'early') continue;
    for (let si = 0; si < p.step.slots.length; si++) {
      if (!p.slotDone[si] && p.step.slots[si].equipment === eq) {
        return { p, slot: p.step.slots[si], slotIndex: si };
      }
    }
  }
  return null;
}

export interface WeekDayCell {
  ymd: string;
  dow: number;
  isToday: boolean;
  isPast: boolean;
  kind: 'done' | 'today' | 'upcoming' | 'rest';
  equipments: Equipment[];
  stepIndices: number[];
}

/** Builds a Sunday–Saturday 7-day strip mixing actual logged sessions with rolling-plan projections. */
export function buildWeekGlimpse(args: {
  today: string;
  proj: ProjectedStep[];
  todayEquipment?: Equipment[];
  loggedByDate: Map<string, { equipment: Equipment; name: string }[]>;
}): WeekDayCell[] {
  const { today, proj, todayEquipment = [], loggedByDate } = args;
  const sun = shiftYmd(today, -dayOfWeek(today));
  const cells: WeekDayCell[] = [];

  for (let d = 0; d < 7; d++) {
    const ymd = shiftYmd(sun, d);
    const logged = loggedByDate.get(ymd) ?? [];
    const stepSet = new Set<number>();
    const eqSet = new Set<Equipment>();

    if (ymd < today) {
      for (const l of logged) eqSet.add(l.equipment);
      cells.push({
        ymd,
        dow: d,
        isToday: false,
        isPast: true,
        kind: logged.length ? 'done' : 'rest',
        equipments: Array.from(eqSet),
        stepIndices: [],
      });
      continue;
    }

    if (ymd === today) {
      for (const l of logged) eqSet.add(l.equipment);
      let remaining = 0;
      for (const p of proj) {
        if (p.status !== 'current') continue;
        p.step.slots.forEach((sl, si) => {
          if (!p.slotDone[si] && p.slotDates[si] === today) {
            eqSet.add(sl.equipment);
            stepSet.add(p.step.index);
            remaining++;
          }
        });
      }
      for (const eq of todayEquipment) {
        eqSet.add(eq);
        remaining++;
      }
      const kind: WeekDayCell['kind'] = remaining > 0 ? 'today' : logged.length > 0 ? 'done' : 'rest';
      cells.push({ ymd, dow: d, isToday: true, isPast: false, kind, equipments: Array.from(eqSet), stepIndices: Array.from(stepSet) });
      continue;
    }

    for (const p of proj) {
      if (p.status !== 'current' && p.status !== 'upcoming') continue;
      p.step.slots.forEach((sl, si) => {
        if (!p.slotDone[si] && p.slotDates[si] === ymd) {
          eqSet.add(sl.equipment);
          stepSet.add(p.step.index);
        }
      });
    }
    cells.push({
      ymd,
      dow: d,
      isToday: false,
      isPast: false,
      kind: eqSet.size ? 'upcoming' : 'rest',
      equipments: Array.from(eqSet),
      stepIndices: Array.from(stepSet),
    });
  }
  return cells;
}
