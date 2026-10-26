"""T-B1 契约测试：策略内核的数据契约（枚举 + 判定结果结构）。

依据（可反查）：
- **附录D §4「规则 ID 登记表（唯一权威）」**——11 个 rule_id 与登记顺序；
  §4 尾注——`hit_rule_ids` 与 `suppressed_rule_ids` 必须分开记。
- **架构 v5.2 §4.3**——`evaluate()` 返回 `ALLOW / REQUIRE_APPROVAL / DENY` 三值。
- **`.trellis/spec/backend/directory-structure.md`**——`src/policy_kernel/` 只允许
  标准库 + `governance.yaml`，禁止 LLM / 框架 / 供应商 SDK。
- **红线 ①**（纯函数零 LLM）、**红线 ②**（DENY 不可绕过）。

纯函数单测：无 PG、无 LLM、无框架。
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Table

from src.models.decision import Decision as DecisionModel
from src.models.rule_ids import REGISTERED_RULE_IDS
from src.policy_kernel.kernel import Decision, DecisionResult, RuleId

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
KERNEL_PY = ROOT / "src" / "policy_kernel" / "kernel.py"

#: 内核模块**绝对不允许**出现的依赖（红线 ① + 目录边界）。
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "src",  # 内核不得依赖任何 src.* 包（含 src.models → SQLAlchemy）
        "sqlalchemy",
        "litellm",
        "fastapi",
        "pydantic",
        "langchain",
        "langgraph",
        "yaml",  # 解析 YAML 属配置加载（config.py），判定内核不需要
    }
)


# ---------------------------------------------------------------------------
# 一、RuleId 枚举：数量 / 内容 / 顺序 与附录D §4 登记表逐项一致
# ---------------------------------------------------------------------------


def test_rule_id_count_is_eleven() -> None:
    """附录D §4 登记表共 11 条；内核枚举必须恰好 11 个，不多不少。

    多一个 = 自造 rule_id（附录D §4 尾注「禁止自造 ID」）；
    少一个 = 登记表有规则没有实现，评测集回放必然对不上。
    """
    assert len(list(RuleId)) == 11, (
        f"RuleId 应有 11 个（附录D §4 登记表），实际 {len(list(RuleId))}："
        f"{[r.name for r in RuleId]}"
    )


def test_rule_id_matches_registered_table() -> None:
    """RuleId 的取值与顺序必须与 `src/models.rule_ids.REGISTERED_RULE_IDS` 逐项相等。

    两侧同源于附录D §4 冻结版；这里用**字面量比对**（不是 import 比对）——
    内核侧不得 import `src.models`（会把 SQLAlchemy 带进纯函数内核）。
    """
    kernel_values = tuple(r.value for r in RuleId)
    assert kernel_values == REGISTERED_RULE_IDS, (
        "RuleId 与附录D §4 登记表漂移。\n"
        f"  内核枚举 = {kernel_values}\n"
        f"  登记镜像 = {REGISTERED_RULE_IDS}\n"
        "  顺序也必须一致（登记表顺序即登记顺序）。"
    )


def test_rule_id_member_name_equals_its_value() -> None:
    """每个成员的 name 与 value 必须相同——杜绝「名字对、值写错」的静默漂移。

    内核枚举是 str 枚举，值会直接写进 `decision.hit_rule_ids`（TEXT[]），
    name/value 不一致会让审计流水里的 rule_id 与登记表对不上。
    """
    mismatched = [r.name for r in RuleId if r.name != r.value]
    assert not mismatched, f"以下 RuleId 的 name 与 value 不一致：{mismatched}"


def test_rule_id_values_are_registered_names_only() -> None:
    """RuleId 的每个取值必须字面出现在冻结版登记表镜像中（禁止自造）。"""
    for rule in RuleId:
        assert rule.value in REGISTERED_RULE_IDS, f"自造 rule_id：{rule.value}"


# ---------------------------------------------------------------------------
# 二、Decision 枚举：恰好三值
# ---------------------------------------------------------------------------


def test_decision_has_exactly_three_values() -> None:
    """架构 v5.2 §4.3：`evaluate()` 返回 ALLOW / REQUIRE_APPROVAL / DENY 三值。

    不得增删第四个取值（例如「强制放行」——那会从契约层打开红线②的绕过口）。
    """
    values = {d.value for d in Decision}
    assert values == {"ALLOW", "REQUIRE_APPROVAL", "DENY"}, (
        f"Decision 取值必须恰为三值，实际 {sorted(values)}"
    )
    assert len(list(Decision)) == 3, f"Decision 取值数应为 3，实际 {len(list(Decision))}"


def test_decision_values_match_database_conclusion_constraint() -> None:
    """Decision 的取值必须与 `decision` 表 `chk_decision_conclusion` 的白名单一致。

    内核结论要落库；契约不一致时内核会写出无法入库的结论行。
    （ORM 侧 import 只发生在**测试层**——内核层禁止 import `src.models`。）
    """
    table = cast(Table, DecisionModel.__table__)
    constraint = next(c for c in table.constraints if c.name == "chk_decision_conclusion")
    sqltext = str(cast(CheckConstraint, constraint).sqltext)
    for decision in Decision:
        assert f"'{decision.value}'" in sqltext, (
            f"decision 表 CHECK 约束缺少结论取值 {decision.value}"
        )


# ---------------------------------------------------------------------------
# 三、判定结果结构：hit / suppressed 两组 rule_id 分离承载
# ---------------------------------------------------------------------------


def test_hit_and_suppressed_rule_ids_are_separate_fields() -> None:
    """附录D §4 尾注：`hit_rule_ids` 与 `suppressed_rule_ids` **必须分开记**。

    `CONFLICT_WINDOW_72H` 表示「**其他**候选被抑制」，不得混进本候选的命中集合；
    评测基准的三要素对照（conclusion / hit / suppressed）直接依赖这个分离。
    """
    assert dataclasses.is_dataclass(DecisionResult), "DecisionResult 必须是 dataclass"
    field_names = {f.name for f in dataclasses.fields(DecisionResult)}
    for required in ("hit_rule_ids", "suppressed_rule_ids"):
        assert required in field_names, (
            f"DecisionResult 缺少字段 {required}（附录D §4 尾注要求 hit/suppressed 分离）"
        )

    result = DecisionResult(
        conclusion=Decision.ALLOW,
        hit_rule_ids=(RuleId.LAYER_RESOLVE,),
        suppressed_rule_ids=(RuleId.CONFLICT_WINDOW_72H,),
        suppressed_candidates=1,
    )
    assert result.hit_rule_ids == (RuleId.LAYER_RESOLVE,), "hit 集合被污染"
    assert result.suppressed_rule_ids == (RuleId.CONFLICT_WINDOW_72H,), "suppressed 集合被污染"
    assert set(result.hit_rule_ids).isdisjoint(result.suppressed_rule_ids), (
        "hit 与 suppressed 不得出现同一 rule_id"
    )


def test_result_carries_all_contract_fields() -> None:
    """判定结果必须承载评测对照与频控延迟所需的全部字段。

    依据：附录D §4 尾注（三要素）+ 规则 2（`cooling_remaining_seconds` /
    `channel_cooling_remaining_seconds`）+ 规则 3（`suppressed_candidates`）。
    """
    field_names = {f.name for f in dataclasses.fields(DecisionResult)}
    required = {
        "conclusion",
        "hit_rule_ids",
        "suppressed_rule_ids",
        "suppressed_candidates",
        "evidence",
        "cooling_remaining_seconds",
        "channel_cooling_remaining_seconds",
    }
    missing = required - field_names
    assert not missing, f"DecisionResult 缺少字段：{sorted(missing)}"


def test_result_defaults_are_neutral() -> None:
    """默认判定结果必须中性：无命中、无抑制、无冷却、无证据。"""
    result = DecisionResult(conclusion=Decision.ALLOW)
    assert hasattr(result, "suppressed_rule_ids"), (
        "DecisionResult 缺少 suppressed_rule_ids（附录D §4 尾注要求 hit/suppressed 分离）"
    )
    assert result.hit_rule_ids == ()
    assert result.suppressed_rule_ids == ()
    assert result.suppressed_candidates == 0
    assert dict(result.evidence) == {}
    assert result.cooling_remaining_seconds is None
    assert result.channel_cooling_remaining_seconds is None


def test_result_is_immutable() -> None:
    """判定结果必须不可变——审计留痕的前提（红线③：append-only）。"""
    assert hasattr(RuleId, "PATH_ASSERT"), "RuleId 缺少 PATH_ASSERT（附录D §4 登记表）"
    result = DecisionResult(conclusion=Decision.DENY, hit_rule_ids=(RuleId.PATH_ASSERT,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.conclusion = Decision.ALLOW  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 四、内核模块的依赖边界（红线 ①）
# ---------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    """解析模块文件，返回所有 import 的顶层包名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对 import：包内引用，不引入外部依赖
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_kernel_module_does_not_import_models() -> None:
    """内核模块**不得** import `src.models` / `sqlalchemy` / 任何第三方 SDK。

    依据：`.trellis/spec/backend/directory-structure.md`——`src/policy_kernel/`
    只允许标准库 + `governance.yaml`；PRD 红线「policy_kernel 纯函数零 LLM」。
    `src.models` 会连带引入 SQLAlchemy，把 ORM 带进纯函数内核。
    """
    assert KERNEL_PY.exists(), f"内核模块不存在：{KERNEL_PY}"
    roots = _imported_roots(KERNEL_PY)
    forbidden = sorted(roots & FORBIDDEN_IMPORT_ROOTS)
    assert not forbidden, f"{KERNEL_PY.name} 出现禁止依赖：{forbidden}（内核只允许标准库）"


def test_kernel_module_only_imports_standard_library() -> None:
    """内核模块的所有 import 顶层包名都必须是标准库模块。"""
    roots = _imported_roots(KERNEL_PY)
    non_stdlib = sorted(root for root in roots if root not in sys.stdlib_module_names)
    assert not non_stdlib, f"内核出现非标准库依赖：{non_stdlib}"


def test_kernel_module_exposes_no_bypass_entry_point() -> None:
    """红线 ②：内核不得提供任何强制放行 / 绕过 DENY 的入口。

    「不可绕过」要落到可测的形态：模块的公开名字里不允许出现
    force / override / bypass / skip 一类字样。
    """
    import src.policy_kernel.kernel as kernel_module

    banned = ("force", "override", "bypass", "skip", "ignore")
    offenders = sorted(
        name
        for name in dir(kernel_module)
        if not name.startswith("_") and any(word in name.lower() for word in banned)
    )
    assert not offenders, f"内核暴露疑似绕过入口：{offenders}"


def test_kernel_module_is_free_of_llm_and_sdk_symbols() -> None:
    """内核模块的**代码**里不出现 LLM / 供应商 SDK 的标识符（注释与文档字符串不计）。

    只扫 AST 里的标识符（名称 / 属性），避免把「文档里说明禁止用 litellm」
    这类说明文字误判为真实依赖。
    """
    tree = ast.parse(KERNEL_PY.read_text(encoding="utf-8"), filename=str(KERNEL_PY))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    banned_symbols = (
        "litellm",
        "openai",
        "anthropic",
        "langchain",
        "langgraph",
        "requests",
        "httpx",
        "sqlalchemy",
        "sessionmaker",
        "create_engine",
    )
    hits = sorted(symbol for symbol in banned_symbols if symbol in identifiers)
    assert not hits, f"内核代码出现 LLM / SDK 标识符：{hits}"
