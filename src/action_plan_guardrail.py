# -*- coding: utf-8 -*-
"""Reconcile the public action with position and price-plan semantics."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

from src.report_language import normalize_report_language
from src.schemas.decision_action import localize_action_label, normalize_decision_action


_NEUTRAL_ACTIONS = {"hold", "watch", "avoid", "alert"}
_EXIT_ACTIONS = {"reduce", "sell"}


def reconcile_action_plan_with_final_action(
    result: Any,
    *,
    portfolio_context: Optional[Mapping[str, Any]] = None,
    report_language: str = "zh",
) -> list[str]:
    """Make action, holding state, and ``battle_plan`` describe one decision.

    Generic scheduled reports do not know whether the reader owns the stock.
    Their neutral result must be ``watch`` rather than ``hold``. Price levels
    may remain as conditional references, but the position plan must not keep
    recommending an entry after an upstream buy was downgraded.
    """

    if result is None:
        return []

    action = normalize_decision_action(getattr(result, "action", None))
    if action is None:
        action = normalize_decision_action(getattr(result, "operation_advice", None))
    if action is None:
        return []

    language = normalize_report_language(
        report_language or getattr(result, "report_language", "zh")
    )
    holding_state = _holding_state(portfolio_context)
    adjustments: list[str] = []

    if action == "hold" and holding_state != "holding":
        action = "watch"
        _set_public_action(result, action=action, language=language)
        adjustments.append("hold_without_position_changed_to_watch")

    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard

    plan_mode = _plan_mode(action)
    battle = dashboard.get("battle_plan")
    if not isinstance(battle, dict):
        battle = {}
        dashboard["battle_plan"] = battle

    previous_mode = str(battle.get("plan_mode") or "").strip()
    battle["plan_mode"] = plan_mode
    if previous_mode != plan_mode:
        adjustments.append("battle_plan_mode_aligned")

    if action in _NEUTRAL_ACTIONS:
        if _reconcile_neutral_position_strategy(
            dashboard,
            battle,
            action=action,
            language=language,
        ):
            adjustments.append("neutral_position_strategy_aligned")
    elif action in _EXIT_ACTIONS:
        if _reconcile_exit_plan(battle, action=action, language=language):
            adjustments.append("exit_plan_removed_entry_advice")

    _sync_decision_metadata(
        result,
        dashboard,
        action=action,
        language=language,
        holding_state=holding_state,
        plan_mode=plan_mode,
        adjustments=adjustments,
    )
    return adjustments


def _holding_state(portfolio_context: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(portfolio_context, Mapping):
        return "unknown"
    quantity = portfolio_context.get("quantity")
    if quantity in (None, ""):
        return "unknown"
    try:
        numeric_quantity = float(quantity)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(numeric_quantity):
        return "unknown"
    return "holding" if abs(numeric_quantity) > 0 else "empty"


def _plan_mode(action: str) -> str:
    if action in {"buy", "add"}:
        return "entry"
    if action == "hold":
        return "holding_risk"
    if action in _EXIT_ACTIONS:
        return "exit_only"
    return "conditional_watch"


def _set_public_action(result: Any, *, action: str, language: str) -> None:
    label = localize_action_label(action, language) or action
    result.action = action
    result.action_label = label
    result.operation_advice = label
    result.decision_type = "hold"

    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard
    dashboard["action"] = action
    dashboard["action_label"] = label
    dashboard["operation_advice"] = label
    dashboard["decision_type"] = "hold"

    calibration = dashboard.get("decision_score_calibration")
    if isinstance(calibration, dict):
        calibration["final_action"] = action
    stability = dashboard.get("decision_stability")
    if isinstance(stability, dict):
        stability["final_action"] = action

    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    reason = _decision_reason(dashboard)
    core["signal_type"] = _neutral_signal_type(language)
    if reason:
        separator = ": " if language == "en" else "："
        core["one_sentence"] = f"{label}{separator}{reason}"
    else:
        core["one_sentence"] = label


def _decision_reason(dashboard: Mapping[str, Any]) -> str:
    stability = dashboard.get("decision_stability")
    if isinstance(stability, Mapping):
        reason = str(stability.get("reason") or "").strip()
        if reason:
            return reason
    calibration = dashboard.get("decision_score_calibration")
    if isinstance(calibration, Mapping):
        return str(calibration.get("guardrail_reason") or "").strip()
    return ""


def _neutral_signal_type(language: str) -> str:
    if language == "en":
        return "🟡 Hold / Watch"
    if language == "ko":
        return "🟡 보유 / 관망"
    return "🟡持有观望"


def _reconcile_neutral_position_strategy(
    dashboard: Mapping[str, Any],
    battle: dict[str, Any],
    *,
    action: str,
    language: str,
) -> bool:
    position = battle.get("position_strategy")
    if not isinstance(position, dict):
        position = {}
        battle["position_strategy"] = position

    core = dashboard.get("core_conclusion")
    core = core if isinstance(core, Mapping) else {}
    position_advice = core.get("position_advice")
    position_advice = position_advice if isinstance(position_advice, Mapping) else {}
    no_position = str(position_advice.get("no_position") or "").strip()
    has_position = str(position_advice.get("has_position") or "").strip()

    if language == "en":
        suggested = (
            "Manage existing holdings only; do not open a position from a hold label."
            if action == "hold"
            else "Keep zero exposure until the stated confirmation conditions are met."
        )
        fallback_entry = "Do not open or add until the watch conditions are confirmed."
        fallback_risk = "Existing holdings should follow the stated risk-control level."
    elif language == "ko":
        suggested = (
            "기존 보유분만 관리하고 보유 의견만으로 신규 진입하지 마세요."
            if action == "hold"
            else "명시된 확인 조건이 충족될 때까지 신규 비중은 0으로 유지하세요."
        )
        fallback_entry = (
            "관찰 조건이 확인되기 전에는 신규 진입하거나 비중을 늘리지 마세요."
        )
        fallback_risk = "기존 보유분은 명시된 위험 관리선을 따르세요."
    else:
        suggested = (
            "仅管理已有仓位；空仓不得依据持有结论新开仓"
            if action == "hold"
            else "空仓保持0成，等待明确触发条件后再重新评估"
        )
        fallback_entry = "观察条件确认前不新开仓、不加仓。"
        fallback_risk = "已有仓位按报告中的风控线管理。"

    replacement = {
        "suggested_position": suggested,
        "entry_plan": no_position or fallback_entry,
        "risk_control": str(position.get("risk_control") or has_position or fallback_risk).strip(),
    }
    changed = any(position.get(key) != value for key, value in replacement.items())
    position.update(replacement)
    return changed


def _reconcile_exit_plan(
    battle: dict[str, Any],
    *,
    action: str,
    language: str,
) -> bool:
    changed = False
    sniper = battle.get("sniper_points")
    if isinstance(sniper, dict):
        for key in ("ideal_buy", "secondary_buy"):
            if key in sniper:
                sniper.pop(key, None)
                changed = True

    position = battle.get("position_strategy")
    if not isinstance(position, dict):
        position = {}
        battle["position_strategy"] = position
        changed = True
    if language == "en":
        suggested = "Exit existing holdings" if action == "sell" else "Reduce existing holdings"
        entry_plan = "Do not open or add to the position."
    elif language == "ko":
        suggested = "기존 보유분 청산" if action == "sell" else "기존 보유 비중 축소"
        entry_plan = "신규 진입하거나 비중을 늘리지 마세요."
    else:
        suggested = (
            "仅处理已有仓位，按计划退出"
            if action == "sell"
            else "仅处理已有仓位，按计划减仓"
        )
        entry_plan = "不新开仓、不加仓。"
    replacement = {
        "suggested_position": suggested,
        "entry_plan": entry_plan,
    }
    if any(position.get(key) != value for key, value in replacement.items()):
        position.update(replacement)
        changed = True
    return changed


def _sync_decision_metadata(
    result: Any,
    dashboard: dict[str, Any],
    *,
    action: str,
    language: str,
    holding_state: str,
    plan_mode: str,
    adjustments: list[str],
) -> None:
    label = localize_action_label(action, language) or action
    result.action = action
    result.action_label = label
    dashboard["action"] = action
    dashboard["action_label"] = label
    dashboard["operation_advice"] = getattr(result, "operation_advice", label)
    dashboard["decision_type"] = getattr(result, "decision_type", "hold")
    dashboard["action_plan_reconciliation"] = {
        "applied": bool(adjustments),
        "final_action": action,
        "holding_state": holding_state,
        "plan_mode": plan_mode,
        "adjustments": list(dict.fromkeys(adjustments)),
    }
