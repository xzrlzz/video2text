"""Tests for theme → storyboard shot building (no API)."""

import unittest

from video2text.core.theme import _build_shots_from_theme_items


class ThemeShotsTest(unittest.TestCase):
    def test_builds_timeline_and_dialogue(self) -> None:
        items = [
            {
                "shot_type": "中景",
                "camera_movement": "平视固定",
                "scene_description": "室内",
                "character_action": "站立",
                "dialogue": "阿珍：「你来了。」",
                "mood": "平静",
                "lighting": "柔光",
                "audio_description": "安静",
                "generation_prompt": "Medium shot, interior, soft light",
                "duration_sec": 2.0,
            },
            {
                "shot_type": "近景",
                "camera_movement": "缓慢推近",
                "scene_description": "面部",
                "character_action": "点头",
                "dialogue": "",
                "mood": "期待",
                "lighting": "侧光",
                "audio_description": "",
                "generation_prompt": "Close-up slow push-in",
                "duration_sec": 3.0,
            },
        ]
        shots = _build_shots_from_theme_items(items)
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].shot_id, 1)
        self.assertEqual(shots[0].start_time, "00:00:00")
        self.assertEqual(shots[0].end_time, "00:00:02")
        self.assertEqual(shots[0].duration, 2.0)
        self.assertIn("阿珍", shots[0].dialogue)
        self.assertEqual(shots[1].start_time, "00:00:02")
        self.assertEqual(shots[1].end_time, "00:00:05")


if __name__ == "__main__":
    unittest.main()
