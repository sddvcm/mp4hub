"""Read-only original-file delivery with explicit video types and standard Range handling.

The response also records the descriptor it opens in :mod:`app.self_handles`, so
the delete flow can close the backend's own live stream before unlinking. Without
that, a playing video pins the file with the backend as the holder, and Windows
answers the unlink with ``WinError 32`` — which is exactly the "quit the app and
it deletes fine" symptom.
"""
from pathlib import Path
import mimetypes
import stat

import anyio
from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import self_handles


VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".ts": "video/mp2t", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
}


class MediaFileResponse(FileResponse):
    # Keep Starlette's suffix/multipart/If-Range/HEAD behavior. Larger reads reduce
    # threadpool hops for local high-bitrate video without buffering the whole file.
    chunk_size = 1024 * 1024

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Filled in by _tracked_open while a read is in flight.
        self._tracked = None

    async def _tracked_open(self, mode: str = "rb"):
        """Open the target and register the fd so a delete can close it.

        Starlette opens the file itself in three places (simple, single-range,
        multi-range). Routing all of them through here means the registry always
        knows which descriptor is live, no matter which branch the request took.
        """
        file = await anyio.open_file(self.path, mode=mode)
        real = getattr(file, "_fp", None)
        fd = getattr(real, "fileno", None)
        try:
            fileno = fd() if fd is not None else None
        except (OSError, ValueError):
            fileno = None
        if fileno is not None:
            self._tracked = self_handles.register(fileno, self.path)
        return file

    async def _handle_simple(self, send, send_header_only, send_pathsend) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if send_pathsend:
            # The server hands the path to the kernel, so our process never holds
            # a descriptor and there is nothing to release.
            await send({"type": "http.response.pathsend", "path": str(self.path)})
            return
        file = await self._tracked_open("rb")
        try:
            more_body = True
            while more_body:
                try:
                    chunk = await file.read(self.chunk_size)
                except (OSError, ValueError):
                    # A concurrent delete closed our fd under us. The file is gone
                    # (that was the point), so end the response quietly.
                    break
                more_body = len(chunk) == self.chunk_size
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})
        finally:
            self_handles.unregister(self._tracked)
            self._tracked = None
            await self._quiet_close(file)

    async def _handle_single_range(self, send, start, end, file_size, send_header_only) -> None:
        from starlette.responses import MutableHeaders

        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        file = await self._tracked_open("rb")
        try:
            await file.seek(start)
            more_body = True
            while more_body:
                try:
                    chunk = await file.read(min(self.chunk_size, end - start))
                except (OSError, ValueError):
                    break
                start += len(chunk)
                more_body = len(chunk) == self.chunk_size and start < end
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})
        finally:
            self_handles.unregister(self._tracked)
            self._tracked = None
            await self._quiet_close(file)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only) -> None:
        # Multipart byteranges are never sent to a local video player, but keep
        # the same tracked handle so the registry stays accurate if they are.
        from starlette.responses import MutableHeaders, token_hex

        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        file = await self._tracked_open("rb")
        try:
            for start, end in ranges:
                await send({"type": "http.response.body", "body": header_generator(start, end), "more_body": True})
                await file.seek(start)
                while start < end:
                    try:
                        chunk = await file.read(min(self.chunk_size, end - start))
                    except (OSError, ValueError):
                        break
                    start += len(chunk)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
            await send({"type": "http.response.body", "body": f"--{boundary}--".encode("latin-1"), "more_body": False})
        except (OSError, ValueError):
            pass
        finally:
            self_handles.unregister(self._tracked)
            self._tracked = None
            await self._quiet_close(file)

    @staticmethod
    async def _quiet_close(file) -> None:
        """Close the async file, tolerating a descriptor a delete already closed.

        When ``self_handles.close_for()`` runs mid-stream it closes the raw fd;
        ``aclose()`` then raises ``EBADF`` because anyio still tries to close the
        same underlying ``BufferedReader``. That error is expected and harmless —
        the descriptor is gone, which is exactly what we wanted — so it must not
        escape as a 500 after the delete has already succeeded.
        """
        try:
            await file.aclose()
        except (OSError, ValueError):
            pass


def original_file_response(path: str | Path) -> MediaFileResponse:
    source = Path(path)
    try:
        info = source.stat()
    except OSError as exc:
        raise HTTPException(404, "视频文件不存在，请检查磁盘连接") from exc
    if not stat.S_ISREG(info.st_mode):
        raise HTTPException(404, "视频文件不存在")
    media_type = VIDEO_MEDIA_TYPES.get(source.suffix.lower()) or mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    return MediaFileResponse(source, media_type=media_type, stat_result=info,
                             headers={"Cache-Control": "private, no-cache"})
