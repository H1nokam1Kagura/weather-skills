"""Run one explicit local check with bounded logs and source identity."""
import datetime
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
label, *command = sys.argv[1:]
if not label.replace('-', '').isalnum() or not command:
    raise SystemExit('usage: run_local_check.py LABEL executable [arguments...]')
receipt = root / (label + '.receipt.json')
if receipt.exists():
    raise SystemExit('Refusing to overwrite an earlier receipt; choose a new label')
paths = [root.parent / 'tools/workflow/runner.py', root.parent / 'tools/workflow/agents.py',
         root / 'containment_canary.py']
pins = {str(p.relative_to(root.parent)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
record = {'command': command, 'cwd': str(Path.cwd()), 'platform': platform.platform(),
          'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'source_pins': pins, 'kind': 'LOCAL_COMPONENT_CHECK_NOT_REGISTERED_CASE',
          'status': 'STARTED_OUTCOME_UNKNOWN'}
receipt.write_text(json.dumps(record, indent=2) + '\n')
started = time.monotonic()
with (root / (label + '.stdout.log')).open('wb') as stdout, (root / (label + '.stderr.log')).open('wb') as stderr:
    result = subprocess.run(command, stdout=stdout, stderr=stderr, check=False)
record.update(status='COMPLETED', exit_code=result.returncode, elapsed_seconds=time.monotonic()-started,
              source_unchanged=all(hashlib.sha256((root.parent / p).read_bytes()).hexdigest() == h for p,h in pins.items()))
receipt.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({'receipt': str(receipt), 'exit_code': result.returncode, 'source_unchanged': record['source_unchanged']}))
raise SystemExit(result.returncode)
