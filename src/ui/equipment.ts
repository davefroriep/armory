import { ensureSensors, releaseBike } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { PROGRAMS, fmtDur, fmtRpm, resolveProgram, targetWatts, totalSeconds, workSeconds, type Program, type ProgramGroup } from '../domain/programs';
import { getSettings, settingsChanged } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { ICON, esc } from './dom';
import { calendarCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';

// Zone 2 and Free Ride are the least-used on the AirBike, so they sit at the bottom.
const GROUPS: { id: ProgramGroup; title: string }[] = [
  { id: 'z5', title: 'ZONE 5 · VO2' },
  { id: 'hiit', title: 'INTERVALS' },
  { id: 'goal', title: 'GOALS' },
  { id: 'z2', title: 'ZONE 2' },
];

function programCard(raw: Program, today: boolean, ftp: number): string {
  const p = resolveProgram(raw, ftp);
  const total = totalSeconds(p);
  const work = workSeconds(p);
  const chips: string[] = [];
  const w = p.segments.find((g) => g.kind === 'work' || g.kind === 'steady');
  if (w?.rpm != null) chips.push(`${fmtRpm(w.rpm)}${w.allOut ? '+' : ''} RPM${w.allOut ? '' : ` · ${targetWatts(w)} W`}`);
  if (work && p.zone === 'Z5') chips.push(`${fmtDur(work)} Z5`);
  if (total) chips.push(fmtDur(total));
  if (p.goal) chips.push(`${p.goal.def} ${p.goal.unit}`);
  return `<a class="prog ${p.group} ${today ? 'today' : ''}" href="#/airbike/p/${p.id}">
    <span class="prog-h"><b>${esc(p.name)}</b>${today ? '<em>TODAY</em>' : ''}</span>
    <span class="prog-b">${esc(p.blurb)}</span>
    <span class="prog-c">${chips.map((c) => `<i>${esc(c)}</i>`).join('')}</span>
  </a>`;
}

export function airbikeScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: true });
  void calendar.refresh();

  const paint = () => {
    const ftp = getSettings().ftp;
    const todays = calendar.forEquipment('airbike');
    const todayIds = new Set(todays.map((t) => t.match.programId).filter(Boolean));
    const groups = GROUPS.map(
      (g) => `<h2 class="sec">${g.title}</h2><div class="progs">${PROGRAMS.filter((p) => p.group === g.id)
        .map((p) => programCard(p, todayIds.has(p.id), ftp)).join('')}</div>`,
    ).join('');
    root.innerHTML = `${topbar({ back: '#/', title: 'AirBike', bike: true, cog: true })}
      <main class="page">
        ${calendarCard(todays, false)}
        ${groups}
        <a class="free" href="#/airbike/p/free"><span>${ICON.airbike}</span><b>Free Ride</b><small>Just ride &amp; record</small><i>›</i></a>
        ${revFooter()}
      </main>`;
  };
  paint();
  const cleanup = wireChips(root);
  const offCal = wireCalendar(root, paint);
  const offSet = settingsChanged.on(paint);
  return () => {
    cleanup();
    offCal();
    offSet();
    releaseBike();
  };
}

const NOTES: Record<Exclude<Equipment, 'airbike' | 'strength'>, { head: string; body: string }> = {
  spin: {
    head: 'Spin Bike is resting',
    body: 'The Bowflex C7 + SS2K still needs calibration, so ERG programs would be inaccurate. We’ll build this tab (same programs, ERG watts instead of RPM) once calibration is done.',
  },
  treadmill: {
    head: 'Treadmill is coming',
    body: 'Waiting on the Horizon T101 pod (roller + tilt sensor). Programs, HUD and upload will mirror the AirBike tab.',
  },
};

export function stubScreen(eq: Exclude<Equipment, 'airbike' | 'strength'>) {
  return (root: HTMLElement): (() => void) => {
    ensureSensors({ bike: false });
    void calendar.refresh();
    const n = NOTES[eq];
    const paint = () => {
      root.innerHTML = `${topbar({ back: '#/', title: EQUIPMENT_LABEL[eq], cog: true })}
        <main class="page">
          ${calendarCard(calendar.forEquipment(eq), false)}
          <section class="card soon"><span class="soon-ico ${eq}">${ICON[eq]}</span><h3>${n.head}</h3><p>${n.body}</p>
</section>
          ${revFooter()}
        </main>`;
    };
    paint();
    const cleanup = wireChips(root);
    const offCal = wireCalendar(root, paint);
    return () => { cleanup(); offCal(); };
  };
}
