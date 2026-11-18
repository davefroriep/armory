import { bikeLink, hrLink, spinLink } from './devices';
import { hrFresh, bikeFresh, linkStatus, live } from './telemetry';
import { sim } from './sim';
import { getSettings } from '../store/settings';

export type BikeKind = 'airbike' | 'spin';

let bikeKind: BikeKind = 'airbike';

export const activeBikeKind = (): BikeKind => bikeKind;
export const activeBikeLink = () => (bikeKind === 'spin' ? spinLink : bikeLink);

/** Start (or keep) the sensors this screen needs. Silent: no chooser prompts. */
export function ensureSensors(opts: { bike: false | boolean | BikeKind }): void {
  if (opts.bike) bikeKind = opts.bike === 'spin' ? 'spin' : 'airbike';
  sim.setMode(bikeKind);
  if (getSettings().simulator) {
    sim.start();
    return;
  }
  sim.stop();
  void hrLink.ensure();
  if (opts.bike) {
    if (bikeKind === 'spin') {
      bikeLink.release();
      void spinLink.ensure();
    } else {
      spinLink.release();
      void bikeLink.ensure();
    }
  }
}

/** Retry a stalled sensor link right now (app resumed, chip tapped). */
export function kickSensors(): void {
  if (getSettings().simulator) return;
  hrLink.kick();
  activeBikeLink().kick();
}

export function releaseBike(): void {
  bikeLink.release();
  spinLink.release();
}

export type ChipKind = 'hr' | 'bike';
export interface ChipView {
  cls: 'on' | 'busy' | 'off' | 'na';
  text: string;
}

export function chipView(kind: ChipKind): ChipView {
  const spin = bikeKind === 'spin';
  if (getSettings().simulator) {
    if (kind === 'hr') return { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ SIM' };
    return { cls: 'on', text: spin ? `${Math.round(live.w)} W · SIM` : `${Math.round(live.cad)} RPM · SIM` };
  }
  const link = kind === 'hr' ? hrLink : activeBikeLink();
  if (link.state === 'unsupported') return { cls: 'na', text: kind === 'hr' ? '♥ NO BLE' : 'NO BLE' };
  if (link.connected) {
    if (kind === 'hr') return { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ …' };
    // Live watts (Spin) or pedal RPM (AirBike) on the chip doubles as alive confirmation.
    return bikeFresh()
      ? { cls: 'on', text: spin ? `${Math.round(live.w)} W` : `${Math.round(live.cad)} RPM` }
      : { cls: 'busy', text: spin ? 'SPIN …' : 'BIKE …' };
  }
  if (link.state === 'connecting' || link.state === 'lost') {
    return { cls: 'busy', text: kind === 'hr' ? '♥ …' : spin ? 'SPIN …' : 'BIKE …' };
  }
  return { cls: 'off', text: kind === 'hr' ? '♥ CONNECT' : spin ? 'CONNECT SPIN' : 'CONNECT BIKE' };
}

/** Tap on a chip: pairs if not connected (needs the user gesture), otherwise does nothing. */
export function chipTap(kind: ChipKind): void {
  if (getSettings().simulator) return;
  const link = kind === 'hr' ? hrLink : activeBikeLink();
  if (link.connected || link.state === 'unsupported') return;
    // Already paired: just retry now. First time (or after Forget): open the chooser.
  if (link.hasDevice) link.kick();
  else void link.pair();
}

/** Status line for the HUD while the bike is not delivering data. */
export function bikeStatusNote(): string {
  if (getSettings().simulator) return '';
  const link = activeBikeLink();
  if (link.connected) return bikeFresh() ? '' : 'Connected · waiting for the first reading…';
  const d = link.diag();
  const label = bikeKind === 'spin' ? 'Spin Bike (SS2K)' : 'AirBike';
  return d ? `${label}: ${d}. Tap the chip to retry.` : '';
}

export { linkStatus };
