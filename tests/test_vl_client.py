"""
测试 VL 客户端的模型适配逻辑（v4fve 触发优化）
"""

import os
import tempfile

from PIL import Image

import pytest

from tools._vl_client import (
    LOW_DETAIL_LONG_EDGE,
    TARGET_LONG_EDGE,
    V4FVE_LONG_EDGE,
    ImageTooLargeError,
    _compress_image,
    effective_target_edge,
    encode_image,
    is_v4fve,
    normalize_detail,
    normalize_reasoning_effort,
    target_edge_for_model,
)


def test_is_v4fve():
    assert is_v4fve("deepseek-v4-flash-vision-exp")
    assert is_v4fve("DeepSeek-V4-Flash-Vision-Exp")  # 大小写不敏感
    assert is_v4fve("v4fve")
    assert is_v4fve("deepseek-flash")  # 现行模型名（旧名已下线由其承接）
    assert not is_v4fve("gpt-4o")
    assert not is_v4fve("deepseek-chat")
    assert not is_v4fve("deepseek-v4-pro")  # 纯文本模型不走视觉优化档
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
    # DeepSeek 系：high 官方等价 original（保留原图）→ 同样不降采样
    assert effective_target_edge("deepseek-flash", "high") is None
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "high") is None
    # OpenAI 的 high 是精细档（自带缩放），客户端 2048 对齐
    assert effective_target_edge("gpt-4o", "high") == TARGET_LONG_EDGE
    # detail=low：DeepSeek/OpenAI 服务端都缩到 512×512，客户端对齐（更大无收益只费带宽）
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "low") == LOW_DETAIL_LONG_EDGE
    assert effective_target_edge("gpt-4o", "low") == LOW_DETAIL_LONG_EDGE
    # v4fve auto → 1024 档
    assert effective_target_edge("deepseek-v4-flash-vision-exp", "auto") == V4FVE_LONG_EDGE
    # 其他模型 → 默认 2048
    assert effective_target_edge("gpt-4o", "auto") == TARGET_LONG_EDGE


def test_normalize_reasoning_effort():
    assert normalize_reasoning_effort("low") == "low"
    assert normalize_reasoning_effort("none") == "none"
    assert normalize_reasoning_effort("max") == "max"
    assert normalize_reasoning_effort(" High ") == "high"  # strip + 大小写归一
    assert normalize_reasoning_effort("") == "low"
    assert normalize_reasoning_effort(None) == "low"
    # DeepSeek 官方兼容映射：minimal→low，medium/xhigh→high，ultra→max
    assert normalize_reasoning_effort("minimal") == "low"
    assert normalize_reasoning_effort("medium") == "high"
    assert normalize_reasoning_effort("xhigh") == "high"
    assert normalize_reasoning_effort("ultra") == "max"
    # 非法值回退 low，不原样发给 API
    assert normalize_reasoning_effort("turbo") == "low"


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
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

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


# ---------- finish_reason 截断处理（DeepSeek 思考模式） ----------

def _resp(content="", reasoning="", finish="stop"):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            msg = {"content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            return {"choices": [{"message": msg, "finish_reason": finish}]}

    return _R()


class _SeqClient:
    """按序返回预设响应的假 client，记录每次请求的 payload。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def post(self, url, headers=None, json=None):
        self.payloads.append(json)
        return self.responses.pop(0)


def _run_read(img_path, client, model, max_tokens=4096, image_url=None):
    import asyncio

    from tools._vl_client import read_image

    async def _call():
        return await read_image(
            img_path, "p", max_tokens=max_tokens, client=client,
            vl_config={"api_key": "k", "model": model, "base_url": "http://x"},
            json_mode=True, image_url=image_url,
        )

    return asyncio.run(_call())


def test_read_image_with_prepared_url_skips_encoding(tmp_path):
    """传入预编码 image_url 时不碰磁盘（重试/降级链复用编码结果的接口契约）。"""
    data_url = "data:image/png;base64,iVBORw0KGgo="
    c = _SeqClient([_resp(content="ok")])
    # 路径不存在：若走编码必抛错；成功返回证明跳过了压缩编码
    out = _run_read(str(tmp_path / "nonexistent.png"), c, "gpt-4o", image_url=data_url)
    assert out == "ok"
    blocks = c.payloads[0]["messages"][0]["content"]
    assert blocks[1]["image_url"]["url"] == data_url


def test_read_images_with_prepared_urls_skips_encoding(tmp_path):
    """read_images 传入预编码 image_urls 时不碰磁盘；预算检查对传入 urls 同样生效。"""
    import asyncio

    from tools._vl_client import ImageTooLargeError, MAX_INLINE_IMAGES_B64, read_images

    async def _run():
        c = _SeqClient([_resp(content="ok")])
        out = await read_images(
            [str(tmp_path / "no1.png"), str(tmp_path / "no2.png")], "p",
            labels=["图1", "图2"], client=c,
            vl_config={"api_key": "k", "model": "gpt-4o", "base_url": "http://x"},
            image_urls=["data:image/png;base64,AAA=", "data:image/png;base64,BBB="],
        )
        assert out == "ok"
        blocks = c.payloads[0]["messages"][0]["content"]
        # prompt + (label+image)×2
        assert len(blocks) == 5
        assert blocks[1] == {"type": "text", "text": "图1"}
        assert blocks[2]["image_url"]["url"] == "data:image/png;base64,AAA="

        # 预算对传入 urls 生效：单个 url 超总量预算即抛 ImageTooLargeError
        huge = "data:image/png;base64," + "A" * MAX_INLINE_IMAGES_B64
        with pytest.raises(ImageTooLargeError):
            await read_images(
                ["x", "y"], "p", client=_SeqClient([]),
                vl_config={"api_key": "k", "model": "gpt-4o", "base_url": "http://x"},
                image_urls=[huge, "data:image/png;base64,CCC="],
            )

    asyncio.run(_run())


def test_v4fve_min_max_tokens_baseline(tmp_path):
    """思考型模型（思维链与答案共享额度）的 max_tokens 基线抬到 8192；其他模型不变。"""
    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    c1 = _SeqClient([_resp(content="ok")])
    _run_read(str(img), c1, "deepseek-flash", max_tokens=4096)
    assert c1.payloads[0]["max_tokens"] == 8192

    c2 = _SeqClient([_resp(content="ok")])
    _run_read(str(img), c2, "deepseek-v4-flash-vision-exp", max_tokens=4096)
    assert c2.payloads[0]["max_tokens"] == 8192

    c3 = _SeqClient([_resp(content="ok")])
    _run_read(str(img), c3, "gpt-4o", max_tokens=4096)
    assert c3.payloads[0]["max_tokens"] == 4096


def test_length_retry_escalates_and_succeeds(tmp_path):
    """finish_reason=length（含 content 非空的半截 JSON）→ 放大额度重试一次并返回完整结果。"""
    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    c = _SeqClient([
        _resp(content='{"peek": "半截', finish="length"),
        _resp(content='{"peek": "完整", "text": "全"}', finish="stop"),
    ])
    out = _run_read(str(img), c, "gpt-4o", max_tokens=4096)
    assert out == '{"peek": "完整", "text": "全"}'
    assert len(c.payloads) == 2
    assert c.payloads[0]["max_tokens"] == 4096
    assert c.payloads[1]["max_tokens"] == 8192  # 2 倍放大


def test_length_empty_content_does_not_return_reasoning(tmp_path):
    """content 空 + reasoning_content + length：思维链是被截断的思考过程，不是答案——
    必须放大重试，而非拿思维链交差（1.0.5 回退逻辑的截断漏洞）。"""
    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    c = _SeqClient([
        _resp(content="", reasoning="让我想想这张图……（被截断的思维链）", finish="length"),
        _resp(content='{"peek": "真正的答案", "text": "完整"}', finish="stop"),
    ])
    out = _run_read(str(img), c, "deepseek-flash", max_tokens=4096)
    assert "真正的答案" in out
    assert "思维链" not in out
    assert len(c.payloads) == 2
    assert c.payloads[0]["max_tokens"] == 8192  # 基线已抬
    assert c.payloads[1]["max_tokens"] == 16384  # 放大到封顶


def test_length_still_truncated_raises(tmp_path):
    """放大重试后仍 length → OutputTruncatedError（不再落库截断内容）。"""
    import asyncio

    from tools._vl_client import OutputTruncatedError, read_image

    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    c = _SeqClient([
        _resp(content="", reasoning="思考中", finish="length"),
        _resp(content="", reasoning="还在思考", finish="length"),
    ])

    async def _call():
        await read_image(
            str(img), "p", client=c,
            vl_config={"api_key": "k", "model": "deepseek-flash", "base_url": "http://x"},
            json_mode=True,
        )

    with pytest.raises(OutputTruncatedError):
        asyncio.run(_call())
    assert len(c.payloads) == 2  # 只放大重试一次


def test_reasoning_fallback_only_when_not_length(tmp_path):
    """content 空且 finish_reason 非 length（罕见网关行为）→ 保留 1.0.5 兼容回退。"""
    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    c = _SeqClient([_resp(content="", reasoning="网关放在这里的答案", finish="stop")])
    out = _run_read(str(img), c, "some-gateway-model", max_tokens=4096)
    assert out == "网关放在这里的答案"
    assert len(c.payloads) == 1  # 非截断不重试


def test_thinking_effort_payload(tmp_path):
    """reasoning_effort 仅对 DeepSeek 思考型模型附加 thinking 参数；
    none（关闭思考）时 max_tokens 基线不抬；别名映射与非法回退生效。"""
    import asyncio

    from tools._vl_client import read_image

    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img)

    def _call(model, effort_cfg, max_tokens=4096):
        c = _SeqClient([_resp(content="ok")])
        cfg = {"api_key": "k", "model": model, "base_url": "http://x"}
        if effort_cfg is not None:
            cfg["reasoning_effort"] = effort_cfg

        async def _run():
            await read_image(
                str(img), "p", max_tokens=max_tokens, client=c,
                vl_config=cfg, json_mode=True,
            )

        asyncio.run(_run())
        return c.payloads[0]

    # 默认 low（未配置）——reasoning_effort 是顶层参数（DeepSeek 官方形态，
    # 嵌套进 thinking 对象会被服务端静默忽略）
    p = _call("deepseek-flash", None)
    assert p["reasoning_effort"] == "low"
    assert "thinking" not in p
    assert p["max_tokens"] == 8192  # 思考开启 → 基线抬升
    assert p["response_format"] == {"type": "json_object"}  # json_mode=True
    # 配置 high
    p = _call("deepseek-flash", "high")
    assert p["reasoning_effort"] == "high"
    # none：关闭思考 → 基线不抬；response_format 与 effort 独立，照常附加
    p = _call("deepseek-flash", "none")
    assert p["reasoning_effort"] == "none"
    assert p["max_tokens"] == 4096
    assert p["response_format"] == {"type": "json_object"}
    # 官方别名 minimal→low
    p = _call("deepseek-flash", "minimal")
    assert p["reasoning_effort"] == "low"
    # 非法值回退 low
    p = _call("deepseek-flash", "turbo")
    assert p["reasoning_effort"] == "low"
    # 非 DeepSeek 模型不附加（其他 provider 不识别该字段，会 400）
    p = _call("gpt-4o", "low")
    assert "reasoning_effort" not in p
    assert "thinking" not in p


def test_provider_detail():
    """跨 provider detail 方言映射：original→OpenAI 发 high（消除 400）；
    high→DeepSeek 发 original（官方等价）。压缩档位不受发送侧映射影响。"""
    from tools._vl_client import provider_detail

    # DeepSeek 系
    assert provider_detail("deepseek-flash", "original") == "original"
    assert provider_detail("deepseek-flash", "high") == "original"
    assert provider_detail("deepseek-flash", "low") == "low"
    assert provider_detail("deepseek-flash", "auto") == "auto"
    # OpenAI 系
    assert provider_detail("gpt-4o", "original") == "high"
    assert provider_detail("gpt-4o", "high") == "high"
    assert provider_detail("gpt-4o", "low") == "low"
    assert provider_detail("gpt-4o", "auto") == "auto"
