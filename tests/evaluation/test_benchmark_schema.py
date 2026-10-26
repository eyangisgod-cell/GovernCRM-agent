"""评测标注基准夹具的 schema 校验与防漂移守卫（T-G1）。

依据（每个测试的 docstring 都标注了可反查的权威条款）：

- 《GovernCRM_附录D_冻结版.md》§3「评测集标注基准（冻结版）」
  —— 样本构成 15 简单 + 5 边界 = 20；CI 回放对照一致率须 100%
- 《GovernCRM_附录D_冻结版.md》§4「规则 ID 登记表（唯一权威）」
  —— 11 个 rule_id；**禁止自造 ID**
- 《GovernCRM_技术架构方案_v5.2.md》§4.3 / §4.4-9 / §4.4-10
  —— 路径完整性断言：必经节点缺失即 DENY（E14）；``REQUIRED_NODES ⊇
  {confirm_plan, validate, compliance, guard}``
- 《MVP_任务拆解.md》§三【F】T-G1
  —— 本任务只出**夹具 + schema 守卫**；CI 回放对照属 T-G2（Wave 4），
  路径断言实现属 T-E4（Wave 3）

红线（不可破）：

- **禁止自造 rule_id**：本模块以《附录D §4》表格为唯一权威，与
  ``src/models/rule_ids.py`` 静态镜像三方比对，任何一处漂移即红。
- **标注纪律**：期望值来自人工标注（v1.3 已由 eyangisgod 于 2026-10-25 逐条拍板），
  **不得**由内核输出反推。本模块**不导入** ``src.policy_kernel`` 的任何判定逻辑。
- **路径断言样本独立验收**：不计入一致率分母（红线④：路径断言不可 opt-in）。

纯函数测试：无 PG、无 LLM、无框架依赖。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.models.rule_ids import REGISTERED_RULE_IDS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "tests" / "evaluation"
BENCHMARK_YAML = EVAL_DIR / "benchmark.yaml"
PATH_ASSERTIONS_YAML = EVAL_DIR / "path_assertions.yaml"
FROZEN_BASELINE_YAML = ROOT / "docs" / "specs" / "评测标注基准_v1.3.yaml"
APPENDIX_D_MD = ROOT / "docs" / "specs" / "GovernCRM_附录D_冻结版.md"
GOVERNANCE_YAML = ROOT / "governance.yaml"

#: 附录D §3 冻结的样本构成：15 简单 + 5 边界 = 20（一致率分母）
EXPECTED_TOTAL_SAMPLES = 20
EXPECTED_SIMPLE_SAMPLES = 15
EXPECTED_BOUNDARY_SAMPLES = 5

#: 架构 v5.2 §4.3 / §4.4-9：必经节点（路径断言样本只能跳这些节点）
REQUIRED_NODES = ("confirm_plan", "validate", "compliance", "guard")

#: 附录D §1：合法判定取值
VALID_CONCLUSIONS = ("ALLOW", "REQUIRE_APPROVAL", "DENY")

#: 附录D §4 登记的 rule_id 数量（登记表是唯一权威，11 条）
EXPECTED_REGISTERED_RULE_ID_COUNT = 11

#: benchmark.yaml 允许出现的顶层块（与冻结件一一对应）
EXPECTED_TOP_LEVEL_BLOCKS = ("layers", "net_value", "thresholds", "now", "benchmarks", "meta")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 夹具；文件缺失或顶层非映射 → pytest.fail（可操作，不静默跳过）。"""
    if not path.exists():
        pytest.fail(
            f"评测夹具缺失：{path} 不存在。\n"
            f"  该文件应从 {FROZEN_BASELINE_YAML} 逐字抄录（禁止手改数值）。\n"
            "  夹具缺失 = 门禁失效——请补齐后重跑，不要 skip。"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        pytest.fail(f"{path} 顶层必须是映射，实际是 {type(data).__name__}")
    return data


def _registered_rule_ids_from_appendix_d() -> frozenset[str]:
    """从《附录D §4 规则 ID 登记表》表格解析权威 rule_id 集合。

    表格行形如：``| `SUP_HIGH_VALUE` | 规则 1a | DENY + 转人工 | #13 / FR-18 |``。
    以文档表格（而非代码镜像）为权威，才能真正挡住「自造 rule_id」。
    """
    text = APPENDIX_D_MD.read_text(encoding="utf-8")
    parts = text.split("## 4. 规则 ID 登记表", 1)
    assert len(parts) == 2, "《附录D_冻结版.md》找不到 §4「规则 ID 登记表」小节"
    body = parts[1].split("\n## ", 1)[0]
    rule_ids = set(re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|", body, flags=re.MULTILINE))
    assert rule_ids, "《附录D §4》表格里没解析出任何 rule_id"
    return frozenset(rule_ids)


def _samples(data: dict[str, Any]) -> list[dict[str, Any]]:
    """取出 benchmarks 列表并校验条目类型。"""
    samples = data.get("benchmarks")
    if not isinstance(samples, list):
        pytest.fail("benchmark.yaml 的 benchmarks 块必须是列表")
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            pytest.fail(f"benchmarks 第 {index} 条必须是映射，实际是 {type(sample).__name__}")
    return list(samples)


def _rule_ids_of(sample: dict[str, Any]) -> list[str]:
    """取一条样本 expected 块里出现的全部 rule_id（命中 + 被抑制）。"""
    expected = sample.get("expected", {})
    if not isinstance(expected, dict):
        return []
    hit = expected.get("hit_rule_ids", [])
    suppressed = expected.get("suppressed_rule_ids", [])
    rule_ids = list(hit) if isinstance(hit, list) else []
    rule_ids += list(suppressed) if isinstance(suppressed, list) else []
    return [str(rule_id) for rule_id in rule_ids]


def _validate_strategy_sample(sample: Any, registry: frozenset[str]) -> None:
    """校验单条策略样本的 expected 块；不合规 → AssertionError（信息须可操作）。"""
    assert isinstance(sample, dict), f"样本条目必须是映射，实际 {type(sample).__name__}"
    sample_id = str(sample.get("id", "<缺少 id>"))

    expected = sample.get("expected")
    assert isinstance(expected, dict), (
        f"样本 {sample_id}: 缺少 expected 块（判定期望值必须显式给出）"
    )

    conclusion = expected.get("conclusion")
    assert conclusion in VALID_CONCLUSIONS, (
        f"样本 {sample_id}: expected.conclusion = {conclusion!r} 非法，"
        f"必须是 {VALID_CONCLUSIONS} 之一（附录D §1 判定枚举）"
    )

    hit = expected.get("hit_rule_ids")
    assert isinstance(hit, list), f"样本 {sample_id}: expected.hit_rule_ids 缺失或不是列表"
    for rule_id in hit:
        assert rule_id in registry, (
            f"样本 {sample_id}: rule_id {rule_id!r} 不在附录D §4 登记表内 —— 禁止自造 rule_id"
        )

    suppressed = expected.get("suppressed_rule_ids", [])
    assert isinstance(suppressed, list), f"样本 {sample_id}: expected.suppressed_rule_ids 不是列表"
    for rule_id in suppressed:
        assert rule_id in registry, (
            f"样本 {sample_id}: suppressed rule_id {rule_id!r} 不在附录D §4 登记表内 —— 禁止自造 rule_id"
        )

    count = expected.get("suppressed_candidates", 0)
    assert isinstance(count, int), (
        f"样本 {sample_id}: expected.suppressed_candidates 必须是整数，实际 {count!r}"
    )
    assert not isinstance(count, bool), (
        f"样本 {sample_id}: expected.suppressed_candidates 不能是布尔值，实际 {count!r}"
    )
    assert count >= 0, f"样本 {sample_id}: expected.suppressed_candidates 不能为负（{count}）"


def _validate_path_sample(sample: Any, required_nodes: frozenset[str]) -> None:
    """校验单条路径断言样本；不合规 → AssertionError（信息须可操作）。"""
    assert isinstance(sample, dict), f"样本条目必须是映射，实际 {type(sample).__name__}"
    sample_id = str(sample.get("id", "<缺少 id>"))

    skipped = sample.get("skipped_nodes")
    assert isinstance(skipped, list), f"样本 {sample_id}: skipped_nodes 缺失或不是列表"
    assert skipped, f"样本 {sample_id}: skipped_nodes 必须是非空列表"
    for node in skipped:
        assert node in required_nodes, (
            f"样本 {sample_id}: 跳过的节点 {node!r} 不在必经节点集合 {sorted(required_nodes)} 内"
        )

    visited = sample.get("visited_nodes")
    assert isinstance(visited, list), f"样本 {sample_id}: visited_nodes 必须是列表"
    assert len(set(visited)) == len(visited), f"样本 {sample_id}: visited_nodes 有重复节点"
    assert set(visited) == required_nodes - set(skipped), (
        f"样本 {sample_id}: visited_nodes 与 skipped_nodes 不自洽 —— "
        f"期望 {sorted(required_nodes - set(skipped))}，实际 {sorted(visited)}"
    )

    expected = sample.get("expected")
    assert isinstance(expected, dict), f"样本 {sample_id}: 缺少 expected 块"

    conclusion = expected.get("conclusion")
    assert conclusion == "DENY", (
        f"样本 {sample_id}: 路径断言的期望 conclusion 必须是 DENY，实际 {conclusion!r}"
        "（架构 v5.2 §4.4-9：缺失必经节点即 DENY + 告警 E14）"
    )

    hit = expected.get("hit_rule_ids")
    assert isinstance(hit, list), f"样本 {sample_id}: expected.hit_rule_ids 缺失或不是列表"
    assert "PATH_ASSERT" in hit, (
        f"样本 {sample_id}: expected.hit_rule_ids 必须含 PATH_ASSERT，实际 {hit}"
    )

    missing = expected.get("missing_nodes")
    assert isinstance(missing, list), f"样本 {sample_id}: expected.missing_nodes 缺失或不是列表"
    assert sorted(missing) == sorted(skipped), (
        f"样本 {sample_id}: expected.missing_nodes {sorted(missing)} 与 skipped_nodes "
        f"{sorted(skipped)} 不一致（缺失节点报错须点名全部缺失节点）"
    )


# ---------------------------------------------------------------------------
# 一、夹具存在且可解析
# ---------------------------------------------------------------------------


def test_benchmark_fixture_exists_and_loads() -> None:
    """benchmark.yaml 必须存在且可解析（缺失即门禁失效，不得静默跳过）。"""
    data = _load_yaml(BENCHMARK_YAML)
    assert "benchmarks" in data, "benchmark.yaml 缺少 benchmarks 块（附录D §3 样本构成）"


def test_path_assertions_fixture_exists_and_loads() -> None:
    """path_assertions.yaml 必须存在且可解析（路径断言不可 opt-in）。"""
    data = _load_yaml(PATH_ASSERTIONS_YAML)
    assert "samples" in data, "path_assertions.yaml 缺少 samples 块"


# ---------------------------------------------------------------------------
# 二、样本构成（附录D §3）
# ---------------------------------------------------------------------------


def test_benchmark_has_twenty_strategy_samples() -> None:
    """附录D §3：15 简单 + 5 边界 = 20 条策略样本，一致率分母 = 这 20 条。"""
    data = _load_yaml(BENCHMARK_YAML)
    samples = _samples(data)
    assert len(samples) == EXPECTED_TOTAL_SAMPLES, (
        f"策略样本数应为 {EXPECTED_TOTAL_SAMPLES}，实际 {len(samples)}（附录D §3 样本构成）"
    )

    ids = [str(sample.get("id")) for sample in samples]
    assert len(set(ids)) == len(ids), f"样本 id 重复：{ids}"

    simple = [sample_id for sample_id in ids if sample_id.startswith("S")]
    boundary = [sample_id for sample_id in ids if sample_id.startswith("B")]
    assert len(simple) == EXPECTED_SIMPLE_SAMPLES, (
        f"简单样本应为 {EXPECTED_SIMPLE_SAMPLES} 条，实际 {len(simple)}：{simple}"
    )
    assert len(boundary) == EXPECTED_BOUNDARY_SAMPLES, (
        f"边界样本应为 {EXPECTED_BOUNDARY_SAMPLES} 条，实际 {len(boundary)}：{boundary}"
    )

    meta = data.get("meta", {})
    assert isinstance(meta, dict), "benchmark.yaml 的 meta 块必须是映射"
    assert meta.get("total_samples") == EXPECTED_TOTAL_SAMPLES, "meta.total_samples 与 §3 不符"
    assert meta.get("simple_samples") == EXPECTED_SIMPLE_SAMPLES, "meta.simple_samples 与 §3 不符"
    assert meta.get("boundary_samples") == EXPECTED_BOUNDARY_SAMPLES, (
        "meta.boundary_samples 与 §3 不符"
    )


def test_expected_block_has_three_comparison_elements() -> None:
    """附录D §3 对照口径三要素：conclusion / hit_rule_ids / suppressed_rule_ids + 数量。

    冻结件只对**涉及多候选**的样本显式写 ``suppressed_rule_ids`` 与
    ``suppressed_candidates``；单候选样本缺省按「空集 / 0」归一
    （见 ``meta.comparison_rule`` 的缺省语义）。多候选样本必须显式写全——
    少写会让「另一候选被抑制」在回放时不可比对。
    """
    data = _load_yaml(BENCHMARK_YAML)
    registry = _registered_rule_ids_from_appendix_d()
    for sample in _samples(data):
        _validate_strategy_sample(sample, registry)
        expected = sample["expected"]
        candidates = sample.get("candidates", [])
        if isinstance(candidates, list) and len(candidates) >= 2:
            assert "suppressed_rule_ids" in expected, (
                f"样本 {sample['id']}: 多候选样本必须显式声明 suppressed_rule_ids"
            )
            assert "suppressed_candidates" in expected, (
                f"样本 {sample['id']}: 多候选样本必须显式声明 suppressed_candidates"
            )


def test_single_candidate_samples_default_to_empty_suppression() -> None:
    """单候选样本的 suppressed_* 缺省 = 空集 / 0（冻结件现状，回放时按缺省归一）。"""
    data = _load_yaml(BENCHMARK_YAML)
    for sample in _samples(data):
        candidates = sample.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) >= 2:
            continue
        expected = sample.get("expected", {})
        assert isinstance(expected, dict), f"样本 {sample.get('id')}: 缺少 expected 块"
        assert expected.get("suppressed_rule_ids", []) == [], (
            f"样本 {sample['id']}: 单候选样本不应声明非空 suppressed_rule_ids"
        )
        assert expected.get("suppressed_candidates", 0) == 0, (
            f"样本 {sample['id']}: 单候选样本的 suppressed_candidates 应为 0"
        )


# ---------------------------------------------------------------------------
# 三、rule_id 登记（附录D §4）
# ---------------------------------------------------------------------------


def test_rule_id_registry_matches_frozen_appendix_d() -> None:
    """《附录D §4》表格 ↔ ``src/models/rule_ids.py`` 静态镜像必须一致。

    文档表格是唯一权威；代码镜像只是它的投影。两者漂移即说明有人单改了一处。
    """
    authoritative = _registered_rule_ids_from_appendix_d()
    mirror = frozenset(REGISTERED_RULE_IDS)
    assert mirror == authoritative, (
        "rule_id 登记表漂移：src/models/rule_ids.py 与《附录D §4》不一致。\n"
        f"  仅在文档中：{sorted(authoritative - mirror)}\n"
        f"  仅在代码中：{sorted(mirror - authoritative)}\n"
        "  登记表是唯一权威，增删须同改两处并过回归门禁。"
    )
    assert len(authoritative) == EXPECTED_REGISTERED_RULE_ID_COUNT, (
        f"附录D §4 应登记 {EXPECTED_REGISTERED_RULE_ID_COUNT} 个 rule_id，"
        f"实际 {len(authoritative)}：{sorted(authoritative)}"
    )


def test_every_rule_id_is_registered() -> None:
    """附录D §4：评测集里出现的每个 rule_id 都必须在登记表内（禁止自造 ID）。"""
    data = _load_yaml(BENCHMARK_YAML)
    registry = _registered_rule_ids_from_appendix_d()
    for sample in _samples(data):
        for rule_id in _rule_ids_of(sample):
            assert rule_id in registry, (
                f"样本 {sample.get('id')}: rule_id {rule_id!r} 不在附录D §4 登记表内 —— "
                f"禁止自造 rule_id；合法值：{sorted(registry)}"
            )


# ---------------------------------------------------------------------------
# 四、阈值一致性（governance.yaml ↔ 标注基准）
# ---------------------------------------------------------------------------


def test_thresholds_match_governance_yaml() -> None:
    """governance.yaml 与 benchmark.yaml 的 ``thresholds`` 块逐项相等（防两处漂移）。"""
    benchmark_thresholds = _load_yaml(BENCHMARK_YAML).get("thresholds")
    governance_thresholds = _load_yaml(GOVERNANCE_YAML).get("thresholds")
    assert isinstance(benchmark_thresholds, dict), "benchmark.yaml 缺少 thresholds 块"
    assert isinstance(governance_thresholds, dict), "governance.yaml 缺少 thresholds 块"
    assert governance_thresholds == benchmark_thresholds, (
        "thresholds 块漂移：governance.yaml 与 benchmark.yaml 不一致。\n"
        f"  governance.yaml  = {governance_thresholds}\n"
        f"  benchmark.yaml   = {benchmark_thresholds}\n"
        "  两处必须同改（红线阈值走「配置变更审批 + 回归门禁」），不允许单改一处。"
    )


# ---------------------------------------------------------------------------
# 五、防漂移：benchmark.yaml 必须与冻结件语义相等
# ---------------------------------------------------------------------------


def test_benchmark_matches_frozen_source() -> None:
    """benchmark.yaml 与《评测标注基准_v1.3.yaml》语义相等（防标注被悄悄改）。

    标注件由 eyangisgod 逐条拍板冻结；任何改动必须先走「改规则 → 审批 →
    回归门禁 → rule_version 递增 → 写审计」，不能由夹具单方面改动。
    """
    frozen = _load_yaml(FROZEN_BASELINE_YAML)
    benchmark = _load_yaml(BENCHMARK_YAML)
    assert set(benchmark) == set(frozen), (
        f"benchmark.yaml 顶层块与冻结件不一致 —— 期望 {sorted(frozen)}，实际 {sorted(benchmark)}"
    )
    for block in EXPECTED_TOP_LEVEL_BLOCKS:
        assert block in frozen, f"冻结件缺少 {block} 块"
        assert block in benchmark, f"benchmark.yaml 缺少 {block} 块"
        assert benchmark[block] == frozen[block], (
            f"benchmark.yaml 的 {block} 块与冻结件《评测标注基准_v1.3.yaml》不一致 —— "
            "标注件只能由 eyangisgod 拍板后同步，禁止手改（附录D §3 标注纪律）"
        )


# ---------------------------------------------------------------------------
# 六、路径断言样本（独立验收，不计入一致率分母）
# ---------------------------------------------------------------------------


def test_path_assertions_have_five_samples() -> None:
    """T-G1：5 条路径断言样本，各跳一个不同的必经节点，期望 DENY + PATH_ASSERT。"""
    data = _load_yaml(PATH_ASSERTIONS_YAML)
    required = data.get("required_nodes")
    assert isinstance(required, list), "path_assertions.yaml 缺少 required_nodes 块"
    required_nodes = frozenset(str(node) for node in required)
    assert required_nodes >= set(REQUIRED_NODES), (
        f"required_nodes 必须覆盖架构 v5.2 §4.3 的必经节点 {list(REQUIRED_NODES)}，"
        f"实际 {sorted(required_nodes)}"
    )

    samples = data.get("samples")
    assert isinstance(samples, list), "path_assertions.yaml 的 samples 块必须是列表"
    assert len(samples) == 5, f"路径断言样本应为 5 条，实际 {len(samples)}"

    by_id: dict[str, dict[str, Any]] = {}
    for sample in samples:
        assert isinstance(sample, dict), f"样本条目必须是映射，实际 {type(sample).__name__}"
        sample_id = str(sample.get("id"))
        assert sample_id not in by_id, f"路径断言样本 id 重复：{sample_id}"
        by_id[sample_id] = sample
        _validate_path_sample(sample, required_nodes)

    assert set(by_id) == {"PA01", "PA02", "PA03", "PA04", "PA05"}, (
        f"路径断言样本 id 应为 PA01–PA05，实际 {sorted(by_id)}"
    )

    first_four_skips = [
        tuple(by_id[key]["skipped_nodes"]) for key in ("PA01", "PA02", "PA03", "PA04")
    ]
    assert all(len(skip) == 1 for skip in first_four_skips), (
        f"PA01–PA04 每条必须恰好跳过一个必经节点，实际 {first_four_skips}"
    )
    assert len(set(first_four_skips)) == 4, (
        f"PA01–PA04 必须各跳一个**不同**的必经节点，实际 {first_four_skips}"
    )
    covered = {node for skip in first_four_skips for node in skip}
    assert covered == set(REQUIRED_NODES), (
        f"PA01–PA04 合计必须覆盖全部必经节点 {list(REQUIRED_NODES)}，实际 {sorted(covered)}"
    )

    multi = by_id["PA05"]["skipped_nodes"]
    assert isinstance(multi, list), "PA05 的 skipped_nodes 缺失或不是列表"
    assert len(multi) >= 2, f"PA05 必须覆盖多节点缺失场景，实际跳过 {multi}"


# ---------------------------------------------------------------------------
# 七、负向用例（不许 skip：夹具/字段/rule_id 有问题时必须显式失败）
# ---------------------------------------------------------------------------


def test_missing_benchmark_file_is_reported_as_actionable_failure() -> None:
    """夹具文件缺失时必须报出可操作信息，而不是静默跳过（可跳过的门等于没有门）。"""
    with pytest.raises(pytest.fail.Exception, match="评测夹具缺失"):
        _load_yaml(EVAL_DIR / "does-not-exist.yaml")


def test_sample_without_conclusion_is_rejected() -> None:
    """expected 块缺 conclusion → 拒绝，并点名缺哪个字段。"""
    registry = _registered_rule_ids_from_appendix_d()
    with pytest.raises(AssertionError, match="conclusion"):
        _validate_strategy_sample({"id": "X01", "expected": {"hit_rule_ids": []}}, registry)


def test_sample_without_hit_rule_ids_is_rejected() -> None:
    """expected 块缺 hit_rule_ids → 拒绝，并点名缺哪个字段。"""
    registry = _registered_rule_ids_from_appendix_d()
    with pytest.raises(AssertionError, match="hit_rule_ids"):
        _validate_strategy_sample({"id": "X02", "expected": {"conclusion": "ALLOW"}}, registry)


def test_unregistered_rule_id_is_rejected() -> None:
    """自造 rule_id → 拒绝，并点名该 rule_id 与「禁止自造」字样。"""
    registry = _registered_rule_ids_from_appendix_d()
    sample = {"id": "X03", "expected": {"conclusion": "DENY", "hit_rule_ids": ["MY_OWN_RULE"]}}
    with pytest.raises(AssertionError, match="禁止自造 rule_id"):
        _validate_strategy_sample(sample, registry)
    with pytest.raises(AssertionError, match="MY_OWN_RULE"):
        _validate_strategy_sample(sample, registry)


def test_negative_suppressed_candidates_is_rejected() -> None:
    """suppressed_candidates 为负 → 拒绝（被抑制候选数不可能为负）。"""
    registry = _registered_rule_ids_from_appendix_d()
    sample = {
        "id": "X04",
        "expected": {"conclusion": "ALLOW", "hit_rule_ids": [], "suppressed_candidates": -1},
    }
    with pytest.raises(AssertionError, match="suppressed_candidates"):
        _validate_strategy_sample(sample, registry)


def test_path_sample_without_deny_conclusion_is_rejected() -> None:
    """路径断言样本的 conclusion 不是 DENY → 拒绝（缺必经节点必须 DENY + E14 告警）。"""
    required = frozenset(REQUIRED_NODES)
    sample = {
        "id": "PX01",
        "skipped_nodes": ["confirm_plan"],
        "visited_nodes": ["validate", "compliance", "guard"],
        "expected": {"conclusion": "ALLOW", "hit_rule_ids": [], "missing_nodes": ["confirm_plan"]},
    }
    with pytest.raises(AssertionError, match="DENY"):
        _validate_path_sample(sample, required)


def test_path_sample_without_path_assert_rule_id_is_rejected() -> None:
    """路径断言样本的 hit_rule_ids 缺 PATH_ASSERT → 拒绝（登记表内的唯一路径断言 ID）。"""
    required = frozenset(REQUIRED_NODES)
    sample = {
        "id": "PX02",
        "skipped_nodes": ["validate"],
        "visited_nodes": ["confirm_plan", "compliance", "guard"],
        "expected": {"conclusion": "DENY", "hit_rule_ids": [], "missing_nodes": ["validate"]},
    }
    with pytest.raises(AssertionError, match="PATH_ASSERT"):
        _validate_path_sample(sample, required)


def test_path_sample_skipping_unknown_node_is_rejected() -> None:
    """跳过不在必经节点集合内的节点 → 拒绝（样本必须针对真实必经节点）。"""
    required = frozenset(REQUIRED_NODES)
    sample = {
        "id": "PX03",
        "skipped_nodes": ["reflect"],
        "visited_nodes": ["confirm_plan", "validate", "compliance", "guard"],
        "expected": {
            "conclusion": "DENY",
            "hit_rule_ids": ["PATH_ASSERT"],
            "missing_nodes": ["reflect"],
        },
    }
    with pytest.raises(AssertionError, match="reflect"):
        _validate_path_sample(sample, required)


def test_path_sample_with_inconsistent_trace_is_rejected() -> None:
    """visited_nodes 与 skipped_nodes 不自洽 → 拒绝（trace 必须真实反映跳节点）。"""
    required = frozenset(REQUIRED_NODES)
    sample = {
        "id": "PX04",
        "skipped_nodes": ["confirm_plan"],
        "visited_nodes": ["validate", "compliance", "guard", "confirm_plan"],
        "expected": {
            "conclusion": "DENY",
            "hit_rule_ids": ["PATH_ASSERT"],
            "missing_nodes": ["confirm_plan"],
        },
    }
    with pytest.raises(AssertionError, match="不自洽"):
        _validate_path_sample(sample, required)
