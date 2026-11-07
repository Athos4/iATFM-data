"""Rebuild the candidate/report as capture fragments arrive; never activate it."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def status(**fields):
    document={'updatedAt':datetime.now(timezone.utc).isoformat(),**fields}
    temporary=ROOT/'conversion-watch-status.tmp'
    temporary.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
    temporary.replace(ROOT/'conversion-watch-status.json')
    print(json.dumps(document,ensure_ascii=False),flush=True)

def signature():
    paths=list(ROOT.glob('database-2611-part-*.json'))+[ROOT/name for name in (
        'database-2611-dependencies.json','import_database.py','audit_local_references.py','report_integration.py')]
    return tuple(sorted((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in paths if p.exists()))

def main():
    lock=ROOT/'conversion-watch.lock'
    try:
        with lock.open('x',encoding='ascii') as stream:
            import os
            stream.write(str(os.getpid()))
    except FileExistsError:
        raise SystemExit('Conversion watcher already registered; inspect its PID before restart.')
    previous=None
    try:
        for _ in range(144):  # At most twelve hours; no unbounded service.
            if (ROOT/'conversion-watch.stop').exists():
                status(state='stoppedByRequest');return
            current=signature()
            if current!=previous:
                # A browser download may still be writing its latest fragment.
                time.sleep(3)
                if current!=signature():
                    time.sleep(5);continue
                status(state='buildingCandidate')
                for script in ('import_database.py','audit_local_references.py','report_integration.py'):
                    result=subprocess.run([sys.executable,str(ROOT/script)],cwd=ROOT.parent,
                        capture_output=True,text=True,encoding='utf-8',timeout=240)
                    if result.returncode:
                        status(state='buildFailed',script=script,error=(result.stderr or result.stdout)[-3000:]);return
                report=json.loads((ROOT/'database-integration-report.json').read_text(encoding='utf-8'))
                summary=report['summary'];complete=summary['missingRestrictions']==0
                status(state='extractionComplete' if complete else 'waitingForMoreCaptures',
                    capturedRestrictions=summary['capturedRestrictions'],expectedRestrictions=summary['expectedRestrictions'],
                    conversionPercent=report['conversionPercent'],
                    conversionThresholdReached=report['conversionPercent']>90,
                    operationallyValidated=False,activeCatalogueChanged=False)
                previous=current
                if complete:return
            time.sleep(300)
        status(state='watchDurationElapsed',operationallyValidated=False)
    finally:
        lock.unlink(missing_ok=True)

if __name__=='__main__':main()
