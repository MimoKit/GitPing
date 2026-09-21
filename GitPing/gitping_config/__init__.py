"""GitPing 配置。"""

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT

CONFIG_PATH = get_res_path() / "GitPing" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

GITPING_CONFIG = StringConfig("GitPing", CONFIG_PATH, CONFIG_DEFAULT)
GITPING_CONFIG.plugin_name = "GitPing"


def config_text(key: str, default: str) -> str:
    """读字符串配置，空值回退默认。"""
    value = GITPING_CONFIG.get_config(key, default).data
    return value if isinstance(value, str) and value.strip() else default


def config_int(key: str, default: int) -> int:
    value = GITPING_CONFIG.get_config(key, default).data
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def config_bool(key: str, default: bool) -> bool:
    value = GITPING_CONFIG.get_config(key, default).data
    return value if isinstance(value, bool) else default
