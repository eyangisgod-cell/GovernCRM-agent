"""审计流水（append-only + 哈希链 + 四版本号）。

哈希链字段构成（红线③）：

======================  ==========================================================
``record_seq``          链序号（``BIGSERIAL``，全局单调递增，链的遍历顺序）
``prev_hash``           前一条记录的 ``record_hash``；创世值为 64 个 ``0``
``record_hash``         本条记录摘要 = ``sha256(prev_hash || 规范化内容)``（64 位十六进制）
``hash_algo``           哈希算法标识（默认 ``sha256``）
======================  ==========================================================

触发器（迁移文件内建）拒绝 ``UPDATE`` / ``DELETE`` / ``TRUNCATE``——
**审计写入失败即阻断业务**，审计记录不可修改或删除，只能追加。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base
from .rule_ids import rule_id_subset_check


class AuditLog(Base):
    """append-only 审计流水。"""

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('system', 'user', 'agent', 'provider')",
            name="chk_audit_actor_type",
        ),
        CheckConstraint(rule_id_subset_check("hit_rule_ids"), name="chk_audit_hit_rule_ids"),
        CheckConstraint("record_hash ~ '^[0-9a-f]{64}$'", name="chk_audit_record_hash"),
        CheckConstraint("prev_hash ~ '^[0-9a-f]{64}$'", name="chk_audit_prev_hash"),
        Index("idx_audit_log_tenant_task", "tenant_id", "task_id", "record_seq"),
        Index("idx_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("idx_audit_log_tenant_trace", "tenant_id", "trace_id"),
        Index("idx_audit_log_tenant_action", "tenant_id", "action_type", "created_at"),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    record_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("decision.decision_id"), nullable=True, default=None
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approval.approval_id"), nullable=True, default=None
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    actor_role: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    object_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    hit_rule_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    skill_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    record_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash_algo: Mapped[str] = mapped_column(Text, nullable=False, default="sha256")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
