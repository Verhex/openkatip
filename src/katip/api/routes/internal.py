"""Bot sürecinin API'ye veri yüklediği iç uçlar (ör. ekran görüntüsü).

Bot, toplantıya özel anahtarıyla (`X-Katip-Bot-Token`) kimlik doğrular; yönetici token'ı
bot sürecine verilmez.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from katip.api.security import check_bot_token
from katip.pipeline.session import registry

router = APIRouter(prefix="/api/internal", tags=["internal"])

_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024


async def require_bot(
    session_id: str, x_katip_bot_token: str | None = Header(default=None)
) -> None:
    if not check_bot_token(session_id, x_katip_bot_token):
        raise HTTPException(401, "yetkisiz erişim")


@router.post("/{session_id}/screenshots/{request_id}", dependencies=[Depends(require_bot)])
async def upload_screenshot(
    session_id: str,
    request_id: str,
    request: Request,
    x_katip_bot_token: str | None = Header(default=None),
) -> dict:
    if not check_bot_token(session_id, x_katip_bot_token):
        raise HTTPException(status_code=401, detail="yetkisiz erişim")
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="oturum bulunamadı")
    body = await request.body()
    if not body or len(body) > _MAX_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="görüntü boş ya da çok büyük")
    number = await session.on_screenshot(request_id, body)
    if number is None:
        raise HTTPException(status_code=410, detail="istek zaman aşımına uğradı")
    return {"number": number}
