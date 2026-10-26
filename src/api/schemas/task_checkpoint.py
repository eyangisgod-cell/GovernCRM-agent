"""位点接口的响应模型（T-E7 · FR-14）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TaskCheckpointOut(BaseModel):
    """``GET /api/v1/tasks/{task_id}/checkpoints`` 的单条位点。

    字段与 ``task_checkpoint`` 表列一一对应（无 snapshot 大字段，G-04）。
    """

    model_config = ConfigDict(from_attributes=True)

    checkpoint_id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    node_name: str
    checkpoint_at: datetime
    decision_id: uuid.UUID | None = None
    langgraph_checkpoint_id: str | None = None
