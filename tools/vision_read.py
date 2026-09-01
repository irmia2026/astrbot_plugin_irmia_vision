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
# v2 提示词：目标从「为视障人士描述」改为「为智能体提供事实性、可检索的结构化档案」，
# 按图片类型分流描述重点，文字要求逐字原文提取，明确「看不清」处理。
DEFAULT_PROMPT = (
    "你是一位图像分析助手。请基于图片内容给出事实性、可检索的结构化输出，"
    "供智能体后续查询与决策使用。\n\n"
    "【图片类型】先判断图片属于哪一类：照片 / 截图 / 文档 / 表格 / 发票票据 / 代码 / "
    "UI界面 / 图表 / 其他，并据此调整描述重点。\n\n"
    "【text 字段：按图片类型组织详细内容】\n"
    "- 截图/UI界面：程序或界面名称、布局、菜单/按钮文字、操作状态（前台窗口、运行中、报错、进度等）\n"
    "- 文档/发票/票据：文档类型、抬头、关键字段与数值（金额、日期、编号等，逐字准确）\n"
    "- 代码：逐字符抄写代码内容，并说明语言与用途\n"
    "- 照片：场景、主体（外观/姿态/表情）、空间布局、光线、色调\n"
    "- 图表：图表类型、坐标含义、关键数据点与趋势\n"
    "图中所有可见文字请保留原文逐字提取（不翻译、不改写、不纠错）；描述语言用中文。\n"
    "仅依据图中可见内容，不要推测画面外信息；看不清/被遮挡/模糊的部分明确写「看不清」。\n\n"
    "【peek 字段】一句话（不超过80字）概括：图片类型 + 主要内容 + 值得注意的关键信息"
    "（如金额、报错、人名、数字），让没看图的人快速建立预期。\n\n"
    "【tags 字段】3-6 个中文名词短语标签，便于检索：类型 + 主体 + 场景/领域 + 关键实体"
    "（例如：发票、报销单、金额100元、扫描件）。"
)

# 结构化输出：要求模型返回 JSON（peek/text/tags），插件容错解析。
# prompt 中必须含 "json" 字样（DeepSeek JSON Output 的官方要求）。
STRUCTURED_SUFFIX_DEFAULT = (
    "\n\n【输出格式】只输出 JSON 对象：\n"
    '{"peek": "一句话预览（不超过80字）", '
    '"text": "按上述要求组织的详细内容", '
    '"tags": ["3-6 个中文标签"]}\n'
    "不要输出 JSON 以外的任何内容。"
)
STRUCTURED_SUFFIX_QUESTION = (
    "\n\n请基于图片内容回答问题，遵循以下规则：\n"
    "1. 只依据图片中可见的信息，不要推测或编造；看不清/无法确认的部分明确写「图片中无法确认」。\n"
    "2. 涉及图中文字/数字时，保留原文逐字引用，不翻译、不改写、不纠错。\n"
    "3. 回答语言用中文。\n\n"
    "【输出格式】只输出 JSON 对象：\n"
    '{"peek": "用一句话直接回答（不超过80字）", '
    '"text": "完整回答，包含关键依据（引用的原文/数字/位置）", '
    '"tags": ["3-6 个中文标签"]}\n'
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


def _parse_result(raw: str) -> dict:
    """解析 VL 返回：优先按结构化 JSON（peek/text/tags）解析，
    模型不遵守格式时回退到「首行作为预览」的旧行为，读图永不因解析失败而失败。
    兼容旧缓存：读取时 peek 优先，summary 兜底（老记录/老模型输出的字段名）。"""
    text = raw.strip()
    data = _extract_json(text)
    if data:
        peek = str(data.get("peek") or data.get("summary") or "").strip()
        body = str(data.get("text") or "").strip()
        tags_raw = data.get("tags")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        if peek or body:
            return {
                "peek": (peek or body.split("\n")[0])[:200],
                "text": body or peek,
                "tags": tags,
            }
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
                    previous_peek = previous_result.get("peek", "") or previous_result.get("summary", "")
                    previous_text = previous_result.get("text", "")
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
