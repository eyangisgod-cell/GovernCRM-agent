"""T-E4 端到端：跳过 confirm_plan 的执行路径在 gate 被 DENY（真实接线，红线④演示）。

权威依据：

- **PRD v5.1:514（E14）** + **架构 v5.2:206（⓪ 路径完整性断言，guard/gate 入口最先执行）**：
  trace 缺失必经节点 → 一律 DENY + 告警（E14），进人工审查。
- **ADR-035 / 裁决 #7**：confirm_plan 是图内必经人工节点，纳入 ``REQUIRED_NODES``，
  跳过即 DENY——本文件用短路图证明「结构性绕过」在 gate 被真实拦截。

构造方式（任务 prd ②允许）：测试内拼一条 decision 直连 gate 的短路图 + 直调 ``run_gate``。
生产图（``workflow.build_graph``）无此边——结构保证由 ``test_workflow_structure`` 守护。
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.graph import gate, workflow
from src.graph.nodes import (
    NODE_CONFIRM_PLAN,
    NODE_DECISION,
    NODE_GATE,
    NODE_SEGMENT,
    NODE_SEND,
)
from src.graph.workflow import GraphState

pytestmark = pytest.mark.unit

#: 跳过 confirm_plan 的 gate 入口轨迹（短路图实际记录的 visited 前缀）。
_GATE_ARRIVAL_SKIPPING_CONFIRM = [NODE_SEGMENT, NODE_DECISION]


def _short_circuit_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """测试内短路图：decision 直连 gate（结构性跳过 confirm_plan）——生产图禁止此边。"""
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(GraphState)
    graph.add_node(NODE_SEGMENT, workflow._segment_node)
    graph.add_node(NODE_DECISION, workflow._decision_node)
    graph.add_node(NODE_GATE, workflow._gate_node)
    graph.add_node(NODE_SEND, workflow._send_node)
    graph.add_edge(START, NODE_SEGMENT)
    graph.add_edge(NODE_SEGMENT, NODE_DECISION)
    graph.add_edge(NODE_DECISION, NODE_GATE)  # 短路：跳过 confirm_plan（红线④ 负向）
    graph.add_edge(NODE_GATE, NODE_SEND)
    graph.add_edge(NODE_SEND, END)
    return graph.compile()


def test_short_circuit_graph_denied_at_gate_with_e14() -> None:
    """端到端：跳过 confirm_plan 的图执行到 gate → DENY + E14 + 不发出任何候选。"""
    final = _short_circuit_graph().invoke(workflow.dry_run_initial_state())

    assert final.get("gate_outcome") == "deny", "跳过 confirm_plan 必须在 gate 被 DENY"
    marker = str(final.get("approval_marker", ""))
    assert "E14" in marker, f"告警码 E14 必须出现在 DENY 标记里：{marker}"
    assert "PATH_ASSERT" in marker, f"rule_id=PATH_ASSERT 必须可审计反查：{marker}"
    assert "confirm_plan" in marker, f"缺失清单必须点名 confirm_plan：{marker}"
    assert final.get("sendable_candidate_ids", []) == [], "DENY 后放行白名单必须清空"
    assert final.get("sent", []) == [], "DENY 批次不得发出任何候选（红线②）"
    assert "confirm_plan" not in final.get("visited_nodes", [])


def test_run_gate_denies_state_missing_confirm_plan() -> None:
    """直调 run_gate：state 轨迹缺 confirm_plan → DENY（真实接线在 gate 入口生效）。"""
    state: dict[str, Any] = dict(workflow.dry_run_initial_state())
    state["visited_nodes"] = list(_GATE_ARRIVAL_SKIPPING_CONFIRM)

    update = gate.run_gate(state)

    assert update["gate_outcome"] == "deny"
    marker = str(update["approval_marker"])
    assert "confirm_plan" in marker
    assert "E14" in marker
    assert update["sendable_candidate_ids"] == []


def test_run_gate_normal_arrival_is_not_path_denied() -> None:
    """正常到达 gate 的轨迹（segment→decision→confirm_plan）不被路径断言误伤（dry-run 直通）。"""
    state: dict[str, Any] = dict(workflow.dry_run_initial_state())
    state["visited_nodes"] = [NODE_SEGMENT, NODE_DECISION, NODE_CONFIRM_PLAN]
    state["sendable_candidate_ids"] = ["CAND-1"]

    update = gate.run_gate(state)

    assert update["gate_outcome"] == "pass"
    assert "path-assert-denied" not in str(update.get("approval_marker", ""))


def test_forged_visited_nodes_in_initial_state_cannot_bypass_assertion() -> None:
    """防伪造（红线④）：调用方在初始 state 注入 ``visited_nodes`` 不得掩盖未访问的必经节点。

    ``visited_nodes`` 无 reducer（LastValue 通道），注入值会被原样带到 gate 的断言；
    入口节点（segment）**重建**轨迹后，伪造值被丢弃 → 短路图仍被 DENY + E14。
    """
    state: dict[str, Any] = dict(workflow.dry_run_initial_state())
    state["visited_nodes"] = ["confirm_plan"]  # 伪造：声称走过 confirm_plan（实际从未访问）

    final = _short_circuit_graph().invoke(state)

    assert final.get("gate_outcome") == "deny", (
        "伪造 visited_nodes 不得让跳过 confirm_plan 的图通过"
    )
    marker = str(final.get("approval_marker", ""))
    assert "E14" in marker
    assert "PATH_ASSERT" in marker
    assert "confirm_plan" in marker, f"缺失清单必须点名 confirm_plan：{marker}"
    assert final.get("sent", []) == [], "伪造轨迹不得导致任何候选被发出"


def _short_circuit_to_send_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """测试内短路图：decision 直连 send（**同时跳过 confirm_plan 与 gate**）。"""
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(GraphState)
    graph.add_node(NODE_SEGMENT, workflow._segment_node)
    graph.add_node(NODE_DECISION, workflow._decision_node)
    graph.add_node(NODE_SEND, workflow._send_node)
    graph.add_edge(START, NODE_SEGMENT)
    graph.add_edge(NODE_SEGMENT, NODE_DECISION)
    graph.add_edge(NODE_DECISION, NODE_SEND)  # 短路：跳过 confirm_plan + gate（红线④ 负向）
    graph.add_edge(NODE_SEND, END)
    return graph.compile()


def test_send_path_bypassing_gate_is_also_blocked() -> None:
    """红线④（收口）：断言**不只在 gate 内**——不经 gate 的 send 路径同样被拦。

    短路图 decision → send 同时跳过 confirm_plan 与 gate；若断言只在 gate 里，
    这条路径会把候选直接发出去（``sent`` 非空）→ 本用例必红。
    """
    final = _short_circuit_to_send_graph().invoke(workflow.dry_run_initial_state())

    assert final.get("sent", []) == [], "不经 gate 的 send 路径不得发出任何候选（红线④）"
    assert final.get("dispatch_halted") is True, "派发必须被拦下"
    reason = str(final.get("dispatch_halt_reason", ""))
    assert "PATH_ASSERT" in reason, f"拦截原因必须可审计反查：{reason}"
    assert "confirm_plan" in reason, f"缺失清单必须点名 confirm_plan：{reason}"
    assert "gate" in reason, f"缺失清单必须点名 gate：{reason}"
    assert final.get("pending_candidate_ids", []) == [], "路径断言拦下的候选不进熔断队列"
