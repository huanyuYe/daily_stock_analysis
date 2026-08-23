"""Deterministic contracts for twice-daily cross-market theme reports."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.services.cross_market_theme_service import (
    CrossMarketThemeService,
    load_theme_catalog,
    merge_watchlist,
    parse_market_review_rankings,
    parse_report_decisions,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _report(*rows: tuple[str, str, str, int, str]) -> str:
    lines = ["# 决策仪表盘", "", "## 📊 分析结果摘要", ""]
    for name, code, action, score, trend in rows:
        lines.append(f"⚪ **{name}({code})**: {action} | 评分 {score} | {trend}")
    return "\n".join(lines)


def _catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "themes": [
                    {
                        "id": "cloud",
                        "label": "AI 云",
                        "keywords": ["AI cloud", "云计算"],
                        "us_proxies": ["CRWV", "NBIS", "ORCL"],
                        "official_symbols": ["ORCL"],
                        "target_symbols": ["HK00700", "002409"],
                    },
                    {
                        "id": "lithium",
                        "label": "锂资源",
                        "keywords": ["lithium", "锂"],
                        "us_proxies": ["ALB", "SQM", "TSLA"],
                        "official_symbols": ["ALB"],
                        "target_symbols": ["002460"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_report(
    root: Path,
    market: str,
    phase: str,
    text: str,
    now: datetime,
    *,
    prefix: str = "report",
) -> Path:
    directory = root / market / phase
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}_{now:%Y%m%d}.md"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (now.timestamp(), now.timestamp()))
    return path


class _FakeIntelligence:
    def refresh_auto_sources(self, *, force=False):
        return {"ok": True, "skipped": False, "saved_count": 1}

    def list_items(self, *, market, page, page_size):
        if market != "us":
            return {"items": [], "total": 0}
        return {
            "items": [
                {
                    "id": 1,
                    "title": "AI cloud demand expands",
                    "summary": "云计算 capital expenditure rises",
                    "url": "https://example.com/cloud",
                    "source_name": "Example RSS",
                    "market": "us",
                    "published_at": "2026-08-13T07:00:00+08:00",
                    "fetched_at": "2026-08-13T07:01:00+08:00",
                }
            ],
            "total": 1,
        }


class _FakeBundle:
    def __init__(self, code: str, status: str = "available"):
        self.code = code
        self.status = status

    def to_dict(self, *, include_items=True):
        return {
            "status": self.status,
            "as_of": "2026-08-13T01:00:00+00:00",
            "source_status": {"official": "available"},
            "warnings": [],
            "filings": [],
            "company_facts": [],
        }


class _FakeRegulatory:
    def __init__(self, *, failing_code: str = ""):
        self.failing_code = failing_code
        self.calls = []

    def fetch(self, code, name, *, max_filings, lookback_days):
        self.calls.append(code)
        if code == self.failing_code:
            raise RuntimeError("official endpoint unavailable")
        return _FakeBundle(code)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        cross_market_theme_config_path="",
        cross_market_theme_news_window_hours=36,
        cross_market_theme_proxy_request_interval_sec=0,
        cross_market_theme_max_news_per_theme=2,
    )


def test_catalog_and_watchlist_contracts_are_stable():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "themes.json"
        _catalog(path)
        themes = load_theme_catalog(path)

    assert [item.theme_id for item in themes] == ["cloud", "lithium"]
    assert merge_watchlist(["hk00700", "AAPL"], ["HK00700", "002409"]) == [
        "HK00700",
        "AAPL",
        "002409",
    ]


def test_bundled_catalog_covers_tencent_and_three_added_a_share_targets():
    root = Path(__file__).resolve().parents[1]
    themes = load_theme_catalog(root / "config" / "cross_market_themes.json")
    targets = {code for theme in themes for code in theme.target_symbols}

    assert {"HK00700", "002409", "688300", "688368"}.issubset(targets)


def test_report_summary_parser_preserves_action_and_trend():
    parsed = parse_report_decisions(
        _report(("腾讯控股", "HK00700", "持有", 67, "看多"))
    )

    assert parsed["HK00700"] == {
        "code": "HK00700",
        "name": "腾讯控股",
        "action": "持有",
        "score": 67,
        "trend": "看多",
    }


def test_market_review_ranking_parser_keeps_direction_and_category():
    parsed = parse_market_review_rankings(
        "\n".join(
            [
                "#### 行业板块领涨 Top 5",
                "| 排名 | 行业板块 | 涨跌幅 |",
                "|------|------|--------|",
                "| 1 | 云服务 | +2.50% |",
                "#### 概念板块领跌 Top 5",
                "| 排名 | 概念板块 | 涨跌幅 |",
                "|------|------|--------|",
                "| 1 | 锂电池 | -1.20% |",
            ]
        )
    )

    assert parsed["leaders"][0]["name"] == "云服务"
    assert parsed["leaders"][0]["category"] == "industry"
    assert parsed["laggards"][0]["change_pct"] == -1.2
    assert parsed["laggards"][0]["category"] == "concept"


def test_morning_and_close_reports_form_a_same_day_validation_loop():
    now_holder = [datetime(2026, 8, 13, 9, 25, tzinfo=SHANGHAI)]
    quote_changes = {
        "CRWV": 2.0,
        "NBIS": 1.5,
        "ORCL": 1.0,
        "ALB": -2.0,
        "SQM": -1.5,
        "TSLA": -1.0,
        "HK00700": 1.2,
        "002409": 0.8,
        "002460": -1.1,
    }

    def quote_loader(code):
        return {
            "code": code,
            "name": {"HK00700": "腾讯控股", "002409": "雅克科技", "002460": "赣锋锂业"}.get(code, code),
            "source": "test",
            "price": 100,
            "change_pct": quote_changes[code],
            "provider_timestamp": now_holder[0].isoformat(),
        }

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reports = root / "reports"
        catalog = root / "themes.json"
        _catalog(catalog)
        pre_report = _report(
            ("腾讯控股", "HK00700", "持有", 67, "看多"),
            ("雅克科技", "002409", "观望", 52, "震荡"),
            ("赣锋锂业", "002460", "观望", 45, "看空"),
        )
        _write_report(reports, "us", "postmarket", _report(("甲骨文", "ORCL", "观望", 55, "看多")), now_holder[0])
        _write_report(reports, "cn", "premarket", pre_report, now_holder[0])
        _write_report(reports, "hk", "premarket", pre_report, now_holder[0])
        regulatory = _FakeRegulatory()
        service = CrossMarketThemeService(
            project_root=root,
            reports_root=reports,
            output_root=reports / "theme",
            catalog_path=catalog,
            config=_config(),
            quote_loader=quote_loader,
            intelligence_service=_FakeIntelligence(),
            regulatory_service=regulatory,
            now_provider=lambda: now_holder[0],
            sleep_fn=lambda _seconds: None,
        )

        morning = service.generate("morning", ["HK00700", "002409", "002460"])
        morning_snapshot = json.loads(Path(morning["snapshot"]).read_text(encoding="utf-8"))

        assert morning["skipped"] is False
        assert [item["direction"] for item in morning_snapshot["themes"]] == [
            "strengthening",
            "weakening",
        ]
        cloud = morning_snapshot["themes"][0]
        assert cloud["evidence_complete"] is True
        assert cloud["proxies"][0]["fresh_for_report_purpose"] is True
        assert cloud["proxies"][0]["freshness_basis"]["max_age_hours"] == 30.0
        assert cloud["proxy_metrics"]["purpose_fresh_count"] == 3
        assert [item["code"] for item in cloud["targets"]] == ["HK00700", "002409"]
        assert cloud["targets"][0]["report_action"] == "持有"
        assert {"ORCL", "HK00700", "ALB"}.issubset(set(regulatory.calls))
        assert "上午结论属于待验证假设" in Path(morning["report"]).read_text(encoding="utf-8")

        now_holder[0] = datetime(2026, 8, 13, 16, 50, tzinfo=SHANGHAI)
        _write_report(reports, "cn", "postmarket", pre_report, now_holder[0])
        _write_report(reports, "hk", "postmarket", pre_report, now_holder[0])
        _write_report(
            reports,
            "cn",
            "postmarket",
            "\n".join(
                [
                    "#### 行业板块领涨 Top 5",
                    "| 排名 | 行业板块 | 涨跌幅 |",
                    "|------|------|--------|",
                    "| 1 | 云计算 | +2.50% |",
                    "#### 概念板块领跌 Top 5",
                    "| 排名 | 概念板块 | 涨跌幅 |",
                    "|------|------|--------|",
                    "| 1 | 锂资源 | -1.20% |",
                ]
            ),
            now_holder[0],
            prefix="market_review",
        )
        close = service.generate("close", ["HK00700", "002409", "002460"])
        close_snapshot = json.loads(Path(close["snapshot"]).read_text(encoding="utf-8"))

        assert close["skipped"] is False
        assert [item["validation"] for item in close_snapshot["themes"]] == [
            "confirmed",
            "confirmed_downside",
        ]
        assert close_snapshot["themes"][0]["targets"][0]["quote"]["change_pct"] == 1.2
        assert close_snapshot["themes"][0]["validation_scope"] == "watchlist_and_board"
        assert close_snapshot["themes"][0]["board_alignment"] == "aligned"
        assert close_snapshot["themes"][1]["board_alignment"] == "aligned"
        assert "主线验证不覆盖个股估值" in Path(close["report"]).read_text(encoding="utf-8")


def test_stale_us_report_fails_closed_before_quote_calls():
    now = datetime(2026, 8, 13, 9, 25, tzinfo=SHANGHAI)
    calls = []
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reports = root / "reports"
        catalog = root / "themes.json"
        _catalog(catalog)
        stale = now.replace(day=11)
        _write_report(reports, "us", "postmarket", "stale", stale)
        service = CrossMarketThemeService(
            project_root=root,
            reports_root=reports,
            catalog_path=catalog,
            config=_config(),
            quote_loader=lambda code: calls.append(code),
            intelligence_service=_FakeIntelligence(),
            regulatory_service=_FakeRegulatory(),
            now_provider=lambda: now,
            sleep_fn=lambda _seconds: None,
        )

        result = service.generate("morning", ["HK00700"])

    assert result["skipped"] is True
    assert result["reason"] == "fresh_us_postmarket_report_required"
    assert calls == []


def test_official_failure_is_visible_and_prevents_complete_evidence_claim():
    now = datetime(2026, 8, 13, 9, 25, tzinfo=SHANGHAI)
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reports = root / "reports"
        catalog = root / "themes.json"
        _catalog(catalog)
        _write_report(reports, "us", "postmarket", _report(("甲骨文", "ORCL", "观望", 50, "震荡")), now)
        service = CrossMarketThemeService(
            project_root=root,
            reports_root=reports,
            catalog_path=catalog,
            config=_config(),
            quote_loader=lambda code: {
                "code": code,
                "source": "test",
                "price": 1,
                "change_pct": 2,
                "provider_timestamp": now.isoformat(),
            },
            intelligence_service=_FakeIntelligence(),
            regulatory_service=_FakeRegulatory(failing_code="ORCL"),
            now_provider=lambda: now,
            sleep_fn=lambda _seconds: None,
        )

        result = service.generate("morning", ["HK00700"])
        snapshot = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))

    assert snapshot["themes"][0]["direction"] == "strengthening"
    assert snapshot["themes"][0]["evidence_complete"] is False
    assert snapshot["themes"][0]["official_checks"][0]["check_status"] == "failed"
