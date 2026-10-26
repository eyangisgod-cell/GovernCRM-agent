"""T-C1 身份匹配引擎（三档）测试。

依据（可反查）：

- **架构 v5.2 §9.5**「身份匹配三档算法」——高 / 中 / 低三档的匹配条件与处理方式
  （裁决 #17 / ADR-037）：高置信跨平台 ``ext_key`` 精确匹配 → 自动归集；
  中置信手机号哈希 + 收件地址指纹**部分匹配** → 进人工复核队列（E4）、**复核前不归集**；
  低置信无匹配特征 → **保留独立身份**。
- **PRD P1 红线**「不得强行合并」——低置信绝不合并。
- **数据库 CHECK** ``chk_customer_identity_low_not_merged``——低置信身份不得指向已归集客户。
- **红线 ①⑤**——零 LLM、零供应商依赖（静态扫描本模块 import）。

两层分开验证：

- **纯判定层**（默认集，无 PG）：``classify_identity`` 的三档语义与确定性；
- **落库层**（``-m pg``，**真 PostgreSQL**）：``customer_identity`` 的落库口径、
  归集后抑制/频控用的统一客户键、以及「低置信未被合并」的**显式断言**
  （不得用 SQLite 代替——``.trellis/spec/backend/database-guidelines.md``）。
"""

from __future__ import annotations

import ast
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.models.customer import Customer
from src.models.customer_identity import CustomerIdentity
from src.unification.identity import (
    ConfidenceTier,
    IdentityRecord,
    MatchBasis,
    ReviewStatus,
    classify_identity,
    list_pending_review,
    register_identity,
    resolve_unified_customer,
)

# ── 固定夹具（确定性：无随机、无时钟依赖）──────────────────────────────────

TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000009")
PEER_CUSTOMER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
PEER_IDENTITY_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")

#: 数据库 CHECK 约束允许的取值（``migrations/001_core_tables.sql``）——
#: 引擎枚举与之必须逐字一致，多一个值就是自造口径。
DB_MATCH_BASIS = {"ext_key_exact", "phone_hash_address_fingerprint", "no_feature"}
DB_CONFIDENCE_TIERS = {"high", "medium", "low"}
DB_REVIEW_STATUSES = {"not_required", "pending_review", "confirmed", "rejected"}

MODULE_PATH = Path(__file__).resolve().parents[3] / "src" / "unification" / "identity.py"


def _record(
    platform: str,
    ext_key: str,
    *,
    shop_id: str = "s1",
    tenant_id: uuid.UUID = TENANT_ID,
    phone_hash: str | None = None,
    address_fingerprint: str | None = None,
    customer_id: uuid.UUID | None = None,
    identity_id: uuid.UUID | None = None,
) -> IdentityRecord:
    """构造一条平台身份记录（默认无任何跨平台匹配特征）。"""
    return IdentityRecord(
        tenant_id=tenant_id,
        platform=platform,
        shop_id=shop_id,
        ext_key=ext_key,
        phone_hash=phone_hash,
        address_fingerprint=address_fingerprint,
        customer_id=customer_id,
        identity_id=identity_id,
    )


# ── 一、纯判定层：三档语义 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_ext_key_exact_match_merges_automatically() -> None:
    """跨平台 ``ext_key`` 精确相等 → 高置信并自动归集为同一 customer。

    依据：架构 v5.2 §9.5 高置信行——「跨平台 ``ext_key`` 精确匹配 → 自动归集」。
    """
    candidate = _record("douyin", "U-1001", phone_hash="PH-1", address_fingerprint="AF-1")
    peer = _record(
        "taobao",
        "U-1001",
        phone_hash="PH-9",
        address_fingerprint="AF-9",
        customer_id=PEER_CUSTOMER_ID,
        identity_id=PEER_IDENTITY_ID,
    )

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.HIGH
    assert outcome.match_basis is MatchBasis.EXT_KEY_EXACT
    assert outcome.customer_id == PEER_CUSTOMER_ID
    assert outcome.review_status is ReviewStatus.NOT_REQUIRED


@pytest.mark.unit
def test_phone_hash_and_address_fingerprint_partial_match_goes_to_review_queue() -> None:
    """手机号哈希命中、地址指纹不命中 → 中置信，进人工复核队列（E4）。

    依据：架构 v5.2 §9.5 中置信行——「手机号哈希 + 收件地址指纹**部分匹配**
    → 进人工复核队列（E4）；**复核前不归集**」。
    """
    candidate = _record("douyin", "U-2002", phone_hash="PH-1", address_fingerprint="AF-2")
    peer = _record(
        "taobao",
        "U-2002-OTHER",
        phone_hash="PH-1",
        address_fingerprint="AF-9",
        customer_id=PEER_CUSTOMER_ID,
        identity_id=PEER_IDENTITY_ID,
    )

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.MEDIUM
    assert outcome.match_basis is MatchBasis.PHONE_HASH_ADDRESS_FINGERPRINT
    assert outcome.review_status is ReviewStatus.PENDING_REVIEW
    assert outcome.matched_features == ("phone_hash",)


@pytest.mark.unit
def test_no_feature_match_keeps_identity_independent() -> None:
    """无任何跨平台匹配特征 → 低置信，保留独立身份。

    依据：架构 v5.2 §9.5 低置信行——「无匹配特征 → 保留独立身份（P1 红线）」。
    """
    candidate = _record("douyin", "U-3003", phone_hash="PH-1", address_fingerprint="AF-1")
    peer = _record(
        "taobao",
        "U-3003-OTHER",
        phone_hash="PH-9",
        address_fingerprint="AF-9",
        customer_id=PEER_CUSTOMER_ID,
        identity_id=PEER_IDENTITY_ID,
    )

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.LOW
    assert outcome.match_basis is MatchBasis.NO_FEATURE
    assert outcome.customer_id is None
    assert outcome.review_status is ReviewStatus.NOT_REQUIRED


@pytest.mark.unit
def test_low_confidence_is_never_merged() -> None:
    """低置信**永不**拿到 customer_id——逐个场景显式断言。

    依据：PRD P1 红线「不得强行合并」+ 数据库 CHECK
    ``chk_customer_identity_low_not_merged``。
    """
    # peer 带着一个已归集客户；低置信候选仍不得继承它。
    peer = _record(
        "taobao",
        "U-4004",
        phone_hash="PH-1",
        address_fingerprint="AF-1",
        customer_id=PEER_CUSTOMER_ID,
        identity_id=PEER_IDENTITY_ID,
    )
    low_candidates = (
        _record("douyin", "U-4004-OTHER"),  # 完全没有特征
        _record("douyin", "U-4004-OTHER", phone_hash="PH-X"),  # 手机号不命中
        _record("douyin", "U-4004-OTHER", address_fingerprint="AF-X"),  # 地址不命中
    )

    for candidate in low_candidates:
        outcome = classify_identity(candidate, [peer])
        assert outcome.confidence_tier is ConfidenceTier.LOW, candidate
        assert outcome.customer_id is None, f"低置信候选被归集了：{candidate}"
        assert outcome.is_merged is False, candidate


@pytest.mark.unit
def test_medium_confidence_not_merged_before_review() -> None:
    """中置信在人工复核前**不归集**：peer 已归集，候选仍 ``customer_id IS NULL``。

    依据：架构 v5.2 §9.5 中置信行——「复核前不归集」。
    """
    candidate = _record("douyin", "U-5005", phone_hash="PH-1", address_fingerprint="AF-2")
    peer = _record(
        "taobao",
        "U-5005-OTHER",
        phone_hash="PH-1",
        address_fingerprint="AF-9",
        customer_id=PEER_CUSTOMER_ID,
        identity_id=PEER_IDENTITY_ID,
    )

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.MEDIUM
    assert outcome.customer_id is None, "中置信在复核前不得归集"
    assert outcome.review_status is ReviewStatus.PENDING_REVIEW


@pytest.mark.unit
def test_match_basis_and_confidence_tier_are_recorded() -> None:
    """匹配置信度可回放：枚举取值与数据库 CHECK 逐字一致。

    依据：架构 v5.2 §9.5 实现约束——「匹配置信度落库 ``customer_identity``
    表存匹配依据与置信档位，可回放」。
    """
    assert {basis.value for basis in MatchBasis} == DB_MATCH_BASIS
    assert {tier.value for tier in ConfidenceTier} == DB_CONFIDENCE_TIERS
    assert {status.value for status in ReviewStatus} == DB_REVIEW_STATUSES

    candidate = _record("douyin", "U-6006", phone_hash="PH-1", address_fingerprint="AF-1")
    peer = _record("taobao", "U-6006", customer_id=PEER_CUSTOMER_ID)
    outcome = classify_identity(candidate, [peer])

    assert outcome.match_basis is MatchBasis.EXT_KEY_EXACT
    assert outcome.confidence_tier is ConfidenceTier.HIGH
    assert outcome.confidence_score == Decimal("1.0000")


@pytest.mark.unit
def test_confidence_score_ranks_tiers_and_stays_in_range() -> None:
    """置信分单调：高 > 中 > 低，且落在 ``[0, 1]``（库列 ``numeric(6,4)``）。

    依据：架构 v5.2 §9.5——置信度分档可回放；打分必须确定性、可排序。
    """
    candidate = _record("douyin", "U-7007", phone_hash="PH-1", address_fingerprint="AF-1")

    high = classify_identity(candidate, [_record("taobao", "U-7007")])
    medium = classify_identity(
        _record("douyin", "U-7007-OTHER", phone_hash="PH-1", address_fingerprint="AF-9"),
        [_record("taobao", "U-7007", phone_hash="PH-1", address_fingerprint="AF-8")],
    )
    low = classify_identity(candidate, [_record("taobao", "U-7007-OTHER")])

    assert high.confidence_score > medium.confidence_score > low.confidence_score
    for outcome in (high, medium, low):
        assert Decimal("0") <= outcome.confidence_score <= Decimal("1")


@pytest.mark.unit
def test_matching_is_deterministic() -> None:
    """同输入同输出：peer 顺序打乱 / 重复调用，判定结果完全一致。

    依据：架构 v5.2 §9.5「确定性规则三档匹配」+ PRD「决策可复现」（架构 A2）。
    """
    candidate = _record("douyin", "U-8008", phone_hash="PH-1", address_fingerprint="AF-1")
    peers = [
        _record("taobao", "U-8008-OTHER", phone_hash="PH-1", customer_id=PEER_CUSTOMER_ID),
        _record("wechat", "U-8008", customer_id=PEER_CUSTOMER_ID),
        _record("jd", "U-8008-OTHER2", address_fingerprint="AF-1"),
    ]

    first = classify_identity(candidate, peers)
    second = classify_identity(candidate, list(reversed(peers)))
    third = classify_identity(candidate, peers)

    assert first == second
    assert first == third


@pytest.mark.unit
def test_same_platform_identity_is_not_treated_as_cross_platform_match() -> None:
    """同平台身份不算跨平台匹配（归集只发生在跨平台之间）。

    依据：架构 v5.2 §9.5——三档均以「跨平台」为前提。
    """
    candidate = _record("taobao", "U-9009", phone_hash="PH-1", address_fingerprint="AF-1")
    peer = _record("taobao", "U-9009", shop_id="other-shop", customer_id=PEER_CUSTOMER_ID)

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.LOW
    assert outcome.customer_id is None


@pytest.mark.unit
def test_identities_of_other_tenants_are_never_matched() -> None:
    """跨租户不得匹配（租户隔离在数据接入层强制，不依赖调用方自觉）。

    依据：``.trellis/spec/backend/database-guidelines.md``——租户隔离强制过滤。
    """
    candidate = _record("douyin", "U-1010", phone_hash="PH-1", address_fingerprint="AF-1")
    peer = _record(
        "taobao",
        "U-1010",
        tenant_id=OTHER_TENANT_ID,
        phone_hash="PH-1",
        customer_id=PEER_CUSTOMER_ID,
    )

    outcome = classify_identity(candidate, [peer])

    assert outcome.confidence_tier is ConfidenceTier.LOW
    assert outcome.customer_id is None


# ── 二、零 LLM 守卫 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_identity_module_imports_no_model_sdk() -> None:
    """静态扫描本模块 import：不得含任何模型 SDK / ``litellm``（红线 ①⑤）。

    依据：红线 ①（policy_kernel 纯函数零 LLM）、红线 ⑤（业务层零供应商依赖）、
    架构 v5.2 §9.5「确定性规则三档匹配，纯代码、可解释、零 LLM」。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "litellm",
        "openai",
        "anthropic",
        "google.genai",
        "google.generativeai",
        "cohere",
        "ollama",
        "dashscope",
        "zhipuai",
        "mistralai",
        "groq",
        "vertexai",
        "transformers",
        "langchain",
        "langgraph",
    }
    hits = sorted(name for name in imported if name.split(".")[0] in forbidden)
    assert not hits, f"身份匹配模块不得依赖模型 SDK / 供应商 SDK：{hits}"


# ── 三、落库层（真 PostgreSQL）─────────────────────────────────────────────


@pytest.fixture
def session() -> Iterator[Session]:
    """真 PostgreSQL 会话；用例结束回滚，不留脏数据。

    不用 SQLite 代替：``.trellis/spec/backend/database-guidelines.md`` 明确
    CHECK 约束必须用真 PostgreSQL 验证。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 用例无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    engine = create_engine(url)
    with Session(engine) as db_session:
        try:
            yield db_session
        finally:
            db_session.rollback()
            engine.dispose()


def _identity_row(
    session: Session, tenant_id: uuid.UUID, platform: str, ext_key: str
) -> CustomerIdentity | None:
    statement = select(CustomerIdentity).where(
        CustomerIdentity.tenant_id == tenant_id,
        CustomerIdentity.platform == platform,
        CustomerIdentity.ext_key == ext_key,
    )
    return session.execute(statement).scalars().first()


@pytest.mark.pg
def test_high_confidence_identity_is_persisted_and_shares_one_customer(session: Session) -> None:
    """高置信落库：两个平台身份归集到**同一个** customer，置信依据可回放。

    依据：架构 v5.2 §9.5 高置信行 + 实现约束「匹配置信度落库，可回放」。
    """
    tenant_id = uuid.uuid4()
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    first = register_identity(
        session,
        _record("taobao", "U-2001", tenant_id=tenant_id, phone_hash="PH-1"),
        now=now,
    )
    second = register_identity(
        session,
        _record("douyin", "U-2001", tenant_id=tenant_id, phone_hash="PH-1"),
        now=now,
    )

    # 首次登记时还没有跨平台证据 → 低置信、未归集
    assert first.confidence_tier is ConfidenceTier.LOW
    assert first.unified_customer_id is None

    assert second.confidence_tier is ConfidenceTier.HIGH
    assert second.match_basis is MatchBasis.EXT_KEY_EXACT
    assert second.unified_customer_id is not None

    row = _identity_row(session, tenant_id, "douyin", "U-2001")
    assert row is not None
    assert row.match_basis == "ext_key_exact"
    assert row.confidence_tier == "high"
    assert row.customer_id == second.unified_customer_id

    # 归集的两个平台身份共享同一个统一客户（抑制 / 频控据此生效）
    taobao_customer = resolve_unified_customer(session, tenant_id, "taobao", "s1", "U-2001")
    douyin_customer = resolve_unified_customer(session, tenant_id, "douyin", "s1", "U-2001")
    assert taobao_customer is not None
    assert taobao_customer == douyin_customer == second.unified_customer_id

    # 归集后，对端身份行的置信度同步升级为高置信并指向同一归集主体
    taobao_row = _identity_row(session, tenant_id, "taobao", "U-2001")
    assert taobao_row is not None
    assert taobao_row.confidence_tier == "high"
    assert taobao_row.match_basis == "ext_key_exact"
    assert taobao_row.customer_id == second.unified_customer_id


@pytest.mark.pg
def test_high_confidence_merge_reuses_existing_customer_without_creating_new_row(
    session: Session,
) -> None:
    """高置信归集复用已归集 ID，不新建 customer 行（拍板④）。

    依据：G-02「同一客户多条动作只计 1 次」——若每条身份都建一行 customer，
    下游客户数统计会重复计数。
    """
    tenant_id = uuid.uuid4()
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    first = register_identity(
        session,
        _record("taobao", "U-2401", tenant_id=tenant_id, phone_hash="PH-5"),
        now=now,
    )
    assert first.confidence_tier is ConfidenceTier.LOW

    customer_count_before = (
        session.execute(select(Customer).where(Customer.tenant_id == tenant_id)).scalars().all()
    )
    assert len(customer_count_before) == 1, "首次登记应建 1 行 customer"

    second = register_identity(
        session,
        _record("douyin", "U-2401", tenant_id=tenant_id, phone_hash="PH-5"),
        now=now,
    )
    assert second.confidence_tier is ConfidenceTier.HIGH
    assert second.unified_customer_id is not None

    customer_count_after = (
        session.execute(select(Customer).where(Customer.tenant_id == tenant_id)).scalars().all()
    )
    assert len(customer_count_after) == 1, (
        f"高置信归集不应新建 customer 行，但实际有 {len(customer_count_after)} 行"
    )
    assert customer_count_after[0].customer_id == second.unified_customer_id


@pytest.mark.pg
def test_medium_confidence_identity_is_persisted_as_pending_review_and_not_merged(
    session: Session,
) -> None:
    """中置信落库：``review_status=pending_review`` 且 ``customer_id IS NULL``。

    依据：架构 v5.2 §9.5 中置信行 + 数据库列注释
    「NULL = 未归集（低置信独立身份 / 待人工复核）」。
    """
    tenant_id = uuid.uuid4()
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    register_identity(
        session,
        _record(
            "taobao", "U-2101", tenant_id=tenant_id, phone_hash="PH-2", address_fingerprint="AF-2"
        ),
        now=now,
    )
    result = register_identity(
        session,
        _record(
            "douyin",
            "U-2101-OTHER",
            tenant_id=tenant_id,
            phone_hash="PH-2",
            address_fingerprint="AF-9",
        ),
        now=now,
    )

    assert result.confidence_tier is ConfidenceTier.MEDIUM
    assert result.match_basis is MatchBasis.PHONE_HASH_ADDRESS_FINGERPRINT
    assert result.review_status is ReviewStatus.PENDING_REVIEW
    assert result.unified_customer_id is None

    row = _identity_row(session, tenant_id, "douyin", "U-2101-OTHER")
    assert row is not None
    assert row.review_status == "pending_review"
    assert row.customer_id is None, "中置信在人工复核前不得归集"

    queued = list_pending_review(session, tenant_id)
    assert [item.ext_key for item in queued] == ["U-2101-OTHER"]


@pytest.mark.pg
def test_low_confidence_identity_is_never_merged_in_database(session: Session) -> None:
    """低置信落库：**显式**断言未被合并——``customer_id IS NULL`` 且不与他人共享客户。

    依据：PRD P1 红线「不得强行合并」+ CHECK ``chk_customer_identity_low_not_merged``。
    """
    tenant_id = uuid.uuid4()
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    register_identity(
        session,
        _record(
            "taobao", "U-2201", tenant_id=tenant_id, phone_hash="PH-3", address_fingerprint="AF-3"
        ),
        now=now,
    )
    result = register_identity(
        session,
        _record(
            "douyin",
            "U-2201-OTHER",
            tenant_id=tenant_id,
            phone_hash="PH-99",
            address_fingerprint="AF-99",
        ),
        now=now,
    )

    assert result.confidence_tier is ConfidenceTier.LOW
    assert result.match_basis is MatchBasis.NO_FEATURE
    assert result.unified_customer_id is None

    row = _identity_row(session, tenant_id, "douyin", "U-2201-OTHER")
    assert row is not None
    assert row.confidence_tier == "low"
    assert row.customer_id is None, "低置信身份被合并了（P1 红线）"

    # 显式断言「未被合并」：该身份没有任何指向已归集客户的链接，
    # 且它的平台客户没有被租户内任何其它身份共享。
    platform_customer = (
        session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.platform == "douyin",
                Customer.ext_key == "U-2201-OTHER",
            )
        )
        .scalars()
        .first()
    )
    assert platform_customer is not None
    shared = (
        session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.tenant_id == tenant_id,
                CustomerIdentity.customer_id == platform_customer.customer_id,
                CustomerIdentity.ext_key != "U-2201-OTHER",
            )
        )
        .scalars()
        .first()
    )
    assert shared is None, "低置信身份与其它身份共享了客户 —— 等于被合并"


@pytest.mark.pg
def test_re_registering_the_same_identity_is_idempotent(session: Session) -> None:
    """同一平台身份重复登记不产生第二行，且归集结论稳定。

    依据：``.trellis/spec/backend/database-guidelines.md``——唯一约束
    ``uq_customer_identity_platform_shop_extkey``。
    """
    tenant_id = uuid.uuid4()
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    first = register_identity(
        session,
        _record("taobao", "U-2301", tenant_id=tenant_id, phone_hash="PH-4"),
        now=now,
    )
    second = register_identity(
        session,
        _record("taobao", "U-2301", tenant_id=tenant_id, phone_hash="PH-4"),
        now=now,
    )

    assert first.identity_id == second.identity_id
    rows = (
        session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.tenant_id == tenant_id,
                CustomerIdentity.platform == "taobao",
                CustomerIdentity.ext_key == "U-2301",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
