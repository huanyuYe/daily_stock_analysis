from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from src.brokers.futu.options import fetch_futu_option_snapshot


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.rows)


class _QuoteContext:
    def __init__(self, *, host, port):
        assert (host, port) == ("127.0.0.1", 11111)
        self.snapshot_codes = []

    def get_option_chain(self, code, *, start, end):
        assert code == "US.OKLO"
        assert (start, end) == ("2026-08-09", "2026-09-07")
        return 0, _Rows(
            [
                {"code": "US.OLD", "strike_time": "2026-08-08"},
                {"code": "US.C1", "strike_time": "2026-08-14"},
                {"code": "US.P1", "strike_time": "2026-08-14"},
                {"code": "US.C2", "strike_time": "2026-08-21"},
            ]
        )

    def get_market_snapshot(self, codes):
        self.snapshot_codes.extend(codes)
        return 0, _Rows([{"code": code, "volume": 1} for code in codes])


def test_futu_option_snapshot_selects_one_expiry_and_reads_only_its_contracts():
    contexts = []

    def context_factory(**kwargs):
        context = _QuoteContext(**kwargs)
        contexts.append(context)
        return context

    futu_module = SimpleNamespace(OpenQuoteContext=context_factory, RET_OK=0)
    with (
        patch.dict("sys.modules", {"futu": futu_module}),
        patch("src.brokers.futu.options._connection_settings", return_value=("127.0.0.1", 11111)),
        patch("src.brokers.futu.options._safe_close") as close,
    ):
        payload = fetch_futu_option_snapshot(
            "OKLO",
            start=date(2026, 8, 9),
            end=date(2026, 9, 7),
            preferred_expiry=date(2026, 8, 20),
        )

    assert payload["expiry"] == "2026-08-21"
    assert payload["contracts"] == [{"code": "US.C2", "volume": 1}]
    assert contexts[0].snapshot_codes == ["US.C2"]
    close.assert_called_once_with(contexts[0])
