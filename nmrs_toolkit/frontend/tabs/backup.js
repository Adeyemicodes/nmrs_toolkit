// Backup tab. Stat tiles + per-facility status table + recent-activity card +
// a "Show full backup.log" expander (a LogViewer locked to category=BACKUP).
// All actions wrap the legacy backend (perform_backup / install_schedules) via
// the bridge — no behavior change.

import bridge from '../bridge.js';
import { onLog, onOp } from '../events.js';
import { createLogViewer } from '../logviewer.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function relTime(epoch) {
  if (!epoch) return 'never';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const STATUS = {
  encrypted: { label: 'Encrypted', cls: 'chip--success' },
  stale24: { label: 'Stale 24h', cls: 'chip--warning' },
  stale48: { label: 'Stale 48h+', cls: 'chip--danger' },
  never: { label: 'Never', cls: 'chip--neutral' },
};

export function renderBackupTab(root) {
  const subs = [];
  let viewer = null;
  let sortKey = 'facility';
  let sortDir = 1;
  let data = { facilities: [] };

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Backup</h2>
        <p class="tabview__desc">Encrypted, per-facility mysqldump snapshots written to
          <code id="bk-dir"></code>. AES-GCM with the facility key.</p>
      </header>

      <div class="tiles" id="bk-tiles"></div>

      <div class="actionrow">
        <button class="btn btn--primary" id="bk-run">Back up now</button>
        <button class="btn btn--secondary" id="bk-sched">Update schedules</button>
        <button class="btn btn--secondary" id="bk-folder">Open folder</button>
        <span class="actionrow__status" id="bk-status"></span>
      </div>

      <div class="panel">
        <table class="dtable" id="bk-table">
          <thead><tr>
            <th data-key="facility">Facility</th>
            <th data-key="last_run_epoch">Last run</th>
            <th data-key="size_bytes">Size</th>
            <th data-key="status">Status</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="panel">
        <div class="panel__title">Recent backup activity</div>
        <div class="activitycard" id="bk-recent"></div>
      </div>

      <details class="expander" id="bk-fulllog">
        <summary>Show full backup.log</summary>
        <div class="expander__body" id="bk-logviewer"></div>
      </details>
    </section>`;

  const el = (id) => root.querySelector(id);

  // -- stat tiles ----------------------------------------------------------
  function renderTiles() {
    const d = data;
    el('#bk-tiles').innerHTML = `
      ${tile('Facilities backed up', `${d.fresh ?? 0} / ${d.total ?? 0}`, 'fresh / total')}
      ${tile('Last successful backup', relTime(d.last_run_epoch),
             d.last_run_iso ? esc(d.last_run_iso) : 'no backups yet')}
      ${tile('Encryption', `<span class="badge badge--accent">${esc(d.encryption || 'AES-GCM')}</span>`, 'envelope')}
      ${tile('Total on disk', esc(d.total_human || '0 B'), `${d.total ?? 0} facility set(s)`)}`;
  }
  function tile(label, value, sub) {
    return `<div class="tile">
      <div class="tile__label">${esc(label)}</div>
      <div class="tile__value">${value}</div>
      <div class="tile__sub">${esc(sub)}</div>
    </div>`;
  }

  // -- table ---------------------------------------------------------------
  function renderTable() {
    const rows = [...data.facilities].sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av > bv ? 1 : av < bv ? -1 : 0) * sortDir;
    });
    el('#bk-table').querySelector('tbody').innerHTML = rows.map((r) => {
      const s = STATUS[r.status] || STATUS.never;
      return `<tr>
        <td>${esc(r.facility)}</td>
        <td title="${esc(r.last_run_iso || '')}">${r.last_run_epoch ? esc(relTime(r.last_run_epoch)) : '—'}</td>
        <td>${esc(r.size_human)}</td>
        <td><span class="chip-status ${s.cls}">${s.label}</span></td>
      </tr>`;
    }).join('') || `<tr><td colspan="4" class="dtable__empty">No backups yet.</td></tr>`;
  }
  el('#bk-table').querySelector('thead').addEventListener('click', (ev) => {
    const th = ev.target.closest('th'); if (!th) return;
    const key = th.dataset.key;
    if (key === sortKey) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
    el('#bk-table').querySelectorAll('th').forEach((h) =>
      h.classList.toggle('th--sorted', h === th));
    renderTable();
  });

  // -- recent activity card ------------------------------------------------
  const recent = [];
  function pushRecent(e) {
    recent.unshift(e);
    if (recent.length > 6) recent.pop();
    el('#bk-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}${ev.facility ? ' · ' + esc(ev.facility) : ''}</div>
      </div>`).join('') || '<div class="activitycard__empty">No recent backup events.</div>';
  }

  // -- data load -----------------------------------------------------------
  async function reload() {
    data = await bridge.backup.list_facilities();
    el('#bk-dir').textContent = data.backup_dir || '';
    renderTiles();
    renderTable();
  }

  // -- actions -------------------------------------------------------------
  let running = false;
  el('#bk-run').addEventListener('click', async () => {
    if (running) return;
    running = true;
    el('#bk-run').disabled = true;
    el('#bk-run').innerHTML = '<span class="btn__spinner"></span> Backing up…';
    el('#bk-status').textContent = 'Running mysqldump → gzip → encrypt…';
    const res = await bridge.backup.run_now();
    if (!res.ok) { finishRun(false, res.message); }
    else { el('#bk-run').dataset.op = res.operation_id; }
  });

  function finishRun(ok, message) {
    running = false;
    el('#bk-run').disabled = false;
    el('#bk-run').textContent = 'Back up now';
    el('#bk-status').textContent = ok ? `Done — ${message}` : `Failed — ${message}`;
    el('#bk-status').className = 'actionrow__status ' + (ok ? 'is-ok' : 'is-err');
    if (window.__toast) window.__toast(ok ? `Backup complete: ${message}` : `Backup failed: ${message}`);
    reload();
  }

  subs.push(onOp((ev) => {
    if (ev.op === 'backup' && ev.operation_id === el('#bk-run').dataset.op) {
      finishRun(ev.ok, ev.message);
    }
  }));

  el('#bk-sched').addEventListener('click', async () => {
    el('#bk-sched').disabled = true;
    const res = await bridge.backup.update_schedules();
    el('#bk-sched').disabled = false;
    if (window.__toast) window.__toast(res.ok ? `Schedules: ${res.status || 'updated'}` : `Failed: ${res.message}`);
  });
  el('#bk-folder').addEventListener('click', () => bridge.backup.open_folder());

  // -- recent activity: live BACKUP events + seed --------------------------
  subs.push(onLog((e) => { if (e.category === 'BACKUP') pushRecent(e); }));
  (async () => {
    const seed = await bridge.log.search('', { categories: ['BACKUP'] });
    (seed || []).slice(-6).forEach(pushRecent);
  })();

  // -- full backup.log expander (lazy LogViewer) ---------------------------
  el('#bk-fulllog').addEventListener('toggle', (ev) => {
    if (ev.target.open && !viewer) {
      viewer = createLogViewer(el('#bk-logviewer'), { fixedCategory: 'BACKUP' });
    }
  });

  reload();

  return {
    destroy() {
      subs.forEach((u) => u());
      if (viewer) viewer.destroy();
    },
  };
}
