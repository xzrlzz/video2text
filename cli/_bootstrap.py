"""
未 pip install -e . 时，将仓库根目录下的 src/ 加入 sys.path。
由 cli 包在 import 时加载；与 video-person-split 的 cli 引导方式一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
