from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试运行时不生成 pyc；配合禁用 pytest cache，避免验收后污染工作区。
sys.dont_write_bytecode = True


@pytest.fixture(autouse=True)
def isolated_safe_test_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """默认测试必须关闭外部连接，确保不会访问真实经纪商或实盘账户。"""

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("TRADING_MODE", "READ_ONLY")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TRADING_ALLOWED_ACCOUNTS", "")
    monkeypatch.setenv("SERVICE_API_KEYS", "")
    monkeypatch.setenv("SERVICE_API_IDENTITIES", "")

    original_socket_connect = socket.socket.connect

    def deny_tcp_connect(sock: socket.socket, address: object) -> None:
        # ASGI 集成测试走进程内 transport；任何 TCP 都意味着测试误触了外部系统。
        if sock.family == socket.AF_UNIX:
            original_socket_connect(sock, address)
            return
        raise AssertionError(f"tests must not open TCP connections: {address!r}")

    def deny_create_connection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tests must not create external network connections")

    monkeypatch.setattr(socket.socket, "connect", deny_tcp_connect)
    monkeypatch.setattr(socket, "create_connection", deny_create_connection)

    yield

    # 各运行时工厂带有进程级缓存，测试之间必须彻底隔离 PAPER/LIVE 状态。
    from platform_core.sdk import runtime as runtime_module

    runtime_module._get_trading_runtime.cache_clear()
    runtime_module.get_risk_control_runtime.cache_clear()
