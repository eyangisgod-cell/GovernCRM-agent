"""T-E1 LangGraph 执行主链：segment → decision → confirm_plan → gate → send(mock)。

权威依据：

- **ADR-035 / 裁决 #7**（架构 v5.2:237）：``confirm_plan`` 图内必经人工节点，
  位于判定（decision）之后、闸门（gate）之前，**所有自主度等级必需**；
  T-E1 在 dry-run 下 auto-confirm 打标记，正式 interrupt 挂点留给 T-E2。
- **架构 v5.2:258**：``GraphState.visited_nodes`` 路径轨迹，供 T-E4 路径完整性断言消费。
- **红线①**：graph 只编排调用 ``src/decision/`` 现有函数（resolver / impact /
  rate_limit / suppression），三档路由唯一实现点 = ``src/decision/impact.py``，
  判定唯一实现点 = ``src/policy_kernel/rules.py``——graph 内不出现第二份判定。
- **红线②**：DENY 候选不到达 send——decision 只把 ``resolve_conflicts`` 的
  survivors（ALLOW）写进 ``sendable_candidate_ids``，全 DENY 时条件边直接结束。
- **红线⑤**：零供应商依赖；CLI ``--dry-run`` 无 DB 无网络（send = 内存 mock）。
- **熔断恢复的分组口径**（T-E6）：熔断器是**全局单例**，挂起队列里可能同时躺着
  多个租户 / 多个任务组的候选；恢复结论按 ``(租户, 轨迹)`` 分组保留，恢复派发
  **只消费当前 state 所属组**的放行候选——别的组候选留在熔断挂起队列（不是丢弃），
  避免「恢复任务 B」把「任务 A」的候选错误派发出去。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.decision.impact import calculate_impact, route_by_impact
from src.decision.rate_limit import count_channel_touches, count_global_touches
from src.decision.resolver import resolve_conflicts
from src.decision.suppression import build_scope_facts, build_suppression_hits, build_touches
from src.graph.circuit_breaker import (
    CircuitBreaker,
    RejudgeContext,
    ResumeOutcome,
    get_global_breaker,
)
from src.graph.gate import run_gate
from src.graph.nodes import (
    NODE_CONFIRM_PLAN,
    NODE_DECISION,
    NODE_GATE,
    NODE_SEGMENT,
    NODE_SEND,
    PIPELINE_NODES,
)
from src.graph.path_assert import assert_path
from src.policy_kernel.config import GovernanceConfig, load_and_validate
from src.policy_kernel.rules import (
    ActionCandidate as KernelActionCandidate,
    CustomerContext,
    EvaluationRequest,
)
from src.unification.segmentation import SCOPE_GLOBAL, segment_customer

__all__ = [
    "NODE_CONFIRM_PLAN",
    "NODE_DECISION",
    "NODE_GATE",
    "NODE_SEGMENT",
    "NODE_SEND",
    "PIPELINE_NODES",
    "GraphState",
    "PlanNotConfirmedError",
    "build_graph",
    "downgrade_and_dispatch",
    "dry_run_initial_state",
    "main",
    "resume_and_dispatch",
    "run_dry_run",
]

# 节点名常量（T-E1 prd / MVP 任务拆解 W5：segment → **confirm_plan** → gate → send）
# 定义于 ``src/graph/nodes.py``（T-E4：gate/path_assert 复用，避免 import 环），此处 re-export。


class PlanNotConfirmedError(RuntimeError):
    """计划未经 confirm_plan 确认——缺必经节点，禁止继续（ADR-035 / 红线④）。"""


class GraphState(TypedDict, total=False):
    """执行循环状态（架构 v5.2 §4.2 GraphState 契约的 T-E1 子集）。

    关键字段：
    - ``visited_nodes``：路径轨迹，供路径完整性断言（A9 / 红线④）校验（v5.2:258）；
    - ``plan_confirmed``：计划确认标志（必经人工节点，任务级一次性，裁决 #7）；
    - ``sendable_candidate_ids``：decision 放行（ALLOW）候选白名单——send 只发这份。
    """

    task_id: str
    worker_id: str
    tenant_id: str
    autonomy_level: str
    user_input: str
    dry_run: bool
    now: datetime
    customer_rows: list[dict[str, Any]]
    candidate_rows: list[dict[str, Any]]
    tool_call_rows: list[dict[str, Any]]
    suppression_rows: list[dict[str, Any]]
    segments: list[str]
    sendable_candidate_ids: list[str]
    denied_candidate_ids: list[str]
    denied_details: list[dict[str, str]]
    suppressed_rule_ids: list[str]
    escalation_required: bool
    impact_unique_customers: int
    impact_tier: str
    touch_counts: dict[str, int]
    plan_confirmed: bool
    confirmation_marker: str
    gate_outcome: str
    approval_status: str
    approval_marker: str
    rule_version: str
    sent: list[dict[str, str]]
    visited_nodes: list[str]
    dispatch_halted: bool
    dispatch_halt_reason: str
    pending_candidate_ids: list[str]
    deferred_candidate_ids: list[str]


def _visit(state: GraphState, node: str) -> list[str]:
    """轨迹追加：每个节点 wrapper 先记自己的名字（架构 v5.2:258）。"""
    return [*state.get("visited_nodes", []), node]


def _begin_trace(node: str) -> list[str]:
    """入口节点**重建**轨迹：丢弃调用方随初始 state 注入的 ``visited_nodes``。

    ``visited_nodes`` 无 reducer（LastValue 通道），调用方注入值会被原样保留并一路
    带到 gate 的路径断言——若不重建，伪造的轨迹可掩盖「从未访问」的必经节点，
    红线④被绕过。入口节点只认「自己刚刚走过」的事实，轨迹从此由图自身写入。
    """
    return [node]


def _governance_path() -> str:
    """governance.yaml 路径：先看 cwd，再回退仓库根（策略参数唯一来源）。"""
    local = Path("governance.yaml")
    if local.exists():
        return str(local)
    return str(Path(__file__).resolve().parents[2] / "governance.yaml")


def _governance_config() -> GovernanceConfig:
    """加载治理配置（阈值唯一来源 = governance.yaml；纯文件读取，无 DB 无网络）。"""
    return load_and_validate(_governance_path())


def _as_datetime(value: Any) -> datetime:
    """候选 ``created_at`` 归一为 aware datetime（数据装配，非判定）。"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    """uuid 化（非法值返回 ``None``）——审计与熔断挂起队列的 id 归一（数据装配）。"""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


def _segment_node(state: GraphState) -> GraphState:
    """分层：调用 ``src/unification/segmentation`` 产出各客户九宫格层名（内存样例）。

    度量映射复用 ``build_scope_facts``（数据装配唯一入口），不写第二份单位换算。
    """
    config = _governance_config()
    segments: list[str] = []
    for row in state.get("customer_rows", []):
        facts = build_scope_facts(row)
        segment = segment_customer(facts.metrics, config.segmentation, scope=SCOPE_GLOBAL)
        segments.append(f"{row.get('customer_id')}:{segment.layer_name}")
    return {"visited_nodes": _begin_trace(NODE_SEGMENT), "segments": segments}


def _decision_node(state: GraphState) -> GraphState:
    """编排调用 ``src/decision/``：suppression/rate_limit 接数 → resolver 判定 → impact 路由。

    职责边界（红线①）：本节点只做数据装配与函数调用——
    - ``build_scope_facts`` / ``build_suppression_hits`` / ``build_touches``（接入层）；
    - ``count_global_touches`` / ``count_channel_touches``（频控计数，只记数不判阈）；
    - ``resolve_conflicts``（规则判定唯一实现点的编排入口）；
    - ``calculate_impact`` / ``route_by_impact``（三档路由唯一实现点）。

    DENY 语义（红线②）：只有 ``ResolutionResult.survivors``（ALLOW）进入
    ``sendable_candidate_ids``；DENY 候选只进 ``denied_candidate_ids`` 留痕。
    """
    now: datetime = state["now"]
    config = _governance_config()
    tool_call_rows = state.get("tool_call_rows", [])
    touches = build_touches(tool_call_rows)

    # 频控计数（数据接入，判定仍在 rules.evaluate 内）：count_* 吃原始 tool_call 行。
    # 窗口取 governance.yaml（config.thresholds，与 rules.py 判定同源），不写第二份魔法数。
    thresholds = config.thresholds
    touch_counts: dict[str, int] = {
        f"global_{thresholds.freq_window_days}d": count_global_touches(
            tool_call_rows, now=now, window_days=thresholds.freq_window_days
        ),
    }
    for channel in sorted({str(row.get("channel", "")) for row in state.get("candidate_rows", [])}):
        touch_counts[f"channel_{thresholds.channel_cooldown_hours}h:{channel}"] = (
            count_channel_touches(
                tool_call_rows,
                channel=channel,
                now=now,
                window_hours=thresholds.channel_cooldown_hours,
            )
        )

    hits_by_customer: dict[str, list[dict[str, Any]]] = {}
    for row in state.get("suppression_rows", []):
        hits_by_customer.setdefault(str(row["customer_id"]), []).append(row)

    customer_rows = {str(row["customer_id"]): row for row in state.get("customer_rows", [])}
    customer_of = {
        str(row["candidate_id"]): str(row["customer_id"]) for row in state.get("candidate_rows", [])
    }
    candidates_by_customer: dict[str, list[dict[str, Any]]] = {}
    for row in state.get("candidate_rows", []):
        candidates_by_customer.setdefault(str(row["customer_id"]), []).append(row)

    sendable: list[str] = []
    denied: list[str] = []
    suppressed_rules: list[str] = []
    escalated = False

    for customer_id, rows in candidates_by_customer.items():
        recorded_hits = build_suppression_hits(hits_by_customer.get(customer_id, []))
        global_facts = replace(
            build_scope_facts(customer_rows.get(customer_id, {})),
            suppression_hits=recorded_hits,
        )
        candidates: list[KernelActionCandidate] = []
        for row in rows:
            platform_row = row.get("platform_row")
            platform_facts = None
            if isinstance(platform_row, dict):
                platform_facts = replace(
                    build_scope_facts(platform_row),
                    suppression_hits=recorded_hits,
                )
            candidates.append(
                KernelActionCandidate(
                    candidate_id=str(row["candidate_id"]),
                    platform=str(row["platform"]),
                    coupon_value_cents=int(row["coupon_value_cents"]),
                    coupon_threshold_cents=int(row["coupon_threshold_cents"]),
                    created_at=_as_datetime(row["created_at"]),
                    channel=str(row["channel"]),
                    platform_facts=platform_facts,
                )
            )
        resolution = resolve_conflicts(
            EvaluationRequest(
                customer=CustomerContext(global_facts=global_facts, touches=touches),
                candidates=tuple(candidates),
                now=now,
                config=config,
            )
        )
        sendable.extend(resolution.survivors)
        for record in resolution.suppressed:
            denied.append(record.candidate_id)
            suppressed_rules.append(record.rule_id)
        escalated = escalated or resolution.escalated

    impact = calculate_impact(
        [{"customer_id": customer_of[cid], "candidate_id": cid} for cid in sendable]
    )
    route = route_by_impact(unique_customer_count=impact.unique_customer_count)

    return {
        "visited_nodes": _visit(state, NODE_DECISION),
        "sendable_candidate_ids": sendable,
        "denied_candidate_ids": denied,
        "suppressed_rule_ids": suppressed_rules,
        "escalation_required": escalated,
        "impact_unique_customers": impact.unique_customer_count,
        "impact_tier": route.tier,
        "touch_counts": touch_counts,
    }


def _confirm_plan_node(state: GraphState) -> GraphState:
    """计划确认点：dry-run auto-confirm 打标记；正式模式必须已确认（T-E2 接管 interrupt）。"""
    update: GraphState = {"visited_nodes": _visit(state, NODE_CONFIRM_PLAN)}
    if state.get("dry_run"):
        update["plan_confirmed"] = True
        update["confirmation_marker"] = "auto-confirm (dry-run)"
        return update
    if not state.get("plan_confirmed"):
        raise PlanNotConfirmedError(
            "计划未确认：confirm_plan 是图内必经人工节点（ADR-035 / 裁决 #7），"
            "非 dry-run 下必须先经 confirm_plan 确认（T-E2 负责 interrupt 挂点）。"
        )
    update["confirmation_marker"] = "human-confirmed"
    return update


def _gate_node(state: GraphState) -> GraphState:
    """治理闸门（T-E2）：委托 ``src/graph/gate.py``——DENY 终止 / dry-run 直通 /
    REQUIRE_APPROVAL → ``interrupt()`` 挂起（恢复后按当前 config 重判 + 写审计）。
    """
    update = run_gate(state)
    return cast(GraphState, {**update, "visited_nodes": _visit(state, NODE_GATE)})


def _rejudge_context(state: GraphState) -> RejudgeContext:
    """重判所需的判定输入快照（数据装配，非判定）——send 与恢复派发共用一份。"""
    return RejudgeContext(
        customer_rows=tuple(dict(row) for row in state.get("customer_rows", [])),
        candidate_rows=tuple(dict(row) for row in state.get("candidate_rows", [])),
        tool_call_rows=tuple(dict(row) for row in state.get("tool_call_rows", [])),
        suppression_rows=tuple(dict(row) for row in state.get("suppression_rows", [])),
    )


def _mock_sent(state: GraphState, candidate_ids: Sequence[str]) -> list[dict[str, str]]:
    """mock 发送记录装配（send 节点与熔断恢复派发共用同一份）。"""
    channel_by_id = {
        str(row["candidate_id"]): str(row.get("channel", ""))
        for row in state.get("candidate_rows", [])
    }
    return [
        {"candidate_id": cid, "channel": channel_by_id.get(cid, ""), "status": "mock-sent"}
        for cid in candidate_ids
    ]


def _dispatch_trace(state: GraphState) -> str:
    """派发轨迹 id——熔断分组键 ``(租户, 轨迹)`` 的「轨迹」半边。

    ``send`` 节点与恢复派发路径必须用**同一份**口径，否则分组匹配不上
    （本组候选会被误判成「别的组的」→ 跨组误派发）。
    """
    return f"task-{state.get('task_id', 'task')}"


def _send_node(state: GraphState) -> GraphState:
    """mock 发送：路径断言（红线④）→ 熔断检查（T-E6）→ 只发 ``sendable_candidate_ids``。

    两道闸门都**不可绕过**：

    - **路径完整性断言**：必经节点缺失即拒（红线④）。断言不止在 gate 内——
      派发前再过一遍，任何能到 send 的路径（含不经 gate 的短路）都被拦下，
      候选零发出；
    - **全局熔断开关**（架构 v5.2:614）：熔断打开时**不派发**，候选进挂起队列，
      等待恢复时按当前策略版本重判。
    """
    # 红线④：路径断言必经、不可 opt-in——派发前再走一遍（gate 之外的第二道）。
    path_verdict = assert_path([*state.get("visited_nodes", []), NODE_SEND])
    if not path_verdict.passed:
        missing_text = ",".join(sorted(path_verdict.missing))
        return {
            "visited_nodes": _visit(state, NODE_SEND),
            "sent": [],
            "dispatch_halted": True,
            "dispatch_halt_reason": (
                f"path-assert-denied ({path_verdict.alert_code} {path_verdict.rule_id}): "
                f"missing={missing_text}"
            ),
            "pending_candidate_ids": [],
        }

    # 架构 v5.2:614：熔断触发点在**派发前**——打开时不派发，候选进挂起队列。
    plan = get_global_breaker().before_dispatch(
        candidate_ids=[str(cid) for cid in state.get("sendable_candidate_ids", [])],
        context=_rejudge_context(state),
        tenant_id=_uuid_or_none(state.get("tenant_id")),
        trace_id=_dispatch_trace(state),
        task_id=_uuid_or_none(state.get("task_id")),
        worker_id=_uuid_or_none(state.get("worker_id")),
    )
    if not plan.allowed:
        return {
            "visited_nodes": _visit(state, NODE_SEND),
            "sent": [],
            "dispatch_halted": True,
            "dispatch_halt_reason": plan.hold_reason,
            "pending_candidate_ids": [item.candidate_id for item in plan.pending],
        }

    sent = _mock_sent(state, [str(cid) for cid in state.get("sendable_candidate_ids", [])])
    return {"visited_nodes": _visit(state, NODE_SEND), "sent": sent}


def _dispatch_rejudged(
    state: GraphState,
    *,
    breaker: CircuitBreaker,
    outcome: ResumeOutcome,
) -> GraphState:
    """把重判的**放行集合**交回派发路径（熔断感知 + 路径断言）。

    **只派发本任务组**（跨组不串味）：熔断器是全局单例，恢复结论按
    ``(租户, 轨迹)`` 分组；本函数只消费**当前 state 所属组**的放行候选——
    别的任务 / 别的租户的候选**留在熔断挂起队列**（不是丢弃），等待它们各自的
    任务恢复时再派发。

    两道闸门同样**不可绕过**：

    - **路径完整性断言**（红线④）：派发前仍断言必经节点；
    - **全局熔断开关**：熔断仍打开 → **零派发**，候选留在挂起队列。
    """
    tenant = breaker.resolve_tenant(_uuid_or_none(state.get("tenant_id")))
    trace = _dispatch_trace(state)
    group = outcome.group_for(tenant_id=tenant, trace_id=trace)
    candidate_ids = list(group.sendable_candidate_ids) if group is not None else []
    group_denied = list(group.denied) if group is not None else []
    # 其它组的放行候选：本任务**不派发**它们，但必须让上层看得见（不静默消失）。
    deferred = [
        candidate_id
        for other in outcome.groups
        if other is not group
        for candidate_id in other.sendable_candidate_ids
    ]

    update: GraphState = {
        "denied_candidate_ids": [item.candidate_id for item in group_denied],
        "suppressed_rule_ids": [item.rule_id for item in group_denied],
        "denied_details": [
            {"candidate_id": item.candidate_id, "rule_id": item.rule_id, "reason": item.reason}
            for item in group_denied
        ],
        "rule_version": outcome.rule_version,
        "deferred_candidate_ids": deferred,
    }

    # 红线④：派发同样不得绕过必经节点。
    path_verdict = assert_path([*state.get("visited_nodes", []), NODE_SEND])
    if not path_verdict.passed:
        missing_text = ",".join(sorted(path_verdict.missing))
        return {
            **update,
            "sent": [],
            "sendable_candidate_ids": [],
            "dispatch_halted": True,
            "dispatch_halt_reason": (
                f"path-assert-denied ({path_verdict.alert_code} {path_verdict.rule_id}): "
                f"missing={missing_text}"
            ),
            "pending_candidate_ids": list(breaker.pending_candidate_ids),
        }

    # 熔断感知派发：此刻熔断仍打开 → 零派发（候选留在挂起队列，等待下一次恢复）。
    plan = breaker.before_dispatch(
        candidate_ids=candidate_ids,
        context=_rejudge_context(state),
        tenant_id=_uuid_or_none(state.get("tenant_id")),
        trace_id=trace,
        task_id=_uuid_or_none(state.get("task_id")),
        worker_id=_uuid_or_none(state.get("worker_id")),
    )
    if not plan.allowed:
        return {
            **update,
            "sent": [],
            "sendable_candidate_ids": [],
            "dispatch_halted": True,
            "dispatch_halt_reason": plan.hold_reason,
            "pending_candidate_ids": list(breaker.pending_candidate_ids),
        }

    return {
        **update,
        "sent": _mock_sent(state, candidate_ids),
        "sendable_candidate_ids": candidate_ids,
        "dispatch_halted": False,
        "dispatch_halt_reason": "",
        "pending_candidate_ids": list(breaker.pending_candidate_ids),
    }


def resume_and_dispatch(
    state: GraphState,
    *,
    breaker: CircuitBreaker | None = None,
    reason: str = "operator-resume",
    actor: str = "operator",
) -> GraphState:
    """熔断恢复 → 重判 → **把放行集合交回派发路径**（架构 v5.2:614 / ADR-030）。

    恢复的完整闭环：**重判结论 → 派发**。重判放行的候选由本函数送进**同一套
    熔断感知的派发逻辑**（``before_dispatch`` + mock 发送），不再出现
    「重判出可发集合却无人派发 = 候选静默消失」。

    **只派发本任务组**：熔断器是全局单例，恢复结论按 ``(租户, 轨迹)`` 分组；
    本函数只派发**当前 state 所属组**的放行候选，别组的候选留在熔断挂起队列
    （见返回值的 ``pending_candidate_ids`` / ``deferred_candidate_ids``）。

    返回值是新的 ``GraphState``（含 ``sent`` / 放行与拒绝留痕 / ``rule_version``）。
    """
    active = breaker if breaker is not None else get_global_breaker()
    outcome = active.reset(reason=reason, actor=actor)
    return _dispatch_rejudged(state, breaker=active, outcome=outcome)


def downgrade_and_dispatch(
    state: GraphState,
    *,
    breaker: CircuitBreaker | None = None,
    reason: str = "autonomy-downgraded",
    actor: str = "system",
) -> GraphState:
    """自主度降级 → 排队动作重判 → 把放行集合交回派发路径（裁决 #6）。

    与恢复同一入口（``gate.rejudge_with_current_config``）、同一派发路径。
    **熔断仍打开时零派发**：候选留在挂起队列（待熔断恢复时按当时策略再判），
    绝不丢队列。
    """
    active = breaker if breaker is not None else get_global_breaker()
    outcome = active.apply_autonomy_downgrade(reason=reason, actor=actor)
    return _dispatch_rejudged(state, breaker=active, outcome=outcome)


def _route_after_decision(state: GraphState) -> str:
    """条件边：有放行候选 → confirm_plan；全 DENY → 结束（DENY 候选不到达 send，红线②）。"""
    return "plan" if state.get("sendable_candidate_ids") else "end"


# ---------------------------------------------------------------------------
# 样例数据与图构建
# ---------------------------------------------------------------------------


def dry_run_initial_state() -> GraphState:
    """dry-run 初始态：内存样例（可放行批次），无 DB 无网络。"""
    return {
        "task_id": "TASK-DRYRUN",
        "worker_id": "1",
        "autonomy_level": "L1",
        "user_input": "唤醒沉睡客户",
        "dry_run": True,
        "now": datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        "customer_rows": [
            {
                "customer_id": "CUST-1",
                "avg_order_value_90d": 500.0,
                "last_order_days_ago": 10,
                "complaint_count_90d": 0,
                "consent_status": "granted",
                "refund_rate": 0.01,
                "total_spend_percentile": 50.0,
                "explicit_refusal": False,
            }
        ],
        "candidate_rows": [
            {
                "candidate_id": "CAND-1",
                "customer_id": "CUST-1",
                "platform": "taobao",
                "channel": "sms",
                "coupon_value_cents": 5000,
                "coupon_threshold_cents": 10000,
                "created_at": datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
            }
        ],
        "tool_call_rows": [],
        "suppression_rows": [],
        "visited_nodes": [],
        "plan_confirmed": False,
    }


def build_graph(*, checkpointer: Any | None = None) -> CompiledStateGraph[Any, Any, Any, Any]:
    """构建并编译 StateGraph（测试与 CLI 共用）。

    ``checkpointer``：interrupt/恢复需要（T-E2，内存 checkpointer 即可）；
    缺省 None 保持 dry-run CLI 无 checkpointer 行为不变。
    """
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(GraphState)
    graph.add_node(NODE_SEGMENT, _segment_node)
    graph.add_node(NODE_DECISION, _decision_node)
    graph.add_node(NODE_CONFIRM_PLAN, _confirm_plan_node)
    graph.add_node(NODE_GATE, _gate_node)
    graph.add_node(NODE_SEND, _send_node)
    graph.add_edge(START, NODE_SEGMENT)
    graph.add_edge(NODE_SEGMENT, NODE_DECISION)
    graph.add_conditional_edges(
        NODE_DECISION,
        _route_after_decision,
        {"plan": NODE_CONFIRM_PLAN, "end": END},
    )
    graph.add_edge(NODE_CONFIRM_PLAN, NODE_GATE)
    graph.add_edge(NODE_GATE, NODE_SEND)
    graph.add_edge(NODE_SEND, END)
    return graph.compile(checkpointer=checkpointer)


def run_dry_run(initial: GraphState | None = None) -> GraphState:
    """以 dry-run 初始态走通全图，返回终态（含 visited_nodes 轨迹）。"""
    result = build_graph().invoke(initial if initial is not None else dry_run_initial_state())
    return cast(GraphState, result)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``python -m src.graph.workflow --dry-run``（无 DB 无网络）。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.graph.workflow",
        description="T-E1 执行图 CLI（骨架）：目前仅支持 --dry-run。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="以内存样例走通全图并打印节点访问序列（无 DB 无网络）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.dry_run:
        print("仅支持 --dry-run：正式执行链（interrupt / 审批）由 T-E2 接管。", file=sys.stderr)
        return 2

    state = run_dry_run()
    visited = state.get("visited_nodes", [])
    print(f"节点访问序列: {' -> '.join(visited)}")
    print(f"visited_nodes = {visited}")
    print(f"segments = {state.get('segments', [])}")
    print(
        f"计划确认: {state.get('confirmation_marker')}"
        f"（plan_confirmed={state.get('plan_confirmed')}）"
    )
    print(
        f"影响面: unique_customers={state.get('impact_unique_customers')}"
        f" tier={state.get('impact_tier')}"
    )
    print(f"touch_counts = {state.get('touch_counts', {})}")
    print(
        f"放行候选 = {state.get('sendable_candidate_ids', [])}"
        f"；DENY 留痕 = {state.get('denied_candidate_ids', [])}"
    )
    print(f"mock 发送 {len(state.get('sent', []))} 条（内存 mock，不触网不连库）")
    if visited == list(PIPELINE_NODES):
        print("dry-run 完成：五节点按序全数访问")
        return 0
    print(f"dry-run 异常：节点序列不符预期 {visited}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
