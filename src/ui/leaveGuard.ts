import { syncModalInert, wireModalBackdrop } from './dom';

/**
 * Stops an accidental system Back (Android edge swipe / back button, browser back) from silently ending a
 * running workout. While armed there is one extra history entry above the workout page; Back consumes it,
 * we put it straight back and ask "Leave this workout?" instead. Yes goes back for real, No stays.
 */
export class LeaveGuard {
  private pushed = false;
  private modal: HTMLElement | null = null;
  private offBackdrop: (() => void) | null = null;

  constructor(private readonly isActive: () => boolean) {}

  /** Call when the workout starts. */
  arm(): void {
    if (this.pushed) return;
    history.pushState({ armoryGuard: true }, '', location.href);
    this.pushed = true;
    window.addEventListener('popstate', this.onPop);
  }

  /** Call when the workout ends normally: drops the extra history entry so Back works as usual again. */
  disarm(): void {
    window.removeEventListener('popstate', this.onPop);
    this.closeModal();
    if (this.pushed) {
      this.pushed = false;
      history.back();
    }
  }

  /** Call on screen teardown (no history changes: the route is already changing). */
  dispose(): void {
    window.removeEventListener('popstate', this.onPop);
    this.closeModal();
    this.pushed = false;
  }

  private onPop = (): void => {
    if (!this.isActive()) {
      // Nothing to protect any more; the guard entry is already gone, so let Back proceed normally.
      this.dispose();
      return;
    }
    // Back consumed the guard entry: restore it and ask.
    history.pushState({ armoryGuard: true }, '', location.href);
    this.showModal();
  };

  private showModal(): void {
    if (this.modal) return;
    const m = document.createElement('div');
    m.className = 'modal';
    m.innerHTML = `<div class="sheet" role="alertdialog" aria-label="Leave this workout?">
      <h2>Leave this workout?</h2><p>Tracking will stop.</p>
      <button class="btn primary big" data-leave="no">No, keep going</button>
      <button class="btn danger big" data-leave="yes">Yes, leave</button></div>`;
    m.addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-leave]');
      if (!b) return;
      if (b.dataset.leave === 'yes') this.leave();
      else this.closeModal();
    });
    this.offBackdrop = wireModalBackdrop(m, () => this.closeModal());
    document.body.appendChild(m);
    this.modal = m;
    syncModalInert();
  }

  private closeModal(): void {
    this.offBackdrop?.();
    this.offBackdrop = null;
    this.modal?.remove();
    this.modal = null;
    syncModalInert();
  }

  private leave(): void {
    window.removeEventListener('popstate', this.onPop);
    this.closeModal();
    if (this.pushed) {
      this.pushed = false;
      // Return directly to Home instead of walking back through intermediate tabs.
      location.hash = '#/';
    }
  }
}
