"""Call Qwen vision model to extract structured storyboard from video clips."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from config import Settings
from scene_detector import SceneSegment
from storyboard import Shot, StoryboardDocument


DIRECTOR_SYSTEM = """你是一位世界顶级的电影导演和视觉分析专家。
你的任务不是简单描述画面里有什么，而是拆解【参考视频】的视听语言，并将这些“电影感”元素转化为结构化的【分镜脚本】。
你需要生成的脚本必须足够详细，能够直接作为另一个视频生成模型（如 Sora, Runway Gen-3, Kling, Seedance、通义万相等）的提示词基础。

【重要】忽略具体角色长相与可识别面部特征（便于后续换角）；重点分析 **摄影**（景别、角度、构图、运镜与速度、光影）与 **调度**（主体在画面中的位置、动作、节奏）。

你必须只输出 **严格 JSON**（不要 Markdown 代码块、不要前言后语）。根对象格式如下（字段名必须完全一致，以便程序解析）：

{
  "global_summary": {
    "core_atmosphere": "核心氛围，如：压抑的孤独感、赛博朋克喧嚣、浪漫柔光",
    "color_palette": "色彩基调，如：青橙色调、低饱和黑白、高饱和霓虹",
    "editing_pace": "剪辑节奏，如：快切、慢节奏长镜头"
  },
  "shots": [
    {
      "shot_type": "景别：极远景/远景/全景/中景/近景/特写/大特写",
      "camera_movement": "角度 + 运镜及速度（中文）。务必写清速度，如：平视，缓慢推进；俯视，快速横移",
      "scene_description": "构图（如三分法、中心对称、引导线、框架构图）+ 空间环境与道具；不写具体五官",
      "character_action": "该镜头内主体在做什么、表情与肢体语言（抽象描述动作与情绪，不写长相）",
      "dialogue": "口型可读则写对白，否则空字符串",
      "mood": "该镜头的情绪氛围",
      "lighting": "光影，如：伦勃朗光、侧逆光、柔光、硬光、霓虹光源",
      "audio_description": "听感/音效/配乐氛围推断（画面无声时根据画面推断）",
      "generation_prompt": "英文单段提示词，供文生视频模型使用。公式：[shot scale + angle + camera move] + subject blocking + key action + light/mood + optional film style",
      "duration_sec": 5.0
    }
  ]
}

规则：
- 默认应对**整支参考视频**作答：global_summary 必须体现全片层面的氛围、色彩与剪辑节奏；shots 按时间顺序覆盖全片所有镜头。
- 若输入仅为片段（少见），仍尽量给出合理的 global_summary 与各镜描述。
- 全片内每个独立镜头各占 shots 中一条；duration_sec 为该镜持续秒数（正数），若不确定可估算。
- generation_prompt 必须为英文，信息密度高，便于直接投喂视频模型。"""


USER_ANALYSIS_PROMPT = """【User Prompt (用户指令)】
请分析我上传的参考视频，忽略具体的角色长相（因为我们可能换角色），重点分析 **“摄影”**和 **“调度”**。
请严格按照以下结构完成分析，并将结果映射到系统消息规定的 JSON 字段中（每一镜的 generation_prompt 要为 AI 视频生成模型写好英文提示词）：

1. **全局摘要**（写入 JSON 的 global_summary）：
   - 核心氛围
   - 色彩基调
   - 剪辑节奏

2. **分镜拆解**（写入 JSON 的 shots 数组，按镜头序号）：
   - 景别 → shot_type
   - 构图 → 写入 scene_description 开头部分
   - 运动（含速度）与角度 → 合并写入 camera_movement
   - 主体动作与表情 → character_action
   - 光影 → lighting
   - 时长（秒）→ duration_sec
   - AI 生成提示词（英文）→ generation_prompt

只输出 JSON，不要其他文字。"""


CONSOLIDATE_SYSTEM = """你是与世界顶级电影导演协作的影视编剧与统筹。输入为同一支参考视频经模型**一次性**理解后得到的分镜 JSON（含 shots 与 global_summary，或来自少数片段的合并结果）。
请整合为一份连贯的专业说明，输出 **严格 JSON**（不要 Markdown），格式：
{
  "title": "短片标题或暂定名",
  "synopsis": "先用 2-4 句概括叙事走向；另起一段或并列写出全片层面的：核心氛围、色彩基调、剪辑节奏（可与各镜 mood/lighting 呼应）",
  "characters": "主要角色与关系简述（仍避免具体长相，侧重功能与关系）",
  "shot_notes": [
    {
      "shot_id": 1,
      "refinement": "可选，统一摄影风格或补充调度细节，无则空字符串"
    }
  ]
}
shot_notes 的 shot_id 必须与输入镜头顺序编号一致（从 1 递增）。
只输出 JSON。"""


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


def _video_to_data_url(path: Path, max_bytes: int) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise FileTooLargeForBase64(len(data), max_bytes)
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:video/mp4;base64,{b64}"


class FileTooLargeForBase64(Exception):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(f"Clip {size} bytes exceeds base64 limit {limit}")


def _analyze_clip_openai(
    client: OpenAI,
    settings: Settings,
    model: str,
    clip_path: Path,
    fps: float,
    max_base64_bytes: int,
    extra_user_hint: str,
) -> dict[str, Any]:
    try:
        url = _video_to_data_url(clip_path, max_base64_bytes)
    except FileTooLargeForBase64:
        return _analyze_clip_dashscope(settings, model, clip_path, fps, extra_user_hint)

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
    messages = [
        {
            "role": "user",
            "content": [
                {"video": str(clip_path.resolve()), "fps": fps},
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
    text = f"{DIRECTOR_SYSTEM}\n\n{_full_video_user_text(style_hint)}"
    messages = [
        {
            "role": "user",
            "content": [
                {"video": str(video_path.resolve()), "fps": settings.analysis_fps},
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
    整支本地视频一次调用模型：优先 OpenAI 兼容接口 + Base64；超过体积上限则改用
    DashScope 本地文件路径（见百炼文档对本地视频的大小限制）。
    """
    path = Path(video_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"视频文件不存在: {path}")
    max_b64 = int(settings.max_video_base64_mb * 1024 * 1024)
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    try:
        data_url = _video_to_data_url(path, max_b64)
        data = _run_full_video_openai(client, settings, data_url, style_hint)
    except FileTooLargeForBase64:
        data = _run_full_video_dashscope_local_file(settings, path, style_hint)
    doc = _storyboard_from_full_video_json(data, str(path))
    if consolidate_result:
        doc = consolidate_storyboard(doc, settings)
    return doc
