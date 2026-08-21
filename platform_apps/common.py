from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest

from fastapi import Header, HTTPException, status

from platform_core.core import get_settings

ALL_SERVICE_ROLES = frozenset(
    {
        "read",
        "order_submitter",
        "execution_operator",
        "risk_authorizer",
        "risk_proposer",
        "risk_approver",
        "risk_operator",
        "reconciler",
    }
)


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    actor: str
    roles: frozenset[str]
    strategy_codes: frozenset[str] | None = None


def require_service_api_key(x_api_key: str | None = Header(default=None)) -> str:
    authenticate_service_identity(x_api_key)
    assert x_api_key is not None
    return x_api_key


def require_service_identity(
    x_api_key: str | None = Header(default=None),
) -> ServiceIdentity:
    return authenticate_service_identity(x_api_key)


def require_service_role(*roles: str) -> Callable[..., ServiceIdentity]:
    required = frozenset(roles)
    if not required or not required.issubset(ALL_SERVICE_ROLES):
        raise ValueError("service role dependency contains an unknown role")

    def dependency(x_api_key: str | None = Header(default=None)) -> ServiceIdentity:
        identity = authenticate_service_identity(x_api_key)
        if not required.issubset(identity.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"service identity requires roles: {', '.join(sorted(required))}",
            )
        return identity

    return dependency


def verified_actor(identity: ServiceIdentity, requested: str | None) -> str:
    if requested is not None and requested.strip() != identity.actor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="request actor must match the authenticated service identity",
        )
    return identity.actor


def require_strategy_access(
    identity: ServiceIdentity,
    strategy_code: str,
    *,
    header_strategy_code: str | None = None,
) -> str:
    """把订单请求绑定到已认证的策略凭据。"""

    # strategy_code 同时出现在凭据授权、请求头和订单体中，三者不一致时必须拒绝。
    normalized = strategy_code.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="strategy_code is required",
        )
    if header_strategy_code is not None and header_strategy_code.strip() != normalized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-Strategy-Code must match the order strategy_code",
        )
    if identity.strategy_codes is not None and normalized not in identity.strategy_codes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"service identity is not authorized for strategy {normalized!r}",
        )
    return normalized


def authenticate_service_identity(x_api_key: str | None) -> ServiceIdentity:
    settings = get_settings()
    raw_identities = settings.service_api_identities.strip()
    if raw_identities:
        try:
            payload = json.loads(raw_identities)
            identities = _identity_records(
                payload,
                require_strategy_bindings=settings.trading_mode.upper() == "LIVE",
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="SERVICE_API_IDENTITIES is invalid; sensitive APIs fail closed",
            ) from None
        if x_api_key is None:
            raise _invalid_key()
        for actor, key, roles, strategy_codes in identities:
            if compare_digest(x_api_key, key):
                return ServiceIdentity(
                    actor=actor,
                    roles=roles,
                    strategy_codes=strategy_codes,
                )
        raise _invalid_key()

    if settings.trading_mode.upper() == "LIVE":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LIVE mode requires role-bound SERVICE_API_IDENTITIES",
        )
    configured = [key.strip() for key in settings.service_api_keys.split(",") if key.strip()]
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service API credentials are not configured; sensitive APIs fail closed",
        )
    if x_api_key is None:
        raise _invalid_key()
    for index, key in enumerate(configured, start=1):
        if compare_digest(x_api_key, key):
            return ServiceIdentity(
                actor=f"legacy-api-key-{index}",
                roles=ALL_SERVICE_ROLES,
            )
    raise _invalid_key()


def _identity_records(
    payload: object,
    *,
    require_strategy_bindings: bool = False,
) -> list[tuple[str, str, frozenset[str], frozenset[str] | None]]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("identity mapping must be a non-empty object")
    records = []
    seen_keys: set[str] = set()
    for raw_actor, raw_config in payload.items():
        actor = str(raw_actor).strip()
        if not actor or not isinstance(raw_config, dict):
            raise ValueError("invalid service identity")
        key = str(raw_config.get("key", "")).strip()
        raw_roles = raw_config.get("roles")
        if len(key) < 16 or not isinstance(raw_roles, list):
            raise ValueError("service identity key and roles are required")
        if key in seen_keys:
            raise ValueError("service identity keys must be unique")
        roles = frozenset(str(role).strip() for role in raw_roles if str(role).strip())
        if not roles or not roles.issubset(ALL_SERVICE_ROLES):
            raise ValueError("service identity has unknown or empty roles")
        raw_strategies = raw_config.get("strategies")
        if raw_strategies is None:
            strategy_codes = None
        elif isinstance(raw_strategies, list):
            strategy_codes = frozenset(
                str(strategy).strip() for strategy in raw_strategies if str(strategy).strip()
            )
            if not strategy_codes or any(len(code) > 64 for code in strategy_codes):
                raise ValueError("service identity strategies must be non-empty valid codes")
        else:
            raise ValueError("service identity strategies must be a list")
        if require_strategy_bindings and "order_submitter" in roles and strategy_codes is None:
            # 实盘禁止“可替任意策略下单”的共享送单身份。
            raise ValueError("live order_submitter identities require explicit strategies")
        seen_keys.add(key)
        records.append((actor, key, roles, strategy_codes))
    return records


def _invalid_key() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid API key",
    )
