"""Socket Mode bridge with owner checks at every entry and a single posting gate."""

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import JsonValue

if TYPE_CHECKING:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

from katip_ops.config import Settings
from katip_ops.core.approvals import Approval, Approvals
from katip_ops.core.contract import ProposedAction
from katip_ops.secrets import read_secret
from katip_ops.slack import blocks
from katip_ops.slack.commands import REFUSAL, Commands

logger = logging.getLogger(__name__)
Callback = Callable[..., Awaitable[Any]]


class Applier(Protocol):
    async def apply(self, action: ProposedAction, *, approval_id: str) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class Card:
    channel: str
    ts: str
    digest: str
    message: dict[str, Any]


class SlackBridge:
    def __init__(
        self,
        settings: Settings,
        approvals: Approvals,
        commands: Commands,
        applier: Applier,
        *,
        client: Any,
    ) -> None:
        if (
            approvals.owner_slack_id != settings.owner_slack_id
            or commands.settings.owner_slack_id != settings.owner_slack_id
        ):
            raise ValueError("Slack and approval owners must match")
        self.settings = settings
        self.approvals = approvals
        self.commands = commands
        self.applier = applier
        self._client = client
        self._owner_dm: str | None = None
        self._cards: dict[str, Card] = {}
        self.app: AsyncApp | None = None
        self.handler: AsyncSocketModeHandler | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, approvals: Approvals, commands: Commands, applier: Applier
    ) -> "SlackBridge":
        # Constructing these classes performs no network I/O. Only start connects.
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
            from slack_sdk.web.async_client import AsyncWebClient
        except ModuleNotFoundError:
            raise RuntimeError(
                "Slack başlatılamadı; async SDK bağımlılıkları (aiohttp) eksik."
            ) from None
        bot_token = read_secret(settings.slack.slack_bot_token)
        app_token = read_secret(settings.slack.slack_app_token)
        proxy = settings.slack.proxy or next(
            (
                os.environ[key]
                for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                if os.environ.get(key)
            ),
            None,
        )
        client = AsyncWebClient(token=bot_token, proxy=proxy)
        bridge = cls(settings, approvals, commands, applier, client=client)
        bridge.app = AsyncApp(
            client=client, user_facing_authorize_error_message="Slack yetkisi doğrulanamadı."
        )
        bridge.app.command("/katip")(bridge.on_command)
        bridge.app.event("message")(bridge.on_message)
        for action in ("approve", "edit", "reject"):
            bridge.app.action(f"katip_{action}")(bridge.on_action)
        bridge.app.view("katip_edit_submit")(bridge.on_edit_submit)
        bridge.handler = AsyncSocketModeHandler(
            bridge.app, app_token=app_token, web_client=client, proxy=proxy
        )
        return bridge

    async def start(self) -> None:
        if self.handler is None:
            raise RuntimeError("Socket Mode is not configured")
        await cast(Callable[[], Awaitable[None]], self.handler.start_async)()

    async def close(self) -> None:
        if self.handler is not None:
            await cast(Callable[[], Awaitable[None]], self.handler.close_async)()

    async def owner_dm(self) -> str:
        if self._owner_dm is None:
            response = await self._client.conversations_open(users=self.settings.owner_slack_id)
            channel = response["channel"]["id"]
            if not isinstance(channel, str) or not channel.startswith("D"):
                raise PermissionError("Yetkili kullanıcının özel kanalı doğrulanamadı.")
            self._owner_dm = channel
        return self._owner_dm

    async def _check_channel(self, channel: str) -> None:
        if channel in self.settings.slack.channels.values():
            return
        if channel.startswith("D") and channel == await self.owner_dm():
            return
        logger.warning("slack_channel_refused")
        raise PermissionError("Bu kanala mesaj gönderilmesine izin verilmiyor.")

    async def post(self, channel: str, message: dict[str, Any]) -> Any:
        await self._check_channel(channel)
        if set(message) - {"text", "blocks"}:
            raise ValueError("Geçersiz mesaj alanları.")
        return await self._client.chat_postMessage(
            channel=channel, **message, unfurl_links=False, unfurl_media=False
        )

    async def send_approval(
        self,
        approval_id: str,
        *,
        title: str,
        body: str,
        opinion_check: bool = False,
        channel_key: str = "ops_approvals",
    ) -> Any:
        approval = self._pending(approval_id)
        if channel_key not in {"ops_approvals", "social"}:
            raise ValueError("Onay kanalı doğrulanamadı.")
        channel = self.settings.slack.channels[channel_key]  # type: ignore[index]
        message = blocks.approval_card(
            approval_id, title, body, approval.action.reason, opinion_check=opinion_check
        )
        response = await self.post(channel, message)
        self._cards[approval_id] = Card(channel, str(response["ts"]), approval.digest, message)
        return response

    def _pending(self, approval_id: str) -> Approval:
        approval = self.approvals.get(approval_id)
        if approval.status != "pending":
            raise ValueError("Bu onay daha önce kullanıldı.")
        if self.approvals.clock() >= approval.expires_at:
            raise ValueError("Onayın süresi doldu.")
        return approval

    async def on_command(self, ack: Callback, body: dict[str, Any], respond: Callback) -> None:
        user = str(body.get("user_id", ""))
        if not self.commands.is_owner(user):
            await ack(text=REFUSAL, response_type="ephemeral")
            return
        await ack()
        try:
            await self._check_channel(str(body.get("channel_id", "")))
            result = await self.commands.execute(user, str(body.get("text", "")))
        except (ValueError, PermissionError):
            # Send errors only after the posting gate has accepted this channel.
            await self.post(
                await self.owner_dm(),
                {"text": "Komut tamamlanamadı; kanalı ve girdiyi kontrol edin."},
            )
            return
        await respond(text=blocks.escape(result), response_type="ephemeral")

    async def on_message(self, event: dict[str, Any]) -> None:
        # Ignore bot messages, edits, deletes and channel chatter. There is no chat agent.
        # Messages carrying an app_id were posted by an app on the owner's behalf (e.g. Cursor/Pofi)
        # and are untrusted data, never owner commands (PROTOCOL.md, D-040).
        if (
            event.get("bot_id")
            or event.get("app_id")
            or event.get("subtype")
            or event.get("channel_type") != "im"
        ):
            return
        user = str(event.get("user", ""))
        if not self.commands.is_owner(user):
            # Events have no response_url. A DM refusal would violate the owner-DM-only gate.
            return
        channel = str(event.get("channel", ""))
        await self._check_channel(channel)
        try:
            text = await self.commands.execute(user, str(event.get("text", "")))
        except ValueError:
            text = "Komut tamamlanamadı; girdiyi kontrol edin."
        await self.post(channel, {"text": blocks.escape(text)})

    def _card(self, approval_id: str, channel: str, ts: str) -> Card:
        card = self._cards.get(approval_id)
        if card is None or (card.channel, card.ts) != (channel, ts):
            raise ValueError("Onay kartı doğrulanamadı; kartı yeniden gönderin.")
        if card.digest != self.approvals.get(approval_id).digest:
            raise ValueError("İşlem özeti onay kartıyla eşleşmiyor.")
        return card

    async def on_action(self, ack: Callback, body: dict[str, Any], respond: Callback) -> None:
        user = str(body.get("user", {}).get("id", ""))
        if not self.commands.is_owner(user):
            await ack()
            await respond(text=REFUSAL, response_type="ephemeral", replace_original=False)
            return
        await ack()
        try:
            action = body["actions"][0]
            approval_id = str(action["value"])
            channel, ts = str(body["channel"]["id"]), str(body["message"]["ts"])
            await self._check_channel(channel)
            approval = self._pending(approval_id)
            card = self._card(approval_id, channel, ts)
            action_id = action["action_id"]
            if action_id == "katip_edit":
                field, text = self._edit_text(approval.action)
                await self._client.views_open(
                    trigger_id=body["trigger_id"],
                    view=blocks.edit_modal(
                        approval_id, text, channel=channel, ts=ts, digest=card.digest, field=field
                    ),
                )
                return
            if action_id not in {"katip_approve", "katip_reject"}:
                raise ValueError("Geçersiz onay işlemi.")
            await self._decide(approval_id, user, action_id == "katip_approve", card)
        except (ValueError, PermissionError) as error:
            # Only locally generated Turkish validation messages are shown.
            text = (
                str(error)
                if type(error) in {ValueError, PermissionError}
                else "İşlem doğrulanamadı."
            )
            await respond(text=text, response_type="ephemeral", replace_original=False)

    @staticmethod
    def _edit_text(action: ProposedAction) -> tuple[str, str]:
        values = action.payload.get("values")
        if isinstance(values, dict):
            for field in ("description", "body", "note", "summary", "name"):
                if isinstance(values.get(field), str):
                    return field, cast(str, values[field])
        return "", json.dumps(action.payload, ensure_ascii=False, indent=2)

    async def on_edit_submit(self, ack: Callback, body: dict[str, Any]) -> None:
        user = str(body.get("user", {}).get("id", ""))
        if not self.commands.is_owner(user):
            await ack(response_action="errors", errors={"edit": REFUSAL})
            return
        try:
            view = body["view"]
            metadata = json.loads(view["private_metadata"])
            approval_id = metadata["approval_id"]
            await self._check_channel(metadata["channel"])
            approval = self._pending(approval_id)
            card = self._card(approval_id, metadata["channel"], metadata["ts"])
            if metadata["digest"] != approval.digest:
                raise ValueError("İşlem özeti onayla eşleşmiyor.")
            field, _ = self._edit_text(approval.action)
            if field != metadata["field"]:
                raise ValueError("Düzenlenecek alan doğrulanamadı.")
            text = view["state"]["values"]["edit"]["text"]["value"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("İşlem metni boş olamaz.")
            payload = json.loads(json.dumps(approval.action.payload))
            if field:
                payload["values"][field] = text
            else:
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("İşlem bir JSON nesnesi olmalı.")
        except (ValueError, PermissionError, KeyError, TypeError):
            await ack(
                response_action="errors",
                errors={"edit": "Düzenleme doğrulanamadı; onayın süresini ve metni kontrol edin."},
            )
            return
        # Acknowledge before remote application; Slack gives modals a short deadline.
        await ack()
        try:
            await self._decide(approval_id, user, True, card, edited_payload=payload)
        except (ValueError, PermissionError):
            await self.post(
                await self.owner_dm(),
                {"text": "Düzenlenmiş onay işlenemedi; onay kaydını kontrol edin."},
            )

    async def _decide(
        self,
        approval_id: str,
        user: str,
        approve: bool,
        card: Card,
        *,
        edited_payload: dict[str, JsonValue] | None = None,
    ) -> None:
        decision = self.approvals.decision(
            approval_id, "approved" if approve else "rejected", user, edited_payload=edited_payload
        )
        result = "Reddedildi"
        if approve:
            try:
                applied = await self.applier.apply(decision.action, approval_id=approval_id)
                if applied.get("dry_run") is True:
                    result = "Onaylandı · Deneme modu; işlem gönderilmedi"
                else:
                    self.approvals.mark_applied(approval_id)
                    result = "Onaylandı ve uygulandı"
                    if decision.action.kind == "file.write" and decision.action.target.startswith(
                        "content/approved/"
                    ):
                        result = "Onaylandı · Paylaşıma hazır (ajan otomatik paylaşmaz)"
                        approved_text = decision.action.payload.get("values", {})
                        body = (
                            approved_text.get("body") if isinstance(approved_text, dict) else None
                        )
                        if isinstance(body, str) and body.strip():
                            await self._client.chat_postMessage(
                                channel=card.channel,
                                thread_ts=card.ts,
                                text=(
                                    "Onaylanan metin aşağıda. Kopyalayıp kendiniz paylaşın; "
                                    "Katip Ops hiçbir yerde otomatik paylaşım yapmaz.\n"
                                    "```" + body.replace("```", "'''") + "```"
                                ),
                                unfurl_links=False,
                                unfurl_media=False,
                            )
            except Exception:
                # The external write may have partially succeeded. Never auto-retry it.
                logger.error(
                    "slack_approval_application_failed", extra={"approval_id": approval_id}
                )
                result = "Onaylandı · Uygulama tamamlanamadı; yeniden denemeden kontrol edin"
        message = blocks.decision_message(card.message["blocks"], result, user)
        await self._check_channel(card.channel)
        await self._client.chat_update(channel=card.channel, ts=card.ts, **message)
