"""GitPing - GsCore 的 Git 仓库查询与订阅插件。

支持 GitHub / Gitee / GitCode / CNB 四个平台，提供仓库信息、
提交记录、版本发布卡片，以及新提交与新版本的定时推送。
"""

from gsuid_core.sv import Plugins

Plugins(
    name="GitPing",
    disable_force_prefix=True,
    allow_empty_prefix=True,
    alias=["GitPing", "git", "Git"],
)

from . import gitping_config  # noqa: F401
from . import gitping_core  # noqa: F401
