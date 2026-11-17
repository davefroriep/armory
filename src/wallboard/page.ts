/**
 * Self-contained zero-dependency HTML5 Smart TV / gym monitor wallboard served directly from the Pixel APK.
 * Uses SSE (`/events`) with automatic 1s `/state` polling fallback and accepts `armory:wallboard-state` postMessages
 * for offline desktop preview in `#/wallboard`.
 */
export const WALLBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Armory Fit · Gym Wallboard</title>
<style>
  :root {
    --bg:#070b12; --card:#0f1724; --card2:#141f31; --line:#1e2b42;
    --txt:#f2f6fc; --mut:#8fa2bf; --blue:#38bdf8; --green:#22c55e;
    --amber:#f59e0b; --red:#ef4444; --cyan:#22d3ee;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; background:var(--bg); color:var(--txt); font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; overflow:hidden; }
  .wrap { height:100dvh; display:flex; flex-direction:column; gap:clamp(10px,1.4vh,18px); padding:clamp(12px,1.8vh,24px) clamp(14px,2vw,30px); }
  .top { display:flex; align-items:center; gap:14px; justify-content:space-between; flex-shrink:0; }
  .brand { display:flex; align-items:center; gap:12px; }
  .brand b { font-size:clamp(18px,2.2vw,28px); letter-spacing:.12em; color:#fff; }
  .pill { font-size:clamp(11px,1.2vw,15px); font-weight:700; letter-spacing:.08em; padding:6px 12px; border-radius:999px; background:var(--card2); border:1px solid var(--line); color:var(--mut); text-transform:uppercase; }
  .pill.live { color:#86efac; border-color:rgba(34,197,94,.45); background:rgba(34,197,94,.12); }
  .pill.cardio { color:#7dd3fc; border-color:rgba(56,189,248,.45); background:rgba(56,189,248,.12); }
  .top-r { display:flex; align-items:center; gap:12px; }
  .mic { display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px; border:1px solid var(--line); background:var(--card); font-size:clamp(12px,1.3vw,16px); color:var(--mut); font-weight:600; }
  .mic.on { color:#86efac; border-color:rgba(34,197,94,.5); background:rgba(34,197,94,.12); }
  .mic.armed { color:#67e8f9; border-color:var(--cyan); background:rgba(34,211,238,.2); box-shadow:0 0 20px rgba(34,211,238,.35); }
  .dot { width:9px; height:9px; border-radius:50%; background:currentColor; }
  .clk { font-variant-numeric:tabular-nums; font-size:clamp(18px,2.2vw,28px); font-weight:700; color:var(--mut); }
  .vtoast { padding:12px 18px; border-radius:14px; font-size:clamp(16px,2.2vw,28px); font-weight:800; letter-spacing:.02em; text-align:center; background:rgba(34,197,94,.18); border:2px solid var(--green); color:#dcfce7; flex-shrink:0; }
  .vtoast.info { background:rgba(56,189,248,.18); border-color:var(--blue); color:#e0f2fe; }
  .vtoast.warn { background:rgba(245,158,11,.18); border-color:var(--amber); color:#fef3c7; }
  .grid { flex:1; min-height:0; display:grid; grid-template-columns:1.35fr 0.95fr; gap:clamp(12px,1.8vw,24px); }
  .col { display:flex; flex-direction:column; gap:clamp(10px,1.4vh,18px); min-height:0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:clamp(14px,2vh,24px); }
  .hero { flex:1; display:flex; flex-direction:column; justify-content:space-between; min-height:0; position:relative; }
  .kicker { font-size:clamp(12px,1.3vw,16px); font-weight:800; letter-spacing:.12em; color:var(--blue); text-transform:uppercase; }
  .rest { border-radius:16px; padding:14px 18px; background:rgba(56,189,248,.14); border:2px solid var(--blue); display:flex; align-items:center; justify-content:space-between; gap:16px; position:relative; overflow:hidden; }
  .rest.over { background:rgba(34,197,94,.2); border-color:var(--green); }
  .rest.hold { background:rgba(245,158,11,.18); border-color:var(--amber); }
  .rest-bar { position:absolute; left:0; top:0; bottom:0; background:rgba(255,255,255,.08); transition:width .25s linear; pointer-events:none; }
  .rest b { font-size:clamp(28px,4vw,52px); font-variant-numeric:tabular-nums; position:relative; }
  .rest span { font-size:clamp(14px,1.8vw,22px); font-weight:800; letter-spacing:.08em; position:relative; }
  .cur-head { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:6px; }
  .cur-lab { font-size:clamp(20px,2.8vw,38px); font-weight:900; padding:4px 14px; border-radius:12px; background:rgba(56,189,248,.18); color:var(--blue); border:1px solid rgba(56,189,248,.4); }
  .cur-name { font-size:clamp(26px,3.8vw,50px); font-weight:800; line-height:1.1; }
  .cur-meta { font-size:clamp(14px,1.6vw,20px); color:var(--mut); margin-top:6px; }
  .note { display:inline-block; margin-top:8px; padding:6px 12px; border-radius:10px; background:rgba(245,158,11,.14); border:1px solid rgba(245,158,11,.35); color:#fde68a; font-size:clamp(13px,1.5vw,18px); }
  .target-num { font-size:clamp(42px,6.2vw,84px); font-weight:900; font-variant-numeric:tabular-nums; letter-spacing:-.02em; margin:12px 0; line-height:1.05; }
  .target-num.auto { color:#7dd3fc; }
  .set-pills { display:flex; flex-wrap:wrap; gap:10px; }
  .sp { padding:8px 14px; border-radius:12px; background:var(--card2); border:1px solid var(--line); font-size:clamp(14px,1.5vw,20px); font-weight:700; color:var(--mut); font-variant-numeric:tabular-nums; }
  .sp.done { background:rgba(34,197,94,.16); border-color:rgba(34,197,94,.5); color:#bbf7d0; }
  .sp.cur { background:rgba(56,189,248,.22); border-color:var(--blue); color:#fff; box-shadow:0 0 0 2px rgba(56,189,248,.35); }
  .upnext { display:flex; align-items:center; justify-content:space-between; gap:14px; background:var(--card2); }
  .upnext b { font-size:clamp(18px,2.2vw,28px); }
  .upnext span { font-size:clamp(16px,2vw,24px); color:var(--blue); font-weight:700; font-variant-numeric:tabular-nums; }
  .hr-card { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .hr-bpm { font-size:clamp(48px,6.8vw,92px); font-weight:900; line-height:1; font-variant-numeric:tabular-nums; }
  .hr-ready { padding:10px 16px; border-radius:14px; font-size:clamp(14px,1.7vw,22px); font-weight:800; letter-spacing:.04em; text-align:right; }
  .hr-ready.ready { background:rgba(34,197,94,.2); border:2px solid var(--green); color:#86efac; }
  .hr-ready.recovering { background:rgba(245,158,11,.18); border:2px solid var(--amber); color:#fde68a; }
  .hr-ready.resting { background:rgba(56,189,248,.16); border:2px solid var(--blue); color:#bae6fd; }
  .hr-ready.idle { background:var(--card2); border:1px solid var(--line); color:var(--mut); }
  .ex-list { flex:1; min-height:0; overflow:hidden; display:flex; flex-direction:column; gap:8px; }
  .ex-row { display:flex; align-items:center; gap:12px; padding:10px 14px; border-radius:12px; background:var(--card2); border:1px solid transparent; font-size:clamp(14px,1.6vw,21px); }
  .ex-row.cur { border-color:var(--blue); background:rgba(56,189,248,.14); }
  .ex-row.done { opacity:.55; }
  .ex-row em { font-style:normal; font-weight:800; color:var(--blue); min-width:38px; }
  .ex-row b { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ex-row span { font-variant-numeric:tabular-nums; font-weight:700; color:var(--mut); }
  .ex-row.done span { color:#86efac; }
  .standby-big { font-size:clamp(54px,8vw,112px); font-weight:900; font-variant-numeric:tabular-nums; letter-spacing:-.03em; }
  .sb-item { padding:14px 16px; border-radius:14px; background:var(--card2); border:1px solid var(--line); }
  .sb-item b { font-size:clamp(18px,2.2vw,28px); display:block; margin-bottom:4px; }
  .sb-item span { font-size:clamp(13px,1.5vw,19px); color:var(--mut); }
  .ptt { position:fixed; right:16px; bottom:16px; border:1px solid var(--line); background:var(--card2); color:var(--txt); padding:10px 16px; border-radius:999px; font-weight:700; cursor:pointer; opacity:.85; }
  .ptt.on { background:rgba(34,211,238,.25); border-color:var(--cyan); color:#fff; }
  @media (max-width:860px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap" id="app">
  <div class="top"><div class="brand"><b>ARMORY FIT</b><span class="pill">Connecting…</span></div><div class="clk" id="clk">--:--</div></div>
</div>
<button class="ptt" id="ptt" hidden type="button">🎤 Voice Relay</button>
<script>
const ZCOL = ['#64748b','#8a94a3','#38bdf8','#22c55e','#f59e0b','#ff4d2e'];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const mmss = (sec) => {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? h + ':' + String(m).padStart(2,'0') + ':' + String(r).padStart(2,'0') : m + ':' + String(r).padStart(2,'0');
};
let lastJson = '';

function render(st) {
  if (!st) return;
  const app = document.getElementById('app');
  const nowStr = new Date().toLocaleTimeString([], { hour:'numeric', minute:'2-digit' });
  const modePill = st.mode === 'lift'
    ? '<span class="pill live">' + (st.lift.paused ? 'PAUSED' : 'LIVE · LIFT') + ' · ' + mmss(st.lift.elapsedSec) + '</span>'
    : st.mode === 'cardio'
      ? '<span class="pill cardio">LIVE · CARDIO</span>'
      : '<span class="pill">STANDBY · ' + esc(st.standby?.weekLabel || '') + '</span>';

  const v = st.voice || {};
  const micCls = v.armed ? 'mic armed' : v.listening ? 'mic on' : 'mic';
  const micLbl = v.armed ? 'LISTENING…' : v.listening ? '“' + esc((v.wakeWord || 'armory').toUpperCase()) + ' …”' : 'MIC OFF';
  const banner = v.banner ? '<div class="vtoast ' + esc(v.banner.kind) + '">' + esc(v.banner.text) + '</div>' : '';

  const hr = st.hr || {};
  const hrColor = ZCOL[hr.zone || 0] || '#fff';
  const readyMap = {
    ready: ['ready', 'READY FOR SET', '≤ ' + (hr.readyLimitBpm || 130) + ' BPM' + (hr.drop60 ? ' · ↓' + hr.drop60 + ' / 60s' : '')],
    recovering: ['recovering', 'RECOVERING', 'TARGET ≤ ' + (hr.readyLimitBpm || 130) + ' BPM'],
    resting: ['resting', 'RESTING', 'Z' + (hr.zone || 1) + ' · ' + (hr.pctMax || 0) + '% MAX'],
    idle: ['idle', 'NO STRAP', 'Connect Polar H10']
  };
  const rm = readyMap[hr.readiness || 'idle'];
  const hrHtml = '<section class="card hr-card"><div><div class="kicker">POLAR H10 HEARTRATE</div><div class="hr-bpm" style="color:' + hrColor + '">' + (hr.fresh && hr.bpm ? hr.bpm : '--') + '</div><div class="cur-meta">' + (hr.fresh && hr.bpm ? 'Zone ' + hr.zone + ' · ' + hr.pctMax + '% HRmax' : 'Waiting for chest strap') + '</div></div><div class="hr-ready ' + rm[0] + '"><div>' + rm[1] + '</div><div style="font-size:.7em;opacity:.85;margin-top:4px">' + rm[2] + '</div></div></section>';

  let main = '';
  if (st.mode === 'lift' && st.lift) {
    const l = st.lift;
    let timerBanner = '';
    if (l.hold && l.hold.active) {
      const pct = Math.min(100, Math.max(0, 100 - (l.hold.remainingSec / Math.max(1, l.hold.totalSec)) * 100));
      timerBanner = '<div class="rest hold"><div class="rest-bar" style="width:' + pct + '%"></div><span>HOLD · ' + esc(l.hold.label) + '</span><b>' + l.hold.remainingSec + 's</b></div>';
    } else if (l.rest && l.rest.active) {
      const pct = Math.min(100, Math.max(0, 100 - (l.rest.remainingSec / Math.max(1, l.rest.totalSec)) * 100));
      const tStr = l.rest.over ? 'GO +' + mmss(-l.rest.remainingSec) : mmss(l.rest.remainingSec);
      timerBanner = '<div class="rest' + (l.rest.over ? ' over' : '') + '"><div class="rest-bar" style="width:' + pct + '%"></div><span>' + (l.rest.over ? 'REST COMPLETE' : 'REST TIMER') + (l.rest.label ? ' · ' + esc(l.rest.label) : '') + '</span><b>' + tStr + '</b></div>';
    }

    const c = l.current;
    const curHtml = c ? '<div><div class="kicker">UP NOW · ' + (c.isSuperset ? 'SUPERSET ' : '') + 'SET ' + c.setNumber + ' OF ' + c.totalSets + '</div>'
      + '<div class="cur-head"><span class="cur-lab">' + esc(c.label) + '</span><span class="cur-name">' + esc(c.exName) + '</span></div>'
      + '<div class="cur-meta">' + esc(c.muscles) + (c.prevText ? ' &nbsp;·&nbsp; Last: ' + esc(c.prevText) : '') + '</div>'
      + (c.note ? '<div class="note">' + esc(c.note) + '</div>' : '')
      + '<div class="target-num' + (c.isAuto ? ' auto' : '') + '">' + esc(c.exType === 'time' ? c.repsText : c.weightText + ' × ' + c.repsText) + '</div>'
      + '<div class="set-pills">' + c.sets.map((x) => '<span class="sp' + (x.done ? ' done' : x.isCurrent ? ' cur' : '') + '">' + (x.done ? '✓ ' : '') + esc(x.label) + ': ' + esc(x.text) + '</span>').join('') + '</div></div>'
      : '<div style="margin:auto 0"><div class="kicker">ALL SETS COMPLETE</div><div class="cur-name" style="margin-top:8px">Finish workout on your Pixel to save &amp; sync</div></div>';

    const nextHtml = l.next
      ? '<section class="card upnext"><div><div class="kicker" style="margin-bottom:2px">UP NEXT (AUTO SUPERSET)</div><b>' + esc(l.next.label) + ' · ' + esc(l.next.exName) + '</b></div><span>Set ' + l.next.setNumber + '/' + l.next.totalSets + ' · ' + esc(l.next.targetText) + '</span></section>'
      : '';

    const exRows = (l.exercises || []).map((e) => '<div class="ex-row' + (e.isCurrent ? ' cur' : '') + (e.isDone ? ' done' : '') + '"><em>' + esc(e.label) + '</em><b>' + esc(e.name) + '</b><span>' + (e.isDone ? '✓ ' : '') + e.doneSets + '/' + e.totalSets + '</span></div>').join('');

    main = '<div class="grid"><div class="col"><section class="card hero">' + timerBanner + curHtml + '</section>' + nextHtml + '</div>'
      + '<div class="col">' + hrHtml + '<section class="card" style="flex:1;min-height:0;display:flex;flex-direction:column;gap:10px"><div class="kicker">' + esc(l.title) + ' · ' + l.setsDone + '/' + l.setsTotal + ' SETS · ' + esc(l.volumeText) + '</div><div class="ex-list">' + exRows + '</div></section></div></div>';
  } else if (st.mode === 'cardio' && st.cardio) {
    const k = st.cardio;
    main = '<div class="grid"><div class="col"><section class="card hero">'
      + '<div class="kicker">' + esc(k.title) + ' · ' + esc(k.repText || k.phase) + '</div>'
      + '<div class="cur-name">' + esc(k.phase) + '</div>'
      + '<div class="target-num">' + esc(k.countdownText) + '</div>'
      + '<div class="set-pills"><span class="sp cur">POWER: ' + Math.round(k.watts || 0) + ' W' + (k.targetWatts ? ' / ' + k.targetWatts + ' W' : '') + '</span>'
      + '<span class="sp">CADENCE: ' + Math.round(k.rpm || 0) + ' RPM' + (k.targetRpm ? ' / ' + k.targetRpm + ' RPM' : '') + '</span>'
      + '<span class="sp">ELAPSED: ' + mmss(k.elapsedSec) + '</span></div>'
      + '</section></div><div class="col">' + hrHtml
      + '<section class="card"><div class="kicker">SESSION TOTALS</div><div class="cur-head"><span class="cur-name">' + Math.round(k.kcal || 0) + ' kcal</span><span class="cur-meta">Z5 Bank: ' + mmss(k.z5Sec || 0) + '</span></div></section></div></div>';
  } else if (st.standby) {
    const sb = st.standby;
    const items = (sb.todayItems || []).map((it) => '<div class="sb-item"><b>' + esc(it.title) + (it.carried ? ' · Carry-over' : '') + '</b><span>' + esc(it.equipmentLabel) + (it.preview ? ' · ' + esc(it.preview) : '') + '</span></div>').join('')
      || '<div class="sb-item"><b>Rest / Unscheduled Day</b><span>Start any workout on your Pixel to switch live</span></div>';
    main = '<div class="grid"><div class="col"><section class="card hero"><div class="kicker">THE ARMORY · ' + esc(sb.dateLabel) + '</div><div class="standby-big">' + esc(sb.timeLabel || nowStr) + '</div><div class="cur-meta">' + esc(sb.weekLabel) + ' · Start a workout on your Pixel or speak “' + esc(v.wakeWord || 'armory') + '” while Strength is open</div></section></div>'
      + '<div class="col">' + hrHtml + '<section class="card" style="flex:1;display:flex;flex-direction:column;gap:10px"><div class="kicker">TODAY ON THE SCHEDULE</div>' + items + '</section></div></div>';
  }

  app.innerHTML = '<div class="top"><div class="brand"><b>ARMORY FIT</b>' + modePill + '</div><div class="top-r"><div class="' + micCls + '"><i class="dot"></i><span>' + micLbl + '</span></div><div class="clk">' + esc(nowStr) + '</div></div></div>' + banner + main;
}

window.addEventListener('message', (ev) => {
  if (ev.data && ev.data.type === 'armory:wallboard-state') render(ev.data.state);
});

async function pollOnce() {
  if (location.protocol === 'about:' || location.protocol === 'blob:') return;
  try {
    const r = await fetch('/state?t=' + Date.now(), { cache:'no-store' });
    if (r.ok) {
      const txt = await r.text();
      if (txt !== lastJson) { lastJson = txt; render(JSON.parse(txt)); }
    }
  } catch {}
}

if (location.protocol !== 'about:' && location.protocol !== 'blob:') {
  pollOnce();
  let pollTimer = null;
  const connect = () => {
    try {
      const es = new EventSource('/events');
      es.onmessage = (e) => {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (e.data && e.data !== lastJson) {
          lastJson = e.data;
          try { render(JSON.parse(e.data)); } catch {}
        }
      };
      es.onerror = () => {
        if (!pollTimer) pollTimer = setInterval(pollOnce, 1000);
      };
    } catch {
      if (!pollTimer) pollTimer = setInterval(pollOnce, 1000);
    }
  };
  connect();
}

const keepAwake = () => { navigator.wakeLock?.request?.('screen').catch(() => {}); };
keepAwake();
document.addEventListener('visibilitychange', () => { if (!document.hidden) keepAwake(); });
document.addEventListener('click', keepAwake);

// Optional LAN browser speech relay (available in Chrome on OptiPlex/tablets; hidden on Silk/devices without Web Speech)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR && location.protocol.startsWith('http')) {
  const btn = document.getElementById('ptt');
  btn.hidden = false;
  let rec = null;
  btn.addEventListener('click', () => {
    if (rec) { rec.stop(); rec = null; btn.classList.remove('on'); return; }
    rec = new SR();
    rec.continuous = true;
    rec.interimResults = false;
    rec.lang = 'en-US';
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) {
          const transcript = e.results[i][0].transcript;
          fetch('/api/voice', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ transcript }) }).catch(() => {});
        }
      }
    };
    rec.onend = () => { btn.classList.remove('on'); rec = null; };
    rec.start();
    btn.classList.add('on');
  });
}
</script>
</body>
</html>`;
