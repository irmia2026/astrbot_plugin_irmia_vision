"""
VL 模型客户端
支持 OpenAI 兼容 API（base_url + api_key + model）
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from .config import get_vl_model_config

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


def encode_image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def read_image(path: str, prompt: str) -> str:
    """调用用户配置的 VL 模型读取图片，返回文本结果。"""
    cfg = get_vl_model_config()
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "gpt-4o")

    if not api_key:
        raise ValueError("未配置 vl_model.api_key")

    size = os.path.getsize(path)
    if size > MAX_IMAGE_SIZE:
        raise ValueError(f"图片超过 20MB 限制: {path}")

    b64 = encode_image_to_base64(path)
    ext = Path(path).suffix.lower()
    mime = "image/png"
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif ext == ".webp":
        mime = "image/webp"
    elif ext == ".gif":
        mime = "image/gif"

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
                            "url": f"data:{mime};base64,{b64}",
                            "detail": "auto",
                        },
                    },
                ],
            }
        ],
        "max_tokens": 2048,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]
