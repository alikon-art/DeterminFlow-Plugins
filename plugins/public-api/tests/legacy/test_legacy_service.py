from __future__ import annotations

import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from determinflow_plugin_public_api.backend.catalog import (
    PublicModelCatalogClient,
)
from determinflow_plugin_public_api.backend.legacy_portal import (
    PortalRequestError,
    LegacyPortalClient as PublicApiPortalClient,
)
from determinflow_plugin_public_api.backend.legacy_service import LegacyCredentialService as PublicApiCredentialService


class FakeProviderGateway:
    def __init__(self) -> None:
        self.providers: dict[str, dict[str, Any]] = {
            "deepseek": {
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "",
                "models": ["deepseek-chat"],
            }
        }

    async def is_usable(self, provider_id: str) -> bool:
        provider = self.providers.get(provider_id) or {}
        return bool(provider.get("api_key") and provider.get("base_url"))

    async def apply(self, credential: dict[str, Any]) -> None:
        provider_id = credential["provider_id"]
        self.providers[provider_id] = {
            "name": credential.get("provider_display_name") or "笔枢公益模型",
            "base_url": credential["base_url"],
            "api_key": credential["api_key"],
            "models": credential["models"],
            "models_config": credential["models_config"],
        }
        provider = self.providers[provider_id]
        self.providers = {
            provider_id: provider,
            **{
                key: value
                for key, value in self.providers.items()
                if key != provider_id
            },
        }

    async def remove(self, provider_id: str) -> None:
        self.providers.pop(provider_id, None)


class FakeBrowserAuthorization:
    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.tokens = tokens or {
            "access_token": "access-old",
            "refresh_token": "refresh-old",
        }
        self.installation_ids: list[str] = []

    async def authorize(
        self,
        _portal: PublicApiPortalClient,
        installation_id: str,
    ) -> dict[str, str]:
        self.installation_ids.append(installation_id)
        return self.tokens


def credential_response(
    now: datetime,
    *,
    api_key: str = "public-key-1",
    credential_id: str = "credential-1",
    access_tier: str = "anonymous",
    ttl: timedelta = timedelta(days=1),
    remaining_usd: float = 0.75,
    daily_limit_usd: float | None = None,
    daily_used_usd: float | None = None,
    login_enabled: bool = True,
    payment_enabled: bool = False,
    payment_url: str | None = None,
    header_recharge_enabled: bool | None = None,
    model_page_recharge_enabled: bool | None = None,
    base_url: str = "https://relay.example.test/v1",
    account_display_name: str | None = None,
) -> dict[str, Any]:
    account_balance = 8.5 if access_tier in {"authenticated", "restricted"} else None
    authenticated = access_tier == "authenticated"
    return {
        "provider_id": "determinflow-public",
        "base_url": base_url,
        "api_key": api_key,
        "credential_id": credential_id,
        "expires_at": (now + ttl).isoformat(),
        "models": ["public-model"],
        "access_tier": access_tier,
        "quota": {
            "remaining_usd": remaining_usd,
            "total_limit_usd": 10 if authenticated else 1,
            "total_used_usd": 1.25 if authenticated else 1 - remaining_usd,
            "daily_limit_usd": (
                daily_limit_usd
                if daily_limit_usd is not None
                else (3 if authenticated else 1.5)
            ),
            "daily_used_usd": (
                daily_used_usd
                if daily_used_usd is not None
                else (2.25 if authenticated else 0.25)
            ),
            "weekly_limit_usd": 10 if authenticated else 6,
            "weekly_used_usd": 1.25,
            "measured_at": now.isoformat(),
        },
        "account_balance_usd": account_balance,
        "account_display_name": account_display_name,
        "ui": {
            "login_enabled": login_enabled,
            "payment_enabled": payment_enabled,
            "header_recharge_enabled": (
                payment_enabled
                if header_recharge_enabled is None
                else header_recharge_enabled
            ),
            "model_page_recharge_enabled": (
                payment_enabled
                if model_page_recharge_enabled is None
                else model_page_recharge_enabled
            ),
            "payment_url": (
                payment_url or "https://portal.example.test/public-api/top-up"
                if payment_enabled
                else None
            ),
        },
    }


def build_service(
    tmp_path: Path,
    handler: Any,
    *,
    clock: Any,
    release_channel: str = "stable",
    browser_auth: Any = None,
    client_config: dict[str, Any] | None = None,
    announcements: list[dict[str, Any]] | None = None,
    scheduler_interval_seconds: float = 0.01,
) -> tuple[PublicApiCredentialService, FakeProviderGateway]:
    providers = FakeProviderGateway()
    default_client_config = {
        "service_enabled": True,
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

    def portal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public-api/client-config":
            return httpx.Response(200, json=client_config or default_client_config)
        if request.url.path == "/api/public-api/announcements":
            return httpx.Response(200, json=announcements or [])
        return handler(request)

    portal = PublicApiPortalClient(
        "https://portal.example.test",
        app_version="0.1.0",
        transport=httpx.MockTransport(portal_handler),
    )

    def catalog_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Bearer public-key-")
        return httpx.Response(
            200,
            json={
                "unit": "per_million_tokens",
                "models": [
                    {
                        "id": "public-model",
                        "display_name": "Public Model",
                        "provider_type": "openai_compatible",
                        "prices": [
                            {
                                "input_price": 1.02,
                                "cache_hit_price": 0.02,
                                "output_price": 2.04,
                                "currency": "CNY",
                            }
                        ],
                    }
                ],
            },
        )

    service = PublicApiCredentialService(
        tmp_path,
        app_version="0.1.0",
        release_channel=release_channel,
        portal=portal,
        catalog=PublicModelCatalogClient(
            app_version="0.1.0",
            transport=httpx.MockTransport(catalog_handler),
        ),
        providers=providers,
        clock=clock,
        scheduler_interval_seconds=scheduler_interval_seconds,
        browser_auth=browser_auth,
    )
    return service, providers


def test_runtime_client_config_updates_copy_and_disables_managed_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        enabled = True

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        config = {
            "service_enabled": enabled,
            "login_enabled": False,
            "payment_enabled": False,
            "header_recharge_enabled": False,
            "model_page_recharge_enabled": False,
            "recharge_ratio": 0.8,
            "provider_display_name": "动态供应商名",
            "attribution": "动态来源文案",
            "service_notice": "动态风险说明",
            "official_url": "https://bishuxiezuo.cn/",
            "top_up_title": "笔枢点数充值",
            "top_up_subtitle": "充值金额进入当前账号。",
            "top_up_ratio_notice": "当前比例 {ratio}。",
        }
        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            client_config=config,
        )
        await service.refresh_client_config(force=True)
        status = await service.ensure_credential()
        assert status.ui.provider_display_name == "动态供应商名"
        assert providers.providers["determinflow-public"]["name"] == "动态供应商名"
        assert status.header_status is not None
        assert status.header_status.summary == "动态来源文案"
        assert "account" not in [action.id for action in status.header_status.actions]

        config["service_enabled"] = False
        status = await service.refresh_client_config(force=True)
        assert status.state == "disabled"
        assert "determinflow-public" not in providers.providers

    asyncio.run(scenario())


def test_status_exposes_dedicated_public_model_announcements(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            announcements=[
                {
                    "id": "announcement-one",
                    "title": "模型维护通知",
                    "body": "今晚 23:00 进行短时维护。",
                    "level": "maintenance",
                    "published_at": now.isoformat(),
                    "expires_at": (now + timedelta(days=1)).isoformat(),
                }
            ],
        )

        status = await service.refresh_client_config(force=True)

        assert len(status.announcements) == 1
        assert status.announcements[0].title == "模型维护通知"
        assert status.announcements[0].level == "maintenance"
        assert status.ui.service_notice == "仅供体验。"

    asyncio.run(scenario())


def test_anonymous_credential_becomes_default_without_duplicate_key_storage(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/public-api/credentials"
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(tmp_path, handler, clock=lambda: now)
        status = await service.ensure_credential()

        assert status.state == "active"
        assert status.access_tier == "anonymous"
        assert status.model_catalog[0].display_name == "Public Model"
        assert status.model_catalog[0].prices[0].cache_hit_price == 0.02
        assert status.signed_in is False
        assert status.account_balance_usd is None
        assert status.header_status is not None
        assert status.header_status.label == "公益"
        assert status.header_status.value == "¥0.75"
        assert status.header_status.title == "公益模型额度"
        assert status.header_status.summary == "由笔枢写作（网页版）免费提供"
        assert status.header_status.summary_href == "https://bishuxiezuo.cn/"
        assert [metric.label for metric in status.header_status.metrics] == [
            "今日限额余量",
            "本周限额余量",
        ]
        assert [metric.value for metric in status.header_status.metrics] == [
            "¥1.25",
            "¥4.75",
        ]
        assert [item.label for item in status.header_status.metadata] == [
            "身份",
            "额度状态",
            "有效期至",
            "更新时间",
        ]
        assert status.header_status.metadata[0].value == "匿名"
        assert status.header_status.metadata[1].value == "标准"
        assert status.header_status.metadata[2].value == "08-09 16:00"
        assert status.header_status.metadata[3].value == "08-08 16:00"
        assert [action.id for action in status.header_status.actions] == [
            "models",
            "account",
        ]
        assert status.header_status.actions[0].kind == "page"
        assert status.header_status.actions[1].label == "登录笔枢"
        assert status.header_status.actions[1].kind == "request"
        assert status.header_status.actions[1].endpoint == "/api/public-api/login"
        assert status.header_status.actions[1].method == "POST"
        assert status.renewal_due_at == now + timedelta(hours=18)
        assert next(iter(providers.providers)) == "determinflow-public"
        assert providers.providers["determinflow-public"]["api_key"] == "public-key-1"
        assert requests[0]["platform"] == "windows"
        assert requests[0]["app_version"] == "0.1.0"
        assert requests[0]["release_channel"] == "stable"
        assert "credential_id" not in requests[0]
        state = service.state_path.read_text(encoding="utf-8")
        assert "public-key-1" not in state
        assert stat.S_IMODE(service.state_path.stat().st_mode) & 0o077 == 0

    asyncio.run(scenario())


def test_scheduler_restores_provider_deleted_outside_normal_settings_flow(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(tmp_path, handler, clock=lambda: now)
        await service.start()
        try:
            await asyncio.sleep(0.03)
            assert requests == 0
            assert "determinflow-public" not in providers.providers

            await service.ensure_credential()
            assert "determinflow-public" in providers.providers
            providers.providers.pop("determinflow-public")

            for _ in range(20):
                if "determinflow-public" in providers.providers:
                    break
                await asyncio.sleep(0.01)

            assert "determinflow-public" in providers.providers
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_development_release_channel_is_forwarded(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=credential_response(now))

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            release_channel="development",
        )
        await service.ensure_credential()

        assert requests[0]["release_channel"] == "development"

    asyncio.run(scenario())


def test_development_allows_loopback_http_services(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=credential_response(
                    now,
                    base_url="http://127.0.0.1:8180/v1",
                    payment_enabled=True,
                    payment_url="http://127.0.0.1:5173/site/public-api-top-up.html",
                ),
            )

        portal = PublicApiPortalClient(
            "http://localhost:8006",
            app_version="0.1.2",
            allow_loopback_http=True,
            transport=httpx.MockTransport(handler),
        )
        providers = FakeProviderGateway()

        def catalog_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "unit": "per_million_tokens",
                    "models": [
                        {
                            "id": "public-model",
                            "display_name": "Public Model",
                            "provider_type": "openai_compatible",
                            "prices": [
                                {
                                    "input_price": 1,
                                    "output_price": 2,
                                    "currency": "CNY",
                                }
                            ],
                        }
                    ],
                },
            )

        service = PublicApiCredentialService(
            tmp_path,
            app_version="0.1.2",
            release_channel="development",
            clock=lambda: now,
            portal=portal,
            catalog=PublicModelCatalogClient(
                app_version="0.1.2",
                transport=httpx.MockTransport(catalog_handler),
            ),
            providers=providers,
        )

        status = await service.ensure_credential()

        assert status.state == "active"
        assert status.ui.payment_enabled is True
        assert providers.providers["determinflow-public"]["base_url"] == (
            "http://127.0.0.1:8180/v1"
        )
        assert (
            providers.providers["determinflow-public"]["models_config"]["public-model"][
                "provider_type"
            ]
            == "openai_compatible"
        )

    asyncio.run(scenario())


def test_loopback_http_services_remain_disabled_by_default() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        PublicApiPortalClient(
            "http://127.0.0.1:8006",
            app_version="0.1.2",
        )

    with pytest.raises(ValueError, match="HTTPS"):
        PublicApiPortalClient(
            "http://portal.example.test",
            app_version="0.1.2",
            allow_loopback_http=True,
        )


def test_renewal_uses_existing_credential_inside_lead_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        current = [datetime(2026, 8, 8, 8, tzinfo=UTC)]
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json=credential_response(
                    current[0],
                    api_key=f"public-key-{len(requests)}",
                    credential_id=f"credential-{len(requests)}",
                ),
            )

        service, providers = build_service(tmp_path, handler, clock=lambda: current[0])
        await service.ensure_credential()
        current[0] += timedelta(hours=19)
        status = await service.ensure_credential()

        assert status.state == "active"
        assert requests[1]["credential_id"] == "credential-1"
        assert providers.providers["determinflow-public"]["api_key"] == "public-key-2"

    asyncio.run(scenario())


def test_failed_renewal_keeps_unexpired_provider_and_reports_degradation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        fail = [False]

        def handler(_request: httpx.Request) -> httpx.Response:
            if fail[0]:
                return httpx.Response(503, json={"detail": "disabled"})
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(tmp_path, handler, clock=lambda: now)
        await service.ensure_credential()
        fail[0] = True
        status = await service.ensure_credential(force=True)

        assert status.state == "degraded"
        assert status.last_error == "公益模型服务暂不可用"
        assert providers.providers["determinflow-public"]["api_key"] == "public-key-1"

    asyncio.run(scenario())


def test_failed_anonymous_reissue_after_logout_keeps_header_status_visible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/desktop-auth/logout":
                return httpx.Response(204)
            if request.headers.get("authorization"):
                return httpx.Response(
                    200,
                    json=credential_response(
                        now,
                        access_tier="authenticated",
                        ttl=timedelta(days=7),
                    ),
                )
            return httpx.Response(503, json={"detail": "temporarily unavailable"})

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(),
        )
        await service.start_login()
        assert service._login_task is not None
        await service._login_task
        assert service.status().signed_in is True

        status = await service.logout()

        assert status.state == "unavailable"
        assert status.signed_in is False
        assert status.last_error == "公益模型服务暂不可用"
        assert status.header_status is not None
        assert status.header_status.value == "异常"
        assert status.header_status.summary == "更新失败：公益模型服务暂不可用"
        assert [action.id for action in status.header_status.actions] == [
            "models",
            "account",
        ]
        assert status.header_status.actions[-1].label == "登录笔枢"
        assert "determinflow-public" not in providers.providers

    asyncio.run(scenario())


def test_logout_transition_keeps_a_pollable_header_status(tmp_path: Path) -> None:
    now = datetime(2026, 8, 8, 8, tzinfo=UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=credential_response(now))

    service, _providers = build_service(tmp_path, handler, clock=lambda: now)
    service._logout_in_progress = True

    status = service.status()

    assert status.header_status is not None
    assert status.header_status.value == "更新中"
    assert status.header_status.refresh_after_ms == 1000
    assert status.header_status.metadata[0].value == "正在退出"


def test_repeated_login_and_logout_never_hides_header_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/desktop-auth/logout":
                return httpx.Response(204)
            access_tier = (
                "authenticated" if request.headers.get("authorization") else "anonymous"
            )
            return httpx.Response(
                200,
                json=credential_response(now, access_tier=access_tier),
            )

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(),
        )

        for _ in range(3):
            pending = await service.start_login()
            assert pending.header_status is not None
            assert service._login_task is not None
            await service._login_task
            signed_in = service.status()
            assert signed_in.signed_in is True
            assert signed_in.header_status is not None

            anonymous = await service.logout()
            assert anonymous.signed_in is False
            assert anonymous.header_status is not None

    asyncio.run(scenario())


def test_expired_provider_is_removed_when_portal_is_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        current = [datetime(2026, 8, 8, 8, tzinfo=UTC)]
        fail = [False]

        def handler(_request: httpx.Request) -> httpx.Response:
            if fail[0]:
                return httpx.Response(503, json={"detail": "disabled"})
            return httpx.Response(200, json=credential_response(current[0]))

        service, providers = build_service(tmp_path, handler, clock=lambda: current[0])
        await service.ensure_credential()
        current[0] += timedelta(days=2)
        fail[0] = True
        status = await service.ensure_credential()

        assert status.state == "unavailable"
        assert "determinflow-public" not in providers.providers

    asyncio.run(scenario())


def test_login_refreshes_session_and_issues_seven_day_credential(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)
        authorizations: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/desktop-auth/refresh":
                return httpx.Response(
                    200,
                    json={"access_token": "access-new", "refresh_token": "refresh-new"},
                )
            if request.url.path == "/api/public-api/credentials":
                authorization = request.headers.get("authorization")
                authorizations.append(authorization)
                if authorization is None:
                    return httpx.Response(200, json=credential_response(now))
                if authorization == "Bearer access-old":
                    return httpx.Response(401, json={"detail": "expired"})
                assert authorization == "Bearer access-new"
                return httpx.Response(
                    200,
                    json=credential_response(
                        now,
                        access_tier="authenticated",
                        ttl=timedelta(days=7),
                        account_display_name="测试作者",
                    ),
                )
            raise AssertionError(request.url.path)

        browser_auth = FakeBrowserAuthorization()
        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=browser_auth,
        )
        pending = await service.start_login()
        assert pending.login_pending is True
        assert pending.header_status is not None
        assert pending.header_status.value == "登录中"
        assert pending.header_status.refresh_after_ms == 1000
        assert [action.label for action in pending.header_status.actions] == [
            "取消登录"
        ]
        assert pending.login_endpoint == "/api/public-api/login"
        login_task = service._login_task
        assert login_task is not None
        await login_task
        status = service.status()

        assert status.state == "active"
        assert status.signed_in is True
        assert status.access_tier == "authenticated"
        assert status.account_balance_usd == 8.5
        assert status.balance_tier == "paid"
        assert status.account_display_name == "测试作者"
        assert status.header_status is not None
        assert status.header_status.label == "公益"
        assert status.header_status.value == "¥9.25"
        assert status.header_status.title == "公益模型额度"
        assert [metric.label for metric in status.header_status.metrics] == [
            "今日免费额度",
            "充值余额",
            "本周免费额度",
        ]
        assert [metric.value for metric in status.header_status.metrics] == [
            "¥0.75",
            "¥8.50",
            "¥8.75",
        ]
        assert status.header_status.metadata[0].value == "已登录 · 测试作者"
        assert status.header_status.metadata[1].value == "充值模型组"
        assert status.renewal_due_at == now + timedelta(days=6)
        assert authorizations == ["Bearer access-old", "Bearer access-new"]
        assert browser_auth.installation_ids == [service.state["installation_id"]]
        saved = service.state_path.read_text(encoding="utf-8")
        assert "refresh-new" in saved

    asyncio.run(scenario())


def test_login_accepts_authenticated_credential_when_wallet_is_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            response = credential_response(
                now,
                access_tier=(
                    "authenticated"
                    if request.headers.get("authorization")
                    else "anonymous"
                ),
            )
            if request.headers.get("authorization"):
                response["account_balance_usd"] = None
            return httpx.Response(200, json=response)

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(),
        )
        await service.ensure_credential()
        await service.start_login()
        login_task = service._login_task
        assert login_task is not None
        await login_task
        status = service.status()

        assert status.signed_in is True
        assert status.access_tier == "authenticated"
        assert status.account_balance_usd is None
        assert status.balance_tier == "free"
        assert status.last_error is None
        assert status.header_status is not None
        assert status.header_status.title == "公益模型额度"
        assert status.header_status.metadata[0].value == "已登录"
        assert status.header_status.metadata[1].value == "免费模型组"
        assert status.header_status.metrics[1].value == "—"

    asyncio.run(scenario())


def test_status_survives_a_legacy_session_without_account_balance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=credential_response(now))

        service, _providers = build_service(tmp_path, handler, clock=lambda: now)
        await service.ensure_credential()
        service.state["portal_session"] = {
            "access_token": "legacy-access",
            "refresh_token": "legacy-refresh",
        }
        status = service.status()

        assert status.signed_in is True
        assert status.account_balance_usd is None
        assert status.header_status is not None
        assert status.header_status.value == "¥0.75"
        assert [metric.label for metric in status.header_status.metrics] == [
            "今日限额余量",
            "本周限额余量",
        ]
        assert [metric.value for metric in status.header_status.metrics] == [
            "¥1.25",
            "¥4.75",
        ]

    asyncio.run(scenario())


def test_backend_ui_capabilities_drive_header_actions(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=credential_response(
                    now,
                    access_tier="authenticated",
                    payment_enabled=True,
                ),
            )

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(
                {"access_token": "access", "refresh_token": "refresh"}
            ),
            client_config={
                "service_enabled": True,
                "login_enabled": True,
                "payment_enabled": True,
                "header_recharge_enabled": True,
                "model_page_recharge_enabled": True,
                "payment_url": "https://portal.example.test/public-api/top-up",
                "provider_display_name": "笔枢公益模型",
                "attribution": "由笔枢写作（网页版）免费提供",
                "service_notice": "仅供体验。",
                "official_url": "https://bishuxiezuo.cn/",
            },
        )
        await service.start_login()
        login_task = service._login_task
        assert login_task is not None
        await login_task
        status = service.status()

        assert status.header_status is not None
        assert [action.id for action in status.header_status.actions] == [
            "models",
            "payment",
            "account",
        ]
        assert status.header_status.actions[0].kind == "page"
        assert status.header_status.actions[1].href == (
            "https://portal.example.test/public-api/top-up"
        )
        assert status.header_status.actions[2].label == "退出登录"
        assert status.header_status.actions[2].method == "DELETE"

    asyncio.run(scenario())


def test_header_recharge_switch_does_not_control_model_page_switch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=credential_response(
                    now,
                    access_tier="authenticated",
                    payment_enabled=True,
                    header_recharge_enabled=False,
                    model_page_recharge_enabled=True,
                ),
            )

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(),
            client_config={
                "service_enabled": True,
                "login_enabled": True,
                "payment_enabled": True,
                "header_recharge_enabled": False,
                "model_page_recharge_enabled": True,
                "payment_url": "https://portal.example.test/public-api/top-up",
                "provider_display_name": "笔枢公益模型",
                "attribution": "由笔枢写作（网页版）免费提供",
                "service_notice": "仅供体验。",
                "official_url": "https://bishuxiezuo.cn/",
            },
        )
        await service.start_login()
        assert service._login_task is not None
        await service._login_task
        status = service.status()

        assert status.ui.model_page_recharge_enabled is True
        assert status.ui.header_recharge_enabled is False
        assert status.header_status is not None
        assert [action.id for action in status.header_status.actions] == [
            "models",
            "account",
        ]

    asyncio.run(scenario())


def test_pending_login_can_be_cancelled_without_replacing_anonymous_credential(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        class BlockingBrowserAuthorization:
            async def authorize(
                self,
                _portal: PublicApiPortalClient,
                _installation_id: str,
            ) -> dict[str, str]:
                await asyncio.Future()
                raise AssertionError("unreachable")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/public-api/credentials"
            return httpx.Response(200, json=credential_response(now))

        service, providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=BlockingBrowserAuthorization(),
        )
        await service.ensure_credential()
        original_credential = service.state["credential"]

        pending = await service.start_login()
        assert pending.login_pending is True
        assert pending.header_status is not None
        assert pending.header_status.actions[-1].label == "取消登录"

        cancelled = await service.cancel_login()

        assert cancelled.login_pending is False
        assert cancelled.last_error is None
        assert cancelled.header_status is not None
        assert cancelled.header_status.actions[-1].label == "登录笔枢"
        assert service.state["credential"] == original_credential
        assert "determinflow-public" in providers.providers

    asyncio.run(scenario())


def test_login_is_blocked_when_backend_disables_login(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=credential_response(now, login_enabled=False),
            )

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            client_config={
                "service_enabled": True,
                "login_enabled": False,
                "payment_enabled": False,
                "header_recharge_enabled": False,
                "model_page_recharge_enabled": False,
                "provider_display_name": "笔枢公益模型",
                "attribution": "由笔枢写作（网页版）免费提供",
                "service_notice": "仅供体验。",
                "official_url": "https://bishuxiezuo.cn/",
            },
        )
        await service.ensure_credential()

        with pytest.raises(PortalRequestError, match="登录暂未开放"):
            await service.start_login()

    asyncio.run(scenario())


def test_invalid_refresh_falls_back_to_anonymous_without_core_account_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/desktop-auth/refresh":
                return httpx.Response(401, json={"detail": "invalid"})
            if request.url.path == "/api/public-api/credentials":
                if request.headers.get("authorization"):
                    return httpx.Response(401, json={"detail": "expired"})
                return httpx.Response(200, json=credential_response(now))
            raise AssertionError(request.url.path)

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(
                {"access_token": "expired", "refresh_token": "invalid"}
            ),
        )
        await service.start_login()
        login_task = service._login_task
        assert login_task is not None
        await login_task
        status = service.status()

        assert status.state == "active"
        assert status.signed_in is False
        assert status.access_tier == "anonymous"
        assert service.state["portal_session"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    (
        "restricted_remaining_usd",
        "daily_limit_usd",
        "daily_used_usd",
        "expected_daily_remaining",
    ),
    [
        (0.5, 1.5, 0.25, "¥0.50"),
        (0.75, 1.5, 1.25, "¥0.25"),
    ],
)
def test_restricted_credential_uses_lower_daily_limit_and_anonymous_renewal_window(
    tmp_path: Path,
    restricted_remaining_usd: float,
    daily_limit_usd: float,
    daily_used_usd: float,
    expected_daily_remaining: str,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 8, 8, tzinfo=UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=credential_response(
                    now,
                    access_tier="restricted",
                    ttl=timedelta(days=1),
                    remaining_usd=restricted_remaining_usd,
                    daily_limit_usd=daily_limit_usd,
                    daily_used_usd=daily_used_usd,
                ),
            )

        service, _providers = build_service(
            tmp_path,
            handler,
            clock=lambda: now,
            browser_auth=FakeBrowserAuthorization(
                {"access_token": "access", "refresh_token": "refresh"}
            ),
        )
        await service.start_login()
        login_task = service._login_task
        assert login_task is not None
        await login_task
        status = service.status()

        assert status.signed_in is True
        assert status.access_tier == "restricted"
        assert status.header_status is not None
        assert status.header_status.label == "公益"
        assert status.header_status.value == f"¥{restricted_remaining_usd:.2f}"
        assert [metric.label for metric in status.header_status.metrics] == [
            "今日限额余量",
            "本周限额余量",
        ]
        assert [metric.value for metric in status.header_status.metrics] == [
            expected_daily_remaining,
            "¥4.75",
        ]
        assert all(
            metric.label != "充值余额" for metric in status.header_status.metrics
        )
        assert status.header_status.metadata[0].value == "已登录"
        assert status.header_status.metadata[1].value == "受限"
        assert status.renewal_due_at == now + timedelta(hours=18)

    asyncio.run(scenario())
