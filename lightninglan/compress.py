"""Compressing what is sent to other peers (zstd), where it pays.

A peer asking for file data says it takes zstd (COMPRESS_HEADER: zstd). The answer then says whether
it is compressed (COMPRESSED_HEADER: zstd): its body is a zstd stream of the bytes asked for, while
Content-Range still counts the bytes themselves. Every request opts in, and a peer that doesn't
(LightningLAN 1.6 and older, the Android app, a browser) is sent the bytes as they are.

Data is compressed on the way out at level 1, which keeps up with gigabit on about a quarter of a
core and saves about a tenth of a typical game library (much more on some games). A file that
doesn't compress (video, archives, most of a big game's packed data) is found out over its first
few megabytes and is then sent as it is, so it costs next to nothing.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Iterator

import zstandard

COMPRESS_HEADER = "X-LightningLAN-Compress"
COMPRESSED_HEADER = "X-LightningLAN-Compressed"
ZSTD = "zstd"
LEVEL = 1
TRIAL_BYTES = 4 * 1024 * 1024  # of a file, compressed before deciding whether it's worth it
MIN_SAVING = 0.05  # a file saving less than this is sent as it is from then on
MAX_FILES = 4096  # files whose compressibility is remembered
READ_CHUNK = 256 * 1024

# Already compressed: not even tried. (Video, audio, pictures, archives, packed game data.)
_NEVER = frozenset("""
    .7z .aac .avi .avif .bik .bk2 .br .bz2 .cab .flac .gif .gz .heic .jpeg .jpg .lz4 .lzma .m4a .m4v .mkv
    .mov .mp3 .mp4 .ogg .opus .png .rar .ucas .usm .webm .webp .wem .wmv .xz .zip .zst
""".split())


def wants_compressed(request) -> bool:
    """A request (to this peer's server) that takes a zstd answer."""
    return request.headers.get(COMPRESS_HEADER) == ZSTD and request.method != "HEAD"


def never_compress(name: str) -> bool:
    dot = name.rfind(".")
    return dot >= 0 and name[dot:].lower() in _NEVER


class Compressibility:
    """How well each file has compressed so far, to stop compressing the ones that don't."""

    def __init__(self):
        self._seen: OrderedDict[str, list[int]] = OrderedDict()  # key -> [bytes in, bytes out]
        self._lock = threading.Lock()

    def worth_it(self, key: str, name: str) -> bool:
        if never_compress(name):
            return False
        with self._lock:
            seen = self._seen.get(key)
            if seen is None:
                return True
            self._seen.move_to_end(key)
            raw, packed = seen
            return raw < TRIAL_BYTES or packed < raw * (1 - MIN_SAVING)

    def record(self, key: str, raw: int, packed: int) -> None:
        with self._lock:
            seen = self._seen.setdefault(key, [0, 0])
            seen[0] += raw
            seen[1] += packed
            self._seen.move_to_end(key)
            while len(self._seen) > MAX_FILES:
                self._seen.popitem(last=False)


def compressed(chunks: Iterator[bytes], on_sent, record) -> Iterator[bytes]:
    """`chunks` as a zstd stream. on_sent(n) is told the size of each piece of output as it goes,
    record(raw, packed) the totals at the end."""
    packer = zstandard.ZstdCompressor(level=LEVEL).compressobj()  # one per transfer: not thread-safe
    raw = packed = 0
    try:
        for chunk in chunks:
            raw += len(chunk)
            out = packer.compress(chunk)
            if out:
                packed += len(out)
                on_sent(len(out))
                yield out
        out = packer.flush()
        if out:
            packed += len(out)
            on_sent(len(out))
            yield out
    finally:
        if raw:
            record(raw, packed)


class Body:
    """The file data in a response from another peer: as it arrived, or decompressed if the peer says
    it is compressed. Output comes in chunks of at most READ_CHUNK bytes, so a small compressed body
    can't make anyone hold a huge one in memory. `raw` counts the bytes as they came over the network."""

    def __init__(self, chunks: Iterator[bytes], is_compressed: bool):
        self._chunks = iter(chunks)
        self._compressed = is_compressed
        self._buffer = b""
        self.raw = 0

    @classmethod
    def of(cls, resp) -> Body:
        """The body of an httpx streaming response."""
        return cls(resp.iter_raw(), resp.headers.get(COMPRESSED_HEADER) == ZSTD)

    def __iter__(self) -> Iterator[bytes]:
        if not self._compressed:
            for chunk in self._chunks:
                self.raw += len(chunk)
                yield chunk
            return
        reader = zstandard.ZstdDecompressor().stream_reader(self, read_size=READ_CHUNK)
        try:
            while data := reader.read(READ_CHUNK):
                yield data
        except zstandard.ZstdError as exc:
            raise ValueError(f"the compressed data doesn't decompress: {exc}") from exc

    def read(self, size: int = -1) -> bytes:
        """For the decompressor: compressed bytes, as they arrive."""
        while not self._buffer:
            chunk = next(self._chunks, None)
            if chunk is None:
                return b""
            self.raw += len(chunk)
            self._buffer = chunk
        if size < 0 or size >= len(self._buffer):
            out, self._buffer = self._buffer, b""
        else:
            out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out
