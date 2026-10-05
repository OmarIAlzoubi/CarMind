"""Offline-testable WhatsApp payload adapter; a trusted gateway must verify webhooks."""

from datetime import datetime
from xml.sax.saxutils import escape

from carmind.ownership import utc
from carmind.product import InboundMessage


def normalize_payload(payload: dict, *, received_at: datetime) -> InboundMessage:
    """Normalize Twilio-shaped fields without importing a Twilio client."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid messaging payload.")
    stamp = payload.get("Timestamp")
    if stamp is None:
        timestamp = utc(received_at)
    else:
        try:
            timestamp = utc(datetime.fromisoformat(stamp))
        except (TypeError, ValueError):
            raise ValueError("Invalid messaging timestamp.") from None
    return InboundMessage("whatsapp", payload.get("From"), payload.get("MessageSid"),
                          payload.get("Body"), timestamp)


def handle_payload(service, payload: dict, *, received_at: datetime):
    """The returned product reply is identical to what Web receives."""
    return service.handle(normalize_payload(payload, received_at=received_at))


def twiml(reply) -> str:
    return '<Response><Message>' + escape(reply.text) + '</Message></Response>'


class WhatsAppNotificationSender:
    """Explicit outbound adapter around an injected transport; no Twilio client here."""

    def __init__(self, store, transport):
        if not callable(transport):
            raise ValueError("A configured WhatsApp transport is required")
        self.store, self.transport = store, transport

    def send(self, notification, text):
        recipient = self.store.channel_binding_for_owner(notification.owner_id, "whatsapp")
        if recipient is None:
            raise ValueError("Owner has no linked WhatsApp recipient")
        self.transport(recipient, text)
