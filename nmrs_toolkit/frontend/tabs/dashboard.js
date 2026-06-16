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
      <div class="dash__hint is-hidden" id="dash-hint"></div>
      <div class="dash__grid" id="dash-grid"></div>
    </section>`;

  const el = (id) => root.querySelector(id);
  const grid = el('#dash-grid');

  // -- Chart.js (vendored, global window.Chart) ----------------------------
  const charts = [];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function destroyCharts() { charts.forEach((c) => { try { c.destroy(); } catch (e) { /**/ } }); charts.length = 0; }
  function tok(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  const COLORS = () => ({
    navy: tok('--color-primary', '#0B1F3A'),
    teal: tok('--color-accent', '#1FB6A6'),
    amber: tok('--color-warning', '#B07015'),
    danger: tok('--color-danger', '#B23B3B'),
    success: tok('--color-success', '#1B8B5A'),
    info: tok('--color-info', '#1E5FAA'),
    muted: tok('--color-text-faint', '#8A95A4'),
  });
  function pctTooltip(getTotal) {
    return {
      callbacks: {
        label(ctx) {
          const v = ctx.parsed.x != null ? ctx.parsed.x : ctx.parsed.y != null ? ctx.parsed.y : ctx.parsed;
          const val = Math.abs(typeof v === 'number' ? v : ctx.raw);
          const total = getTotal ? getTotal(ctx) : null;
          const pct = total ? ` (${Math.round((100 * val) / total)}%)` : '';
          return `${ctx.dataset.label || ctx.label}: ${val.toLocaleString()}${pct}`;
        },
      },
    };
  }
  function mk(canvas, config) {
    if (!window.Chart || !canvas) return;
    config.options = Object.assign({ responsive: true, maintainAspectRatio: false,
      animation: reduceMotion ? false : undefined }, config.options || {});
    charts.push(new window.Chart(canvas.getContext('2d'), config));
  }

  // -- date range (instant, in-memory recompute) ---------------------------
  // Enforce start <= end via native min/max (greys out invalid days in the
  // picker) rather than blur() — blur closed the whole calendar on month/year
  // navigation too. recompute() also guards as a backstop for typed input.
  el('#dash-start').addEventListener('change', (e) => {
    state.start = e.target.value;
    el('#dash-end').min = state.start;
    recompute();
  });
  el('#dash-end').addEventListener('change', (e) => {
    state.end = e.target.value;
    el('#dash-start').max = state.end;
    recompute();
  });

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
    if (res && res.ok && window.__toast) {
      window.__toast(`Exported report + ${res.linelist_rows}-client line list → NMRS_Dashboard_Exports`);
    } else if (window.__toast) {
      window.__toast(`Export failed: ${(res && res.message) || 'error'}`);
    }
  }

  // -- compute + render ----------------------------------------------------
  async function recompute() {
    if (state.start > state.end) {  // backstop for typed/out-of-order input
      const hint = el('#dash-hint');
      hint.textContent = `Start date (${state.start}) is after end date (${state.end}).`;
      hint.classList.remove('is-hidden');
      return;
    }
    const res = await bridge.dashboard.compute(state.start, state.end,
      state.facilityFilter.length ? state.facilityFilter : null);
    if (!res.ok) { grid.innerHTML = `<div class="dash__empty">${esc(res.message || 'No data.')}</div>`; return; }
    state.summary = res;
    // Snapshot indicators beyond the data date are projections (patients pushed
    // past their LTFU cutoff) — point the user to Refresh from DB for an exact one.
    const hint = el('#dash-hint');
    if (state.dataDate && state.end > state.dataDate) {
      hint.textContent = `Snapshots after the data date (${state.dataDate}) are projected forward `
        + `(TX_CURR drops as patients pass their next-pickup window). Use “Refresh from DB” for an exact snapshot at ${state.end}.`;
      hint.classList.remove('is-hidden');
    } else {
      hint.classList.add('is-hidden');
    }
    renderCards();
  }

  function renderCards() {
    const s = state.summary;
    const end = state.end, start = state.start;
    const asOf = `as of ${esc(end)}`;
    const period = `Period: ${esc(start)} to ${esc(end)}`;
    destroyCharts();
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
    buildCharts();
  }

  // -- Chart.js renderings -------------------------------------------------
  function buildCharts() {
    if (!window.Chart) return;  // graceful fallback if vendor file missing
    const s = state.summary;
    const c = COLORS();
    const cv = (name) => grid.querySelector(`canvas[data-chart="${name}"]`);
    const noLegend = { plugins: { legend: { display: false } } };

    // Cohort flow — horizontal bar, one bar per stage.
    const cf = s.cohort_flow;
    mk(cv('cohort'), {
      type: 'bar',
      data: { labels: ['Ever Enrolled', 'TX_NEW', 'TX_CURR', 'Currently IIT', 'TX_RTT'],
        datasets: [{ data: [cf.ever_enrolled, cf.tx_new, cf.tx_curr, cf.currently_iit, cf.tx_rtt],
          backgroundColor: [c.navy, c.info, c.teal, c.amber, c.success] }] },
      options: Object.assign({ indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: pctTooltip(() => cf.ever_enrolled) },
        scales: { x: { beginAtZero: true } } }),
    });

    // VL cascade — horizontal bar, graduated by stage.
    const vl = s.vl_cascade;
    mk(cv('vl'), {
      type: 'bar',
      data: { labels: ['Eligible', 'Sampled', 'With result', 'Suppressed'],
        datasets: [{ data: [vl.eligible, vl.sampled, vl.with_result, vl.suppressed],
          backgroundColor: [c.info, c.teal, c.navy, c.success] }] },
      options: { indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: pctTooltip(() => vl.eligible) },
        scales: { x: { beginAtZero: true } } },
    });

    // TX_ML — stacked horizontal bar by reason.
    const ml = s.tx_ml.by_reason || {};
    const mlReasons = ['Newly IIT', 'Died', 'Transferred Out', 'Refused/Stopped'];
    const mlColors = [c.amber, c.danger, c.info, c.muted];
    mk(cv('txml'), {
      type: 'bar',
      data: { labels: ['TX_ML'],
        datasets: mlReasons.map((r, i) => ({ label: r, data: [ml[r] || 0],
          backgroundColor: mlColors[i] })) },
      options: { indexAxis: 'y',
        plugins: { legend: { position: 'bottom' }, tooltip: pctTooltip(() => s.tx_ml.total) },
        scales: { x: { stacked: true, beginAtZero: true }, y: { stacked: true } } },
    });

    // MMD — doughnut.
    const mb = s.mmd_distribution.by_bucket || {};
    const mmdKeys = ['<3 months', '3-5 months', '>=6 months', 'Unknown'].filter((k) => mb[k]);
    mk(cv('mmd'), {
      type: 'doughnut',
      data: { labels: mmdKeys, datasets: [{ data: mmdKeys.map((k) => mb[k]),
        backgroundColor: [c.amber, c.info, c.teal, c.muted] }] },
      options: { plugins: { legend: { position: 'bottom' },
        tooltip: pctTooltip(() => s.mmd_distribution.total) } },
    });

    // Age/sex pyramid — diverging horizontal bar (F negative, M positive).
    const g = s.age_sex_pyramid.grid || {};
    const bands = ['Pediatric (0-9)', 'Adolescent (10-19)', 'Adult (20+)'];
    mk(cv('pyramid'), {
      type: 'bar',
      data: { labels: bands, datasets: [
        { label: 'F', data: bands.map((b) => -((g[b] || {}).F || 0)), backgroundColor: c.teal },
        { label: 'M', data: bands.map((b) => (g[b] || {}).M || 0), backgroundColor: c.navy }] },
      options: { indexAxis: 'y',
        plugins: { legend: { position: 'bottom' },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${Math.abs(ctx.parsed.x).toLocaleString()}` } } },
        scales: { x: { stacked: true, ticks: { callback: (v) => Math.abs(v) } }, y: { stacked: true } } },
    });

    // Biometric — Baseline / Recaptured / No capture (recapture ⊆ baseline).
    const b = s.biometric_coverage;
    mk(cv('bio'), {
      type: 'bar',
      data: { labels: ['Baseline', 'Recaptured', 'No capture'],
        datasets: [{ data: [b.baseline, b.recaptured, b.no_capture],
          backgroundColor: [c.success, c.teal, c.amber] }] },
      options: { indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: pctTooltip(() => b.total) },
        scales: { x: { beginAtZero: true } } },
    });
  }

  // -- card builders -------------------------------------------------------
  function chipRow(slug) {
    return `<div class="dash-chips" data-card="${slug}">` +
      SPLITS.map(([k, label], i) =>
        `<button class="chip ${i === 0 ? 'chip--on' : ''}" data-split="${k}">${label}</button>`).join('') +
      `</div>`;
  }
  // Fixed clinical orderings so disaggregations are stable (never count-sorted).
  const SEX_ORDER = ['F', 'M', 'Unknown'];
  const BAND_ORDER = ['Pediatric (0-9)', 'Adolescent (10-19)', 'Adult (20+)', 'Unknown'];
  function breakdownHtml(indicator, splitKey) {
    const field = (SPLITS.find((s) => s[0] === splitKey) || [])[2];
    if (!field || !indicator[field]) {
      return `<div class="dash-total">${num(indicator.total)}</div>`;
    }
    const data = indicator[field];
    if (splitKey === 'sex') {
      return rows(SEX_ORDER.filter((k) => k in data).map((k) => [k, data[k]]));
    }
    if (splitKey === 'age') {
      return rows(BAND_ORDER.filter((k) => k in data).map((k) => [k, data[k]]));
    }
    if (splitKey === 'sexage') {
      // Aligned grid: one row per age band, F / M columns side by side.
      const sexes = SEX_ORDER.filter((s) => BAND_ORDER.some((b) => `${s} | ${b}` in data));
      const header = `<tr><th>Age band</th>${sexes.map((s) => `<th>${s}</th>`).join('')}</tr>`;
      const body = BAND_ORDER
        .filter((b) => sexes.some((s) => `${s} | ${b}` in data))
        .map((b) => `<tr><td>${esc(b)}</td>${sexes.map((s) =>
          `<td>${num(data[`${s} | ${b}`] || 0)}</td>`).join('')}</tr>`).join('');
      return `<table class="dash-break">${header}${body}</table>`;
    }
    return rows(Object.entries(data).sort((a, b) => b[1] - a[1]));
  }
  function rows(entries) {
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
    return `<div class="dash-card">
      <div class="dash-card__title">Cohort Flow</div>
      <div class="dash-card__sub">Snapshot stages "as of ${esc(end)}"; TX_NEW/TX_RTT for ${esc(start)}..${esc(end)}</div>
      <div class="dash-chart"><canvas data-chart="cohort"></canvas></div>
      <div class="dash-card__sub">TX_ML this period: ${num(cf.tx_ml_total)} · Dead ${num(cf.dead)} · TO ${num(cf.transferred_out)} · Stopped ${num(cf.stopped)}</div>
    </div>`;
  }
  function vlCascadeCard(vl, stamp) {
    return `<div class="dash-card" data-indicator="vl_cascade">
      <div class="dash-card__title">VL Cascade</div>
      <div class="dash-card__sub">${esc(stamp)} · coverage ${vl.coverage_pct}% · suppression ${vl.suppression_pct}%</div>
      <div class="dash-chart"><canvas data-chart="vl"></canvas></div>
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
    return `<div class="dash-card" data-indicator="tx_ml">
      <div class="dash-card__title">TX_ML</div>
      <div class="dash-card__sub">${esc(stamp)} · total ${num(ml.total)}</div>
      <div class="dash-chart"><canvas data-chart="txml"></canvas></div>
      <button class="btn btn--secondary btn--sm" data-export="tx_ml">Export</button>
    </div>`;
  }
  function mmdCard(mmd, stamp) {
    return `<div class="dash-card" data-indicator="mmd_distribution">
      <div class="dash-card__title">MMD Share</div>
      <div class="dash-card__sub">${esc(stamp)} · coverage (≥3mo) ${mmd.mmd_coverage_pct}%</div>
      <div class="dash-chart"><canvas data-chart="mmd"></canvas></div>
    </div>`;
  }
  function pyramidCard(p, stamp) {
    return `<div class="dash-card" data-indicator="age_sex_pyramid">
      <div class="dash-card__title">Age / Sex Pyramid</div>
      <div class="dash-card__sub">${esc(stamp)} (TX_CURR) · F ◀ ▶ M</div>
      <div class="dash-chart"><canvas data-chart="pyramid"></canvas></div>
    </div>`;
  }
  function biometricCard(b, stamp) {
    const flag = b.suspicious
      ? `<div class="dash-card__sub dash-amber-inline">⚠ ${num(b.suspicious)} recapture(s) without a baseline (data anomaly)</div>`
      : '';
    return `<div class="dash-card" data-indicator="biometric_coverage">
      <div class="dash-card__title">Biometric Capture</div>
      <div class="dash-card__sub">${esc(stamp)} (TX_CURR) · recapture cascades from baseline</div>
      <div class="dash-bio">
        <div><div class="dash-total">${num(b.baseline)}</div><div class="dash-card__sub">Baseline</div></div>
        <div><div class="dash-total">${num(b.recaptured)}</div><div class="dash-card__sub">Recaptured</div></div>
        <div><div class="dash-total dash-total--amber">${num(b.no_capture)}</div><div class="dash-card__sub">No capture</div></div>
      </div>
      ${flag}
      <div class="dash-chart dash-chart--short"><canvas data-chart="bio"></canvas></div>
      <button class="btn btn--secondary btn--sm" data-export="biometric_coverage">Export (no-capture list)</button>
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
    // Default the snapshot end-date to the DATA date (the @endDate the linelist
    // was built at), not the current quarter-end. A future end_date would push
    // patients past their LTFU cutoff and make TX_CURR look reduced; anchoring on
    // the data date gives the true "current" count. Start = that date's quarter.
    if (load.generated_at) {
      const dd = new Date(load.generated_at);
      if (!isNaN(dd)) {
        state.dataDate = iso(dd);
        const q = Math.floor(dd.getMonth() / 3);
        state.start = iso(new Date(dd.getFullYear(), q * 3, 1));
        state.end = state.dataDate;
        el('#dash-start').value = state.start;
        el('#dash-end').value = state.end;
      }
    }
    // Enforce ordering from the start (start <= end) in the native pickers.
    el('#dash-start').max = state.end;
    el('#dash-end').min = state.start;
    await recompute();
  })();

  return { destroy() { subs.forEach((u) => u()); destroyCharts(); } };
}
