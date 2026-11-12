from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send


class TrustedRealIPMiddleware:
    """把 nginx 透传的 X-Real-IP 写回 scope["client"]，供限流与审计使用。

    nginx 已按 set_real_ip_from 解析出真实客户端并覆盖 X-Real-IP；这里只在直连对端
    属于受信代理网段时才采信该头，避免绕过 nginx 的请求伪造来源 IP。
    """

    def __init__(self, app: ASGIApp, *, trusted_cidrs: Iterable[str]) -> None:
        self.app = app
        self.trusted_networks = [ipaddress.ip_network(cidr, strict=False) for cidr in trusted_cidrs]

    def _is_trusted_peer(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            client = scope.get("client")
            if client and self._is_trusted_peer(client[0]):
                real_ip = _header(scope, b"x-real-ip")
                if real_ip:
                    try:
                        ipaddress.ip_address(real_ip)
                    except ValueError:
                        real_ip = None
                if real_ip:
                    scope = dict(scope)
                    scope["client"] = (real_ip, client[1])
        await self.app(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1").strip() or None
    return None
