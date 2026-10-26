"""审批责任链（任务级批量，责任链绑定批次快照）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class Approval(Base):
    """审批责任链。

    三档路由（裁决 #25）：

    - ``tier_1`` 影响面 ≤10 → 运营负责人单签；
    - ``tier_2`` 影响面 11–100 → 负责人 + 管理员双签；
    - ``tier_3`` 影响面 >100 → 双签 + 合规知会（合规**只读知会**，不是签署方）。

    超时策略：48h 失效、剩余 24h 升级提醒（``timeout_policy``）。
    批次快照一经生成不可改（ADR-032）。
    """

    __tablename__ = "approval"
    __table_args__ = (
        CheckConstraint(
            "impact_tier IN ('tier_1', 'tier_2', 'tier_3')",
            name="chk_approval_impact_tier",
        ),
        CheckConstraint(
            "status IN ('pending', 'signed', 'rejected', 'expired', 'cancelled')",
            name="chk_approval_status",
        ),
        CheckConstraint("impact_customer_count >= 0", name="chk_approval_impact_count"),
        CheckConstraint("required_signatures >= 1", name="chk_approval_required_signatures"),
        CheckConstraint(
            "escalate_remind_at IS NULL OR escalate_remind_at <= expires_at",
            name="chk_approval_escalate_before_expire",
        ),
        Index("idx_approval_tenant_batch", "tenant_id", "batch_id", "status"),
        Index("idx_approval_tenant_status", "tenant_id", "status", "expires_at"),
    )

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("decision.decision_id"), nullable=True, default=None
    )
    candidate_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)
    impact_tier: Mapped[str] = mapped_column(Text, nullable=False)
    impact_customer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    route_signers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    required_signatures: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    applicant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    applicant_role: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    approver_role: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    signed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    timeout_policy: Mapped[str] = mapped_column(
        Text, nullable=False, default="expire_48h_escalate_24h"
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    escalate_remind_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMPTZ, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
