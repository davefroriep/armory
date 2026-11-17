import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { arrange, moveProgram, restoreOrder, toggleHidden } from '../domain/library';
import { PROGRAMS, fmtDur, fmtRpm, resolveProgram, targetWatts, totalSeconds, workSeconds, type Program } from '../domain/programs';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { ICON, esc } from './dom';
import { equipmentProgramCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';
import { openStepSheet } from './program';

function programCard(raw: Program, today: boolean, ftp: number, gated: boolean): string {
  const p = resolveProgram(raw, ftp);
  const total = totalSeconds(p);
  const work = workSeconds(p);
  const chips: string[] = [];
  const w = p.segments.find((g) => g.kind === 'work' || g.kind === 'steady');
  if (w?.rpm != null) chips.push(`${fmtRpm(w.rpm)}${w.allOut ? '+' : ''} RPM${w.allOut ? '' : ` · ${targetWatts(w)} W`}`);
  if (work && p.zone === 'Z5') chips.push(`${fmtDur(work)} Z5`);
  if (total) chips.push(fmtDur(total));
  if (gated && p.segments.some((g) => g.gate)) chips.push('HR-gated rest');
  if (p.goal) chips.push(`${p.goal.def} ${p.goal.unit}`);
  return `<a class="prog ${p.group} ${today ? 'today' : ''}" data-id="${p.id}" href="#/airbike/p/${p.id}">
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

export function airbikeScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: true });
  void calendar.refresh();
  let editing = false;

  const paint = () => {
    const st = getSettings();
    const arranged = arrange(PROGRAMS, st.programOrder, st.programHidden);
    const todays = calendar.forEquipment('airbike');
    const todayIds = new Set(todays.map((t) => t.match.programId).filter(Boolean));
    const gated = st.recoveryMode === 'gated';
    const n = arranged.visible.length;
    const list = editing
      ? arranged.visible.map((p, i) => editRow(p, i, n)).join('')
      : arranged.visible.map((p) => programCard(p, todayIds.has(p.id), st.ftp, gated)).join('');
    const hid = arranged.hidden.length
      ? `<details class="hid" ${editing ? 'open' : ''}><summary>HIDDEN · ${arranged.hidden.length}</summary>${arranged.hidden.map(hiddenRow).join('')}</details>`
      : '';
    root.innerHTML = `${topbar({ back: '#/', title: 'AirBike', bike: true, cog: true })}
      <main class="page">
        ${equipmentProgramCard('airbike', todays)}
        <div class="sec-row"><h2 class="sec">PROGRAMS</h2><button class="btn sm ${editing ? 'primary' : ''}" id="reorder">${editing ? 'Done' : 'Reorder'}</button></div>
        <div class="progs">${list}</div>
        ${editing ? '' : `<a class="free" href="#/airbike/p/free"><span>${ICON.airbike}</span><b>Free Ride</b><small>Just ride &amp; record</small><i>›</i></a>`}
        ${hid}
        ${revFooter()}
      </main>`;
  };
  paint();

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
      const a = arrange(PROGRAMS, st.programOrder, st.programHidden);
      const kind = act.dataset.act!;
      if (kind === 'top' || kind === 'up' || kind === 'down') {
        updateSettings({ programOrder: moveProgram(a, id, kind), programHidden: st.programHidden ?? a.hidden.map((p) => p.id) });
      } else if (kind === 'hide') {
        updateSettings({ programHidden: toggleHidden(a, id, true), programOrder: st.programOrder.length ? st.programOrder : a.visible.map((p) => p.id) });
      }
      return;
    }
    const restore = t.closest<HTMLElement>('[data-restore]');
    if (restore) {
      const id = restore.dataset.restore!;
      const st = getSettings();
      const a = arrange(PROGRAMS, st.programOrder, st.programHidden);
      updateSettings({ programHidden: toggleHidden(a, id, false), programOrder: restoreOrder(a, id) });
    }
  };
  root.addEventListener('click', click);

  const cleanup = wireChips(root);
  const offCal = wireCalendar(root, paint);
  const offSet = settingsChanged.on(paint);
  return () => {
    root.removeEventListener('click', click);
    cleanup();
    offCal();
    offSet();
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
