"""模拟适配器（T-A2）契约测试。

权威依据（每条用例的 docstring 标注了可反查的条款）：
- 架构 v5.3 附录 I §I.1 `JudgmentProvider` 契约 INV-1~INV-4
- 架构 v5.3 附录 I §I.2 `ModelProvider` 契约 INV-1~INV-4
- 架构 v5.3 附录 I §I.2 INV-5（重试在适配器内部，最多 1 次 + 必须写审计）
- 架构 v5.3 附录 I §I.3 置信度与降级纪律（高 0.85 / 低 0.60 / 中文 0.70）
- 架构 v5.3 附录 L.4 G-06（重试收敛在 Provider 适配器层）
- PRD E3（外部系统不可用 → 动作转人工待办并记录降级原因）
- 红线③（审计 append-only + 哈希链；写审计唯一口径是 `insert_statement_sql()`）
- 红线②（DENY 不可绕过 → 判断模型不得自行返回 ALLOW）

本文件**不含**需要真 PostgreSQL 的用例（那部分在 `test_mock_adapter_pg.py`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.adapters.mock_adapter import (
    AUDIT_ACTION_RETRY,
    AuditEntry,
    AuditWriteError,
    MockJudgmentProvider,
    MockModelProvider,
    pg_audit_writer,
)
from src.audit.hash_chain import HASHED_FIELDS, insert_statement_sql
from src.providers.protocols import (
    DEGRADE_E3_PROVIDER_UNAVAILABLE,
    EXTENSION_WHITELIST,
    JUDGMENT_ABSTAIN,
    ChatResponse,
    EgressDenied,
    GenerationRequest,
    HealthStatus,
    JudgmentRequest,
    JudgmentResult,
    Message,
    ModelCapabilities,
    ProviderError,
    ProviderTimeoutError,
    Question,
    Usage,
    validate_extensions,
)

MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "adapters" / "mock_adapter.py"


# --------------------------------------------------------------------------- 夹具助手


def _judgment_request(
    choices: tuple[str, ...] = ("send_coupon", "wait"),
    *,
    qid: str = "intent",
    locale: str = "zh-CN",
) -> JudgmentRequest:
    return JudgmentRequest(
        task_id="task-judge-1",
        questions=(Question(qid=qid, kind="Choice", choices=choices, score_range=None),),
        locale=locale,
    )


def _generation_request(task_id: str = "task-gen-1") -> GenerationRequest:
    return GenerationRequest(
        task_id=task_id,
        messages=(Message(role="user", content="唤醒沉睡客户并发放优惠券"),),
        temperature=0.0,
        max_tokens=256,
    )


class _RecordingAuditWriter:
    """记录审计条目的写入器（不落库）。"""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def __call__(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _FakeCursor:
    """假游标：记录执行的 SQL 与参数。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def execute(self, query: str, params: Any = None) -> None:
        self._connection.statements.append((query, params))
        self._connection.last_query = query

    def fetchone(self) -> Any:
        if self._connection.last_query.startswith("insert into audit_log"):
            return ("audit-id-0001", 1, "f" * 64)
        if self._connection.last_query.startswith("select record_hash"):
            return None
        return None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeTransaction:
    """假事务：只记录进出嵌套深度，不连库。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeTransaction:
        self._connection.transaction_depth += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._connection.transaction_depth -= 1


class _FakeConnection:
    """假连接：只记录 SQL，不连库。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.last_query = ""
        self.transaction_depth = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


class _DenyAllGate:
    """恒拒绝的出境闸门（桩的降级点）。"""

    def check(self, *, provider_id: str, endpoint_id: str) -> None:
        raise EgressDenied(f"出境开关关闭：{provider_id}/{endpoint_id}")


# ----------------------------------------------------------------- JudgmentProvider


def test_judgment_returns_typed_result_not_bare_dict() -> None:
    """判断结果必须是 `JudgmentResult` 契约结构，不得是裸 dict（架构 v5.3 §I.1 INV-1）。"""
    provider = MockJudgmentProvider()
    result = provider.judge(_judgment_request())

    assert isinstance(result, JudgmentResult)
    assert not isinstance(result, dict)
    assert result.provider_id == provider.provider_id


def test_judgment_result_never_has_none_core_fields() -> None:
    """`choice` / `score` / `confidence` 三个核心字段永不为 None（架构 v5.3 §I.1 INV-2）。"""
    provider = MockJudgmentProvider()
    result = provider.judge(_judgment_request())

    assert result.choice is not None
    assert result.score is not None
    assert result.confidence is not None


def test_judgment_unavailable_returns_abstain_degraded_instead_of_raising() -> None:
    """不可用时转 ABSTAIN + degraded，不向调用方抛业务异常（架构 v5.3 §I.1 INV-3）。"""
    provider = MockJudgmentProvider(available=False, degrade_reason="JEV_TIMEOUT")

    result = provider.judge(_judgment_request())

    assert result.choice == JUDGMENT_ABSTAIN
    assert result.degraded is True
    assert result.degrade_reason == "JEV_TIMEOUT"
    assert result.confidence == 0.0


def test_judgment_never_returns_allow() -> None:
    """判断模型不得自行返回 ALLOW——DENY 权永远在规则内核（架构 v5.3 §I.1 INV-4 / 红线②）。"""
    provider = MockJudgmentProvider()

    result = provider.judge(_judgment_request(choices=("ALLOW", "DENY")))

    assert result.choice != "ALLOW"
    assert result.choice == "DENY"
    assert not hasattr(result, "conclusion")


def test_judgment_extensions_stay_within_whitelist() -> None:
    """扩展字段白名单外一律拒绝，防供应商特定结构泄漏（架构 v5.3 §I.1 扩展字段白名单）。"""
    provider = MockJudgmentProvider(supported_extensions=("rationale", "margin"))

    result = provider.judge(_judgment_request())

    assert set(result.extensions) <= set(EXTENSION_WHITELIST)
    with pytest.raises(ValueError):
        validate_extensions({"vendor_specific_field": 1})


def test_judgment_low_confidence_is_marked_degraded() -> None:
    """置信度低于阈值（中文 0.70）时标 degraded + LOW_CONFIDENCE（架构 v5.3 §I.3）。"""
    provider = MockJudgmentProvider(confidence_override=0.5)

    result = provider.judge(_judgment_request(locale="zh-CN"))

    assert result.degraded is True
    assert result.degrade_reason == "LOW_CONFIDENCE"


# ------------------------------------------------------------------- ModelProvider


def test_model_response_fills_model_version_even_on_failure_and_retry() -> None:
    """每次调用（含失败与重试）都必须回填 `model_version`（架构 v5.3 §I.2 INV-2）。"""
    provider = MockModelProvider(failure_modes=("timeout",), audit_writer=_RecordingAuditWriter())

    response = provider.generate(_generation_request())

    assert response.model_version == provider.model_version
    assert "@" in response.model_version
    assert response.model_version.count("/") == 2

    failing = MockModelProvider(
        failure_modes=("timeout", "timeout"), audit_writer=_RecordingAuditWriter()
    )
    with pytest.raises(ProviderError) as excinfo:
        failing.generate(_generation_request())
    assert excinfo.value.model_version == failing.model_version


def test_model_failure_raises_provider_error() -> None:
    """生成失败抛 `ProviderError` 子类，且不做 provider 自动切换（架构 v5.3 §I.2 INV-1 / INV-4）。"""
    provider = MockModelProvider(
        failure_modes=("error", "error"), audit_writer=_RecordingAuditWriter()
    )

    with pytest.raises(ProviderError):
        provider.generate(_generation_request())

    assert provider.provider_id == "mock_model"


def test_model_call_is_blocked_when_egress_gate_denies() -> None:
    """调用前必过 EgressGate，适配器无权绕过（架构 v5.3 §I.2 INV-3）。"""
    provider = MockModelProvider(egress_gate=_DenyAllGate())

    with pytest.raises(ProviderError) as excinfo:
        provider.generate(_generation_request())

    assert excinfo.value.degrade_reason == "EGRESS_OFF"
    assert provider.attempts == []


def test_model_capabilities_and_healthcheck_are_reported() -> None:
    """适配器须显式声明能力并可健康检查（架构 v5.3 §I.2 `capabilities()` / `healthcheck()`）。"""
    provider = MockModelProvider()

    capabilities = provider.capabilities()
    health = provider.healthcheck()

    assert isinstance(capabilities, ModelCapabilities)
    assert capabilities.provider_id == provider.provider_id
    assert isinstance(health, HealthStatus)
    assert health.status in {"healthy", "degraded", "unavailable"}


# --------------------------------------------------------------------- E3 / INV-5


def test_timeout_triggers_exactly_one_retry() -> None:
    """超时后重试**恰 1 次**（架构 v5.3 §I.2 INV-5 / 附录 L.4 G-06）。"""
    provider = MockModelProvider(failure_modes=("timeout",), audit_writer=_RecordingAuditWriter())

    response = provider.generate(_generation_request())

    assert isinstance(response, ChatResponse)
    assert [(a.attempt_number, a.outcome) for a in provider.attempts] == [
        (1, "timeout"),
        (2, "success"),
    ]


def test_retry_is_capped_at_one_attempt_even_with_more_failures() -> None:
    """失败剧本再长，重试也不得超过 1 次（架构 v5.3 §I.2 INV-5「最多 1 次」）。"""
    provider = MockModelProvider(
        failure_modes=("timeout", "timeout", "timeout"),
        audit_writer=_RecordingAuditWriter(),
    )

    with pytest.raises(ProviderError):
        provider.generate(_generation_request())

    assert len(provider.attempts) == 2


def test_retry_writes_audit_with_attempt_number_reason_and_result() -> None:
    """重试必须写审计，且含 attempt_number / retry_reason / retry_result（架构 v5.3 §I.2 INV-5）。"""
    writer = _RecordingAuditWriter()
    provider = MockModelProvider(failure_modes=("timeout",), audit_writer=writer)

    provider.generate(_generation_request())

    assert len(writer.entries) == 1
    entry = writer.entries[0]
    assert entry.action_type == AUDIT_ACTION_RETRY
    assert entry.payload["attempt_number"] == 2
    assert entry.payload["retry_reason"] == "TIMEOUT"
    assert entry.payload["retry_result"] == "SUCCESS"


def test_retry_audit_uses_insert_statement_sql() -> None:
    """重试审计走 `insert_statement_sql()`（哈希链口径），不得自己拼 INSERT（红线③）。"""
    connection = _FakeConnection()
    recorded: list[AuditEntry] = []
    pg_writer = pg_audit_writer(connection)

    def writer(entry: AuditEntry) -> None:
        recorded.append(entry)
        pg_writer(entry)

    provider = MockModelProvider(failure_modes=("timeout",), audit_writer=writer)
    provider.generate(_generation_request())

    inserts = [sql for sql, _params in connection.statements if sql.startswith("insert into")]
    assert len(inserts) == 1
    assert inserts[0] == insert_statement_sql()
    params = next(params for sql, params in connection.statements if sql.startswith("insert into"))
    assert params is not None
    assert len(params) == len(HASHED_FIELDS) + 1
    assert recorded[0].payload["attempt_number"] == 2


def test_adapter_module_does_not_handwrite_audit_insert() -> None:
    """适配器源码里不得出现自拼的 audit INSERT——唯一口径是 `insert_statement_sql()`（红线③）。"""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "insert into audit_log" not in source


def test_failed_retry_escalates_to_human() -> None:
    """重试仍失败 → 抛 ProviderError 并带转人工信号 + 降级原因（PRD E3 / 架构 v5.3 §I.2 INV-5）。"""
    writer = _RecordingAuditWriter()
    provider = MockModelProvider(failure_modes=("timeout", "timeout"), audit_writer=writer)

    with pytest.raises(ProviderError) as excinfo:
        provider.generate(_generation_request())

    error = excinfo.value
    assert error.escalate_to_human is True
    assert error.degrade_reason == DEGRADE_E3_PROVIDER_UNAVAILABLE
    assert error.attempt_number == 2
    assert error.retry_result == "FAILED"
    assert writer.entries[0].payload["retry_result"] == "FAILED"


def test_retry_without_audit_writer_blocks_the_call() -> None:
    """重试必须写审计——写不了就**阻断业务**，不得静默继续（架构 v5.3 §I.2 INV-5 / 红线③）。"""
    provider = MockModelProvider(failure_modes=("timeout",))

    with pytest.raises(AuditWriteError):
        provider.generate(_generation_request())


def test_provider_timeout_error_is_a_provider_error() -> None:
    """`ProviderTimeoutError` 必须是 `ProviderError` 子类（架构 v5.3 §I.2 INV-1）。"""
    assert issubclass(ProviderTimeoutError, ProviderError)


# ---------------------------------------------------------------------- 确定性 / 流式


def test_mock_responses_are_deterministic() -> None:
    """桩响应确定性：同 seed 同输入 → 同输出。"""
    first = MockModelProvider(seed=7)
    second = MockModelProvider(seed=7)

    assert (
        first.generate(_generation_request()).content
        == second.generate(_generation_request()).content
    )
    assert first.model_version == second.model_version

    judge_first = MockJudgmentProvider(seed=7)
    judge_second = MockJudgmentProvider(seed=7)
    assert (
        judge_first.judge(_judgment_request()).choice
        == judge_second.judge(_judgment_request()).choice
    )


def test_stream_yields_chunks() -> None:
    """`stream()` 返回迭代器，逐块产出，且拼回全文等于 `generate()` 的内容。"""
    provider = MockModelProvider()

    stream = provider.stream(_generation_request())
    assert iter(stream) is stream
    chunks = list(stream)
    assert len(chunks) > 1
    assert "".join(chunks) == provider.generate(_generation_request()).content


def test_usage_is_reported_on_response() -> None:
    """响应须带 usage（input/output tokens + provider），供成本计量使用（架构 v5.3 §I.2）。"""
    provider = MockModelProvider()

    response = provider.generate(_generation_request())

    assert isinstance(response.usage, Usage)
    assert response.usage.provider == provider.provider_id
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
