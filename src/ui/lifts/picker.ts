import { DEFAULT_CAM_OPTIONS, defaultCamsForExercise, normalizeCamList } from '../../lifts/logic';
import { EQUIPS, EQUIP_LABEL, GROUPS, MUSCLE_LABEL } from '../../lifts/muscles';
import { lifts } from '../../lifts/store';
import { shortCamLabel } from '../../lifts/tape';
import { gymCam } from '../../lifts/gymCam';
import { MUSCLES, type Equip, type ExType, type Exercise, type Muscle } from '../../lifts/types';
import { esc } from '../dom';
import { openSheet, toast } from './kit';
import { LIBRARY_BY_ID } from '../../lifts/library';

const musclesLine = (e: Exercise): string => {
  const p = e.primary.map((m) => MUSCLE_LABEL[m]);
  return p.length ? p.join(' · ') : EQUIP_LABEL[e.equip];
};

/** Recently used lifts (most recent first), from history + the live session. */
function recentIds(limit = 8): string[] {
  const seen = new Set<string>();
  const sessions = [...lifts.sessions.values()].sort((a, b) => b.startMs - a.startMs);
  for (const s of sessions) for (const e of s.exs) { if (seen.size < limit && lifts.exercise(e.exId) && !lifts.exercise(e.exId)!.archived) seen.add(e.exId); }
  return [...seen];
}

function similarTo(cur: Exercise): Exercise[] {
  const score = (e: Exercise): number => {
    if (e.id === cur.id) return -1;
    const shared = e.primary.filter((m) => cur.primary.includes(m)).length;
    if (!shared) return 0;
    return shared * 10 + (e.equip === cur.equip ? 2 : 0) + e.secondary.filter((m) => cur.secondary.includes(m)).length;
  };
  return lifts.allExercises().map((e) => [e, score(e)] as const).filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]).slice(0, 12).map(([e]) => e);
}

export interface PickOpts {
  mode: 'add' | 'swap';
  current?: Exercise;
}

/** Master list + custom. Add mode returns any number of lifts; swap mode returns one. Empty = dismissed. */
export function pickExercises(o: PickOpts): Promise<Exercise[]> {
  return new Promise((resolve) => {
    const swap = o.mode === 'swap';
    const picked = new Map<string, Exercise>();
    let result: Exercise[] = [];
    let q = '';
    let group = '';
    const equips = new Set<Equip>();

    const sh = openSheet(
      `<div class="pk-head"><h3 class="sh-title">${swap ? `Swap ${esc(o.current?.name ?? '')}` : 'Add exercises'}</h3><button class="link" data-x>Cancel</button></div>
       <input class="lf-input" id="pq" placeholder="Search exercises" autocomplete="off" enterkeyhint="search">
       <div class="chips-row" id="pg"></div>
       <div class="chips-row" id="pe"></div>
       <div class="pk-list" id="pl"></div>
       <div class="pk-foot"><button class="btn" data-new>+ Custom</button>${swap ? '' : '<button class="btn primary" data-add disabled>Add</button>'}</div>`,
      { full: true, onClose: () => resolve(result) },
    );
    const $q = sh.body.querySelector<HTMLInputElement>('#pq')!;
    const $g = sh.body.querySelector<HTMLElement>('#pg')!;
    const $e = sh.body.querySelector<HTMLElement>('#pe')!;
    const $l = sh.body.querySelector<HTMLElement>('#pl')!;
    const $add = sh.body.querySelector<HTMLButtonElement>('[data-add]');

    const chip = (k: string, label: string, on: boolean): string => `<button class="fchip${on ? ' on' : ''}" data-k="${k}">${label}</button>`;
    const drawChips = (): void => {
      $g.innerHTML = chip('', 'All', !group) + GROUPS.map((g) => chip(g.id, g.label, group === g.id)).join('');
      $e.innerHTML = EQUIPS.map((x) => chip(x, EQUIP_LABEL[x], equips.has(x))).join('');
    };
    const row = (e: Exercise): string =>
      `<button class="pk-item${picked.has(e.id) ? ' sel' : ''}" data-id="${e.id}"><span class="pk-main"><b>${esc(e.name)}${e.custom ? ' <em>CUSTOM</em>' : ''}</b><small>${esc(musclesLine(e))} · ${EQUIP_LABEL[e.equip]}</small></span><i class="pk-chk">${picked.has(e.id) ? '✓' : ''}</i></button>`;

    const draw = (): void => {
      const filtered = !!(q || group || equips.size);
      const g = GROUPS.find((x) => x.id === group);
      const match = (e: Exercise): boolean => {
        if (q) {
          const hay = (lifts.exercise(e.id)?.name ?? '').toLowerCase() + ' ' + (aliasOf(e.id));
          if (!q.split(/\s+/).every((t) => hay.includes(t))) return false;
        }
        if (g && !e.primary.some((m) => g.muscles.includes(m))) return false;
        if (equips.size && !equips.has(e.equip)) return false;
        return true;
      };
      const all = lifts.allExercises();
      let html = '';
      if (filtered) {
        const list = all.filter(match);
        html = list.length ? list.map(row).join('') : '<div class="empty">No match. Tap + Custom to create it.</div>';
      } else {
        if (swap && o.current) {
          const sim = similarTo(o.current);
          if (sim.length) html += `<h4 class="pk-sec">SIMILAR</h4>${sim.map(row).join('')}`;
        } else {
          const rec = recentIds().map((id) => lifts.exercise(id)!).filter(Boolean);
          if (rec.length) html += `<h4 class="pk-sec">RECENT</h4>${rec.map(row).join('')}`;
        }
        html += `<h4 class="pk-sec">ALL EXERCISES</h4>${all.map(row).join('')}`;
      }
      $l.innerHTML = html;
      if ($add) {
        $add.disabled = picked.size === 0;
        $add.textContent = picked.size ? `Add ${picked.size}` : 'Add';
      }
    };

    const choose = (e: Exercise): void => {
      if (swap) { result = [e]; sh.close(); return; }
      if (picked.has(e.id)) picked.delete(e.id); else picked.set(e.id, e);
      draw();
    };

    $q.addEventListener('input', () => { q = $q.value.trim().toLowerCase(); draw(); });
    $g.addEventListener('click', (ev) => { const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-k]'); if (!b) return; group = b.dataset.k!; drawChips(); draw(); });
    $e.addEventListener('click', (ev) => {
      const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-k]');
      if (!b) return;
      const k = b.dataset.k as Equip;
      if (equips.has(k)) equips.delete(k); else equips.add(k);
      drawChips();
      draw();
    });
    $l.addEventListener('click', (ev) => {
      const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-id]');
      const e = b && lifts.exercise(b.dataset.id!);
      if (e) choose(e);
    });
    sh.body.querySelector('[data-x]')!.addEventListener('click', () => sh.close());
    $add?.addEventListener('click', () => { result = [...picked.values()]; sh.close(); });
    sh.body.querySelector('[data-new]')!.addEventListener('click', async () => {
      const ex = await exerciseForm(null, q ? q.replace(/\b\w/g, (c) => c.toUpperCase()) : '');
      if (ex) { toast(`${ex.name} created`); choose(ex); }
    });
    drawChips();
    draw();
  });
}

const aliasOf = (id: string): string => LIBRARY_BY_ID.get(id)?.haystack ?? '';

/** Create (or edit) a custom exercise (with mandatory 1+ camera selection). Resolves with the saved exercise, or null. */
export function exerciseForm(existing: Exercise | null, presetName = ''): Promise<Exercise | null> {
  return new Promise((resolve) => {
    let out: Exercise | null = null;
    let type: ExType = existing?.type ?? 'weight';
    let equip: Equip = existing?.equip ?? 'other';
    const existingRow = existing ? lifts.row(existing.id) : undefined;
    const allCamOpts: string[] = [...DEFAULT_CAM_OPTIONS];
    for (const c of gymCam.cameras) {
      if (!allCamOpts.some((x) => x.toLowerCase() === c.toLowerCase())) allCamOpts.push(c);
    }
    const existingCams = existingRow ? normalizeCamList(existingRow.cams ?? existingRow.camPref) : [];
    const selectedCams = new Set<string>(
      existingCams.length
        ? existingCams
        : defaultCamsForExercise({ id: existing?.id ?? '', name: existing?.name ?? presetName, equip }),
    );
    let camsTouched = existingCams.length > 0;
    const prim = new Set<Muscle>(existing?.primary ?? []);
    const sec = new Set<Muscle>(existing?.secondary ?? []);
    const sh = openSheet(
      `<h3 class="sh-title">${existing ? 'Edit exercise' : 'Custom exercise'}</h3>
       <input class="lf-input" id="cn" placeholder="Name" value="${esc(existing?.name ?? presetName)}" autocomplete="off">
       <div class="fl">LOGGED AS</div><div class="seg" id="ct"></div>
       <div class="fl">EQUIPMENT</div><div class="chips-row wrap" id="ce"></div>
       <div class="fl">CAMERAS <small>Required · pick 1 or more</small></div><div class="chips-row wrap" id="cc"></div>
       <div class="fl">PRIMARY MUSCLES <small>1.0 set each</small></div><div class="chips-row wrap" id="cp"></div>
       <div class="fl">SECONDARY MUSCLES <small>0.5 set each</small></div><div class="chips-row wrap" id="cs"></div>
       <button class="btn primary big" data-save disabled>Save exercise</button>
       <button class="btn big" data-no>Cancel</button>`,
      { full: true, onClose: () => resolve(out) },
    );
    const $ = (s: string): HTMLElement => sh.body.querySelector<HTMLElement>(s)!;
    const save = $('[data-save]') as HTMLButtonElement;
    const name = $('#cn') as HTMLInputElement;
    const chip = (k: string, label: string, on: boolean): string => `<button class="fchip${on ? ' on' : ''}" data-k="${k}">${label}</button>`;
    const canSave = (): boolean => !!name.value.trim() && prim.size > 0 && selectedCams.size > 0;
    const draw = (): void => {
      $('#ct').innerHTML = ([['weight', 'Weight × reps'], ['bodyweight', 'Bodyweight'], ['time', 'Time']] as [ExType, string][]).map(([k, l]) => `<button class="${type === k ? 'on' : ''}" data-k="${k}">${l}</button>`).join('');
      $('#ce').innerHTML = EQUIPS.map((k) => chip(k, EQUIP_LABEL[k], equip === k)).join('');
      $('#cc').innerHTML = allCamOpts.map((c) => chip(esc(c), esc(shortCamLabel(c)), selectedCams.has(c))).join('');
      $('#cp').innerHTML = MUSCLES.map((m) => chip(m, MUSCLE_LABEL[m], prim.has(m))).join('');
      $('#cs').innerHTML = MUSCLES.map((m) => chip(m, MUSCLE_LABEL[m], sec.has(m))).join('');
      save.disabled = !canSave();
    };
    const toggle = (set: Set<Muscle>, other: Set<Muscle>, k: Muscle): void => {
      if (set.has(k)) set.delete(k); else { set.add(k); other.delete(k); }
    };
    const syncDefaultCamsIfUntouched = (): void => {
      if (camsTouched) return;
      selectedCams.clear();
      defaultCamsForExercise({ id: existing?.id ?? '', name: name.value.trim(), equip }).forEach((c) => selectedCams.add(c));
    };
    $('#ct').addEventListener('click', (e) => { const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]'); if (b) { type = b.dataset.k as ExType; draw(); } });
    $('#ce').addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]');
      if (b) { equip = b.dataset.k as Equip; syncDefaultCamsIfUntouched(); draw(); }
    });
    $('#cc').addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]');
      if (!b) return;
      const cam = b.dataset.k!;
      camsTouched = true;
      if (selectedCams.has(cam)) {
        if (selectedCams.size <= 1) { toast('Select at least 1 camera'); return; }
        selectedCams.delete(cam);
      } else {
        selectedCams.add(cam);
      }
      draw();
    });
    $('#cp').addEventListener('click', (e) => { const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]'); if (b) { toggle(prim, sec, b.dataset.k as Muscle); draw(); } });
    $('#cs').addEventListener('click', (e) => { const b = (e.target as HTMLElement).closest<HTMLElement>('[data-k]'); if (b) { toggle(sec, prim, b.dataset.k as Muscle); draw(); } });
    name.addEventListener('input', () => { syncDefaultCamsIfUntouched(); save.disabled = !canSave(); });
    $('[data-no]').addEventListener('click', () => sh.close());
    save.addEventListener('click', async () => {
      if (selectedCams.size === 0) { toast('Select at least 1 camera'); return; }
      const cams = [...selectedCams];
      const def = { name: name.value.trim(), type, equip, primary: [...prim], secondary: [...sec] };
      if (existing) {
        const nextRow = { ...(lifts.row(existing.id) ?? { id: existing.id }), ...(existing.custom ? { custom: true } : {}), ...def, cams };
        delete nextRow.camPref;
        await lifts.saveRow(nextRow);
        out = lifts.exercise(existing.id)!;
      } else out = await lifts.createCustom({ ...def, cams });
      sh.close();
    });
    draw();
  });
}
