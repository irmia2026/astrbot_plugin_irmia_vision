"""
弥亚视觉工具 — AstrBot 插件入口
"""

from __future__ import annotations

import os

from astrbot.api import logger, star
from astrbot.api.star import StarTools

from .tools import config as _tool_config
from .tools._registry import register_tools

_DEFAULT_CONFIG = {
    "vl_model": {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
        "timeout": 120.0,
        "concurrency": 50,
        "max_retries": 2,
    },
    "example": "在 WebUI 配置中填写 vl_model.api_key 即可使用，支持 OpenAI 兼容 API。",
}


class Main(star.Star):
    """弥亚视觉工具插件"""

    def __init__(self, context: star.Context, config: dict = None) -> None:
        super().__init__(context)
        self.context = context

        plug_dir = os.path.dirname(os.path.abspath(__file__))

        try:
            data_dir = StarTools.get_data_dir()
            if not data_dir:
                raise ValueError("get_data_dir() returned falsy")
            config_path = os.path.join(str(data_dir), "config.json")
            db_path = os.path.join(str(data_dir), "vision_cache.db")
        except Exception:
            data_dir = plug_dir
            config_path = os.path.join(plug_dir, "config.json")
            db_path = os.path.join(plug_dir, "vision_cache.db")

        _config = _DEFAULT_CONFIG.copy()
        if os.path.exists(config_path):
            import json

            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    _config = json.load(f)
            except Exception:
                logger.warning("配置文件读取失败，使用默认配置")
        else:
            import copy, json

            _config = copy.deepcopy(_DEFAULT_CONFIG)
            try:
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(_config, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        # WebUI 配置优先
        if config:
            sectioned = {}
            for section_name in ("VL 模型配置",):
                value = config.get(section_name, {})
                if isinstance(value, dict):
                    sectioned.update(value)
            web_config = {**config, **sectioned}
            vl_model = web_config.get("vl_model")
            if isinstance(vl_model, dict):
                _config["vl_model"] = vl_model

        _tool_config.set_config(_config, plug_dir)

        tools = register_tools(db_path)
        context.add_llm_tools(*tools)
        logger.info(f"irmia_vision ready — {len(tools)} tools registered")
