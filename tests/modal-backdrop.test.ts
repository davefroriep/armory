import { describe, expect, it, vi } from 'vitest';
import { wireModalBackdrop } from '../src/ui/dom';

function makeEvent(type: string, opts: { target?: EventTarget; detail?: number; key?: string; cancelable?: boolean } = {}): Event {
  const ev = new Event(type, { bubbles: true, cancelable: opts.cancelable ?? true });
  if (opts.target) Object.defineProperty(ev, 'target', { value: opts.target, configurable: true });
  if (opts.detail !== undefined) Object.defineProperty(ev, 'detail', { value: opts.detail, configurable: true });
  if (opts.key !== undefined) Object.defineProperty(ev, 'key', { value: opts.key, configurable: true });
  return ev;
}

describe('wireModalBackdrop', () => {
  it('does not close on pointerdown alone so touch lift cannot hit shaded controls underneath', () => {
    const el = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, null);

    el.dispatchEvent(makeEvent('pointerdown', { target: el }));
    expect(close).not.toHaveBeenCalled();

    off();
  });

  it('closes once on pointerdown + click on backdrop and cancels propagation/default', () => {
    const el = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, null);

    el.dispatchEvent(makeEvent('pointerdown', { target: el }));
    const clickEv = makeEvent('click', { target: el, detail: 1, cancelable: true });
    const stopSpy = vi.spyOn(clickEv, 'stopPropagation');
    el.dispatchEvent(clickEv);

    expect(clickEv.defaultPrevented).toBe(true);
    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);

    off();
  });

  it('does not close when dragging out from inside the sheet to the backdrop', () => {
    const el = new EventTarget();
    const sheet = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, null);

    el.dispatchEvent(makeEvent('pointerdown', { target: sheet }));
    el.dispatchEvent(makeEvent('click', { target: el, detail: 1 }));
    expect(close).not.toHaveBeenCalled();

    off();
  });

  it('disarms on pointercancel so a subsequent pointer click does not close', () => {
    const el = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, null);

    el.dispatchEvent(makeEvent('pointerdown', { target: el }));
    el.dispatchEvent(makeEvent('pointercancel', { target: el }));
    el.dispatchEvent(makeEvent('click', { target: el, detail: 1 }));
    expect(close).not.toHaveBeenCalled();

    off();
  });

  it('closes on armory:dismiss custom event', () => {
    const el = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, null);

    el.dispatchEvent(new CustomEvent('armory:dismiss'));
    expect(close).toHaveBeenCalledTimes(1);

    off();
  });

  it('closes on Escape keydown and stops listening after cleanup', () => {
    const el = new EventTarget();
    const keys = new EventTarget();
    const close = vi.fn();
    const off = wireModalBackdrop(el, close, keys);

    const escEv = makeEvent('keydown', { key: 'Escape', cancelable: true });
    const stopImmSpy = vi.spyOn(escEv, 'stopImmediatePropagation');
    keys.dispatchEvent(escEv);

    expect(escEv.defaultPrevented).toBe(true);
    expect(stopImmSpy).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);

    off();
    keys.dispatchEvent(makeEvent('keydown', { key: 'Escape', cancelable: true }));
    expect(close).toHaveBeenCalledTimes(1);
  });
});
