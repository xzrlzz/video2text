"""Scene detection, clip extraction, and keyframe sampling."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from scenedetect import ContentDetector, detect


@dataclass
class SceneSegment:
    index: int
    start_sec: float
    end_sec: float
    clip_path: Path | None = None
    keyframe_paths: list[Path] = field(default_factory=list)


@dataclass
class SceneDetectionResult:
    segments: list[SceneSegment]
    video_path: Path
    fps: float
    duration_sec: float


def _probe_video(path: Path) -> tuple[float, float]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frame_count / fps if fps else 0.0
    cap.release()
    return float(fps), float(duration)


def detect_scenes(
    video_path: str | Path,
    threshold: float = 27.0,
    min_scene_len: int = 15,
) -> list[tuple[float, float]]:
    """Return list of (start_sec, end_sec) using PySceneDetect."""
    path = Path(video_path).resolve()
    scene_list = detect(
        str(path),
        ContentDetector(threshold=threshold, min_scene_len=min_scene_len),
    )
    ranges: list[tuple[float, float]] = []
    for st, ed in scene_list:
        ranges.append((st.get_seconds(), ed.get_seconds()))
    return ranges


def extract_clip_ffmpeg(
    src: Path,
    start_sec: float,
    end_sec: float,
    out_path: Path,
) -> None:
    duration = max(0.1, end_sec - start_sec)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
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
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def extract_keyframes(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    out_dir: Path,
    stem: str,
) -> list[Path]:
    """Save first and middle frame of segment as JPEG."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    mid_sec = start_sec + max(0.0, (end_sec - start_sec) / 2.0)
    paths: list[Path] = []

    def grab_at(t: float) -> Path:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"Failed to read frame at {t}s from {video_path}")
        outp = out_dir / f"{stem}_t{int(t * 1000)}.jpg"
        cv2.imwrite(str(outp), frame)
        return outp

    paths.append(grab_at(start_sec))
    if end_sec - start_sec > 0.5:
        paths.append(grab_at(mid_sec))
    cap.release()
    return paths


def build_scene_segments(
    video_path: str | Path,
    threshold: float = 27.0,
    extract_clips: bool = True,
    extract_frames: bool = True,
    work_dir: Path | None = None,
) -> SceneDetectionResult:
    """
    Detect scenes and optionally extract per-scene clips and keyframes under work_dir.
    """
    src = Path(video_path).resolve()
    fps, duration = _probe_video(src)
    ranges = detect_scenes(src, threshold=threshold)
    if not ranges:
        ranges = [(0.0, duration)] if duration > 0 else [(0.0, 1.0)]

    tmp_root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="v2t_scenes_"))
    tmp_root.mkdir(parents=True, exist_ok=True)

    segments: list[SceneSegment] = []
    for i, (start_sec, end_sec) in enumerate(ranges):
        seg = SceneSegment(index=i, start_sec=start_sec, end_sec=end_sec)
        stem = f"scene_{i:04d}"
        if extract_clips:
            clip = tmp_root / f"{stem}.mp4"
            extract_clip_ffmpeg(src, start_sec, end_sec, clip)
            seg.clip_path = clip
        if extract_frames:
            seg.keyframe_paths = extract_keyframes(
                src, start_sec, end_sec, tmp_root, stem
            )
        segments.append(seg)

    return SceneDetectionResult(
        segments=segments,
        video_path=src,
        fps=fps,
        duration_sec=duration,
    )
