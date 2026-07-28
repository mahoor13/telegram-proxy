import hashlib
import hmac
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

os.environ.setdefault("RELAY_ENABLED", "true")
os.environ.setdefault("RELAY_KEY", "test-key")
os.environ.setdefault("RELAY_SECRET", "test-secret")
os.environ.setdefault("RELAY_ALLOWED_HOSTS_YOUTUBE", "www.googleapis.com,oauth2.googleapis.com")

import main  # noqa: E402
import relay  # noqa: E402


class FakeRaw:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def stream(self, _size, decode_content=False):
        del decode_content
        yield self._body


class FakeUpstreamResponse:
    def __init__(self, body=b'{"ok":true}', status=200, headers=None):
        self.status_code = status
        self.raw = FakeRaw(body, headers)
        self.closed = False

    def close(self):
        self.closed = True


class RelayTest(unittest.TestCase):
    def setUp(self):
        os.environ["RELAY_ENABLED"] = "true"
        os.environ["RELAY_KEY"] = "test-key"
        os.environ["RELAY_SECRET"] = "test-secret"
        os.environ["RELAY_ALLOWED_HOSTS_YOUTUBE"] = (
            "www.googleapis.com,oauth2.googleapis.com"
        )
        relay.NONCES._items = {}
        self.client = main.app.test_client()

    def session_with(self, response):
        session = Mock()
        session.request.return_value = response
        return session

    def signed_headers(self, method, target, nonce="nonce-1", provider="youtube"):
        timestamp = str(int(time.time()))
        canonical = "\n".join([
            method,
            provider,
            target,
            timestamp,
            nonce,
            relay.UNSIGNED_PAYLOAD,
        ])
        signature = hmac.new(
            b"test-secret",
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "X-Relay-Provider": provider,
            "X-Relay-Target": target,
            "X-Relay-Key": "test-key",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Nonce": nonce,
            "X-Relay-Content-SHA256": relay.UNSIGNED_PAYLOAD,
            "X-Relay-Signature": signature,
            "Authorization": "Bearer upstream-token",
            "Content-Type": "application/json",
        }

    @patch("relay.socket.getaddrinfo")
    @patch("relay.SESSION_FACTORY")
    def test_forwards_signed_request_and_returns_real_upstream_response(
        self,
        session_factory,
        dns_mock,
    ):
        dns_mock.return_value = [
            (None, None, None, None, ("142.250.180.10", 443)),
        ]
        upstream = FakeUpstreamResponse(
            body=b'{"access_token":"token"}',
            status=201,
            headers={
                "Content-Type": "application/json",
                "X-Upstream": "yes",
                "Location": "https://www.googleapis.com/upload/session-1",
            },
        )
        session = self.session_with(upstream)
        forwarded_body = {}

        def consume_body(**options):
            forwarded_body["length"] = len(options["data"])
            forwarded_body["body"] = b"".join(options["data"])
            return upstream

        session.request.side_effect = consume_body
        session_factory.return_value = session
        target = "https://oauth2.googleapis.com/token?source=test"

        response = self.client.post(
            "/relay",
            headers=self.signed_headers("POST", target),
            data=b'{"grant_type":"refresh_token"}',
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(b'{"access_token":"token"}', response.data)
        self.assertEqual("yes", response.headers["X-Upstream"])
        self.assertEqual(
            "https://www.googleapis.com/upload/session-1",
            response.headers["Location"],
        )
        session.request.assert_called_once()
        options = session.request.call_args.kwargs
        self.assertEqual(target, options["url"])
        self.assertEqual("Bearer upstream-token", options["headers"]["Authorization"])
        self.assertNotIn("X-Relay-Target", options["headers"])
        self.assertFalse(isinstance(options["data"], (bytes, str)))
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertEqual(b'{"grant_type":"refresh_token"}', forwarded_body["body"])
        self.assertEqual(len(forwarded_body["body"]), forwarded_body["length"])
        self.assertTrue(upstream.closed)
        session.close.assert_called_once()

    @patch("relay.SESSION_FACTORY")
    def test_rejects_a_target_outside_provider_allowlist(self, session_factory):
        target = "https://internal.example.com/secrets"

        response = self.client.get(
            "/relay",
            headers=self.signed_headers("GET", target),
        )

        self.assertEqual(403, response.status_code)
        session_factory.assert_not_called()

    @patch("relay.socket.getaddrinfo")
    @patch("relay.SESSION_FACTORY")
    def test_rejects_target_that_resolves_to_private_address(
        self,
        session_factory,
        dns_mock,
    ):
        dns_mock.return_value = [
            (None, None, None, None, ("127.0.0.1", 443)),
        ]
        target = "https://www.googleapis.com/upload"

        response = self.client.get(
            "/relay",
            headers=self.signed_headers("GET", target),
        )

        self.assertEqual(403, response.status_code)
        session_factory.assert_not_called()

    @patch.dict(
        os.environ,
        {
            "OUTBOUND_PROXY_URL": "socks5h://host.docker.internal:10808",
            "RELAY_ALLOWED_HOSTS_TELEGRAM": "api.telegram.org",
        },
    )
    @patch("relay.socket.getaddrinfo")
    @patch("relay.SESSION_FACTORY")
    def test_socks5h_uses_remote_dns_for_an_exactly_allowlisted_host(
        self,
        session_factory,
        dns_mock,
    ):
        session = self.session_with(FakeUpstreamResponse())
        session_factory.return_value = session
        target = "https://api.telegram.org/bot-token/getMe"

        response = self.client.get(
            "/relay",
            headers=self.signed_headers(
                "GET",
                target,
                provider="telegram",
            ),
        )

        self.assertEqual(200, response.status_code)
        dns_mock.assert_not_called()
        session.proxies.update.assert_called_once_with({
            "http": "socks5h://host.docker.internal:10808",
            "https": "socks5h://host.docker.internal:10808",
        })

    def test_rejects_invalid_signature(self):
        target = "https://oauth2.googleapis.com/token"
        headers = self.signed_headers("POST", target)
        headers["X-Relay-Signature"] = "bad-signature"

        response = self.client.post("/relay", headers=headers)

        self.assertEqual(401, response.status_code)

    @patch("relay.socket.getaddrinfo")
    @patch("relay.SESSION_FACTORY")
    def test_rejects_replayed_nonce(self, session_factory, dns_mock):
        dns_mock.return_value = [
            (None, None, None, None, ("142.250.180.10", 443)),
        ]
        session = self.session_with(FakeUpstreamResponse())
        session_factory.return_value = session
        target = "https://oauth2.googleapis.com/token"
        headers = self.signed_headers("POST", target, nonce="same-nonce")

        first = self.client.post("/relay", headers=headers)
        second = self.client.post("/relay", headers=headers)

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, second.status_code)
        session.request.assert_called_once()

    def test_disabled_relay_is_not_exposed(self):
        os.environ["RELAY_ENABLED"] = "false"
        target = "https://oauth2.googleapis.com/token"

        response = self.client.post(
            "/relay",
            headers=self.signed_headers("POST", target),
        )

        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
