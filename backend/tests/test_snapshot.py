from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.snapshot import (
    _session_for_event,
    build_snapshot,
    build_snapshot_if_needed,
)

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

    def test_clean_start_builds_valid_empty_snapshot(self) -> None:
        snapshot = build_snapshot(self.root)
        self.assertEqual(snapshot["schema_version"], 3)
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["portfolios"]["paper"]["data_status"], "NO_DATA")
        self.assertEqual(snapshot["portfolios"]["live"]["holdings"], [])
        self.assertEqual(snapshot["benchmark"]["daily"], [])
        self.assertTrue(
            (self.root / "snapshots" / "portfolio-snapshot.json").exists()
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

    def test_rebuild_repairs_incomplete_tail_and_surfaces_warning(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        path = self.store.path_for("paper")
        with path.open("ab") as handle:
            handle.write(b'{"event_id":')

        snapshot = build_snapshot(self.root)

        self.assertTrue(
            any(
                "paper ledger tail repaired" in warning
                for warning in snapshot["warnings"]
            )
        )
        self.assertEqual(len(self.store.read("paper")), 1)
        self.assertEqual(len(list((self.root / "backups").glob("*.bak"))), 1)
        self.assertEqual(
            len(list((self.root / "quarantine").glob("*.quarantine"))),
            1,
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
            "OK",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["total_return"],
            "0",
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

    def test_weekend_forward_fills_quotes_without_bridging_trading_days(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-05T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-05T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        for day, aapl, spy in (
            ("2024-01-05", "100", "470"),
            ("2024-01-08", "110", "475"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=spy,
                session_date=day,
            )
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=aapl,
                session_date=day,
            )

        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        by_day = {point["date"]: point for point in daily}
        self.assertEqual(by_day["2024-01-06"]["nav"], "1000")
        self.assertEqual(by_day["2024-01-07"]["nav"], "1000")
        self.assertEqual(by_day["2024-01-06"]["data_status"], "OK")
        self.assertEqual(by_day["2024-01-08"]["nav"], "1010")
        benchmark = {
            point["date"]: point for point in snapshot["benchmark"]["daily"]
        }
        self.assertEqual(benchmark["2024-01-06"]["close"], "470")
        self.assertEqual(
            snapshot["portfolios"]["paper"]["holdings"][0][
                "market_price_as_of"
            ],
            "2024-01-08T21:00:00Z",
        )

    def test_trade_after_latest_quote_extends_calendar_and_creates_gap(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-aapl",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )
        self.append(
            "QUOTE",
            "market-aapl-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-02",
        )
        self.append(
            "BUY",
            "paper-buy-msft",
            "2024-01-03T15:00:00Z",
            symbol="MSFT",
            shares="1",
            price="100",
            fee="0",
        )

        snapshot = build_snapshot(self.root, write=False)
        by_day = {
            point["date"]: point
            for point in snapshot["portfolios"]["paper"]["daily"]
        }
        self.assertIn("2024-01-03", by_day)
        self.assertEqual(
            by_day["2024-01-03"]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )
        self.assertCountEqual(
            by_day["2024-01-03"]["missing_symbols"],
            ["AAPL", "MSFT"],
        )

    def test_if_needed_rebuild_skips_unchanged_source_heads(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        first = build_snapshot(self.root)
        original_bytes = (
            self.root / "snapshots" / "portfolio-snapshot.json"
        ).read_bytes()
        current, rebuilt = build_snapshot_if_needed(self.root)
        self.assertFalse(rebuilt)
        self.assertEqual(current["generated_at"], first["generated_at"])
        self.assertEqual(
            (self.root / "snapshots" / "portfolio-snapshot.json").read_bytes(),
            original_bytes,
        )

        self.append(
            "CASH_FLOW",
            "paper-cash",
            "2024-01-02T14:00:00Z",
            amount="10",
        )
        updated, rebuilt = build_snapshot_if_needed(self.root)
        self.assertTrue(rebuilt)
        self.assertEqual(updated["revision"], first["revision"] + 1)
        self.assertFalse((self.root / "state" / "rebuild.pending").exists())

    def test_nav_decomposes_exactly_into_cash_and_market_value(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="2",
            price="100",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )
        self.append(
            "QUOTE",
            "market-aapl-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="110",
            session_date="2024-01-02",
        )
        snapshot = build_snapshot(self.root, write=False)
        point = snapshot["portfolios"]["paper"]["daily"][0]
        holding = snapshot["portfolios"]["paper"]["holdings"][0]
        self.assertEqual(point["cash"], "800")
        self.assertEqual(holding["market_value"], "220")
        self.assertEqual(point["nav"], "1020")

    def test_start_of_day_cash_flow_is_removed_from_daily_return(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "CASH_FLOW",
            "paper-deposit",
            "2024-01-03T14:00:00Z",
            amount="100",
        )
        for day, close in (("2024-01-02", "470"), ("2024-01-03", "471")):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=close,
                session_date=day,
            )
        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        self.assertEqual(daily[1]["external_flow"], "100")
        self.assertEqual(daily[1]["daily_return"], "0")
        self.assertEqual(daily[1]["nav"], "1100")

    def test_max_drawdown_uses_standard_peak_to_trough_ratio(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
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
        for day, aapl, spy in (
            ("2024-01-02", "100", "470"),
            ("2024-01-03", "120", "471"),
            ("2024-01-04", "90", "472"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=spy,
                session_date=day,
            )
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=aapl,
                session_date=day,
            )
        snapshot = build_snapshot(self.root, write=False)
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["max_drawdown"],
            "-0.02941176",
        )


if __name__ == "__main__":
    unittest.main()
