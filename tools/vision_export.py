"""
vision_export — 批量导出已读图结果
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from ._store import VisionStore
from ._helpers import proposal_reply


async def export(
    db: VisionStore,
    *,
    query: str = "",
    filename: str = "",
    path: str = "",
    recent: int = 0,
    limit: int = 1000,
    offset: int = 0,
    output_path: str = "",
    fmt: str = "json",
) -> dict:
    """导出符合条件的图片结果到 JSON 或 CSV 文件。"""

    fmt = fmt.lower()
    if fmt not in ("json", "csv"):
        return proposal_reply(
            False,
            "不支持的导出格式，请使用 'json' 或 'csv'。",
            options=["使用 json 格式", "使用 csv 格式"],
        )

    max_limit = max(1, min(limit, 10000))
    max_offset = max(0, offset)

    if recent > 0:
        rows = db.get_recent(limit=min(recent, max_limit), offset=max_offset)
    elif query:
        rows = db.search(query, limit=max_limit, offset=max_offset)
    elif filename:
        rows = db.get_by_filename(filename, limit=max_limit, offset=max_offset)
    elif path:
        rows = db.get_by_path(path, limit=max_limit, offset=max_offset)
    else:
        rows = db.get_recent(limit=max_limit, offset=max_offset)

    records = []
    for r in rows:
        if not r:
            continue
        tags_raw = r.get("tags", "[]")
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
        records.append(
            {
                "result_id": r.get("result_id", ""),
                "filename": r.get("filename", ""),
                "path": r.get("source_value", ""),
                "sha256": r.get("sha256", ""),
                "summary": r.get("summary", ""),
                "text": r.get("text", ""),
                "tags": tags,
                "model_id": r.get("model_id", ""),
                "question": r.get("question", ""),
                "read_at": r.get("read_at", ""),
                "hit_count": r.get("hit_count", 0),
            }
        )

    if not records:
        return proposal_reply(
            False,
            "未找到可导出的结果。请尝试扩大查询范围或先调用 vision_read 读图。",
            options=["换关键词查询", "读取更多图片"],
        )

    if output_path:
        target_path = os.path.expanduser(output_path)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_path = os.path.abspath(f"vision_export_{timestamp}.{fmt}")

    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)

    try:
        if fmt == "json":
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        else:  # csv
            with open(target_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=records[0].keys())
                writer.writeheader()
                for rec in records:
                    writer.writerow(rec)
    except Exception as e:
        return proposal_reply(
            False,
            f"导出失败: {e}",
            error=str(e),
        )

    return {
        "ok": True,
        "total": len(records),
        "format": fmt,
        "output_path": target_path,
        "proposal": f"已导出 {len(records)} 条结果到 {target_path}。你可以直接读取该文件，或写脚本批量处理。",
    }
