import { getSettings, settingsChanged } from '../store/settings';
import { isNative } from '../native/service';
import { TV_H, TV_W, WALLBOARD_HTML, computeTvPreviewFit } from '../wallboard/page';
import { wallboard } from '../wallboard/sync';
import { revFooter, topbar } from './chrome';
import { $, esc, syncModalInert, wireModalBackdrop } from './dom';
import { toast } from './lifts/kit';

export function wallboardPreviewScreen(root: HTMLElement): () => void {
  let dead = false;
  const st = getSettings();

  root.innerHTML = `${topbar({ back: '#/settings', title: 'TV Wallboard' })}
  <main class="page">
    <section class="card">
      <div class="line">
        <span>Smart TV Wallboard<small>Local Wi-Fi live dashboard (1 Hz HR, rest timer &amp; superset progression)</small></span>
        <button class="switch ${st.wallboardEnabled ? 'on' : ''}" id="wbTog" role="switch" aria-checked="${st.wallboardEnabled}"><i></i></button>
      </div>
      <div class="hint" id="wbUrlLine"></div>
      <div class="row" style="margin-top:10px">
        <button class="btn sm primary" type="button" id="wbCopy">Copy TV URL</button>
        <button class="btn sm" type="button" id="wbRefresh">Refresh IP</button>
      </div>
      <p class="hint" style="margin-top:10px">Open Amazon Silk, Chrome, or Edge on your gym TV/monitor connected to the same Wi-Fi and browse to the address above.</p>
    </section>

    <div style="display:flex;align-items:center;justify-content:space-between;margin:14px 2px 8px">
      <h2 class="sec" style="margin:0">LIVE PREVIEW · 16:9 TV</h2>
      <button class="btn sm" type="button" id="wbExpand">⤢ Fullscreen</button>
    </div>
    <section class="card" style="padding:8px">
      <div id="wbStage" style="position:relative;width:100%;aspect-ratio:16/9;overflow:hidden;border:1px solid #1e2b42;border-radius:10px;background:#070b12;cursor:pointer" role="button" tabindex="0" aria-label="Expand TV wallboard preview fullscreen">
        <iframe id="wbFrame" title="Armory Fit Wallboard Preview" style="position:absolute;top:0;left:0;width:${TV_W}px;height:${TV_H}px;border:0;transform-origin:0 0;pointer-events:none;background:#070b12"></iframe>
      </div>
      <p class="hint" style="margin:8px 2px 2px;text-align:center">Scaled 16:9 TV layout (1600×900) · tap preview for fullscreen</p>
    </section>
    ${revFooter()}
  </main>`;

  const stage = $('#wbStage', root);
  const frame = $<HTMLIFrameElement>('#wbFrame', root);
  frame.srcdoc = WALLBOARD_HTML;

  let fsFrame: HTMLIFrameElement | null = null;
  let fsClose: (() => void) | null = null;

  const postStateTo = (target: HTMLIFrameElement | null): void => {
    try {
      target?.contentWindow?.postMessage({ type: 'armory:wallboard-state', state: wallboard.latestState }, '*');
    } catch { /* ignore */ }
  };

  const pushToFrame = (): void => {
    if (dead) return;
    postStateTo(frame);
    postStateTo(fsFrame);
  };

  const fitInline = (): void => {
    if (dead) return;
    const w = stage.clientWidth;
    if (w <= 0) return;
    const fit = computeTvPreviewFit(w, 0, false);
    stage.style.height = `${fit.boxH}px`;
    frame.style.transform = `scale(${fit.scale.toFixed(5)})`;
  };

  const openFullscreen = (): void => {
    if (dead || fsClose) return;
    const overlay = document.createElement('div');
    overlay.className = 'modal lmodal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:#03060b;display:flex;align-items:center;justify-content:center;padding:0;overflow:hidden';
    overlay.innerHTML = `
      <div id="wbFsBox" style="position:relative">
        <div id="wbFsStage" style="position:absolute;top:50%;left:50%;width:${TV_W}px;height:${TV_H}px;border:1px solid #1e2b42;border-radius:14px;overflow:hidden;background:#070b12;transform-origin:center center">
          <iframe id="wbFsFrame" title="Armory Fit Wallboard Fullscreen Preview" style="width:${TV_W}px;height:${TV_H}px;border:0;pointer-events:none;display:block;background:#070b12"></iframe>
        </div>
      </div>
      <button class="btn sm" type="button" id="wbFsClose" style="position:fixed;top:calc(12px + env(safe-area-inset-top, 0px));right:12px;z-index:2">✕ Close</button>`;

    const fsBox = overlay.querySelector<HTMLElement>('#wbFsBox')!;
    const fsStage = overlay.querySelector<HTMLElement>('#wbFsStage')!;
    const elFrame = overlay.querySelector<HTMLIFrameElement>('#wbFsFrame')!;
    const closeBtn = overlay.querySelector<HTMLButtonElement>('#wbFsClose')!;
    fsFrame = elFrame;
    elFrame.srcdoc = WALLBOARD_HTML;

    const fitFs = (): void => {
      const fit = computeTvPreviewFit(window.innerWidth, window.innerHeight, true);
      fsBox.style.width = `${fit.boxW}px`;
      fsBox.style.height = `${fit.boxH}px`;
      fsStage.style.transform = fit.rotated
        ? `translate(-50%, -50%) rotate(90deg) scale(${fit.scale.toFixed(5)})`
        : `translate(-50%, -50%) scale(${fit.scale.toFixed(5)})`;
    };

    let closed = false;
    let offBackdrop = () => {};
    const onResizeFs = (): void => fitFs();
    const close = (): void => {
      if (closed) return;
      closed = true;
      offBackdrop();
      window.removeEventListener('resize', onResizeFs);
      elFrame.removeEventListener('load', pushToFrame);
      overlay.remove();
      fsFrame = null;
      fsClose = null;
      syncModalInert();
    };
    fsClose = close;

    offBackdrop = wireModalBackdrop(overlay, close);
    closeBtn.addEventListener('click', close);
    elFrame.addEventListener('load', pushToFrame);
    window.addEventListener('resize', onResizeFs);
    document.body.appendChild(overlay);
    syncModalInert();
    fitFs();
    requestAnimationFrame(fitFs);
  };

  frame.addEventListener('load', pushToFrame);
  window.addEventListener('resize', fitInline);
  const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(fitInline) : null;
  ro?.observe(stage);
  requestAnimationFrame(fitInline);

  stage.addEventListener('click', openFullscreen);
  stage.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      openFullscreen();
    }
  });
  $('#wbExpand', root).addEventListener('click', openFullscreen);

  const paint = (): void => {
    if (dead) return;
    const cfg = getSettings();
    const s = wallboard.status;
    const tog = $<HTMLElement>('#wbTog', root);
    tog.classList.toggle('on', cfg.wallboardEnabled);
    tog.setAttribute('aria-checked', String(cfg.wallboardEnabled));
    const urlLine = $('#wbUrlLine', root);
    if (!isNative()) {
      urlLine.innerHTML = 'Running in desktop preview · APK on Pixel hosts live at <b>http://&lt;pixel-wifi-ip&gt;:' + cfg.wallboardPort + '</b>';
    } else if (s.running) {
      urlLine.innerHTML = `<b>● LIVE:</b> <code>${esc(s.url)}</code> · ${s.clients} display${s.clients === 1 ? '' : 's'} connected`;
    } else {
      urlLine.textContent = s.error ? `Error: ${s.error}` : 'Wallboard server off';
    }
    pushToFrame();
  };

  $('#wbTog', root).addEventListener('click', () => {
    void wallboard.setEnabled(!getSettings().wallboardEnabled).then(paint);
  });
  $('#wbCopy', root).addEventListener('click', () => {
    const url = wallboard.status.url;
    void navigator.clipboard?.writeText(url).then(() => toast(`Copied ${url}`)).catch(() => toast(url));
  });
  $('#wbRefresh', root).addEventListener('click', () => {
    void wallboard.refreshStatus().then(paint);
  });

  const offWb = wallboard.changed.on(paint);
  const offCfg = settingsChanged.on(paint);
  void wallboard.refreshStatus();
  paint();

  return () => {
    dead = true;
    fsClose?.();
    ro?.disconnect();
    window.removeEventListener('resize', fitInline);
    frame.removeEventListener('load', pushToFrame);
    offWb();
    offCfg();
  };
}
