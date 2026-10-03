"""Offline checks for sanitized xAI provider failures."""

from dataclasses import asdict
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from carmind.planner import run_full_planner
from carmind.planner_provider import ProviderFailure, XAIPlannerProvider, _diagnose_provider_error
from test_safety import snapshot


def provider_error(name, *, status=None, code=None, kind=None, message=None, request_id=None):
    error = type(name, (Exception,), {})("Raw exception must not enter the trace")
    error.status_code = status
    error.body = {"error": {"code": code, "type": kind, "message": message}}
    error.request_id = request_id
    return error


class ProviderDiagnosticTests(unittest.TestCase):
    def test_structured_status_categories(self):
        cases = (
            (provider_error("AuthenticationError", status=401), "authentication"),
            (provider_error("PermissionDeniedError", status=403), "authorization"),
            (provider_error("BadRequestError", status=400), "invalid_request"),
            (provider_error("RateLimitError", status=429), "rate_limit"),
            (provider_error("RateLimitError", status=429, code="insufficient_quota"), "billing_quota"),
            (provider_error("NotFoundError", status=404, code="model_not_found"), "model_unavailable"),
            (provider_error("APITimeoutError"), "timeout"),
            (provider_error("APIConnectionError"), "connection_network"),
            (provider_error("InternalServerError", status=500), "unknown_provider_error"),
        )
        for error, category in cases:
            with self.subTest(category=category):
                self.assertEqual(_diagnose_provider_error(error).category, category)

    def test_structured_safe_fields_and_request_id(self):
        error = provider_error("BadRequestError", status=400, code="invalid_request_error",
                               kind="invalid_request_error", message="Unsupported response format")
        error.response = SimpleNamespace(headers={"x-request-id": "req-123", "Authorization": "Bearer forbidden"})
        diagnostic = _diagnose_provider_error(error)
        self.assertEqual(diagnostic.status_code, 400)
        self.assertEqual(diagnostic.error_type, "invalid_request_error")
        self.assertEqual(diagnostic.error_code, "invalid_request_error")
        self.assertEqual(diagnostic.message, "Unsupported response format")
        self.assertEqual(diagnostic.request_id, "req-123")
        self.assertNotIn("forbidden", str(asdict(diagnostic)))

    def test_secrets_and_unknown_exception_text_are_not_exposed(self):
        secret = "unit-test-secret-value"
        with patch.dict(os.environ, {"XAI_API_KEY": secret}):
            error = provider_error("AuthenticationError", status=401, code=secret,
                                   message="Authorization: Bearer " + secret,
                                   request_id=secret)
            diagnostic = _diagnose_provider_error(error)
            self.assertIsNone(diagnostic.error_code)
            self.assertIsNone(diagnostic.message)
            self.assertIsNone(diagnostic.request_id)
            self.assertNotIn(secret, str(asdict(diagnostic)))
            self.assertIsNone(_diagnose_provider_error(provider_error(
                "BadRequestError", status=400, request_id="Bearer-hidden-value")).request_id)
            unknown = _diagnose_provider_error(RuntimeError("Authorization: Bearer " + secret))
            self.assertEqual(unknown.category, "unknown_provider_error")
            self.assertIsNone(unknown.message)
            self.assertNotIn(secret, str(asdict(unknown)))

    def test_adapter_and_planner_preserve_safe_trace_without_raw_failure(self):
        secret = "unit-test-secret-value"
        error = provider_error("RateLimitError", status=429, code="rate_limit_exceeded",
                               message="Rate limit exceeded", request_id="req-offline")
        error.response = SimpleNamespace(headers={"Authorization": "Bearer " + secret})
        provider = XAIPlannerProvider.__new__(XAIPlannerProvider)
        provider._model = "configured-model"
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=Mock(side_effect=error))))
        with patch.dict(os.environ, {"XAI_API_KEY": secret}):
            with self.assertRaises(ProviderFailure) as caught:
                provider.generate([])
            self.assertEqual(caught.exception.diagnostic.category, "rate_limit")
            result = run_full_planner(snapshot(), provider)
        self.assertEqual(result.trace.completion_status, "incomplete")
        self.assertEqual(result.trace.provider_error.status_code, 429)
        self.assertEqual(result.trace.provider_error.error_code, "rate_limit_exceeded")
        self.assertEqual(result.trace.provider_error.message, "Rate limit exceeded")
        self.assertEqual(result.trace.provider_error.request_id, "req-offline")
        self.assertEqual(result.result.assessment.evidence_ids, [])
        self.assertNotIn(secret, str(result))
        self.assertNotIn("Raw exception must not enter the trace", str(result))
        self.assertEqual(provider._client.chat.completions.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
