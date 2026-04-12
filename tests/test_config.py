"""Tests for config file loading."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from video2text.config.settings import (
    Settings,
    filter_task_overrides,
    load_config_file,
    load_settings,
    load_settings_from_dict,
    normalize_user_config_delta,
    resolve_effective_settings_dict,
    resolve_theme_idea_model,
    resolve_theme_story_model,
)


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

    def test_resolve_theme_story_model_requires_config(self) -> None:
        s = Settings(dashscope_api_key="k", theme_story_model="")
        with self.assertRaises(ValueError):
            resolve_theme_story_model(s)
        self.assertEqual(
            resolve_theme_story_model(s, override="qwen-turbo"),
            "qwen-turbo",
        )
        s2 = Settings(dashscope_api_key="k", theme_story_model="  qwen-max  ")
        self.assertEqual(resolve_theme_story_model(s2), "qwen-max")

    def test_resolve_theme_idea_model_requires_config(self) -> None:
        s = Settings(dashscope_api_key="k", theme_idea_model="")
        with self.assertRaises(ValueError):
            resolve_theme_idea_model(s)
        s2 = Settings(dashscope_api_key="k", theme_idea_model="qwen-turbo")
        self.assertEqual(resolve_theme_idea_model(s2), "qwen-turbo")

    def test_normalize_user_config_delta_keeps_only_diff_and_allowed(self) -> None:
        global_cfg = {
            "vision_model": "qwen3.6-plus",
            "video_gen_model": "wan2.7-t2v",
            "dashscope_api_key": "sk-global",
        }
        user_cfg = {
            "vision_model": "qwen3.6-plus",  # same as global -> should drop
            "video_gen_model": "wan2.7-custom",  # user diff -> keep
            "task_ttl_days": 99,  # system-only -> should drop
            "dashscope_api_key": "  sk-user-own  ",  # user key -> keep after trim
        }
        normalized = normalize_user_config_delta(global_cfg, user_cfg)
        self.assertEqual(
            normalized,
            {
                "video_gen_model": "wan2.7-custom",
                "dashscope_api_key": "sk-user-own",
            },
        )

    def test_resolve_effective_settings_with_sources(self) -> None:
        global_cfg = {
            "dashscope_api_key": "sk-global",
            "vision_model": "global-vision",
            "default_resolution": "720*1280",
        }
        user_cfg = {
            "dashscope_api_key": "sk-user",
            "vision_model": "user-vision",
        }
        task_overrides = {
            "resolution": "1080*1920",
            "max_segment_seconds": 12,
            "min_shots": 10,  # 非 settings 字段，不进入 settings dict
        }
        try:
            os.environ["V2T_VISION_MODEL"] = "env-vision"
            cfg, sources = resolve_effective_settings_dict(
                global_cfg=global_cfg,
                user_cfg=user_cfg,
                task_overrides=task_overrides,
                enforce_user_api_key=True,
            )
        finally:
            os.environ.pop("V2T_VISION_MODEL", None)

        self.assertEqual(cfg["dashscope_api_key"], "sk-user")
        self.assertEqual(sources["dashscope_api_key"], "user_persistent")
        self.assertEqual(cfg["vision_model"], "env-vision")
        self.assertEqual(sources["vision_model"], "env")
        self.assertEqual(cfg["default_resolution"], "1080*1920")
        self.assertEqual(sources["default_resolution"], "task_transient")
        self.assertEqual(cfg["max_segment_seconds"], 12)
        self.assertEqual(sources["max_segment_seconds"], "task_transient")

    def test_resolve_effective_requires_user_api_key(self) -> None:
        cfg, _ = resolve_effective_settings_dict(
            global_cfg={"dashscope_api_key": "sk-global"},
            user_cfg={},
            task_overrides={},
            enforce_user_api_key=True,
        )
        with self.assertRaises(RuntimeError):
            load_settings_from_dict(cfg)

    def test_filter_task_overrides(self) -> None:
        filtered = filter_task_overrides(
            {
                "min_shots": 8,
                "max_shots": 16,
                "resolution": "1080*1920",
                "vision_model": "ignored",
            }
        )
        self.assertEqual(filtered["min_shots"], 8)
        self.assertEqual(filtered["max_shots"], 16)
        self.assertEqual(filtered["resolution"], "1080*1920")
        self.assertNotIn("vision_model", filtered)


if __name__ == "__main__":
    unittest.main()
