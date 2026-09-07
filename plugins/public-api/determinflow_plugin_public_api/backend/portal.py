"""Novelbuilt Portal client used by the public API Plugin."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx


class PortalRequestError(RuntimeError):
    """A user-safe Portal request failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_allowed_service_url(value: str, *, allow_loopback_http: bool = False) -> bool:
    parsed = urlparse(value)
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        return False
    if parsed.scheme == "https":
        return True
    if not allow_loopback_http or parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class PublicApiPortalClient:
    """Client for the existing Portal auth and credential contracts."""

    def __init__(
        self,
        base_url: str,
        *,
        app_version: str,
        allow_loopback_http: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not is_allowed_service_url(
            normalized,
            allow_loopback_http=allow_loopback_http,
        ):
            raise ValueError("公益模型 Portal 地址必须是有效的 HTTPS 地址")
        self.base_url = normalized
        self.app_version = app_version
        self.transport = transport

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"DeterminFlow-Public-API-Plugin/{self.app_version}",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(6.0, connect=3.0),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=payload,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise PortalRequestError(
                "network_unavailable",
                "暂时无法连接公益模型服务",
            ) from exc

        if response.status_code == 401:
            raise PortalRequestError("authentication_failed", "笔枢登录状态已失效")
        if response.status_code == 403:
            raise PortalRequestError("verification_required", "当前请求需要额外验证")
        if response.status_code == 429:
            raise PortalRequestError("rate_limited", "公益模型请求过于频繁，请稍后重试")
        if response.status_code in {404, 502, 503, 504}:
            raise PortalRequestError("service_unavailable", "公益模型服务暂不可用")
        if response.status_code >= 400:
            raise PortalRequestError("request_failed", "公益模型请求失败")
        try:
            body = response.json()
        except ValueError as exc:
            raise PortalRequestError(
                "invalid_response",
                "公益模型服务返回了无效响应",
            ) from exc
        return body

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        body = await self._request_json(
            method,
            path,
            payload=payload,
            access_token=access_token,
        )
        if not isinstance(body, dict):
            raise PortalRequestError("invalid_response", "公益模型服务返回了无效响应")
        return body

    async def issue(
        self,
        payload: dict[str, Any],
        *,
        access_token: str | None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/public-api/credentials",
            payload=payload,
            access_token=access_token,
        )

    async def client_config(self) -> dict[str, Any]:
        return await self._request("GET", "/api/public-api/client-config")

    async def announcements(self) -> list[dict[str, Any]]:
        body = await self._request_json("GET", "/api/public-api/announcements")
        if not isinstance(body, list) or not all(
            isinstance(item, dict) for item in body
        ):
            raise PortalRequestError("invalid_response", "公益模型公告返回了无效响应")
        return body
