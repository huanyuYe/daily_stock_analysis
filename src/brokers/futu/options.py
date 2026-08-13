# -*- coding: utf-8 -*-
"""Read-only US option-chain snapshots from an existing Futu OpenD."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from src.brokers.futu.portfolio import (
    FutuPortfolioError,
    _connection_settings,
    _safe_close,
)


_SNAPSHOT_BATCH_SIZE = 200


class FutuOptionsError(RuntimeError):
    """Raised when an option-chain snapshot cannot be read safely."""


def _date_text(value: Any) -> str:
    return str(value or "").strip()[:10]


def _as_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(_date_text(value))
    except ValueError:
        return None


def _row_records(frame: Any, operation: str) -> List[Dict[str, Any]]:
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise FutuOptionsError(f"{operation} returned non-tabular data")
    return [dict(row) for row in to_dict("records")]


def fetch_futu_option_snapshot(
    symbol: str,
    *,
    start: date,
    end: date,
    preferred_expiry: Optional[date] = None,
) -> Dict[str, Any]:
    """Return one expiry's full contract snapshots without trade operations.

    Futu limits one option-chain date query to roughly one month, so callers
    pass an already bounded interval.  The function requests snapshots only
    for the selected expiry and batches them to avoid SDK list-size limits.
    """

    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise FutuOptionsError("empty US option symbol")
    try:
        from futu import OpenQuoteContext, RET_OK
    except Exception as exc:  # noqa: BLE001 - optional SDK boundary
        raise FutuOptionsError("Futu OpenAPI SDK is unavailable") from exc

    try:
        host, port = _connection_settings()
    except FutuPortfolioError as exc:
        raise FutuOptionsError(str(exc)) from exc

    context = None
    try:
        context = OpenQuoteContext(host=host, port=port)
        ret, chain = context.get_option_chain(
            f"US.{normalized}",
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if ret != RET_OK:
            raise FutuOptionsError(f"Futu option-chain query failed: {chain}")
        chain_rows = _row_records(chain, "Futu option-chain query")
        expiry_dates = sorted(
            {
                value
                for value in (_as_date(row.get("strike_time")) for row in chain_rows)
                if value is not None and value >= start
            }
        )
        if not expiry_dates:
            return {"expiry": None, "contracts": []}
        selected_expiry = (
            min(expiry_dates, key=lambda value: (abs((value - preferred_expiry).days), value))
            if preferred_expiry is not None
            else expiry_dates[0]
        )
        expiry = selected_expiry.isoformat()
        codes = [
            str(row.get("code") or "").strip()
            for row in chain_rows
            if _date_text(row.get("strike_time")) == expiry and row.get("code")
        ]
        contracts: List[Dict[str, Any]] = []
        for offset in range(0, len(codes), _SNAPSHOT_BATCH_SIZE):
            ret, snapshot = context.get_market_snapshot(
                codes[offset : offset + _SNAPSHOT_BATCH_SIZE]
            )
            if ret != RET_OK:
                raise FutuOptionsError(f"Futu option snapshot query failed: {snapshot}")
            contracts.extend(_row_records(snapshot, "Futu option snapshot query"))
        return {"expiry": expiry, "contracts": contracts}
    except FutuOptionsError:
        raise
    except Exception as exc:  # noqa: BLE001 - SDK/network error translation
        raise FutuOptionsError(f"Futu option snapshot query failed: {exc}") from exc
    finally:
        _safe_close(context)
