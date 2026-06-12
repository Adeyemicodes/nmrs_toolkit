// Linelists tab. Segmented Bundled-script / Custom-file source, a smart output
// filename that auto-updates ({stem}_{YYYY-MM-DD}{ext}; mirrors the legacy
// _linelist_refresh_default_name), single run + weekly batch, recent runs, and a
// "Show full linelist.log" LogViewer. Backend execution/CSV writers are the
// unchanged v1.2.0 functions.

import bridge from '../bridge.js';
import { onLog, onOp } from '../events.js';
import { createLogViewer } from '../logviewer.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function today() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function renderLinelistsTab(root) {
  const subs = [];
  let viewer = null;
  let bundled = [];
  let sourceType = 'bundled';
  let customPath = null;
  let customStem = null;
  let currentOp = null;
  let running = false;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Linelists</h2>
        <p class="tabview__desc">Run a bundled or custom SQL report and write the CSV to
          <code id="ll-dir"></code>.</p>
      </header>

      <ol class="stepper">
        <li class="step">
          <div class="step__n">1</div>
          <div class="step__body">
            <div class="step__label">Source</div>
            <div class="segmented" id="ll-seg">
              <button class="segmented__btn segmented__btn--on" data-src="bundled">Bundled script</button>
              <button class="segmented__btn" data-src="custom">Custom file</button>
            </div>
            <div class="ll-source" id="ll-bundled-wrap">
              <select class="field__input rs-input" id="ll-bundled"></select>
            </div>
            <div class="ll-source is-hidden" id="ll-custom-wrap">
              <div class="actionrow">
                <button class="btn btn--secondary" id="ll-browse">Browse…</button>
                <span class="rs-file" id="ll-custom-file">No file selected.</span>
              </div>
            </div>
          </div>
        </li>
        <li class="step">
          <div class="step__n">2</div>
          <div class="step__body">
            <div class="step__label">Output</div>
            <input class="field__input rs-input" id="ll-outname" type="text" autocomplete="off" />
            <label class="al-toggleline" style="margin-top:8px">
              <input type="checkbox" id="ll-encrypt" /> Encrypt output (.csv.nmrs)
            </label>
          </div>
        </li>
      </ol>

      <div class="actionrow">
        <button class="btn btn--primary" id="ll-run">Run linelist</button>
        <button class="btn btn--secondary" id="ll-batch">Generate weekly batch</button>
        <button class="btn btn--secondary" id="ll-folder">Open folder</button>
        <span class="actionrow__status" id="ll-status">Idle.</span>
      </div>
      <div class="progress"><div class="progress__bar progress__bar--indeterminate is-hidden" id="ll-bar"></div></div>

      <div class="panel">
        <div class="panel__title">Recent runs</div>
        <div class="activitycard" id="ll-recent"></div>
      </div>

      <details class="expander" id="ll-fulllog">
        <summary>Show full linelist.log</summary>
        <div class="expander__body" id="ll-logviewer"></div>
      </details>
    </section>`;

  const el = (id) => root.querySelector(id);

  // -- smart output filename ----------------------------------------------
  function activeStem() {
    if (sourceType === 'custom') return customStem || null;
    const v = el('#ll-bundled').value;
    return v || null;
  }
  function refreshDefaultName() {
    const stem = activeStem() || 'linelist';
    const ext = el('#ll-encrypt').checked ? '.csv.nmrs' : '.csv';
    el('#ll-outname').value = `${stem}_${today()}${ext}`;
  }

  // -- segmented source control -------------------------------------------
  el('#ll-seg').addEventListener('click', (ev) => {
    const b = ev.target.closest('.segmented__btn'); if (!b) return;
    sourceType = b.dataset.src;
    el('#ll-seg').querySelectorAll('.segmented__btn').forEach((x) =>
      x.classList.toggle('segmented__btn--on', x === b));
    el('#ll-bundled-wrap').classList.toggle('is-hidden', sourceType !== 'bundled');
    el('#ll-custom-wrap').classList.toggle('is-hidden', sourceType !== 'custom');
    refreshDefaultName();
  });
  el('#ll-bundled').addEventListener('change', refreshDefaultName);
  el('#ll-encrypt').addEventListener('change', refreshDefaultName);

  el('#ll-browse').addEventListener('click', async () => {
    const res = await bridge.linelists.pick_custom();
    if (!res || !res.ok) {
      if (res && !res.cancelled && res.message && window.__toast) window.__toast(res.message);
      return;
    }
    customPath = res.path;
    customStem = res.stem;
    el('#ll-custom-file').textContent = res.path;
    refreshDefaultName();
  });

  // -- recent runs card ----------------------------------------------------
  const recent = [];
  function pushRecent(e) {
    recent.unshift(e);
    if (recent.length > 6) recent.pop();
    el('#ll-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}</div>
      </div>`).join('') || '<div class="activitycard__empty">No recent linelist runs.</div>';
  }
  subs.push(onLog((e) => { if (e.category === 'LINELIST') pushRecent(e); }));
  (async () => {
    const seed = await bridge.log.search('', { categories: ['LINELIST'] });
    (seed || []).slice(-6).forEach(pushRecent);
  })();

  // -- run state -----------------------------------------------------------
  function setRunning(on) {
    running = on;
    el('#ll-run').disabled = on;
    el('#ll-batch').disabled = on;
    el('#ll-bar').classList.toggle('is-hidden', !on);
  }
  function setStatus(text, cls) {
    el('#ll-status').textContent = text;
    el('#ll-status').className = 'actionrow__status' + (cls ? ' ' + cls : '');
  }

  subs.push(onOp((ev) => {
    if (ev.op !== 'linelist' || ev.operation_id !== currentOp) return;
    setRunning(false);
    currentOp = null;
    if (ev.event === 'done') {
      const msg = ev.written !== undefined
        ? `Batch: ${ev.written} written, ${ev.skipped} skipped, ${ev.failed} failed`
        : ev.message;
      setStatus(`Done — ${msg}`, ev.ok ? 'is-ok' : 'is-warn');
      if (window.__toast) window.__toast(msg);
    } else {
      setStatus(`Failed — ${ev.message}`, 'is-err');
      if (window.__toast) window.__toast(`Linelist failed: ${ev.message}`);
    }
  }));

  // -- actions -------------------------------------------------------------
  el('#ll-run').addEventListener('click', async () => {
    if (running) return;
    const source = sourceType === 'custom'
      ? { type: 'custom', path: customPath }
      : { type: 'bundled', name: el('#ll-bundled').value };
    if (sourceType === 'custom' && !customPath) {
      if (window.__toast) window.__toast('Pick a custom .sql file first.');
      return;
    }
    setRunning(true);
    setStatus('Running…', '');
    const res = await bridge.linelists.run(source, el('#ll-outname').value, el('#ll-encrypt').checked);
    if (!res.ok) { setRunning(false); setStatus(`Failed — ${res.message}`, 'is-err'); return; }
    currentOp = res.operation_id;
  });

  el('#ll-batch').addEventListener('click', async () => {
    if (running) return;
    setRunning(true);
    setStatus('Generating weekly batch…', '');
    const res = await bridge.linelists.run_weekly_batch(el('#ll-encrypt').checked);
    if (!res.ok) { setRunning(false); setStatus(`Failed — ${res.message}`, 'is-err'); return; }
    currentOp = res.operation_id;
  });

  el('#ll-folder').addEventListener('click', () => bridge.linelists.open_folder());

  // -- full linelist.log expander -----------------------------------------
  el('#ll-fulllog').addEventListener('toggle', (ev) => {
    if (ev.target.open && !viewer) {
      viewer = createLogViewer(el('#ll-logviewer'), { fixedCategory: 'LINELIST' });
    }
  });

  // -- load bundled list ---------------------------------------------------
  (async () => {
    const res = await bridge.linelists.list_bundled();
    bundled = res.scripts || [];
    el('#ll-dir').textContent = res.linelist_dir || '';
    el('#ll-batch').textContent = `Generate weekly batch (${res.batch_count || 0})`;
    el('#ll-bundled').innerHTML = bundled.map((s) =>
      `<option value="${esc(s.name)}">${esc(s.name)} — ${esc(s.filename)}</option>`).join('')
      || '<option value="">(no bundled scripts)</option>';
    refreshDefaultName();
  })();

  return {
    destroy() {
      subs.forEach((u) => u());
      if (viewer) viewer.destroy();
    },
  };
}
