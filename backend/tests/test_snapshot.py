from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.snapshot import _session_for_event, build_snapshot

from .helpers import candidate


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LedgerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append(self, action: str, event_id: str, occurred_at: str, **fields: str) -> None:
        portfolio = "market" if action in {"QUOTE", "BENCHMARK_CLOSE"} else "paper"
        self.store.append(
            candidate(
                action,
                portfolio=portfolio,
                event_id=event_id,
                occurred_at=occurred_at,
                **fields,
            )
        )

    def test_missing_quote_breaks_global_return_chain(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        for day, close in (
            ("2024-01-02", "470"),
            ("2024-01-03", "471"),
            ("2024-01-04", "472"),
            ("2024-01-05", "473"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=close,
                session_date=day,
            )
        for day, close in (
            ("2024-01-02", "100"),
            ("2024-01-04", "105"),
            ("2024-01-05", "106"),
        ):
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=close,
                session_date=day,
            )

        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        self.assertEqual(daily[0]["segment_id"], 1)
        self.assertEqual(daily[1]["data_status"], "INSUFFICIENT_MARKET_DATA")
        self.assertIsNone(daily[1]["nav"])
        self.assertEqual(daily[2]["segment_id"], 2)
        self.assertIsNone(daily[2]["daily_return"])
        self.assertIsNone(daily[2]["cumulative_return"])
        self.assertEqual(daily[2]["segment_return"], "0")
        self.assertIsNotNone(daily[3]["daily_return"])
        self.assertIsNone(daily[3]["cumulative_return"])
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["data_status"],
            "INSUFFICIENT_DATA",
        )

    def test_successful_snapshot_clears_rebuild_marker(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        marker = self.root / "state" / "rebuild.pending"
        self.assertTrue(marker.exists())
        snapshot = build_snapshot(self.root)
        self.assertFalse(marker.exists())
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["data_status"],
            "INSUFFICIENT_DATA",
        )

    def test_after_hours_trade_maps_to_next_nyse_session(self) -> None:
        sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
        event = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-after-hours",
            occurred_at="2024-01-02T22:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
        )
        self.assertEqual(_session_for_event(event, sessions), "2024-01-03")

    def test_summer_dst_session_boundary_uses_new_york_time(self) -> None:
        sessions = ["2024-07-01", "2024-07-02"]
        before_close = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-before-close",
            occurred_at="2024-07-01T19:30:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
        )
        after_close = dict(
            before_close,
            event_id="paper-after-close",
            occurred_at="2024-07-01T20:30:00Z",
        )
        self.assertEqual(_session_for_event(before_close, sessions), "2024-07-01")
        self.assertEqual(_session_for_event(after_close, sessions), "2024-07-02")


if __name__ == "__main__":
    unittest.main()
