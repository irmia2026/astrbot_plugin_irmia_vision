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


def test_search_multi_term_and():
    """多词 AND：空格/逗号分隔的每个词都需在任一字段命中（词序无关）；
    单词行为不变；空白查询返回空。"""
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="s1", filename="a.png", phash="", model_id="m", question="",
            result_id="res_both", source_value="/a", peek="浴室照片",
            text="浴缸里放着手机", tags=[], result_json={},
        )
        db.insert(
            sha256="s2", filename="b.png", phash="", model_id="m", question="",
            result_id="res_only_tub", source_value="/b", peek="浴室",
            text="浴缸特写", tags=[], result_json={},
        )
        # 单词：两条都命中
        assert {r["result_id"] for r in db.search("浴缸")} == {"res_both", "res_only_tub"}
        # 多词 AND：「浴缸 手机」= 含浴缸且含手机（实测旧实现单串 LIKE 返回 0 条）
        assert [r["result_id"] for r in db.search("浴缸 手机")] == ["res_both"]
        assert [r["result_id"] for r in db.search("手机 浴缸")] == ["res_both"]  # 词序无关
        # 中文逗号/顿号分隔
        assert [r["result_id"] for r in db.search("浴缸，手机")] == ["res_both"]
        assert [r["result_id"] for r in db.search("浴缸、手机")] == ["res_both"]
        assert db.search("   ") == []
    finally:
        db.close()
        os.unlink(db_path)


def test_search_orders_by_read_at_not_hit_count():
    """排序按 read_at DESC：hit_count 是缓存复用次数，与查询相关度无关——
    被反复复用的老图不应霸榜（实测：今天刚读的图因 hit_count 低被埋在 20 名之外）。"""
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="s_old", filename="old.png", phash="", model_id="m", question="",
            result_id="res_old", source_value="/old", peek="水印", text="老图水印",
            tags=[], result_json={},
        )
        for _ in range(6):  # 老图刷高 hit_count
            db.find_cached("s_old", "m", "")
        db.insert(
            sha256="s_new", filename="new.png", phash="", model_id="m", question="",
            result_id="res_new", source_value="/new", peek="水印", text="新图水印",
            tags=[], result_json={},
        )
        results = db.search("水印")
        assert results[0]["result_id"] == "res_new"  # 新记录在前（hit_count=0）
        assert results[1]["result_id"] == "res_old"  # hit_count=6 反而在后
    finally:
        db.close()
        os.unlink(db_path)


def test_find_cached_by_phash_deterministic_on_tie():
    """同 phash 多记录（等距候选）：命中是确定的——候选按 read_at DESC 遍历、
    等距严格小于不替换 → 最新记录优先，两次调用返回同一条。"""
    db, db_path = _make_db()
    try:
        # 两条 phash 完全相同的记录（popcount 在 4-60 正常范围）
        for rid in ("res_old", "res_new"):
            db.insert(
                sha256=f"sha_{rid}", filename=f"{rid}.png", phash="9e9a2b13bdbd3090",
                model_id="m", question="", result_id=rid, source_value=f"/tmp/{rid}.png",
                peek=rid, text=rid, tags=[], result_json={},
            )
        # 查询 phash 与两者距离相同（末位 0→1，距离 1）
        first = db.find_cached_by_phash("9e9a2b13bdbd3091", "m", "")
        second = db.find_cached_by_phash("9e9a2b13bdbd3091", "m", "")
        assert first is not None and second is not None
        assert first["result_id"] == second["result_id"]  # 确定性
        assert first["result_id"] == "res_new"  # 等距取最新
    finally:
        db.close()
        os.unlink(db_path)


def test_get_by_result_id_does_not_count_hit():
    """get_by_result_id 只读不计数：hit_count 专属于缓存命中，
    「查看」不刷高命中统计（search 按 hit_count DESC 排序的可信度）。"""
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha1", filename="a.png", phash="", model_id="m", question="",
            result_id="res_x", source_value="/tmp/a.png", peek="p", text="t",
            tags=[], result_json={},
        )
        db.get_by_result_id("res_x")
        db.get_by_result_id("res_x")
        row = db.get_by_result_id("res_x")
        assert row["hit_count"] == 0  # 查看不计数
        # 真缓存命中才计数
        db.find_cached("sha1", "m", "")
        assert db.get_by_result_id("res_x")["hit_count"] == 1
    finally:
        db.close()
        os.unlink(db_path)


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
            peek="peek-summary",
            text="text",
            tags=["tag1"],
            result_json={"peek": "peek-summary", "text": "text", "tags": ["tag1"]},
        )

        cached = db.find_cached("sha1", "gpt-4o", "")
        assert cached is not None
        assert cached["result_id"] == "res_001"
        assert cached["hit_count"] == 1

        # 缓存按内容寻址（sha256），lookup 不涉及文件名：
        # 记录即便是在另一个文件名下落库的（如 see_window 时间戳截图），同内容也命中；
        # 多条同 sha 记录时取最新一条（ORDER BY read_at DESC）
        db.insert(
            sha256="sha1",
            filename="see_window_20260901_999999.png",
            phash="phash1",
            model_id="gpt-4o",
            question="",
            result_id="res_002",
            source_value="/tmp/see_window_20260901_999999.png",
            peek="peek2",
            text="text2",
            tags=["tag2"],
            result_json={},
        )
        cached2 = db.find_cached("sha1", "gpt-4o", "")
        assert cached2 is not None
        assert cached2["result_id"] == "res_002"

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
            peek="s",
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


def test_detail_in_cache_key():
    """detail 是缓存键的一部分；'' 与 'auto' 语义等价互相兼容。"""
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha1", filename="a.png", phash="", model_id="m", question="",
            result_id="res_low", source_value="/tmp/a.png", peek="s", text="t",
            tags=[], result_json={}, detail="low",
        )
        # 同 detail → 命中
        assert db.find_cached("sha1", "m", "", "low") is not None
        # 不同 detail → 不命中（改配置后不会静默返回旧档位结果）
        assert db.find_cached("sha1", "m", "", "original") is None
        # '' 与 'auto' 等价：insert 用 ''，查询用 'auto' 也命中（老记录兼容）
        db.insert(
            sha256="sha2", filename="b.png", phash="", model_id="m", question="",
            result_id="res_legacy", source_value="/tmp/b.png", peek="s", text="t",
            tags=[], result_json={}, detail="",
        )
        assert db.find_cached("sha2", "m", "", "auto") is not None
        assert db.find_cached("sha2", "m", "", "") is not None
    finally:
        db.close()
        os.unlink(db_path)


def test_find_cached_by_phash_skips_solid_color():
    """纯色/低信息图片的 phash 趋同（全 0/全 1），近似兜底必须跳过。"""
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha_white", filename="white.png", phash="0000000000000000",
            model_id="m", question="", result_id="res_white",
            source_value="/tmp/white.png", peek="s", text="t", tags=[], result_json={},
        )
        # 纯黑图 phash 全 0：popcount < 4 → 跳过兜底，不错误命中纯白的记录
        assert db.find_cached_by_phash("0000000000000000", "m", "") is None
        # 全 1（popcount 64 > 60）同样跳过
        assert db.find_cached_by_phash("ffffffffffffffff", "m", "") is None
    finally:
        db.close()
        os.unlink(db_path)


def test_find_cached_by_phash_guards_candidates():
    """候选侧守卫：库中已落库的纯色记录不能参与近似匹配。

    查询图 popcount=4 恰好通过查询侧守卫，距离纯色记录仅 4（≤阈值），
    若不过滤候选侧就会误命中。
    """
    db, db_path = _make_db()
    try:
        db.insert(
            sha256="sha_white", filename="white.png", phash="0000000000000000",
            model_id="m", question="", result_id="res_white",
            source_value="/tmp/white.png", peek="s", text="t", tags=[], result_json={},
        )
        # "000000000000000f" popcount=4（过查询侧守卫），距全 0 记录距离=4
        assert db.find_cached_by_phash("000000000000000f", "m", "") is None
    finally:
        db.close()
        os.unlink(db_path)


def test_find_cached_by_phash_tiebreak_prefers_newest():
    """等距离时应取最新记录（与 find_cached 的 read_at DESC 语义一致）。"""
    import time
    db, db_path = _make_db()
    try:
        for rid in ("res_older", "res_newer"):
            db.insert(
                sha256=f"sha_{rid}", filename=f"{rid}.png", phash="0123456789abcdef",
                model_id="m", question="", result_id=rid,
                source_value=f"/tmp/{rid}.png", peek="s", text="t", tags=[], result_json={},
            )
            time.sleep(0.01)  # 确保 read_at 可区分
        hit = db.find_cached_by_phash("0123456789abcdef", "m", "")
        assert hit is not None
        assert hit["result_id"] == "res_newer"
    finally:
        db.close()
        os.unlink(db_path)


def test_ensure_conn_reconnect_is_thread_safe():
    """close() 后重连的连接必须带 check_same_thread=False（offload 线程可用）。"""
    import threading
    db, db_path = _make_db()
    try:
        db.close()  # _conn = None，下次调用走 _ensure_conn 重连
        errors = []

        def _worker():
            try:
                db.find_cached("sha_x", "m", "")
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        assert not errors, f"跨线程重连调用失败: {errors}"
        # 重连后 PRAGMA 也必须完整重放（synchronous=NORMAL → 1, busy_timeout=30000）
        assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
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
            peek="s",
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
            peek="这是一张发票",
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


def test_migration_rename_summary_to_peek():
    """老库（summary 列）打开后自动迁移为 peek 列，数据保留可查询。"""
    import sqlite3

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # 手工构造老 schema + 一条老数据
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE image_cache (
                sha256 TEXT NOT NULL, filename TEXT NOT NULL, phash TEXT,
                model_id TEXT NOT NULL, question TEXT NOT NULL DEFAULT '',
                result_id TEXT PRIMARY KEY, source_value TEXT, summary TEXT,
                text TEXT, tags TEXT, result_json TEXT NOT NULL,
                read_at TEXT NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT
            );
            INSERT INTO image_cache (sha256, filename, phash, model_id, question, result_id, source_value, summary, text, tags, result_json, read_at)
            VALUES ('sha_old', 'old.png', '', 'm', '', 'res_old', '/tmp/old.png', '老预览', '老正文', '[]', '{}', '2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        db = create_store(db_path)  # 触发迁移
        try:
            row = db.get_by_result_id("res_old")
            assert row["peek"] == "老预览"  # 列已重命名且数据保留
            assert row["text"] == "老正文"
            hit = db.find_cached("sha_old", "m", "", "")
            assert hit is not None and hit["peek"] == "老预览"
        finally:
            db.close()
    finally:
        os.unlink(db_path)


def test_find_cached_same_tick_returns_latest(monkeypatch):
    """同一时钟 tick 内连续插入同 sha 记录（Windows 粗时钟 ~15.6ms 粒度场景），
    read_at 并列时 find_cached 必须返回后插入者（rowid DESC 兜底）。"""
    import tools._store as store_mod
    from datetime import datetime as _dt, timezone as _tz

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return _dt(2026, 1, 1, tzinfo=_tz.utc)

    monkeypatch.setattr(store_mod, "datetime", _FixedDateTime)

    db, db_path = _make_db()
    try:
        for rid in ("res_first", "res_second"):
            db.insert(
                sha256="sha_same", filename="a.png", phash="", model_id="m", question="",
                result_id=rid, source_value="/tmp/a.png", peek=f"peek-{rid}", text="t",
                tags=[], result_json={},
            )
        hit = db.find_cached("sha_same", "m", "")
        assert hit is not None
        assert hit["result_id"] == "res_second"  # read_at 并列时 rowid 兜底取后插入者
    finally:
        db.close()
        os.unlink(db_path)
