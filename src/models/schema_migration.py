"""迁移台账（框架自身辅助表）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base


class SchemaMigration(Base):
    """已应用的迁移版本台账。

    由各迁移文件在末尾自登记（``INSERT ... ON CONFLICT DO UPDATE``），
    语义上 append-only：只追加新版本，不改历史行。
    """

    __tablename__ = "schema_migration"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    applied_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
