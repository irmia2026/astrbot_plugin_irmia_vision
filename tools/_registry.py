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
            description="当你需要理解图片内容时调用：读取图片或文件夹中的所有图片，调用用户配置的 VL 模型理解内容，并将结果存入本地数据库。支持单文件、多文件、文件夹路径。读图完成后不会返回每张图的详细内容，需要继续调用 vision_query 查询。",
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
            description="在 vision_read 之后，当你需要查看具体图片结果时调用：从本地数据库查询已读过的图片结果。除 result_id 精确查询外，其他查询只返回摘要；想看完整描述时，请用 result_id 精确查询。",
            parameters={
                "type": "object",
                "properties": {
                    "result_id": {
                        "type": "string",
                        "description": "通过 result_id 精确查询单条结果，会返回包含完整描述的 text 字段。",
                    },
                    "filename": {
                        "type": "string",
                        "description": "按文件名查询。只返回摘要，不返回完整描述。",
                    },
                    "path": {
                        "type": "string",
                        "description": "按文件路径查询，支持路径前缀或包含字符串。只返回摘要，不返回完整描述。",
                    },
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索关键词，会在摘要、文字、标签、文件名、路径中模糊搜索。只返回摘要，不返回完整描述。",
                    },
                    "recent": {
                        "type": "integer",
                        "description": "查询最近读取的 N 条结果。只返回摘要，不返回完整描述。",
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
    ]

    return [tool_cls() for tool_cls in tool_classes]
