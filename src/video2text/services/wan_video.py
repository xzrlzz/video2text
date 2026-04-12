"""万相 wan2.7 文生/参考生视频：官方 HTTP 异步协议（SDK 文档注明 2.7 需走此方式）。"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from dashscope.utils.oss_utils import check_and_upload_local

from video2text.config.settings import Settings
from video2text.services.media_normalize import normalize_local_reference_path


def video_synthesis_post_url(settings: Settings) -> str:
    b = settings.dashscope_api_base.rstrip("/")
    return f"{b}/services/aigc/video-generation/video-synthesis"


def tasks_get_url(settings: Settings, task_id: str) -> str:
    b = settings.dashscope_api_base.rstrip("/")
    return f"{b}/tasks/{task_id}"


def _post_json(url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # 与 dashscope VideoSynthesis 一致：本地上传后得到 oss:// URL，必须在网关侧解析后才能做安检与推理；
    # 缺此头时常见误报 InvalidParameter.DataInspection（并非素材格式问题）。
    hdrs: dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "X-DashScope-Async": "enable",
    }
    if b"oss://" in data:
        hdrs["X-DashScope-OssResourceResolve"] = "enable"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 400 and "DataInspection" in err_body:
            err_body += (
                "\n（说明：若请求体含 oss:// 参考地址，需带请求头 X-DashScope-OssResourceResolve: enable"
                "（本客户端已自动添加）。仍失败时再检查素材：参考图已尽量转为 JPEG、视频为 H.264 MP4；"
                "或见环境变量 V2T_LIGHT_REFERENCE_IMAGE / V2T_REFERENCE_IMAGE_MAX_SIDE。）"
            )
        raise RuntimeError(f"万相 HTTP {e.code}: {err_body}") from e
    return json.loads(raw)


def _get_json(url: str, api_key: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_size_to_wan27_resolution_and_ratio(size: str) -> tuple[str, str]:
    """
    将 default_resolution（如 1280*720）映射为 wan2.7 的 resolution 档位 + ratio。
    """
    s = (size or "1280*720").lower().replace("×", "*")
    parts = s.split("*")
    w, h = 1280, 720
    if len(parts) == 2:
        try:
            w = int(parts[0].strip())
            h = int(parts[1].strip())
        except ValueError:
            pass
    pixels = w * h
    resolution = "1080P" if pixels >= 1920 * 1080 * 0.95 else "720P"
    g = math.gcd(max(w, 1), max(h, 1))
    rw, rh = w // g, h // g
    ratio = f"{rw}:{rh}"
    if ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4"):
        ratio = "16:9"
    return resolution, ratio


def model_max_duration_seconds(model: str) -> int:
    if "r2v" in model:
        return 10
    return 15


def uses_wan27_http(model: str) -> bool:
    return model.startswith("wan2.7")


def preflight_reference_urls_for_r2v(
    settings: Settings,
    reference_image_urls: list[str],
    reference_video_urls: list[str],
) -> tuple[list[str], list[str]]:
    """
    把参考图/视频的本地路径转为公网可访 URL；已是 https / oss:// 则保持不变。
    多段生成前只调用一次，各段复用同一批 URL，避免并行重复上传，有利于多段间主体一致。
    """
    model = settings.video_ref_model
    api_key = settings.dashscope_api_key
    cert = None
    out_v: list[str] = []
    for u in reference_video_urls:
        s = str(u).strip()
        if not s:
            continue
        local = normalize_local_reference_path(s, kind="video")
        _, url, cert = check_and_upload_local(model, local, api_key, cert)
        out_v.append(url)
    out_i: list[str] = []
    for u in reference_image_urls:
        s = str(u).strip()
        if not s:
            continue
        local = normalize_local_reference_path(s, kind="image")
        _, url, cert = check_and_upload_local(model, local, api_key, cert)
        out_i.append(url)
    return out_i, out_v


def submit_wan27_t2v(
    settings: Settings,
    prompt: str,
    duration: int,
    prompt_extend: bool | None = None,
    watermark: bool | None = None,
    size: str | None = None,
) -> str:
    if prompt_extend is None:
        prompt_extend = settings.video_prompt_extend
    if watermark is None:
        watermark = settings.video_watermark
    resolution, ratio = parse_size_to_wan27_resolution_and_ratio(
        size or settings.default_resolution
    )
    d = max(2, min(15, int(duration)))
    body: dict[str, Any] = {
        "model": settings.video_gen_model,
        "input": {"prompt": prompt},
        "parameters": {
            "resolution": resolution,
            "ratio": ratio,
            "duration": d,
            "prompt_extend": prompt_extend,
            "watermark": watermark,
        },
    }
    rsp = _post_json(video_synthesis_post_url(settings), settings.dashscope_api_key, body)
    tid = (rsp.get("output") or {}).get("task_id")
    if not tid:
        raise RuntimeError(f"创建任务失败: {rsp}")
    return tid


def submit_wan27_r2v(
    settings: Settings,
    prompt: str,
    reference_image_urls: list[str],
    reference_video_urls: list[str],
    duration: int,
    prompt_extend: bool | None = None,
    watermark: bool | None = None,
    size: str | None = None,
) -> str:
    """wan2.7-r2v：input.media，顺序为先视频后图像（对应 视频1、视频2… 图1、图2…）。"""
    if prompt_extend is None:
        prompt_extend = settings.video_prompt_extend
    if watermark is None:
        watermark = settings.video_watermark
    model = settings.video_ref_model
    api_key = settings.dashscope_api_key
    media: list[dict[str, str]] = []
    cert = None
    for u in reference_video_urls:
        local = normalize_local_reference_path(u.strip(), kind="video")
        _, url, cert = check_and_upload_local(model, local, api_key, cert)
        media.append({"type": "reference_video", "url": url})
    for u in reference_image_urls:
        local = normalize_local_reference_path(u.strip(), kind="image")
        _, url, cert = check_and_upload_local(model, local, api_key, cert)
        media.append({"type": "reference_image", "url": url})
    if not media:
        raise ValueError("wan2.7-r2v 需要至少 1 个参考图或参考视频")

    resolution, ratio = parse_size_to_wan27_resolution_and_ratio(
        size or settings.default_resolution
    )
    d = max(2, min(10, int(duration)))
    body: dict[str, Any] = {
        "model": model,
        "input": {"prompt": prompt, "media": media},
        "parameters": {
            "resolution": resolution,
            "ratio": ratio,
            "duration": d,
            "prompt_extend": prompt_extend,
            "watermark": watermark,
        },
    }
    rsp = _post_json(video_synthesis_post_url(settings), api_key, body)
    tid = (rsp.get("output") or {}).get("task_id")
    if not tid:
        raise RuntimeError(f"创建任务失败: {rsp}")
    return tid


def wait_for_video_url(
    settings: Settings,
    task_id: str,
    poll_seconds: float = 15.0,
    max_wait_seconds: float = 900.0,
) -> str:
    url = tasks_get_url(settings, task_id)
    key = settings.dashscope_api_key
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        rsp = _get_json(url, key)
        out = rsp.get("output") or {}
        status = out.get("task_status", "")
        if status == "SUCCEEDED":
            vu = out.get("video_url")
            if vu:
                return vu
            raise RuntimeError(f"任务成功但无 video_url: {rsp}")
        if status == "FAILED":
            raise RuntimeError(
                f"万相任务失败: {out.get('code')} {out.get('message', rsp)}"
            )
        time.sleep(poll_seconds)
    raise TimeoutError(f"等待万相任务超时: {task_id}")


def generate_wan27_clip(
    settings: Settings,
    prompt: str,
    duration: int,
    *,
    reference_image_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    prompt_extend: bool | None = None,
    watermark: bool | None = None,
    size: str | None = None,
    poll_callback: Callable[[str], None] | None = None,
) -> str:
    """提交并轮询，返回 video_url。"""
    ref_i = reference_image_urls or []
    ref_v = reference_video_urls or []
    if ref_i or ref_v:
        tid = submit_wan27_r2v(
            settings,
            prompt,
            ref_i,
            ref_v,
            duration,
            prompt_extend=prompt_extend,
            watermark=watermark,
            size=size,
        )
    else:
        tid = submit_wan27_t2v(
            settings,
            prompt,
            duration,
            prompt_extend=prompt_extend,
            watermark=watermark,
            size=size,
        )
    if poll_callback:
        poll_callback(f"submitted task {tid}")
    return wait_for_video_url(settings, tid)
