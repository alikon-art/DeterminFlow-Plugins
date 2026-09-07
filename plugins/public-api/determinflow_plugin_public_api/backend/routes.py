"""HTTP routes exposed by the optional public API Plugin."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from .models import PublicApiStatus
from .portal import PortalRequestError
from .service import PublicApiCredentialService


def build_router(
    get_service: Callable[[], PublicApiCredentialService],
) -> APIRouter:
    router = APIRouter(prefix="/api/public-api", tags=["public-api-plugin"])

    @router.get("/status", response_model=PublicApiStatus)
    async def get_status() -> PublicApiStatus:
        return await get_service().refresh_client_config()

    @router.post("/renew", response_model=PublicApiStatus)
    async def renew() -> PublicApiStatus:
        return await get_service().ensure_credential(force=True)

    @router.post("/login", response_model=PublicApiStatus)
    async def login() -> PublicApiStatus:
        try:
            return await get_service().login_account()
        except PortalRequestError as exc:
            raise HTTPException(status_code=503, detail=exc.message) from exc

    @router.delete("/login", response_model=PublicApiStatus)
    async def logout() -> PublicApiStatus:
        try:
            return await get_service().logout_account()
        except PortalRequestError as exc:
            raise HTTPException(status_code=503, detail=exc.message) from exc

    return router
