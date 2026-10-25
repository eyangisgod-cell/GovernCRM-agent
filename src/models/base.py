"""ORM 模型基类与命名约定。

映射纪律（`.trellis/spec/backend/database-guidelines.md`）：

- **每表一个模型类**，类名 = 表名单数、首字母大写（`customer` → `Customer`）；
- 模型属性与 SQL 列**一一对应**，类型注解完整；
- 迁移仍走原始 SQL（`migrations/NNN_*.sql`），本目录只提供**类型化镜像**：
  SQLAlchemy 元数据是表的唯一「可执行契约」，仓储层、契约测试与
  「模型 ↔ 表结构一一对应」的验收用例都读它；
- 约束名与迁移文件保持同名同义（`chk_*` / `uq_*` / `idx_*`），
  便于两侧逐条比对，不允许出现两个真相来源。

命名约定（与迁移文件一致）：

- 主键 `{业务名}_id`（UUID）；外键列 `{被引用}_id`；
- 索引 `idx_{表}_{列}`；CHECK 约束 `chk_{表}_{语义}`；唯一约束 `uq_{表}_{语义}`。

⚠️ 列类型一律**显式声明**，不靠注解推断：`dict[str, Any]` 会被推断成 JSON、
`datetime` 会被推断成 TIMESTAMP（无时区），与迁移文件里的 JSONB / TIMESTAMPTZ 不一致。
"""

from __future__ import annotations

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

#: 全库统一的约束命名约定。显式命名的约束保持原样，未命名的按此模板生成。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "idx_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: TIMESTAMPTZ（带时区时间戳）——与迁移文件 `TIMESTAMPTZ` 列一一对应
TIMESTAMPTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
