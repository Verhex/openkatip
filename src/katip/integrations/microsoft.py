"""Microsoft 365 takviminden toplantıya özel e-posta kimliklerini okur.

Ses hâlâ Playwright/Soniox hattından gelir. Graph kimlik kaynağıdır; davetliler
roster'da görünmeden gerçekten katılmış sayılmaz ve mail gönderilmez.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx

from katip.config import get_settings
from katip.identity import ParticipantIdentity, participant_name
from katip.logging import get_logger
from katip.store import repo

log = get_logger(__name__)
_GRAPH = "https://graph.microsoft.com/v1.0"


def configured() -> bool:
    s = get_settings()
    return bool(s.m365_tenant_id and s.m365_client_id and s.m365_client_secret and s.m365_mailbox)


def meeting_url_key(url: str, *, identity_only: bool = False) -> tuple:
    parsed = urlsplit(url)
    if (
        identity_only
        and parsed.scheme == "https"
        and parsed.hostname in {"teams.microsoft.com", "teams.live.com"}
    ):
        path = unquote(parsed.path).rstrip("/")
        if match := re.fullmatch(r"/meet/(\d+)", path):
            return ("teams", "meeting", match[1])
        if match := re.fullmatch(
            r"/l/meetup-join/(19:[A-Za-z0-9_+=./:-]+@thread\.(?:v2|tacv2))(?:/[^/]+)?", path
        ):
            return ("teams", "thread", match[1])
    return (
        parsed.hostname,
        unquote(parsed.path).rstrip("/"),
        tuple(sorted((k, v) for k, v in parse_qsl(parsed.query) if k != "context")),
    )


class MicrosoftGraph:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._token: str | None = None

    async def send_mail(self, mime: bytes, client_request_id: str) -> httpx.Response:
        token = await self._authorize()
        mailbox = quote(get_settings().m365_mailbox, safe="")
        # MIME, düz metin + HTML alternatiflerini korur; saveToSentItems varsayılanı true.
        for attempt in range(3):
            response = await self.client.post(
                f"{_GRAPH}/users/{mailbox}/sendMail",
                content=base64.b64encode(mime),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                    "client-request-id": client_request_id,
                    "return-client-request-id": "true",
                },
                timeout=20,
            )
            if response.status_code != 429 or attempt == 2:
                return response
            raw = response.headers.get("Retry-After", "1")
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return response
            if not 0 <= delay <= 30:
                return response
            await asyncio.sleep(delay)
        return response

    async def _authorize(self) -> str:
        if self._token is None:
            s = get_settings()
            tenant = quote(s.m365_tenant_id, safe="")
            response = await self.client.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={
                    "client_id": s.m365_client_id,
                    "client_secret": s.m365_client_secret,
                    "grant_type": "client_credentials",
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            response.raise_for_status()
            self._token = response.json()["access_token"]
        return self._token

    async def collection(self, path: str, params: dict | None = None) -> list[dict]:
        token = await self._authorize()
        url = _GRAPH + path
        rows = []
        for _ in range(20):
            # Graph pagination links must never forward credentials to another host.
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
                raise ValueError("Geçersiz Microsoft Graph sayfalama adresi")
            response = await self.client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": 'outlook.timezone="UTC", IdType="ImmutableId"',
                },
            )
            response.raise_for_status()
            data = response.json()
            rows.extend(data.get("value", []))
            url, params = data.get("@odata.nextLink"), None
            if not url:
                return rows
        raise ValueError("Microsoft Graph sayfalama sınırı aşıldı")

    async def invitation(self, url: str, at: datetime) -> list[ParticipantIdentity]:
        s = get_settings()
        at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
        events = await self.collection(
            f"/users/{quote(s.m365_mailbox, safe='')}/calendarView",
            {
                "startDateTime": (at - timedelta(days=1)).isoformat(),
                "endDateTime": (at + timedelta(days=1)).isoformat(),
                "$select": "id,attendees,organizer,onlineMeeting,start,isCancelled",
                "$top": "100",
            },
        )
        matching = [
            e
            for e in events
            if not e.get("isCancelled")
            and meeting_url_key((e.get("onlineMeeting") or {}).get("joinUrl", ""))
            == meeting_url_key(url)
        ]
        if not matching:
            return []

        # Yinelenen toplantıların aynı linki olabilir: en yakın oluşumu seç.
        def distance(event):
            raw = (event.get("start") or {}).get("dateTime", "")
            try:
                start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return abs((start.replace(tzinfo=UTC) if start.tzinfo is None else start) - at)
            except ValueError:
                return timedelta.max

        event = min(matching, key=distance)
        entries = [event.get("organizer") or {}, *event.get("attendees", [])]
        found = {}
        for entry in entries:
            address = entry.get("emailAddress") or {}
            email = address.get("address", "")
            with contextlib.suppress(ValueError):
                identity = ParticipantIdentity(name=address.get("name") or email, email=email)
                if identity.email != s.m365_mailbox.strip().casefold():
                    found[identity.email] = identity
        return list(found.values())


async def sync_identities(meeting_id: str) -> dict:
    if not configured():
        return {"configured": False, "imported": 0}
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        raise LookupError("Toplantı bulunamadı")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            identities = await MicrosoftGraph(client).invitation(
                meeting.url or "", meeting.started_at
            )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # URL/body/secret içeren exception metinlerini loga veya panele taşıma.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        log.warning("Microsoft 365 kimlikleri alınamadı", meeting_id=meeting_id, status=status)
        return {
            "configured": True,
            "imported": 0,
            "error": "Microsoft 365 erişimi başarısız",
            "status": status,
        }
    from katip.pipeline.session import registry

    session = registry.get(meeting_id)
    if session is not None:
        await session.add_identities(identities, "calendar")
    else:
        for identity in identities:
            await repo.save_meeting_identity(meeting_id, identity, "calendar")
        candidates = await repo.list_meeting_identities(meeting_id)
        for participant in await repo.list_participants(meeting_id):
            if participant.identity_source == "panel":
                continue
            matches = [
                i
                for i in candidates
                if participant_name(i.name) == participant_name(participant.name)
            ]
            if len(matches) == 1:
                candidate = matches[0]
                await repo.bind_participant(
                    meeting_id,
                    participant.name,
                    ParticipantIdentity(
                        name=candidate.name,
                        email=candidate.email,
                        platform_id=candidate.platform_id,
                    ),
                    candidate.source,
                )
    return {"configured": True, "imported": len(identities)}
