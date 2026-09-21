"""GitPing 核心：平台客户端、数据持久化与命令。"""

from .platforms import (
    CNB,
    GITCODE,
    GITEE,
    GITHUB,
    PLATFORM_COLORS,
    PLATFORM_LABELS,
    PLATFORM_ORDER,
    CommitInfo,
    GitClient,
    GitError,
    Platform,
    ReleaseAsset,
    ReleaseInfo,
    RepoInfo,
    RepoRef,
    parse_platform,
    parse_repo_ref,
)
from .store import Binding, Subscription

# 导入命令模块以执行 @git_sv/@push_sv 装饰器完成注册。
# 少了这一行，命令不会生效——装饰器只在模块被 import 时运行。
from . import commands  # noqa: E402, F401
from . import scheduler  # noqa: E402, F401

__all__ = [
    "CNB",
    "GITCODE",
    "GITEE",
    "GITHUB",
    "PLATFORM_COLORS",
    "PLATFORM_LABELS",
    "PLATFORM_ORDER",
    "Binding",
    "CommitInfo",
    "GitClient",
    "GitError",
    "Platform",
    "ReleaseAsset",
    "ReleaseInfo",
    "RepoInfo",
    "RepoRef",
    "Subscription",
    "parse_platform",
    "parse_repo_ref",
]
