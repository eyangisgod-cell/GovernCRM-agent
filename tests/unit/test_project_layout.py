"""仓库骨架自检（T0.1）。

这些断言不是业务逻辑，而是**骨架纪律的机器可读版本**：目录骨架在位、三档依赖组齐备、
`.env.example` 不含真实口令。任何一个骨架文件被挪走或误删，本用例立刻变红。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DIRS = (
    "src/providers",
    "src/nodes",
    "src/skills",
    "src/workflows",
    "src/policy",
    "src/decision",
    "src/audit",
    "src/models",
    "src/api",
    "src/db",
    "tests/unit",
    "tests/integration",
    "migrations",
    "scripts",
)

REQUIRED_FILES = (
    "pyproject.toml",
    "ruff.toml",
    "Makefile",
    "docker-compose.yml",
    ".env.example",
    "LICENSES.md",
)

SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)", re.IGNORECASE)
#: `.env.example` 中允许出现的占位符形态
PLACEHOLDER_PATTERN = re.compile(r"^(CHANGE_ME.*|.*_LOCAL_ONLY|)$")


@pytest.mark.parametrize("relative", REQUIRED_DIRS)
def test_required_directory_exists(relative: str) -> None:
    assert (ROOT / relative).is_dir(), f"缺少目录骨架：{relative}"


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_required_file_exists(relative: str) -> None:
    assert (ROOT / relative).is_file(), f"缺少骨架文件：{relative}"


def test_dependency_groups_are_three_tiers() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    groups = set(data.get("dependency-groups", {}))
    assert {"runtime", "dev", "evaluation"} <= groups, f"依赖组不齐备：{groups}"


def test_env_example_has_no_real_secret() -> None:
    """`.env.example` 只允许占位符——真实口令由种子脚本写进本地 `.env`（不入库）。"""
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not SECRET_KEY_PATTERN.search(key):
            continue
        assert PLACEHOLDER_PATTERN.match(value), f".env.example 的 {key} 含疑似真实口令：{value!r}"
    assert "CHANGE_ME" in _env_value("DATABASE_URL"), "DATABASE_URL 里应保留占位符口令"


def _env_value(key: str) -> str:
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(f"{key}="):
            return line.partition("=")[2]
    return ""


def test_licenses_cover_every_direct_dependency() -> None:
    """`LICENSES.md` 必须覆盖 `pyproject.toml` 的全部直接依赖（依赖准入纪律）。"""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)

    requirements: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(group)

    licenses_text = (ROOT / "LICENSES.md").read_text(encoding="utf-8").lower()
    for requirement in requirements:
        dist = requirement.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].strip().lower()
        assert dist, f"无法解析依赖名：{requirement!r}"
        assert dist in licenses_text, f"LICENSES.md 未覆盖直接依赖：{dist}"


def test_third_party_inference_switch_defaults_to_disabled() -> None:
    """出境开关默认关闭（PRD C9 / 私有化部署默认关闭口径）。"""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "THIRD_PARTY_INFERENCE_ENABLED=false" in content


def test_makefile_recipes_stay_ascii() -> None:
    """Makefile 配方行必须是纯 ASCII。

    Windows/MSYS 下配方行里的非 ASCII 字符会被按 ANSI 代码页转换，
    可能产生转义反斜杠，让 `sh` 报 `unexpected EOF` —— 这是**已经踩过的坑**，
    用例常驻防回归（中文注释与 `## 帮助` 放在规则行，不受影响）。
    """
    for lineno, raw in enumerate((ROOT / "Makefile").read_text(encoding="utf-8").splitlines(), 1):
        if not raw.startswith("\t"):
            continue
        assert raw.isascii(), f"Makefile:{lineno} 配方行含非 ASCII 字符：{raw!r}"
