"""
测试配置管理
"""

import tempfile

from tools import config as tool_config


def test_config_set_and_get():
    cfg = {
        "vl_model": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o",
            "concurrency": 8,
        }
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config.set_config(cfg, tmpdir)
        assert tool_config.get_config() == cfg
        assert tool_config.get_plugin_dir() == tmpdir
        assert tool_config.get_vl_model_config()["api_key"] == "sk-test"
        assert tool_config.get_vl_model_config().get("concurrency") == 8


def test_resolve_provider_chain_fallback_to_vl_model():
    """vl_provider_ids 为空时回退到 vl_model 单条配置。"""
    cfg = {
        "vl_provider_ids": "",
        "vl_model": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-fallback",
            "model": "gpt-4o-mini",
        },
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config.set_config(cfg, tmpdir)
        tool_config.set_providers([])
        chain = tool_config.resolve_provider_chain()
        assert len(chain) == 1
        assert chain[0]["api_key"] == "sk-fallback"
        assert chain[0]["model"] == "gpt-4o-mini"


def test_resolve_provider_chain_with_provider_ids():
    """vl_provider_ids 指定多个 provider 时按顺序解析降级链。"""
    providers = [
        {"id": "prov-a", "type": "openai_chat_completion", "api_base": "https://a.example.com/v1", "key": ["sk-a"], "model": "model-a", "timeout": 60},
        {"id": "prov-b", "type": "openai_chat_completion", "api_base": "https://b.example.com/v1", "key": ["sk-b"], "model": "model-b", "timeout": 90},
        {"id": "prov-c", "type": "openai_chat_completion", "api_base": "https://c.example.com/v1", "key": ["sk-c"], "model": "model-c", "timeout": 120},
    ]
    cfg = {
        "vl_provider_ids": "prov-a, prov-c",
        "vl_model": {"api_key": "sk-fallback", "model": "fallback"},
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config.set_config(cfg, tmpdir)
        tool_config.set_providers(providers)
        chain = tool_config.resolve_provider_chain()
        assert len(chain) == 2
        assert chain[0]["model"] == "model-a"
        assert chain[0]["api_key"] == "sk-a"
        assert chain[0]["base_url"] == "https://a.example.com/v1"
        assert chain[1]["model"] == "model-c"
        assert chain[1]["api_key"] == "sk-c"


def test_resolve_provider_chain_unknown_id_skipped():
    """vl_provider_ids 中包含不存在的 ID 时跳过，只用存在的。"""
    providers = [
        {"id": "prov-a", "type": "openai_chat_completion", "api_base": "https://a.example.com/v1", "key": ["sk-a"], "model": "model-a"},
    ]
    cfg = {
        "vl_provider_ids": "prov-x, prov-a, prov-y",
        "vl_model": {"api_key": "sk-fallback", "model": "fallback"},
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config.set_config(cfg, tmpdir)
        tool_config.set_providers(providers)
        chain = tool_config.resolve_provider_chain()
        assert len(chain) == 1
        assert chain[0]["model"] == "model-a"


def test_resolve_provider_chain_empty_providers_fallback():
    """provider 列表为空但 vl_provider_ids 有值时回退到 vl_model。"""
    cfg = {
        "vl_provider_ids": "prov-a",
        "vl_model": {"api_key": "sk-fallback", "model": "fallback-model"},
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config.set_config(cfg, tmpdir)
        tool_config.set_providers([])
        chain = tool_config.resolve_provider_chain()
        assert len(chain) == 1
        assert chain[0]["model"] == "fallback-model"
