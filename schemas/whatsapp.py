from pydantic import BaseModel, ConfigDict, Field


class WhatsAppText(BaseModel):
    """Text body of an incoming WhatsApp text message."""

    body: str


class WhatsAppReplyItem(BaseModel):
    """Choice selected from an interactive WhatsApp button or list message."""

    id: str
    title: str = ""


class WhatsAppInteractive(BaseModel):
    """Interactive reply payload from Meta's webhook."""

    type: str
    button_reply: WhatsAppReplyItem | None = None
    list_reply: WhatsAppReplyItem | None = None


class WhatsAppMessage(BaseModel):
    """Single message object delivered inside Meta's webhook payload."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    type: str
    text: WhatsAppText | None = None
    interactive: WhatsAppInteractive | None = None


class WhatsAppMetadata(BaseModel):
    """Business phone metadata attached to a webhook change."""

    display_phone_number: str = ""
    phone_number_id: str = ""


class WhatsAppValue(BaseModel):
    """Change value containing metadata and optional messages list."""

    metadata: WhatsAppMetadata = Field(default_factory=WhatsAppMetadata)
    messages: list[WhatsAppMessage] = Field(default_factory=list)


class WhatsAppChange(BaseModel):
    """Single change entry from Meta webhook."""

    value: WhatsAppValue


class WhatsAppEntry(BaseModel):
    """Top-level entry object in Meta webhook."""

    changes: list[WhatsAppChange] = Field(default_factory=list)


class WhatsAppWebhookPayload(BaseModel):
    """Root JSON structure sent to POST /webhooks/whatsapp by Meta."""

    object: str = ""
    entry: list[WhatsAppEntry] = Field(default_factory=list)


class WebhookReply(BaseModel):
    """Reply message generated for a processed customer command."""

    to: str
    text: str
    order_id: int | None = None


class WebhookResponse(BaseModel):
    """Response returned by POST /webhooks/whatsapp."""

    status: str = "ok"
    replies: list[WebhookReply] = Field(default_factory=list)
