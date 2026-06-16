// Frontend event bus. Python pushes live events into the page by calling the
// globals window.__onLogEvent / window.__onOpEvent (via evaluate_js). This
// module owns those globals and fans each event out to any number of
// subscribers, so the Activity Log drawer, a tab's recent-activity card, and a
// tab's "full log" viewer can all listen at once without clobbering each other.

const logSubs = new Set();
const opSubs = new Set();

window.__onLogEvent = (event) => {
  logSubs.forEach((fn) => { try { fn(event); } catch (e) { /* isolate */ } });
};
window.__onOpEvent = (event) => {
  opSubs.forEach((fn) => { try { fn(event); } catch (e) { /* isolate */ } });
};

// Subscribe to live log events. Returns an unsubscribe function.
export function onLog(fn) {
  logSubs.add(fn);
  return () => logSubs.delete(fn);
}

// Subscribe to long-running-operation events. Returns an unsubscribe function.
export function onOp(fn) {
  opSubs.add(fn);
  return () => opSubs.delete(fn);
}
