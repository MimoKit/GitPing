"""GitPing 命令层。

命令前缀统一用 ``git``，平台既可在命令里显式指定（``github:owner/repo``），
也可直接贴仓库 URL（从域名推断），还可省略而用群里绑定的默认仓库。

绑定与推送的目标都按「bot + 群」隔离，避免多个群互相干扰。
"""

from __future__ import annotations

import httpx

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.sv import SV

from ..gitping_config import config_bool, config_int, config_text
from ..gitping_render import render_commit, render_commits, render_release, render_repo
from . import store
from .platforms import (
    GITHUB,
    PLATFORM_LABELS,
    PLATFORM_ORDER,
    GitClient,
    GitError,
    Platform,
    RepoRef,
    parse_platform,
    parse_repo_ref,
)
from .store import SUBSCRIBE_TASK, Binding, Subscription

git_sv = SV("GitPing", priority=5, area="ALL")
push_sv = SV("GitPing推送", priority=5, area="ALL")

# 平台名 → 令牌配置键
_TOKEN_KEYS: dict[Platform, str] = {
    "github": "github_token",
    "gitee": "gitee_token",
    "gitcode": "gitcode_token",
    "cnb": "cnb_token",
}


def _is_admin(ev: Event) -> bool:
    """主人或超级用户。"""
    return ev.user_pm in (0, 1)


def _client(platform: Platform) -> GitClient:
    token = config_text(_TOKEN_KEYS[platform], "")
    return GitClient(platform, token, timeout=float(config_int("request_timeout", 15)))


def _resolve_ref(raw: str) -> RepoRef | None:
    """解析仓库引用。"""
    return parse_repo_ref(raw)


async def _resolve_target(ev: Event, raw: str) -> RepoRef | None:
    """确定要操作的仓库。

    优先用命令里显式给出的（或从 URL 推断的）平台与仓库；
    没有平台时，若群里绑定过同一 owner/repo 就沿用其平台，否则默认 GitHub。
    """
    ref = _resolve_ref(raw)
    if ref is None:
        return None
    if ref.platform is not None:
        return ref
    if ev.group_id is not None:
        binding = await store.get_binding(ev.bot_id, ev.group_id)
        if binding is not None and binding.owner == ref.owner and binding.repo == ref.repo:
            return RepoRef(binding.platform, ref.owner, ref.repo)
    return RepoRef(GITHUB, ref.owner, ref.repo)


async def _group_repo(ev: Event) -> RepoRef | None:
    """取群里绑定的默认仓库。"""
    if ev.group_id is None:
        return None
    binding = await store.get_binding(ev.bot_id, ev.group_id)
    if binding is None:
        return None
    return RepoRef(binding.platform, binding.owner, binding.repo)


async def _send_image(bot: Bot, png: bytes) -> None:
    await bot.send(MessageSegment.image(png))


_NO_REPO_HINT = (
    "请指定仓库，或用「git绑定 owner/repo」先为当前群绑定一个默认仓库。\n"
    "示例：git仓库 MimoKit/MomoTune  ·  git仓库 github:MimoKit/MomoTune"
)


# ── 仓库信息 ─────────────────────────────────────────────────────────────────


@git_sv.on_command(("仓库", "repo", "仓库信息"), block=True, prefix=False)
async def repo_command(bot: Bot, ev: Event) -> None:
    """git仓库 [平台:]owner/repo —— 查询仓库信息。"""
    args = ev.text.strip()
    ref = await _resolve_target(ev, args) if args else await _group_repo(ev)
    if ref is None:
        await bot.send(_NO_REPO_HINT)
        return

    client = _client(ref.platform)
    try:
        info = await client.repo_info(ref.owner, ref.repo)
    except GitError as exc:
        await bot.send(str(exc))
        return
    except httpx.HTTPError as exc:
        logger.warning(f"[GitPing] 查询仓库网络异常: {exc}")
        await bot.send("网络异常，请稍后重试。")
        return

    try:
        png = await render_repo(info)
    except (OSError, RuntimeError) as exc:
        logger.warning(f"[GitPing] 渲染仓库卡片失败: {exc}")
        await bot.send(f"{info.full_name} · ⭐ {info.stars} · {info.language}\n{info.url}")
        return
    await _send_image(bot, png)


# ── 绑定 ─────────────────────────────────────────────────────────────────────


@git_sv.on_command(("绑定", "bind"), block=True, prefix=False)
async def bind_command(bot: Bot, ev: Event) -> None:
    """git绑定 [平台:]owner/repo —— 为当前群绑定默认仓库。"""
    if ev.group_id is None:
        await bot.send("仓库绑定只对群聊生效。")
        return
    if not _is_admin(ev):
        await bot.send("仅主人或超级用户可以绑定仓库。")
        return

    raw = ev.text.strip()
    ref = _resolve_ref(raw)
    if ref is None:
        await bot.send("请给出仓库，格式：git绑定 [平台:]owner/repo\n示例：git绑定 github:MimoKit/MomoTune")
        return

    platform = ref.platform or GITHUB
    # 绑定前先验证仓库确实存在，避免把错别字存进去
    client = _client(platform)
    try:
        info = await client.repo_info(ref.owner, ref.repo)
    except GitError as exc:
        await bot.send(f"绑定失败：{exc}")
        return
    except httpx.HTTPError:
        await bot.send("绑定失败：网络异常，请稍后重试。")
        return

    await store.set_binding(ev.bot_id, ev.group_id, Binding(platform, ref.owner, ref.repo))
    await bot.send(
        f"已绑定 {PLATFORM_LABELS[platform]} 仓库 {info.full_name}，"
        "之后可直接用「git提交」「git版本」查询。"
    )


@git_sv.on_command(("解绑", "unbind"), block=True, prefix=False)
async def unbind_command(bot: Bot, ev: Event) -> None:
    """git解绑 —— 解除当前群的仓库绑定。"""
    if ev.group_id is None:
        await bot.send("仓库绑定只对群聊生效。")
        return
    if not _is_admin(ev):
        await bot.send("仅主人或超级用户可以解绑仓库。")
        return
    removed = await store.remove_binding(ev.bot_id, ev.group_id)
    await bot.send("已解除本群仓库绑定。" if removed else "本群当前没有绑定仓库。")


@git_sv.on_fullmatch(("当前仓库", "绑定信息", "git当前仓库"), block=True, prefix=False)
async def current_command(bot: Bot, ev: Event) -> None:
    ref = await _group_repo(ev)
    if ref is None:
        await bot.send("本群尚未绑定仓库。用「git绑定 owner/repo」绑定一个。")
        return
    await bot.send(f"本群已绑定：{PLATFORM_LABELS[ref.platform]} · {ref.owner}/{ref.repo}")


# ── 提交记录 ─────────────────────────────────────────────────────────────────


@git_sv.on_command(("提交", "commits", "提交记录"), block=True, prefix=False)
async def commits_command(bot: Bot, ev: Event) -> None:
    """git提交 [数量] [平台:]owner/repo —— 查询提交记录。"""
    args, ref = await _split_args(ev)
    if ref is None:
        await bot.send(_NO_REPO_HINT)
        return

    limit = config_int("commit_limit", 5)
    if args and args[0].isdigit():
        limit = max(1, min(int(args[0]), 20))

    client = _client(ref.platform)
    try:
        items = await client.commits(ref.owner, ref.repo, limit=limit)
    except GitError as exc:
        await bot.send(str(exc))
        return
    except httpx.HTTPError:
        await bot.send("网络异常，请稍后重试。")
        return

    if not items:
        await bot.send("没有取到提交记录，可能是空仓库或分支不存在。")
        return

    try:
        png = await render_commits(
            ref.platform, ref.owner, ref.repo, items, note=f"最近 {len(items)} 条"
        )
    except (OSError, RuntimeError) as exc:
        logger.warning(f"[GitPing] 渲染提交卡片失败: {exc}")
        lines = "\n".join(f"{c.short_sha} {c.title}" for c in items)
        await bot.send(f"{ref.owner}/{ref.repo} 提交记录：\n{lines}")
        return
    await _send_image(bot, png)


@git_sv.on_command(("提交详情", "commit"), block=True, prefix=False)
async def commit_command(bot: Bot, ev: Event) -> None:
    """git提交详情 <sha> [平台:]owner/repo —— 查询单条提交。"""
    args, ref = await _split_args(ev)
    if not args or ref is None:
        await bot.send("用法：git提交详情 <sha> [平台:]owner/repo")
        return

    sha = args[0]
    client = _client(ref.platform)
    try:
        item = await client.commit_detail(ref.owner, ref.repo, sha)
    except GitError as exc:
        await bot.send(str(exc))
        return
    except httpx.HTTPError:
        await bot.send("网络异常，请稍后重试。")
        return

    if item is None:
        await bot.send(f"没有找到提交 {sha}。")
        return

    try:
        png = await render_commit(ref.platform, ref.owner, ref.repo, item)
    except (OSError, RuntimeError) as exc:
        logger.warning(f"[GitPing] 渲染提交详情失败: {exc}")
        await bot.send(f"{item.short_sha} {item.title}\n作者：{item.author} · {item.date}")
        return
    await _send_image(bot, png)


# ── 版本发布 ─────────────────────────────────────────────────────────────────


@git_sv.on_command(("版本", "release", "发行版"), block=True, prefix=False)
async def release_command(bot: Bot, ev: Event) -> None:
    """git版本 [tag] [平台:]owner/repo —— 查询版本发布。"""
    args, ref = await _split_args(ev)
    if ref is None:
        await bot.send(_NO_REPO_HINT)
        return

    # 第一个非仓库参数当作 tag
    tag = ""
    for arg in args:
        if parse_repo_ref(arg) is None and not arg.isdigit():
            tag = arg
            break

    client = _client(ref.platform)
    try:
        rel = await client.release_detail(ref.owner, ref.repo, tag)
    except GitError as exc:
        await bot.send(str(exc))
        return
    except httpx.HTTPError:
        await bot.send("网络异常，请稍后重试。")
        return

    if rel is None:
        message = f"没有找到标签为 {tag} 的发布。" if tag else "这个仓库还没有发布任何版本。"
        await bot.send(message)
        return

    # release 接口不返回 owner/repo，补上
    rel = type(rel)(
        platform=rel.platform, owner=ref.owner, repo=ref.repo, tag=rel.tag,
        name=rel.name, body=rel.body, author=rel.author, created_at=rel.created_at,
        prerelease=rel.prerelease, target=rel.target, assets=rel.assets,
    )
    try:
        png = await render_release(ref.platform, ref.owner, ref.repo, rel)
    except (OSError, RuntimeError) as exc:
        logger.warning(f"[GitPing] 渲染发布卡片失败: {exc}")
        await bot.send(f"{ref.owner}/{ref.repo} {rel.tag} · {rel.name}\n{rel.url}")
        return
    await _send_image(bot, png)


# ── 推送订阅 ─────────────────────────────────────────────────────────────────

_MAX_SUBSCRIPTIONS_PER_GROUP = 5


@push_sv.on_command(("订阅", "subscribe"), block=True, prefix=False)
async def subscribe_command(bot: Bot, ev: Event) -> None:
    """git订阅 [平台:]owner/repo —— 订阅仓库更新推送。"""
    if ev.group_id is None:
        await bot.send("推送订阅只对群聊生效。")
        return
    if not _is_admin(ev):
        await bot.send("仅主人或超级用户可以管理订阅。")
        return
    if not config_bool("push_enabled", False):
        await bot.send("推送功能当前未开启，请先在 Web 控制台打开 GitPing 的「启用推送订阅」。")
        return

    raw = ev.text.strip()
    ref = await _resolve_target(ev, raw) if raw else await _group_repo(ev)
    if ref is None:
        await bot.send(_NO_REPO_HINT)
        return

    existing = await store.list_subscriptions()
    mine = [s for s in existing if s.bot_id == ev.bot_id and s.group_id == ev.group_id]
    if len(mine) >= _MAX_SUBSCRIPTIONS_PER_GROUP:
        await bot.send(f"本群最多同时订阅 {_MAX_SUBSCRIPTIONS_PER_GROUP} 个仓库，请先取消一些。")
        return

    client = _client(ref.platform)
    try:
        info = await client.repo_info(ref.owner, ref.repo)
    except GitError as exc:
        await bot.send(f"订阅失败：{exc}")
        return
    except httpx.HTTPError:
        await bot.send("订阅失败：网络异常，请稍后重试。")
        return

    # 记下当前游标，订阅之后产生的新提交才会推送
    last_commit = ""
    last_tag = ""
    try:
        commits = await client.commits(ref.owner, ref.repo, limit=1)
        if commits:
            last_commit = commits[0].sha
        rel = await client.release_detail(ref.owner, ref.repo)
        if rel is not None:
            last_tag = rel.tag
    except (GitError, httpx.HTTPError):
        logger.debug("[GitPing] 初始化订阅游标失败，将从空游标开始", exc_info=True)

    added = await store.add_subscription(
        Subscription(
            platform=ref.platform, owner=ref.owner, repo=ref.repo,
            bot_id=ev.bot_id, group_id=ev.group_id,
            last_commit=last_commit, last_tag=last_tag,
        )
    )
    if not added:
        await bot.send(f"本群已经订阅过 {info.full_name} 了。")
        return

    # 同步登记到 GsCore 订阅体系（用 extra_data 存仓库标识），
    # 这样在 Web 控制台的订阅列表里也能看到，而不是只存在插件自己的文件里
    try:
        from gsuid_core.subscribe import gs_subscribe

        await gs_subscribe.add_subscribe(
            "single",
            SUBSCRIBE_TASK,
            ev,
            extra_message=f"{PLATFORM_LABELS[ref.platform]} {info.full_name}",
            extra_data=f"{ref.platform}:{ref.owner}/{ref.repo}",
        )
    except Exception:
        logger.warning("[GitPing] 同步订阅到 GsCore 失败", exc_info=True)

    await bot.send(f"已订阅 {PLATFORM_LABELS[ref.platform]} 仓库 {info.full_name} 的更新推送。")


@push_sv.on_command(("取消订阅", "退订", "unsubscribe"), block=True, prefix=False)
async def unsubscribe_command(bot: Bot, ev: Event) -> None:
    """git取消订阅 [平台:]owner/repo —— 取消推送订阅。"""
    if ev.group_id is None:
        await bot.send("推送订阅只对群聊生效。")
        return
    if not _is_admin(ev):
        await bot.send("仅主人或超级用户可以管理订阅。")
        return

    raw = ev.text.strip()
    ref = await _resolve_target(ev, raw) if raw else await _group_repo(ev)
    if ref is None:
        await bot.send("请指定要取消订阅的仓库。")
        return
    removed = await store.remove_subscription(
        ev.bot_id, ev.group_id, ref.platform, ref.owner, ref.repo
    )
    if removed:
        try:
            from gsuid_core.subscribe import gs_subscribe

            # 只删属于本仓库的那条：逐条比对 extra_data
            subs = await gs_subscribe.get_subscribe(SUBSCRIBE_TASK)
            if subs:
                for sub in subs:
                    if (
                        getattr(sub, "group_id", None) == ev.group_id
                        and getattr(sub, "bot_id", None) == ev.bot_id
                        and getattr(sub, "extra_data", "") == f"{ref.platform}:{ref.owner}/{ref.repo}"
                    ):
                        await gs_subscribe.delete_subscribe("single", SUBSCRIBE_TASK, ev)
                        break
        except Exception:
            logger.warning("[GitPing] 从 GsCore 取消订阅失败", exc_info=True)

    await bot.send("已取消订阅。" if removed else "本群没有订阅这个仓库。")


@push_sv.on_fullmatch(("订阅列表", "git订阅列表"), block=True, prefix=False)
async def list_subscriptions_command(bot: Bot, ev: Event) -> None:
    subs = await store.list_subscriptions()
    mine = [s for s in subs if s.bot_id == ev.bot_id and s.group_id == ev.group_id]
    if not mine:
        await bot.send("本群尚未订阅任何仓库。")
        return
    lines = "\n".join(
        f"{i}. {PLATFORM_LABELS[s.platform]} · {s.slug}" for i, s in enumerate(mine, 1)
    )
    await bot.send(f"本群订阅的仓库：\n{lines}")


# ── 帮助 ─────────────────────────────────────────────────────────────────────


@git_sv.on_fullmatch(("git帮助", "GitPing帮助", "git菜单"), block=True, prefix=False)
async def help_command(bot: Bot, ev: Event) -> None:
    platforms = " / ".join(PLATFORM_LABELS[p] for p in PLATFORM_ORDER)
    await bot.send(
        "GitPing · Git 仓库查询插件\n"
        f"支持平台：{platforms}\n\n"
        "· git仓库 [平台:]owner/repo — 查询仓库信息\n"
        "· git提交 [数量] [仓库] — 查看提交记录\n"
        "· git提交详情 <sha> [仓库] — 查看单条提交\n"
        "· git版本 [tag] [仓库] — 查看版本发布\n"
        "· git绑定 [平台:]owner/repo — 为本群绑定默认仓库\n"
        "· git解绑 — 解除绑定\n"
        "· git订阅 / git取消订阅 / git订阅列表 — 管理更新推送\n\n"
        "仓库可写 owner/repo、完整 URL，或加平台前缀（如 gitee:owner/repo）。"
    )


# ── 参数解析 ─────────────────────────────────────────────────────────────────


async def _split_args(ev: Event) -> tuple[list[str], RepoRef | None]:
    """把命令参数拆成「其它参数」与「仓库引用」。

    仓库参数可以出现在任意位置，剩下的按顺序作为其它参数。
    """
    parts = ev.text.strip().split()
    others: list[str] = []
    ref: RepoRef | None = None

    for part in parts:
        candidate = _resolve_ref(part)
        if candidate is not None and ref is None:
            ref = await _resolve_target(ev, part)
            continue
        others.append(part)

    if ref is None:
        ref = await _group_repo(ev)
    return others, ref


logger.debug("[GitPing] 命令已注册")
