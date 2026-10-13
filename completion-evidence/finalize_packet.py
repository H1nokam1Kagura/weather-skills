"""Collect local evidence without promoting component tests to case results."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    work_order = json.loads((ROOT / 'WORK-ORDER.json').read_text())
    original = json.loads((ROOT / 'SOURCE_AND_RUN_PINS.json').read_text())
    original['stage'] = 'LOCAL_IMPLEMENTATION_AND_ADMISSION_REPORT'
    original['baseline_selection'] = json.loads((ROOT / 'BASELINE-SELECTION.json').read_text())
    current = []
    paths = [ROOT.parent / r['relative_path'] for r in original['files']]
    paths += [ROOT.parent / 'tools/workflow/tests/test_dispatch_evidence.py']
    for path in paths:
        raw = path.read_bytes()
        current.append({'relative_path': path.relative_to(ROOT.parent).as_posix(),
                        'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    original['current_controller_files'] = current
    original['identity_limit'] = 'Hashes establish content integrity relative to recorded inputs, not signer authentication.'
    (ROOT / 'SOURCE_AND_RUN_PINS.json').write_text(json.dumps(original, indent=2) + '\n')
    blockers = work_order['inventory']['blockers']
    for item in blockers:
        item['owner'] = 'SIMKIT/controller implementation owner'
        if item['id'] == 'B2-PINS':
            item['status'] = 'RESOLVED_EXACT_FROZEN_PACKAGE_PRESERVED'
            item['next'] = 'Use BASELINE-SELECTION.json; no repinning or successor substitution.'
        elif item['id'] == 'B2-RECEIPTS':
            item['status'] = 'PARTIAL_NATIVE_PRODUCER_IMPLEMENTED'
            item['next'] = 'Review opt-in dispatch evidence; complete frozen receipt mapping separately. No full capability promotion.'
        elif item['id'] == 'B2-SIGNAL':
            item['next'] = 'Evaluate separately versioned containment proposal and owned feasibility canaries; original signal requirement remains unmet.'
        elif item['id'] == 'B2-DISPATCH':
            item['next'] = 'No runnable frozen slice while universal capabilities are missing. Keep run_case unavailable; do not install a refusal-only method that falsely advertises dispatch support.'
    (ROOT / 'BLOCKERS.json').write_text(json.dumps({'blockers': blockers,
        'hitl': 'No additional HITL for authorized local implementation or owned feasibility canaries. Verify existing authority before proposing any concrete frozen contract/scorer/policy change. No blanket 66-case human judgment requirement.'}, indent=2) + '\n')
    matrix = json.loads((ROOT / 'CAPABILITY_MATRIX.json').read_text())
    result = {'gs1': 'NOT_MET', 'counts': matrix['counts'],
              'reason': 'All frozen cases lack complete required capabilities; no registered controls or faults executed.',
              'scorer_invoked': False, 'frozen_pins_modified': False,
              'component_tests': 'See XML and logs; not registered SIMKIT case evidence.',
              'containment_candidate': 'Separate feasibility evidence; no frozen equivalence or ratification.'}
    (ROOT / 'GS1_REPORT.json').write_text(json.dumps(result, indent=2) + '\n')
    (ROOT / 'GS1_REPORT.md').write_text(
        '# SIMKIT completion evidence\n\n'
        'GS1 is **not met**. All 66 frozen v2.7 cases (33 fault/control pairs) remain unsupported; '
        'zero were executed, scored, passed, or excluded. Each admission disposition is preserved in '
        '`admission-records/` and `CAPABILITY_MATRIX.json`.\n\n'
        'The original 38-file controller WIP was hash-verified and preserved in an isolated feature tree. '
        'New opt-in dispatch evidence records durable attempts, native invocation correlation, sequence, '
        'role occurrence and resume phase. Unknown outcomes retain pending state and refuse resume. '
        'These native improvements do not claim full frozen receipt support.\n\n'
        'All 24 frozen baseline pins matched after recovering the exact historical CLI bytes into '
        '`frozen-baseline/`. The active policy successor and original source trees were not overwritten. '
        'See `BASELINE-SELECTION.json` for provenance.\n\n'
        'The containment-and-cleanup alternative is separately proposed in `CONTAINMENT-PLAN.json`. '
        'Its bounded canaries are already authorized. Technical review is not HITL. Any later change '
        'to frozen policy, scorer or ledger must be checked against current authority and presented '
        'as a concrete reviewed change if approval is still needed.\n\n'
        'Test XML/logs and the independent review record describe component validation. Initial '
        'sandbox testing had four termination-backend failures; the six process tests passed '
        'outside the sandbox. This does not establish exhaustive target capture.\n\n'
        'Remaining work is listed in `BLOCKERS.json`: complete receipt mapping, real dispatch, '
        'claim semantics, notification/retry/operator semantics, delivery/authentication/output, '
        'and either faithful original process evidence or separately accepted candidate semantics. '
        'No live paid calls, scientific runs, push, merge, deployment or ratification occurred.\n',
        encoding='utf-8')
    print('GS1 NOT_MET packet written; no case execution or scoring.')


if __name__ == '__main__':
    main()
