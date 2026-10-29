"""Running a worker on its standard streams: the packaged entry point.

`run_worker()` first takes private duplicates of stdin and stdout for the
protocol, then points file descriptor 0 at the null device and descriptor 1
at stderr, and only then imports and constructs the provider through its
factory. From then on `print()`, logging and even native writes to stdout
land on stderr, which MIDImap drains and normally discards; they can never
corrupt the protocol stream. Log records are cut to a bounded length.
Nothing here touches the process's streams at import time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from ._wire import policy
from ._wire.contracts import PackageManifest
from ._wire.validation import validate_manifest
from .provider import Provider
from .runtime import ExitCode, WorkerRuntime

log = logging.getLogger("midimap_integration_sdk")


def load_manifest(path: str | os.PathLike) -> PackageManifest:
    """Read and fully validate a package's `manifest.json`."""
    return validate_manifest(Path(path).read_bytes())


class PipeTransport:
    """Worker transport over blocking binary pipes.

    Uses exactly two daemon threads (one reader, one writer), started on first
    use, never one per request. At most `buffered_chunks` reads wait for the
    runtime. The threads are not joined at exit, so a host that keeps a pipe
    open cannot keep the worker process alive.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO, *, buffered_chunks: int = 4,
                 chunk_bytes: int = policy.STREAM_READ_CHUNK_BYTES):
        self._reader = reader
        self._writer = writer
        self._chunk_bytes = chunk_bytes
        self._credit = threading.Semaphore(buffered_chunks)
        self._incoming: asyncio.Queue[bytes] | None = None
        self._outgoing: queue.SimpleQueue = queue.SimpleQueue()
        self._threads_started = False

    def _start(self) -> None:
        if self._threads_started:
            return
        self._threads_started = True
        loop = asyncio.get_running_loop()
        self._incoming = asyncio.Queue()
        threading.Thread(target=self._read_forever, args=(loop,), daemon=True,
                         name="midimap-protocol-reader").start()
        threading.Thread(target=self._write_forever, daemon=True,
                         name="midimap-protocol-writer").start()

    async def read(self, max_bytes: int) -> bytes:
        self._start()
        data = await self._incoming.get()
        self._credit.release()
        return data

    async def write(self, data: bytes) -> None:
        self._start()
        future = asyncio.get_running_loop().create_future()
        self._outgoing.put((bytes(data), future))
        await future

    def _read_forever(self, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            self._credit.acquire()
            try:
                data = self._reader.read(self._chunk_bytes) or b""
            except OSError:
                data = b""
            try:
                loop.call_soon_threadsafe(self._incoming.put_nowait, data)
            except RuntimeError:                 # the loop has closed
                return
            if not data:
                return

    def _write_forever(self) -> None:
        while True:
            data, future = self._outgoing.get()
            error = None
            try:
                view = memoryview(data)
                while view:                       # a blocking raw pipe writes >= 1 byte
                    view = view[self._writer.write(view):]
                self._writer.flush()
            except (OSError, ValueError) as caught:
                error = caught
            loop = future.get_loop()
            try:
                loop.call_soon_threadsafe(_settle, future, error)
            except RuntimeError:
                return


def _settle(future: asyncio.Future, error: BaseException | None) -> None:
    if future.done():
        return
    if error is None:
        future.set_result(None)
    else:
        future.set_exception(OSError("the protocol stream is closed"))


def claim_standard_streams() -> tuple[BinaryIO, BinaryIO]:
    """Take stdin/stdout for the protocol and send everything else to stderr.

    On failure every descriptor taken so far is closed, descriptor 0 is
    restored if it had already been replaced, and the error is raised.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    owned: list[int] = []
    files: list[BinaryIO] = []
    stdin_replaced = False
    stdout_replaced = False
    try:
        owned.append(os.dup(0))
        owned.append(os.dup(1))
        null = os.open(os.devnull, os.O_RDONLY)
        owned.append(null)
        os.dup2(null, 0)
        stdin_replaced = True
        os.dup2(2, 1)
        stdout_replaced = True
        os.close(owned.pop())                      # the null descriptor
        for fd, mode in zip(owned, ("rb", "wb")):
            files.append(os.fdopen(fd, mode, buffering=0))
        owned.clear()                              # now owned by `files`
    except BaseException:
        if stdin_replaced:
            try:
                os.dup2(owned[0], 0)
            except OSError:
                pass
        if stdout_replaced:
            try:
                os.dup2(owned[1], 1)
            except OSError:
                pass
        wrapped = {item.fileno() for item in files}
        for item in (*files, *(fd for fd in owned if fd not in wrapped)):
            try:
                item.close() if hasattr(item, "close") else os.close(item)
            except OSError:
                pass
        raise
    sys.stdout = sys.stderr
    return files[0], files[1]


def package_manifest(entry_file: str | os.PathLike) -> Path:
    """The `manifest.json` of the package this worker belongs to.

    In a frozen worker (`worker/<name>.exe`) it is next to `worker/`; when
    run from source it is next to `entry_file` (pass `__file__`).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "manifest.json"
    return Path(entry_file).resolve().parent / "manifest.json"


class _BoundedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        limit = policy.WORKER_LOG_RECORD_MAX_CHARS
        return text if len(text) <= limit else text[:limit - 3] + "..."


def configure_logging(level: int = logging.WARNING) -> None:
    """Send all logging to stderr with records cut to a bounded length."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_BoundedFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def run_worker(factory: Callable[[], Provider], manifest_path: str | os.PathLike, *,
               log_level: int = logging.WARNING) -> int:
    """Serve MIDImap on this process's standard streams; returns an exit code.

    The protocol streams are claimed *before* `factory()` runs, so import
    your provider module inside the factory: anything it prints (or writes
    to descriptor 1) while importing or constructing goes to stderr and can
    never corrupt the protocol. Typical entry point::

        def create_provider():
            from my_provider import MyProvider
            return MyProvider()

        sys.exit(run_worker(create_provider, package_manifest(__file__)))
    """
    if isinstance(factory, Provider) or not callable(factory):
        raise TypeError("run_worker needs a factory that creates the provider, so the "
                        "protocol streams are claimed before provider code runs")
    reader, writer = claim_standard_streams()
    configure_logging(log_level)
    try:
        provider = factory()
        runtime = WorkerRuntime(provider, load_manifest(manifest_path),
                                transport=PipeTransport(reader, writer))
    except Exception as error:                     # author code: report the type only
        log.error("the worker could not start (%s)", type(error).__name__)
        reader.close()
        writer.close()
        return int(ExitCode.UNCLEAN)
    loop = asyncio.new_event_loop()
    try:
        return int(loop.run_until_complete(runtime.run()))
    finally:
        loop.close()
