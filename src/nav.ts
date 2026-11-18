/**
 * Pure hierarchical parent-route resolver so Android hardware back / edge-swipe always walks up
 * toward the main menu (`#/`) rather than jumping randomly through past tab history.
 */
export function parentHash(path: string): string | null {
  const clean = path.replace(/^#\/?/, '').split('?')[0].replace(/\/+$/, '');
  if (!clean) return null;
  const parts = clean.split('/').filter(Boolean);
  const [section, sub, arg] = parts;
  if (section === 'program') return '#/';
  if (section === 'wallboard') return '#/settings';
  if (section === 'airbike' || section === 'spin') return sub === 'w' ? `#/${section}` : '#/';
  if (section === 'strength') {
    if (!sub || sub === 'hr' || sub === 'history' || sub === 'progress' || sub === 'exercises') return '#/';
    if (sub === 'session') return arg ? `#/strength/workout/${arg}` : '#/';
    if (sub === 'summary' || sub === 'workout') return '#/strength/history';
    if (sub === 'exercise') return '#/strength/exercises';
    return '#/strength';
  }
  return '#/';
}
