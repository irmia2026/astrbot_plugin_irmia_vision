"""
测试 vision_query 查询结果
"""

import asyncio
import os
import tempfile

from tools._store import create_store
from tools import vision_query


async def _run_query_by_result_id(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    try:
        db.insert(
            sha256="sha1",
            filename="a.png",
            phash="",
            model_id="gpt-4o",
            question="",
            result_id="res_q",
            source_value="/tmp/a.png",
            peek="peek-summary",
            text="text content",
            tags=["tag1"],
            result_json={},
        )

        result = await vision_query.query(db, result_id="res_q")
        return result
    finally:
        db.close()


def test_vision_query_by_result_id(tmp_path):
    result = asyncio.run(_run_query_by_result_id(tmp_path))
    assert result["ok"] is True
    assert result["mode"] == "full"
    assert result["total"] == 1
    assert result["results"][0]["result_id"] == "res_q"
    assert "text" in result["results"][0]
    assert "path" in result["results"][0]
    assert "tags" in result["results"][0]


async def _run_query_list(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    try:
        db.insert(
            sha256="sha1",
            filename="invoice.png",
            phash="",
            model_id="gpt-4o",
            question="",
            result_id="res_invoice",
            source_value="/tmp/invoice.png",
            peek="invoice preview",
            text="invoice text content",
            tags=["invoice"],
            result_json={},
        )

        result = await vision_query.query(db, filename="invoice.png")
        return result
    finally:
        db.close()


def test_vision_query_list_returns_peek_only(tmp_path):
    result = asyncio.run(_run_query_list(tmp_path))
    assert result["ok"] is True
    assert result["mode"] == "list"
    assert result["total"] == 1
    item = result["results"][0]
    assert set(item.keys()) == {"result_id", "filename", "peek", "question"}


async def _run_query_empty(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    try:
        result = await vision_query.query(db, query="不存在")
        return result
    finally:
        db.close()


def test_vision_query_empty(tmp_path):
    result = asyncio.run(_run_query_empty(tmp_path))
    assert result["ok"] is False
    assert "proposal" in result


async def _run_query_pagination(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
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
                peek=f"preview {i}",
                text="text",
                tags=[],
                result_json={},
            )

        result = await vision_query.query(db, recent=10, limit=2, offset=0)
        return result
    finally:
        db.close()


def test_vision_query_pagination(tmp_path):
    result = asyncio.run(_run_query_pagination(tmp_path))
    assert result["ok"] is True
    assert result["total"] == 2
    assert result["next_call"]["arguments"]["offset"] == 2
    assert result["next_call"]["tool"] == "vision_query"
