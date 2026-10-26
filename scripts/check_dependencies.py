#!/usr/bin/env python3
"""依赖白名单扫描（红线⑤：业务层零供应商依赖 + 依赖准入白名单制）。

规则：

1. **白名单制**：`pyproject.toml` 的依赖清单（runtime / dev / evaluation 三组）之外的
   第三方库，一律禁止在仓库代码中 import。
2. **标准库豁免**：标准库模块永远允许。
3. **一方包豁免**：`src.*`（本仓库顶层包）与相对导入（`from ..x import y`）永远允许。
4. **litellm 目录限定**：`litellm` 只允许出现在 `src/providers/` 下的文件
   （N4 拍板 B：litellm 作独立 provider 适配层，业务层不得直接依赖）。

用法::

    python scripts/check_dependencies.py            # 扫描 src/ tests/ scripts/
    python scripts/check_dependencies.py --verbose  # 额外打印白名单明细

退出码：0 = 通过；1 = 存在违规；2 = 配置/读取错误。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

#: 发行名（PyPI）→ 导入名 的映射表。表中未列出的按 ``name.replace('-', '_')`` 推导。
DIST_TO_MODULE: dict[str, str] = {
    "psycopg": "psycopg",
    "pgvector": "pgvector",
    "pyyaml": "yaml",
    "pydantic-settings": "pydantic_settings",
    "langchain-core": "langchain_core",
    "langgraph": "langgraph",
    "pytest-cov": "pytest_cov",
    "pytest-asyncio": "pytest_asyncio",
    "inspect-ai": "inspect_ai",
    "types-pyyaml": "yaml",
}

#: 只允许在指定目录下 import 的第三方库（键为导入名，值为允许的相对目录前缀）。
DIRECTORY_SCOPED: dict[str, str] = {
    "litellm": "src/providers",
}

#: 被扫描的源码目录（相对仓库根）。
SCAN_DIRS: tuple[str, ...] = ("src", "tests", "scripts")

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _requirement_to_dist(requirement: str) -> str | None:
    """从 PEP 508 需求串里取出发行名（去掉 extras / 版本约束 / marker）。"""
    text = requirement.split(";", 1)[0].strip()
    if not text:
        return None
    match = _REQUIREMENT_NAME.match(text)
    if match is None:
        return None
    return match.group(0).lower().replace("_", "-")


def _dist_to_module(dist: str) -> str:
    if dist in DIST_TO_MODULE:
        return DIST_TO_MODULE[dist]
    return dist.replace("-", "_")


def load_whitelist(pyproject: Path) -> set[str]:
    """从 pyproject.toml 的三档依赖组解析出允许 import 的第三方模块名。"""
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)

    requirements: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(group)

    modules: set[str] = set()
    for requirement in requirements:
        dist = _requirement_to_dist(requirement)
        if dist is None:
            continue
        modules.add(_dist_to_module(dist))
    return modules


def first_party_modules(root: Path) -> set[str]:
    """仓库内的一方包顶层名（src 下的每个子目录 + 顶层 src 本身）。"""
    src = root / "src"
    modules = {"src"}
    if src.is_dir():
        modules.update(child.name for child in src.iterdir() if child.is_dir())
    return modules


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        files.extend(sorted(p for p in base.rglob("*.py") if p.is_file()))
    return files


def collect_imports(path: Path) -> list[tuple[int, str, int]]:
    """返回 [(行号, 顶层模块名, 相对层级)]；相对导入层级 > 0。"""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))

    found: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name.split(".")[0], 0))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对导入：仓库内，一律允许
            if node.module:
                found.append((node.lineno, node.module.split(".")[0], 0))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="依赖白名单扫描")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        print(f"[deps] 找不到 {pyproject}", file=sys.stderr)
        return 2

    try:
        whitelist = load_whitelist(pyproject)
    except tomllib.TOMLDecodeError as exc:
        print(f"[deps] pyproject.toml 解析失败：{exc}", file=sys.stderr)
        return 2

    stdlib = set(sys.stdlib_module_names)
    own = first_party_modules(root)

    if args.verbose:
        print("[deps] 白名单第三方模块：", ", ".join(sorted(whitelist)))
        print("[deps] 一方包顶层名：", ", ".join(sorted(own)))

    violations: list[str] = []
    scanned = 0
    for path in iter_python_files(root):
        scanned += 1
        try:
            imports = collect_imports(path)
        except SyntaxError as exc:
            violations.append(f"{path.relative_to(root)}:{exc.lineno}: 文件无法解析：{exc.msg}")
            continue

        rel = path.relative_to(root).as_posix()
        for lineno, module, _level in imports:
            if module in stdlib or module in own:
                continue
            scoped_dir = DIRECTORY_SCOPED.get(module)
            if scoped_dir is not None:
                if not rel.startswith(f"{scoped_dir}/"):
                    violations.append(
                        f"{rel}:{lineno}: {module} 只允许在 {scoped_dir}/ 下 import"
                        "（N4 拍板 B：业务层只认 ModelProvider / JudgmentProvider 协议）"
                    )
                continue
            if module not in whitelist:
                violations.append(
                    f"{rel}:{lineno}: {module} 不在 pyproject.toml 依赖白名单内"
                    "（新增第三方库前必须先完成 LICENSE 核对并记入 LICENSES.md）"
                )

    if violations:
        print(f"[deps] 依赖白名单扫描失败：{len(violations)} 处违规", file=sys.stderr)
        for item in violations:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print(f"[deps] 依赖白名单扫描通过：{scanned} 个文件，未发现白名单外 import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
