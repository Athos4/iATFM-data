async (page) => {
  // Continue only an existing capture in this authenticated, user-selected tab.
  // A challenge or any HTTP error stops all workers; no automatic retry.
  await page.evaluate(async () => {
    const c = window.__radCapture;
    if (!c || (c.errors.length && c.manualResumeApproved !== true)) throw Error('Capture missing or requires manual error resolution');
    c.stop = true;
    await c.worker;
    c.queue = c.remaining ? c.remaining() : c.queue.slice(c.cursor);
    c.errors = [];
    c.manualResumeApproved = false;
    c.cursor = 0;
    c.inFlight = {};
    c.stop = false;
    c.finished = false;
    c.nextRequestAt = Date.now();
    c.remaining = () => [...new Set([
      ...c.errors.map(e => e.index),
      ...Object.keys(c.inFlight).map(Number),
      ...c.queue.slice(c.cursor)
    ])];
    async function worker() {
      while (c.cursor < c.queue.length && !c.stop) {
        const wait = Math.max(0, c.nextRequestAt - Date.now());
        c.nextRequestAt = Math.max(Date.now(), c.nextRequestAt) + 1000;
        await new Promise(resolve => setTimeout(resolve, wait));
        if (c.stop || c.cursor >= c.queue.length) break;
        const index = c.queue[c.cursor++];
        c.inFlight[index] = true;
        const summary = window.__radIndex[index].RESTRICTION;
        try {
          const url = '/api/airspace/v1/rs/' + encodeURIComponent(summary.ref.restrictionId)
            + '?' + new URLSearchParams({backendId: summary.ref.backendId, branchId: 'production'});
          const response = await fetch(url, {headers: {accept: 'application/json', 'content-type': 'application/json'}});
          if (!response.ok) throw Error('HTTP ' + response.status);
          const data = await response.json();
          if (!data.ref || !Array.isArray(data.states)
              || ['backendId','uuid','restrictionId'].some(key => data.ref[key] !== summary.ref[key])) {
            throw Error('Invalid or mismatched entity response');
          }
          c.records.push({index, summary, data, fetchedAt: new Date().toISOString()});
          c.done++;
        } catch (error) {
          c.errors.push({index, id: summary.ref.restrictionId, error: String(error)});
          c.stop = true;
        } finally {
          delete c.inFlight[index];
        }
      }
    }
    c.worker = worker().then(() => { c.finished = true; });
    if (c.saveTimer) clearInterval(c.saveTimer);
    c.saveTimer = null;
  });
  return page.evaluate(() => ({done: window.__radCapture.done, pending: window.__radCapture.remaining().length,
                               maximumRequestsPerSecond: 1, maximumConcurrent: 1}));
}
