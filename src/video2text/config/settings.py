"""Configuration for DashScope APIs and defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video2text.utils.paths import get_data_config_dir, get_project_root

_FALSY = frozenset(("0", "false", "no", "off", ""))


def _as_str_tuple(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v.strip(),) if v.strip() else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if x is not None and str(x).strip())
    return ()


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in _FALSY
    return bool(v)


def _as_float(v: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


@dataclass(frozen=True)
class GenerationExtras:
    """视频生成阶段默认：主体描述与万相参考素材（可被 CLI 覆盖/追加）。"""

    subject_descriptions: tuple[str, ...] = ()
    reference_urls: tuple[str, ...] = ()
    reference_video_urls: tuple[str, ...] = ()
    reference_video_descriptions: tuple[str, ...] = ()
    max_segment_seconds: float = 15.0
    require_reference: bool = True
    per_chunk_reference_filter: bool = True


def load_generation_extras(config_path: str | Path | None = None) -> GenerationExtras:
    cfg = load_config_file(config_path)
    subjects = cfg.get("subject_descriptions") or cfg.get("subjects")
    ref_u = cfg.get("reference_urls") or cfg.get("reference_image_urls")
    return GenerationExtras(
        subject_descriptions=_as_str_tuple(subjects),
        reference_urls=_as_str_tuple(ref_u),
        reference_video_urls=_as_str_tuple(cfg.get("reference_video_urls")),
        reference_video_descriptions=_as_str_tuple(cfg.get("reference_video_descriptions")),
        max_segment_seconds=_as_float(cfg.get("max_segment_seconds"), 15.0, lo=2.0, hi=15.0),
        require_reference=_as_bool(cfg.get("require_reference"), True),
        per_chunk_reference_filter=_as_bool(cfg.get("per_chunk_reference_filter"), True),
    )


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/api/v1"
    vision_model: str = "qwen3.6-plus"
    theme_story_model: str = ""
    video_gen_model: str = "wan2.7-t2v"
    video_ref_model: str = "wan2.7-r2v"
    default_resolution: str = "1280*720"
    max_video_base64_mb: float = 9.0
    scene_detect_threshold: float = 27.0
    analysis_fps: float = 2.0


def _default_config_search_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.getenv("V2T_CONFIG", "").strip()
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "config.json")
    paths.append(get_project_root() / "config.json")
    paths.append(get_data_config_dir() / "config.json")
    return paths


def load_config_file(config_path: str | Path | None) -> dict[str, Any]:
    """Load JSON config. If path is None, use V2T_CONFIG / ./config.json / project config.json."""
    if config_path is not None:
        p = Path(config_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return raw

    for p in _default_config_search_paths():
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"配置文件根节点必须是 JSON 对象: {p}")
            return raw
    return {}


def _env_or_file(
    env_name: str,
    file_cfg: dict[str, Any],
    file_key: str,
    default: str | float | int | None = None,
) -> Any:
    """环境变量优先；否则用配置文件；再否则 default。"""
    ev = os.getenv(env_name, "").strip()
    if ev:
        return ev
    if file_key in file_cfg and file_cfg[file_key] is not None:
        v = file_cfg[file_key]
        if isinstance(v, str) and not v.strip():
            v = None
        if v is not None:
            if isinstance(default, float) and isinstance(v, (int, float)):
                return float(v)
            if isinstance(default, int) and isinstance(v, (int, float)):
                return int(v)
            return v
    return default


def load_settings(config_path: str | Path | None = None) -> Settings:
    """
    加载设置。密钥与端点可从 config.json 读取；同名环境变量始终覆盖配置文件。
    """
    file_cfg = load_config_file(config_path)

    key = _env_or_file("DASHSCOPE_API_KEY", file_cfg, "dashscope_api_key", "")
    if isinstance(key, str):
        key = key.strip()
    if not key:
        raise RuntimeError(
            "未配置 API Key：请在 config.json 中设置 dashscope_api_key，"
            "或设置环境变量 DASHSCOPE_API_KEY。"
            "可将 config.example.json 复制为 config.json 后填写。"
            "文档：https://help.aliyun.com/zh/model-studio/get-api-key"
        )

    base_url = str(
        _env_or_file("V2T_BASE_URL", file_cfg, "base_url", Settings.base_url)
    )
    api_base = str(
        _env_or_file(
            "DASHSCOPE_HTTP_BASE",
            file_cfg,
            "dashscope_api_base",
            Settings.dashscope_api_base,
        )
    )

    vision = str(
        _env_or_file("V2T_VISION_MODEL", file_cfg, "vision_model", Settings.vision_model)
    )
    theme_story = str(
        _env_or_file(
            "V2T_THEME_MODEL", file_cfg, "theme_story_model", Settings.theme_story_model
        )
    ).strip()
    gen = str(
        _env_or_file("V2T_GEN_MODEL", file_cfg, "video_gen_model", Settings.video_gen_model)
    )
    ref_gen = str(
        _env_or_file(
            "V2T_REF_MODEL", file_cfg, "video_ref_model", Settings.video_ref_model
        )
    )
    resolution = str(
        _env_or_file(
            "V2T_RESOLUTION", file_cfg, "default_resolution", Settings.default_resolution
        )
    )

    max_b64 = _env_or_file(
        "V2T_MAX_VIDEO_BASE64_MB", file_cfg, "max_video_base64_mb", 9.0
    )
    threshold = _env_or_file(
        "V2T_SCENE_THRESHOLD", file_cfg, "scene_detect_threshold", 27.0
    )
    fps = _env_or_file("V2T_ANALYSIS_FPS", file_cfg, "analysis_fps", 2.0)

    return Settings(
        dashscope_api_key=key,
        base_url=base_url,
        dashscope_api_base=api_base,
        vision_model=vision,
        theme_story_model=theme_story,
        video_gen_model=gen,
        video_ref_model=ref_gen,
        default_resolution=resolution,
        max_video_base64_mb=float(max_b64),
        scene_detect_threshold=float(threshold),
        analysis_fps=float(fps),
    )
