"""跨平台身份映射（三档置信度，低置信不合并）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class CustomerIdentity(Base):
    """平台 / 系统身份与匹配置信度。

    三档匹配依据（架构 v5.2 §9.5 / 裁决 #17）：

    - 高置信 ``ext_key_exact`` → 自动归集；
    - 中置信 ``phone_hash_address_fingerprint`` → 进人工复核队列（E4），复核前不归集；
    - 低置信 ``no_feature`` → **保留独立身份**（PRD P1 红线，数据库 CHECK 强制不得挂客户）。
    """

    __tablename__ = "customer_identity"
    __table_args__ = (
        CheckConstraint(
            "match_basis IN ('ext_key_exact', 'phone_hash_address_fingerprint', 'no_feature')",
            name="chk_customer_identity_basis",
        ),
        CheckConstraint(
            "confidence_tier IN ('high', 'medium', 'low')",
            name="chk_customer_identity_tier",
        ),
        CheckConstraint(
            "review_status IN ('not_required', 'pending_review', 'confirmed', 'rejected')",
            name="chk_customer_identity_review_status",
        ),
        CheckConstraint(
            "confidence_tier <> 'low' OR customer_id IS NULL",
            name="chk_customer_identity_low_not_merged",
        ),
        UniqueConstraint(
            "tenant_id",
            "platform",
            "shop_id",
            "ext_key",
            name="uq_customer_identity_platform_shop_extkey",
        ),
        Index("idx_customer_identity_tenant_customer", "tenant_id", "customer_id"),
        Index("idx_customer_identity_tenant_review", "tenant_id", "review_status", "created_at"),
    )

    identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=True, default=None
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    ext_key: Mapped[str] = mapped_column(Text, nullable=False)
    match_basis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_tier: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 4), nullable=True, default=None
    )
    phone_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    address_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="not_required")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
