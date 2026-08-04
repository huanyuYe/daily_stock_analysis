# -*- coding: utf-8 -*-
"""Structured A-share sell-side research report metadata.

This module deliberately fetches metadata only. It does not download, parse, or
redistribute report PDFs, and it labels ratings / EPS forecasts as third-party
sell-side opinions instead of verified company facts.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from data_provider.base import normalize_stock_code
from src.core.trading_calendar import get_market_for_stock

logger = logging.getLogger(__name__)

_REPORT_API = "https://reportapi.eastmoney.com/report/list"
_PDF_URL_TEMPLATE = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_INFO_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_CACHE_TTL_SECONDS = 30 * 60
_FAILED_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 256
_CACHE_LOCK = threading.RLock()
_REPORT_CACHE: Dict[str, tuple[float, "ResearchReportBundle"]] = {}


@dataclass(frozen=True)
class ResearchReport:
    """One normalized broker research report metadata record."""

    title: str
    organization: str
    published_date: date
    rating: str
    rating_change: str
    industry: str
    eps_this_year: Optional[float]
    eps_next_year: Optional[float]
    eps_next_two_year: Optional[float]
    url: str
    source_id: str = "eastmoney_reportapi"
    source_tier: str = "sell_side_aggregator"
    verification_status: str = "single_source_opinion"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "organization": self.organization,
            "published_date": self.published_date.isoformat(),
            "rating": self.rating,
            "rating_change": self.rating_change,
            "industry": self.industry,
            "eps_forecast": {
                str(self.published_date.year): self.eps_this_year,
                str(self.published_date.year + 1): self.eps_next_year,
                str(self.published_date.year + 2): self.eps_next_two_year,
            },
            "url": self.url,
            "source_id": self.source_id,
            "source_tier": self.source_tier,
            "verification_status": self.verification_status,
        }


@dataclass(frozen=True)
class ResearchReportBundle:
    """Research reports plus explicit provenance and retrieval state."""

    stock_code: str
    as_of: datetime
    lookback_days: int
    status: str
    reports: tuple[ResearchReport, ...]
    source_status: Dict[str, str]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "a-share-research-reports-v1",
            "stock_code": self.stock_code,
            "as_of": self.as_of.isoformat(),
            "retrieved_at": self.as_of.isoformat(),
            "lookback_days": self.lookback_days,
            "status": self.status,
            "report_count": len(self.reports),
            "source_status": dict(self.source_status),
            "warnings": list(self.warnings),
            "reports": [report.to_dict() for report in self.reports],
        }

    def to_prompt_context(self, *, max_reports: int = 5) -> str:
        if self.status != "available" or not self.reports:
            return ""
        lines = [
            "## A股券商研报元数据（卖方观点，非公司事实）",
            (
                f"窗口：近 {self.lookback_days} 天；共 {len(self.reports)} 条；"
                "来源=东方财富研报聚合。"
            ),
            (
                "使用要求：评级、标题和 EPS 预测均是第三方卖方观点，只能用于观察"
                "预期与分歧；不得当作公司公告、已实现业绩或独立核验事实。"
                "缺少第二来源交叉验证时必须降低置信度。"
            ),
        ]
        for index, report in enumerate(self.reports[: max(1, max_reports)], 1):
            parts = [
                report.published_date.isoformat(),
                report.organization or "机构未知",
                report.title,
            ]
            if report.rating:
                parts.append(f"评级={report.rating}")
            forecasts = [
                (report.published_date.year, report.eps_this_year),
                (report.published_date.year + 1, report.eps_next_year),
                (report.published_date.year + 2, report.eps_next_two_year),
            ]
            forecast_text = "；".join(
                f"{year}E EPS={value:g}"
                for year, value in forecasts
                if value is not None
            )
            if forecast_text:
                parts.append(forecast_text)
            lines.append(f"{index}. " + "；".join(parts))
            if report.url:
                lines.append(f"   原始链接：{report.url}")
        return "\n".join(lines)


class AShareResearchReportService:
    """Fetch recent A-share research report metadata from EastMoney."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        timeout_seconds: float = 8.0,
    ):
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Referer": "https://data.eastmoney.com/",
            }
        )
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._timeout_seconds = max(0.5, min(float(timeout_seconds), 30.0))

    def fetch(
        self,
        stock_code: str,
        *,
        max_reports: int = 5,
        lookback_days: int = 180,
    ) -> ResearchReportBundle:
        code = normalize_stock_code(str(stock_code or "").strip())
        as_of = _ensure_aware_utc(self._now_provider())
        safe_max_reports = max(1, min(int(max_reports), 20))
        safe_lookback_days = max(1, min(int(lookback_days), 730))
        if (
            get_market_for_stock(code) != "cn"
            or not code.isdigit()
            or len(code) != 6
        ):
            return ResearchReportBundle(
                stock_code=code,
                as_of=as_of,
                lookback_days=safe_lookback_days,
                status="unsupported",
                reports=(),
                source_status={"eastmoney_reportapi": "unsupported"},
            )

        cache_key = f"{code}:{safe_max_reports}:{safe_lookback_days}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        end_date = as_of.date()
        begin_date = end_date - timedelta(days=safe_lookback_days)
        params = {
            "industryCode": "*",
            "pageSize": str(min(100, max(20, safe_max_reports * 4))),
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": begin_date.isoformat(),
            "endTime": end_date.isoformat(),
            "pageNo": "1",
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": code,
            "rcode": "",
            "p": "1",
            "pageNum": "1",
            "pageNumber": "1",
        }
        try:
            response = self._session.get(
                _REPORT_API,
                params=params,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("invalid reportapi payload")
            reports = tuple(
                self._normalize_rows(
                    rows,
                    begin_date=begin_date,
                    end_date=end_date,
                )[:safe_max_reports]
            )
            bundle = ResearchReportBundle(
                stock_code=code,
                as_of=as_of,
                lookback_days=safe_lookback_days,
                status="available" if reports else "empty",
                reports=reports,
                source_status={
                    "eastmoney_reportapi": "success" if reports else "empty"
                },
            )
        except Exception as exc:
            logger.warning(
                "EastMoney research report fetch failed for %s: %s",
                code,
                type(exc).__name__,
            )
            bundle = ResearchReportBundle(
                stock_code=code,
                as_of=as_of,
                lookback_days=safe_lookback_days,
                status="missing",
                reports=(),
                source_status={"eastmoney_reportapi": "failed"},
                warnings=("eastmoney_reportapi_fetch_failed",),
            )
        self._put_cache(cache_key, bundle)
        return bundle

    @classmethod
    def _normalize_rows(
        cls,
        rows: Iterable[Dict[str, Any]],
        *,
        begin_date: date,
        end_date: date,
    ) -> List[ResearchReport]:
        reports: List[ResearchReport] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = _clean_text(row.get("title"))
            published_date = _parse_date(row.get("publishDate"))
            if not title or published_date is None:
                continue
            if not begin_date <= published_date <= end_date:
                continue
            info_code = _clean_text(row.get("infoCode"))
            dedupe_key = info_code or (
                f"{published_date.isoformat()}:{_clean_text(row.get('orgSName'))}:{title}"
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            report_url = (
                _PDF_URL_TEMPLATE.format(info_code=info_code)
                if _INFO_CODE_RE.fullmatch(info_code)
                else ""
            )
            reports.append(
                ResearchReport(
                    title=title,
                    organization=_clean_text(row.get("orgSName")),
                    published_date=published_date,
                    rating=_clean_text(row.get("emRatingName")),
                    rating_change=_clean_text(row.get("ratingChange")),
                    industry=_clean_text(
                        row.get("indvInduName") or row.get("industryName")
                    ),
                    eps_this_year=_safe_float(row.get("predictThisYearEps")),
                    eps_next_year=_safe_float(row.get("predictNextYearEps")),
                    eps_next_two_year=_safe_float(
                        row.get("predictNextTwoYearEps")
                    ),
                    url=report_url,
                )
            )
        return sorted(
            reports,
            key=lambda item: (item.published_date, item.organization, item.title),
            reverse=True,
        )

    @staticmethod
    def _get_cached(cache_key: str) -> Optional[ResearchReportBundle]:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _REPORT_CACHE.get(cache_key)
            if cached is None:
                return None
            cached_at, bundle = cached
            cache_ttl = (
                _FAILED_CACHE_TTL_SECONDS
                if bundle.status == "missing"
                else _CACHE_TTL_SECONDS
            )
            if now - cached_at <= cache_ttl:
                return bundle
            _REPORT_CACHE.pop(cache_key, None)
        return None

    @staticmethod
    def _put_cache(cache_key: str, bundle: ResearchReportBundle) -> None:
        now = time.monotonic()
        with _CACHE_LOCK:
            expired = [
                key
                for key, (cached_at, _bundle) in _REPORT_CACHE.items()
                if now - cached_at
                > (
                    _FAILED_CACHE_TTL_SECONDS
                    if _bundle.status == "missing"
                    else _CACHE_TTL_SECONDS
                )
            ]
            for key in expired:
                _REPORT_CACHE.pop(key, None)
            if (
                cache_key not in _REPORT_CACHE
                and len(_REPORT_CACHE) >= _CACHE_MAX_ENTRIES
            ):
                oldest_key = min(
                    _REPORT_CACHE,
                    key=lambda key: _REPORT_CACHE[key][0],
                )
                _REPORT_CACHE.pop(oldest_key, None)
            _REPORT_CACHE[cache_key] = (now, bundle)


def reset_research_report_cache() -> None:
    with _CACHE_LOCK:
        _REPORT_CACHE.clear()


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:10].replace("/", "-")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "AShareResearchReportService",
    "ResearchReport",
    "ResearchReportBundle",
    "reset_research_report_cache",
]
