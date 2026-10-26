"""九宫格分层引擎的单测（T-C2）。

权威依据：
- 附录 D 规则 3 —— ``layer_score = amount_score × 10 + recency_score``，取值 11..33，
  数值大者层级更高；层级比较用九宫格 9 层，不是三档。
- 附录 D 规则 4 —— 全局九宫格与平台局部九宫格是**两套独立层级体系**，各自按
  ``layer_score`` 比较，**不存在「全局层级恒大于平台层级」的固定序**。
- 裁决 #11 —— 全局 + 平台局部各一套分层。
- 阈值唯一来源 = ``governance.yaml`` 的 ``segmentation`` 段（代码里不得写死常量）。

纯函数单测：无 PG、无 LLM、无框架。
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.policy_kernel.config import Segmentation as SegmentationThresholds
from src.unification.segmentation import (
    SCOPE_GLOBAL,
    SCOPE_PLATFORM_LOCAL,
    SEGMENT_RULE_CODE,
    CustomerLayerMetrics,
    SegmentationError,
    compare_layer_scores,
    compute_customer_layers,
    days_since,
    layer_score_of,
    segment_customer,
    segment_global,
    segment_platform_local,
    thresholds_from_governance,
    to_cents,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
GOVERNANCE_YAML = ROOT / "governance.yaml"
SEGMENTATION_SOURCE = ROOT / "src" / "unification" / "segmentation.py"


@pytest.fixture
def thresholds() -> SegmentationThresholds:
    """仓库自带的 governance.yaml 阈值（策略参数唯一来源）。"""
    return thresholds_from_governance(GOVERNANCE_YAML)


# ---------------------------------------------------------------------------
# 一、layer_score 比较式与取值域（附录 D 规则 3）
# ---------------------------------------------------------------------------


def test_layer_score_formula_is_amount_times_ten_plus_recency(
    thresholds: SegmentationThresholds,
) -> None:
    """``layer_score == amount_score * 10 + recency_score``（附录 D 规则 3）。"""
    for amount_score in (1, 2, 3):
        for recency_score in (1, 2, 3):
            assert layer_score_of(amount_score, recency_score, thresholds) == (
                amount_score * 10 + recency_score
            )


def test_layer_score_range_is_11_to_33(thresholds: SegmentationThresholds) -> None:
    """取值域 11..33：9 个层级组合全覆盖（附录 D 规则 3「取值 11..33」）。"""
    scores = {
        layer_score_of(amount_score, recency_score, thresholds)
        for amount_score in (1, 2, 3)
        for recency_score in (1, 2, 3)
    }
    assert scores == {11, 12, 13, 21, 22, 23, 31, 32, 33}


def test_layer_score_uses_configured_base_not_a_hardcoded_ten(
    thresholds: SegmentationThresholds,
) -> None:
    """乘数来自配置（``segmentation.layer_score_base``），改配置即改行为。"""
    retuned = replace(thresholds, layer_score_base=100)
    assert layer_score_of(2, 3, retuned) == 203


# ---------------------------------------------------------------------------
# 二、九宫格 9 层边界用例（附录 D 规则 3 层级比较式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount_cents", "days", "amount_tier", "recency_tier", "expected_score"),
    [
        (60000, 10, "high", "recent", 33),
        (60000, 45, "high", "mid", 32),
        (60000, 120, "high", "stale", 31),
        (20000, 10, "mid", "recent", 23),
        (20000, 45, "mid", "mid", 22),
        (20000, 120, "mid", "stale", 21),
        (5000, 10, "low", "recent", 13),
        (5000, 45, "low", "mid", 12),
        (5000, 120, "low", "stale", 11),
    ],
)
def test_nine_grid_covers_all_nine_combinations(
    thresholds: SegmentationThresholds,
    amount_cents: int,
    days: int,
    amount_tier: str,
    recency_tier: str,
    expected_score: int,
) -> None:
    """九宫格 9 层每层都有可判定的边界用例（金额 3 档 × 近度 3 档）。"""
    segment = segment_global(
        CustomerLayerMetrics(avg_order_value_cents=amount_cents, days_since_last_purchase=days),
        thresholds,
    )
    assert segment.amount_tier == amount_tier
    assert segment.recency_tier == recency_tier
    assert segment.layer_score == expected_score
    assert segment.scope == SCOPE_GLOBAL
    assert segment.platform is None


@pytest.mark.parametrize(
    ("amount_cents", "expected_tier"),
    [(50000, "high"), (49999, "mid"), (10000, "mid"), (9999, "low")],
)
def test_amount_tier_boundaries_follow_config(
    thresholds: SegmentationThresholds, amount_cents: int, expected_tier: str
) -> None:
    """金额档边界：≥500 元 = high，[100,500) = mid，<100 元 = low。"""
    segment = segment_global(
        CustomerLayerMetrics(avg_order_value_cents=amount_cents, days_since_last_purchase=1),
        thresholds,
    )
    assert segment.amount_tier == expected_tier


@pytest.mark.parametrize(
    ("days", "expected_tier"),
    [(0, "recent"), (30, "recent"), (31, "mid"), (90, "mid"), (91, "stale")],
)
def test_recency_tier_boundaries_follow_config(
    thresholds: SegmentationThresholds, days: int, expected_tier: str
) -> None:
    """近度档边界：≤30 天 = recent，(30,90] = mid，>90 天 = stale。"""
    segment = segment_global(
        CustomerLayerMetrics(avg_order_value_cents=1000, days_since_last_purchase=days),
        thresholds,
    )
    assert segment.recency_tier == expected_tier


def test_layer_name_uses_the_frozen_benchmark_vocabulary(
    thresholds: SegmentationThresholds,
) -> None:
    """层名与评测标注基准的 ``global_segment`` 字段同口径（如 ``amount_mid_recent``）。"""
    segment = segment_global(
        CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
        thresholds,
    )
    assert segment.layer_name == "amount_mid_recent"


# ---------------------------------------------------------------------------
# 三、全局层与平台局部层是两套独立体系（附录 D 规则 4 更正说明）
# ---------------------------------------------------------------------------


def test_global_and_platform_local_are_independent_layers(
    thresholds: SegmentationThresholds,
) -> None:
    """两套层级独立产出：全局高 / 平台低 与 全局低 / 平台高 两种方向都要成立。

    附录 D 规则 4 已删除「全局层级 > 平台层级」的固定序——哪套更高取决于 score。
    """
    layers = compute_customer_layers(
        CustomerLayerMetrics(avg_order_value_cents=60000, days_since_last_purchase=10),
        {"douyin": CustomerLayerMetrics(avg_order_value_cents=5000, days_since_last_purchase=200)},
        thresholds,
    )
    assert layers.global_segment.layer_score == 33
    assert layers.platform_segments["douyin"].layer_score == 11
    assert compare_layer_scores(layers.global_segment, layers.platform_segments["douyin"]) > 0

    reversed_layers = compute_customer_layers(
        CustomerLayerMetrics(avg_order_value_cents=5000, days_since_last_purchase=200),
        {"taobao": CustomerLayerMetrics(avg_order_value_cents=60000, days_since_last_purchase=10)},
        thresholds,
    )
    assert reversed_layers.global_segment.layer_score == 11
    assert reversed_layers.platform_segments["taobao"].layer_score == 33
    assert (
        compare_layer_scores(
            reversed_layers.global_segment, reversed_layers.platform_segments["taobao"]
        )
        < 0
    ), "平台局部层分数更高时，全局层不得被默认判为更高（附录 D 规则 4 无固定序）"


def test_equal_scores_compare_as_tie(thresholds: SegmentationThresholds) -> None:
    """两套体系分数相同时比较结果为平局（0），不按 scope 判胜负。"""
    metrics = CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20)
    layers = compute_customer_layers(metrics, {"taobao": metrics}, thresholds)
    assert compare_layer_scores(layers.global_segment, layers.platform_segments["taobao"]) == 0


def test_platform_local_segment_carries_platform(thresholds: SegmentationThresholds) -> None:
    """``platform_local`` 必须带 platform（对齐 CHECK ``chk_customer_segment_scope_platform``）。"""
    segment = segment_platform_local(
        CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
        thresholds,
        "taobao",
    )
    assert segment.scope == SCOPE_PLATFORM_LOCAL
    assert segment.platform == "taobao"


def test_platform_local_without_platform_is_rejected(thresholds: SegmentationThresholds) -> None:
    """``platform_local`` 缺 platform 必须报错并说清怎么改（fail-closed）。"""
    with pytest.raises(SegmentationError) as excinfo:
        segment_customer(
            CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
            thresholds,
            scope=SCOPE_PLATFORM_LOCAL,
            platform=None,
        )
    message = str(excinfo.value)
    assert "platform" in message
    assert SCOPE_PLATFORM_LOCAL in message


def test_global_scope_must_not_carry_platform(thresholds: SegmentationThresholds) -> None:
    """全局层不带 platform——全局层是客户级属性（附录 D 规则 3 键①澄清）。"""
    with pytest.raises(SegmentationError) as excinfo:
        segment_customer(
            CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
            thresholds,
            scope=SCOPE_GLOBAL,
            platform="taobao",
        )
    assert "platform" in str(excinfo.value)


def test_unknown_scope_is_rejected(thresholds: SegmentationThresholds) -> None:
    """未知 scope 一律拒绝（fail-closed），错误信息给出可选值。"""
    with pytest.raises(SegmentationError) as excinfo:
        segment_customer(
            CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
            thresholds,
            scope="unknown_scope",
        )
    message = str(excinfo.value)
    assert SCOPE_GLOBAL in message
    assert SCOPE_PLATFORM_LOCAL in message


def test_segment_rule_code_is_frozen(thresholds: SegmentationThresholds) -> None:
    """分段规则码固定为 NINE_GRID（表默认值 / 迁移一致）。"""
    segment = segment_global(
        CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
        thresholds,
    )
    assert segment.segment_rule_code == SEGMENT_RULE_CODE == "NINE_GRID"


# ---------------------------------------------------------------------------
# 四、阈值来自 governance.yaml（唯一来源，代码里不写死）
# ---------------------------------------------------------------------------


def test_thresholds_come_from_governance_config(thresholds: SegmentationThresholds) -> None:
    """阈值必须读自 governance.yaml，且与冻结口径一致（500 元 / 100 元 / 30 天 / 90 天）。"""
    assert thresholds.amount_high_min_cents == 50000
    assert thresholds.amount_mid_min_cents == 10000
    assert thresholds.recency_recent_max_days == 30
    assert thresholds.recency_mid_max_days == 90
    assert thresholds.layer_score_base == 10


def test_changing_configured_threshold_changes_behavior(thresholds: SegmentationThresholds) -> None:
    """改配置即改行为：把 high 下界从 500 元调到 300 元，同一输入换档。"""
    metrics = CustomerLayerMetrics(avg_order_value_cents=40000, days_since_last_purchase=10)
    assert segment_global(metrics, thresholds).amount_tier == "mid"

    retuned = replace(thresholds, amount_high_min_cents=30000)
    assert segment_global(metrics, retuned).amount_tier == "high"


def test_segmentation_module_does_not_hardcode_thresholds() -> None:
    """模块源码里不得出现阈值常量（AST 常量扫描）——阈值只来自 governance.yaml。"""
    tree = ast.parse(SEGMENTATION_SOURCE.read_text(encoding="utf-8"))
    # 阈值常量（元→分后的分值 + 天数）；「1 元 = 100 分」是单位换算系数，不在禁列。
    banned = {50000, 10000, 500, 30, 90}
    found: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and node.value in banned
        ):
            found.append(node.value)
    assert found == [], f"分层阈值不得写死在代码里，发现常量：{sorted(set(found))}"


def test_segmentation_module_is_free_of_database_and_framework_imports() -> None:
    """分层算法是纯函数：不得 import ORM / 驱动 / 框架（落库在 segment_store.py）。"""
    source = SEGMENTATION_SOURCE.read_text(encoding="utf-8")
    for banned in ("sqlalchemy", "psycopg", "src.models", "fastapi", "litellm"):
        assert banned not in source, f"分层纯函数模块不得依赖 {banned}"


def test_unification_package_init_does_not_pull_database_modules() -> None:
    """包 ``__init__`` 不得 import 落库模块——否则策略内核会连带引入 ORM（红线 1）。"""
    source = (ROOT / "src" / "unification" / "__init__.py").read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            imported.append(node.module or "")
    assert imported == [], (
        f"src/unification/__init__.py 不得有 import（会让 ORM 进内核）：{imported}"
    )


# ---------------------------------------------------------------------------
# 五、输入度量工具
# ---------------------------------------------------------------------------


def test_to_cents_converts_yuan_to_cents() -> None:
    """客单价以「分」进分层（governance.yaml 阈值单位 = 分）。"""
    assert to_cents(Decimal("120.00")) == 12000
    assert to_cents(Decimal("120.05")) == 12005
    assert to_cents(Decimal("0")) == 0


def test_to_cents_rejects_negative_amount() -> None:
    """负客单价是数据错误，直接拒绝（fail-closed）。"""
    with pytest.raises(SegmentationError):
        to_cents(Decimal("-1.00"))


def test_days_since_counts_whole_elapsed_days() -> None:
    """距今天数按整天向下取整（近度档 30/31/90/91 的判定基准）。"""
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    assert days_since(now - timedelta(days=30), now) == 30
    assert days_since(now - timedelta(days=30, hours=23), now) == 30
    assert days_since(now - timedelta(days=31), now) == 31


def test_days_since_rejects_future_timestamp() -> None:
    """购买时间在未来 = 数据错误，拒绝而不是静默当成 0 天。"""
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    with pytest.raises(SegmentationError):
        days_since(now + timedelta(days=1), now)
