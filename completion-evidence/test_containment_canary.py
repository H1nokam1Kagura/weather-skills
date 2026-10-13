"""Pure structural checks: importing/testing launches no process."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('containment_canary', Path(__file__).with_name('containment_canary.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_job_list_atomic_association_constant():
    assert probe.JOB_LIST == 0x0002000D
    assert probe.SUSPENDED_NO_WINDOW_EXTENDED & 4
    assert probe.KILL_ON_CLOSE == 0x2000


def test_callback_journal_detached_from_process_execution():
    events = []
    log = probe.event_log(events)
    log('query', active=0)
    log('error', active=None)
    assert [e['seq'] for e in events] == [1, 2]
    assert events[1]['active'] is None


def test_fixture_lifetimes_bounded_and_no_external_broker():
    for case in probe.CASES:
        compile(probe.fixture(case), '<fixture>', 'exec')
    assert 'sleep(8)' in probe.fixture('timeout')
    assert 'sleep(4)' in probe.fixture('orphan')


def test_cleanup_ignores_observer_failure():
    calls = []
    class Kernel:
        def TerminateJobObject(self, *args):
            calls.append('terminate')
        def CloseHandle(self, h):
            calls.append(('close', h))
    boundary = probe.Boundary.__new__(probe.Boundary)
    boundary.n = type('Native', (), {'k': Kernel()})()
    boundary.job, boundary.handles = 10, [20, 30]
    boundary.log = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('lost observer'))
    boundary.close()
    assert calls == ['terminate', ('close', 20), ('close', 30), ('close', 10)]


def test_real_callback_failure_keeps_unknown_after_independent_zero():
    calls = []
    class Kernel:
        def TerminateJobObject(self, *args):
            calls.append('native_terminate')
            return 1
        def QueryInformationJobObject(self, job, kind, accounting, size, returned):
            calls.append('native_query')
            accounting._obj.active = 0
            return 1
    boundary = probe.Boundary.__new__(probe.Boundary)
    boundary.n = type('Native', (), {'k': Kernel()})()
    boundary.job, boundary.handles = 10, []
    events = []
    result = probe.observer_failure(boundary, probe.event_log(events))
    assert calls == ['native_terminate', 'native_query']
    assert result['observer_error'].endswith('termination_request')
    assert result['independent_zero_observed'] is True
    assert result['status'] == 'UNKNOWN'
    assert boundary.admission_closed
    assert events[0]['event'] == 'observer_callback_failed'
    try:
        boundary.log('active_query')
    except RuntimeError:
        pass
    else:
        raise AssertionError('actual callback was not replaced')
