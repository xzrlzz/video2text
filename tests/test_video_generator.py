"""Tests for video chunking (no API)."""

import unittest

from storyboard import Shot
from video_generator import chunk_shots_by_max_duration, format_subject_prompt_block


class ChunkTest(unittest.TestCase):
    def test_single_chunk_when_under_limit(self) -> None:
        shots = [
            Shot(
                1,
                "00:00:00",
                "00:00:05",
                5.0,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
            Shot(
                2,
                "00:00:05",
                "00:00:10",
                5.0,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
        ]
        chunks = chunk_shots_by_max_duration(shots, 15.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 2)

    def test_splits_when_over_15s(self) -> None:
        def s(sid: int, dur: float) -> Shot:
            return Shot(
                sid,
                "00:00:00",
                "00:00:00",
                dur,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )

        # 10+5=15 仍属一段，下一段 10s 单独成段 → 共 2 段
        shots = [s(1, 10.0), s(2, 5.0), s(3, 10.0)]
        chunks = chunk_shots_by_max_duration(shots, 15.0)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 2)
        self.assertEqual(len(chunks[1]), 1)

    def test_subject_block_empty(self) -> None:
        self.assertEqual(format_subject_prompt_block([]), "")


if __name__ == "__main__":
    unittest.main()
