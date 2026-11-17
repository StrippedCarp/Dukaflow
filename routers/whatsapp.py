import secrets
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from core.rate_limit import rate_limit_whatsapp_verify, rate_limit_whatsapp_webhook
from core.redis import RedisClient
from schemas.whatsapp import WebhookReply, WebhookResponse, WhatsAppWebhookPayload
from services.whatsapp import (
    handle_incoming_message,
    send_whatsapp_text,
    verify_whatsapp_signature,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

MAX_WEBHOOK_BYTES = 262_144  # 256 KB cap prevents memory exhaustion from oversized bodies


@router.get(
    "", response_class=PlainTextResponse, dependencies=[Depends(rate_limit_whatsapp_verify)]
)
async def whatsapp_webhook_verify(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_challenge: str = Query(..., alias="hub.challenge", max_length=256),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
) -> str:
    """One-time handshake Meta performs when you register the webhook URL.

    Meta sends our secret verify token; if it matches, we echo the challenge.
    """
    token_ok = bool(settings.whatsapp_verify_token) and secrets.compare_digest(
        hub_verify_token.encode(), settings.whatsapp_verify_token.encode()
    )
    if hub_mode == "subscribe" and token_ok:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@router.post(
    "", response_model=WebhookResponse, dependencies=[Depends(rate_limit_whatsapp_webhook)]
)
async def whatsapp_webhook_receive(
    request: Request,
    db: DbSession,
    redis: RedisClient,
    background_tasks: BackgroundTasks,
) -> WebhookResponse:
    """Verify Meta's HMAC signature and process incoming WhatsApp messages."""
    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload too large")

    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_whatsapp_signature(raw_body, signature, settings.whatsapp_app_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        payload = WhatsAppWebhookPayload.model_validate_json(raw_body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid webhook payload") from None

    replies: list[WebhookReply] = []
    for entry in payload.entry:
        for change in entry.changes:
            display_phone = change.value.metadata.display_phone_number
            for msg in change.value.messages:
                reply = await handle_incoming_message(db, redis, msg, display_phone)
                if reply is not None:
                    background_tasks.add_task(send_whatsapp_text, reply.to, reply.text)
                    replies.append(reply)

    return WebhookResponse(replies=replies)
