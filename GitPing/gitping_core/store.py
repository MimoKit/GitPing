"""仓库绑定与推送订阅的持久化。

数据量很小（每个群一条绑定、每条订阅一个目标列表），用单个 JSON 文件即可，
不需要引入数据库表与迁移。写入走「读-改-写」并在进程内加锁，避免并发覆盖。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger

from .platforms import Platform, parse_platform

STORE_PATH: Path = get_res_path() / "GitPing" / "store.json"

# 订阅在 GsCore 订阅表里的任务名，Web 控制台会以此展示。
# 放在这里而不是 scheduler，是为了让命令层不必反向依赖定时任务模块。
SUBSCRIBE_TASK = "GitPing仓库更新"
_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class Binding:
    """群里绑定的默认仓库。"""

    platform: Platform
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class Subscription:
    """一条推送订阅：某个仓库有新动态时通知到哪里。"""

    platform: Platform
    owner: str
    repo: str
    bot_id: str
    group_id: str
    last_commit: str = ""
    last_tag: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.slug}"


def _empty() -> dict[str, object]:
    return {"bindings": {}, "subscriptions": []}


def _read() -> dict[str, object]:
    """读数据文件；缺失或损坏时回退为空结构，不让插件因此起不来。"""
    if not STORE_PATH.is_file():
        return _empty()
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("[GitPing] 数据文件损坏，已重置", exc_info=True)
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    raw.setdefault("bindings", {})
    raw.setdefault("subscriptions", [])
    return raw


def _write(data: dict[str, object]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _bind_key(bot_id: str, group_id: str) -> str:
    return f"{bot_id}:{group_id}"


# ── 绑定 ─────────────────────────────────────────────────────────────────────


async def set_binding(bot_id: str, group_id: str, binding: Binding) -> None:
    async with _lock:
        data = _read()
        bindings = data["bindings"]
        assert isinstance(bindings, dict)
        bindings[_bind_key(bot_id, group_id)] = asdict(binding)
        _write(data)
    logger.debug(f"[GitPing] 群 {group_id} 绑定 {binding.slug}")


async def get_binding(bot_id: str, group_id: str) -> Binding | None:
    data = _read()
    bindings = data["bindings"]
    if not isinstance(bindings, dict):
        return None
    raw = bindings.get(_bind_key(bot_id, group_id))
    if not isinstance(raw, dict):
        return None
    platform = parse_platform(str(raw.get("platform", "")))
    owner = str(raw.get("owner", ""))
    repo = str(raw.get("repo", ""))
    if platform is None or not owner or not repo:
        return None
    return Binding(platform=platform, owner=owner, repo=repo)


async def remove_binding(bot_id: str, group_id: str) -> bool:
    async with _lock:
        data = _read()
        bindings = data["bindings"]
        if not isinstance(bindings, dict):
            return False
        removed = bindings.pop(_bind_key(bot_id, group_id), None) is not None
        if removed:
            _write(data)
    return removed


# ── 订阅 ─────────────────────────────────────────────────────────────────────


def _load_subscriptions(data: dict[str, object]) -> list[Subscription]:
    raw = data.get("subscriptions")
    if not isinstance(raw, list):
        return []
    result: list[Subscription] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        platform = parse_platform(str(item.get("platform", "")))
        owner = str(item.get("owner", ""))
        repo = str(item.get("repo", ""))
        if platform is None or not owner or not repo:
            continue
        result.append(
            Subscription(
                platform=platform,
                owner=owner,
                repo=repo,
                bot_id=str(item.get("bot_id", "")),
                group_id=str(item.get("group_id", "")),
                last_commit=str(item.get("last_commit", "")),
                last_tag=str(item.get("last_tag", "")),
            )
        )
    return result


async def list_subscriptions() -> list[Subscription]:
    return _load_subscriptions(_read())


async def add_subscription(sub: Subscription) -> bool:
    """新增订阅；已存在同目标同仓库时返回 False。"""
    async with _lock:
        data = _read()
        subs = _load_subscriptions(data)
        for existing in subs:
            if (
                existing.key == sub.key
                and existing.bot_id == sub.bot_id
                and existing.group_id == sub.group_id
            ):
                return False
        subs.append(sub)
        data["subscriptions"] = [asdict(s) for s in subs]
        _write(data)
    return True


async def remove_subscription(bot_id: str, group_id: str, platform: Platform, owner: str, repo: str) -> bool:
    async with _lock:
        data = _read()
        subs = _load_subscriptions(data)
        kept = [
            s
            for s in subs
            if not (
                s.bot_id == bot_id
                and s.group_id == group_id
                and s.platform == platform
                and s.owner == owner
                and s.repo == repo
            )
        ]
        if len(kept) == len(subs):
            return False
        data["subscriptions"] = [asdict(s) for s in kept]
        _write(data)
    return True


async def update_subscription_cursor(key: str, *, commit: str = "", tag: str = "") -> None:
    """推送完成后更新游标，避免重复通知。"""
    async with _lock:
        data = _read()
        subs = _load_subscriptions(data)
        changed = False
        for sub in subs:
            if sub.key != key:
                continue
            if commit and sub.last_commit != commit:
                sub = Subscription(**{**asdict(sub), "last_commit": commit})
                changed = True
            if tag and sub.last_tag != tag:
                sub = Subscription(**{**asdict(sub), "last_tag": tag})
                changed = True
            subs[subs.index(sub)] = sub
        if changed:
            data["subscriptions"] = [asdict(s) for s in subs]
            _write(data)
