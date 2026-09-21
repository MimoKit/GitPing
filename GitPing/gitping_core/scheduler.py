"""新提交与新版本的定时检查与推送。

推送走 ``gss.active_bot[bot_id].target_send(...)``：这是 GsCore 里把消息
发到指定群的标准做法（参见内置插件的推送实现）。只用当前确实在线的 Bot，
离线时跳过而不是硬塞，避免消息丢失。

订阅登记走 GsCore 的 ``gs_subscribe``，与框架的订阅体系保持一致，
用户能在 Web 控制台看到自己订阅了什么。
"""

from __future__ import annotations

import httpx

from gsuid_core.aps import scheduler
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment

from ..gitping_config import config_bool, config_int, config_text
from ..gitping_render import render_commit, render_release
from . import store
from .platforms import GitClient, GitError, Platform
from .store import SUBSCRIBE_TASK, Subscription

_TOKEN_KEYS: dict[Platform, str] = {
    "github": "github_token",
    "gitee": "gitee_token",
    "gitcode": "gitcode_token",
    "cnb": "cnb_token",
}


def _client(platform: Platform) -> GitClient:
    token = config_text(_TOKEN_KEYS[platform], "")
    return GitClient(platform, token, timeout=float(config_int("request_timeout", 15)))


async def _latest_commit(client: GitClient, sub: Subscription) -> tuple[str, bytes | None]:
    """返回 (最新 sha, 有新提交时的卡片)。

    首次订阅（游标为空）只记游标不推送，否则用户刚订阅就会被历史提交刷屏。
    """
    commits = await client.commits(sub.owner, sub.repo, limit=1)
    if not commits:
        return sub.last_commit, None
    latest = commits[0]
    if latest.sha == sub.last_commit or not sub.last_commit:
        return latest.sha, None
    card = await render_commit(sub.platform, sub.owner, sub.repo, latest, note="新提交")
    return latest.sha, card


async def _latest_release(client: GitClient, sub: Subscription) -> tuple[str, bytes | None]:
    rel = await client.release_detail(sub.owner, sub.repo)
    if rel is None:
        return sub.last_tag, None
    if rel.tag == sub.last_tag or not sub.last_tag:
        return rel.tag, None
    card = await render_release(sub.platform, sub.owner, sub.repo, rel, note="新版本发布")
    return rel.tag, card


def _find_bot(bot_id: str):
    """跨适配器与 ID 兼容获取活跃 Bot 实例。"""
    if bot_id in gss.active_bot:
        return gss.active_bot[bot_id]
    for k, b in gss.active_bot.items():
        if bot_id.lower() in k.lower() or k.lower() in bot_id.lower():
            return b
    if gss.active_bot:
        return next(iter(gss.active_bot.values()))
    return None


async def _push_fallback(sub: Subscription, card: bytes, kind: str) -> bool:
    """本地兜底推送（当 gs_subscribe 未命中或异常时触发）。"""
    bot = _find_bot(sub.bot_id)
    if bot is None:
        logger.warning(
            f"[GitPing] 当前无可用活跃 Bot 实例（待发 Bot ID: {sub.bot_id}，"
            f"当前活跃列表: {list(gss.active_bot.keys())}），跳过 {sub.slug} 推送"
        )
        return False
    try:
        await bot.target_send(
            [MessageSegment.image(card)],
            "group",
            sub.group_id,
            sub.bot_id,
            "",
            "",
        )
        logger.info(f"[GitPing] 🚀 [Fallback] 成功推送 {sub.slug} 的{kind}到群 {sub.group_id}")
        return True
    except Exception:
        logger.warning(f"[GitPing] [Fallback] 推送 {sub.slug} 到群 {sub.group_id} 失败", exc_info=True)
        return False


async def _dispatch_push(sub: Subscription, card: bytes, kind: str, target_group: str = "") -> int:
    """优先使用 GsCore 官方标准 gs_subscribe 体系进行推送分发；若未注册则走本地兜底。"""
    pushed = 0
    try:
        from gsuid_core.subscribe import gs_subscribe

        task_name = f"[GitPing] {sub.platform}:{sub.owner}/{sub.repo}"
        gs_subs = await gs_subscribe.get_subscribe(task_name=task_name)
        if not gs_subs:
            legacy_subs = await gs_subscribe.get_subscribe(task_name=SUBSCRIBE_TASK)
            if legacy_subs:
                gs_subs = [
                    s
                    for s in legacy_subs
                    if getattr(s, "extra_data", "") == f"{sub.platform}:{sub.owner}/{sub.repo}"
                ]

        if gs_subs:
            for gs_sub in gs_subs:
                if target_group and str(getattr(gs_sub, "group_id", "")) != str(target_group):
                    continue
                try:
                    await gs_sub.send(reply=card)
                    logger.info(f"[GitPing] 🚀 [gs_subscribe] 成功推送 {sub.slug} 的{kind}到群 {gs_sub.group_id}")
                    pushed += 1
                except Exception:
                    logger.warning(
                        f"[GitPing] [gs_subscribe] 推送 {sub.slug} 到群 {getattr(gs_sub, 'group_id', '')} 失败",
                        exc_info=True,
                    )
            if pushed > 0 or (target_group and any(str(getattr(s, "group_id", "")) == str(target_group) for s in gs_subs)):
                return pushed
    except Exception as e:
        logger.warning(f"[GitPing] 调用 gs_subscribe 推送异常: {e}，尝试本地兜底", exc_info=True)

    if not target_group or str(sub.group_id) == str(target_group):
        if await _push_fallback(sub, card, kind):
            pushed += 1

    return pushed


async def check_subscriptions(*, force_push: bool = False, target_group: str = "") -> int:
    """检查所有订阅，有新动态就推送。

    :param force_push: 是否强制推送一次最新状态（用于测试命令）
    :param target_group: 仅对指定群检查/推送（空表示全部）
    :return: 成功推送的次数
    """
    if not force_push and not config_bool("push_enabled", True):
        logger.debug("[GitPing] 订阅推送全局开关已关闭，跳过检查")
        return 0

    subs = await store.list_subscriptions()
    if not subs:
        return 0

    if target_group:
        subs = [s for s in subs if s.group_id == target_group]
        if not subs:
            return 0

    logger.info(f"[GitPing] ⏰ 开始执行订阅检查，共 {len(subs)} 条订阅 (force_push={force_push})...")
    pushed_count = 0

    for sub in subs:
        client = _client(sub.platform)
        new_commit = ""
        new_tag = ""

        try:
            commits = await client.commits(sub.owner, sub.repo, limit=1)
            if commits:
                latest = commits[0]
                new_commit = latest.sha
                should_push = force_push or (sub.last_commit and latest.sha != sub.last_commit)
                if should_push:
                    card = await render_commit(sub.platform, sub.owner, sub.repo, latest, note="新提交推送")
                    pushed_count += await _dispatch_push(sub, card, "新提交", target_group=target_group)
        except Exception as exc:
            logger.warning(f"[GitPing] 检查 {sub.slug} 提交失败: {exc}")

        try:
            rel = await client.release_detail(sub.owner, sub.repo)
            if rel:
                new_tag = rel.tag
                should_push_rel = force_push or (sub.last_tag and rel.tag != sub.last_tag)
                if should_push_rel:
                    card = await render_release(sub.platform, sub.owner, sub.repo, rel, note="新版本发布")
                    pushed_count += await _dispatch_push(sub, card, "新版本", target_group=target_group)
        except Exception as exc:
            logger.warning(f"[GitPing] 检查 {sub.slug} 版本失败: {exc}")

        if new_commit or new_tag:
            await store.update_subscription_cursor(sub.key, commit=new_commit, tag=new_tag)

    logger.info(f"[GitPing] 🏁 订阅检查完成，本次共推送 {pushed_count} 条动态")
    return pushed_count


# 默认每 2 分钟轮询一次订阅仓库
@scheduler.scheduled_job("interval", minutes=2, id="gitping_check_subscriptions")
async def gitping_poll() -> None:
    try:
        await check_subscriptions()
    except Exception:
        logger.exception("[GitPing] 订阅检查任务异常")
