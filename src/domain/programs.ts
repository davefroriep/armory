import { wattsForRpm } from './airbikeCurve';

export type PhaseKind = 'warmup' | 'work' | 'steady' | 'float' | 'rest' | 'cooldown';

export interface Segment {
  kind: PhaseKind;
  /** Short phase label shown in the banner (e.g. "WORK"). */
  label: string;
  /** Rep / set chip (e.g. "REP 2/4"). */
  tag?: string;
  seconds: number;
  /** Target pedal RPM (null = free / full rest). */
  rpm: number | null;
  /** All-out effort: `rpm` is a minimum, not a corridor. */
  allOut?: boolean;
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
  segments: Segment[];
  goal?: GoalSpec;
  /** Pace used by the metronome when the program has no segment targets (free ride / goals). */
  paceRpm?: number;
}

export const PHASE_COLOR: Record<PhaseKind, string> = {
  warmup: '#f59e0b',
  work: '#ff4d2e',
  steady: '#22c55e',
  float: '#22d3ee',
  rest: '#64748b',
  cooldown: '#14b8a6',
};

const WARM_RPM = 44.0;
const EASY_RPM = 40.0;
export const FREE_PACE_RPM = 44.5;

const s = (kind: PhaseKind, label: string, seconds: number, rpm: number | null, extra: Partial<Segment> = {}): Segment => ({
  kind, label, seconds, rpm, ...extra,
});

const warmup = (): Segment => s('warmup', 'WARM-UP', 300, WARM_RPM);

/** WU + N x (work / easy) where the final easy block is the cool-down. */
function repeats(n: number, workSec: number, workRpm: number, easySec: number): Segment[] {
  const out: Segment[] = [warmup()];
  for (let i = 1; i <= n; i++) {
    out.push(s('work', 'WORK', workSec, workRpm, { tag: `REP ${i}/${n}` }));
    out.push(i < n ? s('rest', 'EASY SPIN', easySec, EASY_RPM, { tag: `REP ${i}/${n}` }) : s('cooldown', 'COOL-DOWN', easySec, EASY_RPM));
  }
  return out;
}

function billat(): Segment[] {
  const out: Segment[] = [warmup()];
  const sets = 2, reps = 8;
  for (let st = 1; st <= sets; st++) {
    for (let r = 1; r <= reps; r++) {
      out.push(s('work', 'WORK', 60, 57.0, { tag: `SET ${st}/${sets} - REP ${r}/${reps}` }));
      if (r < reps) out.push(s('float', 'FLOAT', 30, EASY_RPM, { tag: `SET ${st}/${sets} - KEEP FAN MOVING` }));
    }
    if (st < sets) out.push(s('rest', 'SET REST', 180, null, { tag: 'FULL REST OR <=35 RPM' }));
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

export const PROGRAMS: Program[] = [
  {
    id: 'z2', name: 'Zone 2 Base', intervalsName: 'AirBike Z2', group: 'z2', zone: 'Z2',
    blurb: 'Aerobic base. Hold 44.5 RPM (about 131 W).',
    segments: [
      s('warmup', 'WARM-UP', 300, EASY_RPM),
      s('steady', 'ZONE 2', 2700, 44.5),
      s('cooldown', 'COOL-DOWN', 300, EASY_RPM),
    ],
  },
  {
    id: 'n4x4', name: 'Norwegian 4×4', intervalsName: 'AirBike Z5 — Norwegian 4×4', group: 'z5', zone: 'Z5',
    blurb: 'Helgerud / NTNU. 4 × 4:00 @ 54.5 RPM, 3:00 easy.', segments: repeats(4, 240, 54.5, 180),
  },
  {
    id: 'v5x3', name: 'Classic VO2 5×3', intervalsName: 'AirBike Z5 — Classic VO2 5×3', group: 'z5', zone: 'Z5',
    blurb: 'Fractionated macro-intervals. 5 × 3:00 @ 55 RPM, 2:00 easy.', segments: repeats(5, 180, 55.0, 120),
  },
  {
    id: 'p6x2', name: 'Cardiac Power 6×2', intervalsName: 'AirBike Z5 — Cardiac Power 6×2', group: 'z5', zone: 'Z5',
    blurb: '1:1 aerobic power. 6 × 2:00 @ 56 RPM, 2:00 easy.', segments: repeats(6, 120, 56.0, 120),
  },
  {
    id: 'b6030', name: 'Billat / Rønnestad 60/30', intervalsName: 'AirBike Z5 — Billat 60/30', group: 'z5', zone: 'Z5',
    blurb: '2 sets × 8 × (60s @ 57 RPM / 30s float). Never stop the fan.', segments: billat(),
  },
  {
    id: 'tabata', name: 'Tabata 20/10', intervalsName: 'AirBike Tabata 20/10', group: 'hiit', zone: null,
    blurb: '8 rounds of 20s all-out (65+ RPM) / 10s easy.', segments: rounds(8, 20, 10, 65),
  },
  {
    id: 'sprint1020', name: 'Sprint 10/20', intervalsName: 'AirBike Sprint 10/20', group: 'hiit', zone: null,
    blurb: '8 rounds of 10s all-out (65+ RPM) / 20s easy.', segments: rounds(8, 10, 20, 65),
  },
  {
    id: 'time', name: 'Target Time', intervalsName: 'AirBike Target Time', group: 'goal', zone: null,
    blurb: 'Steady effort until the clock hits zero.', segments: [], paceRpm: FREE_PACE_RPM,
    goal: { type: 'time', unit: 'min', def: 10, min: 1, max: 120, step: 1 },
  },
  {
    id: 'cal', name: 'Target Calories', intervalsName: 'AirBike Target Cal', group: 'goal', zone: null,
    blurb: 'Burn the goal in Assault calories, as fast as you like.', segments: [], paceRpm: FREE_PACE_RPM,
    goal: { type: 'cal', unit: 'cal', def: 20, min: 5, max: 500, step: 5 },
  },
  {
    id: 'free', name: 'Free Ride', intervalsName: 'AirBike Free Ride', group: 'free', zone: null,
    blurb: 'No program. Just ride and record.', segments: [], paceRpm: FREE_PACE_RPM,
  },
];

export const programById = (id: string | null | undefined): Program | undefined => PROGRAMS.find((p) => p.id === id);

export const totalSeconds = (p: Program): number => p.segments.reduce((a, g) => a + g.seconds, 0);
export const workSeconds = (p: Program): number =>
  p.segments.filter((g) => g.kind === 'work').reduce((a, g) => a + g.seconds, 0);

export const targetWatts = (g: Segment): number | null => (g.rpm == null ? null : Math.round(wattsForRpm(g.rpm)));

export function fmtDur(sec: number): string {
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  return `${m}:${String(r).padStart(2, '0')}`;
}
