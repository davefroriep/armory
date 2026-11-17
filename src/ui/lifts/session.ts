import { audio, buzz } from '../../audio/audio';
import { active } from '../../lifts/active';
import { computePRs } from '../../lifts/calc';
import { advanceCursorAfterLog, resolveTargetSet } from '../../lifts/cursor';
import {
  addSet, buildExercise, canLog, collectUnfinishedItems, deleteSet, editReps, editWeight, hasLogged, isSessionPaused, isSessionStarted, labels, midSuperset,
  prevSetFor, removeLastSet, supersetGroups, swapExercise, toggleDone,
} from '../../lifts/logic';
import { setKeepScreenOn } from '../../native/service';
import { calendar } from '../../intervals/calendar';
import { MUSCLE_LABEL } from '../../lifts/muscles';
import { fmtKg, fmtNum, fromDisplay, liftSettings, liftSettingsChanged, stepsFor, updateLiftSettings } from '../../lifts/settings';
import { lifts } from '../../lifts/store';
import { resyncSession } from '../../lifts/intervals';
import { setText } from '../../lifts/summary';
import type { Exercise, LiftSession, SessionEx, SetKind } from '../../lifts/types';
import { voice } from '../../lifts/voice';
import { allowNextRoute } from '../../main';
import { getSettings } from '../../store/settings';
import { setWallboardHoldState } from '../../wallboard/state';
import { wallboard } from '../../wallboard/sync';
import { wireChips } from '../chrome';
import { $, esc } from '../dom';
import { LI, clock, actionSheet, confirmSheet, mount, openSheet, promptSheet, toast, type Sheet } from './kit';
import { pickExercises } from './picker';
import { openSetVideoSheet, playVideo } from './video';

const KIND_LABEL: Record<SetKind, string> = { n: 'Normal', w: 'Warm-up', d: 'Drop set', f: 'To failure' };
const KIND_CHAR: Record<SetKind, string> = { n: '', w: 'W', d: 'D', f: 'F' };
const RPES = [6, 7, 7.5, 8, 8.5, 9, 9.5, 10];

const num = (v: string): number | null => {
  const n = parseFloat(v.replace(',', '.'));
  return Number.isFinite(n) && n >= 0 ? n : null;
};

export function sessionScreen(root: HTMLElement, args: string[]): () => void {
  return mount(() => build(root, args[0]));
}

function build(root: HTMLElement, id?: string): () => void {
  const s: LiftSession | undefined = id ? lifts.sessions.get(id) : lifts.active;
  if (!s) {
    location.replace('#/strength');
    return () => {};
  }
  const edit = s.status === 'done';
  const exOf = lifts.exOrStub;
  const unit = (): 'lb' | 'kg' => liftSettings().unit;
  let dirty = false;
  let finishing = false;
  let dead = false;
  let hold: { setId: string; endMs: number; timer: ReturnType<typeof setInterval> } | null = null;
  let hits = new Map<string, { n: number; kg: number }>();
  const prevCache = new Map<string, SessionEx | null>();
  const prevOf = (se: SessionEx): SessionEx | null => {
    if (!prevCache.has(se.exId)) prevCache.set(se.exId, lifts.previous(se.exId, s.startMs, s.id));
    return prevCache.get(se.exId)!;
  };
  const recomputePRs = (): void => {
    hits = edit
      ? lifts.prs()
      : computePRs([...lifts.finished().filter((x) => x.id !== s.id), { ...s, status: 'done' as const, startMs: Date.now() + 1 }], exOf);
  };
  const save = (): void => { dirty = true; lifts.saveSession(s); };
  const find = (uid: string): SessionEx => s.exs.find((e) => e.uid === uid)!;

  // ---------- render ----------
  const paintHoldBtn = (setId?: string): void => {
    const id = setId ?? hold?.setId;
    if (!id) return;
    const btn = root.querySelector<HTMLElement>(`.srow[data-sid="${id}"] .shold`);
    if (!btn) return;
    const activeHere = hold?.setId === id;
    btn.classList.toggle('on', activeHere);
    btn.textContent = activeHere ? `${Math.max(1, Math.ceil((hold!.endMs - Date.now()) / 1000))}s` : '▶';
  };
  const stopHold = (): void => {
    if (!hold) return;
    const prevId = hold.setId;
    clearInterval(hold.timer);
    hold = null;
    setWallboardHoldState(null);
    void setKeepScreenOn(false);
    paintHoldBtn(prevId);
  };
  const startHold = (se: SessionEx, i: number, card: HTMLElement, row: HTMLElement): void => {
    const set = se.sets[i];
    if (hold?.setId === set.id) { stopHold(); return; }
    stopHold();
    const sec = Math.max(1, Math.round(set.r ?? 45));
    set.r = sec;
    set.rAuto = false;
    const rInp = row.querySelector<HTMLInputElement>('.sr');
    if (rInp) { rInp.value = String(sec); rInp.classList.remove('auto'); }
    if (!edit) {
      active.ensureStarted(s);
      paintClock();
    }
    if (liftSettings().restSound) audio.unlock();
    void setKeepScreenOn(true);
    const endMs = Date.now() + sec * 1000;
    const setId = set.id;
    const timer = setInterval(() => {
      if (dead || !hold || hold.setId !== setId) return;
      if (Date.now() < hold.endMs) {
        paintHoldBtn(setId);
        return;
      }
      stopHold();
      if (liftSettings().restSound) audio.chime();
      buzz([220, 90, 220]);
      if (!set.done) {
        const curCard = root.querySelector<HTMLElement>(`.xcard[data-uid="${se.uid}"]`) ?? card;
        const curRow = curCard.querySelector<HTMLElement>(`.srow[data-sid="${setId}"]`) ?? row;
        logSet(curCard, curRow, false);
      }
    }, 250);
    hold = { setId, endMs, timer };
    setWallboardHoldState({ exUid: se.uid, setId, label: exOf(se.exId).name, endMs, totalSec: sec });
    paintHoldBtn(setId);
    save();
  };

  const rowHtml = (se: SessionEx, ex: Exercise, i: number, labelMap: number[]): string => {
    const set = se.sets[i];
    const prev = prevSetFor(prevOf(se), se, i);
    const kind = KIND_CHAR[set.kind] || String(labelMap[i]);
    const pr = set.done && hits.has(set.id);
    const wShow = set.w != null ? fmtKg(set.w) : '';
    const hasVid = !!set.vids?.length;
    const rpeTag = set.rpe != null ? `<u class="srpe">@${set.rpe}</u>` : '';
    const dot = set.note ? '<i class="ndot"></i>' : '';
    const isTime = ex.type === 'time';
    const holdActive = hold?.setId === set.id;
    const holdBtn = isTime
      ? `<button class="shold${holdActive ? ' on' : ''}" type="button" data-act="hold" aria-label="Hold timer">${holdActive ? `${Math.max(1, Math.ceil((hold!.endMs - Date.now()) / 1000))}s` : '▶'}</button>`
      : '';
    return `<div class="srow${isTime ? ' t-row' : ''}${set.done ? ' done' : ''}${set.kind !== 'n' ? ' k-' + set.kind : ''}" data-i="${i}" data-sid="${set.id}">
      <button class="snum${set.rpe != null ? ' has-rpe' : ''}" data-act="setmenu" aria-label="Set options">${kind}${pr ? '<i class="prstar">★</i>' : ''}${rpeTag}${dot}</button>
      <button class="sprev" data-act="usePrev" ${prev ? '' : 'disabled'}>${prev ? esc(setText(prev, ex, unit())) : '–'}</button>
      <input class="sw${set.wAuto ? ' auto' : ''}" inputmode="decimal" enterkeyhint="next" autocomplete="off" value="${wShow}" placeholder="${ex.type === 'weight' ? '–' : 'BW'}" aria-label="Weight">
      <input class="sr${set.rAuto ? ' auto' : ''}" inputmode="numeric" enterkeyhint="next" autocomplete="off" value="${set.r ?? ''}" placeholder="${isTime ? 'sec' : '–'}" aria-label="${isTime ? 'Seconds' : 'Reps'}">
      ${holdBtn}
      <button class="svid${hasVid ? ' on' : ''}" type="button" data-act="vid" aria-label="${hasVid ? 'Manage linked video' : 'Link or record lift video'}">${LI.video}</button>
      <button class="schk${set.done ? ' on' : ''}" data-act="done" aria-label="Log set">✓</button>
    </div>`;
  };

  const cardHtml = (se: SessionEx, idx: number, labs: string[]): string => {
    const ex = exOf(se.exId);
    let w = 0;
    const labelMap = se.sets.map((x) => (x.kind === 'w' ? 0 : ++w));
    const note = lifts.row(se.exId)?.note;
    const wHead = ex.type === 'bodyweight' ? `+${unit().toUpperCase()}` : unit().toUpperCase();
    const rHead = ex.type === 'time' ? 'SEC' : 'REPS';
    const restSec = active.restSecFor(se);
    const autoOn = active.restAutoFor(s, idx);
    const ls = liftSettings();
    const showTimerGroup = !edit && (ls.showTimer || ls.showAutoTimer);
    return `<section class="xcard" data-idx="${idx}" data-uid="${se.uid}">
      <div class="xhead">
        <span class="xlab">${labs[idx]}</span>
        <button class="xname" data-act="exmenu"><b>${esc(ex.name)}</b><small>${ex.primary.map((m) => MUSCLE_LABEL[m]).join(' · ')}</small></button>
        <button class="xmore" data-act="exmenu" aria-label="Exercise options">⋯</button>
      </div>
      ${note ? `<button class="xnote" data-act="exnote">${esc(note)}</button>` : ''}
      <div class="xth${ex.type === 'time' ? ' t-row' : ''}"><span>SET</span><span>PREVIOUS</span><span>${wHead}</span><span>${rHead}</span>${ex.type === 'time' ? '<span>HOLD</span>' : ''}<span></span><span></span></div>
      <div class="xrows">${se.sets.map((_, i) => rowHtml(se, ex, i, labelMap)).join('')}</div>
      <div class="xfoot">
        <button class="stp" data-act="rmset" aria-label="Remove set">−</button><span>SET</span><button class="stp" data-act="addset" aria-label="Add set">+</button>
        ${showTimerGroup ? `<div class="xtimer">
          ${ls.showAutoTimer ? `<button class="xt-auto${autoOn ? ' on' : ''}" type="button" data-act="restauto" aria-pressed="${autoOn}" title="Auto-start rest timer on check">Auto</button>` : ''}
          ${ls.showTimer ? `<button class="xt-go" type="button" data-act="reststart" aria-label="Start rest timer">▶ ${clock(restSec)}</button>` : ''}
        </div>` : ''}
      </div>
    </section>`;
  };

  const cardsHtml = (labs: string[]): string =>
    supersetGroups(s.exs).map((g) => {
      if (g.ssIdx == null) return cardHtml(s.exs[g.start], g.start, labs);
      const inner = Array.from({ length: g.end - g.start + 1 }, (_, k) => cardHtml(s.exs[g.start + k], g.start + k, labs)).join('');
      return `<div class="ss-group ss-c${g.ssIdx % 2}" data-ss="${g.ssIdx}"><div class="ss-tag">SUPERSET</div>${inner}</div>`;
    }).join('');

  const clockCtlHtml = (): string => {
    if (edit) return '';
    if (!isSessionStarted(s)) return '<button class="sstart" type="button" data-act="start">▶ Start</button>';
    return isSessionPaused(s)
      ? '<button class="sstart" type="button" data-act="resume">▶ Resume</button>'
      : '<button class="spause" type="button" data-act="pause">⏸ Pause</button>';
  };

  const header = (): string => {
    const back = edit ? `#/strength/workout/${s.id}` : '#/strength';
    const micBtn = edit ? '' : '<button class="s-mic" id="smic" type="button" data-act="voice" aria-label="Voice commands"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 1 3 3v7a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg></button>';
    return `<header class="bar sbar">
      <a class="icon-btn" href="${back}" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></a>
      <button class="stitle" data-act="rename"><b>${esc(s.name)}</b><small>${edit ? new Date(s.startMs).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }) + ' · editing' : '<span data-clock>0:00</span>'}</small></button>
      <div class="chips"><span id="sclkctl">${clockCtlHtml()}</span>${micBtn}<button class="chip" data-chip="hr"></button><button class="icon-btn" data-act="wkmenu" aria-label="Workout options">⋯</button></div>
    </header>`;
  };

  const paintTarget = (): void => {
    const ptr = edit ? null : resolveTargetSet(s.exs, voice.cursor);
    root.querySelectorAll<HTMLElement>('.srow').forEach((row) => {
      row.classList.toggle('cur-target', !!ptr && row.dataset.sid === ptr.setId && !row.classList.contains('done'));
    });
  };

  const paintMic = (): void => {
    const btn = root.querySelector<HTMLElement>('#smic');
    if (!btn) return;
    const on = getSettings().voiceEnabled;
    const armed = voice.isArmed();
    btn.classList.toggle('on', on && voice.listening);
    btn.classList.toggle('armed', on && armed);
    btn.setAttribute('aria-pressed', String(on));
    btn.title = armed ? 'Listening for command…' : on ? `Say "${getSettings().voiceWakeWord || 'armory'} …"` : 'Enable hands-free voice logging';
  };

  const paint = (): void => {
    recomputePRs();
    prevCache.clear();
    const labs = labels(s.exs);
    root.innerHTML = `${header()}
      <main class="page lsession">
        ${cardsHtml(labs)}
        <button class="addex-banner${s.exs.length === 0 ? ' hero' : ''}" type="button" data-act="addex">
          <span class="aeb-ico">${LI.plus}</span>
          <span class="aeb-txt">
            <b>${s.exs.length === 0 ? 'Add Exercise' : 'Add Next Exercise'}</b>
            <small>${s.exs.length === 0 ? 'Pick from 120+ lifts or create custom — add exercises &amp; sets live as you go' : 'Pick or create another exercise in real time'}</small>
          </span>
        </button>
        ${edit ? '<button class="btn primary big" data-act="finish">Done</button>' : '<button class="btn primary big" data-act="finish">Finish workout</button>'}
      </main>
      <div class="restbar" id="restbar" hidden><i class="rb-fill"></i><span class="rb-lab"></span><b class="rb-time"></b><button data-rest="-15">−15</button><button data-rest="15">+15</button><button data-rest="0">Skip</button></div>
      <div class="dock" id="dock" hidden></div>`;
    paintRest();
    paintClock();
    paintTarget();
    paintMic();
  };

  // ---------- targeted refresh (never touches the focused input) ----------
  const syncCard = (card: HTMLElement): void => {
    const se = find(card.dataset.uid!);
    const ex = exOf(se.exId);
    card.querySelectorAll<HTMLElement>('.srow').forEach((row) => {
      const i = Number(row.dataset.i);
      const set = se.sets[i];
      if (!set) return;
      row.classList.toggle('done', set.done);
      row.querySelector('.schk')!.classList.toggle('on', set.done);
      const w = row.querySelector<HTMLInputElement>('.sw')!;
      const r = row.querySelector<HTMLInputElement>('.sr')!;
      if (document.activeElement !== w) w.value = set.w != null ? fmtKg(set.w) : '';
      if (document.activeElement !== r) r.value = set.r != null ? String(set.r) : '';
      w.classList.toggle('auto', !!set.wAuto);
      r.classList.toggle('auto', !!set.rAuto);
      row.querySelector('.svid')?.classList.toggle('on', !!set.vids?.length);
      const star = row.querySelector('.prstar');
      const pr = set.done && hits.has(set.id);
      if (pr && !star) row.querySelector('.snum')!.insertAdjacentHTML('beforeend', '<i class="prstar">★</i>');
      if (!pr && star) star.remove();
    });
    void ex;
  };

  // ---------- clock + rest ----------
  const paintClock = (): void => {
    if (edit) return;
    const started = isSessionStarted(s);
    const paused = isSessionPaused(s);
    const txt = started ? `${clock(active.elapsedSec())}${paused ? ' · paused' : ''}` : '0:00';
    root.querySelectorAll<HTMLElement>('[data-clock]').forEach((e) => { e.textContent = txt; });
    const ctl = root.querySelector<HTMLElement>('#sclkctl');
    const want = clockCtlHtml();
    if (ctl && ctl.innerHTML !== want) ctl.innerHTML = want;
  };
  const paintRest = (): void => {
    const bar = root.querySelector<HTMLElement>('#restbar');
    if (!bar) return;
    const r = active.rest;
    bar.hidden = !r || edit;
    if (!r) return;
    const left = (r.endMs - Date.now()) / 1000;
    bar.classList.toggle('over', left <= 0);
    bar.querySelector('.rb-lab')!.textContent = left > 0 ? 'REST' : 'GO';
    bar.querySelector('.rb-time')!.textContent = left > 0 ? clock(Math.ceil(left)) : `+${clock(-left)}`;
    (bar.querySelector('.rb-fill') as HTMLElement).style.width = `${Math.max(0, Math.min(100, (1 - left / r.totalSec) * 100))}%`;
  };
  const offTick = active.tick.on(() => { paintClock(); paintRest(); });
  const offRest = active.restChanged.on(paintRest);
  const offChips = wireChips(root, { resume: false });
  const offCfg = liftSettingsChanged.on(() => { if (!dead) paint(); });
  const syncVisibleCards = (): void => {
    if (dead) return;
    const domSig = [...root.querySelectorAll<HTMLElement>('.xcard')]
      .map((c) => `${c.dataset.uid}:${[...c.querySelectorAll<HTMLElement>('.srow')].map((r) => r.dataset.sid).join(',')}`)
      .join('|');
    const modelSig = s.exs.map((e) => `${e.uid}:${e.sets.map((x) => x.id).join(',')}`).join('|');
    if (domSig !== modelSig) {
      paint();
      root.querySelector('.srow.cur-target')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return;
    }
    recomputePRs();
    root.querySelectorAll<HTMLElement>('.xcard').forEach((card) => syncCard(card));
    paintTarget();
    paintMic();
    paintClock();
  };
  const offVoice = edit ? () => {} : voice.changed.on(syncVisibleCards);
  if (!edit) {
    voice.setLiftScreenMounted(true);
    voice.holdStarter = (exUid, setId, seconds) => {
      const se = s.exs.find((e) => e.uid === exUid);
      const si = se?.sets.findIndex((x) => x.id === setId) ?? -1;
      if (!se || si < 0) return false;
      if (seconds != null) { se.sets[si].r = seconds; se.sets[si].rAuto = false; }
      const card = root.querySelector<HTMLElement>(`.xcard[data-uid="${exUid}"]`);
      const row = card?.querySelector<HTMLElement>(`.srow[data-sid="${setId}"]`);
      if (!card || !row) return false;
      startHold(se, si, card, row);
      return true;
    };
  }
  // another screen/tab finished, deleted, or voice-updated this workout
  const offStore = lifts.changed.on(() => {
    if (finishing) return;
    if (!lifts.sessions.has(s.id)) { allowNextRoute(); location.replace('#/strength'); }
    else if (!edit && lifts.sessions.get(s.id)!.status === 'done' && location.hash.startsWith('#/strength/session')) {
      allowNextRoute();
      location.replace(`#/strength/summary/${s.id}`);
    }
    else if (!dead) syncVisibleCards();
  });

  // ---------- dock (steppers & rep presets above the keyboard) ----------
  const REP_PRESETS = [8, 10, 12, 15];
  let focused: HTMLInputElement | null = null;
  let scrollT1: ReturnType<typeof setTimeout> | null = null;
  let scrollT2: ReturnType<typeof setTimeout> | null = null;
  const dock = (): HTMLElement => $('#dock', root);
  const ensureFieldVisible = (inp: HTMLInputElement): void => {
    if (dead || !document.body.contains(inp)) return;
    const vv = window.visualViewport;
    const vpTop = vv?.offsetTop ?? 0;
    const vpH = vv?.height ?? window.innerHeight;
    const dk = dock();
    const dkH = dk.hidden ? 0 : dk.getBoundingClientRect().height || 62;
    const rb = root.querySelector<HTMLElement>('#restbar');
    const rbH = rb && !rb.hidden ? rb.getBoundingClientRect().height : 0;
    const barEl = root.querySelector<HTMLElement>('.sbar');
    const headerH = barEl ? Math.max(52, barEl.getBoundingClientRect().bottom - vpTop) : 58;
    const safeH = Math.max(110, vpH - dkH - rbH - headerH);
    const rect = inp.getBoundingClientRect();
    const targetY = vpTop + headerH + safeH * 0.42;
    const delta = rect.top + rect.height / 2 - targetY;
    if (Math.abs(delta) > 10) window.scrollBy({ top: delta, behavior: 'smooth' });
  };
  const onVvResize = (): void => { if (focused) ensureFieldVisible(focused); };
  window.visualViewport?.addEventListener('resize', onVvResize);
  const syncDockPreset = (): void => {
    if (!focused || !dock().classList.contains('reps')) return;
    const v = Number(focused.value);
    dock().querySelectorAll<HTMLElement>('[data-rep]').forEach((b) => b.classList.toggle('on', Number(b.dataset.rep) === v));
  };
  const showDock = (inp: HTMLInputElement): void => {
    focused = inp;
    const isW = inp.classList.contains('sw');
    const isTime = !isW && exOf(rowOf(inp).se.exId).type === 'time';
    const d = dock();
    if (isW || isTime) {
      const steps = isW ? stepsFor() : [-15, -5, 5, 15];
      d.classList.remove('reps');
      d.innerHTML = `${steps.map((v) => `<button class="dk" data-step="${v}">${v > 0 ? '+' : '−'}${fmtNum(Math.abs(v))}</button>`).join('')}<button class="dk log" data-dock="log">LOG ✓</button>`;
    } else {
      d.classList.add('reps');
      const curV = Number(inp.value);
      d.innerHTML = `${REP_PRESETS.map((p) => `<button class="dk pre${curV === p ? ' on' : ''}" data-rep="${p}">${p}</button>`).join('')}<button class="dk pm" data-step="-1">−1</button><button class="dk pm" data-step="1">+1</button><button class="dk log" data-dock="log">LOG ✓</button>`;
    }
    d.hidden = false;
    document.body.classList.add('dock-open');
  };
  const hideDock = (): void => {
    if (scrollT1) { clearTimeout(scrollT1); scrollT1 = null; }
    if (scrollT2) { clearTimeout(scrollT2); scrollT2 = null; }
    dock().hidden = true;
    dock().classList.remove('reps');
    document.body.classList.remove('dock-open');
    focused = null;
  };
  const rowOf = (el: Element): { card: HTMLElement; row: HTMLElement; se: SessionEx; i: number } => {
    const row = el.closest<HTMLElement>('.srow')!;
    const card = el.closest<HTMLElement>('.xcard')!;
    return { card, row, se: find(card.dataset.uid!), i: Number(row.dataset.i) };
  };

  root.addEventListener('focusin', (e) => {
    const t = e.target as HTMLElement;
    if (!(t instanceof HTMLInputElement) || !t.closest('.srow')) return;
    if (!edit) {
      const { se, i } = rowOf(t);
      const stSet = se.sets[i];
      if (stSet && !stSet.done) voice.setCursor({ exUid: se.uid, setId: stSet.id });
    }
    showDock(t);
    if (scrollT1) clearTimeout(scrollT1);
    if (scrollT2) clearTimeout(scrollT2);
    scrollT1 = setTimeout(() => { t.select(); ensureFieldVisible(t); }, 60);
    scrollT2 = setTimeout(() => { if (focused === t) ensureFieldVisible(t); }, 280);
  });
  root.addEventListener('focusout', () => {
    setTimeout(() => { if (!(document.activeElement instanceof HTMLInputElement && document.activeElement.closest('.srow'))) hideDock(); }, 120);
  });
  root.addEventListener('input', (e) => {
    const t = e.target as HTMLInputElement;
    if (!t.closest?.('.srow')) return;
    const { card, se, i } = rowOf(t);
    const v = num(t.value);
    if (t.classList.contains('sw')) editWeight(se, i, v == null ? null : fromDisplay(v));
    else editReps(se, i, v == null ? null : Math.round(v));
    t.classList.remove('auto');
    syncCard(card);
    syncDockPreset();
    save();
  });
  root.addEventListener('keydown', (e) => {
    const t = e.target as HTMLInputElement;
    if (e.key !== 'Enter' || !t.closest?.('.srow')) return;
    e.preventDefault();
    const inputs = [...root.querySelectorAll<HTMLInputElement>('.srow input')];
    const nxt = inputs[inputs.indexOf(t) + 1];
    if (nxt) nxt.focus(); else t.blur();
  });
  // keep focus in the field while tapping the dock
  root.addEventListener('pointerdown', (e) => { if ((e.target as HTMLElement).closest('.dock')) e.preventDefault(); });

  const logSet = (card: HTMLElement, row: HTMLElement, fromDock: boolean): void => {
    const { se, i } = rowOf(row);
    const idx = s.exs.indexOf(se);
    const ex = exOf(se.exId);
    const set = se.sets[i];
    const was = set.done;
    const shakeIncomplete = (): void => {
      row.classList.remove('shake'); void row.offsetWidth; row.classList.add('shake');
      buzz(40);
      row.querySelector<HTMLInputElement>(!set.r || set.r < 1 ? '.sr' : '.sw')?.focus();
    };
    const commitBuzz = (hadPr: boolean): void => {
      const h = !hadPr ? hits.get(set.id) : undefined;
      if (h) {
        const prW = ex.type === 'bodyweight' && h.kg === 0 ? 'BW' : `${fmtKg(h.kg)} ${unit()}`;
        toast(`New ${h.n}RM PR · ${prW}`, { kind: 'pr', ms: 3200 });
        buzz([60, 40, 60, 40, 120]);
      } else {
        buzz(25);
      }
    };
    if (fromDock && was) {
      if (!canLog(set, ex)) return shakeIncomplete();
      const hadPr = hits.has(set.id);
      recomputePRs();
      commitBuzz(hadPr);
      syncCard(card);
      save();
      focused?.blur();
      hideDock();
      return;
    }
    const r = toggleDone(se, i, ex);
    if (r === null) return shakeIncomplete();
    if (r && !was) {
      if (!edit) {
        active.ensureStarted(s);
        voice.recordLoggedExercise(se.uid);
        voice.setCursor(advanceCursorAfterLog(s.exs, { exIdx: idx, setIdx: i }));
      }
      recomputePRs();
      if (!edit) {
        active.afterSetLogged(s, idx);
        paintClock();
      }
      commitBuzz(false);
    } else {
      recomputePRs();
      if (!r && was && !edit) {
        voice.setCursor({ exUid: se.uid, setId: set.id });
        void active.resetIfNoneLogged(s).then((reset) => { if (reset && !dead) paintClock(); });
      }
    }
    syncCard(card);
    paintTarget();
    save();
    if (r && fromDock) {
      const nextPtr = !edit ? resolveTargetSet(s.exs, voice.cursor) : null;
      const nxtW = nextPtr
        ? root.querySelector<HTMLInputElement>(`.xcard[data-uid="${nextPtr.exUid}"] .srow[data-sid="${nextPtr.setId}"] .sw`)
        : (() => {
            const next = card.querySelectorAll<HTMLElement>('.srow')[i + 1];
            return next && !se.sets[i + 1].done ? next.querySelector<HTMLInputElement>('.sw') : null;
          })();
      if (nxtW) nxtW.focus(); else (document.activeElement as HTMLElement | null)?.blur();
    }
  };

  // ---------- actions ----------
  const snapshot = (): string => JSON.stringify(s.exs);
  const restore = (snap: string): void => { s.exs = JSON.parse(snap); save(); paint(); };
  const defaultSets = (ex: Exercise): number => (ex.type === 'time' ? 2 : 3);

  const exMenu = async (se: SessionEx): Promise<void> => {
    const ex = exOf(se.exId);
    const idx = s.exs.indexOf(se);
    const k = await actionSheet(ex.name, [
      { key: 'swap', label: 'Swap exercise', sub: 'Just today, this day, or everywhere' },
      { key: 'note', label: lifts.row(se.exId)?.note ? 'Edit note' : 'Add note', sub: 'Sticks to the exercise (seat height, handle…)' },
      ...(idx < s.exs.length - 1 ? [{ key: 'ss', label: se.ss ? 'Remove superset with next' : 'Superset with next', sub: 'No rest timer between the pair' }] : []),
      { key: 'rest', label: 'Settings', sub: `${clock(active.restSecFor(se))} · Auto ${active.restAutoFor(s, idx) ? 'on' : 'off'}` },
      ...(idx > 0 ? [{ key: 'up', label: 'Move up' }] : []),
      ...(idx < s.exs.length - 1 ? [{ key: 'down', label: 'Move down' }] : []),
      { key: 'records', label: 'Records & history' },
      { key: 'remove', label: 'Remove exercise', danger: true },
    ]);
    if (!k) return;
    if (k === 'swap') await swap(se);
    else if (k === 'note') await editNote(se);
    else if (k === 'ss') { se.ss = !se.ss; save(); paint(); }
    else if (k === 'rest') await openTimerSheet(se);
    else if (k === 'up' || k === 'down') {
      const j = k === 'up' ? idx - 1 : idx + 1;
      [s.exs[idx], s.exs[j]] = [s.exs[j], s.exs[idx]];
      s.exs.forEach((e, n) => { if (n === s.exs.length - 1) delete e.ss; });
      save(); paint();
    } else if (k === 'records') {
      allowNextRoute();
      location.hash = `#/strength/exercise/${se.exId}`;
    } else if (k === 'remove') {
      const snap = snapshot();
      s.exs.splice(idx, 1);
      if (s.exs.length) delete s.exs[s.exs.length - 1].ss;
      save(); paint();
      toast(`${ex.name} removed`, { undo: () => restore(snap) });
    }
  };

  const editNote = async (se: SessionEx): Promise<void> => {
    const cur = lifts.row(se.exId)?.note ?? '';
    const v = await promptSheet(`${exOf(se.exId).name} note`, cur, 'Seat 4, neutral handles, pin 7…', true);
    if (v == null) return;
    await lifts.saveRow({ ...(lifts.row(se.exId) ?? { id: se.exId }), note: v || undefined });
    paint();
  };

  const openTimerSheet = async (se: SessionEx): Promise<void> => {
    const ex = exOf(se.exId);
    const idx = s.exs.indexOf(se);
    const opts = [30, 45, 60, 90, 120, 150, 180, 240, 300];
    let resolveClosed: () => void = () => {};
    const closed = new Promise<void>((res) => { resolveClosed = res; });
    const sh = openSheet(
      `<h3 class="sh-title">Settings · ${esc(ex.name)}</h3>
       <div class="fl">CARD BUTTONS</div>
       <div class="line"><span>Show timer<small>Show ▶ rest button on exercise cards</small></span><button class="switch" type="button" data-tcfg="showTimer" role="switch"><i></i></button></div>
       <div class="line"><span>Show auto-timer<small>Show Auto toggle pill on exercise cards</small></span><button class="switch" type="button" data-tcfg="showAutoTimer" role="switch"><i></i></button></div>
       <div class="fl">REST DURATION <small>THIS EXERCISE</small></div>
       <div class="chips-row wrap" id="tdur"></div>
       <div class="fl">THIS CARD</div>
       <div class="line"><span>Auto-timer on check<small>Starts countdown automatically when logging a set here</small></span><button class="switch" type="button" data-tcfg="cardAuto" role="switch"><i></i></button></div>
       <div class="fl">ALL EXERCISES (DEFAULTS)</div>
       <div class="line"><span>Auto-timer by default<small>Applies across all cards</small></span><button class="switch" type="button" data-tcfg="masterAuto" role="switch"><i></i></button></div>
       <div class="line"><span>Default duration</span><select class="lf-input sel" id="tdef">${opts.map((n) => `<option value="${n}">${clock(n)}</option>`).join('')}</select></div>
       <div class="line"><span>Rest-over chime</span><button class="switch" type="button" data-tcfg="sound" role="switch"><i></i></button></div>
       ${edit ? '' : '<button class="btn primary big" type="button" data-tnow></button>'}
       <button class="btn big" type="button" data-tok>Done</button>`,
      { onClose: resolveClosed },
    );
    const syncTog = (sel: string, on: boolean): void => {
      const b = sh.body.querySelector<HTMLElement>(sel);
      if (!b) return;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    };
    const draw = (): void => {
      const cur = active.restSecFor(se);
      const ls = liftSettings();
      sh.body.querySelector('#tdur')!.innerHTML = opts
        .map((o) => `<button class="fchip${o === cur ? ' on' : ''}" type="button" data-d="${o}">${clock(o)}</button>`)
        .join('');
      syncTog('[data-tcfg="showTimer"]', ls.showTimer);
      syncTog('[data-tcfg="showAutoTimer"]', ls.showAutoTimer);
      syncTog('[data-tcfg="cardAuto"]', active.restAutoFor(s, idx));
      syncTog('[data-tcfg="masterAuto"]', ls.restAuto);
      syncTog('[data-tcfg="sound"]', ls.restSound);
      sh.body.querySelector<HTMLSelectElement>('#tdef')!.value = String(ls.restSec);
      const nowBtn = sh.body.querySelector<HTMLElement>('[data-tnow]');
      if (nowBtn) nowBtn.textContent = `▶ Start ${clock(cur)} now`;
    };
    draw();
    sh.body.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const d = t.closest<HTMLElement>('[data-d]');
      if (d) {
        delete se.rest;
        await lifts.saveRow({ ...(lifts.row(se.exId) ?? { id: se.exId }), restSec: Number(d.dataset.d) });
        draw();
        return;
      }
      const cfg = t.closest<HTMLElement>('[data-tcfg]')?.dataset.tcfg;
      if (cfg === 'showTimer') {
        updateLiftSettings({ showTimer: !liftSettings().showTimer });
        draw();
      } else if (cfg === 'showAutoTimer') {
        updateLiftSettings({ showAutoTimer: !liftSettings().showAutoTimer });
        draw();
      } else if (cfg === 'cardAuto') {
        const next = !active.restAutoFor(s, idx);
        se.restAuto = next;
        if (!midSuperset(s.exs, idx)) {
          await lifts.saveRow({ ...(lifts.row(se.exId) ?? { id: se.exId }), restAuto: next });
        }
        draw();
      } else if (cfg === 'masterAuto') {
        await lifts.setMasterRestAuto(!liftSettings().restAuto);
        draw();
      } else if (cfg === 'sound') {
        updateLiftSettings({ restSound: !liftSettings().restSound });
        draw();
      } else if (t.closest('[data-tnow]')) {
        if (liftSettings().restSound) audio.unlock();
        active.startRest(active.restSecFor(se), ex.name);
        sh.close();
      } else if (t.closest('[data-tok]')) {
        sh.close();
      }
    });
    sh.body.querySelector('#tdef')!.addEventListener('change', (e) => {
      updateLiftSettings({ restSec: Number((e.target as HTMLSelectElement).value) });
      draw();
    });
    await closed;
    save();
    paint();
  };

  /** Swap with scope: just today / this repeating day / everywhere. */
  const swap = async (se: SessionEx): Promise<void> => {
    const cur = exOf(se.exId);
    const [pick] = await pickExercises({ mode: 'swap', current: cur });
    if (!pick || pick.id === cur.id) return;
    const routine = s.routineId ? lifts.routines.get(s.routineId) : undefined;
    const rIdx = routine ? (se.slot != null && routine.items[se.slot] ? se.slot : routine.items.findIndex((it) => it.exId === cur.id)) : -1;
    const scopes = [
      { key: 'today', label: 'Just today', sub: 'This workout only' },
      ...(routine && !edit && rIdx >= 0 ? [{ key: 'day', label: `This day · ${routine.name}`, sub: `Every ${routine.name} from now on` }] : []),
      ...(!edit ? [{ key: 'all', label: 'Everywhere', sub: 'Every day that uses it' }] : []),
    ];
    const scope = edit ? 'today' : await actionSheet(`${cur.name} → ${pick.name}`, scopes);
    if (!scope) return;
    if (hasLogged(se) && !(await confirmSheet('Clear logged sets?', `${cur.name} has logged sets. Swapping resets them.`, 'Swap anyway', true))) return;
    swapExercise(se, pick.id, lifts.previous(pick.id, s.startMs, s.id));
    if (scope === 'day' && routine && rIdx >= 0) {
      routine.items = routine.items.map((it, n) => (n === rIdx ? { ...it, exId: pick.id } : it));
      await lifts.saveRoutine({ ...routine });
      toast(`${routine.name} now uses ${pick.name}`);
    } else if (scope === 'all') {
      const n = await lifts.replaceEverywhere(cur.id, pick.id);
      toast(`${pick.name} replaces ${cur.name} in ${n} day${n === 1 ? '' : 's'}`);
    }
    save(); paint();
  };

  const addEx = async (): Promise<void> => {
    const picks = await pickExercises({ mode: 'add' });
    if (!picks.length) return;
    if (s.exs.length) delete s.exs[s.exs.length - 1].ss;
    for (const ex of picks) s.exs.push(buildExercise(ex.id, defaultSets(ex), lifts.previous(ex.id, s.startMs, s.id)));
    save(); paint();
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  };

  const setSheet = async (se: SessionEx, i: number): Promise<void> => {
    const set = se.sets[i];
    const ex = exOf(se.exId);
    const n = se.sets.slice(0, i + 1).filter((x) => x.kind !== 'w').length;
    const vids = (): string => (set.vids ?? []).map((v) => lifts.videos.get(v)).filter(Boolean)
      .map((v) => `<button class="vthumb" data-play="${v!.id}" style="${v!.thumb ? `background-image:url(${v!.thumb})` : ''}"><i>▶</i></button>`).join('');
    let sh!: Sheet;
    let resolveClosed: () => void = () => {};
    const closed = new Promise<void>((res) => { resolveClosed = res; });
    sh = openSheet(`
      <h3 class="sh-title">${esc(ex.name)} · ${set.kind === 'w' ? 'Warm-up' : `Set ${n}`}</h3>
      <div class="fl">SET TYPE</div><div class="seg" id="sk"></div>
      <div class="fl">EFFORT <small>RPE</small></div><div class="chips-row wrap" id="sr"></div>
      <div class="fl">NOTE</div><input class="lf-input" id="sn" value="${esc(set.note ?? '')}" placeholder="Optional" autocomplete="off">
      <div class="fl">VIDEO</div>
      <div class="vrow"><div id="sv" class="vthumbs">${vids()}</div></div>
      <button class="btn big" type="button" data-vmanage>Link or Record Video…</button>
      <button class="btn danger big" data-del ${se.sets.length <= 1 ? 'disabled' : ''}>Delete set</button>
      <button class="btn primary big" data-ok>Done</button>`, { onClose: resolveClosed });
    const q = (x: string): HTMLElement => sh.body.querySelector<HTMLElement>(x)!;
    const draw = (): void => {
      q('#sk').innerHTML = (['n', 'w', 'd', 'f'] as SetKind[]).map((k) => `<button class="${set.kind === k ? 'on' : ''}" data-k="${k}">${KIND_LABEL[k]}</button>`).join('');
      q('#sr').innerHTML = RPES.map((r) => `<button class="fchip${set.rpe === r ? ' on' : ''}" data-r="${r}">${r}</button>`).join('') + `<button class="fchip${set.rpe == null ? ' on' : ''}" data-r="0">–</button>`;
      q('#sv').innerHTML = vids();
    };
    draw();
    sh.body.addEventListener('click', async (e) => {
      const t = e.target as HTMLElement;
      const k = t.closest<HTMLElement>('[data-k]');
      const r = t.closest<HTMLElement>('[data-r]');
      const p = t.closest<HTMLElement>('[data-play]');
      if (k) { set.kind = k.dataset.k as SetKind; draw(); }
      else if (r) { const v = Number(r.dataset.r); if (v) set.rpe = v; else delete set.rpe; draw(); }
      else if (p) { await playVideo(p.dataset.play!); draw(); }
      else if (t.closest('[data-vmanage]')) {
        if (!edit) {
          active.ensureStarted(s, true);
          paintClock();
        }
        await openSetVideoSheet({ session: s, se, set }, set.kind === 'w' ? 'Warm-up' : `Set ${n}`);
        draw();
      } else if (t.closest('[data-del]')) {
        const snap = snapshot();
        deleteSet(se, i);
        sh.close();
        save(); paint();
        toast('Set deleted', { undo: () => restore(snap) });
      } else if (t.closest('[data-ok]')) sh.close();
    });
    await closed;
    const note = (sh.body.querySelector<HTMLInputElement>('#sn')?.value ?? set.note ?? '').trim();
    if (note) set.note = note; else delete set.note;
    save(); paint();
  };

  const promptUnfinishedAction = (title: string, msg: string): Promise<'today' | 'tomorrow' | 'delete' | 'cancel'> =>
    new Promise((resolve) => {
      let out: 'today' | 'tomorrow' | 'delete' | 'cancel' = 'cancel';
      const sh = openSheet(
        `<h3 class="sh-title">${esc(title)}</h3><p class="sh-text">${esc(msg)}</p>
         <button class="btn primary big" type="button" data-uf="today">Do later today</button>
         <button class="btn big" type="button" data-uf="tomorrow">Do tomorrow</button>
         <button class="btn danger big" type="button" data-uf="delete">Delete unfinished sets &amp; finish</button>
         <button class="btn big" type="button" data-uf="cancel">Keep lifting</button>`,
        { onClose: () => resolve(out) },
      );
      sh.body.addEventListener('click', (ev) => {
        const uf = (ev.target as HTMLElement).closest<HTMLElement>('[data-uf]')?.dataset.uf as typeof out | undefined;
        if (!uf) return;
        out = uf;
        sh.close();
      });
    });

  const finish = async (skipConfirm = false): Promise<boolean> => {
    if (edit) { allowNextRoute(); location.hash = `#/strength/workout/${s.id}`; return true; }
    if (!s.exs.some(hasLogged)) {
      if (await confirmSheet('Nothing logged', 'No sets are logged yet. Discard this workout?', 'Discard workout', true, 'Keep lifting')) {
        finishing = true;
        allowNextRoute();
        await active.discard();
        location.hash = '#/strength';
        return true;
      }
      return false;
    }
    if (!skipConfirm) {
      const unfinished = collectUnfinishedItems(s);
      if (unfinished.length > 0) {
        const leftSets = unfinished.reduce((n, x) => n + x.sets, 0);
        const summary = unfinished.map((u) => `${exOf(u.exId).name} (${u.sets})`).join(' · ');
        const choice = await promptUnfinishedAction(
          'There are unfinished sets',
          `${leftSets} unfinished set${leftSets === 1 ? '' : 's'}: ${summary}. Delete them or do them later?`,
        );
        if (choice === 'cancel') return false;
        if (choice === 'today' || choice === 'tomorrow') {
          calendar.scheduleSkippedMakeup({
            sessionId: s.id,
            name: s.name,
            items: unfinished,
            when: choice,
            description: `Remaining from ${s.name}: ${summary}`,
          });
          toast(choice === 'today' ? 'Scheduled remaining sets for later today' : 'Scheduled remaining sets for tomorrow');
        }
      } else {
        const ok = await confirmSheet('Finish workout?', 'It uploads to Intervals.icu automatically.', 'Finish & save', false, 'Keep lifting');
        if (!ok) return false;
      }
    }
    finishing = true;
    allowNextRoute();
    const done = await active.finish();
    location.replace(done ? `#/strength/summary/${done.session.id}` : '#/strength');
    return true;
  };

  const wkMenu = async (): Promise<void> => {
    const wbSt = wallboard.status;
    const k = await actionSheet(s.name, [
      { key: 'rename', label: 'Rename workout' },
      { key: 'note', label: s.note ? 'Edit workout note' : 'Add workout note' },
      ...(!edit ? [{ key: 'wallboard', label: wbSt.running ? `TV Wallboard · ${wbSt.url}` : 'TV Wallboard (Gym Display)', sub: wbSt.running ? `${wbSt.clients} TV${wbSt.clients === 1 ? '' : 's'} connected` : 'Stream live HR, rest timer & next set over Wi-Fi' }] : []),
      { key: 'del', label: edit ? 'Delete workout' : 'Discard workout', danger: true },
    ]);
    if (k === 'rename') await rename();
    else if (k === 'note') {
      const v = await promptSheet('Workout note', s.note ?? '', 'How it felt…', true);
      if (v != null) { if (v) s.note = v; else delete s.note; save(); }
    } else if (k === 'wallboard') {
      const sh = openSheet(`
        <h3 class="sh-title">Smart TV Wallboard</h3>
        <p class="sh-text" id="wbSheetInfo">${wallboard.status.running ? `Live on your gym Wi-Fi at <b>${esc(wallboard.status.url)}</b> (${wallboard.status.clients} connected).` : 'Broadcast live HR readiness, rest timer, and superset progression to your gym TV browser.'}</p>
        <button class="btn primary big" type="button" data-wbs="tog">${getSettings().wallboardEnabled ? 'Turn off TV Wallboard' : 'Turn on TV Wallboard'}</button>
        <button class="btn big" type="button" data-wbs="copy">Copy TV URL</button>
        <a class="btn big" href="#/wallboard" data-wbs="open">Open TV Setup &amp; Preview ›</a>`);
      sh.body.addEventListener('click', (ev) => {
        const wbs = (ev.target as HTMLElement).closest<HTMLElement>('[data-wbs]')?.dataset.wbs;
        if (wbs === 'tog') {
          void wallboard.setEnabled(!getSettings().wallboardEnabled).then((st) => {
            sh.body.querySelector('#wbSheetInfo')!.innerHTML = st.running ? `Live at <b>${esc(st.url)}</b>` : 'TV Wallboard turned off';
            sh.body.querySelector('[data-wbs="tog"]')!.textContent = st.running ? 'Turn off TV Wallboard' : 'Turn on TV Wallboard';
          });
        } else if (wbs === 'copy') {
          void navigator.clipboard?.writeText(wallboard.status.url).then(() => toast(`Copied ${wallboard.status.url}`));
        } else if (wbs === 'open') {
          sh.close();
        }
      });
    } else if (k === 'del') {
      if (!(await confirmSheet(edit ? 'Delete workout?' : 'Discard workout?', edit ? 'Removes it from history (not from Intervals.icu).' : 'Everything logged so far is lost.', edit ? 'Delete' : 'Discard', true))) return;
      finishing = true;
      allowNextRoute();
      if (edit) { await lifts.deleteSession(s.id); location.hash = '#/strength/history'; }
      else { await active.discard(); location.hash = '#/strength'; }
    }
  };
  const rename = async (): Promise<void> => {
    const v = await promptSheet('Workout name', s.name);
    if (v) { s.name = v; save(); const b = root.querySelector('.stitle b'); if (b) b.textContent = v; }
  };

  root.addEventListener('click', async (e) => {
    const t = e.target as HTMLElement;
    const repBtn = t.closest<HTMLElement>('[data-rep]');
    if (repBtn && focused) {
      focused.value = repBtn.dataset.rep!;
      focused.dispatchEvent(new Event('input', { bubbles: true }));
      syncDockPreset();
      focused.select?.();
      return;
    }
    const step = t.closest<HTMLElement>('[data-step]');
    if (step && focused) {
      const isW = focused.classList.contains('sw');
      const cur = num(focused.value) ?? 0;
      const nv = Math.max(0, Math.round((cur + Number(step.dataset.step)) * 100) / 100);
      focused.value = fmtNum(isW ? nv : Math.round(nv));
      focused.dispatchEvent(new Event('input', { bubbles: true }));
      syncDockPreset();
      focused.select?.();
      return;
    }
    if (t.closest('[data-dock="log"]') && focused) {
      const row = focused.closest<HTMLElement>('.srow')!;
      logSet(row.closest<HTMLElement>('.xcard')!, row, true);
      return;
    }
    const rest = t.closest<HTMLElement>('[data-rest]');
    if (rest) {
      const v = Number(rest.dataset.rest);
      if (v === 0) active.clearRest(); else active.adjustRest(v);
      return;
    }
    const a = t.closest<HTMLElement>('[data-act]');
    if (!a) return;
    const act = a.dataset.act!;
    if (act === 'addex') return void (await addEx());
    if (act === 'finish') return void (await finish());
    if (act === 'rename') return void (await rename());
    if (act === 'wkmenu') return void (await wkMenu());
    if (act === 'start' || act === 'resume') {
      if (!edit) {
        if (liftSettings().restSound) audio.unlock();
        if (act === 'start') active.ensureStarted(s, true);
        else active.resume();
        buzz(25);
        paintClock();
      }
      return;
    }
    if (act === 'pause') {
      if (!edit && active.pause()) {
        buzz(20);
        paintClock();
      }
      return;
    }
    if (act === 'voice') {
      if (!edit) {
        void voice.setEnabled(!getSettings().voiceEnabled).then(() => paintMic());
      }
      return;
    }
    const card = a.closest<HTMLElement>('.xcard');
    if (!card) return;
    const se = find(card.dataset.uid!);
    if (act === 'exmenu') return void (await exMenu(se));
    if (act === 'exnote') return void (await editNote(se));
    if (act === 'reststart') {
      if (!edit) active.ensureStarted(s, true);
      if (liftSettings().restSound) audio.unlock();
      active.startRest(active.restSecFor(se), exOf(se.exId).name);
      buzz(20);
      paintClock();
      return;
    }
    if (act === 'restauto') {
      const idx = s.exs.indexOf(se);
      const next = !active.restAutoFor(s, idx);
      se.restAuto = next;
      if (!midSuperset(s.exs, idx)) {
        await lifts.saveRow({ ...(lifts.row(se.exId) ?? { id: se.exId }), restAuto: next });
      }
      save();
      a.classList.toggle('on', next);
      a.setAttribute('aria-pressed', String(next));
      buzz(15);
      return;
    }
    if (act === 'addset') {
      addSet(se, lifts.previous(se.exId, s.startMs, s.id));
      save(); paint();
      return;
    }
    if (act === 'rmset') {
      const snap = snapshot();
      if (!removeLastSet(se)) return void toast('A card needs at least one set');
      save(); paint();
      toast('Set removed', { undo: () => restore(snap) });
      return;
    }
    const row = a.closest<HTMLElement>('.srow')!;
    const i = Number(row.dataset.i);
    if (act === 'hold') { startHold(se, i, card, row); return; }
    if (act === 'done') return logSet(card, row, false);
    if (act === 'vid') {
      if (!edit) {
        active.ensureStarted(s, true);
        paintClock();
      }
      const n = se.sets.slice(0, i + 1).filter((x) => x.kind !== 'w').length;
      await openSetVideoSheet({ session: s, se, set: se.sets[i] }, se.sets[i].kind === 'w' ? 'Warm-up' : `Set ${n}`);
      if (!dead) { save(); paint(); }
      return;
    }
    if (act === 'setmenu') return void (await setSheet(se, i));
    if (act === 'usePrev') {
      const p = prevSetFor(prevOf(se), se, i);
      if (!p) return;
      se.sets[i].w = p.w; se.sets[i].r = p.r;
      se.sets[i].wAuto = false; se.sets[i].rAuto = false;
      syncCard(card); save();
    }
  });

  paint();

  return () => {
    dead = true;
    stopHold();
    window.visualViewport?.removeEventListener('resize', onVvResize);
    offTick(); offRest(); offChips(); offCfg(); offStore(); offVoice();
    if (!edit) {
      voice.holdStarter = null;
      voice.setLiftScreenMounted(false);
    }
    hideDock();
    void lifts.flush();
    if (edit && dirty) void resyncSession(s.id);
  };
}
