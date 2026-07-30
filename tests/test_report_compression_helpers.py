# -*- coding: utf-8 -*-
"""Regression tests for lossless report compaction helpers."""

from src.utils.data_processing import (
    compact_phase_data_limitations,
    format_signal_attribution_weights_line,
)


def test_compact_phase_data_limitations_keeps_details_and_collapses_statuses() -> None:
    compacted = compact_phase_data_limitations(
        [
            "行情日期早于分析日期，现价判断仅供参考",
            "quote: stale",
            "technical: partial",
            "quote: stale",
        ]
    )

    assert compacted == {
        "details": ["行情日期早于分析日期，现价判断仅供参考"],
        "status_line": "quote=stale · technical=partial",
    }


def test_compact_phase_data_limitations_does_not_reclassify_free_text_colons() -> None:
    compacted = compact_phase_data_limitations(
        ["provider: timeout after 10 seconds", "fundamentals: fetch_failed"]
    )

    assert compacted["details"] == ["provider: timeout after 10 seconds"]
    assert compacted["status_line"] == "fundamentals=fetch_failed"


def test_format_signal_attribution_weights_line_preserves_all_weights() -> None:
    line = format_signal_attribution_weights_line(
        {
            "technical_indicators": 45,
            "news_sentiment": 10,
            "fundamentals": 25,
            "market_conditions": 20,
        },
        {
            "technical_indicators_label": "技术",
            "news_sentiment_label": "新闻",
            "fundamentals_label": "基本面",
            "market_conditions_label": "市场",
        },
    )

    assert line == "📈 技术: 45% · 📰 新闻: 10% · 📊 基本面: 25% · 🌐 市场: 20%"
