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
from ._vl_client import (
    ImageDecodeError,
    ImageTooLargeError,
    OutputTruncatedError,
    effective_target_edge,
    encode_image,
    normalize_detail,
    read_image as vl_read_image,
)
from .config import get_max_batch, resolve_provider_chain

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# v3 提示词：刻意精简。只保留解析契约（JSON schema）与三条质量护栏
# （原文提取 / 不猜测 / 中文），把描述重点的判断权交给模型，
# 避免类型枚举清单把模型注意力锚死在清单内维度、忽略清单外的细节。
# JSON 契约放在独立后缀：追问上下文注入在 base 与 suffix 之间，
# 保证任何路径下模型最后看到的都是格式要求（previous_result_id 无 question 路径也一样）。
DEFAULT_PROMPT = (
    "请基于图片内容给出结构化描述。"
    "图中文字/数字逐字保留原文（不翻译不纠错）；只依据可见内容，"
    "看不清的部分写「看不清」；正文用中文。"
)
STRUCTURED_SUFFIX_DEFAULT = (
    "\n\n输出 JSON（json）："
    '{"peek": "一句话预览：这是什么图+最值得注意的信息", '
    '"text": "详细描述：你注意到的一切，按图片类型自行决定重点'
    '（如截图重在界面文字与状态、票据重在关键字段数值）", '
    '"tags": ["3-6个中文检索标签"]}。'
    "不要输出 JSON 以外的任何内容。"
)

# 结构化输出：要求模型返回 JSON（peek/text/tags），插件容错解析。
# prompt 中必须含 "json" 字样（DeepSeek JSON Output 的官方要求）。
STRUCTURED_SUFFIX_QUESTION = (
    "\n\n仅依据图片可见内容回答（文字/数字逐字引用原文；无法确认就明说），"
    "回答语言用中文。输出 JSON（json）："
    '{"peek": "一句话直接回答", '
    '"text": "完整回答与依据", '
    '"tags": ["3-6个检索标签"]}。'
    "不要输出 JSON 以外的任何内容。"
)


def _collect_image_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """收集图片路径，返回 (找到的图片, 未找到/不支持的传入路径)。

    顺序契约：保持传入顺序（顶层路径的顺序即用户意图——compare 的图1/图2 编号
    依赖它，before/after 不能被字典序静默调换）；目录展开时内部排序保确定性；
    按首现去重。相对路径按 AstrBot 进程 CWD 解析（agent 难以预测）——
    丢弃的路径必须回显，不能静默。"""
    results: list[str] = []
    missing: list[str] = []
    for p in paths:
        expanded = os.path.expanduser(p)
        if not os.path.exists(expanded):
            missing.append(p)
            continue
        if os.path.isdir(expanded):
            found = []
            for root, _, files in os.walk(expanded):
                for f in files:
                    if Path(f).suffix.lower() in SUPPORTED_EXTS:
                        found.append(os.path.join(root, f))
            results.extend(sorted(found))  # 目录内排序保确定性
        else:
            if Path(expanded).suffix.lower() in SUPPORTED_EXTS:
                results.append(expanded)
            else:
                missing.append(p)  # 存在但格式不支持，同样回显
    return list(dict.fromkeys(results)), missing


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
    "3-6个中文检索标签",
    "3-6个检索标签",
})


def _parse_result(raw: str) -> dict:
    """解析 VL 返回：优先按结构化 JSON（peek/text/tags）解析，
    模型不遵守格式时回退到「首行作为预览」的旧行为，读图永不因解析失败而失败。
    兼容旧缓存：读取时 peek 优先，summary 兜底（老记录/老模型输出的字段名）。"""
    text = raw.strip()
    data = _extract_json(text)
    if data is not None:  # 含空对象 {}：走结构化分支返回空字段，避免兜底把 JSON 原文当预览
        peek = str(data.get("peek") or data.get("summary") or "").strip()
        body = str(data.get("text") or "").strip()
        # 占位符被原样照抄时视为无内容，回退处理
        if peek in _PLACEHOLDER_VALUES:
            peek = ""
        if body in _PLACEHOLDER_VALUES:
            body = ""
        tags_raw = data.get("tags")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        tags = [t for t in tags if t not in _PLACEHOLDER_VALUES]  # 剔除照抄的占位符标签
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
    image_paths, missing = _collect_image_paths(paths)
    if not image_paths:
        detail = ""
        if missing:
            detail = (
                f"未找到或不支持的路径: {missing}。"
                "注意：相对路径按 AstrBot 进程的工作目录解析（不是对话中的目录），建议用绝对路径或 ~。"
            )
        return proposal_reply(
            False,
            "未找到任何支持的图片文件。请确认路径存在，并且包含 png/jpg/jpeg/webp/gif/bmp 格式的图片。" + detail,
            options=["检查路径是否正确", "使用绝对路径或 ~ 家目录", "使用 vision_query 查看已有结果"],
        )
    # 账单保险丝：批量读图按张调用 VL 模型计费，误传大目录会产生失控费用
    max_batch = get_max_batch()
    if len(image_paths) > max_batch:
        return proposal_reply(
            False,
            f"找到 {len(image_paths)} 张图片，超过单次批量上限 {max_batch} 张（可在插件配置 max_batch 调整）。"
            "请分批传入子目录，避免失控的 VL 调用费用。",
            options=["分批传入子目录", "调大 max_batch 配置后重试", "使用 vision_query 查看已有结果"],
        )
    # 默认/追问模式都有 JSON 契约后缀：追问上下文注入在 base 与 suffix 之间，
    # 保证任何路径下模型最后看到的都是格式要求
    base_prompt = question if question else DEFAULT_PROMPT
    prompt_suffix = STRUCTURED_SUFFIX_QUESTION if question else STRUCTURED_SUFFIX_DEFAULT

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
    decode_failed_count = 0  # 解码失败单独计数：与模型无关，不归因「VL 模型调用失败」
    failed_paths: list[str] = []
    semaphore = asyncio.Semaphore(concurrency)
    # 每张图的实际结果记录（命中给缓存 id、新读给新 id）：
    # 批量含命中时 next_call 用 result_ids 精确指向这批记录，而非 recent 撞运气
    result_ids_by_path: dict = {}

    import httpx
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    _httpx_client = httpx.AsyncClient(timeout=vl_timeout, limits=limits)

    async def _read_one(path: str) -> None:
        nonlocal cached_count, phash_cached_count, read_count, failed_count, decode_failed_count

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
                # 命中路径登记 result_id（命中给缓存 id）：单图命中时 next_call
                # 精确指向该记录，而非退化成 recent 指向库中最新记录（可能是无关图）
                rid = cached.get("result_id", "")
                if rid:
                    result_ids_by_path[path] = rid
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
                    # 同精确命中：登记 result_id。此时指向「相似图」记录，
                    # 配套的 phash_note 已在响应中提示差异，语义自洽
                    rid = cached.get("result_id", "")
                    if rid:
                        result_ids_by_path[path] = rid
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
            # 重试/降级间按压缩档位复用编码结果：同一张图的压缩+base64 只做一次，
            # 避免 (retries+1)×链长 次重复编码（降级链上 detail 一致，档位只随模型类型变）
            encoded_urls: dict = {}
            for vl_cfg in chain:
                if not vl_cfg.get("api_key"):
                    continue
                for attempt in range(max_retries + 1):
                    async with semaphore:
                        try:
                            edge = effective_target_edge(vl_cfg.get("model", ""), vl_cfg.get("detail", "auto"))
                            if edge not in encoded_urls:
                                encoded_urls[edge] = await run_sync(encode_image, path, edge)
                            raw = await vl_read_image(
                                path, final_prompt, client=_httpx_client, vl_config=vl_cfg,
                                json_mode=True, image_url=encoded_urls[edge],
                            )
                            last_err = None
                            used_model = vl_cfg.get("model", "unknown")
                            used_detail = normalize_detail(vl_cfg.get("detail", "auto"))
                            break
                        except ImageTooLargeError as e:
                            # 同一 provider 重试无意义（同压缩档位重编码结果相同）；
                            # 但压缩档位按模型类型分档（非DS 2048 / DS 1024），
                            # 降级到更小档位的模型可能通过 → break 进降级链
                            last_err = e
                            break
                        except ImageDecodeError:
                            raise  # 图片损坏：与模型无关，不重试不降级，直接出循环
                        except OutputTruncatedError as e:
                            # 额度已在客户端内部放大重试过，同一模型重试无意义；
                            # 但截断是模型相关的（思考型模型易触发），降级换模型可能成功
                            last_err = e
                            break
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
            if not parsed["peek"] and not parsed["text"]:
                # 模型返回了空内容 JSON（如 {"tags": [...]} / {}）：
                # 视为本次读图失败，不落库——否则空记录会永久占用缓存键，
                # 后续命中返回空内容且 agent 无任何失败信号
                raise ValueError(f"模型返回内容结构化解析后为空: {raw[:100]}")
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
            result_ids_by_path[path] = result_id
        except ImageDecodeError as e:
            # 解码失败（0 字节/截断/损坏）：单独计数，不归因模型失败
            logger.warning(f"图片解码失败 {path}: {e}")
            decode_failed_count += 1
            if len(failed_paths) < 10:
                failed_paths.append(f"{path}: {e}")
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

    total_failed = failed_count + decode_failed_count
    if read_count == 0 and cached_count == 0 and total_failed > 0:
        if failed_count == 0:
            # 全是解码失败：与模型无关——不归因模型、不 dump provider 链，并保留 failed 计数
            return proposal_reply(
                False,
                f"{decode_failed_count} 张图片无法解码（文件损坏/0 字节/格式不支持），未调用 VL 模型。",
                error=failed_paths[0] if failed_paths else "",
                options=["检查图片文件是否完整", "移除损坏文件后重试"],
                failed=decode_failed_count,
                failed_paths=failed_paths,
            )
        chain_desc = [
            f"{c.get('model','?')}@{c.get('base_url','')[:40]} key={'有' if c.get('api_key') else '无'}"
            for c in chain
        ]
        err_detail = " | ".join(failed_paths[:3]) or "无错误详情"
        fail_reply = proposal_reply(
            False,
            "所有 VL 模型均调用失败。chain=" + "; ".join(chain_desc) + " | 错误: " + err_detail,
            options=["检查模型配置", "使用 vision_query 查询已缓存结果"],
        )
        fail_reply["failed"] = total_failed
        if decode_failed_count:
            fail_reply["decode_failed"] = decode_failed_count
        return fail_reply

    if total_failed > 0 and read_count == 0 and cached_count == 0:
        status = "failed"
    elif total_failed > 0:
        status = "partial"
    else:
        status = "success"

    # 与传入顺序一致的结果 id 序列（命中给缓存 id、新读给新 id；失败的不在内；
    # 相同内容的多个路径解析为同一记录，去重保首现——重复 id 对查询无意义）。
    # hint 与 next_call 都从这里构造：ordered_ids 是传入序（确定性），
    # 不用「谁先完成谁当 first」的并发完成序变量（竞态产物，两次跑结果不同）。
    ordered_ids: list = []
    for p in image_paths:
        rid = result_ids_by_path.get(p)
        if rid and rid not in ordered_ids:
            ordered_ids.append(rid)

    result_id_hint = ""
    if ordered_ids:
        # 三态文案：纯新读=新结果 / 纯命中=命中结果 / 混合=结果（混合批量下
        # 范围起点可能是命中记录，写「新结果」自相矛盾）
        if read_count > 0 and cached_count > 0:
            label = "结果"
        elif read_count > 0:
            label = "新结果"
        else:
            label = "命中结果"
        if len(ordered_ids) == 1:
            result_id_hint = f"{label} result_id: {ordered_ids[0]}"
        else:
            result_id_hint = (
                f"{label} result_id 范围: {ordered_ids[0]} ~ {ordered_ids[-1]}"
                f"（按传入顺序，共 {len(ordered_ids)} 条）"
            )

    next_args: dict = {}
    if len(ordered_ids) == 1:
        # 单条结果（单图，或同内容多路径去重后）：目标明确，直接 full 查这条
        next_args = {"result_id": ordered_ids[0]}
    elif cached_count > 0 and ordered_ids:
        # 批量含命中（纯命中/混合）：result_ids 精确指向本次涉及的记录——
        # recent=N 按时间倒序会撞上无关的最新记录，把命中集合漏掉
        next_args = {"result_ids": ordered_ids[:10]}
    else:
        # 纯新读批量：只给 recent（list 模式浏览），不夹带 result_id——
        # 否则 vision_query 里 result_id 优先级最高，会直接 full 第一张而跳过其余
        next_args = {"recent": min(len(image_paths), 10)}

    reply = {
        "ok": True,
        "status": status,
        "total": len(image_paths),
        "cached": cached_count,
        "read": read_count,
        "failed": total_failed,
        "proposal": "读图完成。请调用 vision_query 查看具体结果。",
        "next_call": {
            "tool": "vision_query",
            "arguments": next_args,
        },
    }
    if decode_failed_count:
        reply["decode_failed"] = decode_failed_count
    if missing:
        # 部分传入路径未找到/不支持：必须回显，静默丢图会让 agent 基于不完整集合下结论
        reply["missing_paths"] = missing
    if cached_count > 0 and len(ordered_ids) > 10:
        # result_ids cap 10 的截断必须告知，否则超出的记录通过 next_call 永远够不着
        reply["unlisted_count"] = len(ordered_ids) - 10
        reply["proposal"] += f"（本次共 {len(ordered_ids)} 条记录，next_call 列出前 10 个 result_id，其余可按 path/query 查询）"
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
