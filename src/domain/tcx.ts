export interface TcxSample {
  /** Active seconds since workout start. */
  t: number;
  w: number;
  cad: number;
  hr: number;
  /** Cumulative metres. */
  dist: number;
}

export interface TcxInput {
  startEpochMs: number;
  seconds: number;
  /** Mechanical work in kJ. Exported as Calories (1 kJ = 1 kcal, matching Bowflex / Intervals). */
  kj: number;
  /** Energy in kcal when it is not mechanical work (strength: HR-based estimate). Overrides `kj`. */
  kcal?: number;
  samples: TcxSample[];
  /** 'bike' = power/cadence/distance; 'hr' = heart-rate-only session (strength). */
  kind?: 'bike' | 'hr';
}

const esc = (s: string) => s.replace(/[<>&]/g, (c) => (c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&amp;'));

export function buildTcx(input: TcxInput, name: string): string {
  const start = new Date(input.startEpochMs).toISOString();
  const pts = input.samples
    .map((p) => {
      const time = new Date(input.startEpochMs + p.t * 1000).toISOString();
      const hr = p.hr > 30 ? `<HeartRateBpm><Value>${Math.round(p.hr)}</Value></HeartRateBpm>` : '';
      if (input.kind === 'hr') return `<Trackpoint><Time>${time}</Time>${hr}</Trackpoint>`;
      return (
        `<Trackpoint><Time>${time}</Time><DistanceMeters>${p.dist.toFixed(1)}</DistanceMeters>${hr}` +
        `<Cadence>${Math.round(p.cad)}</Cadence>` +
        `<Extensions><ns3:TPX><ns3:Watts>${Math.round(p.w)}</ns3:Watts></ns3:TPX></Extensions></Trackpoint>`
      );
    })
    .join('');
  const dist = input.samples.length ? input.samples[input.samples.length - 1].dist : 0;
  return `<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
<Activities><Activity Sport="${input.kind === 'hr' ? 'Other' : 'Biking'}"><Id>${start}</Id><Notes>${esc(name)}</Notes>
<Lap StartTime="${start}"><TotalTimeSeconds>${input.seconds}</TotalTimeSeconds>${input.kind === 'hr' ? '' : `<DistanceMeters>${dist.toFixed(1)}</DistanceMeters>`}<Calories>${Math.round(input.kcal ?? input.kj)}</Calories><Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod>
<Track>${pts}</Track></Lap></Activity></Activities>
</TrainingCenterDatabase>`;
}
