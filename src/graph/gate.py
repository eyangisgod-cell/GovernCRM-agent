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
  **时间源信任模型**：过期与否**只**由服务端真实墙钟决定（挂起时刻 + 恢复时刻）；
  客户端自带的 ``resumed_at`` 不受信任——仅作参考记录，**不参与判定**
  （客户端可回填过去时刻或伪造未来时刻绕过超时，E9 将形同虚设）。
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

from langgraph.config import get_config
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
    """resume 值解读结果（E9：``expired`` 时一律不通过）。

    字段语义见 ``interpret_resume`` 的时间源信任模型：

    - ``approved``：是否放行（``expired`` 时恒为 ``False``——过期不默认通过）；
    - ``expired``：是否超时，**只**由服务端墙钟与服务端挂起时刻决定；
    - ``resumed_at``：恢复时刻 = **服务端墙钟**（可信）；
    - ``rule_judgement_at``：业务规则重判的求值时刻（任务级锚点，规则语义）；
    - ``client_resumed_at``：客户端自报的恢复时刻，**仅提取**（随本结构返回，
      当前**不落审计**），不参与任何判定。
    """

    approved: bool
    expired: bool
    resumed_at: datetime
    rule_judgement_at: datetime
    client_resumed_at: datetime | None = None


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
    submitted_at: datetime | None,
    now: datetime,
    rule_now: datetime,
    timeout_hours: int,
) -> ResumeDecision:
    """解读 ``Command(resume=...)`` 的值（裁决 #8：任务级批量审批，一次覆盖全批）。

    resume 值约定：dict ``{"approved": bool, "resumed_at": datetime?, ...}`` 或任意真值。

    **时间源信任模型（E9 / 裁决 #25）**：

    | 时间 | 来源 | 可信 | 用途 |
    | --- | --- | --- | --- |
    | ``submitted_at`` | 服务端记录的挂起时刻 | ✅ | 过期窗口的起点 |
    | ``now`` | 服务端墙钟（恢复时刻） | ✅ | 过期窗口的当前时刻 |
    | ``rule_now`` | 任务级判定锚点（state 的 ``now``） | ✅（规则语义） | 业务规则重判的求值时刻 |
    | 客户端 ``resumed_at`` | 请求载荷 | ❌ | **仅提取**（``client_resumed_at``，当前不落审计），不参与判定 |

    客户端可自造任意时刻：回填一个**过去**时刻 → 旧实现判「未过期」；伪造一个
    **未来**时刻 → 把窗口整体推后。两者都能绕过 48h 超时，E9 因此形同虚设。
    故客户端时间**一律不作判定依据**，过期与否只由服务端墙钟决定。

    ``submitted_at is None`` = 服务端无法确定挂起时刻 → **fail-closed**：
    视为过期（无法证明未超时就不放行，与「过期不默认通过」同向）。
    """
    client_resumed_at: datetime | None = None
    if isinstance(resume_value, Mapping):
        approved = bool(resume_value.get("approved", False))
        client_value = resume_value.get("resumed_at")
        client_resumed_at = client_value if isinstance(client_value, datetime) else None
    else:
        approved = bool(resume_value)

    def _decide(expired: bool) -> ResumeDecision:
        return ResumeDecision(
            approved=False if expired else approved,
            expired=expired,
            resumed_at=now,
            rule_judgement_at=rule_now,
            client_resumed_at=client_resumed_at,
        )

    if submitted_at is None:
        return _decide(expired=True)
    timeout = check_timeout(submitted_at, now, timeout_hours=timeout_hours)
    return _decide(expired=timeout.expired)


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
    """state 的判定时刻（缺省当前 UTC 时刻；mypy 友好的窄化）。

    **这是业务规则重判的求值时刻**（规则语义），**不是**超时判定的时钟——
    超时判定只用服务端墙钟（见 ``_utc_now``）。
    """
    value = state.get("now")
    return value if isinstance(value, datetime) else _utc_now()


def _utc_now() -> datetime:
    """服务端墙钟（UTC 当前时刻）——E9 超时判定的**唯一**时间基准。

    挂起时刻与恢复时刻都取自本函数；客户端提供的 ``resumed_at`` 不参与判定。
    测试通过 monkeypatch 本函数推进时间（不依赖真实 sleep）。
    """
    return datetime.now(UTC)


def _pending_interrupt_payload(
    pending_writes: Any,
    task_id: str | None,
) -> Mapping[str, Any] | None:
    """从 checkpointer 的 pending writes 里取出**本次挂起**的 interrupt 载荷。

    按「载荷里含 ``submitted_at``」识别，不依赖 LangGraph 的私有通道名。
    """
    if not isinstance(pending_writes, (list, tuple)):
        return None
    for write_task_id, _channel, value in pending_writes:
        if task_id is not None and write_task_id != task_id:
            continue
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            payload = getattr(item, "value", None)
            if isinstance(payload, Mapping) and "submitted_at" in payload:
                return payload
    return None


def _persisted_interrupt_payload() -> Mapping[str, Any] | None:
    """取回 checkpointer 中已持久化的**本次挂起**的 interrupt 载荷。

    为什么需要它：LangGraph 在节点抛 ``GraphInterrupt`` 时会**丢弃**该节点本次的
    state 写入（只保留 interrupt 载荷本身），因此「挂起时刻」无法随 state 穿过
    中断；服务端唯一留痕 = checkpointer 持久化的 interrupt 载荷（跨进程可靠，
    Postgres checkpointer 同样落库）。

    返回 ``None`` = 没有可用记录（尚未挂起 / 无 checkpointer / 记录不可读）。
    """
    try:
        conf = get_config()
    except Exception:  # pragma: no cover - 不在图内执行时无 runnable config
        return None
    configurable = conf.get("configurable", {})
    saver = configurable.get("__pregel_checkpointer")
    thread_id = configurable.get("thread_id")
    task_id = configurable.get("__pregel_task_id")
    if saver is None or not thread_id:
        return None
    # pending writes 落在**图级** checkpoint 命名空间下（根图 = ""）；
    # 节点 config 里的 checkpoint_ns 是任务级命名空间（"gate:<task_id>"），两者不同。
    ns_candidates = ["", str(configurable.get("checkpoint_ns") or "")]
    for ns in ns_candidates:
        thread_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
        try:
            latest = saver.get_tuple(thread_config)
        except Exception:  # pragma: no cover - checkpointer 读失败 → 无记录（fail-closed）
            latest = None
        if latest is not None:
            payload = _pending_interrupt_payload(getattr(latest, "pending_writes", None), task_id)
            if payload is not None:
                return payload
        try:
            for tup in saver.list(thread_config):
                payload = _pending_interrupt_payload(getattr(tup, "pending_writes", None), task_id)
                if payload is not None:
                    return payload
        except Exception:  # pragma: no cover - 同上
            continue
    return None


def _server_submitted_at() -> datetime | None:
    """服务端记录的**挂起时刻**（可信时间源；``None`` = 尚无服务端记录）。

    - 挂起路径：还没有记录 → ``None``（调用方记当前服务端墙钟）；
    - 恢复路径：从 checkpointer 已持久化的 interrupt 载荷取回**挂起那次**记录的时刻
      （服务端留痕，客户端无法伪造）。
    """
    payload = _persisted_interrupt_payload()
    if payload is None:
        return None
    value = payload.get("submitted_at")
    return value if isinstance(value, datetime) else None


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

    **E9 超时判定的时间源**：挂起时刻 = 服务端墙钟（写入 interrupt 载荷，恢复时从
    checkpointer 已持久化的载荷取回），恢复时刻 = 服务端墙钟；两者都**不**取自
    state["now"] 或客户端载荷。详见 ``interpret_resume`` 的时间源信任模型。
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
    # E9 时间源：超时窗口的起点 = **服务端记录的真实挂起时刻**。
    # - 恢复路径：``interrupt()`` 返回（挂起时它抛 GraphInterrupt，走不到这里），
    #   挂起时刻从 checkpointer 已持久化的 interrupt 载荷取回（服务端留痕，
    #   客户端无法伪造）；取不到 → ``None`` → fail-closed（视为过期，不放行）。
    # - 挂起路径：此刻还没有服务端记录 → 用当前服务端墙钟。
    # 不用 state["now"]——那是任务起始时刻，随任务推进变旧，会让超时永远不触发。
    submitted_at = _server_submitted_at()
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
        "submitted_at": submitted_at if submitted_at is not None else _utc_now(),
    }
    resume_value = interrupt(payload)
    return _apply_resume(
        state,
        resume_value,
        submitted_at=submitted_at,
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
    submitted_at: datetime | None,
    config_loader: Callable[[], GovernanceConfig],
    audit_writer: Callable[[RejudgeAudit], Any] | None,
    signers: tuple[str, ...],
) -> dict[str, Any]:
    """resume 路径：以**当前** config 重判（ADR-030）→ 写审计（红线③）→ 更新 state。"""
    # ADR-030 落点：interrupt 返回后再加载 config —— 拿到的是「当前」版本，非挂起快照。
    config = config_loader()
    # 服务端墙钟 = 超时判定的当前时刻（E9：唯一可信的"现在"）。
    server_now = _utc_now()
    # 业务规则重判的求值时刻 = 任务级锚点（规则语义，与超时判定无关）。
    rule_now = _state_now(state)
    decision = interpret_resume(
        resume_value,
        submitted_at=submitted_at,
        now=server_now,
        rule_now=rule_now,
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
            now=decision.rule_judgement_at,
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
