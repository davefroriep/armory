import {
  BACKUP_DIR_LABEL,
  backupChanged,
  lastBackupStatus,
  parseBackupJson,
  pickBackupText,
  restoreFromJson,
  runBackupNow,
  shareFullBackup,
} from '../store/backup';
import { isNative } from '../native/service';
import { esc } from './dom';
import { confirmSheet, toast } from './lifts/kit';

export function backupCardHtml(statsLine: string): string {
  const st = lastBackupStatus();
  let statusTxt = 'No auto-backup saved on this phone yet.';
  if (st?.error) {
    statusTxt = `Last backup error: ${st.error}`;
  } else if (st) {
    const when = new Date(st.atMs).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    statusTxt = `Last auto-backup: <b>${esc(when)}</b> (${st.lifts} lifts · ${st.cardio} cardio · ${st.routines} days)`;
  }
  const dirNote = isNative()
    ? `Auto-saves after workouts &amp; daily (settings + API key included) to <b>${BACKUP_DIR_LABEL}</b> (point DriveSync here → Google Drive <b>Backups/ArmoryFit Backup</b>).`
    : 'Exports complete Strength + Cardio + rolling schedule + settings &amp; API key.';
  return `<section class="card" data-backup-card>
    <div class="hint">${esc(statsLine)}</div>
    <div class="hint" data-bk-status>${statusTxt}</div>
    <div class="hint">${dirNote}</div>
    <div class="row">
      ${isNative() ? '<button class="btn sm primary" type="button" data-bk-now>Back up now</button>' : ''}
      <button class="btn sm" type="button" data-bk-restore>Restore backup</button>
      <button class="btn sm" type="button" data-bk-share>${isNative() ? 'Share file' : 'Export backup'}</button>
    </div>
  </section>`;
}

export function wireBackupCard(root: HTMLElement, onRestored?: () => void): () => void {
  const paintStatus = (): void => {
    const el = root.querySelector<HTMLElement>('[data-bk-status]');
    const st = lastBackupStatus();
    if (!el || !st) return;
    if (st.error) {
      el.textContent = `Last backup error: ${st.error}`;
    } else {
      const when = new Date(st.atMs).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      el.innerHTML = `Last auto-backup: <b>${esc(when)}</b> (${st.lifts} lifts · ${st.cardio} cardio · ${st.routines} days)`;
    }
  };
  const offBk = backupChanged.on(paintStatus);

  const onClick = async (e: Event): Promise<void> => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-bk-now]')) {
      const st = await runBackupNow();
      paintStatus();
      toast(st.error ? `Backup failed: ${st.error}` : `Saved to ${BACKUP_DIR_LABEL}`);
      return;
    }
    if (t.closest('[data-bk-share]')) {
      try {
        await shareFullBackup();
      } catch { /* cancelled */ }
      return;
    }
    if (t.closest('[data-bk-restore]')) {
      const picked = await pickBackupText();
      if (!picked) return;
      try {
        const parsed = parseBackupJson(picked.content);
        const cardioN = parsed.app === 'armory-fit-backup' ? (parsed.workoutsMeta?.filter((m) => m.equipment !== 'strength').length ?? 0) : 0;
        const ok = await confirmSheet(
          'Restore backup?',
          `${picked.fileName}: ${parsed.sessions.length} lifts${cardioN ? `, ${cardioN} cardio` : ''}, ${parsed.routines?.length ?? 0} days. Matching IDs are restored; settings and Intervals API key are restored when present in the backup (a pre-restore snapshot is saved first).`,
          'Restore',
        );
        if (!ok) return;
        const res = await restoreFromJson(picked.content);
        toast(`Restored ${res.lifts} lifts${res.cardio ? `, ${res.cardio} cardio` : ''}, ${res.routines} days`);
        onRestored?.();
      } catch (err) {
        toast(`Restore failed: ${esc((err as Error).message)}`);
      }
    }
  };
  root.addEventListener('click', onClick);
  return () => {
    offBk();
    root.removeEventListener('click', onClick);
  };
}
