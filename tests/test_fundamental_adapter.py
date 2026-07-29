# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        dividend_payload = _build_dividend_payload(
            dividend_df,
            "600519",
            max_events=5,
        )
        with patch.object(
            adapter,
            "get_financial_bundle",
            return_value={
                "status": "partial",
                "growth": {
                    "revenue_yoy": 12.0,
                    "net_profit_yoy": 9.5,
                    "roe": 18.2,
                },
                "earnings": {
                    "financial_report": {
                        "report_date": within_ttm,
                        "revenue": 1000.0,
                        "net_profit_parent": 300.0,
                        "operating_cash_flow": 500.0,
                        "roe": 18.2,
                    }
                },
                "institution": {},
                "source_chain": ["financial:test"],
                "errors": [],
            },
        ), patch.object(
            adapter,
            "get_earnings_bundle",
            return_value={
                "status": "partial",
                "growth": {},
                "earnings": {
                    "forecast_summary": "预增",
                    "quick_report_summary": "快报摘要",
                    "dividend": dividend_payload,
                },
                "institution": {},
                "source_chain": ["earnings:test"],
                "errors": [],
            },
        ), patch.object(
            adapter,
            "get_institution_bundle",
            return_value=adapter._empty_bundle(),
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_financial_bundle_uses_report_period_endpoint_and_provider_fields(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fin_df = pd.DataFrame(
            {
                "SECUCODE": ["600519.SH"],
                "REPORT_DATE": ["2026-03-31"],
                "TOTALOPERATEREVE": [1000.0],
                "PARENTNETPROFIT": [300.0],
                "TOTALOPERATEREVETZ": [12.0],
                "PARENTNETPROFITTZ": [9.5],
                "ROEJQ": [18.2],
                "XSMLL": [91.3],
                "MGJYXJJE": [4.2],
                "MGJYXJJETZ": [8.0],
                "ZCFZL": [22.5],
            }
        )
        with patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(
                fin_df,
                "stock_financial_analysis_indicator_em",
                [],
            ),
        ) as mocked:
            result = adapter.get_financial_bundle("600519")

        first_candidate = mocked.call_args.args[0][0]
        self.assertEqual(first_candidate[0], "stock_financial_analysis_indicator_em")
        self.assertEqual(
            first_candidate[1],
            {"symbol": "600519.SH", "indicator": "按报告期"},
        )
        report = result["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2026-03-31")
        self.assertEqual(report["revenue"], 1000.0)
        self.assertEqual(report["net_profit_parent"], 300.0)
        self.assertEqual(report["operating_cash_flow_per_share"], 4.2)
        self.assertEqual(report["verification_status"], "single_source")
        self.assertEqual(result["growth"]["operating_cash_flow_yoy"], 8.0)

    def test_financial_bundle_transposes_legacy_abstract_layout(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fin_df = pd.DataFrame(
            {
                "选项": ["常用指标", "常用指标", "盈利能力"],
                "指标": ["归母净利润", "营业总收入", "净资产收益率"],
                "20260331": [300.0, 1000.0, 18.2],
                "20251231": [250.0, 900.0, 17.0],
            }
        )
        with patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(fin_df, "stock_financial_abstract", []),
        ):
            result = adapter.get_financial_bundle("600519")

        report = result["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2026-03-31")
        self.assertEqual(report["revenue"], 1000.0)
        self.assertEqual(report["net_profit_parent"], 300.0)
        self.assertEqual(report["roe"], 18.2)

    def test_financial_bundle_pivots_ths_long_layout(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fin_df = pd.DataFrame(
            [
                {
                    "report_date": "2026-03-31",
                    "metric_name": "operating_income_total",
                    "value": "1000",
                    "yoy": "0.12",
                },
                {
                    "report_date": "2026-03-31",
                    "metric_name": "parent_holder_net_profit",
                    "value": "300",
                    "yoy": "0.095",
                },
                {
                    "report_date": "2026-03-31",
                    "metric_name": "index_per_operating_cash_flow_net",
                    "value": "4.2",
                    "yoy": "-0.15",
                },
                {
                    "report_date": "2025-12-31",
                    "metric_name": "operating_income_total",
                    "value": "900",
                    "yoy": "0.1",
                },
            ]
        )
        with patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(
                fin_df,
                "stock_financial_abstract_new_ths",
                [],
            ),
        ):
            result = adapter.get_financial_bundle("600519")

        report = result["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2026-03-31")
        self.assertEqual(report["revenue"], 1000.0)
        self.assertEqual(report["net_profit_parent"], 300.0)
        self.assertEqual(report["operating_cash_flow_per_share"], 4.2)
        self.assertEqual(report["operating_cash_flow_yoy"], -15.0)

    def test_earnings_bundle_uses_explicit_report_dates(self) -> None:
        adapter = AkshareFundamentalAdapter()
        empty = pd.DataFrame()
        with patch.object(
            adapter,
            "_call_df_candidates_for_stock",
            side_effect=[(None, None, []), (None, None, [])],
        ) as stock_candidates, patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(empty, None, []),
        ):
            adapter.get_earnings_bundle("600519")

        forecast_candidates = stock_candidates.call_args_list[0].args[0]
        quick_candidates = stock_candidates.call_args_list[1].args[0]
        self.assertTrue(forecast_candidates)
        self.assertTrue(quick_candidates)
        self.assertTrue(
            all("date" in kwargs and "symbol" not in kwargs for _, kwargs in forecast_candidates)
        )
        self.assertTrue(
            all("date" in kwargs and "symbol" not in kwargs for _, kwargs in quick_candidates)
        )

    def test_capital_flow_routes_shenzhen_and_aggregates_latest_windows(self) -> None:
        adapter = AkshareFundamentalAdapter()
        stock_df = pd.DataFrame(
            {
                "日期": pd.date_range("2026-07-01", periods=10, freq="D"),
                "主力净流入-净额": list(range(1, 11)),
            }
        )
        with patch.object(
            adapter,
            "_call_efinance_history_bill",
            return_value=(None, []),
        ), patch.object(
            adapter,
            "_call_df_candidates_for_stock",
            return_value=(stock_df, "stock_individual_fund_flow", []),
        ) as stock_call, patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(None, None, []),
        ):
            result = adapter.get_capital_flow("002460")

        stock_candidates = stock_call.call_args.args[0]
        self.assertEqual(
            stock_candidates[0],
            (
                "stock_individual_fund_flow",
                {"stock": "002460", "market": "sz"},
            ),
        )
        flow = result["stock_flow"]
        self.assertEqual(flow["main_net_inflow"], 10.0)
        self.assertEqual(flow["inflow_5d"], 40.0)
        self.assertEqual(flow["inflow_10d"], 55.0)

    def test_capital_flow_prefers_fast_efinance_history(self) -> None:
        adapter = AkshareFundamentalAdapter()
        stock_df = pd.DataFrame(
            {
                "日期": pd.date_range("2026-07-01", periods=10, freq="D"),
                "主力净流入": list(range(1, 11)),
            }
        )
        with patch.object(
            adapter,
            "_call_efinance_history_bill",
            return_value=(stock_df, []),
        ), patch.object(
            adapter,
            "_call_df_candidates_for_stock",
        ) as akshare_stock_call, patch.object(
            adapter,
            "_call_df_candidates",
        ) as sector_call:
            result = adapter.get_capital_flow("600089")

        akshare_stock_call.assert_not_called()
        sector_call.assert_not_called()
        self.assertEqual(
            result["stock_flow"]["source_id"],
            "efinance_get_history_bill",
        )
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 10.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 40.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 55.0)


if __name__ == "__main__":
    unittest.main()
