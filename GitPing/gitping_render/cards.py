"""卡片构建：把平台数据填进模板并渲染成图。

四种卡片：
- :func:`render_repo`    仓库信息
- :func:`render_commits` 提交列表
- :func:`render_commit`  单条提交详情
- :func:`render_release` 版本发布

构建与渲染分离：``build_*`` 只产出 HTML（便于测试与复用），
``render_*`` 再交给 :mod:`.canvas` 选择后端出图。
"""

from __future__ import annotations

from html import escape, unescape
import re

from ..gitping_core.platforms import (
    PLATFORM_LABELS,
    CommitInfo,
    Platform,
    ReleaseInfo,
    RepoInfo,
)
from . import icons
from .canvas import FONT_STACK, get_font_css, load_template, render

_MAX_DESC = 160
_MAX_COMMIT_BODY = 1400
_MAX_RELEASE_BODY = 2600
_MAX_ASSETS = 12


# ── 小工具 ───────────────────────────────────────────────────────────────────


def _text(value: str, limit: int = 0) -> str:
    """转义并可选截断。"""
    cleaned = escape((value or "").strip())
    if limit and len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _num(value: int) -> str:
    """大数用 k 缩写，避免统计条被撑开。"""
    if value >= 10000:
        return f"{value / 10000:.1f}w"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _date(value: str) -> str:
    """把 ISO 时间裁成 YYYY-MM-DD。"""
    if not value:
        return "未知时间"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return value[:10]


def _datetime(value: str) -> str:
    """裁成 YYYY-MM-DD HH:MM。"""
    if not value:
        return "未知时间"
    match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value[:16]


def _size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} B"


def _markdown(raw: str, limit: int) -> str:
    """把 release / commit 正文里的 Markdown 转成安全 HTML。

    支持标题、列表、围栏代码块、行内代码、链接与分隔线，够表达发布说明。
    先转义再套标签，避免正文里的尖括号破坏结构。

    代码块必须单独处理：否则 ``` 围栏会被当成普通段落显示出来。
    """
    if not raw.strip():
        return ""

    # 先按围栏切片，再逐段处理，这样代码块内部的标记不会被误解析
    body = raw.strip()
    truncated = len(body) > limit
    if truncated:
        body = body[:limit]

    out: list[str] = []
    in_list = False
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    def flush_code() -> None:
        nonlocal in_code, code_lines
        if not in_code:
            return
        # 代码块内容整体转义，保留原始缩进
        content = escape("\n".join(code_lines).strip("\n"))
        if content:
            cls = f' class="lang-{escape(code_lang)}"' if code_lang else ""
            out.append(f"<pre><code{cls}>{content}</code></pre>")
        code_lines = []
        in_code = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in body.splitlines():
        stripped = line.strip()

        # 围栏：``` 或 ~~~，可带语言标记
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if in_code:
                flush_code()
            else:
                close_list()
                in_code = True
                code_lang = stripped.strip("`~").strip()
            continue

        if in_code:
            code_lines.append(line)
            continue

        if stripped.startswith(("- ", "* ", "+ ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        close_list()

        if not stripped:
            continue
        if stripped.startswith("###"):
            out.append(f"<h3>{_inline(stripped.lstrip('#').strip())}</h3>")
        elif stripped.startswith("##"):
            out.append(f"<h2>{_inline(stripped.lstrip('#').strip())}</h2>")
        elif stripped.startswith("#"):
            out.append(f"<h1>{_inline(stripped.lstrip('#').strip())}</h1>")
        elif stripped in ("---", "***", "___"):
            out.append("<hr>")
        elif stripped.startswith(">"):
            out.append(f"<p class=\"quote\">{_inline(stripped.lstrip('> ').strip())}</p>")
        else:
            out.append(f"<p>{_inline(stripped)}</p>")

    flush_code()
    close_list()

    if truncated:
        out.append('<p class="cut">说明过长，此处仅显示开头部分</p>')
    return "".join(out)


def _inline(text: str) -> str:
    """处理行内的代码与链接。"""
    escaped = escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )
    # 裸链接也点上
    escaped = re.sub(
        r"(?<![\"=>])(https?://[^\s<]+)",
        r'<a href="\1">\1</a>',
        escaped,
    )
    return escaped


def _shell(*, platform: Platform, crumb: str, body: str, footer_left: str) -> str:
    """套上外壳模板，并按平台注入品牌色变量。"""
    html = load_template("card.html")
    for token, value in (
        ("{{FONTS_CSS}}", get_font_css()),
        ("{{STYLE}}", load_template("style.css")),
        ("{{BRAND_VARS}}", icons.brand_vars(platform)),
        ("{{BRAND_ICON}}", icons.brand_mark()),
        ("{{CRUMB}}", crumb),
        ("{{BODY}}", body),
        ("{{FOOTER_LEFT}}", footer_left),
    ):
        html = html.replace(token, value)
    return html


def _section(title: str, inner: str, note: str = "") -> str:
    head = f'<div class="section-head"><span class="section-title">{title}</span>'
    if note:
        head += f'<span class="section-note">{note}</span>'
    head += "</div>"
    return f'<section class="section">{head}<div class="panel">{inner}</div></section>'


def _crumb(platform: Platform, text: str) -> str:
    return f'{icons.icon(platform, size=20)}<span>{_text(text)}</span>'


# ── 仓库信息 ─────────────────────────────────────────────────────────────────


def build_repo(info: RepoInfo, *, note: str = "") -> str:
    title_class = " is-long" if len(info.repo) > 22 else ""
    tags = [f'<span class="tag is-accent">{_text(info.language)}</span>']
    tags.append(f'<span class="tag">{_text(info.license_name)}</span>')
    if info.private:
        tags.append('<span class="tag">私有仓库</span>')
    if info.default_branch:
        tags.append(f'<span class="tag mono">{_text(info.default_branch)}</span>')

    stats = [
        ("Stars", _num(info.stars), False),
        ("Forks", _num(info.forks), False),
        ("Issues", _num(info.issues), False),
        ("更新于", _date(info.updated_at), True),
    ]
    stats_html = "".join(
        f'<div class="stat"><div class="k">{k}</div>'
        f'<div class="v{" is-sm" if small else ""}">{v}</div></div>'
        for k, v, small in stats
    )

    body = (
        '<section class="repo-head">'
        f"{icons.icon_tile(info.platform, tile=52, size=32)}"
        '<div class="repo-title-wrap">'
        f'<div class="repo-owner">{_text(info.owner)}</div>'
        f'<div class="repo-name{title_class}">{_text(info.repo)}</div>'
        f'<div class="repo-desc">{_text(info.description, _MAX_DESC)}</div>'
        f'<div class="tags">{"".join(tags)}</div>'
        "</div></section>"
        f'<div class="stats">{stats_html}</div>'
    )

    footer = f"{PLATFORM_LABELS[info.platform]} · {_text(info.full_name)}"
    if note:
        footer = f"{note} · {footer}"
    return _shell(
        platform=info.platform,
        crumb=_crumb(info.platform, info.full_name),
        body=body,
        footer_left=footer,
    )


# ── 提交列表 ─────────────────────────────────────────────────────────────────


def _commit_row(item: CommitInfo) -> str:
    diff = ""
    if item.additions or item.deletions:
        total = item.additions + item.deletions
        add_pct = round(item.additions / total * 100) if total else 0
        del_pct = 100 - add_pct if total else 0
        diff = (
            '<span class="diff">'
            f'<span class="add mono">+{item.additions}</span>'
            f'<span class="del mono">-{item.deletions}</span>'
            '</span>'
            '<span class="diffbar">'
            f'<span class="a" style="width:{add_pct}%"></span>'
            f'<span class="d" style="width:{del_pct}%"></span>'
            "</span>"
        )
    avatar_html = f'<img class="avatar avatar-36" src="{item.author_avatar}" alt="{_text(item.author)}" />' if item.author_avatar else ""
    return (
        '<article class="commit">'
        f"{avatar_html}"
        '<div class="meta">'
        f'<div class="msg">{_text(item.title)}</div>'
        '<div class="sub">'
        f'<span class="author-name">{_text(item.author)}</span>'
        '<span class="mono">·</span>'
        f'<span class="mono">{_datetime(item.date)}</span>'
        '<span class="mono">·</span>'
        f'<span class="sha mono">{_text(item.short_sha)}</span>'
        f"{diff}"
        "</div></div></article>"
    )


def build_commits(
    platform: Platform,
    owner: str,
    repo: str,
    items: list[CommitInfo],
    *,
    branch: str = "",
    note: str = "",
) -> str:
    slug = f"{owner}/{repo}"
    if items:
        inner = "".join(_commit_row(c) for c in items)
    else:
        inner = '<div class="empty">这个分支还没有提交记录</div>'

    head_stats = [
        ("提交数", str(len(items)), False),
        ("分支", branch or "默认", True),
    ]
    stats_html = "".join(
        f'<div class="stat"><div class="k">{k}</div>'
        f'<div class="v{" is-sm" if small else ""}">{_text(v)}</div></div>'
        for k, v, small in head_stats
    )

    body = (
        '<section class="repo-head">'
        f"{icons.icon_tile(platform, tile=52, size=32)}"
        '<div class="repo-title-wrap">'
        f'<div class="repo-owner">{_text(owner)}</div>'
        f'<div class="repo-name">{_text(repo)}</div>'
        '<div class="repo-desc">提交记录</div>'
        "</div></section>"
        f'<div class="stats">{stats_html}</div>'
        f"{_section('Recent Commits', inner, note or f'最近 {len(items)} 条')}"
    )
    return _shell(
        platform=platform,
        crumb=_crumb(platform, f"{slug} · commits"),
        body=body,
        footer_left=f"{PLATFORM_LABELS[platform]} · {slug}",
    )


# ── 提交详情 ─────────────────────────────────────────────────────────────────


def build_commit(
    platform: Platform,
    owner: str,
    repo: str,
    item: CommitInfo,
    *,
    note: str = "",
) -> str:
    slug = f"{owner}/{repo}"

    # 1. 提交者信息卡片（参考 karin-plugin-git User 组件）
    if item.is_same_author:
        user_action = f'由 <span class="user-highlight">{_text(item.author)}</span> 提交'
        avatar_content = f'<img class="avatar avatar-44" src="{item.author_avatar}" alt="{_text(item.author)}" />'
    else:
        user_action = (
            f'由 <span class="user-highlight">{_text(item.author)}</span> 编写，'
            f'并由 <span class="user-highlight">{_text(item.committer)}</span> 提交'
        )
        avatar_content = (
            '<div class="avatar-group">'
            f'<img class="avatar avatar-44" src="{item.author_avatar}" alt="{_text(item.author)}" />'
            f'<img class="avatar avatar-44" src="{item.committer_avatar}" alt="{_text(item.committer)}" />'
            '</div>'
        )

    sub_email = f'<div class="user-sub mono">{_text(item.author_email)}</div>' if item.author_email else ""
    user_card_html = (
        '<div class="user-card">'
        '<div class="user-info">'
        f"{avatar_content}"
        '<div class="user-meta">'
        f'<div class="user-action">{user_action}</div>'
        f"{sub_email}"
        "</div></div>"
        f'<div class="user-time-badge mono">{_datetime(item.date)}</div>'
        "</div>"
    )

    # 2. 变更统计与标题概览卡片（参考 karin-plugin-git Content 组件）
    stats_pills: list[str] = []
    if item.additions:
        stats_pills.append(f'<span class="stat-pill stat-add mono">+{item.additions}</span>')
    if item.deletions:
        stats_pills.append(f'<span class="stat-pill stat-del mono">-{item.deletions}</span>')
    if item.files_changed:
        stats_pills.append(f'<span class="stat-files">{item.files_changed} 个文件被更改</span>')
    stats_pills.append('<span class="dot">•</span>')
    stats_pills.append(f'<span class="sha mono">{_text(item.sha)}</span>')

    overview_html = (
        '<div class="commit-overview">'
        f'<div class="commit-title-text">{_text(item.title)}</div>'
        f'<div class="commit-stats-bar">{"".join(stats_pills)}</div>'
        "</div>"
    )

    body = (
        '<section class="repo-head">'
        f"{icons.icon_tile(platform, tile=52, size=32)}"
        '<div class="repo-title-wrap">'
        f'<div class="repo-owner">{_text(owner)}</div>'
        f'<div class="repo-name is-long">{_text(repo)}</div>'
        '<div class="repo-desc">提交详情</div>'
        "</div></section>"
        f"{user_card_html}"
        f"{overview_html}"
    )

    detail = _html_body(item.body, _MAX_COMMIT_BODY)
    if detail:
        body += _section("Message", f'<div class="commit-body">{detail}</div>')

    return _shell(
        platform=platform,
        crumb=_crumb(platform, f"{item.short_sha} · {slug}"),
        body=body,
        footer_left=note or f"{PLATFORM_LABELS[platform]} · {slug}",
    )


# Git 提交里的元信息尾注，对阅读没有价值，展示前剔除
_TRAILER_RE = re.compile(
    r"^(co-authored-by|signed-off-by|reviewed-by|tested-by|acked-by|"
    r"co-committed-by|change-id|closes|fixes)\s*:",
    re.IGNORECASE,
)


def _html_body(raw: str, limit: int) -> str:
    """提交正文：剔除元信息尾注，保留换行，转义 HTML。"""
    lines = [
        line
        for line in raw.strip().splitlines()
        if not _TRAILER_RE.match(line.strip())
    ]
    body = "\n".join(lines).strip()
    if not body:
        return ""
    return escape(body[:limit])


# ── 版本发布 ─────────────────────────────────────────────────────────────────


def build_release(
    platform: Platform,
    owner: str,
    repo: str,
    rel: ReleaseInfo,
    *,
    note: str = "",
) -> str:
    slug = f"{owner}/{repo}"
    tag_class = " is-pre" if rel.prerelease else ""

    head = (
        '<div class="release-head">'
        f'<span class="release-tag{tag_class} mono">{_text(rel.tag)}</span>'
        f'<span class="release-name">{_text(rel.name)}</span>'
        "</div>"
    )

    user_card_html = (
        '<div class="user-card is-release">'
        '<div class="user-info">'
        f'<img class="avatar avatar-44" src="{rel.author_avatar}" alt="{_text(rel.author)}" />'
        '<div class="user-meta">'
        f'<div class="user-action">发布者 <span class="user-highlight">{_text(rel.author)}</span></div>'
        f'<div class="user-sub mono">{_datetime(rel.created_at)}</div>'
        "</div></div>"
        f'<div class="user-time-badge mono">{_text(rel.tag)}</div>'
        "</div>"
    )

    rows = []
    if rel.target:
        rows.append(("目标", rel.target))
    if rel.prerelease:
        rows.append(("类型", "预发布"))
    kv_html = "".join(
        f'<div class="kv"><span class="k">{k}</span>'
        f'<span class="v">{_text(v)}</span></div>'
        for k, v in rows
    )

    inner = head + kv_html if kv_html else head
    body = (
        '<section class="repo-head">'
        f"{icons.icon_tile(platform, tile=52, size=32)}"
        '<div class="repo-title-wrap">'
        f'<div class="repo-owner">{_text(owner)}</div>'
        f'<div class="repo-name is-long">{_text(repo)}</div>'
        '<div class="repo-desc">版本发布</div>'
        "</div></section>"
        f"{user_card_html}"
        f"{_section('Release', inner)}"
    )

    if rel.body.strip():
        md = _markdown(rel.body, _MAX_RELEASE_BODY)
        if md:
            body += _section("Release Notes", f'<div class="md">{md}</div>')

    if rel.assets:
        shown = rel.assets[:_MAX_ASSETS]
        assets_html = "".join(
            f'<span class="asset"><span>{_text(a.name)}</span>'
            f'<span class="sz mono">{_size(a.size)}</span></span>'
            for a in shown
        )
        if len(rel.assets) > _MAX_ASSETS:
            assets_html += f'<span class="asset"><span>另有 {len(rel.assets) - _MAX_ASSETS} 个文件</span></span>'
        note_text = f"{len(rel.assets)} 个文件"
        body += _section("Assets", f'<div class="assets">{assets_html}</div>', note_text)

    return _shell(
        platform=platform,
        crumb=_crumb(platform, f"{rel.tag} · {slug}"),
        body=body,
        footer_left=note or f"{PLATFORM_LABELS[platform]} · {slug}",
    )


# ── 渲染入口 ─────────────────────────────────────────────────────────────────


async def render_repo(info: RepoInfo, *, note: str = "") -> bytes:
    return await render(build_repo(info, note=note), min_height=460)


async def render_commits(
    platform: Platform,
    owner: str,
    repo: str,
    items: list[CommitInfo],
    *,
    branch: str = "",
    note: str = "",
) -> bytes:
    html = build_commits(platform, owner, repo, items, branch=branch, note=note)
    return await render(html, min_height=520)


async def render_commit(
    platform: Platform,
    owner: str,
    repo: str,
    item: CommitInfo,
    *,
    note: str = "",
) -> bytes:
    html = build_commit(platform, owner, repo, item, note=note)
    return await render(html, min_height=520)


async def render_release(
    platform: Platform,
    owner: str,
    repo: str,
    rel: ReleaseInfo,
    *,
    note: str = "",
) -> bytes:
    html = build_release(platform, owner, repo, rel, note=note)
    return await render(html, min_height=560)
