#!/usr/bin/env python3
"""
video2text Web UI — Flask 后端：配置、任务、工作区缓存与断点续传。
本地运行：v2t-web 或 python -m video2text.web.app → http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_from_directory

from openai import OpenAI

from video2text.config.settings import load_generation_extras, load_settings
from video2text.utils.paths import (
    get_config_example_path,
    get_default_config_path,
    get_static_dir,
    get_workspace_dir,
)
from video2text.core.analyzer import (
    analyze_full_video_local,
    analyze_full_video_url,
    analyze_scene_segments,
    consolidate_storyboard,
)
from video2text.core.scene_detector import build_scene_segments
from video2text.core.storyboard import StoryboardDocument
from video2text.core.theme import generate_next_shot, generate_storyboard_from_theme
from video2text.pipeline.generator import (
    CancellationError,
    assign_generation_prompts,
    generation_duration_cap,
    parse_character_pool,
    reference_subject_lock_hint,
    run_storyboard_clip_generation,
)

from video2text.web.auth import init_auth

WORKSPACE = get_workspace_dir()
STATIC = get_static_dir()
CONFIG_PATH = get_default_config_path()
CONFIG_EXAMPLE = get_config_example_path()

# 任务自动清理：超过 N 天未更新的任务目录将在下次启动时清理
TASK_TTL_DAYS = 7

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB uploads

init_auth(app)

_tasks_lock = threading.Lock()
_active_threads: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}

# SSE 事件队列：task_id -> list of subscriber queues
_sse_queues: dict[str, list[queue.Queue]] = {}
_sse_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_workspace() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)


def _task_dir(task_id: str) -> Path:
    return WORKSPACE / task_id


def _read_task_meta(task_id: str) -> dict[str, Any]:
    p = _task_dir(task_id) / "task.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _write_task_meta(task_id: str, data: dict[str, Any]) -> None:
    d = _task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    cur = {}
    if p.is_file():
        cur = json.loads(p.read_text(encoding="utf-8"))
    cur.update(data)
    cur["updated"] = _iso_now()
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_progress(task_id: str, msg: str) -> None:
    with _tasks_lock:
        meta = _read_task_meta(task_id)
        prog = list(meta.get("progress", []))
        prog.append({"t": _iso_now(), "msg": msg})
        meta["progress"] = prog[-500:]
        _write_task_meta(task_id, meta)
    # 推送 SSE 事件
    _sse_push(task_id, {"type": "progress", "msg": msg, "t": _iso_now()})


def _sse_push(task_id: str, event: dict[str, Any]) -> None:
    with _sse_lock:
        qs = _sse_queues.get(task_id, [])
        for q in qs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "" if not k else "****"
    return k[:4] + "…" + k[-4:]


def _get_config_for_api() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        if CONFIG_EXAMPLE.is_file():
            return json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config_file(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _cleanup_old_tasks() -> None:
    """清理超过 TASK_TTL_DAYS 天未更新的任务目录。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TASK_TTL_DAYS)
    if not WORKSPACE.is_dir():
        return
    for d in WORKSPACE.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "task.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            updated_str = meta.get("updated", "")
            if updated_str:
                updated = datetime.fromisoformat(updated_str)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 路由：静态页与配置
# ---------------------------------------------------------------------------

@app.route("/")
def index_page():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = _get_config_for_api()
    safe = dict(cfg)
    if "dashscope_api_key" in safe and safe["dashscope_api_key"]:
        safe["dashscope_api_key_masked"] = _mask_key(str(safe["dashscope_api_key"]))
        if len(str(safe["dashscope_api_key"])) > 8:
            safe["dashscope_api_key"] = ""
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    body = request.get_json(force=True, silent=True) or {}
    cur = _get_config_for_api()
    for k, v in body.items():
        if k == "dashscope_api_key" and (v is None or str(v).strip() == ""):
            continue
        cur[k] = v
    _save_config_file(cur)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 路由：任务管理
# ---------------------------------------------------------------------------

@app.route("/api/task/create", methods=["POST"])
def api_task_create():
    _ensure_workspace()
    task_id = uuid.uuid4().hex[:12]
    d = _task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "references").mkdir(exist_ok=True)
    (d / "segments").mkdir(exist_ok=True)
    _write_task_meta(
        task_id,
        {
            "task_id": task_id,
            "status": "created",
            "created": _iso_now(),
            "progress": [],
            "type": "pending",
            "params": {},
        },
    )
    return jsonify({"task_id": task_id})


@app.route("/api/upload/reference", methods=["POST"])
def api_upload_reference():
    task_id = request.form.get("task_id") or ""
    if not task_id or not _task_dir(task_id).is_dir():
        return jsonify({"error": "invalid task_id"}), 400
    ref_dir = _task_dir(task_id) / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, str]] = []
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files"}), 400
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower() or ".bin"
        name = f"ref_{len(list(ref_dir.glob('ref_*')))}_{i}{ext}"
        dest = ref_dir / name
        f.save(str(dest))
        kind = "video" if ext in (".mp4", ".webm", ".mov", ".avi") else "image"
        saved.append(
            {
                "path": str(dest.resolve()),
                "name": name,
                "kind": kind,
            }
        )
    return jsonify({"files": saved})


# ---------------------------------------------------------------------------
# 路由：preflight 配置校验
# ---------------------------------------------------------------------------

@app.route("/api/task/preflight", methods=["POST"])
def api_task_preflight():
    """生成前的快速配置校验，不做任何 API 调用。"""
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")

    issues: list[str] = []
    cfg = _get_config_for_api()

    if not cfg.get("dashscope_api_key"):
        issues.append("未配置 dashscope_api_key，请在设置中填写")

    if not cfg.get("video_gen_model") and not cfg.get("video_ref_model"):
        issues.append("未配置视频生成模型（video_gen_model / video_ref_model）")

    if task_id:
        td = _task_dir(task_id)
        if not (td / "storyboard.json").is_file():
            issues.append("尚无分镜文件，请先完成步骤 1 生成分镜")

    if issues:
        return jsonify({"ok": False, "issues": issues}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 后台任务函数
# ---------------------------------------------------------------------------

def _run_theme_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "theme_running", "type": "theme"})
        _sse_push(task_id, {"type": "status", "status": "theme_running"})
        log("Storyboard generation started, loading config…")
        settings = load_settings(str(CONFIG_PATH))
        theme = (params.get("theme") or "").strip()
        if not theme:
            raise ValueError("主题为空")
        use_model = params.get("model") or settings.theme_story_model or settings.vision_model
        min_shots = int(params.get("min_shots") or 8)
        max_shots = int(params.get("max_shots") or 24)
        log(f"Calling LLM ({use_model}) to generate {min_shots}–{max_shots} shots… (this may take 15–60s)")
        doc = generate_storyboard_from_theme(
            theme,
            settings,
            style_hint=str(params.get("style") or ""),
            min_shots=min_shots,
            max_shots=max_shots,
            model=params.get("model") or None,
        )
        td = _task_dir(task_id)
        doc.save_json(td / "storyboard.json")
        doc.save_markdown(td / "storyboard.md")
        style_used = str(params.get("style") or "").strip()
        _write_task_meta(
            task_id,
            {
                "status": "storyboard_ready",
                "shot_count": len(doc.shots),
                "style": style_used,
            },
        )
        _sse_push(task_id, {
            "type": "status",
            "status": "storyboard_ready",
            "shot_count": len(doc.shots),
            "style": style_used,
        })
        log(f"Storyboard saved: {len(doc.shots)} shots → {td / 'storyboard.json'}")
        # 自动生成主体描述
        log("Generating character subject descriptions…")
        try:
            subjects = _generate_subjects_from_storyboard(doc, settings)
            if subjects:
                _write_subjects(task_id, subjects)
                _sse_push(task_id, {"type": "subjects_ready", "count": len(subjects), "subjects": subjects})
                log(f"Subject descriptions generated: {len(subjects)} characters.")
        except Exception as se:
            log(f"Subject generation skipped: {se}")
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        _sse_push(task_id, {"type": "status", "status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _run_analyze_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "analyze_running", "type": "analyze"})
        _sse_push(task_id, {"type": "status", "status": "analyze_running"})
        log("Video analysis started, loading config…")
        settings = load_settings(str(CONFIG_PATH))
        td = _task_dir(task_id)
        consolidate_flag = not params.get("skip_consolidate")
        style = str(params.get("style") or "")

        video_path = params.get("video_path")
        video_url = params.get("video_url")

        if video_url:
            log(f"Analyzing full video via URL (this may take 30–120s)… {video_url[:80]}")
            doc = analyze_full_video_url(
                video_url,
                settings,
                style_hint=style,
                consolidate_result=consolidate_flag,
            )
            doc.source_video = video_url
        elif video_path and Path(video_path).is_file():
            vp = Path(video_path)
            if params.get("segment_scenes"):
                log(f"Scene detection on: {vp.name}")
                wd = td / "analyze_work"
                wd.mkdir(parents=True, exist_ok=True)
                thr = params.get("threshold")
                if thr is None:
                    thr = settings.scene_detect_threshold
                result = build_scene_segments(
                    str(vp),
                    threshold=float(thr),
                    extract_clips=True,
                    extract_frames=True,
                    work_dir=wd,
                )
                log(f"Detected {len(result.segments)} scenes, calling vision LLM for each… (may take 1–3 min)")
                doc, _ = analyze_scene_segments(result.segments, settings, style_hint=style)
                doc.source_video = str(vp.resolve())
                if consolidate_flag:
                    log("Consolidating narrative across scenes…")
                    doc = consolidate_storyboard(doc, settings)
            else:
                log(f"Analyzing full video (local): {vp.name} — calling vision LLM (may take 30–120s)…")
                doc = analyze_full_video_local(
                    str(vp),
                    settings,
                    style_hint=style,
                    consolidate_result=consolidate_flag,
                )
                doc.source_video = str(vp.resolve())
        else:
            raise ValueError("缺少视频文件或 video_url")

        doc.save_json(td / "storyboard.json")
        doc.save_markdown(td / "storyboard.md")
        style_used = str(params.get("style") or "").strip()
        _write_task_meta(
            task_id,
            {
                "status": "storyboard_ready",
                "shot_count": len(doc.shots),
                "style": style_used,
            },
        )
        _sse_push(task_id, {
            "type": "status",
            "status": "storyboard_ready",
            "shot_count": len(doc.shots),
            "style": style_used,
        })
        log(f"Storyboard saved: {len(doc.shots)} shots → {td / 'storyboard.json'}")
        # 自动生成主体描述
        log("Generating character subject descriptions…")
        try:
            subjects = _generate_subjects_from_storyboard(doc, settings)
            if subjects:
                _write_subjects(task_id, subjects)
                _sse_push(task_id, {"type": "subjects_ready", "count": len(subjects), "subjects": subjects})
                log(f"Subject descriptions generated: {len(subjects)} characters.")
        except Exception as se:
            log(f"Subject generation skipped: {se}")
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        _sse_push(task_id, {"type": "status", "status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _run_generate_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    cancel_event = _cancel_flags.get(task_id)

    try:
        td = _task_dir(task_id)
        sb = td / "storyboard.json"
        if not sb.is_file():
            raise FileNotFoundError("无 storyboard.json，请先生成分镜")

        _write_task_meta(task_id, {"status": "generating"})
        _sse_push(task_id, {"type": "status", "status": "generating"})
        settings = load_settings(str(CONFIG_PATH))
        extras = load_generation_extras(str(CONFIG_PATH))

        doc = StoryboardDocument.load_json(sb)
        text_only = bool(params.get("text_only_video"))

        ref_images = [str(x) for x in (params.get("reference_images") or []) if x]
        ref_videos = [str(x) for x in (params.get("reference_videos") or []) if x]
        ref_video_descs = [
            str(x) for x in (params.get("reference_video_descriptions") or []) if x
        ]
        subject_lines = [str(x).strip() for x in (params.get("subject_lines") or []) if str(x).strip()]

        char_pool = None
        if text_only:
            ref_images, ref_videos = [], []
            ref_video_descs = []
            char_pool = parse_character_pool(subject_lines)
            if char_pool:
                log(f"角色池已解析：{', '.join(e.name for e in char_pool)}（共 {len(char_pool)} 个角色）")
        else:
            if not ref_images and not ref_videos and extras.require_reference:
                raise ValueError(
                    "参考生需要上传参考图或视频，或勾选「纯文生（无参考）」"
                )

        if ref_videos and ref_video_descs and len(ref_videos) != len(ref_video_descs):
            raise ValueError("参考视频数量与视频说明数量须一致")

        max_seg = float(
            params.get("max_segment_seconds") or extras.max_segment_seconds
        )
        # 优先用参数里的 style，其次用任务元数据里已保存的统一风格
        meta_style = str(_read_task_meta(task_id).get("style") or "")
        style = str(params.get("style") or meta_style or "")
        resolution = params.get("resolution") or None
        max_workers = int(params.get("max_workers") or 2)

        has_refs = bool(ref_images or ref_videos)
        dur_cap = generation_duration_cap(settings, has_refs)
        max_seg_eff = max(2.0, min(max_seg, float(dur_cap)))

        ref_hint = reference_subject_lock_hint(settings, has_refs)
        assign_generation_prompts(
            doc,
            style,
            max_segment_seconds=max_seg_eff,
            subject_descriptions=subject_lines,
            api_duration_cap=dur_cap,
            reference_hint=ref_hint,
            character_pool=char_pool,
            settings=settings,
        )
        sb.write_text(
            json.dumps(doc.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log(
            f"模式：{'文生 t2v' if not has_refs else '参考生 r2v ' + settings.video_ref_model}"
        )
        log(f"并发数：{max_workers}")

        td = _task_dir(task_id)
        run_storyboard_clip_generation(
            doc,
            settings,
            style=style,
            size=resolution,
            max_segment_seconds=max_seg_eff,
            subject_descriptions=subject_lines,
            reference_urls=ref_images,
            reference_video_urls=ref_videos,
            reference_video_descriptions=ref_video_descs,
            per_chunk_reference_filter=extras.per_chunk_reference_filter,
            character_pool=char_pool,
            progress_callback=log,
            checkpoint_dir=td / "segments",
            output_video=td / "output.mp4",
            meta_update=lambda d: _write_task_meta(task_id, d),
            max_workers=max_workers,
            cancel_event=cancel_event,
        )

        _write_task_meta(task_id, {"status": "done"})
        _sse_push(task_id, {"type": "status", "status": "done"})
    except CancellationError:
        _write_task_meta(task_id, {"status": "cancelled", "error": "用户已取消，已生成的片段可继续使用"})
        _sse_push(task_id, {"type": "status", "status": "cancelled"})
        log("任务已取消，已生成的片段缓存保留，可继续生成。")
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        _sse_push(task_id, {"type": "status", "status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _spawn(name: str, target: Callable[..., None], task_id: str, params: dict) -> bool:
    """启动后台线程。若该 task_id 已有作业在执行则返回 False（不重复排队）。"""

    def wrap() -> None:
        try:
            target(task_id, params)
        finally:
            with _tasks_lock:
                _active_threads.pop(task_id, None)
                _cancel_flags.pop(task_id, None)

    # 为每个任务创建取消标志
    cancel_ev = threading.Event()
    t = threading.Thread(target=wrap, daemon=True, name=name)
    with _tasks_lock:
        if task_id in _active_threads:
            return False
        _active_threads[task_id] = t
        _cancel_flags[task_id] = cancel_ev
    t.start()
    return True


# ---------------------------------------------------------------------------
# 路由：任务操作
# ---------------------------------------------------------------------------

@app.route("/api/task/cancel/<task_id>", methods=["POST"])
def api_task_cancel(task_id: str):
    """协作式取消：设置 cancel_event，当前段生成完成后停止。"""
    with _tasks_lock:
        ev = _cancel_flags.get(task_id)
    if ev is None:
        return jsonify({"error": "该任务未在运行"}), 404
    ev.set()
    _write_task_meta(task_id, {"cancelling": True})
    return jsonify({"ok": True, "msg": "取消信号已发送，当前段生成完成后停止"})


@app.route("/api/task/theme", methods=["POST"])
def api_task_theme():
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id，请先 POST /api/task/create"}), 400
    _write_task_meta(task_id, {"params": body})
    if not _spawn("theme", _run_theme_job, task_id, body):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/task/theme/generate-idea", methods=["POST"])
def api_task_theme_generate_idea():
    """调用 LLM 生成一个随机的故事主题创意，供用户一键填充。"""
    body = request.get_json(force=True, silent=True) or {}
    style_hint = (body.get("style") or "").strip()
    try:
        settings = load_settings(str(CONFIG_PATH))
        cfg = _get_config_for_api()
        # 优先用专门配置的 theme_idea_model，否则回退到 theme_story_model / vision_model
        use_model = (
            cfg.get("theme_idea_model", "").strip()
            or settings.theme_story_model
            or settings.vision_model
            or ""
        ).strip()
        client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
        style_clause = f"，风格偏好：{style_hint}" if style_hint else ""
        sys_prompt = (
            "你是一位极具创意的故事策划师，擅长构思简洁而引人入胜的短视频故事主题。"
            "每次仅输出一个故事主题/创意，用一到三句话描述，不要序号、不要额外说明。"
            "内容要有情感张力、画面感强、适合视频表现。"
        )
        user_prompt = f"请随机生成一个全新的、独特的短视频故事主题创意{style_clause}。"
        completion = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        idea = (completion.choices[0].message.content or "").strip()
        return jsonify({"ok": True, "idea": idea})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/task/theme/next", methods=["POST"])
def api_task_theme_next():
    """逐条续写：根据已有分镜生成下一个镜头，立即返回（同步调用，约 5-15s）。"""
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400

    td = _task_dir(task_id)
    sb_path = td / "storyboard.json"

    try:
        settings = load_settings(str(CONFIG_PATH))
        theme = str(body.get("theme") or "")
        style = str(body.get("style") or "")
        model = body.get("model") or None

        existing_shots: list[dict[str, Any]] = []
        existing_title = ""
        existing_synopsis = ""
        existing_characters = ""

        if sb_path.is_file():
            doc_data = json.loads(sb_path.read_text(encoding="utf-8"))
            existing_shots = doc_data.get("shots", [])
            existing_title = doc_data.get("title", "")
            existing_synopsis = doc_data.get("synopsis", "")
            existing_characters = doc_data.get("characters", "")

        shot = generate_next_shot(
            theme,
            settings,
            existing_shots,
            title=existing_title,
            synopsis=existing_synopsis,
            characters=existing_characters,
            style_hint=style,
            model=model,
        )

        # 追加到 storyboard.json
        if sb_path.is_file():
            doc = StoryboardDocument.load_json(sb_path)
        else:
            doc = StoryboardDocument(
                title=existing_title,
                synopsis=existing_synopsis,
                characters=existing_characters,
            )
        doc.shots.append(shot)
        doc.save_json(sb_path)
        doc.save_markdown(td / "storyboard.md")
        _write_task_meta(task_id, {"status": "storyboard_ready", "shot_count": len(doc.shots)})

        return jsonify({"ok": True, "shot": shot.to_dict(), "shot_count": len(doc.shots)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/task/analyze", methods=["POST"])
def api_task_analyze():
    params: dict[str, Any] = {}
    task_id: str | None = None

    ct = (request.content_type or "").lower()
    if "application/json" in ct:
        body = request.get_json(force=True, silent=True) or {}
        task_id = body.get("task_id")
        params = dict(body)
    else:
        task_id = request.form.get("task_id")
        params = {k: request.form[k] for k in request.form if k != "video"}
        f = request.files.get("video")
        if f and f.filename and task_id:
            td = _task_dir(str(task_id))
            td.mkdir(parents=True, exist_ok=True)
            dest = td / "input_video.mp4"
            f.save(str(dest))
            params["video_path"] = str(dest.resolve())

    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400

    for k in ("segment_scenes", "skip_consolidate"):
        if k in params:
            params[k] = str(params.get(k)).lower() in ("1", "true", "yes", "on")
    if "threshold" in params and params["threshold"] not in (None, ""):
        try:
            params["threshold"] = float(params["threshold"])
        except (TypeError, ValueError):
            params.pop("threshold", None)
    else:
        params.pop("threshold", None)

    _write_task_meta(str(task_id), {"params": params})
    if not _spawn("analyze", _run_analyze_job, str(task_id), params):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/task/generate", methods=["POST"])
def api_task_generate():
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400
    _write_task_meta(task_id, {"params_generate": body})
    if not _spawn("generate", _run_generate_job, task_id, body):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/task/run", methods=["POST"])
def api_task_run():
    """一步：主题或分析 + 生成（顺序在同一线程中执行）。"""
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400

    def pipeline(tid: str, p: dict) -> None:
        try:
            if (p.get("theme") or "").strip():
                _run_theme_job(tid, p)
            elif p.get("video_path") or p.get("video_url"):
                _run_analyze_job(tid, p)
            else:
                _append_progress(tid, "run 需要 theme 或 video_path / video_url")
                _write_task_meta(tid, {"status": "failed", "error": "缺少主题或视频"})
                return
            meta = _read_task_meta(tid)
            if meta.get("status") != "storyboard_ready":
                return
            gen_params = {
                "text_only_video": p.get("text_only_video"),
                "reference_images": p.get("reference_images") or [],
                "reference_videos": p.get("reference_videos") or [],
                "reference_video_descriptions": p.get("reference_video_descriptions") or [],
                "subject_lines": p.get("subject_lines") or [],
                "style": p.get("style") or "",
                "resolution": p.get("resolution"),
                "max_segment_seconds": p.get("max_segment_seconds"),
                "max_workers": p.get("max_workers") or 2,
            }
            _run_generate_job(tid, gen_params)
        except Exception as e:
            _write_task_meta(tid, {"status": "failed", "error": str(e)})
            _append_progress(tid, f"流水线失败：{e}")

    _write_task_meta(task_id, {"params_run": body})
    if not _spawn("run", pipeline, task_id, body):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


# ---------------------------------------------------------------------------
# 路由：任务状态与文件
# ---------------------------------------------------------------------------

@app.route("/api/task/<task_id>", methods=["GET"])
def api_task_status(task_id: str):
    meta = _read_task_meta(task_id)
    if not meta:
        return jsonify({"error": "unknown task"}), 404
    td = _task_dir(task_id)
    storyboard = None
    if (td / "storyboard.json").is_file():
        storyboard = json.loads((td / "storyboard.json").read_text(encoding="utf-8"))
    segments = []
    seg_dir = td / "segments"
    if seg_dir.is_dir():
        for p in sorted(seg_dir.glob("seg_*.mp4")):
            if p.stat().st_size > 1024:
                segments.append(
                    {"name": p.name, "url": f"/api/files/{task_id}/segments/{p.name}"}
                )
    out_url = None
    if (td / "output.mp4").is_file():
        out_url = f"/api/files/{task_id}/output.mp4"
    # 标记是否正在运行
    with _tasks_lock:
        is_running = task_id in _active_threads
    return jsonify(
        {
            **meta,
            "storyboard": storyboard,
            "segments": segments,
            "output_url": out_url,
            "is_running": is_running,
        }
    )


@app.route("/api/task/stream/<task_id>", methods=["GET"])
def api_task_stream(task_id: str):
    """SSE 实时进度流。前端订阅此端点替代轮询。"""
    q: queue.Queue = queue.Queue(maxsize=200)
    with _sse_lock:
        if task_id not in _sse_queues:
            _sse_queues[task_id] = []
        _sse_queues[task_id].append(q)

    def generate():
        try:
            # 推送当前快照
            meta = _read_task_meta(task_id)
            if meta:
                yield f"data: {json.dumps({'type': 'snapshot', **meta}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") == "status" and event.get("status") in ("done", "failed", "cancelled"):
                        break
                except queue.Empty:
                    # 心跳
                    yield ": ping\n\n"
        finally:
            with _sse_lock:
                qs = _sse_queues.get(task_id, [])
                if q in qs:
                    qs.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/files/<task_id>/<path:subpath>", methods=["GET"])
def api_files(task_id: str, subpath: str):
    td = _task_dir(task_id)
    parent = (td / subpath).resolve()
    if not str(parent).startswith(str(td.resolve())):
        return jsonify({"error": "invalid path"}), 400
    if not parent.is_file():
        return jsonify({"error": "not found"}), 404
    mimetype = None
    if parent.suffix.lower() == ".mp4":
        mimetype = "video/mp4"
    return send_from_directory(
        parent.parent, parent.name, mimetype=mimetype, as_attachment=False
    )


@app.route("/api/task/style/<task_id>", methods=["PUT"])
def api_task_style_put(task_id: str):
    """保存/更新任务的统一风格（style），供分镜与视频生成共用。"""
    if not _task_dir(task_id).is_dir():
        return jsonify({"error": "unknown task"}), 404
    body = request.get_json(force=True, silent=True) or {}
    style = str(body.get("style") or "").strip()
    _write_task_meta(task_id, {"style": style})
    return jsonify({"ok": True, "style": style})


@app.route("/api/storyboard/<task_id>", methods=["PUT"])
def api_storyboard_put(task_id: str):
    body = request.get_json(force=True, silent=True) or {}
    td = _task_dir(task_id)
    p = td / "storyboard.json"
    if not p.is_file():
        return jsonify({"error": "no storyboard"}), 404
    doc = StoryboardDocument.from_dict(body)
    doc.save_json(p)
    doc.save_markdown(td / "storyboard.md")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 路由：主体描述 (subjects.json)
# ---------------------------------------------------------------------------

def _read_subjects(task_id: str) -> list[dict[str, Any]]:
    p = _task_dir(task_id) / "subjects.json"
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_subjects(task_id: str, subjects: list[dict[str, Any]]) -> None:
    p = _task_dir(task_id) / "subjects.json"
    p.write_text(json.dumps(subjects, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/task/subjects/<task_id>", methods=["GET"])
def api_subjects_get(task_id: str):
    if not _task_dir(task_id).is_dir():
        return jsonify({"error": "unknown task"}), 404
    return jsonify({"subjects": _read_subjects(task_id)})


@app.route("/api/task/subjects/<task_id>", methods=["PUT"])
def api_subjects_put(task_id: str):
    if not _task_dir(task_id).is_dir():
        return jsonify({"error": "unknown task"}), 404
    body = request.get_json(force=True, silent=True) or {}
    subjects = body.get("subjects", [])
    _write_subjects(task_id, subjects)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 路由：LLM 翻译
# ---------------------------------------------------------------------------

@app.route("/api/translate", methods=["POST"])
def api_translate():
    """单字段翻译：给定文本和目标语言，返回翻译结果。"""
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    target = (body.get("target") or "en").strip()  # "en" or "zh"
    if not text:
        return jsonify({"result": ""})
    try:
        settings = load_settings(str(CONFIG_PATH))
        client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
        use_model = (settings.theme_story_model or settings.vision_model or "").strip()
        if target == "zh":
            sys_prompt = "You are a professional translator. Translate the given English text to natural Simplified Chinese. Output the translation only, no explanation."
            user_prompt = f"Translate to Chinese:\n{text}"
        else:
            sys_prompt = "You are a professional translator. Translate the given Chinese text to natural English. Output the translation only, no explanation."
            user_prompt = f"Translate to English:\n{text}"
        completion = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        result = (completion.choices[0].message.content or "").strip()
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 主体自动生成（分镜完成后调用）
# ---------------------------------------------------------------------------

_SUBJECT_GEN_SYSTEM = """You are a character description expert for AI video generation.
Given a storyboard's character list and shot list, extract each named character and write a detailed visual description for each.

Output strict JSON array only (no Markdown):
[
  {
    "name": "Character name as used in dialogue/shots",
    "name_zh": "Character name in Chinese (transliterate or translate)",
    "description_en": "Detailed English visual description for video generation prompt injection. Include: gender, approximate age, distinctive physical traits (hair color/length/style, eye color, skin tone), typical outfit/wardrobe in this story, personality/expression tendency. Be specific and consistent. 2-4 sentences.",
    "description_zh": "同上，中文版本。2-4句。"
  }
]

Rules:
- Only include named characters that appear in dialogue or character_action fields.
- description_en must be detailed enough to maintain visual consistency across video segments.
- Output JSON array only."""


def _generate_subjects_from_storyboard(doc: StoryboardDocument, settings: Any) -> list[dict[str, Any]]:
    """从分镜文档中提取角色列表，调用 LLM 生成详细主体描述。"""
    client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
    use_model = (settings.theme_story_model or settings.vision_model or "").strip()

    # 构建角色上下文
    dialogue_samples = []
    action_samples = []
    for s in doc.shots[:12]:
        if s.dialogue and s.dialogue.strip():
            dialogue_samples.append(s.dialogue.strip())
        if s.character_action and s.character_action.strip():
            action_samples.append(s.character_action.strip()[:120])

    user_msg = (
        f"Story title: {doc.title}\n"
        f"Characters info: {doc.characters}\n"
        f"Synopsis: {doc.synopsis[:300]}\n\n"
        f"Sample dialogues:\n" + "\n".join(dialogue_samples[:8]) + "\n\n"
        f"Sample actions:\n" + "\n".join(action_samples[:6]) + "\n\n"
        "Extract all named characters and write detailed descriptions. Output JSON array only."
    )

    try:
        completion = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": _SUBJECT_GEN_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()
        # 提取 JSON 数组
        import re as _re
        m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# 路由：工作区管理
# ---------------------------------------------------------------------------

@app.route("/api/workspace/list", methods=["GET"])
def api_workspace_list():
    _ensure_workspace()
    rows = []
    for d in sorted(WORKSPACE.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "task.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            rows.append(meta)
        except json.JSONDecodeError:
            continue
    return jsonify({"tasks": rows})


@app.route("/api/workspace/resume/<task_id>", methods=["POST"])
def api_workspace_resume(task_id: str):
    """从已有分镜继续生成视频（可复用 segments 缓存）。"""
    body = request.get_json(force=True, silent=True) or {}
    td = _task_dir(task_id)
    if not (td / "storyboard.json").is_file():
        return jsonify({"error": "无分镜可恢复"}), 400
    merged = {
        "task_id": task_id,
        "text_only_video": body.get("text_only_video"),
        "reference_images": body.get("reference_images") or [],
        "reference_videos": body.get("reference_videos") or [],
        "reference_video_descriptions": body.get("reference_video_descriptions") or [],
        "subject_lines": body.get("subject_lines") or [],
        "style": body.get("style") or "",
        "resolution": body.get("resolution"),
        "max_segment_seconds": body.get("max_segment_seconds"),
        "max_workers": body.get("max_workers") or 2,
    }
    _write_task_meta(task_id, {"status": "queued_generate", "params_resume": merged})
    if not _spawn("resume", _run_generate_job, task_id, merged):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True})


@app.route("/api/workspace/clear-segments/<task_id>", methods=["POST"])
def api_clear_segments(task_id: str):
    """删除片段缓存以便重新生成。"""
    seg = _task_dir(task_id) / "segments"
    if seg.is_dir():
        shutil.rmtree(seg, ignore_errors=True)
    _task_dir(task_id).mkdir(parents=True, exist_ok=True)
    (_task_dir(task_id) / "segments").mkdir(exist_ok=True)
    _write_task_meta(task_id, {"segments_done": 0})
    return jsonify({"ok": True})


@app.route("/api/workspace/delete/<task_id>", methods=["DELETE"])
def api_workspace_delete(task_id: str):
    """删除整个任务目录（不可恢复）。"""
    td = _task_dir(task_id)
    if not td.is_dir():
        return jsonify({"error": "任务不存在"}), 404
    with _tasks_lock:
        if task_id in _active_threads:
            return jsonify({"error": "任务正在运行，请先取消再删除"}), 409
    shutil.rmtree(td, ignore_errors=True)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _ensure_config_file() -> None:
    if not CONFIG_PATH.is_file() and CONFIG_EXAMPLE.is_file():
        CONFIG_PATH.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> None:
    _ensure_workspace()
    _ensure_config_file()
    _cleanup_old_tasks()
    import socket
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    print(f"video2text Web UI: http://127.0.0.1:8000  (本机)")
    print(f"                   http://{local_ip}:8000  (局域网)")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
