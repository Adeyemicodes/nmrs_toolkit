// Analytics Dashboard tab (Phase C: cards + stat tiles; charts arrive in D).
// Loads the latest Treatment linelist, computes indicators in-memory for the
// chosen date range (no DB call), and renders every card with consistent
// disaggregation chips and snapshot-vs-period date-stamping. "Refresh from DB"
// regenerates the linelist at the chosen end_date (snapshots are then exact).

import bridge from '../bridge.js';
import { onOp } from '../events.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (n) => (n == null ? '—' : Number(n).toLocaleString());

function iso(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function currentQuarter() {
  const d = new Date();
  const q = Math.floor(d.getMonth() / 3);
  return [iso(new Date(d.getFullYear(), q * 3, 1)),
          iso(new Date(d.getFullYear(), q * 3 + 3, 0))];
}

// Disaggregation: which split a chip selects out of an indicator's payload.
const SPLITS = [
  ['all', 'All', null],
  ['sex', 'By sex', 'by_sex'],
  ['age', 'By age', 'by_age_band'],
  ['sexage', 'By sex × age', 'by_sex_age'],
];

export function renderDashboardTab(root) {
  const subs = [];
  const state = {
    status: null, summary: null, start: null, end: null,
    facilities: [], facilityFilter: [], running: false,
  };
  let currentOp = null;
  [state.start, state.end] = currentQuarter();

  root.innerHTML = `
    <section class="dash">
      <div class="dash__toolbar">
        <label class="dash__dates">From <input type="date" id="dash-start" value="${state.start}"></label>
        <label class="dash__dates">To <input type="date" id="dash-end" value="${state.end}"></label>
        <span class="dash__facility" id="dash-facility"></span>
        <span class="dash__source" id="dash-source">Loading…</span>
        <span class="dash__spacer"></span>
        <button class="btn btn--secondary btn--sm" id="dash-refresh">Refresh from DB</button>
        <button class="btn btn--secondary btn--sm" id="dash-export">Export current view</button>
        <button class="btn btn--secondary btn--sm" id="dash-exports-folder">Open exports</button>
      </div>
      <div class="dash__progress is-hidden" id="dash-progress">
        <div class="progress"><div class="progress__bar progress__bar--indeterminate"></div></div>
        <span id="dash-progress-text">Regenerating linelist from the database…</span>
      </div>
      <div class="dash__grid" id="dash-grid"></div>
    </section>`;

  const el = (id) => root.querySelector(id);
  const grid = el('#dash-grid');

  // -- date range (instant, in-memory recompute) ---------------------------
  el('#dash-start').addEventListener('change', (e) => { state.start = e.target.value; recompute(); });
  el('#dash-end').addEventListener('change', (e) => { state.end = e.target.value; recompute(); });

  // -- toolbar actions -----------------------------------------------------
  el('#dash-refresh').addEventListener('click', refreshFromDb);
  el('#dash-export').addEventListener('click', () => exportIndicator('cohort_flow'));
  el('#dash-exports-folder').addEventListener('click', () => bridge.dashboard.open_exports_folder());

  subs.push(onOp((ev) => {
    if (ev.op !== 'dashboard' || ev.operation_id !== currentOp) return;
    currentOp = null;
    el('#dash-progress').classList.add('is-hidden');
    if (ev.ok) {
      if (window.__toast) window.__toast(ev.message);
      // The worker already reloaded the cache at end_date; recompute + render.
      el('#dash-source').textContent = `Source: ${ev.source} · ${num(ev.rows)} rows`;
      recompute();
    } else {
      if (window.__toast) window.__toast(`Refresh failed: ${ev.message}`);
    }
  }));

  async function refreshFromDb() {
    if (state.running) return;
    state.running = true;
    el('#dash-progress').classList.remove('is-hidden');
    const res = await bridge.dashboard.refresh_from_db(state.end);
    state.running = false;
    if (!res.ok) {
      el('#dash-progress').classList.add('is-hidden');
      if (window.__toast) window.__toast(`Refresh failed: ${res.message}`);
      return;
    }
    currentOp = res.operation_id;
  }

  async function exportIndicator(slug) {
    const res = await bridge.dashboard.export(slug, state.start, state.end,
      state.facilityFilter.length ? state.facilityFilter : null);
    if (res && res.ok && window.__toast) window.__toast(`Exported → ${res.path}`);
    else if (window.__toast) window.__toast(`Export failed: ${(res && res.message) || 'error'}`);
  }

  // -- compute + render ----------------------------------------------------
  async function recompute() {
    const res = await bridge.dashboard.compute(state.start, state.end,
      state.facilityFilter.length ? state.facilityFilter : null);
    if (!res.ok) { grid.innerHTML = `<div class="dash__empty">${esc(res.message || 'No data.')}</div>`; return; }
    state.summary = res;
    renderCards();
  }

  function renderCards() {
    const s = state.summary;
    const end = state.end, start = state.start;
    const asOf = `as of ${esc(end)}`;
    const period = `Period: ${esc(start)} to ${esc(end)}`;
    grid.innerHTML = `
      <div class="dash__row dash__row--3">
        ${kpiCard('ever_enrolled', 'Ever Enrolled', s.ever_enrolled, asOf)}
        ${kpiCard('tx_new', 'TX_NEW', s.tx_new, period)}
        ${kpiCard('tx_curr', 'TX_CURR', s.tx_curr, asOf)}
      </div>
      <div class="dash__row">${cohortFlowCard(s.cohort_flow, start, end)}</div>
      <div class="dash__row dash__row--2">
        ${vlCascadeCard(s.vl_cascade, asOf)}
        ${iitCard(s.currently_iit, asOf)}
      </div>
      <div class="dash__row dash__row--2">
        ${txmlCard(s.tx_ml, period)}
        ${kpiCard('tx_rtt', 'TX_RTT', s.tx_rtt, period)}
      </div>
      <div class="dash__row dash__row--2">
        ${mmdCard(s.mmd_distribution, asOf)}
        ${pyramidCard(s.age_sex_pyramid, asOf)}
      </div>
      <div class="dash__row">${biometricCard(s.biometric_coverage, asOf)}</div>`;
    wireDisaggChips();
  }

  // -- card builders -------------------------------------------------------
  function chipRow(slug) {
    return `<div class="dash-chips" data-card="${slug}">` +
      SPLITS.map(([k, label], i) =>
        `<button class="chip ${i === 0 ? 'chip--on' : ''}" data-split="${k}">${label}</button>`).join('') +
      `</div>`;
  }
  function breakdownHtml(indicator, splitKey) {
    const field = (SPLITS.find((s) => s[0] === splitKey) || [])[2];
    if (!field || !indicator[field]) {
      return `<div class="dash-total">${num(indicator.total)}</div>`;
    }
    const entries = Object.entries(indicator[field]).sort((a, b) => b[1] - a[1]);
    return `<table class="dash-break">${entries.map(([g, c]) =>
      `<tr><td>${esc(g)}</td><td>${num(c)}</td></tr>`).join('')}</table>`;
  }
  function kpiCard(slug, title, indicator, stamp) {
    return `<div class="dash-card" data-indicator="${slug}">
      <div class="dash-card__title">${esc(title)}</div>
      <div class="dash-card__sub">${esc(stamp)}</div>
      <div class="dash-card__body" data-body="${slug}">${breakdownHtml(indicator, 'all')}</div>
      ${chipRow(slug)}
    </div>`;
  }
  function cohortFlowCard(cf, start, end) {
    const stages = [
      ['Ever Enrolled', cf.ever_enrolled, `as of ${end}`],
      ['TX_NEW', cf.tx_new, `${start}..${end}`],
      ['TX_CURR', cf.tx_curr, `as of ${end}`],
      ['Currently IIT', cf.currently_iit, `as of ${end}`],
      ['TX_RTT', cf.tx_rtt, `${start}..${end}`],
    ];
    return `<div class="dash-card">
      <div class="dash-card__title">Cohort Flow</div>
      <div class="dash-flow">${stages.map(([t, n, st]) =>
        `<div class="dash-flow__stage"><div class="dash-flow__n">${num(n)}</div>
          <div class="dash-flow__t">${esc(t)}</div><div class="dash-flow__st">${esc(st)}</div></div>`).join('<div class="dash-flow__arrow">→</div>')}</div>
      <div class="dash-card__sub">TX_ML this period: ${num(cf.tx_ml_total)} · Dead ${num(cf.dead)} · TO ${num(cf.transferred_out)} · Stopped ${num(cf.stopped)}</div>
    </div>`;
  }
  function vlCascadeCard(vl, stamp) {
    const step = (label, n, pct) =>
      `<div class="dash-funnel__step"><span>${esc(label)}</span><span>${num(n)}${pct != null ? ` (${pct}%)` : ''}</span></div>`;
    return `<div class="dash-card" data-indicator="vl_cascade">
      <div class="dash-card__title">VL Cascade</div>
      <div class="dash-card__sub">${esc(stamp)}</div>
      <div class="dash-funnel">
        ${step('Eligible (TX_CURR ≥6mo)', vl.eligible, null)}
        ${step('Sampled (≤12mo)', vl.sampled, vl.eligible ? Math.round(100 * vl.sampled / vl.eligible) : 0)}
        ${step('With result', vl.with_result, vl.sampled ? Math.round(100 * vl.with_result / vl.sampled) : 0)}
        ${step('Suppressed (<1000)', vl.suppressed, vl.suppression_pct)}
      </div>
      <button class="btn btn--secondary btn--sm" data-export="vl_cascade">Export</button>
    </div>`;
  }
  function iitCard(iit, stamp) {
    const dur = iit.by_duration || {};
    const sub = iit.by_subgroup || {};
    return `<div class="dash-card dash-card--amber" data-indicator="currently_iit">
      <div class="dash-card__title">Currently IIT</div>
      <div class="dash-card__sub">${esc(stamp)}</div>
      <div class="dash-total dash-total--amber">${num(iit.total)}</div>
      <div class="dash-card__sub">Duration: ${['<3 months', '3-<6 months', '>=6 months']
        .map((b) => `${b} ${num(dur[b] || 0)}`).join(' · ')}</div>
      <div class="dash-card__sub">${Object.entries(sub).map(([k, v]) => `${esc(k)}: ${num(v)}`).join(' · ')}</div>
      <button class="btn btn--secondary btn--sm" data-export="currently_iit">View patient list (export)</button>
    </div>`;
  }
  function txmlCard(ml, stamp) {
    const r = ml.by_reason || {};
    return `<div class="dash-card" data-indicator="tx_ml">
      <div class="dash-card__title">TX_ML</div>
      <div class="dash-card__sub">${esc(stamp)}</div>
      <div class="dash-total">${num(ml.total)}</div>
      <table class="dash-break">${['Newly IIT', 'Died', 'Transferred Out', 'Refused/Stopped']
        .map((k) => `<tr><td>${esc(k)}</td><td>${num(r[k] || 0)}</td></tr>`).join('')}</table>
      <button class="btn btn--secondary btn--sm" data-export="tx_ml">Export</button>
    </div>`;
  }
  function mmdCard(mmd, stamp) {
    const b = mmd.by_bucket || {};
    return `<div class="dash-card" data-indicator="mmd_distribution">
      <div class="dash-card__title">MMD Share</div>
      <div class="dash-card__sub">${esc(stamp)} · coverage (≥3mo) ${mmd.mmd_coverage_pct}%</div>
      <table class="dash-break">${['<3 months', '3-5 months', '>=6 months']
        .map((k) => `<tr><td>${esc(k)}</td><td>${num(b[k] || 0)}</td></tr>`).join('')}</table>
    </div>`;
  }
  function pyramidCard(p, stamp) {
    const g = p.grid || {};
    const bands = ['Pediatric (0-9)', 'Adolescent (10-19)', 'Adult (20+)'];
    return `<div class="dash-card" data-indicator="age_sex_pyramid">
      <div class="dash-card__title">Age / Sex</div>
      <div class="dash-card__sub">${esc(stamp)} (TX_CURR)</div>
      <table class="dash-break"><tr><th>Band</th><th>F</th><th>M</th></tr>
        ${bands.map((b) => `<tr><td>${esc(b)}</td><td>${num((g[b] || {}).F || 0)}</td><td>${num((g[b] || {}).M || 0)}</td></tr>`).join('')}</table>
    </div>`;
  }
  function biometricCard(b, stamp) {
    return `<div class="dash-card" data-indicator="biometric_coverage">
      <div class="dash-card__title">Biometric Capture</div>
      <div class="dash-card__sub">${esc(stamp)} (TX_CURR)</div>
      <div class="dash-bio">
        <div><div class="dash-total">${num(b.captured)}</div><div class="dash-card__sub">Captured</div></div>
        <div><div class="dash-total">${num(b.valid)}</div><div class="dash-card__sub">Valid</div></div>
        <div><div class="dash-total">${num(b.up_to_date)}</div><div class="dash-card__sub">Up to date</div></div>
        <div><div class="dash-total dash-total--amber">${num(b.needs_recapture)}</div><div class="dash-card__sub">Needs recapture</div></div>
      </div>
      <button class="btn btn--secondary btn--sm" data-export="biometric_coverage">Export</button>
    </div>`;
  }

  // -- disagg chips + per-card export --------------------------------------
  function wireDisaggChips() {
    grid.querySelectorAll('.dash-chips').forEach((row) => {
      row.addEventListener('click', (ev) => {
        const b = ev.target.closest('.chip'); if (!b) return;
        row.querySelectorAll('.chip').forEach((c) => c.classList.remove('chip--on'));
        b.classList.add('chip--on');
        const slug = row.dataset.card;
        const body = grid.querySelector(`[data-body="${slug}"]`);
        if (body) body.innerHTML = breakdownHtml(state.summary[slug], b.dataset.split);
      });
    });
    grid.querySelectorAll('[data-export]').forEach((btn) => {
      btn.addEventListener('click', () => exportIndicator(btn.dataset.export));
    });
  }

  // -- mount ---------------------------------------------------------------
  (async () => {
    state.status = await bridge.dashboard.status();
    const load = await bridge.dashboard.load_latest();
    if (!load.ok) {
      el('#dash-source').textContent = 'No Treatment linelist found in NMRS_Linelists.';
      grid.innerHTML = `<div class="dash__empty">No Treatment linelist found. Generate one
        (Linelists tab) or use “Refresh from DB”, then reopen this tab.</div>`;
      return;
    }
    state.facilities = [load.facility];
    el('#dash-facility').textContent = load.facility;
    el('#dash-source').textContent =
      `Source: ${load.source} · ${num(load.rows)} rows${load.generated_at ? ' · generated ' + load.generated_at : ''}`;
    await recompute();
  })();

  return { destroy() { subs.forEach((u) => u()); } };
}
