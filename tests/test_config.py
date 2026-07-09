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
