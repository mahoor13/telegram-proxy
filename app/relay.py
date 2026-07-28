import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
from flask import Response, jsonify, request, stream_with_context

from outbound import outbound_proxies, uses_remote_proxy_dns


logger = logging.getLogger("relay")

UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RELAY_HEADERS = {
    "x-relay-provider",
    "x-relay-target",
    "x-relay-key",
    "x-relay-timestamp",
    "x-relay-nonce",
    "x-relay-content-sha256",
    "x-relay-signature",
}
HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class SizedRequestBody:
    def __init__(self, stream, length):
        self.stream = stream
        self.length = length

    def __len__(self):
        return self.length

    def __iter__(self):
        while True:
            chunk = self.stream.read(64 * 1024)
            if not chunk:
                return
            yield chunk


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class NonceStore:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def remember(self, key, nonce, expires_at):
        identity = f"{key}:{nonce}"
        now = time.time()
        with self._lock:
            self._items = {
                item: expiry for item, expiry in self._items.items()
                if expiry > now
            }
            if identity in self._items:
                return False
            self._items[identity] = expires_at
            return True


NONCES = NonceStore()
SESSION_FACTORY = requests.Session


class RelayRejected(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def relay_path():
    path = os.environ.get("RELAY_PATH", "/relay").strip() or "/relay"
    return path if path.startswith("/") else f"/{path}"


def relay_enabled():
    return _env_bool("RELAY_ENABLED", False)


def _allowed_hosts(provider):
    key = "RELAY_ALLOWED_HOSTS_" + re.sub(r"[^A-Z0-9]", "_", provider.upper())
    return {
        host.strip().lower().rstrip(".")
        for host in os.environ.get(key, "").split(",")
        if host.strip()
    }


def _validate_target(provider, target, remote_dns=False):
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError as exc:
        raise RelayRejected("invalid relay target") from exc

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in ({"https", "http"} if _env_bool("RELAY_ALLOW_HTTP") else {"https"}):
        raise RelayRejected("relay target scheme is not allowed", 403)
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise RelayRejected("invalid relay target")
    if parsed.fragment:
        raise RelayRejected("relay target fragments are not allowed")

    expected_port = 80 if scheme == "http" else 443
    if port not in (None, expected_port):
        raise RelayRejected("relay target port is not allowed", 403)

    allowed = _allowed_hosts(provider)
    if hostname not in allowed:
        raise RelayRejected("relay target host is not allowed", 403)

    # socks5h deliberately delegates DNS to the proxy. Local resolvers in
    # restricted networks can return a private interception address for an
    # otherwise allowlisted provider such as api.telegram.org. The exact
    # provider host allowlist remains mandatory, so skipping local resolution
    # here does not turn the Relay into an arbitrary-host proxy.
    if remote_dns:
        return target

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, expected_port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise RelayRejected("relay target DNS resolution failed", 502) from exc

    if not addresses:
        raise RelayRejected("relay target DNS resolution failed", 502)

    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise RelayRejected("relay target resolved to a non-public address", 403)
        except ValueError as exc:
            raise RelayRejected("relay target resolved to an invalid address", 502) from exc

    return target


def _authenticate(method, provider, target):
    expected_key = os.environ.get("RELAY_KEY", "").strip()
    secret = os.environ.get("RELAY_SECRET", "").strip()
    supplied_key = request.headers.get("X-Relay-Key", "").strip()
    timestamp = request.headers.get("X-Relay-Timestamp", "").strip()
    nonce = request.headers.get("X-Relay-Nonce", "").strip()
    content_hash = request.headers.get("X-Relay-Content-SHA256", "").strip()
    supplied_signature = request.headers.get("X-Relay-Signature", "").strip()

    if not expected_key or not secret:
        raise RelayRejected("relay authentication is not configured", 503)
    if not supplied_key or not hmac.compare_digest(supplied_key, expected_key):
        raise RelayRejected("relay authentication failed", 401)
    if not nonce or len(nonce) > 128:
        raise RelayRejected("invalid relay nonce", 401)
    if content_hash != UNSIGNED_PAYLOAD:
        raise RelayRejected("unsupported relay payload digest", 400)

    try:
        request_time = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RelayRejected("invalid relay timestamp", 401) from exc

    clock_skew = max(1, _env_int("RELAY_CLOCK_SKEW", 60))
    if abs(int(time.time()) - request_time) > clock_skew:
        raise RelayRejected("expired relay request", 401)

    canonical = "\n".join([
        method.upper(),
        provider,
        target,
        timestamp,
        nonce,
        content_hash,
    ])
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not supplied_signature or not hmac.compare_digest(supplied_signature, expected_signature):
        raise RelayRejected("relay authentication failed", 401)

    if not NONCES.remember(expected_key, nonce, request_time + clock_skew):
        raise RelayRejected("replayed relay request", 409)


def _upstream_headers():
    excluded = HOP_BY_HOP | RELAY_HEADERS | {
        "content-length",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "forwarded",
    }
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded
    }


def _upstream_body():
    content_length = request.content_length
    if content_length == 0:
        return b""
    if content_length is not None:
        return SizedRequestBody(request.stream, max(0, content_length))

    def chunks():
        while True:
            chunk = request.stream.read(64 * 1024)
            if not chunk:
                return
            yield chunk

    return chunks()


def _response_headers(response):
    return [
        (key, value)
        for key, value in response.raw.headers.items()
        if key.lower() not in HOP_BY_HOP
    ]


def _stream_upstream(response, session):
    try:
        for chunk in response.raw.stream(64 * 1024, decode_content=False):
            if chunk:
                yield chunk
    finally:
        response.close()
        session.close()


def forward_relay():
    if not relay_enabled():
        return jsonify({"error": "relay is disabled"}), 404

    provider = request.headers.get("X-Relay-Provider", "").strip().lower()
    target = request.headers.get("X-Relay-Target", "").strip()

    try:
        if not PROVIDER_PATTERN.fullmatch(provider):
            raise RelayRejected("invalid relay provider")
        _authenticate(request.method, provider, target)
        target = _validate_target(
            provider,
            target,
            remote_dns=uses_remote_proxy_dns(target),
        )
    except RelayRejected as exc:
        return jsonify({"error": str(exc)}), exc.status

    connect_timeout = max(1, _env_int("RELAY_CONNECT_TIMEOUT", 15))
    read_timeout = max(1, _env_int("RELAY_READ_TIMEOUT", 1800))

    session = SESSION_FACTORY()
    session.trust_env = False
    session.proxies.update(outbound_proxies(target))
    try:
        upstream = session.request(
            method=request.method,
            url=target,
            headers=_upstream_headers(),
            data=_upstream_body(),
            allow_redirects=False,
            stream=True,
            timeout=(connect_timeout, read_timeout),
        )
    except requests.RequestException as exc:
        session.close()
        logger.warning(
            "Relay connection failed provider=%s host=%s error=%s",
            provider,
            urlsplit(target).hostname,
            exc.__class__.__name__,
        )
        return jsonify({"error": "relay upstream connection failed"}), 502

    logger.info(
        "Relay provider=%s method=%s host=%s status=%d",
        provider,
        request.method,
        urlsplit(target).hostname,
        upstream.status_code,
    )

    if request.method == "HEAD":
        headers = _response_headers(upstream)
        status = upstream.status_code
        upstream.close()
        session.close()
        return Response(status=status, headers=headers)

    return Response(
        stream_with_context(_stream_upstream(upstream, session)),
        status=upstream.status_code,
        headers=_response_headers(upstream),
        direct_passthrough=True,
    )
