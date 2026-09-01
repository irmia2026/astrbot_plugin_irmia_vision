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
        cached = db.find_cached("sha1", "gpt-4o", "")
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

        cached = db.find_cached("sha1", "gpt-4o", "")
        assert cached is not None
        assert cached["result_id"] == "res_001"
        assert cached["hit_count"] == 1

        # 缓存按内容寻址（sha256），不含 filename 维度：
        # 同内容不同文件名（如 see_window 的时间戳截图）也应命中
        cached2 = db.find_cached("sha1", "gpt-4o", "")
        assert cached2 is not None

        # 不同 question 不应命中
        cached3 = db.find_cached("sha1", "gpt-4o", "question")
        assert cached3 is None

        # 不同 model_id 不应命中
        cached4 = db.find_cached("sha1", "other-model", "")
        assert cached4 is None

        # model_id 为空时放宽为任意模型命中
        cached5 = db.find_cached("sha1", "", "")
        assert cached5 is not None
    finally:
        db.close()
        os.unlink(db_path)


def test_find_cached_by_phash():
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha1",
            filename="a.png",
            phash="0123456789abcdef",
            model_id="gpt-4o",
            question="",
            result_id="res_phash",
            source_value="/tmp/a.png",
            summary="s",
            text="t",
            tags=[],
            result_json={},
        )

        # 相同 phash → 命中，且标注 matched_by
        hit = db.find_cached_by_phash("0123456789abcdef", "gpt-4o", "")
        assert hit is not None
        assert hit["result_id"] == "res_phash"
        assert hit["matched_by"] == "phash"
        assert hit["phash_distance"] == 0

        # 距离 4（末位 f→0）≤ 阈值 5 → 命中
        near = db.find_cached_by_phash("0123456789abcde0", "gpt-4o", "")
        assert near is not None
        assert near["phash_distance"] == 4

        # 距离远超阈值 → 不命中
        far = db.find_cached_by_phash("fedcba9876543210", "gpt-4o", "")
        assert far is None

        # 空 phash → 直接 None
        assert db.find_cached_by_phash("", "gpt-4o", "") is None

        # question 不同 → 不命中
        assert db.find_cached_by_phash("0123456789abcdef", "gpt-4o", "别的问题") is None

        # model_id 不同 → 不命中；model_id 为空放宽 → 命中
        assert db.find_cached_by_phash("0123456789abcdef", "other-model", "") is None
        assert db.find_cached_by_phash("0123456789abcdef", "", "") is not None
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
