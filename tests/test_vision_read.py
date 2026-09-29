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

        found, missing = _collect_image_paths([tmpdir])
        assert found == sorted([img1, img2, img3])  # 目录内排序保确定性
        assert missing == []

        found, missing = _collect_image_paths([os.path.join(tmpdir, "a.png")])
        assert found == [img1]
        assert missing == []

        # 未找到与格式不支持的传入路径都必须回显（相对路径静默丢弃的修复）
        found, missing = _collect_image_paths([os.path.join(tmpdir, "nope.png"), txt])
        assert found == []
        assert len(missing) == 2


def test_collect_image_paths_preserves_input_order(tmp_path):
    """顶层顺序 = 传入顺序（compare 的图1/图2 编号依赖它，before/after 不能被字典序调换）；
    目录内排序保确定性；首现去重。"""
    a = str(tmp_path / "aa_second.png")
    z = str(tmp_path / "zz_first.png")
    open(a, "wb").close()
    open(z, "wb").close()
    found, missing = _collect_image_paths([z, a])  # 传入顺序与字典序相反
    assert found == [z, a]
    # 目录 + 文件混合：目录内 sorted，顶层保传入序
    sub = tmp_path / "sub"
    sub.mkdir()
    s1 = str(sub / "m.png")
    s2 = str(sub / "b.png")
    open(s1, "wb").close()
    open(s2, "wb").close()
    found, _ = _collect_image_paths([z, str(sub), a])
    assert found == [z, s2, s1, a]
    # 首现去重
    found, _ = _collect_image_paths([z, a, z])
    assert found == [z, a]


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

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
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

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
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

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
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


def test_parse_result_json_only_tags():
    """模型只输出 tags 的 JSON 时返回空字段，不把 JSON 原文当预览落库。"""
    parsed = _parse_result('{"tags": ["a", "b"]}')
    assert parsed["peek"] == ""
    assert parsed["text"] == ""
    assert parsed["tags"] == ["a", "b"]


def test_batch_read_next_call_is_list_mode(tmp_path):
    """批量读图后 next_call 应引导 list 模式（recent），不能夹带 result_id
    （否则 vision_query 会优先走 full，agent 只能看到第一张的详情）。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    _make_test_image(str(tmp_path / "a.png"), size=(800, 600))
    _make_test_image(str(tmp_path / "b.png"), size=(820, 610))  # 尺寸不同 → 内容指纹不同，避免互相命中缓存

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        return '{"peek": "预览", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["read"] == 2
            args = result["next_call"]["arguments"]
            assert args == {"recent": 2}
            assert "result_id" not in args
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_parse_result_tags_placeholder_filtered():
    """tags 中的占位符照抄（如 '3-6个检索标签'）会被剔除。"""
    raw = '{"peek": "真实预览", "text": "正文", "tags": ["3-6个检索标签", "发票"]}'
    parsed = _parse_result(raw)
    assert parsed["tags"] == ["发票"]


def test_parse_result_empty_json_object():
    """空 JSON 对象 {} 不落入兜底路径把原文当预览。"""
    parsed = _parse_result("{}")
    assert parsed["peek"] == ""
    assert parsed["text"] == ""


def test_follow_up_without_question_keeps_json_instruction_last(tmp_path):
    """无 question 但带 previous_result_id 的路径：上下文仍在 JSON 格式要求之前。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

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

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        captured["prompt"] = prompt
        return '{"peek": "预览", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path], previous_result_id="res_prev")
            assert result["read"] == 1
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())
    p = captured["prompt"]
    assert "之前对这张图的理解" in p
    assert p.index("之前对这张图的理解") < p.index("输出 JSON")


def test_empty_structured_content_not_stored(tmp_path):
    """模型返回纯 tags/空 JSON（解析后无实质内容）→ 计为失败且不落库，
    避免空记录永久占用缓存键。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        return '{"tags": ["抽风"]}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            # 全部失败时返回提案式错误形态（无 read/cached 键）
            assert result["ok"] is False
            assert "结构化解析后为空" in result["proposal"]
            # 不落库：DB 应为空
            assert db.get_recent(limit=10) == []
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_decode_failure_not_attributed_to_model(tmp_path):
    """坏图（0 字节）：不归因模型失败、不 dump provider 链、不调 VL、保留 failed 计数。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"")  # 0 字节

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        raise AssertionError("坏图不应触发 VL 调用")

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            return await vision_read.read(db, paths=[str(broken)])
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "无法解码" in result["proposal"]
    assert "VL 模型均调用失败" not in result["proposal"]  # 不归因模型
    assert "chain=" not in result["proposal"]  # 不 dump provider 链
    assert result["failed"] == 1  # 保留计数


def test_partial_decode_failure_counts(tmp_path):
    """1 好 1 坏：status=partial，decode_failed 单独计数，好图正常落库。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    good = tmp_path / "good.png"
    _make_test_image(str(good))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"truncated")

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        return '{"peek": "好图", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            return await vision_read.read(db, paths=[str(good), str(broken)])
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["read"] == 1
    assert result["failed"] == 1
    assert result["decode_failed"] == 1


def test_result_ids_cap10_reports_unlisted(tmp_path):
    """12 张全命中：result_ids 列前 10 个，unlisted_count 告知剩余（不再静默截断）。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    paths = []
    for i in range(12):
        p = tmp_path / f"img_{i:02d}.png"
        _make_test_image(str(p), size=(800 + i, 600))
        db.insert(
            sha256=db.sha256_of_file(str(p)), filename=p.name, phash="",
            model_id="fake-vl", question="", result_id=f"res_{i:02d}", source_value=str(p),
            peek=str(i), text=str(i), tags=[], result_json={},
        )
        paths.append(str(p))

    async def _run():
        return await vision_read.read(db, paths=paths)

    result = asyncio.run(_run())
    assert result["cached"] == 12
    args = result["next_call"]["arguments"]
    assert len(args["result_ids"]) == 10
    assert result["unlisted_count"] == 2
    assert "共 12 条" in result["proposal"]
    db.close()


def test_batch_hint_consistent_with_result_ids(tmp_path):
    """hint 范围与 result_ids 同序同源（传入序）：起点=result_ids[0]、终点=result_ids[-1]——
    不再是并发完成序的竞态产物（同一批图两次运行 hint 必须相同）。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    ids = []
    paths = []
    for i in range(5):
        p = tmp_path / f"img_{i:02d}.png"
        _make_test_image(str(p), size=(800 + i, 600))
        rid = f"res_{i:02d}"
        db.insert(
            sha256=db.sha256_of_file(str(p)), filename=p.name, phash="",
            model_id="fake-vl", question="", result_id=rid, source_value=str(p),
            peek=str(i), text=str(i), tags=[], result_json={},
        )
        ids.append(rid)
        paths.append(str(p))

    async def _run():
        return await vision_read.read(db, paths=paths)

    r1 = asyncio.run(_run())
    r2 = asyncio.run(_run())  # 同一批全命中跑两遍
    for r in (r1, r2):
        assert r["next_call"]["arguments"] == {"result_ids": ids}
        assert f"范围: {ids[0]} ~ {ids[-1]}" in r["result_id_hint"]  # 传入序
        assert "共 5 条" in r["result_id_hint"]
    assert r1["result_id_hint"] == r2["result_id_hint"]  # 确定性：两次必须相同
    db.close()


def test_result_ids_dedup_same_content(tmp_path):
    """相同内容的多个路径解析为同一记录（同 sha）：result_ids 去重保首现，
    去重后只剩一条 → 走 result_id 单查分支（重复 id 对查询无意义）。"""
    import shutil

    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    a = tmp_path / "a.png"
    _make_test_image(str(a))
    dup = tmp_path / "中文 空格 名.PNG"  # 同内容不同文件名（反馈实测场景）
    shutil.copy(str(a), str(dup))
    db.insert(
        sha256=db.sha256_of_file(str(a)), filename="a.png", phash="",
        model_id="fake-vl", question="", result_id="res_same", source_value=str(a),
        peek="同一图", text="同一", tags=[], result_json={},
    )

    async def _run():
        return await vision_read.read(db, paths=[str(a), str(dup)])

    result = asyncio.run(_run())
    assert result["cached"] == 2
    assert result["next_call"]["arguments"] == {"result_id": "res_same"}  # 去重后单条
    db.close()


def test_batch_all_cached_next_call_uses_result_ids(tmp_path):
    """批量全命中：next_call 用 result_ids 精确指向命中集合（顺序与传入一致），
    而非 recent=N 撞时间倒序的无关记录。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    paths = []
    ids = []
    for i, name in enumerate(("a.png", "b.png")):
        p = tmp_path / name
        _make_test_image(str(p), size=(800 + i, 600))
        rid = f"res_hit_{i}"
        db.insert(
            sha256=db.sha256_of_file(str(p)), filename=name, phash="",
            model_id="fake-vl", question="", result_id=rid, source_value=str(p),
            peek=name, text=name, tags=[], result_json={},
        )
        paths.append(str(p))
        ids.append(rid)

    async def _run():
        return await vision_read.read(db, paths=paths)

    result = asyncio.run(_run())
    assert result["cached"] == 2
    assert result["read"] == 0
    assert result["next_call"]["arguments"] == {"result_ids": ids}
    assert "命中结果" in result["result_id_hint"]  # 纯命中三态文案
    db.close()


def test_batch_mixed_next_call_result_ids_cover_all(tmp_path):
    """混合批量（1 命中 + 1 新读）：result_ids 覆盖全部传入图
    （命中给缓存 id、新读给新 id），顺序与传入一致。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    cached_img = tmp_path / "a_cached.png"
    _make_test_image(str(cached_img), size=(800, 600))
    db.insert(
        sha256=db.sha256_of_file(str(cached_img)), filename="a_cached.png", phash="",
        model_id="fake-vl", question="", result_id="res_hit", source_value=str(cached_img),
        peek="命中图", text="命中", tags=[], result_json={},
    )
    new_img = tmp_path / "b_new.png"
    _make_test_image(str(new_img), size=(810, 610))

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        return '{"peek": "新图", "text": "新", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            return await vision_read.read(db, paths=[str(cached_img), str(new_img)])
        finally:
            vision_read.vl_read_image = original_vl

    result = asyncio.run(_run())
    assert result["cached"] == 1
    assert result["read"] == 1
    args = result["next_call"]["arguments"]
    assert "result_ids" in args
    assert args["result_ids"][0] == "res_hit"  # 命中给缓存 id
    new_id = args["result_ids"][1]
    assert new_id != "res_hit" and db.get_by_result_id(new_id) is not None  # 新读给新 id 且已落库
    assert result["result_id_hint"].startswith("结果")  # 混合三态文案（非「新结果」）
    db.close()


def test_cached_hit_next_call_points_to_cached_record(tmp_path):
    """缓存命中的 next_call 必须指向命中的记录本身（result_id 精确查）——
    旧逻辑退化成 recent=1，指向库中最新记录（可能是完全无关的图），污染上下文。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    img = tmp_path / "target.png"
    _make_test_image(str(img))
    # 先插目标记录（待命中）
    db.insert(
        sha256=db.sha256_of_file(str(img)), filename="target.png", phash="",
        model_id="fake-vl", question="", result_id="res_target", source_value=str(img),
        peek="目标图", text="目标", tags=[], result_json={},
    )
    # 后插无关记录——recent=1 必然指向它（旧逻辑的错误落点）
    other = tmp_path / "z_other.png"
    _make_test_image(str(other), size=(830, 620))
    db.insert(
        sha256=db.sha256_of_file(str(other)), filename="z_other.png", phash="",
        model_id="fake-vl", question="", result_id="res_other", source_value=str(other),
        peek="无关图", text="无关", tags=[], result_json={},
    )

    async def _run():
        return await vision_read.read(db, paths=[str(img)])

    result = asyncio.run(_run())
    assert result["cached"] == 1
    assert result["read"] == 0
    assert result["next_call"]["arguments"] == {"result_id": "res_target"}
    assert "res_target" in result["result_id_hint"]
    assert "命中结果" in result["result_id_hint"]  # 纯命中场景的文案不是「新结果」
    db.close()


def test_phash_hit_next_call_points_to_similar_record(tmp_path):
    """phash 近似命中同样登记 result_id：next_call 指向相似图的记录。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    orig_path = str(tmp_path / "orig.png")
    img = _make_test_image(orig_path)
    import imagehash
    from PIL import Image

    with Image.open(orig_path) as _im:
        phash = str(imagehash.phash(_im))
    db.insert(
        sha256=db.sha256_of_file(orig_path), filename="orig.png",
        phash=phash, model_id="fake-vl", question="", result_id="res_orig",
        source_value=orig_path, peek="原图", text="原图描述", tags=[], result_json={},
    )
    # 缩尺变体（sha256 不同，phash 近似）
    img.resize((400, 300)).save(tmp_path / "resized.png")

    async def _run():
        return await vision_read.read(db, paths=[str(tmp_path / "resized.png")])

    result = asyncio.run(_run())
    assert result["cached"] == 1
    assert result.get("cached_via_phash") == 1
    assert result["next_call"]["arguments"] == {"result_id": "res_orig"}
    db.close()


def test_image_too_large_falls_back_to_smaller_edge_provider(tmp_path):
    """混合链 [非DS主, DS备]：主档位（2048）超限不掐死 fallback——
    DS 备用的 1024 档可能通过（独立审查发现：原「重试/降级都一样」注释不成立）。"""
    from tools import vision_read
    from tools._vl_client import ImageTooLargeError

    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    tool_config.set_config({"vl_provider_ids": "p-big, p-ds", "vl_model": {}}, str(tmp_path))
    tool_config.set_providers([
        {"id": "p-big", "key": ["k1"], "api_base": "http://x", "model": "gpt-4o"},
        {"id": "p-ds", "key": ["k2"], "api_base": "http://x", "model": "deepseek-flash"},
    ])
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    seen = []
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        model = (vl_config or {}).get("model", "")
        seen.append(model)
        if model == "gpt-4o":
            raise ImageTooLargeError("2048 档超限")
        return '{"peek": "DS 1024 档通过", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["ok"] is True
            assert result["read"] == 1
            # gpt-4o 只调一次（同档位重试无意义），降级 deepseek-flash 成功
            assert seen == ["gpt-4o", "deepseek-flash"]
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_max_batch_boundary_allows_exact_limit(tmp_path):
    """len == max_batch 恰好放行（边界：超限才拦截）。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    tool_config.set_config({**tool_config.get_config(), "max_batch": 2}, str(tmp_path))
    _make_test_image(str(tmp_path / "x1.png"), size=(210, 210))
    _make_test_image(str(tmp_path / "x2.png"), size=(220, 220))

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        return '{"peek": "p", "text": "t", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[str(tmp_path / "x1.png"), str(tmp_path / "x2.png")])
            assert result["ok"] is True
            assert result["read"] == 2
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_partial_status_when_some_images_fail(tmp_path):
    """部分成功部分失败 → status=partial，read/failed 计数与 failed_paths 正确。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    ok_img = str(tmp_path / "ok.png")
    bad_img = str(tmp_path / "bad.png")
    _make_test_image(ok_img)
    _make_test_image(bad_img, size=(820, 610))

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        if path == bad_img:
            raise ConnectionError("模拟失败")
        return '{"peek": "预览", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[ok_img, bad_img])
            assert result["ok"] is True
            assert result["status"] == "partial"
            assert result["read"] == 1
            assert result["failed"] == 1
            assert len(result["failed_paths"]) == 1
            assert bad_img in result["failed_paths"][0]
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_image_too_large_not_retried(tmp_path):
    """ImageTooLargeError：不重试不降级，直接计为失败（压缩后仍超限，重试结果不变）。"""
    from tools import vision_read
    from tools._vl_client import ImageTooLargeError

    db, db_path = _setup_fake_vl(tmp_path)
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    calls = []
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        calls.append(path)
        raise ImageTooLargeError("压缩后仍超过 20MB")

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["ok"] is False
            assert len(calls) == 1  # 不重试
            assert db.get_recent(limit=10) == []
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_max_batch_guard(tmp_path):
    """超过 max_batch 上限：直接报错不调 VL（账单保险丝，防误传大目录失控计费）。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)
    tool_config.set_config({**tool_config.get_config(), "max_batch": 3}, str(tmp_path))
    for i in range(4):
        _make_test_image(str(tmp_path / f"batch{i}.png"), size=(210 + i, 210))

    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        raise AssertionError("超限时不应调用 VL")

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[str(tmp_path)])
            assert result["ok"] is False
            assert "max_batch" in result["proposal"]
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_retry_reuses_encoded_image(tmp_path):
    """重试/降级链上同一压缩档位的编码结果复用：两次调用收到同一个 image_url，
    同一张图的压缩+base64 只做一次。"""
    from tools import vision_read

    db, db_path = _setup_fake_vl(tmp_path)  # max_retries 默认 2
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    received_urls = []
    attempts = {"n": 0}
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        received_urls.append(image_url)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("第一次失败")
        return '{"peek": "预览", "text": "正文", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["read"] == 1
            assert len(received_urls) == 2
            assert received_urls[0] is not None
            assert received_urls[0].startswith("data:image/")
            assert received_urls[0] == received_urls[1]  # 编码复用，不重复压缩
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_truncated_output_no_same_model_retry(tmp_path):
    """OutputTruncatedError（放大重试后仍截断）：同一 provider 不再重复重试
    （额度已在客户端内部放大过），链尽后计入 failed。"""
    from tools import vision_read
    from tools._vl_client import OutputTruncatedError

    db, db_path = _setup_fake_vl(tmp_path)  # max_retries 默认 2
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    calls = []
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        calls.append(path)
        raise OutputTruncatedError("模型 x 输出在 max_tokens=16384 下仍被截断")

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["ok"] is False
            assert len(calls) == 1  # 不重试同一模型
            assert db.get_recent(limit=10) == []  # 截断内容不落库
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())


def test_truncated_first_provider_falls_back_to_second(tmp_path):
    """首选思考型模型截断 → 降级到次选成功（截断是模型相关的，降级有意义）。"""
    from tools import vision_read
    from tools._vl_client import OutputTruncatedError

    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    tool_config.set_config(
        {
            "vl_provider_ids": "p-thinker, p-plain",
            "vl_model": {},
        },
        str(tmp_path),
    )
    tool_config.set_providers([
        {"id": "p-thinker", "key": ["k1"], "api_base": "http://x", "model": "deepseek-flash"},
        {"id": "p-plain", "key": ["k2"], "api_base": "http://x", "model": "gpt-4o"},
    ])
    img_path = str(tmp_path / "doc.png")
    _make_test_image(img_path)

    seen_models = []
    original_vl = vision_read.vl_read_image

    async def fake_vl(path, prompt, *, max_tokens=4096, client=None, vl_config=None, json_mode=False, image_url=None):
        model = (vl_config or {}).get("model", "")
        seen_models.append(model)
        if model == "deepseek-flash":
            raise OutputTruncatedError("仍被截断")
        return '{"peek": "次选模型的答案", "text": "完整", "tags": []}'

    async def _run():
        vision_read.vl_read_image = fake_vl
        try:
            result = await vision_read.read(db, paths=[img_path])
            assert result["ok"] is True
            assert result["read"] == 1
            # deepseek-flash 只调一次（不重试），随后降级 gpt-4o 成功
            assert seen_models == ["deepseek-flash", "gpt-4o"]
            rid = result["next_call"]["arguments"]["result_id"]
            assert db.get_by_result_id(rid)["peek"] == "次选模型的答案"
        finally:
            vision_read.vl_read_image = original_vl
            db.close()

    asyncio.run(_run())
