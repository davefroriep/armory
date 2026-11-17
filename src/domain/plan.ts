import type { Equipment } from '../types';

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
  /** Skipped slots: key `"${stepIndex}:${slotIndex}"` -> `"YYYY-MM-DD"`. */
  skippedSlots?: Record<string, string>;
  /** Removed slots (dropped cleanly without `(skipped)` badge): key `"${stepIndex}:${slotIndex}"` -> `"YYYY-MM-DD"`. */
  removedSlots?: Record<string, string>;
  /** Individual slots carried forward ("Add to tomorrow") independently of queue advancement. */
  carryOver?: { planSlot: string; date: string }[];
  /** One-off carry-over / make-up workouts attached to specific dates (e.g. skipped exercises). */
  extras?: ExtraWorkout[];
  /** Intervals.icu activity IDs already consumed so we never double-advance. */
  consumedIds: string[];
}

export interface ExtraWorkoutItem {
  exId: string;
  sets: number;
  ss?: boolean;
}

export interface ExtraWorkout {
  id: string;
  name: string;
  date: string;
  equipment: Equipment;
  seconds: number;
  description: string;
  items?: ExtraWorkoutItem[];
  doneDate?: string;
  skippedDate?: string;
}

export const UPPER1_FINISH_ITEMS: ExtraWorkoutItem[] = [
  { exId: 'seated-dumbbell-shoulder-press', sets: 3, ss: true },
  { exId: 'pull-up', sets: 3 },
  { exId: 'dumbbell-lateral-raise', sets: 3, ss: true },
  { exId: 'cable-triceps-pushdown', sets: 3 },
];

export interface IntervalsActivityLite {
  id: string;
  name?: string;
  type?: string;
  start_date_local?: string;
  external_id?: string | null;
  paired_event_id?: number | null;
}

export type SlotState = 'pending' | 'done' | 'skipped' | 'removed';
export type StepStatus = 'done' | 'skipped' | 'removed' | 'early' | 'current' | 'upcoming';

export interface LocalTodayItem {
  planSlot: string;
  stepIndex: number;
  slotIndex: number;
  slot: PlanSlot;
  equipment: Equipment;
  programId: string | null;
  carried?: boolean;
  items?: ExtraWorkoutItem[];
}

export interface QueueOps {
  nextState: QueueState;
}

export const extraKey = (id: string): string => `extra:${id}`;

export function parseExtraKey(key: string | null | undefined): string | null {
  return key && key.startsWith('extra:') ? key.slice(6) : null;
}

const intervalsTypeFor = (eq: Equipment): PlanSlot['intervalsType'] =>
  eq === 'strength' ? 'WeightTraining' : eq === 'treadmill' ? 'VirtualRun' : 'VirtualRide';

const PREFIX = 'armory-q-';

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
 * Pure rolling-queue reconciliation: matches external completed activities (e.g. MyWhoosh rides) against
 * the local queue, absorbs skipped/early/carried-over slots, and advances `stepIndex` cleanly.
 */
export function reconcileQueue(args: {
  plan: RollingPlan;
  state: QueueState;
  activities?: IntervalsActivityLite[];
  today: string;
  tomorrow: string;
  restDay?: number | null;
}): QueueOps {
  const { plan, state, today, tomorrow } = args;
  const activities = args.activities ?? [];
  const restDay = args.restDay ?? null;
  let stepIndex = Math.max(0, Math.min(state.stepIndex, plan.steps.length));
  const consumed = new Set(state.consumedIds);
  let completedSlots = new Set(state.completedSlots);
  let slotDates: Record<number, string> = { ...(state.slotDates ?? {}) };
  const doneEarly: Record<string, string> = { ...(state.doneEarly ?? {}) };
  const stepDates: Record<number, string> = { ...(state.stepDates ?? {}) };
  const skippedSlots: Record<string, string> = { ...(state.skippedSlots ?? {}) };
  const removedSlots: Record<string, string> = { ...(state.removedSlots ?? {}) };
  const carryOver: { planSlot: string; date: string }[] = (state.carryOver ?? []).map((c) => ({ ...c }));
  const extras: ExtraWorkout[] = (state.extras ?? []).map((x) =>
    !x.doneDate && !x.skippedDate && x.date < today ? { ...x, date: today } : { ...x },
  );
  let stagedDate = state.stagedDate;
  let justAdvanced = false;

  const isSlotResolvedOnStep = (si: number): boolean => {
    const k = slotKey(stepIndex, si);
    return completedSlots.has(si) || !!skippedSlots[k] || !!removedSlots[k] || carryOver.some((c) => c.planSlot === k);
  };

  const allSlotsResolved = (): boolean => {
    const step = plan.steps[stepIndex];
    return !!step && step.slots.every((_, si) => isSlotResolvedOnStep(si));
  };

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
        delete skippedSlots[k];
        delete removedSlots[k];
        delete slotDates[si];
        const ci = carryOver.findIndex((c) => c.planSlot === k);
        if (ci >= 0) carryOver.splice(ci, 1);
        if (d > latest) latest = d;
      }
    }
    return latest;
  };

  let curLatest = absorbEarlyForCurrent();

  // Match recent external activities (non-Armory) against carried-over, current, or future audible slots
  const since = stagedDate && stagedDate <= today ? stagedDate : today;
  const recent = activities
    .filter((a) => !(a.external_id ?? '').startsWith('armory-') && !a.paired_event_id && !consumed.has(String(a.id)) && (a.start_date_local ?? '').slice(0, 10) >= since)
    .sort((a, b) => (a.start_date_local ?? '').localeCompare(b.start_date_local ?? ''));

  for (const act of recent) {
    const actDate = (act.start_date_local ?? today).slice(0, 10);
    const carryIdx = carryOver.findIndex((c) => {
      if (c.date > today) return false;
      const p = parseSlotKey(c.planSlot);
      const futSlot = p ? plan.steps[p.stepIndex]?.slots[p.slotIndex] : undefined;
      return !!futSlot && activityMatchesSlot(act, futSlot) && !hasExplicitConflict(act.name, futSlot.name);
    });
    if (carryIdx >= 0) {
      const [hit] = carryOver.splice(carryIdx, 1);
      const p = parseSlotKey(hit.planSlot)!;
      if (p.stepIndex === stepIndex) {
        completedSlots.add(p.slotIndex);
        delete slotDates[p.slotIndex];
      } else {
        doneEarly[hit.planSlot] = actDate;
      }
      delete skippedSlots[hit.planSlot];
      delete removedSlots[hit.planSlot];
      consumed.add(String(act.id));
      continue;
    }

    if (stepIndex < plan.steps.length) {
      const step = plan.steps[stepIndex];
      const idx = step.slots.findIndex(
        (sl, si) => !isSlotResolvedOnStep(si) && activityMatchesSlot(act, sl) && !hasExplicitConflict(act.name, sl.name),
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
            if (!doneEarly[k] && !skippedSlots[k] && !removedSlots[k] && activityMatchesSlot(act, fut.slots[slIdx]) && matchesExplicitTag(act.name, fut.slots[slIdx].name)) {
              doneEarly[k] = actDate;
              consumed.add(String(act.id));
              matched = true;
              break;
            }
          }
        }
      }
    }
  }

  // Advance when every slot of the active step is resolved (completed, skipped, or carried over)
  if (stepIndex < plan.steps.length && allSlotsResolved()) {
    stepDates[stepIndex] = curLatest || today;
    stepIndex = Math.min(stepIndex + 1, plan.steps.length);
    completedSlots = new Set();
    slotDates = {};
    justAdvanced = true;
    while (stepIndex < plan.steps.length) {
      curLatest = absorbEarlyForCurrent();
      if (allSlotsResolved()) {
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

  for (const [k, d] of Object.entries(slotDates)) {
    if (d <= today) delete slotDates[Number(k)];
  }

  // Decide targetDate for the resulting active step
  if (stepIndex < plan.steps.length) {
    if (!justAdvanced && (!stagedDate || stagedDate <= today)) stagedDate = today;
  } else {
    stagedDate = null;
  }

  const cleanCarry = carryOver.filter((c) => !doneEarly[c.planSlot] && !skippedSlots[c.planSlot] && !removedSlots[c.planSlot]);
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
      skippedSlots,
      removedSlots,
      carryOver: cleanCarry,
      extras,
      consumedIds: consumedList,
    },
  };
}

/** Synchronously extracts today's active workouts (extras + carry-overs + unresolved current-step slots). */
export function todayItemsFromQueue(plan: RollingPlan, state: QueueState, today: string): LocalTodayItem[] {
  const out: LocalTodayItem[] = [];
  const seen = new Set<string>();

  for (const x of state.extras ?? []) {
    if (x.doneDate || x.skippedDate || x.date > today) continue;
    const k = extraKey(x.id);
    seen.add(k);
    out.push({
      planSlot: k,
      stepIndex: -1,
      slotIndex: -1,
      slot: {
        equipment: x.equipment,
        intervalsType: intervalsTypeFor(x.equipment),
        name: x.name,
        description: x.description,
        seconds: x.seconds,
      },
      equipment: x.equipment,
      programId: null,
      carried: true,
      items: x.items,
    });
  }

  for (const c of state.carryOver ?? []) {
    if (c.date > today || state.skippedSlots?.[c.planSlot] || state.removedSlots?.[c.planSlot] || state.doneEarly?.[c.planSlot]) continue;
    const p = parseSlotKey(c.planSlot);
    const slot = p ? plan.steps[p.stepIndex]?.slots[p.slotIndex] : undefined;
    if (!p || !slot) continue;
    seen.add(c.planSlot);
    out.push({
      planSlot: c.planSlot,
      stepIndex: p.stepIndex,
      slotIndex: p.slotIndex,
      slot,
      equipment: slot.equipment,
      programId: slot.programId ?? null,
      carried: true,
    });
  }

  if (state.stepIndex < plan.steps.length) {
    const step = plan.steps[state.stepIndex];
    const baseDate = state.stagedDate && state.stagedDate > today ? state.stagedDate : today;
    step.slots.forEach((slot, si) => {
      const k = slotKey(state.stepIndex, si);
      if (seen.has(k) || state.completedSlots.includes(si) || state.doneEarly?.[k] || state.skippedSlots?.[k] || state.removedSlots?.[k]) return;
      if ((state.carryOver ?? []).some((c) => c.planSlot === k)) return;
      const ov = state.slotDates?.[si];
      const d = ov && ov > baseDate ? ov : baseDate;
      if (d <= today) {
        out.push({
          planSlot: k,
          stepIndex: state.stepIndex,
          slotIndex: si,
          slot,
          equipment: slot.equipment,
          programId: slot.programId ?? null,
        });
      }
    });
  }

  return out;
}

export interface ProjectedStep {
  step: PlanStep;
  status: StepStatus;
  date: string | null;
  slotDone: boolean[];
  slotStates: SlotState[];
  slotDates: (string | null)[];
}

/** Projects calendar dates across the entire rolling block, skipping `restDay` and steps finished early. */
export function projectPlan(plan: RollingPlan, state: QueueState, today: string, restDay: number | null): ProjectedStep[] {
  const out: ProjectedStep[] = [];
  let cursor: string | null = null;
  const carryMap = new Map((state.carryOver ?? []).map((c) => [c.planSlot, c.date]));

  for (let i = 0; i < plan.steps.length; i++) {
    const step = plan.steps[i];
    if (i < state.stepIndex) {
      const slotStates: SlotState[] = step.slots.map((_, si) => {
        const k = slotKey(i, si);
        if (state.removedSlots?.[k]) return 'removed';
        if (state.skippedSlots?.[k]) return 'skipped';
        if (carryMap.has(k)) return 'pending';
        return 'done';
      });
      const slotDone = slotStates.map((st) => st === 'done');
      const slotDates = step.slots.map((_, si) => {
        const k = slotKey(i, si);
        if (state.removedSlots?.[k]) return state.removedSlots[k];
        if (state.skippedSlots?.[k]) return state.skippedSlots[k];
        if (carryMap.has(k)) return carryMap.get(k)!;
        return state.doneEarly?.[k] ?? state.stepDates?.[i] ?? null;
      });
      const d = state.stepDates?.[i] ?? slotDates.find((x) => !!x) ?? null;
      const status: StepStatus = slotStates.every((st) => st === 'removed')
        ? 'removed'
        : slotStates.every((st) => st === 'skipped' || st === 'removed')
          ? 'skipped'
          : 'done';
      out.push({ step, status, date: d, slotDone, slotStates, slotDates });
      continue;
    }

    if (i === state.stepIndex) {
      const slotStates: SlotState[] = step.slots.map((_, si) => {
        const k = slotKey(i, si);
        if (state.removedSlots?.[k]) return 'removed';
        if (state.skippedSlots?.[k]) return 'skipped';
        if (state.completedSlots.includes(si) || state.doneEarly?.[k]) return 'done';
        return 'pending';
      });
      const slotDone = slotStates.map((st) => st === 'done');
      const baseDate = state.stagedDate && state.stagedDate > today ? state.stagedDate : today;
      const slotDates = step.slots.map((_, si) => {
        const k = slotKey(i, si);
        if (carryMap.has(k)) return carryMap.get(k)!;
        if (state.removedSlots?.[k]) return state.removedSlots[k];
        if (state.skippedSlots?.[k]) return state.skippedSlots[k];
        const early = state.doneEarly?.[k];
        if (early) return early;
        if (slotStates[si] === 'done') return today;
        const ov = state.slotDates?.[si];
        return ov && ov > baseDate ? ov : baseDate;
      });
      const pendingDates = slotDates.filter((_, si) => slotStates[si] === 'pending' && !carryMap.has(slotKey(i, si))) as string[];
      const stepDate = pendingDates.length ? [...pendingDates].sort()[0] : baseDate;
      cursor = pendingDates.length ? [...pendingDates].sort()[pendingDates.length - 1] : baseDate;
      out.push({ step, status: 'current', date: stepDate, slotDone, slotStates, slotDates });
      continue;
    }

    const slotStates: SlotState[] = step.slots.map((_, si) => {
      const k = slotKey(i, si);
      if (state.removedSlots?.[k]) return 'removed';
      if (state.skippedSlots?.[k]) return 'skipped';
      if (state.doneEarly?.[k]) return 'done';
      return 'pending';
    });
    const slotDone = slotStates.map((st) => st === 'done');
    const resolvedDates = step.slots.map((_, si) => {
      const k = slotKey(i, si);
      return state.removedSlots?.[k] ?? state.skippedSlots?.[k] ?? state.doneEarly?.[k] ?? null;
    });
    if (slotStates.every((st) => st !== 'pending')) {
      const latest = resolvedDates.filter((d): d is string => !!d).sort().pop() ?? null;
      const status: StepStatus = slotStates.every((st) => st === 'removed')
        ? 'removed'
        : slotStates.some((st) => st === 'done')
          ? 'early'
          : 'skipped';
      out.push({ step, status, date: latest, slotDone, slotStates, slotDates: resolvedDates });
      continue;
    }

    cursor = cursor === null
      ? nextTrainingDate(shiftYmd(today, 1), restDay)
      : nextTrainingDate(shiftYmd(cursor, 1), restDay);
    const slotDates = resolvedDates.map((d, si) => carryMap.get(slotKey(i, si)) ?? d ?? cursor);
    out.push({ step, status: 'upcoming', date: cursor, slotDone, slotStates, slotDates });
  }
  return out;
}

export function nextEquipmentSlot(proj: ProjectedStep[], eq: Equipment): { p: ProjectedStep; slot: PlanSlot; slotIndex: number } | null {
  for (const p of proj) {
    if (p.status === 'early' || p.status === 'skipped' || p.status === 'removed') continue;
    for (let si = 0; si < p.step.slots.length; si++) {
      if (p.slotStates[si] === 'pending' && p.step.slots[si].equipment === eq) {
        return { p, slot: p.step.slots[si], slotIndex: si };
      }
    }
  }
  return null;
}

const MONTHS_UP = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

/** Formats a Sun–Sat date span for the locked calendar strip header (e.g. "NOV 15 – 21"). */
export function fmtWeekSpan(sunYmd: string): string {
  const satYmd = shiftYmd(sunYmd, 6);
  const s = parseYmd(sunYmd);
  const e = parseYmd(satYmd);
  const m1 = MONTHS_UP[s.m - 1] ?? '';
  const m2 = MONTHS_UP[e.m - 1] ?? '';
  return s.m === e.m ? `${m1} ${s.d} – ${e.d}` : `${m1} ${s.d} – ${m2} ${e.d}`;
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
  anchorYmd?: string;
  proj: ProjectedStep[];
  todayEquipment?: Equipment[];
  extras?: ExtraWorkout[];
  loggedByDate: Map<string, { equipment: Equipment; name: string }[]>;
}): WeekDayCell[] {
  const { today, anchorYmd = today, proj, todayEquipment = [], extras = [], loggedByDate } = args;
  const sun = shiftYmd(anchorYmd, -dayOfWeek(anchorYmd));
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
        p.step.slots.forEach((sl, si) => {
          if (p.slotStates[si] === 'pending' && p.slotDates[si] === today) {
            eqSet.add(sl.equipment);
            stepSet.add(p.step.index);
            remaining++;
          }
        });
      }
      for (const x of extras) {
        if (!x.doneDate && !x.skippedDate && x.date <= today) {
          eqSet.add(x.equipment);
          remaining++;
        }
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
      p.step.slots.forEach((sl, si) => {
        if (p.slotStates[si] === 'pending' && p.slotDates[si] === ymd) {
          eqSet.add(sl.equipment);
          stepSet.add(p.step.index);
        }
      });
    }
    for (const x of extras) {
      if (!x.doneDate && !x.skippedDate && x.date === ymd) eqSet.add(x.equipment);
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
