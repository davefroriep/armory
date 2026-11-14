export const $ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document): T => {
  const el = root.querySelector<T>(sel);
  if (!el) throw new Error(`missing ${sel}`);
  return el;
};

export const esc = (s: string): string =>
  s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);

export const mmss = (sec: number): string => {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

export const hms = (sec: number): string => {
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : mmss(s);
};

export const ICON: Record<string, string> = {
  strength: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="10" y="11" width="6" height="26" rx="2" fill="currentColor" fill-opacity=".16"/><rect x="32" y="11" width="6" height="26" rx="2" fill="currentColor" fill-opacity=".16"/><rect x="5" y="16" width="4" height="16" rx="1.5"/><rect x="39" y="16" width="4" height="16" rx="1.5"/><path d="M3 24h2M16 24h16M43 24h2"/></svg>',
  treadmill: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="34" width="38" height="6" rx="3" fill="currentColor" fill-opacity=".15"/><circle cx="9" cy="37" r="1.2" fill="currentColor" stroke="none"/><circle cx="38" cy="37" r="1.2" fill="currentColor" stroke="none"/><path d="M27 34l2.5-4.5h9L41 34" fill="currentColor" fill-opacity=".18"/><path d="M33.5 34L38 13"/><path d="M33 11.5l8.5-3.5 1.8 4.8-8.5 3.5z" fill="currentColor" fill-opacity=".18"/><path d="M36.5 19H21a2.5 2.5 0 0 0-2.5 2.5V23"/></svg>',
  spin: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="35" cy="30" r="8" fill="currentColor" fill-opacity=".15"/><circle cx="35" cy="30" r="2.2"/><circle cx="18" cy="32" r="2.8"/><path d="M7 40h34M11 40l7-8 12.5-12 4.5 10M18 32L13.5 16M10 16h7.5M15.5 23l15-3M30.5 20L29 13.5h6.5a2 2 0 0 1 2 2"/></svg>',
  airbike: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="34" cy="28" r="10" fill="currentColor" fill-opacity=".14"/><circle cx="34" cy="28" r="2.4"/><path d="M34 18v7M34 31v7M24 28h7M37 28h7M27 21l5 5M36 30l5 5M41 21l-5 5M32 30l-5 5"/><circle cx="18" cy="32" r="2.6"/><path d="M6 40h36M10.5 40L15 18M11 18h8M14 26l11 5M18 32l6.5-2.5M28 34L25 11l-4-2M35 38l1 2"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.6.85 1 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
  note: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 20L10 4h4l3.5 16z" fill="currentColor" fill-opacity=".14"/><path d="M12 16.5L17.5 7"/><circle cx="12" cy="16.5" r="1.4" fill="currentColor"/></svg>',
};
