# -*- coding: utf-8 -*-
"""Regression tests for final action and battle-plan reconciliation."""

from types import SimpleNamespace

from src.action_plan_guardrail import reconcile_action_plan_with_final_action


def _result(action: str = "hold") -> SimpleNamespace:
    return SimpleNamespace(
        action=action,
        action_label="持有" if action == "hold" else action,
        operation_advice="持有观察" if action == "hold" else action,
        decision_type="hold" if action in {"hold", "watch"} else action,
        report_language="zh",
        dashboard={
            "decision_score_calibration": {
                "raw_score": 66,
                "adjusted_score": 59,
                "final_action": action,
                "guardrail_reason": "资金流数据缺失，买入结论缺少资金面确认。",
            },
            "decision_stability": {
                "applied": True,
                "reason": "资金流数据缺失，买入结论缺少资金面确认。",
                "final_action": action,
            },
            "core_conclusion": {
                "one_sentence": "持有观察",
                "position_advice": {
                    "no_position": "空仓等待资金流恢复后再行动。",
                    "has_position": "已有仓位以支撑位作为风控线。",
                },
            },
            "battle_plan": {
                "sniper_points": {
                    "ideal_buy": "52.51元",
                    "secondary_buy": "51.16元",
                    "stop_loss": "50.20元",
                    "take_profit": "55.00元",
                },
                "position_strategy": {
                    "suggested_position": "建议仓位2成，最高4成",
                    "entry_plan": "先买1成，确认后继续加仓",
                    "risk_control": "跌破50.20元止损",
                },
            },
        },
    )


def test_unknown_holding_converts_hold_to_watch_and_removes_direct_entry_plan() -> None:
    result = _result()

    adjustments = reconcile_action_plan_with_final_action(result)

    assert result.action == "watch"
    assert result.action_label == "观望"
    assert result.operation_advice == "观望"
    assert result.decision_type == "hold"
    assert "hold_without_position_changed_to_watch" in adjustments
    assert result.dashboard["decision_score_calibration"]["final_action"] == "watch"
    assert result.dashboard["decision_stability"]["final_action"] == "watch"
    assert result.dashboard["battle_plan"]["plan_mode"] == "conditional_watch"
    position = result.dashboard["battle_plan"]["position_strategy"]
    assert position["suggested_position"].startswith("空仓保持0成")
    assert position["entry_plan"] == "空仓等待资金流恢复后再行动。"
    assert "先买1成" not in position["entry_plan"]
    assert result.dashboard["battle_plan"]["sniper_points"]["take_profit"] == "55.00元"


def test_known_holding_preserves_hold_but_does_not_recommend_opening() -> None:
    result = _result()

    reconcile_action_plan_with_final_action(
        result,
        portfolio_context={"quantity": 200},
    )

    assert result.action == "hold"
    assert result.operation_advice == "持有观察"
    assert result.dashboard["battle_plan"]["plan_mode"] == "holding_risk"
    position = result.dashboard["battle_plan"]["position_strategy"]
    assert position["suggested_position"].startswith("仅管理已有仓位")
    assert "先买1成" not in position["entry_plan"]


def test_sell_plan_removes_entry_levels_and_blocks_new_positions() -> None:
    result = _result(action="sell")
    result.operation_advice = "卖出"
    result.decision_type = "sell"

    reconcile_action_plan_with_final_action(result)

    assert result.action == "sell"
    assert result.dashboard["battle_plan"]["plan_mode"] == "exit_only"
    sniper = result.dashboard["battle_plan"]["sniper_points"]
    assert "ideal_buy" not in sniper
    assert "secondary_buy" not in sniper
    position = result.dashboard["battle_plan"]["position_strategy"]
    assert position["entry_plan"] == "不新开仓、不加仓。"
    assert position["suggested_position"] == "仅处理已有仓位，按计划退出"
