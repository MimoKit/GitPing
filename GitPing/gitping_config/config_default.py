"""GitPing WebConsole 配置项。

令牌按平台分别配置：GitHub 的公开仓库无需令牌也能查询，
其余平台（Gitee / GitCode / CNB）必须提供访问令牌。
"""

from typing import Dict

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsBoolConfig,
    GsIntConfig,
    GsStrConfig,
)


CONFIG_DEFAULT: Dict[str, GSC] = {
    "github_token": GsStrConfig(
        title="GitHub 访问令牌",
        desc="可选；查询公开仓库无需填写。私有仓库或高频调用建议填写 Personal Access Token",
        data="",
        secret=True,
    ),
    "gitee_token": GsStrConfig(
        title="Gitee 访问令牌",
        desc="Gitee 私有仓库必需；在 Gitee 设置 → 私人令牌 生成",
        data="",
        secret=True,
    ),
    "gitcode_token": GsStrConfig(
        title="GitCode 访问令牌",
        desc="GitCode 私有仓库必需",
        data="",
        secret=True,
    ),
    "cnb_token": GsStrConfig(
        title="CNB 访问令牌",
        desc="CNB（cnb.cool）私有仓库必需",
        data="",
        secret=True,
    ),
    "request_timeout": GsIntConfig(
        title="请求超时（秒）",
        desc="调用各平台 API 的超时时间",
        data=15,
        max_value=60,
    ),
    "render_backend": GsStrConfig(
        title="卡片渲染方式",
        desc=(
            "auto=优先用无头浏览器（排版最准），不可用时自动回退到 GsCore 内置渲染；"
            "browser=只用无头浏览器；"
            "builtin=只用 GsCore 内置渲染（无需 Chromium，依赖最轻）"
        ),
        data="auto",
        options=["auto", "browser", "builtin"],
    ),
    "commit_limit": GsIntConfig(
        title="提交记录条数",
        desc="查询提交记录时默认展示的条数",
        data=5,
        max_value=20,
    ),
    "push_enabled": GsBoolConfig(
        title="启用推送订阅",
        desc="开启后可按仓库订阅新提交与新版本发布",
        data=False,
    ),
    "push_interval": GsIntConfig(
        title="推送轮询间隔（分钟）",
        desc="定时检查各订阅仓库是否有更新",
        data=10,
        max_value=120,
    ),
}
