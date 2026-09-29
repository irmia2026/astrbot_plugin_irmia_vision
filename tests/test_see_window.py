"""
测试 see_window 工具：屏幕/窗口截图分析
纯逻辑部分（系统窗口过滤、窗口匹配）不依赖真实显示环境，可直接单测。
"""

import os

import pytest

from tools.see_window import (
    DEFAULT_SCREEN_PROMPT,
    _cleanup_old_screenshots,
    _is_system_window,
    _pick_window,
)


class TestScreenshotCleanup:
    """截图临时目录滚动清理（磁盘泄漏防护）"""

    def test_keeps_only_latest_n(self, tmp_path):
        d = str(tmp_path)
        for i in range(55):
            # 文件名带时间戳，按名称排序即时间序
            open(os.path.join(d, f"see_window_2026092{i:02d}_000000_000000.png"), "w").close()
        open(os.path.join(d, "other_file.txt"), "w").close()  # 非截图文件不动

        _cleanup_old_screenshots(d, keep=50)

        remaining = os.listdir(d)
        shots = [f for f in remaining if f.startswith("see_window_")]
        assert len(shots) == 50
        assert "other_file.txt" in remaining
        # 删的是最旧的 5 张，最新的保留
        assert "see_window_202609254_000000_000000.png" in remaining
        assert "see_window_202609200_000000_000000.png" not in remaining

    def test_fewer_than_keep_is_noop(self, tmp_path):
        d = str(tmp_path)
        for i in range(3):
            open(os.path.join(d, f"see_window_2026092{i:02d}_000000_000000.png"), "w").close()
        _cleanup_old_screenshots(d, keep=50)
        assert len(os.listdir(d)) == 3

    def test_missing_dir_is_silent(self):
        _cleanup_old_screenshots("Z:/nonexistent_dir_irmia_xxx")  # 不拋异常

    def test_exactly_keep_is_noop(self, tmp_path):
        d = str(tmp_path)
        for i in range(50):
            open(os.path.join(d, f"see_window_2026092{i:02d}_000000_000000.png"), "w").close()
        _cleanup_old_screenshots(d, keep=50)
        assert len(os.listdir(d)) == 50

    def test_keep_zero_removes_all(self, tmp_path):
        d = str(tmp_path)
        for i in range(3):
            open(os.path.join(d, f"see_window_2026092{i:02d}_000000_000000.png"), "w").close()
        _cleanup_old_screenshots(d, keep=0)
        assert [f for f in os.listdir(d) if f.startswith("see_window_")] == []


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
