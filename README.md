# telegram-proxy

Two-in-one Telegram proxy service behind Nginx Proxy Manager.

## Features

- **API Proxy** — proxies any request to `api.telegram.org`. Request flow is controlled by the `X-Bypass-Queue` header:
  - **Without the header (default):** request is enqueued, processed asynchronously with rate-limit compliance, and Telegram's 429 `retry_after` is honoured automatically. Returns `202 Accepted` with a tracking ID.
  - **With `X-Bypass-Queue: true`:** request is forwarded synchronously and the real Telegram response is returned immediately.
  - **GET/HEAD requests** always bypass the queue (they are not subject to message rate limits).
- **Webhook Hook** — `POST /hook` receives Telegram bot webhook updates and synchronously forwards them to `HOOK_ENDPOINT`.

## Setup

1. Clone and enter the directory.

2. Create a `data/` directory for the SQLite queue:

```bash
mkdir -p data
```

3. Copy `.env` and fill in your values:

```env
API_BASE_URL=https://api.telegram.org
HOOK_ENDPOINT=https://your-site.com/api/v1/telegram-hook
HOOK_SECRET_TOKEN=your-telegram-webhook-secret
ALLOWED_TOKENS=bot123456:ABC-def,bot789012:GHI-jkl
HOST=0.0.0.0
PORT=5000

QUEUE_DB_PATH=/data/queue.db
MAX_RETRIES=8
WORKER_POLL_INTERVAL=0.1
WORKER_BATCH_SIZE=10

RATE_LIMIT_GLOBAL=28
RATE_LIMIT_CHAT_PRIVATE=1
RATE_LIMIT_CHAT_GROUP=20
RATE_LIMIT_CHAT_GROUP_WINDOW=60
```

4. Start the service:

```bash
docker compose up -d --build
```

The service listens on `127.0.0.1:5000`.

## Rate Limiting

The queue worker enforces three token-bucket rate limiters before every outbound call:

| Scope | Limit |
|---|---|
| Global (bot-wide) | 28 messages/second |
| Private chat | 1 message/second |
| Group / supergroup | 20 messages/minute |

When Telegram responds with `429` and a `retry_after` value, the worker waits exactly that duration plus a random jitter (0–500 ms) before retrying. Network and server errors use exponential backoff. Items that exceed `MAX_RETRIES` are marked as `failed`.

## Nginx Proxy Manager

1. Add a **Proxy Host**.
2. Domain: your domain (e.g. `tg-proxy.example.com`).
3. Forward to `http://telegram-proxy:5000`.
4. Enable SSL if desired.

## Security

### API Proxy — Token Whitelist

Set `ALLOWED_TOKENS` with a comma-separated list of bot tokens that are allowed through the proxy:

```env
ALLOWED_TOKENS=bot123456:ABC-def,bot789012:GHI-jkl
```

Requests for tokens not in this list get a `403 Forbidden`. Leave `ALLOWED_TOKENS` empty to allow all tokens (not recommended).

### Webhook Hook — Telegram Secret Token

Set `HOOK_SECRET_TOKEN` to the same value you pass as `secret_token` to `setWebhook`:

```bash
curl -F "url=https://tg-proxy.example.com/hook" \
     -F "secret_token=your-telegram-webhook-secret" \
     https://api.telegram.org/bot<TOKEN>/setWebhook
```

If the incoming `X-Telegram-Bot-Api-Secret-Token` header doesn't match, the request is rejected with `403`.

## Usage

### API Proxy (queue mode — default)

```python
import requests

resp = requests.post(
    "https://tg-proxy.example.com/bot<TOKEN>/sendMessage",
    json={"chat_id": 12345, "text": "Hello"},
)
# resp.status_code == 202
# resp.json() == {"status": "queued", "id": "uuid-here"}
```

Check delivery status:

```bash
curl https://tg-proxy.example.com/status/<uuid>
# {"id": "...", "status": "delivered", "attempt": 0}
```

### API Proxy (bypass mode — synchronous)

Add the `X-Bypass-Queue: true` header to get the real Telegram response immediately:

```python
resp = requests.post(
    "https://tg-proxy.example.com/bot<TOKEN>/sendMessage",
    json={"chat_id": 12345, "text": "Hello"},
    headers={"X-Bypass-Queue": "true"},
)
# resp.status_code == 200  (real Telegram response)
```

GET and HEAD requests always bypass the queue.

### Webhook Hook

Point your Telegram bot's webhook to:

```
https://tg-proxy.example.com/hook
```

The incoming update (headers + body) is synchronously forwarded to `HOOK_ENDPOINT` and the upstream response is relayed back.

## Project Structure

```
.
├── docker-compose.yaml
├── Dockerfile
├── .env
├── data/                  # SQLite queue storage (auto-created)
└── app/
    ├── main.py
    ├── msgqueue.py
    ├── ratelimiter.py
    ├── worker.py
    └── requirements.txt
```
