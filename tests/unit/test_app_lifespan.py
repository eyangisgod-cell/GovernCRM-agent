"""T-B4：FastAPI 应用生命周期 fail-fast 接入。

规格来源（测试即规格）：
  - `.trellis/tasks/10-26-t-b4-fastapi-lifespan/prd.md` §3 验收标准
  - `docs/specs/MVP_任务拆解.md:162-169`（T-B4，门禁 G-05 fail-fast）
  - 红线② DENY 不可绕过：坏配置 = 治理链失效，必须**在启动时阻断**，
    绝不带着失效配置跑批量判定。

负向用例**常驻不许 skip**：可跳过的门等于没有门。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_CONFIG = REPO_ROOT / "governance.yaml"

# 被删掉 / 被改坏的红线字段（属 REDLINE_PATHS，见 src/policy_kernel/config.py:71）
REDLINE_FIELD_PATH = "thresholds.refund_rate_threshold"


def _load_template() -> dict[str, Any]:
    if not VALID_CONFIG.exists():
        pytest.fail(
            f"缺少合法配置模板 {VALID_CONFIG}；该文件是本套用例的基准，"
            f"缺失说明仓库状态被破坏，请先恢复 governance.yaml 再跑测试。"
        )
    raw = yaml.safe_load(VALID_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        pytest.fail(f"governance.yaml 顶层必须是映射，实际是 {type(raw).__name__}")
    return raw


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _use_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    from src import app as app_module

    monkeypatch.setattr(app_module, "CONFIG_PATH", path)


def _startup(app_module: ModuleType) -> None:
    """走真实的应用启动路径（lifespan）；失败时把异常抛给调用方。"""
    import asyncio

    async def _run() -> None:
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(_run())


@pytest.fixture
def app_module() -> Iterator[ModuleType]:
    """导入应用模块；每个用例前后清掉可能残留的 app.state 配置。"""
    import src.app as module

    state = getattr(module.app, "state", None)
    if state is not None and hasattr(state, "governance_config"):
        delattr(state, "governance_config")
    yield module
    state = getattr(module.app, "state", None)
    if state is not None and hasattr(state, "governance_config"):
        delattr(state, "governance_config")


def test_app_exposes_asgi_app_for_uvicorn(app_module: ModuleType) -> None:
    """`uvicorn src.app:app` 必须能加载到一个 FastAPI 实例。

    来源：prd.md §3 首条验收。
    """
    from fastapi import FastAPI

    assert isinstance(app_module.app, FastAPI), (
        "src.app 必须导出名为 `app` 的 FastAPI 实例，否则 uvicorn src.app:app 无法启动"
    )


def test_app_starts_with_valid_governance_config(
    app_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """正常配置 → 启动成功，且配置挂到 app.state 供依赖注入取用。

    来源：prd.md §2.1 / §3 第二条。
    """
    good = tmp_path / "governance.yaml"
    _write_config(good, _load_template())
    _use_config(monkeypatch, good)

    _startup(app_module)

    loaded = getattr(app_module.app.state, "governance_config", None)
    assert loaded is not None, "启动成功后 app.state 上必须能取到已加载的配置"
    assert type(loaded).__name__ == "GovernanceConfig"


def test_app_fails_fast_when_redline_field_missing(
    app_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """坏配置（缺红线字段）→ 启动**抛错并退出**，错误信息指明缺哪个配置项。

    来源：prd.md §3 第三条 / §2.2；红线②。
    """
    from src.policy_kernel.config import ConfigError

    data = _load_template()
    del data["thresholds"]["refund_rate_threshold"]  # SUP_HIGH_REFUND 红线参数
    bad = tmp_path / "governance.yaml"
    _write_config(bad, data)
    _use_config(monkeypatch, bad)

    with pytest.raises(ConfigError) as excinfo:
        _startup(app_module)

    message = str(excinfo.value)
    assert REDLINE_FIELD_PATH in message, (
        f"错误信息必须指明缺失的配置项路径 `{REDLINE_FIELD_PATH}`，实际是：{message}"
    )


def test_app_fails_fast_when_threshold_out_of_range(
    app_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """坏配置（阈值越界）→ 启动**抛错并退出**，信息可操作。

    来源：prd.md §3 第四条；SCHEMA 规定 refund_rate_threshold ∈ [0, 1]。
    """
    from src.policy_kernel.config import ConfigError

    data = _load_template()
    data["thresholds"]["refund_rate_threshold"] = 5.0  # 越界（maximum=1）
    bad = tmp_path / "governance.yaml"
    _write_config(bad, data)
    _use_config(monkeypatch, bad)

    with pytest.raises(ConfigError) as excinfo:
        _startup(app_module)

    message = str(excinfo.value)
    assert REDLINE_FIELD_PATH in message, (
        f"错误信息必须指明越界的配置项 `{REDLINE_FIELD_PATH}`，实际是：{message}"
    )
    assert "1" in message or "0" in message, f"错误信息必须给出可操作的取值范围，实际是：{message}"


def test_app_does_not_silently_continue_with_invalid_config(
    app_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """**显式断言没有静默降级**：异常必须抛出，且不得用默认配置继续跑。

    来源：prd.md §2.2 / §3 第六条；implement.md 步骤 3 第四小点。
    """
    from src.policy_kernel.config import ConfigError

    data = _load_template()
    del data["thresholds"]["refund_rate_threshold"]
    bad = tmp_path / "governance.yaml"
    _write_config(bad, data)
    _use_config(monkeypatch, bad)

    # 断言①：异常被抛出（不是仅断言日志）
    with pytest.raises(ConfigError):
        _startup(app_module)

    # 断言②：失败路径上没有留下任何「已加载配置」的状态
    leftover = getattr(app_module.app.state, "governance_config", None)
    assert leftover is None, (
        "启动失败后 app.state 不得残留 governance_config —— "
        "残留意味着有代码在失败后继续用配置跑（静默降级）"
    )
