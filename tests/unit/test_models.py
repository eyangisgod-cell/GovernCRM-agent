"""核心数据模型的静态契约（T0.2）。

这些断言把 prd.md §4 的验收标准写成**机器可读**的形式：

- 14 张核心表 **每表一个模型类**，类名 = 表名单数首字母大写；
- 模型与 SQL 表 **一一对应**（不多不少）；
- 客户域 **无明文 PII 列**；
- ``rule_id`` **不得自造**——必须取自附录 D §4 冻结登记表；
- 关键约束（记忆空间键、技能卡双门禁、低置信不合并）在模型里必须存在。

本文件是**纯静态**用例，不连数据库；真 PG 行为验证见
``tests/integration/test_core_tables.py``（marker ``pg``）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.orm import DeclarativeBase

from src.models import Base
from src.models.rule_ids import REGISTERED_RULE_IDS, rule_id_subset_check

pytestmark = pytest.mark.unit

#: prd.md §1 的 14 张核心表
CORE_TABLES: tuple[str, ...] = (
    "customer",
    "customer_identity",
    "customer_segment",
    "action_candidate",
    "decision",
    "audit_log",
    "suppression_record",
    "approval",
    "tool_call",
    "worker",
    "worker_template",
    "worker_metrics",
    "skill_card",
    "cost_record",
)

#: 框架辅助表 + 记忆表（结构先留，逻辑后置 v0.3 —— MVP_任务拆解 §1.2 X1）
EXTRA_TABLES: tuple[str, ...] = (
    "schema_migration",
    "memory_episode",
    "memory_quarantine",
)

#: 附录 D §4 冻结登记表的独立副本（**测试不复用被测常量**，否则断言自证）
EXPECTED_RULE_IDS: tuple[str, ...] = (
    "SUP_HIGH_VALUE",
    "SUP_HIGH_COMPLAINT",
    "SUP_HIGH_REFUND",
    "SUP_EXPLICIT_REFUSAL",
    "NO_CONSENT",
    "FREQ_GLOBAL_7D",
    "CONFLICT_WINDOW_72H",
    "LAYER_RESOLVE",
    "BENEFIT_MAX",
    "ESCALATE_HUMAN",
    "PATH_ASSERT",
)

#: 客户域的明文 PII 关键词（命中即违规，除非列名带脱敏后缀）
PII_KEYWORDS = ("phone", "mobile", "tel", "email", "id_card", "idcard", "address", "password")
#: 允许的脱敏后缀
PII_SAFE_SUFFIXES = ("_hash", "_fingerprint", "_cipher", "_token", "_masked")


def _model_classes() -> list[type[DeclarativeBase]]:
    """全部已映射的模型类（来自 ``Base.registry``，而非模块属性）。"""
    return sorted(
        {mapper.class_ for mapper in Base.registry.mappers},
        key=lambda model: model.__name__,
    )


def test_every_core_table_has_exactly_one_model_class() -> None:
    by_table: dict[str, list[str]] = {}
    for model in _model_classes():
        table_name = model.__tablename__
        by_table.setdefault(table_name, []).append(model.__name__)

    for table_name in CORE_TABLES + EXTRA_TABLES:
        assert table_name in by_table, f"缺少表 {table_name} 的模型类"
        assert len(by_table[table_name]) == 1, (
            f"表 {table_name} 有多个模型类：{by_table[table_name]}"
        )

    assert set(by_table) == set(CORE_TABLES) | set(EXTRA_TABLES), (
        f"模型表集合与预期不符，多出：{sorted(set(by_table) - set(CORE_TABLES) - set(EXTRA_TABLES))}"
    )


@pytest.mark.parametrize("table_name", CORE_TABLES + EXTRA_TABLES)
def test_model_class_name_follows_convention(table_name: str) -> None:
    """类名 = 表名单数 + 首字母大写（customer → Customer）。"""
    expected = "".join(part.capitalize() for part in table_name.split("_"))
    assert expected in {model.__name__ for model in _model_classes()}


@pytest.mark.parametrize("table_name", CORE_TABLES)
def test_model_columns_are_fully_annotated(table_name: str) -> None:
    """每列都要有类型注解（``Mapped[...]``），不允许裸列。"""
    table = Base.metadata.tables[table_name]
    assert table.columns, f"表 {table_name} 没有列"
    for column in table.columns:
        assert column.type is not None
        assert column.nullable is not None


def test_customer_has_no_plaintext_pii_columns() -> None:
    """PRD C1（明文零落库）：客户域只允许哈希 / 指纹形态的 PII 列。"""
    for table_name in ("customer", "customer_identity"):
        table = Base.metadata.tables[table_name]
        for column in table.columns:
            lowered = column.name.lower()
            if not any(keyword in lowered for keyword in PII_KEYWORDS):
                continue
            assert lowered.endswith(PII_SAFE_SUFFIXES), (
                f"{table_name}.{column.name} 疑似明文 PII 列（PRD C1）"
            )


def test_customer_has_three_consent_fields() -> None:
    """PRD C4 / 裁决 #15：知情同意三字段必须齐全。"""
    columns = {column.name for column in Base.metadata.tables["customer"].columns}
    assert {"consent_status", "consent_time", "consent_source"} <= columns


def test_rule_id_registry_matches_frozen_appendix_d() -> None:
    """``rule_id`` 登记表必须与附录 D §4 冻结版逐条一致（禁止自造）。"""
    assert REGISTERED_RULE_IDS == EXPECTED_RULE_IDS
    assert len(set(REGISTERED_RULE_IDS)) == len(REGISTERED_RULE_IDS), "登记表存在重复 ID"


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("decision", "hit_rule_ids"),
        ("decision", "suppressed_rule_ids"),
        ("audit_log", "hit_rule_ids"),
        ("suppression_record", "rule_id"),
    ],
)
def test_rule_id_columns_are_locked_to_registry(table_name: str, column_name: str) -> None:
    """存放 rule_id 的列必须带 CHECK 约束，把取值锁在登记表内。"""
    table = Base.metadata.tables[table_name]
    constraints = [
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    ]
    matched = [
        constraint
        for constraint in constraints
        if column_name in str(constraint.sqltext)
        and all(rule_id in str(constraint.sqltext) for rule_id in EXPECTED_RULE_IDS)
    ]
    assert matched, f"{table_name}.{column_name} 缺少 rule_id 登记表 CHECK 约束"


def test_worker_memory_scope_key_is_enforced() -> None:
    """``worker.memory_scope_key`` 固定为 ``{tenant_id}:{worker_id}``（v5.3 附录 E §E.2）。"""
    table = Base.metadata.tables["worker"]
    sqltexts = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert any("memory_scope_key" in text and "tenant_id::text" in text for text in sqltexts)


def test_worker_strategy_binding_cannot_override_redlines() -> None:
    """策略包绑定是「覆盖」不是「替换」：红线键在写入期即被拒绝。"""
    table = Base.metadata.tables["worker"]
    sqltexts = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert any("strategy_binding" in text and "no_consent" in text for text in sqltexts)


def test_skill_card_publish_requires_double_gate() -> None:
    """技能卡发布需「回归评测通过 + 人工确认」双门禁（裁决 #10a）。"""
    table = Base.metadata.tables["skill_card"]
    sqltexts = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert any(
        "published" in text and "regression_passed" in text and "human_confirmed" in text
        for text in sqltexts
    )


def test_low_confidence_identity_cannot_be_merged() -> None:
    """低置信身份不得强行合并（PRD P1 红线）。"""
    table = Base.metadata.tables["customer_identity"]
    sqltexts = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert any("confidence_tier" in text and "customer_id IS NULL" in text for text in sqltexts)


def test_audit_log_carries_four_versions_and_hash_chain_fields() -> None:
    """审计表必须带四版本号 + 输入快照 + 命中规则 ID + 哈希链字段。"""
    columns = {column.name for column in Base.metadata.tables["audit_log"].columns}
    assert {
        "model_version",
        "prompt_version",
        "skill_version",
        "rule_version",
        "input_snapshot",
        "hit_rule_ids",
        "record_seq",
        "prev_hash",
        "record_hash",
    } <= columns


def test_all_tables_are_tenant_scoped() -> None:
    """所有业务表带 tenant_id，且索引以 tenant_id 打头（租户隔离强制过滤）。"""
    for table_name in CORE_TABLES:
        table = Base.metadata.tables[table_name]
        assert "tenant_id" in {column.name for column in table.columns}, (
            f"表 {table_name} 缺 tenant_id"
        )
        for index in table.indexes:
            columns = [column.name for column in index.columns]
            if columns and columns[0] != "tenant_id":
                continue  # 唯一约束自带的索引不在 table.indexes 里
            assert columns[0] == "tenant_id", f"{table_name} 索引未以 tenant_id 打头：{columns}"


def test_rule_id_subset_check_helper_shape() -> None:
    """CHECK 表达式生成器：列名 + 登记表全集。"""
    expression = rule_id_subset_check("hit_rule_ids")
    assert expression.startswith("hit_rule_ids <@ ARRAY[")
    for rule_id in EXPECTED_RULE_IDS:
        assert f"'{rule_id}'" in expression
