#!/usr/bin/env python3
"""
CLI：视频 analyze 或主题 theme → 分镜 JSON；generate 万相成片。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import click

from video2text.config.settings import load_settings, resolve_theme_story_model
from video2text.core.analyzer import (
    analyze_full_video_local,
    analyze_full_video_url,
    analyze_scene_segments,
    consolidate_storyboard,
)
from video2text.core.scene_detector import build_scene_segments
from video2text.core.storyboard import StoryboardDocument
from video2text.core.theme import generate_storyboard_from_theme
from video2text.pipeline.generator import (
    assign_generation_prompts,
    generation_duration_cap,
    parse_character_pool,
    reference_subject_lock_hint,
    run_storyboard_clip_generation,
)
from video2text.services.wan_video import uses_wan27_http


def _resolve_config_path(ctx: click.Context | None) -> str | None:
    c = ctx
    while c is not None:
        obj = getattr(c, "obj", None)
        if isinstance(obj, dict) and "config_path" in obj:
            return obj.get("config_path")
        c = c.parent
    return None


def _merge_generation_subjects(
    extras_subjects: tuple[str, ...],
    subjects_file: str | None,
    cli_subjects: tuple[str, ...],
) -> list[str]:
    out: list[str] = list(extras_subjects)
    if subjects_file:
        raw = Path(subjects_file).read_text(encoding="utf-8")
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    out.extend(cli_subjects)
    return [x.strip() for x in out if x.strip()]


def _merge_reference_urls(
    extras_urls: tuple[str, ...],
    cli_urls: tuple[str, ...],
    local_images: tuple[str, ...],
) -> list[str]:
    merged = list(extras_urls) + list(cli_urls)
    for p in local_images:
        merged.append(str(Path(p).expanduser().resolve()))
    return [u for u in merged if str(u).strip()]


def _merge_reference_videos(
    extras_v: tuple[str, ...],
    cli_v: tuple[str, ...],
) -> list[str]:
    out: list[str] = list(extras_v)
    for v in cli_v:
        pv = Path(v)
        out.append(str(pv.resolve()) if pv.is_file() else str(v))
    return [x for x in out if str(x).strip()]


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    envvar="V2T_CONFIG",
    help="JSON 配置文件路径；默认同目录或项目下的 config.json",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """视频/主题 → 分镜；万相生成视频。"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    try:
        import dashscope
        from video2text.config.settings import load_config_file
        cfg = load_config_file(config_path)
        dashscope.base_http_api_url = cfg.get(
            "dashscope_api_base", "https://dashscope.aliyuncs.com/api/v1"
        )
    except Exception:
        pass


@cli.command("analyze")
@click.pass_context
@click.option(
    "--input",
    "input_path",
    required=False,
    type=click.Path(exists=True, dir_okay=False),
    help="本地视频路径；与 --video-url 二选一",
)
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(),
    help="输出 storyboard .json 路径",
)
@click.option("--markdown", "md_path", type=click.Path(), help="可选：同时输出 Markdown")
@click.option(
    "--segment-scenes",
    is_flag=True,
    help="按镜头自动切片并多次调用模型（旧模式）；默认改为整片一次理解分镜",
)
@click.option(
    "--work-dir",
    type=click.Path(file_okay=False),
    help="与 --segment-scenes 联用：切片与关键帧缓存目录",
)
@click.option("--style", default="", help="风格/改编提示，会传入理解模型")
@click.option(
    "--threshold",
    default=None,
    type=float,
    help="与 --segment-scenes 联用：PySceneDetect 阈值；省略则用配置 scene_detect_threshold",
)
@click.option("--skip-consolidate", is_flag=True, help="跳过叙事整合二次调用")
@click.option(
    "--video-url",
    default=None,
    help="公网 HTTPS 视频 URL：整片一次送入模型理解分镜",
)
def cmd_analyze(
    ctx: click.Context,
    input_path: str,
    output: str,
    md_path: str | None,
    segment_scenes: bool,
    work_dir: str | None,
    style: str,
    threshold: float | None,
    skip_consolidate: bool,
    video_url: str | None,
) -> None:
    """从视频生成分镜脚本（JSON）。"""
    if not video_url and not input_path:
        raise click.UsageError("请提供 --input 本地视频，或使用 --video-url")
    if video_url and segment_scenes:
        raise click.UsageError("--segment-scenes 仅适用于本地 --input，不能与 --video-url 同时使用")
    settings = load_settings(_resolve_config_path(ctx))
    scene_threshold = (
        threshold if threshold is not None else settings.scene_detect_threshold
    )
    consolidate_flag = not skip_consolidate
    out_json = Path(output)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    if video_url:
        click.echo("整片分析（单次模型调用）…")
        doc = analyze_full_video_url(
            video_url,
            settings,
            style_hint=style,
            consolidate_result=consolidate_flag,
        )
        doc.source_video = video_url
    elif segment_scenes:
        wd = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="v2t_work_"))
        click.echo(f"场景检测与切片（多次模型调用），工作目录：{wd}")
        result = build_scene_segments(
            input_path,
            threshold=scene_threshold,
            extract_clips=True,
            extract_frames=True,
            work_dir=wd,
        )
        doc, _ = analyze_scene_segments(result.segments, settings, style_hint=style)
        doc.source_video = str(Path(input_path).resolve())
        if consolidate_flag:
            click.echo("叙事整合（二次模型调用）…")
            doc = consolidate_storyboard(doc, settings)
    else:
        click.echo("整片分析（单次模型调用，不切分场景）…")
        doc = analyze_full_video_local(
            input_path,
            settings,
            style_hint=style,
            consolidate_result=consolidate_flag,
        )
        doc.source_video = str(Path(input_path).resolve())

    doc.save_json(out_json)
    click.echo(f"已写入 {out_json}")
    if md_path:
        p = Path(md_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc.save_markdown(p)
        click.echo(f"已写入 {p}")


@cli.command("theme")
@click.pass_context
@click.option(
    "--theme",
    "theme_text",
    default=None,
    help="故事主题或创意描述（可与 --theme-file 二选一或合用：文本接在文件内容之后）",
)
@click.option(
    "--theme-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="从文件读取主题全文（UTF-8）",
)
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(),
    help="输出分镜 JSON，供 generate 使用",
)
@click.option("--markdown", "md_path", type=click.Path(), help="可选：同时输出 Markdown 分镜")
@click.option("--style", default="", help="类型/视觉风格偏好，传入创作模型")
@click.option("--min-shots", default=8, type=int, help="最少镜头数（默认 8）")
@click.option("--max-shots", default=24, type=int, help="最多镜头数（默认 24）")
@click.option(
    "--model",
    default=None,
    help="文本模型名；省略则必须已在配置中填写 theme_story_model",
)
def cmd_theme(
    ctx: click.Context,
    theme_text: str | None,
    theme_file: str | None,
    output: str,
    md_path: str | None,
    style: str,
    min_shots: int,
    max_shots: int,
    model: str | None,
) -> None:
    """根据主题文本由大模型创作故事，输出详细分镜与角色对白（JSON），再接 generate 生成视频。"""
    parts: list[str] = []
    if theme_file:
        parts.append(Path(theme_file).read_text(encoding="utf-8").strip())
    if theme_text:
        parts.append(theme_text.strip())
    combined = "\n\n".join(p for p in parts if p)
    if not combined:
        raise click.UsageError("请提供 --theme 和/或 --theme-file")

    settings = load_settings(_resolve_config_path(ctx))
    out_json = Path(output)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    try:
        display_model = resolve_theme_story_model(settings, override=model)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    click.echo(
        f"主题创作中（模型：{display_model}，镜头 {min_shots}～{max_shots}）\n"
        f"  Phase 1：生成故事大纲…  Phase 2：设计分镜…"
    )
    doc = generate_storyboard_from_theme(
        combined,
        settings,
        style_hint=style,
        min_shots=min_shots,
        max_shots=max_shots,
        model=model,
    )
    doc.save_json(out_json)
    click.echo(f"已写入 {out_json}（共 {len(doc.shots)} 镜，含对白字段 dialogue）")
    click.echo("下一步：v2t generate --storyboard ... --output ...（并准备好参考图/视频）")
    if md_path:
        p = Path(md_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc.save_markdown(p)
        click.echo(f"已写入 {p}")


@cli.command("generate")
@click.pass_context
@click.option("--storyboard", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(),
    help="最终拼接视频路径 (.mp4)",
)
@click.option("--style", default="", help="生成时的额外风格提示")
@click.option("--resolution", default=None, help="如 1280*720，默认取环境变量或 config")
@click.option("--workers", default=2, type=int, help="并行生成任务数")
@click.option("--update-storyboard/--no-update-storyboard", default=False)
@click.option(
    "--subject",
    multiple=True,
    help="主体文字设定（可多次传入），写入每段生成 prompt，保证全片一致",
)
@click.option(
    "--subjects-file",
    type=click.Path(exists=True, dir_okay=False),
    help="每行一条主体描述，# 开头为注释",
)
@click.option(
    "--reference-url",
    multiple=True,
    help="参考图 HTTPS URL，可多次传入；与配置、本地图按顺序合并为图1、图2…",
)
@click.option(
    "--reference-image",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help="本地参考图路径，可多次传入；每张图尽量单角色，顺序对应万相「图1」「图2」",
)
@click.option(
    "--reference-video",
    multiple=True,
    help="参考视频 URL 或本地路径（万相 reference_video_urls）",
)
@click.option(
    "--reference-video-desc",
    multiple=True,
    help="与 --reference-video 顺序一一对应的画面/声音说明（数量须一致）",
)
@click.option(
    "--max-segment-seconds",
    default=None,
    type=float,
    help="每段累计分镜时长上限；文生≤15s，参考生≤10s（可写 config max_segment_seconds）",
)
@click.option(
    "--require-reference",
    is_flag=True,
    default=False,
    help="在配置已关闭 require_reference 时，仍强制要求参考素材（参考生）",
)
@click.option(
    "--no-require-reference",
    is_flag=True,
    default=False,
    help="在 config 已设 require_reference=false 时，本次允许无参考走文生（t2v）",
)
@click.option(
    "--text-only-video",
    is_flag=True,
    default=False,
    help="纯文生视频（万相 t2v）：不要求参考图/视频，一条命令即可，无需改 config",
)
def cmd_generate(
    ctx: click.Context,
    storyboard: str,
    output: str,
    style: str,
    resolution: str | None,
    workers: int,
    update_storyboard: bool,
    subject: tuple[str, ...],
    subjects_file: str | None,
    reference_url: tuple[str, ...],
    reference_image: tuple[str, ...],
    reference_video: tuple[str, ...],
    reference_video_desc: tuple[str, ...],
    max_segment_seconds: float | None,
    require_reference: bool,
    no_require_reference: bool,
    text_only_video: bool,
) -> None:
    """根据分镜 JSON 调用万相生成并拼接视频（默认必须参考生 r2v）。"""
    sb_p = Path(storyboard)
    if sb_p.suffix.lower() in (".md", ".markdown"):
        raise click.UsageError(
            "--storyboard 必须是 JSON 分镜（例如 theme -o my_story.json 生成的文件）。"
            ".md 仅供阅读，不能用于 generate。"
        )
    cfg_path = _resolve_config_path(ctx)
    settings = load_settings(cfg_path)
    max_seg = (
        float(max_segment_seconds)
        if max_segment_seconds is not None
        else settings.max_segment_seconds
    )
    subjects_merged = _merge_generation_subjects(
        (), subjects_file, subject
    )
    ref_urls_merged = _merge_reference_urls(
        (), reference_url, reference_image
    )
    ref_videos_merged = _merge_reference_videos(
        (), reference_video
    )
    ref_desc_merged = list(reference_video_desc)
    if ref_videos_merged and ref_desc_merged and len(ref_videos_merged) != len(
        ref_desc_merged
    ):
        raise click.UsageError(
            "参考视频数量与 --reference-video-desc 不一致时，万相可能报错；"
            "请保证两者个数相同，或暂时去掉描述/视频。"
        )

    if require_reference and no_require_reference:
        raise click.UsageError(
            "不能同时使用 --require-reference 与 --no-require-reference。"
        )
    if text_only_video and require_reference:
        raise click.UsageError(
            "不能同时使用 --text-only-video 与 --require-reference。"
        )
    has_refs = bool(ref_urls_merged or ref_videos_merged)
    if text_only_video:
        need_ref = False
    else:
        allow_text_only_without_refs = (
            no_require_reference and not settings.require_reference
        )
        need_ref = (not allow_text_only_without_refs) or require_reference
    if need_ref and not has_refs:
        raise click.UsageError(
            "当前须使用参考生视频（r2v）：请配置 reference_urls / reference_video_urls，"
            "或使用 --reference-image / --reference-url / --reference-video 提供至少一类参考素材。"
            "若确需无参考的文生（t2v），请加 --text-only-video（推荐），"
            "或 config 设 \"require_reference\": false 且加 --no-require-reference。"
        )
    dur_cap = generation_duration_cap(settings, has_refs)
    max_seg_eff = max(2.0, min(max_seg, float(dur_cap)))
    ref_hint = reference_subject_lock_hint(settings, has_refs)

    if has_refs:
        n_v, n_i = len(ref_videos_merged), len(ref_urls_merged)
        if uses_wan27_http(settings.video_ref_model):
            parts = []
            if n_v:
                parts.append(f"{n_v} 个参考视频 → 视频1～视频{n_v}")
            if n_i:
                parts.append(f"{n_i} 张参考图 → 图1～图{n_i}")
            click.echo(
                "参考素材编号（wan2.7-r2v，与官方文档一致）："
                + ("；".join(parts) if parts else "（无）")
                + "。请在 subject / 分镜 prompt 中用上述序号指代角色。"
            )
        else:
            parts = []
            if n_v:
                parts.append(f"{n_v} 个参考视频")
            if n_i:
                parts.append(f"{n_i} 张参考图")
            click.echo(
                "参考素材已按接口顺序传入（wan2.6-r2v 请在 prompt 中用 character1、character2… 指代）："
                + "；".join(parts)
            )

    char_pool = None
    if text_only_video and subjects_merged:
        char_pool = parse_character_pool(subjects_merged)
        if char_pool:
            click.echo(f"角色池已解析：{', '.join(e.name for e in char_pool)}（共 {len(char_pool)} 个角色）")

    doc = StoryboardDocument.load_json(storyboard)
    assign_generation_prompts(
        doc,
        style,
        max_segment_seconds=max_seg_eff,
        subject_descriptions=subjects_merged,
        api_duration_cap=dur_cap,
        reference_hint=ref_hint,
        character_pool=char_pool,
        settings=settings,
    )
    if update_storyboard:
        Path(storyboard).write_text(
            json.dumps(doc.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if has_refs:
        if (
            settings.per_chunk_reference_filter
            and len(ref_videos_merged) + len(ref_urls_merged) > 1
        ):
            click.echo(
                f"生成模式：参考生视频，模型 {settings.video_ref_model}"
                f"（多参考时各段按分镜文案自动选参考子集，单次最长约 {dur_cap}s）。"
            )
        else:
            click.echo(
                f"生成模式：参考生视频，模型 {settings.video_ref_model}"
                f"（每段携带同一批参考，单次最长约 {dur_cap}s）。"
            )
    else:
        click.echo(
            f"生成模式：文生视频，模型 {settings.video_gen_model}（无参考图/视频，主体仅靠文字，跨段一致性弱于 r2v）。"
        )
        if subjects_merged and not char_pool:
            click.echo(
                "提示：当前为文生模式；默认流程应为参考生。若需锁定长相/造型，请提供参考图/视频并去掉 --no-require-reference。"
            )
    click.echo("提交万相生成任务（可能需数分钟）…")

    def _cb(msg: str) -> None:
        click.echo(f"  {msg}")

    out = run_storyboard_clip_generation(
        doc,
        settings,
        style=style,
        size=resolution,
        max_segment_seconds=max_seg_eff,
        subject_descriptions=subjects_merged,
        reference_urls=ref_urls_merged,
        reference_video_urls=ref_videos_merged,
        reference_video_descriptions=ref_desc_merged,
        per_chunk_reference_filter=settings.per_chunk_reference_filter,
        character_pool=char_pool,
        progress_callback=_cb,
        checkpoint_dir=None,
        output_video=Path(output),
        meta_update=None,
        max_workers=workers,
    )
    click.echo(f"完成：{out.resolve()}")


@cli.command("run")
@click.pass_context
@click.option(
    "--input",
    "input_path",
    required=False,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(),
    help="最终视频 .mp4",
)
@click.option("--style", default="", help="理解与生成共用风格提示（主题模式仅用于创作与生成）")
@click.option(
    "--threshold",
    default=None,
    type=float,
    help="场景检测阈值；省略则使用配置文件",
)
@click.option("--work-dir", type=click.Path(file_okay=False))
@click.option("--resolution", default=None)
@click.option("--workers", default=2, type=int)
@click.option("--video-url", default=None, help="公网视频 URL 时与 analyze 行为一致")
@click.option(
    "--theme",
    "run_theme_text",
    default=None,
    help="与 --input 二选一：仅主题创作分镜再生成（跳过 analyze）",
)
@click.option(
    "--theme-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="主题全文文件 UTF-8；可与 --theme 合用",
)
@click.option("--min-shots", default=8, type=int, help="仅 --theme 模式：最少镜头数")
@click.option("--max-shots", default=24, type=int, help="仅 --theme 模式：最多镜头数")
@click.option(
    "--theme-model",
    default=None,
    help="仅 --theme 模式：覆盖 theme_story_model；省略则使用配置中的 theme_story_model",
)
@click.option(
    "--segment-scenes",
    is_flag=True,
    help="本地视频按镜头切片并多次调用模型（与 analyze 一致）",
)
@click.option("--subject", multiple=True, help="同 generate：主体文字，可多个")
@click.option("--subjects-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--reference-url", multiple=True)
@click.option(
    "--reference-image",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option("--reference-video", multiple=True)
@click.option("--reference-video-desc", multiple=True)
@click.option("--max-segment-seconds", default=None, type=float)
@click.option(
    "--require-reference",
    is_flag=True,
    default=False,
    help="同 generate",
)
@click.option(
    "--no-require-reference",
    is_flag=True,
    default=False,
    help="同 generate",
)
@click.option(
    "--text-only-video",
    is_flag=True,
    default=False,
    help="同 generate：纯文生 t2v，不要参考图",
)
@click.option("--keep-storyboard", type=click.Path(), help="保存中间分镜 JSON 的路径")
def cmd_run(
    ctx: click.Context,
    input_path: str,
    output: str,
    style: str,
    threshold: float | None,
    work_dir: str | None,
    resolution: str | None,
    workers: int,
    video_url: str | None,
    run_theme_text: str | None,
    theme_file: str | None,
    min_shots: int,
    max_shots: int,
    theme_model: str | None,
    segment_scenes: bool,
    subject: tuple[str, ...],
    subjects_file: str | None,
    reference_url: tuple[str, ...],
    reference_image: tuple[str, ...],
    reference_video: tuple[str, ...],
    reference_video_desc: tuple[str, ...],
    max_segment_seconds: float | None,
    require_reference: bool,
    no_require_reference: bool,
    text_only_video: bool,
    keep_storyboard: str | None,
) -> None:
    """视频：analyze + generate；或主题：theme + generate 一步。"""
    out = Path(output)
    sb_path = (
        Path(keep_storyboard)
        if keep_storyboard
        else out.with_suffix(".storyboard.json")
    )
    sb_path.parent.mkdir(parents=True, exist_ok=True)

    theme_parts: list[str] = []
    if theme_file:
        theme_parts.append(Path(theme_file).read_text(encoding="utf-8").strip())
    if run_theme_text:
        theme_parts.append(run_theme_text.strip())
    theme_combined = "\n\n".join(p for p in theme_parts if p)
    theme_mode = bool(theme_combined)

    if theme_mode:
        if video_url or input_path:
            raise click.UsageError(
                "已指定主题（--theme / --theme-file）时，不要同时使用 --input 或 --video-url。"
            )
        settings = load_settings(_resolve_config_path(ctx))
        try:
            display_model = resolve_theme_story_model(settings, override=theme_model)
        except ValueError as e:
            raise click.UsageError(str(e)) from e
        click.echo(
            f"主题 → 分镜（模型 {display_model}，"
            f"镜头 {min_shots}～{max_shots}）\n"
            f"  Phase 1：生成故事大纲…  Phase 2：设计分镜…"
        )
        doc = generate_storyboard_from_theme(
            theme_combined,
            settings,
            style_hint=style,
            min_shots=min_shots,
            max_shots=max_shots,
            model=theme_model,
        )
        doc.save_json(sb_path)
        click.echo(f"已写入分镜 {sb_path}（{len(doc.shots)} 镜）")
    else:
        if not video_url and not input_path:
            raise click.UsageError(
                "请提供 --input 本地视频、或 --video-url、或使用 --theme / --theme-file 走主题流程。"
            )
        ctx.invoke(
            cmd_analyze,
            input_path=input_path,
            output=str(sb_path),
            md_path=None,
            segment_scenes=segment_scenes,
            work_dir=work_dir,
            style=style,
            threshold=threshold,
            skip_consolidate=False,
            video_url=video_url,
        )

    ctx.invoke(
        cmd_generate,
        storyboard=str(sb_path),
        output=str(out),
        style=style,
        resolution=resolution,
        workers=workers,
        update_storyboard=False,
        subject=subject,
        subjects_file=subjects_file,
        reference_url=reference_url,
        reference_image=reference_image,
        reference_video=reference_video,
        reference_video_desc=reference_video_desc,
        max_segment_seconds=max_segment_seconds,
        require_reference=require_reference,
        no_require_reference=no_require_reference,
        text_only_video=text_only_video,
    )


# ---------------------------------------------------------------------------
# IP 子命令组
# ---------------------------------------------------------------------------


@cli.group("ip")
@click.pass_context
def ip_group(ctx: click.Context) -> None:
    """IP 模式：创建/管理 IP，基于 IP 生成故事与视频。"""
    pass


@ip_group.command("create")
@click.pass_context
@click.option("--seed", required=True, help="种子创意描述（如'胖猫搞笑日常'）")
@click.option("--style", "style_id", default="", help="风格预设 ID（可选，如 cartoon_3d_cute）")
@click.option("--user", "username", default="admin", help="用户名")
@click.option("--no-images", is_flag=True, help="跳过角色图生成")
@click.option("-o", "--output", "output_path", type=click.Path(), help="IP JSON 输出路径（可选）")
def ip_create(
    ctx: click.Context,
    seed: str,
    style_id: str,
    username: str,
    no_images: bool,
    output_path: str | None,
) -> None:
    """从种子创意创建新 IP。"""
    from video2text.core.ip_creator import (
        create_ip_from_proposal,
        generate_character_images,
        generate_ip_proposal,
    )

    config_path = _resolve_config_path(ctx)
    settings = load_settings(config_path)

    click.echo(f"正在从种子创意生成 IP 提案…")
    proposal = generate_ip_proposal(seed, settings, style_preset_id=style_id)
    click.echo(f"IP 提案：{proposal.get('name', '')} — {proposal.get('tagline', '')}")
    click.echo(f"角色数量：{len(proposal.get('characters', []))}")

    profile = create_ip_from_proposal(proposal, username)
    click.echo(f"IP 已创建：{profile.id}")

    if not no_images:
        click.echo("正在生成角色参考图…")
        profile = generate_character_images(
            profile, username, settings,
            progress_cb=lambda m: click.echo(f"  {m}"),
        )

    if output_path:
        import json as _json
        Path(output_path).write_text(
            _json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        click.echo(f"IP JSON 已保存到 {output_path}")

    click.echo(f"完成！IP ID: {profile.id}, 名称: {profile.name}")


@ip_group.command("list")
@click.option("--user", "username", default="admin", help="用户名")
def ip_list(username: str) -> None:
    """列出所有 IP。"""
    from video2text.core.ip_manager import list_ips

    ips = list_ips(username)
    if not ips:
        click.echo("暂无 IP。")
        return
    for ip in ips:
        chars = ", ".join(c.name for c in ip.characters)
        click.echo(f"  {ip.id}  {ip.name} ({ip.name_en}) — {ip.tagline}")
        click.echo(f"         角色: {chars}")
        click.echo(f"         风格: {ip.visual_dna.style_preset_id}")


@ip_group.command("show")
@click.argument("ip_id")
@click.option("--user", "username", default="admin", help="用户名")
def ip_show(ip_id: str, username: str) -> None:
    """查看 IP 详情。"""
    from video2text.core.ip_manager import load_ip

    ip = load_ip(username, ip_id)
    if not ip:
        click.echo(f"IP {ip_id} 不存在。")
        raise SystemExit(1)

    click.echo(f"ID: {ip.id}")
    click.echo(f"名称: {ip.name} ({ip.name_en})")
    click.echo(f"标语: {ip.tagline}")
    click.echo(f"风格: {ip.visual_dna.style_preset_id} — {ip.visual_dna.style_keywords}")
    click.echo(f"类型: {ip.story_dna.genre}")
    click.echo(f"叙事模式: {ip.story_dna.narrative_pattern}")
    click.echo(f"世界设定: {ip.world_dna.primary_setting}")
    click.echo(f"\n角色 ({len(ip.characters)}):")
    for c in ip.characters:
        ref = "✓" if c.reference_image_path else "✗"
        click.echo(f"  [{ref}] {c.name} ({c.name_en}) — {c.role}")
        click.echo(f"       {c.visual_description[:80]}…")


@ip_group.command("regen-image")
@click.argument("ip_id")
@click.option("--char", "char_id", default=None, help="角色 ID（不指定则重新生成所有角色）")
@click.option("--user", "username", default="admin", help="用户名")
@click.pass_context
def ip_regen_image(ctx: click.Context, ip_id: str, char_id: str | None, username: str) -> None:
    """重新生成角色参考图。"""
    from video2text.core.ip_creator import generate_character_images
    from video2text.core.ip_manager import load_ip

    ip = load_ip(username, ip_id)
    if not ip:
        click.echo(f"IP {ip_id} 不存在。")
        raise SystemExit(1)

    config_path = _resolve_config_path(ctx)
    settings = load_settings(config_path)
    char_ids = [char_id] if char_id else [c.id for c in ip.characters]

    ip = generate_character_images(
        ip, username, settings, char_ids=char_ids,
        progress_cb=lambda m: click.echo(f"  {m}"),
    )
    click.echo("角色图生成完成。")


@ip_group.command("theme")
@click.argument("ip_id")
@click.option("--hint", default="", help="本期主题提示（可选，如'阿肥减肥'）")
@click.option("--user", "username", default="admin", help="用户名")
@click.option("-o", "--output", required=True, type=click.Path(), help="分镜 JSON 输出路径")
@click.option("--markdown", "md_path", type=click.Path(), help="可选：同时输出 Markdown")
@click.option("--min-shots", default=8, type=int, help="最少镜头数")
@click.option("--max-shots", default=16, type=int, help="最多镜头数")
@click.pass_context
def ip_theme(
    ctx: click.Context,
    ip_id: str,
    hint: str,
    username: str,
    output: str,
    md_path: str | None,
    min_shots: int,
    max_shots: int,
) -> None:
    """基于 IP 生成本期故事分镜。"""
    from video2text.core.ip_manager import load_ip
    from video2text.core.theme import generate_storyboard_from_ip

    ip = load_ip(username, ip_id)
    if not ip:
        click.echo(f"IP {ip_id} 不存在。")
        raise SystemExit(1)

    config_path = _resolve_config_path(ctx)
    settings = load_settings(config_path)

    click.echo(f"正在为 IP '{ip.name}' 生成故事分镜…")
    doc = generate_storyboard_from_ip(
        ip, settings,
        theme_hint=hint,
        min_shots=min_shots,
        max_shots=max_shots,
    )
    doc.save_json(output)
    click.echo(f"分镜已保存到 {output}（{len(doc.shots)} 个镜头）")
    if md_path:
        doc.save_markdown(md_path)
        click.echo(f"Markdown 已保存到 {md_path}")


@ip_group.command("generate")
@click.argument("ip_id")
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path(), help="输出视频路径")
@click.option("--user", "username", default="admin", help="用户名")
@click.option("--max-workers", default=2, type=int, help="并发生成数")
@click.pass_context
def ip_generate(
    ctx: click.Context,
    ip_id: str,
    storyboard_json: str,
    output: str,
    username: str,
    max_workers: int,
) -> None:
    """基于 IP 分镜生成视频（自动注入角色参考图）。"""
    from video2text.core.ip_manager import load_ip
    from video2text.pipeline.generator import run_ip_storyboard_generation

    ip = load_ip(username, ip_id)
    if not ip:
        click.echo(f"IP {ip_id} 不存在。")
        raise SystemExit(1)

    config_path = _resolve_config_path(ctx)
    settings = load_settings(config_path)
    doc = StoryboardDocument.load_json(storyboard_json)

    out_path = Path(output)
    seg_dir = out_path.parent / f"{out_path.stem}_segments"

    click.echo(f"正在为 IP '{ip.name}' 生成视频…")
    run_ip_storyboard_generation(
        doc, ip, settings,
        segments_dir=seg_dir,
        output_mp4=out_path,
        progress_cb=lambda m: click.echo(f"  {m}"),
        max_workers=max_workers,
    )
    click.echo(f"视频已保存到 {output}")


@ip_group.command("styles")
@click.option("--search", "query", default="", help="搜索关键词")
def ip_styles(query: str) -> None:
    """列出可用的风格预设。"""
    from video2text.core.styles import get_all_style_presets, search_styles

    if query:
        results = search_styles(query)
        if not results:
            click.echo(f"未找到匹配 '{query}' 的风格。")
            return
        for s in results:
            click.echo(f"  {s['id']:25s} {s['name_zh']} ({s['name_en']})")
            click.echo(f"  {'':25s} {s['description_zh']}")
    else:
        presets = get_all_style_presets()
        for cat in presets:
            click.echo(f"\n[{cat['category_zh']}]")
            for s in cat["styles"]:
                click.echo(f"  {s['id']:25s} {s['name_zh']} — {s['description_zh']}")


if __name__ == "__main__":
    cli()
