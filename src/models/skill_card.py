"""技能卡（版本化 + draft/published 双门禁）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class SkillCard(Base):
    """技能卡片（版本化）。

    状态机（裁决 #10a）：

    - ``draft`` —— 轻量蒸馏产出的草稿卡，**不自动进入运行中员工技能包**；
    - ``published`` —— 须「技能回归评测通过 + 人工确认」**双门禁**方可发布
      （数据库 CHECK 强制）；
    - ``deprecated`` —— 废弃版本（保留可回放）。

    ``owner_worker_id`` 为 NULL 表示平台级共享技能卡（v5.3 附录 E §E.4）。
    """

    __tablename__ = "skill_card"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'deprecated')", name="chk_skill_card_status"
        ),
        CheckConstraint(
            "status <> 'published' OR (regression_passed AND human_confirmed)",
            name="chk_skill_card_publish_gates",
        ),
        UniqueConstraint("tenant_id", "skill_code", "version", name="uq_skill_card_code_version"),
        Index("idx_skill_card_tenant_owner", "tenant_id", "owner_worker_id", "status"),
        Index("idx_skill_card_tenant_code", "tenant_id", "skill_code", "version"),
    )

    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    skill_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    skill_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    regression_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    human_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
