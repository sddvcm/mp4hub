"""Win32 helpers to find (and release) processes holding a file open.

Deleting a video is the one MP4Hub operation that touches the original file, and
on Windows a file cannot be unlinked while another process keeps an open handle
to it. The usual blockers are:

* an external player the user launched from the card menu (「用系统播放器打开」),
* our own FFmpeg transcode / remux child for that video,
* a downloader still writing the file.

Two independent detection strategies are used, because neither is sufficient
alone:

1. **Restart Manager** (``RmGetList``) is the API Microsoft documents for "who is
   holding this file". It is accurate when it works, but on a machine where the
   caller is not elevated it commonly returns ``ERROR_MORE_DATA`` together with
   ``RmRebootReasonPermissionDenied`` and an empty process list. It is still
   worth trying first because it is cheap.

2. **System handle table** (``NtQuerySystemInformation`` class 64) is the
   fallback and the one that actually works here. We scan every handle, keep the
   ``File`` object type, resolve each handle's backing path and compare it to the
   target. A naive ``Path`` substring match is wrong: media players also open the
   *containing directory* for change notification, which would implicate
   ``explorer.exe``. Resolving the final path is what keeps this precise.

Termination safety rules:

1. Never touch our own process tree (the service, its FFmpeg children, Electron).
2. Never touch the shell or core system processes.
3. Terminate the owning **root** application, so ``cmd.exe -> VLC.exe`` kills VLC
   rather than the console that launched it.
4. Every step is best-effort — any failure leaves the file locked and the caller
   keeps its original "file is occupied" error instead of guessing.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == 'win32'

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_DUP_HANDLE = 0x0040

DUPLICATE_CLOSE_SOURCE = 0x00000001
DUPLICATE_SAME_ACCESS = 0x00000002

SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

FILE_TYPE_DISK = 0x0001

CCH_RM_SESSION_KEY = 32
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234

# The object type index of "File" objects. Resolving a non-file handle with
# NtQueryObject is not merely wasteful: for named pipes and several device types
# the call blocks indefinitely, which is what made the first full-table scans
# appear to hang. The type index is read straight out of the table instead.
# 37 is the value observed on this Win10 x64 build, confirmed by reading it back
# from a handle opened on a file in this very process.
FILE_TYPE_INDEX = 37
_FILE_TYPE_FALLBACK = FILE_TYPE_INDEX

# Never terminate these, even when one of their handles matches the target.
PROTECTED_NAMES = {
    'system', 'system idle process', 'registry', 'memory compression', 'secure system',
    'smss.exe', 'csrss.exe', 'wininit.exe', 'winlogon.exe', 'services.exe', 'lsass.exe',
    'svchost.exe', 'fontdrvhost.exe', 'dwm.exe', 'sihost.exe', 'explorer.exe',
    'taskhostw.exe', 'runtimebroker.exe', 'searchindexer.exe', 'audiodg.exe',
    'wmiprvse.exe', 'wmiprvse', 'mpcmdrun.exe', 'msmpeng.exe',
}

# Processes that host unrelated work; never promote a blocker up to one of these.
GENERIC_HOSTS = {
    'cmd.exe', 'powershell.exe', 'pwsh.exe', 'conhost.exe', 'windowsterminal.exe',
    'wt.exe', 'openwith.exe', 'dllhost.exe', 'svchost.exe', 'explorer.exe',
}

# Processes whose handles must never be probed during a scan, even when they are
# not part of our own tree. Opening or duplicating one of their handles can block
# indefinitely (console handles, pipe ends, device handles) and would hang the
# whole scan, so anything known to own such handles is listed here.
#
# This list is deliberately small. It must contain only processes that could
# never be a legitimate holder of a video: scripts, browsers, players and generic
# runtimes all *can* be, so excluding them by name silently breaks detection --
# which is exactly what happened when `python.exe` and `chrome.exe` were listed
# here during development. Anything genuinely dangerous is kept out by
# PROTECTED_NAMES, the own-tree check, and the GetFileType guard instead.
NEVER_PROBE_NAMES = PROTECTED_NAMES | {
    'workbuddy.exe', 'sandbox-center.exe', 'sandbox-cli-gc.exe', 'executor.exe',
    'crashpad_handler.exe', 'applicationframehost.exe',
    'secure system', 'registry', 'memory compression',
}

MAX_PARENT_HOPS = 4

# How many handles of a single process the scanner is willing to resolve. The
# target file's handle is normally within the first few dozen, while services such
# as cloud-drive helpers can hold 30,000+ handles; the cap keeps a full scan in
# the low hundreds of milliseconds.
MAX_HANDLES_PER_PID = 400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_ushort),
        ('fAnyOperationsAborted', wintypes.BOOL), ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ('Process', _RM_UNIQUE_PROCESS),
        ('strAppName', wintypes.WCHAR * 256),
        ('strServiceShortName', wintypes.WCHAR * 64),
        ('ApplicationType', wintypes.DWORD),
        ('AppStatus', wintypes.ULONG),
        ('TSSessionId', wintypes.DWORD),
        ('bRestartable', wintypes.BOOL),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
        ('szExeFile', wintypes.WCHAR * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                ('Buffer', wintypes.LPWSTR)]


class _OBJECT_NAME_INFORMATION(ctypes.Structure):
    _fields_ = [('Name', _UNICODE_STRING)]


def _kernel32():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                    wintypes.BOOL, wintypes.DWORD]
    k32.DuplicateHandle.restype = wintypes.BOOL
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.GetFileType.restype = wintypes.DWORD
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    return k32


def _ntdll():
    ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
    ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    # NTSTATUS is signed; without masking, 0xC0000004 arrives as a negative value
    # and every `== STATUS_INFO_LENGTH_MISMATCH` test silently fails.
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    ntdll.NtQueryObject.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryObject.restype = ctypes.c_long
    return ntdll


def _status(value: int) -> int:
    """Normalise an NTSTATUS read through a signed c_long into unsigned form."""
    return value & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# Restart Manager (fast path, often blocked without elevation)
# --------------------------------------------------------------------------- #

def _restart_manager_lockers(paths: list[Path]) -> set[int]:
    if not IS_WINDOWS or not paths:
        return set()
    try:
        rm = ctypes.WinDLL('rstrtmgr', use_last_error=True)
        rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
        rm.RmStartSession.restype = wintypes.DWORD
        rm.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT,
                                           ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT,
                                           ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
        rm.RmRegisterResources.restype = wintypes.DWORD
        rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
                                 ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_RM_PROCESS_INFO),
                                 ctypes.POINTER(wintypes.DWORD)]
        rm.RmGetList.restype = wintypes.DWORD
        rm.RmEndSession.argtypes = [wintypes.DWORD]

        session = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
        if rm.RmStartSession(ctypes.byref(session), 0, key) != ERROR_SUCCESS:
            return set()
        try:
            files = [str(Path(item).resolve()) for item in paths]
            arr = (wintypes.LPCWSTR * len(files))(*files)
            if rm.RmRegisterResources(session, len(files), arr, 0, None, 0, None) != ERROR_SUCCESS:
                return set()
            # Pass a generous buffer up front: a zero-sized first probe makes the
            # API answer ERROR_MORE_DATA without ever filling nProcInfo.
            slots = 16
            needed = wintypes.UINT(slots)
            count = wintypes.UINT(0)
            reboot = wintypes.DWORD(0)
            buffer = (_RM_PROCESS_INFO * slots)()
            status = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), buffer,
                                  ctypes.byref(reboot))
            if status not in (ERROR_SUCCESS, ERROR_MORE_DATA) or count.value == 0:
                return set()
            return {buffer[i].Process.dwProcessId for i in range(count.value)
                    if buffer[i].Process.dwProcessId}
        finally:
            try:
                rm.RmEndSession(session)
            except OSError:
                pass
    except (OSError, AttributeError, ValueError):
        return set()


# --------------------------------------------------------------------------- #
# System handle table (fallback that works without elevation)
# --------------------------------------------------------------------------- #

def _volume_map(k32) -> dict[str, str]:
    """Map every DOS drive letter to its ``\\Device\\...`` target."""
    mapping: dict[str, str] = {}
    if not IS_WINDOWS:  # pragma: no cover
        return mapping
    k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.QueryDosDeviceW.restype = wintypes.DWORD
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(f'{letter}:', buf, 1024):
            mapping[buf.value.lower()] = f'{letter}:\\'
    return mapping


def _device_to_dos(path: str, volume_map: dict[str, str], unc_prefix: str | None) -> str | None:
    """Translate ``\\Device\\HarddiskVolumeX\\...`` into a DOS / UNC path."""
    if not path:
        return None
    lowered = path.lower()
    # Network redirectors and named pipes are reachable as UNC directly.
    if lowered.startswith('\\device\\mup\\') or lowered.startswith('\\??\\unc\\'):
        tail = path[len('\\Device\\Mup\\'):] if lowered.startswith('\\device\\mup\\') else path[8:]
        return '\\\\' + tail.lstrip('\\')
    for device, dos in volume_map.items():
        if lowered.startswith(device + '\\'):
            return dos + path[len(device) + 1:]
    if unc_prefix and lowered.startswith(unc_prefix.lower()):
        return path[len(unc_prefix):]
    return None


def _long_path(k32, path: str) -> str:
    """Expand an 8.3 short path into its long form.

    The kernel reports device paths with short components
    (``\\Device\\HarddiskVolume2\\Users\\ADMINI~1\\...``). Comparing those against
    a normal Python path never matches, so both sides are normalised first.
    """
    if not path:
        return path
    try:
        k32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetLongPathNameW.restype = wintypes.DWORD
        needed = k32.GetLongPathNameW(path, None, 0)
        if not needed:
            return path
        buf = ctypes.create_unicode_buffer(needed + 1)
        if k32.GetLongPathNameW(path, buf, len(buf)):
            return buf.value
    except (OSError, AttributeError):  # pragma: no cover
        pass
    return path


def _object_name(ntdll, handle) -> str | None:
    """Resolve an opened handle's object name (``\\Device\\...``)."""
    try:
        size = wintypes.ULONG(2048)
        buf = ctypes.create_string_buffer(size.value)
        status = _status(ntdll.NtQueryObject(handle, 1, buf, size.value, ctypes.byref(size)))
        if status != 0:
            return None
        info = ctypes.cast(buf, ctypes.POINTER(_OBJECT_NAME_INFORMATION))
        name = info.contents.Name
        if not name.Buffer or not name.Length:
            return None
        return ctypes.wstring_at(name.Buffer, name.Length // 2)
    except (OSError, ValueError):
        return None


def _query_handles(ntdll) -> bytes | None:
    """Fetch the system-wide handle table.

    The first probe always answers ``STATUS_INFO_LENGTH_MISMATCH`` with a
    *minimum* buffer size (56 bytes here) rather than the real requirement, so a
    single retry is never enough: the buffer must be grown until the call
    succeeds. Sizing off that first answer is what makes this work.
    """
    size = wintypes.ULONG(0)
    status = _status(ntdll.NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION, None, 0,
                                                    ctypes.byref(size)))
    if status != STATUS_INFO_LENGTH_MISMATCH or size.value <= 0:
        return None
    # The first probe returns a placeholder minimum, not the real requirement, and
    # the call only fills NumberOfHandles once the buffer is genuinely large
    # enough (observed ~7.7 MB here). Grow geometrically until it succeeds.
    capacity = int(max(size.value * 64, 1 << 20))
    for _ in range(10):
        buffer = ctypes.create_string_buffer(capacity)
        status = _status(ntdll.NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION, buffer,
                                                        capacity, ctypes.byref(size)))
        if status == 0:
            return buffer
        if status != STATUS_INFO_LENGTH_MISMATCH:
            return None
        capacity = max(int(size.value), capacity * 2)
    return None


def _handle_targets(target_aliases: set[str], volume_map: dict[str, str],
                    unc_prefix: str | None, table: dict[int, tuple[int, str]] | None = None,
                    own: set[int] | None = None) -> set[int]:
    """PIDs of every process holding a handle whose final path is the target."""
    ntdll = _ntdll()
    k32 = _kernel32()
    if own is None:
        own = _own_pids(table or _snapshot(k32))
    table = table or {}
    # Processes we will not touch, beyond our own tree: probing a shell or agent
    # host's handles can block forever on a console or pipe handle. Both sets are
    # resolved to pids up front so the inner loop is a plain integer test.
    skip = set(own)
    for pid, (_, name) in table.items():
        if (name or '').lower() in NEVER_PROBE_NAMES:
            skip.add(pid)
    # Probing a process is not free (OpenProcess + DuplicateHandle + NtQueryObject
    # per handle), so each pid gets a handle budget; a cloud-drive service can
    # hold 30,000+ handles and would otherwise dominate the scan.
    budget: dict[int, int] = {}

    raw = _query_handles(ntdll)
    if raw is None:
        return set()

    # Layout verified empirically on Win10 x64 by opening a file in this process
    # and locating its exact OS handle inside the table. Consecutive rows of the
    # same process sit exactly 40 bytes apart, with the pid immediately followed
    # by the handle:
    #
    #   header : 16 bytes  (4-byte NumberOfHandles + 12 bytes of padding)
    #   entry  : 40 bytes each
    #     +0   Object            (8 bytes; 0 = free slot)
    #     +8   UniqueProcessId   (4 bytes, then 4 bytes padding)
    #     +16  HandleValue       (4 bytes, then 4 bytes padding)
    #     +24  GrantedAccess     (4 bytes)
    #     +30  ObjectTypeIndex   (2 bytes; 37 == "File" on this build)
    #
    # Four earlier guesses were all wrong, and every one of them failed silently
    # by matching nothing rather than raising:
    #   * 8-byte header with a 40-byte strip  -> handle landed on GrantedAccess
    #   * 4-byte handle at +12                -> landed on the pid's high bytes
    #   * 80-byte stride                      -> aliased adjacent row pairs
    #   * type index read from +20            -> always zero
    # The 16-byte header is the one that matters: with an 8-byte header every row
    # is off by 8 and no real handle/pid pair is ever adjacent.
    entry_size = 40
    header_size = 16
    object_offset = 0
    pid_offset = 8
    handle_offset = 16
    type_offset = 30

    count = ctypes.cast(raw, ctypes.POINTER(ctypes.c_ulong))[0]
    base = ctypes.addressof(raw) + header_size
    # Guard against reading past the allocation when the reported count and the
    # buffer size disagree.
    capacity = (len(raw) - header_size) // entry_size
    count = min(count, capacity)

    def read(entry: int, offset: int, size: int) -> int:
        pointer = ctypes.cast(entry + offset, ctypes.POINTER(ctypes.c_ulonglong if size == 8
                                                              else ctypes.c_ulong))
        return pointer[0]

    def read16(entry: int, offset: int) -> int:
        return ctypes.cast(entry + offset, ctypes.POINTER(ctypes.c_ushort))[0]

    found: set[int] = set()
    for index in range(count):
        entry = base + entry_size * index
        if not read(entry, object_offset, 8):
            continue
        # Only File objects can carry a path we could match, and calling
        # NtQueryObject on anything else risks an indefinite block.
        if read16(entry, type_offset) != FILE_TYPE_INDEX:
            continue
        pid = read(entry, pid_offset, 4)
        if not pid or pid in (0, 4) or pid in skip or pid in found:
            continue
        if budget.get(pid, 0) >= MAX_HANDLES_PER_PID:
            continue
        handle = read(entry, handle_offset, 4)
        if not handle:
            continue
        budget[pid] = budget.get(pid, 0) + 1
        resolved = _resolve_handle(k32, ntdll, pid, handle, volume_map, unc_prefix)
        if resolved and resolved.lower() in target_aliases:
            found.add(pid)
    return found


def _resolve_handle(k32, ntdll, pid: int, handle: int, volume_map: dict[str, str],
                    unc_prefix: str | None) -> str | None:
    """Duplicate `handle` out of `pid` and translate its path to DOS form.

    ``GetFileType`` is called before ``NtQueryObject`` on purpose. Enumerating a
    handle table turns up device handles owned by GPU-driver containers and
    similar services for which ``NtQueryObject`` blocks indefinitely; the scan
    would then never return. ``GetFileType`` answers ``FILE_TYPE_DISK`` (1) for
    ordinary files, which is the only kind that can hold a video, and it never
    blocks.
    """
    source = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not source:
        return None
    try:
        dup = wintypes.HANDLE()
        # DUPLICATE_CLOSE_SOURCE would rip the handle out of the inspected
        # process, which is both destructive and a reason for the whole scan to
        # fail. A read-only duplicate is all we need to name the object.
        if not k32.DuplicateHandle(source, wintypes.HANDLE(handle), k32.GetCurrentProcess(),
                                   ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS):
            return None
        try:
            if k32.GetFileType(dup) != FILE_TYPE_DISK:
                return None
            resolved = _device_to_dos(_object_name(ntdll, dup) or '', volume_map, unc_prefix)
            return _long_path(k32, resolved) if resolved else None
        finally:
            k32.CloseHandle(dup)
    except (OSError, ValueError):
        return None
    finally:
        k32.CloseHandle(source)


# --------------------------------------------------------------------------- #
# Process tree helpers
# --------------------------------------------------------------------------- #

def _snapshot(k32) -> dict[int, tuple[int, str]]:
    """Return ``{pid: (parent_pid, exe_name)}`` for every running process."""
    if not IS_WINDOWS:  # pragma: no cover
        return {}
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, wintypes.HANDLE(-1).value):
        return {}
    result: dict[int, tuple[int, str]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return {}
        while True:
            result[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)
    return result


def _own_pids(table: dict[int, tuple[int, str]]) -> set[int]:
    """PIDs belonging to our own tree: ancestors, siblings, and descendants."""
    own: set[int] = set()
    current = os.getpid()
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        own.add(current)
        if current not in table:
            break
        current = table[current][0]
    # Descendants of anything in the ancestor chain are ours too (FFmpeg workers,
    # helper windows). A bounded walk per process keeps this O(n).
    for pid in table:
        probe, hops = pid, 0
        while probe and hops < 32:
            if probe in own:
                own.add(pid)
                break
            if probe not in table:
                break
            probe = table[probe][0]
            hops += 1
    return own


def _ancestry(pid: int, table: dict[int, tuple[int, str]]) -> list[tuple[int, str]]:
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    current = pid
    for _ in range(MAX_PARENT_HOPS + 1):
        if current in seen or current not in table:
            break
        seen.add(current)
        parent, name = table[current]
        chain.append((current, name))
        current = parent
    return chain


def _choose_root(chain: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Pick the owning application from a blocker's parent chain.

    ``chain`` runs from the blocker outwards: ``[(vlc.exe), (cmd.exe), ...]``.
    A generic host (a shell, the desktop, a console host) is *skipped*, not used
    as a stopping point, because a player is very often started from one. Anything
    protected ends the walk, since nothing beyond it could be ours to kill.
    """
    chosen: tuple[int, str] | None = None
    for hop_pid, hop_name in chain:
        low = (hop_name or '').lower()
        if not low or low in NEVER_PROBE_NAMES:
            break
        if low in GENERIC_HOSTS:
            continue
        chosen = (hop_pid, hop_name)
    return chosen


def _kill(k32, pid: int) -> bool:
    handle = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def find_lockers(paths: list[Path]) -> list[str]:
    """Return the exe names of applications locking `paths` (diagnostics only)."""
    if not IS_WINDOWS:
        return []
    targets = [Path(item) for item in paths if item is not None]
    if not targets:
        return []
    k32 = _kernel32()
    table = _snapshot(k32)
    own = _own_pids(table)
    pids = _restart_manager_lockers(targets) - own
    if not pids:
        pids = _handle_targets(_aliases(targets, k32), _volume_map(k32), None, table, own)
    pids -= own
    names = {table.get(pid, (0, f'PID {pid}'))[1] for pid in pids}
    return sorted(names)


def release(paths: list[Path], wait: float = 1.2) -> list[str]:
    """Terminate the processes locking `paths` so the caller can delete them.

    Returns the exe names of the processes that were terminated. An empty list
    means nobody was holding the file, or the holders were protected.
    """
    if not IS_WINDOWS:
        return []
    targets = [Path(item) for item in paths if item is not None]
    if not targets:
        return []

    k32 = _kernel32()
    table = _snapshot(k32)
    own = _own_pids(table)

    pids = _restart_manager_lockers(targets) - own
    if not pids:
        pids = _handle_targets(_aliases(targets, k32), _volume_map(k32), None, table, own)
    pids -= own

    promoted: dict[int, str] = {}
    for pid in pids:
        chain = _ancestry(pid, table)
        chosen = _choose_root(chain)
        if chosen:
            promoted[chosen[0]] = chosen[1]
    if not promoted:
        return []

    killed: list[str] = []
    for pid, name in promoted.items():
        if _kill(k32, pid):
            killed.append(name)
    if killed:
        time.sleep(wait)  # Give the OS a moment to drop the handles.
    return killed


def _entry_aliases(path: str, k32) -> set[str]:
    """Every spelling a handle could use for one path, across mount entry points.

    A single volume can be reachable under several names at once. RaiDrive is the
    case that matters here: one WebDAV share is exposed both as
    ``\\\\RaiDrive-Administrator\\WebDAV\\...`` and as ``Z:\\...``, and the *same*
    underlying entry is mapped to ``\\Device\\cbfs6Nr\\;Z:...\\RaiDrive-Administrator\\WebDAV``.
    A handle may therefore report either spelling, so matching only the spelling
    the library row happens to use silently misses real lockers.
    """
    out: set[str] = set()
    lowered = path.lower()
    out.add(lowered)

    # Drive letter -> UNC: use the device mapping to learn the share it fronts.
    if len(path) > 2 and path[1] == ':':
        letter = path[0].upper()
        device = _dos_device(k32, letter)
        if device:
            tail = _share_tail(device, letter)
            if tail:
                out.add(('\\\\' + tail + path[2:]).lower())

    # UNC -> drive letter: find any drive whose share matches the UNC prefix.
    if lowered.startswith('\\\\'):
        parts = path[2:].split('\\')
        if len(parts) >= 2:
            share = '\\' + '\\'.join(parts[:2]).lower()  # \host\share
            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                device = _dos_device(k32, letter)
                if not device:
                    continue
                if share in device.lower():
                    tail = '\\' + '\\'.join(parts[2:])
                    out.add(f'{letter}:{tail}'.lower())
                    break
    return out


def _dos_device(k32, letter: str) -> str | None:
    """Return the ``\\Device\\...`` target of a drive letter, if it has one."""
    try:
        k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.QueryDosDeviceW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(f'{letter}:', buf, 1024):
            return buf.value
    except (OSError, AttributeError):  # pragma: no cover
        pass
    return None


# A redirected drive's device name embeds a fixed-width volume serial:
#     \Device\cbfs6Nr\;Z:0000000000044cf5\RaiDrive-Administrator\WebDAV
#                    ^^^ marker  ^^^^^^^^^^^^^^^^ serial  ^^^^^^^^^^^^^^^^^^ share
VOLUME_SERIAL_WIDTH = 16


def _share_tail(device: str, letter: str) -> str | None:
    """Extract the ``host\\share`` portion of a redirected drive's device name.

    The serial has a *fixed* width, so it is skipped positionally. Trimming
    leading hex characters instead would silently corrupt any share whose name
    starts with one -- a share called ``abbey`` would lose ``abb``.
    """
    marker = f';{letter}:'
    index = device.upper().find(marker.upper())
    if index == -1:
        return None
    rest = device[index + len(marker):]
    if len(rest) <= VOLUME_SERIAL_WIDTH:
        return None
    tail = rest[VOLUME_SERIAL_WIDTH:]
    if not tail.startswith('\\'):
        return None
    return tail.lstrip('\\')


def _aliases(targets: list[Path], k32=None) -> set[str]:
    """Case-folded set of path spellings a handle might report for `targets`."""
    k32 = k32 or _kernel32()
    out: set[str] = set()
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError:  # pragma: no cover
            resolved = target
        spellings = [str(resolved), _long_path(k32, str(resolved))]
        try:
            spellings.append(os.path.realpath(resolved))
        except OSError:  # pragma: no cover
            pass
        for spelling in spellings:
            if spelling:
                out |= _entry_aliases(spelling, k32)
    return out
