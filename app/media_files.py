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


def recycle(paths: list[Path]) -> None:
    """Move every path to the Windows Recycle Bin.

    Raises DeleteError if the shell refuses the operation or a path survives.
    """
    targets = [str(Path(item)) for item in paths if item is not None]
    if not targets:
        return
    if not IS_WINDOWS:
        raise DeleteError('回收站删除仅支持 Windows')

    for target in targets:
        if '"' in target:  # SHFileOperationW parses the buffer as a command line.
            raise DeleteError('路径包含不支持的字符')

    # Double-null-terminated multi-string buffer.
    buffer = ctypes.create_unicode_buffer('\0'.join(targets) + '\0\0')
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

    survivors = _wait_for_removal(targets)
    if survivors:  # pragma: no cover - depends on host shell
        raise DeleteError('文件未能移入回收站，可能被其他程序占用')


def permanent(paths: list[Path]) -> None:
    """Delete every path without using the Recycle Bin."""
    targets = [Path(item) for item in paths if item is not None]
    if not targets:
        return
    failures: list[str] = []
    for target in targets:
        try:
            os.remove(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f'{target.name}：{exc}')
    survivors = _wait_for_removal([str(item) for item in targets])
    if failures or survivors:
        detail = '；'.join(failures) if failures else '文件仍被占用'
        raise DeleteError(f'无法彻底删除：{detail}')


def _wait_for_removal(targets: list[str]) -> list[str]:
    """Return the subset of targets that still exist after a short grace period."""
    import time

    for _ in range(RECYCLE_VERIFY_ATTEMPTS):
        remaining = [item for item in targets if os.path.exists(item)]
        if not remaining:
            return []
        time.sleep(RECYCLE_VERIFY_INTERVAL)
    return [item for item in targets if os.path.exists(item)]
