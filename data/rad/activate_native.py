"""Explicitly select the complete NM capture, retaining incomplete-rule diagnostics."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from import_database import write_json_atomic

root=Path(__file__).resolve().parent
source=root/'catalogue.database.candidate.json'
candidate=json.loads(source.read_text(encoding='utf-8'))
verification=json.loads((root/'database-final-verification.json').read_text(encoding='utf-8'))
assert verification['candidateSha256']==hashlib.sha256(source.read_bytes()).hexdigest()
assert verification['completeExtraction'] and verification['allEligibleSourceStatesPreservedUnchanged']
assert candidate['summary']['missingRestrictions']==0
candidate['allowIncompleteNative']=True
candidate['version']='NMP-2611-diagnostics'
candidate['activation']={'at':datetime.now(timezone.utc).isoformat(),
    'mode':'native-with-diagnostics','sourceCandidateSha256':verification['candidateSha256']}
target=root/'catalogue.database.active.json'
if target.exists():
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    backup=root/('catalogue.database.active.'+digest[:12]+'.backup.json')
    if not backup.exists():backup.write_bytes(target.read_bytes())
write_json_atomic(target,candidate)
write_json_atomic(root/'database-activation.json',{
    **candidate['activation'],'file':target.name,'version':candidate['version'],
    'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
    'summary':candidate['summary'],'operationallyValidated':candidate['operationallyValidated'],
    'excelFallbackFilePreserved':(root/'catalogue.json').exists()})
print(json.dumps({'activeFile':str(target),'version':candidate['version'],
    'rules':len(candidate['rules']),'pending':candidate['summary']['rulesWithPendingSemantics']}))
