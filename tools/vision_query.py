"""
vision_query — 查询已读图结果
"""

from __future__ import annotations

import json

from ._store import VisionStore
from ._helpers import proposal_reply


def _clean_result(row: dict, include_text: bool = False) -> dict:
    tags_raw = row.get("tags", "[]")
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []

    item = {
        "result_id": row.get("result_id", ""),
        "filename": row.get("filename", ""),
        "path": row.get("source_value", ""),
        "summary": row.get("summary", ""),
        "tags": tags,
        "read_at": row.get("read_at", ""),
        "hit_count": row.get("hit_count", 0),
    }

    if include_text:
        text = row.get("text", "")
        item["text"] = (text[:2000] + "..." if len(text) > 2000 else text)

    return item


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
        row = db.get_by_result_id(result_id)
        results = [row] if row else []
        include_text = True
    elif filename:
        results = db.get_by_filename(filename, limit=max_limit, offset=max_offset)
        include_text = False
    elif path:
        results = db.get_by_path(path, limit=max_limit, offset=max_offset)
        include_text = False
    elif query:
        results = db.search(query, limit=max_limit, offset=max_offset)
        include_text = False
    elif recent > 0:
        results = db.get_recent(limit=min(max(0, recent), max_limit), offset=max_offset)
        include_text = False
    else:
        results = []
        include_text = False

    cleaned = [_clean_result(r, include_text=include_text) for r in results if r]

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

    proposal_text = "查询完成，共 {} 条结果。".format(len(cleaned))
    if not include_text:
        proposal_text += " 列表中只包含摘要；如需查看某条完整描述，请用 result_id 精确查询。"
    else:
        proposal_text += " 这是单条完整结果。"

    return {
        "ok": True,
        "total": len(cleaned),
        "results": cleaned,
        "proposal": proposal_text,
        "next_call": {
            "tool": "vision_query",
            "arguments": next_args,
        },
    }
