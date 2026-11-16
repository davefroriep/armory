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
}): QueueOps {
  const { plan, state, events, activities, today, tomorrow } = args;
  let stepIndex = Math.max(0, Math.min(state.stepIndex, plan.steps.length));
  const consumed = new Set(state.consumedIds);
  let completedSlots = new Set(state.completedSlots);
  let slotDates: Record<number, string> = { ...(state.slotDates ?? {}) };
  let stagedDate = state.stagedDate;
  let justAdvanced = false;

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

    // 2. Check recent unpaired external activities matching any remaining slot on/after stagedDate (or today)
    const since = stagedDate && stagedDate <= today ? stagedDate : today;
    const recent = activities
      .filter((a) => !a.paired_event_id && !consumed.has(String(a.id)) && (a.start_date_local ?? '').slice(0, 10) >= since)
      .sort((a, b) => (a.start_date_local ?? '').localeCompare(b.start_date_local ?? ''));

    for (const act of recent) {
      const idx = step.slots.findIndex((sl, si) => !completedSlots.has(si) && activityMatchesSlot(act, sl));
      if (idx >= 0) {
        completedSlots.add(idx);
        consumed.add(String(act.id));
        delete slotDates[idx];
      }
    }

    // 3. Advance to next step if all slots of the current step are done
    if (completedSlots.size >= step.slots.length) {
      stepIndex = Math.min(stepIndex + 1, plan.steps.length);
      completedSlots = new Set();
      slotDates = {};
      stagedDate = stepIndex < plan.steps.length ? tomorrow : null;
      justAdvanced = true;
    }
  }

  for (const [k, d] of Object.entries(slotDates)) {
    if (d <= today) delete slotDates[Number(k)];
  }

  // Decide targetDate for the resulting active step
  let targetDate: string | null = null;
  if (stepIndex < plan.steps.length) {
    if (justAdvanced) targetDate = tomorrow;
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
      consumedIds: consumedList,
    },
    targetDate,
    deletes,
    moves,
    creates,
  };
}
