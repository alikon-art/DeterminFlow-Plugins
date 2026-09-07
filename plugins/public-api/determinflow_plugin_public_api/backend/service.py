"""Credential lifecycle owned entirely by the optional public API Plugin."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol
from uuid import uuid4

from .catalog import CatalogRequestError, PublicModelCatalogClient
from .models import (
    HeaderStatus,
    HeaderStatusAction,
    HeaderStatusMetric,
    PublicApiAnnouncement,
    PublicApiClientUI,
    PublicApiQuota,
    PublicApiStatus,
)
from .portal import PortalRequestError, PublicApiPortalClient, is_allowed_service_url
from .provider import ProviderGateway, ProviderRequestError

logger = logging.getLogger(__name__)

_STATE_SCHEMA_VERSION = 3
_SCHEDULER_INTERVAL_SECONDS = 15 * 60
_QUOTA_STALE_AFTER = timedelta(minutes=20)
_ANONYMOUS_RENEWAL_LEAD = timedelta(hours=6)
_AUTHENTICATED_RENEWAL_LEAD = timedelta(days=1)
_BEIJING_TIME = timezone(timedelta(hours=8))


class CoreAccountSession(Protocol):
    @property
    def installation_id(self) -> str: ...

    def access_token(self) -> str | None: ...

    async def refresh_access_token(self, stale_access_token: str) -> str | None: ...

    async def login(self) -> dict[str, Any]: ...

    async def logout(self) -> dict[str, Any]: ...


class PublicApiCredentialService:
    """Manage one ordinary Provider credential for an installed Plugin."""

    def __init__(
        self,
        data_dir: Path,
        *,
        app_version: str,
        release_channel: str = "stable",
        platform_name: str = "windows",
        portal: PublicApiPortalClient | None,
        catalog: PublicModelCatalogClient,
        providers: ProviderGateway,
        disabled_reason: str | None = None,
        account_session: CoreAccountSession | None = None,
        clock: Callable[[], datetime] | None = None,
        scheduler_interval_seconds: float = _SCHEDULER_INTERVAL_SECONDS,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.app_version = app_version.strip() or "unknown"
        self.release_channel = release_channel.strip() or "stable"
        self.platform_name = platform_name
        self.portal = portal
        self.catalog = catalog
        self.providers = providers
        self.disabled_reason = disabled_reason
        self.account_session = account_session
        self.state_path = self.data_dir / "state.json"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scheduler_interval_seconds = scheduler_interval_seconds
        self._lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._runtime_ui: PublicApiClientUI | None = None
        self._runtime_announcements: list[PublicApiAnnouncement] = []
        self._runtime_ui_fetched_at: datetime | None = None
        self.state = self._load_state()

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "installation_id": f"plugin:{uuid4()}",
            "credential": None,
            "last_attempt_at": None,
            "last_error": self.disabled_reason,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            state = self._new_state()
            self._save_state(state)
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("schema_version") == 2:
                state.pop("portal_session", None)
                state["schema_version"] = _STATE_SCHEMA_VERSION
                self._save_state(state)
            if (
                not isinstance(state, dict)
                or state.get("schema_version") != _STATE_SCHEMA_VERSION
                or not isinstance(state.get("installation_id"), str)
            ):
                raise ValueError("unsupported state")
            return state
        except (OSError, ValueError):
            logger.warning("公益模型 Plugin 状态无效，已重建本地状态")
            state = self._new_state()
            state["last_error"] = "本地公益模型状态已重建，请重试"
            self._save_state(state)
            return state

    def _save_state(self, state: dict[str, Any] | None = None) -> None:
        target = self.state if state is None else state
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(target, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.state_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def start(self) -> None:
        await self._bootstrap_runtime(force=True, renew_credential=True)
        if self.portal is not None and self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(),
                name="public-api-plugin-renewal",
            )

    async def stop(self) -> None:
        if self._scheduler_task is None:
            return
        self._scheduler_task.cancel()
        await asyncio.gather(self._scheduler_task, return_exceptions=True)
        self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scheduler_interval_seconds)
            if self._credential() is None and not self._account_signed_in():
                continue
            try:
                await self.ensure_credential(force=True)
            except Exception:
                logger.exception("公益模型 Plugin 后台续签异常")

    def status(self) -> PublicApiStatus:
        credential = self._credential()
        signed_in = self._account_signed_in()
        expires_at = self._parse_datetime(
            credential.get("expires_at") if credential else None
        )
        last_error = self.disabled_reason or self.state.get("last_error")
        if self.portal is None or (
            self._runtime_ui is not None and not self._runtime_ui.service_enabled
        ):
            state_name = "disabled"
        elif expires_at and expires_at > self._clock():
            state_name = "degraded" if last_error else "active"
        else:
            state_name = "unavailable"
        quota = self._quota(credential)
        ui = self._client_ui(credential)
        account_balance = credential.get("account_balance_usd") if credential else None
        if not isinstance(account_balance, (int, float)) or isinstance(
            account_balance, bool
        ):
            account_balance = None
        account_display_name = (
            credential.get("account_display_name") if credential else None
        )
        if (
            not isinstance(account_display_name, str)
            or not account_display_name.strip()
        ):
            account_display_name = None
        balance_tier = None
        if credential and credential.get("access_tier") == "authenticated":
            balance_tier = (
                "paid"
                if isinstance(account_balance, (int, float)) and account_balance > 0
                else "free"
            )
        response = PublicApiStatus(
            state=state_name,
            signed_in=signed_in,
            login_pending=False,
            access_tier=credential.get("access_tier") if credential else None,
            balance_tier=balance_tier,
            provider_id=credential.get("provider_id") if credential else None,
            models=list(credential.get("models") or []) if credential else [],
            model_catalog=list(credential.get("model_catalog") or [])
            if credential
            else [],
            expires_at=expires_at,
            renewal_due_at=(
                self._renewal_due_at(credential) if credential and expires_at else None
            ),
            last_attempt_at=self._parse_datetime(self.state.get("last_attempt_at")),
            last_error=last_error if isinstance(last_error, str) else None,
            quota=quota,
            account_balance_usd=account_balance,
            account_display_name=account_display_name,
            announcements=list(self._runtime_announcements),
            ui=ui,
            header_status=None,
        )
        response.header_status = self._header_status(response)
        return response

    async def refresh_client_config(self, *, force: bool = False) -> PublicApiStatus:
        await self._bootstrap_runtime(force=force, renew_credential=False)
        # Core login can finish before the Plugin has ever issued a credential.
        # Recheck under the lock: concurrent status refreshes must provision once.
        async with self._lock:
            credential = self._credential()
            signed_in = self._account_signed_in()
            if (credential is None and signed_in) or (
                credential is not None
                and bool(credential.get("authenticated")) != signed_in
            ):
                return await self._ensure_locked(force=True)
            return self.status()

    async def _bootstrap_runtime(
        self,
        *,
        force: bool,
        renew_credential: bool,
    ) -> None:
        if self.portal is None:
            return
        now = self._clock()
        if (
            not force
            and self._runtime_ui_fetched_at is not None
            and now - self._runtime_ui_fetched_at < timedelta(seconds=60)
        ):
            return

        ui_task = asyncio.create_task(self._fetch_runtime_ui())
        announcements_task = asyncio.create_task(self._refresh_announcements())
        try:
            fetched_ui = await ui_task
        except BaseException:
            announcements_task.cancel()
            await asyncio.gather(announcements_task, return_exceptions=True)
            raise

        if fetched_ui is not None:
            self._runtime_ui = fetched_ui
            self._runtime_ui_fetched_at = now

        follow_ups: list[asyncio.Task[Any]] = [announcements_task]
        if fetched_ui is not None and not fetched_ui.service_enabled:
            follow_ups.append(asyncio.create_task(self._clear_managed_credential()))
        elif (
            renew_credential
            and (self._runtime_ui is None or self._runtime_ui.service_enabled)
            and (self._credential() is not None or self._account_signed_in())
        ):
            follow_ups.append(asyncio.create_task(self.ensure_credential(force=True)))
        results = await asyncio.gather(*follow_ups, return_exceptions=True)
        errors = [item for item in results if isinstance(item, BaseException)]
        for error in errors:
            if not isinstance(error, Exception):
                raise error
        if errors:
            raise errors[0]

    async def _fetch_runtime_ui(self) -> PublicApiClientUI | None:
        assert self.portal is not None
        try:
            body = await self.portal.client_config()
            return PublicApiClientUI.model_validate(body)
        except (PortalRequestError, TypeError, ValueError):
            return None

    async def _refresh_announcements(self) -> None:
        assert self.portal is not None
        try:
            announcements = await self.portal.announcements()
            self._runtime_announcements = [
                PublicApiAnnouncement.model_validate(item) for item in announcements
            ]
        except (PortalRequestError, TypeError, ValueError):
            return

    async def _clear_managed_credential(self) -> None:
        credential = self._credential()
        if not credential:
            return
        try:
            await self._remove_managed_provider(credential)
        except ProviderRequestError:
            logger.warning("关闭公益模型服务时无法移除托管 Provider")
        self.state["credential"] = None
        self._save_state()

    async def ensure_credential(self, *, force: bool = False) -> PublicApiStatus:
        async with self._lock:
            return await self._ensure_locked(force=force)

    async def _ensure_locked(self, *, force: bool) -> PublicApiStatus:
        if self.portal is None:
            return self.status()
        if self._runtime_ui is not None and not self._runtime_ui.service_enabled:
            return self.status()

        credential = self._credential()
        expires_at = self._parse_datetime(
            credential.get("expires_at") if credential else None
        )
        now = self._clock()
        provider_usable = False
        if credential and isinstance(credential.get("provider_id"), str):
            try:
                provider_usable = await self.providers.is_usable(
                    credential["provider_id"]
                )
            except ProviderRequestError:
                provider_usable = False
        if (
            not force
            and credential
            and expires_at
            and expires_at > now
            and provider_usable
            and bool(credential.get("model_catalog"))
            and now < self._renewal_due_at(credential)
        ):
            return self.status()

        if credential and (expires_at is None or expires_at <= now):
            try:
                await self._remove_managed_provider(credential)
            except ProviderRequestError:
                logger.warning("无法移除已过期的公益模型 Provider")
            self.state["credential"] = None
            credential = None

        self.state["last_attempt_at"] = now.isoformat()
        try:
            await self._request_and_apply(credential)
            self.state["last_error"] = None
        except PortalRequestError as exc:
            self.state["last_error"] = exc.message
        except CatalogRequestError as exc:
            self.state["last_error"] = exc.message
        except ProviderRequestError:
            self.state["last_error"] = "公益模型凭据无法写入 DeterminFlow"
        except (OSError, ValueError) as exc:
            logger.warning("公益模型 Plugin 状态保存失败: %s", exc)
            self.state["last_error"] = "公益模型凭据无法保存到本机"
        self._save_state()
        return self.status()

    async def login_account(self) -> PublicApiStatus:
        if self.account_session is None:
            raise PortalRequestError("service_unavailable", "Core 账号服务未启用")
        try:
            await self.account_session.login()
        except Exception as exc:
            self._raise_account_error(exc)
        return await self.ensure_credential(force=True)

    async def logout_account(self) -> PublicApiStatus:
        if self.account_session is None:
            raise PortalRequestError("service_unavailable", "Core 账号服务未启用")
        try:
            await self.account_session.logout()
        except Exception as exc:
            self._raise_account_error(exc)
        async with self._lock:
            credential = self._credential()
            if credential:
                try:
                    await self._remove_managed_provider(credential)
                except ProviderRequestError:
                    logger.warning("退出 Core 账号时无法移除公益模型 Provider")
            self.state["credential"] = None
            self.state["last_error"] = None
            self._save_state()
            return await self._ensure_locked(force=True)

    async def _request_and_apply(
        self,
        credential: dict[str, Any] | None,
    ) -> None:
        assert self.portal is not None
        access_token = self._account_access_token()
        signed_in = access_token is not None
        credential_id = self._renewable_credential_id(
            credential,
            signed_in,
        )
        payload = {
            "request_id": f"plugin:{uuid4()}",
            "installation_id": self._installation_id(),
            "app_version": self.app_version,
            "release_channel": self.release_channel,
            "platform": self.platform_name,
        }
        if credential_id:
            payload["credential_id"] = credential_id

        try:
            response = await self.portal.issue(payload, access_token=access_token)
        except PortalRequestError as exc:
            if exc.code != "authentication_failed" or access_token is None:
                raise
            try:
                refreshed = await self.account_session.refresh_access_token(access_token)
            except Exception as account_error:
                self._raise_account_error(account_error)
            if refreshed is None:
                payload.pop("credential_id", None)
                response = await self.portal.issue(payload, access_token=None)
                signed_in = False
            else:
                response = await self.portal.issue(
                    payload,
                    access_token=refreshed,
                )

        parsed = self._validate_credential_response(response)
        catalog = await self.catalog.fetch(
            parsed["base_url"],
            parsed["api_key"],
            parsed["models"],
        )
        parsed["models"] = catalog["models"]
        parsed["models_config"] = catalog["models_config"]
        parsed["model_catalog"] = catalog["model_catalog"]
        parsed["provider_display_name"] = self._client_ui(parsed).provider_display_name
        previous = credential
        await self.providers.apply(parsed)
        if previous and previous.get("provider_id") != parsed["provider_id"]:
            await self._remove_managed_provider(previous)
        parsed["authenticated"] = signed_in
        parsed["issued_at"] = self._clock().isoformat()
        parsed.pop("api_key")
        self.state["credential"] = parsed

    def _validate_credential_response(self, body: dict[str, Any]) -> dict[str, Any]:
        provider_id = body.get("provider_id")
        base_url = body.get("base_url")
        api_key = body.get("api_key")
        credential_id = body.get("credential_id")
        models = body.get("models")
        access_tier = body.get("access_tier")
        account_balance = body.get("account_balance_usd")
        account_display_name = body.get("account_display_name")
        try:
            quota = PublicApiQuota.model_validate(body.get("quota"))
            ui = PublicApiClientUI.model_validate(body.get("ui"))
        except (TypeError, ValueError) as exc:
            raise PortalRequestError(
                "invalid_response", "公益模型服务返回了无效额度"
            ) from exc
        expires_at = self._parse_datetime(body.get("expires_at"))
        base_allowed = (
            is_allowed_service_url(
                base_url,
                allow_loopback_http=self.release_channel == "development",
            )
            if isinstance(base_url, str)
            else False
        )
        payment_allowed = (
            is_allowed_service_url(
                ui.payment_url,
                allow_loopback_http=self.release_channel == "development",
            )
            if ui.payment_url
            else False
        )
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or not isinstance(base_url, str)
            or not base_allowed
            or not isinstance(api_key, str)
            or not api_key
            or not isinstance(credential_id, str)
            or not credential_id
            or not isinstance(models, list)
            or not models
            or not all(isinstance(model, str) and model for model in models)
            or access_tier not in {"anonymous", "authenticated", "restricted"}
            or (
                account_balance is not None
                and (
                    not isinstance(account_balance, (int, float))
                    or isinstance(account_balance, bool)
                    or account_balance < 0
                )
            )
            or (
                account_display_name is not None
                and (
                    not isinstance(account_display_name, str)
                    or not account_display_name.strip()
                    or len(account_display_name.strip()) > 80
                )
            )
            or (ui.payment_enabled and not payment_allowed)
            or expires_at is None
            or expires_at <= self._clock() + timedelta(minutes=1)
        ):
            raise PortalRequestError("invalid_response", "公益模型服务返回了无效凭据")
        return {
            "provider_id": provider_id,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "credential_id": credential_id,
            "expires_at": expires_at.isoformat(),
            "models": list(dict.fromkeys(models)),
            "access_tier": access_tier,
            "quota": quota.model_dump(mode="json"),
            "account_balance_usd": account_balance,
            "account_display_name": (
                account_display_name.strip()
                if isinstance(account_display_name, str)
                else None
            ),
            "ui": ui.model_dump(mode="json"),
        }

    def _header_status(self, status: PublicApiStatus) -> HeaderStatus | None:
        if status.state == "disabled":
            return HeaderStatus(
                visible=True, label="公益", value="不可用",
                title="公益模型暂不可用",
                summary=status.last_error or "公益模型服务暂未开放，请稍后重试。",
                tone="attention", metrics=[], metadata=[],
                actions=[HeaderStatusAction(id="models", label="模型列表", kind="page")],
                updated_at=self._clock(),
            )
        if status.state in {"active", "degraded"} and status.quota is None:
            return HeaderStatus(
                visible=True, label="公益", value="额度未知",
                title="公益模型额度暂不可用",
                summary=status.last_error or "暂未获取到额度，请刷新后重试。",
                tone="attention", metrics=[], metadata=[],
                actions=[HeaderStatusAction(id="retry", label="重试", kind="request",
                    endpoint="/api/public-api/renew", method="POST")],
                updated_at=self._clock(),
            )
        if status.state == "unavailable":
            now = self._clock()
            actions: list[HeaderStatusAction] = [
                HeaderStatusAction(
                    id="retry" if status.last_error else "enable",
                    label="重试" if status.last_error else "启用公益模型",
                    kind="request",
                    endpoint="/api/public-api/renew",
                    method="POST",
                ),
                HeaderStatusAction(
                    id="models",
                    label="模型列表",
                    kind="page",
                )
            ]
            return HeaderStatus(
                visible=True,
                label="公益",
                value="异常" if status.last_error else "未启用",
                title="公益模型更新异常" if status.last_error else "公益模型未启用",
                summary=(
                    f"更新失败：{status.last_error}"
                    if status.last_error
                    else "启用后可查看并使用公益模型额度。"
                ),
                tone="critical" if status.last_error else "attention",
                metrics=[],
                metadata=[
                    HeaderStatusMetric(
                        label="身份",
                        value=self._identity_label(status),
                    )
                ],
                actions=actions,
                updated_at=now,
            )
        if status.state not in {"active", "degraded"} or status.quota is None:
            return None

        is_account = status.access_tier == "authenticated"
        is_restricted = status.access_tier == "restricted"
        wallet_amount = (status.account_balance_usd or 0) if is_account else 0
        assert wallet_amount is not None
        amount = status.quota.remaining_usd + wallet_amount
        measured_at = status.quota.measured_at
        age = self._clock() - measured_at.astimezone(UTC)
        if amount <= 0:
            tone = "critical"
        elif age > _QUOTA_STALE_AFTER:
            tone = "stale"
        elif amount <= 1:
            tone = "attention"
        else:
            tone = "normal"

        actions: list[HeaderStatusAction] = [
            HeaderStatusAction(
                id="models",
                label="模型列表",
                kind="page",
            )
        ]
        if (
            status.signed_in
            and status.ui.payment_enabled
            and status.ui.header_recharge_enabled
            and status.ui.payment_url
        ):
            actions.append(
                HeaderStatusAction(
                    id="payment",
                    label="充值",
                    kind="link",
                    href=status.ui.payment_url,
                )
            )
        metrics: list[HeaderStatusMetric]
        if is_account:
            metrics = [
                HeaderStatusMetric(
                    label="今日免费额度",
                    value=self._money(status.quota.remaining_usd),
                ),
                HeaderStatusMetric(
                    label="充值余额",
                    value=(
                        self._money(status.account_balance_usd)
                        if status.account_balance_usd is not None
                        else "—"
                    ),
                ),
                HeaderStatusMetric(
                    label="本周免费额度",
                    value=self._money(
                        max(
                            0,
                            status.quota.weekly_limit_usd
                            - status.quota.weekly_used_usd,
                        )
                    ),
                ),
            ]
        else:
            daily_window_remaining = max(
                0,
                status.quota.daily_limit_usd - status.quota.daily_used_usd,
            )
            daily_remaining = daily_window_remaining
            if is_restricted:
                daily_remaining = min(
                    max(0, status.quota.remaining_usd),
                    daily_window_remaining,
                )
            metrics = [
                HeaderStatusMetric(
                    label="今日限额余量",
                    value=self._money(daily_remaining),
                ),
                HeaderStatusMetric(
                    label="本周限额余量",
                    value=self._money(
                        max(
                            0,
                            status.quota.weekly_limit_usd
                            - status.quota.weekly_used_usd,
                        )
                    ),
                ),
            ]

        metadata = [
            HeaderStatusMetric(
                label="身份",
                value=self._identity_label(status),
            ),
            HeaderStatusMetric(
                label="额度状态",
                value=self._tier_label(status),
            ),
            HeaderStatusMetric(
                label="有效期至",
                value=self._display_time(status.expires_at),
            ),
            HeaderStatusMetric(
                label="更新时间",
                value=self._display_time(measured_at),
            ),
        ]

        return HeaderStatus(
            visible=True,
            label="公益",
            value=self._money(amount),
            title="公益模型额度",
            summary=(
                f"更新失败：{status.last_error}"
                if status.last_error
                else status.ui.attribution
            ),
            summary_href=status.ui.official_url,
            tone=tone,
            metrics=metrics,
            metadata=metadata,
            actions=actions,
            refresh_after_ms=None,
            updated_at=measured_at,
        )

    @staticmethod
    def _money(value: float) -> str:
        return f"¥{value:.2f}"

    @staticmethod
    def _tier_label(status: PublicApiStatus) -> str:
        if status.access_tier == "authenticated":
            return "充值模型组" if status.balance_tier == "paid" else "免费模型组"
        return {
            "anonymous": "标准",
            "restricted": "受限",
        }.get(status.access_tier, "未知")

    @classmethod
    def _identity_label(cls, status: PublicApiStatus) -> str:
        if status.signed_in:
            suffix = (
                f" · {status.account_display_name}"
                if status.account_display_name
                else ""
            )
            return f"已登录{suffix}"
        return "匿名"

    @staticmethod
    def _display_time(value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.astimezone(_BEIJING_TIME).strftime("%m-%d %H:%M")

    @staticmethod
    def _quota(credential: dict[str, Any] | None) -> PublicApiQuota | None:
        if not credential:
            return None
        try:
            return PublicApiQuota.model_validate(credential.get("quota"))
        except (TypeError, ValueError):
            return None

    def _client_ui(self, credential: dict[str, Any] | None) -> PublicApiClientUI:
        credential_ui = PublicApiClientUI()
        try:
            if credential:
                credential_ui = PublicApiClientUI.model_validate(credential.get("ui"))
        except (TypeError, ValueError):
            pass
        runtime_ui = self._runtime_ui
        if runtime_ui is None:
            return credential_ui
        return credential_ui.model_copy(
            update={
                "service_enabled": runtime_ui.service_enabled,
                "login_enabled": runtime_ui.login_enabled,
                "payment_enabled": bool(
                    runtime_ui.payment_enabled and credential_ui.payment_url
                ),
                "header_recharge_enabled": runtime_ui.header_recharge_enabled,
                "model_page_recharge_enabled": runtime_ui.model_page_recharge_enabled,
                "provider_display_name": runtime_ui.provider_display_name,
                "attribution": runtime_ui.attribution,
                "service_notice": runtime_ui.service_notice,
                "official_url": runtime_ui.official_url,
            }
        )

    async def _remove_managed_provider(self, credential: dict[str, Any]) -> None:
        provider_id = credential.get("provider_id")
        if isinstance(provider_id, str) and provider_id:
            await self.providers.remove(provider_id)

    def _renewal_due_at(self, credential: dict[str, Any]) -> datetime:
        expires_at = self._parse_datetime(credential.get("expires_at"))
        if expires_at is None:
            return self._clock()
        lead = (
            _AUTHENTICATED_RENEWAL_LEAD
            if credential.get("access_tier") == "authenticated"
            else _ANONYMOUS_RENEWAL_LEAD
        )
        return expires_at - lead

    @staticmethod
    def _renewable_credential_id(
        credential: dict[str, Any] | None,
        signed_in: bool,
    ) -> str | None:
        if not credential or bool(credential.get("authenticated")) != signed_in:
            return None
        value = credential.get("credential_id")
        return value if isinstance(value, str) and value else None

    def _credential(self) -> dict[str, Any] | None:
        value = self.state.get("credential")
        return value if isinstance(value, dict) else None

    def _account_access_token(self) -> str | None:
        if self.account_session is None:
            return None
        value = self.account_session.access_token()
        return value if isinstance(value, str) and value else None

    def _account_signed_in(self) -> bool:
        return self._account_access_token() is not None

    def _installation_id(self) -> str:
        if self.account_session is not None:
            value = self.account_session.installation_id
            if isinstance(value, str) and value:
                return value
        return str(self.state["installation_id"])

    @staticmethod
    def _raise_account_error(exc: Exception) -> NoReturn:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            raise PortalRequestError(code, message) from exc
        raise exc

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
