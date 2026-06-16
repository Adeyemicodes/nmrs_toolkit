// Merge Reports tab. Reorderable input-file list (Add files / drag-drop, with
// per-row Remove / Up / Down), sort column + descending, output filename +
// encrypt, MERGE. Backend merge logic is the unchanged v1.2.0 algorithm.

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

export function renderMergeTab(root) {
  const subs = [];
  let viewer = null;
  let files = []; // [{path, name}]
  let currentOp = null;
  let running = false;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Merge Reports</h2>
        <p class="tabview__desc">Combine multiple CSV / encrypted-CSV exports into one,
          unioning columns in first-seen order.</p>
      </header>

      <ol class="stepper">
        <li class="step">
          <div class="step__n">1</div>
          <div class="step__body">
            <div class="step__label">Input files</div>
            <div class="dropzone" id="mg-drop">
              <span>Drag CSV files here, or</span>
              <button class="btn btn--secondary btn--sm" id="mg-add">Add files</button>
            </div>
            <ul class="filelist" id="mg-list"></ul>
          </div>
        </li>
        <li class="step">
          <div class="step__n">2</div>
          <div class="step__body">
            <div class="step__label">Sort &amp; output</div>
            <div class="formgrid">
              <label class="field">
                <span class="field__label">Sort column (blank = preserve order)</span>
                <input class="field__input" id="mg-sortcol" type="text" autocomplete="off" />
              </label>
              <label class="al-toggleline"><input type="checkbox" id="mg-desc" /> Descending</label>
              <label class="field">
                <span class="field__label">Output filename</span>
                <input class="field__input" id="mg-outname" type="text" autocomplete="off" />
              </label>
              <label class="al-toggleline"><input type="checkbox" id="mg-encrypt" /> Encrypt output (.csv.nmrs)</label>
            </div>
          </div>
        </li>
      </ol>

      <div class="actionrow">
        <button class="btn btn--primary" id="mg-run">MERGE</button>
        <span class="actionrow__status" id="mg-status">Idle.</span>
      </div>
      <div class="progress"><div class="progress__bar progress__bar--indeterminate is-hidden" id="mg-bar"></div></div>

      <div class="panel">
        <div class="panel__title">Recent merges</div>
        <div class="activitycard" id="mg-recent"></div>
      </div>

      <details class="expander" id="mg-fulllog">
        <summary>Show full merge log</summary>
        <div class="expander__body" id="mg-logviewer"></div>
      </details>
    </section>`;

  const el = (id) => root.querySelector(id);

  // -- file list -----------------------------------------------------------
  function renderList() {
    el('#mg-list').innerHTML = files.map((f, i) => `
      <li class="filelist__row">
        <span class="filelist__name" title="${esc(f.path)}">${esc(f.name)}</span>
        <span class="filelist__ops">
          <button class="iconbtn" data-op="up" data-i="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="iconbtn" data-op="down" data-i="${i}" ${i === files.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="iconbtn iconbtn--danger" data-op="remove" data-i="${i}">✕</button>
        </span>
      </li>`).join('') || '<li class="filelist__empty">No files added.</li>';
  }
  el('#mg-list').addEventListener('click', (ev) => {
    const b = ev.target.closest('.iconbtn'); if (!b) return;
    const i = Number(b.dataset.i);
    if (b.dataset.op === 'remove') files.splice(i, 1);
    else if (b.dataset.op === 'up' && i > 0) { [files[i - 1], files[i]] = [files[i], files[i - 1]]; }
    else if (b.dataset.op === 'down' && i < files.length - 1) { [files[i + 1], files[i]] = [files[i], files[i + 1]]; }
    renderList();
  });

  function addAccepted(res) {
    if (!res || !res.ok) {
      if (res && res.cancelled) return;
      if (window.__toast) window.__toast(`Could not add files: ${(res && res.message) || 'error'}`);
      return;
    }
    const have = new Set(files.map((f) => f.path));
    (res.accepted || []).forEach((f) => { if (!have.has(f.path)) files.push(f); });
    if ((res.rejected || []).length && window.__toast) {
      window.__toast(`${res.rejected.length} file(s) skipped (not found).`);
    }
    renderList();
  }

  el('#mg-add').addEventListener('click', async () => addAccepted(await bridge.merge.pick_files()));

  // Drag-and-drop (paths only available on webviews that expose File.path).
  const drop = el('#mg-drop');
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('dropzone--over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dropzone--over'));
  drop.addEventListener('drop', async (e) => {
    e.preventDefault();
    drop.classList.remove('dropzone--over');
    const paths = Array.from(e.dataTransfer.files || []).map((f) => f.path).filter(Boolean);
    if (!paths.length) {
      if (window.__toast) window.__toast('Drag-and-drop paths unavailable here — use “Add files”.');
      return;
    }
    addAccepted(await bridge.merge.add_files(paths));
  });

  // -- smart output name ---------------------------------------------------
  function refreshDefaultName() {
    const ext = el('#mg-encrypt').checked ? '.csv.nmrs' : '.csv';
    el('#mg-outname').value = `merged_${today()}${ext}`;
  }
  el('#mg-encrypt').addEventListener('change', refreshDefaultName);

  // -- recent merges card --------------------------------------------------
  const recent = [];
  function pushRecent(e) {
    recent.unshift(e);
    if (recent.length > 6) recent.pop();
    el('#mg-recent').innerHTML = recent.map((ev) =>
      `<div class="activitycard__row activitycard__row--${ev.level}">
        <div class="activitycard__msg">${esc(ev.message)}</div>
        <div class="activitycard__meta">${esc(ev.ts)}</div>
      </div>`).join('') || '<div class="activitycard__empty">No recent merges.</div>';
  }
  subs.push(onLog((e) => { if (e.category === 'MERGE') pushRecent(e); }));
  (async () => {
    const seed = await bridge.log.search('', { categories: ['MERGE'] });
    (seed || []).slice(-6).forEach(pushRecent);
  })();

  // -- run -----------------------------------------------------------------
  function setRunning(on) {
    running = on;
    el('#mg-run').disabled = on;
    el('#mg-bar').classList.toggle('is-hidden', !on);
  }
  function setStatus(text, cls) {
    el('#mg-status').textContent = text;
    el('#mg-status').className = 'actionrow__status' + (cls ? ' ' + cls : '');
  }
  subs.push(onOp((ev) => {
    if (ev.op !== 'merge' || ev.operation_id !== currentOp) return;
    setRunning(false); currentOp = null;
    if (ev.event === 'done') { setStatus(`Done — ${ev.message}`, 'is-ok'); if (window.__toast) window.__toast(ev.message); }
    else { setStatus(`Failed — ${ev.message}`, 'is-err'); if (window.__toast) window.__toast(`Merge failed: ${ev.message}`); }
  }));

  el('#mg-run').addEventListener('click', async () => {
    if (running) return;
    if (!files.length) { if (window.__toast) window.__toast('Add at least one CSV.'); return; }
    const picked = await bridge.merge.pick_output(el('#mg-outname').value || 'merged.csv');
    if (!picked || !picked.ok) return; // cancelled
    setRunning(true); setStatus('Merging…', '');
    const res = await bridge.merge.run(
      files.map((f) => f.path), el('#mg-sortcol').value, el('#mg-desc').checked,
      picked.path, el('#mg-encrypt').checked);
    if (!res.ok) { setRunning(false); setStatus(`Failed — ${res.message}`, 'is-err'); return; }
    currentOp = res.operation_id;
  });

  // -- full merge log expander --------------------------------------------
  el('#mg-fulllog').addEventListener('toggle', (ev) => {
    if (ev.target.open && !viewer) {
      viewer = createLogViewer(el('#mg-logviewer'), { fixedCategory: 'MERGE' });
    }
  });

  renderList();
  refreshDefaultName();

  return {
    destroy() {
      subs.forEach((u) => u());
      if (viewer) viewer.destroy();
    },
  };
}
