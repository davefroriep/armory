import { ensureSensors } from '../ble/sources';
import { calendar } from '../intervals/calendar';
import { wattsForRpm } from '../domain/airbikeCurve';
import { arrange, moveProgram, restoreOrder, toggleHidden } from '../domain/library';
import {
  applyProgramTarget, clampProgramTarget, defaultProgramTarget, effectiveProgramTarget,
  fmtDur, fmtRpm, formatProgramTarget, programsForEquipment, targetWatts, totalSeconds, workSeconds,
  type Program,
} from '../domain/programs';
import { allMeta } from '../store/db';
import { getSettings, settingsChanged, updateSettings } from '../store/settings';
import { EQUIPMENT_LABEL, type WorkoutMeta } from '../types';
import { ICON, esc, syncModalInert, wireModalBackdrop } from './dom';
import { equipmentProgramCard, revFooter, topbar, wireCalendar, wireChips } from './chrome';
import { openStepSheet } from './program';

function programCard(
  eq: 'airbike' | 'spin',
  raw: Program,
  today: boolean,
  storedTargets: Record<string, number>,
  gated: boolean,
  guest: boolean,
  todayPick?: string,
  last?: WorkoutMeta,
): string {
  const p = applyProgramTarget(raw, effectiveProgramTarget(raw, storedTargets));
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
  if (!guest && gated && p.segments.some((g) => g.gate)) chips.push('HR-gated rest');
  if (p.goal) chips.push(`${p.goal.def} ${p.goal.unit}`);
  if (!guest && last && last.avgW > 0) chips.push(`Last: ${last.avgW} W${last.maxHr ? ` · ${last.maxHr} bpm` : ''}`);

  if (guest && defaultProgramTarget(raw) != null) {
    return `<button type="button" class="prog ${p.group}" data-guest-prog="${p.id}">
      <span class="prog-h"><b>${esc(p.name)}</b></span>
      <span class="prog-b">${esc(p.blurb)}</span>
      <span class="prog-c">${chips.map((c) => `<i>${esc(c)}</i>`).join('')}</span>
    </button>`;
  }

  const params = new URLSearchParams();
  if (guest) params.set('guest', '1');
  else if (todayPick) params.set('pick', todayPick);
  const qs = params.toString();
  const q = qs ? `?${qs}` : '';
  return `<a class="prog ${p.group} ${!guest && today ? 'today' : ''}" data-id="${p.id}" href="#/${eq}/p/${p.id}${q}">
    <span class="prog-h"><b>${esc(p.name)}</b>${!guest && today ? '<em>TODAY</em>' : ''}</span>
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
  let guest = new URLSearchParams(location.hash.split('?')[1] ?? '').get('guest') === '1';
  let rides: WorkoutMeta[] = [];
  let closeGuestModal: (() => void) | null = null;
  const lib = programsForEquipment(eq);
  const orderKey = eq === 'spin' ? 'spinProgramOrder' : 'programOrder';
  const hiddenKey = eq === 'spin' ? 'spinProgramHidden' : 'programHidden';
  const freeId = eq === 'spin' ? 'spin-free' : 'free';

  const openGuestStartSheet = (raw: Program) => {
    const st = getSettings();
    const baseTarget = effectiveProgramTarget(raw, st.programTargets);
    if (baseTarget == null) {
      location.hash = `#/${eq}/p/${raw.id}?guest=1`;
      return;
    }
    closeGuestModal?.();
    const isSpin = eq === 'spin';
    const refSeg = raw.segments.find((g) => (g.kind === 'work' || g.kind === 'steady') && !g.allOut);
    let val = baseTarget;
    const minVal = isSpin ? 50 : 30;
    const maxVal = isSpin ? 500 : 80;
    const steps = isSpin ? [-25, -5, 5, 25] : [-5, -1, 1, 5];

    const el = document.createElement('div');
    el.className = 'modal';
    el.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">
      <h2>Guest · ${esc(raw.name)}</h2>
      <p class="guest-ref">Starting at Dave's target: <b>${esc(formatProgramTarget(raw, baseTarget))}</b></p>
      <div class="guest-tgt-val" id="guestTgtVal"></div>
      <div class="guest-tgt-btns">
        ${steps.map((d) => `<button type="button" data-gstep="${d}">${d > 0 ? `+${d}` : d}</button>`).join('')}
      </div>
      <button class="btn sm" type="button" id="guestReset">Reset to ${baseTarget} ${isSpin ? 'W' : 'RPM'}</button>
      <p class="hint">Fixed protocol timers · Not saved or uploaded to Intervals.icu</p>
      <button class="btn primary big" type="button" id="guestStart">Start Guest Workout</button>
      <button class="btn big" type="button" id="guestCancel">Cancel</button>
    </div>`;

    const paintModal = () => {
      const valEl = el.querySelector<HTMLElement>('#guestTgtVal')!;
      valEl.innerHTML = isSpin
        ? `<b>${val} W</b><small>~${fmtRpm(refSeg?.rpm ?? 85)} RPM</small>`
        : `<b>${val} RPM</b><small>${Math.round(wattsForRpm(val))} W</small>`;
      const rst = el.querySelector<HTMLButtonElement>('#guestReset')!;
      rst.disabled = val === baseTarget;
    };
    paintModal();

    let closed = false;
    let offBackdrop = () => {};
    const close = (): void => {
      if (closed) return;
      closed = true;
      if (closeGuestModal === close) closeGuestModal = null;
      offBackdrop();
      el.remove();
      syncModalInert();
    };
    closeGuestModal = close;
    offBackdrop = wireModalBackdrop(el, close);

    el.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const stepBtn = t.closest<HTMLElement>('[data-gstep]');
      if (stepBtn) {
        val = Math.max(minVal, Math.min(maxVal, clampProgramTarget(raw, val + Number(stepBtn.dataset.gstep))));
        paintModal();
        return;
      }
      if (t.closest('#guestReset')) {
        val = baseTarget;
        paintModal();
        return;
      }
      if (t.closest('#guestStart')) {
        close();
        location.hash = `#/${eq}/p/${raw.id}?guest=1&target=${val}`;
        return;
      }
      if (t.closest('#guestCancel')) close();
    });

    document.body.appendChild(el);
    syncModalInert();
  };

  const paint = () => {
    const st = getSettings();
    const arranged = arrange(lib, st[orderKey], st[hiddenKey]);
    const todays = calendar.forEquipment(eq);
    const todaySlotById = new Map(todays.filter((t) => t.programId).map((t) => [t.programId!, t.planSlot]));
    const lastByProg = new Map<string, WorkoutMeta>();
    for (const r of rides) if (r.programId && !lastByProg.has(r.programId)) lastByProg.set(r.programId, r);
    const gated = st.recoveryMode === 'gated';
    const n = arranged.visible.length;
    const list = editing && !guest
      ? arranged.visible.map((p, i) => editRow(p, i, n)).join('')
      : arranged.visible.map((p) => {
          const pick = todaySlotById.get(p.id);
          return programCard(eq, p, !!pick, st.programTargets, gated, guest, pick, lastByProg.get(p.id));
        }).join('');
    const hid = arranged.hidden.length
      ? `<details class="hid" ${editing ? 'open' : ''}><summary>HIDDEN · ${arranged.hidden.length}</summary>${arranged.hidden.map(hiddenRow).join('')}</details>`
      : '';
    const recentHtml = !editing && !guest && rides.length
      ? `<h2 class="sec">RECENT RIDES</h2><section class="card cal">${rides.slice(0, 6).map((m) => {
          const dStr = new Date(m.startEpochMs).toLocaleDateString([], { month: 'short', day: 'numeric' });
          const sub = [dStr, `${Math.max(1, Math.round(m.seconds / 60))} min`, m.avgW > 0 ? `${m.avgW} W` : '', m.avgHr > 0 ? `${m.avgHr} bpm` : ''].filter(Boolean).join(' · ');
          return `<div class="cal-item"><a class="cal-link" href="#/${eq}/w/${m.id}"><span class="cal-ico ${eq}">${ICON[eq]}</span><span class="cal-txt"><b>${esc(m.name)}</b><small>${esc(sub)}</small></span><span class="chev">›</span></a></div>`;
        }).join('')}</section>`
      : '';
    const guestBanner = guest
      ? `<section class="card guest-banner"><span><b>GUEST MODE · NO UPLOAD</b><small>Pick any workout to confirm a starting target. Uses fixed timers and skips Dave's log.</small></span><button class="btn sm" type="button" id="guestExit">Exit Guest</button></section>`
      : '';
    root.innerHTML = `${topbar({ back: '#/', title: EQUIPMENT_LABEL[eq], bike: true, cog: true })}
      <main class="page">
        ${guest ? guestBanner : equipmentProgramCard(eq, todays)}
        <div class="sec-row">
          <h2 class="sec">PROGRAMS</h2>
          <div class="sec-acts">
            <button class="btn sm ${guest ? 'guest-on' : ''}" type="button" id="guestToggle">${guest ? 'Guest ON' : 'Guest'}</button>
            ${guest ? '' : `<button class="btn sm ${editing ? 'primary' : ''}" type="button" id="reorder">${editing ? 'Done' : 'Reorder'}</button>`}
          </div>
        </div>
        <div class="progs">${list}</div>
        ${editing ? '' : `<a class="free" href="#/${eq}/p/${freeId}${guest ? '?guest=1' : ''}"><span>${ICON[eq]}</span><b>Free Ride</b><small>${eq === 'spin' ? 'Manual resistance · ride &amp; record' : 'Just ride &amp; record'}</small><i>›</i></a>`}
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
    if (t.closest('#guestToggle')) {
      guest = !guest;
      editing = false;
      paint();
      return;
    }
    if (t.closest('#guestExit')) {
      guest = false;
      paint();
      return;
    }
    const guestProg = t.closest<HTMLElement>('[data-guest-prog]');
    if (guestProg) {
      const prog = lib.find((p) => p.id === guestProg.dataset.guestProg);
      if (prog) openGuestStartSheet(prog);
      return;
    }
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
    closeGuestModal?.();
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
