from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from core.rate_limit import rate_limit_mpesa_webhook
from models.order import OrderStatus
from schemas.mpesa import MpesaCallbackPayload, MpesaCallbackResponse
from services.mpesa import format_merchant_paid_alert, process_stk_callback, verify_mpesa_token
from services.whatsapp import send_whatsapp_text

router = APIRouter(prefix="/webhooks/mpesa", tags=["mpesa"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

MAX_WEBHOOK_BYTES = 262_144


@router.post(
    "/callback",
    response_model=MpesaCallbackResponse,
    dependencies=[Depends(rate_limit_mpesa_webhook)],
)
async def mpesa_stk_callback(
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
    token: str | None = Query(default=None),
) -> MpesaCallbackResponse:
    """Receive Safaricom Daraja STK Push payment results and settle order inventory."""
    provided_token = token if token is not None else request.headers.get("X-Mpesa-Token")
    if not verify_mpesa_token(provided_token, settings.mpesa_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid M-Pesa webhook token")

    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload too large")

    try:
        payload = MpesaCallbackPayload.model_validate_json(raw_body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid callback payload") from None

    order, transitioned = await process_stk_callback(db, payload.body.stk_callback)
    if order is not None and transitioned:
        if order.status == OrderStatus.PAID:
            background_tasks.add_task(
                send_whatsapp_text,
                order.customer_phone,
                (
                    f"Payment confirmed for Order #{order.id} (KES {order.total_amount:.2f}). "
                    f"M-Pesa Receipt: {order.mpesa_receipt_number}."
                ),
            )
            background_tasks.add_task(
                send_whatsapp_text,
                order.merchant.phone_number,
                format_merchant_paid_alert(order),
            )
        else:
            background_tasks.add_task(
                send_whatsapp_text,
                order.customer_phone,
                f"Payment failed for Order #{order.id}. Reserved stock has been released.",
            )

    return MpesaCallbackResponse(   
            order_id=order.id if order else None,
        order_status=order.status if order else None,
    )
