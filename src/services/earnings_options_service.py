# -*- coding: utf-8 -*-
"""US earnings-calendar and near-expiry option-chain analysis.

The service is deliberately fail-open.  Yahoo Finance is an aggregator rather
than an exchange feed, so every payload keeps source and verification metadata
and the probability calculation is labelled as a model estimate.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from datetime import date, datetime, time as datetime_time, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from data_provider.us_index_mapping import is_us_stock_code


logger = logging.getLogger(__name__)

_NEW_YORK = ZoneInfo("America/New_York")


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int:
    number = _as_float(value)
    return max(0, int(number)) if number is not None else 0


def _iso_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_NEW_YORK)
            return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return None
    return None


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.astimezone(_NEW_YORK).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def estimate_long_option_profit_probability(
    *,
    option_type: str,
    spot: float,
    strike: float,
    premium: float,
    implied_volatility: float,
    years_to_expiry: float,
) -> Optional[float]:
    """Estimate expiry probability of finishing beyond a long option breakeven.

    This is a risk-neutral lognormal estimate with a zero short-rate assumption;
    it is not an empirical win rate and deliberately excludes slippage and fees.
    """

    if min(spot, strike, premium, implied_volatility, years_to_expiry) <= 0:
        return None
    normalized_type = option_type.lower()
    breakeven = strike + premium if normalized_type == "call" else strike - premium
    if breakeven <= 0:
        return None
    sigma_t = implied_volatility * math.sqrt(years_to_expiry)
    if sigma_t <= 0:
        return None
    d2 = (math.log(spot / breakeven) - 0.5 * implied_volatility**2 * years_to_expiry) / sigma_t
    probability = _normal_cdf(d2) if normalized_type == "call" else _normal_cdf(-d2)
    return max(0.0, min(1.0, probability))


def _phase_label(context: Optional[Dict[str, Any]], language: str) -> str:
    phase = str((context or {}).get("phase") or "unknown").lower()
    labels = {
        "zh": {"premarket": "盘前", "intraday": "盘中", "postmarket": "盘后", "unknown": "当前阶段"},
        "en": {"premarket": "Pre-market", "intraday": "Intraday", "postmarket": "Post-market", "unknown": "Current phase"},
        "ko": {"premarket": "장전", "intraday": "장중", "postmarket": "장후", "unknown": "현재 단계"},
    }
    selected = labels.get(language, labels["zh"])
    return selected.get(phase, selected["unknown"])


def earnings_options_title(context: Optional[Dict[str, Any]], language: str = "zh") -> str:
    phase_context = context.get("market_phase_context") if isinstance(context, dict) else None
    phase = _phase_label(phase_context, language)
    if language == "en":
        return f"{phase} Earnings and Near-Expiry Options"
    if language == "ko":
        return f"{phase} 실적 및 근접 만기 옵션"
    return f"{phase}财报与邻近到期期权重点"


def format_earnings_options_prompt_section(
    context: Optional[Dict[str, Any]],
    *,
    report_language: str = "zh",
) -> str:
    if not isinstance(context, dict) or context.get("status") != "ok":
        return ""
    title = earnings_options_title(context, report_language)
    earnings = context.get("earnings") or {}
    earnings_estimates = earnings.get("estimates") or {}
    expiry = context.get("expiry") or {}
    underlying = context.get("underlying") or {}
    activity = context.get("activity") or {}
    candidates = context.get("high_probability_contracts") or []
    unusual = context.get("recent_unusual_activity") or []
    lines = [f"\n## 🧾 {title}", ""]
    if report_language == "en":
        labels = {
            "earnings": "Earnings",
            "estimates": "Earnings estimates",
            "expiry": "Selected expiry",
            "underlying": "Underlying",
            "flow": "Call/Put volume",
            "source": "Source",
            "candidates": "Contracts above the model PoP threshold (up to 5)",
            "unusual": "Recent unusual options activity",
            "contract_metrics": "premium={premium}, BE={breakeven}, PoP={probability:.1%}, 1-sigma net/contract={scenario_profit}, volume/OI={volume}/{open_interest}, spread={spread_pct}",
            "unusual_metrics": "volume/OI={ratio}, volume={volume}, IV={iv}, last trade={last_trade}",
            "instruction": (
                "Analysis requirement: when earnings and expiry are adjacent, explicitly assess gap risk, "
                "IV crush, theta and liquidity together with the underlying move. PoP is a risk-neutral "
                "model estimate, not a historical win rate or return guarantee; PoP above 50% alone must "
                "not produce a buy recommendation."
            ),
        }
    elif report_language == "ko":
        labels = {
            "earnings": "실적 발표일",
            "estimates": "실적 예상",
            "expiry": "분석 만기일",
            "underlying": "기초자산",
            "flow": "Call/Put 거래량",
            "source": "데이터 소스",
            "candidates": "모형 PoP 기준 초과 계약 (최대 5개)",
            "unusual": "최근 비정상 옵션 활동",
            "contract_metrics": "프리미엄={premium}, 손익분기={breakeven}, PoP={probability:.1%}, 1시그마 순손익/계약={scenario_profit}, 거래량/OI={volume}/{open_interest}, 스프레드={spread_pct}",
            "unusual_metrics": "거래량/OI={ratio}, 거래량={volume}, IV={iv}, 최근 거래={last_trade}",
            "instruction": (
                "분석 요구: 실적 발표일과 만기일이 인접하면 갭 위험, IV crush, 시간가치와 유동성을 "
                "기초자산 변동과 함께 평가해야 합니다. PoP는 위험중립 모형 추정치이며 과거 승률이나 "
                "수익 보장이 아니므로 PoP 50% 초과만으로 매수 결론을 내리지 마세요."
            ),
        }
    else:
        labels = {
            "earnings": "财报日",
            "estimates": "财报预期",
            "expiry": "分析到期日",
            "underlying": "正股",
            "flow": "Call/Put 成交量",
            "source": "数据源",
            "candidates": "模型估算到期盈利概率超过阈值的合约（最多 5 个）",
            "unusual": "最近异常期权行为",
            "contract_metrics": "权利金≈{premium}，盈亏平衡={breakeven}，PoP={probability:.1%}，1σ情景净收益/张={scenario_profit}，成交量/OI={volume}/{open_interest}，价差={spread_pct}",
            "unusual_metrics": "量/OI={ratio}，成交量={volume}，IV={iv}，最近成交={last_trade}",
            "instruction": (
                "分析要求：若财报日与到期日相邻，必须重点评估财报跳空、IV crush、时间价值衰减和流动性；"
                "将正股走势与期权偏斜联合判断。PoP 仅是风险中性模型估算，不是历史胜率或收益保证；"
                "不得因 PoP>50% 单独给出买入结论。"
            ),
        }
    lines.extend(
        [
            f"- {labels['earnings']}: {earnings.get('date') or 'N/A'} (T{earnings.get('days_until', 'N/A'):+}d if known; timing={earnings.get('timing') or 'unknown'})" if isinstance(earnings.get('days_until'), int) else f"- {labels['earnings']}: {earnings.get('date') or 'N/A'} (timing={earnings.get('timing') or 'unknown'})",
            f"- {labels['estimates']}: EPS avg/range={earnings_estimates.get('eps_average', 'N/A')}/[{earnings_estimates.get('eps_low', 'N/A')}, {earnings_estimates.get('eps_high', 'N/A')}]; revenue avg={earnings_estimates.get('revenue_average', 'N/A')}",
            f"- {labels['expiry']}: {expiry.get('date') or 'N/A'} (earnings gap={expiry.get('gap_from_earnings_days', 'N/A')}d; adjacent={expiry.get('is_earnings_adjacent', False)})",
            f"- {labels['underlying']}: {underlying.get('price', 'N/A')}; change={underlying.get('change_pct', 'N/A')}%",
            f"- {labels['flow']}: {activity.get('call_volume', 0)}/{activity.get('put_volume', 0)}; Put/Call={activity.get('put_call_volume_ratio', 'N/A')}",
            f"- {labels['source']}: {context.get('source_id')}; verification={context.get('verification_status')}; as_of={context.get('as_of')}",
        ]
    )
    if candidates:
        lines.append(f"- {labels['candidates']}:")
        for item in candidates[:5]:
            lines.append(
                "  - {contract} {kind} K={strike}; {metrics}".format(
                    contract=item.get("contract_symbol", "N/A"),
                    kind=str(item.get("option_type", "")).upper(),
                    strike=item.get("strike", "N/A"),
                    metrics=labels["contract_metrics"].format(
                        premium=item.get("premium", "N/A"),
                        breakeven=item.get("breakeven", "N/A"),
                        probability=float(item.get("profit_probability") or 0),
                        scenario_profit=item.get("one_sigma_net_profit_per_contract", "N/A"),
                        volume=item.get("volume", 0),
                        open_interest=item.get("open_interest", 0),
                        spread_pct=item.get("spread_pct", "N/A"),
                    ),
                )
            )
    if unusual:
        lines.append(f"- {labels['unusual']}:")
        for item in unusual[:3]:
            lines.append(
                "  - {contract}: {metrics}".format(
                    contract=item.get("contract_symbol"),
                    metrics=labels["unusual_metrics"].format(
                        ratio=item.get("volume_open_interest_ratio"),
                        volume=item.get("volume"),
                        iv=item.get("implied_volatility"),
                        last_trade=item.get("last_trade_at"),
                    ),
                )
            )
    lines.extend(
        [
            "",
            f"> {labels['instruction']}",
        ]
    )
    return "\n".join(lines)


def build_earnings_options_report_view(
    context: Optional[Dict[str, Any]],
    *,
    report_language: str = "zh",
) -> Optional[Dict[str, Any]]:
    if not isinstance(context, dict) or context.get("status") != "ok":
        return None
    earnings = context.get("earnings") or {}
    expiry = context.get("expiry") or {}
    underlying = context.get("underlying") or {}
    activity = context.get("activity") or {}
    if report_language == "en":
        report_labels = {
            "earnings": "Earnings",
            "expiry": "Expiry",
            "underlying_move": "Underlying / change",
            "call_put_volume": "Call/Put volume",
            "earnings_estimates": "Earnings estimates",
            "adjacent_warning": "Earnings is adjacent to expiry: focus on gap risk, IV crush, theta, and liquidity.",
            "candidates": "Model-estimated PoP above threshold",
            "unusual": "Recent unusual options activity",
            "source_note": "Single-source aggregator data; PoP is not a historical win rate or return guarantee.",
        }
    elif report_language == "ko":
        report_labels = {
            "earnings": "실적 발표일",
            "expiry": "만기일",
            "underlying_move": "기초자산 / 등락",
            "call_put_volume": "Call/Put 거래량",
            "earnings_estimates": "실적 예상",
            "adjacent_warning": "실적 발표일과 만기일이 인접합니다. 갭, IV crush, 시간가치와 유동성을 중점 점검하세요.",
            "candidates": "모형 추정 PoP 기준 초과 계약",
            "unusual": "최근 비정상 옵션 활동",
            "source_note": "단일 집계 소스이며 PoP는 과거 승률 또는 수익 보장이 아닙니다.",
        }
    else:
        report_labels = {
            "earnings": "财报日",
            "expiry": "邻近到期日",
            "underlying_move": "正股/涨跌",
            "call_put_volume": "Call/Put 成交量",
            "earnings_estimates": "财报预期",
            "adjacent_warning": "财报日与期权到期日相邻，需重点关注财报跳空、IV crush、时间价值衰减与流动性风险。",
            "candidates": "模型估算到期盈利概率超过阈值的合约",
            "unusual": "最近异常期权行为",
            "source_note": "聚合单源数据；PoP 不是历史胜率或收益保证。",
        }
    return {
        "title": earnings_options_title(context, report_language),
        "earnings_date": earnings.get("date"),
        "days_until_earnings": earnings.get("days_until"),
        "earnings_timing": earnings.get("timing"),
        "earnings_estimates": dict(earnings.get("estimates") or {}),
        "expiry_date": expiry.get("date"),
        "expiry_gap_days": expiry.get("gap_from_earnings_days"),
        "is_earnings_adjacent": bool(expiry.get("is_earnings_adjacent")),
        "underlying_price": underlying.get("price"),
        "underlying_change_pct": underlying.get("change_pct"),
        "call_volume": activity.get("call_volume", 0),
        "put_volume": activity.get("put_volume", 0),
        "put_call_ratio": activity.get("put_call_volume_ratio"),
        "candidates": list(context.get("high_probability_contracts") or [])[:5],
        "unusual_activity": list(context.get("recent_unusual_activity") or [])[:3],
        "source_id": context.get("source_id"),
        "as_of": context.get("as_of"),
        "model_disclaimer": context.get("model_disclaimer"),
        "labels": report_labels,
    }


class EarningsOptionsService:
    """Fetch and normalize one US symbol's nearest useful option expiry."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        lookahead_days: int = 45,
        earnings_window_days: int = 3,
        profit_probability_threshold: float = 0.50,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: int = 900,
        ticker_factory: Optional[Callable[[str], Any]] = None,
        now_factory: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.lookahead_days = max(1, int(lookahead_days))
        self.earnings_window_days = max(0, int(earnings_window_days))
        self.profit_probability_threshold = max(0.01, min(0.99, float(profit_probability_threshold)))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self._ticker_factory = ticker_factory
        self._now_factory = now_factory or (lambda: datetime.now(_NEW_YORK))
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    def _new_ticker(self, symbol: str) -> Any:
        if self._ticker_factory is not None:
            return self._ticker_factory(symbol)
        import yfinance as yf

        return yf.Ticker(symbol)

    def analyze(
        self,
        symbol: str,
        *,
        realtime_quote: Any = None,
        market_phase_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if not self.enabled:
            return {"status": "disabled", "symbol": normalized}
        if not normalized or not is_us_stock_code(normalized):
            return {"status": "unsupported", "symbol": normalized, "market": "non_us"}

        cache_key = normalized
        now_monotonic = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now_monotonic - cached[0] <= self.cache_ttl_seconds:
                payload = dict(cached[1])
                payload["market_phase_context"] = dict(market_phase_context or {})
                return payload

        result_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put((True, self._analyze_sync(normalized, realtime_quote)))
            except Exception as exc:  # pragma: no cover - defensive thread boundary
                result_queue.put((False, exc))

        threading.Thread(target=worker, name=f"earnings-options-{normalized}", daemon=True).start()
        try:
            success, payload = result_queue.get(timeout=self.timeout_seconds)
        except queue.Empty:
            logger.warning("[%s] earnings/options fetch timed out after %.1fs", normalized, self.timeout_seconds)
            return {"status": "failed", "symbol": normalized, "error": "timeout", "fail_open": True}
        if not success:
            logger.warning("[%s] earnings/options fetch failed: %s", normalized, payload)
            return {"status": "failed", "symbol": normalized, "error": type(payload).__name__, "fail_open": True}

        payload["market_phase_context"] = dict(market_phase_context or {})
        with self._cache_lock:
            self._cache[cache_key] = (now_monotonic, dict(payload))
        return payload

    def _analyze_sync(self, symbol: str, realtime_quote: Any) -> Dict[str, Any]:
        now = self._now_factory().astimezone(_NEW_YORK)
        ticker = self._new_ticker(symbol)
        earnings_date, earnings_timing, earnings_estimates = self._extract_earnings_date(
            ticker,
            now.date(),
        )
        expiries = sorted(
            expiry for expiry in (_as_date(value) for value in (getattr(ticker, "options", ()) or ()))
            if expiry is not None and expiry >= now.date()
        )
        if not expiries:
            return {
                "status": "missing",
                "symbol": symbol,
                "source_id": "yfinance_options",
                "verification_status": "single_source",
                "warnings": ["no_future_option_expiry"],
            }

        eligible_earnings = (
            earnings_date is not None
            and -1 <= (earnings_date - now.date()).days <= self.lookahead_days
        )
        expiry_date = (
            min(expiries, key=lambda item: (abs((item - earnings_date).days), item))
            if eligible_earnings and earnings_date is not None
            else expiries[0]
        )
        chain = ticker.option_chain(expiry_date.isoformat())
        try:
            fast_info = getattr(ticker, "fast_info", {}) or {}
        except Exception as exc:
            logger.debug("[%s] yfinance fast_info unavailable: %s", symbol, exc)
            fast_info = {}
        spot = self._quote_number(realtime_quote, "price") or _as_float(
            self._mapping_value(fast_info, "last_price", "lastPrice")
        )
        previous_close = self._quote_number(realtime_quote, "pre_close") or _as_float(
            self._mapping_value(fast_info, "previous_close", "previousClose")
        )
        change_pct = self._quote_number(realtime_quote, "change_pct")
        if change_pct is None and spot and previous_close:
            change_pct = (spot - previous_close) / previous_close * 100.0
        if not spot or spot <= 0:
            return {
                "status": "missing",
                "symbol": symbol,
                "source_id": "yfinance_options",
                "verification_status": "single_source",
                "warnings": ["underlying_price_missing"],
            }

        expiry_close = datetime.combine(expiry_date, datetime_time(16, 0), tzinfo=_NEW_YORK)
        years_to_expiry = max((expiry_close - now).total_seconds(), 60.0) / (365.0 * 86400.0)
        calls = self._normalize_contracts(getattr(chain, "calls", None), "call", spot, years_to_expiry)
        puts = self._normalize_contracts(getattr(chain, "puts", None), "put", spot, years_to_expiry)
        contracts = calls + puts
        activity = self._activity_summary(calls, puts)
        eligible_probability = sorted(
            (
                item for item in contracts
                if item.get("profit_probability") is not None
                and item["profit_probability"] >= self.profit_probability_threshold
                and item.get("volume", 0) > 0
                and item.get("open_interest", 0) > 0
                and (item.get("spread_pct") is None or item["spread_pct"] <= 0.35)
                and 0.75 <= item.get("moneyness", 0) <= 1.25
            ),
            key=lambda item: (-item["profit_probability"], -item["volume"], item.get("spread_pct") or 0),
        )
        high_probability: List[Dict[str, Any]] = []
        for option_type in ("call", "put"):
            first = next(
                (item for item in eligible_probability if item.get("option_type") == option_type),
                None,
            )
            if first is not None:
                high_probability.append(first)
        high_probability.extend(
            item for item in eligible_probability if item not in high_probability
        )
        high_probability = high_probability[:5]
        unusual = sorted(
            (
                item for item in contracts
                if item.get("volume", 0) >= 20
                and item.get("volume_open_interest_ratio") is not None
                and item["volume_open_interest_ratio"] >= 1.0
            ),
            key=lambda item: (-item["volume_open_interest_ratio"], -item["volume"]),
        )[:5]
        gap = (expiry_date - earnings_date).days if earnings_date is not None else None
        payload = {
            "status": "ok",
            "symbol": symbol,
            "market": "us",
            "source_id": "yfinance_options_calendar",
            "source_tier": "market_aggregator",
            "verification_status": "single_source",
            "as_of": now.astimezone(timezone.utc).isoformat(),
            "earnings": {
                "date": earnings_date.isoformat() if earnings_date else None,
                "days_until": (earnings_date - now.date()).days if earnings_date else None,
                "timing": earnings_timing,
                "within_lookahead": bool(eligible_earnings),
                "estimates": earnings_estimates,
            },
            "expiry": {
                "date": expiry_date.isoformat(),
                "days_to_expiry": (expiry_date - now.date()).days,
                "gap_from_earnings_days": gap,
                "is_earnings_adjacent": gap is not None and abs(gap) <= self.earnings_window_days,
                "is_zero_dte": expiry_date == now.date(),
            },
            "underlying": {
                "price": round(spot, 4),
                "previous_close": round(previous_close, 4) if previous_close else None,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
            },
            "activity": activity,
            "high_probability_threshold": self.profit_probability_threshold,
            "high_probability_contracts": high_probability,
            "recent_unusual_activity": unusual,
            "warnings": [
                "aggregator_data_may_be_delayed",
                "earnings_date_single_source_unverified",
                "option_probability_is_model_estimate",
            ],
            "model_disclaimer": (
                "Risk-neutral lognormal expiry estimate using provider IV and a zero short-rate; "
                "fees, slippage, early exercise, volatility skew changes and earnings gaps are excluded."
            ),
        }
        logger.info(
            "[earnings_options] symbol=%s earnings=%s expiry=%s adjacent=%s stock_change_pct=%s "
            "call_volume=%s put_volume=%s put_call_ratio=%s pop_over_threshold=%s unusual=%s source=%s",
            symbol,
            payload["earnings"]["date"],
            expiry_date.isoformat(),
            payload["expiry"]["is_earnings_adjacent"],
            payload["underlying"]["change_pct"],
            activity["call_volume"],
            activity["put_volume"],
            activity["put_call_volume_ratio"],
            len(high_probability),
            len(unusual),
            payload["source_id"],
        )
        return payload

    @staticmethod
    def _mapping_value(mapping: Any, *keys: str) -> Any:
        for key in keys:
            try:
                value = mapping.get(key) if hasattr(mapping, "get") else mapping[key]
            except (KeyError, TypeError, AttributeError):
                continue
            if value is not None:
                return value
        return None

    @staticmethod
    def _quote_number(quote: Any, field: str) -> Optional[float]:
        if quote is None:
            return None
        value = quote.get(field) if isinstance(quote, dict) else getattr(quote, field, None)
        return _as_float(value)

    def _extract_earnings_date(
        self,
        ticker: Any,
        today: date,
    ) -> Tuple[Optional[date], str, Dict[str, Optional[float]]]:
        candidates: List[date] = []
        timing = "unknown"
        estimates: Dict[str, Optional[float]] = {}
        try:
            calendar = getattr(ticker, "calendar", None)
        except Exception as exc:
            logger.debug("earnings calendar unavailable: %s", exc)
            calendar = None
        if isinstance(calendar, dict):
            raw_dates = calendar.get("Earnings Date") or calendar.get("EarningsDate")
            if not isinstance(raw_dates, (list, tuple)):
                raw_dates = [raw_dates]
            candidates.extend(filter(None, (_as_date(item) for item in raw_dates)))
            timing = str(calendar.get("Earnings Call Time") or calendar.get("Earnings Time") or "unknown")
            estimate_keys = {
                "eps_average": ("Earnings Average", "EPS Average"),
                "eps_low": ("Earnings Low", "EPS Low"),
                "eps_high": ("Earnings High", "EPS High"),
                "revenue_average": ("Revenue Average",),
                "revenue_low": ("Revenue Low",),
                "revenue_high": ("Revenue High",),
            }
            for normalized_key, raw_keys in estimate_keys.items():
                value = next((calendar.get(key) for key in raw_keys if calendar.get(key) is not None), None)
                estimates[normalized_key] = _as_float(value)
        try:
            earnings_dates = ticker.get_earnings_dates(limit=8)
            index_values: Iterable[Any] = getattr(earnings_dates, "index", ())
            if index_values is None:
                index_values = ()
            candidates.extend(filter(None, (_as_date(item) for item in index_values)))
        except Exception as exc:
            logger.debug("earnings date fallback unavailable: %s", exc)
        candidates = sorted(set(candidates))
        future = [item for item in candidates if item >= today]
        return (
            future[0] if future else (candidates[-1] if candidates else None),
            timing,
            {key: value for key, value in estimates.items() if value is not None},
        )

    def _normalize_contracts(
        self,
        frame: Any,
        option_type: str,
        spot: float,
        years_to_expiry: float,
    ) -> List[Dict[str, Any]]:
        if frame is None or not hasattr(frame, "to_dict"):
            return []
        try:
            rows = frame.to_dict("records")
        except Exception:
            return []
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            strike = _as_float(row.get("strike"))
            bid = _as_float(row.get("bid"))
            ask = _as_float(row.get("ask"))
            last_price = _as_float(row.get("lastPrice"))
            iv = _as_float(row.get("impliedVolatility"))
            if strike is None or strike <= 0 or iv is None or iv <= 0:
                continue
            premium = (bid + ask) / 2.0 if bid is not None and ask is not None and ask > 0 else last_price
            if premium is None or premium <= 0:
                continue
            spread_pct = None
            if bid is not None and ask is not None and ask >= bid and premium > 0:
                spread_pct = (ask - bid) / premium
            probability = estimate_long_option_profit_probability(
                option_type=option_type,
                spot=spot,
                strike=strike,
                premium=premium,
                implied_volatility=iv,
                years_to_expiry=years_to_expiry,
            )
            volume = _as_int(row.get("volume"))
            open_interest = _as_int(row.get("openInterest"))
            volume_oi = volume / open_interest if open_interest > 0 else (float(volume) if volume else None)
            breakeven = strike + premium if option_type == "call" else strike - premium
            implied_move = iv * math.sqrt(years_to_expiry)
            scenario_spot = (
                spot * (1.0 + implied_move)
                if option_type == "call"
                else max(0.0, spot * (1.0 - implied_move))
            )
            scenario_payoff = (
                max(0.0, scenario_spot - strike)
                if option_type == "call"
                else max(0.0, strike - scenario_spot)
            )
            scenario_net_profit = (scenario_payoff - premium) * 100.0
            normalized.append(
                {
                    "contract_symbol": str(row.get("contractSymbol") or ""),
                    "option_type": option_type,
                    "strike": round(strike, 4),
                    "bid": round(bid, 4) if bid is not None else None,
                    "ask": round(ask, 4) if ask is not None else None,
                    "premium": round(premium, 4),
                    "max_loss_per_contract": round(premium * 100.0, 2),
                    "breakeven": round(breakeven, 4),
                    "breakeven_move_pct": round((breakeven / spot - 1.0) * 100.0, 2),
                    "one_sigma_implied_move_pct": round(implied_move * 100.0, 2),
                    "one_sigma_underlying_target": round(scenario_spot, 4),
                    "one_sigma_net_profit_per_contract": round(scenario_net_profit, 2),
                    "one_sigma_return_pct": round(
                        scenario_net_profit / (premium * 100.0) * 100.0,
                        2,
                    ),
                    "profit_probability": round(probability, 4) if probability is not None else None,
                    "implied_volatility": round(iv, 4),
                    "volume": volume,
                    "open_interest": open_interest,
                    "volume_open_interest_ratio": round(volume_oi, 2) if volume_oi is not None else None,
                    "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                    "moneyness": round(strike / spot, 4),
                    "in_the_money": bool(row.get("inTheMoney")),
                    "last_trade_at": _iso_datetime(row.get("lastTradeDate")),
                }
            )
        return normalized

    @staticmethod
    def _activity_summary(calls: List[Dict[str, Any]], puts: List[Dict[str, Any]]) -> Dict[str, Any]:
        call_volume = sum(item.get("volume", 0) for item in calls)
        put_volume = sum(item.get("volume", 0) for item in puts)
        return {
            "call_volume": call_volume,
            "put_volume": put_volume,
            "call_open_interest": sum(item.get("open_interest", 0) for item in calls),
            "put_open_interest": sum(item.get("open_interest", 0) for item in puts),
            "put_call_volume_ratio": round(put_volume / call_volume, 3) if call_volume > 0 else None,
            "contracts_observed": len(calls) + len(puts),
        }
