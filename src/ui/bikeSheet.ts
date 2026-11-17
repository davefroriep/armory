import { bikeLink } from '../ble/devices';
import { chipTap } from '../ble/sources';
import { bikeFrame, bikeFresh, linkStatus, live } from '../ble/telemetry';
import { getSettings } from '../store/settings';
import { esc } from './dom';
import { openSheet, toast } from './lifts/kit';

let activeBikeSheet: { close: () => void } | null = null;

/** Opens AirBike BLE diagnostics & NUS calibration controls when connected, or pairs on tap. */
export function openBikeSheet(): void {
  const s = getSettings();
  if (!s.simulator && !bikeLink.connected) {
    chipTap('bike');
    return;
  }
  if (activeBikeSheet) return;

  let dead = false;
  let offFrame = () => {};
  let offLink = () => {};

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

  const render = (): void => {
    if (dead) return;
    const cfg = getSettings();
    const fresh = bikeFresh();
    const title = cfg.simulator ? 'Simulator · AssaultBike' : (bikeLink.deviceName || 'AssaultBike');
    const distMi = (live.dist / 1609.344).toFixed(2);
    host.innerHTML = `<h3 class="sh-title">${esc(title)}</h3>
      <p class="sh-text">${cfg.simulator ? 'Simulated 5 Hz telemetry active.' : fresh ? 'Connected · 5 Hz Direct BLE (NUS)' : 'Connected · waiting for frames…'}</p>
      <div class="stats three flat">
        <div><small>POWER</small><b>${Math.round(live.w)} W</b></div>
        <div><small>PEDAL</small><b>${live.cad.toFixed(1)}</b></div>
        <div><small>FAN RPM</small><b>${Math.round(live.fan)}</b></div>
      </div>
      <div class="stats three flat">
        <div><small>PULSES</small><b>${live.pls || '--'}</b></div>
        <div><small>DT (µs)</small><b>${live.dt || '--'}</b></div>
        <div><small>RATIO</small><b>${live.ratio.toFixed(2)}</b></div>
      </div>
      <p class="sh-text">Bike odometer: ${distMi} mi · ${live.kcal.toFixed(1)} Assault kcal</p>
      ${!cfg.simulator ? '<button class="btn primary big" type="button" data-bk="zero">Zero Bike Distance &amp; Calories</button><button class="btn big" type="button" data-bk="drop">Disconnect Bike</button>' : ''}
      <button class="btn big" type="button" data-bk="close">Close</button>`;
  };

  sh.body.addEventListener('click', async (e) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('[data-bk]')?.dataset.bk;
    if (!b) return;
    if (b === 'close') { sh.close(); return; }
    if (b === 'zero') {
      try {
        await bikeLink.command('RESET_PULSES');
        toast('Sent RESET_PULSES to AssaultBike');
      } catch (err) {
        toast(`Command failed: ${esc((err as Error).message)}`);
      }
      return;
    }
    if (b === 'drop') {
      bikeLink.release();
      sh.close();
    }
  });

  offFrame = bikeFrame.on(render);
  offLink = linkStatus.on(render);
  render();
}
