import {
  PHASE1_PLAN, dayOfWeek, extraKey, parseYmd, shiftYmd, slotKey,
  type ExtraWorkout, type PlanSlot, type ProjectedStep, type SlotState,
} from '../domain/plan';
import { sessionStats, type ExOf, type PRHit } from '../lifts/calc';
import { labels } from '../lifts/logic';
import { resolvePrescription } from '../lifts/prescriptions';
import { matchRoutineByTitle } from '../lifts/seed';
import type { Unit } from '../lifts/settings';
import { condenseSets, fmtDuration, fmtVolume, sessionPRs, setText } from '../lifts/summary';
import type { ExRow, ExerciseRx, LiftSession, Routine, SessionEx } from '../lifts/types';
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
  e1rmOf?: (exId: string) => number | null;
  routines: Routine[];
  prs: Map<string, PRHit>;
  unit: Unit;
}

export interface ResolvedDayZoom {
  liftsDone: LiftSession[];
  cardioDone: WorkoutMeta[];
  externalDoneSlots: { p: ProjectedStep; slot: PlanSlot; slotIndex: number }[];
  skippedSlots: { p: ProjectedStep; slot: PlanSlot; slotIndex: number }[];
  pendingSlots: { p: ProjectedStep; slot: PlanSlot; slotIndex: number }[];
  pendingExtras: ExtraWorkout[];
  doneExtras: ExtraWorkout[];
  skippedExtras: ExtraWorkout[];
  associatedStep: ProjectedStep | undefined;
  canShift: boolean;
  afterPlanEnd: boolean;
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
  opts?: { today?: string; proj?: ProjectedStep[]; extras?: ExtraWorkout[] },
): TodayCompletedItem[] {
  const liftsDone = dayLog?.lifts ?? [];
  const cardioDone = dayLog?.cardio ?? [];
  const doneEqs = new Set<Equipment>();
  const out: TodayCompletedItem[] = [];
  for (const s of liftsDone) {
    doneEqs.add('strength');
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
  for (const m of cardioDone) {
    doneEqs.add(m.equipment);
    const sub = [
      EQUIPMENT_LABEL[m.equipment],
      `${Math.max(1, Math.round(m.seconds / 60))} min`,
      m.avgW > 0 ? `${m.avgW} W` : '',
      m.avgHr > 0 ? `${m.avgHr} bpm` : '',
    ].filter(Boolean).join(' · ');
    out.push({ equipment: m.equipment, name: m.name, sub });
  }
  if (opts?.today) {
    for (const p of opts.proj ?? []) {
      p.step.slots.forEach((slot, si) => {
        const onToday = p.slotDates[si] === opts.today || (p.slotDates[si] == null && p.date === opts.today);
        if (p.slotStates[si] === 'done' && onToday && !doneEqs.has(slot.equipment)) {
          out.push({
            equipment: slot.equipment,
            name: slot.name,
            sub: `${EQUIPMENT_LABEL[slot.equipment]} · ${Math.max(1, Math.round(slot.seconds / 60))} min`,
          });
        }
      });
    }
    for (const x of opts.extras ?? []) {
      if (x.doneDate === opts.today) {
        const k = extraKey(x.id);
        const hasLocal = liftsDone.some((ls) => ls.planSlot === k || ls.name === x.name)
          || cardioDone.some((cm) => cm.planSlot === k || cm.name === x.name);
        if (!hasLocal) {
          out.push({
            equipment: x.equipment,
            name: x.name,
            sub: `${EQUIPMENT_LABEL[x.equipment]} · ${Math.max(1, Math.round(x.seconds / 60))} min`,
          });
        }
      }
    }
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
      <span class="sh-ex-m"><b>${esc(ex?.name ?? e.exId)}</b><small>${esc(summary || `${e.sets.length} sets`)}</small></span>
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
    <div class="sh-subrow"><a class="link" href="#/airbike/w/${m.id}">Details &amp; delete ›</a></div>
  </div>`;
}

function renderItemList(items: { exId: string; sets: number; ss?: boolean; rx?: ExerciseRx }[], lookup: ZoomLookup, templateId?: string): string {
  const labs = labels(items);
  const rows = items.map((it, idx) => {
    const ex = lookup.exOf(it.exId);
    const rx = resolvePrescription(ex, { rx: it.rx, sets: it.sets, base1RmKg: lookup.e1rmOf?.(it.exId) ?? null, unit: lookup.unit });
    const prev = lookup.prevOf(it.exId);
    const best = prev?.sets.filter((x) => x.done && x.kind !== 'w').slice(-1)[0];
    const prevTxt = best ? setText(best, ex, lookup.unit) : '';
    return `<div class="sh-ex">
      <span class="xlab">${labs[idx]}</span>
      <span class="sh-ex-m"><b>${esc(ex?.name ?? it.exId)}</b><small>${esc(rx.roadmapLine)}</small></span>
      ${prevTxt ? `<em>Last: ${esc(prevTxt)}</em>` : ''}
    </div>`;
  }).join('');
  return `<div class="sh-exlist">${rows}</div>${templateId ? `<div class="sh-subrow"><a class="link" href="#/strength/routine/${templateId}">Edit template ›</a></div>` : ''}`;
}

function slotBadgeAndClass(state: SlotState): { badge: string; cls: string } {
  return {
    badge: state === 'done'
      ? '<span class="p-badge ok">✓ Done</span>'
      : state === 'skipped'
        ? '<span class="p-badge sk">(skipped)</span>'
        : state === 'removed'
          ? '<span class="p-badge">(removed)</span>'
          : '',
    cls: state === 'done' ? ' done' : (state === 'skipped' || state === 'removed') ? ' skipped' : '',
  };
}

/** Planned, externally completed, or skipped slot view with matched local routine exercises/sets/notes. */
export function renderPlannedSlot(slot: PlanSlot, state: SlotState, lookup: ZoomLookup, planSlot?: string): string {
  const mins = Math.round(slot.seconds / 60);
  const { badge, cls } = slotBadgeAndClass(state);
  const doBtn = state === 'pending' && planSlot
    ? `<button class="btn primary sh-do-btn" type="button" data-zact="do-slot" data-slot="${esc(planSlot)}">Do this workout</button>`
    : '';
  if (slot.equipment === 'strength') {
    const r = matchRoutineByTitle(slot.name, lookup.routines);
    const detail = r ? renderItemList(r.items, lookup, r.id) : `<p class="sh-text">${esc(slot.description)}</p>`;
    return `<div class="sh-slot${cls}">
      <div class="sh-slot-h">
        <span class="cal-ico strength">${ICON.strength}</span>
        <span><b>${esc(r?.name ?? slot.name)}</b><small>Strength · ${mins} min</small></span>
        ${badge}
      </div>
      ${detail}
      ${doBtn}
    </div>`;
  }
  return `<div class="sh-slot${cls}">
    <div class="sh-slot-h">
      <span class="cal-ico ${slot.equipment}">${ICON[slot.equipment]}</span>
      <span><b>${esc(slot.name)}</b><small>${EQUIPMENT_LABEL[slot.equipment]} · ${mins} min</small></span>
      ${badge}
    </div>
    <p class="sh-text">${esc(slot.description)}</p>
    ${doBtn}
  </div>`;
}

function renderExtraSlot(x: ExtraWorkout, state: SlotState, lookup: ZoomLookup, planSlot?: string): string {
  const mins = Math.round(x.seconds / 60);
  const { badge, cls } = slotBadgeAndClass(state);
  const detail = x.items?.length
    ? renderItemList(x.items, lookup)
    : `<p class="sh-text">${esc(x.description)}</p>`;
  const doBtn = state === 'pending' && planSlot
    ? `<button class="btn primary sh-do-btn" type="button" data-zact="do-slot" data-slot="${esc(planSlot)}">Do this workout</button>`
    : '';
  return `<div class="sh-slot${cls}">
    <div class="sh-slot-h">
      <span class="cal-ico ${x.equipment}">${ICON[x.equipment]}</span>
      <span><b>${esc(x.name)}</b><small>${EQUIPMENT_LABEL[x.equipment]} · Carry-over · ${mins} min</small></span>
      ${badge || '<span class="p-badge cur">Carry-over</span>'}
    </div>
    ${detail}
    ${doBtn}
  </div>`;
}

/** Resolves completed workouts, externally completed slots, skipped slots, extras, and pending slots for a date. */
export function resolveDayZoom(args: {
  ymd: string;
  today: string;
  proj: ProjectedStep[];
  dayLog?: DayLogs;
  extras?: ExtraWorkout[];
  restDay: number | null;
}): ResolvedDayZoom {
  const { ymd, today, proj, dayLog, extras = [] } = args;
  const liftsDone = dayLog?.lifts ?? [];
  const cardioDone = dayLog?.cardio ?? [];
  const doneEqs = new Set<Equipment>();
  if (liftsDone.length) doneEqs.add('strength');
  for (const m of cardioDone) doneEqs.add(m.equipment);

  const externalDoneSlots: ResolvedDayZoom['externalDoneSlots'] = [];
  const skippedSlots: ResolvedDayZoom['skippedSlots'] = [];
  const pendingSlots: ResolvedDayZoom['pendingSlots'] = [];
  for (const p of proj) {
    p.step.slots.forEach((slot, slotIndex) => {
      const state = p.slotStates[slotIndex];
      const onYmd = p.slotDates[slotIndex] === ymd || (p.slotDates[slotIndex] == null && p.date === ymd);
      if (state === 'done' && onYmd && !doneEqs.has(slot.equipment)) {
        externalDoneSlots.push({ p, slot, slotIndex });
      } else if (state === 'skipped' && onYmd) {
        skippedSlots.push({ p, slot, slotIndex });
      }
      if (ymd >= today && state === 'pending' && p.slotDates[slotIndex] === ymd) {
        pendingSlots.push({ p, slot, slotIndex });
      }
    });
  }

  const pendingExtras: ExtraWorkout[] = [];
  const doneExtras: ExtraWorkout[] = [];
  const skippedExtras: ExtraWorkout[] = [];
  for (const x of extras) {
    if (x.doneDate === ymd) {
      const k = extraKey(x.id);
      const hasLocal = liftsDone.some((ls) => ls.planSlot === k || ls.name === x.name)
        || cardioDone.some((cm) => cm.planSlot === k || cm.name === x.name);
      if (!hasLocal) doneExtras.push(x);
    } else if (x.skippedDate === ymd) {
      skippedExtras.push(x);
    } else if (!x.doneDate && !x.skippedDate && ymd >= today && (x.date === ymd || (ymd === today && x.date <= today))) {
      pendingExtras.push(x);
    }
  }

  const associatedStep = pendingSlots[0]?.p
    ?? externalDoneSlots[0]?.p
    ?? skippedSlots[0]?.p
    ?? proj.find((p) => p.slotDates.some((d) => d === ymd) || p.date === ymd);
  const canShift = !!associatedStep && (
    associatedStep.status === 'upcoming'
    || (associatedStep.status === 'current' && associatedStep.date != null && associatedStep.date > today)
    || ((associatedStep.status === 'done' || associatedStep.status === 'early' || associatedStep.status === 'skipped' || associatedStep.status === 'removed') && ymd < today)
  );
  const futureDates = proj
    .flatMap((p) => p.slotDates)
    .filter((d): d is string => !!d && d > today);
  const maxFutureDate = futureDates.length ? futureDates.sort()[futureDates.length - 1] : null;
  const afterPlanEnd = ymd > today
    && !pendingSlots.length
    && !pendingExtras.length
    && !externalDoneSlots.length
    && !doneExtras.length
    && !skippedSlots.length
    && !skippedExtras.length
    && maxFutureDate != null
    && ymd > maxFutureDate;

  const curStep = proj.find((p) => p.status === 'current');
  const week = associatedStep?.step.week
    ?? curStep?.step.week
    ?? PHASE1_PLAN.steps[PHASE1_PLAN.steps.length - 1]?.week
    ?? 1;

  return {
    liftsDone,
    cardioDone,
    externalDoneSlots,
    skippedSlots,
    pendingSlots,
    pendingExtras,
    doneExtras,
    skippedExtras,
    associatedStep,
    canShift,
    afterPlanEnd,
    week,
  };
}

function dayStatusPill(ymd: string, today: string, res: ResolvedDayZoom): string {
  const hasDone = res.liftsDone.length > 0 || res.cardioDone.length > 0 || res.externalDoneSlots.length > 0 || res.doneExtras.length > 0;
  if (ymd === today) {
    return res.pendingSlots.length === 0 && res.pendingExtras.length === 0 && hasDone
      ? '<span class="p-badge ok">✓ Today · Done</span>'
      : '<span class="p-badge cur">● Today</span>';
  }
  if (ymd === shiftYmd(today, 1)) return '<span class="p-badge">Tomorrow</span>';
  if (hasDone) return '<span class="p-badge ok">✓ Done</span>';
  if (res.skippedSlots.length > 0 || res.skippedExtras.length > 0) return '<span class="p-badge sk">Skipped</span>';
  if (ymd < today) return '<span class="p-badge">Past</span>';
  return '<span class="p-badge">Projected</span>';
}

/** Inline always-open day-zoom card shown below the Today bar on Home. */
export function renderDayZoomCard(args: {
  ymd: string;
  today: string;
  proj: ProjectedStep[];
  dayLog?: DayLogs;
  extras?: ExtraWorkout[];
  restDay: number | null;
  anim?: '' | 'from-right' | 'from-left' | 'fade-in';
  lookup: ZoomLookup;
}): string {
  const { ymd, today, proj, dayLog, extras = [], restDay, anim = '', lookup } = args;
  const res = resolveDayZoom({ ymd, today, proj, dayLog, extras, restDay });
  const { y, m, d } = parseYmd(ymd);
  const fullDate = new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  const isRestPref = restDay != null && dayOfWeek(ymd) === restDay;

  const pendingList = [
    ...res.pendingExtras.map((x) => ({ kind: 'extra' as const, x, key: extraKey(x.id) })),
    ...res.pendingSlots.map(({ p, slot, slotIndex }) => ({ kind: 'plan' as const, slot, key: slotKey(p.step.index, slotIndex) })),
  ];
  const singlePendingKey = pendingList.length === 1 ? pendingList[0].key : null;

  const slotCards = [
    ...res.liftsDone.map((s) => renderCompletedLiftSlot(s, lookup)),
    ...res.cardioDone.map((cm) => renderCompletedCardioSlot(cm)),
    ...res.doneExtras.map((x) => renderExtraSlot(x, 'done', lookup)),
    ...res.externalDoneSlots.map(({ slot }) => renderPlannedSlot(slot, 'done', lookup)),
    ...res.skippedExtras.map((x) => renderExtraSlot(x, 'skipped', lookup)),
    ...res.skippedSlots.map(({ slot }) => renderPlannedSlot(slot, 'skipped', lookup)),
    ...pendingList.map((item) => item.kind === 'extra'
      ? renderExtraSlot(item.x, 'pending', lookup, singlePendingKey ? undefined : item.key)
      : renderPlannedSlot(item.slot, 'pending', lookup, singlePendingKey ? undefined : item.key)),
  ];

  const body = slotCards.length
    ? slotCards.join('')
    : res.afterPlanEnd
      ? `<div class="sh-rest">
          <b>End of Phase 1 Block</b>
          <small>All 4 weeks of Phase 1 finish before this date.</small>
        </div>`
      : `<div class="sh-rest">
          <b>${ymd < today ? 'Rest · Nothing logged' : 'Rest · Nothing scheduled'}</b>
          <small>${isRestPref ? 'Configured weekly rest day — skipped in future projections.' : ymd < today ? 'No workouts saved on this date.' : 'Open training day — tap or swipe to pull a future day ahead.'}</small>
        </div>`;

  const topRowActs: string[] = [];
  if (singlePendingKey) {
    topRowActs.push(`<button class="btn primary dz-btn" type="button" data-zact="do-slot" data-slot="${esc(singlePendingKey)}">Do this workout</button>`);
  }
  if (res.canShift && res.associatedStep) {
    topRowActs.push(`<button class="btn primary dz-btn" type="button" data-zact="shift" data-step-idx="${res.associatedStep.step.index}">Shift schedule to here</button>`);
  }
  const actsHtml = `${topRowActs.length ? `<div class="dz-row">${topRowActs.join('')}</div>` : ''}<a class="btn dz-btn dz-full" href="#/program?w=${res.week}">View Full Program</a>`;

  return `<section class="card day-zoom${anim ? ` ${anim}` : ''}" id="dayZoom" data-zoom-ymd="${ymd}">
    <div class="dz-top">
      <div class="dz-top-l">
        ${dayStatusPill(ymd, today, res)}
        ${res.associatedStep ? `<span class="sh-week">Wk ${res.associatedStep.step.week} · Day ${res.associatedStep.step.day}</span>` : ''}
        ${ymd !== today ? '<button class="link dz-today-jump" type="button" data-back-today>Today ↩</button>' : ''}
      </div>
      <div class="dz-navs">
        <button class="dz-nav" type="button" data-znav="-1" aria-label="Previous day">‹</button>
        <button class="dz-nav" type="button" data-znav="1" aria-label="Next day">›</button>
      </div>
    </div>
    <div class="dz-head">
      <h3 class="sh-title">${esc(fullDate)}</h3>
      ${res.associatedStep ? `<small class="dz-sub">${esc(res.associatedStep.step.title)}</small>` : ''}
    </div>
    ${body}
    <div class="dz-acts">${actsHtml}</div>
  </section>`;
}
