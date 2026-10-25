"""未触达记录（治理看板的计量来源）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import TIMESTAMPTZ, Base
from .rule_ids import REGISTERED_RULE_IDS


def _rule_id_in_list() -> str:
    literal = ", ".join(f"'{rule_id}'" for rule_id in REGISTERED_RULE_IDS)
    return f"rule_id IN ({literal})"


class SuppressionRecord(Base):
    """未触达记录（含原因）。

    - **批量判定阶段单客户 DENY 属正常业务结果**，正常写入本表，**不计入 deny_retry**
      （裁决 #9 / ADR-036）→ ``deny_retry_counted`` 恒为 ``False``；
    - 频控延迟类（``FREQ_GLOBAL_7D``）返回冷却剩余秒数，供恢复后重排。
    """

    __tablename__ = "suppression_record"
    __table_args__ = (
        CheckConstraint(_rule_id_in_list(), name="chk_suppression_rule_id"),
        CheckConstraint(
            "cooling_remaining_seconds IS NULL OR cooling_remaining_seconds >= 0",
            name="chk_suppression_cooling",
        ),
        CheckConstraint(
            "channel_cooling_remaining_seconds IS NULL OR channel_cooling_remaining_seconds >= 0",
            name="chk_suppression_channel_cooling",
        ),
        Index("idx_suppression_record_tenant_rule", "tenant_id", "rule_id", "suppressed_at"),
        Index(
            "idx_suppression_record_tenant_customer",
            "tenant_id",
            "customer_id",
            "suppressed_at",
        ),
    )

    suppression_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("decision.decision_id"), nullable=True, default=None
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("action_candidate.candidate_id"), nullable=True, default=None
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customer.customer_id"), nullable=False
    )
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("worker.worker_id"), nullable=True, default=None
    )
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    channel: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cooling_remaining_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    channel_cooling_remaining_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    deny_retry_counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suppressed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
