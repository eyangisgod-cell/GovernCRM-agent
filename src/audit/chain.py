"""审计哈希链链尾读取（红线③：口径只有一份）。

**为什么单独成模块**：写审计的**全部**入口——通用事件写入 ``src/audit/events.py``、
恢复重判写入 ``src/approval/audit.py``、越权审计 ``src/auth/rbac.py``、
供应商适配器写入器 ``src/adapters/mock_adapter.py``——都要取「同租户上一条记录的
``record_hash``」作为 ``prev_hash``。多处各抄一份 SQL，口径一旦漂移，
链校验必然失败且难以定位。本模块是**唯一实现点**，各处共用。

**分层**：本模块只负责「取链尾」这一个动作（一条 SQL + 一次查询），
写审计的其余部分仍留在各自模块；模块级**零 DB 依赖**（只依赖一个最小连接协议，
运行期不需要 DB 驱动即可 import）。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from src.audit.hash_chain import GENESIS_HASH

__all__ = ["CHAIN_TIP_SQL", "ChainTipConnection", "chain_tip"]

#: 取链尾的 SQL（**唯一一份**——写入方与链校验共用同一口径）。
CHAIN_TIP_SQL: str = (
    "select record_hash from audit_log where tenant_id = %s order by record_seq desc limit 1"
)


class ChainTipConnection(Protocol):
    """最小连接协议：取链尾只需要 ``cursor()`` → ``execute`` / ``fetchone``。

    协议化而非绑定 ``psycopg.Connection``：既让真库连接直接可用，也让不依赖
    DB 驱动的适配层（假连接 / 结构化类型）复用同一份口径。
    """

    def cursor(self) -> Any: ...


def chain_tip(conn: ChainTipConnection, tenant_id: uuid.UUID | str) -> str:
    """取该租户哈希链当前链尾的 ``record_hash``（无记录则为创世值）。

    ``prev_hash`` = 该租户链尾的 ``record_hash``；首条记录用创世值
    （64 个 '0'，见 ``src/audit/hash_chain.py`` 的 ``GENESIS_HASH``）。

    ``tenant_id`` 允许传 ``str``（适配层按字符串持有租户）——统一归一为
    ``uuid.UUID`` 再入参，SQL 口径与驱动适配保持唯一。
    """
    tenant = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    with conn.cursor() as cur:
        cur.execute(CHAIN_TIP_SQL, (tenant,))
        row = cur.fetchone()
    return str(row[0]) if row else GENESIS_HASH
