"""
配置模块
"""

from __future__ import annotations

from astrbot.api import logger

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


def get_max_batch() -> int:
    """单次批量读图数量上限（账单保险丝）。

    批量读图按张调用 VL 模型计费，误传大目录（如 C:\\）会产生失控费用。
    非法值/0/负数回退默认 2000。"""
    try:
        v = int(_CONFIG.get("max_batch", 2000) or 2000)
        return v if v > 0 else 2000
    except (TypeError, ValueError):
        logger.warning(f"max_batch 配置非法: {_CONFIG.get('max_batch')!r}，回退为 2000")
        return 2000


def _safe_timeout(value, default: float = 120.0) -> float:
    """timeout 归一化：AstrBot provider_config 中可能是字符串/None/非法值，
    下游 float() 或 httpx 收到会直接炸。"""
    try:
        return float(value or default)
    except (TypeError, ValueError):
        logger.warning(f"timeout 配置非法: {value!r}，回退为 {default}")
        return default


def _safe_int(value, default: int) -> int:
    """整数配置归一化：手编 config.json 可能写入字符串/None/非法值，
    下游 int() 收到会穿透成不透明的「工具执行失败」。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(f"整数配置非法: {value!r}，回退为 {default}")
        return default


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
        "timeout": _safe_timeout(provider.get("timeout", 120.0)),
        "concurrency": _safe_int(_CONFIG.get("vl_model", {}).get("concurrency", 50), 50),
        "max_retries": _safe_int(_CONFIG.get("vl_model", {}).get("max_retries", 2), 2),
        # detail / reasoning_effort 与 concurrency/max_retries 一样从全局 vl_model 继承，
        # 否则走 provider 链时 WebUI 配置的 detail/思考强度不生效
        "detail": _CONFIG.get("vl_model", {}).get("detail", "auto"),
        "reasoning_effort": _CONFIG.get("vl_model", {}).get("reasoning_effort", "low"),
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
        vl_model = dict(vl_model)
        vl_model["timeout"] = _safe_timeout(vl_model.get("timeout", 120.0))
        vl_model["max_retries"] = _safe_int(vl_model.get("max_retries", 2), 2)
        vl_model["concurrency"] = _safe_int(vl_model.get("concurrency", 50), 50)
        return [vl_model]

    return []
