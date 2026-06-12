// Decrypt tab (config-gated). Decrypt + preview an NMRS-encrypted CSV (.csv.nmrs)
// or backup (.sql.gz.enc), then optionally save the plaintext. Key precedence:
// pasted hex -> facility (derived from master_secret) -> configured backup_key.

import bridge from '../bridge.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function renderDecryptTab(root) {
  let selected = null;

  root.innerHTML = `
    <section class="tabview">
      <header class="tabview__head">
        <h2 class="tabview__title">Decrypt</h2>
        <p class="tabview__desc">Decrypt and preview an NMRS-encrypted CSV or backup, then
          optionally save the plaintext.</p>
      </header>

      <ol class="stepper">
        <li class="step">
          <div class="step__n">1</div>
          <div class="step__body">
            <div class="step__label">Encrypted file</div>
            <div class="actionrow">
              <button class="btn btn--secondary" id="dc-browse">Browse…</button>
              <span class="rs-file" id="dc-file">No file selected.</span>
            </div>
          </div>
        </li>
        <li class="step">
          <div class="step__n">2</div>
          <div class="step__body">
            <div class="step__label">Key</div>
            <div class="formgrid">
              <label class="field is-hidden" id="dc-facwrap">
                <span class="field__label">Facility (derives from master_secret)</span>
                <select class="field__input" id="dc-facility"></select>
              </label>
              <label class="field">
                <span class="field__label">Backup key (hex) — overrides facility/config</span>
                <input class="field__input" id="dc-key" type="password" autocomplete="off"
                       placeholder="64 hex chars (optional)" />
              </label>
            </div>
          </div>
        </li>
      </ol>

      <div class="actionrow">
        <button class="btn btn--primary" id="dc-preview" disabled>Preview</button>
        <button class="btn btn--secondary" id="dc-save" disabled>Save as plain…</button>
        <span class="actionrow__status" id="dc-status">Idle.</span>
      </div>

      <div class="panel" id="dc-resultwrap" style="overflow:auto; max-height:340px">
        <div id="dc-result"><span class="activitycard__empty">No preview yet.</span></div>
      </div>`;

  const el = (id) => root.querySelector(id);

  function setActions(on) { el('#dc-preview').disabled = !on; el('#dc-save').disabled = !on; }

  el('#dc-browse').addEventListener('click', async () => {
    const res = await bridge.decrypt.pick_file();
    if (!res || !res.ok) return;
    selected = res.path;
    el('#dc-file').textContent = res.name;
    setActions(true);
  });

  function keyArgs() { return [selected, el('#dc-key').value, el('#dc-facility').value]; }

  el('#dc-preview').addEventListener('click', async () => {
    if (!selected) return;
    el('#dc-status').textContent = 'Decrypting…';
    const res = await bridge.decrypt.preview(...keyArgs());
    if (!res.ok) { el('#dc-status').textContent = `Failed — ${res.message}`; el('#dc-status').className = 'actionrow__status is-err'; return; }
    el('#dc-status').className = 'actionrow__status is-ok';
    if (res.kind === 'sql') {
      el('#dc-status').textContent = `Decrypted SQL dump (${res.size.toLocaleString()} bytes). Save to extract the full .sql.`;
      el('#dc-result').innerHTML = `<pre class="preview">${esc(res.head)}</pre>`;
    } else {
      el('#dc-status').textContent = `Decrypted OK. Showing ${res.shown} of ${res.total} row(s); ${res.headers.length} column(s).`;
      const head = `<tr>${res.headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>`;
      const body = res.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('');
      el('#dc-result').innerHTML = `<table class="dtable dtable--compact"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    }
  });

  el('#dc-save').addEventListener('click', async () => {
    if (!selected) return;
    const res = await bridge.decrypt.save(...keyArgs());
    if (res && res.ok) { if (window.__toast) window.__toast(`Wrote ${res.bytes.toLocaleString()} bytes → ${res.path}`); }
    else if (res && res.cancelled) { /* cancelled */ }
    else if (window.__toast) window.__toast(`Save failed: ${(res && res.message) || 'error'}`);
  });

  // Manager mode: populate the facility dropdown when a master_secret exists.
  (async () => {
    const res = await bridge.decrypt.list_facilities();
    if (res.ok && res.has_master && (res.facilities || []).length) {
      el('#dc-facwrap').classList.remove('is-hidden');
      el('#dc-facility').innerHTML =
        `<option>${esc(res.placeholder)}</option>` +
        res.facilities.map((f) => `<option>${esc(f)}</option>`).join('');
    }
  })();

  return { destroy() {} };
}
