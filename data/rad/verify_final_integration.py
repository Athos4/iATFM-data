"""Independently verify full capture, lossless source retention and >90% translation.

This does not certify operational equivalence to IFPS and never publishes a
candidate. It intentionally fails until the requested measurable threshold is met.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from import_database import START, END, write_json_atomic

ROOT=Path(__file__).resolve().parent


def main():
    candidate=json.loads((ROOT/'catalogue.database.candidate.json').read_text(encoding='utf-8'))
    report=json.loads((ROOT/'database-integration-report.json').read_text(encoding='utf-8'))
    expected={};records={};states={};historical=0
    paths=sorted(ROOT.glob('database-2611-part-*.json'))
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert hashes=={s['file']:s['sha256'] for s in candidate['sourceFiles']},'Candidate is not current with all fragments'
    for path in paths:
        data=json.loads(path.read_text(encoding='utf-8'))
        assert data['branch']=='production' and data['referenceStart']==START
        for item in data.get('index',[]):
            summary=item['RESTRICTION'];key=summary['ref']['backendId']
            assert key not in expected or expected[key]==summary
            expected[key]=summary
        for record in data['records']:
            entity=record['data'];key=entity['ref']['backendId']
            assert entity['ref']==record['summary']['ref']
            if key in records:
                assert records[key]==record['data'],'Conflicting duplicate capture'
                continue
            records[key]=entity
            for state in entity['states']:
                assert state['period']['start']<state['period']['end']
                if state['period']['start']<END and state['period']['end']>START:
                    state_key='NMP:'+key+':'+state['stateId']
                    assert state_key not in states
                    states[state_key]=state
                else:historical+=1
    assert set(expected)==set(records),'Extraction remains incomplete'
    assert all(records[k]['ref']==expected[k]['ref'] for k in records)
    actual={r['key']:r for r in candidate['rules']}
    assert len(actual)==len(candidate['rules']) and set(actual)==set(states),'Missing or duplicate AIRAC states'
    assert all(actual[k]['sourceState']==s for k,s in states.items()),'Native source state modified'
    def unknown(value):
        if isinstance(value,dict):return value.get('op')=='unknown' or any(unknown(v) for v in value.values())
        return isinstance(value,list) and any(unknown(v) for v in value)
    translated=[]
    for rule in candidate['rules']:
        if not rule['conversionDiagnostics']:
            assert not any(unknown(rule.get(k)) for k in ('scope','schedule','referenceLocation','clauses','profileAdjustments'))
            translated.append(rule)
    rate=100*len(translated)/len(states)
    assert rate>90,f'Only {rate:.6f}% of AIRAC states fully translated'
    summary=candidate['summary']
    assert summary['rules']==len(states) and summary['rulesWithPendingSemantics']==len(states)-len(translated)
    assert summary['missingRestrictions']==0 and summary['capturedRestrictions']==len(records)
    assert report['summary']==summary and abs(report['conversionPercent']-rate)<1e-10
    dependencies=json.loads((ROOT/'database-2611-dependencies.json').read_text(encoding='utf-8'))['entities']
    assert dependencies==candidate['sourceDependencies'],'Candidate uses stale dependencies'
    from collect_dependencies_standalone import pending_references
    refs=pending_references()
    assert all(k in dependencies and dependencies[k]['ref']==v for k,v in refs.items()),'Referenced aerodrome set still absent'
    repo=ROOT.parents[1]
    installed={
        'runtime_integration.rs':'crates/iatfm-server/src/app/rad_native_integration.rs',
        'runtime_integration_tests.rs':'crates/iatfm-server/src/app/rad_native_regression_tests.rs',
        'profile_core_integration.rs':'crates/iatfm-core/src/dashboard/rad_native_profile.rs',
        'profile_server_integration.rs':'crates/iatfm-server/src/app/rad_native_profile.rs',
    }
    for source,target in installed.items():assert (ROOT/source).read_bytes()==(repo/target).read_bytes(),target+' differs from tested staging'
    result={'verifiedAt':datetime.now(timezone.utc).isoformat(),'airac':candidate['airac'],
        'completeExtraction':True,'capturedRestrictions':len(records),'expectedRestrictions':len(expected),
        'eligibleStates':len(states),'historicalStatesRetainedInRawCaptures':historical,
        'translatedStates':len(translated),'incompleteStates':len(states)-len(translated),'conversionPercent':rate,
        'allEligibleSourceStatesPreservedUnchanged':True,'allFragmentsIncluded':True,
        'allReferencedAerodromeSetsPresent':True,'referencedAerodromeSets':len(refs),
        'candidateSha256':hashlib.sha256((ROOT/'catalogue.database.candidate.json').read_bytes()).hexdigest(),
        'operationallyValidated':False,'nativeCatalogueActivated':(ROOT/'catalogue.database.active.json').is_file()}
    write_json_atomic(ROOT/'database-final-verification.json',result)
    print(json.dumps(result))


if __name__=='__main__':main()
