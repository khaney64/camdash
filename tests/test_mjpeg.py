import asyncio
import socketserver
import threading

import pytest

from camdash import mjpeg
from camdash.mjpeg import MjpegRelay, jpeg_frames, mjpeg_frames, multipart_frame, response_chunks
from camdash.cameras import open_streaming_http


JPEG_1 = b"\xff\xd8one\xff\xd9"
JPEG_2 = b"\xff\xd8two\xff\xd9"


class FakeResponse:
    def __init__(self, chunks, release: threading.Event | None = None, hold_open: bool = False):
        self.chunks = chunks
        self.release = release
        self.hold_open = hold_open
        self.closed = False

    def iter_content(self, _chunk_size):
        if self.release:
            self.release.wait(2)
        yield from self.chunks
        while self.hold_open and not self.closed:
            threading.Event().wait(0.01)

    def close(self):
        self.closed = True


def test_jpeg_frames_handles_split_boundaries_and_multiple_frames():
    chunks = [b"junk\xff", b"\xd8one\xff\xd9\xff\xd8t", b"wo\xff\xd9tail"]
    assert list(jpeg_frames(chunks)) == [JPEG_1, JPEG_2]
    assert multipart_frame(JPEG_1).startswith(b"--camdashframe\r\nContent-Type: image/jpeg")


def test_mjpeg_frames_uses_multipart_content_length_without_jpeg_end_marker():
    frame = b"\xff\xd8payload-without-end-marker"
    part = (
        b"--camera-boundary\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
    )
    assert list(mjpeg_frames([part[:23], part[23:61], part[61:]])) == [frame]


def test_mjpeg_frames_accepts_thingino_oversized_content_length():
    part = (
        b"--camera-boundary\r\nContent-Type: image/jpeg\r\nContent-Length: 45310\r\n\r\n"
        + JPEG_1 + b"\r\n"
    )
    assert list(mjpeg_frames([part])) == [JPEG_1]


def test_mjpeg_frames_recovers_after_missing_boundary_before_headers(monkeypatch):
    monkeypatch.setattr(mjpeg, "MAX_FRAME_BUFFER", 32)
    boundary = b"--camera-boundary"

    def part(frame: bytes) -> bytes:
        return boundary + b"\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame

    chunks = [part(b"one"), b"x" * 64, b"--camera-", b"boundary\r\nContent-Length: 3\r\n\r\ntwo"]
    assert list(mjpeg_frames(chunks)) == [b"one", b"two"]


def test_mjpeg_frames_recovers_after_missing_boundary_in_body(monkeypatch):
    monkeypatch.setattr(mjpeg, "MAX_FRAME_BUFFER", 32)
    boundary = b"--camera-boundary"
    malformed = boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + b"x" * 64
    valid = b"--camera-boundary\r\nContent-Length: 3\r\n\r\ntwo"
    assert list(mjpeg_frames([malformed, valid[:9], valid[9:]])) == [b"two"]


def test_response_chunks_prefers_available_byte_reader():
    class Raw:
        chunks = [b"one", b"two", b""]

        def read1(self, _size):
            return self.chunks.pop(0)

    class Response:
        raw = Raw()

        def iter_content(self, _size):
            raise AssertionError("iter_content should not be used when read1 is available")

    assert list(response_chunks(Response())) == [b"one", b"two"]


def test_streaming_http_reader_returns_before_stream_ends():
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while self.rfile.readline() not in {b"\r\n", b"\n", b""}:
                pass
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace\r\n\r\n" + JPEG_1
            )
            self.wfile.flush()

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = open_streaming_http(
            f"http://127.0.0.1:{server.server_address[1]}/stream", None, 2, 2,
        )
        assert response.status_code == 200
        assert response.raw.read1(1024) == JPEG_1
        response.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_streaming_http_reader_decodes_chunked_camera_response():
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while self.rfile.readline() not in {b"\r\n", b"\n", b""}:
                pass
            self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            for part in (JPEG_1[:4], JPEG_1[4:]):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = open_streaming_http(
            f"http://127.0.0.1:{server.server_address[1]}/stream", None, 2, 2,
        )
        assert list(jpeg_frames(response_chunks(response))) == [JPEG_1]
        response.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.asyncio
async def test_relay_shares_one_upstream_and_fans_out_frames():
    release = threading.Event()
    response = FakeResponse([JPEG_1, JPEG_2], release, hold_open=True)
    opens = 0

    def open_stream():
        nonlocal opens
        opens += 1
        return response

    relay = MjpegRelay("test", [open_stream], retry_seconds=0.01)
    token_1, queue_1 = relay.subscribe()
    token_2, queue_2 = relay.subscribe()
    release.set()

    assert await asyncio.wait_for(asyncio.to_thread(queue_1.get), 2) in (JPEG_1, JPEG_2)
    assert await asyncio.wait_for(asyncio.to_thread(queue_2.get), 2) in (JPEG_1, JPEG_2)
    assert opens == 1

    relay.unsubscribe(token_1)
    relay.unsubscribe(token_2)
    await asyncio.sleep(0.05)
    assert response.closed is False
    relay.close()
    await asyncio.sleep(0.05)
    assert response.closed is True


@pytest.mark.asyncio
async def test_relay_reconnects_to_fallback_source():
    failed = FakeResponse([])
    recovered = FakeResponse([JPEG_2], hold_open=True)
    relay = MjpegRelay("fallback", [lambda: failed, lambda: recovered], retry_seconds=0.01)
    token, queue = relay.subscribe()

    assert await asyncio.wait_for(asyncio.to_thread(queue.get), 2) == JPEG_2
    assert failed.closed is True

    relay.unsubscribe(token)
    relay.close()
