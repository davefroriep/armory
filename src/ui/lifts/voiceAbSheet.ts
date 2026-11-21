import { getSettings } from '../../store/settings';
import { voice, type AbMicSample } from '../../lifts/voice';
import { AB_SHOOTOUT_PHRASES, voiceTokenMatchPct } from '../../lifts/voiceParser';
import { esc } from '../dom';
import { openSheet, toast } from './kit';

interface ShootoutItemState {
  phrase: string;
  expectedIntentKey: string;
  expectedLabel: string;
  room: AbMicSample | null;
  phone: AbMicSample | null;
  roomMatch: boolean;
  phoneMatch: boolean;
  roomWordPct: number;
  phoneWordPct: number;
}

/** Opens the Guided 10-Phrase Dual-Mic Shootout comparing PowerConf S330 vs Pixel Phone Mic. */
export async function openVoiceAbShootoutSheet(onDone?: () => void): Promise<void> {
  const s = getSettings();
  const wakeRaw = (s.voiceWakeWord || 'armory').trim() || 'armory';
  const wakeDisp = wakeRaw.charAt(0).toUpperCase() + wakeRaw.slice(1);
  const items: ShootoutItemState[] = AB_SHOOTOUT_PHRASES.map((p) => ({
    phrase: wakeRaw.toLowerCase() === 'armory' ? p.phrase : p.phrase.replace(/^Armory\b/, wakeDisp),
    expectedIntentKey: p.expectedIntentKey,
    expectedLabel: p.expectedLabel,
    room: null,
    phone: null,
    roomMatch: false,
    phoneMatch: false,
    roomWordPct: 0,
    phoneWordPct: 0,
  }));

  let idx = 0;
  let showScorecard = false;
  let savedRunId: string | null = null;
  let saving = false;
  let label = 'Walking Desk';

  let offVx = () => {};
  const sh = openSheet('<div id="abSheetRoot"></div>', {
    full: true,
    onClose: () => {
      offVx();
      voice.stopShootout();
      onDone?.();
    },
  });

  const stats = () => {
    const tested = items.filter((it) => it.room !== null || it.phone !== null);
    const denom = tested.length || items.length;
    const roomCorrect = tested.filter((it) => it.roomMatch).length;
    const phoneCorrect = tested.filter((it) => it.phoneMatch).length;
    const roomIntentPct = Math.round((roomCorrect / denom) * 1000) / 10;
    const phoneIntentPct = Math.round((phoneCorrect / denom) * 1000) / 10;
    const deltaPts = Math.round((roomIntentPct - phoneIntentPct) * 10) / 10;
    const relPct = phoneIntentPct > 0 ? Math.round(((roomIntentPct - phoneIntentPct) / phoneIntentPct) * 1000) / 10 : null;
    const roomWordPct = tested.length
      ? Math.round((tested.reduce((sum, it) => sum + it.roomWordPct, 0) / tested.length) * 10) / 10
      : 0;
    const phoneWordPct = tested.length
      ? Math.round((tested.reduce((sum, it) => sum + it.phoneWordPct, 0) / tested.length) * 10) / 10
      : 0;
    return { testedCount: tested.length, denom, roomCorrect, phoneCorrect, roomIntentPct, phoneIntentPct, deltaPts, relPct, roomWordPct, phoneWordPct };
  };

  const renderSideCard = (title: string, sample: AbMicSample | null, matched: boolean, wordPct: number, live: boolean, subOff: string): string => {
    const statusTxt = sample
      ? (matched ? '✓ MATCH' : '✗ MISS')
      : live ? '● Listening…' : subOff;
    const statusCol = sample
      ? (matched ? '#86efac' : '#fca5a5')
      : live ? '#7cc4ff' : 'var(--mut)';
    const borderCol = sample ? (matched ? 'rgba(34,197,94,.45)' : 'rgba(239,68,68,.45)') : 'var(--line)';
    return `<div style="padding:10px;border-radius:12px;background:var(--card2);border:1px solid ${borderCol}">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px">
        <b style="font-size:.86rem">${esc(title)}</b>
        <span style="font-size:.74rem;font-weight:800;color:${statusCol}">${esc(statusTxt)}</span>
      </div>
      <div style="font-size:.9rem;min-height:20px">${sample ? `“${esc(sample.transcript)}”` : '<span style="color:var(--mut)">Waiting for speech…</span>'}</div>
      <div style="font-size:.74rem;color:var(--mut);margin-top:4px">${sample ? `${esc(sample.intentLabel)} · Word match ${wordPct}%` : ''}</div>
    </div>`;
  };

  const render = (): void => {
    const rootEl = sh.body.querySelector('#abSheetRoot') as HTMLElement | null;
    if (!rootEl) return;
    const focusedId = (document.activeElement as HTMLElement | null)?.id;
    const cur = items[idx];
    const st = stats();
    const roomLive = voice.roomListening;
    const phoneLive = voice.listening;

    if (showScorecard) {
      const sign = st.deltaPts > 0 ? '+' : '';
      const relStr = st.relPct != null ? ` (${st.relPct > 0 ? '+' : ''}${st.relPct}% relative)` : '';
      const runsHtml = voice.abRuns.length
        ? `<div class="fl" style="margin-top:12px">SAVED PLACEMENT RUNS (DESK VS. CEILING)</div>
           <div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">
             ${voice.abRuns.slice(0, 6).map((r) => {
               const dSign = r.deltaPts > 0 ? '+' : '';
               return `<div class="aitem" style="cursor:default">
                 <b>${esc(r.label)} · Room ${r.roomIntentPct}% vs Phone ${r.phoneIntentPct}% (${dSign}${r.deltaPts} pts)</b>
                 <small>${r.phrasesCount} phrases · Word match Room ${r.roomWordPct}% vs Phone ${r.phoneWordPct}%</small>
               </div>`;
             }).join('')}
           </div>`
        : '';

      rootEl.innerHTML = `
        <h3 class="sh-title">10-Phrase Shootout Scorecard</h3>
        <p class="sh-text">Placement: <b>${esc(label)}</b> · ${st.testedCount}/${items.length} phrases tested</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0">
          <div style="padding:10px;border-radius:12px;background:var(--card2);border:1px solid var(--line)">
            <small style="color:var(--mut)">ROOM MIC (S330)</small>
            <div style="font-size:1.35rem;font-weight:900;color:#86efac">${st.roomIntentPct}%</div>
            <small style="color:var(--mut)">${st.roomCorrect}/${st.denom} intents · ${st.roomWordPct}% words</small>
          </div>
          <div style="padding:10px;border-radius:12px;background:var(--card2);border:1px solid var(--line)">
            <small style="color:var(--mut)">PHONE MIC (PIXEL)</small>
            <div style="font-size:1.35rem;font-weight:900;color:#7dd3fc">${st.phoneIntentPct}%</div>
            <small style="color:var(--mut)">${st.phoneCorrect}/${st.denom} intents · ${st.phoneWordPct}% words</small>
          </div>
        </div>
        <div style="padding:10px;border-radius:12px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.35);margin-bottom:10px">
          <b>Net Improvement: ${sign}${st.deltaPts} percentage points${esc(relStr)}</b>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;max-height:190px;overflow:auto;font-size:.78rem">
          ${items.map((it, n) => `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--line)">
            <span>${n + 1}. ${esc(it.phrase)}</span>
            <span>R:${it.room ? (it.roomMatch ? '✓' : '✗') : '–'} · P:${it.phone ? (it.phoneMatch ? '✓' : '✗') : '–'}</span>
          </div>`).join('')}
        </div>
        ${runsHtml}
        <div class="vp-foot-row" style="margin-top:12px">
          <button class="btn primary" type="button" data-ab="save" ${saving || !!savedRunId || !st.testedCount ? 'disabled' : ''}>${savedRunId ? '✓ Saved to OptiPlex' : saving ? 'Saving…' : 'Save Scorecard to OptiPlex'}</button>
          <button class="btn" type="button" data-ab="back">← Back to Phrases</button>
        </div>
        <button class="btn big" type="button" data-ab="close" style="margin-top:8px">Close</button>`;
      return;
    }

    rootEl.innerHTML = `
      <h3 class="sh-title">10-Phrase Dual-Mic Shootout</h3>
      <p class="sh-text">Speak naturally from your rack, Smith, or treadmill. Both mics score simultaneously without editing workouts.</p>
      <label class="field" style="margin:6px 0">Placement Label<input id="abLabelInp" class="lf-input" value="${esc(label)}" placeholder="Walking Desk"></label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
        <button class="fchip${label === 'Walking Desk' ? ' on' : ''}" type="button" data-abpreset="Walking Desk">Walking Desk</button>
        <button class="fchip${label === 'Ceiling Mount' ? ' on' : ''}" type="button" data-abpreset="Ceiling Mount">Ceiling Mount</button>
        <button class="fchip${label === 'Rack + Music' ? ' on' : ''}" type="button" data-abpreset="Rack + Music">Rack + Music</button>
      </div>
      <div style="padding:12px;border-radius:14px;background:var(--card2);border:1px solid var(--blue);margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:.76rem;color:var(--mut)">
          <span>PHRASE ${idx + 1} OF ${items.length}</span>
          <span>Target: ${esc(cur.expectedLabel)}</span>
        </div>
        <div style="font-size:1.25rem;font-weight:900;margin-top:4px">“${esc(cur.phrase)}”</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px">
        ${renderSideCard('Room Mic · PowerConf S330', cur.room, cur.roomMatch, cur.roomWordPct, roomLive, voice.roomOnline ? 'Opening S330…' : 'OptiPlex offline')}
        ${renderSideCard('Phone Mic · Pixel', cur.phone, cur.phoneMatch, cur.phoneWordPct, phoneLive, 'Starting Pixel mic…')}
      </div>
      <div class="vp-foot-row">
        <button class="btn sm" type="button" data-ab="prev" ${idx === 0 ? 'disabled' : ''}>← Prev</button>
        <button class="btn sm" type="button" data-ab="retry">Retry Phrase</button>
        <button class="btn sm primary" type="button" data-ab="next">${idx === items.length - 1 ? 'Finish & Score →' : 'Next Phrase →'}</button>
      </div>
      <div class="vp-foot-row" style="margin-top:8px">
        <button class="btn sm" type="button" data-ab="score" ${st.testedCount === 0 ? 'disabled' : ''}>View Scorecard (${st.testedCount}/${items.length})</button>
        <button class="btn sm" type="button" data-ab="close">Close</button>
      </div>`;

    if (focusedId === 'abLabelInp') {
      const inp = rootEl.querySelector<HTMLInputElement>('#abLabelInp');
      inp?.focus();
    }
  };

  sh.body.addEventListener('input', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.id === 'abLabelInp') label = t.value;
  });

  sh.body.addEventListener('click', async (ev) => {
    const target = ev.target as HTMLElement;
    const pre = target.closest<HTMLElement>('[data-abpreset]');
    if (pre) {
      label = pre.dataset.abpreset || 'Walking Desk';
      render();
      return;
    }
    const act = target.closest<HTMLElement>('[data-ab]')?.dataset.ab;
    if (!act) return;
    if (act === 'close') { sh.close(); return; }
    if (act === 'prev' && idx > 0) { idx -= 1; render(); return; }
    if (act === 'retry') {
      const cur = items[idx];
      cur.room = null;
      cur.phone = null;
      cur.roomMatch = false;
      cur.phoneMatch = false;
      cur.roomWordPct = 0;
      cur.phoneWordPct = 0;
      savedRunId = null;
      render();
      return;
    }
    if (act === 'next') {
      if (idx < items.length - 1) idx += 1;
      else showScorecard = true;
      render();
      return;
    }
    if (act === 'score') { showScorecard = true; render(); return; }
    if (act === 'back') { showScorecard = false; render(); return; }
    if (act === 'save') {
      saving = true;
      render();
      const st = stats();
      const saved = await voice.saveShootoutRun({
        tsMs: Date.now(),
        label: label.trim() || 'Shootout Run',
        phrasesCount: st.denom,
        roomIntentPct: st.roomIntentPct,
        phoneIntentPct: st.phoneIntentPct,
        roomWordPct: st.roomWordPct,
        phoneWordPct: st.phoneWordPct,
        items: items.map((it) => ({
          phrase: it.phrase,
          expectedIntentKey: it.expectedIntentKey,
          roomTranscript: it.room?.transcript ?? '',
          phoneTranscript: it.phone?.transcript ?? '',
          roomMatch: it.roomMatch,
          phoneMatch: it.phoneMatch,
          roomWordPct: it.roomWordPct,
          phoneWordPct: it.phoneWordPct,
        })),
      });
      saving = false;
      if (saved) {
        savedRunId = saved.id;
        toast(`Saved shootout: Room ${st.roomIntentPct}% vs Phone ${st.phoneIntentPct}%`);
      } else {
        toast('Could not save to OptiPlex — check home Wi-Fi');
      }
      render();
    }
  });

  offVx = voice.changed.on(render);
  void voice.fetchAbSummary();
  await voice.startShootout((sample) => {
    if (showScorecard) return;
    const cur = items[idx];
    const match = sample.matched && sample.intentKey === cur.expectedIntentKey;
    const wordPct = voiceTokenMatchPct(sample.transcript, cur.phrase);
    if (sample.source === 'room') {
      cur.room = sample;
      cur.roomMatch = match;
      cur.roomWordPct = wordPct;
    } else {
      cur.phone = sample;
      cur.phoneMatch = match;
      cur.phoneWordPct = wordPct;
    }
    savedRunId = null;
    render();
  });
  render();
}
