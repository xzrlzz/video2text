"""Call Qwen vision model to extract structured storyboard from video clips."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from openai import OpenAI

from video2text.config.settings import Settings
from video2text.core.scene_detector import SceneSegment
from video2text.core.storyboard import Shot, StoryboardDocument


DIRECTOR_SYSTEM = """You are a world-class film director and visual analysis expert.
Your task is not simply to describe what is on screen, but to deconstruct the cinematic language of the reference video and convert those filmmaking elements into a structured storyboard script.
The script must be detailed enough to serve directly as prompt input for another video generation model (e.g. Sora, Runway Gen-3, Kling, Seedance, Wan, etc.).

IMPORTANT: Ignore specific facial features and identifiable appearances (to allow character replacement later). Focus on **cinematography** (shot scale, angle, composition, camera movement + speed, lighting) and **staging** (subject position in frame, action, rhythm).

Output **strict JSON only** (no Markdown code blocks, no preamble or postamble). Root object format (field names must match exactly for parsing):

{
  "global_summary": {
    "core_atmosphere": "Overall mood in English. E.g.: oppressive solitude, cyberpunk chaos, romantic soft light",
    "color_palette": "Color grading in English. E.g.: teal-orange grade, desaturated monochrome, high-saturation neon",
    "editing_pace": "Editing rhythm in English. E.g.: rapid cuts, slow-paced long takes"
  },
  "shots": [
    {
      "shot_type": "Shot scale in English: extreme wide / wide / full / medium / close / extreme close / macro",
      "camera_movement": "Angle + camera movement with speed, in English. E.g.: eye-level, slow push-in; overhead, fast pan left",
      "scene_description": "Composition (rule of thirds, center symmetry, leading lines, frame-within-frame) + environment and props, in English. No specific facial features.",
      "character_action": "What the subject is doing, expression and body language, in English. Abstract — no appearance details.",
      "dialogue": "Write any spoken line in English if lip-readable; otherwise empty string \"\"",
      "mood": "Emotional atmosphere in English. E.g.: melancholic, tense, euphoric, eerie",
      "lighting": "Lighting in English. E.g.: Rembrandt lighting, soft backlight, neon rim light, hard sidelight",
      "audio_description": "Music / sound effects / ambient sound (excluding dialogue) in English. E.g.: distant train rumble, slow piano melody",
      "generation_prompt": "PURE ENGLISH ONLY. Single paragraph. Formula: [shot scale + angle + camera move] + subject blocking + key action + light/mood + optional film style. NO non-English characters.",
      "duration_sec": 5.0
    }
  ]
}

Rules:
- Analyze the **entire reference video** by default: global_summary must reflect the overall mood, color, and editing rhythm; shots must cover all cuts in chronological order.
- If the input is a short clip, still provide a reasonable global_summary and per-shot descriptions.
- Each distinct cut gets its own entry in shots; duration_sec is the shot duration in seconds (positive, estimate if unsure).
- ALL text fields must be written in English. No non-English characters in any field."""


USER_ANALYSIS_PROMPT = """Analyze the uploaded reference video. Ignore specific character appearances (we may replace characters later). Focus on **cinematography** and **staging**.

Fill in the JSON fields defined in the system prompt:

1. **Global summary** (→ global_summary):
   - core_atmosphere
   - color_palette
   - editing_pace

2. **Shot breakdown** (→ shots array, chronological order):
   - shot_type
   - composition + environment → scene_description
   - movement (with speed) + angle → camera_movement
   - subject action + expression → character_action
   - lighting → lighting
   - duration in seconds → duration_sec
   - AI video generation prompt (English) → generation_prompt

Output JSON only. ALL fields must be in English."""


CONSOLIDATE_SYSTEM = """You are a film editor and story consultant working with a world-class director. The input is a storyboard JSON extracted from a reference video (contains shots and global_summary, possibly merged from multiple segments).

Consolidate into a coherent professional overview. Output **strict JSON only** (no Markdown), format:
{
  "title": "Short film title or working title, in English",
  "synopsis": "2-4 sentences summarizing the narrative arc; then describe overall: core atmosphere, color palette, editing rhythm (echoing per-shot mood/lighting). All in English.",
  "characters": "Main characters and their relationships in English (no specific appearances — focus on role and dynamic)",
  "shot_notes": [
    {
      "shot_id": 1,
      "refinement": "Optional: unified cinematography style note or staging detail for this shot, in English. Empty string if nothing to add."
    }
  ]
}
shot_notes shot_id must match input shot order starting from 1.
Output JSON only. ALL fields must be in English."""


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip markdown fences if any
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"No JSON object in model output: {text[:500]}")
    return json.loads(text[start : end + 1])


# API hard limit for base64-encoded data-uri payload (DashScope/OpenAI compatible APIs)
_API_BASE64_HARD_LIMIT = 10 * 1024 * 1024  # 10 MB encoded
# Target raw size for compression attempts — gives ~8.5 MB encoded, safely under limit
_COMPRESS_TARGET_BYTES = 6 * 1024 * 1024   # 6 MB raw


def _video_to_data_url(path: Path, max_bytes: int) -> str:
    data = path.read_bytes()
    # Apply both the user-configured limit and the API hard limit.
    # base64 encoding inflates size by ~4/3, so pre-check raw bytes against both limits.
    raw_limit = min(max_bytes, int(_API_BASE64_HARD_LIMIT * 3 / 4))  # ≈7.5 MB raw
    if len(data) > raw_limit:
        raise FileTooLargeForBase64(len(data), raw_limit)
    b64 = base64.standard_b64encode(data).decode("ascii")
    # Double-check encoded size against the hard limit
    if len(b64) > _API_BASE64_HARD_LIMIT:
        raise FileTooLargeForBase64(len(data), raw_limit)
    return f"data:video/mp4;base64,{b64}"


class FileTooLargeForBase64(Exception):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(f"Clip {size} bytes exceeds base64 limit {limit}")


def _compress_video_for_api(src: Path, target_bytes: int = _COMPRESS_TARGET_BYTES) -> Path | None:
    """
    Use ffmpeg to shrink a video clip so its raw size fits within target_bytes.
    Strategy: scale down to ≤720p, then estimate bitrate from target size and duration.
    Returns a Path to a temp file on success, or None if ffmpeg is unavailable / compression failed.
    The caller is responsible for deleting the temp file when done.
    """
    try:
        # Probe duration
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(src),
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip() or "0")
        if duration <= 0:
            return None

        # Calculate target video bitrate (kbps): leave ~64kbps for audio
        audio_kbps = 64
        target_kbps = max(100, int(target_bytes * 8 / duration / 1000) - audio_kbps)

        tmp = tempfile.NamedTemporaryFile(suffix="_compressed.mp4", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            # Scale to max 720p, keep aspect ratio
            "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "fast", "-crf", "28",
            "-b:v", f"{target_kbps}k", "-maxrate", f"{target_kbps * 2}k",
            "-bufsize", f"{target_kbps * 4}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0 or not out_path.is_file():
            out_path.unlink(missing_ok=True)
            return None

        # If compression didn't help enough, try again with lower resolution (480p)
        if out_path.stat().st_size > target_bytes:
            target_kbps2 = max(80, target_kbps // 2)
            tmp2 = tempfile.NamedTemporaryFile(suffix="_compressed2.mp4", delete=False)
            tmp2.close()
            out_path2 = Path(tmp2.name)
            cmd2 = [
                "ffmpeg", "-y", "-i", str(src),
                "-vf", "scale='min(854,iw)':'min(480,ih)':force_original_aspect_ratio=decrease",
                "-c:v", "libx264", "-preset", "fast", "-crf", "32",
                "-b:v", f"{target_kbps2}k",
                "-c:a", "aac", "-b:a", "48k",
                "-movflags", "+faststart",
                str(out_path2),
            ]
            result2 = subprocess.run(cmd2, capture_output=True, timeout=120)
            out_path.unlink(missing_ok=True)
            if result2.returncode == 0 and out_path2.is_file():
                out_path = out_path2
            else:
                out_path2.unlink(missing_ok=True)
                return None

        return out_path
    except Exception:
        return None


# DashScope local file path supports up to 100MB (per official docs)
_DASHSCOPE_LOCAL_FILE_LIMIT = 100 * 1024 * 1024  # 100 MB


def _analyze_clip_openai(
    client: OpenAI,
    settings: Settings,
    model: str,
    clip_path: Path,
    fps: float,
    max_base64_bytes: int,
    extra_user_hint: str,
) -> dict[str, Any]:
    raw_limit = min(max_base64_bytes, int(_API_BASE64_HARD_LIMIT * 3 / 4))
    file_size = clip_path.stat().st_size
    compressed_tmp: Path | None = None
    working_path = clip_path

    if file_size > raw_limit:
        # If under 100MB, skip compression entirely and go straight to dashscope local path
        # (DashScope SDK local file path supports up to 100MB, much better than base64 10MB limit)
        if file_size <= _DASHSCOPE_LOCAL_FILE_LIMIT:
            return _analyze_clip_dashscope(settings, model, clip_path, fps, extra_user_hint)

        # Over 100MB: try compression first to get under base64 limit for OpenAI-compat path
        compressed_tmp = _compress_video_for_api(clip_path, _COMPRESS_TARGET_BYTES)
        if compressed_tmp and compressed_tmp.stat().st_size <= raw_limit:
            working_path = compressed_tmp
        else:
            # Compression failed or still too large → dashscope fallback
            if compressed_tmp:
                compressed_tmp.unlink(missing_ok=True)
            return _analyze_clip_dashscope(settings, model, clip_path, fps, extra_user_hint)

    try:
        url = _video_to_data_url(working_path, max_base64_bytes)
    except FileTooLargeForBase64:
        if compressed_tmp:
            compressed_tmp.unlink(missing_ok=True)
        return _analyze_clip_dashscope(settings, model, clip_path, fps, extra_user_hint)
    finally:
        if compressed_tmp and compressed_tmp != working_path:
            compressed_tmp.unlink(missing_ok=True)

    try:
        user_content: list[dict[str, Any]] = [
            {
                "type": "video_url",
                "video_url": {"url": url},
                "fps": fps,
            },
            {
                "type": "text",
                "text": f"{USER_ANALYSIS_PROMPT}\n{extra_user_hint}",
            },
        ]
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": DIRECTOR_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
        raw = completion.choices[0].message.content or ""
        return _extract_json_object(raw)
    finally:
        if compressed_tmp:
            compressed_tmp.unlink(missing_ok=True)


def _analyze_clip_dashscope(
    settings: Settings,
    model: str,
    clip_path: Path,
    fps: float,
    extra_user_hint: str,
) -> dict[str, Any]:
    import dashscope
    from dashscope import MultiModalConversation

    dashscope.base_http_api_url = settings.dashscope_api_base
    # DashScope SDK requires "file://" prefix for local file paths (per official docs)
    video_file_uri = f"file://{clip_path.resolve()}"
    messages = [
        {
            "role": "user",
            "content": [
                {"video": video_file_uri, "fps": fps},
                {
                    "text": f"{DIRECTOR_SYSTEM}\n\n{USER_ANALYSIS_PROMPT}\n{extra_user_hint}"
                },
            ],
        }
    ]
    rsp = MultiModalConversation.call(
        model=model,
        messages=messages,
        api_key=settings.dashscope_api_key,
    )
    if rsp.status_code != 200:
        raise RuntimeError(
            f"MultiModalConversation failed: {rsp.code} {rsp.message}"
        )
    parts = rsp.output.choices[0].message.content
    text = ""
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            text += p["text"]
        elif isinstance(p, str):
            text += p
    return _extract_json_object(text)


def _sec_to_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _shot_time_ranges_in_segment(
    start_sec: float, end_sec: float, shots_data: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """Split segment timeline across shots using duration_sec when all present, else equal split."""
    seg_dur = max(0.01, end_sec - start_sec)
    n = len(shots_data)
    if n == 0:
        return []
    weights: list[float | None] = []
    for item in shots_data:
        ds = item.get("duration_sec")
        if ds is None:
            weights.append(None)
            continue
        try:
            weights.append(max(0.01, float(ds)))
        except (TypeError, ValueError):
            weights.append(None)
    if all(w is not None for w in weights):
        total = sum(weights)  # type: ignore[arg-type]
        if total > 0:
            scale = seg_dur / total
            out: list[tuple[float, float]] = []
            t = start_sec
            for w in weights:
                d = float(w) * scale
                out.append((t, t + d))
                t += d
            return out
    slice_dur = seg_dur / n
    return [
        (start_sec + j * slice_dur, start_sec + (j + 1) * slice_dur)
        for j in range(n)
    ]


def _synopsis_from_global_summary(gs: Any) -> str:
    if not isinstance(gs, dict):
        return ""
    parts: list[str] = []
    mapping = (
        ("core_atmosphere", "核心氛围"),
        ("color_palette", "色彩基调"),
        ("editing_pace", "剪辑节奏"),
    )
    for key, label in mapping:
        v = gs.get(key)
        if v is not None and str(v).strip():
            parts.append(f"{label}：{str(v).strip()}")
    return "\n".join(parts)


def _shot_from_analysis_dict(
    shot_id: int,
    item: dict[str, Any],
    t0: float,
    t1: float,
) -> Shot:
    t0, t1 = max(0.0, t0), max(0.0, t1)
    if t1 < t0:
        t0, t1 = t1, t0
    return Shot(
        shot_id=shot_id,
        start_time=_sec_to_ts(t0),
        end_time=_sec_to_ts(t1),
        duration=round(max(0.01, t1 - t0), 2),
        shot_type=str(item.get("shot_type", "")),
        camera_movement=str(item.get("camera_movement", "")),
        scene_description=str(item.get("scene_description", "")),
        character_action=str(item.get("character_action", "")),
        dialogue=str(item.get("dialogue", "")),
        mood=str(item.get("mood", "")),
        lighting=str(item.get("lighting", "")),
        audio_description=str(item.get("audio_description", "")),
        generation_prompt=str(item.get("generation_prompt", "")),
    )


def _build_shots_from_full_video_items(shots_data: list[Any]) -> list[Shot]:
    valid = [x for x in shots_data if isinstance(x, dict)]
    if not valid:
        return []
    times: list[tuple[float, float]] = []
    approx_ok = True
    for item in valid:
        a0, a1 = item.get("approx_start_sec"), item.get("approx_end_sec")
        if a0 is None or a1 is None:
            approx_ok = False
            break
        try:
            t0, t1 = float(a0), float(a1)
        except (TypeError, ValueError):
            approx_ok = False
            break
        t0, t1 = max(0.0, t0), max(0.0, t1)
        if t1 < t0:
            t0, t1 = t1, t0
        times.append((t0, t1))
    if approx_ok and len(times) == len(valid):
        return [
            _shot_from_analysis_dict(i + 1, valid[i], times[i][0], times[i][1])
            for i in range(len(valid))
        ]
    cursor = 0.0
    out: list[Shot] = []
    for i, item in enumerate(valid):
        ds = item.get("duration_sec")
        try:
            d = float(ds) if ds is not None else 3.0
        except (TypeError, ValueError):
            d = 3.0
        d = max(0.01, d)
        t0, t1 = cursor, cursor + d
        cursor = t1
        out.append(_shot_from_analysis_dict(i + 1, item, t0, t1))
    return out


def analyze_scene_segments(
    segments: list[SceneSegment],
    settings: Settings,
    style_hint: str = "",
) -> tuple[StoryboardDocument, list[str]]:
    """
    Run vision model per scene clip; build StoryboardDocument with sequential shots.
    """
    max_b64 = int(settings.max_video_base64_mb * 1024 * 1024)
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )

    import dashscope

    dashscope.api_key = settings.dashscope_api_key

    all_shots: list[Shot] = []
    raw_texts: list[str] = []
    shot_counter = 0
    hint = style_hint.strip()
    if hint:
        hint = f" 改编/统一风格要求：{hint}"

    for seg in segments:
        if not seg.clip_path or not seg.clip_path.exists():
            raise FileNotFoundError(f"Missing clip for scene {seg.index}")
        data = _analyze_clip_openai(
            client,
            settings,
            settings.vision_model,
            seg.clip_path,
            settings.analysis_fps,
            max_b64,
            hint,
        )
        raw_texts.append(json.dumps(data, ensure_ascii=False))
        shots_data = data.get("shots") or []
        if not isinstance(shots_data, list):
            shots_data = [shots_data]

        ranges = _shot_time_ranges_in_segment(seg.start_sec, seg.end_sec, shots_data)
        for j, item in enumerate(shots_data):
            if not isinstance(item, dict):
                continue
            shot_counter += 1
            t0, t1 = ranges[j] if j < len(ranges) else (seg.start_sec, seg.end_sec)
            all_shots.append(
                Shot(
                    shot_id=shot_counter,
                    start_time=_sec_to_ts(t0),
                    end_time=_sec_to_ts(t1),
                    duration=round(t1 - t0, 2),
                    shot_type=str(item.get("shot_type", "")),
                    camera_movement=str(item.get("camera_movement", "")),
                    scene_description=str(item.get("scene_description", "")),
                    character_action=str(item.get("character_action", "")),
                    dialogue=str(item.get("dialogue", "")),
                    mood=str(item.get("mood", "")),
                    lighting=str(item.get("lighting", "")),
                    audio_description=str(item.get("audio_description", "")),
                    generation_prompt=str(item.get("generation_prompt", "")),
                )
            )

    doc = StoryboardDocument(
        source_video="",
        shots=all_shots,
        raw_scene_analyses=raw_texts,
    )
    return doc, raw_texts


def consolidate_storyboard(
    doc: StoryboardDocument,
    settings: Settings,
) -> StoryboardDocument:
    """Second LLM pass: title, synopsis, characters, optional refinements."""
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    payload = {
        "shots": [s.to_dict() for s in doc.shots],
        "per_scene_raw": doc.raw_scene_analyses,
    }
    user_text = json.dumps(payload, ensure_ascii=False)
    completion = client.chat.completions.create(
        model=settings.vision_model,
        messages=[
            {"role": "system", "content": CONSOLIDATE_SYSTEM},
            {"role": "user", "content": user_text},
        ],
    )
    raw = completion.choices[0].message.content or ""
    data = _extract_json_object(raw)
    doc.title = str(data.get("title", doc.title))
    doc.synopsis = str(data.get("synopsis", doc.synopsis))
    doc.characters = str(data.get("characters", doc.characters))
    notes = data.get("shot_notes") or []
    if isinstance(notes, list):
        by_id: dict[int, str] = {}
        for n in notes:
            if isinstance(n, dict) and "shot_id" in n:
                try:
                    by_id[int(n["shot_id"])] = str(n.get("refinement", ""))
                except (TypeError, ValueError):
                    continue
        for s in doc.shots:
            if s.shot_id in by_id and by_id[s.shot_id]:
                extra = by_id[s.shot_id]
                s.scene_description = f"{s.scene_description} {extra}".strip()
    return doc


def _full_video_user_text(style_hint: str) -> str:
    hint = f"\n{style_hint}" if style_hint else ""
    return (
        f"{USER_ANALYSIS_PROMPT}\n"
        "请完整观看整支参考视频。除 global_summary 与 shots 外，每个镜头尽量给出在原片时间轴上的 "
        "approx_start_sec 与 approx_end_sec（浮点秒，从 0 起算）。若无法精确，可用 duration_sec "
        "表示该镜时长并由系统推算。\n"
        "只输出 JSON。"
        f"{hint}"
    )


def _run_full_video_openai(
    client: OpenAI,
    settings: Settings,
    video_url: str,
    style_hint: str,
) -> dict[str, Any]:
    user_content: list[dict[str, Any]] = [
        {
            "type": "video_url",
            "video_url": {"url": video_url},
            "fps": settings.analysis_fps,
        },
        {"type": "text", "text": _full_video_user_text(style_hint)},
    ]
    completion = client.chat.completions.create(
        model=settings.vision_model,
        messages=[
            {"role": "system", "content": DIRECTOR_SYSTEM},
            {"role": "user", "content": user_content},
        ],
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


def _run_full_video_dashscope_local_file(
    settings: Settings,
    video_path: Path,
    style_hint: str,
) -> dict[str, Any]:
    import dashscope
    from dashscope import MultiModalConversation

    dashscope.base_http_api_url = settings.dashscope_api_base
    # DashScope SDK requires "file://" prefix for local file paths (per official docs)
    video_file_uri = f"file://{video_path.resolve()}"
    text = f"{DIRECTOR_SYSTEM}\n\n{_full_video_user_text(style_hint)}"
    messages = [
        {
            "role": "user",
            "content": [
                {"video": video_file_uri, "fps": settings.analysis_fps},
                {"text": text},
            ],
        }
    ]
    rsp = MultiModalConversation.call(
        model=settings.vision_model,
        messages=messages,
        api_key=settings.dashscope_api_key,
    )
    if rsp.status_code != 200:
        raise RuntimeError(
            f"MultiModalConversation failed: {rsp.code} {rsp.message}"
        )
    parts = rsp.output.choices[0].message.content
    out = ""
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            out += p["text"]
        elif isinstance(p, str):
            out += p
    return _extract_json_object(out)


def _storyboard_from_full_video_json(
    data: dict[str, Any],
    source_video: str,
) -> StoryboardDocument:
    shots_data = data.get("shots") or []
    synopsis_seed = _synopsis_from_global_summary(data.get("global_summary"))
    shots = _build_shots_from_full_video_items(shots_data)
    return StoryboardDocument(
        title="",
        synopsis=synopsis_seed,
        characters="",
        source_video=source_video,
        shots=shots,
        raw_scene_analyses=[json.dumps(data, ensure_ascii=False)],
    )


def analyze_full_video_url(
    video_url: str,
    settings: Settings,
    style_hint: str = "",
    consolidate_result: bool = True,
) -> StoryboardDocument:
    """整支视频一次调用模型（公网 HTTPS URL）。"""
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    data = _run_full_video_openai(client, settings, video_url, style_hint)
    doc = _storyboard_from_full_video_json(data, video_url)
    if consolidate_result:
        doc = consolidate_storyboard(doc, settings)
    return doc


def analyze_full_video_local(
    video_path: str | Path,
    settings: Settings,
    style_hint: str = "",
    consolidate_result: bool = True,
) -> StoryboardDocument:
    """
    整支本地视频一次调用模型：优先 OpenAI 兼容接口 + Base64。
    超过 base64 体积限制时先尝试压缩，压缩后仍超限则改用 DashScope 本地文件路径。
    """
    path = Path(video_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"视频文件不存在: {path}")
    max_b64 = int(settings.max_video_base64_mb * 1024 * 1024)
    raw_limit = min(max_b64, int(_API_BASE64_HARD_LIMIT * 3 / 4))
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    compressed_tmp: Path | None = None
    working_path = path
    file_size = path.stat().st_size
    if file_size > raw_limit:
        # Under 100MB: skip compression, use dashscope local file path directly
        if file_size <= _DASHSCOPE_LOCAL_FILE_LIMIT:
            data = _run_full_video_dashscope_local_file(settings, path, style_hint)
            doc = _storyboard_from_full_video_json(data, str(path))
            if consolidate_result:
                doc = consolidate_storyboard(doc, settings)
            return doc

        # Over 100MB: try to compress down to base64-compatible size
        compressed_tmp = _compress_video_for_api(path, _COMPRESS_TARGET_BYTES)
        if compressed_tmp and compressed_tmp.stat().st_size <= raw_limit:
            working_path = compressed_tmp
        else:
            if compressed_tmp:
                compressed_tmp.unlink(missing_ok=True)
                compressed_tmp = None
            # Compression failed → dashscope as last resort
            data = _run_full_video_dashscope_local_file(settings, path, style_hint)
            doc = _storyboard_from_full_video_json(data, str(path))
            if consolidate_result:
                doc = consolidate_storyboard(doc, settings)
            return doc
    try:
        data_url = _video_to_data_url(working_path, max_b64)
        data = _run_full_video_openai(client, settings, data_url, style_hint)
    except FileTooLargeForBase64:
        data = _run_full_video_dashscope_local_file(settings, path, style_hint)
    finally:
        if compressed_tmp:
            compressed_tmp.unlink(missing_ok=True)
    doc = _storyboard_from_full_video_json(data, str(path))
    if consolidate_result:
        doc = consolidate_storyboard(doc, settings)
    return doc
