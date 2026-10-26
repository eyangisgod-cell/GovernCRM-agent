"""governance.yaml 的加载与校验（策略内核的一部分）。

红线 1：本模块必须是**纯函数**——零 LLM、零框架依赖、零 I/O 副作用（除了读一次 YAML）。
只依赖标准库 + PyYAML（PyYAML 是解析器，不是框架；LICENSES.md 已核准入）。

职责边界（T0.4）：只做「配置加载 + 校验 + 红线保护」。
**规则 1–6 的判定逻辑在 T-B2**，本文件不实现任何判定。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "REDLINE_PATHS",
    "REDLINE_RULE_IDS",
    "Approval",
    "Autonomy",
    "Benefit",
    "Budget",
    "ConfigError",
    "ConflictResolution",
    "Distillation",
    "GovernanceConfig",
    "RuleValidationError",
    "Segmentation",
    "Thresholds",
    "apply_overrides",
    "load_and_validate",
]


class ConfigError(ValueError):
    """配置错误。所有消息必须**可操作**：说清缺哪个项、去哪配、有哪些可选值。"""


#: 配置校验错误的**专名**（T-B3 拆解验收点名了这个名字）。
#:
#: 做成**别名**而非子类，是为了让两边的 `except` 都成立：
#:   - 既有调用方 `except ConfigError` —— 不受影响（别名即同一类型）；
#:   - 新调用方 `except RuleValidationError` —— 能捕获本模块抛出的**全部**配置错误
#:     （若改成子类，既有 `raise ConfigError(...)` 的异常反而捕不到，等于要求改写全部抛出点）。
#: 详见《MVP_任务拆解.md》§三 · T-B3 与 `.trellis/tasks/10-26-t-b3-rule-load-failfast/prd.md` §2.1。
RuleValidationError = ConfigError


# ---------------------------------------------------------------------------
# 冻结常量
# ---------------------------------------------------------------------------

#: 抑制红线规则 ID（FR-01b / 附录D §4）。这些规则**对任何角色都不可绕过**，
#: 员工级策略包不得覆盖。其中 SUP_EXPLICIT_REFUSAL 与 NO_CONSENT **没有**
#: 可配置参数（硬编码于代码层），因此在 REDLINE_PATHS 里不出现——
#: 也就是说它们连被覆盖的入口都不存在。
REDLINE_RULE_IDS: tuple[str, ...] = (
    "SUP_HIGH_VALUE",
    "SUP_HIGH_COMPLAINT",
    "SUP_HIGH_REFUND",
    "SUP_EXPLICIT_REFUSAL",
    "NO_CONSENT",
)

#: 红线参数路径 → 其保护的 rule_id。`apply_overrides` 命中即拒绝。
#: 整个 thresholds 块都在红线内：它与 评测标注基准_v1.3.yaml 逐项锁定，
#: 任何改动都必须同改基准并过回归门禁，不属于员工级可覆盖范围。
REDLINE_PATHS: dict[str, str] = {
    "thresholds.complaint_window_days": "SUP_HIGH_COMPLAINT",
    "thresholds.refund_rate_threshold": "SUP_HIGH_REFUND",
    "thresholds.refund_dispute_window_days": "SUP_HIGH_REFUND",
    "thresholds.high_value_percentile": "SUP_HIGH_VALUE",
    "thresholds.freq_window_days": "FREQ_GLOBAL_7D",
    "thresholds.freq_max_touches": "FREQ_GLOBAL_7D",
    "thresholds.channel_cooldown_hours": "FREQ_GLOBAL_7D",
    "thresholds.conflict_window_hours": "CONFLICT_WINDOW_72H",
}

#: 非红线、允许员工级策略包覆盖的路径（白名单，未列出的路径一律拒绝）。
OVERRIDABLE_PATHS: frozenset[str] = frozenset(
    {
        "conflict_resolution.platform_priority_overrides",
        "benefit.floor",
        "approval.impact_tiers.single_sign_max",
        "approval.impact_tiers.dual_sign_max",
        "approval.timeout_hours",
        "approval.escalate_remind_hours_before",
        "budget.per_task_token_limit",
        "budget.per_task_amount_cents",
        "budget.per_worker_daily_amount_cents",
        "distillation.min_success_count",
    }
)


# ---------------------------------------------------------------------------
# 声明式 schema：类型 / 必填 / 取值范围 / 可操作描述
# ---------------------------------------------------------------------------

_INT = "int"
_FLOAT = "float"
_STR = "str"
_DICT = "dict"
_STR_LIST = "list[str]"


@dataclass(frozen=True)
class Field:
    kind: str
    desc: str
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    allowed: tuple[Any, ...] | None = None
    #: 列表的**精确内容**（含顺序）。非 None 时列表必须与之逐项相等——
    #: 用于附录D 冻结的三键排序这类「顺序不可改」的配置项。
    exact: tuple[Any, ...] | None = None
    #: 列表的最小元素个数。空列表会让配置失去意义，必须拒绝。
    min_items: int | None = None


SCHEMA: dict[str, Field] = {
    "version": Field(_STR, "配置版本号（语义化版本，变更时递增并追加变更记录）"),
    "updated_at": Field(_STR, "最后更新日期，YYYY-MM-DD"),
    # 一、thresholds —— 与 评测标注基准_v1.3.yaml 逐项相等
    "thresholds.complaint_window_days": Field(
        _INT, "SUP_HIGH_COMPLAINT 投诉回溯窗口（天）", minimum=1
    ),
    "thresholds.refund_rate_threshold": Field(
        _FLOAT, "SUP_HIGH_REFUND 退款率阈值", minimum=0, maximum=1
    ),
    "thresholds.refund_dispute_window_days": Field(
        _INT, "SUP_HIGH_REFUND 纠纷回溯窗口（天）", minimum=1
    ),
    "thresholds.high_value_percentile": Field(
        _FLOAT, "SUP_HIGH_VALUE 高价值分位（Top N%）", minimum=0, maximum=100
    ),
    "thresholds.freq_window_days": Field(_INT, "规则 2 频控滚动窗口（天）", minimum=1),
    "thresholds.freq_max_touches": Field(_INT, "规则 2 窗口内最大触达次数", minimum=1),
    "thresholds.channel_cooldown_hours": Field(_INT, "规则 2 同通道冷却（小时）", minimum=1),
    "thresholds.conflict_window_hours": Field(_INT, "规则 3 冲突窗口（小时）", minimum=1),
    # 二、九宫格分层
    "segmentation.layer_score_base": Field(
        _INT,
        "分层乘数。**锁死 10**：附录D 规则 3 规定 layer_score = amount_score × 10 + "
        "recency_score，且数据库 CHECK `chk_customer_segment_layer_score` 硬编码 ×10"
        "（migrations/001_core_tables.sql:225）。改成别的值会在写库时被拒。",
        allowed=(10,),
    ),
    "segmentation.amount.high_min_cents": Field(
        _INT, "金额档 high 的下界（分）；≥ 此值 = high = 3", minimum=0
    ),
    "segmentation.amount.mid_min_cents": Field(
        _INT, "金额档 mid 的下界（分）；≥ 此值且 < high = mid = 2", minimum=0
    ),
    "segmentation.recency.recent_max_days": Field(
        _INT, "近度档 recent 的上界（天）；≤ 此值 = recent = 3", minimum=1
    ),
    "segmentation.recency.mid_max_days": Field(
        _INT, "近度档 mid 的上界（天）；≤ 此值且 > recent = mid = 2", minimum=1
    ),
    # 三、冲突消解
    "conflict_resolution.tie_break_order": Field(
        _STR_LIST,
        "三键排序顺序（附录D 规则 3），顺序不可改",
        allowed=("platform_layer_score", "benefit_net_value", "created_at_earliest"),
        exact=("platform_layer_score", "benefit_net_value", "created_at_earliest"),
    ),
    "conflict_resolution.platform_priority_overrides": Field(
        _DICT, "平台优先级覆盖（数值小者优先）；未列出的平台走三键"
    ),
    # 四、权益净值
    "benefit.formula": Field(_STR, "权益净值折算式（附录D 规则 5）"),
    "benefit.floor": Field(_INT, "净值下限（不得低于此值）"),
    # 五、审批路由
    "approval.impact_tiers.single_sign_max": Field(
        _INT, "单签上限：影响面 ≤ 此值由运营负责人单签", minimum=1
    ),
    "approval.impact_tiers.dual_sign_max": Field(
        _INT, "双签上限：影响面 ≤ 此值需双签；超过则双签 + 合规知会", minimum=1
    ),
    "approval.timeout_hours": Field(
        _INT, "审批超时小时数（超时自动失效，**不默认通过**）", minimum=1
    ),
    "approval.escalate_remind_hours_before": Field(_INT, "超时前多少小时发升级提醒", minimum=1),
    # 六、预算
    "budget.per_task_token_limit": Field(_INT, "单任务 Token 上限", minimum=1),
    "budget.per_task_amount_cents": Field(_INT, "单任务金额上限（分）", minimum=1),
    "budget.per_worker_daily_amount_cents": Field(_INT, "单数字员工单日金额上限（分）", minimum=1),
    "budget.currency": Field(_STR, "预算币种", allowed=("CNY",)),
    # 七、蒸馏
    "distillation.min_success_count": Field(
        _INT, "轻量蒸馏触发阈值 N（同一动作模式成功执行次数）", minimum=1
    ),
    # 八、自主度
    "autonomy.mvp_allowed_levels": Field(
        _STR_LIST,
        "MVP 开放的自主度等级",
        allowed=("L0", "L1", "L2", "L3"),
        min_items=1,
    ),
}

#: **整值叶子**路径：SCHEMA 里 kind 为 `_DICT` 的项**整体作为一个叶子**，不递归摊平。
#:
#: 否则 `conflict_resolution.platform_priority_overrides` 的每个平台键都会被摊成
#: `conflict_resolution.platform_priority_overrides.<平台>`，脱离 SCHEMA 登记 →
#: 被当成未知字段拒绝，规则 3 的平台优先级覆盖在生产路径上永远装不进来。
#: （拍板③，2026-10-26）
WHOLE_VALUE_PATHS: frozenset[str] = frozenset(
    path for path, spec in SCHEMA.items() if spec.kind == _DICT
)


# ---------------------------------------------------------------------------
# 校验结果：不可变数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    complaint_window_days: int
    refund_rate_threshold: float
    refund_dispute_window_days: int
    high_value_percentile: float
    freq_window_days: int
    freq_max_touches: int
    channel_cooldown_hours: int
    conflict_window_hours: int


@dataclass(frozen=True)
class Segmentation:
    layer_score_base: int
    amount_high_min_cents: int
    amount_mid_min_cents: int
    recency_recent_max_days: int
    recency_mid_max_days: int


@dataclass(frozen=True)
class ConflictResolution:
    tie_break_order: tuple[str, ...]
    platform_priority_overrides: dict[str, int]


@dataclass(frozen=True)
class Benefit:
    formula: str
    floor: int


@dataclass(frozen=True)
class Approval:
    single_sign_max: int
    dual_sign_max: int
    timeout_hours: int
    escalate_remind_hours_before: int


@dataclass(frozen=True)
class Budget:
    per_task_token_limit: int
    per_task_amount_cents: int
    per_worker_daily_amount_cents: int
    currency: str


@dataclass(frozen=True)
class Distillation:
    min_success_count: int


@dataclass(frozen=True)
class Autonomy:
    mvp_allowed_levels: tuple[str, ...]


@dataclass(frozen=True)
class GovernanceConfig:
    version: str
    updated_at: str
    thresholds: Thresholds
    segmentation: Segmentation
    conflict_resolution: ConflictResolution
    benefit: Benefit
    approval: Approval
    budget: Budget
    distillation: Distillation
    autonomy: Autonomy


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 摊平成 "a.b.c" -> 值；空 dict 作为叶子保留。

    `WHOLE_VALUE_PATHS` 里的路径**不递归**（整值叶子，见该常量的说明）。
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if path in WHOLE_VALUE_PATHS:
            out[path] = value
        elif isinstance(value, dict) and value:
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _parent_of(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else "(顶层)"


def _type_ok(kind: str, value: Any) -> bool:
    if kind == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == _FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == _STR:
        return isinstance(value, str)
    if kind == _DICT:
        return isinstance(value, dict)
    if kind == _STR_LIST:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def _type_name(kind: str) -> str:
    return {
        _INT: "整数",
        _FLOAT: "数字",
        _STR: "字符串",
        _DICT: "映射（key: value）",
        _STR_LIST: "字符串列表",
    }[kind]


def _validate_leaf(path: str, spec: Field, value: Any, where: str) -> None:
    if not _type_ok(spec.kind, value):
        raise ConfigError(
            f"{where}：配置项 `{path}` 类型不对。\n"
            f"  用途：{spec.desc}\n"
            f"  期望：{_type_name(spec.kind)}，实际拿到 {type(value).__name__}（{value!r}）。\n"
            f"  请在 governance.yaml 的 `{_parent_of(path)}` 段改这一项。"
        )
    if spec.min_items is not None and isinstance(value, list) and len(value) < spec.min_items:
        raise ConfigError(
            f"{where}：配置项 `{path}` 元素太少。\n"
            f"  用途：{spec.desc}\n"
            f"  实际 {len(value)} 个（{value!r}），要求至少 {spec.min_items} 个。\n"
            f"  请在 governance.yaml 的 `{_parent_of(path)}` 段补齐——空列表会让该配置失去意义。"
        )
    if spec.exact is not None:
        actual = tuple(value) if isinstance(value, list) else (value,)
        if actual != spec.exact:
            raise ConfigError(
                f"{where}：配置项 `{path}` 与冻结值不一致。\n"
                f"  用途：{spec.desc}\n"
                f"  实际值：{list(actual)}\n"
                f"  冻结值：{list(spec.exact)}\n"
                f"  该配置项是**冻结语义**，内容与顺序都不可改。\n"
                f"  请在 governance.yaml 的 `{_parent_of(path)}` 段恢复为冻结值。"
            )
    if spec.allowed is not None:
        checked = tuple(value) if isinstance(value, list) else (value,)
        bad = [item for item in checked if item not in spec.allowed]
        if bad:
            raise ConfigError(
                f"{where}：配置项 `{path}` 取值越界。\n"
                f"  用途：{spec.desc}\n"
                f"  不允许的取值：{bad}\n"
                f"  可选值只有：{list(spec.allowed)}\n"
                f"  请在 governance.yaml 的 `{_parent_of(path)}` 段改成可选值之一。"
            )
        return
    if spec.minimum is not None or spec.maximum is not None:
        numeric = float(value)
        if spec.minimum is not None:
            too_small = (
                numeric <= spec.minimum if spec.exclusive_minimum else numeric < spec.minimum
            )
            if too_small:
                op = ">" if spec.exclusive_minimum else ">="
                raise ConfigError(
                    f"{where}：配置项 `{path}` 取值越界。\n"
                    f"  用途：{spec.desc}\n"
                    f"  实际值 {value!r}，要求 {op} {spec.minimum}。\n"
                    f"  请在 governance.yaml 的 `{_parent_of(path)}` 段改成范围内取值。"
                )
        if spec.maximum is not None and numeric > spec.maximum:
            raise ConfigError(
                f"{where}：配置项 `{path}` 取值越界。\n"
                f"  用途：{spec.desc}\n"
                f"  实际值 {value!r}，要求 <= {spec.maximum}。\n"
                f"  请在 governance.yaml 的 `{_parent_of(path)}` 段改成范围内取值。"
            )


def _validate_shape(raw: dict[str, Any], where: str) -> dict[str, Any]:
    flat = _flatten(raw)

    missing = [path for path in SCHEMA if path not in flat]
    if missing:
        raise ConfigError(
            f"{where}：缺少必填配置项。\n"
            + "\n".join(
                f"  - `{path}`（{SCHEMA[path].desc}）→ 去 governance.yaml 的 `{_parent_of(path)}` 段补上"
                for path in sorted(missing)
            )
        )

    unknown = [path for path in flat if path not in SCHEMA]
    if unknown:
        raise ConfigError(
            f"{where}：出现未定义的配置项，一律拒绝（防止拼写错误被静默忽略）。\n"
            + "\n".join(
                f"  - `{path}` = {flat[path]!r} → 去掉它，或先登记到 src/policy_kernel/config.py 的 SCHEMA"
                for path in sorted(unknown)
            )
        )

    for path, spec in SCHEMA.items():
        _validate_leaf(path, spec, flat[path], where)

    # 跨字段一致性
    seg = raw["segmentation"]
    if seg["amount"]["mid_min_cents"] >= seg["amount"]["high_min_cents"]:
        raise ConfigError(
            f"{where}：金额档边界不自洽。\n"
            f"  `segmentation.amount.mid_min_cents` 必须 < `segmentation.amount.high_min_cents`"
            f"（当前 {seg['amount']['mid_min_cents']} >= {seg['amount']['high_min_cents']}）。\n"
            f"  语义：mid 是 [mid_min, high_min)，high 是 >= high_min。"
        )
    if seg["recency"]["recent_max_days"] >= seg["recency"]["mid_max_days"]:
        raise ConfigError(
            f"{where}：近度档边界不自洽。\n"
            f"  `segmentation.recency.recent_max_days` 必须 < `segmentation.recency.mid_max_days`"
            f"（当前 {seg['recency']['recent_max_days']} >= {seg['recency']['mid_max_days']}）。"
        )
    approval = raw["approval"]
    if approval["impact_tiers"]["single_sign_max"] >= approval["impact_tiers"]["dual_sign_max"]:
        raise ConfigError(
            f"{where}：审批影响面分档不自洽。\n"
            f"  `single_sign_max` 必须 < `dual_sign_max`"
            f"（当前 {approval['impact_tiers']['single_sign_max']} >= {approval['impact_tiers']['dual_sign_max']}）。\n"
            f"  语义：<= single_sign_max 单签；<= dual_sign_max 双签；再大则双签 + 合规知会。"
        )
    if approval["escalate_remind_hours_before"] > approval["timeout_hours"]:
        raise ConfigError(
            f"{where}：升级提醒时间不合理。\n"
            f"  `approval.escalate_remind_hours_before` 必须 <= `approval.timeout_hours`"
            f"（当前 {approval['escalate_remind_hours_before']} > {approval['timeout_hours']}）。"
        )
    return flat


def _build(flat: dict[str, Any]) -> GovernanceConfig:
    seg = flat
    return GovernanceConfig(
        version=flat["version"],
        updated_at=flat["updated_at"],
        thresholds=Thresholds(
            complaint_window_days=flat["thresholds.complaint_window_days"],
            refund_rate_threshold=float(flat["thresholds.refund_rate_threshold"]),
            refund_dispute_window_days=flat["thresholds.refund_dispute_window_days"],
            high_value_percentile=float(flat["thresholds.high_value_percentile"]),
            freq_window_days=flat["thresholds.freq_window_days"],
            freq_max_touches=flat["thresholds.freq_max_touches"],
            channel_cooldown_hours=flat["thresholds.channel_cooldown_hours"],
            conflict_window_hours=flat["thresholds.conflict_window_hours"],
        ),
        segmentation=Segmentation(
            layer_score_base=seg["segmentation.layer_score_base"],
            amount_high_min_cents=seg["segmentation.amount.high_min_cents"],
            amount_mid_min_cents=seg["segmentation.amount.mid_min_cents"],
            recency_recent_max_days=seg["segmentation.recency.recent_max_days"],
            recency_mid_max_days=seg["segmentation.recency.mid_max_days"],
        ),
        conflict_resolution=ConflictResolution(
            tie_break_order=tuple(flat["conflict_resolution.tie_break_order"]),
            platform_priority_overrides=dict(
                flat["conflict_resolution.platform_priority_overrides"]
            ),
        ),
        benefit=Benefit(
            formula=flat["benefit.formula"],
            floor=flat["benefit.floor"],
        ),
        approval=Approval(
            single_sign_max=flat["approval.impact_tiers.single_sign_max"],
            dual_sign_max=flat["approval.impact_tiers.dual_sign_max"],
            timeout_hours=flat["approval.timeout_hours"],
            escalate_remind_hours_before=flat["approval.escalate_remind_hours_before"],
        ),
        budget=Budget(
            per_task_token_limit=flat["budget.per_task_token_limit"],
            per_task_amount_cents=flat["budget.per_task_amount_cents"],
            per_worker_daily_amount_cents=flat["budget.per_worker_daily_amount_cents"],
            currency=flat["budget.currency"],
        ),
        distillation=Distillation(min_success_count=flat["distillation.min_success_count"]),
        autonomy=Autonomy(mvp_allowed_levels=tuple(flat["autonomy.mvp_allowed_levels"])),
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def load_and_validate(path: str | Path = "governance.yaml") -> GovernanceConfig:
    """加载并校验 governance.yaml。失败抛 ConfigError，消息必须可操作。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(
            f"找不到配置文件：{file_path}。\n"
            f"  请确认仓库根目录下有 governance.yaml（策略参数唯一来源）。\n"
            f"  也可以显式传路径：load_and_validate('path/to/governance.yaml')"
        )
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"governance.yaml 不是合法 YAML，解析失败：{exc}\n"
            f"  请检查缩进与冒号（YAML 用两个空格缩进，值里含冒号要用引号包起来）。"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"governance.yaml 顶层必须是映射（key: value），实际是 {type(raw).__name__}。\n"
            f"  请参考 src/policy_kernel/config.py 的 SCHEMA 结构重写。"
        )
    flat = _validate_shape(raw, str(file_path))
    return _build(flat)


def _flatten_overrides(overrides: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """摊平覆盖项。空 dict **作为叶子保留**（与 `_flatten` 一致）。

    否则 `{"platform_priority_overrides": {}}` 这类「清空某项」的覆盖会被
    摊平成零个叶子，覆盖项凭空消失。

    `WHOLE_VALUE_PATHS` 里的路径**不递归**（与 `_flatten` 同口径），
    否则非空 `platform_priority_overrides` 的每个平台键都会各自变成叶子，
    落在 `OVERRIDABLE_PATHS` 白名单外 → 被误拒。
    """
    out: dict[str, Any] = {}
    for key, value in overrides.items():
        path = f"{prefix}.{key}" if prefix else key
        if path in WHOLE_VALUE_PATHS:
            out[path] = value
        elif isinstance(value, dict) and value:
            out.update(_flatten_overrides(value, path))
        else:
            out[path] = value
    return out


def apply_overrides(
    base: dict[str, Any],
    overrides: dict[str, Any],
    *,
    source: str = "员工级策略包",
) -> dict[str, Any]:
    """把员工级策略包的覆盖项合并进基础配置。

    FR-01b：**抑制红线参数不可被业务层覆盖**。命中红线直接拒绝并给可操作错误。
    未登记在 OVERRIDABLE_PATHS 的路径同样拒绝——可覆盖范围是白名单，不是黑名单。
    返回新的 dict，不修改入参。
    """
    if not isinstance(overrides, dict):
        raise ConfigError(f"{source} 必须是映射（key: value），实际是 {type(overrides).__name__}。")

    leaves = _flatten_overrides(overrides)
    blocked: list[str] = []
    for path in leaves:
        if path in REDLINE_PATHS:
            rule_id = REDLINE_PATHS[path]
            blocked.append(f"  - `{path}` → 触碰红线规则 {rule_id}（DENY 不可绕过，含管理员）")
    if blocked:
        redline_list = "、".join(REDLINE_RULE_IDS)
        raise ConfigError(
            f"{source} 试图覆盖抑制红线参数，已拒绝。\n" + "\n".join(blocked) + "\n\n"
            f"  红线规则共 {len(REDLINE_RULE_IDS)} 条：{redline_list}\n"
            f"  其中 SUP_EXPLICIT_REFUSAL 与 NO_CONSENT **没有任何可配置参数**"
            f"（硬编码于代码层），连被覆盖的入口都不存在。\n"
            f"  需要调整红线阈值，走「配置变更审批 + 回归门禁」改 governance.yaml 根配置，"
            f"不是在员工级策略包里覆盖。"
        )

    not_allowlisted = sorted(path for path in leaves if path not in OVERRIDABLE_PATHS)
    if not_allowlisted:
        raise ConfigError(
            f"{source} 含不可覆盖的配置项（可覆盖范围是白名单）。\n"
            + "\n".join(f"  - `{path}`" for path in not_allowlisted)
            + "\n\n"
            f"  允许覆盖的路径共 {len(OVERRIDABLE_PATHS)} 项：\n"
            + "\n".join(f"  - `{path}`" for path in sorted(OVERRIDABLE_PATHS))
        )

    merged = copy.deepcopy(base)
    for path, value in leaves.items():
        parts = path.split(".")
        node: dict[str, Any] = merged
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                raise ConfigError(
                    f"{source} 的覆盖项 `{path}` 在基础配置里找不到父级 `{part}`。\n"
                    f"  基础配置缺少这一段，无法落位。\n"
                    f"  请确认基础配置来自当前版本的 governance.yaml，"
                    f"或改用允许覆盖的路径（见下方白名单）。"
                )
            node = node[part]
        node[parts[-1]] = value
    return merged
