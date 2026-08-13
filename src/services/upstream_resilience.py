"""Small, auditable resilience primitives for public market-data upstreams.

The helpers in this module intentionally do not hide freshness.  A caller must
opt in to a last-good value and is responsible for exposing its age/provenance
to report consumers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, TypeVar

import requests


logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class UpstreamBusyError(RuntimeError):
    """Raised when a non-blocking upstream gate already has an in-flight call."""


class UpstreamCooldownError(RuntimeError):
    """Raised while an upstream is cooling down after a rate-limit response."""


def is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort classification without depending on a provider SDK type."""

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "too many requests",
            "rate limited",
            "rate limit",
            "http 429",
            "status code 429",
        )
    )


def is_retryable_http_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code in {429, 500, 502, 503, 504}:
        return True
    return isinstance(
        exc,
        (
            ConnectionError,
            TimeoutError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ),
    ) or any(
        marker in str(exc).lower()
        for marker in ("timed out", "timeout", "temporarily unavailable")
    )


class UpstreamRequestGate:
    """Serialize logical calls, add spacing, and open a short 429 cooldown.

    ``block_when_busy=False`` is useful around SDK calls that run in a bounded
    worker thread: when the caller times out, a later symbol must not queue a
    second SDK call behind the still-running first one.
    """

    def __init__(
        self,
        name: str,
        *,
        min_interval_seconds: float,
        jitter_seconds: float = 0.0,
        rate_limit_cooldown_seconds: float = 90.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.name = name
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.jitter_seconds = max(0.0, float(jitter_seconds))
        self.rate_limit_cooldown_seconds = max(0.0, float(rate_limit_cooldown_seconds))
        self._monotonic = monotonic
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0
        self._cooldown_until = 0.0

    def call(self, operation: Callable[[], _T], *, block_when_busy: bool = True) -> _T:
        acquired = self._lock.acquire(blocking=block_when_busy)
        if not acquired:
            raise UpstreamBusyError(f"{self.name} request already in flight")
        try:
            now = self._monotonic()
            if now < self._cooldown_until:
                remaining = self._cooldown_until - now
                raise UpstreamCooldownError(
                    f"{self.name} rate-limit cooldown active ({remaining:.1f}s remaining)"
                )
            delay = max(0.0, self._next_allowed_at - now)
            if delay > 0:
                self._sleep(delay)
            try:
                return operation()
            except Exception as exc:
                if is_rate_limit_error(exc):
                    self._cooldown_until = self._monotonic() + self.rate_limit_cooldown_seconds
                    logger.warning(
                        "[%s] upstream rate limit detected; cooldown %.1fs",
                        self.name,
                        self.rate_limit_cooldown_seconds,
                    )
                raise
            finally:
                interval = self.min_interval_seconds
                if self.jitter_seconds:
                    interval += self._random_uniform(0.0, self.jitter_seconds)
                self._next_allowed_at = self._monotonic() + interval
        finally:
            self._lock.release()

    def before_request(self) -> None:
        """Reserve one paced logical request without holding the lock over SDK I/O."""

        with self._lock:
            now = self._monotonic()
            if now < self._cooldown_until:
                remaining = self._cooldown_until - now
                raise UpstreamCooldownError(
                    f"{self.name} rate-limit cooldown active ({remaining:.1f}s remaining)"
                )
            delay = max(0.0, self._next_allowed_at - now)
            if delay > 0:
                self._sleep(delay)
            interval = self.min_interval_seconds
            if self.jitter_seconds:
                interval += self._random_uniform(0.0, self.jitter_seconds)
            self._next_allowed_at = self._monotonic() + interval

    def record_error(self, exc: BaseException) -> None:
        if not is_rate_limit_error(exc):
            return
        with self._lock:
            self._cooldown_until = self._monotonic() + self.rate_limit_cooldown_seconds
        logger.warning(
            "[%s] upstream rate limit detected; cooldown %.1fs",
            self.name,
            self.rate_limit_cooldown_seconds,
        )

    def reset(self) -> None:
        with self._lock:
            self._next_allowed_at = 0.0
            self._cooldown_until = 0.0


class PersistentJsonCache:
    """Atomic public-data cache with explicit age returned to the caller."""

    def __init__(self, namespace: str, *, cache_dir: Optional[Path] = None) -> None:
        root = cache_dir or Path(os.getenv("UPSTREAM_CACHE_DIR", "data/cache/upstreams"))
        self.root = Path(root) / namespace
        self._lock = threading.RLock()

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{self._digest(key)}.json"

    def put(self, key: str, value: Any) -> None:
        payload = {"stored_at": time.time(), "value": value}
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, path)

    def get(self, key: str, *, max_age_seconds: float) -> Optional[Tuple[Any, float]]:
        path = self._path(key)
        try:
            with self._lock:
                payload = json.loads(path.read_text(encoding="utf-8"))
            stored_at = float(payload["stored_at"])
            age = max(0.0, time.time() - stored_at)
            if age > max(0.0, float(max_age_seconds)):
                return None
            return payload.get("value"), age
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None


_YAHOO_REQUEST_GATE = UpstreamRequestGate(
    "yahoo",
    min_interval_seconds=2.0,
    jitter_seconds=0.75,
    rate_limit_cooldown_seconds=90.0,
)
_SEC_REQUEST_GATE = UpstreamRequestGate(
    "sec",
    min_interval_seconds=0.35,
    jitter_seconds=0.15,
    rate_limit_cooldown_seconds=30.0,
)
_HKEX_REQUEST_GATE = UpstreamRequestGate(
    "hkex",
    min_interval_seconds=1.0,
    jitter_seconds=0.5,
    rate_limit_cooldown_seconds=60.0,
)


def get_yahoo_request_gate() -> UpstreamRequestGate:
    return _YAHOO_REQUEST_GATE


def get_sec_request_gate() -> UpstreamRequestGate:
    return _SEC_REQUEST_GATE


def get_hkex_request_gate() -> UpstreamRequestGate:
    return _HKEX_REQUEST_GATE
