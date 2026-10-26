from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReadModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", populate_by_name=True)


class Account(ReadModel):
    id: str = Field(min_length=1, max_length=512)
    aad_object_id: str | None = Field(None, alias="aadObjectId", max_length=36)

    @field_validator("aad_object_id")
    @classmethod
    def aad_guid(cls, value):
        return str(UUID(value)) if value else value


class Conversation(ReadModel):
    id: str = Field(min_length=1, max_length=1024)
    conversation_type: Literal["personal", "groupChat", "channel"] = Field(alias="conversationType")
    tenant_id: str | None = Field(None, alias="tenantId", max_length=36)


class Tenant(ReadModel):
    id: str = Field(min_length=36, max_length=36)


class MeetingData(ReadModel):
    id: str = Field(max_length=1024)


class ChannelData(ReadModel):
    tenant: Tenant
    meeting: MeetingData | None = None


class Mention(ReadModel):
    type: str = Field(max_length=32)
    mentioned: Account | None = None
    text: str | None = Field(None, max_length=512)


class CardData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    meeting_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=32, max_length=32)
    version: int = Field(ge=1)


class ExecuteAction(ReadModel):
    type: Literal["Action.Execute"]
    verb: Literal["mail.send"]
    data: CardData


class InvokeValue(ReadModel):
    action: ExecuteAction


def service_url(value: str) -> str:
    url = urlsplit(value)
    host = url.hostname or ""
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.query
        or url.fragment
        or not (host == "smba.trafficmanager.net" or host.endswith(".botapi.skype.com"))
        or ".." in url.path
    ):
        raise ValueError("Geçersiz Teams hizmet adresi")
    return value


class Activity(ReadModel):
    type: Literal["message", "conversationUpdate", "installationUpdate", "invoke"]
    id: str = Field(min_length=1, max_length=512)
    channel_id: Literal["msteams"] = Field(alias="channelId")
    service_url: str = Field(alias="serviceUrl", max_length=2048)
    conversation: Conversation
    sender: Account = Field(alias="from")
    recipient: Account
    channel_data: ChannelData = Field(alias="channelData")
    text: str = Field("", max_length=16000)
    entities: list[Mention] = Field(default_factory=list, max_length=100)
    name: str | None = Field(None, max_length=100)
    value: InvokeValue | None = None
    action: str | None = Field(None, max_length=32)

    @field_validator("service_url")
    @classmethod
    def validate_service(cls, value):
        return service_url(value)

    def reference(self) -> dict:
        return {
            "service_url": self.service_url,
            "conversation_id": self.conversation.id,
            "tenant": self.channel_data.tenant.id,
            "bot_id": self.recipient.id,
        }

    def command_text(self) -> str | None:
        text = self.text
        mentioned = False
        for entity in self.entities:
            if (
                entity.type == "mention"
                and entity.mentioned
                and entity.mentioned.id == self.recipient.id
                and entity.text
                and entity.text in text
            ):
                text = text.replace(entity.text, "")
                mentioned = True
        if self.conversation.conversation_type != "personal" and not mentioned:
            return None
        return text.strip()
