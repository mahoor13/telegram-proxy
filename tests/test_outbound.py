import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from outbound import outbound_proxies, uses_remote_proxy_dns  # noqa: E402


class OutboundProxyTest(unittest.TestCase):
    def test_returns_no_proxy_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual({}, outbound_proxies())

    def test_configures_http_and_https_through_socks5h(self):
        proxy_url = "socks5h://host.docker.internal:10808"

        with patch.dict(
            os.environ,
            {"OUTBOUND_PROXY_URL": proxy_url},
            clear=True,
        ):
            self.assertEqual(
                {"http": proxy_url, "https": proxy_url},
                outbound_proxies(),
            )

    def test_rejects_an_invalid_proxy_url(self):
        with patch.dict(
            os.environ,
            {"OUTBOUND_PROXY_URL": "ftp://proxy.test:21"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP\\(S\\) or SOCKS5"):
                outbound_proxies()

    def test_bypasses_an_explicit_internal_host(self):
        with patch.dict(
            os.environ,
            {
                "OUTBOUND_PROXY_URL": "socks5h://host.docker.internal:10808",
                "OUTBOUND_PROXY_BYPASS_HOSTS": "api,localhost",
            },
            clear=True,
        ):
            self.assertEqual({}, outbound_proxies("http://api:8000/webhook"))

    def test_socks5h_uses_remote_dns(self):
        with patch.dict(
            os.environ,
            {"OUTBOUND_PROXY_URL": "socks5h://host.docker.internal:10808"},
            clear=True,
        ):
            self.assertTrue(
                uses_remote_proxy_dns("https://api.telegram.org/bot/getMe"),
            )


if __name__ == "__main__":
    unittest.main()
