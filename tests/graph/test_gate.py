"""T-E2 治理闸门验收用例：DENY 终止 / interrupt 挂起 / 恢复重判 / 审计。

权威依据：

- **红线②**：DENY 对所有角色（含管理员）不可绕过——DENY 候选混入 sendable 白名单进 gate
  即拒绝终止；错误必须携带 DENY 理由与 rule_id（非裸 Exception）。
- **ADR-030**（架构 v5.2:238）：恢复 = 挂起动作以**当前**策略版本（rule_version）重新判定，
  **不沿用挂起时快照**；重判结论写审计。
- **架构 v5.2:612 / 裁决 #8**：REQUIRE_APPROVAL → LangGraph ``interrupt()`` 挂起 →
  人工 → ``Command(resume=...)`` 恢复；任务级批量审批 = 一次 resume 覆盖该批全部候选。
- **E9 / 裁决 #25**：审批 48h 超时 = 过期，**不默认通过**——超时后 resume 不放行。
  **时间源信任模型**：过期判定只认**服务端墙钟**（挂起时刻 + 恢复时刻）；
  客户端自带的 ``resumed_at`` **不受信任**，仅作参考记录，不得参与判定
  （客户端可回填过去时刻 / 伪造未来时刻绕过超时）。
- **红线③**：审计 append-only + 哈希链；写审计唯一入口 = ``insert_statement_sql()``，
  ``record_hash`` 由 DB 算。
- **ADR-035**：confirm_plan 位置与必经语义不动（由 T-E1 回归用例保障）。

用例标记：纯函数/内存图用例标 ``unit``；真 PG 审计用例标 ``pg``（默认 ``-m "not pg"`` 跳过）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.approval.audit import RejudgeAudit, write_rejudge_audit
from src.audit.hash_chain import verify_chain, verify_query_sql
from src.graph import gate, workflow
from src.graph.gate import DenyVetoError
from src.policy_kernel.config import GovernanceConfig, load_and_validate

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 挂起时的 config 版本（对照 ADR-030：resume 后必须用新版本，不得沿用此快照）。
_SUSPEND_RULE_VERSION = "2026.10.26-suspend-snapshot"
#: resume 前替换进来的**当前** config 版本（测试中途修改 version 字段，prd 验收②）。
_CURRENT_RULE_VERSION = "2026.10.26-current-rejudge"

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

#: E9 / 裁决 #25：审批超时 48h（取 governance.yaml 种子值）。
_TIMEOUT_HOURS = 48

#: 服务端墙钟的基准读数（与 state["now"] 刻意不同——用于验证挂起时刻取的是墙钟）。
_WALL_CLOCK = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)


class _FakeClock:
    """可注入的服务端墙钟（测试缝）：直接推进时间，**不依赖真实 sleep**。

    E9 的过期判定只认服务端墙钟；测试通过 monkeypatch ``gate._utc_now``
    把「真实经过的时间」变成确定性的读数。
    """

    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance_hours(self, hours: float) -> None:
        """把服务端墙钟往前拨（等价于真实经过 N 小时）。"""
        self.current = self.current + timedelta(hours=hours)


def _base_config() -> GovernanceConfig:
    """仓库根 governance.yaml 的基准 config（策略参数唯一来源）。"""
    return load_and_validate(_REPO_ROOT / "governance.yaml")


def _customer_row(customer_id: str = "CUST-1", **overrides: object) -> dict[str, object]:
    """一条可放行的客户行（无投诉 / 有授权 / 非 Top1%）。"""
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


def _candidate_row(candidate_id: str, customer_id: str = "CUST-1") -> dict[str, object]:
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


def _tool_call_row(*, called_at: datetime, channel: str = "sms") -> dict[str, object]:
    """一条历史触达记录（频控计数输入）。"""
    return {"called_at": called_at, "channel": channel}


def _approval_state(**overrides: object) -> dict[str, object]:
    """正式模式（非 dry-run）待审批态：uuid 化的 task/worker/tenant（审计列要求）。"""
    state: dict[str, object] = dict(workflow.dry_run_initial_state())
    state.update(
        {
            "dry_run": False,
            "plan_confirmed": True,
            "task_id": str(uuid.uuid4()),
            "worker_id": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "now": _NOW,
            "customer_rows": [_customer_row()],
            "candidate_rows": [_candidate_row("CAND-1")],
            # 基准 config（freq_max_touches=2）下 1 次窗口内触达 → 放行；
            # 历史触达走 email 通道，避开 sms 的 72h 同通道冷却（附录D 规则 2）
            "tool_call_rows": [_tool_call_row(called_at=_NOW - timedelta(days=1), channel="email")],
            "suppression_rows": [],
            # 直调 run_gate 的用例依赖此白名单；走全图时 decision 节点会重算覆盖
            "sendable_candidate_ids": ["CAND-1"],
        }
    )
    state.update(overrides)
    return state


#: 直调 run_gate 的用例专用：模拟「已按正常路径到达 gate」的轨迹。
#: 红线④（T-E4 接线后）：缺必经节点的直调状态会被路径断言 DENY——
#: 负向演示见 tests/graph/test_path_assert_e2e.py。
_GATE_ARRIVAL_VISITED = ["segment", "decision", "confirm_plan"]


def _thread_config() -> RunnableConfig:
    """LangGraph checkpointer 线程配置（内存 checkpointer，prd 允许）。"""
    return {"configurable": {"thread_id": f"t-e2-{uuid.uuid4()}"}}


def _record_audit_call(
    audit_calls: list[RejudgeAudit], record: RejudgeAudit
) -> tuple[str, int, str]:
    """monkeypatch 用假审计写入：记录载荷并返回 (audit_id, version, record_hash)。"""
    audit_calls.append(record)
    return ("audit-1", 1, "0" * 64)


def _current_config_with(
    *,
    version: str,
    freq_max_touches: int,
) -> GovernanceConfig:
    """构造"当前" config：改 version 字段 + 频控阈值（证明重判吃的是新对象而非挂起快照）。"""
    base = _base_config()
    return replace(
        base,
        version=version,
        thresholds=replace(base.thresholds, freq_max_touches=freq_max_touches),
    )


# ---------------------------------------------------------------------------
# 红线②：DENY 进 gate 拒绝终止（负向先红）
# ---------------------------------------------------------------------------


def test_deny_candidate_in_sendable_is_vetoed_with_rule_id_and_reason() -> None:
    """红线②负向（先红）：DENY 候选混入 sendable 白名单进 gate → 拒绝终止。

    错误必须携带 DENY 理由与 rule_id（非裸 Exception）——任何角色不可绕过，
    即使上游把 DENY 候选塞进放行名单，闸门也要二次拦截。
    """
    denied_detail = {
        "candidate_id": "CAND-D",
        "rule_id": "SUP_EXPLICIT_REFUSAL",
        "reason": "命中抑制红线：明确拒绝/勿扰标记",
    }
    state = _approval_state(
        sendable_candidate_ids=["CAND-D"],
        denied_candidate_ids=["CAND-D"],
        suppressed_rule_ids=["SUP_EXPLICIT_REFUSAL"],
        denied_details=[denied_detail],
        visited_nodes=list(_GATE_ARRIVAL_VISITED),
    )

    with pytest.raises(DenyVetoError) as exc_info:
        gate.run_gate(state)

    error = exc_info.value
    assert error.rule_id == "SUP_EXPLICIT_REFUSAL", "拒绝错误必须携带 rule_id"
    assert error.candidate_id == "CAND-D"
    message = str(error)
    assert "SUP_EXPLICIT_REFUSAL" in message, "错误消息必须含 DENY rule_id"
    assert "明确拒绝" in message, "错误消息必须含 DENY 理由"


def test_deny_veto_fires_even_in_dry_run() -> None:
    """红线②不可 opt-in：dry-run 也不放行 DENY 混入 sendable 的状态。"""
    state = _approval_state(
        dry_run=True,
        sendable_candidate_ids=["CAND-D"],
        denied_details=[
            {
                "candidate_id": "CAND-D",
                "rule_id": "NO_CONSENT",
                "reason": "无营销授权（附录D 规则 1b）",
            }
        ],
        visited_nodes=list(_GATE_ARRIVAL_VISITED),
    )

    with pytest.raises(DenyVetoError, match="NO_CONSENT"):
        gate.run_gate(state)


def test_dry_run_gate_passes_without_interrupt() -> None:
    """dry-run 下闸门直通（与 confirm_plan auto-confirm 对齐），不触发 interrupt、不写审计。"""
    state = _approval_state(dry_run=True, visited_nodes=list(_GATE_ARRIVAL_VISITED))

    update = gate.run_gate(state)

    assert update["gate_outcome"] == "pass"
    assert "dry-run" in str(update["approval_marker"])


# ---------------------------------------------------------------------------
# ADR-030：interrupt 挂起 → resume 后按当前 config 重判（负向先红）
# ---------------------------------------------------------------------------


def test_require_approval_interrupts_then_resume_reevaluates_with_current_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-030（架构 v5.2:238）：恢复后以**当前** config 重判，禁止沿用挂起时快照。

    证明方式（prd 验收②「测试中途修改 governance config 的 version 字段」）：
    - 挂起时基准 config：freq_max_touches=2，1 次窗口内触达 → 候选放行，触发审批挂起；
    - resume **前**把 config 换成 version=当前新版 + freq_max_touches=1 的对象；
    - 恢复后重判：1 次触达已达新上限 → FREQ_GLOBAL_7D DENY，sendable 清空；
    - state.rule_version 与审计载荷 rule_version 均为**新版本**
      （若沿用挂起快照，候选仍会放行、版本仍是旧值——两条断言同时证伪快照路径）。
    """
    audit_calls: list[RejudgeAudit] = []
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: _record_audit_call(audit_calls, record),
    )
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    # ① 首跑：decision（真实 governance.yaml，freq_max_touches=2）放行 → confirm_plan →
    #    gate 判 REQUIRE_APPROVAL → interrupt 挂起（裁决 #8：任务级批量审批）。
    suspended = graph.invoke(_approval_state(), thread)

    assert "__interrupt__" in suspended, "REQUIRE_APPROVAL 必须 interrupt 挂起（架构 v5.2:612）"
    payload = suspended["__interrupt__"][0].value
    assert payload["kind"] == "task_batch_approval"
    assert payload["candidate_ids"] == ["CAND-1"], "审批对象 = 候选名单快照（裁决 #8）"
    assert payload["required_signers"] == ["ops_lead"], "1 客户 → tier1 运营负责人单签（裁决 #25）"
    assert payload["rule_version_at_suspend"] == _base_config().version
    assert "send" not in suspended.get("visited_nodes", []), "挂起时不得到达 send"

    # ② 中途修改 governance config：version 字段变更 + 频控阈值收紧（当前版本）。
    monkeypatch.setattr(
        gate,
        "_load_config",
        lambda: _current_config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=1),
    )

    # ③ resume：任务级批量审批一次覆盖（裁决 #8）→ 恢复后以当前 config 重判（ADR-030）。
    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["approval_status"] == "resumed-approved"
    assert final["sendable_candidate_ids"] == [], "新 config 下候选被 DENY，不得放行"
    assert "CAND-1" in final["denied_candidate_ids"]
    assert "FREQ_GLOBAL_7D" in final["suppressed_rule_ids"], "重判 DENY 须留痕 rule_id"
    assert final["rule_version"] == _CURRENT_RULE_VERSION, (
        "恢复后 state.rule_version 必须是重判时的当前版本（ADR-030，非挂起快照）"
    )
    assert final.get("sent", []) == [], "重判 DENY 的批次 send 不得发出任何候选"
    assert "gate" in final["visited_nodes"]
    assert "send" in final["visited_nodes"]

    # ④ 重判结论写审计：rule_version 同为新版本（与 state 断言互证）。
    assert len(audit_calls) == 1, "恢复重判必须写一条审计（ADR-030）"
    audit_record = audit_calls[0]
    assert audit_record.rule_version == _CURRENT_RULE_VERSION
    assert audit_record.approval_status == "resumed-approved"
    assert audit_record.sendable_candidate_ids == ()
    assert any(d.rule_id == "FREQ_GLOBAL_7D" for d in audit_record.denied)


def test_expired_approval_is_not_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E9 负向（先红）：挂起后真实经过 > 48h 才 resume → 审批过期，**不默认通过**。

    时间推进用**注入的服务端墙钟**（不 sleep）：过期判定必须以服务端真实墙钟为基准，
    不能用客户端自带的时刻，也不能用 state 里那个任务起始时刻的陈旧 ``now``。
    """
    audit_calls: list[RejudgeAudit] = []
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: _record_audit_call(audit_calls, record),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    suspended = graph.invoke(_approval_state(), thread)
    assert "__interrupt__" in suspended

    # 真实经过 48h + 1h（服务端墙钟推进）
    clock.advance_hours(_TIMEOUT_HOURS + 1)
    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["approval_status"] == "approval-expired", "E9：48h 后 resume = 过期"
    assert final["sendable_candidate_ids"] == [], "过期审批不得默认通过、不得放行"
    assert final.get("sent", []) == []
    assert audit_calls
    assert audit_calls[0].approval_status == "approval-expired"


def test_client_supplied_resumed_at_cannot_bypass_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E9 负向（先红）：客户端带**过去的** ``resumed_at`` 伪造未超时 → 仍判过期。

    这是旧实现的核心漏洞：过期判定直接采信客户端时刻，真实过了 100h 也判
    ``expired=False``——E9 形同虚设。客户端时刻**只作参考记录**，不参与判定。
    """
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(), thread)
    clock.advance_hours(_TIMEOUT_HOURS + 52)  # 真实已过 100h
    # 客户端回填一个「刚刚才提交」的过去时刻，企图伪装成未超时
    forged_resumed_at = _NOW + timedelta(minutes=5)
    final = graph.invoke(
        Command(resume={"approved": True, "resumed_at": forged_resumed_at}),
        thread,
    )

    assert final["approval_status"] == "approval-expired", (
        "客户端自带的过去时刻不得绕过 48h 超时（E9 形同虚设）"
    )
    assert final["sendable_candidate_ids"] == []
    assert final.get("sent", []) == []


def test_client_supplied_future_resumed_at_cannot_extend_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E9 负向（先红）：客户端带**未来的** ``resumed_at`` 不得延长超时窗口。

    钳制到 ``min(client, server_now)`` 也挡不住这一路——未来时刻会把
    ``submitted_at + timeout`` 推后，从而让已过期审批复活。
    """
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(), thread)
    clock.advance_hours(_TIMEOUT_HOURS + 1)
    future_resumed_at = _WALL_CLOCK + timedelta(days=365)
    final = graph.invoke(
        Command(resume={"approved": True, "resumed_at": future_resumed_at}),
        thread,
    )

    assert final["approval_status"] == "approval-expired", (
        "客户端自带的未来时刻不得延长超时窗口、不得让过期审批复活"
    )
    assert final["sendable_candidate_ids"] == []
    assert final.get("sent", []) == []


def test_approval_within_window_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E9 正向（防修复过头）：窗口内 resume → 正常批准并走**当前** config 重判。"""
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(), thread)
    clock.advance_hours(1)  # 远在 48h 窗口内
    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["approval_status"] == "resumed-approved", "窗口内的批准不得被误判为过期"
    assert final["sendable_candidate_ids"] == ["CAND-1"], "窗口内批准 → 按当前 config 重判后放行"


def test_suspend_payload_records_server_wall_clock_not_state_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起载荷里的 ``submitted_at`` 必须是**服务端墙钟**，不是 state 的陈旧 ``now``。

    state["now"] 是任务起始时刻（客户端可带、且随任务推进变旧）；超时窗口的起点
    必须是服务端记录的真实挂起时刻。
    """
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    suspended = graph.invoke(_approval_state(), thread)
    assert "__interrupt__" in suspended

    payload = suspended["__interrupt__"][0].value
    assert payload["submitted_at"] == _WALL_CLOCK, "挂起时刻必须取服务端墙钟"
    assert payload["submitted_at"] != _NOW, "挂起时刻不得取 state['now']（任务起始时刻）"


def test_missing_server_suspend_record_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拿不到服务端挂起记录 → **fail-closed**（视为过期，不放行）。

    不得回退成「按当前时刻起算」——那正是旧实现让超时永远不触发的漏洞。
    """
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    clock = _FakeClock(_WALL_CLOCK)
    monkeypatch.setattr(gate, "_utc_now", clock, raising=False)
    # 服务端留痕不可读（checkpointer 缺失 / 记录丢失）
    monkeypatch.setattr(gate, "_server_submitted_at", lambda: None)
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(), thread)
    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["approval_status"] == "approval-expired", (
        "无法证明未超时 → 不得放行（fail-closed）"
    )
    assert final["sendable_candidate_ids"] == []
    assert final.get("sent", []) == []


def test_rejected_resume_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """裁决 #8 语义边界：resume 明确拒绝 → 批次不放行（一次 resume 覆盖全批）。"""
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(), thread)
    final = graph.invoke(Command(resume={"approved": False}), thread)

    assert final["approval_status"] == "rejected"
    assert final["sendable_candidate_ids"] == []
    assert final.get("sent", []) == []


def test_approve_resume_cannot_rescue_denied_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """红线②负向（resume 维度）：``Command(resume=approve)`` 也救不回 DENY 候选。

    - 挂起批次 sendable 不含 CAND-D（DENY 留痕于 denied_details）；
    - 人工批准整批后：CAND-D 仍不得进入 sendable / sent，DENY 留痕必须保留；
    - approve 只放行 REQUIRE_APPROVAL 批次，DENY 交集始终被拒。
    """
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        lambda record: ("audit-1", 1, "0" * 64),
    )
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()
    state = _approval_state(
        sendable_candidate_ids=["CAND-1"],
        denied_candidate_ids=["CAND-D"],
        suppressed_rule_ids=["SUP_EXPLICIT_REFUSAL"],
        denied_details=[
            {
                "candidate_id": "CAND-D",
                "rule_id": "SUP_EXPLICIT_REFUSAL",
                "reason": "命中抑制红线：明确拒绝/勿扰标记",
            }
        ],
    )

    suspended = graph.invoke(state, thread)
    assert "__interrupt__" in suspended

    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["approval_status"] == "resumed-approved"
    assert "CAND-D" not in final["sendable_candidate_ids"], "approve 不得救回 DENY 候选（红线②）"
    assert all(item["candidate_id"] != "CAND-D" for item in final.get("sent", [])), (
        "DENY 候选不得出现在 mock 发送记录"
    )
    assert "CAND-D" in final["denied_candidate_ids"], "approve 后 DENY 留痕必须保留"
    assert "SUP_EXPLICIT_REFUSAL" in final["suppressed_rule_ids"]


# ---------------------------------------------------------------------------
# 真 PG：重判写 audit_log（红线③，唯一走 insert_statement_sql）
# ---------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        yield connection


@pytest.mark.pg
def test_resume_rejudge_writes_audit_log_with_current_rule_version(
    conn: psycopg.Connection[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """红线③ + ADR-030（真 PG）：恢复重判结论写 ``audit_log``。

    - 写入唯一走 ``insert_statement_sql()``（``record_hash`` 由 DB 算）；
    - ``rule_version`` = 重判时**当前** config 版本（非挂起快照）；
    - 租户链经 ``verify_chain`` 校验完整。
    """
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        gate,
        "write_rejudge_audit",
        partial(write_rejudge_audit, conn=conn),
    )
    graph = workflow.build_graph(checkpointer=MemorySaver())
    thread = _thread_config()

    graph.invoke(_approval_state(tenant_id=str(tenant_id)), thread)
    # 中途换当前 config（version 变更）→ resume 重判
    monkeypatch.setattr(
        gate,
        "_load_config",
        lambda: _current_config_with(version=_CURRENT_RULE_VERSION, freq_max_touches=1),
    )
    final = graph.invoke(Command(resume={"approved": True}), thread)

    assert final["sendable_candidate_ids"] == []
    assert final["rule_version"] == _CURRENT_RULE_VERSION

    with conn.cursor() as cur:
        cur.execute(
            "select rule_version, action_type, payload->>'approval_status' "
            "from audit_log where tenant_id = %s",
            (tenant_id,),
        )
        row = cur.fetchone()
    assert row is not None, "恢复重判必须在 audit_log 留下记录（ADR-030）"
    assert row[0] == _CURRENT_RULE_VERSION, "审计 rule_version 必须是重判时当前版本"
    assert row[1] == "gate_rejudge"
    assert row[2] == "resumed-approved"

    with conn.cursor() as cur:
        cur.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        rows = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
    result = verify_chain(rows)
    assert result.ok, f"审计哈希链必须完整：{[b.describe() for b in result.breaks]}"
