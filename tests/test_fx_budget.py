from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from trading_bot.fx_budget import (
    ECB_DAILY_URL, FxQuote, MAX_RESPONSE_BYTES, _fetch_ecb_quote, _parse_ecb_xml,
    load_cached_reference_for_risk_only, load_ecb_reference_quote,
)


TODAY = datetime.now(ZoneInfo("Europe/Vilnius")).date()
NOW = datetime.now(timezone.utc)


def xml(day="2026-10-21", rate="1.1234", extra=""):
    return (f'<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
            f'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
            f'<Cube><Cube time="{day}"><Cube currency="USD" rate="{rate}"/>{extra}'
            f'</Cube></Cube></gesmes:Envelope>').encode()


def response(body, status=200):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.iter_content.return_value = [body]
    return result


class FxBudgetTests(unittest.TestCase):
    def test_conversion_direction_and_cent_rounding(self):
        quote = FxQuote("1.1234", date(2026, 10, 21))
        self.assertEqual(quote.eur_to_usd(5000), Decimal("5617.00"))
        self.assertEqual(quote.eur_to_usd("10.001"), Decimal("11.23"))
        self.assertEqual(quote.usd_to_eur("1123.40"), Decimal("1000"))
        self.assertEqual(quote.usd_to_eur("-1123.40"), Decimal("-1000"))
        self.assertEqual(quote.eur_to_usd(5000, buffer_bps=50), Decimal("5588.91"))

    def test_invalid_rates_amounts_and_buffers_are_rejected(self):
        for rate in (0, -1, True, "NaN", "Infinity", "not-a-rate", None):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                FxQuote(rate, TODAY)
        quote = FxQuote("1.1234", TODAY)
        for amount in (-1, True, "NaN", float("inf"), None):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                quote.eur_to_usd(amount)
        for buffer in (-1, 10_000, True, "NaN"):
            with self.subTest(buffer=buffer), self.assertRaises(ValueError):
                quote.eur_to_usd(100, buffer_bps=buffer)

    def test_weekend_and_holiday_age_is_bounded_at_four_calendar_days(self):
        quote = FxQuote("1.1234", date(2026, 10, 16))
        quote.validate(today=date(2026, 10, 20))
        with self.assertRaisesRegex(ValueError, "stale"):
            quote.validate(today=date(2026, 10, 21))
        with self.assertRaisesRegex(ValueError, "future"):
            quote.validate(today=date(2026, 10, 15))
        with self.assertRaises(ValueError):
            quote.validate(today=date(2026, 10, 21), max_age_days=5)

    def test_source_and_timestamps_cannot_reverse_or_future_date_the_reference(self):
        with self.assertRaisesRegex(ValueError, "pinned"):
            FxQuote("1.1234", TODAY, source="https://example.com/rates")
        with self.assertRaisesRegex(ValueError, "timezone"):
            FxQuote("1.1234", TODAY, fetched_at=datetime(2026, 10, 22))
        with self.assertRaisesRegex(ValueError, "future"):
            FxQuote("1.1234", TODAY, fetched_at=NOW + timedelta(days=1)).validate(today=TODAY)
        with self.assertRaisesRegex(ValueError, "precedes"):
            FxQuote("1.1234", TODAY, fetched_at=NOW - timedelta(days=2)).validate(today=TODAY)

    def test_json_roundtrip_preserves_explicit_rate_date_and_source(self):
        quote = FxQuote("1.123400", TODAY, fetched_at=NOW)
        restored = FxQuote.from_dict(json.loads(json.dumps(quote.to_dict())), today=TODAY)
        self.assertEqual(restored, quote)
        self.assertEqual(restored.to_dict()["usd_per_eur"], "1.123400")
        for changes in (dict(version=True), dict(base_currency="USD"), dict(quote_currency="EUR"),
                        dict(as_of="not-a-date"), dict(fetched_at="not-a-timestamp"),
                        dict(usd_per_eur=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                FxQuote.from_dict({**quote.to_dict(), **changes}, today=TODAY)

    def test_parser_requires_one_dated_usd_reference_in_ecb_namespace(self):
        quote = _parse_ecb_xml(xml(), fetched_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                               today=date(2026, 10, 22))
        self.assertEqual(quote.usd_per_eur, Decimal("1.1234"))
        self.assertEqual(quote.as_of, date(2026, 10, 21))
        for body in (b"not XML", b"<Envelope/>", xml(extra='<Cube currency="USD" rate="1.1"/>'),
                     xml().replace(b'currency="USD"', b'currency="GBP"'),
                     xml().replace(b'http://www.ecb.int/', b'http://example.com/'),
                     xml(rate="NaN"), xml(day="2026-10-23"), xml(day="2026-10-16"),
                     b"<!DOCTYPE Envelope>" + xml(), b"x" * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(body=body[:60]), self.assertRaises(ValueError):
                _parse_ecb_xml(body, fetched_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                               today=date(2026, 10, 22))

    def test_http_request_is_pinned_verified_bounded_and_does_not_follow_redirects(self):
        with patch("requests.get", return_value=response(xml(TODAY.isoformat()))) as get:
            quote = _fetch_ecb_quote(today=TODAY)
        self.assertEqual(quote.as_of, TODAY)
        self.assertEqual(get.call_args.args, (ECB_DAILY_URL,))
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 15))
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)
        self.assertNotIn("verify", get.call_args.kwargs)  # requests default verifies TLS.
        self.assertIs(get.call_args.kwargs["stream"], True)
        redirect = response(xml(), status=302)
        with patch("requests.get", return_value=redirect), self.assertRaisesRegex(RuntimeError, "redirects"):
            _fetch_ecb_quote(today=TODAY)
        redirect.iter_content.assert_not_called()

    def test_download_size_and_elapsed_time_are_bounded(self):
        with patch("requests.get", return_value=response(b"x" * (MAX_RESPONSE_BYTES + 1))):
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                _fetch_ecb_quote(today=TODAY)
        with patch("requests.get", return_value=response(xml())), patch(
            "trading_bot.fx_budget.time.monotonic", side_effect=[0, 21],
        ), self.assertRaisesRegex(TimeoutError, "time limit"):
            _fetch_ecb_quote(today=TODAY)

    def test_fresh_cache_avoids_network_and_retains_exact_decimal_rate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fx.json"
            quote = FxQuote("1.123400", TODAY, fetched_at=NOW)
            path.write_text(json.dumps(quote.to_dict()), encoding="utf-8")
            with patch("trading_bot.fx_budget._fetch_ecb_quote") as fetch:
                self.assertEqual(load_ecb_reference_quote(path, today=TODAY), quote)
            fetch.assert_not_called()

    def test_forced_refresh_can_use_fresh_cache_during_an_outage(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fx.json"
            quote = FxQuote("1.1234", TODAY - timedelta(days=1))
            original = json.dumps(quote.to_dict())
            path.write_text(original, encoding="utf-8")
            with patch("trading_bot.fx_budget._fetch_ecb_quote", side_effect=ConnectionError("offline")):
                self.assertEqual(load_ecb_reference_quote(path, today=TODAY, force_refresh=True), quote)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_stale_future_or_corrupt_cache_cannot_authorize_a_budget_on_outage(self):
        for contents in (
            json.dumps(FxQuote("1.1", TODAY - timedelta(days=5)).to_dict()),
            json.dumps(FxQuote("1.1", TODAY + timedelta(days=1)).to_dict()), "not JSON",
        ):
            with self.subTest(contents=contents), TemporaryDirectory() as directory:
                path = Path(directory) / "fx.json"
                path.write_text(contents, encoding="utf-8")
                with patch("trading_bot.fx_budget._fetch_ecb_quote", side_effect=ConnectionError("offline")):
                    with self.assertRaisesRegex(RuntimeError, "No fresh"):
                        load_ecb_reference_quote(path, today=TODAY)
                self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_successful_refresh_atomically_replaces_only_the_reference_cache(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fx.json"
            path.write_text("old invalid cache", encoding="utf-8")
            quote = FxQuote("1.1234", TODAY, fetched_at=NOW)
            with patch("trading_bot.fx_budget._fetch_ecb_quote", return_value=quote):
                self.assertEqual(load_ecb_reference_quote(path, today=TODAY), quote)
            self.assertEqual(FxQuote.from_dict(json.loads(path.read_text()), today=TODAY), quote)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_stale_cache_is_available_only_for_existing_risk_monitoring(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fx.json"
            old = FxQuote("1.1234", TODAY - timedelta(days=30), fetched_at=NOW - timedelta(days=25))
            path.write_text(json.dumps(old.to_dict()), encoding="utf-8")
            with patch("trading_bot.fx_budget._fetch_ecb_quote") as fetch:
                recovered = load_cached_reference_for_risk_only(path, today=TODAY)
            fetch.assert_not_called()
            self.assertEqual(recovered, old)
            with self.assertRaisesRegex(ValueError, "stale"):
                recovered.validate(today=TODAY)

    def test_risk_only_cache_still_rejects_future_invalid_and_ambiguous_quotes(self):
        valid = FxQuote("1.1234", TODAY).to_dict()
        for contents in (
            json.dumps({**valid, "as_of": (TODAY + timedelta(days=1)).isoformat()}),
            json.dumps({**valid, "usd_per_eur": "NaN"}),
            json.dumps({**valid, "source": "https://example.com"}),
            json.dumps({**valid, "fetched_at": (NOW + timedelta(minutes=1)).isoformat()}),
            json.dumps(valid).replace('"usd_per_eur": "1.1234"', '"usd_per_eur":"1.1","usd_per_eur":"1.2"'),
        ):
            with self.subTest(contents=contents), TemporaryDirectory() as directory:
                path = Path(directory) / "fx.json"
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_cached_reference_for_risk_only(path, today=TODAY)

    def test_failed_atomic_cache_write_preserves_previous_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fx.json"
            path.write_text("old invalid cache", encoding="utf-8")
            with patch("trading_bot.fx_budget._fetch_ecb_quote", return_value=FxQuote("1.1", TODAY)), patch(
                "trading_bot.fx_budget.replace_file", side_effect=PermissionError("file locked"),
            ), self.assertRaisesRegex(PermissionError, "file locked"):
                load_ecb_reference_quote(path, today=TODAY)
            self.assertEqual(path.read_text(), "old invalid cache")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
