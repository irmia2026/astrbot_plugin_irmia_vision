"""
vision_compare — 多图对比询问（单次 VL 请求携带多张图片）

与 vision_read 的差异：
- vision_read：每张图一次请求，逐图落库，单图内容寻址缓存（sha256）。
- vision_compare：整组图一次请求（模型同时看到所有图才能做跨图判断），
  结论直接返回；缓存键为「组指纹」（成员 sha256 排序后联合哈希），
  与 paths 顺序、文件名无关。

DeepSeek 多图契约（api-docs.deepseek.com/zh-cn/guides/vision）：
- 多个 image_url 块放同一条 user 消息（system/assistant 携带图片返回 400）；
- 每张图独立按同一规则缩放计费（≤1024 token/张），多图无额外算法；
- 请求体 48 MiB；内联图片总大小 ≤64 MiB；≥15 张时单边上限降为 4096px。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import httpx

from astrbot.api import logger

from ._helpers import proposal_reply, run_sync
from ._store import VisionStore
from ._vl_client import (
    MAX_COMPARE_IMAGES,
    ImageTooLargeError,
    OutputTruncatedError,
    effective_target_edge,
    encode_image,
    normalize_detail,
    read_images as vl_read_images,
)
from .config import resolve_provider_chain
from .vision_read import _collect_image_paths, _parse_result

# 与 vision_read 提示词 v3 同一设计：不设对比维度枚举清单，判断权交给模型
DEFAULT_COMPARE_PROMPT = (
    "请对比这组图片：先逐图概括关键内容（用图1/图2…引用），再指出相同点与不同点，最后给出结论。"
    "图中文字/数字逐字保留原文（不翻译不纠错）；只依据可见内容，看不清的部分写「看不清」；正文用中文。"
)
STRUCTURED_SUFFIX_COMPARE = (
    "\n\n输出 JSON（json）："
    '{"peek": "一句话对比结论", '
    '"text": "完整对比分析：逐图要点 → 相同点 → 不同点 → 结论（用图1/图2…引用具体图片）", '
    '"tags": ["3-6个中文检索标签"]}。'
    "不要输出 JSON 以外的任何内容。"
)
STRUCTURED_SUFFIX_COMPARE_QUESTION = (
    "\n\n仅依据这组图片的可见内容回答对比问题（文字/数字逐字引用原文；无法确认就明说），"
    "回答中用图1/图2…引用具体图片，语言用中文。输出 JSON（json）："
    '{"peek": "一句话直接回答", '
    '"text": "完整回答与依据", '
    '"tags": ["3-6个检索标签"]}。'
    "不要输出 JSON 以外的任何内容。"
)


def _group_sha256(member_sha256s: list[str]) -> str:
    """对比组的缓存指纹：成员 sha256 排序后联合哈希。

    与 paths 顺序、文件名无关——同一组图片任意顺序传入都命中同一缓存键。
    grp_ 前缀使组记录与单图记录（裸 sha256）在库中可区分。
    """
    h = hashlib.sha256()
    for s in sorted(member_sha256s):
        h.update(s.encode("ascii"))
        h.update(b"\0")
    return "grp_" + h.hexdigest()


def _ok_reply(*, result_id: str, peek: str, text: str, tags: list, members: list[dict], question: str, cached: bool) -> dict:
    """对比场景与 vision_read 不同：结论就是交付物，直接返回（4000 字上限保护上下文），
    同时已落库，可用 result_id 在 vision_query 复查完整记录。"""
    return {
        "ok": True,
        "status": "cached" if cached else "success",
        "result_id": result_id,
        "images": len(members),
        "cached": cached,
        "question": question,
        "peek": peek,
        "text": text[:4000] + ("..." if len(text) > 4000 else ""),
        "tags": tags,
        "filenames": [m["filename"] for m in members],
        "proposal": "对比完成，结论已直接返回并落库。如需复查完整记录（含逐图成员与模型原文），用 result_id 精确查询。",
        "next_call": {
            "tool": "vision_query",
            "arguments": {"result_id": result_id},
        },
    }


async def compare(
    db: VisionStore,
    paths: list[str],
    question: str = "",
    force_reread: bool = False,
    previous_result_id: str = "",
) -> dict:
    image_paths = _collect_image_paths(paths)
    if len(image_paths) < 2:
        return proposal_reply(
            False,
            f"多图对比至少需要 2 张图片（找到 {len(image_paths)} 张）。请确认路径存在且包含 png/jpg/jpeg/webp/gif/bmp 图片。",
            options=["检查路径是否正确", "单张图片追问用 vision_read"],
        )
    if len(image_paths) > MAX_COMPARE_IMAGES:
        return proposal_reply(
            False,
            f"找到 {len(image_paths)} 张图片，超过单次对比上限 {MAX_COMPARE_IMAGES} 张。请挑选关键图片分批对比，或先用 vision_read 逐张读取。",
            options=["减少图片数量", "用 vision_read 逐张读取后用 vision_query 汇总"],
        )

    # 成员内容指纹 + 按内容去重（同一张图重复传入对对比无意义）
    members: list[dict] = []
    seen: set[str] = set()
    for p in image_paths:
        sha256 = await run_sync(db.sha256_of_file, p)
        if sha256 in seen:
            continue
        seen.add(sha256)
        members.append({"path": p, "filename": os.path.basename(p), "sha256": sha256})
    if len(members) < 2:
        return proposal_reply(
            False,
            "去重后只剩 1 张不同内容的图片，对比至少需要 2 张不同内容的图片。",
            options=["检查是否重复传入了同一张图", "单张图片追问用 vision_read"],
        )

    group_hash = _group_sha256([m["sha256"] for m in members])

    chain = resolve_provider_chain()
    primary = chain[0] if chain else None
    max_retries = max(0, int((primary or {}).get("max_retries", 2)))
    # 用降级链中最大的 timeout 构建共享 client，避免 fallback 被 primary 的短 timeout 截断
    vl_timeout = max(float(cfg.get("timeout", 120.0)) for cfg in chain) if chain else 120.0
    cache_detail = normalize_detail((primary or {}).get("detail", "auto"))

    # 追问：仅当 previous_result_id 指向同一组图片（组指纹相同）时注入上文，
    # 避免把别组图片的对比结论污染进本组
    previous_context = ""
    is_follow_up = False
    if previous_result_id:
        previous_result = await run_sync(db.get_by_result_id, previous_result_id)
        if previous_result and previous_result.get("sha256", "") == group_hash:
            is_follow_up = True
            prev_peek = (previous_result.get("peek", "") or previous_result.get("summary", ""))[:200]
            prev_text = previous_result.get("text", "")[:1000]  # 上限防超长上文
            previous_context = f"\n之前对这组图片的对比理解：{prev_peek}\n对比内容：{prev_text}\n"

    # 组缓存：同组 + 同模型 + 同问题 + 同 detail 命中。
    # phash 近似兜底对多图组无对应物（组指纹不是图片），不参与；
    # 组记录落库 phash=""，天然被近似匹配的双侧纯色守卫排除。
    if not is_follow_up and not force_reread:
        cached = await run_sync(db.find_cached, group_hash, (primary or {}).get("model", ""), question, cache_detail)
        if cached:
            try:
                cached_tags = json.loads(cached.get("tags") or "[]")
            except Exception:
                cached_tags = []
            return _ok_reply(
                result_id=cached["result_id"],
                peek=cached.get("peek", ""),
                text=cached.get("text", ""),
                tags=cached_tags if isinstance(cached_tags, list) else [],
                members=members,
                question=question,
                cached=True,
            )

    # 不在组缓存命中之前拦截空链：命中缓存不需要 VL 配置；走到这里才必须要求模型可用
    if not chain:
        return proposal_reply(
            False,
            "未配置任何 VL 模型（vl_provider_ids 为空且 vl_model 缺少 api_key），无法进行对比。",
            options=["检查模型配置"],
        )

    base_prompt = question if question else DEFAULT_COMPARE_PROMPT
    prompt_suffix = STRUCTURED_SUFFIX_COMPARE_QUESTION if question else STRUCTURED_SUFFIX_COMPARE
    # 追问上文注入在 base 与 suffix 之间：模型最后看到的永远是 JSON 格式要求
    final_prompt = base_prompt + previous_context + prompt_suffix

    member_paths = [m["path"] for m in members]
    labels = [f"图{i + 1}（{m['filename']}）" for i, m in enumerate(members)]

    raw = ""
    used_model = ""
    used_detail = cache_detail
    last_err: Exception | None = None
    # 重试/降级间按压缩档位复用编码结果：多图场景重复压缩的浪费会被图片数放大
    encoded: dict = {}
    async with httpx.AsyncClient(timeout=vl_timeout) as client:
        for vl_cfg in chain:
            if not vl_cfg.get("api_key"):
                continue
            for attempt in range(max_retries + 1):
                try:
                    edge = effective_target_edge(vl_cfg.get("model", ""), vl_cfg.get("detail", "auto"))
                    if edge not in encoded:
                        encoded[edge] = [await run_sync(encode_image, p, edge) for p in member_paths]
                    raw = await vl_read_images(
                        member_paths,
                        final_prompt,
                        labels=labels,
                        client=client,
                        vl_config=vl_cfg,
                        json_mode=True,
                        image_urls=encoded[edge],
                    )
                    last_err = None
                    used_model = vl_cfg.get("model", "unknown")
                    used_detail = normalize_detail(vl_cfg.get("detail", "auto"))
                    break
                except ImageTooLargeError as e:
                    # 同一 provider 重试无意义；但压缩档位按模型类型分档（非DS 2048 / DS 1024）——
                    # 16 张 2048 档大图可破 40MB 内联预算，DS 备用的 1024 档本可通过，
                    # 直接返回会掐死 fallback → break 进降级链
                    last_err = e
                    break
                except OutputTruncatedError as e:
                    # 额度已在客户端内部放大重试过，同一模型重试无意义；降级换模型可能成功
                    last_err = e
                    break
                except Exception as e:
                    last_err = e
                if attempt < max_retries:
                    logger.warning(f"对比重试 (provider={vl_cfg.get('model','')}): {last_err}")
                    await asyncio.sleep(1.0)
            if last_err is None:
                if raw:
                    break  # 调用成功且返回内容
                # 调用成功但返回空内容（如模型不支持视觉输入）→ 视为失败，继续降级
                last_err = ValueError(f"模型 {vl_cfg.get('model','')} 返回空内容")
            logger.warning(f"provider {vl_cfg.get('model','')} 失败，尝试降级: {last_err}")

    if last_err is not None:
        chain_desc = [
            f"{c.get('model','?')}@{c.get('base_url','')[:40]} key={'有' if c.get('api_key') else '无'}"
            for c in chain
        ]
        return proposal_reply(
            False,
            "所有 VL 模型均调用失败。chain=" + "; ".join(chain_desc) + f" | 错误: {last_err}",
            options=["检查模型配置", "减少图片数量"],
        )
    if not raw:
        return proposal_reply(False, "所有 VL 模型均返回空内容，无法对比", options=["检查模型配置"])

    parsed = _parse_result(raw)
    if not parsed["peek"] and not parsed["text"]:
        # 与 vision_read 同一防护：空内容不落库——否则空记录会永久占用组缓存键
        return proposal_reply(
            False,
            f"模型返回内容结构化解析后为空: {raw[:100]}",
            options=["重试", "换个对比问题"],
        )

    result_id = f"cmp_{uuid.uuid4().hex[:12]}"
    filename_label = f"[对比{len(members)}图] " + " + ".join(m["filename"] for m in members)
    await run_sync(
        db.insert,
        sha256=group_hash,
        filename=filename_label[:200],
        phash="",  # 组记录无 phash 对应物；空值被近似匹配的双侧守卫天然排除
        model_id=used_model or (primary or {}).get("model", "unknown"),
        question=question,
        result_id=result_id,
        source_value=json.dumps(member_paths, ensure_ascii=False),
        peek=parsed["peek"],
        text=parsed["text"],
        tags=parsed["tags"],
        result_json={
            "kind": "compare",
            "peek": parsed["peek"],
            "text": parsed["text"],
            "tags": parsed["tags"],
            "raw": raw,
            "members": members,
        },
        detail=used_detail,
    )

    return _ok_reply(
        result_id=result_id,
        peek=parsed["peek"],
        text=parsed["text"],
        tags=parsed["tags"],
        members=members,
        question=question,
        cached=False,
    )
