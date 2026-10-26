"""Private, allowlisted JSON-2 transport. Error bodies and credentials stay private."""

import json
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from katip_ops.config import OdooSettings
from katip_ops.secrets import read_secret


class OdooError(RuntimeError):
    """Safe to show to the owner; never contains remote exception text."""


class OdooAccessError(OdooError):
    pass


class OdooAuthenticationError(OdooError):
    pass


class OdooValidationError(OdooError):
    pass


class OdooNotFoundError(OdooError):
    pass


class OdooConnectionError(OdooError):
    pass


_METHODS = {
    "crm.lead": {"search_read", "read", "create", "write", "message_post", "activity_schedule"},
    "project.task": {"search_read", "read", "create", "write", "message_post", "activity_schedule"},
    "project.project": {"search_read"},
    "account.analytic.line": {"search_read"},
    "mail.message": {"read"},
    "mail.activity": {"read"},
    "crm.tag": {"search_read", "create"},
    "project.tags": {"search_read", "create"},
}


class OdooClient:
    def __init__(
        self, settings: OdooSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        parsed = urlsplit(settings.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid Odoo base URL")
        self.settings = settings
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, model: str, method: str, body: dict[str, JsonValue]) -> JsonValue:
        if method not in _METHODS.get(model, set()):
            raise OdooAccessError("Bu Odoo işlemine izin verilmiyor.")
        key = read_secret(self.settings.key_file)
        try:
            response = await self._http.post(
                f"{self.settings.base_url.rstrip('/')}/json/2/{model}/{method}",
                headers={"Authorization": f"bearer {key}", "X-Odoo-Database": self.settings.db},
                json=body,
            )
        except httpx.RequestError:
            raise OdooConnectionError(
                "Odoo bağlantısı kurulamadı; işlem durumunu kontrol edin."
            ) from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            data = None
        if not response.is_success or (isinstance(data, dict) and "error" in data):
            # Inspect only the type, never interpolate the server's message/debug fields.
            error = data.get("error", data) if isinstance(data, dict) else {}
            name = str(error.get("name", "")) if isinstance(error, dict) else ""
            if isinstance(error, dict) and isinstance(error.get("data"), dict):
                name = str(error["data"].get("name", name))
            if response.status_code == 401:
                raise OdooAuthenticationError("Odoo kimlik doğrulaması başarısız.")
            if response.status_code == 403 or name.endswith("AccessError"):
                raise OdooAccessError("Bu Odoo kaydına erişim yetkisi yok.")
            if response.status_code == 404 or name.endswith("MissingError"):
                raise OdooNotFoundError("Odoo kaydı bulunamadı.")
            if response.status_code in {400, 422} or name.endswith(
                ("ValidationError", "UserError")
            ):
                raise OdooValidationError("Odoo alanları doğrulanamadı.")
            raise OdooError("Odoo işlemi tamamlanamadı; işlem durumunu kontrol edin.")
        if data is None:
            raise OdooError("Odoo yanıtı okunamadı.")
        # Reject non-JSON numbers as well as malformed remote payloads.
        try:
            json.dumps(data, allow_nan=False)
        except ValueError:
            raise OdooError("Odoo yanıtı okunamadı.") from None
        return cast(JsonValue, data)
