import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi import HTTPException
from app import media_files, self_handles
from app.media_delivery import original_file_response


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='avhub-range-')
        self.source = Path(self.temp.name) / 'sample.TS'
        self.data = bytes(range(256)) * 10000
        self.source.write_bytes(self.data)
        self.mtime = self.source.stat().st_mtime_ns

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(self.source.stat().st_mtime_ns, self.mtime)
        self.temp.cleanup()

    async def deliver(self, method='GET', **headers):
        messages = []
        async def send(message): messages.append(message)
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        scope = {'type': 'http', 'method': method, 'headers': [
            (key.replace('_', '-').encode(), value.encode()) for key, value in headers.items()
        ]}
        await original_file_response(self.source)(scope, receive, send)
        start = messages[0]
        body = b''.join(message.get('body', b'') for message in messages[1:])
        return start['status'], dict(start['headers']), body, messages

    async def test_full_delivery_uses_explicit_type_and_bounded_large_reads(self):
        status, headers, body, messages = await self.deliver()
        self.assertEqual(status, 200)
        self.assertEqual(headers[b'content-type'], b'video/mp2t')
        self.assertEqual(headers[b'accept-ranges'], b'bytes')
        self.assertEqual(body, self.data)
        self.assertEqual(len(messages[1]['body']), 1024 * 1024)
        self.assertLessEqual(max(len(message.get('body', b'')) for message in messages), 1024 * 1024)
        for extension, expected in [('.mkv', 'video/x-matroska'), ('.m2ts', 'video/mp2t'), ('.mp4', 'video/mp4')]:
            path = self.source.with_suffix(extension)
            path.write_bytes(b'test')
            self.assertEqual(original_file_response(path).media_type, expected)

    async def test_random_open_ended_suffix_and_head_ranges(self):
        for header, start, end in [('bytes=1000000-2000000', 1000000, 2000000),
                                   ('bytes=2559980-', 2559980, len(self.data) - 1),
                                   ('bytes=-17', len(self.data) - 17, len(self.data) - 1)]:
            status, headers, body, _ = await self.deliver(range=header)
            self.assertEqual(status, 206)
            self.assertEqual(body, self.data[start:end + 1])
            self.assertEqual(headers[b'content-range'], f'bytes {start}-{end}/{len(self.data)}'.encode())
            head_status, head_headers, head_body, _ = await self.deliver(method='HEAD', range=header)
            self.assertEqual(head_status, 206)
            self.assertEqual(head_headers[b'content-length'], headers[b'content-length'])
            self.assertEqual(head_body, b'')
        status, headers, body, _ = await self.deliver(range='bytes=9999999-')
        self.assertEqual(status, 416)
        self.assertEqual(headers[b'content-range'], f'bytes */{len(self.data)}'.encode())
        self.assertEqual(body, b'')

    async def test_if_range_and_multipart_keep_standard_semantics(self):
        _, headers, _, _ = await self.deliver(method='HEAD')
        for validator in [headers[b'etag'].decode(), headers[b'last-modified'].decode()]:
            status, _, body, _ = await self.deliver(range='bytes=10-19', if_range=validator)
            self.assertEqual((status, body), (206, self.data[10:20]))
        status, _, body, _ = await self.deliver(range='bytes=10-19', if_range='"stale"')
        self.assertEqual((status, body), (200, self.data))
        status, headers, body, _ = await self.deliver(range='bytes=10-19,40-49')
        self.assertEqual(status, 206)
        self.assertIn(b'multipart/byteranges', headers[b'content-type'])
        self.assertIn(self.data[10:20], body)
        self.assertIn(self.data[40:50], body)
        self.assertEqual(int(headers[b'content-length']), len(body))

    def test_missing_and_directory_sources_are_rejected(self):
        for path in [self.source.parent, self.source.with_name('absent.mkv')]:
            with self.assertRaises(HTTPException) as error:
                original_file_response(path)
            self.assertEqual(error.exception.status_code, 404)


class SelfHandleTests(unittest.IsolatedAsyncioTestCase):
    """The backend must not lock a video it is streaming against its own delete.

    A direct-play response keeps the file open for as long as the player's Range
    connection lives, so deleting a playing video used to fail with WinError 32.
    These tests pin the contract that closes that hole: the live descriptor is
    registered, ``close_for`` really frees the file, and the interrupted response
    unwinds without raising.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='avhub-selflock-')
        self.source = Path(self.temp.name) / 'playing.mp4'
        self.source.write_bytes(bytes(range(256)) * 8192)  # 2 MiB, several chunks

    def tearDown(self):
        self.temp.cleanup()

    async def stream_and_freeze(self):
        """Start a GET, stop on the first body chunk, and return the controls."""
        started = asyncio.Event()
        release = threading.Event()
        sent = []

        async def send(message):
            sent.append(message)
            if message['type'] == 'http.response.body' and message.get('body') and not started.is_set():
                started.set()
                await asyncio.to_thread(release.wait)

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        scope = {'type': 'http', 'method': 'GET', 'http_version': '1.1', 'scheme': 'http',
                 'path': '/media/1/file', 'raw_path': b'/media/1/file', 'query_string': b'',
                 'root_path': '', 'headers': [(b'host', b'127.0.0.1')],
                 'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 80), 'extensions': {}}
        task = asyncio.create_task(original_file_response(self.source)(scope, receive, send))
        await asyncio.wait_for(started.wait(), timeout=5)
        return task, release

    async def test_live_stream_registers_then_delete_closes_it(self):
        task, release = await self.stream_and_freeze()
        try:
            self.assertTrue(self_handles.open_on(self.source))
            self.assertEqual(media_files.occupancy(self.source), 'locked')

            # The delete flow's fix: stop our own reader, then the file is free.
            self.assertEqual(self_handles.close_for(self.source), 1)
            self.assertFalse(self_handles.open_on(self.source))
            self.assertEqual(media_files.occupancy(self.source), 'free')
        finally:
            release.set()
            # The fd is already closed, so the frozen reader must unwind and the
            # response must end cleanly instead of surfacing the EBADF.
            await asyncio.wait_for(task, timeout=5)

    async def test_interrupted_stream_finishes_without_raising(self):
        """The fd-close must surface as a clean end-of-response, never a 500."""
        started = asyncio.Event()

        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body') and not started.is_set():
                started.set()
                # Close our own handle at the exact moment the body is live.
                self_handles.close_for(self.source)

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        scope = {'type': 'http', 'method': 'GET', 'http_version': '1.1', 'scheme': 'http',
                 'path': '/media/1/file', 'raw_path': b'/media/1/file', 'query_string': b'',
                 'root_path': '', 'headers': [(b'host', b'127.0.0.1')],
                 'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 80), 'extensions': {}}
        await asyncio.wait_for(original_file_response(self.source)(scope, receive, send), timeout=5)
        self.assertTrue(started.is_set())
        self.assertFalse(self_handles.open_on(self.source))

    async def test_head_request_does_not_register_a_handle(self):
        async def send(message):
            pass

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        scope = {'type': 'http', 'method': 'HEAD', 'http_version': '1.1', 'scheme': 'http',
                 'path': '/media/1/file', 'raw_path': b'/media/1/file', 'query_string': b'',
                 'root_path': '', 'headers': [(b'host', b'127.0.0.1')],
                 'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 80), 'extensions': {}}
        await original_file_response(self.source)(scope, receive, send)
        self.assertFalse(self_handles.open_on(self.source))

    def test_short_name_and_long_name_resolve_to_one_key(self):
        """8.3 short paths and their long form must address the same entry."""
        handle = os.open(self.source, os.O_RDONLY)
        try:
            entry = self_handles.register(handle, self.source)
            self.assertTrue(self_handles.open_on(self.source))
            self.assertTrue(self_handles.open_on(Path(self.source).resolve()))
        finally:
            self_handles.unregister(entry)
            os.close(handle)
        self.assertFalse(self_handles.open_on(self.source))
