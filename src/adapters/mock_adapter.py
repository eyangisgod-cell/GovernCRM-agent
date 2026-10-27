"""模拟适配器（T-A2）：`JudgmentProvider` / `ModelProvider` 的**确定性桩**。

权威依据
--------
- 架构 v5.3 附录 I §I.1 `JudgmentProvider` 契约（INV-1~INV-4、扩展字段白名单）
- 架构 v5.3 附录 I §I.2 `ModelProvider` 契约（INV-1~INV-5、`model_version` 格式）
- 架构 v5.3 附录 I §I.3 置信度与降级纪律（高 0.85 / 低 0.60 / 中文 0.70）
- 架构 v5.3 附录 L.4 G-06（重试收敛在 Provider 适配器层：最多 1 次 + 必须写审计）
- PRD E3（外部系统不可用 → 动作转人工待办并记录降级原因）
- 红线③（审计 append-only + 哈希链；写审计唯一口径 `src.audit.hash_chain.insert_statement_sql()`）
- 红线②（DENY 不可绕过 → 判断模型不得自行返回 ALLOW）

红线遵守
--------
- **红线②**：判断桩永远不会产出 "放行" 语义的 `choice`，`JudgmentResult` 也没有结论字段——
  DENY 权永远在 `policy_kernel`。
- **红线③**：审计写入只经 `pg_audit_writer()` → `insert_statement_sql()`；`record_hash`
  由数据库按链口径算出，调用方不传；写失败即抛 `AuditWriteError` **阻断业务**。
- **红线⑤**：本模块是桩，**不 import 任何供应商 SDK**（litellm / openai 等一律不碰），
  只依赖标准库 + `psycopg`（白名单内的数据库驱动）。

范围边界
--------
- 本模块只提供桩；**不实现真实供应商适配器**（Jev / DeepSeek / 通义）。
- **不实现 `EgressGate` 本身**（属 `src/providers/` 协议与网关，Wave 3/4）：桩只预留
  「调用前必过闸门」的校验点——注入 `egress_gate` 即可，未注入则视为本地桩不出境。
- 契约类型（`JudgmentRequest` / `ChatResponse` 等）的**唯一定义点**是
  `src.providers.protocols`（拍板⑥）；本模块只从那里 import，不再自带类型定义。
  业务层不得 import 本模块的桩类型。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from src.audit.chain import chain_tip, chain_write_transaction, retry_on_chain_conflict
from src.audit.hash_chain import insert_statement_sql
from src.providers.protocols import (
    ALLOW_LIKE_CHOICES,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_LOW_ZH,
    DEGRADE_E3_PROVIDER_UNAVAILABLE,
    DEGRADE_EGRESS_OFF,
    EXTENSION_WHITELIST,
    JUDGMENT_ABSTAIN,
    MAX_RETRY_ATTEMPTS,
    AttemptRecord,
    ChatResponse,
    EgressDenied,
    EgressGate,
    GenerationRequest,
    HealthStatus,
    JudgmentCapabilities,
    JudgmentRequest,
    JudgmentResult,
    ModelCapabilities,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Question,
    Usage,
    validate_extensions,
)

__all__ = [
    "AUDIT_ACTION_RETRY",
    "AuditEntry",
    "AuditWriteError",
    "ConnectionLike",
    "CursorLike",
    "MockJudgmentProvider",
    "MockModelProvider",
    "pg_audit_writer",
]

#: 重试审计的 `action_type`。
AUDIT_ACTION_RETRY = "provider_retry"


@dataclass(frozen=True)
class AuditEntry:
    """一条审计记录的业务内容（写入 `audit_log` 的业务列全集）。

    列口径来自 `src.audit.hash_chain.HASHED_FIELDS`；本结构**不含** `record_hash`
    与 `prev_hash`——前者由数据库按链口径算出，后者由写入器按租户链查询得出。
    """

    tenant_id: str
    trace_id: str
    action_type: str
    object_type: str
    object_id: str
    model_version: str
    prompt_version: str
    skill_version: str
    rule_version: str
    task_id: str | None = None
    hit_rule_ids: tuple[str, ...] = ()
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


#: 审计写入器签名（依赖注入：桩不自己连库）。
AuditWriter = Callable[[AuditEntry], None]


class AuditWriteError(RuntimeError):
    """审计写入失败/缺失——**阻断业务**（红线③：写失败即阻断，不得 catch 后继续）。"""


# ------------------------------------------------------------------------- 协议与闸门


class CursorLike(Protocol):
    """最小游标协议（只为让写入器可被假连接测试）。"""

    def execute(self, query: str, params: Any = None) -> None: ...

    def fetchone(self) -> Any: ...

    def __enter__(self) -> CursorLike: ...

    def __exit__(self, *exc: object) -> None: ...


class ConnectionLike(Protocol):
    """最小连接协议。

    `cursor()` 的返回类型放宽为 `Any`：`psycopg.Connection.cursor` 是重载签名，
    与具体的游标协议在类型层面不兼容，而本写入器只用到 `execute` / `fetchone` /
    上下文管理器三项能力。

    `transaction()` 用于把「取链尾 + 插入」关进同一事务（红线③：并发写不断链）。
    """

    def cursor(self) -> Any: ...

    def transaction(self) -> Any: ...


# -------------------------------------------------------------------------- 工具函数


def _digest_int(*parts: object) -> int:
    """确定性摘要（**不用**内置 `hash`——它按进程随机化，破坏可复现性）。"""
    payload = "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")


def _last_record_hash(connection: ConnectionLike, tenant_id: str) -> str:
    """取该租户链上最后一条记录的哈希（无记录时为创世值）。"""
    return chain_tip(connection, tenant_id)


def pg_audit_writer(connection: ConnectionLike) -> AuditWriter:
    """返回把审计条目写进真 PostgreSQL 的写入器。

    **唯一写入口径**：`src.audit.hash_chain.insert_statement_sql()`——`record_hash`
    由数据库按链口径算出，调用方不传（客户端算的哈希不是链上那份）。
    `prev_hash` 由写入器按「同租户上一条 `record_hash`」查得，首条为创世值。

    写失败**不吞异常**：异常向上抛，阻断业务（红线③）。
    """

    def _write(entry: AuditEntry) -> None:
        def _insert() -> None:
            # 取链尾与插入同事务、同租户串行（红线③：并发写不断链）。
            with chain_write_transaction(connection, entry.tenant_id):
                prev_hash = _last_record_hash(connection, entry.tenant_id)
                # 占位符顺序必须严格等于 HASHED_FIELDS 顺序 + prev_hash。
                values: list[Any] = [
                    entry.tenant_id,
                    entry.trace_id,
                    entry.task_id,
                    None,  # worker_id
                    None,  # decision_id
                    None,  # approval_id
                    "provider",  # actor_type（audit_log CHECK 允许 system/user/agent/provider）
                    None,  # actor_id
                    None,  # actor_role
                    entry.action_type,
                    entry.object_type,
                    entry.object_id,
                    list(entry.hit_rule_ids),
                    entry.model_version,
                    entry.prompt_version,
                    entry.skill_version,
                    entry.rule_version,
                    Jsonb(dict(entry.input_snapshot)),
                    Jsonb(dict(entry.evidence)),
                    Jsonb(dict(entry.payload)),
                    "sha256",
                    prev_hash,
                ]
                with connection.cursor() as cursor:
                    cursor.execute(insert_statement_sql(), values)
                    cursor.fetchone()

        # 链尾被并发写入方抢占（唯一约束冲突）→ 重读链尾重试一次；再失败则异常上抛。
        retry_on_chain_conflict(_insert)

    return _write


# ------------------------------------------------------------------ JudgmentProvider 桩


class MockJudgmentProvider:
    """判断模型的确定性桩（架构 v5.3 §I.1）。

    - **INV-1**：只返回 `JudgmentResult`，绝不返回裸 dict / 自由文本；
    - **INV-2**：`choice` / `score` / `confidence` 永不为 None；
    - **INV-3**：永不向调用方抛业务异常——不可用一律转 `ABSTAIN + degraded`；
    - **INV-4**：**永不**产出「放行」语义的 `choice`（红线②）。

    桩的可配置行为（不使用 mock 框架，全部是构造参数）：
    `available` / `degrade_reason` / `confidence_override` / `supported_extensions` /
    `egress_gate`。
    """

    def __init__(
        self,
        *,
        provider_id: str = "mock_judgment",
        endpoint_id: str = "mock://local/judge",
        seed: int = 0,
        available: bool = True,
        degrade_reason: str = "MOCK_JUDGMENT_UNAVAILABLE",
        confidence_override: float | None = None,
        supported_extensions: tuple[str, ...] = ("rationale", "margin"),
        egress_gate: EgressGate | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.endpoint_id = endpoint_id
        self.seed = seed
        self.available = available
        self.degrade_reason = degrade_reason
        self.confidence_override = confidence_override
        self.supported_extensions = supported_extensions
        self.egress_gate = egress_gate

    # ---------------------------------------------------------------- 能力声明

    def capabilities(self) -> JudgmentCapabilities:
        """显式声明支持哪些扩展（架构 v5.3 §I.1：业务层消费前先查，不得盲读）。"""
        return JudgmentCapabilities(
            provider_id=self.provider_id,
            supported_extensions=self.supported_extensions,
            max_questions=len(self.questions_supported()),
        )

    def questions_supported(self) -> tuple[str, ...]:
        """桩支持的题型（`Choice` / `Score` / `Noul`）。"""
        return ("Choice", "Score", "Noul")

    def supports_extension(self, name: str) -> bool:
        """是否支持某扩展字段。"""
        return name in self.supported_extensions and name in EXTENSION_WHITELIST

    # -------------------------------------------------------------------- 判定

    def judge(self, req: JudgmentRequest) -> JudgmentResult:
        """判断入口——**永不**向调用方抛业务异常（INV-3）。"""
        try:
            if self.egress_gate is not None:
                self.egress_gate.check(provider_id=self.provider_id, endpoint_id=self.endpoint_id)
            if not self.available:
                return self._abstain(self.degrade_reason)
            return self._judge_available(req)
        except EgressDenied:
            return self._abstain(DEGRADE_EGRESS_OFF)
        except Exception as exc:
            return self._abstain(f"MOCK_JUDGMENT_ERROR:{type(exc).__name__}")

    # ------------------------------------------------------------------ 内部实现

    def _abstain(self, reason: str) -> JudgmentResult:
        """不可用/异常时的统一出口：ABSTAIN + degraded + 降级原因。"""
        return JudgmentResult(
            choice=JUDGMENT_ABSTAIN,
            score=0.0,
            confidence=0.0,
            degraded=True,
            degrade_reason=reason,
            provider_id=self.provider_id,
            extensions={},
        )

    def _judge_available(self, req: JudgmentRequest) -> JudgmentResult:
        if not req.questions:
            return self._abstain("NO_QUESTION")
        question = req.questions[0]
        # INV-4：剔除「放行」语义的候选项——判断模型不得产出放行结论。
        candidates = [c for c in question.choices if c not in ALLOW_LIKE_CHOICES]
        if not candidates:
            return self._abstain("NO_SAFE_CHOICE")

        choice = candidates[_digest_int(self.seed, req.task_id, question.qid) % len(candidates)]
        score = self._score_for(question)
        confidence = self._confidence_for(req, question)
        threshold = CONFIDENCE_LOW_ZH if req.locale.startswith("zh") else CONFIDENCE_LOW
        degraded = confidence < threshold

        extensions = self._extensions_for(req, choice)
        validate_extensions(extensions)
        return JudgmentResult(
            choice=choice,
            score=score,
            confidence=confidence,
            degraded=degraded,
            degrade_reason="LOW_CONFIDENCE" if degraded else None,
            provider_id=self.provider_id,
            extensions=extensions,
        )

    def _score_for(self, question: Question) -> float:
        if question.score_range is not None:
            low, high = question.score_range
            span = max(1, high - low + 1)
            return float(low + _digest_int(self.seed, question.qid, "score") % span)
        return round(_digest_int(self.seed, question.qid, "score") % 1001 / 1000, 4)

    def _confidence_for(self, req: JudgmentRequest, question: Question) -> float:
        if self.confidence_override is not None:
            return self.confidence_override
        # 落在「高置信」档（≥ 0.85），避免桩默认就把判断推入降级路径。
        raw = _digest_int(self.seed, req.task_id, question.qid, "confidence") % 15
        return round(CONFIDENCE_HIGH + raw / 100, 4)

    def _extensions_for(self, req: JudgmentRequest, choice: str) -> dict[str, Any]:
        extensions: dict[str, Any] = {}
        if self.supports_extension("rationale"):
            extensions["rationale"] = f"mock-judgement:{choice}"
        if self.supports_extension("margin"):
            extensions["margin"] = round(
                _digest_int(self.seed, req.task_id, "margin") % 1000 / 1000, 4
            )
        if self.supports_extension("per_candidate_scores"):
            extensions["per_candidate_scores"] = {
                question.qid: self._score_for(question) for question in req.questions
            }
        return extensions


# --------------------------------------------------------------------- ModelProvider 桩


class MockModelProvider:
    """生成模型的确定性桩（架构 v5.3 §I.2）。

    - **INV-1**：`generate` / `stream` 返回统一 `ChatResponse`，失败抛 `ProviderError` 子类；
    - **INV-2**：每次调用（**含失败与重试**）都回填 `model_version`；
    - **INV-3**：调用前必过 `EgressGate`（注入 `egress_gate` 即生效，桩无权绕过）；
    - **INV-4**：不做 provider 自动切换；
    - **INV-5**：重试在适配器内部实现，**最多 1 次**，且**必须写审计**
      （`attempt_number` / `retry_reason` / `retry_result`）；重试仍失败 → 抛
      `ProviderError` 交由调用方决定降级/转人工。

    桩的可配置行为（不使用 mock 框架）：
    `failure_modes`（失败剧本，按尝试序号消耗，超出长度即成功）、`egress_gate`、
    `audit_writer`、`egressed`、`seed`。
    """

    def __init__(
        self,
        *,
        provider_id: str = "mock_model",
        model_name: str = "mock-chat",
        model_revision: str = "20260101",
        adapter_version: str = "adapter-0.1.0",
        endpoint_id: str = "mock://local/v1/chat",
        seed: int = 0,
        failure_modes: Sequence[str] = (),
        egressed: bool = False,
        egress_gate: EgressGate | None = None,
        audit_writer: AuditWriter | None = None,
        tenant_id: str = "00000000-0000-0000-0000-000000000000",
        trace_id: str = "mock-trace-0001",
        prompt_version: str = "prompt@mock",
        skill_version: str = "skill@mock",
        rule_version: str = "2026-10-24.frozen",
        chunk_size: int = 24,
    ) -> None:
        self.provider_id = provider_id
        self.model_name = model_name
        self.model_revision = model_revision
        self.adapter_version = adapter_version
        self.endpoint_id = endpoint_id
        self.seed = seed
        self.failure_modes = tuple(failure_modes)
        self.egressed = egressed
        self.egress_gate = egress_gate
        self.audit_writer = audit_writer
        self.tenant_id = tenant_id
        self.trace_id = trace_id
        self.prompt_version = prompt_version
        self.skill_version = skill_version
        self.rule_version = rule_version
        self.chunk_size = chunk_size
        #: 本次调用内的尝试轨迹（每次调用开始时重置）。
        self.attempts: list[AttemptRecord] = []

    # ---------------------------------------------------------------- 能力声明

    @property
    def model_version(self) -> str:
        """四版本号之一：`<provider_id>/<model_name>/<model_revision>@<adapter_version>`。"""
        return f"{self.provider_id}/{self.model_name}/{self.model_revision}@{self.adapter_version}"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            provider_id=self.provider_id,
            model_name=self.model_name,
            context_window=8192,
            supports_tools=False,
            supports_stream=True,
            supports_prefix_cache=False,
        )

    def healthcheck(self) -> HealthStatus:
        """健康检查：失败剧本的首个模式非成功 → `degraded`。"""
        first = self._scripted_outcome(1)
        if first == "success":
            return HealthStatus(
                provider_id=self.provider_id, endpoint_id=self.endpoint_id, status="healthy"
            )
        return HealthStatus(
            provider_id=self.provider_id,
            endpoint_id=self.endpoint_id,
            status="degraded",
            detail=f"首尝试剧本={first}",
        )

    # -------------------------------------------------------------------- 生成

    def generate(self, req: GenerationRequest) -> ChatResponse:
        """生成入口——失败抛 `ProviderError`（INV-1），重试最多 1 次并写审计（INV-5）。"""
        self._check_egress()
        self.attempts = []
        content = self._run_with_retry(req)
        return self._build_response(req, content)

    def stream(self, req: GenerationRequest) -> Iterator[str]:
        """流式生成——返回**迭代器**，逐块产出（架构 v5.3 §I.2 `stream()`）。"""
        self._check_egress()
        self.attempts = []
        content = self._run_with_retry(req)
        return iter(self._chunked(content))

    # ------------------------------------------------------------------ 内部实现

    def _check_egress(self) -> None:
        """调用前必过 EgressGate（INV-3）——桩无权绕过，故拒绝即抛、不重试。"""
        if self.egress_gate is None:
            return
        try:
            self.egress_gate.check(provider_id=self.provider_id, endpoint_id=self.endpoint_id)
        except EgressDenied as exc:
            raise ProviderError(
                f"出境闸门拒绝调用：{exc}",
                provider_id=self.provider_id,
                endpoint_id=self.endpoint_id,
                model_version=self.model_version,
                reason="EGRESS_OFF",
                degrade_reason=DEGRADE_EGRESS_OFF,
                escalate_to_human=True,
            ) from exc

    def _scripted_outcome(self, attempt_number: int) -> str:
        """失败剧本：按尝试序号消耗 `failure_modes`，超出长度即成功。"""
        index = attempt_number - 1
        if index < len(self.failure_modes):
            return self.failure_modes[index]
        return "success"

    def _run_with_retry(self, req: GenerationRequest) -> str:
        """执行调用 + 最多 1 次重试（INV-5）。"""
        try:
            return self._invoke(req, attempt_number=1)
        except ProviderError as first_error:
            retry_reason = first_error.reason
            try:
                content = self._invoke(req, attempt_number=1 + MAX_RETRY_ATTEMPTS)
            except ProviderError as retry_error:
                self._write_retry_audit(
                    req,
                    attempt_number=1 + MAX_RETRY_ATTEMPTS,
                    retry_reason=retry_reason,
                    retry_result="FAILED",
                )
                raise ProviderError(
                    f"模型调用失败（重试 {MAX_RETRY_ATTEMPTS} 次后仍失败）：{retry_error}",
                    provider_id=self.provider_id,
                    endpoint_id=self.endpoint_id,
                    model_version=self.model_version,
                    reason=retry_error.reason,
                    attempt_number=1 + MAX_RETRY_ATTEMPTS,
                    retry_reason=retry_reason,
                    retry_result="FAILED",
                    degrade_reason=DEGRADE_E3_PROVIDER_UNAVAILABLE,
                    escalate_to_human=True,
                ) from retry_error
            self._write_retry_audit(
                req,
                attempt_number=1 + MAX_RETRY_ATTEMPTS,
                retry_reason=retry_reason,
                retry_result="SUCCESS",
            )
            return content

    def _invoke(self, req: GenerationRequest, *, attempt_number: int) -> str:
        """一次尝试：按剧本失败，或产出确定性文本。"""
        outcome = self._scripted_outcome(attempt_number)
        self.attempts.append(AttemptRecord(attempt_number=attempt_number, outcome=outcome))
        if outcome == "timeout":
            raise ProviderTimeoutError(
                f"供应商超时（模拟）：attempt={attempt_number}",
                provider_id=self.provider_id,
                endpoint_id=self.endpoint_id,
                model_version=self.model_version,
                attempt_number=attempt_number,
            )
        if outcome == "error":
            raise ProviderUnavailableError(
                f"供应商不可用（模拟）：attempt={attempt_number}",
                provider_id=self.provider_id,
                endpoint_id=self.endpoint_id,
                model_version=self.model_version,
                attempt_number=attempt_number,
            )
        return self._build_content(req)

    def _build_content(self, req: GenerationRequest) -> str:
        """确定性文本：同 seed + 同输入 → 同输出。"""
        token = (
            _digest_int(
                self.seed,
                req.task_id,
                req.temperature,
                req.max_tokens,
                *[(message.role, message.content) for message in req.messages],
            )
            % 100000
        )
        body = " ".join(message.content for message in req.messages)
        return f"[mock-model:{token:05d}] {body}"

    def _chunked(self, content: str) -> list[str]:
        size = max(1, self.chunk_size)
        return [content[index : index + size] for index in range(0, len(content), size)]

    def _build_response(self, req: GenerationRequest, content: str) -> ChatResponse:
        return ChatResponse(
            content=content,
            model_version=self.model_version,
            provider_id=self.provider_id,
            endpoint_id=self.endpoint_id,
            egressed=self.egressed,
            usage=Usage(
                input_tokens=sum(len(message.content) for message in req.messages) // 4,
                output_tokens=len(content) // 4,
                provider=self.provider_id,
            ),
            degraded=False,
        )

    def _write_retry_audit(
        self,
        req: GenerationRequest,
        *,
        attempt_number: int,
        retry_reason: str,
        retry_result: str,
    ) -> None:
        """重试审计（INV-5）：含 `attempt_number` / `retry_reason` / `retry_result`。

        写失败**阻断业务**（红线③）——不得 catch 后继续。
        """
        if self.audit_writer is None:
            raise AuditWriteError(
                "重试必须写审计（架构 v5.3 §I.2 INV-5），但未配置审计写入器——阻断业务（红线③）"
            )
        payload: dict[str, Any] = {
            "attempt_number": attempt_number,
            "retry_reason": retry_reason,
            "retry_result": retry_result,
            "provider_id": self.provider_id,
            "endpoint_id": self.endpoint_id,
            "model_version": self.model_version,
            "max_retry_attempts": MAX_RETRY_ATTEMPTS,
        }
        entry = AuditEntry(
            tenant_id=self.tenant_id,
            trace_id=self.trace_id,
            task_id=req.task_id,
            action_type=AUDIT_ACTION_RETRY,
            object_type="provider_call",
            object_id=req.task_id,
            hit_rule_ids=(),
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            skill_version=self.skill_version,
            rule_version=self.rule_version,
            input_snapshot={
                "task_id": req.task_id,
                "messages": [
                    {"role": message.role, "content": message.content} for message in req.messages
                ],
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
            },
            evidence=payload,
            payload=payload,
        )
        try:
            self.audit_writer(entry)
        except AuditWriteError:
            raise
        except Exception as exc:
            raise AuditWriteError(f"重试审计写入失败——阻断业务（红线③）：{exc}") from exc
