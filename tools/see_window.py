"""
see_window 工具：快速查看电脑屏幕或指定窗口

职责边界（充分复用 vision_read 管线）：
- 本模块只负责：窗口枚举/匹配、截图、保存临时 PNG。
- 读图 / VL 模型调用 / 降级链 / 缓存 / 落库 / result_id 全部复用 vision_read.read。

仅支持 Windows（win32gui 窗口枚举 + PIL.ImageGrab 截图）；非 Windows 返回明确错误。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from . import vision_read
from ._helpers import proposal_reply

DEFAULT_SCREEN_PROMPT = """这是一张电脑屏幕截图（可能是指定窗口的截图）。请以「帮忙搞清楚用户此刻在干什么」为目标，仔细分析屏幕内容并回答：
1. 屏幕上/窗口里主要显示的是什么程序、界面或内容？
2. 用户正在进行的操作或任务是什么？界面处于什么状态（前台窗口、正在输入、运行中、报错、等待等）？
3. 提取界面上的关键信息：文字、数据、代码、日志、报错信息、菜单、按钮、进度等。
4. 如果看到终端/IDE/浏览器/文档/聊天窗口，请概述其中可见的关键内容。
用中文回答，条理清晰，直接陈述观察到的内容，不要推测屏幕之外的情况。"""

# 常见的系统/桌面窗口标题，匹配时一律跳过，避免误选
_SYSTEM_WINDOW_TITLES = {
    "",
    "program manager",
    "default ime",
    "msctfime ui",
    "application frame host",
    "windows shell experience host",
    "search",
    "设置",
    "任务视图",
    "任务栏",
}

# 常用缩写/别名 → 展开为窗口标题常见写法（子串匹配前先展开）
_WINDOW_ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "qq": "腾讯qq",
    "wechat": "微信",
}


def _is_system_window(title: str) -> bool:
    """判断窗口标题是否为系统窗口（纯逻辑，可单测）。"""
    t = (title or "").lower().strip()
    return t in _SYSTEM_WINDOW_TITLES


def _pick_window(candidates: list[tuple[int, str]], keyword: str | None) -> int | None:
    """从候选窗口（按 z-order 排序）中匹配窗口标题关键词，返回 hwnd。

    - keyword 为空 → None（表示截全屏）。
    - 优先精确匹配，其次包含匹配；系统窗口一律跳过。
    - 多个匹配时取 z-order 最前（列表最靠前的非系统窗口）。
    （纯逻辑，可单测；candidates 为 [(hwnd, title), ...]）
    """
    if keyword is None:
        return None
    kw = str(keyword).strip().lower()
    if not kw:
        return None
    # 别名展开：原词 + 展开值都参与匹配（"qq" 既要能命中标题 "QQ"，也要能命中 "腾讯QQ"）
    kws = [kw]
    expanded = _WINDOW_ALIASES.get(kw)
    if expanded:
        kws.append(expanded)
    for hwnd, title in candidates:
        if _is_system_window(title):
            continue
        if title.lower().strip() in kws:
            return hwnd
    for hwnd, title in candidates:
        if _is_system_window(title):
            continue
        t = title.lower()
        if any(k in t for k in kws):
            return hwnd
    return None


def _enable_dpi_awareness() -> None:
    """让进程感知 DPI，保证 GetWindowRect 与截图坐标一致（否则窗口截图会偏移）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _list_visible_windows() -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """枚举可见顶层窗口，返回 [(hwnd, title, (left, top, right, bottom)), ...]（z-order 从顶到底）。"""
    if sys.platform != "win32":
        return []
    import win32gui

    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    def _cb(hwnd: int, _) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w < 80 or h < 60:
            return  # 过滤极小窗口（托盘图标、角标等）
        found.append((hwnd, title, tuple(rect)))

    win32gui.EnumWindows(_cb, None)
    return found


def _grab_to_file(bbox: tuple[int, int, int, int] | None, save_dir: str) -> str:
    """截图（全屏或 bbox 区域）保存为 PNG，返回文件路径。"""
    from PIL import ImageGrab

    os.makedirs(save_dir, exist_ok=True)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(save_dir, f"see_window_{ts}.png")
    img.save(path, "PNG")
    return path


async def see_window(
    db,
    window: str = "",
    question: str = "",
    force_reread: bool = False,
    previous_result_id: str = "",
) -> dict:
    """截取整个屏幕或指定窗口的画面，复用 vision_read 管线分析并落库。

    Args:
        window: 窗口标题关键词（精确/包含匹配），留空=截全屏。
        question: 自定义分析问题，留空使用默认「干活向」提示词。
        force_reread: 忽略缓存强制重新读图。
        previous_result_id: 追问模式，基于上一次读图结果追问。
    """
    if sys.platform != "win32":
        return proposal_reply(
            False,
            f"see_window 仅支持 Windows 平台（当前: {sys.platform}）",
            options=["使用 vision_read 读取已有图片"],
        )

    _enable_dpi_awareness()

    plug_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_dir = os.path.join(plug_dir, "data", "temp", "tool_images")

    bbox = None
    window_label = "整个屏幕"
    if window and str(window).strip():
        windows = _list_visible_windows()
        candidates = [(hwnd, title) for hwnd, title, _ in windows]
        hwnd = _pick_window(candidates, str(window))
        if hwnd is None:
            titles = [t for _, t, _ in windows][:15]
            hint = "；".join(titles) if titles else "（没有可见窗口）"
            return proposal_reply(
                False,
                f"找不到标题包含「{window}」的窗口。当前可见窗口：{hint}",
                options=["不带 window 参数截全屏", "换个窗口标题关键词"],
            )
        for h, title, rect in windows:
            if h == hwnd:
                bbox = rect
                window_label = title
                break

    try:
        shot_path = _grab_to_file(bbox=bbox, save_dir=save_dir)
    except Exception as e:
        return proposal_reply(
            False,
            f"截图失败: {e}",
            options=["检查是否有桌面会话权限", "使用 vision_read 读取已有图片"],
        )

    prompt = question if str(question).strip() else DEFAULT_SCREEN_PROMPT
    result = await vision_read.read(
        db,
        paths=[shot_path],
        question=prompt,
        force_reread=force_reread,
        previous_result_id=previous_result_id,
        # 屏幕内容时刻在变：同一窗口代码滚动后 phash 距离仅 0-4，
        # 近似命中会返回过期屏幕描述，see_window 禁用 phash 兜底（sha256 精确命中保留）
        allow_phash=False,
    )
    result["screenshot_path"] = shot_path
    result["window"] = window_label
    return result
