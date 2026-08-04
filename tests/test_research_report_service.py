from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from src.agent.tools import search_tools
from src.search_service import SearchResponse, SearchResult, SearchService
from src.services.research_report_service import (
    AShareResearchReportService,
    ResearchReport,
    ResearchReportBundle,
    reset_research_report_cache,
)


NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload, *, error: Exception | None = None):
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self):
        return self.payload


class _Session:
    def __init__(self, response: _Response):
        self.headers = {}
        self.response = response
        self.get = MagicMock(return_value=response)


def setup_function() -> None:
    reset_research_report_cache()


def test_fetch_normalizes_recent_reports_and_preserves_opinion_provenance() -> None:
    session = _Session(
        _Response(
            {
                "data": [
                    {
                        "title": "渠道改革继续推进",
                        "publishDate": "2026-07-23 00:00:00.000",
                        "orgSName": "示例证券",
                        "infoCode": "AP202607231827290069",
                        "predictThisYearEps": "67.19",
                        "predictNextYearEps": "69.76",
                        "predictNextTwoYearEps": "73.96",
                        "emRatingName": "买入",
                        "ratingChange": "维持",
                        "indvInduName": "白酒Ⅱ",
                    },
                    {
                        "title": "渠道改革继续推进",
                        "publishDate": "2026-07-23 00:00:00.000",
                        "orgSName": "示例证券",
                        "infoCode": "AP202607231827290069",
                    },
                    {
                        "title": "过期研报",
                        "publishDate": "2025-01-01 00:00:00.000",
                        "orgSName": "旧机构",
                        "infoCode": "AP202501010000000001",
                    },
                    {
                        "title": "未来异常数据",
                        "publishDate": "2026-08-10 00:00:00.000",
                        "orgSName": "异常机构",
                        "infoCode": "AP202608100000000001",
                    },
                ]
            }
        )
    )
    service = AShareResearchReportService(
        session=session,
        now_provider=lambda: NOW,
    )

    bundle = service.fetch("600519.SH", max_reports=5, lookback_days=180)

    assert bundle.status == "available"
    assert bundle.source_status == {"eastmoney_reportapi": "success"}
    assert len(bundle.reports) == 1
    report = bundle.reports[0]
    assert report.organization == "示例证券"
    assert report.rating == "买入"
    assert report.eps_this_year == 67.19
    assert report.source_tier == "sell_side_aggregator"
    assert report.verification_status == "single_source_opinion"
    assert report.url == (
        "https://pdf.dfcfw.com/pdf/H3_AP202607231827290069_1.pdf"
    )
    payload = bundle.to_dict()
    assert payload["reports"][0]["eps_forecast"] == {
        "2026": 67.19,
        "2027": 69.76,
        "2028": 73.96,
    }
    prompt = bundle.to_prompt_context()
    assert "卖方观点，非公司事实" in prompt
    assert "不得当作公司公告" in prompt
    assert "2026E EPS=67.19" in prompt


def test_fetch_is_fail_open_when_endpoint_fails() -> None:
    session = _Session(_Response({}, error=TimeoutError("timeout")))
    service = AShareResearchReportService(
        session=session,
        now_provider=lambda: NOW,
    )

    bundle = service.fetch("600519")

    assert bundle.status == "missing"
    assert bundle.reports == ()
    assert bundle.source_status == {"eastmoney_reportapi": "failed"}
    assert bundle.warnings == ("eastmoney_reportapi_fetch_failed",)


def test_fetch_skips_non_a_share_without_network_call() -> None:
    session = _Session(_Response({"data": []}))
    service = AShareResearchReportService(
        session=session,
        now_provider=lambda: NOW,
    )

    bundle = service.fetch("AAPL")

    assert bundle.status == "unsupported"
    assert bundle.source_status == {"eastmoney_reportapi": "unsupported"}
    session.get.assert_not_called()


def test_search_service_converts_reports_without_general_search_key(
    monkeypatch,
) -> None:
    report = ResearchReport(
        title="需求根基稳固",
        organization="中邮证券",
        published_date=date(2026, 7, 23),
        rating="买入",
        rating_change="维持",
        industry="白酒Ⅱ",
        eps_this_year=67.19,
        eps_next_year=69.76,
        eps_next_two_year=73.96,
        url="https://pdf.dfcfw.com/pdf/H3_AP202607231827290069_1.pdf",
    )
    bundle = ResearchReportBundle(
        stock_code="600519",
        as_of=NOW,
        lookback_days=180,
        status="available",
        reports=(report,),
        source_status={"eastmoney_reportapi": "success"},
    )
    monkeypatch.setattr(
        AShareResearchReportService,
        "fetch",
        lambda self, stock_code, **kwargs: bundle,
    )
    service = SearchService(searxng_public_instances_enabled=False)

    response = service.search_stock_research_reports("600519")

    assert response.success is True
    assert response.provider == "EastMoneyResearch"
    assert response.results[0].relevance_score == 100
    assert "第三方卖方观点" in response.results[0].snippet


def test_comprehensive_intel_uses_direct_research_without_search_provider(
    monkeypatch,
) -> None:
    service = SearchService(searxng_public_instances_enabled=False)
    direct = SearchResponse(
        query="600519 A股券商研报元数据",
        results=[
            SearchResult(
                title="研报",
                snippet="卖方观点",
                url="https://example.invalid/report",
                source="示例证券 via 东方财富",
                published_date="2026-07-23",
            )
        ],
        provider="EastMoneyResearch",
        success=True,
    )
    monkeypatch.setattr(
        service,
        "search_stock_research_reports",
        lambda *args, **kwargs: direct,
    )

    results = service.search_comprehensive_intel(
        "600519",
        "贵州茅台",
        max_searches=3,
    )

    assert results == {"market_analysis": direct}


def test_comprehensive_tool_uses_direct_research_without_search_provider(
    monkeypatch,
) -> None:
    service = MagicMock()
    service.is_available = False
    service.search_comprehensive_intel.return_value = {
        "market_analysis": SearchResponse(
            query="600519 A股券商研报元数据",
            results=[
                SearchResult(
                    title="研报",
                    snippet="卖方观点",
                    url="https://example.invalid/report",
                    source="示例证券 via 东方财富",
                    published_date="2026-07-23",
                )
            ],
            provider="EastMoneyResearch",
            success=True,
        )
    }
    service.format_intel_report.return_value = "structured report"
    monkeypatch.setattr(search_tools, "_get_search_service", lambda: service)
    monkeypatch.setattr(search_tools, "_persist_news_response", lambda **kwargs: None)

    result = search_tools._handle_search_comprehensive_intel(
        stock_code="600519",
        stock_name="贵州茅台",
    )

    assert result["report"] == "structured report"
    assert result["dimensions"]["market_analysis"]["results_count"] == 1
