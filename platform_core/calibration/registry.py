from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Any


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    strategy_code: str
    calibration_version: str
    parameters: dict[str, Any]
    metrics: dict[str, Decimal]
    split: WalkForwardSplit | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GridSearchCalibrationJob:
    def __init__(
        self,
        *,
        strategy_code: str,
        calibration_version: str,
        param_grid: Mapping[str, Sequence[Any]],
        objective: Callable[[dict[str, Any]], Mapping[str, Any]],
        metric_name: str,
        maximize: bool = True,
        split: WalkForwardSplit | None = None,
    ) -> None:
        self.strategy_code = strategy_code
        self.calibration_version = calibration_version
        self.param_grid = dict(param_grid)
        self.objective = objective
        self.metric_name = metric_name
        self.maximize = maximize
        self.split = split

    def run(self) -> CalibrationResult:
        names = list(self.param_grid)
        best_params: dict[str, Any] | None = None
        best_metrics: dict[str, Decimal] | None = None
        best_score: Decimal | None = None
        for values in product(*(self.param_grid[name] for name in names)):
            params = dict(zip(names, values))
            raw_metrics = self.objective(params)
            score = Decimal(str(raw_metrics[self.metric_name]))
            if best_score is None or (score > best_score if self.maximize else score < best_score):
                best_score = score
                best_params = params
                best_metrics = {
                    name: Decimal(str(value))
                    for name, value in raw_metrics.items()
                    if isinstance(value, (int, float, str, Decimal))
                }
        if best_params is None or best_metrics is None:
            raise RuntimeError("calibration produced no candidates")
        return CalibrationResult(
            strategy_code=self.strategy_code,
            calibration_version=self.calibration_version,
            parameters=best_params,
            metrics=best_metrics,
            split=self.split,
            metadata={"method": "grid_search", "objective": self.metric_name},
        )
