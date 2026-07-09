"""
测试 vision_query 查询结果
"""

import os
import tempfile

from tools._cache import VisionCache
from tools import vision_query


def test_vision_query_by_result_id():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = VisionCache(db_path)
    try:
        db.insert(
            sha256="sha1",
            filename="a.png",
            phash="",
            model_id="gpt-4o",
            question="",
            result_id="res_q",
            source_value="/tmp/a.png",
            summary="summary",
            text="text content",
            tags=["tag1"],
            result_json={},
        )

        result = vision_query.query(db, result_id="res_q")
        assert result["ok"] is True
        assert result["total"] == 1
        assert result["results"][0]["result_id"] == "res_q"
        assert result["results"][0]["tags"] == ["tag1"]
    finally:
        db.close()
        os.unlink(db_path)


def test_vision_query_empty():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = VisionCache(db_path)
    try:
        result = vision_query.query(db, query="不存在")
        assert result["ok"] is False
        assert "proposal" in result
    finally:
        db.close()
        os.unlink(db_path)


def test_vision_query_pagination():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = VisionCache(db_path)
    try:
        for i in range(3):
            db.insert(
                sha256=f"sha{i}",
                filename=f"img{i}.png",
                phash="",
                model_id="gpt-4o",
                question="",
                result_id=f"res_{i}",
                source_value=f"/tmp/img{i}.png",
                summary=f"summary {i}",
                text="text",
                tags=[],
                result_json={},
            )

        result = vision_query.query(db, recent=10, limit=2, offset=0)
        assert result["ok"] is True
        assert result["total"] == 2
        assert result["next_call"]["arguments"]["offset"] == 2
        assert result["next_call"]["tool"] == "vision_query"
    finally:
        db.close()
        os.unlink(db_path)
