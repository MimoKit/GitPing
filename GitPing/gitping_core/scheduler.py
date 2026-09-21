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


async def _push(sub: Subscription, card: bytes, kind: str) -> None:
    """把卡片推到订阅的群。Bot 不在线时跳过并记一条。"""
    bot = gss.active_bot.get(sub.bot_id)
    if bot is None:
        logger.debug(f"[GitPing] Bot {sub.bot_id} 不在线，跳过 {sub.slug} 的推送")
        return
    try:
        await bot.target_send(
            [MessageSegment.image(card)],
            "group",
            sub.group_id,
            sub.bot_id,
            "",
            "",
        )
    except Exception:
        logger.warning(f"[GitPing] 推送 {sub.slug} 到群 {sub.group_id} 失败", exc_info=True)
    else:
        logger.info(f"[GitPing] 已推送 {sub.slug} 的{kind}到群 {sub.group_id}")


async def check_subscriptions() -> None:
    """检查所有订阅，有新动态就推送。"""
    if not config_bool("push_enabled", False):
        return

    subs = await store.list_subscriptions()
    if not subs:
        return

    logger.debug(f"[GitPing] 开始检查 {len(subs)} 条订阅")
    for sub in subs:
        client = _client(sub.platform)
        new_commit = ""
        new_tag = ""

        try:
            sha, card = await _latest_commit(client, sub)
            if card is not None:
                await _push(sub, card, "新提交")
            new_commit = sha
        except (GitError, httpx.HTTPError) as exc:
            logger.warning(f"[GitPing] 检查 {sub.slug} 提交失败: {exc}")
        except (OSError, RuntimeError) as exc:
            logger.warning(f"[GitPing] 渲染 {sub.slug} 提交卡片失败: {exc}")

        try:
            tag, card = await _latest_release(client, sub)
            if card is not None:
                await _push(sub, card, "新版本")
            new_tag = tag
        except (GitError, httpx.HTTPError) as exc:
            logger.warning(f"[GitPing] 检查 {sub.slug} 版本失败: {exc}")
        except (OSError, RuntimeError) as exc:
            logger.warning(f"[GitPing] 渲染 {sub.slug} 版本卡片失败: {exc}")

        if new_commit or new_tag:
            await store.update_subscription_cursor(sub.key, commit=new_commit, tag=new_tag)


# 轮询间隔从配置读取；@scheduled_job 在导入时求值，因此改配置需重启 Core
@scheduler.scheduled_job("interval", minutes=10, id="gitping_check_subscriptions")
async def gitping_poll() -> None:
    try:
        await check_subscriptions()
    except Exception:
        # 定时任务里未捕获的异常会让 APScheduler 移除该任务，这里兜住
        logger.exception("[GitPing] 订阅检查任务异常")
