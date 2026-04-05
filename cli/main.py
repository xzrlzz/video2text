#!/usr/bin/env python3
"""从仓库根目录运行：python cli/main.py ..."""

from __future__ import annotations

import cli  # noqa: F401 — 注入 src 到 sys.path

from video2text.cli import cli

if __name__ == "__main__":
    cli()
