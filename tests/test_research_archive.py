from __future__ import annotations

import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from trading_bot.research_archive import archive_research, bars_csv, fx_csv, immutable_write, _tracked_fetch


XML = b'''<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>
 <Cube time="2026-10-21"><Cube currency="USD" rate="1.12"/></Cube>
 <Cube time="2026-09-15"><Cube currency="USD" rate="1.11"/></Cube>
 </Cube></gesmes:Envelope>'''


def bar(day="2026-10-21", **overrides):
    return {"t": day + "T04:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000, **overrides}


class FakeClient:
    def __init__(self, kind="bars", error=None):
        self.kind = kind
        self.error = error
        self.requests = []

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return {self.kind: {"AAPL": [bar()]} if self.kind == "bars" else {
            "cash_dividends": [{"symbol": "AAPL", "rate": 1, "ex_date": "2026-10-21"}]},
            "next_page_token": None}

    def get_stock_bars(self, request):
        self.requests.append(request)
        return self.get()["bars"]

    def get_corporate_actions(self, request):
        self.requests.append(request)
        return self.get()["corporate_actions"]


def fx_session():
    response = Mock()
    response.status_code = 200
    response.iter_content.return_value = [XML]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return Mock(get=Mock(return_value=response))


class ResearchArchiveTests(unittest.TestCase):
    def test_immutable_write_reuses_exact_bytes_and_refuses_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            digest = immutable_write(path, b"original")
            self.assertEqual(digest, hashlib.sha256(b"original").hexdigest())
            immutable_write(path, b"original")
            with self.assertRaises(RuntimeError):
                immutable_write(path, b"changed")
            self.assertEqual(path.read_bytes(), b"original")

    def test_bar_dates_are_market_dates_and_csv_is_provider_compatible(self):
        contents, metadata = bars_csv({"AAPL": [bar()]}, ["AAPL"], date(2026, 10, 21), date(2026, 10, 21))
        rows = list(csv.DictReader(io.StringIO(contents.decode())))
        self.assertEqual(rows[0]["date"], "2026-10-21")
        self.assertEqual(metadata["row_count"], 1)
        self.assertFalse(metadata["exchange_calendar_verified"])

    def test_missing_symbol_cannot_be_called_complete(self):
        with self.assertRaises(ValueError):
            bars_csv({"AAPL": [bar()]}, ["AAPL", "SPY"], date(2026, 10, 1), date(2026, 10, 21))

    def test_symbol_specific_missing_dates_are_flagged(self):
        _, metadata = bars_csv({"AAPL": [bar("2026-10-20"), bar()], "SPY": [bar()]},
                              ["AAPL", "SPY"], date(2026, 10, 1), date(2026, 10, 21))
        self.assertFalse(metadata["union_coverage_complete"])
        self.assertEqual(metadata["per_symbol"]["SPY"]["missing_union_dates"], ["2026-10-20"])

    def test_invalid_duplicate_and_outside_bars_rejected(self):
        for rows in [[bar(), bar()], [bar(h=90)], [bar(v=float("nan"))], [bar("2026-10-22")]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                bars_csv({"AAPL": rows}, ["AAPL"], date(2026, 10, 1), date(2026, 10, 21))

    def test_fx_is_dated_sorted_and_does_not_include_future_marks(self):
        contents, metadata = fx_csv(XML, date(2026, 9, 8), date(2026, 10, 20))
        self.assertEqual(metadata["rows"], 1)
        self.assertIn(b"2026-09-15,1.11", contents)
        self.assertNotIn(b"2026-10-21", contents)

    def test_fx_entity_and_duplicate_or_nonfinite_rates_rejected(self):
        for content in [b'<!DOCTYPE x><x/>', XML.replace(b'rate="1.12"', b'rate="NaN"'),
                        XML.replace(b'2026-09-15', b'2026-10-21')]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                fx_csv(content, date(2026, 9, 8), date(2026, 10, 21))

    def test_nonterminal_sdk_result_is_not_complete(self):
        client = FakeClient()
        client.get = Mock(return_value={"bars": {"AAPL": [bar()]}, "next_page_token": "more"})
        with self.assertRaises(RuntimeError):
            _tracked_fetch(client, "get_stock_bars", Mock())

    def test_official_corporate_action_sdk_fetches_beyond_its_default_thousand(self):
        from alpaca.data.historical.corporate_actions import CorporateActionsClient
        from alpaca.data.requests import CorporateActionsRequest
        client = CorporateActionsClient("test-key", "test-secret", raw_data=True)
        client.get = Mock(side_effect=[
            {"corporate_actions": {"cash_dividends": [{"id": str(i)} for i in range(1000)]},
             "next_page_token": "second-page"},
            {"corporate_actions": {"cash_dividends": [{"id": "last"}]}, "next_page_token": None},
        ])
        raw, metadata = _tracked_fetch(client, "get_corporate_actions", CorporateActionsRequest(
            symbols=["AAPL"], start=date(2018, 1, 1), end=date(2026, 10, 21), limit=None))
        self.assertEqual(len(raw["cash_dividends"]), 1001)
        self.assertEqual(metadata["pages"], 2)
        self.assertTrue(metadata["terminal_page_without_next_token"])

    def test_repeated_page_tokens_abort_and_restore_original_get(self):
        client = FakeClient()
        original = Mock(return_value={"bars": {}, "next_page_token": "repeated"})
        client.get = original
        def fetch(request):
            client.get()
            client.get()
        client.get_stock_bars = fetch
        with self.assertRaises(ValueError):
            _tracked_fetch(client, "get_stock_bars", Mock())
        self.assertIs(client.get, original)

    def test_archive_reuse_checks_every_hash_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            stock, actions, session = FakeClient(), FakeClient("corporate_actions"), fx_session()
            result = archive_research(Path(directory), symbols=["AAPL"], stock_client=stock,
                                      actions_client=actions, fx_session=session)
            self.assertTrue(result["collection_succeeded"])
            self.assertEqual([str(request.feed.value) for request in stock.requests], ["sip"] * 3)
            self.assertEqual([request.adjustment.value for request in stock.requests], ["raw", "split", "all"])
            self.assertIsNone(actions.requests[0].limit)
            self.assertGreater(actions.requests[0].end.date() if hasattr(actions.requests[0].end, 'date')
                               else actions.requests[0].end, date(2026, 10, 21))
            self.assertTrue(result["corporate_actions"]["pagination"]["terminal_page_without_next_token"])
            session.get.assert_called_once()
            self.assertTrue(session.get.call_args.kwargs["verify"])
            second = archive_research(Path(directory), symbols=["AAPL"], stock_client=Mock(),
                                      actions_client=Mock(), fx_session=Mock())
            self.assertEqual(result, second)
            (Path(directory) / "bars_raw.csv").write_bytes(b"tampered")
            with self.assertRaises(RuntimeError):
                archive_research(Path(directory), symbols=["AAPL"], stock_client=Mock(),
                                 actions_client=Mock(), fx_session=Mock())

    def test_forbidden_archive_retains_errors_without_secret_or_feed_fallback(self):
        class Forbidden(Exception):
            status_code = 403
        with tempfile.TemporaryDirectory() as directory:
            stock = FakeClient(error=Forbidden("VERYSECRET should never be saved"))
            result = archive_research(Path(directory), symbols=["AAPL"], stock_client=stock,
                actions_client=FakeClient("corporate_actions", error=Forbidden("secret")), fx_session=fx_session())
            self.assertFalse(result["collection_succeeded"])
            self.assertEqual(result["corporate_actions"]["http_status"], 403)
            self.assertFalse(result["corporate_actions"]["complete"])
            self.assertEqual(len(stock.requests), 3)
            text = "".join(path.read_text() for path in Path(directory).iterdir())
            self.assertNotIn("VERYSECRET", text)
            self.assertNotIn('"iex"', text)

    def test_archive_refuses_different_request_in_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_research(Path(directory), symbols=["AAPL"], stock_client=FakeClient(),
                             actions_client=FakeClient("corporate_actions"), fx_session=fx_session())
            with self.assertRaises(RuntimeError):
                archive_research(Path(directory), symbols=["SPY"], stock_client=Mock(),
                                 actions_client=Mock(), fx_session=Mock())


if __name__ == "__main__":
    unittest.main()
