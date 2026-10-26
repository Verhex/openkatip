"""WebSocket uçları.

/ws/ingest/{session_id} — bot sayfasının bağlandığı uç (çift yönlü).
    bot → sunucu, binary : uint32 track_id | uint32 seq | int16[] pcm (16 kHz mono, LE)
                           (track_id şu an hep 0: tarayıcı tüm sesi tek akışta karıştırıyor)
    bot → sunucu, text   : events.py'deki bot olayları (status / roster / chat / ...)
    sunucu → bot, text   : {"kind": "chat_send", "text": "..."} — toplantı chat'ine yaz

/ws/panel/{session_id} — canlı altyazı panelinin bağlandığı uç (sunucu → istemci).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import struct

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, WebSocketException
from pydantic import ValidationError

from katip.api.security import check_bot_token, is_admin, panel_identity, verify
from katip.async_utils import wait_tasks
from katip.config import get_settings
from katip.events import ChatMessage, PanelEvent, RosterUpdate, Segment, SessionState, Translation
from katip.logging import get_logger
from katip.pipeline.session import registry

log = get_logger(__name__)
router = APIRouter()

_HEADER = struct.Struct("<II")  # track_id, sequence
_SENDER_STOP_TIMEOUT_S = 1.0


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


async def require_ingest_socket(ws: WebSocket, session_id: str) -> None:
    if get_settings().ingest_local_only and (
        ws.client is None or ws.client.host not in _LOCAL_HOSTS
    ):
        raise WebSocketException(code=1008)
    if not check_bot_token(session_id, ws.headers.get("x-katip-bot-token")):
        raise WebSocketException(code=1008)


async def require_panel_socket(ws: WebSocket, session_id: str) -> None:
    if verify(session_id, ws.query_params.get("t")) or is_admin(ws.headers.get("authorization")):
        return
    access = await panel_identity(ws)
    if access is None or not await access.allows(session_id):
        raise WebSocketException(code=1008)


@router.websocket("/ws/ingest/{session_id}", dependencies=[Depends(require_ingest_socket)])
async def ingest(ws: WebSocket, session_id: str) -> None:
    if get_settings().ingest_local_only and (
        ws.client is None or ws.client.host not in _LOCAL_HOSTS
    ):
        # Bot aynı konteynerde; dışarıdan ses/komut enjeksiyonunu reddet.
        await ws.close(code=1008)
        return
    # Toplantıya özel bot anahtarı: yalnızca bu toplantı için başlatılan bot bağlanabilir.
    if not check_bot_token(session_id, ws.headers.get("x-katip-bot-token")):
        log.warning("ingest reddedildi: bot anahtarı geçersiz", session_id=session_id)
        await ws.close(code=1008)
        return
    session = registry.get(session_id)
    if session is None:
        await ws.close(code=1008)
        return
    await ws.accept()
    log.info("bot bağlandı", session_id=session_id)
    sender = asyncio.create_task(_forward_to_bot(ws, session))

    try:
        while True:
            message = await ws.receive()
            # A revoked worker must not keep writing on an already-open socket.
            if not check_bot_token(session_id, ws.headers.get("x-katip-bot-token")):
                await ws.close(code=1008)
                break
            if (data := message.get("bytes")) is not None:
                if len(data) > 262144:
                    await ws.close(code=1009)
                    break
                if len(data) <= _HEADER.size:
                    continue
                session.push_audio(data[_HEADER.size :])
            elif (text := message.get("text")) is not None:
                if len(text) > 65536:
                    await ws.close(code=1009)
                    break
                if text == '{"kind":"heartbeat"}':
                    from katip.workers import jobs

                    await jobs.touch_ingest(session_id)
                    await ws.send_text('{"kind":"heartbeat_ack"}')
                    continue
                await _handle_control(session_id, text)
            elif message.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await wait_tasks(
            {sender}, budget_s=_SENDER_STOP_TIMEOUT_S, label="bot göndericisi", cancel=True
        )
        log.info("bot bağlantısı kapandı", session_id=session_id)


async def _forward_to_bot(ws: WebSocket, session) -> None:
    while True:
        message = await session.outbox.get()
        await ws.send_text(json.dumps(message, ensure_ascii=False))


async def _handle_control(session_id: str, raw: str) -> None:
    session = registry.get(session_id)
    if session is None:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("bozuk kontrol mesajı", session_id=session_id)
        return

    if not isinstance(payload, dict) or len(raw) > 65536:
        return
    try:
        if payload.get("kind") == "roster":
            RosterUpdate(**payload)
        elif payload.get("kind") == "chat":
            ChatMessage(**payload)
        elif payload.get("kind") == "status":
            SessionState(payload.get("state", SessionState.PENDING))
            if payload.get("detail") is not None and (
                not isinstance(payload["detail"], str) or len(payload["detail"]) > 1000
            ):
                return
    except (ValidationError, ValueError, TypeError):
        log.warning("geçersiz kontrol olayı", session_id=session_id)
        return

    match payload.get("kind"):
        case "presentation_status":
            page, pages, error = (payload.get(k) for k in ("page", "pages", "error"))
            if (
                type(page) is int
                and type(pages) is int
                and 0 <= page <= pages <= 60
                and (error is None or isinstance(error, str) and len(error) <= 1000)
                and isinstance(payload.get("request_id"), str)
            ):
                session.on_presentation_status(payload)
        case "status":
            state = SessionState(payload.get("state", SessionState.PENDING))
            session.set_state(state, payload.get("detail"))
            if state in (SessionState.ENDED, SessionState.FAILED):
                await session.shutdown()
        case "track_opened" | "track_closed":
            # Tanılama amaçlı: ses zaten tek karışık akışta geliyor.
            log.info("uzak ses track'i", session_id=session_id, **payload)
        case "roster":
            session.update_roster(RosterUpdate(**payload))
        case "chat":
            if payload.get("text"):
                session.on_chat(str(payload.get("author") or ""), str(payload["text"]))
        case "audio_health":
            # Tarayıcıdaki ses hattının 5 sn'lik özeti: paket yoksa ya da tepe 0 ise sessizlik.
            # Paket yoksa ses hattı kopmuştur (uyarı). Tepe 0 ise yalnızca kimse konuşmuyordur.
            if not payload.get("packets"):
                level = log.warning
            elif not payload.get("peak"):
                level = log.debug
            else:
                level = log.info
            level(
                "ses sağlığı",
                session_id=session_id,
                packets=payload.get("packets"),
                peak=payload.get("peak"),
                tracks=payload.get("tracks"),
                ctx=payload.get("ctx"),
            )
        case "speaker_style":
            # Teams "konuşuyor" çerçevesinin stil değişimleri: gösterge kalibrasyonu için.
            log.info(
                "konuşma göstergesi",
                session_id=session_id,
                # Loglarda kişisel veri yok: ad yerine kısa özet (kutucuklar yine ayırt edilir).
                tile=hashlib.sha256(str(payload.get("name")).encode()).hexdigest()[:8],
                active=payload.get("active"),
                sig=payload.get("sig"),
            )
        case "speaker_dom":
            # Teams kutucuklarında öznitelik değişimleri (adlar maskeli): gösterge kalibrasyonu.
            log.info(
                "konuşma dom",
                session_id=session_id,
                el=str(payload.get("el"))[:60],
                attr=payload.get("attr"),
                detail=str(payload.get("detail"))[:160],
            )
        case unknown:
            log.debug("bilinmeyen kontrol mesajı", kind=unknown)


@router.websocket("/ws/panel/{session_id}", dependencies=[Depends(require_panel_socket)])
async def panel(ws: WebSocket, session_id: str) -> None:
    # Canlı transkript yalnızca bu toplantının imzalı linkiyle (ya da yönetici token'ıyla) açılır.
    ticket = ws.query_params.get("t")
    if not (verify(session_id, ticket) or is_admin(ws.headers.get("authorization"))):
        access = await panel_identity(ws)
        if access is None or not await access.allows(session_id):
            await ws.close(code=1008)
            return
    await ws.accept()
    session = registry.get(session_id)
    if session is None:
        # Panel oturum AÇMAZ (açsaydı: eski sekmeler hayalet oturum yaratıp toplantı kaydını
        # değiştiriyordu, herkes rastgele oturum açtırabiliyordu). Biten toplantının geçmişi
        # veritabanından gösterilir.
        await _send_history(ws, session_id)
        return
    queue = session.subscribe()

    # Yeni bağlanan panel mevcut durumu ve geçmişi görsün.
    await ws.send_text(PanelEvent(kind="mode", text=session.reply_mode).model_dump_json())
    if session.paused:
        await ws.send_text(PanelEvent(kind="paused", text="on").model_dump_json())
    for segment in session.final_segments()[-200:]:
        await ws.send_text(PanelEvent(kind="segment", segment=segment).model_dump_json())
        if segment.translation:
            translation = Translation(
                segment_id=segment.id,
                session_id=session_id,
                text=segment.translation,
                lang=session.target_lang,
                is_final=True,
            )
            await ws.send_text(
                PanelEvent(kind="translation", translation=translation).model_dump_json()
            )

    try:
        while True:
            event = await queue.get()
            if not (verify(session_id, ticket) or is_admin(ws.headers.get("authorization"))):
                access = await panel_identity(ws)
                if access is None or not await access.allows(session_id):
                    await ws.close(code=1008)
                    break
            await ws.send_text(event.model_dump_json(exclude_none=True))
    except WebSocketDisconnect:
        pass
    finally:
        session.unsubscribe(queue)


async def _send_history(ws: WebSocket, session_id: str) -> None:
    from katip.store import repo

    meeting = await repo.get_meeting(session_id)
    if meeting is None:
        await ws.send_text(PanelEvent(kind="error", text="oturum bulunamadı").model_dump_json())
        await ws.close(code=1008)
        return
    for s in meeting.segments[-300:]:
        segment = Segment(
            id=s.id,
            session_id=session_id,
            speaker=s.speaker_name,
            speaker_label=s.speaker_label,
            start_s=s.start_s,
            end_s=s.end_s,
            text=s.text,
            lang=s.lang,
            is_final=True,
            translation=s.translation,
        )
        await ws.send_text(PanelEvent(kind="segment", segment=segment).model_dump_json())
        if s.translation:
            translation = Translation(
                segment_id=s.id,
                session_id=session_id,
                text=s.translation,
                lang=meeting.target_lang or "en",
                is_final=True,
            )
            await ws.send_text(
                PanelEvent(kind="translation", translation=translation).model_dump_json()
            )
    await ws.send_text(
        PanelEvent(kind="status", state=SessionState.ENDED, text="toplantı bitti").model_dump_json()
    )
    with contextlib.suppress(WebSocketDisconnect, Exception):
        while True:
            await ws.receive_text()
