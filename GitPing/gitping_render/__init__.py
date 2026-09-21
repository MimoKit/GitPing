"""GitPing 卡片渲染：双后端（无头浏览器 / GsCore 内置）。"""

from .canvas import browser_available, render, resolve_backend
from .cards import (
    render_commit,
    render_commits,
    render_release,
    render_repo,
)

__all__ = [
    "browser_available",
    "render",
    "render_commit",
    "render_commits",
    "render_release",
    "render_repo",
    "resolve_backend",
]
