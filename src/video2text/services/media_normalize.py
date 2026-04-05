"""
参考图/视频在提交万相 wan2.7 HTTP 前的本地规范化。

解决 InvalidParameter.DataInspection（安检无法识别媒体格式）的常见原因：
HEVC/VP9/WebM、MOV 特殊编码、WebP/HEIC/GIF 等；参考图即使是 JPG/PNG 也可能因 CMYK、
透明通道、过大分辨率、非标准子采样等被拒——默认会对本地参考图一律重编码为 baseline JPEG。

环境变量：
- V2T_SKIP_REFERENCE_NORMALIZE：跳过全部预处理
- V2T_LIGHT_REFERENCE_IMAGE=1：参考图仅当扩展名非 jpg/png 时才转码（旧逻辑，更快但不稳）
- V2T_REFERENCE_IMAGE_MAX_SIDE：重编码时长边上限，默认 2048

依赖系统 PATH 中的 ffprobe/ffmpeg（与 README 一致）。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


class MediaNormalizeError(RuntimeError):
    pass


def _run_json(cmd: list[str]) -> dict:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise MediaNormalizeError("未找到 ffprobe/ffmpeg，请安装并加入 PATH") from e
    except subprocess.CalledProcessError as e:
        raise MediaNormalizeError(
            f"命令失败: {' '.join(cmd)}\n{e.stderr or e.stdout}"
        ) from e
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise MediaNormalizeError(
            f"ffprobe 输出非 JSON: {(r.stdout or '')[:400]!r}"
        ) from e


def _ffprobe_streams(path: str) -> list[dict]:
    data = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            path,
        ]
    )
    return list(data.get("streams") or [])


def reference_video_needs_transcode(path: str) -> bool:
    streams = _ffprobe_streams(path)
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        return True
    c = (v.get("codec_name") or "").lower()
    pix = (v.get("pix_fmt") or "").lower()
    if c not in ("h264", "avc", "avc1"):
        return True
    if pix not in ("yuv420p", "yuvj420p"):
        return True
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if a:
        ac = (a.get("codec_name") or "").lower()
        if ac not in ("aac", "mp3"):
            return True
    return False


def transcode_reference_video_to_mp4(src: str) -> str:
    streams = _ffprobe_streams(src)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fd, out = tempfile.mkstemp(suffix=".mp4", prefix="v2t_refv_")
    os.close(fd)
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd.append(out)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise MediaNormalizeError("未找到 ffmpeg") from e
    except subprocess.CalledProcessError as e:
        raise MediaNormalizeError(
            f"参考视频转码失败: {src}\n{e.stderr or e.stdout}"
        ) from e
    return out


_IMAGE_OK_EXT = frozenset({".jpg", ".jpeg", ".png"})


def reference_image_needs_convert(path: str) -> bool:
    suf = Path(path).suffix.lower()
    if not suf or suf == ".bin":
        return True
    return suf not in _IMAGE_OK_EXT


def _reference_image_max_side() -> int:
    try:
        n = int(os.environ.get("V2T_REFERENCE_IMAGE_MAX_SIDE", "2048"))
    except ValueError:
        n = 2048
    return max(512, min(n, 8192))


def convert_reference_image_to_jpeg(src: str) -> str:
    """输出 sRGB baseline JPEG；缩放以通过常见「尺寸/像素格式」安检。"""
    mx = _reference_image_max_side()
    fd, out = tempfile.mkstemp(suffix=".jpg", prefix="v2t_refi_")
    os.close(fd)
    vf = (
        f"scale={mx}:{mx}:force_original_aspect_ratio=decrease,"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise MediaNormalizeError("未找到 ffmpeg") from e
    except subprocess.CalledProcessError as e:
        raise MediaNormalizeError(
            f"参考图转 JPEG 失败: {src}\n{e.stderr or e.stdout}"
        ) from e
    return out


def normalize_local_reference_path(path: str, *, kind: str) -> str:
    """
    若为本地文件且需要规范化，返回新文件路径（临时文件）；否则返回原 path。
    kind: 'video' | 'image'
    """
    p = os.path.expanduser(str(path).strip())
    if not p or p.startswith(("http://", "https://", "oss://")):
        return path
    if not os.path.isfile(p):
        return path
    if os.environ.get("V2T_SKIP_REFERENCE_NORMALIZE", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return path
    try:
        if kind == "video":
            if reference_video_needs_transcode(p):
                return transcode_reference_video_to_mp4(p)
            suf = Path(p).suffix.lower()
            if suf != ".mp4":
                return transcode_reference_video_to_mp4(p)
            return p
        if kind == "image":
            light = os.environ.get("V2T_LIGHT_REFERENCE_IMAGE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if light:
                if reference_image_needs_convert(p):
                    return convert_reference_image_to_jpeg(p)
                return p
            return convert_reference_image_to_jpeg(p)
        raise ValueError(f"kind 须为 video 或 image，收到: {kind!r}")
    except (MediaNormalizeError, json.JSONDecodeError, OSError) as e:
        raise MediaNormalizeError(
            f"参考媒体预处理失败（{kind}）: {p}\n{e}"
        ) from e
