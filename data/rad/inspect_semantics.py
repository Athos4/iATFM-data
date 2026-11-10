"""Print small native examples for unresolved semantics without full captures."""
import json
from pathlib import Path

found = {}
def walk(value):
    if isinstance(value, dict):
        if 'verticalLimits' in value:
            name = value.get('@class', '').rsplit('.', 1)[-1]
            key = 'vertical:' + name
            if key not in found:
                found[key] = value
        for child in value.values():
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)

for path in sorted(Path(__file__).parent.glob('database-2611-part-*.json')):
    for record in json.loads(path.read_text(encoding='utf-8'))['records']:
        for state in record['data']['states']:
            if state.get('duringApplicability') is False and 'negativeApplicability' not in found:
                found['negativeApplicability'] = {k: state[k] for k in ('period','applicabilities','eventApplicabilities','duringApplicability') if k in state}
            walk(state)
    if len(found) >= 14:
        break
print(json.dumps(found, ensure_ascii=False))
