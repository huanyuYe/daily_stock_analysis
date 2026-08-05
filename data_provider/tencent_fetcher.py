# -*- coding: utf-8 -*-
"""Tencent direct daily K-line fetcher for A-share fallback routing."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import exchange_calendars as xcals
except ImportError:  # pragma: no cover - dependency is present in supported installs
    xcals = None

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code, is_bse_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from .us_index_mapping import is_us_stock_code

logger = logging.getLogger(__name__)

_MAX_KLINE_BARS = 800


class TencentFetcher(BaseFetcher):
    """Fetch qfq daily K-line data from Tencent's direct quote endpoint."""

    name = "TencentFetcher"
    # This direct endpoint is the last-resort A-share daily fallback. Keeping
    # it at priority 0 made a single Efinance failure skip the richer built-in
    # fallback chain and try Tencent before AkShare/PyTDX/Baostock/YFinance.
    priority = 5
    allow_empty_daily_data = True

    _KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    _QUOTE_ENDPOINT = "https://qt.gtimg.cn/q="
    _HTTP_TIMEOUT_SECONDS = 8

    def __init__(self) -> None:
        self.priority = _read_tencent_priority()

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = normalize_stock_code(stock_code)
        symbol = _to_tencent_symbol(code)
        if not symbol:
            raise DataFetchError(f"TencentFetcher unsupported stock code: {stock_code}")
        if symbol.startswith("us") and "." not in symbol:
            symbol = self._resolve_us_exchange_symbol(symbol)

        lookback = _estimate_lookback_days(start_date=start_date, end_date=end_date)
        explicit_start = _format_tencent_date(start_date)
        explicit_end = _format_tencent_date(end_date)
        explicit_window = (
            f"{explicit_start},{explicit_end}"
            if explicit_start and explicit_end
            else ","
        )
        response = requests.get(
            self._KLINE_ENDPOINT,
            params={"param": f"{symbol},day,{explicit_window},{lookback},qfq"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
            timeout=self._HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        rows = _extract_kline_rows(payload, symbol=symbol)
        if not rows:
            logger.info("TencentFetcher empty daily history for %s", stock_code)
            return _empty_daily_frame()

        df = pd.DataFrame(rows)
        first_returned_date = _first_returned_date(df)
        if first_returned_date and _is_capped_history_incomplete(
            first_returned_date=first_returned_date,
            start_date=start_date,
            lookback=lookback,
            returned_rows=len(rows),
        ):
            logger.info(
                "TencentFetcher incomplete capped daily history for %s: first_date=%s requested_start=%s",
                stock_code,
                first_returned_date,
                start_date,
            )
            return _empty_daily_frame()

        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if df.empty:
            logger.info(
                "TencentFetcher daily history outside requested range for %s: %s~%s",
                stock_code,
                start_date,
                end_date,
            )
            return _empty_daily_frame()
        return df

    def _resolve_us_exchange_symbol(self, symbol: str) -> str:
        """Resolve Tencent's exchange suffix (for example ``NVDA.OQ``)."""
        quote_fields = self._fetch_quote_fields(symbol)
        provider_code = quote_fields[2].strip() if len(quote_fields) > 2 else ""
        if not provider_code or "." not in provider_code:
            raise DataFetchError(f"TencentFetcher cannot resolve US exchange for {symbol}")
        return f"us{provider_code}"

    def _fetch_quote_fields(self, symbol: str) -> list[str]:
        response = requests.get(
            f"{self._QUOTE_ENDPOINT}{symbol}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,*/*"},
            timeout=self._HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.content.decode("gb18030", "ignore").strip()
        if '="' not in payload:
            return []
        body = payload.split('="', 1)[1].rsplit('";', 1)[0]
        return body.split("~")

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """Fetch public delayed/real-time quote data for HK and US stocks."""
        symbol = _to_tencent_symbol(stock_code)
        if not symbol or not symbol.startswith(("hk", "us")):
            return None
        fields = self._fetch_quote_fields(symbol)
        if len(fields) < 38:
            return None

        price = safe_float(fields[3])
        if price is None or price <= 0:
            return None
        market = "hk" if symbol.startswith("hk") else "us"
        provider_timestamp = _parse_provider_timestamp(
            fields[30],
            market=market,
        )
        missing_fields = [
            key
            for key, value in {
                "price": price,
                "prev_close": safe_float(fields[4]),
                "volume": safe_int(fields[6]),
                "amount": safe_float(fields[37]),
            }.items()
            if value is None
        ]
        return UnifiedRealtimeQuote(
            code=normalize_stock_code(stock_code),
            name=fields[1].strip(),
            source=RealtimeSource.TENCENT,
            provider_timestamp=provider_timestamp,
            market=market,
            currency=(fields[35].strip().upper() or ("HKD" if market == "hk" else "USD")),
            data_quality="partial" if missing_fields else "ok",
            missing_fields=missing_fields or None,
            price=price,
            change_pct=safe_float(fields[32]),
            change_amount=safe_float(fields[31]),
            volume=safe_int(fields[6]),
            amount=safe_float(fields[37]),
            amplitude=safe_float(fields[43]) if len(fields) > 43 else None,
            open_price=safe_float(fields[5]),
            high=safe_float(fields[33]),
            low=safe_float(fields[34]),
            pre_close=safe_float(fields[4]),
            pe_ratio=safe_float(fields[39]) if len(fields) > 39 else None,
            total_mv=_market_value_to_base_currency(fields[45]) if len(fields) > 45 else None,
            circ_mv=_market_value_to_base_currency(fields[44]) if len(fields) > 44 else None,
        )

    def get_main_indices(self, region: str = "cn") -> Optional[list[dict[str, Any]]]:
        """Fetch HK/US headline indices from Tencent's public quote endpoint."""
        mappings = {
            "hk": [
                ("hkHSI", "HSI", "恒生指数"),
                ("hkHSTECH", "HSTECH", "恒生科技指数"),
                ("hkHSCEI", "HSCEI", "国企指数"),
            ],
            "us": [
                ("usINX", "SPX", "标普500"),
                ("usIXIC", "IXIC", "纳斯达克"),
                ("usDJI", "DJI", "道琼斯"),
            ],
        }
        configured = mappings.get(str(region or "").lower())
        if not configured:
            return None

        results: list[dict[str, Any]] = []
        for symbol, return_code, default_name in configured:
            try:
                fields = self._fetch_quote_fields(symbol)
            except Exception as exc:
                logger.warning("TencentFetcher index quote failed for %s: %s", symbol, exc)
                continue
            if len(fields) < 38:
                continue
            current = safe_float(fields[3])
            if current is None or current <= 0:
                continue
            results.append(
                {
                    "code": return_code,
                    "name": fields[1].strip() or default_name,
                    "current": current,
                    "change": safe_float(fields[31]),
                    "change_pct": safe_float(fields[32]),
                    "open": safe_float(fields[5]),
                    "high": safe_float(fields[33]),
                    "low": safe_float(fields[34]),
                    "prev_close": safe_float(fields[4]),
                    "volume": safe_float(fields[6]),
                    "amount": safe_float(fields[37]),
                    "amplitude": safe_float(fields[43]) if len(fields) > 43 else None,
                    "source": self.name,
                    "provider_timestamp": _parse_provider_timestamp(
                        fields[30],
                        market=str(region).lower(),
                    ),
                }
            )
        return results or None

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "pct_chg" not in normalized.columns:
            normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        normalized = normalized[["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]
        return normalized


def _to_tencent_symbol(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    upper = code.upper()
    if upper.startswith("HK") and upper[2:].isdigit():
        return f"hk{upper[2:].zfill(5)}"
    if upper.isdigit() and 4 <= len(upper) <= 5:
        return f"hk{upper.zfill(5)}"
    if is_us_stock_code(upper):
        return f"us{upper}"
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    if is_bse_code(code):
        return f"bj{code}"
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _read_tencent_priority() -> int:
    raw_value = os.getenv("TENCENT_PRIORITY", "5")
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
        logger.warning(
            "TENCENT_PRIORITY=%r is not a valid integer; falling back to 5",
            raw_value,
        )
        return 5


def _estimate_lookback_days(*, start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        calendar_days = max(1, (end - start).days + 1)
    except ValueError:
        calendar_days = 90
    # Trading days are sparse over calendar days; add margin for holidays/suspensions.
    return max(30, min(_MAX_KLINE_BARS, int(calendar_days * 1.8) + 20))


def _empty_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def _first_returned_date(df: pd.DataFrame) -> Optional[str]:
    if "date" not in df.columns or df.empty:
        return None
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.min().strftime("%Y-%m-%d")


def _is_capped_history_incomplete(
    *,
    first_returned_date: str,
    start_date: str,
    lookback: int,
    returned_rows: int,
) -> bool:
    hit_cap = lookback >= _MAX_KLINE_BARS and returned_rows >= _MAX_KLINE_BARS
    if not hit_cap:
        return False
    try:
        first = datetime.strptime(first_returned_date, "%Y-%m-%d")
        requested_start = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return False
    return first > _first_trading_date_on_or_after(requested_start)


def _first_trading_date_on_or_after(start_date: datetime) -> datetime:
    if xcals is not None:
        try:
            cal = xcals.get_calendar("XSHG")
            session = cal.date_to_session(start_date.date(), direction="next")
            return datetime.combine(session.date(), datetime.min.time())
        except Exception:
            pass

    current = start_date
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _format_tencent_date(date_text: str) -> Optional[str]:
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _lots_to_shares(volume: Any) -> Any:
    try:
        return float(volume) * 100
    except (TypeError, ValueError):
        return volume


def _parse_provider_timestamp(value: str, *, market: str) -> Optional[str]:
    text = str(value or "").strip().replace("/", "-")
    if not text:
        return None
    timezone_name = "Asia/Hong_Kong" if market == "hk" else "America/New_York"
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=ZoneInfo(timezone_name)).isoformat()
    except ValueError:
        return None


def _market_value_to_base_currency(value: Any) -> Optional[float]:
    """Tencent quote market-value fields are expressed in 100 million units."""
    parsed = safe_float(value)
    return parsed * 100_000_000 if parsed is not None else None


def _extract_kline_rows(payload: dict[str, Any], *, symbol: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return []
    rows = item.get("qfqday") or item.get("day") or []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        amount: Optional[Any] = row[6] if len(row) > 6 and not isinstance(row[6], dict) else None
        volume = _lots_to_shares(row[5]) if symbol.startswith(("sh", "sz", "bj")) else row[5]
        result.append(
            {
                "date": str(row[0]),
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": volume,
                "amount": amount,
            }
        )
    return result
