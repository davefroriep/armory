import { Emitter } from '../emitter';
import { getSettings } from '../store/settings';
import { hasKey } from './api';
import { classifyEvent, isPlannedWorkout, type EventMatch, type IntervalsEvent } from './parse';

export interface TodayItem {
  ev: IntervalsEvent;
  match: EventMatch;
}

export type CalendarStatus = 'nokey' | 'loading' | 'ok' | 'error';

const BASE = 'https://intervals.icu/api/v1';
const CACHE_KEY = 'armory.calendar.today';

export const localDate = (d = new Date()): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

class Calendar {
  items: TodayItem[] = [];
  status: CalendarStatus = hasKey() ? 'loading' : 'nokey';
  error = '';
  readonly changed = new Emitter();

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

  async refresh(): Promise<void> {
    if (!hasKey()) {
      this.status = 'nokey';
      this.items = [];
      this.changed.emit();
      return;
    }
    this.status = 'loading';
    this.changed.emit();
    const day = localDate();
    try {
      const s = getSettings();
      const url = `${BASE}/athlete/${encodeURIComponent(s.athleteId.trim())}/events?oldest=${day}&newest=${day}`;
      const res = await fetch(url, { headers: { Authorization: 'Basic ' + btoa('API_KEY:' + s.apiKey.trim()) } });
      if (!res.ok) throw new Error(res.status === 401 || res.status === 403 ? 'API key rejected' : `HTTP ${res.status}`);
      const evs: IntervalsEvent[] = await res.json();
      this.items = evs.filter(isPlannedWorkout).map((ev) => ({ ev, match: classifyEvent(ev) }));
      localStorage.setItem(CACHE_KEY, JSON.stringify({ date: day, items: this.items }));
      this.status = 'ok';
      this.error = '';
    } catch (e) {
      this.status = 'error';
      this.error = e instanceof TypeError ? 'Offline - showing last known plan' : (e as Error).message;
    }
    this.changed.emit();
  }
}

export const calendar = new Calendar();
