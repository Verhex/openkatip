import asyncio
from urllib.parse import quote
from uuid import UUID

from fastapi import HTTPException

from katip import usage
from katip.agent.commands import Command, Intent, Level, normalize
from katip.api.routes import meetings as meeting_routes
from katip.api.security import PanelAccess, absolute_url, view_link_params
from katip.async_utils import LoopLocal
from katip.bot.platforms.teams import MicrosoftTeams
from katip.config import get_settings
from katip.identity import normalize_email
from katip.integrations import mail
from katip.logging import get_logger
from katip.pipeline.session import registry
from katip.store import crypto, repo
from katip.teams import cards, store
from katip.teams.activity import Activity
from katip.teams.client import connector
from katip.teams.config import credentials

log = get_logger(__name__)
_binding_lock = LoopLocal(asyncio.Lock)
DETAILS_UNAVAILABLE = (
    "Toplantı bilgileri alınamadı. OnlineMeeting.ReadBasic.Chat izni için Katip'in "
    "1.0.1 paketini yükleyin; uygulamayı bu toplantıdan kaldırıp yeniden ekleyin ve "
    "izin isteğini onaylayın. Ardından tekrar deneyin."
)
HELP = (
    "Beni **@Katip** diye çağırın:\n\n"
    "- **katıl**: yetkili panel hesabıyla toplantıya çağırır\n"
    "- **bağla**: otomatik bağlama yapılamazsa sohbeti bağlar\n"
    "- **durum** · **özetle** · **rapor**\n"
    "- **not al: …** · **karar: …** · **aksiyon: …**\n\n"
    "Örnek: @Katip karar: Bütçe taslağı cuma günü onaylanacak"
)
HELP_PERSONAL = (
    "Merhaba, ben Katip. Toplantılarda konuşmaları yazıya döker, not tutar ve toplantı "
    "bitince rapor hazırlarım.\n\n"
    "Toplantıya katılmam için toplantı sohbetinde **@Katip katıl** yazın veya "
    "yönetim panelinden toplantı bağlantısını gönderin. Sonra "
    "toplantı sohbetinde beni **@Katip** diye çağırın:\n\n"
    "- **durum**: toplantıda olup olmadığımı söyler\n"
    "- **özetle**: şu ana kadar konuşulanları özetler\n"
    "- **not al: …**, **karar: …**, **aksiyon: …**: toplantı kaydına ekler\n"
    "- **rapor**: hazır olunca raporun bağlantısını verir\n\n"
    "Toplantı bitince rapor bağlantısını ve, toplantı notu maili açıksa, onay kartını "
    "size buradan iletirim."
)
UNLINKED = (
    "Bu sohbet henüz bir Katip toplantısına bağlı değil. Yetkili panel hesabıyla "
    "@Katip katıl yazabilirsiniz. Katip toplantıdayken, toplantıyı "
    "panelden gönderen kişi bu sohbete @Katip bağla yazsın."
)


def command(text: str) -> Command | str:
    normalized = normalize(text.strip().lstrip("/"))
    simple = {"yardım": Intent.HELP, "özetle": Intent.SUMMARY}
    if normalized in {"durum", "rapor", "bağla", "katıl"}:
        return normalized
    if normalized in simple:
        return Command(simple[normalized], text, "teams", text)
    for prefix, intent in (
        ("not al:", Intent.NOTE),
        ("karar:", Intent.DECISION),
        ("aksiyon:", Intent.ACTION),
    ):
        if text.replace("İ", "i").replace("I", "ı").lower().startswith(prefix):
            return Command(intent, text[len(prefix) :].strip(), "teams", text)
    return Command(Intent.HELP, "", "teams", text)


async def dispatch(activity: Activity) -> dict | None:
    if activity.type == "invoke":
        return await cards.invoke(activity)
    if activity.type == "installationUpdate" and activity.action == "remove":
        return None
    text = activity.command_text() if activity.type == "message" else None
    account = None
    author = None
    if text is not None and activity.sender.aad_object_id:
        try:
            member = await connector.get().member(activity)
            author = member["email"]
            account = await repo.find_panel_account(author)
            if account and not account.enabled:
                account = None
        except Exception as exc:
            log.warning("Teams kimliği doğrulanamadı", error_type=type(exc).__name__)
    meeting, details = await resolve_meeting(activity, account)
    if activity.type == "message" and text is None:
        return None
    if activity.type != "message":
        if meeting and meeting.report:
            await report_ready(meeting.id)
        return None
    cmd = command(text)
    if isinstance(cmd, Command) and cmd.intent == Intent.HELP:
        personal = activity.conversation.conversation_type == "personal"
        reply = HELP_PERSONAL if personal else HELP
    elif cmd == "bağla":
        reply = await bind(activity, account, meeting)
    elif cmd == "katıl":
        reply = await join(activity, account, meeting, details)
    elif meeting is None:
        reply = UNLINKED
    else:
        live = registry.get(meeting.id)
        if cmd == "durum":
            reply = (
                f"Katip bu toplantıda. Mod: {'sessiz kayıt' if live.mode == 'kayit' else 'tam'}."
                if live
                else "Katip şu anda bu toplantıda değil."
            )
        elif cmd == "rapor":
            reply = (
                "Toplantı raporu hazır: " + report_link(meeting.id) + " adresinde."
                if meeting.report
                else "Toplantı raporu henüz hazır değil."
            )
        elif live is None:
            reply = "Bu komut için bağlı toplantının canlı olması gerekiyor."
        elif live.paused:
            reply = "Toplantı kayıt dışı; içerik komutları duraklatıldı."
        elif not author:
            reply = "Teams katılımcı kimliği doğrulanamadı."
        elif activity.conversation.conversation_type == "personal" and (
            not account or not await PanelAccess(account.id, account.role).allows(meeting.id)
        ):
            reply = "Bu toplantıya erişiminiz yok."
        elif isinstance(cmd, Command) and cmd.level == Level.PUBLIC:
            if cmd.intent == Intent.SUMMARY:
                if not live.agent._take_llm_budget():
                    reply = "Bu toplantı için soru limitine ulaşıldı."
                else:
                    with usage.meeting(meeting.id):
                        reply = await live.agent._llm.summarize_so_far(
                            transcript=live.transcript_text(),
                            notes=live.agent.notes,
                            directives=await repo.list_directives(),
                            company=get_settings().company_name,
                        )
            elif not cmd.text:
                reply = "Kaydedilecek metni komuttan sonra yazın."
            else:
                kind = {Intent.NOTE: "note", Intent.DECISION: "decision", Intent.ACTION: "action"}[
                    cmd.intent
                ]
                await live.agent._save_note(kind, cmd.text, author)
                reply = {
                    "note": "Not alındı.",
                    "decision": "Karar kaydedildi.",
                    "action": "Aksiyon eklendi.",
                }[kind]
        else:
            reply = HELP
    await connector.get().send(
        activity.reference(), text=reply, markdown=reply in (HELP, HELP_PERSONAL)
    )
    return None


async def get_meeting_details(activity: Activity) -> dict | None:
    try:
        data = await connector.get().meeting_details(activity)
        url = data["details"]["joinUrl"].strip()
        if not MicrosoftTeams.matches(url) or len(url) > 4096:
            raise ValueError("Geçersiz Teams toplantı bağlantısı")
        if (
            data.get("conversation", {}).get("id", activity.conversation.id)
            != activity.conversation.id
        ):
            raise ValueError("Toplantı sohbeti eşleşmedi")
        data["details"]["joinUrl"] = url
        return data
    except Exception as exc:
        log.warning("Teams toplantı bilgileri alınamadı", error_type=type(exc).__name__)
        return None


async def resolve_meeting(activity: Activity, account):
    async with _binding_lock.get():
        meeting = await store.linked_meeting(activity, account)
        if (
            meeting is not None
            or activity.conversation.conversation_type != "groupChat"
            or not activity.channel_data.meeting
        ):
            return meeting, None
        details = await get_meeting_details(activity)
        if details:
            candidates = await store.matching_meetings(
                details["details"]["joinUrl"], conversation_id=activity.conversation.id
            )
            if len(candidates) == 1 and await store.bind(candidates[0].id, activity):
                meeting = candidates[0]
                await connector.get().send(
                    activity.reference(), text="Katip bu toplantı sohbetine bağlandı."
                )
        return meeting, details or {}


async def join(activity: Activity, account, meeting, details) -> str:
    if activity.conversation.conversation_type != "groupChat" or not activity.channel_data.meeting:
        return "Katılma komutu toplantı sohbetinde kullanılır: @Katip katıl yazın."
    if not account or account.role not in {"operator", "admin"} or not account.enabled:
        return "Katip'i çağırmak için açık bir operatör veya yönetici panel hesabınız olmalı."
    if meeting and registry.get(meeting.id):
        return "Katip bu toplantıda zaten canlı; yeniden katılamaz."
    if details is None:
        details = await get_meeting_details(activity)
    if not details:
        return DETAILS_UNAVAILABLE
    if not crypto.available():
        return "Sohbet bağlanamadı: sunucuda veri şifreleme anahtarı tanımlı olmalı."

    async def bind_before_launch(mid):
        if await store.bind(mid, activity) is None:
            raise RuntimeError("Sohbet bağlanamadı")

    try:
        await meeting_routes.start_meeting(
            meeting_routes.JoinRequest(
                url=details["details"]["joinUrl"],
                title=str(details["details"].get("title") or "")[:200],
            ),
            owner_account_id=account.id,
            before_launch=bind_before_launch,
            reject_duplicate=True,
            conversation_id=activity.conversation.id,
        )
    except HTTPException as exc:
        if exc.status_code in {409, 429}:
            return "Katip katılamadı: " + str(exc.detail)
        return "Katip toplantıya katılamadı; bağlantıyı ve sunucu ayarlarını panelden kontrol edin."
    return "Katip toplantıya katılıyor; lobiden kabul edin."


async def bind(activity: Activity, account, meeting) -> str:
    if activity.conversation.conversation_type == "personal":
        return "Bağlama toplantı sohbetinde yapılır: toplantı sohbetinde @Katip bağla yazın."
    if meeting is not None:
        return f"Bu sohbet zaten bağlı: {meeting.title or 'Toplantı'}."
    if account is None:
        return "Bağlamak için Katip panelinde hesabınız olmalı."
    access = PanelAccess(account.id, account.role)
    # Yalnız çağıranın yönetebildiği, şu anda canlı Teams toplantıları adaydır.
    candidates = []
    for live in registry.all():
        row = await repo.get_meeting(live.session_id)
        if row and row.platform == "teams" and await access.allows(live.session_id, "manage"):
            candidates.append(live)
    if not candidates:
        return (
            "Yönettiğiniz canlı bir Teams toplantısı yok. "
            "Önce Katip'i panelden toplantıya gönderin."
        )
    if len(candidates) > 1:
        return "Yönettiğiniz birden çok canlı toplantı var; bu aşamada otomatik bağlanamıyor."
    mid = candidates[0].session_id
    if await store.bind(mid, activity) is None:
        return "Sohbet bağlanamadı: sunucuda veri şifreleme anahtarı tanımlı olmalı."
    title = (await repo.get_meeting(mid)).title or "Toplantı"
    return f"Bu sohbet bağlandı: {title}. Artık @Katip komutlarını kullanabilirsiniz."


def report_link(mid: str) -> str:
    return absolute_url(f"/panel/{quote(mid, safe='')}?{view_link_params(mid)}")


async def _owner_reference(meeting, group):
    owner = await repo.get_panel_account(meeting.owner_account_id)
    if not owner or not owner.enabled or owner.role not in {"admin", "operator"}:
        return None
    connection = connector.get()
    page = await connection.request(
        group.reference,
        "GET",
        f"conversations/{quote(group.reference['conversation_id'], safe='')}/members",
    )
    for member in page if isinstance(page, list) else []:
        try:
            email = normalize_email(member.get("email") or member.get("userPrincipalName", ""))
        except ValueError:
            continue
        if email == owner.email and (member.get("aadObjectId") or member.get("objectId")):
            aad_id = str(UUID(member.get("aadObjectId") or member["objectId"]))
            # Kurulu uygulaması olmayan kullanıcıda Teams 403 döner; Graph ile yükleme yapılmaz.
            created = await connection.request(
                group.reference,
                "POST",
                "conversations",
                {
                    "bot": {"id": group.reference["bot_id"]},
                    "members": [{"id": aad_id}],
                    "isGroup": False,
                    "tenantId": group.reference["tenant"],
                    "channelData": {"tenant": {"id": group.reference["tenant"]}},
                },
            )
            return await store.save_reference(
                meeting.id,
                {
                    **group.reference,
                    "conversation_id": created["id"],
                },
                owner.id,
            )
    return None


async def report_ready(mid: str):
    if not credentials() or not get_settings().teams_notify:
        return
    try:
        meeting = await repo.get_meeting(mid)
        if not meeting or not meeting.report:
            return
        refs = await store.references(mid)
        group = next((r for r in refs if r.account_id is None), None)
        if group and await store.claim_notification(group.id):
            await connector.get().send(
                group.reference,
                text=f"Rapor hazır: {meeting.title or 'Toplantı'}\n{report_link(mid)} adresinde.",
            )
        if not meeting.owner_account_id or mail.disabled_reason():
            return
        personal = next((r for r in refs if r.account_id == meeting.owner_account_id), None)
        if personal is None and group:
            personal = await _owner_reference(meeting, group)
        if personal:
            owner = await repo.get_panel_account(meeting.owner_account_id)
            if not owner or not owner.enabled:
                return
            access = PanelAccess(owner.id, owner.role)
            if not await access.allows(mid, "manage"):
                return
            draft = await mail.get_draft(mid, access)
            if await store.claim_notification(personal.id, version=draft["version"]):
                await connector.get().send(personal.reference, card=cards.approval_card(mid, draft))
    except (HTTPException, ValueError, RuntimeError, KeyError, OSError) as exc:
        log.warning("Teams bildirimi gönderilemedi", error_type=type(exc).__name__)
    except Exception as exc:
        log.warning("Teams bildirimi gönderilemedi", error_type=type(exc).__name__)
