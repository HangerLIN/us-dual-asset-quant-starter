from __future__ import annotations

from datetime import date
from decimal import Decimal

from _bootstrap import ROOT  # noqa: F401
from platform_core.calibration import GridSearchCalibrationJob, WalkForwardSplit


def main() -> None:
    split = WalkForwardSplit(
        train_start=date(2026, 1, 1),
        train_end=date(2026, 3, 31),
        validation_start=date(2026, 4, 1),
        validation_end=date(2026, 4, 30),
    )
    job = GridSearchCalibrationJob(
        strategy_code="dual-asset-momentum",
        calibration_version="demo-cal-v1",
        param_grid={
            "rvol_min": [Decimal("1.2"), Decimal("1.5"), Decimal("2.0")],
            "option_spread_pct_max": [Decimal("0.05"), Decimal("0.10")],
            "order_ttl_seconds": [60, 90],
        },
        objective=lambda params: {
            "clean_trade_rate": Decimal("0.50") + Decimal(str(params["rvol_min"])) / Decimal("20"),
            "stale_fill_rate": Decimal("0.05") if params["order_ttl_seconds"] <= 90 else Decimal("0.10"),
        },
        metric_name="clean_trade_rate",
        split=split,
    )
    result = job.run()
    print("calibration_version=", result.calibration_version)
    print("parameters=", result.parameters)
    print("metrics=", {key: str(value) for key, value in result.metrics.items()})


if __name__ == "__main__":
    main()
