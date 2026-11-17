import {
  PHASE1_PLAN, dayOfWeek, parseYmd, shiftYmd,
  type PlanSlot, type ProjectedStep,
} from '../domain/plan';
import { sessionStats, type ExOf, type PRHit } from '../lifts/calc';
import { labels } from '../lifts/logic';
import { matchRoutineByTitle } from '../lifts/seed';
import type { Unit } from '../lifts/settings';
import { condenseSets, fmtDuration, fmtVolume, sessionPRs, setText } from '../lifts/summary';
import type { ExRow, LiftSession, Routine, SessionEx } from '../lifts/types';
import { EQUIPMENT_LABEL, type Equipment, type WorkoutMeta } from '../types';
import { ICON, esc, hms } from './dom';

export interface TodayCompletedItem {
  equipment: Equipment;
  name: string;
  sub: string;
}

export interface DayLogs {
  lifts: LiftSession[];
  cardio: WorkoutMeta[];
}

export interface ZoomLookup {
  exOf: ExOf;
  rowOf: (exId: string) => ExRow | undefined;
  prevOf: (exId: string) => SessionEx | null;
  routines: Routine[];
  prs: Map<string, PRHit>;
  unit: Unit;
}

export interface ResolvedDayZoom {
  liftsDone: LiftSession[];
  cardioDone: WorkoutMeta[];
  externalDoneSlots: { p: ProjectedStep; slot: PlanSlot; slotIndex: number }[];
  pendingSlots: { p: ProjectedStep; slot: PlanSlot; slotIndex: number }[];
  associatedStep: ProjectedStep | undefined;
  canShift: boolean;
  week: number;
}

export function localYmdFromMs(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Groups finished lift sessions and saved cardio sessions by local YYYY-MM-DD date. */
export function groupDayLogs(
  liftSessions: LiftSession[],
  metas: WorkoutMeta[],
): { byDate: Map<string, DayLogs>; summaryByDate: Map<string, { equipment: Equipment; name: string }[]> } {
  const byDate = new Map<string, DayLogs>();
  const summaryByDate = new Map<string, { equipment: Equipment; name: string }[]>();

  const ensure = (ymd: string): DayLogs => {
    let entry = byDate.get(ymd);
    if (!entry) {
      entry = { lifts: [], cardio: [] };
      byDate.set(ymd, entry);
    }
    return entry;
  };
  const pushSum = (ymd: string, equipment: Equipment, name: string): void => {
    const arr = summaryByDate.get(ymd) ?? [];
    arr.push({ equipment, name });
    summaryByDate.set(ymd, arr);
  };

  for (const s of [...liftSessions].filter((x) => x.status === 'done').sort((a, b) => a.startMs - b.startMs)) {
    const ymd = localYmdFromMs(s.startMs);
    ensure(ymd).lifts.push(s);
    pushSum(ymd, 'strength', s.name);
  }
  for (const m of [...metas].filter((x) => x.status === 'saved' && x.equipment !== 'strength').sort((a, b) => a.startEpochMs - b.startEpochMs)) {
    const ymd = localYmdFromMs(m.startEpochMs);
    ensure(ymd).cardio.push(m);
    pushSum(ymd, m.equipment, m.name);
  }
  return { byDate, summaryByDate };
}

/** Compact summary rows for Today's hero card when sessions are already completed today. */
export function todayCompletedItems(
  dayLog: DayLogs | undefined,
  lookup: Pick<ZoomLookup, 'exOf' | 'prs' | 'unit'>,
): TodayCompletedItem[] {
  if (!dayLog) return [];
  const out: TodayCompletedItem[] = [];
  for (const s of dayLog.lifts) {
    const st = sessionStats(s, lookup.exOf);
    const pr = sessionPRs(s, lookup.prs);
    const sub = [
      'Strength',
      fmtDuration(st.durationSec),
      `${st.sets} sets`,
      fmtVolume(st.volumeKg, lookup.unit),
      pr.count ? `★ ${pr.count} PR${pr.count > 1 ? 's' : ''}` : '',
    ].filter(Boolean).join(' · ');
    out.push({ equipment: 'strength', name: s.name, sub });
  }
  for (const m of dayLog.cardio) {
    const sub = [
      EQUIPMENT_LABEL[m.equipment],
      `${Math.max(1, Math.round(m.seconds / 60))} min`,
      m.avgW > 0 ? `${m.avgW} W` : '',
      m.avgHr > 0 ? `${m.avgHr} bpm` : '',
    ].filter(Boolean).join(' · ');
    out.push({ equipment: m.equipment, name: m.name, sub });
  }
  return out;
}

/** Rich completed strength slot showing logged exercises, sets, weights/reps, notes and PRs. */
export function renderCompletedLiftSlot(
  s: LiftSession,
  lookup: Pick<ZoomLookup, 'exOf' | 'prs' | 'unit'>,
): string {
  const st = sessionStats(s, lookup.exOf);
  const pr = sessionPRs(s, lookup.prs);
  const sub = [
    'Strength',
    fmtDuration(st.durationSec),
    `${st.sets} sets`,
    fmtVolume(st.volumeKg, lookup.unit),
    pr.count ? `★ ${pr.count} PR${pr.count > 1 ? 's' : ''}` : '',
  ].filter(Boolean).join(' · ');
  const labs = labels(s.exs);
  const rows = s.exs.map((e, idx) => {
    const ex = lookup.exOf(e.exId);
    const summary = condenseSets(e, ex, lookup.unit);
    const hit = pr.byEx.get(e.uid);
    return `<div class="sh-ex">
      <span class="xlab">${labs[idx]}</span>
      <span class="sh-ex-m"><b>${esc(ex?.name ?? e.exId)}</b><small>${esc(summary || `${e.sets.length} sets`)}${e.note ? ` · ${esc(e.note)}` : ''}</small></span>
      ${hit ? `<em>★ ${hit.n}RM</em>` : ''}
    </div>`;
  }).join('');
  return `<div class="sh-slot done">
    <div class="sh-slot-h">
      <span class="cal-ico strength">${ICON.strength}</span>
      <span><b>${esc(s.name)}</b><small>${esc(sub)}</small></span>
      <span class="p-badge ok">✓ Done</span>
    </div>
    ${rows ? `<div class="sh-exlist">${rows}</div>` : ''}
    ${s.note ? `<p class="sh-text">${esc(s.note)}</p>` : ''}
    <div class="sh-subrow"><a class="link" href="#/strength/workout/${s.id}">Full workout &amp; edit ›</a></div>
  </div>`;
}

/** Rich completed cardio slot showing duration, average/max power, HR, energy and RPE. */
export function renderCompletedCardioSlot(m: WorkoutMeta): string {
  const mins = Math.max(1, Math.round(m.seconds / 60));
  const pills = [
    hms(m.seconds),
    m.avgW > 0 ? `${m.avgW} W avg${m.maxW > 0 ? ` · ${m.maxW} W max` : ''}` : '',
    m.avgHr > 0 ? `${m.avgHr} bpm avg${m.maxHr > 0 ? ` · ${m.maxHr} max` : ''}` : '',
    m.kj > 0 ? `${m.kj} kJ` : '',
    m.rpe != null ? `RPE ${m.rpe}` : '',
  ].filter(Boolean);
  return `<div class="sh-slot done">
    <div class="sh-slot-h">
      <span class="cal-ico ${m.equipment}">${ICON[m.equipment]}</span>
      <span><b>${esc(m.name)}</b><small>${EQUIPMENT_LABEL[m.equipment]} · ${mins} min</small></span>
      <span class="p-badge ok">✓ Done</span>
    </div>
    ${pills.length ? `<div class="dz-stats">${pills.map((x) => `<i>${esc(x)}</i>`).join('')}</div>` : ''}
    ${m.notes ? `<p class="sh-text">${esc(m.notes)}</p>` : ''}
  </div>`;
}

/** Planned (or externally completed) slot view with matched local routine exercises/sets/notes. */
export function renderPlannedSlot(slot: PlanSlot, done: boolean, lookup: ZoomLookup): string {
  const mins = Math.round(slot.seconds / 60);
  if (slot.equipment === 'strength') {
    const r = matchRoutineByTitle(slot.name, lookup.routines);
    let detail = `<p class="sh-text">${esc(slot.description)}</p>`;
    if (r) {
      const labs = labels(r.items);
      const rows = r.items.map((it, idx) => {
        const ex = lookup.exOf(it.exId);
        const note = lookup.rowOf(it.exId)?.note;
        const prev = lookup.prevOf(it.exId);
        const best = prev?.sets.filter((x) => x.done && x.kind !== 'w').slice(-1)[0];
        const prevTxt = best ? setText(best, ex, lookup.unit) : '';
        return `<div class="sh-ex">
          <span class="xlab">${labs[idx]}</span>
          <span class="sh-ex-m"><b>${esc(ex?.name ?? it.exId)}</b><small>${it.sets} sets${note ? ` · ${esc(note)}` : ''}</small></span>
          ${prevTxt ? `<em>Last: ${esc(prevTxt)}</em>` : ''}
        </div>`;
      }).join('');
      detail = `<div class="sh-exlist">${rows}</div>
        <div class="sh-subrow"><a class="link" href="#/strength/routine/${r.id}">Edit template ›</a></div>`;
    }
    return `<div class="sh-slot${done ? ' done' : ''}">
      <div class="sh-slot-h">
        <span class="cal-ico strength">${ICON.strength}</span>
        <span><b>${esc(r?.name ?? slot.name)}</b><small>Strength · ${mins} min</small></span>
        ${done ? '<span class="p-badge ok">✓ Done</span>' : ''}
      </div>
      ${detail}
    </div>`;
  }
  return `<div class="sh-slot${done ? ' done' : ''}">
    <div class="sh-slot-h">
      <span class="cal-ico ${slot.equipment}">${ICON[slot.equipment]}</span>
      <span><b>${esc(slot.name)}</b><small>${EQUIPMENT_LABEL[slot.equipment]} · ${mins} min</small></span>
      ${done ? '<span class="p-badge ok">✓ Done</span>' : ''}
    </div>
    <p class="sh-text">${esc(slot.description)}</p>
  </div>`;
}

/** Resolves completed workouts, externally completed slots, and pending slots for a specific calendar date. */
export function resolveDayZoom(args: {
  ymd: string;
  today: string;
  proj: ProjectedStep[];
  dayLog?: DayLogs;
  restDay: number | null;
}): ResolvedDayZoom {
  const { ymd, today, proj, dayLog } = args;
  const liftsDone = dayLog?.lifts ?? [];
  const cardioDone = dayLog?.cardio ?? [];
  const doneEqs = new Set<Equipment>();
  if (liftsDone.length) doneEqs.add('strength');
  for (const m of cardioDone) doneEqs.add(m.equipment);

  const externalDoneSlots: ResolvedDayZoom['externalDoneSlots'] = [];
  const pendingSlots: ResolvedDayZoom['pendingSlots'] = [];
  for (const p of proj) {
    p.step.slots.forEach((slot, slotIndex) => {
      const doneOnYmd = p.slotDone[slotIndex] && (p.slotDates[slotIndex] === ymd || (p.slotDates[slotIndex] == null && p.date === ymd));
      if (doneOnYmd && !doneEqs.has(slot.equipment)) {
        externalDoneSlots.push({ p, slot, slotIndex });
      }
      if (ymd >= today && (p.status === 'current' || p.status === 'upcoming') && !p.slotDone[slotIndex] && p.slotDates[slotIndex] === ymd) {
        pendingSlots.push({ p, slot, slotIndex });
      }
    });
  }

  const associatedStep = pendingSlots[0]?.p
    ?? externalDoneSlots[0]?.p
    ?? proj.find((p) => p.slotDates.some((d) => d === ymd) || p.date === ymd);
  const canShift = !!associatedStep && (
    associatedStep.status === 'upcoming'
    || (associatedStep.status === 'current' && associatedStep.date != null && associatedStep.date > today)
    || ((associatedStep.status === 'done' || associatedStep.status === 'early') && ymd < today)
  );
  const curStep = proj.find((p) => p.status === 'current');
  const week = associatedStep?.step.week
    ?? curStep?.step.week
    ?? PHASE1_PLAN.steps[PHASE1_PLAN.steps.length - 1]?.week
    ?? 1;

  return { liftsDone, cardioDone, externalDoneSlots, pendingSlots, associatedStep, canShift, week };
}

function dayStatusPill(ymd: string, today: string, res: ResolvedDayZoom): string {
  const hasDone = res.liftsDone.length > 0 || res.cardioDone.length > 0 || res.externalDoneSlots.length > 0;
  if (ymd === today) {
    return res.pendingSlots.length === 0 && hasDone
      ? '<span class="p-badge ok">✓ Today · Done</span>'
      : '<span class="p-badge cur">● Today</span>';
  }
  if (ymd === shiftYmd(today, 1)) return '<span class="p-badge">Tomorrow</span>';
  if (hasDone) return '<span class="p-badge ok">✓ Done</span>';
  if (ymd < today) return '<span class="p-badge">Past</span>';
  return '<span class="p-badge">Projected</span>';
}

/** Inline expandable day-zoom card shown below the Today hero card on Home. */
export function renderDayZoomCard(args: {
  ymd: string;
  today: string;
  proj: ProjectedStep[];
  dayLog?: DayLogs;
  restDay: number | null;
  anim?: '' | 'from-right' | 'from-left' | 'fade-in';
  lookup: ZoomLookup;
}): string {
  const { ymd, today, proj, dayLog, restDay, anim = '', lookup } = args;
  const res = resolveDayZoom({ ymd, today, proj, dayLog, restDay });
  const { y, m, d } = parseYmd(ymd);
  const fullDate = new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  const isRestPref = restDay != null && dayOfWeek(ymd) === restDay;
  const sameWeek = shiftYmd(ymd, -dayOfWeek(ymd)) === shiftYmd(today, -dayOfWeek(today));

  const slotCards = [
    ...res.liftsDone.map((s) => renderCompletedLiftSlot(s, lookup)),
    ...res.cardioDone.map((cm) => renderCompletedCardioSlot(cm)),
    ...res.externalDoneSlots.map(({ slot }) => renderPlannedSlot(slot, true, lookup)),
    ...res.pendingSlots.map(({ slot }) => renderPlannedSlot(slot, false, lookup)),
  ];

  const body = slotCards.length
    ? slotCards.join('')
    : `<div class="sh-rest">
        <b>${ymd < today ? 'Rest · Nothing logged' : 'Rest · Nothing scheduled'}</b>
        <small>${isRestPref ? 'Configured weekly rest day — skipped in future projections.' : ymd < today ? 'No workouts saved on this date.' : 'Open training day — swipe or open Program to pull ahead.'}</small>
      </div>`;

  const acts: string[] = [];
  if (res.pendingSlots.length > 0) {
    acts.push('<button class="btn primary dz-btn" type="button" data-zact="do-now">Do this workout now</button>');
  }
  if (res.canShift && res.associatedStep) {
    acts.push(`<button class="btn dz-btn" type="button" data-zact="shift" data-step-idx="${res.associatedStep.step.index}">Shift schedule to here</button>`);
  }
  acts.push(`<a class="btn dz-btn" href="#/program?w=${res.week}">View Program</a>`);

  return `<section class="card day-zoom${anim ? ` ${anim}` : ''}" id="dayZoom" data-zoom-ymd="${ymd}">
    <div class="dz-top">
      <div class="dz-top-l">
        ${dayStatusPill(ymd, today, res)}
        ${res.associatedStep ? `<span class="sh-week">Wk ${res.associatedStep.step.week} · Day ${res.associatedStep.step.day}</span>` : ''}
        ${!sameWeek ? '<button class="link dz-today-jump" type="button" data-zact="today">Today ↩</button>' : ''}
      </div>
      <div class="dz-navs">
        <button class="dz-nav" type="button" data-znav="-1" aria-label="Previous day">‹</button>
        <button class="dz-nav" type="button" data-znav="1" aria-label="Next day">›</button>
        <button class="dz-close" type="button" data-inline-dismiss aria-label="Close">✕</button>
      </div>
    </div>
    <div class="dz-head">
      <h3 class="sh-title">${esc(fullDate)}</h3>
      ${res.associatedStep ? `<small class="dz-sub">${esc(res.associatedStep.step.title)}</small>` : ''}
    </div>
    ${body}
    <div class="dz-acts">${acts.join('')}</div>
  </section>`;
}
