# -*- coding: utf-8 -*-
"""
Search tools — wraps SearchService methods as agent-callable tools.

Tools:
- search_stock_news: search latest stock news
- search_comprehensive_intel: multi-dimensional intelligence search
- get_stock_events: fetch structured A-share disclosures and stock news events
- get_research_reports: fetch structured A-share sell-side report metadata
- get_regulatory_disclosures: fetch official SEC/HKEXnews issuer evidence
"""

import logging

from src.agent.tools.registry import ToolParameter, ToolDefinition, ToolPolicy

logger = logging.getLogger(__name__)

_NEWS_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_write_cache"],
    permissions=["news:read"],
    scope_dimensions=["stock"],
)
_INTEL_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_write_cache"],
    permissions=["intel:read"],
    scope_dimensions=["stock"],
)
_EVENT_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_write_cache"],
    permissions=["news:read"],
    scope_dimensions=["stock"],
)
_RESEARCH_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read"],
    permissions=["intel:read"],
    scope_dimensions=["stock"],
)
_REGULATORY_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read"],
    permissions=["intel:read"],
    scope_dimensions=["stock"],
)


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db
    return get_db()


def _get_search_service():
    """Return shared SearchService singleton."""
    from src.search_service import get_search_service
    return get_search_service()


def _canonical_search_code(stock_code: str) -> str:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    return canonical_stock_code(normalize_stock_code(str(stock_code or "").strip()))


def _persist_news_response(
    *,
    stock_code: str,
    stock_name: str,
    dimension: str,
    response,
) -> None:
    """Best-effort news persistence for Agent search tools."""
    if not response or not getattr(response, "success", False) or not getattr(response, "results", None):
        return

    code = _canonical_search_code(stock_code)
    try:
        saved_count = _get_db().save_news_intel(
            code=code,
            name=stock_name,
            dimension=dimension,
            query=response.query,
            response=response,
            query_context=None,
        )
        logger.info(
            "Agent news intel persisted for %s (dimension=%s, new_records=%s)",
            code,
            dimension,
            saved_count,
        )
    except Exception as exc:
        logger.warning(
            "Agent news intel persistence failed for %s (dimension=%s): %s",
            code,
            dimension,
            exc,
        )


def _handle_search_stock_news(stock_code: str, stock_name: str) -> dict:
    """Search latest news for a stock."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    response = service.search_stock_news(stock_code, stock_name, max_results=5)

    if not response.success:
        return {
            "query": response.query,
            "success": False,
            "error": response.error_message,
        }

    _persist_news_response(
        stock_code=stock_code,
        stock_name=stock_name,
        dimension="latest_news",
        response=response,
    )

    return {
        "query": response.query,
        "provider": response.provider,
        "success": True,
        "results_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "source": r.source,
                "published_date": r.published_date,
            }
            for r in response.results
        ],
    }


search_stock_news_tool = ToolDefinition(
    name="search_stock_news",
    description="Search for the latest news articles about a specific stock. "
                "Requires both stock_code and stock_name for accurate search. "
                "Returns news titles, snippets, sources, and URLs.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_stock_news,
    category="search",
    policy=_NEWS_READ_POLICY,
)


# ============================================================
# get_stock_events
# ============================================================

def _handle_get_stock_events(stock_code: str, stock_name: str) -> dict:
    """Fetch normalized A-share disclosures and stock-news events."""
    from src.services.stock_event_service import AShareStockEventService

    bundle = AShareStockEventService().fetch(stock_code, stock_name)
    return bundle.to_dict(include_events=True)


get_stock_events_tool = ToolDefinition(
    name="get_stock_events",
    description=(
        "Fetch recent structured events for an A-share stock from CNINFO official "
        "disclosures and EastMoney stock news. Returns event type, impact, "
        "materiality, source tier, publication time, and a deterministic event "
        "regime/score. Unsupported markets return status=unsupported."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Six-digit A-share stock code, e.g., '600519'.",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'.",
        ),
    ],
    handler=_handle_get_stock_events,
    category="search",
    policy=_EVENT_READ_POLICY,
)


# ============================================================
# get_research_reports
# ============================================================

def _handle_get_research_reports(stock_code: str) -> dict:
    """Fetch normalized A-share sell-side research metadata."""
    from src.services.research_report_service import (
        AShareResearchReportService,
    )

    return AShareResearchReportService().fetch(
        stock_code,
        max_reports=5,
        lookback_days=180,
    ).to_dict()


get_research_reports_tool = ToolDefinition(
    name="get_research_reports",
    description=(
        "Fetch recent A-share broker research report metadata by exact stock code. "
        "Returns institution, publication date, rating, EPS forecasts, provenance, "
        "and the original report link. Ratings and forecasts are third-party "
        "sell-side opinions, not verified company facts. Unsupported markets "
        "return status=unsupported."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Six-digit A-share stock code, e.g., '600519'.",
        ),
    ],
    handler=_handle_get_research_reports,
    category="search",
    policy=_RESEARCH_READ_POLICY,
)


# ============================================================
# get_regulatory_disclosures
# ============================================================

def _handle_get_regulatory_disclosures(stock_code: str, stock_name: str = "") -> dict:
    """Fetch official SEC or public HKEXnews issuer disclosures."""
    from src.services.regulatory_disclosure_service import RegulatoryDisclosureService

    return RegulatoryDisclosureService().fetch(
        stock_code,
        stock_name,
        max_filings=12,
        lookback_days=120,
    ).to_dict(include_items=True)


get_regulatory_disclosures_tool = ToolDefinition(
    name="get_regulatory_disclosures",
    description=(
        "Fetch official issuer evidence for US and Hong Kong stocks. US output "
        "combines SEC submissions (SEC-A) and point-in-time SEC XBRL company facts "
        "(SEC-B). Hong Kong output uses the public HKEXnews Title Search. Returns "
        "filing dates, form/category, original links, source tier, verification "
        "status, and source diagnostics. Unsupported markets return "
        "status=unsupported."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="US ticker or Hong Kong stock code, e.g., 'AAPL' or 'hk00700'.",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Optional issuer name.",
            required=False,
        ),
    ],
    handler=_handle_get_regulatory_disclosures,
    category="search",
    policy=_REGULATORY_READ_POLICY,
)


# ============================================================
# search_comprehensive_intel
# ============================================================

def _handle_search_comprehensive_intel(stock_code: str, stock_name: str) -> dict:
    """Multi-dimensional intelligence search."""
    service = _get_search_service()

    intel_results = service.search_comprehensive_intel(
        stock_code=stock_code,
        stock_name=stock_name,
        max_searches=6,
    )

    if not intel_results:
        if not service.is_available:
            return {"error": "No search engine available (no API keys configured)"}
        return {"error": "Comprehensive intel search returned no results"}

    # Format into readable report
    report = service.format_intel_report(intel_results, stock_name)

    # Also return structured data
    dimensions = {}
    for dim_name, response in intel_results.items():
        if response and response.success:
            _persist_news_response(
                stock_code=stock_code,
                stock_name=stock_name,
                dimension=dim_name,
                response=response,
            )
            dimensions[dim_name] = {
                "query": response.query,
                "results_count": len(response.results),
                "results": [
                    {
                        "title": r.title,
                        "snippet": r.snippet,
                        "source": r.source,
                    }
                    for r in response.results[:3]  # limit to 3 per dimension to save tokens
                ],
            }

    return {
        "report": report,
        "dimensions": dimensions,
    }


search_comprehensive_intel_tool = ToolDefinition(
    name="search_comprehensive_intel",
    description="Multi-dimensional intelligence search: latest news, market analysis, "
                "risk checking, earnings outlook, and industry trends for a stock. "
                "Returns a formatted report and structured results.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_comprehensive_intel,
    category="search",
    policy=_INTEL_READ_POLICY,
)


ALL_SEARCH_TOOLS = [
    search_stock_news_tool,
    get_stock_events_tool,
    get_research_reports_tool,
    get_regulatory_disclosures_tool,
    search_comprehensive_intel_tool,
]
