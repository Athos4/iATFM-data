async (page) => {
  // Run through the existing authenticated Playwright tab. This only saves
  // browser downloads; no credentials or external browser profile are read.
  if (!page.__radAutoSaveHandler) {
    page.__radSavedDownloads = [];
    page.__radDownloadErrors = [];
    page.__radAutoSaveHandler = async download => {
      const name = download.suggestedFilename();
      if (!/^database-2611-part-auto-\d+\.json$/.test(name)) return;
      try {
        await download.saveAs('Z:/ATC Tool/data/rad/' + name);
        page.__radSavedDownloads.push(name);
        await page.evaluate(name => {
          const c = window.__radCapture;
          const batch = c.pendingExports[name];
          if (!batch) return;
          const saved = new Set(batch.map(x => x.index));
          c.records = c.records.filter(x => !saved.has(x.index));
          c.savedCount = (c.savedCount || 0) + batch.length;
          delete c.pendingExports[name];
        }, name);
      } catch (error) {
        page.__radDownloadErrors.push({name, error: String(error)});
        await page.evaluate(() => { window.__radCapture.stop = true; });
      }
    };
    page.on('download', page.__radAutoSaveHandler);
  }
  return page.evaluate(() => {
    const c = window.__radCapture;
    if (!c) throw Error('No active RAD capture');
    if (c.saveTimer) clearInterval(c.saveTimer);
    c.pendingExports ||= {};
    c.savedCount = c.done - c.records.length;
    c.saveTimer = setInterval(() => {
      if (Object.keys(c.pendingExports).length) return;
      if (c.records.length < 50 && !c.finished) return;
      if (!c.records.length) return;
      const records = c.records.slice();
      const name = 'database-2611-part-auto-' + Date.now() + '.json';
      c.pendingExports[name] = records;
      const data = {
        schema: 'eurocontrol-rs-capture/1', branch: 'production',
        referenceStart: 1793232000000, capturedAt: new Date().toISOString(),
        expectedTotal: c.total, records,
        resume: {remainingIndices: c.remaining ? c.remaining() : c.queue.slice(c.cursor), errors: c.errors}
      };
      const url = URL.createObjectURL(new Blob([JSON.stringify(data)], {type: 'application/json'}));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    }, 5000);
    return {autosaveInstalled: true, done: c.done, buffered: c.records.length};
  });
}
