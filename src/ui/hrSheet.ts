import { hrLink } from '../ble/devices';
import { chipTap } from '../ble/sources';
import { currentStrapBeats, hrBeat, hrFresh, linkStatus, live, recentHrPoints } from '../ble/telemetry';
import { bodyProfile, kcalFromSamples } from '../domain/energy';
import { recoveryLimitBpm } from '../domain/gate';
import { active } from '../lifts/active';
import { getSettings } from '../store/settings';
import { mmss } from './dom';
import { ZONE_COLOR, ZONE_NAME, hrDropStats, hrZone } from './hr';
import { openSheet } from './lifts/kit';

let activeHrSheet: { close: () => void } | null = null;

function renderSparkline(
  pts: { at: number; hr: number }[],
  now: number,
  windowMs: number,
  recLimit: number,
  color: string,
): string {
  const valid = pts.filter((p) => p.hr > 0);
  if (valid.length < 2) {
    return `<div class="hr-spark-empty">Waiting for enough beats to plot a 2-min trace…</div>`;
  }
  const W = 280;
  const H = 74;
  const padY = 8;
  let lo = Math.min(recLimit, ...valid.map((p) => p.hr));
  let hi = Math.max(recLimit, ...valid.map((p) => p.hr));
  if (hi - lo < 16) {
    const mid = (hi + lo) / 2;
    lo = Math.max(30, mid - 8);
    hi = mid + 8;
  } else {
    lo = Math.max(30, lo - 4);
    hi += 4;
  }
  const startAt = now - windowMs;
  const xOf = (at: number): number => Math.max(0, Math.min(W, ((at - startAt) / windowMs) * W));
  const yOf = (hr: number): number => padY + ((hi - hr) / Math.max(1, hi - lo)) * (H - padY * 2);

  const recY = yOf(recLimit).toFixed(1);
  const coords = valid.map((p) => [xOf(p.at), yOf(p.hr)] as const);
  const linePts = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const firstX = coords[0][0].toFixed(1);
  const [lastX, lastY] = coords[coords.length - 1];
  const areaPts = `${firstX},${H} ${linePts} ${lastX.toFixed(1)},${H}`;

  return `<svg class="hr-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="hrGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity=".35"/>
        <stop offset="100%" stop-color="${color}" stop-opacity=".02"/>
      </linearGradient>
    </defs>
    <line x1="0" y1="${recY}" x2="${W}" y2="${recY}" stroke="rgba(138,148,163,.38)" stroke-width="1" stroke-dasharray="3 3"/>
    <text x="${W - 2}" y="${Math.max(9, Number(recY) - 3)}" text-anchor="end" fill="#8a94a3" font-size="8.5" font-weight="700">${recLimit} rec</text>
    <polygon points="${areaPts}" fill="url(#hrGrad)"/>
    <polyline points="${linePts}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="${color}"/>
  </svg>`;
}

/** Concise live Heart Rate telemetry sheet opened by tapping the top [ ♥ HR ] chip. */
export function openHrSheet(): void {
  if (activeHrSheet) return;

  const s = getSettings();
  if (!s.simulator && !hrLink.connected && hrLink.hasDevice && hrLink.state !== 'unsupported') {
    hrLink.kick();
  }

  let dead = false;
  let offBeat = () => {};
  let offLink = () => {};
  let timer: ReturnType<typeof setInterval> | null = null;

  const sh = openSheet('<div id="hrBody"></div>', {
    onClose: () => {
      dead = true;
      offBeat();
      offLink();
      if (timer) clearInterval(timer);
      activeHrSheet = null;
    },
  });
  sh.body.classList.add('hr-sheet');
  activeHrSheet = sh;

  const host = sh.body.querySelector<HTMLElement>('#hrBody')!;
  host.innerHTML = `<div class="hr-hero" id="hrHero" style="--zc:#8a94a3">
      <div class="hr-top">
        <span class="hr-conn" id="hrConn"><i id="hrDot"></i><span id="hrConnTxt"></span></span>
        <span class="hr-zpill" id="hrZpill"></span>
      </div>
      <div class="hr-bpm" id="hrBpm">-- <small>BPM</small></div>
    </div>

    <div class="hr-spark-card">
      <div class="hr-spark-h"><span>LAST 2 MIN</span><b id="hrDropTxt"></b></div>
      <div id="hrSparkBox"></div>
    </div>

    <div class="hr-sec-h"><span id="hrScope"></span><span id="hrMaxCfg"></span></div>
    <div class="stats three hr-stats">
      <div><small>AVG HR</small><b id="hrAvg">--</b></div>
      <div><small>MAX HR</small><b id="hrMax">--</b></div>
      <div><small>~KCAL</small><b id="hrKcal">--</b></div>
    </div>

    <div class="hr-zones-card">
      <div class="card-h"><span>TIME IN ZONE</span></div>
      <div class="zones">${[1, 2, 3, 4, 5].map((zn) => `<div class="zrow"><span>Z${zn}</span><i><u id="hrZbar${zn}" style="width:0%;background:${ZONE_COLOR[zn]}"></u></i><b id="hrZsec${zn}">0:00</b></div>`).join('')}</div>
    </div>

    <div class="hr-acts" id="hrActs">
      <button class="btn primary big" type="button" data-hr-conn id="hrConnBtn" hidden></button>
      <button class="btn big" type="button" data-hr-close>Close</button>
    </div>`;

  const q = (id: string) => host.querySelector<HTMLElement>('#' + id)!;

  const render = (): void => {
    if (dead) return;
    const cfg = getSettings();
    const now = Date.now();
    const hr = hrFresh(now) ? live.hr : 0;
    const z = hr > 0 ? hrZone(hr, cfg.hrMax) : null;
    const zCol = z ? ZONE_COLOR[z.z] : '#8a94a3';
    const zLabel = z ? `Z${z.z} · ${ZONE_NAME[z.z]} · ${Math.round(z.pct * 100)}%` : 'No live signal';

    const statusTxt = cfg.simulator
      ? 'Simulator · Polar H10'
      : hrLink.state === 'unsupported'
        ? 'Bluetooth unavailable'
        : hrLink.connected
          ? `${hrLink.deviceName || 'Polar H10'} · connected`
          : hrLink.state === 'connecting' || hrLink.state === 'lost'
            ? `${hrLink.deviceName || 'Polar H10'} · reconnecting…`
            : hrLink.hasDevice
              ? `${hrLink.deviceName || 'Polar H10'} · disconnected`
              : 'Polar H10 not paired';

    const pts2m = recentHrPoints(120_000, now);
    const drop = hrDropStats(pts2m, hr, now, 60_000);
    const dropText = !drop
      ? 'No recent beats'
      : drop.drop >= 3
        ? `Peak ${drop.peak} → ${hr || drop.peak} · −${drop.drop} bpm`
        : drop.peak > drop.low
          ? `60s range ${drop.low}–${drop.peak} bpm`
          : `Steady at ${drop.peak} bpm`;

    const recLimit = recoveryLimitBpm(cfg.hrMax, cfg.recoveryHrPct);
    const recSamples = active.rec?.samples.filter((x) => x.hr > 0) ?? [];
    const strapSamples = recSamples.length ? [] : currentStrapBeats();
    const samples: { hr: number }[] = recSamples.length ? recSamples : strapSamples;
    const scopeLabel = recSamples.length ? 'THIS WORKOUT' : strapSamples.length ? 'STRAP SESSION' : 'SESSION';

    let sumHr = 0;
    let maxHr = 0;
    const secs = [0, 0, 0, 0, 0, 0];
    for (const smp of samples) {
      sumHr += smp.hr;
      if (smp.hr > maxHr) maxHr = smp.hr;
      secs[hrZone(smp.hr, cfg.hrMax).z]++;
    }
    const avgHr = samples.length ? Math.round(sumHr / samples.length) : 0;
    const prof = bodyProfile(cfg);
    const kcal = prof && samples.length ? kcalFromSamples(samples, prof) : null;
    const totalSec = Math.max(1, secs.slice(1).reduce((a, b) => a + b, 0));

    q('hrHero').style.setProperty('--zc', zCol);
    q('hrDot').className = hrLink.connected || cfg.simulator ? 'on' : '';
    q('hrConnTxt').textContent = statusTxt;
    const zp = q('hrZpill');
    zp.style.color = zCol;
    zp.style.background = `${zCol}22`;
    zp.style.boxShadow = `inset 0 0 0 1px ${zCol}66`;
    zp.textContent = zLabel;
    const bpmEl = q('hrBpm');
    bpmEl.style.color = zCol;
    bpmEl.innerHTML = `${hr > 0 ? hr : '--'} <small>BPM</small>`;

    q('hrDropTxt').textContent = dropText;
    q('hrSparkBox').innerHTML = renderSparkline(pts2m, now, 120_000, recLimit, zCol);

    q('hrScope').textContent = scopeLabel;
    q('hrMaxCfg').textContent = `HRmax ${cfg.hrMax}`;
    q('hrAvg').textContent = avgHr ? String(avgHr) : '--';
    q('hrMax').textContent = maxHr ? String(maxHr) : '--';
    q('hrKcal').textContent = kcal != null ? String(kcal) : '--';

    for (const zn of [1, 2, 3, 4, 5]) {
      q(`hrZbar${zn}`).style.width = `${(secs[zn] / totalSec) * 100}%`;
      q(`hrZsec${zn}`).textContent = mmss(secs[zn]);
    }

    const showConnBtn = !cfg.simulator && !hrLink.connected && hrLink.state !== 'unsupported';
    const connBtn = q('hrConnBtn');
    connBtn.hidden = !showConnBtn;
    if (showConnBtn) connBtn.textContent = hrLink.hasDevice ? 'Reconnect Polar H10' : 'Pair Polar H10';
  };

  sh.body.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-hr-close]')) {
      sh.close();
      return;
    }
    if (t.closest('[data-hr-conn]')) {
      chipTap('hr');
      render();
    }
  });

  offBeat = hrBeat.on(render);
  offLink = linkStatus.on(render);
  timer = setInterval(render, 1000);
  render();
}
