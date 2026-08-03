from __future__ import annotations

import logging
import queue as thread_queue
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any


LOG = logging.getLogger("camdash.mjpeg")
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_FRAME_BUFFER = 8 * 1024 * 1024


def jpeg_frames(chunks: Iterable[bytes]) -> Iterable[bytes]:
    """Extract complete JPEG images from an arbitrary MJPEG byte stream."""
    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        while True:
            start = buffer.find(JPEG_SOI)
            if start < 0:
                if len(buffer) > 1:
                    del buffer[:-1]
                break
            if start:
                del buffer[:start]
            end = buffer.find(JPEG_EOI, 2)
            if end < 0:
                if len(buffer) > MAX_FRAME_BUFFER:
                    buffer.clear()
                break
            end += len(JPEG_EOI)
            yield bytes(buffer[:end])
            del buffer[:end]


def mjpeg_frames(chunks: Iterable[bytes]) -> Iterable[bytes]:
    """Extract multipart Content-Length frames, with raw-JPEG fallback."""
    buffer = bytearray()
    boundary: bytes | None = None
    raw_jpeg = False
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        while True:
            if boundary is None and not raw_jpeg:
                soi = buffer.find(JPEG_SOI)
                marker = buffer.find(b"--")
                if soi >= 0 and (marker < 0 or soi < marker):
                    raw_jpeg = True
                elif marker >= 0:
                    line_end = buffer.find(b"\r\n", marker)
                    if line_end < 0:
                        break
                    boundary = bytes(buffer[marker:line_end])
                    del buffer[:marker]
                elif len(buffer) > 4096:
                    raw_jpeg = True
                else:
                    break

            if raw_jpeg:
                start = buffer.find(JPEG_SOI)
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(JPEG_EOI, start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                end += len(JPEG_EOI)
                yield bytes(buffer[start:end])
                del buffer[:end]
                continue

            while buffer.startswith(b"\r\n"):
                del buffer[:2]
            if boundary is None:
                break
            marker = buffer.find(boundary)
            if marker < 0:
                break
            if marker:
                del buffer[:marker]
            header_end = buffer.find(b"\r\n\r\n")
            if header_end < 0:
                break
            headers = bytes(buffer[len(boundary):header_end]).decode("iso-8859-1")
            content_length = None
            for line in headers.split("\r\n"):
                name, separator, value = line.partition(":")
                if separator and name.strip().lower() == "content-length":
                    try:
                        content_length = int(value.strip())
                    except ValueError:
                        content_length = None
                    break
            body_start = header_end + 4
            if content_length is not None:
                body_end = body_start + content_length
                if len(buffer) < body_end:
                    jpeg_end = buffer.find(JPEG_EOI, body_start + 2)
                    if jpeg_end >= 0:
                        jpeg_end += len(JPEG_EOI)
                        yield bytes(buffer[body_start:jpeg_end])
                        del buffer[:jpeg_end]
                        continue
                    break
                yield bytes(buffer[body_start:body_end])
                del buffer[:body_end]
                continue
            next_marker = buffer.find(boundary, body_start)
            if next_marker < 0:
                break
            yield bytes(buffer[body_start:next_marker]).rstrip(b"\r\n")
            del buffer[:next_marker]


def multipart_frame(frame: bytes, boundary: str = "camdashframe") -> bytes:
    header = (
        f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode("ascii")
    return header + frame + b"\r\n"


def response_chunks(response: Any, chunk_size: int = 4 * 1024) -> Iterable[bytes]:
    """Read available streaming bytes without waiting for a full nominal chunk."""
    raw = getattr(response, "raw", None)
    read1 = getattr(raw, "read1", None)
    if callable(read1):
        while True:
            chunk = read1(chunk_size)
            if not chunk:
                return
            yield chunk
    else:
        yield from response.iter_content(chunk_size)


class MjpegRelay:
    """Own one upstream stream and fan complete, latest-only frames to viewers."""

    def __init__(
        self,
        name: str,
        open_streams: Sequence[Callable[[], Any]],
        retry_seconds: float = 1.0,
    ):
        if not open_streams:
            raise ValueError("at least one MJPEG source is required")
        self.name = name
        self._open_streams = tuple(open_streams)
        self._retry_seconds = retry_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._response: Any = None
        self._next_token = 1
        self._subscribers: dict[int, thread_queue.Queue[bytes]] = {}

    def subscribe(self) -> tuple[int, thread_queue.Queue[bytes]]:
        queue: thread_queue.Queue[bytes] = thread_queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise RuntimeError("MJPEG relay is closed")
            token = self._next_token
            self._next_token += 1
            self._subscribers[token] = queue
            self._stop.clear()
            if self._thread is None:
                self._start_worker_locked()
        return token, queue

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)

    def close(self) -> None:
        response = None
        with self._lock:
            self._closed = True
            self._subscribers.clear()
            self._stop.set()
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _start_worker_locked(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"mjpeg-{self.name}", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        source_index = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._closed:
                        return
                opener = self._open_streams[source_index % len(self._open_streams)]
                source_index += 1
                response = None
                try:
                    response = opener()
                    LOG.debug("MJPEG relay %s connected upstream", self.name)
                    with self._lock:
                        self._response = response
                    delivered = False
                    for frame in mjpeg_frames(response_chunks(response)):
                        delivered = True
                        if self._stop.is_set():
                            return
                        self._broadcast(frame)
                    if not self._stop.is_set():
                        reason = "ended" if delivered else "returned no frames"
                        raise RuntimeError(f"upstream {reason}")
                except Exception as exc:
                    if not self._stop.is_set():
                        LOG.warning("MJPEG relay %s reconnecting after %s", self.name, type(exc).__name__)
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                    with self._lock:
                        if self._response is response:
                            self._response = None
                self._stop.wait(self._retry_seconds)
        finally:
            restart = False
            with self._lock:
                self._thread = None
                restart = not self._closed
                if restart:
                    self._stop.clear()
                    self._start_worker_locked()

    def _broadcast(self, frame: bytes) -> None:
        with self._lock:
            subscribers = list(self._subscribers.values())
        for queue in subscribers:
            self._offer_latest(queue, frame)

    @staticmethod
    def _offer_latest(queue: thread_queue.Queue[bytes], frame: bytes) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except thread_queue.Empty:
                pass
        try:
            queue.put_nowait(frame)
        except thread_queue.Full:
            pass
