"""跨平台身份匹配引擎（三档置信度，纯代码、零 LLM）。

权威依据（可反查）：

- **架构 v5.2 §9.5**「身份匹配三档算法」（裁决 #17 / ADR-037）——
  高置信：跨平台 ``ext_key`` 精确匹配 → **自动归集**为同一 customer；
  中置信：手机号哈希 + 收件地址指纹**部分匹配** → 进**人工复核队列**（E4），
  **复核前不归集**；低置信：无匹配特征 → **保留独立身份**（PRD P1 红线）。
- **PRD P1 红线**「不得强行合并」——低置信绝不合并。
- **PRD P3 红线**——归集仅用于抑制与频控，**不用于跨平台营销投放**。
- **数据库 CHECK** ``chk_customer_identity_low_not_merged``——低置信身份不得指向已归集客户。

本模块分两层（同一文件，便于对照）：

- **纯判定层**（``classify_identity``）：纯函数、零 I/O、零时钟依赖、零随机，
  同输入必同输出——只回答「这条平台身份属于哪一档、依据是什么」。
- **落库层**（``register_identity`` / ``resolve_unified_customer`` /
  ``list_pending_review``）：把判定结果写进 ``customer_identity``
  （``match_basis`` / ``confidence_tier`` / ``confidence_score``），可回放。

**零 LLM / 零供应商依赖**（红线 ①⑤）：本模块只 import 标准库 + SQLAlchemy + ORM 模型，
不接触任何模型 SDK；``tests/unification/identity/test_identity.py`` 有静态扫描守卫。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.customer import Customer
from src.models.customer_identity import CustomerIdentity

__all__ = [
    "ConfidenceTier",
    "IdentityRecord",
    "MatchBasis",
    "MatchOutcome",
    "RegistrationResult",
    "ReviewStatus",
    "classify_identity",
    "list_pending_review",
    "register_identity",
    "resolve_unified_customer",
]


class MatchBasis(StrEnum):
    """匹配依据（与 ``chk_customer_identity_basis`` 逐字一致）。"""

    EXT_KEY_EXACT = "ext_key_exact"
    PHONE_HASH_ADDRESS_FINGERPRINT = "phone_hash_address_fingerprint"
    NO_FEATURE = "no_feature"


class ConfidenceTier(StrEnum):
    """置信档位（与 ``chk_customer_identity_tier`` 逐字一致）。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(StrEnum):
    """人工复核状态（与 ``chk_customer_identity_review_status`` 逐字一致）。"""

    NOT_REQUIRED = "not_required"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


#: 可参与部分匹配的特征（**不含明文 PII**，只有哈希 / 指纹——PRD C1）。
_FEATURE_FIELDS: tuple[str, ...] = ("phone_hash", "address_fingerprint")

_HIGH_SCORE = Decimal("1.0000")
_MEDIUM_SCORE_ONE_FEATURE = Decimal("0.5000")
_MEDIUM_SCORE_BOTH_FEATURES = Decimal("0.7500")
_LOW_SCORE = Decimal("0.0000")

#: 客户状态：已归集到统一客户的平台客户标记为 ``merged``（``chk_customer_status``）。
_CUSTOMER_STATUS_MERGED = "merged"


@dataclass(frozen=True)
class IdentityRecord:
    """一条平台身份（归集前）。

    - ``platform`` / ``shop_id`` / ``ext_key`` —— 平台内身份坐标；
    - ``phone_hash`` / ``address_fingerprint`` —— **哈希 / 指纹**，不是明文 PII；
    - ``customer_id`` —— 已归集到的统一客户（``None`` = 未归集）；
    - ``identity_id`` —— 已落库的 ``customer_identity`` 行（纯判定层可为空）。
    """

    tenant_id: uuid.UUID
    platform: str
    shop_id: str
    ext_key: str
    phone_hash: str | None = None
    address_fingerprint: str | None = None
    customer_id: uuid.UUID | None = None
    identity_id: uuid.UUID | None = None


@dataclass(frozen=True)
class MatchOutcome:
    """三档判定结果（不可变，可落库回放）。

    - ``match_basis`` / ``confidence_tier`` / ``confidence_score`` —— 落 ``customer_identity``；
    - ``customer_id`` —— 归集目标（**``None`` = 未归集**：低置信永不归集、中置信复核前不归集）；
    - ``matched_*`` —— 命中的对端身份坐标与命中的特征（供审计回放）。
    """

    match_basis: MatchBasis
    confidence_tier: ConfidenceTier
    confidence_score: Decimal
    review_status: ReviewStatus
    customer_id: uuid.UUID | None = None
    matched_platform: str | None = None
    matched_shop_id: str | None = None
    matched_ext_key: str | None = None
    matched_identity_id: uuid.UUID | None = None
    matched_features: tuple[str, ...] = ()

    @property
    def is_merged(self) -> bool:
        """是否已归集到统一客户。"""
        return self.customer_id is not None


@dataclass(frozen=True)
class RegistrationResult:
    """登记结果。

    - ``identity_id`` —— 落库的 ``customer_identity`` 行；
    - ``outcome`` —— 三档判定结果；
    - ``unified_customer_id`` —— **归集主体**（``None`` = 未归集）。
      未归集时抑制 / 频控仍可用 ``resolve_unified_customer`` 取本平台客户作为管控键。
    """

    identity_id: uuid.UUID
    outcome: MatchOutcome
    unified_customer_id: uuid.UUID | None

    @property
    def match_basis(self) -> MatchBasis:
        return self.outcome.match_basis

    @property
    def confidence_tier(self) -> ConfidenceTier:
        return self.outcome.confidence_tier

    @property
    def confidence_score(self) -> Decimal:
        return self.outcome.confidence_score

    @property
    def review_status(self) -> ReviewStatus:
        return self.outcome.review_status


# ─────────────────────────────────────────────────────────────────────────────
# 一、纯判定层
# ─────────────────────────────────────────────────────────────────────────────


def _feature_value(record: IdentityRecord, field_name: str) -> str | None:
    """取特征值（空串视作缺失）。"""
    values: dict[str, str | None] = {
        "phone_hash": record.phone_hash,
        "address_fingerprint": record.address_fingerprint,
    }
    return values[field_name] or None


def _peer_sort_key(record: IdentityRecord) -> tuple[str, str, str, str]:
    """对端身份的确定性排序键——保证「命中谁」与输入顺序无关。"""
    return (record.platform, record.shop_id, record.ext_key, str(record.identity_id or ""))


def classify_identity(candidate: IdentityRecord, peers: Sequence[IdentityRecord]) -> MatchOutcome:
    """判定一条平台身份的置信档位（**纯函数**：不连库、不取时钟、不随机）。

    求值顺序与架构 v5.2 §9.5 三档一致：

    ① **高置信** ``ext_key_exact``：跨平台 ``ext_key`` 精确相等 → 自动归集；
    ② **中置信** ``phone_hash_address_fingerprint``：手机号哈希 / 收件地址指纹**部分匹配**
       → 进人工复核队列（E4），**复核前不归集**；
    ③ **低置信** ``no_feature``：无匹配特征 → **保留独立身份**（P1 红线）。

    只在**跨平台**之间匹配：同平台身份不是「归集」问题（平台内 ``ext_key`` 唯一），
    跨租户身份一律不匹配（租户隔离在数据接入层强制）。
    """
    cross_platform = [
        peer
        for peer in peers
        if peer.tenant_id == candidate.tenant_id
        and peer.platform != candidate.platform
        and (peer.platform, peer.shop_id, peer.ext_key)
        != (candidate.platform, candidate.shop_id, candidate.ext_key)
    ]
    ordered = sorted(cross_platform, key=_peer_sort_key)

    # ① 高置信：跨平台 ext_key 精确相等
    for peer in ordered:
        if candidate.ext_key and peer.ext_key == candidate.ext_key:
            return MatchOutcome(
                match_basis=MatchBasis.EXT_KEY_EXACT,
                confidence_tier=ConfidenceTier.HIGH,
                confidence_score=_HIGH_SCORE,
                review_status=ReviewStatus.NOT_REQUIRED,
                customer_id=peer.customer_id,
                matched_platform=peer.platform,
                matched_shop_id=peer.shop_id,
                matched_ext_key=peer.ext_key,
                matched_identity_id=peer.identity_id,
            )

    # ② 中置信：手机号哈希 / 地址指纹部分匹配（命中最多的对端优先）
    best_peer: IdentityRecord | None = None
    best_features: tuple[str, ...] = ()
    for peer in ordered:
        features = tuple(
            field_name
            for field_name in _FEATURE_FIELDS
            if _feature_value(candidate, field_name) is not None
            and _feature_value(candidate, field_name) == _feature_value(peer, field_name)
        )
        if len(features) > len(best_features):
            best_peer = peer
            best_features = features
    if best_peer is not None:
        score = (
            _MEDIUM_SCORE_BOTH_FEATURES if len(best_features) >= 2 else _MEDIUM_SCORE_ONE_FEATURE
        )
        return MatchOutcome(
            match_basis=MatchBasis.PHONE_HASH_ADDRESS_FINGERPRINT,
            confidence_tier=ConfidenceTier.MEDIUM,
            confidence_score=score,
            review_status=ReviewStatus.PENDING_REVIEW,
            customer_id=None,
            matched_platform=best_peer.platform,
            matched_shop_id=best_peer.shop_id,
            matched_ext_key=best_peer.ext_key,
            matched_identity_id=best_peer.identity_id,
            matched_features=best_features,
        )

    # ③ 低置信：无匹配特征 → 保留独立身份（永不归集）
    return MatchOutcome(
        match_basis=MatchBasis.NO_FEATURE,
        confidence_tier=ConfidenceTier.LOW,
        confidence_score=_LOW_SCORE,
        review_status=ReviewStatus.NOT_REQUIRED,
        customer_id=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 二、落库层
# ─────────────────────────────────────────────────────────────────────────────


def register_identity(
    session: Session, candidate: IdentityRecord, *, now: datetime
) -> RegistrationResult:
    """登记一条平台身份：判定三档 → 落 ``customer_identity`` → 高置信时归集。

    - **高置信**：两个平台身份归集到同一 ``customer``（对端身份行同步升级为高置信）；
    - **中置信**：``review_status='pending_review'`` 且 ``customer_id IS NULL``；
    - **低置信**：``customer_id IS NULL``（**未被合并**，P1 红线 + 数据库 CHECK）。

    ``now`` 由调用方传入（**不取服务端时钟**）：判定与落库都可复现。
    同一平台身份重复登记是幂等的（唯一约束
    ``uq_customer_identity_platform_shop_extkey``）。
    """
    peers = _load_peer_records(session, candidate)
    outcome = classify_identity(candidate, peers)

    # 高置信归集复用已归集 ID（拍板④）：不新建 customer 行，直接指向对端归集主体。
    # 中/低置信仍需一行平台 customer 作为「未归集时的管控键载体」。
    if outcome.confidence_tier is ConfidenceTier.HIGH:
        platform_customer = _load_customer(
            session, candidate.tenant_id, candidate.platform, candidate.shop_id, candidate.ext_key
        )
    else:
        platform_customer = _ensure_platform_customer(session, candidate, now)
    identity = _load_or_create_identity(session, candidate, now)

    identity.phone_hash = candidate.phone_hash
    identity.address_fingerprint = candidate.address_fingerprint
    identity.match_basis = outcome.match_basis.value
    identity.confidence_tier = outcome.confidence_tier.value
    identity.confidence_score = outcome.confidence_score
    identity.review_status = outcome.review_status.value
    identity.updated_at = now

    unified_customer_id: uuid.UUID | None = None
    if outcome.confidence_tier is ConfidenceTier.HIGH:
        unified_customer_id = _aggregate(session, candidate, outcome, platform_customer, now)
        identity.customer_id = unified_customer_id
    else:
        # 中置信复核前不归集；低置信永不归集（数据库 CHECK 同样强制低置信为 NULL）。
        identity.customer_id = None

    session.flush()
    return RegistrationResult(
        identity_id=identity.identity_id,
        outcome=outcome,
        unified_customer_id=unified_customer_id,
    )


def resolve_unified_customer(
    session: Session,
    tenant_id: uuid.UUID,
    platform: str,
    shop_id: str,
    ext_key: str,
) -> uuid.UUID | None:
    """取抑制 / 频控用的**管控键**（统一客户 id）。

    - 已归集（高置信）→ 返回归集主体；
    - 未归集（中置信待复核 / 低置信独立）→ 回落到**本平台客户**：
      「不强行合并」不等于「不受管控」。

    归集结果**只用于抑制与频控**，不得用于跨平台营销投放（PRD P3 红线）。
    """
    identity = _load_identity(session, tenant_id, platform, shop_id, ext_key)
    if identity is not None and identity.customer_id is not None:
        return identity.customer_id
    customer = _load_customer(session, tenant_id, platform, shop_id, ext_key)
    return customer.customer_id if customer is not None else None


def list_pending_review(session: Session, tenant_id: uuid.UUID) -> list[CustomerIdentity]:
    """中置信待人工复核队列（E4）。

    队列项 ``customer_id IS NULL``——**复核前不归集**（架构 v5.2 §9.5）。
    复核动作本身不在本任务范围（Wave 3+）。
    """
    statement = (
        select(CustomerIdentity)
        .where(
            CustomerIdentity.tenant_id == tenant_id,
            CustomerIdentity.review_status == ReviewStatus.PENDING_REVIEW.value,
        )
        .order_by(CustomerIdentity.created_at, CustomerIdentity.ext_key)
    )
    return list(session.execute(statement).scalars().all())


# ── 落库层内部工具 ───────────────────────────────────────────────────────────


def _load_identity(
    session: Session,
    tenant_id: uuid.UUID,
    platform: str,
    shop_id: str,
    ext_key: str,
) -> CustomerIdentity | None:
    statement = select(CustomerIdentity).where(
        CustomerIdentity.tenant_id == tenant_id,
        CustomerIdentity.platform == platform,
        CustomerIdentity.shop_id == shop_id,
        CustomerIdentity.ext_key == ext_key,
    )
    return session.execute(statement).scalars().first()


def _load_customer(
    session: Session,
    tenant_id: uuid.UUID,
    platform: str,
    shop_id: str,
    ext_key: str,
) -> Customer | None:
    statement = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.platform == platform,
        Customer.shop_id == shop_id,
        Customer.ext_key == ext_key,
    )
    return session.execute(statement).scalars().first()


def _load_peer_records(session: Session, candidate: IdentityRecord) -> list[IdentityRecord]:
    """取同租户的全部平台身份（跨平台过滤在判定层完成）。"""
    statement = select(CustomerIdentity).where(CustomerIdentity.tenant_id == candidate.tenant_id)
    rows = session.execute(statement).scalars().all()
    return [
        IdentityRecord(
            tenant_id=row.tenant_id,
            platform=row.platform,
            shop_id=row.shop_id,
            ext_key=row.ext_key,
            phone_hash=row.phone_hash,
            address_fingerprint=row.address_fingerprint,
            customer_id=row.customer_id,
            identity_id=row.identity_id,
        )
        for row in rows
    ]


def _ensure_platform_customer(
    session: Session, candidate: IdentityRecord, now: datetime
) -> Customer:
    """确保该平台身份有一行平台客户（归集主体 / 未归集时的管控键载体）。"""
    customer = _load_customer(
        session, candidate.tenant_id, candidate.platform, candidate.shop_id, candidate.ext_key
    )
    if customer is not None:
        return customer
    customer = Customer(
        tenant_id=candidate.tenant_id,
        platform=candidate.platform,
        shop_id=candidate.shop_id,
        ext_key=candidate.ext_key,
        phone_hash=candidate.phone_hash,
        address_fingerprint=candidate.address_fingerprint,
        consent_status="unknown",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(customer)
    session.flush()
    return customer


def _load_or_create_identity(
    session: Session, candidate: IdentityRecord, now: datetime
) -> CustomerIdentity:
    """取已有身份行；没有则新建（重复登记幂等）。"""
    identity = _load_identity(
        session, candidate.tenant_id, candidate.platform, candidate.shop_id, candidate.ext_key
    )
    if identity is not None:
        return identity
    identity = CustomerIdentity(
        tenant_id=candidate.tenant_id,
        platform=candidate.platform,
        shop_id=candidate.shop_id,
        ext_key=candidate.ext_key,
        match_basis=MatchBasis.NO_FEATURE.value,
        confidence_tier=ConfidenceTier.LOW.value,
        confidence_score=_LOW_SCORE,
        phone_hash=candidate.phone_hash,
        address_fingerprint=candidate.address_fingerprint,
        review_status=ReviewStatus.NOT_REQUIRED.value,
        created_at=now,
        updated_at=now,
    )
    session.add(identity)
    session.flush()
    return identity


def _aggregate(
    session: Session,
    candidate: IdentityRecord,
    outcome: MatchOutcome,
    platform_customer: Customer | None,
    now: datetime,
) -> uuid.UUID:
    """执行高置信归集：两个平台身份指向同一统一客户。

    归集主体（canonical）的确定性选取（拍板④：复用已归集 ID，不新建 customer 行）：

    ① 对端身份已指向某个客户 → 用它（已归集链路保持稳定）；
    ② 否则用对端平台的客户行（**先到先得**，统一客户保留最初平台上下文）；
    ③ 都没有 → 用本次候选的平台客户行兜底（仅当 ``platform_customer`` 非 None 时）。
    """
    peer_identity = _load_identity(
        session,
        candidate.tenant_id,
        outcome.matched_platform or "",
        outcome.matched_shop_id or "",
        outcome.matched_ext_key or "",
    )
    canonical: uuid.UUID | None = None
    if peer_identity is not None and peer_identity.customer_id is not None:
        canonical = peer_identity.customer_id
    else:
        peer_customer = _load_customer(
            session,
            candidate.tenant_id,
            outcome.matched_platform or "",
            outcome.matched_shop_id or "",
            outcome.matched_ext_key or "",
        )
        if peer_customer is not None:
            canonical = peer_customer.customer_id
    if canonical is None:
        if platform_customer is None:
            raise RuntimeError(
                "高置信归集找不到归集主体：对端既无 customer_id 也无 customer 行，"
                "且候选平台未创建 customer 行（拍板④：高置信不新建）。"
                "请检查数据一致性。"
            )
        canonical = platform_customer.customer_id

    if peer_identity is not None:
        # 对端身份现在有了确凿的 ext_key 证据 → 同步升级为高置信并指向归集主体。
        peer_identity.customer_id = canonical
        peer_identity.match_basis = MatchBasis.EXT_KEY_EXACT.value
        peer_identity.confidence_tier = ConfidenceTier.HIGH.value
        peer_identity.confidence_score = _HIGH_SCORE
        peer_identity.review_status = ReviewStatus.NOT_REQUIRED.value
        peer_identity.updated_at = now

    if platform_customer is not None and platform_customer.customer_id != canonical:
        # 候选平台客户不是归集主体 → 标记 merged（归集痕迹，可审计）。
        platform_customer.status = _CUSTOMER_STATUS_MERGED
        platform_customer.updated_at = now

    return canonical
