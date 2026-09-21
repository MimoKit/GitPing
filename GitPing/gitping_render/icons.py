"""平台图标。

内置渲染器不支持内联 SVG 的尺寸控制，且 CSS 宽高对内联 ``<img>`` 不生效
（两者都是实测结论），因此图标统一预渲染成 PNG，用 ``background-image``
加载并靠 ``background-size`` 控制尺寸——这是唯一在两套后端下表现一致的方式。
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from ..gitping_core.platforms import PLATFORM_COLORS, Platform

LOGO_DIR = Path(__file__).resolve().parents[2] / "resources" / "logos"


@lru_cache(maxsize=8)
def _logo_uri(platform: str) -> str:
    """读平台图标的 data URI；文件缺失时返回空串，由调用方降级。"""
    path = LOGO_DIR / f"{platform}.png"
    if not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def icon(platform: Platform, *, size: int = 28) -> str:
    """平台图标元素；图标缺失时退化为品牌色圆点。"""
    uri = _logo_uri(platform)
    color = PLATFORM_COLORS.get(platform, "#666")
    if not uri:
        return (
            f'<span class="ico ico-{size}" style="border-radius:50%;'
            f'background:{color};width:{size // 2}px;height:{size // 2}px"></span>'
        )
    return f'<span class="ico ico-{size}" style="background-image:url(\'{uri}\')"></span>'


def icon_tile(platform: Platform, *, tile: int = 44, size: int = 28) -> str:
    """带品牌色淡底的图标方块。"""
    color = PLATFORM_COLORS.get(platform, "#666")
    return (
        f'<span class="ico-tile tile-{tile}" style="background:{_tint(color)}">'
        f"{icon(platform, size=size)}</span>"
    )


def brand_mark() -> str:
    """顶栏品牌标记：叠一个平台图标不合适的通用分支图形，用蓝色调。"""
    svg_uri = _brand_uri()
    return f'<span class="ico ico-32" style="background-image:url(\'{svg_uri}\')"></span>'


@lru_cache(maxsize=1)
def _brand_uri() -> str:
    """品牌标记做成本地 SVG data URI（只在 browser 后端用于装饰，
    内置后端若渲染异常也不影响主体布局，因为它是固定 32px 的 background）。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="9" fill="#2f6bff"/>'
        '<circle cx="16" cy="9.5" r="3.1" fill="#fff"/>'
        '<circle cx="16" cy="22.5" r="3.1" fill="#fff"/>'
        '<circle cx="9.5" cy="16" r="3.1" fill="#fff"/>'
        '<path d="M16 12.6v6.8M12.6 16h6.8" stroke="#2f6bff" stroke-width="1.7" fill="none"/>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")


def brand_vars(platform: Platform) -> str:
    """按平台生成 CSS 变量覆盖，让同一套模板跟随平台切换情绪。

    只改强调色，不动中性色与留白——版式保持一致，观感靠色彩区分。
    """
    base = PLATFORM_COLORS.get(platform, "#2f6bff")
    return (
        ":root{"
        f"--brand:{base};"
        f"--brand-soft:{_tint(base, 0.09)};"
        f"--brand-line:{_tint(base, 0.26)};"
        f"--brand-ink:{_darken(base)};"
        "}"
    )


def _darken(hex_color: str, factor: float = 0.76) -> str:
    """压暗品牌色，保证在浅底上达到正文对比度。"""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        return hex_color
    r, g, b = (max(0, min(255, round(int(value[i : i + 2], 16) * factor))) for i in (0, 2, 4))
    return f"#{r:02x}{g:02x}{b:02x}"


def _tint(hex_color: str, alpha: float = 0.10) -> str:
    """把品牌色转成淡底。"""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        return "rgba(0,0,0,.06)"
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
