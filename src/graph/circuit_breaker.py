"""T-E6 全局熔断（一键停）：停止派发 + 挂起排队 + 恢复按当前 rule_version 重判。

权威依据：

- **架构 v5.2:614（熔断行）**：全局开关；停止派发 + 挂起排队；**实测耗时须记录**；
  **恢复语义 = 挂起动作以当前策略版本（rule_version）重新判定，不沿用旧判定**；
  重新判定结果写审计；**自主度降级同样触发排队动作重判（裁决 #6，与 PRD FR-28 对齐）**。
- **PRD v5.2:159（C2 一键停止）**、**PRD v5.2:407（FR-28）**：一键停止；停止派发、挂起排队；
  恢复后挂起动作以当前策略版本重新判定。
- **红线②**：熔断 / 降级只减流量、不放行——恢复必须走既有重判入口，结论由既有判定给出。
- **红线③**：审计 append-only + 哈希链；写审计唯一入口 = ``insert_statement_sql()``，
  ``record_hash`` 由数据库按同一口径算出（调用方不传）。写库的活落在审计层
  ``src/audit/events.py``（``src/graph`` 保持零 DB 依赖，dry-run 链路不连库）。
- **红线④**：路径完整性断言必经节点不可 opt-in——熔断不是绕过路径断言的通道
  （派发前仍断言，见 ``src/graph/workflow.py`` 的 ``_send_node``）。
- **唯一实现点**：重判复用 ``src/graph/gate.py`` 的 ``rejudge_with_current_config``；
  本模块**不出现**第二份判定 / 三档路由 / 抑制逻辑（源码级断言见
  ``tests/graph/test_circuit_breaker.py``）。
- **多候选排队口径**（复核阻断缺陷后的收口）：
  - **判定输入按上下文整体去重、行绝不合并**——同一批候选共享**同一份批次级
    上下文**，装配时「同一份上下文只算一次」；否则工具调用行翻倍 → 越过频控
    （假拒），候选行翻倍 → 冲突三键全平 → 候选**静默消失**（既不放行也不留痕）；
  - **行不合并是红线②要求**：恢复路径的判定输入必须与正常判定路径**逐行一致**；
    合并行会低估频控计数 → 熔断恢复比正常判定**更宽松**（等于用熔断路径绕开
    ``FREQ_GLOBAL_7D`` 的 DENY）；
  - **每个候选都有明确结论**——放行 / 拒绝 / 显式未决（升级人工）三选一，
    未决候选留在队列并在结论里点名，绝不静默丢弃；
  - **队列结算按 (租户, 轨迹, 候选) 组合定位**——同一 candidate_id 出现在不同
    trace 组时，一组被拒**不得**连带清掉另一组；
  - **可发集合交回派发路径**——恢复后放行的候选进入待派发集合，由派发路径
    （``src/graph/workflow.py`` 的 ``resume_and_dispatch``）消费并真正发出；
    降级时熔断仍打开 → 候选**留在队列**（待恢复时再判），同时明确返回可发集合。
- **持久化**：MVP 熔断状态**进程内**实现（本波唯一迁移写者是 T-E7，不新增迁移）；
  跨进程持久化留 Wave 4。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

from src.approval.audit import DeniedCandidate, RejudgeAudit, write_rejudge_audit
from src.audit.events import AuditEvent, write_audit_event
from src.graph import gate
from src.policy_kernel.config import GovernanceConfig

__all__ = [
    "ACTION_AUTONOMY_DOWNGRADE",
    "ACTION_CIRCUIT_RESUME",
    "ACTION_CIRCUIT_TRIP",
    "AuditEvent",
    "CircuitBreaker",
    "DispatchPlan",
    "PendingAction",
    "RejudgeContext",
    "ResumeOutcome",
    "TripEvent",
    "get_global_breaker",
    "set_global_breaker",
    "write_audit_event",
]

#: 熔断触发的审计 action_type（红线③：可反查）。
ACTION_CIRCUIT_TRIP: Final = "circuit_breaker_trip"
#: 熔断恢复（重判完成）的审计 action_type。
ACTION_CIRCUIT_RESUME: Final = "circuit_breaker_resume"
#: 自主度降级触发重判的审计 action_type（裁决 #6）。
ACTION_AUTONOMY_DOWNGRADE: Final = "autonomy_downgrade_rejudge"

#: 恢复重判在重判审计里的状态标记（与审批恢复区分：来源是熔断/降级，不是人工审批）。
_RESUME_STATUS: Final = "circuit-breaker-resume"

#: 判定输入的**四类行**（装配分桶用；行本身不合并，见 ``_assemble_rows``）。
_ROW_KINDS: Final[tuple[str, ...]] = (
    "customer_rows",
    "candidate_rows",
    "tool_call_rows",
    "suppression_rows",
)


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejudgeContext:
    """重判所需的判定**输入**快照（数据装配，非判定）。

    只承载输入：客户行 / 候选行 / 历史触达 / 抑制命中。判定仍在
    ``src/policy_kernel/rules.evaluate``（唯一实现点）——本模块不复制任何规则。
    """

    customer_rows: tuple[dict[str, Any], ...] = ()
    candidate_rows: tuple[dict[str, Any], ...] = ()
    tool_call_rows: tuple[dict[str, Any], ...] = ()
    suppression_rows: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PendingAction:
    """一个被熔断拦下、进入挂起队列的候选动作。"""

    candidate_id: str
    tenant_id: uuid.UUID
    trace_id: str
    task_id: uuid.UUID | None
    worker_id: uuid.UUID | None
    queued_at: datetime
    context: RejudgeContext


@dataclass(frozen=True)
class TripEvent:
    """熔断触发事件（含实测耗时）。"""

    tripped_at: datetime
    reason: str
    actor: str
    elapsed_ms: float
    rule_version: str


@dataclass(frozen=True)
class DispatchPlan:
    """派发前检查结论。

    - ``allowed``：是否允许派发（熔断打开时恒为 ``False``）；
    - ``pending``：本次被拦下、进入挂起队列的候选；
    - ``hold_reason``：拦截原因标记。
    """

    allowed: bool
    pending: tuple[PendingAction, ...] = ()
    hold_reason: str = ""


@dataclass(frozen=True)
class ResumeOutcome:
    """恢复重判结论（ADR-030：以当前 rule_version 重判的结果）。

    - ``sendable_candidate_ids``：重判**放行集合**——交回派发路径（消费者 =
      ``src/graph/workflow.py`` 的派发逻辑，熔断感知）；
    - ``denied``：被拒集合（携带 rule_id 与理由，红线②留痕）；
    - ``undecided_candidate_ids``：规则**未给出结论**的候选（如冲突三键全平 →
      升级人工）——显式上报，**绝不静默丢弃**；
    - ``rejudged_candidate_count``：本次重判覆盖的候选数（去重后）。
    """

    rule_version: str
    sendable_candidate_ids: tuple[str, ...]
    denied: tuple[DeniedCandidate, ...]
    rejudged_candidate_count: int
    elapsed_ms: float
    undecided_candidate_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """当前 UTC 时刻（aware）。"""
    return datetime.now(UTC)


def _as_aware(moment: datetime) -> datetime:
    """时刻归一为 aware（naive 视为 UTC）——避免时间差计算抛异常。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _affected_customers(actions: Sequence[PendingAction]) -> int:
    """挂起动作涉及的去重客户数（审计载荷用；纯计数，不做任何路由判断）。"""
    customers: set[str] = set()
    for action in actions:
        for row in action.context.candidate_rows:
            customer_id = row.get("customer_id")
            if customer_id is not None:
                customers.add(str(customer_id))
    return len(customers)


def _distinct(values: Iterable[str]) -> list[str]:
    """按首次出现顺序去重（同一候选在一批里只算一次）。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _first_uuid(values: Iterable[uuid.UUID | None]) -> uuid.UUID | None:
    """取第一个非空 id（审计的 task_id / worker_id 透传用）。"""
    for value in values:
        if value is not None:
            return value
    return None


def _assemble_rows(actions: Sequence[PendingAction]) -> dict[str, list[dict[str, Any]]]:
    """装配重判的判定输入行——**按上下文整体去重，行一律不合并**。

    **为什么必须去重**：``before_dispatch`` 给同一批的每个候选存的是**同一份批次级
    上下文**，若按「每个候选拼一次行」装配，同一行会被计入 N 次——

    - 工具调用行翻倍 → 全局频控计数翻倍 → 误判越限（假拒）；
    - 候选行翻倍 → 同一候选出现两次 → 冲突三键全平 → 候选既不放行也不留痕
      （**静默消失**）。

    **为什么行绝不合并（红线②）**：熔断恢复路径的判定输入必须与正常判定路径
    （``workflow._decision_node``）**逐行一致**——正常路径不去重，恢复路径一旦
    合并行就会**低估频控计数** → 熔断恢复比正常判定**更宽松**（等于用熔断路径
    绕开 ``FREQ_GLOBAL_7D`` 的 DENY）。兜底键 ``(called_at, channel)`` 正是这种
    合并：无稳定 id 的两条**独立**触达会被并成一条。

    因此装配口径 = **同一份上下文只算一次**（按上下文内容去重），上下文内部的
    行**一行不少**地交给既有判定入口；客户多行时由既有字典推导取**末条**
    （与正常路径同一口径）。
    """
    buckets: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _ROW_KINDS}
    seen_contexts: list[RejudgeContext] = []
    for action in actions:
        context = action.context
        if any(context == seen for seen in seen_contexts):
            continue
        seen_contexts.append(context)
        buckets["customer_rows"].extend(dict(row) for row in context.customer_rows)
        buckets["candidate_rows"].extend(dict(row) for row in context.candidate_rows)
        buckets["tool_call_rows"].extend(dict(row) for row in context.tool_call_rows)
        buckets["suppression_rows"].extend(dict(row) for row in context.suppression_rows)
    return buckets


class CircuitBreaker:
    """全局熔断开关（架构 v5.2:614）。

    语义：

    - **打开**：所有数字员工的新动作**停止派发**（``before_dispatch`` 返回
      ``allowed=False``），候选进入**挂起队列**；
    - **恢复**：挂起动作以**当前** ``rule_version`` **重新判定**
      （ADR-030：``config`` 在恢复时刻加载，不沿用挂起时快照），重判结论写审计；
      放行集合**交回派发路径**（待派发集合 → 派发路径消费），不丢候选；
    - **实测耗时**：触发耗时（进入 ``trip`` → 熔断状态生效）与「触发 → 首次停止
      派发」延迟均被记录（单调时钟测量）。

    **一致性红线③**：熔断态与审计必须一致——触发审计写失败即撤销熔断态
    （补偿），不得出现「已熔断但无审计」。

    MVP = 进程内实现（跨进程持久化留 Wave 4）。
    """

    def __init__(
        self,
        *,
        config_loader: Callable[[], GovernanceConfig] | None = None,
        event_audit_writer: Callable[[AuditEvent], Any] | None = None,
        rejudge_audit_writer: Callable[[RejudgeAudit], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """构造熔断器。

        ``config_loader``：当前治理配置来源（默认 = ``governance.yaml``）；
        ``event_audit_writer`` / ``rejudge_audit_writer``：审计写入器（测试缝；
        默认走 ``insert_statement_sql()``）；``clock`` / ``monotonic``：时钟（测试缝）。
        """
        self._config_loader = config_loader
        self._event_audit_writer = event_audit_writer
        self._rejudge_audit_writer = rejudge_audit_writer
        self._clock = clock if clock is not None else _utc_now
        self._monotonic = monotonic if monotonic is not None else time.perf_counter
        self._open = False
        self._pending: list[PendingAction] = []
        self._ready: list[str] = []
        self._trip: TripEvent | None = None
        self._trip_tenant: uuid.UUID | None = None
        self._dispatch_stop_elapsed_ms: float | None = None
        self._default_tenant = uuid.uuid4()

    # -- 状态 -------------------------------------------------------------

    def is_open(self) -> bool:
        """熔断是否打开（打开 = 停止派发）。"""
        return self._open

    @property
    def pending_candidate_ids(self) -> tuple[str, ...]:
        """挂起队列中的候选 id（顺序 = 入队顺序）。"""
        return tuple(item.candidate_id for item in self._pending)

    @property
    def pending_actions(self) -> tuple[PendingAction, ...]:
        """挂起队列快照。"""
        return tuple(self._pending)

    @property
    def ready_candidate_ids(self) -> tuple[str, ...]:
        """已重判放行、**等待派发**的候选（顺序 = 重判结论顺序）。

        恢复（``reset``）把放行集合交回派发路径：交回之前它们既不在挂起队列里
        （已判完），也还没发出去——本集合就是这段窗口的**显式落点**，杜绝
        「重判出可发集合却无人派发 = 候选静默消失」。
        """
        return tuple(self._ready)

    @property
    def dispatch_stop_elapsed_ms(self) -> float | None:
        """实测：熔断触发 → 首次停止派发的耗时（毫秒；未发生则 ``None``）。"""
        return self._dispatch_stop_elapsed_ms

    @property
    def trip_event(self) -> TripEvent | None:
        """最近一次熔断触发事件（未触发过则 ``None``）。"""
        return self._trip

    # -- 触发 -------------------------------------------------------------

    def trip(
        self,
        *,
        reason: str,
        actor: str,
        tenant_id: uuid.UUID | None = None,
        trace_id: str = "",
        task_id: uuid.UUID | None = None,
        worker_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> TripEvent:
        """触发熔断：全局停止派发；实测触发耗时并写审计（架构 v5.2:614）。

        「实测耗时」= 从进入本方法到熔断状态真正生效（此后任何派发都被拒）的
        真实耗时，由**单调时钟**测量（不受墙钟跳变影响）。
        """
        started = self._monotonic()
        moment = _as_aware(now if now is not None else self._clock())
        tenant = tenant_id if tenant_id is not None else self._default_tenant
        config = self._load_config()

        # 补偿用的前态：审计写失败时**恢复前态**（而不是无条件关闭）——
        # 重复触发熔断且第二次审计失败时，熔断必须保持打开（首次触发已有审计）。
        previous_state = (
            self._open,
            self._trip,
            self._trip_tenant,
            self._dispatch_stop_elapsed_ms,
        )
        self._open = True
        self._trip_tenant = tenant
        self._dispatch_stop_elapsed_ms = None
        event = TripEvent(
            tripped_at=moment,
            reason=reason,
            actor=actor,
            elapsed_ms=0.0,
            rule_version=config.version,
        )
        event = replace(event, elapsed_ms=(self._monotonic() - started) * 1000.0)
        self._trip = event

        try:
            self._write_event(
                AuditEvent(
                    tenant_id=tenant,
                    trace_id=trace_id or f"circuit-breaker-{tenant}",
                    task_id=task_id,
                    worker_id=worker_id,
                    action_type=ACTION_CIRCUIT_TRIP,
                    rule_version=config.version,
                    actor_type="system",
                    actor_id="circuit-breaker",
                    actor_role="system",
                    input_snapshot={"tripped_at": moment.isoformat()},
                    evidence={
                        "adr": "ADR-030",
                        "scope": "global-switch",
                        "measure": "trip-to-dispatch-stop",
                        "reason": reason,
                        "actor": actor,
                        "elapsed_ms": event.elapsed_ms,
                    },
                    payload={"state": "open"},
                )
            )
        except Exception:
            # 补偿（红线③）：审计写失败 → **撤销本次熔断态**（恢复前态）。
            # 不得出现「已熔断但无审计」——审计不可反查的熔断态比不熔断更危险。
            # 恢复**前态**而非无条件关闭：重复触发熔断时，首次触发已有审计，
            # 熔断必须保持打开（否则审计说 open、实际却是 closed）。
            (
                self._open,
                self._trip,
                self._trip_tenant,
                self._dispatch_stop_elapsed_ms,
            ) = previous_state
            raise
        return event

    # -- 派发前检查 -------------------------------------------------------

    def before_dispatch(
        self,
        *,
        candidate_ids: Sequence[str],
        context: RejudgeContext | None = None,
        tenant_id: uuid.UUID | None = None,
        trace_id: str = "",
        task_id: uuid.UUID | None = None,
        worker_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> DispatchPlan:
        """派发前检查（架构 v5.2:614 的触发点，在 send 之前）。

        熔断关闭 → 放行（与既有流程一致）；熔断打开 → **不派发**，
        候选进入挂起队列，等待恢复时按当前策略版本重判。

        放行路径顺带做**派发结算**：本次真正派发的候选从挂起队列与待派发集合中
        出队（派发即终结），避免「已派发的候选仍挂在队列上」。
        """
        moment = _as_aware(now if now is not None else self._clock())
        # 租户缺省取「熔断触发时的租户」——同一开关下的动作落在同一条审计链上。
        tenant = tenant_id if tenant_id is not None else (self._trip_tenant or self._default_tenant)
        batch_trace = trace_id or f"circuit-breaker-{tenant}"
        if not self._open:
            self._settle_dispatched(candidate_ids, trace_id=batch_trace)
            return DispatchPlan(allowed=True)

        snapshot = context if context is not None else RejudgeContext()
        # 同一批候选共享**同一份批次级上下文**（不复制 N 份）；重判装配时再按主键去重。
        # 已在队列里的同一（租户, 轨迹, 候选）不再重复入队（防队列膨胀）。
        queued_keys = {(item.tenant_id, item.trace_id, item.candidate_id) for item in self._pending}
        pending = tuple(
            PendingAction(
                candidate_id=candidate_id,
                tenant_id=tenant,
                trace_id=batch_trace,
                task_id=task_id,
                worker_id=worker_id,
                queued_at=moment,
                context=snapshot,
            )
            for candidate_id in _distinct(str(cid) for cid in candidate_ids)
            if (tenant, batch_trace, candidate_id) not in queued_keys
        )
        self._pending.extend(pending)
        # 回报**本批次当前**的挂起队列（已入队的候选不重复入队，但仍在本批次队列里）。
        batch_pending = tuple(
            item
            for item in self._pending
            if item.tenant_id == tenant and item.trace_id == batch_trace
        )

        if self._dispatch_stop_elapsed_ms is None and self._trip is not None:
            self._dispatch_stop_elapsed_ms = (
                moment - self._trip.tripped_at
            ).total_seconds() * 1000.0

        return DispatchPlan(
            allowed=False, pending=batch_pending, hold_reason="circuit-breaker-open"
        )

    # -- 恢复 / 降级 ------------------------------------------------------

    def reset(
        self,
        *,
        reason: str = "operator-resume",
        actor: str = "operator",
        now: datetime | None = None,
    ) -> ResumeOutcome:
        """熔断恢复：挂起动作以**当前** ``rule_version`` 重新判定（ADR-030）。

        关键落点：``config`` 在**恢复时刻**加载（不是挂起时的快照）——挂起期间
        的策略变更（收紧频控等）在恢复时立即生效；重判结论写审计（红线③）。

        放行集合**交回派发路径**：结论里的 ``sendable_candidate_ids`` 即待派发集合
        （``ready_candidate_ids``），由派发路径消费（``src/graph/workflow.py``）；
        尚未被派发前它们既不在挂起队列（已判完）也没发出去——本方法不丢候选。
        """
        moment = _as_aware(now if now is not None else self._clock())
        config = self._load_config()
        outcome = self._rejudge_pending(
            config=config,
            now=moment,
            reason=reason,
            actor=actor,
            action_type=ACTION_CIRCUIT_RESUME,
            retain_sendable=False,
        )
        self._open = False
        return outcome

    def apply_autonomy_downgrade(
        self,
        *,
        reason: str,
        actor: str = "system",
        now: datetime | None = None,
    ) -> ResumeOutcome:
        """自主度降级 → 排队动作重判（裁决 #6 / 架构 v5.2:238）。

        与熔断恢复**同一**重判入口（``gate.rejudge_with_current_config``），
        不另写判定；降级本身不关闭熔断开关，只让排队动作按当前策略版本重新判定
        （裁决 #6 与 PRD FR-28 对齐）。

        **队列不丢**：熔断仍打开 → 此刻**不可派发** → 重判放行的候选**留在挂起
        队列**（待熔断恢复时按当时策略再判一次，ADR-030 语义一致），
        同时在结论的 ``sendable_candidate_ids`` 里**明确返回**可发集合。
        """
        moment = _as_aware(now if now is not None else self._clock())
        config = self._load_config()
        return self._rejudge_pending(
            config=config,
            now=moment,
            reason=reason,
            actor=actor,
            action_type=ACTION_AUTONOMY_DOWNGRADE,
            retain_sendable=True,
        )

    # -- 内部 -------------------------------------------------------------

    def _load_config(self) -> GovernanceConfig:
        """加载**当前**治理配置（阈值与 ``rule_version`` 的唯一来源）。"""
        if self._config_loader is not None:
            return self._config_loader()
        return gate.load_current_config()

    def _rejudge_pending(
        self,
        *,
        config: GovernanceConfig,
        now: datetime,
        reason: str,
        actor: str,
        action_type: str,
        retain_sendable: bool,
    ) -> ResumeOutcome:
        """挂起队列重判 + 审计（唯一实现点 = ``gate.rejudge_with_current_config``）。

        按（租户, 轨迹）分组重判，避免不同批次的输入互相污染；组内判定输入
        **按主键去重**装配（同一批次上下文不得按候选数量重复计入）。

        **队列结算**（候选绝不静默消失）：

        - 被规则拒绝的候选 → 出队（终态，留痕在 ``denied``）；
        - 重判放行的候选 → ``retain_sendable=False``（恢复）时出队并进入**待派发
          集合**（交回派发路径）；``retain_sendable=True``（降级，熔断仍打开）
          时**留在队列**（此刻不可派发），同时在结论里明确返回可发集合；
        - 规则未给出结论的候选（升级人工）→ **留在队列**并在
          ``undecided_candidate_ids`` 中显式上报。
        """
        groups: dict[tuple[uuid.UUID, str], list[PendingAction]] = {}
        for action in self._pending:
            groups.setdefault((action.tenant_id, action.trace_id), []).append(action)

        started = self._monotonic()
        sendable: list[str] = []
        denied: list[DeniedCandidate] = []
        undecided: list[str] = []
        # 队列结算键 = **(租户, 轨迹, 候选)** 组合——同一 candidate_id 出现在不同
        # trace 组时，一组被拒**不得**连带清掉另一组（否则候选静默消失）。
        concluded: set[tuple[uuid.UUID, str, str]] = set()
        denied_keys: set[tuple[uuid.UUID, str, str]] = set()
        rejudged_count = 0

        for (tenant_id, trace_id), actions in groups.items():
            candidate_ids = _distinct(item.candidate_id for item in actions)
            rows = _assemble_rows(actions)
            outcome = gate.rejudge_with_current_config(
                pending_candidate_ids=candidate_ids,
                customer_rows=rows["customer_rows"],
                candidate_rows=rows["candidate_rows"],
                tool_call_rows=rows["tool_call_rows"],
                suppression_rows=rows["suppression_rows"],
                now=now,
                config=config,
            )
            group_sendable = list(outcome.sendable)
            group_denied = list(outcome.denied)
            denied_ids = {item.candidate_id for item in group_denied}
            # 规则没给出结论的候选（既不在放行集也不在拒绝集）→ 显式记账，不静默丢弃。
            group_undecided = [
                candidate_id
                for candidate_id in candidate_ids
                if candidate_id not in set(group_sendable) and candidate_id not in denied_ids
            ]
            sendable.extend(group_sendable)
            denied.extend(group_denied)
            undecided.extend(group_undecided)
            concluded.update((tenant_id, trace_id, cid) for cid in group_sendable)
            concluded.update((tenant_id, trace_id, cid) for cid in denied_ids)
            denied_keys.update((tenant_id, trace_id, cid) for cid in denied_ids)
            rejudged_count += len(candidate_ids)
            self._write_rejudge_audit(
                RejudgeAudit(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    task_id=_first_uuid(item.task_id for item in actions),
                    worker_id=_first_uuid(item.worker_id for item in actions),
                    approval_status=_RESUME_STATUS,
                    rule_version=config.version,
                    pending_candidate_ids=tuple(candidate_ids),
                    sendable_candidate_ids=tuple(group_sendable),
                    denied=tuple(group_denied),
                    impact_unique_customers=_affected_customers(actions),
                    signers=(),
                )
            )

        elapsed_ms = (self._monotonic() - started) * 1000.0

        all_actions = [item for actions in groups.values() for item in actions]

        first_key = next(iter(groups), None)
        tenant = (
            first_key[0] if first_key is not None else (self._trip_tenant or self._default_tenant)
        )
        trace = first_key[1] if first_key is not None else ""
        # 收尾事件先写：写失败 → 异常上抛 → **队列原样保留**（熔断仍打开，
        # 下次恢复会按当时策略把这些候选重新判定一次，候选不丢）。
        self._write_event(
            AuditEvent(
                tenant_id=tenant,
                trace_id=trace or f"circuit-breaker-{tenant}",
                # 恢复事件的 task_id / worker_id **透传**挂起队列里的真实值
                # （审计必须能反查到具体任务与数字员工）。
                task_id=_first_uuid(item.task_id for item in all_actions),
                worker_id=_first_uuid(item.worker_id for item in all_actions),
                action_type=action_type,
                rule_version=config.version,
                actor_type="system",
                actor_id="circuit-breaker",
                actor_role="system",
                hit_rule_ids=tuple(str(item.rule_id) for item in denied),
                input_snapshot={"rejudged_candidate_count": rejudged_count},
                evidence={
                    "adr": "ADR-030",
                    "scope": "resume-rejudge",
                    "measure": "rejudge-elapsed",
                    "dispatch_stop_elapsed_ms": self._dispatch_stop_elapsed_ms,
                    "reason": reason,
                    "actor": actor,
                    "elapsed_ms": elapsed_ms,
                },
                payload={
                    "sendable_candidate_ids": list(sendable),
                    "denied_candidate_ids": [item.candidate_id for item in denied],
                    "undecided_candidate_ids": list(undecided),
                },
            )
        )

        # 队列结算（见方法说明）：**收尾事件审计写成功之后**才动队列——
        # 审计写失败时异常上抛、队列原样保留（候选不丢，下次恢复重判）。
        # 结算按 **(租户, 轨迹, 候选)** 组合定位，不跨组连带清理。
        if retain_sendable:
            self._pending = [
                item
                for item in self._pending
                if (item.tenant_id, item.trace_id, item.candidate_id) not in denied_keys
            ]
        else:
            self._pending = [
                item
                for item in self._pending
                if (item.tenant_id, item.trace_id, item.candidate_id) not in concluded
            ]
            for candidate_id in sendable:
                if candidate_id not in self._ready:
                    self._ready.append(candidate_id)

        return ResumeOutcome(
            rule_version=config.version,
            sendable_candidate_ids=tuple(sendable),
            denied=tuple(denied),
            rejudged_candidate_count=rejudged_count,
            elapsed_ms=elapsed_ms,
            undecided_candidate_ids=tuple(undecided),
        )

    def _settle_dispatched(self, candidate_ids: Sequence[str], *, trace_id: str = "") -> None:
        """派发结算：本次真正派发的候选从挂起队列与待派发集合中出队。

        派发即终结——候选不该继续挂在「待重判 / 待派发」集合里。

        挂起队列按 **(轨迹, 候选)** 组合出队：同一 candidate_id 在别的轨迹组里
        排着时，本次派发不牵连它（否则那条候选**静默消失**）。
        """
        ids = {str(candidate_id) for candidate_id in candidate_ids}
        if not ids:
            return
        if trace_id:
            self._pending = [
                item
                for item in self._pending
                if not (item.candidate_id in ids and item.trace_id == trace_id)
            ]
        else:
            self._pending = [item for item in self._pending if item.candidate_id not in ids]
        self._ready = [candidate_id for candidate_id in self._ready if candidate_id not in ids]

    def _write_event(self, record: AuditEvent) -> None:
        """写熔断事件审计（红线③：默认走 ``insert_statement_sql``）。"""
        writer = (
            self._event_audit_writer if self._event_audit_writer is not None else write_audit_event
        )
        writer(record)

    def _write_rejudge_audit(self, record: RejudgeAudit) -> None:
        """写恢复重判审计（复用 ``src.approval.audit`` 的既有入口）。"""
        writer = (
            self._rejudge_audit_writer
            if self._rejudge_audit_writer is not None
            else write_rejudge_audit
        )
        writer(record)


# ---------------------------------------------------------------------------
# 全局开关
# ---------------------------------------------------------------------------

_GLOBAL_BREAKER: CircuitBreaker | None = None


def get_global_breaker() -> CircuitBreaker:
    """全局熔断开关单例（架构 v5.2:614：全局开关——所有数字员工共用一个）。"""
    global _GLOBAL_BREAKER
    if _GLOBAL_BREAKER is None:
        _GLOBAL_BREAKER = CircuitBreaker()
    return _GLOBAL_BREAKER


def set_global_breaker(breaker: CircuitBreaker | None) -> None:
    """替换全局熔断开关（测试隔离 / 接线用；``None`` = 复位为默认实例）。"""
    global _GLOBAL_BREAKER
    _GLOBAL_BREAKER = breaker
