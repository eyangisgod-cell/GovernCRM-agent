"""动作候选（冲突消解的输入：同客户多候选共存）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class ActionCandidate(Base):
    """Agent 产出的动作候选：**未过闸门不得执行**。

    - ``benefit_net_value`` = 附录 D 规则 5 的折算净值（``面额 − max(0, 门槛 − 客单价)``，下限 0）；
    - ``platform_layer_score`` 是附录 D 规则 3 键①的取值（全局层级是客户级属性，
      同客户的多平台候选必然相同，无法区分候选）。
    """

    __tablename__ = "action_candidate"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'suppressed', 'executed', 'cancelled')",
            name="chk_action_candidate_status",
        ),
        CheckConstraint(
            "benefit_net_value IS NULL OR benefit_net_value >= 0",
            name="chk_action_candidate_benefit_net",
        ),
        Index("idx_action_candidate_tenant_customer", "tenant_id", "customer_id", "created_at"),
        Index("idx_action_candidate_tenant_task", "tenant_id", "task_id", "created_at"),
        Index("idx_action_candidate_tenant_status", "tenant_id", "status", "created_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_template_code: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    coupon_ref: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    benefit_face_value: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True, default=None
    )
    benefit_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True, default=None
    )
    benefit_net_value: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True, default=None
    )
    global_layer_score: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    platform_layer_score: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
