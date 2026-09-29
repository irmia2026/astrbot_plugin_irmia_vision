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


class ImageTooLargeError(ValueError):
    """压缩后仍超过上传限制。调用方不应重试或降级——结果不会变。"""
TARGET_LONG_EDGE = 2048
TARGET_QUALITY = 85

# DeepSeek 视觉模型（deepseek-flash，旧名 deepseek-v4-flash-vision-exp）现行官方文档：
# 图片在进入模型前会被自动缩放——小图放大、大图缩小到总像素约 1300×1300
# （每张 token 上限 1024，2000×2000 与 5000×5000 消耗相同）。
# 传 2048 不会提升模型看到的细节，只浪费上传带宽。
# 注：1024 长边（方形 1.05M 像素）低于服务端满配（~1.69M），细节有取舍；
# 追求满配细节可配置 detail=original（跳过客户端降采样，由服务端缩放）。
V4FVE_LONG_EDGE = 1024

# detail=low 的服务端行为在 DeepSeek 与 OpenAI 一致：缩放到 512×512。
# 客户端对齐到 512——更大输入无收益只费带宽（与 v4fve 1024 档同一逻辑）。
LOW_DETAIL_LONG_EDGE = 512

# vision_compare 多图对比的单次图片数上限。DeepSeek 单请求最多 600 张、
# ≥15 张时单边上限降为 4096px（本插件压缩到 2048/1024 本就低于此）；
# 真正的约束是请求体 48 MiB 与上下文 token（每张 ≤1024 token，16 张 ≈ 16K），
# 取 16 为体验与成本的平衡点。
MAX_COMPARE_IMAGES = 16
# 多图内联 base64 总量预算：DeepSeek 请求体上限 48 MiB，base64 有 ~33% 膨胀，
# 40 MiB 预算为 prompt 与协议开销留出余量。
MAX_INLINE_IMAGES_B64 = 40 * 1024 * 1024

# 思考型模型（思维链与答案共享 max_tokens 额度）的 max_tokens 基线。
# DeepSeek 未设置时思考模式默认 64K；显式小额度（如旧默认 4096）会把思考+答案
# 压在一起，导致 content 被思维链吃光或 JSON 写一半（finish_reason=length）。
V4FVE_MIN_MAX_TOKENS = 8192
# finish_reason=length 时单次放大重试的封顶（DeepSeek 上限 384K；
# 主流 OpenAI 兼容模型输出上限 ≥8K，16K 仍在安全范围）
TRUNCATION_RETRY_MAX_TOKENS = 16384


class OutputTruncatedError(ValueError):
    """放大重试后输出仍被 max_tokens 截断（finish_reason=length）。

    调用方语义：同一 provider 重试无意义（额度已在内部放大过），
    但应降级——截断是模型相关的，换非思考型模型可能成功。
    """


def is_v4fve(model: str) -> bool:
    """判断模型是否为 DeepSeek 思考型视觉模型。

    匹配：deepseek-v4-flash-vision-exp（官方已下线，请求由 deepseek-flash 承接）、
    v4fve 别名、deepseek-flash（现行模型，默认思考模式 reasoning_effort=high）。
    命中后启用：1024 压缩档、response_format JSON Output、max_tokens 基线抬升。
    """
    m = (model or "").lower()
    return "v4-flash-vision" in m or "v4fve" in m or "deepseek-flash" in m


_DETAIL_ALLOWED = ("low", "auto", "original", "high")


def normalize_detail(value) -> str:
    """归一化 detail 配置：strip + lower + 白名单校验，非法值告警并回退 auto。

    白名单是两家并集（DeepSeek 认 low/auto/original/high，OpenAI 认 low/high/auto）；
    provider 专属方言的发送侧映射见 provider_detail。
    """
    v = str(value or "").strip().lower()
    if not v:
        return "auto"
    if v not in _DETAIL_ALLOWED:
        logger.warning(f"detail 配置非法: {value!r}，回退为 auto（合法值: {', '.join(_DETAIL_ALLOWED)}）")
        return "auto"
    return v


def provider_detail(model: str, detail: str) -> str:
    """按 provider 方言归一 detail 的发送值，消除跨 provider 400：
    - DeepSeek 认四值（high 官方等价 original）→ high 统一发 original；
    - OpenAI 只认 low/high/auto → original 映射为 high（精细档，最接近保留原图语义）。
    注意：缓存键始终用归一化前的用户档位（detail），发送侧映射不影响缓存。
    """
    d = normalize_detail(detail)
    if is_v4fve(model):
        return "original" if d == "high" else d
    return "high" if d == "original" else d


_EFFORT_ALLOWED = ("none", "low", "high", "max")
# DeepSeek 官方兼容映射：API 参考页 minimal→low、medium/xhigh→high；
# 思考模式指南另列 ultra→max（参考页未列 ultra，客户端映射后发送值恒在合法枚举内，
# 比透传 ultra 更稳——遇严格校验有 400 风险）
_EFFORT_ALIAS = {"minimal": "low", "medium": "high", "xhigh": "high", "ultra": "max"}


def normalize_reasoning_effort(value) -> str:
    """归一化 reasoning_effort（DeepSeek 思考强度）：strip + lower + 官方别名映射 +
    白名单校验，非法值告警并回退 low。仅对 DeepSeek 思考型模型附加到请求
    （其他 provider 不识别该字段，原样发会 400）。"""
    v = str(value or "").strip().lower()
    if not v:
        return "low"
    v = _EFFORT_ALIAS.get(v, v)
    if v not in _EFFORT_ALLOWED:
        logger.warning(f"reasoning_effort 配置非法: {value!r}，回退为 low（合法值: {', '.join(_EFFORT_ALLOWED)}）")
        return "low"
    return v


def target_edge_for_model(model: str) -> int:
    """按目标模型选择压缩长边：v4fve 对齐其服务端 ~1300×1300 总像素缩放，其余用默认。"""
    return V4FVE_LONG_EDGE if is_v4fve(model) else TARGET_LONG_EDGE


def effective_target_edge(model: str, detail: str) -> int | None:
    """综合模型与 detail 决定压缩长边。返回 None 表示不降采样（保留原图）。

    - detail=original：官方语义「保留原图」，跳过客户端降采样——若客户端仍无条件
      压缩则名存实亡，尤其是 see_window 截图读小字代码的场景。
    - detail=high：DeepSeek 官方等价 original（保留原图）→ 同样不降采样；
      OpenAI 的 high 是精细档（自带短边 768/长边 2000 缩放），客户端 2048 对齐。
    - detail=low：DeepSeek/OpenAI 服务端都缩到 512×512 → 客户端对齐 512。
    """
    d = normalize_detail(detail)
    if d == "original":
        return None
    if d == "high" and is_v4fve(model):
        return None  # DeepSeek: high 等价 original（保留原图）
    if d == "low":
        return LOW_DETAIL_LONG_EDGE
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
            raise ImageTooLargeError(f"图片压缩后仍超过 20MB 上传限制: {path}")
        return data, mime


def encode_image(path: str, target_long_edge: int | None = TARGET_LONG_EDGE) -> str:
    """读取图片并压缩后编码为 base64。"""
    raw, mime = _compress_image(path, target_long_edge=target_long_edge)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


async def _post_chat(content: list[dict], *, max_tokens: int, client, cfg: dict, json_mode: bool) -> str:
    """发送一条 user 消息（content 块数组）到 OpenAI 兼容端点，返回文本内容。

    DeepSeek 限制：图片只能出现在 user 消息中（system/assistant 携带图片返回 400），
    单图与多图调用都收敛到这一条消息形态。

    截断处理（DeepSeek 思考模式的核心坑）：
    - 思维链与答案共享 max_tokens 额度，思考型模型基线抬到 V4FVE_MIN_MAX_TOKENS；
    - finish_reason=length 表示输出被 max_tokens/上下文截断（content 可能是半截 JSON，
      content 为空时 reasoning_content 只是被截断的思维链，不是答案）——
      放大额度单次重试，封顶 TRUNCATION_RETRY_MAX_TOKENS；
    - 放大后仍 length 抛 OutputTruncatedError（同一模型重试无意义，应降级）；
    - reasoning_content 回退仅限非截断场景（兼容某些网关把内容放在该字段）。
    """
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "gpt-4o")
    try:
        timeout = float(cfg.get("timeout", 120.0) or 120.0)
    except (TypeError, ValueError):
        timeout = 120.0

    if not api_key:
        raise ValueError("未配置 VL 模型的 api_key（vl_provider_ids 或 vl_model.api_key）")

    # DeepSeek 思考强度（官方参数）：读图是感知任务，low 足够——思维链 token 大减，
    # 截断风险、费用、延迟同步下降。none = 关闭思考模式。
    effort = ""
    if is_v4fve(model):
        effort = normalize_reasoning_effort(cfg.get("reasoning_effort", "low"))
        if effort != "none":
            # 思维链与答案共享 max_tokens 额度，思考型模型基线抬升；
            # 关闭思考（none）时无思维链抢额度，不必抬
            max_tokens = max(max_tokens, V4FVE_MIN_MAX_TOKENS)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempt_tokens = max_tokens
    for attempt in range(2):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": attempt_tokens,
        }
        if effort:
            # DeepSeek 官方形态（思考模式指南 + SDK 示例）：reasoning_effort 是顶层参数；
            # thinking 对象仅含 type（enabled/disabled），effort=none 即关闭思考模式。
            payload["reasoning_effort"] = effort
        if json_mode and is_v4fve(model):
            # DeepSeek JSON Output：官方保证输出合法 JSON（prompt 需含 json 字样，由调用方保证）
            payload["response_format"] = {"type": "json_object"}

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
        choice = data["choices"][0]
        msg = choice["message"]
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        finish = str(choice.get("finish_reason") or "")

        if finish == "length":
            # 带上 usage：reasoning_tokens 直接显示思维链吃了多少额度（截断排障的关键证据）
            usage = data.get("usage") or {}
            completion_tokens = usage.get("completion_tokens")
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            usage_hint = f"completion_tokens={completion_tokens}, reasoning_tokens={reasoning_tokens}"
            if attempt == 0 and attempt_tokens < TRUNCATION_RETRY_MAX_TOKENS:
                new_tokens = min(max(attempt_tokens * 2, V4FVE_MIN_MAX_TOKENS), TRUNCATION_RETRY_MAX_TOKENS)
                logger.warning(
                    f"模型 {model} 输出被 max_tokens={attempt_tokens} 截断"
                    f"（finish_reason=length，{usage_hint}），放大到 {new_tokens} 重试一次"
                )
                attempt_tokens = new_tokens
                continue
            raise OutputTruncatedError(
                f"模型 {model} 输出在 max_tokens={attempt_tokens} 下仍被截断"
                f"（finish_reason=length，{usage_hint}）"
            )
        if text:
            return text
        # content 空且非截断：保留 1.0.5 的兼容回退（罕见：某些网关把内容放在 reasoning_content）
        return reasoning


async def read_image(path: str, prompt: str, *, max_tokens: int = 4096, client=None, vl_config: dict | None = None, json_mode: bool = False, image_url: str | None = None) -> str:
    """异步调用 VL 模型读取图片，返回文本结果。

    Args:
        vl_config: 显式传入的 VL 配置（来自 provider 降级链）。为 None 时回退到全局配置。
        client: 外部 httpx.AsyncClient 以共享连接池。
        json_mode: 请求结构化 JSON 输出。仅对 v4fve 附加官方 response_format
            （DeepSeek JSON Output 文档）；其他模型仅靠 prompt 引导，由调用方容错解析。
        image_url: 预编码的 data URL。传入后跳过压缩编码——重试/降级链上同一
            压缩档位的编码结果可复用，避免同一张图重复压缩+base64。
    """
    cfg = vl_config if vl_config is not None else get_vl_model_config()
    if not cfg.get("api_key", ""):
        # 在压缩前拦截（保持与重构前一致的失败顺序）
        raise ValueError("未配置 VL 模型的 api_key（vl_provider_ids 或 vl_model.api_key）")

    model = cfg.get("model", "gpt-4o")
    # detail 归一化（非法值回退 auto）。压缩档位用原始档位（用户意图，original=保留原图），
    # 发送值按 provider 方言映射（original→OpenAI 发 high，消除 400；缓存键不受映射影响）。
    raw_detail = cfg.get("detail", "auto")

    if image_url is None:
        # 压缩编码（PIL 解码 + 缩放 + 重编码 + base64）是 CPU/IO 密集同步操作，
        # 移出事件循环避免阻塞宿主。大小限制在 _compress_image 内对压缩结果生效。
        # v4fve 触发官方文档适配：压缩长边 2048 → 1024（其服务端缩放到 ~1300×1300 总像素，
        # token 上限 1024/张，更大输入无收益只费带宽）。
        image_url = await run_sync(encode_image, path, effective_target_edge(model, raw_detail))

    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": image_url, "detail": provider_detail(model, raw_detail)}},
    ]
    return await _post_chat(content, max_tokens=max_tokens, client=client, cfg=cfg, json_mode=json_mode)


async def read_images(paths: list[str], prompt: str, *, labels: list[str] | None = None, max_tokens: int = 8192, client=None, vl_config: dict | None = None, json_mode: bool = False, image_urls: list[str] | None = None) -> str:
    """一次请求携带多张图片（对比场景），返回文本结果。

    DeepSeek 契约：多个 image_url 块放同一条 user 消息（单请求最多 600 张）；
    每张图独立按同一规则缩放计费（≤1024 token/张），多图无额外算法。
    图片之间插入文本标记（图1/图2…），让模型在回答中能引用具体图片。
    内联 base64 总量受 MAX_INLINE_IMAGES_B64 预算约束，超预算抛 ImageTooLargeError
    （调用方不应重试或降级——结果不会变）。
    image_urls: 预编码的 data URL 列表（与 paths 等长同序）。传入后跳过压缩编码，
    供重试/降级链复用同一压缩档位的结果，避免多图场景重复压缩的 N 倍放大浪费。
    """
    cfg = vl_config if vl_config is not None else get_vl_model_config()
    if not cfg.get("api_key", ""):
        raise ValueError("未配置 VL 模型的 api_key（vl_provider_ids 或 vl_model.api_key）")

    model = cfg.get("model", "gpt-4o")
    raw_detail = cfg.get("detail", "auto")
    detail = provider_detail(model, raw_detail)

    if image_urls is None:
        edge = effective_target_edge(model, raw_detail)
        image_urls = [await run_sync(encode_image, p, edge) for p in paths]
    total_b64 = sum(len(u) for u in image_urls)
    if total_b64 > MAX_INLINE_IMAGES_B64:
        raise ImageTooLargeError(
            f"多图内联总量超预算：{total_b64 // 1048576}MB > "
            f"{MAX_INLINE_IMAGES_B64 // 1048576}MB，请减少图片数量"
        )

    content: list[dict] = [{"type": "text", "text": prompt}]
    for i, image_url in enumerate(image_urls):
        label = labels[i] if labels and i < len(labels) else f"图{i + 1}"
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": detail}})
    return await _post_chat(content, max_tokens=max_tokens, client=client, cfg=cfg, json_mode=json_mode)


def encode_image_to_base64(path: str) -> str:
    """兼容旧接口：直接读取原始文件并 base64（不推荐）。"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
