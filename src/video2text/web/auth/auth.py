"""
认证蓝图：登录 / 登出 / 修改密码 / 用户管理 / 会话守卫。

用户数据存储在 data/config/users.json，密码使用 werkzeug PBKDF2 哈希。
首次启动时若无用户文件，自动创建默认管理员 admin / admin123。
每个用户包含 role 字段：admin（管理员）或 user（普通用户）。
"""

from __future__ import annotations

import json
import re
import secrets
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash

from video2text.utils.paths import get_static_dir

auth_bp = Blueprint("auth", __name__)

_USERS_FILE: Path | None = None
STATIC = get_static_dir()

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

ROLE_ADMIN = "admin"
ROLE_USER = "user"

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,32}$")


# ---------------------------------------------------------------------------
# 用户文件读写
# ---------------------------------------------------------------------------

def _users_path() -> Path:
    global _USERS_FILE
    if _USERS_FILE is None:
        raise RuntimeError("auth module not initialized, call init_auth() first")
    return _USERS_FILE


def _load_users() -> list[dict[str, Any]]:
    p = _users_path()
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_users(users: list[dict[str, Any]]) -> None:
    p = _users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_user(username: str) -> dict[str, Any] | None:
    for u in _load_users():
        if u.get("username") == username:
            return u
    return None


def _is_admin(username: str) -> bool:
    u = _find_user(username)
    return u is not None and u.get("role") == ROLE_ADMIN


def get_current_user() -> str | None:
    """返回当前会话中的用户名，未登录则返回 None。"""
    return session.get("user")


def is_current_user_admin() -> bool:
    user = get_current_user()
    return user is not None and _is_admin(user)


def _require_admin():
    """检查当前会话是否为管理员，否则返回 403 响应。返回 None 表示通过。"""
    user = session.get("user")
    if not user:
        return jsonify({"error": "未登录"}), 401
    if not _is_admin(user):
        return jsonify({"error": "需要管理员权限"}), 403
    return None


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_auth(app: Any, users_file: Path | None = None) -> None:
    """初始化认证模块：设置 secret_key、用户文件路径，注册蓝图。"""
    global _USERS_FILE

    if users_file is None:
        from video2text.utils.paths import get_data_config_dir
        users_file = get_data_config_dir() / "users.json"
    _USERS_FILE = users_file

    if not app.secret_key:
        app.secret_key = secrets.token_hex(32)

    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", 86400 * 7)

    _ensure_default_user()

    app.register_blueprint(auth_bp)

    @app.before_request
    def _check_auth():
        allowed_prefixes = ("/auth/", "/static/", "/health", "/metrics")
        if any(request.path.startswith(p) for p in allowed_prefixes):
            return None
        if request.path == "/login":
            return None
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "code": "AUTH_REQUIRED"}), 401
            return redirect("/login")
        return None


def _ensure_default_user() -> None:
    """首次启动若无用户则创建默认 admin；同时为旧数据补 role 字段。"""
    users = _load_users()
    if not users:
        users.append({
            "username": DEFAULT_USERNAME,
            "password_hash": generate_password_hash(DEFAULT_PASSWORD),
            "role": ROLE_ADMIN,
        })
        _save_users(users)
        return

    dirty = False
    for u in users:
        if "role" not in u:
            u["role"] = ROLE_ADMIN if u.get("username") == DEFAULT_USERNAME else ROLE_USER
            dirty = True
    if dirty:
        _save_users(users)


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------

def login_required(f):
    """显式装饰器（可选，before_request 已做全局拦截）。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "code": "AUTH_REQUIRED"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 路由：登录 / 登出 / 当前用户 / 修改密码
# ---------------------------------------------------------------------------

@auth_bp.route("/login")
def login_page():
    return send_from_directory(STATIC, "login.html")


@auth_bp.route("/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return jsonify({"error": "请输入用户名和密码"}), 400

    user = _find_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "用户名或密码错误"}), 401

    session.permanent = True
    session["user"] = username
    return jsonify({"ok": True, "username": username, "role": user.get("role", ROLE_USER)})


@auth_bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user", None)
    return jsonify({"ok": True})


@auth_bp.route("/auth/me", methods=["GET"])
def auth_me():
    user = session.get("user")
    if not user:
        return jsonify({"error": "未登录"}), 401
    u = _find_user(user)
    role = u.get("role", ROLE_USER) if u else ROLE_USER
    return jsonify({"username": user, "role": role})


@auth_bp.route("/auth/change-password", methods=["POST"])
def auth_change_password():
    user = session.get("user")
    if not user:
        return jsonify({"error": "未登录"}), 401

    body = request.get_json(force=True, silent=True) or {}
    old_pw = body.get("old_password") or ""
    new_pw = body.get("new_password") or ""

    if not old_pw or not new_pw:
        return jsonify({"error": "请填写旧密码和新密码"}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400

    users = _load_users()
    for u in users:
        if u["username"] == user:
            if not check_password_hash(u["password_hash"], old_pw):
                return jsonify({"error": "旧密码错误"}), 401
            u["password_hash"] = generate_password_hash(new_pw)
            _save_users(users)
            return jsonify({"ok": True})

    return jsonify({"error": "用户不存在"}), 404


# ---------------------------------------------------------------------------
# 路由：用户管理（仅管理员）
# ---------------------------------------------------------------------------

@auth_bp.route("/auth/users", methods=["GET"])
def auth_users_list():
    """列出所有用户（脱敏，不返回密码哈希）。"""
    deny = _require_admin()
    if deny:
        return deny
    users = _load_users()
    safe = [{"username": u["username"], "role": u.get("role", ROLE_USER)} for u in users]
    return jsonify({"users": safe})


@auth_bp.route("/auth/users", methods=["POST"])
def auth_users_create():
    """管理员创建新用户。"""
    deny = _require_admin()
    if deny:
        return deny

    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    role = (body.get("role") or ROLE_USER).strip()

    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "用户名只能包含字母、数字、下划线、短横线，长度 2-32"}), 400
    if not password:
        return jsonify({"error": "密码不能为空"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if role not in (ROLE_ADMIN, ROLE_USER):
        return jsonify({"error": f"角色只能是 {ROLE_ADMIN} 或 {ROLE_USER}"}), 400

    users = _load_users()
    if any(u["username"] == username for u in users):
        return jsonify({"error": f"用户 {username} 已存在"}), 409

    users.append({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
    })
    _save_users(users)
    return jsonify({"ok": True, "username": username, "role": role})


@auth_bp.route("/auth/users/<username>", methods=["DELETE"])
def auth_users_delete(username: str):
    """管理员删除用户（不能删除自己）。"""
    deny = _require_admin()
    if deny:
        return deny

    current = session.get("user")
    if username == current:
        return jsonify({"error": "不能删除自己的账号"}), 400

    users = _load_users()
    before = len(users)
    users = [u for u in users if u["username"] != username]
    if len(users) == before:
        return jsonify({"error": "用户不存在"}), 404

    _save_users(users)
    return jsonify({"ok": True})


@auth_bp.route("/auth/users/<username>/reset-password", methods=["POST"])
def auth_users_reset_password(username: str):
    """管理员重置指定用户的密码。"""
    deny = _require_admin()
    if deny:
        return deny

    body = request.get_json(force=True, silent=True) or {}
    new_pw = (body.get("new_password") or "").strip()

    if not new_pw:
        return jsonify({"error": "新密码不能为空"}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400

    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["password_hash"] = generate_password_hash(new_pw)
            _save_users(users)
            return jsonify({"ok": True})

    return jsonify({"error": "用户不存在"}), 404


@auth_bp.route("/auth/users/<username>/role", methods=["PUT"])
def auth_users_change_role(username: str):
    """管理员修改用户角色（不能降级自己）。"""
    deny = _require_admin()
    if deny:
        return deny

    current = session.get("user")
    if username == current:
        return jsonify({"error": "不能修改自己的角色"}), 400

    body = request.get_json(force=True, silent=True) or {}
    role = (body.get("role") or "").strip()
    if role not in (ROLE_ADMIN, ROLE_USER):
        return jsonify({"error": f"角色只能是 {ROLE_ADMIN} 或 {ROLE_USER}"}), 400

    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["role"] = role
            _save_users(users)
            return jsonify({"ok": True})

    return jsonify({"error": "用户不存在"}), 404
