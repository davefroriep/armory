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
    return kind === 'hr' ? { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ SIM' } : { cls: 'on', text: 'BIKE SIM' };
  }
  const link = kind === 'hr' ? hrLink : bikeLink;
  if (link.state === 'unsupported') return { cls: 'na', text: kind === 'hr' ? '♥ NO BLE' : 'NO BLE' };
  if (link.connected) {
    if (kind === 'hr') return { cls: 'on', text: hrFresh() ? `♥ ${live.hr}` : '♥ …' };
    return { cls: bikeFresh() ? 'on' : 'busy', text: 'BIKE' };
  }
  if (link.state === 'connecting' || link.state === 'lost') return { cls: 'busy', text: kind === 'hr' ? '♥ …' : 'BIKE …' };
  return { cls: 'off', text: kind === 'hr' ? '♥ CONNECT' : 'CONNECT BIKE' };
}

/** Tap on a chip: pairs if not connected (needs the user gesture), otherwise does nothing. */
export function chipTap(kind: ChipKind): void {
  if (getSettings().simulator) return;
  const link = kind === 'hr' ? hrLink : bikeLink;
  if (!link.connected && link.state !== 'unsupported') void link.pair();
}

export { linkStatus };
