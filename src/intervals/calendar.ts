import { Emitter } from '../emitter';
import { PHASE1_PLAN, parsePlanExternalId, reconcileQueue, type PlanStep } from '../domain/plan';
import { getSettings, updateSettings } from '../store/settings';
import { createEvent, createManualActivity, deleteEvent, fetchActivities, fetchEvents, hasKey, moveEvent, syncAthleteFtp } from './api';
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

const localIsoNow = (d = new Date()): string => {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${localDate(d)}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

function resolveSlotInSettings(parsed: ReturnType<typeof parsePlanExternalId>): void {
  const s = getSettings();
  if (!s.autoStagePlan || !parsed || parsed.stepIndex !== s.queue.stepIndex) return;
  const completedSlots = Array.from(new Set([...s.queue.completedSlots, parsed.slotIndex])).sort((a, b) => a - b);
  const slotDates = { ...(s.queue.slotDates ?? {}) };
  delete slotDates[parsed.slotIndex];
  updateSettings({ queue: { ...s.queue, completedSlots, slotDates } });
}

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

  /** Non-null when today's remaining work is staged for tomorrow (or a future date). */
  upcomingStep(): { title: string; date: string } | null {
    const s = getSettings();
    if (!s.autoStagePlan) return null;
    const step = PHASE1_PLAN.steps[s.queue.stepIndex];
    if (!step) return null;
    const today = localDate();
    if (s.queue.stagedDate && s.queue.stagedDate > today) {
      return { title: step.title, date: s.queue.stagedDate };
    }
    const pending = step.slots.map((sl, si) => ({ sl, si })).filter((x) => !s.queue.completedSlots.includes(x.si));
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

  /** Marks one planned workout complete externally (creates & pairs a manual activity on Intervals.icu). */
  async markItemDone(evId: number): Promise<void> {
    const it = this.items.find((x) => x.ev.id === evId);
    if (!it) return;
    const type = it.ev.type ?? (it.match.equipment === 'strength' ? 'WeightTraining' : it.match.equipment === 'treadmill' ? 'VirtualRun' : 'VirtualRide');
    const seconds = it.ev.moving_time && it.ev.moving_time > 0 ? it.ev.moving_time : 2400;
    await createManualActivity({
      startLocalIso: localIsoNow(),
      name: it.ev.name ?? 'Workout',
      description: 'Marked done in Armory Fit (completed externally)',
      seconds,
      type,
      externalId: `armory-done-${evId}`,
      pairedEventId: evId,
    });
    resolveSlotInSettings(parsePlanExternalId(it.ev.external_id));
    await this.refresh();
  }

  /** Skips just this planned workout (deletes it from today without touching other workouts on the day). */
  async skipItem(evId: number): Promise<void> {
    const it = this.items.find((x) => x.ev.id === evId);
    if (!it) return;
    const s = getSettings();
    const parsed = parsePlanExternalId(it.ev.external_id);
    if (s.autoStagePlan && parsed && parsed.stepIndex === s.queue.stepIndex) {
      resolveSlotInSettings(parsed);
    } else {
      await deleteEvent(evId);
    }
    await this.refresh();
  }

  /** Moves just this workout to tomorrow and holds the rolling schedule back until it is finished. */
  async pushItemToTomorrow(evId: number): Promise<void> {
    const it = this.items.find((x) => x.ev.id === evId);
    if (!it) return;
    const tomorrow = addDays(localDate(), 1);
    const s = getSettings();
    const parsed = parsePlanExternalId(it.ev.external_id);
    if (s.autoStagePlan && parsed && parsed.stepIndex === s.queue.stepIndex) {
      const slotDates = { ...(s.queue.slotDates ?? {}), [parsed.slotIndex]: tomorrow };
      updateSettings({ queue: { ...s.queue, slotDates } });
    } else {
      await moveEvent(evId, tomorrow);
    }
    await this.refresh();
  }

  /** Moves just this workout onto tomorrow as a carry-over while letting tomorrow's regular schedule advance normally. */
  async addItemToTomorrow(evId: number): Promise<void> {
    const it = this.items.find((x) => x.ev.id === evId);
    if (!it) return;
    const tomorrow = addDays(localDate(), 1);
    const s = getSettings();
    const parsed = parsePlanExternalId(it.ev.external_id);
    if (s.autoStagePlan && parsed && parsed.stepIndex === s.queue.stepIndex) {
      const t = it.ev.type === 'WeightTraining' || it.ev.type === 'VirtualRun' || it.ev.type === 'VirtualRide' ? it.ev.type : 'VirtualRide';
      await createEvent({
        external_id: `armory-carry-${evId}-${Date.now().toString(36)}`,
        start_date_local: `${tomorrow}T00:00:00`,
        category: 'WORKOUT',
        type: t,
        name: it.ev.name ?? 'Workout',
        description: it.ev.description ?? '',
        moving_time: it.ev.moving_time ?? 1800,
      });
      resolveSlotInSettings(parsed);
    } else {
      await moveEvent(evId, tomorrow);
    }
    await this.refresh();
  }

  /** Pulls tomorrow's already-staged step onto today so you can train ahead of schedule. */
  async pullTomorrowToToday(): Promise<void> {
    const s = getSettings();
    updateSettings({
      queue: {
        ...s.queue,
        stagedDate: localDate(),
        slotDates: {},
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
        slotDates: {},
      },
    });
    await this.refresh();
  }
}

export const calendar = new Calendar();
