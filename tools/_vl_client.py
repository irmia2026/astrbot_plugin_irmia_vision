"""
VL 模型客户端
支持 OpenAI 兼容 API（base_url + api_key + model）
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import httpx
from PIL import Image

from .config import get_vl_model_config

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB
TARGET_LONG_EDGE = 2048
TARGET_QUALITY = 85


def _compress_image(path: str, target_long_edge: int = TARGET_LONG_EDGE, quality: int = TARGET_QUALITY) -> tuple[bytes, str]:
    """压缩图片到指定长边，返回字节和 MIME 类型。"""
    ext = Path(path).suffix.lower()
    with Image.open(path) as img:
        # 处理动画 gif 的第一帧
        if getattr(img, "is_animated", False):
            img.seek(0)

        # 转换为 RGB 以统一处理
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size
        if max(w, h) > target_long_edge:
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
        return buf.getvalue(), mime


def encode_image(path: str) -> str:
    """读取图片并压缩后编码为 base64。"""
    raw, mime = _compress_image(path)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


async def read_image(path: str, prompt: str, *, max_tokens: int = 2048, client = None) -> str:
    """异步调用用户配置的 VL 模型读取图片，返回文本结果。

    支持传入外部 httpx.AsyncClient 以共享连接池，减少并发时的建连开销。
    """
    cfg = get_vl_model_config()
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "gpt-4o")
    timeout = cfg.get("timeout", 120.0)

    if not api_key:
        raise ValueError("未配置 vl_model.api_key")

    size = os.path.getsize(path)
    if size > MAX_IMAGE_SIZE:
        raise ValueError(f"图片超过 20MB 限制: {path}")

    image_url = encode_image(path)

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
                            "detail": "auto",
                        },
                    },
                ],
            }
        ],
        "max_tokens": max_tokens,
    }

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
    return data["choices"][0]["message"]["content"]


def encode_image_to_base64(path: str) -> str:
    """兼容旧接口：直接读取原始文件并 base64（不推荐）。"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
