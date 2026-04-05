"""在未 pip install -e . 时，将 src 加入 path 后运行 CLI。"""

from __future__ import annotations

from . import _bootstrap  # noqa: F401 — 注入 src 到 sys.path
