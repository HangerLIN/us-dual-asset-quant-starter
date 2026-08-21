from __future__ import annotations

from importlib import import_module
from inspect import Parameter, signature
from typing import Any

from .contracts import StrategyPlugin


def load_strategy(import_path: str, parameters: dict[str, Any] | None = None) -> StrategyPlugin:
    """从外部策略包加载 ``package.module:attribute``。"""
    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("strategy path must use 'package.module:attribute'")
    target = getattr(import_module(module_name), attribute_name)
    if isinstance(target, type) or callable(target):
        parameters_spec = list(signature(target).parameters.values())
        required = [
            item
            for item in parameters_spec
            if item.default is Parameter.empty
            and item.kind in {Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD}
        ]
        accepts_parameters = any(
            item.kind
            in {Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD, Parameter.VAR_POSITIONAL}
            for item in parameters_spec
        )
        if parameters and accepts_parameters:
            strategy = target(parameters)
        else:
            strategy = target() if not required else target(parameters or {})
    else:
        strategy = target
    _validate_strategy(strategy)
    return strategy


def _validate_strategy(strategy: object) -> None:
    required_attributes = ("strategy_code", "strategy_version")
    required_methods = (
        "on_start",
        "on_bar",
        "on_quote",
        "on_order_update",
        "on_fill",
        "on_stop",
    )
    missing = [name for name in required_attributes if not getattr(strategy, name, None)]
    missing.extend(name for name in required_methods if not callable(getattr(strategy, name, None)))
    if missing:
        raise TypeError(f"strategy plugin is missing required members: {', '.join(missing)}")
