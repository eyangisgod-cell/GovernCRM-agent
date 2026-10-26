"""数字员工实例（v5.3 附录 E §E.2）。

五要素（PRD 7.1）：角色定义 + 技能包 + 策略包 + 记忆空间 + 自主度等级。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class Worker(Base):
    """数字员工实例。

    - ``skill_binding`` / ``strategy_binding`` **只存绑定关系与版本号**，不存技能内容与规则内容；
    - ``strategy_binding`` 是「覆盖」不是「替换」，红线键在写入期即被拒绝；
    - ``memory_scope_key`` 固定为 ``{tenant_id}:{worker_id}``（数据库 CHECK 强制）。
    """

    __tablename__ = "worker"
    __table_args__ = (
        CheckConstraint(
            "autonomy_level IN ('L0', 'L1', 'L2', 'L3')",
            name="chk_worker_autonomy",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'suspended', 'archived')",
            name="chk_worker_status",
        ),
        CheckConstraint(
            "memory_scope_key = tenant_id::text || ':' || worker_id::text",
            name="chk_worker_memory_scope_key",
        ),
        CheckConstraint(
            "NOT (strategy_binding ?| ARRAY["
            "'suppression', 'suppress', 'redline', 'deny', "
            "'no_consent', 'consent', 'exemption'])",
            name="chk_worker_strategy_binding_no_redline",
        ),
        Index("idx_worker_tenant_status", "tenant_id", "status", "priority"),
        Index("idx_worker_tenant_template", "tenant_id", "template_id"),
        UniqueConstraint("memory_scope_key", name="uq_worker_memory_scope_key"),
    )

    worker_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker_template.template_id"), nullable=True, default=None
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False, default="L0")
    skill_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    strategy_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    memory_scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
