"""Audit captured EUROCONTROL restriction records without altering the live catalogue."""
import collections
import hashlib
import json
import sys
from pathlib import Path


def audit(paths):
    records = {}
    expected = {}
    classes = collections.Counter()
    fields = collections.defaultdict(collections.Counter)
    examples = {}
    types = collections.Counter()
    state_count = 0
    dependencies = {}
    enums = collections.defaultdict(collections.Counter)

    def walk(value):
        if isinstance(value, dict):
            kind = value.get('@class', '').rsplit('.', 1)[-1]
            if kind:
                classes[kind] += 1
                fields[kind].update(value.keys())
                examples.setdefault(kind, value)
                for key in ('operator', 'operand', 'value', 'restrictionType', 'verticalLimitReference',
                            'LIMIT', 'terminalType', 'enabled'):
                    val = value.get(key)
                    if isinstance(val, str) and len(val) < 64:
                        enums[kind + '.' + key][val] += 1
                if kind == 'AerodromeSetSummary' and 'ref' in value:
                    ref = value['ref']
                    dependencies[ref['backendId']] = ref
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for path in paths:
        data = json.loads(path.read_text(encoding='utf-8'))
        for item in data.get('index', []):
            item = item['RESTRICTION']
            expected[item['ref']['backendId']] = item
        for record in data['records']:
            entity = record['data']
            key = entity['ref']['backendId']
            digest = hashlib.sha256(json.dumps(entity, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            if key in records and records[key] != digest:
                raise ValueError(f'Conflicting capture: {key}')
            if key not in records:
                records[key] = digest
                types[entity['@class'].rsplit('.', 1)[-1]] += 1
                state_count += len(entity['states'])
                walk(entity)
    missing = sorted(set(expected) - set(records))
    return {
        'expectedRestrictions': len(expected),
        'capturedRestrictions': len(records),
        'capturedStates': state_count,
        'missingRestrictions': missing,
        'captureComplete': bool(expected) and not missing,
        'conversionValidated': False,
        'types': dict(types),
        'classes': dict(classes),
        'fieldsByClass': {k: dict(v) for k, v in fields.items()},
        'examplesByClass': examples,
        'aerodromeSetDependencies': list(dependencies.values()),
        'enums': {k: dict(v) for k, v in enums.items()},
    }


if __name__ == '__main__':
    root = Path(__file__).parent
    result = audit(sorted(root.glob('database-2611-part-*.json')))
    (root / 'database-audit.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if '--dependencies' in sys.argv:
        print(json.dumps(result['aerodromeSetDependencies']))
    elif '--enums' in sys.argv:
        print(json.dumps(result['enums']))
    else:
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ('missingRestrictions', 'fieldsByClass', 'examplesByClass', 'aerodromeSetDependencies', 'enums')}))
