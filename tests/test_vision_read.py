"""
测试 vision_read 路径收集与缓存命中
"""

import asyncio
import os
import tempfile

from tools._store import create_store
from tools import config as tool_config
from tools.vision_read import _collect_image_paths, _parse_result


def test_collect_image_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        img1 = os.path.join(tmpdir, "a.png")
        img2 = os.path.join(tmpdir, "b.jpg")
        subdir = os.path.join(tmpdir, "sub")
        os.makedirs(subdir)
        img3 = os.path.join(subdir, "c.webp")
        txt = os.path.join(tmpdir, "not_image.txt")

        open(img1, "wb").close()
        open(img2, "wb").close()
        open(img3, "wb").close()
        open(txt, "w").close()

        paths = _collect_image_paths([tmpdir])
        assert sorted(paths) == sorted([img1, img2, img3])

        paths = _collect_image_paths([os.path.join(tmpdir, "a.png")])
        assert paths == [img1]


def test_parse_result():
    raw = "第一行摘要\n第二行细节\n第三行文字"
    parsed = _parse_result(raw)
    assert parsed["peek"] == "第一行摘要"
    assert parsed["text"] == raw
    assert parsed["tags"] == []


def test_parse_result_structured_json():
    raw = '{"peek": "这是一张发票图片，可以看到金额 100 元", "text": "完整描述……", "tags": ["发票", "文档"]}'
    parsed = _parse_result(raw)
    assert parsed["peek"] == "这是一张发票图片，可以看到金额 100 元"
    assert parsed["text"] == "完整描述……"
    assert parsed["tags"] == ["发票", "文档"]


def test_parse_result_fenced_json():
    raw = '好的，这是结果：\n```json\n{"peek": "这是一张截图", "text": "细节", "tags": ["截图"]}\n```'
    parsed = _parse_result(raw)
    assert parsed["peek"] == "这是一张截图"
    assert parsed["text"] == "细节"
    assert parsed["tags"] == ["截图"]


def test_parse_result_broken_json_fallback():
    raw = '这是描述：{"peek": "broken'
    parsed = _parse_result(raw)
    assert parsed["peek"].startswith("这是描述")  # 回退首行预览
    assert parsed["tags"] == []


def test_parse_result_json_missing_fields():
    raw = '{"text": "只有正文没有预览"}'
    parsed = _parse_result(raw)
    assert parsed["text"] == "只有正文没有预览"
    assert parsed["peek"] == "只有正文没有预览"


def test_parse_result_legacy_summary_key():
    """旧模型输出的 summary 字段名仍能解析（peek 优先、summary 兜底）。"""
    raw = '{"summary": "老格式预览", "text": "正文", "tags": ["旧标签"]}'
    parsed = _parse_result(raw)
    assert parsed["peek"] == "老格式预览"
    assert parsed["text"] == "正文"
    assert parsed["tags"] == ["旧标签"]


async def _run_vision_read_hits_cache(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)

    img = tmp_path / "cached.png"
    img.write_bytes(b"fake image")

    db = create_store(db_path)
    tool_config.set_config(
        {
            "vl_provider_ids": "",
            "vl_model": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o",
                "concurrency": 1,
            }
        },
        str(tmp_path),
    )
    tool_config.set_providers([])

    db.insert(
        sha256=db.sha256_of_file(str(img)),
        filename="cached.png",
        phash="",
        model_id="gpt-4o",
        question="",
        result_id="res_cached",
        source_value=str(img),
        peek="预设预览",
        text="预设文字",
        tags=[],
        result_json={},
    )

    from tools import vision_read

    result = await vision_read.read(db, paths=[str(img)])
    db.close()
    return result


def test_vision_read_hits_cache(tmp_path):
    result = asyncio.run(_run_vision_read_hits_cache(tmp_path))
    assert result["ok"] is True
    assert result["total"] == 1
    assert result["cached"] == 1
    assert result["read"] == 0


async def _run_vision_read_missing_key(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)

    img = tmp_path / "new.png"
    img.write_bytes(b"fake image")

    db = create_store(db_path)
    tool_config.set_config(
        {
            "vl_provider_ids": "",
            "vl_model": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o",
            }
        },
        str(tmp_path),
    )
    tool_config.set_providers([])

    from tools import vision_read

    result = await vision_read.read(db, paths=[str(img)])
    db.close()
    return result


def test_vision_read_missing_key_for_new_image(tmp_path):
    result = asyncio.run(_run_vision_read_missing_key(tmp_path))
    assert result["ok"] is False
    assert "api_key" in result["proposal"] or "VL 模型" in result["proposal"]


# ---------- 结构化输出与 allow_phash 的集成测试 ----------

def _setup_fake_vl(tmp_path):
    """配置一个带假 key 的 vl_model，返回 (db, db_path)。"""
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    tool_config.set_config(
        {
            "vl_provider_ids": "",
            "vl_model": {
                "provider": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "fake-key",
                "model": "fake-vl",
                "concurrency": 1,
            },
        },
        str(tmp_path),
    )
    tool_config.set_providers([])
    return db, db_path


def _make_test_image(path, size=(800, 600)):
    """生成有结构的测试图（渐变+图形），phash 对缩放稳定。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size)
    px = img.load()
    for x in range(0, size[0], 4):
        for y in range(0, size[1], 4):
            c = (x % 256, (x + y) % 256, y % 256)
            for dx in range(4):
                for dy in range(4):
                    if x + dx < size[0] and y + dy < size[1]:
                        px[x + dx, y + dy] = c
    d = ImageDraw.Draw(img)
    d.rectangle([100, 100, size[0] // 2, size[1] // 2], fill=(200, 30, 30))
    d.ellipse([size[0] // 2, 100, size[0] - 100, size[1] - 100], fill=(30, 30, 200))
    img.save(path)
    return img


def test_structured_read_populates_tags(tmp_path):
    """mock VL 返回结构化 JSON → peek 取字段、tags 真正落库。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False):
        assert "json" in prompt.lower()  # 结构化 prompt 必须含 json 字样
        return '{"peek": "这是一张测试图片，可以看到红蓝图形", "text": "完整描述", "tags": ["测试", "图形"]}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["ok"] is True
            assert result["read"] == 1
            rid = result["next_call"]["arguments"]["result_id"]
            row = db.get_by_result_id(rid)
            assert row["peek"] == "这是一张测试图片，可以看到红蓝图形"
            import json as _json
            assert _json.loads(row["tags"]) == ["测试", "图形"]
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_allow_phash_false_disables_fallback(tmp_path):
    """allow_phash=False（see_window 场景）：缩尺变体不再近似命中，会调 VL；
    allow_phash=True 时同变体近似命中。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img = _make_test_image(str(tmp_path / "orig.png"))
    img.resize((400, 300)).save(tmp_path / "resized.png")

    calls = []
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False):
        calls.append(path)
        return '{"peek": "描述", "text": "细节", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            # 第一次读原图（落库）
            r1 = await vision_read.read(db, paths=[str(tmp_path / "orig.png")])
            assert r1["read"] == 1

            # allow_phash=True：缩尺变体近似命中
            r2 = await vision_read.read(db, paths=[str(tmp_path / "resized.png")])
            assert r2["cached"] == 1
            assert r2.get("cached_via_phash") == 1  # 近似命中透传进响应
            assert len(calls) == 1

            # allow_phash=False：同变体必须重新调 VL（屏幕内容新鲜性优先）
            r3 = await vision_read.read(db, paths=[str(tmp_path / "resized.png")], allow_phash=False)
            assert r3["read"] == 1
            assert len(calls) == 2
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_parse_result_placeholder_copy_rejected():
    """弱模型照抄 JSON 骨架占位符时，占位符被剔除并按兜底路径处理。"""
    raw = '{"peek": "一句话直接回答", "text": "真正的回答内容", "tags": []}'
    parsed = _parse_result(raw)
    assert parsed["peek"] == "真正的回答内容"  # 占位符 peek 被剔除，用正文首行兜底
    assert parsed["text"] == "真正的回答内容"


def test_follow_up_context_before_json_instruction(tmp_path):
    """追问模式：「之前的理解」上下文必须注入在 JSON 输出要求之前。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    # 预置一条同图记录作为追问上文
    db.insert(
        sha256=db.sha256_of_file(img_path),
        filename="doc.png",
        phash="",
        model_id="fake-vl",
        question="",
        result_id="res_prev",
        source_value=img_path,
        peek="之前的预览",
        text="之前的正文",
        tags=[],
        result_json={},
    )

    captured = {}
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False):
        captured["prompt"] = prompt
        return '{"peek": "回答", "text": "细节", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(
                db, paths=[img_path], question="金额是多少？", previous_result_id="res_prev"
            )
            assert result["read"] == 1
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())
    p = captured["prompt"]
    assert "之前对这张图的理解" in p
    assert p.index("之前对这张图的理解") < p.index("JSON")  # 上下文在格式要求之前
