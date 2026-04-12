#!/usr/bin/env python3
"""
video2text Web UI — Flask 后端：配置、任务、工作区缓存与断点续传。
本地运行：v2t-web 或 python -m video2text.web.app → http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import logging

import shutil
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_from_directory

from openai import OpenAI
from werkzeug.exceptions import HTTPException

from video2text.config.settings import (
    allowed_user_config_fields,
    filter_task_overrides,
    load_settings,
    load_settings_from_dict,
    normalize_user_config_delta,
    resolve_effective_settings_dict,
    resolve_theme_idea_model,
    resolve_theme_story_model,
)
from video2text.utils.paths import (
    get_data_config_dir,
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
from video2text.web.telemetry import (
    bind_log_context,
    get_request_id,
    init_observability,
    metrics_response,
    record_exception,
    record_task_event,
)

WORKSPACE = get_workspace_dir()
STATIC = get_static_dir()
CONFIG_PATH = get_default_config_path()

_CLEANUP_INTERVAL_SEC = 6 * 3600  # 6 hours

_RUNNING_STATUSES = frozenset({"running", "pending", "analyzing", "generating", "consolidating"})

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB uploads

init_observability(app)
init_auth(app)

log = logging.getLogger(__name__)

_tasks_lock = threading.Lock()
_active_threads: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}

# 注册 IP 蓝图
from video2text.web.bp_ip import bp as ip_bp, init_ip_blueprint
app.register_blueprint(ip_bp)



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
    prev_status = str(cur.get("status") or "")
    cur.update(data)
    cur["updated"] = _iso_now()
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    status = str(cur.get("status") or "")
    task_type = str(cur.get("type") or "unknown")
    task_owner = str(cur.get("owner") or owner or "")
    if status and status != prev_status:
        record_task_event(task_type, status)
        log.info(
            "task status updated",
            extra={
                "event": "task_status_updated",
                "task_id": task_id,
                "task_type": task_type,
                "task_status": status,
                "owner": task_owner,
            },
        )
    err = data.get("error")
    if err:
        record_exception("task_failure")
        log.error(
            "task error recorded",
            extra={
                "event": "task_error",
                "task_id": task_id,
                "task_type": task_type,
                "task_status": status or "unknown",
                "owner": task_owner,
            },
        )


def _append_progress(task_id: str, msg: str) -> None:
    _sse_push(task_id, msg)


def _sse_push(task_id: str, event: dict[str, Any] | str) -> None:
    """写入进度事件，由文件轮询 stream 读取。纯 str 视为 progress 消息。"""
    if isinstance(event, str):
        msg = event
    elif isinstance(event, dict):
        msg = event.get("msg", "")
    else:
        msg = str(event)
    if msg:
        with _tasks_lock:
            meta = _read_task_meta(task_id)
            prog = list(meta.get("progress", []))
            prog.append({"t": _iso_now(), "msg": msg})
            meta["progress"] = prog[-500:]
            _write_task_meta(task_id, meta)
    if isinstance(event, dict) and event.get("type") == "status":
        log.info(
            "task status event",
            extra={
                "event": "task_status_event",
                "task_id": task_id,
                "task_status": str(event.get("status") or ""),
                "task_type": str(_read_task_meta(task_id).get("type") or "unknown"),
            },
        )


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "" if not k else "****"
    return k[:4] + "…" + k[-4:]


def _get_global_config() -> dict[str, Any]:
    """读取全局配置。"""
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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


def _get_effective_config_for_user(
    username: str | None,
    *,
    task_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """返回有效配置及来源信息。"""
    global_cfg = _get_global_config()
    user_cfg = _get_user_config(username) if username else {}
    return resolve_effective_settings_dict(
        global_cfg=global_cfg,
        user_cfg=user_cfg,
        task_overrides=filter_task_overrides(task_overrides),
        enforce_user_api_key=bool(username),
    )


def _get_config_for_api(username: str | None = None) -> dict[str, Any]:
    """返回用于 API 展示的有效配置（默认 + 全局 + 用户 + env）。"""
    effective, _ = _get_effective_config_for_user(username)
    return effective


def _load_settings_for_user(
    username: str,
    task_overrides: dict[str, Any] | None = None,
):
    """在内存中构造用户生效配置（支持任务临时覆盖），不写盘。"""
    effective, _ = _get_effective_config_for_user(
        username,
        task_overrides=task_overrides,
    )
    return load_settings_from_dict(effective)


def _mask_config_for_response(cfg: dict[str, Any]) -> dict[str, Any]:
    safe = dict(cfg)
    if "dashscope_api_key" in safe and safe["dashscope_api_key"]:
        safe["dashscope_api_key_masked"] = _mask_key(str(safe["dashscope_api_key"]))
        if len(str(safe["dashscope_api_key"])) > 8:
            safe["dashscope_api_key"] = ""
    return safe


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


def _cleanup_old_tasks(dry_run: bool = False, ttl_days: int = 7) -> dict[str, Any]:
    """清理超过 ttl_days 天未更新的任务目录（保护运行中的任务）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
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


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"ok": True, "ts": _iso_now()})


@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    return metrics_response()


@app.errorhandler(Exception)
def handle_unexpected_exception(err: Exception):
    if isinstance(err, HTTPException):
        return err
    record_exception("unhandled_exception")
    log.exception(
        "unhandled server exception",
        extra={
            "event": "unhandled_exception",
            "request_id": get_request_id(),
            "path": request.path,
            "method": request.method,
            "route": request.url_rule.rule if request.url_rule else request.path,
        },
    )
    if request.path.startswith("/api/"):
        return jsonify({"error": "服务器内部错误", "request_id": get_request_id()}), 500
    return "Internal Server Error", 500


@app.route("/api/config", methods=["GET"])
def api_config_get():
    username = _require_user()
    cfg, sources = _get_effective_config_for_user(username)
    safe = _mask_config_for_response(cfg)
    with_source = str(request.args.get("with_source", "")).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if with_source:
        return jsonify({"config": safe, "sources": sources})
    return jsonify(safe)


@app.route("/api/config/effective", methods=["GET"])
def api_config_effective():
    username = _require_user()
    cfg, sources = _get_effective_config_for_user(username)
    return jsonify({"config": _mask_config_for_response(cfg), "sources": sources})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    username = _require_user()
    body = request.get_json(force=True, silent=True) or {}
    global_cfg = _get_global_config()
    cur = normalize_user_config_delta(global_cfg, _get_user_config(username))
    allow = allowed_user_config_fields()
    ignored: list[str] = []
    touched: list[str] = []
    for k, v in body.items():
        if k not in allow:
            ignored.append(k)
            continue
        touched.append(k)
        if k == "dashscope_api_key":
            key = ""
            if v is not None:
                key = str(v).strip()
            if key:
                cur[k] = key
            else:
                cur.pop(k, None)
            continue

        if v is None:
            cur.pop(k, None)
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                cur.pop(k, None)
                continue
        if global_cfg.get(k) == v:
            cur.pop(k, None)
            continue
        cur[k] = v

    final_delta = normalize_user_config_delta(global_cfg, cur)
    _save_user_config(username, final_delta)
    resp: dict[str, Any] = {
        "ok": True,
        "stored_delta_keys": sorted(final_delta.keys()),
    }
    if touched:
        resp["updated_fields"] = touched
    if ignored:
        resp["ignored_fields"] = ignored
    return jsonify(resp)


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
        settings = (
            _load_settings_for_user(owner, task_overrides=params)
            if owner
            else load_settings(str(CONFIG_PATH))
        )
        theme = (params.get("theme") or "").strip()
        if not theme:
            raise ValueError("主题为空")
        raw_m = params.get("model")
        override = None
        if raw_m is not None:
            s = str(raw_m).strip()
            if s:
                override = s
        use_model = resolve_theme_story_model(settings, override=override)
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
            else:
                log("Subject generation returned empty — no named characters found or LLM response unparseable.")
        except Exception as se:
            log(f"Subject generation skipped: {se}")
        _write_task_meta(task_id, {"status": "done"})
        _sse_push(task_id, {"type": "status", "status": "done"})
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        _sse_push(task_id, {"type": "status", "status": "failed", "error": str(e)})
        record_exception("theme_job")
        logging.getLogger(__name__).exception(
            "theme job failed",
            extra={"event": "theme_job_failed", "task_id": task_id, "owner": owner},
        )
        log(f"失败：{e}")


def _run_analyze_job(task_id: str, params: dict[str, Any]) -> None:
    owner = params.get("_owner", "")

    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "analyze_running", "type": "analyze"})
        _sse_push(task_id, {"type": "status", "status": "analyze_running"})
        log("Video analysis started, loading config…")
        settings = (
            _load_settings_for_user(owner, task_overrides=params)
            if owner
            else load_settings(str(CONFIG_PATH))
        )
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
            else:
                log("Subject generation returned empty — no named characters found or LLM response unparseable.")
        except Exception as se:
            log(f"Subject generation skipped: {se}")
        _write_task_meta(task_id, {"status": "done"})
        _sse_push(task_id, {"type": "status", "status": "done"})
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        _sse_push(task_id, {"type": "status", "status": "failed", "error": str(e)})
        record_exception("analyze_job")
        logging.getLogger(__name__).exception(
            "analyze job failed",
            extra={"event": "analyze_job_failed", "task_id": task_id, "owner": owner},
        )
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
        settings = (
            _load_settings_for_user(owner, task_overrides=params)
            if owner
            else load_settings(str(CONFIG_PATH))
        )

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
            if not ref_images and not ref_videos and settings.require_reference:
                raise ValueError(
                    "参考生需要上传参考图或视频，或勾选「纯文生（无参考）」"
                )

        if ref_videos and ref_video_descs and len(ref_videos) != len(ref_video_descs):
            raise ValueError("参考视频数量与视频说明数量须一致")

        max_seg = float(
            params.get("max_segment_seconds") or settings.max_segment_seconds
        )
        # 优先用参数里的 style，其次用任务元数据里已保存的统一风格
        meta_style = str(_read_task_meta(task_id).get("style") or "")
        style = str(params.get("style") or meta_style or "")
        resolution = params.get("resolution") or None
        max_workers = int(params.get("max_workers") or settings.max_workers)

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
            per_chunk_reference_filter=settings.per_chunk_reference_filter,
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
        record_exception("generate_job")
        logging.getLogger(__name__).exception(
            "generate job failed",
            extra={"event": "generate_job_failed", "task_id": task_id, "owner": owner},
        )
        log(f"失败：{e}")


def _spawn(name: str, target: Callable[..., None], task_id: str, params: dict) -> bool:
    """启动后台线程（跨进程原子去重）。
    使用 .running 标志文件 + fcntl 文件锁确保多 worker 进程间只有一个实例运行。
    """
    import fcntl
    import os

    td = _task_dir(task_id)
    td.mkdir(parents=True, exist_ok=True)
    flag = td / ".running"
    lock_path = td / ".spawn.lock"
    request_id = get_request_id()
    owner = str(params.get("_owner") or "")

    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if flag.exists():
                log.warning(
                    "spawn rejected: running flag exists",
                    extra={
                        "event": "task_spawn_rejected",
                        "task_id": task_id,
                        "task_type": name,
                        "owner": owner,
                        "request_id": request_id,
                    },
                )
                return False
            with _tasks_lock:
                if task_id in _active_threads:
                    log.warning(
                        "spawn rejected: task already active",
                        extra={
                            "event": "task_spawn_rejected",
                            "task_id": task_id,
                            "task_type": name,
                            "owner": owner,
                            "request_id": request_id,
                        },
                    )
                    return False
            flag.write_text(str(os.getpid()), encoding="utf-8")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

    def wrap() -> None:
        with bind_log_context(request_id=request_id, task_id=task_id, user=owner):
            log.info(
                "task thread started",
                extra={
                    "event": "task_thread_start",
                    "task_id": task_id,
                    "task_type": name,
                    "owner": owner,
                },
            )
            try:
                target(task_id, params)
            except Exception:
                record_exception("task_thread_unhandled")
                log.exception(
                    "unhandled exception in task thread",
                    extra={
                        "event": "task_thread_unhandled_exception",
                        "task_id": task_id,
                        "task_type": name,
                        "owner": owner,
                    },
                )
                raise
            finally:
                flag.unlink(missing_ok=True)
                with _tasks_lock:
                    _active_threads.pop(task_id, None)
                    _cancel_flags.pop(task_id, None)
                log.info(
                    "task thread finished",
                    extra={
                        "event": "task_thread_end",
                        "task_id": task_id,
                        "task_type": name,
                        "owner": owner,
                    },
                )

    cancel_ev = threading.Event()
    t = threading.Thread(target=wrap, daemon=True, name=name)
    with _tasks_lock:
        _active_threads[task_id] = t
        _cancel_flags[task_id] = cancel_ev
    t.start()
    log.info(
        "task spawned",
        extra={
            "event": "task_spawned",
            "task_id": task_id,
            "task_type": name,
            "owner": owner,
            "request_id": request_id,
        },
    )
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
        settings = _load_settings_for_user(username)
        try:
            use_model = resolve_theme_idea_model(settings)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
        style_clause = f" Style preference: {style_hint}" if style_hint else ""
        sys_prompt = (
            "You are a creative story producer for short-form video. "
            "Each time output exactly one story theme or pitch in one to three sentences. "
            "No numbering, no preamble or explanation. "
            "Make it emotionally engaging, visually vivid, and suitable for video."
        )
        user_prompt = (
            f"Generate one fresh, distinctive short-video story idea.{style_clause}"
        )
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
        record_exception("api_theme_generate_idea")
        log.exception(
            "api_task_theme_generate_idea failed",
            extra={"event": "api_theme_generate_idea_failed"},
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


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
        settings = _load_settings_for_user(username)
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
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        record_exception("api_theme_next")
        log.exception(
            "api_task_theme_next failed",
            extra={"event": "api_theme_next_failed", "task_id": task_id},
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


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
            td = _task_dir(tid)
            if not (td / "storyboard.json").is_file():
                return
            gen_params = {
                "_owner": p.get("_owner", ""),
                "text_only_video": p.get("text_only_video"),
                "reference_images": p.get("reference_images") or [],
                "reference_videos": p.get("reference_videos") or [],
                "reference_video_descriptions": p.get("reference_video_descriptions") or [],
                "subject_lines": p.get("subject_lines") or [],
                "style": p.get("style") or "",
                "resolution": p.get("resolution"),
                "max_segment_seconds": p.get("max_segment_seconds"),
                "max_workers": p.get("max_workers"),
            }
            _run_generate_job(tid, gen_params)
        except Exception as e:
            _write_task_meta(tid, {"status": "failed", "error": str(e)})
            record_exception("run_pipeline")
            log.exception(
                "run pipeline failed",
                extra={"event": "run_pipeline_failed", "task_id": tid},
            )
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
    """SSE 实时进度流——基于文件轮询，完全跨 worker 进程。"""
    username = _require_user()
    if not _check_task_access(task_id, username):
        return jsonify({"error": "unknown task"}), 404
    _terminal = {"done", "failed", "cancelled"}

    def generate():
        sent_count = 0
        last_status: str | None = None
        subjects_sent = False
        tick = 0

        meta = _read_task_meta(task_id)
        if meta:
            yield f"data: {json.dumps({'type': 'snapshot', **meta}, ensure_ascii=False)}\n\n"
            sent_count = len(meta.get("progress", []))
            last_status = meta.get("status")
            subjects = _read_subjects(task_id)
            if subjects:
                yield f"data: {json.dumps({'type': 'subjects_ready', 'count': len(subjects), 'subjects': subjects}, ensure_ascii=False)}\n\n"
                subjects_sent = True

        if last_status in _terminal:
            return

        while True:
            time.sleep(1)
            tick += 1
            meta = _read_task_meta(task_id)
            if not meta:
                if tick % 15 == 0:
                    yield ": keepalive\n\n"
                continue

            progress = meta.get("progress", [])
            for entry in progress[sent_count:]:
                yield f"data: {json.dumps({'type': 'progress', 'msg': entry.get('msg',''), 't': entry.get('t','')}, ensure_ascii=False)}\n\n"
            sent_count = len(progress)

            status = meta.get("status", "")
            if status and status != last_status:
                ev: dict[str, Any] = {"type": "status", "status": status}
                if status == "failed":
                    ev["error"] = meta.get("error", "")
                if status == "storyboard_ready":
                    ev["shot_count"] = meta.get("shot_count", 0)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                last_status = status

            if not subjects_sent:
                subjects = _read_subjects(task_id)
                if subjects:
                    yield f"data: {json.dumps({'type': 'subjects_ready', 'count': len(subjects), 'subjects': subjects}, ensure_ascii=False)}\n\n"
                    subjects_sent = True

            if status in _terminal:
                break

            if tick % 15 == 0:
                yield ": keepalive\n\n"

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
        settings = _load_settings_for_user(username)
        try:
            use_model = resolve_theme_story_model(settings)
        except ValueError as e:
            return jsonify({"error": str(e), "result": ""}), 400
        client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
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
        record_exception("api_translate")
        log.exception(
            "api_translate failed",
            extra={"event": "api_translate_failed"},
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


# ---------------------------------------------------------------------------
# 主体自动生成（分镜完成后调用）
# ---------------------------------------------------------------------------

_SUBJECT_GEN_SYSTEM = """You are a character visual-tag extractor for AI video generation.
Given a storyboard, extract each named character and output a detailed **sentence-based visual appearance description** for each.

Output strict JSON array only (no Markdown):
[
  {
    "name": "Character name as used in shots",
    "name_zh": "Same name in Simplified Chinese characters (natural translation if the story is English)",
    "region_en": "Short label for regional/casting appearance: e.g. Eastern Asian, Western, South Asian, Middle Eastern, African, Latino, mixed, or ambiguous — NOT nationality/citizenship, only observable look for video casting",
    "region_zh": "Same regional label as region_en, written in Simplified Chinese characters",
    "description_en": "A series of English sentences describing visual attributes in this order: gender, approximate age range, build/body type, regional appearance (must match region_en, one sentence e.g. 'The regional appearance is Eastern Asian.' or 'The regional appearance is Western.'), hair color, hair type (straight/wavy/curly), hair length (short/medium/long), eye color, skin tone, upper clothing, lower clothing, footwear. Example: 'The gender is female. The age is around 20s. The build is slim. The regional appearance is Eastern Asian. The hair is black. The hair type is straight. The hair length is long. The eye color is brown. The skin tone is fair. The upper clothing is a white blouse. The lower clothing is blue jeans. The footwear is white sneakers.'",
    "description_zh": "Simplified Chinese mirror of description_en: same sentence-based pattern, one visual attribute per sentence"
  }
]

Rules:
- Only include named characters that appear in dialogue or character_action fields.
- region_en and region_zh MUST be filled with a concise casting-region label; infer from story context, names, and dialogue when possible.
- description_en must include one sentence "The regional appearance is …" that agrees with region_en.
- description_en must be a series of complete English sentences, each describing one visual attribute in the format "The [attribute] is [value]." — NOT comma-separated tags.
- Include ONLY observable appearance: gender, age, build, regional appearance (Eastern vs Western etc.), hair (color + type + length), eye color, skin tone, main clothing top/bottom, footwear.
- EXCLUDE: expressions, emotions, actions, personality, detailed cosmetics, accessories unless plot-critical.
- Keep each description covering about 11-19 visual attributes per character (including regional appearance).
- Output JSON array only, no extra text."""


def _generate_subjects_from_storyboard(doc: StoryboardDocument, settings: Any) -> list[dict[str, Any]]:
    """从分镜文档中提取角色列表，调用 LLM 生成详细主体描述。"""
    client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.base_url)
    use_model = resolve_theme_story_model(settings)

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

    import logging as _logging
    _log = _logging.getLogger(__name__)

    try:
        completion = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": _SUBJECT_GEN_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()
        _log.info("Subject LLM raw response (first 500 chars): %s", raw[:500])
        # 提取 JSON 数组
        import re as _re
        m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end + 1])
            _log.info("Subject parsed OK: %d characters", len(parsed))
            return parsed
        _log.warning("Subject LLM response did not contain a valid JSON array")
    except Exception as exc:
        _log.error("Subject generation failed: %s", exc, exc_info=True)
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
        "max_workers": body.get("max_workers"),
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
    if not is_current_user_admin():
        return jsonify({"error": "需要管理员权限"}), 403
    return jsonify(_workspace_disk_usage())


@app.route("/api/admin/cleanup", methods=["POST"])
def api_admin_cleanup():
    """手动触发缓存清理（支持 dry_run 预览）。"""
    _require_user()
    if not is_current_user_admin():
        return jsonify({"error": "需要管理员权限"}), 403
    dry_run = request.json.get("dry_run", False) if request.is_json else False
    result = _cleanup_old_tasks(dry_run=dry_run, ttl_days=_get_task_ttl_days())
    result["freed_mb"] = round(result["freed_bytes"] / (1024 * 1024), 1)
    return jsonify(result)


# ---------------------------------------------------------------------------
# IP 管理 API — 已迁移至 bp_ip.py 蓝图
# ---------------------------------------------------------------------------


def _update_task_meta(task_dir: Path, updates: dict[str, Any]) -> None:
    """更新任务元数据 JSON。"""
    meta_path = task_dir / "task.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    prev_status = str(meta.get("status") or "")
    meta.update(updates)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    status = str(meta.get("status") or "")
    task_type = str(meta.get("type") or "unknown")
    task_id = str(meta.get("task_id") or task_dir.name)
    owner = str(meta.get("owner") or "")
    if status and status != prev_status:
        record_task_event(task_type, status)
        log.info(
            "task status updated (bp)",
            extra={
                "event": "task_status_updated",
                "task_id": task_id,
                "task_type": task_type,
                "task_status": status,
                "owner": owner,
            },
        )
    if updates.get("error"):
        record_exception("task_failure")
        log.error(
            "task error recorded (bp)",
            extra={
                "event": "task_error",
                "task_id": task_id,
                "task_type": task_type,
                "task_status": status or "unknown",
                "owner": owner,
            },
        )




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


def _migrate_user_config_deltas() -> None:
    """将历史用户配置收敛为差异集并做一次性备份。"""
    users_dir = get_data_config_dir() / "users"
    if not users_dir.is_dir():
        return
    global_cfg = _get_global_config()
    migrated = 0
    for p in sorted(users_dir.glob("*/config.json")):
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
        except (json.JSONDecodeError, OSError):
            continue
        normalized = normalize_user_config_delta(global_cfg, raw)
        if normalized == raw:
            continue
        backup = p.with_name("config.backup.json")
        if not backup.exists():
            backup.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        p.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        migrated += 1
    if migrated:
        log.info(
            "user config deltas migrated",
            extra={"event": "startup_user_config_migrated", "migrated_users": migrated},
        )


def _start_cleanup_timer() -> None:
    """Start a daemon thread that runs cleanup periodically."""
    import time

    def _loop() -> None:
        while True:
            time.sleep(_CLEANUP_INTERVAL_SEC)
            try:
                result = _cleanup_old_tasks(ttl_days=_get_task_ttl_days())
                if result["deleted"]:
                    freed_mb = round(result["freed_bytes"] / (1024 * 1024), 1)
                    log.info(
                        "periodic cleanup completed",
                        extra={
                            "event": "periodic_cleanup",
                            "deleted_count": len(result["deleted"]),
                            "freed_mb": freed_mb,
                        },
                    )
            except Exception as e:
                record_exception("cleanup_loop")
                log.exception(
                    "periodic cleanup failed",
                    extra={"event": "periodic_cleanup_failed"},
                )

    t = threading.Thread(target=_loop, daemon=True, name="cleanup-timer")
    t.start()


def _get_task_ttl_days() -> int:
    """从全局配置读取 task_ttl_days，避免在启动时依赖完整 Settings（可能缺 API Key）。"""
    cfg = _get_global_config()
    try:
        return int(cfg.get("task_ttl_days", 7))
    except (TypeError, ValueError):
        return 7


# ---------------------------------------------------------------------------
# 初始化蓝图依赖（必须在所有函数定义之后）
# ---------------------------------------------------------------------------
init_ip_blueprint(
    require_user=_require_user,
    load_settings_for_user=_load_settings_for_user,
    spawn=_spawn,
    sse_push=_sse_push,
    cancel_flags=_cancel_flags,
    task_dir_fn=_task_dir,
    update_task_meta=_update_task_meta,
    ensure_workspace=_ensure_workspace,
    get_user_workspace_dir_fn=get_user_workspace_dir,
)


def main() -> None:
    try:
        import dashscope
        cfg = _get_global_config()
        dashscope.base_http_api_url = cfg.get(
            "dashscope_api_base", "https://dashscope.aliyuncs.com/api/v1"
        )
    except Exception:
        record_exception("dashscope_init")
        log.exception(
            "dashscope base url init failed",
            extra={"event": "dashscope_init_failed"},
        )
    _ensure_workspace()
    _migrate_legacy_tasks()
    _migrate_user_config_deltas()
    ttl = _get_task_ttl_days()
    result = _cleanup_old_tasks(ttl_days=ttl)
    if result["deleted"]:
        freed_mb = round(result["freed_bytes"] / (1024 * 1024), 1)
        log.info(
            "startup cleanup completed",
            extra={
                "event": "startup_cleanup",
                "deleted_count": len(result["deleted"]),
                "freed_mb": freed_mb,
            },
        )
    _start_cleanup_timer()
    import socket
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        record_exception("detect_local_ip")
        log.exception(
            "detect local ip failed",
            extra={"event": "local_ip_detect_failed"},
        )
    log.info(
        "web ui startup",
        extra={
            "event": "web_startup",
            "path": f"http://127.0.0.1:8000,http://{local_ip}:8000",
        },
    )
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
