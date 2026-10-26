-- ============================================================================
-- migrations/001_core_tables.sql
-- GovernCRM MVP 核心数据模型（T0.2）
--
-- 权威依据：
--   - docs/specs/GovernCRM_技术架构方案_v5.2.md 十三章（数据模型）
--   - docs/specs/GovernCRM_技术架构方案_v5.3_补充章节.md 附录 E（数字员工实体与配置化）
--   - docs/specs/GovernCRM_附录D_冻结版.md §4（rule_id 唯一权威登记表）
--   - docs/specs/GovernCRM_产品需求文档_PRD_v5.2.md 十二章（数据合规 C1–C11）
--
-- 表清单（14 张核心表）：
--   customer / customer_identity / customer_segment / action_candidate /
--   decision / audit_log / suppression_record / approval / tool_call /
--   worker / worker_template / worker_metrics / skill_card / cost_record
-- 附表：
--   schema_migration（框架自身辅助表：迁移台账）
--   memory_episode / memory_quarantine（记忆表结构先留，见 MVP_任务拆解 §1.2 X1；
--     memory_episode 按 v5.3 附录 E §E.4 带 scope_key，记忆按「租户 + 数字员工」隔离）
--
-- 纪律：
--   1) 幂等：重复执行不报错（CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
--      CREATE OR REPLACE FUNCTION / DROP TRIGGER IF EXISTS）。
--   2) 单一写者：Wave 0 仅 T0.2 写迁移；Wave 3 唯一迁移 = T-E7 的 task_checkpoint
--      （**本文件不含 task_checkpoint**）。
--   3) 无明文 PII 列（PRD C1）：手机号/邮箱/地址一律以哈希或指纹存储。
--   4) rule_id 不得自造：所有存放 rule_id 的列都用 CHECK 约束锁死在附录 D §4 登记表内。
--   5) 租户隔离：所有业务表带 tenant_id，且所有索引以 tenant_id 打头。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. 迁移台账（框架自身辅助表）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migration (
    version     TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE schema_migration IS '迁移台账（框架辅助表）：记录已应用的迁移版本，append-only 语义由约定保证';


-- ---------------------------------------------------------------------------
-- 1. 数字员工（v5.3 附录 E）
--    顺序：worker_template 先于 worker（worker.template_id 外键指向它）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_template (
    template_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 UUID NULL,
    code                      TEXT NOT NULL,
    name                      TEXT NOT NULL,
    role_definition           JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_skill_binding     JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_strategy_binding  JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_autonomy_level    TEXT NOT NULL,
    runnable                  BOOLEAN NOT NULL DEFAULT TRUE,
    is_builtin                BOOLEAN NOT NULL DEFAULT FALSE,
    version                   INTEGER NOT NULL DEFAULT 1,
    created_by                UUID NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_worker_template_autonomy
        CHECK (default_autonomy_level IN ('L0', 'L1', 'L2', 'L3')),
    -- 内置模板不属于任何租户（平台级），租户自建模板必须带 tenant_id
    CONSTRAINT chk_worker_template_builtin_scope
        CHECK ((is_builtin AND tenant_id IS NULL) OR (NOT is_builtin))
);

COMMENT ON TABLE worker_template IS
    '数字员工模板：内置「沉睡唤醒专员」(runnable=true) 与「售后关怀专员」(runnable=false)；模板 != 实例（v5.3 附录 E §E.3）';
COMMENT ON COLUMN worker_template.tenant_id IS 'NULL = 平台内置模板；非空 = 租户自建模板';
COMMENT ON COLUMN worker_template.runnable IS '是否可运行：MVP 唯一可运行的是 SLEEPING_WAKEUP_SPECIALIST（裁决 #3）';
COMMENT ON COLUMN worker_template.default_strategy_binding IS
    '默认策略覆盖项（差量，非替换）；不得覆盖抑制红线 / NO_CONSENT';


CREATE TABLE IF NOT EXISTS worker (
    worker_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL,
    template_id         UUID NULL REFERENCES worker_template (template_id),
    name                TEXT NOT NULL,
    role_definition     JSONB NOT NULL DEFAULT '{}'::jsonb,
    autonomy_level      TEXT NOT NULL DEFAULT 'L0',
    skill_binding       JSONB NOT NULL DEFAULT '{}'::jsonb,
    strategy_binding    JSONB NOT NULL DEFAULT '{}'::jsonb,
    memory_scope_key    TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',
    priority            INTEGER NOT NULL DEFAULT 100,
    version             INTEGER NOT NULL DEFAULT 1,
    created_by          UUID NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_worker_autonomy
        CHECK (autonomy_level IN ('L0', 'L1', 'L2', 'L3')),
    CONSTRAINT chk_worker_status
        CHECK (status IN ('draft', 'active', 'suspended', 'archived')),
    -- 记忆空间键格式固定 {tenant_id}:{worker_id}（v5.3 附录 E §E.2）
    CONSTRAINT chk_worker_memory_scope_key
        CHECK (memory_scope_key = tenant_id::text || ':' || worker_id::text),
    -- 策略包绑定是「覆盖」不是「替换」：红线键一律拒绝写入（写入期即拒绝，见 v5.3 附录 E §E.2 第 2 条）
    CONSTRAINT chk_worker_strategy_binding_no_redline
        CHECK (NOT (strategy_binding ?| ARRAY[
            'suppression', 'suppress', 'redline', 'deny',
            'no_consent', 'consent', 'exemption'
        ])),
    CONSTRAINT uq_worker_memory_scope_key UNIQUE (memory_scope_key)
);

COMMENT ON TABLE worker IS
    '数字员工实例：角色定义 + 技能包 + 策略包 + 记忆空间 + 自主度等级 五要素（PRD 7.1 / v5.3 附录 E §E.2）';
COMMENT ON COLUMN worker.skill_binding IS '技能包绑定：技能卡片 ID → 版本号 的映射；只存绑定关系，不存技能内容';
COMMENT ON COLUMN worker.strategy_binding IS '策略包绑定：对 governance.yaml 种子参数的按员工差量覆盖；红线不可覆盖';
COMMENT ON COLUMN worker.memory_scope_key IS '记忆空间键 {tenant_id}:{worker_id}；memory_episode.scope_key 引用本列';
COMMENT ON COLUMN worker.priority IS '员工优先级（数值小者优先）；多员工冲突消解用（v5.3 附录 E §E.5，v0.2 生效）';
COMMENT ON COLUMN worker.autonomy_level IS '自主度等级；MVP 有效值仅 L0/L1（裁决 #5）';


-- ---------------------------------------------------------------------------
-- 2. 客户域
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer (
    customer_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL,
    platform           TEXT NOT NULL,
    shop_id            TEXT NOT NULL,
    ext_key            TEXT NOT NULL,
    alias              TEXT NULL,
    -- 明文 PII 一律不落库（PRD C1）：只存哈希 / 指纹
    phone_hash         TEXT NULL,
    email_hash         TEXT NULL,
    address_fingerprint TEXT NULL,
    -- 知情同意三字段（PRD C4 / 裁决 #15）：发送前强制校验，无授权 → NO_CONSENT → DENY
    consent_status     TEXT NOT NULL DEFAULT 'unknown',
    consent_time       TIMESTAMPTZ NULL,
    consent_source     TEXT NULL,
    -- 分层与抑制红线的判定输入（裁决 #11 / 附录 D 规则 1、3、5）
    total_amount       NUMERIC(14, 2) NOT NULL DEFAULT 0,
    order_count        INTEGER NOT NULL DEFAULT 0,
    last_order_at      TIMESTAMPTZ NULL,
    avg_order_value_90d NUMERIC(14, 2) NULL,
    complaint_count_90d INTEGER NOT NULL DEFAULT 0,
    has_refund_dispute BOOLEAN NOT NULL DEFAULT FALSE,
    refund_rate        NUMERIC(8, 6) NULL,
    explicit_refusal   BOOLEAN NOT NULL DEFAULT FALSE,
    is_top_value       BOOLEAN NOT NULL DEFAULT FALSE,
    status             TEXT NOT NULL DEFAULT 'active',
    _synthetic         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_customer_consent_status
        CHECK (consent_status IN ('granted', 'denied', 'expired', 'revoked', 'unknown')),
    CONSTRAINT chk_customer_status
        CHECK (status IN ('active', 'merged', 'archived')),
    CONSTRAINT uq_customer_platform_shop_extkey
        UNIQUE (platform, shop_id, ext_key)
);

COMMENT ON TABLE customer IS
    '统一客户：无明文 PII 列 + 知情同意三字段（PRD C1 / C4；v5.2 十三章）';
COMMENT ON COLUMN customer.avg_order_value_90d IS
    '近 90 天客单价：权益净值折算式输入（附录 D 规则 5；窗口由 governance.yaml 参数化）';
COMMENT ON COLUMN customer.is_top_value IS '消费金额处于租户 Top 1%：命中 SUP_HIGH_VALUE → DENY + 转人工';
COMMENT ON COLUMN customer._synthetic IS '合成数据标记（MVP 数据集全部为 true）';


CREATE TABLE IF NOT EXISTS customer_identity (
    identity_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL,
    customer_id         UUID NULL REFERENCES customer (customer_id),
    platform            TEXT NOT NULL,
    shop_id             TEXT NOT NULL,
    ext_key             TEXT NOT NULL,
    match_basis         TEXT NOT NULL,
    confidence_tier     TEXT NOT NULL,
    confidence_score    NUMERIC(6, 4) NULL,
    phone_hash          TEXT NULL,
    address_fingerprint TEXT NULL,
    review_status       TEXT NOT NULL DEFAULT 'not_required',
    reviewed_by         UUID NULL,
    reviewed_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 三档匹配依据（架构 v5.2 §9.5 / 裁决 #17）
    CONSTRAINT chk_customer_identity_basis
        CHECK (match_basis IN ('ext_key_exact', 'phone_hash_address_fingerprint', 'no_feature')),
    CONSTRAINT chk_customer_identity_tier
        CHECK (confidence_tier IN ('high', 'medium', 'low')),
    CONSTRAINT chk_customer_identity_review_status
        CHECK (review_status IN ('not_required', 'pending_review', 'confirmed', 'rejected')),
    -- 低置信不得强行合并（PRD P1 红线）：low 档不得指向已归集客户
    CONSTRAINT chk_customer_identity_low_not_merged
        CHECK (confidence_tier <> 'low' OR customer_id IS NULL),
    CONSTRAINT uq_customer_identity_platform_shop_extkey
        UNIQUE (tenant_id, platform, shop_id, ext_key)
);

COMMENT ON TABLE customer_identity IS
    '跨平台身份映射：存匹配依据与置信档位，可回放；低置信保留独立身份（P1 红线）';
COMMENT ON COLUMN customer_identity.customer_id IS 'NULL = 未归集（低置信独立身份 / 待人工复核）';
COMMENT ON COLUMN customer_identity.review_status IS '中置信进人工复核队列（E4）；复核前不归集';


CREATE TABLE IF NOT EXISTS customer_segment (
    segment_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL,
    customer_id       UUID NOT NULL REFERENCES customer (customer_id),
    scope             TEXT NOT NULL,
    platform          TEXT NULL,
    segment_rule_code TEXT NOT NULL DEFAULT 'NINE_GRID',
    rule_version      TEXT NOT NULL,
    layer_score       INTEGER NOT NULL,
    amount_score      INTEGER NOT NULL,
    recency_score     INTEGER NOT NULL,
    amount_tier       TEXT NOT NULL,
    recency_tier      TEXT NOT NULL,
    dimension_values  JSONB NOT NULL DEFAULT '{}'::jsonb,
    effective_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- scope：全局层 + 平台局部层，两套独立层级体系（附录 D 规则 3 / 4）
    CONSTRAINT chk_customer_segment_scope
        CHECK (scope IN ('global', 'platform_local')),
    -- layer_score = amount_score × 10 + recency_score，取值 11..33
    CONSTRAINT chk_customer_segment_layer_score
        CHECK (layer_score = amount_score * 10 + recency_score),
    CONSTRAINT chk_customer_segment_amount_score
        CHECK (amount_score IN (1, 2, 3)),
    CONSTRAINT chk_customer_segment_recency_score
        CHECK (recency_score IN (1, 2, 3)),
    CONSTRAINT chk_customer_segment_amount_tier
        CHECK (amount_tier IN ('high', 'mid', 'low')),
    CONSTRAINT chk_customer_segment_recency_tier
        CHECK (recency_tier IN ('recent', 'mid', 'stale')),
    CONSTRAINT chk_customer_segment_scope_platform
        CHECK (scope <> 'platform_local' OR platform IS NOT NULL)
);

COMMENT ON TABLE customer_segment IS
    '分层结果：全局九宫格 + 平台局部九宫格两套（裁决 #11 / 附录 D 规则 3、4）';
COMMENT ON COLUMN customer_segment.segment_rule_code IS
    '分层规则代码（非决策 rule_id，不在附录 D §4 登记表内）';
COMMENT ON COLUMN customer_segment.rule_version IS '分层所用策略版本（governance.yaml@N）';


-- ---------------------------------------------------------------------------
-- 3. 候选动作与决策
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS action_candidate (
    candidate_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL,
    task_id             UUID NULL,
    worker_id           UUID NULL REFERENCES worker (worker_id),
    customer_id         UUID NOT NULL REFERENCES customer (customer_id),
    platform            TEXT NOT NULL,
    channel             TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    content_template_code TEXT NULL,
    coupon_ref          TEXT NULL,
    benefit_face_value  NUMERIC(12, 2) NULL,
    benefit_threshold   NUMERIC(12, 2) NULL,
    benefit_net_value   NUMERIC(12, 2) NULL,
    global_layer_score  INTEGER NULL,
    platform_layer_score INTEGER NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    idempotency_key     TEXT NULL,
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_action_candidate_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'suppressed', 'executed', 'cancelled')),
    CONSTRAINT chk_action_candidate_benefit_net
        CHECK (benefit_net_value IS NULL OR benefit_net_value >= 0)
);

COMMENT ON TABLE action_candidate IS
    'Agent 产出的动作候选：未过闸门不得执行；多候选共存是冲突消解的输入';
COMMENT ON COLUMN action_candidate.benefit_net_value IS
    '权益净值（附录 D 规则 5 折算式结果，下限 0）';
COMMENT ON COLUMN action_candidate.platform_layer_score IS
    '平台局部层级 score（附录 D 规则 3 键①；全局层级是客户级属性，无法区分同客户候选）';


-- 注：decision 表在 MVP 用普通表 + 复合索引实现（X13：分区表后置），
--     但**预留分区**：将来改造成 RANGE(created_at) 分区表时，
--     下列所有索引都已把 created_at 放在末位，索引定义可原样保留。
CREATE TABLE IF NOT EXISTS decision (
    decision_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL,
    task_id             UUID NULL,
    candidate_id        UUID NULL REFERENCES action_candidate (candidate_id),
    customer_id         UUID NULL REFERENCES customer (customer_id),
    worker_id           UUID NULL REFERENCES worker (worker_id),
    conclusion          TEXT NOT NULL,
    hit_rule_ids        TEXT[] NOT NULL DEFAULT '{}'::text[],
    suppressed_rule_ids TEXT[] NOT NULL DEFAULT '{}'::text[],
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_version       TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    skill_version       TEXT NOT NULL,
    rule_version        TEXT NOT NULL,
    trace_id            TEXT NULL,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_decision_conclusion
        CHECK (conclusion IN ('ALLOW', 'REQUIRE_APPROVAL', 'DENY')),
    -- rule_id 锁死在附录 D §4 登记表内，禁止自造
    CONSTRAINT chk_decision_hit_rule_ids
        CHECK (hit_rule_ids <@ ARRAY[
            'SUP_HIGH_VALUE', 'SUP_HIGH_COMPLAINT', 'SUP_HIGH_REFUND',
            'SUP_EXPLICIT_REFUSAL', 'NO_CONSENT', 'FREQ_GLOBAL_7D',
            'CONFLICT_WINDOW_72H', 'LAYER_RESOLVE', 'BENEFIT_MAX',
            'ESCALATE_HUMAN', 'PATH_ASSERT'
        ]::text[]),
    CONSTRAINT chk_decision_suppressed_rule_ids
        CHECK (suppressed_rule_ids <@ ARRAY[
            'SUP_HIGH_VALUE', 'SUP_HIGH_COMPLAINT', 'SUP_HIGH_REFUND',
            'SUP_EXPLICIT_REFUSAL', 'NO_CONSENT', 'FREQ_GLOBAL_7D',
            'CONFLICT_WINDOW_72H', 'LAYER_RESOLVE', 'BENEFIT_MAX',
            'ESCALATE_HUMAN', 'PATH_ASSERT'
        ]::text[])
);

COMMENT ON TABLE decision IS
    '策略内核判定结果：命中规则 ID + 四版本号 + 证据快照；含 PATH_ASSERT 类判定';
COMMENT ON COLUMN decision.hit_rule_ids IS '命中的规则 ID 集合（多命中全记，附录 D 规则 1）';
COMMENT ON COLUMN decision.suppressed_rule_ids IS '被抑制（他规则压制）的规则 ID 集合，与 hit_rule_ids 分开记';
COMMENT ON COLUMN decision.evidence IS '证据快照：LAYER_RESOLVE / BENEFIT_MAX 等过程性记录写在此处';
COMMENT ON COLUMN decision.input_snapshot IS '输入快照（PRD C1：任意历史动作 3 步内还原决策链）';


-- ---------------------------------------------------------------------------
-- 4. 审批责任链（任务级批量，ADR-032）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval (
    approval_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL,
    batch_id              UUID NOT NULL,
    task_id               UUID NULL,
    decision_id           UUID NULL REFERENCES decision (decision_id),
    candidate_snapshot_ref TEXT NOT NULL,
    impact_tier           TEXT NOT NULL,
    impact_customer_count INTEGER NOT NULL DEFAULT 0,
    route_signers         TEXT[] NOT NULL DEFAULT '{}'::text[],
    required_signatures   INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL DEFAULT 'pending',
    applicant_id          UUID NULL,
    applicant_role        TEXT NULL,
    approver_id           UUID NULL,
    approver_role         TEXT NULL,
    signed_at             TIMESTAMPTZ NULL,
    decision_note         TEXT NULL,
    timeout_policy        TEXT NOT NULL DEFAULT 'expire_48h_escalate_24h',
    expires_at            TIMESTAMPTZ NOT NULL,
    escalate_remind_at    TIMESTAMPTZ NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 三档路由（裁决 #25）：≤10 单签 / 11–100 双签 / >100 双签 + 合规知会
    CONSTRAINT chk_approval_impact_tier
        CHECK (impact_tier IN ('tier_1', 'tier_2', 'tier_3')),
    CONSTRAINT chk_approval_status
        CHECK (status IN ('pending', 'signed', 'rejected', 'expired', 'cancelled')),
    CONSTRAINT chk_approval_impact_count
        CHECK (impact_customer_count >= 0),
    CONSTRAINT chk_approval_required_signatures
        CHECK (required_signatures >= 1),
    CONSTRAINT chk_approval_escalate_before_expire
        CHECK (escalate_remind_at IS NULL OR escalate_remind_at <= expires_at)
);

COMMENT ON TABLE approval IS
    '审批责任链：责任链绑定批次快照（ADR-032）；批次快照一经生成不可改';
COMMENT ON COLUMN approval.candidate_snapshot_ref IS '候选名单快照引用（不可变）';
COMMENT ON COLUMN approval.timeout_policy IS '超时策略：48h 失效 / 剩余 24h 升级提醒（裁决 #25）';
COMMENT ON COLUMN approval.route_signers IS '路由签署人角色列表（单签 / 双签 / 双签 + 合规知会）';


-- ---------------------------------------------------------------------------
-- 5. 审计（append-only + 哈希链）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL,
    record_seq       BIGSERIAL NOT NULL,
    trace_id         TEXT NOT NULL,
    task_id          UUID NULL,
    worker_id        UUID NULL,
    decision_id      UUID NULL REFERENCES decision (decision_id),
    approval_id      UUID NULL REFERENCES approval (approval_id),
    actor_type       TEXT NOT NULL DEFAULT 'system',
    actor_id         TEXT NULL,
    actor_role       TEXT NULL,
    action_type      TEXT NOT NULL,
    object_type      TEXT NULL,
    object_id        TEXT NULL,
    hit_rule_ids     TEXT[] NOT NULL DEFAULT '{}'::text[],
    model_version    TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    skill_version    TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    input_snapshot   JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash        TEXT NOT NULL,
    record_hash      TEXT NOT NULL,
    hash_algo        TEXT NOT NULL DEFAULT 'sha256',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_audit_actor_type
        CHECK (actor_type IN ('system', 'user', 'agent', 'provider')),
    CONSTRAINT chk_audit_hit_rule_ids
        CHECK (hit_rule_ids <@ ARRAY[
            'SUP_HIGH_VALUE', 'SUP_HIGH_COMPLAINT', 'SUP_HIGH_REFUND',
            'SUP_EXPLICIT_REFUSAL', 'NO_CONSENT', 'FREQ_GLOBAL_7D',
            'CONFLICT_WINDOW_72H', 'LAYER_RESOLVE', 'BENEFIT_MAX',
            'ESCALATE_HUMAN', 'PATH_ASSERT'
        ]::text[]),
    CONSTRAINT chk_audit_record_hash
        CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_audit_prev_hash
        CHECK (prev_hash ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE audit_log IS
    '审计流水：append-only（触发器拒绝 UPDATE/DELETE/TRUNCATE）+ 哈希链 + 四版本号';
COMMENT ON COLUMN audit_log.record_seq IS '哈希链序号（全局单调递增，链的遍历顺序）';
COMMENT ON COLUMN audit_log.prev_hash IS '哈希链：前一条记录的 record_hash（创世值为 64 个 0）';
COMMENT ON COLUMN audit_log.record_hash IS '哈希链：本条记录内容摘要 = sha256(prev_hash || 规范化内容)';
COMMENT ON COLUMN audit_log.hash_algo IS '哈希算法标识（默认 sha256）';
COMMENT ON COLUMN audit_log.actor_role IS '审批人角色（PRD C1：回放需看到审批人）';
COMMENT ON COLUMN audit_log.input_snapshot IS '输入快照：审计写入失败即阻断业务（红线③）';


-- ---------------------------------------------------------------------------
-- 6. 未触达记录（抑制）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppression_record (
    suppression_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL,
    task_id          UUID NULL,
    decision_id      UUID NULL REFERENCES decision (decision_id),
    candidate_id     UUID NULL REFERENCES action_candidate (candidate_id),
    customer_id      UUID NOT NULL REFERENCES customer (customer_id),
    worker_id        UUID NULL REFERENCES worker (worker_id),
    rule_id          TEXT NOT NULL,
    reason_detail    TEXT NULL,
    channel          TEXT NULL,
    cooling_remaining_seconds INTEGER NULL,
    channel_cooling_remaining_seconds INTEGER NULL,
    deny_retry_counted BOOLEAN NOT NULL DEFAULT FALSE,
    suppressed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_suppression_rule_id
        CHECK (rule_id IN (
            'SUP_HIGH_VALUE', 'SUP_HIGH_COMPLAINT', 'SUP_HIGH_REFUND',
            'SUP_EXPLICIT_REFUSAL', 'NO_CONSENT', 'FREQ_GLOBAL_7D',
            'CONFLICT_WINDOW_72H', 'LAYER_RESOLVE', 'BENEFIT_MAX',
            'ESCALATE_HUMAN', 'PATH_ASSERT'
        )),
    CONSTRAINT chk_suppression_cooling
        CHECK (cooling_remaining_seconds IS NULL OR cooling_remaining_seconds >= 0),
    CONSTRAINT chk_suppression_channel_cooling
        CHECK (channel_cooling_remaining_seconds IS NULL OR channel_cooling_remaining_seconds >= 0)
);

COMMENT ON TABLE suppression_record IS
    '未触达记录（含原因）：治理看板的计量来源；批量判定阶段单客户 DENY 正常写入本表';
COMMENT ON COLUMN suppression_record.deny_retry_counted IS
    '恒为 false：批量判定阶段单客户 DENY 不计入 deny_retry（裁决 #9 / ADR-036）';
COMMENT ON COLUMN suppression_record.cooling_remaining_seconds IS '频控延迟类返回值（附录 D 规则 2）';


-- ---------------------------------------------------------------------------
-- 7. 工具调用
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_call (
    tool_call_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL,
    task_id          UUID NULL,
    worker_id        UUID NULL REFERENCES worker (worker_id),
    tool_name        TEXT NOT NULL,
    tool_call_type   TEXT NOT NULL DEFAULT 'internal',
    idempotency_key  TEXT NOT NULL,
    request_summary  JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'success',
    error_code       TEXT NULL,
    cost_amount      NUMERIC(14, 6) NULL,
    cost_currency    TEXT NULL,
    cost_tokens      INTEGER NULL,
    trace_id         TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ NULL,
    CONSTRAINT chk_tool_call_type
        CHECK (tool_call_type IN ('external_send', 'read', 'write', 'internal')),
    CONSTRAINT chk_tool_call_status
        CHECK (status IN ('success', 'failed', 'timeout', 'denied')),
    CONSTRAINT uq_tool_call_idempotency
        UNIQUE (tenant_id, idempotency_key)
);

COMMENT ON TABLE tool_call IS
    '工具调用记录：含幂等键、成本、trace_id；external_send 类调用必过治理闸门';
COMMENT ON COLUMN tool_call.tool_call_type IS
    'external_send 类动作同样必须过闸门（智能体不是后门，v5.3 附录 G §G.2.2）';


-- ---------------------------------------------------------------------------
-- 8. 技能卡（版本化 + draft/published 双门禁）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skill_card (
    skill_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL,
    owner_worker_id   UUID NULL REFERENCES worker (worker_id),
    skill_code        TEXT NOT NULL,
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'draft',
    skill_content     JSONB NOT NULL DEFAULT '{}'::jsonb,
    regression_passed BOOLEAN NOT NULL DEFAULT FALSE,
    human_confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    published_at      TIMESTAMPTZ NULL,
    published_by      UUID NULL,
    created_by        UUID NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_skill_card_status
        CHECK (status IN ('draft', 'published', 'deprecated')),
    -- 双门禁：技能回归评测通过 + 人工确认，方可发布（裁决 #10a）
    CONSTRAINT chk_skill_card_publish_gates
        CHECK (status <> 'published' OR (regression_passed AND human_confirmed)),
    CONSTRAINT uq_skill_card_code_version
        UNIQUE (tenant_id, skill_code, version)
);

COMMENT ON TABLE skill_card IS
    '技能卡片（版本化）：draft 草稿卡 → published 发布卡；草稿卡不自动进入运行中员工技能包';
COMMENT ON COLUMN skill_card.owner_worker_id IS 'NULL = 平台级共享技能卡；非空 = 员工私有（v5.3 附录 E §E.4）';
COMMENT ON COLUMN skill_card.skill_content IS '技能内容（脱敏后存储，PRD P4：技能库不得含 PII）';


-- ---------------------------------------------------------------------------
-- 9. 数字员工指标聚合（晋级校验 G1/G2 + 运营效率四项口径）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_metrics (
    metric_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL,
    worker_id        UUID NOT NULL REFERENCES worker (worker_id),
    stat_date        DATE NOT NULL,
    action_type      TEXT NOT NULL,
    channel          TEXT NOT NULL,
    autonomy_level   TEXT NOT NULL DEFAULT 'L0',
    execution_count  INTEGER NOT NULL DEFAULT 0,
    success_count    INTEGER NOT NULL DEFAULT 0,
    correction_count INTEGER NOT NULL DEFAULT 0,
    candidate_count  INTEGER NOT NULL DEFAULT 0,
    adopted_count    INTEGER NOT NULL DEFAULT 0,
    candidate_to_send_avg_ms BIGINT NULL,
    contains_synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_worker_metrics_autonomy
        CHECK (autonomy_level IN ('L0', 'L1', 'L2', 'L3')),
    CONSTRAINT chk_worker_metrics_counts
        CHECK (execution_count >= 0 AND success_count >= 0 AND correction_count >= 0
               AND candidate_count >= 0 AND adopted_count >= 0),
    -- 「同类型动作」= action_type + channel 组合（裁决 #22）
    CONSTRAINT uq_worker_metrics_grain
        UNIQUE (tenant_id, worker_id, stat_date, action_type, channel)
);

COMMENT ON TABLE worker_metrics IS
    '数字员工执行指标聚合：支撑晋级校验 G1/G2 与运营效率四项指标（裁决 #21/#22）';
COMMENT ON COLUMN worker_metrics.contains_synthetic IS '口径标注：含模拟执行（模拟回执 status=success 计入）';
COMMENT ON COLUMN worker_metrics.autonomy_level IS '统计时的自主度快照（晋级后指标按等级分列）';


-- ---------------------------------------------------------------------------
-- 10. 成本记录（按员工 / 任务 / 日）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_record (
    cost_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL,
    worker_id         UUID NOT NULL REFERENCES worker (worker_id),
    task_id           UUID NULL,
    stat_date         DATE NOT NULL,
    provider_id       TEXT NULL,
    model_version     TEXT NULL,
    token_count       INTEGER NOT NULL DEFAULT 0,
    cost_amount       NUMERIC(14, 6) NOT NULL DEFAULT 0,
    cost_currency     TEXT NOT NULL DEFAULT 'CNY',
    budget_cap_amount NUMERIC(14, 6) NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_cost_record_counts
        CHECK (token_count >= 0 AND cost_amount >= 0)
);

COMMENT ON TABLE cost_record IS
    '成本记录：按员工 / 任务 / 日聚合（裁决 #27：单任务 50 万 Token / ¥20，单员工日 ¥200）';
COMMENT ON COLUMN cost_record.budget_cap_amount IS '预算上限快照（裁决 #27 默认值）';


-- ---------------------------------------------------------------------------
-- 11. 记忆表（结构先留，逻辑后置 v0.3 —— MVP_任务拆解 §1.2 X1）
--     memory_episode 按 v5.3 附录 E §E.4 带 scope_key（按租户 + 数字员工隔离）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_episode (
    episode_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    scope_key    TEXT NOT NULL,
    worker_id    UUID NULL REFERENCES worker (worker_id),
    customer_id  UUID NULL REFERENCES customer (customer_id),
    source_id    TEXT NOT NULL,
    trust_level  TEXT NOT NULL DEFAULT 'unknown',
    gate_status  TEXT NOT NULL DEFAULT 'accepted',
    content      JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_memory_episode_trust_level
        CHECK (trust_level IN ('high', 'medium', 'low', 'unknown')),
    CONSTRAINT chk_memory_episode_gate_status
        CHECK (gate_status IN ('accepted', 'quarantined', 'rejected'))
);

COMMENT ON TABLE memory_episode IS
    '情景记忆（脱敏后存储）：按「租户 + 数字员工」隔离（scope_key 引用 worker.memory_scope_key）';
COMMENT ON COLUMN memory_episode.source_id IS '来源标注（PRD C10：检索结果须标来源与可信级别）';
COMMENT ON COLUMN memory_episode.trust_level IS '可信级别（PRD C10）';


CREATE TABLE IF NOT EXISTS memory_quarantine (
    quarantine_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    scope_key     TEXT NULL,
    worker_id     UUID NULL REFERENCES worker (worker_id),
    customer_id   UUID NULL REFERENCES customer (customer_id),
    episode_id    UUID NULL REFERENCES memory_episode (episode_id),
    reject_reason TEXT NOT NULL,
    raw_content   JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by   UUID NULL,
    reviewed_at   TIMESTAMPTZ NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_memory_quarantine_review_status
        CHECK (review_status IN ('pending', 'confirmed', 'dismissed', 'deleted'))
);

COMMENT ON TABLE memory_quarantine IS
    '记忆门禁拒绝的隔离内容：拒绝原因 + 原文 + 人工审查状态（PRD C6 / C10）';


-- ---------------------------------------------------------------------------
-- 12. 索引（全部以 tenant_id 打头；末位带 created_at 以便将来改造成分区表）
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_customer_tenant_platform ON customer (tenant_id, platform, shop_id);
CREATE INDEX IF NOT EXISTS idx_customer_tenant_consent ON customer (tenant_id, consent_status);
CREATE INDEX IF NOT EXISTS idx_customer_tenant_synthetic ON customer (tenant_id, _synthetic);

CREATE INDEX IF NOT EXISTS idx_customer_identity_tenant_customer
    ON customer_identity (tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_identity_tenant_review
    ON customer_identity (tenant_id, review_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_customer_segment_tenant_customer
    ON customer_segment (tenant_id, customer_id, scope);
CREATE INDEX IF NOT EXISTS idx_customer_segment_tenant_score
    ON customer_segment (tenant_id, scope, layer_score DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_action_candidate_tenant_customer
    ON action_candidate (tenant_id, customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_candidate_tenant_task
    ON action_candidate (tenant_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_candidate_tenant_status
    ON action_candidate (tenant_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_decision_tenant_customer
    ON decision (tenant_id, customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_tenant_task
    ON decision (tenant_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_tenant_conclusion
    ON decision (tenant_id, conclusion, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_tenant_hit_rules
    ON decision USING GIN (hit_rule_ids);

CREATE INDEX IF NOT EXISTS idx_approval_tenant_batch
    ON approval (tenant_id, batch_id, status);
CREATE INDEX IF NOT EXISTS idx_approval_tenant_status
    ON approval (tenant_id, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_task
    ON audit_log (tenant_id, task_id, record_seq);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created
    ON audit_log (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_trace
    ON audit_log (tenant_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_action
    ON audit_log (tenant_id, action_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_suppression_record_tenant_rule
    ON suppression_record (tenant_id, rule_id, suppressed_at DESC);
CREATE INDEX IF NOT EXISTS idx_suppression_record_tenant_customer
    ON suppression_record (tenant_id, customer_id, suppressed_at DESC);

CREATE INDEX IF NOT EXISTS idx_tool_call_tenant_task
    ON tool_call (tenant_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_call_tenant_trace
    ON tool_call (tenant_id, trace_id);

CREATE INDEX IF NOT EXISTS idx_worker_tenant_status
    ON worker (tenant_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_worker_tenant_template
    ON worker (tenant_id, template_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_template_builtin_code
    ON worker_template (code) WHERE tenant_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_worker_template_tenant_code
    ON worker_template (tenant_id, code);

CREATE INDEX IF NOT EXISTS idx_skill_card_tenant_owner
    ON skill_card (tenant_id, owner_worker_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_card_tenant_code
    ON skill_card (tenant_id, skill_code, version);

CREATE INDEX IF NOT EXISTS idx_worker_metrics_tenant_date
    ON worker_metrics (tenant_id, stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_worker_metrics_tenant_worker
    ON worker_metrics (tenant_id, worker_id, stat_date DESC);

CREATE INDEX IF NOT EXISTS idx_cost_record_tenant_worker_date
    ON cost_record (tenant_id, worker_id, stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_cost_record_tenant_task
    ON cost_record (tenant_id, task_id, stat_date DESC);

CREATE INDEX IF NOT EXISTS idx_memory_episode_tenant_scope
    ON memory_episode (tenant_id, scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_quarantine_tenant_status
    ON memory_quarantine (tenant_id, review_status, created_at DESC);


-- ---------------------------------------------------------------------------
-- 13. 触发器：审计 append-only（红线③）
--     UPDATE / DELETE 一律拒绝；TRUNCATE 同样拒绝。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_log_append_only_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log 是 append-only 审计表：% 操作被拒绝（audit_id=%, tenant_id=%）。'
        '审计记录不可修改或删除，变更只能追加新记录。',
        TG_OP, OLD.audit_id, OLD.tenant_id
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION audit_log_append_only_guard() IS
    '审计 append-only 行级守卫：UPDATE / DELETE 一律抛错（PRD C7 / 红线③）';

DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log;
CREATE TRIGGER trg_audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only_guard();


CREATE OR REPLACE FUNCTION audit_log_truncate_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log 是 append-only 审计表：TRUNCATE 操作被拒绝（tenant 级清空即破坏哈希链）。'
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION audit_log_truncate_guard() IS '审计 append-only 语句级守卫：TRUNCATE 一律抛错';

DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log;
CREATE TRIGGER trg_audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_truncate_guard();


-- ---------------------------------------------------------------------------
-- 14. 迁移台账登记（幂等）
-- ---------------------------------------------------------------------------
INSERT INTO schema_migration (version, description)
VALUES ('001_core_tables', 'MVP 核心表（14 张）+ 审计 append-only 触发器 + 记忆表结构预留')
ON CONFLICT (version) DO UPDATE SET applied_at = now();

COMMIT;
