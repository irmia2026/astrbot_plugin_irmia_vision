"""
vision_query — 查询已读图结果
"""

from __future__ import annotations

import json

from ._store import VisionStore
from ._helpers import proposal_reply, run_sync


async def query(
    db: VisionStore,
    query: str = "",
    result_id: str = "",
    filename: str = "",
    path: str = "",
    recent: int = 0,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    max_limit = max(0, min(limit, 100))
    max_offset = max(0, offset)

    if result_id:
        row = await run_sync(db.get_by_result_id, result_id)
        results = [row] if row else []
        mode = "full"
    elif filename:
        results = await run_sync(db.get_by_filename, filename, limit=max_limit, offset=max_offset)
        mode = "peek"
    elif path:
        results = await run_sync(db.get_by_path, path, limit=max_limit, offset=max_offset)
        mode = "peek"
    elif query:
        results = await run_sync(db.search, query, limit=max_limit, offset=max_offset)
        mode = "peek"
    elif recent > 0:
        results = await run_sync(db.get_recent, limit=min(max(0, recent), max_limit), offset=max_offset)
        mode = "peek"
    else:
        results = []
        mode = "peek"

    cleaned = []
    for r in results:
        if not r:
            continue
        item = {
            "result_id": r.get("result_id", ""),
            "filename": r.get("filename", ""),
            "summary": r.get("summary", ""),
            # 同一张图可能有多条不同 question 的记录，透出 question 让 LLM 能区分
            "question": r.get("question", ""),
        }
        if mode == "full":
            tags_raw = r.get("tags", "[]")
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
            except Exception:
                tags = []
            text = r.get("text", "")
            item.update({
                "path": r.get("source_value", ""),
                "text": (text[:2000] + "..." if len(text) > 2000 else text),
                "tags": tags,
                "read_at": r.get("read_at", ""),
                "hit_count": r.get("hit_count", 0),
            })
        cleaned.append(item)

    if not cleaned:
        return proposal_reply(
            False,
            "未找到匹配结果。请尝试扩大搜索范围、按路径查询或查看最近结果。",
            options=["换关键词搜索", "按路径查询", "查看最近 10 条"],
        )

    # 根据当前查询类型，构造保留条件的 next_call
    next_args: dict = {"offset": max_offset + max_limit, "limit": max_limit}
    if query:
        next_args["query"] = query
    elif filename:
        next_args["filename"] = filename
    elif path:
        next_args["path"] = path
    elif recent > 0:
        next_args["recent"] = recent

    if mode == "full":
        proposal_text = "查询完成，返回单条完整结果。"
    else:
        proposal_text = (
            f"查询完成，共 {len(cleaned)} 条摘要结果。"
            " 如需查看某条完整信息（包括 path 和完整描述），请用 result_id 精确查询。"
        )

    return {
        "ok": True,
        "total": len(cleaned),
        "mode": mode,
        "results": cleaned,
        "proposal": proposal_text,
        "next_call": {
            "tool": "vision_query",
            "arguments": next_args,
        },
    }
