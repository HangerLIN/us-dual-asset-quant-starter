from pathlib import Path

import pytest

from scripts.scaffold_strategy import scaffold


def test_scaffold_creates_external_strategy_package(tmp_path: Path) -> None:
    target = scaffold("Earnings Reversal", tmp_path)
    assert (target / "pyproject.toml").is_file()
    strategy = target / "src" / "earnings_reversal" / "strategy.py"
    assert strategy.is_file()
    assert "return []" in strategy.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        scaffold("Earnings Reversal", tmp_path)
