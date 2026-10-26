"""T-E2 治理闸门：DENY 终止 / REQUIRE_APPROVAL interrupt / 恢复重判（ADR-030）。

权威依据：

- **红线②**：DENY 对所有角色（含管理员、含 AI）不可绕过——sendable 白名单与
  DENY 留痕有交集即拒绝终止，错误携带 rule_id 与理由（非裸 Exception）。
- **架构 v5.2:612 / 裁决 #8**：REQUIRE_APPROVAL → LangGraph ``interrupt()`` 挂起 →
  人工审批 → ``Command(resume=...)`` 恢复；任务级批量审批 = 一次 resume 覆盖该批全部候选。
- **ADR-030**（架构 v5.2:238）：恢复 = 挂起动作以**当前**策略版本（rule_version）
  重新判定，**不沿用挂起时快照**；重判结论写审计（红线③：唯一走
  ``insert_statement_sql``）。
- **E9 / 裁决 #25**：审批 48h 超时 = 过期，不默认通过——resume 晚于超时点一律不放行。
- **E14 / 红线④ / 架构 v5.2:206（⓪）**：gate 入口最先执行路径完整性断言
  ``assert_path``——trace 缺失必经节点（如 confirm_plan）一律 DENY + E14，
  进人工审查；断言必经，不可 opt-in。
- **红线①**：判定唯一实现点 = ``src/policy_kernel/rules.py`` + ``src/decision/``；
  本模块只做编排调用与数据装配，不出现第二份阈值 / 结论分支。
- **红线⑤**：零供应商依赖；审计 IO 落在 ``src/approval/audit.py``，本模块不连库。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from src.approval.audit import (
    DeniedCandidate,
    RejudgeAudit,
    write_rejudge_audit,
)
from src.approval.router import check_timeout, required_signers
from src.decision.resolver import resolve_conflicts
from src.decision.suppression import build_scope_facts, build_suppression_hits, build_touches
from src.graph.nodes import NODE_GATE, NODE_SEND
from src.graph.path_assert import PathVerdict, assert_path
from src.policy_kernel.config import GovernanceConfig, load_and_validate
from src.policy_kernel.rules import (
    ActionCandidate as KernelActionCandidate,
    CustomerContext,
    EvaluationRequest,
)

__all__ = [
    "ApprovalRejectedError",
    "DeniedCandidate",
    "DenyVetoError",
    "GateVerdict",
    "RejudgeOutcome",
    "ResumeDecision",
    "evaluate_gate",
    "interpret_resume",
    "load_current_config",
    "rejudge_with_current_config",
    "run_gate",
]


class DenyVetoError(RuntimeError):
    """红线②：DENY 候选被闸门拒绝终止——任何角色不可绕过。

    携带 ``candidate_id`` / ``rule_id`` / ``reason``，非裸 Exception。
    """

    def __init__(self, candidate_id: str, rule_id: str, reason: str) -> None:
        self.candidate_id = candidate_id
        self.rule_id = rule_id
        self.reason = reason
        super().__init__(
            f"DENY 不可绕过（红线②）：candidate={candidate_id} rule_id={rule_id} 理由={reason}"
        )


class ApprovalRejectedError(RuntimeError):
    """resume 明确拒绝审批——任务级批次不放行（裁决 #8）。"""


@dataclass(frozen=True)
class GateVerdict:
    """闸门纯判定结果。``outcome`` ∈ {``pass``, ``deny``, ``require_approval``}。"""

    outcome: str
    denied: tuple[DeniedCandidate, ...] = ()


@dataclass(frozen=True)
class RejudgeOutcome:
    """恢复重判结论（ADR-030）：放行集合 + DENY 留痕。"""

    sendable: tuple[str, ...]
    denied: tuple[DeniedCandidate, ...]


@dataclass(frozen=True)
class ResumeDecision:
    """resume 值解读结果（E9：``expired`` 时一律不通过）。"""

    approved: bool
    expired: bool
    resumed_at: datetime


# ---------------------------------------------------------------------------
# 纯函数：闸门判定 / resume 解读 / 恢复重判
# ---------------------------------------------------------------------------


def evaluate_gate(
    *,
    sendable_candidate_ids: Sequence[str],
    denied: Sequence[DeniedCandidate],
    approval_required: bool,
) -> GateVerdict:
    """闸门纯判定：DENY 交集 → 拒绝终止（红线②）；有待批 → require_approval；否则 pass。"""
    denied_by_id = {item.candidate_id: item for item in denied}
    for candidate_id in sendable_candidate_ids:
        hit = denied_by_id.get(candidate_id)
        if hit is not None:
            raise DenyVetoError(hit.candidate_id, hit.rule_id, hit.reason)
    if not sendable_candidate_ids:
        return GateVerdict(outcome="deny", denied=tuple(denied))
    if approval_required:
        return GateVerdict(outcome="require_approval", denied=tuple(denied))
    return GateVerdict(outcome="pass", denied=tuple(denied))


def interpret_resume(
    resume_value: Any,
    *,
    submitted_at: datetime,
    now: datetime,
    timeout_hours: int,
) -> ResumeDecision:
    """解读 ``Command(resume=...)`` 的值（裁决 #8：任务级批量审批，一次覆盖全批）。

    resume 值约定：dict ``{"approved": bool, "resumed_at": datetime?, ...}`` 或任意真值。
    E9：``resumed_at`` 晚于 ``submitted_at + timeout_hours`` 一律过期——即使值为批准。
    """
    if isinstance(resume_value, Mapping):
        approved = bool(resume_value.get("approved", False))
        resumed_at_value = resume_value.get("resumed_at")
        resumed_at = resumed_at_value if isinstance(resumed_at_value, datetime) else now
    else:
        approved = bool(resume_value)
        resumed_at = now
    timeout = check_timeout(submitted_at, resumed_at, timeout_hours=timeout_hours)
    if timeout.expired:
        return ResumeDecision(approved=False, expired=True, resumed_at=resumed_at)
    return ResumeDecision(approved=approved, expired=False, resumed_at=resumed_at)


def rejudge_with_current_config(
    *,
    pending_candidate_ids: Sequence[str],
    customer_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    tool_call_rows: Sequence[Mapping[str, Any]],
    suppression_rows: Sequence[Mapping[str, Any]],
    now: datetime,
    config: GovernanceConfig,
) -> RejudgeOutcome:
    """ADR-030：以**当前** config 重跑 ``src/decision`` 判定（不沿用挂起时快照）。

    只重判挂起批次（``pending_candidate_ids``）；判定仍在
    ``src/policy_kernel/rules.evaluate``（唯一实现点），此处仅数据装配与编排。
    """
    pending = {str(candidate_id) for candidate_id in pending_candidate_ids}
    touches = build_touches([dict(row) for row in tool_call_rows])

    hits_by_customer: dict[str, list[dict[str, Any]]] = {}
    for row in suppression_rows:
        hits_by_customer.setdefault(str(row["customer_id"]), []).append(dict(row))

    customer_by_id = {str(row["customer_id"]): dict(row) for row in customer_rows}
    pending_by_customer: dict[str, list[dict[str, Any]]] = {}
    for row in candidate_rows:
        if str(row["candidate_id"]) in pending:
            pending_by_customer.setdefault(str(row["customer_id"]), []).append(dict(row))

    sendable: list[str] = []
    denied: list[DeniedCandidate] = []
    for customer_id, rows in pending_by_customer.items():
        recorded_hits = build_suppression_hits(hits_by_customer.get(customer_id, []))
        global_facts = replace(
            build_scope_facts(customer_by_id.get(customer_id, {})),
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
            denied.append(
                DeniedCandidate(
                    candidate_id=record.candidate_id,
                    rule_id=record.rule_id,
                    reason=record.reason_detail,
                )
            )
    return RejudgeOutcome(sendable=tuple(sendable), denied=tuple(denied))


# ---------------------------------------------------------------------------
# 图节点入口（workflow._gate_node 委托到这里）
# ---------------------------------------------------------------------------


def _load_config() -> GovernanceConfig:
    """加载当前 governance config（阈值与 rule_version 的唯一来源；测试可 monkeypatch）。"""
    local = Path("governance.yaml")
    path = local if local.exists() else Path(__file__).resolve().parents[2] / "governance.yaml"
    return load_and_validate(path)


def load_current_config() -> GovernanceConfig:
    """加载**当前** governance config（对外公开入口，T-E6 熔断恢复复用）。

    ADR-030 的落点：恢复 / 重判时取的是**当前**版本，而非挂起时的快照。
    内部仍走 ``_load_config``，测试对该函数的 monkeypatch 依然生效。
    """
    return _load_config()


def _as_datetime(value: Any) -> datetime:
    """候选 ``created_at`` 归一为 aware datetime（数据装配，非判定）。"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _state_now(state: Mapping[str, Any]) -> datetime:
    """state 的判定时刻（缺省当前 UTC 时刻；mypy 友好的窄化）。"""
    value = state.get("now")
    return value if isinstance(value, datetime) else datetime.now(UTC)


def _denied_from_state(state: Mapping[str, Any]) -> tuple[DeniedCandidate, ...]:
    """从 state 还原 DENY 留痕：优先 ``denied_details``（带理由），回退平行列表。"""
    details = state.get("denied_details")
    if details:
        return tuple(
            DeniedCandidate(
                candidate_id=str(item["candidate_id"]),
                rule_id=str(item["rule_id"]),
                reason=str(item.get("reason", "")),
            )
            for item in details
        )
    candidate_ids = [str(cid) for cid in state.get("denied_candidate_ids", [])]
    rule_ids = [str(rid) for rid in state.get("suppressed_rule_ids", [])]
    return tuple(
        DeniedCandidate(
            candidate_id=candidate_id,
            rule_id=rule_ids[index] if index < len(rule_ids) else "UNKNOWN",
            reason="命中抑制/冲突规则（decision 阶段 DENY 留痕）",
        )
        for index, candidate_id in enumerate(candidate_ids)
    )


def run_gate(
    state: Mapping[str, Any],
    *,
    config_loader: Callable[[], GovernanceConfig] | None = None,
    audit_writer: Callable[[RejudgeAudit], Any] | None = None,
) -> dict[str, Any]:
    """闸门节点主体：路径断言⓪ → DENY 终止 / dry-run 直通 / REQUIRE_APPROVAL → interrupt → 重判。

    ``config_loader`` / ``audit_writer`` 为测试缝；默认分别读 governance.yaml 与
    ``write_rejudge_audit``（红线③：唯一走 ``insert_statement_sql``）。
    **config 在 interrupt 返回之后才加载**——这正是 ADR-030「恢复时取当前版本」的落点。
    """
    # ⓪ 路径完整性断言（PRD E14 / 架构 v5.2:206 / 红线④）——gate 入口最先执行，不可 opt-in。
    # 输入 = state 轨迹 + 本节点与无条件后继（gate 由本节点即将记录、send 由 gate→send
    # 无条件边保证必然执行）＝「到 send 为止的完整路径」。上游必经节点（如 confirm_plan）
    # 被跳过 → missing 非空 → 一律 DENY + E14，sendable 清空并升级人工审查。
    path_verdict = assert_path([*state.get("visited_nodes", []), NODE_GATE, NODE_SEND])
    if not path_verdict.passed:
        return _path_denied_update(path_verdict)

    loader = config_loader if config_loader is not None else _load_config
    config = loader()
    sendable = [str(cid) for cid in state.get("sendable_candidate_ids", [])]
    denied = _denied_from_state(state)
    dry_run = bool(state.get("dry_run", False))

    verdict = evaluate_gate(
        sendable_candidate_ids=sendable,
        denied=denied,
        approval_required=not dry_run,
    )
    if verdict.outcome == "deny":
        return {
            "gate_outcome": "deny",
            "approval_marker": "deny-terminated",
            "rule_version": config.version,
            "sendable_candidate_ids": [],
        }
    if verdict.outcome == "pass":
        marker = "auto-pass (dry-run)" if dry_run else "no-approval-required"
        return {"gate_outcome": "pass", "approval_marker": marker, "rule_version": config.version}

    # require_approval → interrupt（任务级批量审批，裁决 #8：一次 resume 覆盖全批）
    impact_count = int(state.get("impact_unique_customers", 0) or 0)
    routing = required_signers(unique_customer_count=impact_count)
    state_now = _state_now(state)
    payload = {
        "kind": "task_batch_approval",
        "task_id": state.get("task_id", ""),
        "worker_id": state.get("worker_id", ""),
        "candidate_ids": sendable,
        "impact_unique_customers": impact_count,
        "impact_tier": routing.tier,
        "required_signers": [role.value for role in routing.signers],
        "notified": [role.value for role in routing.notified],
        "rule_version_at_suspend": config.version,
        "submitted_at": state_now,
    }
    resume_value = interrupt(payload)
    return _apply_resume(
        state,
        resume_value,
        submitted_at=state_now,
        config_loader=loader,
        audit_writer=audit_writer,
        signers=tuple(role.value for role in routing.signers),
    )


def _path_denied_update(verdict: PathVerdict) -> dict[str, Any]:
    """E14：路径完整性断言失败 → DENY 终止（sendable 清空 + 告警标识 + 人工审查）。"""
    missing_text = ",".join(sorted(verdict.missing))
    return {
        "gate_outcome": "deny",
        "approval_marker": (
            f"path-assert-denied ({verdict.alert_code} {verdict.rule_id}): missing={missing_text}"
        ),
        "sendable_candidate_ids": [],
        "escalation_required": True,
    }


def _apply_resume(
    state: Mapping[str, Any],
    resume_value: Any,
    *,
    submitted_at: datetime,
    config_loader: Callable[[], GovernanceConfig],
    audit_writer: Callable[[RejudgeAudit], Any] | None,
    signers: tuple[str, ...],
) -> dict[str, Any]:
    """resume 路径：以**当前** config 重判（ADR-030）→ 写审计（红线③）→ 更新 state。"""
    # ADR-030 落点：interrupt 返回后再加载 config —— 拿到的是「当前」版本，非挂起快照。
    config = config_loader()
    state_now = _state_now(state)
    decision = interpret_resume(
        resume_value,
        submitted_at=submitted_at,
        now=state_now,
        timeout_hours=config.approval.timeout_hours,
    )
    pending = [str(cid) for cid in state.get("sendable_candidate_ids", [])]

    if not decision.approved:
        status = "approval-expired" if decision.expired else "rejected"
        outcome = RejudgeOutcome(sendable=(), denied=())
    else:
        status = "resumed-approved"
        outcome = rejudge_with_current_config(
            pending_candidate_ids=pending,
            customer_rows=state.get("customer_rows", []),
            candidate_rows=state.get("candidate_rows", []),
            tool_call_rows=state.get("tool_call_rows", []),
            suppression_rows=state.get("suppression_rows", []),
            now=decision.resumed_at,
            config=config,
        )

    prior_denied = _denied_from_state(state)
    merged_denied = [*prior_denied, *outcome.denied]

    record = RejudgeAudit(
        tenant_id=_uuid_or_none(state.get("tenant_id")) or uuid.uuid4(),
        trace_id=f"gate-rejudge-{state.get('task_id', 'task')}",
        task_id=_uuid_or_none(state.get("task_id")),
        worker_id=_uuid_or_none(state.get("worker_id")),
        approval_status=status,
        rule_version=config.version,
        pending_candidate_ids=tuple(pending),
        sendable_candidate_ids=tuple(outcome.sendable),
        denied=tuple(outcome.denied),
        impact_unique_customers=int(state.get("impact_unique_customers", 0) or 0),
        signers=signers,
    )
    writer = audit_writer if audit_writer is not None else write_rejudge_audit
    writer(record)

    return {
        "sendable_candidate_ids": list(outcome.sendable),
        "denied_candidate_ids": [item.candidate_id for item in merged_denied],
        "suppressed_rule_ids": [item.rule_id for item in merged_denied],
        "denied_details": [
            {
                "candidate_id": item.candidate_id,
                "rule_id": item.rule_id,
                "reason": item.reason,
            }
            for item in merged_denied
        ],
        "gate_outcome": status,
        "approval_status": status,
        "approval_marker": f"batch-{status}",
        "rule_version": config.version,
    }
