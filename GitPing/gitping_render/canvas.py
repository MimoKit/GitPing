"""渲染底座：两种后端的统一入口。

- ``browser``：Playwright + 无头 Chromium。排版最准，支持毛玻璃与内联 SVG，
  但需要用户额外装 Chromium。
- ``builtin``：GsCore 自带的 ``render_html_to_bytes``（pytakumi，无浏览器）。
  零额外依赖，但有两个限制（实测得出）：
    1. 不支持 ``backdrop-filter``，毛玻璃必须用半透明底色近似；
    2. 内联 ``<svg>`` 尺寸失控，图标必须转成 PNG data URI，
       且要用 ``background-image`` + ``background-size`` 控制大小，
       因为 CSS 的 ``width/height`` 对 ``<img>`` 不生效。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Literal

from gsuid_core.logger import logger

from ..gitping_config import config_int, config_text

RenderBackend = Literal["browser", "builtin"]
ResolvedBackend = Literal["browser", "builtin", "none"]

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PLUGIN_ROOT / "templates"
# 字体策略：不随插件打包字体（霞鹜文楷约 25MB，会让仓库膨胀）。
# 浏览器后端直接注入 GsCore 自带的 MiSans 可变字体，内置后端由 pytakumi 自行注册，
# 两条路径因此拿到同一个字体文件，出图观感一致。
# 用户若系统装了 HarmonyOS Sans SC（kkk 用的那套）或霞鹜文楷，字体栈会自动优先使用。
FONT_STACK = '"HarmonyOS Sans SC", "LXGW WenKai", "MiSans", "Noto Sans CJK SC", sans-serif'


def core_font_path() -> Path | None:
    """GsCore 自带字体路径；取不到时返回 None，由渲染器自行兜底。"""
    try:
        from gsuid_core.utils.fonts.fonts import FONT_ORIGIN_PATH

        return FONT_ORIGIN_PATH if FONT_ORIGIN_PATH.is_file() else None
    except ImportError:
        return None

# 画布参数。两套后端共用同一份画布宽度，保证出图观感一致。
CANVAS_WIDTH = 1080
DEFAULT_SCALE = 2.0

_CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
_browser_available: bool | None = None


def load_template(name: str) -> str:
    path = TEMPLATE_DIR / name
    if not path.is_file():
        raise RuntimeError(f"模板文件缺失：{path}")
    return path.read_text(encoding="utf-8")


def png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def svg_data_uri(svg: str) -> str:
    """把 SVG 转成 data URI。

    只有 browser 后端能用；builtin 后端必须走 :func:`png_data_uri`。
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _browsers_root() -> Path:
    """Playwright 的浏览器安装目录。"""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        return Path(configured)
    return Path.home() / ".cache" / "ms-playwright"


def _find_chromium() -> Path | None:
    """定位 Chromium 可执行文件。

    刻意不用 ``sync_playwright()`` 探测：它内部会起自己的事件循环，
    在已经运行的事件循环里调用会直接抛错（插件运行时必然处于事件循环中），
    结果是浏览器明明装了却被判为不可用。这里改为直接找可执行文件。
    """
    root = _browsers_root()
    if not root.is_dir():
        return None
    # 目录形如 chromium-1208/chrome-linux64/chrome（新）
    # 或 chromium-1091/chrome-linux/chrome（旧）
    for candidate in sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    # 兜底：headless shell 也能截图
    for candidate in sorted(root.glob("chromium_headless_shell-*/chrome-linux*/headless_shell"), reverse=True):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def browser_available() -> bool:
    """检测 Playwright 与 Chromium 是否就绪。结果缓存，避免每次渲染都探测。"""
    global _browser_available
    if _browser_available is not None:
        return _browser_available
    try:
        import playwright  # noqa: F401
    except ImportError:
        logger.debug("[GitPing] 未安装 playwright，回退到内置渲染")
        _browser_available = False
        return False

    _browser_available = _find_chromium() is not None
    if not _browser_available:
        logger.info("[GitPing] 未找到 Chromium，将使用 GsCore 内置渲染")
    return _browser_available


def resolve_backend() -> ResolvedBackend:
    """按配置决定用哪套后端；auto 时优先浏览器，不可用则回退内置。"""
    configured = config_text("render_backend", "auto").strip().lower()
    if configured == "builtin":
        return "builtin"
    if configured == "browser":
        if browser_available():
            return "browser"
        logger.warning("[GitPing] 配置要求浏览器渲染，但 Chromium 不可用，已回退内置渲染")
        return "builtin"
    return "browser" if browser_available() else "builtin"


def request_timeout() -> float:
    return float(config_int("request_timeout", 15))


def scale_for_quality() -> float:
    return 2.0 if config_text("render_quality", "default") == "high" else 1.0


async def render(html: str, *, min_height: int = 600, scale: float = 0) -> bytes:
    """按配置选择后端渲染 HTML，返回 PNG 字节。"""
    backend = resolve_backend()
    scale = scale or scale_for_quality()
    if backend == "browser":
        try:
            return await _render_browser(html, min_height=min_height, scale=scale)
        except Exception:
            # 探测通过但启动仍可能失败（缺系统库、权限不足等）。
            # 渲染失败不该让整条命令挂掉，降级到内置渲染并记一条。
            logger.warning("[GitPing] 浏览器渲染失败，已回退到内置渲染", exc_info=True)
            global _browser_available
            _browser_available = False
    return await _render_builtin(html, min_height=min_height, scale=scale)


async def _render_browser(html: str, *, min_height: int, scale: float) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        try:
            page = await browser.new_page(
                viewport={"width": CANVAS_WIDTH, "height": min_height},
                device_scale_factor=scale,
            )
            await page.set_content(html, wait_until="load")
            font_path = core_font_path()
            if font_path is not None:
                font_b64 = base64.b64encode(font_path.read_bytes()).decode("ascii")
                await page.add_style_tag(
                    content=(
                        "@font-face{font-family:'MiSans';"
                        f"src:url(data:font/ttf;base64,{font_b64}) format('truetype');"
                        "font-display:block;}"
                    )
                )
                await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(120)
            png = await page.screenshot(full_page=True, type="png")
        finally:
            await browser.close()
    logger.debug(f"[GitPing] 浏览器渲染完成 {len(png) / 1024:.1f} KB")
    return png


async def _render_builtin(html: str, *, min_height: int, scale: float) -> bytes:
    from gsuid_core.utils.html_render import render_html_to_bytes

    dpi = 96.0 * scale
    # builtin 后端要求 root_max_width 等于 CSS 布局宽，否则右侧会被裁切
    png = await render_html_to_bytes(
        html,
        max_width=float(CANVAS_WIDTH),
        dpi=dpi,
        default_font_size=14.0,
        font_name="sans-serif",
        allow_refit=True,
        image_format="png",
        lang="zh",
        root_max_width=float(CANVAS_WIDTH),
    )
    logger.debug(f"[GitPing] 内置渲染完成 {len(png) / 1024:.1f} KB")
    return png
