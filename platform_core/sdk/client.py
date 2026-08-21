from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from collections.abc import Iterator
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from platform_core.schemas import BrokerOrderRequest

from .models import (
    BracketOrderIntent,
    DefinedRiskOptionComboIntent,
    ExecutionResult,
    LiveOrderIntent,
    OCAOrderIntentGroup,
    OrderCancelCommand,
    OrderReplaceCommand,
    StrategyOrderEvent,
    StrategyOrderEventPage,
)


class StrategyExecutionClientError(RuntimeError):
    """隔离执行服务返回的结构化失败。"""

    def __init__(self, *, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"execution service returned HTTP {status_code}: {detail}")


class _HTTPResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class _HTTPClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> _HTTPResponse: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _UrllibResponse:
    status_code: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


class _UrllibClient:
    def __init__(self) -> None:
        self._opener = build_opener(_RejectRedirects())

    def request(self, method: str, url: str, **kwargs: Any) -> _UrllibResponse:
        payload = kwargs.get("json")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = dict(kwargs.get("headers") or {})
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=kwargs.get("timeout")) as response:
                return _UrllibResponse(
                    status_code=int(response.status),
                    body=response.read(),
                )
        except HTTPError as exc:
            return _UrllibResponse(status_code=int(exc.code), body=exc.read())

    def close(self) -> None:
        return None


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True, slots=True)
class StrategyExecutionClientConfig:
    """由策略进程持有的网络边界配置。

    策略只接收此服务凭据；IBKR 主机、端口、客户端编号、账户凭据和数据库写凭据均归
    ``exec_svc`` 所有。
    """

    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 10.0
    allow_insecure_loopback: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("execution service base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("execution service base_url must not contain credentials")
        if parsed.scheme == "http" and not (
            self.allow_insecure_loopback
            and parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("non-loopback execution service URLs must use HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("execution service base_url must not contain a query or fragment")
        if len(self.api_key) < 16 or self.api_key != self.api_key.strip() or "\n" in self.api_key:
            raise ValueError("strategy execution API key must contain at least 16 characters")
        if self.timeout_seconds <= 0:
            raise ValueError("execution service timeout must be positive")


class StrategyExecutionClient:
    """供策略跨进程提交类型化意图的 SDK。

    该 SDK 有意不暴露 IBKR 适配器或 PAPER/LIVE 开关；目标执行服务拥有交易模式，并把
    此凭据绑定到一个或多个已配置策略代码。
    """

    def __init__(
        self,
        *,
        strategy_code: str,
        config: StrategyExecutionClientConfig,
        http_client: _HTTPClient | None = None,
    ) -> None:
        normalized_strategy = strategy_code.strip()
        if not normalized_strategy or len(normalized_strategy) > 64:
            raise ValueError("strategy_code must contain between 1 and 64 characters")
        self.strategy_code = normalized_strategy
        self.config = config
        # 策略进程只持有执行服务凭据；这里故意不提供 IBKR 地址、账号或 PAPER/LIVE 开关。
        self._owns_http_client = http_client is None
        self._http = http_client or _UrllibClient()

    def intent(
        self,
        request: BrokerOrderRequest,
        *,
        client_order_id: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LiveOrderIntent:
        return LiveOrderIntent(
            client_order_id=client_order_id or f"{self.strategy_code[:31]}-{uuid4().hex}",
            strategy_code=self.strategy_code,
            request=request,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )

    def submit(self, intent: LiveOrderIntent) -> ExecutionResult:
        self._assert_strategy(intent.strategy_code)
        return ExecutionResult.model_validate(
            self._request("POST", "/v1/orders", payload=intent.model_dump(mode="json"))
        )

    def place(
        self,
        request: BrokerOrderRequest,
        *,
        client_order_id: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        return self.submit(
            self.intent(
                request,
                client_order_id=client_order_id,
                expires_at=expires_at,
                metadata=metadata,
            )
        )

    def submit_bracket(self, bracket: BracketOrderIntent) -> list[ExecutionResult]:
        for intent in (bracket.entry, bracket.take_profit, bracket.stop_loss):
            self._assert_strategy(intent.strategy_code)
        payload = self._request(
            "POST", "/v1/orders/bracket", payload=bracket.model_dump(mode="json")
        )
        return [ExecutionResult.model_validate(row) for row in payload]

    def submit_oca(self, group: OCAOrderIntentGroup) -> list[ExecutionResult]:
        for intent in group.orders:
            self._assert_strategy(intent.strategy_code)
        payload = self._request("POST", "/v1/orders/oca", payload=group.model_dump(mode="json"))
        return [ExecutionResult.model_validate(row) for row in payload]

    def submit_combo(self, intent: DefinedRiskOptionComboIntent) -> ExecutionResult:
        self._assert_strategy(intent.strategy_code)
        return ExecutionResult.model_validate(
            self._request("POST", "/v1/orders/combo", payload=intent.model_dump(mode="json"))
        )

    def replace(self, command: OrderReplaceCommand) -> ExecutionResult:
        return ExecutionResult.model_validate(
            self._request("POST", "/v1/orders/replace", payload=command.model_dump(mode="json"))
        )

    def cancel(self, command: OrderCancelCommand) -> ExecutionResult:
        return ExecutionResult.model_validate(
            self._request("POST", "/v1/orders/cancel", payload=command.model_dump(mode="json"))
        )

    def order(self, client_order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/orders/{quote(client_order_id, safe='')}")

    def readiness(self) -> dict[str, Any]:
        return self._request("GET", "/readyz")

    def order_events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        wait_seconds: float = 0,
    ) -> StrategyOrderEventPage:
        """读取持久化事件分页；游标可写入 checkpoint 并恢复。"""

        query = urlencode(
            {
                "after_event_id": after_event_id,
                "limit": limit,
                "wait_seconds": wait_seconds,
            }
        )
        return StrategyOrderEventPage.model_validate(
            self._request("GET", f"/v1/order-events?{query}")
        )

    def iter_order_events(
        self,
        *,
        after_event_id: int = 0,
        wait_seconds: float = 20,
    ) -> Iterator[StrategyOrderEvent]:
        """持续消费订单事件，重连后不会丢失读取位置。"""

        cursor = after_event_id
        while True:
            page = self.order_events(
                after_event_id=cursor,
                wait_seconds=wait_seconds,
            )
            for event in page.events:
                cursor = event.event_id
                yield event

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "StrategyExecutionClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _assert_strategy(self, requested: str) -> None:
        # 客户端先做一次本地约束，服务端仍会用凭据绑定关系再次校验，不能依赖客户端自觉。
        if requested != self.strategy_code:
            raise PermissionError(
                f"client for strategy {self.strategy_code!r} cannot submit for {requested!r}"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._http.request(
                method,
                f"{self.config.base_url.rstrip('/')}{path}",
                headers={
                    "X-API-Key": self.config.api_key,
                    "X-Strategy-Code": self.strategy_code,
                    "Accept": "application/json",
                    "User-Agent": "quant-strategy-execution-sdk/1.0",
                },
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except (OSError, URLError) as exc:
            raise StrategyExecutionClientError(
                status_code=0,
                detail=f"execution service is unavailable: {exc}",
            ) from exc
        if 200 <= response.status_code < 300:
            return response.json()
        try:
            body = response.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = response.text or "execution service request failed"
        raise StrategyExecutionClientError(
            status_code=response.status_code,
            detail=str(detail),
        )
