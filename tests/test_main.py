import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402


class FakeRaw:
    headers = {"Content-Type": "application/json"}


class FakeResponse:
    status_code = 200
    content = b'{"ok":true}'
    raw = FakeRaw()


class TelegramGatewayTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    def test_extracts_tokens_from_api_and_file_paths(self):
        self.assertEqual(
            "123:ABC",
            main._token_from_path("bot123:ABC/sendMessage"),
        )
        self.assertEqual(
            "123:ABC",
            main._token_from_path("file/bot123:ABC/photos/file.jpg"),
        )

    @patch("main.requests.request")
    def test_post_requests_are_synchronous_by_default(self, request_mock):
        request_mock.return_value = FakeResponse()

        response = self.client.post(
            "/bot123:ABC/sendMessage",
            json={"chat_id": 1, "text": "hello"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b'{"ok":true}', response.data)
        request_mock.assert_called_once()

    @patch("main.requests.request")
    def test_queue_requires_explicit_header_and_enabled_configuration(self, request_mock):
        os.environ["QUEUE_ENABLED"] = "false"

        response = self.client.post(
            "/bot123:ABC/sendMessage",
            headers={"X-Queue-Request": "true"},
            json={"chat_id": 1, "text": "hello"},
        )

        self.assertEqual(503, response.status_code)
        request_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
