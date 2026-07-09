"""
vision_export — 批量导出已读图结果到文件
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from ._helpers import proposal_reply
from ._store import VisionStore


SUPPORTED_FORMATS = {"json", "csv"}


def _default_output_path(plugin_dir: str, fmt: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return os.path.join(plugin_dir, "exports", f"vision_export_{timestamp}.{fmt}")


def _to_json_rows(rows: list[dict]) -> list[dict]:
    cleaned = []
    for r in rows:
        tags_raw = r.get("tags", "[]")
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
        cleaned.append(
            {
                "result_id": r.get("result_id", ""),
                "sha256": r.get("sha256", ""),
                "filename": r.get("filename", ""),
                "path": r.get("source_value", ""),
                "summary": r.get("summary", ""),
                "text": r.get("text", ""),
                "tags": tags,
                "model_id": r.get("model_id", ""),
                "question": r.get("question", ""),
                "read_at": r.get("read_at", ""),
                "hit_count": r.get("hit_count", 0),
            }
        )
    return cleaned


def _write_json(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("")
        return

    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _fetch_rows(
    db: VisionStore,
    *,
    query: str = "",
    filename: str = "",
    path: str = "",
    recent: int = 0,
    limit: int = 0,
    offset: int = 0,
) -> list[dict]:
    if filename:
        return db.get_by_filename(filename, limit=limit or 10000, offset=offset)
    if path:
        return db.get_by_path(path, limit=limit or 10000, offset=offset)
    if query:
        return db.search(query, limit=limit or 10000, offset=offset)
    if recent > 0:
        return db.get_recent(limit=limit or recent, offset=offset)
    return db.export_all(limit=limit, offset=offset)


async def export(
    db: VisionStore,
    output_path: str = "",
    fmt: str = "json",
    query: str = "",
    filename: str = "",
    path: str = "",
    recent: int = 0,
    limit: int = 1000,
    offset: int = 0,
) -> dict:
    from .config import get_plugin_dir

    fmt = fmt.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        return proposal_reply(
            False,
            f"不支持的导出格式：{fmt}。请选择 json 或 csv。",
            options=["导出为 json", "导出为 csv"],
        )

    if not output_path:
        output_path = _default_output_path(get_plugin_dir(), fmt)

    output_path = os.path.expanduser(output_path)
    dirname = os.path.dirname(output_path)
    if dirname:
        try:
            os.makedirs(dirname, exist_ok=True)
        except Exception as e:
            return proposal_reply(
                False,
                f"无法创建输出目录：{e}",
                options=["检查路径权限", "换一个输出路径"],
            )

    max_limit = max(0, min(limit, 10000))
    max_offset = max(0, offset)
    rows = _fetch_rows(
        db,
        query=query,
        filename=filename,
        path=path,
        recent=recent,
        limit=max_limit,
        offset=max_offset,
    )
    cleaned = _to_json_rows(rows)

    try:
        if fmt == "json":
            _write_json(output_path, cleaned)
        else:
            _write_csv(output_path, cleaned)
    except Exception as e:
        return proposal_reply(
            False,
            f"导出失败：{e}",
            options=["检查路径权限", "换输出格式"],
        )

    return {
        "ok": True,
        "exported": len(cleaned),
        "output_path": output_path,
        "format": fmt,
        "proposal": f"导出完成，共 {len(cleaned)} 条结果，文件保存在 {output_path}。",
    }
