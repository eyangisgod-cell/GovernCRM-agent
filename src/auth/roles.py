"""RBAC 角色与权限矩阵定义（T-E5）。

权威依据：

- **裁决 #23 / FR-30（PRD v5.2:409）**：MVP = 单店 + 4 角色。
- **PRD v5.2 十一章（:512 起）权限矩阵 + ⚠️脚注 a1–a10**（G-12「整包采纳不改」，
  2026-10-24 冻结）——矩阵唯一权威源，本文件逐格照抄。
- **ADR-044 / K-M3（架构 v5.3:70、:880）**：管理员不自动蕴含下级权限——admin 只是矩阵中
  一行，不是例外分支。

红线②：DENY 对所有角色（含管理员）不可绕过；「强制放行（覆盖 DENY）」与「查看明文 PII」
两行对所有角色均为 ``DENY``。

本模块只定义数据（枚举 + 矩阵字典），不含判定逻辑；判定在 ``src/auth/rbac.py``。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PERMISSION_MATRIX",
    "Access",
    "Permission",
    "Role",
]


class Role(StrEnum):
    """4 角色（裁决 #23，PRD v5.2:409 FR-30）。"""

    ADMIN = "admin"
    OPS_LEAD = "ops_lead"
    OPS_SPECIALIST = "ops_specialist"
    COMPLIANCE = "compliance"


class Permission(StrEnum):
    """权限矩阵的功能行（PRD v5.2 十一章表格逐行）。"""

    #: 创建/配置数字员工
    CREATE_CONFIGURE_DIGITAL_WORKER = "create_configure_digital_worker"
    #: 调整自主度等级
    ADJUST_AUTONOMY_LEVEL = "adjust_autonomy_level"
    #: 下达任务
    DISPATCH_TASK = "dispatch_task"
    #: 审批动作
    APPROVE_ACTION = "approve_action"
    #: 配置分层与策略
    CONFIGURE_LAYER_STRATEGY = "configure_layer_strategy"
    #: 配置抑制规则
    CONFIGURE_SUPPRESSION_RULE = "configure_suppression_rule"
    #: 强制放行（覆盖 DENY）——红线②：所有角色均 ❌
    FORCE_RELEASE = "force_release"
    #: 触发熔断
    TRIGGER_CIRCUIT_BREAKER = "trigger_circuit_breaker"
    #: 查看/导出审计
    VIEW_EXPORT_AUDIT = "view_export_audit"
    #: 查看明文 PII——所有角色均 ❌（PRD C1 明文零落库）
    VIEW_PLAINTEXT_PII = "view_plaintext_pii"
    #: 修改技能库并发布
    MODIFY_PUBLISH_SKILL = "modify_publish_skill"
    #: 管理平台密钥
    MANAGE_PLATFORM_SECRET = "manage_platform_secret"
    #: 关闭/开启第三方推理 API 出境（C9）
    TOGGLE_CROSS_BORDER_INFERENCE = "toggle_cross_border_inference"


class Access(StrEnum):
    """矩阵单元格取值：✅ 允许 / ❌ 拒绝 / ⚠️ 有条件允许（脚注 a1–a10）。"""

    ALLOW = "allow"
    DENY = "deny"
    #: ⚠️ 有条件允许：脚注条件（a1–a10）在更高层（审批路由等）校验，本任务不展开。
    CONDITIONAL = "conditional"


#: 十一章权限矩阵逐格（唯一权威源 = PRD v5.2:516-545 表格 + 脚注 a1–a10）。
#: 结构 = 角色 → 动作 → Access；「强制放行」「查看明文 PII」两行对所有角色恒为 DENY。
PERMISSION_MATRIX: dict[Role, dict[Permission, Access]] = {
    Role.ADMIN: {
        Permission.CREATE_CONFIGURE_DIGITAL_WORKER: Access.ALLOW,
        Permission.ADJUST_AUTONOMY_LEVEL: Access.ALLOW,
        Permission.DISPATCH_TASK: Access.ALLOW,
        Permission.APPROVE_ACTION: Access.ALLOW,
        Permission.CONFIGURE_LAYER_STRATEGY: Access.ALLOW,
        Permission.CONFIGURE_SUPPRESSION_RULE: Access.ALLOW,
        Permission.FORCE_RELEASE: Access.DENY,
        Permission.TRIGGER_CIRCUIT_BREAKER: Access.ALLOW,
        Permission.VIEW_EXPORT_AUDIT: Access.ALLOW,
        Permission.VIEW_PLAINTEXT_PII: Access.DENY,
        Permission.MODIFY_PUBLISH_SKILL: Access.DENY,
        Permission.MANAGE_PLATFORM_SECRET: Access.ALLOW,
        Permission.TOGGLE_CROSS_BORDER_INFERENCE: Access.ALLOW,
    },
    Role.OPS_LEAD: {
        Permission.CREATE_CONFIGURE_DIGITAL_WORKER: Access.ALLOW,
        Permission.ADJUST_AUTONOMY_LEVEL: Access.ALLOW,
        Permission.DISPATCH_TASK: Access.ALLOW,
        Permission.APPROVE_ACTION: Access.ALLOW,
        Permission.CONFIGURE_LAYER_STRATEGY: Access.ALLOW,
        Permission.CONFIGURE_SUPPRESSION_RULE: Access.CONDITIONAL,  # a6
        Permission.FORCE_RELEASE: Access.DENY,
        Permission.TRIGGER_CIRCUIT_BREAKER: Access.ALLOW,
        Permission.VIEW_EXPORT_AUDIT: Access.ALLOW,
        Permission.VIEW_PLAINTEXT_PII: Access.DENY,
        Permission.MODIFY_PUBLISH_SKILL: Access.ALLOW,
        Permission.MANAGE_PLATFORM_SECRET: Access.DENY,
        Permission.TOGGLE_CROSS_BORDER_INFERENCE: Access.DENY,
    },
    Role.OPS_SPECIALIST: {
        Permission.CREATE_CONFIGURE_DIGITAL_WORKER: Access.CONDITIONAL,  # a1
        Permission.ADJUST_AUTONOMY_LEVEL: Access.DENY,
        Permission.DISPATCH_TASK: Access.ALLOW,
        Permission.APPROVE_ACTION: Access.CONDITIONAL,  # a3
        Permission.CONFIGURE_LAYER_STRATEGY: Access.CONDITIONAL,  # a4
        Permission.CONFIGURE_SUPPRESSION_RULE: Access.DENY,
        Permission.FORCE_RELEASE: Access.DENY,
        Permission.TRIGGER_CIRCUIT_BREAKER: Access.ALLOW,
        Permission.VIEW_EXPORT_AUDIT: Access.CONDITIONAL,  # a7
        Permission.VIEW_PLAINTEXT_PII: Access.DENY,
        Permission.MODIFY_PUBLISH_SKILL: Access.CONDITIONAL,  # a8
        Permission.MANAGE_PLATFORM_SECRET: Access.DENY,
        Permission.TOGGLE_CROSS_BORDER_INFERENCE: Access.DENY,
    },
    Role.COMPLIANCE: {
        Permission.CREATE_CONFIGURE_DIGITAL_WORKER: Access.DENY,
        Permission.ADJUST_AUTONOMY_LEVEL: Access.CONDITIONAL,  # a2
        Permission.DISPATCH_TASK: Access.DENY,
        Permission.APPROVE_ACTION: Access.ALLOW,
        Permission.CONFIGURE_LAYER_STRATEGY: Access.CONDITIONAL,  # a5
        Permission.CONFIGURE_SUPPRESSION_RULE: Access.ALLOW,
        Permission.FORCE_RELEASE: Access.DENY,
        Permission.TRIGGER_CIRCUIT_BREAKER: Access.ALLOW,
        Permission.VIEW_EXPORT_AUDIT: Access.ALLOW,
        Permission.VIEW_PLAINTEXT_PII: Access.DENY,
        Permission.MODIFY_PUBLISH_SKILL: Access.CONDITIONAL,  # a9
        Permission.MANAGE_PLATFORM_SECRET: Access.DENY,
        Permission.TOGGLE_CROSS_BORDER_INFERENCE: Access.CONDITIONAL,  # a10
    },
}
