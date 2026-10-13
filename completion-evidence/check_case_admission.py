"""Persist frozen-case admission refusals; never execute or score a case."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / 'frozen-baseline'
sys.path[:0] = [str(BASELINE), str(ROOT.parent)]
from adapter_support.weather_adapter import WeatherAdapter
from simkit.runner import run


def main():
    pins = json.loads((BASELINE / 'FINAL-PINS.json').read_text())['files']
    for relative, expected in pins.items():
        if hashlib.sha256((BASELINE / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Baseline changed: {relative}')
    frozen = BASELINE / 'casesets/cached-onset-e2e-v2_7/frozen'
    cases = json.loads((frozen / 'agentic-e2e-injections.DEV-ONLY.json').read_text())['cases']
    adapter = WeatherAdapter()
    implemented = adapter.implemented_capabilities()
    # This command only records refusals. Runnable cases require a different,
    # explicitly prepared dispatch command and complete per-case run evidence.
    if any(adapter.can_run() and set(c['adapter_requirements']) <= implemented for c in cases):
        raise RuntimeError('Runnable cases present: use an execution driver, not admission-only check')
    directory = ROOT / 'admission-records'
    directory.mkdir(exist_ok=False)
    rows = []
    for case in cases:
        record, = run([case], adapter, profile=None)
        assert record['status'] == 'NOT_IMPLEMENTED' and record['evidence'] is None
        row = {**record, 'executed': False, 'scored': False,
               'kind': 'control' if case['id'].endswith('-CTL') else 'injection',
               'pair_id': case['id'].rsplit('-', 1)[0],
               'required_capabilities': case['adapter_requirements']}
        (directory / (case['id'] + '.json')).write_text(json.dumps(row, indent=2) + '\n')
        rows.append(row)
    assert len(rows) == 66 and len({r['pair_id'] for r in rows}) == 33
    result = {'stage': 'FROZEN_V2_7_ADMISSION_ONLY', 'gs1': 'NOT_MET',
              'implemented_capabilities': sorted(implemented), 'cases': rows,
              'counts': {'total': 66, 'pairs': 33, 'executed': 0, 'passed': 0,
                         'unsupported': 66, 'excluded': 0},
              'reason': 'No complete runnable capability slice. No evidence fabricated or scorer invoked.'}
    (ROOT / 'CAPABILITY_MATRIX.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['counts']))


if __name__ == '__main__':
    main()
