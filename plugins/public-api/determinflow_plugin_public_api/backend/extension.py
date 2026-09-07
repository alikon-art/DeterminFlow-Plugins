"""Extension entrypoint for the optional public API Plugin."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from src.extension_api import ExtensionManifest

from .catalog import PublicModelCatalogClient
from .legacy_portal import LegacyPortalClient
from .legacy_service import LegacyCredentialService
from .portal import PublicApiPortalClient
from .provider import CoreProviderGateway
from .routes import build_router
from .service import PublicApiCredentialService

_DEFAULT_PORTAL_BASE_URL = "https://bishuxiezuo.cn"
_DEVELOPMENT_OVERRIDE_ENV = "DETERMINFLOW_PUBLIC_API_DEVELOPMENT"


def _runtime_access() -> tuple[bool, str, str | None]:
    supported_desktop = (
        os.getenv("DETERMINFLOW_DESKTOP") == "1" and sys.platform in {"win32", "darwin"}
    )
    if supported_desktop:
        return True, "stable", None
    if os.getenv(_DEVELOPMENT_OVERRIDE_ENV) == "1":
        return True, "development", None
    return False, "stable", "仅支持 Windows 和 macOS 桌面版"


class PublicApiExtension:
    manifest = ExtensionManifest(
        extension_id="public-api",
        name="笔枢公益模型",
        version="0.1.36",
    )

    def __init__(self) -> None:
        self._service: PublicApiCredentialService | LegacyCredentialService | None = None
        self._router = build_router(self._get_service)

    @property
    def service(self) -> PublicApiCredentialService | LegacyCredentialService | None:
        return self._service

    def _get_service(self) -> PublicApiCredentialService | LegacyCredentialService:
        if self._service is None:
            raise HTTPException(status_code=503, detail="公益模型 Plugin 尚未启动")
        return self._service

    def register(self, registrar: Any) -> None:
        self.manifest = registrar.manifest
        registrar.add_router(self._router)

    async def start(self, runtime: Any) -> None:
        config = runtime.get_service("plugin_config", {})
        data_dir = Path(runtime.get_service("plugin_data_dir"))
        runtime_allowed, release_channel, disabled_reason = _runtime_access()
        account_session = runtime.get_service("account_session")
        portal_type = (
            PublicApiPortalClient if account_session is not None else LegacyPortalClient
        )
        portal = None
        if runtime_allowed:
            portal_url = str(config.get("PORTAL_BASE_URL", _DEFAULT_PORTAL_BASE_URL))
            try:
                portal = portal_type(
                    portal_url,
                    app_version=self.manifest.version,
                    allow_loopback_http=release_channel == "development",
                )
            except ValueError as exc:
                disabled_reason = str(exc)
        service_type = (
            PublicApiCredentialService
            if account_session is not None else LegacyCredentialService
        )
        account_options = (
            {"account_session": account_session} if account_session is not None else {}
        )
        self._service = service_type(
            data_dir,
            app_version=self.manifest.version,
            release_channel=release_channel,
            platform_name="macos" if sys.platform == "darwin" else "windows",
            portal=portal,
            catalog=PublicModelCatalogClient(
                app_version=self.manifest.version,
            ),
            providers=CoreProviderGateway(
                runtime.app,
                owner=runtime.resource_owner,
            ),
            **account_options,
            disabled_reason=disabled_reason,
        )
        await self._service.start()

    async def stop(self) -> None:
        if self._service is not None:
            await self._service.stop()
            self._service = None


def create_extension() -> PublicApiExtension:
    return PublicApiExtension()
