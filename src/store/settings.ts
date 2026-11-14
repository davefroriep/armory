import { Emitter } from '../emitter';
import type { MetroMode } from '../domain/pacer';

export interface Settings {
  /** Intervals.icu athlete id (not a secret). */
  athleteId: string;
  /** Intervals.icu API key. Lives ONLY in this device's localStorage - never in the repo. */
  apiKey: string;
  hrMax: number;
  metroEnabled: boolean;
  metroMode: MetroMode;
  metroLead: number;
  metroTail: number;
  metroAutoMute: boolean;
  metroVolume: number;
  chime: boolean;
  haptics: boolean;
  /** Fake H10 + AirBike for testing the UI without hardware. */
  simulator: boolean;
}

const KEY = 'armory.settings.v1';

export const DEFAULTS: Settings = {
  athleteId: 'i758105',
  apiKey: '',
  hrMax: 180,
  metroEnabled: true,
  metroMode: 'transitions',
  metroLead: 5,
  metroTail: 15,
  metroAutoMute: false,
  metroVolume: 0.8,
  chime: true,
  haptics: true,
  simulator: false,
};

function load(): Settings {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') };
  } catch {
    return { ...DEFAULTS };
  }
}

let current = load();
export const settingsChanged = new Emitter<Settings>();

export const getSettings = (): Settings => current;

export function updateSettings(patch: Partial<Settings>): Settings {
  current = { ...current, ...patch };
  localStorage.setItem(KEY, JSON.stringify(current));
  settingsChanged.emit(current);
  return current;
}
