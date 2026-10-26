"""Mobile-friendly Block Kit; all displayed fixed text is Turkish."""

import json
from collections.abc import Sequence
from typing import Any


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text, "emoji": True}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def report_message(title: str, sections: Sequence[str], cost_usd: float | None) -> dict[str, Any]:
    cost = "bilinmiyor" if cost_usd is None else f"{cost_usd:.4f} USD"
    blocks: list[dict[str, Any]] = [{"type": "header", "text": _plain(title[:150])}]
    blocks.extend(_section(escape(section)[:3000]) for section in sections if section)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Maliyet: {cost}"}]})
    if len(blocks) > 50:
        raise ValueError("Rapor çok uzun.")
    return {"text": f"{title} · Maliyet: {cost}", "blocks": blocks}


def approval_card(
    approval_id: str, title: str, body: str, reason: str, *, opinion_check: bool = False
) -> dict[str, Any]:
    lines = body.splitlines()
    if not 3 <= len(lines) <= 6 or not all(line.strip() for line in lines):
        raise ValueError("Onay açıklaması 3–6 dolu satır içermeli.")
    if len(body) > 2800 or len(reason) > 2400:
        raise ValueError("Onay açıklaması çok uzun.")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(title[:150])},
        _section(escape(body)),
        _section(f"*Neden önemli:* {escape(reason)}"),
    ]
    if opinion_check:
        blocks.append(
            _section("Görüş kontrolü: Bu, içerik sahibinin gerçekten düşündüğü bir şey mi?")
        )
    buttons = []
    for action, label, style in (
        ("approve", "Onayla", "primary"),
        ("edit", "Düzenle", None),
        ("reject", "Reddet", "danger"),
    ):
        button = {
            "type": "button",
            "text": _plain(label),
            "action_id": f"katip_{action}",
            "value": approval_id,
        }
        if style:
            button["style"] = style
        buttons.append(button)
    blocks.append({"type": "actions", "elements": buttons})
    return {"text": f"{title} · Onay bekliyor", "blocks": blocks}


def decision_message(
    original_blocks: Sequence[dict[str, Any]], result: str, actor: str
) -> dict[str, Any]:
    blocks = [block for block in original_blocks if block.get("type") != "actions"]
    blocks.append(_section(f"*Sonuç:* {escape(result)}\n*Karar veren:* <@{escape(actor)}>"))
    return {"text": f"{result} · Karar veren: {actor}", "blocks": blocks}


def edit_modal(
    approval_id: str, text: str, *, channel: str, ts: str, digest: str, field: str = ""
) -> dict[str, Any]:
    if len(text) > 3000:
        raise ValueError("Düzenlenecek metin 3000 karakteri aşamaz.")
    return {
        "type": "modal",
        "callback_id": "katip_edit_submit",
        "title": _plain("İşlemi düzenle"),
        "submit": _plain("Onayla"),
        "close": _plain("Vazgeç"),
        "private_metadata": json.dumps(
            {
                "approval_id": approval_id,
                "channel": channel,
                "ts": ts,
                "digest": digest,
                "field": field,
            }
        ),
        "blocks": [
            {
                "type": "input",
                "block_id": "edit",
                "label": _plain("İşlem metni"),
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "initial_value": text,
                    "max_length": 3000,
                },
            }
        ],
    }
