"""IP 管理 & 风格预设蓝图。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_from_directory

from video2text.core.ip_creator import (
    create_ip_from_proposal,
    generate_character_images,
    generate_ip_proposal,
    refine_ip_section,
)
from video2text.core.ip_manager import (
    IPProfile,
    delete_ip,
    get_character_reference_path,
    list_ips,
    load_ip,
    save_character_reference_image,
    save_ip,
    update_character_reference_in_profile,
)
from video2text.core.styles import get_all_style_presets, get_style_by_id, search_styles
from video2text.core.theme import (
    generate_ip_story_outline,
    generate_storyboard_from_ip,
)
from video2text.pipeline.generator import CancellationError

bp = Blueprint("ip", __name__)

# ---------------------------------------------------------------------------
# 运行时依赖：由 app.py 注册蓝图时注入
# ---------------------------------------------------------------------------
_deps: dict[str, Any] = {}


def init_ip_blueprint(
    *,
    require_user,
    load_settings_for_user,
    spawn,
    sse_push,
    cancel_flags,
    task_dir_fn,
    update_task_meta,
    ensure_workspace,
    get_user_workspace_dir_fn,
):
    """注入 app.py 中的共享函数/状态，避免循环导入。"""
    _deps["require_user"] = require_user
    _deps["load_settings_for_user"] = load_settings_for_user
    _deps["spawn"] = spawn
    _deps["sse_push"] = sse_push
    _deps["cancel_flags"] = cancel_flags
    _deps["task_dir"] = task_dir_fn
    _deps["update_task_meta"] = update_task_meta
    _deps["ensure_workspace"] = ensure_workspace
    _deps["get_user_workspace_dir"] = get_user_workspace_dir_fn


# ---------------------------------------------------------------------------
# 风格预设
# ---------------------------------------------------------------------------

@bp.route("/api/styles")
def api_styles():
    q = request.args.get("q", "").strip()
    if q:
        return jsonify(search_styles(q))
    return jsonify(get_all_style_presets())


@bp.route("/api/styles/<style_id>")
def api_style_detail(style_id: str):
    style = get_style_by_id(style_id)
    if not style:
        return jsonify({"error": "风格不存在"}), 404
    return jsonify(style)


# ---------------------------------------------------------------------------
# IP CRUD
# ---------------------------------------------------------------------------

@bp.route("/api/ips")
def api_list_ips():
    user = _deps["require_user"]()
    ips = list_ips(user)
    return jsonify([ip.to_dict() for ip in ips])


@bp.route("/api/ip/<ip_id>")
def api_get_ip(ip_id: str):
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    return jsonify(ip.to_dict())


@bp.route("/api/ip/create", methods=["POST"])
def api_create_ip():
    user = _deps["require_user"]()
    data = request.json or {}
    seed = data.get("seed_idea", "").strip()
    if not seed:
        return jsonify({"error": "请输入种子创意"}), 400
    style_preset_id = data.get("style_preset_id", "")
    try:
        settings = _deps["load_settings_for_user"](user)
        proposal = generate_ip_proposal(seed, settings, style_preset_id=style_preset_id)
        return jsonify({"proposal": proposal})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/ip/confirm", methods=["POST"])
def api_confirm_ip():
    """确认 IP 提案并保存。角色图生成不再在此处同步执行。"""
    user = _deps["require_user"]()
    data = request.json or {}
    proposal = data.get("proposal")
    if not proposal:
        return jsonify({"error": "缺少 proposal"}), 400
    try:
        profile = create_ip_from_proposal(proposal, user)
        return jsonify({"ip": profile.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/ip/<ip_id>", methods=["PUT"])
def api_update_ip(ip_id: str):
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.json or {}
    updated = IPProfile.from_dict({**ip.to_dict(), **data, "id": ip_id})
    save_ip(user, updated)
    return jsonify(updated.to_dict())


@bp.route("/api/ip/<ip_id>", methods=["DELETE"])
def api_delete_ip(ip_id: str):
    user = _deps["require_user"]()
    if delete_ip(user, ip_id):
        return jsonify({"ok": True})
    return jsonify({"error": "IP 不存在"}), 404


# ---------------------------------------------------------------------------
# 角色图管理（异步）
# ---------------------------------------------------------------------------

@bp.route("/api/ip/<ip_id>/generate-images", methods=["POST"])
def api_generate_character_images(ip_id: str):
    """异步生成所有角色参考图。返回 task_id 供前端轮询进度。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404

    char_ids = (request.json or {}).get("char_ids")

    task_id = uuid.uuid4().hex[:16]
    _deps["ensure_workspace"](user)
    task_dir = _deps["get_user_workspace_dir"](user) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task_id": task_id,
        "owner": user,
        "type": "ip-images",
        "ip_id": ip_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (task_dir / "task.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def _run_image_gen(tid: str, params: dict) -> None:
        td = _deps["task_dir"](tid)
        owner = params["_owner"]
        ip_obj = load_ip(owner, params["ip_id"])
        if not ip_obj:
            _deps["update_task_meta"](td, {"status": "failed", "error": "IP 不存在"})
            return
        try:
            _deps["update_task_meta"](td, {"status": "generating"})
            settings = _deps["load_settings_for_user"](owner)
            ip_obj = generate_character_images(
                ip_obj, owner, settings,
                char_ids=params.get("char_ids"),
                progress_cb=lambda m: _deps["sse_push"](tid, m),
            )
            result = {
                "status": "done",
                "ip": ip_obj.to_dict(),
            }
            _deps["update_task_meta"](td, result)
            _deps["sse_push"](tid, "角色图生成完成！")
        except Exception as e:
            _deps["update_task_meta"](td, {"status": "failed", "error": str(e)})
            _deps["sse_push"](tid, f"错误：{e}")

    ip_params = {"_owner": user, "ip_id": ip_id, "char_ids": char_ids}
    if not _deps["spawn"]("ip-images", _run_image_gen, task_id, ip_params):
        return jsonify({"error": "角色图生成任务已在执行中"}), 409
    return jsonify({"task_id": task_id, "status": "pending"})


@bp.route("/api/ip/<ip_id>/character/<char_id>/regenerate", methods=["POST"])
def api_regenerate_character_image(ip_id: str, char_id: str):
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    try:
        settings = _deps["load_settings_for_user"](user)
        ip = generate_character_images(ip, user, settings, char_ids=[char_id])
        return jsonify({"ip": ip.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/ip/<ip_id>/character/<char_id>/image", methods=["GET"])
def api_character_image(ip_id: str, char_id: str):
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    ref_path = char.reference_image_path.strip()
    if not ref_path:
        return jsonify({"error": "暂无参考图"}), 404
    p = Path(ref_path)
    if not p.is_file():
        p = get_character_reference_path(user, ip_id, char_id)
    if not p.is_file():
        return jsonify({"error": "参考图文件不存在"}), 404
    return send_from_directory(p.parent, p.name, mimetype="image/jpeg")


@bp.route("/api/ip/<ip_id>/character/<char_id>/upload", methods=["POST"])
def api_upload_character_image(ip_id: str, char_id: str):
    user = _deps["require_user"]()
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

    fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    tmp = Path(tmp_path)
    f.save(str(tmp))
    try:
        dest = save_character_reference_image(user, ip_id, char_id, tmp)
        ip = update_character_reference_in_profile(
            user, ip_id, char_id, str(dest), ref_type="uploaded",
        )
        return jsonify({"ip": ip.to_dict() if ip else {}})
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# IP 故事大纲生成（独立于分镜）
# ---------------------------------------------------------------------------

@bp.route("/api/ip/<ip_id>/story", methods=["POST"])
def api_ip_story(ip_id: str):
    """仅生成故事大纲（Phase 1），不生成分镜。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.json or {}
    theme_hint = data.get("theme_hint", "")
    min_shots = int(data.get("min_shots", 8))
    max_shots = int(data.get("max_shots", 16))
    try:
        settings = _deps["load_settings_for_user"](user)
        outline = generate_ip_story_outline(
            ip, settings,
            theme_hint=theme_hint,
            min_shots=min_shots,
            max_shots=max_shots,
        )
        return jsonify({"outline": outline})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# AI 润色
# ---------------------------------------------------------------------------

@bp.route("/api/ip/<ip_id>/refine", methods=["POST"])
def api_ip_refine(ip_id: str):
    """AI 润色：用户提意见，AI 结合全局 context 修改指定段落。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.json or {}
    section = data.get("section", "")
    instruction = data.get("instruction", "").strip()
    current_content = data.get("current_content", "")
    if not instruction:
        return jsonify({"error": "请输入修改意见"}), 400
    try:
        settings = _deps["load_settings_for_user"](user)
        result = refine_ip_section(
            ip, settings,
            section=section,
            instruction=instruction,
            current_content=current_content,
        )
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# IP 主题生成任务（分镜 + 可选视频）
# ---------------------------------------------------------------------------

@bp.route("/api/task/ip-theme", methods=["POST"])
def api_task_ip_theme():
    """基于 IP 的主题生成任务（生成分镜 + 视频）。"""
    user = _deps["require_user"]()
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
    story_outline = data.get("story_outline")

    task_id = uuid.uuid4().hex[:16]
    _deps["ensure_workspace"](user)
    task_dir = _deps["get_user_workspace_dir"](user) / task_id
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

    def _run_ip_theme_job(tid: str, params: dict) -> None:
        td = _deps["task_dir"](tid)
        owner = params["_owner"]
        ip_obj = load_ip(owner, params["ip_id"])
        if not ip_obj:
            _deps["update_task_meta"](td, {"status": "failed", "error": "IP 不存在"})
            return
        try:
            _deps["update_task_meta"](td, {"status": "generating_storyboard"})
            _deps["sse_push"](tid, "正在生成 IP 分镜…")

            settings = _deps["load_settings_for_user"](owner)
            doc = generate_storyboard_from_ip(
                ip_obj, settings,
                theme_hint=params.get("theme_hint", ""),
                min_shots=int(params.get("min_shots", 8)),
                max_shots=int(params.get("max_shots", 16)),
                story_outline=params.get("story_outline"),
            )
            doc.save_json(td / "storyboard.json")
            doc.save_markdown(td / "storyboard.md")
            _deps["update_task_meta"](td, {"status": "storyboard_ready"})
            _deps["sse_push"](tid, f"分镜已生成：{len(doc.shots)} 个镜头")

            if params.get("generate_video", True):
                _deps["update_task_meta"](td, {"status": "generating_video"})
                _deps["sse_push"](tid, "正在生成 IP 视频…")

                from video2text.pipeline.generator import run_ip_storyboard_generation
                run_ip_storyboard_generation(
                    doc, ip_obj, settings,
                    segments_dir=td / "segments",
                    output_mp4=td / "output.mp4",
                    progress_cb=lambda m: _deps["sse_push"](tid, m),
                    meta_update=lambda d: _deps["update_task_meta"](td, d),
                    cancel_event=_deps["cancel_flags"].get(tid),
                )
                _deps["update_task_meta"](td, {"status": "done"})
                _deps["sse_push"](tid, "IP 视频生成完成！")
            else:
                _deps["update_task_meta"](td, {"status": "done"})

        except CancellationError:
            _deps["update_task_meta"](td, {"status": "cancelled"})
            _deps["sse_push"](tid, "任务已取消")
        except Exception as e:
            _deps["update_task_meta"](td, {"status": "failed", "error": str(e)})
            _deps["sse_push"](tid, f"错误：{e}")

    ip_params = {
        "_owner": user,
        "ip_id": ip_id,
        "theme_hint": theme_hint,
        "min_shots": min_shots,
        "max_shots": max_shots,
        "generate_video": generate_video,
        "story_outline": story_outline,
    }
    if not _deps["spawn"]("ip-theme", _run_ip_theme_job, task_id, ip_params):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"task_id": task_id, "status": "pending"})
