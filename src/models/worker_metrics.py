"""数字员工执行指标聚合（晋级校验 G1/G2 + 运营效率四项口径）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class WorkerMetrics(Base):
    """数字员工执行指标聚合。

    统计粒度 = 「同类型动作」= ``action_type + channel`` 组合（裁决 #22），
    再按日聚合。模拟成功回执（``status=success``）计入 G1/G2，口径由
    ``contains_synthetic`` 标注（数据带 ``_synthetic: true``）。
    """

    __tablename__ = "worker_metrics"
    __table_args__ = (
        CheckConstraint(
            "autonomy_level IN ('L0', 'L1', 'L2', 'L3')",
            name="chk_worker_metrics_autonomy",
        ),
        CheckConstraint(
            "execution_count >= 0 AND success_count >= 0 AND correction_count >= 0 "
            "AND candidate_count >= 0 AND adopted_count >= 0",
            name="chk_worker_metrics_counts",
        ),
        UniqueConstraint(
            "tenant_id",
            "worker_id",
            "stat_date",
            "action_type",
            "channel",
            name="uq_worker_metrics_grain",
        ),
        Index("idx_worker_metrics_tenant_date", "tenant_id", "stat_date"),
        Index("idx_worker_metrics_tenant_worker", "tenant_id", "worker_id", "stat_date"),
    )

    metric_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=False
    )
    stat_date: Mapped[date] = mapped_column(nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False, default="L0")
    execution_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adopted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_to_send_avg_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None
    )
    contains_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
