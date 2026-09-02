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
# v3 提示词：刻意精简。只保留解析契约（JSON schema）与三条质量护栏
# （原文提取 / 不猜测 / 中文），把描述重点的判断权交给模型，
# 避免类型枚举清单把模型注意力锚死在清单内维度、忽略清单外的细节。
DEFAULT_PROMPT = (
    "请基于图片内容输出 JSON（json）："
    '{"peek": "一句话预览：这是什么图+最值得注意的信息", '
    '"text": "详细描述：你注意到的一切，按图片类型自行决定重点'
    '（如截图重在界面文字与状态、票据重在关键字段数值）", '
    '"tags": ["3-6个中文检索标签"]}\n'
    "要求：图中文字/数字逐字保留原文（不翻译不纠错）；只依据可见内容，"
    "看不清的部分写「看不清」；text 用中文。不要输出 JSON 以外的任何内容。"
)

# 结构化输出：要求模型返回 JSON（peek/text/tags），插件容错解析。
# prompt 中必须含 "json" 字样（DeepSeek JSON Output 的官方要求）。
STRUCTURED_SUFFIX_QUESTION = (
    "\n\n仅依据图片可见内容回答（文字/数字逐字引用原文；无法确认就明说），"
    "输出 JSON（json）："
    '{"peek": "一句话直接回答", '
    '"text": "完整回答与依据", '
    '"tags": ["3-6个检索标签"]}。'
    "不要输出 JSON 以外的任何内容。"
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


# 提示词中 JSON 骨架的示例值——弱模型可能照抄占位符原文落库，解析时识别并剔除
_PLACEHOLDER_VALUES = frozenset({
    "一句话预览：这是什么图+最值得注意的信息",
    "一句话直接回答",
    "详细描述：你注意到的一切，按图片类型自行决定重点（如截图重在界面文字与状态、票据重在关键字段数值）",
    "完整回答与依据",
})


def _parse_result(raw: str) -> dict:
    """解析 VL 返回：优先按结构化 JSON（peek/text/tags）解析，
    模型不遵守格式时回退到「首行作为预览」的旧行为，读图永不因解析失败而失败。
    兼容旧缓存：读取时 peek 优先，summary 兜底（老记录/老模型输出的字段名）。"""
    text = raw.strip()
    data = _extract_json(text)
    if data:
        peek = str(data.get("peek") or data.get("summary") or "").strip()
        body = str(data.get("text") or "").strip()
        # 占位符被原样照抄时视为无内容，回退处理
        if peek in _PLACEHOLDER_VALUES:
            peek = ""
        if body in _PLACEHOLDER_VALUES:
            body = ""
        tags_raw = data.get("tags")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        if peek or body:
            return {
                "peek": (peek or body.split("\n")[0])[:200],
                "text": body or peek,
                "tags": tags,
            }
        # 提取到了 JSON 但无实质内容（如只有 tags）：返回空字段，
        # 避免兜底路径把 JSON 原文第一行当预览落库
        return {"peek": "", "text": "", "tags": tags}
    peek = text.split("\n")[0] if text else ""
    return {
        "peek": peek[:200],
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
    # 默认模式的 JSON 要求已并入 DEFAULT_PROMPT，无需后缀；追问模式才需要规则+格式后缀
    base_prompt = question if question else DEFAULT_PROMPT
    prompt_suffix = STRUCTURED_SUFFIX_QUESTION if question else ""

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
                    previous_peek = (previous_result.get("peek", "") or previous_result.get("summary", ""))[:200]
                    previous_text = previous_result.get("text", "")[:1000]  # 上限防超长上文
                    previous_context = f"\n之前对这张图的理解：{previous_peek}\n提取的文字：{previous_text}\n"

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

            final_prompt = base_prompt + previous_context + prompt_suffix

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
                peek=parsed["peek"],
                text=parsed["text"],
                tags=parsed["tags"],
                result_json={
                    "peek": parsed["peek"],
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
        # 单图：目标明确，直接 full 查这条
        next_args = {"result_id": first_result_id}
    else:
        # 批量：只给 recent（list 模式浏览全部预览），不夹带 result_id——
        # 否则 vision_query 里 result_id 优先级最高，会直接 full 第一张而跳过其余
        next_args = {"recent": min(len(image_paths), 10)}

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
