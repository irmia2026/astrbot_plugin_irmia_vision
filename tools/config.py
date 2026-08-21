"""
配置模块
"""

from __future__ import annotations

_CONFIG: dict = {}
_PLUGIN_DIR: str = ""
_PROVIDERS: list[dict] = []


def set_config(config: dict, plugin_dir: str) -> None:
    global _CONFIG, _PLUGIN_DIR
    _CONFIG = config
    _PLUGIN_DIR = plugin_dir


def set_providers(providers: list[dict]) -> None:
    """存储从 AstrBot context 读取的 provider 列表。"""
    global _PROVIDERS
    _PROVIDERS = providers


def get_config() -> dict:
    return _CONFIG


def get_plugin_dir() -> str:
    return _PLUGIN_DIR


def get_providers() -> list[dict]:
    return _PROVIDERS


def get_vl_model_config() -> dict:
    return _CONFIG.get("vl_model", {})


def _provider_to_vl_config(provider: dict) -> dict:
    """将 AstrBot provider_config 转换为本插件使用的 VL 模型配置格式。"""
    keys = provider.get("key", [])
    if isinstance(keys, list):
        api_key = keys[0] if keys else ""
    elif isinstance(keys, str):
        api_key = keys
    else:
        api_key = ""
    return {
        "provider": provider.get("type", "openai_chat_completion"),
        "base_url": provider.get("api_base", "https://api.openai.com/v1"),
        "api_key": api_key,
        "model": provider.get("model", "gpt-4o"),
        "timeout": provider.get("timeout", 120.0),
        "concurrency": _CONFIG.get("vl_model", {}).get("concurrency", 50),
        "max_retries": _CONFIG.get("vl_model", {}).get("max_retries", 2),
    }


def resolve_provider_chain() -> list[dict]:
    """解析降级链：
    1. 如果配置了 vl_provider_ids（逗号分隔的 provider id 列表），按指定顺序解析；
    2. 如果 vl_provider_ids 为空但 AstrBot 有已保存的模型，自动使用全部已保存模型（按保存顺序）；
    3. 以上都没有时，回退到 vl_model 手动配置；
    4. 返回 VL 配置格式的列表，第一个是主选，后续是降级。
    """
    vl_provider_ids_raw = _CONFIG.get("vl_provider_ids", "")
    if isinstance(vl_provider_ids_raw, str):
        ids = [x.strip() for x in vl_provider_ids_raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(vl_provider_ids_raw, list):
        ids = [str(x).strip() for x in vl_provider_ids_raw if str(x).strip()]
    else:
        ids = []

    if ids and _PROVIDERS:
        provider_map = {p.get("id", ""): p for p in _PROVIDERS}
        chain = []
        for pid in ids:
            p = provider_map.get(pid)
            if p:
                chain.append(_provider_to_vl_config(p))
        if chain:
            return chain

    # vl_provider_ids 为空时，自动使用所有已保存的模型
    if not ids and _PROVIDERS:
        chain = [_provider_to_vl_config(p) for p in _PROVIDERS]
        if chain:
            return chain

    # 回退到 vl_model 手动配置
    vl_model = _CONFIG.get("vl_model", {})
    if vl_model and vl_model.get("api_key"):
        return [vl_model]

    return []
