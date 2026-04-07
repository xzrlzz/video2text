"""根据用户给定的主题文本，由大模型创作故事并输出与 video2text 兼容的分镜 JSON。"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from video2text.config.settings import Settings
from video2text.core.analyzer import _extract_json_object, _shot_from_analysis_dict
from video2text.core.storyboard import Shot, StoryboardDocument


THEME_STORYBOARD_SYSTEM = """You are a professional film screenwriter and storyboard director. The user will provide a story theme or creative description (possibly brief). You need to:
1. Expand it into a complete, filmable short film story (with narrative arc or clear emotional curve);
2. Break it down into a professional shot list, with audiovisual and staging details for each shot;
3. For each shot with dialogue, write the [dialogue] field in ENGLISH: indicate the speaker, e.g. "Alex: \"Are you okay?\""; no dialogue → empty string "";
4. Write one English generation_prompt for each shot, for use by text-to-video or reference-to-video models.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble or postamble).
- ALL text fields in the JSON must be written in ENGLISH. No Chinese or other non-English characters anywhere.
- Root object field names must match exactly:

{
  "title": "Short film title in English",
  "synopsis": "2–5 sentences of story synopsis + optional overall mood/pacing notes, in English",
  "characters": "Main character names, personalities, and relationships in English; character names in dialogue must match these",
  "shots": [
    {
      "characters_in_shot": ["character name 1", "character name 2"],
      "shot_type": "Shot scale: extreme wide / wide / medium / close / extreme close / macro, etc.",
      "camera_movement": "Angle + camera movement with speed, in English. E.g.: eye-level, slow push-in; overhead, fast pan left",
      "scene_description": "Environment, composition, key props in English. E.g.: dimly lit subway platform, rule-of-thirds framing, worn wooden bench in foreground",
      "character_action": "Detailed character actions, micro-expressions, and body language in this shot (no appearance/outfit details), in English. MUST include: (1) specific facial micro-expressions (e.g. slight frown, eyes narrowing, lip quivering, jaw clenching); (2) body language and gestures (e.g. fingers drumming on table, shoulders dropping, leaning forward); (3) movement quality (hesitant, fluid, abrupt). E.g.: young woman's eyes narrow with suspicion, her lips press into a thin line as her fingers tighten around the umbrella handle, shoulders tensing visibly",
      "dialogue": "Speaker name followed by line in English. E.g.: Alex: \"Are you okay?\" — or empty string \"\" if no dialogue",
      "mood": "Emotional atmosphere in English. E.g.: melancholic, tense, euphoric",
      "lighting": "Lighting description in English. E.g.: Rembrandt lighting, soft backlight, neon rim light",
      "audio_description": "Music / sound effects / ambient sound (excluding dialogue) in English. E.g.: distant train rumble, melancholic piano melody",
      "generation_prompt": "PURE ENGLISH. Single paragraph. MUST start with the character's name if a character appears in this shot (e.g. 'Alex sits alone on a subway bench...' NOT 'A young woman sits...'). Formula: [character name if present] + [shot scale + angle + camera move] + subject action with micro-expressions and body language + light/mood keywords. Include specific expressions and gestures but NO appearance/outfit details. E.g.: 'Alex, medium close-up, eye-level slow push-in, sits alone on a subway bench, eyes darting sideways with quiet unease, fingers gripping the bench edge, jaw slightly clenched, soft overhead fluorescent light with cool blue cast, melancholic mood.'",
      "duration_sec": 4.0
    }
  ]
}

- shots are in narrative chronological order; duration_sec is a positive number in seconds, recommended 2–8 seconds per shot; total shot count must fall within the user-specified range.
- characters_in_shot: an array of character names that appear in this shot. Use the EXACT names from the "characters" field. Empty array [] for shots with no characters (e.g. landscape, establishing shots). This is critical for downstream character-consistent video generation.
- EVERY text field must be in English. Dialogue must be in English — this video is intended for an international English-speaking audience.
- generation_prompt: CRITICAL — always use the character's actual name (from the characters field) in the generation_prompt, never say "a man", "a woman", "the character" etc. Using the character's name is essential for maintaining visual consistency across shots."""


NEXT_SHOT_SYSTEM = """You are a professional film screenwriter and storyboard director. The user will provide an existing shot list and overall story information. You need to write the next shot for the story.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble or postamble).
- ALL text fields must be in ENGLISH. No Chinese or other non-English characters anywhere.
- Output a single shot object with the following fields:

{
  "characters_in_shot": ["character name"],
  "shot_type": "Shot scale: extreme wide / wide / medium / close / extreme close / macro, etc.",
  "camera_movement": "Angle + camera movement with speed, in English",
  "scene_description": "Environment, composition, key props in English",
  "character_action": "Detailed character actions, micro-expressions, and body language (no appearance/outfit details), in English. Include specific facial expressions, gestures, and movement quality to make characters feel human and alive.",
  "dialogue": "Speaker: \"line in English\" — or empty string \"\" if no dialogue",
  "mood": "Emotional atmosphere in English",
  "lighting": "Lighting description in English",
  "audio_description": "Music / sound effects / ambient sound (excluding dialogue) in English",
  "generation_prompt": "PURE ENGLISH. Single paragraph. MUST use the character's actual name if they appear. Formula: [character name] + [shot scale + camera move] + action with micro-expressions and body language (no appearance) + light/mood. E.g.: 'Sarah, wide shot, slow pull-back, stands at rain-soaked window with eyes glistening, one hand pressed against the glass, chin slightly trembling, warm backlight against cold exterior, bittersweet longing.'",
  "duration_sec": 4.0
}

- characters_in_shot: array of character names present in this shot (use exact names from the existing characters list). Empty array [] if no characters.
- The new shot must naturally connect with the existing story in plot and atmosphere.
- ALL fields must be in English. This video targets an international English-speaking audience.
- generation_prompt: CRITICAL — always use the character's actual name, never generic pronouns like "a man", "a woman", "the character"."""


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
        extra += f"\nStyle/genre preference: {style_hint.strip()}"
    user_msg = (
        f"Story theme / creative brief:\n{theme}\n\n"
        f"Generate {min_shots}–{max_shots} shots (shots array length must fall within this range)."
        f"{extra}\nOutput JSON only. All text fields must be in English."
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


def generate_next_shot(
    theme: str,
    settings: Settings,
    existing_shots: list[dict[str, Any]],
    *,
    title: str = "",
    synopsis: str = "",
    characters: str = "",
    style_hint: str = "",
    model: str | None = None,
) -> Shot:
    """
    根据已有分镜列表，续写下一个镜头。
    返回一个 Shot 对象，由调用方追加到 storyboard。
    """
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    use_model = (model or settings.theme_story_model or settings.vision_model).strip()
    if not use_model:
        use_model = settings.vision_model

    # Build context from existing shots (last 6 to limit tokens)
    recent = existing_shots[-6:] if len(existing_shots) > 6 else existing_shots
    context_lines = []
    if title:
        context_lines.append(f"Story title: {title}")
    if synopsis:
        context_lines.append(f"Synopsis: {synopsis}")
    if characters:
        context_lines.append(f"Characters: {characters}")
    if style_hint:
        context_lines.append(f"Style preference: {style_hint}")
    context_lines.append(f"\nExisting storyboard has {len(existing_shots)} shots. Most recent shots:")
    for i, s in enumerate(recent, start=len(existing_shots) - len(recent) + 1):
        context_lines.append(
            f"  Shot {i}: {s.get('scene_description', '')} | {s.get('character_action', '')} | {s.get('dialogue', '')}"
        )
    context_lines.append("\nWrite the next shot as a single JSON object. Output JSON only. All fields in English.")

    user_msg = "\n".join(context_lines)

    completion = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": NEXT_SHOT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = completion.choices[0].message.content or ""
    data = _extract_json_object(raw)

    # 计算时间轴（从已有分镜末尾续接）
    cursor = 0.0
    for s in existing_shots:
        try:
            cursor += float(s.get("duration", s.get("duration_sec", 4.0)))
        except (TypeError, ValueError):
            cursor += 4.0

    ds = data.get("duration_sec")
    try:
        d = float(ds) if ds is not None else 4.0
    except (TypeError, ValueError):
        d = 4.0
    d = max(0.5, min(30.0, d))

    shot_id = len(existing_shots) + 1
    return _shot_from_analysis_dict(shot_id, data, cursor, cursor + d)


def format_theme_source_tag(theme: str) -> str:
    """写入 source_video 字段便于区分来源（可选）。"""
    t = theme.strip().replace("\n", " ")[:120]
    return f"theme:{t}"
