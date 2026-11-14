// Official Assault AirBike Elite RPM -> Watts curve (same anchors as the firmware's airbike_math.h
// and the AssaultBike console page). Linear between anchors, cubic extrapolation outside.
export const RPM_WATTS: ReadonlyArray<readonly [number, number]> = [
  [15, 13], [20, 21], [25, 33], [30, 50], [35, 71], [40, 99], [45, 134], [50, 176], [55, 228],
  [60, 288], [65, 359], [70, 442], [75, 536], [80, 644], [85, 763], [90, 896], [95, 1043], [100, 1205],
];

const FIRST = RPM_WATTS[0];
const LAST = RPM_WATTS[RPM_WATTS.length - 1];

export function wattsForRpm(rpm: number): number {
  if (rpm <= 0) return 0;
  if (rpm < FIRST[0]) return FIRST[1] * Math.pow(rpm / FIRST[0], 3);
  if (rpm >= LAST[0]) return LAST[1] * Math.pow(rpm / LAST[0], 3);
  for (let i = 1; i < RPM_WATTS.length; i++) {
    const [r1, w1] = RPM_WATTS[i];
    if (rpm <= r1) {
      const [r0, w0] = RPM_WATTS[i - 1];
      return w0 + ((rpm - r0) / (r1 - r0)) * (w1 - w0);
    }
  }
  return LAST[1];
}

export function rpmForWatts(watts: number): number {
  if (watts <= 0) return 0;
  if (watts < FIRST[1]) return FIRST[0] * Math.cbrt(watts / FIRST[1]);
  if (watts >= LAST[1]) return LAST[0] * Math.cbrt(watts / LAST[1]);
  for (let i = 1; i < RPM_WATTS.length; i++) {
    const [r1, w1] = RPM_WATTS[i];
    if (watts <= w1) {
      const [r0, w0] = RPM_WATTS[i - 1];
      return r0 + ((watts - w0) / (w1 - w0)) * (r1 - r0);
    }
  }
  return LAST[0];
}

/** One metronome beat per leg stroke: BPM = 2 x pedal RPM. */
export const bpmForRpm = (rpm: number): number => rpm * 2;
