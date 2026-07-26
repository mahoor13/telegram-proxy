import os
import time
import threading

GLOBAL_CAPACITY = int(os.environ.get("RATE_LIMIT_GLOBAL", 28))
GLOBAL_REFILL = GLOBAL_CAPACITY

CHAT_PRIVATE_CAPACITY = int(os.environ.get("RATE_LIMIT_CHAT_PRIVATE", 1))
CHAT_PRIVATE_REFILL = CHAT_PRIVATE_CAPACITY

CHAT_GROUP_CAPACITY = int(os.environ.get("RATE_LIMIT_CHAT_GROUP", 20))
CHAT_GROUP_WINDOW = int(os.environ.get("RATE_LIMIT_CHAT_GROUP_WINDOW", 60))
CHAT_GROUP_REFILL = CHAT_GROUP_CAPACITY / CHAT_GROUP_WINDOW


class TokenBucket:
    def __init__(self, capacity, fill_rate):
        self.capacity = capacity
        self.fill_rate = fill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, tokens=1):
        with self.lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
        self.last_refill = now


class RateLimiterSet:
    def __init__(self):
        self.global_bucket = TokenBucket(GLOBAL_CAPACITY, GLOBAL_REFILL)
        self.chat_buckets = {}
        self.chat_locks = threading.Lock()

    def _get_chat_bucket(self, chat_key):
        if chat_key not in self.chat_buckets:
            if chat_key.startswith("group:") or chat_key.startswith("supergroup:"):
                cap, fill = CHAT_GROUP_CAPACITY, CHAT_GROUP_REFILL
            else:
                cap, fill = CHAT_PRIVATE_CAPACITY, CHAT_PRIVATE_REFILL
            self.chat_buckets[chat_key] = TokenBucket(cap, fill)
        return self.chat_buckets[chat_key]

    def acquire(self, chat_key=None):
        if not self.global_bucket.acquire():
            return False
        if chat_key:
            with self.chat_locks:
                bucket = self._get_chat_bucket(chat_key)
            if not bucket.acquire():
                return False
        return True
