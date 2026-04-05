"""Concatenate video files with ffmpeg."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def concat_videos_ffmpeg(video_paths: list[Path], output_path: Path) -> None:
    """Concatenate videos (same codec ideal) using concat demuxer."""
    if not video_paths:
        raise ValueError("No video segments to concatenate")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as f:
        list_path = Path(f.name)
        for p in video_paths:
            p = Path(p).resolve()
            if not p.exists():
                raise FileNotFoundError(p)
            # concat demuxer requires escaped paths
            safe = str(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)


def reencode_concat(video_paths: list[Path], output_path: Path) -> None:
    """Re-encode to H.264/AAC when stream copy fails (mixed inputs)."""
    if not video_paths:
        raise ValueError("No video segments")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as f:
        list_path = Path(f.name)
        for p in video_paths:
            p = Path(p).resolve()
            safe = str(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)
