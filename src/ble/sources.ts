import { bikeLink, hrLink } from './devices';
import { hrFresh, bikeFresh, linkStatus, live } from './telemetry';
import { sim } from './sim';
import { getSettings } from '../store/settings';

/** Start (or keep) the sensors this screen needs. Silent: no chooser prompts. */
export function ensureSensors(opts: { bike: boolean }): void {
  if (getSettings().simulator) {
    sim.start();
    return;
  }
  sim.stop();
  void hrLink.ensure();
  if (opts.bike) void bikeLink.ensure();
}

/** Retry a stalled sensor link right now (app resumed, chip tapped). */
export function kickSensors(): void {
  if (getSettings().simulator) return;
  hrLink.kick();
  bikeLink.kick();
}

export function releaseBike(): void {
  bikeLink.release();
}

export type ChipKind = 'hr' | 'bike';
export interface ChipView {
  cls: 'on' | 'busy' | 'off' | 'na';
  text: string;
}

export function chipView(kind: ChipKind): ChipView {
  if (getSettings().simulator) {
    return kind === 'hr' ? { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ SIM' } : { cls: 'on', text: `${Math.round(live.cad)} RPM · SIM` };
  }
  const link = kind === 'hr' ? hrLink : bikeLink;
  if (link.state === 'unsupported') return { cls: 'na', text: kind === 'hr' ? '♥ NO BLE' : 'NO BLE' };
  if (link.connected) {
    if (kind === 'hr') return { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ …' };
    // Live pedal RPM on the chip doubles as the "bike is alive" confirmation.
    return bikeFresh() ? { cls: 'on', text: `${Math.round(live.cad)} RPM` } : { cls: 'busy', text: 'BIKE …' };
  }
  if (link.state === 'connecting' || link.state === 'lost') return { cls: 'busy', text: kind === 'hr' ? '♥ …' : 'BIKE …' };
  return { cls: 'off', text: kind === 'hr' ? '♥ CONNECT' : 'CONNECT BIKE' };
}

/** Tap on a chip: pairs if not connected (needs the user gesture), otherwise does nothing. */
export function chipTap(kind: ChipKind): void {
  if (getSettings().simulator) return;
  const link = kind === 'hr' ? hrLink : bikeLink;
  if (link.connected || link.state === 'unsupported') return;
  // Already paired: just retry now. First time (or after Forget): open the chooser.
  if (link.hasDevice) link.kick();
  else void link.pair();
}

/** Status line for the HUD while the bike is not delivering data. */
export function bikeStatusNote(): string {
  if (getSettings().simulator) return '';
  if (bikeLink.connected) return bikeFresh() ? '' : 'Connected · waiting for the first reading…';
  const d = bikeLink.diag();
  return d ? `AirBike: ${d}. Tap the chip to retry.` : '';
}

export { linkStatus };
