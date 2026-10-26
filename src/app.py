"""FastAPI 应用入口（T-B4 · G-05 fail-fast）。

启动时校验 `governance.yaml`：**失败即 raise，进程退出**。
坏配置 = 治理链失效，绝不带着失效配置跑批量判定（红线②）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from src.policy_kernel.config import GovernanceConfig, load_and_validate

#: 配置文件路径。生产用环境变量 `GOVERNANCE_CONFIG_PATH` 覆盖（导入时读一次）；
#: 测试用 `monkeypatch.setattr(src.app, "CONFIG_PATH", ...)` 覆盖。
CONFIG_PATH_ENV = "GOVERNANCE_CONFIG_PATH"
CONFIG_PATH: Path = Path(os.environ.get(CONFIG_PATH_ENV, "governance.yaml"))


def get_governance_config(app: FastAPI) -> GovernanceConfig:
    """从 `app.state` 取启动时已校验的配置；缺失即启动不完整，必须报错。"""
    config = getattr(app.state, "governance_config", None)
    if not isinstance(config, GovernanceConfig):
        raise RuntimeError(
            "app.state.governance_config 不存在：应用未经过 lifespan 启动，"
            "或启动时配置校验失败。请确认 governance.yaml 合法后再启动。"
        )
    return config


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_and_validate(CONFIG_PATH)
    app.state.governance_config = config
    yield


app = FastAPI(lifespan=lifespan)
