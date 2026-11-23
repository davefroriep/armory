import { elapsedSessionSec, estimateActiveSessionSec } from '../../lifts/logic';
import type { LiftSession } from '../../lifts/types';
import { esc } from '../dom';
import { clock, openSheet } from './kit';

const MAX_SEC = 86_400;
const DELTAS: readonly [string, number][] = [
  ['−1h', -3600],
  ['−15m', -900],
  ['−5m', -300],
  ['+5m', 300],
  ['+15m', 900],
];
const PRESETS: readonly [string, number][] = [
  ['30m', 1800],
  ['45m', 2700],
  ['1h', 3600],
  ['1h 15m', 4500],
  ['1h 30m', 5400],
];

const clampSec = (v: number): number => Math.max(0, Math.min(MAX_SEC, Math.round(v)));

const intVal = (inp: HTMLInputElement): number => {
  const n = parseInt(inp.value.replace(/\D+/g, ''), 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
};

/** Modal clock editor for active and finished strength workouts, including 1-tap idle-gap trimming. */
export function openEditTimeSheet(
  s: LiftSession,
  opts?: { initialSec?: number; paused?: boolean },
): Promise<number | null> {
  const curSec = clampSec(opts?.initialSec ?? elapsedSessionSec(s));
  const estSec = estimateActiveSessionSec(s);
  const showTrim = estSec != null && curSec - estSec >= 300;
  const pausedTag = opts?.paused ? ' · paused' : '';

  return new Promise((resolve) => {
    let out: number | null = null;
    const sh = openSheet(
      `<h3 class="sh-title">Edit Workout Time</h3>
       <p class="sh-text te-preview" id="tePrev">Currently ${esc(clock(curSec))}${pausedTag}</p>
       ${showTrim ? `<button class="aitem on te-trim" type="button" data-te-set="${estSec}"><b>Trim idle time → ${esc(clock(estSec))}</b><small>Estimated active time from your logged sets</small></button>` : ''}
       <div class="te-grid">
         <label class="te-field"><span>HR</span><input class="lf-input" id="teHr" inputmode="numeric" pattern="[0-9]*" autocomplete="off" aria-label="Hours"></label>
         <span class="te-sep" aria-hidden="true">:</span>
         <label class="te-field"><span>MIN</span><input class="lf-input" id="teMin" inputmode="numeric" pattern="[0-9]*" autocomplete="off" aria-label="Minutes"></label>
         <span class="te-sep" aria-hidden="true">:</span>
         <label class="te-field"><span>SEC</span><input class="lf-input" id="teSec" inputmode="numeric" pattern="[0-9]*" autocomplete="off" aria-label="Seconds"></label>
       </div>
       <div class="te-deltas">${DELTAS.map(([lbl, d]) => `<button class="te-delta" type="button" data-te-delta="${d}">${lbl}</button>`).join('')}</div>
       <div class="chips-row wrap">${PRESETS.map(([lbl, sec]) => `<button class="fchip" type="button" data-te-set="${sec}">${lbl}</button>`).join('')}</div>
       <button class="btn primary big" type="button" data-te-ok>Save time</button>
       <button class="btn big" type="button" data-te-no>Cancel</button>`,
      { onClose: () => resolve(out) },
    );

    const hrInp = sh.body.querySelector<HTMLInputElement>('#teHr')!;
    const minInp = sh.body.querySelector<HTMLInputElement>('#teMin')!;
    const secInp = sh.body.querySelector<HTMLInputElement>('#teSec')!;
    const prevEl = sh.body.querySelector<HTMLElement>('#tePrev')!;

    const readSec = (): number => clampSec(intVal(hrInp) * 3600 + intVal(minInp) * 60 + intVal(secInp));

    const refreshPreview = (v: number): void => {
      prevEl.textContent = v === curSec
        ? `Currently ${clock(curSec)}${pausedTag}`
        : `New time: ${clock(v)} (was ${clock(curSec)})`;
      sh.body.querySelectorAll<HTMLElement>('.fchip[data-te-set]').forEach((b) => {
        b.classList.toggle('on', Number(b.dataset.teSet) === v);
      });
    };

    const writeSec = (raw: number): void => {
      const v = clampSec(raw);
      const h = Math.floor(v / 3600);
      const m = Math.floor((v % 3600) / 60);
      const sec = v % 60;
      hrInp.value = String(h);
      minInp.value = String(m).padStart(2, '0');
      secInp.value = String(sec).padStart(2, '0');
      refreshPreview(v);
    };

    writeSec(curSec);

    const commit = (): void => {
      out = readSec();
      sh.close();
    };

    sh.body.addEventListener('focusin', (e) => {
      if (e.target instanceof HTMLInputElement) e.target.select();
    });
    sh.body.addEventListener('input', (e) => {
      if (e.target instanceof HTMLInputElement) refreshPreview(readSec());
    });
    sh.body.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target instanceof HTMLInputElement) {
        e.preventDefault();
        commit();
      }
    });
    sh.body.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const setBtn = t.closest<HTMLElement>('[data-te-set]');
      if (setBtn) {
        writeSec(Number(setBtn.dataset.teSet));
        return;
      }
      const deltaBtn = t.closest<HTMLElement>('[data-te-delta]');
      if (deltaBtn) {
        writeSec(readSec() + Number(deltaBtn.dataset.teDelta));
        return;
      }
      if (t.closest('[data-te-ok]')) { commit(); return; }
      if (t.closest('[data-te-no]')) sh.close();
    });
  });
}
