from __future__ import annotations

import json

import pytest

from src.services.upstream_resilience import (
    PersistentJsonCache,
    UpstreamCooldownError,
    UpstreamRequestGate,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_request_gate_spaces_calls_and_opens_rate_limit_cooldown() -> None:
    clock = _Clock()
    gate = UpstreamRequestGate(
        "test",
        min_interval_seconds=2.0,
        jitter_seconds=0.0,
        rate_limit_cooldown_seconds=30.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert gate.call(lambda: "first") == "first"
    assert gate.call(lambda: "second") == "second"
    assert clock.value == 102.0

    with pytest.raises(RuntimeError, match="Too Many Requests"):
        gate.call(lambda: (_ for _ in ()).throw(RuntimeError("Too Many Requests")))
    with pytest.raises(UpstreamCooldownError):
        gate.call(lambda: "must not run")

    clock.value += 30.0
    assert gate.call(lambda: "recovered") == "recovered"


def test_persistent_json_cache_returns_value_and_rejects_expired_entry(tmp_path) -> None:
    cache = PersistentJsonCache("test", cache_dir=tmp_path)
    cache.put("AAPL", {"status": "ok", "value": 1})

    cached = cache.get("AAPL", max_age_seconds=60)
    assert cached is not None
    assert cached[0] == {"status": "ok", "value": 1}
    assert cached[1] >= 0

    cache_file = next((tmp_path / "test").glob("*.json"))
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    payload["stored_at"] = 1
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.get("AAPL", max_age_seconds=60) is None
