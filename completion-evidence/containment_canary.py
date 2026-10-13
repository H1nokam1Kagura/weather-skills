"""Opt-in Windows owned-job feasibility probe; not SIMKIT signal evidence."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CASES = ('normal', 'timeout', 'orphan', 'observer_failure', 'supervisor_crash')
KILL_ON_CLOSE = 0x2000
JOB_LIST = 0x0002000D
SUSPENDED_NO_WINDOW_EXTENDED = 0x4 | 0x08000000 | 0x00080000
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258


class BasicLimits(C.Structure):
    _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong),
                ('flags', W.DWORD), ('min_ws', C.c_size_t), ('max_ws', C.c_size_t),
                ('active_limit', W.DWORD), ('affinity', C.c_size_t),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]


class IO(C.Structure):
    _fields_ = [(n, C.c_ulonglong) for n in ('ro', 'wo', 'oo', 'rb', 'wb', 'ob')]


class ExtendedLimits(C.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IO), ('process_memory', C.c_size_t),
                ('job_memory', C.c_size_t), ('peak_process', C.c_size_t),
                ('peak_job', C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [(n, C.c_longlong) for n in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        ('faults', W.DWORD), ('total', W.DWORD), ('active', W.DWORD), ('terminated', W.DWORD)]


class Startup(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR),
                ('title', W.LPWSTR)] + [(n, W.DWORD) for n in
                ('x', 'y', 'xs', 'ys', 'xc', 'yc', 'fill', 'flags')] + [
                ('show', W.WORD), ('reserved_size', W.WORD), ('reserved_bytes', C.c_void_p),
                ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]


class StartupEx(C.Structure):
    _fields_ = [('startup', Startup), ('attributes', C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE),
                ('pid', W.DWORD), ('tid', W.DWORD)]


class Native:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Windows-only probe; no process launched')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        specs = {
            'CreateJobObjectW': ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            'SetInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            'QueryInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
            'InitializeProcThreadAttributeList': ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
            'UpdateProcThreadAttribute': ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
            'DeleteProcThreadAttributeList': ([C.c_void_p], None),
            'CreateProcessW': ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
                                C.c_void_p, W.LPCWSTR, C.c_void_p, C.POINTER(ProcessInfo)], W.BOOL),
            'IsProcessInJob': ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
            'ResumeThread': ([W.HANDLE], W.DWORD),
            'TerminateJobObject': ([W.HANDLE, W.UINT], W.BOOL),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result

    def checked(self, name, *args):
        result = getattr(self.k, name)(*args)
        if not result:
            raise C.WinError(C.get_last_error())
        return result


class Boundary:
    def __init__(self, log):
        self.n = Native()
        self.log = log
        self.job = None
        self.handles = []
        self.admission_closed = False
        try:
            self.job = self.n.checked('CreateJobObjectW', None, None)
            limits = ExtendedLimits()
            limits.basic.flags = KILL_ON_CLOSE
            self.n.checked('SetInformationJobObject', self.job, 9, C.byref(limits), C.sizeof(limits))
            observed = ExtendedLimits()
            self.n.checked('QueryInformationJobObject', self.job, 9, C.byref(observed), C.sizeof(observed), None)
            if observed.basic.flags != KILL_ON_CLOSE:
                raise RuntimeError('unexpected job limits; refuse launch')
            self.log('job_ready', flags=int(observed.basic.flags), noninheritable=True)
        except BaseException:
            if self.job:
                self.n.k.CloseHandle(self.job)
                self.job = None
            raise

    def launch(self, code):
        if self.admission_closed:
            raise RuntimeError('admission closed')
        size = C.c_size_t()
        self.n.k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
        if not size.value:
            raise RuntimeError('attribute list sizing unsupported')
        buf = C.create_string_buffer(size.value)
        self.n.checked('InitializeProcThreadAttributeList', buf, 1, 0, C.byref(size))
        try:
            jobs = (W.HANDLE * 1)(self.job)
            self.n.checked('UpdateProcThreadAttribute', buf, 0, JOB_LIST, jobs, C.sizeof(jobs), None, None)
            si = StartupEx()
            si.startup.cb = C.sizeof(si)
            si.attributes = C.cast(buf, C.c_void_p)
            pi = ProcessInfo()
            cmd = C.create_unicode_buffer(subprocess.list2cmdline([sys.executable, '-I', '-c', code]))
            self.n.checked('CreateProcessW', sys.executable, cmd, None, None, False,
                           SUSPENDED_NO_WINDOW_EXTENDED, None, None, C.byref(si), C.byref(pi))
            self.handles.extend([pi.process, pi.thread])
            member = W.BOOL()
            self.n.checked('IsProcessInJob', pi.process, self.job, C.byref(member))
            if not member.value:
                raise RuntimeError('initial association failed; refuse resume')
            self.log('admitted_suspended', pid=int(pi.pid), membership=True)
            resumed = self.n.k.ResumeThread(pi.thread)
            if resumed == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            self.log('resumed', pid=int(pi.pid), prior_suspend_count=int(resumed))
            return pi
        finally:
            self.n.k.DeleteProcThreadAttributeList(buf)

    def stop(self):
        self.admission_closed = True
        self.log('termination_request', api='TerminateJobObject')
        result = self.n.k.TerminateJobObject(self.job, 97)
        error = C.get_last_error() if not result else None
        self.log('termination_return', result=int(result), error=error)
        if not result:
            raise OSError(error, 'TerminateJobObject failed')

    def zero(self, seconds=3):
        self.admission_closed = True
        deadline = time.monotonic() + seconds
        while True:
            a = Accounting()
            self.n.checked('QueryInformationJobObject', self.job, 1, C.byref(a), C.sizeof(a), None)
            self.log('active_query', result=1, active=int(a.active), total=int(a.total))
            if a.active == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)

    def close(self):
        # Native cleanup never depends on the observation callback.
        if self.job:
            self.n.k.TerminateJobObject(self.job, 98)
        for h in self.handles:
            self.n.k.CloseHandle(h)
        if self.job:
            self.n.k.CloseHandle(self.job)
            self.job = None


def event_log(events):
    def emit(event, **fields):
        events.append(dict(seq=len(events) + 1, monotonic_ns=time.monotonic_ns(), event=event, **fields))
    return emit


def fixture(case):
    if case == 'normal':
        return 'import time; time.sleep(.1)'
    if case == 'orphan':
        return ('import subprocess,sys; subprocess.Popen([sys.executable,"-I","-c",'
                '"import time; time.sleep(4)"],creationflags=0x08000000)')
    return 'import time; time.sleep(8)'


def observer_failure(boundary, independent_log):
    """Fail the actual stop callback; independent cleanup cannot repair the gap."""
    def broken_callback(event, **fields):
        raise RuntimeError('owned observer failure at ' + event)

    boundary.log = broken_callback
    outcome = {'status': 'UNKNOWN', 'observation_integrity': 'GAP',
               'independent_zero_observed': False}
    try:
        boundary.stop()
    except RuntimeError as exc:
        outcome['observer_error'] = str(exc)
        independent_log('observer_callback_failed', error=str(exc))
    finally:
        # Do not use Boundary.stop/zero: their callback is still broken.
        boundary.admission_closed = True
        stopped = boundary.n.k.TerminateJobObject(boundary.job, 99)
        error = C.get_last_error() if not stopped else None
        independent_log('independent_cleanup_return', native_result=int(stopped), error=error)
        outcome['independent_termination_succeeded'] = bool(stopped)
        deadline = time.monotonic() + 3
        while True:
            a = Accounting()
            queried = boundary.n.k.QueryInformationJobObject(
                boundary.job, 1, C.byref(a), C.sizeof(a), None)
            independent_log('independent_active_query', native_result=int(queried),
                            active=int(a.active) if queried else None,
                            error=C.get_last_error() if not queried else None)
            if not queried or a.active == 0 or time.monotonic() >= deadline:
                outcome['independent_zero_observed'] = bool(queried and a.active == 0)
                break
            time.sleep(.05)
    return outcome


def run_case(case, out):
    events = []
    log = event_log(events)
    boundary = None
    result = dict(case=case, status='UNKNOWN', evidence=events,
                  exhaustive_signal_capture=False, scope='trusted direct-launch synthetic fixtures')
    try:
        if case == 'supervisor_crash':
            return crash_case(out, result, log)
        boundary = Boundary(log)
        pi = boundary.launch(fixture(case))
        if case in ('normal', 'orphan'):
            wait = boundary.n.k.WaitForSingleObject(pi.process, 1500)
            log('root_wait', native_result=int(wait))
            if wait != WAIT_OBJECT_0:
                raise RuntimeError('root did not exit within fixture deadline')
        else:
            time.sleep(.15)
        if case == 'orphan':
            if boundary.zero(.05):
                raise RuntimeError('orphan fixture not observed active after root exit')
        if case == 'observer_failure':
            result.update(observer_failure(boundary, log))
            return result
        if case != 'normal':
            try:
                boundary.stop()
            except OSError:
                result['status'] = 'FAILED'
                raise
        zero = boundary.zero()
        result['status'] = 'CLEAN' if zero else 'FAILED'
        result['zero_observed'] = zero
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if boundary:
            boundary.close()
    return result


def crash_helper(out):
    # Outer observer controls only this owned supervisor and the retained fixture.
    b = Boundary(lambda *a, **kw: None)
    try:
        pi = b.launch(fixture('timeout'))
        (out / 'child.json').write_text(json.dumps({'pid': int(pi.pid)}), encoding='utf-8')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (out / 'crash-now').exists():
                os._exit(73)  # Intentional crash closes sole job handle.
            time.sleep(.03)
    finally:
        b.close()


def crash_case(out, result, log):
    n = Native()
    child = None
    with (out / 'supervisor.stdout').open('wb') as stdout, (out / 'supervisor.stderr').open('wb') as stderr:
        proc = subprocess.Popen([sys.executable, '-I', __file__, '--crash-helper', str(out)],
                                stdout=stdout, stderr=stderr, creationflags=0x08000000)
        try:
            deadline = time.monotonic() + 2
            while not (out / 'child.json').exists() and time.monotonic() < deadline:
                time.sleep(.03)
            data = json.loads((out / 'child.json').read_text())
            child = n.checked('OpenProcess', 0x00100000, False, data['pid'])
            if n.k.WaitForSingleObject(child, 0) != WAIT_TIMEOUT:
                raise RuntimeError('fixture no longer alive before supervisor crash')
            log('child_retained_before_crash', pid=data['pid'])
            (out / 'crash-now').write_text('owned crash injection', encoding='utf-8')
            log('supervisor_exit', exit_code=proc.wait(timeout=4))
            wait = n.k.WaitForSingleObject(child, 4000)
            log('independent_child_wait', native_result=int(wait))
            result['independent_fixture_exit_observed'] = wait == WAIT_OBJECT_0
            result['reason'] = 'Supervisor crash: mandatory job accounting/journal unavailable; fixture exit is not boundary-zero evidence.'
            result['status'] = 'UNKNOWN'
        finally:
            if proc.poll() is None:
                proc.terminate()  # Only retained owned supervisor handle.
                proc.wait(timeout=3)
            if child:
                n.k.CloseHandle(child)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='new evidence directory (must not exist)')
    parser.add_argument('--case', choices=CASES, action='append', help='explicit case selection; repeat to serialize')
    parser.add_argument('--crash-helper', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.crash_helper:
        crash_helper(args.crash_helper)
        return 0
    if not args.out or not args.case:
        parser.error('--out and at least one explicit --case are required')
    args.out.mkdir(parents=True, exist_ok=False)
    for index, case in enumerate(args.case):
        out = args.out / (str(index) + '-' + case)
        out.mkdir()
        result = run_case(case, out)
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'case': case, 'status': result['status'], 'result': str(out / 'result.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
