"""governance.yaml 加载 / 校验 / 红线保护的单测（T0.4）。

三类断言：
1. **一致性**：`governance.yaml` 的 `thresholds` 块与 `docs/specs/评测标注基准_v1.3.yaml`
   的 `thresholds` 块逐项相等（防两处漂移）。
2. **红线保护**：`apply_overrides()` 命中抑制红线参数直接拒绝，报错可操作。
3. **最小单测**：合法配置加载成功；缺字段 / 错类型 / 未知字段 / 越界值各自报错且信息可操作。

纯函数单测：无 PG、无 LLM、无框架。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.policy_kernel import (
    REDLINE_PATHS,
    REDLINE_RULE_IDS,
    ConfigError,
    GovernanceConfig,
    apply_overrides,
    load_and_validate,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_YAML = ROOT / "governance.yaml"
BASELINE_YAML = ROOT / "docs" / "specs" / "评测标注基准_v1.3.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} 顶层必须是映射"
    return data


# ---------------------------------------------------------------------------
# 一致性：thresholds 块两处逐项相等
# ---------------------------------------------------------------------------


def test_thresholds_match_eval_baseline() -> None:
    """governance.yaml 与 评测标注基准_v1.3.yaml 的 thresholds 块必须逐项相等。

    两处是双真相来源，任何一处单改都会在这里变红；要改就同改并过回归门禁。
    """
    baseline = _load_yaml(BASELINE_YAML)["thresholds"]
    governance = _load_yaml(GOVERNANCE_YAML)["thresholds"]
    assert governance == baseline, (
        "thresholds 块漂移：governance.yaml 与 评测标注基准_v1.3.yaml 不一致。\n"
        f"  governance.yaml      = {governance}\n"
        f"  评测标注基准_v1.3.yaml = {baseline}\n"
        "  两处必须同改（红线阈值走「配置变更审批 + 回归门禁」），不允许单改一处。"
    )


def test_thresholds_cover_all_redline_params() -> None:
    """REDLINE_PATHS 里列出的 thresholds.* 路径必须真的存在于配置中。

    反向防漏：如果哪天加了红线参数却忘了登记进 REDLINE_PATHS，这里先红。
    """
    flat_governance = _load_yaml(GOVERNANCE_YAML)
    thresholds = flat_governance["thresholds"]
    for path in REDLINE_PATHS:
        if not path.startswith("thresholds."):
            continue
        key = path.split(".", 1)[1]
        assert key in thresholds, f"红线参数 `{path}` 在 governance.yaml 的 thresholds 块里不存在"


# ---------------------------------------------------------------------------
# 加载：合法配置
# ---------------------------------------------------------------------------


def test_load_and_validate_returns_frozen_config() -> None:
    config = load_and_validate(GOVERNANCE_YAML)
    assert isinstance(config, GovernanceConfig)
    assert config.thresholds.complaint_window_days == 90
    assert config.thresholds.refund_rate_threshold == 0.20
    assert config.thresholds.high_value_percentile == 1.0
    assert config.segmentation.layer_score_base == 10
    assert config.conflict_resolution.tie_break_order == (
        "platform_layer_score",
        "benefit_net_value",
        "created_at_earliest",
    )
    assert config.benefit.floor == 0
    assert config.approval.timeout_hours == 48
    assert config.budget.currency == "CNY"
    assert config.distillation.min_success_count == 3
    assert config.autonomy.mvp_allowed_levels == ("L0", "L1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.thresholds.freq_max_touches = 999  # type: ignore[misc]


def test_load_missing_file_gives_actionable_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(ROOT / "does-not-exist.yaml")
    message = str(excinfo.value)
    assert "找不到配置文件" in message
    assert "governance.yaml" in message


# ---------------------------------------------------------------------------
# 校验：缺字段 / 错类型 / 未知字段 / 越界值
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def valid_raw() -> dict[str, Any]:
    return _load_yaml(GOVERNANCE_YAML)


def test_missing_required_field_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    del valid_raw["thresholds"]["complaint_window_days"]
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "缺少必填配置项" in message
    assert "thresholds.complaint_window_days" in message
    assert "SUP_HIGH_COMPLAINT" in message


def test_wrong_type_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    valid_raw["thresholds"]["freq_max_touches"] = "two"
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "类型不对" in message
    assert "thresholds.freq_max_touches" in message
    assert "整数" in message


def test_unknown_field_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    valid_raw["thresholds"]["my_own_threshold"] = 1
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "未定义的配置项" in message
    assert "thresholds.my_own_threshold" in message
    assert "SCHEMA" in message


def test_out_of_range_value_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    valid_raw["thresholds"]["refund_rate_threshold"] = 1.5
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "取值越界" in message
    assert "thresholds.refund_rate_threshold" in message


def test_disallowed_enum_value_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    valid_raw["autonomy"]["mvp_allowed_levels"] = ["L9"]
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "取值越界" in message
    assert "autonomy.mvp_allowed_levels" in message
    assert "L0" in message


def test_cross_field_inconsistency_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    valid_raw["segmentation"]["amount"]["mid_min_cents"] = 60000  # > high_min
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "金额档边界不自洽" in message
    assert "mid_min_cents" in message


# ---------------------------------------------------------------------------
# 红线保护：apply_overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "rule_id"),
    [
        ("thresholds.complaint_window_days", 30, "SUP_HIGH_COMPLAINT"),
        ("thresholds.refund_rate_threshold", 0.99, "SUP_HIGH_REFUND"),
        ("thresholds.refund_dispute_window_days", 7, "SUP_HIGH_REFUND"),
        ("thresholds.high_value_percentile", 50.0, "SUP_HIGH_VALUE"),
        ("thresholds.freq_window_days", 30, "FREQ_GLOBAL_7D"),
        ("thresholds.freq_max_touches", 99, "FREQ_GLOBAL_7D"),
        ("thresholds.channel_cooldown_hours", 1, "FREQ_GLOBAL_7D"),
        ("thresholds.conflict_window_hours", 1, "CONFLICT_WINDOW_72H"),
    ],
)
def test_redline_paths_are_rejected(path: str, value: Any, rule_id: str) -> None:
    nested = _nest({path: value})
    with pytest.raises(ConfigError) as excinfo:
        apply_overrides({"thresholds": {}}, nested)
    message = str(excinfo.value)
    assert "抑制红线" in message
    assert path in message
    assert rule_id in message
    assert "DENY 不可绕过" in message


def test_redline_rejection_mentions_all_five_rule_ids() -> None:
    """报错必须点名五条红线规则，且说明 SUP_EXPLICIT_REFUSAL / NO_CONSENT 无可覆盖入口。"""
    with pytest.raises(ConfigError) as excinfo:
        apply_overrides({"thresholds": {}}, _nest({"thresholds.complaint_window_days": 1}))
    message = str(excinfo.value)
    for rule_id in REDLINE_RULE_IDS:
        assert rule_id in message
    assert "没有任何可配置参数" in message


def test_path_outside_allowlist_is_rejected() -> None:
    """可覆盖范围是白名单：未登记的路径一律拒绝（含非红线路径）。"""
    with pytest.raises(ConfigError) as excinfo:
        apply_overrides({}, _nest({"budget.currency": "USD"}))
    message = str(excinfo.value)
    assert "不可覆盖的配置项" in message
    assert "budget.currency" in message


def test_overridable_path_is_applied() -> None:
    base = {"budget": {"per_task_token_limit": 500000}, "benefit": {"floor": 0}}
    merged = apply_overrides(base, _nest({"budget.per_task_token_limit": 1000}))
    assert merged["budget"]["per_task_token_limit"] == 1000
    assert merged["benefit"]["floor"] == 0
    assert base["budget"]["per_task_token_limit"] == 500000, "入参不得被修改"


# ---------------------------------------------------------------------------
# 回归：评审发现的 4 个 P1 缺陷（缺陷编号与评审报告一致）
# ---------------------------------------------------------------------------


def test_b1_empty_list_is_rejected(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    """B1：空列表曾被静默接受——`allowed` 检查用列表推导，空列表 → bad=[] → 不报错。"""
    valid_raw["autonomy"]["mvp_allowed_levels"] = []
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "元素太少" in message
    assert "autonomy.mvp_allowed_levels" in message


def test_b2_tie_break_order_must_match_frozen_value(
    tmp_path: Path, valid_raw: dict[str, Any]
) -> None:
    """B2：三键排序曾接受任意字符串——附录D 规则 3 的顺序可被悄悄改掉。"""
    valid_raw["conflict_resolution"]["tie_break_order"] = ["foo", "bar"]
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "冻结值" in message
    assert "platform_layer_score" in message


def test_b2_tie_break_order_rejects_reordering(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    """B2 续：三键元素对但顺序变了，同样必须拒绝。"""
    valid_raw["conflict_resolution"]["tie_break_order"] = [
        "benefit_net_value",
        "platform_layer_score",
        "created_at_earliest",
    ]
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    assert "冻结值" in str(excinfo.value)


def test_b3_missing_parent_key_raises_actionable_error() -> None:
    """B3：父键缺失曾抛裸 KeyError，调用方拿不到任何上下文。"""
    with pytest.raises(ConfigError) as excinfo:
        apply_overrides({}, _nest({"benefit.floor": 0}))
    message = str(excinfo.value)
    assert "找不到父级" in message
    assert "benefit" in message


def test_b4_empty_dict_override_is_not_silently_dropped() -> None:
    """B4：空 dict 覆盖曾被摊平成零个叶子，覆盖项凭空消失。"""
    base = {"conflict_resolution": {"platform_priority_overrides": {"taobao": 10}}}
    merged = apply_overrides(base, _nest({"conflict_resolution.platform_priority_overrides": {}}))
    assert merged["conflict_resolution"]["platform_priority_overrides"] == {}
    assert base["conflict_resolution"]["platform_priority_overrides"] == {"taobao": 10}, (
        "入参不得被修改"
    )


def test_redline_rule_ids_are_frozen() -> None:
    """五条红线 ID 与附录D §4 登记表一致；增删需同改基准并过回归门禁。"""
    assert REDLINE_RULE_IDS == (
        "SUP_HIGH_VALUE",
        "SUP_HIGH_COMPLAINT",
        "SUP_HIGH_REFUND",
        "SUP_EXPLICIT_REFUSAL",
        "NO_CONSENT",
    )
    assert set(REDLINE_PATHS.values()) <= set(REDLINE_RULE_IDS) | {
        "FREQ_GLOBAL_7D",
        "CONFLICT_WINDOW_72H",
    }


# ---------------------------------------------------------------------------
# 拍板③⑤（2026-10-26）：整值叶子 + 分层乘数锁死
# ---------------------------------------------------------------------------


def test_nonempty_platform_priority_overrides_loads(
    tmp_path: Path, valid_raw: dict[str, Any]
) -> None:
    """拍板③：非空 `platform_priority_overrides` 必须能被 `load_and_validate` 装进来。

    成因：`_flatten` 把非空映射递归摊平成逐条叶子 →
    `conflict_resolution.platform_priority_overrides.<平台>` 脱离 SCHEMA 登记 →
    被当成未知字段拒绝，规则 3 的平台优先级覆盖在生产路径上永远装不进来。
    """
    valid_raw["conflict_resolution"]["platform_priority_overrides"] = {
        "taobao": 10,
        "wechat_moments": 20,
    }
    config = load_and_validate(_write(tmp_path, valid_raw))
    assert config.conflict_resolution.platform_priority_overrides == {
        "taobao": 10,
        "wechat_moments": 20,
    }


def test_nonempty_platform_priority_overrides_override_is_allowlisted() -> None:
    """拍板③续：覆盖非空 `platform_priority_overrides` 必须落在白名单内，不得误拒。"""
    base = {"conflict_resolution": {"platform_priority_overrides": {"taobao": 10}}}
    merged = apply_overrides(
        base,
        _nest({"conflict_resolution.platform_priority_overrides": {"taobao": 5, "douyin": 1}}),
    )
    assert merged["conflict_resolution"]["platform_priority_overrides"] == {
        "taobao": 5,
        "douyin": 1,
    }


def test_layer_score_base_is_locked_to_ten(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    """拍板⑤：分层乘数锁死 10。

    附录D 规则 3 规定 `layer_score = amount_score × 10 + recency_score`，
    且数据库 CHECK `chk_customer_segment_layer_score` 硬编码 ×10
    （migrations/001_core_tables.sql:225）。配成别的值会在写库时被拒——
    必须在启动前拦住，不要等写库才炸。
    """
    valid_raw["segmentation"]["layer_score_base"] = 20
    with pytest.raises(ConfigError) as excinfo:
        load_and_validate(_write(tmp_path, valid_raw))
    message = str(excinfo.value)
    assert "segmentation.layer_score_base" in message
    assert "10" in message, f"错误信息必须给出唯一可选值 10，实际是：{message}"


def test_layer_score_base_ten_is_accepted(tmp_path: Path, valid_raw: dict[str, Any]) -> None:
    """拍板⑤续：出厂值 10 必须照常通过。"""
    valid_raw["segmentation"]["layer_score_base"] = 10
    assert load_and_validate(_write(tmp_path, valid_raw)).segmentation.layer_score_base == 10


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in flat.items():
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out
