"""
测试 VL 客户端的模型适配逻辑（v4fve 触发优化）
"""

import os
import tempfile

from PIL import Image

import pytest

from tools._vl_client import (
    TARGET_LONG_EDGE,
    V4FVE_LONG_EDGE,
    ImageTooLargeError,
    _compress_image,
    effective_target_edge,
    encode_image,
    is_v4fve,
    normalize_detail,
    target_edge_for_model,
)


def test_is_v4fve():
    assert is_v4fve("deepseek-v4-flash-vision-exp")
    assert is_v4fve("DeepSeek-V4-Flash-Vision-Exp")  # 大小写不敏感
    assert is_v4fve("v4fve")
    assert not is_v4fve("gpt-4o")
    assert not is_v4fve("deepseek-chat")
    assert not is_v4fve("")
    assert not is_v4fve(None)


def test_target_edge_for_model():
    assert target_edge_for_model("deepseek-v4-flash-vision-exp") == V4FVE_LONG_EDGE
    assert target_edge_for_model("gpt-4o") == TARGET_LONG_EDGE


def test_normalize_detail():
    assert normalize_detail("low") == "low"
    assert normalize_detail("  Auto  ") == "auto"  # strip + 大小写归一
    assert normalize_detail("original") == "original"
    assert normalize_detail("high") == "high"
    assert normalize_detail("") == "auto"
    assert normalize_detail(None) == "auto"
    assert normalize_detail("高清") == "auto"  # 非法值回退，不原样发给 API
    assert normalize_detail("ORIGINAL") == "original"


def test_effective_target_edge():
    # original = 保留原图，跳过客户端降采样（任何模型）
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "original") is None
    assert effective_target_edge("gpt-4o", "original") is None
    # v4fve 非 original → 1024 档
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "auto") == V4FVE_LONG_EDGE
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "low") == V4FVE_LONG_EDGE
    # 其他模型 → 默认 2048
    assert effective_target_edge("gpt-4o", "auto") == TARGET_LONG_EDGE


def test_encode_image_v4fve_smaller_payload():
    """v4fve 档位（1024 长边）的 payload 应明显小于默认档位（2048）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "big.jpg")
        # 生成 3000×2000 随机噪声图（保证压缩后体积差异可测）
        img = Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3))
        img.save(img_path, quality=95)

        import base64

        default_url = encode_image(img_path, TARGET_LONG_EDGE)
        v4fve_url = encode_image(img_path, V4FVE_LONG_EDGE)

        default_bytes = len(base64.b64decode(default_url.split(",", 1)[1]))
        v4fve_bytes = len(base64.b64decode(v4fve_url.split(",", 1)[1]))

        assert v4fve_bytes < default_bytes * 0.6  # 至少省 40%（典型场景 ~75%）


def test_compress_over_limit_raises_too_large():
    """压缩后仍超 20MB 时抛 ImageTooLargeError（调用方不重试不降级的依据）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "huge.png")
        # 4096×4096 纯噪声 PNG 不可压缩，payload > 20MB
        Image.frombytes("RGB", (4096, 4096), os.urandom(4096 * 4096 * 3)).save(img_path)
        with pytest.raises(ImageTooLargeError):
            _compress_image(img_path, target_long_edge=4096)
        # ImageTooLargeError 是 ValueError 子类，兼容旧调用方的 except ValueError
        assert issubclass(ImageTooLargeError, ValueError)


def test_read_image_response_format_only_v4fve():
    """json_mode=True 时仅 v4fve 附加官方 response_format，其他模型不带。"""
    import asyncio

    from tools._vl_client import read_image

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _Client:
        def __init__(self):
            self.payloads = []

        async def post(self, url, headers=None, json=None):
            self.payloads.append(json)
            return _Resp()

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "a.png")
        Image.new("RGB", (10, 10), (255, 0, 0)).save(img_path)

        async def _run():
            c1 = _Client()
            await read_image(
                img_path, "p", client=c1,
                vl_config={"api_key": "k", "model": "deepseek-v4-flash-vision-exp", "base_url": "http://x"},
                json_mode=True,
            )
            assert c1.payloads[0].get("response_format") == {"type": "json_object"}

            c2 = _Client()
            await read_image(
                img_path, "p", client=c2,
                vl_config={"api_key": "k", "model": "gpt-4o", "base_url": "http://x"},
                json_mode=True,
            )
            assert "response_format" not in c2.payloads[0]

            c3 = _Client()
            await read_image(
                img_path, "p", client=c3,
                vl_config={"api_key": "k", "model": "deepseek-v4-flash-vision-exp", "base_url": "http://x"},
                json_mode=False,
            )
            assert "response_format" not in c3.payloads[0]

        asyncio.run(_run())
