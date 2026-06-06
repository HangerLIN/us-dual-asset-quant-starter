from __future__ import annotations

from decimal import Decimal

from platform_core.calibration import GridSearchCalibrationJob


def test_grid_search_calibration_selects_best_params() -> None:
    job = GridSearchCalibrationJob(
        strategy_code="demo",
        calibration_version="test",
        param_grid={"threshold": [Decimal("1"), Decimal("2")]},
        objective=lambda params: {"score": Decimal(str(params["threshold"]))},
        metric_name="score",
    )
    result = job.run()
    assert result.parameters["threshold"] == Decimal("2")
    assert result.metrics["score"] == Decimal("2")
