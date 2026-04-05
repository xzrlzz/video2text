"""Unit tests for storyboard serialization (no API calls)."""

import json
import tempfile
import unittest
from pathlib import Path

from video2text.core.storyboard import Shot, StoryboardDocument


class StoryboardTest(unittest.TestCase):
    def test_roundtrip_json(self) -> None:
        doc = StoryboardDocument(
            title="测试片",
            synopsis="主角走进房间。",
            characters="主角A",
            source_video="/tmp/x.mp4",
            shots=[
                Shot(
                    shot_id=1,
                    start_time="00:00:00",
                    end_time="00:00:03",
                    duration=3.0,
                    shot_type="中景",
                    camera_movement="固定",
                    scene_description="室内",
                    character_action="行走",
                    dialogue="",
                    mood="平静",
                    lighting="暖光",
                    audio_description="脚步声",
                    generation_prompt="测试prompt",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sb.json"
            doc.save_json(p)
            loaded = StoryboardDocument.load_json(p)
        self.assertEqual(loaded.title, doc.title)
        self.assertEqual(len(loaded.shots), 1)
        self.assertEqual(loaded.shots[0].shot_type, "中景")
        self.assertEqual(loaded.shots[0].generation_prompt, "测试prompt")

    def test_markdown_contains_title(self) -> None:
        doc = StoryboardDocument(title="T", shots=[])
        self.assertIn("T", doc.to_markdown())


if __name__ == "__main__":
    unittest.main()
