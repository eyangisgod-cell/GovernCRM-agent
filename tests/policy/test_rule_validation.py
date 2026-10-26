"""T-B3 验收测试：规则加载与 fail-fast 校验（G-05）。

对应《MVP_任务拆解.md》§三 · T-B3：
  ① 合法配置加载成功
  ② 非法配置（红线缺字段 / 阈值越界）→ 抛**可操作**错误
  ③ 未知字段拒绝（防拼写错误静默通过）
  ④ 红线参数覆盖拒绝（FR-01b）
  ⑤ `RuleValidationError` 与既有 `ConfigError` 兼容

纯函数单测：无 PG、无 LLM、无框架（红线①）。
负向用例**常驻**，一律不许 skip；环境缺失用 `pytest.fail` 报可操作错误。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from src.policy_kernel.config import (
    ConfigError,
    GovernanceConfig,
    apply_overrides,
    load_and_validate,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_YAML = ROOT / "governance.yaml"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


@pytest.fixture
def governance_yaml() -> Path:
    """仓库根的 governance.yaml —— 配置参数的唯一来源（T0.4）。

    缺失即 `pytest.fail`（不 skip）：配置源缺失是环境问题，不是「用例不适用」。
    """
    if not GOVERNANCE_YAML.exists():
        pytest.fail(
            f"找不到 {GOVERNANCE_YAML}。\n"
            "  governance.yaml 是策略参数的唯一来源（T0.4 产出），缺失则无法验收配置加载。\n"
            "  请在仓库根目录补回该文件（参见 docs/specs/MVP_任务拆解.md §二 · T0.4）。"
        )
    return GOVERNANCE_YAML


@pytest.fixture
def valid_raw(governance_yaml: Path) -> dict[str, Any]:
    """一份合法的原始配置（深拷贝，改动不污染仓库文件）。"""
    raw = yaml.safe_load(governance_yaml.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        pytest.fail(
            f"{governance_yaml} 顶层必须是映射（key: value），实际是 {type(raw).__name__}。"
        )
    return raw


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _rule_validation_error_type() -> type[ConfigError]:
    """取 `RuleValidationError` 类型。

    T-B3 拆解验收点名了这个名字（`RuleValidationError`）。本测试**不**在模块顶层
    import 它——顶层 import 缺失会变成收集错误（ImportError），看不到「断言不成立」的
    红灯信息；改成运行期取，取不到就用 `pytest.fail` 报可操作的失败原因。
    """
    module = importlib.import_module("src.policy_kernel.config")
    error_type = getattr(module, "RuleValidationError", None)
    if error_type is None:
        pytest.fail(
            "src.policy_kernel.config 未导出 `RuleValidationError`。\n"
            "  T-B3 验收要求配置校验错误有专名（拆解文档 T-B3 验收条款点名该名字）。\n"
            "  请在 config.py 补上与 `ConfigError` 兼容的别名（不破坏既有 `except ConfigError` 调用方）。"
        )
    return cast(type[ConfigError], error_type)


# ---------------------------------------------------------------------------
# ① 合法配置加载成功
# ---------------------------------------------------------------------------


def test_valid_config_loads_successfully(governance_yaml: Path) -> None:
    """合法配置加载成功，返回不可变的配置对象。

    依据：《MVP_任务拆解.md》§三 · T-B3「① 合法配置加载成功」。
    """
    config = load_and_validate(governance_yaml)
    assert isinstance(config, GovernanceConfig)
    # 关键红线阈值与《GovernCRM_附录D_冻结版.md》§1 规则 1/2 的口径一致
    assert config.thresholds.complaint_window_days == 90
    assert config.thresholds.refund_rate_threshold == 0.20
    assert config.thresholds.high_value_percentile == 1.0
    assert config.thresholds.freq_window_days == 7
    assert config.thresholds.freq_max_touches == 2
    assert config.thresholds.channel_cooldown_hours == 72
    assert config.thresholds.conflict_window_hours == 72
    # 附录D 规则 3 的三键顺序是冻结语义
    assert config.conflict_resolution.tie_break_order == (
        "platform_layer_score",
        "benefit_net_value",
        "created_at_earliest",
    )


# ---------------------------------------------------------------------------
# ② 红线缺字段 → 可操作错误
# ---------------------------------------------------------------------------


def test_missing_redline_field_raises_actionable_error(
    tmp_path: Path, valid_raw: dict[str, Any]
) -> None:
    """红线参数缺字段 → 抛 `RuleValidationError`，错误信息可操作。

    可操作 = 说清**缺哪个配置项**、**去哪配**、**这个项是干什么的**。
    依据：《GovernCRM_附录D_冻结版.md》§1 规则 1a（`SUP_HIGH_COMPLAINT` 投诉窗口）。
    """
    del valid_raw["thresholds"]["complaint_window_days"]
    error_type = _rule_validation_error_type()
    with pytest.raises(error_type) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "缺少必填配置项" in message
    assert "thresholds.complaint_window_days" in message, "必须点名缺哪个配置项"
    assert "SUP_HIGH_COMPLAINT" in message, "必须说明缺的是哪条红线规则的参数"
    assert "governance.yaml" in message, "必须说明去哪配"
    assert "thresholds" in message, "必须说明改哪个段"


# ---------------------------------------------------------------------------
# ③ 阈值越界 → 可操作错误
# ---------------------------------------------------------------------------


def test_out_of_range_threshold_raises_actionable_error(
    tmp_path: Path, valid_raw: dict[str, Any]
) -> None:
    """阈值越界 → 抛 `RuleValidationError`，错误信息可操作（给出合法区间）。

    依据：《GovernCRM_附录D_冻结版.md》§1 规则 1a —— `SUP_HIGH_REFUND` 的退款率阈值
    是比例，取值必须在 [0, 1]。
    """
    valid_raw["thresholds"]["refund_rate_threshold"] = 5.0
    error_type = _rule_validation_error_type()
    with pytest.raises(error_type) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "取值越界" in message
    assert "thresholds.refund_rate_threshold" in message
    assert "5.0" in message, "必须回显实际值"
    assert "<= 1" in message, "必须给出合法区间上界"


# ---------------------------------------------------------------------------
# ④ 未知字段 → 拒绝
# ---------------------------------------------------------------------------


def test_unknown_field_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    """未知字段一律拒绝——防拼写错误静默通过（fail-closed）。

    依据：`.trellis/spec/backend/error-handling.md`「治理相关检查一律 fail-closed」。
    """
    valid_raw["thresholds"]["my_own_threshold"] = 1
    error_type = _rule_validation_error_type()
    with pytest.raises(error_type) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "未定义的配置项" in message
    assert "thresholds.my_own_threshold" in message, "必须点名是哪个字段"


# ---------------------------------------------------------------------------
# ⑤ 红线参数覆盖 → 拒绝（FR-01b）
# ---------------------------------------------------------------------------


def test_redline_parameter_override_is_rejected() -> None:
    """员工级策略包覆盖红线参数 → 直接拒绝，报错可操作。

    依据：《GovernCRM_产品需求文档_PRD_v5.3_补充章节.md》FR-01b
    ——「任何试图覆盖抑制红线或 NO_CONSENT 的配置**在写入时直接拒绝并给出可操作错误**；
    **不得通过员工级配置绕过 DENY**」（红线②：DENY 对所有角色不可绕过）。
    """
    error_type = _rule_validation_error_type()
    with pytest.raises(error_type) as excinfo:
        apply_overrides({"thresholds": {}}, {"thresholds": {"refund_rate_threshold": 0.99}})
    message = str(excinfo.value)
    assert "抑制红线" in message
    assert "thresholds.refund_rate_threshold" in message, "必须点名被拒的配置路径"
    assert "SUP_HIGH_REFUND" in message, "必须点名被触发的红线规则"
    assert "DENY 不可绕过" in message, "必须说明 DENY 不可绕过（红线②）"


# ---------------------------------------------------------------------------
# ⑥ `RuleValidationError` 与既有 `ConfigError` 兼容
# ---------------------------------------------------------------------------


def test_rule_validation_error_is_compatible_with_config_error(tmp_path: Path) -> None:
    """`RuleValidationError` 是 `ConfigError` 家族的成员，且能捕获配置错误。

    依据：《MVP_任务拆解.md》§三 · T-B3 验收「抛 `RuleValidationError`」+ 本任务 PRD §2.1
    ——「**不破坏现有调用方**」：既有 `except ConfigError` 仍要能兜住，
    且新增的 `except RuleValidationError` 也要能捕获配置错误。
    """
    error_type = _rule_validation_error_type()

    # ① 既有调用方不受影响：`ConfigError` 家族关系成立，且仍是 ValueError 的后代
    assert issubclass(error_type, ConfigError), (
        "`RuleValidationError` 必须是 `ConfigError` 的子类或别名，"
        "否则既有 `except ConfigError` 的调用方会漏捕获。"
    )
    assert issubclass(error_type, ValueError), "配置错误必须是 ValueError 的后代（既有约定）"

    # ② `except RuleValidationError` 能捕获 load_and_validate 抛出的配置错误
    with pytest.raises(error_type):
        load_and_validate(tmp_path / "definitely-missing.yaml")

    # ③ 既有 `except ConfigError` 依然能捕获同一异常
    with pytest.raises(ConfigError):
        load_and_validate(tmp_path / "definitely-missing.yaml")
