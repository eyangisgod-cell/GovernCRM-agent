"""分层结果（全局九宫格 + 平台局部九宫格两套）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class CustomerSegment(Base):
    """分层结果。

    ``layer_score = amount_score × 10 + recency_score``，取值 11..33，
    数值大者层级更高（附录 D 规则 3）。全局层与平台局部层是**两套独立层级体系**，
    不存在「全局层级恒大于平台层级」的固定序（附录 D 规则 4）。
    """

    __tablename__ = "customer_segment"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('global', 'platform_local')",
            name="chk_customer_segment_scope",
        ),
        CheckConstraint(
            "layer_score = amount_score * 10 + recency_score",
            name="chk_customer_segment_layer_score",
        ),
        CheckConstraint("amount_score IN (1, 2, 3)", name="chk_customer_segment_amount_score"),
        CheckConstraint("recency_score IN (1, 2, 3)", name="chk_customer_segment_recency_score"),
        CheckConstraint(
            "amount_tier IN ('high', 'mid', 'low')", name="chk_customer_segment_amount_tier"
        ),
        CheckConstraint(
            "recency_tier IN ('recent', 'mid', 'stale')",
            name="chk_customer_segment_recency_tier",
        ),
        CheckConstraint(
            "scope <> 'platform_local' OR platform IS NOT NULL",
            name="chk_customer_segment_scope_platform",
        ),
        Index("idx_customer_segment_tenant_customer", "tenant_id", "customer_id", "scope"),
        Index(
            "idx_customer_segment_tenant_score",
            "tenant_id",
            "scope",
            "layer_score",
            "created_at",
        ),
    )

    segment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    segment_rule_code: Mapped[str] = mapped_column(Text, nullable=False, default="NINE_GRID")
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    layer_score: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_score: Mapped[int] = mapped_column(Integer, nullable=False)
    recency_score: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_tier: Mapped[str] = mapped_column(Text, nullable=False)
    recency_tier: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
