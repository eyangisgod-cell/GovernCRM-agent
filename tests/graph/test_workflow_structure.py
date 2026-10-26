"""T-E1 LangGraph 执行图结构与负向路径验收用例。

权威依据：

- **ADR-035 / 裁决 #7**（`docs/specs/GovernCRM_技术架构方案_v5.2.md:237`）：
  ``confirm_plan`` 是图内必经人工节点，位于判定（decision）之后、闸门（gate）之前，
  **所有自主度等级必需**——L2 仅免动作级审批，不免计划确认；纳入 ``REQUIRED_NODES``。
- **架构 v5.2:258**：``GraphState.visited_nodes`` 是路径轨迹，
  供路径完整性断言（A9 / 红线④，断言器由 T-E4 实现）消费；T-E1 负责让图天然带轨迹。
- **红线②**：DENY 对所有角色不可绕过——decision 结论 DENY 的候选不得到达 send。
- **红线①**：判定唯一实现点在 ``src/policy_kernel`` + ``src/decision``；
  graph 只编排调用，不得出现第二份三档路由 / 阈值判定。
- **红线⑤**：业务层零供应商依赖（graph 内禁 litellm 等供应商 SDK）。

纯函数用例标 ``@pytest.mark.unit``（pytest 默认 ``-m "not pg"`` 会放行）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.graph.workflow import build_graph, dry_run_initial_state

pytestmark = pytest.mark.unit

#: 图内五个管线节点（权威：MVP 任务拆解 T-E1「节点 segment → **confirm_plan** → gate → send」）。
#: 测试**硬编码**期望值，不从实现里 import——否则删掉 confirm_plan 会与实现同流合污。
PIPELINE_NODES: tuple[str, ...] = ("segment", "decision", "confirm_plan", "gate", "send")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GRAPH_SOURCES = sorted((_REPO_ROOT / "src" / "graph").glob("*.py"))

# ---------------------------------------------------------------------------
# 样例数据（内存，零 DB 零网络）
# ---------------------------------------------------------------------------


def _customer_row(customer_id: str = "CUST-1", **overrides: object) -> dict[str, object]:
    """一条可放行的客户行（无投诉 / 无纠纷 / 有授权 / 非 Top1%）。"""
    row: dict[str, object] = {
        "customer_id": customer_id,
        "avg_order_value_90d": 500.0,
        "last_order_days_ago": 10,
        "complaint_count_90d": 0,
        "consent_status": "granted",
        "refund_rate": 0.01,
        "total_spend_percentile": 50.0,
        "explicit_refusal": False,
    }
    row.update(overrides)
    return row


def _candidate_row(
    candidate_id: str,
    customer_id: str = "CUST-1",
    *,
    platform: str = "taobao",
    channel: str = "sms",
    platform_row: dict[str, object] | None = None,
) -> dict[str, object]:
    """一条候选动作行（``created_at`` 落在冲突窗口 72h 内）。"""
    row: dict[str, object] = {
        "candidate_id": candidate_id,
        "customer_id": customer_id,
        "platform": platform,
        "channel": channel,
        "coupon_value_cents": 5000,
        "coupon_threshold_cents": 10000,
        "created_at": "2026-10-25T09:00:00+00:00",
    }
    if platform_row is not None:
        row["platform_row"] = platform_row
    return row


def _deny_customer_row() -> dict[str, object]:
    """命中规则 1b（无有效营销授权）→ 该客户全部候选 DENY（附录D 规则 1b）。"""
    return _customer_row(consent_status="denied")


def _state(**overrides: object) -> dict[str, object]:
    """基于 dry-run 初始态打补丁（返回 plain dict，invoke 时整体作为输入）。"""
    state: dict[str, object] = dict(dry_run_initial_state())
    state.update(overrides)
    return state


def _edges() -> set[tuple[str, str]]:
    graph = build_graph().get_graph()
    return {(edge.source, edge.target) for edge in graph.edges}


def _all_paths(edges: set[tuple[str, str]], start: str, end: str) -> list[list[str]]:
    """枚举 start→end 的所有简单路径（环防护：节点不重复入路径）。"""
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    paths: list[list[str]] = []

    def walk(node: str, path: list[str]) -> None:
        if node == end:
            paths.append(path)
            return
        for nxt in adjacency.get(node, []):
            if nxt not in path:
                walk(nxt, [*path, nxt])

    walk(start, [start])
    return paths


# ---------------------------------------------------------------------------
# 图结构：节点集合与连线
# ---------------------------------------------------------------------------


def test_graph_declares_exactly_the_five_pipeline_nodes() -> None:
    """图节点集合 == {segment, decision, confirm_plan, gate, send}（T-E1 prd 验收）。"""
    node_names = {name for name in build_graph().get_graph().nodes if not name.startswith("__")}
    assert node_names == set(PIPELINE_NODES)


def test_linear_wiring_follows_segment_decision_confirm_gate_send() -> None:
    """主链连线：START→segment→decision，decision→confirm_plan，confirm_plan→gate→send→END。

    依据：MVP 任务拆解 T-E1 节点链；ADR-035（confirm_plan 在 decision 之后、gate 之前）。
    """
    edges = _edges()
    assert ("__start__", "segment") in edges
    assert ("segment", "decision") in edges
    assert ("decision", "confirm_plan") in edges
    assert ("confirm_plan", "gate") in edges
    assert ("gate", "send") in edges
    assert ("send", "__end__") in edges


def test_no_path_from_decision_can_bypass_confirm_plan() -> None:
    """负向结构断言（红线④ + ADR-035）：decision→gate 任一路径必经 confirm_plan。

    任何绕过连线（decision→gate / decision→send / confirm_plan→send）都让
    「必经节点缺失即 DENY」的演示不成立——该断言先于实现存在（先红后绿）。
    """
    edges = _edges()
    assert ("decision", "gate") not in edges, "decision 直连 gate 绕过了 confirm_plan"
    assert ("decision", "send") not in edges, "decision 直连 send 绕过了 confirm_plan"
    assert ("confirm_plan", "send") not in edges, "confirm_plan 直连 send 绕过了 gate"

    paths = _all_paths(edges, "decision", "gate")
    assert paths, "图中不存在 decision→gate 的路径"
    for path in paths:
        assert "confirm_plan" in path, f"路径 {path} 缺失必经节点 confirm_plan（ADR-035 / 红线④）"


def test_every_path_to_send_goes_through_confirm_plan_and_gate() -> None:
    """静态扫描（架构 v5.3 §F.4 ⑦ / 红线④）：**任何**到达 send 的路径必经 confirm_plan + gate。

    前一条用例只覆盖 decision→gate 子图——若出现 segment→send 这类旁路边，
    三条 edge 断言都不触发，而路径断言（只在 gate 内执行）根本不会运行。
    本用例从 START 枚举全部 send 路径，堵死该缺口。
    """
    edges = _edges()
    paths = _all_paths(edges, "__start__", "send")
    assert paths, "图中不存在 START→send 的路径"
    for path in paths:
        assert "confirm_plan" in path, f"路径 {path} 绕过必经节点 confirm_plan（红线④）"
        assert "gate" in path, f"路径 {path} 绕过治理闸门 gate（红线④ / 红线②）"


@pytest.mark.parametrize("autonomy_level", ["L0", "L1", "L2"])
def test_confirm_plan_is_mandatory_for_every_autonomy_level(autonomy_level: str) -> None:
    """参数化 L0/L1/L2：confirm_plan 必经，且严格位于 decision 之后、gate 之前。

    权威：ADR-035 / 裁决 #7（架构 v5.2:237）——任务级一次性、所有自主度等级必需；
    L1/L2 只免动作级审批，不免计划确认，与自主度解耦。
    """
    state = _state(autonomy_level=autonomy_level)
    state["customer_rows"] = [_customer_row()]
    state["candidate_rows"] = [_candidate_row("CAND-1")]
    out = build_graph().invoke(state)

    visited: list[str] = out["visited_nodes"]
    assert "confirm_plan" in visited, f"{autonomy_level} 下 confirm_plan 不在路径中（ADR-035）"
    assert visited.index("decision") < visited.index("confirm_plan") < visited.index("gate")


def test_visited_nodes_records_full_pipeline_in_order() -> None:
    """GraphState.visited_nodes 完整按序记录五节点（架构 v5.2:258 路径轨迹）。"""
    out = build_graph().invoke(dry_run_initial_state())
    assert out["visited_nodes"] == list(PIPELINE_NODES)


# ---------------------------------------------------------------------------
# 负向路径：DENY 不到 send（红线②，先红后绿）
# ---------------------------------------------------------------------------


def test_deny_conclusion_candidates_never_reach_send() -> None:
    """负向路径（红线②）：decision 结论 DENY 的候选不得到达 send。

    样例命中附录D 规则 1b（无营销授权 → NO_CONSENT → DENY）：
    全员 DENY 时 send 节点不可达，sendable 为空，候选只进 denied 留痕。
    """
    state = _state()
    state["customer_rows"] = [_deny_customer_row()]
    state["candidate_rows"] = [_candidate_row("CAND-DENY")]

    out = build_graph().invoke(state)

    assert "send" not in out["visited_nodes"], "DENY 批次不得到达 send 节点（红线②）"
    assert out.get("sent", []) == [], "send 不得发出任何 DENY 候选"
    assert out.get("sendable_candidate_ids", []) == []
    assert out.get("denied_candidate_ids", []) == ["CAND-DENY"]


def test_mixed_batch_sends_only_allow_survivors() -> None:
    """混合批次：同客户两候选 → 冲突消解只放行 1 条，被抑制者不得出现在 sent 里。

    权威：附录D 规则 3（三键排序只放行 1 条，另一条 DENY 留痕）、红线②。
    """
    state = _state()
    state["customer_rows"] = [_customer_row()]
    state["candidate_rows"] = [
        _candidate_row(
            "CAND-WIN",
            platform="taobao",
            platform_row=_customer_row(avg_order_value_90d=500.0, last_order_days_ago=10),
        ),
        _candidate_row(
            "CAND-LOSE",
            platform="douyin",
            platform_row=_customer_row(avg_order_value_90d=50.0, last_order_days_ago=200),
        ),
    ]

    out = build_graph().invoke(state)

    sent_ids = {item["candidate_id"] for item in out.get("sent", [])}
    assert "send" in out["visited_nodes"]
    assert sent_ids == {"CAND-WIN"}, f"只应发送放行候选，实际发送 {sorted(sent_ids)}"
    assert "CAND-LOSE" not in sent_ids, "被抑制（DENY）候选不得到达 send（红线②）"
    assert "CAND-LOSE" in out.get("denied_candidate_ids", [])


def test_unconfirmed_plan_blocks_before_gate() -> None:
    """负向运行断言：非 dry-run 且计划未确认 → confirm_plan 挂点抛错，路径到不了 gate。

    权威：ADR-035（confirm_plan 图内必经，跳过即缺节点）；
    正式 interrupt 挂点由 T-E2 接管，T-E1 至少保证「未确认不放行」。
    """
    state = _state(dry_run=False, plan_confirmed=False)
    state["customer_rows"] = [_customer_row()]
    state["candidate_rows"] = [_candidate_row("CAND-1")]

    with pytest.raises(RuntimeError, match="计划未确认"):
        build_graph().invoke(state)


# ---------------------------------------------------------------------------
# 红线：graph 只编排，不重写判定
# ---------------------------------------------------------------------------


def test_graph_only_orchestrates_existing_decision_functions() -> None:
    """红线① + 三档路由唯一实现点：graph 只编排调用 src/decision/ 现有函数。

    - 必须 import：resolver（resolve_conflicts）、impact（calculate_impact/route_by_impact）、
      rate_limit（count_*_touches）、suppression（build_*）；
    - 禁止在 graph 内出现三档阈值比较（唯一实现点 = ``src/decision/impact.py``）、
      结论分支比较（唯一实现点 = ``src/policy_kernel/rules.py``）、供应商 SDK（红线⑤）。
    """
    source = "\n".join(path.read_text(encoding="utf-8") for path in _GRAPH_SOURCES)

    for module in (
        "src.decision.resolver",
        "src.decision.impact",
        "src.decision.rate_limit",
        "src.decision.suppression",
    ):
        assert module in source, f"graph 未编排调用 {module}"

    assert not re.search(r"unique_customer_count\s*<=", source), "graph 内出现三档路由重写"
    assert not re.search(r"\bconclusion\s*==", source), "graph 内出现结论判定分支"
    for tier in ("tier1", "tier2", "tier3"):
        assert tier not in source, (
            f"graph 内出现硬编码档位 {tier}（三档路由唯一实现点在 impact.py）"
        )
    assert "litellm" not in source, "红线⑤：业务层零供应商依赖"
    assert "httpx" not in source, "dry-run 图内不得触网"
    assert "psycopg" not in source, "dry-run 图内不得连库"
