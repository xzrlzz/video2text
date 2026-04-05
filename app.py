#!/usr/bin/env python3
"""
video2text Web UI — Flask 后端：配置、任务、工作区缓存与断点续传。
本地运行：python app.py  →  http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, request, send_from_directory

from config import load_config_file, load_generation_extras, load_settings
from story_from_theme import generate_storyboard_from_theme
from storyboard import StoryboardDocument
from video_analyzer import (
    analyze_full_video_local,
    analyze_full_video_url,
    analyze_scene_segments,
    consolidate_storyboard,
)
from video_composer import concat_videos_ffmpeg, reencode_concat
from video_generator import (
    assign_generation_prompts,
    build_wan_multi_shot_prompt,
    chunk_shots_by_max_duration,
    download_url,
    extract_reference_slot_bodies,
    format_subject_prompt_block,
    generate_video_clip,
    generation_duration_cap,
    preflight_reference_urls_for_r2v,
    reference_subject_lock_hint,
    select_reference_indices_for_chunk,
    subject_block_for_chunk_refs,
)
from scene_detector import build_scene_segments

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
STATIC = ROOT / "static"
CONFIG_PATH = ROOT / "config.json"

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB uploads

_tasks_lock = threading.Lock()
_active_threads: dict[str, threading.Thread] = {}


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


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "" if not k else "****"
    return k[:4] + "…" + k[-4:]


def _get_config_for_api() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        example = ROOT / "config.example.json"
        if example.is_file():
            return json.loads(example.read_text(encoding="utf-8"))
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config_file(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def generate_clips_checkpointed(
    task_id: str,
    doc: StoryboardDocument,
    settings: Any,
    *,
    style: str = "",
    size: str | None = None,
    max_segment_seconds: float,
    subject_descriptions: list[str],
    reference_urls: list[str],
    reference_video_urls: list[str],
    reference_video_descriptions: list[str],
    progress_cb: Callable[[str], None],
    per_chunk_reference_filter: bool = True,
) -> Path:
    """顺序生成各段，已存在的 seg_*.mp4 跳过；最后拼接 output.mp4。"""
    task_d = _task_dir(task_id)
    segments_dir = task_d / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = task_d / "output.mp4"

    shots = doc.shots
    if not shots:
        raise ValueError("分镜为空")

    ref_u = [str(x).strip() for x in reference_urls if x and str(x).strip()]
    ref_v = [str(x).strip() for x in reference_video_urls if x and str(x).strip()]
    ref_d = [str(x).strip() for x in reference_video_descriptions if x and str(x).strip()]
    has_refs = bool(ref_u or ref_v)
    n_v, n_i = len(ref_v), len(ref_u)
    v_bodies, i_bodies = extract_reference_slot_bodies(
        subject_descriptions, ref_d, n_v, n_i
    )
    multi_ref = n_v + n_i > 1
    do_filter = bool(per_chunk_reference_filter and multi_ref and has_refs)

    dur_cap = generation_duration_cap(settings, has_refs)
    max_seg_eff = max(2.0, min(float(max_segment_seconds), float(dur_cap), 15.0))

    subject_block = format_subject_prompt_block(subject_descriptions)
    ref_hint = reference_subject_lock_hint(settings, has_refs)
    if ref_hint:
        subject_block = (
            f"{subject_block}。{ref_hint}" if subject_block.strip() else ref_hint
        )

    chunks = chunk_shots_by_max_duration(shots, max_seg_eff)
    if has_refs:
        ref_u, ref_v = preflight_reference_urls_for_r2v(settings, ref_u, ref_v)
        if do_filter:
            progress_cb(
                f"参考已解析：{n_v} 视频 + {n_i} 图，共 {len(chunks)} 段；"
                f"各段按分镜文本自动选取参考子集。"
            )
        else:
            progress_cb(
                f"参考已解析：{n_v} 视频 + {n_i} 图，共 {len(chunks)} 段待生成。"
            )

    _write_task_meta(
        task_id,
        {
            "segments_total": len(chunks),
            "segments_done": sum(
                1
                for p in segments_dir.glob("seg_*.mp4")
                if p.stat().st_size > 1024
            ),
        },
    )

    for idx, chunk in enumerate(chunks):
        seg_path = segments_dir / f"seg_{idx:03d}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 1024:
            progress_cb(f"使用缓存片段 {idx + 1}/{len(chunks)}：{seg_path.name}")
            continue

        if do_filter:
            sv, si = select_reference_indices_for_chunk(
                chunk,
                n_video=n_v,
                n_image=n_i,
                subject_descriptions=subject_descriptions,
                ref_video_descriptions=ref_d,
                enabled=True,
            )
            sb = subject_block_for_chunk_refs(sv, si, v_bodies, i_bodies, settings)
            cu = [ref_u[j] for j in si]
            cv = [ref_v[j] for j in sv]
            cd = [ref_d[j] for j in sv if j < len(ref_d)]
        else:
            sb = subject_block
            cu, cv, cd = ref_u, ref_v, ref_d
        prompt, dur = build_wan_multi_shot_prompt(
            chunk, doc, style, sb, max_duration=dur_cap
        )
        sub = ""
        if do_filter and len(cv) + len(cu) < n_v + n_i:
            sub = f" 本段参考 {len(cv)} 视频+{len(cu)} 图。"
        progress_cb(
            f"生成第 {idx + 1}/{len(chunks)} 段（约 {dur}s，{len(chunk)} 镜）。{sub}".strip()
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
        download_url(url, seg_path)
        done = sum(
            1
            for p in segments_dir.glob("seg_*.mp4")
            if p.stat().st_size > 1024
        )
        _write_task_meta(task_id, {"segments_done": done})
        progress_cb(f"第 {idx + 1} 段已保存。")

    paths = sorted(segments_dir.glob("seg_*.mp4"))
    if len(paths) != len(chunks):
        raise RuntimeError(
            f"片段数量不一致：期望 {len(chunks)}，实际 {len(paths)}，请删除损坏缓存后重试。"
        )

    progress_cb("正在拼接最终视频…")
    try:
        concat_videos_ffmpeg(paths, out_mp4)
    except subprocess.CalledProcessError:
        reencode_concat(paths, out_mp4)
    progress_cb(f"完成：{out_mp4.name}")
    return out_mp4


@app.route("/")
def index_page():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = _get_config_for_api()
    safe = dict(cfg)
    if "dashscope_api_key" in safe and safe["dashscope_api_key"]:
        safe["dashscope_api_key_masked"] = _mask_key(str(safe["dashscope_api_key"]))
        # 前端编辑时若留空 masked 则不覆盖
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


def _run_theme_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "theme_running", "type": "theme"})
        log("主题分镜任务已启动，正在加载配置…")
        settings = load_settings(str(CONFIG_PATH))
        theme = (params.get("theme") or "").strip()
        if not theme:
            raise ValueError("主题为空")
        log(
            f"主题创作中（模型 {params.get('model') or settings.theme_story_model or settings.vision_model}）…"
        )
        doc = generate_storyboard_from_theme(
            theme,
            settings,
            style_hint=str(params.get("style") or ""),
            min_shots=int(params.get("min_shots") or 8),
            max_shots=int(params.get("max_shots") or 24),
            model=params.get("model") or None,
        )
        td = _task_dir(task_id)
        doc.save_json(td / "storyboard.json")
        doc.save_markdown(td / "storyboard.md")
        _write_task_meta(
            task_id,
            {
                "status": "storyboard_ready",
                "shot_count": len(doc.shots),
            },
        )
        log(f"分镜已写入（{len(doc.shots)} 镜）。")
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _run_analyze_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        _write_task_meta(task_id, {"status": "analyze_running", "type": "analyze"})
        log("视频分析任务已启动，正在加载配置…")
        settings = load_settings(str(CONFIG_PATH))
        td = _task_dir(task_id)
        consolidate_flag = not params.get("skip_consolidate")
        style = str(params.get("style") or "")

        video_path = params.get("video_path")
        video_url = params.get("video_url")

        if video_url:
            log("整片分析（URL）…")
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
                log("场景切片分析…")
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
                    work_dir=str(wd),
                )
                doc, _ = analyze_scene_segments(result.segments, settings, style_hint=style)
                doc.source_video = str(vp.resolve())
                if consolidate_flag:
                    log("叙事整合…")
                    doc = consolidate_storyboard(doc, settings)
            else:
                log("整片分析（本地）…")
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
        _write_task_meta(
            task_id,
            {
                "status": "storyboard_ready",
                "shot_count": len(doc.shots),
            },
        )
        log(f"分镜已写入（{len(doc.shots)} 镜）。")
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _run_generate_job(task_id: str, params: dict[str, Any]) -> None:
    def log(m: str) -> None:
        _append_progress(task_id, m)

    try:
        td = _task_dir(task_id)
        sb = td / "storyboard.json"
        if not sb.is_file():
            raise FileNotFoundError("无 storyboard.json，请先生成分镜")

        _write_task_meta(task_id, {"status": "generating"})
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

        if text_only:
            ref_images, ref_videos = [], []
            ref_video_descs = []
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
        style = str(params.get("style") or "")
        resolution = params.get("resolution") or None

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
        )
        sb.write_text(
            json.dumps(doc.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log(
            f"模式：{'文生 t2v' if not has_refs else '参考生 r2v ' + settings.video_ref_model}"
        )

        generate_clips_checkpointed(
            task_id,
            doc,
            settings,
            style=style,
            size=resolution,
            max_segment_seconds=max_seg_eff,
            subject_descriptions=subject_lines,
            reference_urls=ref_images,
            reference_video_urls=ref_videos,
            reference_video_descriptions=ref_video_descs,
            progress_cb=log,
            per_chunk_reference_filter=extras.per_chunk_reference_filter,
        )

        _write_task_meta(task_id, {"status": "done"})
    except Exception as e:
        _write_task_meta(task_id, {"status": "failed", "error": str(e)})
        log(f"失败：{e}")


def _spawn(name: str, target: Callable[..., None], task_id: str, params: dict) -> bool:
    """启动后台线程。若该 task_id 已有作业在执行则返回 False（不重复排队）。"""

    def wrap() -> None:
        try:
            target(task_id, params)
        finally:
            with _tasks_lock:
                _active_threads.pop(task_id, None)

    t = threading.Thread(target=wrap, daemon=True, name=name)
    with _tasks_lock:
        if task_id in _active_threads:
            return False
        _active_threads[task_id] = t
    t.start()
    return True


@app.route("/api/task/theme", methods=["POST"])
def api_task_theme():
    body = request.get_json(force=True, silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"error": "需要 task_id，请先 POST /api/task/create"}), 400
    _write_task_meta(task_id, {"params": body})
    if not _spawn("theme", _run_theme_job, task_id, body):
        return (
            jsonify(
                {
                    "error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试",
                }
            ),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


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
            jsonify(
                {
                    "error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试",
                }
            ),
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
            jsonify(
                {
                    "error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试",
                }
            ),
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
                "reference_video_descriptions": p.get("reference_video_descriptions")
                or [],
                "subject_lines": p.get("subject_lines") or [],
                "style": p.get("style") or "",
                "resolution": p.get("resolution"),
                "max_segment_seconds": p.get("max_segment_seconds"),
            }
            _run_generate_job(tid, gen_params)
        except Exception as e:
            _write_task_meta(tid, {"status": "failed", "error": str(e)})
            _append_progress(tid, f"流水线失败：{e}")

    _write_task_meta(task_id, {"params_run": body})
    if not _spawn("run", pipeline, task_id, body):
        return (
            jsonify(
                {
                    "error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试",
                }
            ),
            409,
        )
    return jsonify({"ok": True, "task_id": task_id})


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
    return jsonify(
        {
            **meta,
            "storyboard": storyboard,
            "segments": segments,
            "output_url": out_url,
        }
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
        "reference_video_descriptions": body.get("reference_video_descriptions")
        or [],
        "subject_lines": body.get("subject_lines") or [],
        "style": body.get("style") or "",
        "resolution": body.get("resolution"),
        "max_segment_seconds": body.get("max_segment_seconds"),
    }
    _write_task_meta(task_id, {"status": "queued_generate", "params_resume": merged})
    if not _spawn("resume", _run_generate_job, task_id, merged):
        return (
            jsonify(
                {
                    "error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试",
                }
            ),
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


def _ensure_config_file() -> None:
    if not CONFIG_PATH.is_file():
        example = ROOT / "config.example.json"
        if example.is_file():
            CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    _ensure_workspace()
    _ensure_config_file()
    print("video2text Web UI: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
