import { Emitter } from '../emitter';
import type { MetroMode } from '../domain/pacer';
import type { RecoveryMode } from '../domain/gate';
import { defaultQueueState, type QueueState } from '../domain/plan';

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
  /** Body profile for HR-based strength calories (Keytel). Synced from the Intervals.icu profile; never committed. */
  sex: 'M' | 'F';
  weightKg: number | null;
  /** Date (`YYYY-MM-DD`) of the latest smart-scale wellness weight synced from Intervals.icu. */
  weightDate: string | null;
  /** ISO date (YYYY-MM-DD). */
  birthDate: string | null;
  /** "gated": recoveries last until HR is back at the target (floor + ceiling apply); "timed": plain timers. */
  recoveryMode: RecoveryMode;
  /** Recovered once HR is at or below this % of HRmax. */
  recoveryHrPct: number;
  /** AirBike program order (ids). Programs not listed follow in library order. */
  programOrder: string[];
  /** Hidden AirBike programs; null = never customised (library defaults). */
  programHidden: string[] | null;
  /** Spin Bike program order (ids). Programs not listed follow in library order. */
  spinProgramOrder: string[];
  /** Hidden Spin Bike programs; null = never customised (library defaults). */
  spinProgramHidden: string[] | null;
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
  /** Preferred weekly off-day (`0` = Sun .. `6` = Sat, `null` = None). Skipped when projecting future plan dates. */
  restDay: number | null;
  /** Local Wi-Fi HTTP/SSE server hosting the Smart TV wallboard. */
  wallboardEnabled: boolean;
  wallboardPort: number;
  /** Foreground-only hands-free voice command listener ("armory ..."). */
  voiceEnabled: boolean;
  voiceWakeWord: string;
  /** OptiPlex Gym Camera LAN DVR integration (probes silently; hidden off home Wi-Fi). */
  gymCamEnabled: boolean;
  gymCamUrl: string;
  gymCamAutoPr: boolean;
  /** Current position in the rolling training queue. */
  queue: QueueState;
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
  sex: 'M',
  weightKg: null,
  weightDate: null,
  birthDate: null,
  recoveryMode: 'gated',
  recoveryHrPct: 72,
  programOrder: [],
  programHidden: null,
  spinProgramOrder: [],
  spinProgramHidden: null,
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
  restDay: 0,
  wallboardEnabled: false,
  wallboardPort: 8765,
  voiceEnabled: false,
  voiceWakeWord: 'armory',
  gymCamEnabled: true,
  gymCamUrl: 'http://192.168.1.152:8766',
  gymCamAutoPr: true,
  queue: defaultQueueState(),
};

function load(): Settings {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Settings>;
    const defQ = defaultQueueState();
    const q = parsed.queue && typeof parsed.queue === 'object' ? { ...defQ, ...parsed.queue } : defQ;
    return {
      ...DEFAULTS,
      ...parsed,
      apiKey: typeof parsed.apiKey === 'string' && parsed.apiKey.trim() ? parsed.apiKey.trim() : DEFAULTS.apiKey,
      queue: q,
    };
  } catch {
    return { ...DEFAULTS, queue: defaultQueueState() };
  }
}

let current = load();
export const settingsChanged = new Emitter<Settings>();

export const getSettings = (): Settings => current;

export function updateSettings(patch: Partial<Settings>): Settings {
  current = { ...current, ...patch };
  if (typeof localStorage !== 'undefined') localStorage.setItem(KEY, JSON.stringify(current));
  settingsChanged.emit(current);
  return current;
}
