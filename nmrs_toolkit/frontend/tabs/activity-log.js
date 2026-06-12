// Activity Log drawer — the unified forensic surface (MIGRATION_PLAN.md section
// 4). It is the drawer chrome (collapse, disk-size indicator + manual rotate,
// open application.log) wrapped around a LogViewer showing ALL categories. The
// per-tab "Show full <workflow>.log" expanders reuse the same LogViewer locked
// to one category.

import bridge from '../bridge.js';
import { createLogViewer } from '../logviewer.js';

export function initActivityLog(root) {
  root.innerHTML = `
    <div class="drawer__head">
      <button class="drawer__toggle" id="al-toggle" aria-expanded="true">
        <span class="drawer__caret">▾</span> Activity Log
      </button>
      <div class="drawer__headright">
        <button class="drawer__link" id="al-open">Open application.log</button>
        <button class="drawer__disk" id="al-disk" title="Click to rotate now"></button>
      </div>
    </div>
    <div class="drawer__body"><div class="drawer__viewer" id="al-viewer"></div></div>`;

  createLogViewer(root.querySelector('#al-viewer'), { fixedCategory: null });

  // Enable Python -> JS live log push once for the whole app (the drawer is
  // always mounted). Every onLog subscriber (drawer, tab cards, tab full-logs)
  // then receives live events via the frontend event bus.
  bridge.log.subscribe();

  const el = (id) => root.querySelector(id);

  el('#al-toggle').addEventListener('click', () => {
    const collapsed = root.classList.toggle('drawer--collapsed');
    el('#al-toggle').setAttribute('aria-expanded', String(!collapsed));
  });

  el('#al-open').addEventListener('click', () => bridge.log.open_in_editor());

  async function refreshDisk() {
    const info = await bridge.log.disk_info();
    const rot = info.last_rotated ? ` · last rotated ${info.last_rotated}` : '';
    el('#al-disk').textContent = `${info.name} · ${info.size_human}${rot}`;
  }
  el('#al-disk').addEventListener('click', async () => {
    await bridge.log.rotate_now();
    refreshDisk();
  });
  refreshDisk();
}
