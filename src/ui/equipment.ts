import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { arrange, moveProgram, restoreOrder, toggleHidden } from '../domain/library';
import { PROGRAMS, fmtDur, fmtRpm, resolveProgram, targetWatts, totalSeconds, workSeconds, type Program } from '../domain/programs';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type Equipment } from '../types';
import { ICON, esc } from './dom';
import { calendarCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';

const HOLD_MS = 520;
const MOVE_TOLERANCE_PX = 10;

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

function hiddenRow(p: Program): string {
  return `<div class="prog-hid ${p.group}"><span><b>${esc(p.name)}</b><small>${esc(p.blurb)}</small></span><button class="btn sm" data-restore="${p.id}">Show</button></div>`;
}

export function airbikeScreen(root: HTMLElement): () => void {
  ensureSensors({ bike: true });
  void calendar.refresh();

  const paint = () => {
    const st = getSettings();
    const arranged = arrange(PROGRAMS, st.programOrder, st.programHidden);
    const todays = calendar.forEquipment('airbike');
    const todayIds = new Set(todays.map((t) => t.match.programId).filter(Boolean));
    const gated = st.recoveryMode === 'gated';
    const list = arranged.visible.map((p) => programCard(p, todayIds.has(p.id), st.ftp, gated)).join('');
    const hid = arranged.hidden.length
      ? `<details class="hid"><summary>HIDDEN · ${arranged.hidden.length}</summary>${arranged.hidden.map(hiddenRow).join('')}</details>`
      : '';
    root.innerHTML = `${topbar({ back: '#/', title: 'AirBike', bike: true, cog: true })}
      <main class="page">
        ${calendarCard(todays, false)}
        <h2 class="sec">PROGRAMS · HOLD ONE TO REORDER OR HIDE</h2>
        <div class="progs">${list}</div>
        <a class="free" href="#/airbike/p/free"><span>${ICON.airbike}</span><b>Free Ride</b><small>Just ride &amp; record</small><i>›</i></a>
        ${hid}
        ${revFooter()}
      </main>
      <div class="modal" id="progSheet" hidden></div>`;
  };
  paint();

  const sheet = () => root.querySelector<HTMLElement>('#progSheet')!;
  const closeSheet = () => { sheet().hidden = true; sheet().innerHTML = ''; };
  const openSheet = (id: string) => {
    const st = getSettings();
    const a = arrange(PROGRAMS, st.programOrder, st.programHidden);
    const i = a.visible.findIndex((p) => p.id === id);
    const p = a.visible[i];
    if (!p) return;
    sheet().innerHTML = `<div class="sheet" data-id="${id}"><h2>${esc(p.name)}</h2>
      <button class="btn big" data-act="top" ${i === 0 ? 'disabled' : ''}>★ Pin to top</button>
      <button class="btn big" data-act="up" ${i === 0 ? 'disabled' : ''}>↑ Move up</button>
      <button class="btn big" data-act="down" ${i === a.visible.length - 1 ? 'disabled' : ''}>↓ Move down</button>
      <button class="btn big danger" data-act="hide">Hide from list</button>
      <button class="btn big" data-act="close">Done</button></div>`;
    sheet().hidden = false;
  };

  // Long press = arrange. The click that follows a long press must not open the program.
  let timer: ReturnType<typeof setTimeout> | null = null;
  let held = false;
  let x0 = 0;
  let y0 = 0;
  const cancel = () => { if (timer) clearTimeout(timer); timer = null; };
  const down = (e: PointerEvent) => {
    const card = (e.target as HTMLElement).closest<HTMLElement>('a.prog');
    if (!card) return;
    held = false;
    x0 = e.clientX;
    y0 = e.clientY;
    cancel();
    timer = setTimeout(() => {
      held = true;
      timer = null;
      navigator.vibrate?.(30);
      openSheet(card.dataset.id!);
    }, HOLD_MS);
  };
  const move = (e: PointerEvent) => {
    if (timer && Math.hypot(e.clientX - x0, e.clientY - y0) > MOVE_TOLERANCE_PX) cancel();
  };
  const click = (e: Event) => {
    const t = e.target as HTMLElement;
    if (held && t.closest('a.prog')) {
      e.preventDefault();
      e.stopPropagation();
      held = false;
      return;
    }
    const act = t.closest<HTMLElement>('[data-act]');
    if (act) {
      const id = act.closest<HTMLElement>('.sheet')!.dataset.id!;
      const st = getSettings();
      const a = arrange(PROGRAMS, st.programOrder, st.programHidden);
      const kind = act.dataset.act!;
      if (kind === 'top' || kind === 'up' || kind === 'down') updateSettings({ programOrder: moveProgram(a, id, kind), programHidden: st.programHidden ?? a.hidden.map((p) => p.id) });
      else if (kind === 'hide') updateSettings({ programHidden: toggleHidden(a, id, true), programOrder: st.programOrder.length ? st.programOrder : a.visible.map((p) => p.id) });
      closeSheet();
      return;
    }
    const restore = t.closest<HTMLElement>('[data-restore]');
    if (restore) {
      const id = restore.dataset.restore!;
      const st = getSettings();
      const a = arrange(PROGRAMS, st.programOrder, st.programHidden);
      updateSettings({ programHidden: toggleHidden(a, id, false), programOrder: restoreOrder(a, id) });
      return;
    }
    if (t.id === 'progSheet') closeSheet();
  };
  root.addEventListener('pointerdown', down);
  root.addEventListener('pointermove', move);
  root.addEventListener('pointerup', cancel);
  root.addEventListener('pointercancel', cancel);
  root.addEventListener('contextmenu', (e) => { if ((e.target as HTMLElement).closest('a.prog')) e.preventDefault(); });
  root.addEventListener('click', click, true);

  const cleanup = wireChips(root);
  const offCal = wireCalendar(root, paint);
  const offSet = settingsChanged.on(paint);
  return () => {
    cancel();
    root.removeEventListener('pointerdown', down);
    root.removeEventListener('pointermove', move);
    root.removeEventListener('pointerup', cancel);
    root.removeEventListener('pointercancel', cancel);
    root.removeEventListener('click', click, true);
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
