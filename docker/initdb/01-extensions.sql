-- 容器首次初始化时执行（数据卷为空时跑一次）。
--
-- 范围限定：**只启用扩展，不建业务表**。
-- 业务表由 migrations/*.sql 原始 SQL 管理，Wave 0 内唯一写者是 T0.2，
-- 迁移与应用启动分离（见 .trellis/spec/backend/database-guidelines.md）。

-- 向量扩展：架构 v5.2 §3.7 选用 PostgreSQL 16 + pgvector 三合一（关系 + 队列 + 向量）
CREATE EXTENSION IF NOT EXISTS vector;
