import { wattsForRpm } from '../domain/airbikeCurve';
import { recoveryLimitBpm } from '../domain/gate';
import {
  PHASE_COLOR, applyProgramTarget, clampProgramTarget, defaultProgramTarget, effectiveProgramTarget,
  fmtDur, fmtRpm, formatProgramTarget, summarizeProgramPhases, totalSeconds, workSeconds, type Program,
} from '../domain/programs';
import { getSettings, updateSettings } from '../store/settings';
import { esc, syncModalInert, wireModalBackdrop } from './dom';
import { toast } from './lifts/kit';

export interface ProgramTargetSheetOpts {
  baseProgram: Program;
  mode: 'hud' | 'schedule';
  guest?: boolean;
  /** Current live session target (defaults to saved target). */
  currentTarget?: number | null;
  /** Plan slot key when opened from Home/Equipment Today menu. */
  planSlot?: string;
  /** Called in `hud` mode when user applies a target for this workout only. */
  onApplyToday?: (target: number) => void;
  /** Called when user saves a new persistent default target. */
  onSaveDefault?: (target: number) => void;
}

/**
 * Opt-in Targets & Setpoints bottom sheet (never shown automatically on load or Start).
 * Displays every phase's exact duration & target and lets the athlete adjust today's target
 * or save a new default with Guest-style step buttons.
 */
export function openProgramTargetsSheet(opts: ProgramTargetSheetOpts): () => void {
  const { baseProgram, mode, guest = false } = opts;
  const eq = baseProgram.equipment ?? 'airbike';
  const isSpin = eq === 'spin' || baseProgram.segments.some((g) => g.watts != null);
  const unit = isSpin ? 'W' : 'RPM';
  const steps = isSpin ? [-25, -5, 5, 25] : [-5, -1, 1, 5];
  const canAdjust = defaultProgramTarget(baseProgram) != null && (baseProgram.zone === 'Z5' || baseProgram.zone === 'Z2');
  const refSeg = baseProgram.segments.find((g) => (g.kind === 'work' || g.kind === 'steady') && !g.allOut);

  const st = getSettings();
  let savedTarget = effectiveProgramTarget(baseProgram, st.programTargets);
  const initialCurrent = opts.currentTarget ?? savedTarget;
  let val = initialCurrent;

  const hrGated = !guest && st.recoveryMode === 'gated' && baseProgram.segments.some((g) => g.gate);
  const hrLimit = recoveryLimitBpm(st.hrMax, st.recoveryHrPct);
  const total = totalSeconds(baseProgram);
  const work = workSeconds(baseProgram);
  const subParts = [
    total ? `${fmtDur(total)} total` : '',
    work ? `${fmtDur(work)} work` : '',
    baseProgram.segments.some((g) => g.gate)
      ? (hrGated ? `HR-gated ≤ ${hrLimit} bpm` : 'Fixed timers')
      : '',
  ].filter(Boolean);

  const el = document.createElement('div');
  el.className = 'modal';
  el.id = 'targetsSheet';
  el.innerHTML = `<div class="sheet tgt-sheet" role="dialog" aria-modal="true" aria-label="Targets and setpoints">
    <h2>${guest ? `Guest · ${esc(baseProgram.name)}` : esc(baseProgram.name)}</h2>
    ${subParts.length ? `<p class="tgt-sheet-sub">${esc(subParts.join(' · '))}</p>` : ''}
    ${canAdjust ? `
      <div class="tgt-box">
        <div class="guest-tgt-val" id="tgtVal"></div>
        <div class="tgt-delta" id="tgtDelta"></div>
        <div class="guest-tgt-btns">
          ${steps.map((d) => `<button type="button" data-tstep="${d}">${d > 0 ? `+${d}` : d}</button>`).join('')}
        </div>
        <button class="btn sm" type="button" id="tgtReset"></button>
      </div>
    ` : ''}
    <div class="tgt-sec">SETPOINTS</div>
    <div class="phase-list" id="tgtPhases"></div>
    <div class="tgt-acts" id="tgtActs"></div>
  </div>`;

  let closed = false;
  let offBackdrop = () => {};
  const close = (): void => {
    if (closed) return;
    closed = true;
    offBackdrop();
    el.remove();
    syncModalInert();
  };

  const paint = (): void => {
    const preview = applyProgramTarget(baseProgram, val);
    const phases = summarizeProgramPhases(preview, { hrGated });
    const listEl = el.querySelector<HTMLElement>('#tgtPhases')!;
    listEl.innerHTML = phases.length
      ? phases.map((r) => `<div class="phase-row${r.editable ? ' primary' : ''}">
          <i class="phase-dot" style="background:${PHASE_COLOR[r.kind]}"></i>
          <span class="phase-main"><b>${esc(r.label)}</b><small>${esc(r.durationText)}</small></span>
          <span class="phase-tgt">${esc(r.targetText)}</span>
        </div>`).join('')
      : `<div class="phase-row"><span class="phase-main"><b>Free Ride</b><small>No fixed intervals</small></span><span class="phase-tgt">Open</span></div>`;

    if (canAdjust && val != null && savedTarget != null) {
      const valEl = el.querySelector<HTMLElement>('#tgtVal')!;
      valEl.innerHTML = isSpin
        ? `<b>${val} W</b><small>~${fmtRpm(refSeg?.rpm ?? 85)} RPM</small>`
        : `<b>${val} RPM</b><small>${Math.round(wattsForRpm(val))} W</small>`;

      const delta = val - savedTarget;
      const deltaEl = el.querySelector<HTMLElement>('#tgtDelta')!;
      if (guest) {
        deltaEl.textContent = delta === 0
          ? `Matching Dave's target (${formatProgramTarget(baseProgram, savedTarget)})`
          : `${delta > 0 ? '+' : '−'}${Math.abs(delta)} ${unit} vs Dave's ${savedTarget} ${unit}`;
      } else {
        deltaEl.textContent = delta === 0
          ? `Saved default · ${formatProgramTarget(baseProgram, savedTarget)}`
          : `${delta > 0 ? '+' : '−'}${Math.abs(delta)} ${unit} vs saved ${savedTarget} ${unit}`;
      }
      deltaEl.classList.toggle('mod', delta !== 0);

      const rstBtn = el.querySelector<HTMLButtonElement>('#tgtReset')!;
      rstBtn.textContent = `Reset to saved (${savedTarget} ${unit})`;
      rstBtn.disabled = val === savedTarget;
    }

    const actsEl = el.querySelector<HTMLElement>('#tgtActs')!;
    const changedFromCurrent = val != null && val !== initialCurrent;
    const changedFromSaved = val != null && savedTarget != null && val !== savedTarget;

    if (mode === 'hud') {
      if (!canAdjust || val == null) {
        actsEl.innerHTML = `<button class="btn primary big" type="button" data-tact="close">Done</button>`;
      } else if (guest) {
        actsEl.innerHTML = changedFromCurrent
          ? `<button class="btn primary big" type="button" data-tact="apply">Apply target (${val} ${unit})</button>
             <button class="btn big" type="button" data-tact="close">Cancel</button>`
          : `<button class="btn primary big" type="button" data-tact="close">Done</button>`;
      } else if (changedFromCurrent || changedFromSaved) {
        actsEl.innerHTML = `<button class="btn primary big" type="button" data-tact="apply">Apply for this workout (${val} ${unit})</button>
          ${changedFromSaved ? `<button class="btn big" type="button" data-tact="save">Save ${val} ${unit} as default</button>` : ''}
          <button class="btn big" type="button" data-tact="close">Cancel</button>`;
      } else {
        actsEl.innerHTML = `<button class="btn primary big" type="button" data-tact="close">Done</button>`;
      }
    } else {
      const startLbl = changedFromSaved && val != null ? `Start at ${val} ${unit} (today only)` : 'Start workout';
      actsEl.innerHTML = `<button class="btn primary big" type="button" data-tact="start">${esc(startLbl)}</button>
        ${canAdjust && changedFromSaved && val != null ? `<button class="btn big" type="button" data-tact="save">Save ${val} ${unit} as default</button>` : ''}
        <button class="btn big" type="button" data-tact="close">Close</button>`;
    }
  };

  paint();
  offBackdrop = wireModalBackdrop(el, close);

  el.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    const stepBtn = t.closest<HTMLElement>('[data-tstep]');
    if (stepBtn && val != null) {
      val = clampProgramTarget(baseProgram, val + Number(stepBtn.dataset.tstep));
      paint();
      return;
    }
    if (t.closest('#tgtReset') && savedTarget != null) {
      val = savedTarget;
      paint();
      return;
    }
    const act = t.closest<HTMLElement>('[data-tact]')?.dataset.tact;
    if (!act) return;
    if (act === 'close') {
      close();
      return;
    }
    if (act === 'apply' && val != null) {
      opts.onApplyToday?.(val);
      close();
      return;
    }
    if (act === 'save' && val != null) {
      const cur = getSettings().programTargets ?? {};
      updateSettings({ programTargets: { ...cur, [baseProgram.id]: val } });
      savedTarget = val;
      opts.onSaveDefault?.(val);
      toast(`Saved ${formatProgramTarget(baseProgram, val)} as default`);
      if (mode === 'hud') close();
      else paint();
      return;
    }
    if (act === 'start') {
      const params = new URLSearchParams();
      if (opts.planSlot) params.set('pick', opts.planSlot);
      if (canAdjust && val != null && savedTarget != null && val !== savedTarget) {
        params.set('target', String(val));
      }
      const qs = params.toString();
      close();
      location.hash = `#/${eq}/p/${baseProgram.id}${qs ? `?${qs}` : ''}`;
    }
  });

  document.body.appendChild(el);
  syncModalInert();
  return close;
}
