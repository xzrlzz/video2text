"""根据用户给定的主题文本，由大模型创作故事并输出与 video2text 兼容的分镜 JSON。

两阶段流程：
  Phase 1 — Story Architect: 从主题生成完整故事大纲（叙事节拍、角色弧线、对白）
  Phase 2 — Shot Designer:  基于故事大纲设计分镜，写出与叙事紧密绑定的 generation_prompt
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from video2text.config.settings import Settings
from video2text.core.analyzer import _extract_json_object, _shot_from_analysis_dict
from video2text.core.storyboard import Shot, StoryboardDocument

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from video2text.core.ip_manager import IPProfile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 1 — Story Architect
# ---------------------------------------------------------------------------

STORY_ARCHITECT_SYSTEM = """You are an expert film screenwriter. Your sole task is to create a COMPLETE, VISUALLY CONCRETE short film story from a theme or creative brief. You are NOT designing shots or camera work — only the STORY.

CRITICAL PRINCIPLE: Every narrative beat must be FILMABLE — only things a camera can see and a microphone can hear. "She feels sad" is WRONG. "She is sitting alone at a table, slowly pushing food around her plate, her eyes red-rimmed" is CORRECT.

Output **strict JSON only** (no Markdown code blocks, no preamble). ALL fields in ENGLISH — this is NON-NEGOTIABLE.

LANGUAGE RULE (ABSOLUTE — ZERO EXCEPTIONS):
- Every single text field in the JSON MUST be written in English. No Chinese, Japanese, Korean, or ANY non-English characters allowed anywhere.
- ALL dialogue MUST be in English. Even if the theme/brief is given in Chinese, the dialogue you write MUST be in English.
- Characters MUST speak English. Write natural, fluent English dialogue.
- If the story is set in a non-English-speaking location, characters still speak English (like a dubbed film).

{
  "title": "Evocative working title reflecting the core theme",
  "logline": "One compelling sentence: WHO wants WHAT, but WHAT stands in the way?",
  "synopsis": "3-5 sentences covering: (1) setup and character goal, (2) central tension/conflict, (3) climax, (4) resolution or lingering question.",
  "setting": {
    "primary_location": "Detailed physical space with sensory anchors — textures, colors, light quality, sounds. This is the PERSISTENT VISUAL WORLD shared by all shots.",
    "time_of_day": "dawn / morning / midday / afternoon / dusk / night",
    "atmosphere": "Overall mood the environment contributes (oppressive, warm, sterile, chaotic, etc.)"
  },
  "characters": [
    {
      "name": "Short, distinctive name",
      "role": "protagonist / antagonist / supporting / environment",
      "motivation": "What they WANT in this story",
      "emotional_arc": "How their emotional state CHANGES from beginning to end",
      "key_trait": "One defining behavioral trait visible on screen (fidgets with ring, avoids eye contact, speaks too fast, etc.)"
    }
  ],
  "narrative_beats": [
    {
      "beat_id": 1,
      "beat_type": "SETUP | INCITING_INCIDENT | RISING_ACTION | CLIMAX | FALLING_ACTION | RESOLUTION",
      "description": "VISUALLY CONCRETE description of what happens. What does the camera SEE? What does the microphone HEAR?",
      "characters_involved": ["exact character names"],
      "emotional_tone": "What the AUDIENCE should feel (tension, warmth, unease, relief, etc.)",
      "key_action": "The SPECIFIC physical action that makes this beat visible, filmable in 2-5 seconds. E.g.: 'She slams the letter on the table, sending the coffee cup rattling.'",
      "dialogue": "'Speaker: \"Line.\"' format, or empty string if no speech",
      "visual_focus": "What should be the CENTER of the frame. E.g.: 'Her trembling hand holding the crumpled photograph.'"
    }
  ],
  "emotional_arc_summary": "One paragraph: how tension/emotion builds, peaks, and resolves. Reference specific beat_ids."
}

STORY QUALITY RULES:
1. SHOW DON'T TELL: Every beat must be expressible through action, dialogue, or visual detail. No internal monologue. No narrator.
2. CAUSE AND EFFECT: Each beat logically follows from the previous. The audience always understands WHY.
3. EMOTIONAL ESCALATION: Beats build in intensity toward the climax. Don't peak too early.
4. CHARACTER THROUGH ACTION: Reveal personality through behavior. A nervous person fidgets; a controlling person adjusts objects; a grieving person holds onto a memento.
5. SPECIFICITY: "A crumpled letter with a coffee stain" beats "a letter." "Gripping the steering wheel until knuckles turn white" beats "nervous while driving."
6. VISUAL VARIETY: Mix action types — movement, object interaction, reaction, confrontation, stillness. Not every beat is "sit and talk."
7. DIALOGUE ECONOMY: Short, impactful lines. Each reveals character or advances plot. No exposition dumps.
8. CONCRETE TRANSITIONS: End each beat with a visual or audio element that naturally leads to the next (a sound, a glance, an object handed over, a door opening). The audience should never wonder "why did we cut here?"

BEAT COUNT: Aim for roughly (requested_shot_count ÷ 1.5) beats. Some beats will span 2-3 shots; some are single shots."""


# ---------------------------------------------------------------------------
# Phase 2 — Shot Designer (receives complete story outline)
# ---------------------------------------------------------------------------

SHOT_DESIGNER_SYSTEM = """You are a professional storyboard director and AI video prompt engineer. You are given a COMPLETED STORY OUTLINE with narrative beats, characters, and setting. Your job is to EXECUTE this story visually as a shot list. Do NOT reinvent the story.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble or postamble).
- ALL text fields must be in ENGLISH — ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere in the JSON.
- ALL dialogue MUST be in English. Characters MUST speak English regardless of the story setting or theme language.
- Do NOT change the story, characters, or dialogue from the outline. Faithfully translate each narrative beat into 1-3 shots.

JSON SCHEMA:
{
  "title": "string (from outline)",
  "synopsis": "string (from outline)",
  "rhythm_profile": "TENSE_RAPID | CONTEMPLATIVE_SLOW | ACTION_DRIVEN | EMOTIONAL_CRESCENDO",
  "characters": [{"name": "string", "description": "string"}],
  "shots": [
    {
      "shot_id": 1,
      "narrative_beat_id": 1,
      "continuity_anchor": "EXACT spatial/visual link to previous shot. E.g.: 'Match on action: door continues swinging', 'Eyeline match: showing what she was staring at'",
      "characters_in_shot": ["EXACT names from characters"],
      "focal_character": "primary subject or null",
      "shot_type": "ECU / CU / MCU / MS / WS / EWS / Two-Shot / OTS / POV",
      "camera_movement": "movement + speed (static, slow push-in, fast pan left, etc.)",
      "scene_description": "Environment and composition. MUST reference a persistent spatial anchor from the setting.",
      "character_action": "Present Continuous tense. Include: (1) micro-expression, (2) body language, (3) movement quality.",
      "dialogue": "'CHARACTER: \"Line.\"' or empty string",
      "mood": "emotional atmosphere",
      "lighting": "Direction + Hard/Soft + Source. Consistent within same location.",
      "ambient_sound": "Diegetic in-world sounds only. Maintain continuity across shots.",
      "score_suggestion": "Post-production music reference (optional).",
      "generation_prompt": "CRITICAL — see rules below",
      "duration_sec": 2.0,
      "cut_rhythm": "SMASH_CUT / STANDARD_CUT / LINGERING_CUT / JUMP_CUT / MATCH_CUT",
      "negative_prompt_hint": "Elements to avoid: distorted face, extra limbs, text, etc."
    }
  ]
}

GENERATION_PROMPT RULES (MOST IMPORTANT):
Each generation_prompt must read as a NARRATIVE MOMENT, not an isolated image. It must answer THREE questions:
  (a) What is the CHARACTER DOING and WHY? (intention + emotion)
  (b) WHERE are we in the story? (spatial + narrative context)
  (c) How does this connect to what we just SAW? (visual continuity)

Formula: [framing + camera] + [spatial anchor from setting] + [character name + pose anchor] + [motivated action with micro-expression revealing intent] + [lighting with source] + [cinematic qualifier].

WEAK: "Close-up of a woman looking sad. Soft lighting."
STRONG: "Close-up framing from chin to forehead. Against the same rain-streaked diner window from the establishing shot, JANE is seated motionless, her coffee growing cold. Having just heard MARK's confession, she is slowly closing her eyes, a single tear tracing down her cheek as her jaw tightens — suppressing the urge to respond. Warm Rembrandt light from the candle on the table catches the tear. Cinematic 35mm, shallow depth of field."

CONTINUITY RULES:
1. 180-DEGREE RULE: Consistent screen direction in dialogue/interaction scenes.
2. EYELINE MATCH: Cut from character looking off-screen → what they see must match the angle.
3. SPATIAL ANCHORING: Every scene_description references the setting's persistent elements.
4. ACTION CONTINUITY: Action started in shot N completes in shot N+1.
5. LIGHTING CONTINUITY: Identical within same scene unless story-motivated change.

TIMING (SHORT-FORM VIDEO — FAST PACING IS CRITICAL):
- DEFAULT 2s: Non-dialogue shots should be ~2 seconds. Use static or quick camera moves only.
- DIALOGUE 3-4s: Shots with spoken dialogue get 3-4 seconds to deliver the line, no more.
- NEVER exceed 5s for any single shot. This is short-form content — every second counts.
- Prefer SMASH_CUT and STANDARD_CUT. LINGERING_CUT should be rare (climax only, max 4s).
- Every 3-shot sequence needs at least one clear visual link."""


# ---------------------------------------------------------------------------
# Legacy single-pass prompt (kept for fallback compatibility)
# ---------------------------------------------------------------------------

THEME_STORYBOARD_SYSTEM = """You are a professional film screenwriter and storyboard director with deep expertise in AI video generation pipelines. The user will provide a story theme or creative description (possibly brief). You need to:
1. Expand it into a complete, filmable short film story (with narrative arc or clear emotional curve);
2. Break it down into a professional shot list with STRICT VISUAL CONTINUITY between shots;
3. For each shot with dialogue, write the [dialogue] field in ENGLISH: indicate the speaker, e.g. "Alex: \\"Are you okay?\\""; no dialogue → empty string "";
4. Write one English generation_prompt for each shot, optimized for text-to-video or image-to-video AI models.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble or postamble).
- ALL text fields in the JSON must be written in ENGLISH. ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere.
- ALL dialogue lines MUST be in English. Even if the user's theme is in Chinese, ALL character speech MUST be natural, fluent English. Characters always speak English like a dubbed film.
- Root object field names must match exactly as defined in the schema below.

JSON SCHEMA STRUCTURE:
{
  "title": "string",
  "synopsis": "string",
  "rhythm_profile": "string (One of: 'TENSE_RAPID', 'CONTEMPLATIVE_SLOW', 'ACTION_DRIVEN', 'EMOTIONAL_CRESCENDO'. Determines cutting pattern.)",
  "characters": [
    {
      "name": "string",
      "description": "string (brief visual and personality description)"
    }
  ],
  "shots": [
    {
      "shot_id": number,
      "continuity_anchor": "string (CRITICAL: Describes the EXACT spatial or visual element that links this shot to the previous shot. Examples: 'Match on action: door continues swinging from previous shot', 'Eyeline match: following her gaze from shot 3', 'Graphic match: circular lamp echoes previous shot\\'s moon', 'Same background wall texture as shot 2', 'Direct cut to reverse angle of same moment', 'Continuation of walking path from previous frame')",
      "characters_in_shot": ["string (EXACT names from characters list, empty array for no characters)"],
      "focal_character": "string or null (the primary subject in focus, even if multiple characters present)",
      "shot_type": "string (ECU, CU, MCU, MS, WS, EWS, Two-Shot, OTS, POV)",
      "camera_movement": "string",
      "scene_description": "string (detailed visual of the environment and composition. MUST include spatial reference that persists across adjacent shots: e.g., 'same room, opposite corner', 'same park bench, different angle')",
      "character_action": "string (MUST use Present Continuous tense: 'is walking', 'is staring', etc.)",
      "dialogue": "string (Speaker-attributed lines. Each line MUST begin with speaker name: 'CharName: spoken words'. Multiple lines separated by newlines. No stage directions, no SFX. Empty string if no speech.)",
      "mood": "string",
      "lighting": "string (MUST follow format: Direction + Hard/Soft + Source. MUST remain consistent within the same scene/location. Example: 'Top-left hard key light from window, soft fill from practical lamp')",
      "ambient_sound": "string (ONLY diegetic, in-world sounds. Describe what is heard within the scene: footsteps, wind, traffic, etc. Do NOT include musical score here.)",
      "score_suggestion": "string (Optional post-production music reference. Not for AI sound generation.)",
      "generation_prompt": "string (CRITICAL RULES below. MUST include spatial continuity cues. Balance ~50% character appearance + ~50% cinematography/staging.)",
      "duration_sec": number,
      "cut_rhythm": "string (One of: 'SMASH_CUT', 'STANDARD_CUT', 'LINGERING_CUT', 'JUMP_CUT', 'MATCH_CUT'. Determines how this shot transitions from previous.)",
      "negative_prompt_hint": "string (Specific elements to avoid in this shot: e.g., 'distorted face, extra limbs, text, slow motion, scene change hallucination')"
    }
  ]
}

RHYTHM PROFILE DEFINITIONS:
- TENSE_RAPID: Short durations (1.5-2s), rapid cutting, minimal camera movement, high shot count. Use for suspense, arguments, panic.
- CONTEMPLATIVE_SLOW: Moderate durations (2-4s), slow camera moves. Use for grief, wonder, intimacy.
- ACTION_DRIVEN: Mixed durations (2-3s), dynamic camera, emphasis on match cuts and motion continuity.
- EMOTIONAL_CRESCENDO: Starts at 3s, gradually decreases to 2s toward climax.

CUT RHYTHM DEFINITIONS:
- SMASH_CUT: Abrupt transition, duration <= 2s. Creates shock or urgency.
- STANDARD_CUT: Invisible transition, duration 2-3s. Narrative flow.
- LINGERING_CUT: Extended hold, duration 3-4s. Emotional weight. Use sparingly.
- JUMP_CUT: Same framing, subject position shifts abruptly. Disorientation or time passage.
- MATCH_CUT: Visual element from previous shot carries into this shot's composition.

VISUAL CONTINUITY RULES (CRITICAL FOR COHERENT SEQUENCES):
1. 180-DEGREE RULE: In dialogue or two-character scenes, maintain consistent screen direction. If character A faces right in shot 1, they must face right in all shots until crossing the line is explicitly motivated.
2. EYELINE MATCH: When cutting from a character looking off-screen to what they see, the viewed object must match the eyeline angle described in the previous shot.
3. SPATIAL ANCHORING: Every shot's scene_description must reference a persistent environmental element from the same location. First shot of new location establishes it; subsequent shots reference it (e.g., "Same cracked leather chair from the wide shot").
4. ACTION CONTINUITY: If an action starts in shot N (e.g., "She is reaching for the door handle"), shot N+1 must complete or follow that action logically ("Her hand is now gripping the handle, beginning to turn it").
5. LIGHTING CONTINUITY: lighting description must remain identical for all shots within the same scene. Do not change light direction or quality unless story motivation exists (e.g., cloud passes, lamp turns on).

SHOT SEQUENCING AND TIMING RULES (SHORT-FORM VIDEO — FAST PACING):
- Shots are in narrative chronological order.
- duration_sec is a positive number. SHORT-FORM PACING:
  - ~2 seconds (DEFAULT): Non-dialogue shots — static, quick whip pan, smash cut. This is the standard duration.
  - 3-4 seconds: Shots WITH DIALOGUE — just enough to deliver the line naturally.
  - NEVER exceed 5 seconds for any single shot. This is short-form content.
- Total shot count must fall within the user-specified range.
- 70-80% of shots should be ~2 seconds. Only dialogue shots should be longer.
- Every sequence of 3 shots must contain at least one clear visual link (object, color, motion, or spatial reference) that connects them.

GENERATION_PROMPT CONSTRUCTION RULES (EXTREMELY CRITICAL):
Each generation_prompt MUST be a single, cohesive paragraph balanced ~50% character appearance + ~50% cinematography/staging:
1. CHARACTER APPEARANCE FIRST: For each character in the shot, write their name followed by key visual traits in parentheses (gender, approximate age, build, hair color/style/length, main clothing). E.g.: 'Emma (young woman, slim, long black straight hair, white blouse, blue jeans)'.
2. FRAMING: Translate shot_type into explicit visual language. Do NOT use film terms like "CU" alone.
3. KEY POSE ANCHOR: Describe the subject's EXACT initial physical state.
4. ACTION: Use PRESENT CONTINUOUS tense.
5. SPATIAL CONTINUITY CUE: Include a visual anchor linking to previous shot.
6. LIGHTING MOOD: Incorporate the lighting field description naturally.
7. CINEMATIC QUALIFIER: Append a short style anchor.
8. If dialogue occurs in this shot, quote it with exact words in the generation_prompt.

GENERATION_PROMPT EXAMPLE:
"Emma (young woman, slim, long black straight hair, white blouse, blue jeans), medium shot framing from waist to top of head. She is standing motionless in the same dim hallway from the establishing shot, her back now against the peeling wallpaper, staring directly into the lens with a tense expression, her eyes darting slightly leftward. Hard key light from a single overhead bulb. Cinematic 35mm film, shallow depth of field."

AUDIO FIELD CLARIFICATION:
- ambient_sound: STRICTLY for sounds occurring inside the world of the film (doors creaking, wind, footsteps). This may be used for AI sound effect generation. If there is no dialogue, ambient_sound MUST be populated to prevent dead air.
- ambient_sound should maintain continuity: if wind is established in shot 1, it should persist in subsequent shots of the same scene unless explicitly stopped.
- score_suggestion: For post-production reference ONLY. Do not merge with ambient_sound.

DIALOGUE FORMAT:
- Each spoken line MUST begin with the character's name: "CharName: spoken words".
- Multiple utterances in one shot: separate with single newlines, each prefixed with speaker name.
- No stage directions (not "(whispering)"), no narrator text, no SFX. Empty string "" for shots with no speech.

LANGUAGE RULES:
- Every field **except** dialogue must be in **English**.
- **dialogue field (language):** Use **English** for spoken lines when the story is English-language (default). If the user's theme/brief is **primarily in Chinese** or explicitly requests **Chinese dialogue**, write spoken lines in **Chinese** (still speaker-prefixed, e.g. "小明: 你好吗？"); do **not** translate Chinese lines to English in dialogue. All other JSON fields stay English.
- Do NOT paraphrase dialogue elsewhere: character_action may describe how they speak; dialogue holds speaker-attributed lines only.

PACING AND TENSION BUILDING (SHORT-FORM — EVERY SECOND COUNTS):
- DEFAULT: Most shots are ~2 seconds. Only add time for dialogue delivery.
- To create urgency: Use 1.5-2s shots with SMASH_CUT. Dialogue fragments only.
- To build tension: Use 2s shots, slowing to 3s at the emotional peak, then back to 2s.
- For compact storytelling: NO pure establishing shots. Start in medias res. Use detail inserts (hands, eyes, objects) to convey information in 2s.
- Every shot must either: (a) advance plot, (b) reveal character, or (c) build mood. If a shot does none of these, cut it.
- Keep dialogue lines SHORT — one sentence max per shot. Split longer exchanges across multiple 3s shots."""


NEXT_SHOT_SYSTEM = """You are a professional film screenwriter and storyboard director. The user will provide an existing shot list and overall story information. You need to write the next shot that maintains VISUAL CONTINUITY with the previous shots.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble or postamble).
- ALL text fields must be in ENGLISH. ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere.
- ALL dialogue MUST be in English. Characters MUST speak English regardless of the story's setting or the language of previous context. Write natural, fluent English dialogue.
- Output a single shot object with the following fields:

{
  "continuity_anchor": "CRITICAL: Describes the EXACT spatial or visual element that links this shot to the previous shot. E.g.: 'Match on action: door continues swinging from previous shot', 'Eyeline match: following her gaze from previous shot'",
  "characters_in_shot": ["character name (EXACT names from characters list)"],
  "focal_character": "primary subject in focus, or null if no characters",
  "shot_type": "ECU, CU, MCU, MS, WS, EWS, Two-Shot, OTS, or POV",
  "camera_movement": "Camera movement with speed, in English. E.g.: static, slow push-in, fast pan left",
  "scene_description": "Environment, composition, key props. MUST include spatial reference that connects to the established scene.",
  "character_action": "Use Present Continuous tense. MUST include: (1) micro-expressions; (2) body language; (3) movement quality. No appearance/outfit details.",
  "dialogue": "Speaker-attributed: CharName: spoken words; multiple lines → single newlines; or empty string",
  "mood": "Emotional atmosphere in English",
  "lighting": "Direction + Hard/Soft + Source. Must be consistent with the scene's established lighting.",
  "ambient_sound": "Diegetic, in-world sounds ONLY (footsteps, wind, etc.). Maintain continuity with previous shots.",
  "score_suggestion": "Optional post-production music reference.",
  "generation_prompt": "Single paragraph, balanced ~50% character appearance + ~50% cinematography/staging. Character name with key visual traits in parentheses first, then camera/staging. Quote dialogue with exact words if speech occurs. MUST use character's actual name.",
  "duration_sec": 2.0,
  "cut_rhythm": "SMASH_CUT, STANDARD_CUT, LINGERING_CUT, JUMP_CUT, or MATCH_CUT",
  "negative_prompt_hint": "Elements to avoid: e.g., 'distorted face, extra limbs, text, slow motion, scene change hallucination'"
}

CONTINUITY RULES:
- The new shot MUST connect to the previous shot via continuity_anchor (match on action, eyeline match, graphic match, etc.).
- Maintain consistent screen direction (180-degree rule) within dialogue/interaction sequences.
- Lighting must match the established scene unless a motivated change occurs.
- ambient_sound should note if sounds from the previous shot continue, fade, or change.
- character_action MUST use Present Continuous tense ("is walking", "is staring").
- generation_prompt MUST include a spatial continuity cue and the character's actual name.
- ALL fields must be in English."""


def _normalize_characters_field(raw: Any) -> str:
    """Convert characters from [{name, description}] array format to readable string."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                desc = str(item.get("description", "")).strip()
                if name:
                    parts.append(f"{name}: {desc}" if desc else name)
            elif isinstance(item, str):
                parts.append(item)
        return "; ".join(parts)
    return str(raw)


def _build_shots_from_theme_items(items: list[dict[str, Any]]) -> list[Shot]:
    valid = [x for x in items if isinstance(x, dict)]
    if not valid:
        return []
    cursor = 0.0
    out: list[Shot] = []
    for i, item in enumerate(valid):
        ds = item.get("duration_sec")
        try:
            d = float(ds) if ds is not None else 2.0
        except (TypeError, ValueError):
            d = 2.0
        d = max(0.5, min(10.0, d))
        t0, t1 = cursor, cursor + d
        cursor = t1
        out.append(_shot_from_analysis_dict(i + 1, item, t0, t1))
    return out


def _generate_story_outline(
    theme: str,
    client: OpenAI,
    model: str,
    style_hint: str,
    min_shots: int,
    max_shots: int,
) -> dict[str, Any]:
    """Phase 1: 纯故事创作——生成叙事大纲、角色弧线、情感节拍。"""
    extra = ""
    if style_hint.strip():
        extra = f"\nStyle/genre preference: {style_hint.strip()}"
    user_msg = (
        f"Story theme / creative brief:\n{theme}\n\n"
        f"The final storyboard will have {min_shots}–{max_shots} shots, "
        f"so provide roughly {max(3, min_shots * 2 // 3)}–{max_shots} narrative beats.\n"
        f"Each beat should be filmable in 2-5 seconds of screen time (short-form video pacing).{extra}\n"
        f"Output JSON only. All text fields must be in English. ALL dialogue must be in English — no Chinese or other non-English text."
    )
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": STORY_ARCHITECT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=8192,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


def _generate_shots_from_outline(
    outline: dict[str, Any],
    client: OpenAI,
    model: str,
    style_hint: str,
    min_shots: int,
    max_shots: int,
) -> dict[str, Any]:
    """Phase 2: 基于故事大纲设计分镜——每个 generation_prompt 嵌入叙事上下文。"""
    extra = ""
    if style_hint.strip():
        extra = f"\nStyle/genre preference: {style_hint.strip()}"
    outline_json = json.dumps(outline, ensure_ascii=False, indent=2)
    user_msg = (
        "A COMPLETED STORY OUTLINE is provided below. Do NOT modify the story, "
        "characters, or dialogue. Your task is to faithfully TRANSLATE each "
        "narrative beat into specific, filmable shots.\n\n"
        f"=== STORY OUTLINE ===\n{outline_json}\n=== END OUTLINE ===\n\n"
        f"Design {min_shots}–{max_shots} shots. Each narrative beat should map "
        f"to 1-3 shots. The generation_prompt for each shot MUST clearly convey "
        f"what is happening in the STORY at this moment — the viewer should "
        f"understand the character's intention and emotional state from the "
        f"visual alone.{extra}\n"
        f"Output JSON only. All text fields must be in English. ALL dialogue must be in English — no Chinese or other non-English text."
    )
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SHOT_DESIGNER_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=16384,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


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
    两阶段流程生成分镜文档：
      Phase 1 — Story Architect: 生成完整故事大纲（叙事节拍、角色弧线、对白）
      Phase 2 — Shot Designer:  基于大纲设计分镜，写出叙事驱动的 generation_prompt
    Phase 1 失败时自动回退到单次调用（旧流程）。
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

    # ------ Phase 1: Story Architect ------
    outline: dict[str, Any] | None = None
    try:
        log.info("Phase 1 — Story Architect: generating narrative outline…")
        outline = _generate_story_outline(
            theme, client, use_model, style_hint, min_shots, max_shots,
        )
        beats = outline.get("narrative_beats") or []
        log.info(
            "Phase 1 complete: title=%r, %d characters, %d beats",
            outline.get("title", ""),
            len(outline.get("characters") or []),
            len(beats),
        )
    except Exception as exc:
        log.warning("Phase 1 failed (%s), falling back to single-pass…", exc)
        outline = None

    # ------ Phase 2: Shot Designer (or legacy fallback) ------
    if outline and outline.get("narrative_beats"):
        log.info("Phase 2 — Shot Designer: translating outline into shots…")
        data = _generate_shots_from_outline(
            outline, client, use_model, style_hint, min_shots, max_shots,
        )
    else:
        log.info("Single-pass mode: generating story + shots in one call…")
        data = _single_pass_generate(
            theme, client, use_model, style_hint, min_shots, max_shots,
        )

    # ------ Build document ------
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

    characters_raw = data.get("characters", "")
    characters_str = _normalize_characters_field(characters_raw)

    synopsis = str(data.get("synopsis", ""))
    if not synopsis and outline:
        synopsis = str(outline.get("synopsis", ""))

    scene_geo = ""
    if outline and isinstance(outline.get("setting"), dict):
        setting = outline["setting"]
        scene_geo = str(setting.get("primary_location", ""))

    doc = StoryboardDocument(
        title=str(data.get("title", "")),
        synopsis=synopsis,
        characters=characters_str,
        source_video=format_theme_source_tag(theme),
        shots=shots,
        raw_scene_analyses=[f"[from_theme]{theme[:2000]}"],
        rhythm_profile=str(data.get("rhythm_profile", "")),
        scene_geography=scene_geo,
    )
    if outline:
        logline = str(outline.get("logline", ""))
        if logline:
            doc.logline = logline
        arc = str(outline.get("emotional_arc_summary", ""))
        if arc:
            doc.pacing_flow = arc
    return doc


def _single_pass_generate(
    theme: str,
    client: OpenAI,
    model: str,
    style_hint: str,
    min_shots: int,
    max_shots: int,
) -> dict[str, Any]:
    """旧的单次调用流程，作为 Phase 1 失败时的兜底。"""
    extra = ""
    if style_hint.strip():
        extra += f"\nStyle/genre preference: {style_hint.strip()}"
    user_msg = (
        f"Story theme / creative brief:\n{theme}\n\n"
        f"Generate {min_shots}–{max_shots} shots (shots array length must fall within this range)."
        f"{extra}\nOutput JSON only. All fields English except dialogue: dialogue = spoken words only "
        "(speaker-prefixed); lines in English unless the brief is Chinese / Chinese dialogue requested."
    )
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": THEME_STORYBOARD_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=16384,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


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
        parts = [
            s.get('scene_description', ''),
            s.get('character_action', ''),
            s.get('dialogue', ''),
        ]
        if s.get('continuity_anchor'):
            parts.append(f"anchor: {s['continuity_anchor']}")
        if s.get('focal_character'):
            parts.append(f"focal: {s['focal_character']}")
        context_lines.append(
            f"  Shot {i}: {' | '.join(p for p in parts if p)}"
        )
    context_lines.append(
        "\nWrite the next shot as a single JSON object. Output JSON only. "
        "All fields English except dialogue (spoken words only, no speaker labels; match prior dialogue language)."
    )

    user_msg = "\n".join(context_lines)

    completion = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": NEXT_SHOT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=4096,
    )
    raw = completion.choices[0].message.content or ""
    data = _extract_json_object(raw)

    cursor = 0.0
    for s in existing_shots:
        try:
            cursor += float(s.get("duration", s.get("duration_sec", 2.0)))
        except (TypeError, ValueError):
            cursor += 2.0

    ds = data.get("duration_sec")
    try:
        d = float(ds) if ds is not None else 2.0
    except (TypeError, ValueError):
        d = 2.0
    d = max(0.5, min(10.0, d))

    shot_id = len(existing_shots) + 1
    return _shot_from_analysis_dict(shot_id, data, cursor, cursor + d)


def format_theme_source_tag(theme: str) -> str:
    """写入 source_video 字段便于区分来源（可选）。"""
    t = theme.strip().replace("\n", " ")[:120]
    return f"theme:{t}"


# ---------------------------------------------------------------------------
# IP 模式：专用故事/分镜生成
# ---------------------------------------------------------------------------

IP_STORY_ARCHITECT_SYSTEM = """You are an expert screenwriter for SHORT-FORM viral video content. You create standalone mini-stories for an established IP (character series).

You are given the IP's COMPLETE CREATIVE GENOME: characters, world, story patterns, and visual style. Your job is to create ONE EPISODE — a self-contained 15-30 second mini-story using ONLY these characters in THEIR established world.

CRITICAL CONSTRAINTS:
1. ONLY use characters from the provided character roster. Do NOT invent new characters.
2. Each character's actions MUST match their established behavior_patterns and personality.
3. The story MUST follow the IP's narrative_pattern formula.
4. VISUAL STORYTELLING: The story must be COMPLETELY UNDERSTANDABLE without audio. Every beat must be conveyed through ACTION, EXPRESSION, and PHYSICAL REACTION.
5. FAST PACING: Each beat should be filmable in 2-3 seconds. No slow moments.
6. EXAGGERATED EXPRESSIONS: Characters should have big, readable reactions — this is short-form content for small screens.
7. Scenes should use the IP's recurring_locations.

Output **strict JSON only** (no Markdown code blocks, no preamble). ALL fields in ENGLISH — this is NON-NEGOTIABLE.

LANGUAGE RULE (ABSOLUTE — ZERO EXCEPTIONS):
- Every single text field in the JSON MUST be written in English. No Chinese, Japanese, Korean, or ANY non-English characters allowed anywhere.
- ALL dialogue MUST be in English.

{
  "title": "Episode title reflecting the mini-story",
  "logline": "One sentence: what happens in this episode?",
  "synopsis": "2-3 sentences covering the complete mini-story arc.",
  "setting": {
    "primary_location": "Which recurring location from the IP world (with visual details)",
    "time_of_day": "dawn / morning / midday / afternoon / dusk / night",
    "atmosphere": "Mood of this episode's environment"
  },
  "characters_featured": ["EXACT character names from the IP roster that appear in this episode"],
  "narrative_beats": [
    {
      "beat_id": 1,
      "beat_type": "SETUP | INCITING_INCIDENT | RISING_ACTION | CLIMAX | RESOLUTION",
      "description": "VISUALLY CONCRETE: What does the camera SEE? What physical action happens?",
      "characters_involved": ["exact character names"],
      "emotional_tone": "What the AUDIENCE should feel",
      "key_action": "THE specific physical action (2-3 seconds). Must match character's behavior_patterns.",
      "dialogue": "'Speaker: \"Line.\"' or empty string",
      "visual_focus": "What is the CENTER of the frame?"
    }
  ],
  "emotional_arc_summary": "How emotion builds and resolves in this 15-30 second story."
}

STORY QUALITY FOR SHORT-FORM:
1. START IN MEDIA RES: No slow setup. First beat should hook immediately.
2. CAUSE AND EFFECT: Clear visual cause → exaggerated reaction → consequence.
3. PUNCHLINE ENDING: End with a visual gag, twist, or satisfying payoff.
4. VISUAL VARIETY: Mix close-ups (expressions), medium shots (action), wide shots (reaction).
5. CHARACTER-DRIVEN: The humor/emotion comes FROM the characters' established quirks.

BEAT COUNT: 4-8 beats for a 15-30 second video. Each beat = 2-3 seconds of screen time."""


IP_SHOT_DESIGNER_SYSTEM = """You are a professional storyboard director for SHORT-FORM viral video content. You are given a COMPLETED STORY OUTLINE for an IP episode, plus the IP's character roster and visual style. Your job is to EXECUTE this story as a shot list.

STRICT REQUIREMENTS:
- Output **strict JSON only** (no Markdown code blocks, no preamble).
- ALL text fields must be in ENGLISH — ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere.
- ALL dialogue MUST be in English.
- Do NOT change the story from the outline. Faithfully translate each beat into shots.
- characters_in_shot MUST use the EXACT character names from the IP roster.

JSON SCHEMA:
{
  "title": "string",
  "synopsis": "string",
  "rhythm_profile": "TENSE_RAPID | ACTION_DRIVEN | EMOTIONAL_CRESCENDO",
  "characters": [{"name": "EXACT character name", "description": "visual description from IP"}],
  "shots": [
    {
      "shot_id": 1,
      "narrative_beat_id": 1,
      "continuity_anchor": "Visual link to previous shot",
      "characters_in_shot": ["EXACT character names from IP roster"],
      "focal_character": "primary character or null",
      "shot_type": "ECU / CU / MCU / MS / WS",
      "camera_movement": "static / slow push-in / quick pan / etc.",
      "scene_description": "Environment with IP world details",
      "character_action": "Present Continuous. Include: micro-expression + body language + movement.",
      "dialogue": "'CHARACTER: \"Line.\"' or empty string",
      "mood": "emotional atmosphere",
      "lighting": "Direction + Hard/Soft + Source",
      "ambient_sound": "Diegetic sounds only",
      "score_suggestion": "Optional music reference",
      "generation_prompt": "CRITICAL — see rules below",
      "duration_sec": 2.0,
      "cut_rhythm": "SMASH_CUT / STANDARD_CUT / MATCH_CUT / JUMP_CUT",
      "negative_prompt_hint": "Elements to avoid"
    }
  ]
}

GENERATION_PROMPT RULES FOR IP MODE:
Each generation_prompt must:
1. Use the character's EXACT English name (name_en from IP roster) — this will later be replaced with reference image tags.
2. Include the character's KEY VISUAL TRAITS from their visual_description for reinforcement.
3. Append the IP's style keywords at the end (provided below).
4. Follow the formula: [framing + camera] + [spatial anchor] + [CHARACTER_NAME + key visual traits] + [motivated action with exaggerated expression] + [lighting] + [IP style keywords].

EXAMPLE:
"Medium shot, static camera. In a bright modern kitchen with a large window. CHUBBY (a plump orange tabby cat in blue hoodie) is reaching with both paws toward a fish-shaped cake on the counter, his big green eyes wide with desire, tongue slightly out. Warm natural light from the window. 3D cartoon style, chibi, vibrant colors, soft lighting, Pixar style."

TIMING (SHORT-FORM — ULTRA FAST):
- DEFAULT 2s for all non-dialogue shots.
- DIALOGUE shots get 3s max.
- NEVER exceed 4s.
- Prefer SMASH_CUT. Every shot must advance the story.

IP STYLE KEYWORDS TO APPEND:
{style_keywords}"""


def _build_ip_character_roster(profile: IPProfile) -> str:
    """格式化 IP 角色花名册供 LLM prompt 使用。"""
    lines: list[str] = []
    for c in profile.characters:
        lines.append(
            f"- {c.name} ({c.name_en}): role={c.role}, "
            f"visual={c.visual_description[:200]}, "
            f"personality={c.personality}, "
            f"behaviors={c.behavior_patterns}"
        )
    return "\n".join(lines)


def _generate_ip_story_outline(
    profile: IPProfile,
    theme_hint: str,
    client: OpenAI,
    model: str,
    min_shots: int,
    max_shots: int,
) -> dict[str, Any]:
    """Phase 1 (IP mode): 基于 IP 世界观生成单集故事大纲。"""
    char_roster = _build_ip_character_roster(profile)

    user_msg = (
        f"=== IP CREATIVE GENOME ===\n"
        f"IP Name: {profile.name} ({profile.name_en})\n"
        f"Tagline: {profile.tagline}\n\n"
        f"VISUAL STYLE: {profile.visual_dna.style_keywords_en}\n"
        f"Color Tone: {profile.visual_dna.color_tone}\n\n"
        f"STORY DNA:\n"
        f"  Genre: {profile.story_dna.genre}\n"
        f"  Narrative Pattern: {profile.story_dna.narrative_pattern}\n"
        f"  Emotional Tone: {profile.story_dna.emotional_tone}\n"
        f"  Pacing: {profile.story_dna.pacing}\n"
        f"  Episode Structure: {profile.story_dna.episode_structure}\n"
        f"  Plot Hook Ideas: {json.dumps(profile.story_dna.typical_plot_hooks, ensure_ascii=False)}\n\n"
        f"WORLD:\n"
        f"  Primary Setting: {profile.world_dna.primary_setting}\n"
        f"  Recurring Locations: {profile.world_dna.recurring_locations}\n"
        f"  World Rules: {profile.world_dna.world_rules}\n\n"
        f"CHARACTER ROSTER:\n{char_roster}\n\n"
        f"=== END IP GENOME ===\n\n"
    )

    if theme_hint.strip():
        user_msg += f"Episode theme hint: {theme_hint.strip()}\n\n"
    else:
        user_msg += "Create an episode using one of the typical_plot_hooks as inspiration.\n\n"

    user_msg += (
        f"Design a mini-story with {max(3, min_shots * 2 // 3)}-{max_shots} narrative beats.\n"
        f"Each beat = 2-3 seconds of screen time.\n"
        f"Output JSON only. All fields in English."
    )

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": IP_STORY_ARCHITECT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=8192,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


def _generate_ip_shots_from_outline(
    outline: dict[str, Any],
    profile: IPProfile,
    client: OpenAI,
    model: str,
    min_shots: int,
    max_shots: int,
) -> dict[str, Any]:
    """Phase 2 (IP mode): 基于大纲和 IP 角色生成分镜。"""
    style_kw = profile.visual_dna.style_keywords_en or profile.visual_dna.style_keywords
    system = IP_SHOT_DESIGNER_SYSTEM.replace("{style_keywords}", style_kw)

    char_info = []
    for c in profile.characters:
        char_info.append({
            "name": c.name,
            "name_en": c.name_en,
            "visual_description": c.visual_description,
        })

    outline_json = json.dumps(outline, ensure_ascii=False, indent=2)
    char_json = json.dumps(char_info, ensure_ascii=False, indent=2)

    user_msg = (
        f"=== STORY OUTLINE ===\n{outline_json}\n=== END OUTLINE ===\n\n"
        f"=== IP CHARACTER VISUAL REFERENCE ===\n{char_json}\n=== END CHARACTERS ===\n\n"
        f"Design {min_shots}-{max_shots} shots. Use EXACT character names.\n"
        f"The generation_prompt for each shot MUST include the character's name_en "
        f"and key visual traits, followed by the IP style keywords.\n"
        f"Output JSON only. All fields in English."
    )

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=16384,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


def generate_storyboard_from_ip(
    ip_profile: IPProfile,
    settings: Settings,
    *,
    theme_hint: str = "",
    min_shots: int = 8,
    max_shots: int = 16,
    model: str | None = None,
) -> StoryboardDocument:
    """IP 模式：两阶段流程生成分镜文档。

    Phase 1 — IP Story Architect: 基于 IP 世界观生成单集故事
    Phase 2 — IP Shot Designer: 分镜设计，generation_prompt 中引用角色名
    """
    min_shots = max(3, min(40, int(min_shots)))
    max_shots = max(min_shots, min(60, int(max_shots)))

    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    use_model = (model or settings.theme_story_model or settings.vision_model).strip()
    if not use_model:
        use_model = settings.vision_model

    # Phase 1
    log.info("IP Phase 1 — Story Architect for IP %r…", ip_profile.name)
    outline = _generate_ip_story_outline(
        ip_profile, theme_hint, client, use_model, min_shots, max_shots,
    )
    beats = outline.get("narrative_beats") or []
    log.info(
        "IP Phase 1 complete: title=%r, %d beats",
        outline.get("title", ""),
        len(beats),
    )

    # Phase 2
    log.info("IP Phase 2 — Shot Designer…")
    data = _generate_ip_shots_from_outline(
        outline, ip_profile, client, use_model, min_shots, max_shots,
    )

    # Build document
    shots_data = data.get("shots") or []
    if not isinstance(shots_data, list):
        raise ValueError("模型返回的 shots 不是数组")
    shots = _build_shots_from_theme_items(shots_data)
    if len(shots) < min_shots:
        raise ValueError(
            f"模型仅返回 {len(shots)} 个镜头，少于要求下限 {min_shots}"
        )
    if len(shots) > max_shots:
        shots = shots[:max_shots]

    characters_raw = data.get("characters", "")
    characters_str = _normalize_characters_field(characters_raw)

    synopsis = str(data.get("synopsis", ""))
    if not synopsis and outline:
        synopsis = str(outline.get("synopsis", ""))

    scene_geo = ""
    if outline and isinstance(outline.get("setting"), dict):
        scene_geo = str(outline["setting"].get("primary_location", ""))

    source_tag = f"ip:{ip_profile.id}"
    if theme_hint:
        source_tag += f"|{format_theme_source_tag(theme_hint)}"

    doc = StoryboardDocument(
        title=str(data.get("title", "")),
        synopsis=synopsis,
        characters=characters_str,
        source_video=source_tag,
        shots=shots,
        raw_scene_analyses=[f"[from_ip]{ip_profile.name}"],
        rhythm_profile=str(data.get("rhythm_profile", "")),
        scene_geography=scene_geo,
    )
    if outline:
        logline = str(outline.get("logline", ""))
        if logline:
            doc.logline = logline
        arc = str(outline.get("emotional_arc_summary", ""))
        if arc:
            doc.pacing_flow = arc
    return doc
