"""
测试 vision_read 路径收集与缓存命中
"""

import os
import tempfile

from tools._cache import VisionCache
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
    assert parsed["summary"] == "第一行摘要"
    assert parsed["text"] == raw
    assert parsed["tags"] == []


def test_vision_read_hits_cache(tmp_path):
    """模拟读图命中缓存，不实际调用 VL。"""
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)

    img = tmp_path / "cached.png"
    img.write_bytes(b"fake image")

    db = VisionCache(db_path)
    tool_config.set_config(
        {
            "vl_model": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o",
            }
        },
        str(tmp_path),
    )

    # 预插缓存
    db.insert(
        sha256=db.sha256_of_file(str(img)),
        filename="cached.png",
        phash="",
        model_id="gpt-4o",
        question="",
        result_id="res_cached",
        source_value=str(img),
        summary="预设摘要",
        text="预设文字",
        tags=[],
        result_json={},
    )

    from tools import vision_read

    result = vision_read.read(db, paths=[str(img)])
    assert result["ok"] is True
    assert result["total"] == 1
    assert result["cached"] == 1
    assert result["read"] == 0


def test_vision_read_missing_key_for_new_image(tmp_path):
    """未配置 api_key 时读取新图应友好提示。"""
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=tmp_path)
    os.close(fd)

    img = tmp_path / "new.png"
    img.write_bytes(b"fake image")

    db = VisionCache(db_path)
    tool_config.set_config(
        {
            "vl_model": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o",
            }
        },
        str(tmp_path),
    )

    from tools import vision_read

    result = vision_read.read(db, paths=[str(img)])
    assert result["ok"] is False
    assert "api_key" in result["proposal"]
