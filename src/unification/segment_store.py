"""九宫格分层结果落库（``customer_segment``）。

与纯计算层分离：只有本模块允许 import ORM（``src.models``）。
``src/unification/__init__.py`` **不得** import 本模块——否则会把 SQLAlchemy
带进策略内核（红线 1：``.trellis/spec/backend/quality-guidelines.md``）。

写入前自检（fail-closed）：
- ``scope`` ∈ {global, platform_local}；``platform_local`` 必须带 ``platform``
  （表约束 ``chk_customer_segment_scope_platform``）；
- ``layer_score == amount_score * 10 + recency_score``——与库里的 CHECK
  ``chk_customer_segment_layer_score`` 同一口径（库侧乘数写死为 10，
  与 ``governance.yaml`` 出厂值一致；若有人改了配置乘数，这里先报错而不是让库拒收）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from src.models.customer_segment import CustomerSegment
from src.unification.segmentation import (
    SCOPE_GLOBAL,
    SCOPE_PLATFORM_LOCAL,
    SEGMENT_RULE_CODE,
    CustomerLayers,
    LayerProfile,
    SegmentationError,
)

__all__ = ["build_segment_rows", "persist_segments"]

#: 库侧 CHECK 写死的 layer_score 乘数（``chk_customer_segment_layer_score``）。
_DB_LAYER_SCORE_BASE = 10


def _row(
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    profile: LayerProfile,
    rule_version: str,
    effective_at: datetime,
    segment_rule_code: str = SEGMENT_RULE_CODE,
) -> CustomerSegment:
    segment = profile.segment
    if segment.scope not in (SCOPE_GLOBAL, SCOPE_PLATFORM_LOCAL):
        raise SegmentationError(
            f"落库拒绝：未知分层范围 {segment.scope!r}（表约束 chk_customer_segment_scope）。"
        )
    if segment.scope == SCOPE_PLATFORM_LOCAL and not segment.platform:
        raise SegmentationError(
            "落库拒绝：平台局部层缺 platform（表约束 chk_customer_segment_scope_platform）。"
        )
    expected_score = segment.amount_score * _DB_LAYER_SCORE_BASE + segment.recency_score
    if segment.layer_score != expected_score:
        raise SegmentationError(
            "落库拒绝：layer_score 与库约束不一致"
            f"（算出 {segment.layer_score}，库要求 {expected_score} = "
            f"amount_score * {_DB_LAYER_SCORE_BASE} + recency_score）。\n"
            "  说明 `segmentation.layer_score_base` 被改成了非 10 的值，"
            "而库侧 CHECK 写死乘数 10——两者必须一致（改配置要走审批 + 迁移）。"
        )
    return CustomerSegment(
        tenant_id=tenant_id,
        customer_id=customer_id,
        scope=segment.scope,
        platform=segment.platform,
        segment_rule_code=segment.segment_rule_code or segment_rule_code,
        rule_version=rule_version,
        layer_score=segment.layer_score,
        amount_score=segment.amount_score,
        recency_score=segment.recency_score,
        amount_tier=segment.amount_tier,
        recency_tier=segment.recency_tier,
        dimension_values={
            "layer_name": segment.layer_name,
            "avg_order_value_cents": profile.metrics.avg_order_value_cents,
            "days_since_last_purchase": profile.metrics.days_since_last_purchase,
        },
        effective_at=effective_at,
        created_at=effective_at,
    )


def build_segment_rows(
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    layers: CustomerLayers,
    rule_version: str,
    effective_at: datetime,
    segment_rule_code: str = SEGMENT_RULE_CODE,
) -> list[CustomerSegment]:
    """把分层结果转成待落库的行（**不写库**），供调用方审阅后再写。"""
    profiles = [layers.global_profile, *layers.platform_profiles.values()]
    return [
        _row(
            tenant_id=tenant_id,
            customer_id=customer_id,
            profile=profile,
            rule_version=rule_version,
            effective_at=effective_at,
            segment_rule_code=segment_rule_code,
        )
        for profile in profiles
    ]


def persist_segments(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    layers: CustomerLayers,
    rule_version: str,
    effective_at: datetime,
    segment_rule_code: str = SEGMENT_RULE_CODE,
) -> list[CustomerSegment]:
    """写入分层结果并 flush（提交由调用方决定）。"""
    rows = build_segment_rows(
        tenant_id=tenant_id,
        customer_id=customer_id,
        layers=layers,
        rule_version=rule_version,
        effective_at=effective_at,
        segment_rule_code=segment_rule_code,
    )
    session.add_all(rows)
    session.flush()
    return rows
