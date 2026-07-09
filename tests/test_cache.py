"""
测试缓存层
"""

import os
import tempfile

from tools._store import create_store, SQLiteVisionStore


def _make_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return create_store(db_path), db_path


def test_find_cached_and_insert():
    db, db_path = _make_db()
    try:
        cached = db.find_cached("sha1", "a.png", "gpt-4o", "")
        assert cached is None

        db.insert(
            sha256="sha1",
            filename="a.png",
            phash="phash1",
            model_id="gpt-4o",
            question="",
            result_id="res_001",
            source_value="/tmp/a.png",
            summary="summary",
            text="text",
            tags=["tag1"],
            result_json={"summary": "summary", "text": "text", "tags": ["tag1"]},
        )

        cached = db.find_cached("sha1", "a.png", "gpt-4o", "")
        assert cached is not None
        assert cached["result_id"] == "res_001"
        assert cached["hit_count"] == 1

        # 不同 filename 不应命中
        cached2 = db.find_cached("sha1", "b.png", "gpt-4o", "")
        assert cached2 is None

        # 不同 question 不应命中
        cached3 = db.find_cached("sha1", "a.png", "gpt-4o", "question")
        assert cached3 is None
    finally:
        db.close()
        os.unlink(db_path)


def test_get_by_result_id_and_recent():
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha1",
            filename="a.png",
            phash="",
            model_id="gpt-4o",
            question="",
            result_id="res_recent",
            source_value="/tmp/a.png",
            summary="s",
            text="t",
            tags=[],
            result_json={},
        )
        row = db.get_by_result_id("res_recent")
        assert row is not None
        assert row["result_id"] == "res_recent"

        recent = db.get_recent(limit=5)
        assert len(recent) == 1
        assert recent[0]["result_id"] == "res_recent"
    finally:
        db.close()
        os.unlink(db_path)


def test_search_and_path_filename():
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha1",
            filename="invoice_001.png",
            phash="",
            model_id="gpt-4o",
            question="",
            result_id="res_invoice",
            source_value="/data/invoice/invoice_001.png",
            summary="这是一张发票",
            text="金额 100 元",
            tags=["invoice"],
            result_json={},
        )

        results = db.search("发票")
        assert len(results) == 1
        assert results[0]["filename"] == "invoice_001.png"

        results = db.get_by_filename("invoice_001.png")
        assert len(results) == 1

        results = db.get_by_path("/data/invoice")
        assert len(results) == 1
    finally:
        db.close()
        os.unlink(db_path)


def test_create_store_returns_sqlite():
    db = create_store(":memory:")
    assert isinstance(db, SQLiteVisionStore)
    db.close()
