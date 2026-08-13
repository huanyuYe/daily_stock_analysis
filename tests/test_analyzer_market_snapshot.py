from src.analyzer import GeminiAnalyzer


def test_market_snapshot_keeps_realtime_change_fields_atomic() -> None:
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    context = {
        "date": "2026-08-11",
        "today": {
            "close": 44.27,
            "open": 47.23,
            "high": 47.45,
            "low": 44.13,
            "pct_chg": -8.57,
        },
        "yesterday": {"close": 42.19},
        "realtime": {
            "price": 44.27,
            "pre_close": 48.42,
            "change_pct": -8.57,
            "source": "tencent",
        },
    }

    snapshot = analyzer._build_market_snapshot(context)

    assert snapshot["close"] == "44.27"
    assert snapshot["prev_close"] == "48.42"
    assert snapshot["change_amount"] == "-4.15"
    assert snapshot["pct_chg"] == "-8.57%"


def test_market_snapshot_derives_previous_close_from_same_quote_percentage() -> None:
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    context = {
        "date": "2026-08-11",
        "today": {"close": 105.0, "pct_chg": 5.0},
        "yesterday": {"close": 90.0},
        "realtime": {"price": 105.0, "change_pct": 5.0, "source": "test"},
    }

    snapshot = analyzer._build_market_snapshot(context)

    assert snapshot["prev_close"] == "100.00"
    assert snapshot["change_amount"] == "5.00"
    assert snapshot["pct_chg"] == "5.00%"
