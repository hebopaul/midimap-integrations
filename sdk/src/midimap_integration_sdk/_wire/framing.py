"""Incremental newline framing shared by the host engine and the author SDK.

Part of the pure wire-contract set (`policy`, `contracts`, `validation`,
`framing`): standard library only, relative imports only, nothing at import.
The author SDK ships a byte-identical copy of these four modules.
"""
from __future__ import annotations

from collections.abc import Iterator

from . import policy
from .contracts import DomainError, ErrorCode


class LineDecoder:
    """Incremental LF framing with the size bound applied before buffering.

    A frame, including its LF (and an optional CR), may be at most `max_bytes`.
    UTF-8 is decoded per complete frame, so characters split across reads are
    fine. Raises PROTOCOL_ERROR as soon as an unterminated line cannot fit.
    """

    def __init__(self, max_bytes: int = policy.MESSAGE_MAX_BYTES):
        self.max_bytes = max_bytes
        self._buffer = bytearray()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                break
            if len(self._buffer) + end - start + 1 > self.max_bytes:
                raise self._too_long()
            line = bytes(self._buffer) + chunk[start:end]
            self._buffer.clear()
            start = end + 1
            yield line[:-1] if line.endswith(b"\r") else line
        if len(self._buffer) + len(chunk) - start >= self.max_bytes:
            raise self._too_long()
        self._buffer += chunk[start:]

    def _too_long(self) -> DomainError:
        self._buffer.clear()
        return DomainError(ErrorCode.PROTOCOL_ERROR, (),
                           f"exceeds the maximum size of {self.max_bytes} bytes")
