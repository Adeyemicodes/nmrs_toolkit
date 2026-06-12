// LogViewer — the reusable monospace log surface (MIGRATION_PLAN.md component
// library). Used by both the unified Activity Log drawer (all categories) and a
// tab's "Show full <workflow>.log" expander (locked to one category). It renders
// a toolbar (filters + search + actions) and a scrollable byte-identical log
// body, subscribes to the live event bus, and seeds from the persisted ring.

import bridge from './bridge.js';
import { onLog } from './events.js';

const CATEGORIES = ['APP', 'BACKUP', 'RESTORE', 'LINELIST', 'MERGE', 'UNVOID', 'SCHED', 'UI'];
const LEVELS = ['INFO', 'WARN', 'ERROR', 'DEBUG'];
const MAX_VIEW = 5000;

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function rangeSince(range) {
  const now = new Date();
  if (range === 'hour') return new Date(now.getTime() - 3600e3);
  if (range === 'today') return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (range === '7d') return new Date(now.getTime() - 7 * 86400e3);
  return null;
}
function localIso(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// createLogViewer(root, opts) -> { destroy, reseed }
//   opts.fixedCategory : lock to one category (hides category chips)
//   opts.showDateRange : default true
export function createLogViewer(root, opts = {}) {
  const fixedCategory = opts.fixedCategory || null;
  const showCats = !fixedCategory;
  const showDateRange = opts.showDateRange !== false;

  const state = {
    entries: [], seen: new Set(),
    cats: new Set(), levels: new Set(),
    query: '', since: null, until: null,
    autoScroll: true, matchEls: [], matchPos: -1,
  };

  root.innerHTML = `
    <div class="lv">
      <div class="al-toolbar">
        ${showCats ? `<div class="al-chips" data-role="cats">
          ${CATEGORIES.map((c) => `<button class="chip" data-cat="${c}">${c}</button>`).join('')}
        </div>` : ''}
        <div class="al-chips" data-role="levels">
          ${LEVELS.map((l) =>
            `<button class="chip chip--lvl chip--${l.toLowerCase()}" data-lvl="${l}">${l}</button>`).join('')}
        </div>
        <div class="al-controls">
          ${showDateRange ? `<select class="al-range" data-role="range" title="Date range">
            <option value="all">All time</option>
            <option value="hour">Last hour</option>
            <option value="today">Today</option>
            <option value="7d">Last 7 days</option>
            <option value="custom">Custom…</option>
          </select>
          <input class="al-custom is-hidden" data-role="from" type="datetime-local" title="From" />
          <input class="al-custom is-hidden" data-role="to" type="datetime-local" title="To" />` : ''}
          <input class="al-search" data-role="search" type="search"
                 placeholder="Search… (Enter, F3 next)" />
          <span class="al-count" data-role="count"></span>
        </div>
        <div class="al-actions">
          <label class="al-toggleline"><input type="checkbox" data-role="autoscroll" checked /> Auto-scroll</label>
          <button class="btn btn--secondary btn--sm" data-role="copy">Copy visible</button>
          <button class="btn btn--secondary btn--sm" data-role="export">Export…</button>
          <button class="btn btn--secondary btn--sm" data-role="clear"
                  title="Logs on disk are untouched">Clear view (logs on disk untouched)</button>
        </div>
      </div>
      <div class="al-log" data-role="log" tabindex="0"></div>
    </div>`;

  const r = (sel) => root.querySelector(`[data-role="${sel}"]`);
  const logEl = r('log');
  const countEl = r('count');

  function passes(e) {
    if (fixedCategory && e.category !== fixedCategory) return false;
    if (state.cats.size && !state.cats.has(e.category)) return false;
    if (state.levels.size && !state.levels.has((e.level || '').toUpperCase())) return false;
    if (state.since && e.ts < state.since) return false;
    if (state.until && e.ts > state.until) return false;
    if (state.query && !e.line.toLowerCase().includes(state.query.toLowerCase())) return false;
    return true;
  }

  function rowHtml(e) {
    let body = esc(e.line);
    if (state.query) {
      body = body.replace(new RegExp(escRe(state.query), 'ig'), (m) => `<mark>${m}</mark>`);
    }
    return `<div class="al-row al-row--${e.level}" data-seq="${e.seq}">` +
           `<button class="al-rowcopy" title="Copy line" data-seq="${e.seq}">⧉</button>` +
           `<span class="al-line">${body}</span></div>`;
  }

  function refreshMatches() {
    state.matchEls = Array.from(logEl.querySelectorAll('mark'));
    state.matchPos = -1;
  }
  function gotoMatch(delta) {
    if (!state.matchEls.length) return;
    if (state.matchPos >= 0) state.matchEls[state.matchPos].classList.remove('mark--active');
    state.matchPos = (state.matchPos + delta + state.matchEls.length) % state.matchEls.length;
    const m = state.matchEls[state.matchPos];
    m.classList.add('mark--active');
    m.scrollIntoView({ block: 'center' });
  }

  function render() {
    const visible = state.entries.filter(passes);
    logEl.innerHTML = visible.map(rowHtml).join('');
    countEl.textContent = `${visible.length} / ${state.entries.length}`;
    refreshMatches();
    if (state.autoScroll) logEl.scrollTop = logEl.scrollHeight;
  }

  function addEntry(e) {
    if (e == null || state.seen.has(e.seq)) return;
    if (fixedCategory && e.category !== fixedCategory) { state.seen.add(e.seq); return; }
    state.seen.add(e.seq);
    state.entries.push(e);
    if (state.entries.length > MAX_VIEW) {
      state.entries.splice(0, state.entries.length - MAX_VIEW)
        .forEach((d) => state.seen.delete(d.seq));
    }
    if (passes(e)) {
      logEl.insertAdjacentHTML('beforeend', rowHtml(e));
      countEl.textContent = `${logEl.children.length} / ${state.entries.length}`;
      if (state.autoScroll) logEl.scrollTop = logEl.scrollHeight;
    }
  }

  // chips
  if (showCats) {
    r('cats').addEventListener('click', (ev) => {
      const b = ev.target.closest('.chip'); if (!b) return;
      const k = b.dataset.cat;
      if (state.cats.has(k)) { state.cats.delete(k); b.classList.remove('chip--on'); }
      else { state.cats.add(k); b.classList.add('chip--on'); }
      render();
    });
  }
  r('levels').addEventListener('click', (ev) => {
    const b = ev.target.closest('.chip'); if (!b) return;
    const k = b.dataset.lvl;
    if (state.levels.has(k)) { state.levels.delete(k); b.classList.remove('chip--on'); }
    else { state.levels.add(k); b.classList.add('chip--on'); }
    render();
  });

  // date range
  if (showDateRange) {
    r('range').addEventListener('change', (ev) => {
      const custom = ev.target.value === 'custom';
      r('from').classList.toggle('is-hidden', !custom);
      r('to').classList.toggle('is-hidden', !custom);
      if (!custom) {
        const since = rangeSince(ev.target.value);
        state.since = since ? localIso(since) : null;
        state.until = null;
      }
      render();
    });
    const applyCustom = () => {
      state.since = r('from').value ? r('from').value + ':00' : null;
      state.until = r('to').value ? r('to').value + ':59' : null;
      render();
    };
    r('from').addEventListener('change', applyCustom);
    r('to').addEventListener('change', applyCustom);
  }

  // search
  const searchEl = r('search');
  searchEl.addEventListener('input', () => { state.query = searchEl.value; render(); });
  searchEl.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); gotoMatch(1); }
  });
  logEl.addEventListener('keydown', (ev) => {
    if (ev.key === 'F3' || ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'g')) {
      ev.preventDefault(); gotoMatch(ev.shiftKey ? -1 : 1);
    }
  });

  // copy
  function copyText(text) {
    try { navigator.clipboard.writeText(text); }
    catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
  }
  logEl.addEventListener('click', (ev) => {
    const cp = ev.target.closest('.al-rowcopy'); if (!cp) return;
    const e = state.entries.find((x) => String(x.seq) === cp.dataset.seq);
    if (e) copyText(e.line);
  });
  r('copy').addEventListener('click', () => {
    copyText(state.entries.filter(passes).map((e) => e.line).join('\n'));
  });

  function currentFilters() {
    const cats = fixedCategory ? [fixedCategory]
      : (state.cats.size ? Array.from(state.cats) : null);
    return {
      categories: cats,
      levels: state.levels.size ? Array.from(state.levels).map((l) => l.toLowerCase()) : null,
      since: state.since, until: state.until,
    };
  }

  r('export').addEventListener('click', async () => {
    const res = await bridge.log.export(currentFilters(), state.query);
    if (res && res.ok && window.__toast) window.__toast(`Exported ${res.lines} line(s) → ${res.path}`);
    else if (res && res.cancelled) { /* cancelled */ }
    else if (window.__toast) window.__toast(`Export failed: ${(res && res.message) || 'unknown'}`);
  });
  r('clear').addEventListener('click', () => {
    state.entries = []; state.seen.clear(); render();
  });
  r('autoscroll').addEventListener('change', (ev) => {
    state.autoScroll = ev.target.checked;
    if (state.autoScroll) logEl.scrollTop = logEl.scrollHeight;
  });

  // live + seed
  const unsub = onLog(addEntry);
  (async () => {
    const seed = fixedCategory
      ? await bridge.log.search('', { categories: [fixedCategory] })
      : await bridge.log.tail(opts.seedCount || 2000);
    (seed || []).forEach(addEntry);
    render();
  })();

  return {
    destroy() { unsub(); },
    focusSearch() { searchEl.focus(); },
  };
}
