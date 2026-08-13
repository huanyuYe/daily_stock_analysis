# -*- coding: utf-8 -*-
"""Official US filings and public HKEXnews issuer disclosures."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests

from data_provider.base import normalize_stock_code
from src.config import Config, get_config
from src.core.trading_calendar import get_market_for_stock
from src.services.upstream_resilience import (
    PersistentJsonCache,
    UpstreamRequestGate,
    get_hkex_request_gate,
    get_sec_request_gate,
    is_retryable_http_error,
)

logger = logging.getLogger(__name__)

_SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
_HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
_HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"
_CACHE_TTL_SECONDS = 30 * 60
_FAILURE_CACHE_TTL_SECONDS = 60
_CACHE_LOCK = threading.RLock()
_BUNDLE_CACHE: Dict[str, tuple[float, "RegulatoryDisclosureBundle"]] = {}
_SEC_MAPPING_CACHE: tuple[float, Dict[str, str]] = (0.0, {})
_SEC_MAPPING_FAILURE_RETRY_AT: tuple[float, str] = (0.0, "")

_SEC_FORMS_FOR_FACTS = frozenset({"10-K", "10-Q", "20-F", "40-F", "6-K"})
_SEC_FACT_METRICS = (
    ("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"), ("USD",)),
    ("net_income", ("NetIncomeLoss",), ("USD",)),
    ("assets", ("Assets",), ("USD",)),
    ("liabilities", ("Liabilities",), ("USD",)),
    ("stockholders_equity", ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), ("USD",)),
    ("operating_income", ("OperatingIncomeLoss",), ("USD",)),
    ("operating_cash_flow", ("NetCashProvidedByUsedInOperatingActivities",), ("USD",)),
    ("diluted_eps", ("EarningsPerShareDiluted",), ("USD/shares", "USD / shares")),
)


@dataclass(frozen=True)
class RegulatoryFiling:
    """One regulator-published filing or issuer disclosure."""

    market: str
    stock_code: str
    issuer_id: str
    title: str
    form_type: str
    filed_at: Optional[datetime]
    period_of_report: Optional[date]
    document_id: str
    url: str
    source_id: str
    source_tier: str = "official_regulator"
    verification_status: str = "official_primary"
    amended: bool = False
    language: str = "en"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "stock_code": self.stock_code,
            "issuer_id": self.issuer_id,
            "title": self.title,
            "form_type": self.form_type,
            "filed_at": self.filed_at.isoformat() if self.filed_at else None,
            "period_of_report": self.period_of_report.isoformat() if self.period_of_report else None,
            "document_id": self.document_id,
            "url": self.url,
            "source_id": self.source_id,
            "source_tier": self.source_tier,
            "verification_status": self.verification_status,
            "amended": self.amended,
            "language": self.language,
        }


@dataclass(frozen=True)
class CompanyFact:
    """One point-in-time SEC XBRL fact with filing provenance."""

    metric: str
    concept: str
    label: str
    unit: str
    value: Any
    period_start: Optional[date]
    period_end: Optional[date]
    filed_at: Optional[date]
    form_type: str
    accession_no: str
    fiscal_year: Optional[int]
    fiscal_period: str
    source_url: str
    source_id: str = "sec_companyfacts"
    source_tier: str = "official_regulator"
    verification_status: str = "official_primary"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "concept": self.concept,
            "label": self.label,
            "unit": self.unit,
            "value": self.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "filed_at": self.filed_at.isoformat() if self.filed_at else None,
            "form_type": self.form_type,
            "accession_no": self.accession_no,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "source_url": self.source_url,
            "source_id": self.source_id,
            "source_tier": self.source_tier,
            "verification_status": self.verification_status,
        }


@dataclass(frozen=True)
class RegulatoryDisclosureBundle:
    """Cross-market regulatory evidence plus source diagnostics."""

    stock_code: str
    stock_name: str
    market: str
    as_of: datetime
    status: str
    filings: tuple[RegulatoryFiling, ...]
    company_facts: tuple[CompanyFact, ...]
    source_status: Dict[str, str]
    warnings: tuple[str, ...] = ()

    def to_dict(self, *, include_items: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "market": self.market,
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            "filing_count": len(self.filings),
            "company_fact_count": len(self.company_facts),
            "source_status": dict(self.source_status),
            "warnings": list(self.warnings),
        }
        if include_items:
            payload["filings"] = [item.to_dict() for item in self.filings]
            payload["company_facts"] = [item.to_dict() for item in self.company_facts]
        return payload

    def to_prompt_context(self, *, max_filings: int = 8) -> Optional[str]:
        if not self.filings and not self.company_facts:
            return None
        lines = [
            f"## 官方监管披露（{self.stock_name}/{self.stock_code}）",
            f"状态：{self.status}；截至：{self.as_of.isoformat()}；来源状态：{self.source_status}",
            "说明：仅将监管机构发布的元数据/结构化事实视为官方一手证据；不据此推断未披露事实。",
        ]
        for index, filing in enumerate(self.filings[:max_filings], 1):
            filed_at = filing.filed_at.isoformat() if filing.filed_at else "unknown"
            lines.append(
                f"{index}. [{filing.form_type or 'disclosure'}] {filing.title} "
                f"（{filed_at}，{filing.source_id}）"
            )
            lines.append(f"   原文：{filing.url}")
        if self.company_facts:
            lines.append("### SEC 结构化财务事实（按 filed_at 选取最新已申报值）")
            for fact in self.company_facts:
                lines.append(
                    f"- {fact.metric}: {fact.value} {fact.unit}；period_end="
                    f"{fact.period_end or 'unknown'}；filed={fact.filed_at or 'unknown'}；"
                    f"form={fact.form_type}；concept={fact.concept}"
                )
        return "\n".join(lines)


class _HKEXRowParser(HTMLParser):
    """Extract table rows and announcement links without adding a parser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[tuple[List[str], List[str]]] = []
        self._in_row = False
        self._cell_depth = 0
        self._cells: List[str] = []
        self._cell_parts: List[str] = []
        self._links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered == "tr":
            self._in_row = True
            self._cells = []
            self._links = []
        elif self._in_row and lowered in {"td", "th"}:
            self._cell_depth += 1
            self._cell_parts = []
        elif self._in_row and lowered == "a":
            href = dict(attrs).get("href")
            if href:
                self._links.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_row and self._cell_depth:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._in_row and lowered in {"td", "th"} and self._cell_depth:
            self._cells.append(" ".join("".join(self._cell_parts).split()))
            self._cell_parts = []
            self._cell_depth -= 1
        elif lowered == "tr" and self._in_row:
            if self._cells:
                self.rows.append((list(self._cells), list(self._links)))
            self._in_row = False
            self._cell_depth = 0


class RegulatoryDisclosureService:
    """Fetch SEC-A/SEC-B and public HKEXnews evidence with bounded failures."""

    def __init__(
        self,
        *,
        config: Optional[Config] = None,
        session: Optional[requests.Session] = None,
        now_provider=None,
        persistent_cache_dir: Optional[Path] = None,
        sec_request_gate: Optional[UpstreamRequestGate] = None,
        hkex_request_gate: Optional[UpstreamRequestGate] = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.config = config or get_config()
        self.session = session or requests.Session()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._persistent_cache = (
            None
            if session is not None and persistent_cache_dir is None
            else PersistentJsonCache("regulatory", cache_dir=persistent_cache_dir)
        )
        # Unit/integration callers commonly inject an in-memory session; keep
        # those deterministic unless they explicitly inject a gate as well.
        self._sec_request_gate = sec_request_gate or (
            None if session is not None else get_sec_request_gate()
        )
        self._hkex_request_gate = hkex_request_gate or (
            None if session is not None else get_hkex_request_gate()
        )
        if self._sec_request_gate is not None:
            self._sec_request_gate.min_interval_seconds = max(
                0.0,
                float(getattr(self.config, "regulatory_sec_request_min_interval_sec", 0.35)),
            )
        if self._hkex_request_gate is not None:
            self._hkex_request_gate.min_interval_seconds = max(
                0.0,
                float(getattr(self.config, "regulatory_hkex_request_min_interval_sec", 1.0)),
            )
        self._sleep = sleep_fn
        self._request_context = threading.local()

    def fetch(
        self,
        stock_code: str,
        stock_name: str = "",
        *,
        max_filings: int = 12,
        lookback_days: int = 120,
    ) -> RegulatoryDisclosureBundle:
        code = normalize_stock_code(str(stock_code or "").strip())
        market = get_market_for_stock(code) or ""
        as_of = _ensure_utc(self.now_provider())
        if market not in {"us", "hk"}:
            return RegulatoryDisclosureBundle(
                stock_code=code,
                stock_name=stock_name,
                market=market,
                as_of=as_of,
                status="unsupported",
                filings=(),
                company_facts=(),
                source_status={"sec_submissions": "unsupported", "sec_companyfacts": "unsupported", "hkexnews": "unsupported"},
            )
        if not getattr(self.config, "regulatory_disclosures_enabled", True):
            return RegulatoryDisclosureBundle(
                stock_code=code,
                stock_name=stock_name,
                market=market,
                as_of=as_of,
                status="disabled",
                filings=(),
                company_facts=(),
                source_status={"regulatory_disclosures": "disabled"},
            )

        safe_max = max(1, min(int(max_filings), 30))
        safe_days = max(1, min(int(lookback_days), 3650))
        cache_key = f"{market}:{code}:{safe_max}:{safe_days}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        self._request_context.stale_warnings = []

        if market == "us":
            bundle = self._fetch_sec(code, stock_name, as_of, safe_max, safe_days)
        else:
            bundle = self._fetch_hkex(code, stock_name, as_of, safe_max, safe_days)
        stale_warnings = list(getattr(self._request_context, "stale_warnings", []) or [])
        if stale_warnings:
            source_status = dict(bundle.source_status)
            for warning in stale_warnings:
                if "sec_ticker_mapping" in warning:
                    source_status["sec_ticker_mapping"] = "stale_last_good"
                elif "sec_submissions" in warning:
                    source_status["sec_submissions"] = "stale_last_good"
                elif "sec_companyfacts" in warning:
                    source_status["sec_companyfacts"] = "stale_last_good"
                elif "hkex_prefix" in warning:
                    source_status["hkex_stock_mapping"] = "stale_last_good"
                elif "hkex_search" in warning:
                    source_status["hkexnews"] = "stale_last_good"
            bundle = RegulatoryDisclosureBundle(
                stock_code=bundle.stock_code,
                stock_name=bundle.stock_name,
                market=bundle.market,
                as_of=bundle.as_of,
                status="degraded" if bundle.status == "available" else bundle.status,
                filings=bundle.filings,
                company_facts=bundle.company_facts,
                source_status=source_status,
                warnings=tuple(dict.fromkeys([*bundle.warnings, *stale_warnings])),
            )
        self._put_cached(cache_key, bundle)
        return bundle

    def _fetch_sec(
        self,
        code: str,
        stock_name: str,
        as_of: datetime,
        max_filings: int,
        lookback_days: int,
    ) -> RegulatoryDisclosureBundle:
        statuses: Dict[str, str] = {}
        warnings: List[str] = []
        filings: List[RegulatoryFiling] = []
        facts: List[CompanyFact] = []
        try:
            cik = self._resolve_cik(code)
            statuses["sec_ticker_mapping"] = "success" if cik else "empty"
        except Exception as exc:
            logger.warning("SEC ticker mapping failed for %s: %s", code, type(exc).__name__)
            cik = None
            statuses["sec_ticker_mapping"] = "failed"
            warnings.append("sec_ticker_mapping_failed")

        if cik:
            try:
                payload = self._get_json(_SEC_SUBMISSIONS_URL.format(cik=cik))
                filings = self._parse_sec_submissions(payload, code, cik, as_of, lookback_days)[:max_filings]
                statuses["sec_submissions"] = "success" if filings else "empty"
            except Exception as exc:
                logger.warning("SEC submissions failed for %s: %s", code, type(exc).__name__)
                statuses["sec_submissions"] = "failed"
                warnings.append("sec_submissions_failed")
            try:
                payload = self._get_json(_SEC_COMPANYFACTS_URL.format(cik=cik))
                facts = self._parse_sec_companyfacts(payload, cik, as_of)
                statuses["sec_companyfacts"] = "success" if facts else "empty"
            except Exception as exc:
                logger.warning("SEC companyfacts failed for %s: %s", code, type(exc).__name__)
                statuses["sec_companyfacts"] = "failed"
                warnings.append("sec_companyfacts_failed")
        else:
            statuses.setdefault("sec_submissions", "unavailable")
            statuses.setdefault("sec_companyfacts", "unavailable")

        return self._bundle(code, stock_name, "us", as_of, filings, facts, statuses, warnings)

    def _fetch_hkex(
        self,
        code: str,
        stock_name: str,
        as_of: datetime,
        max_filings: int,
        lookback_days: int,
    ) -> RegulatoryDisclosureBundle:
        warnings: List[str] = []
        statuses: Dict[str, str] = {}
        filings: List[RegulatoryFiling] = []
        normalized_code = re.sub(r"\D", "", code)[-5:].zfill(5)
        try:
            stock_id = self._resolve_hkex_stock_id(normalized_code)
            statuses["hkex_stock_mapping"] = "success" if stock_id else "empty"
            if stock_id:
                start = (as_of.date() - timedelta(days=lookback_days)).strftime("%Y%m%d")
                end = as_of.date().strftime("%Y%m%d")
                search_data = {
                        "lang": "EN",
                        "category": "0",
                        "market": "SEHK",
                        "searchType": "0",
                        "documentType": "",
                        "t1code": "",
                        "t2Gcode": "",
                        "t2code": "",
                        "stockId": stock_id,
                        "from": start,
                        "to": end,
                        "MB-Daterange": "0",
                    }
                html = self._request_text_cached(
                    namespace=f"hkex_search:{normalized_code}:{start}:{end}",
                    method="POST",
                    url=_HKEX_SEARCH_URL,
                    data=search_data,
                    headers=self._hkex_headers(),
                    gate=self._hkex_request_gate,
                    fresh_ttl_seconds=15 * 60,
                    stale_ttl_seconds=24 * 60 * 60,
                )
                filings = self._parse_hkex_html(html, normalized_code, stock_id, as_of)[:max_filings]
                statuses["hkexnews"] = "success" if filings else "empty"
            else:
                statuses["hkexnews"] = "unavailable"
        except Exception as exc:
            logger.warning("HKEXnews disclosure search failed for %s: %s", code, type(exc).__name__)
            statuses["hkexnews"] = "failed"
            warnings.append("hkexnews_fetch_failed")
        return self._bundle(code, stock_name, "hk", as_of, filings, [], statuses, warnings)

    def _resolve_cik(self, ticker: str) -> Optional[str]:
        global _SEC_MAPPING_CACHE, _SEC_MAPPING_FAILURE_RETRY_AT
        now = time.monotonic()
        with _CACHE_LOCK:
            if _SEC_MAPPING_CACHE[1] and now - _SEC_MAPPING_CACHE[0] < 24 * 60 * 60:
                return _SEC_MAPPING_CACHE[1].get(ticker.upper())
            retry_at, failure_label = _SEC_MAPPING_FAILURE_RETRY_AT
            if retry_at > now:
                raise RuntimeError(
                    f"SEC ticker mapping cooldown active: {failure_label or 'upstream failure'}"
                )
        try:
            payload = self._get_json(_SEC_TICKER_URL)
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 403:
                cooldown_seconds = 30 * 60
            elif status_code == 429:
                cooldown_seconds = 5 * 60
            else:
                cooldown_seconds = 60
            label = f"HTTP {status_code}" if isinstance(status_code, int) else type(exc).__name__
            with _CACHE_LOCK:
                _SEC_MAPPING_FAILURE_RETRY_AT = (now + cooldown_seconds, label)
            raise
        fields = payload.get("fields") or []
        mapping: Dict[str, str] = {}
        try:
            ticker_index = fields.index("ticker")
            cik_index = fields.index("cik")
        except ValueError:
            ticker_index, cik_index = 2, 0
        for row in payload.get("data") or []:
            if not isinstance(row, list) or len(row) <= max(ticker_index, cik_index):
                continue
            symbol = str(row[ticker_index] or "").strip().upper()
            try:
                cik = f"{int(row[cik_index]):010d}"
            except (TypeError, ValueError):
                continue
            if symbol:
                mapping[symbol] = cik
        with _CACHE_LOCK:
            _SEC_MAPPING_CACHE = (now, mapping)
            _SEC_MAPPING_FAILURE_RETRY_AT = (0.0, "")
        return mapping.get(ticker.upper())

    def _resolve_hkex_stock_id(self, stock_code: str) -> Optional[str]:
        text = self._request_text_cached(
            namespace=f"hkex_prefix:{stock_code}",
            method="GET",
            url=_HKEX_PREFIX_URL,
            params={"callback": "callback", "lang": "EN", "type": "A", "name": stock_code, "market": "SEHK"},
            headers=self._hkex_headers(),
            gate=self._hkex_request_gate,
            fresh_ttl_seconds=24 * 60 * 60,
            stale_ttl_seconds=7 * 24 * 60 * 60,
        )
        match = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text.strip(), re.DOTALL)
        payload = json.loads(match.group(1) if match else text)
        for item in payload.get("stockInfo") or []:
            code_value = str(item.get("code") or item.get("stockCode") or "").strip()
            if code_value and re.sub(r"\D", "", code_value).zfill(5) != stock_code:
                continue
            stock_id = str(item.get("stockId") or "").strip()
            if stock_id:
                return stock_id
        return None

    def _get_json(self, url: str) -> Dict[str, Any]:
        headers = {
            "User-Agent": getattr(self.config, "sec_edgar_user_agent", "") or "daily-stock-analysis contact=https://github.com/ZhuLinsen/daily_stock_analysis",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if url == _SEC_TICKER_URL:
            fresh_ttl = 24 * 60 * 60
            stale_ttl = 7 * 24 * 60 * 60
            namespace = "sec_ticker_mapping"
        elif "/companyfacts/" in url:
            fresh_ttl = 6 * 60 * 60
            stale_ttl = 24 * 60 * 60
            namespace = f"sec_companyfacts:{url.rsplit('/', 1)[-1]}"
        else:
            fresh_ttl = 30 * 60
            stale_ttl = 24 * 60 * 60
            namespace = f"sec_submissions:{url.rsplit('/', 1)[-1]}"
        payload = self._request_json_cached(
            namespace=namespace,
            url=url,
            headers=headers,
            gate=self._sec_request_gate,
            fresh_ttl_seconds=fresh_ttl,
            stale_ttl_seconds=stale_ttl,
        )
        if not isinstance(payload, dict):
            raise ValueError("upstream response is not an object")
        return payload

    def _request_json_cached(
        self,
        *,
        namespace: str,
        url: str,
        headers: Dict[str, str],
        gate: Optional[UpstreamRequestGate],
        fresh_ttl_seconds: int,
        stale_ttl_seconds: int,
    ) -> Dict[str, Any]:
        cached = (
            self._persistent_cache.get(namespace, max_age_seconds=fresh_ttl_seconds)
            if self._persistent_cache is not None
            else None
        )
        if cached is not None and isinstance(cached[0], dict):
            return dict(cached[0])

        def request() -> Dict[str, Any]:
            response = self.session.get(url, headers=headers, timeout=self._timeout())
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("upstream response is not an object")
            return payload

        try:
            payload = self._run_request_with_retry(request, gate=gate)
            if self._persistent_cache is not None:
                self._persistent_cache.put(namespace, payload)
            return payload
        except Exception:
            stale = (
                self._persistent_cache.get(namespace, max_age_seconds=stale_ttl_seconds)
                if self._persistent_cache is not None
                else None
            )
            if stale is None or not isinstance(stale[0], dict):
                raise
            self._record_stale_warning(namespace, stale[1])
            return dict(stale[0])

    def _request_text_cached(
        self,
        *,
        namespace: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        gate: Optional[UpstreamRequestGate],
        fresh_ttl_seconds: int,
        stale_ttl_seconds: int,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        cached = (
            self._persistent_cache.get(namespace, max_age_seconds=fresh_ttl_seconds)
            if self._persistent_cache is not None
            else None
        )
        if cached is not None and isinstance(cached[0], str):
            return cached[0]

        def request() -> str:
            if method == "POST":
                response = self.session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=self._timeout(),
                )
            else:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout(),
                )
            response.raise_for_status()
            return str(response.text)

        try:
            text = self._run_request_with_retry(request, gate=gate)
            if self._persistent_cache is not None:
                self._persistent_cache.put(namespace, text)
            return text
        except Exception:
            stale = (
                self._persistent_cache.get(namespace, max_age_seconds=stale_ttl_seconds)
                if self._persistent_cache is not None
                else None
            )
            if stale is None or not isinstance(stale[0], str):
                raise
            self._record_stale_warning(namespace, stale[1])
            return stale[0]

    def _run_request_with_retry(
        self,
        operation,
        *,
        gate: Optional[UpstreamRequestGate],
    ):
        attempts = max(1, int(getattr(self.config, "regulatory_retry_max", 2)))
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return gate.call(operation) if gate is not None else operation()
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts or not is_retryable_http_error(exc):
                    raise
                self._sleep(min(2.0 ** attempt, 4.0))
        assert last_error is not None
        raise last_error

    def _record_stale_warning(self, namespace: str, age_seconds: float) -> None:
        warnings = list(getattr(self._request_context, "stale_warnings", []) or [])
        warnings.append(f"stale_last_good:{namespace}:age_seconds={int(age_seconds)}")
        self._request_context.stale_warnings = warnings

    def _timeout(self) -> float:
        return max(1.0, min(float(getattr(self.config, "regulatory_fetch_timeout_sec", 8.0)), 30.0))

    @staticmethod
    def _hkex_headers() -> Dict[str, str]:
        return {
            "User-Agent": "daily-stock-analysis/1.0 (+https://github.com/ZhuLinsen/daily_stock_analysis)",
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Referer": _HKEX_SEARCH_URL,
        }

    @staticmethod
    def _parse_sec_submissions(
        payload: Dict[str, Any],
        stock_code: str,
        cik: str,
        as_of: datetime,
        lookback_days: int,
    ) -> List[RegulatoryFiling]:
        recent = ((payload.get("filings") or {}).get("recent") or {})
        keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "primaryDocument", "primaryDocDescription")
        arrays = {key: recent.get(key) or [] for key in keys}
        count = max((len(value) for value in arrays.values()), default=0)
        cutoff = as_of.date() - timedelta(days=lookback_days)
        filings: List[RegulatoryFiling] = []
        for index in range(count):
            values = {key: arrays[key][index] if index < len(arrays[key]) else "" for key in keys}
            filed_date = _parse_date(values["filingDate"])
            if filed_date and (filed_date < cutoff or filed_date > as_of.date() + timedelta(days=1)):
                continue
            accession = str(values["accessionNumber"] or "").strip()
            primary_document = str(values["primaryDocument"] or "").strip()
            if not accession or not primary_document:
                continue
            form = str(values["form"] or "").strip()
            filed_at = _parse_sec_datetime(values["acceptanceDateTime"]) or (
                datetime.combine(filed_date, datetime.min.time(), tzinfo=timezone.utc) if filed_date else None
            )
            accession_path = accession.replace("-", "")
            url = _SEC_ARCHIVE_URL.format(
                cik=str(int(cik)),
                accession=accession_path,
                document=primary_document,
            )
            title = str(values["primaryDocDescription"] or "").strip() or f"SEC Form {form}"
            filings.append(
                RegulatoryFiling(
                    market="us",
                    stock_code=stock_code,
                    issuer_id=cik,
                    title=title,
                    form_type=form,
                    filed_at=filed_at,
                    period_of_report=_parse_date(values["reportDate"]),
                    document_id=accession,
                    url=url,
                    source_id="sec_submissions",
                    amended=form.endswith("/A"),
                )
            )
        return sorted(filings, key=lambda item: item.filed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @staticmethod
    def _parse_sec_companyfacts(payload: Dict[str, Any], cik: str, as_of: datetime) -> List[CompanyFact]:
        us_gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
        selected: List[CompanyFact] = []
        for metric, concepts, preferred_units in _SEC_FACT_METRICS:
            chosen: Optional[CompanyFact] = None
            chosen_key: tuple[date, date] = (date.min, date.min)
            for concept in concepts:
                node = us_gaap.get(concept) or {}
                units = node.get("units") or {}
                for unit in preferred_units:
                    for item in units.get(unit) or []:
                        form = str(item.get("form") or "")
                        filed = _parse_date(item.get("filed"))
                        end = _parse_date(item.get("end"))
                        if form not in _SEC_FORMS_FOR_FACTS or filed is None or filed > as_of.date() + timedelta(days=1):
                            continue
                        key = (filed, end or date.min)
                        if key <= chosen_key:
                            continue
                        accession = str(item.get("accn") or "")
                        chosen = CompanyFact(
                            metric=metric,
                            concept=concept,
                            label=str(node.get("label") or concept),
                            unit=unit,
                            value=item.get("val"),
                            period_start=_parse_date(item.get("start")),
                            period_end=end,
                            filed_at=filed,
                            form_type=form,
                            accession_no=accession,
                            fiscal_year=_safe_int(item.get("fy")),
                            fiscal_period=str(item.get("fp") or ""),
                            source_url=(
                                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                                f"{accession.replace('-', '')}/"
                            ),
                        )
                        chosen_key = key
            if chosen is not None:
                selected.append(chosen)
        return selected

    @staticmethod
    def _parse_hkex_html(
        html: str,
        stock_code: str,
        stock_id: str,
        as_of: datetime,
    ) -> List[RegulatoryFiling]:
        parser = _HKEXRowParser()
        parser.feed(html)
        filings: List[RegulatoryFiling] = []
        for cells, links in parser.rows:
            document_url = next(
                (urljoin("https://www1.hkexnews.hk/", link) for link in links if re.search(r"\.(?:pdf|htm|html)(?:$|\?)", link, re.I)),
                "",
            )
            if not document_url:
                continue
            text_cells = [cell for cell in cells if cell]
            filed_at = next((_parse_hkex_datetime(cell) for cell in text_cells if _parse_hkex_datetime(cell)), None)
            if filed_at and filed_at > as_of + timedelta(days=1):
                continue
            title = max(
                (cell for cell in text_cells if not _parse_hkex_datetime(cell) and stock_code.lstrip("0") not in cell),
                key=len,
                default="HKEX issuer disclosure",
            )
            document_id = document_url.rsplit("/", 1)[-1].split(".", 1)[0]
            filings.append(
                RegulatoryFiling(
                    market="hk",
                    stock_code=f"hk{stock_code}",
                    issuer_id=stock_id,
                    title=title,
                    form_type="issuer_disclosure",
                    filed_at=filed_at,
                    period_of_report=None,
                    document_id=document_id,
                    url=document_url,
                    source_id="hkexnews_public_title_search",
                )
            )
        unique = {item.url: item for item in filings}
        return sorted(
            unique.values(),
            key=lambda item: item.filed_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    @staticmethod
    def _bundle(
        code: str,
        stock_name: str,
        market: str,
        as_of: datetime,
        filings: Iterable[RegulatoryFiling],
        facts: Iterable[CompanyFact],
        statuses: Dict[str, str],
        warnings: Iterable[str],
    ) -> RegulatoryDisclosureBundle:
        filing_tuple = tuple(filings)
        fact_tuple = tuple(facts)
        failed = sum(status == "failed" for status in statuses.values())
        if (filing_tuple or fact_tuple) and failed:
            status = "degraded"
        elif filing_tuple or fact_tuple:
            status = "available"
        elif failed:
            status = "missing"
        else:
            status = "empty"
        return RegulatoryDisclosureBundle(
            stock_code=code,
            stock_name=stock_name,
            market=market,
            as_of=as_of,
            status=status,
            filings=filing_tuple,
            company_facts=fact_tuple,
            source_status=statuses,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _get_cached(key: str) -> Optional[RegulatoryDisclosureBundle]:
        now = time.monotonic()
        with _CACHE_LOCK:
            item = _BUNDLE_CACHE.get(key)
            if item is None:
                return None
            created_at, bundle = item
            ttl = _FAILURE_CACHE_TTL_SECONDS if bundle.status == "missing" else _CACHE_TTL_SECONDS
            if now - created_at <= ttl:
                return bundle
            _BUNDLE_CACHE.pop(key, None)
        return None

    @staticmethod
    def _put_cached(key: str, bundle: RegulatoryDisclosureBundle) -> None:
        with _CACHE_LOCK:
            if len(_BUNDLE_CACHE) >= 256:
                oldest = min(_BUNDLE_CACHE, key=lambda item: _BUNDLE_CACHE[item][0])
                _BUNDLE_CACHE.pop(oldest, None)
            _BUNDLE_CACHE[key] = (time.monotonic(), bundle)


def _parse_date(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_sec_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return _ensure_utc(parsed)
        except ValueError:
            continue
    return None


def _parse_hkex_datetime(value: str) -> Optional[datetime]:
    normalized = " ".join(str(value or "").split())
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def reset_regulatory_disclosure_cache() -> None:
    global _SEC_MAPPING_CACHE, _SEC_MAPPING_FAILURE_RETRY_AT
    with _CACHE_LOCK:
        _BUNDLE_CACHE.clear()
        _SEC_MAPPING_CACHE = (0.0, {})
        _SEC_MAPPING_FAILURE_RETRY_AT = (0.0, "")


__all__ = [
    "CompanyFact",
    "RegulatoryDisclosureBundle",
    "RegulatoryDisclosureService",
    "RegulatoryFiling",
    "reset_regulatory_disclosure_cache",
]
