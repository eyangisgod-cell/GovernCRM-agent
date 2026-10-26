"""统一层（身份匹配 + 分层）。

⚠️ **本包 ``__init__`` 不得 import 任何落库模块**（如 ``segment_store``）。
理由：策略内核（T-B2 的 ``rules.py``）会 import ``src.unification.segmentation``，
若这里连带引入 ORM，就会把 SQLAlchemy 带进纯函数内核，违反红线 1
（``.trellis/spec/backend/quality-guidelines.md``）。

- ``segmentation`` —— 九宫格分层（**分层算法唯一实现点**，纯函数）。
- ``segment_store`` —— 分层结果落库（引 ORM，只在落库链路 import）。
"""

from __future__ import annotations
