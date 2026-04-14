"""Wan 2.x text-to-video via DashScope VideoSynthesis."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading

log = logging.getLogger(__name__)
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING


class CancellationError(Exception):
    """Raised when the user cancels a running generation task."""



from dashscope import VideoSynthesis

from video2text.config.settings import Settings, resolve_theme_story_model
from video2text.core.storyboard import Shot, StoryboardDocument
from video2text.pipeline.composer import concat_videos_ffmpeg, reencode_concat
from video2text.services.wan_video import (
    generate_wan27_clip,
    model_max_duration_seconds,
    preflight_reference_urls_for_r2v,
    uses_wan27_http,
)

if TYPE_CHECKING:
    from video2text.core.ip_manager import IPCharacter, IPProfile

_ROLE_VIDEO_LINE = re.compile(r"^视频\s*(\d+)\s*[：:]\s*(.*)$")
_ROLE_IMAGE_LINE = re.compile(r"^图\s*(\d+)\s*[：:]\s*(.*)$")
# t2v subject format: "character1: Name — description" or "character1: Name: description"
_ROLE_CHAR_LINE = re.compile(r"^character\s*(\d+)\s*[：:]\s*(.*)$", re.IGNORECASE)
_VAGUE_ROLE_BODY_MARKERS = (
    "见参考图主体",
    "见参考",
    "参考图",
    "参考视频中",
    "外观与动作见参考",
    "无具体",
    "待定",
)

_ENGLISH_AUDIO_TEXT_GUARD = (
    "All dialogue, voice-over, narration, and spoken lines must be in natural English only. "
    "Do not use Chinese or other non-English languages."
)


@dataclass(frozen=True)
class WanClipTask:
    """单段万相生成任务（与 Web 断点续传、CLI 并行共用）。"""

    index: int
    prompt: str
    duration: int
    reference_urls: list[str]
    reference_video_urls: list[str]
    reference_video_descriptions: list[str]
    chunk_size: int
    reference_voice_url: str = ""
    audio: bool | None = None


def generation_duration_cap(settings: Settings, has_refs: bool) -> int:
    """当前生成路径下单次请求的时长上限（秒）：r2v 多为 10，t2v 多为 15。"""
    return model_max_duration_seconds(
        settings.video_ref_model if has_refs else settings.video_gen_model
    )


def build_wan_clip_tasks(
    doc: StoryboardDocument,
    settings: Settings,
    *,
    style: str = "",
    max_segment_seconds: float,
    subject_descriptions: list[str] | None,
    reference_urls: list[str] | None,
    reference_video_urls: list[str] | None,
    reference_video_descriptions: list[str] | None,
    per_chunk_reference_filter: bool,
    character_pool: list[CharacterPoolEntry] | None = None,
    poll_callback: Callable[[str], None] | None = None,
) -> tuple[list[WanClipTask], list[list[Shot]], bool, int, int, bool]:
    """
    构建各段万相任务列表（含参考预解析与 per-chunk 参考筛选）。
    返回 (tasks, chunks, do_filter, n_v, n_i, has_refs)。
    """
    shots = doc.shots
    if not shots:
        raise ValueError("Storyboard has no shots")

    ref_u = [str(p) for p in (reference_urls or []) if p and str(p).strip()]
    ref_v = [str(p) for p in (reference_video_urls or []) if p and str(p).strip()]
    ref_d = [
        str(p) for p in (reference_video_descriptions or []) if p and str(p).strip()
    ]
    has_refs = bool(ref_u or ref_v)
    dur_cap = generation_duration_cap(settings, has_refs)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 15.0))
    subject_block = format_subject_prompt_block(list(subject_descriptions or []))
    ref_hint = reference_subject_lock_hint(settings, has_refs)
    if ref_hint:
        subject_block = (
            f"{subject_block}。{ref_hint}" if subject_block.strip() else ref_hint
        )
    chunks = chunk_shots_by_max_duration(shots, max_seg_eff)

    pool = character_pool or []

    n_v, n_i = len(ref_v), len(ref_u)
    subj_list = list(subject_descriptions or [])
    v_bodies, i_bodies = extract_reference_slot_bodies(subj_list, ref_d, n_v, n_i)
    multi_ref = n_v + n_i > 1
    do_filter = bool(per_chunk_reference_filter and multi_ref and has_refs)

    # t2v per-chunk character filtering: parse character slots and filter per segment
    t2v_chars = parse_t2v_character_lines(subj_list) if not has_refs else []
    do_t2v_filter = bool(t2v_chars and len(t2v_chars) > 1)

    total_story_sec = sum(max(0.01, float(s.duration)) for s in shots)
    if poll_callback:
        if len(chunks) > 1:
            poll_callback(
                f"分镜总长约 {total_story_sec:.1f}s，超过单次 {max_seg_eff:.0f}s 上限，"
                f"将分为 {len(chunks)} 段并发生成后拼接。"
            )
        else:
            poll_callback(
                f"单次生成（1 段，约 {max_seg_eff:.0f}s 内多镜头），总参考时长约 {total_story_sec:.1f}s。"
            )
        if do_t2v_filter:
            names = [name for _, name, _ in t2v_chars]
            poll_callback(
                f"t2v 主体：{len(t2v_chars)} 个角色 ({', '.join(names)})；"
                f"各段将按分镜文本自动筛选本段出现的角色描述。"
            )

    if pool and poll_callback:
        poll_callback(
            f"角色池已加载 {len(pool)} 个角色，各段将按分镜内容自动匹配注入角色描述。"
        )

    if has_refs:
        ref_u, ref_v = preflight_reference_urls_for_r2v(settings, ref_u, ref_v)
        if poll_callback:
            if do_filter:
                poll_callback(
                    f"参考已解析：{n_v} 视频 + {n_i} 图；"
                    f"各段按分镜文本自动选取参考子集（主体说明中的关键词须出现在该段分镜文案中）。"
                )
            else:
                poll_callback(
                    f"参考主体：{n_v} 个视频 + {n_i} 张图 → 已解析为 URL，"
                    f"各段生成将复用同一批素材（对应 视频1… 图1…）。"
                )

    tasks: list[WanClipTask] = []
    for i, chunk in enumerate(chunks):
        if do_filter:
            sv, si = select_reference_indices_for_chunk(
                chunk,
                n_video=n_v,
                n_image=n_i,
                subject_descriptions=subj_list,
                ref_video_descriptions=ref_d,
                enabled=True,
            )
            sb = subject_block_for_chunk_refs(sv, si, v_bodies, i_bodies, settings)
        elif do_t2v_filter:
            # t2v per-chunk: only include characters whose name appears in this chunk's text
            # sv/si are empty because t2v has no reference images/videos
            sv, si = [], []
            blob = chunk_text_for_reference_match(chunk)
            matched = [
                (slot, name, desc) for slot, name, desc in t2v_chars
                if name and name.lower() in blob.lower()
            ]
            # fallback: if nothing matched, include all characters
            if not matched:
                matched = t2v_chars
            # renumber slots sequentially
            char_lines = [
                f"character{ni}: {name} — {desc}"
                for ni, (_, name, desc) in enumerate(matched, start=1)
            ]
            sb = format_subject_prompt_block(char_lines)
        else:
            sv, si = list(range(n_v)), list(range(n_i))
            sb = subject_block

        char_block = ""
        if pool:
            matched = match_characters_for_chunk(chunk, pool, settings)
            char_block = format_character_pool_block(matched)

        prompt, dur = build_wan_multi_shot_prompt(
            chunk, style, sb, max_duration=dur_cap,
            character_block=char_block,
            enforce_english_audio_text=settings.enforce_english_audio_text,
        )
        cu = [ref_u[j] for j in si]
        cv = [ref_v[j] for j in sv]
        cd = [ref_d[j] for j in sv if j < len(ref_d)]
        tasks.append(
            WanClipTask(
                index=i,
                prompt=prompt,
                duration=dur,
                reference_urls=cu,
                reference_video_urls=cv,
                reference_video_descriptions=cd,
                chunk_size=len(chunk),
            )
        )
    return tasks, chunks, do_filter, n_v, n_i, has_refs


def chunk_shots_by_max_duration(
    shots: list[Shot],
    max_seconds: float = 15.0,
) -> list[list[Shot]]:
    """
    将分镜按累计时长切分为多段，每段累计时长不超过 max_seconds。
    """
    max_seconds = max(2.0, min(15.0, float(max_seconds)))
    chunks: list[list[Shot]] = []
    cur: list[Shot] = []
    acc = 0.0
    for s in shots:
        d = max(1.0, float(s.duration))
        if cur and acc + d > max_seconds + 1e-6:
            chunks.append(cur)
            cur = []
            acc = 0.0
        cur.append(s)
        acc += d
    if cur:
        chunks.append(cur)
    return chunks


def reference_subject_lock_hint(settings: Settings, has_reference_media: bool) -> str:
    """
    官方参考生要求 prompt 中用约定序号指代多路参考，否则模型难以对齐主体。
    wan2.7：视频1/图1；wan2.6：character1 等。
    """
    if not has_reference_media:
        return ""
    if uses_wan27_http(settings.video_ref_model):
        return (
            "[Reference subject lock] Reference video/image uploaded. Generated content must match "
            "the appearance of subjects in the reference. For multiple references, use 'video1', "
            "'video2', 'image1', 'image2', etc. in order of upload (per wan2.7-r2v documentation)."
        )
    return (
        "[Reference subject lock] Reference video/image uploaded. Generated content must match "
        "the appearance of subjects in the reference. For multiple references, use 'character1', "
        "'character2', etc. in order of upload (per wan2.6-r2v documentation)."
    )


def parse_t2v_character_lines(
    subject_descriptions: list[str],
) -> list[tuple[str, str, str]]:
    """
    解析 t2v 格式的主体描述行: "character1: Name — description"
    返回 [(slot_label, name, full_body), ...]，name 用于关键词匹配。
    """
    results: list[tuple[str, str, str]] = []
    for line in subject_descriptions:
        s = str(line).strip()
        if not s:
            continue
        mc = _ROLE_CHAR_LINE.match(s)
        if mc:
            idx = mc.group(1)
            body = mc.group(2).strip()
            # 分离 "Name — desc" 或 "Name: desc"
            sep_match = re.match(r"^([^—\-:]+?)\s*(?:—|-{1,2}|:)\s*(.+)$", body)
            if sep_match:
                name = sep_match.group(1).strip()
                desc = sep_match.group(2).strip()
            else:
                name = body
                desc = body
            results.append((f"character{idx}", name, desc))
    return results


def is_t2v_subject_format(subject_descriptions: list[str]) -> bool:
    """判断 subject_descriptions 是否为 t2v character 格式。"""
    return any(_ROLE_CHAR_LINE.match(str(s).strip()) for s in subject_descriptions if s)


def format_subject_prompt_block(descriptions: list[str]) -> str:
    """
    将用户上传的参考主体描述（视频N: xxx / 图N: xxx / characterN: Name — desc）
    格式化为 prompt 前缀主体声明块。
    """
    lines = [t.strip() for t in descriptions if t and str(t).strip()]
    if not lines:
        return ""
    return "[Reference subjects] " + "; ".join(lines) + "."


@dataclass(frozen=True)
class CharacterPoolEntry:
    """角色池中的一条：角色名 + 外貌/特征描述。"""
    name: str
    description: str


def parse_character_pool(lines: list[str]) -> list[CharacterPoolEntry]:
    """
    解析用户输入的角色池（纯文生时，一行一条）。
    支持格式：
      "Alice: tall woman, black hair, red dress"
      "Alice, tall woman, black hair, red dress"  （首个逗号前当角色名）
      "tall woman, black hair"  （无冒号/逗号分隔名 → 整行做描述，名字取前两个词）
    """
    pool: list[CharacterPoolEntry] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if ":" in s or "：" in s:
            sep = ":" if ":" in s else "："
            name, desc = s.split(sep, 1)
            name, desc = name.strip(), desc.strip()
        elif "," in s:
            name, desc = s.split(",", 1)
            name, desc = name.strip(), desc.strip()
        else:
            name = s
            desc = s
        if name:
            pool.append(CharacterPoolEntry(name=name, description=desc or name))
    return pool


def match_characters_for_chunk(
    chunk: list[Shot],
    pool: list[CharacterPoolEntry],
    settings: Settings | None = None,
) -> list[CharacterPoolEntry]:
    """
    确定一段 chunk 里应注入哪些角色描述。
    优先级：
      1. 镜头上已有 characters_in_shot 标注 → 按名字精确/模糊匹配
      2. 关键字在 generation_prompt / character_action / dialogue 中出现 → 匹配
      3. 以上都不命中且有 settings → 调 LLM 判断
      4. 无法判断 → 返回全量池（保守策略）
    """
    if not pool:
        return []

    matched_indices: set[int] = set()

    annotated_names: set[str] = set()
    for shot in chunk:
        for n in shot.characters_in_shot:
            annotated_names.add(n.strip().lower())

    if annotated_names:
        for i, entry in enumerate(pool):
            en = entry.name.strip().lower()
            for an in annotated_names:
                if en in an or an in en:
                    matched_indices.add(i)

    if not matched_indices:
        blob = _chunk_text_blob(chunk)
        blob_lower = blob.lower()
        for i, entry in enumerate(pool):
            name_lower = entry.name.strip().lower()
            if len(name_lower) >= 2 and name_lower in blob_lower:
                matched_indices.add(i)
            else:
                for kw in _keywords_from_role_body(entry.description):
                    if len(kw) >= 3 and kw.lower() in blob_lower:
                        matched_indices.add(i)
                        break

    if not matched_indices and settings:
        try:
            matched_indices = _llm_match_characters(chunk, pool, settings)
        except Exception:
            pass

    if not matched_indices:
        return list(pool)

    return [pool[i] for i in sorted(matched_indices)]


def _chunk_text_blob(chunk: list[Shot]) -> str:
    parts: list[str] = []
    for s in chunk:
        for f in (
            s.generation_prompt,
            s.scene_description,
            s.character_action,
            s.dialogue,
            s.mood,
            getattr(s, "continuity_anchor", ""),
            getattr(s, "focal_character", ""),
        ):
            if f and str(f).strip():
                parts.append(str(f).strip())
    return "\n".join(parts)


def _llm_match_characters(
    chunk: list[Shot],
    pool: list[CharacterPoolEntry],
    settings: Settings,
) -> set[int]:
    """调用 LLM 判断 chunk 中出现了池里哪些角色，返回匹配的下标集合。"""
    from openai import OpenAI

    blob = _chunk_text_blob(chunk)
    names = [e.name for e in pool]
    system = (
        "You are given a set of shot descriptions from a storyboard and a list of character names. "
        "Return a JSON array of character names that appear or are referenced in the shots. "
        "Output strict JSON only — an array of strings, e.g. [\"Alice\", \"Bob\"]. "
        "If none match, return []."
    )
    user = (
        f"Character pool:\n{json.dumps(names)}\n\n"
        f"Shots text:\n{blob[:3000]}"
    )
    from video2text.config.settings import resolve_light_model
    client = OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.base_url,
    )
    model = resolve_light_model(settings)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw = (completion.choices[0].message.content or "").strip()
    arr = json.loads(raw) if raw.startswith("[") else []
    if not isinstance(arr, list):
        return set()
    matched: set[int] = set()
    for item in arr:
        item_lower = str(item).strip().lower()
        for i, entry in enumerate(pool):
            if entry.name.strip().lower() == item_lower:
                matched.add(i)
    return matched


def format_character_pool_block(entries: list[CharacterPoolEntry]) -> str:
    """将匹配到的角色描述格式化为 prompt 前缀块。"""
    if not entries:
        return ""
    parts = [f"{e.name}: {e.description}" for e in entries]
    return "[Character descriptions] " + "; ".join(parts) + "."


def chunk_text_for_reference_match(chunk: list[Shot]) -> str:
    """仅用语义匹配「本段可能出现哪些参考角色」。"""
    parts: list[str] = []
    for s in chunk:
        for field in (
            s.generation_prompt,
            s.scene_description,
            s.character_action,
            s.dialogue,
            s.shot_type,
            s.mood,
            s.lighting,
        ):
            if field and str(field).strip():
                parts.append(str(field).strip())
    return "\n".join(parts)


def _keywords_from_role_body(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    out: list[str] = []
    if len(t) <= 48:
        out.append(t)
    for p in re.split(r"[，,。；;、/\n|]+", t):
        p = p.strip()
        if len(p) >= 2:
            out.append(p)
    for m in re.finditer(r"[A-Za-z][A-Za-z\s\-]{3,}", t):
        w = m.group(0).strip()
        if len(w) >= 4:
            out.append(w)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _role_body_matches_chunk(body: str, chunk_blob: str) -> bool:
    b = (body or "").strip()
    if not b:
        return True
    if len(b) <= 20 and any(m in b for m in _VAGUE_ROLE_BODY_MARKERS):
        return True
    for kw in _keywords_from_role_body(b):
        if len(kw) >= 2 and kw in chunk_blob:
            return True
    return False


def extract_reference_slot_bodies(
    subject_descriptions: list[str],
    ref_video_descriptions: list[str],
    n_video: int,
    n_image: int,
) -> tuple[list[str], list[str]]:
    """与 Web 端一致：视频槽、图槽的说明文案（用于匹配与重编号）。"""
    v_bodies = [""] * max(0, n_video)
    i_bodies = [""] * max(0, n_image)
    for line in subject_descriptions:
        s = str(line).strip()
        if not s:
            continue
        mv = _ROLE_VIDEO_LINE.match(s)
        if mv:
            idx = int(mv.group(1)) - 1
            if 0 <= idx < n_video:
                v_bodies[idx] = mv.group(2).strip()
            continue
        mi = _ROLE_IMAGE_LINE.match(s)
        if mi:
            idx = int(mi.group(1)) - 1
            if 0 <= idx < n_image:
                i_bodies[idx] = mi.group(2).strip()
            continue
    for i in range(n_video):
        if not v_bodies[i] and i < len(ref_video_descriptions):
            v_bodies[i] = str(ref_video_descriptions[i]).strip()
    return v_bodies, i_bodies


def select_reference_indices_for_chunk(
    chunk: list[Shot],
    *,
    n_video: int,
    n_image: int,
    subject_descriptions: list[str],
    ref_video_descriptions: list[str],
    enabled: bool,
) -> tuple[list[int], list[int]]:
    """
    返回本段应传入万相的参考视频下标、参考图下标（相对原始列表顺序）。
    匹配不上任何槽位时退回「全量参考」，避免误杀。
    """
    if not enabled or n_video + n_image <= 1:
        return list(range(n_video)), list(range(n_image))
    v_bodies, i_bodies = extract_reference_slot_bodies(
        subject_descriptions, ref_video_descriptions, n_video, n_image
    )
    blob = chunk_text_for_reference_match(chunk)
    sv = [i for i in range(n_video) if _role_body_matches_chunk(v_bodies[i], blob)]
    si = [i for i in range(n_image) if _role_body_matches_chunk(i_bodies[i], blob)]
    if not sv and not si:
        return list(range(n_video)), list(range(n_image))
    return sv, si


def renumbered_reference_subject_lines(
    sel_video: list[int],
    sel_image: list[int],
    v_bodies: list[str],
    i_bodies: list[str],
) -> list[str]:
    """按万相顺序：先视频后图，序号从 1 连续编号，与传入的 reference_* 顺序一致。"""
    lines: list[str] = []
    for ni, oi in enumerate(sel_video, start=1):
        body = v_bodies[oi] if oi < len(v_bodies) else ""
        lines.append(f"视频{ni}：{body or '外观与动作见参考视频'}")
    for ni, oi in enumerate(sel_image, start=1):
        body = i_bodies[oi] if oi < len(i_bodies) else ""
        lines.append(f"图{ni}：{body or '见参考图主体'}")
    return lines


def subject_block_for_chunk_refs(
    sel_video: list[int],
    sel_image: list[int],
    v_bodies: list[str],
    i_bodies: list[str],
    settings: Settings,
) -> str:
    lines = renumbered_reference_subject_lines(
        sel_video, sel_image, v_bodies, i_bodies
    )
    block = format_subject_prompt_block(lines)
    has_refs = bool(sel_video or sel_image)
    rh = reference_subject_lock_hint(settings, has_refs)
    if rh:
        block = f"{block}{rh}" if block.strip() else rh
    return block


def _chunk_target_duration(shots: list[Shot], max_duration: int = 15) -> int:
    total = int(round(sum(max(0.01, s.duration) for s in shots)))
    return max(2, min(max_duration, total))


def _allocate_lens_seconds(
    shots: list[Shot], target: int, max_duration: int = 15
) -> list[int]:
    n = len(shots)
    if n == 0:
        return []
    target = max(target, n)
    target = min(max_duration, target)
    base, rem = divmod(target, n)
    alloc = [base + (1 if i < rem else 0) for i in range(n)]
    for i in range(n):
        if alloc[i] < 1:
            alloc[i] = 1
    while sum(alloc) > target:
        j = max(range(n), key=lambda i: alloc[i])
        if alloc[j] > 1:
            alloc[j] -= 1
        else:
            break
    while sum(alloc) < target:
        j = min(range(n), key=lambda i: alloc[i])
        alloc[j] += 1
    return alloc


def build_wan_multi_shot_prompt(
    chunk: list[Shot],
    style: str,
    subject_block: str = "",
    max_duration: int = 15,
    character_block: str = "",
    enforce_english_audio_text: bool = True,
) -> tuple[str, int]:
    """
    构建万相多镜头 prompt。
    结构：[角色描述] [主体声明] [风格] [第N个镜头 generation_prompt。对白：xxx]
    """
    target = _chunk_target_duration(chunk, max_duration)
    target = max(target, len(chunk))
    target = min(max_duration, target)
    lens = _allocate_lens_seconds(chunk, target, max_duration)
    if len(lens) != len(chunk):
        raise ValueError("lens allocation mismatch")

    prefix_parts: list[str] = []
    if character_block.strip():
        prefix_parts.append(character_block.strip())
    if subject_block.strip():
        prefix_parts.append(subject_block.strip())
    if style.strip():
        prefix_parts.append(f"Visual style: {style.strip()}")
    prefix = " ".join(prefix_parts) if prefix_parts else ""

    t0 = 0
    segs: list[str] = []
    neg_hints: list[str] = []
    for shot, sec in zip(chunk, lens):
        t1 = t0 + sec
        gp = shot.generation_prompt.strip()
        if gp:
            visual = gp
        else:
            fallback_parts = [
                x for x in (shot.scene_description, shot.character_action) if x and x.strip()
            ]
            visual = ", ".join(fallback_parts) if fallback_parts else shot.shot_type or "scene"

        seg_text = f"Shot {len(segs)+1} [{t0}-{t1}s]: {visual}"
        if shot.dialogue and shot.dialogue.strip():
            seg_text += f" | dialogue: {shot.dialogue.strip()}"
        ambient = getattr(shot, "ambient_sound", "") or ""
        if ambient.strip():
            seg_text += f" | ambient: {ambient.strip()}"
        segs.append(seg_text)
        t0 = t1

        neg = getattr(shot, "negative_prompt_hint", "") or ""
        if neg.strip():
            neg_hints.append(neg.strip())

    shots_text = ". ".join(segs)
    prompt = f"{prefix} {shots_text}".strip() if prefix else shots_text

    if neg_hints:
        combined_neg = ", ".join(dict.fromkeys(neg_hints))
        prompt += f" Avoid unwanted elements: {combined_neg}."

    if enforce_english_audio_text:
        prompt += f" {_ENGLISH_AUDIO_TEXT_GUARD}"

    return prompt, target


def assign_generation_prompts(
    doc: StoryboardDocument,
    style: str = "",
    max_segment_seconds: float = 15.0,
    subject_descriptions: list[str] | None = None,
    api_duration_cap: int = 15,
    reference_hint: str = "",
    character_pool: list[CharacterPoolEntry] | None = None,
    settings: Settings | None = None,
) -> None:
    """为仍缺 generation_prompt 的镜头填入本段万相合成 prompt（与分段逻辑一致）。"""
    shots = doc.shots
    if not shots:
        return
    block = format_subject_prompt_block(list(subject_descriptions or []))
    rh = (reference_hint or "").strip()
    if rh:
        block = f"{block}{rh}" if block.strip() else rh
    pool = character_pool or []
    chunk_max = max(
        2.0, min(float(max_segment_seconds), float(api_duration_cap))
    )
    for chunk in chunk_shots_by_max_duration(shots, chunk_max):
        char_block = ""
        if pool:
            matched = match_characters_for_chunk(chunk, pool, settings)
            char_block = format_character_pool_block(matched)
        prompt, _ = build_wan_multi_shot_prompt(
            chunk, style, block, max_duration=api_duration_cap,
            character_block=char_block,
            enforce_english_audio_text=(
                settings.enforce_english_audio_text if settings else True
            ),
        )
        for s in chunk:
            if not s.generation_prompt.strip():
                s.generation_prompt = prompt


def generate_video_clip(
    prompt: str,
    duration: int,
    settings: Settings,
    size: str | None = None,
    watermark: bool | None = None,
    poll_callback: Callable[[str], None] | None = None,
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    reference_video_description: list[str] | None = None,
) -> str:
    """
    Submit async video generation and wait for result URL.
    reference_* 为万相可选参数：参考图/视频 URL 或本地路径（SDK 可自动上传）。
    """
    api_key = settings.dashscope_api_key
    if watermark is None:
        watermark = settings.video_watermark

    ref_u = [u for u in (reference_urls or []) if u and str(u).strip()]
    ref_v = [u for u in (reference_video_urls or []) if u and str(u).strip()]
    ref_d = [d for d in (reference_video_description or []) if d and str(d).strip()]
    has_refs = bool(ref_u or ref_v)
    size_eff = size or settings.default_resolution

    if has_refs:
        ref_model = settings.video_ref_model
        if uses_wan27_http(ref_model):
            return generate_wan27_clip(
                settings,
                prompt,
                duration,
                reference_image_urls=ref_u,
                reference_video_urls=ref_v,
                watermark=watermark,
                size=size_eff,
                poll_callback=poll_callback,
            )
        kw_model = ref_model
    else:
        if uses_wan27_http(settings.video_gen_model):
            return generate_wan27_clip(
                settings,
                prompt,
                duration,
                watermark=watermark,
                size=size_eff,
                poll_callback=poll_callback,
            )
        kw_model = settings.video_gen_model

    kw: dict[str, Any] = dict(
        api_key=api_key,
        model=kw_model,
        prompt=prompt,
        size=size_eff,
        duration=duration,
        shot_type="multi",
        prompt_extend=settings.video_prompt_extend,
        watermark=watermark,
    )
    if ref_u:
        kw["reference_urls"] = ref_u
    if ref_v:
        kw["reference_video_urls"] = ref_v
    if ref_d:
        kw["reference_video_description"] = ref_d

    rsp = VideoSynthesis.async_call(**kw)
    if rsp.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"VideoSynthesis.async_call failed: {rsp.code} {rsp.message}"
        )
    task_id = rsp.output.task_id
    if poll_callback:
        poll_callback(f"submitted task {task_id}")

    rsp = VideoSynthesis.wait(task=rsp, api_key=api_key)
    if rsp.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"VideoSynthesis.wait failed: {rsp.code} {rsp.message}"
        )
    url = getattr(rsp.output, "video_url", None)
    if not url:
        raise RuntimeError("No video_url in response")
    return url


def generate_all_clips(
    doc: StoryboardDocument,
    settings: Settings,
    style: str = "",
    size: str | None = None,
    max_workers: int = 2,
    poll_callback: Callable[[str], None] | None = None,
    max_segment_seconds: float = 15.0,
    subject_descriptions: list[str] | None = None,
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    reference_video_descriptions: list[str] | None = None,
    per_chunk_reference_filter: bool = True,
    character_pool: list[CharacterPoolEntry] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[tuple[str, int]]:
    """
    按 max_segment_seconds 将分镜切段；每段一次万相调用（≤15s），最后由调用方拼接。
    支持 cancel_event 协作式取消。
    """
    has_refs_preview = bool(
        (reference_urls and any(str(x).strip() for x in reference_urls))
        or (
            reference_video_urls
            and any(str(x).strip() for x in reference_video_urls)
        )
    )
    dur_cap = generation_duration_cap(settings, has_refs_preview)
    max_segment_seconds = max(
        2.0, min(float(max_segment_seconds), float(dur_cap), 15.0)
    )

    tasks, chunks, do_filter, n_v, n_i, _has_refs = build_wan_clip_tasks(
        doc,
        settings,
        style=style,
        max_segment_seconds=max_segment_seconds,
        subject_descriptions=subject_descriptions,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        reference_video_descriptions=reference_video_descriptions,
        per_chunk_reference_filter=per_chunk_reference_filter,
        character_pool=character_pool,
        poll_callback=poll_callback,
    )

    results: dict[int, str] = {}

    def run_one(t: WanClipTask) -> tuple[int, str]:
        if cancel_event and cancel_event.is_set():
            raise CancellationError("用户取消了任务")
        if poll_callback:
            extra = ""
            if do_filter and (
                len(t.reference_video_urls) + len(t.reference_urls) < n_v + n_i
            ):
                extra = f" 本段参考 {len(t.reference_video_urls)} 视频+{len(t.reference_urls)} 图。"
            poll_callback(
                f"第 {t.index + 1}/{len(tasks)} 段：生成中（API 时长 {t.duration}s，"
                f"镜头数 {t.chunk_size}）。{extra}".strip()
            )
        url = generate_video_clip(
            t.prompt,
            t.duration,
            settings,
            size=size,
            poll_callback=None,
            reference_urls=t.reference_urls or None,
            reference_video_urls=t.reference_video_urls or None,
            reference_video_description=t.reference_video_descriptions or None,
        )
        return t.index, url

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(run_one, t) for t in tasks]
        for fut in as_completed(futs):
            idx, url = fut.result()
            results[idx] = url

    return [(results[i], i) for i in sorted(results.keys())]


def download_url(url: str, dest: Path, timeout: float = 600.0) -> None:
    from urllib import request

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = request.Request(url, headers={"User-Agent": "video2text/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


def _count_valid_segments(segments_dir: Path) -> int:
    return sum(
        1
        for p in segments_dir.glob("seg_*.mp4")
        if p.is_file() and p.stat().st_size > 1024
    )


def run_checkpointed_storyboard_generation(
    doc: StoryboardDocument,
    settings: Settings,
    *,
    segments_dir: Path,
    output_mp4: Path,
    style: str = "",
    size: str | None = None,
    max_segment_seconds: float,
    subject_descriptions: list[str],
    reference_urls: list[str],
    reference_video_urls: list[str],
    reference_video_descriptions: list[str],
    per_chunk_reference_filter: bool = True,
    character_pool: list[CharacterPoolEntry] | None = None,
    progress_cb: Callable[[str], None],
    meta_update: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int = 2,
    cancel_event: threading.Event | None = None,
) -> Path:
    """
    并发生成各段，已存在的 seg_*.mp4 跳过；最后拼接 output.mp4。
    - max_workers 控制并发数（默认 2，Web 端可配置）
    - cancel_event 用于协作式取消：生成前检查，若已设置则抛出 CancellationError
    - meta_update 用于写 segments_total / segments_done
    """
    segments_dir.mkdir(parents=True, exist_ok=True)
    has_refs_preview = bool(reference_urls or reference_video_urls)
    dur_cap = generation_duration_cap(settings, has_refs_preview)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 15.0))

    tasks, _chunks, do_filter, n_v, n_i, _has_refs = build_wan_clip_tasks(
        doc,
        settings,
        style=style,
        max_segment_seconds=max_seg_eff,
        subject_descriptions=subject_descriptions,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        reference_video_descriptions=reference_video_descriptions,
        per_chunk_reference_filter=per_chunk_reference_filter,
        character_pool=character_pool,
        poll_callback=progress_cb,
    )

    if meta_update:
        meta_update(
            {
                "segments_total": len(tasks),
                "segments_done": _count_valid_segments(segments_dir),
            }
        )

    # 过滤出需要生成的任务（跳过缓存）
    pending_tasks: list[WanClipTask] = []
    for t in tasks:
        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 1024:
            progress_cb(f"使用缓存片段 {t.index + 1}/{len(tasks)}：{seg_path.name}")
        else:
            pending_tasks.append(t)

    if cancel_event and cancel_event.is_set():
        raise CancellationError("用户取消了任务")

    # 保护 meta_update 的线程安全
    _meta_lock = threading.Lock()

    def run_one(t: WanClipTask) -> None:
        if cancel_event and cancel_event.is_set():
            raise CancellationError("用户取消了任务")

        sub = ""
        if do_filter and len(t.reference_video_urls) + len(t.reference_urls) < n_v + n_i:
            sub = (
                f" 本段参考 {len(t.reference_video_urls)} 视频+"
                f"{len(t.reference_urls)} 图。"
            )
        progress_cb(
            f"生成第 {t.index + 1}/{len(tasks)} 段（约 {t.duration}s，"
            f"{t.chunk_size} 镜）。{sub}".strip()
        )
        url = generate_video_clip(
            t.prompt,
            t.duration,
            settings,
            size=size,
            poll_callback=None,
            reference_urls=t.reference_urls or None,
            reference_video_urls=t.reference_video_urls or None,
            reference_video_description=t.reference_video_descriptions or None,
        )
        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        download_url(url, seg_path)
        with _meta_lock:
            if meta_update:
                meta_update({"segments_done": _count_valid_segments(segments_dir)})
        progress_cb(f"第 {t.index + 1} 段已保存。")

    effective_workers = max(1, min(max_workers, len(pending_tasks))) if pending_tasks else 1
    if len(pending_tasks) > 1:
        progress_cb(f"并发生成 {len(pending_tasks)} 个待生成片段（并发数 {effective_workers}）…")

    if pending_tasks:
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            futs = [ex.submit(run_one, t) for t in pending_tasks]
            for fut in as_completed(futs):
                fut.result()  # 会传播 CancellationError 或其他异常

    # 检查片段完整性
    paths = sorted(segments_dir.glob("seg_*.mp4"))
    valid_paths = [p for p in paths if p.stat().st_size > 1024]
    if len(valid_paths) != len(tasks):
        raise RuntimeError(
            f"片段数量不一致：期望 {len(tasks)}，实际有效 {len(valid_paths)}，请删除损坏缓存后重试。"
        )

    progress_cb("正在拼接最终视频…")
    try:
        concat_videos_ffmpeg(valid_paths, output_mp4)
    except subprocess.CalledProcessError:
        reencode_concat(valid_paths, output_mp4)
    progress_cb(f"完成：{output_mp4.name}")
    return output_mp4


def run_storyboard_clip_generation(
    doc: StoryboardDocument,
    settings: Settings,
    *,
    style: str = "",
    size: str | None = None,
    max_segment_seconds: float,
    subject_descriptions: list[str] | None = None,
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    reference_video_descriptions: list[str] | None = None,
    per_chunk_reference_filter: bool = True,
    character_pool: list[CharacterPoolEntry] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    checkpoint_dir: Path | None = None,
    output_video: str | Path | None = None,
    meta_update: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int = 2,
    cancel_event: threading.Event | None = None,
) -> Path | list[tuple[str, int]]:
    """
    万相分段生成的统一入口。

    - **断点并发模式**：传入 ``checkpoint_dir`` 与 ``output_video``，并发生成（max_workers）、
      跳过已存在 ``seg_*.mp4``，并写入最终成片。
    - **内存并行模式**：``checkpoint_dir`` 为 ``None`` 时，用 ``ThreadPoolExecutor(max_workers)``
      拉取各段 URL；若提供 ``output_video`` 则下载后 ffmpeg 拼接并返回 ``Path``，
      否则返回 ``[(url, index), ...]`` 由调用方自行处理。

    两种模式均支持 ``cancel_event`` 协作式取消。
    """
    subj = list(subject_descriptions or [])
    ref_u = list(reference_urls or [])
    ref_v = list(reference_video_urls or [])
    ref_d = list(reference_video_descriptions or [])
    cb = progress_callback or (lambda _m: None)

    if checkpoint_dir is not None:
        if output_video is None:
            raise ValueError("checkpoint_dir 模式下必须提供 output_video")
        return run_checkpointed_storyboard_generation(
            doc,
            settings,
            segments_dir=checkpoint_dir,
            output_mp4=Path(output_video),
            style=style,
            size=size,
            max_segment_seconds=max_segment_seconds,
            subject_descriptions=subj,
            reference_urls=ref_u,
            reference_video_urls=ref_v,
            reference_video_descriptions=ref_d,
            per_chunk_reference_filter=per_chunk_reference_filter,
            character_pool=character_pool,
            progress_cb=cb,
            meta_update=meta_update,
            max_workers=max_workers,
            cancel_event=cancel_event,
        )

    has_refs_preview = bool(
        (ref_u and any(str(x).strip() for x in ref_u))
        or (ref_v and any(str(x).strip() for x in ref_v))
    )
    dur_cap = generation_duration_cap(settings, has_refs_preview)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 15.0))

    clips = generate_all_clips(
        doc,
        settings,
        style=style,
        size=size,
        max_workers=max_workers,
        poll_callback=progress_callback,
        max_segment_seconds=max_seg_eff,
        subject_descriptions=subj,
        reference_urls=ref_u,
        reference_video_urls=ref_v,
        reference_video_descriptions=ref_d,
        per_chunk_reference_filter=per_chunk_reference_filter,
        character_pool=character_pool,
        cancel_event=cancel_event,
    )

    if output_video is None:
        return clips

    out = Path(output_video)
    tmp = Path(tempfile.mkdtemp(prefix="v2t_gen_"))
    try:
        paths: list[Path] = []
        for i, (url, _) in enumerate(clips):
            seg = tmp / f"seg_{i:03d}.mp4"
            cb(f"下载片段 {i + 1}/{len(clips)} …")
            download_url(url, seg)
            paths.append(seg)
        try:
            concat_videos_ffmpeg(paths, out)
        except subprocess.CalledProcessError:
            cb("流复制拼接失败，尝试重编码拼接…")
            reencode_concat(paths, out)
        return out.resolve()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 主题模式：按角色参考图（复用 IP per-chunk 检测与图N 改写）
# ---------------------------------------------------------------------------


@dataclass
class SubjectCharacter:
    """主题任务 subjects.json 中的角色，字段满足 detect/build_media 所需。"""

    id: str
    name: str
    name_en: str
    reference_image_path: str = ""


def subjects_json_to_characters(subjects: list[dict[str, Any]]) -> list[SubjectCharacter]:
    out: list[SubjectCharacter] = []
    for i, d in enumerate(subjects):
        en = str(d.get("name") or "").strip()
        zh = str(d.get("name_zh") or "").strip()
        out.append(
            SubjectCharacter(
                id=f"theme_subj_{i}",
                name=zh or en,
                name_en=en or zh,
                reference_image_path=str(d.get("reference_image_path") or "").strip(),
            )
        )
    return out


def build_subject_ref_wan_clip_tasks(
    doc: StoryboardDocument,
    characters: list[SubjectCharacter],
    settings: Settings,
    char_url_map: dict[str, str],
    *,
    style_keywords: str = "",
    max_segment_seconds: float = 10.0,
    poll_callback: Callable[[str], None] | None = None,
) -> list[WanClipTask]:
    """主题模式：每段按分镜检测出场角色，注入对应参考图并改写 prompt（同 IP 策略）。"""
    shots = doc.shots
    if not shots:
        raise ValueError("Storyboard has no shots")

    dur_cap = model_max_duration_seconds(settings.video_ref_model)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 10.0))
    chunks = chunk_shots_by_max_duration(shots, max_seg_eff)
    style_kw = (style_keywords or "").strip()

    cb = poll_callback or (lambda _: None)
    cb(
        f"主题·角色参考：{len(chunks)} 段，{len(characters)} 个主体，"
        f"{len(char_url_map)} 张参考图已上传"
    )

    tasks: list[WanClipTask] = []
    for i, chunk in enumerate(chunks):
        chunk_chars = detect_characters_in_chunk(chunk, list(characters))

        has_character_intent = any(
            shot.characters_in_shot or shot.focal_character
            for shot in chunk
        )
        if not chunk_chars and has_character_intent and poll_callback:
            poll_callback(
                f"⚠ 第 {i + 1}/{len(chunks)} 段：分镜标注了角色但未匹配到主体，"
                f"请检查主体卡片英文名/中文名与分镜一致"
            )

        media, name_map = build_ip_media_array(chunk_chars, char_url_map)
        ref_urls = [m["url"] for m in media if m["type"] == "image"]
        ref_hint = _build_ip_ref_hint(name_map) if ref_urls else ""

        prompt, dur = build_wan_multi_shot_prompt(
            chunk,
            "",
            ref_hint,
            max_duration=dur_cap,
            enforce_english_audio_text=settings.enforce_english_audio_text,
        )
        prompt = rewrite_prompt_for_ip_refs(prompt, name_map, style_kw)

        if poll_callback:
            if chunk_chars:
                char_names = [c.name for c in chunk_chars[:5]]
                poll_callback(
                    f"第 {i + 1}/{len(chunks)} 段：{len(chunk)} 镜头，"
                    f"角色 {char_names}，{len(ref_urls)} 张参考图"
                )
            else:
                poll_callback(
                    f"第 {i + 1}/{len(chunks)} 段：{len(chunk)} 镜头，"
                    f"无角色参考（环境镜头），走 t2v"
                )

        tasks.append(
            WanClipTask(
                index=i,
                prompt=prompt,
                duration=dur,
                reference_urls=ref_urls,
                reference_video_urls=[],
                reference_video_descriptions=[],
                chunk_size=len(chunk),
                reference_voice_url="",
                audio=None,
            )
        )

    return tasks


def run_subject_ref_storyboard_generation(
    doc: StoryboardDocument,
    subjects: list[dict[str, Any]],
    settings: Settings,
    *,
    segments_dir: Path,
    output_mp4: Path,
    size: str | None = None,
    max_segment_seconds: float = 10.0,
    style_keywords: str = "",
    progress_cb: Callable[[str], None] | None = None,
    meta_update: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int = 2,
    cancel_event: threading.Event | None = None,
) -> Path:
    """主题任务：按角色参考图生成视频（无音色管线）。"""
    cb = progress_cb or (lambda _: None)
    log.info(
        "主题角色参考视频管线启动: %d 镜头, style=%s",
        len(doc.shots),
        (style_keywords or "")[:80],
    )

    characters = subjects_json_to_characters(subjects)
    cb("正在上传角色参考图…")
    char_url_map = preflight_ip_character_images(characters, settings)
    log.info("主题角色参考图上传完成: %d 张", len(char_url_map))
    cb(f"已上传 {len(char_url_map)} 张角色参考图")

    tasks = build_subject_ref_wan_clip_tasks(
        doc,
        characters,
        settings,
        char_url_map,
        style_keywords=style_keywords,
        max_segment_seconds=max_segment_seconds,
        poll_callback=cb,
    )

    _, full_map = build_ip_media_array(characters, char_url_map)
    if full_map:
        doc.ip_char_ref_map = full_map
        storyboard_path = segments_dir.parent / "storyboard.json"
        if storyboard_path.is_file():
            doc.save_json(storyboard_path)

    segments_dir.mkdir(parents=True, exist_ok=True)
    if meta_update:
        meta_update({
            "segments_total": len(tasks),
            "segments_done": _count_valid_segments(segments_dir),
        })

    pending_tasks: list[WanClipTask] = []
    for t in tasks:
        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 1024:
            cb(f"使用缓存片段 {t.index + 1}/{len(tasks)}")
        else:
            pending_tasks.append(t)

    if cancel_event and cancel_event.is_set():
        raise CancellationError("用户取消了任务")

    _meta_lock = threading.Lock()
    _active_segments: dict[int, str] = {}
    _last_broadcast: str = ""

    def _broadcast_progress() -> None:
        nonlocal _last_broadcast
        with _meta_lock:
            done_count = _count_valid_segments(segments_dir)
            parts = []
            for idx in sorted(_active_segments):
                parts.append(f"段{idx + 1}: {_active_segments[idx]}")
            active_str = " | ".join(parts) if parts else ""
        total = len(tasks)
        summary = f"已完成 {done_count}/{total}"
        if active_str:
            summary += f"  ▶ {active_str}"
        if summary != _last_broadcast:
            _last_broadcast = summary
            cb(summary)

    def run_one(t: WanClipTask) -> None:
        if cancel_event and cancel_event.is_set():
            raise CancellationError("用户取消了任务")
        log.info(
            "主题角色参考段 %d/%d 开始（约 %ds，%d 镜）",
            t.index + 1,
            len(tasks),
            t.duration,
            t.chunk_size,
        )

        with _meta_lock:
            _active_segments[t.index] = "提交中"
        _broadcast_progress()

        def _poll_cb(_msg: str) -> None:
            with _meta_lock:
                _active_segments[t.index] = "渲染中"
            _broadcast_progress()

        ref_u = t.reference_urls or None
        if ref_u:
            url = generate_wan27_clip(
                settings,
                t.prompt,
                t.duration,
                reference_image_urls=ref_u,
                reference_voice_url=None,
                audio=t.audio,
                size=size,
                poll_callback=_poll_cb,
            )
        else:
            url = generate_video_clip(
                t.prompt,
                t.duration,
                settings,
                size=size,
                poll_callback=_poll_cb,
            )
        with _meta_lock:
            _active_segments[t.index] = "下载中"
        _broadcast_progress()

        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        download_url(url, seg_path)
        with _meta_lock:
            del _active_segments[t.index]
            if meta_update:
                meta_update({"segments_done": _count_valid_segments(segments_dir)})
        log.info("主题角色参考段 %d/%d 完成", t.index + 1, len(tasks))
        _broadcast_progress()

    if pending_tasks:
        effective_workers = max(1, min(max_workers, len(pending_tasks)))
        if len(pending_tasks) > 1:
            cb(f"并发生成 {len(pending_tasks)} 个片段（并发数 {effective_workers}）…")
            log.info(
                "主题角色参考并发生成 %d 段（并发数 %d）",
                len(pending_tasks),
                effective_workers,
            )
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            futs = [ex.submit(run_one, t) for t in pending_tasks]
            for fut in as_completed(futs):
                fut.result()

    paths = sorted(segments_dir.glob("seg_*.mp4"))
    valid_paths = [p for p in paths if p.stat().st_size > 1024]
    if len(valid_paths) != len(tasks):
        raise RuntimeError(
            f"片段数量不一致：期望 {len(tasks)}，实际 {len(valid_paths)}"
        )

    log.info("主题角色参考：拼接（%d 段）", len(valid_paths))
    cb("正在拼接最终视频…")
    try:
        concat_videos_ffmpeg(valid_paths, output_mp4)
    except subprocess.CalledProcessError:
        reencode_concat(valid_paths, output_mp4)
    log.info("视频拼接完成: %s", output_mp4.name)
    cb(f"完成：{output_mp4.name}")
    return output_mp4


# ---------------------------------------------------------------------------
# IP 模式：角色参考注入管线
# ---------------------------------------------------------------------------


def preflight_ip_character_images(
    characters: list[Any],
    settings: Settings,
) -> dict[str, str]:
    """上传所有角色参考图到 OSS，返回 {char_id: public_url} 映射。

    只处理有 reference_image_path 的角色。已是 http/oss URL 的保持不变。
    """
    from dashscope.utils.oss_utils import check_and_upload_local
    from video2text.services.media_normalize import normalize_local_reference_path

    model = settings.video_ref_model
    api_key = settings.dashscope_api_key
    cert = None
    result: dict[str, str] = {}

    for char in characters:
        path = char.reference_image_path.strip()
        if not path:
            continue
        if path.startswith("http://") or path.startswith("https://") or path.startswith("oss://"):
            result[char.id] = path
            continue
        local = normalize_local_reference_path(path, kind="image")
        _, url, cert = check_and_upload_local(model, local, api_key, cert)
        result[char.id] = url

    return result


def preflight_ip_character_voices(
    characters: list[IPCharacter],
    settings: Settings,
) -> dict[str, str]:
    """上传角色参考音频到 OSS，返回 {char_id: voice_audio_url} 映射。

    仅处理 voice_profile.mode == "clone" 且有本地参考音频的角色；
    已有 URL 的或使用预置音色的跳过。
    """
    from dashscope.utils.oss_utils import check_and_upload_local

    model = settings.video_ref_model
    api_key = settings.dashscope_api_key
    cert = None
    result: dict[str, str] = {}

    for char in characters:
        vp = char.voice_profile
        if not vp.is_configured:
            continue
        url = vp.reference_audio_url.strip()
        if url:
            result[char.id] = url
            continue
        path = vp.reference_audio_path.strip()
        if not path:
            continue
        if path.startswith(("http://", "https://", "oss://")):
            result[char.id] = path
            continue
        _, url, cert = check_and_upload_local(model, path, api_key, cert)
        result[char.id] = url
        vp.reference_audio_url = url

    return result


def detect_characters_in_chunk(
    chunk: list[Shot],
    characters: list[Any],
) -> list[Any]:
    """检测 chunk 中出现的 IP 角色，按出现频率排序。

    匹配逻辑：角色的 name、name_en 出现在 characters_in_shot / generation_prompt /
    character_action / dialogue 等字段中。
    """
    blob = _chunk_text_blob(chunk).lower()

    annotated: set[str] = set()
    for shot in chunk:
        for n in shot.characters_in_shot:
            annotated.add(n.strip().lower())

    scored: list[tuple[int, Any]] = []
    for char in characters:
        score = 0
        names = [char.name.lower(), char.name_en.lower()]
        names = [n for n in names if n]

        for n in names:
            if any(n in an or an in n for an in annotated):
                score += 10
            count = blob.count(n)
            score += count

        if score > 0:
            scored.append((score, char))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [char for _, char in scored]


def build_ip_media_array(
    chunk_characters: list[Any],
    char_url_map: dict[str, str],
    max_refs: int = 5,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """构建 wan2.7-r2v 的 media 数组和角色名 -> 「图N」映射。

    Returns:
        (media_array, name_to_tag_map)
        media_array: [{"type": "image", "url": "..."}, ...]
        name_to_tag_map: {"Chubby": "图1", "Fishy": "图2", ...}
    """
    media: list[dict[str, str]] = []
    name_map: dict[str, str] = {}
    img_index = 0

    for char in chunk_characters:
        url = char_url_map.get(char.id)
        if not url:
            continue
        if img_index >= max_refs:
            break
        img_index += 1
        media.append({"type": "image", "url": url})
        tag = f"图{img_index}"
        if char.name:
            name_map[char.name] = tag
        if char.name_en and char.name_en != char.name:
            name_map[char.name_en] = tag

    return media, name_map


def _build_ip_ref_hint(name_to_tag: dict[str, str]) -> str:
    """为 IP 模式构建参考图说明（类似普通模式的 reference_subject_lock_hint）。"""
    if not name_to_tag:
        return ""
    seen: dict[str, str] = {}
    for name, tag in name_to_tag.items():
        if tag not in seen:
            seen[tag] = name
    parts = [f"{tag} is {name}" for tag, name in sorted(seen.items())]
    return "Reference images: " + ", ".join(parts) + "."


def rewrite_prompt_for_ip_refs(
    prompt: str,
    name_to_tag: dict[str, str],
    style_keywords_en: str = "",
) -> str:
    """将 generation_prompt 中的角色名替换为「图N」引用格式。

    也在末尾追加风格关键词（如果尚未包含）。
    """
    result = prompt
    for name, tag in sorted(name_to_tag.items(), key=lambda x: len(x[0]), reverse=True):
        result = result.replace(name, tag)

    if style_keywords_en and style_keywords_en.strip() not in result:
        result = f"{result.rstrip('.')}. {style_keywords_en.strip()}."

    return result


def build_ip_wan_clip_tasks(
    doc: StoryboardDocument,
    ip_profile: IPProfile,
    settings: Settings,
    char_url_map: dict[str, str],
    *,
    max_segment_seconds: float = 10.0,
    voice_url_map: dict[str, str] | None = None,
    voice_mode: str = "native",
    poll_callback: Callable[[str], None] | None = None,
) -> list[WanClipTask]:
    """构建 IP 模式的万相视频生成任务列表。

    与普通模式的区别：
    - 每个 chunk 自动检测出场角色
    - 构建角色图 media 数组
    - 改写 prompt 为「图N」引用格式
    - 追加风格关键词
    - voice_mode="native" 时传入角色参考音频（仅取每段的首个说话角色）
    - voice_mode="pipeline" 时生成静音视频（audio=False）
    """
    shots = doc.shots
    if not shots:
        raise ValueError("Storyboard has no shots")

    dur_cap = model_max_duration_seconds(settings.video_ref_model)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 10.0))
    chunks = chunk_shots_by_max_duration(shots, max_seg_eff)

    characters = ip_profile.characters
    style_kw = ip_profile.visual_dna.style_keywords_en or ip_profile.visual_dna.style_keywords
    voice_map = voice_url_map or {}

    cb = poll_callback or (lambda _: None)
    cb(f"IP 模式：{len(chunks)} 段，{len(characters)} 个角色，{len(char_url_map)} 张参考图已上传")
    if voice_mode == "native" and voice_map:
        cb(f"声音模式：原生音色（{len(voice_map)} 个角色有参考音频）")
    elif voice_mode == "native":
        cb("声音模式：原生音色（无参考音频，万相自行配音）")
    elif voice_mode == "pipeline":
        cb("声音模式：独立音频管线（生成静音视频 + TTS 配音）")
    elif voice_mode == "silent":
        cb("声音模式：静音（纯画面，无任何声音）")

    tasks: list[WanClipTask] = []
    for i, chunk in enumerate(chunks):
        chunk_chars = detect_characters_in_chunk(chunk, characters)

        has_character_intent = any(
            shot.characters_in_shot or shot.focal_character
            for shot in chunk
        )
        if not chunk_chars and has_character_intent:
            if poll_callback:
                poll_callback(
                    f"⚠ 第 {i+1}/{len(chunks)} 段：分镜标注了角色但未匹配到 IP 角色，"
                    f"建议在分镜编辑中检查角色名"
                )

        media, name_map = build_ip_media_array(chunk_chars, char_url_map)

        ref_urls = [m["url"] for m in media if m["type"] == "image"]
        ref_hint = _build_ip_ref_hint(name_map) if ref_urls else ""

        prompt, dur = build_wan_multi_shot_prompt(
            chunk, "", ref_hint, max_duration=dur_cap,
            enforce_english_audio_text=settings.enforce_english_audio_text,
        )

        prompt = rewrite_prompt_for_ip_refs(prompt, name_map, style_kw)

        chunk_voice_url = ""
        chunk_audio: bool | None = None
        if voice_mode == "native" and voice_map and chunk_chars:
            for cc in chunk_chars:
                vu = voice_map.get(cc.id)
                if vu:
                    chunk_voice_url = vu
                    break
        elif voice_mode in ("pipeline", "silent"):
            chunk_audio = False

        if poll_callback:
            voice_info = ""
            if chunk_voice_url:
                voice_info = "，含参考音频"
            elif chunk_audio is False:
                voice_info = "，静音"
            if chunk_chars:
                char_names = [c.name for c in chunk_chars[:5]]
                poll_callback(
                    f"第 {i+1}/{len(chunks)} 段：{len(chunk)} 镜头，"
                    f"角色 {char_names}，{len(ref_urls)} 张参考图{voice_info}"
                )
            else:
                poll_callback(
                    f"第 {i+1}/{len(chunks)} 段：{len(chunk)} 镜头，"
                    f"无角色参考（环境/物件镜头），走 t2v{voice_info}"
                )

        tasks.append(
            WanClipTask(
                index=i,
                prompt=prompt,
                duration=dur,
                reference_urls=ref_urls,
                reference_video_urls=[],
                reference_video_descriptions=[],
                chunk_size=len(chunk),
                reference_voice_url=chunk_voice_url,
                audio=chunk_audio,
            )
        )

    return tasks


def run_ip_storyboard_generation(
    doc: StoryboardDocument,
    ip_profile: IPProfile,
    settings: Settings,
    *,
    segments_dir: Path,
    output_mp4: Path,
    size: str | None = None,
    max_segment_seconds: float = 10.0,
    progress_cb: Callable[[str], None] | None = None,
    meta_update: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int = 2,
    cancel_event: threading.Event | None = None,
    voice_mode: str | None = None,
) -> Path:
    """IP 模式视频生成的完整管线。

    1. 预上传角色参考图（+ 参考音频）
    2. 构建 IP 模式任务列表
    3. 并发生成各段视频
    4. 拼接成片
    """
    cb = progress_cb or (lambda _: None)
    effective_voice_mode = voice_mode or settings.voice_mode
    log.info("IP 视频管线启动: %d 镜头, voice_mode=%s, size=%s", len(doc.shots), effective_voice_mode, size)

    # 预上传角色图
    cb("正在上传角色参考图…")
    char_url_map = preflight_ip_character_images(ip_profile.characters, settings)
    log.info("角色参考图上传完成: %d 张", len(char_url_map))
    cb(f"已上传 {len(char_url_map)} 张角色参考图")

    # 预上传角色参考音频（native 模式 + 有克隆音频时）
    voice_url_map: dict[str, str] = {}
    if effective_voice_mode == "native":
        voice_url_map = preflight_ip_character_voices(ip_profile.characters, settings)
        if voice_url_map:
            log.info("角色参考音频上传完成: %d 个", len(voice_url_map))
            cb(f"已上传 {len(voice_url_map)} 个角色参考音频")

    # 构建任务
    tasks = build_ip_wan_clip_tasks(
        doc, ip_profile, settings, char_url_map,
        max_segment_seconds=max_segment_seconds,
        voice_url_map=voice_url_map,
        voice_mode=effective_voice_mode,
        poll_callback=cb,
    )

    # 保存角色→图N 映射到分镜文档（供前端高亮显示 @角色名）
    global_name_map: dict[str, str] = {}
    all_chars = ip_profile.characters
    _, full_map = build_ip_media_array(all_chars, char_url_map)
    if full_map:
        global_name_map = full_map
        doc.ip_char_ref_map = global_name_map
        storyboard_path = segments_dir.parent / "storyboard.json"
        if storyboard_path.is_file():
            doc.save_json(storyboard_path)

    segments_dir.mkdir(parents=True, exist_ok=True)
    if meta_update:
        meta_update({
            "segments_total": len(tasks),
            "segments_done": _count_valid_segments(segments_dir),
        })

    pending_tasks = []
    for t in tasks:
        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 1024:
            cb(f"使用缓存片段 {t.index + 1}/{len(tasks)}")
        else:
            pending_tasks.append(t)

    if cancel_event and cancel_event.is_set():
        raise CancellationError("用户取消了任务")

    _meta_lock = threading.Lock()
    _active_segments: dict[int, str] = {}
    _last_broadcast: str = ""

    def _broadcast_progress() -> None:
        """汇总所有并行段的状态，仅在内容变化时推送。"""
        nonlocal _last_broadcast
        with _meta_lock:
            done_count = _count_valid_segments(segments_dir)
            parts = []
            for idx in sorted(_active_segments):
                parts.append(f"段{idx+1}: {_active_segments[idx]}")
            active_str = " | ".join(parts) if parts else ""
        total = len(tasks)
        summary = f"已完成 {done_count}/{total}"
        if active_str:
            summary += f"  ▶ {active_str}"
        if summary != _last_broadcast:
            _last_broadcast = summary
            cb(summary)

    def run_one(t: WanClipTask) -> None:
        if cancel_event and cancel_event.is_set():
            raise CancellationError("用户取消了任务")
        seg_label = f"段{t.index+1}"
        log.info("IP 视频段 %d/%d 开始（约 %ds，%d 镜）", t.index+1, len(tasks), t.duration, t.chunk_size)

        with _meta_lock:
            _active_segments[t.index] = "提交中"
        _broadcast_progress()

        def _poll_cb(msg: str) -> None:
            with _meta_lock:
                _active_segments[t.index] = "渲染中"
            _broadcast_progress()

        ref_u = t.reference_urls or None
        voice_u = t.reference_voice_url or None
        if ref_u or voice_u:
            url = generate_wan27_clip(
                settings,
                t.prompt,
                t.duration,
                reference_image_urls=ref_u,
                reference_voice_url=voice_u,
                audio=t.audio,
                size=size,
                poll_callback=_poll_cb,
            )
        else:
            url = generate_video_clip(
                t.prompt,
                t.duration,
                settings,
                size=size,
                poll_callback=_poll_cb,
            )
        with _meta_lock:
            _active_segments[t.index] = "下载中"
        _broadcast_progress()

        seg_path = segments_dir / f"seg_{t.index:03d}.mp4"
        download_url(url, seg_path)
        with _meta_lock:
            del _active_segments[t.index]
            if meta_update:
                meta_update({"segments_done": _count_valid_segments(segments_dir)})
        log.info("IP 视频段 %d/%d 完成", t.index+1, len(tasks))
        _broadcast_progress()

    if pending_tasks:
        effective_workers = max(1, min(max_workers, len(pending_tasks)))
        if len(pending_tasks) > 1:
            cb(f"并发生成 {len(pending_tasks)} 个片段（并发数 {effective_workers}）…")
            log.info("IP 视频并发生成 %d 段（并发数 %d）", len(pending_tasks), effective_workers)
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            futs = [ex.submit(run_one, t) for t in pending_tasks]
            for fut in as_completed(futs):
                fut.result()

    paths = sorted(segments_dir.glob("seg_*.mp4"))
    valid_paths = [p for p in paths if p.stat().st_size > 1024]
    if len(valid_paths) != len(tasks):
        raise RuntimeError(
            f"片段数量不一致：期望 {len(tasks)}，实际 {len(valid_paths)}"
        )

    log.info("所有视频段生成完成，开始拼接（%d 段）", len(valid_paths))
    cb("正在拼接最终视频…")
    silent_mp4 = output_mp4.with_suffix(".silent.mp4") if effective_voice_mode == "pipeline" else output_mp4
    try:
        concat_videos_ffmpeg(valid_paths, silent_mp4)
    except subprocess.CalledProcessError:
        reencode_concat(valid_paths, silent_mp4)
    log.info("视频拼接完成: %s", silent_mp4.name)

    # Mode B：独立音频管线 — 生成 TTS 音频并合并
    if effective_voice_mode == "pipeline":
        try:
            log.info("开始 TTS 音频管线（%d 个镜头）", len(doc.shots))
            cb("声音管线：生成 TTS 音频…")
            from video2text.pipeline.audio_align import build_chunk_audio
            from video2text.pipeline.composer import merge_audio_video

            def _tts_cb(m: str) -> None:
                cb(f"TTS: {m}")
                log.info("TTS: %s", m)

            chunk_result = build_chunk_audio(
                doc.shots, ip_profile, settings,
                progress_cb=_tts_cb,
            )

            audio_path = segments_dir / "tts_audio.wav"
            audio_path.write_bytes(chunk_result.audio_data)
            log.info("TTS 音频已生成（%dms）", chunk_result.duration_ms)
            cb(f"TTS 音频已生成（{chunk_result.duration_ms}ms）")

            log.info("开始合并音视频")
            cb("合并音视频…")
            merge_audio_video(silent_mp4, audio_path, output_mp4, replace_audio=True)
            silent_mp4.unlink(missing_ok=True)
            log.info("音视频合并完成: %s", output_mp4.name)
            cb(f"完成：{output_mp4.name}（含 TTS 音频）")
        except Exception as e:
            log.warning("声音管线失败，使用静音视频: %s", e, exc_info=True)
            cb(f"声音管线异常（{e}），使用静音视频")
            if silent_mp4 != output_mp4:
                shutil.copy2(silent_mp4, output_mp4)
                silent_mp4.unlink(missing_ok=True)
    else:
        log.info("视频生成完成（无 TTS）: %s", output_mp4.name)
        cb(f"完成：{output_mp4.name}")

    return output_mp4
