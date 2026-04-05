"""
项目根目录与运行时路径解析（Web / CLI / 配置查找共用）。

环境变量（可选）：
- V2T_WORKSPACE — Web 任务与片段缓存根目录，默认 <项目根>/workspace
- V2T_STATIC — Flask 静态资源目录，默认 <项目根>/static
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_MARKER = "pyproject.toml"


def get_project_root() -> Path:
    """仓库根目录（含 pyproject.toml）；检测失败退回 cwd。"""
    repo = Path(__file__).resolve().parent.parent.parent.parent
    if (repo / _REPO_MARKER).is_file():
        return repo
    return Path.cwd()


def get_data_dir() -> Path:
    return get_project_root() / "data"


def get_data_input_dir() -> Path:
    return get_data_dir() / "input"


def get_data_output_dir() -> Path:
    return get_data_dir() / "output"


def get_data_config_dir() -> Path:
    return get_data_dir() / "config"


def get_config_example_path() -> Path:
    """data/config/config.example.json — 配置模板。"""
    return get_data_config_dir() / "config.example.json"


def get_workspace_dir() -> Path:
    env = os.getenv("V2T_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return get_data_dir() / "workspace"


def get_static_dir() -> Path:
    env = os.getenv("V2T_STATIC", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return get_project_root() / "static"


def get_default_config_path() -> Path:
    """Web UI 读写的主配置文件路径。"""
    return get_project_root() / "config.json"
