"""T-E6 全局熔断（一键停）验收用例：停止派发 / 挂起排队 / 恢复按当前 rule_version 重判 / 审计。

权威依据：

- **架构 v5.2:614（熔断行）**：全局开关；停止派发 + 挂起排队；**实测耗时须记录**；
  **恢复语义 = 挂起动作以当前策略版本（rule_version）重新判定，不沿用旧判定**；
  重新判定结果写审计；**自主度降级同样触发排队动作重判（裁决 #6，与 PRD FR-28 对齐）**。
- **PRD v5.2:159（C2 一键停止）**、**PRD v5.2:407（FR-28）**：停止派发、挂起排队；
  恢复后挂起动作以当前策略版本重新判定。
- **红线②**：熔断 / 降级只减流量、不放行——恢复必须走既有重判入口，重判结论由既有判定给出。
- **红线③**：审计 append-only + 哈希链；写审计唯一入口 = ``insert_statement_sql()``，
  ``record_hash`` 由 DB 算。
- **唯一实现点**：重判复用 ``src/graph/gate.py`` 的 ``rejudge_with_current_config``——
  熔断模块内不出现第二份判定 / 路由 / 抑制逻辑（源码级断言见末节）。

用例标记：纯内存用例标 ``unit``；真 PG 审计用例标 ``pg``（默认 ``-m "not pg"`` 跳过）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src.approval import audit as approval_audit
from src.approval.audit import RejudgeAudit, write_rejudge_audit
from src.audit import events as audit_events
from src.audit.events import AuditEvent, write_audit_event
from src.audit.hash_chain import verify_chain, verify_query_sql
from src.graph import circuit_breaker, gate, workflow
from src.graph.circuit_breaker import (
    ACTION_CIRCUIT_RESUME,
    ACTION_CIRCUIT_TRIP,
    CircuitBreaker,
    RejudgeContext,
)
from src.policy_kernel.config import GovernanceConfig, load_and_validate

#: 熔断模块自身不含任何真 PG 依赖（psycopg 仅惰性 import）——dry-run 链路零 DB。
pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 挂起时的策略版本（对照 ADR-030：恢复后必须用新版本，不得沿用此快照）。
_SUSPEND_RULE_VERSION = "2026.10.26-suspend-snapshot"
#: 恢复前替换进来的**当前**策略版本（测试中途修改 version 字段，prd 验收②）。
_CURRENT_RULE_VERSION = "2026.10.26-current-rejudge"

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

#: 基准 config 的全局频控上限（governance.yaml 种子值 = 2）。
_BASE_FREQ_MAX_TOUCHES = 2
#: 恢复时收紧后的频控上限（1 次窗口内触达即超限）。
_TIGHT_FREQ_MAX_TOUCHES = 1


# ---------------------------------------------------------------------------
# 夹具与样例数据
# ---------------------------------------------------------------------------


def _base_config() -> GovernanceConfig:
    """仓库根 governance.yaml 的基准 config（策略参数唯一来源）。"""
    return load_and_validate(_REPO_ROOT / "governance.yaml")


def _config_with(*, version: str, freq_max_touches: int) -> GovernanceConfig:
    """构造指定版本 config（改 version 字段 + 频控阈值，证明重判吃的是新对象）。"""
    base = _base_config()
    return replace(
        base,
        version=version,
        thresholds=replace(base.thresholds, freq_max_touches=freq_max_touches),
    )


def _customer_row(
    customer_id: str = "CUST-1",
    *,
    consent_status: str = "granted",
) -> dict[str, Any]:
    """一条可放行的客户行（无投诉 / 有授权 / 非 Top1%）。"""
    return {
        "customer_id": customer_id,
        "avg_order_value_90d": 500.0,
        "last_order_days_ago": 10,
        "complaint_count_90d": 0,
        "consent_status": consent_status,
        "refund_rate": 0.01,
        "total_spend_percentile": 50.0,
        "explicit_refusal": False,
    }


def _candidate_row(candidate_id: str = "CAND-1", customer_id: str = "CUST-1") -> dict[str, Any]:
    """一条候选动作行（created_at 落在冲突窗口内）。"""
    return {
        "candidate_id": candidate_id,
        "customer_id": customer_id,
        "platform": "taobao",
        "channel": "sms",
        "coupon_value_cents": 5000,
        "coupon_threshold_cents": 10000,
        "created_at": "2026-10-25T09:00:00+00:00",
    }


def _tool_call_row(
    *,
    called_at: datetime,
    channel: str = "email",
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """一条历史触达记录（频控计数输入；走 email 通道以避开 sms 72h 同通道冷却）。

    ``tool_call_id`` 缺省 = **无稳定 id**（此时任何按业务字段的去重都会把两条
    独立触达合并成一条 → 频控计数被低估）。
    """
    row: dict[str, Any] = {"called_at": called_at, "channel": channel}
    if tool_call_id is not None:
        row["tool_call_id"] = tool_call_id
    return row


def _context() -> RejudgeContext:
    """重判所需的判定输入快照（数据装配，非判定）。"""
    return RejudgeContext(
        customer_rows=(_customer_row(),),
        candidate_rows=(_candidate_row(),),
        tool_call_rows=(_tool_call_row(called_at=_NOW - timedelta(days=1)),),
        suppression_rows=(),
    )


def _two_candidate_context() -> RejudgeContext:
    """**一个批次**里的两个候选（两个客户）——批次级上下文对两个候选是同一份。"""
    return RejudgeContext(
        customer_rows=(_customer_row("CUST-1"), _customer_row("CUST-2")),
        candidate_rows=(_candidate_row("CAND-1", "CUST-1"), _candidate_row("CAND-2", "CUST-2")),
        tool_call_rows=(_tool_call_row(called_at=_NOW - timedelta(days=1)),),
        suppression_rows=(),
    )


class _RecordingWriters:
    """假审计写入器：记录熔断事件与恢复重判载荷（单测不发真库）。"""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.rejudges: list[RejudgeAudit] = []

    def write_event(self, record: AuditEvent) -> tuple[str, int, str]:
        self.events.append(record)
        return ("audit-event", len(self.events), "0" * 64)

    def write_rejudge(self, record: RejudgeAudit) -> tuple[str, int, str]:
        self.rejudges.append(record)
        return ("audit-rejudge", len(self.rejudges), "0" * 64)


def _make_breaker(
    *,
    config_loader: Callable[[], GovernanceConfig],
    writers: _RecordingWriters | None = None,
) -> tuple[CircuitBreaker, _RecordingWriters]:
    """构造熔断器（注入 config 加载器与假审计写入器）。"""
    recorder = writers if writers is not None else _RecordingWriters()
    breaker = CircuitBreaker(
        config_loader=config_loader,
        event_audit_writer=recorder.write_event,
        rejudge_audit_writer=recorder.write_rejudge,
        clock=lambda: _NOW,
    )
    return breaker, recorder


@pytest.fixture
def clean_global_breaker() -> Iterator[None]:
    """用例前后清空全局熔断开关（进程内单例，测试隔离）。"""
    circuit_breaker.set_global_breaker(None)
    yield
    circuit_breaker.set_global_breaker(None)


# ---------------------------------------------------------------------------
# 负向：熔断打开后不得有任何 send 发生（先红）
# ---------------------------------------------------------------------------


def test_open_breaker_blocks_dispatch_and_queues_pending_candidate() -> None:
    """负向（先红）：熔断打开 → 派发被拒 + 候选进入挂起队列（架构 v5.2:614）。"""
    config = _config_with(version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())

    plan = breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=_context(),
    )

    assert plan.allowed is False, "熔断打开后不得放行派发"
    assert [item.candidate_id for item in plan.pending] == ["CAND-1"], "候选必须进挂起队列"
    assert breaker.pending_candidate_ids == ("CAND-1",)
    assert breaker.is_open() is True


def test_send_node_emits_nothing_while_breaker_is_open(
    clean_global_breaker: None,
) -> None:
    """负向（先红）：熔断打开时 ``send`` 节点**零发送**——mock 发送列表必须为空。

    证明方式：把全局开关置于打开态，走真实执行图（``run_dry_run``）——
    若 send 仍发生，本用例必红（架构 v5.2:614「停止派发」）。
    """
    config = _config_with(version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    circuit_breaker.set_global_breaker(breaker)

    final = workflow.run_dry_run()

    assert final.get("sent", []) == [], "熔断打开后不得有任何 send 发生"
    assert final.get("dispatch_halted") is True
    assert final.get("pending_candidate_ids", []) == ["CAND-1"], "候选必须进挂起队列"
    assert breaker.pending_candidate_ids == ("CAND-1",)


def test_trip_records_measured_elapsed_time_and_stop_latency() -> None:
    """实测耗时须记录（架构 v5.2:614）：触发耗时入审计，派发停止延迟可观测。"""
    config = _config_with(version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, recorder = _make_breaker(config_loader=lambda: config)

    event = breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1"], context=_context())

    assert event.elapsed_ms >= 0.0, "熔断触发耗时必须被实测并记录"
    assert breaker.dispatch_stop_elapsed_ms is not None, "触发→停止派发的实测耗时必须被记录"
    assert recorder.events, "熔断触发必须写审计"
    assert float(recorder.events[0].evidence["elapsed_ms"]) >= 0.0


# ---------------------------------------------------------------------------
# 核心红线：恢复 = 以当前 rule_version 重新判定（ADR-030）
# ---------------------------------------------------------------------------


def test_resume_rejudges_pending_with_current_rule_version() -> None:
    """ADR-030（架构 v5.2:614 / PRD FR-28）：恢复后挂起动作以**当前**版本重判。

    证明链（任一条不成立即红）：
    - 挂起时策略 = 宽松频控（2 次）→ 候选本可放行；
    - 恢复前把**当前**策略换成收紧频控（1 次）+ 新 version；
    - 恢复后重判结论：该候选命中全局频控被拒（不再是放行名单成员）；
    - 重判审计的 ``rule_version`` = **当前**版本（非挂起快照）。
    """
    current: dict[str, GovernanceConfig] = {
        "config": _config_with(
            version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES
        )
    }
    breaker, recorder = _make_breaker(config_loader=lambda: current["config"])

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1"], context=_context())

    # 恢复前换当前策略：收紧频控 + 换版本号
    current["config"] = _config_with(
        version=_CURRENT_RULE_VERSION, freq_max_touches=_TIGHT_FREQ_MAX_TOUCHES
    )

    outcome = breaker.reset(reason="operator-resume", actor="operator")

    assert outcome.rule_version == _CURRENT_RULE_VERSION, "恢复必须使用当前策略版本"
    assert outcome.sendable_candidate_ids == (), "按新规则重判后候选被拒，不得放行"
    assert [item.candidate_id for item in outcome.denied] == ["CAND-1"]
    assert outcome.denied[0].rule_id == "FREQ_GLOBAL_7D", "重判拒绝理由必须来自既有规则"
    assert recorder.rejudges, "恢复重判结论必须写审计"
    assert recorder.rejudges[0].rule_version == _CURRENT_RULE_VERSION, (
        "重判审计的 rule_version 必须是恢复时的当前版本（ADR-030）"
    )
    assert breaker.is_open() is False, "恢复后熔断开关关闭"
    assert breaker.pending_candidate_ids == (), "重判后挂起队列清空"


def test_old_policy_would_have_allowed_candidate_so_rejudge_is_load_bearing() -> None:
    """对照证明：若沿用**挂起时**的旧判定，候选会被放行——所以重判必须是真重判。

    本用例用既有重判入口跑一遍旧策略，证明「旧判定 = 放行」；
    若熔断模块偷懒沿用旧判定，``test_resume_rejudges_pending_with_current_rule_version``
    就会红（放行 ≠ 被拒）。
    """
    old_config = _config_with(
        version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES
    )
    context = _context()

    outcome = gate.rejudge_with_current_config(
        pending_candidate_ids=["CAND-1"],
        customer_rows=[dict(row) for row in context.customer_rows],
        candidate_rows=[dict(row) for row in context.candidate_rows],
        tool_call_rows=[dict(row) for row in context.tool_call_rows],
        suppression_rows=[dict(row) for row in context.suppression_rows],
        now=_NOW,
        config=old_config,
    )

    assert outcome.sendable == ("CAND-1",), "旧判定下候选放行——重判因此可被证伪"


def test_resume_audit_records_trip_and_resume_events() -> None:
    """触发与恢复都写审计（红线③ + 架构 v5.2:614）。"""
    config = _config_with(version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, recorder = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1"], context=_context())
    breaker.reset(reason="operator-resume", actor="operator")

    action_types = [event.action_type for event in recorder.events]
    assert ACTION_CIRCUIT_TRIP in action_types, "熔断触发必须写审计"
    assert ACTION_CIRCUIT_RESUME in action_types, "熔断恢复必须写审计"
    trip_event = next(e for e in recorder.events if e.action_type == ACTION_CIRCUIT_TRIP)
    assert trip_event.evidence["reason"] == "manual-stop"
    assert trip_event.evidence["actor"] == "operator"


def test_autonomy_downgrade_rejudges_pending_through_same_entry() -> None:
    """裁决 #6 / 架构 v5.2:238：自主度降级同样触发排队动作重判，且**复用同一入口**。

    降级路径与熔断恢复走同一个重判入口（``gate.rejudge_with_current_config``），
    不另写判定；本用例证明降级后挂起动作同样按**当前**版本重判。
    """
    current: dict[str, GovernanceConfig] = {
        "config": _config_with(
            version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES
        )
    }
    breaker, recorder = _make_breaker(config_loader=lambda: current["config"])

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1"], context=_context())
    current["config"] = _config_with(
        version=_CURRENT_RULE_VERSION, freq_max_touches=_TIGHT_FREQ_MAX_TOUCHES
    )

    outcome = breaker.apply_autonomy_downgrade(reason="autonomy-downgraded", actor="system")

    assert outcome.rule_version == _CURRENT_RULE_VERSION
    assert outcome.sendable_candidate_ids == (), "降级后按当前版本重判，被拒候选不得放行"
    assert recorder.rejudges, "降级重判结论必须写审计"
    assert recorder.rejudges[0].rule_version == _CURRENT_RULE_VERSION


def test_global_breaker_is_a_single_shared_switch() -> None:
    """全局开关（架构 v5.2:614）：所有数字员工共用同一个熔断实例。"""
    circuit_breaker.set_global_breaker(None)
    first = circuit_breaker.get_global_breaker()
    second = circuit_breaker.get_global_breaker()

    assert first is second, "熔断开关必须是全局单例"
    circuit_breaker.set_global_breaker(None)


# ---------------------------------------------------------------------------
# 回归：多候选排队恢复（复核阻断缺陷——行复制 N 倍 / 候选静默消失）
# ---------------------------------------------------------------------------


def test_resume_of_two_pending_candidates_does_not_duplicate_input_rows() -> None:
    """回归（阻断缺陷）：同批两个候选排队恢复 → 判定输入**不得按候选数复制**。

    缺陷形态：``before_dispatch`` 给同批的每个候选存**同一份批次级上下文**，
    恢复装配时按「每个候选拼一次行」→ 工具调用行翻倍 → 全局频控计数翻倍 →
    越过当前频控被**误拒**（``FREQ_GLOBAL_7D``）；候选行翻倍 → 同一候选出现两次
    → 冲突三键全平 → 候选**既不放行也不留痕（静默消失）**。

    本用例锁定：一次触达 + 当前宽松频控（上限 2）→ 两个候选**都放行**。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1", "CAND-2"], context=_two_candidate_context())

    outcome = breaker.reset(reason="operator-resume", actor="operator")

    assert outcome.sendable_candidate_ids == ("CAND-1", "CAND-2"), (
        "同批两候选必须都放行——判定输入不得因候选数量而翻倍"
    )
    assert outcome.denied == (), "宽松频控下不得误拒（工具调用行翻倍会越过频控）"
    assert outcome.undecided_candidate_ids == (), "两候选都应有明确结论"
    assert outcome.rejudged_candidate_count == 2


def test_resume_gives_every_pending_candidate_an_explicit_conclusion() -> None:
    """回归（阻断缺陷）：多候选排队恢复 → **每个候选都有明确结论**，不得静默消失。

    结论集合 = 放行 ∪ 拒绝 ∪ 显式未决（三键全平升级人工）；三者并集必须覆盖
    本次重判覆盖的全部候选——任何候选都不许「既不可发也不入拒绝」。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1", "CAND-2"], context=_two_candidate_context())

    outcome = breaker.reset(reason="operator-resume", actor="operator")

    concluded = {
        *outcome.sendable_candidate_ids,
        *(item.candidate_id for item in outcome.denied),
        *outcome.undecided_candidate_ids,
    }
    assert concluded == {"CAND-1", "CAND-2"}, "每个候选都必须有明确结论，不得静默消失"
    assert outcome.rejudged_candidate_count == 2


def test_resume_of_two_pending_candidates_applies_current_frequency_limit() -> None:
    """回归：两候选排队 → 恢复按**当前**频控判定 → 两个候选都被拒且**留痕**。

    收紧频控（上限 1）后，一次触达即越限 → 两个候选都命中 ``FREQ_GLOBAL_7D``；
    结论必须齐全（不是「一个放行一个消失」）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_TIGHT_FREQ_MAX_TOUCHES)
    breaker, recorder = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1", "CAND-2"], context=_two_candidate_context())

    outcome = breaker.reset(reason="operator-resume", actor="operator")

    assert outcome.sendable_candidate_ids == (), "收紧频控后不得放行"
    assert sorted(item.candidate_id for item in outcome.denied) == ["CAND-1", "CAND-2"], (
        "两个候选都必须留痕（拒绝集合），不得静默消失"
    )
    assert {item.rule_id for item in outcome.denied} == {"FREQ_GLOBAL_7D"}
    assert outcome.undecided_candidate_ids == ()
    assert recorder.rejudges, "重判结论必须写审计"
    assert recorder.rejudges[0].pending_candidate_ids == ("CAND-1", "CAND-2")


def test_resume_and_dispatch_actually_sends_the_rejudged_candidates(
    clean_global_breaker: None,
) -> None:
    """回归（次要③）：恢复后**可发候选确实被派发**（重判 → 派发闭环）。

    流程：熔断打开 → 真实执行图零发送（候选进挂起队列）→ 恢复（重判放行）→
    放行集合交回派发路径 → 候选被派发出去。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    circuit_breaker.set_global_breaker(breaker)

    initial = workflow.dry_run_initial_state()
    state: dict[str, object] = {
        **initial,
        "customer_rows": [_customer_row("CUST-1"), _customer_row("CUST-2")],
        "candidate_rows": [
            _candidate_row("CAND-1", "CUST-1"),
            _candidate_row("CAND-2", "CUST-2"),
        ],
    }
    halted = workflow.run_dry_run(state)  # type: ignore[arg-type]

    assert halted.get("sent", []) == [], "熔断打开时零发送"
    assert sorted(halted.get("pending_candidate_ids", [])) == ["CAND-1", "CAND-2"]

    resumed = workflow.resume_and_dispatch(halted, breaker=breaker)

    sent_ids = sorted(str(item["candidate_id"]) for item in resumed.get("sent", []))
    assert sent_ids == ["CAND-1", "CAND-2"], "恢复后放行候选必须真的被派发"
    assert resumed.get("dispatch_halted") is False
    assert breaker.pending_candidate_ids == (), "派发完成后队列清空"
    assert breaker.ready_candidate_ids == (), "派发完成后待派发集合清空"


def test_downgrade_while_breaker_open_keeps_the_queue_and_reports_sendable() -> None:
    """回归（次要③）：降级时熔断仍打开 → **不丢队列**，可发集合明确返回。

    降级按当前策略重判（裁决 #6）；此刻熔断仍打开 → 不可派发 → 候选留在挂起队列，
    但重判结论里的可发集合必须**明确返回**（不吞掉）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(candidate_ids=["CAND-1", "CAND-2"], context=_two_candidate_context())

    outcome = breaker.apply_autonomy_downgrade(reason="autonomy-downgraded", actor="system")

    assert outcome.sendable_candidate_ids == ("CAND-1", "CAND-2"), "降级必须明确返回可发集合"
    assert breaker.is_open() is True, "降级不关闭熔断开关"
    assert sorted(breaker.pending_candidate_ids) == ["CAND-1", "CAND-2"], (
        "熔断仍打开时不得丢队列（放行候选留待恢复时重判）"
    )


def test_downgrade_and_dispatch_keeps_queue_and_sends_nothing_while_open(
    clean_global_breaker: None,
) -> None:
    """回归（次要③）：降级也走同一派发路径——熔断仍打开 → **零派发 + 队列保留**。

    降级按当前策略重判（裁决 #6），但熔断仍打开 → 此刻不可派发 → 候选留在挂起
    队列（待熔断恢复时再判）；派发路径必须**零发送**（红线：熔断打开时零发生）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    circuit_breaker.set_global_breaker(breaker)

    initial = workflow.dry_run_initial_state()
    state: dict[str, object] = {
        **initial,
        "customer_rows": [_customer_row("CUST-1"), _customer_row("CUST-2")],
        "candidate_rows": [
            _candidate_row("CAND-1", "CUST-1"),
            _candidate_row("CAND-2", "CUST-2"),
        ],
    }
    halted = workflow.run_dry_run(state)  # type: ignore[arg-type]

    downgraded = workflow.downgrade_and_dispatch(halted, breaker=breaker)

    assert downgraded.get("sent", []) == [], "熔断仍打开时降级不得派发"
    assert downgraded.get("dispatch_halted") is True
    assert sorted(breaker.pending_candidate_ids) == ["CAND-1", "CAND-2"], (
        "降级不得丢队列（熔断仍打开，候选待恢复时重判）"
    )


def test_downgrade_then_resume_dispatches_the_queued_candidates(
    clean_global_breaker: None,
) -> None:
    """回归（次要③）：降级（熔断仍打开）→ 恢复 → 队列中的候选**最终被派发**。"""
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    circuit_breaker.set_global_breaker(breaker)

    initial = workflow.dry_run_initial_state()
    state: dict[str, object] = {
        **initial,
        "customer_rows": [_customer_row("CUST-1"), _customer_row("CUST-2")],
        "candidate_rows": [
            _candidate_row("CAND-1", "CUST-1"),
            _candidate_row("CAND-2", "CUST-2"),
        ],
    }
    halted = workflow.run_dry_run(state)  # type: ignore[arg-type]
    workflow.downgrade_and_dispatch(halted, breaker=breaker)

    resumed = workflow.resume_and_dispatch(halted, breaker=breaker)

    sent_ids = sorted(str(item["candidate_id"]) for item in resumed.get("sent", []))
    assert sent_ids == ["CAND-1", "CAND-2"], "降级后恢复，队列中的候选必须被派发"
    assert breaker.pending_candidate_ids == ()


def test_resume_event_carries_task_and_worker_ids() -> None:
    """回归（次要④）：恢复事件的 ``task_id`` / ``worker_id`` 透传挂起队列里的真实值。"""
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, recorder = _make_breaker(config_loader=lambda: config)
    task_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    breaker.trip(
        reason="manual-stop",
        actor="operator",
        tenant_id=uuid.uuid4(),
        task_id=task_id,
        worker_id=worker_id,
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=_context(),
        task_id=task_id,
        worker_id=worker_id,
    )
    breaker.reset(reason="operator-resume", actor="operator")

    resume_event = next(e for e in recorder.events if e.action_type == ACTION_CIRCUIT_RESUME)
    assert resume_event.task_id == task_id, "恢复事件必须透传 task_id"
    assert resume_event.worker_id == worker_id, "恢复事件必须透传 worker_id"


def test_trip_audit_failure_leaves_breaker_closed() -> None:
    """回归（次要④）：审计写失败 → 熔断态与审计**一致**（不得「已熔断但无审计」）。

    补偿语义：审计不可反查的熔断态比不熔断更危险 → 写审计失败即撤销熔断态并抛出。
    """

    class _BoomWriter:
        def __call__(self, record: AuditEvent) -> tuple[str, int, str]:
            raise RuntimeError("audit-db-down")

    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker = CircuitBreaker(
        config_loader=lambda: config,
        event_audit_writer=_BoomWriter(),
        rejudge_audit_writer=lambda record: ("audit-rejudge", 1, "0" * 64),
        clock=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="audit-db-down"):
        breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())

    assert breaker.is_open() is False, "审计写失败后不得停留在熔断态"
    assert breaker.trip_event is None, "审计写失败不得留下熔断事件"


# ---------------------------------------------------------------------------
# 第二轮阻断缺陷：去重过度 → 熔断恢复路径比正常判定路径**更宽松**（红线②）
# ---------------------------------------------------------------------------

#: 触达输入矩阵：``(called_at, tool_call_id 或 None)`` 序列。
#: ``tool_call_id=None`` = **无稳定 id**（兜底键合并的受害者）。
_TOUCH_CASES: dict[str, tuple[tuple[datetime, str | None], ...]] = {
    "zero": (),
    "one": ((_NOW - timedelta(days=1), None),),
    # 两条**独立**触达，(called_at, channel) 完全相同、无稳定 id —— 阻断缺陷现场。
    "two_same_no_id": (
        (_NOW - timedelta(days=1), None),
        (_NOW - timedelta(days=1), None),
    ),
    # 两条独立触达，稳定 id 相同（重复记录）——判定输入不做行级去重 → 计两次。
    "two_same_same_id": (
        (_NOW - timedelta(days=1), "TC-1"),
        (_NOW - timedelta(days=1), "TC-1"),
    ),
    # 两条独立触达，稳定 id 不同。
    "two_distinct_ids": (
        (_NOW - timedelta(days=1), "TC-1"),
        (_NOW - timedelta(days=1), "TC-2"),
    ),
    "two_distinct_times": (
        (_NOW - timedelta(days=1), None),
        (_NOW - timedelta(days=2), None),
    ),
    "three": (
        (_NOW - timedelta(days=1), None),
        (_NOW - timedelta(days=2), None),
        (_NOW - timedelta(days=3), None),
    ),
}

#: 各触达矩阵下的预期判定（当前频控上限 = 2 → 窗口内 ≥2 次即 DENY）。
_EXPECTED_DENIED: dict[str, bool] = {
    "zero": False,
    "one": False,
    "two_same_no_id": True,
    "two_same_same_id": True,
    "two_distinct_ids": True,
    "two_distinct_times": True,
    "three": True,
}


def _touch_rows(case: str) -> list[dict[str, Any]]:
    """按矩阵取触达行（无 id / 有 id 两种形态都覆盖）。"""
    return [
        _tool_call_row(called_at=moment, tool_call_id=tool_call_id)
        for moment, tool_call_id in _TOUCH_CASES[case]
    ]


def _normal_decision(touch_rows: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """走**正常判定路径**（``workflow._decision_node``，不做任何去重）。

    返回 ``(放行候选集合, 拒绝候选集合)``。
    """
    state: workflow.GraphState = {
        "now": _NOW,
        "customer_rows": [_customer_row()],
        "candidate_rows": [_candidate_row()],
        "tool_call_rows": [dict(row) for row in touch_rows],
        "suppression_rows": [],
        "visited_nodes": [],
    }
    decision = workflow._decision_node(state)
    return (
        set(decision.get("sendable_candidate_ids", [])),
        set(decision.get("denied_candidate_ids", [])),
    )


def _recovery_decision(touch_rows: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """走**熔断恢复路径**（排队 → 恢复重判），返回 ``(放行集合, 拒绝集合)``。"""
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=RejudgeContext(
            customer_rows=(_customer_row(),),
            candidate_rows=(_candidate_row(),),
            tool_call_rows=tuple(dict(row) for row in touch_rows),
            suppression_rows=(),
        ),
    )
    outcome = breaker.reset(reason="operator-resume", actor="operator")
    return (
        set(outcome.sendable_candidate_ids),
        {item.candidate_id for item in outcome.denied},
    )


def test_resume_must_not_merge_two_identical_touches_without_stable_id() -> None:
    """红线②（阻断缺陷）：两条 ``(called_at, channel)`` 相同、无 id 的触达 → 恢复后必须 DENY。

    缺陷形态：``_assemble_rows`` 的兜底键 ``(called_at, channel)`` 把两条**不同的
    独立触达**合并成一条 → 频控计数被低估 → 熔断恢复路径比正常判定路径**更宽松**
    （等于用熔断路径绕开 ``FREQ_GLOBAL_7D`` 的 DENY）。

    本用例锁定：当前频控上限 2 + 窗口内 2 次触达 → 恢复重判必须命中
    ``FREQ_GLOBAL_7D`` 并拒绝（与正常判定路径同结论）。
    """
    moment = _NOW - timedelta(days=1)
    touch_rows = [
        _tool_call_row(called_at=moment),
        _tool_call_row(called_at=moment),
    ]

    normal_sendable, normal_denied = _normal_decision(touch_rows)
    recovery_sendable, recovery_denied = _recovery_decision(touch_rows)

    assert normal_sendable == set(), "正常判定路径：窗口内 2 次触达 → 频控拒绝"
    assert normal_denied == {"CAND-1"}, "正常判定路径：拒绝必须留痕"
    assert recovery_sendable == normal_sendable, "熔断恢复路径不得比正常判定路径宽松（红线②）"
    assert recovery_denied == normal_denied, "熔断恢复路径的拒绝集合必须与正常路径一致"
    assert recovery_denied == {"CAND-1"}, "两条独立触达必须计入频控 → 恢复后必须 DENY"


@pytest.mark.parametrize("case", sorted(_TOUCH_CASES))
def test_circuit_recovery_conclusion_matches_normal_decision_path(case: str) -> None:
    """对照测试：同一批输入在「正常判定路径」与「熔断恢复路径」结论集合必须一致。

    覆盖 0 / 1 / 2 / 3 条触达 × 含 / 不含稳定 id——两种路径都必须给出**同一**结论
    （红线②：熔断恢复路径不得比正常判定路径宽松）。
    """
    touch_rows = _touch_rows(case)

    normal_sendable, normal_denied = _normal_decision(touch_rows)
    recovery_sendable, recovery_denied = _recovery_decision(touch_rows)

    assert recovery_sendable == normal_sendable, f"放行集合不一致（case={case}）"
    assert recovery_denied == normal_denied, f"拒绝集合不一致（case={case}）"
    assert bool(recovery_denied) is _EXPECTED_DENIED[case], (
        f"频控结论与预期不符（case={case}）：{recovery_denied}"
    )


def test_customer_rows_multi_row_uses_last_row_consistently_on_both_paths() -> None:
    """次要②：同一客户多行时，两条路径必须取**同一条**（口径 = 末条，与 gate 一致）。

    缺陷形态：``_assemble_rows`` 对 ``customer_rows`` 取**首条**，而
    ``src/graph/gate.py`` 的字典推导取**末条** → 同客户多行内容不一致时，
    两条路径可能给出不同判定。
    """
    benign = _customer_row()
    withdrawn = _customer_row(consent_status="withdrawn")
    customer_rows = [benign, withdrawn]

    state: workflow.GraphState = {
        "now": _NOW,
        "customer_rows": [dict(row) for row in customer_rows],
        "candidate_rows": [_candidate_row()],
        "tool_call_rows": [],
        "suppression_rows": [],
        "visited_nodes": [],
    }
    decision = workflow._decision_node(state)
    normal_sendable = set(decision.get("sendable_candidate_ids", []))
    normal_denied = set(decision.get("denied_candidate_ids", []))

    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=uuid.uuid4())
    breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=RejudgeContext(
            customer_rows=tuple(dict(row) for row in customer_rows),
            candidate_rows=(_candidate_row(),),
            tool_call_rows=(),
            suppression_rows=(),
        ),
    )
    outcome = breaker.reset(reason="operator-resume", actor="operator")

    recovery_sendable = set(outcome.sendable_candidate_ids)
    recovery_denied = {item.candidate_id for item in outcome.denied}

    assert normal_sendable == set(), "正常判定路径取末条（无授权）→ 拒绝"
    assert normal_denied == {"CAND-1"}
    assert recovery_sendable == normal_sendable, "客户多行口径不一致（熔断恢复 vs 正常判定）"
    assert recovery_denied == normal_denied, "客户多行口径不一致（熔断恢复 vs 正常判定）"


def test_pending_cleanup_does_not_cross_trace_groups() -> None:
    """次要③：清理按 ``(trace, candidate_id)`` 组合，不得跨组连带清掉。

    缺陷形态：``denied_ids_all`` 按 candidate_id 清理 ``_pending``——同一
    candidate_id 出现在不同 trace 组时，一组被拒会连带把另一组的候选清出队列
    （候选**静默消失**）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant)

    # 组 A：客户无有效营销授权 → NO_CONSENT → 被拒。
    breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=RejudgeContext(
            customer_rows=(_customer_row(consent_status="withdrawn"),),
            candidate_rows=(_candidate_row(),),
            tool_call_rows=(),
            suppression_rows=(),
        ),
        tenant_id=tenant,
        trace_id="trace-A",
    )
    # 组 B：同 candidate_id、不同 trace、客户有授权 → 放行。
    breaker.before_dispatch(
        candidate_ids=["CAND-1"],
        context=RejudgeContext(
            customer_rows=(_customer_row(),),
            candidate_rows=(_candidate_row(),),
            tool_call_rows=(),
            suppression_rows=(),
        ),
        tenant_id=tenant,
        trace_id="trace-B",
    )

    outcome = breaker.apply_autonomy_downgrade(reason="autonomy-downgraded", actor="system")

    assert [item.candidate_id for item in outcome.denied] == ["CAND-1"], "组 A 必须被拒"
    assert outcome.sendable_candidate_ids == ("CAND-1",), "组 B 必须放行"

    queued = {(item.trace_id, item.candidate_id) for item in breaker.pending_actions}
    assert ("trace-A", "CAND-1") not in queued, "被拒候选必须出队"
    assert ("trace-B", "CAND-1") in queued, (
        "另一 trace 组的同 id 候选不得被连带清出队列（候选不得静默消失）"
    )


# ---------------------------------------------------------------------------
# 第三轮阻断缺陷：跨租户 / 跨任务组串味
#   ① 派发结算缺 tenant 维度 → 跨租户误清理，候选**静默消失**
#   ② 恢复结论拍平成并集 → 跨任务 / 跨租户错发 + 审计行租户与任务错位
# ---------------------------------------------------------------------------


def _context_for(candidate_id: str, customer_id: str = "CUST-1") -> RejudgeContext:
    """指定候选的判定输入快照（一个候选一份上下文，便于按组隔离）。"""
    return RejudgeContext(
        customer_rows=(_customer_row(customer_id),),
        candidate_rows=(_candidate_row(candidate_id, customer_id),),
        tool_call_rows=(_tool_call_row(called_at=_NOW - timedelta(days=1)),),
        suppression_rows=(),
    )


def test_settle_dispatched_keeps_same_candidate_in_other_tenant() -> None:
    """阻断缺陷 A①：同名候选分属两个租户 → 派发其中一个**不得**清掉另一个。

    缺陷形态：``_settle_dispatched`` 的匹配键只有 ``(trace_id, candidate_id)``，
    **租户维度缺失** → 租户 A 派发 ``CAND-X`` 时，租户 B 同名的 ``CAND-X`` 被连带
    清出挂起队列 → 该候选**静默消失**（无 DENY 留痕、无审计，后续也上报不到）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant_a)

    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-A"),
        tenant_id=tenant_a,
        trace_id="trace-A",
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-B"),
        tenant_id=tenant_b,
        trace_id="trace-A",
    )
    assert len(breaker.pending_actions) == 2, "两个租户的同名候选必须各自入队"

    breaker.reset(reason="operator-resume", actor="operator")
    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-A"),
        tenant_id=tenant_a,
        trace_id="trace-A",
    )

    remaining_tenants = [action.tenant_id for action in breaker.pending_actions]
    assert remaining_tenants == [tenant_b], "派发租户 A 的候选不得连带清掉租户 B 的同名候选"


def test_settle_dispatched_keeps_same_candidate_in_other_trace() -> None:
    """阻断缺陷 A②：同租户、同候选 id、不同轨迹 → 派发一个不得影响另一个。

    缺陷形态：``_ready`` 是**扁平候选 id 列表**（跨租户 / 跨轨迹共享）→ 派发
    trace-A 的候选时按 candidate_id 把 trace-B 的待派发条目一并删掉（待派发集合
    被跨轨迹清空 = 候选静默消失）；且同名条目在两个组里**合并成一条**，其中一个
    组的放行结论被吞掉。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant)

    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-A"),
        tenant_id=tenant,
        trace_id="trace-A",
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-B"),
        tenant_id=tenant,
        trace_id="trace-B",
    )

    breaker.reset(reason="operator-resume", actor="operator")
    assert len(breaker.ready_entries) == 2, "两个轨迹组的待派发条目必须各自独立"

    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-A"),
        tenant_id=tenant,
        trace_id="trace-A",
    )

    assert len(breaker.pending_actions) == 1, "另一轨迹组的候选不得被连带清出队列"
    assert breaker.pending_actions[0].trace_id == "trace-B"
    assert (tenant, "trace-B", "CAND-X") in breaker.ready_entries, (
        "另一轨迹组的待派发条目不得被连带清掉（候选不得静默消失）"
    )


def test_ready_set_is_scoped_by_tenant_and_trace() -> None:
    """阻断缺陷 A③：待派发集合必须按 ``(租户, 轨迹, 候选)`` 分组，不得跨组串扰。

    缺陷形态：``_ready`` 是扁平候选 id 列表 → 两个租户的同名候选在待派发集合里
    **合并成一条**（其中一个租户 / 轨迹的放行结论被吞掉 → 该组候选再也派不出去）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant_a)

    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-A"),
        tenant_id=tenant_a,
        trace_id="trace-A",
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-X"],
        context=_context_for("CAND-X", "CUST-B"),
        tenant_id=tenant_b,
        trace_id="trace-B",
    )

    breaker.reset(reason="operator-resume", actor="operator")

    entries = breaker.ready_entries
    assert len(entries) == 2, "两个租户的同名候选必须各有各的待派发条目"
    assert {entry[0] for entry in entries} == {tenant_a, tenant_b}
    assert {entry[1] for entry in entries} == {"trace-A", "trace-B"}


def test_reset_does_not_mix_candidates_across_tenants() -> None:
    """阻断缺陷 B①：恢复结论必须**按组**给出，不得拍平成跨租户并集。

    缺陷形态：``reset()`` 把各组放行集合 ``extend`` 成一个并集 → 「恢复任务 B」
    会把「任务 A（另一租户）」的候选一起派发出去（跨任务 / 跨租户错发）。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant_a)

    breaker.before_dispatch(
        candidate_ids=["CAND-A"],
        context=_context_for("CAND-A", "CUST-A"),
        tenant_id=tenant_a,
        trace_id="trace-A",
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-B"],
        context=_context_for("CAND-B", "CUST-B"),
        tenant_id=tenant_b,
        trace_id="trace-B",
    )

    outcome = breaker.reset(reason="operator-resume", actor="operator")

    assert len(outcome.groups) == 2, "恢复结论必须按 (租户, 轨迹) 分组保留"
    group_a = outcome.group_for(tenant_id=tenant_a, trace_id="trace-A")
    group_b = outcome.group_for(tenant_id=tenant_b, trace_id="trace-B")
    assert group_a is not None, "A 组结论必须可按 (租户, 轨迹) 取到"
    assert group_b is not None, "B 组结论必须可按 (租户, 轨迹) 取到"
    assert group_a.sendable_candidate_ids == ("CAND-A",), "A 组只含 A 组的候选"
    assert group_b.sendable_candidate_ids == ("CAND-B",), "B 组只含 B 组的候选"
    # 扁平字段是**只读聚合视图**（仅供展示，不可用于派发）。
    assert set(outcome.sendable_candidate_ids) == {"CAND-A", "CAND-B"}


def test_reset_writes_one_resume_event_per_group() -> None:
    """阻断缺陷 B②：恢复收尾事件**按组各写一条**，字段全部取自本组。

    缺陷形态：只写一条合并事件 → ``tenant_id`` / ``trace_id`` 取首个组，
    ``task_id`` / ``worker_id`` 却取自**全部组** → 一条审计行里租户与任务错位
    （审计反查不到正确的租户与任务）；其余组更是没有任何恢复事件。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, recorder = _make_breaker(config_loader=lambda: config)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    task_a = uuid.uuid4()
    task_b = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant_a)

    breaker.before_dispatch(
        candidate_ids=["CAND-A"],
        context=_context_for("CAND-A", "CUST-A"),
        tenant_id=tenant_a,
        trace_id="trace-A",
        task_id=task_a,
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-B"],
        context=_context_for("CAND-B", "CUST-B"),
        tenant_id=tenant_b,
        trace_id="trace-B",
        task_id=task_b,
    )

    breaker.reset(reason="operator-resume", actor="operator")

    resume_events = [e for e in recorder.events if e.action_type == ACTION_CIRCUIT_RESUME]
    assert len(resume_events) == 2, "每个分组必须各写一条恢复事件"
    by_trace = {event.trace_id: event for event in resume_events}
    assert by_trace["trace-A"].tenant_id == tenant_a
    assert by_trace["trace-A"].task_id == task_a
    assert by_trace["trace-B"].tenant_id == tenant_b
    assert by_trace["trace-B"].task_id == task_b


def test_dispatch_rejudged_only_dispatches_current_task_group() -> None:
    """阻断缺陷 B③：恢复任务 B 只能派发 **B 自己**的候选；A 的候选留在队列。

    缺陷形态：``_dispatch_rejudged`` 直接取 ``outcome.sendable_candidate_ids``
    （跨组并集）→ 任务 B 恢复时把任务 A 的候选在 B 的任务 / 租户下派发出去。
    """
    config = _config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES)
    breaker, _ = _make_breaker(config_loader=lambda: config)
    tenant = uuid.uuid4()
    task_a = uuid.uuid4()
    task_b = uuid.uuid4()
    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant)

    breaker.before_dispatch(
        candidate_ids=["CAND-A"],
        context=_context_for("CAND-A", "CUST-A"),
        tenant_id=tenant,
        trace_id=f"task-{task_a}",
        task_id=task_a,
    )
    breaker.before_dispatch(
        candidate_ids=["CAND-B"],
        context=_context_for("CAND-B", "CUST-B"),
        tenant_id=tenant,
        trace_id=f"task-{task_b}",
        task_id=task_b,
    )

    state: workflow.GraphState = {
        "task_id": str(task_b),
        "worker_id": "1",
        "tenant_id": str(tenant),
        "now": _NOW,
        "customer_rows": [_customer_row("CUST-B")],
        "candidate_rows": [_candidate_row("CAND-B", "CUST-B")],
        "tool_call_rows": [],
        "suppression_rows": [],
        "visited_nodes": list(workflow.PIPELINE_NODES),
    }

    resumed = workflow.resume_and_dispatch(state, breaker=breaker)

    sent_ids = [str(item["candidate_id"]) for item in resumed.get("sent", [])]
    assert sent_ids == ["CAND-B"], "恢复任务 B 只能派发 B 自己的候选"
    assert "CAND-A" in breaker.pending_candidate_ids, (
        "任务 A 的候选必须留在熔断挂起队列（不是丢弃）"
    )
    assert resumed.get("deferred_candidate_ids", []) == ["CAND-A"], (
        "其它组的放行候选必须显式上报（不得静默消失）"
    )


# ---------------------------------------------------------------------------
# 分层守卫：graph 层零 DB 依赖 + 审计链尾口径只有一份
# ---------------------------------------------------------------------------


def test_importing_graph_workflow_does_not_load_database_driver() -> None:
    """graph 层零 DB 依赖：``import src.graph.workflow`` 不得加载 psycopg。

    ``src/approval/audit.py`` 曾模块级 ``import psycopg``，而 graph → approval.audit
    是 import 链上的必经段 → dry-run 链路被迫加载 DB 驱动（与「src/graph 零 DB
    依赖」自相矛盾）。本用例在**独立进程**里验证（进程内 sys.modules 已被其他用例污染）。
    """
    code = "import sys; import src.graph.workflow; print('psycopg' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(_REPO_ROOT),
        check=False,
    )
    assert completed.returncode == 0, f"探测进程失败：{completed.stderr}"
    assert completed.stdout.strip() == "False", (
        "导入 src.graph.workflow 不得加载 psycopg（graph 层零 DB 依赖）"
    )


def test_approval_audit_has_no_module_level_database_driver_import() -> None:
    """``src/approval/audit.py`` 模块级不得 import psycopg（惰性导入）。"""
    source = Path(approval_audit.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.add(node.module)

    assert "psycopg" not in module_level_imports, "psycopg 只能在写库路径惰性 import"


def test_chain_tip_sql_has_a_single_implementation() -> None:
    """审计链尾口径只有一份（红线③）：**全仓**共用 ``src/audit/chain.py``。

    「字段规范化口径不一致 → 校验永远失败」的唯一解法是口径只有一份；多处各抄
    一份 SQL 即是漂移风险。断言：取链尾的 SQL 在 ``src/`` 源码里**只出现一次**，
    且每个写入方都从共享 helper 取链尾（而不是各自抄一份）。
    """
    from src.audit import chain as audit_chain

    assert callable(audit_chain.chain_tip)

    sql_occurrences = 0
    for module_path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        text = module_path.read_text(encoding="utf-8")
        sql_occurrences += text.count("select record_hash from audit_log")
        if module_path == _REPO_ROOT / "src" / "audit" / "chain.py":
            continue
        if "chain_tip" not in text:
            continue
        assert "from src.audit.chain import chain_tip" in text, (
            f"{module_path.name} 必须复用 src/audit/chain.py 的链尾 helper"
        )
    assert sql_occurrences == 1, "取链尾的 SQL 必须只有一份实现"

    # 每个写入模块都真的调用共享 helper（而不是各自抄一份）。
    for module in (audit_events, approval_audit):
        module_file = module.__file__
        assert module_file is not None
        assert "chain_tip(conn" in Path(module_file).read_text(encoding="utf-8")

    # 越权审计（RBAC）与供应商适配器写入器同样复用共享口径。
    for relative in ("src/auth/rbac.py", "src/adapters/mock_adapter.py"):
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "from src.audit.chain import chain_tip" in text, (
            f"{relative} 必须复用 src/audit/chain.py 的链尾 helper"
        )
        assert "chain_tip(" in text, f"{relative} 必须调用共享的 chain_tip"


# ---------------------------------------------------------------------------
# 源码级断言：熔断模块内没有第二份判定 / 路由 / 抑制逻辑
# ---------------------------------------------------------------------------


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """收集模块/类/函数 docstring 的常量节点 id（docstring 不算判定字面量）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_circuit_breaker_module_has_no_second_decision_logic() -> None:
    """熔断模块只允许编排既有实现（唯一实现点）。

    源码级断言（prd 验收⑤）：
    - 只 import ``src.graph.gate`` 等既有模块，**不得** import 决策内核
      （``src.decision.*`` / ``src.policy_kernel.rules`` / ``src.policy_kernel.kernel``）；
    - 不得出现三档路由字面量（``L0``…``L3`` / ``low`` / ``medium`` / ``high``）；
    - 不得出现影响面比较口径（``unique_customer_count``）；
    - 不得出现结论字面量分支（``ALLOW`` / ``DENY`` / ``require_approval``）。
    """
    source = Path(circuit_breaker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_constant_ids(tree)

    imported: set[str] = set()
    imported_names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported_names.setdefault(node.module, set()).update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            for alias in node.names:
                imported_names.setdefault(alias.name, set()).add(alias.name.rsplit(".", 1)[-1])

    gate_imported = "src.graph.gate" in imported or (
        "gate" in imported_names.get("src.graph", set())
    )
    assert gate_imported, "重判必须复用 src/graph/gate.py 的既有入口"
    forbidden_modules = {
        "src.decision.resolver",
        "src.decision.suppression",
        "src.decision.impact",
        "src.decision.rate_limit",
        "src.policy_kernel.rules",
        "src.policy_kernel.kernel",
    }
    assert not (imported & forbidden_modules), (
        f"熔断模块不得直接 import 判定内核：{sorted(imported & forbidden_modules)}"
    )

    forbidden_literals = {
        "ALLOW",
        "DENY",
        "allow",
        "deny",
        "require_approval",
        "L0",
        "L1",
        "L2",
        "L3",
        "low",
        "medium",
        "high",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            assert node.value not in forbidden_literals, (
                f"熔断模块不得出现判定 / 路由字面量：{node.value!r}"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert "tier" not in node.id.lower(), "熔断模块不得出现三档路由标识"
            assert "unique_customer_count" not in node.id.lower(), "熔断模块不得出现影响面比较口径"
        if isinstance(node, ast.Attribute):
            assert "tier" not in node.attr.lower(), "熔断模块不得出现三档路由标识"
            assert "unique_customer_count" not in node.attr.lower(), (
                "熔断模块不得出现影响面比较口径"
            )

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "rejudge_with_current_config" in called, "必须调用 gate 的既有重判入口"


def test_circuit_breaker_module_does_not_import_database_driver_at_module_level() -> None:
    """graph 层零 DB 依赖（dry-run 链路无库）：psycopg 只允许惰性 import。"""
    source = Path(circuit_breaker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.add(node.module)

    assert "psycopg" not in module_level_imports, "psycopg 只能在写审计路径惰性 import"


# ---------------------------------------------------------------------------
# 真 PG：熔断触发 / 恢复重判写 audit_log（红线③）
# ---------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` + `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        yield connection


@pytest.mark.pg
def test_circuit_breaker_events_and_rejudge_are_written_to_audit_log(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """红线③（真 PG）：熔断触发与恢复重判写 ``audit_log``，哈希链完整。

    - 写入唯一走 ``insert_statement_sql()``（``record_hash`` 由 DB 算）；
    - 恢复重判的 ``rule_version`` = 恢复时**当前**版本（非挂起快照）；
    - 租户链经 ``verify_chain`` 校验完整。
    """
    tenant_id = uuid.uuid4()
    current: dict[str, GovernanceConfig] = {
        "config": _config_with(
            version=_SUSPEND_RULE_VERSION, freq_max_touches=_BASE_FREQ_MAX_TOUCHES
        )
    }
    breaker = CircuitBreaker(
        config_loader=lambda: current["config"],
        event_audit_writer=partial(write_audit_event, conn=conn),
        rejudge_audit_writer=partial(write_rejudge_audit, conn=conn),
        clock=lambda: _NOW,
    )

    breaker.trip(reason="manual-stop", actor="operator", tenant_id=tenant_id)
    breaker.before_dispatch(candidate_ids=["CAND-1"], context=_context(), tenant_id=tenant_id)
    current["config"] = _config_with(
        version=_CURRENT_RULE_VERSION, freq_max_touches=_TIGHT_FREQ_MAX_TOUCHES
    )
    outcome = breaker.reset(reason="operator-resume", actor="operator")

    assert outcome.sendable_candidate_ids == ()

    with conn.cursor() as cur:
        cur.execute(
            "select action_type, rule_version from audit_log "
            "where tenant_id = %s order by record_seq",
            (tenant_id,),
        )
        rows = cur.fetchall()

    by_action = {str(row[0]): str(row[1]) for row in rows}
    assert ACTION_CIRCUIT_TRIP in by_action, "熔断触发必须落 audit_log"
    assert ACTION_CIRCUIT_RESUME in by_action, "熔断恢复必须落 audit_log"
    assert "gate_rejudge" in by_action, "恢复重判结论必须落 audit_log"
    assert by_action["gate_rejudge"] == _CURRENT_RULE_VERSION, (
        "重判审计 rule_version 必须是恢复时的当前版本"
    )
    assert by_action[ACTION_CIRCUIT_RESUME] == _CURRENT_RULE_VERSION

    with conn.cursor() as cur:
        cur.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        chain_rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    result = verify_chain(chain_rows)
    assert result.ok, f"审计哈希链必须完整：{result.first_break}"
