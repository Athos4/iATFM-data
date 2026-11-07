"""Continue the authorized read-only NMP capture independently of the chat tab.

Authentication is handed over once over a token-protected loopback endpoint,
kept only in memory, and used only for the fixed EUROCONTROL origin. No retries
on HTTP/authentication errors. Existing fragments remain the source of truth.
"""
import json
import argparse
import os
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT=Path(__file__).resolve().parent
ORIGIN='https://b2c.nm.eurocontrol.int'
START=1793232000000

def write_atomic(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    temporary.replace(path)

def status(**fields):
    document={'updatedAt':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),**fields}
    write_atomic(ROOT/'collection-status.json',document)
    print(json.dumps(document,ensure_ascii=False),flush=True)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        # Never forward the authentication header to another origin/login page.
        return None

def receive_session(descriptor_name='collection-bootstrap.json',report=status):
    token=secrets.token_urlsafe(32)
    session={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            if self.path!='/bootstrap/'+token:
                self.send_error(403);return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<65536:
                self.send_error(400);return
            value=json.loads(self.rfile.read(length))
            if set(value)!={'cookie','userAgent'} or not all(isinstance(v,str) and '\r' not in v and '\n' not in v for v in value.values()):
                self.send_error(400);return
            session.update(value)
            self.send_response(200);self.end_headers();self.wfile.write(b'OK')
    server=HTTPServer(('127.0.0.1',0),Handler)
    server.timeout=120
    descriptor=ROOT/descriptor_name
    write_atomic(descriptor,{'url':f'http://127.0.0.1:{server.server_port}/bootstrap/{token}','pid':os.getpid()})
    report(state='waitingForSession')
    try:server.handle_request()
    finally:
        server.server_close();descriptor.unlink(missing_ok=True)
    if not session:raise RuntimeError('No authenticated session received within bootstrap timeout')
    return session

def capture(session, interval=1.0):
    initial=json.loads((ROOT/'database-2611-part-0001.json').read_text(encoding='utf-8'))
    index=initial['index'];expected={item['RESTRICTION']['ref']['backendId'] for item in index}
    known=set()
    for path in sorted(ROOT.glob('database-2611-part-*.json')):
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('referenceStart')!=START or data.get('branch')!='production':
            raise ValueError('Capture metadata mismatch: '+path.name)
        for record in data['records']:
            if record['data']['ref']!=record['summary']['ref']:
                raise ValueError('Capture identity mismatch: '+path.name)
            known.add(record['data']['ref']['backendId'])
    if known-expected:raise ValueError('Unexpected restriction in local fragments')
    pending=[i for i,item in enumerate(index) if item['RESTRICTION']['ref']['backendId'] not in known]
    opener=build_opener(NoRedirect())
    buffer=[];saved=len(known);done=saved;began=time.monotonic();next_request=began
    def flush():
        nonlocal saved
        if not buffer:return
        write_atomic(ROOT/f'database-2611-part-cli-{time.time_ns()}.json',{
            'schema':'eurocontrol-rs-capture/1','branch':'production','referenceStart':START,
            'capturedAt':datetime.now(timezone.utc).isoformat(),'expectedTotal':len(index),'records':buffer})
        saved+=len(buffer);buffer.clear()
    status(state='running',done=done,saved=saved,total=len(index),requestsPerSecond=1/interval)
    try:
        for ordinal in pending:
            if (ROOT/'collection.stop').exists():
                flush();status(state='stoppedByRequest',done=done,saved=saved,total=len(index));return
            if time.monotonic()-began>12*3600:
                flush();status(state='durationLimit',done=done,saved=saved,total=len(index));return
            time.sleep(max(0,next_request-time.monotonic()))
            next_request=time.monotonic()+interval
            summary=index[ordinal]['RESTRICTION'];ref=summary['ref']
            url=ORIGIN+'/api/airspace/v1/rs/'+quote(ref['restrictionId'],safe='')+'?'+urlencode({'backendId':ref['backendId'],'branchId':'production'})
            request=Request(url,headers={'Accept':'application/json','Content-Type':'application/json',
                'Cookie':session['cookie'],'User-Agent':session['userAgent']})
            with opener.open(request,timeout=60) as response:
                if response.status!=200 or 'application/json' not in response.headers.get('Content-Type',''):
                    raise RuntimeError('Non-JSON or unsuccessful response; session intervention required')
                entity=json.load(response)
            if entity.get('ref')!=ref or not isinstance(entity.get('states'),list):
                raise ValueError('Restriction identity/shape mismatch: '+ref['restrictionId'])
            buffer.append({'index':ordinal,'summary':summary,'data':entity,'fetchedAt':datetime.now(timezone.utc).isoformat()})
            done+=1
            if len(buffer)>=50:flush()
            if done%10==0:status(state='running',done=done,saved=saved,total=len(index),requestsPerSecond=1/interval)
        flush();status(state='extractionComplete',done=done,saved=saved,total=len(index))
    except Exception as error:
        flush()
        status(state='stoppedOnError',done=done,saved=saved,total=len(index),error=str(error))
        raise

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--interval',type=float,default=1.0)
    args=parser.parse_args()
    if not 0.25<=args.interval<=60:parser.error('Interval must be between 0.25 and 60 seconds')
    lock=ROOT/'collection.lock'
    try:
        with lock.open('x',encoding='ascii') as stream:stream.write(str(os.getpid()))
    except FileExistsError:raise SystemExit('Collector registered already; inspect PID before restart')
    try:capture(receive_session(),args.interval)
    finally:lock.unlink(missing_ok=True)

if __name__=='__main__':main()
