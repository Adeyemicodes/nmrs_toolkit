// Bridge to Python. The frontend NEVER touches the OS, the DB, or the
// filesystem directly — every privileged action goes through window.pywebview.api
// (MIGRATION_PLAN.md section 7). No browser storage is used anywhere.
//
// pywebview exposes Python methods on window.pywebview.api after it fires the
// `pywebviewready` event. We wait for that, then present the flat Python
// methods (auth_login, config_get_summary) as the dotted namespaces from the
// plan (auth.login, config.get_summary).

const ready = new Promise((resolve) => {
  if (window.pywebview && window.pywebview.api) {
    resolve();
  } else {
    window.addEventListener('pywebviewready', () => resolve(), { once: true });
  }
});

// Serialize all bridge calls: only one may be in flight at a time. PyWebView's
// (GTK/WebKit2 4.0) Python<->JS bridge delivers each call's result by evaluating
// JS, and concurrent in-flight calls can collide so some promises never settle.
// Chaining every call behind the previous one makes the bridge reliable
// regardless of how many components invoke it at once.
let chain = Promise.resolve();

function call(method, ...args) {
  const run = async () => {
    await ready;
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api[method]) {
      throw new Error(`bridge method not available: ${method}`);
    }
    return window.pywebview.api[method](...args);
  };
  const result = chain.then(run, run); // run regardless of the previous outcome
  chain = result.then(() => {}, () => {}); // keep the chain alive past errors
  return result;
}

export const bridge = {
  ready,
  auth: {
    status: () => call('auth_status'),
    login: (password) => call('auth_login', password),
  },
  config: {
    get_summary: () => call('config_get_summary'),
  },
  backup: {
    list_facilities: () => call('backup_list_facilities'),
    run_now: () => call('backup_run_now'),
    update_schedules: () => call('backup_update_schedules'),
    open_folder: () => call('backup_open_folder'),
    schedule_status: () => call('backup_schedule_status'),
  },
  merge: {
    pick_files: () => call('merge_pick_files'),
    add_files: (paths) => call('merge_add_files', paths || []),
    pick_output: (suggested) => call('merge_pick_output', suggested || 'merged.csv'),
    run: (filePaths, sortCol, descending, outputPath, encrypt) =>
      call('merge_run', filePaths, sortCol || '', !!descending, outputPath, !!encrypt),
  },
  linelists: {
    list_bundled: () => call('linelist_list_bundled'),
    pick_custom: () => call('linelist_pick_custom'),
    run: (source, outputName, encrypt) => call('linelist_run', source, outputName, !!encrypt),
    run_weekly_batch: (encrypt) => call('linelist_run_weekly_batch', !!encrypt),
    open_folder: () => call('linelist_open_folder'),
  },
  restore: {
    pick_file: () => call('restore_pick_file'),
    preview: (dumpPath) => call('restore_preview', dumpPath),
    run: (dumpPath, targetDb, keyHex, typedConfirmation) =>
      call('restore_run', dumpPath, targetDb, keyHex, typedConfirmation),
    cancel: (operationId) => call('restore_cancel', operationId),
  },
  log: {
    subscribe: (categories) => call('log_subscribe', categories || null),
    unsubscribe: () => call('log_unsubscribe'),
    tail: (n, categories, levels) => call('log_tail', n || 1000, categories || null, levels || null),
    search: (query, filters) => call('log_search', query || '', filters || null),
    export: (filters, query) => call('log_export', filters || null, query || ''),
    open_in_editor: () => call('log_open_in_editor'),
    disk_info: () => call('log_disk_info'),
    rotate_now: () => call('log_rotate_now'),
  },
};

export default bridge;
