"""T-E4 路径完整性断言（纯函数）验收用例。

权威依据：

- **PRD v5.1:514（E14）**：路径完整性断言失败（trace 缺失必经节点）一律 DENY + 告警，
  该动作进人工审查。
- **架构 v5.2:272**：``missing = REQUIRED_NODES - set(ctx.visited_nodes)``；非空 → DENY。
- **附录D 冻结版:202**：规则 ``PATH_ASSERT`` → conclusion=DENY（审计反查键）。
- **红线④**（``.trellis/spec/backend/index.md:28``）：必经节点缺失即 DENY，
  断言必经、不可 opt-in——无任何参数/环境变量能缩小 ``REQUIRED_NODES``。

纯函数零 IO（红线① 同定位）：无 DB、无网络、无时钟访问。
"""

from __future__ import annotations

import inspect

import pytest

from src.graph import path_assert
from src.graph.nodes import PIPELINE_NODES
from src.graph.path_assert import PathVerdict, assert_path, required_nodes

pytestmark = pytest.mark.unit

#: 红线④ 锚定的最小必经节点集（任务 prd ①：至少含这五个）。
#: 测试硬编码期望值，不从实现取值——防止与实现同流合污。
_MINIMUM_REQUIRED = frozenset({"segment", "decision", "confirm_plan", "gate", "send"})


def test_required_nodes_is_frozen_superset_of_minimum_pipeline() -> None:
    """必经节点集为 frozenset（对调用方只读）且 ⊇ 五个管线节点。"""
    nodes = required_nodes()
    assert isinstance(nodes, frozenset)
    assert nodes >= _MINIMUM_REQUIRED
    assert not hasattr(nodes, "add"), "frozenset 不可变——不存在缩小入口"


def test_required_nodes_reuses_pipeline_nodes_constant() -> None:
    """实现复用 ``PIPELINE_NODES``（复用常量，不重定义节点字符串）。"""
    assert frozenset(PIPELINE_NODES) == required_nodes()


def test_full_pipeline_trace_passes() -> None:
    """完整轨迹 ⊇ REQUIRED_NODES → 通过（conclusion=ALLOW）。"""
    verdict = assert_path(list(PIPELINE_NODES))

    assert isinstance(verdict, PathVerdict)
    assert verdict.passed is True
    assert verdict.missing == frozenset()
    assert verdict.conclusion == "ALLOW"
    assert verdict.rule_id == "PATH_ASSERT"


def test_missing_confirm_plan_denies_with_e14_and_missing_list() -> None:
    """缺 confirm_plan（ADR-035 硬要求）→ DENY + E14 + 缺失清单精确列出（非裸 Exception）。"""
    trace = [node for node in PIPELINE_NODES if node != "confirm_plan"]

    verdict = assert_path(trace)

    assert verdict.passed is False
    assert verdict.conclusion == "DENY"
    assert verdict.alert_code == "E14"
    assert verdict.rule_id == "PATH_ASSERT"
    assert verdict.missing == frozenset({"confirm_plan"})


def test_empty_trace_denies_with_every_required_node_missing() -> None:
    """空轨迹 → missing == 必经节点全集（E14）。"""
    verdict = assert_path([])

    assert verdict.passed is False
    assert verdict.conclusion == "DENY"
    assert verdict.alert_code == "E14"
    assert verdict.missing == required_nodes()


@pytest.mark.parametrize("node", sorted(_MINIMUM_REQUIRED))
def test_each_required_node_missing_denies(node: str) -> None:
    """参数化逐节点：从完整轨迹剔除任一必经节点 → DENY + E14 + missing 恰为该节点。"""
    trace = [item for item in PIPELINE_NODES if item != node]

    verdict = assert_path(trace)

    assert verdict.passed is False, f"缺 {node} 必须 DENY（红线④）"
    assert verdict.conclusion == "DENY"
    assert verdict.alert_code == "E14"
    assert verdict.rule_id == "PATH_ASSERT"
    assert verdict.missing == frozenset({node})


def test_assert_path_signature_has_no_shrink_parameter() -> None:
    """签名只有 ``visited``——不存在任何"缩小 REQUIRED_NODES"的参数（红线④）。"""
    parameters = inspect.signature(assert_path).parameters
    assert list(parameters) == ["visited"]

    with pytest.raises(TypeError):
        assert_path(list(PIPELINE_NODES), required=frozenset())  # type: ignore[call-arg]


def test_env_vars_cannot_shrink_required_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量无法缩小必经集合（红线④：断言不可 opt-in、无旁路开关）。"""
    for name in (
        "PATH_ASSERT_SKIP",
        "PATH_ASSERT_REQUIRED_NODES",
        "GOVERN_REQUIRED_NODES",
        "PATH_ASSERT_DISABLED",
    ):
        monkeypatch.setenv(name, "confirm_plan")
    trace = [node for node in PIPELINE_NODES if node != "confirm_plan"]

    verdict = assert_path(trace)

    assert verdict.passed is False, "环境变量不得让缺 confirm_plan 的轨迹通过"
    assert verdict.missing == frozenset({"confirm_plan"})
    assert verdict.conclusion == "DENY"


def test_required_nodes_cannot_be_rewritten_at_runtime() -> None:
    """红线④（收口）：必经节点集**运行期不可改写**——外部属性赋值改不动断言。

    集合是模块私有的（``_REQUIRED_NODES``）且被断言器**闭包捕获**：
    外部把 ``REQUIRED_NODES`` / ``_REQUIRED_NODES`` 都赋成空集后，
    只读视图与断言行为**均不变**（缺节点的轨迹仍被拦下）。
    """
    original = required_nodes()
    module_dict = vars(path_assert)
    try:
        # 直接改模块 __dict__（绕过静态类型检查的 Final 保护，正是「运行期改写」场景）
        module_dict["REQUIRED_NODES"] = frozenset()
        module_dict["_REQUIRED_NODES"] = frozenset()

        assert required_nodes() == original, "只读视图不得被外部赋值改写"
        verdict = assert_path([node for node in PIPELINE_NODES if node != "confirm_plan"])

        assert verdict.passed is False, "必经节点缺失必须仍然被拦（红线④）"
        assert verdict.missing == frozenset({"confirm_plan"})
        assert verdict.conclusion == "DENY"
    finally:
        module_dict["_REQUIRED_NODES"] = original
        module_dict.pop("REQUIRED_NODES", None)
