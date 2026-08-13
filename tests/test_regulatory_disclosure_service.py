# -*- coding: utf-8 -*-
"""Tests for SEC-A/SEC-B and public HKEXnews regulatory evidence."""

from __future__ import annotations

import unittest
import json
import requests
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from types import SimpleNamespace

from src.services.regulatory_disclosure_service import (
    RegulatoryDisclosureService,
    reset_regulatory_disclosure_cache,
)


class _Response:
    def __init__(self, *, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _HttpErrorResponse(_Response):
    def __init__(self, status_code: int):
        super().__init__(payload={})
        self.status_code = status_code

    def raise_for_status(self):
        response = SimpleNamespace(status_code=self.status_code)
        raise requests.HTTPError(f"HTTP {self.status_code}", response=response)


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        for needle, response in self.responses:
            if needle in url:
                return response
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        for needle, response in self.responses:
            if needle in url:
                return response
        raise AssertionError(f"unexpected POST {url}")


class RegulatoryDisclosureServiceTestCase(unittest.TestCase):
    def setUp(self):
        reset_regulatory_disclosure_cache()
        self.config = SimpleNamespace(
            regulatory_disclosures_enabled=True,
            regulatory_fetch_timeout_sec=4.0,
            sec_edgar_user_agent="unit-test contact=test@example.com",
        )
        self.now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        reset_regulatory_disclosure_cache()

    def test_sec_a_and_sec_b_are_normalized_with_official_provenance(self):
        mapping = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
        }
        submissions = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-26-000001"],
                    "filingDate": ["2026-08-01"],
                    "reportDate": ["2026-06-30"],
                    "acceptanceDateTime": ["2026-08-01T12:30:00Z"],
                    "form": ["10-Q"],
                    "primaryDocument": ["aapl-20260630.htm"],
                    "primaryDocDescription": ["Quarterly report"],
                }
            }
        }
        companyfacts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "label": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    "val": 100,
                                    "start": "2026-04-01",
                                    "end": "2026-06-30",
                                    "filed": "2026-08-01",
                                    "form": "10-Q",
                                    "accn": "0000320193-26-000001",
                                    "fy": 2026,
                                    "fp": "Q3",
                                }
                            ]
                        },
                    }
                }
            }
        }
        session = _Session([
            ("company_tickers_exchange", _Response(payload=mapping)),
            ("submissions/CIK", _Response(payload=submissions)),
            ("companyfacts/CIK", _Response(payload=companyfacts)),
        ])
        bundle = RegulatoryDisclosureService(
            config=self.config,
            session=session,
            now_provider=lambda: self.now,
        ).fetch("AAPL", "Apple")

        self.assertEqual(bundle.status, "available")
        self.assertEqual(bundle.source_status["sec_submissions"], "success")
        self.assertEqual(bundle.source_status["sec_companyfacts"], "success")
        self.assertEqual(bundle.filings[0].form_type, "10-Q")
        self.assertEqual(bundle.filings[0].verification_status, "official_primary")
        self.assertIn("000032019326000001", bundle.filings[0].url)
        self.assertEqual(bundle.company_facts[0].metric, "revenue")
        self.assertEqual(bundle.company_facts[0].filed_at.isoformat(), "2026-08-01")
        self.assertIn("SEC 结构化财务事实", bundle.to_prompt_context())
        self.assertEqual(
            next(call for call in session.calls if "data.sec.gov" in call[1])[2]["headers"]["User-Agent"],
            "unit-test contact=test@example.com",
        )

    def test_sec_mapping_http_403_is_not_repeated_for_every_symbol(self):
        session = _Session([
            ("company_tickers_exchange", _HttpErrorResponse(403)),
        ])
        service = RegulatoryDisclosureService(
            config=self.config,
            session=session,
            now_provider=lambda: self.now,
        )

        first = service.fetch("AAPL", "Apple")
        second = service.fetch("MSFT", "Microsoft")

        mapping_calls = [
            call for call in session.calls if "company_tickers_exchange" in call[1]
        ]
        self.assertEqual(len(mapping_calls), 1)
        self.assertEqual(first.source_status["sec_ticker_mapping"], "failed")
        self.assertEqual(second.source_status["sec_ticker_mapping"], "failed")

    def test_sec_companyfacts_reject_future_filing_and_uses_latest_known(self):
        payload = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "label": "Assets",
                        "units": {
                            "USD": [
                                {"val": 1, "end": "2026-03-31", "filed": "2026-05-01", "form": "10-Q", "accn": "old"},
                                {"val": 2, "end": "2026-06-30", "filed": "2026-08-01", "form": "10-Q", "accn": "known"},
                                {"val": 3, "end": "2026-09-30", "filed": "2026-12-01", "form": "10-Q", "accn": "future"},
                            ]
                        },
                    }
                }
            }
        }
        facts = RegulatoryDisclosureService._parse_sec_companyfacts(
            payload,
            "0000320193",
            self.now,
        )
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].value, 2)
        self.assertEqual(facts[0].accession_no, "known")

    def test_public_hkex_title_search_is_normalized(self):
        prefix = _Response(
            text='callback({"stockInfo":[{"stockId":"7609","code":"00700","name":"TENCENT"}]})'
        )
        html = """
        <table><tr><th>Release Time</th><th>Stock</th><th>Title</th></tr>
        <tr><td>04/08/2026 17:30</td><td>00700 TENCENT</td>
        <td><div class="doc-link"><a href="/listedco/listconews/sehk/2026/0804/2026080400123.pdf">
        Monthly Return</a></div></td></tr></table>
        """
        session = _Session([
            ("prefix.do", prefix),
            ("titlesearch.xhtml", _Response(text=html)),
        ])
        bundle = RegulatoryDisclosureService(
            config=self.config,
            session=session,
            now_provider=lambda: self.now.replace(hour=10),
        ).fetch("hk00700", "腾讯")

        self.assertEqual(bundle.status, "available")
        self.assertEqual(bundle.source_status["hkexnews"], "success")
        self.assertEqual(bundle.filings[0].source_id, "hkexnews_public_title_search")
        self.assertEqual(bundle.filings[0].issuer_id, "7609")
        self.assertTrue(bundle.filings[0].url.endswith("2026080400123.pdf"))
        post = next(call for call in session.calls if call[0] == "POST")
        self.assertEqual(post[2]["data"]["stockId"], "7609")

    def test_unsupported_market_does_not_call_network(self):
        session = _Session([])
        bundle = RegulatoryDisclosureService(
            config=self.config,
            session=session,
            now_provider=lambda: self.now,
        ).fetch("600519", "贵州茅台")
        self.assertEqual(bundle.status, "unsupported")
        self.assertEqual(session.calls, [])

    def test_sec_uses_marked_last_good_payload_when_fresh_request_fails(self):
        mapping = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
        }
        submissions = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-26-000001"],
                    "filingDate": ["2026-08-01"],
                    "reportDate": ["2026-06-30"],
                    "acceptanceDateTime": ["2026-08-01T12:30:00Z"],
                    "form": ["10-Q"],
                    "primaryDocument": ["aapl.htm"],
                    "primaryDocDescription": ["Quarterly report"],
                }
            }
        }
        companyfacts = {"facts": {}}
        session = _Session([
            ("company_tickers_exchange", _Response(payload=mapping)),
            ("submissions/CIK", _Response(payload=submissions)),
            ("companyfacts/CIK", _Response(payload=companyfacts)),
        ])

        with TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first = RegulatoryDisclosureService(
                config=self.config,
                session=session,
                now_provider=lambda: self.now,
                persistent_cache_dir=cache_dir,
            ).fetch("AAPL", "Apple")
            self.assertEqual(first.status, "available")

            for cache_file in (cache_dir / "regulatory").glob("*.json"):
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                payload["stored_at"] = time.time() - 7 * 60 * 60
                cache_file.write_text(json.dumps(payload), encoding="utf-8")

            reset_regulatory_disclosure_cache()
            degraded = RegulatoryDisclosureService(
                config=self.config,
                session=_Session([]),
                now_provider=lambda: self.now,
                persistent_cache_dir=cache_dir,
            ).fetch("AAPL", "Apple")

        self.assertEqual(degraded.status, "degraded")
        self.assertEqual(len(degraded.filings), 1)
        self.assertEqual(degraded.source_status["sec_submissions"], "stale_last_good")
        self.assertEqual(degraded.source_status["sec_companyfacts"], "stale_last_good")
        self.assertTrue(any(item.startswith("stale_last_good:sec_submissions") for item in degraded.warnings))
        self.assertTrue(any(item.startswith("stale_last_good:sec_companyfacts") for item in degraded.warnings))


if __name__ == "__main__":
    unittest.main()
