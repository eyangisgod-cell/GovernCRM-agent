"""成本记录（按员工 / 任务 / 日聚合）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class CostRecord(Base):
    """成本记录。

    预算默认值（裁决 #27）：单任务 50 万 Token / ¥20（先到为准），单员工日 ¥200。
    MVP 期间超预算以审计日志可查，成本看板 UI 后置（PRD FR-36 缩量）。
    """

    __tablename__ = "cost_record"
    __table_args__ = (
        CheckConstraint("token_count >= 0 AND cost_amount >= 0", name="chk_cost_record_counts"),
        Index("idx_cost_record_tenant_worker_date", "tenant_id", "worker_id", "stat_date"),
        Index("idx_cost_record_tenant_task", "tenant_id", "task_id", "stat_date"),
    )

    cost_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    stat_date: Mapped[date] = mapped_column(nullable=False)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal("0")
    )
    cost_currency: Mapped[str] = mapped_column(Text, nullable=False, default="CNY")
    budget_cap_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
