"""
工具使用统计
"""

from __future__ import annotations

_call_counts: dict[str, int] = {}


def record(name: str) -> None:
    _call_counts[name] = _call_counts.get(name, 0) + 1


def snapshot() -> dict:
    return dict(_call_counts)
