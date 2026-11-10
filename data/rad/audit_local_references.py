"""Audit source identifier presence in the local iATFM reference files.

This is a static presence check, not a flight evaluation or a cycle/provenance
validation. It deliberately does not import navigation from EUROCONTROL.
"""
import collections
import json
from pathlib import Path
from import_database import identifier, kind, write_json_atomic

ROOT=Path(__file__).resolve().parent

def run():
    data=ROOT.parent
    available={k:set() for k in ('point','airport','airspace','route')}
    manifest=json.loads((data/'eurocontrol-iatfm/manifest.json').read_text(encoding='utf-8'))
    for name in manifest['datasets']['sectors']:
        sectors=json.loads((data/'eurocontrol-iatfm'/name).read_text(encoding='utf-8'))
        for sector in sectors:
            if sector.get('id') and sector.get('blocks'):
                available['airspace'].add(sector['id'])
    for path in (data/'navdata').rglob('*.json'):
        nav=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(nav,dict):continue
        for field,target in [('points','point'),('airports','airport'),('airways','route'),('airwaySegments','route')]:
            available[target].update(nav.get(field,{}))
        # NMP RouteSummary also identifies terminal procedures.
        for procedures in nav.get('procedures',{}).values():
            available['route'].update(p['name'] for p in procedures if p.get('name'))
    candidate=json.loads((ROOT/'catalogue.database.candidate.json').read_text(encoding='utf-8'))
    classes={'PointSummary':'point','AerodromeSummary':'airport','AirspaceSummary':'airspace','RouteSummary':'route'}
    missing=collections.defaultdict(lambda:collections.defaultdict(set))
    by_rule={};converted_missing=set()
    def visit(value,refs):
        if isinstance(value,dict):
            group=classes.get(kind(value));name=identifier(value)
            if group and name:refs.add((group,name))
            for v in value.values():visit(v,refs)
        elif isinstance(value,list):
            for v in value:visit(v,refs)
    for rule in candidate['rules']:
        refs=set();visit(rule['sourceState'],refs)
        absent=[]
        for group,name in sorted(refs):
            if name not in available[group]:
                missing[group][name].add(rule['key']);absent.append({'kind':group,'id':name})
        if absent:
            by_rule[rule['key']]={'id':rule['id'],'references':absent}
            if not rule['conversionDiagnostics']:converted_missing.add(rule['key'])
    report={'schema':'iatfm-rad-reference-presence/1','airac':candidate['airac'],
        'capturedRestrictions':candidate['summary']['capturedRestrictions'],
        'states':len(candidate['rules']),'statesWithAbsentSourceIdentifiers':len(by_rule),
        'translatedStatesWithAbsentSourceIdentifiers':len(converted_missing),
        'spaceSourceDate':manifest.get('summary',{}).get('sourceDate'),
        'limitations':['Static identifier presence only; no flight, geometry identity, alias or AIRAC validity validation.',
                      'Includes source conditions whose conversion remains incomplete. Missing source identifiers are not a conversion percentage.'],
        'availableIdentifiers':{k:len(v) for k,v in available.items()},
        'missingIdentifiers':{k:[{'id':name,'affectedStates':len(rules)} for name,rules in sorted(v.items())] for k,v in missing.items()},
        'affectedRules':by_rule}
    write_json_atomic(ROOT/'local-reference-audit.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('affectedRules','missingIdentifiers')}))
    print(json.dumps({'missingIdentifierCounts':{k:len(v) for k,v in missing.items()}}))

if __name__=='__main__':run()
