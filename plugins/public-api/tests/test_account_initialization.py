from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from test_service import FakeBrowserAuthorization, build_service, credential_response


@pytest.mark.parametrize("existing_anonymous", [False, True])
def test_global_login_initializes_models_once(tmp_path: Path, existing_anonymous: bool) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 7, 13, tzinfo=UTC)
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            tier = "authenticated" if request.headers.get("authorization") else "anonymous"
            return httpx.Response(200, json=credential_response(now, access_tier=tier))

        account = FakeBrowserAuthorization()
        service, providers = build_service(tmp_path, handler, clock=lambda: now, browser_auth=account)
        # Merely opening onboarding must not issue anonymous credentials.
        await service.refresh_client_config()
        assert requests == []
        if existing_anonymous:
            await service.ensure_credential()
        requests.clear()
        await account.login()
        # Account event and login completion can both refresh the status.
        statuses = await asyncio.gather(service.refresh_client_config(), service.refresh_client_config())
        assert len(requests) == 1
        for status in statuses:
            assert status.signed_in is True
            assert status.state == "active"
            assert status.models == ["public-model"]
            assert status.access_tier == "authenticated"
            assert await providers.is_usable(status.provider_id)
        await service.refresh_client_config()
        assert len(requests) == 1

    asyncio.run(scenario())


def test_fresh_login_failure_is_visible_and_retry_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 7, 13, tzinfo=UTC)
        failing = True

        def handler(_request: httpx.Request) -> httpx.Response:
            if failing:
                return httpx.Response(503, json={"detail": "temporarily unavailable"})
            return httpx.Response(200, json=credential_response(now, access_tier="authenticated"))

        account = FakeBrowserAuthorization()
        service, _ = build_service(tmp_path, handler, clock=lambda: now, browser_auth=account)
        await account.login()
        failed = await service.refresh_client_config()
        assert failed.signed_in is True
        assert failed.state == "unavailable"
        assert failed.last_error
        assert failed.models == []
        assert failed.header_status is not None
        assert any(action.id == "retry" for action in failed.header_status.actions)
        failing = False
        recovered = await service.ensure_credential(force=True)
        assert recovered.state == "active"
        assert recovered.models == ["public-model"]
        assert recovered.last_error is None

    asyncio.run(scenario())
