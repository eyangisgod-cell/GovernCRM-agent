"""情景记忆（结构先留，逻辑后置 v0.3）。

按 v5.3 附录 E §E.4：``scope_key`` 引用 ``worker.memory_scope_key``，
记忆由「按租户 + 角色隔离」升级为「按租户 + **数字员工**隔离」——
多员工场景下不同员工的记忆不得互相污染。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class MemoryEpisode(Base):
    """情景记忆（脱敏后存储）。"""

    __tablename__ = "memory_episode"
    __table_args__ = (
        CheckConstraint(
            "trust_level IN ('high', 'medium', 'low', 'unknown')",
            name="chk_memory_episode_trust_level",
        ),
        CheckConstraint(
            "gate_status IN ('accepted', 'quarantined', 'rejected')",
            name="chk_memory_episode_gate_status",
        ),
        Index("idx_memory_episode_tenant_scope", "tenant_id", "scope_key", "created_at"),
    )

    episode_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=True, default=None
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    gate_status: Mapped[str] = mapped_column(Text, nullable=False, default="accepted")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
