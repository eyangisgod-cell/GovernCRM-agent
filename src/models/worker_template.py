"""数字员工模板（v5.3 附录 E §E.3）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class WorkerTemplate(Base):
    """数字员工模板：模板 != 实例。

    内置两个模板（PRD 7.3，MVP 不新增）：

    - ``SLEEPING_WAKEUP_SPECIALIST`` 沉睡唤醒专员 —— MVP 唯一可运行员工（裁决 #3）；
    - ``AFTER_SALE_CARE_SPECIALIST``  售后关怀专员 —— ``runnable=false``，可创建不可派发。
    """

    __tablename__ = "worker_template"
    __table_args__ = (
        CheckConstraint(
            "default_autonomy_level IN ('L0', 'L1', 'L2', 'L3')",
            name="chk_worker_template_autonomy",
        ),
        CheckConstraint(
            "(is_builtin AND tenant_id IS NULL) OR (NOT is_builtin)",
            name="chk_worker_template_builtin_scope",
        ),
        Index("idx_worker_template_tenant_code", "tenant_id", "code"),
        # 平台内置模板的 code 全局唯一（tenant_id 为 NULL，故用部分唯一索引）
        Index(
            "idx_worker_template_builtin_code",
            "code",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    default_skill_binding: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    default_strategy_binding: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    default_autonomy_level: Mapped[str] = mapped_column(Text, nullable=False)
    runnable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
