"""策略内核判定结果（命中规则 ID + 四版本号 + 证据快照）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base
from .rule_ids import rule_id_subset_check


class Decision(Base):
    """判定结果。

    - ``hit_rule_ids`` 与 ``suppressed_rule_ids`` **分开记**（附录 D §4 尾注）；
    - ``LAYER_RESOLVE`` / ``BENEFIT_MAX`` 是过程性记录，写进 ``evidence``，
      不独立产生 DENY / ALLOW 判定；
    - 含 ``PATH_ASSERT`` 类判定（路径完整性断言缺失即 DENY）；
    - 四版本号（``model_version`` / ``prompt_version`` / ``skill_version`` / ``rule_version``）
      随判定一并落库，保证可回放。
    """

    __tablename__ = "decision"
    __table_args__ = (
        CheckConstraint(
            "conclusion IN ('ALLOW', 'REQUIRE_APPROVAL', 'DENY')",
            name="chk_decision_conclusion",
        ),
        CheckConstraint(rule_id_subset_check("hit_rule_ids"), name="chk_decision_hit_rule_ids"),
        CheckConstraint(
            rule_id_subset_check("suppressed_rule_ids"), name="chk_decision_suppressed_rule_ids"
        ),
        Index("idx_decision_tenant_customer", "tenant_id", "customer_id", "created_at"),
        Index("idx_decision_tenant_task", "tenant_id", "task_id", "created_at"),
        Index("idx_decision_tenant_conclusion", "tenant_id", "conclusion", "created_at"),
        Index("idx_decision_tenant_hit_rules", "hit_rule_ids", postgresql_using="gin"),
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("action_candidate.candidate_id"), nullable=True, default=None
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=True, default=None
    )
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    hit_rule_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    suppressed_rule_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    skill_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    decided_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
