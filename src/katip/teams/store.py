import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from sqlalchemy import or_, select

from katip.integrations.microsoft import meeting_url_key
from katip.store import crypto
from katip.store.db import session_scope, write_scope
from katip.store.models import Meeting, TeamsConversation
from katip.teams.activity import Activity

THREAD = re.compile(r"19:meeting_[A-Za-z0-9_+=./-]+@thread\.v2")


def thread_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"teams.microsoft.com", "teams.live.com"}:
        return None
    if not parsed.path.startswith("/l/meetup-join/"):
        return None
    path = unquote(parsed.path.removeprefix("/l/meetup-join/"))
    match = THREAD.match(path)
    if match and path[match.end() :] in {"", "/0"}:
        return match.group()
    return None


async def save_reference(mid: str, reference: dict, account_id: int | None = None):
    # Teams kişi kimlikleri anahtarsız diske yazılmaz; panel akışı çalışmaya devam eder.
    if not crypto.available():
        return None
    async with write_scope() as session:
        meeting = await session.scalar(select(Meeting).where(Meeting.id == mid).with_for_update())
        if meeting is None:
            return None
        row = await session.scalar(
            select(TeamsConversation).where(
                TeamsConversation.meeting_id == mid,
                TeamsConversation.account_id == account_id,
            )
        )
        if row is None:
            row = TeamsConversation(meeting_id=mid, account_id=account_id, reference=reference)
            session.add(row)
        else:
            row.reference = reference
        await session.flush()
        return row


async def linked_meeting(activity: Activity, account=None) -> Meeting | None:
    async with session_scope() as session:
        if activity.conversation.conversation_type == "personal":
            if account is None:
                return None
            meeting = await session.scalar(
                select(Meeting)
                .where(
                    Meeting.owner_account_id == account.id,
                )
                .order_by(Meeting.started_at.desc())
                .limit(1)
            )
        else:
            meeting = await _bound_meeting(session, activity.conversation.id)
            if (
                meeting is None
                and not activity.channel_data.meeting
                and THREAD.fullmatch(activity.conversation.id)
            ):
                candidates = await matching_meetings(conversation_id=activity.conversation.id)
                meeting = candidates[0] if len(candidates) == 1 else None
    if meeting:
        await save_reference(
            meeting.id,
            activity.reference(),
            account.id if activity.conversation.conversation_type == "personal" else None,
        )
    return meeting


async def matching_meetings(
    join_url: str = "", *, conversation_id: str | None = None, live_only: bool = False
) -> list[Meeting]:
    from katip.pipeline.session import registry

    live_ids = {s.session_id for s in registry.all()}
    since = datetime.now(UTC) - timedelta(days=2)
    async with session_scope() as session:
        meetings = await session.scalars(
            select(Meeting).where(
                Meeting.platform == "teams",
                or_(Meeting.started_at >= since, Meeting.id.in_(live_ids)),
            )
        )
        return [
            m
            for m in meetings
            if (not live_only or m.id in live_ids)
            and (
                (
                    join_url
                    and meeting_url_key(m.url or "", identity_only=True)
                    == meeting_url_key(join_url, identity_only=True)
                )
                or (conversation_id and thread_id(m.url) == conversation_id)
            )
        ]


async def _bound_meeting(session, conversation_id: str) -> Meeting | None:
    from katip.pipeline.session import registry

    live_ids = {s.session_id for s in registry.all()}
    since = datetime.now(UTC) - timedelta(days=2)
    rows = await session.execute(
        select(TeamsConversation, Meeting)
        .join(Meeting, Meeting.id == TeamsConversation.meeting_id)
        .where(
            TeamsConversation.account_id.is_(None),
            or_(Meeting.started_at >= since, Meeting.id.in_(live_ids)),
        )
        .order_by(Meeting.started_at.desc())
    )
    for conversation, meeting in rows:
        if conversation.reference.get("conversation_id") == conversation_id:
            return meeting
    return None


async def bind(mid: str, activity: Activity):
    """Toplantı sohbetini canlı bir Katip toplantısına bağlar (yetki çağıranda denetlenir)."""
    return await save_reference(mid, activity.reference(), None)


async def references(mid: str) -> list[TeamsConversation]:
    async with session_scope() as session:
        return list(
            await session.scalars(
                select(TeamsConversation).where(
                    TeamsConversation.meeting_id == mid,
                )
            )
        )


async def claim_notification(row_id: int, *, version: int = 0) -> bool:
    async with write_scope() as session:
        row = await session.scalar(
            select(TeamsConversation)
            .where(
                TeamsConversation.id == row_id,
            )
            .with_for_update()
        )
        if row is None:
            return False
        if version:
            if row.card_version >= version:
                return False
            row.card_version = version
        else:
            if row.report_notified:
                return False
            row.report_notified = True
        return True
