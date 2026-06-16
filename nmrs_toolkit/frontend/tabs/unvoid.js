// Unvoid Patient tab. Validate ART identifier(s), review the ready/skipped
// preview, then commit. High-stakes + audited + reversible: the commit is gated
// behind a confirmation modal, and the validated batch lives server-side (the
// frontend never round-trips patient state). Backend logic is the unchanged
// v1.2.0 unvoid workflow.

import bridge from '../bridge.js';
import { onLog, onOp } from '../events.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function renderUnvoidTab(root) {
  const subs = [];
  let ready = [];
  let currentOp = null;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Unvoid Patient</h2>
        <div class="danger-banner">
          Unvoids records within the time window of the <strong>most recent void only</strong>,
          for patients whose void reason is in the accepted set. Every change is audited and
          can be reversed.
        </div>
      </header>

      <ol class="stepper">
        <li class="step">
          <div class="step__n">1</div>
          <div class="step__body">
            <div class="step__label">Enter ART identifier(s) — single, or comma/space separated</div>
            <div class="actionrow">
              <input class="field__input rs-input" id="uv-ids" type="text" autocomplete="off"
                     placeholder="IMO01104166, IMO01104167" />
              <button class="btn btn--secondary" id="uv-validate">Validate</button>
            </div>
          </div>
        </li>
        <li class="step">
          <div class="step__n">2</div>
          <div class="step__body">
            <div class="step__label">Verify (one block per identifier)</div>
            <pre class="preview" id="uv-preview">Validate one or more identifiers to begin.</pre>
          </div>
        </li>
        <li class="step step--gate">
          <div class="step__n">3</div>
          <div class="step__body">
            <div class="step__label">Unvoid</div>
            <div class="actionrow">
              <button class="btn btn--danger" id="uv-run" disabled>UNVOID PATIENT RECORDS</button>
              <span class="actionrow__status" id="uv-status">Idle.</span>
            </div>
          </div>
        </li>
      </ol>

      <div class="panel">
        <div class="panel__title">Recent unvoid activity</div>
        <div class="activitycard" id="uv-recent"></div>
      </div>`;

  const el = (id) => root.querySelector(id);

  function setRunEnabled(on, count) {
    el('#uv-run').disabled = !on;
    el('#uv-run').textContent = on && count
      ? `UNVOID ${count} PATIENT${count !== 1 ? 'S' : ''}` : 'UNVOID PATIENT RECORDS';
  }

  async function validate() {
    setRunEnabled(false);
    el('#uv-status').textContent = 'Validating…';
    const res = await bridge.unvoid.validate(el('#uv-ids').value);
    if (!res.ok) { el('#uv-preview').textContent = res.message; el('#uv-status').textContent = 'Idle.'; return; }
    ready = res.ready || [];
    const blocks = [];
    blocks.push(`${ready.length} ready, ${(res.skipped || []).length} skipped of ${res.total} identifier(s).`);
    blocks.push('-'.repeat(60));
    ready.forEach((p) => blocks.push(
      `[READY] ${p.identifier}  (patient_id=${p.patient_id})\n` +
      `        Name:    ${p.patient_name}\n` +
      `        Reason:  ${p.accepted_reason}\n` +
      `        Voided:  ${p.date_voided}\n` +
      `        Window:  ${p.window_start} .. ${p.window_end} (±${p.window_seconds}s)`));
    (res.skipped || []).forEach((s) => blocks.push(`[SKIP]  ${s.identifier}  —  ${s.reason}`));
    el('#uv-preview').textContent = blocks.join('\n\n');
    el('#uv-status').textContent = 'Idle.';
    setRunEnabled(ready.length > 0, ready.length);
  }
  el('#uv-validate').addEventListener('click', validate);
  el('#uv-ids').addEventListener('keydown', (e) => { if (e.key === 'Enter') validate(); });

  // recent card
  const recent = [];
  function pushRecent(e) {
    recent.unshift(e); if (recent.length > 6) recent.pop();
    el('#uv-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}</div></div>`).join('')
      || '<div class="activitycard__empty">No recent unvoid activity.</div>';
  }
  subs.push(onLog((e) => { if (e.category === 'UNVOID') pushRecent(e); }));
  (async () => { (await bridge.log.search('', { categories: ['UNVOID'] }) || []).slice(-6).forEach(pushRecent); })();

  subs.push(onOp((ev) => {
    if (ev.op !== 'unvoid' || ev.operation_id !== currentOp) return;
    currentOp = null;
    el('#uv-status').textContent = (ev.ok ? 'Done — ' : 'Completed with errors — ') + ev.message;
    el('#uv-status').className = 'actionrow__status ' + (ev.ok ? 'is-ok' : 'is-warn');
    if (window.__toast) window.__toast(ev.message);
    ready = []; el('#uv-ids').value = '';
  }));

  el('#uv-run').addEventListener('click', async () => {
    if (!ready.length) return;
    const proceed = await confirmModal(ready);
    if (!proceed) return;
    setRunEnabled(false);
    el('#uv-status').textContent = 'Unvoiding…';
    const res = await bridge.unvoid.commit();
    if (!res.ok) { el('#uv-status').textContent = `Failed — ${res.message}`; el('#uv-status').className = 'actionrow__status is-err'; return; }
    currentOp = res.operation_id;
  });

  function confirmModal(batch) {
    return new Promise((resolve) => {
      const list = batch.slice(0, 15).map((p) => `  • ${esc(p.identifier)}  (${esc(p.patient_name)})`).join('\n')
        + (batch.length > 15 ? `\n  … and ${batch.length - 15} more` : '');
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true">
          <div class="modal__title modal__title--danger">Confirm unvoid</div>
          <div class="modal__body"><p>Unvoid ${batch.length} patient(s)?</p>
            <pre class="preview preview--sm">${list}</pre>
            <p>These operations are audited and can be reversed by an administrator.</p></div>
          <div class="modal__actions">
            <button class="btn btn--secondary" id="m-no">Cancel</button>
            <button class="btn btn--danger" id="m-yes">Unvoid</button></div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (v) => { overlay.remove(); resolve(v); };
      overlay.querySelector('#m-no').addEventListener('click', () => close(false));
      overlay.querySelector('#m-yes').addEventListener('click', () => close(true));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
      overlay.querySelector('#m-no').focus();
    });
  }

  return { destroy() { subs.forEach((u) => u()); } };
}
