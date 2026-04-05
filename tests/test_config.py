"""Tests for config file loading."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from video2text.config.settings import load_config_file, load_settings


class ConfigLoadTest(unittest.TestCase):
    def test_load_settings_from_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "dashscope_api_key": "sk-test-from-file",
                    "vision_model": "qwen3.6-plus",
                    "scene_detect_threshold": 30.5,
                },
                f,
            )
            path = f.name
        try:
            s = load_settings(path)
            self.assertEqual(s.dashscope_api_key, "sk-test-from-file")
            self.assertEqual(s.scene_detect_threshold, 30.5)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_env_overrides_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump({"dashscope_api_key": "sk-file"}, f)
            path = f.name
        try:
            os.environ["DASHSCOPE_API_KEY"] = "sk-from-env"
            s = load_settings(path)
            self.assertEqual(s.dashscope_api_key, "sk-from-env")
        finally:
            del os.environ["DASHSCOPE_API_KEY"]
            Path(path).unlink(missing_ok=True)

    def test_load_config_file_explicit_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_config_file("/nonexistent/video2text-config.json")


if __name__ == "__main__":
    unittest.main()
