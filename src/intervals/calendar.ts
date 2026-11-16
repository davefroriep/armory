import { Emitter } from '../emitter';
import { PHASE1_PLAN, reconcileQueue, type PlanStep } from '../domain/plan';
import { getSettings, updateSettings } from '../store/settings';
import { createEvent, deleteEvent, fetchActivities, fetchEvents, hasKey, moveEvent, syncAthleteFtp } from './api';
import { classifyEvent, isPlannedWorkout, type EventMatch, type IntervalsEvent } from './parse';

export interface TodayItem {
  ev: IntervalsEvent;
  match: EventMatch;
}

export type CalendarStatus = 'nokey' | 'loading' | 'ok' | 'error';

const CACHE_KEY = 'armory.calendar.today';
let lastFtpSync = 0;

export const localDate = (d = new Date()): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

export function addDays(ymd: string, days: number): string {
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(y, m - 1, d + days);
  return localDate(dt);
}

class Calendar {
  items: TodayItem[] = [];
  status: CalendarStatus = hasKey() ? 'loading' : 'nokey';
  error = '';
  readonly changed = new Emitter();
  private inflight: Promise<void> | null = null;

  constructor() {
    try {
      const c = JSON.parse(localStorage.getItem(CACHE_KEY) ?? 'null');
      if (c?.date === localDate()) this.items = c.items;
    } catch { /* ignore */ }
  }

  /** Today's planned workouts, for one equipment tab. */
  forEquipment(eq: string): TodayItem[] {
    return this.items.filter((i) => i.match.equipment === eq);
  }

  /** Active rolling-plan step (or null if plan is complete or auto-staging is off). */
  currentStep(): PlanStep | null {
    const s = getSettings();
    if (!s.autoStagePlan) return null;
    return PHASE1_PLAN.steps[s.queue.stepIndex] ?? null;
  }

  /** Non-null when today's step is done and the next step is staged for tomorrow (or a future date). */
  upcomingStep(): { step: PlanStep; date: string } | null {
    const s = getSettings();
    if (!s.autoStagePlan) return null;
    const step = PHASE1_PLAN.steps[s.queue.stepIndex];
    const today = localDate();
    if (!step || !s.queue.stagedDate || s.queue.stagedDate <= today) return null;
    return { step, date: s.queue.stagedDate };
  }

  refresh(): Promise<void> {
    if (this.inflight) return this.inflight;
    this.inflight = this.runRefresh().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
  }

  private async runRefresh(): Promise<void> {
    if (!hasKey()) {
      this.status = 'nokey';
      this.items = [];
      this.changed.emit();
      return;
    }
    this.status = 'loading';
    this.changed.emit();
    const today = localDate();
    const tomorrow = addDays(today, 1);

    try {
      const s = getSettings();
      let todayEvents: IntervalsEvent[];

      if (s.autoStagePlan) {
        const [events, activities] = await Promise.all([
          fetchEvents(addDays(today, -14), addDays(today, 14)),
          fetchActivities(addDays(today, -3), tomorrow),
        ]);
        const ops = reconcileQueue({
          plan: PHASE1_PLAN,
          state: s.queue,
          events,
          activities,
          today,
          tomorrow,
        });

        for (const id of ops.deletes) await deleteEvent(id);
        for (const m of ops.moves) await moveEvent(m.id, m.date);
        for (const c of ops.creates) await createEvent(c);

        updateSettings({ queue: ops.nextState });

        const refreshed = ops.deletes.length || ops.moves.length || ops.creates.length
          ? await fetchEvents(today, today)
          : events.filter((e) => (e.start_date_local ?? '').slice(0, 10) === today);
        todayEvents = refreshed.filter((e) => !e.paired_activity_id);
      } else {
        todayEvents = (await fetchEvents(today, today)).filter((e) => !e.paired_activity_id);
      }

      this.items = todayEvents.filter(isPlannedWorkout).map((ev) => ({ ev, match: classifyEvent(ev) }));
      localStorage.setItem(CACHE_KEY, JSON.stringify({ date: today, items: this.items }));
      this.status = 'ok';
      this.error = '';

      if (Date.now() - lastFtpSync > 10 * 60_000) {
        lastFtpSync = Date.now();
        void syncAthleteFtp(false).catch(() => {});
      }
    } catch (e) {
      this.status = 'error';
      this.error = e instanceof TypeError ? 'Offline - showing last known plan' : (e as Error).message;
    }
    this.changed.emit();
  }

  /** Marks the current step complete manually (e.g., lifted in Boostcamp without Armory HR) and stages the next one for tomorrow. */
  async markCurrentDone(): Promise<void> {
    const s = getSettings();
    if (s.queue.stepIndex >= PHASE1_PLAN.steps.length) return;
    const nextIdx = Math.min(s.queue.stepIndex + 1, PHASE1_PLAN.steps.length);
    const tomorrow = addDays(localDate(), 1);
    updateSettings({
      queue: {
        ...s.queue,
        stepIndex: nextIdx,
        completedSlots: [],
        stagedDate: nextIdx < PHASE1_PLAN.steps.length ? tomorrow : null,
      },
    });
    await this.refresh();
  }

  /** Pulls tomorrow's already-staged step onto today so you can train ahead of schedule. */
  async pullTomorrowToToday(): Promise<void> {
    const s = getSettings();
    updateSettings({
      queue: {
        ...s.queue,
        stagedDate: localDate(),
      },
    });
    await this.refresh();
  }

  /** Jumps the rolling queue to a specific 0-based step and stages it for today. */
  async setStep(stepIndex: number): Promise<void> {
    const clamped = Math.max(0, Math.min(stepIndex, PHASE1_PLAN.steps.length));
    const s = getSettings();
    updateSettings({
      queue: {
        ...s.queue,
        stepIndex: clamped,
        completedSlots: [],
        stagedDate: clamped < PHASE1_PLAN.steps.length ? localDate() : null,
      },
    });
    await this.refresh();
  }
}

export const calendar = new Calendar();
