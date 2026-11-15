import { Emitter } from '../emitter';
import type { MetroMode } from '../domain/pacer';
import type { RecoveryMode } from '../domain/gate';

export interface Settings {
  /** Intervals.icu athlete id (not a secret). */
  athleteId: string;
  /** Intervals.icu API key. Baked via build-time env or pasted in Settings; never committed to git. */
  apiKey: string;
  /** Threshold power (W) used to scale AirBike Z2/Z5 targets (% FTP). */
  ftp: number;
  /** Automatically update `ftp` whenever Intervals.icu models a new eFTP. */
  ftpAutoSync: boolean;
  /** Last modeled eFTP reported by Intervals.icu (`sportSettings.mmp_model.ftp`). */
  intervalsEftp: number | null;
  /** Last manual FTP configured on Intervals.icu (`sportSettings.indoor_ftp` / `ftp`). */
  intervalsSetFtp: number | null;
  hrMax: number;
  /** "gated": recoveries last until HR is back at the target (floor + ceiling apply); "timed": plain timers. */
  recoveryMode: RecoveryMode;
  /** Recovered once HR is at or below this % of HRmax. */
  recoveryHrPct: number;
  /** AirBike program order (ids). Programs not listed follow in library order. */
  programOrder: string[];
  /** Hidden AirBike programs; null = never customised (library defaults). */
  programHidden: string[] | null;
  metroEnabled: boolean;
  metroMode: MetroMode;
  metroLead: number;
  metroTail: number;
  metroAutoMute: boolean;
  metroVolume: number;
  chime: boolean;
  haptics: boolean;
  /** Seconds of averaging on the cadence bar + cue text ('0' = raw). Stored as a string by the segmented control. */
  needleSmooth: '0' | '1' | '2' | '3';
  /** Fake H10 + AirBike for testing the UI without hardware. */
  simulator: boolean;
}

const KEY = 'armory.settings.v1';
const BAKED_KEY = ((import.meta.env as Record<string, string | undefined> | undefined)?.VITE_INTERVALS_API_KEY ?? '').trim();

export const DEFAULTS: Settings = {
  athleteId: 'i758105',
  apiKey: BAKED_KEY,
  ftp: 182,
  ftpAutoSync: false,
  intervalsEftp: null,
  intervalsSetFtp: null,
  hrMax: 180,
  recoveryMode: 'gated',
  recoveryHrPct: 72,
  programOrder: [],
  programHidden: null,
  metroEnabled: true,
  metroMode: 'transitions',
  metroLead: 5,
  metroTail: 15,
  metroAutoMute: false,
  metroVolume: 0.8,
  chime: true,
  haptics: true,
  needleSmooth: '2',
  simulator: false,
};

function load(): Settings {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Settings>;
    return {
      ...DEFAULTS,
      ...parsed,
      apiKey: typeof parsed.apiKey === 'string' && parsed.apiKey.trim() ? parsed.apiKey.trim() : DEFAULTS.apiKey,
    };
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
