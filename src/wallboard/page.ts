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
  .top { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:14px; flex-shrink:0; }
  .brand { display:flex; align-items:center; gap:12px; flex-shrink:0; }
  .brand b { font-size:clamp(18px,2.2vw,28px); letter-spacing:.12em; color:#fff; }
  .pill { font-size:clamp(11px,1.2vw,15px); font-weight:700; letter-spacing:.08em; padding:6px 12px; border-radius:999px; background:var(--card2); border:1px solid var(--line); color:var(--mut); text-transform:uppercase; }
  .pill.live { color:#86efac; border-color:rgba(34,197,94,.45); background:rgba(34,197,94,.12); }
  .pill.cardio { color:#7dd3fc; border-color:rgba(56,189,248,.45); background:rgba(56,189,248,.12); }
  .pill.since { color:#7dd3fc; border-color:rgba(56,189,248,.45); background:rgba(56,189,248,.12); font-variant-numeric:tabular-nums; }
  .top-mid { display:flex; justify-content:center; align-items:center; min-width:0; }
  .top-r { display:flex; align-items:center; gap:12px; flex-shrink:0; }
  .mic { display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px; border:1px solid var(--line); background:var(--card); font-size:clamp(12px,1.3vw,16px); color:var(--mut); font-weight:600; }
  .mic.on { color:#86efac; border-color:rgba(34,197,94,.5); background:rgba(34,197,94,.12); }
  .mic.armed { color:#67e8f9; border-color:var(--cyan); background:rgba(34,211,238,.2); box-shadow:0 0 20px rgba(34,211,238,.35); }
  .dot { width:9px; height:9px; border-radius:50%; background:currentColor; }
  .clk { font-variant-numeric:tabular-nums; font-size:clamp(18px,2.2vw,28px); font-weight:700; color:var(--mut); }
  .vtoast { display:inline-flex; align-items:center; max-width:100%; padding:5px 14px; border-radius:999px; font-size:clamp(12px,1.4vw,17px); font-weight:800; letter-spacing:.02em; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; background:rgba(34,197,94,.18); border:1.5px solid var(--green); color:#dcfce7; }
  .vtoast.info { background:rgba(56,189,248,.18); border-color:var(--blue); color:#e0f2fe; }
  .vtoast.warn { background:rgba(245,158,11,.18); border-color:var(--amber); color:#fef3c7; }
  .grid { flex:1; min-height:0; display:grid; grid-template-columns:minmax(0,1.28fr) minmax(0,1fr); gap:clamp(12px,1.6vw,22px); }
  .col { display:flex; flex-direction:column; gap:clamp(10px,1.4vh,16px); min-height:0; min-width:0; }
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
  .wb-chk { width:clamp(20px,2.1vw,26px); height:clamp(20px,2.1vw,26px); border-radius:7px; border:2px solid rgba(148,163,184,.38); background:rgba(7,11,18,.6); display:inline-flex; align-items:center; justify-content:center; font-size:clamp(13px,1.4vw,18px); font-weight:900; color:transparent; flex-shrink:0; }
  .wb-chk.active { border-color:var(--blue); background:rgba(56,189,248,.12); }
  .wb-chk.on { border-color:var(--green); background:rgba(34,197,94,.22); color:#4ade80; }
  .wb-chk.sm { width:clamp(17px,1.7vw,22px); height:clamp(17px,1.7vw,22px); border-radius:6px; font-size:clamp(11px,1.2vw,15px); }

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
  .ex-list.dense { gap:4px; }
  .ex-row { display:flex; align-items:center; gap:10px; padding:8px 12px; border-radius:11px; background:var(--card2); border:1px solid transparent; flex-shrink:0; }
  .ex-list.dense .ex-row { padding:5px 10px; gap:8px; }
  .ex-row.cur { border-color:var(--blue); background:rgba(56,189,248,.14); }
  .ex-row.done { opacity:.6; }
  .ex-row em { font-style:normal; font-weight:800; color:var(--blue); min-width:32px; font-size:clamp(13px,1.4vw,18px); flex-shrink:0; }
  .ex-main { flex:1; min-width:0; display:flex; flex-direction:column; gap:2px; }
  .ex-main b { font-size:clamp(14px,1.45vw,19px); font-weight:800; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ex-list.dense .ex-main b { font-size:clamp(13px,1.3vw,17px); }
  .ex-main small { font-size:clamp(11px,1.12vw,14px); font-weight:600; color:var(--mut); line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-variant-numeric:tabular-nums; }
  .ex-list.dense .ex-main small { font-size:clamp(10px,1.02vw,13px); }
  .ex-main small.rx-hi { color:#bae6fd; font-weight:700; }
  .ex-row.done .ex-main small { color:#86efac; opacity:.85; }
  .ex-r { display:flex; align-items:center; gap:8px; flex-shrink:0; }
  .ex-r span { font-variant-numeric:tabular-nums; font-weight:700; font-size:clamp(12px,1.25vw,16px); color:var(--mut); }
  .ex-row.done .ex-r span { color:#86efac; }
  .ex-ss { --ss:var(--amber); border-radius:12px; padding-left:4px; background:var(--card2); box-shadow:inset 4px 0 0 var(--ss), inset 0 0 0 1px rgba(245,158,11,.28); overflow:hidden; display:flex; flex-direction:column; flex-shrink:0; }
  .ex-ss.c1 { --ss:var(--cyan); box-shadow:inset 4px 0 0 var(--ss), inset 0 0 0 1px rgba(34,211,238,.28); }
  .ex-ss .ex-row { border-radius:0; background:transparent; border:0; }
  .ex-ss .ex-row + .ex-row { border-top:1px dashed rgba(148,163,184,.22); }
  .ex-ss .ex-row.cur { background:rgba(56,189,248,.16); }
  .ex-ss .ex-row em { color:var(--ss); }
  .standby-big { font-size:clamp(54px,8vw,112px); font-weight:900; font-variant-numeric:tabular-nums; letter-spacing:-.03em; }
  .kicker.done { color:#86efac; }
  .sb-item { padding:14px 16px; border-radius:14px; background:var(--card2); border:1px solid var(--line); }
  .sb-item.done { background:rgba(34,197,94,.11); border-color:rgba(34,197,94,.45); }
  .sb-row { display:flex; align-items:center; justify-content:space-between; gap:14px; }
  .sb-row > div { min-width:0; }
  .sb-done-pill { display:inline-flex; align-items:center; gap:6px; padding:6px 14px; border-radius:999px; background:rgba(34,197,94,.22); border:1.5px solid var(--green); color:#bbf7d0; font-size:clamp(13px,1.45vw,19px); font-weight:900; letter-spacing:.06em; white-space:nowrap; flex-shrink:0; }
  .sb-next { display:flex; align-items:center; gap:12px; padding:11px 16px; border-radius:12px; background:rgba(7,11,18,.55); border:1px solid var(--line); margin-top:auto; }
  .sb-next em { font-style:normal; font-size:clamp(11px,1.1vw,14px); font-weight:800; letter-spacing:.1em; color:var(--blue); text-transform:uppercase; flex-shrink:0; }
  .sb-next b { font-size:clamp(15px,1.65vw,22px); font-weight:800; color:var(--txt); flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sb-next span { font-size:clamp(13px,1.35vw,17px); font-weight:700; color:var(--mut); white-space:nowrap; flex-shrink:0; }
  .sb-item b { font-size:clamp(18px,2.2vw,28px); display:block; margin-bottom:4px; }
  .sb-item span { font-size:clamp(13px,1.5vw,19px); color:var(--mut); }
  .rec-halo { position:fixed; inset:0; z-index:40; pointer-events:none; opacity:0; transition:opacity .22s ease; box-shadow:inset 0 0 0 5px rgba(239,68,68,.94), inset 0 0 38px 6px rgba(239,68,68,.58), inset 0 0 115px 12px rgba(185,28,28,.34); }
  body.cam-rec .rec-halo { opacity:1; animation:recHaloPulse 2.2s ease-in-out infinite; }
  @keyframes recHaloPulse { 0%,100% { opacity:1; } 50% { opacity:.74; } }
  .rec-pill { display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px; border:1.5px solid rgba(239,68,68,.88); background:rgba(127,29,29,.45); color:#fecaca; font-size:clamp(12px,1.3vw,16px); font-weight:800; letter-spacing:.08em; text-transform:uppercase; box-shadow:0 0 18px rgba(239,68,68,.38); }
  .rec-pill .dot { background:#ef4444; box-shadow:0 0 10px #ef4444; animation:recDotBlink 1.4s ease-in-out infinite; }
  .rec-pill.saving { border-color:rgba(245,158,11,.78); background:rgba(120,53,15,.38); color:#fde68a; box-shadow:none; }
  .rec-pill.saving .dot { background:var(--amber); box-shadow:none; animation:none; }
  @keyframes recDotBlink { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .ptt { position:fixed; right:16px; bottom:16px; border:1px solid var(--line); background:var(--card2); color:var(--txt); padding:10px 16px; border-radius:999px; font-weight:700; cursor:pointer; opacity:.85; }
  .ptt.on { background:rgba(34,211,238,.25); border-color:var(--cyan); color:#fff; }
  .stale-banner { display:none; position:fixed; top:14px; right:18px; z-index:45; padding:7px 14px; border-radius:999px; background:rgba(245,158,11,.22); border:1.5px solid var(--amber); color:#fef3c7; font-size:clamp(12px,1.25vw,16px); font-weight:800; letter-spacing:.04em; box-shadow:0 4px 18px rgba(0,0,0,.45); }
  body.stale .stale-banner { display:inline-flex; align-items:center; gap:8px; }
  body.stale .hr-bpm, body.stale .target-num, body.stale .s-logged { opacity:.55; }
  .wb-replay { position:fixed; inset:0; z-index:50; background:#030712; display:flex; flex-direction:column; }
  .wb-replay[hidden] { display:none !important; }
  #wbRepVid { width:100%; height:100%; object-fit:contain; display:block; background:#000; }
  .wb-rep-top { position:absolute; left:24px; right:24px; top:20px; display:flex; justify-content:space-between; align-items:flex-start; gap:16px; pointer-events:none; }
  .wb-rep-title { background:rgba(7,11,18,.84); border:1px solid rgba(56,189,248,.45); border-radius:14px; padding:10px 16px; display:flex; flex-direction:column; gap:2px; max-width:60%; }
  .wb-rep-title b { font-size:clamp(18px,2.2vw,28px); font-weight:900; color:#fff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .wb-rep-title small { font-size:clamp(12px,1.4vw,18px); color:#93c5fd; font-weight:700; }
  .wb-rep-hud { display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
  .wb-hud-pill { background:rgba(7,11,18,.86); border:1px solid #1e2b42; border-radius:999px; padding:8px 16px; font-weight:800; font-variant-numeric:tabular-nums; font-size:clamp(14px,1.6vw,22px); color:#fff; }
  .wb-rep-foot { position:absolute; left:24px; right:24px; bottom:20px; display:flex; justify-content:space-between; align-items:center; pointer-events:none; font-size:clamp(12px,1.3vw,17px); font-weight:700; color:#cbd5e1; text-shadow:0 1px 6px #000; }
  .wb-rep-foot span { background:rgba(7,11,18,.78); padding:6px 12px; border-radius:999px; border:1px solid rgba(255,255,255,.12); }
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
    .wb-chk { width:18px; height:18px; font-size:12px; border-width:1.5px; }
    .wb-chk.sm { width:16px; height:16px; font-size:11px; }
    .ex-list { gap:4px; }
    .ex-list.dense { gap:3px; }
    .ex-row { padding:4px 9px; gap:8px; }
    .ex-row em { font-size:clamp(11px,2vh,14px); min-width:26px; }
    .ex-main b { font-size:clamp(11px,2.15vh,14px); }
    .ex-main small { font-size:clamp(10px,1.75vh,12px); }
    .ex-r span { font-size:clamp(11px,2vh,13px); }
  }
</style>
</head>
<body>
<div class="wrap" id="app">
  <div class="top"><div class="brand"><b>ARMORY FIT</b><span class="pill">Connecting…</span></div><div class="top-mid"></div><div class="top-r"><div class="clk" id="clk">--:--</div></div></div>
</div>
<div class="rec-halo" id="recHalo" aria-hidden="true"></div>
<div class="stale-banner" id="staleBanner"></div>
<div class="wb-replay" id="wbReplay" hidden>
  <video id="wbRepVid" playsinline muted></video>
  <div class="wb-rep-top">
    <div class="wb-rep-title"><b id="wbRepT">—</b><small id="wbRepSub">—</small></div>
    <div class="wb-rep-hud" id="wbRepHud"></div>
  </div>
  <div class="wb-rep-foot"><span id="wbRepBadge">FORM REPLAY</span></div>
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
let lastRxMs = Date.now();
let lastState = null;
let activeReplayKey = '';
let dismissedReplayKey = '';
let replayLoopsLeft = 0;
let replayLoading = false;
let replayT0 = 0;
let replayT1 = 0;
let replayEndBound = 0;
let replayRate = 1;
let replayMeta = { camera: '', rate: 1, viewIndex: 0, viewCount: 0 };

const wbReplayEl = document.getElementById('wbReplay');
const wbRepVid = document.getElementById('wbRepVid');
const wbRepBadge = document.getElementById('wbRepBadge');

function replayBadgeText(playNum) {
  return [
    replayMeta.camera || '',
    replayMeta.viewCount > 1 && replayMeta.viewIndex > 0 ? 'VIEW ' + replayMeta.viewIndex + '/' + replayMeta.viewCount : '',
    replayMeta.rate && replayMeta.rate !== 1 ? replayMeta.rate + '× SLOW-MO' : '',
    'PLAY ' + playNum + '/2',
  ].filter(Boolean).join(' · ');
}

function closeLocalReplay() {
  replayLoading = false;
  try { wbRepVid.pause(); } catch {}
  wbRepVid.removeAttribute('src');
  try { wbRepVid.load(); } catch {}
  if (wbReplayEl) wbReplayEl.hidden = true;
}

if (wbRepVid) {
  wbRepVid.addEventListener('loadedmetadata', () => {
    replayLoading = false;
    const dur = isFinite(wbRepVid.duration) ? wbRepVid.duration : 0;
    replayEndBound = replayT1 > replayT0 + 0.15 ? (dur ? Math.min(replayT1, dur) : replayT1) : dur;
    if (replayT0 > 0 && dur > 0.2) wbRepVid.currentTime = Math.min(replayT0, dur - 0.1);
    wbRepVid.playbackRate = replayRate;
    wbRepVid.play().catch(() => {});
  });
  const advanceReplayLoop = (fromEnded) => {
    if (!activeReplayKey || wbReplayEl.hidden || replayLoading) return;
    if (!fromEnded && !(replayEndBound > 0 && wbRepVid.currentTime >= replayEndBound - 0.05)) return;
    replayLoopsLeft--;
    if (replayLoopsLeft > 0) {
      wbRepVid.currentTime = replayT0;
      wbRepVid.playbackRate = replayRate;
      wbRepVid.play().catch(() => {});
      if (wbRepBadge) wbRepBadge.textContent = replayBadgeText(2);
    } else {
      dismissedReplayKey = activeReplayKey;
      closeLocalReplay();
    }
  };
  wbRepVid.addEventListener('timeupdate', () => advanceReplayLoop(false));
  wbRepVid.addEventListener('ended', () => advanceReplayLoop(true));
}

function syncReplay(st, hr, hrColor) {
  const rep = st.replay;
  const hud = document.getElementById('wbRepHud');
  if (hud) {
    const pills = [];
    if (st.lift && st.lift.rest && st.lift.rest.active) {
      pills.push('<span class="wb-hud-pill" style="border-color:' + (st.lift.rest.over ? 'var(--green)' : 'var(--blue)') + ';color:' + (st.lift.rest.over ? '#86efac' : '#7dd3fc') + '">' + (st.lift.rest.over ? 'GO +' : 'REST ') + mmss(Math.abs(st.lift.rest.remainingSec)) + '</span>');
    }
    if (st.lift) {
      pills.push('<span class="wb-hud-pill" style="border-color:rgba(56,189,248,.45);color:#7dd3fc">SINCE LAST ' + (st.lift.sinceLastSetSec != null ? mmss(st.lift.sinceLastSetSec) : '--:--') + '</span>');
    }
    pills.push('<span class="wb-hud-pill" style="color:' + hrColor + '">' + (hr.fresh && hr.bpm ? '♥ ' + hr.bpm + ' BPM' : 'NO HR') + '</span>');
    hud.innerHTML = pills.join('');
  }
  if (!rep || !rep.url) {
    if (activeReplayKey || (wbReplayEl && !wbReplayEl.hidden)) closeLocalReplay();
    activeReplayKey = '';
    dismissedReplayKey = '';
    return;
  }
  const key = String(rep.id || '') + ':' + String(rep.startedAtMs || 0);
  if (key === dismissedReplayKey) return;
  if (key !== activeReplayKey) {
    activeReplayKey = key;
    replayLoading = true;
    replayLoopsLeft = 2;
    replayT0 = Math.max(0, Number(rep.trimStartSec) || 0);
    replayT1 = Number(rep.trimEndSec) || 0;
    replayEndBound = 0;
    replayRate = Number(rep.rate) || 1;
    replayMeta = {
      camera: rep.camera || '',
      rate: replayRate,
      viewIndex: Number(rep.viewIndex) || 0,
      viewCount: Number(rep.viewCount) || 0,
    };
    const tEl = document.getElementById('wbRepT');
    const sEl = document.getElementById('wbRepSub');
    if (tEl) tEl.textContent = rep.title || 'Form Replay';
    if (sEl) sEl.textContent = rep.subtitle || '';
    if (wbRepBadge) wbRepBadge.textContent = replayBadgeText(1);
    if (wbReplayEl) wbReplayEl.hidden = false;
    try { wbRepVid.pause(); } catch {}
    wbRepVid.playbackRate = replayRate;
    wbRepVid.defaultPlaybackRate = replayRate;
    wbRepVid.src = rep.url;
    try { wbRepVid.load(); } catch {}
  }
}

function markFresh() {
  lastRxMs = Date.now();
  document.body.classList.remove('stale');
}

function render(st) {
  if (!st) return;
  if (st.htmlVersion && window.__WB_HTML_VER && st.htmlVersion !== window.__WB_HTML_VER) {
    try { location.reload(); } catch {}
    return;
  }
  lastState = st;
  markFresh();
  const app = document.getElementById('app');
  const nowStr = new Date().toLocaleTimeString([], { hour:'numeric', minute:'2-digit' });
  const modePill = st.mode === 'lift'
    ? '<span class="pill live">' + (st.lift.paused ? 'PAUSED' : 'LIVE · LIFT') + ' · ' + mmss(st.lift.elapsedSec) + '</span>'
    : st.mode === 'cardio'
      ? '<span class="pill cardio">LIVE · CARDIO</span>'
      : '<span class="pill">STANDBY' + (st.standby?.weekLabel ? ' · ' + esc(st.standby.weekLabel) : '') + '</span>';
  const sincePill = st.mode === 'lift'
    ? '<span class="pill since">SINCE LAST SET ' + (st.lift.sinceLastSetSec != null ? mmss(st.lift.sinceLastSetSec) : '--:--') + '</span>'
    : '';

  const v = st.voice || {};
  const micCls = v.armed ? 'mic armed' : v.listening ? 'mic on' : 'mic';
  const micLbl = v.armed ? 'LISTENING…' : v.listening ? '“' + esc((v.wakeWord || 'armory').toUpperCase()) + ' …”' : 'MIC OFF';
  const micBadge = st.mode === 'lift' ? '<div class="' + micCls + '"><i class="dot"></i><span>' + micLbl + '</span></div>' : '';
  const cam = st.camera || {};
  const isCamRec = !!cam.recording;
  const isCamSaving = !!cam.saving;
  document.body.classList.toggle('cam-rec', isCamRec);
  const camList = Array.isArray(cam.cameras) ? cam.cameras.filter(Boolean) : [];
  let recBadge = '';
  if (isCamRec) {
    const camTxt = cam.guest
      ? (camList.length ? 'GUEST REC · ' + camList.join(' + ') : 'GUEST REC')
      : (camList.length > 1 ? 'REC · ' + camList.length + ' CAMS' : camList.length === 1 ? 'REC · ' + camList[0] : 'REC');
    recBadge = '<div class="rec-pill"><i class="dot"></i><span>' + esc(camTxt) + '</span></div>';
  } else if (isCamSaving) {
    recBadge = '<div class="rec-pill saving"><i class="dot"></i><span>' + (cam.guest ? 'SAVING GUEST…' : 'SAVING CLIP…') + '</span></div>';
  }
  const banner = st.mode === 'lift' && v.banner ? '<div class="vtoast ' + esc(v.banner.kind) + '">' + esc(v.banner.text) + '</div>' : '';

  const hr = st.hr || {};
  const hrColor = ZCOL[hr.zone || 0] || '#fff';
  const readyMap = {
    ready: ['ready', 'READY FOR SET', '≤ ' + (hr.readyLimitBpm || 130) + ' BPM' + (hr.drop60 ? ' · ↓' + hr.drop60 + ' / 60s' : '')],
    recovering: ['recovering', 'RECOVERING', 'TARGET ≤ ' + (hr.readyLimitBpm || 130) + ' BPM'],
    resting: ['resting', 'RESTING', 'Z' + (hr.zone || 1) + ' · ' + (hr.pctMax || 0) + '% MAX'],
    idle: ['idle', 'NO STRAP', '']
  };
  const rm = readyMap[hr.readiness || 'idle'];
  const hrHtml = '<section class="card hr-card"><div><div class="kicker">POLAR H10 HEARTRATE</div><div class="hr-bpm" style="color:' + hrColor + '">' + (hr.fresh && hr.bpm ? hr.bpm : '--') + '</div>' + (hr.fresh && hr.bpm ? '<div class="cur-meta">Zone ' + hr.zone + ' · ' + hr.pctMax + '% HRmax</div>' : '') + '</div><div class="hr-ready ' + rm[0] + '"><div>' + rm[1] + '</div>' + (rm[2] ? '<div style="font-size:.7em;opacity:.85;margin-top:4px">' + rm[2] + '</div>' : '') + '</div></section>';
  syncReplay(st, hr, hrColor);

  const renderSetRow = (slot, exType) => {
    const tagTxt = slot.kind === 'w' ? 'W' : 'SET ' + slot.label;
    if (slot.state === 'done') {
      const valTxt = exType === 'time'
        ? (slot.weightVal && slot.weightVal !== 'HOLD' ? slot.repsVal + ' @ ' + slot.weightVal : slot.repsVal)
        : slot.weightVal + ' × ' + slot.repsVal;
      return '<div class="s-row done"><span class="s-tag">' + esc(tagTxt) + '</span>'
        + '<div class="s-slots"><span class="s-logged">' + esc(valTxt) + '</span>'
        + (slot.rpeText ? '<span class="s-rpe">' + esc(slot.rpeText) + '</span>' : '')
        + '</div><span class="wb-chk on">✓</span></div>';
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
        + '</div><span class="wb-chk"></span></div>';
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
    return '<div class="s-row active"><span class="s-tag">▶ ' + esc(tagTxt) + '</span>'
      + '<div class="s-slots">' + slotsHtml + '</div>'
      + '<span class="wb-chk active"></span></div>';
  };

  const renderVoiceBar = (vh) => '<div class="vbar ' + esc(vh.micState) + '">'
    + '<div class="vbar-l"><i class="dot"></i><span>' + esc(vh.headline || 'SAY') + '</span></div>'
    + '<div class="vchips">' + vh.chips.map((ch) => '<span class="vchip' + (ch.primary ? ' primary' : '') + '"><b>' + esc(ch.say) + '</b></span>').join('') + '</div></div>';

  const renderStation = (stCard, isSingle) => {
    const cls = 'st-card' + (isSingle ? ' card single' : '') + (stCard.isActive ? ' active' : ' partner');
    return '<section class="' + cls + '">'
      + '<div class="st-top"><div class="st-title"><span class="st-lab">' + esc(stCard.label) + '</span><span class="st-name">' + esc(stCard.exName) + '</span></div>'
      + '<div class="rx-pills">'
      + (stCard.lastBenchmark ? '<span class="rx-pill last">Last ' + esc(stCard.lastBenchmark) + '</span>' : '')
      + '<span class="rx-pill reps">' + esc(stCard.repTarget) + '</span>'
      + (stCard.rpeTarget ? '<span class="rx-pill rpe">' + esc(stCard.rpeTarget) + '</span>' : '') + '</div></div>'
      + '<div class="set-rows cur-body">' + stCard.sets.map((s) => renderSetRow(s, stCard.exType)).join('') + '</div>'
      + (stCard.isActive && stCard.voiceHint ? renderVoiceBar(stCard.voiceHint) : '')
      + '</section>';
  };

  const renderOnDeck = (od) => '<section class="card ondeck"><div class="od-head"><div class="kicker">ON DECK NEXT</div>'
    + '<span class="od-tag">' + (od.isSuperset ? 'SUPERSET PAIR · ' : '') + esc(od.label) + '</span></div>'
    + '<div class="od-items">' + od.items.map((it) => '<div class="od-row"><em>' + esc(it.label) + '</em><b>' + esc(it.exName) + '</b><span>'
      + [it.sets + '× ' + esc(it.repTarget), it.rpeTarget ? esc(it.rpeTarget) : '', it.lastText ? 'Last ' + esc(it.lastText) : ''].filter(Boolean).join(' · ')
      + '</span></div>').join('')
    + '</div></section>';

  const renderExRow = (e) => '<div class="ex-row' + (e.isCurrent ? ' cur' : '') + (e.isDone ? ' done' : '') + '">'
    + '<em>' + esc(e.label) + '</em>'
    + '<div class="ex-main"><b>' + esc(e.name) + '</b>' + (e.subText ? '<small class="' + (e.isPrescribedLoad && !e.isDone ? 'rx-hi' : '') + '">' + esc(e.subText) + '</small>' : '') + '</div>'
    + '<div class="ex-r"><span>' + e.doneSets + '/' + e.totalSets + '</span><span class="wb-chk sm' + (e.isDone ? ' on' : (e.isCurrent ? ' active' : '')) + '">' + (e.isDone ? '✓' : '') + '</span></div>'
    + '</div>';

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
      leftStage = '<section class="card hero"><div class="cur-body" style="margin:auto 0"><div class="kicker">' + esc(l.title) + '</div><div class="cur-name" style="margin-top:8px">All ' + l.setsTotal + ' Sets Complete</div></div></section>';
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
      + '<div class="col">' + liftHrHtml + '<section class="card" style="flex:1;min-height:0;display:flex;flex-direction:column;gap:10px"><div class="kicker">' + esc(l.title) + ' · ' + l.setsDone + '/' + l.setsTotal + ' SETS' + (l.setsDone > 0 ? ' · ' + esc(l.volumeText) : '') + '</div><div class="ex-list' + (exArr.length >= 8 ? ' dense' : '') + '">' + exRows + '</div></section></div></div>';
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
    const comp = Array.isArray(sb.completedItems) ? sb.completedItems : [];
    const pend = Array.isArray(sb.todayItems) ? sb.todayItems : [];
    const doneRows = comp.map((c) => '<div class="sb-item done"><div class="sb-row"><div><b>' + esc(c.title) + '</b><span>' + esc([c.equipmentLabel, c.summary].filter(Boolean).join(' · ')) + '</span></div><span class="sb-done-pill">✓ DONE</span></div></div>').join('');
    const pendRows = pend.map((it) => '<div class="sb-item"><b>' + esc(it.title) + (it.carried ? ' · Carry-over' : '') + '</b><span>' + esc(it.equipmentLabel) + (it.preview ? ' · ' + esc(it.preview) : '') + '</span></div>').join('');
    const nextRow = (pend.length === 0 && sb.nextUp && sb.nextUp.title)
      ? '<div class="sb-next"><em>UP NEXT</em><b>' + esc(sb.nextUp.title) + '</b><span>' + esc(sb.nextUp.dateLabel || '') + '</span></div>'
      : '';
    const allDoneToday = pend.length === 0 && comp.length > 0;
    const schedKicker = allDoneToday ? 'TODAY COMPLETE' : 'TODAY ON THE SCHEDULE';
    const items = (doneRows || pendRows)
      ? (doneRows + pendRows + nextRow)
      : (sb.scheduleSynced === false
        ? '<div class="sb-item"><b>—</b></div>'
        : sb.isRestDay === false
          ? ('<div class="sb-item"><b>All Caught Up</b></div>' + nextRow)
          : ('<div class="sb-item"><b>Rest Day</b></div>' + nextRow));
    main = '<div class="grid"><div class="col"><section class="card hero"><div class="kicker">THE ARMORY · ' + esc(sb.dateLabel) + '</div><div class="standby-big">' + esc(sb.timeLabel || nowStr) + '</div>' + (sb.weekLabel ? '<div class="cur-meta">' + esc(sb.weekLabel) + '</div>' : '') + '</section></div>'
      + '<div class="col">' + hrHtml + '<section class="card" style="flex:1;display:flex;flex-direction:column;gap:10px"><div class="kicker' + (allDoneToday ? ' done' : '') + '">' + schedKicker + '</div>' + items + '</section></div></div>';
  }

  app.innerHTML = '<div class="top"><div class="brand"><b>ARMORY FIT</b>' + modePill + sincePill + '</div><div class="top-mid">' + banner + '</div><div class="top-r">' + recBadge + micBadge + '<div class="clk">' + esc(nowStr) + '</div></div></div>' + main;
}

window.addEventListener('message', (ev) => {
  if (ev.data && ev.data.type === 'armory:wallboard-state') render(ev.data.state);
});

const WB_BASE = location.pathname.endsWith('/wallboard') || location.pathname.endsWith('/wallboard/') ? '/wallboard' : '';
const wbUrl = (p) => WB_BASE + p;

async function pollOnce() {
  if (location.protocol === 'about:' || location.protocol === 'blob:') return;
  try {
    const r = await fetch(wbUrl('/state') + '?t=' + Date.now(), { cache:'no-store' });
    if (r.ok) {
      markFresh();
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
      const es = new EventSource(wbUrl('/events'));
      es.onmessage = (e) => {
        markFresh();
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

setInterval(() => {
  const nowD = new Date();
  const t = nowD.toLocaleTimeString([], { hour:'numeric', minute:'2-digit' });
  document.querySelectorAll('.clk, .standby-big').forEach((el) => { el.textContent = t; });
  const stale = !!(lastState && lastState.mode !== 'standby' && Date.now() - lastRxMs > 12000);
  document.body.classList.toggle('stale', stale);
  if (stale) {
    const sbEl = document.getElementById('staleBanner');
    const agoT = new Date(lastRxMs).toLocaleTimeString([], { hour:'numeric', minute:'2-digit' });
    if (sbEl) sbEl.textContent = 'PHONE PAUSED · last update ' + agoT;
  }
  if (lastState && lastState.mode === 'standby' && lastState.standby && lastState.standby.ymd) {
    const localToday = nowD.getFullYear() + '-' + String(nowD.getMonth() + 1).padStart(2, '0') + '-' + String(nowD.getDate()).padStart(2, '0');
    if (localToday !== lastState.standby.ymd) pollOnce();
  }
}, 5000);

// Optional LAN browser speech relay (available in Chrome on OptiPlex/tablets; hidden on Silk/devices without Web Speech)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR && location.protocol.startsWith('http') && !/; wv[)]|AFT/i.test(navigator.userAgent)) {
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
          fetch(WB_BASE ? '/wallboard/voice' : '/api/voice', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ transcript }) }).catch(() => {});
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
