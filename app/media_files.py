"""Destructive media-file operations: recycle-bin removal and permanent deletion.

Both modes remove the physical video file. The caller is responsible for dropping
the matching library row so the list view stays in sync with the disk.

Windows recycle-bin removal uses SHFileOperationW(FO_DELETE|FOF_ALLOWUNDO) because
it needs no third-party dependency and honours the shell's "send to Recycle Bin"
semantics. The API reports per-operation codes that are not reliable on their own
(it can return DE_* values such as 2 even when every file was moved successfully),
so the authoritative success check is whether the source path disappeared.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == 'win32'

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

# Give the shell a moment to finish moving files before we conclude failure.
RECYCLE_VERIFY_ATTEMPTS = 40
RECYCLE_VERIFY_INTERVAL = 0.05


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND),
        ('wFunc', wintypes.UINT),
        ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR),
        ('fFlags', ctypes.c_ushort),
        ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]


class DeleteError(RuntimeError):
    """Raised when the underlying operating-system delete call fails."""


# Windows error codes worth naming in the message the user sees.
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

# A redirected drive's device name embeds a fixed-width volume serial:
#     \Device\cbfs6Nr\;Z:0000000000044cf5\RaiDrive-Administrator\WebDAV
VOLUME_SERIAL_WIDTH = 16


def _winerror(exc: OSError) -> int | None:
    """Extract the Win32 error code, tolerating wrappers that hide it."""
    code = getattr(exc, 'winerror', None)
    if code:
        return code
    # Some shims and drivers surface the code only inside the message text.
    text = str(exc)
    for marker in ('WinError ', 'winerror '):
        index = text.find(marker)
        if index != -1:
            digits = text[index + len(marker):].split(']')[0].split()[0].rstrip(':,')
            if digits.isdigit():
                return int(digits)
    return None


# CreateFileW arguments for a write probe.
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_NONE = 0
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def occupancy(path: Path) -> str:
    """Classify why a path could not be removed.

    Returns one of ``'free'``, ``'locked'``, ``'denied'`` or ``'missing'``.

    This asks the filesystem directly with an exclusive write open, which is the
    only test that answers the question that matters: *is somebody else holding
    this file?* Enumerating process handles cannot answer it for user-mode
    filesystems -- RaiDrive's ``cbfs6`` driver, WinFsp and Dokany keep their locks
    inside the driver, so nothing shows up in the system handle table even while
    the file is genuinely exclusive.
    """
    if not IS_WINDOWS:  # pragma: no cover
        return 'missing' if not path.exists() else 'free'
    if not os.path.exists(path):
        return 'missing'
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), _GENERIC_WRITE, _FILE_SHARE_NONE, None,
                             _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle and handle != _INVALID_HANDLE:
        k32.CloseHandle(handle)
        return 'free'
    code = ctypes.get_last_error()
    if code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return 'locked'
    if code == ERROR_ACCESS_DENIED:
        # Nobody is holding it exclusively, yet writing was refused: that is a
        # permission or sync-client problem, not an occupier.
        return 'denied'
    return 'denied'



def _explain(exc: OSError | None, name: str, path: Path | None = None) -> str:
    """Turn a raw OS error into something the user can act on.

    A probe of the file itself is used to pick between "somebody is holding it"
    and "the filesystem refused for another reason". The distinction matters
    because the two need completely different advice, and the raw error code
    alone does not separate them: a network mount reports ACCESS_DENIED both when
    a sync client is mid-upload and when the entry point simply forbids deletes.
    """
    state = occupancy(path) if path is not None else None
    if state == 'locked':
        return (f'{name}：文件正被其他程序占用，且该程序无法被自动关闭'
                '（例如网盘客户端的同步进程）。请关闭后重试。')
    if state == 'denied':
        return (f'{name}：系统拒绝访问。该文件所在位置不允许删除，常见原因是'
                '网盘客户端正在同步该文件、或该文件在网盘端为只读；'
                '请稍后重试，或先在网盘客户端中暂停同步。')
    if state == 'free':
        return f'{name}：删除请求被系统拒绝，但文件当前并未被占用，请稍后重试。'
    if exc is None:
        return f'{name}：文件仍被占用'
    code = _winerror(exc)
    if code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return f'{name}：文件正被其他程序占用（{exc}）'
    if code == ERROR_ACCESS_DENIED:
        return (f'{name}：系统拒绝访问（{exc}）。'
                '常见原因是网盘客户端（RaiDrive / OneDrive 等）正在同步该文件，'
                '或该文件在网盘端为只读；请稍后重试或先在网盘客户端中处理。')
    return f'{name}：{exc}'


def _alternates(path: Path) -> list[Path]:
    """Other mount entry points for the same file.

    A volume can be reachable through more than one name -- RaiDrive exposes one
    WebDAV share as both ``\\\\host\\WebDAV\\...`` and ``Z:\\...``, and the two
    entry points do not always accept the same operations. When a delete is
    refused through the spelling the library recorded, retrying through the twin
    is cheap and often succeeds.
    """
    if not IS_WINDOWS:
        return []
    text = str(path)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.QueryDosDeviceW.restype = wintypes.DWORD

    out: list[Path] = []
    lowered = text.lower()
    if lowered.startswith('\\\\'):
        parts = text[2:].split('\\')
        if len(parts) >= 2:
            share = ('\\' + '\\'.join(parts[:2])).lower()
            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                buf = ctypes.create_unicode_buffer(1024)
                if not k32.QueryDosDeviceW(f'{letter}:', buf, 1024):
                    continue
                if share in buf.value.lower():
                    out.append(Path(f'{letter}:' + '\\' + '\\'.join(parts[2:])))
                    break
    elif len(text) > 2 and text[1] == ':':
        letter = text[0].upper()
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(f'{letter}:', buf, 1024):
            # The device name carries a fixed-width serial before the share, e.g.
            # \Device\cbfs6Nr\;Z:0000000000044cf5\RaiDrive-Administrator\WebDAV
            # Skipping it positionally avoids corrupting share names that begin
            # with a hex character.
            marker = f';{letter}:'
            index = buf.value.upper().find(marker.upper())
            if index != -1:
                rest = buf.value[index + len(marker):]
                if len(rest) > VOLUME_SERIAL_WIDTH:
                    share = rest[VOLUME_SERIAL_WIDTH:]
                    if share.startswith('\\'):
                        out.append(Path('\\\\' + share.lstrip('\\') + text[2:]))
    return out


def _try_delete(target: Path) -> None:
    """Delete one path, raising OSError from the OS layer."""
    os.remove(target)


def recycle(paths: list[Path]) -> None:
    """Move every path to the Windows Recycle Bin.

    Raises DeleteError if the shell refuses the operation or a path survives. A
    refusal through the recorded spelling is retried through the file's other
    mount entry point, the same way :func:`permanent` does.
    """
    targets = [Path(item) for item in paths if item is not None]
    if not targets:
        return
    if not IS_WINDOWS:
        raise DeleteError('回收站删除仅支持 Windows')

    def _shell_delete(items: list[Path]) -> None:
        texts = [str(item) for item in items]
        for text in texts:
            if '"' in text:  # SHFileOperationW parses the buffer as a command line.
                raise DeleteError('路径包含不支持的字符')
        # Double-null-terminated multi-string buffer.
        buffer = ctypes.create_unicode_buffer('\0'.join(texts) + '\0\0')
        operation = _SHFILEOPSTRUCTW()
        operation.hwnd = None
        operation.wFunc = FO_DELETE
        operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
        operation.pTo = None
        operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        operation.fAnyOperationsAborted = False
        operation.hNameMappings = None
        operation.lpszProgressTitle = None
        try:
            ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        except OSError as exc:  # pragma: no cover - depends on host shell
            raise DeleteError(f'回收站删除调用失败：{exc}') from exc
        if operation.fAnyOperationsAborted:
            raise DeleteError('回收站删除已被取消')

    _shell_delete(targets)
    if not _wait_for_removal([str(item) for item in targets]):
        return

    # Something survived. Retry the stragglers through their alternate entry
    # points before giving up -- network mounts do not all accept the same calls.
    for target in targets:
        if not os.path.exists(target):
            continue
        for alternate in _alternates(target):
            _shell_delete([alternate])

    survivors = _wait_for_removal([str(item) for item in targets])
    if survivors:
        names = '、'.join(Path(item).name for item in survivors)
        raise DeleteError(f'文件未能移入回收站，可能被其他程序占用：{names}')


def permanent(paths: list[Path]) -> None:
    """Delete every path without using the Recycle Bin.

    Two deliberate departures from a naive ``os.remove`` loop:

    * The call and the outcome are judged separately. On network shares
      (WebDAV/SMB, RaiDrive, mapped drives) and with antivirus or filesystem
      filter drivers in the path, ``os.remove`` may raise even though the entry
      is already gone. Treating that exception as failure reports an error for a
      file the user can see has been deleted, so the path's disappearance is the
      authoritative success signal.
    * A refused delete is retried through the file's other mount entry point. One
      volume is often reachable under several names and they do not all accept
      the same operations.
    """
    targets = [Path(item) for item in paths if item is not None]
    if not targets:
        return
    errors: dict[str, OSError] = {}
    for target in targets:
        error = _remove_with_fallback(target)
        if error is not None:
            errors[str(target)] = error
    survivors = _wait_for_removal([str(item) for item in targets])
    if not survivors:
        return
    # Only paths genuinely still present count as failures.
    details = [_explain(errors.get(item), Path(item).name, Path(item)) for item in survivors]
    raise DeleteError(f'无法彻底删除：{"；".join(details)}')


def _remove_with_fallback(target: Path) -> OSError | None:
    """Delete one file, retrying through alternate mount names.

    Returns the last OS error when every attempt failed, or None on success
    (including the case where the entry is already gone).
    """
    last: OSError | None = None
    for candidate in [target, *_alternates(target)]:
        try:
            _try_delete(candidate)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
            # ACCESS_DENIED on a network mount is often transient sync state, so
            # give the driver one more chance before moving to the next entry.
            if _winerror(exc) == ERROR_ACCESS_DENIED:
                time.sleep(0.25)
                try:
                    _try_delete(candidate)
                    return None
                except FileNotFoundError:
                    return None
                except OSError as retry_exc:
                    last = retry_exc
    return last


def _wait_for_removal(targets: list[str]) -> list[str]:
    """Return the subset of targets that still exist after a short grace period."""
    import time

    for _ in range(RECYCLE_VERIFY_ATTEMPTS):
        remaining = [item for item in targets if os.path.exists(item)]
        if not remaining:
            return []
        time.sleep(RECYCLE_VERIFY_INTERVAL)
    return [item for item in targets if os.path.exists(item)]
