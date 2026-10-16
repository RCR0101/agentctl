"""Incremental JSONL reading: byte offsets, partial trailing lines, rotation. Never rereads."""

import os
from dataclasses import dataclass
from pathlib import Path

HEAD_BYTES = 64 << 10  # startup: metadata from the head...
TAIL_BYTES = 512 << 10  # ...and recent events from the tail; never the whole file.


@dataclass
class FileTail:
    path: Path
    offset: int = 0
    ino: int = 0
    buf: bytes = b""
    mtime: float = 0.0

    def skip_to_tail(self) -> list[bytes]:
        """For a big file at startup: return the head's complete lines (for metadata) and move
        the offset to the start of the last TAIL_BYTES. Small files are read whole later."""
        st = os.stat(self.path)
        self.ino = st.st_ino
        if st.st_size <= HEAD_BYTES + TAIL_BYTES:
            return []
        with self.path.open("rb") as f:
            head = f.read(HEAD_BYTES).split(b"\n")[:-1]
            f.seek(st.st_size - TAIL_BYTES)
            f.readline()  # skip to a line boundary
            self.offset = f.tell()
        return head

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
