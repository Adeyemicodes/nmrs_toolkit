// Reverse Unvoid tab (config-gated, admin-only). Lists reversible UNVOID
// operations; reversing re-voids EXACTLY the rows that operation unvoided,
// restoring their captured prior state. Rows changed since are skipped, never
// clobbered. Backend logic is the unchanged v1.2.0 reverse_one.

import bridge from '../bridge.js';
import { onLog, onOp } from '../events.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function renderReverseTab(root) {
  const subs = [];
  let selected = null;
  let ops = [];
  let currentOp = null;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Reverse Unvoid</h2>
        <div class="danger-banner">
          Re-voids only the exact rows a prior unvoid changed, restoring their original void
          state. Rows changed since are skipped.
        </div>
      </header>

      <div class="actionrow">
        <button class="btn btn--secondary" id="rv-refresh">Refresh</button>
        <button class="btn btn--danger" id="rv-run" disabled>Reverse selected</button>
        <span class="actionrow__status" id="rv-status"></span>
      </div>

      <div class="panel">
        <table class="dtable" id="rv-table">
          <thead><tr>
            <th>Op</th><th>Time</th><th>Identifier</th><th>Name</th><th>Rows</th><th>Anchor voided</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="panel">
        <div class="panel__title">Recent reverse activity</div>
        <div class="activitycard" id="rv-recent"></div>
      </div>`;

  const el = (id) => root.querySelector(id);

  function renderTable() {
    el('#rv-table').querySelector('tbody').innerHTML = ops.map((r) =>
      `<tr data-op="${r.op_id}" class="${selected === r.op_id ? 'tr--selected' : ''}">
        <td>${r.op_id}</td><td>${esc(r.op_time)}</td><td>${esc(r.identifier)}</td>
        <td>${esc(r.patient_name)}</td><td>${r.rows_affected}</td>
        <td>${esc(r.anchor_date_voided)}</td></tr>`).join('')
      || '<tr><td colspan="6" class="dtable__empty">No reversible operations.</td></tr>';
    el('#rv-run').disabled = selected === null;
  }
  el('#rv-table').querySelector('tbody').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-op]'); if (!tr) return;
    selected = Number(tr.dataset.op);
    renderTable();
  });

  async function refresh() {
    selected = null;
    const res = await bridge.reverse.list();
    if (!res.ok) { el('#rv-status').textContent = res.message; el('#rv-status').className = 'actionrow__status is-err'; return; }
    ops = res.operations || [];
    el('#rv-status').textContent = `${ops.length} reversible operation(s).`;
    el('#rv-status').className = 'actionrow__status';
    renderTable();
  }
  el('#rv-refresh').addEventListener('click', refresh);

  const recent = [];
  function pushRecent(e) {
    recent.unshift(e); if (recent.length > 6) recent.pop();
    el('#rv-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}</div></div>`).join('')
      || '<div class="activitycard__empty">No recent reverse activity.</div>';
  }
  subs.push(onLog((e) => { if (e.category === 'UNVOID') pushRecent(e); }));

  subs.push(onOp((ev) => {
    if (ev.op !== 'reverse' || ev.operation_id !== currentOp) return;
    currentOp = null;
    el('#rv-status').textContent = ev.ok ? `Done — ${ev.message}` : `Failed — ${ev.message}`;
    el('#rv-status').className = 'actionrow__status ' + (ev.ok ? 'is-ok' : 'is-err');
    if (window.__toast) window.__toast(ev.message);
    refresh();
  }));

  el('#rv-run').addEventListener('click', async () => {
    if (selected === null) return;
    const op = ops.find((o) => o.op_id === selected);
    const proceed = await confirmModal(op);
    if (!proceed) return;
    el('#rv-run').disabled = true;
    el('#rv-status').textContent = 'Reversing…';
    el('#rv-status').className = 'actionrow__status';
    const res = await bridge.reverse.run(selected);
    if (!res.ok) { el('#rv-status').textContent = `Failed — ${res.message}`; el('#rv-status').className = 'actionrow__status is-err'; return; }
    currentOp = res.operation_id;
  });

  function confirmModal(op) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true">
          <div class="modal__title modal__title--danger">Confirm reverse</div>
          <div class="modal__body">
            <p>Reverse unvoid operation <code>${op ? op.op_id : ''}</code>
               (${esc(op ? op.identifier : '')} · ${esc(op ? op.patient_name : '')},
               ${op ? op.rows_affected : 0} row(s))?</p>
            <p>This re-voids only the rows that operation unvoided, restoring their original
               void state. Rows changed since are skipped.</p></div>
          <div class="modal__actions">
            <button class="btn btn--secondary" id="m-no">Cancel</button>
            <button class="btn btn--danger" id="m-yes">Reverse</button></div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (v) => { overlay.remove(); resolve(v); };
      overlay.querySelector('#m-no').addEventListener('click', () => close(false));
      overlay.querySelector('#m-yes').addEventListener('click', () => close(true));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
      overlay.querySelector('#m-no').focus();
    });
  }

  refresh();
  return { destroy() { subs.forEach((u) => u()); } };
}
