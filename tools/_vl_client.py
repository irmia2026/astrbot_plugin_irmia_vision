"""
VL 模型客户端
支持 OpenAI 兼容 API（base_url + api_key + model）
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
from PIL import Image

from astrbot.api import logger

from ._helpers import run_sync
from .config import get_vl_model_config

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 压缩后上传 payload 上限 20MB（不限制原始文件）
TARGET_LONG_EDGE = 2048
TARGET_QUALITY = 85

# DeepSeek v4fve（deepseek-v4-flash-vision-exp）官方文档：
# 图片在进入模型前会被自动缩放到总像素约 800×800（每张 token 上限 384，
# 2000×2000 与 5000×5000 消耗相同）。传 2048 不会提升模型看到的细节，
# 只浪费上传带宽；压到长边 1024 留有充足余量，上传体积可省 ~75%。
V4FVE_LONG_EDGE = 1024


def is_v4fve(model: str) -> bool:
    """判断模型是否为 DeepSeek v4fve 视觉模型（deepseek-v4-flash-vision-exp）。"""
    m = (model or "").lower()
    return "v4-flash-vision" in m or "v4fve" in m


_DETAIL_ALLOWED = ("low", "auto", "original", "high")


def normalize_detail(value) -> str:
    """归一化 detail 配置：strip + lower + 白名单校验，非法值告警并回退 auto。

    注意跨 provider 语义差异：DeepSeek 认 low/auto/original，OpenAI 认 low/high/auto，
    非法值原样发给 API 会导致 400，必须在这里拦住。
    """
    v = str(value or "").strip().lower()
    if not v:
        return "auto"
    if v not in _DETAIL_ALLOWED:
        logger.warning(f"detail 配置非法: {value!r}，回退为 auto（合法值: {', '.join(_DETAIL_ALLOWED)}）")
        return "auto"
    return v


def target_edge_for_model(model: str) -> int:
    """按目标模型选择压缩长边：v4fve 对齐其服务端 800×800 缩放，其余用默认。"""
    return V4FVE_LONG_EDGE if is_v4fve(model) else TARGET_LONG_EDGE


def effective_target_edge(model: str, detail: str) -> int | None:
    """综合模型与 detail 决定压缩长边。返回 None 表示不降采样（保留原图）。

    detail=original 的官方语义是「保留原图」，若客户端仍无条件压缩则名存实亡，
    尤其是 see_window 截图读小字代码的场景。
    """
    if normalize_detail(detail) == "original":
        return None
    return target_edge_for_model(model)


def _compress_image(path: str, target_long_edge: int | None = TARGET_LONG_EDGE, quality: int = TARGET_QUALITY) -> tuple[bytes, str]:
    """压缩图片到指定长边，返回字节和 MIME 类型。target_long_edge=None 时不缩放。"""
    ext = Path(path).suffix.lower()
    with Image.open(path) as img:
        # 处理动画 gif 的第一帧
        if getattr(img, "is_animated", False):
            img.seek(0)

        # 转换为 RGB 以统一处理
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size
        if target_long_edge is not None and max(w, h) > target_long_edge:
            ratio = target_long_edge / max(w, h)
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        if ext in (".jpg", ".jpeg"):
            fmt = "JPEG"
            mime = "image/jpeg"
        elif ext == ".webp":
            fmt = "WEBP"
            mime = "image/webp"
        elif ext == ".gif":
            fmt = "JPEG"  # gif 压缩为 jpeg
            mime = "image/jpeg"
        elif ext == ".bmp":
            fmt = "JPEG"
            mime = "image/jpeg"
        else:
            fmt = "PNG"
            mime = "image/png"

        buf = io.BytesIO()
        if fmt in ("JPEG", "WEBP"):
            img.save(buf, format=fmt, quality=quality)
        else:
            img.save(buf, format=fmt)
        data = buf.getvalue()
        # 大小限制作用于压缩后的实际上传内容，而非原始文件：
        # 一张 25MB 的照片压缩到长边 2048 后通常只有 1-2MB，完全可以正常上传。
        if len(data) > MAX_IMAGE_SIZE:
            raise ValueError(f"图片压缩后仍超过 20MB 上传限制: {path}")
        return data, mime


def encode_image(path: str, target_long_edge: int | None = TARGET_LONG_EDGE) -> str:
    """读取图片并压缩后编码为 base64。"""
    raw, mime = _compress_image(path, target_long_edge=target_long_edge)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


async def read_image(path: str, prompt: str, *, max_tokens: int = 4096, client=None, vl_config: dict | None = None, json_mode: bool = False) -> str:
    """异步调用 VL 模型读取图片，返回文本结果。

    Args:
        vl_config: 显式传入的 VL 配置（来自 provider 降级链）。为 None 时回退到全局配置。
        client: 外部 httpx.AsyncClient 以共享连接池。
        json_mode: 请求结构化 JSON 输出。仅对 v4fve 附加官方 response_format
            （DeepSeek JSON Output 文档）；其他模型仅靠 prompt 引导，由调用方容错解析。
    """
    cfg = vl_config if vl_config is not None else get_vl_model_config()
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "gpt-4o")
    timeout = cfg.get("timeout", 120.0)

    if not api_key:
        raise ValueError("未配置 VL 模型的 api_key（vl_provider_ids 或 vl_model.api_key）")

    # detail 归一化（非法值回退 auto）；original=保留原图，跳过客户端降采样。
    detail = normalize_detail(cfg.get("detail", "auto"))

    # 压缩编码（PIL 解码 + 缩放 + 重编码 + base64）是 CPU/IO 密集同步操作，
    # 移出事件循环避免阻塞宿主。大小限制在 _compress_image 内对压缩结果生效。
    # v4fve 触发官方文档适配：压缩长边 2048 → 1024（其服务端缩放到 ~800×800，
    # token 上限 384/张，更大输入无收益只费带宽）。
    image_url = await run_sync(encode_image, path, effective_target_edge(model, detail))

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": detail,
                        },
                    },
                ],
            }
        ],
        "max_tokens": max_tokens,
    }
    if json_mode and is_v4fve(model):
        # DeepSeek JSON Output：官方保证输出合法 JSON（prompt 需含 json 字样，由调用方保证）
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if client is not None:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
    else:
        async with httpx.AsyncClient(timeout=timeout) as client_inner:
            resp = await client_inner.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content:
        # DeepSeek 推理模型（如 deepseek-v4-flash-vision-exp）思考模式下
        # 思维链走 reasoning_content 字段，content 可能为空（token 被思考耗尽）。
        # 回退到 reasoning_content，保证结果不为空。
        content = msg.get("reasoning_content") or ""
    return content


def encode_image_to_base64(path: str) -> str:
    """兼容旧接口：直接读取原始文件并 base64（不推荐）。"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
