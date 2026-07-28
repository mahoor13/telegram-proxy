import os
from urllib.parse import urlsplit


ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def outbound_proxies(target_url=None):
    proxy_url = os.environ.get("OUTBOUND_PROXY_URL", "").strip()
    if not proxy_url:
        return {}

    if target_url:
        target_host = (urlsplit(target_url).hostname or "").lower().rstrip(".")
        bypass_hosts = {
            host.strip().lower().rstrip(".")
            for host in os.environ.get("OUTBOUND_PROXY_BYPASS_HOSTS", "").split(",")
            if host.strip()
        }
        if target_host in bypass_hosts:
            return {}

    try:
        parsed = urlsplit(proxy_url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("OUTBOUND_PROXY_URL is invalid") from exc

    if (
        parsed.scheme.lower() not in ALLOWED_PROXY_SCHEMES
        or not parsed.hostname
        or port is None
    ):
        raise RuntimeError(
            "OUTBOUND_PROXY_URL must be an HTTP(S) or SOCKS5 proxy URL with a port"
        )

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def uses_remote_proxy_dns(target_url=None):
    proxies = outbound_proxies(target_url)
    proxy_url = proxies.get("https") or proxies.get("http")

    return bool(
        proxy_url
        and urlsplit(proxy_url).scheme.lower() == "socks5h"
    )
