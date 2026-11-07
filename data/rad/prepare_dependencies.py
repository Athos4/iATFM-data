"""Prepare read-only collection of referenced aerodrome sets, sharing pacing."""
import json
from pathlib import Path

root = Path(__file__).parent
audit = json.loads((root / 'database-audit.json').read_text(encoding='utf-8'))
known = json.loads((root / 'database-2611-dependencies.json').read_text(encoding='utf-8'))['entities']
pending = [r for r in audit['aerodromeSetDependencies'] if 'az:' + r['backendId'] not in known]
source = '''async page => page.evaluate(refs => {
  const c = window.__radCapture;
  if (!c || c.stop) throw Error('Capture not running');
  if (window.__radDependencyCapture && !window.__radDependencyCapture.finished) throw Error('Already running');
  const d = window.__radDependencyCapture = {done:0,total:refs.length,errors:[],finished:false};
  d.worker = (async () => {
    for (const ref of refs) {
      if (c.stop) break;
      const wait = Math.max(0, c.nextRequestAt - Date.now());
      c.nextRequestAt = Math.max(Date.now(), c.nextRequestAt) + 2000;
      await new Promise(resolve => setTimeout(resolve, wait));
      if (c.stop) break;
      try {
        const response = await fetch('/api/airspace/v1/az/' + encodeURIComponent(ref.aerodromeSetId)
          + '?' + new URLSearchParams({backendId:ref.backendId,branchId:'production'}),
          {headers:{accept:'application/json','content-type':'application/json'}});
        if (!response.ok) throw Error('HTTP '+response.status);
        const entity = await response.json();
        if (!entity.ref || entity.ref.backendId !== ref.backendId || entity.ref.uuid !== ref.uuid
            || !Array.isArray(entity.states)) throw Error('Invalid dependency identity');
        window.__radDependencies['az:'+ref.backendId] = entity;
        d.done++;
      } catch(error) {d.errors.push({ref,error:String(error)});c.stop=true;break;}
    }
    d.finished=true;
  })();
  return {pending:d.total};
}, REFS)
'''
(root / 'collect_dependencies.js').write_text(source.replace('REFS', json.dumps(pending)), encoding='utf-8')
print(json.dumps({'known':len(known),'pending':len(pending)}))
