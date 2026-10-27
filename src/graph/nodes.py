"""执行图管线节点名常量（T-E1 定义，T-E4 复用）。

独立成模块的原因（技术性，非语义变更）：``gate.py`` / ``path_assert.py`` 需要这些
常量，而 ``workflow.py`` 已在模块级 import ``gate``——若它们反过来 import
``workflow`` 会形成环。本模块零依赖，处于 import 图的叶子位置。

节点语义与顺序权威：MVP 任务拆解 T-E1「segment → decision → **confirm_plan** →
gate → send」；``confirm_plan`` 纳入 ``REQUIRED_NODES``（ADR-035 / 裁决 #7）。
"""

from __future__ import annotations

__all__ = [
    "NODE_CONFIRM_PLAN",
    "NODE_DECISION",
    "NODE_GATE",
    "NODE_SEGMENT",
    "NODE_SEND",
    "PIPELINE_NODES",
]

NODE_SEGMENT = "segment"
NODE_DECISION = "decision"
NODE_CONFIRM_PLAN = "confirm_plan"
NODE_GATE = "gate"
NODE_SEND = "send"

PIPELINE_NODES: tuple[str, ...] = (
    NODE_SEGMENT,
    NODE_DECISION,
    NODE_CONFIRM_PLAN,
    NODE_GATE,
    NODE_SEND,
)
