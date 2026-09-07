from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import pytest

from determinflow_plugin_public_api.backend.browser_auth import BrowserAuthorizationFlow
from determinflow_plugin_public_api.backend.legacy_portal import (
    PortalRequestError,
    LegacyPortalClient as PublicApiPortalClient,
)


def test_browser_authorization_uses_loopback_state_and_pkce() -> None:
    async def scenario() -> None:
        opened_urls: list[str] = []

        def opener(url: str) -> bool:
            opened_urls.append(url)
            query = parse_qs(urlsplit(url).query)
            redirect_uri = query["redirect_uri"][0]
            callback = f"{redirect_uri}?{urlencode({'code': 'authorization-code-value', 'state': query['state'][0]})}"
            with urlopen(callback, timeout=2) as response:
                assert response.status == 200
                assert "登录已完成" in response.read().decode("utf-8")
            return True

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/desktop-auth/token"
            payload = json.loads(request.content)
            assert payload["grant_type"] == "authorization_code"
            assert payload["client_id"] == "determinflow-public-api"
            assert payload["code"] == "authorization-code-value"
            assert payload["redirect_uri"].startswith("http://127.0.0.1:")
            assert len(payload["code_verifier"]) >= 43
            return httpx.Response(
                200,
                json={"access_token": "desktop-access", "refresh_token": "desktop-refresh"},
            )

        portal = PublicApiPortalClient(
            "https://portal.example.test",
            app_version="0.1.11",
            transport=httpx.MockTransport(handler),
        )
        flow = BrowserAuthorizationFlow(opener=opener, callback_timeout_seconds=2)
        tokens = await flow.authorize(portal, "plugin:12345678-1234-1234-1234-123456789abc")

        assert tokens == {
            "access_token": "desktop-access",
            "refresh_token": "desktop-refresh",
        }
        assert len(opened_urls) == 1
        query = parse_qs(urlsplit(opened_urls[0]).query)
        assert urlsplit(opened_urls[0]).path == "/desktop-authorize.html"
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["code_challenge"][0]) == 43

    asyncio.run(scenario())


def test_browser_authorization_times_out_without_callback() -> None:
    async def scenario() -> None:
        portal = PublicApiPortalClient(
            "https://portal.example.test",
            app_version="0.1.11",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500)
            ),
        )
        flow = BrowserAuthorizationFlow(
            opener=lambda _url: True,
            callback_timeout_seconds=0.01,
        )
        with pytest.raises(PortalRequestError, match="登录已超时"):
            await flow.authorize(
                portal,
                "plugin:12345678-1234-1234-1234-123456789abc",
            )

    asyncio.run(scenario())


def test_browser_authorization_reports_portal_cancellation_immediately() -> None:
    async def scenario() -> None:
        def opener(url: str) -> bool:
            query = parse_qs(urlsplit(url).query)
            callback = (
                f"{query['redirect_uri'][0]}?"
                f"{urlencode({'error': 'access_denied', 'state': query['state'][0]})}"
            )
            with pytest.raises(HTTPError):
                urlopen(callback, timeout=2)
            return True

        portal = PublicApiPortalClient(
            "https://portal.example.test",
            app_version="0.1.11",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        flow = BrowserAuthorizationFlow(opener=opener, callback_timeout_seconds=2)

        with pytest.raises(PortalRequestError, match="登录已取消"):
            await flow.authorize(
                portal,
                "plugin:12345678-1234-1234-1234-123456789abc",
            )

    asyncio.run(scenario())
