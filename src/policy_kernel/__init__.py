"""policy_kernel —— 策略内核（纯函数、零 LLM、零框架依赖、零 I/O 副作用）。

红线 1：本包**不得** import LLM / 框架 / 供应商 SDK / 数据库。
所有判定都必须是可单测的纯函数。写法见 `.trellis/spec/backend/quality-guidelines.md`。

T0.4 只落地配置加载；T-B1 落地数据契约（`Decision` / `RuleId` / `DecisionResult`）；
规则 1–6 判定在 T-B2。
"""

from __future__ import annotations

from src.policy_kernel.config import (
    REDLINE_PATHS,
    REDLINE_RULE_IDS,
    Approval,
    Autonomy,
    Benefit,
    Budget,
    ConfigError,
    ConflictResolution,
    Distillation,
    GovernanceConfig,
    RuleValidationError,
    Segmentation,
    Thresholds,
    apply_overrides,
    load_and_validate,
)
from src.policy_kernel.kernel import Decision, DecisionResult, RuleId

__all__ = [
    "REDLINE_PATHS",
    "REDLINE_RULE_IDS",
    "Approval",
    "Autonomy",
    "Benefit",
    "Budget",
    "ConfigError",
    "ConflictResolution",
    "Decision",
    "DecisionResult",
    "Distillation",
    "GovernanceConfig",
    "RuleId",
    "RuleValidationError",
    "Segmentation",
    "Thresholds",
    "apply_overrides",
    "load_and_validate",
]
