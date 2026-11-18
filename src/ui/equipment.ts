import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { arrange, moveProgram, restoreOrder, toggleHidden } from '../domain/library';
import { fmtDur, fmtRpm, programsForEquipment, resolveProgram, targetWatts, totalSeconds, workSeconds, type Program } from '../domain/programs';
import { allMeta } from '../store/db';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type WorkoutMeta } from '../types';
import { ICON, esc } from './dom';
import { equipmentProgramCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';
import { openStepSheet } from './program';

function programCard(eq: 'airbike' | 'spin', raw: Program, today: boolean, ftp: number, gated: boolean, todayPick?: string, last?: WorkoutMeta): string {
  const p = resolveProgram(raw, ftp);
  const total = totalSeconds(p);
  const work = workSeconds(p);
  const chips: string[] = [];
  const w = p.segments.find((g) => g.kind === 'work' || g.kind === 'steady');
  if (w?.watts != null) {
    chips.push(`${targetWatts(w)} W${w.rpm != null ? ` · ~${fmtRpm(w.rpm)} RPM` : ''}`);
  } else if (w?.rpm != null) {
    chips.push(`${fmtRpm(w.rpm)}${w.allOut ? '+' : ''} RPM${w.allOut ? '' : ` · ${targetWatts(w)} W`}`);
  }
  if (work && p.zone === 'Z5') chips.push(`${fmtDur(work)} Z5`);
  if (total) chips.push(fmtDur(total));
  if (gated && p.segments.some((g) => g.gate)) chips.push('HR-gated rest');
  if (p.goal) chips.push(`${p.goal.def} ${p.goal.unit}`);
  if (last && last.avgW > 0) chips.push(`Last: ${last.avgW} W${last.maxHr ? ` · ${last.maxHr} bpm` : ''}`);
  const q = todayPick ? `?pick=${encodeURIComponent(todayPick)}` : '';
  return `<a class="prog ${p.group} ${today ? 'today' : ''}" data-id="${p.id}" href="#/${eq}/p/${p.id}${q}">
    <span class="prog-h"><b>${esc(p.name)}</b>${today ? '<em>TODAY</em>' : ''}</span>
    <span class="prog-b">${esc(p.blurb)}</span>
    <span class="prog-c">${chips.map((c) => `<i>${esc(c)}</i>`).join('')}</span>
  </a>`;
}

/** Reorder mode: no navigation, just big explicit controls. */
function editRow(p: Program, i: number, n: number): string {
  return `<div class="prog-edit ${p.group}" data-id="${p.id}">
    <b>${esc(p.name)}</b>
    <span class="ed-btns">
      <button data-act="top" aria-label="Pin to top" ${i === 0 ? 'disabled' : ''}>★</button>
      <button data-act="up" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
      <button data-act="down" aria-label="Move down" ${i === n - 1 ? 'disabled' : ''}>↓</button>
      <button data-act="hide" class="ed-hide" aria-label="Hide">Hide</button>
    </span>
  </div>`;
}

function hiddenRow(p: Program): string {
  return `<div class="prog-hid ${p.group}"><span><b>${esc(p.name)}</b><small>${esc(p.blurb)}</small></span><button class="btn sm" data-restore="${p.id}">Show</button></div>`;
}

function bikeEquipmentScreen(root: HTMLElement, eq: 'airbike' | 'spin'): () => void {
  ensureSensors({ bike: eq });
  void calendar.refresh();
  let editing = false;
  let rides: WorkoutMeta[] = [];
  const lib = programsForEquipment(eq);
  const orderKey = eq === 'spin' ? 'spinProgramOrder' : 'programOrder';
  const hiddenKey = eq === 'spin' ? 'spinProgramHidden' : 'programHidden';
  const freeId = eq === 'spin' ? 'spin-free' : 'free';

  const paint = () => {
    const st = getSettings();
    const arranged = arrange(lib, st[orderKey], st[hiddenKey]);
    const todays = calendar.forEquipment(eq);
    const todaySlotById = new Map(todays.filter((t) => t.programId).map((t) => [t.programId!, t.planSlot]));
    const lastByProg = new Map<string, WorkoutMeta>();
    for (const r of rides) if (r.programId && !lastByProg.has(r.programId)) lastByProg.set(r.programId, r);
    const gated = st.recoveryMode === 'gated';
    const n = arranged.visible.length;
    const list = editing
      ? arranged.visible.map((p, i) => editRow(p, i, n)).join('')
      : arranged.visible.map((p) => {
          const pick = todaySlotById.get(p.id);
          return programCard(eq, p, !!pick, st.ftp, gated, pick, lastByProg.get(p.id));
        }).join('');
    const hid = arranged.hidden.length
      ? `<details class="hid" ${editing ? 'open' : ''}><summary>HIDDEN · ${arranged.hidden.length}</summary>${arranged.hidden.map(hiddenRow).join('')}</details>`
      : '';
    const recentHtml = !editing && rides.length
      ? `<h2 class="sec">RECENT RIDES</h2><section class="card cal">${rides.slice(0, 6).map((m) => {
          const dStr = new Date(m.startEpochMs).toLocaleDateString([], { month: 'short', day: 'numeric' });
          const sub = [dStr, `${Math.max(1, Math.round(m.seconds / 60))} min`, m.avgW > 0 ? `${m.avgW} W` : '', m.avgHr > 0 ? `${m.avgHr} bpm` : ''].filter(Boolean).join(' · ');
          return `<div class="cal-item"><a class="cal-link" href="#/${eq}/w/${m.id}"><span class="cal-ico ${eq}">${ICON[eq]}</span><span class="cal-txt"><b>${esc(m.name)}</b><small>${esc(sub)}</small></span><span class="chev">›</span></a></div>`;
        }).join('')}</section>`
      : '';
    root.innerHTML = `${topbar({ back: '#/', title: EQUIPMENT_LABEL[eq], bike: true, cog: true })}
      <main class="page">
        ${equipmentProgramCard(eq, todays)}
        <div class="sec-row"><h2 class="sec">PROGRAMS</h2><button class="btn sm ${editing ? 'primary' : ''}" id="reorder">${editing ? 'Done' : 'Reorder'}</button></div>
        <div class="progs">${list}</div>
        ${editing ? '' : `<a class="free" href="#/${eq}/p/${freeId}"><span>${ICON[eq]}</span><b>Free Ride</b><small>${eq === 'spin' ? 'Manual resistance · ride &amp; record' : 'Just ride &amp; record'}</small><i>›</i></a>`}
        ${recentHtml}
        ${hid}
        ${revFooter()}
      </main>`;
  };
  paint();
  let dead = false;
  void allMeta().then((ms) => {
    if (dead) return;
    rides = ms.filter((m) => m.status === 'saved' && m.equipment === eq).sort((a, b) => b.startEpochMs - a.startEpochMs);
    paint();
  });

  const click = (e: Event) => {
    const t = e.target as HTMLElement;
    const peek = t.closest<HTMLElement>('[data-step-peek]');
    if (peek) { void openStepSheet(Number(peek.dataset.stepPeek)); return; }
    if (t.closest('#reorder')) {
      editing = !editing;
      paint();
      return;
    }
    const act = t.closest<HTMLElement>('[data-act]');
    if (act) {
      const id = act.closest<HTMLElement>('[data-id]')!.dataset.id!;
      const st = getSettings();
      const a = arrange(lib, st[orderKey], st[hiddenKey]);
      const kind = act.dataset.act!;
      if (kind === 'top' || kind === 'up' || kind === 'down') {
        updateSettings({ [orderKey]: moveProgram(a, id, kind), [hiddenKey]: st[hiddenKey] ?? a.hidden.map((p) => p.id) });
      } else if (kind === 'hide') {
        updateSettings({ [hiddenKey]: toggleHidden(a, id, true), [orderKey]: st[orderKey].length ? st[orderKey] : a.visible.map((p) => p.id) });
      }
      return;
    }
    const restore = t.closest<HTMLElement>('[data-restore]');
    if (restore) {
      const id = restore.dataset.restore!;
      const st = getSettings();
      const a = arrange(lib, st[orderKey], st[hiddenKey]);
      updateSettings({ [hiddenKey]: toggleHidden(a, id, false), [orderKey]: restoreOrder(a, id) });
    }
  };
  root.addEventListener('click', click);

  const cleanup = wireChips(root);
  const offCal = wireCalendar(root, paint);
  const offSet = settingsChanged.on(paint);
  return () => {
    dead = true;
    root.removeEventListener('click', click);
    cleanup();
    offCal();
    offSet();
  };
}

export const airbikeScreen = (root: HTMLElement): (() => void) => bikeEquipmentScreen(root, 'airbike');
export const spinScreen = (root: HTMLElement): (() => void) => bikeEquipmentScreen(root, 'spin');

const NOTES: Record<'treadmill', { head: string; body: string }> = {
  treadmill: {
    head: 'Treadmill is coming',
    body: 'Waiting on the Horizon T101 pod (roller + tilt sensor). Programs, HUD and upload will mirror the AirBike tab.',
  },
};

export function stubScreen(eq: 'treadmill') {
  return (root: HTMLElement): (() => void) => {
    ensureSensors({ bike: false });
    void calendar.refresh();
    const n = NOTES[eq];
    const paint = () => {
      root.innerHTML = `${topbar({ back: '#/', title: EQUIPMENT_LABEL[eq], cog: true })}
        <main class="page">
          ${equipmentProgramCard(eq, calendar.forEquipment(eq))}
          <section class="card soon"><span class="soon-ico ${eq}">${ICON[eq]}</span><h3>${n.head}</h3><p>${n.body}</p>
</section>
          ${revFooter()}
        </main>`;
    };
    paint();
    const click = (e: Event) => {
      const peek = (e.target as HTMLElement).closest<HTMLElement>('[data-step-peek]');
      if (peek) void openStepSheet(Number(peek.dataset.stepPeek));
    };
    root.addEventListener('click', click);
    const cleanup = wireChips(root);
    const offCal = wireCalendar(root, paint);
    const offSet = settingsChanged.on(paint);
    return () => { root.removeEventListener('click', click); cleanup(); offCal(); offSet(); };
  };
}
