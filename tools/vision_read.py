"""
vision_read — 批量读图并落库
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from astrbot.api import logger
from PIL import Image

from ._helpers import proposal_reply, run_sync
from ._store import VisionStore
from ._vl_client import ImageTooLargeError, normalize_detail, read_image as vl_read_image
from .config import resolve_provider_chain

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_PROMPT = (
    "你是一位专业的视觉描述者，正在为一位视力障碍人士描述图片。"
    "请用自然流畅的中文段落，客观、生动地描述图片内容。"
    "包括：整体场景与背景、主要主体的外观/姿态/表情、空间布局（左、右、前景、背景）、"
    "光线（光源、对比、阴影）、色调与整体氛围。"
    "请逐字读出图中所有可见文字。"
    "严格基于可见内容进行描述，不要猜测。"
    "最后用 2-3 句话总结图片的主题或隐含叙事。"
)

# 结构化输出：要求模型返回 JSON（summary/text/tags），插件容错解析。
# prompt 中必须含 "json" 字样（DeepSeek JSON Output 的官方要求）。
STRUCTURED_SUFFIX_DEFAULT = (
    "\n\n请以 JSON 格式输出（json），结构如下：\n"
    '{"summary": "这是一张xxx图片，可以看到……（一两句话概括，让没看过图的人快速建立预期）", '
    '"text": "按上述要求展开的完整详尽描述", '
    '"tags": ["3-6 个内容标签，如 发票/截图/风景/代码/聊天"]}\n'
    "只输出这个 JSON 对象，不要输出任何其他内容。"
)
STRUCTURED_SUFFIX_QUESTION = (
    "\n\n请以 JSON 格式输出（json），结构如下：\n"
    '{"summary": "对问题的一句话直接回答", '
    '"text": "完整详细的回答（包含依据和细节）", '
    '"tags": ["3-6 个内容标签"]}\n'
    "只输出这个 JSON 对象，不要输出任何其他内容。"
)


def _collect_image_paths(paths: list[str]) -> list[str]:
    results: list[str] = []
    for p in paths:
        expanded = os.path.expanduser(p)
        if not os.path.exists(expanded):
            continue
        if os.path.isdir(expanded):
            for root, _, files in os.walk(expanded):
                for f in files:
                    if Path(f).suffix.lower() in SUPPORTED_EXTS:
                        results.append(os.path.join(root, f))
        else:
            if Path(expanded).suffix.lower() in SUPPORTED_EXTS:
                results.append(expanded)
    return sorted(set(results))


def _compute_phash(path: str) -> str:
    """计算感知哈希。若 imagehash 未安装则跳过，不阻塞读图流程。"""
    try:
        import imagehash

        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except ImportError:
        return ""
    except Exception as e:
        logger.warning(f"计算 phash 失败 {path}: {e}")
        return ""


def _extract_json(text: str) -> dict | None:
    """从模型输出中提取 JSON 对象：容忍 ```json 围栏和前后杂音。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_result(raw: str) -> dict:
    """解析 VL 返回：优先按结构化 JSON（summary/text/tags）解析，
    模型不遵守格式时回退到「首行作为摘要」的旧行为，读图永不因解析失败而失败。"""
    text = raw.strip()
    data = _extract_json(text)
    if data:
        summary = str(data.get("summary") or "").strip()
        body = str(data.get("text") or "").strip()
        tags_raw = data.get("tags")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        if summary or body:
            return {
                "summary": (summary or body.split("\n")[0])[:200],
                "text": body or summary,
                "tags": tags,
            }
    summary = text.split("\n")[0] if text else ""
    return {
        "summary": summary[:200],
        "text": text,
        "tags": [],
    }


def _adaptive_concurrency(model_cfg: dict) -> int:
    """自适应并发数：
    - 用户显式配置了 concurrency 则优先使用；
    - 否则根据 timeout 估算，避免所有请求同时排队超时；
    - 最高不超过 200。
    """
    user_value = model_cfg.get("concurrency")
    if isinstance(user_value, int) and user_value > 0:
        return user_value

    timeout = float(model_cfg.get("timeout", 120.0))
    suggested = max(1, int(timeout / 2.5))
    return min(suggested, 200)


async def read(
    db: VisionStore,
    paths: list[str],
    question: str = "",
    force_reread: bool = False,
    previous_result_id: str = "",
    allow_phash: bool = True,
) -> dict:
    image_paths = _collect_image_paths(paths)
    if not image_paths:
        return proposal_reply(
            False,
            "未找到任何支持的图片文件。请确认路径存在，并且包含 png/jpg/jpeg/webp/gif/bmp 格式的图片。",
            options=["检查路径是否正确", "使用 vision_query 查看已有结果"],
        )

    # 结构化输出：所有模型走 prompt 引导 + 容错解析；v4fve 额外由客户端附加 response_format
    prompt = (question + STRUCTURED_SUFFIX_QUESTION) if question else (DEFAULT_PROMPT + STRUCTURED_SUFFIX_DEFAULT)
    # 不在此处拦截空链：命中缓存（此前已读过的图）不需要 VL 模型配置。
    # 只有存在未命中、需要调用模型的路径才要求 chain 非空（见 _read_one）。
    chain = resolve_provider_chain()
    primary = chain[0] if chain else None
    concurrency = _adaptive_concurrency(primary) if primary else 2
    max_retries = max(0, int((primary or {}).get("max_retries", 2)))
    # 用降级链中最大的 timeout 构建共享 client，避免 fallback 被 primary 的短 timeout 截断
    vl_timeout = max(float(cfg.get("timeout", 120.0)) for cfg in chain) if chain else 120.0
    # detail 影响模型实际输入（从而影响输出），是缓存键的一部分
    cache_detail = normalize_detail((primary or {}).get("detail", "auto"))

    previous_result = None
    if previous_result_id:
        previous_result = await run_sync(db.get_by_result_id, previous_result_id)

    cached_count = 0
    phash_cached_count = 0
    read_count = 0
    failed_count = 0
    failed_paths: list[str] = []
    first_result_id = ""
    last_result_id = ""
    semaphore = asyncio.Semaphore(concurrency)

    import httpx
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    _httpx_client = httpx.AsyncClient(timeout=vl_timeout, limits=limits)

    async def _read_one(path: str) -> None:
        nonlocal cached_count, phash_cached_count, read_count, failed_count, first_result_id, last_result_id

        try:
            filename = os.path.basename(path)
            # sha256 / phash / SQLite 均为同步 I/O，offload 到线程池避免阻塞事件循环
            sha256 = await run_sync(db.sha256_of_file, path)

            is_follow_up = False
            previous_context = ""
            if previous_result:
                prev_sha256 = previous_result.get("sha256", "")
                if prev_sha256 and prev_sha256 == sha256:
                    is_follow_up = True
                    previous_summary = previous_result.get("summary", "")
                    previous_text = previous_result.get("text", "")
                    previous_context = f"\n之前对这张图的理解：{previous_summary}\n提取的文字：{previous_text}\n"

            cached = None
            if not is_follow_up and not force_reread:
                # 缓存按内容寻址（sha256），不含文件名：see_window 的时间戳截图也能命中
                cached = await run_sync(db.find_cached, sha256, (primary or {}).get("model", ""), question, cache_detail)
            if cached:
                cached_count += 1
                return

            phash = await run_sync(_compute_phash, path)

            # allow_phash=False（如 see_window）：屏幕内容时刻在变，近似命中会返回过期描述，禁用兜底
            if allow_phash and not is_follow_up and not force_reread and phash:
                # sha256 精确未命中 → phash 近似兜底：缩尺/重压缩的同图也能命中
                cached = await run_sync(
                    db.find_cached_by_phash, phash, (primary or {}).get("model", ""), question, cache_detail
                )
                if cached:
                    cached_count += 1
                    phash_cached_count += 1
                    logger.info(
                        f"phash 近似命中 {path} → {cached.get('result_id')} "
                        f"(distance={cached.get('phash_distance')})"
                    )
                    return

            final_prompt = prompt + previous_context if previous_context else prompt

            if not chain:
                raise ValueError(
                    "未配置任何 VL 模型（vl_provider_ids 为空且 vl_model 缺少 api_key），无法读取新图片"
                )

            raw = ""
            used_model = ""
            used_detail = cache_detail
            last_err: Exception | None = None
            for vl_cfg in chain:
                if not vl_cfg.get("api_key"):
                    continue
                for attempt in range(max_retries + 1):
                    async with semaphore:
                        try:
                            raw = await vl_read_image(path, final_prompt, client=_httpx_client, vl_config=vl_cfg, json_mode=True)
                            last_err = None
                            used_model = vl_cfg.get("model", "unknown")
                            used_detail = normalize_detail(vl_cfg.get("detail", "auto"))
                            break
                        except ImageTooLargeError:
                            raise  # 压缩后仍超限：重试/降级结果都一样，直接失败不放大
                        except Exception as e:
                            last_err = e
                    if last_err is None:
                        break
                    if attempt < max_retries:
                        logger.warning(f"读图重试 {path} (provider={vl_cfg.get('model','')}): {last_err}")
                        await asyncio.sleep(1.0)
                if last_err is None:
                    if raw:
                        break  # 调用成功且返回内容
                    # 调用成功但返回空内容（如模型不支持视觉输入）→ 视为失败，继续降级
                    last_err = ValueError(f"模型 {vl_cfg.get('model','')} 返回空内容")
                logger.warning(f"provider {vl_cfg.get('model','')} 失败，尝试降级: {last_err}")
            if last_err is not None:
                raise last_err
            if not raw:
                raise ValueError("所有 VL 模型均返回空内容，无法读图")

            parsed = _parse_result(raw)
            result_id = f"res_{uuid.uuid4().hex[:12]}"

            await run_sync(
                db.insert,
                sha256=sha256,
                filename=filename,
                phash=phash,
                model_id=used_model or (primary or {}).get("model", "unknown"),
                question=question,
                result_id=result_id,
                source_value=path,
                summary=parsed["summary"],
                text=parsed["text"],
                tags=parsed["tags"],
                result_json={
                    "summary": parsed["summary"],
                    "text": parsed["text"],
                    "tags": parsed["tags"],
                    "raw": raw,
                },
                detail=used_detail,
            )
            read_count += 1
            if not first_result_id:
                first_result_id = result_id
            last_result_id = result_id
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"读图失败 {path}: {e}")
            failed_count += 1
            if len(failed_paths) < 10:
                failed_paths.append(f"{path}: {err_msg}")

    try:
        await asyncio.gather(*[_read_one(p) for p in image_paths])
    finally:
        await _httpx_client.aclose()

    if read_count == 0 and cached_count == 0 and failed_count > 0:
        chain_desc = [
            f"{c.get('model','?')}@{c.get('base_url','')[:40]} key={'有' if c.get('api_key') else '无'}"
            for c in chain
        ]
        err_detail = " | ".join(failed_paths[:3]) or "无错误详情"
        return proposal_reply(
            False,
            "所有 VL 模型均调用失败。chain=" + "; ".join(chain_desc) + " | 错误: " + err_detail,
            options=["检查模型配置", "使用 vision_query 查询已缓存结果"],
        )

    if failed_count > 0 and read_count == 0 and cached_count == 0:
        status = "failed"
    elif failed_count > 0:
        status = "partial"
    else:
        status = "success"

    result_id_hint = ""
    if first_result_id and last_result_id:
        if first_result_id == last_result_id:
            result_id_hint = f"新结果 result_id: {first_result_id}"
        else:
            result_id_hint = f"新结果 result_id 范围: {first_result_id} ~ {last_result_id}"

    next_args: dict = {}
    if first_result_id and last_result_id and first_result_id == last_result_id:
        next_args = {"result_id": first_result_id}
    else:
        next_args = {"recent": min(len(image_paths), 10)}
        if first_result_id:
            next_args["result_id"] = first_result_id

    reply = {
        "ok": True,
        "status": status,
        "total": len(image_paths),
        "cached": cached_count,
        "read": read_count,
        "failed": failed_count,
        "proposal": "读图完成。请调用 vision_query 查看具体结果。",
        "next_call": {
            "tool": "vision_query",
            "arguments": next_args,
        },
    }
    if result_id_hint:
        reply["result_id_hint"] = result_id_hint
    if phash_cached_count > 0:
        # 近似命中透明化：调用方（LLM）应知道这些结果是「相似图」的缓存而非精确同图
        reply["cached_via_phash"] = phash_cached_count
        reply["phash_note"] = (
            f"其中 {phash_cached_count} 条命中的是感知哈希近似缓存（缩尺/重压缩的同图），"
            "内容可能与原图存在细微差异；如需精确结果请用 force_reread 重读。"
        )
    if failed_paths:
        reply["failed_paths"] = failed_paths

    return reply
