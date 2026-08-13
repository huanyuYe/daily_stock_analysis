# -*- coding: utf-8 -*-
"""Evidence-first, twice-daily cross-market theme tracking reports."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import Config, get_config
from src.core.trading_calendar import get_market_for_stock
from src.services.run_diagnostics import sanitize_diagnostic_text

logger = logging.getLogger(__name__)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SUMMARY_LINE_RE = re.compile(
    r"\*\*(?P<name>.+?)\((?P<code>[^)]+)\)\*\*:\s*"
    r"(?P<action>[^|]+)\|\s*评分\s*(?P<score>\d+)\s*\|\s*(?P<trend>[^\n]+)"
)
_ALLOWED_PHASES = {"morning", "close"}


@dataclass(frozen=True)
class ThemeSpec:
    theme_id: str
    label: str
    keywords: tuple[str, ...]
    board_keywords: tuple[str, ...]
    us_proxies: tuple[str, ...]
    official_symbols: tuple[str, ...]
    target_symbols: tuple[str, ...]


def merge_watchlist(
    portfolio_codes: Iterable[str],
    configured_codes: Iterable[str],
) -> list[str]:
    """Return a stable, normalized union with portfolio entries first."""

    merged: list[str] = []
    for raw in [*portfolio_codes, *configured_codes]:
        code = str(raw or "").strip().upper()
        if code and code not in merged:
            merged.append(code)
    return merged


def load_theme_catalog(path: Path) -> list[ThemeSpec]:
    """Load and validate the auditable cross-market theme catalog."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("version") or 0) != 1:
        raise ValueError("cross-market theme catalog version must be 1")
    raw_themes = payload.get("themes")
    if not isinstance(raw_themes, list) or not raw_themes:
        raise ValueError("cross-market theme catalog must contain themes")

    themes: list[ThemeSpec] = []
    seen: set[str] = set()
    for raw in raw_themes:
        if not isinstance(raw, Mapping):
            raise ValueError("each cross-market theme must be an object")
        theme_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not theme_id or not label or theme_id in seen:
            raise ValueError("cross-market theme ids and labels must be unique and non-empty")
        seen.add(theme_id)
        keywords = _string_tuple(raw.get("keywords"), preserve_case=True)
        board_keywords = _string_tuple(
            raw.get("board_keywords"), preserve_case=True
        ) or keywords
        proxies = _string_tuple(raw.get("us_proxies"))
        targets = _string_tuple(raw.get("target_symbols"))
        official = _string_tuple(raw.get("official_symbols"))
        if not keywords or not proxies or not targets:
            raise ValueError(f"cross-market theme {theme_id} requires keywords, proxies and targets")
        themes.append(
            ThemeSpec(
                theme_id=theme_id,
                label=label,
                keywords=keywords,
                board_keywords=board_keywords,
                us_proxies=proxies,
                official_symbols=official,
                target_symbols=targets,
            )
        )
    return themes


def parse_report_decisions(text: str) -> dict[str, dict[str, Any]]:
    """Extract only the rendered summary contract from an aggregate report."""

    result: dict[str, dict[str, Any]] = {}
    for match in _SUMMARY_LINE_RE.finditer(text or ""):
        code = match.group("code").strip().upper()
        result[code] = {
            "code": code,
            "name": match.group("name").strip(),
            "action": match.group("action").strip(),
            "score": int(match.group("score")),
            "trend": match.group("trend").strip(),
        }
    return result


def parse_market_review_rankings(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse rendered top/bottom industry and concept ranking tables."""

    result: dict[str, list[dict[str, Any]]] = {"leaders": [], "laggards": []}
    current: Optional[tuple[str, str]] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if line.startswith("#"):
            if "领涨" in line or "leading" in lowered:
                direction = "leaders"
            elif "领跌" in line or "lagging" in lowered:
                direction = "laggards"
            else:
                current = None
                continue
            category = (
                "concept"
                if "概念" in line or "concept" in lowered
                else "industry"
            )
            current = (direction, category)
            continue
        if current is None or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        change = _safe_float(cells[2].replace("%", "").replace("+", ""))
        if not cells[1] or change is None:
            continue
        direction, category = current
        result[direction].append(
            {
                "rank": int(cells[0]),
                "name": cells[1],
                "change_pct": change,
                "category": category,
            }
        )
    return result


class CrossMarketThemeService:
    """Build a US-catalyst morning report and an A/HK close validation report."""

    def __init__(
        self,
        *,
        project_root: Path,
        reports_root: Optional[Path] = None,
        output_root: Optional[Path] = None,
        config: Optional[Config] = None,
        catalog_path: Optional[Path] = None,
        quote_loader: Optional[Callable[[str], Any]] = None,
        intelligence_service: Any = None,
        regulatory_service: Any = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project_root = Path(project_root)
        self.reports_root = Path(reports_root or self.project_root / "reports")
        self.output_root = Path(
            output_root or self.reports_root / "cross_market_theme"
        )
        self.config = config or get_config()
        configured_path = str(
            getattr(self.config, "cross_market_theme_config_path", "") or ""
        ).strip()
        selected_catalog = catalog_path or (
            Path(configured_path).expanduser() if configured_path else None
        )
        if selected_catalog is not None and not selected_catalog.is_absolute():
            selected_catalog = self.project_root / selected_catalog
        self.catalog_path = selected_catalog or (
            self.project_root / "config" / "cross_market_themes.json"
        )
        self.themes = load_theme_catalog(self.catalog_path)
        self._quote_loader = quote_loader
        self._intelligence_service = intelligence_service
        self._regulatory_service = regulatory_service
        self._now_provider = now_provider or (lambda: datetime.now(_SHANGHAI))
        self._sleep = sleep_fn

    def generate(self, phase: str, watchlist: Sequence[str]) -> dict[str, Any]:
        normalized_phase = str(phase or "").strip().lower()
        if normalized_phase not in _ALLOWED_PHASES:
            raise ValueError("phase must be morning or close")
        now = _as_shanghai(self._now_provider())
        normalized_watchlist = merge_watchlist([], watchlist)
        active_themes = [
            (theme, [code for code in theme.target_symbols if code in normalized_watchlist])
            for theme in self.themes
        ]
        active_themes = [(theme, targets) for theme, targets in active_themes if targets]
        if not active_themes:
            return self._skipped(normalized_phase, now, "no_catalog_targets_in_watchlist")
        if normalized_phase == "morning":
            return self._generate_morning(now, normalized_watchlist, active_themes)
        return self._generate_close(now, normalized_watchlist, active_themes)

    def _generate_morning(
        self,
        now: datetime,
        watchlist: list[str],
        active_themes: list[tuple[ThemeSpec, list[str]]],
    ) -> dict[str, Any]:
        us_max_age = 72.0 if now.weekday() == 0 else 18.0
        source_reports = {
            "us_postmarket": self._load_report("us", "postmarket", now, us_max_age),
            "us_market_review": self._load_report(
                "us", "postmarket", now, us_max_age, prefix="market_review"
            ),
            "cn_premarket": self._load_report("cn", "premarket", now, 4.0),
            "hk_premarket": self._load_report("hk", "premarket", now, 4.0),
        }
        if source_reports["us_postmarket"]["status"] != "available":
            return self._skipped(
                "morning",
                now,
                "fresh_us_postmarket_report_required",
                source_reports=source_reports,
            )

        decisions = self._merge_report_decisions(source_reports.values())
        news_window = max(
            float(getattr(self.config, "cross_market_theme_news_window_hours", 36)),
            72.0 if now.weekday() == 0 else 0.0,
        )
        news_items, news_status = self._load_news(now, news_window)

        proxy_codes = _stable_unique(
            code for theme, _targets in active_themes for code in theme.us_proxies
        )
        quotes = self._load_quotes(proxy_codes, max_age_hours=72.0 if now.weekday() == 0 else 30.0)

        official_codes = _stable_unique(
            [
                *(code for theme, _targets in active_themes for code in theme.official_symbols),
                *(
                    code
                    for _theme, targets in active_themes
                    for code in targets
                    if get_market_for_stock(code) == "hk"
                ),
            ]
        )
        official = self._load_official(official_codes, decisions, now, news_window)
        board_rankings = self._merge_board_rankings(source_reports.values())

        theme_rows: list[dict[str, Any]] = []
        for theme, targets in active_themes:
            proxy_rows = [quotes[code] for code in theme.us_proxies if code in quotes]
            metrics = _quote_metrics(proxy_rows, len(theme.us_proxies))
            direction = _morning_direction(metrics)
            matched_news = self._match_news(theme, news_items)
            requested_official = _stable_unique(
                [
                    *theme.official_symbols,
                    *(code for code in targets if get_market_for_stock(code) == "hk"),
                ]
            )
            official_checks = [official[code] for code in requested_official if code in official]
            board_evidence = _match_board_rankings(theme, board_rankings)
            evidence_complete = (
                metrics["coverage_ratio"] >= 2 / 3
                and bool(requested_official)
                and len(official_checks) == len(requested_official)
                and all(item["check_status"] in {"available", "empty"} for item in official_checks)
            )
            theme_rows.append(
                {
                    "id": theme.theme_id,
                    "label": theme.label,
                    "direction": direction,
                    "proxy_metrics": metrics,
                    "proxies": proxy_rows,
                    "targets": [
                        self._target_context(code, decisions, quote=None) for code in targets
                    ],
                    "news": matched_news,
                    "board_evidence": board_evidence,
                    "official_checks": official_checks,
                    "evidence_complete": evidence_complete,
                }
            )
        theme_rows.sort(
            key=lambda row: (
                row["proxy_metrics"]["average_change_pct"] is not None,
                row["proxy_metrics"]["average_change_pct"] or -math.inf,
            ),
            reverse=True,
        )
        snapshot = {
            "schema_version": 1,
            "phase": "morning",
            "as_of": now.isoformat(),
            "watchlist": watchlist,
            "catalog": str(self.catalog_path),
            "source_reports": _strip_report_text(source_reports),
            "news_status": news_status,
            "themes": theme_rows,
            "unmapped_watchlist": self._unmapped_watchlist(watchlist),
        }
        markdown = _render_morning(snapshot)
        return self._persist("morning", now, snapshot, markdown)

    def _generate_close(
        self,
        now: datetime,
        watchlist: list[str],
        active_themes: list[tuple[ThemeSpec, list[str]]],
    ) -> dict[str, Any]:
        morning_path = self._snapshot_path("morning", now)
        if not morning_path.is_file():
            return self._skipped("close", now, "same_day_morning_snapshot_required")
        try:
            morning = json.loads(morning_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Cross-market morning snapshot is unreadable: %s", exc)
            return self._skipped("close", now, "morning_snapshot_unreadable")
        morning_as_of = _parse_datetime(morning.get("as_of"))
        if morning_as_of is None or _as_shanghai(morning_as_of).date() != now.date():
            return self._skipped("close", now, "same_day_morning_snapshot_required")

        source_reports = {
            "cn_postmarket": self._load_report("cn", "postmarket", now, 4.0),
            "hk_postmarket": self._load_report("hk", "postmarket", now, 4.0),
            "cn_market_review": self._load_report(
                "cn", "postmarket", now, 4.0, prefix="market_review"
            ),
            "hk_market_review": self._load_report(
                "hk", "postmarket", now, 4.0, prefix="market_review"
            ),
        }
        if not any(
            source_reports[key]["status"] == "available"
            for key in ("cn_postmarket", "hk_postmarket")
        ):
            return self._skipped(
                "close",
                now,
                "fresh_cn_or_hk_postmarket_report_required",
                source_reports=source_reports,
            )
        decisions = self._merge_report_decisions(source_reports.values())
        target_codes = _stable_unique(
            code for _theme, targets in active_themes for code in targets
        )
        quotes = self._load_quotes(target_codes, max_age_hours=8.0)
        news_window = float(
            getattr(self.config, "cross_market_theme_news_window_hours", 36)
        )
        news_items, news_status = self._load_news(now, min(news_window, 18.0))
        hk_codes = [code for code in target_codes if get_market_for_stock(code) == "hk"]
        official = self._load_official(hk_codes, decisions, now, min(news_window, 18.0))
        board_rankings = self._merge_board_rankings(source_reports.values())

        morning_by_id = {
            str(item.get("id")): item for item in morning.get("themes", []) if isinstance(item, Mapping)
        }
        theme_rows: list[dict[str, Any]] = []
        for theme, targets in active_themes:
            morning_theme = morning_by_id.get(theme.theme_id)
            if morning_theme is None:
                continue
            target_quotes = [quotes[code] for code in targets if code in quotes]
            metrics = _quote_metrics(target_quotes, len(targets))
            watchlist_validation = _close_validation(
                str(morning_theme.get("direction") or "unavailable"), metrics
            )
            board_evidence = _match_board_rankings(theme, board_rankings)
            board_alignment = _board_alignment(
                str(morning_theme.get("direction") or "unavailable"), board_evidence
            )
            validation = _combine_validation(watchlist_validation, board_alignment)
            matched_news = self._match_news(theme, news_items)
            official_checks = [official[code] for code in targets if code in official]
            theme_rows.append(
                {
                    "id": theme.theme_id,
                    "label": theme.label,
                    "morning_direction": morning_theme.get("direction"),
                    "morning_proxy_metrics": morning_theme.get("proxy_metrics", {}),
                    "validation": validation,
                    "watchlist_validation": watchlist_validation,
                    "validation_scope": (
                        "watchlist_and_board"
                        if board_evidence["leaders"] or board_evidence["laggards"]
                        else "watchlist_only"
                    ),
                    "board_alignment": board_alignment,
                    "board_evidence": board_evidence,
                    "target_metrics": metrics,
                    "targets": [
                        self._target_context(code, decisions, quote=quotes.get(code))
                        for code in targets
                    ],
                    "news": matched_news,
                    "official_checks": official_checks,
                }
            )
        snapshot = {
            "schema_version": 1,
            "phase": "close",
            "as_of": now.isoformat(),
            "morning_snapshot": str(morning_path),
            "morning_as_of": morning.get("as_of"),
            "watchlist": watchlist,
            "source_reports": _strip_report_text(source_reports),
            "news_status": news_status,
            "themes": theme_rows,
            "unmapped_watchlist": self._unmapped_watchlist(watchlist),
        }
        markdown = _render_close(snapshot)
        return self._persist("close", now, snapshot, markdown)

    def _load_report(
        self,
        market: str,
        phase: str,
        now: datetime,
        max_age_hours: float,
        *,
        prefix: str = "report",
    ) -> dict[str, Any]:
        directory = self.reports_root / market / phase
        candidates = sorted(
            directory.glob(f"{prefix}_*.md") if directory.is_dir() else [],
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if not candidates:
            return {"status": "missing", "market": market, "phase": phase}
        path = candidates[0]
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_hours = max(0.0, (now.astimezone(timezone.utc) - modified).total_seconds() / 3600)
        result = {
            "status": "available" if age_hours <= max_age_hours else "stale",
            "market": market,
            "phase": phase,
            "kind": prefix,
            "path": str(path),
            "modified_at": modified.isoformat(),
            "age_hours": round(age_hours, 2),
            "max_age_hours": max_age_hours,
        }
        if result["status"] == "available":
            result["text"] = path.read_text(encoding="utf-8")
        return result

    def _merge_report_decisions(
        self,
        reports: Iterable[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for report in reports:
            if report.get("status") == "available":
                merged.update(parse_report_decisions(str(report.get("text") or "")))
        return merged

    def _merge_board_rankings(
        self,
        reports: Iterable[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        merged: dict[str, list[dict[str, Any]]] = {"leaders": [], "laggards": []}
        for report in reports:
            if report.get("status") != "available" or report.get("kind") != "market_review":
                continue
            parsed = parse_market_review_rankings(str(report.get("text") or ""))
            for direction in ("leaders", "laggards"):
                for item in parsed[direction]:
                    merged[direction].append(
                        {
                            **item,
                            "market": report.get("market"),
                            "source_report": report.get("path"),
                        }
                    )
        return merged

    def _load_quotes(
        self,
        codes: Sequence[str],
        *,
        max_age_hours: float,
    ) -> dict[str, dict[str, Any]]:
        loader = self._quote_loader
        if loader is None:
            from data_provider.base import DataFetcherManager

            manager = DataFetcherManager()

            def load_quote(code: str) -> Any:
                return manager.get_realtime_quote(code, log_final_failure=False)

            loader = load_quote
            self._quote_loader = loader
        spacing = max(
            0.0,
            float(
                getattr(
                    self.config,
                    "cross_market_theme_proxy_request_interval_sec",
                    0.25,
                )
            ),
        )
        rows: dict[str, dict[str, Any]] = {}
        for index, code in enumerate(codes):
            if index and spacing:
                self._sleep(spacing)
            try:
                raw = loader(code)
                rows[code] = _quote_to_dict(code, raw, self._now_provider(), max_age_hours)
            except Exception as exc:
                rows[code] = {
                    "code": code,
                    "status": "failed",
                    "error": sanitize_diagnostic_text(str(exc), max_length=180) or type(exc).__name__,
                }
        return rows

    def _load_news(
        self,
        now: datetime,
        window_hours: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        service = self._intelligence_service
        if service is None:
            from src.services.intelligence_service import IntelligenceService

            service = IntelligenceService(config=self.config)
            self._intelligence_service = service
        refresh: dict[str, Any]
        try:
            refresh = dict(service.refresh_auto_sources(force=False))
        except Exception as exc:
            refresh = {
                "ok": False,
                "error": sanitize_diagnostic_text(str(exc), max_length=180) or type(exc).__name__,
            }
        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        for market in ("us", "global", "cn", "hk"):
            try:
                page = service.list_items(market=market, page=1, page_size=100)
                collected.extend(page.get("items") or [])
            except Exception as exc:
                errors.append(
                    f"{market}:{sanitize_diagnostic_text(str(exc), max_length=120) or type(exc).__name__}"
                )
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=max(1.0, window_hours))
        recent: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in collected:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("url") or item.get("id") or "")
            if not key or key in seen:
                continue
            observed_at = _parse_datetime(item.get("published_at")) or _parse_datetime(
                item.get("fetched_at")
            )
            if observed_at is not None and observed_at.astimezone(timezone.utc) < cutoff:
                continue
            seen.add(key)
            recent.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "summary": str(item.get("summary") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "source": str(item.get("source_name") or item.get("source") or "unknown").strip(),
                    "market": str(item.get("market") or "unknown").strip(),
                    "published_at": item.get("published_at"),
                    "fetched_at": item.get("fetched_at"),
                    "time_basis": "published_at" if item.get("published_at") else "fetched_at",
                }
            )
        recent.sort(
            key=lambda item: _parse_datetime(item.get("published_at") or item.get("fetched_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return recent, {
            "refresh": refresh,
            "query_errors": errors,
            "recent_item_count": len(recent),
            "window_hours": window_hours,
        }

    def _match_news(
        self,
        theme: ThemeSpec,
        news_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        max_items = max(
            1,
            int(getattr(self.config, "cross_market_theme_max_news_per_theme", 2)),
        )
        keywords = [keyword.casefold() for keyword in theme.keywords]
        matches: list[dict[str, Any]] = []
        for item in news_items:
            searchable = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
            hit = [keyword for keyword in keywords if keyword in searchable]
            if not hit:
                continue
            row = dict(item)
            row["matched_keywords"] = hit[:4]
            matches.append(row)
            if len(matches) >= max_items:
                break
        return matches

    def _load_official(
        self,
        codes: Sequence[str],
        decisions: Mapping[str, Mapping[str, Any]],
        now: datetime,
        window_hours: float,
    ) -> dict[str, dict[str, Any]]:
        service = self._regulatory_service
        if service is None:
            from src.services.regulatory_disclosure_service import RegulatoryDisclosureService

            service = RegulatoryDisclosureService(config=self.config)
            self._regulatory_service = service
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=max(1.0, window_hours))
        result: dict[str, dict[str, Any]] = {}
        for code in codes:
            name = str((decisions.get(code) or {}).get("name") or "")
            try:
                bundle = service.fetch(code, name, max_filings=6, lookback_days=14)
                payload = bundle.to_dict(include_items=True)
                recent_filings = []
                for filing in payload.get("filings") or []:
                    filed_at = _parse_datetime(filing.get("filed_at"))
                    if filed_at is None or filed_at.astimezone(timezone.utc) >= cutoff:
                        recent_filings.append(filing)
                raw_status = str(payload.get("status") or "unknown")
                if raw_status in {"available", "empty"}:
                    check_status = raw_status
                elif raw_status in {"unsupported", "disabled"}:
                    check_status = raw_status
                else:
                    check_status = "failed"
                result[code] = {
                    "code": code,
                    "name": name,
                    "check_status": check_status,
                    "bundle_status": raw_status,
                    "as_of": payload.get("as_of"),
                    "source_status": payload.get("source_status") or {},
                    "warnings": payload.get("warnings") or [],
                    "recent_filings": recent_filings[:3],
                }
            except Exception as exc:
                result[code] = {
                    "code": code,
                    "name": name,
                    "check_status": "failed",
                    "error": sanitize_diagnostic_text(str(exc), max_length=180) or type(exc).__name__,
                }
        return result

    def _target_context(
        self,
        code: str,
        decisions: Mapping[str, Mapping[str, Any]],
        *,
        quote: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        decision = dict(decisions.get(code) or {})
        result = {
            "code": code,
            "market": get_market_for_stock(code) or "unknown",
            "name": decision.get("name") or (quote or {}).get("name") or "",
            "report_action": decision.get("action"),
            "report_score": decision.get("score"),
            "report_trend": decision.get("trend"),
        }
        if quote is not None:
            result["quote"] = dict(quote)
        return result

    def _unmapped_watchlist(self, watchlist: Sequence[str]) -> list[str]:
        mapped = {code for theme in self.themes for code in theme.target_symbols}
        return [code for code in watchlist if code not in mapped]

    def _snapshot_path(self, phase: str, now: datetime) -> Path:
        return self.output_root / phase / f"theme_{now:%Y%m%d}.json"

    def _persist(
        self,
        phase: str,
        now: datetime,
        snapshot: dict[str, Any],
        markdown: str,
    ) -> dict[str, Any]:
        directory = self.output_root / phase
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"theme_{now:%Y%m%d}.json"
        report_path = directory / f"theme_{now:%Y%m%d}.md"
        _atomic_write(json_path, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(report_path, markdown.rstrip() + "\n")
        return {
            "success": True,
            "skipped": False,
            "phase": phase,
            "as_of": now.isoformat(),
            "report": str(report_path),
            "snapshot": str(json_path),
            "content": markdown,
            "theme_count": len(snapshot.get("themes") or []),
        }

    @staticmethod
    def _skipped(
        phase: str,
        now: datetime,
        reason: str,
        *,
        source_reports: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": True,
            "skipped": True,
            "phase": phase,
            "as_of": now.isoformat(),
            "reason": reason,
        }
        if source_reports is not None:
            result["source_reports"] = _strip_report_text(source_reports)
        return result


def _render_morning(snapshot: Mapping[str, Any]) -> str:
    lines = [
        "# 跨市场主线跟踪 · 美股收盘映射",
        "",
        f"> 截至北京时间 {snapshot['as_of']}。观察美股已完成交易时段的产业代理表现，映射至当前 A/HK 关注池；不替代逐股买卖结论。",
        "",
        "## 主线概览",
        "",
        "| 排名 | 主线 | 美股代理均值 | 上涨广度 | 方向 | A/HK 关注标的 | 证据完整性 |",
        "|---:|---|---:|---:|---|---|---|",
    ]
    for index, theme in enumerate(snapshot.get("themes") or [], 1):
        metrics = theme["proxy_metrics"]
        lines.append(
            f"| {index} | {theme['label']} | {_pct(metrics.get('average_change_pct'))} | "
            f"{_ratio(metrics.get('positive_ratio'))} | {_direction_label(theme.get('direction'))} | "
            f"{', '.join(target['code'] for target in theme['targets'])} | "
            f"{'完整' if theme.get('evidence_complete') else '部分'} |"
        )
    lines.extend(["", "## 分主题证据", ""])
    for theme in snapshot.get("themes") or []:
        lines.append(f"### {theme['label']} · {_direction_label(theme.get('direction'))}")
        lines.append("")
        lines.append("- 美股代理：" + _render_quote_list(theme.get("proxies") or []))
        lines.append(
            "- 美股板块榜单：" + _render_board_evidence(theme.get("board_evidence") or {})
        )
        lines.append("- A/HK 映射：" + _render_target_list(theme.get("targets") or []))
        if theme.get("news"):
            lines.append("- 事件证据：")
            for item in theme["news"]:
                published = item.get("published_at") or item.get("fetched_at") or "时间未知"
                lines.append(
                    f"  - [{_escape_link_text(item.get('title') or '未命名资讯')}]({item.get('url')}) "
                    f"— {item.get('source')}，{published}（{item.get('time_basis')}）"
                )
        else:
            lines.append("- 事件证据：精选资讯池在当前窗口内未命中；不能据此推断不存在催化。")
        lines.append("- 官方披露检查：" + _render_official(theme.get("official_checks") or []))
        lines.append("")
    lines.extend(_render_source_status(snapshot))
    lines.extend(
        [
            "",
            "## 判定边界",
            "",
            "- ‘强化/走弱’要求代理均值与多数方向同时满足阈值；‘分化’表示代理之间未形成一致方向。",
            "- 资讯只做事件证据，缺少发布时间的条目按抓取时间标注且降低可验证性；官方披露失败会让证据完整性降级。",
            "- 上午结论属于待验证假设，下午报告会用 A/HK 实际收盘涨跌与逐股报告观点进行确认或证伪。",
        ]
    )
    return "\n".join(lines)


def _render_close(snapshot: Mapping[str, Any]) -> str:
    lines = [
        "# 跨市场主线复盘 · A/HK 收盘验证",
        "",
        f"> 截至北京时间 {snapshot['as_of']}；上午基线 {snapshot.get('morning_as_of')}。本报告只验证跨市场催化是否传导，不自动生成买卖指令。",
        "",
        "## 验证概览",
        "",
        "| 主线 | 上午假设 | A/HK 均值 | 上涨广度 | 板块榜单 | 验证结果 | 数据覆盖 |",
        "|---|---|---:|---:|---|---|---:|",
    ]
    for theme in snapshot.get("themes") or []:
        metrics = theme["target_metrics"]
        lines.append(
            f"| {theme['label']} | {_direction_label(theme.get('morning_direction'))} | "
            f"{_pct(metrics.get('average_change_pct'))} | {_ratio(metrics.get('positive_ratio'))} | "
            f"{_board_alignment_label(theme.get('board_alignment'))} | "
            f"{_validation_label(theme.get('validation'))} | {_ratio(metrics.get('coverage_ratio'))} |"
        )
    lines.extend(["", "## 分主题复盘", ""])
    for theme in snapshot.get("themes") or []:
        lines.append(f"### {theme['label']} · {_validation_label(theme.get('validation'))}")
        lines.append("")
        lines.append(
            f"- 上午美股假设：{_direction_label(theme.get('morning_direction'))}；"
            f"代理均值 {_pct((theme.get('morning_proxy_metrics') or {}).get('average_change_pct'))}。"
        )
        lines.append("- A/HK 收盘表现：" + _render_target_list(theme.get("targets") or [], include_quote=True))
        lines.append(
            "- A/HK 板块榜单："
            + _render_board_evidence(theme.get("board_evidence") or {})
            + f"；验证范围={theme.get('validation_scope')}。"
        )
        if theme.get("news"):
            lines.append("- 日内新增事件：")
            for item in theme["news"]:
                published = item.get("published_at") or item.get("fetched_at") or "时间未知"
                lines.append(
                    f"  - [{_escape_link_text(item.get('title') or '未命名资讯')}]({item.get('url')}) "
                    f"— {item.get('source')}，{published}（{item.get('time_basis')}）"
                )
        else:
            lines.append("- 日内新增事件：精选资讯池未命中，不作为无事件的证明。")
        lines.append("- 港股官方披露检查：" + _render_official(theme.get("official_checks") or []))
        lines.append("")
    lines.extend(_render_source_status(snapshot))
    lines.extend(
        [
            "",
            "## 验证规则",
            "",
            "- 同向且均值达到 0.5%、多数标的同向，记为‘已验证’；反向达到同一阈值，记为‘已证伪’；其余为‘分化/待观察’。",
            "- 行情覆盖不足 60% 时直接标为‘数据不足’，不会用少数样本替代板块结论。",
            "- 逐股报告动作与主线方向是两个层次：主线验证不覆盖个股估值、位置、止损和持仓约束。",
        ]
    )
    return "\n".join(lines)


def _render_source_status(snapshot: Mapping[str, Any]) -> list[str]:
    lines = ["", "## 数据完整性", ""]
    for name, item in (snapshot.get("source_reports") or {}).items():
        suffix = f"，age={item.get('age_hours')}h" if item.get("age_hours") is not None else ""
        lines.append(
            f"- {name}: {item.get('status')}{suffix}；"
            f"{_report_path_label(item.get('path'))}"
        )
    news = snapshot.get("news_status") or {}
    refresh = news.get("refresh") or {}
    lines.append(
        f"- 精选资讯池：recent={news.get('recent_item_count', 0)}，"
        f"refresh_ok={refresh.get('ok')}，query_errors={len(news.get('query_errors') or [])}。"
    )
    unmapped = snapshot.get("unmapped_watchlist") or []
    lines.append(f"- 未映射关注标的：{', '.join(unmapped) if unmapped else '无'}。")
    return lines


def _render_quote_list(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "无可用代理行情"
    parts = []
    for row in rows:
        if row.get("status") != "available":
            parts.append(f"{row.get('code')}=失败")
            continue
        time_flag = "可核时" if row.get("timestamp_verified") else "时间未核"
        parts.append(
            f"{row.get('name') or row.get('code')}({row.get('code')}) {_pct(row.get('change_pct'))} "
            f"[{row.get('source')},{time_flag}]"
        )
    return "；".join(parts)


def _render_target_list(
    rows: Sequence[Mapping[str, Any]],
    *,
    include_quote: bool = False,
) -> str:
    parts = []
    for row in rows:
        label = f"{row.get('name') or row.get('code')}({row.get('code')})"
        if include_quote:
            quote = row.get("quote") or {}
            label += f" {_pct(quote.get('change_pct')) if quote.get('status') == 'available' else '行情失败'}"
        if row.get("report_action"):
            label += f" / 逐股:{row.get('report_action')}·{row.get('report_trend')}"
        parts.append(label)
    return "；".join(parts) if parts else "无当前关注标的"


def _render_official(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "本主线没有需要单独查询的 SEC/HKEX 发行人"
    parts = []
    for row in rows:
        recent = row.get("recent_filings") or []
        label = f"{row.get('code')}={row.get('check_status')}"
        if recent:
            filing = recent[0]
            label += f"，最新[{filing.get('form_type') or 'disclosure'}] {filing.get('title')}"
        parts.append(label)
    return "；".join(parts)


def _render_board_evidence(evidence: Mapping[str, Any]) -> str:
    parts = []
    for direction, label in (("leaders", "领涨"), ("laggards", "领跌")):
        rows = evidence.get(direction) or []
        if rows:
            parts.append(
                f"{label}:"
                + "、".join(
                    f"{item.get('name')}({_pct(item.get('change_pct'))},{item.get('market')})"
                    for item in rows
                )
            )
    return "；".join(parts) if parts else "相关行业/概念未进入现有 Top/Bottom 榜单"


def _quote_to_dict(
    code: str,
    raw: Any,
    now: datetime,
    max_age_hours: float,
) -> dict[str, Any]:
    if raw is None:
        return {"code": code, "status": "unavailable"}
    payload = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
    change = _safe_float(payload.get("change_pct", payload.get("change_percent")))
    price = _safe_float(payload.get("price"))
    provider_at = _parse_datetime(payload.get("provider_timestamp"))
    timestamp_verified = False
    age_hours: Optional[float] = None
    if provider_at is not None:
        age_hours = max(
            0.0,
            (_as_shanghai(now).astimezone(timezone.utc) - provider_at.astimezone(timezone.utc)).total_seconds()
            / 3600,
        )
        timestamp_verified = age_hours <= max_age_hours
    source = payload.get("source")
    source_value = getattr(source, "value", source) or "unknown"
    return {
        "code": code,
        "name": str(payload.get("name") or "").strip(),
        "status": "available" if change is not None else "partial",
        "price": price,
        "change_pct": change,
        "source": str(source_value),
        "provider_timestamp": _serializable_time(payload.get("provider_timestamp")),
        "fetched_at": _serializable_time(payload.get("fetched_at")),
        "timestamp_verified": timestamp_verified,
        "provider_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "provider_is_stale": payload.get("is_stale"),
        "trade_session": payload.get("trade_session"),
        "data_quality": payload.get("data_quality"),
    }


def _quote_metrics(rows: Sequence[Mapping[str, Any]], expected_count: int) -> dict[str, Any]:
    values = [
        float(row["change_pct"])
        for row in rows
        if row.get("status") == "available" and _safe_float(row.get("change_pct")) is not None
    ]
    available = len(values)
    expected = max(1, int(expected_count))
    return {
        "expected_count": expected_count,
        "available_count": available,
        "verified_timestamp_count": sum(
            1
            for row in rows
            if row.get("status") == "available" and row.get("timestamp_verified") is True
        ),
        "coverage_ratio": round(available / expected, 4),
        "average_change_pct": round(sum(values) / available, 4) if values else None,
        "positive_ratio": round(sum(value > 0 for value in values) / available, 4) if values else None,
        "negative_ratio": round(sum(value < 0 for value in values) / available, 4) if values else None,
    }


def _morning_direction(metrics: Mapping[str, Any]) -> str:
    if float(metrics.get("coverage_ratio") or 0) < 2 / 3:
        return "unavailable"
    average = _safe_float(metrics.get("average_change_pct"))
    positive = float(metrics.get("positive_ratio") or 0)
    negative = float(metrics.get("negative_ratio") or 0)
    if average is not None and average >= 1.0 and positive >= 2 / 3:
        return "strengthening"
    if average is not None and average <= -1.0 and negative >= 2 / 3:
        return "weakening"
    if average is not None and abs(average) < 0.5:
        return "flat"
    return "mixed"


def _close_validation(direction: str, metrics: Mapping[str, Any]) -> str:
    if float(metrics.get("coverage_ratio") or 0) < 0.6:
        return "unavailable"
    average = _safe_float(metrics.get("average_change_pct"))
    positive = float(metrics.get("positive_ratio") or 0)
    negative = float(metrics.get("negative_ratio") or 0)
    if average is None:
        return "unavailable"
    if direction == "strengthening":
        if average >= 0.5 and positive >= 0.6:
            return "confirmed"
        if average <= -0.5 and negative >= 0.6:
            return "falsified"
    elif direction == "weakening":
        if average <= -0.5 and negative >= 0.6:
            return "confirmed_downside"
        if average >= 0.5 and positive >= 0.6:
            return "falsified"
    return "mixed"


def _match_board_rankings(
    theme: ThemeSpec,
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    keywords = [keyword.casefold() for keyword in theme.board_keywords]
    result: dict[str, list[dict[str, Any]]] = {"leaders": [], "laggards": []}
    for direction in ("leaders", "laggards"):
        for item in rankings.get(direction) or []:
            name = str(item.get("name") or "").casefold()
            if not name or not any(keyword in name or name in keyword for keyword in keywords):
                continue
            result[direction].append(dict(item))
    return result


def _board_alignment(direction: str, evidence: Mapping[str, Any]) -> str:
    has_leader = bool(evidence.get("leaders"))
    has_laggard = bool(evidence.get("laggards"))
    if not has_leader and not has_laggard:
        return "unavailable"
    if has_leader and has_laggard:
        return "mixed"
    if direction == "strengthening":
        return "aligned" if has_leader else "contradictory"
    if direction == "weakening":
        return "aligned" if has_laggard else "contradictory"
    return "mixed"


def _combine_validation(watchlist_validation: str, board_alignment: str) -> str:
    if board_alignment == "unavailable":
        return watchlist_validation
    if board_alignment == "mixed":
        return "mixed" if watchlist_validation != "unavailable" else "unavailable"
    if board_alignment == "contradictory":
        if watchlist_validation in {"confirmed", "confirmed_downside"}:
            return "mixed"
        return watchlist_validation
    if board_alignment == "aligned" and watchlist_validation == "falsified":
        return "mixed"
    return watchlist_validation


def _strip_report_text(
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        name: {key: value for key, value in item.items() if key != "text"}
        for name, item in reports.items()
    }


def _string_tuple(value: Any, *, preserve_case: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = []
    for raw in value:
        item = str(raw or "").strip()
        if not preserve_case:
            item = item.upper()
        if item and item not in items:
            items.append(item)
    return tuple(items)


def _stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip().upper()
        if value and value not in result:
            result.append(value)
    return result


def _safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result


def _serializable_time(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_SHANGHAI)
    return value.astimezone(_SHANGHAI)


def _pct(value: Any) -> str:
    number = _safe_float(value)
    return "N/A" if number is None else f"{number:+.2f}%"


def _ratio(value: Any) -> str:
    number = _safe_float(value)
    return "N/A" if number is None else f"{number * 100:.0f}%"


def _direction_label(value: Any) -> str:
    return {
        "strengthening": "强化",
        "weakening": "走弱",
        "flat": "无明显方向",
        "mixed": "分化",
        "unavailable": "数据不足",
    }.get(str(value), "数据不足")


def _validation_label(value: Any) -> str:
    return {
        "confirmed": "已验证",
        "confirmed_downside": "下行已验证",
        "falsified": "已证伪",
        "mixed": "分化 / 待观察",
        "unavailable": "数据不足",
    }.get(str(value), "数据不足")


def _board_alignment_label(value: Any) -> str:
    return {
        "aligned": "同向",
        "contradictory": "反向",
        "mixed": "分化",
        "unavailable": "未命中榜单",
    }.get(str(value), "未命中榜单")


def _escape_link_text(value: str) -> str:
    return str(value).replace("[", "［").replace("]", "］")


def _report_path_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "无文件"
    parts = Path(text).parts
    return "/".join(parts[-3:])


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
