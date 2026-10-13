"""Write confinement + run-directory creation capture for a controller tree (Windows, non-admin).

Feasibility prototype for CAP-EXHAUSTIVE-WRITES. It is NOT wired into the controller and NOT
advertised as a capability. The approach is "exhaustive by confinement" rather than "exhaustive by
tracing", because kernel file tracing (ETW Kernel-File, Process Monitor) needs elevation this host
does not grant.

Mechanism ``confined`` (the prototype):

1. The tree runs under a copy of the caller's own token that is (a) lowered to Low integrity and
   (b) WRITE-RESTRICTED to {logon SID, Everyone}, with privileges stripped, in a kill-on-close job.
   It inherits only stdin/stdout/stderr, through an explicit handle list. A write-type open must
   pass THREE independent kernel checks:
     - Mandatory integrity (no-write-up). The object must carry an explicit Low (or lower) label.
       Unlabelled objects count as Medium, so most of the disk fails here.
     - The normal DACL check for the user.
     - The restricted DACL check. Some ACE must grant write to the logon SID or Everyone. That
       rules out Low-labelled locations such as AppData\\LocalLow, which grant the user only.
   The harness gives exactly one directory, the run dir, a Low label plus a logon-SID ACE.
   Grandchildren inherit the token and the job.
2. A recursive ReadDirectoryChangesExW watcher on the run dir is armed before launch and drained
   after the job is empty. It records every name created there, including files created then
   deleted and rename targets. A buffer overflow (the OS dropped a batch) makes the record
   non-exhaustive.
3. ``audit_create_access`` asks the kernel (``AccessCheck`` with the very same token) whether the
   tree could create a file or directory anywhere in a declared directory scope. It returns every
   directory where the answer is yes, plus every directory it could not read. Exhaustiveness is
   claimed only for that audited scope. It is not proved for the whole machine: the precondition
   "no other directory is Low-labelled AND grants Everyone/logon write" is a host property, checked
   rather than constructed. It is FALSE on this host. The audit found
   C:\\ProgramData\\Microsoft\\DeviceSync and C:\\Users\\Public\\Documents\\AdobeGCInfo (Low label +
   Everyone full control), and a confined child really writes there. Such directories can be
   passed as ``shared_watch_roots``: they are watched too and their creations kept, but other
   processes write there as well, so ``attribution_exact`` becomes False.
4. ``release`` withdraws the label and ACE after a run. The logon SID outlives the run, so a stale
   run dir would otherwise stay writable by every later confined tree in the same logon session.

``exhaustive`` is True only when every link was verified for this run. Any gap makes it False:
token facts differ from what was requested, a watcher was not armed or overflowed or errored, the
tree was not drained, an 8.3 name could not be resolved, or the precondition audit is missing,
incomplete, or found a creatable directory that is not watched. A failed observer yields
``created_paths=None`` (missing evidence), never [].

Observed on this host, so not obvious (see completion-evidence/write-capture-canary):
  - A fresh random restricting SID is IGNORED for access grants. Only SIDs that are also enabled
    groups of the token take effect, so "grant a never-before-seen SID on the run dir" cannot work
    without SeCreateToken (admin).
  - With {logon SID} alone, python.exe, claude.exe and powershell fail with
    STATUS_DLL_INIT_FAILED or CLR E_ACCESSDENIED. Everyone is also required. Untrusted integrity
    breaks every binary tried, including cmd.exe.

Residual scope, stated rather than hidden: writes made on the tree's behalf by processes OUTSIDE
the tree (COM/RPC brokers, services, Task Scheduler) are not writes by the tree and are not seen.
Creating a named stream on a pre-existing writable file is a modification, not a directory entry.
"""

from __future__ import annotations

import ctypes as C
import msvcrt
import os
import re
import secrets
import subprocess
import threading
import time
from ctypes import wintypes as W
from pathlib import Path

# token / process constants
TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_QUERY = 0x1, 0x2, 0x8
TOKEN_ADJUST_DEFAULT = 0x80
MAXIMUM_ALLOWED = 0x02000000
DISABLE_MAX_PRIVILEGE, WRITE_RESTRICTED = 0x1, 0x8
TOKEN_GROUPS, TOKEN_DEFAULT_DACL, TOKEN_RESTRICTED_SIDS = 2, 6, 11
TOKEN_ELEVATION_TYPE, TOKEN_ELEVATION, TOKEN_INTEGRITY_LEVEL = 18, 20, 25
SE_GROUP_INTEGRITY, SE_GROUP_LOGON_ID = 0x20, 0xC0000000
SECURITY_IDENTIFICATION, TOKEN_PRIMARY, TOKEN_IMPERSONATION = 1, 1, 2
LOW_IL, UNTRUSTED_IL, MEDIUM_IL = "S-1-16-4096", "S-1-16-0", "S-1-16-8192"
EVERYONE = "S-1-1-0"
GENERIC_ALL = 0x10000000
CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT = 0x4, 0x400
EXTENDED_STARTUPINFO_PRESENT, CREATE_NO_WINDOW = 0x80000, 0x08000000
STARTF_USESTDHANDLES = 0x100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x20002, 0x2000D
KILL_ON_CLOSE = 0x2000
JOB_BASIC_ACCOUNTING, JOB_PROCESS_ID_LIST, JOB_EXTENDED_LIMITS = 1, 3, 9
WAIT_OBJECT_0 = 0

# security descriptor constants
SE_FILE_OBJECT = 1
OWNER_SI, GROUP_SI, DACL_SI, LABEL_SI = 0x1, 0x2, 0x4, 0x10
FILE_ALL_ACCESS = 0x1F01FF
FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY = 0x2, 0x4
FILE_GENERIC = (0x120089, 0x120116, 0x1200A0, FILE_ALL_ACCESS)  # read, write, execute, all
GRANT_ACCESS, REVOKE_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT, TRUSTEE_IS_SID = 1, 4, 3, 0

# directory watch constants
FILE_LIST_DIRECTORY = 0x1
FILE_SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OVERLAPPED = 0x02000000, 0x40000000
NOTIFY_FILE_NAME, NOTIFY_DIR_NAME = 0x1, 0x2
NOTIFY_EXTENDED_INFORMATION = 2
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT = 0x10, 0x400
ERROR_NOTIFY_ENUM_DIR = 1022
INVALID_HANDLE = W.HANDLE(-1).value
ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed_old", 5: "renamed_new"}
SHORT_NAME = re.compile(r"^[^.~]{1,6}~\d{1,6}(\.[^.]{0,3})?$")


class SidAndAttributes(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("attributes", W.DWORD)]


class Trustee(C.Structure):
    _fields_ = [
        ("multiple", C.c_void_p),
        ("multiple_op", C.c_int),
        ("form", C.c_int),
        ("type", C.c_int),
        ("name", C.c_void_p),
    ]


class ExplicitAccess(C.Structure):
    _fields_ = [
        ("access", W.DWORD),
        ("mode", C.c_int),
        ("inheritance", W.DWORD),
        ("trustee", Trustee),
    ]


class GenericMapping(C.Structure):
    _fields_ = [(n, W.DWORD) for n in ("read", "write", "execute", "all")]


class Startup(C.Structure):
    _fields_ = (
        [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR)]
        + [(n, W.DWORD) for n in ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")]
        + [
            ("show", W.WORD),
            ("reserved_size", W.WORD),
            ("reserved_bytes", C.c_void_p),
            ("stdin", W.HANDLE),
            ("stdout", W.HANDLE),
            ("stderr", W.HANDLE),
        ]
    )


class StartupEx(C.Structure):
    _fields_ = [("startup", Startup), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


class BasicLimits(C.Structure):
    _fields_ = [
        ("process_time", C.c_longlong),
        ("job_time", C.c_longlong),
        ("flags", W.DWORD),
        ("min_ws", C.c_size_t),
        ("max_ws", C.c_size_t),
        ("active_limit", W.DWORD),
        ("affinity", C.c_size_t),
        ("priority", W.DWORD),
        ("scheduling", W.DWORD),
    ]


class ExtendedLimits(C.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", C.c_ulonglong * 6),
        ("process_memory", C.c_size_t),
        ("job_memory", C.c_size_t),
        ("peak_process", C.c_size_t),
        ("peak_job", C.c_size_t),
    ]


class Accounting(C.Structure):
    _fields_ = [(n, C.c_longlong) for n in ("user", "kernel", "period_user", "period_kernel")] + [
        ("faults", W.DWORD),
        ("total", W.DWORD),
        ("active", W.DWORD),
        ("terminated", W.DWORD),
    ]


class Overlapped(C.Structure):
    _fields_ = [
        ("internal", C.c_size_t),
        ("internal_high", C.c_size_t),
        ("offset", W.DWORD),
        ("offset_high", W.DWORD),
        ("event", W.HANDLE),
    ]


def _bind(dll, specs):
    lib = C.WinDLL(dll, use_last_error=True)
    for name, (args, result) in specs.items():
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes, fn.restype = args, result
    return lib


_V, _D, _H, _B = C.c_void_p, W.DWORD, W.HANDLE, W.BOOL
_k = _bind(
    "kernel32",
    {
        "GetCurrentProcess": ([], _H),
        "CloseHandle": ([_H], _B),
        "CreateFileW": ([W.LPCWSTR, _D, _D, _V, _D, _D, _H], _H),
        "CreateEventW": ([_V, _B, _B, W.LPCWSTR], _H),
        "ResetEvent": ([_H], _B),
        "ReadDirectoryChangesW": ([_H, _V, _D, _B, _D, _V, _V, _V], _B),
        "ReadDirectoryChangesExW": ([_H, _V, _D, _B, _D, _V, _V, _V, C.c_int], _B),
        "GetOverlappedResult": ([_H, _V, C.POINTER(_D), _B], _B),
        "CancelIoEx": ([_H, _V], _B),
        "WaitForSingleObject": ([_H, _D], _D),
        "GetLongPathNameW": ([W.LPCWSTR, W.LPWSTR, _D], _D),
        "GetShortPathNameW": ([W.LPCWSTR, W.LPWSTR, _D], _D),
        "CreateJobObjectW": ([_V, W.LPCWSTR], _H),
        "SetInformationJobObject": ([_H, C.c_int, _V, _D], _B),
        "QueryInformationJobObject": ([_H, C.c_int, _V, _D, _V], _B),
        "TerminateJobObject": ([_H, W.UINT], _B),
        "InitializeProcThreadAttributeList": ([_V, _D, _D, C.POINTER(C.c_size_t)], _B),
        "UpdateProcThreadAttribute": ([_V, _D, C.c_size_t, _V, C.c_size_t, _V, _V], _B),
        "DeleteProcThreadAttributeList": ([_V], None),
        "ResumeThread": ([_H], _D),
        "GetExitCodeProcess": ([_H, C.POINTER(_D)], _B),
        "LocalFree": ([_V], _V),
    },
)
_a = _bind(
    "advapi32",
    {
        "OpenProcessToken": ([_H, _D, C.POINTER(_H)], _B),
        "CreateRestrictedToken": ([_H, _D, _D, _V, _D, _V, _D, _V, C.POINTER(_H)], _B),
        "DuplicateTokenEx": ([_H, _D, _V, C.c_int, C.c_int, C.POINTER(_H)], _B),
        "SetTokenInformation": ([_H, C.c_int, _V, _D], _B),
        "GetTokenInformation": ([_H, C.c_int, _V, _D, C.POINTER(_D)], _B),
        "IsTokenRestricted": ([_H], _B),
        "ConvertStringSidToSidW": ([W.LPCWSTR, C.POINTER(_V)], _B),
        "ConvertSidToStringSidW": ([_V, C.POINTER(W.LPWSTR)], _B),
        "GetLengthSid": ([_V], _D),
        "GetNamedSecurityInfoW": ([W.LPCWSTR, C.c_int, _D, _V, _V, _V, _V, _V], _D),
        "SetNamedSecurityInfoW": ([W.LPWSTR, C.c_int, _D, _V, _V, _V, _V], _D),
        "SetEntriesInAclW": ([W.ULONG, _V, _V, C.POINTER(_V)], _D),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
            [_V, _D, _D, C.POINTER(W.LPWSTR), _V],
            _B,
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [W.LPCWSTR, _D, C.POINTER(_V), _V],
            _B,
        ),
        "GetSecurityDescriptorSacl": ([_V, C.POINTER(_B), C.POINTER(_V), C.POINTER(_B)], _B),
        "AccessCheck": (
            [_V, _H, _D, _V, _V, C.POINTER(_D), C.POINTER(_D), C.POINTER(_B)],
            _B,
        ),
        "CreateProcessAsUserW": (
            [_H, W.LPCWSTR, W.LPWSTR, _V, _V, _B, _D, _V, W.LPCWSTR, _V, C.POINTER(ProcessInfo)],
            _B,
        ),
    },
)


def _check(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


def _check_err(err):
    if err:
        raise C.WinError(err)


# ---------------------------------------------------------------- SIDs and tokens


def _sid_from_string(text):
    sid = C.c_void_p()
    _check(_a.ConvertStringSidToSidW(text, C.byref(sid)))
    return sid  # caller LocalFree()s


def _sid_to_string(sid):
    out = W.LPWSTR()
    _check(_a.ConvertSidToStringSidW(sid, C.byref(out)))
    try:
        return out.value
    finally:
        _k.LocalFree(out)


def _token_info(token, cls):
    size = W.DWORD()
    _a.GetTokenInformation(token, cls, None, 0, C.byref(size))
    if not size.value:
        raise C.WinError(C.get_last_error())
    buf = (C.c_ubyte * size.value)()
    _check(_a.GetTokenInformation(token, cls, buf, size, C.byref(size)))
    return buf


def _token_groups(token, cls):
    """[(sid_string, attributes)] from a TOKEN_GROUPS-shaped class."""
    buf = _token_info(token, cls)
    count = W.DWORD.from_buffer(buf).value
    if not count:
        return []
    offset = C.sizeof(C.c_void_p)  # DWORD count, padded to pointer alignment
    arr = (SidAndAttributes * count).from_address(C.addressof(buf) + offset)
    return [(_sid_to_string(g.sid), int(g.attributes)) for g in arr]


def _own_token(access=TOKEN_QUERY):
    token = W.HANDLE()
    _check(_a.OpenProcessToken(_k.GetCurrentProcess(), access, C.byref(token)))
    return token


def logon_sid(token=None):
    own = token is None
    token = _own_token() if own else token
    try:
        found = [
            s
            for s, a in _token_groups(token, TOKEN_GROUPS)
            if a & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID
        ]
        if len(found) != 1:
            raise OSError(f"expected exactly one logon SID, found {found}")
        return found[0]
    finally:
        if own:
            _k.CloseHandle(token)


def token_facts(token=None):
    """Elevation / integrity / restriction facts for ``token`` (default: this process)."""
    own = token is None
    token = _own_token() if own else token
    try:
        elevated = W.DWORD.from_buffer(_token_info(token, TOKEN_ELEVATION)).value
        etype = W.DWORD.from_buffer(_token_info(token, TOKEN_ELEVATION_TYPE)).value
        label = SidAndAttributes.from_buffer(_token_info(token, TOKEN_INTEGRITY_LEVEL))
        groups = [s for s, _ in _token_groups(token, TOKEN_GROUPS)]
        return {
            "elevated": bool(elevated),
            "elevation_type": {1: "default", 2: "full", 3: "limited"}.get(etype, etype),
            "integrity_sid": _sid_to_string(label.sid),
            "is_restricted": bool(_a.IsTokenRestricted(token)),
            "restricting_sids": [s for s, _ in _token_groups(token, TOKEN_RESTRICTED_SIDS)],
            "admin_group_present": "S-1-5-32-544" in groups,
            "perf_log_users_present": "S-1-5-32-559" in groups,
        }
    finally:
        if own:
            _k.CloseHandle(token)


def new_run_sid():
    """A fresh SID no pre-existing ACE can name. (Ignored as a restricting SID: see module doc.)"""
    parts = [secrets.randbits(32) for _ in range(3)] + [1000 + secrets.randbits(20)]
    return "S-1-5-21-" + "-".join(str(p) for p in parts)


def _set_token_integrity(token, il_sid):
    sid = _sid_from_string(il_sid)
    try:
        label = SidAndAttributes(sid, SE_GROUP_INTEGRITY)
        size = C.sizeof(label) + _a.GetLengthSid(sid)
        _check(_a.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, C.byref(label), size))
    finally:
        _k.LocalFree(sid)


def _add_to_default_dacl(token, sid_text):
    """Let tree processes open their OWN kernel objects under a write-restricted token.

    Objects a process creates without an inherited ACL get the token's default DACL; under a
    write-restricted token each write-type open of them is also checked against the restricting
    SIDs. Files are unaffected: they inherit the ACL of the directory they are created in.
    """
    cur = _token_info(token, TOKEN_DEFAULT_DACL)
    old_acl = C.c_void_p.from_buffer(cur).value
    sid = _sid_from_string(sid_text)
    new_acl = C.c_void_p()
    try:
        ea = ExplicitAccess(
            access=GENERIC_ALL,
            mode=GRANT_ACCESS,
            inheritance=0,
            trustee=Trustee(form=TRUSTEE_IS_SID, name=sid),
        )
        _check_err(_a.SetEntriesInAclW(1, C.byref(ea), old_acl, C.byref(new_acl)))
        holder = C.c_void_p(new_acl.value)
        _check(_a.SetTokenInformation(token, TOKEN_DEFAULT_DACL, C.byref(holder), C.sizeof(holder)))
    finally:
        if new_acl:
            _k.LocalFree(new_acl)
        _k.LocalFree(sid)


def make_token(restricting=(), integrity=None):
    """Primary token for the tree: own token, optionally write-restricted and/or IL-lowered."""
    base = _own_token(TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT)
    new = W.HANDLE()
    try:
        if restricting:
            sids = [_sid_from_string(s) for s in restricting]
            try:
                arr = (SidAndAttributes * len(sids))(*(SidAndAttributes(s, 0) for s in sids))
                flags = DISABLE_MAX_PRIVILEGE | WRITE_RESTRICTED
                _check(
                    _a.CreateRestrictedToken(
                        base, flags, 0, None, 0, None, len(sids), arr, C.byref(new)
                    )
                )
            finally:
                for s in sids:
                    _k.LocalFree(s)
        else:
            _check(_a.DuplicateTokenEx(base, MAXIMUM_ALLOWED, None, 2, TOKEN_PRIMARY, C.byref(new)))
        try:
            for s in restricting:
                if s != EVERYONE:
                    _add_to_default_dacl(new, s)
            if integrity:
                _set_token_integrity(new, integrity)
        except BaseException:
            _k.CloseHandle(new)
            raise
        return new
    finally:
        _k.CloseHandle(base)


# ---------------------------------------------------------------- file security


def security_sddl(path, info=DACL_SI | LABEL_SI):
    sd = C.c_void_p()
    _check_err(
        _a.GetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, info, None, None, None, None, C.byref(sd)
        )
    )
    try:
        text = W.LPWSTR()
        _check(
            _a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, 1, info, C.byref(text), None
            )
        )
        try:
            return text.value
        finally:
            _k.LocalFree(text)
    finally:
        _k.LocalFree(sd)


def grant_sid(path, sid_text, access=FILE_ALL_ACCESS):
    """Add an inheritable (OI)(CI) allow ACE (native: icacls refuses unresolvable SIDs)."""
    sid = _sid_from_string(sid_text)
    old_dacl, sd, new_dacl = C.c_void_p(), C.c_void_p(), C.c_void_p()
    try:
        _check_err(
            _a.GetNamedSecurityInfoW(
                str(path),
                SE_FILE_OBJECT,
                DACL_SI,
                None,
                None,
                C.byref(old_dacl),
                None,
                C.byref(sd),
            )
        )
        ea = ExplicitAccess(
            access=access,
            mode=GRANT_ACCESS,
            inheritance=SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            trustee=Trustee(form=TRUSTEE_IS_SID, name=sid),
        )
        _check_err(_a.SetEntriesInAclW(1, C.byref(ea), old_dacl, C.byref(new_dacl)))
        _check_err(
            _a.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SI, None, None, new_dacl, None)
        )
    finally:
        for p in (new_dacl, sd, sid):
            if p:
                _k.LocalFree(p)


def set_mandatory_label(path, il_sid):
    """Set an inheritable no-write-up label on ``path`` (an owner may lower a label)."""
    sd = C.c_void_p()
    _check(
        _a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"S:(ML;OICI;NW;;;{il_sid})", 1, C.byref(sd), None
        )
    )
    try:
        present, sacl, defaulted = W.BOOL(), C.c_void_p(), W.BOOL()
        _check(
            _a.GetSecurityDescriptorSacl(sd, C.byref(present), C.byref(sacl), C.byref(defaulted))
        )
        _check_err(
            _a.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SI, None, None, None, sacl)
        )
    finally:
        _k.LocalFree(sd)


def _impersonation_copy(token):
    imp = W.HANDLE()
    _check(
        _a.DuplicateTokenEx(
            token,
            MAXIMUM_ALLOWED,
            None,
            SECURITY_IDENTIFICATION,
            TOKEN_IMPERSONATION,
            C.byref(imp),
        )
    )
    return imp


def can_create(path, imp_token):
    """Kernel AccessCheck: may ``imp_token`` create a file or subdirectory in ``path``?

    True/False, or None when the directory's security descriptor cannot be read.
    """
    sd = C.c_void_p()
    info = OWNER_SI | GROUP_SI | DACL_SI | LABEL_SI
    if _a.GetNamedSecurityInfoW(
        str(path), SE_FILE_OBJECT, info, None, None, None, None, C.byref(sd)
    ):
        return None
    try:
        mapping = GenericMapping(*FILE_GENERIC)
        privs = (C.c_ubyte * 256)()
        plen, granted, status = W.DWORD(256), W.DWORD(), W.BOOL()
        ok = _a.AccessCheck(
            sd,
            imp_token,
            MAXIMUM_ALLOWED,
            C.byref(mapping),
            privs,
            C.byref(plen),
            C.byref(granted),
            C.byref(status),
        )
        if not ok:
            return None
        return bool(status.value) and bool(granted.value & (FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY))
    finally:
        _k.LocalFree(sd)


def audit_create_access(token, roots, allowed=(), max_dirs=None):
    """Every directory under ``roots`` where ``token`` could create an entry (kernel AccessCheck).

    Reparse points are not descended (their targets are audited where they lie inside ``roots``).
    ``complete`` is False if any directory could not be read or enumerated, or ``max_dirs`` hit.
    """
    imp = _impersonation_copy(token)
    allowed_n = [os.path.normcase(os.path.abspath(a)) for a in allowed]
    creatable, unreadable, scanned, truncated = [], [], 0, False
    started = time.monotonic()
    missing = [str(r) for r in roots if not Path(r).exists()]
    try:
        stack = [str(Path(r)) for r in roots if Path(r).exists()]
        while stack:
            d = stack.pop()
            scanned += 1
            if max_dirs and scanned > max_dirs:
                truncated = True
                break
            verdict = can_create(d, imp)
            if verdict is None:
                unreadable.append(d)
            elif verdict and not any(_is_under(d, a) for a in allowed_n):
                creatable.append(d)
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            attrs = e.stat(follow_symlinks=False).st_file_attributes
                        except OSError:
                            continue
                        if attrs & FILE_ATTRIBUTE_DIRECTORY and not (
                            attrs & FILE_ATTRIBUTE_REPARSE_POINT
                        ):
                            stack.append(e.path)
            except OSError:
                if not unreadable or unreadable[-1] != d:
                    unreadable.append(d)
    finally:
        _k.CloseHandle(imp)
    return {
        "roots": [str(r) for r in roots],
        "missing_roots": missing,
        "allowed": [str(a) for a in allowed],
        "dirs_scanned": scanned,
        "creatable_outside_allowed": creatable,
        "unreadable": unreadable,
        "truncated": truncated,
        "seconds": round(time.monotonic() - started, 2),
        "complete": not unreadable and not truncated,
        "clean": not creatable,
    }


# ---------------------------------------------------------------- watcher


def _long_path(path):
    buf = C.create_unicode_buffer(32768)
    n = _k.GetLongPathNameW(path, buf, 32768)
    return buf.value if 0 < n < 32768 else None


def short_names_enabled(directory):
    """Empirical 8.3 check: does a long name in ``directory`` get a distinct short alias?"""
    probe = Path(directory) / ("long-name-probe-" + secrets.token_hex(6) + ".textfile")
    probe.write_bytes(b"")
    try:
        buf = C.create_unicode_buffer(32768)
        n = _k.GetShortPathNameW(str(probe), buf, 32768)
        return bool(n) and Path(buf.value).name.lower() != probe.name.lower()
    finally:
        probe.unlink()


class RunDirWatcher:
    """Recursive ReadDirectoryChangesExW watcher; records names, flags overflow and errors."""

    def __init__(self, root, buffer_bytes=65536, settle=0.3, read_delay=0.0):
        self.root = str(Path(root))
        self.buffer_bytes = buffer_bytes
        self.settle = settle
        self.read_delay = read_delay  # test hook: provoke kernel-buffer overflow
        self.events = []
        self.overflow = False
        self.error = None
        self.extended = hasattr(_k, "ReadDirectoryChangesExW")
        self.armed = threading.Event()
        self.started = False
        self._stop = threading.Event()
        self._thread = None
        self._handle = None

    def start(self, timeout=5):
        self.started = True
        self._thread = threading.Thread(target=self._run, name="rdcw-watcher", daemon=True)
        self._thread.start()
        if not self.armed.wait(timeout) and self.error is None:
            self.error = "watcher did not arm within timeout"
        return self.armed.is_set() and self.error is None

    def stop(self, timeout=30):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive() and self.error is None:
                self.error = "watcher did not drain within timeout"

    def _issue(self, buf, ov):
        flags = NOTIFY_FILE_NAME | NOTIFY_DIR_NAME
        if self.extended:
            return _k.ReadDirectoryChangesExW(
                self._handle,
                buf,
                C.sizeof(buf),
                True,
                flags,
                None,
                C.byref(ov),
                None,
                NOTIFY_EXTENDED_INFORMATION,
            )
        return _k.ReadDirectoryChangesW(
            self._handle, buf, C.sizeof(buf), True, flags, None, C.byref(ov), None
        )

    def _parse(self, buf, n):
        base, off = C.addressof(buf), 0
        while off < n:
            nxt = W.DWORD.from_address(base + off).value
            action = W.DWORD.from_address(base + off + 4).value
            if self.extended:  # FILE_NOTIFY_EXTENDED_INFORMATION
                attrs = W.DWORD.from_address(base + off + 56).value
                file_id = C.c_longlong.from_address(base + off + 64).value
                nlen = W.DWORD.from_address(base + off + 80).value
                name = C.wstring_at(base + off + 84, nlen // 2)
                is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
            else:  # FILE_NOTIFY_INFORMATION
                nlen = W.DWORD.from_address(base + off + 8).value
                name = C.wstring_at(base + off + 12, nlen // 2)
                file_id, is_dir = None, None
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "t_ns": time.monotonic_ns(),
                    "action": ACTIONS.get(action, action),
                    "path": os.path.join(self.root, name),
                    "is_dir": is_dir,
                    "file_id": file_id,
                }
            )
            if not nxt:
                break
            off += nxt

    def _run(self):
        self._handle = _k.CreateFileW(
            self.root,
            FILE_LIST_DIRECTORY,
            FILE_SHARE_ALL,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None,
        )
        if self._handle in (None, 0, INVALID_HANDLE):
            self.error = f"open failed: WinError {C.get_last_error()}"
            self._handle = None
            return
        event = _k.CreateEventW(None, True, False, None)
        buf = (C.c_ulonglong * (self.buffer_bytes // 8))()
        quiet_since = None
        try:
            while True:
                ov = Overlapped(event=event)
                _k.ResetEvent(event)
                if not self._issue(buf, ov):
                    self.error = f"ReadDirectoryChanges failed: WinError {C.get_last_error()}"
                    return
                self.armed.set()
                while True:
                    if _k.WaitForSingleObject(event, 50) == WAIT_OBJECT_0:
                        n = W.DWORD()
                        if not _k.GetOverlappedResult(self._handle, C.byref(ov), C.byref(n), False):
                            err = C.get_last_error()
                            if err != ERROR_NOTIFY_ENUM_DIR:
                                self.error = f"GetOverlappedResult failed: WinError {err}"
                                return
                            self.overflow = True
                        elif n.value == 0:
                            self.overflow = True  # the OS discarded a batch: record incomplete
                        else:
                            self._parse(buf, n.value)
                        quiet_since = None
                        if self.read_delay:
                            time.sleep(self.read_delay)
                        break
                    if self._stop.is_set():
                        quiet_since = quiet_since or time.monotonic()
                        if time.monotonic() - quiet_since >= self.settle:
                            _k.CancelIoEx(self._handle, C.byref(ov))
                            n = W.DWORD()
                            _k.GetOverlappedResult(self._handle, C.byref(ov), C.byref(n), True)
                            return
        except BaseException as exc:  # observer failure is missing evidence, never []
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            _k.CloseHandle(event)
            _k.CloseHandle(self._handle)


def summarize(events, root):
    """created_paths (files incl. deleted + rename targets), created_dirs, deleted, ambiguous."""
    created, dirs, deleted, ambiguous, seen = [], [], [], [], set()
    removed = {e["path"] for e in events if e["action"] in ("removed", "renamed_old")}
    for e in events:
        if e["action"] not in ("added", "renamed_new"):
            continue
        path = e["path"]
        if any(SHORT_NAME.match(part) for part in Path(os.path.relpath(path, root)).parts):
            resolved = _long_path(path)
            if resolved is None:
                ambiguous.append(path)
            else:
                path = resolved
        if path in seen:
            continue
        seen.add(path)
        (dirs if e["is_dir"] else created).append(path)
        if e["path"] in removed:
            deleted.append(path)
    return created, dirs, deleted, ambiguous


# ---------------------------------------------------------------- environment


def confined_environment(run_dir, base=None):
    """Point profile/temp variables into the run dir so env-honouring tools write there."""
    env = dict(os.environ if base is None else base)
    root = Path(run_dir)
    homes = {
        "TEMP": "tmp",
        "TMP": "tmp",
        "TMPDIR": "tmp",
        "APPDATA": "profile/AppData/Roaming",
        "LOCALAPPDATA": "profile/AppData/Local",
        "USERPROFILE": "profile",
        "HOME": "profile",
        "XDG_CACHE_HOME": "profile/.cache",
        "MPLCONFIGDIR": "profile/.matplotlib",
    }
    for key, rel in homes.items():
        (root / rel).mkdir(parents=True, exist_ok=True)
        env[key] = str(root / rel)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _env_block(env):
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    return C.create_unicode_buffer("".join(f"{k}={v}\0" for k, v in items) + "\0")


# ---------------------------------------------------------------- launcher


def _inheritable(f):
    h = msvcrt.get_osfhandle(f.fileno())
    os.set_handle_inheritable(h, True)
    return h


def _is_under(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _job_pids(job):
    # JOBOBJECT_BASIC_PROCESS_ID_LIST: DWORD assigned, DWORD in_list, ULONG_PTR ids[]
    buf = (C.c_size_t * 257)()
    if not _k.QueryInformationJobObject(job, JOB_PROCESS_ID_LIST, buf, C.sizeof(buf), None):
        return []
    count = W.DWORD.from_buffer(buf, 4).value
    return [int(buf[1 + i]) for i in range(min(count, 256))]


# label: mandatory label applied to the run dir; grant: which SID gets an (OI)(CI) ACE on it;
# restricting: write-restricting SIDs ("logon" / "fresh" resolved per run); integrity: token IL.
MODE_SPECS = {
    "confined": dict(
        label=LOW_IL, grant="logon", restricting=("logon", EVERYONE), integrity=LOW_IL
    ),
    "confined_logon_only": dict(
        label=LOW_IL, grant="logon", restricting=("logon",), integrity=LOW_IL
    ),
    "fresh_sid_write_restricted": dict(
        label=None, grant="fresh", restricting=("fresh",), integrity=None
    ),
    "untrusted_integrity": dict(
        label=UNTRUSTED_IL, grant=None, restricting=(), integrity=UNTRUSTED_IL
    ),
    "low_integrity": dict(label=LOW_IL, grant=None, restricting=(), integrity=LOW_IL),
    "none": dict(label=None, grant=None, restricting=(), integrity=None),
}

MODES = tuple(MODE_SPECS)


def _resolve(symbol, lsid, fresh):
    return {"logon": lsid, "fresh": fresh}.get(symbol, symbol)


def mode_token(mode):
    """The tree token for ``mode`` without touching any directory (used by audits)."""
    spec, lsid, fresh = MODE_SPECS[mode], logon_sid(), new_run_sid()
    sids = [_resolve(s, lsid, fresh) for s in spec["restricting"]]
    return make_token(sids, spec["integrity"])


def prepare(mode, run_dir):
    """Apply the run-dir security for ``mode`` and build the tree token. Returns (token, facts)."""
    if mode not in MODE_SPECS:
        raise ValueError(f"mode must be one of {MODES}")
    spec, lsid, fresh = MODE_SPECS[mode], logon_sid(), new_run_sid()
    if spec["label"]:
        set_mandatory_label(run_dir, spec["label"])
    if spec["grant"]:
        grant_sid(run_dir, _resolve(spec["grant"], lsid, fresh))
    sids = [_resolve(s, lsid, fresh) for s in spec["restricting"]]
    token = make_token(sids, spec["integrity"])
    facts = {
        "mode": mode,
        "expected": {"integrity_sid": spec["integrity"] or MEDIUM_IL, "restricting_sids": sids},
    }
    try:
        facts["run_dir_security"] = security_sddl(run_dir)
        facts["token"] = token_facts(token)
        exp = facts["expected"]
        facts["token_matches_expected"] = all(
            facts["token"][k] == v for k, v in exp.items()
        ) and facts["token"]["is_restricted"] == bool(exp["restricting_sids"])
    except BaseException:
        _k.CloseHandle(token)
        raise
    return token, facts


def release(run_dir):
    """Withdraw confinement grants from a finished run dir and prove nothing stays creatable.

    The logon SID lives for the whole logon session, so a run dir that keeps its Low label and
    logon ACE stays writable by every LATER confined tree. Relabel Medium, revoke the logon ACE
    (inherited copies are recomputed), then AccessCheck the whole subtree with the confined token:
    a tree could have set an explicit Low label or ACE on something it created.
    """
    lsid = logon_sid()
    set_mandatory_label(run_dir, MEDIUM_IL)
    sid = _sid_from_string(lsid)
    old_dacl, sd, new_dacl = C.c_void_p(), C.c_void_p(), C.c_void_p()
    try:
        _check_err(
            _a.GetNamedSecurityInfoW(
                str(run_dir),
                SE_FILE_OBJECT,
                DACL_SI,
                None,
                None,
                C.byref(old_dacl),
                None,
                C.byref(sd),
            )
        )
        ea = ExplicitAccess(
            access=0,
            mode=REVOKE_ACCESS,
            inheritance=0,
            trustee=Trustee(form=TRUSTEE_IS_SID, name=sid),
        )
        _check_err(_a.SetEntriesInAclW(1, C.byref(ea), old_dacl, C.byref(new_dacl)))
        _check_err(
            _a.SetNamedSecurityInfoW(
                str(run_dir), SE_FILE_OBJECT, DACL_SI, None, None, new_dacl, None
            )
        )
    finally:
        for p in (new_dacl, sd, sid):
            if p:
                _k.LocalFree(p)
    token = mode_token("confined")
    try:
        return audit_create_access(token, [run_dir])
    finally:
        _k.CloseHandle(token)


def run_confined(
    argv,
    run_dir,
    *,
    stdout_path,
    stderr_path,
    mode="confined",
    env=None,
    cwd=None,
    timeout=60.0,
    buffer_bytes=65536,
    read_delay=0.0,
    watch=True,
    precondition=None,
    shared_watch_roots=(),
):
    """Launch ``argv`` as a confined, job-owned tree and capture names created under run_dir.

    ``precondition`` is an ``audit_create_access`` result for this mode's token. Unless it is
    complete and every creatable directory it found lies under the run dir or a
    ``shared_watch_roots`` entry, ``exhaustive`` stays False. Shared roots are pre-existing
    directories the tree can write to (found by the audit), so they are watched too. Other
    processes also write there, so creations seen in them are kept (no omission), but
    ``attribution_exact`` turns False.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    run_dir = Path(run_dir).resolve()
    for p in (stdout_path, stderr_path):
        if _is_under(p, run_dir):
            raise ValueError("harness output files must live outside the watched run dir")
    result = {
        "mechanism": mode,
        "run_dir": str(run_dir),
        "argv": list(argv),
        "confinement": None,
        "handle_inheritance": "explicit PROC_THREAD_ATTRIBUTE_HANDLE_LIST: stdin/stdout/stderr",
        "exit_code": None,
        "timed_out": False,
        "tree_pids": [],
        "tree_drained": False,
        "watcher": None,
        "precondition_audit": None,
        "created_paths": None,
        "created_dirs": None,
        "created_then_deleted": None,
        "ambiguous_short_names": None,
        "shared_roots": [],
        "attribution_exact": None,
        "exhaustive": False,
        "not_exhaustive_because": [],
        "error": None,
    }
    gaps = result["not_exhaustive_because"]
    token = job = None
    handles = []
    watcher = RunDirWatcher(run_dir, buffer_bytes, read_delay=read_delay) if watch else None
    shared = [RunDirWatcher(Path(r), buffer_bytes) for r in shared_watch_roots] if watch else []
    try:
        token, facts = prepare(mode, run_dir)
        result["confinement"] = facts
        if mode != "confined":
            gaps.append(f"mode {mode} is not the confinement prototype")
        if not facts["token_matches_expected"]:
            gaps.append("tree token differs from the requested confinement")

        job = _check(_k.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.basic.flags = KILL_ON_CLOSE
        _check(
            _k.SetInformationJobObject(job, JOB_EXTENDED_LIMITS, C.byref(limits), C.sizeof(limits))
        )

        if watcher is not None:
            if not watcher.start():
                raise OSError(f"watcher failed to arm: {watcher.error}")
            for w in shared:
                if not w.start():
                    raise OSError(f"shared-root watcher failed to arm on {w.root}: {w.error}")
        else:
            gaps.append("no watcher")

        with (
            open(os.devnull, "rb") as stdin,
            open(stdout_path, "wb") as stdout,
            open(stderr_path, "wb") as stderr,
        ):
            std = [_inheritable(stdin), _inheritable(stdout), _inheritable(stderr)]
            size = C.c_size_t()
            _k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            attrs = C.create_string_buffer(size.value)
            _check(_k.InitializeProcThreadAttributeList(attrs, 2, 0, C.byref(size)))
            try:
                hlist = (W.HANDLE * 3)(*std)
                jobs = (W.HANDLE * 1)(job)
                for attr, val in (
                    (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, hlist),
                    (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
                ):
                    _check(
                        _k.UpdateProcThreadAttribute(attrs, 0, attr, val, C.sizeof(val), None, None)
                    )
                si = StartupEx()
                si.startup.cb = C.sizeof(si)
                si.startup.flags = STARTF_USESTDHANDLES
                si.startup.stdin, si.startup.stdout, si.startup.stderr = std
                si.attributes = C.cast(attrs, C.c_void_p)
                pi = ProcessInfo()
                cmd = C.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
                envb = _env_block(env if env is not None else os.environ)
                flags = (
                    CREATE_SUSPENDED
                    | CREATE_UNICODE_ENVIRONMENT
                    | EXTENDED_STARTUPINFO_PRESENT
                    | CREATE_NO_WINDOW
                )
                _check(
                    _a.CreateProcessAsUserW(
                        token,
                        None,
                        cmd,
                        None,
                        None,
                        True,
                        flags,
                        envb,
                        str(cwd or run_dir),
                        C.byref(si),
                        C.byref(pi),
                    )
                )
            finally:
                _k.DeleteProcThreadAttributeList(attrs)
            handles += [pi.process, pi.thread]
            if _k.ResumeThread(pi.thread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())

            deadline, terminated_at, pids = time.monotonic() + timeout, None, set()
            while True:
                pids.update(_job_pids(job))
                acct = Accounting()
                _check(
                    _k.QueryInformationJobObject(
                        job, JOB_BASIC_ACCOUNTING, C.byref(acct), C.sizeof(acct), None
                    )
                )
                if acct.active == 0:
                    result["tree_drained"] = True
                    break
                now = time.monotonic()
                if terminated_at is None and now >= deadline:
                    result["timed_out"] = True
                    _k.TerminateJobObject(job, 124)
                    terminated_at = now
                elif terminated_at is not None and now - terminated_at > 10:
                    break
                time.sleep(0.02)
            code = W.DWORD()
            _k.GetExitCodeProcess(pi.process, C.byref(code))
            result["exit_code"] = int(code.value)
            result["tree_pids"] = sorted(pids)
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        gaps.append("launch or confinement error")
    finally:
        if job:
            _k.TerminateJobObject(job, 98)
        for w in [watcher, *shared]:
            if w is not None and w.started:
                w.stop()
        for h in handles:
            _k.CloseHandle(h)
        if job:
            _k.CloseHandle(job)
        if token:
            _k.CloseHandle(token)

    if watcher is not None:
        if not watcher.armed.is_set() and watcher.error is None:
            watcher.error = "not armed (launch aborted before watch)"
        result["watcher"] = {
            "armed_before_launch": watcher.armed.is_set(),
            "extended_info": watcher.extended,
            "buffer_bytes": buffer_bytes,
            "overflow": watcher.overflow,
            "error": watcher.error,
            "events": len(watcher.events),
        }
        if watcher.error:
            gaps.append(f"watcher failure: {watcher.error}")
        else:  # a failed observer leaves created_paths None (missing), never []
            created, dirs, deleted, ambiguous = summarize(watcher.events, str(run_dir))
            result.update(
                created_paths=created,
                created_dirs=dirs,
                created_then_deleted=deleted,
                ambiguous_short_names=ambiguous,
            )
            if watcher.overflow:
                gaps.append("watcher buffer overflow: OS discarded notifications")
            if ambiguous:
                gaps.append("unresolvable 8.3 short names in record")
        result["events"] = watcher.events
        shared_created = []
        for w in shared:
            info = {
                "root": w.root,
                "armed_before_launch": w.armed.is_set(),
                "overflow": w.overflow,
                "error": w.error or (None if w.armed.is_set() else "not armed"),
            }
            if info["error"]:
                gaps.append(f"shared-root watcher failure on {w.root}: {info['error']}")
            else:
                info["created_paths"], info["created_dirs"], _, amb = summarize(w.events, w.root)
                shared_created += info["created_paths"]
                if w.overflow:
                    gaps.append(f"shared-root watcher overflow on {w.root}")
                if amb:
                    gaps.append(f"unresolvable 8.3 short names under {w.root}")
            result["shared_roots"].append(info)
        if result["created_paths"] is not None:
            result["created_paths"] = result["created_paths"] + shared_created
            result["attribution_exact"] = not shared_created
    if not result["tree_drained"]:
        gaps.append("controller tree not observed drained before watcher stop")
    if precondition is None:
        gaps.append("no precondition audit: other creatable directories not ruled out")
    else:
        result["precondition_audit"] = {
            k: precondition[k] for k in ("roots", "dirs_scanned", "complete", "clean", "seconds")
        }
        covered = [str(run_dir), *map(str, shared_watch_roots)]
        uncovered = [
            d
            for d in precondition["creatable_outside_allowed"]
            if not any(_is_under(d, c) for c in covered)
        ]
        result["precondition_audit"]["uncovered_creatable"] = uncovered
        if uncovered:
            gaps.append(f"audit found {len(uncovered)} other creatable dirs not watched")
        if not precondition["complete"]:
            gaps.append("precondition audit incomplete (unreadable dirs or truncated)")
    result["exhaustive"] = not gaps
    return result


def filesystem_snapshot(result):
    """Contract-shaped projection. ``exhaustive`` is what would license empty_is_exhaustive."""
    return {
        "created_paths": result["created_paths"],
        "exhaustive": bool(result["exhaustive"]),
        "attribution_exact": result["attribution_exact"],
        "scope": {
            "mechanism": result["mechanism"],
            "audited_roots": (result["precondition_audit"] or {}).get("roots"),
        },
    }


def audit_for_mode(mode, run_dir, roots, max_dirs=None):
    """Precondition audit with the token ``run_confined`` would build for ``mode``."""
    token = mode_token(mode)
    try:
        return audit_create_access(token, roots, allowed=[run_dir], max_dirs=max_dirs)
    finally:
        _k.CloseHandle(token)
