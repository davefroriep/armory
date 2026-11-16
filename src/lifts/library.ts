import type { Equip, ExType, Exercise, Muscle } from './types';

/**
 * Master exercise list. One compact row per exercise:
 *   Name | equipment | type | primary muscles | secondary muscles | search aliases
 * type: w = weight x reps, b = bodyweight (+ optional load) x reps, t = time (seconds, optional load).
 * Muscle codes are in types.ts. Primary movers count 1.0 set, secondary 0.5 (see muscles.ts).
 * No descriptions, photos or how-to: names and muscle mapping only.
 */
const RAW = `
# Chest
Barbell Bench Press|bb|w|ch|fd,tri|bench flat
Paused Bench Press|bb|w|ch|fd,tri|pause bench
Incline Barbell Bench Press|bb|w|ch|fd,tri|incline bench
Close-Grip Bench Press|bb|w|tri|ch,fd|cgbp
Dumbbell Bench Press|db|w|ch|fd,tri|db bench
Incline Dumbbell Press|db|w|ch|fd,tri|incline db
Smith Machine Bench Press|smith|w|ch|fd,tri|
Smith Machine Incline Press|smith|w|ch|fd,tri|
Machine Chest Press|machine|w|ch|fd,tri|
Chest Fly Machine|machine|w|ch|fd|pec deck fly
Dumbbell Fly|db|w|ch|fd|flye
Incline Dumbbell Fly|db|w|ch|fd|
Cable Crossover|cable|w|ch|fd|cable fly crossover high to low
Low-to-High Cable Fly|cable|w|ch|fd|
Push-Up|bw|b|ch|fd,tri|pushup
Dip|bw|b|ch,tri|fd|dips weighted
Landmine Press|other|w|fd,ch|tri|
# Shoulders
Overhead Press|bb|w|fd|sd,tri|ohp military press
Seated Dumbbell Shoulder Press|db|w|fd|sd,tri|db press
Standing Dumbbell Shoulder Press|db|w|fd|sd,tri|
Arnold Press|db|w|fd|sd,tri|
Smith Machine Overhead Press|smith|w|fd|sd,tri|smith ohp
Machine Shoulder Press|machine|w|fd|sd,tri|
Landmine Overhead Press|other|w|fd|sd,tri|landmine ohp
Dumbbell Lateral Raise|db|w|sd||lat raise side raise
Cable Lateral Raise|cable|w|sd||
Machine Lateral Raise|machine|w|sd||
Dumbbell Front Raise|db|w|fd|sd|
Rear Delt Fly|db|w|rd|ub|reverse fly
Reverse Pec Deck|machine|w|rd|ub|rear delt machine
Cable Rear Delt Fly|cable|w|rd|ub|
Face Pull|cable|w|rd,ub|tr|
Dumbbell Shrug|db|w|tr|fa|
Barbell Shrug|bb|w|tr|fa|
# Back
Deadlift|bb|w|gl,ha,lb|qu,tr,la,fa|conventional dl
Deficit Deadlift|bb|w|gl,ha,lb,qu|tr,la,fa|
Sumo Deadlift|bb|w|gl,qu,ad|ha,lb,tr,fa|
Romanian Deadlift|bb|w|ha,gl|lb,fa|rdl
Dumbbell Romanian Deadlift|db|w|ha,gl|lb,fa|db rdl
Smith Machine Romanian Deadlift|smith|w|ha,gl|lb|smith rdl
Rack Pull|bb|w|ub,tr,lb,gl|ha,fa|
Good Morning|bb|w|ha,gl,lb||
Barbell Row|bb|w|la,ub|rd,bi,lb,fa|bent over row bb row
Pendlay Row|bb|w|la,ub|rd,bi,lb,fa|
Dumbbell Row|db|w|la,ub|rd,bi,fa|one arm row db row
Chest-Supported Dumbbell Row|db|w|ub,la|rd,bi|
Chest-Supported Machine Row|machine|w|ub,la|rd,bi|
Seated Cable Row|cable|w|la,ub|rd,bi|low row
Single-Arm Cable Row|cable|w|la,ub|rd,bi|
Smith Machine Row|smith|w|la,ub|rd,bi,lb|smith row
T-Bar Row|other|w|ub,la|rd,bi,lb|landmine row
Lat Pulldown|cable|w|la|bi,ub,rd|wide grip pulldown
Neutral-Grip Lat Pulldown|cable|w|la|bi,ub,rd|
Close-Grip Lat Pulldown|cable|w|la|bi,ub|
Single-Arm Lat Pulldown|cable|w|la|bi,ub|
Straight-Arm Pulldown|cable|w|la|tri|
Pull-Up|bw|b|la|bi,ub,rd,fa|pullup weighted
Chin-Up|bw|b|la,bi|ub,fa|chinup
Neutral-Grip Pull-Up|bw|b|la|bi,ub,fa|
Inverted Row|bw|b|ub,la|bi,rd|
Dumbbell Pullover|db|w|la,ch|tri|
Back Extension|bw|b|lb,gl|ha|45 roman chair hyperextension
Dead Hang|bw|t|fa,la||
# Biceps
Barbell Curl|bb|w|bi|fa|
EZ-Bar Curl|bb|w|bi|fa|ez curl
Dumbbell Curl|db|w|bi|fa|db curl
Incline Dumbbell Curl|db|w|bi|fa|
Hammer Curl|db|w|bi,fa||
Preacher Curl|bb|w|bi|fa|
Machine Preacher Curl|machine|w|bi|fa|
Dumbbell Preacher Curl|db|w|bi|fa|
Cable Curl|cable|w|bi|fa|
Bayesian Cable Curl|cable|w|bi|fa|
Reverse Curl|bb|w|fa|bi|
Wrist Curl|db|w|fa||
Reverse Wrist Curl|db|w|fa||
# Triceps
Cable Triceps Pushdown|cable|w|tri||rope pushdown
Straight-Bar Pushdown|cable|w|tri||
Overhead Cable Triceps Extension|cable|w|tri||
Skull Crusher|bb|w|tri||lying triceps extension
Dumbbell Overhead Triceps Extension|db|w|tri||
Triceps Kickback|db|w|tri||
Triceps Isometric Hold|other|t|tri||iso hold primer
# Quads
Barbell Back Squat|bb|w|qu,gl|ad,lb|high bar squat
Low Bar Squat|bb|w|qu,gl|ad,ha,lb|
Paused Squat|bb|w|qu,gl|ad,lb|pause squat
Front Squat|bb|w|qu|gl,ad,ab|
Belt Squat|machine|w|qu,gl|ad|
Smith Machine Squat|smith|w|qu,gl|ad|
Hack Squat|other|w|qu|gl|landmine hack squat
Leg Press|machine|w|qu,gl|ad|
Cable Leg Press|cable|w|qu,gl|ad|
Leg Extension|machine|w|qu|||
Goblet Squat|db|w|qu,gl|ad|
Bulgarian Split Squat|db|w|qu,gl|ad|bss split squat
Smith Machine Bulgarian Split Squat|smith|w|qu,gl|ad|
Walking Lunge|db|w|qu,gl|ad,ha|
Reverse Lunge|db|w|qu,gl|ad,ha|
Step-Up|db|w|qu,gl||
# Hamstrings / Glutes
Lying Leg Curl|machine|w|ha|ca|prone hamstring curl
Seated Leg Curl|machine|w|ha|ca|
Nordic Hamstring Curl|bw|b|ha|ca,gl|nordic
Glute-Ham Raise|bw|b|ha,gl|lb,ca|ghd ghr
Hip Thrust|bb|w|gl|ha,ad|barbell hip thrust
Smith Machine Hip Thrust|smith|w|gl|ha,ad|
Glute Bridge|bw|b|gl|ha|
Cable Pull-Through|cable|w|gl,ha|lb|
Cable Kickback|cable|w|gl||
Hip Abduction Machine|machine|w|gl||abductor
Hip Adduction Machine|machine|w|ad||adductor
Single-Leg Romanian Deadlift|db|w|ha,gl|lb,ad|single leg rdl
# Calves
Standing Calf Raise|smith|w|ca||smith calf raise
Seated Calf Raise|machine|w|ca||
Leg Press Calf Raise|machine|w|ca||
Single-Leg Calf Raise|db|w|ca||
# Core
Cable Crunch|cable|w|ab||
Hanging Leg Raise|bw|b|ab|fa|
Hanging Knee Raise|bw|b|ab|fa|
Ab Wheel Rollout|bw|b|ab|la,fd|
Decline Sit-Up|bw|b|ab||
Pallof Press|cable|w|ab||
Cable Woodchop|cable|w|ab||
Plank|bw|t|ab||
Side Plank|bw|t|ab||
Farmer's Carry|db|t|fa,tr|ab,gl,qu|farmers walk
`;

const TYPE: Record<string, ExType> = { w: 'weight', b: 'bodyweight', t: 'time' };
const m = (s: string): Muscle[] => (s ? (s.split(',') as Muscle[]) : []);
export const slug = (name: string): string =>
  name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

export interface LibraryEntry extends Exercise {
  /** Lower-case searchable text (name + aliases). */
  haystack: string;
}

function parse(): LibraryEntry[] {
  const out: LibraryEntry[] = [];
  for (const line of RAW.split('\n')) {
    if (!line.trim() || line.startsWith('#')) continue;
    const [name, equip, type, primary, secondary = '', alias = ''] = line.split('|');
    out.push({
      id: slug(name),
      name,
      equip: equip as Equip,
      type: TYPE[type],
      primary: m(primary),
      secondary: m(secondary),
      haystack: `${name} ${alias}`.toLowerCase(),
    });
  }
  return out;
}

export const LIBRARY: LibraryEntry[] = parse();
export const LIBRARY_BY_ID: ReadonlyMap<string, LibraryEntry> = new Map(LIBRARY.map((e) => [e.id, e]));
