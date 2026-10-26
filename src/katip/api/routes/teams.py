import json
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from katip.logging import get_logger
from katip.teams.activity import Activity
from katip.teams.config import credentials
from katip.teams.security import AuthenticationError, limits, validator
from katip.teams.service import dispatch

router = APIRouter(tags=["teams"])
log = get_logger(__name__)
MAX_BODY = 128 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Yinelenen JSON alanı")
        result[key] = value
    return result


async def authenticated_activity(request: Request):
    config = credentials()
    if config is None:
        raise HTTPException(404, "Bulunamadı")
    if not request.headers.get("authorization"):
        raise HTTPException(401, "Yetkisiz erişim")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "JSON gerekli")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "İstek çok büyük")
        body.extend(chunk)
    try:
        activity = Activity.model_validate(json.loads(body, object_pairs_hook=_unique_object))
    except (ValidationError, ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Geçersiz istek") from None
    try:
        await validator.get().validate(request.headers.get("authorization"), activity, config)
        if activity.recipient.id.lower() != "28:" + config.app_id:
            raise AuthenticationError()
    except AuthenticationError:
        raise HTTPException(401, "Yetkisiz erişim") from None
    except (httpx.HTTPError, KeyError, ValueError):
        raise HTTPException(503, "Hizmet kullanılamıyor") from None
    return config, activity


@router.post("/api/messages")
async def messages(authenticated: Annotated[tuple, Depends(authenticated_activity)]):
    config, activity = authenticated
    limiter = limits.get()
    if not limiter.take("tenant:" + config.tenant, 300) or not limiter.take(
        "chat:" + activity.conversation.id, 30
    ):
        raise HTTPException(429, "Lütfen daha sonra deneyin")
    try:
        response = await dispatch(activity)
    except HTTPException as exc:
        if activity.type == "invoke":
            return JSONResponse(
                {
                    "statusCode": exc.status_code,
                    "type": "application/vnd.microsoft.error",
                    "value": {"code": "Forbidden", "message": "İşlem tamamlanamadı; paneli açın."},
                }
            )
        raise
    except Exception as exc:
        log.warning("Teams isteği tamamlanamadı", error_type=type(exc).__name__)
        raise HTTPException(503, "Hizmet kullanılamıyor") from None
    return JSONResponse(response) if response is not None else Response(status_code=200)
