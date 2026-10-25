"""记忆门禁拒绝的隔离内容（结构先留，逻辑后置 v0.3）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class MemoryQuarantine(Base):
    """隔离内容：拒绝原因 + 原文 + 人工审查状态。

    按客户删除时联动清理（PRD C6）。
    """

    __tablename__ = "memory_quarantine"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'dismissed', 'deleted')",
            name="chk_memory_quarantine_review_status",
        ),
        Index("idx_memory_quarantine_tenant_status", "tenant_id", "review_status", "created_at"),
    )

    quarantine_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=True, default=None
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memory_episode.episode_id"), nullable=True, default=None
    )
    reject_reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
