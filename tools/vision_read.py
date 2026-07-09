"""
vision_read — 批量读图并落库
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from astrbot.api import logger
from PIL import Image
import imagehash

from ._cache import VisionCache
from ._helpers import proposal_reply
from ._vl_client import read_image as vl_read_image, MAX_IMAGE_SIZE
from .config import get_vl_model_config

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_PROMPT_ZH = (
    "你是一位专业的视觉描述者，正在为一位视力障碍人士描述图片。"
    "请用自然流畅的中文段落，客观、生动地描述图片内容。"
    "包括：整体场景与背景、主要主体的外观/姿态/表情、空间布局（左、右、前景、背景）、"
    "光线（光源、对比、阴影）、色调与整体氛围。"
    "请逐字读出图中所有可见文字。"
    "严格基于可见内容进行描述，不要猜测。"
    "最后用 2-3 句话总结图片的主题或隐含叙事。"
)
DEFAULT_PROMPT_EN = (
    "You are a professional visual describer assisting a visually impaired person. "
    "Describe the image in vivid, objective detail using natural flowing paragraphs. "
    "Include: overall scene and setting, appearance/pose/expression of main subjects, "
    "spatial layout (left, right, foreground, background), lighting (source, contrasts, shadows), "
    "color palette, and the overall mood. Read out any visible text verbatim. "
    "Base your description strictly on what is visible, without speculation. "
    "End with a 2–3 sentence summary of the image's theme or implied narrative."
)
MAX_WORKERS = 5


def _check_image_size(path: str) -> None:
    size = os.path.getsize(path)
    if size > MAX_IMAGE_SIZE:
        raise ValueError(f"图片 {path} 大小 {size} 超过 {MAX_IMAGE_SIZE} 字节限制")


def _collect_image_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    results: list[str] = []
    missing: list[str] = []
    for p in paths:
        expanded = os.path.expanduser(p)
        if not os.path.exists(expanded):
            missing.append(p)
            continue
        if os.path.isdir(expanded):
            for root, _, files in os.walk(expanded):
                for f in files:
                    if Path(f).suffix.lower() in SUPPORTED_EXTS:
                        results.append(os.path.join(root, f))
        else:
            if Path(expanded).suffix.lower() in SUPPORTED_EXTS:
                results.append(expanded)
            else:
                missing.append(p)
    return sorted(set(results)), missing


def _is_chinese(text: str) -> bool:
    """简单判断文本是否包含中文字符。"""
    if not text:
        return False
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _default_prompt(question: str) -> str:
    if _is_chinese(question):
        return DEFAULT_PROMPT_ZH
    return DEFAULT_PROMPT_EN


def _compute_phash(path: str) -> str:
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        logger.warning(f"计算 phash 失败 {path}: {e}")
        return ""


def _parse_result(raw: str) -> dict:
    """从模型返回文本中解析 summary、text、tags。简单实现。"""
    summary = raw.strip().split("\n")[0] if raw.strip() else ""
    return {
        "summary": summary[:200],
        "text": raw.strip(),
        "tags": [],
    }


def _read_single_image(
    *,
    db: VisionCache,
    path: str,
    prompt: str,
    model_id: str,
    question: str,
    previous_result: dict | None,
    force_reread: bool,
) -> dict:
    """读取单张图片，返回包含 result_id 或失败信息。"""
    filename = os.path.basename(path)
    sha256 = db.sha256_of_file(path)

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
        cached = db.find_cached(sha256, filename, model_id, question)
    if cached:
        return {"type": "cached", "result_id": cached["result_id"], "path": path}

    _check_image_size(path)
    phash = _compute_phash(path)
    final_prompt = prompt + previous_context if previous_context else prompt
    raw = vl_read_image(path, final_prompt)
    parsed = _parse_result(raw)
    result_id = f"res_{uuid.uuid4().hex[:12]}"

    db.insert(
        sha256=sha256,
        filename=filename,
        phash=phash,
        model_id=model_id,
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
    )
    return {"type": "read", "result_id": result_id, "path": path}


def read(
    db: VisionCache,
    paths: list[str],
    question: str = "",
    force_reread: bool = False,
    previous_result_id: str = "",
) -> dict:
    image_paths, missing_paths = _collect_image_paths(paths)
    if not image_paths and missing_paths:
        return proposal_reply(
            False,
            f"未找到任何支持的图片文件。以下路径不存在或格式不支持：{', '.join(missing_paths[:10])}",
            options=["检查路径是否正确", "使用 vision_query 查看已有结果"],
        )

    if not image_paths:
        return proposal_reply(
            False,
            "未找到任何支持的图片文件。请确认路径存在，并且包含 png/jpg/jpeg/webp/gif/bmp 格式的图片。",
            options=["检查路径是否正确", "使用 vision_query 查看已有结果"],
        )

    prompt = question if question else _default_prompt(question)
    model_cfg = get_vl_model_config()
    model_id = model_cfg.get("model", "unknown")
    api_key = model_cfg.get("api_key", "")

    previous_result = None
    if previous_result_id:
        previous_result = db.get_by_result_id(previous_result_id)

    cached_count = 0
    read_count = 0
    failed_paths: list[str] = []
    sample_result_ids: list[str] = []
    first_result_id = ""
    last_result_id = ""
    api_key_missing = False

    # 并发读取
    workers = min(MAX_WORKERS, len(image_paths))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_path = {
            executor.submit(
                _read_single_image,
                db=db,
                path=path,
                prompt=prompt,
                model_id=model_id,
                question=question,
                previous_result=previous_result,
                force_reread=force_reread,
            ): path
            for path in image_paths
        }

        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                result = future.result()
                if result["type"] == "cached":
                    cached_count += 1
                elif result["type"] == "read":
                    read_count += 1

                result_id = result["result_id"]
                if not first_result_id:
                    first_result_id = result_id
                last_result_id = result_id
                if len(sample_result_ids) < 3:
                    sample_result_ids.append(result_id)
            except Exception as e:
                err = str(e)
                if "未配置 vl_model.api_key" in err or "api_key" in err.lower():
                    api_key_missing = True
                    failed_paths.append(f"{path} (未配置 api_key)")
                else:
                    logger.warning(f"读图失败 {path}: {e}")
                    failed_paths.append(f"{path} ({e})")

    if api_key_missing and read_count == 0 and cached_count == 0:
        return proposal_reply(
            False,
            "未配置 VL 模型的 api_key，无法读取新图片。请在 AstrBot WebUI 或 config.json 中配置 vl_model.api_key。",
            options=["去配置 api_key", "使用 vision_query 查询已缓存结果"],
        )

    range_text = ""
    if first_result_id and last_result_id:
        if first_result_id == last_result_id:
            range_text = f"新结果 result_id: {first_result_id}。"
        else:
            range_text = f"新结果 result_id 范围: {first_result_id} ~ {last_result_id}。"
    elif cached_count == len(image_paths):
        range_text = "全部命中缓存，没有新 result_id。"

    missing_text = ""
    if missing_paths:
        missing_text = f"以下路径不存在或格式不支持：{', '.join(missing_paths[:10])}。"

    return {
        "ok": True,
        "total": len(image_paths),
        "cached": cached_count,
        "read": read_count,
        "failed_count": len(failed_paths),
        "failed_paths": failed_paths[:10],
        "missing_paths": missing_paths[:10],
        "force_reread": force_reread,
        "previous_result_id": previous_result_id,
        "first_result_id": first_result_id,
        "last_result_id": last_result_id,
        "sample_result_ids": sample_result_ids,
        "proposal": "读图完成。建议先用 vision_query 查看样例或最近结果，确认读图质量后再批量处理。",
        "next_call": {
            "tool": "vision_query",
            "arguments": {"result_id": sample_result_ids[0]} if sample_result_ids else {"recent": 10},
        },
        "message": f"读图完成。共 {len(image_paths)} 张图片，{cached_count} 张命中缓存，{read_count} 张新读。{range_text}{missing_text}结果已存入数据库，请使用 vision_query 查看。",
    }
