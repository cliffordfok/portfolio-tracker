from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "market_quotes.py"
)
SPEC = importlib.util.spec_from_file_location("market_quotes_provider", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load scripts/market_quotes.py")

PROVIDER = importlib.util.module_from_spec(SPEC)
ORIGINAL_YFINANCE = sys.modules.get("yfinance")
sys.modules["yfinance"] = types.SimpleNamespace(Ticker=None)
try:
    SPEC.loader.exec_module(PROVIDER)
finally:
    if ORIGINAL_YFINANCE is None:
        del sys.modules["yfinance"]
    else:
        sys.modules["yfinance"] = ORIGINAL_YFINANCE


class MarketQuoteProviderTests(unittest.TestCase):
    def test_session_timestamp_uses_dst_and_early_close(self) -> None:
        self.assertEqual(
            PROVIDER.session_occurred_at("2026-01-02"),
            "2026-01-02T21:00:00Z",
        )
        self.assertEqual(
            PROVIDER.session_occurred_at("2026-08-07"),
            "2026-08-07T20:00:00Z",
        )
        self.assertEqual(
            PROVIDER.session_occurred_at("2026-11-27"),
            "2026-11-27T18:00:00Z",
        )

    def test_batch_retry_reproduces_complete_payload(self) -> None:
        calls: list[tuple[str, bool]] = []

        def fetch_close(
            symbol: str,
            _session_date: str,
            *,
            adjust: bool,
        ) -> float:
            calls.append((symbol, adjust))
            if symbol == "SPY" and adjust:
                return 501.25
            return 502.5

        occurred_at = "2026-08-07T20:00:00Z"
        with (
            patch.object(PROVIDER, "SYMBOLS", ["AAPL", "SPY"]),
            patch.object(PROVIDER, "fetch_close", fetch_close),
            patch("builtins.print"),
        ):
            first = PROVIDER.build_quote_batch("2026-08-07", occurred_at)
            retry = PROVIDER.build_quote_batch("2026-08-07", occurred_at)

        self.assertEqual(first, retry)
        self.assertEqual(
            [event["event_id"] for event in first],
            [
                "market-quote-2026-08-07-aapl",
                "market-quote-2026-08-07-spy",
                "market-benchmark-2026-08-07-spy",
            ],
        )
        self.assertEqual(
            {event["created_at"] for event in first},
            {occurred_at},
        )
        self.assertNotIn("benchmark", first[1])
        self.assertIs(first[2]["benchmark"], True)
        self.assertEqual(first[1]["close"], "502.5")
        self.assertEqual(first[2]["close"], "501.25")
        self.assertEqual(
            calls,
            [
                ("AAPL", False),
                ("SPY", False),
                ("SPY", True),
            ]
            * 2,
        )

    def test_batch_rejects_noncanonical_timestamp(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "official session close"):
            PROVIDER.build_quote_batch(
                "2026-08-07",
                "2026-08-07T20:00:01Z",
            )


if __name__ == "__main__":
    unittest.main()
