async page => {
  const data = await page.evaluate(() => ({
    schema:'eurocontrol-rs-dependencies/1',branch:'production',referenceStart:1793232000000,
    capturedAt:new Date().toISOString(),entities:window.__radDependencies,
    errors:window.__radDependencyCapture?.errors || []
  }));
  if (!data.entities) throw Error('No dependencies');
  const downloadPromise = page.waitForEvent('download');
  await page.evaluate(data => {
    const url=URL.createObjectURL(new Blob([JSON.stringify(data)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download='database-2611-dependencies.json';a.click();
    setTimeout(()=>URL.revokeObjectURL(url),30000);
  },data);
  const download=await downloadPromise;
  await download.saveAs('Z:/ATC Tool/data/rad/database-2611-dependencies.json');
  return {saved:Object.keys(data.entities).length,errors:data.errors};
}
