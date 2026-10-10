"""Twilio transport boundary. Core receives only channel-neutral messages."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import logging
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

from carmind.proactive_runner import DeliveryReceipt, PermanentDeliveryError, TransientDeliveryError
from carmind.product import ProductReply
from carmind.whatsapp import WhatsAppNotificationSender, normalize_payload, twiml


INBOUND_PATH = "/webhooks/twilio/whatsapp/inbound"
STATUS_PATH = "/webhooks/twilio/whatsapp/status"
class TwilioDeliveryStatus(str, Enum):
    ACCEPTED = "accepted"
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNDELIVERED = "undelivered"
    DELIVERED = "delivered"
    READ = "read"


STATUS_RANK = {TwilioDeliveryStatus.ACCEPTED: 0, TwilioDeliveryStatus.SCHEDULED: 5,
               TwilioDeliveryStatus.QUEUED: 10, TwilioDeliveryStatus.SENDING: 15,
               TwilioDeliveryStatus.SENT: 20, TwilioDeliveryStatus.FAILED: 25,
               TwilioDeliveryStatus.UNDELIVERED: 25, TwilioDeliveryStatus.DELIVERED: 30,
               TwilioDeliveryStatus.READ: 40}


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str = field(repr=False)
    auth_token: str = field(repr=False)
    whatsapp_from: str = field(repr=False)
    public_base_url: str
    timeout_seconds: float = 10.0
    content_sid: str | None = None

    def __post_init__(self):
        parsed = urlsplit(self.public_base_url)
        if (not isinstance(self.account_sid, str) or not self.account_sid.strip() or
                not isinstance(self.auth_token, str) or not self.auth_token.strip() or
                not isinstance(self.whatsapp_from, str) or not re.fullmatch(r"whatsapp:\+[0-9]{7,15}", self.whatsapp_from) or
                parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 30):
            raise ValueError("Invalid Twilio WhatsApp configuration.")
        if self.content_sid is not None and not re.fullmatch(r"HX[0-9a-fA-F]{32}", self.content_sid):
            raise ValueError("Invalid Twilio content template configuration.")

    @classmethod
    def from_environment(cls):
        names = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM",
                 "CARMIND_PUBLIC_WEBHOOK_BASE_URL")
        values = [os.environ.get(name) for name in names]
        if any(not value for value in values):
            raise ValueError("Missing Twilio configuration: " + ", ".join(
                name for name, value in zip(names, values) if not value))
        return cls(*values, content_sid=os.environ.get("TWILIO_WHATSAPP_CONTENT_SID") or None)

    def url(self, path):
        return self.public_base_url.rstrip("/") + path


def parse_twilio_form(raw: bytes) -> dict[str, str]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8192:
        raise ValueError("Invalid Twilio payload size.")
    try:
        fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                          max_num_fields=40)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("Invalid Twilio form payload.") from None
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError("Duplicate Twilio form field.")
    return {key: values[0] for key, values in fields.items()}


class TwilioGateway:
    def __init__(self, service, config: TwilioConfig):
        from twilio.request_validator import RequestValidator
        self.service, self.config = service, config
        self.validator = RequestValidator(config.auth_token)

    def _verified(self, path, payload, signature):
        if not signature or not self.validator.validate(self.config.url(path), payload, signature):
            raise PermissionError("Invalid Twilio signature.")

    def inbound(self, raw: bytes, signature: str, *, now=None):
        payload = parse_twilio_form(raw)
        self._verified(INBOUND_PATH, payload, signature)
        if payload.get("AccountSid") != self.config.account_sid:
            raise PermissionError("Unexpected Twilio account.")
        if payload.get("To") != self.config.whatsapp_from or not re.fullmatch(
                r"whatsapp:\+[0-9]{7,15}", payload.get("From", "")):
            raise ValueError("Invalid WhatsApp endpoints.")
        sid = payload.get("MessageSid")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", sid):
            raise ValueError("Invalid MessageSid.")
        now = now or datetime.now(timezone.utc)
        inbound = normalize_payload(payload, received_at=now)
        sender_hash = sha256(payload["From"].encode("utf-8")).hexdigest()
        store = self.service.store
        with store.transaction():
            claimed = store.claim_twilio_inbound(sid, sender_hash, now)
            if not claimed:
                response = store.twilio_inbound_response(sid, sender_hash)
                if response is None:
                    # A crash after ProductService persisted its own receipt can
                    # be recovered without asking the planner or mutating again.
                    prior = store.receipt("whatsapp", payload.get("From", ""), sid)
                    if prior is None:
                        received = store.twilio_inbound_received_at(sid)
                        if now - received < timedelta(minutes=10):
                            raise RuntimeError("Inbound processing is in progress.")
                        store.finish_twilio_inbound(sid)
                        return twiml(ProductReply("This message could not be completed. Please send a new message.", "unavailable"))
                    response = twiml(ProductReply(**prior))
                    store.finish_twilio_inbound(sid)
                return response
        # A processing failure preserves the claim: never rerun a paid planner
        # for this SID. Product receipts recover completed work; an orphaned
        # claim eventually returns a bounded failure and asks for a new message.
        response = twiml(self.service.handle(inbound))
        with store.transaction():
            store.finish_twilio_inbound(sid)
        return response

    def status(self, raw: bytes, signature: str, *, now=None):
        payload = parse_twilio_form(raw)
        self._verified(STATUS_PATH, payload, signature)
        if payload.get("AccountSid") != self.config.account_sid:
            raise PermissionError("Unexpected Twilio account.")
        sid = payload.get("MessageSid")
        try:
            status = TwilioDeliveryStatus(payload.get("MessageStatus"))
        except ValueError:
            raise ValueError("Invalid Twilio delivery status.") from None
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", sid):
            raise ValueError("Invalid Twilio delivery status.")
        error_code = payload.get("ErrorCode") or None
        if error_code is not None and not re.fullmatch(r"[0-9]{1,12}", error_code):
            raise ValueError("Invalid Twilio error code.")
        with self.service.store.transaction():
            self.service.store.save_twilio_status(sid, status.value, STATUS_RANK[status],
                                                  now or datetime.now(timezone.utc), error_code)


class TwilioWhatsAppTransport:
    """Injected SDK client in tests; no send happens at construction."""

    def __init__(self, config: TwilioConfig, client=None):
        self.config = config
        if client is None:
            from twilio.rest import Client
            from twilio.http.http_client import TwilioHttpClient
            # SDK request logging includes destinations/bodies. This dedicated
            # transport logger stays disabled; application diagnostics use categories.
            logger = logging.getLogger("carmind.twilio.transport")
            logger.disabled = True
            client = Client(config.account_sid, config.auth_token,
                            http_client=TwilioHttpClient(timeout=config.timeout_seconds, max_retries=0,
                                                        logger=logger))
        self.client = client

    def __call__(self, recipient, text):
        if not isinstance(recipient, str) or not re.fullmatch(r"whatsapp:\+[0-9]{7,15}", recipient) or not text:
            raise PermanentDeliveryError("invalid_destination")
        try:
            content = ({"content_sid": self.config.content_sid,
                        "content_variables": json.dumps({"1": " ".join(text.split())}, ensure_ascii=True)}
                       if self.config.content_sid else {"body": text})
            result = self.client.messages.create(from_=self.config.whatsapp_from, to=recipient,
                status_callback=self.config.url(STATUS_PATH), **content)
        except Exception as exc:
            from twilio.base.exceptions import TwilioRestException
            from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
            if isinstance(exc, TwilioRestException):
                if exc.status == 429 or exc.status >= 500:
                    raise TransientDeliveryError("provider_unavailable") from None
                raise PermanentDeliveryError("provider_rejected") from None
            if isinstance(exc, (TimeoutError, ConnectionError, RequestsTimeout, RequestsConnectionError)):
                raise TransientDeliveryError("transport_unavailable") from None
            raise PermanentDeliveryError("transport_failure") from None
        sid = getattr(result, "sid", None)
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", sid):
            raise PermanentDeliveryError("invalid_provider_receipt")
        return DeliveryReceipt(sid)


def notification_sender(store, config: TwilioConfig, *, client=None):
    return WhatsAppNotificationSender(store, TwilioWhatsAppTransport(config, client))
