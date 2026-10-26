"""供应商契约类型（架构 v5.3 §I.1 / §I.2 / §I.3）。

**这一层只放契约**：请求 / 响应 / 错误 / 能力声明 / 出境闸门协议。
实现放在 `src/adapters/`，真正的供应商 SDK 只允许出现在 `src/providers/` 的其它模块
（红线⑤：业务层零供应商依赖）。

拍板⑥（2026-10-26）：这些类型原先暂住在 `src/adapters/mock_adapter.py`，
按架构应归 `src/providers/`。此处是**唯一定义点**，适配器从这里 import。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ALLOW_LIKE_CHOICES",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_LOW_ZH",
    "DEGRADE_E3_PROVIDER_UNAVAILABLE",
    "DEGRADE_EGRESS_OFF",
    "EXTENSION_WHITELIST",
    "JUDGMENT_ABSTAIN",
    "MAX_RETRY_ATTEMPTS",
    "AttemptRecord",
    "ChatResponse",
    "EgressDenied",
    "EgressGate",
    "GenerationRequest",
    "HealthStatus",
    "JudgmentCapabilities",
    "JudgmentRequest",
    "JudgmentResult",
    "Message",
    "ModelCapabilities",
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "Question",
    "Usage",
    "validate_extensions",
]


# --------------------------------------------------------------------------- 常量


#: 判断不可用时的 `choice` 取值（架构 v5.3 §I.1 INV-3）。
JUDGMENT_ABSTAIN = "ABSTAIN"

#: 扩展字段白名单（架构 v5.3 §I.1）——白名单外字段直接拒绝。
EXTENSION_WHITELIST: tuple[str, ...] = ("per_candidate_scores", "rationale", "margin")

#: 置信度档位阈值（架构 v5.3 §I.3；`governance.yaml` 可配，变更走审批）。
CONFIDENCE_HIGH = 0.85
CONFIDENCE_LOW = 0.60
CONFIDENCE_LOW_ZH = 0.70

#: 重试次数上限（架构 v5.3 §I.2 INV-5 / 附录 L.4 G-06）——最多 1 次。
MAX_RETRY_ATTEMPTS = 1

#: 降级原因常量。
DEGRADE_E3_PROVIDER_UNAVAILABLE = "E3_PROVIDER_UNAVAILABLE"
DEGRADE_EGRESS_OFF = "EGRESS_OFF"

#: 「放行」语义的候选项——判断桩**永不**产出这些取值（红线②）。
ALLOW_LIKE_CHOICES: frozenset[str] = frozenset(
    {"ALLOW", "allow", "Allow", "放行", "允许放行", "PASS", "放行通过"}
)


# --------------------------------------------------------------------------- 契约类型


@dataclass(frozen=True)
class Question:
    """判断问题（架构 v5.3 §I.1 `Question`）。"""

    qid: str
    kind: str
    choices: tuple[str, ...]
    score_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class JudgmentRequest:
    """判断请求（架构 v5.3 §I.1 `JudgmentRequest`）。

    契约语义：请求进入 Provider 后**不可被修改**（判断内容要进审计，可变输入无法举证），
    故本实现用 frozen dataclass + tuple 承载。
    """

    task_id: str
    questions: tuple[Question, ...]
    locale: str = "zh-CN"


@dataclass(frozen=True)
class JudgmentResult:
    """判断结果（架构 v5.3 §I.1 `JudgmentResult`）。

    与契约 TypedDict 的差异只有一处，且是**收紧**：`choice` / `score` / `confidence`
    在契约里写作 `X | None`，但 INV-2 要求三者**永不为 None**（不可用时 `choice` 取
    `ABSTAIN` 而非 `None`），故此处声明为非可选——类型层面即杜绝 None。
    """

    choice: str
    score: float
    confidence: float
    degraded: bool
    degrade_reason: str | None
    provider_id: str
    extensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgmentCapabilities:
    """判断适配器的能力声明（架构 v5.3 §I.1「适配器必须显式声明支持哪些扩展」）。"""

    provider_id: str
    supported_extensions: tuple[str, ...]
    max_questions: int


@dataclass(frozen=True)
class Message:
    """一条对话消息（架构 v5.3 §I.2 `Message`）。"""

    role: str
    content: str


@dataclass(frozen=True)
class GenerationRequest:
    """生成请求（架构 v5.3 §I.2 `GenerationRequest`）。"""

    task_id: str
    messages: tuple[Message, ...]
    temperature: float = 0.0
    max_tokens: int = 512


@dataclass(frozen=True)
class Usage:
    """用量（架构 v5.3 §I.2 `Usage`）。"""

    input_tokens: int
    output_tokens: int
    provider: str


@dataclass(frozen=True)
class ChatResponse:
    """生成响应（架构 v5.3 §I.2 `ChatResponse`）。"""

    content: str
    model_version: str
    provider_id: str
    endpoint_id: str
    egressed: bool
    usage: Usage
    degraded: bool


@dataclass(frozen=True)
class ModelCapabilities:
    """生成适配器的能力声明（架构 v5.3 §I.2 `ModelCapabilities`）。"""

    provider_id: str
    model_name: str
    context_window: int
    supports_tools: bool
    supports_stream: bool
    supports_prefix_cache: bool


@dataclass(frozen=True)
class HealthStatus:
    """健康检查结果（架构 v5.3 §I.2 `healthcheck()`）。"""

    provider_id: str
    endpoint_id: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    """一次调用的尝试记录（INV-5 轨迹）。"""

    attempt_number: int
    outcome: str


# ------------------------------------------------------------------------------ 异常


class ProviderError(RuntimeError):
    """供应商调用失败（架构 v5.3 §I.2 INV-1：失败抛 `ProviderError` 子类）。

    承载 E3「转人工待办 + 降级原因」的信号：

    - `escalate_to_human` —— 调用方据此把动作转人工待办；
    - `degrade_reason` —— 降级原因（如 `EGRESS_OFF` / `E3_PROVIDER_UNAVAILABLE`）；
    - `attempt_number` / `retry_reason` / `retry_result` —— 重试轨迹（INV-5 审计三字段）。
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        endpoint_id: str,
        model_version: str,
        reason: str,
        attempt_number: int = 1,
        retry_reason: str | None = None,
        retry_result: str | None = None,
        degrade_reason: str | None = None,
        escalate_to_human: bool = True,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.endpoint_id = endpoint_id
        self.model_version = model_version
        self.reason = reason
        self.attempt_number = attempt_number
        self.retry_reason = retry_reason
        self.retry_result = retry_result
        self.degrade_reason = degrade_reason
        self.escalate_to_human = escalate_to_human


class ProviderTimeoutError(ProviderError):
    """供应商超时（PRD E3 的典型触发形态）。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("reason", "TIMEOUT")
        super().__init__(message, **kwargs)


class ProviderUnavailableError(ProviderError):
    """供应商不可用（连接失败 / 5xx 等非超时故障）。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("reason", "PROVIDER_UNAVAILABLE")
        super().__init__(message, **kwargs)


class EgressDenied(RuntimeError):
    """出境闸门拒绝（架构 v5.3 §I.2 INV-3）。"""


# --------------------------------------------------------------------------- 协议与闸门


class EgressGate(Protocol):
    """出境闸门协议（架构 v5.3 §I.2 INV-3）。

    **不实现 `EgressGate` 本身**（网关实现属 Wave 3/4）：适配器只持有一个闸门引用，
    调用前必过闸门；拒绝由闸门**抛 `EgressDenied`**——桩无权绕过，故拒绝即抛、不重试。
    """

    def check(self, *, provider_id: str, endpoint_id: str) -> None: ...


def validate_extensions(extensions: Mapping[str, Any]) -> None:
    """校验扩展字段白名单——白名单外字段直接拒绝（架构 v5.3 §I.1）。"""
    unknown = sorted(set(extensions) - set(EXTENSION_WHITELIST))
    if unknown:
        raise ValueError(f"扩展字段不在白名单内：{unknown}；白名单 = {list(EXTENSION_WHITELIST)}")
