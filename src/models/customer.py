"""统一客户（无明文 PII 列 + 知情同意三字段）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class Customer(Base):
    """统一客户。

    - **无明文 PII 列**（PRD C1）：手机号 / 邮箱 / 地址一律哈希或指纹存储；
    - **知情同意三字段**（PRD C4 / 裁决 #15）：发送前强制校验，无授权 → ``NO_CONSENT`` → DENY；
    - ``(platform, shop_id, ext_key)`` 唯一。
    """

    __tablename__ = "customer"
    __table_args__ = (
        CheckConstraint(
            "consent_status IN ('granted', 'denied', 'expired', 'revoked', 'unknown')",
            name="chk_customer_consent_status",
        ),
        CheckConstraint(
            "status IN ('active', 'merged', 'archived')",
            name="chk_customer_status",
        ),
        UniqueConstraint("platform", "shop_id", "ext_key", name="uq_customer_platform_shop_extkey"),
        Index("idx_customer_tenant_platform", "tenant_id", "platform", "shop_id"),
        Index("idx_customer_tenant_consent", "tenant_id", "consent_status"),
        Index("idx_customer_tenant_synthetic", "tenant_id", "_synthetic"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    ext_key: Mapped[str] = mapped_column(Text, nullable=False)
    alias: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    phone_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    email_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    address_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    consent_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    consent_time: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    consent_source: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_order_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    avg_order_value_90d: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True, default=None
    )
    complaint_count_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_refund_dispute: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refund_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True, default=None)
    explicit_refusal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_top_value: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    _synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
