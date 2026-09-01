"""
数据库后端抽象
提供统一接口，默认使用 SQLite，可选 PostgreSQL / MongoDB。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone


def _like_escape(text: str) -> str:
    return text.replace("%", "\\%").replace("_", "\\_")


class VisionStore(ABC):
    """图片缓存存储抽象基类。"""

    @abstractmethod
    def find_cached(self, sha256: str, model_id: str, question: str = "") -> dict | None:
        """查询缓存，命中时返回结果并更新 hit_count。

        按内容寻址（sha256），与文件名无关：同一内容的图片无论文件名如何都命中，
        这对 see_window 的时间戳截图文件名至关重要。
        """
        raise NotImplementedError

    @abstractmethod
    def find_cached_by_phash(self, phash: str, model_id: str, question: str = "", max_distance: int = 5) -> dict | None:
        """sha256 精确未命中后的近似兑底：按感知哈希匹配。

        命中条件：同 model_id + question 的记录中，phash 汉明距离 <= max_distance
        的最近一条。缩尺/重压缩的同一张图 sha256 必变，但 phash 几乎不变。
        返回字典附带 matched_by="phash" 和 phash_distance，供调用方知晓是近似命中。
        """
        raise NotImplementedError

    @abstractmethod
    def insert(self, *, sha256: str, filename: str, phash: str, model_id: str, question: str,
               result_id: str, source_value: str, summary: str, text: str, tags: list[str],
               result_json: dict) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, limit: int = 20, offset: int = 0) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_by_path(self, path: str, limit: int = 20, offset: int = 0) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_by_result_id(self, result_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def get_by_filename(self, filename: str, limit: int = 20, offset: int = 0) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def export_all(self, limit: int = 0, offset: int = 0) -> list[dict]:
        """导出原始结果记录，limit=0 表示全部。"""
        raise NotImplementedError

    @staticmethod
    def sha256_of_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS image_cache (
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    phash TEXT,
    model_id TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    result_id TEXT PRIMARY KEY,
    source_value TEXT,
    summary TEXT,
    text TEXT,
    tags TEXT,
    result_json TEXT NOT NULL,
    read_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sha256 ON image_cache(sha256);
CREATE INDEX IF NOT EXISTS idx_filename ON image_cache(filename);
CREATE INDEX IF NOT EXISTS idx_phash ON image_cache(phash);
CREATE INDEX IF NOT EXISTS idx_tags ON image_cache(tags);
"""


class SQLiteVisionStore(VisionStore):
    def __init__(self, db_path: str):
        self.db_path = db_path
        # 调用方会从事件循环 offload 到线程池并发访问本对象，
        # 单个 sqlite3 连接跨线程共享时必须串行化。
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=60.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _ensure_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=60.0)
        return self._conn

    def find_cached(self, sha256: str, model_id: str, question: str = "") -> dict | None:
        with self._lock:
            db = self._ensure_conn()
            if model_id:
                row = db.execute(
                    "SELECT result_id, result_json, summary, text, tags, read_at FROM image_cache WHERE sha256 = ? AND model_id = ? AND question = ? ORDER BY read_at DESC",
                    (sha256, model_id, question),
                ).fetchone()
            else:
                # model_id 为空时放宽为任意模型命中（同一张图的缓存不因换配置而失效）
                row = db.execute(
                    "SELECT result_id, result_json, summary, text, tags, read_at FROM image_cache WHERE sha256 = ? AND question = ? ORDER BY read_at DESC",
                    (sha256, question),
                ).fetchone()
            if row:
                db.execute(
                    "UPDATE image_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE result_id = ?",
                    (datetime.now(timezone.utc).isoformat(), row[0]),
                )
                db.commit()
                hit_row = db.execute(
                    "SELECT hit_count FROM image_cache WHERE result_id = ?",
                    (row[0],),
                ).fetchone()
                return {
                    "result_id": row[0],
                    "result_json": row[1],
                    "summary": row[2],
                    "text": row[3],
                    "tags": row[4],
                    "read_at": row[5],
                    "hit_count": hit_row[0] if hit_row else 1,
                }
            return None

    @staticmethod
    def _phash_distance(a: str, b: str) -> int | None:
        """两个十六进制 phash 的汉明距离；为空、长度不一致或非十六进制时返回 None。"""
        a = (a or "").strip()
        b = (b or "").strip()
        if not a or not b or len(a) != len(b):
            return None
        try:
            return bin(int(a, 16) ^ int(b, 16)).count("1")
        except ValueError:
            return None

    def find_cached_by_phash(self, phash: str, model_id: str, question: str = "", max_distance: int = 5) -> dict | None:
        if not phash:
            return None
        with self._lock:
            db = self._ensure_conn()
            if model_id:
                rows = db.execute(
                    "SELECT result_id, result_json, summary, text, tags, read_at, phash FROM image_cache WHERE model_id = ? AND question = ? AND phash IS NOT NULL AND phash != ''",
                    (model_id, question),
                ).fetchall()
            else:
                # model_id 为空时放宽为任意模型命中（与 find_cached 一致）
                rows = db.execute(
                    "SELECT result_id, result_json, summary, text, tags, read_at, phash FROM image_cache WHERE question = ? AND phash IS NOT NULL AND phash != ''",
                    (question,),
                ).fetchall()
            best = None
            best_dist = max_distance + 1
            for row in rows:
                d = self._phash_distance(phash, row[6])
                if d is not None and d < best_dist:
                    best = row
                    best_dist = d
            if best is None:
                return None
            db.execute(
                "UPDATE image_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE result_id = ?",
                (datetime.now(timezone.utc).isoformat(), best[0]),
            )
            db.commit()
            hit_row = db.execute(
                "SELECT hit_count FROM image_cache WHERE result_id = ?",
                (best[0],),
            ).fetchone()
            return {
                "result_id": best[0],
                "result_json": best[1],
                "summary": best[2],
                "text": best[3],
                "tags": best[4],
                "read_at": best[5],
                "hit_count": hit_row[0] if hit_row else 1,
                "matched_by": "phash",
                "phash_distance": best_dist,
            }

    def insert(
        self,
        *,
        sha256: str,
        filename: str,
        phash: str,
        model_id: str,
        question: str,
        result_id: str,
        source_value: str,
        summary: str,
        text: str,
        tags: list[str],
        result_json: dict,
    ) -> None:
        with self._lock:
            db = self._ensure_conn()
            db.execute(
                """
                INSERT INTO image_cache (sha256, filename, phash, model_id, question, result_id, source_value, summary, text, tags, result_json, read_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256,
                    filename,
                    phash,
                    model_id,
                    question,
                    result_id,
                    source_value,
                    summary,
                    text,
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(result_json, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.commit()

    def search(self, query: str, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            like = f"%{_like_escape(query)}%"
            rows = db.execute(
                """
                SELECT result_id, sha256, filename, source_value, summary, text, tags, read_at, hit_count
                FROM image_cache
                WHERE summary LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'
                  OR filename LIKE ? ESCAPE '\\' OR source_value LIKE ? ESCAPE '\\'
                ORDER BY hit_count DESC, read_at DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, like, like, limit, offset),
            ).fetchall()
            db.row_factory = None
            return [dict(r) for r in rows]

    def get_by_path(self, path: str, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            like = f"%{_like_escape(path)}%"
            rows = db.execute(
                """
                SELECT * FROM image_cache
                WHERE source_value LIKE ? ESCAPE '\\'
                ORDER BY read_at DESC
                LIMIT ? OFFSET ?
                """,
                (like, limit, offset),
            ).fetchall()
            db.row_factory = None
            return [dict(r) for r in rows]

    def get_recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM image_cache
                ORDER BY read_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            db.row_factory = None
            return [dict(r) for r in rows]

    def get_by_result_id(self, result_id: str) -> dict | None:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM image_cache WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            db.row_factory = None
            if row:
                db.execute(
                    "UPDATE image_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE result_id = ?",
                    (datetime.now(timezone.utc).isoformat(), result_id),
                )
                db.commit()
                d = dict(row)
                d["hit_count"] = d.get("hit_count", 0) + 1
                return d
            return None

    def get_by_filename(self, filename: str, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM image_cache WHERE filename = ? ORDER BY read_at DESC LIMIT ? OFFSET ?",
                (filename, limit, offset),
            ).fetchall()
            db.row_factory = None
            return [dict(r) for r in rows]

    def export_all(self, limit: int = 0, offset: int = 0) -> list[dict]:
        with self._lock:
            db = self._ensure_conn()
            db.row_factory = sqlite3.Row
            if limit > 0:
                rows = db.execute(
                    "SELECT * FROM image_cache ORDER BY read_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM image_cache ORDER BY read_at DESC LIMIT -1 OFFSET ?",
                    (offset,),
                ).fetchall()
            db.row_factory = None
            return [dict(r) for r in rows]


def create_store(db_path: str) -> VisionStore:
    """根据配置创建存储后端。"""
    # 未来可在此根据 URI 前缀选择 PostgreSQL / MongoDB
    return SQLiteVisionStore(db_path)
