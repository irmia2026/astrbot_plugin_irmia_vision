"""
测试 VL 客户端的模型适配逻辑（v4fve 触发优化）
"""

import os
import tempfile

from PIL import Image

from tools._vl_client import (
    TARGET_LONG_EDGE,
    V4FVE_LONG_EDGE,
    encode_image,
    is_v4fve,
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
