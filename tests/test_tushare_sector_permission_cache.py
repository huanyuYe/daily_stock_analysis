from unittest.mock import patch

from data_provider.tushare_fetcher import TushareFetcher


def _make_fetcher() -> TushareFetcher:
    with patch.object(TushareFetcher, "_init_api", return_value=None), patch.object(
        TushareFetcher, "_determine_priority", return_value=2
    ):
        return TushareFetcher()


def test_sector_permission_denials_are_probed_once_per_process_instance() -> None:
    fetcher = _make_fetcher()
    calls: list[str] = []

    def denied(api_name: str, **_kwargs):
        calls.append(api_name)
        raise RuntimeError(f"抱歉，您没有接口({api_name})访问权限")

    with patch.object(fetcher, "get_trade_time", return_value="20260812"), patch.object(
        fetcher, "_call_api_with_rate_limit", side_effect=denied
    ):
        assert fetcher.get_sector_rankings() is None
        assert fetcher.get_sector_rankings() is None

    assert calls == ["moneyflow_ind_ths", "moneyflow_ind_dc"]
