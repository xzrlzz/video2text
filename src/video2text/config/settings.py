"""Configuration for DashScope APIs and defaults."""

from __future__ import annotations

import json
import os
from dataclasses import MISSING, dataclass, fields
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
    """任务级参数：主体描述与万相参考素材（仅从 CLI 参数或任务文件加载）。"""

    subject_descriptions: tuple[str, ...] = ()
    reference_urls: tuple[str, ...] = ()
    reference_video_urls: tuple[str, ...] = ()
    reference_video_descriptions: tuple[str, ...] = ()


def load_generation_extras(data: dict[str, Any] | None = None) -> GenerationExtras:
    """从 dict 加载任务级生成参数。不再从全局 config.json 读取。"""
    cfg = data or {}
    subjects = cfg.get("subject_descriptions") or cfg.get("subjects")
    ref_u = cfg.get("reference_urls") or cfg.get("reference_image_urls")
    return GenerationExtras(
        subject_descriptions=_as_str_tuple(subjects),
        reference_urls=_as_str_tuple(ref_u),
        reference_video_urls=_as_str_tuple(cfg.get("reference_video_urls")),
        reference_video_descriptions=_as_str_tuple(cfg.get("reference_video_descriptions")),
    )


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/api/v1"
    vision_model: str = "qwen3.6-plus"
    theme_story_model: str = ""
    theme_idea_model: str = ""
    video_gen_model: str = "wan2.7-t2v"
    video_ref_model: str = "wan2.7-r2v"
    default_resolution: str = "720*1280"
    image_gen_model: str = "wan2.7-image-pro"
    image_gen_thinking_mode: bool = True
    image_gen_size: str = "2K"
    max_video_base64_mb: float = 9.0
    scene_detect_threshold: float = 27.0
    analysis_fps: float = 2.0
    max_segment_seconds: float = 15.0
    require_reference: bool = True
    per_chunk_reference_filter: bool = True
    task_ttl_days: int = 7
    max_workers: int = 4
    video_watermark: bool = True
    video_prompt_extend: bool = True
    enforce_english_audio_text: bool = True
    llm_light_model: str = "qwen3.5-flash"
    video_max_workers: int = 4
    voice_mode: str = "native"          # "native" | "pipeline" | "silent"
    tts_model: str = "cosyvoice-v3-flash"
    tts_provider: str = "cosyvoice"     # "cosyvoice" | "fish_speech"


SETTINGS_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(Settings)
)

SETTINGS_FIELD_ORDER: tuple[str, ...] = tuple(f.name for f in fields(Settings))

SETTINGS_DEFAULTS: dict[str, Any] = {}
for _f in fields(Settings):
    if _f.default is not MISSING:
        SETTINGS_DEFAULTS[_f.name] = _f.default
    elif _f.default_factory is not MISSING:
        SETTINGS_DEFAULTS[_f.name] = _f.default_factory()
    else:
        # 目前仅 dashscope_api_key 为无默认必填项。
        SETTINGS_DEFAULTS[_f.name] = ""


# 系统级配置：仅管理员/全局配置控制，用户配置文件不允许长期覆盖。
SYSTEM_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "base_url",
        "dashscope_api_base",
        "task_ttl_days",
        "max_workers",
        "video_watermark",
        "video_prompt_extend",
        "enforce_english_audio_text",
    }
)

# 用户必须显式配置的密钥字段（不从全局默认继承）。
SECRET_USER_REQUIRED_FIELDS: frozenset[str] = frozenset({"dashscope_api_key"})

# 用户长期偏好字段（重启后仍生效）。
USER_PERSISTENT_FIELDS: frozenset[str] = frozenset(
    f
    for f in SETTINGS_FIELDS
    if f not in SYSTEM_ONLY_FIELDS and f not in SECRET_USER_REQUIRED_FIELDS
)

# 任务级临时覆盖字段（仅本任务生效，不写用户配置）。
TASK_TRANSIENT_FIELDS: frozenset[str] = frozenset(
    {
        "style",
        "min_shots",
        "max_shots",
        "max_segment_seconds",
        "resolution",
        "workers",
        "theme_model",
        "model",
        "threshold",
        "segment_scenes",
        "skip_consolidate",
        "text_only_video",
        "require_reference",
        "no_require_reference",
    }
)

TASK_OVERRIDE_TO_SETTINGS_FIELD: dict[str, str] = {
    "max_segment_seconds": "max_segment_seconds",
    "resolution": "default_resolution",
}

SETTINGS_ENV_MAP: dict[str, str] = {
    "dashscope_api_key": "DASHSCOPE_API_KEY",
    "base_url": "V2T_BASE_URL",
    "dashscope_api_base": "DASHSCOPE_HTTP_BASE",
    "vision_model": "V2T_VISION_MODEL",
    "theme_story_model": "V2T_THEME_MODEL",
    "theme_idea_model": "V2T_THEME_IDEA_MODEL",
    "video_gen_model": "V2T_GEN_MODEL",
    "video_ref_model": "V2T_REF_MODEL",
    "default_resolution": "V2T_RESOLUTION",
    "image_gen_model": "IMAGE_GEN_MODEL",
    "image_gen_thinking_mode": "IMAGE_GEN_THINKING_MODE",
    "image_gen_size": "IMAGE_GEN_SIZE",
    "max_video_base64_mb": "V2T_MAX_VIDEO_BASE64_MB",
    "scene_detect_threshold": "V2T_SCENE_THRESHOLD",
    "analysis_fps": "V2T_ANALYSIS_FPS",
    "max_segment_seconds": "V2T_MAX_SEGMENT_SECONDS",
    "require_reference": "V2T_REQUIRE_REFERENCE",
    "per_chunk_reference_filter": "V2T_PER_CHUNK_REF_FILTER",
    "task_ttl_days": "V2T_TASK_TTL_DAYS",
    "max_workers": "V2T_MAX_WORKERS",
    "video_watermark": "V2T_VIDEO_WATERMARK",
    "video_prompt_extend": "V2T_VIDEO_PROMPT_EXTEND",
    "enforce_english_audio_text": "V2T_ENFORCE_ENGLISH_AUDIO_TEXT",
    "llm_light_model": "V2T_LLM_LIGHT_MODEL",
    "video_max_workers": "V2T_VIDEO_MAX_WORKERS",
    "voice_mode": "V2T_VOICE_MODE",
    "tts_model": "V2T_TTS_MODEL",
    "tts_provider": "V2T_TTS_PROVIDER",
}


def allowed_user_config_fields() -> frozenset[str]:
    """用户配置文件可持久化字段（含用户必填密钥）。"""
    return USER_PERSISTENT_FIELDS | SECRET_USER_REQUIRED_FIELDS


def allowed_admin_config_fields() -> frozenset[str]:
    """管理员可配置字段（全量 Settings 字段）。"""
    return SETTINGS_FIELDS


def allowed_task_override_fields() -> frozenset[str]:
    """任务级可临时覆盖字段。"""
    return TASK_TRANSIENT_FIELDS


def filter_task_overrides(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """过滤出允许的任务级临时覆盖字段。"""
    if not data:
        return {}
    out: dict[str, Any] = {}
    for k in TASK_TRANSIENT_FIELDS:
        if k in data and data[k] is not None:
            out[k] = data[k]
    return out


def _normalize_layer_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def normalize_user_config_delta(
    global_cfg: dict[str, Any] | None,
    user_cfg: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    归一化用户配置为“差异集”：
    - 仅保留允许的用户字段；
    - 普通字段与全局相同则不保存；
    - dashscope_api_key 作为用户显式密钥，非空则保留。
    """
    g = global_cfg or {}
    u = user_cfg or {}
    out: dict[str, Any] = {}
    for k in allowed_user_config_fields():
        if k not in u:
            continue
        raw_v = u.get(k)
        if k in SECRET_USER_REQUIRED_FIELDS:
            v = _normalize_layer_value(raw_v)
            if v is not None:
                out[k] = v
            continue
        v = _normalize_layer_value(raw_v)
        if v is None:
            continue
        gv = _normalize_layer_value(g.get(k))
        if gv == v:
            continue
        out[k] = v
    return out


def resolve_effective_settings_dict(
    global_cfg: dict[str, Any] | None = None,
    user_cfg: dict[str, Any] | None = None,
    task_overrides: dict[str, Any] | None = None,
    *,
    enforce_user_api_key: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    统一分层解析（default < global < user_persistent < task_transient < env）。
    返回最终配置与字段来源映射。
    """
    g = global_cfg or {}
    u = user_cfg or {}
    t = task_overrides or {}

    effective = {k: SETTINGS_DEFAULTS.get(k, "") for k in SETTINGS_FIELD_ORDER}
    sources: dict[str, str] = {k: "default" for k in SETTINGS_FIELD_ORDER}

    for k in SETTINGS_FIELD_ORDER:
        if k == "dashscope_api_key" and enforce_user_api_key:
            # Web 用户模式下不继承全局密钥。
            continue
        if k not in g:
            continue
        v = _normalize_layer_value(g.get(k))
        if v is None:
            continue
        effective[k] = v
        sources[k] = "global"

    allowed_user = allowed_user_config_fields()
    for k in SETTINGS_FIELD_ORDER:
        if k not in allowed_user or k not in u:
            continue
        v = _normalize_layer_value(u.get(k))
        if v is None:
            continue
        effective[k] = v
        sources[k] = "user_persistent"

    for tk, tv in filter_task_overrides(t).items():
        sk = TASK_OVERRIDE_TO_SETTINGS_FIELD.get(tk, tk)
        if sk not in SETTINGS_FIELDS:
            continue
        v = _normalize_layer_value(tv)
        if v is None:
            continue
        effective[sk] = v
        sources[sk] = "task_transient"

    for sk, env_name in SETTINGS_ENV_MAP.items():
        ev = os.getenv(env_name, "").strip()
        if not ev:
            continue
        effective[sk] = ev
        sources[sk] = "env"

    return effective, sources


def _default_config_search_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.getenv("V2T_CONFIG", "").strip()
    if env:
        paths.append(Path(env).expanduser())
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


def _build_settings_from_dict(file_cfg: dict[str, Any]) -> Settings:
    """从 dict 构造 Settings，环境变量仍可覆盖。"""
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
    theme_idea = str(
        _env_or_file(
            "V2T_THEME_IDEA_MODEL", file_cfg, "theme_idea_model", Settings.theme_idea_model
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

    img_model = str(
        _env_or_file(
            "IMAGE_GEN_MODEL", file_cfg, "image_gen_model", Settings.image_gen_model
        )
    )
    img_thinking_raw = _env_or_file(
        "IMAGE_GEN_THINKING_MODE", file_cfg, "image_gen_thinking_mode", True
    )
    img_thinking = _as_bool(img_thinking_raw, default=True)
    img_size = str(
        _env_or_file("IMAGE_GEN_SIZE", file_cfg, "image_gen_size", Settings.image_gen_size)
    )

    max_b64 = _env_or_file(
        "V2T_MAX_VIDEO_BASE64_MB", file_cfg, "max_video_base64_mb",
        Settings.max_video_base64_mb,
    )
    threshold = _env_or_file(
        "V2T_SCENE_THRESHOLD", file_cfg, "scene_detect_threshold", 27.0
    )
    fps = _env_or_file("V2T_ANALYSIS_FPS", file_cfg, "analysis_fps", 2.0)
    max_seg = _env_or_file(
        "V2T_MAX_SEGMENT_SECONDS", file_cfg, "max_segment_seconds",
        Settings.max_segment_seconds,
    )
    req_ref = _as_bool(
        _env_or_file("V2T_REQUIRE_REFERENCE", file_cfg, "require_reference", None),
        default=Settings.require_reference,
    )
    chunk_filter = _as_bool(
        _env_or_file(
            "V2T_PER_CHUNK_REF_FILTER", file_cfg, "per_chunk_reference_filter", None
        ),
        default=Settings.per_chunk_reference_filter,
    )
    ttl = _env_or_file(
        "V2T_TASK_TTL_DAYS", file_cfg, "task_ttl_days", Settings.task_ttl_days
    )
    workers_raw = _env_or_file(
        "V2T_MAX_WORKERS", file_cfg, "max_workers", Settings.max_workers
    )
    vid_watermark = _as_bool(
        _env_or_file("V2T_VIDEO_WATERMARK", file_cfg, "video_watermark", None),
        default=Settings.video_watermark,
    )
    vid_prompt_ext = _as_bool(
        _env_or_file("V2T_VIDEO_PROMPT_EXTEND", file_cfg, "video_prompt_extend", None),
        default=Settings.video_prompt_extend,
    )
    enforce_en_audio = _as_bool(
        _env_or_file(
            "V2T_ENFORCE_ENGLISH_AUDIO_TEXT",
            file_cfg,
            "enforce_english_audio_text",
            None,
        ),
        default=Settings.enforce_english_audio_text,
    )
    llm_light = str(
        _env_or_file("V2T_LLM_LIGHT_MODEL", file_cfg, "llm_light_model", Settings.llm_light_model)
    )
    video_max_workers = int(
        _env_or_file("V2T_VIDEO_MAX_WORKERS", file_cfg, "video_max_workers", Settings.video_max_workers)
    )
    voice_mode = str(
        _env_or_file("V2T_VOICE_MODE", file_cfg, "voice_mode", Settings.voice_mode)
    )
    tts_model = str(
        _env_or_file("V2T_TTS_MODEL", file_cfg, "tts_model", Settings.tts_model)
    )
    tts_provider = str(
        _env_or_file("V2T_TTS_PROVIDER", file_cfg, "tts_provider", Settings.tts_provider)
    )

    return Settings(
        dashscope_api_key=key,
        base_url=base_url,
        dashscope_api_base=api_base,
        vision_model=vision,
        theme_story_model=theme_story,
        theme_idea_model=theme_idea,
        video_gen_model=gen,
        video_ref_model=ref_gen,
        default_resolution=resolution,
        image_gen_model=img_model,
        image_gen_thinking_mode=img_thinking,
        image_gen_size=img_size,
        max_video_base64_mb=float(max_b64),
        scene_detect_threshold=float(threshold),
        analysis_fps=float(fps),
        max_segment_seconds=_as_float(max_seg, Settings.max_segment_seconds, lo=2.0, hi=15.0),
        require_reference=req_ref,
        per_chunk_reference_filter=chunk_filter,
        task_ttl_days=int(ttl) if ttl is not None else Settings.task_ttl_days,
        max_workers=max(1, int(workers_raw)) if workers_raw is not None else Settings.max_workers,
        video_watermark=vid_watermark,
        video_prompt_extend=vid_prompt_ext,
        enforce_english_audio_text=enforce_en_audio,
        llm_light_model=llm_light,
        video_max_workers=max(1, min(8, video_max_workers)),
        voice_mode=voice_mode,
        tts_model=tts_model,
        tts_provider=tts_provider,
    )


def load_settings(config_path: str | Path | None = None) -> Settings:
    """
    加载设置。密钥与端点可从 config.json 读取；同名环境变量始终覆盖配置文件。
    """
    return _build_settings_from_dict(load_config_file(config_path))


def load_settings_from_dict(cfg: dict[str, Any]) -> Settings:
    """从已合并的 dict 构造 Settings（不读文件），供 Web 层在内存合并后调用。"""
    return _build_settings_from_dict(cfg)


THEME_STORY_MODEL_REQUIRED_MSG = (
    "请先在配置中填写 theme_story_model，或设置环境变量 V2T_THEME_MODEL。"
)
THEME_IDEA_MODEL_REQUIRED_MSG = (
    "请先在配置中填写 theme_idea_model，或设置环境变量 V2T_THEME_IDEA_MODEL。"
)


def resolve_theme_story_model(settings: Settings, *, override: str | None = None) -> str:
    """主题分镜、翻译、主体提取等文案类 LLM 使用的模型；不再回退到 vision_model。"""
    if override is not None:
        m = override.strip()
        if m:
            return m
    m = (settings.theme_story_model or "").strip()
    if not m:
        raise ValueError(THEME_STORY_MODEL_REQUIRED_MSG)
    return m


def resolve_theme_idea_model(settings: Settings) -> str:
    """「生成创意」专用模型；不再回退到 theme_story_model。"""
    m = (settings.theme_idea_model or "").strip()
    if not m:
        raise ValueError(THEME_IDEA_MODEL_REQUIRED_MSG)
    return m


def resolve_light_model(settings: Settings) -> str:
    """轻量级任务使用的模型（默认 qwen3.5-flash），回退到 theme_story_model。"""
    m = (settings.llm_light_model or "").strip()
    if m:
        return m
    return resolve_theme_story_model(settings)
