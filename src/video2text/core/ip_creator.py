"""IP 创建引导流程：LLM 引导的创意会话，从种子创意生成完整 IP 提案。

流程:
  1. 用户给出种子创意 + 可选风格选择
  2. LLM 扩展为完整 IP 提案（视觉DNA/故事DNA/世界观/角色花名册）
  3. 用户审阅、修改
  4. 确认后保存 IP 元数据
  5. 为每个角色生成参考图
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from video2text.config.settings import Settings, resolve_theme_story_model
from video2text.core.analyzer import _extract_json_object
from video2text.core.ip_manager import (
    IPCharacter,
    IPProfile,
    StoryDNA,
    VisualDNA,
    WorldDNA,
    generate_character_id,
    generate_ip_id,
    get_character_reference_path,
    save_character_reference_image,
    save_ip,
    update_character_reference_in_profile,
)
from video2text.core.styles import format_styles_for_llm, get_style_keywords
from video2text.services.image_gen import (
    build_character_image_prompt,
    generate_image,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IP 提案生成 System Prompt
# ---------------------------------------------------------------------------

IP_CREATOR_SYSTEM = """You are an expert creative director specializing in short-form video IP (Intellectual Property) design for platforms like YouTube Shorts and TikTok.

Your task is to take a SEED IDEA from the user and expand it into a complete IP proposal — a "Creative Genome" that defines everything needed to create a consistent series of short videos.

CRITICAL: The IP is for SHORT-FORM VIDEO (5-30 seconds per episode). Each episode is a standalone mini-story. No long-form narrative arcs. Think: viral, visual, fast-paced, understandable WITHOUT audio.

Output **strict JSON only** (no Markdown code blocks, no preamble). ALL fields except name/tagline should be in ENGLISH for downstream video generation. name and tagline should be in the user's input language (likely Chinese).

{
  "name": "IP 名称（用户输入语言）",
  "name_en": "English name",
  "tagline": "一句话标语（用户输入语言）",
  "visual_dna": {
    "style_preset_id": "MUST be one of the style IDs from the available list below",
    "style_keywords": "中文风格关键词, 逗号分隔",
    "style_keywords_en": "English style keywords, comma separated",
    "color_tone": "Color palette description",
    "lighting_preference": "Preferred lighting style"
  },
  "story_dna": {
    "genre": "Genre tags, e.g. comedy/slice-of-life/heartwarming",
    "narrative_pattern": "Repeatable story formula: setup -> complication -> punchline",
    "emotional_tone": "Primary emotional register",
    "pacing": "Pacing description for short-form, e.g. fast-paced, 2-3s per shot",
    "episode_structure": "How each episode is structured, e.g. standalone mini-story",
    "typical_plot_hooks": [
      "5-8 reusable plot hook ideas that fit this IP's personality",
      "Each hook should be a single sentence describing a mini-story premise",
      "These will be used as inspiration for future episode generation"
    ]
  },
  "world_dna": {
    "primary_setting": "Main recurring location with visual details",
    "recurring_locations": ["List of 3-5 locations that episodes can take place in"],
    "world_rules": "How the world works (e.g. animals are anthropomorphic, magic exists, etc.)"
  },
  "characters": [
    {
      "name": "角色名（用户输入语言）",
      "name_en": "English name",
      "role": "protagonist or supporting",
      "visual_description": "EXTREMELY DETAILED visual description for text-to-image generation. Must include: species/type, body shape, face details (eye color, expression), hair/fur details, specific outfit with colors and materials, accessories. This description alone must be sufficient to generate a consistent character image. Write in Chinese for best text-to-image results.",
      "personality": "2-4 core personality traits",
      "behavior_patterns": ["3-5 signature behaviors/reactions that make this character recognizable in video"],
      "relationship": "Role in the cast and relationships with other characters"
    }
  ]
}

CHARACTER DESIGN RULES:
1. visual_description MUST be detailed enough for a text-to-image model to generate the character. Include: body type, face shape, eye color/shape, hair/fur style and color, exact outfit (color + material + style), footwear, accessories.
2. Each character needs 3-5 behavior_patterns — these are the character's "visual vocabulary" that viewers will recognize across episodes (e.g. "eyes bulge when seeing food", "does a victory dance wiggle").
3. The protagonist should have a clear flaw or quirk that drives comedy/conflict.
4. Supporting characters should complement or contrast with the protagonist.
5. Limit to 2-4 characters total for consistency.

STORY DNA RULES:
1. narrative_pattern should be a reusable FORMULA, not a single plot.
2. typical_plot_hooks should be 5-8 diverse ideas that all fit the formula.
3. pacing MUST emphasize: fast cuts (2-3s per shot), visual storytelling (understandable without audio), exaggerated expressions and reactions.

AVAILABLE STYLE PRESETS:
{style_list}

Choose the most appropriate style_preset_id from the list above. You may also add custom keywords beyond the preset.
"""


# ---------------------------------------------------------------------------
# IP 提案生成
# ---------------------------------------------------------------------------


def generate_ip_proposal(
    seed_idea: str,
    settings: Settings,
    *,
    style_preset_id: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """从种子创意生成完整 IP 提案 JSON。

    Args:
        seed_idea: 用户的创意种子文本
        settings: 配置
        style_preset_id: 可选的预选风格 ID
        model: 可选的 LLM 模型覆盖

    Returns:
        IP 提案 dict（尚未保存，供用户审阅）
    """
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    use_model = resolve_theme_story_model(settings, override=model)

    style_list = format_styles_for_llm()
    system = IP_CREATOR_SYSTEM.replace("{style_list}", style_list)

    user_parts = [f"种子创意：{seed_idea}"]
    if style_preset_id:
        kw = get_style_keywords(style_preset_id, "zh")
        user_parts.append(f"用户已选择风格预设：{style_preset_id}（{kw}），请基于此风格展开。")
    user_parts.append("请输出完整的 IP 提案 JSON。")

    log.info("Generating IP proposal from seed: %s", seed_idea[:100])
    completion = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
        max_tokens=8192,
    )

    raw = completion.choices[0].message.content or ""
    proposal = _extract_json_object(raw)
    log.info(
        "IP proposal generated: name=%r, %d characters",
        proposal.get("name", ""),
        len(proposal.get("characters") or []),
    )
    return proposal


# ---------------------------------------------------------------------------
# 从提案创建 IPProfile
# ---------------------------------------------------------------------------


def create_ip_from_proposal(
    proposal: dict[str, Any],
    username: str,
) -> IPProfile:
    """将 LLM 生成的提案转为 IPProfile 并保存到文件系统。"""
    ip_id = generate_ip_id()

    characters: list[IPCharacter] = []
    for c in proposal.get("characters") or []:
        char = IPCharacter(
            id=generate_character_id(),
            name=str(c.get("name", "")),
            name_en=str(c.get("name_en", "")),
            role=str(c.get("role", "supporting")),
            visual_description=str(c.get("visual_description", "")),
            personality=str(c.get("personality", "")),
            behavior_patterns=list(c.get("behavior_patterns") or []),
            relationship=str(c.get("relationship", "")),
        )
        characters.append(char)

    vdna_raw = proposal.get("visual_dna") or {}
    sdna_raw = proposal.get("story_dna") or {}
    wdna_raw = proposal.get("world_dna") or {}

    profile = IPProfile(
        id=ip_id,
        name=str(proposal.get("name", "")),
        name_en=str(proposal.get("name_en", "")),
        tagline=str(proposal.get("tagline", "")),
        visual_dna=VisualDNA.from_dict(vdna_raw),
        story_dna=StoryDNA.from_dict(sdna_raw),
        world_dna=WorldDNA.from_dict(wdna_raw),
        characters=characters,
    )

    save_ip(username, profile)
    log.info("IP created: id=%s, name=%r, user=%s", ip_id, profile.name, username)
    return profile


# ---------------------------------------------------------------------------
# 角色参考图生成
# ---------------------------------------------------------------------------


def generate_character_images(
    profile: IPProfile,
    username: str,
    settings: Settings,
    *,
    char_ids: list[str] | None = None,
    progress_cb: Any = None,
) -> IPProfile:
    """为 IP 中的角色生成参考图。

    Args:
        profile: IP profile
        username: 用户名
        settings: 配置
        char_ids: 只为指定角色生成；None 则为所有缺图的角色生成
        progress_cb: 可选的进度回调 (message: str) -> None

    Returns:
        更新后的 IPProfile
    """
    targets = profile.characters
    if char_ids:
        targets = [c for c in targets if c.id in char_ids]

    if not targets:
        return profile

    cb = progress_cb or (lambda m: None)
    total = len(targets)

    for i, char in enumerate(targets, 1):
        if char.reference_image_path and not char_ids:
            cb(f"角色 {char.name} 已有参考图，跳过 ({i}/{total})")
            continue

        cb(f"正在为角色 {char.name} 生成参考图 ({i}/{total})…")

        prompt = build_character_image_prompt(char, profile.visual_dna)
        log.info("Generating character image: %s, prompt=%s", char.name, prompt[:200])

        dest = get_character_reference_path(username, profile.id, char.id)

        try:
            img_path = generate_image(
                prompt,
                settings,
                save_to=dest,
            )
            char.reference_image_path = str(img_path)
            char.reference_type = "generated"
            cb(f"角色 {char.name} 参考图已生成")
        except Exception as exc:
            log.error("Failed to generate image for %s: %s", char.name, exc)
            cb(f"角色 {char.name} 图片生成失败: {exc}")

    save_ip(username, profile)
    return profile


# ---------------------------------------------------------------------------
# AI 润色
# ---------------------------------------------------------------------------

_REFINE_SYSTEM = """You are an expert creative director. The user will provide:
1. The full IP context (name, visual_dna, story_dna, characters, etc.)
2. A SECTION of the IP they want to refine (e.g. visual_dna, story_dna, a character)
3. The CURRENT CONTENT of that section
4. Their INSTRUCTION for how to modify it

Your task: apply the user's instruction to modify the section while maintaining consistency with the rest of the IP. Output ONLY the modified section as strict JSON (no Markdown, no preamble). Keep the same JSON schema/keys as the input.

RULES:
- Maintain consistency with the IP's overall style, tone, and world
- Only change what the user asks for; preserve everything else
- If modifying a character, keep visual_description detailed enough for text-to-image
- All English content stays English; Chinese content stays Chinese (matching original)
"""


def refine_ip_section(
    profile: IPProfile,
    settings: Settings,
    *,
    section: str,
    instruction: str,
    current_content: str | dict | list = "",
    model: str | None = None,
) -> dict | str:
    """AI 润色 IP 的某个段落。

    Args:
        profile: IP profile（提供全局 context）
        settings: 配置
        section: 段落类型，如 visual_dna / story_dna / world_dna / character / story_outline / storyboard_shot
        instruction: 用户的修改意见
        current_content: 当前内容（JSON dict/list 或字符串）

    Returns:
        修改后的内容（与输入格式匹配）
    """
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    use_model = resolve_theme_story_model(settings, override=model)

    ip_context = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    if isinstance(current_content, (dict, list)):
        content_str = json.dumps(current_content, ensure_ascii=False, indent=2)
    else:
        content_str = str(current_content)

    user_msg = (
        f"=== IP CONTEXT ===\n{ip_context}\n=== END CONTEXT ===\n\n"
        f"SECTION: {section}\n\n"
        f"CURRENT CONTENT:\n{content_str}\n\n"
        f"INSTRUCTION: {instruction}\n\n"
        f"Output the modified section as strict JSON only."
    )

    log.info("Refining IP section=%s, instruction=%s", section, instruction[:100])
    completion = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": _REFINE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=4096,
    )
    raw = completion.choices[0].message.content or ""
    try:
        return _extract_json_object(raw)
    except Exception:
        return raw.strip()
