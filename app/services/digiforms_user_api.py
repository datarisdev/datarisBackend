from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urljoin

import httpx

from app.core.config import settings


class DigiformsAPIError(RuntimeError):
    """Raised when DigiformsApp rejects or cannot complete a request."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(frozen=True)
class DigiformsUserPayload:
    user_id: str
    client_id: int
    user_name: str
    password: str
    email: str
    active: bool = True
    profile: str = "user"

    def to_api_json(self) -> Dict[str, Any]:
        return {
            "UserId": self.user_id,
            "ClientId": self.client_id,
            "UserName": self.user_name,
            "Password": self.password,
            "Email": self.email,
            "Active": self.active,
            "Profile": self.profile,
            "HasTwoFctor": False,
            "LastLogon": "",
            "UniqueId": "",
        }


class DigiformsUserAPI:
    """
    Small defensive client for the Digiforms User API.

    The PDF supplied by Digiforms has inconsistent examples around the exact URL
    (/Digiforms/Api/user versus /Digiforms/api/{clientId}/{userId}). For that
    reason this client tries the documented variants and stops on the first
    endpoint that does not return 404.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        api_user: Optional[str] = None,
        api_password: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url = str(base_url if base_url is not None else settings.DIGIFORMS_BASE_URL or "").rstrip("/")
        self.client_id = str(client_id if client_id is not None else settings.DIGIFORMS_CLIENT_ID or "").strip()
        self.api_user = str(api_user if api_user is not None else settings.DIGIFORMS_API_USER or "").strip()
        self.api_password = api_password if api_password is not None else settings.DIGIFORMS_API_PASSWORD
        self.timeout = float(timeout_seconds if timeout_seconds is not None else settings.DIGIFORMS_AUTH_TIMEOUT_SECONDS or 30)

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.client_id and self.api_user and self.api_password)

    def _auth(self) -> httpx.BasicAuth:
        if not self.is_configured:
            raise DigiformsAPIError(
                "DigiformsApp no está configurado para esta empresa. Completa ClientId, usuario técnico y contraseña desde Extensiones → DigiForms."
            )
        return httpx.BasicAuth(f"{self.client_id}/{self.api_user}", str(self.api_password))

    def _url(self, suffix: str = "") -> str:
        if not suffix:
            return self.base_url
        return f"{self.base_url}/{suffix.lstrip('/')}"

    def _candidate_get_urls(self, client_id: str, user_id: str) -> Iterable[str]:
        client_id_q = quote(str(client_id), safe="")
        user_id_q = quote(str(user_id), safe="")
        yield self._url(f"{client_id_q}/{user_id_q}")
        yield self._url(f"user/{client_id_q}/{user_id_q}")
        if not self.base_url.lower().endswith("/user"):
            yield f"{self.base_url}/user/{client_id_q}/{user_id_q}"

    def _candidate_post_urls(self) -> Iterable[str]:
        yield self.base_url
        if not self.base_url.lower().endswith("/user"):
            yield f"{self.base_url}/user"

    def _candidate_update_urls(self, client_id: str, user_id: str) -> Iterable[str]:
        yield from self._candidate_get_urls(client_id, user_id)

    async def _request_first_available(self, method: str, urls: Iterable[str], **kwargs: Any) -> Any:
        last_status: Optional[int] = None
        last_text: Optional[str] = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for url in dict.fromkeys(urls):
                response = await client.request(method, url, auth=self._auth(), **kwargs)
                last_status = response.status_code
                last_text = response.text
                if response.status_code == 404:
                    continue
                if response.status_code >= 400:
                    raise DigiformsAPIError(
                        f"DigiformsApp respondió con error HTTP {response.status_code}.",
                        status_code=response.status_code,
                        response_text=response.text,
                    )
                try:
                    return response.json()
                except Exception:
                    return {"ok": True, "raw": response.text}
        raise DigiformsAPIError(
            "No se encontró una ruta válida para Digiforms User API.",
            status_code=last_status,
            response_text=last_text,
        )

    async def get_user(self, user_id: str, client_id: Optional[str] = None) -> Any:
        client_id = str(client_id or self.client_id)
        return await self._request_first_available("GET", self._candidate_get_urls(client_id, user_id))

    async def create_user(self, payload: DigiformsUserPayload) -> Any:
        return await self._request_first_available(
            "POST",
            self._candidate_post_urls(),
            json=payload.to_api_json(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    async def update_user(self, payload: DigiformsUserPayload) -> Any:
        return await self._request_first_available(
            "PUT",
            self._candidate_update_urls(str(payload.client_id), payload.user_id),
            json=payload.to_api_json(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    async def deactivate_user(self, user_id: str, client_id: Optional[str] = None) -> Any:
        client_id = str(client_id or self.client_id)
        return await self._request_first_available("DELETE", self._candidate_update_urls(client_id, user_id))


def generate_temporary_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%*-_"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def build_digiforms_user_id(email: str, fallback_name: str = "usuario") -> str:
    base = (email.split("@", 1)[0] if email and "@" in email else fallback_name).lower()
    safe = "".join(ch if ch.isalnum() else "_" for ch in base).strip("_") or "usuario"
    return f"dt_{safe[:22]}_{secrets.token_hex(3)}"
