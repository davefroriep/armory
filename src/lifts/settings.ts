import { Emitter } from '../emitter';

export type Unit = 'lb' | 'kg';

export interface LiftSettings {
  unit: Unit;
  /** Default rest between sets (s). */
  restSec: number;
  restAuto: boolean;
  restSound: boolean;
  /** Show the manual rest timer button (`▶ 1:30`) on exercise cards. */
  showTimer: boolean;
  /** Show the per-card `Auto` timer toggle pill on exercise cards. */
  showAutoTimer: boolean;
  /** Seconds of countdown before the camera starts recording (time to step back to the bar). */
  videoDelay: number;
}

const KEY = 'armory.lifts.settings.v1';
const CLEAN_TIMERS_KEY = 'armory.lifts.clean_timers_cfg.v1';
const DEFAULTS: LiftSettings = {
  unit: 'lb',
  restSec: 90,
  restAuto: false,
  restSound: true,
  showTimer: false,
  showAutoTimer: false,
  videoDelay: 5,
};

function load(): LiftSettings {
  if (typeof localStorage === 'undefined') return { ...DEFAULTS };
  let parsed: Partial<LiftSettings> = {};
  try {
    parsed = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<LiftSettings>;
  } catch {
    parsed = {};
  }
  const out: LiftSettings = { ...DEFAULTS, ...parsed };
  if (!localStorage.getItem(CLEAN_TIMERS_KEY)) {
    out.restAuto = false;
    out.showTimer = false;
    out.showAutoTimer = false;
    localStorage.setItem(KEY, JSON.stringify(out));
    localStorage.setItem(CLEAN_TIMERS_KEY, '1');
  }
  return out;
}

let cur = load();
export const liftSettingsChanged = new Emitter<LiftSettings>();
export const liftSettings = (): LiftSettings => cur;
export function updateLiftSettings(p: Partial<LiftSettings>): void {
  cur = { ...cur, ...p };
  if (typeof localStorage !== 'undefined') localStorage.setItem(KEY, JSON.stringify(cur));
  liftSettingsChanged.emit(cur);
}

// ---------- units ----------
export const KG_PER_LB = 0.45359237;
export const LB_PER_KG = 1 / KG_PER_LB;

/** Round to 0.1 so lb<->kg conversion never leaks float noise into the UI. */
const r1 = (n: number): number => Math.round(n * 10) / 10;

export const toDisplay = (kg: number, unit: Unit = cur.unit): number => (unit === 'lb' ? r1(kg * LB_PER_KG) : r1(kg));
export const fromDisplay = (v: number, unit: Unit = cur.unit): number => (unit === 'lb' ? v * KG_PER_LB : v);

/** "135", "137.5", "62.5". */
export function fmtNum(v: number): string {
  return Number.isInteger(v) ? String(v) : String(r1(v));
}
export const fmtKg = (kg: number | null | undefined, unit: Unit = cur.unit): string =>
  kg == null ? '' : fmtNum(toDisplay(kg, unit));

/** Weight-field stepper buttons, in display units. */
export const stepsFor = (unit: Unit = cur.unit): number[] => (unit === 'lb' ? [-10, -5, 5, 10] : [-5, -2.5, 2.5, 5]);
