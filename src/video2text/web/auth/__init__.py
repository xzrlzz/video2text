"""认证模块：基于 Flask session 的用户名密码验证，与主业务逻辑分离。"""

from video2text.web.auth.auth import (  # noqa: F401
    auth_bp,
    get_current_user,
    init_auth,
    is_current_user_admin,
    login_required,
)
