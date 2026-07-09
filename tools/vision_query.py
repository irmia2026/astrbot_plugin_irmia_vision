"""
vision_query — 查询已读图结果
"""

from __future__ import annotations

from ._cache import VisionCache
from ._helpers import proposal_reply


def query(
    db: VisionCache,
    query: str = "",
    result_id: str = "",
    filename: str = "",
    path: str = "",
    recent: int = 0,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    max_limit = min(limit, 100)
    max_offset = max(offset, 0)

    if result_id:
        row = db.get_by_result_id(result_id)
        results = [row] if row else []
    elif filename:
        results = db.get_by_filename(filename, limit=max_limit, offset=max_offset)
    elif path:
        results = db.get_by_path(path, limit=max_limit, offset=max_offset)
    elif query:
        results = db.search(query, limit=max_limit, offset=max_offset)
    elif recent > 0:
        results = db.get_recent(limit=min(recent, max_limit), offset=max_offset)
    else:
        results = []

    cleaned = []
    for r in results:
        if not r:
            continue
        cleaned.append(
            {
                "result_id": r.get("result_id", ""),
                "filename": r.get("filename", ""),
                "path": r.get("source_value", ""),
                "summary": r.get("summary", ""),
                "text": r.get("text", ""),
                "tags": r.get("tags", "[]"),
                "read_at": r.get("read_at", ""),
                "hit_count": r.get("hit_count", 0),
            }
        )

    if not cleaned:
        return proposal_reply(
            False,
            "未找到匹配结果。请尝试扩大搜索范围、按路径查询或查看最近结果。",
            options=["换关键词搜索", "按路径查询", "查看最近 10 条"],
        )

    return {
        "ok": True,
        "total": len(cleaned),
        "results": cleaned,
        "proposal": f"查询完成，共 {len(cleaned)} 条结果。如需继续查看，可调整 limit/offset 分页。",
        "next_call": {
            "tool": "vision_query",
            "arguments": {"offset": max_offset + max_limit, "limit": max_limit},
        },
    }
