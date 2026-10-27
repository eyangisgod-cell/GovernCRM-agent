"""任务相关路由（T-E7）：位点查询 GET /api/v1/tasks/{task_id}/checkpoints（FR-14）。

只提供查询：写入走 `src.db.checkpoints.record_checkpoint`（图节点侧调用），
**不提供** UPDATE/DELETE（G-04：只增不改是约定）。
"""

from __future__ import annotations

import os
import uuid

import psycopg
from fastapi import APIRouter, HTTPException, Query

from src.api.schemas.task_checkpoint import TaskCheckpointOut
from src.db.checkpoints import list_checkpoints

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

DATABASE_URL_ENV = "DATABASE_URL"


def _database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV, "")
    if not url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL 未配置，位点查询不可用",
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@router.get("/{task_id}/checkpoints", response_model=list[TaskCheckpointOut])
def get_task_checkpoints(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(default=None, description="可选租户过滤"),
) -> list[TaskCheckpointOut]:
    """返回该任务的位点列表，按 checkpoint_at 升序（FR-14）。"""
    try:
        with psycopg.connect(_database_url()) as conn:
            rows = list_checkpoints(conn, task_id=task_id, tenant_id=tenant_id)
    except psycopg.Error as exc:  # pragma: no cover - 基础设施故障路径
        raise HTTPException(status_code=503, detail=f"位点查询失败：{exc}") from exc
    return [TaskCheckpointOut.model_validate(row) for row in rows]
