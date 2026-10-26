import asyncio
import time
from collections import OrderedDict
from urllib.parse import quote

import httpx

from katip.async_utils import LoopLocal
from katip.identity import normalize_email
from katip.teams.activity import Activity, service_url
from katip.teams.config import credentials

SCOPE = "https://api.botframework.com/.default"


class Connector:
    def __init__(self):
        self.token = ""
        self.expires = 0.0
        self.identity = None
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.next_tenant_send = 0.0
        self.next_chat_send = OrderedDict()

    async def access_token(self, client: httpx.AsyncClient) -> str:
        config = credentials()
        if config is None:
            raise RuntimeError("Teams kapalı")
        async with self.lock:
            if self.identity != config or time.monotonic() >= self.expires:
                response = await client.post(
                    f"https://login.microsoftonline.com/{config.tenant}/oauth2/v2.0/token",
                    data={
                        "client_id": config.app_id,
                        "client_secret": config.secret,
                        "grant_type": "client_credentials",
                        "scope": SCOPE,
                    },
                )
                response.raise_for_status()
                data = response.json()
                self.token = data["access_token"]
                self.expires = time.monotonic() + max(0, int(data["expires_in"]) - 60)
                self.identity = config
            return self.token

    async def request(self, reference: dict, method: str, path: str, body=None, *, version="v3"):
        config = credentials()
        if config is None or reference["tenant"] != config.tenant:
            raise RuntimeError("Teams kapalı")
        base = service_url(reference["service_url"]).rstrip("/")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            token = await self.access_token(client)
            response = await client.request(
                method,
                base + f"/{version}/" + path,
                headers={"Authorization": "Bearer " + token},
                json=body,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

    async def meeting_details(self, activity: Activity) -> dict:
        if not activity.channel_data.meeting:
            raise ValueError("Toplantı kimliği bulunamadı")
        return await self.request(
            activity.reference(),
            "GET",
            "meetings/" + quote(activity.channel_data.meeting.id, safe=""),
            version="v1",
        )

    async def wait_send_slot(self, reference):
        key = (reference["tenant"], reference["conversation_id"])
        async with self.send_lock:
            now = time.monotonic()
            delay = max(self.next_tenant_send, self.next_chat_send.get(key, 0)) - now
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self.next_tenant_send = now + 0.2
            self.next_chat_send.pop(key, None)
            self.next_chat_send[key] = now + 2
            while len(self.next_chat_send) > 10000:
                self.next_chat_send.popitem(last=False)

    async def member(self, activity: Activity) -> dict:
        if not activity.sender.aad_object_id:
            raise ValueError("Teams kimliği bulunamadı")
        member = await self.request(
            activity.reference(),
            "GET",
            f"conversations/{quote(activity.conversation.id, safe='')}/members/"
            + quote(activity.sender.aad_object_id, safe=""),
        )
        if member.get("objectId", member.get("aadObjectId")) != activity.sender.aad_object_id:
            raise ValueError("Teams kimliği eşleşmedi")
        return {
            "email": normalize_email(member.get("email") or member.get("userPrincipalName", ""))
        }

    async def send(
        self, reference: dict, *, text: str = "", card: dict | None = None, markdown=False
    ):
        await self.wait_send_slot(reference)
        # Toplantıdan gelen metin düz gönderilir; markdown yalnız Katip'in sabit metinlerinde.
        body = {
            "type": "message",
            "from": {"id": reference["bot_id"]},
            "conversation": {"id": reference["conversation_id"]},
            "text": text,
            "textFormat": "markdown" if markdown else "plain",
        }
        if card:
            body["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ]
        return await self.request(
            reference,
            "POST",
            f"conversations/{quote(reference['conversation_id'], safe='')}/activities",
            body,
        )


connector = LoopLocal(Connector)
