"""
测试 vision_export 导出功能
"""

import asyncio
import json
import os
import tempfile

from tools._store import create_store
from tools import vision_export


async def _run_export_json(tmp_path):
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

        output = os.path.join(tmp_path, "export.json")
        result = await vision_export.export(db, output_path=output, fmt="json")
        return result, output
    finally:
        db.close()


def test_vision_export_json(tmp_path):
    result, output = asyncio.run(_run_export_json(tmp_path))
    assert result["ok"] is True
    assert result["exported"] == 1
    assert result["format"] == "json"
    assert os.path.exists(output)
    with open(output, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["result_id"] == "res_invoice"
    assert data[0]["text"] == "invoice text content"


async def _run_export_csv(tmp_path):
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

        output = os.path.join(tmp_path, "export.csv")
        result = await vision_export.export(db, output_path=output, fmt="csv")
        return result, output
    finally:
        db.close()


def test_vision_export_csv(tmp_path):
    result, output = asyncio.run(_run_export_csv(tmp_path))
    assert result["ok"] is True
    assert result["format"] == "csv"
    assert os.path.exists(output)


async def _run_export_default_path(tmp_path):
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
            result_id="res_a",
            source_value="/tmp/a.png",
            peek="peek-summary",
            text="text",
            tags=[],
            result_json={},
        )

        from tools import config as tool_config
        tool_config.set_config({}, str(tmp_path))
        result = await vision_export.export(db)
        return result
    finally:
        db.close()


def test_vision_export_default_path(tmp_path):
    result = asyncio.run(_run_export_default_path(tmp_path))
    assert result["ok"] is True
    assert os.path.exists(result["output_path"])
