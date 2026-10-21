"""How files are split into pieces for swarm downloads: piece sizes, piece hashes and have-maps.

A file larger than one piece is split into equal pieces (the last one may be shorter). The piece
size depends only on the file size, so every peer computes the same pieces for the same file.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

MIN_PIECE_SIZE = 1024 * 1024
MAX_PIECE_SIZE = 16 * 1024 * 1024
TARGET_PIECES = 2048
DIGEST_SIZE = 32
HASH_CHUNK = 1024 * 1024  # divides every piece size


def piece_size_for(size: int) -> int:
    """A power of two from 1 MiB to 16 MiB, chosen so a file has at most ~2048 pieces."""
    piece = MIN_PIECE_SIZE
    while piece < MAX_PIECE_SIZE and size > piece * TARGET_PIECES:
        piece *= 2
    return piece


def piece_count(size: int) -> int:
    return -(-size // piece_size_for(size))


def swarmable(size: int) -> bool:
    """Files of more than one piece are split up; smaller ones are always sent whole."""
    return size > MIN_PIECE_SIZE


def hash_file(path: Path, stop: threading.Event | None = None) -> tuple[str, bytes]:
    """SHA-256 of the whole file and, for files of more than one piece, of each piece (one pass).

    Returns (hex digest, concatenated 32-byte piece digests or b"").
    """
    whole = hashlib.sha256()
    digests = bytearray()
    buf = bytearray(HASH_CHUNK)
    view = memoryview(buf)
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        split = swarmable(size)
        piece_size = piece_size_for(size)
        piece, in_piece = hashlib.sha256(), 0
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("hashing cancelled")
            n = f.readinto(buf)
            if not n:
                break
            whole.update(view[:n])
            if not split:
                continue
            offset = 0
            while offset < n:
                take = min(n - offset, piece_size - in_piece)
                piece.update(view[offset:offset + take])
                in_piece += take
                offset += take
                if in_piece == piece_size:
                    digests += piece.digest()
                    piece, in_piece = hashlib.sha256(), 0
        if split and in_piece:
            digests += piece.digest()
    return whole.hexdigest(), bytes(digests)


@dataclass(frozen=True)
class PieceInfo:
    """A file's piece hashes: what a downloader checks every piece against."""

    file_id: str
    size: int
    hashes: bytes  # 32-byte SHA-256 digests, concatenated

    @property
    def piece_size(self) -> int:
        return piece_size_for(self.size)

    @property
    def count(self) -> int:
        return len(self.hashes) // DIGEST_SIZE

    def digest(self, index: int) -> bytes:
        return self.hashes[index * DIGEST_SIZE:(index + 1) * DIGEST_SIZE]

    def span(self, index: int) -> tuple[int, int]:
        """(offset, length) of a piece in the file."""
        start = index * self.piece_size
        return start, min(self.piece_size, self.size - start)

    def to_json(self) -> dict:
        return {
            "file_id": self.file_id,
            "size": self.size,
            "piece_size": self.piece_size,
            "hashes": base64.b64encode(self.hashes).decode("ascii"),
        }

    @classmethod
    def from_json(cls, data: object, file_id: str, size: int) -> PieceInfo:
        """Validate another peer's piece list for the file we expect. Raises ValueError."""
        if not isinstance(data, dict):
            raise ValueError("piece list is not a JSON object")
        if data.get("file_id") != file_id or data.get("size") != size:
            raise ValueError("piece list is for a different file")
        if data.get("piece_size") != piece_size_for(size):
            raise ValueError("unexpected piece size")
        raw = data.get("hashes")
        if not isinstance(raw, str):
            raise ValueError("piece list has no hashes")
        hashes = base64.b64decode(raw.encode("ascii"), validate=True)
        if len(hashes) != piece_count(size) * DIGEST_SIZE:
            raise ValueError("wrong number of piece hashes")
        return cls(file_id, size, hashes)


def encode_bits(have: Sequence[int]) -> str:
    """A have-map (one 0/1 entry per piece) as base64 of the packed bits, first piece in the high bit."""
    packed = bytearray((len(have) + 7) // 8)
    for i, bit in enumerate(have):
        if bit:
            packed[i >> 3] |= 0x80 >> (i & 7)
    return base64.b64encode(bytes(packed)).decode("ascii")


def decode_bits(text: str, count: int) -> bytearray:
    """The inverse of encode_bits. Raises ValueError if it doesn't describe `count` pieces."""
    packed = base64.b64decode(text.encode("ascii"), validate=True)
    if len(packed) != (count + 7) // 8:
        raise ValueError("have-map has the wrong length")
    return bytearray(1 if packed[i >> 3] & (0x80 >> (i & 7)) else 0 for i in range(count))
