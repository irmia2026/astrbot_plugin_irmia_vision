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
    "vl_provider_1": "",
    "vl_provider_2": "",
    "vl_provider_3": "",
    "vl_provider_ids": "",
    "vl_model": {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
        "timeout": 120.0,
        "concurrency": 50,
        "max_retries": 2,
        "detail": "auto",
    },
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
            import copy
            import json

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
            # 三个下拉框合并为降级链
            p1 = str(web_config.get("vl_provider_1", "") or "").strip()
            p2 = str(web_config.get("vl_provider_2", "") or "").strip()
            p3 = str(web_config.get("vl_provider_3", "") or "").strip()
            if p1 or p2 or p3:
                _config["vl_provider_1"] = p1
                _config["vl_provider_2"] = p2
                _config["vl_provider_3"] = p3
                # 合并为 vl_provider_ids 供 resolve_provider_chain 使用
                merged = ",".join(x for x in [p1, p2, p3] if x)
                _config["vl_provider_ids"] = merged
            else:
                # 回退到手动填写的 vl_provider_ids
                vl_provider_ids = web_config.get("vl_provider_ids", "")
                if vl_provider_ids:
                    _config["vl_provider_ids"] = vl_provider_ids

        # 从 AstrBot context 读取已保存的模型提供商列表
        providers = []
        try:
            for prov in context.get_all_providers():
                pc = getattr(prov, "provider_config", None)
                if isinstance(pc, dict) and pc.get("id"):
                    providers.append(pc)
        except Exception as e:
            logger.warning(f"读取 AstrBot provider 列表失败: {e}")

        # 兜底：插件加载早于 provider 初始化（get_all_providers 返回空）时，
        # 从磁盘 cmd_config.json 读取 provider_sources + provider 合并构建，
        # 保证 vl_provider_ids 能解析出带 api_key 的配置。
        if not providers:
            try:
                import json as _json

                cfg_path = os.path.abspath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cmd_config.json")
                )
                if os.path.exists(cfg_path):
                    # cmd_config.json 可能带 UTF-8 BOM，用 utf-8-sig 兼容
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        disk_cfg = _json.load(f)
                    sources = {
                        ps.get("id"): ps
                        for ps in disk_cfg.get("provider_sources", [])
                        if ps.get("id")
                    }
                    for pc in disk_cfg.get("provider", []):
                        if not pc.get("id") or not pc.get("enable", True):
                            continue
                        if pc.get("type") is None and not pc.get("provider_source_id"):
                            continue  # 跳过无法解析的占位记录
                        merged = dict(pc)
                        psid = pc.get("provider_source_id", "")
                        if psid and psid in sources:
                            merged = {**sources[psid], **pc}
                            merged["id"] = pc["id"]
                        providers.append(merged)
                    logger.info(
                        f"irmia_vision: 插件加载早于 provider 初始化，从磁盘配置兜底读取 {len(providers)} 个 provider"
                    )
            except Exception as e:
                logger.warning(f"irmia_vision: 磁盘配置兜底失败: {e}")
        _tool_config.set_providers(providers)

        # 将可用模型 ID 写入 _conf_schema.json 的 options，供 WebUI 下拉选择
        if providers:
            provider_ids = [p["id"] for p in providers]
            provider_labels = [
                f"{p['id']} ({p.get('model', '?')})" for p in providers
            ]
            self._update_schema_options(plug_dir, provider_ids, provider_labels)
            logger.info(f"irmia_vision: 发现 {len(providers)} 个已保存模型: {', '.join(provider_ids)}")

        _tool_config.set_config(_config, plug_dir)

        tools = register_tools(db_path)
        context.add_llm_tools(*tools)
        logger.info(f"irmia_vision ready — {len(tools)} tools registered")

    @staticmethod
    def _update_schema_options(plug_dir: str, ids: list[str], labels: list[str]) -> None:
        """将可用模型 ID 写入 _conf_schema.json 的三个下拉框字段，
        使 WebUI 下次刷新时显示下拉选择框。"""
        import json

        schema_path = os.path.join(plug_dir, "_conf_schema.json")
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            items = schema.get("VL 模型配置", {}).get("items", {})
            changed = False
            for field_name in ("vl_provider_1", "vl_provider_2", "vl_provider_3"):
                field = items.get(field_name)
                if field is not None:
                    field["options"] = ids
                    field["labels"] = labels
                    changed = True
            if changed:
                with open(schema_path, "w", encoding="utf-8") as f:
                    json.dump(schema, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 写入失败不影响功能
