import os
import time
import json
import logging
import random
import threading

import requests
from msgqueue import dequeue_ready, mark_delivered, mark_failed, reschedule
from ratelimiter import RateLimiterSet

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 8))
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL", 0.1))
BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", 10))

logger = logging.getLogger("worker")


class DeliveryWorker(threading.Thread):
    def __init__(self, api_base_url, rate_limiter=None):
        super().__init__(daemon=True)
        self.api_base_url = api_base_url
        self.rate_limiter = rate_limiter or RateLimiterSet()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        logger.info("Worker started")
        while not self._stop_event.is_set():
            try:
                self._process_batch()
            except Exception:
                logger.exception("Worker error")
            self._stop_event.wait(POLL_INTERVAL)
        logger.info("Worker stopped")

    def _process_batch(self):
        items = dequeue_ready(BATCH_SIZE)
        for item in items:
            if self._stop_event.is_set():
                return
            self._process_item(item)

    def _process_item(self, item):
        chat_key = None
        if item["chat_id"] and item["chat_type"]:
            chat_key = f"{item['chat_type']}:{item['chat_id']}"

        if not self.rate_limiter.acquire(chat_key):
            return

        headers = json.loads(item["headers"]) if item["headers"] else {}
        body = item["body"] if item["body"] else b""

        url = f"{self.api_base_url}/{item['path']}"

        try:
            resp = requests.request(
                method=item["method"],
                url=url,
                headers=headers,
                data=body,
                timeout=60,
            )
        except requests.RequestException as e:
            if item["attempt"] >= MAX_RETRIES:
                mark_failed(item["id"], str(e))
                return
            backoff = min(300, 2 ** item["attempt"]) + random.randint(0, 1)
            reschedule(item["id"], time.time() + backoff)
            logger.warning("Request error for %s (attempt %d): %s", item["id"], item["attempt"], e)
            return

        if resp.status_code == 200:
            mark_delivered(item["id"])
            logger.info("Delivered %s — 200", item["id"])
            return

        body_data = resp.json() if resp.text else {}

        if resp.status_code == 429:
            retry_after = int(
                body_data.get("parameters", {}).get("retry_after", 1)
            ) if isinstance(body_data, dict) else 1
            jitter = random.randint(0, 500) / 1000.0
            next_at = time.time() + retry_after + jitter
            reschedule(item["id"], next_at)
            logger.warning("429 for %s — retry_after %ds", item["id"], retry_after)
            return

        if 500 <= resp.status_code < 600:
            if item["attempt"] >= MAX_RETRIES:
                mark_failed(item["id"], resp.text[:200])
                return
            backoff = min(300, 2 ** item["attempt"]) + random.randint(0, 1)
            reschedule(item["id"], time.time() + backoff)
            logger.warning("5xx for %s (attempt %d): %s", item["id"], item["attempt"], resp.status_code)
            return

        mark_failed(item["id"], f"HTTP {resp.status_code}: {resp.text[:200]}")
        logger.warning("Permanent error for %s: HTTP %d", item["id"], resp.status_code)
