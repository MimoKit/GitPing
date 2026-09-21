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
import shutil
from typing import Literal

from gsuid_core.logger import logger

from ..gitping_config import config_int, config_text

RenderBackend = Literal["browser", "builtin"]
ResolvedBackend = Literal["browser", "builtin", "none"]

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PLUGIN_ROOT / "templates"
FONTS_DIR = PLUGIN_ROOT / "resources" / "fonts"

# 参考 karin-plugin-git：
# 优先使用 DouyinSans 作为正文字体，MapleMono 作为等宽与加粗代码字体
FONT_STACK = '"DouyinSans", "HarmonyOS Sans SC", "MiSans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
FONT_MONO_STACK = '"MapleMono-Medium", "MapleMono-Bold", ui-monospace, "SF Mono", Menlo, Consolas, monospace'

_FONTS_CSS_CACHE: str | None = None


def get_font_css() -> str:
    """获取内嵌字体 @font-face 样式（带内存缓存）。

    把 DouyinSans 与 MapleMono 编码为 base64 data URI，实现零外网依赖的离线秒级渲染。
    """
    global _FONTS_CSS_CACHE
    if _FONTS_CSS_CACHE is not None:
        return _FONTS_CSS_CACHE

    css_parts: list[str] = []
    font_configs = [
        ("DouyinSans", "DouyinSans.woff2", 700),
        ("MapleMono-Medium", "MapleMono-Medium.woff2", 500),
        ("MapleMono-Bold", "MapleMono-Bold.woff2", 700),
    ]

    for family, filename, weight in font_configs:
        font_file = FONTS_DIR / filename
        if font_file.is_file():
            try:
                b64 = base64.b64encode(font_file.read_bytes()).decode("ascii")
                css_parts.append(
                    f"@font-face {{\n"
                    f"  font-family: '{family}';\n"
                    f"  src: url('data:font/woff2;base64,{b64}') format('woff2');\n"
                    f"  font-weight: {weight};\n"
                    f"  font-style: normal;\n"
                    f"  font-display: swap;\n"
                    f"}}"
                )
            except Exception as e:
                logger.warning(f"[GitPing] 加载本地字体 {filename} 失败: {e}")

    _FONTS_CSS_CACHE = "\n".join(css_parts)
    return _FONTS_CSS_CACHE


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


def _browsers_roots() -> list[Path]:
    """收集可能存放 Playwright 或独立浏览器的目录列表。"""
    roots: list[Path] = []
    # 1. 环境变量优先
    for env_k in ("PLAYWRIGHT_BROWSERS_PATH", "CHROME_PATH", "CHROMIUM_PATH", "BROWSER_PATH"):
        val = os.environ.get(env_k)
        if val and val != "0":
            p = Path(val)
            if p.is_dir() and p not in roots:
                roots.append(p)
    # 2. 常见 Playwright 安装目录
    for p in (
        Path("/ms-playwright"),
        Path.home() / ".cache" / "ms-playwright",
        Path("/root/.cache/ms-playwright"),
    ):
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


def _find_chromium() -> Path | None:
    """定位系统中任意可用的 Chromium / Chrome / Edge 浏览器可执行文件。

    不固定任何特定版本号，只要是可用的浏览器即可直接驱动（支持系统全局 Chrome/Edge
    以及 Playwright 目录下的任意版本）。
    """
    # 1. 环境变量直接指向可执行文件
    for env_k in ("CHROME_PATH", "CHROMIUM_PATH", "BROWSER_PATH"):
        val = os.environ.get(env_k)
        if val and val != "0":
            p = Path(val)
            if p.is_file() and os.access(p, os.X_OK):
                return p

    # 2. 系统 PATH 中的常见浏览器（系统级安装，无需 Playwright 特供版）
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "microsoft-edge",
        "msedge",
    ):
        found = shutil.which(name)
        if found:
            p = Path(found)
            if p.is_file() and os.access(p, os.X_OK):
                return p

    # 3. 常见系统固定安装路径
    fixed_paths = [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/opt/google/chrome/chrome"),
        Path("/opt/microsoft/msedge/msedge"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for p in fixed_paths:
        if p.is_file() and os.access(p, os.X_OK):
            return p

    # 4. 扫描所有可能存在的 Playwright 浏览器目录（递归匹配任意版本号）
    # 兼容 chromium-*, chromium_headless_shell-*, 任意版本数字
    for root in _browsers_roots():
        for candidate in sorted(root.glob("**/chrome*"), reverse=True):
            if (
                candidate.is_file()
                and candidate.name in ("chrome", "chrome.exe", "chrome-headless-shell")
                and os.access(candidate, os.X_OK)
            ):
                return candidate
        for candidate in sorted(root.glob("**/headless_shell*"), reverse=True):
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

    exe = _find_chromium()
    _browser_available = exe is not None
    if not _browser_available:
        logger.info("[GitPing] 未找到可用浏览器内核，将使用 GsCore 内置渲染")
    else:
        logger.debug(f"[GitPing] 检测到可用浏览器内核: {exe}")
    return _browser_available


def resolve_backend() -> ResolvedBackend:
    """按配置决定用哪套后端；auto 时优先浏览器，不可用则回退内置。"""
    configured = config_text("render_backend", "auto").strip().lower()
    if configured == "builtin":
        return "builtin"
    if configured == "browser":
        if browser_available():
            return "browser"
        logger.warning("[GitPing] 配置要求浏览器渲染，但未找到可用浏览器内核，已回退内置渲染")
        return "builtin"
    return "browser" if browser_available() else "builtin"


def request_timeout() -> float:
    return float(config_int("request_timeout", 15))


# 采用 2.0x 视网膜高清比例（1080px -> 2160px，兼顾秒级出图、文字锐利度与 QQ 平台传输规格）
DEFAULT_SCALE = 2.0


async def render(html: str, *, min_height: int = 500, scale: float = 0) -> bytes:
    """按配置选择后端渲染 HTML，返回 PNG 字节。"""
    backend = resolve_backend()
    scale = scale or DEFAULT_SCALE
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

    chromium_path = _find_chromium()
    launch_kwargs = {"headless": True, "args": _CHROMIUM_ARGS}
    if chromium_path is not None:
        launch_kwargs["executable_path"] = str(chromium_path)

    logger.info(
        f"[GitPing] 🌐 正在调用无头浏览器渲染卡片 (清晰度: {scale}x, 内核: {chromium_path.name if chromium_path else 'default'})..."
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            page = await browser.new_page(
                viewport={"width": CANVAS_WIDTH, "height": min_height},
                device_scale_factor=scale,
            )
            await page.set_content(html, wait_until="load", timeout=15000)
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(80)

            # 参考 karin-plugin-kkk: 对 .canvas 主卡片容器精确截图，避免 full_page 产生的外部空隙与比例失真
            canvas_el = await page.query_selector(".canvas")
            if canvas_el:
                png = await canvas_el.screenshot(type="png")
            else:
                png = await page.screenshot(full_page=True, type="png")
        finally:
            await browser.close()

    size_mb = len(png) / 1024 / 1024
    logger.info(f"[GitPing] ✨ 无头浏览器渲染完成，生成超清卡片大小: {size_mb:.2f} MB ({len(png) / 1024:.1f} KB)")
    return png


async def _render_builtin(html: str, *, min_height: int, scale: float) -> bytes:
    from gsuid_core.utils.html_render import render_html_to_bytes

    dpi = 96.0 * scale
    # builtin 后端要求 root_max_width 等于 CSS 布局宽，否则右侧会被裁切
    png = await render_html_to_bytes(
        html,
        max_width=float(CANVAS_WIDTH),
        dpi=dpi,
        default_font_size=16.0,
        font_name="sans-serif",
        allow_refit=True,
        image_format="png",
        lang="zh",
        root_max_width=float(CANVAS_WIDTH),
    )
    logger.debug(f"[GitPing] 内置渲染完成 {len(png) / 1024:.1f} KB")
    return png
