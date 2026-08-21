"""
测试 see_window 工具：屏幕/窗口截图分析
纯逻辑部分（系统窗口过滤、窗口匹配）不依赖真实显示环境，可直接单测。
"""

import pytest

from tools.see_window import (
    DEFAULT_SCREEN_PROMPT,
    _is_system_window,
    _pick_window,
)


class TestIsSystemWindow:
    """系统窗口过滤（纯逻辑）"""

    def test_desktop_program_manager(self):
        assert _is_system_window("Program Manager") is True

    def test_empty_title(self):
        assert _is_system_window("") is True
        assert _is_system_window("   ") is True

    def test_normal_window(self):
        assert _is_system_window("腾讯QQ") is False
        assert _is_system_window("Visual Studio Code") is False
        assert _is_system_window("弥亚庄园") is False

    def test_case_insensitive(self):
        assert _is_system_window("program manager") is True
        assert _is_system_window("PROGRAM MANAGER") is True


class TestPickWindow:
    """窗口匹配（纯逻辑：候选列表按 z-order 传入，keyword 为空返回 None=全屏）"""

    CANDIDATES = [
        (101, "腾讯QQ"),
        (102, "Visual Studio Code - main.py"),
        (103, "Program Manager"),  # 桌面，应被排除
        (104, "弥亚庄园 - 庄园通讯窗"),
    ]

    def test_empty_keyword_means_fullscreen(self):
        assert _pick_window(self.CANDIDATES, "") is None
        assert _pick_window(self.CANDIDATES, None) is None

    def test_exact_match(self):
        assert _pick_window(self.CANDIDATES, "腾讯QQ") == 101

    def test_exact_match_case_insensitive(self):
        assert _pick_window(self.CANDIDATES, "腾讯qq") == 101

    def test_contains_match(self):
        # "vs code" 应命中 "Visual Studio Code - main.py"
        assert _pick_window(self.CANDIDATES, "vs code") == 102

    def test_keyword_contained_in_system_window_is_skipped(self):
        # 关键词命中系统窗口时跳过，继续找下一个匹配
        assert _pick_window([(103, "Program Manager")], "program") is None

    def test_system_window_not_returned_even_if_exact(self):
        assert _pick_window([(103, "Program Manager")], "Program Manager") is None

    def test_no_match_returns_none(self):
        assert _pick_window(self.CANDIDATES, "不存在的窗口") is None

    def test_alias_qq_matches_bare_qq_title(self):
        # 真实场景：窗口标题就叫 "QQ"，别名 "qq" 也应命中
        assert _pick_window([(301, "QQ")], "qq") == 301

    def test_alias_qq_matches_tencent_title(self):
        assert _pick_window([(302, "腾讯QQ")], "qq") == 302

    def test_alias_vscode_matches_full_title(self):
        assert _pick_window([(303, "Visual Studio Code - main.py")], "vs code") == 303

    def test_first_z_order_wins_on_multiple_contains(self):
        # 多个窗口包含同一关键词 → 取 z-order 最前（列表第一个非系统窗口）
        cands = [(201, "Chrome - 弥亚庄园"), (202, "弥亚庄园 - 通讯窗")]
        assert _pick_window(cands, "弥亚庄园") == 201

    def test_whitespace_keyword(self):
        assert _pick_window(self.CANDIDATES, "   ") is None


class TestDefaultPrompt:
    """默认读图提示词应偏向"干活"：搞清楚用户在干什么"""

    def test_prompt_mentions_user_activity(self):
        assert "在干什么" in DEFAULT_SCREEN_PROMPT or "正在" in DEFAULT_SCREEN_PROMPT

    def test_prompt_mentions_screen_content(self):
        assert "屏幕" in DEFAULT_SCREEN_PROMPT

    def test_prompt_asks_for_key_info(self):
        # 提示词应引导提取关键信息（代码/报错/数据等）
        assert any(k in DEFAULT_SCREEN_PROMPT for k in ["关键", "代码", "报错", "信息"])

    def test_prompt_is_chinese(self):
        assert "中文" in DEFAULT_SCREEN_PROMPT or "用中文" in DEFAULT_SCREEN_PROMPT
