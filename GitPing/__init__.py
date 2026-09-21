"""GitPing - GsCore 的 Git 仓库查询与订阅插件。

支持 GitHub / Gitee / GitCode / CNB 四个平台，提供仓库信息、
提交记录、版本发布卡片，以及新提交与新版本的定时推送。

前缀说明：触发词一律写成裸词（如「仓库」），由框架自动拼上 force_prefix。
用户在群里发「git仓库」「git提交」即可命中。
"""

from gsuid_core.sv import Plugins

Plugins(
    name="GitPing",
    # git 为前缀：支持「git仓库」与带空格的「git 仓库」，大小写兼容
    force_prefix=["git", "git ", "Git", "Git "],
    allow_empty_prefix=False,
    alias=["GitPing", "Git", "gitping"],
)

from . import gitping_config  # noqa: F401
from . import gitping_core  # noqa: F401
