import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock astrbot 依赖，使独立测试可以导入 tools 模块
if "astrbot" not in sys.modules:
    astrbot_pkg = types.ModuleType("astrbot")
    sys.modules["astrbot"] = astrbot_pkg

if "astrbot.api" not in sys.modules:
    api_pkg = types.ModuleType("astrbot.api")
    sys.modules["astrbot.api"] = api_pkg

    class _FakeLogger:
        def debug(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    api_pkg.logger = _FakeLogger()

    # 部分模块还会引用 star 装饰器，这里也做 mock
    star_mod = types.ModuleType("astrbot.api.star")
    sys.modules["astrbot.api.star"] = star_mod

    class _Star:
        pass

    class _Context:
        pass

    star_mod.Star = _Star
    star_mod.Context = _Context
    star_mod.star = _Star

    if not hasattr(api_pkg, "star"):
        api_pkg.star = star_mod

    if not hasattr(api_pkg, "StarTools"):
        class _StarTools:
            @staticmethod
            def get_data_dir():
                return "."

        api_pkg.StarTools = _StarTools
        sys.modules["astrbot.api.star"].StarTools = _StarTools

    if not hasattr(api_pkg, "FunctionTool"):
        class _FunctionTool:
            pass

        api_pkg.FunctionTool = _FunctionTool


@pytest.fixture(autouse=True)
def _reset_tool_config_globals():
    """每个测试后重置插件全局配置，防止 set_config/set_providers 跨测试泄漏
    （如 test_max_batch_guard 把 max_batch=3 写全局）。机制保障，不靠纪律维持。"""
    yield
    from tools import config as tool_config

    tool_config.set_config({}, "")
    tool_config.set_providers([])
