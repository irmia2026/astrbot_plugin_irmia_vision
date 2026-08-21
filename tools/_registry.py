"""
工具注册表
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from astrbot.api import FunctionTool as _AstrBotFunctionTool

from ._helpers import unwrap as _unwrap, proposal_reply
from . import tool_stats as _tool_stats
from ._store import create_store, VisionStore
from . import vision_read as _vision_read
from . import vision_query as _vision_query
from . import vision_export as _vision_export
from . import see_window as _see_window


@dataclass
class FunctionTool(_AstrBotFunctionTool):
    """AstrBot v4.16+ 兼容基类。"""


def make_tool(
    name: str,
    description: str,
    parameters: dict,
    fn: Callable[..., dict],
    db: VisionStore,
) -> type[FunctionTool]:
    """工厂函数：创建工具类。"""

    _T_dict = {
        "__annotations__": {"name": str, "description": str, "parameters": dict},
        "name": name,
        "description": description,
        "parameters": field(default_factory=lambda: parameters),
    }
    _T = type(name.title().replace("_", "") + "Tool", (FunctionTool,), _T_dict)
    _T = dataclass(_T)

    async def call(self, context, **kwargs):
        _tool_stats.record(self.name)
        try:
            result = await fn(db=db, **kwargs)
            return _unwrap(result)
        except Exception as e:
            return _unwrap(proposal_reply(False, "工具执行失败", error=str(e)))

    _T.call = call
    return _T


def register_tools(db_path: str) -> list[FunctionTool]:
    db = create_store(db_path)

    tool_classes = [
        make_tool(
            name="vision_read",
            description="当你需要理解图片内容时主动调用——包括但不限于：用户在对话中要求分析图片、你在 dir_list/es_search 等文件操作中发现了 .jpg/.png 等图片文件且猜测其内容可能对任务有帮助、浏览目录时遇到照片/截图/发票等任何需要视觉理解才能获取信息的场景。读取图片或文件夹中所有图片，调用 VL 模型理解内容并存入数据库。读图完成后不会返回详情，需继续调用 vision_query 获取结果。",
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "图片文件路径或文件夹路径列表，支持绝对路径、相对路径、~ 用户主目录。例如 [\"C:/Users/me/Pictures/invoice.png\", \"~/Pictures\"]",
                    },
                    "question": {
                        "type": "string",
                        "description": "可选，高级用法。默认情况下工具会自动使用专业的图片描述 prompt 读取图片，无需传此参数。如果你需要对图片追问特定问题，可以传入。同一个 question 会命中缓存，不同 question 会重新读图。",
                    },
                    "force_reread": {
                        "type": "boolean",
                        "description": "可选。强制重新读图，忽略缓存。用于想换角度或追问细节时。",
                        "default": False,
                    },
                    "previous_result_id": {
                        "type": "string",
                        "description": "可选。追问模式：传入之前读图结果的 result_id，VL 模型会基于之前的理解回答新问题。注意：仅对本次 paths 中与之前同一张图片（sha256 相同）生效；如果你传了多个不同的图片，只有那张相同的图会进入追问模式，其他图正常读取。",
                    },
                },
                "required": ["paths"],
            },
            fn=_vision_read.read,
            db=db,
        ),
        make_tool(
            name="vision_query",
            description="在 vision_read 之后，当你需要查看具体图片结果时调用：从本地数据库查询已读过的图片结果。除 result_id 精确查询外，其他查询只返回 result_id、filename、summary 的轻量摘要；想看完整描述和路径时，请用 result_id 精确查询。",
            parameters={
                "type": "object",
                "properties": {
                    "result_id": {
                        "type": "string",
                        "description": "通过 result_id 精确查询单条结果，返回 full 模式：包含 path、完整描述 text、tags 等。",
                    },
                    "filename": {
                        "type": "string",
                        "description": "按文件名查询。返回 peek 模式：只包含 result_id、filename、summary。",
                    },
                    "path": {
                        "type": "string",
                        "description": "按文件路径查询，支持路径前缀或包含字符串。返回 peek 模式：只包含 result_id、filename、summary。",
                    },
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索关键词，会在摘要、文字、标签、文件名、路径中模糊搜索。返回 peek 模式：只包含 result_id、filename、summary。",
                    },
                    "recent": {
                        "type": "integer",
                        "description": "查询最近读取的 N 条结果。返回 peek 模式：只包含 result_id、filename、summary。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20，最大 100。",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移量，默认 0。",
                        "default": 0,
                    },
                },
                "required": [],
            },
            fn=_vision_query.query,
            db=db,
        ),
        make_tool(
            name="vision_export",
            description="当你需要批量处理大量已读图结果时调用：将数据库中符合筛选条件的结果导出为本地 JSON 或 CSV 文件。导出后可以把文件路径交给 Python 脚本进行批量处理，避免把大量结果塞进上下文。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索条件，与 vision_query 一致。",
                    },
                    "filename": {
                        "type": "string",
                        "description": "按文件名筛选。",
                    },
                    "path": {
                        "type": "string",
                        "description": "按文件路径筛选。",
                    },
                    "recent": {
                        "type": "integer",
                        "description": "导出最近读取的 N 条结果。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多导出条数，默认 1000，最大 10000。",
                        "default": 1000,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移量，默认 0。",
                        "default": 0,
                    },
                    "output_path": {
                        "type": "string",
                        "description": "导出文件路径。默认保存到插件目录下的 exports/vision_export_时间戳.json。",
                    },
                    "fmt": {
                        "type": "string",
                        "description": "导出格式：json 或 csv。默认 json。",
                        "default": "json",
                    },
                },
                "required": [],
            },
            fn=_vision_export.export,
            db=db,
        ),
        make_tool(
            name="see_window",
            description="当你需要快速了解用户电脑屏幕上正在发生什么、用户在干什么时调用：截取整个屏幕或指定窗口的画面，用 VL 模型分析并落库。适合：用户求助时先看屏幕判断现场、检查程序运行状态或报错、了解用户当前操作。window 参数传窗口标题关键词（如 'Visual Studio Code'、'qq'），留空则截全屏；找不到窗口时会返回当前可见窗口列表。分析结果存库，可用 vision_query 查看。",
            parameters={
                "type": "object",
                "properties": {
                    "window": {
                        "type": "string",
                        "description": "可选。窗口标题关键词（精确或包含匹配，支持 vs code/qq/wechat 等常见缩写）。留空或省略=截取整个屏幕。",
                    },
                    "question": {
                        "type": "string",
                        "description": "可选。自定义分析问题。留空使用默认提示词（偏向判断用户在干什么、屏幕上的关键信息）。",
                    },
                    "force_reread": {
                        "type": "boolean",
                        "description": "可选。忽略缓存强制重新截图分析。",
                        "default": False,
                    },
                    "previous_result_id": {
                        "type": "string",
                        "description": "可选。追问模式：传入之前 see_window/vision_read 的 result_id，基于之前的理解回答新问题。",
                    },
                },
                "required": [],
            },
            fn=_see_window.see_window,
            db=db,
        ),
    ]

    return [tool_cls() for tool_cls in tool_classes]
