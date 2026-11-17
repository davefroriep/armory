import { getSettings, settingsChanged } from '../store/settings';
import { isNative } from '../native/service';
import { WALLBOARD_HTML } from '../wallboard/page';
import { wallboard } from '../wallboard/sync';
import { revFooter, topbar } from './chrome';
import { $, esc } from './dom';
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

    <h2 class="sec">LIVE PREVIEW</h2>
    <section class="card" style="padding:8px">
      <iframe id="wbFrame" title="Armory Fit Wallboard Preview" style="width:100%;height:320px;border:1px solid #1e2b42;border-radius:12px;background:#070b12"></iframe>
    </section>
    ${revFooter()}
  </main>`;

  const frame = $<HTMLIFrameElement>('#wbFrame', root);
  frame.srcdoc = WALLBOARD_HTML;

  const pushToFrame = (): void => {
    if (dead) return;
    try {
      frame.contentWindow?.postMessage({ type: 'armory:wallboard-state', state: wallboard.latestState }, '*');
    } catch { /* ignore */ }
  };

  frame.addEventListener('load', pushToFrame);

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
    offWb();
    offCfg();
  };
}
