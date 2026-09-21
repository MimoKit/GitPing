"""各 Git 平台的 API 适配层。

四个平台的响应结构差异很大，这里统一收敛成 RepoInfo / CommitInfo /
ReleaseInfo 三个结构，上层命令与渲染只依赖这三个结构。

平台差异备忘：
- GitHub 用 ``Authorization: Bearer``，release 有 assets；
- Gitee 用 ``access_token`` 查询参数，release 接口路径不同；
- GitCode 兼容 GitHub 的响应形状，但地址与鉴权头不同；
- CNB（cnb.cool）路径带仓库全名，commit 列表接口是 ``/git/commits``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import httpx

from gsuid_core.logger import logger

Platform = Literal["github", "gitee", "gitcode", "cnb"]

GITHUB: Platform = "github"
GITEE: Platform = "gitee"
GITCODE: Platform = "gitcode"
CNB: Platform = "cnb"

PLATFORM_ORDER: tuple[Platform, ...] = (GITHUB, GITEE, GITCODE, CNB)

PLATFORM_LABELS: dict[Platform, str] = {
    GITHUB: "GitHub",
    GITEE: "Gitee",
    GITCODE: "GitCode",
    CNB: "CNB",
}

# 平台主题色，用于卡片上的平台标识
PLATFORM_COLORS: dict[Platform, str] = {
    GITHUB: "#24292f",
    GITEE: "#c71d23",
    GITCODE: "#2f6bff",
    CNB: "#00a6a6",
}

# 各平台 API 基址与鉴权方式
_API_BASE: dict[Platform, str] = {
    GITHUB: "https://api.github.com",
    GITEE: "https://gitee.com/api/v5",
    GITCODE: "https://api.gitcode.com/api/v5",
    CNB: "https://api.cnb.cool",
}

# 平台别名：用户在命令里可能写中文或各种大小写
PLATFORM_ALIASES: dict[str, Platform] = {
    "github": GITHUB, "gh": GITHUB, "ghub": GITHUB,
    "gitee": GITEE, "码云": GITEE, "ge": GITEE,
    "gitcode": GITCODE, "gc": GITCODE, "csdn": GITCODE,
    "cnb": CNB, "cnbcool": CNB, "cnb.cool": CNB,
}


def parse_platform(raw: str) -> Platform | None:
    """把用户输入的平台名归一化。"""
    return PLATFORM_ALIASES.get(raw.strip().lower())


@dataclass(frozen=True, slots=True)
class RepoInfo:
    platform: Platform
    owner: str
    repo: str
    full_name: str
    description: str = ""
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    issues: int = 0
    language: str = ""
    license_name: str = ""
    default_branch: str = "main"
    homepage: str = ""
    updated_at: str = ""
    avatar: str = ""
    private: bool = False

    @property
    def url(self) -> str:
        return _repo_url(self.platform, self.owner, self.repo)


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    message: str
    author: str
    author_email: str = ""
    date: str = ""
    additions: int = 0
    deletions: int = 0
    files_changed: int = 0

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def title(self) -> str:
        """提交信息首行。"""
        return self.message.strip().splitlines()[0] if self.message.strip() else "(无提交信息)"

    @property
    def body(self) -> str:
        """提交信息除首行外的正文。"""
        lines = self.message.strip().splitlines()
        return "\n".join(lines[1:]).strip() if len(lines) > 1 else ""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    size: int = 0
    downloads: int = 0


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    platform: Platform
    owner: str
    repo: str
    tag: str
    name: str = ""
    body: str = ""
    author: str = ""
    created_at: str = ""
    prerelease: bool = False
    target: str = ""
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        return f"{_repo_url(self.platform, self.owner, self.repo)}/releases/tag/{self.tag}"


class GitError(RuntimeError):
    """可直接提示用户的错误。"""


def _repo_url(platform: Platform, owner: str, repo: str) -> str:
    if platform == GITHUB:
        return f"https://github.com/{owner}/{repo}"
    if platform == GITEE:
        return f"https://gitee.com/{owner}/{repo}"
    if platform == GITCODE:
        return f"https://gitcode.com/{owner}/{repo}"
    return f"https://cnb.cool/{owner}/{repo}"


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _first(data: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


class GitClient:
    """四个平台统一的只读客户端。"""

    def __init__(self, platform: Platform, token: str = "", timeout: float = 15.0) -> None:
        self.platform = platform
        self.token = token
        self.timeout = timeout

    @property
    def label(self) -> str:
        return PLATFORM_LABELS[self.platform]

    # ── 请求 ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "GitPing-GsCore-Plugin",
        }
        if self.token:
            if self.platform in (GITHUB, GITCODE, CNB):
                headers["Authorization"] = f"Bearer {self.token}"
            else:
                # Gitee 用 access_token 查询参数，见 _params
                headers["Authorization"] = f"token {self.token}"
        return headers

    def _params(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        params: dict[str, object] = dict(extra or {})
        if self.token and self.platform == GITEE:
            params["access_token"] = self.token
        return params

    async def _get(self, path: str, *, params: dict[str, object] | None = None) -> object:
        url = f"{_API_BASE[self.platform]}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url, headers=self._headers(), params=self._params(params))
        except httpx.HTTPError as exc:
            logger.warning(f"[GitPing] {self.label} 请求失败 {path}: {exc}")
            raise GitError(f"连接 {self.label} 失败，请检查网络后重试。") from exc

        if response.status_code == 404:
            raise GitError("仓库不存在，或这是一个私有仓库（需要配置访问令牌）。")
        if response.status_code in (401, 403):
            raise GitError(f"{self.label} 拒绝了请求，请检查访问令牌是否有足够权限。")
        if response.status_code == 429:
            raise GitError(f"{self.label} 请求过于频繁，请稍后再试。")
        if response.status_code >= 400:
            raise GitError(f"{self.label} 返回异常（HTTP {response.status_code}）。")

        try:
            return response.json()
        except ValueError as exc:
            raise GitError(f"{self.label} 返回了无法解析的内容。") from exc

    # ── 仓库 ─────────────────────────────────────────────────────────────

    async def repo_info(self, owner: str, repo: str) -> RepoInfo:
        if self.platform == CNB:
            data = await self._get(f"/{owner}/{repo}")
        else:
            data = await self._get(f"/repos/{owner}/{repo}")
        if not isinstance(data, dict):
            raise GitError(f"{self.label} 返回的仓库信息格式异常。")

        license_obj = data.get("license") or data.get("license_name")
        if isinstance(license_obj, dict):
            license_name = _text(_first(license_obj, "spdx_id", "name", "key"))
        else:
            license_name = _text(license_obj)

        return RepoInfo(
            platform=self.platform,
            owner=owner,
            repo=repo,
            full_name=_text(_first(data, "full_name", "path_with_namespace", "path")) or f"{owner}/{repo}",
            description=_text(_first(data, "description", "desc", "intro")) or "暂无简介",
            stars=_int(_first(data, "stargazers_count", "stars_count", "stars", "star_count")),
            forks=_int(_first(data, "forks_count", "forks", "fork_count")),
            watchers=_int(_first(data, "subscribers_count", "watchers_count", "watchers")),
            issues=_int(_first(data, "open_issues_count", "issues_count")),
            language=_text(_first(data, "language", "lang")) or "未知",
            license_name=license_name or "未标注",
            default_branch=_text(_first(data, "default_branch", "default_branch_name")) or "main",
            homepage=_text(_first(data, "homepage", "html_url", "web_url")),
            updated_at=_text(_first(data, "updated_at", "pushed_at", "last_activity_at")),
            avatar=self._avatar(data),
            private=bool(data.get("private") or data.get("visibility") == "private"),
        )

    def _avatar(self, data: dict[str, object]) -> str:
        owner_obj = data.get("owner") or data.get("namespace")
        if isinstance(owner_obj, dict):
            return _text(_first(owner_obj, "avatar_url", "avatar", "image"))
        return ""

    # ── 提交 ─────────────────────────────────────────────────────────────

    async def commits(self, owner: str, repo: str, limit: int = 5, ref: str = "") -> list[CommitInfo]:
        if self.platform == CNB:
            path = f"/{owner}/{repo}/-/git/commits"
            params: dict[str, object] = {"page_size": limit}
        else:
            path = f"/repos/{owner}/{repo}/commits"
            params = {"per_page": limit}
        if ref:
            params["sha" if self.platform != GITEE else "sha"] = ref

        data = await self._get(path, params=params)
        if not isinstance(data, list):
            return []

        result: list[CommitInfo] = []
        for raw in data[:limit]:
            if not isinstance(raw, dict):
                continue
            # GitHub/GitCode 用 commit.author，Gitee 用 commit.author.name 且顶层有 author
            inner = raw.get("commit")
            inner = inner if isinstance(inner, dict) else {}
            author_obj = inner.get("author") if isinstance(inner.get("author"), dict) else {}
            if not author_obj:
                author_obj = raw.get("author") if isinstance(raw.get("author"), dict) else {}
            committer_obj = inner.get("committer") if isinstance(inner.get("committer"), dict) else {}

            message = _text(_first(inner, "message", "title")) or _text(raw.get("title"))
            date = _text(_first(author_obj, "date")) or _text(_first(committer_obj, "date")) or _text(
                raw.get("created_at")
            )
            result.append(
                CommitInfo(
                    sha=_text(_first(raw, "sha", "id", "hash")),
                    message=message or "(无提交信息)",
                    author=_text(_first(author_obj, "name", "login")) or _text(raw.get("author_name")) or "未知",
                    author_email=_text(author_obj.get("email")),
                    date=date,
                    additions=_int(raw.get("additions") or (raw.get("stats") or {}).get("additions") if isinstance(raw.get("stats"), dict) else 0),
                    deletions=_int(raw.get("deletions") or (raw.get("stats") or {}).get("deletions") if isinstance(raw.get("stats"), dict) else 0),
                    files_changed=_int(_first(raw, "files_changed", "total")),
                )
            )
        return result

    async def commit_detail(self, owner: str, repo: str, sha: str) -> CommitInfo | None:
        if self.platform == CNB:
            path = f"/{owner}/{repo}/-/git/commits/{sha}"
        else:
            path = f"/repos/{owner}/{repo}/commits/{sha}"
        data = await self._get(path)
        if not isinstance(data, dict):
            return None

        inner = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        author_obj = inner.get("author") if isinstance(inner.get("author"), dict) else {}
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        return CommitInfo(
            sha=_text(_first(data, "sha", "id", "hash")),
            message=_text(inner.get("message")) or "(无提交信息)",
            author=_text(_first(author_obj, "name", "login")) or "未知",
            author_email=_text(author_obj.get("email")),
            date=_text(author_obj.get("date")),
            additions=_int(stats.get("additions")),
            deletions=_int(stats.get("deletions")),
            files_changed=_int(_first(stats, "total")) or len(data.get("files") or []),
        )

    # ── 版本发布 ─────────────────────────────────────────────────────────

    async def releases(self, owner: str, repo: str, limit: int = 5) -> list[ReleaseInfo]:
        if self.platform == CNB:
            path = f"/{owner}/{repo}/-/releases"
            params: dict[str, object] = {"page_size": limit}
        else:
            path = f"/repos/{owner}/{repo}/releases"
            params = {"per_page": limit}
        data = await self._get(path, params=params)
        if not isinstance(data, list):
            return []
        return [info for raw in data[:limit] if (info := self._parse_release(raw)) is not None]

    async def release_detail(self, owner: str, repo: str, tag: str = "") -> ReleaseInfo | None:
        if tag:
            if self.platform == CNB:
                path = f"/{owner}/{repo}/-/releases/{tag}"
            else:
                path = f"/repos/{owner}/{repo}/releases/tags/{tag}"
            data = await self._get(path)
        else:
            if self.platform == CNB:
                path = f"/{owner}/{repo}/-/releases"
                params: dict[str, object] = {"page_size": 1}
            else:
                path = f"/repos/{owner}/{repo}/releases"
                params = {"per_page": 1}
            data = await self._get(path, params=params)
            if isinstance(data, list):
                data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return self._parse_release(data)

    def _parse_release(self, raw: object) -> ReleaseInfo | None:
        if not isinstance(raw, dict):
            return None
        tag = _text(_first(raw, "tag_name", "tag", "name"))
        if not tag:
            return None
        author_obj = raw.get("author") if isinstance(raw.get("author"), dict) else {}
        assets_raw = raw.get("assets") if isinstance(raw.get("assets"), list) else []
        assets = tuple(
            ReleaseAsset(
                name=_text(_first(a, "name", "file_name")),
                size=_int(_first(a, "size", "file_size")),
                downloads=_int(_first(a, "download_count", "downloads")),
            )
            for a in assets_raw
            if isinstance(a, dict) and _text(a.get("name") or a.get("file_name"))
        )
        return ReleaseInfo(
            platform=self.platform,
            owner="",
            repo="",
            tag=tag,
            name=_text(raw.get("name")) or tag,
            body=_text(_first(raw, "body", "description", "note")) or "",
            author=_text(_first(author_obj, "login", "name")) or _text(raw.get("author_name")),
            created_at=_text(_first(raw, "created_at", "published_at", "createdAt")),
            prerelease=bool(raw.get("prerelease") or raw.get("is_prerelease")),
            target=_text(_first(raw, "target_commitish", "target")),
            assets=assets,
        )


# 仓库全名的解析：支持 "owner/repo"、完整 URL、以及带平台前缀的写法
_URL_RE = re.compile(
    r"^(?:https?://)?(?P<host>github\.com|gitee\.com|gitcode\.com|cnb\.cool)/"
    r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s?#]+)",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+?)(?:\.git)?$")

_HOST_TO_PLATFORM: dict[str, Platform] = {
    "github.com": GITHUB,
    "gitee.com": GITEE,
    "gitcode.com": GITCODE,
    "cnb.cool": CNB,
}


@dataclass(frozen=True, slots=True)
class RepoRef:
    platform: Platform | None
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_repo_ref(raw: str) -> RepoRef | None:
    """解析仓库引用。

    支持三种写法：
    - ``owner/repo``（平台由调用方决定或取默认）
    - ``https://github.com/owner/repo``（从域名推断平台）
    - ``github:owner/repo``（显式指定平台）
    """
    text = raw.strip()
    if not text:
        return None

    # platform:owner/repo
    if ":" in text and "//" not in text:
        prefix, _, rest = text.partition(":")
        if (platform := parse_platform(prefix)) is not None:
            slug = _SLUG_RE.match(rest.strip())
            if slug:
                return RepoRef(platform, slug.group("owner"), slug.group("repo"))
            return None

    if match := _URL_RE.match(text):
        host = match.group("host").lower()
        platform = _HOST_TO_PLATFORM.get(host)
        repo = match.group("repo")
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        return RepoRef(platform, match.group("owner"), repo)

    if slug := _SLUG_RE.match(text):
        return RepoRef(None, slug.group("owner"), slug.group("repo"))
    return None
