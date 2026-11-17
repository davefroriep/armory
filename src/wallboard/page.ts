export const TV_W = 1600;
export const TV_H = 900;

export interface TvPreviewFit {
  scale: number;
  boxW: number;
  boxH: number;
  rotated: boolean;
}

/** Computes scaled 16:9 stage dimensions (rotating 90deg in portrait so a landscape TV fills tall phones). */
export function computeTvPreviewFit(vw: number, vh: number, rotateInPortrait = false): TvPreviewFit {
  const rotated = rotateInPortrait && vh > vw;
  const scale = rotated
    ? Math.max(0.05, Math.min((vh - 56) / TV_W, (vw - 20) / TV_H))
    : rotateInPortrait
      ? Math.max(0.05, Math.min((vw - 32) / TV_W, (vh - 48) / TV_H))
      : Math.max(0.05, vw / TV_W);
  const boxW = Math.round((rotated ? TV_H : TV_W) * scale);
  const boxH = Math.round((rotated ? TV_W : TV_H) * scale);
  return { scale, boxW, boxH, rotated };
}

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
  .grid { flex:1; min-height:0; display:grid; grid-template-columns:1.42fr 0.88fr; gap:clamp(12px,1.6vw,22px); }
  .col { display:flex; flex-direction:column; gap:clamp(10px,1.4vh,16px); min-height:0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:clamp(14px,1.8vh,22px); overflow:hidden; }
  .hero { flex:1; display:flex; flex-direction:column; justify-content:space-between; min-height:0; position:relative; }
  .cur-body { min-height:0; overflow:hidden; }
  .kicker { font-size:clamp(11px,1.15vw,15px); font-weight:800; letter-spacing:.12em; color:var(--blue); text-transform:uppercase; }
  .rest { border-radius:14px; padding:12px 16px; background:rgba(56,189,248,.14); border:2px solid var(--blue); display:flex; align-items:center; justify-content:space-between; gap:14px; position:relative; overflow:hidden; flex-shrink:0; }
  .rest.over { background:rgba(34,197,94,.2); border-color:var(--green); }
  .rest.wait { background:rgba(245,158,11,.18); border-color:var(--amber); }
  .rest.hold { background:rgba(245,158,11,.18); border-color:var(--amber); }
  .rest-bar { position:absolute; left:0; top:0; bottom:0; background:rgba(255,255,255,.08); transition:width .25s linear; pointer-events:none; }
  .rest b { font-size:clamp(24px,3.4vw,44px); font-variant-numeric:tabular-nums; position:relative; }
  .rest span { font-size:clamp(13px,1.55vw,20px); font-weight:800; letter-spacing:.06em; position:relative; }
  .cur-head { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:6px; }
  .cur-lab { font-size:clamp(20px,2.8vw,38px); font-weight:900; padding:4px 14px; border-radius:12px; background:rgba(56,189,248,.18); color:var(--blue); border:1px solid rgba(56,189,248,.4); }
  .cur-name { font-size:clamp(26px,3.8vw,50px); font-weight:800; line-height:1.1; }
  .cur-meta { font-size:clamp(14px,1.5vw,19px); color:var(--mut); margin-top:4px; }
  .note { display:inline-block; margin-top:6px; padding:4px 10px; border-radius:8px; background:rgba(245,158,11,.14); border:1px solid rgba(245,158,11,.35); color:#fde68a; font-size:clamp(12px,1.35vw,16px); }
  .target-num { font-size:clamp(42px,6.2vw,84px); font-weight:900; font-variant-numeric:tabular-nums; letter-spacing:-.02em; margin:12px 0; line-height:1.05; }
  .set-pills { display:flex; flex-wrap:wrap; gap:10px; }
  .sp { padding:8px 14px; border-radius:12px; background:var(--card2); border:1px solid var(--line); font-size:clamp(14px,1.5vw,20px); font-weight:700; color:var(--mut); font-variant-numeric:tabular-nums; }
  .sp.done { background:rgba(34,197,94,.16); border-color:rgba(34,197,94,.5); color:#bbf7d0; }
  .sp.cur { background:rgba(56,189,248,.22); border-color:var(--blue); color:#fff; box-shadow:0 0 0 2px rgba(56,189,248,.35); }

  /* Live Set Cockpit */
  .ss-stage { flex:1; min-height:0; display:flex; flex-direction:column; gap:clamp(8px,1.1vh,12px); border-left:4px solid var(--cyan); }
  .ss-head { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-shrink:0; }
  .ss-pair { flex:1; min-height:0; display:flex; flex-direction:column; gap:clamp(8px,1vh,12px); overflow:hidden; }
  .st-card { background:var(--card2); border:1px solid var(--line); border-radius:14px; padding:clamp(10px,1.3vh,16px) clamp(12px,1.4vw,18px); display:flex; flex-direction:column; gap:clamp(6px,0.8vh,10px); min-height:0; overflow:hidden; }
  .st-card.single { flex:1; background:var(--card); border-radius:18px; border-left:4px solid var(--blue); padding:clamp(14px,1.8vh,22px); justify-content:space-between; }
  .st-card.active { border-color:rgba(56,189,248,.65); box-shadow:inset 0 0 0 1px rgba(56,189,248,.25); flex:1.28 1 0; }
  .st-card.partner { flex:1 1 0; opacity:.92; }
  .st-card.single.active { flex:1 1 0; box-shadow:none; border-color:var(--line); border-left:4px solid var(--blue); }
  .st-top { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-shrink:0; }
  .st-title { display:flex; align-items:center; gap:10px; min-width:0; flex:1; }
  .st-lab { font-size:clamp(14px,1.6vw,22px); font-weight:900; padding:3px 11px; border-radius:9px; background:rgba(56,189,248,.18); color:var(--blue); border:1px solid rgba(56,189,248,.4); flex-shrink:0; }
  .st-card.single .st-lab { font-size:clamp(18px,2.2vw,28px); padding:4px 14px; border-radius:11px; }
  .st-card.partner .st-lab { background:var(--card); color:var(--mut); border-color:var(--line); }
  .st-name { font-size:clamp(18px,2.1vw,30px); font-weight:800; line-height:1.12; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .st-card.single .st-name { font-size:clamp(24px,3vw,42px); }
  .st-tag { font-size:clamp(10px,1vw,12px); font-weight:800; letter-spacing:.08em; padding:3px 8px; border-radius:999px; background:var(--card); border:1px solid var(--line); color:var(--mut); text-transform:uppercase; flex-shrink:0; }
  .rx-pills { display:flex; align-items:center; gap:6px; flex-shrink:0; }
  .rx-pill { font-size:clamp(11px,1.2vw,16px); font-weight:800; padding:4px 10px; border-radius:8px; white-space:nowrap; }
  .rx-pill.last { background:var(--card); border:1px solid var(--line); color:var(--mut); }
  .rx-pill.reps { background:rgba(34,211,238,.14); border:1px solid rgba(34,211,238,.4); color:#67e8f9; }
  .rx-pill.rpe { background:rgba(245,158,11,.14); border:1px solid rgba(245,158,11,.4); color:#fde68a; }

  /* Fill-in-the-blank Set Slots */
  .set-rows { display:flex; flex-direction:column; gap:6px; min-height:0; overflow:hidden; flex:1; justify-content:center; }
  .s-row { display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:12px; padding:6px 12px; border-radius:11px; background:rgba(7,11,18,.52); border:1px solid transparent; }
  .st-card.single .s-row { padding:9px 16px; gap:16px; }
  .s-row.active { background:rgba(56,189,248,.12); border:1.5px solid var(--blue); }
  .s-row.done { background:rgba(15,23,36,.75); border-color:rgba(34,197,94,.22); }
  .s-row.queued { opacity:.68; }
  .s-tag { font-size:clamp(11px,1.15vw,15px); font-weight:800; letter-spacing:.06em; color:var(--mut); min-width:58px; font-variant-numeric:tabular-nums; }
  .s-row.active .s-tag { color:var(--blue); }
  .s-slots { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-variant-numeric:tabular-nums; }
  .s-x { font-size:clamp(14px,1.6vw,22px); font-weight:800; color:var(--mut); }
  .s-logged { font-size:clamp(16px,1.95vw,26px); font-weight:800; color:#fff; }
  .st-card.single .s-logged { font-size:clamp(20px,2.5vw,34px); }
  .s-queued { font-size:clamp(14px,1.65vw,22px); font-weight:700; color:rgba(148,163,184,.36); }
  .st-card.single .s-queued { font-size:clamp(17px,2vw,26px); }
  .s-rpe { font-size:clamp(12px,1.25vw,16px); font-weight:800; color:#fde68a; background:rgba(245,158,11,.14); border:1px solid rgba(245,158,11,.35); padding:2px 8px; border-radius:999px; }
  .slot-box { padding:4px 12px; border-radius:9px; font-size:clamp(15px,1.8vw,24px); font-weight:800; letter-spacing:.01em; line-height:1.15; }
  .st-card.single .slot-box { padding:7px 16px; font-size:clamp(19px,2.4vw,32px); }
  .slot-box.empty { border:2px dashed rgba(56,189,248,.8); background:rgba(7,11,18,.88); color:#7dd3fc; }
  .slot-box.ghost { border:1.5px dashed rgba(148,163,184,.26); background:rgba(7,11,18,.55); color:rgba(148,163,184,.44); font-weight:700; }
  .slot-box.filled { border:2px solid rgba(56,189,248,.85); background:rgba(56,189,248,.16); color:#fff; }
  .slot-box.bw { border:1px solid var(--line); background:var(--card); color:var(--mut); }
  .s-stat { font-size:clamp(11px,1.1vw,14px); font-weight:800; letter-spacing:.07em; color:var(--mut); text-transform:uppercase; white-space:nowrap; }
  .s-stat.ok { font-size:clamp(16px,1.8vw,24px); color:#4ade80; }
  .s-row.active .s-stat { color:#7dd3fc; }

  /* Contextual Voice Strip */
  .vbar { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:7px 12px; border-radius:11px; background:rgba(7,11,18,.82); border:1px solid rgba(56,189,248,.32); flex-shrink:0; }
  .vbar.armed { border-color:var(--cyan); background:rgba(34,211,238,.15); box-shadow:0 0 18px rgba(34,211,238,.2); }
  .vbar.off { border-color:var(--line); opacity:.82; }
  .vbar-l { display:flex; align-items:center; gap:7px; font-size:clamp(11px,1.1vw,14px); font-weight:800; letter-spacing:.08em; color:#86efac; text-transform:uppercase; flex-shrink:0; }
  .vbar.armed .vbar-l { color:#67e8f9; }
  .vbar.standby .vbar-l, .vbar.off .vbar-l { color:var(--mut); }
  .vchips { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
  .vchip { display:inline-flex; align-items:center; padding:4px 10px; border-radius:9px; background:var(--card); border:1px solid var(--line); font-size:clamp(11px,1.2vw,15px); }
  .vchip.primary { background:rgba(56,189,248,.16); border-color:rgba(56,189,248,.55); }
  .vchip b { color:#fff; font-weight:800; }

  /* On Deck Card (Straight Sets) */
  .ondeck { background:var(--card2); padding:clamp(10px,1.4vh,16px) clamp(14px,1.8vw,22px); flex-shrink:0; }
  .od-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px; }
  .od-tag { font-size:clamp(11px,1.1vw,14px); font-weight:800; color:var(--mut); letter-spacing:.06em; }
  .od-items { display:flex; flex-direction:column; gap:5px; }
  .od-row { display:flex; align-items:baseline; gap:10px; font-size:clamp(13px,1.45vw,19px); }
  .od-row em { font-style:normal; font-weight:800; color:var(--blue); min-width:30px; }
  .od-row b { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .od-row span { color:var(--mut); font-size:.88em; font-weight:600; white-space:nowrap; }

  .hr-card { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .hr-slim { display:flex; align-items:center; justify-content:space-between; padding:10px 16px; flex-shrink:0; }
  .hr-slim-st { font-size:clamp(12px,1.3vw,16px); color:var(--mut); font-weight:700; }
  .hr-bpm { font-size:clamp(46px,6.4vw,88px); font-weight:900; line-height:1; font-variant-numeric:tabular-nums; }
  .hr-ready { padding:10px 16px; border-radius:14px; font-size:clamp(14px,1.65vw,21px); font-weight:800; letter-spacing:.04em; text-align:right; }
  .hr-ready.ready { background:rgba(34,197,94,.2); border:2px solid var(--green); color:#86efac; }
  .hr-ready.recovering { background:rgba(245,158,11,.18); border:2px solid var(--amber); color:#fde68a; }
  .hr-ready.resting { background:rgba(56,189,248,.16); border:2px solid var(--blue); color:#bae6fd; }
  .hr-ready.idle { background:var(--card2); border:1px solid var(--line); color:var(--mut); }
  .ex-list { flex:1; min-height:0; overflow:hidden; display:flex; flex-direction:column; gap:7px; }
  .ex-row { display:flex; align-items:center; gap:12px; padding:9px 13px; border-radius:11px; background:var(--card2); border:1px solid transparent; font-size:clamp(13px,1.5vw,20px); }
  .ex-row.cur { border-color:var(--blue); background:rgba(56,189,248,.14); }
  .ex-row.done { opacity:.55; }
  .ex-row em { font-style:normal; font-weight:800; color:var(--blue); min-width:34px; }
  .ex-row b { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ex-row span { font-variant-numeric:tabular-nums; font-weight:700; color:var(--mut); }
  .ex-row.done span { color:#86efac; }
  .ex-ss { --ss:var(--amber); border-radius:12px; padding-left:4px; background:var(--card2); box-shadow:inset 4px 0 0 var(--ss), inset 0 0 0 1px rgba(245,158,11,.28); overflow:hidden; display:flex; flex-direction:column; flex-shrink:0; }
  .ex-ss.c1 { --ss:var(--cyan); box-shadow:inset 4px 0 0 var(--ss), inset 0 0 0 1px rgba(34,211,238,.28); }
  .ex-ss .ex-row { border-radius:0; background:transparent; border:0; }
  .ex-ss .ex-row + .ex-row { border-top:1px dashed rgba(148,163,184,.22); }
  .ex-ss .ex-row.cur { background:rgba(56,189,248,.16); }
  .ex-ss .ex-row em { color:var(--ss); }
  .standby-big { font-size:clamp(54px,8vw,112px); font-weight:900; font-variant-numeric:tabular-nums; letter-spacing:-.03em; }
  .sb-item { padding:14px 16px; border-radius:14px; background:var(--card2); border:1px solid var(--line); }
  .sb-item b { font-size:clamp(18px,2.2vw,28px); display:block; margin-bottom:4px; }
  .sb-item span { font-size:clamp(13px,1.5vw,19px); color:var(--mut); }
  .ptt { position:fixed; right:16px; bottom:16px; border:1px solid var(--line); background:var(--card2); color:var(--txt); padding:10px 16px; border-radius:999px; font-weight:700; cursor:pointer; opacity:.85; }
  .ptt.on { background:rgba(34,211,238,.25); border-color:var(--cyan); color:#fff; }
  @media (max-width:860px) { .grid { grid-template-columns:1fr; } }
  @media (max-height:700px) {
    .wrap { gap:6px; padding:8px 14px; }
    .card { padding:9px 12px; border-radius:13px; }
    .rest { padding:7px 12px; }
    .rest b { font-size:clamp(20px,4.2vh,32px); }
    .rest span { font-size:clamp(11px,2.1vh,15px); }
    .cur-lab { font-size:clamp(15px,3vh,22px); padding:2px 10px; }
    .cur-name { font-size:clamp(19px,4vh,30px); }
    .target-num { font-size:clamp(28px,6.5vh,48px); margin:4px 0; }
    .ss-stage { gap:6px; }
    .ss-pair { gap:6px; }
    .st-card { padding:7px 10px; gap:4px; border-radius:11px; }
    .st-card.single { padding:10px 14px; }
    .st-lab { font-size:clamp(12px,2.2vh,16px); padding:2px 8px; }
    .st-name { font-size:clamp(15px,2.9vh,22px); }
    .st-card.single .st-name { font-size:clamp(19px,3.8vh,28px); }
    .rx-pill { font-size:clamp(10px,1.9vh,13px); padding:2px 7px; }
    .set-rows { gap:3px; }
    .s-row { padding:4px 9px; gap:8px; }
    .st-card.single .s-row { padding:6px 12px; }
    .slot-box { padding:2px 9px; font-size:clamp(13px,2.4vh,18px); border-width:1.5px; }
    .st-card.single .slot-box { padding:4px 12px; font-size:clamp(15px,3vh,22px); }
    .s-logged { font-size:clamp(13px,2.5vh,19px); }
    .s-queued { font-size:clamp(12px,2.1vh,16px); }
    .vbar { padding:5px 9px; gap:6px; }
    .vchip { padding:2px 8px; font-size:clamp(10px,1.9vh,13px); }
    .hr-slim { padding:7px 12px; }
    .hr-bpm { font-size:clamp(32px,7vh,52px); }
    .ex-list { gap:4px; }
    .ex-row { padding:5px 9px; font-size:clamp(11px,2.1vh,14px); }
  }
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
  const micBadge = st.mode === 'lift' ? '<div class="' + micCls + '"><i class="dot"></i><span>' + micLbl + '</span></div>' : '';
  const banner = st.mode === 'lift' && v.banner ? '<div class="vtoast ' + esc(v.banner.kind) + '">' + esc(v.banner.text) + '</div>' : '';

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

  const renderSetRow = (slot, exType) => {
    const tagTxt = slot.kind === 'w' ? 'W' : 'SET ' + slot.label;
    if (slot.state === 'done') {
      const valTxt = exType === 'time'
        ? (slot.weightVal && slot.weightVal !== 'HOLD' ? slot.repsVal + ' @ ' + slot.weightVal : slot.repsVal)
        : slot.weightVal + ' × ' + slot.repsVal;
      return '<div class="s-row done"><span class="s-tag">' + esc(tagTxt) + '</span>'
        + '<div class="s-slots"><span class="s-logged">' + esc(valTxt) + '</span>'
        + (slot.rpeText ? '<span class="s-rpe">' + esc(slot.rpeText) + '</span>' : '')
        + '</div><span class="s-stat ok">✓</span></div>';
    }
    if (slot.state === 'queued') {
      const qTxt = exType === 'time'
        ? (slot.repsVal || slot.repsPlaceholder)
        : exType === 'bodyweight'
          ? (slot.repsVal ? slot.weightVal + ' × ' + slot.repsVal : slot.weightVal)
          : ((slot.weightVal && slot.repsVal) ? slot.weightVal + ' × ' + slot.repsVal : '—');
      return '<div class="s-row queued"><span class="s-tag">' + esc(tagTxt) + '</span>'
        + '<div class="s-slots"><span class="s-queued">' + esc(qTxt) + '</span>'
        + (slot.rpeText ? '<span class="s-rpe">' + esc(slot.rpeText) + '</span>' : '')
        + '</div><span class="s-stat">○</span></div>';
    }
    const showWeightBox = exType !== 'time' || !!slot.weightVal;
    let wCls = 'slot-box ';
    if (exType === 'bodyweight' && !slot.isGhost && slot.weightVal === 'BW') wCls += 'bw';
    else if (!slot.weightVal) wCls += 'empty';
    else wCls += slot.isGhost ? 'ghost' : 'filled';
    const wTxt = slot.weightVal || slot.weightPlaceholder;
    const rCls = 'slot-box ' + (!slot.repsVal ? 'empty' : slot.isGhost ? 'ghost' : 'filled');
    const rTxt = slot.repsVal || slot.repsPlaceholder;
    const slotsHtml = (showWeightBox ? '<span class="' + wCls + '">' + esc(wTxt) + '</span><span class="s-x">×</span>' : '')
      + '<span class="' + rCls + '">' + esc(rTxt) + '</span>'
      + (slot.rpeText ? '<span class="s-rpe">' + esc(slot.rpeText) + '</span>' : '');
    const statTxt = slot.isGhost ? 'SUGGESTED' : exType === 'time' ? 'HOLD' : 'SPEAK SET';
    return '<div class="s-row active"><span class="s-tag">▶ ' + esc(tagTxt) + '</span>'
      + '<div class="s-slots">' + slotsHtml + '</div>'
      + '<span class="s-stat">' + esc(statTxt) + '</span></div>';
  };

  const renderVoiceBar = (vh) => '<div class="vbar ' + esc(vh.micState) + '">'
    + '<div class="vbar-l"><i class="dot"></i><span>' + esc(vh.headline || 'SAY') + '</span></div>'
    + '<div class="vchips">' + vh.chips.map((ch) => '<span class="vchip' + (ch.primary ? ' primary' : '') + '"><b>' + esc(ch.say) + '</b></span>').join('') + '</div></div>';

  const renderStation = (stCard, isSingle) => {
    const cls = 'st-card' + (isSingle ? ' card single' : '') + (stCard.isActive ? ' active' : ' partner');
    return '<section class="' + cls + '">'
      + '<div class="st-top"><div class="st-title"><span class="st-lab">' + esc(stCard.label) + '</span><span class="st-name">' + esc(stCard.exName) + '</span>'
      + (!isSingle && !stCard.isActive ? '<span class="st-tag">PARTNER</span>' : '') + '</div>'
      + '<div class="rx-pills">'
      + (stCard.lastBenchmark ? '<span class="rx-pill last">Last ' + esc(stCard.lastBenchmark) + '</span>' : '')
      + '<span class="rx-pill reps">' + esc(stCard.repTarget) + '</span><span class="rx-pill rpe">' + esc(stCard.rpeTarget) + '</span></div></div>'
      + '<div class="set-rows cur-body">' + stCard.sets.map((s) => renderSetRow(s, stCard.exType)).join('') + '</div>'
      + (stCard.isActive && stCard.voiceHint ? renderVoiceBar(stCard.voiceHint) : '')
      + '</section>';
  };

  const renderOnDeck = (od) => '<section class="card ondeck"><div class="od-head"><div class="kicker">ON DECK NEXT</div>'
    + '<span class="od-tag">' + (od.isSuperset ? 'SUPERSET PAIR · ' : '') + esc(od.label) + '</span></div>'
    + '<div class="od-items">' + od.items.map((it) => '<div class="od-row"><em>' + esc(it.label) + '</em><b>' + esc(it.exName) + '</b><span>'
      + it.sets + '× ' + esc(it.repTarget) + ' · ' + esc(it.rpeTarget) + (it.lastText ? ' · Last ' + esc(it.lastText) : '') + '</span></div>').join('')
    + '</div></section>';

  const renderExRow = (e) => '<div class="ex-row' + (e.isCurrent ? ' cur' : '') + (e.isDone ? ' done' : '') + '"><em>' + esc(e.label) + '</em><b>' + esc(e.name) + '</b><span>' + (e.isDone ? '✓ ' : '') + e.doneSets + '/' + e.totalSets + '</span></div>';

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
      const restingHrHigh = l.rest.over && hr.fresh && hr.bpm > (hr.readyLimitBpm || 130);
      const restCls = l.rest.over ? (restingHrHigh ? 'rest wait' : 'rest over') : 'rest';
      const restTitle = l.rest.over
        ? (restingHrHigh ? 'TIMER UP · CATCH BREATH (HR ' + hr.bpm + ' → ≤' + (hr.readyLimitBpm || 130) + ')' : 'GO WHEN READY · REST COMPLETE')
        : 'REST TIMER';
      timerBanner = '<div class="' + restCls + '"><div class="rest-bar" style="width:' + pct + '%"></div><span>' + restTitle + (l.rest.label ? ' · ' + esc(l.rest.label) : '') + '</span><b>' + tStr + '</b></div>';
    }

    let leftStage = '';
    const sg = l.stage;
    if (!sg) {
      leftStage = '<section class="card hero"><div class="cur-body" style="margin:auto 0"><div class="kicker">ALL ' + l.setsTotal + ' SETS COMPLETE</div><div class="cur-name" style="margin-top:8px">Finish workout on your Pixel to save &amp; sync</div><div class="cur-meta" style="margin-top:10px">Or say “' + esc((v.wakeWord || 'armory')) + ' finish workout”</div></div></section>';
    } else if (sg.kind === 'superset') {
      leftStage = '<section class="card ss-stage"><div class="ss-head"><div class="kicker">' + esc(sg.kicker) + '</div></div>'
        + '<div class="ss-pair">' + sg.stations.map((sc) => renderStation(sc, false)).join('') + '</div></section>';
    } else {
      leftStage = renderStation(sg.stations[0], true) + (sg.onDeck ? renderOnDeck(sg.onDeck) : '');
    }

    let exRows = '';
    const exArr = l.exercises || [];
    for (let i = 0; i < exArr.length; ) {
      const g = exArr[i].ssGroup;
      if (g == null) {
        exRows += renderExRow(exArr[i]);
        i++;
      } else {
        let j = i;
        while (j + 1 < exArr.length && exArr[j + 1].ssGroup === g) j++;
        let inner = '';
        for (let k = i; k <= j; k++) inner += renderExRow(exArr[k]);
        exRows += '<div class="ex-ss' + (g % 2 ? ' c1' : '') + '">' + inner + '</div>';
        i = j + 1;
      }
    }

    const liftHrHtml = (hr.fresh && hr.bpm)
      ? hrHtml
      : '<section class="card hr-slim"><div class="kicker">POLAR H10</div><span class="hr-slim-st">Not connected</span></section>';

    main = '<div class="grid"><div class="col">' + timerBanner + leftStage + '</div>'
      + '<div class="col">' + liftHrHtml + '<section class="card" style="flex:1;min-height:0;display:flex;flex-direction:column;gap:10px"><div class="kicker">' + esc(l.title) + ' · ' + l.setsDone + '/' + l.setsTotal + ' SETS' + (l.setsDone > 0 ? ' · ' + esc(l.volumeText) : '') + '</div><div class="ex-list">' + exRows + '</div></section></div></div>';
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

  app.innerHTML = '<div class="top"><div class="brand"><b>ARMORY FIT</b>' + modePill + '</div><div class="top-r">' + micBadge + '<div class="clk">' + esc(nowStr) + '</div></div></div>' + banner + main;
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
