"""Concatenate video files with ffmpeg."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _write_concat_list(video_paths: list[Path]) -> Path:
    if not video_paths:
        raise ValueError("No video segments to concatenate")
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for p in video_paths:
            p = Path(p).resolve()
            if not p.exists():
                raise FileNotFoundError(p)
            safe = str(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    finally:
        f.close()
    return Path(f.name)


def _ffmpeg_concat(list_path: Path, output_path: Path, extra_args: list[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        *extra_args,
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)


def concat_videos_ffmpeg(video_paths: list[Path], output_path: Path) -> None:
    """Concatenate videos (same codec ideal) using concat demuxer."""
    _ffmpeg_concat(_write_concat_list(video_paths), Path(output_path), ["-c", "copy"])


def reencode_concat(video_paths: list[Path], output_path: Path) -> None:
    """Re-encode to H.264/AAC when stream copy fails (mixed inputs)."""
    _ffmpeg_concat(
        _write_concat_list(video_paths),
        Path(output_path),
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-movflags", "+faststart"],
    )


def merge_audio_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    replace_audio: bool = True,
) -> None:
    """将音频轨合并到视频文件中。

    replace_audio=True: 替换原有音频（用于静音视频 + TTS 音频）
    replace_audio=False: 混合原有音频和新音频
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if replace_audio:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=shortest:dropout_transition=2[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ]

    subprocess.run(cmd, check=True)


def strip_audio(video_path: Path, output_path: Path) -> None:
    """移除视频中的音频轨。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-c:v", "copy", "-an",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)
