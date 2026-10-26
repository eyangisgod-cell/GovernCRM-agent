"""RBAC 权限判定（纯函数）+ 越权审计记录辅助（T-E5）。

权威依据：

- **PRD v5.2 十一章矩阵 + FR-30（:409）**：越权被拒并审计。
- **ADR-044 / K-M3（架构 v5.3:70、:880）**：管理员不自动蕴含下级权限。
- **红线②**：DENY 对所有角色（含管理员）不可绕过。
- **架构 v5.3:199**：命中 ``SUP_*`` / ``NO_CONSENT`` 的强制放行一律拒 → ``E_DENY_IRREVERSIBLE``。

分层（红线① / 红线③）：

- ``check_permission`` —— **纯函数**判定，无 IO、不 import 框架/DB；审计写入不藏在这里。
- ``record_denial`` —— 越权拒绝的审计写入，**独立显式函数**，唯一走
  ``src/audit/hash_chain.insert_statement_sql()``（``record_hash`` 由 DB 算）；
  链尾（``prev_hash``）取自共享口径 ``src/audit/chain.py::chain_tip()``
  （**不另抄一份 SQL**——口径漂移会让链校验永远失败）。

抑制/频控语义不在本任务展开——这里只做「命中 SUP_* / NO_CONSENT 即 Deny」的权限层判定。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from src.audit.chain import chain_tip
from src.audit.hash_chain import insert_statement_sql
from src.auth.roles import PERMISSION_MATRIX, Access, Permission, Role
from src.policy_kernel.kernel import Decision, RuleId

if TYPE_CHECKING:
    import psycopg

__all__ = [
    "IRREVERSIBLE_RULE_IDS",
    "PermissionCheck",
    "check_permission",
    "record_denial",
]

#: 命中即不可逆、强制放行也必须拒绝的红线规则（架构 v5.3:199 / 附录D 规则 1a + 1b）。
IRREVERSIBLE_RULE_IDS: frozenset[str] = frozenset(
    str(rule)
    for rule in (
        RuleId.SUP_HIGH_VALUE,
        RuleId.SUP_HIGH_COMPLAINT,
        RuleId.SUP_HIGH_REFUND,
        RuleId.SUP_EXPLICIT_REFUSAL,
        RuleId.NO_CONSENT,
    )
)


@dataclass(frozen=True)
class PermissionCheck:
    """权限判定结果（不可变）：结论 + 机器可读错误码 + 命中规则标识。

    不是裸异常——越权/强制放行被拒都以本结构返回，reason 携带权限与 rule 标识。
    """

    conclusion: Decision
    reason: str
    #: 机器可读错误码（如 ``E_DENY_IRREVERSIBLE`` / ``E_RBAC_DENIED``）；允许时为 None。
    error_code: str | None = None
    #: 命中的不可逆红线 rule_id（强制放行场景）；其余场景为 None。
    rule_id: str | None = None


def _irreversible_hits(context: Mapping[str, Any]) -> tuple[str, ...]:
    """从 context 取命中规则，过滤出不可逆红线（SUP_* / NO_CONSENT）。"""
    raw = context.get("hit_rule_ids") or ()
    return tuple(str(rule) for rule in raw if str(rule) in IRREVERSIBLE_RULE_IDS)


def check_permission(
    role: Role,
    action: Permission,
    *,
    context: Mapping[str, Any] | None = None,
) -> PermissionCheck:
    """纯函数判定角色能否执行动作（无 IO，红线①）。

    ``context.hit_rule_ids`` 可携带候选命中的规则；当 ``action`` 为强制放行且命中
    ``SUP_*`` / ``NO_CONSENT`` 时，返回 ``E_DENY_IRREVERSIBLE``（架构 v5.3:199）。
    """
    ctx = context or {}

    # 红线②：强制放行命中 SUP_* / NO_CONSENT → 一律拒（含管理员）。
    if action is Permission.FORCE_RELEASE:
        hits = _irreversible_hits(ctx)
        if hits:
            return PermissionCheck(
                conclusion=Decision.DENY,
                reason=(
                    f"E_DENY_IRREVERSIBLE：强制放行命中不可逆红线 {', '.join(hits)}；"
                    f"角色 {role.value} 不可绕过 DENY（ADR-044 / 红线②）"
                ),
                error_code="E_DENY_IRREVERSIBLE",
                rule_id=hits[0],
            )

    row = PERMISSION_MATRIX.get(role)
    if row is None:
        return PermissionCheck(
            conclusion=Decision.DENY,
            reason=f"未知角色 {role.value!r}，fail-closed 拒绝（红线②）",
            error_code="E_RBAC_UNKNOWN_ROLE",
        )

    access = row.get(action)
    if access is None:
        return PermissionCheck(
            conclusion=Decision.DENY,
            reason=f"权限矩阵未登记 {role.value} 对 {action.value} 的权限，fail-closed 拒绝",
            error_code="E_RBAC_DENIED",
        )

    if access is Access.DENY:
        return PermissionCheck(
            conclusion=Decision.DENY,
            reason=(
                f"权限矩阵禁止 {role.value} 执行 {action.value}"
                "（管理员不自动蕴含下级权限，ADR-044 / K-M3）"
            ),
            error_code="E_RBAC_DENIED",
        )

    if access is Access.CONDITIONAL:
        return PermissionCheck(
            conclusion=Decision.REQUIRE_APPROVAL,
            reason=f"{role.value} 对 {action.value} 为有条件允许（⚠️ 脚注条件须另行校验）",
        )

    return PermissionCheck(
        conclusion=Decision.ALLOW,
        reason=f"{role.value} 允许执行 {action.value}",
    )


def record_denial(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    tenant_id: uuid.UUID,
    role: Role,
    action: Permission,
    check: PermissionCheck,
    trace_id: str,
    actor_id: str | None = None,
    task_id: uuid.UUID | None = None,
    worker_id: uuid.UUID | None = None,
    decision_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
    object_id: str | None = None,
) -> tuple[str, int, str]:
    """写入一条「越权/强制放行被拒」审计记录（红线③：唯一走 ``insert_statement_sql``）。

    ``record_hash`` 由数据库按同一口径算出（调用方不传）。返回 ``(audit_id, record_seq,
    record_hash)``。这是**显式 IO 函数**，不被纯判定 ``check_permission`` 调用。
    """
    # 局部 import：仅 IO 路径需要 psycopg 的 Jsonb 包装；纯判定路径不拉 psycopg。
    from psycopg.types.json import Jsonb

    prev_hash = chain_tip(conn, tenant_id)
    hit_rule_ids = [check.rule_id] if check.rule_id else []

    # HASHED_FIELDS 顺序（见 hash_chain.HASHED_FIELDS）+ prev_hash。
    values: list[object] = [
        tenant_id,
        trace_id,
        task_id,
        worker_id,
        decision_id,
        approval_id,
        "user",  # actor_type：越权多为人类角色发起
        actor_id,
        role.value,  # actor_role
        action.value,  # action_type
        "permission",  # object_type
        object_id or str(uuid.uuid4()),
        hit_rule_ids,
        "n/a",  # model_version：RBAC 拒绝不涉及模型判定
        "n/a",  # prompt_version
        "n/a",  # skill_version
        "n/a",  # rule_version
        Jsonb({"role": role.value, "action": action.value}),  # input_snapshot
        Jsonb({"reason": check.reason}),  # evidence
        Jsonb(  # payload：结论 + 错误码 + rule 标识
            {
                "decision": check.conclusion.value,
                "error_code": check.error_code,
                "rule_id": check.rule_id,
            }
        ),
        "sha256",  # hash_algo
        prev_hash,
    ]

    with conn.cursor() as cur:
        cur.execute(insert_statement_sql(), values)
        row = cur.fetchone()
    assert row is not None, "insert_statement_sql 应返回 (audit_id, record_seq, record_hash)"
    return str(row[0]), cast(int, row[1]), str(row[2])
