// SPA shell + routing. Phase 1: login screen -> post-login AppShell with the
// branded header, the prominent DB profile banner (safety affordance), a tab
// nav, and a placeholder content area. Later phases mount real tabs + the
// Activity Log drawer into this shell.

import bridge from './bridge.js';
import * as icons from './icons.js';
import { renderLogin } from './tabs/login.js';
import { initActivityLog } from './tabs/activity-log.js';
import { renderBackupTab } from './tabs/backup.js';
import { renderRestoreTab } from './tabs/restore.js';

const root = document.getElementById('root');

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const TABS = ['Linelists', 'Merge Reports', 'Backup', 'Restore', 'Unvoid Patient'];

// Tab name -> renderer. Renderers return { destroy } for cleanup on switch.
// Tabs not yet rebuilt fall back to a placeholder.
const TAB_RENDERERS = {
  Backup: renderBackupTab,
  Restore: renderRestoreTab,
};

let currentTab = null;  // { destroy }

function renderShell(summary) {
  const cls = `db-banner--${summary.profile_class || 'unlabeled'}`;
  const detail =
    `DB: ${esc(summary.db_name)} @ ${esc(summary.host)}:${esc(summary.port)} ` +
    `· user: ${esc(summary.user)}`;

  root.innerHTML = `
    <div class="shell">
      <header class="shell__header">
        <div class="shell__brand">
          <span class="shell__title">${esc(summary.app_name)}</span>
          <span class="shell__meta">v${esc(summary.app_version)} · ${esc(summary.org)}</span>
        </div>
      </header>

      <div class="db-banner ${cls}" role="status" aria-label="Active database profile">
        <span class="db-banner__icon">${icons.database}</span>
        <span class="db-banner__chip">${esc(summary.db_label)}</span>
        <span class="db-banner__detail">${detail}</span>
      </div>

      <nav class="shell__nav" id="tabnav">
        ${TABS.map((t, i) =>
          `<button class="tab ${i === 0 ? 'tab--active' : ''}" data-tab="${esc(t)}">${esc(t)}</button>`
        ).join('')}
      </nav>

      <main class="shell__content" id="content"></main>

      <section class="drawer" id="drawer"></section>
    </div>`;

  initActivityLog(root.querySelector('#drawer'));

  const nav = root.querySelector('#tabnav');
  nav.addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    nav.querySelectorAll('.tab').forEach((b) => b.classList.remove('tab--active'));
    btn.classList.add('tab--active');
    showTab(btn.dataset.tab);
  });

  showTab(TABS[0]);
}

function showTab(tabName) {
  if (currentTab && currentTab.destroy) {
    try { currentTab.destroy(); } catch (e) { /* ignore */ }
  }
  currentTab = null;
  const content = root.querySelector('#content');
  content.innerHTML = '';
  const renderer = TAB_RENDERERS[tabName];
  if (renderer) {
    currentTab = renderer(content) || null;
  } else {
    renderPlaceholder(content, tabName);
  }
}

function renderPlaceholder(content, tabName) {
  content.innerHTML = `
    <div class="placeholder">
      <span class="placeholder__icon">${icons.tools}</span>
      <div class="placeholder__title">${esc(tabName)}</div>
      <div>This tab is being rebuilt under the new design system. Coming in v2.x.</div>
    </div>`;
}

// Global, non-blocking toast (used by tabs + the LogViewer).
function installToast() {
  const t = document.createElement('div');
  t.className = 'al-toast';
  document.body.appendChild(t);
  let timer = null;
  window.__toast = (msg) => {
    t.textContent = msg;
    t.classList.add('al-toast--on');
    clearTimeout(timer);
    timer = setTimeout(() => t.classList.remove('al-toast--on'), 3600);
  };
}

async function enterApp() {
  const summary = await bridge.config.get_summary();
  if (!summary.ok) {
    root.innerHTML = `
      <div class="fatal">
        <div class="fatal__title">Configuration error</div>
        <div class="fatal__body">${esc(summary.config_error || 'Unknown error.')}</div>
      </div>`;
    return;
  }
  renderShell(summary);
}

async function main() {
  installToast();
  await renderLogin(root, enterApp);
}

main();
