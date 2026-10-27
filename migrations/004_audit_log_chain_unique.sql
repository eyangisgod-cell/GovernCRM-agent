-- ============================================================================
-- migrations/004_audit_log_chain_unique.sql
-- GovernCRM MVP 审计哈希链 DB 级不变量（红线③：哈希链不可断）
--
-- 权威依据：
--   - 五条红线③（审计 append-only + 哈希链；审计写入失败即阻断业务）
--   - docs/specs/GovernCRM_技术架构方案_v5.2.md §4.3 / §4.4（治理闸门唯一决策出口、
--     路径完整性断言、审计阻断）
--   - docs/specs/MVP_任务拆解.md §5 T-E3（审计 append-only + 触发器 + 哈希链）
--   - docs/specs/GovernCRM_附录D_冻结版.md（哈希链 prev_hash/record_hash 口径）
--
-- 缺陷背景（人工 QA 复核发现）：
--   审计写入原为「取链尾（SELECT）→ 插入（INSERT）」两步，且写入方用 autocommit，
--   两个 SELECT 立即提交、INSERT 另起事务 —— 同租户两个写入方并发时读到**同一链尾**，
--   两条记录挂同一个 prev_hash → 哈希链断裂、verify_chain 校验失败（红线③被破）。
--   修复是两层：
--     ① 应用层：取链尾与插入同事务 + pg_advisory_xact_lock（同租户串行）
--        —— src/audit/chain.py（唯一实现点）；
--     ② DB 层（本迁移）：唯一索引把「同一 prev_hash 被写两次」变成**写失败**，
--        静默断链 → 显式阻断（配合写入侧的一次重试）。
--
-- 纪律（沿用 001 / 003 头注释）：
--   1) 幂等：重复执行不报错（CREATE UNIQUE INDEX IF NOT EXISTS）。
--   2) 单一写者：**偏离登记** —— 本 wave 原声明「唯一迁移 = 003」，
--      本文件是新增的第 4 个迁移。理由：链结构不变量必须落在 DB 层（应用层
--      纪律可被绕过，DB 约束不会），且 001 已应用不可改。
--   3) 前置条件：若库中已存在**历史分叉数据**（同租户两条记录共用同一 prev_hash），
--      本迁移会失败 —— 需先修复分叉数据再执行（正常链路不会出现分叉：
--      链从创世值起单链推进）。
--   4) 租户隔离：索引以 tenant_id 打头（与全库索引约定一致）。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 哈希链 DB 级不变量：同租户内 prev_hash 唯一
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_log_tenant_prev_hash
    ON audit_log (tenant_id, prev_hash);

COMMENT ON INDEX idx_audit_log_tenant_prev_hash IS
    '哈希链 DB 级不变量（红线③）：同租户内 prev_hash 唯一 —— 「同一链尾被写两次」直接写失败（静默断链 → 显式阻断）';

INSERT INTO schema_migration (version, description)
VALUES ('004_audit_log_chain_unique',
        'audit_log 哈希链唯一索引：同租户 prev_hash 唯一（红线③ DB 级不变量）')
ON CONFLICT (version) DO UPDATE SET applied_at = now();

COMMIT;
