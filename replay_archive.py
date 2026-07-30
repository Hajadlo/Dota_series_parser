"""Streaming decompression for Valve replay archives.

Valve replay URLs retain the ``.dem.bz2`` suffix, but archives created since
2026-07-27 can be Zstandard-compressed. Detect the format from magic bytes so
both new and legacy replays remain supported.
"""

import bz2
import os
from collections.abc import Iterable
from itertools import chain
from pathlib import Path

import zstandard as zstd


BZIP2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def decompress_replay_chunks(chunks: Iterable[bytes], dest_path: str | os.PathLike[str]) -> None:
    """Decompress streamed replay archive chunks atomically into ``dest_path``."""
    iterator = iter(chunks)
    prefix = bytearray()
    while len(prefix) < len(ZSTD_MAGIC):
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        if chunk:
            prefix.extend(chunk)

    if bytes(prefix).startswith(ZSTD_MAGIC):
        decompressor = zstd.ZstdDecompressor().decompressobj()
    elif bytes(prefix).startswith(BZIP2_MAGIC):
        decompressor = bz2.BZ2Decompressor()
    else:
        magic = bytes(prefix[:4]).hex() or "empty"
        raise ValueError(f"Unsupported replay compression (magic bytes: {magic})")

    destination = Path(dest_path)
    partial = Path(f"{destination}.part")
    try:
        with partial.open("wb") as output:
            for chunk in chain((bytes(prefix),), iterator):
                if chunk:
                    output.write(decompressor.decompress(chunk))
        if not decompressor.eof:
            raise EOFError("Replay archive ended before the compressed stream completed")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
