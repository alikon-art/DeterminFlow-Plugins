"""Legacy browser-auth endpoints for pre-account-service Core clients."""

from typing import Any
from urllib.parse import urlencode

from .portal import PortalRequestError, PublicApiPortalClient as _PortalBase


class LegacyPortalClient(_PortalBase):
    def authorization_url(
        self,
        *,
        installation_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": "determinflow-public-api",
                "installation_id": installation_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        return f"{self.base_url}/desktop-authorize.html?{query}"


    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, str]:
        body = await self._request(
            "POST",
            "/api/desktop-auth/token",
            payload={
                "grant_type": "authorization_code",
                "client_id": "determinflow-public-api",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
        return self._parse_tokens(body)


    async def refresh(self, refresh_token: str) -> dict[str, str]:
        body = await self._request(
            "POST",
            "/api/desktop-auth/refresh",
            payload={"refresh_token": refresh_token},
        )
        return self._parse_tokens(body)


    async def logout(self, refresh_token: str) -> None:
        await self._request(
            "POST",
            "/api/desktop-auth/logout",
            payload={"refresh_token": refresh_token},
        )


    @staticmethod
    def _parse_tokens(body: dict[str, Any]) -> dict[str, str]:
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise PortalRequestError("invalid_response", "笔枢登录响应缺少访问令牌")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise PortalRequestError("invalid_response", "笔枢登录响应缺少续期令牌")
        return {"access_token": access_token, "refresh_token": refresh_token}
