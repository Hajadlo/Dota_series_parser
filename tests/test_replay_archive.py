import bz2
import tempfile
import unittest
from pathlib import Path

try:
    from compression import zstd as _zstd

    def zstd_compress(data: bytes) -> bytes:
        return _zstd.compress(data)
except ImportError:
    import zstandard as _zstd

    def zstd_compress(data: bytes) -> bytes:
        return _zstd.ZstdCompressor().compress(data)

from replay_archive import decompress_replay_chunks


class ReplayArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = (b"PBDEMS2 replay fixture\n" * 10_000) + b"end"

    @staticmethod
    def chunks(data: bytes, size: int):
        return (data[i : i + size] for i in range(0, len(data), size))

    def assert_decompresses(self, archive: bytes, chunk_size: int) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "replay.dem"
            decompress_replay_chunks(self.chunks(archive, chunk_size), destination)
            self.assertEqual(self.payload, destination.read_bytes())
            self.assertFalse(Path(f"{destination}.part").exists())

    def test_decompresses_legacy_bzip2_replay(self) -> None:
        self.assert_decompresses(bz2.compress(self.payload), chunk_size=7)

    def test_decompresses_zstandard_replay_when_magic_is_split_across_chunks(self) -> None:
        self.assert_decompresses(zstd_compress(self.payload), chunk_size=1)

    def test_rejects_unknown_compression_without_leaving_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "replay.dem"
            with self.assertRaisesRegex(ValueError, "Unsupported replay compression"):
                decompress_replay_chunks([b"not-a-replay"], destination)
            self.assertFalse(destination.exists())
            self.assertFalse(Path(f"{destination}.part").exists())


if __name__ == "__main__":
    unittest.main()
