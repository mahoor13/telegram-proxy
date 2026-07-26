import os
import json
import logging

import requests
from flask import Flask, request, Response, jsonify

from msgqueue import init_db, enqueue, get_item
from worker import DeliveryWorker
from ratelimiter import RateLimiterSet

app = Flask(__name__)

API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.telegram.org")
HOOK_ENDPOINT = os.environ.get("HOOK_ENDPOINT", "")
HOOK_SECRET_TOKEN = os.environ.get("HOOK_SECRET_TOKEN", "")
ALLOWED_TOKENS = {t.strip() for t in os.environ.get("ALLOWED_TOKENS", "").split(",") if t.strip()}

HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

_worker_started = False


def _start_worker():
    global _worker_started
    if _worker_started:
        return
    init_db()
    rate_limiter = RateLimiterSet()
    worker = DeliveryWorker(API_BASE_URL, rate_limiter)
    worker.start()
    _worker_started = True


def _token_from_path(path):
    if path.startswith("bot"):
        token = path.split("/")[0][3:]
    elif path.startswith("file/bot"):
        token = path.split("/")[2]
    else:
        token = ""
    return token


def _extract_chat_info(body):
    chat_id = None
    chat_type = None
    try:
        data = json.loads(body) if body else {}
        if isinstance(data, dict):
            chat_id = data.get("chat_id")
            if data.get("chat_type"):
                chat_type = data["chat_type"]
            elif chat_id is not None:
                if isinstance(chat_id, int) and chat_id > 0:
                    chat_type = "private"
                else:
                    chat_type = "group"
    except (json.JSONDecodeError, TypeError):
        pass
    return chat_id, chat_type


def _forward_sync(path):
    url = f"{API_BASE_URL}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP}
    headers.pop("X-Forwarded-For", None)
    headers.pop("X-Bypass-Queue", None)

    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            cookies=request.cookies,
            stream=True,
            timeout=60,
        )
    except requests.RequestException as e:
        logging.error("Proxy error: %s", e)
        return Response(str(e), status=502, content_type="text/plain")

    excluded = {"transfer-encoding", "connection", "keep-alive", "content-encoding"}
    response_headers = [
        (k, v) for k, v in resp.raw.headers.items()
        if k.lower() not in excluded
    ]

    return Response(
        resp.content,
        status=resp.status_code,
        headers=response_headers,
    )


def _parse_body():
    raw = request.get_data()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


@app.route("/hook", methods=["POST"])
def hook():
    if not HOOK_ENDPOINT:
        return Response("HOOK_ENDPOINT not configured", status=500)

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if HOOK_SECRET_TOKEN and secret != HOOK_SECRET_TOKEN:
        return Response("unauthorized", status=403)

    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP}
    headers.pop("X-Forwarded-For", None)
    headers.pop("X-Bypass-Queue", None)

    try:
        resp = requests.request(
            method="POST",
            url=HOOK_ENDPOINT,
            headers=headers,
            data=request.get_data(),
            timeout=60,
        )
    except requests.RequestException as e:
        logging.error("Hook proxy error: %s", e)
        return Response(str(e), status=502, content_type="text/plain")

    excluded = {"transfer-encoding", "connection", "keep-alive", "content-encoding"}
    response_headers = [
        (k, v) for k, v in resp.raw.headers.items()
        if k.lower() not in excluded
    ]

    return Response(resp.content, status=resp.status_code, headers=response_headers)


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def proxy(path):
    _start_worker()

    token = _token_from_path(path)
    if token and ALLOWED_TOKENS and token not in ALLOWED_TOKENS:
        return Response("unauthorized token", status=403)

    bypass = request.headers.get("X-Bypass-Queue", "")
    if bypass or request.method in ("GET", "HEAD"):
        return _forward_sync(path)

    body = _parse_body()
    chat_id, chat_type = _extract_chat_info(body)

    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP}
    headers.pop("X-Forwarded-For", None)
    headers.pop("X-Bypass-Queue", None)

    item_id = enqueue(
        method=request.method,
        path=path,
        headers=headers,
        body=body if isinstance(body, str) else body,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    return jsonify({"status": "queued", "id": item_id}), 202


@app.route("/status/<item_id>")
def status(item_id):
    item = get_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": item["id"],
        "status": item["status"],
        "attempt": item["attempt"],
    })


@app.route("/")
def root():
    return {"status": "proxy running"}


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5000))
    _start_worker()
    app.run(host=host, port=port, debug=False)
