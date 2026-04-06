"""认证模块：基于 Flask session 的用户名密码验证，与主业务逻辑分离。"""

from video2text.web.auth.auth import auth_bp, login_required, init_auth  # noqa: F401
