async page => {
  const data=await page.evaluate(()=>{
    const c=window.__radCapture;
    if(!c || !c.finished || c.records.length) throw Error('Capture not fully saved');
    return {schema:'eurocontrol-rs-checkpoint/1',started:c.started,checkpointAt:new Date().toISOString(),
      expectedTotal:c.total,captured:c.done,saved:c.savedCount,remainingIndices:c.remaining(),
      errors:c.errors,requiresHumanVerification:c.errors.some(e=>/HTTP (429|403)/.test(e.error))};
  });
  const pending=page.waitForEvent('download');
  await page.evaluate(data=>{
    const u=URL.createObjectURL(new Blob([JSON.stringify(data)],{type:'application/json'}));
    const a=document.createElement('a');a.href=u;a.download='database-checkpoint.json';a.click();
    setTimeout(()=>URL.revokeObjectURL(u),30000);
  },data);
  const download=await pending;
  await download.saveAs('Z:/ATC Tool/data/rad/database-checkpoint.json');
  return {saved:data.saved,remaining:data.remainingIndices.length,errors:data.errors};
}
