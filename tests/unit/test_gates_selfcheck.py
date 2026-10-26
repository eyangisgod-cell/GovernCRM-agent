"""门禁自检：证明「故意违规」时门禁真的会红。

这些用例把违规写进临时探针文件，跑真实的门禁命令，断言它失败，然后删除探针。
它们是常驻回归用例——门禁本身失效（例如规则被误删、脚本被改成永远 exit 0）时，
这里会第一时间红掉，而不是等到有人偷偷引入违规依赖才发现。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def probe_file(relative: str, source: str) -> Iterator[Path]:
    """在指定目录写入临时探针文件，退出时无条件删除。"""
    path = REPO_ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def run_scanner() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_dependencies.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def run_ruff_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(path), "--output-format", "concise"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


# 探针一律「真的用上」所 import 的模块，避免 F401（unused import）混进来，
# 使得断言能精确落在目标规则上，而不是撞巧被任何 lint 规则拦下。
FOREIGN_PROBE = "import requests\n\nVALUE = requests\n"
LITELLM_PROBE = "import litellm\n\nVALUE = litellm\n"


@pytest.mark.unit
def test_gate_rejects_import_outside_dependency_whitelist() -> None:
    """负向验证①：清单外第三方库 import -> 扫描脚本必须报错退出 1。"""
    with probe_file("src/nodes/_gate_probe_foreign.py", FOREIGN_PROBE):
        result = run_scanner()
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "依赖白名单门禁失效：清单外的 `import requests` 未被拦截\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "requests" in combined, f"报错未定位到违规 import：\n{combined}"
    # 探针删除后，扫描必须恢复为通过
    assert run_scanner().returncode == 0, "探针清理后门禁未恢复通过，说明门禁状态不干净"


@pytest.mark.unit
def test_gate_rejects_litellm_outside_providers() -> None:
    """负向验证②：业务层 import litellm -> 扫描脚本与 ruff TID251 双双报错。"""
    with probe_file("src/nodes/_gate_probe_litellm.py", LITELLM_PROBE) as probe:
        scan = run_scanner()
        assert scan.returncode != 0, (
            "供应商依赖门禁失效：业务层 `import litellm` 未被扫描脚本拦截\n"
            f"stdout:\n{scan.stdout}\nstderr:\n{scan.stderr}"
        )
        lint = run_ruff_check(probe)
        assert lint.returncode != 0, (
            "ruff 门禁失效：业务层 `import litellm` 未被拦截\n"
            f"stdout:\n{lint.stdout}\nstderr:\n{lint.stderr}"
        )
        assert "TID251" in (lint.stdout + lint.stderr), (
            "红的是别的规则而非 TID251（业务层禁供应商 SDK），门禁语义不对\n"
            f"stdout:\n{lint.stdout}\nstderr:\n{lint.stderr}"
        )
    assert run_scanner().returncode == 0


@pytest.mark.unit
def test_providers_dir_may_import_litellm() -> None:
    """正向对照：唯一授权目录 src/providers/ 允许 import litellm。"""
    with probe_file("src/providers/_gate_probe_ok.py", LITELLM_PROBE) as probe:
        scan = run_scanner()
        assert scan.returncode == 0, (
            "误伤：授权目录 src/providers/ 下的 `import litellm` 被拦截\n"
            f"stdout:\n{scan.stdout}\nstderr:\n{scan.stderr}"
        )
        lint = run_ruff_check(probe)
        assert lint.returncode == 0, (
            "误伤：ruff 拦截了 src/providers/ 下的 `import litellm`\n"
            f"stdout:\n{lint.stdout}\nstderr:\n{lint.stderr}"
        )
