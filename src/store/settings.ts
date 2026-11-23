import { Emitter } from '../emitter';
import type { MetroMode } from '../domain/pacer';
import type { RecoveryMode } from '../domain/gate';
import { defaultQueueState, type QueueState } from '../domain/plan';
import type { CustomVoiceAliases, VoiceAliasSlot } from '../lifts/voiceParser';

export interface Settings {
  /** Intervals.icu athlete id (not a secret). */
  athleteId: string;
  /** Intervals.icu API key. Stored per-device in localStorage & DriveSync backup; never baked into the APK. */
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
  /** Direct per-program work targets (whole RPM for AirBike, watts for Spin ERG). */
  programTargets: Record<string, number>;
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
  /** Push live wallboard to OptiPlex & auto-wake 32" Fire TV via ADB on workout start. */
  wallboardRelayEnabled: boolean;
  /** Foreground-only hands-free voice command listener ("armory ..."). */
  voiceEnabled: boolean;
  /** OptiPlex Room Mic (Anker PowerConf S330 via :8766 SSE) during active Strength workouts on home Wi-Fi. */
  voiceRoomEnabled: boolean;
  /** Passive dual-mic A/B benchmark mode (runs Room + Phone mics simultaneously during workouts with 4.5s execution dedup). */
  voiceAbCompare: boolean;
  voiceWakeWord: string;
  /** Speak short confirmation readbacks ("For 10, got it" / "Didn't catch that") after voice commands. */
  voiceReadback: boolean;
  /** How logged strength sets confirm: fast +10% voice readback vs crisp commit chime only. */
  voiceSetCommitMode: 'voice' | 'chime';
  /** User-added phrase aliases keyed by command action slot. */
  voiceCustomAliases: CustomVoiceAliases;
  /** OptiPlex Gym Camera LAN DVR integration (probes silently; hidden off home Wi-Fi). */
  gymCamEnabled: boolean;
  gymCamUrl: string;
  /** Permanent Google Drive URL for the live-updated Armory Cam multi-view preview screenshot. */
  gymCamPreviewUrl: string;
  /** Epoch ms when the Drive Armory Cam preview screenshot was last refreshed by OptiPlex. */
  gymCamPreviewUpdatedMs: number | null;
  gymCamAutoPr: boolean;
  /** Persistent Buddy Mode lock: avoids motion-based Auto camera fallbacks across workouts. */
  gymCamBuddyMode: boolean;
  /** Current position in the rolling training queue. */
  queue: QueueState;
}

const KEY = 'armory.settings.v1';
export const ROOM_VOICE_MIG_KEY = 'armory.mig.room_voice_default.v1';
export const TIMED_RECOVERY_MIG_KEY = 'armory.mig.timed_recovery_default.v1';
const BAKED_PREVIEW_URL = ((import.meta.env as Record<string, string | undefined> | undefined)?.VITE_GYM_CAM_PREVIEW_URL ?? '').trim();

export const DEFAULTS: Settings = {
  athleteId: 'i758105',
  apiKey: '',
  ftp: 182,
  ftpAutoSync: false,
  intervalsEftp: null,
  intervalsSetFtp: null,
  hrMax: 180,
  sex: 'M',
  weightKg: null,
  weightDate: null,
  birthDate: null,
  recoveryMode: 'timed',
  recoveryHrPct: 72,
  programOrder: [],
  programHidden: null,
  spinProgramOrder: [],
  spinProgramHidden: null,
  programTargets: {},
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
  wallboardRelayEnabled: true,
  voiceEnabled: false,
  voiceRoomEnabled: true,
  voiceAbCompare: false,
  voiceWakeWord: 'armory',
  voiceReadback: true,
  voiceSetCommitMode: 'voice',
  voiceCustomAliases: {},
  gymCamEnabled: true,
  gymCamUrl: 'http://192.168.1.152:8766',
  gymCamPreviewUrl: BAKED_PREVIEW_URL,
  gymCamPreviewUpdatedMs: null,
  gymCamAutoPr: true,
  gymCamBuddyMode: false,
  queue: defaultQueueState(),
};

function sanitizeAliases(raw: unknown): CustomVoiceAliases {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out: CustomVoiceAliases = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(v)) continue;
    const cleaned = v.map((x) => (typeof x === 'string' ? x.trim() : '')).filter(Boolean);
    if (cleaned.length) out[k as VoiceAliasSlot] = [...new Set(cleaned)];
  }
  return out;
}

export function migrateRoomVoiceDefault(
  settings: Settings,
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null,
): { settings: Settings; migrated: boolean } {
  if (!storage) return { settings, migrated: false };
  try {
    if (storage.getItem(ROOM_VOICE_MIG_KEY)) return { settings, migrated: false };
    const next: Settings = {
      ...settings,
      voiceEnabled: false,
      voiceRoomEnabled: true,
    };
    storage.setItem(KEY, JSON.stringify(next));
    storage.setItem(ROOM_VOICE_MIG_KEY, '1');
    return { settings: next, migrated: true };
  } catch {
    return { settings, migrated: false };
  }
}

function sanitizeProgramTargets(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'number' && Number.isFinite(v) && v > 0) out[k] = Math.round(v);
  }
  return out;
}

export function migrateTimedRecoveryDefault(
  settings: Settings,
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null,
): { settings: Settings; migrated: boolean } {
  if (!storage) return { settings, migrated: false };
  try {
    if (storage.getItem(TIMED_RECOVERY_MIG_KEY)) return { settings, migrated: false };
    const next: Settings = {
      ...settings,
      recoveryMode: 'timed',
    };
    storage.setItem(KEY, JSON.stringify(next));
    storage.setItem(TIMED_RECOVERY_MIG_KEY, '1');
    return { settings: next, migrated: true };
  } catch {
    return { settings, migrated: false };
  }
}

function load(): Settings {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Settings>;
    const defQ = defaultQueueState();
    const q = parsed.queue && typeof parsed.queue === 'object' ? { ...defQ, ...parsed.queue } : defQ;
    const base: Settings = {
      ...DEFAULTS,
      ...parsed,
      apiKey: typeof parsed.apiKey === 'string' && parsed.apiKey.trim() ? parsed.apiKey.trim() : DEFAULTS.apiKey,
      voiceRoomEnabled: typeof parsed.voiceRoomEnabled === 'boolean' ? parsed.voiceRoomEnabled : DEFAULTS.voiceRoomEnabled,
      voiceAbCompare: typeof parsed.voiceAbCompare === 'boolean' ? parsed.voiceAbCompare : DEFAULTS.voiceAbCompare,
      voiceReadback: typeof parsed.voiceReadback === 'boolean' ? parsed.voiceReadback : DEFAULTS.voiceReadback,
      voiceSetCommitMode: parsed.voiceSetCommitMode === 'chime' ? 'chime' : 'voice',
      gymCamBuddyMode: typeof parsed.gymCamBuddyMode === 'boolean' ? parsed.gymCamBuddyMode : DEFAULTS.gymCamBuddyMode,
      gymCamPreviewUrl: typeof parsed.gymCamPreviewUrl === 'string' && parsed.gymCamPreviewUrl.trim() ? parsed.gymCamPreviewUrl.trim() : DEFAULTS.gymCamPreviewUrl,
      gymCamPreviewUpdatedMs: typeof parsed.gymCamPreviewUpdatedMs === 'number' && Number.isFinite(parsed.gymCamPreviewUpdatedMs) ? parsed.gymCamPreviewUpdatedMs : null,
      voiceCustomAliases: sanitizeAliases(parsed.voiceCustomAliases),
      programTargets: sanitizeProgramTargets(parsed.programTargets),
      queue: q,
    };
    if (typeof localStorage === 'undefined') return base;
    const afterVoice = migrateRoomVoiceDefault(base, localStorage).settings;
    return migrateTimedRecoveryDefault(afterVoice, localStorage).settings;
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
