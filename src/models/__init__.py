"""核心数据模型（ORM 镜像）。

**每表一个模型类**，类名 = 表名单数、首字母大写；属性与 SQL 列一一对应。
迁移仍走原始 SQL（`migrations/001_core_tables.sql`），本包只提供类型化镜像，
供仓储层、契约测试与「模型 ↔ 表结构一一对应」验收用例使用。

表 → 模型对照（17 张，含框架辅助表）：

=========================  ==========================
表名                       模型类
=========================  ==========================
``customer``               :class:`Customer`
``customer_identity``      :class:`CustomerIdentity`
``customer_segment``       :class:`CustomerSegment`
``action_candidate``       :class:`ActionCandidate`
``decision``               :class:`Decision`
``audit_log``              :class:`AuditLog`
``suppression_record``     :class:`SuppressionRecord`
``approval``               :class:`Approval`
``tool_call``              :class:`ToolCall`
``worker``                 :class:`Worker`
``worker_template``        :class:`WorkerTemplate`
``worker_metrics``         :class:`WorkerMetrics`
``skill_card``             :class:`SkillCard`
``cost_record``            :class:`CostRecord`
``memory_episode``         :class:`MemoryEpisode`
``memory_quarantine``      :class:`MemoryQuarantine`
``schema_migrations``      :class:`SchemaMigration`
=========================  ==========================
"""

from __future__ import annotations

from .action_candidate import ActionCandidate as ActionCandidate
from .approval import Approval as Approval
from .audit_log import AuditLog as AuditLog
from .base import NAMING_CONVENTION as NAMING_CONVENTION, TIMESTAMPTZ as TIMESTAMPTZ, Base as Base
from .cost_record import CostRecord as CostRecord
from .customer import Customer as Customer
from .customer_identity import CustomerIdentity as CustomerIdentity
from .customer_segment import CustomerSegment as CustomerSegment
from .decision import Decision as Decision
from .memory_episode import MemoryEpisode as MemoryEpisode
from .memory_quarantine import MemoryQuarantine as MemoryQuarantine
from .rule_ids import REGISTERED_RULE_IDS as REGISTERED_RULE_IDS
from .schema_migration import SchemaMigration as SchemaMigration
from .skill_card import SkillCard as SkillCard
from .suppression_record import SuppressionRecord as SuppressionRecord
from .tool_call import ToolCall as ToolCall
from .worker import Worker as Worker
from .worker_metrics import WorkerMetrics as WorkerMetrics
from .worker_template import WorkerTemplate as WorkerTemplate

__all__ = [
    "NAMING_CONVENTION",
    "REGISTERED_RULE_IDS",
    "TIMESTAMPTZ",
    "ActionCandidate",
    "Approval",
    "AuditLog",
    "Base",
    "CostRecord",
    "Customer",
    "CustomerIdentity",
    "CustomerSegment",
    "Decision",
    "MemoryEpisode",
    "MemoryQuarantine",
    "SchemaMigration",
    "SkillCard",
    "SuppressionRecord",
    "ToolCall",
    "Worker",
    "WorkerMetrics",
    "WorkerTemplate",
]
