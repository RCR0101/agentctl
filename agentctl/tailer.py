"""Incremental JSONL reading: byte offsets, partial trailing lines, rotation. Never rereads."""

import os
import time
from dataclasses import dataclass
from pathlib import Path

HEAD_BYTES = 64 << 10  # startup: metadata from the head...
TAIL_BYTES = 512 << 10  # ...and recent events from the tail; never the whole file.
# Sessions untouched for a while only need their title, status and last reply at startup:
# a small tail for those, and the full TAIL_BYTES when someone opens one (`hydrate`).
SHALLOW_TAIL_BYTES = 32 << 10
SHALLOW_AFTER = 6 * 3600  # seconds since the file last changed


@dataclass
class FileTail:
    path: Path
    offset: int = 0
    ino: int = 0
    buf: bytes = b""
    mtime: float = 0.0
    shallow: bool = False  # read from a small tail at startup; `deepen` reads the full one

    def skip_to_tail(self, tail: int | None = None) -> list[bytes]:
        """For a big file at startup: return the head's complete lines (for metadata) and move
        the offset to the start of the last `tail` bytes (TAIL_BYTES, or SHALLOW_TAIL_BYTES for
        a file untouched for SHALLOW_AFTER). Small files are read whole later."""
        st = os.stat(self.path)
        self.ino = st.st_ino
        if tail is None:
            old = time.time() - st.st_mtime > SHALLOW_AFTER
            tail = SHALLOW_TAIL_BYTES if old else TAIL_BYTES
        if st.st_size <= HEAD_BYTES + tail:
            return []
        self.shallow = tail < TAIL_BYTES
        with self.path.open("rb") as f:
            # Include the line containing the tail boundary. Skipping forwards can discard
            # the latest reply entirely when a single JSONL record is larger than the tail.
            boundary = st.st_size - tail
            while boundary > 0:
                start = max(0, boundary - HEAD_BYTES)
                f.seek(start)
                chunk = f.read(boundary - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    boundary = start + newline + 1
                    break
                boundary = start
            self.offset = boundary
            f.seek(0)
            head = f.read(min(HEAD_BYTES, self.offset)).split(b"\n")[:-1]
        return head

    def deepen(self) -> bool:
        """After a shallow start: move back to the full tail so the next read_lines returns
        everything recent again (the caller drops the events it had). False if not shallow."""
        if not self.shallow:
            return False
        self.shallow, self.offset, self.buf = False, 0, b""
        self.skip_to_tail(TAIL_BYTES)
        return True

    def read_lines(self) -> list[bytes]:
        """Complete lines appended since the last call."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        if st.st_ino != self.ino or st.st_size < self.offset:  # replaced or truncated
            self.ino, self.offset, self.buf = st.st_ino, 0, b""
        self.mtime = st.st_mtime
        if st.st_size == self.offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        self.offset += len(chunk)
        lines = (self.buf + chunk).split(b"\n")
        self.buf = lines.pop()  # partial trailing line waits for the next write
        return [ln for ln in lines if ln.strip()]
