"""
配置模块
"""

from __future__ import annotations

_CONFIG: dict = {}
_PLUGIN_DIR: str = ""


def set_config(config: dict, plugin_dir: str) -> None:
    global _CONFIG, _PLUGIN_DIR
    _CONFIG = config
    _PLUGIN_DIR = plugin_dir


def get_config() -> dict:
    return _CONFIG


def get_plugin_dir() -> str:
    return _PLUGIN_DIR


def get_vl_model_config() -> dict:
    return _CONFIG.get("vl_model", {})
