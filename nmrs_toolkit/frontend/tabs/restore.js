// Restore tab — the highest-risk workflow. The UI communicates that at every
// step: explicit danger copy, a 4-step stepper, a TYPED-NAME hard gate that
// keeps the RESTORE button disabled until the operator types the exact target
// database name, a final consequences modal, a determinate progress bar, and a
// working CANCEL (wired to the backend cancel event). All backend logic is the
// extracted legacy restore pipeline — no behavior change.

import bridge from '../bridge.js';
import { onLog, onOp } from '../events.js';
import { createLogViewer } from '../logviewer.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function renderRestoreTab(root) {
  const subs = [];
  let viewer = null;
  let selectedFile = null;
  let currentOp = null;
  let running = false;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Restore</h2>
        <div class="danger-banner">
          <strong>Restore is destructive.</strong> If the target database exists, it will be
          backed up, dropped, and recreated. The live data is replaced with the dump's contents.
        </div>
      </header>

      <ol class="stepper">
        <li class="step">
          <div class="step__n">1</div>
          <div class="step__body">
            <div class="step__label">Dump file</div>
            <div class="actionrow">
              <button class="btn btn--secondary" id="rs-browse">Browse…</button>
              <span class="rs-file" id="rs-file">No file selected.</span>
            </div>
          </div>
        </li>
        <li class="step">
          <div class="step__n">2</div>
          <div class="step__body">
            <div class="step__label">Target database</div>
            <input class="field__input rs-input" id="rs-target" type="text"
                   placeholder="database name" autocomplete="off" />
          </div>
        </li>
        <li class="step">
          <div class="step__n">3</div>
          <div class="step__body">
            <div class="step__label">Backup key (hex)</div>
            <input class="field__input rs-input" id="rs-key" type="password" autocomplete="off"
                   placeholder="64 hex chars — only needed for .sql.gz.enc files" />
            <div class="field__help">Leave blank for plain/unencrypted dumps, or to use the
              configured <code>[backup] backup_key</code>.</div>
          </div>
        </li>
        <li class="step step--gate">
          <div class="step__n">4</div>
          <div class="step__body">
            <div class="step__label">Type the target name to confirm</div>
            <input class="field__input rs-input" id="rs-confirm" type="text" autocomplete="off"
                   placeholder="type the exact database name" />
            <div class="field__help">This is a hard gate — RESTORE stays disabled until this
              matches the target database name exactly.</div>
          </div>
        </li>
      </ol>

      <div class="actionrow">
        <button class="btn btn--danger" id="rs-run" disabled>RESTORE</button>
        <button class="btn btn--secondary" id="rs-cancel" disabled>Cancel</button>
        <span class="actionrow__status" id="rs-status">Idle.</span>
      </div>
      <div class="progress"><div class="progress__bar" id="rs-bar" style="width:0%"></div></div>

      <div class="panel">
        <div class="panel__title">Recent restore activity</div>
        <div class="activitycard" id="rs-recent"></div>
      </div>

      <details class="expander" id="rs-fulllog">
        <summary>Show full restore.log</summary>
        <div class="expander__body" id="rs-logviewer"></div>
      </details>
    </section>`;

  const el = (id) => root.querySelector(id);

  // -- typed-name hard gate ------------------------------------------------
  function refreshGate() {
    const target = el('#rs-target').value.trim();
    const typed = el('#rs-confirm').value.trim();
    const ok = !running && !!selectedFile && !!target && typed === target;
    el('#rs-run').disabled = !ok;
  }
  el('#rs-target').addEventListener('input', refreshGate);
  el('#rs-confirm').addEventListener('input', refreshGate);

  // -- file picker + preview ----------------------------------------------
  el('#rs-browse').addEventListener('click', async () => {
    const res = await bridge.restore.pick_file();
    if (!res || !res.ok) return;
    selectedFile = res.path;
    const pv = await bridge.restore.preview(res.path);
    if (pv.ok) {
      const enc = pv.encrypted ? ' · <span class="tag tag--warn">encrypted</span>' : '';
      el('#rs-file').innerHTML =
        `${esc(pv.name)} <span class="rs-meta">(${esc(pv.format)}, ${esc(pv.size_human)})</span>${enc}`;
      if (!el('#rs-target').value.trim() && pv.default_target) {
        el('#rs-target').value = pv.default_target;
      }
    } else {
      el('#rs-file').textContent = `${res.path} — ${pv.message || 'unreadable'}`;
    }
    refreshGate();
  });

  // -- recent restore activity card ---------------------------------------
  const recent = [];
  function pushRecent(e) {
    recent.unshift(e);
    if (recent.length > 6) recent.pop();
    el('#rs-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}</div>
      </div>`).join('') || '<div class="activitycard__empty">No recent restore events.</div>';
  }
  subs.push(onLog((e) => { if (e.category === 'RESTORE') pushRecent(e); }));
  (async () => {
    const seed = await bridge.log.search('', { categories: ['RESTORE'] });
    (seed || []).slice(-6).forEach(pushRecent);
  })();

  // -- progress / status / completion via op events -----------------------
  function setBar(pct) { el('#rs-bar').style.width = `${Math.max(0, Math.min(100, pct))}%`; }
  function setStatus(text, cls) {
    el('#rs-status').textContent = text;
    el('#rs-status').className = 'actionrow__status' + (cls ? ' ' + cls : '');
  }
  subs.push(onOp((ev) => {
    if (ev.op !== 'restore' || ev.operation_id !== currentOp) return;
    if (ev.event === 'progress') setBar(ev.pct || 0);
    else if (ev.event === 'status') setStatus(ev.status || '');
    else if (ev.event === 'done') endRun(true, ev.message);
    else if (ev.event === 'cancelled') endRun(false, ev.message, 'cancelled');
    else if (ev.event === 'error') endRun(false, ev.message, 'error');
  }));

  function startRunUi() {
    running = true;
    el('#rs-run').disabled = true;
    el('#rs-cancel').disabled = false;
    setBar(0);
    setStatus('Starting…', '');
  }
  function endRun(ok, message, kind) {
    running = false;
    currentOp = null;
    el('#rs-cancel').disabled = true;
    if (ok) { setBar(100); setStatus(`Done — ${message}`, 'is-ok'); }
    else if (kind === 'cancelled') setStatus(`Cancelled — ${message}`, 'is-warn');
    else setStatus(`Failed — ${message}`, 'is-err');
    if (window.__toast) window.__toast(ok ? 'Restore complete' :
      kind === 'cancelled' ? 'Restore cancelled' : `Restore failed: ${message}`);
    refreshGate();
  }

  // -- RESTORE: final consequences modal, then run ------------------------
  el('#rs-run').addEventListener('click', async () => {
    const target = el('#rs-target').value.trim();
    const typed = el('#rs-confirm').value.trim();
    if (!selectedFile || typed !== target) return; // gate (defensive)
    const proceed = await confirmModal(target);
    if (!proceed) return;
    startRunUi();
    const res = await bridge.restore.run(selectedFile, target, el('#rs-key').value, typed);
    if (!res.ok) { endRun(false, res.message, 'error'); return; }
    currentOp = res.operation_id;
  });

  el('#rs-cancel').addEventListener('click', () => {
    if (currentOp) bridge.restore.cancel(currentOp);
    setStatus('Cancelling…', 'is-warn');
  });

  // -- full restore.log expander ------------------------------------------
  el('#rs-fulllog').addEventListener('toggle', (ev) => {
    if (ev.target.open && !viewer) {
      viewer = createLogViewer(el('#rs-logviewer'), { fixedCategory: 'RESTORE' });
    }
  });

  function confirmModal(target) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true">
          <div class="modal__title modal__title--danger">Destructive operation</div>
          <div class="modal__body">
            <p>A safety backup will be taken first. Then the database
               <code>${esc(target)}</code> will be <strong>dropped and recreated</strong>
               from the selected dump.</p>
            <p>Pre-restore backups are kept; but the current live data will be replaced.
               Continue?</p>
          </div>
          <div class="modal__actions">
            <button class="btn btn--secondary" id="modal-no">Cancel</button>
            <button class="btn btn--danger" id="modal-yes">Drop &amp; restore</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (val) => { overlay.remove(); resolve(val); };
      overlay.querySelector('#modal-no').addEventListener('click', () => close(false));
      overlay.querySelector('#modal-yes').addEventListener('click', () => close(true));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
      // Secondary (Cancel) is the default, focused-by-default action.
      overlay.querySelector('#modal-no').focus();
    });
  }

  refreshGate();

  return {
    destroy() {
      subs.forEach((u) => u());
      if (viewer) viewer.destroy();
    },
  };
}
