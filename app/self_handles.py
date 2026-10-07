"""Track file handles *this* process opens while serving media, so a delete can
close them on demand.

Windows refuses to unlink a file that any process still holds open. Starlette's
``FileResponse`` opens the target with the C runtime's ``open()``, whose default
share mode is "read + write" but *not* delete — so while a video stream is live
the backend itself is the holder, ``process_probe.release()`` correctly refuses to
kill our own tree, and the delete bounces back as ``WinError 32``.

Killing ourselves is obviously not an option, so the handle has to be closed from
inside. This module keeps a registry of the delivery handles the backend opens
for a given path; ``close_for(path)`` closes them all, which lets the caller
unlink the file immediately afterwards.

The registry only ever holds duplicate *wrappers* around handles the delivery
layer already owns; closing through ``close_for`` marks the wrapper closed so the
original ``finally`` cleanup becomes a no-op instead of double-closing an fd.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


class _Entry:
    __slots__ = ("handle", "path", "closed")

    def __init__(self, handle, path: str) -> None:
        self.handle = handle
        self.path = path
        self.closed = False


_lock = threading.RLock()
_entries: dict[int, _Entry] = {}


def _key(path: str | Path) -> str:
    """Normalise a path so the delivery layer and the delete flow agree.

    Three spellings of the same file reach us: the media row's stored path (which
    on Windows often carries an 8.3 short name such as ``ADMINI~1``), the path the
    delivery layer was handed, and ``native_media_path()``'s ``.resolve()``d form.
    ``abspath`` alone keeps the short name, so a registry keyed on it would never
    match the resolver's long name — use ``realpath``, which expands it, and fold
    case for good measure.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def register(handle, path: str | Path) -> _Entry:
    """Track `handle` while it is open on `path`."""
    entry = _Entry(handle, _key(path))
    with _lock:
        _entries[id(entry)] = entry
    return entry


def unregister(entry: _Entry | None) -> None:
    """Stop tracking an entry (called from the response's own finally block)."""
    if entry is None:
        return
    with _lock:
        _entries.pop(id(entry), None)


def close_for(path: str | Path) -> int:
    """Close every tracked handle open on `path`; return how many were closed.

    Closing an fd that a still-reading generator holds will make that reader
    raise, which is the intended outcome: the client asked to delete the very
    file it is streaming. The delivery layer catches the resulting ``OSError``
    and simply ends the response.
    """
    target = _key(path)
    closed = 0
    with _lock:
        for entry in list(_entries.values()):
            if entry.closed or entry.path != target:
                continue
            entry.closed = True
            try:
                os.close(entry.handle)
                closed += 1
            except OSError:
                # Already closed by its owner, or never a valid fd. Either way
                # there is nothing left for us to do.
                pass
            _entries.pop(id(entry), None)
    return closed


def open_on(path: str | Path) -> bool:
    """True when this process currently holds a tracked handle on `path`."""
    target = _key(path)
    with _lock:
        return any(not e.closed and e.path == target for e in _entries.values())
