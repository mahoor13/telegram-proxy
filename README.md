# Telegram Gateway and Social Outbound Relay

This service provides three independent capabilities:

1. A synchronous Telegram Bot API reverse proxy.
2. An inbound Telegram webhook relay.
3. A signed, allowlisted outbound relay for social providers such as YouTube
   and Telegram.

The outbound relay is an application-level relay, not an HTTP CONNECT or SOCKS
forward proxy. Laravel sends the original destination in signed `X-Relay-*`
headers and receives the real upstream response synchronously.

## Request flows

```text
Telegram reverse proxy:
Laravel -> /bot<TOKEN>/<method> -> api.telegram.org

Telegram webhook:
Telegram -> /hook -> Laravel webhook endpoint

Social relay:
Laravel -> /relay -> allowlisted provider endpoint
```

## Setup

Create `.env` from the example:

```bash
cp .env.example .env
docker compose up -d --build
```

The service binds to `127.0.0.1:5000` by default.

Never commit real bot tokens, relay keys, or relay secrets. Telegram bot tokens
appear in Bot API URL paths, so access logging is disabled in Gunicorn and must
also be disabled or redacted at Nginx Proxy Manager.

## Optional service-scoped V2Ray proxy

To route only this container's outbound traffic through a V2Ray mixed or SOCKS
listener on the Docker host, configure:

```env
TELEGRAM_PROXY_OUTBOUND_PROXY_URL=socks5h://host.docker.internal:10808
TELEGRAM_PROXY_OUTBOUND_PROXY_BYPASS_HOSTS=api,localhost,127.0.0.1
```

The root Compose file maps this value to `OUTBOUND_PROXY_URL` only inside the
`telegram-proxy` container. It is not injected into Laravel, Portal, or other
containers. `socks5h` is preferred because provider DNS resolution also occurs
through V2Ray.

Exact hosts in `TELEGRAM_PROXY_OUTBOUND_PROXY_BYPASS_HOSTS` bypass V2Ray. The
default keeps the internal webhook hop from `telegram-proxy` to Laravel on the
Docker network while external provider requests still use V2Ray.

The Compose service includes the Linux `host-gateway` mapping required for
`host.docker.internal`. The V2Ray listener must accept connections on the
Docker host interface, not only an inaccessible loopback socket.

The root health check terminates Gunicorn if its HTTP worker stops responding.
Combined with `restart: unless-stopped`, this recovers the service instead of
leaving a master process alive indefinitely in Docker's `unhealthy` state.

## Secure outbound relay

Enable the relay and configure credentials:

```env
RELAY_ENABLED=true
RELAY_PATH=/relay
RELAY_KEY=replace-with-a-random-application-key
RELAY_SECRET=replace-with-at-least-32-random-bytes
RELAY_CLOCK_SKEW=60
RELAY_CONNECT_TIMEOUT=15
RELAY_READ_TIMEOUT=1800
RELAY_ALLOW_HTTP=false
GUNICORN_TIMEOUT=1900
```

Configure exact upstream host allowlists per provider:

```env
RELAY_ALLOWED_HOSTS_YOUTUBE=oauth2.googleapis.com,www.googleapis.com
RELAY_ALLOWED_HOSTS_INSTAGRAM=zigzal.app
RELAY_ALLOWED_HOSTS_TELEGRAM=api.telegram.org
RELAY_ALLOWED_HOSTS_WHATSAPP=graph.facebook.com
```

An empty allowlist denies every target for that provider.

### Relay protocol

Laravel sends requests to `/relay` with:

```text
X-Relay-Provider
X-Relay-Target
X-Relay-Key
X-Relay-Timestamp
X-Relay-Nonce
X-Relay-Content-SHA256
X-Relay-Signature
```

The canonical HMAC payload is:

```text
METHOD
provider
absolute-target-url
timestamp
nonce
UNSIGNED-PAYLOAD
```

`X-Relay-Signature` is the lowercase hexadecimal HMAC-SHA256 of that canonical
text using `RELAY_SECRET`.

The request body uses the `UNSIGNED-PAYLOAD` marker so large and resumable
uploads can stream without being buffered for hashing. TLS protects body
integrity in transit; HMAC authenticates the caller, provider, method, target,
timestamp, and nonce.

### Relay security

The relay:

- accepts only configured application credentials;
- rejects expired timestamps and repeated nonces;
- only accepts known provider names;
- only accepts exact hosts in that provider's allowlist;
- permits HTTPS only by default;
- rejects credentials embedded in target URLs;
- rejects non-standard target ports;
- resolves and rejects private, loopback, link-local, multicast, and otherwise
  non-public destination addresses;
- never follows upstream redirects automatically;
- strips relay authentication and forwarding headers before contacting the
  upstream;
- disables environment-derived proxies in its outbound HTTP session;
- does not queue or automatically retry unsafe requests.

The nonce store is process-local. Keep the configured single Gunicorn worker.
If the service is scaled to multiple workers or replicas, replace it with a
shared Redis-backed nonce store before scaling.

## Nginx Proxy Manager

Create a Proxy Host that forwards to:

```text
http://telegram-proxy:5000
```

Enable SSL. The public Relay URL used by Laravel must be HTTPS.

For YouTube video uploads, configure the front proxy for streaming and long
timeouts. Equivalent Nginx settings:

```nginx
client_max_body_size 0;
proxy_request_buffering off;
proxy_buffering off;
proxy_read_timeout 1800s;
proxy_send_timeout 1800s;
```

Do not log full request paths or sensitive headers.

## Laravel configuration

Example:

```env
SOCIAL_RELAY_URL=https://relay.example.com/relay
SOCIAL_RELAY_KEY=the-same-value-as-RELAY_KEY
SOCIAL_RELAY_SECRET=the-same-value-as-RELAY_SECRET
SOCIAL_RELAY_ALLOW_HTTP=false

YOUTUBE_EGRESS_MODE=relay
TELEGRAM_EGRESS_MODE=relay
ZIGZAL_EGRESS_MODE=relay

EITAA_EGRESS_MODE=none
RUBIKA_EGRESS_MODE=none
BALE_EGRESS_MODE=none
```

Merely setting `SOCIAL_RELAY_URL`, `SOCIAL_DIRECT_PROXY_URL`, or `HTTP_TUNNEL`
does not enable either transport. Every provider must explicitly select
`none`, `direct`, or `relay`.

## Telegram synchronous reverse proxy

Configure:

```env
API_BASE_URL=https://api.telegram.org
ALLOWED_TOKENS=123456789:token-without-the-bot-prefix
```

Requests preserve the Telegram API path:

```text
POST /bot<TOKEN>/sendMessage
GET  /file/bot<TOKEN>/<file-path>
```

Requests are synchronous by default and return Telegram's real status, headers,
and body. This is compatible with clients that require Telegram's
`{"ok":true,"result":...}` response.

Leaving `ALLOWED_TOKENS` empty permits all Telegram bot tokens and is not
recommended on a publicly reachable service.

## Optional legacy Telegram queue

The SQLite queue is retained for callers that explicitly need it, but it is
disabled by default:

```env
QUEUE_ENABLED=true
QUEUE_DB_PATH=/data/queue.db
MAX_RETRIES=8
WORKER_POLL_INTERVAL=0.1
WORKER_BATCH_SIZE=10
```

A caller must explicitly send:

```text
X-Queue-Request: true
```

The response is:

```json
{
  "status": "queued",
  "id": "uuid"
}
```

Check status at:

```text
GET /status/<uuid>
```

Do not enable this queue for Laravel social publishing. Laravel already queues
publishing jobs and requires the real provider response.

## Telegram webhook relay

Configure:

```env
HOOK_ENDPOINT=https://admin.example.com/api/v1/telegram/webhook
HOOK_SECRET_TOKEN=the-secret-used-with-setWebhook
```

Register:

```text
https://relay.example.com/hook
```

Incoming requests must include the matching
`X-Telegram-Bot-Api-Secret-Token` header.

## Health check

```text
GET /health
```

Response:

```json
{"status":"ok"}
```

The Docker Compose service includes a health check for this endpoint.

## Tests

Build the image:

```bash
docker build -t telegram-proxy:test .
```

Run tests:

```bash
docker run --rm \
  -v "$PWD/tests:/tests:ro" \
  --entrypoint python \
  telegram-proxy:test \
  -m unittest discover -s /tests -v
```

Tests cover:

- synchronous Telegram forwarding;
- Telegram API/file token extraction;
- explicit queue activation;
- valid relay forwarding;
- HMAC rejection;
- replay rejection;
- provider host allowlists;
- private destination rejection;
- disabled relay behavior.
