# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}

_REPORT_DATE_KEYS = [
    "REPORT_DATE",
    "REPORTDATE",
    "报告期",
    "报告日期",
    "截止日期",
]
_FINANCIAL_FIELD_KEYS: Dict[str, List[str]] = {
    "revenue": [
        "TOTALOPERATEREVE",
        "operating_income_total",
        "营业总收入",
        "营业收入",
        "营收",
    ],
    "net_profit_parent": [
        "PARENTNETPROFIT",
        "parent_holder_net_profit",
        "归母净利润",
        "归属净利润",
        "母公司股东净利润",
    ],
    "revenue_yoy": [
        "TOTALOPERATEREVETZ",
        "calculate_operating_income_total_yoy_growth_ratio",
        "营业总收入同比增长",
        "营业收入同比",
        "营收同比",
        "收入同比",
    ],
    "net_profit_yoy": [
        "PARENTNETPROFITTZ",
        "calculate_parent_holder_net_profit_yoy_growth_ratio",
        "归属净利润同比增长",
        "归母净利润同比",
        "净利润同比",
        "净利同比",
    ],
    "roe": [
        "ROEJQ",
        "index_weighted_avg_roe",
        "净资产收益率",
        "ROE",
        "净资产收益",
    ],
    "gross_margin": ["XSMLL", "sale_gross_margin", "销售毛利率", "毛利率"],
    "operating_cash_flow_per_share": [
        "MGJYXJJE",
        "index_per_operating_cash_flow_net",
        "每股经营性现金流",
        "每股经营活动现金流量",
    ],
    "operating_cash_flow_yoy": [
        "MGJYXJJETZ",
        "每股经营现金流同比增长",
        "经营现金流同比",
    ],
    "operating_cash_flow": [
        "NETCASH_OPERATE",
        "经营活动产生的现金流量净额",
        "经营现金流",
        "经营活动现金流",
    ],
    "debt_ratio": ["ZCFZL", "assets_debt_ratio", "资产负债率"],
    "current_ratio": ["LD", "current_ratio", "流动比率"],
    "quick_ratio": ["SD", "quick_ratio", "速动比率"],
}


def _cn_market(stock_code: str) -> str:
    code = _normalize_code(stock_code)
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("0", "2", "3")):
        return "sz"
    return "sh"


def _code_with_exchange(stock_code: str, *, prefix: bool = False) -> str:
    code = _normalize_code(stock_code)
    market = _cn_market(code)
    if prefix:
        return f"{market.upper()}{code}"
    return f"{code}.{market.upper()}"


def _recent_report_periods(
    *,
    today: Optional[date] = None,
    limit: int = 5,
) -> List[str]:
    """Return recent completed quarter-end dates in YYYYMMDD form."""
    current = today or datetime.now().date()
    periods: List[date] = []
    for year in range(current.year, current.year - 3, -1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            candidate = date(year, month, day)
            if candidate <= current:
                periods.append(candidate)
    periods.sort(reverse=True)
    return [item.strftime("%Y%m%d") for item in periods[:max(1, limit)]]


def _institution_quarter(report_period: str) -> Optional[str]:
    text = re.sub(r"\D", "", _safe_str(report_period))
    if len(text) != 8:
        return None
    suffix_map = {"0331": "1", "0630": "2", "0930": "3", "1231": "4"}
    quarter = suffix_map.get(text[4:])
    return f"{text[:4]}{quarter}" if quarter else None


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _pick_value(row: pd.Series, keys: List[str]) -> Optional[Any]:
    """Prefer exact provider fields before falling back to Chinese aliases."""
    for key in keys:
        if key not in row.index:
            continue
        value = row.get(key)
        if value is not None and str(value).strip() not in ("", "-", "nan", "None"):
            return value
    return _pick_by_keywords(row, keys)


def _latest_row_by_report_date(
    df: pd.DataFrame,
    stock_code: str,
) -> Optional[pd.Series]:
    work_df = _filter_rows_by_code(df, stock_code)
    if work_df.empty:
        return None

    date_col = next(
        (
            col
            for col in work_df.columns
            if str(col) in _REPORT_DATE_KEYS
            or any(keyword in str(col) for keyword in ("报告期", "报告日期", "截止日期"))
        ),
        None,
    )
    if date_col is not None:
        parsed = pd.to_datetime(work_df[date_col], errors="coerce")
        if parsed.notna().any():
            return work_df.loc[parsed.idxmax()]
    return work_df.iloc[0]


def _financial_abstract_wide_to_row(df: pd.DataFrame) -> Optional[pd.Series]:
    """Transpose Sina's indicator-row/report-period-column layout."""
    if df is None or df.empty:
        return None
    indicator_col = next(
        (col for col in df.columns if str(col) in {"指标", "项目", "ITEM_NAME"}),
        None,
    )
    if indicator_col is None:
        return None

    period_candidates: List[Tuple[datetime, Any]] = []
    for col in df.columns:
        if col == indicator_col:
            continue
        text = re.sub(r"\D", "", str(col))
        if len(text) != 8:
            continue
        try:
            parsed = datetime.strptime(text, "%Y%m%d")
        except ValueError:
            continue
        period_candidates.append((parsed, col))
    if not period_candidates:
        return None

    _, latest_col = max(period_candidates, key=lambda item: item[0])
    values: Dict[str, Any] = {"报告期": str(latest_col)}
    for _, source_row in df.iterrows():
        indicator = _safe_str(source_row.get(indicator_col))
        if indicator:
            values[indicator] = source_row.get(latest_col)
    return pd.Series(values)


def _financial_ths_long_to_row(df: pd.DataFrame) -> Optional[pd.Series]:
    """Pivot the current THS metric-name/value layout for one latest period."""
    required = {"report_date", "metric_name", "value"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None
    parsed_dates = pd.to_datetime(df["report_date"], errors="coerce")
    if not parsed_dates.notna().any():
        return None
    latest_date = parsed_dates.max()
    latest = df[parsed_dates == latest_date]
    values: Dict[str, Any] = {
        "REPORT_DATE": latest_date.date().isoformat(),
    }
    for _, source_row in latest.iterrows():
        metric_name = _safe_str(source_row.get("metric_name"))
        if not metric_name:
            continue
        values[metric_name] = source_row.get("value")
        if metric_name == "index_per_operating_cash_flow_net":
            yoy = _safe_float(source_row.get("yoy"))
            if yoy is not None:
                values["MGJYXJJETZ"] = yoy * 100.0
    return pd.Series(values)


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    @staticmethod
    def _call_efinance_history_bill(
        stock_code: str,
    ) -> Tuple[Optional[pd.DataFrame], List[str]]:
        """Fetch free per-stock historical capital flow through efinance."""
        try:
            import efinance as ef
        except Exception as exc:
            return None, [f"import_efinance:{type(exc).__name__}"]

        try:
            df = ef.stock.get_history_bill(_normalize_code(stock_code))
        except Exception as exc:
            return None, [f"efinance_get_history_bill:{type(exc).__name__}"]
        if isinstance(df, pd.Series):
            df = df.to_frame().T
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df, []
        return None, ["efinance_get_history_bill:empty"]

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def _call_df_candidates_for_stock(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
        stock_code: str,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        """Continue probing until a global dataset contains the target stock."""
        errors: List[str] = []
        for candidate in candidates:
            df, source, candidate_errors = self._call_df_candidates([candidate])
            errors.extend(candidate_errors)
            if df is None:
                continue
            code_cols = [
                col
                for col in df.columns
                if any(
                    key in str(col)
                    for key in (
                        "代码",
                        "股票代码",
                        "证券代码",
                        "ts_code",
                        "symbol",
                    )
                )
            ]
            if not code_cols:
                return df, source, errors
            filtered = _filter_rows_by_code(df, stock_code)
            if not filtered.empty:
                return filtered, source, errors
        return None, None, errors

    @staticmethod
    def _empty_bundle() -> Dict[str, Any]:
        return {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

    def get_financial_bundle(self, stock_code: str) -> Dict[str, Any]:
        """Fetch fast per-stock financial indicators from free providers."""
        result = self._empty_bundle()
        code = _normalize_code(stock_code)
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            (
                "stock_financial_analysis_indicator_em",
                {"symbol": _code_with_exchange(code), "indicator": "按报告期"},
            ),
            (
                "stock_financial_abstract_new_ths",
                {"symbol": code, "indicator": "按报告期"},
            ),
            ("stock_financial_analysis_indicator", {"symbol": code}),
            ("stock_financial_abstract", {"symbol": code}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is None:
            return result

        row = None
        if fin_source == "stock_financial_abstract":
            row = _financial_abstract_wide_to_row(fin_df)
        elif fin_source == "stock_financial_abstract_new_ths":
            row = _financial_ths_long_to_row(fin_df)
        if row is None:
            row = _latest_row_by_report_date(fin_df, code)
        if row is None:
            result["errors"].append("financial_row_not_found")
            return result

        values = {
            key: _safe_float(_pick_value(row, aliases))
            for key, aliases in _FINANCIAL_FIELD_KEYS.items()
        }
        report_date = _normalize_report_date(_pick_value(row, _REPORT_DATE_KEYS))
        retrieved_at = datetime.now(timezone.utc).isoformat()
        growth_payload = {
            "report_date": report_date,
            "revenue_yoy": values["revenue_yoy"],
            "net_profit_yoy": values["net_profit_yoy"],
            "roe": values["roe"],
            "gross_margin": values["gross_margin"],
            "operating_cash_flow_yoy": values["operating_cash_flow_yoy"],
            "debt_ratio": values["debt_ratio"],
            "current_ratio": values["current_ratio"],
            "quick_ratio": values["quick_ratio"],
            "source_id": fin_source,
            "as_of": report_date,
            "retrieved_at": retrieved_at,
            "verification_status": "single_source",
        }
        growth_fields = (
            "revenue_yoy",
            "net_profit_yoy",
            "roe",
            "gross_margin",
            "operating_cash_flow_yoy",
            "debt_ratio",
            "current_ratio",
            "quick_ratio",
        )
        if any(growth_payload.get(key) is not None for key in growth_fields):
            result["growth"] = growth_payload
        financial_report_payload = {
            "report_date": report_date,
            "revenue": values["revenue"],
            "net_profit_parent": values["net_profit_parent"],
            "operating_cash_flow": values["operating_cash_flow"],
            "operating_cash_flow_per_share": values["operating_cash_flow_per_share"],
            "operating_cash_flow_yoy": values["operating_cash_flow_yoy"],
            "roe": values["roe"],
            "gross_margin": values["gross_margin"],
            "debt_ratio": values["debt_ratio"],
            "currency": "CNY",
            "source_id": fin_source,
            "as_of": report_date,
            "retrieved_at": retrieved_at,
            "verification_status": "single_source",
        }
        report_fields = (
            "revenue",
            "net_profit_parent",
            "operating_cash_flow",
            "operating_cash_flow_per_share",
            "operating_cash_flow_yoy",
            "roe",
            "gross_margin",
            "debt_ratio",
        )
        if any(
            financial_report_payload.get(key) is not None
            for key in report_fields
        ):
            result["earnings"]["financial_report"] = financial_report_payload
        if result["growth"] or result["earnings"]:
            result["source_chain"].append(f"financial:{fin_source}")
            result["status"] = "partial"
        return result

    def get_earnings_bundle(self, stock_code: str) -> Dict[str, Any]:
        """Fetch report-period-aware forecasts, quick reports and dividends."""
        result = self._empty_bundle()
        code = _normalize_code(stock_code)
        periods = _recent_report_periods(limit=5)

        forecast_candidates: List[Tuple[str, Dict[str, Any]]] = []
        for report_period in periods:
            forecast_candidates.append(("stock_yjyg_em", {"date": report_period}))
            forecast_candidates.append(("stock_yjbb_em", {"date": report_period}))
        forecast_df, forecast_source, forecast_errors = (
            self._call_df_candidates_for_stock(
                forecast_candidates,
                code,
            )
        )
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, code)
            if row is not None:
                summary = _safe_str(
                    _pick_by_keywords(
                        row,
                        ["业绩变动", "预告类型", "业绩变动原因", "预测指标", "摘要"],
                    )
                )[:300]
                if summary:
                    result["earnings"]["forecast_summary"] = summary
                forecast_date = _normalize_report_date(
                    _pick_by_keywords(row, ["报告期", "公告日期", "公告日"])
                )
                if forecast_date:
                    result["earnings"]["forecast_date"] = forecast_date
                result["source_chain"].append(
                    f"earnings_forecast:{forecast_source}"
                )

        quick_candidates = [
            ("stock_yjkb_em", {"date": report_period})
            for report_period in periods
        ]
        quick_df, quick_source, quick_errors = self._call_df_candidates_for_stock(
            quick_candidates,
            code,
        )
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, code)
            if row is not None:
                summary = _safe_str(
                    _pick_by_keywords(
                        row,
                        ["业绩快报", "营业收入同比", "净利润同比", "摘要", "说明"],
                    )
                )[:300]
                if summary:
                    result["earnings"]["quick_report_summary"] = summary
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": code}),
            (
                "stock_history_dividend_detail",
                {"symbol": code, "indicator": "分红", "date": ""},
            ),
            ("stock_dividend_cninfo", {"symbol": code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(
                dividend_df,
                code,
                max_events=5,
            )
            if dividend_payload:
                dividend_payload["source_id"] = dividend_source
                dividend_payload["retrieved_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                dividend_payload["verification_status"] = "single_source"
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        if result["earnings"]:
            result["status"] = "partial"
        return result

    def get_institution_bundle(self, stock_code: str) -> Dict[str, Any]:
        """Fetch recent institution and shareholder structure data."""
        result = self._empty_bundle()
        code = _normalize_code(stock_code)
        periods = _recent_report_periods(limit=5)
        quarters = [
            quarter
            for quarter in (_institution_quarter(item) for item in periods)
            if quarter
        ]

        inst_df, inst_source, inst_errors = self._call_df_candidates_for_stock(
            [
                ("stock_institute_hold", {"symbol": quarter})
                for quarter in quarters
            ],
            code,
        )
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, code)
            if row is not None:
                institution_payload = {
                    "institution_count": _safe_float(
                        _pick_by_keywords(row, ["机构数"])
                    ),
                    "institution_count_change": _safe_float(
                        _pick_by_keywords(row, ["机构数变化"])
                    ),
                    "institution_holding_ratio": _safe_float(
                        _pick_by_keywords(row, ["持股比例"])
                    ),
                    "institution_holding_change": _safe_float(
                        _pick_by_keywords(
                            row,
                            ["持股比例增幅", "占流通股比例增幅", "持股变化"],
                        )
                    ),
                }
                report_period = _safe_str(
                    _pick_by_keywords(
                        row,
                        ["报告期", "截止日期", "持股报告期"],
                    )
                )
                institution_payload = {
                    key: value
                    for key, value in institution_payload.items()
                    if value is not None
                }
                if institution_payload:
                    if report_period:
                        institution_payload["report_period"] = report_period
                    result["institution"].update(institution_payload)
                    result["source_chain"].append(
                        f"institution:{inst_source}"
                    )

        top10_candidates: List[Tuple[str, Dict[str, Any]]] = []
        prefix_code = _code_with_exchange(code, prefix=True).lower()
        for report_period in periods:
            top10_candidates.append(
                (
                    "stock_gdfx_top_10_em",
                    {"symbol": prefix_code, "date": report_period},
                )
            )
        top10_candidates.append(
            ("stock_zh_a_gdhs_detail_em", {"symbol": code})
        )
        top10_df, top10_source, top10_errors = self._call_df_candidates(
            top10_candidates
        )
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, code)
            if row is not None:
                holder_count = _safe_float(
                    _pick_by_keywords(row, ["股东户数", "本次股东户数"])
                )
                holder_change = _safe_float(
                    _pick_by_keywords(
                        row,
                        ["股东户数增减", "增减比例", "持股变化", "变动"],
                    )
                )
                if holder_count is not None:
                    result["institution"]["shareholder_count"] = holder_count
                if holder_change is not None:
                    result["institution"]["shareholder_count_change"] = (
                        holder_change
                    )
                if holder_count is not None or holder_change is not None:
                    result["source_chain"].append(
                        f"shareholders:{top10_source}"
                    )

        if result["institution"]:
            result["institution"].update({
                "source_id": ",".join(result["source_chain"]),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "verification_status": "single_source",
            })
            result["status"] = "partial"
        return result

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """Aggregate free AkShare blocks for direct callers.

        The manager runs the three methods separately so a slow optional
        endpoint cannot discard an already-successful financial indicator call.
        """
        result = self._empty_bundle()
        for block in (
            self.get_financial_bundle(stock_code),
            self.get_earnings_bundle(stock_code),
            self.get_institution_bundle(stock_code),
        ):
            result["growth"].update(block.get("growth") or {})
            result["earnings"].update(block.get("earnings") or {})
            result["institution"].update(block.get("institution") or {})
            result["source_chain"].extend(block.get("source_chain") or [])
            result["errors"].extend(block.get("errors") or [])
        if result["growth"] or result["earnings"] or result["institution"]:
            result["status"] = "partial"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        code = _normalize_code(stock_code)
        market = _cn_market(code)
        stock_df, stock_errors = self._call_efinance_history_bill(code)
        stock_source = "efinance_get_history_bill" if stock_df is not None else None
        if stock_df is None:
            stock_df, stock_source, akshare_stock_errors = (
                self._call_df_candidates_for_stock(
                    [
                        (
                            "stock_individual_fund_flow",
                            {"stock": code, "market": market},
                        ),
                        (
                            "stock_main_fund_flow",
                            {"symbol": "全部股票"},
                        ),
                    ],
                    code,
                )
            )
            stock_errors.extend(akshare_stock_errors)
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            work_df = _filter_rows_by_code(stock_df, code)
            date_col = next(
                (
                    col
                    for col in work_df.columns
                    if any(key in str(col) for key in ("日期", "交易日", "时间"))
                ),
                None,
            )
            if date_col is not None:
                parsed_dates = pd.to_datetime(work_df[date_col], errors="coerce")
                if parsed_dates.notna().any():
                    work_df = work_df.assign(_parsed_date=parsed_dates).sort_values(
                        "_parsed_date",
                        ascending=False,
                    )
            row = work_df.iloc[0] if not work_df.empty else None
            if row is not None:
                net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                flow_col = next(
                    (
                        col
                        for col in work_df.columns
                        if "主力净流入" in str(col) and "净额" in str(col)
                    ),
                    None,
                )
                if flow_col is None:
                    flow_col = next(
                        (
                            col
                            for col in work_df.columns
                            if any(
                                keyword in str(col)
                                for keyword in ("主力净流入", "主力净额", "净流入")
                            )
                        ),
                        None,
                    )
                flow_values = (
                    pd.to_numeric(work_df[flow_col], errors="coerce").dropna()
                    if flow_col is not None
                    else pd.Series(dtype=float)
                )
                inflow_5d = (
                    float(flow_values.head(5).sum())
                    if not flow_values.empty
                    else None
                )
                inflow_10d = (
                    float(flow_values.head(10).sum())
                    if not flow_values.empty
                    else None
                )
                if any(
                    value is not None
                    for value in (net_inflow, inflow_5d, inflow_10d)
                ):
                    result["stock_flow"] = {
                        "main_net_inflow": net_inflow,
                        "inflow_5d": inflow_5d,
                        "inflow_10d": inflow_10d,
                        "as_of": (
                            _normalize_report_date(row.get(date_col))
                            if date_col is not None
                            else None
                        ),
                        "source_id": stock_source,
                        "retrieved_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                    result["source_chain"].append(
                        f"capital_stock:{stock_source}"
                    )
                else:
                    result["errors"].append(
                        f"{stock_source}:flow_fields_missing"
                    )

        # Keep the fast per-stock flow when it succeeds. Sector-wide EastMoney
        # endpoints are substantially slower and a timeout would otherwise
        # discard the already-fetched stock signal.
        if not result["stock_flow"]:
            sector_df, sector_source, sector_errors = self._call_df_candidates([
                ("stock_sector_fund_flow_rank", {}),
                ("stock_sector_fund_flow_summary", {}),
            ])
            result["errors"].extend(sector_errors)
            if sector_df is not None:
                name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
                flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
                if name_col and flow_col:
                    work_df = sector_df[[name_col, flow_col]].copy()
                    work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                    work_df = work_df.dropna(subset=[flow_col])
                    top_df = work_df.nlargest(top_n, flow_col)
                    bottom_df = work_df.nsmallest(top_n, flow_col)
                    result["sector_rankings"] = {
                        "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                        "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                    }
                    result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
