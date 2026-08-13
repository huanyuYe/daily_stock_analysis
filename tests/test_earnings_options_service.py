from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.agent.executor import AgentExecutor
from src.analyzer import AnalysisResult
from src.notification import NotificationService
from src.services.earnings_options_service import (
    EarningsOptionsService,
    build_earnings_options_report_view,
    estimate_long_option_profit_probability,
    format_earnings_options_prompt_section,
)
from src.services.report_renderer import render


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._rows)


class _EarningsDates:
    index = [datetime(2026, 8, 14, 16, 0, tzinfo=ZoneInfo("America/New_York"))]


class _Ticker:
    calendar = {
        "Earnings Date": [datetime(2026, 8, 14)],
        "Earnings Call Time": "AMC",
        "Earnings Average": -0.12,
        "Earnings Low": -0.20,
        "Earnings High": -0.05,
        "Revenue Average": 15_000_000,
    }
    options = ("2026-08-14", "2026-08-21")
    fast_info = {"last_price": 100.0, "previous_close": 98.0}

    def get_earnings_dates(self, limit):
        assert limit == 8
        return _EarningsDates()

    def option_chain(self, expiry):
        assert expiry == "2026-08-14"
        common = {
            "bid": 9.8,
            "ask": 10.2,
            "lastPrice": 10.0,
            "volume": 200,
            "openInterest": 100,
            "impliedVolatility": 0.8,
            "lastTradeDate": datetime(2026, 8, 8, 15, 30, tzinfo=ZoneInfo("America/New_York")),
            "inTheMoney": True,
        }
        return SimpleNamespace(
            calls=_Rows([{**common, "contractSymbol": "OKLO260814C00080000", "strike": 80.0}]),
            puts=_Rows([{**common, "contractSymbol": "OKLO260814P00120000", "strike": 120.0}]),
        )


def _context():
    service = EarningsOptionsService(
        ticker_factory=lambda symbol: _Ticker(),
        now_factory=lambda: datetime(2026, 8, 9, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        profit_probability_threshold=0.50,
    )
    return service.analyze(
        "OKLO",
        realtime_quote={"price": 100.0, "pre_close": 98.0, "change_pct": 2.04},
        market_phase_context={"phase": "premarket"},
    )


def test_service_selects_earnings_adjacent_expiry_and_logs_cost_probability_inputs():
    payload = _context()

    assert payload["status"] == "ok"
    assert payload["earnings"]["date"] == "2026-08-14"
    assert payload["earnings"]["estimates"]["eps_average"] == -0.12
    assert payload["expiry"]["date"] == "2026-08-14"
    assert payload["expiry"]["is_earnings_adjacent"] is True
    assert payload["underlying"]["change_pct"] == 2.04
    assert payload["activity"]["call_volume"] == 200
    assert payload["activity"]["put_volume"] == 200
    assert len(payload["high_probability_contracts"]) == 2
    assert payload["high_probability_contracts"][0]["max_loss_per_contract"] == 1000.0
    assert payload["high_probability_contracts"][0]["one_sigma_net_profit_per_contract"] is not None
    assert len(payload["recent_unusual_activity"]) == 2
    assert payload["verification_status"] == "single_source"


def test_probability_is_bounded_and_requires_valid_inputs():
    probability = estimate_long_option_profit_probability(
        option_type="call",
        spot=100,
        strike=80,
        premium=10,
        implied_volatility=0.8,
        years_to_expiry=5 / 365,
    )
    assert probability is not None and 0.5 < probability < 1.0
    assert estimate_long_option_profit_probability(
        option_type="put",
        spot=100,
        strike=100,
        premium=0,
        implied_volatility=0.8,
        years_to_expiry=5 / 365,
    ) is None


def test_prompt_and_agent_prompt_have_phase_label_and_model_warning():
    payload = _context()
    section = format_earnings_options_prompt_section(payload, report_language="zh")
    assert "盘前财报与邻近到期期权重点" in section
    assert "IV crush" in section
    assert "不是历史胜率" in section

    executor = object.__new__(AgentExecutor)
    message = executor._build_user_message(
        "分析 OKLO",
        {"stock_code": "OKLO", "report_type": "pre_market", "earnings_options_context": payload},
    )
    assert "盘前财报与邻近到期期权重点" in message
    assert "OKLO260814C00080000" in message


def test_report_view_and_markdown_render_include_dedicated_phase_section():
    payload = _context()
    view = build_earnings_options_report_view(payload, report_language="zh")
    assert view and view["title"] == "盘前财报与邻近到期期权重点"

    result = AnalysisResult(
        code="OKLO",
        name="Oklo",
        sentiment_score=60,
        trend_prediction="看多",
        operation_advice="持有",
        dashboard={},
        analysis_summary="测试",
        earnings_options_context=payload,
    )
    report = render("markdown", [result], report_date="2026-08-09")
    assert report is not None
    assert "盘前财报与邻近到期期权重点" in report
    assert "max loss/contract 1000.0" in report

    fallback_report = NotificationService().generate_dashboard_report(
        [result],
        report_date="2026-08-09",
    )
    assert "盘前财报与邻近到期期权重点" in fallback_report
    assert "max_loss/contract=1000.0" in fallback_report


def test_service_is_fail_open_for_disabled_or_non_us_symbols():
    disabled = EarningsOptionsService(enabled=False)
    assert disabled.analyze("OKLO")["status"] == "disabled"
    assert EarningsOptionsService().analyze("600519")["status"] == "unsupported"


def test_service_uses_explicitly_marked_last_good_after_fresh_failure(tmp_path):
    successful = EarningsOptionsService(
        ticker_factory=lambda symbol: _Ticker(),
        now_factory=lambda: datetime(2026, 8, 9, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        persistent_cache_dir=tmp_path,
    )
    assert successful.analyze("OKLO", realtime_quote={"price": 100.0})["status"] == "ok"

    def fail(_symbol):
        raise RuntimeError("Too Many Requests")

    degraded = EarningsOptionsService(
        ticker_factory=fail,
        now_factory=lambda: datetime(2026, 8, 9, 10, 5, tzinfo=ZoneInfo("America/New_York")),
        persistent_cache_dir=tmp_path,
    ).analyze(
        "OKLO",
        realtime_quote={"price": 100.0},
        market_phase_context={"phase": "intraday"},
    )

    assert degraded["status"] == "ok"
    assert degraded["cache_status"] == "last_good"
    assert degraded["stale"] is True
    assert degraded["verification_status"] == "single_source_stale"
    assert degraded["fresh_fetch_error"] == "RuntimeError"
    assert "using_stale_last_good_after_fresh_fetch_failure" in degraded["warnings"]
    assert degraded["market_phase_context"]["phase"] == "intraday"


def test_service_uses_read_only_futu_option_snapshot_after_yahoo_failure():
    def fail_yahoo(_symbol):
        raise RuntimeError("Too Many Requests")

    def load_futu(symbol, *, start, end, preferred_expiry):
        assert symbol == "OKLO"
        assert start.isoformat() == "2026-08-09"
        assert end.isoformat() == "2026-09-07"
        assert preferred_expiry is None
        common = {
            "last_price": 2.3,
            "bid_price": 2.2,
            "ask_price": 2.4,
            "volume": 200,
            "option_open_interest": 100,
            "option_implied_volatility": 100.0,
            "update_time": "2026-08-07 15:59:00",
        }
        return {
            "expiry": "2026-08-14",
            "contracts": [
                {
                    **common,
                    "code": "US.OKLO260814C48000",
                    "option_type": "CALL",
                    "option_strike_price": 48.0,
                },
                {
                    **common,
                    "code": "US.OKLO260814P48500",
                    "option_type": "PUT",
                    "option_strike_price": 48.5,
                },
            ],
        }

    payload = EarningsOptionsService(
        ticker_factory=fail_yahoo,
        futu_fallback_enabled=True,
        futu_snapshot_loader=load_futu,
        profit_probability_threshold=0.10,
        now_factory=lambda: datetime(2026, 8, 9, 10, 0, tzinfo=ZoneInfo("America/New_York")),
    ).analyze(
        "OKLO",
        realtime_quote={"price": 48.42, "pre_close": 48.0},
    )

    assert payload["status"] == "ok"
    assert payload["source_id"] == "futu_opend_options"
    assert payload["source_tier"] == "broker_market_data"
    assert payload["verification_status"] == "single_source_delayed"
    assert payload["as_of"] == "2026-08-07T19:59:00+00:00"
    assert payload["earnings"]["date"] is None
    assert payload["expiry"]["date"] == "2026-08-14"
    assert payload["activity"]["call_volume"] == 200
    assert payload["activity"]["put_volume"] == 200
    assert payload["activity"]["contracts_observed"] == 2
    assert payload["high_probability_contracts"]
    assert "earnings_calendar_unavailable_after_yahoo_failure" in payload["warnings"]
    assert "yahoo_fresh_fetch_failed:RuntimeError" in payload["warnings"]
    prompt = format_earnings_options_prompt_section(payload, report_language="zh")
    assert "earnings gap=N/A" in prompt
    assert "N/Ad" not in prompt
    view = build_earnings_options_report_view(payload, report_language="zh")
    assert view is not None
    assert "Futu OpenD 只读延迟行情" in view["labels"]["source_note"]
