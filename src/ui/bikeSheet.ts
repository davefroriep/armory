import { activeBikeKind, activeBikeLink, chipTap } from '../ble/sources';
import { bikeFrame, bikeFresh, linkStatus, live } from '../ble/telemetry';
import { getSettings } from '../store/settings';
import { wallboard } from '../wallboard/sync';
import { esc } from './dom';
import { confirmSheet, openSheet, toast } from './lifts/kit';

let activeBikeSheet: { close: () => void } | null = null;

/** Opens AirBike or Spin Bike (SS2K) BLE diagnostics & controls when connected, or pairs on tap. */
export function openBikeSheet(): void {
  const s = getSettings();
  const link = activeBikeLink();
  if (!s.simulator && !link.connected) {
    chipTap('bike');
    return;
  }
  if (activeBikeSheet) return;

  let dead = false;
  let offFrame = () => {};
  let offLink = () => {};
  let layoutKey = '';

  const sh = openSheet('<div id="bkBody"></div>', {
    onClose: () => {
      dead = true;
      offFrame();
      offLink();
      activeBikeSheet = null;
    },
  });
  activeBikeSheet = sh;
  const host = sh.body.querySelector<HTMLElement>('#bkBody')!;

  const mountLayout = (isSpin: boolean, simOn: boolean): void => {
    const statLabels = isSpin
      ? [['POWER', 'bkV0'], ['PEDAL', 'bkV1'], ['LEVEL', 'bkV2'], ['CONTROL', 'bkV3'], ['POT ADC', 'bkV4'], ['STEPS', 'bkV5']]
      : [['POWER', 'bkV0'], ['PEDAL', 'bkV1'], ['FAN RPM', 'bkV2'], ['PULSES', 'bkV3'], ['DT (µs)', 'bkV4'], ['RATIO', 'bkV5']];
    const row = (items: string[][]): string =>
      `<div class="stats three flat">${items.map(([lbl, id]) => `<div><small>${lbl}</small><b id="${id}">--</b></div>`).join('')}</div>`;
    const actBtns = isSpin
      ? (!simOn ? '<button class="btn primary big" type="button" data-bk="easy">Free Spin · Easy Lvl 5</button><button class="btn big" type="button" data-bk="drop">Disconnect Spin Bike</button>' : '')
      : (!simOn ? '<button class="btn primary big" type="button" data-bk="zero">Zero Bike Distance &amp; Calories</button><button class="btn big" type="button" data-bk="drop">Disconnect Bike</button>' : '');
    host.innerHTML = `<h3 class="sh-title" id="bkTitle"></h3>
      <p class="sh-text" id="bkSub"></p>
      ${row(statLabels.slice(0, 3))}
      ${row(statLabels.slice(3, 6))}
      <p class="sh-text" id="bkOdo"></p>
      ${actBtns}
      <button class="btn big" type="button" data-bk="close">Close</button>`;
  };

  const render = (): void => {
    if (dead) return;
    const cfg = getSettings();
    const fresh = bikeFresh();
    const curKind = activeBikeKind();
    const curLink = activeBikeLink();
    const isSpin = curKind === 'spin';
    const nextLayout = `${isSpin ? 'spin' : 'air'}|${cfg.simulator ? 1 : 0}`;
    if (nextLayout !== layoutKey) {
      layoutKey = nextLayout;
      mountLayout(isSpin, cfg.simulator);
    }

    const fallbackTitle = isSpin ? 'SS2K_Tuner' : 'AssaultBike';
    const title = cfg.simulator
      ? `Simulator · ${isSpin ? 'Bowflex C7 (SS2K)' : 'AssaultBike'}`
      : (curLink.deviceName || fallbackTitle);
    const sub = cfg.simulator ? 'Simulated 5 Hz telemetry active.' : fresh ? 'Connected · 5 Hz Direct BLE (NUS)' : 'Connected · waiting for frames…';
    const distMi = (live.dist / 1609.344).toFixed(2);
    const vals = isSpin
      ? [
          `${Math.round(live.w)} W`,
          `${live.cad.toFixed(1)} RPM`,
          live.lvl > 0 ? live.lvl.toFixed(0) : '--',
          live.mode === 1 ? `ERG ${live.tgtW} W` : `LVL ${live.tlvl || '--'}`,
          live.pot ? String(live.pot) : '--',
          live.pos ? String(live.pos) : '--',
        ]
      : [
          `${Math.round(live.w)} W`,
          live.cad.toFixed(1),
          String(Math.round(live.fan)),
          live.pls ? String(live.pls) : '--',
          live.dt ? String(live.dt) : '--',
          live.ratio.toFixed(2),
        ];
    const odo = isSpin
      ? `Virtual odometer: ${distMi} mi · ${live.kcal.toFixed(1)} kcal (1:1 kJ)`
      : `Bike odometer: ${distMi} mi · ${live.kcal.toFixed(1)} Assault kcal`;

    const q = (id: string) => host.querySelector<HTMLElement>('#' + id);
    const tEl = q('bkTitle');
    if (tEl) tEl.textContent = title;
    const sEl = q('bkSub');
    if (sEl) sEl.textContent = sub;
    vals.forEach((v, i) => {
      const el = q(`bkV${i}`);
      if (el) el.textContent = v;
    });
    const oEl = q('bkOdo');
    if (oEl) oEl.textContent = odo;
    const zeroBtn = host.querySelector<HTMLButtonElement>('[data-bk="zero"]');
    if (zeroBtn) zeroBtn.disabled = wallboard.latestState.mode === 'cardio';
  };

  sh.body.addEventListener('click', async (e) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('[data-bk]')?.dataset.bk;
    if (!b) return;
    const curLink = activeBikeLink();
    if (b === 'close') { sh.close(); return; }
    if (b === 'zero') {
      if (wallboard.latestState.mode === 'cardio') {
        const ok = await confirmSheet('Zero bike odometer?', 'A workout is currently running. Session totals will keep accumulating, but the bike odometer will reset.', 'Reset');
        if (!ok) return;
      }
      try {
        await curLink.command('RESET_PULSES');
        toast('Sent RESET_PULSES to AssaultBike');
      } catch (err) {
        toast(`Command failed: ${esc((err as Error).message)}`);
      }
      return;
    }
    if (b === 'easy') {
      try {
        await curLink.command('SET_MODE:0');
        await curLink.command('SET_LEVEL:5');
        toast('SS2K set to Free Mode · Level 5');
      } catch (err) {
        toast(`Command failed: ${esc((err as Error).message)}`);
      }
      return;
    }
    if (b === 'drop') {
      curLink.release();
      sh.close();
    }
  });

  offFrame = bikeFrame.on(render);
  offLink = linkStatus.on(render);
  render();
}
