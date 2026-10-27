"""T-E1 ``--dry-run`` CLI 与全链路走通验收用例。

权威依据：

- **MVP 任务拆解 T-E1 验收**：``python -m src.graph.workflow --dry-run`` 走通全图。
- **ADR-035 / 裁决 #7**：dry-run 下 confirm_plan 以 auto-confirm 打标记；
  正式 interrupt 挂点由 T-E2 接管。
- **T-E1 prd**：CLI 无 DB 无网络；send 为内存 mock（不触网）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.graph import workflow

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 节点访问序列的期望顺序（硬编码，不从实现 import——防止与实现同流合污）。
_EXPECTED_SEQUENCE: tuple[str, ...] = ("segment", "decision", "confirm_plan", "gate", "send")


def _assert_node_order(output: str) -> None:
    """断言输出里五个节点名按期望顺序**首次出现**（串行主链的外部可观察契约）。"""
    positions = [output.index(name) for name in _EXPECTED_SEQUENCE]
    assert positions == sorted(positions), (
        f"节点顺序不符：期望 {' -> '.join(_EXPECTED_SEQUENCE)}，输出为：\n{output}"
    )


def test_cli_dry_run_exits_zero_and_prints_full_node_sequence() -> None:
    """验收命令：``python -m src.graph.workflow --dry-run`` 退出 0 且打印五节点序列。"""
    result = subprocess.run(
        [sys.executable, "-m", "src.graph.workflow", "--dry-run"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        # 编码口径必须**两端一致**，否则中文断言在非 UTF-8 平台上必失败：
        # 子进程 stdout 默认走本地代码页（Windows = GBK），父进程若按 UTF-8 解码，
        # 中文会被解成替换符。故显式给子进程注入 ``PYTHONIOENCODING=utf-8``，
        # 并让父进程同样按 UTF-8 解码 —— 不依赖执行环境的本地代码页。
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"退出码非 0：\nstdout={result.stdout}\nstderr={result.stderr}"
    _assert_node_order(result.stdout)
    assert "auto-confirm" in result.stdout, "dry-run 下 confirm_plan 应打 auto-confirm 标记"
    assert "dry-run 完成" in result.stdout


def test_main_dry_run_returns_zero_and_prints_sequence(capsys: pytest.CaptureFixture[str]) -> None:
    """``main(["--dry-run"])`` 返回 0，stdout 含按序五节点（CLI 可单测，不依赖子进程）。"""
    exit_code = workflow.main(["--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    _assert_node_order(captured.out)


def test_main_without_dry_run_flag_returns_two() -> None:
    """骨架期仅支持 --dry-run：缺 flag 返回 2（正式执行链由 T-E2 接管）。"""
    assert workflow.main([]) == 2


def test_run_dry_run_visits_all_five_nodes_in_order() -> None:
    """``run_dry_run`` 终态 visited_nodes 按序含全部五节点（架构 v5.2:258 轨迹）。"""
    state = workflow.run_dry_run()
    assert state.get("visited_nodes") == list(_EXPECTED_SEQUENCE)


def test_dry_run_auto_confirms_plan_and_flags_it() -> None:
    """dry-run 下 confirm_plan auto-confirm：plan_confirmed=True 且打标记（ADR-035 挂点）。"""
    state = workflow.run_dry_run()
    assert state.get("plan_confirmed") is True
    marker = state.get("confirmation_marker", "")
    assert "auto-confirm" in marker
    assert state.get("visited_nodes", []).index("confirm_plan") < state.get(
        "visited_nodes", []
    ).index("gate")


def test_dry_run_send_is_in_memory_mock() -> None:
    """send = 内存 mock：发出 dry-run 放行候选，状态标记 mock-sent，零网络零 DB。"""
    state = workflow.run_dry_run()
    sent = state.get("sent", [])
    assert sent, "dry-run 样例批次应有放行候选被 mock 发送"
    assert all(item["status"] == "mock-sent" for item in sent)


def test_dry_run_module_has_no_network_or_db_imports() -> None:
    """红线⑤ + T-E1 prd：graph 模块无网络 / DB 依赖（httpx、psycopg、litellm 等）。"""
    source = Path(workflow.__file__).read_text(encoding="utf-8")
    for banned in ("httpx", "psycopg", "litellm", "requests", "socket"):
        assert f"import {banned}" not in source, f"graph 内出现网络/DB 依赖 import：{banned}"
