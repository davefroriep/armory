import { Emitter } from '../emitter';
import {
  PHASE1_PLAN, isPlanExternalId, parseSlotKey, reconcileQueue, slotKey, todayItemsFromQueue,
  type LocalTodayItem, type PlanStep, type QueueState,
} from '../domain/plan';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import type { WorkoutMeta } from '../types';
import { createManualActivity, deleteEvent, fetchActivities, fetchEvents, hasKey, syncAthleteFtp } from './api';

export type TodayItem = LocalTodayItem;
export type CalendarStatus = 'nokey' | 'loading' | 'ok' | 'error';

const LEGACY_CLEANUP_KEY = 'armory.cleanup_staged_events.v1';
const LEGACY_SKIP_Z5_KEY = 'armory.backfill_skipped_z5.v1';
let lastFtpSync = 0;

export const localDate = (d = new Date()): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

const localIsoNow = (d = new Date()): string => {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${localDate(d)}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

export function addDays(ymd: string, days: number): string {
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(y, m - 1, d + days);
  return localDate(dt);
}

/** Milliseconds until 2 seconds past the next local midnight (minimum 1s). */
export function msUntilNextLocalMidnight(now = new Date()): number {
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 2, 0);
  return Math.max(1_000, next.getTime() - now.getTime());
}

class Calendar {
  items: TodayItem[] = [];
  status: CalendarStatus = 'ok';
  error = '';
  readonly changed = new Emitter();
  private inflight: Promise<void> | null = null;
  private activeDay = localDate();
  private lastRefreshMs = 0;
  private midnightTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.syncLocalState();
    settingsChanged.on(() => {
      this.items = todayItemsFromQueue(PHASE1_PLAN, getSettings().queue, localDate());
    });
    if (typeof window !== 'undefined' && typeof document !== 'undefined') {
      this.scheduleMidnightRollover();
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) this.checkDayRollover(true);
      });
      window.addEventListener('focus', () => {
        this.checkDayRollover(true);
      });
    }
  }

  private scheduleMidnightRollover(): void {
    if (typeof window === 'undefined') return;
    if (this.midnightTimer !== null) clearTimeout(this.midnightTimer);
    this.midnightTimer = setTimeout(() => {
      this.checkDayRollover(true);
    }, msUntilNextLocalMidnight());
  }

  /** Reconciles the local rolling queue synchronously with zero network latency. */
  private syncLocalState(overrideQueue?: QueueState): void {
    const today = localDate();
    const s = getSettings();
    const inputQ = overrideQueue ?? s.queue;
    const ops = reconcileQueue({
      plan: PHASE1_PLAN,
      state: inputQ,
      activities: [],
      today,
      tomorrow: addDays(today, 1),
      restDay: s.restDay,
    });
    if (JSON.stringify(ops.nextState) !== JSON.stringify(s.queue)) {
      updateSettings({ queue: ops.nextState });
    }
    this.items = todayItemsFromQueue(PHASE1_PLAN, ops.nextState, today);
  }

  /** One-time local migration so yesterday's skipped AirBike Z5 shows as `(skipped)` rather than `✓`. */
  backfillLegacySkippedZ5(metas: WorkoutMeta[]): void {
    if (typeof localStorage === 'undefined' || localStorage.getItem(LEGACY_SKIP_Z5_KEY)) return;
    localStorage.setItem(LEGACY_SKIP_Z5_KEY, '1');
    const s = getSettings();
    if (s.queue.stepIndex > 1 && !s.queue.skippedSlots?.['1:1']) {
      const d = s.queue.stepDates?.[1] ?? '2026-11-16';
      const didAirbike = metas.some(
        (m) => m.status === 'saved' && m.equipment === 'airbike' && localDate(new Date(m.startEpochMs)) === d,
      );
      if (!didAirbike) {
        updateSettings({
          queue: {
            ...s.queue,
            skippedSlots: { ...(s.queue.skippedSlots ?? {}), '1:1': d },
          },
        });
        this.syncLocalState();
        this.changed.emit();
      }
    }
  }

  /** Rolls over Today if the local calendar date changed (e.g. left open overnight) and refreshes when stale. */
  checkDayRollover(refreshIfStale = false): boolean {
    this.scheduleMidnightRollover();
    const today = localDate();
    if (today !== this.activeDay) {
      this.activeDay = today;
      this.syncLocalState();
      this.changed.emit();
      void this.refresh();
      return true;
    }
    if (refreshIfStale && Date.now() - this.lastRefreshMs > 5 * 60_000) {
      void this.refresh();
    }
    return false;
  }

  /** Today's planned workouts, for one equipment tab. */
  forEquipment(eq: string): TodayItem[] {
    return this.items.filter((i) => i.equipment === eq);
  }

  /** Active rolling-plan step (or null if Phase 1 is complete). */
  currentStep(): PlanStep | null {
    return PHASE1_PLAN.steps[getSettings().queue.stepIndex] ?? null;
  }

  /** Non-null when today's remaining work is pushed/staged to tomorrow (or a future date). */
  upcomingStep(): { title: string; date: string } | null {
    const s = getSettings();
    const step = PHASE1_PLAN.steps[s.queue.stepIndex];
    if (!step) return null;
    const today = localDate();
    if (s.queue.stagedDate && s.queue.stagedDate > today) {
      return { title: step.title, date: s.queue.stagedDate };
    }
    const pending = step.slots
      .map((sl, si) => ({ sl, si, k: slotKey(s.queue.stepIndex, si) }))
      .filter((x) =>
        !s.queue.completedSlots.includes(x.si)
        && !s.queue.doneEarly?.[x.k]
        && !s.queue.skippedSlots?.[x.k]
        && !(s.queue.carryOver ?? []).some((c) => c.planSlot === x.k),
      );
    const dates = pending.map((x) => s.queue.slotDates?.[x.si] ?? '');
    if (pending.length && dates.every((d) => d > today)) {
      return { title: pending.map((x) => x.sl.name).join(' + '), date: dates.sort()[0] };
    }
    return null;
  }

  refresh(): Promise<void> {
    if (this.inflight) return this.inflight;
    this.inflight = this.runRefresh().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
  }

  private async runRefresh(): Promise<void> {
    const today = localDate();
    this.activeDay = today;
    this.syncLocalState();
    this.changed.emit();

    if (!hasKey()) {
      this.status = 'ok';
      this.error = '';
      return;
    }
    this.lastRefreshMs = Date.now();
    const tomorrow = addDays(today, 1);

    try {
      // One-time cleanup of legacy armory-q-* / armory-carry-* staged events on Intervals.icu
      if (typeof localStorage !== 'undefined' && !localStorage.getItem(LEGACY_CLEANUP_KEY)) {
        const oldEvents = await fetchEvents(addDays(today, -14), addDays(today, 21));
        for (const ev of oldEvents) {
          if (!ev.paired_activity_id && (isPlanExternalId(ev.external_id) || (ev.external_id ?? '').startsWith('armory-carry-'))) {
            await deleteEvent(ev.id).catch(() => {});
          }
        }
        localStorage.setItem(LEGACY_CLEANUP_KEY, '1');
      }

      const activities = await fetchActivities(addDays(today, -3), tomorrow);
      const s = getSettings();
      const ops = reconcileQueue({
        plan: PHASE1_PLAN,
        state: s.queue,
        activities,
        today,
        tomorrow,
        restDay: s.restDay,
      });
      if (JSON.stringify(ops.nextState) !== JSON.stringify(s.queue)) {
        updateSettings({ queue: ops.nextState });
      }
      this.items = todayItemsFromQueue(PHASE1_PLAN, ops.nextState, today);
      this.status = 'ok';
      this.error = '';

      if (Date.now() - lastFtpSync > 10 * 60_000) {
        lastFtpSync = Date.now();
        void syncAthleteFtp(false).catch(() => {});
      }
    } catch {
      // Offline / Intervals unreachable never blocks the local schedule
      this.status = 'ok';
      this.error = '';
    }
    this.changed.emit();
  }

  /** Marks one planned slot complete locally and logs a manual completed activity to Intervals.icu if configured. */
  async markSlotDone(planSlot: string): Promise<void> {
    const parsed = parseSlotKey(planSlot);
    const slot = parsed ? PHASE1_PLAN.steps[parsed.stepIndex]?.slots[parsed.slotIndex] : undefined;
    if (!parsed || !slot) return;
    const today = localDate();
    this.markSlotDoneEarly(planSlot, null, today);
    if (hasKey()) {
      try {
        const actId = await createManualActivity({
          startLocalIso: localIsoNow(),
          name: slot.name,
          description: 'Marked done in Armory Fit',
          seconds: slot.seconds || 2400,
          type: slot.intervalsType,
          externalId: `armory-done-${parsed.stepIndex}-${parsed.slotIndex}-${Date.now().toString(36)}`,
          pairedEventId: null,
        });
        const cur = getSettings();
        const consumedIds = Array.from(new Set([...cur.queue.consumedIds, String(actId)])).slice(-40);
        updateSettings({ queue: { ...cur.queue, consumedIds } });
      } catch { /* offline fallback already recorded locally */ }
    }
  }

  /** Marks a planned slot `(skipped)` locally and advances the rolling queue if no slots remain today. */
  skipSlot(planSlot: string): void {
    const s = getSettings();
    const skippedSlots = { ...(s.queue.skippedSlots ?? {}), [planSlot]: localDate() };
    const doneEarly = { ...(s.queue.doneEarly ?? {}) };
    delete doneEarly[planSlot];
    const carryOver = (s.queue.carryOver ?? []).filter((c) => c.planSlot !== planSlot);
    this.syncLocalState({ ...s.queue, skippedSlots, doneEarly, carryOver });
    this.changed.emit();
  }

  /** Moves just this workout to tomorrow and holds the rolling schedule back until it finishes. */
  pushSlotToTomorrow(planSlot: string): void {
    const parsed = parseSlotKey(planSlot);
    if (!parsed) return;
    const tomorrow = addDays(localDate(), 1);
    const s = getSettings();
    const carryIdx = (s.queue.carryOver ?? []).findIndex((c) => c.planSlot === planSlot);
    if (carryIdx >= 0) {
      const carryOver = (s.queue.carryOver ?? []).map((c, idx) => (idx === carryIdx ? { ...c, date: tomorrow } : c));
      this.syncLocalState({ ...s.queue, carryOver });
    } else if (parsed.stepIndex === s.queue.stepIndex) {
      const slotDates = { ...(s.queue.slotDates ?? {}), [parsed.slotIndex]: tomorrow };
      this.syncLocalState({ ...s.queue, slotDates });
    } else {
      return;
    }
    this.changed.emit();
  }

  /** Carries this workout to tomorrow independently while letting tomorrow's regular schedule advance normally. */
  addSlotToTomorrow(planSlot: string): void {
    const parsed = parseSlotKey(planSlot);
    if (!parsed) return;
    const tomorrow = addDays(localDate(), 1);
    const s = getSettings();
    const carryOver = (s.queue.carryOver ?? []).filter((c) => c.planSlot !== planSlot);
    carryOver.push({ planSlot, date: tomorrow });
    const slotDates = { ...(s.queue.slotDates ?? {}) };
    if (parsed.stepIndex === s.queue.stepIndex) delete slotDates[parsed.slotIndex];
    this.syncLocalState({ ...s.queue, carryOver, slotDates });
    this.changed.emit();
  }

  /** Pulls tomorrow's scheduled or carried-over work onto today. */
  pullTomorrowToToday(): void {
    const today = localDate();
    const s = getSettings();
    const carryOver = (s.queue.carryOver ?? []).map((c) => (c.date > today ? { ...c, date: today } : c));
    this.syncLocalState({
      ...s.queue,
      stagedDate: today,
      slotDates: {},
      carryOver,
    });
    this.changed.emit();
  }

  /** Shifts the rolling queue to `stepIndex` starting Today (marking bypassed incomplete slots `(skipped)`). */
  setStep(stepIndex: number): void {
    const clamped = Math.max(0, Math.min(stepIndex, PHASE1_PLAN.steps.length));
    const s = getSettings();
    const today = localDate();
    const doneEarly = { ...(s.queue.doneEarly ?? {}) };
    const skippedSlots = { ...(s.queue.skippedSlots ?? {}) };
    let carryOver = [...(s.queue.carryOver ?? [])];

    if (clamped > s.queue.stepIndex) {
      for (let i = s.queue.stepIndex; i < clamped; i++) {
        const step = PHASE1_PLAN.steps[i];
        if (!step) continue;
        for (let si = 0; si < step.slots.length; si++) {
          const k = slotKey(i, si);
          const wasDone = (i === s.queue.stepIndex && s.queue.completedSlots.includes(si)) || !!doneEarly[k];
          const carried = carryOver.some((c) => c.planSlot === k);
          if (!wasDone && !carried && !skippedSlots[k]) {
            skippedSlots[k] = today;
          }
        }
      }
    }

    const targetStep = PHASE1_PLAN.steps[clamped];
    if (targetStep) {
      for (let si = 0; si < targetStep.slots.length; si++) {
        const k = slotKey(clamped, si);
        delete doneEarly[k];
        delete skippedSlots[k];
        carryOver = carryOver.filter((c) => c.planSlot !== k);
      }
    }

    this.syncLocalState({
      ...s.queue,
      stepIndex: clamped,
      completedSlots: [],
      stagedDate: clamped < PHASE1_PLAN.steps.length ? today : null,
      slotDates: {},
      doneEarly,
      skippedSlots,
      carryOver,
    });
    this.changed.emit();
  }

  /** Marks a specific plan slot (`stepIndex:slotIndex`) done locally and advances/reconciles immediately. */
  markSlotDoneEarly(planSlot: string, activityId?: string | null, date = localDate()): void {
    const parsed = parseSlotKey(planSlot);
    if (!parsed) return;
    const s = getSettings();
    const consumedIds = activityId
      ? Array.from(new Set([...s.queue.consumedIds, String(activityId)])).slice(-40)
      : s.queue.consumedIds;
    const skippedSlots = { ...(s.queue.skippedSlots ?? {}) };
    delete skippedSlots[planSlot];
    const carryOver = (s.queue.carryOver ?? []).filter((c) => c.planSlot !== planSlot);

    if (parsed.stepIndex === s.queue.stepIndex) {
      const completedSlots = Array.from(new Set([...s.queue.completedSlots, parsed.slotIndex])).sort((a, b) => a - b);
      const slotDates = { ...(s.queue.slotDates ?? {}) };
      delete slotDates[parsed.slotIndex];
      this.syncLocalState({ ...s.queue, completedSlots, slotDates, skippedSlots, carryOver, consumedIds });
    } else {
      const doneEarly = { ...(s.queue.doneEarly ?? {}), [planSlot]: date };
      this.syncLocalState({ ...s.queue, doneEarly, skippedSlots, carryOver, consumedIds });
    }
    this.changed.emit();
  }

  /** Marks a cherry-picked future cardio slot done locally and logs to Intervals.icu when available. */
  async completeFutureSlotExternally(stepIndex: number, slotIndex: number): Promise<void> {
    await this.markSlotDone(slotKey(stepIndex, slotIndex));
  }
}

export const calendar = new Calendar();
