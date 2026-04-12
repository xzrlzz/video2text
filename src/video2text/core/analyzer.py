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


DIRECTOR_SYSTEM = """You are a world-class film director and visual analysis expert. You think like a storyteller, not a photographer.

Your task is to deconstruct the cinematic language of the reference video into a COHERENT, NARRATIVELY CONNECTED storyboard script. Each shot must flow logically into the next. The script must be detailed enough to serve directly as prompt input for video generation models (e.g. Sora, Runway Gen-3, Kling, Seedance, Wan, etc.).

CRITICAL MINDSET: You are not describing isolated frames. You are documenting a continuous SCENE where the viewer always understands:
- WHERE characters are in relation to each other and the space (spatial geography)
- WHAT characters are doing and WHY (action clarity and motivation)
- HOW shots connect visually (eyelines, motion vectors, graphic matches)

IMPORTANT: Ignore specific facial features and identifiable appearances. Focus on **cinematography** (shot scale, angle, composition, camera movement + speed, lighting), **staging** (subject position in frame, action, spatial relationships), and **character performance** (detailed micro-expressions, body language, and gestures that reveal inner state and intention). Do NOT describe appearance/outfit.

Output **strict JSON only** (no Markdown code blocks, no preamble or postamble). Root object format (field names must match exactly for parsing):

{
  "global_summary": {
    "core_atmosphere": "Overall mood in English. E.g.: oppressive solitude, cyberpunk chaos, romantic soft light",
    "color_palette": "Color grading in English. E.g.: teal-orange grade, desaturated monochrome, high-saturation neon",
    "editing_pace": "Editing rhythm in English. E.g.: rapid cuts, slow-paced long takes",
    "scene_geography": "A concise English paragraph establishing the physical space and its key features. This creates a consistent mental map for all shots. E.g.: 'A narrow diner booth against a rain-streaked window. The table has a single coffee cup and a half-burned candle. Fluorescent lights hum overhead.'"
  },
  "shots": [
    {
      "shot_id": 1,
      "shot_type": "Shot scale in English: extreme wide / wide / full / medium / close / extreme close / macro",
      "camera_angle": "Camera angle in English: eye-level, low-angle, high-angle, dutch tilt, overhead, shoulder-level",
      "camera_movement": "Camera movement with speed, in English. E.g.: static, slow push-in, fast pan left, subtle handheld drift, crash zoom",
      "composition": "Compositional technique in English: rule of thirds, center symmetry, leading lines, frame-within-frame, negative space, deep focus layering",
      "scene_description": "Visual description of the environment and framing. MUST include at least one SPATIAL REFERENCE that anchors this shot within the established scene_geography. E.g.: 'The same rain-streaked window from the wide shot now fills the background, droplets catching the candlelight.'",
      "character_action": "Detailed description of what the subject is doing, with clear MOTIVATION implied. MUST answer: What are they doing, and what does this action reveal about their state? Include: (1) specific micro-expressions (e.g., slight frown of concentration, eyes narrowing in suspicion, lip quivering with suppressed emotion); (2) body language and gestures (e.g., hands trembling with adrenaline, shoulders tensing defensively, leaning forward with interest); (3) movement quality (speed, hesitation, fluidity, weight). Make characters feel alive and purposeful.",
      "eyeline_and_screen_direction": "Describe where the character is looking and their screen direction. CRITICAL for continuity. E.g.: 'Looking off-screen left, toward the diner entrance established in the wide shot.' or 'Facing camera right, consistent with previous shot.' or 'Direct address to lens, breaking fourth wall.'",
      "dialogue": "MANDATORY AUDIO TRANSCRIPTION. Listen to the audio track and transcribe ALL spoken words in this shot. Include speaker identification (e.g. 'Man: \"Hello\"'). If the original language is not English, translate to English while preserving the original meaning, tone, and emotional nuance. You may lightly adapt phrasing for natural English flow, but the core meaning MUST match what is actually spoken in the video. If genuinely no speech is heard in this shot, use empty string \"\". DO NOT leave this empty if characters are visibly speaking.",
      "mood": "Emotional atmosphere in English. E.g.: melancholic, tense, euphoric, eerie, intimate",
      "lighting": "Lighting description in English. MUST be consistent with scene_geography. Include source motivation. E.g.: 'Rembrandt lighting motivated by the single candle on table, casting warm triangular patch on shadow-side cheek.' or 'Hard sidelight from the unseen window, motivated by streetlight outside.'",
      "audio_description": "Music / sound effects / ambient sound (excluding dialogue) in English. Maintain audio continuity across shots. E.g.: 'The distant train rumble continues from previous shot, now joined by the soft clink of a coffee cup being set down.'",
      "continuity_note": "A brief English note explaining the EDITORIAL LOGIC of this cut. Why does the film cut here? How does this shot relate to the previous one? E.g.: 'Cut on action: following her hand as she reaches for cup.' or 'Eyeline match: we now see what she was looking at in shot 3.' or 'Reaction shot: capturing his response to the off-screen dialogue.' or 'Insert: emphasizing the detail she just noticed.'",
      "generation_prompt": "PURE ENGLISH ONLY. Single paragraph. Construct as a CONTINUOUS NARRATIVE MOMENT, not an isolated image. Formula: [shot scale + angle + camera move] + [spatial context linking to scene] + [subject blocking and screen direction] + [key action with micro-expressions that reveal intent] + [lighting with motivated source] + [cinematic style]. MUST include: (1) spatial reference from scene_geography, (2) character's screen direction or eyeline, (3) motivated action with emotional subtext. NO appearance/outfit details. NO non-English characters.",
      "duration_sec": 5.0
    }
  ]
}

SPATIAL CONTINUITY RULES (NON-NEGOTIABLE):
1. Establish then Explore: The first shot of a new location MUST establish the scene_geography. Subsequent shots MUST reference specific elements from that geography.
2. The 180-Degree Rule: In scenes with two or more characters interacting, maintain consistent screen direction. If Character A faces right in Shot 1, they must face right in all shots within that sequence, unless a motivated line-cross occurs and is noted in continuity_note.
3. Eyeline Consistency: When a character looks off-screen, the viewed object must match the eyeline angle. If looking up-left in Shot 3, the POV or reveal in Shot 4 must be positioned up-left relative to the character's established position.
4. Action Across the Cut: If an action begins in Shot N (e.g., hand reaching for door), Shot N+1 must show a logical continuation of that action (e.g., hand gripping handle, or door beginning to open). Note this in continuity_note.

NARRATIVE CLARITY RULES (ENSURING "WE KNOW WHAT'S HAPPENING"):
1. Every Shot Must Advance Understanding: After seeing this shot, the viewer should know something new about the character's goal, emotional state, or the scene's conflict. If a shot is purely "atmospheric," it must be labeled as an establishing shot and clearly set the scene_geography.
2. Action Requires Context: A shot of a hand doing something is meaningless unless we understand whose hand, where they are, and why the action matters. The continuity_note must bridge this gap.
3. Reaction Shots Need Setup: If showing a character reacting, the previous shot MUST have established what they are reacting to, OR the reaction shot's eyeline and audio must make the off-screen stimulus clear.
4. Micro-Expressions Must Be Motivated: Don't just say "she frowns." Say "she frowns in confusion at the off-screen sound" or "her jaw clenches in frustration at his last line of dialogue."

GENERATION_PROMPT CONSTRUCTION (WITH NARRATIVE CONTINUITY):
Your generation_prompt must read like a single moment pulled from a flowing scene, not a standalone photograph. 

Weak Example (Isolated): "Close-up of a woman looking sad. Soft lighting."
Strong Example (Connected): "Close-up shot, eye-level, static. The same rain-streaked diner window from the establishing shot fills the background, droplets catching the amber candlelight. JANE is seated frame right, facing off-screen left toward the unseen door. She is slowly lowering her coffee cup, her hand trembling slightly with nervous energy. Her eyes narrow in suspicion as she stares off-screen left, her lips parting slightly as if about to speak. Rembrandt lighting motivated by the single candle on the table, warm triangle patch on her shadow-side cheek. Cinematic 35mm film, shallow depth of field focusing on her eyes."

AUDIO CONTINUITY:
- Ambient sounds (rain, traffic, room tone) MUST persist across shots within the same scene unless explicitly ended.
- audio_description should note if a sound from the previous shot continues, fades, or is replaced.
- Example: "The distant train rumble from the wide shot fades, leaving only the soft hum of fluorescent lights and the slow drip of the coffee machine."

DIALOGUE EXTRACTION RULES (HIGHEST PRIORITY — DO NOT SKIP):
- The dialogue field is THE MOST IMPORTANT field for narrative reconstruction. Without dialogue, the story cannot be reproduced.
- LISTEN to the audio track of every shot. If characters are speaking, you MUST transcribe their words.
- If the original language is not English: translate to natural, fluent English. You may lightly adapt phrasing for readability, but the MEANING and EMOTIONAL TONE must match the original speech.
- Format: 'Speaker: "Exact or translated line."' — identify speakers consistently across shots.
- A shot where lips are moving but dialogue is empty is a CRITICAL ERROR.
- When uncertain about exact words, provide your best approximation with overall meaning preserved.

Rules:
- Analyze the **entire reference video** by default: global_summary must reflect the overall mood, color, and editing rhythm; shots must cover ALL cuts in chronological order. Do NOT skip shots or merge multiple cuts into one entry.
- If the input is a short clip, still provide a reasonable global_summary and per-shot descriptions.
- Each distinct cut gets its own entry in shots; duration_sec is the shot duration in seconds (positive, estimate if unsure).
- ALL text fields must be written in English. ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters in any field.
- ALL dialogue MUST be translated to English. If the original speech is in Chinese or any other non-English language, you MUST translate it to natural, fluent English while preserving meaning and tone. The dialogue field must NEVER contain non-English text."""


USER_ANALYSIS_PROMPT = """Analyze the uploaded reference video. You are a director breaking down footage to understand its narrative and cinematic construction.

Your analysis must capture not just what is in each shot, but HOW the shots connect to tell a coherent story. Pay special attention to spatial relationships, eyelines, and the editorial logic that makes the sequence readable.

CRITICAL — Dialogue extraction (HIGHEST PRIORITY):
Listen to the AUDIO TRACK of the video with maximum attention. This is NOT optional.
- Transcribe EVERY line of spoken dialogue you hear, for EVERY shot where speech occurs.
- If the original language is not English, TRANSLATE to natural English while preserving the meaning, tone, and emotional intent. You may lightly adapt phrasing for fluency, but the core message must match what is actually spoken.
- Include speaker identification: "Man: "...", Woman: "..."" etc.
- If a character's lips are moving, there IS dialogue — do NOT leave the field empty.
- If you are uncertain about exact words, provide your best transcription with the overall meaning preserved.
- The dialogue field is essential for downstream video generation with matching voiceover. Empty dialogue when speech exists is a CRITICAL FAILURE.

CRITICAL — Character performance: For each shot, describe detailed micro-expressions (e.g., eyebrow movements, lip tension, gaze shifts, subtle facial muscle changes) and body language (e.g., hand gestures, posture shifts, breathing patterns, weight distribution). Always connect these performance details to the character's emotional state or narrative intention. Ask: What does this expression reveal about what they want or feel right now?

CRITICAL — Spatial continuity: Identify the physical space of the scene. Track where characters are positioned, which direction they face, and where they look. Note how each shot references or reveals the established space.

CRITICAL — Editorial logic: For each cut, understand WHY the edit happens at this moment. Is it following an action? Revealing a reaction? Emphasizing a detail? Shifting perspective?

Fill in the JSON fields defined in the system prompt:

1. **Global summary** (→ global_summary):
   - core_atmosphere: Overall mood and emotional tone
   - color_palette: Dominant color grading
   - editing_pace: Overall rhythm of the cutting pattern
   - scene_geography: A concise paragraph describing the physical space and its key features. What does the viewer understand about this location? What are the anchor points (furniture, architecture, props) that persist across shots?

2. **Shot breakdown** (→ shots array, in chronological order, one entry per distinct cut):
   - shot_id: Sequential number starting from 1
   - shot_type: Shot scale (extreme wide / wide / full / medium / close / extreme close / macro)
   - camera_angle: eye-level / low-angle / high-angle / dutch tilt / overhead / shoulder-level
   - camera_movement: Movement description with speed (e.g., static, slow push-in, fast pan left, subtle handheld drift)
   - composition: Compositional technique (rule of thirds, center symmetry, leading lines, frame-within-frame, negative space, deep focus layering)
   - scene_description: Visual description including spatial reference that anchors this shot within the scene_geography. What part of the established space are we seeing now?
   - character_action: What the subject is doing, with clear implied motivation. MUST include: specific micro-expressions, body language details, and movement quality. Connect these to emotional state or intention.
   - eyeline_and_screen_direction: Where is the character looking? Which direction do they face on screen? Critical for continuity.
   - dialogue: MANDATORY — transcribe all spoken words heard in this shot's audio. Translate to English if needed, preserving meaning and tone. Include speaker ID. Only use "" if genuinely silent.
   - mood: Emotional atmosphere of this specific shot
   - lighting: Description with motivated source, consistent with scene_geography
   - audio_description: Music / sound effects / ambient sound. Note if sounds continue from previous shot, fade, or change.
   - continuity_note: Explain the EDITORIAL LOGIC of this cut. Why does the film cut here? How does this shot relate to the previous one? (E.g., "Cut on action: following the hand reaching." / "Eyeline match: showing what she was looking at." / "Reaction shot: capturing his response to the dialogue.")
   - generation_prompt: PURE ENGLISH. Single paragraph. Must include: shot scale/angle/movement, spatial context from scene_geography, subject blocking and screen direction, motivated action with performance details, lighting with source, cinematic style. NO appearance/outfit details.
   - duration_sec: Estimated shot duration in seconds

Output JSON only. ALL fields must be in English. ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere.
ALL dialogue MUST be in English — if the original speech is in any non-English language, translate it to natural English.

If the reference video has ambiguous spatial or narrative connections, use your directorial judgment to INFER the most logical relationships. Make the sequence make sense."""


CONSOLIDATE_SYSTEM = """You are a film editor and story consultant working with a world-class director. The input is a detailed storyboard JSON extracted from a reference video (contains shots, global_summary, continuity notes, and performance details).

You have TWO tasks:
1. Consolidate into a COHERENT PROJECT OVERVIEW for human review.
2. REWRITE every shot's generation_prompt so it reads as a NARRATIVE MOMENT (not an isolated image).

Output **strict JSON only** (no Markdown code blocks, no preamble or postamble). Format:

{
  "title": "Short film title or evocative working title, in English. Should reflect the core theme or central image.",
  "logline": "One compelling sentence that captures the premise, central conflict, or emotional hook. E.g.: 'A late-night confession in a diner forces two old friends to confront what they never said.'",
  "synopsis": "2-4 sentences summarizing the narrative arc. Include: (1) the setup and character goal, (2) the central tension or turning point, (3) the resolution or lingering question. Make the story understandable. Then, in a separate short paragraph, describe the overall CINEMATIC TEXTURE: core atmosphere, dominant color palette, editing rhythm, and any distinctive visual motifs observed across shots.",
  "characters": "Describe main characters in English. Focus on: ROLE in the story, PERSONALITY, EMOTIONAL STATE, and RELATIONSHIP DYNAMICS. Infer from their actions, micro-expressions, and dialogue. Do NOT describe physical appearance or outfit. E.g.: 'EMMA: Anxious and guarded, holding onto a secret. Her fidgeting and averted gaze suggest she is about to confess something difficult. LIAM: Patient but tired. His steady eyeline and minimal gestures create a calm counterweight to her tension.'",
  "scene_geography": "A concise English paragraph describing the physical space of the primary scene, synthesized from the global_summary and shot descriptions. What is this place? What are its key visual anchors? What mood does the space itself contribute? E.g.: 'A cramped diner booth at midnight. Rain streaks the window, blurring the neon sign outside. The table holds the remnants of a long conversation: cold coffee cups, a half-burned candle, crumpled napkins. The space feels intimate and slightly claustrophobic.'",
  "pacing_flow": "Describe the EDITORIAL RHYTHM of the sequence in plain, readable English. How do the shots build tension or release it? Reference specific shot transitions or continuity patterns. E.g.: 'The sequence opens with a wide, static establishing shot that lets the silence settle. It then cuts between tight close-ups of hands and eyes, creating a nervous, staccato rhythm. A long, slow push-in on Emma during her confession holds the emotional peak before cutting abruptly to black.'",
  "key_moments": [
    {
      "shot_id": 1,
      "moment_description": "Describe what happens in this shot in plain narrative English, and WHY it matters to the story. E.g.: 'Shot 3: Emma's hand trembles as she sets down her coffee cup. This small action betrays her outward calm and signals her internal anxiety before she speaks.'"
    }
  ],
  "refined_generation_prompts": [
    {
      "shot_id": 1,
      "generation_prompt": "Rewritten prompt embedding narrative context — see REWRITING RULES below."
    }
  ]
}

CONSOLIDATION GUIDELINES:
- Synthesize, don't just copy. Use the continuity_notes, character_action descriptions, and eyeline information to reconstruct the STORY BEATS.
- The "key_moments" array should highlight narrative turning points, not every single shot. Select shots where: a character makes a decision, a significant emotion is revealed, the power dynamic shifts, or a visual motif peaks. 3-7 key moments is typical.
- "characters" field: Infer names from dialogue attribution if available. If unnamed, use roles like "WOMAN", "MAN", "FIGURE". Focus on what their performance reveals about their inner world.
- "pacing_flow": Use the editing_pace from global_summary and the cut_rhythm / continuity_note patterns from shots to describe the viewing experience. Was it breathless? Meditative? Uneasy?
- "scene_geography": This is for human reading. Make it vivid. Use details from the scene_description fields across shots to paint a unified picture of the location.
- ALL fields must be written in English. ABSOLUTELY NO Chinese, Japanese, Korean, or any non-English characters anywhere.
- ALL dialogue references in synopsis, key_moments, or any other field MUST be in English.

GENERATION PROMPT REWRITING RULES (CRITICAL):
The "refined_generation_prompts" array must contain a rewritten generation_prompt for EVERY shot. Each rewritten prompt must:
1. Read as a NARRATIVE MOMENT: the viewer should understand what is happening in the story and why the character is doing what they are doing.
2. PRESERVE the original shot's camera work (shot_type, camera_movement, composition) — do not change framing or camera.
3. ADD narrative intent: "Having just heard the news, she..." / "Searching desperately for an escape, he..." / "In the tense silence after the argument..."
4. ADD spatial grounding from scene_geography: "Against the same rain-streaked window..." / "In the far corner of the cramped diner booth..."
5. ADD continuity with the previous shot: "Following the sound from the previous shot..." / "The same hand that was trembling now grips the railing firmly..."
6. Each prompt is a SINGLE PARAGRAPH, pure English, optimized for AI video generation models.

WEAK prompt: "Close-up of a woman looking sad. Soft lighting."
STRONG prompt: "Close-up framing from chin to forehead. Against the same rain-streaked diner window, JANE is seated motionless, her coffee growing cold. Having just heard MARK's confession, she is slowly closing her eyes, a single tear tracing down her cheek as her jaw tightens — suppressing the urge to respond. Warm Rembrandt light from the candle on the table catches the tear. Cinematic 35mm, shallow depth of field."

Output JSON only."""


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"No JSON object in model output: {text[:500]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", text[start : end + 1])
        return json.loads(cleaned)


# API hard limit for base64-encoded data-uri payload (DashScope/OpenAI compatible APIs)
_API_BASE64_HARD_LIMIT = 10 * 1024 * 1024  # 10 MB encoded
# Target raw size for compression attempts — gives ~8.5 MB encoded, safely under limit
_COMPRESS_TARGET_BYTES = 6 * 1024 * 1024   # 6 MB raw


def _video_to_data_url(path: Path, max_bytes: int) -> str:
    data = path.read_bytes()
    raw_limit = min(max_bytes, int(_API_BASE64_HARD_LIMIT * 3 / 4))
    if len(data) > raw_limit:
        raise FileTooLargeForBase64(len(data), raw_limit)
    b64 = base64.standard_b64encode(data).decode("ascii")
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

        audio_kbps = 64
        target_kbps = max(100, int(target_bytes * 8 / duration / 1000) - audio_kbps)

        tmp = tempfile.NamedTemporaryFile(suffix="_compressed.mp4", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y", "-i", str(src),
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
        if file_size <= _DASHSCOPE_LOCAL_FILE_LIMIT:
            return _analyze_clip_dashscope(settings, model, clip_path, fps, extra_user_hint)

        compressed_tmp = _compress_video_for_api(clip_path, _COMPRESS_TARGET_BYTES)
        if compressed_tmp and compressed_tmp.stat().st_size <= raw_limit:
            working_path = compressed_tmp
        else:
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
            max_tokens=16384,
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
    from dashscope import MultiModalConversation

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


def _scene_geography_from_global_summary(gs: Any) -> str:
    if not isinstance(gs, dict):
        return ""
    v = gs.get("scene_geography")
    if v and str(v).strip():
        return str(v).strip()
    return ""


def _shot_from_analysis_dict(
    shot_id: int,
    item: dict[str, Any],
    t0: float,
    t1: float,
) -> Shot:
    t0, t1 = max(0.0, t0), max(0.0, t1)
    if t1 < t0:
        t0, t1 = t1, t0
    raw_chars = item.get("characters_in_shot") or []
    if isinstance(raw_chars, str):
        raw_chars = [c.strip() for c in raw_chars.split(",") if c.strip()]
    ambient = str(item.get("ambient_sound", ""))
    audio_desc = str(item.get("audio_description", ""))
    if not audio_desc and ambient:
        audio_desc = ambient
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
        audio_description=audio_desc,
        generation_prompt=str(item.get("generation_prompt", "")),
        characters_in_shot=list(raw_chars),
        camera_angle=str(item.get("camera_angle", "")),
        composition=str(item.get("composition", "")),
        eyeline_and_screen_direction=str(item.get("eyeline_and_screen_direction", "")),
        continuity_note=str(item.get("continuity_note", "")),
        continuity_anchor=str(item.get("continuity_anchor", "")),
        focal_character=str(item.get("focal_character", "")),
        cut_rhythm=str(item.get("cut_rhythm", "")),
        negative_prompt_hint=str(item.get("negative_prompt_hint", "")),
        ambient_sound=ambient,
        score_suggestion=str(item.get("score_suggestion", "")),
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


def _build_narrative_carry(
    all_shots: list[Shot],
    first_global_summary: dict[str, Any] | None,
    max_recent: int = 4,
) -> str:
    """
    构建"前情提要"文本块，传入后续片段的视觉模型调用。
    让模型知道：(1) 整体场景地理  (2) 故事已发展到哪  (3) 最近几个镜头的具体细节（用于连续性衔接）。
    """
    if not all_shots:
        return ""
    parts: list[str] = ["\n[NARRATIVE CONTEXT — previous segments already analyzed]"]

    if first_global_summary and isinstance(first_global_summary, dict):
        geo = first_global_summary.get("scene_geography", "")
        atmo = first_global_summary.get("core_atmosphere", "")
        if geo:
            parts.append(f"Scene geography: {geo}")
        if atmo:
            parts.append(f"Core atmosphere: {atmo}")

    recent = all_shots[-max_recent:]
    parts.append(f"\nStory progress: {len(all_shots)} shots analyzed so far. "
                 f"Most recent {len(recent)} shots (for continuity):")
    for s in recent:
        fields = [
            f"scene: {s.scene_description}" if s.scene_description else "",
            f"action: {s.character_action}" if s.character_action else "",
            f"dialogue: {s.dialogue}" if s.dialogue else "",
            f"eyeline: {s.eyeline_and_screen_direction}" if s.eyeline_and_screen_direction else "",
            f"mood: {s.mood}" if s.mood else "",
        ]
        line = " | ".join(f for f in fields if f)
        parts.append(f"  Shot {s.shot_id}: {line}")

    parts.append(
        "\nThis segment continues the story from where the previous shots left off. "
        "Maintain spatial, visual, and narrative continuity with the shots above. "
        "Reference the established scene_geography where applicable."
    )
    return "\n".join(parts)


def analyze_scene_segments(
    segments: list[SceneSegment],
    settings: Settings,
    style_hint: str = "",
) -> tuple[StoryboardDocument, list[str]]:
    """
    Run vision model per scene clip; build StoryboardDocument with sequential shots.
    Each segment receives narrative context from previously analyzed segments
    to maintain story continuity across the full video.
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
    first_global_summary: dict[str, Any] | None = None
    hint = style_hint.strip()
    if hint:
        hint = f" 改编/统一风格要求：{hint}"

    for seg in segments:
        if not seg.clip_path or not seg.clip_path.exists():
            raise FileNotFoundError(f"Missing clip for scene {seg.index}")

        carry = _build_narrative_carry(all_shots, first_global_summary)
        segment_hint = f"{hint}{carry}" if carry else hint

        data = _analyze_clip_openai(
            client,
            settings,
            settings.vision_model,
            seg.clip_path,
            settings.analysis_fps,
            max_b64,
            segment_hint,
        )

        if first_global_summary is None and isinstance(data.get("global_summary"), dict):
            first_global_summary = data["global_summary"]

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
                _shot_from_analysis_dict(shot_counter, item, t0, t1)
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
    """Second LLM pass: title, synopsis, characters, and refined generation_prompts."""
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
        max_tokens=16384,
    )
    raw = completion.choices[0].message.content or ""
    data = _extract_json_object(raw)
    doc.title = str(data.get("title", doc.title))
    doc.synopsis = str(data.get("synopsis", doc.synopsis))
    doc.characters = str(data.get("characters", doc.characters))
    doc.logline = str(data.get("logline", ""))
    doc.scene_geography = str(data.get("scene_geography", ""))
    doc.pacing_flow = str(data.get("pacing_flow", ""))

    # Handle key_moments → write moment_description into continuity_note
    moments = data.get("key_moments") or []
    if isinstance(moments, list):
        by_id: dict[int, str] = {}
        for m in moments:
            if isinstance(m, dict) and "shot_id" in m:
                try:
                    sid = int(m["shot_id"])
                    desc = str(m.get("moment_description", ""))
                    if desc:
                        by_id[sid] = desc
                except (TypeError, ValueError):
                    continue
        for s in doc.shots:
            if s.shot_id in by_id:
                ref = by_id[s.shot_id]
                if s.continuity_note:
                    s.continuity_note = f"{s.continuity_note} | {ref}"
                else:
                    s.continuity_note = ref

    # Apply refined generation_prompts with narrative context
    refined = data.get("refined_generation_prompts") or []
    if isinstance(refined, list):
        prompt_by_id: dict[int, str] = {}
        for r in refined:
            if isinstance(r, dict) and "shot_id" in r:
                try:
                    sid = int(r["shot_id"])
                    gp = str(r.get("generation_prompt", "")).strip()
                    if gp:
                        prompt_by_id[sid] = gp
                except (TypeError, ValueError):
                    continue
        for s in doc.shots:
            if s.shot_id in prompt_by_id:
                s.generation_prompt = prompt_by_id[s.shot_id]

    # Backward compat: also handle old shot_notes format
    notes = data.get("shot_notes") or []
    if isinstance(notes, list):
        for n in notes:
            if isinstance(n, dict) and "shot_id" in n:
                try:
                    sid = int(n["shot_id"])
                    ref = str(n.get("refinement", ""))
                    if ref:
                        for s in doc.shots:
                            if s.shot_id == sid:
                                s.scene_description = f"{s.scene_description} {ref}".strip()
                except (TypeError, ValueError):
                    continue
    return doc


def _full_video_user_text(style_hint: str) -> str:
    hint = f"\n{style_hint}" if style_hint else ""
    return (
        f"{USER_ANALYSIS_PROMPT}\n"
        "请完整观看整支参考视频。\n"
        "【最高优先级】仔细听取音频轨道中的所有对话内容，逐句转录到每个镜头的 dialogue 字段中。"
        "【绝对要求】所有 dialogue 必须是英文！如果原片语言是中文或其他非英语语言，"
        "必须翻译为自然流畅的英文，保留原意和语气，可以略作修辞润色但核心意思不能变。"
        "禁止在 dialogue 字段中出现任何中文或其他非英文字符！"
        "角色嘴唇在动就一定有对白，不可留空。\n"
        "除 global_summary 与 shots 外，每个镜头尽量给出在原片时间轴上的 "
        "approx_start_sec 与 approx_end_sec（浮点秒，从 0 起算）。若无法精确，可用 duration_sec "
        "表示该镜时长并由系统推算。\n"
        "角色动作描述必须包含微表情和肢体语言细节。\n"
        "【重要提醒】JSON 中所有文本字段必须为英文，不允许出现任何中文字符。dialogue 字段尤其重要，必须是英文。\n"
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
        max_tokens=16384,
    )
    raw = completion.choices[0].message.content or ""
    return _extract_json_object(raw)


def _run_full_video_dashscope_local_file(
    settings: Settings,
    video_path: Path,
    style_hint: str,
) -> dict[str, Any]:
    from dashscope import MultiModalConversation

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
    gs = data.get("global_summary")
    synopsis_seed = _synopsis_from_global_summary(gs)
    scene_geo = _scene_geography_from_global_summary(gs)
    shots = _build_shots_from_full_video_items(shots_data)
    return StoryboardDocument(
        title="",
        synopsis=synopsis_seed,
        characters="",
        source_video=source_video,
        shots=shots,
        raw_scene_analyses=[json.dumps(data, ensure_ascii=False)],
        scene_geography=scene_geo,
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
        if file_size <= _DASHSCOPE_LOCAL_FILE_LIMIT:
            data = _run_full_video_dashscope_local_file(settings, path, style_hint)
            doc = _storyboard_from_full_video_json(data, str(path))
            if consolidate_result:
                doc = consolidate_storyboard(doc, settings)
            return doc

        compressed_tmp = _compress_video_for_api(path, _COMPRESS_TARGET_BYTES)
        if compressed_tmp and compressed_tmp.stat().st_size <= raw_limit:
            working_path = compressed_tmp
        else:
            if compressed_tmp:
                compressed_tmp.unlink(missing_ok=True)
                compressed_tmp = None
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
