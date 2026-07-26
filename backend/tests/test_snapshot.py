from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_tracker.cli import main as cli_main
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.snapshot import (
    _metrics,
    _session_for_event,
    build_snapshot,
    build_snapshot_if_needed,
    is_nyse_session,
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
        self.assertEqual(daily[0]["daily_return"], "0")
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

    def test_voided_event_does_not_extend_the_performance_calendar(self) -> None:
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
            "paper-buy-msft-voided",
            "2024-01-03T15:00:00Z",
            symbol="MSFT",
            shares="1",
            price="100",
            fee="0",
        )
        self.append(
            "VOID",
            "paper-void-msft",
            "2024-01-04T15:00:00Z",
            void_target="paper-buy-msft-voided",
        )

        snapshot = build_snapshot(self.root, write=False)
        self.assertEqual(
            [
                point["date"]
                for point in snapshot["portfolios"]["paper"]["daily"]
            ],
            ["2024-01-02"],
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

    def test_new_year_observed_holiday_is_not_a_session(self) -> None:
        self.assertFalse(is_nyse_session(date(2021, 12, 31)))
        self.assertTrue(is_nyse_session(date(2021, 12, 30)))

    def test_special_exchange_closures_are_not_sessions(self) -> None:
        closures = (
            date(2001, 9, 11),
            date(2001, 9, 12),
            date(2001, 9, 13),
            date(2001, 9, 14),
            date(2004, 6, 11),
            date(2007, 1, 2),
            date(2012, 10, 29),
            date(2012, 10, 30),
            date(2018, 12, 5),
            date(2025, 1, 9),
        )
        for closure in closures:
            with self.subTest(closure=closure):
                self.assertFalse(is_nyse_session(closure))

        self.assertTrue(is_nyse_session(date(2001, 9, 17)))
        self.assertTrue(is_nyse_session(date(2025, 1, 10)))

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

    def test_if_needed_rebuilds_snapshot_with_nested_corruption(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        build_snapshot(self.root)
        path = self.root / "snapshots" / "portfolio-snapshot.json"
        corrupted = json.loads(path.read_text(encoding="utf-8"))
        corrupted["portfolios"]["paper"]["holdings"] = "not-an-array"
        atomic_write_json(path, corrupted)
        marker = self.root / "state" / "rebuild.pending"
        atomic_write_json(marker, {"requested_by": "corruption-test"})

        repaired, rebuilt = build_snapshot_if_needed(self.root)

        self.assertTrue(rebuilt)
        self.assertEqual(repaired["portfolios"]["paper"]["holdings"], [])
        self.assertFalse(marker.exists())
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, repaired)

    def test_cli_append_reuses_a_systemd_preempted_snapshot(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        build_snapshot(self.root)
        cash_flow = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-deposit",
            occurred_at="2024-01-02T15:00:00Z",
            amount="25",
        )

        def systemd_wins(root: Path, *, output: str | None = None):
            return build_snapshot(root, output=output), False

        output = StringIO()
        with (
            patch(
                "portfolio_tracker.cli.build_snapshot_if_needed",
                side_effect=systemd_wins,
            ) as rebuild,
            redirect_stdout(output),
        ):
            exit_code = cli_main(
                [
                    "--root",
                    str(self.root),
                    "append",
                    "--json",
                    json.dumps(cash_flow),
                    "--rebuild",
                ]
            )

        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "appended")
        self.assertFalse(result["snapshot_rebuilt"])
        self.assertEqual(result["snapshot_status"], "current")
        self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(result["event"]["event_id"], "paper-deposit")
        self.assertTrue((self.root / "state" / "publish.pending").exists())

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

    def test_spy_benchmark_close_is_not_a_holding_quote(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-spy",
            "2024-01-02T15:00:00Z",
            symbol="SPY",
            shares="1",
            price="470",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-benchmark-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="471",
            session_date="2024-01-02",
        )

        without_quote = build_snapshot(self.root, write=False)
        paper = without_quote["portfolios"]["paper"]
        self.assertEqual(
            paper["daily"][0]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )
        self.assertEqual(paper["daily"][0]["missing_symbols"], ["SPY"])
        self.assertIsNone(paper["holdings"][0]["current_price"])

        self.append(
            "QUOTE",
            "market-spy-quote-2024-01-02",
            "2024-01-02T21:00:01Z",
            symbol="SPY",
            close="471",
            session_date="2024-01-02",
        )
        with_quote = build_snapshot(self.root, write=False)
        paper = with_quote["portfolios"]["paper"]
        self.assertEqual(paper["daily"][0]["data_status"], "OK")
        self.assertEqual(paper["holdings"][0]["current_price"], "471")

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

    def test_zero_return_denominator_creates_a_new_performance_segment(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="0",
        )
        self.append(
            "CASH_FLOW",
            "paper-funding",
            "2024-01-04T14:00:00Z",
            amount="100",
        )
        for day, close in (
            ("2024-01-02", "470"),
            ("2024-01-03", "471"),
            ("2024-01-04", "472"),
        ):
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
        self.assertEqual(daily[0]["daily_return"], "0")
        self.assertEqual(daily[1]["data_status"], "INSUFFICIENT_DATA")
        self.assertIsNone(daily[1]["daily_return"])
        self.assertIsNone(daily[1]["segment_id"])
        self.assertEqual(daily[2]["data_status"], "OK")
        self.assertEqual(daily[2]["segment_id"], 2)
        self.assertIsNone(daily[2]["daily_return"])
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["data_status"],
            "INSUFFICIENT_DATA",
        )

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

    def test_sharpe_uses_trading_session_returns_not_weekend_zeros(self) -> None:
        trading_daily: list[dict[str, object]] = []
        calendar_daily: list[dict[str, object]] = []
        cursor = date(2024, 2, 1)
        cumulative = Decimal("0")
        sessions = 0

        while sessions < 20:
            if is_nyse_session(cursor):
                daily_return = (
                    Decimal("0")
                    if sessions == 0
                    else Decimal("0.01")
                    if sessions % 2
                    else Decimal("-0.005")
                )
                cumulative = (
                    Decimal("0")
                    if sessions == 0
                    else (1 + cumulative) * (1 + daily_return) - 1
                )
                point = {
                    "date": cursor.isoformat(),
                    "daily_return": daily_return,
                    "cumulative_return": cumulative,
                    "data_status": "OK",
                }
                trading_daily.append(point)
                calendar_daily.append(point)
                sessions += 1
            else:
                calendar_daily.append(
                    {
                        "date": cursor.isoformat(),
                        "daily_return": Decimal("0"),
                        "cumulative_return": cumulative,
                        "data_status": "OK",
                    }
                )
            cursor += timedelta(days=1)

        replay = SimpleNamespace(
            realized_pnl_total=Decimal("0"),
            win_rate=None,
            closed_episodes=[],
        )
        trading_metrics = _metrics(trading_daily, replay)
        calendar_metrics = _metrics(calendar_daily, replay)

        self.assertIsNotNone(trading_metrics["sharpe_ratio"])
        self.assertEqual(
            calendar_metrics["sharpe_ratio"],
            trading_metrics["sharpe_ratio"],
        )


if __name__ == "__main__":
    unittest.main()
