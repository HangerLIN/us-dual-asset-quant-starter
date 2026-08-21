from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a separate, strategy-plugin package with no trading rules."
    )
    parser.add_argument("name", help="Strategy package name, for example earnings-reversal")
    parser.add_argument("--output-dir", default="strategies")
    args = parser.parse_args()
    target = scaffold(args.name, Path(args.output_dir))
    print(f"created={target}")


def scaffold(name: str, output_dir: Path) -> Path:
    slug = _slug(name)
    module = slug.replace("-", "_")
    class_name = "".join(part.title() for part in module.split("_")) + "Strategy"
    target = output_dir / slug
    if target.exists():
        raise FileExistsError(f"strategy package already exists: {target}")
    package_dir = target / "src" / module
    tests_dir = target / "tests"
    package_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (target / "pyproject.toml").write_text(
        _pyproject(slug, module),
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        f'from .strategy import {class_name}\n\n__all__ = ["{class_name}"]\n',
        encoding="utf-8",
    )
    (package_dir / "strategy.py").write_text(
        _strategy_module(class_name, slug),
        encoding="utf-8",
    )
    (tests_dir / "test_contract.py").write_text(
        _contract_test(module, class_name),
        encoding="utf-8",
    )
    return target


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("strategy name must contain letters or numbers")
    return slug


def _pyproject(slug: str, module: str) -> str:
    return f'''[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "{slug}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["us-dual-asset-quant-starter>=0.1.0"]

[tool.setuptools.packages.find]
where = ["src"]
include = ["{module}*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''


def _strategy_module(class_name: str, strategy_code: str) -> str:
    return f'''from __future__ import annotations

from platform_core.strategy import BaseStrategy, StrategyContext


class {class_name}(BaseStrategy):
    strategy_code = "{strategy_code}"
    strategy_version = "0.1.0"

    def __init__(self, parameters=None):
        self.parameters = parameters or {{}}

    def on_bar(self, event, context: StrategyContext):
        # 在此独立策略包中实现策略决策。
        return []
'''


def _contract_test(module: str, class_name: str) -> str:
    return f'''from {module} import {class_name}
from platform_core.strategy import StrategyPlugin


def test_strategy_contract():
    strategy = {class_name}()
    assert isinstance(strategy, StrategyPlugin)
    assert strategy.strategy_code
    assert strategy.strategy_version
'''


if __name__ == "__main__":
    main()
