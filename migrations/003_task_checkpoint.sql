-- ============================================================================
-- migrations/003_task_checkpoint.sql
-- GovernCRM MVP 位点表（T-E7 · G-04 / FR-14）
--
-- 权威依据：
--   - docs/specs/GovernCRM_待拍板决策总表.md G-04：MVP 加一张位点表，不要过度设计
--   - docs/specs/MVP_任务拆解.md T-E7：存审计级位点索引，**不存 graph_state_snapshot**
--     （恢复用 LangGraph checkpointer，审计用本表）
--   - docs/specs/GovernCRM_产品需求文档_PRD_v5.2.md FR-14：增量同步与位点管理，位点可回放
--
-- 纪律（沿用 001 头注释）：
--   1) 幂等：重复执行不报错（CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS）。
--   2) 单一写者：Wave 3 唯一迁移 = T-E7 的 task_checkpoint（**只此一个 003**）。
--   3) 租户隔离：业务表带 tenant_id，索引以 tenant_id 打头。
--   4) 本表**非** audit_log，不挂 append-only 触发器；只增不改是约定
--      （接口只提供写入+查询，不提供 UPDATE/DELETE API）。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 位点表：图执行到某节点时的审计级位点索引
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_checkpoint (
    checkpoint_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL,
    task_id                  UUID NOT NULL,
    node_name                TEXT NOT NULL,
    checkpoint_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_id              UUID NULL,
    langgraph_checkpoint_id  TEXT NULL,
    CONSTRAINT chk_task_checkpoint_node_name
        CHECK (node_name ~ '^[a-z][a-z0-9_]*$'),
    CONSTRAINT chk_task_checkpoint_at_not_future
        CHECK (checkpoint_at <= now() + interval '1 hour')
);

CREATE INDEX IF NOT EXISTS idx_task_checkpoint_tenant_task_at
    ON task_checkpoint (tenant_id, task_id, checkpoint_at);

COMMENT ON TABLE task_checkpoint IS
    '位点索引（G-04/FR-14）：审计级位点可回放；不存 graph_state_snapshot——恢复走 LangGraph checkpointer，本表只增不改（无 UPDATE/DELETE API）';
COMMENT ON COLUMN task_checkpoint.node_name IS
    '图节点名（小写字母开头，字母/数字/下划线）';
COMMENT ON COLUMN task_checkpoint.checkpoint_at IS
    '位点时间；CHECK 约束拒绝离谱未来时间（时钟漂移保护）';
COMMENT ON COLUMN task_checkpoint.decision_id IS
    '该位点关联的决策 id（可空；非 audit_log，无哈希链）';
COMMENT ON COLUMN task_checkpoint.langgraph_checkpoint_id IS
    'LangGraph checkpointer 的位点 id（恢复用本列定位，状态本体不落本表）';

INSERT INTO schema_migration (version, description)
VALUES ('003_task_checkpoint', '位点表 task_checkpoint（G-04/FR-14，Wave 3 唯一迁移）')
ON CONFLICT (version) DO UPDATE SET applied_at = now();

COMMIT;
