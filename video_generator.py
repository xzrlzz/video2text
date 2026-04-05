"""Wan 2.x text-to-video via DashScope VideoSynthesis."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

_ROLE_VIDEO_LINE = re.compile(r"^视频\s*(\d+)\s*[：:]\s*(.*)$")
_ROLE_IMAGE_LINE = re.compile(r"^图\s*(\d+)\s*[：:]\s*(.*)$")
_VAGUE_ROLE_BODY_MARKERS = (
    "见参考图主体",
    "见参考",
    "参考图",
    "参考视频中",
    "外观与动作见参考",
    "无具体",
    "待定",
)

import dashscope
from dashscope import VideoSynthesis

from config import Settings
from storyboard import StoryboardDocument, Shot
from wan_video_http import (
    generate_wan27_clip,
    model_max_duration_seconds,
    preflight_reference_urls_for_r2v,
    uses_wan27_http,
)


def generation_duration_cap(settings: Settings, has_refs: bool) -> int:
    """当前生成路径下单次请求的时长上限（秒）：r2v 多为 10，t2v 多为 15。"""
    return model_max_duration_seconds(
        settings.video_ref_model if has_refs else settings.video_gen_model
    )


def chunk_shots_by_max_duration(
    shots: list[Shot],
    max_seconds: float = 15.0,
) -> list[list[Shot]]:
    """
    将分镜按累计时长切分为多段，每段累计时长不超过 max_seconds（文生常见上限 15s，参考生常见 10s，由调用方传入）。
    若整条片子超过一段能容纳的时长，会得到多个 chunk，需分段生成再拼接。
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
    wan2.7：视频1/图1；wan2.6：character1 等（见帮助文档）。
    """
    if not has_reference_media:
        return ""
    if uses_wan27_http(settings.video_ref_model):
        return (
            "【参考主体锁定】已上传参考视频/图像，生成内容必须与参考中的主体外观一致；"
            "若有多路参考，请按上传顺序在描述中使用「视频1」「视频2」「图1」「图2」等指代（与万相 wan2.7-r2v 文档一致）。"
        )
    return (
        "【参考主体锁定】已上传参考视频/图像，须与参考角色外观一致；"
        "多路参考时请按传入顺序在描述中使用 character1、character2 等指代（与万相 wan2.6-r2v 文档一致）。"
    )


def format_subject_prompt_block(descriptions: list[str]) -> str:
    """用户提供的跨镜头主体文字说明，拼进万相 prompt 前缀。"""
    lines = [t.strip() for t in descriptions if t and str(t).strip()]
    if not lines:
        return ""
    parts = [f"主体{i+1}：{t}" for i, t in enumerate(lines)]
    return (
        "【用户指定·全片一致的主体参考】"
        + "；".join(parts)
        + "。各镜头中人物/关键物体外观须与上述设定一致，勿随意改变。"
    )


def chunk_text_for_reference_match(chunk: list[Shot]) -> str:
    """仅用语义匹配「本段可能出现哪些参考角色」，不含全片 characters 字段，避免全员误匹配。"""
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
        block = f"{block}。{rh}" if block.strip() else rh
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
    target = max(target, n)  # at least 1 second per shot in prompt
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
    doc: StoryboardDocument,
    style: str,
    subject_block: str = "",
    max_duration: int = 15,
) -> tuple[str, int]:
    """Return (prompt, duration_seconds) for万相多镜头（时长受 max_duration 封顶）。"""
    target = _chunk_target_duration(chunk, max_duration)
    target = max(target, len(chunk))
    target = min(max_duration, target)
    lens = _allocate_lens_seconds(chunk, target, max_duration)
    if len(lens) != len(chunk):
        raise ValueError("lens allocation mismatch")
    prefix_parts = []
    if subject_block.strip():
        prefix_parts.append(subject_block.strip())
    if doc.title:
        prefix_parts.append(doc.title)
    if doc.synopsis:
        prefix_parts.append(doc.synopsis[:200])
    if style.strip():
        prefix_parts.append(f"视觉风格：{style.strip()}")
    prefix = "。".join(prefix_parts) if prefix_parts else "根据下列分镜生成连贯视频"
    t0 = 0
    segs = []
    for shot, sec in zip(chunk, lens):
        t1 = t0 + sec
        if shot.generation_prompt.strip():
            desc = shot.generation_prompt.strip()
        else:
            desc = "，".join(
                x
                for x in (
                    shot.shot_type,
                    shot.scene_description,
                    shot.character_action,
                    shot.camera_movement,
                    shot.mood,
                    shot.lighting,
                )
                if x
            )
            if shot.dialogue:
                desc += f"，对白：{shot.dialogue}"
        segs.append(f"第{len(segs)+1}个镜头[{t0}-{t1}秒] {desc}")
        t0 = t1
    prompt = prefix + "。" + "。".join(segs)
    return prompt, target


def assign_generation_prompts(
    doc: StoryboardDocument,
    style: str = "",
    max_segment_seconds: float = 15.0,
    subject_descriptions: list[str] | None = None,
    api_duration_cap: int = 15,
    reference_hint: str = "",
) -> None:
    """为仍缺 generation_prompt 的镜头填入本段万相合成 prompt（与分段逻辑一致）。"""
    shots = doc.shots
    if not shots:
        return
    block = format_subject_prompt_block(list(subject_descriptions or []))
    rh = (reference_hint or "").strip()
    if rh:
        block = f"{block}。{rh}" if block.strip() else rh
    chunk_max = max(
        2.0, min(float(max_segment_seconds), float(api_duration_cap))
    )
    for chunk in chunk_shots_by_max_duration(shots, chunk_max):
        prompt, _ = build_wan_multi_shot_prompt(
            chunk, doc, style, block, max_duration=api_duration_cap
        )
        for s in chunk:
            if not s.generation_prompt.strip():
                s.generation_prompt = prompt


def generate_video_clip(
    prompt: str,
    duration: int,
    settings: Settings,
    size: str | None = None,
    watermark: bool = True,
    poll_callback: Callable[[str], None] | None = None,
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    reference_video_description: list[str] | None = None,
) -> str:
    """
    Submit async video generation and wait for result URL.
    reference_* 为万相可选参数：参考图/视频 URL 或本地路径（SDK 可自动上传）。
    """
    dashscope.base_http_api_url = settings.dashscope_api_base
    api_key = settings.dashscope_api_key

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
        prompt_extend=True,
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
) -> list[tuple[str, int]]:
    """
    按 max_segment_seconds 将分镜切段；每段一次万相调用（≤15s），最后由调用方拼接。
    若全片超过一段，会自动分为多段生成。
    """
    shots = doc.shots
    if not shots:
        raise ValueError("Storyboard has no shots")

    ref_u = [str(p) for p in (reference_urls or []) if p and str(p).strip()]
    ref_v = [str(p) for p in (reference_video_urls or []) if p and str(p).strip()]
    ref_d = [str(p) for p in (reference_video_descriptions or []) if p and str(p).strip()]
    has_refs = bool(ref_u or ref_v)
    dur_cap = generation_duration_cap(settings, has_refs)
    max_segment_seconds = max(
        2.0, min(float(max_segment_seconds), float(dur_cap), 15.0)
    )
    subject_block = format_subject_prompt_block(list(subject_descriptions or []))
    ref_hint = reference_subject_lock_hint(settings, has_refs)
    if ref_hint:
        subject_block = f"{subject_block}。{ref_hint}" if subject_block.strip() else ref_hint
    chunks = chunk_shots_by_max_duration(shots, max_segment_seconds)

    n_v, n_i = len(ref_v), len(ref_u)
    subj_list = list(subject_descriptions or [])
    v_bodies, i_bodies = extract_reference_slot_bodies(subj_list, ref_d, n_v, n_i)
    multi_ref = n_v + n_i > 1
    do_filter = bool(per_chunk_reference_filter and multi_ref and has_refs)

    total_story_sec = sum(max(0.01, float(s.duration)) for s in shots)
    if poll_callback:
        if len(chunks) > 1:
            poll_callback(
                f"分镜总长约 {total_story_sec:.1f}s，超过单次 {max_segment_seconds:.0f}s 上限，"
                f"将分为 {len(chunks)} 段依次生成后拼接。"
            )
        else:
            poll_callback(
                f"单次生成（1 段，约 {max_segment_seconds:.0f}s 内多镜头），总参考时长约 {total_story_sec:.1f}s。"
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

    tasks: list[tuple[int, str, int, list[str], list[str], list[str]]] = []
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
        else:
            sv, si = list(range(n_v)), list(range(n_i))
            sb = subject_block
        prompt, dur = build_wan_multi_shot_prompt(
            chunk, doc, style, sb, max_duration=dur_cap
        )
        cu = [ref_u[j] for j in si]
        cv = [ref_v[j] for j in sv]
        cd = [ref_d[j] for j in sv if j < len(ref_d)]
        tasks.append((i, prompt, dur, cu, cv, cd))

    results: dict[int, str] = {}

    def run_one(
        item: tuple[int, str, int, list[str], list[str], list[str]],
    ) -> tuple[int, str]:
        idx, prompt, dur, cu, cv, cd = item
        if poll_callback:
            extra = ""
            if do_filter and (len(cv) + len(cu) < n_v + n_i):
                extra = f" 本段参考 {len(cv)} 视频+{len(cu)} 图。"
            poll_callback(
                f"第 {idx + 1}/{len(tasks)} 段：生成中（API 时长 {dur}s，"
                f"镜头数 {len(chunks[idx])}）。{extra}".strip()
            )
        url = generate_video_clip(
            prompt,
            dur,
            settings,
            size=size,
            poll_callback=None,
            reference_urls=cu or None,
            reference_video_urls=cv or None,
            reference_video_description=cd or None,
        )
        return idx, url

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
