"""T-E4 路径完整性断言（PRD E14 / 架构 v5.2:206 ⓪ / 附录D ``PATH_ASSERT`` / 红线④）。

权威依据：

- **PRD v5.1:514（E14）**：路径完整性断言失败（trace 缺失必经节点）一律 DENY + 告警，
  该动作进人工审查。
- **架构 v5.2:272**：``missing = REQUIRED_NODES - set(ctx.visited_nodes)``；非空 → DENY。
- **附录D 冻结版:202**：``PATH_ASSERT`` → conclusion=DENY（审计反查键，禁止自造 ID）。
- **红线④**：必经节点缺失即 DENY；断言必经、不可 opt-in——必经节点集是**模块私有**
  的 frozenset（对外只有只读视图函数 ``required_nodes()``），且被断言器**闭包捕获**：
  运行期任何属性赋值都改不动它，无任何参数/环境变量能缩小它。
- **红线① 同定位**（架构 v5.2 §12.2）：纯函数、零 IO、零框架——同输入必同输出，
  无 DB / 网络 / 时钟 / 随机访问。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from src.graph.nodes import PIPELINE_NODES

__all__ = [
    "ALERT_CODE_E14",
    "RULE_ID_PATH_ASSERT",
    "PathVerdict",
    "assert_path",
    "required_nodes",
]

#: E14 告警码（PRD v5.1:514）——路径断言失败随此码告警，动作进人工审查。
ALERT_CODE_E14: Final = "E14"

#: 审计反查键（附录D 冻结版:202，禁止自造 ID）。
RULE_ID_PATH_ASSERT: Final = "PATH_ASSERT"

#: 必经节点全集 = 五节点管线（复用常量，不重定义字符串）。
#:
#: **模块私有**（红线④：必经节点不可 opt-in）：对外只暴露只读视图函数
#: ``required_nodes()``——不暴露可写集合名。断言器在构造时把该 frozenset
#: **捕获进闭包**，外部属性赋值（``path_assert.REQUIRED_NODES = ...``、
#: ``path_assert._REQUIRED_NODES = ...``）拿不到引用、改不动断言所用的集合；
#: 集合本身是 frozenset，无任何缩小入口（无参数、无环境变量）。
_REQUIRED_NODES: Final[frozenset[str]] = frozenset(PIPELINE_NODES)


@dataclass(frozen=True)
class PathVerdict:
    """路径断言结论（纯值对象，零 IO）。

    - ``passed``：``visited ⊇ 必经节点全集``？
    - ``missing``：缺失的必经节点集合（通过时为空 frozenset）；
    - ``conclusion``：``ALLOW`` | ``DENY``（缺失即 DENY，不可绕过——红线②/④）；
    - ``alert_code``：规则绑定告警码（仅 DENY 时随告警发出）；
    - ``rule_id``：审计反查键 ``PATH_ASSERT``。
    """

    passed: bool
    missing: frozenset[str]
    conclusion: str
    alert_code: str = ALERT_CODE_E14
    rule_id: str = RULE_ID_PATH_ASSERT


def _build(
    required: frozenset[str],
) -> tuple[Callable[[], frozenset[str]], Callable[[Sequence[str]], PathVerdict]]:
    """构造「只读视图 + 断言器」——两者共享同一份**闭包捕获**的必经节点集。

    闭包捕获 = 运行期不可改写（红线④）：模块级名字被外部重新赋值时，
    闭包里的引用不变，断言行为不变。
    """

    def required_nodes() -> frozenset[str]:
        """返回必经节点全集的只读视图（frozenset，调用方无法就地修改）。"""
        return required

    def assert_path(visited: Sequence[str]) -> PathVerdict:
        """断言 ``visited ⊇ 必经节点全集``（架构 v5.2:272）。

        纯函数零 IO：缺任一必经节点 → ``conclusion="DENY"`` + ``alert_code="E14"`` +
        ``missing`` 精确列出缺失节点（非裸 Exception）。
        """
        missing = frozenset(required - set(visited))
        if missing:
            return PathVerdict(passed=False, missing=missing, conclusion="DENY")
        return PathVerdict(passed=True, missing=frozenset(), conclusion="ALLOW")

    return required_nodes, assert_path


#: 只读视图 + 路径断言器（闭包捕获 `_REQUIRED_NODES`，运行期不可改写）。
required_nodes, assert_path = _build(_REQUIRED_NODES)
