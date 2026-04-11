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
    get_user_config_path,
    get_user_workspace_dir,
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

from video2text.web.auth import get_current_user, init_auth, is_current_user_admin

WORKSPACE = get_workspace_dir()
STATIC = get_static_dir()
CONFIG_PATH = get_default_config_path()
CONFIG_EXAMPLE = get_config_example_path()

# 任务自动清理：超过 N 天未更新的任务目录将在下次启动时清理
TASK_TTL_DAYS = 2
_CLEANUP_INTERVAL_SEC = 6 * 3600  # 6 hours

_RUNNING_STATUSES = frozenset({"running", "pending", "analyzing", "generating", "consolidating"})

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


def _require_user() -> str:
    """获取当前登录用户名，未登录则 abort。"""
    user = get_current_user()
    if not user:
        from flask import abort
        abort(401)
    return user


def _ensure_workspace(username: str | None = None) -> None:
    if username:
        get_user_workspace_dir(username).mkdir(parents=True, exist_ok=True)
    else:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)


def _task_dir(task_id: str, owner: str | None = None) -> Path:
    """返回任务目录。若提供 owner 则在用户工作区下查找；否则尝试自动查找。"""
    if owner:
        return get_user_workspace_dir(owner) / task_id
    return _resolve_task_dir(task_id)


def _resolve_task_dir(task_id: str) -> Path:
    """在所有用户工作区下查找 task_id 对应的目录。"""
    if not WORKSPACE.is_dir():
        return WORKSPACE / task_id
    for user_dir in WORKSPACE.iterdir():
        if not user_dir.is_dir():
            continue
        td = user_dir / task_id
        if td.is_dir():
            return td
    return WORKSPACE / task_id


def _task_owner(task_id: str) -> str | None:
    """从 task.json 中读取任务的 owner 字段。"""
    meta = _read_task_meta(task_id)
    return meta.get("owner")


def _check_task_access(task_id: str, username: str) -> bool:
    """检查用户是否有权限访问指定任务。管理员可访问所有任务。"""
    td = _resolve_task_dir(task_id)
    if not td.is_dir():
        return False
    meta_path = td / "task.json"
    if not meta_path.is_file():
        return False
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    owner = meta.get("owner", "")
    if owner == username:
        return True
    if is_current_user_admin():
        return True
    return False


def _read_task_meta(task_id: str, owner: str | None = None) -> dict[str, Any]:
    p = _task_dir(task_id, owner) / "task.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _write_task_meta(task_id: str, data: dict[str, Any], owner: str | None = None) -> None:
    d = _task_dir(task_id, owner)
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


def _get_global_config() -> dict[str, Any]:
    """读取全局配置（作为用户配置的 fallback 默认值）。"""
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if CONFIG_EXAMPLE.is_file():
        return json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    return {}


def _get_user_config(username: str) -> dict[str, Any]:
    """读取用户独立配置，不存在则返回空 dict。"""
    p = get_user_config_path(username)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_user_config(username: str, data: dict[str, Any]) -> None:
    """保存用户独立配置文件。"""
    p = get_user_config_path(username)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_config_for_api(username: str | None = None) -> dict[str, Any]:
    """合并全局配置和用户配置，用户配置覆盖全局配置。"""
    base = _get_global_config()
    if username:
        user_cfg = _get_user_config(username)
        if user_cfg:
            base.update(user_cfg)
    return base


def _get_user_config_path_for_settings(username: str) -> str:
    """返回用户配置文件的路径字符串（供 load_settings 使用）。
    合并全局配置与用户覆盖项，写入用户配置路径后返回。
    如果用户无独立配置则回退全局配置。"""
    user_path = get_user_config_path(username)
    user_cfg = _get_user_config(username)
    if not user_cfg:
        return str(CONFIG_PATH)
    merged = _get_global_config()
    merged.update(user_cfg)
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(user_path)


def _save_config_file(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _task_last_modified(task_dir: Path) -> datetime | None:
    """Get the last modification time of a task (from task.json metadata or filesystem mtime)."""
    meta_path = task_dir / "task.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            updated_str = meta.get("updated", "")
            if updated_str:
                dt = datetime.fromisoformat(updated_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except Exception:
            pass
    try:
        mtime = task_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except Exception:
        return None


def _is_task_running(task_id: str) -> bool:
    """Check if a task is currently running (active thread or running status)."""
    with _tasks_lock:
        if task_id in _active_threads:
            return True
    for user_dir in WORKSPACE.iterdir():
        if not user_dir.is_dir():
            continue
        td = user_dir / task_id
        meta_path = td / "task.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("status", "") in _RUNNING_STATUSES:
                    return True
            except Exception:
                pass
            break
    return False


def _cleanup_old_tasks(dry_run: bool = False) -> dict[str, Any]:
    """清理超过 TASK_TTL_DAYS 天未更新的任务目录（保护运行中的任务）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TASK_TTL_DAYS)
    result: dict[str, Any] = {"deleted": [], "skipped_running": [], "freed_bytes": 0}
    if not WORKSPACE.is_dir():
        return result
    for user_dir in WORKSPACE.iterdir():
        if not user_dir.is_dir():
            continue
        for d in user_dir.iterdir():
            if not d.is_dir():
                continue
            task_id = d.name
            last_mod = _task_last_modified(d)
            if last_mod is None or last_mod >= cutoff:
                continue
            if _is_task_running(task_id):
                result["skipped_running"].append(task_id)
                continue
            dir_size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            result["freed_bytes"] += dir_size
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
            result["deleted"].append(task_id)
    return result


# ---------------------------------------------------------------------------
# 路由：静态页与配置
# ---------------------------------------------------------------------------

@app.route("/")
def index_page():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    username = _require_user()
    cfg = _get_config_for_api(username)
    safe = dict(cfg)
    if "dashscope_api_key" in safe and safe["dashscope_api_key"]:
        safe["dashscope_api_key_masked"] = _mask_key(str(safe["dashscope_api_key"]))
        if len(str(safe["dashscope_api_key"])) > 8:
            safe["dashscope_api_key"] = ""
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    cur = _get_user_config(username)
    if not cur:
        cur = dict(_get_global_config())
    for k, v in body.items():
        if k == "dashscope_api_key" and (v is None or str(v).strip() == ""):
            continue
        cur[k] = v
    _save_user_config(username, cur)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 路由：任务管理
# ---------------------------------------------------------------------------

@app.route("/api/task/create", methods=["POST"])
def api_task_create():
    username = _require_user()
    _ensure_workspace(username)
    task_id = uuid.uuid4().hex[:12]
    d = _task_dir(task_id, owner=username)
    d.mkdir(parents=True, exist_ok=True)
    (d / "references").mkdir(exist_ok=True)
    (d / "segments").mkdir(exist_ok=True)
    _write_task_meta(
        task_id,
        {
            "task_id": task_id,
            "owner": username,
            "status": "created",
            "created": _iso_now(),
            "progress": [],
            "type": "pending",
            "params": {},
        },
        owner=username,
    )
    return jsonify({"task_id": task_id})


@app.route("/api/upload/reference", methods=["POST"])
def api_upload_reference():
    username = _require_user()
    task_id = request.form.get("task_id") or ""
    if not task_id or not _check_task_access(task_id, username):
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
    # 持久化参考文件列表到 task.json
    meta = _read_task_meta(task_id)
    ref_list = list(meta.get("reference_files", []))
    ref_list.extend(saved)
    _write_task_meta(task_id, {"reference_files": ref_list})
    return jsonify({"files": saved})


@app.route("/api/task/references/<task_id>", methods=["PUT"])
def api_task_references_put(task_id: str):
    """更新参考文件的描述信息（前端编辑后保存）。"""
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    body = request.get_json(force=True, silent=True) or {}
    refs = body.get("reference_files", [])
    _write_task_meta(task_id, {"reference_files": refs})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 路由：preflight 配置校验
# ---------------------------------------------------------------------------

@app.route("/api/task/preflight", methods=["POST"])
def api_task_preflight():
    """生成前的快速配置校验，不做任何 API 调用。"""
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")

    issues: list[str] = []
    cfg = _get_config_for_api(username)

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
    owner = params.get("_owner", "")

    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "theme_running", "type": "theme"})
        _sse_push(task_id, {"type": "status", "status": "theme_running"})
        log("Storyboard generation started, loading config…")
        cfg_path = _get_user_config_path_for_settings(owner) if owner else str(CONFIG_PATH)
        settings = load_settings(cfg_path)
        theme = (params.get("theme") or "").strip()
        if not theme:
            raise ValueError("主题为空")
        use_model = params.get("model") or settings.theme_story_model or settings.vision_model
        min_shots = int(params.get("min_shots") or 8)
        max_shots = int(params.get("max_shots") or 24)
        log(f"Calling LLM ({use_model}) — Phase 1: story outline, Phase 2: shot design ({min_shots}–{max_shots} shots)… (this may take 30–90s)")
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
    owner = params.get("_owner", "")

    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "analyze_running", "type": "analyze"})
        _sse_push(task_id, {"type": "status", "status": "analyze_running"})
        log("Video analysis started, loading config…")
        cfg_path = _get_user_config_path_for_settings(owner) if owner else str(CONFIG_PATH)
        settings = load_settings(cfg_path)
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
    owner = params.get("_owner", "")

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
        cfg_path = _get_user_config_path_for_settings(owner) if owner else str(CONFIG_PATH)
        settings = load_settings(cfg_path)
        extras = load_generation_extras(cfg_path)

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
        max_workers = int(params.get("max_workers") or 4)

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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    with _tasks_lock:
        ev = _cancel_flags.get(task_id)
    if ev is None:
        return jsonify({"error": "该任务未在运行"}), 404
    ev.set()
    _write_task_meta(task_id, {"cancelling": True})
    return jsonify({"ok": True, "msg": "取消信号已发送，当前段生成完成后停止"})


@app.route("/api/task/theme", methods=["POST"])
def api_task_theme():
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id，请先 POST /api/task/create"}), 400
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    body["_owner"] = username
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
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    style_hint = (body.get("style") or "").strip()
    try:
        cfg_path = _get_user_config_path_for_settings(username)
        settings = load_settings(cfg_path)
        cfg = _get_config_for_api(username)
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
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403

    td = _task_dir(task_id)
    sb_path = td / "storyboard.json"

    try:
        cfg_path = _get_user_config_path_for_settings(username)
        settings = load_settings(cfg_path)
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
    username = _require_user()
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
    if not _check_task_access(str(task_id), username):
        return jsonify({"error": "无权限操作该任务"}), 403

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

    params["_owner"] = username
    _write_task_meta(str(task_id), {"params": params})
    if not _spawn("analyze", _run_analyze_job, str(task_id), params):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/task/generate", methods=["POST"])
def api_task_generate():
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    body["_owner"] = username
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
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id"}), 400
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    body["_owner"] = username

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
                "max_workers": p.get("max_workers") or 4,
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "unknown task"}), 404
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

    # 输入视频（分析模式上传的视频）
    input_video_url = None
    if (td / "input_video.mp4").is_file():
        input_video_url = f"/api/files/{task_id}/input_video.mp4"

    # 参考文件列表：补充可访问的 URL
    ref_files = list(meta.get("reference_files", []))
    for rf in ref_files:
        if rf.get("name"):
            rf["url"] = f"/api/files/{task_id}/references/{rf['name']}"

    # 标记是否正在运行
    with _tasks_lock:
        is_running = task_id in _active_threads
    return jsonify(
        {
            **meta,
            "storyboard": storyboard,
            "segments": segments,
            "output_url": out_url,
            "input_video_url": input_video_url,
            "reference_files": ref_files,
            "is_running": is_running,
        }
    )


@app.route("/api/task/stream/<task_id>", methods=["GET"])
def api_task_stream(task_id: str):
    """SSE 实时进度流。前端订阅此端点替代轮询。"""
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "unknown task"}), 404
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限访问该文件"}), 403
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "unknown task"}), 404
    body = request.get_json(force=True, silent=True) or {}
    style = str(body.get("style") or "").strip()
    _write_task_meta(task_id, {"style": style})
    return jsonify({"ok": True, "style": style})


@app.route("/api/storyboard/<task_id>", methods=["PUT"])
def api_storyboard_put(task_id: str):
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "unknown task"}), 404
    return jsonify({"subjects": _read_subjects(task_id)})


@app.route("/api/task/subjects/<task_id>", methods=["PUT"])
def api_subjects_put(task_id: str):
    username = _require_user()
    if not _check_task_access(task_id, username):
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
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    target = (body.get("target") or "en").strip()  # "en" or "zh"
    if not text:
        return jsonify({"result": ""})
    try:
        cfg_path = _get_user_config_path_for_settings(username)
        settings = load_settings(cfg_path)
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
    username = _require_user()
    user_ws = get_user_workspace_dir(username)
    user_ws.mkdir(parents=True, exist_ok=True)

    rows = []
    # 当前用户工作区
    for d in sorted(user_ws.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    body = request.get_json(force=True, silent=True) or {}
    td = _task_dir(task_id)
    if not (td / "storyboard.json").is_file():
        return jsonify({"error": "无分镜可恢复"}), 400
    merged = {
        "task_id": task_id,
        "_owner": username,
        "text_only_video": body.get("text_only_video"),
        "reference_images": body.get("reference_images") or [],
        "reference_videos": body.get("reference_videos") or [],
        "reference_video_descriptions": body.get("reference_video_descriptions") or [],
        "subject_lines": body.get("subject_lines") or [],
        "style": body.get("style") or "",
        "resolution": body.get("resolution"),
        "max_segment_seconds": body.get("max_segment_seconds"),
        "max_workers": body.get("max_workers") or 4,
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
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
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "无权限操作该任务"}), 403
    td = _task_dir(task_id)
    if not td.is_dir():
        return jsonify({"error": "任务不存在"}), 404
    with _tasks_lock:
        if task_id in _active_threads:
            return jsonify({"error": "任务正在运行，请先取消再删除"}), 409
    shutil.rmtree(td, ignore_errors=True)
    return jsonify({"ok": True})


def _workspace_disk_usage() -> list[dict[str, Any]]:
    """统计各用户工作区磁盘占用。"""
    usage: list[dict[str, Any]] = []
    if not WORKSPACE.is_dir():
        return usage
    for user_dir in sorted(WORKSPACE.iterdir(), key=lambda x: x.name):
        if not user_dir.is_dir():
            continue
        total = 0
        task_count = 0
        for d in user_dir.iterdir():
            if d.is_dir() and (d / "task.json").is_file():
                task_count += 1
                total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        usage.append({
            "user": user_dir.name,
            "tasks": task_count,
            "bytes": total,
            "mb": round(total / (1024 * 1024), 1),
        })
    return usage


@app.route("/api/admin/disk-usage")
def api_admin_disk_usage():
    """查看各用户工作区磁盘占用。"""
    _require_user()
    return jsonify(_workspace_disk_usage())


@app.route("/api/admin/cleanup", methods=["POST"])
def api_admin_cleanup():
    """手动触发缓存清理（支持 dry_run 预览）。"""
    _require_user()
    dry_run = request.json.get("dry_run", False) if request.is_json else False
    result = _cleanup_old_tasks(dry_run=dry_run)
    result["freed_mb"] = round(result["freed_bytes"] / (1024 * 1024), 1)
    return jsonify(result)


# ---------------------------------------------------------------------------
# IP 管理 API
# ---------------------------------------------------------------------------

from video2text.core.styles import get_all_style_presets, get_style_by_id, search_styles
from video2text.core.ip_manager import (
    delete_ip,
    list_ips,
    load_ip,
    save_ip,
    save_character_reference_image,
    update_character_reference_in_profile,
)
from video2text.core.ip_creator import (
    create_ip_from_proposal,
    generate_character_images,
    generate_ip_proposal,
)
from video2text.core.theme import generate_storyboard_from_ip


@app.route("/api/styles")
def api_styles():
    """获取分类风格预设列表。"""
    q = request.args.get("q", "").strip()
    if q:
        return jsonify(search_styles(q))
    return jsonify(get_all_style_presets())


@app.route("/api/styles/<style_id>")
def api_style_detail(style_id: str):
    """获取单个风格预设。"""
    style = get_style_by_id(style_id)
    if not style:
        return jsonify({"error": "风格不存在"}), 404
    return jsonify(style)


@app.route("/api/ips")
def api_list_ips():
    """列出当前用户的所有 IP。"""
    user = _require_user()
    ips = list_ips(user)
    return jsonify([ip.to_dict() for ip in ips])


@app.route("/api/ip/<ip_id>")
def api_get_ip(ip_id: str):
    """获取 IP 详情。"""
    user = _require_user()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    return jsonify(ip.to_dict())


@app.route("/api/ip/create", methods=["POST"])
def api_create_ip():
    """从种子创意生成 IP 提案。"""
    user = _require_user()
    data = request.json or {}
    seed = data.get("seed_idea", "").strip()
    if not seed:
        return jsonify({"error": "请输入种子创意"}), 400
    style_preset_id = data.get("style_preset_id", "")

    try:
        settings = load_settings()
        proposal = generate_ip_proposal(
            seed, settings, style_preset_id=style_preset_id,
        )
        return jsonify({"proposal": proposal})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ip/confirm", methods=["POST"])
def api_confirm_ip():
    """确认 IP 提案并保存，可选立即生成角色图。"""
    user = _require_user()
    data = request.json or {}
    proposal = data.get("proposal")
    if not proposal:
        return jsonify({"error": "缺少 proposal"}), 400
    generate_images = data.get("generate_images", True)

    try:
        profile = create_ip_from_proposal(proposal, user)

        if generate_images:
            settings = load_settings()
            profile = generate_character_images(profile, user, settings)

        return jsonify({"ip": profile.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ip/<ip_id>", methods=["PUT"])
def api_update_ip(ip_id: str):
    """更新 IP 元数据。"""
    user = _require_user()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404

    data = request.json or {}
    from video2text.core.ip_manager import IPProfile
    updated = IPProfile.from_dict({**ip.to_dict(), **data, "id": ip_id})
    save_ip(user, updated)
    return jsonify(updated.to_dict())


@app.route("/api/ip/<ip_id>", methods=["DELETE"])
def api_delete_ip(ip_id: str):
    """删除 IP 及其所有资产。"""
    user = _require_user()
    if delete_ip(user, ip_id):
        return jsonify({"ok": True})
    return jsonify({"error": "IP 不存在"}), 404


@app.route("/api/ip/<ip_id>/character/<char_id>/regenerate", methods=["POST"])
def api_regenerate_character_image(ip_id: str, char_id: str):
    """重新生成角色参考图。"""
    user = _require_user()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404

    try:
        settings = load_settings()
        ip = generate_character_images(ip, user, settings, char_ids=[char_id])
        return jsonify({"ip": ip.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ip/<ip_id>/character/<char_id>/upload", methods=["POST"])
def api_upload_character_image(ip_id: str, char_id: str):
    """上传替换角色参考图。"""
    user = _require_user()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404

    if "file" not in request.files:
        return jsonify({"error": "缺少文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    f.save(str(tmp))
    try:
        dest = save_character_reference_image(user, ip_id, char_id, tmp)
        ip = update_character_reference_in_profile(
            user, ip_id, char_id, str(dest), ref_type="uploaded",
        )
        return jsonify({"ip": ip.to_dict() if ip else {}})
    finally:
        tmp.unlink(missing_ok=True)


@app.route("/api/task/ip-theme", methods=["POST"])
def api_task_ip_theme():
    """基于 IP 的主题生成任务（生成分镜 + 视频）。"""
    user = _require_user()
    data = request.json or {}
    ip_id = data.get("ip_id", "").strip()
    if not ip_id:
        return jsonify({"error": "请指定 IP"}), 400

    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404

    theme_hint = data.get("theme_hint", "")
    min_shots = int(data.get("min_shots", 8))
    max_shots = int(data.get("max_shots", 16))
    generate_video = data.get("generate_video", True)

    task_id = uuid.uuid4().hex[:16]
    _ensure_workspace(user)
    task_dir = get_user_workspace_dir(user) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task_id": task_id,
        "owner": user,
        "type": "ip-theme",
        "ip_id": ip_id,
        "ip_name": ip.name,
        "theme_hint": theme_hint,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (task_dir / "task.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def _run():
        try:
            _update_task_meta(task_dir, {"status": "generating_storyboard"})
            _sse_push(task_id, "正在生成 IP 分镜…")

            settings = load_settings()
            doc = generate_storyboard_from_ip(
                ip, settings,
                theme_hint=theme_hint,
                min_shots=min_shots,
                max_shots=max_shots,
            )
            doc.save_json(task_dir / "storyboard.json")
            doc.save_markdown(task_dir / "storyboard.md")
            _update_task_meta(task_dir, {"status": "storyboard_ready"})
            _sse_push(task_id, f"分镜已生成：{len(doc.shots)} 个镜头")

            if generate_video:
                _update_task_meta(task_dir, {"status": "generating_video"})
                _sse_push(task_id, "正在生成 IP 视频…")

                from video2text.pipeline.generator import run_ip_storyboard_generation
                run_ip_storyboard_generation(
                    doc, ip, settings,
                    segments_dir=task_dir / "segments",
                    output_mp4=task_dir / "output.mp4",
                    progress_cb=lambda m: _sse_push(task_id, m),
                    meta_update=lambda d: _update_task_meta(task_dir, d),
                    cancel_event=_cancel_flags.get(task_id),
                )
                _update_task_meta(task_dir, {"status": "done"})
                _sse_push(task_id, "IP 视频生成完成！")
            else:
                _update_task_meta(task_dir, {"status": "done"})

        except CancellationError:
            _update_task_meta(task_dir, {"status": "cancelled"})
            _sse_push(task_id, "任务已取消")
        except Exception as e:
            _update_task_meta(task_dir, {"status": "error", "error": str(e)})
            _sse_push(task_id, f"错误：{e}")

    cancel_evt = threading.Event()
    _cancel_flags[task_id] = cancel_evt
    t = threading.Thread(target=_run, daemon=True, name=f"ip-task-{task_id}")
    with _tasks_lock:
        _active_threads[task_id] = t
    t.start()

    return jsonify({"task_id": task_id, "status": "pending"})


def _update_task_meta(task_dir: Path, updates: dict[str, Any]) -> None:
    """更新任务元数据 JSON。"""
    meta_path = task_dir / "task.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    meta.update(updates)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _sse_push(task_id: str, message: str) -> None:
    """向 SSE 订阅者推送消息。"""
    with _sse_lock:
        qs = _sse_queues.get(task_id, [])
        for q in qs:
            try:
                q.put_nowait(message)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _migrate_legacy_tasks() -> None:
    """将旧版无 owner 的任务迁移到 admin 用户工作区下。"""
    if not WORKSPACE.is_dir():
        return
    admin_ws = get_user_workspace_dir("admin")
    for d in list(WORKSPACE.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "task.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("owner"):
            continue
        meta["owner"] = "admin"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        dest = admin_ws / d.name
        if not dest.exists():
            admin_ws.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest))


def _start_cleanup_timer() -> None:
    """Start a daemon thread that runs cleanup periodically."""
    import time

    def _loop() -> None:
        while True:
            time.sleep(_CLEANUP_INTERVAL_SEC)
            try:
                result = _cleanup_old_tasks()
                if result["deleted"]:
                    freed_mb = round(result["freed_bytes"] / (1024 * 1024), 1)
                    print(f"[cleanup] 定时清理：删除 {len(result['deleted'])} 个过期任务，释放 {freed_mb} MB")
            except Exception as e:
                print(f"[cleanup] 定时清理异常: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="cleanup-timer")
    t.start()


def main() -> None:
    _ensure_workspace()
    _migrate_legacy_tasks()
    result = _cleanup_old_tasks()
    if result["deleted"]:
        freed_mb = round(result["freed_bytes"] / (1024 * 1024), 1)
        print(f"[startup] 清理 {len(result['deleted'])} 个过期任务（>{TASK_TTL_DAYS} 天），释放 {freed_mb} MB")
    _start_cleanup_timer()
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
