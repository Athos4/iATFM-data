"""Build a browser bootstrap from verified captures after a page reload.

The generated function contains restriction metadata only, never credentials.
It restores state but performs no HTTP requests; continue_capture.js starts work.
"""
import json
import gzip
import base64
from pathlib import Path

root = Path(__file__).parent
index = None
seen = {}
for path in sorted(root.glob('database-2611-part-*.json')):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 'eurocontrol-rs-capture/1' or data.get('branch') != 'production' or data.get('referenceStart') != 1793232000000:
        raise ValueError('Mixed capture: ' + path.name)
    if 'index' in data:
        if index is not None and index != data['index']:
            raise ValueError('Conflicting source index')
        index = data['index']
    for record in data['records']:
        i = record['index']
        ref = record['data']['ref']
        if i in seen and seen[i] != ref:
            raise ValueError('Conflicting saved index')
        seen[i] = ref
if index is None:
    raise ValueError('No saved source index')
for i, ref in seen.items():
    if index[i]['RESTRICTION']['ref'] != ref:
        raise ValueError('Record/index identity mismatch')
checkpoint_path = root / 'database-checkpoint.json'
checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.exists() else {}
remaining = [i for i in checkpoint.get('remainingIndices', []) if i not in seen]
queued = set(remaining)
remaining += [i for i in range(len(index)) if i not in seen and i not in queued]
if len(set(remaining)) + len(seen) != len(index):
    raise ValueError('Incomplete resume partition')
dependencies_path = root / 'database-2611-dependencies.json'
dependencies = json.loads(dependencies_path.read_text(encoding='utf-8'))['entities'] if dependencies_path.exists() else {}
payload = {'index': index, 'remaining': remaining, 'done': len(seen),
           'started': checkpoint.get('started'), 'dependencies': dependencies}
function = '''async (page) => page.evaluate(async encoded => {
  const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  const payload = JSON.parse(await new Response(stream).text());
  if (location.origin !== 'https://b2c.nm.eurocontrol.int') throw Error('Wrong browser origin');
  if (window.__radCapture && !window.__radCapture.finished) throw Error('Capture already running');
  window.__radIndex = payload.index;
  window.__radDependencies = payload.dependencies;
  window.__radCapture = {
    queue: payload.remaining, cursor: 0, done: payload.done,
    savedCount: payload.done, total: payload.index.length,
    started: payload.started, resumedAt: new Date().toISOString(),
    records: [], errors: [], finished: true, stop: true, worker: Promise.resolve()
  };
  return {restored: payload.done, remaining: payload.remaining.length, expected: payload.index.length};
}, PAYLOAD)
'''
encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(',', ':')).encode('utf-8'))).decode('ascii')
(root / 'resume_bootstrap.js').write_text(function.replace('PAYLOAD', json.dumps(encoded)), encoding='utf-8')
print(json.dumps({'captured': len(seen), 'remaining': len(remaining), 'expected': len(index)}))
