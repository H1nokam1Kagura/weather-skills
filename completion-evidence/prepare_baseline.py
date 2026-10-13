"""Preserve exact frozen baseline bytes; no case execution or oracle changes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
SOURCE = Path('C:/Users/neilha/.agentbus/aws-harness-plan/simkit')
RECOVERED = Path('C:/Users/neilha/.agentbus/simkit-v2_7-closure-20261011/simkit/cli.py')
DEST = ROOT / 'frozen-baseline'
PINS = SOURCE / 'casesets/cached-onset-e2e-v2_7/verification/FINAL-PINS.json'


def main():
    pins = json.loads(PINS.read_text())
    checked = []
    for relative, expected in pins['files'].items():
        source = RECOVERED if relative == 'simkit/cli.py' else SOURCE / relative
        raw = source.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError(f'Frozen source mismatch: {relative}')
        checked.append((relative, source, raw, actual))
    DEST.mkdir(exist_ok=False)
    records = []
    for relative, source, raw, actual in checked:
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        records.append({'relative_path': relative, 'source': str(source),
                        'sha256': actual, 'bytes': len(raw)})
    shutil.copyfile(PINS, DEST / 'FINAL-PINS.json')
    (ROOT / 'BASELINE-SELECTION.json').write_text(json.dumps({
        'selection': 'EXACT_FROZEN_V2_7_FINAL_PINS',
        'status': 'SOURCE_PACKAGE_ONLY_NOT_EXECUTED',
        'pins_manifest_sha256': hashlib.sha256(PINS.read_bytes()).hexdigest(),
        'cli_provenance': 'Recovered uncommitted file bytes; identified by frozen hash, not its source HEAD.',
        'policy_successor': 'Separate oracle-policy-1-r2 package remains unadopted and unratified.',
        'files': records,
    }, indent=2) + '\n')
    print(f'Preserved {len(records)} exact frozen files; no pins changed.')


if __name__ == '__main__':
    main()
