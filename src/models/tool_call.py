"""工具调用记录（含幂等键、成本、trace_id）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class ToolCall(Base):
    """工具调用记录。

    ``tool_call_type='external_send'`` 的调用**必须过治理闸门**——智能体不是后门
    （v5.3 附录 G §G.2.2）。幂等键在租户内唯一。
    """

    __tablename__ = "tool_call"
    __table_args__ = (
        CheckConstraint(
            "tool_call_type IN ('external_send', 'read', 'write', 'internal')",
            name="chk_tool_call_type",
        ),
        CheckConstraint(
            "status IN ('success', 'failed', 'timeout', 'denied')",
            name="chk_tool_call_status",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tool_call_idempotency"),
        Index("idx_tool_call_tenant_task", "tenant_id", "task_id", "created_at"),
        Index("idx_tool_call_tenant_trace", "tenant_id", "trace_id"),
    )

    tool_call_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call_type: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="success")
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, default=None)
    cost_currency: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cost_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
