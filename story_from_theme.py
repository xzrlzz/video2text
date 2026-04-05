"""根据用户给定的主题文本，由大模型创作故事并输出与 video2text 兼容的分镜 JSON。"""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from config import Settings
from storyboard import Shot, StoryboardDocument
from video_analyzer import _extract_json_object, _shot_from_analysis_dict


THEME_STORYBOARD_SYSTEM = """你是一位资深影视编剧与分镜导演。用户会提供一个【故事主题/创意描述】（可能很简短），你需要：
1. 展开为完整、可拍的短片故事（有起承转合或清晰情绪曲线）；
2. 拆解为专业分镜列表，每镜包含视听与调度细节；
3. 为每个有台词的镜头写出【对白】：必须标明说话人，格式为「角色名：「台词原文」」；无对白则写空字符串 "" 或写「（无对白）」；
4. 为每镜写一条英文 generation_prompt，供文生/参考生视频模型使用（与现有 video2text 流程一致）。

硬性要求：
- 只输出 **严格 JSON**（不要 Markdown 代码块、不要前言后语）。
- 根对象字段名必须完全一致：

{
  "title": "短片标题",
  "synopsis": "2～5 句故事梗概 + 可选的整体氛围/节奏说明",
  "characters": "主要角色姓名、性格与关系；后续对白中的角色名须与此一致",
  "shots": [
    {
      "shot_type": "景别：远景/全景/中景/近景/特写等",
      "camera_movement": "角度 + 运镜（含速度），中文",
      "scene_description": "环境、构图、关键道具，中文",
      "character_action": "该镜内人物动作与神态，中文",
      "dialogue": "例：林晓：「你还好吗？」 或多行用 \\n；无对白则 \"\"",
      "mood": "该镜情绪氛围",
      "lighting": "光影描述",
      "audio_description": "配乐/音效/环境声（除对白外），中文",
      "generation_prompt": "单段英文，含景别、运镜、主体动作、光线与情绪关键词",
      "duration_sec": 4.0
    }
  ]
}

- shots 按叙事时间顺序排列；duration_sec 为正数秒，单镜建议 2～8 秒，全片镜头数须落在用户给定的范围内。
- 对白语言：若用户未指定，默认使用中文书面自然对白；generation_prompt 始终英文。"""


def _build_shots_from_theme_items(items: list[dict[str, Any]]) -> list[Shot]:
    valid = [x for x in items if isinstance(x, dict)]
    if not valid:
        return []
    cursor = 0.0
    out: list[Shot] = []
    for i, item in enumerate(valid):
        ds = item.get("duration_sec")
        try:
            d = float(ds) if ds is not None else 4.0
        except (TypeError, ValueError):
            d = 4.0
        d = max(0.5, min(30.0, d))
        t0, t1 = cursor, cursor + d
        cursor = t1
        out.append(_shot_from_analysis_dict(i + 1, item, t0, t1))
    return out


def generate_storyboard_from_theme(
    theme: str,
    settings: Settings,
    *,
    style_hint: str = "",
    min_shots: int = 8,
    max_shots: int = 24,
    model: str | None = None,
) -> StoryboardDocument:
    """
    调用文本大模型，根据主题生成分镜文档；可直接交给 generate 子命令做万相生成。
    """
    theme = theme.strip()
    if not theme:
        raise ValueError("主题描述不能为空")

    min_shots = max(3, min(40, int(min_shots)))
    max_shots = max(min_shots, min(60, int(max_shots)))

    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    use_model = (model or settings.theme_story_model or settings.vision_model).strip()
    if not use_model:
        use_model = settings.vision_model

    extra = ""
    if style_hint.strip():
        extra += f"\n【风格/类型偏好】{style_hint.strip()}"
    user_msg = (
        f"【用户主题/创意】\n{theme}\n\n"
        f"请生成 {min_shots}～{max_shots} 个镜头（shots 数组长度须在此区间内）。"
        f"{extra}\n只输出 JSON。"
    )

    completion = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": THEME_STORYBOARD_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = completion.choices[0].message.content or ""
    data = _extract_json_object(raw)

    shots_data = data.get("shots") or []
    if not isinstance(shots_data, list):
        raise ValueError("模型返回的 shots 不是数组")
    shots = _build_shots_from_theme_items(shots_data)
    if len(shots) < min_shots:
        raise ValueError(
            f"模型仅返回 {len(shots)} 个镜头，少于要求下限 {min_shots}，请改主题或增大 max_shots 后重试"
        )
    if len(shots) > max_shots:
        shots = shots[:max_shots]

    doc = StoryboardDocument(
        title=str(data.get("title", "")),
        synopsis=str(data.get("synopsis", "")),
        characters=str(data.get("characters", "")),
        source_video=format_theme_source_tag(theme),
        shots=shots,
        raw_scene_analyses=[f"[from_theme]{theme[:2000]}"],
    )
    return doc


def format_theme_source_tag(theme: str) -> str:
    """写入 source_video 字段便于区分来源（可选）。"""
    t = theme.strip().replace("\n", " ")[:120]
    return f"theme:{t}"
