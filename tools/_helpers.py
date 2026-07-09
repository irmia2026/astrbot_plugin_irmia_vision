"""
通用工具函数
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any


def unwrap(result: Any) -> str:
    """序列化工具返回。若包含 proposal/options/next_call 等诊断字段则直接透传。"""
    if not isinstance(result, dict):
        return err_json(f"工具返回了非预期类型: {type(result).__name__}")
    if any(k in result for k in ("proposal", "options", "next_call")):
        return json.dumps(result, ensure_ascii=False)
    if result.get("ok") is False:
        return err_json(result.get("error", "未知错误"))
    return json.dumps({"ok": True, "data": result}, ensure_ascii=False)


def err_json(error: str) -> str:
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def proposal_reply(
    ok: bool,
    proposal: str,
    *,
    error: str = "",
    options: list[str] | None = None,
    next_call: dict | None = None,
    **extra,
) -> dict:
    """构建提案式返回，用于引导 LLM 下一步。"""
    result = {"ok": ok, "proposal": proposal, **extra}
    if error:
        result["error"] = error
    if options:
        result["options"] = options
    if next_call:
        result["next_call"] = next_call
    return result


async def run_sync(fn, *args, **kwargs):
    """在事件循环中运行同步函数。"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
