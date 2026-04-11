"""文生图服务模块：多模型支持、角色参考图 prompt 工程。

当前支持:
  - wan2.7-image-pro / wan2.7-image  (ImageGeneration SDK, 异步)
  - qwen-image-2.0-pro / qwen-image-plus  (MultiModalConversation SDK, 同步)
  - z-image-turbo  (ImageGeneration SDK, 同步)

后续可在此模块内扩展图像编辑（wan2.6-image）、风格重绘等，不改调用方接口。
"""

from __future__ import annotations

import logging
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from video2text.config.settings import Settings
from video2text.core.ip_manager import IPCharacter, VisualDNA

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 角色参考图 Prompt 工程
# ---------------------------------------------------------------------------

_QUALITY_SUFFIX_ZH = "高清, 精致细节, 专业角色设计, 高品质渲染"

def build_character_image_prompt(
    character: IPCharacter,
    visual_dna: VisualDNA,
) -> str:
    """构建角色参考图的文生图 prompt。

    公式: [身份锚点] + [视觉描述] + [姿态] + [构图] + [背景] + [风格] + [质量]
    """
    parts: list[str] = []

    desc = character.visual_description.strip()
    if desc:
        parts.append(desc)

    parts.append("自然站立, 正面朝向镜头, 表情友善")
    parts.append("全身像, 居中构图, 角色占画面80%")
    parts.append("纯白色背景")

    style_kw = visual_dna.style_keywords.strip()
    if style_kw:
        parts.append(style_kw)

    parts.append(_QUALITY_SUFFIX_ZH)

    return "。".join(parts) + "。"


# ---------------------------------------------------------------------------
# 统一文生图接口
# ---------------------------------------------------------------------------


def generate_image(
    prompt: str,
    settings: Settings,
    *,
    model: str | None = None,
    size: str | None = None,
    negative_prompt: str = "",
    thinking_mode: bool | None = None,
    save_to: str | Path | None = None,
) -> Path:
    """统一文生图接口。根据 model 名称自动选择调用方式，返回本地图片路径。"""
    model = model or settings.image_gen_model
    size = size or settings.image_gen_size
    if thinking_mode is None:
        thinking_mode = settings.image_gen_thinking_mode

    if model.startswith("wan2.7") or model.startswith("wan2.6"):
        url = _generate_wan_image(prompt, settings, model, size, thinking_mode)
    elif model.startswith("qwen-image"):
        url = _generate_qwen_image(prompt, settings, model, size, negative_prompt)
    elif model.startswith("z-image"):
        url = _generate_z_image(prompt, settings, model, size)
    else:
        url = _generate_wan_image(prompt, settings, model, size, thinking_mode)

    if save_to:
        dest = Path(save_to)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="imggen_")
        import os
        os.close(fd)
        dest = Path(tmp)
    _download_image(url, dest)
    log.info("Image saved to %s (model=%s)", dest, model)
    return dest


# ---------------------------------------------------------------------------
# 万相 2.7 / 2.6 系列
# ---------------------------------------------------------------------------


def _generate_wan_image(
    prompt: str,
    settings: Settings,
    model: str,
    size: str,
    thinking_mode: bool,
) -> str:
    """万相系列文生图（ImageGeneration SDK 异步调用）。"""
    from dashscope.aigc.image_generation import ImageGeneration
    from dashscope.api_entities.dashscope_response import Message

    message = Message(role="user", content=[{"text": prompt}])

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": settings.dashscope_api_key,
        "messages": [message],
        "enable_sequential": False,
        "n": 1,
        "size": size,
        "watermark": False,
    }
    if model.startswith("wan2.7"):
        kwargs["thinking_mode"] = thinking_mode

    log.info("Submitting wan image generation: model=%s, size=%s", model, size)
    response = ImageGeneration.async_call(**kwargs)

    if response.status_code != 200:
        raise RuntimeError(
            f"ImageGeneration.async_call failed: {response.code} {response.message}"
        )

    task_id = response.output.task_id
    log.info("Wan image task submitted: %s", task_id)

    result = ImageGeneration.wait(task=response, api_key=settings.dashscope_api_key)

    if result.output.task_status != "SUCCEEDED":
        raise RuntimeError(
            f"Image generation failed: status={result.output.task_status}"
        )

    choices = getattr(result.output, "choices", None)
    if choices and len(choices) > 0:
        content = choices[0].message.content
        if isinstance(content, list) and content:
            for item in content:
                if isinstance(item, dict) and "image" in item:
                    return item["image"]

    results = getattr(result.output, "results", None)
    if results and len(results) > 0:
        url = results[0].get("url") or results[0].get("b64_image", "")
        if url:
            return url

    raise RuntimeError("No image URL in generation response")


# ---------------------------------------------------------------------------
# 千问 qwen-image 系列
# ---------------------------------------------------------------------------


def _generate_qwen_image(
    prompt: str,
    settings: Settings,
    model: str,
    size: str,
    negative_prompt: str,
) -> str:
    """千问文生图（MultiModalConversation SDK 同步调用）。"""
    from dashscope import MultiModalConversation

    wan_size = _convert_size_for_qwen(size)

    kwargs: dict[str, Any] = {
        "api_key": settings.dashscope_api_key,
        "model": model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "watermark": False,
        "size": wan_size,
    }
    if negative_prompt:
        kwargs["negative_prompt"] = negative_prompt
    kwargs["prompt_extend"] = True

    log.info("Submitting qwen image generation: model=%s, size=%s", model, wan_size)
    response = MultiModalConversation.call(**kwargs)

    if response.status_code != 200:
        raise RuntimeError(
            f"MultiModalConversation.call failed: {response.code} {response.message}"
        )

    choices = response.output.choices
    if choices and len(choices) > 0:
        content = choices[0].message.content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "image" in item:
                    return item["image"]

    raise RuntimeError("No image URL in qwen response")


# ---------------------------------------------------------------------------
# z-image-turbo
# ---------------------------------------------------------------------------


def _generate_z_image(
    prompt: str,
    settings: Settings,
    model: str,
    size: str,
) -> str:
    """z-image-turbo 文生图。"""
    from dashscope.aigc.image_generation import ImageGeneration
    from dashscope.api_entities.dashscope_response import Message

    message = Message(role="user", content=[{"text": prompt}])

    log.info("Submitting z-image generation: model=%s", model)
    response = ImageGeneration.call(
        model=model,
        api_key=settings.dashscope_api_key,
        messages=[message],
        n=1,
        size=_convert_size_for_qwen(size),
        watermark=False,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"z-image call failed: {response.code} {response.message}"
        )

    choices = getattr(response.output, "choices", None)
    if choices and len(choices) > 0:
        content = choices[0].message.content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "image" in item:
                    return item["image"]

    results = getattr(response.output, "results", None)
    if results and len(results) > 0:
        url = results[0].get("url") or results[0].get("b64_image", "")
        if url:
            return url

    raise RuntimeError("No image URL in z-image response")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _convert_size_for_qwen(size: str) -> str:
    """将 '2K'/'4K' 等万相格式转为 qwen 兼容的 'WxH' 格式。"""
    s = size.strip().upper()
    if s == "2K":
        return "1024*1024"
    if s == "4K":
        return "2048*2048"
    if s == "1K":
        return "512*512"
    return size


def _download_image(url: str, dest: Path, timeout: float = 120.0) -> None:
    """下载图片到本地路径。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "video2text/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())
