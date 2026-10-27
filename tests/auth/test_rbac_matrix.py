"""T-E5 RBAC 权限矩阵纯函数验收用例。

权威依据：

- **裁决 #23 / FR-30（PRD v5.2:409）**：MVP = 单店 + 4 角色（admin/ops_lead/ops_specialist/compliance）。
- **PRD v5.2 十一章（:512 起）权限矩阵 + ⚠️脚注 a1–a10**（G-12「整包采纳不改」，2026-10-24 冻结）——
  矩阵唯一权威源，逐格照抄进测试数据。
- **ADR-044 / K-M3（架构 v5.3:70、:880）**：管理员不自动蕴含下级权限，admin 只是矩阵中一行。
- **红线②**：DENY 对所有角色（含管理员）不可绕过；「强制放行（覆盖 DENY）」对所有角色均为 ❌。
- **架构 v5.3:199**：命中 SUP_* / NO_CONSENT 的强制放行一律拒 → ``E_DENY_IRREVERSIBLE``。

纯函数部分零 DB、零框架；审计写入见 ``test_rbac_audit_pg.py``。
"""

from __future__ import annotations

import pytest

from src.auth.rbac import check_permission
from src.auth.roles import PERMISSION_MATRIX, Access, Permission, Role
from src.policy_kernel.kernel import Decision

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 十一章矩阵逐格（照抄 PRD v5.2:516-545 表格 + 脚注 a1–a10）
# 每格 = (role, action, 期望 Access, 脚注编号或 None)
# ---------------------------------------------------------------------------
_MATRIX_CELLS: list[tuple[Role, Permission, Access, str | None]] = [
    # 创建/配置数字员工
    (Role.ADMIN, Permission.CREATE_CONFIGURE_DIGITAL_WORKER, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.CREATE_CONFIGURE_DIGITAL_WORKER, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.CREATE_CONFIGURE_DIGITAL_WORKER, Access.CONDITIONAL, "a1"),
    (Role.COMPLIANCE, Permission.CREATE_CONFIGURE_DIGITAL_WORKER, Access.DENY, None),
    # 调整自主度等级
    (Role.ADMIN, Permission.ADJUST_AUTONOMY_LEVEL, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.ADJUST_AUTONOMY_LEVEL, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.ADJUST_AUTONOMY_LEVEL, Access.DENY, None),
    (Role.COMPLIANCE, Permission.ADJUST_AUTONOMY_LEVEL, Access.CONDITIONAL, "a2"),
    # 下达任务
    (Role.ADMIN, Permission.DISPATCH_TASK, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.DISPATCH_TASK, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.DISPATCH_TASK, Access.ALLOW, None),
    (Role.COMPLIANCE, Permission.DISPATCH_TASK, Access.DENY, None),
    # 审批动作
    (Role.ADMIN, Permission.APPROVE_ACTION, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.APPROVE_ACTION, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.APPROVE_ACTION, Access.CONDITIONAL, "a3"),
    (Role.COMPLIANCE, Permission.APPROVE_ACTION, Access.ALLOW, None),
    # 配置分层与策略
    (Role.ADMIN, Permission.CONFIGURE_LAYER_STRATEGY, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.CONFIGURE_LAYER_STRATEGY, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.CONFIGURE_LAYER_STRATEGY, Access.CONDITIONAL, "a4"),
    (Role.COMPLIANCE, Permission.CONFIGURE_LAYER_STRATEGY, Access.CONDITIONAL, "a5"),
    # 配置抑制规则
    (Role.ADMIN, Permission.CONFIGURE_SUPPRESSION_RULE, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.CONFIGURE_SUPPRESSION_RULE, Access.CONDITIONAL, "a6"),
    (Role.OPS_SPECIALIST, Permission.CONFIGURE_SUPPRESSION_RULE, Access.DENY, None),
    (Role.COMPLIANCE, Permission.CONFIGURE_SUPPRESSION_RULE, Access.ALLOW, None),
    # 强制放行（覆盖 DENY）——红线②：所有角色均 ❌
    (Role.ADMIN, Permission.FORCE_RELEASE, Access.DENY, None),
    (Role.OPS_LEAD, Permission.FORCE_RELEASE, Access.DENY, None),
    (Role.OPS_SPECIALIST, Permission.FORCE_RELEASE, Access.DENY, None),
    (Role.COMPLIANCE, Permission.FORCE_RELEASE, Access.DENY, None),
    # 触发熔断
    (Role.ADMIN, Permission.TRIGGER_CIRCUIT_BREAKER, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.TRIGGER_CIRCUIT_BREAKER, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.TRIGGER_CIRCUIT_BREAKER, Access.ALLOW, None),
    (Role.COMPLIANCE, Permission.TRIGGER_CIRCUIT_BREAKER, Access.ALLOW, None),
    # 查看/导出审计
    (Role.ADMIN, Permission.VIEW_EXPORT_AUDIT, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.VIEW_EXPORT_AUDIT, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.VIEW_EXPORT_AUDIT, Access.CONDITIONAL, "a7"),
    (Role.COMPLIANCE, Permission.VIEW_EXPORT_AUDIT, Access.ALLOW, None),
    # 查看明文 PII——所有角色均 ❌（PRD C1 明文零落库）
    (Role.ADMIN, Permission.VIEW_PLAINTEXT_PII, Access.DENY, None),
    (Role.OPS_LEAD, Permission.VIEW_PLAINTEXT_PII, Access.DENY, None),
    (Role.OPS_SPECIALIST, Permission.VIEW_PLAINTEXT_PII, Access.DENY, None),
    (Role.COMPLIANCE, Permission.VIEW_PLAINTEXT_PII, Access.DENY, None),
    # 修改技能库并发布
    (Role.ADMIN, Permission.MODIFY_PUBLISH_SKILL, Access.DENY, None),
    (Role.OPS_LEAD, Permission.MODIFY_PUBLISH_SKILL, Access.ALLOW, None),
    (Role.OPS_SPECIALIST, Permission.MODIFY_PUBLISH_SKILL, Access.CONDITIONAL, "a8"),
    (Role.COMPLIANCE, Permission.MODIFY_PUBLISH_SKILL, Access.CONDITIONAL, "a9"),
    # 管理平台密钥
    (Role.ADMIN, Permission.MANAGE_PLATFORM_SECRET, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.MANAGE_PLATFORM_SECRET, Access.DENY, None),
    (Role.OPS_SPECIALIST, Permission.MANAGE_PLATFORM_SECRET, Access.DENY, None),
    (Role.COMPLIANCE, Permission.MANAGE_PLATFORM_SECRET, Access.DENY, None),
    # 关闭/开启第三方推理 API 出境（C9）
    (Role.ADMIN, Permission.TOGGLE_CROSS_BORDER_INFERENCE, Access.ALLOW, None),
    (Role.OPS_LEAD, Permission.TOGGLE_CROSS_BORDER_INFERENCE, Access.DENY, None),
    (Role.OPS_SPECIALIST, Permission.TOGGLE_CROSS_BORDER_INFERENCE, Access.DENY, None),
    (Role.COMPLIANCE, Permission.TOGGLE_CROSS_BORDER_INFERENCE, Access.CONDITIONAL, "a10"),
]


def test_four_roles_defined() -> None:
    """4 角色 = admin / ops_lead / ops_specialist / compliance（裁决 #23，PRD v5.2:409）。"""
    assert {role.value for role in Role} == {
        "admin",
        "ops_lead",
        "ops_specialist",
        "compliance",
    }


@pytest.mark.parametrize(
    ("role", "action", "expected", "footnote"),
    _MATRIX_CELLS,
    ids=[f"{r.value}-{a.value}" for r, a, _, _ in _MATRIX_CELLS],
)
def test_matrix_cell_matches_prd_chapter_11(
    role: Role, action: Permission, expected: Access, footnote: str | None
) -> None:
    """逐格断言与 PRD 十一章矩阵一致（含 ⚠️ 脚注 a1–a10）。"""
    assert PERMISSION_MATRIX[role][action] is expected


def test_conditional_cells_carry_footnote_ids() -> None:
    """所有 ⚠️（CONDITIONAL）单元格都应对应 G-12 冻结脚注 a1–a10 之一。"""
    frozen = {"a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10"}
    for role, action, access, footnote in _MATRIX_CELLS:
        if access is Access.CONDITIONAL:
            assert footnote in frozen, f"{role.value}·{action.value} 的 ⚠️ 未登记脚注编号"


def test_admin_does_not_inherit_lower_permissions() -> None:
    """管理员不自动蕴含下级权限（ADR-044 / K-M3）。

    「修改技能库并发布」admin ❌、ops_lead ✅——admin 不能因为是管理员就发布技能。
    """
    result = check_permission(Role.ADMIN, Permission.MODIFY_PUBLISH_SKILL)
    assert result.conclusion is Decision.DENY
    assert result.error_code == "E_RBAC_DENIED"


def test_admin_force_release_sup_high_value_denied() -> None:
    """管理员对 SUP_HIGH_VALUE 客户强制放行 → 被拒（ADR-044 / K-M3 / 架构 v5.3:199）。

    理由含权限/rule 标识（E_DENY_IRREVERSIBLE + SUP_HIGH_VALUE），不是裸 Exception。
    """
    result = check_permission(
        Role.ADMIN,
        Permission.FORCE_RELEASE,
        context={"hit_rule_ids": ["SUP_HIGH_VALUE"]},
    )
    assert result.conclusion is Decision.DENY
    assert result.error_code == "E_DENY_IRREVERSIBLE"
    assert result.rule_id == "SUP_HIGH_VALUE"
    assert "SUP_HIGH_VALUE" in result.reason


@pytest.mark.parametrize("role", list(Role))
def test_force_release_denied_for_all_roles(role: Role) -> None:
    """「强制放行（覆盖 DENY）」对所有角色均为 ❌（红线②）。"""
    result = check_permission(role, Permission.FORCE_RELEASE)
    assert result.conclusion is Decision.DENY


@pytest.mark.parametrize("role", list(Role))
def test_view_plaintext_pii_denied_for_all_roles(role: Role) -> None:
    """「查看明文 PII」对所有角色均为 ❌（PRD C1 明文零落库）。"""
    result = check_permission(role, Permission.VIEW_PLAINTEXT_PII)
    assert result.conclusion is Decision.DENY


def test_ops_specialist_cannot_configure_suppression_rule() -> None:
    """越权：运营专员执行合规独占操作（配置抑制规则）→ Deny。

    PRD 十一章：配置抑制规则 ops_specialist ❌、compliance ✅。
    """
    result = check_permission(Role.OPS_SPECIALIST, Permission.CONFIGURE_SUPPRESSION_RULE)
    assert result.conclusion is Decision.DENY


def test_ops_specialist_can_dispatch_task() -> None:
    """正向：运营专员可下达任务（PRD 十一章 ✅）。"""
    result = check_permission(Role.OPS_SPECIALIST, Permission.DISPATCH_TASK)
    assert result.conclusion is Decision.ALLOW


def test_compliance_can_approve_action() -> None:
    """正向：合规可审批动作（PRD 十一章 ✅）。"""
    result = check_permission(Role.COMPLIANCE, Permission.APPROVE_ACTION)
    assert result.conclusion is Decision.ALLOW


def test_conditional_cell_requires_approval() -> None:
    """⚠️ 有条件允许 → 判定为 REQUIRE_APPROVAL（脚注条件另行校验，本任务不展开）。"""
    result = check_permission(Role.COMPLIANCE, Permission.ADJUST_AUTONOMY_LEVEL)
    assert result.conclusion is Decision.REQUIRE_APPROVAL
