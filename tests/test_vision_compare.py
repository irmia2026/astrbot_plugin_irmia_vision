"""
测试 vision_compare 多图对比：组指纹缓存、内容去重、追问注入、护栏
"""

import asyncio
import json
import os
import shutil
import tempfile

from PIL import Image

from tools._store import create_store
from tools import config as tool_config
from tools.vision_compare import _group_sha256


def _setup_fake_vl(tmp_path):
    """配置一个带假 key 的 vl_model，返回已打开的 db。"""
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
    return db


def _make_image(path, color):
    Image.new("RGB", (64, 64), color).save(path)


def _fake_vl_factory(calls, payload='{"peek": "红图更暖", "text": "图1为纯红色，图2为纯蓝色", "tags": ["纯色"]}'):
    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        calls.append({"paths": list(paths), "labels": labels, "prompt": prompt})
        return payload

    return fake_vl


def test_group_sha256_order_independent():
    a = "a" * 64
    b = "b" * 64
    c = "c" * 64
    assert _group_sha256([a, b]) == _group_sha256([b, a])
    assert _group_sha256([a, b]) != _group_sha256([a, c])
    assert _group_sha256([a, b]) != _group_sha256([a, b, c])
    assert _group_sha256([a, b]).startswith("grp_")


def test_compare_requires_two_images(tmp_path):
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)

    async def _run():
        _make_image(str(tmp_path / "one.png"), (255, 0, 0))
        r1 = await vision_compare.compare(db, paths=[str(tmp_path / "one.png")])
        assert r1["ok"] is False
        assert "至少需要 2 张" in r1["proposal"]
        r2 = await vision_compare.compare(db, paths=[str(tmp_path / "nope")])
        assert r2["ok"] is False

    asyncio.run(_run())
    db.close()


def test_compare_dedupes_same_content(tmp_path):
    """同一内容重复传入被去重；去重后不足 2 张则明确报错。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    shutil.copy(str(tmp_path / "a.png"), str(tmp_path / "a_copy.png"))  # 同内容不同文件名

    async def _run():
        result = await vision_compare.compare(
            db, paths=[str(tmp_path / "a.png"), str(tmp_path / "a_copy.png")]
        )
        assert result["ok"] is False
        assert "去重" in result["proposal"]

    asyncio.run(_run())
    db.close()


def test_compare_success_and_group_cache(tmp_path):
    """首次对比调一次 VL（labels 绑定图序与文件名），换顺序再调 → 组缓存命中。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "red.png"), (255, 0, 0))
    _make_image(str(tmp_path / "blue.png"), (0, 0, 255))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(calls)
        try:
            r1 = await vision_compare.compare(
                db, paths=[str(tmp_path / "red.png"), str(tmp_path / "blue.png")]
            )
            assert r1["ok"] is True
            assert r1["status"] == "success"
            assert r1["cached"] is False
            assert r1["images"] == 2
            assert r1["result_id"].startswith("cmp_")
            assert r1["peek"] == "红图更暖"
            assert r1["text"]  # 结论直接返回
            assert len(calls) == 1
            # labels 绑定图序与文件名（_collect_image_paths 会排序，不断言顺序），
            # prompt 含 json 字样（DeepSeek JSON Output 要求）
            assert calls[0]["labels"][0].startswith("图1")
            assert calls[0]["labels"][1].startswith("图2")
            joined_labels = " ".join(calls[0]["labels"])
            assert "red.png" in joined_labels and "blue.png" in joined_labels
            assert "json" in calls[0]["prompt"].lower()
            # 落库：组指纹 + 成员信息
            row = db.get_by_result_id(r1["result_id"])
            assert row["sha256"].startswith("grp_")
            assert row["phash"] == ""
            rj = json.loads(row["result_json"])
            assert rj["kind"] == "compare"
            assert len(rj["members"]) == 2
            # 换顺序再调 → 组缓存命中，不再调 VL
            r2 = await vision_compare.compare(
                db, paths=[str(tmp_path / "blue.png"), str(tmp_path / "red.png")]
            )
            assert r2["ok"] is True
            assert r2["cached"] is True
            assert r2["status"] == "cached"
            assert r2["result_id"] == r1["result_id"]
            assert r2["peek"] == "红图更暖"
            assert len(calls) == 1
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_question_and_force_reread_bypass_cache(tmp_path):
    """同组图片换 question 重新对比；force_reread 忽略组缓存。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(
            calls, '{"peek": "结论", "text": "依据", "tags": []}'
        )
        try:
            paths = [str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            await vision_compare.compare(db, paths=paths)
            r2 = await vision_compare.compare(db, paths=paths)  # 同问题（空）→ 命中
            assert r2["cached"] is True
            r3 = await vision_compare.compare(db, paths=paths, question="哪张更亮？")
            assert r3["cached"] is False  # 新问题 → 重读
            r4 = await vision_compare.compare(db, paths=paths, force_reread=True)
            assert r4["cached"] is False  # 强制 → 重读
            assert len(calls) == 3
            assert "哪张更亮" in calls[1]["prompt"]
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_follow_up_same_group(tmp_path):
    """追问：previous_result_id 指向同组记录时注入上文，且在 JSON 格式要求之前。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(
            calls, '{"peek": "回答", "text": "细节", "tags": []}'
        )
        try:
            paths = [str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            r1 = await vision_compare.compare(db, paths=paths)
            r2 = await vision_compare.compare(
                db, paths=paths, question="哪张更适合做告警色？",
                previous_result_id=r1["result_id"],
            )
            assert r2["cached"] is False  # 追问不走缓存
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())
    p = calls[-1]["prompt"]
    assert "之前对这组图片的对比理解" in p
    assert p.index("之前对这组图片的对比理解") < p.index("输出 JSON")


def test_compare_follow_up_ignores_different_group(tmp_path):
    """previous_result_id 指向另一组图片时，不注入上文（防跨组污染）。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))
    _make_image(str(tmp_path / "c.png"), (0, 0, 255))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(
            calls, '{"peek": "回答", "text": "细节", "tags": []}'
        )
        try:
            r1 = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            # 用 a+c 组成的新组 + 指向 a+b 组的 previous_result_id
            await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "c.png")],
                question="对比", previous_result_id=r1["result_id"],
            )
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())
    assert "之前对这组图片的对比理解" not in calls[-1]["prompt"]


def test_compare_preserves_input_order(tmp_path):
    """图1/图2 编号按传入顺序，不被文件名字典序调换（before/after 静默反向结论的修复）。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "zz_first.png"), (255, 0, 0))
    _make_image(str(tmp_path / "aa_second.png"), (0, 0, 255))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(calls)
        try:
            # 传入顺序与文件名字典序相反
            return await vision_compare.compare(
                db, paths=[str(tmp_path / "zz_first.png"), str(tmp_path / "aa_second.png")]
            )
        finally:
            vision_compare.vl_read_images = original
            db.close()

    result = asyncio.run(_run())
    assert result["ok"] is True
    labels = calls[0]["labels"]
    assert "zz_first.png" in labels[0]  # 图1 = 传入第一张
    assert "aa_second.png" in labels[1]
    assert result["filenames"] == ["zz_first.png", "aa_second.png"]  # 返回同样保序


def test_compare_decode_error_fast_fail(tmp_path):
    """坏图：直接报「无法解码」，不走降级链、不归因模型、不调 VL。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    (tmp_path / "broken.png").write_bytes(b"")

    calls = []
    original = vision_compare.vl_read_images

    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        calls.append(1)
        return '{"peek": "x", "text": "y", "tags": []}'

    async def _run():
        vision_compare.vl_read_images = fake_vl
        try:
            return await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "broken.png")]
            )
        finally:
            vision_compare.vl_read_images = original
            db.close()

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "无法解码" in result["proposal"]
    assert len(calls) == 0  # VL 未被调用


def test_compare_missing_paths_reported(tmp_path):
    """部分传入路径未找到：结果带 missing_paths 回显（不静默丢图导致结论基于不完整集合）。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    calls = []
    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory(calls)
        try:
            return await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "ghost.png"), str(tmp_path / "b.png")]
            )
        finally:
            vision_compare.vl_read_images = original
            db.close()

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["images"] == 2
    assert result["missing_paths"] == [str(tmp_path / "ghost.png")]


def test_compare_max_images_guard(tmp_path):
    """超过单次对比上限时明确报错，不调 VL。"""
    from tools import vision_compare
    from tools._vl_client import MAX_COMPARE_IMAGES

    db = _setup_fake_vl(tmp_path)
    paths = []
    for i in range(MAX_COMPARE_IMAGES + 1):
        p = str(tmp_path / f"img_{i:02d}.png")
        _make_image(p, (i % 256, 0, 0))
        paths.append(p)

    async def _run():
        result = await vision_compare.compare(db, paths=paths)
        assert result["ok"] is False
        assert "上限" in result["proposal"]

    asyncio.run(_run())
    db.close()


def test_compare_empty_content_not_stored(tmp_path):
    """空内容 JSON 不落库（与 vision_read 同一防护：空记录会永久占用组缓存键）。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    original = vision_compare.vl_read_images

    async def _run():
        vision_compare.vl_read_images = _fake_vl_factory([], '{"tags": ["抽风"]}')
        try:
            result = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["ok"] is False
            assert "结构化解析后为空" in result["proposal"]
            assert db.get_recent(limit=10) == []
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_missing_key_with_cache_still_hits(tmp_path):
    """未配置 VL 模型时：组缓存仍可命中（与 vision_read 一致），未命中则报配置错误。"""
    from tools import vision_compare

    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    tool_config.set_config(
        {
            "vl_provider_ids": "",
            "vl_model": {
                "provider": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "",  # 无 key
                "model": "fake-vl",
            },
        },
        str(tmp_path),
    )
    tool_config.set_providers([])

    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    async def _run():
        paths = [str(tmp_path / "a.png"), str(tmp_path / "b.png")]
        # 未命中时 → 报配置错误
        r1 = await vision_compare.compare(db, paths=paths)
        assert r1["ok"] is False
        assert "api_key" in r1["proposal"] or "VL 模型" in r1["proposal"]
        # 手工落库一条同组记录 → 无 key 也能命中
        sha_a = db.sha256_of_file(paths[0])
        sha_b = db.sha256_of_file(paths[1])
        db.insert(
            sha256=_group_sha256([sha_a, sha_b]),
            filename="[对比2图] a.png + b.png",
            phash="",
            model_id="fake-vl",
            question="",
            result_id="cmp_preset",
            source_value=json.dumps(paths),
            peek="预设结论",
            text="预设正文",
            tags=[],
            result_json={"kind": "compare", "members": []},
        )
        r2 = await vision_compare.compare(db, paths=paths)
        assert r2["ok"] is True
        assert r2["cached"] is True
        assert r2["peek"] == "预设结论"

    asyncio.run(_run())
    db.close()


def test_compare_retry_reuses_encoded_images(tmp_path):
    """重试时复用预编码 image_urls（多图场景重复压缩的浪费被图片数放大）。"""
    from tools import vision_compare

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    received = []
    attempts = {"n": 0}
    original = vision_compare.vl_read_images

    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        received.append(image_urls)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("第一次失败")
        return '{"peek": "结论", "text": "依据", "tags": []}'

    async def _run():
        vision_compare.vl_read_images = fake_vl
        try:
            result = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["ok"] is True
            assert len(received) == 2
            assert received[0] is not None and len(received[0]) == 2
            assert received[0] == received[1]  # 编码复用
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_image_too_large_returns_proposal(tmp_path):
    """ImageTooLargeError：同 provider 不重试；链尽后失败提案携带超限原因。"""
    from tools import vision_compare
    from tools._vl_client import ImageTooLargeError

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    calls = []
    original = vision_compare.vl_read_images

    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        calls.append(1)
        raise ImageTooLargeError("多图内联总量超预算")

    async def _run():
        vision_compare.vl_read_images = fake_vl
        try:
            result = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["ok"] is False
            assert "超预算" in result["proposal"]  # 超限原因透传进失败提案
            assert len(calls) == 1  # 同 provider 不重试
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_image_too_large_falls_back_to_smaller_edge(tmp_path):
    """混合链 [非DS主, DS备]：2048 档破 40MB 预算时，降级到 DS 的 1024 档可通过
    （encoded 按档位重编码机制支撑；独立审查发现的场景）。"""
    import tempfile

    from tools import vision_compare
    from tools._vl_client import ImageTooLargeError

    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)
    db = create_store(db_path)
    tool_config.set_config({"vl_provider_ids": "p-big, p-ds", "vl_model": {}}, str(tmp_path))
    tool_config.set_providers([
        {"id": "p-big", "key": ["k1"], "api_base": "http://x", "model": "gpt-4o"},
        {"id": "p-ds", "key": ["k2"], "api_base": "http://x", "model": "deepseek-flash"},
    ])
    # 长边 > 2048：两个压缩档（2048/1024）都会实际缩放，编码结果必然不同
    Image.new("RGB", (2100, 900), (255, 0, 0)).save(str(tmp_path / "a.png"))
    Image.new("RGB", (2100, 900), (0, 255, 0)).save(str(tmp_path / "b.png"))

    seen = []
    received_urls = []
    original = vision_compare.vl_read_images

    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        model = (vl_config or {}).get("model", "")
        seen.append(model)
        received_urls.append(image_urls)
        if model == "gpt-4o":
            raise ImageTooLargeError("2048 档内联总量超预算")
        return '{"peek": "1024 档通过", "text": "正文", "tags": []}'

    async def _run():
        vision_compare.vl_read_images = fake_vl
        try:
            result = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["ok"] is True
            assert result["peek"] == "1024 档通过"
            assert seen == ["gpt-4o", "deepseek-flash"]
            # 两次调用收到不同档位的编码结果（1024 档重编码，非复用 2048 档）
            assert received_urls[0] != received_urls[1]
            assert len(received_urls[1]) == 2
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())


def test_compare_truncated_output_no_same_model_retry(tmp_path):
    """OutputTruncatedError：同一模型不重试，链尽后报失败且不落库。"""
    from tools import vision_compare
    from tools._vl_client import OutputTruncatedError

    db = _setup_fake_vl(tmp_path)
    _make_image(str(tmp_path / "a.png"), (255, 0, 0))
    _make_image(str(tmp_path / "b.png"), (0, 255, 0))

    calls = []
    original = vision_compare.vl_read_images

    async def fake_vl(paths, prompt, *, labels=None, max_tokens=8192, client=None, vl_config=None, json_mode=False, image_urls=None):
        calls.append(1)
        raise OutputTruncatedError("仍被截断")

    async def _run():
        vision_compare.vl_read_images = fake_vl
        try:
            result = await vision_compare.compare(
                db, paths=[str(tmp_path / "a.png"), str(tmp_path / "b.png")]
            )
            assert result["ok"] is False
            assert "截断" in result["proposal"]
            assert len(calls) == 1  # 不重试同一模型
            assert db.get_recent(limit=10) == []
        finally:
            vision_compare.vl_read_images = original
            db.close()

    asyncio.run(_run())
