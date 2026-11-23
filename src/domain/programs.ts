import { rpmForWatts, wattsForRpm } from './airbikeCurve';

export const BASE_FTP = 182;

export function fmtRpm(rpm: number): string {
  return String(Math.round(rpm));
}

export type PhaseKind = 'warmup' | 'work' | 'steady' | 'float' | 'rest' | 'cooldown';

export interface Segment {
  kind: PhaseKind;
  /** Short phase label shown in the banner (e.g. "WORK"). */
  label: string;
  /** Rep / set chip (e.g. "REP 2/4"). */
  tag?: string;
  seconds: number;
  /** Target pedal RPM (null = free / full rest; recommended spin cadence when `watts` is set). */
  rpm: number | null;
  /** Direct ERG wattage target (used by Bowflex C7 / SS2K Spin; null/undefined = derive from AirBike RPM curve). */
  watts?: number | null;
  /** All-out effort: `rpm` is a minimum, not a corridor. */
  allOut?: boolean;
  /**
   * Heart-rate gated recovery. `seconds` is the nominal (timer) length and also the floor; the block then
   * stays open until HR has dropped to the recovery target, at most `maxSec`.
   */
  gate?: Gate;
}

export interface Gate {
  minSec: number;
  maxSec: number;
}

export type ProgramGroup = 'z2' | 'z5' | 'hiit' | 'goal';

export interface GoalSpec {
  type: 'time' | 'cal';
  unit: string;
  def: number;
  min: number;
  max: number;
  step: number;
}

export interface Program {
  id: string;
  name: string;
  /** Name used on Intervals.icu activities. */
  intervalsName: string;
  blurb: string;
  group: ProgramGroup | 'free';
  zone: 'Z2' | 'Z5' | null;
  /** Owning machine (defaults to `'airbike'` for existing programs). */
  equipment?: 'airbike' | 'spin';
  segments: Segment[];
  goal?: GoalSpec;
  /** Pace used by the metronome when the program has no segment targets (free ride / goals). */
  paceRpm?: number;
  /** Tucked into the "Hidden" shelf until the athlete restores it. */
  hidden?: boolean;
}

export const PHASE_COLOR: Record<PhaseKind, string> = {
  warmup: '#f59e0b',
  work: '#ff4d2e',
  steady: '#22c55e',
  float: '#22d3ee',
  rest: '#64748b',
  cooldown: '#14b8a6',
};

const WARM_RPM = 44;
const EASY_RPM = 40;
export const FREE_PACE_RPM = 45;

const s = (kind: PhaseKind, label: string, seconds: number, rpm: number | null, extra: Partial<Segment> = {}): Segment => ({
  kind, label, seconds, rpm, ...extra,
});

const warmup = (): Segment => s('warmup', 'WARM-UP', 300, WARM_RPM);

/** WU + N x (work / easy) where the final easy block is the cool-down. */
function repeats(n: number, workSec: number, workRpm: number, easySec: number, gateMaxSec?: number): Segment[] {
  const out: Segment[] = [warmup()];
  for (let i = 1; i <= n; i++) {
    out.push(s('work', 'WORK', workSec, workRpm, { tag: `REP ${i}/${n}` }));
    out.push(i < n
      ? s('rest', 'EASY SPIN', easySec, EASY_RPM, { tag: `REP ${i}/${n}`, ...(gateMaxSec ? { gate: { minSec: easySec, maxSec: gateMaxSec } } : {}) })
      : s('cooldown', 'COOL-DOWN', easySec, EASY_RPM));
  }
  return out;
}

function billat(): Segment[] {
  const out: Segment[] = [warmup()];
  const sets = 2, reps = 8;
  for (let st = 1; st <= sets; st++) {
    for (let r = 1; r <= reps; r++) {
      out.push(s('work', 'WORK', 60, 56, { tag: `SET ${st}/${sets} - REP ${r}/${reps}` }));
      if (r < reps) out.push(s('float', 'FLOAT', 30, EASY_RPM, { tag: `SET ${st}/${sets} - KEEP FAN MOVING` }));
    }
    if (st < sets) out.push(s('rest', 'SET REST', 180, null, { tag: 'FULL REST OR <=35 RPM', gate: { minSec: 180, maxSec: 300 } }));
  }
  out.push(s('cooldown', 'COOL-DOWN', 120, EASY_RPM));
  return out;
}

function rounds(n: number, workSec: number, restSec: number, workRpm: number): Segment[] {
  const out: Segment[] = [];
  for (let i = 1; i <= n; i++) {
    out.push(s('work', 'ALL OUT', workSec, workRpm, { tag: `ROUND ${i}/${n}`, allOut: true }));
    out.push(s('rest', 'EASY SPIN', restSec, EASY_RPM, { tag: `ROUND ${i}/${n}` }));
  }
  return out;
}

/**
 * Default library order = preferred first. The athlete can reorder / hide programs from the AirBike
 * screen (hold a card); that choice is stored in settings, so editing this list never needs a rebuild
 * to keep their arrangement.
 *
 * Whole-integer AirBike work RPM defaults: 54 (218 W) / 55 (228 W) / 56 (240 W).
 */
export const PROGRAMS: Program[] = [
  {
    id: 'n4x4', name: 'Norwegian 4×4', intervalsName: 'AirBike Z5 — Norwegian 4×4', group: 'z5', zone: 'Z5',
    blurb: 'NTNU CERG · 4 × 4:00 / 3:00 easy. Chase HR ≥ 90%.', segments: repeats(4, 240, 54, 180, 300),
  },
  {
    id: 'v5x3', name: 'Classic VO2 5×3', intervalsName: 'AirBike Z5 — Classic VO2 5×3', group: 'z5', zone: 'Z5',
    blurb: 'Macro-intervals · 5 × 3:00 / 2:00 easy. Chase HR ≥ 90%.', segments: repeats(5, 180, 55, 120, 240),
  },
  {
    id: 'b6030', name: '60/30 Float', intervalsName: 'AirBike Z5 — Billat 60/30', group: 'z5', zone: 'Z5',
    blurb: '2 × 8 × (60s work / 30s float). Fast clock, keep the fan moving.', segments: billat(),
  },
  {
    id: 'sprint1020', name: 'Sprint 10/20', intervalsName: 'AirBike Sprint 10/20', group: 'hiit', zone: null,
    blurb: '8 rounds of 10s all-out (65+ RPM) / 20s easy. Painless 4-minute hit.', segments: rounds(8, 10, 20, 65),
  },
  {
    id: 'p6x2', name: 'Cardiac Power 6×2', intervalsName: 'AirBike Z5 — Cardiac Power 6×2', group: 'z5', zone: 'Z5', hidden: true,
    blurb: 'Jamieson-style · 6 × 2:00 / 2:00 easy.', segments: repeats(6, 120, 56, 120, 300),
  },
  {
    id: 'tabata', name: 'Tabata 20/10', intervalsName: 'AirBike Tabata 20/10', group: 'hiit', zone: null, hidden: true,
    blurb: '8 rounds of 20s all-out (65+ RPM) / 10s easy. High dread, little carry-over.', segments: rounds(8, 20, 10, 65),
  },
  {
    id: 'z2', name: 'Zone 2 Base', intervalsName: 'AirBike Z2', group: 'z2', zone: 'Z2', hidden: true,
    blurb: 'Aerobic base · 45:00 steady (Z2 lives on spin / treadmill).',
    segments: [
      s('warmup', 'WARM-UP', 300, EASY_RPM),
      s('steady', 'ZONE 2', 2700, 45),
      s('cooldown', 'COOL-DOWN', 300, EASY_RPM),
    ],
  },
  {
    id: 'free', name: 'Free Ride', intervalsName: 'AirBike Free Ride', group: 'free', zone: null,
    blurb: 'No program. Just ride and record.', segments: [], paceRpm: FREE_PACE_RPM,
  },
];

export const SPIN_FREE_PACE_RPM = 85;

function spinRepeats(n: number, workSec: number, workW: number, workRpm: number, easySec: number, gateMaxSec: number): Segment[] {
  const out: Segment[] = [s('warmup', 'WARM-UP', 300, 82, { watts: 105 })];
  for (let i = 1; i <= n; i++) {
    out.push(s('work', 'WORK', workSec, workRpm, { watts: workW, tag: `REP ${i}/${n}` }));
    out.push(i < n
      ? s('rest', 'EASY SPIN', easySec, 80, { watts: 105, tag: `REP ${i}/${n}`, gate: { minSec: easySec, maxSec: gateMaxSec } })
      : s('cooldown', 'COOL-DOWN', easySec, 78, { watts: 95 }));
  }
  return out;
}

function spinBillat(): Segment[] {
  const out: Segment[] = [s('warmup', 'WARM-UP', 300, 82, { watts: 105 })];
  const sets = 2, reps = 8;
  for (let st = 1; st <= sets; st++) {
    for (let r = 1; r <= reps; r++) {
      out.push(s('work', 'WORK', 60, 90, { watts: 245, tag: `SET ${st}/${sets} - REP ${r}/${reps}` }));
      if (r < reps) out.push(s('float', 'FLOAT', 30, 78, { watts: 110, tag: `SET ${st}/${sets} - KEEP SPINNING` }));
    }
    if (st < sets) out.push(s('rest', 'SET REST', 180, 75, { watts: 95, tag: 'EASY RECOVERY', gate: { minSec: 180, maxSec: 300 } }));
  }
  out.push(s('cooldown', 'COOL-DOWN', 120, 78, { watts: 95 }));
  return out;
}

/**
 * Calibrated Bowflex C7 + SS2K Auto-ERG Spin library (`0%–50%`, 20–542 W model).
 * Work blocks drive SS2K `SET_MODE:1` / `SET_WATTS:<w>` automatically, keeping load locked across 50–110 RPM.
 */
export const SPIN_PROGRAMS: Program[] = [
  {
    id: 'spin-z2', name: 'Zone 2 Base (45m)', intervalsName: 'Spin Bike Z2 (45m)', group: 'z2', zone: 'Z2', equipment: 'spin',
    blurb: 'Aerobic base · 35m @ 129 W (71% FTP, ~85 RPM) + 5m WU/CD. Auto-ERG.',
    segments: [
      s('warmup', 'WARM-UP', 300, 82, { watts: 100 }),
      s('steady', 'ZONE 2', 2100, 85, { watts: 129 }),
      s('cooldown', 'COOL-DOWN', 300, 80, { watts: 95 }),
    ],
  },
  {
    id: 'spin-z2-25', name: 'Zone 2 Top-Off (25m)', intervalsName: 'Spin Bike Z2 (25m)', group: 'z2', zone: 'Z2', equipment: 'spin',
    blurb: 'Galloway run top-off · 19m @ 129 W (~85 RPM) + 3m WU/CD. Auto-ERG.',
    segments: [
      s('warmup', 'WARM-UP', 180, 82, { watts: 100 }),
      s('steady', 'ZONE 2', 1140, 85, { watts: 129 }),
      s('cooldown', 'COOL-DOWN', 180, 80, { watts: 95 }),
    ],
  },
  {
    id: 'spin-z2-20', name: 'Zone 2 Top-Off (20m)', intervalsName: 'Spin Bike Z2 (20m)', group: 'z2', zone: 'Z2', equipment: 'spin', hidden: true,
    blurb: 'Wk 4 top-off · 16m @ 129 W (~85 RPM) + 2m WU/CD. Auto-ERG.',
    segments: [
      s('warmup', 'WARM-UP', 120, 82, { watts: 95 }),
      s('steady', 'ZONE 2', 960, 85, { watts: 129 }),
      s('cooldown', 'COOL-DOWN', 120, 80, { watts: 90 }),
    ],
  },
  {
    id: 'spin-n4x4', name: 'Norwegian 4×4 (ERG)', intervalsName: 'Spin Z5 — Norwegian 4×4', group: 'z5', zone: 'Z5', equipment: 'spin',
    blurb: 'NTNU CERG · 4 × 4:00 / 3:00 easy · Auto-ERG.',
    segments: spinRepeats(4, 240, 220, 85, 180, 300),
  },
  {
    id: 'spin-v5x3', name: 'Classic VO2 5×3 (ERG)', intervalsName: 'Spin Z5 — Classic VO2 5×3', group: 'z5', zone: 'Z5', equipment: 'spin',
    blurb: 'Macro-intervals · 5 × 3:00 / 2:00 easy · Auto-ERG.',
    segments: spinRepeats(5, 180, 225, 85, 120, 240),
  },
  {
    id: 'spin-b6030', name: '60/30 Billat (ERG)', intervalsName: 'Spin Z5 — Billat 60/30', group: 'z5', zone: 'Z5', equipment: 'spin',
    blurb: '2 × 8 × (60s / 30s) · Auto-ERG.',
    segments: spinBillat(),
  },
  {
    id: 'spin-p6x2', name: 'Cardiac Power 6×2 (ERG)', intervalsName: 'Spin Z5 — Cardiac Power 6×2', group: 'z5', zone: 'Z5', equipment: 'spin', hidden: true,
    blurb: 'Jamieson-style · 6 × 2:00 / 2:00 easy · Auto-ERG.',
    segments: spinRepeats(6, 120, 235, 88, 120, 300),
  },
  {
    id: 'spin-free', name: 'Free Ride', intervalsName: 'Spin Bike Free Ride', group: 'free', zone: null, equipment: 'spin',
    blurb: 'Manual SS2K gear control. Ride freely and record power, cadence & HR.',
    segments: [], paceRpm: SPIN_FREE_PACE_RPM,
  },
];

export const programsForEquipment = (eq: 'airbike' | 'spin'): Program[] => (eq === 'spin' ? SPIN_PROGRAMS : PROGRAMS);

export function defaultProgramTarget(p: Program): number | null {
  const ref = p.segments.find((g) => (g.kind === 'work' || g.kind === 'steady') && !g.allOut);
  if (!ref) return null;
  if (ref.watts != null) return Math.round(ref.watts);
  if (ref.rpm != null) return Math.round(ref.rpm);
  return null;
}

export function clampProgramTarget(p: Program, value: number): number {
  const isSpin = p.equipment === 'spin' || p.segments.some((g) => g.watts != null);
  const rounded = Math.round(value);
  return isSpin ? Math.max(50, Math.min(550, rounded)) : Math.max(30, Math.min(85, rounded));
}

export function effectiveProgramTarget(p: Program, storedTargets?: Record<string, number> | null): number | null {
  const def = defaultProgramTarget(p);
  if (def == null) return null;
  const stored = storedTargets?.[p.id];
  if (typeof stored === 'number' && Number.isFinite(stored) && stored > 0) return clampProgramTarget(p, stored);
  return def;
}

export function applyProgramTarget(p: Program, target: number | null | undefined): Program {
  if (target == null || !Number.isFinite(target) || target <= 0) return p;
  const val = clampProgramTarget(p, target);
  return {
    ...p,
    segments: p.segments.map((g) => {
      if ((g.kind !== 'work' && g.kind !== 'steady') || g.allOut) return g;
      if (g.watts != null) return { ...g, watts: val };
      if (g.rpm != null) return { ...g, rpm: val };
      return g;
    }),
  };
}

export function formatProgramTarget(p: Program, target: number): string {
  const isSpin = p.equipment === 'spin' || p.segments.some((g) => g.watts != null);
  const rounded = Math.round(target);
  return isSpin ? `${rounded} W` : `${rounded} RPM (${Math.round(wattsForRpm(rounded))} W)`;
}

export function scaleSpinTargetsForFtp(ftp: number): Record<string, number> {
  const out: Record<string, number> = {};
  if (!Number.isFinite(ftp) || ftp <= 0) return out;
  for (const p of SPIN_PROGRAMS) {
    if (p.zone !== 'Z2' && p.zone !== 'Z5') continue;
    const def = defaultProgramTarget(p);
    if (def == null) continue;
    const raw = def * (ftp / BASE_FTP);
    const val = p.zone === 'Z5' ? Math.round(raw / 5) * 5 : Math.round(raw);
    out[p.id] = clampProgramTarget(p, val);
  }
  return out;
}

export interface ProgressionAdvice {
  kind: 'adopt' | 'bump' | 'ease' | 'hold';
  programId: string;
  currentTarget: number;
  nextTarget: number;
  currentLabel: string;
  nextLabel: string;
  reason: string;
  defaultApply: boolean;
}

export function evaluateCardioProgression(args: {
  program: Program;
  storedTarget: number;
  sessionTarget: number;
  elapsedSec: number;
  totalPlannedSec: number;
  z5Seconds: number;
  maxHr: number;
  hrMax: number;
  rpe: number;
}): ProgressionAdvice | null {
  const { program: p, storedTarget, sessionTarget, elapsedSec, totalPlannedSec, z5Seconds, maxHr, hrMax, rpe } = args;
  if (p.zone !== 'Z5' && p.zone !== 'Z2') return null;
  if (defaultProgramTarget(p) == null || totalPlannedSec <= 0 || elapsedSec < totalPlannedSec * 0.5) return null;
  const isSpin = p.equipment === 'spin' || p.segments.some((g) => g.watts != null);
  const step = isSpin ? 5 : 1;
  const unit = isSpin ? 'W' : 'RPM';
  const currentLabel = formatProgramTarget(p, storedTarget);

  if (sessionTarget !== storedTarget) {
    const nextTarget = clampProgramTarget(p, sessionTarget);
    const nextLabel = formatProgramTarget(p, nextTarget);
    return {
      kind: 'adopt',
      programId: p.id,
      currentTarget: storedTarget,
      nextTarget,
      currentLabel,
      nextLabel,
      reason: `You finished at ${nextLabel} — save as your new target`,
      defaultApply: true,
    };
  }

  if (p.zone === 'Z5' && elapsedSec >= totalPlannedSec * 0.8 && maxHr > 0 && hrMax > 0) {
    const cap = isSpin ? 450 : 68;
    const floor = isSpin ? 100 : 42;
    const underCooked =
      (rpe <= 6 && (z5Seconds < 210 || maxHr < Math.round(hrMax * 0.92)))
      || (rpe <= 7 && z5Seconds < 120 && maxHr < Math.round(hrMax * 0.91));
    if (underCooked) {
      const nextTarget = Math.min(cap, storedTarget + step);
      if (nextTarget > storedTarget) {
        const nextLabel = formatProgramTarget(p, nextTarget);
        return {
          kind: 'bump',
          programId: p.id,
          currentTarget: storedTarget,
          nextTarget,
          currentLabel,
          nextLabel,
          reason: `Z5 bank ${fmtDur(z5Seconds)} (peak ${Math.round(maxHr)} bpm, RPE ${rpe}) — bump +${step} ${unit} next time`,
          defaultApply: true,
        };
      }
    }

    const redlined = rpe >= 9 && (rpe === 10 || maxHr >= Math.round(hrMax * 0.97));
    if (redlined) {
      const nextTarget = Math.max(floor, storedTarget - step);
      if (nextTarget < storedTarget) {
        const nextLabel = formatProgramTarget(p, nextTarget);
        return {
          kind: 'ease',
          programId: p.id,
          currentTarget: storedTarget,
          nextTarget,
          currentLabel,
          nextLabel,
          reason: `Very hard (peak ${Math.round(maxHr)} bpm, RPE ${rpe}) — optionally ease −${step} ${unit}`,
          defaultApply: false,
        };
      }
    }

    return {
      kind: 'hold',
      programId: p.id,
      currentTarget: storedTarget,
      nextTarget: storedTarget,
      currentLabel,
      nextLabel: currentLabel,
      reason: `Z5 bank ${fmtDur(z5Seconds)} · RPE ${rpe} — target dialed in`,
      defaultApply: false,
    };
  }

  return null;
}

export function resolveProgram(
  p: Program,
  ftp = BASE_FTP,
  offsetWatts = 0,
  storedTargets?: Record<string, number> | null,
): Program {
  const stored = storedTargets?.[p.id];
  if (typeof stored === 'number' && Number.isFinite(stored) && stored > 0 && offsetWatts === 0) {
    return applyProgramTarget(p, stored);
  }
  if ((p.zone !== 'Z5' && p.zone !== 'Z2') || (ftp === BASE_FTP && offsetWatts === 0)) return p;
  return {
    ...p,
    segments: p.segments.map((g) => {
      if (g.watts != null) {
        if (g.kind === 'work' || g.kind === 'steady') {
          return { ...g, watts: Math.max(50, Math.round(g.watts * (ftp / BASE_FTP)) + offsetWatts) };
        }
        if (ftp === BASE_FTP) return g;
        return { ...g, watts: Math.max(50, Math.round(g.watts * (ftp / BASE_FTP))) };
      }
      if (g.rpm == null || g.allOut) return g;
      if (g.kind === 'work' || g.kind === 'steady') {
        const w = Math.max(50, Math.round(wattsForRpm(g.rpm) * (ftp / BASE_FTP)) + offsetWatts);
        return { ...g, rpm: Math.min(85, Math.max(30, Math.round(rpmForWatts(w)))) };
      }
      if (ftp === BASE_FTP) return g;
      const w = wattsForRpm(g.rpm) * (ftp / BASE_FTP);
      return { ...g, rpm: Math.min(48, Math.max(36, Math.round(rpmForWatts(w)))) };
    }),
  };
}

export const programById = (id: string | null | undefined): Program | undefined =>
  PROGRAMS.find((p) => p.id === id) ?? SPIN_PROGRAMS.find((p) => p.id === id);

export const totalSeconds = (p: Program): number => p.segments.reduce((a, g) => a + g.seconds, 0);
export const workSeconds = (p: Program): number =>
  p.segments.filter((g) => g.kind === 'work').reduce((a, g) => a + g.seconds, 0);

export const targetWatts = (g: Segment): number | null => {
  if (g.watts != null) return Math.round(g.watts);
  return g.rpm == null ? null : Math.round(wattsForRpm(g.rpm));
};

export function fmtDur(sec: number): string {
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  return `${m}:${String(r).padStart(2, '0')}`;
}
