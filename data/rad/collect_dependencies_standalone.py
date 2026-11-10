"""Fetch only aerodrome sets referenced by the captured RAD restrictions.

Run after the restriction collector has stopped. Credentials stay in RAM and
each successful entity is saved atomically; HTTP/authentication errors stop.
"""
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener
from collect_standalone import ROOT, ORIGIN, START, NoRedirect, receive_session, write_atomic
from import_database import END


def status(**fields):
    value={'updatedAt':datetime.now(timezone.utc).isoformat(),**fields}
    write_atomic(ROOT/'dependency-collection-status.json',value)
    print(json.dumps(value),flush=True)


def pending_references():
    refs={}
    def visit(v):
        if isinstance(v,dict):
            if v.get('@class','').endswith('.AerodromeSetSummary'):
                ref=v['ref'];key='az:'+ref['backendId']
                if key in refs and refs[key]!=ref:raise ValueError('Conflicting aerodrome-set identity: '+key)
                refs[key]=ref
            for x in v.values():visit(x)
        elif isinstance(v,list):
            for x in v:visit(x)
    for path in ROOT.glob('database-2611-part-*.json'):
        data=json.loads(path.read_text(encoding='utf-8'))
        if data['branch']!='production' or data['referenceStart']!=START:raise ValueError('Wrong capture cycle')
        for record in data['records']:
            for state in record['data']['states']:
                if state['period']['start']<END and state['period']['end']>START:visit(state)
    return refs


def main():
    if (ROOT/'collection.lock').exists():raise SystemExit('Restriction collector is still registered; do not overlap requests')
    path=ROOT/'database-2611-dependencies.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    if data['branch']!='production' or data['referenceStart']!=START:raise ValueError('Wrong dependency cycle')
    refs=pending_references();entities=data['entities']
    for key,ref in refs.items():
        if key in entities and entities[key]['ref']!=ref:raise ValueError('Saved dependency identity mismatch: '+key)
    pending=[(k,r) for k,r in sorted(refs.items()) if k not in entities]
    if not pending:
        status(state='complete',saved=len(entities),referenced=len(refs),remaining=0);return
    session=receive_session('dependency-bootstrap.json',status)
    opener=build_opener(NoRedirect());done=0;next_request=time.monotonic()
    try:
        for key,ref in pending:
            time.sleep(max(0,next_request-time.monotonic()));next_request=time.monotonic()+0.5
            url=ORIGIN+'/api/airspace/v1/az/'+quote(ref['aerodromeSetId'],safe='')+'?'+urlencode({
                'backendId':ref['backendId'],'branchId':'production'})
            request=Request(url,headers={'Accept':'application/json','Content-Type':'application/json',
                'Cookie':session['cookie'],'User-Agent':session['userAgent']})
            with opener.open(request,timeout=60) as response:
                if response.status!=200 or 'application/json' not in response.headers.get('Content-Type',''):
                    raise RuntimeError('Non-JSON or unsuccessful dependency response')
                entity=json.load(response)
            if entity.get('ref')!=ref or not isinstance(entity.get('states'),list):raise ValueError('Invalid dependency identity: '+key)
            entities[key]=entity;data['capturedAt']=datetime.now(timezone.utc).isoformat()
            write_atomic(path,data);done+=1
            status(state='running',saved=len(entities),referenced=len(refs),remaining=len(pending)-done)
        status(state='complete',saved=len(entities),referenced=len(refs),remaining=0)
    except Exception as error:
        status(state='stoppedOnError',saved=len(entities),remaining=len(pending)-done,error=str(error));raise


if __name__=='__main__':main()
