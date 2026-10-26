from urllib.parse import quote

from fastapi import HTTPException

from katip.api.security import PanelAccess, absolute_url
from katip.identity import company_email
from katip.integrations import mail
from katip.store import repo
from katip.teams.activity import Activity
from katip.teams.client import connector


def approval_card(mid: str, draft: dict, *, error: str = "") -> dict:
    recipients = draft.get("recipients", [])
    external = [email for email in recipients if not company_email(email, mail.internal_domains())]
    body = [
        {"type": "TextBlock", "text": "Toplantı notu maili", "weight": "Bolder", "wrap": True},
        {"type": "TextBlock", "text": draft.get("subject", "Mail onayı"), "wrap": True},
        {"type": "TextBlock", "text": f"Alıcı sayısı: {len(recipients)}", "wrap": True},
        {
            "type": "TextBlock",
            "text": f"Dış alıcı sayısı: {len(external)}",
            "wrap": True,
            "color": "Attention" if external else "Default",
            "weight": "Bolder",
        },
    ]
    if external:
        body.append(
            {
                "type": "TextBlock",
                "text": "Dış alıcılar: " + ", ".join(external),
                "wrap": True,
                "color": "Attention",
                "weight": "Bolder",
            }
        )
    status = draft["status"]
    if status == "gönderildi":
        body.append({"type": "TextBlock", "text": f"Gönderildi · {draft['sent_at']}", "wrap": True})
    elif error or status == "gönderiliyor":
        body.append({"type": "TextBlock", "text": error or "Gönderim işleniyor.", "wrap": True})
    actions = []
    if status in {"taslak", "hata"}:
        actions.append(
            {
                "type": "Action.Execute",
                "title": "Onayla ve gönder",
                "verb": "mail.send",
                "data": {
                    "meeting_id": mid,
                    "draft_id": draft["draft_id"],
                    "version": draft["version"],
                },
            }
        )
    actions.append(
        {
            "type": "Action.OpenUrl",
            "title": "Panelde aç",
            "url": absolute_url(f"/panel/{quote(mid, safe='')}"),
        }
    )
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


def invoke_response(card: dict, status: int = 200) -> dict:
    return {"statusCode": status, "type": "application/vnd.microsoft.card.adaptive", "value": card}


async def invoke(activity: Activity) -> dict:
    if (
        activity.name != "adaptiveCard/action"
        or activity.value is None
        or activity.conversation.conversation_type != "personal"
    ):
        raise HTTPException(400, "Geçersiz kart işlemi")
    data = activity.value.action.data
    member = await connector.get().member(activity)
    account = await repo.find_panel_account(member["email"])
    if not account or not account.enabled or account.role not in {"admin", "operator"}:
        raise HTTPException(403, "Bu işlem için yetkiniz yok")
    access = PanelAccess(account.id, account.role)
    await access.check(data.meeting_id, "manage")
    draft = await mail.get_draft(data.meeting_id, access)
    if draft["draft_id"] != data.draft_id or draft["version"] != data.version:
        return invoke_response(
            approval_card(data.meeting_id, draft, error="Taslak değişti; yeniden onaylayın.")
        )
    if draft["status"] in {"gönderildi", "gönderiliyor"}:
        return invoke_response(approval_card(data.meeting_id, draft))
    error = ""
    try:
        draft = await mail.send(data.meeting_id, data.version, access, draft_id=data.draft_id)
    except HTTPException:
        draft = await mail.get_draft(data.meeting_id, access)
        if draft["status"] != "gönderildi":
            error = "Gönderim tamamlanamadı veya sürüyor. Ayrıntılar için paneli açın."
    return invoke_response(approval_card(data.meeting_id, draft, error=error))
