"""
vision_read — 批量读图并落库
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from astrbot.api import logger
from PIL import Image
import imagehash

from ._cache import VisionCache
from ._vl_client import read_image as vl_read_image
from ._helpers import proposal_reply

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_PROMPT = (
    "You are a professional visual describer assisting a visually impaired person. "
    "Describe the image in vivid, objective detail using natural flowing paragraphs. "
    "Include: overall scene and setting, appearance/pose/expression of main subjects, "
    "spatial layout (left, right, foreground, background), lighting (source, contrasts, shadows), "
    "color palette, and the overall mood. Read out any visible text verbatim. "
    "Base your description strictly on what is visible, without speculation. "
    "End with a 2–3 sentence summary of the image's theme or implied narrative."
)


def _collect_image_paths(paths: list[str]) -> list[str]:
    results: list[str] = []
    for p in paths:
        if not os.path.exists(p):
            continue
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    if Path(f).suffix.lower() in SUPPORTED_EXTS:
                        results.append(os.path.join(root, f))
        else:
            if Path(p).suffix.lower() in SUPPORTED_EXTS:
                results.append(p)
    return sorted(set(results))


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


def read(
    db: VisionCache,
    paths: list[str],
    question: str = "",
    force_reread: bool = False,
    previous_result_id: str = "",
) -> dict:
    image_paths = _collect_image_paths(paths)
    if not image_paths:
        return proposal_reply(
            False,
            "未找到任何支持的图片文件。请确认路径存在，并且包含 png/jpg/jpeg/webp/gif/bmp 格式的图片。",
            options=["检查路径是否正确", "使用 vision_query 查看已有结果"],
        )

    prompt = question if question else DEFAULT_PROMPT
    cached_count = 0
    read_count = 0
    first_result_id = ""
    last_result_id = ""

    model_cfg = __import__(
        "astrbot_plugin_irmia_vision.tools.config", fromlist=["get_vl_model_config"]
    ).get_vl_model_config()
    model_id = model_cfg.get("model", "unknown")

    previous_context = ""
    if previous_result_id:
        previous = db.get_by_result_id(previous_result_id)
        if previous:
            previous_summary = previous.get("summary", "")
            previous_text = previous.get("text", "")
            previous_context = f"\n之前对这张图的理解：{previous_summary}\n提取的文字：{previous_text}\n"

    for path in image_paths:
        try:
            filename = os.path.basename(path)
            sha256 = db.sha256_of_file(path)

            is_follow_up = bool(previous_result_id) or force_reread
            cached = None
            if not is_follow_up:
                cached = db.find_cached(sha256, filename, model_id, question)
            if cached:
                cached_count += 1
                continue

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
            read_count += 1
            if not first_result_id:
                first_result_id = result_id
            last_result_id = result_id
        except Exception as e:
            logger.warning(f"读图失败 {path}: {e}")

    range_text = ""
    if first_result_id and last_result_id:
        if first_result_id == last_result_id:
            range_text = f"新结果 result_id: {first_result_id}。"
        else:
            range_text = f"新结果 result_id 范围: {first_result_id} ~ {last_result_id}。"
    elif cached_count == len(image_paths):
        range_text = "全部命中缓存，没有新 result_id。"

    return {
        "ok": True,
        "total": len(image_paths),
        "cached": cached_count,
        "read": read_count,
        "force_reread": force_reread,
        "previous_result_id": previous_result_id,
        "first_result_id": first_result_id,
        "last_result_id": last_result_id,
        "proposal": "读图完成。请用 vision_query 查看最近结果或按关键词搜索。",
        "next_call": {
            "tool": "vision_query",
            "arguments": {"recent": min(read_count, 10) or 10},
        },
        "message": f"读图完成。共 {len(image_paths)} 张图片，{cached_count} 张命中缓存，{read_count} 张新读。{range_text}结果已存入数据库，请使用 vision_query 查看。",
    }
