"""审计哈希链链尾读取 + 同租户写入串行化（红线③：口径只有一份）。

**为什么单独成模块**：写审计的**全部**入口——通用事件写入 ``src/audit/events.py``、
恢复重判写入 ``src/approval/audit.py``、越权审计 ``src/auth/rbac.py``、
供应商适配器写入器 ``src/adapters/mock_adapter.py``——都要取「同租户上一条记录的
``record_hash``」作为 ``prev_hash``。多处各抄一份 SQL，口径一旦漂移，
链校验必然失败且难以定位。本模块是**唯一实现点**，各处共用。

**原子性纪律（红线③：哈希链不可断）**：取链尾与插入必须落在**同一事务**内，
且同租户写入必须串行。否则两个写入方读到同一链尾 → 两条记录挂同一 ``prev_hash``
→ 哈希链断裂、``verify_chain`` 校验失败——治理链当场失效。本模块提供三件套：

1. ``chain_tip`` —— 取链尾（**唯一一份 SQL 口径**）；
2. ``chain_write_transaction`` —— 开事务 + 同租户 advisory 写锁（**锁在取链尾之前**）；
3. ``retry_on_chain_conflict`` —— 唯一约束冲突时「重读链尾 + 重试」一次（**不吞异常**）。

**分层**：本模块只负责「链结构」这一个动作，**模块级零 DB 依赖**（只依赖最小连接
协议；``psycopg`` 仅在重试 helper 内部惰性 import，运行期不需要 DB 驱动即可 import）。
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol, TypeVar

from src.audit.hash_chain import GENESIS_HASH

__all__ = [
    "CHAIN_LOCK_SQL",
    "CHAIN_TIP_SQL",
    "ChainTipConnection",
    "ChainWriteConnection",
    "chain_tip",
    "chain_write_transaction",
    "lock_tenant_chain",
    "retry_on_chain_conflict",
]

#: 取链尾的 SQL（**唯一一份**——写入方与链校验共用同一口径）。
CHAIN_TIP_SQL: str = (
    "select record_hash from audit_log where tenant_id = %s order by record_seq desc limit 1"
)

#: 同租户链写入的事务级互斥锁（**唯一一份**）。
#:
#: ``pg_advisory_xact_lock`` 随事务结束自动释放：同租户写入在事务内排队，
#: 不同租户互不干扰（锁键 = ``tenant_id`` 的 64 位哈希）。
#: **必须与插入同事务**——autocommit 下事务级锁会立刻释放，等于没锁。
CHAIN_LOCK_SQL: str = "select pg_advisory_xact_lock(hashtextextended(%s::text, 0))"

_T = TypeVar("_T")


class ChainTipConnection(Protocol):
    """最小连接协议：取链尾只需要 ``cursor()`` → ``execute`` / ``fetchone``。

    协议化而非绑定 ``psycopg.Connection``：既让真库连接直接可用，也让不依赖
    DB 驱动的适配层（假连接 / 结构化类型）复用同一份口径。
    """

    def cursor(self) -> Any: ...


class ChainWriteConnection(ChainTipConnection, Protocol):
    """最小连接协议（**写入**路径）：除取链尾外，还要能开事务。

    ``transaction()`` 返回 psycopg 的事务上下文管理器（``with conn.transaction():``）。
    """

    def transaction(self) -> Any: ...


def _normalize_tenant(tenant_id: uuid.UUID | str) -> uuid.UUID:
    """``tenant_id`` 统一归一为 ``uuid.UUID``（适配层按字符串持有租户）。"""
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


def chain_tip(conn: ChainTipConnection, tenant_id: uuid.UUID | str) -> str:
    """取该租户哈希链当前链尾的 ``record_hash``（无记录则为创世值）。

    ``prev_hash`` = 该租户链尾的 ``record_hash``；首条记录用创世值
    （64 个 '0'，见 ``src/audit/hash_chain.py`` 的 ``GENESIS_HASH``）。

    ``tenant_id`` 允许传 ``str``（适配层按字符串持有租户）——统一归一为
    ``uuid.UUID`` 再入参，SQL 口径与驱动适配保持唯一。

    **调用前提**：必须在 ``chain_write_transaction`` 的事务内调用（取链尾与插入
    同事务 + 同租户串行），否则并发写入会读到同一链尾 → 断链。
    """
    tenant = _normalize_tenant(tenant_id)
    with conn.cursor() as cur:
        cur.execute(CHAIN_TIP_SQL, (tenant,))
        row = cur.fetchone()
    return str(row[0]) if row else GENESIS_HASH


def lock_tenant_chain(conn: ChainTipConnection, tenant_id: uuid.UUID | str) -> None:
    """在**当前事务内**对同租户链加写锁（同租户串行、不同租户并行）。

    事务级 advisory 锁：提交/回滚即释放，不残留会话锁；不同 ``tenant_id``
    哈希不同 → 多租户互不阻塞。
    """
    tenant = _normalize_tenant(tenant_id)
    with conn.cursor() as cur:
        cur.execute(CHAIN_LOCK_SQL, (str(tenant),))


@contextmanager
def chain_write_transaction(
    conn: ChainWriteConnection, tenant_id: uuid.UUID | str
) -> Iterator[None]:
    """链写入事务：**开事务 → 同租户写锁**，交给调用方在事务内取链尾并插入。

    纪律（红线③）：

    - 取链尾与插入必须在**同一事务**内——分两步走会给并发写入方留竞态窗口；
    - 写锁必须在取链尾**之前**拿——否则锁住的是已经读到的旧链尾。

    事务边界由 psycopg 的 ``conn.transaction()`` 负责：块内异常 → 回滚，
    块内正常 → 提交（调用方自带的事务则退化为 savepoint，由调用方决定提交时机）。
    """
    with conn.transaction():
        lock_tenant_chain(conn, tenant_id)
        yield


def retry_on_chain_conflict(operation: Callable[[], _T]) -> _T:
    """链尾被并发写入方抢占时**重试一次**（重读链尾 + 重写）。

    触发场景：DB 级唯一索引（``migrations/004_audit_log_chain_unique.sql``）
    把「同一 ``prev_hash`` 被写两次」变成写失败——这是**显式阻断**而非静默断链。
    典型来源是**未走 advisory 锁**的写入方（如 ``src/auth/rbac.py``）插到了我们前面。

    **不吞异常**：第二次仍失败 → 异常直接上抛（审计写入失败必须阻断业务，红线③）。
    """
    # 惰性 import：模块级零 DB 依赖（运行期不需要 DB 驱动即可 import 本模块）。
    import psycopg

    # 第一次：正常写入。唯一约束冲突（链尾被抢占）→ 落到下面重试。
    with contextlib.suppress(psycopg.errors.UniqueViolation):
        return operation()

    # 第二次：新事务内重读链尾后重写。仍失败 → 异常上抛，不静默吞掉。
    return operation()
