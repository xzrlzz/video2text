"""IP 管理 & 风格预设蓝图。"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_from_directory

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
from video2text.core.voices import get_all_voice_presets, get_voice_by_id, search_voices
from video2text.core.theme import (
    generate_ip_story_outline,
    generate_storyboard_from_ip,
)
from video2text.pipeline.generator import CancellationError
from video2text.web.telemetry import get_request_id, record_exception

bp = Blueprint("ip", __name__)
log = logging.getLogger(__name__)

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
# 音色预设
# ---------------------------------------------------------------------------

@bp.route("/api/voices")
def api_voices():
    q = request.args.get("q", "").strip()
    if q:
        return jsonify(search_voices(q))
    return jsonify(get_all_voice_presets())


@bp.route("/api/voices/<voice_id>")
def api_voice_detail(voice_id: str):
    v = get_voice_by_id(voice_id)
    if not v:
        return jsonify({"error": "音色不存在"}), 404
    return jsonify(v.to_dict())


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
    data = request.get_json(silent=True) or {}
    seed = data.get("seed_idea", "").strip()
    if not seed:
        return jsonify({"error": "请输入种子创意"}), 400
    style_preset_id = data.get("style_preset_id", "")
    try:
        settings = _deps["load_settings_for_user"](user)
        proposal = generate_ip_proposal(seed, settings, style_preset_id=style_preset_id)
        return jsonify({"proposal": proposal})
    except Exception as e:
        record_exception("api_ip_create")
        log.exception(
            "api_ip_create failed",
            extra={
                "event": "api_ip_create_failed",
                "request_id": get_request_id(),
                "user": user,
                "ip_id": "",
            },
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


@bp.route("/api/ip/confirm", methods=["POST"])
def api_confirm_ip():
    """确认 IP 提案并保存。角色图生成不再在此处同步执行。"""
    user = _deps["require_user"]()
    data = request.get_json(silent=True) or {}
    proposal = data.get("proposal")
    if not proposal:
        return jsonify({"error": "缺少 proposal"}), 400
    try:
        profile = create_ip_from_proposal(proposal, user)
        return jsonify({"ip": profile.to_dict()})
    except Exception as e:
        record_exception("api_ip_confirm")
        log.exception(
            "api_ip_confirm failed",
            extra={
                "event": "api_ip_confirm_failed",
                "request_id": get_request_id(),
                "user": user,
            },
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


@bp.route("/api/ip/<ip_id>", methods=["PUT"])
def api_update_ip(ip_id: str):
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.get_json(silent=True) or {}
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

    char_ids = (request.get_json(silent=True) or {}).get("char_ids")

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
    _deps["update_task_meta"](task_dir, meta)

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

            def _on_char_done(updated_profile):
                _deps["update_task_meta"](td, {"status": "generating", "ip": updated_profile.to_dict()})

            ip_obj = generate_character_images(
                ip_obj, owner, settings,
                char_ids=params.get("char_ids"),
                progress_cb=lambda m: _deps["sse_push"](tid, m),
                char_done_cb=_on_char_done,
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
            record_exception("ip_images_job")
            log.exception(
                "ip image generation job failed",
                extra={
                    "event": "ip_images_job_failed",
                    "task_id": tid,
                    "user": owner,
                    "ip_id": params.get("ip_id", ""),
                },
            )

    ip_params = {"_owner": user, "ip_id": ip_id, "char_ids": char_ids}
    if not _deps["spawn"]("ip-images", _run_image_gen, task_id, ip_params):
        return jsonify({"error": "角色图生成任务已在执行中"}), 409
    return jsonify({"task_id": task_id, "status": "pending"})


@bp.route("/api/ip/<ip_id>/character/<char_id>/regenerate", methods=["POST"])
def api_regenerate_character_image(ip_id: str, char_id: str):
    """异步重新生成单个角色参考图，返回 task_id 供前端轮询进度。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404

    data = request.get_json(silent=True) or {}
    auto_fix = data.get("auto_fix", False)
    error_reason = data.get("error_reason", "版权侵权")

    task_id = uuid.uuid4().hex[:16]
    _deps["ensure_workspace"](user)
    task_dir = _deps["get_user_workspace_dir"](user) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task_id": task_id,
        "owner": user,
        "type": "ip-char-regen",
        "ip_id": ip_id,
        "char_id": char_id,
        "char_name": char.name,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _deps["update_task_meta"](task_dir, meta)

    def _run_char_regen(tid: str, params: dict) -> None:
        td = _deps["task_dir"](tid)
        owner = params["_owner"]
        ip_obj = load_ip(owner, params["ip_id"])
        if not ip_obj:
            _deps["update_task_meta"](td, {"status": "failed", "error": "IP 不存在"})
            return
        c = ip_obj.get_character(params["char_id"])
        if not c:
            _deps["update_task_meta"](td, {"status": "failed", "error": "角色不存在"})
            return
        try:
            _deps["update_task_meta"](td, {"status": "generating"})
            settings = _deps["load_settings_for_user"](owner)

            if params.get("auto_fix"):
                _deps["sse_push"](tid, f"检测到内容策略问题，正在用 AI 修正「{c.name}」的描述…")
                from video2text.core.ip_creator import _auto_fix_character_description
                fixed = _auto_fix_character_description(
                    c, ip_obj, settings, params.get("error_reason", "版权侵权"),
                )
                if fixed:
                    c.visual_description = fixed
                    save_ip(owner, ip_obj)
                    _deps["sse_push"](tid, f"描述已修正，开始重新生成图片…")
                else:
                    _deps["sse_push"](tid, "AI 未能修正描述，尝试直接重新生成…")

            _deps["sse_push"](tid, f"正在为「{c.name}」生成参考图…")
            ip_obj = generate_character_images(
                ip_obj, owner, settings,
                char_ids=[params["char_id"]],
                progress_cb=lambda m: _deps["sse_push"](tid, m),
            )
            updated = ip_obj.get_character(params["char_id"])
            char_ok = bool(updated and updated.reference_image_path)

            result = {
                "status": "done",
                "ip": ip_obj.to_dict(),
                "char_ok": char_ok,
                "auto_fixed": params.get("auto_fix", False),
            }
            _deps["update_task_meta"](td, result)
            if char_ok:
                _deps["sse_push"](tid, f"「{c.name}」参考图生成完成！")
            else:
                from video2text.core.ip_creator import _classify_image_error
                _deps["update_task_meta"](td, {
                    "status": "done",
                    "char_ok": False,
                    "char_error_type": "generation_failed",
                })
                _deps["sse_push"](tid, f"「{c.name}」图片生成未成功")
        except Exception as e:
            err_str = str(e)
            from video2text.core.ip_creator import _classify_image_error
            error_type = _classify_image_error(err_str)
            _deps["update_task_meta"](td, {
                "status": "failed",
                "error": err_str,
                "error_type": error_type,
                "can_auto_fix": bool(error_type),
            })
            _deps["sse_push"](tid, f"错误：{e}")
            record_exception("ip_char_regen_job")
            log.exception(
                "ip char regen job failed",
                extra={
                    "event": "ip_char_regen_failed",
                    "task_id": tid,
                    "user": owner,
                    "ip_id": params.get("ip_id", ""),
                    "char_id": params.get("char_id", ""),
                },
            )

    regen_params = {
        "_owner": user,
        "ip_id": ip_id,
        "char_id": char_id,
        "auto_fix": auto_fix,
        "error_reason": error_reason,
    }
    if not _deps["spawn"]("ip-char-regen", _run_char_regen, task_id, regen_params):
        return jsonify({"error": "该角色正在生成中，请稍候"}), 409
    return jsonify({"task_id": task_id, "status": "pending"})


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
# 角色音色管理
# ---------------------------------------------------------------------------

@bp.route("/api/ip/<ip_id>/character/<char_id>/voice", methods=["PUT"])
def api_set_character_voice(ip_id: str, char_id: str):
    """设置角色音色（预置或克隆模式）。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    data = request.get_json(silent=True) or {}
    from video2text.core.ip_manager import VoiceProfile
    vp = VoiceProfile.from_dict({**char.voice_profile.to_dict(), **data})
    char.voice_profile = vp
    save_ip(user, ip)
    return jsonify({"ip": ip.to_dict()})


@bp.route("/api/ip/<ip_id>/character/<char_id>/voice/upload", methods=["POST"])
def api_upload_character_voice(ip_id: str, char_id: str):
    """上传角色参考音频（用于声音克隆或 wan2.7 reference_voice）。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    if "file" not in request.files:
        return jsonify({"error": "缺少音频文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    from video2text.core.ip_manager import VoiceProfile, get_character_voice_path
    dest = get_character_voice_path(user, ip_id, char_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dest))

    # 立即上传到 OSS 获取公网 URL（native 模式需要）
    oss_url = ""
    try:
        settings = _deps["load_settings_for_user"](user)
        from dashscope.utils.oss_utils import check_and_upload_local
        _, oss_url, _ = check_and_upload_local(
            settings.video_ref_model, str(dest), settings.dashscope_api_key, None,
        )
        log.info("Voice audio uploaded to OSS: char=%s, url=%s", char.name, oss_url[:80])
    except Exception:
        log.exception("Failed to upload voice audio to OSS for char %s", char.name)

    char.voice_profile = VoiceProfile(
        mode="clone",
        reference_audio_path=str(dest),
        reference_audio_url=oss_url,
        provider=char.voice_profile.provider or "cosyvoice",
    )
    save_ip(user, ip)
    return jsonify({"ip": ip.to_dict()})


@bp.route("/api/ip/<ip_id>/character/<char_id>/voice/preview", methods=["POST"])
def api_preview_character_voice(ip_id: str, char_id: str):
    """试听：用角色音色 TTS 一句话，返回音频 URL 或 base64。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    char = ip.get_character(char_id)
    if not char:
        return jsonify({"error": "角色不存在"}), 404
    if not char.voice_profile.is_configured:
        return jsonify({"error": "该角色尚未设置音色"}), 400
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        text = f"你好，我是{char.name}。"
    try:
        settings = _deps["load_settings_for_user"](user)
        from video2text.services.tts import get_tts_provider
        provider = get_tts_provider(settings)
        result = provider.synthesize(
            text=text,
            voice_id=char.voice_profile.effective_voice_id,
            model=settings.tts_model,
        )
        import base64
        audio_b64 = base64.b64encode(result.audio_data).decode("ascii")
        return jsonify({
            "audio_base64": audio_b64,
            "format": result.audio_format,
            "duration_ms": result.duration_ms,
        })
    except Exception as e:
        log.exception(
            "voice preview failed",
            extra={
                "event": "voice_preview_failed",
                "user": user,
                "ip_id": ip_id,
                "char_id": char_id,
                "voice_id": char.voice_profile.effective_voice_id,
            },
        )
        return jsonify({"error": str(e)}), 500


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
    data = request.get_json(silent=True) or {}
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
        ip.last_story_outline = outline
        save_ip(user, ip)
        return jsonify({"outline": outline})
    except Exception as e:
        record_exception("api_ip_story")
        log.exception(
            "api_ip_story failed",
            extra={
                "event": "api_ip_story_failed",
                "request_id": get_request_id(),
                "user": user,
                "ip_id": ip_id,
            },
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


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
    data = request.get_json(silent=True) or {}
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
        record_exception("api_ip_refine")
        log.exception(
            "api_ip_refine failed",
            extra={
                "event": "api_ip_refine_failed",
                "request_id": get_request_id(),
                "user": user,
                "ip_id": ip_id,
            },
        )
        return jsonify({"error": str(e), "request_id": get_request_id()}), 500


# ---------------------------------------------------------------------------
# 大纲保存 + 反馈系统
# ---------------------------------------------------------------------------

@bp.route("/api/ip/<ip_id>/outline", methods=["PUT"])
def api_save_outline(ip_id: str):
    """保存/更新故事大纲到 IP profile。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.get_json(silent=True) or {}
    outline = data.get("outline")
    if outline is None:
        return jsonify({"error": "缺少 outline"}), 400
    ip.last_story_outline = dict(outline) if isinstance(outline, dict) else {}
    save_ip(user, ip)
    return jsonify({"ok": True})


@bp.route("/api/ip/<ip_id>/feedback", methods=["GET"])
def api_get_feedback(ip_id: str):
    """查看 IP 的反馈历史。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    return jsonify({
        "feedback_log": [f.to_dict() for f in ip.feedback_log],
        "creative_guidelines": ip.creative_guidelines,
    })


@bp.route("/api/ip/<ip_id>/feedback", methods=["POST"])
def api_add_feedback(ip_id: str):
    """记录一条反馈并检查是否需要自动提炼 guidelines。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.get_json(silent=True) or {}

    from video2text.core.ip_manager import FeedbackEntry
    entry = FeedbackEntry(
        id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(timezone.utc).isoformat(),
        phase=str(data.get("phase", "")),
        section=str(data.get("section", "")),
        instruction=str(data.get("instruction", "")),
        before_snapshot=str(data.get("before_snapshot", ""))[:500],
        after_snapshot=str(data.get("after_snapshot", ""))[:500],
        accepted=bool(data.get("accepted", True)),
    )
    ip.feedback_log.append(entry)

    auto_distill = len(ip.feedback_log) % 5 == 0 and len(ip.feedback_log) > 0
    if auto_distill:
        try:
            settings = _deps["load_settings_for_user"](user)
            from video2text.core.ip_creator import distill_creative_guidelines
            ip.creative_guidelines = distill_creative_guidelines(ip, settings)
        except Exception:
            log.exception("auto distill guidelines failed")

    save_ip(user, ip)
    return jsonify({
        "ok": True,
        "feedback_count": len(ip.feedback_log),
        "auto_distilled": auto_distill,
        "creative_guidelines": ip.creative_guidelines,
    })


@bp.route("/api/ip/<ip_id>/feedback/distill", methods=["POST"])
def api_distill_guidelines(ip_id: str):
    """手动触发提炼创作指南。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    if not ip.feedback_log:
        return jsonify({"error": "尚无反馈记录"}), 400
    try:
        settings = _deps["load_settings_for_user"](user)
        from video2text.core.ip_creator import distill_creative_guidelines
        ip.creative_guidelines = distill_creative_guidelines(ip, settings)
        save_ip(user, ip)
        return jsonify({"creative_guidelines": ip.creative_guidelines})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/ip/<ip_id>/guidelines", methods=["PUT"])
def api_update_guidelines(ip_id: str):
    """手动编辑创作指南。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.get_json(silent=True) or {}
    guidelines = data.get("guidelines")
    if not isinstance(guidelines, list):
        return jsonify({"error": "guidelines 必须是列表"}), 400
    ip.creative_guidelines = [str(g) for g in guidelines]
    save_ip(user, ip)
    return jsonify({"creative_guidelines": ip.creative_guidelines})


# ---------------------------------------------------------------------------
# 分镜更新（翻译持久化、单镜头润色回写）
# ---------------------------------------------------------------------------

@bp.route("/api/task/<task_id>/storyboard", methods=["PUT"])
def api_update_storyboard(task_id: str):
    """更新 storyboard.json 中的 shot 数据（翻译字段、润色后的字段等）。"""
    user = _deps["require_user"]()
    td = _deps["task_dir"](task_id)
    sb_path = td / "storyboard.json"
    if not sb_path.is_file():
        return jsonify({"error": "storyboard not found"}), 404

    data = request.get_json(silent=True) or {}
    shots_update = data.get("shots")
    if not shots_update or not isinstance(shots_update, list):
        return jsonify({"error": "需要 shots 数组"}), 400

    sb = json.loads(sb_path.read_text(encoding="utf-8"))
    existing = sb.get("shots", [])

    for upd in shots_update:
        idx = upd.get("_index")
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(existing):
            continue
        for key, val in upd.items():
            if key == "_index":
                continue
            existing[idx][key] = val

    sb["shots"] = existing
    sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "shot_count": len(existing)})


@bp.route("/api/ip/<ip_id>/video-tasks", methods=["PUT"])
def api_ip_video_tasks(ip_id: str):
    """保存视频生成任务 ID 列表到 IP profile。"""
    user = _deps["require_user"]()
    ip = load_ip(user, ip_id)
    if not ip:
        return jsonify({"error": "IP 不存在"}), 404
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids")
    if not isinstance(task_ids, list):
        return jsonify({"error": "需要 task_ids 数组"}), 400
    ip.last_video_task_ids = [str(t) for t in task_ids][-20:]
    save_ip(user, ip)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# IP 主题生成任务（分镜 + 可选视频）
# ---------------------------------------------------------------------------

@bp.route("/api/task/ip-theme", methods=["POST"])
def api_task_ip_theme():
    """基于 IP 的主题生成任务（生成分镜 + 视频）。"""
    user = _deps["require_user"]()
    data = request.get_json(silent=True) or {}
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
    voice_mode = data.get("voice_mode", "")
    resolution = data.get("resolution", "")
    avg_shot_duration = float(data.get("avg_shot_duration", 2.5))
    target_duration = float(data.get("target_duration", 0))
    dialogue_mode = data.get("dialogue_mode", "normal")
    resume_task_id = data.get("resume_task_id", "").strip()

    _deps["ensure_workspace"](user)

    if resume_task_id:
        task_id = resume_task_id
        task_dir = _deps["get_user_workspace_dir"](user) / task_id
        if not task_dir.is_dir():
            return jsonify({"error": "续跑任务不存在"}), 404
        generate_video = True
    else:
        task_id = uuid.uuid4().hex[:16]
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
        "error": "",
        "progress": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _deps["update_task_meta"](task_dir, meta)

    def _run_ip_theme_job(tid: str, params: dict) -> None:
        td = _deps["task_dir"](tid)
        owner = params["_owner"]
        ip_obj = load_ip(owner, params["ip_id"])
        if not ip_obj:
            _deps["update_task_meta"](td, {"status": "failed", "error": "IP 不存在"})
            return
        try:
            settings = _deps["load_settings_for_user"](owner)
            is_resume = params.get("_resume", False)

            if is_resume and (td / "storyboard.json").is_file():
                _deps["sse_push"](tid, "续跑模式：加载已有分镜…")
                from video2text.core.storyboard import StoryboardDocument
                doc = StoryboardDocument.load_json(td / "storyboard.json")
            else:
                _deps["update_task_meta"](td, {"status": "generating_storyboard"})
                _deps["sse_push"](tid, "正在生成 IP 分镜…")
                doc = generate_storyboard_from_ip(
                    ip_obj, settings,
                    theme_hint=params.get("theme_hint", ""),
                    min_shots=int(params.get("min_shots", 8)),
                    max_shots=int(params.get("max_shots", 16)),
                    story_outline=params.get("story_outline"),
                    avg_shot_duration=float(params.get("avg_shot_duration", 2.5)),
                    target_duration=float(params.get("target_duration", 0)),
                    dialogue_mode=params.get("dialogue_mode", "normal"),
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
                    size=params.get("resolution") or None,
                    progress_cb=lambda m: _deps["sse_push"](tid, m),
                    meta_update=lambda d: _deps["update_task_meta"](td, d),
                    cancel_event=_deps["cancel_flags"].get(tid),
                    voice_mode=params.get("voice_mode") or None,
                    max_workers=settings.video_max_workers,
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
            record_exception("ip_theme_job")
            log.exception(
                "ip theme job failed",
                extra={
                    "event": "ip_theme_job_failed",
                    "task_id": tid,
                    "user": owner,
                    "ip_id": params.get("ip_id", ""),
                },
            )

    ip_params = {
        "_owner": user,
        "ip_id": ip_id,
        "theme_hint": theme_hint,
        "min_shots": min_shots,
        "max_shots": max_shots,
        "generate_video": generate_video,
        "story_outline": story_outline,
        "voice_mode": voice_mode,
        "resolution": resolution,
        "avg_shot_duration": avg_shot_duration,
        "target_duration": target_duration,
        "dialogue_mode": dialogue_mode,
        "_resume": bool(resume_task_id),
    }
    if not _deps["spawn"]("ip-theme", _run_ip_theme_job, task_id, ip_params):
        return (
            jsonify({"error": "该任务已有后台作业在执行，请等待完成或换一个新任务后再试"}),
            409,
        )
    return jsonify({"task_id": task_id, "status": "pending"})
