"""Optional live SDK adapter and deterministic scripted offline provider."""

from copy import deepcopy
from dataclasses import dataclass
import json
import os
import re
from math import isfinite
from typing import Protocol


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached_input_tokens: int | None = None


DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_TIMEOUT_SECONDS = 180.0


def validate_timeout_seconds(value: float) -> float:
    """A bounded SDK operation timeout, not an end-to-end deadline."""
    if type(value) not in (int, float) or not 0 < value <= MAX_TIMEOUT_SECONDS or not isfinite(value):
        raise ValueError("timeout_seconds must be finite, greater than 0 and at most 180")
    return float(value)


@dataclass(frozen=True)
class ProviderDiagnostic:
    exception_type: str
    status_code: int | None
    error_type: str | None
    error_code: str | None
    message: str | None
    request_id: str | None
    category: str


class ProviderFailure(RuntimeError):
    def __init__(self, diagnostic: ProviderDiagnostic):
        super().__init__("xAI request failed; see sanitized planner trace diagnostic.")
        self.diagnostic = diagnostic


def _safe_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        return None
    if any(marker in value.lower() for marker in ("authorization", "bearer", "apikey", "api_key", "secret", "token", "sk-")):
        return None
    if any(secret and secret in value for secret in (os.environ.get("XAI_API_KEY"), os.environ.get("TYPESAFE_API_KEY"))):
        return None
    return value


def _safe_message(value):
    """Keep short structured provider messages, never headers or echoed payloads."""
    if not isinstance(value, str) or not 0 < len(value) <= 240 or any(char in value for char in "\r\n{}[]"):
        return None
    lowered = value.lower()
    sensitive = ("authorization", "bearer", "api key", "api_key", "apikey", "password",
                 "secret", "sk-", "xai-", "request body", "messages", "prompt", "content")
    if any(fragment in lowered for fragment in sensitive):
        return None
    if any(secret and secret in value for secret in (os.environ.get("XAI_API_KEY"), os.environ.get("TYPESAFE_API_KEY"))):
        return None
    return value.strip() or None


def _diagnose_provider_error(error: Exception) -> ProviderDiagnostic:
    # SDK status exceptions expose structured response fields. Never serialize
    # the exception, response, headers, request body, or its raw body.
    name = _safe_identifier(type(error).__name__) or "Exception"
    status = getattr(error, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(details, dict):
        details = {}
    error_type = _safe_identifier(getattr(error, "type", None) or details.get("type"))
    error_code = _safe_identifier(getattr(error, "code", None) or details.get("code"))
    message = _safe_message(details.get("message") or getattr(error, "message", None))
    request_id = _safe_identifier(getattr(error, "request_id", None))
    if request_id is None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            request_id = _safe_identifier(headers.get("x-request-id"))
    indicator = " ".join(filter(None, (error_type, error_code))).lower()
    if status == 402 or any(word in indicator for word in ("quota", "billing", "credits", "payment_required")):
        category = "billing_quota"
    elif status == 401 or name == "AuthenticationError":
        category = "authentication"
    elif status == 403 or name == "PermissionDeniedError":
        category = "authorization"
    elif status == 429 or name == "RateLimitError":
        category = "rate_limit"
    elif any(word in indicator for word in ("model_not_found", "model_unavailable", "model_not_available")):
        category = "model_unavailable"
    elif status in (408, 504) or name in ("APITimeoutError", "TimeoutError") or isinstance(error, TimeoutError):
        category = "timeout"
    elif name in ("APIConnectionError", "ConnectError", "ConnectionError") or isinstance(error, ConnectionError):
        category = "connection_network"
    elif status in (400, 404, 422) or name in ("BadRequestError", "NotFoundError", "UnprocessableEntityError"):
        category = "invalid_request"
    else:
        category = "unknown_provider_error"
    return ProviderDiagnostic(name, status, error_type, error_code, message, request_id, category)


class PlannerProvider(Protocol):
    def generate(self, messages: list[dict]) -> ProviderResponse: ...


class FakePlannerProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, messages):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, ProviderResponse):
            return response
        return ProviderResponse(response if isinstance(response, str) else json.dumps(response))


def missing_configuration() -> list[str]:
    return [name for name in ("XAI_API_KEY", "XAI_MODEL") if not os.environ.get(name, "").strip()]


class XAIPlannerProvider:
    def __init__(self, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        # Validate before reading credentials or constructing the SDK client.
        self.timeout_seconds = validate_timeout_seconds(timeout_seconds)
        missing = missing_configuration()
        if missing:
            raise RuntimeError("Missing configuration: " + ", ".join(missing))
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("Optional openai SDK or its runtime dependencies are unavailable; no packages were installed automatically.") from None
        self._model = os.environ["XAI_MODEL"]
        self._client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1", timeout=self.timeout_seconds, max_retries=0)

    def generate(self, messages):
        try:
            response = self._client.chat.completions.create(
                model=self._model, messages=messages, response_format={"type": "json_object"},
            )
            usage = response.usage
            return ProviderResponse(response.choices[0].message.content or "", usage.prompt_tokens if usage else None,
                                    usage.completion_tokens if usage else None,
                                    cached_input_tokens=getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None))
        except Exception as error:
            raise ProviderFailure(_diagnose_provider_error(error)) from None
