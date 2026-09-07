from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import httpx
from determinflow_plugin_public_api.backend.portal import (
    PortalRequestError,
    PublicApiPortalClient,
)

from test_service import FakeBrowserAuthorization, build_service, credential_response

T = TypeVar("T")


def client_config(*, service_enabled: bool = True) -> dict[str, Any]:
    return {
        "service_enabled": service_enabled,
        "login_enabled": True,
        "payment_enabled": False,
        "header_recharge_enabled": False,
        "model_page_recharge_enabled": False,
        "recharge_ratio": 0.8,
        "provider_display_name": "笔枢公益模型",
        "attribution": "由笔枢写作（网页版）免费提供",
        "service_notice": "仅供体验。",
        "official_url": "https://bishuxiezuo.cn/",
        "top_up_title": "笔枢点数充值",
        "top_up_subtitle": "充值金额进入当前账号。",
        "top_up_ratio_notice": "当前比例 {ratio}。",
    }


class PortalCallTracker:
    def __init__(self) -> None:
        self.inflight: set[str] = set()
        self.overlapped: set[tuple[str, str]] = set()
        self.started_at: dict[str, float] = {}
        self.ended_at: dict[str, float] = {}
        self.calls: list[str] = []

    def observe(
        self,
        name: str,
        original: Callable[..., Awaitable[T]],
        delay: float = 0.0,
    ) -> Callable[..., Awaitable[T]]:
        async def wrapped(*args: Any, **kwargs: Any) -> T:
            loop = asyncio.get_running_loop()
            self.calls.append(name)
            self.inflight.add(name)
            for other in self.inflight:
                if other != name:
                    pair = tuple(sorted((name, other)))
                    self.overlapped.add((pair[0], pair[1]))
            self.started_at.setdefault(name, loop.time())
            try:
                if delay:
                    await asyncio.sleep(delay)
                return await original(*args, **kwargs)
            finally:
                self.ended_at[name] = loop.time()
                self.inflight.discard(name)

        return wrapped

    def attach(
        self,
        portal: PublicApiPortalClient,
        delays: dict[str, float] | None = None,
    ) -> None:
        delays = delays or {}
        portal.client_config = self.observe(
            "client_config",
            portal.client_config,
            delays.get("client_config", 0.0),
        )
        portal.announcements = self.observe(
            "announcements",
            portal.announcements,
            delays.get("announcements", 0.0),
        )
        portal.issue = self.observe(
            "issue",
            portal.issue,
            delays.get("issue", 0.0),
        )


def test_refresh_fetches_client_config_and_announcements_in_parallel(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("refresh must not issue credentials")

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            client_config=client_config(),
            announcements=[
                {
                    "id": "announcement-one",
                    "title": "并行公告",
                    "body": "启动时独立拉取。",
                    "level": "info",
                    "published_at": now.isoformat(),
                }
            ],
            scheduler_interval_seconds=3600,
        )
        tracker = PortalCallTracker()
        tracker.attach(
            service.portal,
            {"client_config": 0.03, "announcements": 0.03},
        )

        status = await service.refresh_client_config(force=True)

        assert ("announcements", "client_config") in tracker.overlapped
        assert "issue" not in tracker.calls
        assert status.ui.service_enabled is True
        assert status.announcements[0].title == "并行公告"
        assert status.state == "unavailable"
        assert status.header_status is not None
        assert status.header_status.value == "未启用"
        assert status.header_status.title == "公益模型未启用"
        assert status.header_status.summary == "启用后可查看并使用公益模型额度。"
        assert status.header_status.tone == "attention"
        assert [action.id for action in status.header_status.actions] == [
            "enable",
            "models",
        ]
        enable_action = status.header_status.actions[0]
        assert enable_action.label == "启用公益模型"
        assert enable_action.kind == "request"
        assert enable_action.endpoint == "/api/public-api/renew"
        assert enable_action.method == "POST"
        assert status.header_status.actions[1].kind == "page"

    asyncio.run(scenario())


def test_start_overlaps_announcements_with_existing_credential_renewal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            announcements=[
                {
                    "id": "announcement-one",
                    "title": "续签并行公告",
                    "body": "公告可与续签重叠。",
                    "level": "info",
                    "published_at": now.isoformat(),
                }
            ],
            scheduler_interval_seconds=3600,
        )
        await service.ensure_credential()
        tracker = PortalCallTracker()
        tracker.attach(
            service.portal,
            {"announcements": 0.04, "issue": 0.04},
        )

        try:
            await service.start()
            status = service.status()

            assert tracker.started_at["issue"] >= tracker.ended_at["client_config"]
            assert ("announcements", "issue") in tracker.overlapped
            assert ("client_config", "issue") not in tracker.overlapped
            assert status.state == "active"
            assert status.announcements[0].title == "续签并行公告"
            assert "determinflow-public" in providers.providers
            assert service._scheduler_task is not None
            assert "public-key-1" not in service.state_path.read_text(encoding="utf-8")
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_does_not_renew_when_service_is_disabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        config = client_config(service_enabled=True)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            client_config=config,
            announcements=[
                {
                    "id": "announcement-disabled",
                    "title": "服务关闭",
                    "body": "关闭后仍展示公告。",
                    "level": "warning",
                    "published_at": now.isoformat(),
                }
            ],
            scheduler_interval_seconds=3600,
        )
        await service.ensure_credential()
        config["service_enabled"] = False
        tracker = PortalCallTracker()
        tracker.attach(
            service.portal,
            {"client_config": 0.03, "announcements": 0.03},
        )

        try:
            await service.start()
            status = service.status()

            assert "issue" not in tracker.calls
            assert ("announcements", "client_config") in tracker.overlapped
            assert status.state == "disabled"
            assert status.announcements[0].title == "服务关闭"
            assert "determinflow-public" not in providers.providers
            assert service.state["credential"] is None
            assert service._scheduler_task is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_renews_when_client_config_fails_with_existing_credential(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            announcements=[
                {
                    "id": "announcement-degraded",
                    "title": "配置失败仍续签",
                    "body": "配置失败时沿用已有凭据路径。",
                    "level": "info",
                    "published_at": now.isoformat(),
                }
            ],
            scheduler_interval_seconds=3600,
        )
        await service.ensure_credential()

        async def failing_config() -> dict[str, Any]:
            raise PortalRequestError("service_unavailable", "公益模型服务暂不可用")

        service.portal.client_config = failing_config
        tracker = PortalCallTracker()
        tracker.attach(service.portal, {"announcements": 0.03, "issue": 0.03})

        try:
            await service.start()
            status = service.status()

            assert "issue" in tracker.calls
            assert ("announcements", "issue") in tracker.overlapped
            assert status.state == "active"
            assert status.announcements[0].title == "配置失败仍续签"
            assert "determinflow-public" in providers.providers
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_does_not_renew_when_client_config_fails_after_disable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("disabled runtime must not issue credentials")

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            client_config=client_config(service_enabled=False),
            scheduler_interval_seconds=3600,
        )
        await service.refresh_client_config(force=True)
        service.state["credential"] = {
            "provider_id": "determinflow-public",
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "access_tier": "anonymous",
        }
        service.state["last_error"] = None
        service._save_state()
        providers.providers["determinflow-public"] = {
            "name": "笔枢公益模型",
            "base_url": "https://relay.example.test/v1",
            "api_key": "public-key-1",
            "models": ["public-model"],
        }

        async def failing_config() -> dict[str, Any]:
            raise PortalRequestError("service_unavailable", "公益模型服务暂不可用")

        service.portal.client_config = failing_config
        issue_calls: list[str] = []
        original_issue = service.portal.issue

        async def tracked_issue(*args: Any, **kwargs: Any) -> dict[str, Any]:
            issue_calls.append("issue")
            return await original_issue(*args, **kwargs)

        service.portal.issue = tracked_issue

        try:
            await service.start()
            status = service.status()

            assert issue_calls == []
            assert status.state == "disabled"
            assert service.state["credential"] is not None
            assert "determinflow-public" in providers.providers
            assert service._scheduler_task is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_keeps_unexpired_provider_when_renewal_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        fail = [False]

        def handler(_request: httpx.Request) -> httpx.Response:
            if fail[0]:
                return httpx.Response(503, json={"detail": "disabled"})
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            announcements=[
                {
                    "id": "announcement-keep",
                    "title": "续签失败公告",
                    "body": "失败后仍保留未过期 Provider。",
                    "level": "warning",
                    "published_at": now.isoformat(),
                }
            ],
            scheduler_interval_seconds=3600,
        )
        await service.ensure_credential()
        fail[0] = True

        try:
            await service.start()
            status = service.status()

            assert status.state == "degraded"
            assert status.last_error == "公益模型服务暂不可用"
            assert status.announcements[0].title == "续签失败公告"
            assert providers.providers["determinflow-public"]["api_key"] == "public-key-1"
            assert service.state_path.is_file()
            assert service._scheduler_task is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_applies_config_when_announcements_fail(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            scheduler_interval_seconds=3600,
        )
        await service.ensure_credential()
        previous_announcements = list(service._runtime_announcements)

        async def failing_announcements() -> list[dict[str, Any]]:
            raise PortalRequestError("service_unavailable", "公益模型服务暂不可用")

        service.portal.announcements = failing_announcements

        try:
            await service.start()
            status = service.status()

            assert status.state == "active"
            assert status.ui.service_enabled is True
            assert status.announcements == previous_announcements
            assert "determinflow-public" in providers.providers
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_start_renews_existing_session_without_credential(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer access-saved"
            return httpx.Response(
                200,
                json=credential_response(now, access_tier="authenticated"),
            )

        account_session = FakeBrowserAuthorization(
            {"access_token": "access-saved", "refresh_token": "refresh-saved"}
        )
        await account_session.login()
        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=account_session,
            scheduler_interval_seconds=3600,
        )

        try:
            await service.start()
            status = service.status()

            assert status.state == "active"
            assert status.signed_in is True
            assert status.access_tier == "authenticated"
            assert "determinflow-public" in providers.providers
            assert service._scheduler_task is not None
        finally:
            await service.stop()

    asyncio.run(scenario())
